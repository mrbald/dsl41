"""The engine's `update` (spec-reload "`dsl41 update`", DL-427): the `update` chart's tier bound to
the engine's store, its attempt tier, its oracle and its scheduler.

An update request is one step of the engine's single writer (`Engine.request_update`). Its
admission reads the version 2 plan `reread` stored under `plans/<token>/`, loads its captured
main-config inputs (DL-486), and re-checks the runtime
preconditions against the engine's state now (`EngineUpdate.admit`). A refusal is the
reply and writes nothing. An admission appends `config intent` (the token, the stop set with the
attempt each stop interrupts, the dispatch filter, the admitted box roots (DL-489), and the
request's id and fingerprint), then
stops each attempt of the stop set with cause `update_stop` through the attempt tier. While the
update stops, no job of the plan's added, changed and removed sets is dispatched: a start the
oracle decides for one is held as `dispatch_pending`.

An attempt of the stop set that ends while the update stops keeps its outcome back: the outcome
and its follow-up plan are appended after the application, under the applied catalog and
marked set (spec-wiring 2b, the interruption rule's step 1). A task the attempt tier does not run
(a file watch) is cancelled; after the application it is launched again under its run id while
its job is in the catalog and its run is live, and closed with no plan otherwise.

When the last attempt has ended, the application swaps the oracle's catalog and settings
(`reload_update.apply_application`), the scheduler's plans (`Scheduler.replace`), the marked set
and the profile; one `config applied` record and a configuration row carry the new rev. The
engine then feeds one time advance, so the queue scan the swap owes runs before anything else
(DL-427), commits the outcomes it kept back, and releases what the dispatch filter held.

A restart that finds a `config intent` with no `config applied` resumes the update in STOPPING
(spec-restart §2): the catch-up observes the attempts it stops like any open one, and it applies
as a step of the loop once its stops are finished. The recovered outcomes of jobs outside its
stop set are fed under the old catalog before it; no job of its plan dispatches and no outcome
it keeps commits before it (spec-restart §6, ruling R1, DL-433).

An exact retry of an admitted request binds operation, baseline and actor (DL-463),
and is answered from its `config intent` record and, once applied, from its
`config applied` record. A refusal is not recorded, so a retry of one is admitted afresh.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from functools import partial
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

from dsl41.canon import digest
from dsl41.engine_charts import UPDATE_STOP, AttemptOutcome
from dsl41.idempotence import Exceptions
from dsl41.ir import CatalogIR
from dsl41.reload_plan import (
    BoxScope,
    Estate,
    Live,
    RereadRefused,
    RetiredPlan,
    estate_settings,
    read_plan,
    box_scope,
    ends_with_roots,
    marked_holds,
    root_starts,
)
from dsl41.reload_diff import catalog_hash_v2, diff
from dsl41.reload_update import (
    STALE_PLAN,
    UPDATE_IN_PROGRESS,
    Admitted,
    Application,
    OnStop,
    Refused,
    UpdateTier,
    admission_data,
    apply_application,
    live_of,
    plan_application,
    plan_of,
)
from dsl41.runner_attempts import UNSETTLED, FollowUp
from dsl41.runner_clock import EngineError
from dsl41.runner_codes import Code
from dsl41.runner_config import Configuration, RuntimeProfile, tz_aliases_of, with_host_settings
from dsl41.runner_journal import CONFIG_MARKER, config_marker, update_box_roots
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_store import Record, Store
from dsl41.runtime_policy import policy_values

if TYPE_CHECKING:  # annotation only: the engine imports this module
    from dsl41.runner import Engine
    from dsl41.runner_journal import Journal

#: The plans directory of a run root.
PLANS: Final = "plans"

#: The `config applied` fields an update adds to the segment's: what the application did.
APPLICATION_KEYS: Final = ("token", "added", "changed", "removed", "restored", "stopped")


@dataclass(frozen=True)
class UpdateRequest:
    """An internal prepared candidate plus the socket request's identity (DL-463)."""

    token: str
    expect: int
    baseline_id: str
    request_id: str
    claimed_actor: str | None = None

    @property
    def fingerprint(self) -> str:
        """What an exact retry repeats: everything but `--wait` and the request id."""
        return digest(
            {
                "cmd": "update",
                "baseline_id": self.baseline_id,
                "claimed_actor": self.claimed_actor,
            }
        )


@dataclass
class UpdateAnswer:
    """What the engine answers an update request: the admission's decision, or a refusal the
    chart does not make (`code` and `error`: no plan, a draining engine, a reused request id),
    and once the update applied, its `config applied` record's body."""

    decision: Admitted | Refused | None = None
    code: Code | None = None
    error: str | None = None
    application: dict[str, Any] | None = None
    #: resolved with the application's body once the update applied; None for a refusal
    applied: asyncio.Future[dict[str, Any]] | None = field(default=None, repr=False)
    #: Equal candidate's current revision/hash, without admission (DL-478).
    unchanged: dict[str, Any] | None = None


def settings_of(profile: RuntimeProfile, catalog: CatalogIR) -> dict[str, object]:
    """The estate settings an engine runs `catalog` under with `profile`, spelled as `reread`
    spells a candidate's (`reload_plan.estate_settings`), so an unchanged estate diffs equal."""
    return estate_settings(
        profile.default_tz,
        tz_aliases_of(profile),
        profile.semantics,
        catalog,
        job_env=profile.job_env,
        runtime_policy=profile.model_dump(),
        fw_default_interval_s=round(profile.fw_default_interval_us / 1_000_000),
    )


def profile_for(settings: Mapping[str, Any], base: RuntimeProfile) -> RuntimeProfile:
    """`base` running the estate settings `settings`: the base zone, the timezone map, the
    semantic switches, the job environment and the runtime policy follow the candidate; the
    machine identity stays (DL-469)."""
    aliases = settings.get("timezone_aliases")
    return base.model_copy(
        update={
            "default_tz": settings.get("timezone") or "UTC",
            "tz_aliases": dict(aliases or {}),
            "semantics": dict(settings.get("semantics") or {}),
            "job_env": dict(settings.get("job_env") or {}),
            **policy_values(settings),
            "fw_default_interval_us": settings.get("fw_default_interval_s", 60) * 1_000_000,
        }
    )


def application_body(application: Application) -> dict[str, Any]:
    """What a `config applied` record keeps of an update's application, beside the segment."""
    return {
        "token": application.token,
        "added": list(application.added),
        "changed": list(application.changed),
        "removed": list(application.removed),
        "restored": [[name, list(flags)] for name, flags in application.restored],
        "stopped": [[job, choice] for job, choice in application.stopped],
    }


class PlanUnreadable(Exception):
    """A plan the engine cannot take: no complete plan under the token, or one whose bytes do
    not load."""


@dataclass(frozen=True)
class OpenedPlan:
    """A stored plan as the engine takes it: `plan.json`, the candidate loaded from the plan's
    own bytes, and the candidate's marked set."""

    plan: dict[str, Any]
    estate: Estate
    marked: Exceptions

    def fingerprint(self) -> dict[str, str]:
        """Captured inputs at their original paths, for `spec_drift` (DL-65, DL-486)."""
        from dsl41.main_config import fingerprint

        return fingerprint(self.estate)


def open_plan(run_root: Path, token: str) -> OpenedPlan:
    """Load the captured main-config candidate after checking its plan (DL-486).

    Raises PlanUnreadable naming an incomplete, retired or invalid plan.
    """
    from dsl41.main_config import verified_from_parts

    plans = run_root / PLANS
    try:
        plan = read_plan(plans, token)
    except RereadRefused as exc:
        raise PlanUnreadable(str(exc)) from exc
    if plan is None:
        raise PlanUnreadable(f"no complete plan {token} under {plans}: run reread first")
    plan_dir = plans / token
    try:
        estate = verified_from_parts(
            [(p["name"], (plan_dir / p["name"]).read_bytes()) for p in plan["parts"]]
        )
        return OpenedPlan(plan, estate, Exceptions(estate.metadata["non_idempotent"]))
    except (RereadRefused, OSError, KeyError, TypeError) as exc:
        raise PlanUnreadable(f"plan {token} does not load: {exc}") from exc


class EngineUpdate:
    """The update tier's engine side (module docstring): the request in flight, the `UpdateTier`
    that runs it, the outcomes it keeps back and the tasks it cancelled. It is the tier's
    `UpdateIntents`."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        #: the update in STOPPING, or the last one that applied; None before the first
        self.tier: UpdateTier | None = None
        #: the outcomes of stop-set attempts that ended while the update stops, by run id
        self._kept: dict[str, AttemptOutcome] = {}
        #: kept attempts whose stop's cleanup is not done yet, and kept interruptions of jobs
        #: whose mark the plan changes, each by run id with its job
        self._cleaning: dict[str, str] = {}
        #: the stop-set jobs that ended during a restart's catch-up, for its first look
        self._ended_meanwhile: list[str] = []
        self._others: dict[str, str] = {}
        #: the jobs whose `non-idempotent.toml` mark the plan adds or removes
        self._moved: frozenset[str] = frozenset()
        #: the stop-set tasks the attempt tier does not run, cancelled: (job, run, run id)
        self._cancelled: list[tuple[str, int, str | None]] = []
        #: the request, plan, candidate and marked set of the update in flight
        self._request: UpdateRequest | None = None
        self._opened: OpenedPlan | None = None
        #: the attempt each stop of the update in flight interrupts, by job
        self._attempts: dict[str, str | None] = {}
        #: the admission of the update in flight
        self._admitted: Admitted | None = None
        #: the request's answer, resolved with the application
        self._answer: UpdateAnswer | None = None
        #: the last application's `config applied` body
        self._body: dict[str, Any] = {}
        #: the plans chosen at the application for the kept outcomes, by run id, and whether
        #: each cancelled task is taken up again, by job (`_choose_follow_ups`)
        self._chosen: dict[str, FollowUp] = {}
        self._takes_up: dict[str, bool] = {}
        self._box_scope = BoxScope((), frozenset())
        #: the jobs the dispatch filter held while the update stopped, released after it
        self._held: frozenset[str] = frozenset()
        #: called with each applied plan once the update applied live (the control server's
        #: `spec` texts and `spec_drift` baseline)
        self.listeners: list[Callable[[OpenedPlan], None]] = []
        #: True while the update tier is posted to inside a single-writer step: an attempt that
        #: ends then posts at once instead of queueing a step
        self.direct = False

    # ---- what the engine asks

    @property
    def stopping(self) -> bool:
        return self.tier is not None and self.tier.state == "STOPPING"

    @property
    def token(self) -> str | None:
        """The token of the update in STOPPING, None when none stops."""
        return None if not self.stopping or self._request is None else self._request.token

    def holds(self, job: str) -> bool:
        """Whether the dispatch filter keeps `job` from starting now."""
        return self.tier is not None and job in self.tier.dispatch_filter

    def keeps(self, run_id: str, job: str, outcome: AttemptOutcome) -> bool:
        """Whether the outcome of attempt `run_id` of `job` waits for the application, where
        the interruption rule's plan for it is chosen under the applied catalog and marked set
        (spec-wiring 2b, step 1): every attempt the update in STOPPING stops, and an interrupted
        attempt of a job whose mark the plan changes (`non-idempotent.toml`), which the update
        does not stop or wait for."""
        tier = self.tier
        if tier is None or tier.state != "STOPPING":
            return False
        if job in tier.open and self._attempts.get(job) == run_id:
            return True
        return job in self._moved and outcome.kind in ("stopped", "unobservable")

    def view(self) -> dict[str, Any] | None:
        """What `status` shows of the update in STOPPING: its token and the jobs still being
        stopped; None when none stops."""
        if not self.stopping or self.tier is None or self._request is None:
            return None
        return {"token": self._request.token, "stopping": sorted(self.tier.open)}

    def live(self) -> Live:
        engine = self.engine
        profile = engine.profile or RuntimeProfile()
        live = live_of(
            engine.oracle, settings_of(profile, engine.oracle.catalog), engine.config_rev
        )
        from dsl41.runner_environment import path_environment

        live = replace(live, path_environment=path_environment(engine.startup_env))
        if engine.source_capture:
            import base64
            from dsl41.canon import decode
            from dsl41.main_config import CAPTURE

            metadata = cast(
                dict[str, Any], decode(base64.b64decode(engine.source_capture[CAPTURE]))
            )
            live = replace(live, launch=engine.launch_settings or metadata["launch"])
        return live

    # ---- the request (one single-writer step)

    def admit(self, request: UpdateRequest) -> UpdateAnswer:
        """Admit or refuse one update request (module docstring). The answer's `applied` future
        resolves once the update applied, at once when nothing was to stop."""
        engine = self.engine
        # ControlServer admits updates only on stored start_run/resume_run engines (DL-427).
        assert engine.run_root is not None and engine.journal is not None
        # a retry is answered from its stored admission whatever the engine does now; only a
        # new update is refused while the engine drains
        prior = self._prior(request, engine.journal)
        if prior is not None:
            return prior
        if engine.draining:
            return UpdateAnswer(
                code="engine_draining",
                error="the engine is draining: it takes no update until it restarts",
            )
        open_token = self.token
        if open_token is not None:
            return self._decide(Refused(request.token, UPDATE_IN_PROGRESS, (open_token,)))
        try:
            opened = open_plan(engine.run_root, request.token)
        except PlanUnreadable as exc:
            return UpdateAnswer(code="invalid_argument", error=str(exc))
        if not isinstance(opened.plan.get("estate_dir"), str):
            # a version 2 plan always names its estate directory; one without it is damaged
            # and refused before any write (DL-486)
            return UpdateAnswer(
                code="stale_plan",
                error=f"plan {request.token} predates this engine: run update again",
            )
        if (
            request.expect != engine.config_rev
            or opened.plan.get("live_rev") != engine.config_rev
            or opened.plan.get("live_digest") != catalog_hash_v2(engine.oracle.catalog)
        ):
            # the precondition names the rev the caller read; the plan names the rev and the
            # catalog reread saw, checked before anything is written, so the application's own
            # check of them never meets a mismatch after the intent
            return self._decide(Refused(request.token, STALE_PLAN))
        from dsl41.runner_preflight import runtime_variable_preflight

        candidate = configuration_of(opened, engine.profile or RuntimeProfile())
        problems = runtime_variable_preflight(
            opened.estate.catalog, job_env=candidate.profile.job_env, startup_env=engine.startup_env
        )
        if problems:
            return UpdateAnswer(
                code="invalid_argument",
                error=f"preflight ERROR [runtime-variable]: {problems[0].message}",
            )
        applied = Configuration.from_bytes(
            engine.journal.store.get_config(engine.config_rev),
            where=f"configuration rev {engine.config_rev}",
        )
        # DL-478: only equal applied inputs and live policy complete without admission.
        if same_configuration(applied, candidate) and candidate.profile == engine.profile:
            with suppress(OSError):
                shutil.rmtree(engine.run_root / PLANS / request.token)
            return UpdateAnswer(
                Admitted(request.token, (), frozenset()),
                unchanged={"rev": engine.config_rev, "catalog_hash": applied.catalog_hash},
            )
        live = self.live()
        scope = box_scope(plan_of(opened.plan, live.catalog), live, opened.estate.catalog)
        decision = admission_data(
            opened.plan,
            live=live,
            candidate=opened.estate.catalog,
            scope=scope,
        )
        self._begin(request, opened, decision, scope)
        answer = self._answer = UpdateAnswer(decision)
        answer.applied = asyncio.get_running_loop().create_future()
        self._post(lambda tier: tier.admit(decision))
        return answer

    def _decide(self, refusal: Refused) -> UpdateAnswer:
        """A refusal, through a fresh instance of the chart (its REFUSED row)."""
        tier = UpdateTier(self, self._no_application)
        tier.admit(refusal)
        return UpdateAnswer(refusal)

    @staticmethod
    def _no_application(admitted: Admitted) -> Application:
        raise AssertionError(f"a refused update {admitted.token} reached its application")

    def _begin(
        self, request: UpdateRequest, opened: OpenedPlan, admitted: Admitted, scope: BoxScope
    ) -> None:
        attempts = {job: self.engine.attempt_of_run(job, run) for job, run in admitted.stop_runs}
        self._install(request, opened, admitted, attempts, {}, scope)

    def _install(
        self,
        request: UpdateRequest,
        opened: OpenedPlan,
        admitted: Admitted,
        attempts: dict[str, str | None],
        cleaning: dict[str, str],
        scope: BoxScope,
    ) -> None:
        """Install the update and its selected attempts before creating its tier (DL-432)."""
        engine = self.engine
        self._request, self._opened = request, opened
        self._kept, self._cancelled, self._cleaning, self._others = {}, [], {}, {}
        self._moved = frozenset(engine.marked ^ set(opened.marked.names))
        self._held = admitted.dispatch_filter
        self._admitted = admitted
        self._box_scope = scope
        self._attempts = attempts
        self._cleaning = cleaning
        self.tier = UpdateTier(self, self._prepare)

    def _post(self, post: Callable[[UpdateTier], object]) -> None:
        assert self.tier is not None
        direct, self.direct = self.direct, True
        try:
            post(self.tier)
        finally:
            self.direct = direct

    def _prior(self, request: UpdateRequest, journal: Journal) -> UpdateAnswer | None:
        """The answer an exact retry gets from the store, or a reused id's refusal; None for a
        request id the store does not hold. A sendevent's id the engine knows is a reuse too."""
        if self.engine.decisions.state(request.request_id) is not None:
            return _reused(request.request_id)
        found = journal.store.find_request(request.request_id)
        if found is None:
            return None
        body = found.payload
        if found.kind != "config" or body.get("fingerprint") != request.fingerprint:
            return _reused(request.request_id)
        admitted = admitted_of(body)
        answer = UpdateAnswer(admitted)
        answer.applied = asyncio.get_running_loop().create_future()
        if self.token == admitted.token:
            # still stopping, live or resumed by a restart: the retry is answered with the
            # admission now and with the application once it is done
            assert self._answer is not None and self._answer.applied is not None
            self._answer.applied.add_done_callback(
                lambda done: _settle(answer.applied, done.result())
            )
            return answer
        answer.application = next(
            (
                dict(record.payload)
                for _, record in journal.store.read_from(0)
                if record.kind == "config"
                and record.payload.get("event") == "applied"
                and record.payload.get("token") == admitted.token
            ),
            None,
        )
        if answer.application is not None:
            answer.applied.set_result(answer.application)
        return answer

    # ---- the restart

    def resume(self, intent: Mapping[str, Any], *, opened: OpenedPlan | None = None) -> None:
        """Restore the update a restart found in STOPPING from its `config intent` record (the
        fold, spec-restart §2): its dispatch filter holds from here, and it waits for each
        attempt of its stop set the open table holds, file watches excepted (the fold takes a
        watch up). The catch-up observes those attempts like any open one; their ends are steps
        of the single writer, and the catch-up's owed step queues the first look at a stop set
        already down (`look`), so the update applies in the loop (DL-432, spec-restart §6).
        Nothing is appended or applied here."""
        engine = self.engine
        assert engine.journal is not None and engine.run_root is not None
        admitted = admitted_of(intent)
        if opened is None:
            opened = reopen_plan(
                engine.run_root,
                admitted.token,
                configuration=Configuration(
                    catalog=engine.oracle.catalog, profile=engine.profile or RuntimeProfile()
                ),
            )
        request = UpdateRequest(
            admitted.token,
            int(intent.get("live_rev", engine.config_rev)),
            engine.baseline_id,
            str(intent.get("request_id", "")),
        )
        attempts = {str(entry["job"]): entry.get("attempt") for entry in intent.get("stop") or ()}
        table = engine.journal.open.attempts
        catalog = engine.oracle.catalog.jobs
        still = {
            job: run_id
            for job, run_id in attempts.items()
            if run_id in table and catalog[job].job_type != "FW"  # the fold takes a watch up
        }
        # an attempt whose outcome is in while its cleanup was cut short is down once the
        # catch-up's cleanup step has finished it (`cleaned`)
        cleaning = {run_id: job for job, run_id in still.items() if table[run_id].closing}
        live = self.live()
        scope = box_scope(
            plan_of(opened.plan, live.catalog),
            live,
            opened.estate.catalog,
            roots=update_box_roots(intent),
        )
        self._install(request, opened, admitted, attempts, cleaning, scope)
        # the request's own answer: a retry under its id while it still stops waits on it
        self._answer = UpdateAnswer(admitted)
        self._answer.applied = asyncio.get_running_loop().create_future()
        self._post(lambda tier: tier.resume(admitted, still.keys()))

    def look(self) -> None:
        """Queue the loop's first look at a resumed update's stop set, after every catch-up
        step: it takes the stops that ended during the catch-up and applies the update once
        none is left, so the recovered outcomes of jobs outside the stop set are fed first
        (DL-432, spec-restart §6)."""
        if self.tier is not None:
            self.engine.post_update_exit(None)

    # ---- an attempt of the stop set ended

    def keep(self, run_id: str, job: str, outcome: AttemptOutcome) -> None:
        """Keep an ended attempt's outcome for the application (`keeps`). A stop-set attempt
        is down once its stop is finished (`Engine.stop_done`: the ladder done, nothing of it
        left), which may come after its record (`cleaned`); any other is only kept."""
        self._kept[run_id] = outcome
        if self._attempts.get(job) != run_id:
            self._others[run_id] = job
        elif self.engine.stop_done(run_id):
            self.ended(job)
        else:
            self._cleaning[run_id] = job  # down once `cleaned`

    def cleaned(self, run_id: str) -> None:
        """An attempt's ladder ended or its held group is gone: a kept stop-set attempt whose
        stop is now finished is down."""
        job = self._cleaning.get(run_id)
        if job is not None and self.engine.stop_done(run_id):
            del self._cleaning[run_id]
            self.ended(job)

    def ended(self, job: str | None) -> None:
        """One job of the stop set is down (None: look whether the stop set is down): post it
        inside a step of the single writer, which finishes an application it reaches; from
        anywhere else, queue it as such a step (`Engine.post_update_exit`). The update applies
        only in the loop."""
        if not self.direct and self.engine.catching_up:
            # the catch-up's own first look takes it (`look`, spec-restart §6)
            # Only the loop's direct first look passes None; keep/cleaned name a job (DL-432).
            assert job is not None
            self._ended_meanwhile.append(job)
        elif not self.direct:
            self.engine.post_update_exit(job)
        elif job is None:
            ended, self._ended_meanwhile = self._ended_meanwhile, []
            for one in ended:
                self._post(partial(UpdateTier.exit_recorded, job=one))
        else:
            self._post(lambda tier: tier.exit_recorded(job))

    # ---- UpdateIntents

    def journal_intent(self, token: str, stop_set: tuple[str, ...]) -> None:
        engine = self.engine
        request = self._request
        assert request is not None and self.tier is not None
        assert engine.journal is not None  # `admit` and `resume` refuse an engine with none
        at = engine.now_admitted()
        runs = dict(self._runs())
        body = {
            "event": "intent",
            "at": at.isoformat(),
            "token": token,
            "live_rev": engine.config_rev,
            # the cause of a stop an attempt already takes is its first; the update's own stop
            # is the first only for an attempt that takes none yet
            "stop": [
                {
                    "job": job,
                    "run_number": runs[job],
                    "attempt": (attempt := self._attempts.get(job)),
                    "cause": (None if attempt is None else engine.stop_cause(attempt))
                    or UPDATE_STOP,
                }
                for job in stop_set
            ],
            "filter": sorted(self._held),
            "restart_boxes": list(self._box_scope.roots),
            "stop_running": True,
            "request_id": request.request_id,
            "fingerprint": request.fingerprint,
        }
        record = Record(at, "config", request.claimed_actor, request.request_id, body)
        engine.journal.append(record)

    def _runs(self) -> tuple[tuple[str, int], ...]:
        return () if self._admitted is None else self._admitted.stop_runs

    def stop_attempt(self, job: str, cause: str) -> None:
        """Stop the attempt the update interrupts. One the attempt tier runs gets `stop`; any
        other task is cancelled and taken up again after the application. A job with no
        attempt left to stop is down at once."""
        engine = self.engine
        tier = engine.attempts
        run_id = self._attempts.get(job)
        known = run_id is not None and tier.knows(run_id)
        if known and tier.state(str(run_id)) in UNSETTLED:
            tier.stop(str(run_id), cause)
            return
        if run_id is not None and not engine.stop_done(run_id):
            # ended before the update, its cleanup still running: down once that is finished
            self._cleaning[run_id] = job
            return
        if not known and (run := engine.cancel_and_reap(job, run_id)) is not None:
            self._cancelled.append((job, run, run_id))
        assert self.tier is not None
        self.tier.exit_recorded(job)

    def box_ends(self) -> list[str]:
        """End old box runs before application can choose replacement attempts (DL-463)."""
        assert self._opened is not None
        old = self.engine.oracle.catalog.jobs
        new = self._opened.estate.catalog
        names = ends_with_roots(self._box_scope, new)

        def order(name: str) -> tuple[bool, int, str]:
            depth, parent = 0, old[name].box.box_name
            while parent is not None:
                depth += 1
                parent = old[parent].box.box_name
            return old[name].job_type != "BOX", depth, name

        return sorted(
            (
                name
                for name in names
                if name in old
                and self.engine.oracle.store.job[name].status in {"RUNNING", "STARTING", "QUE_WAIT"}
            ),
            key=order,
        )

    def box_starts(self) -> list[tuple[Any, str]]:
        """Hold every non-INACTIVE marked member before an outer box starts (DL-463)."""
        engine = self.engine
        status = {name: row.status for name, row in engine.oracle.store.job.items()}
        held = marked_holds(self._box_scope, engine.marked, status)
        starts = root_starts(self._box_scope, engine.oracle.catalog, engine.marked)
        return [("ON_HOLD", name) for name in held] + [("FORCE_STARTJOB", box) for box in starts]

    def _prepare(self, _admitted: Admitted) -> Application:
        """The application, computed from the oracle as the stops left it, under the settings
        the engine itself read from the plan's bytes."""
        opened = self._opened
        assert opened is not None
        plan = {**opened.plan, "settings": dict(opened.estate.settings)}
        return plan_application(self.engine.oracle, opened.estate.catalog, plan)

    def apply(self, application: Application) -> None:
        """Swap the catalog, the settings, the plans, the marked set and the profile."""
        engine = self.engine
        assert self._opened is not None
        apply_application(engine.oracle, application, revision=engine.config_rev)
        at = engine.now_admitted(remember=False)
        if engine.scheduler is not None:
            engine.scheduler.replace(
                application.catalog,
                added=application.added,
                changed=application.changed,
                removed=application.removed,
                now=at,
                default_tz=application.timezone,
                tz_aliases=application.tz_aliases,
                semantics=application.switches,
            )
        engine.profile = profile_for(application.settings, engine.profile or RuntimeProfile())
        engine.marked = frozenset(self._opened.marked.names)
        engine.refit_adapters()
        engine.config_rev = application.rev

    def journal_applied(self, application: Application) -> Application:
        """The new configuration row and its `config applied` record."""
        engine = self.engine
        assert self._opened is not None
        journal = engine.journal
        assert journal is not None  # `admit` and `resume` refuse an engine with none
        configuration = configuration_of(self._opened, engine.profile or RuntimeProfile())
        store_config(journal.store, configuration, application.rev)
        engine.source_capture = dict(configuration.source_capture)
        engine.reread_marks = {}
        application = replace(application, stopped=self._choose_follow_ups())
        at = engine.now_admitted()
        body = {
            "event": "applied",
            "rev": application.rev,
            "at": at.isoformat(),
            "catalog_hash": configuration.catalog_hash,
            "runtime_hash": configuration.runtime_hash,
            "state_machine_version": STATE_MACHINE_VERSION,
            "baseline_id": journal.baseline_id,
            "clock_domain": journal.clock_domain,
            **application_body(application),
        }
        journal.append(Record(at, "config", None, None, body))
        # the trace says where the rev moved, as a replay of the record does
        engine.oracle.mark(at, CONFIG_MARKER, config_marker(application.rev))
        self._body = body
        return application

    def _choose_follow_ups(self) -> tuple[tuple[str, OnStop], ...]:
        """What follows each stop of the update, chosen once, under the applied catalog and
        marked set, before the record that names them (spec-wiring 2b, the interruption rule's
        step 1): a kept outcome's plan (`complete`, `rerun`, `hold` or `none`), which `finish`
        commits as chosen; `rerun` for a cancelled task taken up again; `none` for a task
        closed, and for a job the update found no attempt of (its held start goes under the new
        definition)."""
        engine = self.engine
        assert self._admitted is not None
        cancelled = {job: run for job, run, _ in self._cancelled}
        self._chosen = {}
        self._takes_up = {}
        stopped: list[tuple[str, OnStop]] = []
        for job in self._admitted.stop_set:
            attempt = self._attempts.get(job)
            if attempt is not None and attempt in self._kept:
                plan = self._chosen[attempt] = engine.plan_for(attempt, self._kept[attempt])
                stopped.append((job, plan.kind))
            elif job in cancelled:
                again = self._takes_up[job] = engine.takes_up(job, cancelled[job])
                stopped.append((job, "rerun" if again else "none"))
            else:
                stopped.append((job, "none"))
        for attempt in self._others:
            self._chosen[attempt] = engine.plan_for(attempt, self._kept[attempt])
        return tuple(stopped)

    # ---- after the application

    def finish(self) -> None:
        """What follows an application, once the time advance is fed or queued: the outcomes
        kept back are committed with their plans under the applied catalog, the cancelled tasks
        are taken up or closed, the held starts are released, and the request is answered."""
        engine = self.engine
        kept, self._kept = self._kept, {}
        # in the stop set's order, by job, whatever order the attempts ended in
        chosen, self._chosen = self._chosen, {}
        for job in sorted(self._attempts):
            attempt = self._attempts[job]
            if attempt in kept:
                engine.commit_outcome(attempt, kept[attempt], chosen[attempt])
        others, self._others = self._others, {}
        for attempt in sorted(others, key=others.__getitem__):
            engine.commit_outcome(attempt, kept[attempt], chosen[attempt])
        cancelled, self._cancelled = self._cancelled, []
        for job, run, task_id in cancelled:
            engine.resume_cancelled(job, run, task_id, take_up=self._takes_up[job])
        held, self._held = self._held, frozenset()
        for job in sorted(held):
            engine._release(job)
        assert self._opened is not None
        for listener in self.listeners:
            listener(self._opened)
        answer, self._answer = self._answer, None
        assert answer is not None  # `admit` and `resume` each set the request's answer
        _settle(answer.applied, dict(self._body))


def admitted_of(intent: Mapping[str, Any]) -> Admitted:
    """The admission a `config intent` record holds."""
    stop = [dict(entry) for entry in intent.get("stop") or ()]
    return Admitted(
        str(intent["token"]),
        tuple(str(entry["job"]) for entry in stop),
        frozenset(str(name) for name in intent.get("filter") or ()),
        tuple((str(entry["job"]), int(entry["run_number"])) for entry in stop),
    )


def _source_capture(estate: Estate) -> dict[str, str]:
    from dsl41.main_config import encoded_parts

    return encoded_parts(estate)


def configuration_of(opened: OpenedPlan, base: RuntimeProfile) -> Configuration:
    """The candidate's configuration, retaining its captured host profile (DL-437, DL-478)."""
    profile = profile_for(opened.estate.settings, base)
    launch = opened.estate.metadata["launch"]
    profile = with_host_settings(profile, launch["as_machine"], launch["machine_policy"])
    return Configuration(
        catalog=opened.estate.catalog,
        profile=profile,
        non_idempotent=dict(opened.marked.reasons),
        source_capture=_source_capture(opened.estate),
    )


def same_configuration(existing: Configuration, candidate: Configuration) -> bool:
    """Equal definitions, order, policy, marks and captured inputs (DL-468, DL-478)."""
    return (
        existing.catalog_hash == candidate.catalog_hash
        and list(existing.catalog.jobs) == list(candidate.catalog.jobs)
        and existing.profile == candidate.profile
        and existing.non_idempotent == candidate.non_idempotent
        and existing.source_capture == candidate.source_capture
    )


def store_config(store: Store, configuration: Configuration, rev: int) -> None:
    """Append the next rev, or retain equal definitions, order and inputs (DL-438, DL-468).

    Compiler version metadata is provenance, not a definition. Every refusal precedes a
    write, so retrying a mismatched revision cannot grow the configuration history.
    """
    current = store.current_config_rev()
    if current == rev - 1:
        store.put_config(configuration.to_bytes(), configuration.digest())
        return
    if current == rev and rev > 0:
        existing = Configuration.from_bytes(store.get_config(rev), where=f"configuration rev {rev}")
        if same_configuration(existing, configuration):
            return
    raise EngineError(
        f"the store's configuration rev {current} does not match the update's configuration rev {rev}"
    )


def reopen_plan(run_root: Path, token: str, *, configuration: Configuration) -> OpenedPlan:
    """The plan of an update a restart found stopping (`open_plan`), refused as a restart is."""
    try:
        opened = open_plan(run_root, token)
        catalog = configuration.catalog
        recorded_settings = opened.plan["settings"]
        if not isinstance(recorded_settings, dict):
            raise PlanUnreadable("plan settings must be an object")
        defaults = policy_values({})
        omitted = defaults.keys() - recorded_settings.keys() - {"kill_signals"}
        old_settings = settings_of(configuration.profile, catalog)
        new_settings = opened.estate.settings
        # Missing definition settings use earlier-behaviour defaults; kills are host policy
        # (DL-468, DL-462).
        compiled = diff(
            catalog,
            opened.estate.catalog,
            {key: value for key, value in old_settings.items() if key not in omitted},
            {key: value for key, value in new_settings.items() if key not in omitted},
        )
        recorded = plan_of(opened.plan, catalog)
        if (
            any(
                side[key] != defaults[key]
                for side in (old_settings, new_settings)
                for key in omitted
            )
            or opened.plan["live_digest"] != configuration.catalog_hash
            or compiled.added != recorded.added
            or compiled.removed != recorded.removed
            or compiled.unchanged != recorded.unchanged
            or [(c.name, c.old_hash, c.new_hash) for c in compiled.changed]
            != [(c.name, c.old_hash, c.new_hash) for c in recorded.changed]
        ):
            raise EngineError(
                f"update {token} recovery refused: compiled job diff differs from the recorded plan."
                " Finish the update using the binary that began it, then upgrade. (DL-468)"
            )
        return opened
    except (PlanUnreadable, KeyError, TypeError) as exc:
        if isinstance(exc.__cause__, RetiredPlan):
            raise EngineError(
                f"the update {token} was stopping and its plan cannot be read: {exc};"
                " this build cannot finish the open update."
                " Finish the update with the build that began it."
            ) from exc
        raise EngineError(
            f"the update {token} was stopping and its plan cannot be read: {exc}; restore"
            f" {run_root / PLANS / token} if missing or damaged, or use a compatible binary"
        ) from exc


def _reused(request_id: str) -> UpdateAnswer:
    return UpdateAnswer(
        code="request_id_reused",
        error=(
            f"request_id {request_id!r} was admitted for a different command: reuse an id only"
            " for an exact retry"
        ),
    )


def _settle(future: asyncio.Future[dict[str, Any]] | None, body: dict[str, Any]) -> None:
    assert future is not None  # an admission's answer carries its `applied` future
    future.set_result(body)


def now_of(clock_now: datetime, frontier: datetime | None) -> datetime:
    """The instant an update's step is stamped: the clock, never before the admission
    frontier, so time stays monotone."""
    return clock_now if frontier is None else max(clock_now, frontier)


__all__ = [
    "APPLICATION_KEYS",
    "PLANS",
    "EngineUpdate",
    "UpdateAnswer",
    "UpdateRequest",
    "admitted_of",
    "application_body",
    "OpenedPlan",
    "PlanUnreadable",
    "open_plan",
    "reopen_plan",
    "store_config",
    "now_of",
    "profile_for",
    "settings_of",
]
