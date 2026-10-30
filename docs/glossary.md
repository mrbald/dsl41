# Glossary of the runner contracts

This page is a reader's index to the runner contracts. It is not normative.
Each entry says what a thing is and links the section that defines it.
The rules live in the linked sections.
Entries are in alphabetical order, ignoring case and punctuation.

### baseline_id

The identity of one period on the control wire. Request envelopes and
revision-bearing reads carry it, and each period derives a fresh one.
See period-model.md §4 (`period-model.md`, retired in 2.0.0) and
concurrency-model.md §6 (`concurrency-model.md`, retired in 2.0.0).
The code is [`run_root.py`](../src/dsl41/run_root.py).

### catalog_hash

The runner's versioned hash of a loaded catalog (`CatalogIR`). It is a
different hash from `equiv.catalog_hash`, which serves equivalence checks.
See period-model.md §1.1 (`period-model.md`, retired in 2.0.0).
The code is [`run_root.py`](../src/dsl41/run_root.py).

### code

The stable error code on every `ok: false` control answer, beside the prose
`error`. A client branches on `code`; the prose may change. A code names the
reason, not the outcome. A rejected `decision` stores its code, so an exact
retry answers the same one; a decision written before DL-272 stores none.
The control-protocol code table lists every code and the client's next move;
`runner_codes.py` holds the same set.
See [control-protocol.md §2](control-protocol.md#2-transport-and-framing-frozen)
and period-model.md §2.3 (`period-model.md`, retired in 2.0.0).

### decision

The WAL record that closes one admitted input. One `decision` line holds the
outcome (`applied` or `rejected`), the revisions that moved and the effects the
input implies. A rejected one also stores its [code](#code).
See period-model.md §2.3 (`period-model.md`, retired in 2.0.0) and
concurrency-model.md §4 (`concurrency-model.md`, retired in 2.0.0).

### detached

The lifecycle mode chosen with `dsl41 run --detached`. A supervisor holds the
wrappers' lifelines, so an engine restart reattaches to running jobs instead
of ending them, unless a `--deadman` interval runs out first.
See [runner-design.md §1](runner-design.md#1-mission-and-scope),
[§6a](runner-design.md#6a-process-lifecycle-tiers-dl-41a) and, for the deadman,
concurrency-model.md §8 (`concurrency-model.md`, retired in 2.0.0).

### effect

One act the engine intends for one run: a `SPAWN` or a `KILL`. It is
recorded in its `decision` before it is attempted, and its outcome later
reads `applied`, `indeterminate` or `retired`.
See concurrency-model.md §5 (`concurrency-model.md`, retired in 2.0.0).
The code is [`runner_effects.py`](../src/dsl41/runner_effects.py).

### epoch

The leader's term number. A leader takes one by appending a `leader` record,
and each request envelope carries the epoch its client read.
See concurrency-model.md §7 (`concurrency-model.md`, retired in 2.0.0)
and period-model.md §2.4 (`period-model.md`, retired in 2.0.0).
The code is [`runner_ledger.py`](../src/dsl41/runner_ledger.py).

### estate

In the period model, one lineage of periods from genesis until retirement,
named by `estate_id`, a uuid4 minted once at genesis. Outside the period model,
the word also means the jobs one catalog defines.
See period-model.md §1 (`period-model.md`, retired in 2.0.0) and
§1.2 (`period-model.md`, retired in 2.0.0).
The code is [`estate_manifest.py`](../src/dsl41/estate_manifest.py).

### expect

The envelope field that names the revision the caller read: a map from one
entity key (`job:`, `global:` or `host:`) to its `state_rev`. Admission
compares it with the current revision.
See concurrency-model.md §0 (`concurrency-model.md`, retired in 2.0.0) and
§6 (`concurrency-model.md`, retired in 2.0.0).
The code is [`control-protocol.md`](control-protocol.md).

### fail-stop

The engine stops instead of refusing, and runs no abort. It is used where an
abort would reopen admission over a state the WAL does not describe. The seal
gets no answer, and recovery rebuilds from the WAL. A seal fail-stops in two
kinds of case. Before the `seal` append: a fence loss, and DL-274's three
cases, which are an exception while an attempt admitted during the seal is
not fully applied, a failed WAL append, and a `clock_regressed` on an
engine-made input. After those, the period stays open. At or after the `seal`
append, including an anchor close after a durable seal line, the outcome is
unknown. Recovery commits the seal when the line is complete and its `fsync`
succeeds; it truncates a torn or absent line and reopens the period (PR-28d).
See period-model.md §7 (`period-model.md`, retired in 2.0.0) and
concurrency-model.md §4 (`concurrency-model.md`, retired in 2.0.0).
The code is [`statechart-engine.md`](statechart-engine.md).

### fencing token

The integer `token` a supervisor issues with each lease. Mutating supervisor
verbs carry it beside the incarnation; the concurrency model also uses the
phrase for the leader's epoch.
See supervisor-protocol.md §5 (`supervisor-protocol.md`, retired in 2.0.0)
and concurrency-model.md §1 (`concurrency-model.md`, retired in 2.0.0).
The code is [`runner_ledger.py`](../src/dsl41/runner_ledger.py).

### gateway

A proposed HTTP and WebSocket front for `control.sock`: one process per
exposed tier, each a client of the socket. Its status is proposed, and
nothing of it is built (DL-273).
See [gateway.md](gateway.md).

### ghost

A status row with no job definition in the current catalog, so `status`
reports `job_type: null` for it. `CHANGE_STATUS` invents one for a `JOB^INST`
target, and a job the next period's catalog drops while it is not live stays
as one.
See [control-protocol.md §3](control-protocol.md#3-mutating-verbs-sendevent-host),
[§4](control-protocol.md#status-job) and
period-model.md §10.1 (`period-model.md`, retired in 2.0.0).

### ghost run

A `STARTING` that an injected `CHANGE_STATUS` writes without advancing the
job's run number, so it is not a start the oracle decided. The ghost-run gate,
the engine's `_dispatched` map, keeps per job the last run number a SPAWN was
planned for, held or retired ones included (DL-234), and plans no SPAWN for a
ghost run.
See [runner-design.md §4](runner-design.md#4-engine-loop--single-writer) and
period-model.md §3.3 (`period-model.md`, retired in 2.0.0).

### incarnation

A hex id the supervisor mints at every start and reports from `PING`, `LIST`
and `ACQUIRE`. A changed incarnation tells a controller that the supervisor it
knew is gone.
See supervisor-protocol.md §5 (`supervisor-protocol.md`, retired in 2.0.0).
The code is [`runner_startup.py`](../src/dsl41/runner_startup.py).

### lease

The single-controller right to change a supervisor's runs, taken with
`ACQUIRE` and kept with `RENEW` before its TTL runs out. Read-only clients need
none, and the engine's leadership lock is not a lease.
See supervisor-protocol.md §5 (`supervisor-protocol.md`, retired in 2.0.0),
[runner-design.md §6a](runner-design.md#6a-process-lifecycle-tiers-dl-41a)
and, for the leadership lock,
concurrency-model.md §1 (`concurrency-model.md`, retired in 2.0.0).

### outbox

The set of effects the engine has intended, with the outcome of each. The WAL
carries it in `decision` and `effect_result` records, and replay rebuilds it.
See concurrency-model.md §1 (`concurrency-model.md`, retired in 2.0.0) and
§5 (`concurrency-model.md`, retired in 2.0.0).
The code is [`runner_effects.py`](../src/dsl41/runner_effects.py).

### perimeter

The access gate in front of `control.sock` and the served web TUI. It maps an
authenticated principal to a tier (read, ops or adm) and writes its receipts to
its own journal, `perimeter.jsonl`.
See [access-model.md §1](access-model.md#1-the-model),
[§2](access-model.md#2-the-boundary) and
[§6](access-model.md#6-receipts-the-perimeter-journal).

### preflight

The checks a catalog passes before `run` or `rehearse` starts. An ERROR
finding refuses the run; a WARN finding is printed and journaled.
See [runner-design.md §8](runner-design.md#8-preflight--refuse-loudly-run-honestly).

### rehearse

The engine's second duty, `dsl41 rehearse`: the same engine code under a
virtual clock with scripted adapters. A day of an estate plays in seconds, and
no process is spawned.
See [runner-design.md §1](runner-design.md#1-mission-and-scope).

### request_id

The id a client puts on each mutating request. With the envelope's
fingerprint, it lets the engine answer an exact retry from the recorded
decision.
See concurrency-model.md §4 (`concurrency-model.md`, retired in 2.0.0)
and [control-protocol.md §3](control-protocol.md#3-mutating-verbs-sendevent-host).

### run root

The directory one engine runs in, given by `--run-root`; the period model also
calls it the estate root. It holds the sockets, the WAL segments, the seals and
the `runs/` spool, and it is a path only, not an identity.
See period-model.md §1 (`period-model.md`, retired in 2.0.0) and
§1.1 (`period-model.md`, retired in 2.0.0).
The code is [`run_root.py`](../src/dsl41/run_root.py).

### runtime profile

The launch options that change how a period interprets or dispatches its
catalog, such as the timezone basis, the machine identity, the deadman and the
semantic switches. The period manifest stores it, and `runtime_hash` is its
hash.
See period-model.md §2.1 (`period-model.md`, retired in 2.0.0)
and [runner-design.md §8a](runner-design.md#8a-semantic-switches).

### scheduler frontier

The latest instant a segment's journal proves the scheduler reached; the
linked section lists which records count. Resume re-derives schedule ticks
from it.
See period-model.md §6 (`period-model.md`, retired in 2.0.0).
The code is [`runner_scheduler.py`](../src/dsl41/runner_scheduler.py).

### segment

One WAL file, `wal/<segment_no>.jsonl`, whose first record is a `segment`
record. Segment N holds period N.
See period-model.md §1 (`period-model.md`, retired in 2.0.0) and
§2.1 (`period-model.md`, retired in 2.0.0).
The code is [`run_root.py`](../src/dsl41/run_root.py).

### SPAWN

The effect kind that launches one run of a CMD or FW job; an FW run is a watch
inside the engine, not a process. In detached mode, SPAWN is also the
supervisor verb that launches a CMD run, and the effect's `run_id` is its
idempotency key.
See concurrency-model.md §5 (`concurrency-model.md`, retired in 2.0.0),
supervisor-protocol.md §5 (`supervisor-protocol.md`, retired in 2.0.0)
and period-model.md §11a (`period-model.md`, retired in 2.0.0).
The code is [`runner_effects.py`](../src/dsl41/runner_effects.py).

### state_rev

A revision counter on one job, global or host row. It moves when a committed
input changes the row's semantic projection.
See concurrency-model.md §3 (`concurrency-model.md`, retired in 2.0.0).
The code is [`oracle_state.py`](../src/dsl41/oracle_state.py).

### supervisor

The per-run-root process that spawns wrappers for a detached engine and holds
their lifelines across engine restarts. It speaks a line protocol on
`supervisor.sock` and keeps no scheduling policy.
See supervisor-protocol.md §1 (`supervisor-protocol.md`, retired in 2.0.0) and
[runner-design.md §6a](runner-design.md#6a-process-lifecycle-tiers-dl-41a).

### tethered

The default lifecycle mode. The engine is the wrappers' parent, so engine
death ends every running job, and each wrapper records the outcome.
See [runner-design.md §1](runner-design.md#1-mission-and-scope) and
[§6a](runner-design.md#6a-process-lifecycle-tiers-dl-41a).

### wrapper

The small stdlib-only process, `runner_wrapper.py`, that is the direct parent
of one run's command. It writes `spawn.json` and `status.json` to the run's
spool directory and kills the command's process group when its lifeline
closes.
See supervisor-protocol.md §1 (`supervisor-protocol.md`, retired in 2.0.0) and
§4 (`supervisor-protocol.md`, retired in 2.0.0).
The code is [`runner_wrapper.py`](../src/dsl41/runner_wrapper.py).

