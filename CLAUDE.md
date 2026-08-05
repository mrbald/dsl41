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
- **Open questions stay open.** Q1–Q9 (autosys dossier §9), U1–U8 (stonebranch
  Part III). DL-53 (2026-07-28) closed nine — Q1, Q4, Q5, U2, U4, U5, U6a, U7,
  U8 — each pinned to a dossier citation; DL-54 (same day) resolved Q2a
  (zero-lookback anchors to the dependent's own last end; anchor switch
  deleted); DL-55 (same day) split U3 — U3a (base CREATE-ONLY record schema)
  doc-frozen in docs/uc-edge-schema.md, U3b (rich forms + live write-path)
  open; DL-57 (2026-07-29) doc-froze extended-calendar semantics (SEM-36..39)
  and opened Q8a–Q8e/Q9; DL-58 (2026-07-30, verified-citation sweep) closed
  Q2b (never-run dependent: satisfied), Q3 (arm-and-wait confirmed, no-expiry
  latch; abandon switch deleted; new residue Q3c — box-arm scope), Q7
  (fail_codes decides alone — corner iv FLIPPED in ir.exit_is_success), Q8a
  (holiday action governs holcal dates; disagreement gate deleted), Q8e
  (CWEEK = period-start 7-day chunks), and E11 (run_calendar row-time firing;
  refusal deleted); DL-60 (same day) resolved Q9 from one observed
  autocal_asc export sample (extended_calendar: spelling, empty-valued keys
  emitted, workday `all`, braces as grouping, WORKD#L, holiday S without
  holcal, HH:MM:SS row tails — all folded into SEM-36/37 at the [F] tier
  and fixed in the interpreter/scanner). Do not relitigate any of them.
  Still open, with a live `# PENDING: Qn/Un` code marker: Q3c (oracle.py),
  Q8b–Q8d (autocal.py), U1, U3b. Q6 has no code switch, dossier-only
  (narrowed by DL-58: atom half cited; Q6-adjacent aside in oracle.py); U6b
  lives in backend_uc's `_U_QUESTIONS` table, pruned to exactly U1+U6b (U3b
  deliberately NOT there — it gates emission, not a mapping row; it surfaces
  via quarantine + the report footer). Q8b–Q8d close mechanically once a
  live instance exists: autocal date-set diff (dossier §9 + the runbook).
  E8 stays open runner-side (TERMINATED pin, FAILURE-leaning evidence
  recorded). DL-59 (2026-07-30, project decision — no live instance is
  available; the scheduler must load and schedule an ordinary estate):
  open COMPOSITION corners in the scheduler path carry documented
  deterministic defaults, never refusals — Q8b runs the pipeline order
  (replace-then-shift), Q8d(iv) evaluates all-exclusive compounds
  literally; CalendarRuleError is reserved for the genuinely
  uninterpretable (unknown/doc-defective tokens, missing deps, degenerate
  walks). A separate `# PENDING: Qr*` series in ir.py/oracle.py/runner.py
  is unrelated — resource-manager questions (DL-49/50), no dossier, outside
  this ledger. Do not guess-resolve any open question; implement the
  documented default and keep the switch where one exists.

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
`docs/live-instance-runbook.md` has the exact CLI protocols for every open
item plus the DL-58 source catalog (KB/thread/TechDocs URLs and the fetch
technique) — start there. Summary:
- Resolve E8 (one mid-run external kill + the trap-TERM KILLJOB variant to
  discriminate the mechanism), Q3c (member latch across box runs), and Q6
  (box_success over an iced member) with tiny throwaway jobs; record answers
  as SEM amendments + trace tests.
- Pull OpenAPI (U3b), pin UC version, verify the frozen base schema's write
  path (one live POST + GET readback), then unfreeze the rich condition forms.
- Close Q8b–Q8d (DL-57, pruned by DL-58/60): define the corner calendars on
  the instance, let autocal materialize (dates land in ujo_calendar on save,
  ~365 days — KB 14195), diff the vendor date sets against `dsl41`'s
  autocal.py generator (autocal_asc preview is the primary oracle,
  `job_depends -t -e` the consumption check). Q9 is resolved at the [F]
  tier (DL-60); one `autocal_asc -e ALL -E file` remains the byte-exact
  re-verification if access improves.
- `autorep -q` samples may be inspected locally to inform synthetic fixture
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
