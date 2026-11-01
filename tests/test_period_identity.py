"""Configuration-identity tests: the catalog hash, the runtime profile, the configuration row
and `engine.toml` (DL-130, DL-424).

The golden vectors pin EXACT bytes and EXACT digests as literals, because equality and
sensitivity tests alone would pass an implementation that is consistently wrong. The PR-15 sweep
is DERIVED from `RuntimeProfile.model_fields`, so a field added later is tested by default rather
than by somebody remembering to name it (the DL-83 discipline).

The run root's bundle, its period manifests and its segments are gone with the JSONL journal
(spec-wiring 1b): the store's configuration row carries the catalog and the profile, and the
restart holds a launch to them.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_runner_leadership import engine
from typer.testing import CliRunner

from dsl41.canon import canonical_bytes
from dsl41.cli import app
from dsl41.cli_run import _serve_run
from dsl41.ir import CatalogIR, CatalogMeta, lower_source
from dsl41.runner_adapters import FakeAdapter
from dsl41.runner_clock import EngineError, VirtualClock
from dsl41.runner_config import (
    FW_DEFAULT_INTERVAL_S,
    GRACE_TERM_S,
    Configuration,
    EngineSettings,
    RuntimeProfile,
    load_settings,
    runtime_hash,
    runtime_profile_from_cli,
    to_us,
)
from dsl41.reload_diff import catalog_hash_v2 as catalog_hash
from dsl41.runner_journal import open_reader
from dsl41.runner_startup import resume_run, start_run

T0 = datetime(2026, 7, 1, 8, 0)
_SOLO_JIL = "insert_job: j1\njob_type: c\ncommand: echo hi\nmachine: m1\n"


# ------------------------------------------------------------------ helpers


def _catalog(text: str = _SOLO_JIL, *, file: str = "estate.jil") -> CatalogIR:
    return lower_source(text, file=file)


def _close(engine) -> None:
    assert engine.journal is not None
    engine.journal.close()


def _configuration(run_root: Path) -> Configuration:
    """The configuration row a run root's store holds now."""
    store = open_reader(run_root)
    try:
        return Configuration.from_bytes(store.get_config(store.current_config_rev()), where="t")
    finally:
        store.close()


def _retired_v1(catalog: CatalogIR) -> str:
    """The RETIRED `catalog_hash` v1 recipe: a bare hexdigest over the whole model, `meta`
    included. DL-138 deleted it from the product, so the test that shows why it left spells it
    here."""
    return hashlib.sha256(catalog.model_dump_json().encode("utf-8")).hexdigest()


# ------------------------------------------------- 1. catalog_hash (PR-08a)


def _golden_catalog() -> CatalogIR:
    """A catalog with `source_files`, a non-null `tool_version`, a non-null
    `parsed_at` and a span -- exactly what PR-08a asks the vector to
    exercise."""
    catalog = _catalog()
    return catalog.model_copy(
        update={
            "meta": CatalogMeta(
                source_files=["estate.jil"],
                tool_version="1.2.3",
                parsed_at="2026-08-20T00:00:00",
            )
        }
    )


GOLDEN_CATALOG_BYTES = (
    b'{"calendars":{},"cycles":{},"external_instances":{},"globals_declared":{},'
    b'"ir_version":"0.3","jobs":{"j1":{"annotations":{},"box":{"box_name":null,'
    b'"box_terminator":false,"job_terminator":false},"exec_":{"command":"echo hi",'
    b'"envvars":null,"kind":"cmd","machine":"m1","owner":null,"profile":null,'
    b'"std_err_file":null,"std_in_file":null,"std_out_file":null},"job_type":"CMD",'
    b'"name":"j1","passthrough":{},"resources":[],"schedule":null,'
    b'"sem":{"auto_hold":false,"box_failure":null,"box_success":null,"condition":null,'
    b'"fail_codes":null,"initial_status":null,"max_exit_success":0,"n_retrys":0,'
    b'"success_codes":null,"term_run_time_min":null},'
    b'"span":{"byte_end":55,"byte_start":0,"file":"estate.jil","line_end":4,'
    b'"line_start":1},"var_sites":[]}},"machines":{},'
    b'"meta":{"source_files":["estate.jil"]},"resources":{}}'
)
GOLDEN_CATALOG_HASH = "sha256:d461eff80c08b65bdcac7e5002026ac48825f701be65db1b79e01989288c143f"


def test_pr08a_catalog_hash_v2_golden_vector() -> None:
    """The bytes themselves are the assertion: `meta` projected to
    `{source_files}`, spans in, keys sorted at every depth."""
    catalog = _golden_catalog()
    payload = catalog.model_dump(mode="json")
    payload["meta"] = {"source_files": ["estate.jil"]}
    assert canonical_bytes(payload) == GOLDEN_CATALOG_BYTES
    assert catalog_hash(catalog) == GOLDEN_CATALOG_HASH
    assert catalog_hash(catalog) == "sha256:" + hashlib.sha256(GOLDEN_CATALOG_BYTES).hexdigest()


def test_pr08a_tool_version_and_parsed_at_do_not_move_v2() -> None:
    """The whole reason for v2 (ss1.1): a patch release moves
    `tool_version`, and under the retired v1 recipe -- which hashed the
    whole model -- a seal committed by 1.2.3 could never be opened by
    1.2.4, an outage manufactured by bookkeeping (DL-100). The recipe is
    spelled HERE, because DL-138 deleted it from the product."""
    catalog = _golden_catalog()
    patched = catalog.model_copy(
        update={
            "meta": CatalogMeta(
                source_files=["estate.jil"],
                tool_version="1.2.4",
                parsed_at="2026-08-21T09:15:00",
            )
        }
    )
    assert catalog_hash(patched) == catalog_hash(catalog)
    assert _retired_v1(patched) != _retired_v1(catalog)


def test_v2_still_moves_for_a_source_file_list_and_for_a_span() -> None:
    """Only the two diagnostic keys leave. `source_files` stays -- it is
    the command-line order -- and so do spans."""
    catalog = _golden_catalog()
    reordered = catalog.model_copy(
        update={"meta": CatalogMeta(source_files=["estate.jil", "more.jil"])}
    )
    relocated = _catalog(file="elsewhere/estate.jil")
    assert catalog_hash(reordered) != catalog_hash(catalog)
    assert catalog_hash(relocated) != catalog_hash(_catalog())


def test_the_v2_projection_accounts_for_every_meta_field() -> None:
    """ss1.1 names the three fields `CatalogMeta` had when the recipe was
    frozen: `source_files` stays, `tool_version` and `parsed_at` leave. A
    fourth is a decision about what a period's identity IS, so it fails
    here rather than dropping out of the hash unnoticed."""
    assert set(CatalogMeta.model_fields) == {"source_files", "tool_version", "parsed_at"}


# ------------------------------------------- 2. RuntimeProfile (PR-08c, PR-15)


def _full_profile() -> RuntimeProfile:
    return RuntimeProfile(
        default_tz="Europe/Zurich",
        tz_aliases={"CET": "Europe/Zurich", "EST": "America/New_York"},
        as_machine=("alpha", "beta"),
        machine_policy="local-eligible",
        fw_default_interval_us=30_000_000,
    )


GOLDEN_PROFILE_BYTES = (
    b'{"as_machine":["alpha","beta"],"default_tz":"Europe/Zurich",'
    b'"fw_default_interval_us":30000000,"machine_policy":"local-eligible","semantics":{},'
    b'"tz_aliases":{"CET":"Europe/Zurich","EST":"America/New_York"}}'
)
GOLDEN_RUNTIME_HASH = "sha256:1a358ecc4da7df260ed6418f71329a1f5a2eac317aa53894c37871d72cc05389"


def test_the_profile_fields_default_to_the_seconds_constants_unmoved() -> None:
    """The microsecond field reads the seconds constant (one source)."""
    assert RuntimeProfile().fw_default_interval_us == to_us(FW_DEFAULT_INTERVAL_S) == 60_000_000


def test_pr08c_runtime_hash_golden_vector() -> None:
    """One fully populated profile, its canonical bytes and its hash."""
    profile = _full_profile()
    assert canonical_bytes(profile.model_dump(mode="json")) == GOLDEN_PROFILE_BYTES
    assert runtime_hash(profile) == GOLDEN_RUNTIME_HASH


def test_the_profile_holds_only_what_the_engine_reads_under() -> None:
    """spec-wiring 1b: the profile holds the zone, the alias table, the machine identity and
    policy, the FW window and the semantic switches, and no execution mode, deadman, settle or
    spawn window or retry horizon; the stop grace is `engine.toml`'s, one setting for every
    estate the engine runs."""
    assert set(RuntimeProfile.model_fields) == {
        "default_tz",
        "tz_aliases",
        "as_machine",
        "machine_policy",
        "fw_default_interval_us",
        "semantics",
    }


#: One alternate value per field, for the PR-15 sweep. Keyed by field name and checked against
#: `RuntimeProfile.model_fields` below, so a field added later fails loudly here instead of going
#: quietly unhashed.
_ALTERNATES: dict[str, Any] = {
    "default_tz": "Europe/Zurich",
    "tz_aliases": {"CET": "Europe/Zurich"},
    "as_machine": ("alpha",),
    "machine_policy": "local-eligible",
    "fw_default_interval_us": 30_000_000,
    "semantics": {"ice-lookback": "ordinary"},
}


def test_pr15_every_field_of_the_model_has_a_case() -> None:
    """The sweep is derived from the model, so hashing a named subset cannot pass."""
    assert set(_ALTERNATES) == set(RuntimeProfile.model_fields)


@pytest.mark.parametrize("field", sorted(RuntimeProfile.model_fields))
def test_pr15_runtime_hash_moves_for_every_field(field: str) -> None:
    base = RuntimeProfile()
    changed = RuntimeProfile(**{**base.model_dump(), field: _ALTERNATES[field]})
    assert changed != base, f"the alternate for {field} does not change the value"
    assert runtime_hash(changed) != runtime_hash(base)


def test_every_profile_field_is_wired_or_declared() -> None:
    """`_derive_runtime_profile`'s partition is exhaustive over the model (DL-152): every field
    is read back from a wired component or supplied by the launcher's declaration, so none
    inherits the record for ever and none can never disagree with it. The wired group is read
    off the function's own source, so a deleted rung fails here."""
    import inspect
    import re

    from dsl41 import runner_startup

    source = inspect.getsource(runner_startup._derive_runtime_profile)
    assigned = set(re.findall(r'values\["(\w+)"\]\s*=', source))
    assert assigned == set(RuntimeProfile.model_fields)


def test_the_profile_is_frozen_and_forbids_extras() -> None:
    profile = RuntimeProfile()
    with pytest.raises(Exception):
        profile.default_tz = "Europe/Zurich"  # type: ignore[misc]
    with pytest.raises(Exception):
        RuntimeProfile(execution_mode="tethered")  # type: ignore[call-arg]


# ------------------------------------------- 2a. CLI -> profile (PR-15a)


def test_pr15a_omitted_options_resolve_to_the_stated_defaults() -> None:
    profile = runtime_profile_from_cli()
    assert profile == RuntimeProfile()
    assert profile.default_tz == "UTC"
    assert profile.tz_aliases == {} and profile.as_machine == ()
    assert profile.machine_policy == "strict"
    assert profile.fw_default_interval_us == 60_000_000


def test_pr15a_an_absent_timezone_is_utc_never_null() -> None:
    assert runtime_profile_from_cli(timezone=None).default_tz == "UTC"
    assert runtime_profile_from_cli(timezone="Europe/Zurich").default_tz == "Europe/Zurich"


def test_pr15a_local_eligible_round_trips() -> None:
    profile = runtime_profile_from_cli(machine_policy="local-eligible")
    assert profile.machine_policy == "local-eligible"
    assert runtime_hash(profile) != runtime_hash(runtime_profile_from_cli())


def test_pr15a_duplicate_as_machine_collapses_and_sorts() -> None:
    """Two spellings of one machine set are one profile."""
    left = runtime_profile_from_cli(as_machine=["beta", "alpha", "beta"])
    right = runtime_profile_from_cli(as_machine=["alpha", "beta"])
    assert left.as_machine == ("alpha", "beta")
    assert runtime_hash(left) == runtime_hash(right)


def test_pr15a_fractional_seconds_round_to_microseconds() -> None:
    assert runtime_profile_from_cli(fw_default_interval_s=1.5).fw_default_interval_us == 1_500_000


def test_pr15a_tz_aliases_are_the_resolved_contents_and_never_null() -> None:
    assert runtime_profile_from_cli(tz_aliases=None).tz_aliases == {}
    assert runtime_profile_from_cli(tz_aliases={"CET": "Europe/Zurich"}).tz_aliases == {
        "CET": "Europe/Zurich"
    }


@pytest.mark.parametrize("value", [0, -1, None])
def test_pr15a_the_fw_interval_is_strictly_positive(value: object) -> None:
    with pytest.raises(ValueError):
        RuntimeProfile(**{**RuntimeProfile().model_dump(), "fw_default_interval_us": value})


# ------------------------------------------- 3. the configuration row and engine.toml


def test_the_configuration_row_round_trips_and_names_its_hashes() -> None:
    """The row carries the canonical catalog, the profile and the marked set; reading it back
    gives the same configuration, and its hashes are the catalog's and the profile's own."""
    catalog = _golden_catalog()
    row = Configuration(
        catalog=catalog, profile=_full_profile(), non_idempotent={"j1": "posts twice"}
    )
    back = Configuration.from_bytes(row.to_bytes(), where="here")
    assert back == row
    assert back.catalog_hash == catalog_hash(catalog) == GOLDEN_CATALOG_HASH
    assert back.runtime_hash == GOLDEN_RUNTIME_HASH
    assert row.digest() == "sha256:" + hashlib.sha256(row.to_bytes()).hexdigest()
    with pytest.raises(EngineError, match="here: unreadable configuration row"):
        Configuration.from_bytes(b'{"catalog": {}}', where="here")


def test_genesis_records_the_configuration_with_the_marked_set(tmp_path: Path) -> None:
    """Genesis writes configuration rev 1: the catalog, the profile the wiring runs, and the
    jobs `non-idempotent.toml` named, which recovery reads from the store, never from disk."""
    run_root = tmp_path / "run"
    catalog = _catalog()
    _close(
        start_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            non_idempotent={"j1": "posts twice"},
        )
    )
    row = _configuration(run_root)
    assert row.catalog_hash == catalog_hash(catalog)
    assert row.profile == RuntimeProfile()
    assert row.non_idempotent == {"j1": "posts twice"}


def test_engine_toml_is_written_with_the_defaults_and_read_back(tmp_path: Path) -> None:
    """spec-wiring 1b: the first run writes `engine.toml` with the four defaults; an operator's
    edit is read back; a flag overrides one value for one run and is not written back; a file
    that does not read refuses naming it."""
    settings = load_settings(tmp_path)
    assert settings == EngineSettings()
    assert (tmp_path / "engine.toml").read_text() == (
        "grace_term_s = 10.0\ngrace_kill_s = 5.0\nsnapshot_interval_s = 300.0\nretention_days = 90\n"
    )
    (tmp_path / "engine.toml").write_text("grace_term_s = 3.0\nretention_days = 7\n")
    edited = load_settings(tmp_path)
    assert (edited.grace_term_s, edited.retention_days, edited.grace_kill_s) == (3.0, 7, 5.0)
    once = edited.overridden(snapshot_interval_s=60, grace_kill_s=None)
    assert (once.snapshot_interval_s, once.grace_kill_s) == (60, 5.0)
    assert load_settings(tmp_path) == edited
    (tmp_path / "engine.toml").write_text("grace_term_s = -1\n")
    with pytest.raises(EngineError, match="engine.toml"):
        load_settings(tmp_path)


# ------------------------------------------- 4. the restart gates


def test_a_resume_with_different_launch_options_refuses(tmp_path: Path) -> None:
    """PR-22's runtime half: the same estate resumed under a different declared machine
    policy refuses NAMING the moved field -- a restart that quietly rebuilt under new options
    would change semantics with every identity gate green."""
    catalog = _catalog()
    _close(
        start_run(
            catalog,
            tmp_path / "root",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            launched=runtime_profile_from_cli(machine_policy="local-eligible"),
        )
    )

    async def scenario() -> None:
        with pytest.raises(EngineError, match="runtime-profile mismatch on machine_policy"):
            await resume_run(
                catalog,
                tmp_path / "root",
                clock=VirtualClock(start=T0 + timedelta(minutes=1)),
                adapters={"CMD": FakeAdapter(default=None)},
                declared=runtime_profile_from_cli(),
            )
        _close(
            await resume_run(
                catalog,
                tmp_path / "root",
                clock=VirtualClock(start=T0 + timedelta(minutes=1)),
                adapters={"CMD": FakeAdapter(default=None)},
                declared=runtime_profile_from_cli(machine_policy="local-eligible"),
            )
        )

    asyncio.run(scenario())


def test_the_record_is_derived_from_the_wiring_not_the_flags(tmp_path: Path) -> None:
    """An embedder that builds a Europe/Zurich scheduler and declares nothing records a
    profile that SAYS Zurich: the record describes the machine that runs. And a launch profile
    the wiring disagrees with is a fiction, refused before anything is durable."""
    from dsl41.runner_scheduler import Scheduler

    catalog = _catalog()
    _close(
        start_run(
            catalog,
            tmp_path / "zurich",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=Scheduler(catalog, start=T0, default_tz="Europe/Zurich"),
        )
    )
    assert _configuration(tmp_path / "zurich").profile.default_tz == "Europe/Zurich"
    with pytest.raises(EngineError, match="disagrees with the engine's wiring on default_tz"):
        start_run(
            catalog,
            tmp_path / "fiction",
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=Scheduler(catalog, start=T0, default_tz="Europe/Zurich"),
            launched=runtime_profile_from_cli(timezone="UTC"),
        )
    assert not (tmp_path / "fiction" / "engine.db").exists()


def test_a_pinned_scheduled_root_refuses_a_schedulerless_resume(tmp_path: Path) -> None:
    """The profile cannot see a scheduler's ABSENCE (default_tz inherits the
    pin and reports no drift), so the refusal is its own: a pinned root
    whose catalog schedules jobs, resumed with no scheduler, would silently
    stop firing them."""
    import asyncio as _asyncio

    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_scheduler import Scheduler
    from dsl41.runner_startup import resume_run, start_run

    scheduled = lower_source(
        "insert_job: tick\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\ndays_of_week: all\nstart_mins: 0\n"
    )
    t0 = datetime(2026, 7, 1, 8, 0)
    engine = start_run(
        scheduled,
        tmp_path / "root",
        clock=VirtualClock(start=t0),
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=Scheduler(scheduled, start=t0),
    )
    assert engine.journal is not None
    engine.journal.close()

    async def scenario() -> None:
        with pytest.raises(EngineError, match="wired no scheduler"):
            await resume_run(
                scheduled,
                tmp_path / "root",
                clock=VirtualClock(start=t0 + timedelta(minutes=1)),
                adapters={"CMD": FakeAdapter(default=None)},
            )

    _asyncio.run(scenario())


def test_a_pinned_root_refuses_resume_with_a_missing_adapter(tmp_path: Path) -> None:
    """The profile inherits pinned values for wiring it cannot see, so a
    missing CMD adapter drifts nothing -- and the job would reach RUNNING
    with no process behind it. A pinned estate refuses instead."""
    import asyncio as _asyncio

    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_startup import resume_run, start_run

    catalog = lower_source(_SOLO_JIL)
    t0 = datetime(2026, 7, 1, 8, 0)
    engine = start_run(
        catalog,
        tmp_path / "root",
        clock=VirtualClock(start=t0),
        adapters={"CMD": FakeAdapter(default=None)},
    )
    assert engine.journal is not None
    engine.journal.close()

    async def scenario() -> None:
        with pytest.raises(EngineError, match="no adapter"):
            await resume_run(
                catalog,
                tmp_path / "root",
                clock=VirtualClock(start=t0 + timedelta(minutes=1)),
                adapters={},
            )

    _asyncio.run(scenario())


def test_the_model_itself_normalizes_as_machine() -> None:
    """ss2.1 says the field IS sorted and de-duplicated -- whoever
    constructed it. Two spellings of one machine set are one profile and
    one hash."""
    a = RuntimeProfile(as_machine=("b", "a", "b"))
    b = RuntimeProfile(as_machine=("a", "b"))
    assert a.as_machine == ("a", "b")
    assert runtime_hash(a) == runtime_hash(b)


def test_genesis_refuses_a_catalog_it_cannot_dispatch(tmp_path: Path) -> None:
    """The adapter gate runs at GENESIS too: an estate that opens unable to
    dispatch its own catalog is the same silent no-process hole one gate
    later."""
    from dsl41.runner_startup import start_run

    with pytest.raises(EngineError, match="genesis wired no adapter"):
        start_run(
            lower_source(_SOLO_JIL),
            tmp_path / "root",
            clock=VirtualClock(start=datetime(2026, 7, 1, 8, 0)),
            adapters={},
        )


def test_the_normalizer_never_launders_a_wrong_typed_machine() -> None:
    """A `7` in as_machine is refused, not spelled "7": the before-validator
    must not convert what strict validation exists to catch."""
    from pydantic import ValidationError as _VE

    with pytest.raises(_VE, match="as_machine"):
        RuntimeProfile(as_machine=(7,))  # type: ignore[arg-type]
    with pytest.raises(_VE, match="as_machine"):
        RuntimeProfile(as_machine="alpha")  # type: ignore[arg-type]  # one name, not a set


def test_genesis_refuses_a_scheduled_catalog_with_no_or_the_wrong_scheduler(
    tmp_path: Path,
) -> None:
    """The scheduler gate runs at genesis too -- and it checks the CATALOG
    the scheduler compiled, because one built over another estate's plans
    passes every timezone comparison while firing nothing of this one's."""
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_scheduler import Scheduler
    from dsl41.runner_startup import start_run

    scheduled = lower_source(
        "insert_job: tick\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\ndays_of_week: all\nstart_mins: 0\n"
    )
    t0 = datetime(2026, 7, 1, 8, 0)
    with pytest.raises(EngineError, match="genesis wired no scheduler"):
        start_run(
            scheduled,
            tmp_path / "none",
            clock=VirtualClock(start=t0),
            adapters={"CMD": FakeAdapter(default=None)},
        )
    other = lower_source(_SOLO_JIL)
    with pytest.raises(EngineError, match="different catalog"):
        start_run(
            scheduled,
            tmp_path / "wrong",
            clock=VirtualClock(start=t0),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=Scheduler(other, start=t0),
        )


def test_a_catalog_mutated_after_the_plans_compiled_refuses(tmp_path: Path) -> None:
    """The scheduler pins its catalog's v2 hash AT COMPILE TIME, so an
    object mutated afterwards cannot pass by identity: the plans are stale
    the moment the catalog moved, whoever holds the reference."""
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_scheduler import Scheduler
    from dsl41.runner_startup import start_run

    scheduled = lower_source(
        "insert_job: tick\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\ndays_of_week: all\nstart_mins: 0\n"
    )
    t0 = datetime(2026, 7, 1, 8, 0)
    scheduler = Scheduler(scheduled, start=t0)
    scheduled.jobs["tick"].exec_.command = "y"  # mutated AFTER the compile
    with pytest.raises(EngineError, match="different catalog"):
        start_run(
            scheduled,
            tmp_path / "stale",
            clock=VirtualClock(start=t0),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=scheduler,
        )


def test_a_scheduler_over_a_detriggered_catalog_still_refuses(tmp_path: Path) -> None:
    """Removing the catalog's last trigger after the compile must not skip
    the hash comparison: the stale plans would still fire the removed
    jobs. Every supplied scheduler is held to its compile-time hash."""
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_scheduler import Scheduler
    from dsl41.runner_startup import start_run

    scheduled = lower_source(
        "insert_job: tick\njob_type: c\ncommand: x\nmachine: m1\n"
        "date_conditions: 1\ndays_of_week: all\nstart_mins: 0\n"
    )
    t0 = datetime(2026, 7, 1, 8, 0)
    scheduler = Scheduler(scheduled, start=t0)
    scheduled.jobs["tick"].schedule = None  # the last trigger, removed post-compile
    with pytest.raises(EngineError, match="different catalog"):
        start_run(
            scheduled,
            tmp_path / "stale",
            clock=VirtualClock(start=t0),
            adapters={"CMD": FakeAdapter(default=None)},
            scheduler=scheduler,
        )


def test_the_schedulers_compile_time_inputs_are_read_only() -> None:
    """A post-compile edit of default_tz or tz_aliases would pin one
    timezone while the plans execute under another: the attributes are
    properties over compile-time copies, and mutating the returned mapping
    moves nothing."""
    catalog = lower_source(_SOLO_JIL)
    from dsl41.runner_scheduler import Scheduler

    scheduler = Scheduler(catalog, start=datetime(2026, 7, 1, 8, 0), default_tz="UTC")
    with pytest.raises(AttributeError):
        scheduler.default_tz = "Europe/Zurich"  # type: ignore[misc]
    scheduler.tz_aliases["x"] = "y"  # a copy: the compile-time inputs are unmoved
    assert scheduler.tz_aliases == {}


def test_rehearse_reads_the_calendar_day_in_the_base_zone(tmp_path: Path) -> None:
    """DL-212 end to end: `--timezone` is the fallback preflight's calendar
    probes read the anchor in, so the WARN names the day the scheduler will.
    At 23:30Z on 2026-03-10 Auckland is already on the 11th and the calendar
    is spent; under the naive UTC basis it is not."""
    jil = tmp_path / "cal.jil"
    jil.write_text(_CAL_JIL)
    args = ["rehearse", str(jil), "--start", "2026-03-10T23:30:00", "--hours", "1"]
    plain = CliRunner().invoke(app, [*args, "--run-root", str(tmp_path / "utc")])
    assert plain.exit_code == 0, plain.output
    assert "exhausted" not in plain.output
    zoned = CliRunner().invoke(
        app, [*args, "--run-root", str(tmp_path / "nz"), "--timezone", "Pacific/Auckland"]
    )
    assert zoned.exit_code == 0, zoned.output
    assert "preflight WARN [calendar] bz: run_calendar 'lastday' is exhausted" in zoned.output


def test_an_unresolvable_base_zone_is_refused_before_preflight_prints(tmp_path: Path) -> None:
    """DL-212's one visible change: `check_base_tz` runs BEFORE the preflight
    in `run` and `rehearse`, so the zone preflight falls back to has already
    been refused. The control is the same estate without the flag, which
    does print the WARN."""
    jil = tmp_path / "stale.jil"
    jil.write_text(_CAL_JIL.replace("03/10/2026", "01/01/2020"))
    args = ["rehearse", str(jil), "--start", "2026-03-10T23:30:00", "--hours", "1"]
    warned = CliRunner().invoke(app, [*args, "--run-root", str(tmp_path / "ok")])
    assert warned.exit_code == 0, warned.output
    assert "is exhausted" in warned.output
    refused = CliRunner().invoke(
        app, [*args, "--run-root", str(tmp_path / "bad"), "--timezone", "Bogus/Zone"]
    )
    assert refused.exit_code == 2
    assert "--timezone 'Bogus/Zone' is not resolvable" in refused.stderr
    assert "preflight" not in refused.stdout and "preflight" not in refused.stderr


def test_resume_refuses_wiring_that_moved_off_the_pin(tmp_path: Path) -> None:
    """The core half of the runtime gate: the same root resumed with a
    scheduler wired to another timezone refuses in `resume_run` itself --
    no CLI in the loop -- naming the moved field."""
    import asyncio as _asyncio

    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_scheduler import Scheduler
    from dsl41.runner_startup import resume_run, start_run

    catalog = lower_source(_SOLO_JIL)
    t0 = datetime(2026, 7, 1, 8, 0)
    engine = start_run(
        catalog,
        tmp_path / "root",
        clock=VirtualClock(start=t0),
        adapters={"CMD": FakeAdapter(default=None)},
        scheduler=Scheduler(catalog, start=t0, default_tz="UTC"),
    )
    assert engine.journal is not None
    engine.journal.close()

    async def scenario() -> None:
        with pytest.raises(EngineError, match="default_tz"):
            await resume_run(
                catalog,
                tmp_path / "root",
                clock=VirtualClock(start=t0 + timedelta(minutes=1)),
                adapters={"CMD": FakeAdapter(default=None)},
                scheduler=Scheduler(
                    catalog, start=t0 + timedelta(minutes=1), default_tz="Europe/Zurich"
                ),
            )

    _asyncio.run(scenario())


_CAL_JIL = (
    "calendar: lastday\n03/10/2026 00:00\n\n"
    "insert_job: bz\njob_type: c\ncommand: echo hi\nmachine: localhost\n"
    'date_conditions: 1\nrun_calendar: lastday\nstart_times: "08:00"\n'
)


# ------------------------------------------- 5. the launchers


def test_the_run_cli_records_the_profile_it_was_launched_with(short_root: Path) -> None:
    """End to end through the real `dsl41 run` process: the launch options reach the
    configuration row and its `runtime_hash`."""
    with engine(
        short_root,
        extra=[
            "--timezone",
            "Europe/Zurich",
            "--machine-policy",
            "local-eligible",
            "--as-machine",
            "beta",
            "--as-machine",
            "alpha",
            "--as-machine",
            "beta",
        ],
    ) as proc:
        profile = _configuration(proc.run_root).profile
        assert profile.default_tz == "Europe/Zurich"
        assert profile.machine_policy == "local-eligible"
        assert profile.as_machine == ("alpha", "beta")


def test_a_rehearsal_run_root_records_the_profile_it_rehearsed_under(tmp_path: Path) -> None:
    """A rehearsal is evidence about production behavior, so its run root is a self-contained
    artifact too: the timezone it interpreted the estate under is in the configuration row,
    and the store says it is a rehearsal's."""
    jil = tmp_path / "estate.jil"
    jil.write_text(_SOLO_JIL)
    run_root = tmp_path / "run"
    result = CliRunner().invoke(
        app,
        [
            "rehearse",
            str(jil),
            "--run-root",
            str(run_root),
            "--start",
            "2026-07-01T08:00:00",
            "--hours",
            "1",
            "--timezone",
            "Europe/Zurich",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _configuration(run_root).profile.default_tz == "Europe/Zurich"
    store = open_reader(run_root)
    try:
        assert store.meta()["clock_domain"] == "virtual"
    finally:
        store.close()


def test_a_journal_less_engine_is_untouched_by_any_of_this() -> None:
    """The bisimulation harness runs an Engine with no journal at all."""
    from dsl41.runner import Engine

    live = Engine(_catalog(), clock=VirtualClock(start=T0), adapters={"CMD": FakeAdapter()})
    assert live.journal is None


def test_serve_wires_every_adapter_window_from_the_profile_and_the_settings(
    tmp_path: Path, monkeypatch
) -> None:
    """DL-137: one builder (`runner_startup.wire_from_profile`). This drives a profile and an
    override off every default so the coincidence becomes an equality: the engine is wired
    with the grace the settings carry, and the FW interval and the timezone the PROFILE
    carries. The `timezone: Zurich` job pins the other half: the profile cannot spell "no
    --timezone-map", so an empty alias table has to read as ABSENT or SEM-35's unique-city
    ladder is silently retired for every per-job zone."""
    from dsl41 import runner_startup

    catalog = lower_source(
        _SOLO_JIL + 'date_conditions: 1\ndays_of_week: all\nstart_times: "03:00"\n'
        "timezone: Zurich\n"
    )
    profile = RuntimeProfile(default_tz="Europe/Zurich", fw_default_interval_us=23_000_000)
    captured: dict[str, Any] = {}

    def _capture(catalog_arg: Any, root: Path, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise EngineError("pin test: stop before the loop")

    monkeypatch.setattr(runner_startup, "start_run", _capture)
    code = asyncio.run(
        _serve_run(
            catalog,
            tmp_path / "root",
            False,
            [],
            profile=profile,
            overrides={"grace_term_s": 7.0},
        )
    )
    assert code == 2
    assert captured["adapters"]["CMD"].grace_seconds == 7.0
    assert captured["adapters"]["FW"].default_interval_s == 23
    assert captured["scheduler"].default_tz == "Europe/Zurich"
    assert captured["settings"].grace_term_s == 7.0
    assert str(captured["scheduler"]._plans["j1"].tz) == "Europe/Zurich"
    # the override was for one run: engine.toml keeps the default
    assert load_settings(tmp_path / "root").grace_term_s == GRACE_TERM_S


def test_a_failing_store_close_still_gives_the_leader_lock_back(
    tmp_path: Path, monkeypatch
) -> None:
    """DL-145, the other half of the one teardown. A failing close must PROPAGATE; what it must
    not ALSO do is strand the estate's leader lock: the operator's retry would then be refused
    with "held by another engine" by a process that is gone."""
    from dsl41 import runner_control, runner_journal
    from dsl41.runner_ledger import acquire_run_root

    run_root = tmp_path / "root"

    async def _no_socket(self: Any) -> None:
        raise EngineError("pin: the control socket could not be bound")

    def _bad_close(self: Any) -> None:
        self.store.close()  # the failure is after the close; the connection still goes
        raise OSError("pin: the close failed on the way out")

    monkeypatch.setattr(runner_control.ControlServer, "start", _no_socket)
    monkeypatch.setattr(runner_journal.Journal, "close", _bad_close)
    with pytest.raises(OSError, match="the close failed on the way out"):
        asyncio.run(_serve_run(_catalog(), run_root, False, [], profile=runtime_profile_from_cli()))
    acquire_run_root(run_root).release()


def test_the_profile_defaults_are_the_engine_s_own() -> None:
    """A record that disagreed with the running engine would record a fiction. The durations
    with an engine counterpart are asserted against it."""
    import inspect

    from dsl41.runner_adapters import FileWatcherAdapter, LocalCommandAdapter

    assert LocalCommandAdapter().grace_seconds == GRACE_TERM_S == EngineSettings().grace_term_s
    assert FileWatcherAdapter().default_interval_s == FW_DEFAULT_INTERVAL_S
    parameters = inspect.signature(resume_run).parameters
    # None = "resolve from the settings": only an explicit caller override departs from them;
    # a restart waits for nothing, so it has no settle window (spec-restart, DL-433)
    assert not [name for name in parameters if name.startswith("settle")]
    assert parameters["grace_seconds"].default is None
