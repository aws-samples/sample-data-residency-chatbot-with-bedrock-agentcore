"""Parameterized_Query_API request validation for the Tool_Lambda (Task 4.1).

WHY this exists
---------------
The Tool_Lambda NEVER accepts raw SQL. Before any SQL is built or executed, the
incoming structured request is validated against the per-table whitelist from
``common.schema`` (the single source of truth). Any request that references an
unsupported table, column, operator, or aggregation function — or that smuggles
raw SQL — is rejected with a descriptive error and ``executed=False``, and NO
query is built or run (Req 5.1, 5.4, 5.6, 5.10, 11.7; Property 3).

This module is PURE: no AWS calls, no DB connection, no I/O. Import as::

    from tool.validate import validate_request
    err = validate_request(req)
    if err is not None:   # {'executed': False, 'error': ...}
        return err        # reject, run nothing

``validate_request`` returns ``None`` when the request is contract-valid, or an
error dict ``{"executed": False, "error": "<reason>"}`` on the FIRST violation.
"""
from __future__ import annotations

from common.schema import TABLES, get_whitelist

# Top-level request keys that are part of the Parameterized_Query_API contract.
# Anything else (notably a 'sql'/'query'/'raw_sql' key) is treated as an attempt
# to smuggle raw SQL / out-of-contract input and is rejected (Req 5.6, 11.7).
_ALLOWED_REQUEST_KEYS = frozenset(
    {"table", "filters", "group_by", "aggregations", "having", "order_by", "limit"}
)

# Operators that take NO value (the value field is ignored / must be absent).
_NULLARY_OPS = frozenset({"is_null", "not_null"})

# Comparison operators allowed in a HAVING condition (value-bearing only).
_HAVING_OPS = frozenset({"eq", "ne", "gt", "gte", "lt", "lte"})


def _error(message: str) -> dict:
    """Build the standard rejection envelope (no query executed)."""
    return {"executed": False, "error": message}


def validate_request(req: object) -> dict | None:
    """Validate a Parameterized_Query_API request against the schema whitelist.

    Returns ``None`` when the request is contract-valid (safe to build/execute),
    otherwise an error dict ``{"executed": False, "error": ...}`` describing the
    first violation found. NEVER raises for contract violations and NEVER builds
    or runs a query (Property 3; Req 5.1, 5.4, 5.6, 5.10, 11.7).

    Validation rules:
      - request must be a dict whose keys are all within the API contract
        (a stray ``sql``/``query``/``raw_sql`` key => raw-SQL rejection);
      - ``table`` must be one of the four curated tables;
      - every ``filters[*].column`` / ``group_by[*]`` / ``aggregations[*].column``
        must be a whitelisted column;
      - every ``filters[*].op`` must be a whitelisted operator;
      - every ``aggregations[*].fn`` must be a whitelisted aggregation function;
      - a non-``count`` aggregation may only target a numeric column.
    """
    if not isinstance(req, dict):
        return _error(f"request must be an object, got {type(req).__name__}")

    # Reject raw SQL / any out-of-contract top-level key (Req 5.6, 11.7).
    extra_keys = set(req.keys()) - _ALLOWED_REQUEST_KEYS
    if extra_keys:
        return _error(
            "raw SQL or unsupported field(s) not allowed: "
            f"{', '.join(sorted(str(k) for k in extra_keys))}"
        )

    table = req.get("table")
    if table not in TABLES:
        return _error(
            f"unsupported table '{table}'; expected one of {', '.join(TABLES)}"
        )

    wl = get_whitelist(table)
    columns = wl["columns"]
    filter_ops = wl["filter_ops"]
    agg_fns = wl["agg_fns"]
    date_diff_fns = wl["date_diff_fns"]
    numeric_columns = wl["numeric_columns"]
    date_columns = wl["date_columns"]

    # ----- filters -----
    filters = req.get("filters") or []
    if not isinstance(filters, list):
        return _error("'filters' must be a list")
    for f in filters:
        if not isinstance(f, dict):
            return _error("each filter must be an object with 'column' and 'op'")
        col = f.get("column")
        op = f.get("op")
        if col not in columns:
            return _error(f"unsupported column '{col}' for table '{table}'")
        if op not in filter_ops:
            return _error(
                f"unsupported operator '{op}'; expected one of "
                f"{', '.join(sorted(filter_ops))}"
            )
        # value-bearing ops require a 'value'; 'in' requires a non-empty list.
        if op not in _NULLARY_OPS:
            if "value" not in f:
                return _error(f"operator '{op}' on '{col}' requires a 'value'")
            if op == "in":
                val = f.get("value")
                if not isinstance(val, (list, tuple)) or len(val) == 0:
                    return _error(
                        f"operator 'in' on '{col}' requires a non-empty list value"
                    )

    # ----- group_by -----
    group_by = req.get("group_by") or []
    if not isinstance(group_by, list):
        return _error("'group_by' must be a list")
    for col in group_by:
        if col not in columns:
            return _error(f"unsupported column '{col}' for table '{table}'")

    # ----- aggregations -----
    aggregations = req.get("aggregations") or []
    if not isinstance(aggregations, list):
        return _error("'aggregations' must be a list")
    for agg in aggregations:
        if not isinstance(agg, dict):
            return _error("each aggregation must be an object with 'fn' and 'column'")
        fn = agg.get("fn")
        col = agg.get("column")
        # Date-difference aggregations (TAT/SLA): fn in DATE_DIFF_FNS operate on
        # two DATE columns — 'column' (start) and 'column2' (end). Report the
        # day gap (column2 - column).
        if fn in date_diff_fns:
            col2 = agg.get("column2")
            if col not in date_columns:
                return _error(
                    f"date-difference '{fn}' requires a date 'column', "
                    f"but '{col}' is not a date column"
                )
            if col2 not in date_columns:
                return _error(
                    f"date-difference '{fn}' requires a date 'column2', "
                    f"but '{col2}' is not a date column"
                )
            continue
        if fn not in agg_fns:
            return _error(
                f"unsupported aggregation '{fn}'; expected one of "
                f"{', '.join(sorted(agg_fns | date_diff_fns))}"
            )
        if col not in columns:
            return _error(f"unsupported column '{col}' for table '{table}'")
        # Only count() may run on a non-numeric column (Req 5.4).
        if fn != "count" and col not in numeric_columns:
            return _error(
                f"aggregation '{fn}' requires a numeric column, "
                f"but '{col}' is not numeric"
            )

    # ----- having (post-aggregation filter on an aggregated value) -----
    # Enables "groups that appear N+ times" style questions. Each condition is
    # {fn, column, op, value}: it compares FN(column) against a numeric value,
    # e.g. {fn:count, column:application_id, op:gte, value:2}. Requires group_by.
    having = req.get("having") or []
    if not isinstance(having, list):
        return _error("'having' must be a list")
    if having and not group_by:
        return _error("'having' requires 'group_by'")
    for h in having:
        if not isinstance(h, dict):
            return _error("each having condition must be an object {fn, column, op, value}")
        fn = h.get("fn")
        col = h.get("column")
        op = h.get("op")
        if fn not in agg_fns:
            return _error(
                f"unsupported having aggregation '{fn}'; expected one of "
                f"{', '.join(sorted(agg_fns))}"
            )
        if col not in columns:
            return _error(f"unsupported column '{col}' for table '{table}'")
        if fn != "count" and col not in numeric_columns:
            return _error(
                f"having aggregation '{fn}' requires a numeric column, "
                f"but '{col}' is not numeric"
            )
        if op not in _HAVING_OPS:
            return _error(
                f"unsupported having operator '{op}'; expected one of "
                f"{', '.join(sorted(_HAVING_OPS))}"
            )
        val = h.get("value")
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            try:
                float(val)
            except (TypeError, ValueError):
                return _error(
                    f"having condition on '{fn}({col})' requires a numeric 'value'"
                )

    # ----- order_by (sort results; enables "top N" / "highest"/"most" queries) -----
    # Each item is {by, direction?}: `by` is a whitelisted column OR an aggregation
    # alias declared in `aggregations`; direction is 'asc' or 'desc' (default desc).
    order_by = req.get("order_by") or []
    if not isinstance(order_by, list):
        return _error("'order_by' must be a list")
    agg_aliases = {
        a.get("alias") for a in aggregations
        if isinstance(a, dict) and isinstance(a.get("alias"), str) and a.get("alias")
    }
    for o in order_by:
        if not isinstance(o, dict):
            return _error("each order_by item must be an object {by, direction}")
        by = o.get("by")
        direction = (o.get("direction") or "desc").lower()
        if by not in columns and by not in agg_aliases:
            return _error(
                f"unsupported order_by '{by}'; must be a column or an aggregation alias"
            )
        if direction not in ("asc", "desc"):
            return _error("order_by direction must be 'asc' or 'desc'")

    return None
