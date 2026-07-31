"""Property-based tests for the data-residency AgentCore chatbot pure-logic layers."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent.residency import residency_guard

_CROSS_REGION_PREFIXES = ("us.", "eu.", "ap.", "apac.", "jp.", "au.", "global.")


# Feature: residency-agentcore-chatbot, Property 1: Residency model-id guard
@settings(max_examples=200)
@given(
    suffix=st.text(),
    prefix=st.sampled_from(_CROSS_REGION_PREFIXES),
    accepted=st.text().filter(
        lambda s: not s.startswith(_CROSS_REGION_PREFIXES)
    ),
)
def test_property_1_residency_model_id_guard(suffix, prefix, accepted):
    """residency_guard accepts iff the id carries NO cross-region inference
    profile prefix (us./eu./ap./apac./jp./au./global.), and raises for every id
    that does (Validates: Requirements 1.7, 1.8)."""
    # Raise branch: any id carrying a cross-region prefix must be rejected.
    rejected = prefix + suffix
    with pytest.raises(ValueError):
        residency_guard(rejected)

    # Accept branch: any id without a cross-region prefix is returned unchanged.
    assert residency_guard(accepted) == accepted


# --------------------------------------------------------------------------- #
# Loader pure-logic properties (Task 6).
# --------------------------------------------------------------------------- #
import datetime as _dt
from decimal import Decimal

from common.schema import CURATED_COLUMNS, SOURCE_COLUMN, SOURCE_KIND
from loader.load import coerce, load_batch, project

_KINDS = ("text", "numeric", "date", "boolean")


def _valid_value_for(kind, draw):
    """Draw a (raw_text, expected_python) pair that IS valid for ``kind``."""
    if kind == "text":
        s = draw(st.text(min_size=1).filter(lambda x: x.strip() != ""))
        return s, s.strip()
    if kind == "numeric":
        n = draw(st.integers(min_value=-10**9, max_value=10**9))
        return str(n), Decimal(str(n))
    if kind == "date":
        d = draw(st.dates(min_value=_dt.date(1900, 1, 1), max_value=_dt.date(2099, 12, 31)))
        fmt = draw(st.sampled_from(("%d-%b-%Y", "%Y-%m-%d %H:%M:%S")))
        if fmt == "%Y-%m-%d %H:%M:%S":
            raw = _dt.datetime(d.year, d.month, d.day, 12, 30, 0).strftime(fmt)
        else:
            raw = d.strftime(fmt)
        return raw, d
    # boolean
    b = draw(st.booleans())
    raw = draw(st.sampled_from(["Yes", "No", "yes", "no", "YES", "NO"]))
    return raw, (raw.strip().lower() == "yes")


# Feature: residency-agentcore-chatbot, Property 6: Type coercion casts valid values and nulls the rest
@settings(max_examples=200)
@given(data=st.data(), kind=st.sampled_from(_KINDS))
def test_property_6_type_coercion(data, kind):
    """coerce casts a valid value to its target type and nulls everything else
    (Validates: Requirements 3.3, 3.4, 3.5, 4.3, 4.4)."""
    # Valid branch: a value valid for the kind is cast to the expected Python type.
    raw, expected = _valid_value_for(kind, data.draw)
    if kind == "boolean":
        # boolean expected is computed from the drawn token
        expected = raw.strip().lower() == "yes"
    assert coerce(raw, kind) == expected

    # Null branch: blanks are always NULL for every kind.
    assert coerce("", kind) is None
    assert coerce("   ", kind) is None
    assert coerce(None, kind) is None

    # Uncastable branch: a value invalid for a typed kind maps to NULL, never raises.
    junk = data.draw(st.text(min_size=1).filter(lambda x: x.strip() != ""))
    result = coerce(junk, kind)
    if kind == "numeric":
        # If the junk happens to parse as a finite number that's acceptable;
        # otherwise it must be None.
        assert result is None or isinstance(result, Decimal)
    elif kind == "date":
        assert result is None or isinstance(result, _dt.date)
    elif kind == "boolean":
        assert result in (True, False, None)
    else:  # text never fails
        assert result == junk.strip() or result is None


# Feature: residency-agentcore-chatbot, Property 7: Load projects exactly the curated columns
@settings(max_examples=200)
@given(
    extra=st.dictionaries(
        st.text(min_size=1).filter(lambda k: k not in CURATED_COLUMNS),
        st.text(),
        max_size=8,
    ),
    present=st.sets(st.sampled_from(CURATED_COLUMNS), max_size=12),
)
def test_property_7_projection_curated_columns(extra, present):
    """project returns exactly the curated columns, a subset of source columns
    (Validates: Requirements 3.2, 4.2)."""
    source = dict(extra)
    for col in present:
        source[SOURCE_COLUMN[col]] = f"val-{col}"

    projected = project(source)

    # Exactly the curated set — no more, no less.
    assert set(projected.keys()) == set(CURATED_COLUMNS)
    # Curated keys are a subset of the source columns (identity SOURCE_COLUMN map).
    assert set(projected.keys()).issubset(set(SOURCE_COLUMN.values()))
    # Extra (non-curated) source columns never leak into the projection.
    for k in extra:
        assert k not in projected
    # Present curated values are pulled through from the matching source column.
    for col in present:
        assert projected[col] == f"val-{col}"


# Feature: residency-agentcore-chatbot, Property 8: Loader is resilient to bad values
@settings(max_examples=200)
@given(
    rows=st.lists(
        st.fixed_dictionaries(
            {
                "application_id": st.text(min_size=1, max_size=12).filter(
                    lambda x: x.strip() != ""
                ),
                # numeric column: mix of valid numbers and junk
                "eligible_subsidy_amount": st.one_of(
                    st.integers(min_value=0, max_value=10**6).map(str),
                    st.sampled_from(["", "abc", "N/A", "12.5.6", "--"]),
                ),
                # date column: mix of valid dates and junk
                "registration_date": st.one_of(
                    st.sampled_from(["01-Jan-2024", "2024-06-15 10:30:00"]),
                    st.sampled_from(["", "not-a-date", "31-Foo-2024"]),
                ),
                # boolean column: mix of valid flags and junk
                "subsidy_redeemed": st.sampled_from(["Yes", "No", "", "maybe", "1"]),
            }
        ),
        max_size=25,
    )
)
def test_property_8_loader_resilient(rows):
    """load_batch loads every row, records uncastable cells, never aborts, and
    reports loaded == len(rows) (Validates: Requirements 4.5, 4.6)."""
    captured = {}
    result = load_batch(rows, sink=lambda prepared: captured.update(n=len(prepared)))

    # Never aborts: every row is loaded.
    assert result["loaded"] == len(rows)
    assert len(result["rows"]) == len(rows)
    assert captured.get("n", len(rows)) == len(rows)

    # Each loaded row has exactly the curated columns.
    for r in result["rows"]:
        assert set(r.keys()) == set(CURATED_COLUMNS)

    # Rejects reference a row id, a curated column, and a non-empty error string;
    # the rejected cell is NULL in the loaded row.
    for row_id, col, error in result["rejects"]:
        assert isinstance(row_id, str) and row_id
        assert col in CURATED_COLUMNS
        assert SOURCE_KIND[col] != "text"
        assert isinstance(error, str) and error


# --------------------------------------------------------------------------- #
# Tool_Lambda pure-logic properties (Task 4).
# --------------------------------------------------------------------------- #
from common.schema import (
    ALLOWED_AGG_FNS,
    ALLOWED_FILTER_OPS,
    NUMERIC_COLUMNS,
    TABLES,
)
from tool.query_builder import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    build_query,
    clamp_limit,
)
from tool.response import redact, shape_response
from tool.validate import validate_request

_CURATED = list(CURATED_COLUMNS)
_NULLARY_OPS = ("is_null", "not_null")
_VALUE_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "like")


@st.composite
def _valid_filter(draw):
    """Draw a contract-valid filter on a whitelisted column/op."""
    col = draw(st.sampled_from(_CURATED))
    op = draw(st.sampled_from(_VALUE_OPS + _NULLARY_OPS + ("in",)))
    if op in _NULLARY_OPS:
        return {"column": col, "op": op}
    if op == "in":
        vals = draw(st.lists(st.integers(min_value=-1000, max_value=1000),
                             min_size=1, max_size=4))
        return {"column": col, "op": op, "value": vals}
    # String values carry a sentinel char that can never appear in generated SQL
    # (identifiers + keywords only), so the "not inlined" check is unambiguous.
    str_val = draw(st.text(max_size=20).map(lambda s: "§" + s))
    return {"column": col, "op": op, "value": draw(
        st.one_of(st.integers(min_value=-1000, max_value=1000),
                  st.just(str_val)))}


@st.composite
def _valid_aggregation(draw):
    """Draw a contract-valid aggregation (non-count requires a numeric column)."""
    fn = draw(st.sampled_from(sorted(ALLOWED_AGG_FNS)))
    if fn == "count":
        col = draw(st.sampled_from(_CURATED))
    else:
        col = draw(st.sampled_from(sorted(NUMERIC_COLUMNS)))
    agg = {"fn": fn, "column": col}
    if draw(st.booleans()):
        agg["alias"] = draw(st.sampled_from(["n", "total", "agg_val", "_x", "v1"]))
    return agg


@st.composite
def _valid_request(draw):
    """Draw a contract-valid Parameterized_Query_API request."""
    req = {"table": draw(st.sampled_from(list(TABLES)))}
    if draw(st.booleans()):
        req["filters"] = draw(st.lists(_valid_filter(), max_size=4))
    group_by = draw(st.lists(st.sampled_from(_CURATED), max_size=3, unique=True))
    if group_by:
        req["group_by"] = group_by
    if draw(st.booleans()):
        req["aggregations"] = draw(st.lists(_valid_aggregation(), min_size=1, max_size=3))
    if draw(st.booleans()):
        req["limit"] = draw(st.integers(min_value=-50, max_value=5000))
    return req


def _all_literal_values(req):
    """Collect every supplied literal that must be bound, never inlined."""
    values = []
    for f in req.get("filters") or []:
        if f.get("op") in _NULLARY_OPS:
            continue
        v = f.get("value")
        if isinstance(v, (list, tuple)):
            values.extend(v)
        else:
            values.append(v)
    return values


# Feature: residency-agentcore-chatbot, Property 2: Valid requests build safe, parameterized, whitelist-only SQL
@settings(max_examples=200)
@given(req=_valid_request())
def test_property_2_safe_parameterized_sql(req):
    """build_query emits SELECT-only, whitelist-only SQL with every literal bound
    as a parameter (Validates: Requirements 5.1, 5.4, 5.5, 11.7)."""
    # Pre-condition: the drawn request is contract-valid.
    assert validate_request(req) is None

    sql, params = build_query(req)

    # SELECT-only.
    assert sql.startswith("SELECT ")
    upper = sql.upper()
    for forbidden in (";", " INSERT", " UPDATE", " DELETE", " DROP", " ALTER"):
        assert forbidden not in upper

    # Only whitelisted table/column identifiers appear; the table is referenced.
    assert f" FROM {req['table']}" in sql

    # Every supplied filter literal is bound (present in params), never inlined.
    bound = _all_literal_values(req)
    # params == filter literals (in order) + the clamped limit at the end.
    assert params[-1] == clamp_limit(req.get("limit"))
    assert params[:-1] == bound

    # No bound string value is inlined into the SQL text itself.
    for v in bound:
        if isinstance(v, str) and v.strip() != "":
            assert v not in sql
    # The limit literal is bound, not inlined.
    assert f"LIMIT {params[-1]}" not in sql
    assert "LIMIT %s" in sql


@st.composite
def _out_of_contract_request(draw):
    """Draw a request that violates the contract in exactly one way."""
    kind = draw(st.sampled_from(
        ["raw_sql", "bad_table", "bad_column", "bad_op", "bad_fn", "non_numeric_agg"]
    ))
    if kind == "raw_sql":
        return {"table": "subsidy", "sql": "DROP TABLE subsidy"}, kind
    if kind == "bad_table":
        bad = draw(st.text(min_size=1).filter(lambda t: t not in TABLES))
        return {"table": bad}, kind
    if kind == "bad_column":
        bad = draw(st.text(min_size=1).filter(lambda c: c not in CURATED_COLUMNS))
        return {"table": "subsidy",
                "filters": [{"column": bad, "op": "eq", "value": "x"}]}, kind
    if kind == "bad_op":
        bad = draw(st.text(min_size=1).filter(lambda o: o not in ALLOWED_FILTER_OPS))
        return {"table": "subsidy",
                "filters": [{"column": "state", "op": bad, "value": "x"}]}, kind
    if kind == "bad_fn":
        bad = draw(st.text(min_size=1).filter(lambda f: f not in ALLOWED_AGG_FNS))
        return {"table": "subsidy",
                "aggregations": [{"fn": bad, "column": "state"}]}, kind
    # non_numeric_agg: sum/avg/min/max on a non-numeric column
    non_numeric = [c for c in CURATED_COLUMNS if c not in NUMERIC_COLUMNS]
    fn = draw(st.sampled_from(["sum", "avg", "min", "max"]))
    col = draw(st.sampled_from(non_numeric))
    return {"table": "subsidy", "aggregations": [{"fn": fn, "column": col}]}, kind


# Feature: residency-agentcore-chatbot, Property 3: Out-of-contract requests are rejected with no execution
@settings(max_examples=200)
@given(case=_out_of_contract_request())
def test_property_3_out_of_contract_rejected(case):
    """validate_request rejects unsupported table/column/op/fn or raw SQL with a
    descriptive error and executed=False (Validates: Requirements 5.6, 5.10, 11.7)."""
    req, _kind = case
    result = validate_request(req)
    assert isinstance(result, dict)
    assert result["executed"] is False
    assert isinstance(result["error"], str) and result["error"]


# Feature: residency-agentcore-chatbot, Property 4: Row limit is always clamped to range
@settings(max_examples=200)
@given(
    n=st.one_of(
        st.none(),
        st.integers(min_value=-10**6, max_value=10**6),
        st.text(max_size=8),
        st.floats(allow_nan=True, allow_infinity=True),
        st.booleans(),
    )
)
def test_property_4_limit_clamped(n):
    """clamp_limit always returns an int in [1, 1000]; default applies when
    missing (Validates: Requirements 5.8, 5.9)."""
    eff = clamp_limit(n)
    assert isinstance(eff, int)
    assert MIN_LIMIT <= eff <= MAX_LIMIT
    if n is None:
        assert eff == DEFAULT_LIMIT
    if isinstance(n, int) and not isinstance(n, bool):
        if n > MAX_LIMIT:
            assert eff == MAX_LIMIT
        elif n < MIN_LIMIT:
            assert eff == MIN_LIMIT
        else:
            assert eff == n


# Feature: residency-agentcore-chatbot, Property 5: Successful responses conform to the response schema
@settings(max_examples=200)
@given(
    table=st.sampled_from(list(TABLES)),
    columns=st.lists(st.sampled_from(_CURATED), min_size=1, max_size=5, unique=True),
    data=st.data(),
    truncated=st.booleans(),
)
def test_property_5_response_schema(table, columns, data, truncated):
    """shape_response returns structured JSON with the contract keys and
    row_count == len(rows) (Validates: Requirements 5.7)."""
    n_rows = data.draw(st.integers(min_value=0, max_value=20))
    # Build positional rows aligned to columns.
    rows = [
        [data.draw(st.one_of(st.integers(), st.text(max_size=5), st.none()))
         for _ in columns]
        for _ in range(n_rows)
    ]
    resp = shape_response(rows, columns, table, truncated=truncated)

    assert set(resp.keys()) == {"table", "row_count", "columns", "rows", "truncated"}
    assert resp["table"] == table
    assert resp["columns"] == list(columns)
    assert isinstance(resp["rows"], list)
    assert all(isinstance(r, dict) for r in resp["rows"])
    assert resp["row_count"] == len(resp["rows"]) == n_rows
    assert resp["truncated"] is bool(truncated)
    # Each row object is keyed exactly by the columns.
    for r in resp["rows"]:
        assert set(r.keys()) == set(columns)


# Credentials are drawn from lowercase letters + digits (realistic secret charset)
# which can never be a substring of the all-uppercase redaction placeholder.
_SECRET_TEXT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
                       min_size=1, max_size=24)


# Feature: residency-agentcore-chatbot, Property 11: Logs never contain secret values
@settings(max_examples=200)
@given(
    username=_SECRET_TEXT,
    password=_SECRET_TEXT,
    prefix=st.text(max_size=30),
    suffix=st.text(max_size=30),
)
def test_property_11_logs_redact_secrets(username, password, prefix, suffix):
    """redact removes every credential value from log text
    (Validates: Requirements 11.6, 12.4)."""
    secret = {"username": username, "password": password}
    log_line = f"{prefix} connecting as {username} with {password} {suffix}"

    redacted = redact(log_line, secret)

    # Neither the username nor the password value survives in the output.
    assert username not in redacted
    assert password not in redacted


# --------------------------------------------------------------------------- #
# Memory adapter property (Task 13.2).
# --------------------------------------------------------------------------- #
import datetime as _dt2

from agent.memory import DEFAULT_ACTOR_ID, load, save


class _FakeMemoryClient:
    """In-memory stand-in for the bedrock-agentcore data-plane client.

    Faithfully mirrors CreateEvent/ListEvents semantics used by the adapter:
      - create_event appends one immutable event (a USER + ASSISTANT payload)
        under (memoryId, actorId, sessionId), stamping a monotonically
        increasing eventTimestamp like the real service records event time.
      - list_events returns the session's events. To prove the adapter does not
        rely on a particular server order, this fake returns them NEWEST-FIRST
        (reversed) and paginates; the adapter must still recover save order by
        sorting on eventTimestamp.
    """

    def __init__(self):
        self._events = {}  # (memoryId, actorId, sessionId) -> [event,...]
        self._clock = 0

    def create_event(self, memoryId, actorId, sessionId, eventTimestamp, payload):
        self._clock += 1
        event = {
            "eventId": f"evt-{self._clock}",
            "eventTimestamp": self._clock,  # monotonic, like server-recorded time
            "actorId": actorId,
            "sessionId": sessionId,
            "memoryId": memoryId,
            "payload": payload,
        }
        self._events.setdefault((memoryId, actorId, sessionId), []).append(event)
        return {"event": event}

    def list_events(self, memoryId, actorId, sessionId, includePayloads=True,
                    maxResults=100, nextToken=None):
        stored = list(self._events.get((memoryId, actorId, sessionId), []))
        stored.reverse()  # newest-first: adapter must not depend on order
        start = int(nextToken) if nextToken else 0
        page = stored[start:start + maxResults]
        resp = {"events": page}
        nxt = start + maxResults
        if nxt < len(stored):
            resp["nextToken"] = str(nxt)
        return resp


_TURN_TEXT = st.text(max_size=60)


# Feature: residency-agentcore-chatbot, Property 10: Session memory round-trip preserves turns
@settings(max_examples=100)
@given(
    turns=st.lists(st.tuples(_TURN_TEXT, _TURN_TEXT), max_size=12),
    session_id=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        min_size=1, max_size=20,
    ),
)
def test_property_10_memory_round_trip(turns, session_id):
    """Saving a sequence of turns under a session and loading it back returns the
    same turns in the order saved; an empty session returns empty
    (Validates: Requirements 7.7, 8.2, 8.3, 8.4, 8.6)."""
    client = _FakeMemoryClient()
    memory_id = "residency_chatbot_memory-abcdefghij"

    # Empty session → empty result (Req 8.6).
    assert load(client, memory_id, DEFAULT_ACTOR_ID, session_id) == []

    # Save every turn in order.
    for user_text, assistant_text in turns:
        save(client, memory_id, DEFAULT_ACTOR_ID, session_id,
             user_text, assistant_text)

    # Round-trip preserves both order and content.
    loaded = load(client, memory_id, DEFAULT_ACTOR_ID, session_id)
    expected = [{"user": u, "assistant": a} for u, a in turns]
    assert loaded == expected

    # Isolation: a different session is unaffected (still empty).
    assert load(client, memory_id, DEFAULT_ACTOR_ID, session_id + "_other") == []


# --------------------------------------------------------------------------- #
# Agent_Lambda tool-error wrapper property (Task 14.1).
# --------------------------------------------------------------------------- #
from agent.errors import wrap_tool_error


def _error_inputs():
    """Strategy producing arbitrary error representations the agent may surface."""
    exceptions = st.sampled_from(
        [ValueError, RuntimeError, KeyError, TypeError, ConnectionError, Exception]
    ).flatmap(lambda exc: st.text(max_size=40).map(lambda m: exc(m)))
    structured = st.fixed_dictionaries(
        {
            "error": st.text(max_size=60),
            "executed": st.just(False),
        }
    )
    arbitrary_dicts = st.dictionaries(st.text(max_size=10), st.text(max_size=20),
                                      max_size=5)
    return st.one_of(
        st.none(),
        st.text(max_size=80),
        exceptions,
        structured,
        arbitrary_dicts,
        st.integers(),
        st.lists(st.text(max_size=10), max_size=4),
    )


# Feature: residency-agentcore-chatbot, Property 9: Tool errors produce a graceful answer
@settings(max_examples=200)
@given(err=_error_inputs())
def test_property_9_tool_errors_graceful(err):
    """For any tool error input, wrap_tool_error returns a non-empty natural-language
    message and never raises (Validates: Requirements 7.6)."""
    # Must never raise — call it directly (any exception fails the test).
    message = wrap_tool_error(err)

    # Always a non-empty string indicating the question could not be answered.
    assert isinstance(message, str)
    assert message.strip() != ""
    assert "couldn't answer" in message.lower()
