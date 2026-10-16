"""The `queue_scan` statechart: the coordinator of the queue's scan (docs/statechart-engine.md
§8, §11; DL-50, DL-247, DL-255, DL-345, DL-372), registered in `machines.MACHINES`.

One instance per oracle lives on the state owner's executor beside the job instances, under
`SCAN_KEY` (`RuntimeState.rebuild_machines`), and rests in `idle` between inputs. Every
sender of a scan sends it one `scan`. The payload (`ScanPass`, `oracle_intents.ScanFacts`) is
the scan's record: its mode, the waiters' order a pass reads when it begins, and the turn the
pass has reached. Each completion of a transient state carries that payload (§4, DL-364), so
every phase of one scan reads one record.

Shape. `idle` between scans; `pass` while one runs, with two regions:

- `turn`: `next_waiter` starts the pass's first turn; `awaiting_readmit` sends the waiter's
  `readmit` from its entry, and its completion, queued behind the readmission's whole cascade
  (§5), reads whether the readmission moved the waiter; `done` decides the pass's end.
- `request`: `no_request`, or `full_requested` once a scan was asked inside the pass.

A state waits for a cascade; a decision that waits for nothing is a choice, resolved inside
the microstep (§4). The choice `turn_choice` gives the turn to the next waiter of the pass's
list still queued, its status read at its turn, or ends the pass at `done`.

Entering `pass` begins a pass: its entry reads the waiters' order and reports the bookkeeping
the oracle keeps for the input (`ScanState`, `ClearReadmitted`), and its regions start in
`next_waiter` and `no_request`. A pass ends at `done`. It begins again when its last
readmission moved its waiter, and it upgrades an admit-only scan to a full one when a scan was
asked inside it (DL-247). Otherwise the scan ends at `idle`.

A scan asked inside a pass sends nothing and never leaves `awaiting_readmit`, whose
completion is still queued: its rows are the `request` region's. Every sender inside a cascade
asks a full scan; the admit-only scan the input owes (DL-255) is a root item after the input's
macrosteps, so it never meets a pass.

Whether a readmission moved its waiter is the oracle's latch (`Readmitted`), which the `job`
chart's readmit rows report and a pass's entry clears.
"""

from __future__ import annotations

from typing import Final, Protocol, runtime_checkable

from dsl41.state_machine import (
    COMPLETION,
    Branch,
    Choice,
    Context,
    Effects,
    Region,
    Row,
    State,
    Statechart,
    StatechartError,
)

#: The event every sender of the queue's scan sends to the coordinator.
SCAN: Final = "scan"
#: The coordinator's instance key on the state owner's executor: one instance per oracle.
SCAN_KEY: Final = "queue_scan"


@runtime_checkable
class ScanPass(Protocol):
    """A `scan` payload: one scan's record, which every completion of its transient states
    carries (DL-364; `oracle_intents.ScanFacts`).

    `keep_stopped`: the scan only admits (DL-247): a waiter of a stopped box stays queued.
    `queued`: a waiter of the pass's list at or after its turn is still queued, read now; the
    record keeps the waiter it found for `readmit`.
    `moved`: the pass's last readmission moved its waiter (`Readmitted`). `begin` reads the
    waiters' order for a new pass, from the first turn, and reports the bookkeeping;
    `readmit` takes the turn `queued` found and sends its `readmit`; `upgrade` makes the
    scan a full one."""

    @property
    def keep_stopped(self) -> bool: ...
    @property
    def queued(self) -> bool: ...
    @property
    def moved(self) -> bool: ...

    def begin(self) -> Effects: ...
    def readmit(self) -> Effects: ...
    def upgrade(self) -> None: ...


def _pass(ctx: Context) -> ScanPass:
    payload = ctx.event.payload
    if not isinstance(payload, ScanPass):
        raise StatechartError(f"{ctx.event.name or 'completion'}: a scan with no record")
    return payload


# ---- guards


def waiter_queued(ctx: Context) -> bool:
    """A waiter of the pass's list from its turn on is QUE_WAIT now: it takes the next turn."""
    return _pass(ctx).queued


def waiter_moved(ctx: Context) -> bool:
    """The turn's readmission reported that it admitted, cancelled or dequeued its waiter."""
    return _pass(ctx).moved


def waiter_stayed(ctx: Context) -> bool:
    """The turn's readmission left its waiter queued."""
    return not waiter_moved(ctx)


def pass_upgrades(ctx: Context) -> bool:
    """An admit-only scan was asked for a full one inside the pass: a full pass follows,
    whether or not the pass ended at a move (DL-247)."""
    return _pass(ctx).keep_stopped and "full_requested" in ctx.active


def pass_restarts(ctx: Context) -> bool:
    """The pass ended at a moved waiter and no upgrade is owed: a pass of the same mode
    follows."""
    return waiter_moved(ctx) and not pass_upgrades(ctx)


def scan_ends(ctx: Context) -> bool:
    """The pass ended with no waiter moved and no upgrade owed: the scan ends."""
    return waiter_stayed(ctx) and not pass_upgrades(ctx)


GUARDS: Final = {
    guard.__name__: guard
    for guard in (
        waiter_queued,
        waiter_moved,
        waiter_stayed,
        pass_upgrades,
        pass_restarts,
        scan_ends,
    )
}

# ---- actions


def begins_pass(ctx: Context) -> Effects:
    """`pass`'s entry: the pass reads the waiters' order, from the first turn, and reports the
    bookkeeping the record gives (the owed scan's mark cleared, the latch cleared)."""
    return _pass(ctx).begin()


def readmits(ctx: Context) -> Effects:
    """`awaiting_readmit`'s entry: the turn `turn_choice` found, its waiter's `readmit` sent."""
    return _pass(ctx).readmit()


def upgrades(ctx: Context) -> None:
    """The admit-only scan becomes a full one before its next pass begins (DL-247)."""
    _pass(ctx).upgrade()


ACTIONS: Final = {action.__name__: action for action in (begins_pass, readmits, upgrades)}

# ---- states and rows

ROOT: Final = State(
    "queue_scan",
    regions=(
        Region(
            "scan",
            "idle",
            (
                State("idle"),
                State(
                    "pass",
                    regions=(
                        Region(
                            "turn",
                            "next_waiter",
                            (
                                State("next_waiter"),
                                State("awaiting_readmit", entry="readmits"),
                                State("done"),
                            ),
                        ),
                        Region(
                            "request", "no_request", (State("no_request"), State("full_requested"))
                        ),
                    ),
                    entry="begins_pass",
                ),
            ),
        ),
    ),
)

_CITE: Final = "DL-50, DL-247, DL-372"

ROWS: Final = (
    Row(
        "queue_scan.01",
        "idle",
        SCAN,
        "pass",
        cite=_CITE,
        note="A scan with no pass running begins one.",
    ),
    Row(
        "queue_scan.02",
        "no_request",
        SCAN,
        "full_requested",
        cite=_CITE,
        note="A scan asked inside a pass sends nothing and asks a full pass of an admit-only one.",
    ),
    Row(
        "queue_scan.03",
        "full_requested",
        SCAN,
        kind="internal",
        cite=_CITE,
        note="A full pass is asked already.",
    ),
    Row(
        "queue_scan.04",
        "next_waiter",
        COMPLETION,
        "turn_choice",
        cite=_CITE,
        note="The pass's first turn.",
    ),
    Row("queue_scan.05", "awaiting_readmit", COMPLETION, "done", "waiter_moved", cite=_CITE),
    Row(
        "queue_scan.06", "awaiting_readmit", COMPLETION, "turn_choice", "waiter_stayed", cite=_CITE
    ),
    Row("queue_scan.07", "done", COMPLETION, "pass", "pass_upgrades", "upgrades", cite=_CITE),
    Row("queue_scan.08", "done", COMPLETION, "pass", "pass_restarts", cite=_CITE),
    Row("queue_scan.09", "done", COMPLETION, "idle", "scan_ends", cite=_CITE),
)

CHOICES: Final = (
    Choice(
        "turn_choice",
        (
            Branch("queue_scan.10", "awaiting_readmit", "waiter_queued"),
            Branch("queue_scan.11", "done"),
        ),
    ),
)

#: The configuration the instance rests in between scans.
IDLE: Final = frozenset({"queue_scan", "idle"})

QUEUE_SCAN: Final = Statechart(
    name="queue_scan",
    root=ROOT,
    rows=ROWS,
    choices=CHOICES,
    guards=GUARDS,
    actions=ACTIONS,
    clock="logical",
)
