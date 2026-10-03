"""Semantic switches (runner-design ss8a, DL-252): the closed registry, the
runtime-profile field that records overrides, the hash-neutral spelling of
"no override", the path from the command line to the period's pin, and the
first switch, `ice-lookback`, across the manifest, the engine and replay.

The oracle's own reading of the switch, on both bisimulation paths, is in
`test_oracle.py` (`test_sem20_ice_lookback_switch_selects_the_q10_reading`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import re
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
    write_period_manifest,
)
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_history import RunHistoryError, replay_trace
from dsl41.runner_journal import read_journal
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_startup import _derive_runtime_profile, resume_run, start_run
from test_period_identity import GOLDEN_RUNTIME_HASH, _full_profile
from test_runner_leadership import engine

T0 = datetime(2026, 7, 1, 8, 0)
ORDINARY = {"ice-lookback": "ordinary"}

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


def test_the_ice_lookback_default_is_the_documented_autosys_reading() -> None:
    switch = semantics.REGISTRY["ice-lookback"]
    assert switch.values == ("true", "ordinary")
    assert switch.default == "true" == switch.autosys


def test_the_profile_refuses_an_unknown_switch_with_the_known_names() -> None:
    with pytest.raises(ValidationError, match="unknown semantic switch 'ice-lookbak'") as info:
        RuntimeProfile(semantics={"ice-lookbak": "true"})
    assert "known switches: ice-lookback" in str(info.value)


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
        (["nope=true"], "known switches: ice-lookback"),
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


def _start_iced(run_root: Path, profile: RuntimeProfile):
    from dsl41.boundary import stage_period
    from dsl41.ast_jil import parse

    jil = run_root.parent / "iced.jil"
    jil.write_text(_ICED_JIL)
    catalog = lower_source(_ICED_JIL, file=str(jil))
    parsed = [parse(_ICED_JIL, file=str(jil))]
    run_root.mkdir()
    staged = stage_period(run_root, parsed, catalog, profile)
    return catalog, start_run(
        catalog,
        run_root,
        clock=VirtualClock(start=T0),
        adapters={"CMD": FakeAdapter(default=None)},
        staged=staged,
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


def test_ice_lookback_affects_exactly_the_jobs_with_a_lookback_atom() -> None:
    affects = semantics.REGISTRY["ice-lookback"].affects
    catalog = lower_source(
        "insert_job: a\njob_type: c\nmachine: m1\ncommand: x\n\n"
        "insert_job: plain\njob_type: c\nmachine: m1\ncommand: y\ncondition: f(a)\n\n"
        "insert_job: lb\njob_type: c\nmachine: m1\ncommand: y\ncondition: e(a, 01.00) = 0\n\n"
        "insert_job: bf\njob_type: b\nbox_failure: t(a, 0)\n"
    )
    assert {name for name, job in catalog.jobs.items() if affects(job)} == {"lb", "bf"}


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
        ("ice-lookbak=true", "known switches: ice-lookback"),
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
