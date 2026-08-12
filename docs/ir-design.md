# IR Design

Status: draft v0.1. This document depends on `autosys-semantics.md` (SEM-xx) and on
`stonebranch-semantics.md` (UCS-xx, M-xx). It is normative for: the parser, the linter, the
visualizer, the equivalence validator, the oracle, the UC backend, and the DSL decompiler.

Design stance (from the project reboot decision): the IR is **AutoSys-shaped first**. The IR
captures JIL semantics faithfully. Vendor neutrality can emerge at Layer G only where the UC
backend forces a distinction. Constitutional carry-over from dsl42: the compiler is pure, with
no runtime. A failed translation is a loud, classified error, never silent loss.

---

## 1. Pipeline & representations

```
JIL text ──parse──▶ AST ──lower──▶ IR-F ──derive──▶ IR-G ──compile──▶ UC records (XML/JSON)
   ▲                 │               │                 │        └────▶ migration report (md)
   └───render────────┘               │                 ├────▶ Mermaid
        (fidelity round trip)        │                 └────▶ DSL source (decompiler)
                                     ├────▶ linter findings
                                     └────▶ oracle (discrete-event interpreter)
```

The four representations have four contracts:

| repr | contract | loss policy |
|---|---|---|
| **AST** | byte-faithful syntax; `parse∘render == id` (preserve mode); `render∘parse∘render == render` (canonical mode is a fixpoint) | zero loss, ever. Unknown attributes, comments, ordering, and whitespace style all survive |
| **IR-F** (faithful) | semantics-complete per SEM entries; `AST→IR-F` total on the supported attribute set, hard error on semantically load-bearing constructs we don't model | lowering can normalize syntax (abbreviations, formats) but never semantics |
| **IR-G** (derived) | analysis product: dependency graph + classifications; regenerable from IR-F at any time (`derive` is pure) | explicitly lossy. Every loss is materialized as an annotation |
| **UC records** | valid UC 7.x import payloads; only E/A-classified edges compile | R-classified constructs become migration-report items, compile refuses to emit them silently |

`IR-F` is the source of truth for equivalence and simulation. Never hand-edit `IR-G`, and
never serialize it as authority (this mirrors the `.nodebook/`-style "index is not truth"
discipline).

## 2. AST layer

The surface grammar of JIL is line-oriented statements. A statement is a subcommand attribute
(`insert_job`, `update_job`, `delete_job`, `insert_machine`, `insert_global`, `override_job`,
…) followed by attribute lines until the next subcommand.

```python
class SourceSpan(BaseModel):
    file: str; line_start: int; line_end: int; byte_start: int; byte_end: int

class Comment(BaseModel):
    text: str                  # raw, including '/*...*/' or '#...' marker
    span: SourceSpan
    attachment: Literal["leading", "trailing", "floating"]

class RawAttr(BaseModel):
    key: str                   # exactly as written (case preserved)
    raw_value: str             # verbatim, unstripped semantics-neutral trim only
    span: SourceSpan
    comments: list[Comment] = []

class JilStatement(BaseModel):
    subcommand: str            # e.g. "insert_job"
    subject: str               # the value after the subcommand key (job name, etc.)
    job_type_inline: str | None  # 'insert_job: X job_type: c' one-line form support
    attrs: list[RawAttr]       # ORDER PRESERVED — this is the fidelity guarantee
    date_lines: list[str] = [] # autocal standard-calendar date rows, verbatim (rule 11/DL-36)
    comments: list[Comment] = []
    span: SourceSpan

class JilFile(BaseModel):
    statements: list[JilStatement]
    trailing_comments: list[Comment] = []
    newline_style: Literal["\n", "\r\n"]
```

Notes:
- **No interpretation at this layer.** `condition` is a RawAttr like any other attribute.
  Lowering parses its expression. This keeps `jil→ast→jil` trivially total.
- There are three fidelity tests. Test (1) is preserve-mode identity on the whole test corpus.
  Test (2) is the canonical-mode fixpoint. Test (3) is fuzz: parse-render-parse equality on
  random JIL-shaped text where parse succeeds.
- Canonical mode (used for diffs and stored artifacts) has a stable attribute order (subcommand
  first, then a fixed key order, unknown keys alphabetically last) and a single space after the
  colon. Canonical mode does NOT expand abbreviations (that step is IR-level, and the AST
  canonical form is purely lexical).

## 3. IR-F: condition algebra

Source: SEM-02/03/04/07/08.

```python
Status = Literal["SUCCESS","FAILURE","DONE","TERMINATED","NOTRUNNING"]
CmpOp  = Literal["=","!=","<",">","<=",">="]

class Lookback(BaseModel):
    kind: Literal["window","zero","indefinite"]   # SEM-04
    minutes: int | None       # for kind=window; parsed from hhhh.mm / hhhh\:mm
    raw: str                   # original token, for round-trip + Q2 auditing

class JobRef(BaseModel):
    name: str
    instance: str | None       # cross-instance '^INST' (SEM-07)

class StatusAtom(BaseModel):
    kind: Literal["status"] = "status"
    job: JobRef; status: Status
    lookback: Lookback | None   # None == indefinite w/o explicit token

class ExitCodeAtom(BaseModel):
    kind: Literal["exitcode"] = "exitcode"
    job: JobRef; op: CmpOp; value: int
    lookback: Lookback | None

class GlobalAtom(BaseModel):
    kind: Literal["global"] = "global"
    name: str; op: CmpOp; value: str      # lookback FORBIDDEN here (SEM-04) — validator enforces

class And(BaseModel):
    kind: Literal["and"] = "and"; operands: list["Cond"]   # n-ary, flattened
class Or(BaseModel):
    kind: Literal["or"] = "or"; operands: list["Cond"]
class Paren(BaseModel):
    kind: Literal["paren"] = "paren"; inner: "Cond"        # fidelity only; erased in canonical

Cond = Annotated[StatusAtom|ExitCodeAtom|GlobalAtom|And|Or|Paren, Field(discriminator="kind")]
```

- Parser precedence: **resolved (DL-53): left-associative, & and | equal precedence** —
  per TechDocs 12.1 "The parentheses force precedence, and the equation is evaluated
  from left to right." There is a single grammar rule (the earlier C-style candidate is
  deleted). The pinning tests `test_sem03_flat_left_to_right_precedence_pinned` (grammar) and
  `test_sem03_precedence_pinned_model_level` (Cond model) hold this shape.
- There is no negation node (SEM-03): the atom set is closed under the actual language of
  AutoSys.

## 4. IR-F: entities

```python
class ScheduleBlock(BaseModel):          # SEM-30..35; present iff date_conditions truthy
    days_of_week: list[str] | None       # XOR run_calendar (SEM-31) — model validator
    run_calendar: str | None
    exclude_calendar: str | None
    start_times: list[Time] | None       # XOR start_mins (SEM-31)
    start_mins: list[int] | None
    run_window: tuple[Time, Time] | None # semantics: SEM-33 gate, NOT trigger
    timezone: str | None
    must_start: SlaSpec | None           # SEM-34: annotation class
    must_complete: SlaSpec | None

class BoxLinkage(BaseModel):
    box_name: str | None
    box_terminator: bool = False         # SEM-14
    job_terminator: bool = False

class ExecSpec(BaseModel):               # command jobs; FW/other types analogous subclasses
    command: str                         # may contain $$VAR sites — kept verbatim,
    machine: str | None                  #   substitution sites indexed separately (below)
    owner: str | None
    profile: str | None
    std_in_file: str | None              # CMD-only; may name a blob (DL-32)
    std_out_file: str | None
    std_err_file: str | None
    envvars: str | None                  # CMD-only; NAME=value list, verbatim (DL-32)

class CondAttr(BaseModel):               # one condition-bearing attribute (DL-73)
    cond: Cond
    span: SourceSpan | None              # where the attr sits in the source file

class Semantics(BaseModel):              # attributes with control-flow teeth (§5 of dossier)
    condition: CondAttr | None
    max_exit_success: int = 0            # SEM-09
    success_codes: list[tuple[int, int]] | None   # SEM-09/DL-33, CMD-only; verdict via
    fail_codes: list[tuple[int, int]] | None      #   ir.exit_is_success (Q7 cited, DL-58)
    term_run_time_min: int | None
    n_retrys: int = 0
    box_success: CondAttr | None         # box jobs only; SEM-12
    box_failure: CondAttr | None
    auto_hold: bool = False

class JobIR(BaseModel):
    name: str
    job_type: str                        # 'CMD','BOX','FW', + extensible
    box: BoxLinkage
    schedule: ScheduleBlock | None
    exec_: ExecSpec | None
    sem: Semantics
    annotations: dict[str, str] = {}     # alarms, notifications — no control flow
    passthrough: dict[str, str] = {}     # unknown/unmodeled attrs, verbatim (AST-sourced)
    var_sites: list[VarSite] = []        # indexed $$VAR occurrences across string attrs

class CatalogIR(BaseModel):              # the compilation unit
    jobs: dict[str, JobIR]
    globals_declared: dict[str, str]     # insert_global
    external_instances: dict[str, XinstIR]  # xtype typed; plumbing attrs opaque (DL-28)
    machines: dict[str, MachineIR]
    calendars: dict[str, CalendarIR]     # autocal exports, opaque; standard+extended share
                                         # the run_calendar namespace (DL-36); repeatable
                                         # `condition:` lines in a .conditions lane (DL-57)
    cycles: dict[str, CycleIR]           # referenced by extended calendars' cyccal (DL-36);
                                         # start_date/end_date pairs in a .periods lane (DL-57)
    meta: CatalogMeta                    # source files, parse timestamp, tool version
```

Important lowering rules:
- `passthrough` is the **semantic firewall**: an attribute goes there only if it is on the
  allow-list of known-inert attributes OR the user passes `--permit-unknown`. An unknown
  attribute NOT on the inert list is a lowering error by default (constitutional: no silent
  loss of possibly-semantic content).
- The box tree is implicit via `box.box_name`. A validator materializes the tree and validates
  it (acyclic, members exist, ≤ depth sanity). The tree itself is Layer-G derived data.
- Every `Cond` keeps a pointer to its AST `SourceSpan` for end-to-end error reports: the
  owning attribute's span travels with the tree in `CondAttr` (DL-73), the per-node char
  offsets in each `Cond`'s own `CondSpan`.

## 5. IR-G: derived graph

```python
EdgeClass = Literal["exact","assumed","redesign"]      # E/A/R from mapping table

class DerivedEdge(BaseModel):
    src: str; dst: str                    # dst's condition references src
    via: Literal["success","failure","done","terminated","notrunning","exitcode","global"]
    atom: StatusAtom | ExitCodeAtom | GlobalAtom   # copy of the IR-F node it came from (DL-73)
    lookback: Lookback | None
    cls: EdgeClass
    mapping_row: str                      # "M01".."M36"
    assumption: str | None                # human-readable, mandatory iff cls=="assumed"
    source_atom: SourceSpan               # provenance

class DerivedGraph(BaseModel):
    nodes: list[str]
    edges: list[DerivedEdge]
    mutex_groups: list[list[str]]         # from n() detector (M07)
    or_shapes: list[OrShape]              # M12 classifier output, each with lowering choice
    box_tree: BoxTree
    external_boundary: list[JobRef]       # cross-instance refs (M33)
```

Derivation passes (pure functions IR-F → IR-G, ordered):
1. atom extraction → raw edges.
2. `n()` mutex detection (deletes those edges from the edge set, adds mutex_groups) — M07.
3. same-cycle analysis (trigger cadence inference from schedule blocks + box tree) →
   classify M01 vs M02, set cls/assumption.
4. OR-shape classification (common-ancestor diamond / independent-OR / mixed) — M12.
5. box_success/failure external-ref detection — M14/M16 (R).
6. run_window presence → M27 (R).
7. structural: parallel antichains & chains detection (feeds DSL decompiler),
   cycle detection over *derived* edges (a cycle here is legal AutoSys but a linter warning:
   possible tight loop / re-trigger pattern).

## 6. Canonical form & equivalence (validator tier a/b)

Canonicalization `C(IR-F)`:
- Expand all atom abbreviations. Normalize lookback to `minutes|zero|indefinite` (the raw
  token is deleted).
- Erase `Paren`. Flatten nested And/And, Or/Or. Sort operand lists by a stable structural key.
- Normalize schedule lists (sorted times, dedup). Delete annotations from the comparison view
  (a separate, softer tier compares them).
- Job identity: names are case-sensitive (JIL job names are case-sensitive on UNIX targets —
  [?] the Windows-instance behavior is an open question). Canonical compare takes a
  `--case-fold` override.
- Rename maps: equivalence accepts an explicit `old→new` name bijection and applies it before
  the compare.

Tier (a): `C(A) == C(B)` structural equality (Pydantic model equality on canonical form).
Tier (b): per-job condition equivalence — a truth table over the atom alphabet (atoms compare
equal after canonicalization, including lookback). This is feasible because per-condition atom
counts are small (<20 in practice). Guard the computation with an atom-count ceiling. If the
count is more than the ceiling, use BDD via `dd` or report "too large, tier-c only". Tier (b)
also includes graph bisimulation on DerivedGraph for structural refactors (subgraph wrapping
must be status-flow-preserving).
Tier (c): oracle trace comparison (below).

## 7. Oracle interface (semantics interpreter)

```python
class Event(BaseModel):                   # injectable + internally generated
    at: datetime
    kind: Literal["STATUS","STARTJOB","FORCE_STARTJOB","SET_GLOBAL","ON_ICE","OFF_ICE",
                  "ON_HOLD","OFF_HOLD","ON_NOEXEC","OFF_NOEXEC","KILLJOB","TIMER"]
    payload: dict

class StatusStore(BaseModel):             # SEM-01 latching store
    job: dict[str, JobRuntime]            # status, status_at, exit_code, run_number
    globals_: dict[str, str]

class Oracle(Protocol):
    def feed(self, ev: Event) -> list[Event]     # returns emitted events (starts, alarms)
    def trace(self) -> list[TraceEntry]          # ordered (at, job, transition, cause)
```

- Deterministic: single logical clock, tie-break by (event kind priority, insertion order).
- Box status is derived state, recomputed on member transitions (SEM-11/12/15 rules).
- Every SEM trace test (dossier §8) is `(catalog, event script, expected trace)` — pytest
  parametrized. hypothesis generates event scripts for tier (c) and the expected-divergence
  pairs (P-Mxx) against the minimal UC interpreter.
- The oracle DOES model machines/load and `resources:` as capacity buckets (DL-50). A job
  acquires an atomic demand vector (job_load vs machine max_load, QUANTITY vs insert_resource
  `amount`) before RUNNING. If the job cannot acquire the vector, it goes to QUE_WAIT (a real
  status). The oracle admits it later, in deterministic order, on the terminal release of a
  holder. These stay non-goals in v1: definition-time mutations (SEM-16, including mid-run
  resource replenishment) and agent failures.

## 8. Serialization & identity

- IR-F serializes as JSON (Pydantic `model_dump_json`, sorted keys, explicit version field
  `ir_version: "0.2"`). One catalog is one file. The output is deterministic (diff-able
  in git).
- `sys_id`-free: all identity is by name. The UC backend owns the name→sys_id/retainSysIds
  strategy (UCS-12) and keeps it out of the IR.
- Hashing: `catalog_hash = sha256(canonical IR-F JSON)`. The equivalence CLI uses it to
  short-circuit, and the migration report uses it to pin the verified content.

## 9. Linter architecture (findings, not treatments)

`Violation(code, severity, message, jobs, span, detail)` — verbatim carry-over of the proven
schedule-validator pattern (stable codes, `exit_code(strict)`, `--strict`). Rule inventory v1
follows, with each rule traceable to a SEM/M row:

| code | severity | rule | source |
|---|---|---|---|
| L001 | error | condition references undefined job | SEM-06 |
| L002 | error | unresolved global reference: `$$VAR` sites and `v(NAME)` atoms (no insert_global, no SET_GLOBAL producer in catalog; DL-25) | SEM-08 |
| L003 | error | lookback on `value()` atom | SEM-04 |
| L004 | error | start_times+start_mins / days_of_week+run_calendar | SEM-31 |
| L005 | warn | time attributes present, date_conditions falsy (dead config) | SEM-30 |
| L006 | warn | contradiction: `s(x)&f(x)` same lookback scope | tier-b engine |
| L007 | warn | tautology / condition always true at box start | tier-b |
| L008 | warn | box_success/box_failure references non-member (hung-RUNNING risk) | SEM-12/M16 |
| L009 | warn | unqualified `s()` feeding a scheduled consumer (stale-latch bug) | SEM-01/R1 |
| L010 | warn | derived-graph cycle | §5 pass 7 |
| L011 | warn | dangling job: no schedule, no consumers, no producers, not in box | hygiene |
| L012 | info | `n()` atoms → mutex candidates (suggest M07 modeling) | M07 |
| L013 | warn | box member with own schedule (double-gate; often unintended) | SEM-31 note |
| L014 | error | duplicate job name within compilation set / name collides per UC rules | UCS-12 |
| L015 | warn/info | lookback format pitfalls in raw — single-digit minutes (`2.5` = 2h05m) warn; bare-hours (`30` = 30h) info, valid + unambiguous, DL-24 — parse-time | SEM-04 |
| L016 | warn | dangling resource reference: `resources:` names a resource with no insert_resource in the set (UC backend cannot size the Virtual Resource; DL-25) | M34/UCS-09 |
| L017 | warn | dangling machine reference — only when the set defines ≥1 machine (job-only slices stay quiet; comma lists checked per name; DL-25) | hygiene |
| L018 | warn | dangling calendar reference — run_calendar/exclude_calendar, and holcal/cyccal inside extended-calendar definitions, name no definition in the set; only when the set carries ≥1 calendar/cycle (DL-36) | M24 |
| L019 | warn | date_conditions + `condition` composition: arm-and-wait start semantics (Q3, cited-resolved DL-58) have no UC-side arm concept — per-estate migration-attention item | SEM-32/M02 |

## 10. Open design decisions (deliberately deferred)

- D1: `Cond` sharing between `condition` and `box_success/box_failure` is done above. The open
  part is whether Layer-G also derives edges from box_success refs (probably yes, class per
  M15/M16).
- D2: DSL surface — postponed by plan. The decompiler emits builder calls
  (`job()`, `box()`, `sequence()`, `parallel()`) over IR-F. The context/interpolation design
  lands after the first corpus pass shows real patterns.
- D3: UC record emission templates — the base subset is SHIPPED (U3a, DL-55: CREATE-ONLY
  records per docs/uc-edge-schema.md). Rich condition forms come after U3b (live openapi.json).
- D4: whether the UC twin of the oracle shares the Event/trace types (goal: yes, one
  comparator).

## 11. What Q1/Q2/Q3 resolution changes (impact ledger)

- Q1 (precedence): RESOLVED (DL-53), as predicted — one lark rule + canonical sort
  stability. There is no model change.
- Q2 (lookback-0 anchor): Q2a RESOLVED (DL-54), nearly as predicted — oracle evaluation
  plus one runtime field (`JobRuntime.last_end_at`) and evaluator threading. The IR models
  were untouched. Q2b (first-run corner) RESOLVED (DL-58) with zero code change — the
  citation agreed with the implemented pin.
- Q3 (time-trigger with false conds): default flipped to arm-and-wait (DL-54), RESOLVED by
  citation (DL-58, abandon switch deleted). The changes are the oracle scheduling semantics
  (`JobRuntime.armed` + the releasable-gate latch) and the predicted L-rule, which landed as
  L019. There is no `ScheduleBlock` flag (nothing suggests it is configurable). The
  box-arm-scope residue is Q3c (oracle-side pin).
As intended, the IR shape was stable across all three. Resolution cost no model change.
