"""WAL journal tests: record shapes, replay, and the `dsl41 journal` CLI
(phase 11b).

Normative spec: docs/runner-design.md ss7 (journal and recovery) and ss4 (the
stale-completion gate whose rejections the WAL also records as drops).
runner.py's Journal/read_journal/replay_inputs/catalog_hash docstrings are
the API under test; cli.py's `journal` command is the CLI surface. Resume
and the crash/reconciliation ladder are tests/test_runner_lifecycle.py's
territory (owned elsewhere, not duplicated here) -- this file stays on the
WAL itself: what gets written, what read_journal tolerates or refuses, and
that replaying the inputs alone reproduces the exact trace a live engine
produced.

House style follows test_runner.py: T0 = datetime(2026, 7, 1, 8, 0), an
ev() helper, one asyncio.run per async scenario. Every expected outcome here
was verified empirically against the real runner/CLI before the assertion
was written (CLAUDE.md: fidelity is tested, not asserted) -- see the final
report for anything that surprised us or contradicted the design doc.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41.cli import app
from dsl41.ir import lower_source
from dsl41.oracle import Event, EventKind, Oracle
from dsl41.runner import (
    Engine,
    EngineError,
    FakeAdapter,
    Journal,
    VirtualClock,
    catalog_hash,
    read_journal,
    replay_inputs,
    start_run,
)

T0 = datetime(2026, 7, 1, 8, 0)


def ev(kind: EventKind, minutes: float = 0.0, **payload: object) -> Event:
    return Event(at=T0 + timedelta(minutes=minutes), kind=kind, payload=payload)


_SOLO_JIL = "insert_job: j1\njob_type: c\ncommand: x\nmachine: m1\n"


# --------------------------------------------------------------- 1. record shapes


def test_header_record_carries_catalog_hash_version_domain_and_started_at(
    tmp_path: Path,
) -> None:
    """(runner-design ss7 / Journal.create docstring): header = {catalog_hash,
    dsl41_version, clock_domain, started_at}. catalog_hash matches the
    standalone catalog_hash() function -- one source of truth, no drift
    between Journal.create's own hashing and the resume gate."""
    catalog = lower_source(_SOLO_JIL)
    journal = Journal.create(
        tmp_path / "journal.jsonl", catalog=catalog, clock_domain="real", started_at=T0
    )
    journal.close()
    header = read_journal(tmp_path / "journal.jsonl")[0]
    assert header["rec"] == "header"
    assert header["catalog_hash"] == catalog_hash(catalog)
    assert isinstance(header["dsl41_version"], str) and header["dsl41_version"]
    assert header["clock_domain"] == "real"
    assert header["started_at"] == T0.isoformat()


def test_input_record_carries_seq_at_kind_payload_source_and_seq_increments(
    tmp_path: Path,
) -> None:
    """(runner-design ss7): input = {seq, at, kind, payload, source}, source
    in {scheduler, adapter, control, reconcile}. seq is a monotone WAL
    sequence number, independent of `at` (adapter completions can enqueue
    out of chronological arrival relative to future-stamped script events --
    module docstring)."""
    journal = Journal.create(
        tmp_path / "journal.jsonl",
        catalog=lower_source(_SOLO_JIL),
        clock_domain="virtual",
        started_at=T0,
    )
    journal.input(ev("STARTJOB", 0, job="j1"), source="control")
    journal.input(ev("STATUS", 1, job="j1", run_number=1, exit_code=0), source="adapter")
    journal.close()
    records = read_journal(tmp_path / "journal.jsonl")
    inputs = [r for r in records if r["rec"] == "input"]
    assert inputs[0] == {
        "rec": "input",
        "seq": 1,
        "at": T0.isoformat(),
        "kind": "STARTJOB",
        "payload": {"job": "j1"},
        "source": "control",
    }
    assert inputs[1]["seq"] == 2
    assert inputs[1]["source"] == "adapter"
    assert inputs[1]["payload"] == {"job": "j1", "run_number": 1, "exit_code": 0}


def test_dispatch_record_carries_job_run_number_wrapper_pid_run_dir_started_at(
    tmp_path: Path,
) -> None:
    """(runner-design ss7): dispatch is audit/ordering only -- the wrapper's
    own spawn.json is the authoritative spawn record (module docstring) --
    so it carries wrapper_pid + run_dir rather than a pgid the engine never
    observes."""
    journal = Journal.create(
        tmp_path / "journal.jsonl",
        catalog=lower_source(_SOLO_JIL),
        clock_domain="real",
        started_at=T0,
    )
    journal.dispatch("j1", 1, wrapper_pid=4242, run_dir="/abs/runs/j1.1", started_at=T0)
    journal.close()
    dispatch = next(r for r in read_journal(tmp_path / "journal.jsonl") if r["rec"] == "dispatch")
    assert dispatch == {
        "rec": "dispatch",
        "job": "j1",
        "run_number": 1,
        "wrapper_pid": 4242,
        "run_dir": "/abs/runs/j1.1",
        "started_at": T0.isoformat(),
    }


def test_drop_record_carries_at_kind_payload_reason_and_no_seq(tmp_path: Path) -> None:
    """(runner-design ss4/ss7): drop records a gate rejection; unlike input
    it consumes no WAL seq number -- it never fed the oracle."""
    journal = Journal.create(
        tmp_path / "journal.jsonl",
        catalog=lower_source(_SOLO_JIL),
        clock_domain="virtual",
        started_at=T0,
    )
    journal.drop(ev("STATUS", 3, job="j1", run_number=0, exit_code=0), "run_number mismatch")
    journal.close()
    drop = next(r for r in read_journal(tmp_path / "journal.jsonl") if r["rec"] == "drop")
    assert drop == {
        "rec": "drop",
        "at": (T0 + timedelta(minutes=3)).isoformat(),
        "kind": "STATUS",
        "payload": {"job": "j1", "run_number": 0, "exit_code": 0},
        "reason": "run_number mismatch",
    }
    assert "seq" not in drop


def test_fsync_each_true_in_real_domain_buffered_in_virtual(tmp_path: Path) -> None:
    """(Journal class docstring): "fsync per record in the real domain
    (write-ahead: append + fsync BEFORE feed); buffered in rehearse, fsync
    on close." White-box on the flag Journal.create derives from
    clock_domain."""
    real = Journal.create(
        tmp_path / "real.jsonl", catalog=lower_source(_SOLO_JIL), clock_domain="real", started_at=T0
    )
    virtual = Journal.create(
        tmp_path / "virtual.jsonl",
        catalog=lower_source(_SOLO_JIL),
        clock_domain="virtual",
        started_at=T0,
    )
    try:
        assert real._fsync_each is True
        assert virtual._fsync_each is False
    finally:
        real.close()
        virtual.close()


# ------------------------------------------------------------- 2. read_journal


def _write_two_input_journal(tmp_path: Path) -> Path:
    path = tmp_path / "journal.jsonl"
    journal = Journal.create(
        path, catalog=lower_source(_SOLO_JIL), clock_domain="virtual", started_at=T0
    )
    journal.input(ev("STARTJOB", 0, job="j1"), source="control")
    journal.input(ev("STATUS", 1, job="j1", run_number=1, exit_code=0), source="adapter")
    journal.close()
    return path


def test_read_journal_tolerates_a_torn_final_line(tmp_path: Path) -> None:
    """(read_journal docstring): a crash mid-append of the LAST line leaves a
    truncated final record -- write-ahead means the feed it would have
    preceded never ran, so it is dropped silently rather than refused."""
    path = _write_two_input_journal(tmp_path)
    lines = path.read_bytes().split(b"\n")
    assert lines[-1] == b""  # trailing newline on a clean write
    torn = b"\n".join(lines[:-2]) + b"\n" + lines[-2][:15]  # cut the last record short
    path.write_bytes(torn)
    records = read_journal(path)
    assert [r["rec"] for r in records] == ["header", "input"]


def test_read_journal_refuses_interior_corruption(tmp_path: Path) -> None:
    """(read_journal docstring): a torn or invalid line that is NOT the final
    line is corruption, never a torn write-ahead append -- raised loudly."""
    path = _write_two_input_journal(tmp_path)
    lines = path.read_bytes().split(b"\n")
    lines[1] = lines[1][:8]  # corrupt the first input record (interior)
    path.write_bytes(b"\n".join(lines))
    with pytest.raises(EngineError, match="corrupt line"):
        read_journal(path)


def test_read_journal_refuses_missing_header(tmp_path: Path) -> None:
    """(read_journal docstring): every journal must open with a header
    record; a file that starts with anything else is refused."""
    path = tmp_path / "journal.jsonl"
    path.write_text(
        '{"rec": "input", "seq": 1, "at": "2026-07-01T08:00:00",'
        ' "kind": "STARTJOB", "payload": {}, "source": "control"}\n'
    )
    with pytest.raises(EngineError, match="missing header"):
        read_journal(path)


# ------------------------------------------------------------- 3. catalog_hash


def test_catalog_hash_stable_across_identical_loads() -> None:
    """(catalog_hash docstring): a sha256 of the canonical JSON dump -- the
    same estate text loaded twice hashes identically."""
    assert catalog_hash(lower_source(_SOLO_JIL)) == catalog_hash(lower_source(_SOLO_JIL))


def test_catalog_hash_differs_on_any_change() -> None:
    """(catalog_hash docstring): "an estate that changed in ANY way
    re-baselines explicitly" -- even a one-token command change flips it."""
    changed = _SOLO_JIL.replace("command: x", "command: y")
    assert catalog_hash(lower_source(_SOLO_JIL)) != catalog_hash(lower_source(changed))


# ------------------------------------------------------------------ 4. replay


async def _run_virtual(
    text: str, run_root: Path, script: list[Event], *, adapter: FakeAdapter, horizon: datetime
) -> Engine:
    engine = start_run(
        lower_source(text),
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": adapter, "FW": adapter},
    )
    for e in script:
        engine.inject(e)
    await engine.run_until_quiescent(horizon)
    await engine.shutdown()
    assert engine.journal is not None
    engine.journal.close()
    return engine


def test_replay_reproduces_a_startjob_cascade_and_killjob_trace(tmp_path: Path) -> None:
    """(runner-design ss7 inputs-only principle): a virtual-domain engine
    run's WAL, replayed through a fresh Oracle, reproduces the SAME trace the
    live engine produced -- byte-identical, proving emitted events really are
    a pure function of the input sequence and nothing else is ever needed to
    reconstruct them."""
    text = (
        "insert_job: job_a\njob_type: c\ncommand: x\nmachine: m1\n\n"
        "insert_job: job_b\njob_type: c\ncommand: y\nmachine: m1\ncondition: s(job_a)\n"
    )
    adapter = FakeAdapter({("job_a", 1): (300.0, 0)})
    script = [ev("STARTJOB", 0, job="job_a"), ev("KILLJOB", 2, job="job_a")]

    async def scenario() -> Engine:
        return await _run_virtual(
            text, tmp_path / "run", script, adapter=adapter, horizon=T0 + timedelta(minutes=30)
        )

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["job_a"].status == "TERMINATED"  # killed before its 5min exit
    assert engine.oracle.store.job["job_b"].status == "INACTIVE"  # s(job_a) never satisfied

    records = read_journal(tmp_path / "run" / "journal.jsonl")
    fresh = Oracle(lower_source(text))
    replay_inputs(fresh, records)
    assert [t.model_dump() for t in fresh.trace()] == [
        t.model_dump() for t in engine.oracle.trace()
    ]


def test_replay_reproduces_a_set_global_gated_trace(tmp_path: Path) -> None:
    """(runner-design ss7): a second, structurally different script --
    SET_GLOBAL rather than STARTJOB/KILLJOB -- reproduces just as exactly,
    proving replay_inputs is not special-cased to any one event kind."""
    text = "insert_job: gate\njob_type: c\ncommand: x\nmachine: m1\ncondition: v(FLAG) = go\n"
    adapter = FakeAdapter({("gate", 1): (5.0, 0)})
    script = [
        ev("SET_GLOBAL", 0, name="FLAG", value="stop"),
        ev("SET_GLOBAL", 1, name="FLAG", value="go"),
    ]

    async def scenario() -> Engine:
        return await _run_virtual(
            text, tmp_path / "run", script, adapter=adapter, horizon=T0 + timedelta(minutes=10)
        )

    engine = asyncio.run(scenario())
    assert engine.oracle.store.job["gate"].status == "SUCCESS"

    records = read_journal(tmp_path / "run" / "journal.jsonl")
    fresh = Oracle(lower_source(text))
    replay_inputs(fresh, records)
    assert [t.model_dump() for t in fresh.trace()] == [
        t.model_dump() for t in engine.oracle.trace()
    ]


# ------------------------------------------------ 5. engine journal-first behavior


def test_input_records_tag_injected_events_control_and_completions_adapter(
    tmp_path: Path,
) -> None:
    """(runner-design ss7 input record: source in {scheduler, adapter,
    control, reconcile}): Engine.inject() defaults to source="control";
    an adapter completion enqueues with source="adapter" (Engine._run_adapter
    -> _enqueue's default) -- both land in the WAL untouched."""
    text = "insert_job: j1\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine = start_run(
            lower_source(text),
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("j1", 1): (60.0, 0)})},
        )
        engine.inject(ev("STARTJOB", 0, job="j1"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    inputs = [r for r in read_journal(tmp_path / "run" / "journal.jsonl") if r["rec"] == "input"]
    assert inputs[0]["kind"] == "STARTJOB"
    assert inputs[0]["source"] == "control"
    completion = next(r for r in inputs if r["kind"] == "STATUS")
    assert completion["source"] == "adapter"
    assert completion["payload"] == {"job": "j1", "run_number": 1, "exit_code": 0}


def test_gate_dropped_completion_is_journaled_as_a_drop_record(tmp_path: Path) -> None:
    """(runner-design ss4 stale-completion gate + ss7): a completion the
    engine drops (run_number mismatch) never feeds the oracle but IS
    journaled -- "never a silent overwrite" extends to never silently
    vanishing either. White-box via the same Engine._enqueue(is_completion=
    True) trick test_runner.py's gate test uses -- 11a has no black-box
    trigger for this gate under VirtualClock (runner.py module docstring:
    the natural-exit-vs-kill race always resolves to the kill there)."""
    text = "insert_job: sa\njob_type: c\ncommand: x\nmachine: m1\n"

    async def scenario() -> None:
        engine = start_run(
            lower_source(text),
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter({("sa", 1): (60.0, 0)})},
        )
        engine.inject(ev("STARTJOB", 0, job="sa"))
        await engine.run_until_quiescent(T0 + timedelta(minutes=2))
        assert engine.oracle.store.job["sa"].status == "SUCCESS"
        engine._enqueue(
            Event(
                at=T0 + timedelta(minutes=3),
                kind="STATUS",
                payload={"job": "sa", "run_number": 0, "exit_code": 0},
            ),
            is_completion=True,
        )
        await engine.run_until_quiescent(T0 + timedelta(minutes=3))
        assert len(engine.drops) == 1
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    drops = [r for r in read_journal(tmp_path / "run" / "journal.jsonl") if r["rec"] == "drop"]
    assert len(drops) == 1
    assert drops[0]["reason"] == "run_number mismatch"
    assert drops[0]["kind"] == "STATUS"
    assert drops[0]["payload"] == {"job": "sa", "run_number": 0, "exit_code": 0}
    assert "seq" not in drops[0]


# ------------------------------------------------------------- 6. start_run gate


def test_start_run_refuses_an_existing_journal(tmp_path: Path) -> None:
    """(start_run docstring): a run_root that already holds a journal.jsonl
    must be resumed (resume_run), never silently re-baselined."""
    catalog = lower_source(_SOLO_JIL)
    run_root = tmp_path / "run"
    engine = start_run(
        catalog, run_root, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
    )
    assert engine.journal is not None
    engine.journal.close()
    with pytest.raises(EngineError, match="already exists"):
        start_run(catalog, run_root, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()})


# --------------------------------------------------------- 7. `dsl41 journal` CLI


def test_cli_journal_renders_the_trace_when_the_catalog_matches(tmp_path: Path) -> None:
    """(cli.py journal command docstring): replays the WAL through a fresh
    Oracle and prints "{at} {job} {transition} [{cause}]" per trace entry --
    the same information `dsl41.oracle.Oracle.trace()` carries, rendered."""
    text = "insert_job: j1\njob_type: c\ncommand: x\nmachine: m1\n"
    jil_path = tmp_path / "estate.jil"
    jil_path.write_text(text)
    # loaded with file=str(jil_path) so its catalog_hash matches what the CLI
    # computes from the SAME path via parse_file -- SourceSpan.file is part
    # of the hashed model, so a "<memory>" load would mismatch a real one
    catalog = lower_source(text, file=str(jil_path))
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            catalog, run_root, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
        )
        engine.inject(ev("STARTJOB", 0, job="j1"))
        await engine.run_until_quiescent(T0)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    result = CliRunner().invoke(app, ["journal", str(run_root / "journal.jsonl"), str(jil_path)])
    assert result.exit_code == 0
    assert result.output == (
        "2026-07-01T08:00:00 j1 INACTIVE->STARTING [STARTJOB event]\n"
        "2026-07-01T08:00:00 j1 STARTING->RUNNING"
        " [QUE_WAIT collapses to immediate (ss7 non-goal)]\n"
        "2026-07-01T08:00:00 j1 RUNNING->SUCCESS [injected STATUS]\n"
    )


def test_cli_journal_exits_2_on_catalog_hash_mismatch(tmp_path: Path) -> None:
    """(cli.py journal command docstring / runner-design ss7): the estate
    file differs from the one this journal ran -- refuse rather than render
    a trace that silently mixes an old run with new semantics."""
    text = "insert_job: j1\njob_type: c\ncommand: x\nmachine: m1\n"
    jil_path = tmp_path / "estate.jil"
    jil_path.write_text(text)
    catalog = lower_source(text, file=str(jil_path))
    run_root = tmp_path / "run"

    async def scenario() -> None:
        engine = start_run(
            catalog, run_root, clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()}
        )
        engine.inject(ev("STARTJOB", 0, job="j1"))
        await engine.run_until_quiescent(T0)
        await engine.shutdown()
        assert engine.journal is not None
        engine.journal.close()

    asyncio.run(scenario())
    jil_path.write_text(text.replace("command: x", "command: y"))  # the estate drifted
    result = CliRunner().invoke(app, ["journal", str(run_root / "journal.jsonl"), str(jil_path)])
    assert result.exit_code == 2
    assert "catalog hash mismatch" in result.output
