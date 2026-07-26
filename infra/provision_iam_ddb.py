"""Provision the DynamoDB connections table and the least-privilege IAM roles for
the MNRE chatbot (ap-south-1).

Idempotent. Creates:
  - DynamoDB table `mnre-chatbot-connections` (PK connectionId, PAY_PER_REQUEST, TTL on `ttl`)
    — used by the production WebSocket transport ($connect/$disconnect helpers).
  - IAM role `mnre-chatbot-tool-lambda-role`  (Tool_Lambda, runs IN VPC, psycopg)
  - IAM role `mnre-chatbot-agent-lambda-role` (Agent_Lambda, OUT of VPC)

Both API Gateway transports (REST for the demo, WebSocket for production) invoke
the Agent_Lambda via a resource-based permission (lambda:AddPermission, principal
apigateway.amazonaws.com) added at API-wiring time — NOT via an assumed role. So
no separate integration role is created here.

Reads infra/network_ids.json (needs db_master_secret_arn). Appends created
table/role identifiers back to that file.

Run:  uv run python infra/provision_iam_ddb.py
"""
from __future__ import annotations

import json
import os
import sys

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (  # noqa: E402
    ACCOUNT,
    MODEL_ARN,
    PROJECT,
    REGION,
    TAGS,
    load_ids,
    save_ids,
)

CONNECTIONS_TABLE = f"{PROJECT}-connections"
TOOL_ROLE = f"{PROJECT}-tool-lambda-role"
AGENT_ROLE = f"{PROJECT}-agent-lambda-role"

# App DB-user secret created later (bootstrap) — scope reads to this name prefix.
APP_SECRET_PREFIX_ARN = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{PROJECT}-*"

LAMBDA_TRUST = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
})

ddb = boto3.client("dynamodb", region_name=REGION)
iam = boto3.client("iam", region_name=REGION)


# --------------------------------------------------------------------------- DDB
def ensure_connections_table():
    try:
        ddb.create_table(
            TableName=CONNECTIONS_TABLE,
            AttributeDefinitions=[{"AttributeName": "connectionId", "AttributeType": "S"}],
            KeySchema=[{"AttributeName": "connectionId", "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
            Tags=TAGS,
        )
        print(f"[create] DynamoDB table: {CONNECTIONS_TABLE}")
    except ddb.exceptions.ResourceInUseException:
        print(f"[skip] DynamoDB table exists: {CONNECTIONS_TABLE}")

    ddb.get_waiter("table_exists").wait(TableName=CONNECTIONS_TABLE)

    ttl = ddb.describe_time_to_live(TableName=CONNECTIONS_TABLE)["TimeToLiveDescription"]
    if ttl.get("TimeToLiveStatus") in ("ENABLED", "ENABLING"):
        print(f"[skip] TTL already {ttl['TimeToLiveStatus']} on {CONNECTIONS_TABLE}")
    else:
        ddb.update_time_to_live(
            TableName=CONNECTIONS_TABLE,
            TimeToLiveSpecification={"Enabled": True, "AttributeName": "ttl"},
        )
        print(f"[create] TTL enabled (attr=ttl) on {CONNECTIONS_TABLE}")

    desc = ddb.describe_table(TableName=CONNECTIONS_TABLE)["Table"]
    print(f"         status={desc['TableStatus']} arn={desc['TableArn']}")
    return desc["TableArn"]


# --------------------------------------------------------------------------- IAM
def _ensure_role(name, description):
    try:
        iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=LAMBDA_TRUST,
            Description=description,
            Tags=TAGS,
        )
        print(f"[create] IAM role: {name}")
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"[skip] IAM role exists: {name}")
    return iam.get_role(RoleName=name)["Role"]["Arn"]


def _attach_managed(role, policy_arn):
    iam.attach_role_policy(RoleName=role, PolicyArn=policy_arn)
    print(f"         attached managed: {policy_arn.split('/')[-1]}")


def _put_inline(role, policy_name, document):
    iam.put_role_policy(
        RoleName=role,
        PolicyName=policy_name,
        PolicyDocument=json.dumps(document),
    )
    print(f"         inline policy: {policy_name}")


def ensure_tool_role(ids):
    """Tool_Lambda: runs IN VPC, reads Aurora secret, connects via psycopg."""
    arn = _ensure_role(TOOL_ROLE, "MNRE chatbot Tool_Lambda (in-VPC, read-only DB)")
    _attach_managed(TOOL_ROLE, "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole")

    secrets_resources = [ids["db_master_secret_arn"], APP_SECRET_PREFIX_ARN]
    _put_inline(TOOL_ROLE, "mnre-tool-secrets-and-logs", {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ReadDbSecrets",
                "Effect": "Allow",
                "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                "Resource": secrets_resources,
            },
            {
                "Sid": "CloudWatchLogs",
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{PROJECT}-*",
            },
        ],
    })
    return arn


def ensure_agent_role():
    """Agent_Lambda: OUT of VPC; Bedrock + AgentCore (gateway/memory) + WS @connections."""
    arn = _ensure_role(AGENT_ROLE, "MNRE chatbot Agent_Lambda (Bedrock + AgentCore + WS)")
    _attach_managed(AGENT_ROLE, "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")

    _put_inline(AGENT_ROLE, "mnre-agent-bedrock-agentcore-ws", {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockInvokeInRegion",
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"],
                # The configured ON_DEMAND model ARN; also allow any ap-south-1
                # foundation model so a same-region fallback needs no policy change.
                "Resource": [
                    MODEL_ARN,
                    f"arn:aws:bedrock:{REGION}::foundation-model/*",
                ],
            },
            {
                "Sid": "AgentCoreGatewayInvoke",
                "Effect": "Allow",
                "Action": ["bedrock-agentcore:InvokeGateway"],
                # Tightened to the specific gateway ARN by agentcore_setup.py once
                # the gateway is created (it is unknown at this step).
                "Resource": "*",
            },
            {
                "Sid": "AgentCoreMemoryDataPlane",
                "Effect": "Allow",
                "Action": [
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:ListSessions",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:ListMemoryRecords",
                    "bedrock-agentcore:GetMemoryRecord",
                ],
                "Resource": "*",
            },
            {
                "Sid": "WebSocketManageConnections",
                "Effect": "Allow",
                "Action": ["execute-api:ManageConnections"],
                # Production WebSocket transport: the agent POSTs answers back to
                # the originating connection via @connections.
                "Resource": f"arn:aws:execute-api:{REGION}:{ACCOUNT}:*/*",
            },
            {
                # The WebSocket sendMessage route invokes the agent synchronously,
                # but API Gateway's WS integration caps at 29s. So the route handler
                # returns 200 immediately and re-invokes ITSELF asynchronously
                # (InvocationType=Event) to run the agent loop and post the answer
                # via @connections. This permission allows that self-invoke.
                "Sid": "SelfAsyncInvoke",
                "Effect": "Allow",
                "Action": ["lambda:InvokeFunction"],
                "Resource": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{PROJECT}-agent",
            },
        ],
    })
    return arn


def main():
    ids = load_ids()
    print(f"Region {REGION}  Account {ACCOUNT}\n")

    if "db_master_secret_arn" not in ids:
        raise SystemExit("network_ids.json missing db_master_secret_arn (run provision_aurora/wait first)")

    table_arn = ensure_connections_table()
    print()
    tool_arn = ensure_tool_role(ids)
    print()
    agent_arn = ensure_agent_role()

    ids.update({
        "connections_table": CONNECTIONS_TABLE,
        "connections_table_arn": table_arn,
        "tool_lambda_role_arn": tool_arn,
        "agent_lambda_role_arn": agent_arn,
    })
    save_ids(ids)
    print(f"\nSaved ids to network_ids.json")
    print(f"  connections_table     = {CONNECTIONS_TABLE}")
    print(f"  tool_lambda_role_arn  = {tool_arn}")
    print(f"  agent_lambda_role_arn = {agent_arn}")


if __name__ == "__main__":
    main()
