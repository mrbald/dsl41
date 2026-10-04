# Architecture overview

This page is a reader's map of dsl41. It holds no rules.
Each rule lives in the contract that the page links.
Read this page first, then read the [block cards](blocks/README.md) one at a
time.
The [glossary](glossary.md) defines the terms.
The [risk map](risk-map.md) shows where to look first.

Sections 2 to 4 follow the first three levels of the C4 model: context,
processes and stores, engine components. Section 5 follows one control
input end to end. Section 6 maps it all onto `examples/fulfilment`.

Every Mermaid node label and sequence participant names a module under
`src/dsl41/`, a `dsl41` CLI verb, or a process or actor from a short list.
`tests/test_architecture_doc.py` checks this.

## 1. Built and specified only

The sources are README's [Status](../README.md#status) and
[What is not built](../README.md#what-is-not-built), and the status lines of
the contracts.
A dashed node or edge in the diagrams below is specified only.

| Part | State | Source |
| --- | --- | --- |
| Compiler: parse, lower, derive, lint, graph, equivalence, UC twin, decompiler | built | README Status |
| Rich UC condition forms, the live OpenAPI pull, write-path verification, the generated client | specified only; needs a live controller | README What is not built |
| The decompiler's `--patterns` option | specified only | README What is not built |
| Engine, scheduler, admission, journal, outbox, control socket, access perimeter | built | [runner-design](runner-design.md) status line and [§14](runner-design.md#14-module-layout-and-phasing) |
| Supervisor and wrapper, the lifecycle tier | built | [runner-design §14](runner-design.md#14-module-layout-and-phasing) |
| TUI and web TUI | built | [runner-design §11](runner-design.md#11-ui--one-textual-app-terminal-and-web-e3) |
| Periods: seal, lineage anchor, audit, retention | built | README Status; [period-model](period-model.md) |
| Remote relay and shared store for more than one job host | specified only | [concurrency-model §7](concurrency-model.md#7-leadership-relay-takeover) |
| Authentication of a web session | outside dsl41: a reverse proxy or an ssh tunnel provides it | [runner-design §11](runner-design.md#11-ui--one-textual-app-terminal-and-web-e3); [access-model §9](access-model.md#9-the-web-tier) |

## 2. Context

dsl41 runs on one job host.
It reads an estate of JIL files.
It compiles the estate into reports and UC records, or it runs the estate
under AutoSys semantics.
An operator drives it from a shell on that host.
The web TUI listens on loopback by default. A browser reaches it through a
reverse proxy or an ssh tunnel
([deployment-runbook §4](deployment-runbook.md#4-ui-surfaces)).

```mermaid
flowchart LR
  op["operator"]
  br["browser"]
  px["reverse proxy"]
  est["estate<br/>JIL files and properties"]
  sys["host<br/>runs dsl41"]
  uc["UC bundle and migration report<br/>dsl41 uc, dsl41 report"]
  remote["host<br/>remote, specified only"]
  op -->|"compile, run and control commands"| sys
  br -->|"HTTPS with auth"| px
  px -->|"loopback HTTP"| sys
  est -->|"read at compile and at start"| sys
  sys -->|"writes files"| uc
  sys -.->|"multihost relay"| remote
  classDef specified stroke-dasharray: 5 5
  class remote specified
```

The compiler pipeline is drawn in
[ir-design §1](ir-design.md#1-pipeline--representations).
The runner's place in that pipeline is
[runner-design §2](runner-design.md#2-position-in-the-pipeline).
The UC bundle is a file. dsl41 writes nothing to a live controller.

## 3. Containers

The operator starts the engine, and optionally the supervisor, with
`dsl41`. A detached engine starts a missing supervisor itself (DL-210).
The supervisor and the wrappers run by file path, not as a module of the package.
`dsl41 supervise start` and the engine both start the supervisor that way.
The runner keeps state in two stores: the [run root](glossary.md#run-root)
and the lineage [anchor](glossary.md#anchor).

```mermaid
flowchart TB
  op["operator"]
  px["reverse proxy"]
  est["estate"]
  subgraph box["host"]
    comp["compiler<br/>dsl41 lint, dsl41 viz, dsl41 uc"]
    eng["engine<br/>dsl41 run, dsl41 rehearse"]
    sock["control socket<br/>runner_control.py"]
    sup["supervisor<br/>dsl41 supervise, runner_supervisor.py"]
    wr["wrapper<br/>runner_wrapper.py"]
    cmd["command"]
    tui["TUI<br/>dsl41 ui, dsl41 run --ui"]
    web["web TUI<br/>dsl41 serve"]
    cli["headless clients<br/>dsl41 query, dsl41 sendevent, dsl41 host, dsl41 seal"]
    off["offline tools<br/>dsl41 journal, dsl41 runs, dsl41 audit, dsl41 estate prune, dsl41 seal"]
    rr[("run root")]
    anc[("estate anchor")]
  end
  op --> comp
  op --> tui
  op --> cli
  op -->|"dsl41 run"| eng
  op -->|"dsl41 supervise"| sup
  px -->|"one dsl41 ui per browser session"| web
  est --> comp
  est --> eng
  tui --> sock
  web --> sock
  cli --> sock
  sock -->|"served by"| eng
  eng -->|"tethered: starts one per run"| wr
  eng -->|"detached: starts one if absent, then SPAWN, SIGNAL"| sup
  sup -->|"starts one per run"| wr
  wr -->|"spawns and waits"| cmd
  eng -->|"wal, seals, catalogs, perimeter.jsonl"| rr
  sup -->|"receipt.json, reply.json"| rr
  wr -->|"spawn.json, status.json"| rr
  eng -->|"anchor.lock, anchor.json"| anc
  off -->|"read; audit attests, prune deletes"| rr
  off -->|"offline seal takes both locks"| anc
```

- **Compiler.** The compile verbs read JIL and write reports, Mermaid,
  DSL source or a UC bundle. They hold no state between calls.
- **Engine.** `dsl41 run` is one asyncio process per run root. It owns
  the oracle, the WAL and the control socket. `dsl41 rehearse` runs the
  same engine on a virtual clock
  ([rehearse](glossary.md#rehearse)).
- **Control socket.** A Unix socket in the run root. It speaks JSON lines
  ([control-protocol §2](control-protocol.md#2-transport-and-framing-frozen)).
  Every client goes through it, even the TUI that `dsl41 run --ui` runs
  inside the engine's process.
- **Supervisor.** It keeps job processes alive across an engine restart.
  It runs only in [detached](glossary.md#detached) mode. Its socket
  protocol is
  [supervisor-protocol §5](supervisor-protocol.md#5-supervisor-socket-protocol-frozen--phase-11f-dl-48).
- **Wrapper.** One small process per job run. It is the direct parent of
  the command and writes the command's outcome to `status.json`
  ([wrapper](glossary.md#wrapper)). In [tethered](glossary.md#tethered)
  mode the engine starts it. In detached mode the supervisor starts it.
- **TUI and web TUI.** One Textual app, a client of the control socket.
  `dsl41 serve` starts one `dsl41 ui` per browser session and ships no
  authentication.
- **Run root and anchor.** The run root holds the WAL, the spool of each
  run, the seals, the catalogs and the perimeter journal. The anchor, a
  directory outside every run root, holds the estate's lineage. Their
  layout is [period-model §1.1](period-model.md#11-layout).

The service shape, with systemd units, owners and modes, is
[deployment-runbook §0, "Processes, units and storage"](deployment-runbook.md#processes-units-and-storage).
The identities that these stores carry (estate, period, segment, seal,
execution) are the
[period-model identity table](period-model.md#1-identities).

## 4. Engine components

The engine is a functional core inside an imperative shell
([runner-design §3](runner-design.md#3-architecture--functional-core-imperative-shell)).
The oracle is the core. It decides every status change.
The shell adds the clock, the processes, durability and the control
surface. It adds no semantics.

```mermaid
flowchart TB
  ctl["control server<br/>runner_control.py"]
  acc["access perimeter<br/>runner_access.py"]
  loop["engine loop<br/>runner.py"]
  sch["scheduler<br/>runner_scheduler.py"]
  adm["admission<br/>runner_admission.py"]
  orc["oracle core<br/>oracle.py, oracle_state.py"]
  jr["journal<br/>runner_journal.py"]
  ob["outbox<br/>runner_effects.py"]
  ad["adapters<br/>runner_adapters.py"]
  led["leadership fence<br/>runner_ledger.py"]
  st["startup and resume<br/>runner_startup.py"]
  ctl -->|"one decision per request"| acc
  ctl -->|"submit event and envelope"| loop
  sch -->|"calendar ticks"| loop
  ad -->|"completions"| loop
  loop -->|"admit, gate, apply"| adm
  adm -->|"feed in one batch"| orc
  loop -->|"attempt, decision, effect outcome"| jr
  loop -->|"record and dispatch effects"| ob
  loop -->|"launch and cancel runs"| ad
  led -->|"fence before append and dispatch"| loop
  st -->|"genesis, replay, reattach"| loop
```

- **Engine loop.** One asyncio task. It is the only writer of the oracle
  ([runner-design §4](runner-design.md#4-engine-loop--single-writer)).
  Every input, from any source, goes through it.
- **Admission.** The one admission order for every input
  ([concurrency-model §4](concurrency-model.md#4-admission-and-application)).
  It answers an exact retry from the decision index by
  [request_id](glossary.md#request_id).
- **Oracle core.** The AutoSys semantics interpreter
  ([ir-design §7](ir-design.md#7-oracle-interface-semantics-interpreter)).
- **Scheduler.** It turns calendars into STARTJOB inputs
  ([runner-design §5](runner-design.md#5-scheduler--the-calendar-the-oracle-deliberately-lacks)).
- **Journal.** The WAL, one JSONL segment per [period](glossary.md#period),
  and its replay
  ([runner-design §7](runner-design.md#7-journal-and-recovery-e1-prod-grade)).
- **Outbox.** The effects the engine has decided and not yet finished
  ([outbox](glossary.md#outbox);
  [concurrency-model §5](concurrency-model.md#5-effects)).
- **Adapters.** One per job type. The command adapters run the wrapper,
  directly or through the supervisor
  ([runner-design §6](runner-design.md#6-adapters)).
- **Control server.** The socket server and its clients
  ([control-protocol](control-protocol.md)).
- **Access perimeter.** Off unless `dsl41 run --access-map` arms it. Then
  it gives each peer a tier and writes receipts to `perimeter.jsonl`
  ([perimeter](glossary.md#perimeter);
  [access-model §5](access-model.md#5-the-enforcement-point)).
- **Leadership fence and startup.** They take possession of a run root and
  the lineage, and prove both before each append and each dispatch
  ([concurrency-model §7](concurrency-model.md#7-leadership-relay-takeover);
  [period-model §1.3](period-model.md#13-the-successor-fence)).

## 5. One control input, end to end

This is `dsl41 sendevent FORCE_STARTJOB` for a command job, in detached
mode, with the access map armed.
The order is the code's. Arrows name the code's functions and files.

```mermaid
sequenceDiagram
  actor Op as operator<br/>dsl41 sendevent
  participant Ctl as control server<br/>runner_control.py
  participant Acc as access perimeter<br/>runner_access.py
  participant Eng as engine loop<br/>runner.py
  participant Adm as admission<br/>runner_admission.py
  participant Jr as journal<br/>runner_journal.py
  participant Orc as oracle<br/>oracle.py
  participant Ob as outbox<br/>runner_effects.py
  participant Ad as adapter<br/>runner_adapters.py
  participant Sup as supervisor<br/>runner_supervisor.py
  participant Wr as wrapper<br/>runner_wrapper.py
  participant Cmd as command
  Op->>Ctl: status read
  Ctl-->>Op: baseline_id, epoch, state_rev
  Op->>Ctl: sendevent with request_id, expect, epoch
  Ctl->>Ctl: line framing, JSON, protocol version
  Ctl->>Acc: decide for the peer's principal
  Acc-->>Ctl: allowed, receipt in perimeter.jsonl
  Ctl->>Ctl: lineage check, event framing, parse_envelope
  Ctl->>Eng: submit event and envelope
  Eng->>Adm: fingerprint, decision lookup, epoch check
  Eng->>Jr: admit the attempt, WAL append and fsync
  Eng->>Adm: apply_attempt
  Adm->>Orc: batch with due timers, gate, feed
  Orc-->>Eng: emitted STATUS STARTING
  Eng->>Ob: plan_effects, SPAWN with a new run_id
  Eng->>Jr: decision and effects in one record
  Eng->>Ob: record the effect
  Eng-->>Ctl: the decision, through the submit future
  Eng->>Eng: dispatch, fence check
  Eng->>Ad: launch the adapter task
  Eng->>Jr: effect outcome applied
  Ctl-->>Op: the decision
  Ad->>Sup: SPAWN over supervisor.sock
  Sup->>Sup: write receipt.json
  Sup->>Wr: start the wrapper
  Sup-->>Ad: reply, kept in reply.json
  Ad->>Jr: dispatch record
  Wr->>Cmd: spawn
  Wr->>Wr: write spawn.json
  Cmd-->>Wr: exit code
  Wr->>Wr: write status.json, then reap
  Wr-->>Sup: wrapper exits
  Sup-->>Ad: exit push
  Ad->>Ad: read status.json
  Ad->>Eng: completion, STATUS with the exit code
  Note over Eng,Orc: The completion is admitted like the command, with its own WAL records
```

Where the real order differs from a simple list of hops:

- The client reads before it writes. `dsl41 sendevent` reads `status` to
  learn the baseline, the epoch and the revision for its `expect`, unless
  the operator gives them
  ([control-protocol §3](control-protocol.md#3-mutating-verbs-sendevent-host-seal)).
- The access gate runs before the control server routes the request
  ([access-model §5](access-model.md#5-the-enforcement-point)).
- The WAL takes two records per input. The attempt is appended before the
  oracle sees it. The decision and its planned effects are appended
  together after the oracle decides. So the outbox entry is durable before
  any effect runs.
- The answer is the decision. It does not wait for the job to start.
- The engine records the effect as applied when it launches the adapter
  task. That is before the supervisor receives SPAWN. For a SPAWN that
  fails, see [runner-design §3](runner-design.md#3-architecture--functional-core-imperative-shell).
- The supervisor answers SPAWN once the wrapper is started. The wrapper
  writes `spawn.json` a moment later, so these two steps can swap.
- In tethered mode there is no supervisor. The adapter starts the wrapper
  itself and waits on it.
- The completion is an input like any other. It takes the same admission
  order, with a stale-completion gate
  ([concurrency-model §4](concurrency-model.md#4-admission-and-application)).

The spool files are
[supervisor-protocol §3](supervisor-protocol.md#3-spool-format-frozen).
[SPAWN](glossary.md#spawn) idempotency across a supervisor restart is
[period-model §11a](period-model.md#11a-spawn-idempotency-that-outlives-the-supervisor).

## 6. The runnable example: `examples/fulfilment`

The example runs a synthetic warehouse wave under the real engine.
Its [README](../examples/fulfilment/README.md) is the authority for what
to run and what it proves.
It needs the Linux environment of
[examples/workflows](../examples/workflows/README.md) and a disposable
PostgreSQL database in `DATABASE_URL`.
From the repository root:

```sh
python examples/fulfilment/demo.py --run-dir /runs/fulfilment-drop
python examples/fulfilment/demo.py --no-incident --run-dir /runs/fulfilment-normal
```

What each container does in it:

| Container | In the example |
| --- | --- |
| estate | `estate.jil`: one box, `FF_WAVE_B`, and its command jobs. `demo.py` writes the properties that resolve `~{$WORKER}~` and `~{$PROFILE}~`. |
| engine | `demo.py` starts `python -m dsl41 run estate.jil --run-root <run>/engine -p <properties> --timezone UTC`. It runs tethered. |
| supervisor | Not started. The example has no `--detached`. |
| wrapper and command | The engine starts one wrapper per job run. The command is `worker.py`, which works on PostgreSQL and on the carrier. |
| control socket | `<run>/engine/control.sock`. `demo.py` sends `dsl41 sendevent STARTJOB` for the box and polls `dsl41 query status`. After the incident it sends one `FORCE_STARTJOB` for `FF_LABEL_2_C`. |
| run root | `<run>/engine`: the WAL, the spools and the logs. |
| offline reader | After the engine stops, `dsl41 runs --format json` supplies `runs.json`. `check.py` compares it with the worker's attempt rows. |

Two processes in the example are not dsl41: `carrier.py`, a local HTTP
service with a SQLite ledger, and PostgreSQL.
The README's "Operator recovery" section runs the same `query status` and
`sendevent FORCE_STARTJOB` by hand against a live engine.

## 7. Where to read next

- The block cards: [docs/blocks/README.md](blocks/README.md).
- The terms: [glossary](glossary.md).
- Coverage and open findings per machine: [risk map](risk-map.md).
- The runner's design: [runner-design](runner-design.md).
- The frozen runner contracts: [control-protocol](control-protocol.md),
  [supervisor-protocol](supervisor-protocol.md),
  [concurrency-model](concurrency-model.md), [period-model](period-model.md),
  [protocol-evolution](protocol-evolution.md), [access-model](access-model.md).
- The operator path: [deployment-runbook §0](deployment-runbook.md#0-the-operator-path).
- The decisions behind all of it: [decision index](decision-index.md).
