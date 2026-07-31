"""Agent_Lambda handler — Strands agent over the AgentCore Gateway (Task 14.3).

ARCHITECTURE (design Req 7)
---------------------------
Input from the WebSocket integration: ``{connectionId, question}``. Per request:
  1. ``residency_guard(MODEL_ID)`` at startup — fail fast on any cross-region
     inference-profile id (us./eu./ap./apac./jp./au./global.) (Req 1.6, 1.7).
  2. Load prior turns from AgentCore Memory keyed by sessionId=connectionId
     (actorId=ACTOR_ID) so follow-ups have context (Req 7.7, 8.2-8.4, 8.6).
  3. Run a Strands ``Agent`` with a region-pinned ``BedrockModel`` (the
     configured in-region ON_DEMAND modelId, ap-south-1),
     a system prompt describing the 4 program tables, the prior history as context,
     and an MCP client connected to the Gateway MCP URL using SigV4 IAM auth
     (Req 7.1-7.5).
  4. Save the (question, answer) turn to Memory (Req 7.7).
  5. POST the answer back to the originating connection via API Gateway
     Management API @connections (Req 9.5). On any tool/model error, POST a
     graceful "couldn't answer" message and never raise (Req 7.6, 9.6).

Region-pinned everywhere (region_name="ap-south-1"); bare ON_DEMAND modelId only.
"""
from __future__ import annotations

import json
import os
import re

import boto3

from agent.errors import wrap_tool_error
from agent.memory import (
    DEFAULT_ACTOR_ID,
    load as memory_load,
    safe_session_id,
    save as memory_save,
)
from agent.prompt import build_system_prompt
from agent.residency import residency_guard

REGION = os.environ.get("REGION", "ap-south-1")
# Model-agnostic: MODEL_ID is injected by the deploy (infra/deploy_agent.py from
# deploy.config.json / CHATBOT_MODEL_ID). No hardcoded default — pick any bare
# in-region ON_DEMAND modelId with Converse tool-use support from the Bedrock
# regional model availability page.
MODEL_ID = os.environ.get("MODEL_ID", "")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "")
MEMORY_ID = os.environ.get("MEMORY_ID", "")
ACTOR_ID = os.environ.get("ACTOR_ID", DEFAULT_ACTOR_ID)
# WebSocket @connections callback endpoint, e.g.
# https://<api-id>.execute-api.ap-south-1.amazonaws.com/<stage>
# Empty until Task 15 wires the WS API; we degrade gracefully + log when unset.
WS_CALLBACK_URL = os.environ.get("WS_CALLBACK_URL", "")

# This function's own name, used to re-invoke itself asynchronously (see the
# async-dispatch note in handler()). Lambda sets AWS_LAMBDA_FUNCTION_NAME.
SELF_FUNCTION_NAME = os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "")

# Fail fast at import (cold start) on a residency violation: the configured id
# must be an in-region bare modelId, never a cross-region profile (Req 1.6, 1.7).
# A missing MODEL_ID is reported at agent construction (_build_agent).
MODEL_ID = residency_guard(MODEL_ID) if MODEL_ID else ""

_MAX_HISTORY_TURNS = 10  # cap prior turns injected as context


def _history_text(turns: list[dict]) -> str:
    """Render prior turns as a compact text block for the system context."""
    recent = turns[-_MAX_HISTORY_TURNS:]
    if not recent:
        return ""
    lines = ["Prior conversation in this session (oldest first):"]
    for t in recent:
        user = (t.get("user") or "").strip()
        assistant = (t.get("assistant") or "").strip()
        if user:
            lines.append(f"User: {user}")
        if assistant:
            lines.append(f"Assistant: {assistant}")
    return "\n".join(lines)


def _build_agent(history_block: str):
    """Construct the Strands Agent + Gateway MCP client (returns (agent, mcp_client)).

    The MCP client connects to the Gateway MCP URL using a SigV4 ``httpx.Auth``
    for service ``bedrock-agentcore`` in-region. Imports are local so the pure
    helpers (and tests) don't require the strands/mcp stack to be importable.
    """
    from strands.models import BedrockModel
    from strands.tools.mcp import MCPClient
    from mcp.client.streamable_http import streamablehttp_client

    from agent.sigv4 import SigV4HTTPXAuth

    if not MODEL_ID:
        raise RuntimeError(
            "MODEL_ID is not configured. Set model_id in deploy.config.json (or "
            "the MODEL_ID env var) to a bare in-region ON_DEMAND modelId with "
            "Converse tool-use support — see the Bedrock regional model "
            "availability page: https://docs.aws.amazon.com/bedrock/latest/"
            "userguide/models-region-compatibility.html"
        )
    if not GATEWAY_URL:
        raise RuntimeError("GATEWAY_URL is not configured")

    auth = SigV4HTTPXAuth(region=REGION)
    mcp_client = MCPClient(
        lambda: streamablehttp_client(url=GATEWAY_URL, auth=auth)
    )

    # Some in-region models do not support tool use in streaming mode, so
    # disable streaming (use the Converse, not ConverseStream, API) for
    # portability across configured models.
    model = BedrockModel(model_id=MODEL_ID, region_name=REGION, streaming=False)
    system_prompt = build_system_prompt()
    if history_block:
        system_prompt = f"{system_prompt}\n\n{history_block}"

    return model, system_prompt, mcp_client


def _run_agent(question: str, history_block: str) -> str:
    """Run the agent loop and return the final natural-language answer text."""
    from strands import Agent

    model, system_prompt, mcp_client = _build_agent(history_block)

    # The MCP connection lifecycle is managed by the context manager; tools are
    # discovered from the Gateway and handed to the agent. We keep ONLY the
    # per-table query_* tools and drop the Gateway's built-in search tool, which
    # otherwise muddies the model's tool selection (verified during research).
    with mcp_client:
        tools = mcp_client.list_tools_sync()
        query_tools = [t for t in tools if "query_" in t.tool_name] or tools
        agent = Agent(model=model, system_prompt=system_prompt, tools=query_tools)
        result = agent(question)

    return str(result).strip() or "I couldn't produce an answer for that question."


def _post_to_connection(connection_id: str, message: str) -> bool:
    """POST ``message`` to the WS connection via @connections. Returns success.

    Degrades gracefully (logs + returns False) when WS_CALLBACK_URL is unset
    (pre-Task-15) or the post fails, so the handler never raises (Req 9.6).
    """
    if not WS_CALLBACK_URL:
        print(f"[ws] WS_CALLBACK_URL unset; answer for {connection_id} (logged only): {message}")
        return False
    try:
        api = boto3.client(
            "apigatewaymanagementapi",
            region_name=REGION,
            endpoint_url=WS_CALLBACK_URL,
        )
        api.post_to_connection(
            ConnectionId=connection_id, Data=message.encode("utf-8")
        )
        print(f"[ws] posted answer to {connection_id} ({len(message)} chars)")
        return True
    except Exception as exc:  # noqa: BLE001 - never raise out of the handler
        print(f"[ws] failed to post to {connection_id}: {type(exc).__name__}: {exc}")
        return False


def _memory_client():
    """Region-pinned bedrock-agentcore data-plane client for Memory load/save."""
    return boto3.client("bedrock-agentcore", region_name=REGION)


def _normalize_event(event: dict) -> tuple[str, str]:
    """Return (connectionId, question) from either a direct invoke or a WS proxy event.

    Two callers deliver different shapes:
      - Direct invoke (deploy_agent.py smoke, integration tests):
        ``{connectionId, question}`` at the top level.
      - API Gateway WebSocket AWS_PROXY (Task 15 sendMessage/$default route):
        ``connectionId`` under ``requestContext`` and a JSON string ``body`` of
        ``{action, question}``.
    Top-level keys win (backward compatible); the WS shape is the fallback.
    """
    event = event or {}
    connection_id = event.get("connectionId")
    question = event.get("question")

    if not connection_id:
        connection_id = (event.get("requestContext") or {}).get("connectionId")
    if not question:
        body = event.get("body")
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except (ValueError, TypeError):
                body = {}
        if isinstance(body, dict):
            question = body.get("question")

    return connection_id or "unknown", question or ""


def _dispatch_async(event: dict) -> bool:
    """Re-invoke this function asynchronously to run the agent loop off the
    WebSocket route's 29s budget. Returns True if the async invoke was issued.

    WHY: the sendMessage route invokes the agent synchronously, but API Gateway's
    WebSocket integration times out at 29s. Slow aggregation queries exceed that,
    so API Gateway returns "Internal server error" to the browser while the Lambda
    keeps running and posts a late (misaligned) frame. Instead, the route handler
    returns 200 immediately and the answer is produced by an async worker that
    posts it via @connections. Falls back to synchronous execution if the
    self-invoke can't be issued.
    """
    if not SELF_FUNCTION_NAME:
        return False
    connection_id, question = _normalize_event(event)
    worker_payload = {"connectionId": connection_id, "question": question, "_async": True}
    try:
        boto3.client("lambda", region_name=REGION).invoke(
            FunctionName=SELF_FUNCTION_NAME,
            InvocationType="Event",
            Payload=json.dumps(worker_payload).encode("utf-8"),
        )
        print(f"[dispatch] async worker invoked for {connection_id}")
        return True
    except Exception as exc:  # noqa: BLE001 - fall back to sync on any failure
        print(f"[dispatch] async invoke failed ({type(exc).__name__}: {exc}); running sync")
        return False


def _clean_answer(text: str) -> str:
    """Strip mechanical / query-jargon phrasing from the model's answer.

    The model is told to answer for executives, but models occasionally still
    leak phrases like "The query on the 'inspection' table shows ...". This is a
    deterministic safety net so the user NEVER sees query mechanics regardless of
    what the model emits. It only rewrites known lead-in patterns; the factual
    content (the numbers) is preserved.
    """
    if not text:
        return text
    s = text.strip()

    # Remove leading mechanical clauses up to a connecting verb, e.g.
    #   "The query on the 'inspection' table shows there are 0 ..." -> "There are 0 ..."
    #   "The query result indicates that 391 ..." -> "391 ..."
    lead = re.compile(
        r"^(the\s+)?(query|tool|result|data|output|response)[^.]*?"
        r"\b(shows?|indicates?|returns?|reveals?|reports?|found|gives?|tells us)\b"
        r"\s*(that\s+)?",
        re.IGNORECASE,
    )
    s = lead.sub("", s, count=1).strip()

    # Drop any remaining explicit references to query mechanics mid-sentence.
    s = re.sub(r"\bthe\s+'?\w+'?\s+table\b", "the data", s, flags=re.IGNORECASE)
    s = re.sub(r"\bby (aggregating|filtering|grouping)[^.,]*", "", s, flags=re.IGNORECASE)

    s = s.strip()
    # Re-capitalise the first letter if we trimmed a lead-in.
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    return s or text.strip()


def _run_turn(session_key: str, question: str) -> tuple[str, bool]:
    """Run one full turn: load memory, run the agent, save memory.

    ``session_key`` is the conversation/session id (a client UUID for the HTTP
    Function URL path, or a sanitized connectionId for the legacy WS path).
    Returns ``(answer, ok)`` and never raises.
    """
    session_id = safe_session_id(session_key)

    history_block = ""
    if MEMORY_ID:
        try:
            turns = memory_load(_memory_client(), MEMORY_ID, ACTOR_ID, session_id)
            history_block = _history_text(turns)
            print(f"[memory] loaded {len(turns)} prior turn(s)")
        except Exception as exc:  # noqa: BLE001 - memory is non-fatal
            print(f"[memory] load failed: {type(exc).__name__}: {exc}")

    ok = True
    try:
        answer = _run_agent(question, history_block)
        answer = _clean_answer(answer)
    except Exception as exc:  # noqa: BLE001 - never raise to caller
        print(f"[agent] error: {type(exc).__name__}: {exc}")
        answer = wrap_tool_error(exc)
        ok = False

    if MEMORY_ID:
        try:
            memory_save(_memory_client(), MEMORY_ID, ACTOR_ID, session_id,
                        question, answer)
        except Exception as exc:  # noqa: BLE001 - save is non-fatal
            print(f"[memory] save failed: {type(exc).__name__}: {exc}")

    return answer, ok


_CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "content-type",
}


def _http_response(status: int, body: dict) -> dict:
    """Build a Lambda Function URL (HTTP proxy) response with CORS headers."""
    return {"statusCode": status, "headers": _CORS_HEADERS, "body": json.dumps(body)}


def _is_function_url_event(event: dict) -> bool:
    """True if this is an HTTP-proxy invocation: Lambda Function URL / API Gateway
    HTTP API (payload v2, has requestContext.http) OR REST API v1 proxy (has a
    top-level httpMethod). Distinguishes from the WebSocket route shape, which
    has requestContext.connectionId and no httpMethod."""
    rc = event.get("requestContext") or {}
    if "http" in rc or event.get("version") == "2.0":
        return True
    # REST API v1 proxy: top-level httpMethod, and NOT a WS connection event.
    return bool(event.get("httpMethod")) and "connectionId" not in rc


def _handle_http(event: dict) -> dict:
    """Synchronous HTTP path (Lambda Function URL): run the turn, return the answer.

    The browser POSTs ``{"question": "...", "sessionId": "<uuid>"}``; the agent
    runs synchronously (Function URLs allow long timeouts — no 29s ceiling) and
    the answer is returned directly in the HTTP response body. CORS preflight
    (OPTIONS) returns 200 immediately.
    """
    rc = event.get("requestContext") or {}
    method = (rc.get("http") or {}).get("method") or event.get("httpMethod") or "POST"
    if method == "OPTIONS":
        return _http_response(200, {"ok": True})

    body = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8")
    try:
        data = json.loads(body) if isinstance(body, str) else (body or {})
    except (ValueError, TypeError):
        data = {}

    question = (data.get("question") or "").strip()
    session_key = (data.get("sessionId") or "anonymous").strip() or "anonymous"
    print(f"[agent-http] session={session_key} model={MODEL_ID} region={REGION}")

    if not question:
        return _http_response(200, {
            "answer": "Please ask a question about the rooftop-solar program data.",
            "ok": False,
        })

    answer, ok = _run_turn(session_key, question)
    return _http_response(200, {"answer": answer, "ok": ok})


def handler(event, context):  # noqa: ARG001 - Lambda signature
    event = event or {}

    # Primary path: Lambda Function URL (synchronous HTTP request/response).
    if _is_function_url_event(event):
        return _handle_http(event)

    # ---- Legacy WebSocket path (kept for backward compatibility) -------------
    # If this is the WebSocket route invocation (has connectionId) and NOT the
    # async worker, hand off to an async worker and return 200 immediately so the
    # 29s WebSocket integration never times out. The answer is delivered later by
    # the worker via @connections.
    is_ws_route = bool((event.get("requestContext") or {}).get("connectionId"))
    if is_ws_route and not event.get("_async") and _dispatch_async(event):
        return {"statusCode": 200, "dispatched": True}

    connection_id, question = _normalize_event(event)
    print(f"[agent] connection={connection_id} model={MODEL_ID} region={REGION}")

    if not question or not str(question).strip():
        answer = "Please ask a question about the rooftop-solar program data."
        _post_to_connection(connection_id, answer)
        return {"statusCode": 200, "connectionId": connection_id, "answer": answer, "ok": False}

    answer, ok = _run_turn(connection_id, str(question).strip())
    posted = _post_to_connection(connection_id, answer)
    return {
        "statusCode": 200,
        "connectionId": connection_id,
        "answer": answer,
        "ok": ok,
        "posted": posted,
    }
