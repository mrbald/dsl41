"""Execution hosts: the routing table, its four states, and eviction's gate.

Normative spec: docs/concurrency-model.md ss8 (host lifecycle: active, passive, quarantined,
evicted), ss7 (quarantine and the takeover barrier), ss2 (identity). `ssN` in this module names
concurrency-model; bare `ssN` elsewhere in the runner means docs/runner-design.md.

The routing state is operator-owned and durable, held under the ss3 state owner so a failover
does not undo a drain (DL-93).

- `HostCommand` is an admitted input that carries no oracle event. It is not an `EventKind`: a
  job's condition truth cannot depend on where its machine routes.
- `HOST` is ss8's routing table as a statechart, one instance per host on the store's executor,
  declared beside the row it projects into (`oracle_state`). The chart decides every move; a cell
  with no move is a refusal row with its code. The gate (`host_rejection_reason`) reads it with
  the engine's selection read (`host_move`) and adds the rules that read the input's own facts:
  the eviction bound and the attribution of a force. `apply_host_command` dispatches an applied
  command and carries out the intents its rows and entries report. Both halves are pure
  functions of the row and the input's own facts, because replay has no live host to probe.
- `routes_new_effects` is ss8's "new effects routed" column as one predicate.
- What a host is running is the outbox's (S5c), keyed by `effect_id` bound to `executor_id`; a
  second record of "did this run start" would be the parallel model DL-91 exists to catch.

One host: this engine's own executor is seeded into the table at genesis and every job routes to
it. Machine names resolve to a relay through this table (ss5) and there is no relay (DL-97), so
a resolution step would have one possible answer. Draining that host (S5a) holds new work and
lets running work finish (CM-13).

Every input to the ss8 bound is produced: `deadman_s` is what the supervisor reports it runs and
`last_contact` is stamped by the lease exchange (S5b); `quarantined` is set when the leader's
lease renewal fails five times in a row (DL-291) and cleared when it answers again (S5d),
remembering the state it interrupted so a blip cannot undo a drain. The return of an evicted
host is refused: it must re-register at its new generation and self-fence first, and
self-fencing is the relay's act.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict

from dsl41.oracle_state import (
    FORCE_EVICT,
    HostEntered,
    HostEvicted,
    HostRemembered,
    HostRuntime,
    RuntimeState,
)
from dsl41.runner_codes import Rejection
from dsl41.state_machine import Event as ChartEvent
from dsl41.state_machine import Outcome, StatechartError

#: The id of the engine's own executor. One engine per run root owns one local executor
#: (control-protocol ss2's socket probe enforces it), so the id is a constant.
LOCAL_EXECUTOR_ID = "local"

#: ss8's verbs, in two sets. `HOST_VERBS` is the operator's and what the wire accepts.
#: `quarantine` and `reinstate` are the leader's, set from what it can reach; they arrive through
#: `Engine.inject_host` with no `expect`, because ss0's mandate covers externally requested
#: mutations and the leader's reachability observation is not one. `drain` asserts nothing about
#: whether the host answers, so an operator has no quarantine verb.
HostVerb = Literal["activate", "drain", "evict", "quarantine", "reinstate"]
HOST_VERBS: frozenset[str] = frozenset(("activate", "drain", "evict"))
#: Derived from the verb type (DL-98): a verb added to the type and not to `HOST_VERBS` is a
#: leader verb, unreachable from the wire until it is listed.
LEADER_VERBS: frozenset[str] = frozenset(get_args(HostVerb)) - HOST_VERBS

#: What `kill_allowance` adds to the two graces: the supervisor's own exit after its last wrapper.
#: Generous on purpose: the wait can be skipped with `--force`, and a short one costs a double run.
KILL_MARGIN_S = 10.0


def kill_allowance(grace_s: float) -> float:
    """ss8's `T_kill` over the period's command grace: two graces (the wrapper's TERM wait and
    the supervisor's wait for its wrappers) plus `KILL_MARGIN_S`.

    It is derived from the period's `cmd_grace_us`, not fixed (DL-151): the wrapper waits the
    real value, and a constant would permit eviction inside a long grace."""
    return 2.0 * grace_s + KILL_MARGIN_S


#: ss8's `T_skew` covers monotonic-clock drift between hosts, not clock synchronization. Drift is
#: a fraction of the interval; 200 ppm is loose for any real oscillator, and the floor absorbs
#: scheduling jitter on a busy leader.
SKEW_PPM = 200.0
SKEW_FLOOR_S = 1.0


def skew_allowance(interval_s: float) -> float:
    """ss8's `T_skew` over an interval: drift, plus a floor for jitter."""
    return max(SKEW_FLOOR_S, interval_s * SKEW_PPM / 1_000_000.0)


class HostCommand(BaseModel):
    """One externally requested change to the routing table.

    An admitted input like any other -- it takes an index, it is journaled,
    it names the revision it was composed against -- but it carries no
    oracle event, so it rides the seam ss4 already had for an attempt with
    no verb (DL-93). `force` is on the command rather than being a fourth
    verb because it is the same act with its proof waived, and ss8 wants
    that visible as one thing in the log."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verb: HostVerb
    host_id: str
    #: ss8: skips the eviction preconditions. Attributed and durable; the one path that can
    #: produce a double run. Meaningless on the other verbs.
    force: bool = False

    @property
    def key(self) -> str:
        """The ss6 entity this command addresses; the only key its `expect` may name."""
        return RuntimeState.host_key(self.host_id)

    def wire(self) -> dict[str, Any]:
        """The command as it goes on the wire and into the log, with `id` as the request's
        `payload` spells it."""
        return {"verb": self.verb, "id": self.host_id, "force": self.force}

    @classmethod
    def from_wire(cls, wire: dict[str, Any]) -> HostCommand:
        return cls(verb=wire["verb"], host_id=wire["id"], force=bool(wire.get("force", False)))


def routes_new_effects(row: HostRuntime | None) -> bool:
    """ss8's "new effects routed" column: only `active` routes, and a host with no row routes
    nothing, because the ss7 takeover barrier would never reconcile it."""
    return row is not None and row.state == "active"


def seed_local_executor(
    store: RuntimeState, host_id: str, *, at: datetime, deadman_s: float | None = None
) -> None:
    """Put this engine's own executor in the routing table, at genesis.

    This is registration, not an admitted input, as the catalog seed is (`Oracle.__init__`): the
    row's existence is identical on every replay of the run root. The seed creates the row at
    `active`; a replayed drain lands on top of it, and `register_host` keeps an existing row's
    state."""
    store.begin_input()
    try:
        store.register_host(host_id, deadman_s=deadman_s, at=at)
    finally:
        store.commit_input()


# ------------------------------------------------------- the routing table

#: The rows whose firing needs the input's own facts (ss8): the gated eviction's bound and the
#: forced one's attribution. No chart guard reads them: a rejection replays as stored and
#: `last_contact` is re-seeded fresher at resume, so the gate decides once (DL-94).
GATED_EVICTION: Final = "host.10"
FORCED_EVICTION: Final = "host.11"


def host_event(cmd: HostCommand, *, actor: str | None) -> ChartEvent:
    """The chart's event for a host command: its verb, or `force_evict` for `evict --force`.
    Its payload is the input's `claimed_actor`, which a forced eviction records (host.11); the
    gate's read runs no action and passes None."""
    return ChartEvent(FORCE_EVICT if cmd.verb == "evict" and cmd.force else cmd.verb, actor)


def host_move(store: RuntimeState, cmd: HostCommand) -> Outcome:
    """What `cmd` would do to its host's instance, with nothing moved. The host must have a row.

    It is `Executor.resolve` on the store's instance: selection with no action and no move
    (statechart-engine ss5, DL-335), because the gate also runs where no dry apply precedes it.
    The read names the rows and refusals a dispatch would fire, and no choice branch (DL-375).
    It runs no action, so it reports no intent and nothing reaches the row.

    It reads the instance, not the row, so it agrees with a dispatch only while every dispatch
    is projected into the row: the apply carries out the leaf's write, and `_replace_host`
    refuses a state the instance is not in. A call from inside a macrostep of the store's
    executor raises `ReentrantDispatch`, and a host with no instance raises a
    `StatechartError`; the gate answers `unknown_host` for a host with no row before it reads."""
    return store.machines.resolve(cmd.key, host_event(cmd, actor=None))


def _refused(code: str, row: HostRuntime, cmd: HostCommand) -> Rejection:
    """The gate's sentence for a refusal row's code, each code by name."""
    match code:
        case "host_not_quarantined":
            return Rejection(
                code,
                f"host {cmd.host_id!r} is {row.state}: eviction needs the leader's own durable"
                " record that the host is unreachable (ss8 precondition 1), which is the"
                " `quarantined` state. Draining does not assert that, and out-of-band"
                " knowledge that the machine is dead is what --force is for",
            )
        case "host_quarantined":
            return Rejection(
                code,
                f"host {cmd.host_id!r} is quarantined: the leader set that because the host"
                " stopped answering and clears it when the host answers again, holding its"
                " jobs meanwhile (ss7). An operator state change would not make it reachable",
            )
        case "host_already_evicted":
            return Rejection(
                code,
                f"host {cmd.host_id!r} is already evicted, at generation {row.generation}",
            )
        case "host_evicted" if cmd.verb in LEADER_VERBS:
            # reaching an evicted host must not un-evict it: eviction is the one state an
            # operator takes deliberately
            return Rejection(
                code,
                f"host {cmd.host_id!r} was evicted at generation {row.generation}:"
                " reaching it again does not un-evict it, and it may not be routed"
                " to until it re-registers at that generation and self-fences (ss8)",
            )
        case "host_evicted":
            return Rejection(
                code,
                f"host {cmd.host_id!r} was evicted at generation {row.generation}: it returns"
                " by re-registering at that generation and self-fencing first, not by an"
                " operator state change (ss8)",
            )
    # a code the chart emits and the gate lacks is a bug, never a rejection
    raise StatechartError(f"host: the refusal code {code!r} has no sentence in the gate")


def _forced(cmd: HostCommand, actor: str | None) -> Rejection | None:
    """`host.11`'s attribution: a force that names nobody is refused (ss8). `forced_by` is
    non-null iff the state was reached by force. It is checked in the batch, after the perimeter
    may have stamped an authenticated principal (DL-146)."""
    if actor is not None and actor.strip():
        return None
    return Rejection(
        "force_needs_actor",
        f"host {cmd.host_id!r}: --force skips the ss8 preconditions, so the"
        " record is the only safety story it has -- name who is asking in"
        " `claimed_actor`. An unattributed force is refused",
    )


def _bound(row: HostRuntime, cmd: HostCommand, at: datetime, grace_s: float) -> Rejection | None:
    """`host.10`'s proof: ss8's preconditions 2 and 3 (precondition 1 is the `quarantined` state
    the chart takes it from). A rejection reports the remaining wait."""
    if row.deadman_s is None:
        return Rejection(
            "host_no_deadman",
            f"host {cmd.host_id!r} runs no deadman (ss8 precondition 2): nothing bounds"
            " when its wrappers die, so no wait can prove its work is safe to reroute."
            " --force is the only eviction such a host can have, and it is recorded as one",
        )
    if row.last_contact is None:
        return Rejection(
            "host_never_contacted",
            f"host {cmd.host_id!r} has never been in contact, so the ss8 bound has no"
            " start: nothing here can say how long its wrappers have had to die",
        )
    kill_s = kill_allowance(grace_s)
    bound = row.deadman_s + kill_s
    bound += skew_allowance(bound)
    waited = (at - row.last_contact).total_seconds()
    if waited <= bound:
        return Rejection(
            "eviction_bound_pending",
            f"host {cmd.host_id!r} was in contact {waited:.1f}s ago and the ss8 bound is"
            f" {bound:.1f}s (deadman {row.deadman_s:.1f}s + kill {kill_s:.1f}s + skew):"
            f" wait {bound - waited:.1f}s more, or --force with proof it is dead",
        )
    return None


def host_rejection_reason(
    store: RuntimeState,
    cmd: HostCommand,
    at: datetime,
    *,
    grace_s: float,
    actor: str | None = None,
) -> Rejection | None:
    """ss8's preconditions: why this command must not apply, or None.

    The chart's refusal code comes first; then, on host.10 and host.11 only, the checks that read
    the input's own facts. This is a rejection, not a refusal (control-protocol ss3): it is made
    inside the input's batch, in log order, where `expect` is made.

    `grace_s` is the period's command grace (period-model ss2.1) and is required (DL-152), so a
    caller cannot get a bound for a different estate. `actor` is the attempt's `claimed_actor`,
    which force needs. Both are journaled with the input, so replay reaches the same verdict."""
    row = store.host(cmd.host_id)
    if row is None:
        return Rejection(
            "unknown_host",
            f"no host {cmd.host_id!r} in the routing table: a host joins it by"
            " registering, never by being addressed",
        )
    move = host_move(store, cmd)
    if move.refusals:
        (code,) = move.refusals
        return _refused(code, row, cmd)
    if move.rows == (FORCED_EVICTION,):
        return _forced(cmd, actor)
    if move.rows == (GATED_EVICTION,):
        return _bound(row, cmd, at, grace_s)
    return None


def apply_host_command(store: RuntimeState, cmd: HostCommand, *, actor: str | None) -> None:
    """Apply one decided host command: dispatch its event, then carry out the intents of its
    outcome in the order the outcome reports them, through the store's verbs (DL-86), inside the
    input's batch (ss3). The intents follow the dispatch, so the instance has its new leaf when
    the row's state is written, and every guard and action read the row before any write.

    A refusal here or a host with no instance means this build is not the chart that decided it:
    a `StatechartError` that stops the tier and names the input on replay (statechart-engine ss5).
    """
    key = cmd.key
    if store.host(cmd.host_id) is None:
        error = StatechartError(
            f"host {cmd.verb} on {cmd.host_id!r} is decided applied, but no host instance"
            " holds it: this build is not the state machine that decided it"
            " (concurrency-model ss8)"
        )
        error.facts = [("machine", "host"), ("instance", repr(key)), ("active", "no instance")]
        raise error
    event = host_event(cmd, actor=actor)
    (outcome,) = store.machines.dispatch(key, event)
    if outcome.refusals:
        error = StatechartError(
            f"host {cmd.verb} on {cmd.host_id!r} is decided applied, but the chart refuses"
            f" it ({outcome.refusals[0]}): this build is not the state machine that"
            " decided it (concurrency-model ss8)",
            candidates=outcome.rows,
        )
        raise store.machines.explain(error, key, event)
    for intent in outcome.intents:
        match intent:
            case HostEntered(state):
                store.set_host_state(cmd.host_id, state)
            case HostRemembered(state):
                store.remember_host_state(cmd.host_id, state)
            case HostEvicted(forced_by):
                store.evict_host(cmd.host_id, forced_by=forced_by)
            case _:
                raise StatechartError(f"host: the intent {intent!r} has no verb in the apply")
