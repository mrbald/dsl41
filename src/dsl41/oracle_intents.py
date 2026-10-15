"""The cascade's payloads and intents (docs/statechart-engine.md ss5, ss8,
ss11; DL-350).

A row of the `job` chart that sends beyond its own move carries one of these
payloads, and an item the hook cannot derive from its row and its payload
reports an intent, which the hook applies after the row's writes
(`oracle.Oracle._perform`). `Move` is one status move's record, shared by its
box rules, its settlement and its wakes. `Program`, `Once` and `Settle` are a
moment's payload: a self-event whose next step the oracle reads when it pops
(`oracle_cascade`).
"""

from __future__ import annotations

from collections.abc import Hashable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

from dsl41.capacity import DemandEntry
from dsl41.ir import JobIR
from dsl41.job_chart import (
    SETTLE,
    Admission,
    FlagMoved,
    Injection,
    Payload,
    Terminator,
)
from dsl41.oracle_facts import ReadmitFacts
from dsl41.oracle_state import LIVE
from dsl41.state_machine import Context, Effects
from dsl41.state_machine import Event as ChartEvent

if TYPE_CHECKING:
    from dsl41.oracle_cascade import OracleCascade

type Send = tuple[Hashable, ChartEvent]

# ---- intents: what the hook performs for a row that cannot derive it


class Intent(Protocol):
    """A write the hook performs after the microstep's row writes."""

    def apply(self, oracle: OracleCascade) -> None: ...


class Move:
    """One status move's record (DL-350): the job, the status it leaves
    and its run, which the row's action reads before the write; the status it
    reaches (`new`), which the hook fills after the write; whether the run's
    units settled early (`done`, job.401) and whether a settlement released
    any (`released`). Its box rules, its settlement and its wakes share it.

    As an intent it is the move's bookkeeping after the write. A move on its
    own (`OracleCascade._after`): a box leaving RUNNING owes the admit-only
    scan, and a move that leaves STARTING or RUNNING registers its late
    settlement for its box rules (DL-340). A row of a batch
    (`OracleCascade._on_cascades`, `batch`): its rank drops now, a box leaving
    RUNNING owes the scan, and the batch's second phase reads it."""

    __slots__ = ("job", "old", "run", "new", "done", "released", "batch")

    def __init__(self, job: str, old: str, run: int, batch: Batch | None = None) -> None:
        self.job = job
        self.old = old
        self.run = run
        self.new = ""
        self.done = False
        self.released = False
        self.batch = batch

    @property
    def releasing(self) -> bool:
        """The move leaves STARTING or RUNNING."""
        return self.old in LIVE and self.new not in LIVE

    def apply(self, oracle: OracleCascade) -> None:
        self.new = oracle._runtime(self.job).status
        if oracle._box_stopped(self.job, self.old, self.new):
            oracle._scan_owed = True
        if self.batch is not None:
            oracle._drop_rank(self.job)
            self.batch.rows.append(self)
        elif self.releasing:
            lates = cast(list[Move], oracle._late_settlements.setdefault(self.job, []))
            lates.append(self)


@dataclass(eq=False)
class Batch:
    """One batch INACTIVE path (DL-242): the box it begins at, the rows its
    first phase wrote and settled, in order, and how they are written: a box
    start's reset (`starting`, the box's own move to STARTING between the
    phases) or a box set INACTIVE (`clear`, its exit codes; `code`, the
    first row's)."""

    box: str
    starting: Starting | None = None
    clear: bool = False
    code: int | None = None
    rows: list[Move] = field(default_factory=list)


@dataclass(frozen=True)
class Readmitted:
    """A waiter's `readmit` moved it (admitted, cancelled, left): the scan's
    pass ends there and starts again (`OracleCascade._scan`)."""

    def apply(self, oracle: OracleCascade) -> None:
        oracle._readmitted = True


@dataclass(frozen=True)
class WatchReadmit:
    """The scan sends a `readmit`: whether it moves is read after its cascade."""

    def apply(self, oracle: OracleCascade) -> None:
        oracle._readmitted = False


@dataclass(frozen=True)
class ScanState:
    """The scan's bookkeeping (DL-247, DL-255): the fields given, set."""

    in_wake: bool | None = None
    owed: bool | None = None
    full: bool | None = None

    def apply(self, oracle: OracleCascade) -> None:
        if self.in_wake is not None:
            oracle._in_wake = self.in_wake
        if self.owed is not None:
            oracle._scan_owed = self.owed
        if self.full is not None:
            oracle._full_scan_requested = self.full


@dataclass(frozen=True)
class OpenPass:
    """A box's RUNNING found no window pass pending: its own pass collects
    every box run its cascade starts (DL-246)."""

    def apply(self, oracle: OracleCascade) -> None:
        oracle._window_starts = []


@dataclass(frozen=True)
class Register:
    """A box run joins the pending window pass, after its members' attempts."""

    box: str
    run: int
    cause: str

    def apply(self, oracle: OracleCascade) -> None:
        starts = oracle._window_starts
        assert starts is not None, "a box run registered with no window pass pending"
        starts.append((self.box, self.run, self.cause))


@dataclass(frozen=True)
class ClosePass:
    """The pass's list is read: a box started by its decisions begins its own."""

    def apply(self, oracle: OracleCascade) -> None:
        oracle._window_starts = None


@dataclass(frozen=True)
class SettledEarly:
    """job.401 settles the ended run now: its late settlement is done, and the
    row's rank drops (`OracleCascade._drop_rank`)."""

    move: Move

    def apply(self, oracle: OracleCascade) -> None:
        self.move.done = True
        oracle._drop_rank(self.move.job)


@dataclass(frozen=True)
class ClearCodes:
    """A box's ON_NOEXEC clears the exit code of every INACTIVE row it covers (DL-243)."""

    jobs: tuple[str, ...]

    def apply(self, oracle: OracleCascade) -> None:
        for job in self.jobs:
            row = oracle._runtime(job)
            if row.status == "INACTIVE" and row.exit_code is not None:
                oracle.store.clear_exit_code(job)


@dataclass(frozen=True)
class DeferToOpening:
    """A tickless waiter that failed the day check waits for its next eligible
    day's opening (DL-257)."""

    job_ir: JobIR
    cause: str

    def apply(self, oracle: OracleCascade) -> None:
        oracle._defer_to_next_opening(self.job_ir, self.cause)


# ---- the payloads the cascade sends


@dataclass(frozen=True)
class StatusInput(Injection):
    """An injected STATUS with the exit code its write records."""

    code: int | None = field(default=None, compare=False)


@dataclass(frozen=True)
class MemberEnd(Terminator):
    """`member ends FAILURE or TERMINATED`: the member and its end, which the
    box's TERMINATED line names."""

    member: str = field(default="", compare=False)
    new: str = field(default="", compare=False)


@dataclass(frozen=True)
class Rule(Payload):
    """A box's moment on a job it contains: the box, which the job's lines
    name, and the batch its reset or its INACTIVE belongs to."""

    box: str = field(default="", compare=False)
    batch: Batch | None = field(default=None, compare=False)


@dataclass(frozen=True)
class AdmissionFacts(Admission):
    """An admitted start's units: the demand vector it reserves or takes over."""

    vector: tuple[DemandEntry, ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class RunStart(Payload):
    """`actual start`'s payload: the run's cause (`start_run`), and whether the
    run arms its term_run_time first (a bypass does not)."""

    cause: str = field(default="", compare=False)
    arm: bool = field(default=True, compare=False)


@dataclass(frozen=True)
class Starting(Payload):
    """`starting`'s payload: the cause of the move into STARTING."""

    cause: str = field(default="", compare=False)


@dataclass(frozen=True)
class FlagFacts(FlagMoved):
    """`flag moved` after a waiter's failed recheck, with the scan's facts."""

    facts: ReadmitFacts | None = field(default=None, compare=False)


@dataclass(frozen=True)
class Step:
    """One step of a moment: the items it sends, and the intents its hook performs."""

    send: tuple[Send, ...] = ()
    intents: tuple[Intent, ...] = ()


class Program:
    """A moment of several steps (DL-350). Each pop takes one step of
    `steps`, a generator of the oracle's that reads the store when it resumes
    and never writes: the step's items, then the moment again, so its next
    step reads the state their whole cascade left. A pop that finds the
    generator done sends nothing."""

    __slots__ = ("_name", "_steps")

    def __init__(self, name: str, steps: Iterator[Step]) -> None:
        self._name = name
        self._steps = steps

    def __repr__(self) -> str:
        return f"<{self._name}>"

    def step(self, ctx: Context) -> Effects | None:
        step = next(self._steps, None)
        if step is None:
            return None
        again = (ctx.key, ChartEvent(ctx.event.name, self))
        return Effects(send=(*step.send, again), intents=step.intents)


class Once:
    """A moment of one step: `steps` gives it when it pops, and nothing follows."""

    __slots__ = ("_name", "_step")

    def __init__(self, name: str, step: Iterator[Step]) -> None:
        self._name = name
        self._step = step

    def __repr__(self) -> str:
        return f"<{self._name}>"

    def step(self, ctx: Context) -> Effects | None:
        step = next(self._step, None)
        return None if step is None else Effects(send=step.send, intents=step.intents)


class Settle(Once):
    """`settle`'s payload: the move whose run settles, which its hook reads
    on either row, and the step that sends the settlement."""

    __slots__ = ("move",)

    def __init__(self, move: Move, step: Iterator[Step]) -> None:
        super().__init__(f"{SETTLE} of {move.job}", step)
        self.move = move


@dataclass(frozen=True)
class Notice:
    """A move whose box rules run with no move record: the batch's second
    phase, and a window skip of a member already INACTIVE."""

    job: str
    old: str
    new: str


class Moved(Protocol):
    """A move as its box rules read it: the job and its two statuses."""

    @property
    def job(self) -> str: ...

    @property
    def old(self) -> str: ...

    @property
    def new(self) -> str: ...
