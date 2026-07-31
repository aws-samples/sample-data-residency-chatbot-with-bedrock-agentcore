"""AgentCore Memory adapter for the chatbot (Task 13.2, Req 7.7, 8.2-8.6).

Short-term conversational memory: each chat turn is stored as ONE AgentCore
``CreateEvent`` carrying two conversational payload items (a USER message and an
ASSISTANT message) under ``(actorId, sessionId)``. Follow-ups are reconstructed
by listing the session's events oldest-first.

PURITY / TESTABILITY
--------------------
There is NO module-level boto3 client. The caller constructs a region-pinned
``bedrock-agentcore`` (data-plane) client and passes it as the first argument to
both functions, so it can be region-pinned in production (``region_name=
"ap-south-1"``) and replaced by a fake in tests (Property 10).

API shapes (confirmed against AWS docs):
  - create_event(memoryId, actorId, sessionId, eventTimestamp, payload=[...])
      payload item: {"conversational": {"content": {"text": str}, "role": ROLE}}
      ROLE in {"USER","ASSISTANT","TOOL","OTHER"}.
  - list_events(memoryId, actorId, sessionId, includePayloads=True, maxResults<=100,
      nextToken) -> {"events": [{"eventTimestamp","payload":[...]}, ...], "nextToken"}.
    ListEvents returns events; we sort oldest-first by eventTimestamp so the
    round-trip preserves save order regardless of server-side ordering.

TURN SHAPE returned by ``load``
-------------------------------
A list of dicts, one per saved turn, in the order saved::

    [{"user": "<user_text>", "assistant": "<assistant_text>"}, ...]

An empty session returns ``[]``.
"""
from __future__ import annotations

import datetime as _dt
import re as _re

# Stable demo actor id (Req 8.2). sessionId is the WebSocket connectionId,
# passed in by the caller.
DEFAULT_ACTOR_ID = "demo-user"

_ROLE_USER = "USER"
_ROLE_ASSISTANT = "ASSISTANT"
_LIST_PAGE_SIZE = 100  # ListEvents hard max


def safe_session_id(connection_id: str) -> str:
    """Map an arbitrary WebSocket connectionId to an AgentCore-valid sessionId.

    AgentCore Memory requires sessionId to match ``[a-zA-Z0-9][a-zA-Z0-9-_]*``.
    Raw WebSocket connectionIds (e.g. ``gR4Mqy_pYC5gKAIUjA==``) contain ``=``,
    ``/`` or ``+`` and fail that constraint, so both CreateEvent and ListEvents
    raise ValidationException. We deterministically replace every disallowed
    character with ``_`` and prefix an alphanumeric if the first char isn't one,
    so save and load derive the SAME key (Req 8.2-8.4).
    """
    cid = str(connection_id or "")
    cleaned = "".join(c if (c.isascii() and c.isalnum()) or c in "-_" else "_" for c in cid)
    if not cleaned:
        return "s"
    if not (cleaned[0].isascii() and cleaned[0].isalnum()):
        cleaned = "s" + cleaned
    return cleaned

# AgentCore (CreateEvent/ListEvents) requires sessionId to match
# ^[a-zA-Z0-9][a-zA-Z0-9-_]*$ . WebSocket connectionIds (e.g. "gR4Mqy_pYC5gKAIUjA==")
# contain '=' which violates this, so we deterministically sanitize before use.
_INVALID_SESSION_CHARS = _re.compile(r"[^a-zA-Z0-9_-]")


def sanitize_session_id(session_id: str) -> str:
    """Map any connectionId to a regex-valid AgentCore sessionId, deterministically.

    Replaces every char outside ``[a-zA-Z0-9-_]`` with ``_`` and guarantees the
    first character is alphanumeric (prefixing ``s`` when it is not). The mapping
    is stable, so the same connectionId always round-trips to the same key (used
    consistently by both ``save`` and ``load``).
    """
    cleaned = _INVALID_SESSION_CHARS.sub("_", session_id or "")
    if not cleaned or not cleaned[0].isalnum():
        cleaned = "s" + cleaned
    return cleaned


def save(client, memory_id, actor_id, session_id, user_text, assistant_text):
    """Persist one conversational turn (USER + ASSISTANT) via CreateEvent.

    Args:
        client: a region-pinned ``bedrock-agentcore`` data-plane client (or a
            test fake exposing ``create_event``).
        memory_id: the AgentCore Memory resource id.
        actor_id: stable actor id (e.g. ``DEFAULT_ACTOR_ID``).
        session_id: the session key = WebSocket connectionId.
        user_text: the user's message for this turn.
        assistant_text: the assistant's response for this turn.

    Returns:
        The ``create_event`` response from the client.
    """
    payload = [
        {"conversational": {"content": {"text": user_text}, "role": _ROLE_USER}},
        {"conversational": {"content": {"text": assistant_text}, "role": _ROLE_ASSISTANT}},
    ]
    return client.create_event(
        memoryId=memory_id,
        actorId=actor_id,
        sessionId=sanitize_session_id(session_id),
        eventTimestamp=_dt.datetime.now(_dt.timezone.utc),
        payload=payload,
    )


def _iter_events(client, memory_id, actor_id, session_id):
    """Yield every event for the session, following pagination."""
    token = None
    while True:
        kwargs = {
            "memoryId": memory_id,
            "actorId": actor_id,
            "sessionId": session_id,
            "includePayloads": True,
            "maxResults": _LIST_PAGE_SIZE,
        }
        if token:
            kwargs["nextToken"] = token
        resp = client.list_events(**kwargs)
        for ev in resp.get("events", []):
            yield ev
        token = resp.get("nextToken")
        if not token:
            return


def _event_to_turn(event):
    """Reduce one event's conversational payload to {"user","assistant"}.

    Picks the first USER and first ASSISTANT message in the payload; missing
    roles default to an empty string so the shape is always uniform.
    """
    user_text = ""
    assistant_text = ""
    seen_user = False
    seen_assistant = False
    for item in event.get("payload", []):
        conv = item.get("conversational")
        if not conv:
            continue
        role = conv.get("role")
        text = conv.get("content", {}).get("text", "")
        if role == _ROLE_USER and not seen_user:
            user_text = text
            seen_user = True
        elif role == _ROLE_ASSISTANT and not seen_assistant:
            assistant_text = text
            seen_assistant = True
    return {"user": user_text, "assistant": assistant_text}


def load(client, memory_id, actor_id, session_id):
    """Retrieve prior turns for a session, oldest-first (save order).

    Args:
        client: a region-pinned ``bedrock-agentcore`` data-plane client (or a
            test fake exposing ``list_events``).
        memory_id: the AgentCore Memory resource id.
        actor_id: stable actor id.
        session_id: the session key = WebSocket connectionId.

    Returns:
        A list of ``{"user": str, "assistant": str}`` turns in the order they
        were saved. An empty session returns ``[]`` (Req 8.6).
    """
    events = list(_iter_events(client, memory_id, actor_id, sanitize_session_id(session_id)))
    # Sort oldest-first so retrieval order == save order regardless of the
    # server's default ordering. Events created in sequence have monotonic
    # timestamps; ties keep insertion order (stable sort).
    events.sort(key=lambda e: e.get("eventTimestamp") or 0)
    return [_event_to_turn(ev) for ev in events]
