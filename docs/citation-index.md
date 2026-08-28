# Citation index — every reference namespace the sources use

Status: NORMATIVE 2026-08-12 · DL-75. The sources carry roughly 2000
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
- **Not every row is exercised.** The scanner proposes one shape only:
  an uppercase-led word, then an optional hyphen, then digits. So the rows
  whose shape carries an infix hyphen (`P-M12`, `PR-Q3`), the `[EAR]-`
  class words, the `# PENDING:` marker and the tier brackets are never
  offered to the gate. They are reader-facing rows. Keep them right by
  reading, not by trusting a green build.

## Namespaces

| Token shape | Namespace | Means | Defined in |
| --- | --- | --- | --- |
| `SEM-\d{2}` | AutoSys semantics | One observed/cited AutoSys behavior. §8 of the dossier records its trace coverage: one oracle test per entry unless that entry names another suite, and two recorded non-goals with no trace test at all. An entry never splits by letter — a `SEM-10c`-shaped token in prose names a lettered test subcase (`test_sem10c_*`), not an entry of its own. | `docs/autosys-semantics.md` |
| `UCS-\d{1,2}` | UC semantics | One Stonebranch Universal Controller behavior. | `docs/stonebranch-semantics.md` Part I |
| `M\d{2}` | Mapping row | One AutoSys→UC mapping row, M01–M36. Provenance on every compiled UC edge (`UcEdge.mapping_row`). NOT a review finding — see the note below. | `docs/stonebranch-semantics.md` Part II |
| `P-M\d{2}` | Mapping trace pair | The oracle-pair test for one mapping row where migration can change behavior: same script, both interpreters. The expected result is a divergence for some rows and an alignment pin for others, and the row's class may be E, A, A/R or R — P-M27 pairs an R row, because absence is what that pair shows. Tests are named `test_pMxx_*`. | `docs/stonebranch-semantics.md` Part IV |
| `[EAR](/[EAR])?-(row\|rows\|class\|classified)` | Mapping-row class | How a mapping row migrates, in Part II's own words: **E** exact, **A** equivalent under stated assumption (the edge records it), **R** redesign required — no faithful translation, so the UC backend refuses the construct and emits a migration-report item instead. A slashed class (**A/R**, **E/A**) means the row splits by case; where a discriminator is implemented every derived edge still carries exactly one class. | `docs/stonebranch-semantics.md` Part II |
| `R\d` | Migration risk | One row of the migration risk register, R1–R8 — an AutoSys construct with no clean UC analog. NOT the same R as `R-classified` above. | `docs/autosys-semantics.md` §7 |
| `DL-\d{1,3}[a-z]?` | Decision log | A settled decision. Append-only: never edit or renumber an entry. | `docs/decision-log.md` |
| `L\d{3}` | Linter rule | One linter rule, L001–L022, with its stable code and severity. The house rule is a corpus fixture that trips the rule and one that does not; the lint suite names the exceptions it holds instead — L003 and L004 are defensive and structurally unreachable on real input, and L017 is registered but quiet because the corpus defines every machine it references. | `docs/ir-design.md` §9 |
| `CM-\d{2}` | Concurrency obligation | One obligation of the frozen concurrency model, CM-01–CM-23 — a property the multihost design must be *tested* to hold, not asserted to. Tests are named `test_cmNN_*`. The series runs past its first document: CM-01–CM-14 are the frozen contract's own, CM-15–CM-23 are the second host's and the second site's. A draft CM-24–CM-38 set was written in `ops-model.md` §9 and superseded there by the `PR-` series, so those numbers name nothing. None of them is cited by code: the pure run-history fold in `runner_history.py` cited `CM-37` and now cites DL-113, the entry that landed that pair with no obligation row. The regex still admits the drafted range, and a reader who follows one of those numbers should expect to find nothing. | `docs/concurrency-model.md` §9; `docs/ha-deployment.md` §7 |
| `S\d[a-f]?\|H` | Concurrency stage | One build stage of the frozen concurrency model, S0–S8 (S1 split into S1b/S1c; S5 into S5a–S5d by DL-93; S6 into S6a–S6c by DL-99; S7 into S7a–S7c by DL-108/DL-109/DL-112; S8 opened by the HA plan and split into S8a–S8f). One stage carries no number: **H**, the model harness, which comes before the code it validates. The stage order is normative: it fixes what must be frozen before what, and which single owner holds which file while a stage is open. | `docs/concurrency-model.md` §10; `docs/ha-deployment.md` §8 |
| `T-\d{3}` | Decompiler fold | One fold in the closed decompiler registry, T-001–T-007 (DL-38). The registry itself is `FOLDS` in `src/dsl41/dsl.py`. **Not `T01`** — see the note below. | `docs/decision-log.md` DL-38 |
| `T\d{2}[a-z]?` | AutoSys trace test | One entry of the oracle regression set, T01–T34 with lettered splits (T04a, T09b, T12a, T20a, T33a). One per SEM unless noted. **Not `T-001`** — see the note below. | `docs/autosys-semantics.md` §8 |
| `D\d` | IR design decision | One of the four IR/design decisions D1–D4. D1, D2 and D4 are closed; the numbers stay because the sources cite them. A `D\d` that sits beside a DL citation is that entry's own outline label, not this series — see the note below. | `docs/ir-design.md` §10 |
| `F[1-4]` | Fidelity test | One of the four scanner fidelity tests: **F1** preserve-mode identity over the corpus, **F2** canonical fixpoint, **F3** fuzz, **F4** lexical torture matrix. | `docs/jil-statement-syntax.md` |
| `ss\d{1,2}[a-z]?(\.\d{1,2})?` | Section reference | Section N **of the document named next to it** — "runner-design ss7", "dossier ss0", "ss6a". A subsection is spelled with a dot: `ss1.3`, `ss12.10`, `ss8a.4`. There is no repo-wide default for a bare `ssN`. A module that cites one document throughout says so in its docstring (`runner_effects.py`: "`ssN` in this module always names concurrency-model"); a module that cites several resolves each token from its own sentence, so `cli_common.py`'s `ss1.3` is the period model's and its `ss6` the concurrency model's. | the named document |
| `Q\d[a-z]?` | AutoSys open question | One question of the AutoSys semantics series, Q1–Q9 plus lettered splits (Q2a/Q2b, Q3a–Q3d, Q8a–Q8e). `Q8x` means "the Q8 family". The defining document says which are live and which are closed. A live one carries a `# PENDING: Qn` code marker only where the code holds a provisional default; Q6 is live and has none, because it has no code switch. **Unrelated to `Qr\d`** — see the note below. | `docs/autosys-semantics.md` §9; a later split may be recorded at the SEM entry that raised it (Q3d at SEM-32) |
| `Qr\d` | Resource-manager open question | One question of the resource-manager series, Qr1–Qr7. No dossier backs these; they are a runner-side series. **Unrelated to `Q\d`** — see the note below. | `docs/decision-log.md` DL-50 (DL-49 opened the resource work; every `Qr` number is stated in DL-50) |
| `U\d[a-z]?` | UC open question | One question of the Stonebranch series, U1–U8 plus lettered splits (U3a/U3b, U6a/U6b). The defining document says which are live and which are closed. | `docs/stonebranch-semantics.md` Part III |
| `E\d{1,2}` | Runner open question | One question of the runner/execution series, E1–E23. Implemented defaults carry a `# PENDING: En` marker. E1–E3 are answered in the sections that raise them (`runner-design.md` §7, §9, §11); §15 lists E4 onward. E12–E15 were opened by the HA plan, E16–E23 by the ops plan. | `docs/runner-design.md` §7, §9, §11, §15; `docs/ha-deployment.md` §11; `docs/ops-model.md` §11 |
| `PR-\d{2}[a-z]?` | Period-model obligation | One obligation of the frozen period model (DL-114), PR-01–PR-56 with lettered splits — a property the seal, the lineage fence and the classifier must be *tested* to hold **while the row is active**. Tests are named `test_prNN_*` / `test_prNNx_*`. A row has a state: a **retired** row cites the DL entry that retired it and names the replacement refusal tests, and it stays in the table so its citations still resolve (DL-138). | `docs/period-model.md` §13 |
| `PR-Q\d` | Period-model open question | One question of the frozen period model, PR-Q1–PR-Q5; §16 says which are live. A DIFFERENT series from `PR-\d{2}` above: that one is an obligation a test holds the code to, this one is a question no test can settle yet. A live one carries a `# PENDING: <token>` code marker where it has a code switch, naming the runner question it is carried as; PR-Q5 has none, because the anchor it asks about is a deployment shape and not a branch. | `docs/period-model.md` §16 |
| `I[12]` | Period invariant | The frozen period model's two structural invariants: **I1** — a period is exactly one segment (no size rolls; to roll, seal), **I2** — indices, epochs and run_numbers are monotone across the estate. | `docs/period-model.md` §1 |
| `B[12]` | Period baseline | The two `baseline_id`s one boundary spans: **B1** is the closing period's, **B2** the opening one's. Paired with `C[12]` and not the same thing — `C` names the `(catalog, RuntimeProfile)` side, `B` names the identity a client's `expect` was composed under, which is what makes an ordinary revision-bearing retry composed under B1 unanswerable after B2 opens. One command is exempt: a committed seal's exact retry is answered ahead of the baseline gate, because it necessarily carries B1 while B2 is the one answering (PR-30e). | `docs/period-model.md` §2.2, §4 |
| `C[12]` | Period side | The two sides of one boundary: **C1** is the closing period's `(catalog, RuntimeProfile)` pair, **C2** the staged opening one. Not an index into a numbered list — the name the period model gives its two inputs, and the vocabulary the classifier and the seal are written in. | `docs/decision-log.md` DL-131; used throughout `docs/period-model.md` §7, §10 |
| `# PENDING: (Q\d[a-z]?\|Qr\d\|U\d[a-z]?\|E\d{1,2})` | Code marker | The house convention for a documented default standing in for an open question. The token is a `Q`/`Qr`/`U`/`E` reference from the rows above; a `PR-Q` question is carried under the runner question it maps to, never under its own number. The hash belongs to the comment: in a docstring the marker is written `PENDING: E10`, without it. | `CLAUDE.md`; `docs/runner-design.md` §15 for the E half |
| `\[(V\|C\|F\|\?\|V/C\|V/\?\|C/\?)\]` | Evidence tier | How well a claim is backed: **[V]** verified against a cited public source, **[C]** corroborated by several secondary sources, **[F]** one unverified field observation — the weakest tier, **[?]** open. A slashed pair splits one entry — the first letter backs the core, the second the named residue. Only three pairs are in use: **[V/C]**, **[V/?]**, **[C/?]**. | `docs/autosys-semantics.md` header; `docs/stonebranch-semantics.md` header for the pairs |

## Five collisions worth stating plainly

**`Q\d` and `Qr\d` are unrelated series.** One letter separates them and
nothing else does. `Q8b` is an AutoSys extended-calendar question from the
dossier's §9; `Qr2` is a resource-manager priority question from DL-49/50.
They have different owners, different documents, and different resolution
paths (a live AutoSys instance vs. a design decision). Reading one as the
other will send you to the wrong document.

**`M\d{2}` is a mapping row, not a review finding.** `M07` is mapping row 7
in the stonebranch dossier. Sources used to also carry `review M-1`-shaped
tokens pointing at review conversations; those had no in-repo index, so a
reader could not follow them. DL-75 deleted every one in `src/dsl41/`,
where the gate runs, and inlined the reason it stood for. That sweep did
not reach `tests/`; DL-151 finished it there — the eighteen it left
(five in `test_uc_oracle.py`, six in `test_runner_lifecycle.py`, seven in
`test_seal_artifact.py`), hyphenated (`Review M-1`, `Review E-1`),
unhyphenated (`Review M3`, `Review B1`) and compound (`review U6A-05`,
`review U6AR3-04`), now state their reason instead of pointing at it. Two
kinds of residue are left and neither is a citation: some test FUNCTION
NAMES still carry the finding they were written for (`test_review_m2_*`,
`test_m3_*`, `test_b1_*`), and severity-labelled pointers (`Review MAJOR
2`, `Review BLOCKER`) remain in four other test files. An `M`-shaped
citation in `src/` is always a mapping row.

**The two `R`s are different things.** `R3` is risk-register row 3 in the
AutoSys dossier (a construct with no clean UC analog); `R-classified` is
the migration verdict on a mapping row (the UC backend refuses to compile
it). They are related in spirit and unrelated in numbering — an `R\d` never
names a mapping row.

**`T01` and `T-001` differ by one hyphen and share nothing else.** `T05` is
trace test 5 in the dossier's §8, an oracle regression over AutoSys
semantics. `T-005` is decompiler fold 5 in the closed `FOLDS` registry.
Different document, different phase of the pipeline, different owner.

**A `D\d` beside a DL citation is that entry's own label.** `D1`–`D4` in
`ir-design.md` §10 are that document's design decisions. DL-138 numbers its own
outline `D1`–`D9`, and the code cites those the same way — `# D5, DL-138`
in `boundary.py` is a tombstone rule, not a deferred decision. The
adjacent DL number is what tells them apart, so keep it on every such
citation.

## Document-local labels are not namespaces

Some documents number the rows of their own tables. Those labels look like
citations and are not: they mean nothing outside the section that defines
them, and several of them shadow a real namespace.

- `ops-model.md` §5, the scenario catalogue: `A1`–`F6`. Its `E1`–`E11` are
  intervention rows, not runner open questions; `B1`/`B2`, `C1`/`C2`,
  `D1`–`D4` and `F1`–`F4` each shadow a row above. One section holds both
  readings at once: row `E9` is break-glass supervisor shutdown, while row
  `C4`, two sections earlier, cites `E9` the runner question. §5 says so
  where its tables start.
- `ops-model.md` §8b.2, the state-inventory sweep: `G1`–`G11`, all resolved
  and kept for the record.
- `period-model.md` §0a, the revision history: draft and review labels
  (`D25`, `R31`) naming one round of one draft.
- `period-model.md` §14, the worked estate: `B1` is the boundary that
  commits and `B2` the one that refuses — two worked scenarios, not the two
  `baseline_id`s of the row above.
- `docs/decision-log.md` build units: `U6b`–`U9` and `L1`/`L2` name a unit
  of work in one entry, not a UC question and not a linter rule. `U6b` is
  the one that reads both ways: the ledger's is a build unit (DL-133), and
  a bare `U6b` in a source file is the UC question of the row above.

None of these gets a row. A reader who meets one inside its own document
reads it there; a token of that shape anywhere else belongs to the table
above.

## Retired namespaces — do not reintroduce

| Token shape | Why it went |
| --- | --- |
| `sol #\d+` | Pointed at a numbered solution in a review conversation with no in-repo index. DL-75 inlined the actual reason at all five sites (all of them "this artifact is owner-only because of what it holds") and deleted the token. |
| `review [A-Z]-\d+`, `Review [A-Z]\d+`, `review [A-Z0-9]+-\d+` | All three shapes pointed at findings in review conversations with no in-repo index. DL-75 inlined the reason at every `src/dsl41/` site; where the finding restated a dossier entry, the dossier citation replaced it. The sweep did not reach `tests/`: `test_uc_oracle.py`, `test_runner_lifecycle.py` and `test_seal_artifact.py` still carry the three shapes in docstrings. |

A citation whose target is a conversation is not a citation — it is a note
to the one person who was in the room. If a review produces something worth
keeping, the reason goes in the comment and the decision goes in
`docs/decision-log.md`.
