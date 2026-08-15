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
- **The gate.** `host_rejection_reason` is ss8's precondition set as a PURE
  function of the row and the input's own timestamp. Pure is not tidiness:
  replay has no live host to probe, and a gate that consulted one would
  decide differently the second time and make the log a record of nothing.
- **The routing predicate.** `routes_new_effects` is ss8's table's second
  column, as one line, so no caller re-derives which states dispatch.
- NOT the outbox. What a host is RUNNING is S5c's, keyed by `effect_id`
  bound to `executor_id`; a second durable record of "did this run start"
  would be the parallel model DL-91 exists to catch.

**One host, for now.** This engine's own executor is seeded into the table
at genesis and every job routes to it: machine names resolve to a relay
through this table (ss5), and there is no relay until S5d, so a resolution
step here would be an indirection with one possible answer. What S5a makes
real is the STATE -- draining that one host holds new work and lets running
work finish, which is CM-13 and is the operation an operator actually
performs before maintenance.

**What S5a cannot close, and does not pretend to.** `quarantined` has no
producer until S5d's unreachability detector, and nothing keeps
`last_contact` fresh until S5b's deadman. So `evict` in a real estate today
always rejects -- which is precisely CM-11's refusal half, and it is the
honest answer: a host nobody can prove dead may not have its work rerouted.
The permitted side of that bound is written and unit-tested here over rows
the tests build directly, and gets its live path when the two producers
land.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from dsl41.oracle_state import HostRuntime, HostState, RuntimeState

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
LEADER_VERBS: frozenset[str] = frozenset(("quarantine", "reinstate"))

#: The state each simple verb moves a host to. `evict`, `quarantine` and
#: `reinstate` are not here because none is a plain state assignment -- each
#: carries bookkeeping (a fence, an attribution, or the state it interrupted).
_TARGET_STATE: dict[str, HostState] = {"activate": "active", "drain": "passive"}

#: ss8's `T_kill`: how long after a deadman fires until the host's wrappers
#: are certainly gone. The supervisor's exit EOFs every lifeline; each
#: wrapper then sends TERM to its command pgid, waits the run's
#: `grace_seconds`, sends KILL, records and exits (supervisor-protocol ss4
#: step 5). Two graces at the 10 s default covers that plus the supervisor's
#: own wait for the wrappers, and the number is deliberately generous: it is
#: added to a wait an operator can always skip with `--force`, so erring
#: long costs patience and erring short costs a double run.
T_KILL_S = 30.0

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


def host_rejection_reason(store: RuntimeState, cmd: HostCommand, at: datetime) -> str | None:
    """ss8's preconditions: why this command must not apply, or None.

    A REJECTION, not a refusal (control-protocol ss3). Every check below
    reads mutable state, so it can only be made where `expect` is made --
    inside the input's own batch, in log order. Deciding at the door would
    answer against a table that had moved by the time the command applied,
    and would leave the log holding a verdict replay could not reproduce."""
    row = store.host(cmd.host_id)
    if row is None:
        return (
            f"no host {cmd.host_id!r} in the routing table: a host joins it by"
            " registering, never by being addressed"
        )
    if cmd.verb == "evict":
        return _evict_reason(row, cmd, at)
    if cmd.verb in LEADER_VERBS:
        # the leader's own observation. Refused against an EVICTED host and
        # nothing else: eviction is a decision about the host's work, and a
        # leader that quietly un-evicted a host by reaching it again would
        # undo the one state an operator took deliberately. Such a host
        # returns by re-registering at its new generation (ss8), which is
        # where the fence is checked.
        if row.state == "evicted":
            return (
                f"host {cmd.host_id!r} was evicted at generation {row.generation}:"
                " reaching it again does not un-evict it, and it may not be routed"
                " to until it re-registers at that generation and self-fences (ss8)"
            )
        return None
    if row.state == "quarantined":
        return (
            f"host {cmd.host_id!r} is quarantined: the leader set that because the host"
            " stopped answering and clears it when the host answers again, holding its"
            " jobs meanwhile (ss7). An operator state change would not make it reachable"
        )
    if row.state == "evicted":
        return (
            f"host {cmd.host_id!r} was evicted at generation {row.generation}: it returns"
            " by re-registering at that generation and self-fencing first, not by an"
            " operator state change (ss8)"
        )
    return None


def _evict_reason(row: HostRuntime, cmd: HostCommand, at: datetime) -> str | None:
    """ss8's three eviction preconditions, in the order that costs least to
    explain. A refusal reports the remaining wait, so the operator waits
    rather than guesses."""
    if row.state == "evicted":
        return f"host {cmd.host_id!r} is already evicted, at generation {row.generation}"
    if cmd.force:
        return None  # ss8: attributed, not forbidden. The record is the safety story.
    if row.state != "quarantined":
        return (
            f"host {cmd.host_id!r} is {row.state}: eviction needs the leader's own durable"
            " record that the host is unreachable (ss8 precondition 1), which is the"
            " `quarantined` state. Draining does not assert that, and out-of-band"
            " knowledge that the machine is dead is what --force is for"
        )
    if row.deadman_s is None:
        return (
            f"host {cmd.host_id!r} runs no deadman (ss8 precondition 2): nothing bounds"
            " when its wrappers die, so no wait can prove its work is safe to reroute."
            " --force is the only eviction such a host can have, and it is recorded as one"
        )
    if row.last_contact is None:
        return (
            f"host {cmd.host_id!r} has never been in contact, so the ss8 bound has no"
            " start: nothing here can say how long its wrappers have had to die"
        )
    bound = row.deadman_s + T_KILL_S
    bound += skew_allowance(bound)
    waited = (at - row.last_contact).total_seconds()
    if waited <= bound:
        return (
            f"host {cmd.host_id!r} was in contact {waited:.1f}s ago and the ss8 bound is"
            f" {bound:.1f}s (deadman {row.deadman_s:.1f}s + kill {T_KILL_S:.1f}s + skew):"
            f" wait {bound - waited:.1f}s more, or --force with proof it is dead"
        )
    return None


def apply_host_command(store: RuntimeState, cmd: HostCommand, *, actor: str | None) -> None:
    """Apply one gated host command. Called inside the input's batch, so the
    row it moves takes that input's single revision like any other entity
    (ss3)."""
    if cmd.verb == "evict":
        # only a FORCED eviction records an actor: on the gated path the
        # preconditions are the justification, and naming a principal beside
        # them would suggest the eviction rested on who asked for it
        store.evict_host(cmd.host_id, forced_by=actor if cmd.force else None)
        return
    if cmd.verb == "quarantine":
        store.quarantine_host(cmd.host_id)
        return
    if cmd.verb == "reinstate":
        store.reinstate_host(cmd.host_id)
        return
    store.set_host_state(cmd.host_id, _TARGET_STATE[cmd.verb])
