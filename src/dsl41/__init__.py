"""dsl41: AutoSys->Stonebranch migration compiler.

Module map (implementation order per CLAUDE.md; all ten compiler phases
built, the phase-11 runner in progress per docs/runner-design.md ss14):
  ast_jil    - JIL statement scanner + AST + preserve/canonical renderers (docs/jil-statement-syntax.md)
  conditions - condition-expression parsing via grammars/condition.lark -> Cond models
  ir         - IR-F Pydantic models + AST->IR-F lowering (docs/ir-design.md ss3-4)
  lint       - Violation model + rules L001..L015 (docs/ir-design.md ss9)
  derive     - IR-F -> IR-G analysis passes (docs/ir-design.md ss5)
  viz        - IR-G -> Mermaid
  oracle     - discrete-event AutoSys semantics interpreter (docs/ir-design.md ss7)
  equiv      - canonical form + equivalence tiers a/b/c (docs/ir-design.md ss6)
  backend_uc - UC twin model + edge classification + migration report
               (record emission PENDING: U3 -- see BlockedOnU3)
  uc_oracle  - minimal UC workflow interpreter + trace comparator
               (the P-Mxx expected-divergence pairs, stonebranch Part IV)
  dsl        - builder surface (job/box/sequence/parallel) + decompiler
  cli        - typer entry points (lint/equiv/report/viz/decompile/journal)
  runner     - phase-11 engine loop over the oracle: clocks (Virtual/Real),
               adapters (Fake/LocalCommand/FileWatcher), WAL journal +
               resume/reconciliation (docs/runner-design.md; 11c-11f pending)
  runner_wrapper - per-run Tier-0 lifecycle recorder; STDLIB-ONLY, spawned
               by file path (docs/supervisor-protocol.md, DL-42 boundary)
"""
