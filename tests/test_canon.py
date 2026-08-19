"""Canonical-form tests (period-model ss3.2, DL-119).

Normative spec: `docs/period-model.md` ss3.2 and its obligations PR-08..PR-14
in ss13.2. `src/dsl41/canon.py` is the one implementation under test.

House style follows test_run_history.py: plain fixtures, no filesystem where
the property is pure. The golden vector (PR-08) pins EXACT bytes and an EXACT
digest as literals -- equality and sensitivity tests alone would pass a
canonicalizer that is consistently wrong, so the bytes themselves are the
assertion. Every control character in this file is written as an escape
sequence, never as a literal, so the source stays readable.

The ingress tests (PR-10a) drive the real doors -- a control socket, the JIL
lowerer, the spool reader -- rather than the helper they share, because the
obligation is about the doors.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import pathlib
import shutil
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from dsl41.canon import (
    ARTIFACT_FORMAT_VERSION,
    CanonError,
    canonical_bytes,
    check_artifact_version,
    decode,
    digest,
    is_scalar_string,
    with_digest,
)
from dsl41.ast_jil import JilParseError, parse
from dsl41.ir import LoweringError, lower_catalog, lower_source
from dsl41.oracle import TIMER_CHECKS, Oracle
from dsl41.oracle_state import Event, JobRuntime
from dsl41.runner_adapters import FakeAdapter, load_json
from dsl41.runner_admission import PROTOCOL_VERSION, addressed_key
from dsl41.runner_clock import RealClock
from dsl41.runner_control import REFUSED, ControlServer, command, outcome_of, read_for, revision_in
from dsl41.runner_journal import read_journal
from dsl41.runner_startup import start_run

T0 = datetime(2026, 7, 1, 9, 50)


# ----------------------------------------------------------- 1. PR-08 golden vector


def _golden_document() -> dict[str, Any]:
    """One document exercising every clause of ss3.2: control characters, the
    two characters that must NOT be escaped (`/` and DEL's neighbours), non-
    ASCII from three planes, nulls, empty and non-empty opaque payloads, an
    array whose order carries meaning, a nested `digest` key, and a naive and
    an aware datetime that normalize to the same six-digit spelling."""
    return {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        # the aware one is 04:00+02:00 -- the same instant, and the same bytes
        "closed_at": datetime(2026, 8, 19, 2, 0),
        "closed_at_aware": datetime(2026, 8, 19, 4, 0, tzinfo=timezone(timedelta(hours=2))),
        "counts": {"negative": -7, "zero": 0, "positive": 5310},
        "escapes": '\\"\b\f\n\r\t\x00\x1f\x7f\x85\x9f/',  # incl. U+0085/009F (DL-128)
        "estate_id": "nightbank",
        # keys sorted by CODE POINT: an astral key sorts after U+FFFF, which
        # UTF-16 order would get backwards
        "key_order": {"Z": 0, "a": 1, "\u00e9": 2, "\uffff": 3, "\U0001d518": 4},
        "labels": ["\u00e9", "\u65e5\u672c", "\U0001d518"],
        "nested": {"digest": "data, not the top-level one", "note": None},
        "order": [3, 1, 2],
        "payload_empty": {},
        "payload_null": {"x": None},
        "seen": [True, False, None],
        "unset": None,
    }


#: PR-08: pinned, not computed. The test must fail if serialization moves.
GOLDEN_BYTES = (
    b'{"artifact_format_version":1,"closed_at":"2026-08-19T02:00:00.000000","closed_at_aware":'
    b'"2026-08-19T02:00:00.000000","counts":{"negative":-7,"positive":5310,"zero":0},"escapes":'
    b'"\\\\\\"\\b\\f\\n\\r\\t\\u0000\\u001f\\u007f\\u0085\\u009f/","estate_id":"nightbank","key_order":'
    b'{"Z":0,"a":1,"\xc3\xa9":2,"\xef\xbf\xbf":3,"\xf0\x9d\x94\x98":4},"labels":'
    b'["\xc3\xa9","\xe6\x97\xa5\xe6\x9c\xac","\xf0\x9d\x94\x98"],"nested":'
    b'{"digest":"data, not the top-level one","note":null},"order":[3,1,2],"payload_empty":{},'
    b'"payload_null":{"x":null},"seen":[true,false,null],"unset":null}'
)

GOLDEN_DIGEST = "sha256:8cfb10e3cbeadb98309f23010bc9a08c6199a2614228eb38601de9ccb5fce237"


def test_pr08_golden_vector() -> None:
    """PR-08: the fixed bytes and the fixed digest. Both are literals above;
    a change to the encoder, the escaping table, the key order or the datetime
    spelling reds this test, which is the whole point of shipping a vector."""
    document = _golden_document()
    assert canonical_bytes(document) == GOLDEN_BYTES
    assert digest(document) == GOLDEN_DIGEST
    # the bytes are valid JSON and decode cleanly under ss3.2's own reader
    assert isinstance(decode(GOLDEN_BYTES), dict)
    stamped = with_digest(document)
    assert stamped["digest"] == GOLDEN_DIGEST
    assert digest(stamped) == GOLDEN_DIGEST  # the stamp does not move the value


def test_pr08_key_order_is_unicode_code_point_order() -> None:
    """ss3.2 says code point, and Python's str comparison IS code-point
    comparison -- confirmed here rather than assumed, because UTF-16 order
    (which several other languages sort by) disagrees above U+FFFF."""
    keys = ["\U0001d518", "\uffff", "z", "\u00e9", "Z", "a", " "]
    assert sorted(keys) == sorted(keys, key=ord)
    encoded = canonical_bytes({key: 0 for key in keys}).decode("utf-8")
    positions = [encoded.index(json.dumps(key, ensure_ascii=False)) for key in sorted(keys)]
    assert positions == sorted(positions)


# ----------------------------------------------------------- 2. escaping and datetimes


@pytest.mark.parametrize(
    ("char", "expected"),
    [
        ('"', b'"\\""'),
        ("\\", b'"\\\\"'),
        ("\b", b'"\\b"'),
        ("\f", b'"\\f"'),
        ("\n", b'"\\n"'),
        ("\r", b'"\\r"'),
        ("\t", b'"\\t"'),
        ("\x00", b'"\\u0000"'),
        ("\x1f", b'"\\u001f"'),
        ("\x7f", b'"\\u007f"'),
        ("\x80", b'"\\u0080"'),  # U+0080..009F are Cc too (DL-128)
        ("\x85", b'"\\u0085"'),  # NEL
        ("\x9f", b'"\\u009f"'),  # last of that range
        ("\xa0", b'"\xc2\xa0"'),  # NBSP: not Cc, not escaped
        ("/", b'"/"'),
        ("\u00e9", b'"\xc3\xa9"'),
    ],
)
def test_escaping_table(char: str, expected: bytes) -> None:
    """ss3.2's escaping, character by character. `/` is never escaped, non-
    ASCII goes out as UTF-8, and the `\\u00xx` hex is LOWER-case.

    `json.dumps(ensure_ascii=False)` leaves U+007F and U+0080..009F unescaped,
    so this table is also the reason canon.py writes its own escaper instead
    of borrowing one. Cc is all three ranges (DL-128); U+00A0 is the first
    non-Cc neighbour and goes out raw.
    """
    assert canonical_bytes(char) == expected
    assert decode(canonical_bytes(char)) == char


def test_datetimes_are_naive_utc_with_six_fractional_digits() -> None:
    """ss3.2: ISO-8601 naive UTC, exactly six fractional digits -- including
    when the microsecond is zero, where `isoformat()` alone prints none."""
    same_instant = [
        datetime(2026, 8, 19, 2, 0),  # naive, already UTC
        datetime(2026, 8, 19, 2, 0, tzinfo=UTC),
        datetime(2026, 8, 19, 4, 0, tzinfo=timezone(timedelta(hours=2))),
    ]
    for moment in same_instant:
        assert canonical_bytes(moment) == b'"2026-08-19T02:00:00.000000"'
    assert canonical_bytes(datetime(2026, 8, 19, 2, 0, 0, 7)) == b'"2026-08-19T02:00:00.000007"'
    assert canonical_bytes(datetime(2026, 8, 19, 2, 0, 0, 123456)) == (
        b'"2026-08-19T02:00:00.123456"'
    )


# ----------------------------------------------------------- 3. PR-10 nulls and payloads


def test_pr10_null_vs_absent_and_opaque_payloads() -> None:
    """PR-10. Absent-vs-null is the MODEL's job, not this module's: ss3.2
    default-fills at typed schema boundaries only, so a typed row reaches
    canon with its optional already spelled `null` and the two documents are
    literally the same value here. Inside an OPAQUE payload nothing is
    default-filled, and `{}` vs `{"x": null}` must digest differently -- a
    canonicalizer that "drops empty or default values recursively" is
    non-conformant."""
    # the model's job, shown on a REAL row: a JobRuntime built with its
    # optional absent and one built with it explicitly None dump to the same
    # document, so canon sees one value. A writer using `exclude_none` would
    # produce different bytes for the same state -- the non-conformance ss3.2
    # names -- and the third assertion is what would catch it.
    # (mode="json" so the frozenset `ran_members` arrives as a list; canon
    # refuses a set on purpose -- ordering one is the projection's job)
    absent = JobRuntime().model_dump(mode="json")
    explicit = JobRuntime(exit_code=None, status_at=None).model_dump(mode="json")
    assert canonical_bytes(absent) == canonical_bytes(explicit)
    assert "exit_code" in absent and absent["exit_code"] is None
    narrowed = JobRuntime().model_dump(mode="json", exclude_none=True)
    assert canonical_bytes(narrowed) != canonical_bytes(absent)
    assert digest({"deadman_us": None}) != digest({})

    # canon's job: opaque payloads keep what they were given
    assert digest({"payload": {}}) != digest({"payload": {"x": None}})
    assert digest({"payload": {}}) != digest({"payload": {"x": {}}})

    # order in an array is meaning; order of keys is not
    assert digest({"timers": [1, 2]}) != digest({"timers": [2, 1]})
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert canonical_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'


# ----------------------------------------------------------- 4. PR-10a ingress


SURROGATE = "\ud800"


def test_pr10a_surrogate_refused_in_the_envelope() -> None:
    """request_id and claimed_actor ride into the WAL on every externally
    requested attempt; a surrogate in either is refused at parse_envelope,
    before anything is admitted."""
    from dsl41.runner_admission import EnvelopeError, parse_envelope

    base = {"v": PROTOCOL_VERSION, "baseline_id": "b", "epoch": 0, "expect": {"job:j": 0}}
    for field in ("request_id", "claimed_actor"):
        request = {**base, "request_id": "r1", field: "x" + SURROGATE}
        with pytest.raises(EnvelopeError, match="unpaired surrogate"):
            parse_envelope(request, baseline_id="b", addressed="job:j")


def test_pr10a_surrogate_refused_in_a_source_path() -> None:
    """SourceSpan.file rides in the catalog hash; a surrogate-escaped path
    is refused at lowering, naming the path."""
    from dsl41.ir import LoweringError, lower_catalog

    jil = parse("insert_job: j\njob_type: c\ncommand: true\nmachine: m\n", file="ok.jil")
    bad = jil.model_copy(update={"file": "est" + SURROGATE + ".jil"})
    with pytest.raises(LoweringError, match="unpaired surrogate"):
        lower_catalog([bad], permit_unknown=True)


def test_pr10a_surrogate_refused_in_a_span_file() -> None:
    """A hand-built JilFile can keep `file` scalar and carry a surrogate in a
    statement's or attribute's `span.file`; lowering checks every carried AST
    string, not only the top-level path."""
    from dsl41.ir import LoweringError, lower_catalog

    jil = parse("insert_job: j\njob_type: c\ncommand: true\nmachine: m\n", file="ok.jil")
    stmt = jil.statements[0]
    bad_span = stmt.span.model_copy(update={"file": "x" + SURROGATE})
    bad_stmt = stmt.model_copy(update={"span": bad_span})
    bad = jil.model_copy(update={"statements": [bad_stmt]})
    with pytest.raises(LoweringError, match="unpaired surrogate"):
        lower_catalog([bad], permit_unknown=True)


def test_pr10a_surrogate_refused_in_date_lines_and_any_ast_field() -> None:
    """The lowering check walks the statement's whole dump: a surrogate in a
    calendar date line -- a field the first enumerated check missed -- is
    refused like any other."""
    from dsl41.ir import LoweringError, lower_catalog

    jil = parse("calendar: hols\n08/19/2026 00:00\n", file="cal.jil")
    stmt = jil.statements[0]
    assert stmt.date_lines, "fixture must carry a date line"
    bad_stmt = stmt.model_copy(update={"date_lines": ["08/19/2026 00:00" + SURROGATE]})
    bad = jil.model_copy(update={"statements": [bad_stmt]})
    with pytest.raises(LoweringError, match="unpaired surrogate"):
        lower_catalog([bad], permit_unknown=True)


def test_pr10a_surrogate_refused_by_canon() -> None:
    """PR-10a, the canonicalizer's own half: a lone surrogate is a legal
    Python str and never a canonical one, at any depth or in a key."""
    assert is_scalar_string("ok") and not is_scalar_string(SURROGATE)
    for value in (SURROGATE, [SURROGATE], {"a": [{"b": SURROGATE}]}, {SURROGATE: 1}):
        with pytest.raises(CanonError):
            canonical_bytes(value)
    with pytest.raises(CanonError):
        decode('{"v":"\\ud800"}')


@pytest.fixture
def short_root():
    """A short-path base directory for the AF_UNIX control socket. pytest's
    tmp_path lives deep under the platform temp dir and overruns sun_path once
    `run/control.sock` is appended -- test_runner_control.py keeps the same
    fixture for the same reason."""
    directory = tempfile.mkdtemp(prefix="dsl41canon-", dir="/tmp")
    try:
        yield Path(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_pr10a_surrogate_refused_at_the_control_socket(short_root: Path) -> None:
    """PR-10a, ingress 1: SET_GLOBAL. The value crosses the wire as the
    `\\ud800` escape a JSON encoder is happy to write, decodes into a Python
    str, and is refused at the door -- `refused`, so nothing was admitted and
    nothing is in the log (control-protocol ss3)."""
    run_root = short_root / "run"
    text = "insert_job: sg_job\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine = start_run(
            lower_source(text),
            run_root,
            clock=RealClock(),
            adapters={"CMD": FakeAdapter(), "FW": FakeAdapter()},
            hold_open=True,
        )
        server = ControlServer(engine, run_root / "control.sock")
        await server.start()
        loop_task = asyncio.ensure_future(engine.run_until_quiescent(datetime.max))
        try:
            payload = {"name": "FLAG", "value": SURROGATE}
            key = addressed_key("SET_GLOBAL", payload)
            read = await _control_call(server.path, read_for(key))
            response = await _control_call(
                server.path,
                command(
                    "SET_GLOBAL",
                    payload,
                    key=key,
                    revision=revision_in(read, key),
                    baseline_id=str(read["baseline_id"]),
                    epoch=int(read["epoch"]),
                ),
            )
            assert outcome_of(response) == REFUSED
            assert "surrogate" in response["error"]
        finally:
            loop_task.cancel()
            await asyncio.gather(loop_task, return_exceptions=True)
            await server.close()
            await engine.shutdown()
            assert engine.journal is not None
            engine.journal.close()

    asyncio.run(scenario())
    kinds = [r.get("kind") for r in read_journal(run_root / "journal.jsonl")]
    assert "SET_GLOBAL" not in kinds  # refused means the log says nothing


async def _control_call(sock_path: Path, request: dict[str, Any]) -> dict[str, Any]:
    """One control round trip, versioned the way every shipped client is."""
    reader, writer = await asyncio.open_unix_connection(str(sock_path))
    try:
        writer.write(json.dumps({**request, "v": PROTOCOL_VERSION}).encode("utf-8") + b"\n")
        await writer.drain()
        return dict(json.loads(await reader.readline()))
    finally:
        writer.close()
        await asyncio.gather(writer.wait_closed(), return_exceptions=True)


def test_pr10a_surrogate_refused_at_jil_ingress() -> None:
    """PR-10a, ingress 2: a JIL attribute value, refused twice over.

    The scanner refuses first, naming the line -- it has to, because a span is
    a UTF-8 byte offset and a surrogate has no UTF-8 spelling. The lowerer
    refuses too, naming the attribute, for a `JilFile` that did not come from
    `parse` (`lower_catalog` takes any)."""
    text = f"insert_job: sg_j\njob_type: c\ncommand: x{SURROGATE}\nmachine: m1\n"
    with pytest.raises(JilParseError) as parse_failure:
        lower_source(text)
    assert "surrogate" in str(parse_failure.value)
    assert parse_failure.value.line == 3  # the attribute's own line
    assert lower_source(text.replace(SURROGATE, "")).jobs["sg_j"]  # clean without it

    hand_built = parse(text.replace(SURROGATE, ""))
    hand_built.statements[0].attrs[1] = (
        hand_built.statements[0].attrs[1].model_copy(update={"raw_value": f"x{SURROGATE}"})
    )
    with pytest.raises(LoweringError) as lowering_failure:
        lower_catalog([hand_built])
    assert "'sg_j'" in str(lowering_failure.value)  # names the statement (whole-dump check)
    assert "surrogate" in str(lowering_failure.value)


def test_pr10a_surrogate_refused_at_spool_decode(tmp_path: Path) -> None:
    """PR-10a, ingress 3: the spool. `load_json` already answers None for
    anything it cannot trust; a record carrying a surrogate joins that path
    rather than inventing a second one."""
    good = tmp_path / "status.json"
    good.write_bytes(b'{"outcome":"exited","exit_code":0}')
    assert load_json(good) == {"outcome": "exited", "exit_code": 0}

    bad = tmp_path / "bad.json"
    bad.write_bytes(b'{"outcome":"exited","note":"\\ud800"}')
    assert load_json(bad) is None

    nested = tmp_path / "nested.json"
    nested.write_bytes(b'{"env":{"K":["\\udfff"]}}')
    assert load_json(nested) is None


# ----------------------------------------------------------- 5. PR-11 floats


def test_pr11_float_refused_at_any_depth() -> None:
    """PR-11: the grammar has no floats, so a float cannot be written --
    which is what makes `deadman_us: int | null` the canonical form of a
    duration. `bool` is not a float and `int` is fine, including a negative
    zero, which is the int 0."""
    for value in (1.5, [1.5], {"a": [{"b": 1.5}]}, float("nan"), float("inf"), [float("-inf")]):
        with pytest.raises(CanonError):
            canonical_bytes(value)
    assert canonical_bytes({"ok": True, "off": False}) == b'{"off":false,"ok":true}'
    assert canonical_bytes({"deadman_us": 60_000_000}) == b'{"deadman_us":60000000}'
    assert canonical_bytes(-0) == b"0"
    assert canonical_bytes({"n": -1}) == b'{"n":-1}'
    # and at decode, where a float literal is the same refusal
    with pytest.raises(CanonError):
        decode(b'{"deadman_s":60.0}')
    with pytest.raises(CanonError):
        decode(b'{"x":[{"y":1e3}]}')
    with pytest.raises(CanonError):
        decode(b'{"x":NaN}')
    assert decode(b'{"deadman_us":60000000}') == {"deadman_us": 60_000_000}


def test_types_outside_the_grammar_are_refused() -> None:
    """ss3.2's grammar is object, array, string, integer, boolean, null, plus
    the datetime spelling. Everything else is a refusal that names the type,
    so the fix is not a guess."""
    for value in (b"bytes", {1, 2}, object(), datetime(2026, 1, 1).date()):
        with pytest.raises(CanonError):
            canonical_bytes(value)
    with pytest.raises(CanonError) as excinfo:
        canonical_bytes({"a": {"b": b"x"}})
    assert "$.a.b" in str(excinfo.value) and "bytes" in str(excinfo.value)
    with pytest.raises(CanonError):
        canonical_bytes({1: "int key"})


# ----------------------------------------------------------- 6. PR-12 duplicate keys


def test_pr12_duplicate_keys_rejected_at_decode() -> None:
    """PR-12: `json.loads` keeps the LAST of a repeated key and says nothing,
    which turns a document two readers disagree about into a silent choice."""
    with pytest.raises(CanonError) as excinfo:
        decode(b'{"period_id":2,"period_id":3}')
    assert "period_id" in str(excinfo.value)
    with pytest.raises(CanonError):
        decode(b'{"state":{"jobs":{"a":1,"a":2}}}')
    with pytest.raises(CanonError):
        decode(b'{"timers":[{"due":1,"due":2}]}')
    assert decode(b'{"a":{"x":1},"b":{"x":2}}') == {"a": {"x": 1}, "b": {"x": 2}}


# ----------------------------------------------------------- 7. PR-13 the digest key


def test_pr13_only_top_level_digest_excluded() -> None:
    """PR-13: a recursive "strip every digest key" implementation would
    collide two documents that differ only in a nested opaque payload."""
    base = {"period_id": 2, "payload": {"digest": "x"}}
    other = {"period_id": 2, "payload": {"digest": "y"}}
    assert digest(base) != digest(other)  # nested is data
    assert digest(base) == digest({**base, "digest": "anything"})  # top level is not
    assert digest({**base, "digest": "a"}) == digest({**base, "digest": "b"})
    assert b'"digest":"x"' in canonical_bytes({key: base[key] for key in base if key != "digest"})
    stamped = with_digest(base)
    assert stamped["digest"] == digest(base)
    assert stamped["payload"] == {"digest": "x"}
    assert base == {"period_id": 2, "payload": {"digest": "x"}}  # with_digest copies


# ----------------------------------------------------------- 8. PR-08d the version


def test_artifact_version_refused_when_unknown() -> None:
    """PR-08d: every artifact refuses a format version this binary does not
    implement, and NAMES it -- an operator holding a newer artifact needs the
    number, not "unsupported"."""
    check_artifact_version({"artifact_format_version": ARTIFACT_FORMAT_VERSION})
    check_artifact_version({"period_id": 2})  # absent: the caller decides
    with pytest.raises(CanonError) as excinfo:
        check_artifact_version({"artifact_format_version": 2})
    assert "2" in str(excinfo.value) and str(ARTIFACT_FORMAT_VERSION) in str(excinfo.value)
    with pytest.raises(CanonError):
        check_artifact_version({"artifact_format_version": True})  # not an integer
    with pytest.raises(CanonError):
        decode(b'{"artifact_format_version":99}')
    assert decode(b'{"artifact_format_version":1,"period_id":2}')


# ----------------------------------------------------------- 9. PR-09 timer payloads


def _armed_timer_kinds(oracle: Oracle) -> dict[str, Event]:
    """Every armed timer, by the kind of deadline it carries. The kind comes
    off the payload the oracle built, not off a list this test keeps."""
    kinds: dict[str, Event] = {}
    for _due, _token, event in oracle.store.timers():
        check = event.payload.get("check")
        kinds[str(check) if check is not None else "run_window_defer"] = event
    return kinds


def _oracle_with_every_timer_armed() -> Oracle:
    """One catalog that arms every deadline oracle.py can enqueue, all at T0
    and all due later, so nothing has fired when the timers are read."""
    text = (
        # must_complete: armed at the start of the run
        "insert_job: pr09_mc\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:50"\n'
        "must_complete_times: +5\n\n"
        # term_run_time: armed at the start of the run
        "insert_job: pr09_trt\njob_type: c\ncommand: x\nmachine: m1\nterm_run_time: 5\n\n"
        # must_start: armed on the tick; the false condition means no run began
        "insert_job: pr09_ms\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "09:50"\n'
        "must_start_times: +5\ncondition: s(pr09_gate)\n\n"
        "insert_job: pr09_gate\njob_type: c\ncommand: x\nmachine: m1\n\n"
        # run_window: a start 10 minutes before the window opens defers
        "insert_job: pr09_rw\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n'
        'run_window: "10:00-11:00"\n'
    )
    oracle = Oracle(lower_source(text))
    for job in ("pr09_mc", "pr09_trt", "pr09_ms", "pr09_rw"):
        oracle.feed(Event(at=T0, kind="STARTJOB", payload={"job": job}))
    return oracle


def test_pr09_every_timer_payload_canonicalizes() -> None:
    """PR-09: canonicalizability is a LIVENESS property. A timer payload that
    could not be written would leave the estate unsealable for as long as that
    timer stayed armed, so every kind the oracle can enqueue is exercised
    here, in both the python and the json dump."""
    oracle = _oracle_with_every_timer_armed()
    kinds = _armed_timer_kinds(oracle)
    # the expected set is DERIVED from oracle.py's closed registry plus the
    # one non-`check` shape, not listed here: a kind added to TIMER_CHECKS
    # without an arming fixture below fails this line by name
    assert set(kinds) == set(TIMER_CHECKS) | {"run_window_defer"}, (
        f"TIMER_CHECKS is {sorted(TIMER_CHECKS)}; this test armed {sorted(kinds)}"
    )
    for kind, event in kinds.items():
        assert canonical_bytes(event.payload), kind
        assert canonical_bytes(event.model_dump(mode="json")), kind
        assert canonical_bytes(event.model_dump()), kind
        assert decode(canonical_bytes(event.payload)) == event.payload


def test_pr09_covers_every_timer_call_site_in_oracle_py() -> None:
    """The watched set is DERIVED, not listed (DL-83): the arming sites come
    off oracle.py's own AST and the kinds come off the timers the test armed.
    A new `_schedule_timer` call site makes the two counts disagree and this
    test names it -- including one that arms an already-covered kind, which is
    a cheap false positive next to a timer nobody proved canonicalizable.

    The second half pins the funnel: `enqueue_timer` is reached only through
    `_schedule_timer`, so counting that wrapper counts everything."""
    source = pathlib.Path(inspect.getsourcefile(Oracle) or "")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    arming: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "_schedule_timer":
                arming.append(node.lineno)
    covered = _armed_timer_kinds(_oracle_with_every_timer_armed())
    assert len(arming) == len(covered), (
        f"oracle.py arms timers at lines {arming}, and this test exercises"
        f" {sorted(covered)}: cover the new site (PR-09)"
    )
    # the registry is closed at the wrapper: an unregistered check is refused
    # before it reaches the heap, so a new kind cannot slip in behind an
    # existing call site without first being named in TIMER_CHECKS
    from dsl41.oracle_state import Event as _Event

    from dsl41.oracle_state import OracleError as _OracleError

    with pytest.raises(_OracleError, match="unregistered timer check"):
        _oracle_with_every_timer_armed()._schedule_timer(
            datetime(2026, 7, 1, 8, 0),
            _Event(at=datetime(2026, 7, 1, 8, 0), kind="TIMER", payload={"check": "made_up"}),
        )
    with pytest.raises(_OracleError, match="unregistered timer shape"):
        _oracle_with_every_timer_armed()._schedule_timer(
            datetime(2026, 7, 1, 8, 0),
            _Event(at=datetime(2026, 7, 1, 8, 0), kind="TIMER", payload={"job": "j"}),
        )
    # a registered shape whose payload does not canonicalize is refused too:
    # the registry names shapes, the bytes are proven at arm time
    with pytest.raises(_OracleError, match="not canonicalizable"):
        _oracle_with_every_timer_armed()._schedule_timer(
            datetime(2026, 7, 1, 8, 0),
            _Event(at=datetime(2026, 7, 1, 8, 0), kind="TIMER", payload={"deferred_cause": 1.5}),
        )
    wrapper = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_schedule_timer"
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            inside = wrapper.lineno <= node.lineno <= (wrapper.end_lineno or wrapper.lineno)
            assert node.func.attr != "enqueue_timer" or inside, (
                f"oracle.py line {node.lineno} arms a timer around _schedule_timer;"
                " route it through the wrapper so PR-09's guard can see it"
            )
