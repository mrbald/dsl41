"""dsl41: AutoSys->Stonebranch migration compiler.

Module map (implementation order per CLAUDE.md; all ten compiler phases
built, and all six phase-11 runner tiers 11a-11f with them -- the runner is
six sibling modules since DL-74, docs/runner-design.md ss14):
  ast_jil    - JIL statement scanner + AST + preserve/canonical renderers (docs/jil-statement-syntax.md)
  conditions - condition-expression parsing via grammars/condition.lark -> Cond models
  ir         - IR-F Pydantic models + AST->IR-F lowering (docs/ir-design.md ss3-4)
  autocal    - extended-calendar rule interpreter: CalendarIR/CycleIR carry ->
               day sets per the SEM-36..39 doc-freeze (DL-57, DL-60)
  lint       - Violation model + rules L001..L019 (docs/ir-design.md ss9)
  derive     - IR-F -> IR-G analysis passes (docs/ir-design.md ss5)
  viz        - IR-G -> Markdown report of per-workflow Mermaid charts (DL-35)
  viz_html   - the same report content as one offline HTML page, and the
               whole-graph chart alone as another (DL-70, DL-76)
  viz_explore - IR-G -> cytoscape.js elements for the interactive navigation
               page (DL-71)
  oracle_state - the oracle's state and the vocabulary that moves it:
               JobStatus/EventKind/Event/TraceEntry, the frozen JobRuntime
               and GlobalRuntime rows, RuntimeState (the owner, its timer
               heap and the input transaction) and OracleError. Depends on
               nothing in the interpreter, which is the point (DL-91)
  oracle     - discrete-event AutoSys semantics interpreter (docs/ir-design.md ss7)
  equiv      - canonical form + equivalence tiers a/b/c (docs/ir-design.md ss6)
  backend_uc - UC twin model + edge classification + migration report +
               U3a base CREATE-ONLY record bundle (docs/uc-edge-schema.md;
               rich condition forms PENDING: U3b)
  uc_oracle  - minimal UC workflow interpreter + trace comparator
               (the P-Mxx expected-divergence pairs, stonebranch Part IV)
  dsl        - builder surface (job/box/sequence/parallel) + decompiler
  placeholders - non-core `~{$NAME}~` estate templating preprocessor behind
               the `resolve` verb; nothing in the core imports it (DL-19)
  cli        - typer entry points (lint/equiv/report/uc/viz/decompile/folds/
               resolve/journal/run/rehearse/sendevent/query/supervise/ui/serve)
  __init__   - this map, and nothing else: the package exports no names
  __main__   - `python -m dsl41`; `serve` spawns each session through it
  runner     - phase-11 engine loop over the oracle: the single-writer loop,
               the run lifecycle (start/resume + reconciliation) and the ss10
               control server (docs/runner-design.md)
  runner_clock - the ss9 time domains (Clock protocol, VirtualClock,
               RealClock) + EngineError, at the bottom of the import DAG
  runner_adapters - the ss6 adapter contract and every adapter, plus the
               ss6a Tier-1 detached path and the ss7 spool ladder
  runner_admission - the frozen admission order (concurrency-model ss4):
               Attempt/ApplyResult, the typed Frontiers, the DecisionIndex,
               the fingerprint, and the gate as a pure function; plus the
               ss6 envelope and the ss0 mandate that a mutation names the
               revision it was composed against (parse_envelope)
  period     - period identity (docs/period-model.md ss1.1/ss2.1):
               catalog_hash v1/v2, source_bundle_hash and the bundle it
               addresses, RuntimeProfile + runtime_hash, the staged and
               committed manifests, and the `segment` record
  runner_journal - the ss7 inputs-only WAL: Journal, read_journal, the
               two-pass replay_inputs, and the catalog_hash resume gate
  runner_scheduler - the ss5 calendar scheduler + the SEM-35 timezone ladder
               that turns its ticks into UTC instants
  runner_preflight - the ss8 ERROR/WARN item model and its rules
  runner_tui - the ss11 Textual TUI, a client of the control socket only
               (optional `dsl41[ui]` extra)
  runner_wrapper - per-run Tier-0 lifecycle recorder; STDLIB-ONLY, spawned
               by file path (docs/supervisor-protocol.md, DL-42 boundary)
  runner_supervisor - the ss6a Tier-1 daemon, one per run_root; STDLIB-ONLY
               on the same boundary (docs/supervisor-protocol.md ss5)
  runner_procid - durable records + process identity shared by the Tier-0
               wrapper and the Tier-1 supervisor; STDLIB-ONLY (DL-72)
"""
