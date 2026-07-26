"""Provision a regional REST API (API Gateway v1) in front of the Agent_Lambda
as the browser's entry point — replacing the fragile WebSocket transport.

WHY REST (not WebSocket, not a public Function URL)
---------------------------------------------------
  - The WebSocket route integration timed out at 29s on slow aggregation queries
    and returned 502 even with async dispatch.
  - A public Lambda Function URL (AuthType=NONE) is blocked by an org SCP in this
    governance account (403 Forbidden).
A regional REST API with a Lambda PROXY integration gives a plain HTTPS
request/response endpoint: the browser does a normal fetch() POST and gets the
answer back in the response body. Our queries finish in ~3-16s, well under the
29s integration timeout, so no quota increase is needed. Residency is unchanged:
regional ap-south-1 endpoint (EndpointConfiguration=REGIONAL), no CloudFront.

Resources:
  POST /chat        -> AWS_PROXY -> Agent_Lambda  (the HTTP path in handler.py)
  OPTIONS /chat     -> MOCK CORS preflight (Access-Control-* for browser fetch)

Auth: none for the demo (open, like the simplified WS scheme). The production
authorizer gap is documented in DEPLOYMENT.md.

Idempotent. Run:  uv run python infra/provision_rest_api.py
"""
from __future__ import annotations

import json
import os
import sys

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import ACCOUNT, PROJECT, REGION, load_ids, save_ids  # noqa: E402

HERE = os.path.dirname(__file__)

API_NAME = f"{PROJECT}-rest"
STAGE = "prod"
RESOURCE_PATH = "chat"

apigw = boto3.client("apigateway", region_name=REGION)
lam = boto3.client("lambda", region_name=REGION)


def _find_api() -> str | None:
    for item in apigw.get_rest_apis(limit=500).get("items", []):
        if item["name"] == API_NAME:
            return item["id"]
    return None


def ensure_api() -> str:
    api_id = _find_api()
    if api_id:
        print(f"[skip] REST API exists: {api_id}")
        return api_id
    resp = apigw.create_rest_api(
        name=API_NAME,
        description="MNRE chatbot REST entry point (regional ap-south-1, no CloudFront)",
        endpointConfiguration={"types": ["REGIONAL"]},
        tags={"Project": "mnre-agentcore-chatbot"},
    )
    print(f"[create] REST API: {resp['id']}")
    return resp["id"]


def _root_id(api_id: str) -> str:
    for r in apigw.get_resources(restApiId=api_id, limit=500)["items"]:
        if r["path"] == "/":
            return r["id"]
    raise RuntimeError("root resource not found")


def ensure_resource(api_id: str, parent_id: str, part: str) -> str:
    for r in apigw.get_resources(restApiId=api_id, limit=500)["items"]:
        if r.get("pathPart") == part:
            return r["id"]
    resp = apigw.create_resource(restApiId=api_id, parentId=parent_id, pathPart=part)
    print(f"[create] resource /{part}")
    return resp["id"]


def _agent_arn(ids: dict) -> str:
    return ids["agent_lambda_arn"]


def ensure_post_proxy(api_id: str, resource_id: str, agent_arn: str) -> None:
    """POST /chat -> AWS_PROXY -> Agent_Lambda."""
    try:
        apigw.put_method(
            restApiId=api_id, resourceId=resource_id, httpMethod="POST",
            authorizationType="NONE", apiKeyRequired=False,
        )
    except ClientError as e:
        if "ConflictException" not in str(e):
            raise
    uri = (f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/"
           f"{agent_arn}/invocations")
    apigw.put_integration(
        restApiId=api_id, resourceId=resource_id, httpMethod="POST",
        type="AWS_PROXY", integrationHttpMethod="POST", uri=uri,
    )
    print("[ok] POST /chat -> AWS_PROXY -> Agent_Lambda")


def ensure_options_cors(api_id: str, resource_id: str) -> None:
    """OPTIONS /chat -> MOCK with CORS headers (browser preflight)."""
    try:
        apigw.put_method(
            restApiId=api_id, resourceId=resource_id, httpMethod="OPTIONS",
            authorizationType="NONE",
        )
    except ClientError as e:
        if "ConflictException" not in str(e):
            raise
    apigw.put_integration(
        restApiId=api_id, resourceId=resource_id, httpMethod="OPTIONS",
        type="MOCK", requestTemplates={"application/json": '{"statusCode": 200}'},
    )
    apigw.put_method_response(
        restApiId=api_id, resourceId=resource_id, httpMethod="OPTIONS",
        statusCode="200",
        responseParameters={
            "method.response.header.Access-Control-Allow-Headers": True,
            "method.response.header.Access-Control-Allow-Methods": True,
            "method.response.header.Access-Control-Allow-Origin": True,
        },
    )
    apigw.put_integration_response(
        restApiId=api_id, resourceId=resource_id, httpMethod="OPTIONS",
        statusCode="200",
        responseParameters={
            "method.response.header.Access-Control-Allow-Headers": "'content-type'",
            "method.response.header.Access-Control-Allow-Methods": "'POST,OPTIONS'",
            "method.response.header.Access-Control-Allow-Origin": "'*'",
        },
    )
    print("[ok] OPTIONS /chat -> MOCK CORS")


def ensure_invoke_permission(api_id: str, agent_arn: str) -> None:
    fn = agent_arn.split(":")[-1]
    source = f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{api_id}/*/POST/{RESOURCE_PATH}"
    try:
        lam.add_permission(
            FunctionName=fn, StatementId="mnre-rest-invoke",
            Action="lambda:InvokeFunction", Principal="apigateway.amazonaws.com",
            SourceArn=source,
        )
        print("[ok] lambda invoke permission for REST API")
    except lam.exceptions.ResourceConflictException:
        print("[skip] invoke permission already present")


def deploy(api_id: str) -> str:
    apigw.create_deployment(restApiId=api_id, stageName=STAGE)
    url = f"https://{api_id}.execute-api.{REGION}.amazonaws.com/{STAGE}/{RESOURCE_PATH}"
    print(f"[deploy] {STAGE} -> {url}")
    return url


def main() -> None:
    print(f"Region {REGION}  Account {ACCOUNT}\n")
    ids = load_ids()
    agent_arn = _agent_arn(ids)

    api_id = ensure_api()
    root = _root_id(api_id)
    chat = ensure_resource(api_id, root, RESOURCE_PATH)
    ensure_post_proxy(api_id, chat, agent_arn)
    ensure_options_cors(api_id, chat)
    ensure_invoke_permission(api_id, agent_arn)
    url = deploy(api_id)

    ids["rest_api_id"] = api_id
    ids["rest_api_url"] = url
    save_ids(ids)
    print(f"\nSaved rest_api_url = {url}")
    print("\nTest with:")
    print(f'  curl -sS -X POST "{url}" -H "content-type: application/json" \\')
    print('    -d \'{"question":"how many duplicate bank accounts exist?","sessionId":"demo-1"}\'')


if __name__ == "__main__":
    main()
