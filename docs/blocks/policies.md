# Policies

A policy here is a rule the runner applies without a stored mode of its own: a pure choice, a calculation, or a fixed procedure.
None of them keeps a stored state that moves along declared transitions, so none has a table in [the state machines](../state-machines.md).
This page is not a block card. It gives each policy one paragraph: what it decides, why it is not a state machine, and where its rules live.

## Scheduler frontier

The [scheduler frontier](../glossary.md#scheduler-frontier) decides which calendar ticks the store has already admitted or dropped, so that a resume neither fires a tick twice nor loses one without a record.
It is a calculation, not a state machine.
`Position` in `runner_journal.py` folds it from the store's record kinds and stamps in seq order, a snapshot carries it, and the ticks after it are computed from the calendars.
No mode is stored. The steps in the scheduler card's flowchart are steps of that computation.
Its rules are in [the scheduler card](scheduler.md) and [runner-design §7](../runner-design.md#7-journal-and-recovery-e1-prod-grade).

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

Period audit and attestation are outside the design ([DL-424](../decision-log.md)).

## Retention

Period retention floors and archive receipts are outside the design ([DL-424](../decision-log.md)).
Current store retention follows [runner-design §7](../runner-design.md#7-journal-and-recovery-e1-prod-grade).

## Wrapper outcome

The wrapper records how one attempt's command ended in `status.json`.
It is a straight-line procedure; the attempt tier owns the lifecycle state machine.
The engine derives the attempt outcome with `engine_charts.outcome_of` (DL-426).
Parent loss becomes `stopped{lifeline}`; the interruption rule chooses the follow-up plan (DL-424, DL-426).
`runner_adapters.outcome_from_status` maps only a `natural` attempt outcome to an oracle completion (`runner_attempts.oracle_input_for`).
The recording and outcome rules are in [the wrapper card](wrapper.md) and [wrapper-protocol §3](../wrapper-protocol.md#3-spool-format-frozen).

## SPAWN idempotency

Supervisor SPAWN idempotency is outside the design ([DL-424](../decision-log.md)).

## Leadership

Leadership decides which process may append to a run root's log and act on its estate, and it fences every later act of a process that lost it.
It is a fence protocol, not a state machine.
Its one stored fact is the run root's `flock`, held for the process lifetime, with a note naming its holder.
`LeaderLock.check` re-proves it before every append by re-reading the lock file's inode, and a lost lock stops the process.
Nothing moves along transitions: the lock is held until the process stops.
Its rules are in DL-99 and DL-101, and `runner_ledger.py` implements them.
