# Block cards

A block card is a short reader's page about one runner building block.
A reviewer reads [the architecture overview](../architecture.md) first,
then one card at a time.
Cards hold no rules. Each invariant on a card links the decision or the
contract section that states it. A card never restates a frozen table; it
links it.

## The card shape

Every card has these sections, in this order:

1. **Purpose.** What the block is for, in two or three sentences.
2. **Fate.** Whether the block's code and contract would carry over if the
   storage under the runner changed, and which storage capabilities it
   needs. The capabilities are the rows of
   [concurrency-model §1](../concurrency-model.md#1-storage--frozen)'s
   storage contract: epoch-conditional append, monotone epoch allocation,
   decision lookup by `request_id`, atomic multi-record commit, and
   linearizable read of the leader record. A block that needs none says
   "none".
3. **Interface.** What the block takes and gives, as module functions,
   records, verbs or files.
4. **States.** A block whose states are a declared machine links that
   machine in [the state machines](../state-machines.md), by anchor. The
   anchor holds the generated diagram and transition table, so the card
   draws no diagram and copies no transition. A block with no declared
   machine either links its paragraph in [the policies](policies.md) or
   says on its own card why it has none. It may keep a Mermaid flowchart
   of its steps, data or ordering, with no state notation. A state diagram
   appears only where a declared, tested machine backs it.
5. **Invariants.** Each one links its decision-log entry or contract
   section.
6. **Failure and recovery.** What can go wrong and what brings the block
   back, with links.
7. **Owning modules.** The modules under `src/dsl41/` that implement it.
8. **Tests.** The tests that exercise it, by name, so the architecture
   gate checks that they exist.
9. **Open findings.** A link to the block's row in
   [the risk map](../risk-map.md).

A card that finds a gap, or a contradiction between the code and a
contract, lists it under **Gaps found** at the end. A card never settles
one.

A card is at most 120 lines. [The policies](policies.md) page is not a
block card: it gives one paragraph to each policy. `tests/test_block_cards.py`
checks the length, the index below, the machine links and that no card
draws a state diagram.

## Cards

The list follows the review order.

Semantics core:

- [Job lifecycle and flags](job-lifecycle.md): one job's status, operator
  flags and schedule arm.
- [Box execution](box-execution.md): the box start reset, the completion
  fold, overrides and cascades.
- [Capacity waiter and reservation](capacity.md): machine load, resources,
  the QUE_WAIT queue and held units.
- [Scheduler and timer frontier](scheduler.md): calendar ticks, the resume
  sweep and the cutoff.

Process tier:

- [Supervisor](supervisor.md): the process that holds detached runs'
  lifelines, its lease and its lifecycle.
- [SPAWN idempotency](spawn-idempotency.md): why a replayed SPAWN never
  starts a second process.
- [Wrapper](wrapper.md): the per-run recorder that writes `spawn.json` and
  `status.json`.
- [FW observation](fw-observation.md): the file watch and its append-only
  evidence log.

Engine:

- [Engine loop: the work choice](engine-loop.md): how the single writer
  picks its next act.
- [Admission and idempotency](admission.md): one ordered path from arrival
  to a durable decision.
- [Effect outbox](effect-outbox.md): SPAWN and KILL intent recorded before
  the attempt, then dispatched or reconciled.
- [Host routing](host-routing.md): the routing table's four states, drain,
  quarantine and eviction.
- [Control subscription](subscription.md): one `subscribe` feed, from its
  backfill to its end.
- [Seal and lineage](seal-and-lineage.md): the anchor head, the period
  registry row and the engine's seal phases.

Rules without a machine:

- [Policies](policies.md): the scheduler frontier, the work choice,
  access, audit, retention, the wrapper outcome, SPAWN idempotency and
  leadership, and why each is not a state machine.

## Machines

Each machine in [the state machines](../state-machines.md) and the card
that explains it.

| Machine | Card |
| --- | --- |
| [job_status](../state-machines.md#job_status) | [Job lifecycle](job-lifecycle.md); its box rows in [Box execution](box-execution.md); QUE_WAIT in [Capacity](capacity.md) |
| [job_flags](../state-machines.md#job_flags) | [Job lifecycle](job-lifecycle.md) |
| [job_holding](../state-machines.md#job_holding) | [Capacity](capacity.md); [Job lifecycle](job-lifecycle.md) |
| [runtime_assembly](../state-machines.md#runtime_assembly) | [Job lifecycle](job-lifecycle.md) |
| [anchor_head](../state-machines.md#anchor_head) | [Seal and lineage](seal-and-lineage.md) |
| [period_row](../state-machines.md#period_row) | [Seal and lineage](seal-and-lineage.md) |
| [supervisor_process](../state-machines.md#supervisor_process) | [Supervisor](supervisor.md) |
| [supervisor_lease](../state-machines.md#supervisor_lease) | [Supervisor](supervisor.md) |
| [supervisor_client](../state-machines.md#supervisor_client) | [Supervisor](supervisor.md) |
| [host](../state-machines.md#host) | [Host routing](host-routing.md) |
| [admission](../state-machines.md#admission) | [Admission](admission.md) |
| [effect](../state-machines.md#effect) | [Effect outbox](effect-outbox.md) |
| [subscription](../state-machines.md#subscription) | [Control subscription](subscription.md) |
| [seal_boundary](../state-machines.md#seal_boundary) | [Seal and lineage](seal-and-lineage.md) |
