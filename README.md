# dsl41 (codename)

[![tests](https://github.com/mrbald/dsl41/actions/workflows/ci.yml/badge.svg)](https://github.com/mrbald/dsl41/actions/workflows/ci.yml)
[![vulnerabilities](https://github.com/mrbald/dsl41/actions/workflows/audit.yml/badge.svg)](https://github.com/mrbald/dsl41/actions/workflows/audit.yml)
[![secrets](https://github.com/mrbald/dsl41/actions/workflows/secrets.yml/badge.svg)](https://github.com/mrbald/dsl41/actions/workflows/secrets.yml)
[![PyPI](https://img.shields.io/pypi/v/dsl41)](https://pypi.org/project/dsl41/)
[![Python](https://img.shields.io/pypi/pyversions/dsl41)](https://pypi.org/project/dsl41/)
[![license](https://img.shields.io/badge/license-AGPL--3.0%20%7C%20commercial-blue)](LICENSING.md)

dsl41 is a migration compiler and a runner for AutoSys job estates. It reads
JIL, lowers it to a semantic IR, lints it, draws its dependency graph, proves
two catalogs equivalent, and emits Stonebranch Universal Controller (UC)
workflow records. A Python DSL builds catalogs, and a decompiler turns JIL
into that DSL. The runner executes an estate under AutoSys semantics on a
wall clock or a virtual clock. It has a control socket, a terminal UI, a
detached supervisor for jobs that must outlive the engine, and period
boundaries for estates that run for months.

## Documents

To review the design, start with the architecture overview,
[docs/architecture.md](docs/architecture.md), then the
[block cards](docs/blocks/README.md).
For everything else, read these in this order:

1. [docs/deployment-runbook.md §0](docs/deployment-runbook.md#0-the-operator-path) - the operator path: tasks, diagrams, recipes, monitoring and retirement
2. [docs/autosys-semantics.md](docs/autosys-semantics.md) - the meaning of JIL (SEM entries)
3. [docs/stonebranch-semantics.md](docs/stonebranch-semantics.md) - the target model and the AutoSys-to-UC mapping (UCS/M entries)
4. [docs/ir-design.md](docs/ir-design.md) - AST, IR-F, IR-G, oracle, and equivalence design
5. [docs/jil-statement-syntax.md](docs/jil-statement-syntax.md) - the statement scanner spec
6. [docs/decision-log.md](docs/decision-log.md) - the reasons for the decisions
7. [docs/citation-index.md](docs/citation-index.md) - what every reference token in the sources means
8. [docs/simulation-coverage.md](docs/simulation-coverage.md) - what the simulation models, refuses, or assumes
9. [CLAUDE.md](CLAUDE.md) - the shared agent contract and task-specific reading routes

The runner's design is
[docs/runner-design.md](docs/runner-design.md).
Its frozen contracts are
[docs/supervisor-protocol.md](docs/supervisor-protocol.md),
[docs/control-protocol.md](docs/control-protocol.md),
[docs/concurrency-model.md](docs/concurrency-model.md),
[docs/period-model.md](docs/period-model.md),
[docs/protocol-evolution.md](docs/protocol-evolution.md),
and [docs/access-model.md](docs/access-model.md).
A proposed HTTP and WebSocket gateway is specified in
[docs/gateway.md](docs/gateway.md); nothing of it is built.

Three more reader aids sit beside the contracts and hold no rules:
[docs/glossary.md](docs/glossary.md) defines the runner's terms and links
each to the section that defines it,
[docs/risk-map.md](docs/risk-map.md) lists each runner machine with its
branch coverage and open findings, and
[docs/decision-index.md](docs/decision-index.md) lists every decision-log
entry with the docs that cite it.

Agent setup, verification commands, and cross-vendor review recipes are in
[docs/agent-workflow.md](docs/agent-workflow.md).
Operating the runner on a server (install, systemd, web UI exposure, the
JIL-update cycle, upgrades) is
[docs/deployment-runbook.md](docs/deployment-runbook.md).

## Status

The compiler and the runner are built and tested. Three designed items are
not built: the remote relay and shared store that multihost execution needs
([docs/concurrency-model.md](docs/concurrency-model.md)
§7), rich UC condition forms with write-path verification (they need a live
controller), and the decompiler's custom-pattern option (`--patterns`). The
open questions that need a live instance are listed under
[What is not built](#what-is-not-built). The UC backend is descoped until
further notice (DL-288): it stays built and tested, and gets no new work.

## Install

```sh
pip install dsl41          # the compiler and the headless runner
pip install 'dsl41[ui]'    # adds the terminal UI and `serve`
```

Python 3.12 or newer. In a checkout, run `uv sync --frozen --extra dev`,
then `uv run dsl41 --help`.

## CLI

There is one entry point, `dsl41`. Run `uv run dsl41 --help` in a checkout,
or install the package and run `dsl41`. Every compiler command takes one or
more JIL files, which together form one catalog. The files may include
`autocal_asc` calendar exports (`calendar`, `cycle`, `extended_calendar`,
and `ext_calendar` statements) next to job definitions.

Exit codes: 0 is success or clean. 2 means the input never reached the
tool: an unreadable file, a JIL parse error, a lowering refusal, or a
preflight refusal. Findings exit 1: `lint`, `equiv`, `uc --strict`, and a
failed `decompile` check. A mid-run engine failure in `run` or `rehearse`
exits 1 as well. `minify` refusals and a `rehearse --check-cadence`
deviation exit 3. The control verbs `sendevent`, `host`, and `seal` spend a
code per outcome: 0 applied, 2 refused, 3 rejected, 4 outcome unknown. On 4,
retry with the printed `request_id` instead of re-sending. `report` exits 0
once the report is written; the findings are in the report. Each verb's
`--help` states its own codes.

Lowering refuses unknown attributes (DL-07). `--permit-unknown` carries them
verbatim instead. Every command that loads a catalog takes it, except
`minify`, which refuses what it cannot classify. The same commands take
`-p/--properties`, which resolves `~{$NAME}~` placeholders from properties
files before parsing.

### Resolve estate templating

```sh
dsl41 resolve jobs.jil.tpl -p env.properties -o jobs.jil
```

Estate JIL often contains `~{$NAME}~` placeholders that an external
properties mechanism replaces before the scheduler sees the text. `resolve`
does that step. It reads `KEY=VALUE` properties files; later files override
earlier ones. Resolution is an order-independent fixpoint. An unresolved
token is an error, unless `--permit-unresolved` leaves it verbatim. The
compiler core never models templating.

### Lint a catalog

```sh
dsl41 lint jobs.jil globals.jil            # errors fail (exit 1)
dsl41 lint --strict jobs.jil globals.jil   # warnings fail too
```

The rules are L001-L022: IR-F rules, truth-table rules, graph rules over the
derived graph, and dangling-name rules. `--strict` is the migration gate. Do
not ship a catalog that lints dirty.

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

`--format` picks one of five outputs: `report` (the default), `chart`,
`html`, `html-chart`, or `explore`. The shaping options
(`--collapse-threshold`, `--direction`, `--include-singletons`, `--elk`,
`--fixed-scale`) apply wherever the chosen format can deliver their effect.
Where it cannot, the command exits 2 and names the reason; `explore`
delivers all but `--fixed-scale`.

The report shows each independent workflow as its own chart, largest first.
A legend and appendices list everything the charts omit: standalone
admin-wrapper jobs (charted with `--include-singletons`), assumed-edge
assumptions, redesign flags, OR shapes, and cycles. Boxes are subgraphs.
Edges carry their E/A/R migration class as solid, dashed, or thick red
arrows. File watchers and schedules are marked as triggers. Mutual
exclusions appear as lock links or as a shared lock hub. A box with more
direct members than the collapse threshold (default 12) folds into one
node. Any Mermaid renderer works. `--fixed-scale` adds frontmatter that
stops renderers from fit-to-width scaling each chart differently.

`--format chart` emits the whole estate as one bare Mermaid chart for
mermaid-cli or a live editor. `--format html` writes the report as one
self-contained page of about 5 MB, with mermaid and ELK embedded (see
THIRD_PARTY_LICENSES). Charts render in the browser at uniform scale with
pan and zoom, offline, from `file://`. `--format html-chart` writes the same
page around the whole-graph chart alone, with the legend and without the
appendices.

`--format explore` writes an interactive map of the whole graph, about 2 MB,
with cytoscape, ELK, an expand-collapse extension, and a customElements
polyfill embedded (see THIRD_PARTY_LICENSES). It is a navigation aid for
large estates; the report stays the artifact of record. What the page does:

- Click a node or an edge for its full annotations. The details panel shows
  a job's condition text and its AND/OR tree.
- Boxes collapse to one node and expand again: double-click, the corner cue,
  the menu, or two toolbar buttons. `--collapse-threshold` folds the
  over-threshold top-level boxes after the first layout; without it nothing
  folds. A collapse or an expand runs no layout, so the rest of the picture
  stays where it is.
- Navigation has three independent layers. The selection is built by
  clicking, by a substring find that selects or highlights every match, or
  by walking a node's or the selection's fan-in or fan-out, direct or
  transitive, from the right-click menu or the toolbar. The highlight is
  sticky and survives clicks, hiding, and folding. Visibility is "hide
  selected", "hide others", and "show all".
- Shift+drag (or ctrl or cmd) on the background adds a region to the
  selection; plain drag pans. Escape closes an open menu or panel, else
  empties the find field while typing there, else clears the selection.
- The header keeps Find, its two match actions, Fit, and Help. Selection,
  Highlight, Visibility, and View open panels over the canvas. A panel stays
  open for repeat commands; Close, Escape, or an outside click dismisses it.
  Help lists the catalog totals, the gestures, and the legend. The bottom
  status shows counts, a fit-to-selection button, and operation feedback. A
  feature that fails to load shows a persistent notice.
- Every operation over the selection has an HTML button, so the page works
  where the menu plugin cannot load. A control is disabled while the layer
  its operation reads is empty.
- Dragging a selected node moves the whole selection. Otherwise nodes move
  only when a layout runs: at load, on "arrange", or after a hide operation
  when "arrange after hiding" is on (it is off by default). There is no undo.
- "Trace through boxes" (on by default) makes walks follow box semantics: a
  member's fan-in adds every enclosing box and what gates it, its fan-out
  adds what an enclosing box's completion releases. Off, walks follow the
  condition edges alone.
- Incoming arrows are an AND unless the job carries the badge ∨. Then each
  alternative's arrows are hollow, are coloured together when the job is
  clicked, and a label names the alternation where it groups several arrows.
  A bare `n()` is a lock: it draws no arrow and appears in the condition
  tree.
- Locks are drawn and can be switched off. A mutual exclusion is a dotted
  link whose tee marks the waiting job; a clique of three or more is one
  hub. A resource semaphore is a hub per consumed `insert_resource`,
  labelled with its capacity. Locks sit outside the layout, beside their
  members, and take no part in walks.
- Edges route orthogonally along the layout axis.

Chrome, Safari, and Firefox all drive the page, and CI runs it in all three
on pushes to main and on pull requests. If a browser refuses the menu or
the collapse extension, the status line says so and every other control
keeps working.

### Migration report

```sh
dsl41 report jobs.jil -o report.md
```

The report comes from the UC backend. It lists refused (R) constructs, the
assumption recorded on each A-classified edge, and the open U-question
table. Use `lint --strict` as the pass/fail gate.

### Emit UC workflow records

```sh
dsl41 uc jobs.jil -o bundle.json            # CREATE-ONLY taskWorkflow records
dsl41 uc --strict jobs.jil                  # exit 1 if anything was quarantined
```

The command emits one `taskWorkflow` record per serializable workflow, in
the shape frozen in
[docs/uc-edge-schema.md](docs/uc-edge-schema.md).
The records use base edge conditions only (Success, Failure,
Success/Failure), with `retainSysIds: false` and no system ids. A workflow
with an edge the base schema cannot express (a `t()`-derived condition, a
variable condition) is quarantined whole. Two workflows that would emit one
record are quarantined as well. The bundle's ledger lists every quarantined
workflow with its reason. There is no partial workflow and no silent edge
drop. Rich condition forms and write-path verification wait on a live
controller (U3b).

### Prove two catalogs equivalent

```sh
dsl41 equiv new.jil --against old.jil                       # all tiers
dsl41 equiv new.jil -b old.jil --tier c --scripts 50        # more oracle runs
dsl41 equiv new.jil -b old.jil --rename OLD=NEW --case-fold # renamed estate
```

Tier a is structural (canonical-form diff). Tier b enumerates per-job truth
tables; if a state space is too large, tier b defers and never fails. Tier c
compares oracle traces over seeded deterministic event scripts. Identical
canonical hashes short-circuit to equivalent. Any divergence exits 1.
Typical use: refactor a catalog, by hand or by decompile-edit-rebuild, then
prove that nothing changed.

### JIL -> DSL (decompile)

```sh
dsl41 decompile jobs.jil -o catalog.py
```

The command emits a runnable Python module over the DSL builders. Running
the module rebuilds a catalog whose canonical form equals the original's;
this round-trip property is tested over the whole corpus. Recognized
structural patterns fold into builder calls from a closed registry, which
`dsl41 folds` lists. `--no-fold CODE` disables the named fold; the option is
repeatable and accepts comma-separated codes.

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

`build()` returns the in-process catalog every phase and the engine consume.
`to_jil()` returns the durable form. `dsl41 run` and the run root take JIL
bytes only: `python catalog.py > jobs.jil && dsl41 run jobs.jil --run-root ./run1`.

`job()` keyword names are JIL attribute names. `sequence()` wires `s()`
chains; `parallel()` wires a fan-out and a fan-in. Both refuse to merge into
an existing condition. There is no second lowering path: the builder
generates JIL and reuses parse and lower, so `lint`, `viz`, and `equiv`
apply unchanged to DSL-built catalogs.

### De-identify an estate (minify)

```sh
dsl41 minify jobs.jil globals.jil -o minified/     # one file per input
dsl41 minify jobs.jil --mapping names.json         # write the name mapping
```

The mapping file re-identifies the estate. Never commit or share it.

`minify` emits a copy of an estate that still exercises the compiler but no
longer names the estate. The job graph, conditions and their lookbacks,
schedules, exit-code policy, resource gates, and numeric timing hints
survive. Names are renamed into synthetic namespaces, `command` becomes an
inert constant, and comments and observability attributes are dropped.
Every kept value is checked against the closed space its key claims. An
attribute the tool cannot classify stops the run (exit 3) rather than
guessing. A structural verify proves the result isomorphic to the original
under the name mapping. The leak guard is a backstop, not a total check: it
sees tokens of four or more characters that carry a letter. Read the output
before you hand it over.

### Run an estate

```sh
dsl41 run jobs.jil --run-root ./run1            # headless engine + control socket
dsl41 run jobs.jil --run-root ./run1 --resume   # after a stop or a crash: replay, reconcile, go on
dsl41 sendevent STARTJOB -J job_a -S ./run1/control.sock
dsl41 query status -S ./run1/control.sock       # JSON: statuses, timers, log paths
dsl41 query status --brief -S ./run1/control.sock   # one line per job, with its rev
dsl41 query global -N GATE -S ./run1/control.sock   # a global's value and rev
dsl41 host list -S ./run1/control.sock          # the routing table, with revs
dsl41 host drain local -S ./run1/control.sock   # stop routing new work here
dsl41 host activate local -S ./run1/control.sock    # route again; re-dispatch held
dsl41 ui -S ./run1/control.sock                 # attach the TUI; q detaches
dsl41 run jobs.jil --run-root ./run1 --ui       # ...or one terminal owning both; q stops it
dsl41 rehearse jobs.jil --format summary        # virtual clock: a day in seconds
dsl41 rehearse jobs.jil --check-cadence         # run counts vs cadence bounds; exit 3 on deviation
dsl41 rehearse jobs.jil --check-cadence --sweep fail  # + per-producer failure replays
dsl41 rehearse jobs.jil --check-cadence --sweep flags # + per-flag replays of global-gated jobs
dsl41 release-held -S ./run1/control.sock       # estate-wide OFF_HOLD sweep
dsl41 serve -S ./run1/control.sock              # the same TUI over the web
```

`run` executes the estate on the wall clock with real processes, a
write-ahead journal, the calendar scheduler, and a control socket. Stop it
with SIGINT or SIGTERM. `rehearse` drives the same engine under a virtual
clock with scripted adapters, so a day of the estate plays in seconds.
`sendevent` and `query` are clients of the control socket; every mutation
carries a precondition, and a `sendevent` is answered with its decision.

The TUI is the optional `[ui]` extra. It is a thin client of the control
socket: a jobs table with pending timers and alarms, an explain pane with
per-atom condition truth, a log tail, and a sendevent console. Zooming the
log tail (`m`) turns it into a less-style pager with `/` search, `&` filter,
`n`/`N`, and `F` follow; the operator verbs are unreachable while paging.
`t` opens a read-only triggers view of every pending timer, calendar tick,
and live filewatch with countdowns. The jobs table marks an armed latch as
flag `A`. Verb keys act only while the jobs table has focus. Kill and force
ask first, naming the box members and the revision they act on. F1 opens
the help panel.

`host drain` is the maintenance verb: new work stops being dispatched to
that execution host, and work already running finishes. Held jobs are not
failed and not moved. A job is rerun elsewhere only after the host is
evicted, which needs proof that the old executor is dead
([docs/concurrency-model.md](docs/concurrency-model.md)
§8). `query status` marks a held job, because a held job otherwise reads
RUNNING with no process behind it.

The scheduler obeys `run_calendar` and `exclude_calendar`. Standard
calendar day sets apply on the job's local day, run minus exclude. A
built-in autocal rule engine interprets extended calendars. An exhausted
calendar makes the job dormant; it is not an error. Before the engine
starts, preflight examines the calendar wiring: dangling references are
errors, and empty or stale calendars are warnings.

`timezone:` names resolve the vendor's way: the zone database first,
case-insensitive, then the instance's ujo_timezones table. Capture that
table read-only with `autotimezone -l` and pass the listing with
`--timezone-map`. Without a map, a city name such as `Zurich` falls back to
the unique zone whose city component matches (`Europe/Zurich`), with a
preflight warning. POSIX fixed offsets (`GMT+5`, west-positive) work. An
unresolvable name is a preflight error that names the remedy.

Where dsl41's default reading of an estate can differ from AutoSys,
`--semantics NAME=VALUE` selects the other reading and records it with the
run; the switches are listed in "Semantic switches",
[docs/runner-design.md](docs/runner-design.md) §8a.

### Detached mode

By default a run is tethered: if the engine dies, its jobs terminate, and
the termination is recorded even under `kill -9`. If a long-running estate
must survive an engine restart, such as an upgrade, add `--detached`:

```sh
dsl41 run jobs.jil --run-root ./run1 --detached   # CMD jobs run under a supervisor
# ...stop the engine (SIGINT); jobs keep running under the supervisor...
dsl41 run jobs.jil --run-root ./run1 --detached --resume   # reattach, no re-run
dsl41 supervise list --run-root ./run1            # what the supervisor is holding
dsl41 supervise shutdown --run-root ./run1        # stop it (TERM, grace, KILL)
dsl41 supervise start --run-root ./run1           # run the supervisor in the foreground
dsl41 run jobs.jil --run-root ./run1 --detached --deadman 600  # opt into eviction
```

A per-run-root supervisor (`runner_supervisor.py`, stdlib-only, one process
per run root) owns the lifelines of the job wrappers, so the parent of the
jobs is the supervisor, not the engine. If the engine stops or crashes, the
jobs continue. `--resume --detached` reconnects and reattaches to the runs
still alive, with no re-run, and resolves from the spool any run that
finished meanwhile. The engine holds a single fencing lease. The socket
protocol is frozen in
[docs/supervisor-protocol.md](docs/supervisor-protocol.md).
`supervise` is read-only unless you ask it to shut down or start.

`--deadman N` trades some of what `--detached` buys. The supervisor exits
after N seconds with no live controller, and every job it holds dies by
lifeline EOF. An engine down longer than N loses its jobs. That bound is
what makes `dsl41 host evict` provable rather than a guess; without it a
run root is never reroutable except by `--force`. Choose N longer than any
planned engine outage.

### Serve the TUI over the web

`dsl41 serve -S ./run1/control.sock` wraps
[textual-serve](https://github.com/Textualize/textual-serve) around the
same app. Every browser tab gets its own `dsl41 ui --socket` subprocess
attached to the run, shown as a terminal. textual-serve ships no
authentication, so the default bind is loopback (`127.0.0.1:8000`). To reach
it from another host, use a reverse proxy or an SSH tunnel, never a wider
`--host`:

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

Put authentication (basic auth, an OIDC gate, client certificates) in that
proxy layer. dsl41 has none of its own here. The control socket is mode
0600 from birth (0660 when an access map names a socket group), so `serve`
only reaches what its own user can already reach directly. It does not
widen access; it makes existing access reachable from a browser.

### Run history

```sh
dsl41 runs ./run1                            # one row per job run, this root
dsl41 runs ./run1 ./run2 --job extract       # multiple roots, one job
dsl41 runs ./lineage --format json           # a lineage anchor: every period's root, in order
```

`runs` folds run history from a run root's journal, manifest, and spool.
It is offline only: no control socket, no live engine. Pointed at a
lineage anchor directory, it reads every root the estate's archive registry
names, in period order, so a rolled lineage reads as one table. `--format`
is `table` (default), `json`, or `csv`. Every row carries its
`catalog_hash`, so a caller can segment a series by watching that field
change. A run root with no stored inputs reports its rows as
`fidelity=records_only`, with a warning on stderr.

### Period boundaries

A long-lived estate runs as a sequence of periods under a lineage anchor.
The contract is
[docs/period-model.md](docs/period-model.md).

- `seal` closes the running period and commits the next one. It works live
  through the engine, which then exits with code 3, or offline when no
  engine holds the root. The lock decides which.
- `run --resume` reopens the next period in the same root. `run --open-from`
  rolls it into a fresh root; the closing period must be attested first.
- `audit` re-derives a closed period from its opening seal, journal, spool,
  and manifests, and writes its attestation. `verify` checks one.
- `journal` replays a run's write-ahead log into the trace it produced,
  across period boundaries.
- `estate prune` deletes what retention allows and refuses the rest.
  `--archive-inputs` writes an archive receipt, then deletes a period's
  inputs. That step is irreversible: the period can never be re-derived
  again, only checked against its attestation. `estate reclaim --force` is
  the break-glass move of a stale successor claim; prove the claimant is
  gone before you run it.

### Training sandbox (examples/nightbank)

A synthetic bank overnight estate: three regions closing follow-the-sun,
demand-driven refdata, and a human approval before the start-of-day flip.
It exists for learning to operate the engine. Scripted incidents (stalled
feeds, hung jobs, failed loads) need an operator's response. A whole night
plays in about 15 real minutes on the real engine. Start with
[examples/nightbank/README.md](examples/nightbank/README.md) and its
[RUNBOOK](examples/nightbank/RUNBOOK.md) of operator exercises
(`uv run examples/nightbank/bin/nightbank up`). Exercises 15 to 21 cover
the period boundary: sealing a night live and offline, opening the next
period in place, the morning-after `audit` and `verify`, rolling to a fresh
run root, break-glass reclaim, and retention. The `deploy/` directory holds
a launcher script and systemd units for running it as a service. The
example is repo-only; it is not packaged.

CI also uses it to test the concurrency model
([docs/concurrency-model.md](docs/concurrency-model.md)
§9): seeded interleavings of leader failover, a spawn decided and never
acted on, duplicated and stale completions, quarantine, and drain, with a
check that no `(job, run_number)` ever runs twice.

## Implementation memo

The compiler modules build in this order: ast_jil, conditions, ir, lint,
derive, viz, oracle, equiv, backend_uc, dsl (DL-03). The DSL is last by
design. The runner sits on the oracle. There is no re-export facade: every
consumer imports a name from the module that owns it.

### Source map

Front end and IR:

- `src/dsl41/ast_jil.py`: the JIL statement scanner, the AST, and the
  preserve and canonical renderers. Fidelity contract F1-F4: preserve mode
  is byte-exact (`render(parse(x)) == x`), canonical mode is a fixpoint.
- `grammars/condition.lark`: the condition-expression grammar (lark, LALR).
  One flat rule; `&` and `|` have equal precedence and associate left.
- `src/dsl41/conditions.py`: the lark loader and the Tree-to-Cond
  transformer for `condition`, `box_success`, and `box_failure`
  expressions, with lookbacks and span retention.
- `src/dsl41/ir.py`: the IR-F Pydantic entity models and AST-to-IR-F
  lowering. Unknown attributes are refused unless `permit_unknown` is set.
  Calendar and cycle repeat-key lanes keep real autocal exports loadable.
- `src/dsl41/lint.py`: the Violation model and rules L001-L022.
- `src/dsl41/derive.py`: IR-F to IR-G. Seven analysis passes produce the
  edges, mutex pairs, box tree, same-cycle detection, and the M01-M36
  mapping-row classification.
- `src/dsl41/classify.py`: boundary classification, what a catalog change
  does to live work. Pure analysis: no disk, socket, or clock.

Visualization:

- `src/dsl41/viz.py`: the Markdown report of per-workflow Mermaid charts:
  component split, trigger and lock grammar, E/A/R arrows, collapse
  threshold, and the appendices.
- `src/dsl41/viz_html.py`: the same report as one self-contained page
  (`html`), and the whole-graph chart alone (`html-chart`). mermaid and ELK
  are vendored under `src/dsl41/_vendor/`.
- `src/dsl41/viz_explore.py`: the cytoscape.js elements and the page for
  `explore`: the compound-node box tree, external-node synthesis, edge
  annotations, condition shapes, lock hubs, ELK layout, and navigation.

Semantics:

- `src/dsl41/oracle_state.py`: the oracle's state and event vocabulary:
  JobStatus, Event, TraceEntry, the frozen runtime rows, RuntimeState with
  its typed verbs, the timer heap, and the input transaction. It imports
  nothing from the interpreter.
- `src/dsl41/oracle.py`: the AutoSys discrete-event interpreter:
  script-driven completion, edge-triggered re-evaluation, and `InputBatch`,
  one admitted input as one store transaction.
- `src/dsl41/capacity.py`: sized buckets (machine `max_load`, resource
  amounts) and the QUE_WAIT queue with its admission order.
- `src/dsl41/semantics.py`: the closed registry of semantic switches
  (`--semantics NAME=VALUE`). Each default lives in code, and each switch
  names the jobs and calendars it can change (DL-252).
- `src/dsl41/autocal.py`: the extended-calendar rule interpreter: pure
  functions from CalendarIR and CycleIR to day sets, per SEM-36..39.
  Undocumented composition corners run on pinned defaults, so an ordinary
  estate always schedules.
- `src/dsl41/timezones.py`: SEM-35 name resolution (zoneinfo, the
  `--timezone-map` table, the unique-city default, POSIX offsets) and the
  one naive-UTC to local conversion. It imports nothing from dsl41.
- `src/dsl41/equiv.py`: the canonical form and the three equivalence tiers.

Backend and DSL:

- `src/dsl41/backend_uc.py`: the UC twin model, edge classification, the
  migration report, and the base CREATE-ONLY record bundle. The UC-side
  twin interpreter that drives the expected-divergence pairs is
  `tests/uc_oracle.py`.
- `src/dsl41/dsl.py`: the builder surface (`job`, `box`, `sequence`,
  `parallel`) and the decompiler, extracted from corpus-observed patterns
  only.

Tools:

- `src/dsl41/placeholders.py`: `~{$NAME}~` resolution from properties
  files, behind `resolve`. Nothing in the core imports it.
- `src/dsl41/minify.py` and `src/dsl41/minify_rules.py`: the minifier and
  its KEEP/RENAME/REPLACE/DROP table with one value predicate per key. A
  key in no class stops the run.
- `src/dsl41/simulation_register.py` and
  `src/dsl41/simulation_register_rows.py`: the coverage register behind
  [docs/simulation-coverage.md](docs/simulation-coverage.md):
  the row model and the rows as data. A test derives every surface's
  members from the code and fails on a member with no row.
- `src/dsl41/rehearse_check.py`: `rehearse --check-cadence`.

Runner:

- `src/dsl41/runner.py`: the engine: the single-writer loop over the
  oracle, with the dispatch table, the time-ordered event queue, the
  stale-completion gate, admission, and the effect outbox's dispatch.
- `src/dsl41/runner_startup.py`: taking possession of a run root: genesis,
  resume, and the takeover barrier (acquire, replay, reconcile every
  execution host, retire superseded and re-drive pending, dispatch).
- `src/dsl41/runner_clock.py`: the Clock protocol, VirtualClock, RealClock,
  and EngineError.
- `src/dsl41/runner_codes.py`: the stable codes of the control protocol's
  `ok: false` answers, and the typed rejection a gate returns. It imports
  nothing from dsl41.
- `src/dsl41/runner_adapters.py`: the adapter contract and every adapter:
  FakeAdapter, LocalCommandAdapter (each command under the wrapper),
  FileWatcherAdapter, the detached path (SupervisorClient and
  SupervisedCommandAdapter), and the spool ladder that resolves an
  interrupted run's outcome.
- `src/dsl41/runner_admission.py`: the one order every input takes (dedup,
  stamp, append, apply, decide, record), the Attempt and ApplyResult
  records, the decision index that answers a retry, and the envelope with
  its mandatory preconditions.
- `src/dsl41/runner_effects.py`: the effect outbox: what the shell intends
  to do to an execution host, recorded before the attempt, in the states
  pending, applied, and indeterminate.
- `src/dsl41/runner_hosts.py`: the execution-host routing table: the
  HostCommand vocabulary, eviction's preconditions as a pure function of
  the row, and the genesis seed.
- `src/dsl41/runner_access.py`: the access perimeter
  ([docs/access-model.md](docs/access-model.md)):
  the optional `--access-map`, its gates over the control verbs, denial
  receipts, and the privileged ledger. With no map, nothing changes.
- `src/dsl41/runner_history.py`: run history for `runs`, a projection over
  journal, manifest, and spool.
- `src/dsl41/runner_journal.py`: the inputs-only write-ahead log: the
  record kinds, append and fsync before every feed, `read_journal`, the
  two-pass `replay_inputs`, `read_backfill` for a subscriber resuming
  across a boundary, and the segment checks (tail, identity, adjacency).
  Records live in `wal/<segment_no>.jsonl`; `journal.jsonl` holds the
  one-line `period_root` sentinel.
- `src/dsl41/runner_ledger.py`: leadership over one run root: the flock
  held for the process lifetime, the epoch allocated by appending under
  it, and the eligibility gate on the opening record.
- `src/dsl41/runner_scheduler.py`: the calendar scheduler: standard day
  sets and windowed extended-calendar generators, turned into UTC instants
  through `timezones.py`.
- `src/dsl41/runner_preflight.py`: the ERROR/WARN item model and its
  rules: job type, machine resolution, owner, calendars, timezones,
  resources, oracle construction, and the AND-success skeleton cycle that
  disables `plan`.
- `src/dsl41/runner_control.py`: the control plane, both ends: the
  unix-socket server (sendevent parity, queries, subscribe), the wire
  vocabulary, and three clients (persistent async for the TUI, one-shot
  blocking for the CLI, a blocking generator for `subscribe`). The
  protocol is
  [docs/control-protocol.md](docs/control-protocol.md).
- `src/dsl41/runner_supervisor.py`: the detached supervisor: stdlib-only,
  one per run root. It owns the wrapper lifelines and speaks the supervisor
  protocol (SPAWN, SIGNAL, LIST, SHUTDOWN, PING, and the lease), with
  same-uid peer credentials and a Linux subreaper.
- `src/dsl41/runner_wrapper.py`: the per-run wrapper, stdlib-only. It
  records `spawn.json` and `status.json` durably, and on lifeline EOF it
  kills and records.
- `src/dsl41/runner_procid.py`: the durable-write sequence (fsync, rename,
  fsync) and process identity (boot id, pid plus start time, group kill)
  shared by the wrapper and the supervisor.
- `src/dsl41/runner_tui.py`: the Textual TUI, a thin client of the control
  socket. Every view comes from the idempotent queries; subscribe is only
  a wake-up signal.

Period boundary:

- `src/dsl41/period.py`: period identity: `catalog_hash`,
  `source_bundle_hash` and the content-addressed input bundle, the
  RuntimeProfile and its hash, the staged and committed manifests, the
  `segment` record every log opens with, and the archive receipt.
- `src/dsl41/canon.py`: the canonical serialization behind every digest.
- `src/dsl41/state_machine.py`: the shared state-machine core: `Transition`,
  `StateMachine` with its `take` check, `well_formed`, and the hit recording
  the test suite turns on. Standard library only, so the supervisor can load
  it by path.
- `src/dsl41/machines.py`: the registry of declared state machines.
- `src/dsl41/seal.py`: the seal artifact a period ends by writing, and the
  two pure functions over it, `close_runtime` and `open_from_seal`. Every
  section is a frozen model; an unknown section is a refusal.
- `src/dsl41/boundary.py`: the boundary operation: the lineage anchor and
  its head states under a lock, the successor claim, the `period_root`
  sentinel, staging, the retry-horizon gate, validation, the three writes
  in the one order that makes the boundary durable, and seal selection at
  resume.
- `src/dsl41/attest.py`: the attestation `audit` produces and `verify`
  consumes. Producing one re-derives the period's seal from the opening
  seal, the journal, the spool, and the manifests, and needs the
  predecessor checkpoint. Consuming one accepts it alone, so a rolled root
  can verify a chain whose earlier roots are gone.
- `src/dsl41/estate.py`: the physical roll (`run --open-from`): opening a
  lineage's next period in a fresh root.
- `src/dsl41/retention.py`: the retention floors and `estate prune`: what
  may never be deleted, computed from the root, with three verdicts
  (floored, held, prunable). The remover refuses a path that is not
  prunable, is outside the run root, or holds a retained artifact.

CLI and scripts:

- `src/dsl41/cli.py` builds the typer app and registers every verb in help
  order. The verbs live one module per domain: `cli_common.py` (shared
  options, the catalog loader, and the readings that turn an exception or a
  control answer into an exit code), `cli_compile.py`, `cli_run.py`,
  `cli_control.py`, and `cli_estate.py`.
- `src/dsl41/__main__.py`: `python -m dsl41`, which `serve` uses to spawn
  one `dsl41 ui` per browser session.
- `src/dsl41/__init__.py`: the module map docstring. No exports.
- `scripts/arch_check.py`: the architecture gate CI runs next to ruff and
  mypy. Blocking checks: a body duplicated across modules, a new private
  cross-module import under `src/`, a citation token with no row in
  [docs/citation-index.md](docs/citation-index.md),
  a `test_...` name in the docs that no test defines, a module file name in
  the docs that git does not know, a `src/dsl41` module the Source map
  above does not name, and an IR-F schema
  change without an `IR_VERSION` bump. Size checks are advisory, ratcheted
  against `scripts/arch_baseline.json`. It also reports when a conceptual
  review is due, and which specifications under `docs/` are due a spec
  review (`--spec-status` prints the table).
- `scripts/render_decision_index.py`: writes `docs/decision-index.md` from
  the decision log and the tracked docs that cite each entry (DL-276).
- `scripts/render_state_machines.py`: writes `docs/state-machines.md`, a
  diagram and a transition table per registered machine.
- `scripts/transition_coverage.py`: the gate that fails when a declared,
  unmarked transition has no recorded hit.

### Tests

The suite has 105 test files (`pytest --collect-only -q` shows the current
count) and a 31-file synthetic or doc-derived JIL corpus under
`tests/corpus/`. Every oracle trace test runs three times: against the
oracle directly, through the engine under a virtual clock via
`tests/bisim_harness.py`, and against an oracle that applies each input
to a fork first via `tests/fork_harness.py`. The browser tests need
`DSL41_BROWSER_TESTS=1` and installed playwright browsers
(`uv run playwright install chromium webkit firefox`); a plain `pytest -q`
skips them, and CI's explore-page job runs them.

Compiler:

- `tests/test_ast_fidelity.py`: F1-F4 round-trip fidelity, scanner
  structure and error paths, whitespace edge cases.
- `tests/test_condition_grammar.py`: grammar-level accept and reject
  cases, doc-derived only; precedence is pinned here.
- `tests/test_conditions.py`: Cond model shapes, lookback semantics, span
  retention.
- `tests/test_ir.py`: lowering decisions, subcommand support,
  type-inapplicable attributes.
- `tests/test_lint.py`: the IR-F lint rules and the lint CLI exit-code
  contract.
- `tests/test_derive.py`: the seven IR-G passes and the graph-rule lint
  additions.
- `tests/test_viz.py`: Mermaid render structure, the markdown report
  (components, appendices, mutex encodings), and the viz CLI.
- `tests/test_viz_html.py`: the `html` page (chart parity with the report,
  the JSON-embedding escape invariant, vendored-asset integrity, appendix
  parity) and the `html-chart` page.
- `tests/test_viz_explore.py`: the `explore` page's emission: box parents,
  external-node synthesis, edge classes, condition shapes and branch
  labels checked over the whole corpus, lock hubs and links, the escape
  invariant, vendored-payload integrity, script order, and CLI flag
  absorption.
- `tests/test_viz_explore_browser.py`: the `explore` page running in
  chromium, webkit, and firefox: layout completes, and the toolbar, find,
  focus, panels, context menu, collapse, condition badge, branch paint,
  and lock controls respond without throwing.
- `tests/test_oracle.py`: the AutoSys oracle trace tests against the SEM
  entries.
- `tests/test_resources.py`: resource-manager tests that need direct
  oracle access, including the cross-order safety and liveness property.
- `tests/test_semantics.py`: the semantic-switch registry, the
  runtime-profile field that records overrides, the path from the command
  line to the period's pin, and the `ice-lookback` switch across the
  manifest, the engine and replay (DL-252).
- `tests/test_autocal.py`: every worked example the vendor docs contain,
  plus one test per pinned default or refusal.
- `tests/test_autocal_breadth.py`: breadth over the interpreter, the
  scheduler and preflight wiring, and the calendar lanes, with every
  expected date derived by hand from the real calendar.
- `tests/test_timezones.py`: the name-resolution ladder and the naive-UTC
  to local conversion at both DST edges.
- `tests/test_equiv.py`: the canonical form, tiers a, b, and c, the
  truth-table lint rules, and the equiv CLI.
- `tests/test_backend_uc.py`: edge classification, the migration report,
  the report and uc CLIs, and the record bundle (frozen-shape golden test,
  CREATE-ONLY hygiene, quarantine).
- `tests/test_uc_oracle.py`: UCS-entry trace semantics and the
  expected-divergence pairs, driving `tests/uc_oracle.py`.
- `tests/test_dsl.py`: the corpus-extracted builders, condition source
  fidelity, and the decompile round-trip property.
- `tests/test_placeholders.py`: every format decision of the templating
  preprocessor, plus a resolved corpus run through the pipeline.
- `tests/test_minify.py`: the minifier's classes, naming, condition
  rewrite, structural verify, leak guard, and CLI exit codes, with a
  triggering and a non-triggering case per refusal.
- `tests/test_simulation_register.py`: the coverage register's own gate:
  every derived surface's members are computed from the code and must
  match the rows; ids are unique; the checked document matches the rows.
- `tests/test_classification.py`: every classifier tier row with a
  contrast case, the profile-field sweep, both closure directions, and
  nested containment.
- `tests/test_arch_check.py`: each blocking check of the architecture gate,
  the advisory size ratchet, and the spec-review status, tripped and not
  tripped.
- `tests/test_branch_coverage_script.py`: the branch-only coverage report,
  `scripts/branch_coverage.py`, over synthetic coverage JSON (DL-265).
- `tests/test_docs_hygiene.py`: no merge-conflict marker reaches the
  documentation (DL-237).
- `tests/test_docs_links.py`: repository links in the documentation are
  relative and resolve to GitHub's heading ids, the build's rewrite of
  README.md for PyPI (DL-239), and every glossary entry has a link.
- `tests/test_architecture_doc.py`: every diagram label in the
  architecture overview names a real module, CLI verb, process or actor.
- `tests/test_decision_index.py`: the committed decision index is the
  rendering, and the generator's parsing, title and citation rules
  (DL-276).
- `tests/test_state_machine.py`: the core's take check, hit recording and
  well-formedness rules, over small invented machines, and that every
  registered machine is well formed.
- `tests/test_transition_coverage.py`: the transition coverage gate, passing
  and failing, with marks, a missing file and the per-source view.
- `tests/test_state_machines_doc.py`: the committed state-machine doc is the
  rendering, and the generator's diagram and table rules.
- `tests/transition_hits_plugin.py`: the pytest plugin that turns hit
  recording on and writes `.transition-hits.json`.
- `tests/test_examples.py`: the three workflow examples' catalogs lower
  cleanly with their placeholders resolved (DL-237).

Runner:

- `tests/test_runner.py`: the engine loop: timers, VirtualClock, dispatch,
  cancellation, the horizon discipline, the stale-completion gate, and the
  oracle-versus-engine properties.
- `tests/test_runner_lifecycle.py`: the wrapper process matrix, the
  phase-boundary kill matrix, spoofed-record and boot-flip guards, and the
  engine-SIGKILL crash-recovery test (`tests/runner_crash_driver.py` is its
  engine subprocess).
- `tests/test_runner_journal.py`: WAL record shapes, read tolerance and
  refusals, catalog-hash sensitivity, replay fidelity, and the `journal`
  CLI.
- `tests/test_journal_replay.py`: `dsl41 journal` across a boundary, and
  one refusal per way a boundary can fail to prove out.
- `tests/test_run_history.py`: the `runs` CLI: per-root and multi-root
  folding, the filters, a history spanning a boundary, and the
  `records_only` fidelity degrade.
- `tests/test_run_reattach.py`: the reattach line a detached run prints
  on exit, built from the process's own argv.
- `tests/test_ledger.py`: one leader per run root, the monotone epoch, the
  eligibility gate, a SIGKILLed holder, the fence, and the takeover
  barrier.
- `tests/test_runner_leadership.py`: routing and election under real
  processes: the flock refused across processes and released by the
  kernel, the inode fence, the outbox window, and a live eviction bound.
- `tests/test_admission.py`: the frozen admission order, the frontier
  invariants, the decision index, and two-pass replay.
- `tests/test_preconditions.py`: mandatory preconditions and the versioned
  wire: the refusals, the check, refused versus rejected in the log, retry
  ordering, and the operator's exit codes and `request_id`.
- `tests/test_decision_record.py`: the atomic `decision` record: a result
  and its effects commit as one write, the version handshake, and what the
  subscribe stream promises.
- `tests/test_recovery.py`: recovering a mutation whose answer was lost:
  every failure after the write is `delivered`, a torn line at EOF is never
  parsed, and re-running with the printed replay flags is an exact retry.
- `tests/test_hosts.py`: the routing table: drain, the re-drive that makes
  `passive` reversible, held-ness derived rather than stored, eviction's
  refusals and bound, quarantine, and the lease heartbeat.
- `tests/test_effects.py`: the outbox: its three states, supersession by
  exact desired state, at-most-once application, and a drain that holds
  spawns while letting kills through.
- `tests/test_access.py`: the access perimeter's obligations in order,
  from zero-config unchanged to actor overwrite.
- `tests/test_runner_adapters.py`: RealClock, LocalCommandAdapter end to
  end, FileWatcherAdapter polling under VirtualClock, and the result
  mapping.
- `tests/test_runner_scheduler.py`: occurrence math, timezone and DST
  corners, engine integration under the virtual clock, resume
  re-anchoring, missed-tick drops, the preflight rule fixtures, and the
  calendar rules.
- `tests/test_runner_control.py`: the control socket verbs and queries,
  subscribe backfill and the live seam, socket hygiene, and the run,
  rehearse, sendevent, and query CLIs.
- `tests/test_subscriber_bound.py`: on real sockets, a stalled subscriber
  removed at the backlog budget, a healthy one kept, a reconnect from its
  cursor that gets the rest exactly once, and a shutdown that a stalled
  peer cannot hang (DL-267).
- `tests/test_rehearse_check.py`: `rehearse --check-cadence` and its CLI
  wiring over inline estates.
- `tests/test_runner_tui.py`: the TUI (skipped without the `[ui]` extra):
  the console parser, ControlClient against a real server, the pilot
  smokes, the log pager, and the safety suite (table-scoped verb keys,
  confirmations, quit posture, help panel).
- `tests/test_runner_serve.py`: the `serve` and `ui` CLIs: missing socket
  and missing extra, the constructed textual-serve command, loopback bind,
  bind failure, and TUI exit-code propagation. The real server is
  monkeypatched.
- `tests/test_runner_supervisor.py`: the supervisor protocol, the
  import-boundary test, the Linux subreaper, the detached kill matrix, the
  deadman, and a kill re-driven at resume.
- `tests/test_supervisor_backlog.py`: a full macOS listen backlog is not
  evidence of a stale socket.
- `tests/test_supervisor_idempotency.py`: SPAWN idempotency that outlives
  the supervisor, driven through a crash matrix and a real subprocess.
- `tests/test_fw_spool.py`: the file-watcher poll spool from which an
  audit re-derives a watch's progress after a restart.
- `tests/test_runtime_state.py`: the state owner and its revisions: frozen
  rows, typed verbs, the timer ordering token, and one increment per
  entity per input.
- `tests/test_capacity_decomposition.py`: the capacity state on the
  entities it describes and the RuntimeState invariants.
- `tests/test_model_harness.py`: the concurrency-model obligations over
  `tests/model_harness.py`, and the seeded fault sweep whose faults must
  all fire.

Period boundary:

- `tests/test_canon.py`: the canonical form, with a golden vector of exact
  bytes and digest.
- `tests/test_period_identity.py`: the estate layout, the hashes, the
  `segment` record, RuntimeProfile, and the manifests, with golden vectors.
- `tests/test_seal_artifact.py`: the seal sidecar: a golden vector, close,
  open, close reproducing its bytes, tamper detection over every key, the
  ingress refusals, and one injected failure per load invariant.
- `tests/test_boundary.py`: the genesis transaction at each crash point,
  ownership refusals, the crash matrix over the write order, candidate
  reuse and quarantine, the seal barrier, the `seal` control verb, and a
  subscriber resuming across a boundary.
- `tests/test_resume_root_authority.py`: `run --resume` and the offline
  seal refuse a root the anchor does not name or does not own, leaving the
  root untouched.
- `tests/test_estate.py`: the offline and live seal, audit, the producer
  rule's negatives, the physical roll, the attestation gate, and
  break-glass reclaim.
- `tests/test_retention.py`: each retention floor refused and released,
  the structural guards, and the prune flags.
- `tests/test_estate_wide.py`: `audit`, `journal`, `runs`, and
  `estate prune` over a lineage rather than one root.
- `tests/test_restore_drill.py`: backup, delete, restore at the same path,
  and re-open over the nightbank estate.

Branch gate (DL-269): one test per branch of its module that the other
suites do not reach, each asserting the branch's observable effect:

- `tests/test_oracle_branches.py`, `tests/test_capacity_branches.py`,
  `tests/test_scheduler_branches.py`, `tests/test_control_branches.py`,
  `tests/test_seal_branches.py`, `tests/test_period_branches.py`,
  `tests/test_retention_branches.py` and `tests/test_boundary_branches.py`.

Training estate:

- `tests/test_nightbank_example.py`: the estate loads, lints clean, and
  reaches the start-of-day flip on one virtual night, plus the seeded
  interleaving sweep.
- `tests/test_nightbank_boundary.py`: the RUNBOOK's exercises 15 to 21 as
  acceptance scenarios, including a detached night sealed mid-flight under
  a real supervisor, and a check that every verb the RUNBOOK types exists.
- `tests/test_nightbank_deploy.py`: the shipped launcher script and the
  systemd units beside it.
- `tests/test_operator_recipes.py`: the runbook's §0 recipes run as printed
  against a synthetic estate under the shipped launcher, content checks of
  the recipes only the service drill runs, and the reboot hold (DL-268).

## What is not built

- The remote relay and shared store that multihost execution needs. They
  are designed in
  [docs/concurrency-model.md](docs/concurrency-model.md)
  §7.
- Rich UC condition forms, the live OpenAPI pull, write-path verification,
  and the generated client (U3b). They need a live controller. UC work is
  descoped until further notice (DL-288).
- The decompiler's custom-pattern option (`--patterns` recognizer and
  expander pairs).

Open questions run on documented defaults marked `# PENDING: <label>` in
the code. The AutoSys questions Q3c, Q3d, Q6, Q8b-Q8d, Q13, Q14, Q15 and Q16 need
a live AutoSys instance; Q6, Q13 and Q14 have no code switch. The resource-manager questions
Qr2-Qr4 are stated in DL-50; DL-247 narrows Qr2. Qr6 is decided: dsl41
keeps its default, and the `queued-recheck` switch selects the vendor's
readings (DL-257). The UC questions
U1, U3b and U6b are descoped (DL-288); U1 and U3b would need a live
controller, and U6b lives in the migration report's question table. The
runner questions E5-E10 are in
[docs/runner-design.md](docs/runner-design.md)
§15. The probe protocols that would settle them are in
[docs/live-instance-runbook.md](docs/live-instance-runbook.md).

## Release

Releases are tag-driven. A push of a tag that matches `v*` starts
[.github/workflows/release.yml](.github/workflows/release.yml).
The workflow runs the whole CI workflow as its first job, checks that the
tag is annotated and names the version in `pyproject.toml`, builds the
sdist and the wheel, exports the locked dependency closure with hashes,
smoke-tests the installed wheel with
[scripts/release_smoke.sh](scripts/release_smoke.sh),
and publishes only what the smoke tested. Publication uses trusted
publishing (OIDC) in the `pypi` environment, which waits for the owner's
approval. The repository holds no PyPI token. The GitHub release that
follows carries the sdist, the wheel, the two requirements files, and
`SHA256SUMS`. The workflow's header comment lists its jobs and records the
one-time trusted-publisher setup on pypi.org.
The package readme is README.md with every relative link rewritten at
build time to the file on `main`, because the PyPI project page cannot
resolve a relative path.

A minor bump (1.3.0 to 1.4.0) carries one or more functional units. A patch
bump (1.3.0 to 1.3.1) carries documentation or a correction with no
behavior change. A module that was never documented as an API may leave the
package inside a minor bump; the tag message names it.

The annotated tag's message is the release note. Its first line is the
summary. Its body answers the questions that
[the runbook's upgrade section](docs/deployment-runbook.md#7-upgrading-dsl41-itself)
reads to pick an upgrade row, in the order of the template under "Make a
release". It also names any Python module that left the package.

### Make a release

Start from a clean working tree with `main` pushed. Run the
[full local gates](docs/agent-workflow.md#verify-a-change).
The list follows CI, including format checking and the scoped 100% branch
coverage requirement.

Set the new version in `pyproject.toml` and run `uv lock`, which writes the
same version into `uv.lock`. Move the install pin (`ver=`) in
[docs/deployment-runbook.md](docs/deployment-runbook.md)
to the same version. Build locally and compare the wheel's file list with
the previous release's (`uv build`, then `unzip -Z1` on both): the
difference must be what the tag message is about to say. Commit the three
files and push them:

```sh
git commit pyproject.toml uv.lock docs/deployment-runbook.md \
  -m "chore: X.Y.Z (one-line summary)"
git push origin main
```

Rehearse the release on that commit before you tag it. A manual run of the
workflow from a branch runs `gates`, `build`, and `smoke` and publishes
nothing, whatever its `publish` input says. Publication needs a tag: a tag
push, or a manual run from a tag with `publish` set.

```sh
gh workflow run release.yml --ref main
gh run list --workflow release.yml --limit 1
```

The tag checks in `build` run whenever the ref is a tag, on a push or a
manual run.

The smoke test also runs locally, on macOS or Linux, against a local build.
`dist/` must hold only this build's two files:

```sh
uv build --sdist && uv build --wheel
uv export --frozen --no-dev --no-emit-project -o exports/requirements-base.txt
uv export --frozen --no-dev --no-emit-project --extra ui -o exports/requirements-ui.txt
bash scripts/release_smoke.sh dist exports
```

Then tag that commit and push the tag. The first `-m` is the summary; the
others are the release note's body, one line per question of the
runbook's §7, in its order:

```sh
git tag -a vX.Y.Z -m "X.Y.Z: one-line summary" \
  -m "State-machine version: unchanged (or from N to M)." \
  -m "Resume-safe: yes (or no)." \
  -m "Wrapper spec and supervisor protocol: unchanged (or which changed)."
git push origin vX.Y.Z
```

The tag must point at the commit that carries the same version in
`pyproject.toml`. The `build` job refuses a tag that disagrees, and a
lightweight one.

Last, approve the deployment: the `publish` job waits in the run's page for
the owner's approval of the `pypi` environment. Then check that the
`release` workflow succeeded, and read the project page at
https://pypi.org/project/dsl41/ and the GitHub release page for the tag.

A local `uv build` writes into the ignored `dist/` directory, and the
exports above into the ignored `exports/` directory. They test the build
only. The workflow is the one publication path.

PyPI refuses a second upload of a version that exists. Do not move a tag
after a successful publish. Release the next patch version instead.

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
