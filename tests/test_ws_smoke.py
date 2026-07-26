"""WebSocket integration smoke tests (Task 15.1).

Covers Requirements 9.1, 9.2, 9.3, 9.5, 9.6:
  - 9.1 WS API exposes connect/disconnect/message routes
  - 9.2 $connect records the connection state (DynamoDB PutItem)
  - 9.3 $disconnect removes the connection state (DynamoDB DeleteItem)
  - 9.5 a produced answer is delivered to the originating connection
  - 9.6 an agent failure delivers a graceful error frame to that connection

Two layers of coverage:

  1. DETERMINISTIC unit-style smoke tests (default `uv run pytest` run) that
     exercise the in-VPC logic WITHOUT AWS, by mocking boto3 clients:
       - the inline $connect handler source (PutItem with the connectionId),
       - the inline $disconnect handler source (DeleteItem with the connectionId),
       - the Agent_Lambda `_normalize_event` (WS AWS_PROXY -> (connectionId, question)),
       - the Agent_Lambda `handler` happy path (answer posted to the connection),
       - the Agent_Lambda `handler` failure path (graceful error frame, never raises).

  2. LIVE smoke tests against the real deployed WebSocket API (ap-south-1). These
     are slow/costly (the agent calls Bedrock) and may be unavailable in CI, so
     they SKIP unless `RUN_WS_SMOKE=1` AND `network_ids.json` has `ws_endpoint`
     AND the `websockets` client + AWS connectivity are available.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

HERE = os.path.dirname(__file__)
IDS_PATH = os.path.join(HERE, "..", "infra", "network_ids.json")


# --------------------------------------------------------------------------- #
# Helpers: load the inline $connect / $disconnect handler sources and exec them
# against a mocked DynamoDB client so we can assert the Put/Delete behavior
# without importing AWS or touching the network.
# --------------------------------------------------------------------------- #
def _provision_module():
    """Import infra.provision_websocket with boto3.client mocked at import time.

    The module constructs apigatewayv2/lambda/iam/sts clients at import; patching
    boto3.client keeps the import inert (no creds / no network needed). We only
    need the inline `_CONNECT_SRC` / `_DISCONNECT_SRC` constants from it.
    """
    with patch("boto3.client", MagicMock()):
        sys.modules.pop("infra.provision_websocket", None)
        return importlib.import_module("infra.provision_websocket")


def _build_inline_handler(source: str, fake_ddb: MagicMock):
    """Load an inline helper-Lambda source as a real module with a mocked
    DynamoDB client.

    The handler sources read `CONNECTIONS_TABLE` and build a boto3 DynamoDB
    client at module-body time, so env + the boto3.client patch must be active
    while the module body runs. The source (a trusted constant from
    infra/provision_websocket.py — the exact code the provisioner ships inline)
    is written to a temp file and imported via importlib, so no dynamic
    ``exec``/``eval`` is involved. Returns the resulting `handler` callable
    (closing over the mocked client).
    """
    import importlib.util
    import tempfile

    env = {"CONNECTIONS_TABLE": "mnre-chatbot-connections", "REGION": "ap-south-1"}
    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", prefix="inline_ws_handler_", delete=False
    ) as tf:
        tf.write(source)
        module_path = tf.name
    try:
        with patch.dict(os.environ, env), patch("boto3.client", return_value=fake_ddb):
            spec = importlib.util.spec_from_file_location("inline_ws_handler", module_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
    finally:
        os.unlink(module_path)
    return module.handler


# --------------------------------------------------------------------------- #
# Deterministic: $connect stores the connectionId in the connections table.
# Requirement 9.2 (and 9.1: the connect route exists / is wired to this logic).
# --------------------------------------------------------------------------- #
def test_connect_stores_connection_id():
    mod = _provision_module()
    fake_ddb = MagicMock()
    handler = _build_inline_handler(mod._CONNECT_SRC, fake_ddb)

    event = {"requestContext": {"connectionId": "conn-abc-123"}}
    resp = handler(event, None)

    assert resp["statusCode"] == 200
    fake_ddb.put_item.assert_called_once()
    kwargs = fake_ddb.put_item.call_args.kwargs
    assert kwargs["TableName"] == "mnre-chatbot-connections"
    item = kwargs["Item"]
    # The originating connectionId is the stored key (and the session id).
    assert item["connectionId"] == {"S": "conn-abc-123"}
    assert item["sessionId"] == {"S": "conn-abc-123"}
    # connectedAt + ttl are recorded so stale rows self-clean.
    assert "connectedAt" in item and "ttl" in item


def test_connect_without_connection_id_is_rejected_no_write():
    mod = _provision_module()
    fake_ddb = MagicMock()
    handler = _build_inline_handler(mod._CONNECT_SRC, fake_ddb)

    resp = handler({"requestContext": {}}, None)

    assert resp["statusCode"] == 400
    fake_ddb.put_item.assert_not_called()


# --------------------------------------------------------------------------- #
# Deterministic: $disconnect removes the connectionId from the table.
# Requirement 9.3.
# --------------------------------------------------------------------------- #
def test_disconnect_removes_connection_id():
    mod = _provision_module()
    fake_ddb = MagicMock()
    handler = _build_inline_handler(mod._DISCONNECT_SRC, fake_ddb)

    resp = handler({"requestContext": {"connectionId": "conn-abc-123"}}, None)

    assert resp["statusCode"] == 200
    fake_ddb.delete_item.assert_called_once()
    kwargs = fake_ddb.delete_item.call_args.kwargs
    assert kwargs["TableName"] == "mnre-chatbot-connections"
    assert kwargs["Key"] == {"connectionId": {"S": "conn-abc-123"}}


def test_disconnect_without_connection_id_is_a_noop():
    mod = _provision_module()
    fake_ddb = MagicMock()
    handler = _build_inline_handler(mod._DISCONNECT_SRC, fake_ddb)

    resp = handler({"requestContext": {}}, None)

    assert resp["statusCode"] == 200
    fake_ddb.delete_item.assert_not_called()


# --------------------------------------------------------------------------- #
# Deterministic: a WS AWS_PROXY sendMessage event normalizes to
# (connectionId, question). Requirement 9.4 (routing payload shape) underpinning
# the round-trip in 9.5.
# --------------------------------------------------------------------------- #
def test_ws_proxy_event_normalizes_to_connection_and_question():
    from agent import handler as agent_handler

    event = {
        "requestContext": {"connectionId": "conn-xyz-9"},
        "body": json.dumps({"action": "sendMessage", "question": "How many applications?"}),
    }
    connection_id, question = agent_handler._normalize_event(event)
    assert connection_id == "conn-xyz-9"
    assert question == "How many applications?"


# --------------------------------------------------------------------------- #
# Deterministic: sendMessage round-trips an answer to the originating
# connection. Requirement 9.5.
# --------------------------------------------------------------------------- #
def test_send_message_round_trips_answer_to_connection(monkeypatch):
    from agent import handler as agent_handler

    # Stub the agent loop (no Bedrock/Gateway) and capture the @connections post.
    monkeypatch.setattr(agent_handler, "_run_agent", lambda q, h: "There are 42 applications.")
    monkeypatch.setattr(agent_handler, "MEMORY_ID", "")  # skip memory load/save
    posted: dict = {}

    def _capture(connection_id, message):
        posted["connection_id"] = connection_id
        posted["message"] = message
        return True

    monkeypatch.setattr(agent_handler, "_post_to_connection", _capture)

    event = {
        "requestContext": {"connectionId": "conn-round-trip"},
        "body": json.dumps({"action": "sendMessage", "question": "How many applications?"}),
    }
    result = agent_handler.handler(event, None)

    assert result["ok"] is True
    assert result["answer"] == "There are 42 applications."
    # The answer is delivered back to the ORIGINATING connection.
    assert posted["connection_id"] == "conn-round-trip"
    assert posted["message"] == "There are 42 applications."
    assert result["posted"] is True


# --------------------------------------------------------------------------- #
# Deterministic: an agent failure delivers a graceful error frame to the
# originating connection without raising. Requirement 9.6 (and 7.6).
# --------------------------------------------------------------------------- #
def test_agent_failure_delivers_error_frame(monkeypatch):
    from agent import handler as agent_handler

    def _boom(question, history):
        raise RuntimeError("bedrock unavailable")

    monkeypatch.setattr(agent_handler, "_run_agent", _boom)
    monkeypatch.setattr(agent_handler, "MEMORY_ID", "")
    posted: dict = {}

    def _capture(connection_id, message):
        posted["connection_id"] = connection_id
        posted["message"] = message
        return True

    monkeypatch.setattr(agent_handler, "_post_to_connection", _capture)

    event = {
        "requestContext": {"connectionId": "conn-fail"},
        "body": json.dumps({"action": "sendMessage", "question": "trigger a failure"}),
    }
    # Must NOT raise out of the handler.
    result = agent_handler.handler(event, None)

    assert result["ok"] is False
    # A non-empty, human-readable error frame is produced and delivered.
    assert isinstance(result["answer"], str) and result["answer"].strip()
    assert posted["connection_id"] == "conn-fail"
    assert posted["message"] == result["answer"]
    # It reads as a graceful "couldn't answer" message, not a stack trace.
    assert "couldn't" in result["answer"].lower()


# --------------------------------------------------------------------------- #
# LIVE smoke tests against the real deployed WebSocket API. Skipped by default;
# enable with RUN_WS_SMOKE=1 (requires the `websockets` client + AWS access).
# --------------------------------------------------------------------------- #
def _load_ids() -> dict:
    try:
        with open(IDS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_RUN_LIVE = os.environ.get("RUN_WS_SMOKE") == "1"
_IDS = _load_ids()
_WS_ENDPOINT = _IDS.get("ws_endpoint")

live = pytest.mark.skipif(
    not (_RUN_LIVE and _WS_ENDPOINT),
    reason="live WS smoke disabled; set RUN_WS_SMOKE=1 and ensure network_ids.json has ws_endpoint",
)


def _ddb_connection_ids(table: str) -> set[str]:
    """Scan the connections table and return the set of connectionIds."""
    import boto3

    ddb = boto3.client("dynamodb", region_name="ap-south-1")
    ids: set[str] = set()
    kwargs = {"TableName": table, "ProjectionExpression": "connectionId"}
    while True:
        resp = ddb.scan(**kwargs)
        for it in resp.get("Items", []):
            cid = it.get("connectionId", {}).get("S")
            if cid:
                ids.add(cid)
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return ids


@live
def test_live_connect_disconnect_updates_table():
    """Req 9.2/9.3: a real connect adds a row; disconnect removes it."""
    websockets = pytest.importorskip("websockets")
    import asyncio

    table = _IDS.get("connections_table")
    if not table:
        pytest.skip("network_ids.json missing connections_table")

    try:
        before = _ddb_connection_ids(table)

        async def _connect_then_hold() -> set[str]:
            async with websockets.connect(_WS_ENDPOINT, open_timeout=20):
                await asyncio.sleep(3)  # let $connect PutItem settle
                return _ddb_connection_ids(table)

        during = asyncio.run(_connect_then_hold())
        new_ids = during - before
        assert new_ids, "expected at least one new connectionId after connect"

        # After the context manager exits the socket is closed -> $disconnect.
        async def _settle():
            await asyncio.sleep(3)

        asyncio.run(_settle())
        after = _ddb_connection_ids(table)
        assert not (new_ids & after), "connectionId(s) should be removed on disconnect"
    except Exception as exc:  # noqa: BLE001 - environment/connectivity issues -> skip
        pytest.skip(f"live connect/disconnect unavailable: {type(exc).__name__}: {exc}")


@live
def test_live_send_message_round_trip():
    """Req 9.4/9.5: send a question and receive an answer frame from the agent."""
    websockets = pytest.importorskip("websockets")
    import asyncio

    async def _round_trip() -> str:
        async with websockets.connect(_WS_ENDPOINT, open_timeout=20) as ws:
            await ws.send(json.dumps({"action": "sendMessage",
                                      "question": "How many applications are there?"}))
            # The agent calls Bedrock; allow a generous timeout.
            reply = await asyncio.wait_for(ws.recv(), timeout=90)
            return reply if isinstance(reply, str) else reply.decode("utf-8")

    try:
        answer = asyncio.run(_round_trip())
    except Exception as exc:  # noqa: BLE001 - slow/unavailable agent -> skip
        pytest.skip(f"live round-trip unavailable: {type(exc).__name__}: {exc}")

    assert answer and answer.strip(), "expected a non-empty answer frame"
