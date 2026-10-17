"""The engine's seal boundary as a statechart (period-model ss6-7, DL-327).

Spec: docs/period-model.md ss6 (the cutoff barrier) and ss7 (the seal operation), and
docs/statechart-engine.md for how the executor reads the declaration. `ssN` here names
period-model unless the sentence names another document.

`SEAL_BOUNDARY` is the declaration. The engine owns one `Executor` and runs one instance per
seal request, keyed by the request, from `submit_seal` until the boundary rests in `sealed` or
`aborted`; `stopped` ends the engine. `Engine._run_boundary` runs the steps and, at each
decision point, posts what happened with the facts the rows read computed into the payload.
The final state the boundary rests in reports one `SealExit` from its entry action, which the
engine carries out: answer and raise `PeriodSealed`, abort and answer, or re-raise. Actions
never raise and do no I/O.

The seal defers nothing. An input stamped after T refuses the boundary
(ss6 step 7, `seal_input_after_cutoff`); inputs are not events of this
machine. A `StatechartError` raised inside the seal never enters this chart:
the engine re-raises it before any post, and exits 7 (statechart-engine
ss5, concurrency-model ss4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from dsl41.boundary import BoundaryFailStop
from dsl41.state_machine import Context, Effects, Exclusion, Region, Row, State, Statechart

#: The barrier parked (row .01's note).
PARKED: Final = "parked"
#: The boundary may commit (row .02's note).
QUIESCED: Final = "quiesced"
#: `commit_boundary` returned: the `seal` record is durable (ss6 step 8).
COMMITTED: Final = "committed"
#: An exception left the boundary's steps. Its payload is a `SealFault`.
EXCEPTION: Final = "exception"

#: Where a boundary can be: the leaf states of `SEAL_BOUNDARY`.
PHASES: Final = frozenset({"requested", "frozen", "committing", "sealed", "aborted", "stopped"})
_FINALS: Final = frozenset({"sealed", "aborted", "stopped"})


def seal_phase(active: frozenset[str]) -> str:
    """The boundary's phase: its one active leaf state."""
    (phase,) = active & PHASES
    return phase


@dataclass(frozen=True, repr=False)
class SealFault:
    """An exception inside a seal, with the facts the exception rows read.

    The engine reads them once, before the post. `fence_lost` is read only for an exception
    that is not `BoundaryFailStop`. The other three are DL-274's flags: the index of an attempt
    admitted during the seal and not fully applied, the kind of an engine-made input the drain
    could not admit, and an unfinished WAL append."""

    error: Exception
    fence_lost: bool = False
    applying: int | None = None
    unadmitted: str | None = None
    append_unfinished: bool = False

    def __repr__(self) -> str:
        """The error first, so a summary of the payload shows what the seal met."""
        return (
            f"SealFault({self.error!r}, fence_lost={self.fence_lost},"
            f" applying={self.applying}, unadmitted={self.unadmitted!r},"
            f" append_unfinished={self.append_unfinished})"
        )


@dataclass(frozen=True)
class SealExit:
    """What the engine does once a boundary rests in a final state: `sealed`
    answers the request and raises `PeriodSealed`; `abort` runs
    `abort_boundary` and fails the request with the exception; `stop`
    re-raises it, with `note` added when there is one."""

    verdict: Literal["sealed", "abort", "stop"]
    note: str = ""


def stops_by_type(error: Exception) -> bool:
    """`BoundaryFailStop`: the exception whose type alone decides the exit."""
    return isinstance(error, BoundaryFailStop)


def _flagged(fault: SealFault) -> bool:
    return fault.applying is not None or fault.unadmitted is not None or fault.append_unfinished


# -- the guards and the predicates they call. A registered guard's docstring is its rows' prose.
# Every fact they read is in the `SealFault` the engine built before the post, so no guard reads
# a lazy fact.


def _past_ponr(ctx: Context) -> bool:
    """The exception is `BoundaryFailStop`."""
    return stops_by_type(ctx.event.payload.error)


def _fence_lost(ctx: Context) -> bool:
    """Not `BoundaryFailStop`, and the fence is lost."""
    fault: SealFault = ctx.event.payload
    return not stops_by_type(fault.error) and fault.fence_lost


def _stops(ctx: Context) -> bool:
    """Past the point of no return, or the fence lost. Past it, the `seal`
    append may have begun, so the outcome is unknown and recovery decides
    (PR-28d): the engine exits 4. A leader that cannot prove it leads does not
    reopen admission (DL-101, PR-28b). Either stops with no abort and no note,
    and takes precedence over DL-274's flags."""
    return _past_ponr(ctx) or _fence_lost(ctx)


def _in_doubt(ctx: Context) -> bool:
    """Not `BoundaryFailStop`, the fence intact, and memory or the WAL in doubt: an
    attempt admitted during the seal is not fully applied, an engine-made input
    the drain could not admit would be lost, or a WAL append is unfinished
    (DL-274)."""
    fault: SealFault = ctx.event.payload
    return not stops_by_type(fault.error) and not fault.fence_lost and _flagged(fault)


def _reversible(ctx: Context) -> bool:
    """Not `BoundaryFailStop`, the fence intact, and nothing in doubt: every other
    exception before the `seal` append. A refusal before the freeze, a phase-2
    refusal, the drain's timeout, a late input (`seal_input_after_cutoff`) and
    a failed sidecar write are among them (PR-28b)."""
    fault: SealFault = ctx.event.payload
    return not stops_by_type(fault.error) and not fault.fence_lost and not _flagged(fault)


def _over(ctx: Context) -> bool:
    """The boundary rests in a final state. Nothing posts to it then: an
    aborted boundary is forgotten at once, a sealed one once the answer has
    had its turns, and a stop ends the engine. In flight, an event out of
    order finds no row and raises `UnhandledEvent`."""
    return bool(ctx.active & _FINALS)


# -- the entry actions of the finals: each reports the one `SealExit` of its state. Its intent
# joins the outcome of the row that entered the state (statechart-engine ss4).


def _answer(_ctx: Context) -> Effects:
    """`sealed`: answer the request with the committed boundary, then raise
    `PeriodSealed`: the engine exits 3 (PR-30b)."""
    return Effects(intents=(SealExit("sealed"),))


def _abort(_ctx: Context) -> Effects:
    """`aborted`: run `abort_boundary` and fail the request with the exception.
    The abort clears the sealing flag, reopens control admission, restarts
    scheduler admission and unparks FW polls. It touches no row, and C1 carries
    on (PR-28b)."""
    return Effects(intents=(SealExit("abort"),))


def _stop(ctx: Context) -> Effects:
    """`stopped`: re-raise without an abort. The request is not answered.

    In doubt, a note names the first of DL-274's flags that is set. An attempt
    whose answer was owed is not answered either, and resume repairs the WAL
    tail and rebuilds from the WAL. Past the point of no return, or with the
    fence lost, there is no note."""
    return Effects(intents=(SealExit("stop", _note(ctx) if _in_doubt(ctx) else ""),))


def _note(ctx: Context) -> str:
    """Which of DL-274's flags stopped the seal, in this order: the attempt
    applying, the unadmitted input, the unfinished append."""
    fault: SealFault = ctx.event.payload
    if fault.applying is not None:
        return (
            f"DL-274: the seal stopped the engine with attempt {fault.applying}"
            " admitted and not fully applied; resume rebuilds it from the WAL"
        )
    if fault.unadmitted is not None:
        return (
            f"DL-274: the seal stopped the engine rather than lose an unadmitted"
            f" {fault.unadmitted} input; resume observes it again"
        )
    return (
        "DL-274: the seal stopped the engine on an unfinished WAL append; resume repairs the tail"
    )


_OUT_OF_ORDER = (
    "the engine posts parked, quiesced and committed once each, in that order,"
    " from straight-line code; one out of order raises UnhandledEvent, which"
    " the chart's own test pins"
)

#: The engine's seal boundary (ss6-7), one instance per seal request. A request
#: while another boundary is in flight is refused `seal_in_flight`
#: (`SealInFlight`) and never becomes an instance. The three exception rows
#: partition every exception in this order of precedence: `BoundaryFailStop`
#: or the fence (`stops`), then DL-274's flags, else the abort. `stops` is the
#: disjunction of two predicates, and `in_doubt` and `reversible` each exclude
#: both of them, so the partition is total and exclusive, not ordered (DL-327).
#: Each final state's entry reports the boundary's exit. No row stops on a
#: transition violation (DL-353).
SEAL_BOUNDARY: Final = Statechart(
    name="seal_boundary",
    root=State(
        "boundary",
        regions=(
            Region(
                "phase",
                "in_flight",
                (
                    State(
                        "in_flight",
                        regions=(
                            Region(
                                "progress",
                                "requested",
                                (
                                    State("requested"),
                                    State("frozen"),
                                    State("committing"),
                                ),
                            ),
                        ),
                    ),
                    State("sealed", entry="answer", final=True),
                    State("aborted", entry="abort", final=True),
                    State("stopped", entry="stop", final=True),
                ),
            ),
        ),
    ),
    rows=(
        Row(
            "seal_boundary.01",
            "requested",
            PARKED,
            "frozen",
            cite="period-model ss6 step 2, ss8, PR-28c",
            note="The barrier parked: admission is frozen and FW polls are parked (ss6 step 2).",
        ),
        Row(
            "seal_boundary.02",
            "frozen",
            QUIESCED,
            "committing",
            cite="period-model ss6 steps 3-8, PR-27",
            note=(
                "The cutoff is chosen, drained and quiescent, and the supervisor proof"
                " holds (ss6 steps 3-7, ss8). `commit_boundary` runs next (step 8)."
            ),
        ),
        Row(
            "seal_boundary.03",
            "committing",
            COMMITTED,
            "sealed",
            cite="period-model ss7",
        ),
        Row(
            "seal_boundary.04",
            "in_flight",
            EXCEPTION,
            "aborted",
            guard="reversible",
            cite="period-model ss7, PR-28b",
        ),
        Row(
            "seal_boundary.13",
            "in_flight",
            EXCEPTION,
            "stopped",
            guard="stops",
            cite="period-model ss7, DL-101, PR-28b",
        ),
        Row(
            "seal_boundary.06",
            "in_flight",
            EXCEPTION,
            "stopped",
            guard="in_doubt",
            cite="DL-274",
        ),
        Row(
            "seal_boundary.09",
            "boundary",
            PARKED,
            guard="over",
            kind="ignored",
            cite="statechart-engine ss10",
        ),
        Row(
            "seal_boundary.10",
            "boundary",
            QUIESCED,
            guard="over",
            kind="ignored",
            cite="statechart-engine ss10",
        ),
        Row(
            "seal_boundary.11",
            "boundary",
            COMMITTED,
            guard="over",
            kind="ignored",
            cite="statechart-engine ss10",
        ),
        Row(
            "seal_boundary.12",
            "boundary",
            EXCEPTION,
            guard="over",
            kind="ignored",
            cite="statechart-engine ss10",
            false_unreachable=(
                "rows .04, .06 and .13 partition every exception, so the walk"
                " reaches the root only from a final state"
            ),
        ),
    ),
    guards={
        "stops": _stops,
        "in_doubt": _in_doubt,
        "reversible": _reversible,
        "over": _over,
    },
    actions={
        "answer": _answer,
        "abort": _abort,
        "stop": _stop,
    },
    clock="logical",
    exclusions=(
        Exclusion("requested", QUIESCED, _OUT_OF_ORDER),
        Exclusion("requested", COMMITTED, _OUT_OF_ORDER),
        Exclusion("frozen", PARKED, _OUT_OF_ORDER),
        Exclusion("frozen", COMMITTED, _OUT_OF_ORDER),
        Exclusion("committing", PARKED, _OUT_OF_ORDER),
        Exclusion("committing", QUIESCED, _OUT_OF_ORDER),
    ),
)
