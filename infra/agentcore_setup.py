"""AgentCore Gateway provisioning for the MNRE chatbot (Task 12, ap-south-1).

WHAT THIS CREATES (idempotent, safe to re-run)
----------------------------------------------
  1. IAM gateway service role ``mnre-chatbot-gateway-role`` — trusted by
     ``bedrock-agentcore.amazonaws.com``, granted ``lambda:InvokeFunction`` on the
     Tool_Lambda so the Gateway can invoke the target on the caller's behalf.
  2. ONE AgentCore Gateway ``mnre-chatbot-gateway`` — MCP server protocol with
     IAM inbound authorization (``authorizerType='AWS_IAM'``, SigV4). NO
     Cognito/OAuth IdP (Req 6.3, 6.4). Callers must hold
     ``bedrock-agentcore:InvokeGateway`` (the Agent_Lambda role already does).
  3. ONE Lambda target on that gateway pointing at the existing Tool_Lambda
     (``arn:...:function:mnre-chatbot-tool``), exposing FOUR per-table MCP tools
     via the target's inline ``toolSchema`` (Req 6.1, 6.2, 6.5):
       - query_applications  -> table=applications
       - query_subsidy       -> table=subsidy
       - query_installation  -> table=installation
       - query_inspection    -> table=inspection
     Outbound auth to the Lambda uses ``credentialProviderType=GATEWAY_IAM_ROLE``.

HOW THE FIXED ``table`` IS PINNED (API-shape note)
--------------------------------------------------
AgentCore Gateway has NO server-side "inject a constant parameter" feature: when
it invokes a Lambda target it passes the model-supplied ``inputSchema`` property
VALUES as the event, plus the called tool name in the Lambda *context*
(``bedrockAgentCoreToolName`` = ``<targetName>___<toolName>``). We therefore pin
``table`` per tool two ways that BOTH agree:
  - the tool is NAMED ``query_<table>`` (clean tool selection by the agent), and
  - each tool's ``inputSchema`` declares a REQUIRED ``table`` property whose
    description fixes the only valid value for that tool (e.g. "MUST be 'subsidy'").
This keeps the already-deployed Tool_Lambda handler (Task 10) unchanged — it
reads ``event['table']`` and validates it against the Parameterized_Query_API
contract. The tool ALSO exposes filters/group_by/aggregations/limit, which the
agent fills from the question and the Gateway forwards verbatim (Req 6.5).

The Parameterized_Query_API contract each tool publishes (Req 6.2):
  - table        : fixed per tool (see above)
  - filters      : list of {column, op, value};
                   op in {eq,ne,gt,gte,lt,lte,like,in,is_null,not_null}
  - group_by     : list of column names
  - aggregations : list of {fn, column, alias}; fn in {count,sum,avg,min,max}
  - limit        : int, default 100, max 1000

Reads/writes infra/network_ids.json (load existing, ADD keys, never overwrite):
  gateway_role_arn, gateway_id, gateway_arn, gateway_url (MCP endpoint),
  gateway_target_id, gateway_tool_names.

Run:  uv run python infra/agentcore_setup.py   (from MNRE-AgentCore-Chatbot/)

TODO(security, later): the agent role ``mnre-chatbot-agent-lambda-role`` currently
holds ``bedrock-agentcore:InvokeGateway`` on Resource "*" (see provision_iam_ddb.py).
Once this gateway exists, tighten that statement's Resource to the gateway_arn
printed by this script.
"""
from __future__ import annotations

import json
import os
import sys
import time

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from config import ACCOUNT, PROJECT, REGION, TAGS_MAP, load_ids, save_ids  # noqa: E402
from common.schema import (  # noqa: E402
    ALLOWED_AGG_FNS,
    ALLOWED_FILTER_OPS,
    CURATED_COLUMNS,
    NUMERIC_COLUMNS,
    TABLES,
)

HERE = os.path.dirname(__file__)

GATEWAY_NAME = f"{PROJECT}-gateway"
GATEWAY_ROLE = f"{PROJECT}-gateway-role"
TARGET_NAME = "mnre-tool-target"
TOOL_LAMBDA_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{PROJECT}-tool"

# AgentCore Memory. NOTE: the memory name pattern is [a-zA-Z][a-zA-Z0-9_]{0,47}
# — hyphens are REJECTED, so use underscores (unlike the gateway, which allows
# hyphens).
MEMORY_NAME = "mnre_chatbot_memory"
# eventExpiryDuration is in DAYS, valid range 3..365. Short-term conversational
# memory for a demo — 30 days is plenty; no long-term strategies (Req 8, demo).
MEMORY_EVENT_EXPIRY_DAYS = 30

# Per-table tool definitions (Req 6.1).
TOOL_FOR_TABLE = {
    "applications": "query_applications",
    "subsidy": "query_subsidy",
    "installation": "query_installation",
    "inspection": "query_inspection",
}

TAGS = TAGS_MAP

iam = boto3.client("iam", region_name=REGION)
acc = boto3.client("bedrock-agentcore-control", region_name=REGION)


# --------------------------------------------------------------------------- #
# 1) Gateway service role (trusted by bedrock-agentcore; can invoke Tool_Lambda).
# --------------------------------------------------------------------------- #
GATEWAY_TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "GatewayAssumeRolePolicy",
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": ACCOUNT}},
        }
    ],
}


def ensure_gateway_role() -> str:
    """Create/lookup the gateway service role and grant it InvokeFunction on the tool."""
    try:
        iam.create_role(
            RoleName=GATEWAY_ROLE,
            AssumeRolePolicyDocument=json.dumps(GATEWAY_TRUST),
            Description="MNRE chatbot AgentCore Gateway service role (invokes Tool_Lambda)",
            Tags=[{"Key": k, "Value": v} for k, v in TAGS.items()],
        )
        print(f"[create] IAM role: {GATEWAY_ROLE}")
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"[skip] IAM role exists: {GATEWAY_ROLE}")
        # keep trust policy current (idempotent)
        iam.update_assume_role_policy(
            RoleName=GATEWAY_ROLE, PolicyDocument=json.dumps(GATEWAY_TRUST)
        )

    iam.put_role_policy(
        RoleName=GATEWAY_ROLE,
        PolicyName="mnre-gateway-invoke-tool-lambda",
        PolicyDocument=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "InvokeToolLambda",
                        "Effect": "Allow",
                        "Action": ["lambda:InvokeFunction"],
                        "Resource": [TOOL_LAMBDA_ARN],
                    }
                ],
            }
        ),
    )
    print("         inline policy: mnre-gateway-invoke-tool-lambda")
    arn = iam.get_role(RoleName=GATEWAY_ROLE)["Role"]["Arn"]
    print(f"         role arn: {arn}")
    return arn


# --------------------------------------------------------------------------- #
# 2) Tool schema — 4 per-table tool definitions (inline payload).
# --------------------------------------------------------------------------- #
def _tool_definition(table: str) -> dict:
    """Build one ToolDefinition for ``table`` publishing the Parameterized_Query_API.

    ``table`` is pinned: the property is REQUIRED and its description fixes the
    only valid value (matching the tool name ``query_<table>``). filters/group_by/
    aggregations/limit are forwarded to the Tool_Lambda verbatim (Req 6.2, 6.5).
    """
    ops = ", ".join(sorted(ALLOWED_FILTER_OPS))
    fns = ", ".join(sorted(ALLOWED_AGG_FNS))
    cols = ", ".join(CURATED_COLUMNS)
    numeric = ", ".join(sorted(NUMERIC_COLUMNS))
    return {
        "name": TOOL_FOR_TABLE[table],
        "description": (
            f"Run a safe, read-only parameterized query against the MNRE '{table}' "
            f"table (PM Surya Ghar rooftop-solar data). Returns aggregated/filtered "
            f"rows as structured JSON. Available columns: {cols}."
        ),
        "inputSchema": {
            "type": "object",
            "description": (
                f"Parameterized_Query_API request for the '{table}' table. No raw SQL."
            ),
            "properties": {
                "table": {
                    "type": "string",
                    "description": (
                        f"Target table. FIXED for this tool — MUST be '{table}'."
                    ),
                },
                "filters": {
                    "type": "array",
                    "description": (
                        "Optional WHERE conditions, ANDed together. Each item is an "
                        f"object {{column, op, value}}. op must be one of: {ops}. "
                        "op 'in' takes a list value; 'is_null'/'not_null' take no value."
                    ),
                    "items": {
                        "type": "object",
                        "description": "A single filter condition {column, op, value}.",
                        "properties": {
                            "column": {
                                "type": "string",
                                "description": f"Column to filter. One of: {cols}.",
                            },
                            "op": {
                                "type": "string",
                                "description": f"Comparison operator. One of: {ops}.",
                            },
                            "value": {
                                "type": "string",
                                "description": (
                                    "Comparison value (e.g. a state name or status). "
                                    "Omit for is_null/not_null."
                                ),
                            },
                        },
                        "required": ["column", "op"],
                    },
                },
                "group_by": {
                    "type": "array",
                    "description": "Optional list of column names to GROUP BY.",
                    "items": {
                        "type": "string",
                        "description": f"A column to group by. One of: {cols}.",
                    },
                },
                "aggregations": {
                    "type": "array",
                    "description": (
                        "Optional aggregations. Each item {fn, column, alias}. "
                        f"fn must be one of: {fns}. Non-count fns require a numeric "
                        f"column ({numeric}). For TAT/SLA 'average days between two "
                        "dates' metrics, use fn one of avg_days/min_days/max_days "
                        "with a start date 'column' and an end date 'column2' (both "
                        "date columns) — it reports the day gap (column2 - column)."
                    ),
                    "items": {
                        "type": "object",
                        "description": "A single aggregation {fn, column, alias}.",
                        "properties": {
                            "fn": {
                                "type": "string",
                                "description": (
                                    f"Aggregation function. One of: {fns}, "
                                    "or avg_days/min_days/max_days for date gaps."
                                ),
                            },
                            "column": {
                                "type": "string",
                                "description": f"Column to aggregate. One of: {cols}.",
                            },
                            "column2": {
                                "type": "string",
                                "description": (
                                    "End date column for avg_days/min_days/max_days "
                                    f"(day gap = column2 - column). One of: {cols}."
                                ),
                            },
                            "alias": {
                                "type": "string",
                                "description": "Output name for the aggregated value.",
                            },
                        },
                        "required": ["fn", "column"],
                    },
                },
                "limit": {
                    "type": "integer",
                    "description": "Max rows to return. Default 100, hard max 1000.",
                },
                "having": {
                    "type": "array",
                    "description": (
                        "Optional post-aggregation filter on an aggregated value "
                        "(SQL HAVING). Requires group_by. Each item {fn, column, op, "
                        "value} compares FN(column) to a number. Use this for "
                        "'appears more than once / N+ times' questions, e.g. to find "
                        "beneficiaries who took the subsidy twice: group_by "
                        "benefiaicry_unique_id_by_pfms with having "
                        "{fn:'count', column:'application_id', op:'gte', value:2}."
                    ),
                    "items": {
                        "type": "object",
                        "description": "A HAVING condition {fn, column, op, value}.",
                        "properties": {
                            "fn": {
                                "type": "string",
                                "description": f"Aggregation function. One of: {fns}.",
                            },
                            "column": {
                                "type": "string",
                                "description": f"Column to aggregate. One of: {cols}.",
                            },
                            "op": {
                                "type": "string",
                                "description": "Comparison: eq, ne, gt, gte, lt, lte.",
                            },
                            "value": {
                                "type": "number",
                                "description": "Numeric threshold (e.g. 2).",
                            },
                        },
                        "required": ["fn", "column", "op", "value"],
                    },
                },
                "order_by": {
                    "type": "array",
                    "description": (
                        "Optional sort, for 'top N' / 'highest' / 'most' / 'largest' "
                        "questions. Each item {by, direction}: 'by' is a column name "
                        "or an aggregation alias you defined; direction is 'asc' or "
                        "'desc' (default desc). Combine with a small limit to return "
                        "a leaderboard, e.g. top 5 states: group_by [state], "
                        "aggregations [{fn:count, column:application_id, alias:n}], "
                        "order_by [{by:n, direction:desc}], limit 5."
                    ),
                    "items": {
                        "type": "object",
                        "description": "A sort key {by, direction}.",
                        "properties": {
                            "by": {
                                "type": "string",
                                "description": "Column name or aggregation alias to sort by.",
                            },
                            "direction": {
                                "type": "string",
                                "description": "'asc' or 'desc' (default 'desc').",
                            },
                        },
                        "required": ["by"],
                    },
                },
            },
            "required": ["table"],
        },
    }


def _tool_schema_payload() -> list[dict]:
    """The 4-tool inline payload, ordered as the curated TABLES tuple."""
    return [_tool_definition(t) for t in TABLES]


# --------------------------------------------------------------------------- #
# 3) Gateway (MCP + AWS_IAM inbound auth) — idempotent.
# --------------------------------------------------------------------------- #
def _find_gateway() -> dict | None:
    paginator_token = None
    while True:
        kwargs = {"maxResults": 50}
        if paginator_token:
            kwargs["nextToken"] = paginator_token
        resp = acc.list_gateways(**kwargs)
        for g in resp.get("items", []):
            if g.get("name") == GATEWAY_NAME:
                return g
        paginator_token = resp.get("nextToken")
        if not paginator_token:
            return None


def _wait_gateway_ready(gateway_id: str) -> dict:
    """Poll get_gateway until it leaves a transient state."""
    for _ in range(60):
        g = acc.get_gateway(gatewayIdentifier=gateway_id)
        status = g.get("status")
        if status in ("READY", "ACTIVE", "AVAILABLE"):
            return g
        if status in ("FAILED", "CREATE_FAILED", "UPDATE_FAILED"):
            raise SystemExit(f"gateway {gateway_id} in status {status}: {g.get('statusReasons')}")
        print(f"  gateway status={status}; waiting...")
        time.sleep(5)
    raise SystemExit(f"gateway {gateway_id} not ready after timeout")


def ensure_gateway(role_arn: str) -> dict:
    existing = _find_gateway()
    if existing:
        gid = existing["gatewayId"]
        print(f"[skip] gateway exists: {GATEWAY_NAME} ({gid})")
        return _wait_gateway_ready(gid)

    print(f"[create] gateway: {GATEWAY_NAME} (MCP, AWS_IAM inbound auth)")
    resp = acc.create_gateway(
        name=GATEWAY_NAME,
        description="MNRE PM Surya Ghar chatbot — 4 per-table read-only query tools (MCP).",
        roleArn=role_arn,
        protocolType="MCP",
        protocolConfiguration={
            "mcp": {
                "instructions": (
                    "Four read-only tools query the MNRE PM Surya Ghar tables "
                    "(applications, subsidy, installation, inspection) via a "
                    "parameterized query API. No raw SQL."
                ),
                "searchType": "SEMANTIC",
            }
        },
        authorizerType="AWS_IAM",
        tags=TAGS,
    )
    print(f"         gatewayId={resp['gatewayId']} status={resp.get('status')}")
    return _wait_gateway_ready(resp["gatewayId"])


# --------------------------------------------------------------------------- #
# 4) Lambda target with the 4-tool inline schema — idempotent.
# --------------------------------------------------------------------------- #
def _find_target(gateway_id: str) -> dict | None:
    token = None
    while True:
        kwargs = {"gatewayIdentifier": gateway_id, "maxResults": 50}
        if token:
            kwargs["nextToken"] = token
        resp = acc.list_gateway_targets(**kwargs)
        for t in resp.get("items", []):
            if t.get("name") == TARGET_NAME:
                return t
        token = resp.get("nextToken")
        if not token:
            return None


def ensure_target(gateway_id: str) -> str:
    existing = _find_target(gateway_id)
    if existing:
        print(f"[skip] target exists: {TARGET_NAME} ({existing['targetId']})")
        return existing["targetId"]

    print(f"[create] Lambda target: {TARGET_NAME} -> {TOOL_LAMBDA_ARN}")
    resp = acc.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=TARGET_NAME,
        description="Single read-only Tool_Lambda exposing 4 per-table query tools.",
        targetConfiguration={
            "mcp": {
                "lambda": {
                    "lambdaArn": TOOL_LAMBDA_ARN,
                    "toolSchema": {"inlinePayload": _tool_schema_payload()},
                }
            }
        },
        credentialProviderConfigurations=[
            {"credentialProviderType": "GATEWAY_IAM_ROLE"}
        ],
    )
    target_id = resp["targetId"]
    print(f"         targetId={target_id} status={resp.get('status')}")
    _wait_target_ready(gateway_id, target_id)
    return target_id


def _wait_target_ready(gateway_id: str, target_id: str) -> None:
    for _ in range(60):
        t = acc.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        status = t.get("status")
        if status in ("READY", "ACTIVE", "AVAILABLE"):
            return
        if status in ("FAILED", "CREATE_FAILED", "UPDATE_FAILED"):
            raise SystemExit(f"target {target_id} in status {status}: {t.get('statusReasons')}")
        print(f"  target status={status}; waiting...")
        time.sleep(5)
    raise SystemExit(f"target {target_id} not ready after timeout")


# --------------------------------------------------------------------------- #
# Verification (Task 12 acceptance).
# --------------------------------------------------------------------------- #
def verify(gateway_id: str) -> None:
    print("\n=== verification ===")
    g = acc.get_gateway(gatewayIdentifier=gateway_id)
    print(f"gateway     : {g['name']} ({g['gatewayId']})")
    print(f"protocol    : {g.get('protocolType')}")
    print(f"inbound auth: {g.get('authorizerType')}  (AWS_IAM => SigV4 required; "
          f"unsigned/unauthorized callers are denied)")
    if g.get("authorizerType") != "AWS_IAM":
        raise SystemExit(f"expected AWS_IAM inbound auth, got {g.get('authorizerType')}")
    print(f"MCP endpoint: {g.get('gatewayUrl')}")

    targets = acc.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", [])
    print(f"targets     : {len(targets)}")
    for t in targets:
        tgt = acc.get_gateway_target(gatewayIdentifier=gateway_id, targetId=t["targetId"])
        cfg = tgt["targetConfiguration"]["mcp"]["lambda"]
        tools = cfg["toolSchema"].get("inlinePayload", [])
        names = [tool["name"] for tool in tools]
        print(f"  - {tgt['name']} ({tgt['targetId']}) lambda={cfg['lambdaArn']}")
        print(f"    tools ({len(names)}): {names}")
        expected = set(TOOL_FOR_TABLE.values())
        if set(names) != expected:
            raise SystemExit(f"tool set mismatch: got {set(names)}, expected {expected}")


# --------------------------------------------------------------------------- #
# 5) AgentCore Memory (Task 13.1) — short-term conversational memory, idempotent.
#
# Control plane: bedrock-agentcore-control (create_memory/get_memory/list_memories).
# Data plane (used by src/agent/memory.py at runtime): bedrock-agentcore
# (create_event/list_events). For a demo we provision short-term memory only:
# no memoryStrategies (long-term extraction) are configured (Req 8.1, 8.5).
# --------------------------------------------------------------------------- #
def _find_memory() -> dict | None:
    """Return the memory summary for MEMORY_NAME, or None. list_memories
    summaries carry id/arn/status but NOT name, so we get_memory each to match."""
    token = None
    while True:
        kwargs = {"maxResults": 50}
        if token:
            kwargs["nextToken"] = token
        resp = acc.list_memories(**kwargs)
        for m in resp.get("memories", []):
            detail = acc.get_memory(memoryId=m["id"])["memory"]
            if detail.get("name") == MEMORY_NAME:
                return detail
        token = resp.get("nextToken")
        if not token:
            return None


def _wait_memory_active(memory_id: str) -> dict:
    """Poll get_memory until ACTIVE (or fail fast on FAILED)."""
    for _ in range(60):
        m = acc.get_memory(memoryId=memory_id)["memory"]
        status = m.get("status")
        if status == "ACTIVE":
            return m
        if status == "FAILED":
            raise SystemExit(f"memory {memory_id} FAILED: {m.get('failureReason')}")
        print(f"  memory status={status}; waiting...")
        time.sleep(5)
    raise SystemExit(f"memory {memory_id} not ACTIVE after timeout")


def ensure_memory() -> dict:
    """Create (or find) the short-term conversational Memory resource."""
    existing = _find_memory()
    if existing:
        mid = existing["id"]
        print(f"[skip] memory exists: {MEMORY_NAME} ({mid})")
        return _wait_memory_active(mid)

    print(f"[create] memory: {MEMORY_NAME} (short-term conversational, "
          f"{MEMORY_EVENT_EXPIRY_DAYS}d expiry)")
    resp = acc.create_memory(
        name=MEMORY_NAME,
        description="MNRE PM Surya Ghar chatbot — short-term session memory "
                    "(conversational turns keyed by actorId + sessionId).",
        eventExpiryDuration=MEMORY_EVENT_EXPIRY_DAYS,
        # No memoryStrategies => short-term only (no long-term extraction). Demo-sufficient.
        tags=TAGS,
    )
    m = resp["memory"]
    print(f"         memoryId={m['id']} status={m.get('status')}")
    return _wait_memory_active(m["id"])


def verify_memory(memory_id: str) -> dict:
    print("\n=== memory verification ===")
    m = acc.get_memory(memoryId=memory_id)["memory"]
    print(f"memory   : {m['name']} ({m['id']})")
    print(f"arn      : {m['arn']}")
    print(f"status   : {m.get('status')}")
    print(f"expiry   : {m.get('eventExpiryDuration')} days")
    print(f"strategies: {len(m.get('strategies', []))} (short-term only)")
    if m.get("status") != "ACTIVE":
        raise SystemExit(f"expected memory status ACTIVE, got {m.get('status')}")
    return m


def main() -> None:
    ids = load_ids()
    print(f"Region {REGION}  Account {ACCOUNT}\n")

    role_arn = ensure_gateway_role()
    # New IAM role can take a moment to be assumable by the service.
    print("  (waiting 10s for IAM role propagation)")
    time.sleep(10)

    print()
    gw = ensure_gateway(role_arn)
    gateway_id = gw["gatewayId"]
    gateway_arn = gw["gatewayArn"]
    gateway_url = gw.get("gatewayUrl")

    print()
    target_id = _create_target_with_retry(gateway_id)

    verify(gateway_id)

    print()
    mem = ensure_memory()
    memory_id = mem["id"]
    memory_arn = mem["arn"]
    verify_memory(memory_id)

    # Save WITHOUT overwriting existing keys' unrelated values.
    ids["gateway_role_arn"] = role_arn
    ids["gateway_id"] = gateway_id
    ids["gateway_arn"] = gateway_arn
    ids["gateway_url"] = gateway_url
    ids["gateway_target_id"] = target_id
    ids["gateway_tool_names"] = [TOOL_FOR_TABLE[t] for t in TABLES]
    ids["memory_id"] = memory_id
    ids["memory_arn"] = memory_arn
    save_ids(ids)

    print(f"\nSaved to network_ids.json:")
    print(f"  gateway_id        = {gateway_id}")
    print(f"  gateway_arn       = {gateway_arn}")
    print(f"  gateway_url (MCP) = {gateway_url}")
    print(f"  gateway_target_id = {target_id}")
    print(f"  gateway_tool_names= {[TOOL_FOR_TABLE[t] for t in TABLES]}")
    print(f"  memory_id         = {memory_id}")
    print(f"  memory_arn        = {memory_arn}")
    print(
        "\nTODO(security): tighten mnre-chatbot-agent-lambda-role's "
        "bedrock-agentcore:InvokeGateway Resource from '*' to:\n  " + gateway_arn
    )


def _create_target_with_retry(gateway_id: str) -> str:
    """create_gateway_target can race IAM role propagation; retry briefly."""
    for attempt in range(6):
        try:
            return ensure_target(gateway_id)
        except ClientError as e:
            msg = str(e)
            if ("cannot be assumed" in msg or "not authorized" in msg
                    or "AccessDenied" in msg) and attempt < 5:
                print(f"  role/permission not ready, retrying ({attempt + 1})...")
                time.sleep(8)
                continue
            raise


if __name__ == "__main__":
    main()
