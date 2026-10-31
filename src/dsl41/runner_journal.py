"""The engine's record over its store: the record kinds, the writer, the wire projection, the
open table, the snapshot, and replay (DL-424; spec-wiring 1b).

The store (`runner_store`) is the engine's only durable record. `Journal` is the one writer over
it: every record it appends is in a kind below, and every append is one store transaction.

Record kinds (`Record.kind`) and what each payload holds:

- `input`: one admitted attempt (concurrency-model ss4 step 4): `index`, `at`, `request_id`,
  `fingerprint`, and, for an input with a verb, `kind`, `payload` and `source`; `expect` and
  `claimed_actor` ride where the input was externally requested. A verbless input is a time
  observation (DL-44): replay reproduces the timers it fired.
- `decision`: the verdict that attempt got (step 7), with its `fingerprint` and the revisions it
  moved. The header's `request_id` is the store's unique column, so an exact retry finds it.
  An attempt and its decision are appended in one transaction, so replay and a retry never see
  half an admission.
- `dispatch`: one attempt's launch intent before the spawn: `job`, `run_number`, `attempt` (the
  `run_id` the wrapper spec carries, DL-118), `ordinal`, `spool`, `env` (the invocation, crash
  spec) and `started_at`. `discharges` names a `dispatch_pending` it ends.
- `dispatch_pending`: a start decision the engine did not dispatch (`event: pending`), and its
  discharge (`event: discharged`).
- `outcome`: an attempt's end and how it was observed: `natural`, `spawn_failed`, `stopped` or
  `unobservable`, with the STATUS fields the adapter's result maps to; `ladder: running` while
  the cleanup of its group is not done.
- `follow_up`: the plan the interruption rule chose for one outcome (`event: plan`), appended
  with the outcome; each step's done mark (`event: done`), appended with the step's effect; and
  `event: ladder_done` once the group of an outcome with `ladder: running` is gone.
- `drop`: a scheduler tick missed across downtime (E9).
- `preflight`: the ss8 WARN items the engine started under.
- `config`: a configuration rev the engine applied (`event: applied`), with, for an update,
  the plan's token and what it added, changed and removed; and an update's `event: intent`
  while it stops: the token, the stop set with each attempt it stops, the dispatch filter,
  and the request's id (the store's unique column) and fingerprint (spec-wiring 1b).
- `engine`: the engine's own life: `started`, `draining`, `clean_shutdown`, `unclean_shutdown`,
  `shutdown_during_startup`, `drain_interrupted`, `snapshot`.

The subscribe stream sends the records of docs/control-protocol.md ss5 in the shapes it fixes
(`wire_record`): `segment` from `config`, `input` and `advance` from `input`, and `decision`,
`dispatch`, `drop` and `preflight` from their own kinds. The other kinds are not on the wire.
"""

from __future__ import annotations

import asyncio
import re
import sys

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from dsl41.canon import CanonError, canonical_bytes, decode, is_wire_int, naive_utc
from dsl41.engine_charts import UPDATE_STOP
from dsl41.oracle import Oracle
from dsl41.oracle_state import CarriedRows, Event
from dsl41.runner_admission import (
    ApplyResult,
    Attempt,
    DecisionIndex,
    Frontiers,
    apply_attempt,
    describe_input,
    dispatch_plan,
    spawn_superseded,
)
from dsl41.runner_clock import EngineError
from dsl41.runner_codes import STORED_CODES
from dsl41.runner_config import Configuration, oracle_reading
from dsl41.runner_ledger import STATE_MACHINE_VERSION, Proof
from dsl41.runner_store import Record, SqliteStore, Store, StoreError
from dsl41.state_machine import (
    Context,
    Effects,
    Exclusion,
    Executor,
    Outcome,
    Region,
    Row,
    State,
    Statechart,
    StatechartError,
)
from dsl41.state_machine import Event as ChartEvent

if TYPE_CHECKING:  # annotation only
    from dsl41.runner_preflight import PreflightItem


#: period-model ss11a pins `run_id` to a filename-safe grammar: the canonical uuid4 string form.
#: Every writer and acceptor of a bound run_id checks it, because a freehand string would pass
#: presence checks and split the one key DL-118 keeps whole.
RUN_ID_RE: Final = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def is_valid_run_id(run_id: str) -> bool:
    return RUN_ID_RE.fullmatch(run_id) is not None


#: The store file under a run root.
STORE_NAME: Final[str] = "engine.db"

#: The record kinds this binary writes and reads (module docstring).
KINDS: Final[frozenset[str]] = frozenset(
    {
        "input",
        "decision",
        "dispatch",
        "dispatch_pending",
        "outcome",
        "follow_up",
        "drop",
        "preflight",
        "config",
        "engine",
    }
)

#: The `engine` record's events.
ENGINE_EVENTS: Final[frozenset[str]] = frozenset(
    {
        "started",
        "draining",
        "clean_shutdown",
        "unclean_shutdown",
        "shutdown_during_startup",
        "drain_interrupted",
        "snapshot",
    }
)

#: The `config` record's events: an update's `intent` while it stops, and an applied rev.
CONFIG_EVENTS: Final[frozenset[str]] = frozenset({"intent", "applied"})

#: The keys of the wire's `segment` record (control-protocol ss5), which an `applied` record
#: carries beside the application it describes.
SEGMENT_KEYS: Final = (
    "rev",
    "at",
    "catalog_hash",
    "runtime_hash",
    "state_machine_version",
    "baseline_id",
    "clock_domain",
)

#: How an attempt ended, as its `outcome` record says.
OUTCOMES: Final[frozenset[str]] = frozenset({"natural", "spawn_failed", "stopped", "unobservable"})

#: The plans the interruption rule chooses (spec-wiring 2b).
PLANS: Final[frozenset[str]] = frozenset({"complete", "hold", "rerun", "none"})


def check_record(record: Record) -> None:
    """Refuse, by name, a record this binary does not know: a kind outside `KINDS`, an engine
    event outside `ENGINE_EVENTS`, an outcome outside `OUTCOMES`, a plan outside `PLANS`
    (protocol-evolution: an unknown kind refuses by name). The writer checks before the append
    and every reader before it acts on the record."""
    kind, body = record.kind, record.payload
    if kind not in KINDS:
        raise EngineError(f"record kind {kind!r}: this binary knows {sorted(KINDS)}")
    if kind == "engine" and body.get("event") not in ENGINE_EVENTS:
        raise EngineError(
            f"engine record {body.get('event')!r}: this binary knows {sorted(ENGINE_EVENTS)}"
        )
    if kind == "config" and body.get("event") not in CONFIG_EVENTS:
        raise EngineError(
            f"config record {body.get('event')!r}: this binary knows {sorted(CONFIG_EVENTS)}"
        )
    if kind == "outcome" and body.get("outcome") not in OUTCOMES:
        raise EngineError(f"outcome {body.get('outcome')!r}: this binary knows {sorted(OUTCOMES)}")
    if kind == "follow_up" and body.get("event") == "plan" and body.get("plan") not in PLANS:
        raise EngineError(f"follow-up plan {body.get('plan')!r}: this binary knows {sorted(PLANS)}")


# ---- one `subscribe` feed as a statechart (control-protocol ss5, DL-267)
#
# `SUBSCRIPTION` is the declaration (docs/statechart-engine.md). The engine owns the one
# `Executor`; the control server hands it to `Journal.subscribe`, and the instance starts there,
# in `backfill`, as the feed joins the fan-out. Its key and data are the `Subscription`, which
# keeps the tier's record: the queue, the backlog bytes, the budget and the overflow reason.
#
# The posters are the journal's fan-out (`append`, from `_publish`) and the subscribe handler
# (`backfill_sent` after a backfill without refusal, and `stream_ends` from its `finally`,
# through `Journal.unsubscribe`). Rows `subscription.02` and `.04` have no guard and no action;
# their notes say when the handler posts. The buffer is bounded in both phases: `append` reads
# the same rows in `backfill` and `live`, and nothing is deferred.
#
# `removed` and `closed` are final, and the tier forgets the instance in the call that made it
# final, so nothing posts to it there; a final state ignores every event (statechart-engine
# ss10, DL-380). A gap in the chart is a `StatechartError`, which stops the engine with exit 7
# (statechart-engine ss5).

#: A record was appended and fanned out. Its payload is an `Offer`.
APPEND: Final = "append"
#: The handler sent the backfill without a refusal and now reads the live queue.
BACKFILL_SENT: Final = "backfill_sent"
#: The feed's stream ended while the feed was in the fan-out.
STREAM_ENDS: Final = "stream_ends"
#: The intent of an overflow: the journal takes the feed out of its fan-out,
#: and tells the owner once every other feed has the record (DL-267).
LEAVE_FAN_OUT: Final = "leave the fan-out, then tell the owner"

#: Where a feed can be: the leaf states of `SUBSCRIPTION`.
FEED_STATES: Final = frozenset({"backfill", "live", "removed", "closed"})
_FEED_FINALS: Final = frozenset({"removed", "closed"})


@dataclass(frozen=True)
class Offer:
    """One appended record for a feed, and the size of its stream line in bytes."""

    record: dict[str, Any]
    size: int


# -- the guards. Each docstring is its rows' prose.


def _fits(ctx: Context) -> bool:
    """The record fits: the backlog is empty, or the backlog plus the record
    stays within the budget. An empty backlog takes one record of any size, so
    one record larger than the budget delays the feed and does not remove it
    (DL-267)."""
    feed: Subscription = ctx.data
    offer: Offer = ctx.event.payload
    return not feed.backlog_bytes or feed.backlog_bytes + offer.size <= feed.budget


def _overflows(ctx: Context) -> bool:
    """The record does not fit: the backlog holds a record, and the backlog
    plus this one would pass the budget (DL-267)."""
    return not _fits(ctx)


# -- the actions


def _enqueue(ctx: Context) -> None:
    """Queue the record and count its bytes against the budget. Memory only:
    the queue has no count limit, so this never blocks."""
    feed: Subscription = ctx.data
    offer: Offer = ctx.event.payload
    feed._queue.put_nowait((offer.record, offer.size))
    feed.backlog_bytes += offer.size


def _drop_backlog(ctx: Context) -> Effects:
    """Record why, and drop the backlog. The journal then takes the feed out
    of its fan-out and, once every other feed has the record, tells the owner,
    which ends the stream (DL-267)."""
    feed: Subscription = ctx.data
    offer: Offer = ctx.event.payload
    feed.overflow = (
        f"subscriber backlog of {feed.backlog_bytes} bytes cannot take a"
        f" {offer.size}-byte record within the {feed.budget}-byte budget"
    )
    while not feed._queue.empty():
        feed._queue.get_nowait()
    feed.backlog_bytes = 0
    return Effects(intents=(LEAVE_FAN_OUT,))


#: One `subscribe` feed (control-protocol ss5), one instance per subscription.
SUBSCRIPTION: Final = Statechart(
    name="subscription",
    root=State(
        "feed",
        regions=(
            Region(
                "phase",
                "open",
                (
                    State(
                        "open",
                        regions=(Region("phase", "backfill", (State("backfill"), State("live"))),),
                    ),
                    State("removed", final=True),
                    State("closed", final=True),
                ),
            ),
        ),
    ),
    rows=(
        Row(
            "subscription.02",
            "backfill",
            BACKFILL_SENT,
            "live",
            cite="control-protocol ss5, DL-45, DL-135",
            note=(
                "The handler posts it once the backfill went out without a refusal;"
                " with no `since` there is nothing to send. The handler then sends"
                " the queued live records past the seam."
            ),
        ),
        Row(
            "subscription.03",
            "open",
            APPEND,
            "removed",
            guard="overflows",
            action="drop_backlog",
            cite="control-protocol ss5, DL-267",
        ),
        Row(
            "subscription.04",
            "open",
            STREAM_ENDS,
            "closed",
            cite="control-protocol ss5, PR-03, period-model ss11",
            note=(
                "The client is gone, a refusal went on the stream, the lineage was"
                " lost, or the handler was cancelled. The journal has already taken"
                " the feed out of its fan-out."
            ),
        ),
        Row(
            "subscription.05",
            "open",
            APPEND,
            guard="fits",
            action="enqueue",
            kind="internal",
            cite="control-protocol ss5, DL-267",
        ),
    ),
    guards={"fits": _fits, "overflows": _overflows},
    actions={"enqueue": _enqueue, "drop_backlog": _drop_backlog},
    clock="logical",
    exclusions=(
        Exclusion(
            "live",
            BACKFILL_SENT,
            "the handler posts backfill_sent once, after the ack and the backfill, with no"
            " await between its last send and the post; one out of order raises"
            " UnhandledEvent, which the chart's own test pins",
        ),
    ),
)


class Subscription:
    """One live feed of ss10 `subscribe`: the records appended since it began, in append order,
    under a byte budget (control-protocol ss5, DL-267). The owner sets the budget; the journal
    enforces it.

    It is the tier's record of one `SUBSCRIPTION` instance, and the instance's key. `offer`,
    `go_live` and `close` post the chart's events and the rows decide: a record enters an empty
    backlog, otherwise only if the backlog plus the record stays within the budget, so the
    backlog never holds more than the budget or one record, whichever is larger. A refused
    record removes the feed: the journal drops the backlog, then tells the owner why. The append
    it came from has already succeeded, and only a `StatechartError` can raise into it.

    `state` is the instance's active leaf; once the instance is final the tier forgets it and
    `state` keeps the final state it rested in."""

    def __init__(
        self, machines: Executor, budget: int, on_overflow: Callable[[str], None] | None
    ) -> None:
        self.budget = budget
        self.backlog_bytes = 0
        #: why the journal removed this feed, or None while it is live
        self.overflow: str | None = None
        self._on_overflow = on_overflow
        self._queue: asyncio.Queue[tuple[dict[str, Any], int]] = asyncio.Queue()
        #: the engine's executor, which holds this feed's instance
        self._machines = machines
        #: the final state the instance rested in, once it is forgotten
        self._rested: str | None = None
        machines.start(self, SUBSCRIPTION, self)
        machines.run()

    @property
    def state(self) -> str:
        if self._rested is not None:
            return self._rested
        (leaf,) = self._machines.active(self) & FEED_STATES
        return leaf

    def _post(self, name: str, payload: object = None) -> Outcome:
        """Post one event and answer its microstep. An instance that the row
        made final is forgotten at once."""
        (outcome,) = self._machines.dispatch(self, ChartEvent(name, payload))
        active = self._machines.active(self)
        if active & _FEED_FINALS:
            (self._rested,) = active & _FEED_FINALS
            self._machines.forget(self)
        return outcome

    def go_live(self) -> None:
        """The backfill is sent: the handler now reads the live queue."""
        self._post(BACKFILL_SENT)

    def close(self) -> None:
        """The stream ended and the feed left the fan-out."""
        self._post(STREAM_ENDS)

    def offer(self, record: dict[str, Any], size: int) -> bool:
        """Offer one record of `size` encoded bytes: True when the feed took it, False when the
        record removed the feed. Never blocks and never raises QueueFull; the rows check the
        budget."""
        return LEAVE_FAN_OUT not in self._post(APPEND, Offer(record, size)).intents

    def tell_owner(self) -> None:
        """Hand an overflowed feed's reason to its owner. Best effort: the append behind it is
        durable and the other feeds have their record, so an owner that raises, or a stderr that
        cannot take the report, changes nothing. A `StatechartError` is not contained: it stops
        the engine (statechart-engine ss5)."""
        if self._on_overflow is None or self.overflow is None:
            return
        try:
            self._on_overflow(self.overflow)
        except StatechartError:
            raise
        except Exception as exc:  # noqa: BLE001 -- see the docstring
            best_effort_report(f"dsl41: subscriber overflow handler failed: {exc!r}")

    async def get(self) -> dict[str, Any]:
        record, size = await self._queue.get()
        self.backlog_bytes -= size
        return record

    def get_nowait(self) -> dict[str, Any]:
        record, size = self._queue.get_nowait()
        self.backlog_bytes -= size
        return record

    def empty(self) -> bool:
        return self._queue.empty()


def best_effort_report(line: str) -> None:
    """One line on stderr, or nothing. For reports made after a durable
    append, where a closed or broken stderr must not become the append's
    failure (control-protocol ss5, DL-267)."""
    try:
        print(line, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001, S110 -- a report that cannot be made is dropped
        pass


# ---------------------------------------------------------------- the wire


def wire_record(record: Record) -> dict[str, Any] | None:
    """`record` in the shape control-protocol ss5 sends it, or None for a kind the wire does not
    carry. The projection is the one place a store kind meets a wire name (module docstring).
    A record of a kind this binary does not know is refused by name (`check_record`)."""
    check_record(record)
    body = dict(record.payload)
    kind = record.kind
    if kind == "input":
        wire: dict[str, Any] = {
            "rec": "input" if body.get("kind") is not None else "advance",
            "seq": body["index"],
        }
        wire |= {key: value for key, value in body.items() if key != "index"}
        return wire
    if kind == "decision":
        return {
            "rec": "decision",
            "index": body["index"],
            "request_id": body["request_id"],
            "decision": body["decision"],
            "reason": body.get("reason"),
            "code": body.get("code"),
            "revisions": body.get("revisions") or {},
            "legacy_batch": False,
        }
    if kind == "dispatch":
        wire = {
            "rec": "dispatch",
            "job": body["job"],
            "run_number": body["run_number"],
            "run_id": body["attempt"],
            "run_dir": body.get("spool"),
            "started_at": body["started_at"],
            "rev": body["rev"],
        }
        if "after" in body:
            wire["after"] = body["after"]  # a replacement attempt's (DL-426)
        return wire
    if kind in ("drop", "preflight"):
        return {"rec": kind, **body}
    if kind == "config" and body.get("event") == "applied":
        # the segment's own keys: an update's application details stay in the store
        return {"rec": "segment", **{key: body[key] for key in SEGMENT_KEYS if key in body}}
    return None


def _wire_size(wire: Mapping[str, Any]) -> int:
    """The bytes of `wire` as one stream line, as the control server encodes it."""
    import json

    return len(json.dumps(wire, sort_keys=True).encode("utf-8")) + 1


# ---------------------------------------------------------------- the open table


@dataclass(frozen=True)
class OpenAttempt:
    """A dispatched attempt the store has not closed: no `outcome` yet, or one committed while
    the cleanup of its group was not done (`closing`)."""

    attempt: str
    job: str
    run_number: int
    ordinal: int
    spool: str | None
    seq: int
    started_at: str
    env: dict[str, str] = field(default_factory=dict)
    #: the cause of the stop this attempt replaced, None for a run's first attempt
    after: str | None = None
    #: the cause of the stop this attempt was taking when the engine left it unsettled
    #: (`engine_stop`, `update_stop`, `oracle_kill`), from the drain's and the shutdown's
    #: records; the restart adopts the attempt with it (DL-426)
    stop: str | None = None
    #: its outcome is committed while its ladder still runs: it leaves the table only with the
    #: ladder's end (`follow_up` `ladder_done`), and a restart finishes the ladder first
    closing: bool = False
    #: a closing attempt's committed outcome (`natural`, `stopped`, ...): a `natural` one's
    #: ladder is a leak's, which `process-leak=leak` leaves alone; None in a snapshot older
    #: than this field, read as a stop's
    outcome: str | None = None


@dataclass(frozen=True)
class PendingDispatch:
    """A start decision with no `dispatch` yet: the run it starts and its invocation instant."""

    seq: int
    job: str
    run_number: int
    scheduled_at: str


@dataclass
class OpenPlan:
    """A follow-up plan with a step not yet done. `done` maps a step's position to the seq of
    the record that discharged it."""

    seq: int
    attempt: str
    job: str
    run_number: int
    plan: str
    steps: list[dict[str, Any]]
    done: dict[int, int | None] = field(default_factory=dict)
    #: the end its `outcome` record holds (the wrapper's, or the completion's instant), which a
    #: restart stamps the plan's steps with; None when the outcome holds none
    ended: str | None = None

    def next_step(self) -> int | None:
        """The first step not yet done, or None."""
        return next((i for i in range(len(self.steps)) if i not in self.done), None)


class OpenTable:
    """What the store's records leave open: attempts with no outcome, pending dispatches, and
    plans with an unfinished step. It is a fold over the records in seq order, so the snapshot
    carries it and startup rebuilds it from the records after the snapshot (spec-wiring 1b)."""

    def __init__(self) -> None:
        self.attempts: dict[str, OpenAttempt] = {}
        self.pending: dict[int, PendingDispatch] = {}
        self.plans: dict[str, OpenPlan] = {}
        #: the update in STOPPING: its `config intent` record's body with the record's seq, until
        #: a `config applied` names its token (startup step 4); None when no update stops
        self.update: dict[str, Any] | None = None
        #: the end of the outcome just noted, which the plan committed with it carries
        self._ended: dict[str, str | None] = {}

    def note(self, seq: int, record: Record) -> None:
        check_record(record)
        body = record.payload
        if record.kind == "dispatch":
            attempt = OpenAttempt(
                attempt=str(body["attempt"]),
                job=str(body["job"]),
                run_number=int(body["run_number"]),
                ordinal=int(body.get("ordinal", 1)),
                spool=body.get("spool"),
                seq=seq,
                started_at=str(body["started_at"]),
                env=dict(body.get("env") or {}),
                after=body.get("after"),
            )
            self.attempts[attempt.attempt] = attempt
            if is_wire_int(body.get("discharges")):
                self.pending.pop(body["discharges"], None)
        elif record.kind == "outcome":
            attempt_id = str(body["attempt"])
            held = self.attempts.get(attempt_id)
            if body.get("ladder") == "running" and held is not None:
                self.attempts[attempt_id] = replace(
                    held, closing=True, outcome=str(body.get("outcome"))
                )
            else:
                self.attempts.pop(attempt_id, None)
            self._ended = {attempt_id: outcome_end(body)}
        elif record.kind == "engine" and body.get("event") in _STOP_RECORDS:
            self._note_stops(body)
        elif record.kind == "dispatch_pending":
            if body.get("event") == "pending":
                self.pending[seq] = PendingDispatch(
                    seq, str(body["job"]), int(body["run_number"]), str(body["scheduled_at"])
                )
            else:
                self.pending.pop(int(body["pending"]), None)
        elif record.kind == "follow_up":
            self._note_follow_up(seq, body)
        elif record.kind == "config":
            self._note_config(seq, body)

    def _note_config(self, seq: int, body: Mapping[str, Any]) -> None:
        """An update's intent opens it and gives each attempt it stops the cause of its first
        stop, as the intent names it (`update_stop` unless the attempt took another first), to
        each one with no stop of its own yet; the application that names its token closes it."""
        if body.get("event") == "intent":
            self.update = {**body, "seq": seq}
            for entry in body.get("stop") or ():
                held = self.attempts.get(str(entry.get("attempt")))
                if held is not None and held.stop is None:
                    cause = str(entry.get("cause") or UPDATE_STOP)
                    self.attempts[held.attempt] = replace(held, stop=cause)
        elif self.update is not None and body.get("token") == self.update.get("token"):
            self.update = None

    def _note_stops(self, body: Mapping[str, Any]) -> None:
        """The stop each unsettled attempt was taking. The first stop an attempt took is its
        cause: the `draining` record fills only an attempt with none, its own `stops` naming the
        causes it already knew and `engine_stop` the rest; a shutdown record's `stops`, the
        engine's last word, replaces whatever is set (DL-426)."""
        stops = {str(a): c for a, c in dict(body.get("stops") or {}).items() if c is not None}
        draining = body.get("event") == "draining"
        if draining:
            unsettled = [str(a) for a in body.get("unsettled") or ()]
            stops = {a: stops.get(a, "engine_stop") for a in unsettled}
        for run_id, cause in stops.items():
            held = self.attempts.get(run_id)
            if held is not None and (held.stop is None or not draining):
                self.attempts[run_id] = replace(held, stop=str(cause))

    def _note_follow_up(self, seq: int, body: Mapping[str, Any]) -> None:
        attempt = str(body["attempt"])
        if body.get("event") == "ladder_done":  # written only for a closing attempt
            self.attempts.pop(attempt, None)
            return
        if body.get("event") == "plan":
            steps = [dict(step) for step in body.get("steps") or ()]
            if steps:
                self.plans[attempt] = OpenPlan(
                    seq,
                    attempt,
                    str(body["job"]),
                    int(body["run_number"]),
                    str(body["plan"]),
                    steps,
                    ended=self._ended.get(attempt),
                )
            return
        plan = self.plans.get(attempt)
        if plan is None:
            return
        plan.done[int(body["step"])] = body["seq"]
        if plan.next_step() is None:
            del self.plans[attempt]

    def floor(self) -> int | None:
        """The oldest seq a record the table refers to holds, or None when nothing is open. The
        pruner never reaches below it."""
        seqs = [a.seq for a in self.attempts.values()]
        seqs += list(self.pending)
        seqs += [p.seq for p in self.plans.values()]
        if self.update is not None:
            seqs.append(int(self.update["seq"]))
        return min(seqs, default=None)

    def to_payload(self) -> dict[str, Any]:
        """The table as the snapshot holds it: three lists, each in seq order."""
        return {
            "attempts": [
                {
                    "attempt": a.attempt,
                    "job": a.job,
                    "run_number": a.run_number,
                    "ordinal": a.ordinal,
                    "spool": a.spool,
                    "seq": a.seq,
                    "started_at": a.started_at,
                    "env": dict(a.env),
                    "after": a.after,
                    "stop": a.stop,
                    "closing": a.closing,
                    "outcome": a.outcome,
                }
                for a in sorted(self.attempts.values(), key=lambda a: a.seq)
            ],
            "pending": [
                {
                    "seq": p.seq,
                    "job": p.job,
                    "run_number": p.run_number,
                    "scheduled_at": p.scheduled_at,
                }
                for p in sorted(self.pending.values(), key=lambda p: p.seq)
            ],
            "plans": [
                {
                    "seq": p.seq,
                    "attempt": p.attempt,
                    "job": p.job,
                    "run_number": p.run_number,
                    "plan": p.plan,
                    "steps": [dict(step) for step in p.steps],
                    "done": [[step, seq] for step, seq in sorted(p.done.items())],
                    "ended": p.ended,
                }
                for p in sorted(self.plans.values(), key=lambda p: p.seq)
            ],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> OpenTable:
        table = cls()
        for a in payload.get("attempts") or ():
            table.attempts[a["attempt"]] = OpenAttempt(
                attempt=a["attempt"],
                job=a["job"],
                run_number=a["run_number"],
                ordinal=a["ordinal"],
                spool=a["spool"],
                seq=a["seq"],
                started_at=a["started_at"],
                env=dict(a["env"]),
                after=a.get("after"),
                stop=a.get("stop"),
                closing=bool(a.get("closing")),
                outcome=a.get("outcome"),
            )
        for p in payload.get("pending") or ():
            table.pending[p["seq"]] = PendingDispatch(
                p["seq"], p["job"], p["run_number"], p["scheduled_at"]
            )
        for p in payload.get("plans") or ():
            table.plans[p["attempt"]] = OpenPlan(
                p["seq"],
                p["attempt"],
                p["job"],
                p["run_number"],
                p["plan"],
                [dict(step) for step in p["steps"]],
                {int(step): seq for step, seq in p["done"]},
                p.get("ended"),
            )
        return table


# ---------------------------------------------------------------- the position


#: The `engine` records that name the stops of the attempts the engine left unsettled.
_STOP_RECORDS: Final = frozenset({"draining", "clean_shutdown", "unclean_shutdown"})


def outcome_end(body: Mapping[str, Any]) -> str | None:
    """The end an `outcome` record holds, naive UTC ISO: the wrapper's recorded end, or for a
    completion with no wrapper record (an adapter's own result) the record's instant; None for
    an interruption with no record (DL-426, spec-crash step 5)."""
    ended = (body.get("status") or {}).get("ended_at")
    if isinstance(ended, str):
        return naive_utc(datetime.fromisoformat(ended)).isoformat()
    if body.get("outcome") in ("natural", "spawn_failed") and isinstance(body.get("at"), str):
        return str(body["at"])
    return None


def _instant(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


@dataclass
class Position:
    """Where the records leave time (ss6, ss7): the last instant they prove the engine reached,
    and the scheduler's durable frontier with the ticks adjudicated at it.

    `last_at` reads the configuration's opening, each input, dispatch and drop. The scheduler
    frontier is semantic, not the newest timestamp (PR-25a): the opening, the admitted scheduler
    ticks, the drops and the time observations. `ticks` are the (job, instant) pairs the records
    adjudicated at the frontier, which a restart's inclusive re-anchor must not act on again
    (DL-45, DL-166)."""

    last_at: datetime | None = None
    frontier: datetime | None = None
    ticks: set[tuple[str, str]] = field(default_factory=set)

    def note(self, record: Record) -> None:
        body = record.payload
        kind = record.kind
        stamp: datetime | None = None
        semantic = False
        if kind == "config" and body.get("event") == "applied":
            stamp, semantic = _instant(body.get("at")), True
        elif kind == "input":
            stamp = _instant(body.get("at"))
            semantic = body.get("kind") is None or body.get("source") == "scheduler"
        elif kind == "drop":
            stamp, semantic = _instant(body.get("at")), True
        elif kind == "dispatch":
            stamp = _instant(body.get("started_at"))
        if stamp is None:
            return
        if self.last_at is None or stamp > self.last_at:
            self.last_at = stamp
        if not semantic:
            return
        if self.frontier is None or stamp > self.frontier:
            self.frontier, self.ticks = stamp, set()
        if stamp == self.frontier and body.get("kind") == "STARTJOB":
            job = (body.get("payload") or {}).get("job")
            if isinstance(job, str):
                self.ticks.add((job, stamp.isoformat()))

    def to_payload(self) -> dict[str, Any]:
        return {
            "last_at": None if self.last_at is None else self.last_at.isoformat(),
            "frontier": None if self.frontier is None else self.frontier.isoformat(),
            "ticks": [list(tick) for tick in sorted(self.ticks)],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Position:
        return cls(
            last_at=_instant(payload.get("last_at")),
            frontier=_instant(payload.get("frontier")),
            ticks={(str(job), str(at)) for job, at in payload.get("ticks") or ()},
        )


# ---------------------------------------------------------------- the snapshot


def _canon_ready(value: object) -> Any:
    """ss3.2's two shape rules a pydantic dump does not apply: a set sorts by value and a tuple
    is a list. Datetimes stay datetimes; the encoder writes them."""
    if isinstance(value, Mapping):
        return {key: _canon_ready(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return [_canon_ready(item) for item in sorted(value)]
    if isinstance(value, (list, tuple)):
        return [_canon_ready(item) for item in value]
    return value


def oracle_rows(oracle: Oracle) -> dict[str, Any]:
    """The oracle's runtime state as the snapshot holds it: every row, the timers in `(due,
    token)` order, the three scalars no row holds, and what an update leaves behind it: the
    tombstones of removed jobs, the queue scan owed to the next input, and the definitions of
    the ghost resources. `Oracle(catalog, carried=...)` installs it back (`restore_oracle`)."""
    store = oracle.store
    rows = CarriedRows(
        jobs=dict(store.job),
        globals_=dict(store.globals_),
        timers=tuple(store.timers()),
        timer_seq=store.timer_seq,
        consumed=dict(store.consumed),
        enqueue_counter=store.enqueue_counter,
        tombstones=dict(store.tombstones),
        wake_owed=oracle._wake_owed,
        ghosts=oracle.ghost_resources(),
        now=oracle._now,
    )
    dumped = _canon_ready(rows.model_dump())
    assert isinstance(dumped, dict)
    return dumped


@dataclass(frozen=True)
class Snapshot:
    """What a snapshot restores (spec-wiring 1b): the oracle's rows, the log's position, the
    scheduler's position, the configuration rev, the open table and the update state."""

    seq: int
    rows: CarriedRows
    frontiers: Frontiers
    position: Position
    config_rev: int
    open: OpenTable
    update: Any = None
    #: the run history (`runner_history.run_history`): job -> the runs still open or ended
    #: inside the retention window, so a reader of a pruned store keeps their rows
    runs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: job -> the `Interrupted` evidence its hold carries until its next start (spec-crash)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)


def encode_snapshot(
    oracle: Oracle,
    *,
    frontiers: Frontiers,
    position: Position,
    config_rev: int,
    open_table: OpenTable,
    runs: Mapping[str, list[dict[str, Any]]] | None = None,
    evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> bytes:
    """The snapshot's canonical bytes. The update state is the open table's update in STOPPING
    (its `config intent`), null when none stops."""
    return canonical_bytes(
        {
            "state_version": STATE_MACHINE_VERSION,
            "oracle": oracle_rows(oracle),
            "frontiers": {
                "committed_index": frontiers.committed_index,
                "applied_index": frontiers.applied_index,
                "at": None if frontiers.at is None else frontiers.at.isoformat(),
            },
            "position": position.to_payload(),
            "config_rev": config_rev,
            "open": open_table.to_payload(),
            "update": open_table.update,
            "runs": dict(runs or {}),
            "evidence": {job: dict(held) for job, held in (evidence or {}).items()},
        }
    )


def _with_update(table: OpenTable, update: Any) -> OpenTable:
    """`table` holding the snapshot's update in STOPPING."""
    table.update = dict(update) if isinstance(update, Mapping) else None
    return table


def _with_revs(runs: dict[str, Any]) -> dict[str, Any]:
    """A snapshot's retained run history, every run with the configuration rev it opened under
    (`rev`, required: a reader never derives one), or ValueError."""
    for job, kept in runs.items():
        for run in kept:
            if not is_wire_int(run.get("rev")):
                raise ValueError(f"retained run {job}.{run.get('run')} records no rev")
    return runs


def decode_snapshot(seq: int, data: bytes, *, where: str) -> Snapshot:
    """A stored snapshot read back; one that does not read is refused naming `where`."""
    try:
        raw: Any = decode(data)
        front = raw["frontiers"]
        return Snapshot(
            seq=seq,
            rows=CarriedRows.model_validate(raw["oracle"]),
            frontiers=Frontiers(
                committed_index=front["committed_index"],
                applied_index=front["applied_index"],
                at=_instant(front["at"]),
            ),
            position=Position.from_payload(raw["position"]),
            config_rev=int(raw["config_rev"]),
            open=_with_update(OpenTable.from_payload(raw["open"]), raw.get("update")),
            update=raw.get("update"),
            runs=_with_revs(dict(raw["runs"])),
            evidence=dict(raw.get("evidence") or {}),
        )
    except (CanonError, KeyError, TypeError, ValueError) as exc:
        raise EngineError(f"{where}: unreadable snapshot at seq {seq}: {exc}") from exc


# ---------------------------------------------------------------- the writer


class Journal:
    """The one writer over an engine's store (module docstring).

    Every append re-proves the run root's lock first (S6b), when there is one: an engine that
    cannot prove it holds the root appends nothing, and a `dispatch` record precedes every
    start, so fencing appends fences dispatch. After the commit the records are folded into the
    open table and the position, and the ones the wire carries are fanned out to the live
    subscribe feeds (DL-267). `last_seq` is the seq of the newest record this writer saw
    committed: the subscribe seam."""

    def __init__(
        self,
        store: Store,
        *,
        lock: Proof | None = None,
        path: Path | None = None,
        open_table: OpenTable | None = None,
        position: Position | None = None,
    ) -> None:
        self.store = store
        #: the store file, None for a `MemoryStore`
        self.path = path
        meta = store.meta()
        #: concurrency-model ss2: this store's identity, part of every fingerprint
        self.baseline_id = meta.get("baseline_id", "")
        self.clock_domain = meta.get("clock_domain", "virtual")
        self._lock = lock
        self._subscribers: list[Subscription] = []
        #: False while a restart has not passed every refusal: nothing is recorded then, not
        #: even `shutdown_during_startup`
        self.writable = True
        self.open = open_table if open_table is not None else OpenTable()
        self.position = position if position is not None else Position()
        self.last_seq = store.last_seq()

    def prove(self) -> None:
        """Re-prove the run root's lock (S6b), when there is one. The engine asks before it
        applies an input, so an engine that lost the root changes nothing, not even in memory."""
        if self._lock is not None:
            self._lock.check()

    def append(self, *records: Record) -> int:
        """Append `records` in one transaction; answer the last seq. A record this binary does
        not know is refused before anything is written (`check_record`)."""
        for record in records:
            check_record(record)
        self.prove()
        last = self.store.append(*records)
        first = last - len(records) + 1
        self.last_seq = last
        for seq, record in enumerate(records, start=first):
            self.open.note(seq, record)
            self.position.note(record)
            wire = wire_record(record)
            if wire is not None:
                self._publish(wire, _wire_size(wire))
        return last

    def next_seq(self) -> int:
        """The seq the next appended record takes. One writer holds the store, so a done mark
        can name the seq of the effect record appended before it in one transaction."""
        return self.store.last_seq() + 1

    # ---- the records

    def admission(self, attempt: Attempt, result: ApplyResult, *also: Record) -> int:
        """One admission (concurrency-model ss4 steps 4-7) as one transaction: the attempt, its
        decision, and the records the caller appends with them (a plan step's done mark). A
        native rejection names one of the append-only `STORED_CODES` (period-model ss2.3)."""
        if result.decision == "rejected" and result.code not in STORED_CODES:
            raise EngineError(
                f"decision {result.index}: a native rejection names its stored code"
                f" at birth, and this one names {result.code!r}"
            )
        return self.append(input_record(attempt), decision_record(attempt, result), *also)

    def drop(self, ev: Event, reason: str) -> None:
        """A scheduler tick missed across downtime (E9), recorded once."""
        at = ev.at.isoformat()
        self.append(
            Record(
                ev.at,
                "drop",
                ev.source,
                None,
                {"at": at, "kind": ev.kind, "payload": dict(ev.payload), "reason": reason},
            )
        )

    def preflight(self, items: list[PreflightItem], at: datetime) -> None:
        """ss8: WARN prints, records, and runs; the record keeps the stated caveats next to the
        inputs. Replay ignores it."""
        self.append(
            Record(at, "preflight", None, None, {"items": [item.model_dump() for item in items]})
        )

    def engine(self, event: str, when: datetime, **fields: Any) -> int:
        """One `engine` lifecycle record, stamped `when`."""
        body = {"event": event, "at": when.isoformat(), **fields}
        return self.append(Record(when, "engine", None, None, body))

    # ---- subscribe fan-out

    def subscribe(
        self,
        machines: Executor,
        on_overflow: Callable[[str], None] | None = None,
        *,
        budget: int = sys.maxsize,
    ) -> Subscription:
        """A live feed of every wire record appended from now on, held to `budget` bytes (the
        control server passes ss5's). `machines` is the engine's executor, which runs the feed's
        `SUBSCRIPTION` instance. `on_overflow` is called once, synchronously inside the append
        that overflowed the feed, after the feed is removed and every other feed has the record
        (control-protocol ss5, DL-267)."""
        feed = Subscription(machines, budget, on_overflow)
        self._subscribers.append(feed)
        return feed

    def unsubscribe(self, feed: Subscription) -> None:
        """The feed's stream ended. A feed removed on overflow is not in the fan-out, posts
        nothing, and stays `removed`."""
        if feed in self._subscribers:
            self._subscribers.remove(feed)
            feed.close()

    def _publish(self, record: dict[str, Any], size: int) -> None:
        """Fan one durable record out to the live feeds. A feed it does not fit is removed, never
        grown and never left open with a hole in it (control-protocol ss5, DL-267); the removal
        is carried out once every feed has been offered the record."""
        overflowed = [feed for feed in self._subscribers if not feed.offer(record, size)]
        for feed in overflowed:
            self._subscribers.remove(feed)
        for feed in overflowed:  # only once every feed has the record
            feed.tell_owner()

    def close(self) -> None:
        """Close the store and release the lock: for an engine the two end together (S6a)."""
        self.detach()
        if self._lock is not None:
            self._lock.release()

    def detach(self) -> None:
        """Close the store, leaving the lock where it is."""
        self.store.close()


def input_record(attempt: Attempt) -> Record:
    """The `input` record of one admitted attempt. `expect` and `claimed_actor` ride only where
    they exist: an input the engine raised has neither (ss7)."""
    body: dict[str, Any] = {
        "index": attempt.index,
        "at": attempt.at.isoformat(),
        "request_id": attempt.request_id,
        "fingerprint": attempt.fingerprint,
    }
    if attempt.kind is not None:
        body |= {"kind": attempt.kind, "payload": attempt.payload, "source": attempt.source}
    if attempt.expect is not None:
        body["expect"] = attempt.expect
    if attempt.claimed_actor is not None:
        body["claimed_actor"] = attempt.claimed_actor
    return Record(attempt.at, "input", attempt.source, None, body)


def decision_record(attempt: Attempt, result: ApplyResult) -> Record:
    """The `decision` record of one attempt, keyed by its request id."""
    return Record(
        attempt.at,
        "decision",
        attempt.source,
        result.request_id,
        {
            "index": result.index,
            "request_id": result.request_id,
            "fingerprint": attempt.fingerprint,
            "decision": result.decision,
            "reason": result.reason,
            "code": result.code,
            "revisions": result.revisions,
        },
    )


def dispatch_record(
    job: str,
    run_number: int,
    *,
    attempt: str,
    spool: str | None,
    started_at: datetime,
    rev: int,
    ordinal: int = 1,
    env: Mapping[str, str] | None = None,
    discharges: int | None = None,
    after: str | None = None,
) -> Record:
    """One attempt's launch intent (DL-118, DL-425). An `attempt` outside the ss11a grammar is
    refused, as a freehand id would split the one key. `attempt` is the run id, the attempt's
    durable identity; `ordinal` names its spool directory and its display. `rev` is the
    configuration rev it is dispatched under, the attempt's own (DL-427). `after` is the cause of
    the stop a replacement follows (DL-426)."""
    if not is_valid_run_id(attempt):
        raise EngineError(
            f"dispatch {job}.{run_number}: run_id {attempt!r} is outside the ss11a uuid4"
            " grammar (DL-118)"
        )
    body: dict[str, Any] = {
        "job": job,
        "run_number": run_number,
        "attempt": attempt,
        "ordinal": ordinal,
        "spool": spool,
        "env": dict(env or {}),
        "started_at": started_at.isoformat(),
        "rev": rev,
    }
    if discharges is not None:
        body["discharges"] = discharges
    if after is not None:
        body["after"] = after
    return Record(started_at, "dispatch", None, None, body)


def pending_record(job: str, run_number: int, scheduled_at: datetime, at: datetime) -> Record:
    """A start decision the engine owes and has not dispatched (`dispatch_pending`): the run it
    starts and the instant its invocation names."""
    return Record(
        at,
        "dispatch_pending",
        None,
        None,
        {
            "event": "pending",
            "job": job,
            "run_number": run_number,
            "scheduled_at": scheduled_at.isoformat(),
        },
    )


def ladder_done_record(attempt: str, at: datetime) -> Record:
    """An attempt whose outcome was committed while its ladder ran: the ladder has ended and the
    group is gone, so the attempt leaves the open table (DL-426)."""
    return Record(at, "follow_up", None, None, {"event": "ladder_done", "attempt": attempt})


def follow_up_done(attempt: str, step: int, seq: int | None, at: datetime) -> Record:
    """One plan step's done mark: `seq` is the record that discharged it, None for a step whose
    effect the run does not need (the oracle moved past the run first)."""
    return Record(
        at, "follow_up", None, None, {"event": "done", "attempt": attempt, "step": step, "seq": seq}
    )


# ---------------------------------------------------------------- readers


def store_path(path: Path | str) -> Path:
    """The store file a reader names: a run root or its `engine.db`."""
    path = Path(path)
    return path if path.name == STORE_NAME else path / STORE_NAME


def open_reader(path: Path | str) -> SqliteStore:
    """A read-only connection to a run root's store. A root with no store is refused, and so is
    a 1.x root, by name: 2.0.0 reads `engine.db` only."""
    file = store_path(path)
    if not file.exists():
        root = file.parent
        if (root / "journal.jsonl").exists() or (root / "wal").is_dir():
            raise EngineError(
                f"{root}: a 1.x run root (journal.jsonl or wal/): 2.0.0 reads {STORE_NAME}"
                " only, and a 1.x root cannot be resumed or read"
            )
        raise EngineError(f"{root}: no {STORE_NAME} here: not a run root")
    try:
        return SqliteStore(file, read_only=True)
    except (StoreError, OSError) as exc:
        raise EngineError(f"{file}: {exc}") from exc
    except Exception as exc:  # sqlite3.Error is not an OSError
        raise EngineError(f"{file}: {exc}") from exc


def read_records(path: Path | str, after: int = 0) -> list[tuple[int, Record]]:
    """Every record of a run root's store after `after`, in seq order."""
    store = open_reader(path)
    try:
        return list(store.read_from(after))
    finally:
        store.close()


def read_journal(path: Path | Store) -> list[dict[str, Any]]:
    """The wire projection of every retained record (`wire_record`), in seq order: what a
    subscriber with `since: 0` receives. `path` is a run root, its store file, or a store."""
    if isinstance(path, (str, Path)):
        records: Iterable[tuple[int, Record]] = read_records(path)
    else:
        records = list(path.read_from(0))
    return [wire for _, record in records if (wire := wire_record(record)) is not None]


@dataclass(frozen=True)
class Backfill:
    """What one resuming subscriber is owed (DL-135): the lowest index this store can still
    serve when the cursor is below it, and every retained wire record after the cursor."""

    gap_from: int | None
    records: list[dict[str, Any]]


class BackfillRefused(EngineError):
    """A refusal on the backfill read; the control code is `backfill_refused` (control-protocol
    ss2)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="backfill_refused")


def read_backfill(store: Store, *, since: int, upto: int) -> Backfill:
    """The wire records a subscriber resuming at `since` is owed, up to the store seq `upto` (the
    seam the caller captured as it joined the fan-out). The cut is positional: everything after
    the last input whose index is at or below `since`.

    A cursor below the earliest index the store can serve gets the gap marker
    (control-protocol ss5): `earliest_retained` is the first retained input's index, and the gap
    holds when `max(since, 0) < earliest - 1`. A pruned store that retains no input yet serves
    from the index after the last one its latest snapshot covers, so a subscriber hears of the
    pruned inputs even then."""
    try:
        everything = list(store.read_from(0))
        retained = [(seq, record) for seq, record in everything if seq <= upto]
        wires = [(record, wire_record(record)) for _, record in retained]
        earliest = next(
            (int(record.payload["index"]) for _, record in everything if record.kind == "input"),
            None,
        )
        pruned = bool(everything) and everything[0][0] > 1
        if pruned and earliest is None:
            earliest = _index_after_snapshot(store)
    except (StoreError, CanonError, EngineError) as exc:
        raise BackfillRefused(f"the store cannot be read: {exc}") from exc
    gap = pruned and earliest is not None and max(since, 0) < earliest - 1
    cut = 0
    for position, (record, _) in enumerate(wires):
        if record.kind == "input" and int(record.payload["index"]) <= since:
            cut = position + 1
    return Backfill(
        gap_from=earliest if gap else None,
        records=[wire for _, wire in wires[cut:] if wire is not None],
    )


def _index_after_snapshot(store: Store) -> int:
    """The index the next input takes, read from the latest snapshot: on a pruned store with no
    input retained, every input is at or below the snapshot's committed index. A store pruned
    with no snapshot breaks the pruner's rule and is refused."""
    stored = store.latest_snapshot()
    if stored is None:
        raise EngineError("the store's head is pruned and it holds no snapshot")
    snapshot = decode_snapshot(stored.seq, stored.data, where="the backfill")
    return snapshot.frontiers.committed_index + 1


# ---------------------------------------------------------------- replay


def read_attempts(records: list[dict[str, Any]]) -> list[Attempt]:
    """The admitted inputs of a wire projection, in admission order."""
    attempts = [a for record in records if (a := _attempt_of(record)) is not None]
    return sorted(attempts, key=lambda a: a.index)


def _attempt_of(record: Mapping[str, Any]) -> Attempt | None:
    """The attempt one wire record admitted, or None for a record that is not an attempt."""
    rec = record.get("rec")
    if rec not in ("input", "advance"):
        return None
    return Attempt(
        index=int(record["seq"]),
        at=datetime.fromisoformat(record["at"]),
        request_id=str(record["request_id"]),
        fingerprint=str(record["fingerprint"]),
        kind=record.get("kind") if rec == "input" else None,
        payload=dict(record.get("payload") or {}),
        source=record.get("source"),
        expect=record.get("expect"),
        claimed_actor=record.get("claimed_actor"),
    )


def _result_of(record: Mapping[str, Any]) -> ApplyResult:
    return ApplyResult(
        index=int(record["index"]),
        request_id=str(record["request_id"]),
        decision=record["decision"],
        reason=record.get("reason"),
        code=record.get("code"),
        revisions=record.get("revisions") or {},
    )


def read_decisions(
    records: list[dict[str, Any]], machines: Executor | None = None
) -> DecisionIndex:
    """The durable decisions, indexed, in store order: an attempt opens its request id's
    `ADMISSION` instance and its decision closes it. `machines` is the executor the index runs
    on: an engine's, or the index's own.

    A `StatechartError` stops the read as itself, its report naming the input (statechart-engine
    ss5)."""
    index = DecisionIndex(machines)
    for record in records:
        attempt = _attempt_of(record)
        if attempt is not None:
            try:
                index.note(attempt)
            except StatechartError as exc:
                raise _at_input(exc, attempt)
        elif record.get("rec") == "decision":
            result = _result_of(record)
            try:
                index.record(result)
            except StatechartError as exc:
                exc.facts.append(("input", f"{result.index}: the {result.decision} decision"))
                raise
    return index


def dispatched_runs(records: list[dict[str, Any]]) -> dict[tuple[str, int], str]:
    """`(job, run_number) -> run_id` of each run's latest dispatched attempt (DL-118): the
    identity that run's spool evidence must name. A `run_id` outside the ss11a grammar is
    refused: the reader keeps the key whole as the writer does."""
    runs: dict[tuple[str, int], str] = {}
    for record in records:
        if record.get("rec") != "dispatch":
            continue
        job, run_number, run_id = str(record["job"]), int(record["run_number"]), record["run_id"]
        if not isinstance(run_id, str) or not is_valid_run_id(run_id):
            raise EngineError(
                f"dispatch {job}.{run_number}: run_id {run_id!r} is outside the ss11a uuid4"
                " grammar (DL-118)"
            )
        runs[(job, run_number)] = run_id
    return runs


class ReplayFault(EngineError):
    """A recorded input raised when this build replayed it (period-model ss11). Replay stops
    there, naming the input, and so does every restart: the same build raises at the same
    input. The cause's `code`, if it has one, rides on; the cause is chained."""

    index: int

    def __init__(self, attempt: Attempt, cause: Exception) -> None:
        what = attempt.kind if attempt.kind is not None else "time observation"
        super().__init__(
            f"replay stopped at input {attempt.index} ({what} from"
            f" {attempt.source or 'no source'} at {attempt.at.isoformat()},"
            f" request_id {attempt.request_id}): {type(cause).__name__}: {cause}",
            code=cause.code if isinstance(cause, EngineError) else None,
        )
        self.index = attempt.index


@dataclass
class Replay:
    """Where the records left the state machine (concurrency-model ss2/ss4)."""

    frontiers: Frontiers = field(default_factory=Frontiers)
    decisions: DecisionIndex = field(default_factory=DecisionIndex)
    #: every start a live engine dispatched, or would have: `dispatch_plan` over each applied
    #: input, less the starts `spawn_superseded` refused. A run here with no `dispatch` record
    #: is a start the previous engine decided and never launched (DL-102)
    spawns: set[tuple[str, int]] = field(default_factory=set)
    #: the configuration rev the oracle runs once the records are replayed
    rev: int | None = None


#: The trace marker an update's application leaves between the inputs it falls between: the
#: entries after it run under the rev it names (`config_marker`).
CONFIG_MARKER: Final = "CONFIG"


def config_marker(rev: int) -> str:
    """The cause of a `CONFIG` trace marker for configuration rev `rev`."""
    return f"configuration rev {rev}"


#: A store's configuration rows by rev, as a replay reads them at each `segment`.
type Configurations = Callable[[int], Configuration]


def apply_configuration(oracle: Oracle, configuration: Configuration) -> None:
    """Swap `oracle`'s catalog for `configuration`'s, read under its profile, as the live
    update applied it (`Oracle.replace_catalog`): replay meets the swap where the update's
    `config applied` record stands, between the same two inputs."""
    oracle.replace_catalog(configuration.catalog, **oracle_reading(configuration.profile))


def replay_inputs(
    oracle: Oracle,
    records: list[dict[str, Any]],
    *,
    machines: Executor | None = None,
    frontiers: Frontiers | None = None,
    rev: int | None = None,
    configs: Configurations | None = None,
) -> Replay:
    """Apply the recorded attempts through `oracle` in admission order, each under its durable
    decision (concurrency-model ss4): a rejected attempt applies its time half only, and an
    applied one is fed without re-deciding, so this build reproduces the record's history and
    not the one its current gate would write. `frontiers` is where a snapshot left the log, when
    replay continues one.

    `rev` is the configuration rev `oracle` runs. A `segment` naming another rev is an update
    the engine applied there, and the oracle swaps to that rev's configuration (`configs`) before
    the next input; a store that applied one and a replay given no `configs` is refused.

    An attempt and its decision are one transaction, so an attempt with no decision means the
    store disagrees with itself and is refused. Each applied input's starts are derived as the
    live engine plans them (`spawns`), for every catalog job that is not a box.

    An input that raises stops the replay with a `ReplayFault` that names it; a
    `StatechartError` stops it as itself, naming the input."""
    decisions = read_decisions(records, machines)
    replay = Replay(decisions=decisions, frontiers=frontiers or Frontiers(), rev=rev)

    def startable() -> frozenset[str]:
        return frozenset(
            name for name, job_ir in oracle.catalog.jobs.items() if job_ir.job_type != "BOX"
        )

    dispatchable = startable()
    # the ghost-run gate starts where the oracle's rows do: a snapshot is taken only when every
    # start it covers has its `dispatch` or its `dispatch_pending`, so a STARTING overwrite of a
    # run the snapshot holds plans no second start (DL-234)
    dispatched = {job: rt.run_number for job, rt in oracle.store.job.items() if rt.run_number}
    for record in records:
        if record.get("rec") == "segment":
            found = int(record["rev"])
            if replay.rev is None:
                replay.rev = found
            elif found != replay.rev:
                if configs is None:
                    raise EngineError(
                        f"the store applied configuration rev {found} and this replay has no"
                        " configurations to read it from"
                    )
                apply_configuration(oracle, configs(found))
                # the trace says where the rev moved, as the live application's does
                oracle.mark(
                    datetime.fromisoformat(record["at"]), CONFIG_MARKER, config_marker(found)
                )
                replay.rev = found
                dispatchable = startable()
            continue
        attempt = _attempt_of(record)
        if attempt is None:
            continue
        durable = decisions.for_index(attempt.index)
        if durable is None:
            raise EngineError(
                f"input {attempt.index} has no decision: an attempt and its decision are one"
                " transaction, so the store disagrees with itself"
            )
        try:
            applied = apply_attempt(oracle, attempt, decided=durable)
        except StatechartError as exc:
            raise _at_input(exc, attempt)
        except Exception as exc:
            raise ReplayFault(attempt, exc) from exc
        plan = dispatch_plan(
            applied.emitted,
            runs={job: rt.run_number for job, rt in oracle.store.job.items()},
            dispatched=dispatched,
            live={},
            dispatchable=dispatchable,
        )
        for _kind, job, run_number in plan:  # with no live run, a plan holds starts only
            dispatched[job] = max(dispatched.get(job, 0), run_number)
            if spawn_superseded(oracle.store.job.get(job), run_number) is None:
                replay.spawns.add((job, run_number))
        replay.frontiers = replay.frontiers.admit(attempt.at).record(attempt.index)
    return replay


@dataclass(frozen=True)
class StoreReplay:
    """A reader's replay of one store (`replay_store`): the oracle it left, the replay, the wire
    records it fed, and the snapshot it started from (None for a replay from genesis)."""

    oracle: Oracle
    replay: Replay
    records: list[dict[str, Any]]
    anchor: Snapshot | None


def load_configuration(store: Store, rev: int, *, where: str) -> Configuration:
    """The store's configuration row `rev`, or refused naming `where`."""
    try:
        data = store.get_config(rev)
    except KeyError as exc:
        raise EngineError(f"{where}: the store holds no configuration rev {rev}") from exc
    return Configuration.from_bytes(data, where=where)


def replay_store(store: Store, *, where: str, fallback: Configuration | None = None) -> StoreReplay:
    """Replay a store for a reader, through a fresh oracle over the configuration it ran first,
    read under that configuration's profile, swapping to each configuration an update applied
    where its `segment` stands. A store whose oldest records were pruned replays from its latest
    snapshot, as a restart does, under the snapshot's configuration, and its trace covers what
    follows the snapshot; a pruned store with no snapshot, or with one another state version
    wrote, is refused by name. `fallback` stands in for a store that holds no configuration
    row yet."""
    first = next(iter(store.read_from(0)), None)
    anchor: Snapshot | None = None
    rev = first_rev(store)
    if first is not None and first[0] > 1:
        stored = store.latest_snapshot()
        if stored is None:
            raise EngineError(
                f"{where}: the store's oldest records are pruned and it holds no snapshot to"
                " replay from"
            )
        if stored.state_version != STATE_MACHINE_VERSION:
            raise EngineError(
                f"{where}: the snapshot at seq {stored.seq} has state version"
                f" {stored.state_version}, this build derives {STATE_MACHINE_VERSION} (DL-424)"
            )
        anchor = decode_snapshot(stored.seq, stored.data, where=where)
        rev = anchor.config_rev
    if fallback is not None and first is None:
        # a store with no record holds no configuration of its own yet
        configuration = fallback.model_copy()
    else:
        configuration = load_configuration(store, rev, where=where)
    catalog = configuration.catalog
    reading = oracle_reading(configuration.profile)
    if anchor is None:
        oracle = Oracle(catalog, **reading)
    else:
        oracle = restore_oracle(catalog, anchor.rows, **reading)
    after = 0 if anchor is None else anchor.seq
    records = [w for _, record in store.read_from(after) if (w := wire_record(record)) is not None]
    replay = replay_inputs(
        oracle,
        records,
        frontiers=None if anchor is None else anchor.frontiers,
        rev=rev,
        configs=lambda found: load_configuration(store, found, where=where),
    )
    return StoreReplay(oracle, replay, records, anchor)


def first_rev(store: Store) -> int:
    """The configuration rev a store's records open with: genesis's `config applied`, the
    first record. A store with none yet runs its lowest rev."""
    for _, record in store.read_from(0):
        if record.kind == "config" and record.payload.get("event") == "applied":
            return int(record.payload["rev"])
        break
    return 1


def _at_input(exc: StatechartError, attempt: Attempt) -> StatechartError:
    """A statechart error met in replay, with the input it met added to its report, as a
    `ReplayFault` names the input (period-model ss11)."""
    exc.facts.append(("input", f"{attempt.index}: {describe_input(attempt)}"))
    return exc


def restore_oracle(catalog: Any, rows: CarriedRows, **reading: Any) -> Oracle:
    """An oracle over `catalog` holding a snapshot's rows."""
    return Oracle(catalog, carried=rows, **reading)
