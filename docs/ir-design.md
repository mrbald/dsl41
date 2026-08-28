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
JIL text ──parse──▶ AST ──lower──▶ IR-F ──derive──▶ IR-G ──compile──▶ UC record bundle (JSON)
   ▲                 │               │                 │        └────▶ migration report (md)
   └───render────────┘               │                 ├────▶ Mermaid
        (fidelity round trip)        │                 └────▶ DSL source (decompiler)
                                     ├────▶ linter findings
                                     └────▶ oracle (discrete-event interpreter)
```

Every leg right of IR-F takes IR-F as its input and derives IR-G beside it, rather than
consuming IR-G alone: `compile_to_uc(catalog, graph=None)`, `to_mermaid(catalog, graph)` and
`decompile(catalog, graph)` all need the faithful layer. The linter has the same shape —
L001–L007 and L015–L019 read IR-F, L008–L014 and L020–L021 read IR-G next to it.

The four representations have four contracts:

| repr | contract | loss policy |
|---|---|---|
| **AST** | byte-faithful syntax; `render∘parse == id` on the source text (preserve mode, F1); `render∘parse∘render == render` (canonical mode is a fixpoint, F2) | zero loss, ever. Unknown attributes, comments, ordering, and whitespace style all survive |
| **IR-F** (faithful) | semantics-complete per SEM entries; `AST→IR-F` total on the supported attribute set, hard error on semantically load-bearing constructs we don't model | lowering can normalize syntax (abbreviations, formats) but never semantics |
| **IR-G** (derived) | analysis product: dependency graph + classifications; regenerable from IR-F at any time (`derive` is pure) | explicitly lossy. Every loss is materialized as an annotation |
| **UC record bundle** | CREATE-ONLY UC workflow records in the frozen base schema (`uc-edge-schema.md`), carried in one self-describing JSON bundle beside its own quarantine and exclusion ledgers; only E/A-classified edges compile | R-classified constructs become migration-report items, compile refuses to emit them silently. Twin lowering drops what UC has no edge condition for (an R row, an `n()` edge) into the bundle's exclusion ledger; an edge that SURVIVES lowering but has no base wire form withholds its whole workflow (DL-55), never part of it |

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
- The shipped models carry more than the sketch above: every layout detail preserve mode needs
  rides on the same rows (`pre_blank_lines`, `indent`, `sep`, `post`, `inline_gap`,
  `inline_key`, `inline_sep`, `eof_blank_lines`, `final_newline`, `JilFile.file`). They are
  trivia to the reader and load-bearing to F1.
- There are four fidelity tests, F1–F4, defined in `jil-statement-syntax.md`. F1 is
  preserve-mode identity on the whole test corpus. F2 is the canonical-mode fixpoint. F3 is
  fuzz over generated JIL-shaped text and raw character soups: where parse succeeds, F1 holds.
  F4 is the lexical torture matrix (escaped and quoted colons, key-shaped lookalikes inside a
  value, and the rest).
- Canonical mode (used for diffs and stored artifacts) has a stable attribute order (subcommand
  first, then a fixed key order, unknown keys alphabetically last) and a single space after the
  colon — nothing after the colon when the value is empty. Canonical mode does NOT expand
  abbreviations (that step is IR-level, and the AST canonical form is purely lexical).

## 3. IR-F: condition algebra

Source: SEM-02/03/04/07/08.

```python
Status = Literal["SUCCESS","FAILURE","DONE","TERMINATED","NOTRUNNING"]
CmpOp  = Literal["=","!=","<",">","<=",">="]

class Lookback(BaseModel):
    kind: Literal["window","zero","indefinite"]   # SEM-04
    minutes: int | None       # for kind=window; from hhhh.mm, hhhh\:mm, or bare hours
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
    name: str; op: CmpOp; value: str      # lookback FORBIDDEN here (SEM-04) — see below

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
- A lookback on a `value()` atom is impossible by construction, not by validation: the
  grammar gives `global_atom` no lookback slot, and `GlobalAtom` carries no `lookback` field.
  L003 stays in the §9 table as a reserved tripwire, and fires only if the model ever grows
  one.
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

class ExecSpecBase(BaseModel):           # shared by every executable type
    machine: str | None
    owner: str | None
    profile: str | None
    std_out_file: str | None
    std_err_file: str | None

class ExecSpec(ExecSpecBase):            # command jobs
    kind: Literal["cmd"] = "cmd"
    command: str                         # may contain $$VAR sites — kept verbatim,
    std_in_file: str | None              #   substitution sites indexed separately (below)
    envvars: str | None                  # CMD-only pair; NAME=value list, verbatim (DL-32)

class FwSpec(ExecSpecBase):              # file-watcher jobs: a SOURCE node in derived graphs
    kind: Literal["fw"] = "fw"
    watch_file: str
    watch_interval: int | None
    watch_file_min_size: int | None

ExecUnion = Annotated[ExecSpec | FwSpec, Field(discriminator="kind")]

class ResourceRef(BaseModel):            # one group of `resources:` (DL-21)
    name: str
    quantity: int                        # QUANTITY, required
    free: Literal["Y","N","A"] | None    # absent = engine default, never guessed

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
    initial_status: InitialStatus | None # SEM-24 [A]/DL-18: `status:` on insert, limited
                                         #   to INACTIVE/ON_HOLD/ON_ICE/ON_NOEXEC

class JobIR(BaseModel):
    name: str
    job_type: str                        # 'CMD','BOX','FW', + extensible
    box: BoxLinkage
    schedule: ScheduleBlock | None
    exec_: ExecUnion | None
    sem: Semantics
    annotations: dict[str, str] = {}     # alarms, notifications — no control flow
    passthrough: dict[str, str] = {}     # unmodeled attrs, AST-sourced text, whitespace-trimmed
    resources: list[ResourceRef] = []    # `resources:` groups (DL-21), typed carry
    var_sites: list[VarSite] = []        # indexed $$VAR occurrences across string attrs
    span: SourceSpan | None              # the statement's own span, for findings

class CatalogIR(BaseModel):              # the compilation unit
    ir_version: Literal["0.2"]           # §8
    jobs: dict[str, JobIR]
    globals_declared: dict[str, str]     # insert_global
    external_instances: dict[str, XinstIR]  # xtype typed; plumbing attrs opaque (DL-28)
    machines: dict[str, MachineIR]
    resources: dict[str, ResourceIR]     # insert_resource, opaque v1 (DL-18); `amount` is
                                         # the bucket size the oracle draws QUANTITY from
    calendars: dict[str, CalendarIR]     # autocal exports, opaque; standard+extended share
                                         # the run_calendar namespace (DL-36); repeatable
                                         # `condition:` lines in a .conditions lane (DL-57)
    cycles: dict[str, CycleIR]           # referenced by extended calendars' cyccal (DL-36);
                                         # start_date/end_date pairs in a .periods lane (DL-57)
    meta: CatalogMeta                    # source files, parse timestamp, tool version
```

Important lowering rules:
- `passthrough` is the **semantic firewall**. Three routes reach it, and only three: an
  attribute on the allow-list of known-inert attributes; an exec-shaped attribute that is
  inert on this job's type (a box does not execute, SEM-10); and the SEM-30 dead time cluster
  (the time attributes plus the falsy `date_conditions` switch itself), carried so L005 can
  see it. Anything else needs `--permit-unknown`. An unknown attribute NOT on the inert list
  is a lowering error by default (constitutional: no silent loss of possibly-semantic
  content). Values are the AST text with leading and trailing whitespace trimmed — syntax
  normalization, which IR-F is allowed; the byte-exact text stays in the AST.
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
    assumption: str | None                # required for "assumed", forbidden for "exact",
                                          #   permitted on "redesign" as context
    source_atom: SourceSpan | None        # provenance: the owning attribute's span

    @property
    def is_start_gate(self) -> bool:      # names a producer JOB that gates dst's START:
        ...                               #   not a box override (M15/M16 fold a box) and
                                          #   not a global (names no job). Says nothing
                                          #   about whether the catalog defines that job
                                          #   (DL-162)

def local_producer(edge: DerivedEdge,     # the catalog job `edge`'s producer names, or None.
                   catalog: CatalogIR     #   Read off the ATOM's `instance` -- NEVER by
                   ) -> str | None:       #   testing `src` against catalog.jobs: for a
    ...                                   #   cross-instance ref `src` is the composite
                                          #   `name^INST`, and a catalog job spelt that way
                                          #   collides with it (DL-162a)

class DerivedGraph(BaseModel):
    nodes: list[str]
    edges: list[DerivedEdge]
    mutex_groups: list[list[str]]         # from n() detector (M07)
    or_shapes: list[OrShape]              # M12 classifier output, each with lowering choice
    box_tree: BoxTree
    external_boundary: list[JobRef]       # every cross-instance ref: M33 from `condition`,
                                          #   M16 from a box override
    redesign_flags: list[RedesignFlag]    # pass 6: per-job R constructs that are not edges
    chains: list[list[str]]               # pass 7: maximal linear chains (feeds the DSL)
    parallel_groups: list[list[str]]      # pass 7: same-(preds,succs) sibling groups
    cycles: list[list[str]]               # pass 7: SCCs > 1 and self-loops (L010)
```

Derivation passes (pure functions IR-F → IR-G, ordered):
1. atom extraction → raw edges.
2. `n()` mutex detection (deletes those edges from the edge set, adds mutex_groups) — M07.
   Only an UNQUALIFIED, LOCAL `n()` atom under `condition` converts. An `n()` with a lookback
   (M03), one naming a cross-instance job, and one under `box_success`/`box_failure` all stay
   edges: there the atom is a completion predicate, not a start gate, and a mutex group cannot
   carry the qualifier (DL-12).
3. same-cycle analysis (trigger cadence inference from schedule blocks + box tree) →
   classify M01 vs M02, set cls/assumption. One rule runs BEFORE the atom-shape branches: an
   atom naming a local producer the catalog does not define is redesign on M02 whatever its
   shape, because latching cannot be assessed against a job that never runs (DL-12; L001
   carries the error).
4. OR-shape classification (common-ancestor diamond / independent-OR / mixed) — M12.
5. box_success/box_failure reference classification — a reference transitively inside the box
   is M15 (A); a non-member, global, or cross-instance reference is M16 (R, the hung-RUNNING
   pattern).
6. run_window presence → M27 (R).
7. structural: parallel antichains & chains detection (feeds DSL decompiler), and cycle
   detection. Both run over the LOCAL job→job edges of `condition` origin only — the M15/M16
   box-override edges describe completion folding, not flow, and would fabricate cycles out of
   ordinary box behavior (DL-12). A cycle here is legal AutoSys but a linter warning: possible
   tight loop / re-trigger pattern.

## 6. Canonical form & equivalence (validator tier a/b)

Canonicalization `C(IR-F)`:
- Expand all atom abbreviations. Normalize the lookback: the raw token is deleted, and an
  explicit indefinite (`9999`) becomes no qualifier at all, so only `window` and `zero`
  survive.
- Erase `Paren`. Flatten nested And/And, Or/Or. Sort operand lists by a stable structural key,
  drop duplicate operands, and collapse a one-operand And/Or to that operand.
- Normalize schedule lists (sorted times, dedup). The trigger lists sort on their own, with
  one exception: SEM-34 pairs each `must_start`/`must_complete` entry with a `start_time` BY
  POSITION, so those lists sort as ONE row set and a duplicate row collapses whole (DL-151). A
  single relative offset covering several start times has no pairing to lose and stays as it
  is. Empty the
  `annotations` dict in the comparison view; no tier compares annotations today. This is the
  dict only — `must_start` and `must_complete` are annotation-CLASS semantics but live on
  `ScheduleBlock`, so they stay in the compare and a difference in them is tier (a)'s to
  report.
- Job identity: names are case-sensitive (JIL job names are case-sensitive on UNIX targets —
  [?] the Windows-instance behavior is an open question). Canonical compare takes a
  `--case-fold` override.
- Rename maps: equivalence accepts an explicit `old→new` name bijection and applies it before
  the compare. It maps job names, `box_name` links, and LOCAL job refs inside all three
  condition attributes. A cross-instance ref is identity in both halves — neither the job name
  nor the instance is renamed — and so are global names, v1.

Tier (a): `C(A) == C(B)` structural equality (Pydantic model equality on canonical form).
Tier (b): per-job condition equivalence by finite-state enumeration, not by a truth table over
independent atom booleans — independent atoms cannot see that `s(x)&f(x)` is unsatisfiable,
which is L006's own flagship case. Each referenced job scope contributes its status, an
ON_ICE flag, an age bucket cut by the referenced lookback windows, a zero-freshness flag, and
a last exit code over the comparison cutpoints; each referenced global contributes its literal,
numeric and string cutpoints, and UNSET (a marker OUTSIDE the string domain, so no literal
can be read as unset — DL-151). Atoms then evaluate as functions of that state, so
status exclusion and window nesting hold by construction. Guard the computation with a
state-space ceiling of 2^18; past it, the condition reports "too large, tier-c only" —
inconclusive, never divergent. The `dd` BDD fallback is deliberately not taken v1 (DL-14): no
new dependency for a path the corpus has never needed. Tier (b) also compares the derived
graph, as exact equality of canonical edge tuples, mutex groups and box tree — the v1 stand-in
for a bisimulation check, which is NOT implemented. Tier (b) reads the jobs the two catalogs
have in COMMON, and its graph half compares edges, mutex groups and the box tree, not the node
list: which jobs exist at all is tier (a)'s question, so run tier (a) beside it.
Tier (c): oracle trace comparison (below), over the `(at, job, transition)` projection of each
trace. `cause` is excluded: it carries names and wording, not semantics.

## 7. Oracle interface (semantics interpreter)

```python
class Event(BaseModel):                   # injectable + internally generated
    at: datetime
    kind: Literal["STATUS","STARTJOB","FORCE_STARTJOB","SET_GLOBAL","ON_ICE","OFF_ICE",
                  "ON_HOLD","OFF_HOLD","ON_NOEXEC","OFF_NOEXEC","DISARM","KILLJOB","TIMER",
                  "MUST_START_ALARM","MUST_COMPLETE_ALARM"]   # DISARM: DL-158
    payload: dict
    source: str | None                    # provenance of an injected event (DL-68); None
                                          #   for oracle-internal and script events

class RuntimeState:                       # SEM-01 latching store; the ONE write path (DL-86)
    job: Mapping[str, JobRuntime]         # read-only view; rows are FROZEN — a change is a
    globals_: Mapping[str, GlobalRuntime] #   replacement, and every write names what changed
                                          # the two members the interpreter reads. The same
                                          # owner also holds the timer heap and the runner's
                                          # published state (hosts, capacity); the oracle
                                          # never reads a host row (DL-93)

class Oracle:                             # one concrete interpreter, no protocol
    def feed(self, ev: Event) -> list[Event]     # returns emitted events (starts, alarms)
    def trace(self) -> list[TraceEntry]          # ordered (at, job, transition, cause)
```

- Deterministic: single logical clock. One `feed()` first fires every timer due at or before
  its stamp, in time order, and drains each cascade; the injected event goes second. Within a
  cascade the queue is FIFO — the event, then its consequences in insertion order, jobs in
  catalog order. Feed times must be non-decreasing. A cascade is never a mixed-kind queue, so
  the (event kind priority, insertion order) tie-break has no observable cross-kind half to
  define.
- Box status is derived state, recomputed on member transitions (SEM-11/12/15 rules).
- Every SEM trace test (dossier §8) is `(catalog, event script, expected trace)`, written one
  test per behavior. Tier (c) scripts come from the caller; `equiv_scripts()` is a seeded
  deterministic generator so CLI runs reproduce. The expected-divergence pairs (P-Mxx) are
  fixed scripts run through both interpreters. hypothesis fuzzes oracle and canonical-form
  properties beside all three; it does not author the scripts.
- The oracle DOES model machines/load and `resources:` as capacity buckets (DL-50). A job
  acquires an atomic demand vector (job_load vs machine max_load, QUANTITY vs insert_resource
  `amount`) before RUNNING. If the job cannot acquire the vector, it goes to QUE_WAIT (a real
  status). The oracle admits it later, in deterministic order, on the terminal release of a
  holder. These stay non-goals in v1: definition-time mutations (SEM-16, including mid-run
  resource replenishment) and agent failures.

## 8. Serialization & identity

- IR-F serializes as JSON: `json.dumps(catalog.model_dump(mode="json"), sort_keys=True,
  indent=2)` plus a trailing newline, with an explicit version field `ir_version: "0.2"`. One
  catalog is one file. The output is deterministic (diff-able in git).
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
| L001 | error | a condition-bearing attribute (`condition`, `box_success`, `box_failure`) references an undefined local job, or a job on an instance with no insert_xinst | SEM-06/SEM-07 |
| L002 | error/warn | unresolved global reference: no insert_global, and no producer in the catalog. "Producer" is a textual heuristic over command strings (DL-11) — a command containing `SET_GLOBAL` is read as producing every `-G NAME=`-shaped assignment in it. Severity splits by read site (DL-25): a `$$VAR` substitution site is an error (a stale or empty value lands in a command line), a `v(NAME)` condition atom is a warn (a comparison waiting on an external setter can be an intended cross-system gate) | SEM-08 |
| L003 | error | lookback on `value()` atom — a reserved tripwire: the grammar and the model already make the shape unbuildable (§3), so the rule fires only if `GlobalAtom` ever grows the field | SEM-04 |
| L004 | error | start_times+start_mins / days_of_week+run_calendar | SEM-31 |
| L005 | warn | time attributes present, date_conditions falsy (dead config) | SEM-30 |
| L006 | warn | contradiction: `s(x)&f(x)` same lookback scope | tier-b engine |
| L007 | warn | tautology / condition always true at box start | tier-b |
| L008 | warn | box_success/box_failure references non-member (hung-RUNNING risk) | SEM-12/M16 |
| L009 | warn | unqualified `s()` feeding a scheduled consumer (stale-latch bug) | SEM-01/R1 |
| L010 | warn | derived-graph cycle | §5 pass 7 |
| L011 | warn | dangling job: no schedule, no derived wiring in or out (edges, globals, mutex groups), no box membership either way, and not an FW source — only reachable by a manual sendevent | hygiene |
| L012 | info | `n()` atoms → mutex candidates (suggest M07 modeling) | M07 |
| L013 | warn | box member with own schedule (double-gate; often unintended) | SEM-31 note |
| L014 | error | job names that collide case-insensitively, which UC addresses as one task. An exact duplicate never reaches the linter — lowering refuses it (§4) | UCS-12 |
| L015 | warn/info | lookback format pitfalls in raw — single-digit minutes (`2.5` = 2h05m) warn; bare-hours (`30` = 30h) info, valid + unambiguous, DL-24 — parse-time | SEM-04 |
| L016 | warn | dangling resource reference: `resources:` names a resource with no insert_resource in the set (UC backend cannot size the Virtual Resource; DL-25) | M34/UCS-09 |
| L017 | warn | dangling machine reference — only when the set defines ≥1 machine (job-only slices stay quiet; comma lists checked per name; DL-25) | hygiene |
| L018 | warn | dangling calendar reference — run_calendar/exclude_calendar, and holcal/cyccal inside extended-calendar definitions, name no definition in the set; only when the set carries ≥1 calendar/cycle (DL-36) | M24 |
| L019 | warn | date_conditions + `condition` composition: arm-and-wait start semantics (Q3, cited-resolved DL-58) have no UC-side arm concept — per-estate migration-attention item | SEM-32/M02 |
| L020 | warn | iced consumer: EVERY immediate predecessor translates to a UC Skip (ON_ICE under M19, ON_NOEXEC under M21). AutoSys runs the consumer — an iced producer satisfies its atoms — while UC cascades the skip. One live predecessor converges; box-override edges and global gates are not start gates and do not count (DL-151) | M19/UCS-02 |
| L021 | warn | condition-only multi-fire: an unscheduled, unboxed consumer with ≥2 wake sources and ≥1 unqualified latching atom can fire more than once per cycle — up to once per source when every latch is unqualified: the `s(A)&s(B)` double fire, and a bare `n()` guard doubling as a trigger. Wake sources = start-gate edges (undefined local producers dropped) and the consumer's own bare `n()` targets (`bare_notrunning`, self included). Scheduled consumers (SEM-32 arm) and box members (SEM-10 once-per-execution) are exempt; lookback-qualified atoms and `n()` atoms (local or cross-instance) wake but never latch; global gates are outside the rule both ways — flag staleness is reset discipline the catalog cannot see (DL-180) | SEM-01/DL-13 |

## 10. Design decisions D1–D4

Three of the four are closed. The numbers stay: they are cited in the sources.

- D1: `Cond` sharing between `condition` and `box_success/box_failure` is done above. CLOSED as
  its own "probably yes": Layer-G derives edges from box-override refs, classed M15 for a
  reference transitively inside the box and M16 for a non-member, global, or cross-instance one
  (§5 pass 5).
- D2: DSL surface — SHIPPED as phase 10. `dsl41 decompile` emits a runnable builder module over
  IR-F. The flow verbs are `job()`, `box()`, `sequence()`, `parallel()`, `mutex()` and
  `contend()`, beside the declaration verbs (`global_()`, `machine()`, `resource()`,
  `xinst()`, the calendar family). The fold registry is closed at T-001–T-007 (DL-38). The
  surface was extracted from corpus patterns, never designed ahead.
- D3: UC record emission templates — the base subset is SHIPPED (U3a, DL-55: CREATE-ONLY
  records per docs/uc-edge-schema.md). Rich condition forms come after U3b (live openapi.json).
  This is the one still open, and it is gated on U3b, not on a decision.
- D4: CLOSED, yes: the UC twin shares `Event` and `TraceEntry` with the AutoSys oracle, so one
  comparator reads both traces.

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
