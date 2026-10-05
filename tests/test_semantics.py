"""Semantic switches (runner-design ss8a, DL-252): the closed registry, the
runtime-profile field that records overrides, the hash-neutral spelling of
"no override", the path from the command line to the period's pin, and the
first switch, `ice-lookback`, across the manifest, the engine and replay.

The oracle's own reading of the switch, on both bisimulation paths, is in
`test_oracle.py` (`test_sem20_ice_lookback_switch_selects_the_q10_reading`).
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import hashlib
import json
import re
import typing
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import typer
from pydantic import ValidationError
from typer.testing import CliRunner

from dsl41 import semantics
from dsl41.canon import canonical_bytes
from dsl41.classify import (
    ARMED_ASSUMPTION,
    SWITCH,
    Baseline,
    CarriedJob,
    CarriedState,
    classify,
)
from dsl41.cli import app
from dsl41.ir import LoweringError, lower_source
from dsl41.oracle_state import Event, JobRuntime
from dsl41.period import (
    EMPTY_BUNDLE_HASH,
    RuntimeProfile,
    estate_wal,
    genesis_manifest,
    period_dir,
    read_period_manifest,
    runtime_hash,
    runtime_profile_from_cli,
    stage_manifest,
    switches_of,
    wal_path,
    write_period_manifest,
)
from dsl41.runner import Engine
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_history import RunHistoryError, replay_trace
from dsl41.runner_journal import read_journal
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_startup import (
    _derive_runtime_profile,
    resume_run,
    start_run,
    wire_from_profile,
)
from test_period_identity import GOLDEN_RUNTIME_HASH, _full_profile
from test_runner_leadership import engine

T0 = datetime(2026, 7, 1, 8, 0)
ORDINARY = {"ice-lookback": "ordinary"}
RECHECK = {"queued-recheck": "1"}

#: `qc` queues behind `hq` on LOCK while its condition s(up) holds; `up`
#: then fails, and `hq` ends: the shape where `queued-recheck` 0 and 1
#: disagree (DL-257).
_QUEUED_JIL = (
    "insert_resource: LOCK\nres_type: R\namount: 1\n\n"
    "insert_job: hq\njob_type: c\ncommand: x\nmachine: m1\nresources: (LOCK, QUANTITY=1)\n\n"
    "insert_job: up\njob_type: c\ncommand: x\nmachine: m1\n\n"
    "insert_job: qc\njob_type: c\ncommand: y\nmachine: m1\nresources: (LOCK, QUANTITY=1)\n"
    "condition: s(up)\n"
)

#: An iced producer and a consumer gated on a lookback f() atom: the one
#: shape where the two `ice-lookback` readings disagree.
_ICED_JIL = (
    "insert_job: prod\njob_type: c\ncommand: x\nmachine: m1\nstatus: ON_ICE\n\n"
    "insert_job: cons\njob_type: c\ncommand: y\nmachine: m1\ncondition: f(prod, 0)\n"
)

#: The default profile's canonical bytes, spelled out by hand: `semantics`
#: is present and `{}` like every other empty collection (period-model ss3.2).
_DEFAULT_PROFILE_BYTES = (
    b'{"as_machine":[],"cmd_grace_us":10000000,"deadman_us":null,"default_tz":"UTC",'
    b'"execution_mode":"tethered","fw_default_interval_us":60000000,'
    b'"machine_policy":"strict","reconcile_settle_us":5000000,"retry_horizon_us":60000000,'
    b'"semantics":{},"spawn_window_us":5000000,"tz_aliases":{}}'
)


# ------------------------------------------------------------------ registry


def test_every_registry_entry_is_well_formed() -> None:
    for name, switch in semantics.REGISTRY.items():
        assert name == switch.name
        assert re.fullmatch(r"[a-z]+(-[a-z]+)*", name), name
        assert len(set(switch.values)) == len(switch.values) >= 2, name
        assert switch.default in switch.values, name
        assert switch.autosys in (*switch.values, "unknown"), name
        assert switch.description, name


def test_the_typed_view_has_one_attribute_per_switch() -> None:
    """`SemanticSwitches` and the registry name the same switches, and the
    resolved defaults are the registry's."""
    attributes = {field.name for field in dataclasses.fields(semantics.SemanticSwitches)}
    assert attributes == {name.replace("-", "_") for name in semantics.REGISTRY}
    for name, switch in semantics.REGISTRY.items():
        assert getattr(semantics.DEFAULTS, name.replace("-", "_")) == switch.default
    assert semantics.resolve() == semantics.DEFAULTS
    assert semantics.resolve(ORDINARY).ice_lookback == "ordinary"


def test_each_typed_attribute_holds_its_registry_switch_value_set() -> None:
    """The registry reads each switch's values from its `Literal` (C5/R10),
    so the one hand-kept link left is which alias annotates which
    attribute. An attribute annotated with another switch's alias would
    type-check values the registry refuses."""
    hints = typing.get_type_hints(semantics.SemanticSwitches)
    for name, switch in semantics.REGISTRY.items():
        assert typing.get_args(hints[name.replace("-", "_")]) == switch.values, name


def test_the_ice_lookback_default_is_the_documented_autosys_reading() -> None:
    switch = semantics.REGISTRY["ice-lookback"]
    assert switch.values == ("true", "ordinary")
    assert switch.default == "true" == switch.autosys


def test_the_queued_recheck_default_is_dsl41_s_and_the_vendor_s_is_1() -> None:
    """DL-257: the owner kept dsl41's admission without a recheck as the
    default; the vendor's EvaluateQueuedJobStarts default is 1."""
    switch = semantics.REGISTRY["queued-recheck"]
    assert switch.values == ("0", "1", "2")
    assert switch.default == "0"
    assert switch.autosys == "1"
    assert semantics.resolve(RECHECK).queued_recheck == "1"


def test_queued_recheck_affects_the_jobs_that_can_queue() -> None:
    """A job that names a resource, or whose positive priority makes its
    start check machine load (DL-247), can wait in QUE_WAIT."""
    affects = semantics.REGISTRY["queued-recheck"].affects
    catalog = lower_source(
        "insert_resource: R1\nres_type: R\namount: 1\n\n"
        "insert_job: res\njob_type: c\nmachine: m1\ncommand: x\nresources: (R1, QUANTITY=1)\n\n"
        "insert_job: prio\njob_type: c\nmachine: m1\ncommand: x\npriority: 3\n\n"
        "insert_job: zero\njob_type: c\nmachine: m1\ncommand: x\njob_load: 5\npriority: 0\n\n"
        "insert_job: plain\njob_type: c\nmachine: m1\ncommand: x\njob_load: 5\n"
    )
    assert {name for name, job in catalog.jobs.items() if affects(job, catalog)} == {"res", "prio"}


@pytest.mark.parametrize(
    ("calendar", "message"),
    [
        ("", "calendar 'ex' has no definition in the loaded set"),
        ("calendar: ex\nnot-a-date\n\n", "unparseable date row"),
    ],
)
def test_queued_recheck_refuses_a_calendar_it_cannot_read(calendar: str, message: str) -> None:
    """DL-257: preflight refuses these before a run; an oracle built
    without it raises, naming the job, rather than guess the day."""
    from dsl41.oracle import Oracle
    from dsl41.oracle_state import OracleError

    catalog = lower_source(
        calendar + "insert_resource: LOCK\nres_type: R\namount: 1\n\n"
        "insert_job: hq\njob_type: c\ncommand: x\nmachine: m1\nresources: (LOCK, QUANTITY=1)\n\n"
        "insert_job: xq\njob_type: c\ncommand: y\nmachine: m1\nresources: (LOCK, QUANTITY=1)\n"
        'date_conditions: 1\nstart_times: "08:00"\nexclude_calendar: ex\n'
    )
    oracle = Oracle(catalog, semantics=semantics.resolve(RECHECK))
    oracle.feed(Event(at=T0, kind="STARTJOB", payload={"job": "hq"}))
    oracle.feed(Event(at=T0, kind="STARTJOB", payload={"job": "xq"}))
    with pytest.raises(OracleError, match=f"xq: .*{message}"):
        oracle.feed(Event(at=T0, kind="STATUS", payload={"job": "hq", "status": "SUCCESS"}))


def test_the_dst_start_times_default_is_the_documented_autosys_reading() -> None:
    """DL-260: the vendor's DST rules for start times are documented
    ("Standard Time Changes", "Daylight Time Changes"), so they are the
    default; fold0 keeps dsl41's earlier conversion selectable."""
    switch = semantics.REGISTRY["dst-start-times"]
    assert switch.values == ("vendor", "fold0")
    assert switch.default == "vendor" == switch.autosys
    assert semantics.DEFAULTS.dst_start_times == "vendor"


def test_the_profile_refuses_an_unknown_switch_with_the_known_names() -> None:
    with pytest.raises(ValidationError, match="unknown semantic switch 'ice-lookbak'") as info:
        RuntimeProfile(semantics={"ice-lookbak": "true"})
    assert "known switches: dst-start-times, fw-existence, ice-lookback" in str(info.value)


def test_the_profile_refuses_a_value_outside_the_switch_s_set() -> None:
    with pytest.raises(ValidationError, match="'maybe' is not one of true, ordinary"):
        RuntimeProfile(semantics={"ice-lookback": "maybe"})


def test_the_profile_from_cli_carries_the_overrides() -> None:
    profile = runtime_profile_from_cli(semantics=ORDINARY)
    assert profile.semantics == ORDINARY
    assert switches_of(profile).ice_lookback == "ordinary"
    assert switches_of(None) == semantics.DEFAULTS
    assert switches_of(RuntimeProfile()) == semantics.DEFAULTS


@pytest.mark.parametrize(
    ("words", "message"),
    [
        (["ice-lookback"], "expected NAME=VALUE"),
        (["nope=true"], "known switches: dst-start-times, fw-existence, ice-lookback"),
        (["ice-lookback=yes"], "is not one of true, ordinary"),
        (["ice-lookback=true", "ice-lookback=ordinary"], "is given twice"),
    ],
)
def test_parse_assignments_refuses_what_it_cannot_record(words: list[str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        semantics.parse_assignments(words)


def test_parse_assignments_accepts_a_repeat_of_the_same_value() -> None:
    assert semantics.parse_assignments(["ice-lookback=ordinary"] * 2) == ORDINARY


def test_pr15a_an_explicit_default_is_the_same_profile_as_no_override() -> None:
    """`ice-lookback=true` names the default: one reading, so one profile
    and one hash, and a resume that spells it is not drift."""
    explicit = runtime_profile_from_cli(semantics={"ice-lookback": "true"})
    assert explicit.semantics == {}
    assert explicit == RuntimeProfile()
    assert runtime_hash(explicit) == runtime_hash(RuntimeProfile())
    assert semantics.parse_assignments(["ice-lookback=true"]) == {}


# ------------------------------------------------------- hash and manifest


def test_an_empty_semantics_map_is_written_as_an_empty_object() -> None:
    """ss3.2: a typed field is always present and an empty collection is
    `{}`. The default profile's bytes and hash are pinned by hand, and so is
    the fully populated golden vector."""
    assert canonical_bytes(RuntimeProfile().model_dump(mode="json")) == _DEFAULT_PROFILE_BYTES
    assert runtime_hash(RuntimeProfile()) == (
        "sha256:" + hashlib.sha256(_DEFAULT_PROFILE_BYTES).hexdigest()
    )
    assert runtime_hash(_full_profile()) == GOLDEN_RUNTIME_HASH
    assert json.loads(RuntimeProfile().model_dump_json())["semantics"] == {}


def test_an_override_is_in_the_bytes_and_moves_the_hash() -> None:
    profile = RuntimeProfile(semantics=ORDINARY)
    data = json.loads(canonical_bytes(profile.model_dump(mode="json")))
    assert data["semantics"] == ORDINARY
    assert runtime_hash(profile) != runtime_hash(RuntimeProfile())


def _write_manifest(root: Path, profile: RuntimeProfile) -> None:
    catalog = lower_source(_ICED_JIL)
    manifest = genesis_manifest(
        catalog,
        clock_domain="virtual",
        state_machine_version=STATE_MACHINE_VERSION,
        staged=stage_manifest(
            catalog,
            source_bundle_hash=EMPTY_BUNDLE_HASH,
            profile=profile,
            state_machine_version=STATE_MACHINE_VERSION,
        ),
    )
    write_period_manifest(root, manifest)


@pytest.mark.parametrize("overrides", [{}, ORDINARY])
def test_the_period_manifest_round_trips_the_overrides(
    tmp_path: Path, overrides: dict[str, str]
) -> None:
    """The manifest always writes `semantics`, `{}` when there is none, and
    reads both back to the profile that was written."""
    profile = RuntimeProfile(semantics=overrides)
    _write_manifest(tmp_path, profile)
    raw = json.loads((period_dir(tmp_path, 1) / "manifest.json").read_bytes())
    assert raw["runtime_profile"]["semantics"] == overrides
    manifest = read_period_manifest(tmp_path)
    assert manifest is not None
    assert manifest.runtime_profile == profile
    assert manifest.runtime_hash == runtime_hash(profile)


@pytest.mark.parametrize("field", ["semantics", "default_tz"])
def test_a_manifest_refuses_a_missing_profile_field(tmp_path: Path, field: str) -> None:
    """`semantics` gets no exemption: absent is a defaulted pin, refused
    like any other missing field."""
    _write_manifest(tmp_path, RuntimeProfile())
    path = period_dir(tmp_path, 1) / "manifest.json"
    raw = json.loads(path.read_bytes())
    del raw["runtime_profile"][field]
    path.write_bytes(canonical_bytes(raw))
    with pytest.raises(EngineError, match=f"runtime_profile missing {field}"):
        read_period_manifest(tmp_path)


# ------------------------------------------------- engine, resume and replay


def _start_iced(
    run_root: Path, profile: RuntimeProfile, text: str = _ICED_JIL, *, scheduled: bool = False
):
    from dsl41.boundary import stage_period
    from dsl41.ast_jil import parse

    jil = run_root.parent / "iced.jil"
    jil.write_text(text)
    catalog = lower_source(text, file=str(jil))
    parsed = [parse(text, file=str(jil))]
    run_root.mkdir()
    staged = stage_period(run_root, parsed, catalog, profile)
    return catalog, start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        staged=staged,
        scheduler=Scheduler(
            catalog, start=T0, default_tz=profile.default_tz, tz_aliases=profile.tz_aliases
        )
        if scheduled
        else None,
    )


def _cons_transitions(trace: list) -> list[str]:
    return [t.transition for t in trace if t.job == "cons"]


@pytest.mark.parametrize(("overrides", "starts"), [({}, True), (ORDINARY, False)])
def test_the_engine_runs_the_period_s_pin_and_replay_reads_it_back(
    tmp_path: Path, overrides: dict[str, str], starts: bool
) -> None:
    """The pinned switch reaches the engine's oracle; a resume runs the same
    pin; and the offline replay of the log reproduces the engine's trace,
    which it can only do by reading the switch from the manifest."""
    run_root = tmp_path / "run"
    catalog, live = _start_iced(run_root, RuntimeProfile(semantics=overrides))
    assert live.oracle.semantics == semantics.resolve(overrides)
    live.inject(Event(at=T0, kind="STARTJOB", payload={"job": "cons"}), source="control")

    async def play() -> None:
        try:
            await live.run_until_quiescent(T0 + timedelta(minutes=1))
        finally:
            await live.shutdown()

    asyncio.run(play())
    live.journal.close()
    expected = ["INACTIVE->STARTING", "STARTING->RUNNING"] if starts else []
    assert _cons_transitions(live.oracle.trace()) == expected
    records = read_journal(estate_wal(run_root))
    assert _cons_transitions(replay_trace(run_root, records, catalog).trace) == expected
    resumed = asyncio.run(
        resume_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0 + timedelta(minutes=2)),
            adapters={"CMD": FakeAdapter(default=None)},
        )
    )
    try:
        assert resumed.oracle.semantics == semantics.resolve(overrides)
    finally:
        asyncio.run(resumed.shutdown())
        resumed.journal.close()


def _play(live, script: list[tuple[datetime, str | None, dict[str, str]]]) -> None:
    """Feed `script` to the engine; a step with no kind only advances the
    clock, so the scheduler's ticks up to it fire."""

    async def play() -> None:
        try:
            for at, kind, payload in script:
                if kind is not None:
                    live.inject(Event(at=at, kind=kind, payload=payload), source=None)
                await live.run_until_quiescent(at)
        finally:
            await live.shutdown()

    asyncio.run(play())
    live.journal.close()


#: Wednesday 2026-07-01 23:00 in Zurich is 21:00 UTC; Thursday 00:30 there is
#: 22:30 UTC, still July 1 on the engine clock. July 2 is excluded.
_ZURICH_JIL = (
    "calendar: ex\n07/02/2026 00:00\n\n"
    "insert_resource: LOCK\nres_type: R\namount: 1\n\n"
    "insert_job: hq\njob_type: c\ncommand: x\nmachine: m1\nresources: (LOCK, QUANTITY=1)\n\n"
    "insert_job: xq\njob_type: c\ncommand: y\nmachine: m1\nresources: (LOCK, QUANTITY=1)\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "23:00"\nexclude_calendar: ex\n'
)


@pytest.mark.parametrize(
    ("mode", "xq_end"), [("0", "RUNNING"), ("1", "INACTIVE"), ("2", "INACTIVE")]
)
def test_queued_recheck_reads_today_in_the_base_zone_on_the_engine_and_replay(
    tmp_path: Path, mode: str, xq_end: str
) -> None:
    """DL-257 with DL-253's base zone: a job with no `timezone:` reads
    "today" in the run's base zone, not on the UTC engine clock. Released at
    00:30 Thursday in Zurich, it is on the excluded July 2 although the
    engine clock still says July 1; modes 1 and 2 refuse it, and the replay
    of the log does the same."""
    run_root = tmp_path / "run"
    profile = RuntimeProfile(default_tz="Europe/Zurich", semantics={"queued-recheck": mode})
    catalog, live = _start_iced(run_root, profile, _ZURICH_JIL, scheduled=True)
    _play(
        live,
        [
            # hq takes the lock; the scheduler ticks xq at 23:00 Zurich
            (datetime(2026, 7, 1, 20, 0), "STARTJOB", {"job": "hq"}),
            (datetime(2026, 7, 1, 21, 0), None, {}),
            (datetime(2026, 7, 1, 22, 30), "STATUS", {"job": "hq", "status": "SUCCESS"}),
        ],
    )
    trace = [t.transition for t in live.oracle.trace() if t.job == "xq"]
    assert trace[0] == "INACTIVE->QUE_WAIT"
    assert trace[-1].endswith(xq_end)
    if mode != "0":
        [left] = [t.cause for t in live.oracle.trace() if t.transition == "QUE_WAIT->INACTIVE"]
        assert "2026-07-02 is in exclude_calendar 'ex'" in left
    records = read_journal(estate_wal(run_root))
    replayed = [
        t.transition for t in replay_trace(run_root, records, catalog).trace if t.job == "xq"
    ]
    assert replayed == trace


@pytest.mark.parametrize(("overrides", "qc_end"), [({}, "RUNNING"), (RECHECK, "INACTIVE")])
def test_the_engine_runs_queued_recheck_and_replay_reads_it_back(
    tmp_path: Path, overrides: dict[str, str], qc_end: str
) -> None:
    """DL-257 on the engine: a queued job whose condition went false starts
    under the default and leaves the queue unstarted under 1, and the
    offline replay of the log narrates the same run."""
    run_root = tmp_path / "run"
    catalog, live = _start_iced(run_root, RuntimeProfile(semantics=overrides), _QUEUED_JIL)
    assert live.oracle.semantics == semantics.resolve(overrides)
    script = [
        (0, "STARTJOB", {"job": "hq"}),
        (0, "STARTJOB", {"job": "up"}),
        (1, "STATUS", {"job": "up", "status": "SUCCESS"}),
        (2, "STATUS", {"job": "up", "status": "FAILURE"}),
        (3, "STATUS", {"job": "hq", "status": "SUCCESS"}),
    ]

    async def play() -> None:
        try:
            for minutes, kind, payload in script:
                at = T0 + timedelta(minutes=minutes)
                live.inject(Event(at=at, kind=kind, payload=payload), source=None)
                await live.run_until_quiescent(at)
        finally:
            await live.shutdown()

    asyncio.run(play())
    live.journal.close()
    trace = [t.transition for t in live.oracle.trace() if t.job == "qc"]
    assert trace[0] == "INACTIVE->QUE_WAIT"
    assert trace[-1].endswith(qc_end)
    records = read_journal(estate_wal(run_root))
    replayed = [
        t.transition for t in replay_trace(run_root, records, catalog).trace if t.job == "qc"
    ]
    assert replayed == trace


def test_an_engine_refuses_switches_that_disagree_with_its_pin(tmp_path: Path) -> None:
    from dsl41.runner import Engine

    run_root = tmp_path / "run"
    catalog, live = _start_iced(run_root, RuntimeProfile(semantics=ORDINARY))
    try:
        with pytest.raises(EngineError, match="disagree with the period's pin"):
            Engine(
                catalog,
                clock=VirtualClock(start=T0),
                adapters={"CMD": FakeAdapter(default=None)},
                estate=live.estate,
                semantics=semantics.DEFAULTS,
            )
    finally:
        asyncio.run(live.shutdown())
        live.journal.close()


def test_a_declared_switch_that_moves_off_the_pin_is_profile_drift() -> None:
    """`semantics` is a DECLARED field: a launcher that says nothing
    inherits the pin, and one that names a different value drifts, exactly
    as a changed machine identity does."""
    pinned = RuntimeProfile(semantics=ORDINARY)
    assert _derive_runtime_profile(None, {}, None, pinned).semantics == ORDINARY
    moved = _derive_runtime_profile(None, {}, None, pinned, RuntimeProfile())
    assert moved.semantics == {}


def test_fw_existence_is_derived_from_the_wired_adapter_not_the_pin() -> None:
    """DL-258 fix: `fw-existence` is read back from the wired FW adapter the
    same way `fw_default_interval_us` is, OVER whatever the pin or
    `declared` said -- so a caller that wires a disagreeing adapter shows up
    as drift instead of being silently believed. A declared `ice-lookback`
    override rides alongside it unchanged: the two switches are
    independent."""
    from dsl41.runner_adapters import FileWatcherAdapter

    pinned = RuntimeProfile()  # fw-existence: stable, the default
    immediate_fw = {"FW": FileWatcherAdapter(existence="immediate")}
    assert _derive_runtime_profile(None, immediate_fw, None, pinned).semantics == {
        "fw-existence": "immediate"
    }
    pinned_immediate = RuntimeProfile(semantics={"fw-existence": "immediate"})
    stable_fw = {"FW": FileWatcherAdapter(existence="stable")}
    assert _derive_runtime_profile(None, stable_fw, None, pinned_immediate).semantics == {}
    declared = RuntimeProfile(semantics={"ice-lookback": "ordinary"})
    derived = _derive_runtime_profile(None, immediate_fw, None, pinned, declared)
    assert derived.semantics == {"ice-lookback": "ordinary", "fw-existence": "immediate"}


@pytest.mark.parametrize(
    ("pinned_existence", "wired_existence"),
    [("stable", "immediate"), ("immediate", "stable")],
)
def test_genesis_refuses_a_staged_profile_the_fw_adapter_disagrees_with(
    tmp_path: Path, pinned_existence: str, wired_existence: str
) -> None:
    """DL-258: a staged profile pinning one `fw-existence` reading over an
    FW adapter actually wired the other is a fiction, refused before
    anything is written -- the same gate `_finish_genesis` already runs for
    every other wired field. Both mismatch directions are checked: the one
    that would report SUCCESS too early, and the one that would lose it."""
    from dsl41.runner_adapters import FileWatcherAdapter

    catalog = lower_source("insert_job: w\njob_type: f\nmachine: m1\nwatch_file: /tmp/x\n")
    staged = stage_manifest(
        catalog,
        source_bundle_hash=EMPTY_BUNDLE_HASH,
        profile=RuntimeProfile(semantics={"fw-existence": pinned_existence}),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    with pytest.raises(EngineError, match="disagrees with the engine's wiring"):
        start_run(
            catalog,
            tmp_path / "run",
            clock=VirtualClock(start=T0),
            adapters={"FW": FileWatcherAdapter(existence=wired_existence)},
            staged=staged,
        )


@pytest.mark.parametrize(
    ("pinned_existence", "wired_existence"),
    [("stable", "immediate"), ("immediate", "stable")],
)
def test_resume_refuses_an_fw_adapter_disagreeing_with_the_pin(
    tmp_path: Path, pinned_existence: str, wired_existence: str
) -> None:
    """DL-258: the bug this fixes -- `resume_run` with an adapter whose
    `existence` contradicts the pin used to be accepted silently, because
    `_derive_runtime_profile` echoed the pin's `semantics` back unchanged.
    Now the runtime-profile drift gate catches it, before reconciliation
    runs (no durable write)."""
    from dsl41.runner_adapters import FileWatcherAdapter

    run_root = tmp_path / "run"
    catalog = lower_source("insert_job: w\njob_type: f\nmachine: m1\nwatch_file: /tmp/x\n")
    engine = start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"FW": FileWatcherAdapter(existence=pinned_existence)},
    )
    asyncio.run(engine.shutdown())
    engine.journal.close()

    with pytest.raises(EngineError, match="runtime-profile mismatch on semantics"):
        asyncio.run(
            resume_run(
                catalog,
                run_root,
                clock=VirtualClock(start=T0),
                adapters={"FW": FileWatcherAdapter(existence=wired_existence)},
            )
        )


def test_a_resume_with_a_different_switch_refuses(tmp_path: Path) -> None:
    from dsl41.cli_run import _resume_profile_error

    _write_manifest(tmp_path, runtime_profile_from_cli(semantics=ORDINARY))
    refused = _resume_profile_error(tmp_path, runtime_profile_from_cli(), None)
    assert refused is not None and "runtime-profile mismatch" in refused
    assert "semantics" in refused
    assert (
        _resume_profile_error(tmp_path, runtime_profile_from_cli(semantics=ORDINARY), None) is None
    )


def _switch_only_boundary(estate: str, carried: dict[str, CarriedJob]):
    """C1 and C2 are the same catalog; only `ice-lookback` flips."""
    catalog = lower_source(estate)
    return classify(
        closing=Baseline(catalog=catalog, profile=RuntimeProfile()),
        opening=Baseline(catalog=catalog, profile=RuntimeProfile(semantics=ORDINARY)),
        carried=CarriedState(jobs=carried, now=T0),
    )


_ICED = CarriedJob(row=JobRuntime(status="INACTIVE", on_ice=True))
_SWITCH_NODE = SWITCH + "ice-lookback"


def test_a_switch_flip_refuses_a_running_box_whose_success_reads_a_lookback_atom() -> None:
    """ss10.2, DL-252: under `true` the box completes SUCCESS once `m`
    succeeds (f(x, 0) on the iced `x` reads true); under `ordinary` it stays
    RUNNING. The run in flight changes, so the boundary refuses it, while a
    job with no lookback atom is untouched."""
    estate = (
        "insert_job: x\njob_type: c\nmachine: m1\ncommand: x\n\n"
        "insert_job: bx\njob_type: b\nbox_success: s(m) & f(x, 0)\n\n"
        "insert_job: m\njob_type: c\nmachine: m1\ncommand: y\nbox_name: bx\n"
    )
    result = _switch_only_boundary(
        estate,
        {
            "x": _ICED,
            "bx": CarriedJob(row=JobRuntime(status="RUNNING", status_at=T0)),
            "m": CarriedJob(row=JobRuntime(status="RUNNING", status_at=T0)),
        },
    )
    assert _SWITCH_NODE in result.changed_nodes
    assert result.by_job["bx"].verdict == "R"
    assert _SWITCH_NODE in result.by_job["bx"].changed
    assert result.by_job["x"].verdict == "carry"
    assert result.by_job["x"].changed == ()


def test_a_switch_flip_carries_an_armed_job_with_the_armed_assumption() -> None:
    """ss10.3, DL-252: an armed job gated on f(a, 0) & s(g), `a` iced, reads
    its gate true under `true` and false under `ordinary`. It is A with the
    armed assumption, and the boundary-truth diff reports the flip although
    nothing in the catalog moved."""
    estate = (
        "insert_job: a\njob_type: c\nmachine: m1\ncommand: x\n\n"
        "insert_job: g\njob_type: c\nmachine: m1\ncommand: y\n\n"
        "insert_job: j\njob_type: c\nmachine: m1\ncommand: z\ncondition: f(a, 0) & s(g)\n"
    )
    result = _switch_only_boundary(
        estate,
        {
            "a": _ICED,
            "g": CarriedJob(row=JobRuntime(status="SUCCESS", status_at=T0, last_end_at=T0)),
            "j": CarriedJob(row=JobRuntime(status="INACTIVE", armed=True)),
        },
    )
    verdict = result.by_job["j"]
    assert verdict.verdict == "A"
    assert verdict.assumption == ARMED_ASSUMPTION
    assert verdict.changed == (_SWITCH_NODE,)
    assert [(f.job, f.before, f.after) for f in result.readiness_flips] == [("j", True, False)]


def test_a_queued_recheck_flip_carries_a_queued_job_and_refuses_its_running_box() -> None:
    """ss10.2, DL-257: a flip of `queued-recheck` changes whether a queued
    job starts when it leaves the queue. A standalone QUE_WAIT row is latent
    intent, so it is A; a running box with a queued member is R, since the
    box depends on its members; a job that cannot queue is untouched."""
    estate = (
        "insert_resource: LOCK\nres_type: R\namount: 1\n\n"
        "insert_job: q\njob_type: c\nmachine: m1\ncommand: x\nresources: (LOCK, QUANTITY=1)\n\n"
        "insert_job: bx\njob_type: b\n\n"
        "insert_job: m\njob_type: c\nmachine: m1\ncommand: y\nbox_name: bx\n"
        "resources: (LOCK, QUANTITY=1)\n\n"
        "insert_job: other\njob_type: c\nmachine: m1\ncommand: z\n"
    )
    catalog = lower_source(estate)
    result = classify(
        closing=Baseline(catalog=catalog, profile=RuntimeProfile()),
        opening=Baseline(catalog=catalog, profile=RuntimeProfile(semantics=RECHECK)),
        carried=CarriedState(
            jobs={
                "q": CarriedJob(row=JobRuntime(status="QUE_WAIT", status_at=T0, waiter_seq=1)),
                "bx": CarriedJob(row=JobRuntime(status="RUNNING", status_at=T0)),
                "m": CarriedJob(row=JobRuntime(status="QUE_WAIT", status_at=T0, waiter_seq=2)),
                "other": CarriedJob(row=JobRuntime(status="RUNNING", status_at=T0)),
            },
            now=T0,
        ),
    )
    node = SWITCH + "queued-recheck"
    assert node in result.changed_nodes
    assert result.by_job["q"].verdict == "A"
    assert result.by_job["q"].changed == (node,)
    assert result.by_job["bx"].verdict == "R"
    assert node in result.by_job["bx"].changed
    assert result.by_job["other"].verdict == "carry"
    assert result.by_job["other"].changed == ()


def test_ice_lookback_affects_exactly_the_jobs_with_a_lookback_atom() -> None:
    affects = semantics.REGISTRY["ice-lookback"].affects
    catalog = lower_source(
        "insert_job: a\njob_type: c\nmachine: m1\ncommand: x\n\n"
        "insert_job: plain\njob_type: c\nmachine: m1\ncommand: y\ncondition: f(a)\n\n"
        "insert_job: lb\njob_type: c\nmachine: m1\ncommand: y\ncondition: e(a, 01.00) = 0\n\n"
        "insert_job: bf\njob_type: b\nbox_failure: t(a, 0)\n"
    )
    assert {name for name, job in catalog.jobs.items() if affects(job, catalog)} == {"lb", "bf"}


def test_renewable_free_affects_exactly_the_renewable_requests_without_free() -> None:
    """DL-256: an omitted FREE on a renewable resource, an absent res_type
    included, is what `renewable-free` reads. An explicit FREE, a depletable
    and a threshold are outside it."""
    affects = semantics.REGISTRY["renewable-free"].affects
    catalog = lower_source(
        "insert_resource: R1\nres_type: R\namount: 2\n\n"
        "insert_resource: U1\namount: 2\n\n"
        "insert_resource: D1\nres_type: D\namount: 2\n\n"
        "insert_resource: T1\nres_type: T\namount: 2\n\n"
        "insert_job: r\njob_type: c\nmachine: m1\ncommand: x\nresources: (R1, QUANTITY=1)\n\n"
        "insert_job: u\njob_type: c\nmachine: m1\ncommand: x\nresources: (U1, QUANTITY=1)\n\n"
        "insert_job: rf\njob_type: c\nmachine: m1\ncommand: x\n"
        "resources: (R1, QUANTITY=1, FREE=Y)\n\n"
        "insert_job: d\njob_type: c\nmachine: m1\ncommand: x\nresources: (D1, QUANTITY=1)\n\n"
        "insert_job: t\njob_type: c\nmachine: m1\ncommand: x\nresources: (T1, QUANTITY=1)\n\n"
        "insert_job: none\njob_type: c\nmachine: m1\ncommand: x\n"
    )
    assert {name for name, job in catalog.jobs.items() if affects(job, catalog)} == {"r", "u"}


# --------------------------------------------------------------- fw-existence

_FW_ESTATE = (
    "insert_job: w\njob_type: f\nmachine: m1\nwatch_file: /tmp/x\n\n"
    "insert_job: wz\njob_type: f\nmachine: m1\nwatch_file: /tmp/y\nwatch_file_min_size: 0\n\n"
    "insert_job: wm\njob_type: f\nmachine: m1\nwatch_file: /tmp/z\nwatch_file_min_size: 10\n\n"
    "insert_job: plain\njob_type: c\nmachine: m1\ncommand: x\n"
)


def test_fw_no_min_size_is_true_only_for_an_fw_job_with_no_minimum_size() -> None:
    """DL-258: `None` and an explicit `0` read the same -- the adapter's own
    `spec_ir.watch_file_min_size or 0` -- and a non-FW job is never affected."""
    affects = semantics.REGISTRY["fw-existence"].affects
    catalog = lower_source(_FW_ESTATE)
    assert {name for name, job in catalog.jobs.items() if affects(job, catalog)} == {"w", "wz"}


def test_fw_existence_immediate_needs_both_the_switch_and_no_minimum_size() -> None:
    catalog = lower_source(_FW_ESTATE)
    w, wz, wm = catalog.jobs["w"], catalog.jobs["wz"], catalog.jobs["wm"]
    assert semantics.fw_existence_immediate(w, "immediate") is True
    assert semantics.fw_existence_immediate(wz, "immediate") is True
    assert semantics.fw_existence_immediate(w, "stable") is False
    assert semantics.fw_existence_immediate(wm, "immediate") is False
    assert semantics.fw_existence_immediate(wm, "stable") is False


def test_a_fw_existence_flip_reaches_exactly_the_no_min_size_fw_jobs() -> None:
    """ss10.2: the switch's own node reaches an FW job with no
    `watch_file_min_size` and not one with a minimum size -- the same
    mechanism `ice-lookback`'s node already proves above."""
    catalog = lower_source(_FW_ESTATE)
    result = classify(
        closing=Baseline(catalog=catalog, profile=RuntimeProfile()),
        opening=Baseline(
            catalog=catalog, profile=RuntimeProfile(semantics={"fw-existence": "immediate"})
        ),
        carried=CarriedState(
            jobs={
                name: CarriedJob(row=JobRuntime(status="INACTIVE"))
                for name in ("w", "wz", "wm", "plain")
            },
            now=T0,
        ),
    )
    node = SWITCH + "fw-existence"
    assert node in result.changed_nodes
    assert result.by_job["w"].changed == (node,)
    assert result.by_job["wz"].changed == (node,)
    assert result.by_job["wm"].changed == ()
    assert result.by_job["plain"].changed == ()


def test_fw_existence_reaches_the_adapter_from_the_pinned_profile(tmp_path: Path) -> None:
    """DL-258: `fw-existence` reaches `FileWatcherAdapter` the same way
    `fw_default_interval_us` does, through `wire_from_profile` -- not a
    second read of the profile inside the adapter."""
    from dsl41.runner_adapters import FileWatcherAdapter
    from dsl41.runner_startup import wire_from_profile

    catalog = lower_source("insert_job: w\njob_type: f\nmachine: m1\nwatch_file: /tmp/x\n")

    async def wire(profile: RuntimeProfile) -> str:
        wiring = await wire_from_profile(tmp_path, catalog, profile, start=T0)
        try:
            fw = wiring.adapters["FW"]
            assert isinstance(fw, FileWatcherAdapter)
            return fw.existence
        finally:
            await wiring.close()

    assert asyncio.run(wire(RuntimeProfile(semantics={"fw-existence": "immediate"}))) == "immediate"
    assert asyncio.run(wire(RuntimeProfile())) == "stable"


# ------------------------------------------------ wekr-first-week (DL-259)

PARTIAL = {"wekr-first-week": "partial"}
BOTH = {"ice-lookback": "ordinary", "wekr-first-week": "partial"}

#: Two calendared jobs, one on a WEKR calendar and one on a plain one.
_WEKR_JIL = (
    "extended_calendar: wk\ncondition: WEKR1#02\n\n"
    "extended_calendar: plain\ncondition: MON\n\n"
    "insert_job: wj\njob_type: c\nmachine: m1\ncommand: x\n"
    'date_conditions: 1\nrun_calendar: wk\nstart_times: "08:00"\n\n'
    "insert_job: pj\njob_type: c\nmachine: m1\ncommand: y\n"
    'date_conditions: 1\nrun_calendar: plain\nstart_times: "08:00"\n'
)


def test_the_wekr_default_is_dsl41_s_first_full_week() -> None:
    switch = semantics.REGISTRY["wekr-first-week"]
    assert switch.values == ("first-full", "partial")
    assert switch.default == "first-full"
    assert switch.autosys == "unknown"


def test_wekr_first_week_affects_exactly_the_calendars_with_a_wekr_token() -> None:
    entry = semantics.REGISTRY["wekr-first-week"]
    catalog = lower_source(_WEKR_JIL)
    assert {name for name, cal in catalog.calendars.items() if entry.affects_calendar(cal)} == {
        "wk"
    }
    assert not any(entry.affects(job, catalog) for job in catalog.jobs.values())
    ice = semantics.REGISTRY["ice-lookback"]
    assert not any(ice.affects_calendar(cal) for cal in catalog.calendars.values())


def test_a_wekr_switch_flip_refuses_a_running_job_on_a_wekr_calendar() -> None:
    """ss10.2, DL-259: the flip moves `wk`'s week 2 from January 13-19 2014
    to January 6-12, so the job on it reaches the switch through its
    calendar and a running one is refused. The job on a calendar with no
    WEKR token does not reach the switch and carries."""
    catalog = lower_source(_WEKR_JIL)
    running = CarriedJob(row=JobRuntime(status="RUNNING", status_at=T0))
    result = classify(
        closing=Baseline(catalog=catalog, profile=RuntimeProfile()),
        opening=Baseline(catalog=catalog, profile=RuntimeProfile(semantics=PARTIAL)),
        carried=CarriedState(jobs={"wj": running, "pj": running}, now=T0),
    )
    node = SWITCH + "wekr-first-week"
    assert node in result.changed_nodes
    assert result.by_job["wj"].verdict == "R"
    assert result.by_job["wj"].changed == (node,)
    assert result.by_job["pj"].verdict == "carry"
    assert result.by_job["pj"].changed == ()


def test_a_wekr_respelling_across_a_boundary_carries_a_running_job() -> None:
    """`WEKRMon#02` and `WEKR1#2` are one calendar (DL-259), so a boundary
    that only respells the anchor and the padding moves nothing."""
    running = CarriedJob(row=JobRuntime(status="RUNNING", status_at=T0))
    result = classify(
        closing=Baseline(catalog=lower_source(_WEKR_JIL), profile=RuntimeProfile()),
        opening=Baseline(
            catalog=lower_source(_WEKR_JIL.replace("WEKR1#02", "WEKRMon#2")),
            profile=RuntimeProfile(),
        ),
        carried=CarriedState(jobs={"wj": running}, now=T0),
    )
    assert result.by_job["wj"].verdict == "carry"
    assert "calendar:wk" not in result.changed_nodes


def test_the_scheduler_compiles_wekr_calendars_under_its_switches() -> None:
    """The scheduler's first tick for WEKR1#02 in 2014: Monday January 13
    under the default, Monday January 6 under `partial`."""
    catalog = lower_source(_WEKR_JIL)
    start = datetime(2014, 1, 1)
    first = {}
    for switches in (None, PARTIAL):
        scheduler = Scheduler(catalog, start=start, semantics=semantics.resolve(switches))
        first[str(switches)] = min(t for t, job in scheduler.upcoming() if job == "wj")
    assert first[str(None)] == datetime(2014, 1, 13, 8, 0)
    assert first[str(PARTIAL)] == datetime(2014, 1, 6, 8, 0)


def test_an_engine_refuses_a_scheduler_built_under_other_calendar_switches() -> None:
    """The scheduler fires what its calendars compiled to, and the engine
    records its own switches: the calendar switches must be one reading
    (DL-259). A switch the scheduler does not read is not compared."""
    catalog = lower_source(_WEKR_JIL)
    scheduler = Scheduler(catalog, start=T0, semantics=semantics.resolve(PARTIAL))
    with pytest.raises(EngineError, match="wekr-first-week=partial, the engine runs first-full"):
        Engine(catalog, clock=VirtualClock(T0), adapters={}, scheduler=scheduler)
    Engine(
        catalog,
        clock=VirtualClock(T0),
        adapters={},
        scheduler=scheduler,
        semantics=semantics.resolve(PARTIAL),
    )
    Engine(
        catalog,
        clock=VirtualClock(T0),
        adapters={},
        scheduler=Scheduler(catalog, start=T0),
        semantics=semantics.resolve(ORDINARY),
    )


def test_the_adapter_switches_are_the_ones_the_fw_adapter_reads() -> None:
    """DL-258, DL-280: the tuple the profile reads back from a wired adapter
    is derived from the registry, and every switch in it is one
    `adapter_switch` can read from a wired `FileWatcherAdapter`; with no
    such adapter there is nothing to read."""
    from dsl41.runner import adapter_switch
    from dsl41.runner_adapters import FileWatcherAdapter

    assert semantics.ADAPTER_SWITCHES == ("fw-existence",)
    wired = {"FW": FileWatcherAdapter(existence="immediate")}
    for name in semantics.ADAPTER_SWITCHES:
        assert adapter_switch(wired, name) == "immediate", name
        assert adapter_switch({"CMD": FakeAdapter(default=None)}, name) is None, name
    assert adapter_switch(wired, "ice-lookback") is None


def test_an_adapter_switch_with_no_read_is_a_code_bug(monkeypatch: pytest.MonkeyPatch) -> None:
    """DL-280: a registry entry gaining an adapter reader without a read in
    `adapter_switch` raises, rather than being skipped by the read-back and
    the backstop."""
    from dsl41 import runner

    monkeypatch.setattr(runner, "ADAPTER_SWITCHES", (*runner.ADAPTER_SWITCHES, "ice-lookback"))
    with pytest.raises(ValueError, match="'ice-lookback' has no read"):
        runner.adapter_switch({}, "ice-lookback")


def test_an_engine_refuses_an_fw_adapter_built_under_another_fw_existence() -> None:
    """The backstop `_engine_switches` holds for the scheduler holds for
    the adapters too (DL-280), once the engine is given a reading: a pin or
    `semantics`. An engine with neither has none to disagree with, so a
    harness that wires an `immediate` adapter alone runs as before."""
    from dsl41.runner_adapters import FileWatcherAdapter

    catalog = lower_source("insert_job: w\njob_type: f\nmachine: m1\nwatch_file: /tmp/x\n")
    immediate = {"FW": FileWatcherAdapter(existence="immediate")}
    with pytest.raises(EngineError, match="fw-existence=immediate, the engine runs stable"):
        Engine(catalog, clock=VirtualClock(T0), adapters=immediate, semantics=semantics.DEFAULTS)
    Engine(
        catalog,
        clock=VirtualClock(T0),
        adapters=immediate,
        semantics=semantics.resolve({"fw-existence": "immediate"}),
    )
    Engine(catalog, clock=VirtualClock(T0), adapters=immediate)


def _wekr_genesis(run_root: Path, scheduler_switches: dict[str, str] | None, staged=None):
    catalog = lower_source(_WEKR_JIL)
    scheduler = Scheduler(catalog, start=T0, semantics=semantics.resolve(scheduler_switches))
    return catalog, start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=scheduler,
        staged=staged,
    )


def _close(live) -> None:
    asyncio.run(live.shutdown())
    live.journal.close()


def test_genesis_pins_the_calendar_switch_its_scheduler_compiled_under(tmp_path: Path) -> None:
    """DL-259: with no staged manifest, genesis reads the calendar switches
    back from the wired scheduler, like its timezone. It used to pin the
    default, write the manifest and the log, and only then refuse in the
    engine, so a retry met an existing log."""
    run_root = tmp_path / "run"
    _catalog, live = _wekr_genesis(run_root, PARTIAL)
    try:
        assert live.oracle.semantics.wekr_first_week == "partial"
    finally:
        _close(live)
    manifest = read_period_manifest(run_root)
    assert manifest is not None
    assert manifest.runtime_profile.semantics == PARTIAL


def test_a_staged_profile_that_disagrees_with_the_scheduler_writes_nothing(
    tmp_path: Path,
) -> None:
    """A staged default pin over a `partial` scheduler is profile drift,
    refused before the manifest and the log exist; a retry with the right
    pin then opens the period."""
    run_root = tmp_path / "run"
    catalog = lower_source(_WEKR_JIL)

    def staged(profile: RuntimeProfile):
        return stage_manifest(
            catalog,
            source_bundle_hash=EMPTY_BUNDLE_HASH,
            profile=profile,
            state_machine_version=STATE_MACHINE_VERSION,
        )

    with pytest.raises(EngineError, match="disagrees with the engine's wiring on semantics"):
        _wekr_genesis(run_root, PARTIAL, staged(RuntimeProfile()))
    assert not (period_dir(run_root, 1) / "manifest.json").exists()
    assert not wal_path(run_root, 1).exists()
    _catalog, live = _wekr_genesis(run_root, PARTIAL, staged(RuntimeProfile(semantics=PARTIAL)))
    _close(live)
    manifest = read_period_manifest(run_root)
    assert manifest is not None and manifest.runtime_profile.semantics == PARTIAL


def test_a_resume_with_a_scheduler_off_the_pin_refuses_before_the_leader_record(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "run"
    catalog, live = _wekr_genesis(run_root, None)
    _close(live)
    before = read_journal(estate_wal(run_root))
    with pytest.raises(EngineError, match="runtime-profile mismatch on semantics"):
        asyncio.run(
            resume_run(
                catalog,
                run_root,
                clock=VirtualClock(start=T0 + timedelta(minutes=1)),
                adapters={"CMD": FakeAdapter(default=None)},
                scheduler=Scheduler(catalog, start=T0, semantics=semantics.resolve(PARTIAL)),
            )
        )
    assert read_journal(estate_wal(run_root)) == before


def test_run_without_a_staged_bundle_wires_and_pins_the_calendar_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_serve_run` with no parsed sources stages nothing; `wire_from_profile`
    builds the scheduler under the profile's switches, and genesis pins
    what it built."""
    from dsl41 import runner_startup
    from dsl41.cli_run import _serve_run

    real_start_run = runner_startup.start_run
    seen: dict[str, object] = {}

    def genesis_then_stop(*args: object, **kwargs: object):
        live = real_start_run(*args, **kwargs)  # type: ignore[arg-type]
        seen["scheduler"] = live.scheduler.semantics
        seen["engine"] = live.oracle.semantics
        live.journal.close()
        raise EngineError("pin test: stop before the loop")

    monkeypatch.setattr(runner_startup, "start_run", genesis_then_stop)
    run_root = tmp_path / "root"
    profile = RuntimeProfile(semantics=BOTH)
    catalog = lower_source(_WEKR_JIL + "\n" + _ICED_JIL)
    with pytest.raises(EngineError, match="stop before the loop"):
        asyncio.run(_serve_run(catalog, run_root, False, [], profile=profile))
    assert seen == {"scheduler": semantics.resolve(BOTH), "engine": semantics.resolve(BOTH)}
    manifest = read_period_manifest(run_root)
    assert manifest is not None and manifest.runtime_profile.semantics == BOTH


@pytest.mark.parametrize("claimed", [False, True])
def test_a_rehearse_root_pins_every_switch_staged_or_not(tmp_path: Path, claimed: bool) -> None:
    """A fresh root stages the launch options; a root holding only the
    sentinel stages nothing, and genesis pins the launch options instead.
    Either way both switches are pinned and the oracle runs `ordinary`:
    the iced producer's f(prod, 0) reads false and `cons` never starts.
    The unstaged path used to pin only the calendar switch."""
    from dsl41.boundary import claim_root

    run_root = tmp_path / "run"
    if claimed:
        run_root.mkdir()
        claim_root(run_root)
    result = _rehearse(
        tmp_path,
        "--semantics",
        "ice-lookback=ordinary",
        "--semantics",
        "wekr-first-week=partial",
        "--run-root",
        str(run_root),
    )
    assert result.exit_code == 0, result.output
    assert "cons runs=0" in result.stdout
    manifest = read_period_manifest(run_root)
    assert manifest is not None and manifest.runtime_profile.semantics == BOTH


def test_a_rehearse_rerun_over_a_claimed_root_pins_the_calendar_switch(tmp_path: Path) -> None:
    """A root holding only the genesis sentinel (a crash before the log) is
    not unused, so the rehearsal stages nothing and genesis completes it.
    The pin is the calendar switch the rehearsal's scheduler runs."""
    from dsl41.boundary import claim_root

    run_root = tmp_path / "run"
    run_root.mkdir()
    claim_root(run_root)
    jil = tmp_path / "wekr.jil"
    jil.write_text(_WEKR_JIL)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"events": []}))
    result = CliRunner().invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--scenario",
            str(scenario),
            "--start",
            "2014-01-06T00:00:00",
            "--hours",
            "12",
            "--format",
            "summary",
            "--semantics",
            "wekr-first-week=partial",
            "--run-root",
            str(run_root),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "wj runs=1" in result.stdout  # January 6 2014 is in week 2 under `partial`
    manifest = read_period_manifest(run_root)
    assert manifest is not None and manifest.runtime_profile.semantics == PARTIAL


def test_dst_start_times_affects_exactly_the_jobs_with_start_times_or_start_mins() -> None:
    """DL-260: a flip moves the ticks of every job with start_times or
    start_mins, whatever its zone; a calendar-only job and an unscheduled
    one keep theirs."""
    affects = semantics.REGISTRY["dst-start-times"].affects
    catalog = lower_source(
        "insert_job: st\njob_type: c\nmachine: m1\ncommand: x\n"
        'date_conditions: 1\nstart_times: "02:05"\n\n'
        "insert_job: sm\njob_type: c\nmachine: m1\ncommand: x\n"
        "date_conditions: 1\nstart_mins: 5\ntimezone: UTC\n\n"
        "insert_job: cal\njob_type: c\nmachine: m1\ncommand: x\n"
        "date_conditions: 1\nrun_calendar: c1\n\n"
        "insert_job: plain\njob_type: c\nmachine: m1\ncommand: y\n\n"
        "calendar: c1\n03/08/2026\n"
    )
    assert {name for name, job in catalog.jobs.items() if affects(job, catalog)} == {"st", "sm"}


def test_a_dst_start_times_flip_refuses_a_running_job_and_carries_a_quiet_one() -> None:
    """DL-260: a flip changes where a job's live ticks land, so a running
    job it reaches is refused (R), as a base-zone change does -- not the
    recorded-assumption reading. The same job, not live, only carries the
    changed switch node."""
    catalog = lower_source(
        "insert_job: st\njob_type: c\nmachine: m1\ncommand: x\n"
        'date_conditions: 1\nstart_times: "02:05"\ntimezone: America/New_York\n'
    )
    closing = Baseline(catalog=catalog, profile=RuntimeProfile())
    opening = Baseline(
        catalog=catalog, profile=RuntimeProfile(semantics={"dst-start-times": "fold0"})
    )
    node = SWITCH + "dst-start-times"

    running = classify(
        closing=closing,
        opening=opening,
        carried=CarriedState(
            jobs={"st": CarriedJob(row=JobRuntime(status="RUNNING", status_at=T0))}, now=T0
        ),
    )
    assert running.by_job["st"].verdict == "R"
    assert node in running.by_job["st"].changed

    quiet = classify(
        closing=closing,
        opening=opening,
        carried=CarriedState(jobs={"st": CarriedJob(row=JobRuntime(status="INACTIVE"))}, now=T0),
    )
    assert quiet.by_job["st"].verdict != "R"
    assert node in quiet.by_job["st"].changed


def test_wire_from_profile_builds_the_scheduler_under_the_profile_s_switch(
    tmp_path: Path,
) -> None:
    """DL-260: `run`, its resume and the offline sealer build the scheduler
    here, so it reads the period's dst-start-times value, as the engine's
    oracle does."""
    catalog = lower_source(
        "insert_job: dj\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\nstart_times: "02:05"\n'
    )

    async def wired(profile: RuntimeProfile) -> str:
        wiring = await wire_from_profile(tmp_path, catalog, profile, start=T0)
        await wiring.close()
        assert wiring.scheduler is not None
        return wiring.scheduler.semantics.dst_start_times

    assert asyncio.run(wired(RuntimeProfile())) == "vendor"
    fold0 = runtime_profile_from_cli(semantics={"dst-start-times": "fold0"})
    assert asyncio.run(wired(fold0)) == "fold0"


# ------------------------------------------------------------------ the CLI


def _rehearse(tmp_path: Path, *extra: str) -> object:
    jil = tmp_path / "iced.jil"
    jil.write_text(_ICED_JIL)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(
        json.dumps(
            {
                "events": [
                    {"at": "2026-07-01T08:00:00", "kind": "STARTJOB", "payload": {"job": "cons"}}
                ]
            }
        )
    )
    return CliRunner().invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--scenario",
            str(scenario),
            "--start",
            "2026-07-01T08:00:00",
            "--hours",
            "1",
            "--format",
            "summary",
            *extra,
        ],
    )


def test_rehearse_reads_the_switch_and_records_it(tmp_path: Path) -> None:
    default = _rehearse(tmp_path)
    assert default.exit_code == 0, default.output
    assert "cons runs=1" in default.stdout
    ordinary = _rehearse(tmp_path, "--semantics", "ice-lookback=ordinary")
    assert ordinary.exit_code == 0, ordinary.output
    assert "cons runs=0" in ordinary.stdout
    run_root = tmp_path / "run"
    recorded = _rehearse(
        tmp_path, "--semantics", "ice-lookback=ordinary", "--run-root", str(run_root)
    )
    assert recorded.exit_code == 0, recorded.output
    assert "cons runs=0" in recorded.stdout
    manifest = read_period_manifest(run_root)
    assert manifest is not None
    assert manifest.runtime_profile.semantics == ORDINARY
    # `dsl41 journal` replays the log under the recorded switch, so it
    # narrates the run the engine made: cons never started
    replayed = CliRunner().invoke(app, ["journal", str(run_root)])
    assert replayed.exit_code == 0, replayed.output
    assert "cons INACTIVE->STARTING" not in replayed.stdout


_DST_JIL = (
    "insert_job: dj\njob_type: c\ncommand: x\nmachine: m1\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "02:05, 02:25"\n'
    "timezone: America/New_York\n"
)


FOLD0 = {"dst-start-times": "fold0"}


def _dst_genesis(run_root: Path, scheduler_switches: dict[str, str] | None, staged=None):
    catalog = lower_source(_DST_JIL)
    scheduler = Scheduler(catalog, start=T0, semantics=semantics.resolve(scheduler_switches))
    return catalog, start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=scheduler,
        staged=staged,
    )


def test_the_scheduler_switches_are_the_calendar_ones_and_dst_start_times() -> None:
    """DL-260: the tuple the profile reads back from a scheduler is derived
    from the registry, not kept by hand."""
    assert set(semantics.SCHEDULER_SWITCHES) == {"wekr-first-week", "dst-start-times"}
    assert set(semantics.CALENDAR_SWITCHES) == {"wekr-first-week"}


@pytest.mark.parametrize(
    ("scheduler_switches", "pinned"), [(FOLD0, FOLD0), (None, {})], ids=["fold0", "vendor"]
)
def test_unstaged_genesis_pins_the_dst_start_times_its_scheduler_compiled_under(
    tmp_path: Path, scheduler_switches: dict[str, str] | None, pinned: dict[str, str]
) -> None:
    """DL-260: with no staged manifest, genesis reads `dst-start-times` back
    from the wired scheduler, so the pin names the reading that ticks."""
    run_root = tmp_path / "run"
    _catalog, live = _dst_genesis(run_root, scheduler_switches)
    try:
        assert live.oracle.semantics == semantics.resolve(scheduler_switches)
    finally:
        _close(live)
    manifest = read_period_manifest(run_root)
    assert manifest is not None
    assert manifest.runtime_profile.semantics == pinned


@pytest.mark.parametrize("staged_switches", [{}, FOLD0], ids=["vendor-pin", "fold0-pin"])
def test_a_staged_dst_pin_off_the_scheduler_writes_nothing(
    tmp_path: Path, staged_switches: dict[str, str]
) -> None:
    """DL-260, period-model PR-22b: a staged vendor pin over a fold0
    scheduler is profile drift, refused before the manifest and the log
    exist. It used to pass the drift gate, write both, and only then be
    refused in the engine. The twin, a staged fold0 pin, opens."""
    run_root = tmp_path / "run"
    catalog = lower_source(_DST_JIL)
    staged = stage_manifest(
        catalog,
        source_bundle_hash=EMPTY_BUNDLE_HASH,
        profile=RuntimeProfile(semantics=staged_switches),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    if staged_switches != FOLD0:
        with pytest.raises(EngineError, match="disagrees with the engine's wiring on semantics"):
            _dst_genesis(run_root, FOLD0, staged)
        assert not (period_dir(run_root, 1) / "manifest.json").exists()
        assert not wal_path(run_root, 1).exists()
        return
    _catalog, live = _dst_genesis(run_root, FOLD0, staged)
    _close(live)
    manifest = read_period_manifest(run_root)
    assert manifest is not None and manifest.runtime_profile.semantics == FOLD0


@pytest.mark.parametrize("resume_switches", [None, FOLD0], ids=["vendor-scheduler", "fold0"])
def test_a_resume_with_a_scheduler_off_the_dst_pin_writes_nothing(
    tmp_path: Path, resume_switches: dict[str, str] | None
) -> None:
    """DL-260, period-model PR-22b: a period pinned fold0, resumed with a
    default (vendor) scheduler and no declared profile, is refused by the
    drift gate: no successor segment, the anchor unchanged, and no leader
    record. The engine check used to refuse it only after the leader record
    was appended. The twin, a fold0 scheduler, resumes and appends one."""
    from dsl41.boundary import EstateAnchor, default_anchor_dir
    from dsl41.period import wal_segments

    run_root = tmp_path / "run"
    catalog, live = _dst_genesis(run_root, FOLD0)
    _close(live)
    before = read_journal(estate_wal(run_root))
    segments = wal_segments(run_root)
    anchor = EstateAnchor(default_anchor_dir(run_root)).read()

    def resume():
        return asyncio.run(
            resume_run(
                catalog,
                run_root,
                clock=VirtualClock(start=T0 + timedelta(minutes=1)),
                adapters={"CMD": FakeAdapter(default=None)},
                scheduler=Scheduler(
                    catalog, start=T0, semantics=semantics.resolve(resume_switches)
                ),
            )
        )

    if resume_switches is None:
        with pytest.raises(EngineError, match="runtime-profile mismatch on semantics"):
            resume()
        assert read_journal(estate_wal(run_root)) == before
        assert wal_segments(run_root) == segments
        assert EstateAnchor(default_anchor_dir(run_root)).read() == anchor
        return
    _close(resume())
    after = read_journal(estate_wal(run_root))
    assert [r["rec"] for r in after[len(before) :]][:1] == ["leader"]


@pytest.mark.parametrize("switch", ["vendor", "fold0"])
@pytest.mark.parametrize("rooted", [False, True], ids=["bare", "run-root"])
def test_rehearse_ticks_under_the_dst_start_times_switch(
    tmp_path: Path, switch: str, rooted: bool
) -> None:
    """DL-260: 02:05 and 02:25 on 2026-03-08 in New York. The vendor runs
    only the first, at 3:00:05; fold0 runs both, past the gap. The
    rehearsal's scheduler and oracle read the same value, with or without a
    run root, so neither path refuses."""
    jil = tmp_path / "dst.jil"
    jil.write_text(_DST_JIL)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"events": []}))
    extra = ["--run-root", str(tmp_path / "run")] if rooted else []
    result = CliRunner().invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--scenario",
            str(scenario),
            "--start",
            "2026-03-08T05:00:00",
            "--hours",
            "4",
            "--format",
            "summary",
            "--semantics",
            f"dst-start-times={switch}",
            *extra,
        ],
    )
    assert result.exit_code == 0, result.output
    assert f"dj runs={1 if switch == 'vendor' else 2}" in result.stdout


@pytest.mark.parametrize("damage", ["missing", "foreign"])
def test_replay_refuses_a_period_whose_manifest_is_not_bound_to_it(
    tmp_path: Path, damage: str
) -> None:
    """DL-252: replay needs the switches the engine ran. A root whose
    manifest is gone, or is another period's, used to replay under the
    default switches and narrate a run the engine never made; it refuses."""
    run_root = tmp_path / "run"
    recorded = _rehearse(
        tmp_path, "--semantics", "ice-lookback=ordinary", "--run-root", str(run_root)
    )
    assert recorded.exit_code == 0, recorded.output
    path = period_dir(run_root, 1) / "manifest.json"
    if damage == "missing":
        path.unlink()
        expected = "manifest.json is not there"
    else:
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        _write_manifest(foreign, RuntimeProfile())
        path.write_bytes((period_dir(foreign, 1) / "manifest.json").read_bytes())
        expected = "is not this segment's"
    replayed = CliRunner().invoke(app, ["journal", str(run_root)])
    assert replayed.exit_code == 2
    assert expected in replayed.stderr
    assert "cons INACTIVE->STARTING" not in replayed.stdout
    with pytest.raises(RunHistoryError, match=expected):
        replay_trace(run_root, read_journal(estate_wal(run_root)), lower_source(_ICED_JIL))


@pytest.mark.parametrize(
    ("word", "message"),
    [
        ("ice-lookbak=true", "known switches: dst-start-times, fw-existence, ice-lookback"),
        ("ice-lookback=maybe", "is not one of true, ordinary"),
        ("ice-lookback", "expected NAME=VALUE"),
    ],
)
def test_the_cli_refuses_an_unknown_switch_or_value(
    tmp_path: Path, word: str, message: str
) -> None:
    result = _rehearse(tmp_path, "--semantics", word)
    assert result.exit_code == 2
    assert "--semantics" in result.stderr and message in result.stderr


def test_the_run_cli_records_the_switch_in_the_manifest(short_root: Path) -> None:
    with engine(short_root, extra=["--semantics", "ice-lookback=ordinary"]) as proc:
        manifest = read_period_manifest(proc.run_root)
        assert manifest is not None
        assert manifest.runtime_profile.semantics == ORDINARY
        assert manifest.runtime_hash == runtime_hash(manifest.runtime_profile)


def test_seal_takes_the_next_period_s_switches() -> None:
    from dsl41.cli_estate import _next_profile

    profile = _next_profile(None, None, [], "strict", False, None, ["ice-lookback=ordinary"])
    assert profile.semantics == ORDINARY
    with pytest.raises(typer.Exit):
        _next_profile(None, None, [], "strict", False, None, ["ice-lookback=maybe"])


def test_the_help_lists_every_switch() -> None:
    result = CliRunner().invoke(app, ["run", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0
    for name, switch in semantics.REGISTRY.items():
        assert name in result.stdout
        assert f"default {switch.default}" in result.stdout


# ---------------------------------------------------------- negative codes


@pytest.mark.parametrize(
    ("attr", "value"),
    [
        ("success_codes", "-1"),
        ("fail_codes", "0,-3"),
        ("success_codes", "-5-3"),
        ("fail_codes", "1--3"),
    ],
)
def test_sem09_a_negative_exit_code_is_refused_with_its_reason(attr: str, value: str) -> None:
    text = f"insert_job: j\njob_type: c\ncommand: x\nmachine: m1\n{attr}: {value}\n"
    with pytest.raises(LoweringError) as info:
        lower_source(text)
    message = str(info.value)
    assert "negative exit code" in message
    assert "0-255" in message and "TERMINATED with no exit code" in message
    assert "lo-hi range" not in message


def test_sem09_a_positive_range_still_lowers() -> None:
    text = "insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nsuccess_codes: 0-5,9\n"
    assert lower_source(text).jobs["j"].sem.success_codes == [(0, 5), (9, 9)]


@pytest.mark.parametrize("value", ["-", "1,-", "--", "-x", "--3"])
def test_sem09_a_malformed_token_keeps_the_ordinary_message(value: str) -> None:
    text = f"insert_job: j\njob_type: c\ncommand: x\nmachine: m1\nfail_codes: {value}\n"
    with pytest.raises(LoweringError) as info:
        lower_source(text)
    message = str(info.value)
    assert "expected an exit code or lo-hi range" in message
    assert "negative" not in message


# ---------------------------------------------- producers and staged ingress


def test_a_journal_only_log_installs_its_manifest_and_stays_replayable(tmp_path: Path) -> None:
    """`Journal.create` with no manifest synthesizes the default pin; it is
    written beside the log, bound to the segment, as genesis writes one, so
    `dsl41 journal FILE estate.jil` still replays a log made that way."""
    from dsl41.runner import Engine
    from dsl41.runner_journal import Journal

    jil = tmp_path / "estate.jil"
    jil.write_text(_ICED_JIL)
    catalog = lower_source(_ICED_JIL, file=str(jil))
    path = tmp_path / "journal.jsonl"
    journal = Journal.create(path, catalog=catalog, clock_domain="virtual", started_at=T0)
    live = Engine(
        catalog,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        journal=journal,
    )
    live.inject(Event(at=T0, kind="STARTJOB", payload={"job": "prod"}), source="control")

    async def play() -> None:
        try:
            await live.run_until_quiescent(T0 + timedelta(minutes=1))
        finally:
            await live.shutdown()

    asyncio.run(play())
    journal.close()
    manifest = read_period_manifest(tmp_path)
    assert manifest is not None
    assert manifest.runtime_profile == RuntimeProfile()
    result = CliRunner().invoke(app, ["journal", str(path), str(jil)])
    assert result.exit_code == 0, result.output


def test_a_staged_manifest_missing_semantics_is_refused_not_restored(tmp_path: Path) -> None:
    """Staged ingress applies the committed manifest's completeness rule: a
    staged profile without `semantics` refuses at read, and a seal over it
    does not commit a period with a restored `{}`."""
    from dsl41.boundary import SealRequest, read_staged_manifest, stage_next_period
    from dsl41.period import STAGED_MANIFEST_NAME, staging_dir, write_bundle
    from dsl41.period import SourceFile

    run_root = tmp_path / "run"
    catalog, live = _start_iced(run_root, RuntimeProfile())
    text = _ICED_JIL.replace("command: y", "command: z")
    manifest = stage_manifest(
        lower_source(text, file="next.jil"),
        source_bundle_hash=write_bundle(run_root, [SourceFile(path="next.jil", text=text)]),
        profile=RuntimeProfile(),
        state_machine_version=STATE_MACHINE_VERSION,
    )
    staged = stage_next_period(run_root, staged_manifest=manifest)
    directory = staging_dir(run_root, staged.stage_digest)
    path = directory / STAGED_MANIFEST_NAME
    payload = json.loads(path.read_bytes())
    del payload["runtime_profile"]["semantics"]
    path.write_bytes(canonical_bytes(payload))
    with pytest.raises(EngineError, match="runtime_profile missing semantics"):
        read_staged_manifest(directory)
    request = SealRequest(
        baseline_id=live.baseline_id,
        epoch=live.epoch,
        request_id="r-semantics",
        next_period=staged,
        stage_digest=staged.stage_digest,
        force_seal=False,
        claimed_actor="alice@ops",
    )

    async def seal() -> None:
        future = live.submit_seal(request)
        try:
            await live.run_until_quiescent(T0)
            assert future.done()
            with pytest.raises(EngineError, match="runtime_profile missing semantics"):
                future.result()
        finally:
            await live.shutdown()

    asyncio.run(seal())
    live.journal.close()
    assert read_period_manifest(run_root, 2) is None


# ------------------------------------------- switches reach every production call

#: Production calls that may leave `semantics` to its default, each with its
#: reason. Everything else in src passes the switches it runs under.
_DEFAULT_SWITCHES_ALLOWED: dict[tuple[str, str], str] = {
    ("equiv.py", "Oracle"): "equiv is a static tool with no runtime profile",
    ("minify_rules.py", "compile_calendar"): "a parse-validity predicate; no switch changes"
    " what the calendar parser accepts",
    ("cli_run.py", "engine.journal.preflight"): "`Journal.preflight` journals WARN items;"
    " it only shares the name of `runner_preflight.preflight`",
}


def _switch_takers(trees: dict[str, ast.Module]) -> dict[str, int | None]:
    """Every function or class in src whose `semantics` parameter is an
    optional `SemanticSwitches` defaulting to None, with the parameter's
    positional index (None when it is keyword-only)."""
    takers: dict[str, int | None] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for init in node.body:
                    if isinstance(init, ast.FunctionDef) and init.name == "__init__":
                        if _optional_switches(init.args):
                            takers[node.name] = _position(init.args, skip_self=True)
            elif isinstance(node, ast.FunctionDef) and node.name != "__init__":
                if _optional_switches(node.args):
                    takers[node.name] = _position(node.args, skip_self=False)
    return takers


def _position(args: ast.arguments, *, skip_self: bool) -> int | None:
    names = [a.arg for a in args.posonlyargs + args.args][1 if skip_self else 0 :]
    return names.index("semantics") if "semantics" in names else None


def _optional_switches(args: ast.arguments) -> bool:
    positional = args.posonlyargs + args.args
    pairs = list(zip(positional[len(positional) - len(args.defaults) :], args.defaults))
    pairs += [(a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
    return any(
        arg.arg == "semantics"
        and isinstance(default, ast.Constant)
        and default.value is None
        and arg.annotation is not None
        and "SemanticSwitches" in ast.unparse(arg.annotation)
        for arg, default in pairs
    )


def test_every_production_call_passes_its_semantic_switches() -> None:
    """A callable that takes optional switches falls back to the registry
    defaults when a caller omits them. A production caller that holds a
    profile must pass its switches, or the code it calls reads another
    reading than the scheduler and the pin: the queued recheck once compiled
    calendars under the default `wekr-first-week` this way. A call is matched
    by its bare name (`Oracle(...)`) or by the attribute it reaches
    (`autocal.compile_calendar(...)`), except a method called on `self` or
    `cls`. A positional `semantics` argument counts as passed. An entry in
    the allow-list names the callee as written."""
    root = Path(semantics.__file__).parent
    trees = {path.name: ast.parse(path.read_text()) for path in sorted(root.glob("*.py"))}
    takers = _switch_takers(trees)
    assert {
        "Oracle",
        "Scheduler",
        "Engine",
        "CapacityPool",
        "compile_calendar",
        "preflight",
    } <= set(takers)
    omitted: set[tuple[str, str]] = set()
    for name, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                callee = func.id
            elif isinstance(func, ast.Attribute):
                receiver = func.value
                if isinstance(receiver, ast.Name) and receiver.id in ("self", "cls"):
                    continue
                callee = func.attr
            else:
                continue
            if callee not in takers:
                continue
            if any(kw.arg in ("semantics", None) for kw in node.keywords):
                continue
            position = takers[callee]
            if position is not None and len(node.args) > position:
                continue
            omitted.add((name, ast.unparse(func)))
    assert omitted == set(_DEFAULT_SWITCHES_ALLOWED), (
        f"calls that omit `semantics`: {sorted(omitted - set(_DEFAULT_SWITCHES_ALLOWED))};"
        f" stale allow-list entries: {sorted(set(_DEFAULT_SWITCHES_ALLOWED) - omitted)}"
    )


@pytest.mark.parametrize(("switches", "dormant"), [(PARTIAL, False), (None, True)])
def test_preflight_reads_calendars_under_the_run_s_switches(
    switches: dict[str, str] | None, dormant: bool
) -> None:
    """DL-259's example: `WEKR1#01 & JAN#01 & TUE` selects no day under
    `first-full` but 2030-01-01 under `partial`. Preflight compiles the
    calendar under the switches it is given, so its dormancy WARN names the
    reading the scheduler fires."""
    from dsl41.runner_preflight import preflight

    catalog = lower_source(
        "extended_calendar: wk1\ncondition: WEKR1#01 & JAN#01 & TUE\n\n"
        "insert_job: j\njob_type: c\nmachine: m1\ncommand: x\n"
        'date_conditions: 1\nrun_calendar: wk1\nstart_times: "08:00"\n'
    )
    items = preflight(
        catalog,
        execution=False,
        start=datetime(2026, 7, 1),
        semantics=semantics.resolve(switches),
    )
    warned = any(i.code == "calendar" and "dormant" in i.message for i in items)
    assert warned is dormant
