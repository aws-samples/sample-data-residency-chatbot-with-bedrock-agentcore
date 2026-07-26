"""Graceful tool-error wrapping for the Agent_Lambda (Task 14.1, Req 7.6).

WHY this exists
---------------
When a Gateway tool invocation (or the model's use of it) fails, the user must
still get a polite, human-readable answer — never a stack trace, and the handler
must never raise out to the WebSocket caller (Req 7.6; Property 9).

``wrap_tool_error`` is PURE: it accepts ANY error shape the agent loop might
surface (a raised ``Exception``, a string, a structured tool error dict such as
``{"error": ..., "executed": false}``, ``None``, or any other object) and ALWAYS
returns a non-empty natural-language string. It never raises.
"""
from __future__ import annotations

# Base message shown to the user when a tool fails (always non-empty).
_BASE_MESSAGE = (
    "Sorry, I couldn't answer that question right now because a data lookup "
    "failed. Please try rephrasing your question or ask about something else "
    "in the MNRE PM Surya Ghar data."
)


def _extract_detail(err: object) -> str:
    """Best-effort, never-raising extraction of a short error detail string."""
    try:
        if err is None:
            return ""
        if isinstance(err, str):
            return err.strip()
        if isinstance(err, dict):
            # Structured tool error, e.g. {"error": "...", "executed": false}.
            for key in ("error", "message", "Error", "detail"):
                val = err.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            return ""
        if isinstance(err, BaseException):
            text = str(err).strip()
            return text or err.__class__.__name__
        text = str(err).strip()
        return text
    except Exception:  # noqa: BLE001 - extraction must never raise
        return ""


def wrap_tool_error(err: object) -> str:
    """Return a non-empty natural-language "couldn't answer" message (Req 7.6).

    Args:
        err: any error representation — a raised exception, a string, a
            structured tool-error dict, ``None``, or any other object.

    Returns:
        A non-empty, user-facing string explaining the question could not be
        answered. This function NEVER raises.
    """
    detail = _extract_detail(err)
    if detail:
        # Keep the detail short so we never dump a stack trace at the user.
        snippet = detail.replace("\n", " ").strip()
        if len(snippet) > 200:
            snippet = snippet[:200].rstrip() + "..."
        return f"{_BASE_MESSAGE} (details: {snippet})"
    return _BASE_MESSAGE
