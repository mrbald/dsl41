"""`update TOKEN` as a module: the admission, the declared `UPDATE` chart, its tier and the
application (spec-reload "`dsl41 update`").

An update applies exactly the plan `reread` stored, against the catalog revision `reread` saw.
It is durable, so it is a declared machine (DL-424): a crash between the journaled intent and the
application resumes it.

```text
STOPPING = IDLE + update_admitted [admitted]   // journal the intent; stop each job in the set
REFUSED  = IDLE + update_admitted [refused]    // the refusal is the reply
APPLIED  = STOPPING + all_stopped_or_none      // apply, then journal the application
STOPPING = STOPPING + exit_recorded            // one more down
```

The admission runs before the event is posted, in the engine's single writer, and its answer is
the event's payload: `Admitted` or `Refused`. The guards read only which one it is. The spec's
`plan.live_rev != catalog.rev` is the `stale_plan` refusal. The chart does no I/O and reads no
clock; `UpdateTier` posts its events and performs its intents through `UpdateIntents`, which the
engine binds to the store and to the attempt chart (`stop` with cause `update_stop`).

The application is computed (`plan_application`) and then applied to the oracle in memory
(`apply_application`, through `Oracle.replace_catalog`). Per job: unchanged, nothing; added, an
INACTIVE row; removed, the row ends into a tombstone that keeps its run number and flags, and a
later update that adds the name back carries them; changed, the definition is replaced in place
and the row keeps status, ON_HOLD, ON_ICE, ON_NOEXEC, run counters, run history and lookback.
Globals, consumed capacity and the clock are untouched; a resource a changed definition no
longer names keeps its consumed bucket. The schedule plans follow in `Scheduler.replace`.

A job the update stops takes the interruption rule once its attempt ends (spec-wiring 2b): a
removed job is not relaunched, a marked one is held, any other reruns under the new definition.
`Application.stopped` states that choice for each job in the stop set, for the reply.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

from dsl41.engine_charts import UPDATE_STOP, Intent
from dsl41.ir import CatalogIR
from dsl41.oracle import Oracle
from dsl41.oracle_state import JobRuntime
from dsl41.reload_diff import Change, Plan, catalog_hash_v2, retyped_jobs, running_boxes
from dsl41.reload_plan import STOPPABLE, Live, stop_set
from dsl41.semantics import SemanticSwitches
from dsl41.semantics import resolve as resolve_switches
from dsl41.state_machine import (
    Context,
    Effects,
    Event,
    Exclusion,
    Executor,
    Outcome,
    Region,
    Row,
    State,
    Statechart,
)
from dsl41.timezones import alias_table

#: The key of the one instance on a request's executor.
UPDATE_KEY: Final = "update"

# -- events

#: The admission's answer. The payload is `Admitted` or `Refused`.
UPDATE_ADMITTED: Final = "update_admitted"
#: The stop set is empty: at once after an admission with none, or when the last one ended.
ALL_STOPPED_OR_NONE: Final = "all_stopped_or_none"
#: One job of the stop set ended. The payload is the job's name.
EXIT_RECORDED: Final = "exit_recorded"

# -- intents, in the order an outcome lists them

#: Journal the update's intent: (token, stop set).
JOURNAL_INTENT: Final = "journal_intent"
#: Stop one job's attempt with cause `update_stop`. The detail is the job.
STOP_ATTEMPT: Final = "stop_attempt"
#: Apply the plan in memory. The detail is the token.
APPLY: Final = "apply"
#: Journal the application. The detail is the token.
JOURNAL_APPLIED: Final = "journal_applied"

# -- refusal codes (the wiring adds them to docs/control-protocol.md ss2)

STALE_PLAN: Final = "stale_plan"
UPDATE_IN_PROGRESS: Final = "update_in_progress"
BOX_RUNNING: Final = "box_running"
WOULD_STOP_RUNNING: Final = "would_stop_running"

#: The states of the chart's one region.
UPDATE_STATES: Final = ("IDLE", "STOPPING", "APPLIED", "REFUSED")

#: What the interruption rule chooses for a stopped job (spec-wiring 2b, the follow-up kinds).
type OnStop = Literal["rerun", "hold", "none"]


@dataclass(frozen=True)
class Admitted:
    """The admission's yes: the plan's token, the RUNNING or STARTING jobs it stops, sorted, and
    the names no dispatch may start while it stops (the plan's added, changed and removed)."""

    token: str
    stop_set: tuple[str, ...]
    dispatch_filter: frozenset[str]
    #: (job, run number) for each job of the stop set: the run its stop interrupts
    stop_runs: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class Refused:
    """The admission's no: the code and the names it concerns, sorted (none for `stale_plan`;
    the open update's token for `update_in_progress`)."""

    token: str
    code: Literal["stale_plan", "box_running", "would_stop_running", "update_in_progress"]
    names: tuple[str, ...] = ()


def plan_of(document: Mapping[str, Any], live: CatalogIR) -> Plan:
    """The `Plan` a stored `plan.json` describes, read against the live catalog it was diffed
    with: the live box of each removed or changed job and the removed boxes come from there."""
    removed = tuple(document["removed"])
    changed = tuple(
        Change(item["name"], item["old_hash"], item["new_hash"], item["reason"])
        for item in document["changed"]
    )
    prior = [(name, live.jobs[name].box.box_name) for name in removed]
    prior += [(change.name, live.jobs[change.name].box.box_name) for change in changed]
    return Plan(
        added=tuple(document["added"]),
        removed=removed,
        changed=changed,
        unchanged=tuple(document["unchanged"]),
        prior_boxes=tuple(sorted(prior)),
        edited_boxes=tuple(name for name in removed if live.jobs[name].job_type == "BOX"),
    )


def _touched(plan: Plan) -> frozenset[str]:
    return frozenset([*plan.added, *(change.name for change in plan.changed), *plan.removed])


def admission_check(
    plan_doc: Mapping[str, Any],
    *,
    live: Live,
    candidate: CatalogIR,
    stop_running: bool,
    open_update: str | None,
) -> Admitted | Refused:
    """Re-check what `reread` only previewed, against the engine's state now (spec-reload).

    One update is in flight at a time: while the update with token `open_update` stops, any
    other is `update_in_progress` naming it. `live` is the engine's catalog, revision and
    statuses. A plan diffed against another
    revision is `stale_plan`. A box the plan edits structurally that is RUNNING now is
    `box_running` with the boxes. The stop set is found again from the statuses now; without
    `stop_running` a non-empty one is `would_stop_running` with the names. Otherwise the plan is
    admitted with that stop set, and `stop_running` is the consent to it.
    """
    token = str(plan_doc["token"])
    if open_update is not None:
        return Refused(token, UPDATE_IN_PROGRESS, (open_update,))
    if plan_doc["live_rev"] != live.revision:
        return Refused(token, STALE_PLAN)
    plan = plan_of(plan_doc, live.catalog)
    boxes = running_boxes(plan, candidate, live.status)
    if boxes:
        return Refused(token, BOX_RUNNING, boxes)
    stops = stop_set(plan, live)
    if stops and not stop_running:
        return Refused(token, WOULD_STOP_RUNNING, stops)
    runs = tuple((job, live.runs.get(job, 0)) for job in stops)
    return Admitted(token, stops, _touched(plan), runs)


# -- the chart


@dataclass
class UpdateRecord:
    """The data of an update instance: its token, the jobs still to stop, and the refusal."""

    token: str | None = None
    open: set[str] = field(default_factory=set)
    refusal: Refused | None = None


def _admitted(ctx: Context) -> bool:
    return isinstance(ctx.event.payload, Admitted)


def _refused(ctx: Context) -> bool:
    return isinstance(ctx.event.payload, Refused)


def _begin_stopping(ctx: Context) -> Effects:
    """Keep the token and the stop set; journal the intent, then stop each job in the set."""
    record: UpdateRecord = ctx.data
    admitted: Admitted = ctx.event.payload
    record.token = admitted.token
    record.open = set(admitted.stop_set)
    return Effects(
        intents=(
            Intent(JOURNAL_INTENT, (admitted.token, admitted.stop_set)),
            *(Intent(STOP_ATTEMPT, job) for job in admitted.stop_set),
        )
    )


def _refuse(ctx: Context) -> Effects:
    """Keep the refusal; the reply is the tier's."""
    record: UpdateRecord = ctx.data
    refusal: Refused = ctx.event.payload
    record.token = refusal.token
    record.refusal = refusal
    return Effects()


def _one_down(ctx: Context) -> Effects:
    """One job of the stop set ended: it leaves the open set."""
    record: UpdateRecord = ctx.data
    record.open.discard(ctx.event.payload)
    return Effects()


def _apply(ctx: Context) -> Effects:
    """Every job of the stop set ended: apply the plan, then journal the application."""
    record: UpdateRecord = ctx.data
    return Effects(intents=(Intent(APPLY, record.token), Intent(JOURNAL_APPLIED, record.token)))


#: One update request, from its admission to its application or its refusal.
UPDATE: Final = Statechart(
    name="update",
    root=State(
        "update",
        regions=(
            Region(
                "phase",
                "IDLE",
                (
                    State("IDLE"),
                    State("STOPPING"),
                    State("APPLIED", final=True),
                    State("REFUSED", final=True),
                ),
            ),
        ),
    ),
    rows=(
        Row(
            "update.01",
            "IDLE",
            UPDATE_ADMITTED,
            "STOPPING",
            guard="admitted",
            action="begin_stopping",
        ),
        Row("update.02", "IDLE", UPDATE_ADMITTED, "REFUSED", guard="refused", action="refuse"),
        Row("update.03", "STOPPING", ALL_STOPPED_OR_NONE, "APPLIED", action="apply"),
        Row("update.04", "STOPPING", EXIT_RECORDED, kind="internal", action="one_down"),
    ),
    guards={"admitted": _admitted, "refused": _refused},
    actions={
        "begin_stopping": _begin_stopping,
        "refuse": _refuse,
        "one_down": _one_down,
        "apply": _apply,
    },
    clock="logical",
    exclusions=(
        Exclusion("IDLE", ALL_STOPPED_OR_NONE, "the tier posts it only after an admission"),
        Exclusion("IDLE", EXIT_RECORDED, "the tier posts it only while the update stops"),
        Exclusion("STOPPING", UPDATE_ADMITTED, "the tier admits its one request once"),
    ),
)


# -- the application


@dataclass(frozen=True)
class Application:
    """What applying a plan does: the new catalog with its revision and hash, the hash of the
    live catalog `reread` diffed against, the per-job sets, the flags a re-added name gets back
    from its tombstone, and, for each job of the admitted stop set, the interruption rule's
    answer (spec-wiring 2b steps 2 to 5)."""

    token: str
    rev: int
    digest: str
    live_digest: str
    catalog: CatalogIR = field(repr=False)
    added: tuple[str, ...]
    changed: tuple[str, ...]
    removed: tuple[str, ...]
    restored: tuple[tuple[str, tuple[str, ...]], ...]
    stopped: tuple[tuple[str, OnStop], ...]
    #: the candidate's estate settings (`reload_plan.estate_settings`), swapped with the catalog
    settings: Mapping[str, Any] = field(default_factory=dict)

    @property
    def timezone(self) -> str | None:
        """The estate's base zone, the scheduler's `default_tz`."""
        zone = self.settings.get("timezone")
        return zone if isinstance(zone, str) else None

    @property
    def tz_aliases(self) -> Mapping[str, str] | None:
        """The timezone map; None when the estate supplies none (DL-151)."""
        aliases = self.settings.get("timezone_aliases")
        return None if aliases is None else dict(aliases)

    @property
    def switches(self) -> SemanticSwitches:
        """The semantic switches the estate runs under (DL-252)."""
        return resolve_switches(self.settings.get("semantics") or {})


def flags_of(job: JobRuntime) -> tuple[str, ...]:
    return tuple(
        flag
        for flag, on in (
            ("ON_HOLD", job.on_hold),
            ("ON_ICE", job.on_ice),
            ("ON_NOEXEC", job.on_noexec),
        )
        if on
    )


def live_of(live_state: Oracle, settings: Mapping[str, object], revision: int) -> Live:
    """The engine's `Live` from its oracle: the catalog, each row's status, and the flags each
    tombstone kept, so `reread` can show what a re-added name gets back."""
    store = live_state.store
    return Live(
        live_state.catalog,
        settings,
        revision,
        {name: row.status for name, row in store.job.items()},
        {name: flags_of(row) for name, row in store.tombstones.items() if flags_of(row)},
        {name: row.run_number for name, row in store.job.items()},
    )


def _on_stop(
    job: str, run: int, live_state: Oracle, removed: Collection[str], marked: Collection[str]
) -> OnStop:
    """The interruption rule's answer for the stopped run `run` of `job` (spec-wiring 2b):
    removed, or a run that is not the oracle's current one in STARTING or RUNNING, is
    `none` (step 3); a marked job is `hold`; any other reruns."""
    row = live_state.store.job.get(job)
    if job in removed or row is None or row.status not in STOPPABLE or row.run_number != run:
        return "none"
    return "hold" if job in marked else "rerun"


def plan_application(
    live_state: Oracle,
    candidate: CatalogIR,
    plan: Mapping[str, Any],
    marked: Collection[str],
    stops: Mapping[str, int],
) -> Application:
    """The application of the stored plan `plan` to the oracle `live_state`, computed only.

    The plan must describe the oracle's catalog: its added and removed names are the two sides'
    difference plus the jobs whose type changed, and its changed and unchanged names are the
    rest, or ValueError. `marked` is the candidate's non-idempotent set and `stops` the
    admitted stop set with each job's stopped run; each gets the interruption rule's answer.
    """
    live_names = live_state.catalog.jobs.keys()
    cand_names = candidate.jobs.keys()
    retyped = retyped_jobs(live_state.catalog, candidate)
    added = tuple(sorted((cand_names - live_names) | retyped))
    removed = tuple(sorted((live_names - cand_names) | retyped))
    changed = tuple(sorted(item["name"] for item in plan["changed"]))
    kept = sorted([*changed, *plan["unchanged"]])
    if (tuple(plan["added"]), tuple(plan["removed"])) != (added, removed) or kept != sorted(
        (live_names & cand_names) - retyped
    ):
        raise ValueError(f"plan {plan['token']} does not describe the live catalog")
    tombstones = live_state.store.tombstones
    return Application(
        token=str(plan["token"]),
        rev=int(plan["live_rev"]) + 1,
        digest=catalog_hash_v2(candidate),
        live_digest=str(plan["live_digest"]),
        catalog=candidate,
        added=added,
        changed=changed,
        removed=removed,
        restored=tuple(
            (name, flags_of(tombstones[name]))
            for name in added
            if name in tombstones and flags_of(tombstones[name])
        ),
        stopped=tuple(
            (job, _on_stop(job, stops[job], live_state, removed, marked)) for job in sorted(stops)
        ),
        settings=dict(plan["settings"]),
    )


def apply_application(live_state: Oracle, application: Application, *, revision: int) -> None:
    """Swap the oracle's catalog for the application's (`Oracle.replace_catalog`), between
    inputs. `revision` is the engine's catalog revision: it must be the one the plan was diffed
    against, and the oracle's catalog must hash to the one `reread` recorded, or ValueError and
    nothing changes. The admission's one-update rule keeps both true; this is the belt."""
    if revision != application.rev - 1:
        raise ValueError(
            f"plan {application.token}: the engine is at revision {revision},"
            f" the plan was diffed against {application.rev - 1}"
        )
    if catalog_hash_v2(live_state.catalog) != application.live_digest:
        raise ValueError(f"plan {application.token}: the live catalog moved since it was computed")
    live_state.replace_catalog(
        application.catalog,
        semantics=application.switches,
        tz_aliases=alias_table(application.tz_aliases),
        default_tz=application.timezone,
    )


# -- the tier


class UpdateIntents(Protocol):
    """The effects an update asks for. The engine binds them; a test records them."""

    def journal_intent(self, token: str, stop_set: tuple[str, ...]) -> None: ...

    def stop_attempt(self, job: str, cause: str) -> None: ...

    def apply(self, application: Application) -> None: ...

    def journal_applied(self, rev: int, digest: str) -> None: ...


class UpdateTier:
    """One update request on its own executor: it posts the chart's events and performs the
    intents its outcomes report, in order, through `intents`.

    `prepare` computes the application from the admitted decision when the chart reaches
    APPLIED, so it reads the oracle as it is after the stops. `exit_recorded` takes the end of
    any attempt and posts only for a job still in the open set; when the set empties it posts
    `all_stopped_or_none` itself, as `admit` does at once for an empty stop set.

    A raise from `prepare` or from an intent propagates out of the call that posted the event.
    It is a crash, not a state: the engine dies, and startup step 4 resumes the update from its
    journaled intent (`resume`), so the chart's APPLIED after such a raise is never read.
    """

    def __init__(self, intents: UpdateIntents, prepare: Callable[[Admitted], Application]) -> None:
        self._intents = intents
        self._prepare = prepare
        self._admitted: Admitted | None = None
        self._record = UpdateRecord()
        self._filter: frozenset[str] = frozenset()
        self._application: Application | None = None
        self._executor = Executor()
        self._executor.start(UPDATE_KEY, UPDATE, self._record)
        self._executor.run()

    @property
    def state(self) -> str:
        (leaf,) = self._executor.active(UPDATE_KEY) & set(UPDATE_STATES)
        return leaf

    @property
    def open(self) -> frozenset[str]:
        """The jobs of the stop set whose attempt has not ended."""
        return frozenset(self._record.open)

    @property
    def refusal(self) -> Refused | None:
        return self._record.refusal

    @property
    def application(self) -> Application | None:
        """The application, once applied."""
        return self._application

    @property
    def dispatch_filter(self) -> frozenset[str]:
        """The names no dispatch may start: the plan's added, changed and removed while the
        update stops, none otherwise."""
        return self._filter if self.state == "STOPPING" else frozenset()

    def admit(self, decision: Admitted | Refused) -> Admitted | Refused:
        """Post the admission's answer and answer it. A tier that already took one answers
        `update_in_progress` naming its own token instead, and changes nothing. The dispatch
        filter is installed once the chart is in STOPPING, before any stop is performed."""
        if self.state != "IDLE":
            return Refused(decision.token, UPDATE_IN_PROGRESS, (str(self._record.token),))
        outcomes = self._executor.dispatch(UPDATE_KEY, Event(UPDATE_ADMITTED, decision))
        if isinstance(decision, Admitted):
            self._admitted = decision
            self._filter = decision.dispatch_filter
        self._perform_all(outcomes)
        self._settle()
        return decision

    def resume(
        self, admitted: Admitted, still_open: Collection[str], *, settle: bool = True
    ) -> None:
        """Restore an update whose intent is journaled and whose application is not (spec-crash
        step 4): STOPPING with the jobs whose attempt has not ended yet. With `settle` false,
        an empty set is left for the caller's `settle`, which applies where the caller
        chooses."""
        self._admitted = admitted
        self._record.token = admitted.token
        self._record.open = set(still_open)
        self._filter = admitted.dispatch_filter
        self._executor = Executor()
        self._executor.restore(UPDATE_KEY, UPDATE, {"update", "STOPPING"}, self._record)
        if settle:
            self._settle()

    def settle(self) -> None:
        """Apply once no job of the stop set is still open: `all_stopped_or_none`."""
        self._settle()

    def exit_recorded(self, job: str) -> None:
        """An attempt of `job` ended."""
        if self.state != "STOPPING" or job not in self._record.open:
            return
        self._send(EXIT_RECORDED, job)
        self._settle()

    def _settle(self) -> None:
        if self.state == "STOPPING" and not self._record.open:
            self._send(ALL_STOPPED_OR_NONE)

    def _send(self, name: str, payload: object = None) -> None:
        self._perform_all(self._executor.dispatch(UPDATE_KEY, Event(name, payload)))

    def _perform_all(self, outcomes: Sequence[Outcome]) -> None:
        for outcome in outcomes:
            for intent in outcome.intents:
                self._perform(intent)

    def _perform(self, intent: Intent) -> None:
        if intent.name == JOURNAL_INTENT:
            token, stops = intent.detail
            self._intents.journal_intent(token, stops)
        elif intent.name == STOP_ATTEMPT:
            self._intents.stop_attempt(intent.detail, UPDATE_STOP)
        elif intent.name == APPLY:
            assert self._admitted is not None  # APPLIED is reached from an admission only
            self._application = self._prepare(self._admitted)
            self._intents.apply(self._application)
        else:  # JOURNAL_APPLIED, which follows APPLY in one outcome
            assert intent.name == JOURNAL_APPLIED and self._application is not None
            self._intents.journal_applied(self._application.rev, self._application.digest)


__all__ = [
    "ALL_STOPPED_OR_NONE",
    "APPLY",
    "BOX_RUNNING",
    "EXIT_RECORDED",
    "JOURNAL_APPLIED",
    "JOURNAL_INTENT",
    "STALE_PLAN",
    "STOP_ATTEMPT",
    "UPDATE_IN_PROGRESS",
    "UPDATE",
    "UPDATE_ADMITTED",
    "UPDATE_KEY",
    "UPDATE_STATES",
    "WOULD_STOP_RUNNING",
    "Admitted",
    "Application",
    "OnStop",
    "Refused",
    "UpdateIntents",
    "UpdateRecord",
    "UpdateTier",
    "admission_check",
    "apply_application",
    "flags_of",
    "live_of",
    "plan_application",
    "plan_of",
]
