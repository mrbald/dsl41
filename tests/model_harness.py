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
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from dsl41.ir import CatalogIR, lower_source
from dsl41.oracle import Event
from dsl41.runner import Engine, resume_run, start_run
from dsl41.runner_adapters import AdapterContext, FakeAdapter
from dsl41.runner_clock import VirtualClock

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

    def _mode(self, job_ir: JobIR, run_number: int) -> str:
        if job_ir.job_type == "FW":
            return WATCH
        reattach = getattr(self.inner, "reattach", None)
        if reattach is not None and (job_ir.name, run_number) in reattach:
            return ATTACH
        return EXEC

    async def run(self, job_ir: JobIR, run_number: int, ctx: AdapterContext) -> AdapterResult:
        spawn = self.log.begin(
            job=job_ir.name,
            run_number=run_number,
            at=ctx.clock.now(),
            mode=self._mode(job_ir, run_number),
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
    Partition, reroute and leader failover arrive with S5/S6 -- they are
    absent here rather than stubbed, because a stub that always passes is
    the failure mode this harness exists to prevent.
    """

    def __init__(
        self,
        jil: str,
        run_root: Path,
        *,
        script: dict[tuple[str, int], tuple[float, int]] | None = None,
    ) -> None:
        self.catalog: CatalogIR = lower_source(jil)
        self.run_root = run_root
        self.script = dict(script or {})
        self.log = SpawnLog()
        self.clock = VirtualClock()
        self.engine: Engine | None = None

    def _adapters(self) -> dict[str, JobAdapter]:
        inner = FakeAdapter(self.script, default=None)  # unscripted == inert park
        recording = RecordingAdapter(inner, self.log)
        return {"CMD": recording, "FW": recording}

    def start(self) -> Engine:
        self.engine = start_run(
            self.catalog, self.run_root, clock=self.clock, adapters=self._adapters()
        )
        return self.engine

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
        arrive the way a real adapter would deliver it."""
        self.live._enqueue(ev, is_completion=True, source=source)

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

    async def resume(self, **kwargs: object) -> Engine:
        assert self.engine is None, "crash() before resume(): two live engines is a different bug"
        self.engine = await resume_run(
            self.catalog,
            self.run_root,
            clock=self.clock,
            adapters=self._adapters(),
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
