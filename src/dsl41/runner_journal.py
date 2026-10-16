"""Runner journal (ss7): the inputs-only write-ahead log and its replay (DL-74).

Lifecycle semantics: DL-41a, DL-42; ss6-ss7.

- Journal (ss7): inputs-only JSONL WAL, journal-first. Every injected event is WAL-appended
  (+fsync in the real domain) before feed(); emitted events and the trace replay from oracle
  determinism and are never stored. The input alphabet has two halves (DL-44): external events
  (input records) and time observations (advance records, written before every Oracle.advance
  the engine performs). Without the advance records an advance-fired term_run_time kill would
  vanish from replay and a late natural-exit record could resurrect the job at resume. Dispatch
  records are audit and ordering only (DL-41a): spawn.json, written by the process that spawned,
  is the authoritative spawn record, so dispatch carries wrapper_pid and run_dir, not a pgid the
  engine never observes.

- Two-record ledger (S2, concurrency-model ss4): an `input`/`advance` record is the
  `InputAttempt`, one line, so a crash cannot tear its batch, and it names its `request_id`
  and `fingerprint`. A `decision` record carries the decision that attempt got, appended after
  it, so replay is two-pass: pass one indexes the decisions, pass two applies. An attempt whose
  decision never landed is applied through the gate the live engine used (concurrency-model ss4,
  runner_admission).

- One-line decision (DL-118, period-model ss2.3): a `decision` record carries the decision, the
  revisions it moved and the effects it planned, in admission order, in one write. Separate
  records would leave a window between the decision and its KILL effect, which ss4 step 7
  forbids. `result` and standalone `effect` records are refused by name (DL-138).

- Ledger (S6a, concurrency-model ss1/ss7): a `leader` record allocates this incarnation's epoch
  by being appended, so allocation and the log's account of it are one write, under a lock on
  the run root that this object releases (runner_ledger). The opening record carries ss7's
  second pin, the state-machine version, beside the catalog hash.

- Opening record (DL-130, period-model ss2.1): `segment` is self-describing (period, estate,
  catalog, runtime profile and semantics) without reading an earlier file. `header` is a
  retired dialect (DL-138).

Every record passes one validator (`check_record`, D1). It reads the `rec` kind (current,
retired or unknown: three outcomes, three messages) and, on a `decision`, `legacy_batch`. Per-key
strictness stays with each record's own schema. It sits at `read_journal` so `runner_history`
and `retention`, which parse decision effects without `read_outbox`, inherit it.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import uuid

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from dsl41.canon import is_wire_int
from dsl41.ir import CatalogIR
from dsl41.oracle import Oracle
from dsl41.oracle_state import Event
from dsl41.runner_admission import (
    INERT_EPOCH,
    ApplyResult,
    Attempt,
    DecisionIndex,
    Frontiers,
    TimeHalfFault,
    apply_attempt,
    classify_fault,
    describe_input,
    fingerprint,
)
from dsl41.period import (
    Manifest,
    catalog_hash_at,
    check_catalog_hash_version,
    check_segment_record,
    check_segment_version,
    estate_segments,
    genesis_manifest,
    is_opening,
    opening_at,
    resolve_wal,
    root_of_wal,
    segment_record,
    write_period_manifest,
)
from dsl41.runner_clock import EngineError
from dsl41.runner_codes import STORED_CODES
from pydantic import ValidationError

from dsl41.runner_effects import Effect, EffectOutcome, Outbox, is_valid_run_id
from dsl41.runner_hosts import LOCAL_EXECUTOR_ID, HostCommand, seed_local_executor
from dsl41.runner_ledger import STATE_MACHINE_VERSION, Proof
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

if TYPE_CHECKING:  # annotation only: the WAL stays a leaf of the DL-74 DAG
    from dsl41.runner_preflight import PreflightItem


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
# final, so nothing posts to it there. The root's ignore rows exist because the completeness
# check is syntactic (statechart-engine ss10). A gap in the chart is a `StatechartError`, which
# stops the engine with exit 7 (statechart-engine ss5).

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


def _over(ctx: Context) -> bool:
    """The feed rests in `removed` or `closed`. Nothing posts to it then: the
    journal forgets the instance in the call that made it final. In `live`, a
    second `backfill_sent` finds no row and raises `UnhandledEvent`."""
    return bool(ctx.active & _FEED_FINALS)


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
        Row(
            "subscription.06",
            "feed",
            APPEND,
            guard="over",
            kind="ignored",
            cite="statechart-engine ss10",
            false_unreachable="rows .03 and .05 partition every append in `open`",
        ),
        Row(
            "subscription.07",
            "feed",
            BACKFILL_SENT,
            guard="over",
            kind="ignored",
            cite="statechart-engine ss10",
        ),
        Row(
            "subscription.08",
            "feed",
            STREAM_ENDS,
            guard="over",
            kind="ignored",
            cite="statechart-engine ss10",
            false_unreachable="row .04 takes every stream end in `open`",
        ),
    ),
    guards={"fits": _fits, "overflows": _overflows, "over": _over},
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


def dsl41_version() -> str:
    try:
        from importlib.metadata import version

        return version("dsl41")
    except Exception:  # not installed (editable src run): version is advisory
        return "0+unknown"


class Journal:
    """ss7 append-only JSONL WAL, one file per run. Inputs-only: emitted events and the trace are
    pure functions of the input sequence (oracle determinism), so only injected inputs are
    stored, and `journal render` replays them through a fresh Oracle. Record kinds: segment /
    input / advance / dispatch / drop / preflight (the module docstring covers why dispatch is
    audit-only and advances are inputs; preflight keeps the ss8 WARN caveats next to the run).
    fsync per record in the real domain (append + fsync before feed); buffered in rehearse,
    fsync on close. macOS: os.fsync does not force the drive cache (F_FULLFSYNC would, at a
    large cost); this is accepted."""

    def __init__(
        self,
        path: Path | str,
        *,
        fsync_each: bool,
        baseline_id: str = "",
        lock: Proof | None = None,
    ) -> None:
        self.path = Path(path)
        repair_tail(self.path)
        self._f = self.path.open("ab")
        os.chmod(self.path, 0o600)  # owner-only: the WAL carries globals + every input
        self._fsync_each = fsync_each
        #: concurrency-model ss2: this log's identity, part of every fingerprint. Empty for a
        #: journal written before S2 and for the journal-less engines of the bisimulation
        #: harness.
        self.baseline_id = baseline_id
        #: S6a: the run root's mutex, acquired before this file was opened and released when it
        #: is closed. Closing the log closes the lock, so an engine that dropped the file and
        #: kept the lock would exclude its own successor. None for the journal-less and
        #: lock-less engines of the bisimulation harness.
        self._lock = lock
        #: live feeds for ss10 subscribe: every appended record is fanned out post-write. The WAL
        #: never blocks on one, and a feed whose backlog would pass its budget is removed
        #: (DL-267)
        self._subscribers: list[Subscription] = []
        #: True from the start of an append's bytes until the append returns. An exception leaves
        #: it set, because a failed append does not prove its line absent or whole. A seal that
        #: meets an exception while it is set fail-stops instead of refusing (DL-274).
        self.append_unfinished = False

    @classmethod
    def create(
        cls,
        path: Path | str,
        *,
        catalog: CatalogIR,
        clock_domain: str,
        started_at: datetime,
        lock: Proof | None = None,
        manifest: Manifest | None = None,
        estate_id: str | None = None,
        opens_from_seal: Mapping[str, Any] | None = None,
        reclaimed: Mapping[str, Any] | None = None,
    ) -> Journal:
        """Open a new log with its `segment` record (period-model ss2.1, DL-130). Every segment is
        self-describing: period, catalog and semantics, without reading an earlier file.

        A caller that has installed `periods/000001/manifest.json` passes the `manifest` it
        committed, so the file and the record say the same thing (PR-22). One that only has a
        catalog (a rehearsal, an embedder, the bisimulation harness) gets a default over the empty
        bundle and the default runtime profile, installed beside the log as genesis installs one
        (DL-252). The `estate_id` is the sentinel's (DL-133): genesis mints it into `journal.jsonl`
        before this file exists and reads it back on every retry (PR-01a); it is minted here only
        for journal-only callers with no root. `opens_from_seal` is the boundary's: null on segment
        1, `{period_id, digest}` on every later segment, copied verbatim from the seal the period
        opened from so the two openings of one seal are byte-identical (PR-07). `reclaimed` is
        null unless a `dsl41 estate reclaim --force` cleared this opener's way; then it names the
        claim that was moved and the actor who claimed to authorize it (ss1.3)."""
        if manifest is None:
            manifest = genesis_manifest(
                catalog,
                clock_domain=clock_domain,
                # ss7's other pin, gated where catalog_hash is (S6a, runner_ledger): leader
                # eligibility means the two match, and an unread pin is not a pin
                state_machine_version=STATE_MACHINE_VERSION,
            )
            # installed before the record that names it, as genesis does: replay reads the
            # period's profile (its semantic switches above all) from a manifest bound to the
            # segment and refuses without one (DL-252)
            write_period_manifest(root_of_wal(Path(path)), manifest)
        else:
            # a native log pins the current recipe through the one dispatcher (D4, DL-138), so
            # this gate, `check_segment_record` and `catalog_hash_at` answer one way
            check_catalog_hash_version(
                manifest.catalog_hash_version, where=f"{path}: a new log's manifest"
            )
            # under the recipe the manifest itself names, so no gate assumes a version
            expected = catalog_hash_at(manifest.catalog_hash_version, catalog)
            if manifest.catalog_hash != expected:
                raise EngineError(
                    f"manifest catalog_hash {manifest.catalog_hash} is not this catalog's"
                    f" ({expected}): the log would open on a period it does not describe"
                )
        if manifest.clock_domain != clock_domain:
            raise EngineError(
                f"manifest clock_domain {manifest.clock_domain!r} is not this run's"
                f" ({clock_domain!r})"
            )
        journal = cls(
            path, fsync_each=clock_domain == "real", baseline_id=manifest.baseline_id, lock=lock
        )
        # the identity lives in the record and nowhere else on this object; a second copy would
        # be a second authority
        journal._write(
            segment_record(
                manifest,
                estate_id=estate_id or str(uuid.uuid4()),
                at=started_at,
                opens_from_seal=opens_from_seal,
                reclaimed=reclaimed,
            )
        )
        # the opening record names the segment, so it is durable before any head action
        # (genesis finalize, a claim's open) relies on the file's existence: a virtual-domain
        # journal buffers everything else
        journal._f.flush()
        os.fsync(journal._f.fileno())
        return journal

    def leader(self, *, epoch: int, at: datetime) -> None:
        """One term of leadership over this run root (ss1's leader record, S6a). Appended under
        the lock right after acquiring it, so the epoch is monotone: it is allocated by being
        written, and no two terms read the same log and choose the same number. Every input
        between two of these records was admitted by the incarnation the earlier one names."""
        self._write(
            {
                "rec": "leader",
                "epoch": epoch,
                "at": at.isoformat(),
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "dsl41_version": dsl41_version(),
            }
        )

    def admit(self, attempt: Attempt) -> None:
        """Append one admitted input (the concurrency-model ss4 step-4 batch) as one line, so a
        crash cannot leave its time observation without its attempt or the reverse.

        The three record names are the three shapes of an attempt: `input` has an oracle verb,
        `host` carries a routing-table command (S5a, concurrency-model ss8), `advance` is the
        standalone time observation (DL-44). All three carry the same admission fields. `source`
        in {scheduler, adapter, control, reconcile} (ss7); None marks an unattributed script
        injection and never occurs in a real run. It is persisted because replay must re-derive
        the same causes and gate verdict from it (DL-68).

        S3 adds the rest of the ss6 envelope. `epoch` rides on every record because it is the
        leader's, and S6 fences on it. `expect` and `claimed_actor` ride only where they exist:
        an input the engine raised has neither, and nulls would blur which inputs were
        externally requested and so had to name a revision."""
        record: dict[str, Any] = {
            "rec": "input"
            if attempt.kind is not None
            else ("host" if attempt.host is not None else "advance"),
            "seq": attempt.index,
            "at": attempt.at.isoformat(),
            "request_id": attempt.request_id,
            "fingerprint": attempt.fingerprint,
            "epoch": attempt.epoch,
        }
        if attempt.kind is not None:
            record |= {"kind": attempt.kind, "payload": attempt.payload, "source": attempt.source}
        elif attempt.host is not None:
            # under its own key, not `payload`: one field name is spelled one way
            record |= {"host": attempt.host.wire(), "source": attempt.source}
        if attempt.expect is not None:
            record["expect"] = attempt.expect
        if attempt.claimed_actor is not None:
            record["claimed_actor"] = attempt.claimed_actor
        self._write(record)

    def decision(self, result: ApplyResult, effects: Sequence[Effect]) -> None:
        """Append the whole ss4 step-7 batch as one line (period-model ss2.3, DL-118): the
        decision an admitted attempt got, the revisions it moved, and every effect it planned.

        One line, one fsync, as `admit`. Each effect lands here before anything attempts it, which
        is what an outbox is: an engine that dies between deciding and acting leaves the record
        that it meant to act, and a recorded kill is re-driven at resume. Separate records leave
        a window in which the decision is durable and its intents are not (CM-17).

        Written unconditionally, including for an application that moved nothing and planned
        nothing: the absence of a decision is how replay recognises the crash window.

        `effects` is in admission order, the order `Outbox` receives them, so a SPAWN precedes its
        run's later KILL.

        The index rides under `index`, not `seq`: `seq` is the ss10 subscribe cursor, and a
        decision shares its attempt's number, so a second record under one cursor value would be
        undeliverable to a resuming subscriber (DL-89).

        `legacy_batch` is on every decision, `false` from this writer, and stays on the record
        (D2, DL-138): false proceeds, true is a retired dialect, anything else is malformed
        (`check_record`).

        A native decision names its identity at birth: `generation` on every effect, `run_id` on
        every SPAWN (DL-118, PR-16/PR-36a); this writer refuses one without. The model's None
        defaults let a hand-built record validate before this gate reads it. A native rejection
        names one of the append-only `STORED_CODES` (period-model ss2.3); a reader never requires
        one."""
        if result.decision == "rejected" and result.code not in STORED_CODES:
            raise EngineError(
                f"decision {result.index}: a native rejection names its stored code"
                f" at birth, and this one names {result.code!r}"
            )
        for effect in effects:
            if (
                effect.generation is None
                or (effect.kind == "SPAWN" and effect.run_id is None)
                or (effect.run_id is not None and not is_valid_run_id(effect.run_id))
            ):
                raise EngineError(
                    f"effect {effect.effect_id}: a native decision binds identity at"
                    " birth -- generation on every effect, run_id on a SPAWN, and any"
                    " run_id in the ss11a uuid4 grammar (DL-118); an empty or"
                    " freehand id would lose to a fallback mint downstream"
                )
        self._write(
            {
                "rec": "decision",
                "index": result.index,
                "request_id": result.request_id,
                "decision": result.decision,
                "reason": result.reason,
                "code": result.code,
                "revisions": result.revisions,
                "legacy_batch": False,
                "effects": [effect.model_dump(mode="json") for effect in effects],
            }
        )

    def seal(self, record: Mapping[str, Any]) -> None:
        """ss2.2's `seal`: the last record of a period's last segment, and the boundary's commit
        point.

        It is a record and not a decision because a `seal` cannot be decided by an ordinary
        `decision`: a crash before it would leave a durable "applied" for a boundary that never
        happened, and records after a seal are forbidden. `boundary.check_seal_record` owns and
        validates the shape; nothing else can be written through this door.

        Appending it is the point of no return (ss7). The writer flushes the whole line before
        `fsync`, and an `fsync` error does not prove the line absent or non-durable, so a failure
        here is an unknown outcome that fail-stops; an abort would reopen admission behind a seal
        line that then survives."""
        if record.get("rec") != "seal":
            raise EngineError(f"Journal.seal writes `seal` records, not {record.get('rec')!r}")
        rec = dict(record)
        if self._lock is not None:
            self._lock.check()  # the same ss1 fence every append passes
        # plain json.dumps, not canon.canonical_bytes (as `_write`): nothing digests a WAL
        # line's own bytes (a seal record's digest is computed over canon bytes in `boundary`
        # and re-derived on read), and the control door refuses a lone surrogate before dispatch
        line = json.dumps(rec, sort_keys=True).encode("utf-8") + b"\n"
        self._f.write(line)
        # unconditionally durable, whatever the per-record policy: a seal durable only at close
        # would let the anchor's open->closed CAS land before the record that justifies it, naming
        # a successor by a head whose naming line a power cut then removes. Durability precedes
        # subscriber publication (not `_write`), because a subscriber told of a commit record
        # whose fsync then fails has heard of a boundary recovery may discard
        self._f.flush()
        os.fsync(self._f.fileno())
        self._publish(rec, len(line))

    def effect_result(self, outcome: EffectOutcome) -> None:
        """What came of one attempt (concurrency-model ss5). Absent means `pending`, the crash
        window; `indeterminate` is kept apart: something was tried and cannot be reported on."""
        self._write({"rec": "effect_result", **outcome.model_dump(mode="json")})

    def dispatch(
        self,
        job: str,
        run_number: int,
        *,
        wrapper_pid: int | None,
        run_dir: str | None,
        started_at: datetime,
    ) -> None:
        self._write(
            {
                "rec": "dispatch",
                "job": job,
                "run_number": run_number,
                "wrapper_pid": wrapper_pid,
                "run_dir": run_dir,
                "started_at": started_at.isoformat(),
            }
        )

    def drop(self, ev: Event, reason: str) -> None:
        self._write(
            {
                "rec": "drop",
                "at": ev.at.isoformat(),
                "kind": ev.kind,
                "payload": ev.payload,
                "reason": reason,
            }
        )

    def preflight(self, items: list[PreflightItem]) -> None:
        """ss8: WARN prints, journals, and runs; the record keeps the run's stated caveats next to
        its inputs. Replay ignores it (not an input); read_journal carries it."""
        self._write(
            {
                "rec": "preflight",
                "items": [item.model_dump() for item in items],
            }
        )

    def subscribe(
        self,
        machines: Executor,
        on_overflow: Callable[[str], None] | None = None,
        *,
        budget: int = sys.maxsize,
    ) -> Subscription:
        """A live feed of every record appended from now on, held to `budget` bytes (the control
        server passes ss5's). `machines` is the engine's executor, which runs the feed's
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

    def _write(self, record: dict[str, Any]) -> None:
        if record.get("rec") not in CURRENT_RECS:
            # the write side of DL-138's registry: a record kind absent from `CURRENT_RECS` is
            # refused here, not written and then refused by every reader. The `seal` record is
            # appended by the boundary under its own schema check; it is in the set for readers
            raise EngineError(
                f"{self.path}: refusing to append record kind"
                f" {record.get('rec')!r} -- this binary writes"
                f" {', '.join(sorted(CURRENT_RECS))} (period-model ss2)"
            )
        if self._lock is not None:
            # ss1's epoch-conditional append, and ss7's "losing proof stops dispatch, not merely
            # renewal" (S6b). Before the write, so a leader that cannot prove it leads admits
            # nothing. An append precedes every effect (the outbox records intent first), so
            # fencing appends fences dispatch. No background prober is needed: an engine with
            # nothing to append dispatches nothing.
            self._lock.check()
        line = json.dumps(record, sort_keys=True).encode("utf-8") + b"\n"
        self.append_unfinished = True
        self._f.write(line)
        self._f.flush()
        if self._fsync_each:
            os.fsync(self._f.fileno())
        self._publish(record, len(line))
        self.append_unfinished = False

    def close(self) -> None:
        self.detach()
        if self._lock is not None:
            self._lock.release()  # the term ends where the log does (S6a)

    def detach(self) -> None:
        """Flush and close the file, leaving the term where it is.

        `close` ends both, because for an engine the log and the lock end together (S6a). A
        physical roll ends neither: it writes the opening `segment` and hands the root to the
        ordinary resume path, which opens its own journal over the same file and holds the same
        proofs. Closing this one the ordinary way would release the proofs its successor appends
        under (period-model ss7, DL-134)."""
        self._f.flush()
        os.fsync(self._f.fileno())
        self._f.close()


def repair_tail(path: Path) -> None:
    """Make an existing WAL end on a line boundary before anything appends.

    `_write` puts `json + "\\n"` in one call, and a crash mid-write leaves a prefix.
    `read_journal` drops a torn final line, and the bytes must agree with that before the next
    record lands: appended after the fragment, the successor's `leader` would make one corrupt
    interior line of the two. Under the caller's lock, with fsync: a tail that parses lost only
    its newline and gets one; any other tail is cut at the last newline. `read_journal` returns
    the same records before and after."""
    if not path.exists():
        return
    with path.open("r+b") as f:
        data = f.read()
        if not data or data.endswith(b"\n"):
            return
        cut = data.rfind(b"\n") + 1
        try:
            json.loads(data[cut:])
        except ValueError:
            f.truncate(cut)
        else:
            f.write(b"\n")
        f.flush()
        os.fsync(f.fileno())


#: ss2's record kinds this binary reads, pinned from the writers above and every read site in
#: the tree (period-model ss2, runner-design ss7). `host` is current: a host command is admitted
#: and replays like an input.
CURRENT_RECS: Final[frozenset[str]] = frozenset(
    {
        "segment",
        "leader",
        "host",
        "input",
        "advance",
        "decision",
        "effect_result",
        "dispatch",
        "drop",
        "preflight",
        "seal",
    }
)

#: Retired record dialects: the kind, and the entry that retired it. Append-only
#: (docs/protocol-evolution.md ss6): a row is never removed or re-used, so a root written before
#: the retirement is told what it holds, not given a generic parse error.
RETIRED_RECS: Final[dict[str, str]] = {
    "header": "DL-138",
    "result": "DL-138",
    "effect": "DL-138",
}


def check_record(record: Mapping[str, Any], *, where: str) -> None:
    """D1's one record validator (DL-138): `rec` and `legacy_batch`, three outcomes each.

    `rec`: a current kind proceeds; a retired kind refuses naming the kind and the entry that
    retired it; anything else refuses as unknown. The two refusals differ because "this was
    legal" and "this was never legal" send an operator to different places
    (docs/protocol-evolution.md ss6). An unrecognised kind inside a version-matched segment is
    corruption.

    `legacy_batch` on a `decision`: exactly `False` proceeds, `True` is the retired fold dialect
    and refuses by name, missing or non-boolean is malformed, because a record that cannot say
    what it is is not an old record.

    Per-record key strictness is whatever each record's own schema declares."""
    kind = record.get("rec")
    retired = RETIRED_RECS.get(kind) if isinstance(kind, str) else None
    if retired is not None:
        raise EngineError(
            f"{where}: `{kind}` is a RETIRED record dialect, refused by name since"
            f" {retired} -- nothing has written one since DL-118/DL-130 and no root"
            " holding one exists (docs/protocol-evolution.md ss6, ss8)"
        )
    # `kind` may be any JSON value, including an unhashable array or object (DL-227):
    # `isinstance` first keeps `in CURRENT_RECS` from raising a TypeError, so every non-current
    # kind takes the unknown-kind refusal by name (ss1)
    if not isinstance(kind, str) or kind not in CURRENT_RECS:
        raise EngineError(
            f"{where}: unknown record kind {kind!r} -- this binary reads"
            f" {', '.join(sorted(CURRENT_RECS))}; an unrecognised kind inside a"
            " version-matched segment is corruption, not an older dialect"
            " (period-model ss2)"
        )
    if kind != "decision":
        return
    marker = record.get("legacy_batch")
    if marker is True:
        raise EngineError(
            f"{where}: `legacy_batch: true` is a RETIRED record dialect, refused by"
            " name since DL-138 -- it marked a batch folded from a legacy estate's"
            " separate fsyncs, and the path that folded one is gone"
            " (docs/protocol-evolution.md ss6, ss8)"
        )
    if marker is not False:
        raise EngineError(
            f"{where}: legacy_batch is {marker!r} -- a `decision` carries the boolean"
            " false, and a record that cannot say what it is is malformed rather"
            " than old (period-model ss2.3)"
        )


def opens_with_rec(path: Path) -> str | None:
    """The `rec` kind this file's first line declares, or None when the first line is not a
    JSON object with a string `rec`.

    The one place the first line is sniffed. Two owners ask, `boundary.claim_root` and
    `retention.plan_retention`, neither routing through `read_journal`; each writes its own
    refusal so the tombstone names the operation the operator ran (D5, DL-138).

    It reports the kind raw and judges nothing. Both tombstones are header-only: `header` was a
    legal opening, so a file that opens with one is a recognised pre-period root. `result` and
    `effect` were never openings, so a file opening with one is unknown residue and gets each
    owner's generic refusal (D5, DL-138)."""
    try:
        with Path(path).open("rb") as handle:
            first = handle.readline()
    except OSError:
        return None
    try:
        payload = json.loads(first)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    kind = payload.get("rec")
    return kind if isinstance(kind, str) else None


def read_journal(path: Path | str) -> list[dict[str, Any]]:
    """Parse a run journal. A torn final line (crash mid-append) is dropped, because write-ahead
    means its feed never ran; a torn or invalid interior line is corruption and raises.

    The opening's `catalog_hash_version` is dispatched first, before any record is validated: a
    segment names the dialect of the whole file, and a file this build cannot read is owed that
    verdict (DL-138, D4). Every record then passes `check_record`, still before the opening
    check, so a journal that opens with a retired `header` is told what it holds (DL-138). The
    first record must be a `segment` (DL-130).

    `path` may be an estate root, its sentinel, or a segment file: `resolve_wal` follows the
    sentinel's `see` once, so callers holding either read the same records (period-model
    ss1.1)."""
    records: list[dict[str, Any]] = []
    path = resolve_wal(path)
    raw = path.read_bytes()
    lines = raw.split(b"\n")
    trailing = lines.pop() if lines and lines[-1] == b"" else None
    for index, line in enumerate(lines):
        if not line:
            raise EngineError(f"journal {path}: empty interior line {index + 1}")
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            if index == len(lines) - 1 and trailing is None:
                break  # torn final append: the feed it preceded never ran
            raise EngineError(f"journal {path}: corrupt line {index + 1}: {exc}") from exc
    # D4 before D1: the opening's version verdict owns the file's fate. A reader that validated
    # records first would answer for a dialect it cannot read, and report a retired opening
    # followed by an unknown kind as corruption (DL-138, D4). The dispatch is on the opening
    # only; a version-matched file's later records are D1's.
    if records and isinstance(records[0], dict) and is_opening(records[0]):
        check_segment_version(records[0])
    # D1's pass is its own loop, after the parse loop so corrupt bytes are called corrupt, not a
    # retired dialect, and before the opening check so a journal that opens with a retired
    # `header` is told what it holds, not that it lacks a segment (DL-138)
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            # a line that is not a JSON object has no `rec` to dispatch on
            raise EngineError(
                f"journal {path}: line {position + 1} is not a record object (period-model ss2)"
            )
        check_record(record, where=f"journal {path}: line {position + 1}")
    if not records or not is_opening(records[0]):
        raise EngineError(f"journal {path}: missing segment record")
    check_segment_record(records[0])  # exact ss2.1 shape, unconditional (DL-138)
    for position, record in enumerate(records):
        if position > 0 and is_opening(record):
            # I1: one segment record per file, and it is line 1. An embedded `segment` mid-file
            # is a splice: a reader that split on it would treat forged records as a validated
            # file boundary and admit an unchained period
            raise EngineError(
                f"journal {path}: an opening record at line {position + 1} -- a"
                " segment opens a FILE, and a second opening inside one is a splice"
                " (period-model ss2.1, I1)"
            )
        if record.get("rec") == "seal" and position != len(records) - 1:
            # ss11's recovery matrix: a `seal` is the last record of its segment, so a record
            # after one is a period that admitted work after its own boundary: interior
            # corruption, not a torn tail
            raise EngineError(
                f"journal {path}: {len(records) - position - 1} record(s) after the `seal`"
                f" at line {position + 1}: the old period admits nothing after its"
                " boundary (period-model ss11, PR-29)"
            )
    return records


def check_segment_tail(segment: Path, records: Sequence[Mapping[str, Any]]) -> None:
    """A closed segment ends in its `seal` (ss11).

    `read_journal` tolerates a torn final line, which is right for the file an appender still
    appends to and wrong for a closed one: there the tolerated tail is a hole in the middle of
    the concatenation.

    One helper and one text, positioned per caller (DL-139, DL-142): each reader knows a segment
    is closed at a different point (the subscriber's backfill when it has read a newer one, the
    cross-period replay when the segment is not the last it was given), and the check belongs
    at each of them. The refusal is one sentence because it is one corruption."""
    if records[-1].get("rec") != "seal":
        raise EngineError(
            f"journal {segment}: a closed segment ends in a `seal` and this one"
            f" ends in {records[-1].get('rec')!r} -- its tail is missing, and a"
            " backfill across it would skip records silently (period-model ss11)"
        )


def check_segment_identity(segment: Path, opening: Mapping[str, Any]) -> int | None:
    """One segment file, checked against where it sits: the estate its root's sentinel names and
    the period its own filename claims.

    One implementation, because it is a fact about a file and not a reader (DL-139, as
    `check_segment_adjacency`). The subscriber's backfill needs it because its early stop can
    read a single segment, where no adjacency check runs. `dsl41 journal`'s cross-period replay
    needs it too: a root whose segments were all rewritten to a stranger's `estate_id` agrees
    with itself at every boundary, so the sentinel alone says whose lineage this is.

    Returns the period the filename names, or None for a file whose name is not a number (a
    sentinel-less root's `journal.jsonl`, which its owner refuses by name, DL-138). Callers that
    number segments read it from here."""
    from dsl41.period import read_sentinel, root_of_wal

    try:
        named = int(segment.stem)
    except ValueError:
        return None
    sentinel = read_sentinel(root_of_wal(segment))
    if sentinel is not None and opening.get("estate_id") != sentinel.estate_id:
        raise EngineError(
            f"journal {segment}: estate {opening.get('estate_id')!r} under a"
            f" sentinel naming {sentinel.estate_id} -- a stranger's segment under this"
            " estate's name (period-model ss1.2)"
        )
    claimed = opening.get("segment_no", opening.get("period_id"))
    if claimed != named:
        # a foreign segment renamed into place parses and even seals; its own opening says which
        # period it is
        raise EngineError(
            f"journal {segment}: names segment {named} and its opening says"
            f" {claimed!r} -- a stranger's file under this estate's name"
            " (period-model ss2.1)"
        )
    return named


def check_segment_adjacency(
    older: Sequence[Mapping[str, Any]], newer: Sequence[Mapping[str, Any]], *, where: str
) -> None:
    """Two consecutive segments of one lineage, checked both ways: the older's closing seal must
    be the one the newer opened from, the two must name one estate, and the index frontier must
    be continuous.

    One implementation, because a boundary is one fact and two readers must not disagree about
    the same corruption (DL-139). The subscriber's backfill meets it inside a run root;
    `dsl41 journal`'s cross-period replay meets it across roots, where the older segment is in the
    root the lineage rolled out of. A rolled boundary is the same record pair in two files.

    The older segment must already have passed `check_segment_tail`: a closed segment with a torn
    tail has no seal to be adjacent to, and each caller runs that check where it first knows the
    segment is closed.

    `where` names the stream being read, since a reader that spans roots cannot say whose chain
    broke from the records alone."""
    seal_record = older[-1]
    opening = newer[0]
    # the full ss2.2 schema first: a `closes_at_index` rewritten to a bool or a string would
    # otherwise skip the exact-int continuity comparison below
    from dsl41.boundary import check_seal_record

    check_seal_record(seal_record)
    link = opening.get("opens_from_seal")
    if not (
        isinstance(link, dict)
        and link.get("digest") == seal_record.get("digest")
        and seal_record.get("next_period_id") == opening.get("period_id")
    ):
        raise EngineError(
            f"{where}: segment {opening.get('period_id')!r} does not open"
            f" from the seal that closes the one before it -- a spliced or foreign"
            " lineage (period-model ss2.1, ss11)"
        )
    if seal_record.get("estate_id") != opening.get("estate_id"):
        raise EngineError(
            f"{where}: estate {seal_record.get('estate_id')!r} sealed and"
            f" {opening.get('estate_id')!r} opened -- two estates in one stream"
            " (period-model ss1.2)"
        )
    opens_at = opening.get("first_index")
    closes = seal_record.get("closes_at_index")
    if is_wire_int(opens_at) and is_wire_int(closes) and opens_at != closes + 1:
        raise EngineError(
            f"{where}: segment {opening.get('period_id')!r} starts its index"
            f" range at {opens_at} and the seal before it closes at {closes} -- the"
            " index frontier is not continuous (I2)"
        )


@dataclass(frozen=True)
class Backfill:
    """What one resuming subscriber is owed, and what this root retains (DL-135, period-model
    ss11). Both halves come out of one read, so "is there a gap" is about the records that
    follow it."""

    #: the lowest index this root can still serve, when the cursor is below it; None when it is
    #: not. The gap is the answer, not the number, so a caller need not re-derive the comparison
    #: (period-model ss11)
    gap_from: int | None
    #: every retained record that could sit after the cursor, oldest first
    records: list[dict[str, Any]]


class BackfillRefused(EngineError):
    """A refusal on the backfill read; the control code is `backfill_refused` (control-protocol
    ss2)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="backfill_refused")


def read_backfill(path: Path | str, *, since: int) -> Backfill:
    """The records a subscriber resuming at `since` may still be owed (DL-135). A refusal from
    any check below reaches the subscriber as `backfill_refused` (control-protocol ss2). The
    checks are shared with replay and audit, which read no code, so the refusal is re-raised as
    a `BackfillRefused` at the one door the stream reads through."""
    try:
        return _read_backfill(path, since=since)
    except EngineError as exc:
        raise BackfillRefused(str(exc)) from exc


def _read_backfill(path: Path | str, *, since: int) -> Backfill:
    """`read_backfill`'s body.

    `read_journal` reads one segment, which is what an appender, a replay and an audit want
    (I1: a segment is a period). A subscriber spans periods: its cursor is an estate-wide index
    (I2) and may name a segment the appender has moved on from.

    It reads backwards and stops at the first segment holding a record at or below the cursor,
    because everything older sits before the caller's positional cut. A cursor in the live
    period costs one segment; only `since` at the very beginning costs the estate. Stopping early
    also says there is no gap: a segment holding the cursor is a root that retains it, so the gap
    question is asked only when every segment has been read.

    Every segment before the last must end in a `seal`, asked of each segment as it is read and
    before any other check, so a doubly-faulted segment names the fault that makes the stream a
    lie."""
    segments = estate_segments(path)
    chunks: list[list[dict[str, Any]]] = []
    holds_cursor = False
    numbers: list[int] = []
    for position, segment in enumerate(reversed(segments)):
        records = read_journal(segment)
        # a torn tail is decided before identity, sequence and contiguity (DL-142), so a segment
        # with two faults reports the one that makes the stream a lie
        if position > 0:
            check_segment_tail(segment, records)
        opening = records[0]
        named = check_segment_identity(segment, opening)
        if named is not None:
            numbers.append(named)
        first = _first_index(opening)
        for record in records:
            seq = record.get("seq")
            if is_wire_int(seq) and seq < first:
                # I2: a segment allocates from its own first_index upward. A forged low seq
                # would make the containment test below claim this segment holds an old cursor
                # and skip every older segment and its chain checks
                raise EngineError(
                    f"journal {segment}: seq {seq} below this segment's first_index"
                    f" {first} -- a record this period could not have admitted (I2)"
                )
        chunks.append(records)
        if first <= since:
            # positional containment from the validated opening, never from a record scan a
            # forged seq could satisfy: the cursor sits in the first segment whose index range
            # begins at or before it
            holds_cursor = True
            break
    if numbers and numbers != list(range(numbers[0], numbers[0] - len(numbers), -1)):
        raise EngineError(
            f"journal {path}: retained segments {sorted(numbers)} are not contiguous --"
            " a missing middle segment is a hole a backfill would stream past"
            " silently (period-model ss11, I1)"
        )
    for newer, older in zip(chunks, chunks[1:]):
        check_segment_adjacency(older, newer, where=f"journal {path}")
    first = _first_index(chunks[-1][0])
    # index 1 is the first index there is, so no cursor below it names a lost record
    gap = not holds_cursor and max(since, 0) < first - 1
    return Backfill(
        gap_from=first if gap else None,
        records=[record for chunk in reversed(chunks) for record in chunk],
    )


def _first_index(opening: Mapping[str, Any]) -> int:
    """A segment's own `first_index`: the first index it may allocate, not the lowest `seq` in
    it. A period that admitted no input still covers its index range, and I2 makes the number
    estate-wide.

    The `else 1` is defensive and describes nothing on disk: `check_segment_record` requires an
    int >= 1 on every segment `read_journal` returns (DL-138)."""
    first = opening.get("first_index")
    return first if is_wire_int(first) else 1


def baseline_id(records: list[dict[str, Any]]) -> str:
    return str(records[0].get("baseline_id") or "")


def read_attempts(records: list[dict[str, Any]]) -> list[Attempt]:
    """The log's admitted inputs, in admission order."""
    base = baseline_id(records)
    attempts = [a for record in records if (a := _attempt_of(record, base)) is not None]
    return sorted(attempts, key=lambda a: a.index)


def _attempt_of(record: dict[str, Any], base: str) -> Attempt | None:
    """The attempt one WAL record admitted, or None for a record that is not an attempt. `base` is
    the log's `baseline_id`.

    A record written before S2 carries no `request_id` or `fingerprint`; it gets the ones it
    would have been admitted under, so the pipeline sees one kind of attempt. The synthesized id
    names its position in this log."""
    rec = record.get("rec")
    if rec not in ("input", "advance", "host"):
        return None
    kind = record.get("kind") if rec == "input" else None
    payload = record.get("payload") or {}
    host = HostCommand.from_wire(record["host"]) if rec == "host" else None
    source = record.get("source")
    expect = record.get("expect")
    actor = record.get("claimed_actor")
    epoch = int(record.get("epoch", INERT_EPOCH))
    return Attempt(
        index=int(record["seq"]),
        at=datetime.fromisoformat(record["at"]),
        request_id=str(record.get("request_id") or f"log:{record['seq']}"),
        fingerprint=str(
            record.get("fingerprint")
            or fingerprint(
                baseline_id=base,
                kind=kind,
                payload=payload,
                source=source,
                epoch=epoch,
                expect=expect,
                claimed_actor=actor,
                host=host,
            )
        ),
        kind=kind,
        payload=payload,
        host=host,
        source=source,
        # the precondition replays with the attempt: an input admitted without a result is
        # re-decided through the same gate, and the revision it named is half of what the gate
        # reads
        expect=expect,
        epoch=epoch,
        claimed_actor=actor,
    )


def read_decisions(
    records: list[dict[str, Any]], machines: Executor | None = None
) -> DecisionIndex:
    """Pass one: the durable decisions, indexed. Built from the whole log before anything is
    applied, because an attempt says nothing about its own fate: its decision is a later record,
    and the gap between them is the crash window (concurrency-model ss4).

    One read in WAL order: an attempt opens its request id's `ADMISSION` instance and its
    decision closes it, so only attempts in the crash window are open at the end, and replay's
    second pass decides them. `machines` is the executor the index runs on: a resuming engine's,
    or the index's own.

    A `StatechartError` stops replay as itself, its report naming the WAL record, as `_at_input`
    names an input (statechart-engine ss5).

    `decision` is the one kind; `result` is a retired dialect refused by name at `check_record`
    (DL-118, DL-138)."""
    index = DecisionIndex(machines)
    base = baseline_id(records)
    for record in records:
        attempt = _attempt_of(record, base)
        if attempt is not None:
            try:
                index.note(attempt)
            except StatechartError as exc:
                raise _at_input(exc, attempt)
        elif record.get("rec") == "decision":
            result = ApplyResult(
                index=int(record["index"]),
                request_id=str(record["request_id"]),
                decision=record["decision"],
                reason=record.get("reason"),
                # absent on a record written before codes existed: null, never required or
                # checked against the registry
                code=record.get("code"),
                revisions=record.get("revisions") or {},
            )
            try:
                index.record(result)
            except StatechartError as exc:
                exc.facts.append(("WAL input", f"{result.index}: the {result.decision} decision"))
                raise
    return index


def decision_effects(record: Mapping[str, Any]) -> list[Effect]:
    """The typed effects one `decision` planned (period-model ss2.3).

    The one decoder for "is this decision's effect list valid": `read_outbox` rebuilds the
    outbox from it, and `runner_history` reads the executor and the bound run_id from the same
    list (DL-139).

    It refuses and never degrades, because a decision is durable evidence. A `decision` has one
    shape, a list `effects`; `.get(...) or []` would read a corrupt or foreign record as an empty
    native decision, losing intents and inventing provenance.

    No `where` argument: the record names itself by `index`, so the same corrupt record gets the
    same refusal from every reader. `rec` and `legacy_batch` are `check_record`'s, upstream of
    every caller (DL-138)."""
    effects_field = record.get("effects")
    if not isinstance(effects_field, list):
        raise EngineError(
            f"decision at index {record.get('index')}: effects is"
            f" {type(effects_field).__name__}, not a list (DL-118)"
        )
    parsed_effects: list[Effect] = []
    for effect in effects_field:
        try:
            parsed = Effect.model_validate(effect)
        except ValidationError as exc:
            # strict fields surface here: `generation: false` or "0" must not coerce past the
            # fold's exact-0 gate
            raise EngineError(
                f"decision at index {record.get('index')}: malformed effect: {exc}"
            ) from exc
        if parsed.run_id is not None and not is_valid_run_id(parsed.run_id):
            # any run_id a decision carries is in the ss11a grammar
            raise EngineError(
                f"decision at index {record.get('index')}: effect"
                f" {parsed.effect_id} carries run_id {parsed.run_id!r} outside"
                " the ss11a grammar (DL-118)"
            )
        if parsed.generation is None or (parsed.kind == "SPAWN" and parsed.run_id is None):
            # the writer refuses these shapes (Journal.decision), so a native decision carrying
            # one is corruption or a foreign writer; accepting it would let resume mint an
            # identity after the transaction (DL-118)
            raise EngineError(
                f"decision at index {record.get('index')}: native effect"
                f" {parsed.effect_id} carries no birth identity (DL-118)"
            )
        parsed_effects.append(parsed)
    return parsed_effects


def read_outbox(records: list[dict[str, Any]], outbox: Outbox | None = None) -> Outbox:
    """The effects this log intended, and what became of them (ss5).

    `outbox` is what the segment opened holding: a period opened from a seal carries C1's
    undelivered intents and applied bindings (period-model ss3.5), and an `effect_result` here
    for one of them is an outcome to attach. It is seeded, not patched in afterwards, because
    `resolve` refuses an outcome for an effect it never saw.

    One pass, in file order, because an outcome follows the effect it resolves; an outcome for
    an unknown effect means the log lost the record that said what was meant. A `decision`'s
    nested list is in admission order, which is all per-run ordering takes.

    The standalone `effect` record and `legacy_batch: true` are retired dialects, refused at
    `check_record` (DL-138). The per-effect shape is `decision_effects`, shared with run history
    (DL-139).

    A `StatechartError` from the outbox's effect chart is not a replay fault: it stops replay as
    itself, its report naming the WAL record, as `_at_input` names an input (statechart-engine
    ss5)."""
    outbox = outbox if outbox is not None else Outbox()
    for record in records:
        try:
            _read_effect_record(outbox, record)
        except StatechartError as exc:
            exc.facts.append(("WAL input", _effect_record_name(record)))
            raise
    return outbox


def _read_effect_record(outbox: Outbox, record: dict[str, Any]) -> None:
    """Apply one WAL record's effects or outcome to `outbox`; any other
    record holds neither."""
    if record.get("rec") == "decision":
        for parsed in decision_effects(record):
            outbox.record(parsed)
    elif record.get("rec") == "effect_result":
        outcome = EffectOutcome.model_validate({k: v for k, v in record.items() if k != "rec"})
        try:
            outbox.resolve(outcome)
        except EngineError as exc:
            # an outcome the outbox refuses (an unknown effect, a stranger's run_id, a second
            # outcome) stops replay naming the effect, as a raising input does (period-model ss11)
            raise OutcomeReplayFault(outcome, exc) from exc


def _effect_record_name(record: dict[str, Any]) -> str:
    """A WAL record of the outbox, as a statechart error's report names it."""
    if record.get("rec") == "decision":
        return f"{record.get('index')}: the decision's effects"
    return f"the {record.get('state')} outcome of effect {record.get('effect_id')}"


class ReplayFault(EngineError):
    """A logged input raised when this build replayed it (period-model ss11). Replay stops there,
    naming the input, and so does every resume of the period: the same build raises at the same
    input. The deployment runbook gives the two ways out: deploy a fix, or roll back to the
    release that wrote the log.

    It stops on an input with a durable decision, and on an undecided one whose time half raises
    on its own. An undecided attempt whose time half applies alone is quarantined instead
    (`classify_fault`), and resume writes that decision.

    The cause's `code`, if it has one, rides on; the cause is chained. `index` is the input's, or
    None when the fault is in a record that is not an input (`OutcomeReplayFault`)."""

    index: int | None

    def __init__(self, attempt: Attempt, cause: Exception) -> None:
        if attempt.kind is not None:
            what = attempt.kind
        elif attempt.host is not None:
            what = f"host {attempt.host.verb}"
        else:
            what = "time observation"
        super().__init__(
            f"replay stopped at input {attempt.index} ({what} from"
            f" {attempt.source or 'no source'} at {attempt.at.isoformat()},"
            f" request_id {attempt.request_id}): {type(cause).__name__}: {cause}",
            code=cause.code if isinstance(cause, EngineError) else None,
        )
        self.index = attempt.index


class OutcomeReplayFault(ReplayFault):
    """A logged `effect_result` the outbox refused when this build replayed it: an outcome for an
    effect it never saw, one naming another run_id, or a second outcome for one effect
    (concurrency-model ss5). A `ReplayFault`, so resume stops with the same advice."""

    def __init__(self, outcome: EffectOutcome, cause: EngineError) -> None:
        EngineError.__init__(
            self,
            f"replay stopped at the {outcome.state} outcome of effect {outcome.effect_id}:"
            f" {type(cause).__name__}: {cause}",
            code=cause.code,
        )
        self.index = None


@dataclass(frozen=True)
class RecoveredAttempt:
    """One attempt admitted with no durable decision, as replay decided it through the gate
    (DL-156), with what a resuming engine needs to commit that verdict (DL-315): the events the
    batch emitted, and every job's run number just before and just after the attempt. The commit
    plans against these rows, not the rows the whole replay ends on: the ghost-run gate compares a
    run with the run before the attempt."""

    result: ApplyResult
    emitted: list[Event]
    runs_before: dict[str, int]
    runs_after: dict[str, int]


@dataclass
class Replay:
    """Where the log left the state machine (concurrency-model ss2/ss4)."""

    frontiers: Frontiers = field(default_factory=Frontiers)
    decisions: DecisionIndex = field(default_factory=DecisionIndex)
    #: attempts admitted with no durable result (the crash window), decided by re-running the
    #: gate
    recovered_attempts: list[RecoveredAttempt] = field(default_factory=list)
    #: the effects this log intended and what became of them (ss5), read from the records: an
    #: effect the previous engine decided is a fact, and re-planning it would let a changed
    #: planner disagree with the log about what was meant to happen
    outbox: Outbox = field(default_factory=Outbox)

    @property
    def recovered(self) -> list[ApplyResult]:
        """The recovered verdicts alone, in admission order."""
        return [attempt.result for attempt in self.recovered_attempts]


def replay_inputs(
    oracle: Oracle,
    records: list[dict[str, Any]],
    *,
    outbox: Outbox | None = None,
    machines: Executor | None = None,
) -> Replay:
    """Apply the journal through `oracle`, two-pass (concurrency-model ss4).

    `outbox` is the carry: a segment opened from a seal starts with C1's undelivered intents and
    applied bindings (`read_outbox`). `machines` is the executor the decision index runs on: a
    resuming engine's, or the index's own (`read_decisions`).

    Pass one indexes the decisions; pass two applies each attempt in admission order. A durable
    decision is authoritative: a rejected attempt is not fed, and an applied one is fed without
    re-deciding, so this build reproduces the log's history and not the one its current gate would
    write. An attempt with no result is applied, because admission is the commit point, and goes
    through the gate on the way, since a decision is what it lacks. It is applied on a fork
    first, as the live engine applies an engine-made input. If that raises and its time half
    applies alone, the verdict is the quarantine the live engine would have logged: rejected,
    `engine_apply_faulted` (concurrency-model ss4). Replay writes nothing: it reports each such
    attempt in `recovered_attempts`, and a resuming engine writes the missing decision (DL-315).

    The time half of every batch applies either way. A rejected completion still observed the
    clock, and the kill that observation let fire is a decision the estate already acted on;
    skipping it would resurrect a killed job (DL-44's advance record).

    An input that raises stops the replay with a `ReplayFault` that names it; a `StatechartError`
    stops it as itself, naming the input."""
    decisions = read_decisions(records, machines)
    # I2: indices are monotone across the estate, not the segment, so a segment that opens at
    # 5311 replays from 5310, the number its own opening record carries. It is derived from the
    # record, since a caller-supplied base would be a second authority for `first_index`. The
    # `else 0` is defensive: every segment on disk carries `first_index` >= 1, but this function
    # is public and harnesses hand it unvalidated records.
    first = records[0].get("first_index")
    base = int(first) - 1 if is_wire_int(first) else 0
    replay = Replay(
        decisions=decisions,
        outbox=read_outbox(records, outbox),
        frontiers=Frontiers(committed_index=base, applied_index=base),
    )
    for attempt in read_attempts(records):
        durable = decisions.for_index(attempt.index)
        runs_before = _run_numbers(oracle) if durable is None else {}
        forced = None
        if durable is None:
            # the crash window: on forks first, so a fault leaves this oracle as the input found
            # it, and is classified (ss4)
            try:
                quarantine = classify_fault(oracle, attempt)
            except StatechartError as exc:
                raise _at_input(exc, attempt)
            except TimeHalfFault as fault:
                # the time half's own fault, which may not be the first: the whole apply's rides
                # on as a note
                stop = ReplayFault(attempt, fault.cause)
                if fault.note is not None:
                    stop.add_note(fault.note)
                raise stop from fault.cause
            forced = None if quarantine is None else quarantine[0]
        try:
            applied = apply_attempt(oracle, attempt, decided=durable, forced=forced)
        except StatechartError as exc:
            # an assertion failure stops replay as itself, naming the input
            raise _at_input(exc, attempt)
        except Exception as exc:
            # never skipped: an oracle bug raises again on the input the recovery ladder would
            # re-derive, so the error names the input
            raise ReplayFault(attempt, exc) from exc
        if durable is None:
            try:
                decisions.record(applied.result)
            except StatechartError as exc:
                raise _at_input(exc, attempt)
            replay.recovered_attempts.append(
                RecoveredAttempt(
                    result=applied.result,
                    emitted=applied.emitted,
                    runs_before=runs_before,
                    runs_after=_run_numbers(oracle),
                )
            )
        replay.frontiers = replay.frontiers.admit(attempt.at).record(attempt.index)
    return replay


def _at_input(exc: StatechartError, attempt: Attempt) -> StatechartError:
    """A statechart error met in replay, with the WAL input it met added to its
    report, as a `ReplayFault` names the input (period-model ss11)."""
    exc.facts.append(("WAL input", f"{attempt.index}: {describe_input(attempt)}"))
    return exc


def _run_numbers(oracle: Oracle) -> dict[str, int]:
    """Every job's run number as the oracle holds it now."""
    return {job: rt.run_number for job, rt in oracle.store.job.items()}


def replay_period(
    oracle: Oracle, records: list[dict[str, Any]], *, outbox: Outbox | None = None
) -> Replay:
    """Replay one period's log offline onto `oracle`, built for the period
    (`period.oracle_reading`) with the rows it opened with: this engine's executor is seeded at
    the opening instant, then `replay_inputs` runs.

    Reproducing a log reproduces the genesis it was replayed onto, not only the catalog: a
    routing-table input lands on a table that already holds this engine's executor
    (concurrency-model ss8), and a replay without it would decide "no such host" where the run
    decided otherwise. The stamp reaches only `last_contact`, which no input reads."""
    seed_local_executor(oracle.store, LOCAL_EXECUTOR_ID, at=opening_at(records[0]))
    return replay_inputs(oracle, records, outbox=outbox)


def scheduler_frontier(records: list[dict[str, Any]]) -> datetime:
    """ss6: the scheduler's durable frontier is semantic, not the newest timestamp in the file.

    `last_journal_at` takes the maximum `at` over every record, `leader` and `dispatch` included.
    If T is 02:00, C2 opens at 02:10 and appends `leader.at = 02:10`, and the process dies before
    the missed-tick sweep, the next resume would anchor at 02:10 and neither admit nor drop a
    02:05 tick. The frontier is the opening watermark, the admitted scheduler ticks, the `drop`
    records and the `advance` records, and nothing else (PR-25a)."""
    stamps = [opening_at(records[0])]
    for record in records:
        rec = record.get("rec")
        semantic = rec in ("drop", "advance") or (
            rec == "input" and record.get("source") == "scheduler"
        )
        if semantic and isinstance(record.get("at"), str):
            stamps.append(datetime.fromisoformat(record["at"]))
    return max(stamps)


def last_journal_at(records: list[dict[str, Any]]) -> datetime:
    """max time the journal proves the run reached (ss7 'last journal at')."""
    stamps = [opening_at(records[0])]
    for record in records:
        for key in ("at", "started_at"):
            if key in record:
                stamps.append(datetime.fromisoformat(record[key]))
    return max(stamps)
