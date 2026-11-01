# Citation index — every reference namespace the sources use

For maintainers resolving citation tokens or registering a namespace.
Follow the maintainer track under [Documents](../README.md#documents).
Writing rules belong to [writing.md](writing.md); verification belongs to
[agent-workflow.md](agent-workflow.md#verify-a-change).

Status: normative (DL-75). Each row gives a token's shape, meaning and authority.
An excluded namespace keeps a decision-log destination so existing references resolve.
The authority for excluded UC, multihost, supervisor and period mechanisms is
[DL-424](decision-log.md). That exclusion does not retire every CM or PR label.
A label cited by current source or a named obligation test remains live;
its defining entry or test supplies the rule. The rows distinguish these labels
from excluded mechanisms.

The registry rules and scanner limits follow:

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
| `SEM-\d{2}` | AutoSys semantics | One observed/cited AutoSys behavior. §8 of the dossier records its trace coverage: one oracle test per entry unless that entry names another suite, and two recorded oracle non-goals with no trace test: SEM-16's definition-time mutation of a running box and SEM-17's ACTIVATED label. An entry never splits by letter — a `SEM-10c`-shaped token in prose names a lettered test subcase (`test_sem10c_*`), not an entry of its own. | `docs/autosys-semantics.md` |
| `UCS-\d{1,2}` | UC semantics | Excluded UC behavior labels; UCS numbers resolve through the UC exclusion. | `docs/decision-log.md` DL-288 as ruled by DL-424 |
| `M\d{2}` | Mapping row | Mapping labels M01–M36; M07 remains graph vocabulary. These are not review findings. The UC target is excluded. | `docs/decision-log.md` DL-424; surviving graph use in `docs/ir-design.md` §5 |
| `P-M\d{2}` | Mapping trace pair | Excluded mapping-pair labels P-M01–P-M36 and their `test_pMxx_*` spelling; no current pair-test qualification is implied. | `docs/decision-log.md` DL-424 |
| `[EAR](/[EAR])?-(row\|rows\|class\|classified)` | Mapping-row class | Excluded mapping verdicts: E exact, A equivalent under an assumption, R redesign required; A/R and E/A split by case. | `docs/decision-log.md` DL-424 |
| `R\d` | Migration risk | One run-scoped graph risk, R1–R8, from the AutoSys semantics dossier. NOT the same R as `R-classified` above. | `docs/autosys-semantics.md` §7 |
| `DL-\d{1,3}[a-z]?` | Decision log | A settled decision. Append-only: never edit, renumber or delete an entry, except a superseded-by mark and a DL-379 compression tranche. | `docs/decision-log.md` |
| `L\d{3}` | Linter rule | One linter rule, L001–L025, with its stable code and severity. The house rule is a corpus fixture that trips the rule and one that does not; the lint suite names the exceptions it holds instead — L003 and L004 are defensive and structurally unreachable on real input, L017 is registered but quiet because the corpus defines every machine it references, L023 is tested on inline JIL because its zones and reference dates are part of each case, and L024 and L025 are tested on inline JIL and a loaded exceptions object because their input is the sidecar, not a JIL file. | `docs/ir-design.md` §9 |
| `CM-\d{2}` | Concurrency obligation | CM-01–CM-14 identify single-host obligations, including admission and replay. Tests are named `test_cmNN_*`. DL-84 defines CM-01–CM-14; DL-89 defines CM-04, CM-05 and CM-07 at S2; DL-93 defines the CM-09–CM-13 and S5 split. CM-15–CM-23 concern the excluded multihost/HA mechanisms (DL-424). CM-24–CM-38 are withdrawn draft labels (DL-230); CM-37 run-history references resolve to DL-113. | `docs/decision-log.md` DL-89 and DL-93; `tests/test_runtime_state.py`, `tests/test_admission.py`, `tests/test_dispatch.py` and `tests/test_runner_leadership.py` for retained single-host checks; DL-424, DL-230 and DL-113 for the excluded or draft labels |
| `S\d[a-f]?\|H` | Concurrency stage | Stage labels H and S0–S8, including S1b/S1c. S2 admission is DL-89. DL-93 defines S5a–S5d; DL-99 defines S6a–S6c; DL-108, DL-109 and DL-112 define S7a, S7b and S7c. S8a–S8f resolve through DL-189. Definitions remain citable; DL-424 rules which mechanisms are excluded. | `docs/decision-log.md` DL-89, DL-93, DL-99, DL-108, DL-109, DL-112 and DL-189; DL-424 for mechanism exclusions |
| `T-\d{3}` | Decompiler fold | One fold in the closed decompiler registry, T-001–T-007 (DL-38). The registry itself is `FOLDS` in `src/dsl41/dsl.py`. **Not `T01`** — see the note below. | `docs/decision-log.md` DL-38 |
| `T\d{2}[a-z]?` | AutoSys trace test | One entry of the oracle regression set, T01–T34 with lettered splits (T04a, T09b, T12a, T20a, T33a). One per SEM unless noted. **Not `T-001`** — see the note below. | `docs/autosys-semantics.md` §8 |
| `D\d` | IR design decision | One of the four IR/design decisions D1–D4. D1, D2 and D4 are closed; the numbers stay because the sources cite them. A `D\d` that sits beside a DL citation is that entry's own outline label, not this series — see the note below. | `docs/ir-design.md` §10 |
| `F[1-4]` | Fidelity test | One of the four scanner fidelity tests: **F1** preserve-mode identity over the corpus, **F2** canonical fixpoint, **F3** fuzz, **F4** lexical torture matrix. | `docs/jil-statement-syntax.md` |
| `ss\d{1,2}[a-z]?(\.\d{1,2})?` | Section reference | Section N **of the document named next to it** — "runner-design ss7", "dossier ss0", "ss6a". A subsection is spelled with a dot: `ss1.3`, `ss12.10`, `ss8a.4`. There is no repo-wide default for a bare `ssN`. A module says in its docstring which document a bare `ssN` names (`runner_admission.py`: "Bare `ssN` elsewhere in the runner means docs/runner-design.md"). A module that cites several documents resolves each token from its own sentence. For example, `cli_common.py` names concurrency-model `ss6` for the control read header; the current envelope rule is `control-protocol.md` §3. | The named document; `docs/decision-log.md` DL-424 for excluded documents |
| `Q\d{1,2}[a-z]?` | AutoSys open question | One question of the AutoSys semantics series, Q1–Q17 plus lettered splits (Q2a/Q2b, Q3a–Q3d, Q8a–Q8e). `Q8x` means "the Q8 family". The defining document says which are live and which are closed. A live one carries a `# PENDING: Qn` code marker only where the code holds a provisional default; Q10 has one in `semantics.iced_atom_truth`, Q15 one in `oracle_cascade.py` and Q16 one in `oracle_facts.py`, and Q6, Q13, Q14 and Q17 are live with none. Q11 is closed (DL-259). Q12 is descoped (DL-288): preflight refuses FREE=Y and FREE=A on a depletable (DL-287). **Unrelated to `Qr\d`** — see the note below. | `docs/autosys-semantics.md` §9; a later split may be recorded at the SEM entry that raised it (Q3d at SEM-32) |
| `Qr\d` | Resource-manager open question | One question of the resource-manager series, Qr1–Qr7. No dossier backs these; they are a runner-side series. **Unrelated to `Q\d`** — see the note below. | `docs/decision-log.md` DL-50 (DL-49 opened the resource work; every `Qr` number is stated in DL-50) |
| `U\d[a-z]?` | UC open question | Excluded UC questions U1–U8, including U3a/U3b and U6a/U6b. Unrelated to runner E questions. | `docs/decision-log.md` DL-288 as ruled by DL-424 |
| `E\d{1,2}` | Runner open question | One question of the runner/execution series, E1–E23. Implemented defaults carry a `# PENDING: En` marker. E1–E3 are answered in the sections that raise them (`runner-design.md` §7, §9, §11); §15 lists E4 onward. DL-189 and DL-230 resolve the HA and ops-plan labels. §15 states which questions remain in scope under DL-424. | `docs/runner-design.md` §7, §9, §11, §15; `docs/decision-log.md` DL-189 and DL-230 as ruled by DL-424 |
| `PR-\d{2}[a-z]?` | Period-model obligation | Obligation labels PR-01–PR-57 and lettered subcases. A label cited in current source or carrying a `test_prNN_*` / `test_prNNx_*` test is live. Canonical-form rules remain under DL-119 and DL-128; the live-label destinations below identify other retained rules. DL-424 excludes period, seal, carry and lineage mechanisms, not the entire PR namespace. Retired-row interpretation is recorded in DL-138. | `docs/decision-log.md` DL-119 and DL-128; the current source and test destinations below; DL-114 and DL-138 as ruled by DL-424 for excluded mechanisms |
| `PR-Q\d` | Period-model open question | Excluded period questions PR-Q1–PR-Q5. These are not PR obligation rows. Runner-carried questions resolve through the E series. | `docs/decision-log.md` DL-114 as ruled by DL-424; `docs/runner-design.md` §15 |
| `I[12]` | Period invariant | Excluded period invariants I1 (one segment per period) and I2 (monotone indices, epochs and run numbers across an estate). They define no current store invariant. | `docs/decision-log.md` DL-114 as ruled by DL-424 |
| `B[12]` | Period baseline | Excluded boundary-side names B1 and B2; PR-30e is a seal-retry label. The current `baseline_id` identifies an engine store and survives restart. | `docs/decision-log.md` DL-424; `docs/control-protocol.md` §3 for current retry identity |
| `C[12]` | Period side | Excluded boundary inputs C1 and C2, the closing and opening `(catalog, RuntimeProfile)` pairs. | `docs/decision-log.md` DL-131 as ruled by DL-424 |
| `# PENDING: (Q\d{1,2}[a-z]?\|Qr\d\|U\d[a-z]?\|E\d{1,2})` | Code marker | The house convention for a documented default standing in for an open question. The token is a `Q`/`Qr`/`U`/`E` reference from the rows above; a `PR-Q` question is carried under the runner question it maps to, never under its own number. The hash belongs to the comment: in a docstring the marker is written `PENDING: E10`, without it. | `docs/citation-index.md`, this row; the Q/Qr/U/E destinations above define each question |
| `[a-z][a-z_]*\.\d{2,3}` | State-machine transition | A declared transition id, such as `job.101`: machine name, dot, transition number. The generated reference defines each current row. Composite job ids retain the job_status.NN to job.1NN, job_flags.NN to job.2NN and job_holding.NN to job.3NN correspondence (DL-337, DL-353). | `docs/state-machines.md`, generated from `src/dsl41/machines.py` (DL-289) |
| `\[(V\|C\|F\|\?\|V/C\|V/\?\|C/\?)\]` | Evidence tier | How well a claim is backed: **[V]** verified against a cited public source, **[C]** corroborated by several secondary sources, **[F]** one unverified field observation — the weakest tier, **[?]** open. A slashed pair splits one entry — the first letter backs the core, the second the named residue. Only three pairs are in use: **[V/C]**, **[V/?]**, **[C/?]**. | `docs/autosys-semantics.md` header; `docs/decision-log.md` DL-424 for excluded UC uses |

Live PR labels resolve by the rule they name:

- PR-08, PR-08d, PR-09, PR-10, PR-10a, PR-11, PR-12 and PR-13:
  DL-119 and DL-128, with `tests/test_canon.py` and `tests/test_oracle_branches.py`.
- PR-08a, PR-08c, PR-15 and PR-15a: `tests/test_period_identity.py`
  and `tests/test_semantics.py` pin catalog/runtime hashes and resolved profile values.
- PR-19, PR-19a, PR-20, PR-21, PR-22 and PR-52:
  `tests/test_capacity_decomposition.py` pins capacity state and its validation.
- PR-01c: `tests/test_runner_leadership.py`; PR-03:
  `tests/test_control_branches.py`; PR-25a: `tests/test_runner_scheduler.py`.
- PR-34 and PR-34a: `tests/test_fw_spool.py`; PR-35 and PR-36a:
  `tests/test_decision_record.py` and `tests/test_runner_adapters.py`.
- PR-50's retained field citation is in `src/dsl41/oracle_state.py`;
  its period mechanism is subject to DL-424. A retained label does not
  authorize an excluded mechanism.

## Five collisions worth stating plainly

**`Q\d` and `Qr\d` are unrelated series.** One letter separates them and
nothing else does. `Q8b` is an AutoSys extended-calendar question from the
dossier's §9; `Qr2` is a resource-manager priority question from DL-49/50.
They have different owners, different documents, and different resolution
paths (a live AutoSys instance vs. a design decision). Reading one as the
other will send you to the wrong document.

**`M\d{2}` is a mapping row, not a review finding.** M07 names a mapping
row. Review M-1, Review E-1, Review M3, Review B1, review U6A-05 and
review U6AR3-04 resolve through DL-75 and DL-151; they are not citation
namespaces. Test-name fragments (`test_review_m2_*`, `test_m3_*`,
`test_b1_*`) and severity labels (Review MAJOR 2, Review BLOCKER) do not
define citations. An M-shaped citation uses the namespace table above.

**The two `R`s are different things.** `R3` is risk-register row 3 in the
AutoSys dossier (a risk in a run-scoped graph); `R-classified` is
the excluded UC mapping verdict defined through DL-424. An `R\d` never
names a mapping row.

**`T01` and `T-001` differ by one hyphen and share nothing else.** `T05` is
trace test 5 in the dossier's §8, an oracle regression over AutoSys
semantics. `T-005` is decompiler fold 5 in the closed `FOLDS` registry.
Different document, different phase of the pipeline, different owner.

**A `D\d` beside a DL citation is that entry's own label.** `D1`–`D4` in
`ir-design.md` §10 are that document's design decisions. DL-138 numbers its own
outline `D1`–`D9`, and the code cites those the same way — `# D5, DL-138`
in the runner source is a tombstone rule, not a deferred decision. The
adjacent DL number is what tells them apart, so keep it on every such
citation.

## Document-local labels are not namespaces

Some documents number the rows of their own tables. Those labels look like
citations and are not: they mean nothing outside the section that defines
them, and several of them shadow a real namespace.

- The period-model.md §14 worked-estate labels B1 (commit) and B2 (refusal)
  resolve through DL-114 as ruled by DL-424; they are not baseline ids.
- `docs/decision-log.md` build units: `U6b`–`U9` and `L1`/`L2` name a unit
  of work in one entry, not a UC question and not a linter rule. `U6b` is
  the one that reads both ways: the ledger's is a build unit (DL-133), and
  a bare `U6b` in a source file is the UC question of the row above.

None of these gets a row. A reader resolves a local label in its named decision or section;
a token of that shape elsewhere belongs to the namespace table above.

## Retired namespaces — do not reintroduce

| Token shape | Why it went |
| --- | --- |
| `sol #\d+` | Conversation-local solution label; authority: DL-75. State the owner-only artifact rule directly instead of citing a conversation. |
| `review [A-Z]-\d+`, `Review [A-Z]\d+`, `review [A-Z0-9]+-\d+` | Conversation-local finding labels; authority: DL-75 and DL-151. State the rule and cite its defining entry or dossier. |

A citation whose target is a conversation is not a citation — it is a note
to the one person who was in the room. If a review produces something worth
keeping, the reason goes in the comment and the decision goes in
`docs/decision-log.md`.

The stonebranch-semantics.md Parts I–IV, concurrency-model.md §§9–10,
period-model.md §§1, 2.2, 4, 7, 10, 13–14 and 16, and the HA plan references
resolve through DL-424 and the per-label decision pointers above.

Stage-split references resolve to their definitions: S5a–S5d in DL-93,
S6a–S6c in DL-99, and S7a/S7b/S7c in DL-108/DL-109/DL-112.
The host.03 transition reference resolves through DL-424; job.100, job.199 and job.4NN
labels resolve through DL-337, DL-349 and DL-350 and the generated transition reference.
The `ss1.3` spelling illustrates subsection notation; it is not a current source citation.
The concurrency `ss6` read-header references in `cli_common.py` resolve to
[control-protocol §3](control-protocol.md#3-mutating-verb-sendevent).
Current store recovery is [runner-design §7](runner-design.md#7-journal-and-recovery-e1-prod-grade),
and mutation envelopes are [control-protocol §3](control-protocol.md#3-mutating-verb-sendevent).
