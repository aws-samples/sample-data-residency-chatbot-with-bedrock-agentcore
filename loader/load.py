"""Loader pure-logic for the MNRE curated tables (Task 6, Requirements 3 & 4).

This module is IMPORTABLE and PURE: ``coerce``/``project``/``load_batch`` do no
I/O and never raise on bad data. They are consumed by:
  - the CSV prep step (``loader/prep_partial.py``) that produces COPY-ready CSVs,
  - the in-VPC loader Lambda (``infra/load_db/handler.py``) that bulk-loads them.

The Curated_Schema in ``common.schema`` is the single source of truth: this
module pulls ``CURATED_COLUMNS``, ``SOURCE_COLUMN`` (identity map) and
``SOURCE_KIND`` from it so the projection/coercion stay in lock-step with the
DDL (Req 3.1, 3.2).

Coercion rules (Req 3.3, 3.4, 3.5, 4.3, 4.4):
  - text    : strip; '' -> None
  - numeric : parse a finite decimal (commas/whitespace tolerated); junk -> None
  - date    : parse 'dd-MMM-yyyy' OR 'yyyy-MM-dd HH:mm:ss' -> date; junk -> None
  - boolean : 'Yes' -> True, 'No' -> False (case-insensitive); else -> None
Any value that cannot be cast maps to None (NULL) — coercion NEVER raises.
"""
from __future__ import annotations

import datetime as _dt
from decimal import Decimal, InvalidOperation

from common.schema import CURATED_COLUMNS, PRIMARY_KEY, SOURCE_COLUMN, SOURCE_KIND

# Accepted source date formats (Req 3.3). ISO columns are 'yyyy-MM-dd HH:mm:ss';
# the loader also accepts the 'dd-MMM-yyyy' report variant.
_DATE_FORMATS: tuple[str, ...] = ("%d-%b-%Y", "%Y-%m-%d %H:%M:%S")

_BOOL_TRUE = "yes"
_BOOL_FALSE = "no"

# source_kind values handled by ``coerce``.
_KINDS = frozenset({"text", "numeric", "date", "boolean"})


def _is_blank(value) -> bool:
    """True if the source value is genuinely empty (legitimately NULL)."""
    return value is None or (isinstance(value, str) and value.strip() == "")


def _coerce_text(value) -> str | None:
    """Strip whitespace; empty -> None."""
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _coerce_numeric(value) -> Decimal | None:
    """Parse a finite Decimal; tolerate commas/whitespace; junk/NaN/Inf -> None."""
    if _is_blank(value):
        return None
    s = str(value).strip().replace(",", "")
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError, TypeError):
        return None
    # Reject NaN/Infinity which Decimal parses but Postgres numeric rejects.
    if not d.is_finite():
        return None
    return d


def _coerce_date(value) -> _dt.date | None:
    """Parse 'dd-MMM-yyyy' or 'yyyy-MM-dd HH:mm:ss' to a date; junk -> None."""
    if _is_blank(value):
        return None
    s = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def _coerce_boolean(value) -> bool | None:
    """'Yes' -> True, 'No' -> False (case-insensitive); anything else -> None."""
    if _is_blank(value):
        return None
    s = str(value).strip().lower()
    if s == _BOOL_TRUE:
        return True
    if s == _BOOL_FALSE:
        return False
    return None


def coerce(value, source_kind: str):
    """Cast ``value`` to the Python type for ``source_kind`` (Property 6).

    Returns the cast value, or ``None`` for any blank/uncastable value or an
    unknown ``source_kind``. NEVER raises (Req 4.3, 4.4).
    """
    if source_kind == "text":
        return _coerce_text(value)
    if source_kind == "numeric":
        return _coerce_numeric(value)
    if source_kind == "date":
        return _coerce_date(value)
    if source_kind == "boolean":
        return _coerce_boolean(value)
    return None


def project(row: dict) -> dict:
    """Return EXACTLY the curated columns from a source row (Property 7).

    The result has one key per ``CURATED_COLUMNS`` entry (a subset of the source
    columns, since ``SOURCE_COLUMN`` is an identity map), pulling each value from
    the source column of the same name. Missing source columns yield ``None``.
    Raw (uncoerced) values are returned; coercion is applied by ``load_batch``.
    """
    return {col: row.get(SOURCE_COLUMN[col]) for col in CURATED_COLUMNS}


def _coerce_cell(raw, kind: str):
    """Coerce one cell, distinguishing a legitimate NULL from a cast failure.

    Returns ``(value, error)``:
      - text cells never error;
      - a blank typed cell is a legitimate NULL (no error);
      - a non-blank typed cell that fails to cast is an error (value None).
    """
    value = coerce(raw, kind)
    if kind == "text" or value is not None or _is_blank(raw):
        return value, None
    return None, f"{kind} cast failed for value {raw!r}"


def load_batch(rows, sink=None, id_column: str = PRIMARY_KEY) -> dict:
    """Project + coerce a batch of source rows resiliently (Property 8).

    For every row it projects the curated columns and coerces each cell. An
    uncastable (non-blank, non-text) cell is set to NULL and recorded as a
    reject ``(row_id, column, error)`` — the load is NEVER aborted. Every row is
    loaded, so ``loaded`` equals ``len(rows)`` (Req 4.5, 4.6).

    Args:
        rows: iterable of source-row dicts (CSV column name -> text value).
        sink: optional callable invoked once with the list of coerced rows
              (e.g. to write CSV / execute INSERTs). Pure when omitted.
        id_column: curated column used as the row identifier in rejects.

    Returns:
        ``{"loaded": int, "rows": list[dict], "rejects": list[tuple]}`` where
        ``loaded == len(rows)`` and ``rejects`` lists one entry per uncastable
        cell.
    """
    prepared: list[dict] = []
    rejects: list[tuple[str, str, str]] = []

    for i, row in enumerate(rows):
        projected = project(row)
        raw_id = projected.get(id_column)
        row_id = str(raw_id) if raw_id not in (None, "") else f"row[{i}]"

        coerced: dict = {}
        for col in CURATED_COLUMNS:
            value, error = _coerce_cell(projected[col], SOURCE_KIND[col])
            coerced[col] = value
            if error is not None:
                rejects.append((row_id, col, error))
        prepared.append(coerced)

    if sink is not None:
        sink(prepared)

    return {"loaded": len(prepared), "rows": prepared, "rejects": rejects}
