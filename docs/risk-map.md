# Risk map of the runner machines

For the architect finding the engine map and its known limits. Use the
[architecture map](architecture.md), [CLI/TUI qualification](cli-tui-qualification.md)
and [simulation coverage](simulation-coverage.md) in the
[architect track](../README.md#documents). A qualification result must name
its revision and executed lane. This page supplies reference pointers and
known limits; [deploy](deploy.md) covers operating procedures.

## Least-tested machines

See [the registered machines](../src/dsl41/machines.py) and [generated reference](state-machines.md).

## Closed by DL-263..DL-275

See [the decision index](decision-index.md).

## Closed by DL-289..DL-306

See [the decision index](decision-index.md).

## Closed by DL-353

See [the generated machine reference](state-machines.md).

## Open findings

See [the qualification scope](cli-tui-qualification.md).

### Torn sole opening segment of a rolled root

See [DL-424](decision-log.md).

### Held-unit circular wait

See [capacity limits](blocks/capacity.md#open-findings) and [DL-286](decision-log.md).

### A replayed oracle fault stops every resume

A faulting time half stops the engine with exit 6. Replaying a faulting
input can stop every resume until a fixed build or rollback makes replay
possible. See [DL-292](decision-log.md) and
[statechart fault handling](statechart-engine.md#11-instances-and-boundaries).

### Instant cascades that do not end

A per-root item cap stops a cascade that does not end. It does not bound
loops across root dispatches or a computation that does not end inside one
action, guard or fact. See [DL-351](decision-log.md) and
[the engine loop](runner-design.md#4-engine-loop--single-writer).

### Open vendor readings for a box and its members

See [open semantics questions](autosys-semantics.md#9-open-questions-and-their-pinned-defaults).

### A completed box and an iced member

See [box execution](blocks/box-execution.md).

### A journal stays open when genesis fails late

See [runner recovery](runner-design.md#7-journal-and-recovery-e1-prod-grade).

### Anchor machine items

See [DL-424](decision-log.md).

### Unbounded trace and counters

The oracle trace and the engine's drop, dedupe and refusal counters grow
without a bound during the process's life. A capacity measurement must
establish a limit; none is recorded here. See [DL-307](decision-log.md).

### A host's boot_id check reads a foreign live run as dead

See [DL-424](decision-log.md).

### Operator procedures not exercised

See [CLI/TUI qualification](cli-tui-qualification.md).

### Stated limits

See [runner scope and limits](runner-design.md#12-non-goals).

## Refresh

See [verification commands](agent-workflow.md#verify-a-change).
