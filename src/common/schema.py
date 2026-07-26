"""Curated_Schema — single source of truth for the 4 MNRE tables (Requirement 3).

WHY this exists
---------------
The four source datasets (applications, subsidy, installation, inspection) are
lifecycle snapshots of the SAME 162-column PM Surya Ghar record, so a single
curated, typed schema is applied to all four tables (Req 3.1). This module
encodes that schema ONCE so two very different consumers stay in lock-step:

  - the loader  (``loader/load.py``) — uses ``source_kind`` to coerce CSV text
    into Postgres types, and uses ``CURATED_COLUMNS`` to project rows.
  - the Tool_Lambda (``src/tool/*``) — uses the per-table WHITELIST to validate
    requests and to build safe, parameterized, whitelist-only SQL.

It is PURE PYTHON: no AWS calls, no DB connection, no I/O. Import as::

    from common.schema import (
        TABLES, CURATED_COLUMNS, NUMERIC_COLUMNS, get_whitelist,
        ddl_for_table, index_ddl_for_table,
    )

Type mapping rationale (verified against the source CSV header)
---------------------------------------------------------------
  - identifiers / geography / classification / vendor / bank / status → ``text``
  - monetary + quantity (source is text, may be blank) → ``numeric`` (Req 3.4)
  - flags ('Yes'/'No' source) → ``boolean`` (Req 3.5)
  - dates (ISO ``yyyy-MM-dd HH:mm:ss``; loader also accepts ``dd-MMM-yyyy``)
    → ``date`` (Req 3.3)

``application_id`` is the primary identifier for every table (Req 3.6). Curated
column names match the CSV header EXACTLY — including the source misspelling
``benefiaicry_unique_id_by_pfms`` — so ``SOURCE_COLUMN`` is an identity map.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Tables (Req 3.1) — all four share this same curated schema.
# --------------------------------------------------------------------------- #
TABLES: tuple[str, ...] = ("applications", "subsidy", "installation", "inspection")

# The single primary identifier column for every table (Req 3.6).
PRIMARY_KEY: str = "application_id"

# Columns indexed per table for common-filter performance (see design Indexes).
INDEXED_COLUMNS: tuple[str, ...] = (
    "state",
    "district",
    "discom",
    "current_stage",
    "name_of_bank",
)

# Allowed filter operators and aggregation functions for the Parameterized_Query_API.
ALLOWED_FILTER_OPS: frozenset[str] = frozenset(
    {"eq", "ne", "gt", "gte", "lt", "lte", "like", "in", "is_null", "not_null"}
)
ALLOWED_AGG_FNS: frozenset[str] = frozenset({"count", "sum", "avg", "min", "max"})

# Date-difference aggregation functions (for TAT/SLA metrics). Each operates on
# TWO date columns and reports the day-gap (column2 - column): average/min/max.
DATE_DIFF_FNS: frozenset[str] = frozenset({"avg_days", "min_days", "max_days"})


# --------------------------------------------------------------------------- #
# Column registry — ordered single source of truth (drives DDL + coercion).
#
# Each entry: (name, pg_type, source_kind)
#   source_kind in {'text', 'numeric', 'date', 'boolean'} → loader coercion +
#   whitelist categorisation. ``application_id`` carries the PRIMARY KEY.
# --------------------------------------------------------------------------- #
# Format: (column_name, postgres_type, source_kind)
COLUMN_REGISTRY: tuple[tuple[str, str, str], ...] = (
    # identifiers
    ("application_id", "text", "text"),  # PRIMARY KEY (Req 3.6)
    ("application_number", "text", "text"),
    # geography (state/district/discom indexed)
    ("state", "text", "text"),
    ("district", "text", "text"),
    ("discom", "text", "text"),
    ("rural_urban", "text", "text"),
    # classification
    ("category", "text", "text"),
    ("consumer_category", "text", "text"),
    ("gender", "text", "text"),
    ("scheme", "text", "text"),
    # vendor
    ("vendor_name", "text", "text"),
    ("vendor_id", "text", "text"),
    # bank (name_of_bank indexed)
    ("name_of_bank", "text", "text"),
    ("bank_account_number", "text", "text"),
    ("benefiaicry_unique_id_by_pfms", "text", "text"),  # source spelling preserved
    ("duplicate_bank_account_number", "boolean", "boolean"),  # 'Yes'/'No' (Req 3.5)
    # status / stage (current_stage indexed)
    ("current_stage", "text", "text"),
    ("current_status", "text", "text"),
    ("feasibility_status", "text", "text"),
    ("installation_status", "text", "text"),
    ("inspection_status", "text", "text"),
    # monetary (Req 3.4) — source text → numeric
    ("eligible_subsidy_amount", "numeric(15,2)", "numeric"),
    ("sanctioned_amount_inr", "numeric(15,2)", "numeric"),
    ("disbursement_tranche_1_amount_inr", "numeric(15,2)", "numeric"),
    ("disbursement_tranche_2_amount_inr", "numeric(15,2)", "numeric"),
    # quantity (Req 3.4)
    ("installed_capacity_in_kw", "numeric(12,3)", "numeric"),
    # flag (Req 3.5)
    ("subsidy_redeemed", "boolean", "boolean"),
    # dates (Req 3.3) — source ISO 'yyyy-MM-dd HH:mm:ss'; loader also reads 'dd-MMM-yyyy'
    ("registration_date", "date", "date"),
    ("sanctioned_date", "date", "date"),
    ("net_metering_date", "date", "date"),
    ("subsidy_redeemed_date", "date", "date"),
)

# --------------------------------------------------------------------------- #
# Derived views over the registry (ordered list + categorised sets).
# --------------------------------------------------------------------------- #
CURATED_COLUMNS: tuple[str, ...] = tuple(name for name, _, _ in COLUMN_REGISTRY)

NUMERIC_COLUMNS: frozenset[str] = frozenset(
    name for name, _, kind in COLUMN_REGISTRY if kind == "numeric"
)
DATE_COLUMNS: frozenset[str] = frozenset(
    name for name, _, kind in COLUMN_REGISTRY if kind == "date"
)
BOOLEAN_COLUMNS: frozenset[str] = frozenset(
    name for name, _, kind in COLUMN_REGISTRY if kind == "boolean"
)
TEXT_COLUMNS: frozenset[str] = frozenset(
    name for name, _, kind in COLUMN_REGISTRY if kind == "text"
)

# Curated name -> source CSV header name. Names match the CSV header exactly
# (including the source misspelling 'benefiaicry_unique_id_by_pfms'), so this is
# an identity map. The curated ``registration_date`` loads from the source
# 'registration_date' column (ISO 'yyyy-MM-dd HH:mm:ss'); the 'registration_date_rpt'
# 'dd-MMM-yyyy' variant is NOT curated.
SOURCE_COLUMN: dict[str, str] = {name: name for name in CURATED_COLUMNS}

# Postgres type per column (for DDL).
_PG_TYPE: dict[str, str] = {name: pg_type for name, pg_type, _ in COLUMN_REGISTRY}

# source_kind per column (for the loader's type coercion).
SOURCE_KIND: dict[str, str] = {name: kind for name, _, kind in COLUMN_REGISTRY}


# --------------------------------------------------------------------------- #
# Table-name validation + per-table whitelist.
# --------------------------------------------------------------------------- #
def _assert_table(table: str) -> None:
    """Raise ``ValueError`` if ``table`` is not one of the four curated tables."""
    if table not in TABLES:
        raise ValueError(
            f"unsupported table '{table}'; expected one of {', '.join(TABLES)}"
        )


def get_whitelist(table: str) -> dict[str, object]:
    """Return the per-table whitelist used by the Tool_Lambda validate/build.

    All four tables share the same curated schema, so the whitelist content is
    identical per table — but the table name is validated so an unknown table is
    rejected before any query is built (Req 5.1, 5.10, 11.7).

    Args:
        table: One of ``applications/subsidy/installation/inspection``.

    Returns:
        A dict with:
          - ``table``: the validated table name
          - ``columns``: frozenset of allowed (curated) column names
          - ``filter_ops``: frozenset of allowed filter operators
          - ``agg_fns``: frozenset of allowed aggregation functions
          - ``numeric_columns``: frozenset of numeric columns (non-count
            aggregations are restricted to these)
          - ``primary_key``: the primary identifier column

    Raises:
        ValueError: If ``table`` is not a curated table.
    """
    _assert_table(table)
    return {
        "table": table,
        "columns": frozenset(CURATED_COLUMNS),
        "filter_ops": ALLOWED_FILTER_OPS,
        "agg_fns": ALLOWED_AGG_FNS,
        "date_diff_fns": DATE_DIFF_FNS,
        "numeric_columns": NUMERIC_COLUMNS,
        "date_columns": DATE_COLUMNS,
        "primary_key": PRIMARY_KEY,
    }


# --------------------------------------------------------------------------- #
# DDL helpers (consumed by the DB bootstrap step, Task 8).
# --------------------------------------------------------------------------- #
def ddl_for_table(table: str) -> str:
    """Return the ``CREATE TABLE`` statement for ``table`` with curated columns.

    The PRIMARY KEY is on ``application_id`` (Req 3.6). All four tables get the
    identical curated column set + types.

    Raises:
        ValueError: If ``table`` is not a curated table.
    """
    _assert_table(table)
    lines = []
    for name in CURATED_COLUMNS:
        pg_type = _PG_TYPE[name]
        if name == PRIMARY_KEY:
            lines.append(f"    {name} {pg_type} PRIMARY KEY")
        else:
            lines.append(f"    {name} {pg_type}")
    cols = ",\n".join(lines)
    return f"CREATE TABLE IF NOT EXISTS {table} (\n{cols}\n);"


def index_ddl_for_table(table: str) -> list[str]:
    """Return ``CREATE INDEX`` statements for the indexed columns of ``table``.

    Indexed columns: state, district, discom, current_stage, name_of_bank
    (common filter columns; see design Indexes section).

    Raises:
        ValueError: If ``table`` is not a curated table.
    """
    _assert_table(table)
    return [
        f"CREATE INDEX IF NOT EXISTS idx_{table}_{col} ON {table} ({col});"
        for col in INDEXED_COLUMNS
    ]


# --------------------------------------------------------------------------- #
# Self-check: print DDL + whitelist for one table (no AWS, no DB).
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    _t = "subsidy"
    print(f"# Curated tables: {TABLES}")
    print(f"# Curated columns ({len(CURATED_COLUMNS)}): {list(CURATED_COLUMNS)}")
    print(f"# numeric={sorted(NUMERIC_COLUMNS)}")
    print(f"# date={sorted(DATE_COLUMNS)}")
    print(f"# boolean={sorted(BOOLEAN_COLUMNS)}")
    print(f"# primary_key={PRIMARY_KEY}\n")

    print(f"--- DDL for '{_t}' ---")
    print(ddl_for_table(_t))
    print()
    print(f"--- Indexes for '{_t}' ---")
    for stmt in index_ddl_for_table(_t):
        print(stmt)
    print()

    print(f"--- Whitelist for '{_t}' ---")
    _wl = get_whitelist(_t)
    print(f"table       : {_wl['table']}")
    print(f"filter_ops  : {sorted(_wl['filter_ops'])}")
    print(f"agg_fns     : {sorted(_wl['agg_fns'])}")
    print(f"#columns    : {len(_wl['columns'])}")
    print(f"#numeric    : {len(_wl['numeric_columns'])}")
    print(f"primary_key : {_wl['primary_key']}")
