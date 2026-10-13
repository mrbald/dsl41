"""The agreement check of the second oracle (docs/statechart-engine.md §11,
DL-341): the job chart's move for an input or a start, recorded at its
dispatch, and today's writes compared with it.

The code writes the status the chart reached, so the status agrees by
construction, and the harness is what judges a wrong status row. The check covers what the code still decides: the holding, which today's
settlement writes; the flags, which the converted inputs move (a flag input, a
waiter's kill, a forced start's clears of ice and hold, a start's arm and its
consumption, the arm of a waiter that leaves the queue) and the other converted
writes leave alone (the flag moves of code the chart does not drive yet, a
box's members among them, are realigned before the dispatch, not checked); and
the job type behind the INACTIVE pair, which `_inject_inactive` reads from the
catalog (a box that fires job.120, or a job that fires job.135, is a
`StatechartError`). A move that leaves STARTING or RUNNING has its flags
compared when its status is written, before the box rules run, and its holding
when its own settlement has run (late, early in a restart by DL-340, or first
on the batch path). Any other move settles nothing, so its holding is compared
with the flags at the write, before the box rules can start the job again. A
later move of the same job by the cascade, which the chart does not declare
yet, is never taken for a difference. A move compared inside the cascade of
another (a readmission in the wakes of the box rules) leaves the other waiting
for its own settlement. A row that the batch INACTIVE path writes for a box
rule (a box start's reset, a box's cascade) is compared whole once the batch
has written and settled it. A difference is a `StatechartError` naming both
configurations.

`AgreeingOracle` is `Oracle` with that check at today's write points (the
trace line that follows every status write, the settlement of a run's
units); `oracle_chart.ChartOracle` subclasses it, records the moves and
compares every row with its instance at construction. The split from
`oracle_chart` (DL-345) moved these methods without changing them, so
the stack under a cascade is as deep as before.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from dsl41.ir import CatalogIR
from dsl41.job_chart import LEAVE_LIVE, STARTED, projection, row_configuration
from dsl41.oracle import Oracle
from dsl41.oracle_state import LIVE, CarriedRows
from dsl41.semantics import SemanticSwitches
from dsl41.state_machine import Event as ChartEvent
from dsl41.state_machine import Outcome, StatechartError


@dataclass
class Agreement:
    """The chart's move for one input (a STATUS, a kill, a term_run_time, a
    flag input and the status its `flag moved` fires) or one start, waiting for
    today's writes to compare against: the job, its instance's key, the event,
    the configuration the chart left, the move as its two statuses, and whether
    it leaves STARTING or RUNNING, so that its holding waits for its
    settlement. `written`: the first write is compared. `moves`: for a start,
    whether the chart's row moves the status, so that a status write must
    come. `outer`: the move this one's block began inside, still waiting for
    its settlement (a readmission's in the cascade of an input's move); it
    waits again once this one is compared."""

    job: str
    key: str
    event: ChartEvent
    active: frozenset[str]
    move: tuple[str, str]
    releasing: bool
    written: bool = False
    moves: bool = True
    outer: Agreement | None = None


class AgreeingOracle(Oracle):
    """`Oracle` whose job writes are compared with the job chart's moves."""

    def __init__(
        self,
        catalog: CatalogIR,
        *,
        carried: CarriedRows | None = None,
        tz_aliases: Mapping[str, str] | None = None,
        default_tz: str | None = None,
        semantics: SemanticSwitches | None = None,
    ) -> None:
        super().__init__(
            catalog,
            carried=carried,
            tz_aliases=tz_aliases,
            default_tz=default_tz,
            semantics=semantics,
        )
        #: the STATUS move being written, between its dispatch and its settlement
        self._agreement: Agreement | None = None
        #: a start's status move, between its dispatch and its first status write
        self._start_move: Agreement | None = None
        #: the moves a batch INACTIVE path writes for the jobs the box rules
        #: address (a box start's reset, a box's cascade), each compared when
        #: its row is written and settled; None outside a batch
        self._batch: dict[str, Agreement] | None = None

    @contextmanager
    def _moving(self, job: str, key: str, event: ChartEvent, old: str) -> Iterator[Agreement]:
        """The status move the chart just made for an input, from `old` to the
        configuration its instance is in, waiting for today's writes inside
        the block: its status and flags are compared at the job's first write,
        and its holding there too or, for a move that leaves STARTING or
        RUNNING, when its own settlement has run. A block can begin inside
        the cascade of another's move (a readmission in the wakes of its box
        rules): that move waits again once this one is compared. The block
        runs in the caller's frame, so it adds no frame under the cascade."""
        active = self.store.machines.active(key)
        new = projection(active)[0]
        releasing = old in LIVE and new not in LIVE
        agreement = Agreement(job, key, event, active, (old, new), releasing, outer=self._agreement)
        self._agreement = agreement
        try:
            yield agreement
            if self._agreement is agreement:
                raise self._differ("the move's settlement never ran", agreement)
        finally:
            if self._agreement is agreement:
                self._agreement = agreement.outer

    def _unwritten(
        self, job: str, event: ChartEvent, fired: tuple[str, ...], what: str
    ) -> StatechartError:
        """The chart fired rows the oracle has no write for."""
        error = StatechartError(
            f"job {job!r}: {event.name} fired {', '.join(fired) or 'no row'}, and no {what}"
            " the oracle writes"
        )
        store = self.store
        return store.machines.explain(error, store.job_key(job), event)

    def _record(self, job: str, transition: str, cause: str) -> None:
        """Every status write is followed at once by its trace line, before
        the box rules or the settlement run, so the first write is compared
        here. A start's writes are compared here too, and the RUNNING that
        follows an admitted start dispatches `started` first."""
        self._compare_written(job)
        if self._start_move is not None or transition == "STARTING->RUNNING":
            self._compare_start_write(job, transition)
        super()._record(job, transition, cause)

    def _agree_after(self, agreement: Agreement, what: str = "the start's writes") -> None:
        """An attempt or a flag input whose row moves no status: after its own
        writes (the forced clears, the arm, the flag), the row is the chart's
        configuration."""
        if self._row_active(agreement.job) != projection(agreement.active):
            raise self._differ(f"{what} differ from the chart's", agreement)

    def _compare_start_write(self, job: str, transition: str) -> None:
        """A start's first status write is the chart's move; a RUNNING after an
        admitted start is the chart's `started` (job.105, job.125), dispatched
        to the instance the admission left in STARTING, a readmission's
        included. A RUNNING that no admission of the chart precedes (an
        injected status) is not the start family's. A window's skip that the
        chart says moves nothing (job.180) is watched the same way until its
        writes are done: a status write there differs from the chart's
        configuration."""
        agreement = self._start_move
        if agreement is not None and agreement.job == job and "->" in transition:
            self._start_move = None
            if self._row_active(job) != projection(agreement.active):
                raise self._differ("the start's status write differs", agreement)
            return
        if transition != "STARTING->RUNNING":
            return
        machines = self.store.machines
        key = self.store.job_key(job)
        if "STARTING" not in machines.active(key):
            return
        event = ChartEvent(STARTED)
        fired = machines.dispatch(key, event)[0].rows
        started = Agreement(job, key, event, machines.active(key), ("STARTING", "RUNNING"), False)
        job_type = self.catalog.jobs[job].job_type
        if fired != (("job.125",) if job_type == "BOX" else ("job.105",)):
            raise self._differ(
                f"started fired {', '.join(fired) or 'no row'} for a {job_type} job", started
            )
        if self._row_active(job) != projection(started.active):
            raise self._differ("the RUNNING write differs", started)

    def _settle_row(self, job: str, old: str, new: str, *, run: int | None = None) -> bool:
        """A move's own settlement compares its holding (`_moving`); a row of
        a batch is compared whole once it is written and settled, before the
        batch's box rules and wakes run."""
        released = super()._settle_row(job, old, new, run=run)
        batched = None if self._batch is None else self._batch.pop(job, None)
        if batched is not None:
            self._agree_after(batched, "the batch's writes")
        agreement = self._agreement
        if agreement is not None and agreement.job == job and agreement.move == (old, new):
            self._agreement = agreement.outer
            if self._row_active(job)[-1] != projection(agreement.active)[-1]:
                raise self._differ("the holding differs after the settlement", agreement)
        return released

    def _compare_written(self, job: str) -> None:
        """At the first write of the job after the dispatch, the status
        move's: its status and flags are the chart's. A move that does not
        leave STARTING or RUNNING settles nothing, so its holding is compared
        here too, and its agreement closes: the box rules that follow may start
        the job again and take units for the new run."""
        agreement = self._agreement
        if agreement is None or agreement.job != job or agreement.written:
            return
        agreement.written = True
        width, what = 5, "the status and the flags differ"
        if not agreement.releasing:
            self._agreement = agreement.outer
            width, what = 6, "the configuration differs"
        if self._row_active(job)[:width] != projection(agreement.active)[:width]:
            raise self._differ(f"{what} as written", agreement)

    def _row_active(self, job: str) -> tuple[str, bool, bool, bool, bool, str]:
        return projection(row_configuration(self.store.job[job]))

    def _differ(self, what: str, agreement: Agreement) -> StatechartError:
        row = row_configuration(self.store.job[agreement.job])
        error = StatechartError(
            f"job {agreement.job!r}: {what}: the chart's configuration is"
            f" {listed(agreement.active)}, the row's {listed(row)}"
        )
        return self.store.machines.explain(error, agreement.key, agreement.event)


def fired_rows(outcomes: list[Outcome]) -> tuple[str, ...]:
    """Every row an input fired on its instance, in order, but the settlement
    of the run it ends (`leave STARTING or RUNNING`), which `_moving` compares
    as the holding. A row beyond the ones a handler performs is named in its
    error."""
    return tuple(r for o in outcomes if o.event.name != LEAVE_LIVE for r in o.rows)


def listed(active: frozenset[str]) -> str:
    return ", ".join(sorted(active))
