"""dsl41: AutoSys migration compiler and runner.

Module map (implementation order per CLAUDE.md; the runner is twenty sibling modules,
docs/runner-design.md ss14):
  ast_jil    - JIL statement scanner + AST + preserve/canonical renderers (docs/jil-statement-syntax.md)
  conditions - condition-expression parsing via grammars/condition.lark -> Cond models
  ir         - IR-F Pydantic models + AST->IR-F lowering (docs/ir-design.md ss3-4)
  autocal    - extended-calendar rule interpreter: CalendarIR/CycleIR carry ->
               day sets per the SEM-36..39 doc-freeze (DL-57, DL-60)
  lint       - Violation model + rules L001..L025 (docs/ir-design.md ss9)
  derive     - IR-F -> IR-G analysis passes (docs/ir-design.md ss5)
  viz        - IR-G -> Markdown report of per-workflow Mermaid charts (DL-35)
  viz_html   - the same report content as one offline HTML page, and the
               whole-graph chart alone as another (DL-70, DL-76)
  viz_explore - IR-G -> cytoscape.js elements for the interactive navigation
               page (DL-71)
  oracle_state - the oracle's state and the vocabulary that moves it: JobStatus, EventKind,
               Event, TraceEntry, the frozen JobRuntime and GlobalRuntime rows, RuntimeState
               and OracleError. It imports nothing from the interpreter (DL-91)
  oracle     - discrete-event AutoSys semantics interpreter (docs/ir-design.md ss7)
  equiv      - canonical form + equivalence tiers a/b/c (docs/ir-design.md ss6)
  dsl        - builder surface (job/box/sequence/parallel) + decompiler
  placeholders - non-core `~{$NAME}~` estate templating preprocessor behind
               the `resolve` verb; nothing in the core imports it (DL-19)
  cli        - typer entry points (lint/equiv/viz/decompile/build/folds/minify/
               resolve/journal/runs/run/rehearse/sendevent/query/ui/serve)
  __init__   - this map, and nothing else: the package exports no names
  __main__   - `python -m dsl41`; `serve` spawns each session through it
  runner     - phase-11 engine loop over the oracle: the single-writer loop,
               the run lifecycle (start/resume + reconciliation) and the ss10
               control server (docs/runner-design.md)
  runner_clock - the ss9 time domains (Clock protocol, VirtualClock,
               RealClock) + EngineError, at the bottom of the import DAG
  runner_adapters - the ss6 adapter contract and every adapter, plus the
               ss7 spool ladder
  runner_admission - the frozen admission order (concurrency-model ss4):
               Attempt/ApplyResult, the typed Frontiers, the DecisionIndex,
               the fingerprint, the gate and the dispatch plan as pure
               functions; plus the ss6 envelope and the ss0 mandate that a
               mutation names the revision it was composed against
               (parse_envelope)
  run_root   - a run root's identity and layout: catalog_hash v2,
               source_bundle_hash and the bundle it addresses, RuntimeProfile +
               runtime_hash, the staged and committed manifests, the `segment`
               record
  runner_journal - the ss7 inputs-only WAL: Journal, read_journal, the
               two-pass replay_inputs, and the catalog_hash resume gate
  runner_scheduler - the ss5 calendar scheduler, turning its ticks into UTC
               instants through the shared timezone module
  timezones - SEM-35 name resolution and the one naive-UTC <-> local
               conversion; phase-free, imports nothing from dsl41 (DL-163)
  runner_preflight - the ss8 ERROR/WARN item model and its rules
  runner_tui - the ss11 Textual TUI, a client of the control socket only
               (optional `dsl41[ui]` extra)
  runner_wrapper - per-run Tier-0 lifecycle recorder; STDLIB-ONLY, spawned
               by file path (DL-42 boundary)
  runner_procid - durable records + process identity used by the wrapper;
               STDLIB-ONLY (DL-72)
"""
