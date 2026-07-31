"""Package the Agent_Lambda as a CONTAINER IMAGE and deploy it OUT of the VPC
(ap-south-1).

WHY a container image (not a zip)
---------------------------------
The Strands Agents SDK + MCP streamable-HTTP client + boto3 dependency closure
exceeds the comfortable Lambda zip/layer size, so the Agent_Lambda ships as a
container image (``PackageType=Image``) per the design's Deployment Approach.

WHY out of the VPC
------------------
The Agent_Lambda only calls Bedrock + AgentCore Gateway/Memory + WebSocket
``@connections`` — all regional public-AWS endpoints. It never touches the
private Aurora DB, so it needs NO VPC attachment and NO NAT gateway.

End-to-end, idempotent:
  1. Build the image with finch (``--platform linux/amd64`` for the Lambda
     runtime) from the repo root using ``infra/agent.Dockerfile``.
  2. Create/reuse the ECR repo ``residency-chatbot-agent``; docker-login finch to
     ECR; tag + push the image (digest captured).
  3. Ensure the Agent_Lambda IAM role has AgentCore observability permissions
     (CloudWatch Logs is already present via AWSLambdaBasicExecutionRole; this
     adds X-Ray trace + CloudWatch EMF metric publishing for AgentCore traces).
  4. Create/update the ``residency-chatbot-agent`` Lambda as ``PackageType=Image``,
     role = agent_lambda_role_arn, NO VpcConfig, env wired from network_ids.json
     (GATEWAY_URL, MEMORY_ID, MODEL_ID, REGION, ACTOR_ID), Timeout=120s,
     MemorySize=1024MB, TracingConfig=Active (observability).
  5. Ensure the CloudWatch log group exists (Req 12.1).
  6. Save ``agent_lambda_arn``, ``agent_ecr_repo_uri``, ``agent_image_uri`` back
     to network_ids.json.
  7. Optional smoke invoke (``--smoke "question"``) — posts nothing (WS unset),
     logs the answer, returns the payload.

Run:  uv run python infra/deploy_agent.py [--smoke "how many applications are installation pending?"]

Requires: finch installed and its VM initialized (``finch vm start``); local AWS
identity with ecr/lambda/iam/logs permissions.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

import boto3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import ACCOUNT, MODEL_ID, PROJECT, REGION, TAGS_MAP, load_ids, save_ids  # noqa: E402

HERE = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(HERE, ".."))
DOCKERFILE = os.path.join(HERE, "agent.Dockerfile")

FUNCTION_NAME = f"{PROJECT}-agent"
ECR_REPO = f"{PROJECT}-agent"
IMAGE_TAG = "latest"
CONTAINER_TOOL = os.environ.get("CONTAINER_TOOL", "finch")

TIMEOUT_S = 120
MEMORY_MB = 1024

# AgentCore observability: X-Ray trace publishing + CloudWatch EMF metrics.
# (CloudWatch Logs themselves come from the attached AWSLambdaBasicExecutionRole.)
OBSERVABILITY_POLICY_NAME = "agent-observability"

lam = boto3.client("lambda", region_name=REGION)
ecr = boto3.client("ecr", region_name=REGION)
iam = boto3.client("iam", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)
sts = boto3.client("sts", region_name=REGION)


# All container-tool work (login/build/tag/push) goes through the static
# infra/container_tool.sh runner so every subprocess argv in this module is a
# fully literal list — no dynamic value ever reaches a Python-built command
# line. Dynamic inputs travel as validated environment variables; the ECR
# password travels on stdin only.
TOOL_RUNNER = os.path.join(HERE, "container_tool.sh")


def _tool_env(**extra: str) -> dict:
    env = dict(os.environ)
    env.update(extra)
    return env


# --------------------------------------------------------------------------- ECR
def ensure_ecr_repo() -> str:
    """Create (or reuse) the ECR repo; return its repositoryUri."""
    try:
        resp = ecr.describe_repositories(repositoryNames=[ECR_REPO])
        uri = resp["repositories"][0]["repositoryUri"]
        print(f"[skip] ECR repo exists: {uri}")
    except ecr.exceptions.RepositoryNotFoundException:
        resp = ecr.create_repository(
            repositoryName=ECR_REPO,
            imageScanningConfiguration={"scanOnPush": True},
            tags=[{"Key": k, "Value": v} for k, v in TAGS_MAP.items()],
        )
        uri = resp["repository"]["repositoryUri"]
        print(f"[create] ECR repo: {uri}")
    return uri


def ecr_login() -> str:
    """Docker-login the container tool to ECR; return the registry host."""
    registry = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com"
    token = ecr.get_authorization_token()["authorizationData"][0]
    import base64

    user_pass = base64.b64decode(token["authorizationToken"]).decode()
    _user, password = user_pass.split(":", 1)
    print(f"[run] container_tool.sh login ({registry})")
    subprocess.run(
        ["bash", TOOL_RUNNER, "login"],
        check=True,
        input=password.encode(),
        env=_tool_env(ECR_REGISTRY=registry),
    )
    print(f"[ok] {CONTAINER_TOOL} logged in to {registry}")
    return registry


def build_and_push(repo_uri: str) -> str:
    """Build the image with finch and push it; return the pushed image URI (by digest)."""
    local_tag = f"{ECR_REPO}:{IMAGE_TAG}"
    remote_tag = f"{repo_uri}:{IMAGE_TAG}"

    # Build for the Lambda runtime architecture (x86_64), then tag + push.
    print(f"[run] container_tool.sh build ({local_tag})")
    subprocess.run(
        ["bash", TOOL_RUNNER, "build"],
        check=True,
        env=_tool_env(DOCKERFILE=DOCKERFILE, LOCAL_TAG=local_tag, BUILD_CONTEXT=REPO_ROOT),
    )
    print(f"[run] container_tool.sh push ({remote_tag})")
    subprocess.run(
        ["bash", TOOL_RUNNER, "push"],
        check=True,
        env=_tool_env(LOCAL_TAG=local_tag, REMOTE_TAG=remote_tag),
    )

    # Resolve the pushed digest so the Lambda pins an immutable image.
    digest = ecr.describe_images(
        repositoryName=ECR_REPO,
        imageIds=[{"imageTag": IMAGE_TAG}],
    )["imageDetails"][0]["imageDigest"]
    image_uri = f"{repo_uri}@{digest}"
    print(f"[ok] pushed image: {image_uri}")
    return image_uri


# --------------------------------------------------------------------------- IAM
def ensure_observability_policy(role_arn: str) -> None:
    """Add X-Ray + CloudWatch EMF metric permissions for AgentCore observability.

    Idempotent: put_role_policy overwrites the same inline policy. CloudWatch
    Logs write perms already come from the attached AWSLambdaBasicExecutionRole.
    """
    role_name = role_arn.split("/")[-1]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "XRayTracePublish",
                "Effect": "Allow",
                "Action": [
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                "Resource": "*",
            },
            {
                "Sid": "CloudWatchEMFMetrics",
                "Effect": "Allow",
                "Action": ["cloudwatch:PutMetricData"],
                "Resource": "*",
                "Condition": {
                    "StringEquals": {
                        "cloudwatch:namespace": ["bedrock-agentcore", "ResidencyChatbot"]
                    }
                },
            },
        ],
    }
    iam.put_role_policy(
        RoleName=role_name,
        PolicyName=OBSERVABILITY_POLICY_NAME,
        PolicyDocument=json.dumps(policy),
    )
    print(f"[ok] observability inline policy {OBSERVABILITY_POLICY_NAME} on {role_name}")


# ------------------------------------------------------------------------ Lambda
def _env(ids: dict) -> dict:
    return {
        "Variables": {
            "REGION": REGION,
            "MODEL_ID": ids.get("model_id", MODEL_ID),
            "GATEWAY_URL": ids["gateway_url"],
            "MEMORY_ID": ids["memory_id"],
            "ACTOR_ID": "demo-user",
            # @connections callback endpoint of the PRODUCTION WebSocket API so the
            # handler can POST the answer back to the originating connection. Empty
            # for the REST demo path; set later by provision_websocket.py.
            "WS_CALLBACK_URL": ids.get("ws_callback_url", ""),
        }
    }


def deploy(ids: dict, image_uri: str) -> str:
    role_arn = ids["agent_lambda_role_arn"]
    env = _env(ids)
    try:
        lam.get_function(FunctionName=FUNCTION_NAME)
        print(f"[update] {FUNCTION_NAME}")
        lam.update_function_code(FunctionName=FUNCTION_NAME, ImageUri=image_uri)
        _wait_updated()
        lam.update_function_configuration(
            FunctionName=FUNCTION_NAME,
            Role=role_arn,
            Timeout=TIMEOUT_S,
            MemorySize=MEMORY_MB,
            Environment=env,
            TracingConfig={"Mode": "Active"},
            # No VpcConfig — Agent_Lambda runs OUT of the VPC.
        )
        _wait_updated()
    except lam.exceptions.ResourceNotFoundException:
        print(f"[create] {FUNCTION_NAME} (PackageType=Image, OUT of VPC)")
        for attempt in range(6):
            try:
                lam.create_function(
                    FunctionName=FUNCTION_NAME,
                    Role=role_arn,
                    PackageType="Image",
                    Code={"ImageUri": image_uri},
                    Timeout=TIMEOUT_S,
                    MemorySize=MEMORY_MB,
                    Environment=env,
                    TracingConfig={"Mode": "Active"},
                    Tags=TAGS_MAP,
                )
                break
            except lam.exceptions.InvalidParameterValueException as e:
                if "cannot be assumed" in str(e) and attempt < 5:
                    print(f"  role not ready, retrying ({attempt + 1})...")
                    time.sleep(5)
                    continue
                raise
        _wait_active()

    cfg = lam.get_function(FunctionName=FUNCTION_NAME)["Configuration"]
    arn = cfg["FunctionArn"]
    print(f"[ok] {FUNCTION_NAME} -> {arn}")
    print(f"     PackageType={cfg.get('PackageType')}  VpcConfig={cfg.get('VpcConfig', {}).get('VpcId', 'none')}")
    print(f"     Tracing={cfg.get('TracingConfig', {}).get('Mode')}  Timeout={cfg['Timeout']}s  Memory={cfg['MemorySize']}MB")
    return arn


def _wait_active() -> None:
    lam.get_waiter("function_active_v2").wait(FunctionName=FUNCTION_NAME)


def _wait_updated() -> None:
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)


def ensure_log_group() -> str:
    """Ensure the Agent_Lambda CloudWatch log group exists (Req 12.1)."""
    lg = f"/aws/lambda/{FUNCTION_NAME}"
    try:
        logs.create_log_group(logGroupName=lg)
        print(f"[create] log group {lg}")
    except logs.exceptions.ResourceAlreadyExistsException:
        print(f"[skip] log group exists {lg}")
    return lg


def smoke(question: str) -> None:
    print(f"\n=== smoke invoke: {question!r} ===")
    payload = {"connectionId": "smoke-deploy-1", "question": question}
    resp = lam.invoke(
        FunctionName=FUNCTION_NAME,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    body = resp["Payload"].read().decode()
    if resp.get("FunctionError"):
        print(f"[smoke] FunctionError={resp['FunctionError']}")
        print(body)
        return
    print(f"[smoke] response: {json.dumps(json.loads(body), default=str, indent=2)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", nargs="?", const="how many applications are installation pending?",
                    help="run a smoke invoke with the given question after deploy")
    ap.add_argument("--skip-build", action="store_true",
                    help="reuse the latest pushed image (skip finch build/push)")
    args = ap.parse_args()

    ids = load_ids()
    print(f"Region {REGION}  Account {ACCOUNT}\n")
    print(f"Identity: {sts.get_caller_identity()['Arn']}\n")

    repo_uri = ensure_ecr_repo()
    if args.skip_build:
        digest = ecr.describe_images(
            repositoryName=ECR_REPO, imageIds=[{"imageTag": IMAGE_TAG}]
        )["imageDetails"][0]["imageDigest"]
        image_uri = f"{repo_uri}@{digest}"
        print(f"[skip-build] reusing {image_uri}")
    else:
        ecr_login()
        image_uri = build_and_push(repo_uri)

    ensure_observability_policy(ids["agent_lambda_role_arn"])
    time.sleep(5)  # let the inline policy propagate before invoke

    arn = deploy(ids, image_uri)
    log_group = ensure_log_group()

    ids.update({
        "agent_lambda_arn": arn,
        "agent_ecr_repo_uri": repo_uri,
        "agent_image_uri": image_uri,
        "agent_log_group": log_group,
        "model_id": MODEL_ID,
    })
    save_ids(ids)
    print("\nSaved ids to network_ids.json")
    print(f"  agent_lambda_arn   = {arn}")
    print(f"  agent_ecr_repo_uri = {repo_uri}")
    print(f"  agent_image_uri    = {image_uri}")

    if args.smoke:
        smoke(args.smoke)


if __name__ == "__main__":
    main()
