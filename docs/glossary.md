# Glossary of the runner contracts

For the architect resolving v2 engine terms and finding their defining
contracts. Continue through the [architect track](../README.md#documents)
and [architecture map](architecture.md). This index holds no rules;
[deploy](deploy.md) covers operating procedures.
Entries are alphabetical, ignoring case and punctuation.

### attempt

One execution of a logical job run. A command attempt has its own `run_id`,
ordinal and spool. An interruption may produce another attempt of the same
run and invocation; an application failure does not.
See [DL-424, DL-428 and DL-433](decision-log.md) and the generated
[ATTEMPT reference](state-machines.md).

### baseline_id

The store's control-wire identity, created at genesis and preserved across
restart and configuration update. It scopes revisions; it does not prove
that a request id remains in the retry index.
See [control headers](control-protocol.md#2-transport-and-framing-frozen)
and [retry lifetime](control-protocol.md#3-mutating-verb-sendevent).

### catalog_hash

The fingerprint of the loaded catalog's canonical definition. The runner
uses `reload_diff.py`'s catalog hash; equivalence checking has its own hash.
See [configuration adoption](control-protocol.md#2-transport-and-framing-frozen)
and [runner_config.py](../src/dsl41/runner_config.py).

### code

The stable reason beside `error` on every `ok: false` control answer.
A code alone does not classify the outcome. A rejected decision stores its
code for exact retries.
See [the code table](control-protocol.md#2-transport-and-framing-frozen)
and [DL-272](decision-log.md).

### decision

The engine's recorded verdict on an admitted input, with the revisions it
moved. The input and decision commit in one SQLite transaction. The decision
is not evidence of command completion.
See [DL-428](decision-log.md) and
[control outcomes](control-protocol.md#3-mutating-verb-sendevent).

### detached

Detached mode and `--detached` resolve to [DL-424](decision-log.md); see [tethered](#tethered).

### estate

The jobs and referenced definitions that form the loaded catalog.
The engine runs them on one host.
See [runner scope](runner-design.md#1-mission-and-scope);
`estate_id` and the period lineage resolve to [DL-424](decision-log.md).

### expect

The precondition naming the entity revision the caller read. Job mutations
address `job:<name>`; global mutations address `global:<name>`. Configuration
update uses the configuration revision. Zero names an absent entity row.
See [mutation envelopes](control-protocol.md#3-mutating-verb-sendevent)
and [update admission](control-protocol.md#2-transport-and-framing-frozen).

### fail-stop

Seal fail-stop (PR-28d) resolves to [DL-274 and DL-424](decision-log.md); engine fault outcomes belong to [control §2](control-protocol.md#2-transport-and-framing-frozen).

### fencing token

Supervisor fencing tokens resolve to [DL-424](decision-log.md); one engine holds the run root's `leader.lock`.

### gateway

The HTTP/WebSocket proposal resolves through [gateway](gateway.md) and [DL-273](decision-log.md); the built browser path is [served TUI](runner-design.md#11-ui--one-textual-app-terminal-and-web-e3).

### ghost

A status row without a definition in the current catalog; its `job_type` is
null. `CHANGE_STATUS` can create one for a cross-instance `JOB^INST` target.
See [sendevent](control-protocol.md#3-mutating-verb-sendevent)
and [status](control-protocol.md#status-job).

### ghost run

A `CHANGE_STATUS STARTING` overwrite that does not advance the run number.
It does not request a command launch. The dispatch frontier prevents a
second SPAWN for the same logical run.
See [the ghost-run gate](runner-design.md#4-engine-loop--single-writer)
and [DL-234](decision-log.md).

### incarnation

Supervisor incarnation resolves to [DL-424](decision-log.md); [baseline_id](#baseline_id) identifies the engine store on the control wire.

### interruption rule

The rule for an attempt ended by engine death or a requested stop.
An idempotent job gets a replacement attempt of the same logical run and
invocation; a named non-idempotent job is held with failure evidence.
Application failure is not retried.
See [DL-424 and DL-433](decision-log.md).

### lease

Supervisor leases, `ACQUIRE` and `RENEW` resolve to [DL-424](decision-log.md); the engine uses a local `leader.lock` flock.

### main config

The engine selector at `/etc/dsl41/dsl41.toml`, or an absolute `--config`.
It names the root, host settings, included inputs, interpretation and lint
policy. Restart uses the stored applied revision without re-reading sources.
See [control §2](control-protocol.md#2-transport-and-framing-frozen)
and [DL-437](decision-log.md).

### non-idempotent job

A job named in the configured idempotence exceptions. An interruption holds
it with failure evidence instead of launching a replacement attempt.
See [the interruption rule](#interruption-rule) and [DL-424](decision-log.md).

### perimeter

The access gate on `control.sock`. With an access map, it authenticates an
OS peer, assigns read, ops or adm, and writes access receipts separately
from engine decisions. Served TUI children use the same gate.
See [the boundary](access-model.md#2-the-boundary),
[receipts](access-model.md#6-receipts-the-perimeter-journal) and
[the web tier](access-model.md#9-the-web-tier).

### plan token

The 64-hex-digit identity printed by `dsl41 reread` for its captured plan
under `plans/<token>/`. `dsl41 update TOKEN` asks the engine to apply those
captured bytes against the named configuration revision.
See [control §2](control-protocol.md#2-transport-and-framing-frozen)
and [DL-437](decision-log.md).

### preflight

The checks before `run` or `rehearse`. Errors refuse startup; warnings
identify limitations under which execution proceeds.
See [runner preflight](runner-design.md#8-preflight--refuse-loudly-run-honestly).

### rehearse

The engine running with a virtual clock and scripted adapters instead of
real job processes. It exercises the engine's semantics path.
See [runner duties](runner-design.md#1-mission-and-scope).

### request_id

The client-supplied identity of a mutation. An exact retained retry uses
that id and the same fingerprint to retrieve its original decision.
Pruning frees the id; it is not a permanent deduplication key.
See [retry obligations](control-protocol.md#3-mutating-verb-sendevent).

### root_id

The persistent UUID in store metadata used as the prefix of
`DSL41_COOKIE=<root_id>.<run_id>`. It identifies processes for attempt
cleanup; it is distinct from the control-wire baseline.
See [the process cookie](wrapper-protocol.md#dsl41_cookie-env-variable--the-process-cookie-an-addition)
and [DL-433](decision-log.md).

### run

One logical execution of a job, identified within a run root by job name
and run number. Replacement attempts preserve that run number and its
invocation. A box has a logical run without a command wrapper.
See [DL-424](decision-log.md) and [run history](runner-design.md#7-journal-and-recovery-e1-prod-grade).

### run root

The local directory owned by one engine: `engine.db`, `leader.lock`,
`engine.toml`, `control.sock`, plans, logs and attempt spools. It is a path,
not a wire identity. The main config selects it by default.
`dsl41 run --run-root PATH` belongs to the legacy explicit-input path;
`--config` refuses `--run-root` ([DL-437](decision-log.md)).
See [deploy](deploy.md) and [runner_startup.py](../src/dsl41/runner_startup.py).

### runtime profile

The recorded interpretation settings, including timezone basis, machine
identity and semantic switches. Its fingerprint is `runtime_hash`.
See [semantic switches](runner-design.md#8a-semantic-switches)
and [runner_config.py](../src/dsl41/runner_config.py).
Deadman and period-manifest fields resolve to [DL-424 and DL-428](decision-log.md).

### scheduler frontier

The recovered scheduler position used to resume calendar scheduling.
Missed downtime ticks are skipped and recorded, rather than fired late.
See [scheduler rules](runner-design.md#5-scheduler--the-calendar-the-oracle-deliberately-lacks)
and [snapshot recovery](runner-design.md#7-journal-and-recovery-e1-prod-grade).

### segment

The subscribe stream's configuration record, projected from a configuration
application. It is not a WAL file or a period identity.
See [the stream record](control-protocol.md#5-streaming-verb-subscribe)
and [DL-428](decision-log.md).

### snapshot

A stored recovery boundary containing oracle and scheduler state,
configuration revision, open work and retained run history. Restart restores
it before replaying the retained journal tail.
See [DL-428](decision-log.md) and [store](#store).

### SPAWN

A dispatch instruction for a CMD or FW run. A command gets a wrapper;
a file watch polls inside the engine. A durable dispatch precedes launch.
See [dispatch](runner-design.md#4-engine-loop--single-writer)
and [DL-428](decision-log.md).

### state_rev

A per-entity revision counter. It advances when an admitted batch changes
the entity's semantic projection and supplies a mutation's `expect` value.
It is distinct from the admission index and the store's record sequence.
See [status revisions](control-protocol.md#status-job).

### store

The engine's SQLite `engine.db`, holding its journal, configuration history,
metadata and snapshots. Input and decision commit together.
See [DL-428](decision-log.md) and [runner_store.py](../src/dsl41/runner_store.py).

### supervisor

The supervisor process, `supervisor.sock` and its verbs resolve to [DL-424](decision-log.md); the engine directly owns [wrappers](#wrapper).

### tethered

The execution posture in which engine death ends running command attempts.
The wrapper's lifeline detects parent loss, ends the command's process group
and records the observed outcome.
Before catch-up, restart TERMs what its root cookie scan finds of interrupted
attempts; the loop runs grace and KILL cleanup (DL-433, DL-436).
Restart applies the interruption rule instead of adopting live commands.
See [wrapper behavior](wrapper-protocol.md#4-wrapper-behavior-frozen-semantics)
and [DL-433](decision-log.md).

### wrapper

The stdlib-only recorder that parents one command attempt. It writes
`spawn.json` and `status.json`, with the latter as the sole outcome channel.
Its lifeline's write end belongs to the engine. Process-group containment
and Linux cookie cleanup have the limits the contract states.
See [input](wrapper-protocol.md#2-wrapper-input-spec-frozen),
[spool and cookie](wrapper-protocol.md#3-spool-format-frozen) and
[behavior](wrapper-protocol.md#4-wrapper-behavior-frozen-semantics).

`period-model.md`, `concurrency-model.md` and `supervisor-protocol.md`
resolve to [DL-424](decision-log.md); the defining sections above supply the v2 contracts.
