# dsl41 (codename)

[![tests](https://github.com/mrbald/dsl41/actions/workflows/ci.yml/badge.svg)](https://github.com/mrbald/dsl41/actions/workflows/ci.yml)
[![vulnerabilities](https://github.com/mrbald/dsl41/actions/workflows/audit.yml/badge.svg)](https://github.com/mrbald/dsl41/actions/workflows/audit.yml)
[![secrets](https://github.com/mrbald/dsl41/actions/workflows/secrets.yml/badge.svg)](https://github.com/mrbald/dsl41/actions/workflows/secrets.yml)
[![PyPI](https://img.shields.io/pypi/v/dsl41)](https://pypi.org/project/dsl41/)
[![Python](https://img.shields.io/pypi/pyversions/dsl41)](https://pypi.org/project/dsl41/)
[![license](https://img.shields.io/badge/license-AGPL--3.0%20%7C%20commercial-blue)](LICENSING.md)

Migration compiler for scheduler estates: AutoSys (JIL) frontend, semantic IR,
linter, Mermaid visualizer, formal equivalence validator, Stonebranch Universal
Controller backend, and a Python DSL extracted from patterns observed in the
synthetic test corpus.

Start here, in order:
1. [docs/autosys-semantics.md](https://github.com/mrbald/dsl41/blob/main/docs/autosys-semantics.md) - what JIL actually means (SEM entries)
2. [docs/stonebranch-semantics.md](https://github.com/mrbald/dsl41/blob/main/docs/stonebranch-semantics.md) - target model + AutoSys->UC mapping (UCS/M entries)
3. [docs/ir-design.md](https://github.com/mrbald/dsl41/blob/main/docs/ir-design.md) - AST / IR-F / IR-G / oracle / equivalence design
4. [docs/jil-statement-syntax.md](https://github.com/mrbald/dsl41/blob/main/docs/jil-statement-syntax.md) - statement scanner spec
5. [docs/decision-log.md](https://github.com/mrbald/dsl41/blob/main/docs/decision-log.md) - why things are the way they are
6. [CLAUDE.md](https://github.com/mrbald/dsl41/blob/main/CLAUDE.md) - working agreement + implementation order

Status: all ten compiler phases built and tested; the phase-11 runner
([docs/runner-design.md](https://github.com/mrbald/dsl41/blob/main/docs/runner-design.md))
is in progress — 11a (engine core + bisimulation gate), 11b (process
lifecycle tier: wrapper shim, real adapters, WAL journal, crash-recovery
resume; spool contract frozen in
[docs/supervisor-protocol.md](https://github.com/mrbald/dsl41/blob/main/docs/supervisor-protocol.md)),
11c (calendar scheduler, preflight, control socket, headless CLI), 11d
(Textual TUI), and 11e (`serve` via textual-serve) are done. See the memo
below for the source map and what remains open.

## CLI

One entry point (pyproject `[project.scripts]`): `dsl41 = dsl41.cli:app`. Run
`uv run dsl41 --help`, or install the package and call `dsl41` directly. Every
command takes one or more JIL files that together form one catalog, and all
share the exit-code contract: 0 success/clean; 1 findings (`lint`, `equiv`
only); 2 the input never reached the tool (unreadable file, JIL parse error,
or DL-07 lowering refusal). `--permit-unknown` is the DL-07 escape hatch on
every command: carry unknown attributes verbatim instead of refusing.

### Lint a catalog

```sh
dsl41 lint jobs.jil globals.jil            # errors fail (exit 1)
dsl41 lint --strict jobs.jil globals.jil   # warnings fail too
```

Runs L001-L018 (IR-F rules, truth-table rules, graph rules over the derived
graph, dangling-name checks). `--strict` is the migration gate: refuse to ship a catalog that lints
dirty.

### Visualize the dependency graph

```sh
dsl41 viz jobs.jil -o graph.md             # Markdown report of Mermaid charts
dsl41 viz --direction TD --collapse-threshold 20 jobs.jil
dsl41 viz --elk jobs.jil                   # ELK layout (VS Code; GitHub ignores it)
```

The report renders each independent workflow as its own chart (largest first),
with a legend and appendices for everything the charts thin out: standalone
admin-wrapper jobs (charted again with `--include-singletons`), assumed-edge
assumptions, redesign flags, OR shapes, and cycles. Within a chart: boxes are
subgraphs, edges carry their E/A/R migration class (solid/dashed/thick-red),
file watchers and schedules are marked as triggers, mutual exclusions render
as lock links or a shared lock hub, and boxes with more direct members than
the collapse threshold (default 12) fold into a single node. Any Mermaid
renderer works (GitHub, mermaid.live, IDE preview).

### Migration report

```sh
dsl41 report jobs.jil -o report.md
```

Per-catalog markdown from the UC backend: refused (R) constructs, recorded
per-edge assumptions (A rows), and the open U-question table. Always exits 0
once generated -- the report IS the loud channel; use `lint --strict` as the
pass/fail gate.

### Prove two catalogs equivalent

```sh
dsl41 equiv new.jil --against old.jil                       # all tiers
dsl41 equiv new.jil -b old.jil --tier c --scripts 50        # more oracle runs
dsl41 equiv new.jil -b old.jil --rename OLD=NEW --case-fold # renamed estate
```

Tier a is structural (canonical-form diff), tier b enumerates per-job truth
tables (defers, never fails, on too-large state spaces), tier c compares
oracle traces over seeded deterministic event scripts. Identical canonical
hashes short-circuit to equivalent. Exit 1 on any divergence. Typical use:
refactor a catalog (by hand or via decompile-edit-rebuild), then prove
nothing changed.

### JIL -> DSL (decompile)

```sh
dsl41 decompile jobs.jil -o catalog.py
```

Emits a runnable Python module over the phase-10 builders. Executing the
module rebuilds a catalog whose canonical form equals the original's (the
round-trip property, tested corpus-wide).

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

`job()` keyword names are JIL attribute names; `sequence()` wires s()-chains
and `parallel()` fans out/in, both refusing to merge into an existing
condition (DL-17: no silent loss). There is no second lowering path -- the
builder generates JIL and reuses parse -> lower, so `lint`, `viz`, and
`equiv` all apply unchanged to DSL-built catalogs. The round-trip workflow:
`decompile` an estate to Python, edit it, run the module, `equiv` the result
against the original.

### Run an estate (phase 11)

```sh
dsl41 run jobs.jil --run-root ./run1            # headless engine + control socket
dsl41 sendevent STARTJOB -J job_a -S ./run1/control.sock
dsl41 query status -S ./run1/control.sock       # JSON: statuses, timers, log paths
dsl41 ui -S ./run1/control.sock                 # attach the TUI; q detaches
dsl41 run jobs.jil --run-root ./run1 --ui       # ...or one terminal owning both
dsl41 rehearse jobs.jil --hours 24              # virtual clock: a day in seconds
dsl41 serve -S ./run1/control.sock              # the same TUI over the web
```

The TUI (jobs table with pending timers and alarms, explain pane with
per-atom condition truth, log tail, sendevent console) is the optional
`[ui]` extra: `pip install 'dsl41[ui]'`. It is a thin client of the run's
control socket — the same protocol `sendevent`/`query` speak.

### Serving the TUI over the web (phase 11e)

`dsl41 serve -S ./run1/control.sock` wraps
[textual-serve](https://github.com/Textualize/textual-serve) around the
same app: every browser tab gets its own `dsl41 ui --socket` subprocess
attached to the run (the ss11 one-instance-per-viewer split), rendered as a
terminal in the page. textual-serve ships **no authentication**, so the
default bind is loopback (`127.0.0.1:8000`) — reach it from elsewhere via a
reverse proxy or an SSH tunnel, never by widening `--host`:

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

Put auth (basic auth, an OIDC gate, client certs — whatever the estate
already trusts) in that proxy layer; dsl41 has none of its own here. The
control socket is 0600 from birth (ss10), so `serve` only ever sees what
its own user could already reach directly — it does not widen access, it
makes existing access reachable from a browser.

## Implementation memo

All ten phases from CLAUDE.md's implementation order are implemented and tested, in
build order: ast_jil, conditions, ir, lint, derive, viz, oracle, equiv, backend_uc, dsl.
Phase 11 (the runner,
[docs/runner-design.md](https://github.com/mrbald/dsl41/blob/main/docs/runner-design.md))
is underway: 11a — the sans-IO engine loop, VirtualClock, FakeAdapter, and the two
oracle additions, gated by the ss13 bisimulation suite — and 11b — the process
lifecycle tier (per-run wrapper shim, LocalCommand/FileWatcher adapters, WAL journal,
crash-recovery resume with the reconciliation ladder; spool contract frozen in
[docs/supervisor-protocol.md](https://github.com/mrbald/dsl41/blob/main/docs/supervisor-protocol.md))
— and 11c — the ss5 calendar scheduler, ss8 preflight, ss10 control socket
(sendevent parity + queries + subscribe), and the headless
`run`/`rehearse`/`sendevent`/`query` CLI verbs — and 11d — the ss11 Textual TUI
(`dsl41 ui` against a running engine, or `dsl41 run --ui`; optional
`dsl41[ui]` extra) — and 11e — `dsl41 serve` via
[textual-serve](https://github.com/Textualize/textual-serve), same extra
— are built; 11f follows the
design's phasing. The suite spans 21 test files
(`pytest --collect-only -q` for the current count) plus the 15-file
synthetic/doc-derived JIL corpus under `tests/corpus/`.

### Source map

- src/dsl41/__init__.py — module map docstring only (no exports); records the ten-phase
  build order
- src/dsl41/ast_jil.py — JIL statement-level scanner + AST + preserve/canonical
  renderers; fidelity contract F1-F4: byte-exact `render(parse(x)) == x` (F1, fuzzed
  by F3), canonical-mode fixpoint (F2), escaped-colon torture (F4)
- grammars/condition.lark — condition-expression grammar (lark, LALR); carries the Q1
  precedence switch as two start rules, `start_flat` (default, flat & / |) and
  `start_prec` (C-style, & binds tighter)
- src/dsl41/conditions.py — lark loader + Tree->Cond transformer for
  condition/box_success/box_failure expressions; lookback + span retention
- src/dsl41/ir.py — IR-F Pydantic entity models + AST->IR-F lowering; DL-07 firewall
  refuses unknown attributes unless `permit_unknown` is set
- src/dsl41/lint.py — Violation model + rules L001-L018 (pure IR-F rules L001-L005/L015,
  truth-table rules L006/L007 joined in phase 8, graph rules L008-L014 over the derived
  graph, dangling-name rules L016-L018)
- src/dsl41/derive.py — IR-F -> IR-G: seven analysis passes producing edges, mutex
  pairs, box tree, same-cycle detection, M01-M36 mapping-row classification
- src/dsl41/viz.py — IR-G -> Markdown report of per-workflow Mermaid charts (DL-35):
  component split, trigger/lock visual grammar, E/A/R edge-class arrows, collapse
  threshold, appendices for everything the charts drop
- src/dsl41/oracle.py — AutoSys discrete-event semantics interpreter; script-driven
  completion, edge-triggered re-evaluation, per-SEM-entry trace tests
- src/dsl41/equiv.py — equivalence validator: canonical form + tier a (structural),
  tier b (per-job state-space enumeration), tier c (oracle trace comparison)
- src/dsl41/backend_uc.py and src/dsl41/uc_oracle.py — UC backend pair: backend_uc
  builds the UC twin model, classifies edges, and emits the migration report (record
  emission blocked on U3); uc_oracle is the UC-side twin interpreter that runs the
  P-Mxx expected-divergence pairs against it, sharing Event/TraceEntry with oracle.py
- src/dsl41/dsl.py — builder surface (job/box/sequence/parallel) + decompiler,
  extracted from corpus-observed patterns only (phase 10, last by design)
- src/dsl41/runner.py — phase-11 engine: single-writer loop over the oracle
  (dispatch table, time-ordered event queue, stale-completion gate), VirtualClock +
  RealClock, FakeAdapter + LocalCommandAdapter + FileWatcherAdapter, inputs-only WAL
  journal, resume/reconciliation ladder, calendar scheduler (ss5), preflight (ss8),
  control-socket server (ss10: sendevent parity, status/trace/explain/plan,
  subscribe); supervisor tier (11f) per docs/runner-design.md ss14
- src/dsl41/runner_tui.py — the ss11 Textual TUI (optional `dsl41[ui]` extra): a thin
  client of the control socket only (jobs table with pending timers/alarms, explain
  pane with per-atom truth, log tail of the ss6 std files, sendevent console);
  subscribe is a wake-up signal, every rendered view comes from the idempotent
  ss10 queries
- src/dsl41/runner_wrapper.py — the ss6a Tier-0 per-run lifecycle recorder: stdlib-only
  (enforced DL-42 extraction boundary), records spawn.json/status.json durably, kills
  and records on lifeline EOF; spool contract in docs/supervisor-protocol.md
- src/dsl41/cli.py — typer entry points: `lint`, `equiv`, `report`, `viz`, `decompile`,
  `journal` (render-by-replay of a run WAL), `run` (headless executor: wall clock,
  real processes, control socket; stop with SIGINT/SIGTERM), `rehearse` (virtual
  clock + scripted adapters: a 24h estate in seconds, same engine path), `sendevent`
  and `query` (clients of a running engine's control socket), `ui` (the ss11
  Textual TUI attached to a running engine; `run --ui` starts both in one terminal),
  and `serve` (11e: wraps textual-serve around the same app, one `dsl41 ui`
  subprocess per browser session; optional `dsl41[ui]` extra, loopback by default)
  (exit 2 = catalog load/usage failure everywhere, incl. preflight refusals; exit 1 =
  findings for `lint`/`equiv`, a mid-run engine failure for `run`/`rehearse` —
  `report` always exits 0 once generated: the report itself is the loud channel)
- src/dsl41/__main__.py — `python -m dsl41`, needed because `serve` spawns each
  session's app as `<sys.executable> -m dsl41 ui --socket <path>`

### Tests

- tests/test_ast_fidelity.py — F1-F4 round-trip fidelity, scanner structure and error
  paths, whitespace-sensitive edge cases
- tests/test_condition_grammar.py — grammar-level accept/reject cases, doc-derived only
- tests/test_conditions.py — Cond model shapes, lookback semantics, span retention, Q1
  precedence switch
- tests/test_ir.py — IR-F lowering decisions: SEM-30/31/32/33/34, subcommand support
  v1, type-inapplicable attributes
- tests/test_lint.py — L001-L005/L015 rules plus the lint CLI exit-code contract
- tests/test_derive.py — the seven IR-G passes plus the graph-rule lint additions
  L008-L014
- tests/test_viz.py — Mermaid render structure (balanced blocks, id-safety, one golden
  render), the DL-35 markdown report (components, appendices, mutex encodings) plus
  the viz CLI
- tests/test_oracle.py — AutoSys oracle trace tests against the SEM entries, citing
  dossier §8's sparse T-ID index (T01–T34 range, not contiguous; T03/precedence is
  pinned at parse time in test_condition_grammar.py, not here); every test runs
  twice — Oracle-direct and Engine(VirtualClock, inert FakeAdapter) via
  tests/bisim_harness.py — the runner-design ss13 bisimulation gate
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
- tests/test_runner_adapters.py — RealClock, LocalCommandAdapter end-to-end (SEM-09
  boundary, append/stdin/profile semantics, KILLJOB kill path), FileWatcherAdapter
  steady-size polling under VirtualClock, and the AdapterResult mapping
- tests/test_runner_scheduler.py — phase-11c scheduler occurrence math (days/times/
  start_mins, timezone + DST corners, E10 defaults), engine integration under the
  virtual clock, resume re-anchoring + the E9 missed-tick drops, and the ss8
  preflight rule fixture pairs
- tests/test_runner_control.py — phase-11c control socket (sendevent parity verbs,
  status/trace/explain/plan queries, subscribe backfill/live seam, socket hygiene),
  the DL-45 commit-discipline regression, the run/rehearse/sendevent/query CLI, and
  the DL-46 status-response fields (pending_timers, log paths)
- tests/test_runner_tui.py — phase-11d TUI (skips without the [ui] extra): the
  sendevent console parser, ControlClient against a real ControlServer (round trip,
  reconnect, subscribe), and the ss13.6 pilot smokes (table, explain atoms, pending
  timers, log tail, key-driven STARTJOB)
- tests/test_runner_serve.py — phase-11e `serve` CLI: missing-socket and
  missing-extra exit-2 paths, the constructed textual-serve command (quoting a
  socket path with a space), default loopback bind, bind-failure exit 2 — the
  real textual-serve Server is always monkeypatched (ss13.6 posture, thinner
  still: a CLI wrapper, not a pilot)
- tests/test_equiv.py — canonical form, tiers a/b/c, the L006/L007 lint rules (tested
  here because they share equiv's truth-table machinery), and the equiv CLI
- tests/test_backend_uc.py — edge classification, migration report, report CLI, the
  U3 block itself
- tests/test_uc_oracle.py — UCS-entry trace semantics (UCS-01/02/03/09/13) plus the
  P-Mxx expected-divergence pairs against the UC twin interpreter
- tests/test_dsl.py — the four corpus-extracted builders, cond_to_source fidelity, and
  the decompile round-trip property

### What's not done

UC record emission is blocked on U3 (pull /resources/openapi.json from a live
controller, freeze docs/uc-edge-schema.md, generate client) — until then backend_uc
emits only the migration report + edge classification. Open questions Q1-Q7 (autosys
dossier §9), U1-U8 (stonebranch Part III), and the runner's E5-E10
(runner-design ss15) are unresolved. Those with a behavior default in code
(Q1-Q4, Q7, U1-U5, U8, E5-E10) run on a documented default marked
`# PENDING: Qn/Un/En`; Q5, Q6, U6, and U7 have no code switch yet — they live in the
dossiers and in backend_uc's migration-report question table. The Q1 precedence sentinel
test stays until Q1 resolves. Q1-Q3 need a live AutoSys instance; U3 needs a live UC
controller. Runner phase 11f (the detached supervisor tier) follows per
runner-design ss14.

## License

dsl41 is dual-licensed:

- **Open source:** [GNU AGPL-3.0-only](LICENSE). Distributing modified versions,
  or offering them as a network service, requires offering the complete
  corresponding source under the same terms.
- **Commercial:** organizations that cannot accept AGPL obligations can obtain a
  commercial license — see [COMMERCIAL.md](COMMERCIAL.md).

Copyright (C) 2026 dsl41 authors. External contributions require a signed CLA
preserving the dual-licensing right; corpus hygiene rules also apply
(see [LICENSING.md](LICENSING.md)).

_Most of the code is written with the assistance of industrial coding agents —
primarily Anthropic's Claude — while the original ideas and design are my own._
