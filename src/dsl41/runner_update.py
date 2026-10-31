"""The engine's `update` (spec-reload "`dsl41 update`", DL-427): the `update` chart's tier bound to
the engine's store, its attempt tier, its oracle and its scheduler.

An update request is one step of the engine's single writer (`Engine.request_update`). Its
admission reads the plan `reread` stored under `plans/<token>/`, loads the candidate from those
bytes with the environment and the site layers `invocation.json` names, and re-checks the runtime
preconditions against the engine's state now (`reload_update.admission_check`). A refusal is the
reply and writes nothing. An admission appends `config intent` (the token, the stop set with the
attempt each stop interrupts, the dispatch filter, and the request's id and fingerprint), then
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
(startup step 4, spec-crash): the attempts it stops are settled first, then it applies, before
any other attempt is settled or anything is dispatched.

An exact retry of an admitted request, under its request id with the same token, consent and
precondition, is answered from its `config intent` record and, once applied, from its
`config applied` record. A refusal is not recorded, so a retry of one is admitted afresh.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from dsl41.canon import digest
from dsl41.engine_charts import UPDATE_STOP, AttemptOutcome
from dsl41.idempotence import Exceptions, IdempotenceError, load_exceptions
from dsl41.ir import CatalogIR
from dsl41.reload_plan import (
    INVOCATION,
    MARKED,
    Estate,
    Live,
    RereadRefused,
    estate_settings,
    load_estate,
    read_plan,
)
from dsl41.reload_diff import catalog_hash_v2
from dsl41.reload_update import (
    STALE_PLAN,
    UPDATE_IN_PROGRESS,
    Admitted,
    Application,
    Refused,
    UpdateTier,
    admission_check,
    apply_application,
    live_of,
    plan_application,
)
from dsl41.runner_attempts import UNSETTLED, FollowUp
from dsl41.runner_clock import EngineError
from dsl41.runner_codes import Code
from dsl41.runner_config import Configuration, RuntimeProfile, tz_aliases_of
from dsl41.runner_journal import CONFIG_MARKER, config_marker
from dsl41.runner_ledger import STATE_MACHINE_VERSION
from dsl41.runner_store import Record, Store

if TYPE_CHECKING:  # annotation only: the engine imports this module
    from dsl41.runner import Engine
    from dsl41.runner_journal import Journal

#: The plans directory of a run root (`cli_reload.PLANS`).
PLANS: Final = "plans"

#: The `config applied` fields an update adds to the segment's: what the application did.
APPLICATION_KEYS: Final = ("token", "added", "changed", "removed", "restored", "stopped")


@dataclass(frozen=True)
class UpdateRequest:
    """One `update` request as the control socket admits it: the plan's token, the consent to
    stop running jobs, the configuration rev the caller read (`expect`), the store it read it
    from, its request id and the actor it claims."""

    token: str
    stop_running: bool
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
                "token": self.token,
                "stop_running": self.stop_running,
                "expect": self.expect,
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


def settings_of(profile: RuntimeProfile, catalog: CatalogIR) -> dict[str, object]:
    """The estate settings an engine runs `catalog` under with `profile`, spelled as `reread`
    spells a candidate's (`reload_plan.estate_settings`), so an unchanged estate diffs equal."""
    return estate_settings(profile.default_tz, tz_aliases_of(profile), profile.semantics, catalog)


def profile_for(settings: Mapping[str, Any], base: RuntimeProfile) -> RuntimeProfile:
    """`base` running the estate settings `settings`: the base zone, the timezone map and the
    semantic switches follow the candidate; the machine identity and the FW window stay."""
    aliases = settings.get("timezone_aliases")
    return base.model_copy(
        update={
            "default_tz": settings.get("timezone") or "UTC",
            "tz_aliases": dict(aliases or {}),
            "semantics": dict(settings.get("semantics") or {}),
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
    #: the site layers' original paths, as `invocation.json` records them (the estate holds
    #: the plan directory's copies it was loaded from)
    site: tuple[str, ...] = ()

    def fingerprint(self) -> dict[str, str]:
        """The applied estate's input files as `run` fingerprints its own (path -> sha256, the
        `spec_drift` baseline, DL-65): each part at its place in the estate directory `reread`
        read (`plan.json`'s `estate_dir`), each site layer at the original path `reread` was
        given, never the plan directory's copy, with the plan's own bytes."""
        directory = str(self.plan["estate_dir"])
        site = {Path(path).name: path for path in self.site}
        prints: dict[str, str] = {}
        for name, data in self.estate.parts:
            if name == INVOCATION or (name == MARKED and not data):
                continue  # not a file `run` reads: the invocation, an absent marked set
            path = site[name[len("site/") :]] if name.startswith("site/") else f"{directory}/{name}"
            prints[path] = hashlib.sha256(data).hexdigest()
        return prints


def open_plan(run_root: Path, token: str) -> OpenedPlan:
    """The plan `reread` stored under `RUN_ROOT/plans/TOKEN`, its candidate loaded as `reread`
    loaded it: the manifest and its files from the plan directory, the site layers from their
    copies under `site/`, the environment `invocation.json` names (`read_plan` checked every
    part against `plan.json`). Raises PlanUnreadable naming what is wrong."""
    from dsl41.canon import decode

    plans = run_root / PLANS
    plan = read_plan(plans, token)
    if plan is None:
        raise PlanUnreadable(f"no complete plan {token} under {plans}: run reread first")
    plan_dir = plans / token
    try:
        invocation: Any = decode((plan_dir / INVOCATION).read_bytes())
        layers = [plan_dir / "site" / Path(str(path)).name for path in invocation["properties"]]
        estate = load_estate(plan_dir, layers, invocation["environment"])
        marked = load_exceptions(plan_dir)
    except (RereadRefused, IdempotenceError, OSError, KeyError, TypeError) as exc:
        raise PlanUnreadable(f"plan {token} does not load: {exc}") from exc
    return OpenedPlan(plan, estate, marked, tuple(str(path) for path in invocation["properties"]))


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
        #: the jobs the dispatch filter held while the update stopped, released after it
        self._held: frozenset[str] = frozenset()
        #: set by an application inside a step, which the step finishes (`finish`)
        self.applied = False
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
        return live_of(
            engine.oracle, settings_of(profile, engine.oracle.catalog), engine.config_rev
        )

    # ---- the request (one single-writer step)

    def admit(self, request: UpdateRequest) -> UpdateAnswer:
        """Admit or refuse one update request (module docstring). The answer's `applied` future
        resolves once the update applied, at once when nothing was to stop."""
        engine = self.engine
        if engine.run_root is None or engine.journal is None:
            return UpdateAnswer(code="invalid_argument", error="this engine has no run root")
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
            # a plan an older `reread` wrote names no estate directory, which the engine's
            # account of the files on disk needs once it applies (`spec_drift`)
            return UpdateAnswer(
                code="stale_plan",
                error=f"plan {request.token} predates this engine: run reread again",
            )
        if request.expect != engine.config_rev or opened.plan.get("live_digest") != (
            catalog_hash_v2(engine.oracle.catalog)
        ):
            # the precondition names the rev the caller read; the plan names the rev and the
            # catalog reread saw, checked before anything is written, so the application's own
            # check of them never meets a mismatch after the intent
            return self._decide(Refused(request.token, STALE_PLAN))
        decision = admission_check(
            opened.plan,
            live=self.live(),
            candidate=opened.estate.catalog,
            stop_running=request.stop_running,
            open_update=None,
        )
        if isinstance(decision, Refused):
            return self._decide(decision)
        self._begin(request, opened, decision)
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

    def _begin(self, request: UpdateRequest, opened: OpenedPlan, admitted: Admitted) -> None:
        engine = self.engine
        self._request, self._opened = request, opened
        self._kept, self._cancelled, self._cleaning, self._others = {}, [], {}, {}
        self._moved = frozenset(engine.marked ^ set(opened.marked.names))
        self._held = admitted.dispatch_filter
        self._admitted = admitted
        self._attempts = {job: engine.attempt_of_run(job, run) for job, run in admitted.stop_runs}
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

    # ---- restart step 4

    def resume(self, intent: Mapping[str, Any]) -> None:
        """Restore the update a restart found in STOPPING from its `config intent` record
        (startup step 4): its dispatch filter holds from here, and it waits for each attempt of
        its stop set the open table holds, file watches excepted (step 5 takes a watch up).
        Step 5 settles those attempts like any open one; their ends, and a first look at a stop
        set already down, are steps of the single writer, so the update applies in the loop.
        Nothing is appended or applied here."""
        engine = self.engine
        assert engine.journal is not None and engine.run_root is not None
        admitted = admitted_of(intent)
        opened = reopen_plan(engine.run_root, admitted.token)
        self._request = UpdateRequest(
            admitted.token,
            bool(intent.get("stop_running")),
            int(intent.get("live_rev", engine.config_rev)),
            engine.baseline_id,
            str(intent.get("request_id", "")),
        )
        self._opened = opened
        self._kept, self._cancelled, self._cleaning, self._others = {}, [], {}, {}
        self._moved = frozenset(engine.marked ^ set(opened.marked.names))
        self._held = admitted.dispatch_filter
        self._admitted = admitted
        self._attempts = {
            str(entry["job"]): entry.get("attempt") for entry in intent.get("stop") or ()
        }
        table = engine.journal.open.attempts
        catalog = engine.oracle.catalog.jobs
        still = {
            job: run_id
            for job, run_id in self._attempts.items()
            if run_id in table and catalog[job].job_type != "FW"  # step 5 takes a watch up
        }
        # an attempt whose outcome is in while its cleanup was cut short is down once step 5
        # has finished that cleanup (`runner_startup._finish_ladder`)
        self._cleaning = {run_id: job for job, run_id in still.items() if table[run_id].closing}
        self.tier = UpdateTier(self, self._prepare)
        # the request's own answer: a retry under its id while it still stops waits on it
        self._answer = UpdateAnswer(admitted)
        self._answer.applied = asyncio.get_running_loop().create_future()
        self._post(lambda tier: tier.resume(admitted, still.keys(), settle=False))
        self.engine.post_update_exit(None)  # the loop's first look: a stop set already down

    # ---- an attempt of the stop set ended

    def keep(self, run_id: str, job: str, outcome: AttemptOutcome) -> None:
        """Keep an ended attempt's outcome for the application (`keeps`). A stop-set attempt
        is down once its stop is finished (`Engine.stop_done`: the ladder done, nothing of it
        left), which may come after its record (`cleaned`); any other is only kept."""
        self._kept[run_id] = outcome
        self.engine.attempt_facts(run_id).settled.set()  # its record is in
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
        if not self.direct:
            self.engine.post_update_exit(job)
        elif job is None:
            self._post(lambda tier: tier.settle())
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
            "stop_running": request.stop_running,
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
        live = engine._live.get(job)
        if not known and live is not None and live.attempt == run_id:
            del engine._live[job]
            live.task.cancel()
            engine._reaping.append(live.task)
            self._cancelled.append((job, live.run_number, run_id))
        assert self.tier is not None
        self.tier.exit_recorded(job)

    def _prepare(self, admitted: Admitted) -> Application:
        """The application, computed from the oracle as the stops left it, under the settings
        the engine itself read from the plan's bytes."""
        opened = self._opened
        assert opened is not None
        plan = {**opened.plan, "settings": dict(opened.estate.settings)}
        return plan_application(
            self.engine.oracle,
            opened.estate.catalog,
            plan,
            opened.marked.names,
            dict(admitted.stop_runs),
        )

    def apply(self, application: Application) -> None:
        """Swap the catalog, the settings, the plans, the marked set and the profile."""
        engine = self.engine
        assert self._opened is not None
        apply_application(engine.oracle, application, revision=engine.config_rev)
        at = engine.now_admitted()
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

    def journal_applied(self, rev: int, catalog_hash: str) -> None:
        """The new configuration row and its `config applied` record."""
        engine = self.engine
        tier = self.tier
        assert tier is not None and tier.application is not None and self._opened is not None
        application = tier.application
        self.applied = True
        journal = engine.journal
        assert journal is not None  # `admit` and `resume` refuse an engine with none
        # `catalog_hash` is `catalog_hash_v2` of this catalog (`plan_application`), the hash the
        # configuration row derives
        configuration = Configuration(
            catalog=application.catalog,
            profile=engine.profile or RuntimeProfile(),
            non_idempotent=dict(self._opened.marked.reasons),
        )
        store_config(journal.store, configuration, rev)
        stopped = self._choose_follow_ups()
        at = engine.now_admitted()
        body = {
            "event": "applied",
            "rev": rev,
            "at": at.isoformat(),
            "catalog_hash": configuration.catalog_hash,
            "runtime_hash": configuration.runtime_hash,
            "state_machine_version": STATE_MACHINE_VERSION,
            "baseline_id": journal.baseline_id,
            "clock_domain": journal.clock_domain,
            **application_body(application),
            "stopped": stopped,
        }
        journal.append(Record(at, "config", None, None, body))
        # the trace says where the rev moved, as a replay of the record does
        engine.oracle.mark(at, CONFIG_MARKER, config_marker(rev))
        self._body = body

    def _choose_follow_ups(self) -> list[list[str]]:
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
        stopped: list[list[str]] = []
        for job in self._admitted.stop_set:
            attempt = self._attempts.get(job)
            if attempt is not None and attempt in self._kept:
                plan = self._chosen[attempt] = engine.plan_for(attempt, self._kept[attempt])
                stopped.append([job, plan.kind])
            elif job in cancelled:
                again = self._takes_up[job] = engine.takes_up(job, cancelled[job])
                stopped.append([job, "rerun" if again else "none"])
            else:
                stopped.append([job, "none"])
        for attempt in self._others:
            self._chosen[attempt] = engine.plan_for(attempt, self._kept[attempt])
        return stopped

    # ---- after the application

    def finish(self) -> None:
        """What follows an application, once the time advance is fed or queued: the outcomes
        kept back are committed with their plans under the applied catalog, the cancelled tasks
        are taken up or closed, the held starts are released, and the request is answered."""
        engine = self.engine
        self.applied = False
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


def store_config(store: Store, configuration: Configuration, rev: int) -> None:
    """Put `configuration` as the store's configuration row `rev`. A row with its digest already
    at `rev` is the one an engine wrote before it died short of `config applied`, and is kept;
    any other row at `rev` refuses, naming both revs."""
    found = store.find_config(configuration.digest())
    stored = (
        found
        if found == rev
        else store.put_config(configuration.to_bytes(), configuration.digest())
    )
    if stored != rev:
        raise EngineError(
            f"the store took configuration rev {stored}, the update applies rev {rev}"
        )


def reopen_plan(run_root: Path, token: str) -> OpenedPlan:
    """The plan of an update a restart found stopping (`open_plan`), refused as a restart is."""
    try:
        return open_plan(run_root, token)
    except PlanUnreadable as exc:
        raise EngineError(
            f"the update {token} was stopping and its plan cannot be read: {exc}; restore"
            f" {run_root / PLANS / token} to finish it"
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
