# Policies

A policy here is a rule the runner applies without a stored mode of its own: a pure choice, a calculation, or a fixed procedure.
None of them keeps a stored state that moves along declared transitions, so none has a table in [the state machines](../state-machines.md).
This page is not a block card. It gives each policy one paragraph: what it decides, why it is not a state machine, and where its rules live.

## Scheduler frontier

The [scheduler frontier](../glossary.md#scheduler-frontier) decides which calendar ticks a segment has already admitted or dropped, so that a resume or a seal neither fires a tick twice nor loses one without a record.
It is a calculation, not a state machine.
`scheduler_frontier(records)` in `runner_journal.py` reads it off the journal's record kinds and stamps each time it is needed, and the ticks after it are computed from the calendars.
No mode is stored. The steps in the scheduler card's flowchart are steps of that computation.
Its rules are in [the scheduler card](scheduler.md) and [period-model §6](../period-model.md#6-the-cutoff-barrier).

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
It then marks the registry row attested, the last move of the [period_row](../state-machines.md#period_row) machine, whose card is [seal and lineage](seal-and-lineage.md).
That flip can fail and leave the file with no row (`attest.Unattested`). The file stays the authority.
Its rules are in [period-model §11](../period-model.md#11-resume-replay-and-recovery) and [§1.3](../period-model.md#13-the-successor-fence), and `attest.py` implements them.

## Retention

Retention decides, for each file in a run root, whether it is floored, held or prunable, and `prune` deletes only what is prunable.
It is a verdict per file, not a state machine.
`retention.py` recomputes the floor from the lineage head each time it runs.
It reads files and writes one receipt, once. Nothing moves along transitions.
A period counts as attested only when its attestation file verifies. The registry row is not the authority.
A period counts as archived when the root holds its bound receipt, `seals/<N>.archive.json`, which is written once, before the first deletion.
Its rules are in [period-model §12](../period-model.md#12-non-goals) and [§12a](../period-model.md#12a-the-archive--pr-q3s-answer-dl-144). The operator flags are in [deployment-runbook §2a](../deployment-runbook.md#2a-retention--the-floors-and-the-prune-verb).

## Wrapper outcome

The wrapper outcome decides what one run's `status.json` says about how its command ended, and how the engine maps that record to an exit code or a failure.
It is a fixed procedure and a classification, not a state machine.
The wrapper is one straight-line process with no state another process moves, and `outcome_from_status` in `runner_adapters.py` maps one record to one result, the same way live and at resume.
Its rules are in [the wrapper card](wrapper.md), [supervisor-protocol §3](../supervisor-protocol.md#3-spool-format-frozen) and [§4](../supervisor-protocol.md#4-wrapper-behavior-frozen-semantics).

## SPAWN idempotency

SPAWN idempotency decides how the supervisor answers a SPAWN whose `run_id` it may have seen: start the run, answer the earlier result, or refuse.
It is a decision table, not a state machine.
The run directory fills in a fixed order, except that the wrapper's `spawn.json` and the supervisor's `reply.json` may land in either order.
A replay reads the files a crash left. Those files are not states that a transition moves, and `_resolve_replay` answers from them.
Its rules are in [the SPAWN idempotency card](spawn-idempotency.md), the crash-point table in [period-model §11a](../period-model.md#11a-spawn-idempotency-that-outlives-the-supervisor) and [supervisor-protocol §5](../supervisor-protocol.md#5-supervisor-socket-protocol-frozen--phase-11f-dl-48).

## Leadership

Leadership decides which process may append to a run root's log and act on its estate, and it fences every later act of a process that lost it.
It is a fence protocol, not a state machine.
Its stored facts are the `leader` record in the ledger and the locks the process holds: the run root's `flock` and, where there is an anchor, the lineage anchor's.
A lock is held for the process lifetime. `Fence.check` re-proves each one before an act by re-reading its file's inode, and a lost lock stops the process.
The epoch is a counter, allocated by appending the `leader` record.
None of these moves along transitions: a lock is held until the process stops, and the epoch only counts up.
Its rules are in [concurrency-model §1](../concurrency-model.md#1-storage--frozen), [§7](../concurrency-model.md#7-leadership-relay-takeover) and [period-model §2.4](../period-model.md#24-leader-and-the-epoch), and `runner_ledger.py` implements them.
