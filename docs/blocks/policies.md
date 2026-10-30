# Policies

A policy here is a rule the runner applies without a stored mode of its own: a pure choice, a calculation, or a fixed procedure.
None of them keeps a stored state that moves along declared transitions, so none has a table in [the state machines](../state-machines.md).
This page is not a block card. It gives each policy one paragraph: what it decides, why it is not a state machine, and where its rules live.

## Scheduler frontier

The [scheduler frontier](../glossary.md#scheduler-frontier) decides which calendar ticks a segment has already admitted or dropped, so that a resume or a seal neither fires a tick twice nor loses one without a record.
It is a calculation, not a state machine.
`scheduler_frontier(records)` in `runner_journal.py` reads it off the journal's record kinds and stamps each time it is needed, and the ticks after it are computed from the calendars.
No mode is stored. The steps in the scheduler card's flowchart are steps of that computation.
Its rules are in [the scheduler card](scheduler.md) and period-model §6 (`period-model.md`, retired in 2.0.0).

## Work choice

The work choice decides the engine loop's next act: take a queued input, take a calendar tick, fire a timer, wait, or return.
It is a pure choice, not a state machine.
`Engine._next_work` reads the queue, the oracle's next timer, the next tick and the clock, returns one `_Work` value, and keeps nothing between turns. Its docstring holds the truth table.
Its rules are in [the engine loop card](engine-loop.md) and [runner-design §4](../runner-design.md#4-engine-loop--single-writer).

## Access

Access decides whether a peer may run a verb, and it stamps the principal on what it admits.
It is a verdict, not a state machine.
The verdict is a pure function of the loaded role map and the peer's kernel credentials. A reload replaces the map whole, and nothing moves per peer.
Its rules are in [access-model §4](../access-model.md#4-the-role-map), [§5](../access-model.md#5-the-enforcement-point) and [§7](../access-model.md#7-reload-and-revocation).

## Audit

Audit decides whether a period's own evidence re-derives its seal, and on success it writes the period's attestation and marks its registry row attested.
It is a calculation, not a state machine.
`audit` replays the period's WAL, spool and manifests and compares the result with the sidecar.
The fact it leaves is a file, the attestation `seals/<N>.audit.json`, written first.
It then marks the registry row attested, the last move of the period_row (retired in 2.0.0) machine, whose card is seal and lineage (`seal-and-lineage.md`, retired in 2.0.0).
That flip can fail and leave the file with no row (`attest.Unattested`). The file stays the authority.
Audit and attestation were retired in 2.0.0.

## Retention

Retention decides, for each file in a run root, whether it is floored, held or prunable, and `prune` deletes only what is prunable.
It is a verdict per file, not a state machine.
Retention recomputes the floor from the lineage head each time it runs.
It reads files and writes one receipt, once. Nothing moves along transitions.
A period counts as attested only when its attestation file verifies. The registry row is not the authority.
A period counts as archived when the root holds its bound receipt, `seals/<N>.archive.json`, which is written once, before the first deletion.
Its rules are in period-model §12 (`period-model.md`, retired in 2.0.0) and §12a (`period-model.md`, retired in 2.0.0). The operator flags are in deployment-runbook §2a (`deployment-runbook.md`, retired in 2.0.0).

## Wrapper outcome

The wrapper outcome decides what one run's `status.json` says about how its command ended, and how the engine maps that record to an exit code or a failure.
It is a fixed procedure and a classification, not a state machine.
The wrapper is one straight-line process with no state another process moves, and `outcome_from_status` in `runner_adapters.py` maps one record to one result, the same way live and at resume.
Its rules are in [the wrapper card](wrapper.md), supervisor-protocol §3 (`supervisor-protocol.md`, retired in 2.0.0) and §4 (`supervisor-protocol.md`, retired in 2.0.0).

## SPAWN idempotency

SPAWN idempotency decides how the supervisor answers a SPAWN whose `run_id` it may have seen: start the run, answer the earlier result, or refuse.
It is a decision table, not a state machine.
The run directory fills in a fixed order, except that the wrapper's `spawn.json` and the supervisor's `reply.json` may land in either order.
A replay reads the files a crash left. Those files are not states that a transition moves, and `_resolve_replay` answers from them.
Its rules are in the SPAWN idempotency card (`spawn-idempotency.md`, retired in 2.0.0), the crash-point table in period-model §11a (`period-model.md`, retired in 2.0.0) and supervisor-protocol §5 (`supervisor-protocol.md`, retired in 2.0.0).

## Leadership

Leadership decides which process may append to a run root's log and act on its estate, and it fences every later act of a process that lost it.
It is a fence protocol, not a state machine.
Its one stored fact is the run root's `flock`, held for the process lifetime, with a note naming its holder.
`LeaderLock.check` re-proves it before every append by re-reading the lock file's inode, and a lost lock stops the process.
Nothing moves along transitions: the lock is held until the process stops.
Its rules are in DL-99 and DL-101, and `runner_ledger.py` implements them.
