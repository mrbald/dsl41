# dsl41 (codename)

[![tests](https://github.com/mrbald/dsl41/actions/workflows/ci.yml/badge.svg)](https://github.com/mrbald/dsl41/actions/workflows/ci.yml)
[![vulnerabilities](https://github.com/mrbald/dsl41/actions/workflows/audit.yml/badge.svg)](https://github.com/mrbald/dsl41/actions/workflows/audit.yml)
[![secrets](https://github.com/mrbald/dsl41/actions/workflows/secrets.yml/badge.svg)](https://github.com/mrbald/dsl41/actions/workflows/secrets.yml)
[![PyPI](https://img.shields.io/pypi/v/dsl41)](https://pypi.org/project/dsl41/)
[![Python](https://img.shields.io/pypi/pyversions/dsl41)](https://pypi.org/project/dsl41/)
[![license](https://img.shields.io/badge/license-AGPL--3.0%20%7C%20commercial-blue)](LICENSING.md)

dsl41 is a migration compiler and a runner for AutoSys job estates. It reads
JIL, lowers it to a semantic IR, lints it, draws its dependency graph, and
proves two catalogs equivalent. A Python DSL builds catalogs, and a
decompiler turns JIL into that DSL. The runner executes an estate under
AutoSys semantics on a wall clock or a virtual clock. It has a control
socket, a terminal UI and access roles. Jobs are tethered to the engine.

## Documents

Each document is written for one reader ([docs/writing.md](docs/writing.md),
DL-405). Pick your track; the architect's track is the background reading for
the other three.

**Operator**: start, watch and recover the engine; incidents.

1. [docs/incident-triage.md](docs/incident-triage.md) - on shift: why a job failed, is late or has not started, the intervention and its read-back, the escalation bundle for a dsl41 fault
2. [examples/nightbank/README.md](examples/nightbank/README.md) - the 82-job training estate
3. [examples/file_processing/README.md](examples/file_processing/README.md) - the operator CLI and procedures of the file-processing block
4. [docs/nav.md](docs/nav.md) - inspect job dependencies and box membership from the CLI or an agent

**Job author**: write an estate in JIL or the Python DSL, or bring an existing one in, and get it accepted.

1. [docs/authoring.md](docs/authoring.md) - the author guide: JIL or the Python DSL, what dsl41 accepts, conditions, lint, rehearse, viz, bringing an estate in, the estate manifest and the interruption contract every job meets
2. [Lint a catalog](#lint-a-catalog) - the rules L001-L025, and `rehearse` and `viz` under [CLI](#cli)
3. [DSL -> JIL (build)](#dsl---jil-build) - the Python DSL builders (`job`, `box`, `sequence`, `parallel`) and `to_jil()`; [examples/file_processing/estate.py](examples/file_processing/estate.py) is a worked Python estate; `examples/nightbank`, `fulfilment` and `media` hold worked JIL estates
4. [JIL -> DSL (decompile)](#jil---dsl-decompile) and [Prove two catalogs equivalent](#prove-two-catalogs-equivalent) - bringing an existing estate in and checking a rewrite; [examples/README.md](examples/README.md) indexes the worked estates
5. [docs/autosys-semantics.md](docs/autosys-semantics.md) - the meaning of JIL (SEM entries); [docs/simulation-coverage.md](docs/simulation-coverage.md) - what the runner models, refuses, or assumes; [docs/jil-statement-syntax.md](docs/jil-statement-syntax.md) - the statement scanner spec

**Architect**: the design and the implementation.

1. [docs/architecture.md](docs/architecture.md) - the reader's map, then the [block cards](docs/blocks/README.md), then [docs/glossary.md](docs/glossary.md)
2. [docs/runner-design.md](docs/runner-design.md) - the runner's design; [docs/ir-design.md](docs/ir-design.md) - AST, IR-F, IR-G, oracle, and equivalence; [Implementation memo](#implementation-memo) - the compiler side and the source map
3. [docs/state-machines.md](docs/state-machines.md), [docs/job-chart-lifecycle.md](docs/job-chart-lifecycle.md) and [docs/chart-lifecycles.md](docs/chart-lifecycles.md) - the generated views of every declared chart
4. [docs/gateway.md](docs/gateway.md) - a proposed HTTP and WebSocket gateway; nothing of it is built
5. [docs/decision-log.md](docs/decision-log.md) - the reasons for the decisions, indexed by [docs/decision-index.md](docs/decision-index.md); [docs/citation-index.md](docs/citation-index.md) - what every reference token in the sources means; [docs/risk-map.md](docs/risk-map.md) - each runner machine with its branch coverage and open findings

**Maintainer**: contribute code or documents, or run an agent that does.

1. [CLAUDE.md](CLAUDE.md) - the shared agent contract with role and task reading routes; [docs/agent-workflow.md](docs/agent-workflow.md) - implementation, setup and verification
2. [docs/writing.md](docs/writing.md) - the rules for human-facing documents; [docs/cli-tui-qualification.md](docs/cli-tui-qualification.md) - how a release's CLI and TUI are qualified
3. [docs/live-instance-runbook.md](docs/live-instance-runbook.md) - probing a live AutoSys instance to settle an open semantic question

The runner's frozen contracts are
[docs/control-protocol.md](docs/control-protocol.md),
[docs/protocol-evolution.md](docs/protocol-evolution.md),
[docs/access-model.md](docs/access-model.md),
and [docs/statechart-engine.md](docs/statechart-engine.md).
They are reference, cited from the pages above, not reading.

## Status

The compiler and the runner are built and tested. The items that are
specified and not built, and the open questions, are listed under
[What is not built](#what-is-not-built). The UC backend, the period
boundaries and the release train are retired in 2.0.0.

## Install

```sh
pip install dsl41          # the compiler and the headless runner
pip install 'dsl41[ui]'    # adds the terminal UI and `serve`
```

Python 3.12 or newer. In a checkout, run `uv sync --frozen --extra dev`,
then `uv run dsl41 --help`.

## CLI

There is one entry point, `dsl41`. Run `uv run dsl41 --help` in a checkout,
or install the package and run `dsl41`. Every compiler command except
`build` takes one or more JIL files, which together form one catalog. The
files may include `autocal_asc` calendar exports (`calendar`, `cycle`,
`extended_calendar`, and `ext_calendar` statements) next to job definitions.

Exit codes: 0 is success or clean. 2 means the input never reached the
tool: an unreadable file, a JIL parse error, a lowering refusal, or a
preflight refusal. Findings exit 1: `lint`, `equiv`, a failed `decompile` check, and a `build --check` difference. A mid-run engine
failure in `run` or `rehearse` exits 1 as well. `minify` refusals and a
`rehearse --check-cadence` deviation exit 3. The control verb `sendevent`
spends a code per outcome: 0 applied, 2 refused, 3
rejected, 4 outcome unknown. On 4, retry with the printed `request_id`
instead of re-sending. Each verb's
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
dsl41 lint --timezone America/New_York jobs.jil   # base zone for L023
```

The rules are L001-L025: IR-F rules, truth-table rules, graph rules over the
derived graph, and dangling-name rules. `--strict` is the migration gate: an
estate brought in under
[authoring §5](docs/authoring.md#5-bring-an-existing-autosys-estate-in)
always lints with it, and a site may require it for every estate.

L023 warns when a DST change moves, drops or doubles a schedule time of the
job's zone, and prints what the runner does. A job's own `timezone` decides the zone, else `--timezone` (UTC by
default, as in `dsl41 run`); `--timezone-map` supplies a site alias table.

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
- Node bodies have bounded sizes by kind. Short captions sit below nodes in
  LR layouts and beside them in TD layouts. Expanded boxes keep their title
  bands. View's Labels offers Adaptive (the default) and Always. Adaptive
  hides small captions when zoomed out; selected captions stay opaque.
  Hovering or inspecting a node shows one full name in a callout. Name
  attention changes no layout, routes, lock placement, or Fit bounds.
  Condition, self-lock, and folded-member marks stay on the body.
- Snapshot imports local JSON observations. The minimum shape is
  `{"nodes":{"ID":{"state":"arbitrary string"}}}`. Catalog jobs and external
  nodes match their exact emitted IDs; details shows an external ID when it
  differs from its displayed name. Locks and edges are ineligible. Optional
  top-level and node metadata remain arbitrary JSON, shown as text.
  Native `JSON.parse` uses the last duplicate key at every level; producers
  must emit unique keys. Empty and whitespace states remain exact supplied
  values, distinct from missing observations. State labels use JSON strings.
  Replacement validates every entry before publishing, drops omitted entries,
  and accepts an empty `nodes` object. Invalid input keeps the prior snapshot.
  Browser JSON parsing and text-display limits apply; input that cannot be
  displayed is rejected before replacement.
  Clear and a new file selection invalidate pending reads and errors.
  Generic marks supplied observations neutrally. Optional AutoSys display
  recognizes a small set of literal codes/names with fixed glyphs and tones;
  unknown states stay neutral. Box marks show only their own supplied state.
  Hover and details show coverage of descendant non-box catalog jobs.
  Details also lists raw-state counts. Nested box scopes overlap and must not be summed.
  Snapshot finds raw states or missing catalog observations. Only its explicit
  Select/Highlight actions reveal, expand or fit matches. Import, Clear,
  display, visibility and filtering preserve the graph and its route cache.
  Hiding observations suppresses their canvas/callout/details annotations
  while keeping data inspectable in Snapshot. Unknown and ineligible IDs
  stay inspectable there. The page performs no network access or live polling.
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
  Multiple Find matches also appear as full-name buttons in the details
  panel. Each button selects and inspects that exact match and fits it.
- Shift+drag (or ctrl or cmd) on the background adds a region to the
  selection; plain drag pans. Escape closes an open menu or panel, else
  empties the find field while typing there, else clears the selection.
- View's Edge style offers Orthogonal (the default), ELK routes, and Bezier.
  Single Bezier connections are direct lines; parallel connections curve
  apart. Switching styles keeps node positions, pan, and zoom.
  ELK routes use the layout's bend points and separate connection ports.
  Moving, hiding, revealing, or folding nodes expires those routes and
  temporarily uses Orthogonal paths. Arrange refreshes them. Locks stay
  straight, and ancestor/member edges stay Bezier. The bottom status shows
  routed and fallback counts, or an Arrange action when routes expire.
  A failed layout releases the controls and offers Arrange to retry.
- The header keeps Find, its two match actions, Fit, and Help. Selection,
  Highlight, Visibility, View, and Snapshot open panels over the canvas. A
  panel stays
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

The command emits a Python module over the DSL builders. Its `estate()`
function returns a `CatalogBuilder`; importing the module builds nothing.
The rebuilt catalog's canonical form equals the original's, and this
round-trip property is tested over the whole corpus. Recognized structural
patterns fold into builder calls from a closed registry, which `dsl41 folds`
lists. `--no-fold CODE` disables the named fold; the option is repeatable and
accepts comma-separated codes. Decompile the template JIL, not a bound copy:
`-p` fills the `~{$NAME}~` placeholders first, and the module cannot recover
them.

```sh
dsl41 decompile --params calendars.jil eod.jil -o estate/
```

An `-o` that ends in `/` or names a directory gets a package: one module per
input file and an `__init__.py` whose `estate()` composes them in the order
given; give the manifest's `files` order. Each module builds its own file's
jobs; a condition on another file's job stays an explicit string. The package
names each file by its base name, so a manifest name with a directory, such as
`jobs/eod.jil`, is not supported, and `--emit` names the base name.
`--params` adds a `Params` dataclass with one field per placeholder, each
`None` by default, and `estate(params)` fills the fields that are set
(DL-418).

### DSL -> JIL (build)

```sh
dsl41 build catalog.py -o jobs.jil          # write the JIL
dsl41 build catalog.py --check jobs.jil     # exit 1 and a diff if jobs.jil is stale
```

A Python estate is a module whose `estate()` function returns a
`CatalogBuilder`:

```python
from dsl41.dsl import CatalogBuilder


def estate() -> CatalogBuilder:
    b = CatalogBuilder()
    b.machine("prod1")
    with b.box("nightly"):
        b.job("extract", command="/opt/etl/extract.sh", machine="prod1")
        b.job("transform", command="/opt/etl/transform.sh", machine="prod1")
        b.job("load", command="/opt/etl/load.sh", machine="prod1")
    b.sequence("extract", "transform", "load")
    return b
```

`dsl41 build` imports the module, which runs its code, so use it only on
modules you trust. It calls `estate()`, lowers the JIL through the compiler,
and writes it through a temporary file and a rename. `--check FILE` compares
bytes and writes nothing; `--stdout` prints. For a package, `--out DIR` writes
and `--check DIR` compares one JIL per module under its file's name.
`--param NAME=VALUE` fills a placeholder for a local check; the estate
manifest fills them through its layers. `dsl41 run` and the run root take
JIL bytes only. In Python, the builder's `to_jil()` returns the JIL
text, and its `build()` returns the in-process catalog.

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
dsl41 ui -S ./run1/control.sock                 # attach the TUI; q detaches
dsl41 run jobs.jil --run-root ./run1 --ui       # ...or one terminal owning both; q stops it
dsl41 rehearse jobs.jil --format summary        # virtual clock: a day in seconds
dsl41 rehearse jobs.jil --check-cadence         # run counts vs cadence bounds; exit 3 on deviation
dsl41 rehearse jobs.jil --check-cadence --sweep fail  # + per-producer failure replays
dsl41 rehearse jobs.jil --check-cadence --sweep flags # + per-flag replays of global-gated jobs
dsl41 off-hold-all -S ./run1/control.sock       # OFF_HOLD every job on operator hold
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

An operator hold is `on_hold`: `sendevent ON_HOLD` sets it, and
`sendevent OFF_HOLD` or `off-hold-all` releases it.

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
dsl41 runs ./run1 --format json
```

`runs` folds run history from a run root's journal, manifest, and spool.
It is offline only: no control socket, no live engine. `--format`
is `table` (default), `json`, or `csv`. Every row carries its
`catalog_hash`, so a caller can segment a series by watching that field
change. A run root with no stored inputs reports its rows as
`fidelity=records_only`, with a warning on stderr.

### Training estate (examples/nightbank)

A synthetic bank overnight estate: three regions closing follow-the-sun,
demand-driven refdata, and a human approval before the start-of-day flip.
Scripted incidents (stalled feeds, hung jobs, failed loads) need an
operator's response. Start with
[examples/nightbank/README.md](examples/nightbank/README.md). The example is
repo-only; it is not packaged.

CI also uses it to test the concurrency model
(docs/concurrency-model.md (retired in 2.0.0)
§9): seeded interleavings of engine failover, a spawn decided and never
acted on, and duplicated and stale completions, with a check that no
`(job, run_number)` ever runs twice.

### Recoverable file processing (examples/file_processing)

A building block for estates that process delivered files: an archiver and
a processor as two ordinary CMD jobs behind an existing file watcher, a
SQLite ledger, immutable content-addressed objects, a JSON handover per
work item, and an operator interface that lists, shows and rules stuck
work. The owner's rulings are in
[examples/file_processing/CONTRACT.md](examples/file_processing/CONTRACT.md)
and the design in
[examples/file_processing/README.md](examples/file_processing/README.md)
(DL-404). The library is `dsl41.file_processing`. A job author adds the two
jobs with `add_file_processing_jobs` and deploys
`python -m dsl41.file_processing_worker` with their own handler
([authoring §1.1](docs/authoring.md#11-add-file-processing-to-an-estate)).
The handler and operator CLIs under `examples/` are samples. The README's
exercise runs the example estate under the real engine and supervisor on one
host, through a stop and an operator's rejection.

## Implementation memo

The compiler modules build in this order: ast_jil, conditions, ir, lint,
derive, viz, oracle, equiv, backend_uc, dsl (DL-03); backend_uc is retired in
2.0.0. The DSL is last by design. The runner sits on the oracle. There is no re-export facade: every
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
- `src/dsl41/lint.py`: the Violation model and rules L001-L025.
- `src/dsl41/derive.py`: IR-F to IR-G. Seven analysis passes produce the
  edges, mutex pairs, box tree, same-cycle detection, and the M01-M36
  mapping-row classification.
- `src/dsl41/nav.py`: pure job navigation over IR-G: dependency neighbors,
  transitive reachability, paths and box membership.
- `src/dsl41/cli_nav.py`: the `nav` command, JSON and text output, and
  navigation from a decompiled module's in-memory estate.

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
- `src/dsl41/job_chart.py`: the composite `job` statechart: one job's status,
  flags and holding in one declaration, the one source of a job's moves;
  the oracle drives it for STATUS, the starts, KILLJOB, the TIMER input,
  the flags, RELEASE_RESOURCE, the readmission, the box rules, a box's
  ON_NOEXEC on the jobs it contains and the cascade's moments. It holds the
  contract, the guards and the actions; its two other parts are
  `src/dsl41/job_chart_events.py` (the events, the data and the payloads) and
  `src/dsl41/job_chart_rows.py` (the states, the rows and a row's
  configuration), and each name is imported from the module that defines it.
- `src/dsl41/scan_chart.py`: the `queue_scan` statechart, the queue scan's
  one coordinator per oracle: its passes, each waiter's turn behind its
  readmission's cascade, a scan asked inside a pass, and the upgrade of an
  admit-only scan to a full one.
- `src/dsl41/oracle.py`: the AutoSys discrete-event interpreter, `Oracle`:
  script-driven completion, edge-triggered re-evaluation, and `InputBatch`,
  one admitted input as one store transaction. Every job row moves through
  the `job` chart: each input a root item, its cascade on the executor's
  worklist, and a hook that writes each microstep's moves and stops a
  cascade that does not end at an item cap. The engine, every replay,
  preflight and equiv build it; it reproduces the golden digests. It is
  built in layers, one module each:
  - `src/dsl41/oracle_reads.py`: the store, the trace and the reads:
    condition truth, box membership, admission, an ignored flag input.
  - `src/dsl41/oracle_schedule.py`: the clock's reads and the timers:
    run_window, deferrals, the queued recheck's calendars, the deadlines.
  - `src/dsl41/oracle_cascade.py`: the cascade: what each row sends beyond
    its own move, the moments that iterate, and the gates that choose each
    rule's audience. A box's end FAILURE or TERMINATED kills its live
    job_terminator members only while the run that ended is still the
    box's run.
  - `src/dsl41/oracle_writers.py`: the hook's write for each row the chart
    can fire, and the error for a fired row it has no write for.
- `src/dsl41/oracle_facts.py`: the payloads the oracle gives the `job`
  chart, each oracle fact computed on its first read, and a job instance's
  data.
- `src/dsl41/oracle_intents.py`: the cascade's payloads, the moments'
  steps, the queue scan's record, and the intents a row's action reports
  for the hook.
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

- `src/dsl41/dsl.py`: the builder surface (`job`, `box`, `sequence`,
  `parallel`) and the decompiler, extracted from corpus-observed patterns
  only.
- `src/dsl41/file_processing.py`: the recoverable file-processing workers,
  `archive` and `process`, the handler and reconciler protocols, and the
  operator interface (`list_work`, `show_work`, `rule_work`) over one
  privately managed root: inbox, immutable objects, TODO handover, SQLite
  ledger with an append-only decision record, one lock per role (DL-404).
- `src/dsl41/file_processing_charts.py`: the flat `file_archive` and
  `file_process` charts run by the executor; every ledger state write is
  the executor's leaf. Not runner machine registrations.
- `src/dsl41/file_processing_dsl.py`: `add_file_processing_jobs`, the two
  CMD jobs (archiver on the watcher, processor on the archiver with zero
  lookback) added to an existing Python estate.
- `src/dsl41/file_processing_worker.py`: the deployed worker command,
  `python -m dsl41.file_processing_worker archive|process`, which loads the
  job author's handler from `--handler MODULE:FACTORY` and maps outcomes to
  the contract's exit codes.

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
  stale-completion gate, admission, and dispatch behind a `dispatch`
  record.
- `src/dsl41/runner_startup.py`: taking possession of a run root: genesis
  and resume (acquire, replay, reconcile the spool, dispatch the starts
  the previous engine decided and never launched).
- `src/dsl41/runner_clock.py`: the Clock protocol, VirtualClock, RealClock,
  and EngineError.
- `src/dsl41/runner_codes.py`: the stable codes of the control protocol's
  `ok: false` answers, and the typed rejection a gate returns. It imports
  nothing from dsl41.
- `src/dsl41/runner_adapters.py`: the adapter contracts and every adapter:
  FakeAdapter, LocalCommandAdapter (each command under the wrapper, observed
  by the attempt tier), FileWatcherAdapter, the spool readers and the stop
  ladder.
- `src/dsl41/runner_admission.py`: the one order every input takes (dedup,
  stamp, append, apply, decide, record), the Attempt and ApplyResult
  records, the decision index that answers a retry, the envelope with
  its mandatory preconditions, and the dispatch plan a decision implies.
- `src/dsl41/runner_access.py`: the access perimeter
  ([docs/access-model.md](docs/access-model.md)):
  the optional `--access-map`, its gates over the control verbs, denial
  receipts, and the privileged ledger. With no map, nothing changes.
- `src/dsl41/runner_history.py`: run history for `runs`, a projection over
  the store's records, its configuration row, and the spool.
- `src/dsl41/runner_journal.py`: the engine's record over its store: the
  record kinds, the one writer (an admission is one transaction), the
  projection onto the subscribe wire, the open table of attempts, pending
  starts and follow-up plans, the snapshot, `read_journal`, `replay_inputs`
  and `read_backfill`.
- `src/dsl41/runner_store.py`: the engine's only durable record: one SQLite
  file per run root, `engine.db`, holding an append-only journal of
  canonical records, an append-only configuration history, snapshots and
  pruning, under one writer with synchronous commits; `MemoryStore` is its
  in-memory twin for rehearsal and tests.
- `src/dsl41/runner_config.py`: what an engine runs under:
  `RuntimeProfile` and `runtime_hash`, the configuration row that holds the
  catalog, the profile and the marked set, and `engine.toml`'s stop grace,
  snapshot cadence and retention.
- `src/dsl41/idempotence.py`: the idempotence declaration:
  `non-idempotent.toml` in the manifest's directory names the jobs whose interrupted
  attempt waits for the operator, with its canonical bytes for the reload
  token, and the four `DSL41_*` variables each `dispatch` record carries.
- `src/dsl41/runner_ledger.py`: one engine per run root: the flock held
  for the process lifetime, its holder note, and the state-machine
  version a store and its snapshots carry.
- `src/dsl41/runner_scheduler.py`: the calendar scheduler: standard day
  sets and windowed extended-calendar generators, turned into UTC instants
  through `timezones.py`.
- `src/dsl41/runner_preflight.py`: the ERROR/WARN item model and its
  rules: job type, machine resolution, owner, calendars, timezones,
  resources, oracle construction, and the AND-success skeleton cycle that
  disables `plan`.
- `src/dsl41/reload_diff.py`: the redeploy diff as pure functions over two
  catalogs: a job's definition hash without source spans, the added,
  removed, changed and unchanged sets with the reason for an indirect
  change, the running-box check, and the plan token.
- `src/dsl41/reload_plan.py`: `reread` as a module: it loads the estate from disk, lints it under `--strict`, diffs it against
  the live catalog the engine supplies, refuses a running box, and stores
  the candidate files and `plan.json` under `plans/<token>/`, the plan file
  last.
- `src/dsl41/reload_update.py`: the 2.x update machine: the
  declared `UPDATE` statechart (IDLE, STOPPING, APPLIED, REFUSED), the
  admission that re-checks the revision, the running boxes and the stop set,
  the tier that journals the intent, stops each job and applies, and the
  application's per-job carry onto the oracle's state.
- `src/dsl41/runner_update.py`: the engine's side of the update: the
  request as one step of the single writer, the candidate loaded from the
  plan's own bytes, the stops through the attempt tier, the dispatch filter,
  the outcomes kept back until the application, and the `config intent` and
  `config applied` records with the new configuration row.
- `src/dsl41/runner_control.py`: the control plane, both ends: the
  unix-socket server (sendevent parity, queries, subscribe), the wire
  vocabulary, and three clients (persistent async for the TUI, one-shot
  blocking for the CLI, a blocking generator for `subscribe`). The
  protocol is
  [docs/control-protocol.md](docs/control-protocol.md).
- `src/dsl41/runner_wrapper.py`: the per-run wrapper, stdlib-only. It
  records `spawn.json` and `status.json` durably, and on lifeline EOF it
  kills and records.
- `src/dsl41/runner_procid.py`: the durable-write sequence (fsync, rename,
  fsync) and process identity (boot id, pid plus start time, group kill)
  shared by the wrapper and the supervisor.
- `src/dsl41/runner_tui.py`: the Textual TUI, a thin client of the control
  socket. Every view comes from the idempotent queries; subscribe is only
  a wake-up signal.

Shared cores:

- `src/dsl41/canon.py`: the canonical serialization behind every digest.
- `src/dsl41/state_machine.py`: the statechart executor of
  `docs/statechart-engine.md`: `Statechart` declarations, the `Executor`,
  its errors under `StatechartError`, `well_formed`, and the hit recording
  the test suite turns on. Standard library only, so the supervisor can load
  it by path.
- `src/dsl41/machines.py`: the registry of declared state machines.
- `src/dsl41/engine_charts.py`: the 2.x engine lifecycle and attempt
  statecharts: STARTING, RUNNING, DRAINING, STOPPED for the engine;
  SPAWNING, RUNNING, STOPPING, ENDED for one attempt, with the outcome
  derived from the wrapper's exit record.
- `src/dsl41/runner_attempts.py`: the 2.x attempt and engine lifecycle tiers:
  the exit record from `status.json`, the missing-record verdict, the oracle
  mapping, the interruption rule's plans, and the tiers that post the two
  charts' events and carry out their intents through injected protocols.
- `src/dsl41/runner_stops.py`: the engine's side of those tiers: the stop
  ladder on a recorded group, the tiers' timers, an attempt's spool reads,
  and the hand-back of each outcome and lifecycle intent to the engine.
- `src/dsl41/estate_manifest.py`: the estate manifest, `estate.toml`: the
  application's one declaration of its catalog files, properties layers,
  timezone and semantic switches, read by the verbs' `--manifest` option.

CLI and scripts:

- `src/dsl41/cli.py` builds the typer app and registers every verb in help
  order. The verbs live one module per domain: `cli_common.py` (shared
  options, the catalog loader, and the readings that turn an exception or a
  control answer into an exit code), `cli_compile.py`, `cli_run.py`,
  and `cli_control.py`.
- `src/dsl41/cli_reload.py`: the 2.x `reread` and `update` verbs: their
  rendering and exit codes, with the engine reached through one function
  bound to the control socket's `catalog` and `update` verbs.
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
  diagram with regions, choices and deferrals, and the row, branch and state
  tables, per registered statechart.
- `scripts/render_job_lifecycle.py`: writes `docs/job-chart-lifecycle.md`, the job
  chart's lifecycle, flags and holding as one line per transition, with the
  folded rows counted, and `docs/chart-lifecycles.md`, the same view of every
  other registered chart (`--chart NAME` prints one chart's view, `--full` the
  folded rows too).
- `scripts/transition_coverage.py`: the gate that fails when a statechart's
  unmarked row, branch, guard outcome, deferral or release has no recorded
  hit.

### Tests

The suite has 126 test files (`pytest --collect-only -q` shows the current
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
- `tests/test_oracle_goldens.py`: the oracle reproduces the golden digests
  recorded before DL-351 over the goldens' sample (`tests/oracle_goldens.py`,
  its inputs and renderings in `tests/oracle_inputs.py`; CI replays the whole
  sample).
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
- `tests/test_dsl.py`: the corpus-extracted builders, condition source
  fidelity, and the decompile round-trip property.
- `tests/test_build_cli.py`: the decompiled module's `estate()` shape, the
  `build` verb's write, print and check modes, and its refusals.
- `tests/test_placeholders.py`: every format decision of the templating
  preprocessor, plus a resolved corpus run through the pipeline.
- `tests/test_minify.py`: the minifier's classes, naming, condition
  rewrite, structural verify, leak guard, and CLI exit codes, with a
  triggering and a non-triggering case per refusal.
- `tests/test_simulation_register.py`: the coverage register's own gate:
  every derived surface's members are computed from the code and must
  match the rows; ids are unique; the checked document matches the rows.
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
- `tests/test_state_machine.py`: the executor's hit recording over a small
  invented chart, the session plugin, the standard-library boundary, and
  that every registered machine is well formed.
- `tests/test_transition_coverage.py`: the transition coverage gate, passing
  and failing, with marks and a missing file.
- `tests/test_state_machines_doc.py`: the committed state-machine doc is the
  rendering, and the generator's diagram and table rules.
- `tests/transition_hits_plugin.py`: the pytest plugin that turns hit
  recording on and writes `.transition-hits.json`.
- `tests/leak_detector_plugin.py`: the pytest plugin that fails a test which
  leaves global process state changed: signal handlers, the environment, a
  child process not yet reaped, a live non-daemon thread, and the rest it
  lists.
- `tests/test_leak_detector.py`: each of the leak detector's probes names a
  leak made on purpose, and the plugin fails a leaking test in a real run.
- `tests/test_statechart.py`: the executor's conformance to
  `docs/statechart-engine.md`, one test per rule, named by section, with a
  mutant check that breaks one rule at a time.
- `tests/test_statechart_tooling.py`: the statechart well-formedness rules,
  coverage hits, the completeness generator against a hand-written table,
  and the structured error report.
- `tests/test_statechart_stop.py`: a `StatechartError` stops every tier
  before any durable write, and the engine exits 7.
- `tests/statechart_completeness.py`: the generated completeness test that a
  converted machine's test module calls (§13).
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
- `tests/test_runner_journal.py`: the store's record shapes, the wire
  projection, the open table, the snapshot, catalog-hash sensitivity,
  replay fidelity, and the `journal` CLI.
- `tests/test_run_history.py`: the `runs` CLI: per-root and multi-root
  folding, the filters, a history spanning a boundary, and the
  `records_only` fidelity degrade.
- `tests/test_ledger.py`: one engine per run root, the eligibility gate, a
  SIGKILLed holder, the fence, and the start a crash left undispatched.
- `tests/test_runner_leadership.py`: one engine per run root under real
  processes: the flock refused across processes and released by the
  kernel, the inode fence, and the dispatch window.
- `tests/test_admission.py`: the frozen admission order, the frontier
  invariants, the decision index, and two-pass replay.
- `tests/test_preconditions.py`: mandatory preconditions and the versioned
  wire: the refusals, the check, refused versus rejected in the log, retry
  ordering, and the operator's exit codes and `request_id`.
- `tests/test_decision_record.py`: the one-line `decision` record and the
  `dispatch` record after it, a crash at every append, the version
  handshake, and what the subscribe stream promises.
- `tests/test_recovery.py`: recovering a mutation whose answer was lost:
  every failure after the write is `delivered`, a torn line at EOF is never
  parsed, and re-running with the printed replay flags is an exact retry.
- `tests/test_dispatch.py`: the dispatch plan: the ghost-run gate,
  supersession by exact desired state, one kill per run, and at-most-once
  launch.
- `tests/test_resume_dispatch.py`: resume's decision for a live start that
  left no run directory: a dispatched one fails or relaunches its watch, a
  decided one is dispatched, and anything else is left.
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

Shared cores:

- `tests/test_canon.py`: the canonical form, with a golden vector of exact
  bytes and digest.
- `tests/test_estate_manifest.py`: the manifest's refusals and the
  `--manifest` option on each verb that reads the estate, against explicit
  arguments.

Branch gate (DL-269): one test per branch of its module that the other
suites do not reach, each asserting the branch's observable effect:

- `tests/test_oracle_branches.py`, `tests/test_capacity_branches.py`,
  `tests/test_scheduler_branches.py` and `tests/test_control_branches.py`.

Training estate:

- `tests/test_nightbank_example.py`: the estate loads, lints clean, and
  reaches the start-of-day flip on one virtual night, plus the seeded
  interleaving sweep.

## What is not built

- The remote relay and shared store that multihost execution needs. They
  are designed in
  docs/concurrency-model.md (retired in 2.0.0)
  §7.
- Follower mode, `standby check` and the multi-executor rig (DL-230). They
  wait for the relay above.
- Fingerprints on tombstones (DL-111). They are specified in
  docs/concurrency-model.md (retired in 2.0.0) §5.
- The HTTP and WebSocket gateway that [docs/gateway.md](docs/gateway.md)
  proposes.
- The decompiler's custom-pattern option (`--patterns` recognizer and
  expander pairs).

Open questions run on documented defaults marked `# PENDING: <label>` in
the code. The AutoSys questions Q3c, Q3d, Q6, Q8b-Q8d, Q10, Q13, Q14, Q15 and Q16 need
a live AutoSys instance; Q6, Q13 and Q14 have no code switch. The resource-manager questions
Qr2-Qr5 are stated in DL-50; DL-247 narrows Qr2. Qr6 is decided: dsl41
keeps its default, and the `queued-recheck` switch selects the vendor's
readings (DL-257). The UC questions U1, U3b and U6b are retired with the
UC backend in 2.0.0. The runner questions E5-E10 are in
[docs/runner-design.md](docs/runner-design.md)
§15. E8's probe is in
[docs/live-instance-runbook.md](docs/live-instance-runbook.md); the others
have none.

## Release

Releases are tag-driven. A push of a tag that matches `v*` starts
[.github/workflows/release.yml](.github/workflows/release.yml).
The workflow runs the whole CI workflow as its first job, checks that the
tag is annotated and names the version in `pyproject.toml`, builds the
sdist and the wheel, exports the locked dependency closure with hashes,
and publishes only those files. Publication uses trusted
publishing (OIDC) in the `pypi` environment, which waits for the owner's
approval. The repository holds no PyPI token. The GitHub release that
follows carries the sdist, the wheel, the two requirements files, their input manifest
and `SHA256SUMS`.
The workflow's header records the one-time trusted-publisher setup on pypi.org.
The package readme is README.md with every relative link rewritten at
build time to the file on `main`, because the PyPI project page cannot
resolve a relative path.

A minor bump (1.3.0 to 1.4.0) carries one or more functional units. A patch
bump (1.3.0 to 1.3.1) carries documentation or a correction with no
behavior change. A module that was never documented as an API may leave the
package inside a minor bump; the tag message names it.

The annotated tag's message is the release note. Its first line is the
summary. Its body answers the questions that
the runbook's upgrade section (`deployment-runbook.md`, retired in 2.0.0)
reads to pick an upgrade row, in the order of the template under "Make a
release". It also names any Python module that left the package.

### Make a release

Start from a clean working tree with `main` pushed. Run the
[full local gates](docs/agent-workflow.md#verify-a-change).
The list follows CI, including format checking and the scoped 100% branch
coverage requirement.

Set the new version in `pyproject.toml` and run `uv lock`, which writes the
same version into `uv.lock`. Update the wheel filename and RPM version
examples in docs/offline-installation.md (retired in 2.0.0)
to the same version. Build locally and compare the wheel's file list with
the previous release's (`uv build`, then `unzip -Z1` on both): the
difference must be what the tag message is about to say. Commit the three
files and push them:

```sh
git commit pyproject.toml uv.lock docs/offline-installation.md \
  -m "chore: X.Y.Z (one-line summary)"
git push origin main
```

Rehearse the release on that commit before you tag it. A manual run of the
workflow from a branch runs `gates` and `build`. It publishes
nothing, whatever its `publish` input says. Publication needs a tag: a tag
push, or a manual run from a tag with `publish` set.

```sh
gh workflow run release.yml --ref main
gh run list --workflow release.yml --limit 1
```

The tag checks in `build` run whenever the ref is a tag, on a push or a
manual run.

Build locally first. `dist/` must hold only this build's two files:

```sh
uv build --sdist && uv build --wheel
bash scripts/export-release-inputs.sh dist/*.whl exports
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
