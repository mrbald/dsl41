"""The deterministic model harness (docs/concurrency-model.md ss9, stage H).

Stage H of the phase-12 programme, and deliberately built BEFORE the code
it validates (concurrency-model ss10): the checkers and the fault driver
have to exist first, or S1b..S6 acquire a proving ground afterwards --
which is how a design gets tests that agree with it instead of tests that
could have caught it being wrong.

**Where it observes.** `adapter.run()` IS the effect application: the
moment a job's work actually begins. Counting entries there counts work
that STARTED, not dispatch intent, which is the difference between the
property and a restatement of the code. The seam survives the programme --
in the multihost model the relay's SPAWN is the same point -- which is why
`Spawn` carries an `executor_id` that is the constant "local" until S5
gives it meaning, rather than acquiring the field later.

**What it checks.** CM-14 (no `(job, run_number)` runs twice) and CM-09's
physical half (two runs of one job never overlap in time), across EVERY
engine incarnation in an interleaving. The log outlives a crash on
purpose: a resume-driven double spawn is invisible to any per-engine
assertion, because each engine is individually correct.

**What it does not check yet.** `state_rev`, admission, election and host
eviction have no code (S1c/S2/S6/S8). The checkers are written against the
frozen model rather than against today's engine, so those arrive as new
rows here, not as a rewrite.

Not a test file: no test_ prefix. `tests/test_model_harness.py` drives it,
and pins that the checkers can actually fail.
"""

from __future__ import annotations

import asyncio
import json
import random

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from dsl41.ir import CatalogIR, lower_source
from dsl41.oracle_state import Event
from dsl41.runner import Engine
from dsl41.runner_scheduler import Scheduler
from dsl41.runner_startup import resume_run, start_run
from dsl41.runner_adapters import AdapterContext, FakeAdapter
from dsl41.runner_admission import Envelope
from dsl41.runner_clock import VirtualClock
from dsl41.runner_hosts import HostCommand
from dsl41.oracle_state import RuntimeState
from dsl41.period import active_wal

if TYPE_CHECKING:
    from dsl41.ir import JobIR
    from dsl41.runner_adapters import AdapterResult, JobAdapter

LOCAL = "local"  # the only executor_id until S5 introduces relays


#: How an adapter invocation relates to a process. Only "exec" starts work,
#: and only "exec" is counted by CM-14/CM-09 -- an adapter call is NOT an
#: effect application, and conflating them makes the checkers report the
#: two legitimate resume paths as double runs:
#:
#:   "watch"  an FW re-dispatch after a crash. runner._reconcile calls it an
#:            "idempotent read": re-arming a file watch executes nothing.
#:   "attach" a detached-resume REATTACH. The wrapper never stopped, so the
#:            adapter awaits its exit push and spawns nothing (runner ss3).
#:
#: The frozen model draws the same line: effects bind to `run_id` -- the
#: per-spawn uuid4 -- not to `run_number`, precisely so a second invocation
#: against one live run is not a second run (concurrency-model ss2, ss5).
EXEC, WATCH, ATTACH = "exec", "watch", "attach"


@dataclass
class Spawn:
    """One observed adapter invocation, classified by `mode`.

    `ended_at is None` means the work was still running when the log was
    read -- including a run whose engine died under it, which is exactly
    the state a resume must not duplicate.
    """

    seq: int
    executor_id: str
    job: str
    run_number: int
    incarnation: int
    started_at: datetime
    mode: str = EXEC
    ended_at: datetime | None = None
    outcome: str | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.job, self.run_number)


@dataclass
class SpawnLog:
    """Every effect application in one interleaving, across engine
    incarnations. Held by the test, not by an engine, so a crash cannot
    take the evidence with it."""

    spawns: list[Spawn] = field(default_factory=list)
    incarnation: int = 0

    def begin(
        self,
        *,
        job: str,
        run_number: int,
        at: datetime,
        mode: str = EXEC,
        executor_id: str = LOCAL,
    ) -> Spawn:
        spawn = Spawn(
            seq=len(self.spawns),
            executor_id=executor_id,
            job=job,
            run_number=run_number,
            incarnation=self.incarnation,
            started_at=at,
            mode=mode,
        )
        self.spawns.append(spawn)
        return spawn

    def execs(self) -> list[Spawn]:
        """The invocations that actually started work -- what the model's
        properties are about."""
        return [s for s in self.spawns if s.mode == EXEC]

    def by_job(self) -> dict[str, list[Spawn]]:
        out: dict[str, list[Spawn]] = defaultdict(list)
        for spawn in self.execs():
            out[spawn.job].append(spawn)
        return dict(out)


class RecordingAdapter:
    """JobAdapter decorator: records the interval of every real run.

    A cancellation is an ending -- the engine cancelling a stale task is
    the process being killed -- so it is recorded before CancelledError
    propagates. Anything else the inner adapter raises is recorded and
    re-raised: a swallowed adapter bug would read as a clean interval.
    """

    def __init__(self, inner: JobAdapter, log: SpawnLog, *, executor_id: str = LOCAL) -> None:
        self.inner = inner
        self.log = log
        self.executor_id = executor_id
        #: when set, the NEXT exec leaves nothing behind -- no directory, no
        #: log entry -- and parks. From disk and from this log that is
        #: indistinguishable from an engine that died between recording the
        #: intent to spawn and acting on it, which is the only state in which
        #: ss7's barrier re-drives rather than fails (DL-102).
        self.park_next = False

    def _mode(self, job_ir: JobIR, run_number: int) -> str:
        if job_ir.job_type == "FW":
            return WATCH
        reattach = getattr(self.inner, "reattach", None)
        if reattach is not None and (job_ir.name, run_number) in reattach:
            return ATTACH
        return EXEC

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> AdapterResult:
        mode = self._mode(job_ir, run_number)
        if mode == EXEC and self.park_next:
            self.park_next = False
            await ctx.clock.sleep_until(datetime.max)  # the crash arrives first
        if mode == EXEC and ctx.run_root is not None:
            # The trace, created the way every real adapter creates it:
            # `mkdir(parents=True)` with NO exist_ok, before anything runs
            # (runner_adapters). Two rules rest on this and neither is
            # visible from the engine, so a model without it is a model of a
            # different system. DL-96 deviated from ss5's "bind run_id before
            # the attempt" precisely BECAUSE this mkdir makes a second spawn
            # of one run fail loudly rather than double; and DL-102's
            # re-drive is sound only because anything that ran left this
            # behind, so "no trace anywhere" really does mean "nothing ran".
            # Without it here, the seeded sweep double-ran on its first
            # afternoon (DL-108) -- the model, not the engine, was wrong.
            (ctx.run_root / "runs" / f"{job_ir.name}.{run_number}").mkdir(parents=True)
        spawn = self.log.begin(
            job=job_ir.name,
            run_number=run_number,
            at=ctx.clock.now(),
            mode=mode,
            executor_id=self.executor_id,
        )
        try:
            result = await self.inner.run(job_ir, run_number, ctx)
        except asyncio.CancelledError:
            spawn.ended_at, spawn.outcome = ctx.clock.now(), "cancelled"
            raise
        except BaseException as exc:  # noqa: BLE001 -- recorded, then re-raised
            spawn.ended_at, spawn.outcome = ctx.clock.now(), f"raised {type(exc).__name__}"
            raise
        spawn.ended_at, spawn.outcome = ctx.clock.now(), repr(result)
        return result


# ------------------------------------------------------------------- checkers
#
# Each returns human-readable violations; empty means the property held.
# They are pure functions of the log so a test can build one by hand and
# prove the checker has teeth (test_model_harness.py does exactly that).


def cm14_double_spawns(log: SpawnLog) -> list[str]:
    """CM-14: no `(job, run_number)` ever executes twice.

    The safety property of the whole model (concurrency-model ss0),
    counted rather than argued."""
    seen: dict[tuple[str, int], Spawn] = {}
    violations: list[str] = []
    for spawn in log.execs():
        first = seen.get(spawn.key)
        if first is None:
            seen[spawn.key] = spawn
            continue
        violations.append(
            f"{spawn.job} run {spawn.run_number} ran twice:"
            f" first at {first.started_at.isoformat()} on {first.executor_id}"
            f" (engine incarnation {first.incarnation}),"
            f" again at {spawn.started_at.isoformat()} on {spawn.executor_id}"
            f" (engine incarnation {spawn.incarnation})"
        )
    return violations


def cm09_overlapping_runs(log: SpawnLog) -> list[str]:
    """CM-09's physical half: two runs of one job never overlap.

    Distinct from CM-14 -- an engine that leaked a stale adapter task and
    then started run N+1 satisfies CM-14 and still has two processes for
    one job. A spawn that never ended overlaps everything after it, which
    is the point: an abandoned run is not a finished one."""
    violations: list[str] = []
    for job, spawns in sorted(log.by_job().items()):
        ordered = sorted(spawns, key=lambda s: (s.started_at, s.seq))
        for prev, nxt in zip(ordered, ordered[1:], strict=False):
            if prev.ended_at is None:
                violations.append(
                    f"{job} run {nxt.run_number} started at"
                    f" {nxt.started_at.isoformat()} while run {prev.run_number}"
                    f" was still running (never ended)"
                )
            elif prev.ended_at > nxt.started_at:
                violations.append(
                    f"{job} run {prev.run_number} ran until"
                    f" {prev.ended_at.isoformat()}, overlapping run"
                    f" {nxt.run_number} which started at {nxt.started_at.isoformat()}"
                )
    return violations


CHECKERS = {"CM-14": cm14_double_spawns, "CM-09": cm09_overlapping_runs}


def check(log: SpawnLog, *, only: str | None = None) -> None:
    """Raise with every violation at once. A harness that reports one
    failure per run makes a multi-fault interleaving take as many runs as
    it has faults."""
    findings: list[str] = []
    for code, checker in CHECKERS.items():
        if only is not None and code != only:
            continue
        findings += [f"{code}: {line}" for line in checker(log)]
    if findings:
        raise AssertionError(
            f"{len(findings)} concurrency-model violation(s) over"
            f" {len(log.spawns)} spawn(s):\n  " + "\n  ".join(findings)
        )


# --------------------------------------------------------------------- driver


class ModelRun:
    """One interleaving over one run root, spanning engine incarnations.

    Faults available today are the ones today's code can actually suffer:
    engine death mid-run, resume, duplicated and dropped completions.

    S7a (DL-108) added the rest of what one host can suffer, and a seed to
    choose between them: leader failover at an arbitrary point, a spawn
    decided and never acted on, duplicated and stale completions,
    quarantine, and a drain under in-flight work. `FaultSchedule` is the
    driver; the sweep in test_model_harness.py is CM-14 over every
    interleaving a seed produces rather than over six an author thought of.

    Partition BETWEEN leaders and reroute to a second host remain absent,
    and this time the reason is stable rather than a promise: there is no
    second host to reroute to and no relay to partition from (DL-97, DL-103).
    They stay absent rather than stubbed, because a stub that always passes
    is the failure mode this harness exists to prevent. What this models is
    therefore ONE run root across SEQUENTIAL incarnations -- which is the
    whole of what today's code can be held to, and not the whole of ss9.
    """

    def __init__(
        self,
        estate: str | CatalogIR,
        run_root: Path,
        *,
        script: dict[tuple[str, int], tuple[float, int]] | None = None,
        default: tuple[float, int] | None = None,
        start: datetime | None = None,
        scheduler: Callable[[datetime], Scheduler] | None = None,
    ) -> None:
        # an estate, however the caller got one: JIL for the small fixtures
        # here, a lowered catalog for nightbank, which needs placeholder
        # substitution across five files before it is one (S7b)
        self.catalog: CatalogIR = estate if isinstance(estate, CatalogIR) else lower_source(estate)
        #: built fresh per incarnation, because resume RE-ANCHORS it at the
        #: last journal instant and dedups against the ticks the log holds --
        #: a scheduler carried across a crash would be one that never
        #: noticed the crash (ss7, DL-45)
        self.new_scheduler = scheduler
        self.run_root = run_root
        self.script = dict(script or {})
        #: what an UNSCRIPTED job does. None parks it forever, which is what
        #: the small fixtures want (a job that never ends is a run a resume
        #: must not duplicate); an estate driven end to end wants a duration
        #: and an exit code, or its cascades never advance (S7b).
        self.default = default
        self.log = SpawnLog()
        self.clock = VirtualClock(start=start) if start else VirtualClock()
        self.engine: Engine | None = None
        #: faults that persist until `settle` puts them back
        self.quarantined = False
        self.drained = False

    def _adapters(self) -> dict[str, JobAdapter]:
        inner = FakeAdapter(self.script, default=self.default)
        self.adapter = RecordingAdapter(inner, self.log)
        return {"CMD": self.adapter, "FW": self.adapter}

    def start(self) -> Engine:
        self.engine = start_run(
            self.catalog,
            self.run_root,
            clock=self.clock,
            adapters=self._adapters(),
            scheduler=self._scheduler(),
        )
        return self.engine

    def _scheduler(self) -> Scheduler | None:
        return None if self.new_scheduler is None else self.new_scheduler(self.clock.now())

    @property
    def live(self) -> Engine:
        assert self.engine is not None, "start() or resume() first"
        return self.engine

    async def run_to(self, horizon: datetime) -> list[Event]:
        return await self.live.run_until_quiescent(horizon)

    def inject(self, ev: Event, *, source: str | None = "control") -> None:
        self.live.inject(ev, source=source)

    def deliver(self, ev: Event, *, source: str = "adapter") -> None:
        """Deliver an event as a COMPLETION -- the path an effect result
        takes, and the only one the ss4 stale gate sees. `inject` cannot
        model this: an external event is never gated (it carries
        CHANGE_STATUS parity), so a duplicated or superseded result has to
        arrive the way a real adapter would deliver it. The SOURCE is what
        makes it a completion (runner_admission.COMPLETION_SOURCES): the
        log carries provenance and nothing else, so replay has to reach the
        gate's verdict from the same field."""
        self.live._enqueue(ev, source=source)

    async def crash(self) -> None:
        """Model engine death: in-flight adapter tasks die without
        reporting, and the journal stops where it stopped. No orderly
        shutdown, because an orderly shutdown is the case that cannot
        produce the bug."""
        engine = self.live
        await engine.shutdown()  # cancels tasks; records no outcomes
        if engine.journal is not None:
            engine.journal.close()
        self.engine = None
        self.log.incarnation += 1

    # ------------------------------------------------- the faults (S7a)
    #
    # Each returns True if it actually happened. A fault whose precondition
    # is absent is a no-op and says so, because a schedule that counted it
    # would let the sweep report coverage it does not have.

    async def fault_failover(self, *, lose_outcome: bool = False) -> bool:
        """Engine death at this point, then ss7's takeover barrier.

        `lose_outcome` additionally cuts the last effect_result from the log
        before the resume, which models the window S5c named: the previous
        leader acted, or was about to, and died before recording what came
        of it. That is the case the barrier has to tell apart from a start
        that simply never happened."""
        if self.engine is None:
            return False
        await self.crash()
        if lose_outcome and not self._cut_last_effect_result():
            pass  # nothing to lose here; the failover still happened
        await self.resume(settle_seconds=0.0, grace_seconds=0.0)
        return True

    async def fault_failover_losing_the_outcome(self) -> bool:
        return await self.fault_failover(lose_outcome=True)

    async def fault_lost_dispatch(self) -> bool:
        """The engine decided a spawn and died before doing it.

        The narrow window ss4 step 7 opens: the effect is in the log, the
        adapter never acted, and nothing is on disk. It is the ONLY state in
        which the barrier re-drives rather than fails (DL-102), so a sweep
        without it would exercise every branch of that rule but the new one.

        Modelled by parking the next exec before it leaves any trace and
        then cutting the outcome record -- from the log and from disk,
        indistinguishable from dying a moment earlier."""
        if self.engine is None:
            return False
        self.adapter.park_next = True
        await self.run_to(self.clock.now())
        await self.crash()
        cut = self._cut_last_effect_result()
        await self.resume(settle_seconds=0.0, grace_seconds=0.0)
        return cut

    async def fault_duplicate_completion(self) -> bool:
        """The same adapter report delivered twice. At-most-once APPLICATION
        is the model's promise; at-least-once delivery is what a real
        transport gives, so a duplicate must change nothing."""
        target = self._a_live_run()
        if target is None:
            return False
        job, run_number = target
        self.deliver(
            Event(
                at=self.clock.now(),
                kind="STATUS",
                payload={"job": job, "run_number": run_number, "status": "SUCCESS"},
            )
        )
        return True

    async def fault_stale_completion(self) -> bool:
        """A report about a run the estate has moved past -- the ss4 stale
        gate's whole subject. It must be dropped AND recorded, never applied
        and never silently absent."""
        target = self._a_live_run()
        if target is None:
            return False
        job, run_number = target
        self.deliver(
            Event(
                at=self.clock.now(),
                kind="STATUS",
                payload={"job": job, "run_number": run_number - 1, "status": "FAILURE"},
            )
        )
        return True

    async def fault_quarantine(self) -> bool:
        """The supervisor stops answering (S5d). New work is HELD rather
        than failed; running work continues. The reinstate rides on the next
        confirmed contact, which `settle` performs at the end of the run."""
        if self.engine is None or self.quarantined:
            return False
        self.live.note_executor_unreachable()
        self.quarantined = True
        return True

    async def fault_drain(self) -> bool:
        """The operator parks the host under in-flight work (S5a, CM-13).
        Unlike quarantine this is an admitted operator command with a
        precondition, so it goes through the door an operator uses."""
        if self.engine is None or self.drained:
            return False
        engine = self.live
        key = RuntimeState.host_key(LOCAL)
        engine.submit_host(
            HostCommand(verb="drain", host_id=LOCAL),
            Envelope(
                request_id=f"drain-{len(self.log.spawns)}-{engine.frontiers.committed_index}",
                expect={key: engine.oracle.store.revision(key)},
                epoch=engine.epoch,
            ),
        )
        self.drained = True
        return True

    def _a_live_run(self) -> tuple[str, int] | None:
        if self.engine is None:
            return None
        for job, rt in sorted(self.live.oracle.store.job.items()):
            if rt.status in ("STARTING", "RUNNING") and rt.run_number > 0:
                return job, rt.run_number
        return None

    def _cut_last_effect_result(self) -> bool:
        """Drop the log's last `effect_result`. Written between crash and
        resume, which is the only moment it is safe to edit a run root's
        log -- no engine holds it, exactly as no engine held it in the crash
        this models."""
        path = active_wal(self.run_root)
        records = [json.loads(line) for line in path.read_text().splitlines() if line]
        for index in range(len(records) - 1, -1, -1):
            if records[index].get("rec") == "effect_result":
                del records[index]
                path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
                return True
        return False

    async def settle(self, horizon: datetime) -> None:
        """Put back everything the faults took away, then run to quiescence.

        Safety is checked over the WHOLE interleaving, but a run left
        quarantined or drained ends with work held for a reason that has
        nothing to do with the property under test -- and a sweep whose runs
        all end held would pass without ever having dispatched anything."""
        if self.engine is None:
            await self.resume(settle_seconds=0.0, grace_seconds=0.0)
        if self.quarantined:
            self.live.note_executor_contact()
            self.quarantined = False
        if self.drained:
            engine = self.live
            key = RuntimeState.host_key(LOCAL)
            engine.submit_host(
                HostCommand(verb="activate", host_id=LOCAL),
                Envelope(
                    request_id=f"activate-{engine.frontiers.committed_index}",
                    expect={key: engine.oracle.store.revision(key)},
                    epoch=engine.epoch,
                ),
            )
            self.drained = False
        await self.run_to(horizon)

    async def resume(self, **kwargs: object) -> Engine:
        assert self.engine is None, "crash() before resume(): two live engines is a different bug"
        self.engine = await resume_run(
            self.catalog,
            self.run_root,
            clock=self.clock,
            adapters=self._adapters(),
            scheduler=self._scheduler(),
            **kwargs,  # type: ignore[arg-type]
        )
        return self.engine

    async def close(self) -> None:
        if self.engine is not None:
            await self.engine.shutdown()
            if self.engine.journal is not None:
                self.engine.journal.close()
            self.engine = None

    def check(self, *, only: str | None = None) -> None:
        check(self.log, only=only)


# ------------------------------------------------- seeded faults (S7a, DL-107)

#: What one host can actually suffer. Every entry is producible by today's
#: code and every one has a rule it is supposed to obey; partition BETWEEN
#: leaders and reroute to a second host are deliberately absent, because the
#: second host does not exist and a stub that always passes is the failure
#: mode this harness exists to prevent (ModelRun's docstring).
FAULT_MENU: tuple[str, ...] = (
    "failover",  # engine death at an arbitrary point, then ss7's barrier
    "failover_losing_the_outcome",  # ...having died between acting and recording
    "duplicate_completion",  # the same adapter report delivered twice
    "stale_completion",  # a report about a run the estate has moved past
    "lost_dispatch",  # decided, never acted on: the ONLY state that re-drives
    "quarantine",  # the supervisor stops answering; new work is HELD
    "drain",  # the operator parks the host under in-flight work
)


@dataclass
class FaultSchedule:
    """What goes wrong and when, decided once from a seed.

    Decided UP FRONT rather than sampled as the run proceeds: the schedule
    is then a pure function of `(seed, steps, menu)`, so a failure can be
    re-run by number without depending on how many draws the engine
    happened to make on the way. `fired` is what actually happened, which
    is not the same list -- a fault whose precondition is absent (a
    duplicate completion for a job that never ran) is a no-op, and a
    harness that reported it as fired would overstate its own coverage.
    """

    seed: int
    plan: dict[int, str]
    fired: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls, seed: int, *, steps: int, menu: Sequence[str] = FAULT_MENU, density: float = 0.6
    ) -> FaultSchedule:
        rng = random.Random(seed)
        plan = {step: rng.choice(list(menu)) for step in range(steps) if rng.random() < density}
        return cls(seed=seed, plan=plan)

    def describe(self) -> str:
        return f"seed={self.seed} plan={self.plan} fired={self.fired}"

    async def at(self, run: ModelRun, step: int) -> None:
        fault = self.plan.get(step)
        if fault is None:
            return
        if await getattr(run, f"fault_{fault}")():
            self.fired.append(f"{step}:{fault}")
