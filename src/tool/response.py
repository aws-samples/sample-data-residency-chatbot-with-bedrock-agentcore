"""Response shaping + log redaction for the Tool_Lambda (Task 4.7).

WHY this exists
---------------
``shape_response`` turns a raw result set (psycopg rows + column names) into the
structured JSON the Parameterized_Query_API contract promises — ``table``,
``row_count``, ``columns``, ``rows``, ``truncated`` — with ``row_count`` always
equal to ``len(rows)`` (Req 5.7; Property 5).

``redact`` scrubs DB credential values out of any string before it is logged, so
the database username/password never reach CloudWatch (Req 11.6, 12.4;
Property 11).

This module is PURE: no AWS calls, no DB connection, no I/O.
"""
from __future__ import annotations

# Placeholder substituted in place of any secret value found in log text.
_REDACTION = "***REDACTED***"


def shape_response(rows, columns, table: str, truncated: bool = False) -> dict:
    """Shape a result set into the Parameterized_Query_API response (Property 5).

    Args:
        rows: an iterable of row values. Each row may be a mapping (already
            keyed by column) or a positional sequence aligned with ``columns``.
        columns: ordered column names for the result set.
        table: the queried table name.
        truncated: whether the result was cut off by the row limit.

    Returns:
        ``{"table", "row_count", "columns", "rows", "truncated"}`` where ``rows``
        is a list of dict objects (column -> value) and ``row_count`` equals
        ``len(rows)`` (Req 5.7).
    """
    col_list = list(columns)
    shaped_rows: list[dict] = []
    for row in rows:
        if isinstance(row, dict):
            shaped_rows.append({c: row.get(c) for c in col_list})
        else:
            # positional sequence aligned to columns
            seq = list(row)
            shaped_rows.append(
                {c: (seq[i] if i < len(seq) else None) for i, c in enumerate(col_list)}
            )

    return {
        "table": table,
        "row_count": len(shaped_rows),
        "columns": col_list,
        "rows": shaped_rows,
        "truncated": bool(truncated),
    }


def redact(text: object, secret: object) -> str:
    """Remove secret value(s) from ``text`` before logging (Property 11).

    Args:
        text: the log message (coerced to ``str``).
        secret: a single secret string, or an iterable/mapping of secret values
            (e.g. a parsed Secrets Manager JSON: its values are redacted).

    Returns:
        ``str(text)`` with every non-empty secret value replaced by a redaction
        placeholder, so no credential value remains in the output (Req 11.6,
        12.4).
    """
    out = str(text)
    for value in _iter_secret_values(secret):
        if value:  # never replace on an empty string (would corrupt all text)
            out = out.replace(value, _REDACTION)
    return out


def _iter_secret_values(secret):
    """Yield string secret values from a str / mapping / iterable of secrets."""
    if secret is None:
        return
    if isinstance(secret, str):
        yield secret
        return
    if isinstance(secret, dict):
        for v in secret.values():
            yield from _iter_secret_values(v)
        return
    if isinstance(secret, (list, tuple, set, frozenset)):
        for v in secret:
            yield from _iter_secret_values(v)
        return
    # any other scalar -> stringify
    yield str(secret)
