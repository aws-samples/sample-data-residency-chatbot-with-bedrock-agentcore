"""Safe, parameterized, whitelist-only SQL builder for the Tool_Lambda (Task 4.3/4.5).

WHY this exists
---------------
The Tool_Lambda turns a validated Parameterized_Query_API request into a single
``SELECT`` statement. EVERY identifier (table, column, aggregation target,
alias) comes ONLY from the per-table whitelist in ``common.schema``; EVERY
literal value (filter operands and the row limit) is bound as a psycopg ``%s``
parameter so no supplied value is ever inlined into the SQL string. This
eliminates SQL injection (Req 5.1, 5.4, 5.5, 11.7; Property 2).

This module is PURE: no AWS calls, no DB connection, no I/O.

    from tool.query_builder import clamp_limit, build_query
    sql, params = build_query(req)   # req MUST already be validated

``build_query`` assumes the request has passed ``tool.validate.validate_request``
(it re-derives identifiers from the whitelist defensively all the same).
"""
from __future__ import annotations

from common.schema import DATE_DIFF_FNS, TEXT_COLUMNS, get_whitelist

# Row-limit policy (Req 5.8, 5.9).
DEFAULT_LIMIT = 100
MIN_LIMIT = 1
MAX_LIMIT = 1000

# Map API operator -> SQL comparison operator for value-bearing ops.
_OP_SQL = {
    "eq": "=",
    "ne": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "like": "LIKE",
}
_NULLARY_SQL = {"is_null": "IS NULL", "not_null": "IS NOT NULL"}


def clamp_limit(n: object) -> int:
    """Clamp a requested row limit into ``[MIN_LIMIT, MAX_LIMIT]`` (Property 4).

    Applies the default when ``n`` is missing/None or not an integer-like value,
    and clamps zero/negative/over-max into range. Always returns an int in
    ``1 <= result <= 1000`` (Req 5.8, 5.9).
    """
    if n is None:
        return DEFAULT_LIMIT
    # Accept ints and integral floats / numeric strings; anything else -> default.
    try:
        if isinstance(n, bool):  # bool is an int subclass; treat as invalid
            return DEFAULT_LIMIT
        value = int(n)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_LIMIT
    if value < MIN_LIMIT:
        return MIN_LIMIT
    if value > MAX_LIMIT:
        return MAX_LIMIT
    return value


def build_query(req: dict) -> tuple[str, list]:
    """Build a SELECT-only, parameterized, whitelist-only query (Property 2).

    Args:
        req: A Parameterized_Query_API request that has already been validated
            by ``tool.validate.validate_request``.

    Returns:
        ``(sql, params)`` where ``sql`` is a string beginning with ``SELECT``
        using only whitelisted identifiers, and ``params`` is the ordered list
        of literal values to bind (every filter operand, then the limit) — no
        literal value appears in ``sql`` itself.
    """
    table = req["table"]
    wl = get_whitelist(table)
    columns = wl["columns"]

    group_by = list(req.get("group_by") or [])
    aggregations = list(req.get("aggregations") or [])
    filters = list(req.get("filters") or [])

    params: list = []

    # ----- SELECT list -----
    select_parts: list[str] = []
    # group_by columns are projected first (only whitelisted identifiers).
    for col in group_by:
        if col in columns:
            select_parts.append(col)
    # aggregations: FN(column) [AS alias]
    for agg in aggregations:
        fn = agg["fn"]
        col = agg["column"]
        # Date-difference aggregation: FN_days(column2 - column) in whole days.
        if fn in DATE_DIFF_FNS:
            col2 = agg.get("column2")
            if col not in columns or col2 not in columns:
                continue
            sql_fn = {"avg_days": "AVG", "min_days": "MIN", "max_days": "MAX"}[fn]
            # Postgres date subtraction (date - date) yields an integer day count.
            expr = f"{sql_fn}({col2} - {col})"
            alias = agg.get("alias")
            if alias and _is_safe_alias(alias):
                expr += f" AS {alias}"
            select_parts.append(expr)
            continue
        if col not in columns:
            continue
        expr = f"{fn.upper()}({col})"
        alias = agg.get("alias")
        if alias and _is_safe_alias(alias):
            expr += f" AS {alias}"
        select_parts.append(expr)
    # No projection requested -> select the whitelisted columns explicitly
    # (never SELECT *), keeping output deterministic and whitelist-only.
    if not select_parts:
        select_parts = list(columns)

    # nosec B608: not an injection vector — every identifier in select_parts /
    # table comes only from the per-table whitelist in common.schema (validated
    # by tool.validate and re-checked above); every literal VALUE is bound as a
    # psycopg %s parameter, never interpolated. Covered by property tests
    # (tests/test_query_builder_properties.py).
    sql = f"SELECT {', '.join(select_parts)} FROM {table}"  # nosec B608

    # ----- WHERE -----
    where_parts: list[str] = []
    for f in filters:
        col = f["column"]
        op = f["op"]
        if col not in columns:
            continue
        if op in _NULLARY_SQL:
            where_parts.append(f"{col} {_NULLARY_SQL[op]}")
        elif op == "in":
            values = list(f["value"])
            # Case-insensitive IN for text columns so values match regardless
            # of source casing (e.g. data stores 'MAHARASHTRA').
            if col in TEXT_COLUMNS:
                placeholders = ", ".join(["UPPER(%s)"] * len(values))
                where_parts.append(f"UPPER({col}) IN ({placeholders})")
            else:
                placeholders = ", ".join(["%s"] * len(values))
                where_parts.append(f"{col} IN ({placeholders})")
            params.extend(values)
        elif op in ("eq", "ne") and col in TEXT_COLUMNS:
            # Case-insensitive equality for text columns: the source data may be
            # upper/mixed case (states are stored UPPERCASE), but the model may
            # supply title-case values. Compare on UPPER() of both sides so a
            # filter like state eq 'Maharashtra' matches 'MAHARASHTRA'.
            where_parts.append(f"UPPER({col}) {_OP_SQL[op]} UPPER(%s)")
            params.append(f["value"])
        elif op == "like" and col in TEXT_COLUMNS:
            # Case-insensitive LIKE for text columns.
            where_parts.append(f"{col} ILIKE %s")
            params.append(f["value"])
        else:
            where_parts.append(f"{col} {_OP_SQL[op]} %s")
            params.append(f["value"])
    if where_parts:
        sql += " WHERE " + " AND ".join(where_parts)

    # ----- GROUP BY -----
    if group_by:
        safe_group = [c for c in group_by if c in columns]
        if safe_group:
            sql += " GROUP BY " + ", ".join(safe_group)

    # ----- HAVING (post-aggregation filter, e.g. COUNT(col) >= 2) -----
    having = list(req.get("having") or [])
    having_parts: list[str] = []
    for h in having:
        col = h["column"]
        fn = h["fn"]
        op = h["op"]
        if col not in columns or op not in _OP_SQL:
            continue
        having_parts.append(f"{fn.upper()}({col}) {_OP_SQL[op]} %s")
        params.append(h["value"])
    if having_parts:
        sql += " HAVING " + " AND ".join(having_parts)

    # ----- ORDER BY (sort; for "top N" / "highest" / "most" questions) -----
    order_by = list(req.get("order_by") or [])
    if order_by:
        agg_aliases = {
            a.get("alias") for a in aggregations
            if isinstance(a, dict) and isinstance(a.get("alias"), str)
        }
        order_parts: list[str] = []
        for o in order_by:
            by = o.get("by")
            direction = (o.get("direction") or "desc").lower()
            if by not in columns and by not in agg_aliases:
                continue
            if direction not in ("asc", "desc"):
                direction = "desc"
            # `by` is a whitelisted column or a validated alias (safe identifier);
            # direction is a fixed keyword — neither is a user literal. NULLS LAST
            # so a DESC leaderboard surfaces real values, not empty/NULL rows.
            nulls = "NULLS LAST" if direction == "desc" else "NULLS FIRST"
            order_parts.append(f"{by} {direction.upper()} {nulls}")
        if order_parts:
            sql += " ORDER BY " + ", ".join(order_parts)

    # ----- LIMIT (always bound as a parameter) -----
    sql += " LIMIT %s"
    params.append(clamp_limit(req.get("limit")))

    return sql, params


def _is_safe_alias(alias: object) -> bool:
    """Allow only simple identifier aliases (letters/digits/underscore)."""
    return (
        isinstance(alias, str)
        and alias != ""
        and (alias[0].isalpha() or alias[0] == "_")
        and all(ch.isalnum() or ch == "_" for ch in alias)
    )
