# dsl41 (codename)

Migration compiler for scheduler estates: AutoSys (JIL) frontend, semantic IR,
linter, Mermaid visualizer, formal equivalence validator, Stonebranch Universal
Controller backend, and a Python DSL extracted from patterns observed in the
synthetic test corpus.

Start here, in order:
1. docs/autosys-semantics.md   - what JIL actually means (SEM entries)
2. docs/stonebranch-semantics.md - target model + AutoSys->UC mapping (UCS/M entries)
3. docs/ir-design.md           - AST / IR-F / IR-G / oracle / equivalence design
4. docs/jil-statement-syntax.md - statement scanner spec
5. docs/decision-log.md        - why things are the way they are
6. CLAUDE.md                   - working agreement + implementation order

Status: all ten implementation phases built and tested; see the memo below for
the source map and what remains open.

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

Runs L001-L015 (IR-F rules, truth-table rules, graph rules over the derived
graph). `--strict` is the migration gate: refuse to ship a catalog that lints
dirty.

### Visualize the dependency graph

```sh
dsl41 viz jobs.jil > graph.mmd             # Mermaid on stdout
dsl41 viz --direction TD --collapse-threshold 20 jobs.jil
```

Boxes render as subgraphs, edges carry their E/A/R migration class, and boxes
with more direct members than the collapse threshold (default 12) fold into a
single node. Paste the output into any Mermaid renderer (GitHub, mermaid.live,
IDE preview).

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

## Implementation memo

All ten phases from CLAUDE.md's implementation order are implemented and tested, in
build order: ast_jil, conditions, ir, lint, derive, viz, oracle, equiv, backend_uc, dsl.
The suite spans 12 test files (`pytest --collect-only -q` for the current count) plus
the 15-file synthetic/doc-derived JIL corpus under `tests/corpus/`.

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
- src/dsl41/lint.py — Violation model + rules L001-L015 (pure IR-F rules L001-L005/L015,
  truth-table rules L006/L007 joined in phase 8, graph rules L008-L014 over the derived
  graph)
- src/dsl41/derive.py — IR-F -> IR-G: seven analysis passes producing edges, mutex
  pairs, box tree, same-cycle detection, M01-M36 mapping-row classification
- src/dsl41/viz.py — IR-G -> Mermaid: boxes as subgraphs, E/A/R edge-class arrows,
  pseudo-node shapes, collapse threshold
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
- src/dsl41/cli.py — typer entry points: `lint`, `equiv`, `report`, `viz`, `decompile`
  (exit 2 = catalog load/usage failure everywhere; exit 1 = findings, `lint`/`equiv`
  only — `report` always exits 0 once generated: the report itself is the loud channel)

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
  render) plus the viz CLI
- tests/test_oracle.py — AutoSys oracle trace tests against the SEM entries, citing
  dossier §8's sparse T-ID index (T01–T34 range, not contiguous; T03/precedence is
  pinned at parse time in test_condition_grammar.py, not here)
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
emits only the migration report + edge classification. Open questions Q1-Q6 (autosys
dossier §9) and U1-U8 (stonebranch Part III) are unresolved. Those with a behavior
default in code (Q1-Q4, U1-U5, U8) run on a documented default behind a switch marked
`# PENDING: Qn/Un`; Q5, Q6, U6, and U7 have no code switch yet — they live in the
dossiers and in backend_uc's migration-report question table. The Q1 precedence sentinel
test stays until Q1 resolves. Q1-Q3 need a live AutoSys instance; U3 needs a live UC
controller.

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
