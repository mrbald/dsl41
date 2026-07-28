# CLAUDE.md — working agreement for dsl41

You are implementing a migration compiler designed in a prior session. The design
is finished and normative; it lives in `docs/`. Do not re-derive it — read it.

## Read first, in this order
1. `docs/ir-design.md` — the central spec (pipeline, AST/IR-F/IR-G models, oracle,
   equivalence tiers, linter rules L001–L015). Model sketches there are the API.
2. `docs/jil-statement-syntax.md` — statement scanner spec + fidelity tests F1–F4.
3. `docs/autosys-semantics.md` — SEM entries; every one implies a trace test (§8).
4. `docs/stonebranch-semantics.md` — UCS entries + M01–M36 mapping table; the UC
   backend refuses R-rows and reports A-row assumptions (Part II requirements 1–3).
5. `docs/decision-log.md` — do not relitigate DL entries; append new ones.

## Non-negotiable disciplines
- **No silent loss.** AST→IR-F lowering errors on unknown non-allow-listed
  attributes (DL-07). UC compile refuses R-classified edges and emits migration-
  report items instead. Every A-classified edge records its assumption.
- **Fidelity is tested, not asserted.** `render(parse(x)) == x` on the whole corpus
  before anything else is built. Canonical mode is a fixpoint (F2).
- **IR-G is derived, never authoritative.** Pure function of IR-F; regenerate, do
  not edit or persist as truth.
- **Pure compiler.** No runtime dependency in any emitted artifact.
- **Corpus hygiene.** `tests/corpus/` is synthetic/doc-derived ONLY. Never accept
  production JIL from any employer estate into the repo, tests, or docs (LICENSING.md).
- **Open questions stay open.** Q1–Q7 (autosys dossier §9), U1–U8 (stonebranch
  Part III). DL-53 (2026-07-28) closed nine — Q1, Q4, Q5, U2, U4, U5, U6a, U7,
  U8 — each pinned to a dossier citation; DL-54 (same day) resolved Q2a
  (zero-lookback anchors to the dependent's own last end; anchor switch
  deleted) and flipped Q3's default to arm-and-wait (switch kept); DL-55
  (same day) split U3 — U3a (base CREATE-ONLY record schema) doc-frozen in
  docs/uc-edge-schema.md, U3b (rich forms + live write-path) open. Do not
  relitigate any of them. Still open, with a live `# PENDING: Qn/Un` code
  marker: Q2b (first-run corner), Q3, Q7, U1, U3b (Q1's switch and Q4's marker
  were deleted per DL-53; Q2's per DL-54). Q6 has no code switch, dossier-only
  (Q6-adjacent aside in oracle.py); U6b lives in backend_uc's `_U_QUESTIONS`
  table, pruned to exactly U1+U6b (U3b deliberately NOT there — it gates
  emission, not a mapping row; it surfaces via quarantine + the report
  footer). A separate `# PENDING: Qr*`
  series in ir.py/oracle.py/runner.py is unrelated — resource-manager
  questions (DL-49/50), no dossier, outside this ledger. Do not guess-resolve
  any open question; implement the documented default and keep the switch
  where one exists.

## Implementation order (DL-03) — one phase per PR-sized unit
All ten phases are built and tested (README's implementation memo has the source
map); this list stays as the normative order and scope of each unit.
1. `ast_jil`: scanner per spec + preserve/canonical renderers + F1–F4 tests.
   Definition of done: all corpus files round-trip byte-identical; fuzz test green.
2. `conditions`: lark loader (historically both start rules + the
   `CONDITION_PRECEDENCE` switch; retired by DL-53 — single flat rule now),
   Tree→Cond transformer, lookback token validation (L015 shapes), span retention.
3. `ir`: Pydantic models exactly as ir-design §3–4 + lowering + model validators
   (XOR rules SEM-31, lookback-on-global ban SEM-04).
4. `lint`: Violation model (stable codes, `exit_code(strict)`) + L001–L005, L015
   first (pure IR-F rules); graph rules follow phase 5.
5. `derive`: passes 1–7 from ir-design §5, including mutex/OR/same-cycle detectors.
6. `viz`: Markdown report of per-component Mermaid charts from IR-G (boxes →
   subgraph, predicate-labeled edges, collapse threshold; visual grammar,
   component split, and appendices per DL-35).
7. `oracle`: event loop + status store + box fold; port dossier §8 trace tests.
8. `equiv`: canonical form, tier a (structural), tier b (truth table w/ atom
   ceiling), tier c (oracle traces, hypothesis event scripts).
9. `backend_uc`: migration-report emitter, edge classification, UC twin, and —
   since DL-55 — the U3a base CREATE-ONLY record bundle (`dsl41 uc`; schema
   frozen in `docs/uc-edge-schema.md`, whole-workflow quarantine for anything
   the base cannot express). Rich-condition emission, the OpenAPI pull, and the
   generated client stay BLOCKED on U3b (live controller).
10. DSL (`decompiler` + surface): LAST, extracted from patterns the corpus shows
    (DL-03). Do not design combinators speculatively.

## Testing conventions
- pytest + hypothesis; trace tests named `test_semXX_*` / pairs `test_pMxx_*`.
- Every linter rule ships with a corpus fixture that triggers it and one that
  doesn't.
- Q1 is resolved (DL-53): `test_sem03_flat_left_to_right_precedence_pinned`
  (grammar, tests/test_condition_grammar.py) and
  `test_sem03_precedence_pinned_model_level` (Cond model,
  tests/test_conditions.py) pin flat left-to-right precedence. The old
  sentinel-test-and-switch protocol (DL-06) is retired.

## Style
- Python ≥3.12, Pydantic V2, typer CLI, ruff line length 100, mypy clean.
- snake_case throughout; small pure functions for analysis passes; no clever
  metaprogramming in the IR.

## When live-instance access is available (ask the user, don't assume)
- Resolve Q2b (zero-lookback first-run corner), Q3 (standalone time+condition
  composition + the disarm boundary) with tiny throwaway jobs; record answers
  as SEM amendments + trace tests.
- Pull OpenAPI (U3b), pin UC version, verify the frozen base schema's write
  path (one live POST + GET readback), then unfreeze the rich condition forms.
- `autorep -q` samples may be INSPECTED by the user to inform synthetic fixture
  shapes but never committed (corpus hygiene).

<!-- hats:core -->
## Engineering core (hats)

This project uses the shared **hats engineering core**. Before substantive
work, read and follow `~/.hats/docs/USING.md`; it loads the hard rules
(`GUARDRAILS.md`), the engineering priors (`PRIORS.md`), and the validated
thinking tools. Re-read each session: the core is the source of truth and
its updates propagate here automatically. If `~/.hats` does not resolve,
the core is not linked on this machine (see the hats repo's README).
<!-- /hats:core -->
