"""`update` as a module (spec-reload "`dsl41 update`"): the chart, the admission, the tier, the
application, the selective schedule replacement and the two command renderings.

The chart is driven by events only, and its transition coverage is held here because it is not
registered yet. The scenarios run the nightbank small estate through the real oracle state and
the real scheduler; the tier's effects go to a recorder that applies the plan as the engine
will, and records every other effect by name. Assertions are on order, never on elapsed time.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import statechart_completeness as sc

from dsl41.cli_reload import UpdateReply, reread_command, update_command
from dsl41.engine_charts import UPDATE_STOP
from dsl41.idempotence import load_exceptions
from dsl41.ir import CatalogIR, ExecSpec, lower_source
from dsl41.oracle import Oracle
from dsl41.canon import canonical_bytes, decode
from dsl41.oracle_state import CarriedRows, Event, JobRuntime, OracleError
from dsl41.reload_plan import Live, RereadResult, load_estate, read_plan, reread
from dsl41.reload_update import (
    ALL_STOPPED_OR_NONE,
    APPLY,
    BOX_RUNNING,
    EXIT_RECORDED,
    JOURNAL_APPLIED,
    JOURNAL_INTENT,
    STALE_PLAN,
    STOP_ATTEMPT,
    UPDATE,
    UPDATE_ADMITTED,
    UPDATE_IN_PROGRESS,
    UPDATE_STATES,
    WOULD_STOP_RUNNING,
    Admitted,
    Application,
    Refused,
    UpdateRecord,
    UpdateTier,
    admission_check,
    apply_application,
    live_of,
    plan_application,
)
from dsl41 import run_root
from dsl41.reload_diff import catalog_hash_v2
from dsl41.runner_clock import EngineError
from dsl41.runner_scheduler import Scheduler
from dsl41.state_machine import (
    HITS_ENV,
    Executor,
    UnhandledEvent,
    alphabet,
    chart_warnings,
    coverage_items,
    well_formed,
)
from dsl41.state_machine import Event as ChartEvent

SMALL = Path(__file__).resolve().parent.parent / "examples" / "nightbank" / "estate" / "small"
START = datetime(2026, 1, 6)
PROPERTIES = """\
OWNER=tester
NB_DATA=/var/nb/data
NB_LOGS=/var/nb/logs
NB_PROFILE=/var/nb/profile.env
EOD_AMER=19:11
EOD_APAC=09:05
EOD_EMEA=01:08
HB_WINDOW=00:05-01:35
"""
TOKEN = "f" * 64
ADMITTED = Admitted(TOKEN, ("A", "B"), frozenset({"A", "B", "C"}))
NONE_TO_STOP = Admitted(TOKEN, (), frozenset({"C"}))
STALE = Refused(TOKEN, STALE_PLAN)


# ---- the chart


def test_the_chart_is_well_formed_has_the_specs_four_rows_and_no_warning() -> None:
    assert well_formed(UPDATE) == []
    assert chart_warnings(UPDATE) == []
    rows = [(r.id, r.source, r.event, r.target, r.kind, r.guard) for r in UPDATE.rows]
    assert rows == [
        ("update.01", "IDLE", UPDATE_ADMITTED, "STOPPING", "external", "admitted"),
        ("update.02", "IDLE", UPDATE_ADMITTED, "REFUSED", "external", "refused"),
        ("update.03", "STOPPING", ALL_STOPPED_OR_NONE, "APPLIED", "external", ""),
        ("update.04", "STOPPING", EXIT_RECORDED, "", "internal", ""),
    ]
    assert alphabet(UPDATE) == sorted([UPDATE_ADMITTED, ALL_STOPPED_OR_NONE, EXIT_RECORDED])


def _executor(state: str) -> Executor:
    executor = Executor()
    executor.start("u", UPDATE, UpdateRecord())
    executor.run()
    if state in ("STOPPING", "APPLIED"):
        executor.dispatch("u", ChartEvent(UPDATE_ADMITTED, ADMITTED))
    if state == "APPLIED":
        executor.dispatch("u", ChartEvent(ALL_STOPPED_OR_NONE))
    if state == "REFUSED":
        executor.dispatch("u", ChartEvent(UPDATE_ADMITTED, STALE))
    return executor


def _witness(state: str) -> sc.Builder:
    return lambda: (_executor(state), "u")


ABSTRACTION = sc.Abstraction(
    witnesses={state: _witness(state) for state in UPDATE_STATES},
    partitions={UPDATE_ADMITTED: {"admitted": ADMITTED, "refused": STALE}},
)


def test_the_completeness_table_is_the_specs_design() -> None:
    table = sc.completeness_table(UPDATE, ABSTRACTION)
    finals = {
        (state, event, label): f"ignored in {state}"
        for state in ("APPLIED", "REFUSED")
        for event, label in [
            (UPDATE_ADMITTED, "admitted"),
            (UPDATE_ADMITTED, "refused"),
            (ALL_STOPPED_OR_NONE, "any"),
            (EXIT_RECORDED, "any"),
        ]
    }
    assert table == {
        ("IDLE", UPDATE_ADMITTED, "admitted"): "update.01",
        ("IDLE", UPDATE_ADMITTED, "refused"): "update.02",
        ("STOPPING", ALL_STOPPED_OR_NONE, "any"): "update.03",
        ("STOPPING", EXIT_RECORDED, "any"): "update.04",
        **finals,
    }


def test_an_excluded_cell_raises_rather_than_being_ignored() -> None:
    assert len(UPDATE.exclusions) == 3
    for exclusion in UPDATE.exclusions:
        executor = _executor(exclusion.state)
        with pytest.raises(UnhandledEvent):
            executor.dispatch("u", ChartEvent(exclusion.event, ADMITTED))


@dataclass
class Effects:
    """The tier's effects in order. `apply` runs `then`, so a test can apply for real."""

    log: list[tuple[Any, ...]] = field(default_factory=list)
    then: Any = None

    def journal_intent(self, token: str, stop_set: tuple[str, ...]) -> None:
        self.log.append((JOURNAL_INTENT, token, stop_set))

    def stop_attempt(self, job: str, cause: str) -> None:
        self.log.append((STOP_ATTEMPT, job, cause))

    def apply(self, application: Application) -> None:
        self.log.append((APPLY, application.token))
        if self.then is not None:
            self.then(application)

    def journal_applied(self, rev: int, digest: str) -> None:
        self.log.append((JOURNAL_APPLIED, rev, digest))


def _application(token: str = TOKEN) -> Application:
    catalog = lower_source("insert_job: C\njob_type: c\ncommand: x\nmachine: m1\n")
    digest = catalog_hash_v2(catalog)
    return Application(token, 8, digest, digest, catalog, (), ("C",), (), (), ())


def _prepare(admitted: Admitted) -> Application:
    return _application(admitted.token)


def _hits(directory: Path) -> set[tuple[str, str, str]]:
    found = set()
    for path in directory.glob("hits-*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["machine"] == UPDATE.name:
                found.add((record["kind"], record["id"], record["detail"]))
    return found


def test_every_coverage_item_of_the_chart_is_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HITS_ENV, str(tmp_path))
    applied = UpdateTier(Effects(), _prepare)
    applied.admit(ADMITTED)
    applied.exit_recorded("A")
    applied.exit_recorded("B")
    refused = UpdateTier(Effects(), _prepare)
    refused.admit(STALE)
    for state in ("APPLIED", "REFUSED"):
        executor = _executor(state)
        for event in alphabet(UPDATE):
            executor.dispatch("u", ChartEvent(event, ADMITTED))
    required, forbidden = coverage_items(UPDATE)
    hits = _hits(tmp_path)
    assert set(required) <= hits
    assert not set(forbidden) & hits


# ---- the tier on its own


def test_an_admission_journals_the_intent_then_stops_each_job_and_applies_after_the_last() -> None:
    effects = Effects()
    tier = UpdateTier(effects, _prepare)
    assert tier.state == "IDLE" and tier.dispatch_filter == frozenset()
    tier.admit(ADMITTED)
    assert tier.state == "STOPPING"
    assert tier.open == {"A", "B"}
    assert tier.dispatch_filter == {"A", "B", "C"}
    assert effects.log == [
        (JOURNAL_INTENT, TOKEN, ("A", "B")),
        (STOP_ATTEMPT, "A", UPDATE_STOP),
        (STOP_ATTEMPT, "B", UPDATE_STOP),
    ]
    tier.exit_recorded("Z")  # not in the stop set: nothing
    tier.exit_recorded("A")
    assert tier.state == "STOPPING" and tier.open == {"B"}
    assert tier.application is None
    tier.exit_recorded("B")
    tier.exit_recorded("B")  # after the application: nothing
    assert tier.state == "APPLIED" and tier.dispatch_filter == frozenset()
    application = _application()
    assert tier.application == application
    assert effects.log[3:] == [(APPLY, TOKEN), (JOURNAL_APPLIED, 8, application.digest)]


def test_an_admission_with_nothing_to_stop_applies_at_once() -> None:
    effects = Effects()
    tier = UpdateTier(effects, _prepare)
    tier.admit(NONE_TO_STOP)
    assert tier.state == "APPLIED"
    assert [entry[0] for entry in effects.log] == [JOURNAL_INTENT, APPLY, JOURNAL_APPLIED]


def test_a_refusal_ends_the_update_with_no_effect() -> None:
    effects = Effects()
    tier = UpdateTier(effects, _prepare)
    tier.admit(Refused(TOKEN, WOULD_STOP_RUNNING, ("A",)))
    assert tier.state == "REFUSED"
    assert tier.refusal == Refused(TOKEN, WOULD_STOP_RUNNING, ("A",))
    assert effects.log == [] and tier.application is None


def test_a_stop_that_ends_at_once_reaches_the_application_inside_the_admission() -> None:
    effects = Effects()
    tier = UpdateTier(effects, _prepare)
    effects.stop_attempt = lambda job, cause: tier.exit_recorded(job)  # type: ignore[method-assign]
    tier.admit(ADMITTED)
    assert tier.state == "APPLIED"
    assert [entry[0] for entry in effects.log] == [JOURNAL_INTENT, APPLY, JOURNAL_APPLIED]


@pytest.mark.parametrize("still_open", [(), ("B",)], ids=["settled", "one_open"])
def test_a_crash_between_intent_and_application_resumes_to_applied(
    still_open: tuple[str, ...],
) -> None:
    effects = Effects()
    tier = UpdateTier(effects, _prepare)
    tier.resume(ADMITTED, still_open)
    if still_open:
        assert tier.state == "STOPPING" and tier.dispatch_filter == {"A", "B", "C"}
        tier.exit_recorded("B")
    assert tier.state == "APPLIED"
    assert [entry[0] for entry in effects.log] == [APPLY, JOURNAL_APPLIED]


# ---- the nightbank engine: the real oracle state and the real scheduler


@dataclass
class World:
    """One engine's state: the oracle, the scheduler, the catalog revision and the clock."""

    root: Path
    estate: Path
    oracle: Oracle
    settings: Mapping[str, object]
    scheduler: Scheduler
    rev: int = 1
    at: datetime = datetime(2026, 1, 6, 1, 0)
    #: the last admitted update, and its token while it stops (`admission_check`'s `open_update`)
    last: UpdateTier | None = None
    last_token: str | None = None

    @property
    def open_update(self) -> str | None:
        return self.last_token if self.last is not None and self.last.state == "STOPPING" else None

    def feed(self, kind: str, **payload: object) -> None:
        self.at += timedelta(seconds=1)
        self.oracle.feed(Event(at=self.at, kind=kind, payload=payload))  # type: ignore[arg-type]

    def row(self, job: str) -> JobRuntime:
        return self.oracle.store.job[job]

    def live(self) -> Live:
        return live_of(self.oracle, self.settings, self.rev)

    def site(self) -> dict[str, Any]:
        return {"properties": [self.root / "night.properties"], "environment": "sandbox"}

    def reread(self) -> tuple[RereadResult, dict[str, Any], CatalogIR]:
        result = reread(self.estate, self.live(), self.root / "plans", start=START, **self.site())
        document = read_plan(self.root / "plans", result.token)
        assert document is not None
        return result, document, load_estate(self.estate, **self.site()).catalog

    def apply(self, application: Application) -> None:
        apply_application(self.oracle, application, revision=self.rev)
        self.scheduler.replace(
            application.catalog,
            added=application.added,
            changed=application.changed,
            removed=application.removed,
            now=self.at,
            default_tz=application.timezone,
            tz_aliases=application.tz_aliases,
            semantics=application.switches,
        )
        self.settings = application.settings
        self.rev = application.rev

    def update(
        self, document: dict[str, Any], candidate: CatalogIR, *, stop_running: bool = False
    ) -> tuple[UpdateTier, Effects]:
        decision = admission_check(
            document,
            live=self.live(),
            candidate=candidate,
            stop_running=stop_running,
            open_update=self.open_update,
        )
        marked = load_exceptions(self.estate).names
        effects = Effects(then=self.apply)

        def prepare(admitted: Admitted) -> Application:
            return plan_application(
                self.oracle, candidate, document, marked, dict(admitted.stop_runs)
            )

        tier = UpdateTier(effects, prepare)
        tier.admit(decision)
        if isinstance(decision, Admitted):
            self.last, self.last_token = tier, document["token"]
        return tier, effects

    def edit(self, name: str, old: str, new: str) -> None:
        path = self.estate / name
        text = path.read_text()
        assert old in text
        path.write_text(text.replace(old, new, 1))

    def remove(self, name: str, job: str) -> str:
        """Remove JOB's definition from file NAME; answers the text removed."""
        path = self.estate / name
        text = path.read_text()
        found = re.search(rf"insert_job: {job}\n.*?(?=\ninsert_job:|/\*|\Z)", text, flags=re.S)
        assert found is not None
        path.write_text(text.replace(found.group(0), "", 1))
        return found.group(0)


def _world(tmp_path: Path, edit: Any = None) -> World:
    estate = tmp_path / "estate"
    estate.mkdir()
    for path in SMALL.glob("*.jil"):
        shutil.copy(path, estate)
    shutil.copy(SMALL / "estate.toml", estate)
    (tmp_path / "night.properties").write_text(PROPERTIES)
    if edit is not None:
        edit(estate)
    loaded = load_estate(estate, [tmp_path / "night.properties"], "sandbox")
    scheduler = Scheduler(loaded.catalog, start=START, default_tz="UTC")
    return World(tmp_path, estate, Oracle(loaded.catalog), loaded.settings, scheduler)


def _command_change(world: World, job: str) -> None:
    for path in world.estate.glob("*.jil"):
        text = path.read_text()
        if f"command: fakework {job} " in text:
            path.write_text(
                text.replace(f"command: fakework {job} ", f"command: fakework {job} -v ")
            )
            return
    raise AssertionError(job)


def _command(world: World, job: str) -> str:
    spec = world.oracle.catalog.jobs[job].exec_
    assert isinstance(spec, ExecSpec)
    return spec.command or ""


def test_a_stale_token_is_refused(tmp_path: Path) -> None:
    world = _world(tmp_path)
    _command_change(world, "OPS_SPOOL_C")
    _, document, candidate = world.reread()
    world.rev = 2
    tier, effects = world.update(document, candidate)
    assert tier.state == "REFUSED"
    assert tier.refusal == Refused(document["token"], STALE_PLAN)
    assert effects.log == []


def test_a_box_that_started_since_reread_refuses_the_plan(tmp_path: Path) -> None:
    world = _world(tmp_path)
    with (world.estate / "global.jil").open("a") as handle:
        handle.write(
            "\ninsert_job: ZZ_NEW_C\njob_type: c\nbox_name: OPS_B\ncommand: true\n"
            "machine: ops_util\nowner: ~{$OWNER}~\n"
        )
    result, document, candidate = world.reread()
    assert result.plan.added == ("ZZ_NEW_C",)
    world.feed("FORCE_STARTJOB", job="OPS_B")
    assert world.row("OPS_B").status == "RUNNING"
    tier, effects = world.update(document, candidate, stop_running=True)
    assert tier.refusal == Refused(document["token"], BOX_RUNNING, ("OPS_B",))
    assert effects.log == []


def _twenty_running(world: World) -> list[str]:
    commands = [name for name, job in world.oracle.catalog.jobs.items() if job.job_type == "CMD"]
    for name in commands[:25]:
        world.feed("FORCE_STARTJOB", job=name)
    running = [name for name in commands[:25] if world.row(name).status == "RUNNING"]
    assert len(running) >= 20
    return running[:20]


def test_a_plan_that_stops_running_jobs_is_refused_without_consent(tmp_path: Path) -> None:
    world = _world(tmp_path)
    running = _twenty_running(world)
    for job in running[3:5]:
        _command_change(world, job)
    result, document, candidate = world.reread()
    assert result.stop == tuple(sorted(running[3:5]))
    tier, effects = world.update(document, candidate)
    assert tier.refusal == Refused(document["token"], WOULD_STOP_RUNNING, result.stop)
    assert effects.log == []


def test_two_running_jobs_are_stopped_and_eighteen_untouched_with_consent(tmp_path: Path) -> None:
    world = _world(tmp_path)
    running = _twenty_running(world)
    touched = sorted(running[3:5])
    for job in touched:
        _command_change(world, job)
    _, document, candidate = world.reread()
    before = {name: world.row(name) for name in world.oracle.store.job}
    tier, effects = world.update(document, candidate, stop_running=True)
    assert tier.state == "STOPPING"
    assert effects.log == [
        (JOURNAL_INTENT, document["token"], tuple(touched)),
        *((STOP_ATTEMPT, job, UPDATE_STOP) for job in touched),
    ]
    # while it stops, nothing in the plan may start, and nothing has been applied
    assert tier.dispatch_filter == set(touched)
    assert "-v" not in _command(world, touched[0])
    untouched = [job for job in running if job not in touched]
    assert len(untouched) == 18
    tier.exit_recorded(untouched[0])  # an unrelated end moves nothing
    for job in touched:
        tier.exit_recorded(job)
    assert tier.state == "APPLIED"
    assert [entry[0] for entry in effects.log[3:]] == [APPLY, JOURNAL_APPLIED]
    assert effects.log[-1] == (JOURNAL_APPLIED, 2, catalog_hash_v2(candidate))
    assert tier.application is not None
    assert tier.application.stopped == tuple((job, "rerun") for job in touched)
    assert tier.application.changed == tuple(touched)
    # every row, the touched ones included, is the row it was: the oracle was told nothing
    assert {name: world.row(name) for name in world.oracle.store.job} == before
    assert all(world.row(job).status == "RUNNING" for job in running)
    assert "-v" in _command(world, touched[0])
    assert world.rev == 2


def test_the_dispatch_filter_names_the_added_changed_and_removed_jobs_while_stopping(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path)
    world.feed("FORCE_STARTJOB", job="OPS_HEARTBEAT_C")
    _command_change(world, "OPS_HEARTBEAT_C")
    world.remove("global.jil", "OPS_FAIL_ALERT_C")
    with (world.estate / "global.jil").open("a") as handle:
        handle.write(
            "\ninsert_job: ZZ_NEW_C\njob_type: c\nbox_name: OPS_B\ncommand: true\n"
            "machine: ops_util\nowner: ~{$OWNER}~\nstatus: ON_ICE\n"
        )
    _, document, candidate = world.reread()
    tier, _ = world.update(document, candidate, stop_running=True)
    assert tier.state == "STOPPING"
    assert tier.dispatch_filter == {"OPS_HEARTBEAT_C", "OPS_FAIL_ALERT_C", "ZZ_NEW_C"}
    assert "ZZ_NEW_C" not in world.oracle.store.job
    tier.exit_recorded("OPS_HEARTBEAT_C")
    assert tier.dispatch_filter == frozenset()
    # an added job is INACTIVE, with the flag its initial_status seeds (SEM-24)
    row = world.row("ZZ_NEW_C")
    assert (row.status, row.on_ice, row.on_hold, row.run_number) == ("INACTIVE", True, False, 0)
    assert "OPS_FAIL_ALERT_C" not in world.oracle.store.job


def test_a_changed_job_keeps_its_row_and_gets_a_rebuilt_plan_and_an_unchanged_one_its_tick(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path)
    world.feed("FORCE_STARTJOB", job="OPS_HEARTBEAT_C")
    world.feed("STATUS", job="OPS_HEARTBEAT_C", exit_code=0)
    world.feed("ON_HOLD", job="OPS_HEARTBEAT_C")
    world.scheduler.pop_due(world.at)
    row = world.row("OPS_HEARTBEAT_C")
    assert (row.status, row.on_hold, row.run_number) == ("SUCCESS", True, 1)
    upcoming = dict((job, tick) for tick, job in world.scheduler.upcoming())
    plan_before = world.scheduler._plans["AMER_EOD_B"]
    assert upcoming["OPS_HEARTBEAT_C"] == datetime(2026, 1, 6, 1, 15)
    world.edit("global.jil", "start_mins: 0, 15, 30, 45", "start_mins: 5, 35")
    result, document, candidate = world.reread()
    assert [change.name for change in result.plan.changed] == ["OPS_HEARTBEAT_C"]
    tier, _ = world.update(document, candidate)
    assert tier.state == "APPLIED"
    assert world.row("OPS_HEARTBEAT_C") == row
    after = dict((job, tick) for tick, job in world.scheduler.upcoming())
    assert after["OPS_HEARTBEAT_C"] == datetime(2026, 1, 6, 1, 5)
    assert after["AMER_EOD_B"] == upcoming["AMER_EOD_B"]
    assert world.scheduler._plans["AMER_EOD_B"] is plan_before
    assert {job for job in after if after[job] != upcoming[job]} == {"OPS_HEARTBEAT_C"}
    assert world.scheduler.catalog_hash == catalog_hash_v2(candidate)
    # history survives: the trace still holds the changed job's run
    assert any(entry.job == "OPS_HEARTBEAT_C" for entry in world.oracle.trace())


def test_a_held_member_edited_then_released_runs_and_globals_survive(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.feed("SET_GLOBAL", name="RECON_APAC", value="CLEAN")
    world.feed("FORCE_STARTJOB", job="SOD_B")
    world.feed("STATUS", job="SOD_PREFLIGHT_C", exit_code=0)
    held = world.row("SOD_APPROVE_C")
    assert (held.status, held.on_hold) == ("INACTIVE", True)
    world.edit("global.jil", "fakework SOD_APPROVE_C --sleep 2", "fakework SOD_APPROVE_C --sleep 3")
    result, document, candidate = world.reread()
    assert [change.name for change in result.plan.changed] == ["SOD_APPROVE_C"]
    assert result.stop == ()
    tier, effects = world.update(document, candidate)
    assert tier.state == "APPLIED"
    assert [entry[1] for entry in effects.log if entry[0] == STOP_ATTEMPT] == []
    assert world.row("SOD_APPROVE_C") == held
    assert world.row("SOD_B").status == "RUNNING"
    assert world.oracle.store.global_value("RECON_APAC") == "CLEAN"
    world.feed("OFF_HOLD", job="SOD_APPROVE_C")
    released = world.row("SOD_APPROVE_C")
    assert (released.status, released.on_hold, released.run_number) == ("RUNNING", False, 1)
    assert "--sleep 3" in _command(world, "SOD_APPROVE_C")


def test_a_removed_jobs_tombstone_keeps_its_hold_and_shows_on_re_add(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    world.feed("FORCE_STARTJOB", job="OPS_FAIL_ALERT_C")
    world.feed("STATUS", job="OPS_FAIL_ALERT_C", exit_code=0)
    world.feed("ON_HOLD", job="OPS_FAIL_ALERT_C")
    seen = world.oracle.store.revision("job:OPS_FAIL_ALERT_C")
    text = world.remove("global.jil", "OPS_FAIL_ALERT_C")
    _, document, candidate = world.reread()
    tier, _ = world.update(document, candidate)
    assert tier.application is not None and tier.application.removed == ("OPS_FAIL_ALERT_C",)
    assert "OPS_FAIL_ALERT_C" not in world.oracle.store.job
    tombstone = world.oracle.store.tombstones["OPS_FAIL_ALERT_C"]
    assert (tombstone.status, tombstone.on_hold, tombstone.run_number) == ("SUCCESS", True, 1)
    with (world.estate / "global.jil").open("a") as handle:
        handle.write("\n" + text)
    assert world.live().tombstones == {"OPS_FAIL_ALERT_C": ("ON_HOLD",)}
    send = _send(world)
    assert reread_command(world.estate, run_root=tmp_path, send=send, **world.site()) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1:3] == [
        "added: OPS_FAIL_ALERT_C",
        "re-added with its earlier flags: OPS_FAIL_ALERT_C (ON_HOLD)",
    ]
    result, document, candidate = world.reread()
    assert result.plan.added == ("OPS_FAIL_ALERT_C",)
    assert document["restored"] == [{"name": "OPS_FAIL_ALERT_C", "flags": ["ON_HOLD"]}]
    tier, _ = world.update(document, candidate)
    assert tier.application is not None
    assert tier.application.restored == (("OPS_FAIL_ALERT_C", ("ON_HOLD",)),)
    row = world.row("OPS_FAIL_ALERT_C")
    assert (row.status, row.on_hold, row.run_number) == ("INACTIVE", True, 1)
    # an `expect` read before the removal no longer holds
    assert world.oracle.store.revision("job:OPS_FAIL_ALERT_C") == seen + 1
    assert "OPS_FAIL_ALERT_C" not in world.oracle.store.tombstones
    assert world.rev == 3
    # the rebuilt instance is the row's: the operator's release moves it as any other
    world.feed("OFF_HOLD", job="OPS_FAIL_ALERT_C")
    assert not world.row("OPS_FAIL_ALERT_C").on_hold


def _tokens(estate: Path) -> None:
    """A depletable resource that OPS_SPOOL_C takes two units of."""
    infra = estate / "infra.jil"
    infra.write_text(infra.read_text() + "\ninsert_resource: TOKENS\nres_type: D\namount: 5\n")
    path = estate / "global.jil"
    text = path.read_text()
    path.write_text(
        text.replace("job_terminator: 1\n", "job_terminator: 1\nresources: (TOKENS, QUANTITY=2)\n")
    )


def test_consumed_capacity_survives_a_change_that_drops_the_resource(tmp_path: Path) -> None:
    world = _world(tmp_path, _tokens)
    world.feed("FORCE_STARTJOB", job="OPS_SPOOL_C")
    world.feed("STATUS", job="OPS_SPOOL_C", exit_code=0)
    assert dict(world.oracle.store.consumed) == {"r:TOKENS": 2}
    world.edit("global.jil", "resources: (TOKENS, QUANTITY=2)\n", "")
    world.edit("infra.jil", "\ninsert_resource: TOKENS\nres_type: D\namount: 5\n", "")
    _, document, candidate = world.reread()
    tier, _ = world.update(document, candidate)
    assert tier.application is not None and tier.application.changed == ("OPS_SPOOL_C",)
    assert dict(world.oracle.store.consumed) == {"r:TOKENS": 2}
    assert world.row("OPS_SPOOL_C").status == "SUCCESS"


def test_a_removed_running_job_is_stopped_not_rerun_and_its_spent_units_stay_spent(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path, _tokens)
    world.feed("FORCE_STARTJOB", job="OPS_SPOOL_C")
    assert world.row("OPS_SPOOL_C").reservations
    world.remove("global.jil", "OPS_SPOOL_C")
    _, document, candidate = world.reread()
    tier, effects = world.update(document, candidate, stop_running=True)
    assert effects.log[1] == (STOP_ATTEMPT, "OPS_SPOOL_C", UPDATE_STOP)
    tier.exit_recorded("OPS_SPOOL_C")
    assert tier.application is not None
    assert tier.application.stopped == (("OPS_SPOOL_C", "none"),)
    tombstone = world.oracle.store.tombstones["OPS_SPOOL_C"]
    assert (tombstone.status, tombstone.reservations) == ("RUNNING", ())
    assert dict(world.oracle.store.consumed) == {"r:TOKENS": 2}


def test_a_marked_stopped_job_is_held_by_the_interruption_rule(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.feed("FORCE_STARTJOB", job="OPS_HEARTBEAT_C")
    _command_change(world, "OPS_HEARTBEAT_C")
    (world.estate / "non-idempotent.toml").write_text(
        '[jobs]\n"OPS_HEARTBEAT_C" = "pages the operator twice"\n'
    )
    _, document, candidate = world.reread()
    tier, _ = world.update(document, candidate, stop_running=True)
    tier.exit_recorded("OPS_HEARTBEAT_C")
    assert tier.application is not None
    assert tier.application.stopped == (("OPS_HEARTBEAT_C", "hold"),)


def test_an_application_refuses_a_live_catalog_it_was_not_computed_for(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.remove("global.jil", "OPS_FAIL_ALERT_C")
    _, document, candidate = world.reread()
    other = {**document, "removed": []}
    with pytest.raises(ValueError, match="does not describe the live catalog"):
        plan_application(world.oracle, candidate, other, (), {})
    application = plan_application(world.oracle, candidate, document, (), {})
    with pytest.raises(ValueError, match="the engine is at revision 2"):
        apply_application(world.oracle, application, revision=2)
    assert "OPS_FAIL_ALERT_C" in world.oracle.catalog.jobs
    apply_application(world.oracle, application, revision=1)
    with pytest.raises(ValueError, match="moved since it was computed"):
        apply_application(world.oracle, application, revision=1)


def _monthly_in_the_base_zone(estate: Path) -> None:
    """OPS_MONTHLY_ATTRIB_C reads its 06:00 in the estate's base zone."""
    path = estate / "global.jil"
    old = 'start_times: "06:00"\ntimezone: Europe/Zurich\n'
    assert old in path.read_text()
    path.write_text(path.read_text().replace(old, 'start_times: "06:00"\n', 1))


def test_a_settings_only_edit_swaps_the_zone_and_the_switches(tmp_path: Path) -> None:
    world = _world(tmp_path, _monthly_in_the_base_zone)
    assert world.scheduler.default_tz == "UTC"
    assert world.scheduler.semantics.dst_start_times == "vendor"
    ticks = dict((job, tick) for tick, job in world.scheduler.upcoming())
    assert ticks["OPS_MONTHLY_ATTRIB_C"] == datetime(2026, 1, 30, 6, 0)
    rows = {name: world.row(name) for name in world.oracle.store.job}
    world.edit("estate.toml", 'timezone = "UTC"', 'timezone = "America/New_York"')
    with (world.estate / "estate.toml").open("a") as handle:
        handle.write('\n[semantics]\n"dst-start-times" = "fold0"\n')
    result, document, candidate = world.reread()
    assert len(result.plan.changed) == len(candidate.jobs)
    assert result.plan.added == result.plan.removed == ()
    tier, _ = world.update(document, candidate)
    assert tier.state == "APPLIED"
    assert world.scheduler.default_tz == "America/New_York"
    assert world.scheduler.semantics.dst_start_times == "fold0"
    assert world.oracle.semantics.dst_start_times == "fold0"
    assert world.settings["timezone"] == "America/New_York"
    # the rebuilt plan reads 06:00 in the new zone: 11:00 UTC in January
    ticks = dict((job, tick) for tick, job in world.scheduler.upcoming())
    assert ticks["OPS_MONTHLY_ATTRIB_C"] == datetime(2026, 1, 30, 11, 0)
    assert world.live().settings == document["settings"]
    assert {name: world.row(name) for name in world.oracle.store.job} == rows


def test_tombstones_travel_in_the_carried_rows_and_restore(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.feed("ON_HOLD", job="OPS_FAIL_ALERT_C")
    world.remove("global.jil", "OPS_FAIL_ALERT_C")
    _, document, candidate = world.reread()
    world.update(document, candidate)
    kept = dict(world.oracle.store.tombstones)
    assert set(kept) == {"OPS_FAIL_ALERT_C"}
    carried = CarriedRows(tombstones=kept, period_id=2)
    again = CarriedRows.model_validate(decode(canonical_bytes(carried.model_dump(mode="json"))))
    assert again == carried
    restored = Oracle(candidate, carried=again)
    assert dict(restored.store.tombstones) == kept
    assert "OPS_FAIL_ALERT_C" not in restored.store.job


def test_the_catalog_hash_is_the_one_the_scheduler_pins(tmp_path: Path) -> None:
    world = _world(tmp_path)
    catalog = world.oracle.catalog
    assert catalog_hash_v2(catalog) == run_root.catalog_hash_v2(catalog)
    assert catalog_hash_v2(catalog) == world.scheduler.catalog_hash


def test_one_update_is_in_flight_at_a_time(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.feed("FORCE_STARTJOB", job="OPS_HEARTBEAT_C")
    _command_change(world, "OPS_HEARTBEAT_C")
    _, first_doc, first = world.reread()
    tier, _ = world.update(first_doc, first, stop_running=True)
    assert world.open_update == first_doc["token"]
    _command_change(world, "OPS_SPOOL_C")
    _, second_doc, second = world.reread()
    other, effects = world.update(second_doc, second, stop_running=True)
    assert other.refusal == Refused(second_doc["token"], UPDATE_IN_PROGRESS, (first_doc["token"],))
    assert effects.log == []
    # the open tier answers a second admission the same way and keeps its filter
    again = tier.admit(Admitted(second_doc["token"], (), frozenset({"OPS_SPOOL_C"})))
    assert again == Refused(second_doc["token"], UPDATE_IN_PROGRESS, (first_doc["token"],))
    assert tier.state == "STOPPING" and tier.dispatch_filter == {"OPS_HEARTBEAT_C"}


def test_a_stopped_job_the_oracle_ended_meanwhile_is_answered_none(tmp_path: Path) -> None:
    world = _world(tmp_path)
    for job in ("OPS_HEARTBEAT_C", "OPS_SPOOL_C"):
        world.feed("FORCE_STARTJOB", job=job)
        _command_change(world, job)
    _, document, candidate = world.reread()
    tier, _ = world.update(document, candidate, stop_running=True)
    world.feed("STATUS", job="OPS_SPOOL_C", exit_code=0)  # a natural end beat the stop
    world.feed("FORCE_STARTJOB", job="OPS_FAIL_ALERT_C")  # not in the stop set
    tier.exit_recorded("OPS_SPOOL_C")
    tier.exit_recorded("OPS_HEARTBEAT_C")
    assert tier.application is not None
    assert tier.application.stopped == (("OPS_HEARTBEAT_C", "rerun"), ("OPS_SPOOL_C", "none"))


def test_a_stopped_run_the_oracle_replaced_meanwhile_is_answered_none(tmp_path: Path) -> None:
    world = _world(tmp_path)
    world.feed("FORCE_STARTJOB", job="OPS_HEARTBEAT_C")
    _command_change(world, "OPS_HEARTBEAT_C")
    _, document, candidate = world.reread()
    tier, _ = world.update(document, candidate, stop_running=True)
    world.feed("KILLJOB", job="OPS_HEARTBEAT_C")
    world.feed("FORCE_STARTJOB", job="OPS_HEARTBEAT_C")
    row = world.row("OPS_HEARTBEAT_C")
    assert (row.status, row.run_number) == ("RUNNING", 2)
    tier.exit_recorded("OPS_HEARTBEAT_C")
    assert tier.application is not None
    assert tier.application.stopped == (("OPS_HEARTBEAT_C", "none"),)


def test_a_dropped_depletable_keeps_its_type_so_its_spent_units_stay_spent(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path, _tokens)
    world.feed("FORCE_STARTJOB", job="OPS_SPOOL_C")
    world.edit("global.jil", "resources: (TOKENS, QUANTITY=2)\n", "")
    world.edit("infra.jil", "\ninsert_resource: TOKENS\nres_type: D\namount: 5\n", "")
    _, document, candidate = world.reread()
    tier, _ = world.update(document, candidate, stop_running=True)
    tier.exit_recorded("OPS_SPOOL_C")
    assert tier.state == "APPLIED"
    assert world.row("OPS_SPOOL_C").reservations  # the live run still holds its units
    world.feed("STATUS", job="OPS_SPOOL_C", exit_code=0)
    assert (world.row("OPS_SPOOL_C").reservations, dict(world.oracle.store.consumed)) == (
        (),
        {"r:TOKENS": 2},
    )
    world.feed("RELEASE_RESOURCE", job="OPS_SPOOL_C")
    assert dict(world.oracle.store.consumed) == {"r:TOKENS": 2}
    _tokens(world.estate)
    _, document, candidate = world.reread()
    tier, _ = world.update(document, candidate)
    assert tier.state == "APPLIED"
    assert "TOKENS" in world.oracle.catalog.resources
    assert dict(world.oracle.store.consumed) == {"r:TOKENS": 2}


def test_a_raise_while_applying_propagates_and_resume_applies(tmp_path: Path) -> None:
    def broken(admitted: Admitted) -> Application:
        raise RuntimeError("disk full")

    crashed = UpdateTier(Effects(), broken)
    with pytest.raises(RuntimeError, match="disk full"):
        crashed.admit(NONE_TO_STOP)
    effects = Effects()
    restarted = UpdateTier(effects, _prepare)
    restarted.resume(NONE_TO_STOP, ())
    assert restarted.state == "APPLIED"
    assert [entry[0] for entry in effects.log] == [APPLY, JOURNAL_APPLIED]


def _retype_heartbeat(world: World) -> None:
    world.remove("global.jil", "OPS_HEARTBEAT_C")
    with (world.estate / "global.jil").open("a") as handle:
        handle.write(
            "\ninsert_job: OPS_HEARTBEAT_C\njob_type: b\nowner: ~{$OWNER}~\n"
            "date_conditions: 1\ndays_of_week: all\nstart_mins: 0, 30\ntimezone: UTC\n"
        )


def test_a_running_job_whose_type_changes_is_stopped_retired_and_admitted(
    tmp_path: Path,
) -> None:
    world = _world(tmp_path)
    world.feed("FORCE_STARTJOB", job="OPS_HEARTBEAT_C")
    world.feed("ON_ICE", job="OPS_FAIL_ALERT_C")  # an unrelated flag stays where it is
    seen = world.oracle.store.revision("job:OPS_HEARTBEAT_C")
    _retype_heartbeat(world)
    result, document, candidate = world.reread()
    assert result.plan.added == result.plan.removed == ("OPS_HEARTBEAT_C",)
    assert result.stop == ("OPS_HEARTBEAT_C",)
    tier, effects = world.update(document, candidate, stop_running=True)
    assert effects.log[1] == (STOP_ATTEMPT, "OPS_HEARTBEAT_C", UPDATE_STOP)
    tier.exit_recorded("OPS_HEARTBEAT_C")
    assert tier.application is not None
    assert tier.application.stopped == (("OPS_HEARTBEAT_C", "none"),)
    row = world.row("OPS_HEARTBEAT_C")
    assert (row.status, row.run_number) == ("INACTIVE", 1)
    assert world.oracle.store.revision("job:OPS_HEARTBEAT_C") == seen + 1
    assert world.oracle.catalog.jobs["OPS_HEARTBEAT_C"].job_type == "BOX"
    assert "OPS_HEARTBEAT_C" not in world.oracle.store.tombstones
    world.feed("FORCE_STARTJOB", job="OPS_HEARTBEAT_C")
    assert world.row("OPS_HEARTBEAT_C").run_number == 2


# ---- the oracle's seam


def test_the_oracle_swaps_its_catalog_only_between_inputs(tmp_path: Path) -> None:
    world = _world(tmp_path)
    catalog = world.oracle.catalog
    store = world.oracle.store
    same: dict[str, Any] = {
        "semantics": world.oracle.semantics,
        "tz_aliases": None,
        "default_tz": None,
    }
    with pytest.raises(OracleError, match="inside an input"):
        with world.oracle.batch(world.at):
            world.oracle.replace_catalog(catalog, **same)
    store.begin_input()
    with pytest.raises(OracleError, match="inside an input"):
        store.retire_job("OPS_FAIL_ALERT_C", None)
    with pytest.raises(OracleError, match="inside an input"):
        store.admit_job("ZZ_NEW_C")
    store.commit_input()
    with pytest.raises(OracleError, match="has no row"):
        store.retire_job("NO_SUCH_JOB", None)
    with pytest.raises(OracleError, match="already has a row"):
        store.admit_job("OPS_FAIL_ALERT_C")
    assert world.oracle.replace_catalog(catalog, **same) == ((), ())


def test_a_removed_jobs_timers_and_queue_rank_go_with_its_row(tmp_path: Path) -> None:
    world = _world(tmp_path)
    for name in ("AMER_ACC_GL_CLOSE_C", "AMER_REC_POSN_C"):
        world.feed("FORCE_STARTJOB", job=name)
    assert world.row("AMER_REC_POSN_C").status == "QUE_WAIT"
    store = world.oracle.store
    store.enqueue_timer(
        world.at + timedelta(hours=1),
        Event(at=world.at, kind="TIMER", payload={"job": "AMER_REC_POSN_C"}),
    )
    tombstone = store.retire_job("AMER_REC_POSN_C", None)
    assert (tombstone.status, tombstone.waiter_seq) == ("QUE_WAIT", None)
    assert store.timers_for("AMER_REC_POSN_C") == []


_HOLDER = (
    "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1, FREE=A)\n\n"
)
_QUEUE = (
    "insert_resource: R\nres_type: R\namount: 1\n\n"
    + _HOLDER
    + "insert_job: b\njob_type: c\ncommand: x\nmachine: m1\nresources: (R, QUANTITY=1, FREE=A)\n\n"
    "insert_job: c\njob_type: c\ncommand: x\nmachine: m1\n"
)
_AT = datetime(2026, 7, 1, 1)


def _swap(oracle: Oracle, catalog: CatalogIR) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return oracle.replace_catalog(
        catalog, semantics=oracle.semantics, tz_aliases=None, default_tz=None
    )


def test_a_retired_holders_units_wake_the_waiter_at_the_next_input() -> None:
    oracle = Oracle(lower_source(_QUEUE))
    oracle.feed(Event(at=_AT, kind="FORCE_STARTJOB", payload={"job": "a"}))
    oracle.feed(Event(at=_AT, kind="FORCE_STARTJOB", payload={"job": "b"}))
    assert [oracle.store.job[j].status for j in "ab"] == ["RUNNING", "QUE_WAIT"]
    without_a = lower_source(_QUEUE.replace(_HOLDER, ""))
    assert _swap(oracle, without_a) == (("a",), ())
    assert oracle.store.job["b"].status == "QUE_WAIT"
    oracle.advance(_AT + timedelta(seconds=1))
    assert oracle.store.job["b"].status == "RUNNING"
    # the wake is owed once: a later swap with no waiter scans nothing
    assert _swap(oracle, without_a) == ((), ())
    oracle.advance(_AT + timedelta(seconds=2))
    assert oracle.store.job["b"].status == "RUNNING"


def test_the_owed_wake_travels_in_the_carried_rows(tmp_path: Path) -> None:
    """A snapshot taken between the swap and the next input keeps the owed scan."""
    oracle = Oracle(lower_source(_QUEUE))
    oracle.feed(Event(at=_AT, kind="FORCE_STARTJOB", payload={"job": "a"}))
    oracle.feed(Event(at=_AT, kind="FORCE_STARTJOB", payload={"job": "b"}))
    without_a = lower_source(_QUEUE.replace(_HOLDER, ""))
    _swap(oracle, without_a)
    store = oracle.store
    carried = CarriedRows(
        jobs=dict(store.job),
        globals_=dict(store.globals_),
        timers=tuple(store.timers()),
        timer_seq=store.timer_seq,
        consumed=dict(store.consumed),
        enqueue_counter=store.enqueue_counter,
        tombstones=dict(store.tombstones),
        wake_owed=oracle._wake_owed,
        now=_AT,
    )
    again = CarriedRows.model_validate(decode(canonical_bytes(carried.model_dump(mode="json"))))
    assert again == carried and again.wake_owed
    restored = Oracle(without_a, carried=again)
    assert restored.store.job["b"].status == "QUE_WAIT"
    restored.advance(_AT + timedelta(seconds=1))
    assert restored.store.job["b"].status == "RUNNING"
    assert not Oracle(without_a, carried=CarriedRows(now=_AT))._wake_owed


def test_a_refused_swap_writes_nothing_and_a_removed_name_takes_no_input() -> None:
    catalog = lower_source(_QUEUE)
    oracle = Oracle(catalog)
    oracle.feed(Event(at=_AT, kind="STATUS", payload={"job": "ghost", "status": "SUCCESS"}))
    assert "ghost" in oracle.store.job  # the oracle addresses a name with no catalog entry
    ghost = _QUEUE.replace("insert_job: c\n", "insert_job: ghost\n")
    with pytest.raises(OracleError, match="refused before any write"):
        _swap(oracle, lower_source(ghost))
    assert oracle.catalog is catalog and "c" in oracle.store.job
    assert oracle.store.tombstones == {}
    without_c = lower_source(_QUEUE.split("insert_job: c")[0])
    assert _swap(oracle, without_c) == (("c",), ())
    with pytest.raises(OracleError, match="removed by an update"):
        oracle.feed(Event(at=_AT, kind="STATUS", payload={"job": "c", "status": "SUCCESS"}))
    oracle.feed(Event(at=_AT, kind="STARTJOB", payload={"job": "c"}))  # as for an unknown name
    assert "c" not in oracle.store.job and "c" in oracle.store.tombstones


_WINDOWED = (
    "insert_job: w\njob_type: c\ncommand: x\nmachine: m1\ndate_conditions: 1\n"
    'days_of_week: all\nstart_times: "08:30"\nrun_window: "08:00-09:00"\n'
)


def test_the_base_zone_reaches_the_oracles_run_windows() -> None:
    catalog = lower_source(_WINDOWED)
    retained = Oracle(catalog, default_tz="UTC")
    before = retained.fork()
    retained.replace_catalog(
        catalog, semantics=retained.semantics, tz_aliases=None, default_tz="America/New_York"
    )
    fresh = Oracle(catalog, default_tz="America/New_York")
    at = datetime(2026, 1, 6, 13, 30)  # 08:30 in New York
    for oracle in (before, retained, fresh):
        oracle.feed(Event(at=at, kind="STARTJOB", payload={"job": "w"}))
    assert before.store.job["w"].status != "RUNNING"
    assert retained.store.job["w"].status == fresh.store.job["w"].status == "RUNNING"


# ---- the scheduler's selective replacement


#: the scheduler's settings when the estate names none: UTC, no map, the default switches
_NO_SETTINGS: dict[str, Any] = {"default_tz": None, "tz_aliases": None, "semantics": None}

_TINY = (
    "calendar: past\n01/02/2025 00:00\n\n"
    "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "08:00"\n\n'
    "insert_job: b\njob_type: c\ncommand: x\nmachine: m1\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "09:00"\n\n'
    "insert_job: c\njob_type: c\ncommand: x\nmachine: m1\n"
)


def _at(hour: int, minute: int = 0, day: int = 1) -> datetime:
    return datetime(2026, 7, day, hour, minute)


def _jobs(due: list[Event]) -> list[tuple[datetime, str]]:
    return [(ev.at, str(ev.payload["job"])) for ev in due]


def _extra(name: str, schedule: str) -> str:
    return (
        f"\ninsert_job: {name}\njob_type: c\ncommand: x\nmachine: m1\n"
        f"date_conditions: 1\n{schedule}\n"
    )


def test_replace_rebuilds_changed_and_added_plans_and_drops_removed_ones() -> None:
    sched = Scheduler(lower_source(_TINY), start=_at(0))
    candidate = lower_source(
        _TINY.replace('start_times: "08:00"', 'start_times: "10:00"')
        .replace('start_times: "09:00"', "")
        .replace("insert_job: c\n", "insert_job: d\n")
        + _extra("e", 'run_calendar: past\nstart_times: "08:00"')
        + _extra("f", 'days_of_week: all\nstart_times: "11:00"')
    )
    sched.replace(
        candidate,
        added=("d", "e", "f"),
        changed=("a", "b"),
        removed=("c",),
        now=_at(7),
        **_NO_SETTINGS,
    )
    # a: rebuilt from now; b: no ticks any more; e: an exhausted calendar, dormant; f: added
    assert sched.upcoming() == [(_at(10), "a"), (_at(11), "f")]
    assert sched.catalog_hash == catalog_hash_v2(candidate)


def test_an_old_tick_due_before_the_update_is_still_owed() -> None:
    text = _extra("q", "days_of_week: all\nstart_mins: 0, 15, 30, 45")
    catalog = lower_source(text)
    sched = Scheduler(catalog, start=_at(1))
    assert _jobs(sched.pop_due(_at(1))) == [(_at(1), "q")]
    # the engine is at 01:40 when the update applies; 01:15 and 01:30 were due and not popped
    changed = lower_source(text.replace("0, 15, 30, 45", "5, 50"))
    sched.replace(changed, added=(), changed=("q",), removed=(), now=_at(1, 40), **_NO_SETTINGS)
    assert _jobs(sched.pop_due(_at(1, 20))) == [(_at(1, 15), "q")]
    assert _jobs(sched.pop_due(_at(1, 40))) == [(_at(1, 30), "q")]
    # the new definition counts from 01:40: its 01:05 is never owed, its 01:50 is next
    assert _jobs(sched.pop_due(_at(2, 6))) == [(_at(1, 50), "q"), (_at(2, 5), "q")]


def test_an_owed_tick_at_the_instant_fires_once_and_an_unscheduled_job_keeps_its_owed_tick() -> (
    None
):
    catalog = lower_source(_TINY)
    sched = Scheduler(catalog, start=_at(0))
    sched.replace(catalog, added=(), changed=("a",), removed=(), now=_at(8), **_NO_SETTINGS)
    unscheduled = lower_source(_TINY.replace('start_times: "09:00"', ""))
    sched.replace(unscheduled, added=(), changed=("b",), removed=(), now=_at(9), **_NO_SETTINGS)
    assert _jobs(sched.pop_due(_at(9))) == [(_at(8), "a"), (_at(9), "b")]
    assert sched.upcoming() == [(_at(8, day=2), "a")]


def test_a_tick_consumed_ahead_of_the_clock_is_not_fired_again() -> None:
    catalog = lower_source(_TINY)
    sched = Scheduler(catalog, start=_at(0))
    assert _jobs(sched.pop_due(_at(10))) == [(_at(8), "a"), (_at(9), "b")]  # a resume sweep
    assert sched.pop_due(_at(7)) == []  # an earlier instant moves nothing
    sched.replace(catalog, added=(), changed=("a", "b"), removed=(), now=_at(7), **_NO_SETTINGS)
    assert sched.upcoming() == [(_at(8, day=2), "a"), (_at(9, day=2), "b")]


def test_with_nothing_popped_the_gap_from_the_reset_start_stays_owed() -> None:
    """Startup step 4 applies before the sweep: the old plan's ticks since the frontier stay
    owed, so the sweep drops and journals them (E9)."""
    catalog = lower_source(_TINY)
    sched = Scheduler(catalog, start=_at(8))
    sched.replace(catalog, added=(), changed=("a",), removed=(), now=_at(12), **_NO_SETTINGS)
    assert sched.upcoming()[0] == (_at(8), "a")
    assert _jobs(sched.pop_due(_at(12))) == [(_at(8), "a"), (_at(9), "b")]
    assert sched.upcoming() == [(_at(8, day=2), "a"), (_at(9, day=2), "b")]


def test_a_job_added_after_its_tick_waits_for_the_next_one() -> None:
    """Opus's case: j fired at 19:11; at 23:00 an update adds k at 21:00."""
    catalog = lower_source(_extra("j", 'days_of_week: all\nstart_times: "19:11"'))
    sched = Scheduler(catalog, start=_at(0))
    assert _jobs(sched.pop_due(_at(19, 11))) == [(_at(19, 11), "j")]
    added = lower_source(
        _extra("j", 'days_of_week: all\nstart_times: "19:11"')
        + _extra("k", 'days_of_week: all\nstart_times: "21:00"')
    )
    sched.replace(added, added=("k",), changed=(), removed=(), now=_at(23), **_NO_SETTINGS)
    assert sched.pop_due(_at(23)) == []
    assert (_at(21, day=2), "k") in sched.upcoming()


def test_a_changed_job_that_gains_a_tick_before_the_update_does_not_fire_it() -> None:
    catalog = lower_source(_TINY)
    sched = Scheduler(catalog, start=_at(0))
    assert _jobs(sched.pop_due(_at(9))) == [(_at(8), "a"), (_at(9), "b")]
    gains = lower_source(_TINY.replace('start_times: "08:00"', 'start_times: "08:00, 09:30"'))
    sched.replace(gains, added=(), changed=("a",), removed=(), now=_at(10), **_NO_SETTINGS)
    assert sched.pop_due(_at(10)) == []
    assert sched.upcoming()[0] == (_at(8, day=2), "a")


def test_a_consumed_tick_does_not_repeat_through_two_replacements_before_a_pop() -> None:
    """Astra's first case: A consumed at 08:00, then 08:00 -> 09:00 -> 08:00 at 08:00."""
    catalog = lower_source(_TINY)
    sched = Scheduler(catalog, start=_at(0))
    assert _jobs(sched.pop_due(_at(8))) == [(_at(8), "a")]
    nine = lower_source(_TINY.replace('start_times: "08:00"', 'start_times: "09:00"'))
    sched.replace(nine, added=(), changed=("a",), removed=(), now=_at(8), **_NO_SETTINGS)
    sched.replace(catalog, added=(), changed=("a",), removed=(), now=_at(8), **_NO_SETTINGS)
    assert _jobs(sched.pop_due(_at(8))) == []
    assert (_at(8, day=2), "a") in sched.upcoming()


def test_an_added_jobs_pending_tick_survives_a_second_replacement_before_a_pop() -> None:
    """Astra's second case, and M3: A consumed at 08:00; N added with a tick at 08:00, then
    replaced again at 08:00; b, changed, gains a tick at 08:00 its old plan never had."""
    catalog = lower_source(_TINY)
    sched = Scheduler(catalog, start=_at(0))
    assert _jobs(sched.pop_due(_at(8))) == [(_at(8), "a")]
    first = lower_source(_TINY + _extra("n", 'days_of_week: all\nstart_times: "08:00"'))
    sched.replace(first, added=("n",), changed=(), removed=(), now=_at(8), **_NO_SETTINGS)
    second = lower_source(
        _TINY.replace('start_times: "09:00"', 'start_times: "08:00"')
        + _extra("n", 'days_of_week: all\nstart_times: "08:00, 12:00"')
    )
    sched.replace(second, added=(), changed=("a", "b", "n"), removed=(), now=_at(8), **_NO_SETTINGS)
    assert _jobs(sched.pop_due(_at(8))) == [(_at(8), "b"), (_at(8), "n")]
    assert sched.pop_due(_at(8)) == []
    assert sched.upcoming()[:2] == [(_at(12), "n"), (_at(8, day=2), "a")]


def test_new_settings_with_a_job_left_unrebuilt_are_refused() -> None:
    catalog = lower_source(_TINY)
    sched = Scheduler(catalog, start=_at(0))
    before = sched.upcoming()
    moved = {**_NO_SETTINGS, "default_tz": "Europe/London"}
    with pytest.raises(EngineError, match="not rebuilt: b, c"):
        sched.replace(catalog, added=(), changed=("a",), removed=(), now=_at(0), **moved)
    assert (sched.default_tz, sched.upcoming()) == (None, before)
    sched.replace(catalog, added=(), changed=("a", "b", "c"), removed=(), now=_at(0), **moved)
    assert sched.default_tz == "Europe/London"
    assert sched.upcoming()[0] == (_at(7), "a")


def test_a_job_whose_type_changed_is_removed_and_added_in_the_schedule() -> None:
    catalog = lower_source(_TINY)
    sched = Scheduler(catalog, start=_at(0))
    retyped = lower_source(
        _TINY.replace(
            "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\n", "insert_job: a\njob_type: b\n"
        )
    )
    sched.replace(retyped, added=("a",), changed=(), removed=("a",), now=_at(0), **_NO_SETTINGS)
    assert sched.upcoming()[0] == (_at(8), "a")


def test_replace_refuses_names_that_do_not_match_the_catalog() -> None:
    catalog = lower_source(_TINY)
    sched = Scheduler(catalog, start=_at(0))
    with pytest.raises(EngineError, match="removed job is still in the catalog"):
        sched.replace(catalog, added=(), changed=(), removed=("c",), now=_at(0), **_NO_SETTINGS)
    with pytest.raises(EngineError, match="not in the catalog: z"):
        sched.replace(catalog, added=("z",), changed=(), removed=(), now=_at(0), **_NO_SETTINGS)


# ---- the commands


def _send(world: World, reply: UpdateReply | None = None) -> Any:
    calls: list[tuple[str, Mapping[str, object]]] = []

    def send(verb: str, args: Mapping[str, object]) -> object:
        calls.append((verb, dict(args)))
        return world.live() if verb == "catalog" else reply

    send.calls = calls  # type: ignore[attr-defined]
    return send


def test_reread_prints_the_plan_and_exits_0_when_it_stops_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    world.edit("global.jil", "start_mins: 0, 15, 30, 45", "start_mins: 5, 35")
    world.remove("global.jil", "OPS_FAIL_ALERT_C")
    send = _send(world)
    code = reread_command(world.estate, run_root=tmp_path, send=send, **world.site())
    out = capsys.readouterr().out.splitlines()
    assert code == 0
    assert send.calls == [("catalog", {})]
    token = out[-1].removeprefix("token: ")
    assert read_plan(tmp_path / "plans", token) is not None
    assert out[:6] == [
        "plan against catalog revision 1",
        "added: none",
        "changed: OPS_HEARTBEAT_C",
        "removed: OPS_FAIL_ALERT_C",
        "unchanged: 80 jobs",
        "stops running: none",
    ]
    assert not any(line.startswith("marked") for line in out)


def test_reread_exits_1_when_the_plan_stops_running_jobs_and_names_the_marked_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    world.feed("FORCE_STARTJOB", job="OPS_HEARTBEAT_C")
    world.feed("FORCE_STARTJOB", job="OPS_SPOOL_C")
    _command_change(world, "OPS_HEARTBEAT_C")
    _command_change(world, "OPS_SPOOL_C")
    world.edit("global.jil", "job_terminator: 1\n", "job_terminator: 1\nn_retrys: 2\n")
    (world.estate / "non-idempotent.toml").write_text('[jobs]\n"OPS_SPOOL_C" = "spools twice"\n')
    code = reread_command(world.estate, run_root=tmp_path, send=_send(world), **world.site())
    out = capsys.readouterr().out.splitlines()
    assert code == 1
    assert "stops running: OPS_HEARTBEAT_C, OPS_SPOOL_C" in out
    assert "marked non-idempotent among them: OPS_SPOOL_C (spools twice)" in out
    assert any(line.startswith("preflight WARN [n-retrys] OPS_SPOOL_C: ") for line in out)


def test_reread_exits_2_with_the_findings_when_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    world.edit("global.jil", "condition: s(SOD_PREFLIGHT_C)", "condition: s(NO_SUCH_JOB_C)")
    code = reread_command(world.estate, run_root=tmp_path, send=_send(world), **world.site())
    captured = capsys.readouterr()
    assert code == 2 and captured.out == ""
    assert captured.err.startswith("reread: ")
    (world.estate / "global.jil").write_text((SMALL / "global.jil").read_text())
    (world.estate / "non-idempotent.toml").write_text("[jobs]\nOPS_SPOOL_C = 3\n")
    assert reread_command(world.estate, run_root=tmp_path, send=_send(world), **world.site()) == 2
    assert "must be a string" in capsys.readouterr().err


def test_update_prints_the_decision_and_with_wait_the_application(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    world.feed("FORCE_STARTJOB", job="OPS_HEARTBEAT_C")
    _command_change(world, "OPS_HEARTBEAT_C")
    _, document, candidate = world.reread()
    token = document["token"]
    decision = admission_check(
        document, live=world.live(), candidate=candidate, stop_running=True, open_update=None
    )
    assert isinstance(decision, Admitted)
    send = _send(world, UpdateReply(decision))
    assert update_command(token, run_root=tmp_path, send=send, stop_running=True) == 0
    assert send.calls == [("update", {"token": token, "stop_running": True, "wait": False})]
    assert capsys.readouterr().out == f"admitted: plan {token}; stopping: OPS_HEARTBEAT_C\n"
    application = Application(
        token,
        2,
        "sha256:" + "0" * 64,
        document["live_digest"],
        candidate,
        ("ZZ_NEW_C",),
        ("OPS_HEARTBEAT_C",),
        (),
        (("ZZ_NEW_C", ("ON_HOLD", "ON_ICE")),),
        (("OPS_HEARTBEAT_C", "rerun"),),
    )
    send = _send(world, UpdateReply(decision, application))
    assert update_command(token, run_root=tmp_path, send=send, stop_running=True, wait=True) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"admitted: plan {token}; stopping: OPS_HEARTBEAT_C",
        f"applied: catalog revision 2 (sha256:{'0' * 64})",
        "added: ZZ_NEW_C",
        "changed: OPS_HEARTBEAT_C",
        "removed: none",
        "re-added with its earlier flags: ZZ_NEW_C (ON_HOLD, ON_ICE)",
        "stopped OPS_HEARTBEAT_C: rerun",
    ]


@pytest.mark.parametrize(
    ("refusal", "line"),
    [
        (Refused(TOKEN, STALE_PLAN), "update: refused stale_plan"),
        (Refused(TOKEN, BOX_RUNNING, ("OPS_B",)), "update: refused box_running: OPS_B"),
        (
            Refused(TOKEN, WOULD_STOP_RUNNING, ("A", "B")),
            "update: refused would_stop_running: A, B; --stop-running consents to stopping them",
        ),
    ],
    ids=["stale", "box", "would_stop"],
)
def test_update_exits_2_with_the_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], refusal: Refused, line: str
) -> None:
    world = _world(tmp_path)
    _, document, _ = world.reread()
    code = update_command(
        document["token"], run_root=tmp_path, send=_send(world, UpdateReply(refusal))
    )
    captured = capsys.readouterr()
    assert (code, captured.out, captured.err) == (2, "", line + "\n")


def test_update_exits_2_for_a_token_with_no_complete_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = _world(tmp_path)
    send = _send(world)
    assert update_command(TOKEN, run_root=tmp_path, send=send) == 2
    assert send.calls == []
    assert capsys.readouterr().err.startswith(f"update: no complete plan {TOKEN}")


def test_a_command_refuses_a_reply_of_the_wrong_shape(tmp_path: Path) -> None:
    world = _world(tmp_path)
    _, document, _ = world.reread()
    with pytest.raises(TypeError, match="not Live"):
        reread_command(world.estate, run_root=tmp_path, send=lambda verb, args: None)
    with pytest.raises(TypeError, match="not UpdateReply"):
        update_command(document["token"], run_root=tmp_path, send=lambda verb, args: None)
