"""PR-34 / PR-34a: the FW watch is evidence, not memory (period-model ss2.2,
DL-129).

The adapter's progress -- last observed size and stable-poll count -- decides
when a watch completes, and a restart used to reset both. Draft 4 carried them
in a local variable fed by unjournaled `os.stat` calls, so an audit replaying
the START input could not derive whether the seal should say
`previous_size=10`, `null`, or a completed watch. So the watch gets an
append-only spool: a `start` line on dispatch, then one line per poll,
fsynced, INCLUDING polls that changed nothing.

The two timestamps `next_poll_at` is made of are asserted DIRECTLY here, off
the log, not through the helper that computes them (ss2.2's obligation).

The anchor-fence re-check per poll and the ss6 seal barrier are a later unit's
and are deliberately absent.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dsl41.canon import ARTIFACT_FORMAT_VERSION, canonical_bytes, decode
from dsl41.ir import lower_source
from dsl41.oracle_state import Event
from dsl41.runner_adapters import WATCH_LOG, FileWatcherAdapter, read_watch_log
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_startup import resume_run, start_run
from dsl41.period import active_wal

T0 = datetime(2026, 7, 1, 8, 0)


def _catalog(watch_file: Path, *, min_size: int = 5, interval: int = 60):
    return lower_source(
        f"insert_job: w\njob_type: f\nwatch_file: {watch_file}\n"
        f"watch_interval: {interval}\nwatch_file_min_size: {min_size}\n"
    )


def _log_path(run_root: Path, run_number: int = 1) -> Path:
    return run_root / "runs" / f"w.{run_number}" / "watch.jsonl"


def _lines(run_root: Path, run_number: int = 1) -> list[dict]:
    """Every durable line, decoded through the ss3.2 reader (so a line this
    project could not canonicalize would fail here first)."""
    raw = _log_path(run_root, run_number).read_bytes()
    return [decode(line) for line in raw.split(b"\n") if line]  # type: ignore[misc]


def _start_engine(catalog, run_root: Path, clock: VirtualClock):
    engine = start_run(catalog, run_root, clock=clock, adapters={"FW": FileWatcherAdapter()})
    engine.inject(Event(at=T0, kind="STARTJOB", payload={"job": "w"}))
    return engine


async def _close(engine) -> None:
    await engine.shutdown()
    if engine.journal is not None:
        engine.journal.close()


def _journal_records(run_root: Path) -> list[dict]:
    return [json.loads(line) for line in active_wal(run_root).read_text().splitlines() if line]


def _rewrite_journal(run_root: Path, records: list[dict]) -> None:
    active_wal(run_root).write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))


def _spawn_effect(records: list[dict]) -> dict:
    for record in records:
        if record.get("rec") == "decision":
            for effect in record.get("effects", []):
                if effect["kind"] == "SPAWN" and effect["job"] == "w":
                    return effect
    raise AssertionError("no SPAWN effect for w in the journal")


# ------------------------------------------------------------- the poll phase


def test_pr34_next_poll_at_is_start_at_then_poll_at_plus_interval(tmp_path: Path) -> None:
    """ss2.2, exactly: after `start` and no poll line, `start.at` -- the first
    poll is immediate; after a poll line, `poll.at + interval`. Draft 8
    derived the first poll from the STARTING row's `status_at`, which for a
    SPAWN pending on a passive host precedes actual dispatch by hours."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start_engine(_catalog(tmp_path / "watched"), run_root, VirtualClock(start=T0))
        await engine.run_until_quiescent(T0)  # dispatch + the immediate poll
        lines = _lines(run_root)
        assert [r["kind"] for r in lines] == ["start", "poll"]
        assert lines[0]["at"] == T0.isoformat(timespec="microseconds")
        assert lines[1]["at"] == lines[0]["at"], "the first poll is at start.at"

        await engine.run_until_quiescent(T0 + timedelta(seconds=90))
        lines = _lines(run_root)
        assert len(lines) == 3
        assert lines[2]["at"] == (T0 + timedelta(seconds=60)).isoformat(timespec="microseconds")
        await _close(engine)

    asyncio.run(scenario())


def test_pr34_a_line_lands_for_every_poll_including_the_ones_that_changed_nothing(
    tmp_path: Path,
) -> None:
    """The ss6 poll-phase table, as a log. Absence, below-min and no-change
    polls all append -- a spool that only recorded transitions would be one
    audit could not use to reproduce `next_poll_at`, which moves on every
    poll."""
    watch_file = tmp_path / "watched"
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start_engine(_catalog(watch_file), run_root, VirtualClock(start=T0))
        await engine.run_until_quiescent(T0 + timedelta(seconds=30))  # absent
        await engine.run_until_quiescent(T0 + timedelta(seconds=90))  # absent again
        watch_file.write_bytes(b"ab")  # present, below min_size
        await engine.run_until_quiescent(T0 + timedelta(seconds=150))
        watch_file.write_bytes(b"abcdef")  # qualifies: first such poll
        await engine.run_until_quiescent(T0 + timedelta(seconds=210))
        watch_file.write_bytes(b"abcdefgh")  # grew: the stability count resets
        await engine.run_until_quiescent(T0 + timedelta(seconds=270))
        await engine.run_until_quiescent(T0 + timedelta(seconds=330))  # same size
        assert engine.oracle.store.job["w"].status == "SUCCESS"

        polls = [r for r in _lines(run_root) if r["kind"] == "poll"]
        assert [(r["exists"], r["size"], r["qualifying"], r["stable_polls"]) for r in polls] == [
            (False, None, False, 0),  # +0    absent
            (False, None, False, 0),  # +60   absent, and still recorded
            (True, 2, False, 0),  # +120  present, below min_size
            (True, 6, True, 1),  # +180  qualifies
            (True, 8, True, 1),  # +240  grew: back to one qualifying poll
            (True, 8, True, 2),  # +300  stable: the watch is over
        ]
        await _close(engine)

    asyncio.run(scenario())


def test_pr34_the_start_line_is_the_first_durable_act_and_carries_the_effects_run_id(
    tmp_path: Path,
) -> None:
    """A dispatched watch always has `watch_seq >= 1` (a watch not yet
    dispatched is a `pending_spawn`, not an `fw_watch`), and the identity on
    the line is the one the DECISION minted -- one key through the WAL and
    the spool (DL-118)."""
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start_engine(_catalog(tmp_path / "watched"), run_root, VirtualClock(start=T0))
        await engine.run_until_quiescent(T0)
        await _close(engine)

    asyncio.run(scenario())
    lines = _lines(run_root)
    assert lines[0]["kind"] == "start"
    effect = _spawn_effect(_journal_records(run_root))
    assert lines[0]["run_id"] == effect["run_id"]
    assert all(r["run_id"] == effect["run_id"] for r in lines)
    fold = read_watch_log(run_root / "runs" / "w.1")
    assert fold is not None and fold.watch_seq == len(lines) and fold.run_id == effect["run_id"]


def test_pr34_a_completing_poll_ends_the_watch_at_that_poll(tmp_path: Path) -> None:
    """No restart in between: the completing observation is the line that
    carries `stable_polls: 2`, and the job's completion is that same poll."""
    watch_file = tmp_path / "watched"
    watch_file.write_bytes(b"abcdef")
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start_engine(_catalog(watch_file), run_root, VirtualClock(start=T0))
        await engine.run_until_quiescent(T0)
        assert engine.oracle.store.job["w"].status == "RUNNING"
        await engine.run_until_quiescent(T0 + timedelta(seconds=90))
        assert engine.oracle.store.job["w"].status == "SUCCESS"
        await _close(engine)

    asyncio.run(scenario())
    lines = _lines(run_root)
    assert lines[-1]["stable_polls"] == 2
    assert lines[-1]["at"] == (T0 + timedelta(seconds=60)).isoformat(timespec="microseconds")
    fold = read_watch_log(run_root / "runs" / "w.1")
    assert fold is not None and fold.complete


def test_pr34_a_restart_between_the_two_qualifying_polls_completes_the_same_watch(
    tmp_path: Path,
) -> None:
    """The same completion with a restart in between (PR-34): the resumed
    watch reconstructs progress from the log, so the second qualifying poll
    still completes it -- and the log is one story, with ONE start line and
    the next poll where the last one said it would be."""
    watch_file = tmp_path / "watched"
    watch_file.write_bytes(b"abcdef")
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = _start_engine(_catalog(watch_file), run_root, VirtualClock(start=T0))
        await engine.run_until_quiescent(T0)  # one qualifying poll, stable=1
        assert engine.oracle.store.job["w"].status == "RUNNING"
        await _close(engine)

    asyncio.run(first())
    before = _lines(run_root)
    assert [r["kind"] for r in before] == ["start", "poll"]
    assert before[1]["stable_polls"] == 1

    async def second() -> None:
        engine = await resume_run(
            _catalog(watch_file),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"FW": FileWatcherAdapter()},
        )
        assert engine.oracle.store.job["w"].status == "RUNNING"
        await engine.run_until_quiescent(T0 + timedelta(seconds=90))
        assert engine.oracle.store.job["w"].status == "SUCCESS"
        await _close(engine)

    asyncio.run(second())
    after = _lines(run_root)
    assert [r["kind"] for r in after].count("start") == 1, "a resume appends no second start"
    assert after[: len(before)] == before, "the log is append-only"
    assert after[-1]["stable_polls"] == 2
    assert after[-1]["at"] == (T0 + timedelta(seconds=60)).isoformat(timespec="microseconds")


def test_pr34_a_torn_final_line_truncates_on_open(tmp_path: Path) -> None:
    """The WAL's rule, applied to the spool: a crash mid-append leaves a
    prefix, the reader drops it, and the bytes must agree with that reading
    before the next line lands -- appended straight after the fragment, the
    two become one corrupt interior line and every later read raises."""
    watch_file = tmp_path / "watched"
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = _start_engine(_catalog(watch_file), run_root, VirtualClock(start=T0))
        await engine.run_until_quiescent(T0)
        await _close(engine)

    asyncio.run(first())
    whole = _lines(run_root)
    with _log_path(run_root).open("ab") as f:
        f.write(b'{"artifact_format_version":1,"at":"2026-07-01T08:01:0')  # torn append

    fold = read_watch_log(run_root / "runs" / "w.1")  # the fold already ignores it
    assert fold is not None and fold.watch_seq == len(whole)
    watch_file.write_bytes(b"abcdef")

    async def second() -> None:
        engine = await resume_run(
            _catalog(watch_file),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"FW": FileWatcherAdapter()},
        )
        await engine.run_until_quiescent(T0 + timedelta(seconds=200))
        assert engine.oracle.store.job["w"].status == "SUCCESS"
        await _close(engine)

    asyncio.run(second())
    raw = _log_path(run_root).read_bytes()
    assert raw.endswith(b"\n") and b'"at":"2026-07-01T08:01:0\n' not in raw
    assert [r["kind"] for r in _lines(run_root)].count("start") == 1


# ------------------------------------------------- the ss11 ladder's FW rules


def test_pr34_a_start_line_resolves_a_pending_spawn(tmp_path: Path) -> None:
    """ss11's ladder gains the rule: a pending FW SPAWN whose run directory
    holds a `start` line carrying the effect's run_id is APPLIED by that line.

    The window is real -- the decision commits, the adapter appends `start`,
    the engine dies before `effect_result{applied}` -- and without the rule
    the ladder treats the pending SPAWN as an applied-SPAWN candidate, looks
    for `spawn.json`, finds none, and re-launches the watch as an untraced
    start: two `start` lines and a fold nothing can reproduce."""
    watch_file = tmp_path / "watched"
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = _start_engine(_catalog(watch_file), run_root, VirtualClock(start=T0))
        await engine.run_until_quiescent(T0)
        await _close(engine)

    asyncio.run(first())
    records = _journal_records(run_root)
    effect = _spawn_effect(records)
    # the crash: the decision and the start line are durable, the outcome is not
    _rewrite_journal(
        run_root,
        [r for r in records if r.get("effect_id") != effect["effect_id"]],
    )

    async def second():
        engine = await resume_run(
            _catalog(watch_file),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"FW": FileWatcherAdapter()},
        )
        state = engine.outbox.state_of(effect["effect_id"])
        outcome = engine.outbox.result_for(effect["effect_id"])
        await _close(engine)
        return state, outcome

    state, outcome = asyncio.run(second())
    assert state == "applied"
    assert outcome.run_id == effect["run_id"]
    assert "watch log" in (outcome.detail or "")
    assert [r["kind"] for r in _lines(run_root)].count("start") == 1


def test_pr34a_a_completing_poll_then_a_crash_injects_the_completion_from_the_log(
    tmp_path: Path,
) -> None:
    """PR-34a. The sibling window: a completing poll is appended and the
    engine dies before the STATUS input is durable. Resume meets a log whose
    last line is a completing observation and a row still RUNNING, and the
    ladder injects the completion FROM THE LOG, exactly as it injects a CMD's
    from `status.json`. Re-polling would decide the watch again against a
    world that has moved on -- the file may be gone by then."""
    watch_file = tmp_path / "watched"
    watch_file.write_bytes(b"abcdef")
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = _start_engine(_catalog(watch_file), run_root, VirtualClock(start=T0))
        await engine.run_until_quiescent(T0 + timedelta(seconds=90))
        assert engine.oracle.store.job["w"].status == "SUCCESS"
        await _close(engine)

    asyncio.run(first())
    completed = _lines(run_root)
    assert completed[-1]["stable_polls"] == 2

    records = _journal_records(run_root)
    cut = next(
        i
        for i, r in enumerate(records)
        if r.get("rec") == "input" and r.get("kind") == "STATUS" and r["payload"]["job"] == "w"
    )
    _rewrite_journal(run_root, records[:cut])  # the crash, at the write-ahead point
    watch_file.unlink()  # the world moved on: a re-poll would never complete

    async def second():
        engine = await resume_run(
            _catalog(watch_file),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"FW": FileWatcherAdapter()},
        )
        await engine.run_until_quiescent(T0 + timedelta(seconds=400))
        status = engine.oracle.store.job["w"].status
        await _close(engine)
        return status

    assert asyncio.run(second()) == "SUCCESS"
    assert _lines(run_root) == completed, "the injection re-polls nothing"
    sources = [
        r.get("source")
        for r in _journal_records(run_root)
        if r.get("rec") == "input" and r.get("kind") == "STATUS"
    ]
    assert sources == ["reconcile"]


def test_pr34_a_run_directory_with_no_log_is_re_dispatched_under_the_bound_id(
    tmp_path: Path,
) -> None:
    """The one FW case with no log to reconstruct from: the directory was
    made and the crash landed before the `start` line. The re-dispatch has no
    effect behind it, so without the bound id riding along it would write
    `run_id: null` -- and the next resume would meet a spool naming no run
    while the WAL names one, which is the split DL-118 refuses."""
    run_root = tmp_path / "run"

    async def first() -> None:
        engine = _start_engine(_catalog(tmp_path / "watched"), run_root, VirtualClock(start=T0))
        await engine.run_until_quiescent(T0)
        await _close(engine)

    asyncio.run(first())
    effect = _spawn_effect(_journal_records(run_root))
    _log_path(run_root).unlink()  # the crash: the directory outlived its log

    async def second() -> None:
        engine = await resume_run(
            _catalog(tmp_path / "watched"),
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"FW": FileWatcherAdapter()},
        )
        await engine.run_until_quiescent(T0)
        await _close(engine)

    asyncio.run(second())
    lines = _lines(run_root)
    assert lines[0]["kind"] == "start"
    assert lines[0]["run_id"] == effect["run_id"]


def test_pr34_a_complete_line_this_binary_cannot_read_is_refused_not_truncated(
    tmp_path: Path,
) -> None:
    """A torn line is a SYNTAX question. A whole line the §3.2 ingress refuses
    -- an `artifact_format_version` this binary does not implement (PR-08d) --
    is evidence a future binary wrote, and truncating it as "torn" would
    delete it. It refuses loudly and the bytes stay."""
    from dsl41.runner_adapters import _repair_watch_tail
    from dsl41.runner_clock import EngineError

    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = _start_engine(_catalog(tmp_path / "watched"), run_root, VirtualClock(start=T0))
        await engine.run_until_quiescent(T0)
        await _close(engine)

    asyncio.run(scenario())
    with _log_path(run_root).open("ab") as f:
        f.write(b'{"artifact_format_version":2,"at":"2026-07-01T08:01:00.000000","kind":"poll"}\n')
    before = _log_path(run_root).read_bytes()

    _repair_watch_tail(_log_path(run_root))
    assert _log_path(run_root).read_bytes() == before

    try:
        read_watch_log(run_root / "runs" / "w.1")
    except EngineError as exc:
        assert "artifact_format_version 2" in str(exc)
    else:
        raise AssertionError("an unimplemented artifact version must refuse")


def test_pr34_a_poll_naming_another_run_never_completes_the_watch(tmp_path: Path) -> None:
    """DL-118 inside the fold: the start line names the run, and every later
    line must name the same one -- a completing poll carrying a stranger's
    run_id must refuse loudly, not complete this watch with someone else's
    observation. An unknown line kind refuses the same way: evidence the
    fold cannot hold is never silently skipped."""
    run_dir = tmp_path / "j.1"
    run_dir.mkdir()
    path = run_dir / WATCH_LOG
    start = {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "at": "2026-07-01T08:00:00.000000",
        "kind": "start",
        "run_id": "00000000-0000-4000-8000-000000000001",
    }
    foreign_poll = {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "at": "2026-07-01T08:01:00.000000",
        "exists": True,
        "kind": "poll",
        "qualifying": True,
        "run_id": "00000000-0000-4000-8000-000000000002",
        "size": 5,
        "stable_polls": 2,
    }
    path.write_bytes(canonical_bytes(start) + b"\n" + canonical_bytes(foreign_poll) + b"\n")
    with pytest.raises(EngineError, match="names run_id"):
        read_watch_log(run_dir)

    stranger_kind = {**foreign_poll, "run_id": start["run_id"], "kind": "checkpoint"}
    path.write_bytes(canonical_bytes(start) + b"\n" + canonical_bytes(stranger_kind) + b"\n")
    with pytest.raises(EngineError, match="checkpoint"):
        read_watch_log(run_dir)

    # and a first line that is not `start` is a foreign log, not an
    # undispatched watch -- None here would re-dispatch over it
    path.write_bytes(canonical_bytes({**foreign_poll, "run_id": start["run_id"]}) + b"\n")
    with pytest.raises(EngineError, match="not 'start'"):
        read_watch_log(run_dir)


def test_pr34_a_recorded_count_the_observations_cannot_derive_refuses(tmp_path: Path) -> None:
    """The completion state is derived, and the recorded count is checked
    against it: a forged `qualifying: false, stable_polls: 2` line would
    otherwise inject a SUCCESS from an observation that observed nothing."""
    run_dir = tmp_path / "j.1"
    run_dir.mkdir()
    rid = "00000000-0000-4000-8000-000000000001"
    start = {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "at": "2026-07-01T08:00:00.000000",
        "kind": "start",
        "run_id": rid,
    }
    forged = {
        "artifact_format_version": ARTIFACT_FORMAT_VERSION,
        "at": "2026-07-01T08:01:00.000000",
        "exists": False,
        "kind": "poll",
        "qualifying": False,
        "run_id": rid,
        "size": None,
        "stable_polls": 2,
    }
    (run_dir / WATCH_LOG).write_bytes(
        canonical_bytes(start) + b"\n" + canonical_bytes(forged) + b"\n"
    )
    with pytest.raises(EngineError, match="derive 0"):
        read_watch_log(run_dir)


def test_pr34_the_adapter_refuses_to_adopt_a_strangers_watch(tmp_path: Path) -> None:
    """The window between the preflight and the adapter's own read: a log
    that appeared there naming another run must refuse at THIS read too --
    adopting it would poll a watch the WAL never dispatched."""
    from dsl41.ir import lower_source
    from dsl41.runner_adapters import AdapterContext

    catalog = lower_source("insert_job: j\njob_type: f\nwatch_file: /tmp/nope\nwatch_interval: 1\n")
    run_dir = tmp_path / "runs" / "j.1"
    run_dir.mkdir(parents=True)
    (run_dir / WATCH_LOG).write_bytes(
        canonical_bytes(
            {
                "artifact_format_version": ARTIFACT_FORMAT_VERSION,
                "at": "2026-07-01T08:00:00.000000",
                "kind": "start",
                "run_id": "00000000-0000-4000-8000-000000000bad",
            }
        )
        + b"\n"
    )
    ctx = AdapterContext(
        clock=VirtualClock(start=datetime(2026, 7, 1, 8, 0)),
        run_root=tmp_path,
        run_id="00000000-0000-4000-8000-000000000001",
    )

    async def scenario() -> None:
        with pytest.raises(EngineError, match="stranger's watch"):
            await FileWatcherAdapter().run(catalog.jobs["j"], 1, ctx)

    asyncio.run(scenario())


def test_pr34_an_unversioned_line_is_unsupported_evidence(tmp_path: Path) -> None:
    """`decode` refuses a version it does not implement but passes an absent
    one; this reader requires it -- a versionless completing line must never
    inject a SUCCESS."""
    run_dir = tmp_path / "j.1"
    run_dir.mkdir()
    rid = "00000000-0000-4000-8000-000000000001"
    start = {"at": "2026-07-01T08:00:00.000000", "kind": "start", "run_id": rid}
    (run_dir / WATCH_LOG).write_bytes(canonical_bytes(start) + b"\n")
    with pytest.raises(EngineError, match="artifact_format_version"):
        read_watch_log(run_dir)
