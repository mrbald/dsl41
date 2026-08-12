# Citation index — every reference namespace the sources use

Status: NORMATIVE 2026-08-12 · DL-75. The sources carry roughly 1600
citation references. Traceability is this project's core discipline, so
they stay — but a reader must be able to follow each one to a document.
This index is the map: one row per namespace, giving the token shape, what
the token means, and the document that defines it.

Two rules follow from it:

- **A citation must resolve.** A token in `src/dsl41/*.py` whose namespace
  has no row here is a defect; `scripts/arch_check.py` fails the build on
  one (blocking check 3). Adding a namespace means adding a row here first.
- **The `Token shape` column is machine-read.** `arch_check.py` parses the
  regexes out of the table below, so keep them exact and keep them in
  backticks.

## Namespaces

| Token shape | Namespace | Means | Defined in |
| --- | --- | --- | --- |
| `SEM-\d{2}` | AutoSys semantics | One observed/cited AutoSys behavior. Each implies a trace test (§8 of the dossier). | `docs/autosys-semantics.md` |
| `UCS-\d{1,2}` | UC semantics | One Stonebranch Universal Controller behavior. | `docs/stonebranch-semantics.md` Part I |
| `M\d{2}` | Mapping row | One AutoSys→UC mapping row, M01–M36. Provenance on every compiled UC edge (`UcEdge.mapping_row`). NOT a review finding — see the note below. | `docs/stonebranch-semantics.md` Part II |
| `P-M\d{2}` | Mapping trace pair | The oracle-pair test for one A-classified mapping row: same script, both interpreters, expected divergence. Tests are named `test_pMxx_*`. | `docs/stonebranch-semantics.md` Part IV |
| `[EAR]-(row\|rows\|class\|classified)` | Mapping-row class | How a mapping row migrates: **E** exact, **A** approximate (the edge records its assumption), **R** refused (the UC backend emits a migration-report item instead of compiling it). | `docs/stonebranch-semantics.md` Part II |
| `R\d` | Migration risk | One row of the migration risk register, R1–R8 — an AutoSys construct with no clean UC analog. NOT the same R as `R-classified` above. | `docs/autosys-semantics.md` §7 |
| `DL-\d{1,3}[a-z]?` | Decision log | A settled decision. Append-only: never edit or renumber an entry. | `docs/decision-log.md` |
| `L\d{3}` | Linter rule | One linter rule, L001–L019, with its stable code and severity. Each ships a corpus fixture that trips it and one that does not. | `docs/ir-design.md` §9 |
| `T-\d{3}` | Decompiler fold | One fold in the closed decompiler registry, T-001–T-007 (DL-38). The registry itself is `FOLDS` in `src/dsl41/dsl.py`. | `docs/decision-log.md` DL-38 |
| `D\d` | Open design decision | One deliberately deferred IR/design decision, D1–D4. | `docs/ir-design.md` §10 |
| `F[1-4]` | Fidelity test | One of the four scanner fidelity tests (preserve identity, canonical fixpoint, fuzz, corpus). | `docs/jil-statement-syntax.md` |
| `ss\d{1,2}[a-z]?` | Section reference | Section N **of the document named next to it** — "runner-design ss7", "dossier ss0", "ss6a". Bare `ssN` in a runner module means `docs/runner-design.md` §N. | the named document |
| `Q\d[a-z]?` | AutoSys open question | An unresolved AutoSys semantics question, Q1–Q9 plus lettered splits (Q8a…Q8e). `Q8x` means "the Q8 family". Live ones carry a `# PENDING: Qn` code marker. **Unrelated to `Qr\d`** — see the note below. | `docs/autosys-semantics.md` §9 |
| `Qr\d` | Resource-manager open question | An unresolved resource-manager question from DL-49/DL-50. No dossier backs these; they are a runner-side series. **Unrelated to `Q\d`** — see the note below. | `docs/decision-log.md` DL-49, DL-50 |
| `U\d[a-z]?` | UC open question | An unresolved Stonebranch question, U1–U8 plus lettered splits (U3a/U3b, U6a/U6b). | `docs/stonebranch-semantics.md` Part III |
| `E\d{1,2}` | Runner open question | An unresolved runner/execution question, E1–E11. Implemented defaults carry a `# PENDING: En` marker. | `docs/runner-design.md` §15 |
| `# PENDING: <token>` | Code marker | The house convention for a documented default standing in for an unresolved question. The token is a `Q`/`Qr`/`U`/`E` reference from the rows above. | `CLAUDE.md` |
| `\[[VF?]\]` | Evidence tier | How well a claim is backed: **[V]** verified against a cited public source, **[F]** one unverified field observation, **[?]** open. | `docs/autosys-semantics.md` |

## Three collisions worth stating plainly

**`Q\d` and `Qr\d` are unrelated series.** One letter separates them and
nothing else does. `Q8b` is an AutoSys extended-calendar question from the
dossier's §9; `Qr2` is a resource-manager priority question from DL-49/50.
They have different owners, different documents, and different resolution
paths (a live AutoSys instance vs. a design decision). Reading one as the
other will send you to the wrong document.

**`M\d{2}` is a mapping row, not a review finding.** `M07` is mapping row 7
in the stonebranch dossier. Sources used to also carry `review M-1`-shaped
tokens pointing at review conversations; those had no in-repo index, so a
reader could not follow them. DL-75 deleted every one and inlined the
reason it stood for, so the only `M`-shaped citation left in the tree is a
mapping row.

**The two `R`s are different things.** `R3` is risk-register row 3 in the
AutoSys dossier (a construct with no clean UC analog); `R-classified` is
the migration verdict on a mapping row (the UC backend refuses to compile
it). They are related in spirit and unrelated in numbering — an `R\d` never
names a mapping row.

## Retired namespaces — do not reintroduce

| Token shape | Why it went |
| --- | --- |
| `sol #\d+` | Pointed at a numbered solution in a review conversation with no in-repo index. DL-75 inlined the actual reason at all five sites (all of them "this artifact is owner-only because of what it holds") and deleted the token. |
| `review [A-Z]-\d+` | Pointed at findings in review conversations with no in-repo index. DL-75 inlined the reason at every site; where the finding restated a dossier entry, the dossier citation replaced it. |

A citation whose target is a conversation is not a citation — it is a note
to the one person who was in the room. If a review produces something worth
keeping, the reason goes in the comment and the decision goes in
`docs/decision-log.md`.
