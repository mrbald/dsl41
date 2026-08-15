# dsl41 (codename)

[![tests](https://github.com/mrbald/dsl41/actions/workflows/ci.yml/badge.svg)](https://github.com/mrbald/dsl41/actions/workflows/ci.yml)
[![vulnerabilities](https://github.com/mrbald/dsl41/actions/workflows/audit.yml/badge.svg)](https://github.com/mrbald/dsl41/actions/workflows/audit.yml)
[![secrets](https://github.com/mrbald/dsl41/actions/workflows/secrets.yml/badge.svg)](https://github.com/mrbald/dsl41/actions/workflows/secrets.yml)
[![PyPI](https://img.shields.io/pypi/v/dsl41)](https://pypi.org/project/dsl41/)
[![Python](https://img.shields.io/pypi/pyversions/dsl41)](https://pypi.org/project/dsl41/)
[![license](https://img.shields.io/badge/license-AGPL--3.0%20%7C%20commercial-blue)](LICENSING.md)

dsl41 is a migration compiler for scheduler estates. It contains an AutoSys
(JIL) frontend, a semantic IR, a linter, a Mermaid visualizer, a formal
equivalence validator, and a Stonebranch Universal Controller backend. It
also contains a Python DSL extracted from patterns found in the synthetic
test corpus.

Read these documents in this order:
1. [docs/autosys-semantics.md](https://github.com/mrbald/dsl41/blob/main/docs/autosys-semantics.md) - the meaning of JIL (SEM entries)
2. [docs/stonebranch-semantics.md](https://github.com/mrbald/dsl41/blob/main/docs/stonebranch-semantics.md) - target model + AutoSys->UC mapping (UCS/M entries)
3. [docs/ir-design.md](https://github.com/mrbald/dsl41/blob/main/docs/ir-design.md) - AST / IR-F / IR-G / oracle / equivalence design
4. [docs/jil-statement-syntax.md](https://github.com/mrbald/dsl41/blob/main/docs/jil-statement-syntax.md) - statement scanner spec
5. [docs/decision-log.md](https://github.com/mrbald/dsl41/blob/main/docs/decision-log.md) - the reasons for the decisions
6. [docs/citation-index.md](https://github.com/mrbald/dsl41/blob/main/docs/citation-index.md) - what every reference token in the sources means
7. [CLAUDE.md](https://github.com/mrbald/dsl41/blob/main/CLAUDE.md) - working agreement + implementation order

Operating the runner on a server — install, systemd, web UI exposure,
the JIL-update cycle, upgrades — is
[docs/deployment-runbook.md](https://github.com/mrbald/dsl41/blob/main/docs/deployment-runbook.md).

Status: all ten compiler phases are built and tested. The phase-11 runner
([docs/runner-design.md](https://github.com/mrbald/dsl41/blob/main/docs/runner-design.md))
is also complete: 11a (engine core + bisimulation gate), 11b (process
lifecycle tier: wrapper shim, real adapters, WAL journal, crash-recovery
resume — spool contract frozen in
[docs/supervisor-protocol.md](https://github.com/mrbald/dsl41/blob/main/docs/supervisor-protocol.md)),
11c (calendar scheduler, preflight, control socket — protocol frozen in
[docs/control-protocol.md](https://github.com/mrbald/dsl41/blob/main/docs/control-protocol.md)
— headless CLI), 11d (Textual TUI), 11e (`serve` via textual-serve), and
11f (the detached supervisor tier). Phase 12 — mandatory optimistic
concurrency and multihost execution — is specified and under construction;
its contract is frozen in
[docs/concurrency-model.md](https://github.com/mrbald/dsl41/blob/main/docs/concurrency-model.md). The scheduler obeys AutoSys calendars (DL-56/57). It
applies standard calendar day sets directly. It applies extended
(autocal-rule) calendars through a built-in interpreter of the doc-frozen
SEM-36..39 semantics. The memo below has the source map.

## CLI

There is one entry point (pyproject `[project.scripts]`):
`dsl41 = dsl41.cli:app`. Run `uv run dsl41 --help`, or install the package
and run `dsl41` directly. Every command takes one or more JIL files, which
together form one catalog. The commands accept `autocal_asc` calendar exports
(`calendar` / `cycle` / `extended_calendar` / `ext_calendar` statements)
together with job definitions. All commands share the exit-code contract:
0 = success or clean, 1 = findings (`lint`, `equiv` only), 2 = the input
never reached the tool (unreadable file, JIL parse error, or DL-07 lowering
refusal). `--permit-unknown` is the DL-07 escape hatch on every command: it
carries unknown attributes verbatim instead of a refusal.

### Resolve estate templating (preprocessor)

```sh
dsl41 resolve jobs.jil.tpl -p env.properties -o jobs.jil
```

Estate JIL frequently contains `~{$NAME}~` placeholders. An external
properties mechanism replaces them before the scheduler sees the text. The
`resolve` command does the same step (DL-19). It reads `KEY=VALUE` properties
files. Later files override earlier files. Resolution is an order-independent
fixpoint. If a token stays unresolved, the command reports a loud error. With
`--permit-unresolved`, the command leaves such tokens verbatim. Thus resolved
JIL flows through the ordinary pipeline. The compiler core itself never
models templating.

### Lint a catalog

```sh
dsl41 lint jobs.jil globals.jil            # errors fail (exit 1)
dsl41 lint --strict jobs.jil globals.jil   # warnings fail too
```

The command runs rules L001-L019 (IR-F rules, truth-table rules, graph rules
over the derived graph, dangling-name rules). `--strict` is the migration
gate: do not ship a catalog that lints dirty.

### Visualize the dependency graph

```sh
dsl41 viz jobs.jil -o graph.md             # Markdown report of Mermaid charts
dsl41 viz --direction TD --collapse-threshold 20 jobs.jil
dsl41 viz --elk jobs.jil                   # ELK layout (VS Code; GitHub ignores it)
dsl41 viz --elk --fixed-scale jobs.jil     # uniform chart scale (no fit-to-width)
dsl41 viz --format chart jobs.jil          # one bare Mermaid chart, no report
dsl41 viz --format html jobs.jil -o graph.html     # self-contained page, offline
dsl41 viz --format html-chart jobs.jil -o chart.html  # that chart as a page
dsl41 viz --format explore jobs.jil -o lens.html   # navigation page, offline
```

`--format` picks one of five exclusive outputs — `report` (the default),
`chart`, `html`, `html-chart`, `explore` (DL-75, DL-76). The shaping options
(`--collapse-threshold`, `--direction`, `--include-singletons`, `--elk`,
`--fixed-scale`) apply wherever the chosen format can deliver their effect,
and exit 2 naming the reason where it cannot.

The report shows each independent workflow as its own chart (largest first).
A legend and appendices list everything that the charts omit: standalone
admin-wrapper jobs (charted again with `--include-singletons`), assumed-edge
assumptions, redesign flags, OR shapes, and cycles. In a chart, boxes are
subgraphs, and edges carry their E/A/R migration class
(solid/dashed/thick-red). The charts mark file watchers and schedules as
triggers. Mutual exclusions appear as lock links or as a shared lock hub. If
a box has more direct members than the collapse threshold (default 12), the
box folds into a single node. Any Mermaid renderer works (GitHub,
mermaid.live, IDE preview); `--fixed-scale` adds frontmatter that stops
renderers from fit-to-width scaling each chart differently. `--format chart`
skips the report and emits the entire estate as one bare Mermaid chart,
ready for mermaid-cli or a live editor. `--format html` writes the whole
report as one self-contained page (~5 MB: mermaid + ELK are embedded — see
THIRD_PARTY_LICENSES): charts render in the browser at uniform scale with
pan/zoom, offline, straight from `file://`. `--format html-chart` writes
that same page holding the whole-graph chart alone, with the legend and
without the appendices — the terminal-artifact counterpart to `chart`,
which is bare pipeable text. `--format explore` writes a different kind of
page (~2 MB: cytoscape + ELK + a customElements polyfill embedded — see
THIRD_PARTY_LICENSES): the whole graph as an interactive map — substring
search, click for full edge annotations, and a right-click menu that focuses
a job's fan-in/fan-out (direct, tree, or both) by hiding everything else and
re-laying-out what remains. Edges route orthogonally along the layout axis, so
the picture keeps the layering ELK computed. Chrome, Safari and Firefox all
drive the page, and CI runs it in all three on every push
(tests/test_viz_explore_browser.py): Safari needs a polyfill for the menu
plugin's customized built-in elements, vendored into the page (DL-77), and if
a browser still refuses the menu, the page says so in its status line and
every other control keeps working. It is a
navigation lens for bank-scale estates; the report stays the artifact of
record with the appendices.

### Migration report

```sh
dsl41 report jobs.jil -o report.md
```

The command writes per-catalog markdown from the UC backend: refused (R)
constructs, recorded per-edge assumptions (A rows), and the open U-question
table. After the report is generated, the command always exits 0. The report
itself is the loud channel. Use `lint --strict` as the pass/fail gate.

### Emit UC workflow records (base subset)

```sh
dsl41 uc jobs.jil -o bundle.json            # CREATE-ONLY taskWorkflow records
dsl41 uc --strict jobs.jil                  # exit 1 if anything was quarantined
```

The command emits one `taskWorkflow` record per serializable workflow, in
exactly the shape frozen in
[docs/uc-edge-schema.md](https://github.com/mrbald/dsl41/blob/main/docs/uc-edge-schema.md)
(U3a, DL-55). The records use base edge conditions only (Success / Failure /
Success/Failure), with `retainSysIds: false` and no system ids. If a workflow
contains an edge that the base schema cannot express (a t()-derived
condition, a variable condition), the command quarantines the whole workflow.
The bundle's own ledger lists the quarantined workflow. There is no partial
workflow and no silent edge drop. Rich condition forms and write-path
verification stay blocked on U3b (live controller).

### Prove two catalogs equivalent

```sh
dsl41 equiv new.jil --against old.jil                       # all tiers
dsl41 equiv new.jil -b old.jil --tier c --scripts 50        # more oracle runs
dsl41 equiv new.jil -b old.jil --rename OLD=NEW --case-fold # renamed estate
```

Tier a is structural (canonical-form diff). Tier b enumerates per-job truth
tables. If a state space is too large, tier b defers and never fails. Tier c
compares oracle traces over seeded deterministic event scripts. Identical
canonical hashes short-circuit to equivalent. On any divergence, the exit
code is 1. Typical use: refactor a catalog (by hand or via
decompile-edit-rebuild), then prove that nothing changed.

### JIL -> DSL (decompile)

```sh
dsl41 decompile jobs.jil -o catalog.py
```

The command emits a runnable Python module over the phase-10 builders. When
you run the module, it rebuilds a catalog whose canonical form equals that of
the original (the round-trip property, tested corpus-wide). Recognized
structural patterns fold into builder calls from the closed DL-38 registry,
which `dsl41 folds` lists. `--no-fold` disables the folding.

### DSL -> JIL (build)

The reverse direction is a Python API, not a CLI command:

```python
from dsl41.dsl import CatalogBuilder

b = CatalogBuilder()
b.machine("prod1")
with b.box("nightly"):
    b.job("extract", command="/opt/etl/extract.sh", machine="prod1")
    b.job("transform", command="/opt/etl/transform.sh", machine="prod1")
    b.job("load", command="/opt/etl/load.sh", machine="prod1")
b.sequence("extract", "transform", "load")

jil_text = b.to_jil()   # JIL text, byte-for-byte what the front end accepts
catalog = b.build()     # ...or parse+lower it through the real pipeline
```

`job()` keyword names are JIL attribute names. `sequence()` wires s()-chains,
and `parallel()` wires a fan-out and fan-in. Both refuse to merge into an
existing condition (DL-17: no silent loss). There is no second lowering path.
The builder generates JIL and reuses parse -> lower, so `lint`, `viz`, and
`equiv` all apply unchanged to DSL-built catalogs. The round-trip workflow:
`decompile` an estate to Python, edit it, run the module, and `equiv` the
result against the original.

### Run an estate (phase 11)

```sh
dsl41 run jobs.jil --run-root ./run1            # headless engine + control socket
dsl41 sendevent STARTJOB -J job_a -S ./run1/control.sock
dsl41 query status -S ./run1/control.sock       # JSON: statuses, timers, log paths
dsl41 query status --brief -S ./run1/control.sock   # one line per job, with its rev
dsl41 query global -N GATE -S ./run1/control.sock   # a global's value and rev
dsl41 ui -S ./run1/control.sock                 # attach the TUI; q detaches
dsl41 run jobs.jil --run-root ./run1 --ui       # ...or one terminal owning both
dsl41 rehearse jobs.jil --hours 24              # virtual clock: a day in seconds
dsl41 serve -S ./run1/control.sock              # the same TUI over the web
```

The TUI (jobs table with pending timers and alarms, explain pane with
per-atom condition truth, log tail, sendevent console) is the optional
`[ui]` extra: `pip install 'dsl41[ui]'`. It is a thin client of the run's
control socket. `sendevent`/`query` speak the same protocol. Zooming the
log tail (`m`) turns it into a less-style pager — `/` search, `&` filter,
`n`/`N`, `F` follow — and the operator verbs are unreachable while paging.
`t` opens a read-only triggers view — every pending timer, calendar tick,
and live filewatch with countdowns — and the jobs table marks the armed
latch (SEM-32) as flag `A`.

The scheduler obeys `run_calendar`/`exclude_calendar` (DL-56/57). Standard
calendar day sets apply on the job's local day (run minus exclude, SEM-31).
The built-in autocal rule engine interprets extended calendars (SEM-36..39).
An exhausted calendar makes the job dormant and does not cause an error.
Before the engine starts, preflight (ss8) examines the calendar wiring:
dangling references are errors, and empty or stale calendars cause warnings.

`timezone:` names resolve the vendor's way (SEM-35/DL-62): the zone
database first (case-insensitive), then the instance's ujo_timezones
table — capture it read-only with `autotimezone -l` and pass the listing
via `--timezone-map`. Without a map, a city name such as `Zurich` falls
back to the unique zone whose city component matches (`Europe/Zurich`)
with a preflight WARN. POSIX fixed offsets (`GMT+5`, west-positive) work;
an unresolvable name is a preflight error naming the remedy.

### Detached mode (phase 11f)

By default, a run is **tethered**: if you kill the engine, its jobs terminate
(durably recorded even under `kill -9`, ss6a). If a long-running estate must
survive an engine restart (an upgrade), add `--detached`:

```sh
dsl41 run jobs.jil --run-root ./run1 --detached   # CMD jobs run under a supervisor
# ...stop the engine (SIGINT) -- jobs keep running under the supervisor...
dsl41 run jobs.jil --run-root ./run1 --detached --resume   # reattach, no re-run
dsl41 supervise list --run-root ./run1            # what the supervisor is holding
dsl41 supervise shutdown --run-root ./run1        # stop it (TERM->grace->KILL)
```

A per-run-root supervisor (`runner_supervisor.py`, stdlib-only, one process
per run root) owns the lifelines of the wrappers. Thus the parent of the jobs
is the supervisor, not the engine. If the engine stops or crashes, the jobs
continue to run. `--resume --detached` reconnects and **reattaches** to the
still-alive runs (no reconciliation injection, no re-run). It also resolves,
from the spool, any runs that finished meanwhile. The engine holds a single
fencing lease. The socket protocol of the supervisor is frozen in
[docs/supervisor-protocol.md](https://github.com/mrbald/dsl41/blob/main/docs/supervisor-protocol.md)
ss5. `supervise` is read-only by default (DL-42).

### Serving the TUI over the web (phase 11e)

`dsl41 serve -S ./run1/control.sock` wraps
[textual-serve](https://github.com/Textualize/textual-serve) around the
same app. Every browser tab gets its own `dsl41 ui --socket` subprocess
attached to the run (the ss11 one-instance-per-viewer split). The page shows
this subprocess as a terminal. textual-serve ships **no authentication**, so
the default bind is loopback (`127.0.0.1:8000`). To reach it from a different
host, use a reverse proxy or an SSH tunnel, never a wider `--host`:

```sh
# tunnel: from the operator's machine
ssh -L 8000:localhost:8000 runhost

# or an nginx location block on the run host
location /dsl41/ {
    proxy_pass http://127.0.0.1:8000/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
}
```

Put authentication (basic auth, an OIDC gate, client certificates — whatever
the estate already trusts) in that proxy layer. dsl41 has no authentication
of its own here. The control socket is 0600 from birth (ss10). Thus `serve`
only sees what its own user can already reach directly. It does not widen
access. It makes existing access reachable from a browser.

### Training sandbox (examples/nightbank)

A synthetic bank overnight estate — three regions closing follow-the-sun,
demand-driven refdata, a human approval before the start-of-day flip — for
learning to *operate* the engine: scripted incidents (stalled feeds, hung
jobs, failed loads) that the night does not survive without you. A whole
night plays in ~15 real minutes on the real engine. Start with
[examples/nightbank/README.md](examples/nightbank/README.md) and its
[RUNBOOK](examples/nightbank/RUNBOOK.md) of operator exercises
(`uv run examples/nightbank/bin/nightbank up`). Repo-only; not packaged.

## Implementation memo

All ten phases from the implementation order in CLAUDE.md are implemented and
tested. The build order is: ast_jil, conditions, ir, lint, derive, viz,
oracle, equiv, backend_uc, dsl. Phase 11 (the runner,
[docs/runner-design.md](https://github.com/mrbald/dsl41/blob/main/docs/runner-design.md))
has six tiers, all built:

- 11a — the sans-IO engine loop, VirtualClock, FakeAdapter, and the two
  oracle additions, gated by the ss13 bisimulation suite
- 11b — the process lifecycle tier (per-run wrapper shim,
  LocalCommand/FileWatcher adapters, WAL journal, crash-recovery resume with
  the reconciliation ladder), with the spool contract frozen in
  [docs/supervisor-protocol.md](https://github.com/mrbald/dsl41/blob/main/docs/supervisor-protocol.md)
- 11c — the ss5 calendar scheduler, ss8 preflight, ss10 control socket
  (sendevent parity + queries + subscribe), and the headless
  `run`/`rehearse`/`sendevent`/`query` CLI verbs
- 11d — the ss11 Textual TUI (`dsl41 ui` against a running engine, or
  `dsl41 run --ui`, with the optional `dsl41[ui]` extra)
- 11e — `dsl41 serve` via
  [textual-serve](https://github.com/Textualize/textual-serve), same extra
- 11f — the ss6a Tier-1 supervisor (`dsl41 run --detached`,
  `dsl41 supervise`), a stdlib-only `runner_supervisor.py` that speaks the
  frozen
  [docs/supervisor-protocol.md](https://github.com/mrbald/dsl41/blob/main/docs/supervisor-protocol.md)
  ss5 socket protocol

The suite spans 25 test files (`pytest --collect-only -q` shows the current
count) plus the 27-file synthetic/doc-derived JIL corpus under
`tests/corpus/`.

### Source map

- src/dsl41/__init__.py — module map docstring only (no exports). It records
  the ten-phase build order.
- src/dsl41/ast_jil.py — JIL statement-level scanner + AST + preserve/canonical
  renderers. Fidelity contract F1-F4: byte-exact `render(parse(x)) == x` (F1, fuzzed
  by F3), canonical-mode fixpoint (F2), escaped-colon torture (F4)
- grammars/condition.lark — condition-expression grammar (lark, LALR). Single flat
  `start` rule, & and | at equal precedence, strictly left-associative (Q1
  resolved, DL-53 — the earlier C-style candidate rule is deleted)
- src/dsl41/conditions.py — lark loader + Tree->Cond transformer for
  condition/box_success/box_failure expressions. Lookback + span retention.
- src/dsl41/ir.py — IR-F Pydantic entity models + AST->IR-F lowering. If
  `permit_unknown` is not set, the DL-07 firewall refuses unknown attributes.
  Calendar/cycle repeat-key lanes (`CalendarIR.conditions`, `CycleIR.periods`,
  DL-57) keep real multi-condition/multi-period autocal exports loadable.
- src/dsl41/lint.py — Violation model + rules L001-L019 (pure IR-F rules L001-L005/L015,
  truth-table rules L006/L007 joined in phase 8, graph rules L008-L014 over the derived
  graph, dangling-name rules L016-L018)
- src/dsl41/derive.py — IR-F -> IR-G: seven analysis passes that produce edges, mutex
  pairs, box tree, same-cycle detection, M01-M36 mapping-row classification
- src/dsl41/viz.py — IR-F + IR-G -> Markdown report of per-workflow Mermaid charts (DL-35):
  component split, trigger/lock visual grammar, E/A/R edge-class arrows, collapse
  threshold, appendices for everything that the charts omit
- src/dsl41/viz_html.py — the same report content as one self-contained offline HTML
  page (DL-70): vendored mermaid + ELK from src/dsl41/_vendor/, uniform chart scale,
  progressive in-browser rendering with pan/zoom; the same page around the whole-graph
  chart alone is --format html-chart (DL-76)
- src/dsl41/viz_explore.py — IR-F + IR-G -> cytoscape.js elements for the interactive
  navigation page (DL-71): compound-node box tree, EXT-node synthesis, edge
  annotations in a click-details panel, ELK layout + focus/search in the browser;
  the customElements polyfill loads ahead of the cytoscape bundle and the context
  menu registers last, guarded, so no optional plugin can take the page (DL-77)
- src/dsl41/oracle_state.py — the oracle's state and the vocabulary of the
  events that move it: JobStatus/TERMINAL/EventKind/Event/TraceEntry, the frozen
  JobRuntime and GlobalRuntime rows with the semantic projection that decides
  when a revision moves, RuntimeState (private maps, typed verbs, the timer heap
  with its ordering token, the input transaction) and OracleError. It imports
  nothing from the interpreter, which is why the split exists (DL-91)
- src/dsl41/oracle.py — AutoSys discrete-event semantics interpreter. Script-driven
  completion, edge-triggered re-evaluation, per-SEM-entry trace tests, and
  `InputBatch` — one admitted input as one store transaction
  ([docs/concurrency-model.md](https://github.com/mrbald/dsl41/blob/main/docs/concurrency-model.md)
  §3). The state it moves lives next door in oracle_state.py
- src/dsl41/capacity.py — the DL-50 capacity subsystem: sized buckets (machine
  max_load, resource amounts) and the QUE_WAIT queue with its admission order. It
  decides who may be admitted and in what order; every transition that decision
  implies stays on the Oracle
- src/dsl41/equiv.py — equivalence validator: canonical form + tier a (structural),
  tier b (per-job state-space enumeration), tier c (oracle trace comparison)
- src/dsl41/backend_uc.py and src/dsl41/uc_oracle.py — UC backend pair. backend_uc
  builds the UC twin model, classifies edges, emits the migration report, and
  serializes the U3a base CREATE-ONLY record bundle
  ([docs/uc-edge-schema.md](https://github.com/mrbald/dsl41/blob/main/docs/uc-edge-schema.md) — rich
  condition forms blocked on U3b). uc_oracle is the UC-side twin interpreter that runs
  the P-Mxx expected-divergence pairs against it. It shares Event/TraceEntry with oracle.py.
- src/dsl41/dsl.py — builder surface (job/box/sequence/parallel) + decompiler,
  extracted from corpus-observed patterns only (phase 10, last by design)
- src/dsl41/placeholders.py — non-core estate templating preprocessor (DL-19):
  `~{$NAME}~` resolution from KEY=VALUE properties files (fixpoint, loud on
  residue), behind the `resolve` verb. Nothing in the core imports it.
- src/dsl41/autocal.py — extended-calendar rule interpreter (DL-57): pure
  functions from the opaque CalendarIR/CycleIR carry (DL-36) to day sets per
  the SEM-36..39 doc-freeze — the SEM-37 date-condition keyword inventory,
  the SEM-38 filter-then-replace disposition pipeline (holiday action
  governs holcal dates — Q8a resolved, DL-58), uniform blind `adjust`,
  cycles, dormancy ceilings. Undocumented composition corners run on
  pinned deterministic defaults (`# PENDING: Q8b-Q8d`, DL-59 — refusals
  only for the genuinely uninterpretable, so an ordinary estate always
  schedules). The runner's scheduler and preflight consume it. It is also
  the reference implementation that a live autocal is diffed against (Q8
  residue).
- src/dsl41/runner.py — phase-11 engine: single-writer loop over the oracle
  (dispatch table, time-ordered event queue, stale-completion gate) and the run
  lifecycle (start/resume + the reconciliation ladder). DL-74 split its other
  subsystems into the sibling modules below, with no re-export facade: every
  consumer imports from the module that owns the name
- src/dsl41/runner_control.py — the ss10 control plane, both ends (DL-78): the
  unix-socket server (sendevent parity, status/trace/explain/spec/deps/timers/
  plan, subscribe), the wire vocabulary, and the two clients — a persistent
  async one for the TUI and a one-shot blocking one for the CLI. The protocol
  is frozen in
  [docs/control-protocol.md](https://github.com/mrbald/dsl41/blob/main/docs/control-protocol.md),
  the outer counterpart to the lifecycle tier's supervisor protocol; every
  query handler is a pure projection of oracle state
- src/dsl41/runner_clock.py — the ss9 time domains: the Clock protocol,
  VirtualClock (the engine drives time; adapters may block only on it) and
  RealClock (naive-UTC wall clock). It also holds EngineError, the shell's one
  refusal type, because it is the bottom of the runner import DAG (DL-74)
- src/dsl41/runner_adapters.py — the ss6 adapter contract and every adapter:
  FakeAdapter, LocalCommandAdapter (each command under the ss6a Tier-0 wrapper),
  FileWatcherAdapter, plus the ss6a Tier-1 detached path (SupervisorClient +
  SupervisedCommandAdapter: SPAWN through the supervisor, await the exit push,
  detach-stop vs oracle-kill cancellation, resume-time reattachment) and the ss7
  spool ladder through which both the detached adapter and resume resolve an
  interrupted run's outcome
- src/dsl41/runner_admission.py — phase-12 stages S2+S3: the one order every
  input takes (dedup, stamp, append, apply the time half, decide, record), the
  record types it leaves (Attempt, ApplyResult), the typed Frontiers, the
  DecisionIndex that answers a retry, the envelope fingerprint, and the gate as
  a pure function of state so replay reaches the same verdict the live engine
  did — plus the v2 envelope (parse_envelope) that makes preconditions
  mandatory, in one function rather than one per transport
- src/dsl41/runner_journal.py — the ss7 inputs-only WAL: Journal (header/input/
  advance/result/dispatch/drop/preflight records, append+fsync before every
  feed), read_journal, the two-pass replay_inputs, and catalog_hash, the resume
  gate written into the header
- src/dsl41/runner_scheduler.py — the ss5 calendar scheduler (standard calendar
  day sets and windowed extended-calendar generators, DL-56/57) and the SEM-35
  timezone ladder that turns its ticks into UTC instants (zoneinfo, the
  `--timezone-map` ujo_timezones table, the DL-62 unique-city default, POSIX
  fixed offsets)
- src/dsl41/runner_preflight.py — ss8 preflight: the ERROR/WARN item model and
  its rules — job type, machine resolution through insert_machine (DL-49/52),
  owner, calendars, timezones, resources (DL-50), oracle construction, and the
  AND-success skeleton cycle that disables `plan`
- src/dsl41/runner_supervisor.py — the ss6a Tier-1 supervisor (phase 11f): stdlib-only
  (same enforced boundary as the wrapper), one per run_root. It owns the wrapper
  lifelines, so an engine restart reattaches and does not kill jobs. It speaks the
  frozen docs/supervisor-protocol.md ss5 socket protocol (SPAWN/SIGNAL/LIST/SHUTDOWN/PING +
  lease), with same-uid peer-cred and a Linux subreaper.
- src/dsl41/runner_tui.py — the ss11 Textual TUI (optional `dsl41[ui]` extra): a thin
  client of the control socket only (jobs table with pending timers/alarms, explain
  pane with per-atom truth, log tail of the ss6 std files — a less-style pager when
  focused/zoomed (DL-67), sendevent console). Subscribe is a wake-up signal, and
  every view that the TUI shows comes from the idempotent ss10 queries.
- src/dsl41/runner_wrapper.py — the ss6a Tier-0 per-run lifecycle recorder: stdlib-only
  (enforced DL-42 extraction boundary). It records spawn.json/status.json durably.
  On lifeline EOF, it kills and records. Spool contract in docs/supervisor-protocol.md.
- src/dsl41/runner_procid.py — the durability liturgy (fsync/rename/fsync) and process
  identity (boot id, (pid, start-time) PID-reuse guard, quiet group kill) the wrapper
  and the supervisor share: one stdlib-only module both import by top-level name (DL-72)
- src/dsl41/cli.py — typer entry points: `lint`, `equiv`, `report`, `uc` (the U3a
  record bundle — `--strict` fails on quarantine), `viz`, `decompile`,
  `folds` (the DL-38 fold registry), `resolve` (the DL-19 templating
  preprocessor), `journal` (render-by-replay of a run WAL), `run` (headless executor: wall clock,
  real processes, control socket, stop with SIGINT/SIGTERM, and `--detached` runs CMD
  jobs under a supervisor that survives engine restarts), `rehearse` (virtual
  clock + scripted adapters: a 24h estate in seconds, same engine path), `sendevent`
  and `query` (clients of a running engine's control socket), `supervise`
  (11f: `list`/`shutdown` a run-root's detached supervisor, read-only by default),
  `ui` (the ss11 Textual TUI attached to a running engine — `run --ui` starts both
  in one terminal), and `serve` (11e: wraps textual-serve around the same app, one
  `dsl41 ui` subprocess per browser session — optional `dsl41[ui]` extra, loopback
  by default).
  Exit 2 = catalog load/usage failure everywhere, preflight refusals included.
  Exit 1 = findings for `lint`/`equiv`, and a mid-run engine failure for
  `run`/`rehearse`. `report` always exits 0 once generated: the report itself
  is the loud channel.
- src/dsl41/__main__.py — `python -m dsl41`. It is needed because `serve` spawns
  the app of each session as `<sys.executable> -m dsl41 ui --socket <path>`.
- scripts/arch_check.py — the DL-75 architecture gate CI runs alongside ruff and
  mypy: stdlib-only, no LLM. Blocking checks are objective regressions (a body
  duplicated across modules, a new private cross-module import in src/, a
  citation token with no row in docs/citation-index.md, an IR-F schema change
  without an
  IR_VERSION bump); size checks are advisory and ratcheted against
  scripts/arch_baseline.json. It also prints when a conceptual review is due —
  on a trip, or on 800+ lines changed since the last `arch-review/<date>` tag.

### Tests

- tests/test_ast_fidelity.py — F1-F4 round-trip fidelity, scanner structure and error
  paths, whitespace-sensitive edge cases
- tests/test_condition_grammar.py — grammar-level accept/reject cases, doc-derived only
- tests/test_conditions.py — Cond model shapes, lookback semantics, span retention,
  the `test_sem03_precedence_pinned_model_level` precedence-pinning test (DL-53)
- tests/test_ir.py — IR-F lowering decisions: SEM-30/31/32/33/34, subcommand support
  v1, type-inapplicable attributes
- tests/test_lint.py — L001-L005/L015 rules plus the lint CLI exit-code contract
- tests/test_derive.py — the seven IR-G passes plus the graph-rule lint additions
  L008-L014
- tests/test_viz.py — Mermaid render structure (balanced blocks, id-safety, one golden
  render), the DL-35 markdown report (components, appendices, mutex encodings) plus
  the viz CLI
- tests/test_viz_html.py — the --format html page: chart parity with the markdown report,
  JSON-embedding escape invariant, vendored-asset integrity, page defaults, appendix
  parity; plus the --format html-chart single-chart page (DL-76) and the shaping
  flags it delivers
- tests/test_viz_explore.py — the --format explore page: elements emission (box parents,
  EXT synthesis, edge classes, DL-35 label grammar, untruncated assumptions), the
  same escape invariant, vendored-payload integrity (cytoscape bundle + pinned
  customElements polyfill), script order and the guarded context menu (DL-77),
  CLI flag absorption
- tests/test_viz_explore_browser.py — the same page RUNNING, in chromium, webkit and
  firefox (playwright): the initial ELK layout completes, the toolbar, search, focus,
  re-layout toggle, details panel and context menu all respond, and nothing throws.
  Opt-in (`DSL41_BROWSER_TESTS=1`) and skipped otherwise, so a plain `pytest -q` still
  needs no browsers; CI's explore-page job is where it runs. Locally:
  `uv run playwright install chromium webkit firefox`, then
  `DSL41_BROWSER_TESTS=1 uv run pytest -q tests/test_viz_explore_browser.py`
  (~50 s). Falsifiability is on the record: replayed against the broken tree,
  webkit failed 9/9 on the layout that never completes (DL-77)
- tests/test_oracle.py — AutoSys oracle trace tests against the SEM entries. They
  cite the sparse T-ID index of dossier §8 (T01–T34 range, not contiguous —
  T03/precedence is pinned at parse time in test_condition_grammar.py, not here).
  Every test runs twice — Oracle-direct and Engine(VirtualClock, inert FakeAdapter)
  via tests/bisim_harness.py — the runner-design ss13 bisimulation gate.
- tests/test_resources.py — DL-50 resource-manager tests that need direct Oracle
  access (bucket introspection, the cross-order safety+liveness Hypothesis
  property), outside the bisimulation harness by design
- tests/test_autocal.py — the SEM-36..39 doc-freeze pinned: every worked
  example that the vendor docs contain, plus one test per Q8 pinned default or
  refusal (`test_sem3x_*` / `test_q8x_*` naming)
- tests/test_autocal_breadth.py — breadth over the interpreter, the
  scheduler/preflight wiring, and the ir.py calendar lanes: SEM-37
  token-family coverage, generation edge behavior, every expected date
  derived by hand from the real 2026/2027 Gregorian calendar independently
  of the code under test
- tests/test_runner.py — phase-11a runner suite: oracle additions
  (next_timer_due/advance), VirtualClock, engine dispatch/cancellation/horizon
  discipline, the stale-completion gate, and the feed-only vs advance+feed and
  oracle-vs-engine hypothesis properties
- tests/test_runner_lifecycle.py — phase-11b lifecycle tier: wrapper process matrix
  (pgid separation, parent-loss kills, fd hygiene), the DL-42 phase-boundary kill
  matrix, spoofed-record/boot-flip guards, the engine-SIGKILL crash-recovery
  integration test (tests/runner_crash_driver.py is its engine subprocess), and the
  DL-44 review-finding regressions (kill-wins gate, advance-record replay)
- tests/test_runner_journal.py — WAL record shapes, read_journal tolerance/refusals,
  catalog-hash sensitivity, replay fidelity, journal-first source tagging, and the
  `journal` CLI
- tests/test_admission.py — phase-12 stage S2: the frozen admission order
  ([docs/concurrency-model.md](https://github.com/mrbald/dsl41/blob/main/docs/concurrency-model.md)
  §4). CM-04 (the deadline fires before the gate reads the status it gates on),
  CM-05 (an exact retry takes no index and moves no time) and CM-07 (two-pass
  replay: a durable rejection is not applied, a durable application is not
  re-decided, and an attempt with no result is applied through the gate), plus
  the frontier invariants, the decision index, and the pre-S2 journal that
  replays unchanged
- tests/test_preconditions.py — phase-12 stage S3: mandatory preconditions and
  protocol v2
  ([docs/concurrency-model.md](https://github.com/mrbald/dsl41/blob/main/docs/concurrency-model.md)
  §0/§6). The refusals that make the mandate real (no `expect`, no version, a
  revision from another baseline, an `expect` naming anything but the addressed
  entity), the check itself and the boundary where a timer inside an input's own
  batch does not invalidate it, refused-vs-rejected in the log, the retry and
  epoch ordering of §4 step 2, replay of a rejected precondition, and the wire:
  every door versioned, every read carrying its header, and a sendevent answered
  with its decision rather than its receipt. Then stage S4, the operator's half
  of the same contract: every `ok: false` a mutation can meet says whether it
  was admitted, the shell spends a different exit code on each outcome and is
  told the `request_id` that makes its retry safe, and `query global` gives a
  script the read its `--expect` has to be composed from
- tests/test_runner_adapters.py — RealClock, LocalCommandAdapter end-to-end (SEM-09
  boundary, append/stdin/profile semantics, KILLJOB kill path), FileWatcherAdapter
  steady-size polling under VirtualClock, and the AdapterResult mapping
- tests/test_runner_scheduler.py — phase-11c scheduler occurrence math (days/times/
  start_mins, timezone + DST corners, E10 defaults), engine integration under the
  virtual clock, resume re-anchoring + the E9 missed-tick drops, the ss8
  preflight rule fixture pairs, and the DL-56/58 calendar rules (local-day
  membership, run-minus-exclude, exhaustion dormancy, row-time firing —
  E11 resolved)
- tests/test_runner_control.py — phase-11c control socket (sendevent parity verbs,
  status/trace/explain/plan queries, subscribe backfill/live seam, socket hygiene),
  the DL-45 commit-discipline regression, the run/rehearse/sendevent/query CLI, and
  the DL-46 status-response fields (pending_timers, log paths)
- tests/test_runner_tui.py — phase-11d TUI (skips without the [ui] extra): the
  sendevent console parser, ControlClient against a real ControlServer (round trip,
  reconnect, subscribe), the ss13.6 pilot smokes (table, explain atoms, pending
  timers, log tail, key-driven STARTJOB), and the DL-67 log-pager suite (search,
  line filter, follow, verb-shadowing with its binding-drift guard)
- tests/test_runner_serve.py — phase-11e `serve` CLI: missing-socket and
  missing-extra exit-2 paths, the constructed textual-serve command (a socket
  path with a space is quoted), default loopback bind, bind-failure exit 2 — the
  real textual-serve Server is always monkeypatched (ss13.6 posture, thinner
  still: a CLI wrapper, not a pilot)
- tests/test_runner_supervisor.py — phase-11f supervisor tier: the frozen ss5
  socket protocol (unknown verb / bad version / malformed line, lease held /
  expire / re-acquire fencing monotonicity / stale token, SPAWN idempotency,
  SIGNAL pid-reuse refusal, peer-cred, stale-socket reclaim), the import-boundary
  AST test, Linux-only subreaper, and the detached kill matrix (SIGKILL engine →
  survive + reattach, kill -9 supervisor → spool-resolve TERMINATED, orderly
  SHUTDOWN, detach-stop SIGINT → reattach SUCCESS, oracle KILLJOB detached)
- tests/test_equiv.py — canonical form, tiers a/b/c, the L006/L007 lint rules (tested
  here because they share equiv's truth-table machinery), and the equiv CLI
- tests/test_backend_uc.py — edge classification, migration report, report + uc CLIs,
  the U3a record bundle (frozen-shape golden test, CREATE-ONLY hygiene, quarantine)
- tests/test_uc_oracle.py — UCS-entry trace semantics (UCS-01/02/03/09/13) plus the
  P-Mxx expected-divergence pairs against the UC twin interpreter
- tests/test_dsl.py — the four corpus-extracted builders, cond_to_source fidelity, and
  the decompile round-trip property
- tests/test_placeholders.py — the DL-19 templating preprocessor: every format
  decision in the docstring of placeholders.py pinned, plus the resolved-corpus
  end-to-end run through the ordinary pipeline
- tests/test_arch_check.py — the DL-75 architecture gate: each blocking check
  (duplicate bodies, private cross-module imports, unresolvable citations, an
  IR-F schema change without an IR_VERSION bump) and the advisory size ratchet,
  tripped and not-tripped over tiny synthesised trees
- tests/test_model_harness.py — phase-12 stage H: the concurrency-model
  obligations (CM-01..CM-14,
  [docs/concurrency-model.md](https://github.com/mrbald/dsl41/blob/main/docs/concurrency-model.md)
  §9) that single-host code can be held to today, over tests/model_harness.py —
  a spawn log that outlives an engine crash, so a resume-driven double run is
  visible at all. The `test_harness_*` half pins that the checkers can fail
- tests/test_runtime_state.py — phase-12 stages S1b+S1c: the state owner and
  its revisions
  ([docs/concurrency-model.md](https://github.com/mrbald/dsl41/blob/main/docs/concurrency-model.md)
  §3). Frozen rows, read-only map views, the validating rebuild path, each typed
  verb for what it must and must not change, the timer ordering token, and the
  two invariants that let the capacity pool stay outside the rows; then CM-02
  (one increment per entity per input, and none for an input that changed
  nothing) and CM-03 (the same property over a widened generator, its
  expectation recomputed from the public surface rather than from the
  implementation)

### What's not done

A 2026-07-28 public-doc sweep (DL-53) closed Q1, Q4, Q5 (autosys dossier §9)
and U2, U4, U5, U6a, U7, U8 (stonebranch Part III). Each closure is pinned to
a dossier citation. DL-54 (same day) then resolved Q2a: zero-lookback anchors
to the dependent job's own last end, cited verbatim. DL-54 also flipped the
Q3 default to arm-and-wait. Now, if a false condition or a hold blocks a
scheduled tick, the job arms, and the run is not abandoned. DL-55 (same day)
split U3. U3a, the base CREATE-ONLY workflow record schema, is doc-frozen in
[docs/uc-edge-schema.md](https://github.com/mrbald/dsl41/blob/main/docs/uc-edge-schema.md),
and `dsl41 uc` emits it. U3b (rich condition forms, the live
/resources/openapi.json pull, write-path verification, and the
generated-from-OpenAPI client, DL-08) stays blocked on a live controller.
DL-56/DL-57 (2026-07-28/29) then doc-froze extended-calendar semantics
(SEM-36..39) and made the runner obey calendars. This opened Q8a-Q8e (autocal
generation corners — each a pinned default or refusal in autocal.py), Q9
(which spelling `autocal_asc -E` emits — both accepted meanwhile), and E11
(run_calendar without start_times/start_mins refused fail-closed). A
2026-07-30 verified-citation sweep (DL-58 — vendor KBs and Broadcom-staff
community answers, every citation re-fetched and examined before any pin
moved) then closed Q2b (a never-run dependent satisfies `s(A,0)` — pin
confirmed), Q3 (arm-and-wait confirmed with a no-expiry latch, the abandon
switch deleted, new narrow residue Q3c — whether a member's latch survives
across box runs), Q7 (a present fail_codes decides alone — one corner pin
flipped in `ir.exit_is_success`: unlisted codes are SUCCESS, not
threshold-judged), Q8a (a specified holiday action governs holcal dates —
the disagreement refusal deleted), Q8e (CWEEK = consecutive 7-day chunks
from each period's start), and E11 (row-time firing implemented: calendar
rows' own HH:MM, 00:00 default, job start_times overrides).
DL-59 (same day, a priority decision) then downgraded the remaining
scheduler-path refusals to documented deterministic defaults, so an ordinary
estate always loads and schedules. Q8b runs replace-then-shift, and the Q8d
all-exclusive compounds evaluate literally. docs/live-instance-runbook.md
keeps the probe protocols. If instance access appears, these protocols can
confirm vendor parity. DL-60 (same day) closed Q9 from one observed
`autocal_asc` export sample, which pinned the format (`extended_calendar:`
spelling, empty-valued keys emitted, `workday: all`, braces as condition
grouping, `WORKD#L`, `holiday: S` without holcal, `HH:MM:SS` row tails).
These facts carry the dossier's weakest confidence marker, **[F]**: one
observation, not verified against TechDocs. Five interpreter/scanner gaps
were also corrected the same day. Without the correction, each of these gaps
refuses an ordinary export.
Still open: Q3c, Q6 (narrowed — the ON_ICE atom half is now cited), Q8b-Q8d
(autosys dossier §9), U1, U3b, U6b (stonebranch Part III), and the runner's
E5-E10 (runner-design ss15). E8 was re-swept: a spawn-path signal-9 KB leans
FAILURE, but the mid-run kill still needs a live instance. The questions
with a behavior default in code (Q3c, Q8b-Q8d, U1, U3b, E5-E10) run on a
documented default marked `# PENDING: Qn/Un/En`. Q6 is dossier-only (no code
switch), and U6b lives in the backend_uc migration-report question table.
Q3c, Q6, and Q8b-Q8d need a live AutoSys instance. U3b needs a live UC
controller. The runner is complete through phase 11f (the detached
supervisor tier). The custom-pattern door of the decompiler (`--patterns`
recognizer/expander pairs, agreed alongside DL-38) remains the one
designed-but-unbuilt item.

## Release

Releases are tag-driven. A push of a tag that matches `v*` starts
[.github/workflows/release.yml](https://github.com/mrbald/dsl41/blob/main/.github/workflows/release.yml).
The workflow runs the test suite. Then it builds the sdist and the wheel. Then
it runs `twine check --strict` and publishes to PyPI. Publication uses trusted
publishing (OIDC) in the `pypi` environment. The repository holds no PyPI
token. The header comment of the workflow records the one-time setup on
pypi.org.

The project is before 1.0. A minor bump (0.6.0 -> 0.7.0) carries a functional
unit. A patch bump (0.6.0 -> 0.6.1) carries documentation or a correction with
no behavior change.

### Make a release

First, make sure that the working tree is clean. Make sure that `main` is
pushed. Then run the same gates as CI:

```sh
uv run ruff check src tests
uv run mypy src
uv run pytest -q
```

If the gates pass, set the new version in `pyproject.toml`. Then run `uv lock`.
This command writes the same version into `uv.lock`. Commit both files and push
them:

```sh
git commit -am "chore: X.Y.Z (one-line summary)"
git push origin main
```

Then tag that commit and push the tag:

```sh
git tag -a vX.Y.Z -m "X.Y.Z: one-line summary"
git push origin vX.Y.Z
```

The tag must point at the commit that carries the same version in
`pyproject.toml`. If the two disagree, the tag and the published artifact
describe different trees.

Last, make sure that the `release` workflow is successful. Then read the
project page at https://pypi.org/project/dsl41/.

Note: a local `uv build` writes into the ignored `dist/` directory. It is a
test of the build only. The workflow is the one publication path.

CAUTION: PyPI refuses a second upload of a version that exists. Do not move a
tag after a successful publish. Release the next patch version instead.

## License

dsl41 is dual-licensed:

- **Open source:** [GNU AGPL-3.0-only](LICENSE). If you distribute modified
  versions, or offer them as a network service, you must offer the complete
  corresponding source under the same terms.
- **Commercial:** organizations that cannot accept AGPL obligations can obtain a
  commercial license — see [COMMERCIAL.md](COMMERCIAL.md).

Copyright (C) 2026 dsl41 authors. External contributions require a signed CLA
that preserves the dual-licensing right. Corpus hygiene rules also apply
(see [LICENSING.md](LICENSING.md)).

_Most of the code is written with the assistance of industrial coding agents —
primarily Anthropic's Claude — while the original ideas and design are my own._
