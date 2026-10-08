"""Execution hosts: the routing table, its four states, and eviction's gate.

Normative spec: docs/concurrency-model.md ss8 (host lifecycle: active,
passive, quarantined, evicted), ss7 (quarantine and the takeover barrier),
ss2 (identity). Stage S5a. `ssN` in this module always names
concurrency-model; bare `ssN` elsewhere in the runner means
docs/runner-design.md, and both are normative here.

ss8 exists because ss7's quarantine is safe and is not sufficient on its
own: one dead host would hold its jobs forever. The answer is an explicit
per-host routing state the operator owns, durable so that a failover does
not undo a drain -- which is what putting it under the ss3 state owner buys
(DL-93), rather than a side table with a revision counter of its own.

Three things live here and the fourth deliberately does not:

- **The command.** `HostCommand` is an admitted input that carries no
  oracle event. It is not an `EventKind` and never will be: a job's
  condition truth cannot depend on where its machine routes.
- **The table.** `HOST` is ss8's routing table as a state machine
  (DL-289), and `host_move` is its one reader: per-state handlers choose
  the transition a verb takes, or the rejection that stops it. The gate
  (`host_rejection_reason`) and the apply (`apply_host_command`) both ask
  it, so the guard, the dispatch and the no-op rules are one place. The
  guards are a PURE function of the row and the input's own facts. Pure is
  not tidiness: replay has no live host to probe, and a gate that consulted
  one would decide differently the second time and make the log a record
  of nothing.
- **The routing predicate.** `routes_new_effects` is ss8's table's second
  column, as one line, so no caller re-derives which states dispatch.
- NOT the outbox. What a host is RUNNING is S5c's, keyed by `effect_id`
  bound to `executor_id`; a second durable record of "did this run start"
  would be the parallel model DL-91 exists to catch.

**One host, for now.** This engine's own executor is seeded into the table
at genesis and every job routes to it: machine names resolve to a relay
through this table (ss5), and there is no relay (DL-97 records why, and its
trigger), so a resolution step here would be an indirection with one
possible answer. What S5a made real is the STATE -- draining that one host
holds new work and lets running work finish, which is CM-13 and is the
operation an operator actually performs before maintenance.

**Every input to the ss8 bound is now produced, not assumed.** `deadman_s`
is what the supervisor reports it runs and `last_contact` is stamped by the
lease exchange (S5b); `quarantined` is set when the leader's lease renewal
fails five times in a row (DL-291) and cleared when it answers again (S5d),
remembering the state it interrupted so a blip cannot undo a drain. What is
still refused rather than automated is the return of an EVICTED host: it
must re-register at its new generation and self-fence first, and
self-fencing is the relay's act.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict

from dsl41.oracle_state import HostRuntime, HostState, RuntimeState
from dsl41.runner_clock import EngineError
from dsl41.runner_codes import Rejection
from dsl41.state_machine import StateMachine, Transition

#: The id of the executor an engine runs on its own machine. One engine per
#: run root owns exactly one local executor (control-protocol ss2's socket
#: probe is what enforces the "one"), so this is a constant rather than a
#: configured name until S5d gives remote relays ids of their own.
LOCAL_EXECUTOR_ID = "local"

#: ss8's verbs, in two sets that are deliberately not one.
#:
#: `HOST_VERBS` is the OPERATOR's, and it is what the wire accepts: an
#: operator asserts intent about routing. `quarantine` and `reinstate` are
#: the LEADER's, set automatically from what it can and cannot reach, and
#: they arrive through the engine's own door (`Engine.inject_host`) with no
#: `expect` -- ss0's mandate is on externally requested mutations, and an
#: observation the leader makes about reachability is not one. Giving an
#: operator a verb for quarantine would blur it with `drain`, which asserts
#: nothing about whether the host is answering.
HostVerb = Literal["activate", "drain", "evict", "quarantine", "reinstate"]
HOST_VERBS: frozenset[str] = frozenset(("activate", "drain", "evict"))
#: DERIVED, not listed again (DL-98): the two sets partition the verb type,
#: and three hand-kept spellings of one vocabulary is three places for a new
#: verb to be forgotten. Deriving also fails SAFE -- a verb added to the type
#: and not to `HOST_VERBS` is a leader verb, so it is not reachable from the
#: wire until someone says it should be.
LEADER_VERBS: frozenset[str] = frozenset(get_args(HostVerb)) - HOST_VERBS

#: What `kill_allowance` adds on top of the two graces: the supervisor's own
#: exit, after its last wrapper is gone. Deliberately generous -- it is added
#: to a wait an operator can always skip with `--force`, so erring long costs
#: patience and erring short costs a double run.
KILL_MARGIN_S = 10.0


def kill_allowance(grace_s: float) -> float:
    """ss8's `T_kill` over the command grace the PERIOD runs: how long after
    a deadman fires until the host's wrappers are certainly gone.

    The supervisor's exit EOFs every lifeline; each wrapper then sends TERM
    to its command pgid, waits the run's `grace_seconds`, sends KILL,
    records and exits (supervisor-protocol ss4 step 5). Two graces -- the
    wrapper's own TERM wait and the supervisor's wait for its wrappers --
    plus the margin above.

    DERIVED, not fixed (DL-151). `RuntimeProfile.cmd_grace_us` is per period
    and unbounded above, and the wrapper waits the real value: a constant
    30 s left eviction permitted while a 60 s-grace command was still inside
    its TERM window, which is the double run this bound exists to prevent.
    At the 10 s default this is 30 s, so a default estate's bound is
    unchanged."""
    return 2.0 * grace_s + KILL_MARGIN_S


#: ss8's `T_skew`, which covers monotonic-clock DRIFT between hosts, not
#: clock synchronization -- the standard lease argument, which depends only
#: on the drift being bounded. Expressed as a fraction of the interval
#: because that is what drift is; 200 ppm is loose for any real oscillator
#: (a cheap uncompensated crystal is ~50 ppm) and still vanishing next to
#: the floor, which is what actually absorbs scheduling jitter on a busy
#: leader.
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
    #: ss8: skips the eviction preconditions. Attributed, durable, and the
    #: one path in the whole concurrency model that can produce a double
    #: run. Meaningless on the other two verbs, which assert nothing.
    force: bool = False

    @property
    def key(self) -> str:
        """The ss6 entity this command addresses -- the only key its
        `expect` may name."""
        return RuntimeState.host_key(self.host_id)

    def wire(self) -> dict[str, Any]:
        """The command as it goes on the wire and into the log. `id` rather
        than `host_id` because the request's `payload` says `id`, and one
        spelling across the transport and the WAL is one fewer place for the
        two to disagree."""
        return {"verb": self.verb, "id": self.host_id, "force": self.force}

    @classmethod
    def from_wire(cls, wire: dict[str, Any]) -> HostCommand:
        return cls(verb=wire["verb"], host_id=wire["id"], force=bool(wire.get("force", False)))


def routes_new_effects(row: HostRuntime | None) -> bool:
    """ss8's "new effects routed" column, as one predicate.

    `active` alone routes. A host with NO row routes nothing either: a host
    the table does not know is a host the ss7 takeover barrier would never
    reconcile, and dispatching to one would put work somewhere no failover
    could find it."""
    return row is not None and row.state == "active"


def seed_local_executor(
    store: RuntimeState, host_id: str, *, at: datetime, deadman_s: float | None = None
) -> None:
    """Put this engine's own executor in the routing table, at genesis.

    Registration rather than an admitted input, for the reason the catalog
    seed is not one either (`Oracle.__init__`): the row's EXISTENCE is a
    fact about how this engine was launched, identical on every replay of
    the same run root, so admitting it would spend a log index per start
    recording something every reader of that log already knows -- and would
    renumber every index in every existing journal.

    What ss8 requires to survive a failover is the STATE, and that is
    admitted input: this seed only ever creates the row at its default
    `active`, and a replayed drain lands on top of it. `register_host` keeps
    an existing row's state for exactly that reason."""
    store.begin_input()
    try:
        store.register_host(host_id, deadman_s=deadman_s, at=at)
    finally:
        store.commit_input()


# ------------------------------------------------------- the routing table

#: The two routing states that route or drain. `quarantine` and `--force`
#: leave either of them, and `reinstate` returns to the one it interrupted.
ROUTED: frozenset[HostState] = frozenset({"active", "passive"})

HOST_ACTIVATE_ACTIVE: Final = Transition[HostState](
    "host.01",
    frozenset({"active"}),
    "activate",
    "active",
    effect="none: the row does not move and no revision moves",
    cite="concurrency-model ss8",
)
HOST_DRAIN: Final = Transition[HostState](
    "host.02",
    frozenset({"active"}),
    "drain",
    "passive",
    effect="new effects are held; running work continues",
    cite="concurrency-model ss8, CM-13",
)
HOST_ACTIVATE: Final = Transition[HostState](
    "host.03",
    frozenset({"passive"}),
    "activate",
    "active",
    effect="held starts dispatch again",
    cite="concurrency-model ss8",
)
HOST_DRAIN_PASSIVE: Final = Transition[HostState](
    "host.04",
    frozenset({"passive"}),
    "drain",
    "passive",
    effect="none: the row does not move and no revision moves",
    cite="concurrency-model ss8",
)
HOST_QUARANTINE: Final = Transition[HostState](
    "host.05",
    ROUTED,
    "quarantine",
    "quarantined",
    guard="the leader's renewal failed five times in a row",
    effect="remember the state it interrupts",
    cite="concurrency-model ss7, ss8, DL-97, DL-291",
)
HOST_QUARANTINE_AGAIN: Final = Transition[HostState](
    "host.06",
    frozenset({"quarantined"}),
    "quarantine",
    "quarantined",
    effect="none: repeated unreachability is one fact",
    cite="DL-97",
)
HOST_REINSTATE: Final = Transition[HostState](
    "host.07",
    frozenset({"quarantined"}),
    "reinstate",
    ROUTED,
    guard="the host answered again",
    effect="put back the state quarantine interrupted, and forget it",
    cite="concurrency-model ss8, DL-97",
)
HOST_REINSTATE_ACTIVE: Final = Transition[HostState](
    "host.08",
    frozenset({"active"}),
    "reinstate",
    "active",
    effect="none: nothing to put back",
    cite="concurrency-model ss8, DL-111",
)
HOST_REINSTATE_PASSIVE: Final = Transition[HostState](
    "host.09",
    frozenset({"passive"}),
    "reinstate",
    "passive",
    effect="none: nothing to put back",
    cite="concurrency-model ss8, DL-111",
)
HOST_EVICT: Final = Transition[HostState](
    "host.10",
    frozenset({"quarantined"}),
    "evict",
    "evicted",
    guard="the host runs a deadman, was in contact, and has been silent past deadman + kill + skew",
    effect="generation += 1; forced_by = none; forget the interrupted state",
    cite="concurrency-model ss8 preconditions 1-3, DL-151",
)
HOST_EVICT_FORCED: Final = Transition[HostState](
    "host.11",
    frozenset({"active", "passive", "quarantined"}),
    "evict (force)",
    "evicted",
    guard="claimed_actor names who asks",
    effect="generation += 1; forced_by = the actor; forget the interrupted state",
    cite="concurrency-model ss8, DL-111, DL-151",
)
HOST_REREGISTER: Final = Transition[HostState](
    "host.12",
    frozenset({"evicted"}),
    "register",
    "active",
    guard="the relay presents the current generation and has self-fenced",
    effect="the relay's held jobs run as new runs with new effect ids",
    cite="concurrency-model ss8 (eviction is fenced on return), period-model ss3.3",
    mark="spec-only",
)

#: concurrency-model ss8's routing table, as UML transitions. A row is born
#: `active` by registration (`register_host`), which runs outside any input
#: (`seed_local_executor`), so registration takes no transition: `active` is
#: the initial state. `touch_host` refreshes `last_contact` in any state; it
#: is extended state outside the semantic projection, not a move. A gate
#: rejection (`host_quarantined`, `host_evicted`, ...) is a decision that
#: moves nothing, so it is not a transition either. `evicted` has one way
#: out, the relay's re-registration, which no code has yet.
HOST: Final[StateMachine[HostState]] = StateMachine(
    name="host",
    states=frozenset(get_args(HostState)),
    initial="active",
    finals=frozenset(),
    transitions=(
        HOST_ACTIVATE_ACTIVE,
        HOST_DRAIN,
        HOST_ACTIVATE,
        HOST_DRAIN_PASSIVE,
        HOST_QUARANTINE,
        HOST_QUARANTINE_AGAIN,
        HOST_REINSTATE,
        HOST_REINSTATE_ACTIVE,
        HOST_REINSTATE_PASSIVE,
        HOST_EVICT,
        HOST_EVICT_FORCED,
        HOST_REREGISTER,
    ),
)

#: What a host command does to its row: a declared transition, or the gate's
#: rejection, which moves nothing.
type HostMove = Transition[HostState] | Rejection


@dataclass(frozen=True)
class HostGate:
    """The input's own facts that ss8's guards read: its stamp, the period's
    command grace and the claimed actor. All are journaled with the input
    or fixed for the period, so replay reaches the same verdict."""

    at: datetime
    grace_s: float
    actor: str | None


def host_move(row: HostRuntime, cmd: HostCommand, gate: HostGate | None) -> HostMove:
    """The transition `cmd` takes from `row`, or the rejection that stops it.

    One handler per state, looked up from the row's stored state (a closure
    as the state would not survive the WAL or a seal). `gate` None skips
    the guards that read the input's stamp and actor: the apply half runs
    after the gate has decided, and a replayed DURABLE decision must not be
    re-decided against a host row the replay seeded itself
    (`apply_attempt`'s docstring). The structural answer -- which
    transition a verb takes from a state -- never depends on `gate`."""
    match row.state:
        case "active" | "passive":
            return _routed(row, cmd, gate)
        case "quarantined":
            return _quarantined(row, cmd, gate)
        case _:  # evicted
            return _evicted(row, cmd)


def _routed(row: HostRuntime, cmd: HostCommand, gate: HostGate | None) -> HostMove:
    active = row.state == "active"
    match cmd.verb:
        case "activate":
            return HOST_ACTIVATE_ACTIVE if active else HOST_ACTIVATE
        case "drain":
            return HOST_DRAIN if active else HOST_DRAIN_PASSIVE
        case "quarantine":
            return HOST_QUARANTINE
        case "reinstate":
            # concurrency-model ss8 (DL-111): the leader reinstates a host it
            # did not quarantine when the lease answers; nothing to put back
            return HOST_REINSTATE_ACTIVE if active else HOST_REINSTATE_PASSIVE
        case _:  # evict
            if cmd.force:
                return _forced(cmd, gate) or HOST_EVICT_FORCED
            return Rejection(
                "host_not_quarantined",
                f"host {cmd.host_id!r} is {row.state}: eviction needs the leader's own durable"
                " record that the host is unreachable (ss8 precondition 1), which is the"
                " `quarantined` state. Draining does not assert that, and out-of-band"
                " knowledge that the machine is dead is what --force is for",
            )


def _quarantined(row: HostRuntime, cmd: HostCommand, gate: HostGate | None) -> HostMove:
    match cmd.verb:
        case "activate" | "drain":
            return Rejection(
                "host_quarantined",
                f"host {cmd.host_id!r} is quarantined: the leader set that because the host"
                " stopped answering and clears it when the host answers again, holding its"
                " jobs meanwhile (ss7). An operator state change would not make it reachable",
            )
        case "quarantine":
            return HOST_QUARANTINE_AGAIN
        case "reinstate":
            return HOST_REINSTATE
        case _:  # evict
            if cmd.force:
                return _forced(cmd, gate) or HOST_EVICT_FORCED
            return _bound(row, cmd, gate) or HOST_EVICT


def _evicted(row: HostRuntime, cmd: HostCommand) -> HostMove:
    """Every verb is refused. Eviction is a decision about the host's work,
    and it returns only by re-registering at its new generation and
    self-fencing (ss8), which is the relay's act (`HOST_REREGISTER`)."""
    if cmd.verb == "evict":
        return Rejection(
            "host_already_evicted",
            f"host {cmd.host_id!r} is already evicted, at generation {row.generation}",
        )
    if cmd.verb in LEADER_VERBS:
        # the leader's own observation: a leader that quietly un-evicted a
        # host by reaching it again would undo the one state an operator
        # took deliberately
        return Rejection(
            "host_evicted",
            f"host {cmd.host_id!r} was evicted at generation {row.generation}:"
            " reaching it again does not un-evict it, and it may not be routed"
            " to until it re-registers at that generation and self-fences (ss8)",
        )
    return Rejection(
        "host_evicted",
        f"host {cmd.host_id!r} was evicted at generation {row.generation}: it returns"
        " by re-registering at that generation and self-fencing first, not by an"
        " operator state change (ss8)",
    )


def _forced(cmd: HostCommand, gate: HostGate | None) -> Rejection | None:
    """`host.11`'s guard. ss8: attributed, not forbidden -- and "attributed"
    is the WHOLE of force's safety story, so a force that names nobody has
    none. The row's `forced_by` is non-null iff the state was reached by
    force, and an unattributed one reads exactly like a proof-gated
    eviction. Checked in the batch rather than at the door so the perimeter
    has already had its chance to stamp an authenticated principal
    (DL-146)."""
    if gate is None or (gate.actor is not None and gate.actor.strip()):
        return None
    return Rejection(
        "force_needs_actor",
        f"host {cmd.host_id!r}: --force skips the ss8 preconditions, so the"
        " record is the only safety story it has -- name who is asking in"
        " `claimed_actor`. An unattributed force is refused",
    )


def _bound(row: HostRuntime, cmd: HostCommand, gate: HostGate | None) -> Rejection | None:
    """`host.10`'s guard: ss8's preconditions 2 and 3 (precondition 1 is the
    `quarantined` state this runs in), in the order that costs least to
    explain. A rejection reports the remaining wait, so the operator waits
    rather than guesses."""
    if gate is None:
        return None
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
    kill_s = kill_allowance(gate.grace_s)
    bound = row.deadman_s + kill_s
    bound += skew_allowance(bound)
    waited = (gate.at - row.last_contact).total_seconds()
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
    """ss8's preconditions: why this command must not apply, or None. The
    gate half of `host_move`.

    A REJECTION, not a refusal (control-protocol ss3). Every check reads
    mutable state, so it can only be made where `expect` is made -- inside
    the input's own batch, in log order. Deciding at the door would answer
    against a table that had moved by the time the command applied, and
    would leave the log holding a verdict replay could not reproduce.

    `grace_s` is the command grace the PERIOD runs, which is what the kill
    half of the bound is made of, and it is REQUIRED (DL-152): a default
    here was the ss2.1 number, so a caller that forgot to pass the period's
    grace got a bound for a different estate and nothing said so. `actor` is
    the attempt's `claimed_actor`, which force needs and the other verbs do
    not. Both are the input's own facts -- journaled with it -- so replay
    reaches this verdict from the same values."""
    row = store.host(cmd.host_id)
    if row is None:
        return Rejection(
            "unknown_host",
            f"no host {cmd.host_id!r} in the routing table: a host joins it by"
            " registering, never by being addressed",
        )
    move = host_move(row, cmd, HostGate(at=at, grace_s=grace_s, actor=actor))
    return move if isinstance(move, Rejection) else None


def apply_host_command(store: RuntimeState, cmd: HostCommand, *, actor: str | None) -> None:
    """Apply one decided host command: take its transition and run its
    effect through the store's verbs (DL-86). Called inside the input's
    batch, so the row takes that input's single revision like any other
    entity (ss3), and the check's result goes on the batch's violation
    channel under the row's `host:` key (DL-292).

    The decision is made: the gate passed, or a replayed durable decision
    says so. A command the table has no transition for here was not decided
    by this state machine, so it raises -- live that is unreachable behind
    the gate, and on replay it stops resume naming the input (period-model
    ss11)."""
    row = store.host(cmd.host_id)
    move = Rejection("unknown_host", "") if row is None else host_move(row, cmd, None)
    if isinstance(move, Rejection):
        raise EngineError(
            f"host {cmd.verb} on {cmd.host_id!r} is decided applied, but the routing table"
            f" has no transition for it ({move.code}): this build is not the state machine"
            " that decided it (concurrency-model ss8)"
        )
    assert row is not None  # a transition is chosen only for a row
    if move is HOST_QUARANTINE:
        store.quarantine_host(cmd.host_id)
    elif move is HOST_REINSTATE:
        store.reinstate_host(cmd.host_id)
    elif move is HOST_EVICT or move is HOST_EVICT_FORCED:
        # only a FORCED eviction records an actor: on the gated path the
        # preconditions are the justification, and naming a principal beside
        # them would suggest the eviction rested on who asked for it
        store.evict_host(cmd.host_id, forced_by=actor if move is HOST_EVICT_FORCED else None)
    elif move is HOST_DRAIN or move is HOST_ACTIVATE:
        store.set_host_state(cmd.host_id, "passive" if move is HOST_DRAIN else "active")
    # every other transition is internal: the row does not move
    after = store.hosts[cmd.host_id].state
    store.note_violation(RuntimeState.host_key(cmd.host_id), HOST.take(move, row.state, after))
