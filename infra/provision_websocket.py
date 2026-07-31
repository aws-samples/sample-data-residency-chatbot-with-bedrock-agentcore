r"""Provision the API Gateway WebSocket API for the chatbot (ap-south-1) —
the PRODUCTION transport.

Architecture (design Req 9, 1.5, 1.11)
--------------------------------------
A regional WebSocket API (NO CloudFront) is the browser's entry point:

  Browser_UI --wss--> WebSocket_API --invoke--> Agent_Lambda
                          |  $connect    -> connect Lambda  (PutItem connections)
                          |  $disconnect -> disconnect Lambda (DeleteItem)
                          |  sendMessage -> Agent_Lambda (AWS_PROXY)
                          \  $default    -> Agent_Lambda (AWS_PROXY, fallback)

RouteSelectionExpression = "$request.body.action": the browser sends
``{action: "sendMessage", question: "..."}``, so the ``sendMessage`` route is
selected; anything unmatched falls through to ``$default`` (also the agent).

The Agent_Lambda's @connections callback URL (https) is wired back into its
``WS_CALLBACK_URL`` env var so it can POST answers to the originating connection.

Idempotent end-to-end (skip-if-exists like the other infra scripts):
  1. Reuse/create a tiny IAM role for the connect/disconnect helper Lambdas
     (DynamoDB PutItem/DeleteItem on the connections table + basic logs).
  2. Create/update two inline (zip) helper Lambdas: connect + disconnect.
  3. Create/reuse the WEBSOCKET API; ensure the 4 integrations + routes.
  4. Add resource-based permissions so API Gateway can invoke all 3 Lambdas
     (principal apigateway.amazonaws.com, SourceArn execute-api/<apiId>/*/*).
  5. Create/reuse the ``prod`` stage with AutoDeploy.
  6. Compute wss:// (browser) and https:// (@connections) endpoints; update the
     Agent_Lambda WS_CALLBACK_URL (merging existing env vars).
  7. Save ids to network_ids.json and print a verification summary (get_routes).

Run:  uv run python infra/provision_websocket.py
Requires the local AWS identity to have apigatewayv2/lambda/iam permissions.
"""
from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import ACCOUNT, PROJECT, REGION, TAGS, TAGS_MAP, load_ids, save_ids  # noqa: E402

HERE = os.path.dirname(__file__)

WS_API_NAME = f"{PROJECT}-ws"
WS_STAGE = "prod"
ROUTE_SELECTION = "$request.body.action"

CONNECT_FN = f"{PROJECT}-ws-connect"
DISCONNECT_FN = f"{PROJECT}-ws-disconnect"
WS_HELPER_ROLE = f"{PROJECT}-ws-helper-role"
RUNTIME = "python3.12"

# TTL for connection items (24h) so stale rows self-clean.
CONN_TTL_SECONDS = 24 * 60 * 60

LAMBDA_TRUST = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
})

apigw = boto3.client("apigatewayv2", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)
iam = boto3.client("iam", region_name=REGION)
sts = boto3.client("sts", region_name=REGION)


# ---------------------------------------------------------------- helper Lambdas
# Inline handler source for the connect Lambda: store connectionId in DynamoDB.
_CONNECT_SRC = '''\
import os, time, boto3

TABLE = os.environ["CONNECTIONS_TABLE"]
TTL_SECONDS = int(os.environ.get("CONN_TTL_SECONDS", "86400"))
ddb = boto3.client("dynamodb", region_name=os.environ.get("REGION", "ap-south-1"))


def handler(event, context):
    cid = (event.get("requestContext") or {}).get("connectionId")
    if not cid:
        return {"statusCode": 400, "body": "missing connectionId"}
    now = int(time.time())
    ddb.put_item(
        TableName=TABLE,
        Item={
            "connectionId": {"S": cid},
            "sessionId": {"S": cid},
            "connectedAt": {"N": str(now)},
            "ttl": {"N": str(now + TTL_SECONDS)},
        },
    )
    return {"statusCode": 200, "body": "connected"}
'''

# Inline handler source for the disconnect Lambda: delete the connectionId item.
_DISCONNECT_SRC = '''\
import os, boto3

TABLE = os.environ["CONNECTIONS_TABLE"]
ddb = boto3.client("dynamodb", region_name=os.environ.get("REGION", "ap-south-1"))


def handler(event, context):
    cid = (event.get("requestContext") or {}).get("connectionId")
    if cid:
        ddb.delete_item(TableName=TABLE, Key={"connectionId": {"S": cid}})
    return {"statusCode": 200, "body": "disconnected"}
'''


def _zip_single(source: str, filename: str = "handler.py") -> bytes:
    """Zip a single inline handler module into a deployable Lambda package."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, source)
    return buf.getvalue()


def ensure_helper_role(ids: dict) -> str:
    """Create (or reuse) the IAM role for the connect/disconnect helper Lambdas.

    Grants DynamoDB PutItem/DeleteItem on the connections table + basic logs.
    """
    try:
        iam.create_role(
            RoleName=WS_HELPER_ROLE,
            AssumeRolePolicyDocument=LAMBDA_TRUST,
            Description="chatbot WS connect/disconnect helper Lambdas",
            Tags=TAGS,
        )
        print(f"[create] IAM role: {WS_HELPER_ROLE}")
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"[skip] IAM role exists: {WS_HELPER_ROLE}")

    iam.attach_role_policy(
        RoleName=WS_HELPER_ROLE,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    iam.put_role_policy(
        RoleName=WS_HELPER_ROLE,
        PolicyName="ws-connections-ddb",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": "ConnectionsTableWrite",
                "Effect": "Allow",
                "Action": ["dynamodb:PutItem", "dynamodb:DeleteItem"],
                "Resource": ids["connections_table_arn"],
            }],
        }),
    )
    arn = iam.get_role(RoleName=WS_HELPER_ROLE)["Role"]["Arn"]
    print(f"         role arn: {arn}")
    return arn


def _deploy_helper(fn_name: str, source: str, role_arn: str, ids: dict) -> str:
    """Create/update a tiny inline helper Lambda; return its ARN."""
    zip_bytes = _zip_single(source)
    env = {
        "Variables": {
            "REGION": REGION,
            "CONNECTIONS_TABLE": ids["connections_table"],
            "CONN_TTL_SECONDS": str(CONN_TTL_SECONDS),
        }
    }
    try:
        lam.get_function(FunctionName=fn_name)
        print(f"[update] {fn_name}")
        lam.update_function_code(FunctionName=fn_name, ZipFile=zip_bytes)
        lam.get_waiter("function_updated_v2").wait(FunctionName=fn_name)
        lam.update_function_configuration(
            FunctionName=fn_name,
            Runtime=RUNTIME,
            Handler="handler.handler",
            Role=role_arn,
            Timeout=15,
            MemorySize=128,
            Environment=env,
        )
        lam.get_waiter("function_updated_v2").wait(FunctionName=fn_name)
    except lam.exceptions.ResourceNotFoundException:
        print(f"[create] {fn_name}")
        for attempt in range(6):
            try:
                lam.create_function(
                    FunctionName=fn_name,
                    Runtime=RUNTIME,
                    Role=role_arn,
                    Handler="handler.handler",
                    Code={"ZipFile": zip_bytes},
                    Timeout=15,
                    MemorySize=128,
                    Environment=env,
                    Tags=TAGS_MAP,
                )
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "cannot be assumed" in str(e) and attempt < 5:
                    print(f"  role not ready, retrying ({attempt + 1})...")
                    time.sleep(5)
                    continue
                raise
        lam.get_waiter("function_active_v2").wait(FunctionName=fn_name)
    arn = lam.get_function(FunctionName=fn_name)["Configuration"]["FunctionArn"]
    print(f"         {fn_name} -> {arn}")
    return arn


# ------------------------------------------------------------------ WebSocket API
def ensure_ws_api() -> str:
    """Create (or reuse) the WEBSOCKET API; return its apiId."""
    paginator = apigw.get_apis()
    for api in paginator.get("Items", []):
        if api.get("Name") == WS_API_NAME and api.get("ProtocolType") == "WEBSOCKET":
            print(f"[skip] WebSocket API exists: {api['ApiId']}")
            return api["ApiId"]
    resp = apigw.create_api(
        Name=WS_API_NAME,
        ProtocolType="WEBSOCKET",
        RouteSelectionExpression=ROUTE_SELECTION,
        Tags=TAGS_MAP,
    )
    print(f"[create] WebSocket API: {resp['ApiId']}")
    return resp["ApiId"]


def _integration_uri(fn_arn: str) -> str:
    return (
        f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/"
        f"{fn_arn}/invocations"
    )


def ensure_integration(api_id: str, fn_arn: str) -> str:
    """Create (or reuse) an AWS_PROXY integration to the given Lambda; return its id."""
    uri = _integration_uri(fn_arn)
    existing = apigw.get_integrations(ApiId=api_id).get("Items", [])
    for integ in existing:
        if integ.get("IntegrationUri") == uri:
            return integ["IntegrationId"]
    resp = apigw.create_integration(
        ApiId=api_id,
        IntegrationType="AWS_PROXY",
        IntegrationUri=uri,
        IntegrationMethod="POST",
        PayloadFormatVersion="1.0",
    )
    print(f"[create] integration -> {fn_arn.split(':')[-1]} ({resp['IntegrationId']})")
    return resp["IntegrationId"]


def ensure_route(api_id: str, route_key: str, integration_id: str) -> None:
    """Create (or reuse) a route bound to the given integration target."""
    target = f"integrations/{integration_id}"
    for route in apigw.get_routes(ApiId=api_id).get("Items", []):
        if route.get("RouteKey") == route_key:
            if route.get("Target") != target:
                apigw.update_route(ApiId=api_id, RouteId=route["RouteId"], Target=target)
                print(f"[update] route {route_key} -> {target}")
            else:
                print(f"[skip] route exists: {route_key}")
            return
    apigw.create_route(ApiId=api_id, RouteKey=route_key, Target=target)
    print(f"[create] route {route_key} -> {target}")


def ensure_stage(api_id: str) -> None:
    """Create (or reuse) the deploy stage with AutoDeploy enabled."""
    try:
        apigw.get_stage(ApiId=api_id, StageName=WS_STAGE)
        print(f"[skip] stage exists: {WS_STAGE}")
    except apigw.exceptions.NotFoundException:
        apigw.create_stage(
            ApiId=api_id,
            StageName=WS_STAGE,
            AutoDeploy=True,
            Tags=TAGS_MAP,
        )
        print(f"[create] stage {WS_STAGE} (AutoDeploy)")


def add_invoke_permission(api_id: str, fn_name: str) -> None:
    """Allow API Gateway to invoke the Lambda (idempotent)."""
    source_arn = f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{api_id}/*/*"
    statement_id = f"apigw-ws-{api_id}"
    try:
        lam.add_permission(
            FunctionName=fn_name,
            StatementId=statement_id,
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
            SourceArn=source_arn,
        )
        print(f"[create] invoke permission on {fn_name} (SourceArn {source_arn})")
    except lam.exceptions.ResourceConflictException:
        print(f"[skip] invoke permission exists on {fn_name}")


def update_agent_callback_url(ids: dict, callback_url: str) -> None:
    """Set WS_CALLBACK_URL on the Agent_Lambda, merging existing env vars."""
    fn = ids["agent_lambda_arn"].split(":")[-1]
    cfg = lam.get_function_configuration(FunctionName=fn)
    env_vars = dict(cfg.get("Environment", {}).get("Variables", {}))
    if env_vars.get("WS_CALLBACK_URL") == callback_url:
        print(f"[skip] WS_CALLBACK_URL already set on {fn}")
        return
    env_vars["WS_CALLBACK_URL"] = callback_url
    lam.update_function_configuration(
        FunctionName=fn,
        Environment={"Variables": env_vars},
    )
    lam.get_waiter("function_updated_v2").wait(FunctionName=fn)
    print(f"[ok] WS_CALLBACK_URL set on {fn} -> {callback_url}")


def verify(api_id: str) -> list[str]:
    """Print a verification summary; return the list of route keys."""
    api = apigw.get_api(ApiId=api_id)
    routes = [r["RouteKey"] for r in apigw.get_routes(ApiId=api_id).get("Items", [])]
    print("\n=== Verification ===")
    print(f"  API id          : {api_id}")
    print(f"  ProtocolType    : {api['ProtocolType']}")
    print(f"  RouteSelection  : {api.get('RouteSelectionExpression')}")
    print(f"  Region          : {REGION} (regional endpoint, NO CloudFront)")
    print(f"  Routes ({len(routes)})     : {sorted(routes)}")
    expected = {"$connect", "$disconnect", "$default", "sendMessage"}
    missing = expected - set(routes)
    if missing:
        print(f"  [warn] missing routes: {sorted(missing)}")
    else:
        print("  [ok] all 4 expected routes present")
    return routes


def main() -> None:
    ids = load_ids()
    print(f"Region {REGION}  Account {ACCOUNT}\n")
    print(f"Identity: {sts.get_caller_identity()['Arn']}\n")

    if "agent_lambda_arn" not in ids:
        raise SystemExit("network_ids.json missing agent_lambda_arn (run deploy_agent first)")

    # 1) Helper role + connect/disconnect Lambdas.
    helper_role = ensure_helper_role(ids)
    time.sleep(5)  # let the new role propagate before Lambda create
    connect_arn = _deploy_helper(CONNECT_FN, _CONNECT_SRC, helper_role, ids)
    disconnect_arn = _deploy_helper(DISCONNECT_FN, _DISCONNECT_SRC, helper_role, ids)
    print()

    # 2) WebSocket API + integrations + routes.
    api_id = ensure_ws_api()
    agent_arn = ids["agent_lambda_arn"]
    connect_integ = ensure_integration(api_id, connect_arn)
    disconnect_integ = ensure_integration(api_id, disconnect_arn)
    agent_integ = ensure_integration(api_id, agent_arn)

    ensure_route(api_id, "$connect", connect_integ)
    ensure_route(api_id, "$disconnect", disconnect_integ)
    ensure_route(api_id, "sendMessage", agent_integ)
    ensure_route(api_id, "$default", agent_integ)
    print()

    # 3) Resource-based invoke permissions for all 3 Lambdas.
    add_invoke_permission(api_id, CONNECT_FN)
    add_invoke_permission(api_id, DISCONNECT_FN)
    add_invoke_permission(api_id, agent_arn.split(":")[-1])
    print()

    # 4) Stage.
    ensure_stage(api_id)

    # 5) Endpoints.
    ws_endpoint = f"wss://{api_id}.execute-api.{REGION}.amazonaws.com/{WS_STAGE}"
    callback_url = f"https://{api_id}.execute-api.{REGION}.amazonaws.com/{WS_STAGE}"
    print(f"\n  wss endpoint  (browser)     : {ws_endpoint}")
    print(f"  https callback (@connections): {callback_url}")

    # 6) Wire the @connections callback URL into the Agent_Lambda.
    print()
    update_agent_callback_url(ids, callback_url)

    # 7) Persist ids.
    ids.update({
        "ws_api_id": api_id,
        "ws_stage": WS_STAGE,
        "ws_endpoint": ws_endpoint,
        "ws_callback_url": callback_url,
        "ws_connect_lambda_arn": connect_arn,
        "ws_disconnect_lambda_arn": disconnect_arn,
        "ws_lambda_role_arn": helper_role,
    })
    save_ids(ids)
    print("\nSaved ids to network_ids.json")

    # 8) Verify.
    verify(api_id)
    print(f"\nWebSocket endpoint: {ws_endpoint}")


if __name__ == "__main__":
    main()
