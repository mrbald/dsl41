# Stonebranch Universal Controller Semantics Dossier + AutoSys→UC Mapping

Status: normative for the UC backend and the mapping classifier; the UC twin interpreter
implements the Part IV subset. Verified against docs.stonebranch.com / Stonebranch Confluence
(UC 7.2–7.9) where marked.
Companion to `autosys-semantics.md`. Same confidence markers: **[V]** verified · **[C]**
corroborated · **[F]** one unverified field observation (both defined in the companion; both
unused here) · **[?]** pin against a live UC instance (most [?] items resolve with
`/resources/openapi.json` and a test workflow). A composite marker such as **[V/?]** means the
entry is verified as far as it goes and the named residue is open.

---

## Part I — UC semantics (UCS entries)

## 0. Execution model (UCS-0)

UC's model is the near-inverse of AutoSys. Code and decision-log citations of `UCS-0` point
here:

- **Task** = unit of work (typed: Windows/Linux, Universal, SQL, File Monitor, Timer, Manual,
  Email, z/OS, and more). Tasks are standalone definitions. The same task can appear in
  multiple workflows.
- **Workflow** = explicit directed graph: vertices reference tasks, and edges carry dependency
  conditions. Workflows are themselves tasks (sub-workflows nest).
- **Trigger** = the *only* scheduled entry point. Triggers launch tasks/workflows and carry
  no inter-task dependency logic. Stonebranch's own docs say that dependencies belong in
  workflows and that triggers only fire. **[V]**
- **Task instance** = per-run object with its own lifecycle:
  `Defined → Waiting → (Time Wait | Resource Wait | Exclusive Wait | Instance Wait | Held)
  → Queued → Running → {Success, Failed, Cancelled, Finished, Skipped, Start Failure}`.
  UC evaluates dependencies **within the enclosing workflow instance run**: no cross-run
  latching, no lookback. This is the fundamental semantic gap between UC and AutoSys (SEM-01).

### UCS-01 · Edge conditions **[V]**
Each edge (predecessor → successor) carries a condition: **Success**, **Failure**,
Success/Failure (that is, done), and optionally a **variable condition expression** (first
value / operator / second value, resolved at evaluation time). Separate conditional paths per
outcome are the idiom for branch logic. An edge is a plain binary link; there is no boolean
algebra on edges. Multiple incoming edges express conjunction (AND). For disjunction, see
UCS-03. Do not read that as geometry: UC supports bend points, and the record field
`straightEdge` is the separate, cosmetic statement that an edge is drawn straight.

### UCS-02 · Skip propagation **[V]**
When a predecessor finishes, edges whose condition does not match put their successor paths
into **Skipped**. Rules (verified verbatim semantics):
- If ALL of a task's incoming edges resolve as skipped, the task is Skipped (skip cascades).
  An edge resolves skipped when its condition did not match, so a predecessor that FAILED
  skips its Success edge without itself being Skipped.
- Otherwise the successor runs once every incoming edge has resolved and at least one of them
  matched; **skipped predecessors do not block it**. A still-pending edge holds the join; a
  matching edge does not start the successor early.
So UC's default join is: "AND over non-skipped incoming edges, skip if everything skipped."

### UCS-03 · Join semantics — the OR problem **[V/?]**
Consequence of UCS-02: multiple incoming edges are conjunctive over the edges that *can* still
fire. But skipped branches leave the evaluation, so a diamond (A→B, A→C, B→D, C→D with a
conditional split at A) gives D "whichever branch ran", an effective OR-via-skip. True OR of
two independent (non-sibling) predecessors has no direct edge encoding. The standard
workaround patterns are: (a) restructure so that the alternatives are conditional paths from a
common ancestor, (b) use a Task Monitor task as an OR-listener, (c) duplicate the successor
for each branch.
**[?]** U1: use a live instance to learn whether 7.x has a native OR join (there are
"All/Any" complete criteria on some constructs). The compiler needs a decided, tested lowering
for `Or` nodes.

### UCS-04 · Workflow-level conditions **[V]**
Workflow completion status derives from member instances (U2, DL-53). Source: UC 7.4
"Displaying Task Instance Status": a workflow whose member is in Cancelled / Confirmation
Required / Failure / In Doubt / Running-Problems (sub-workflow) / Start Failure / Undeliverable
goes **Running/Problems** and continues to run. "Workflows will transition to Success status
when all of its task instances have transitioned to Success, Finished, or Skipped status." The
Failed status row applies to "All (except Workflow)"; a workflow instance itself is never
Failed. UC has no direct analog of the box_success/box_failure predicate override;
external-reference gating (SEM-12) does not exist at all.

### UCS-05 · Triggers **[V]**
Types include Time (calendar-based), Cron, Task Monitor (fires on another task's status, the
latching-adjacent primitive), File Monitor, Variable Monitor, Email Monitor, Composite,
Manual, Application Monitor. A trigger launches one or more tasks. All listed tasks launch on
each satisfaction. UC injects built-in trigger variables. Calendars/Business Days control the
run/exclude date logic.

### UCS-06 · Task Monitor tasks & triggers **[V]**
A Task Monitor detects when other task instances reach specified statuses, within a **Time
Scope** (a window relative to the launch time of the Task Monitor: "The Time Scope window is
always relative to the time that the Task Monitor launched"). Field spec (U5, DL-53; Task
Monitor Task page): From/To format `(+/-)hh:mm`, "hh must be a positive integer", minutes
0–59. The worked example uses ±124:00. There is NO documented maximum; the window is
retention-bound in practice, not spec-capped. This is the nearest UC equivalent of the AutoSys
status-store queries and the main tool for cross-workflow dependencies. A Task Monitor
*trigger* + task pair replaces "condition on a job in another stream."

### UCS-07 · File monitors **[V]**
The Agent File Monitor task watches create/change/delete (blocking, goes Success on the
event) or exists/missing (immediate Success/Finished; Finished = condition not met, and
trigger-launched tasks then do not fire). The trigger + monitor-task composition rules have
traps and are verified: monitor-type Exists under a File Monitor trigger disables the
trigger; use Create + "Trigger on Existence". This maps from AutoSys FW jobs.

### UCS-08 · Variables **[V]**
UC has global variables plus workflow/task-level variables, `${variable}` resolution, and Set
Variable actions on task events. Variable Monitor triggers fire on value changes. An ordering
subtlety (verified): whether edge variable-condition evaluation occurs before or after Set
Variable Actions on Success/Failed depends on the system property `Perform Set Variable
Actions Before Workflow Dependency Evaluation` (U8, DL-53; System Properties:
`uc.perform_actions.before_wf_dependency_evaluation`, "whether or not the Set Variable Action
of a task in a workflow will occur before downstream path conditions of that task are
evaluated", **default `true`**). Variables set by actions are visible to downstream dependency
evaluation on a default-configuration controller. This is per-controller configurable, so the
compiler emits a configuration requirement note that pins `true` as the assumed value.

### UCS-09 · Mutual exclusion & resources **[V]**
- **Mutually Exclusive Tasks**: a declared per-task list. Instances wait in Exclusive Wait.
- **Virtual Resources**: counted semaphores. A task declares the required units and waits in
  Resource Wait otherwise. These are direct targets for AutoSys `n()`-style exclusion (R6)
  and job_load/max_load.
- **Instance Wait** ("wait for previous instance(s) of same task/workflow") serializes
  successive runs. **[V]**

### UCS-10 · SLA / lateness **[V]**
UC has Late Start / Late Finish flags (absolute time or duration relative to the programmed
start, with day-advance rules). These *flag* only; they do not kill. This matches
must_start/must_complete (SEM-34) almost 1:1. Separately, workflow Critical Path calculation
exists as a system property.

### UCS-11 · Run-time overrides **[V]**
Per-instance commands: Clear Predecessor Dependencies (≙ satisfies them; it does NOT clear
resource/exclusive dependencies, which have separate commands), Satisfy/Evaluate single edge,
Force Finish (marks Finished, releases successors; the underlying process continues to run),
Force Finish/Cancel, Skip, Hold/Release, Re-run (with a Suppress Intermediate Failures variant
that deliberately does NOT release failure-path successors **[V]**). Insert-task-into-running-
workflow exists through the API. Operational mapping for runbooks: sendevent ↔ these commands.

### UCS-12 · Definition format & API **[V]**
- UC has a full RESTful API. The controller serves the OpenAPI spec at
  `<controller>/resources/openapi.json|yaml` → **generate the backend client from OpenAPI, do
  not write it by hand** (DL-08).
- Task/workflow/trigger definitions round-trip as XML or JSON records with system attributes
  (`exportTable`, `exportReleaseLevel`, `retainSysIds`, `version`, `sys_id`s). Workflows carry
  vertex and edge lists. API operations address vertices by task name (ambiguous-name and
  missing-name operations fail loudly with defined errors **[V]**).
- Bulk import/export exists for whole-controller definition sets.
- UC name constraints (charset, length, case sensitivity) are NOT documented: searched, not
  found (DL-55 item 4). Task names and box-derived workflow names pass through verbatim, and an
  invalid one fails at create time; only a loose-component workflow gets a synthesized name,
  `wf_<first task>`. The linter takes the conservative reading of the undocumented part: two
  JIL names that differ only in case may address one UC task, so L014 refuses them. Extend
  that rule only if the U3b live pull reveals a real constraint.
**IR consequence:** the UC backend compiles Layer-G graphs to these record sets. Names are the
primary keys (DL-55): vertices reference tasks by name verbatim, and task names are
catalog-unique, so each appears once per workflow. Records pin `retainSysIds: false` and omit
all system attributes; the CREATE-ONLY rules are frozen in `docs/uc-edge-schema.md`.

### UCS-13 · No status latching, no lookback — restated as target constraint **[V]**
All predecessor evaluation is within the workflow instance. Anything in the JIL corpus that
relies on cross-run latching (SEM-01/04) must compile to Task Monitors (with Time Scope) or be
re-expressed, never silently to plain edges.

---

## Part II — AutoSys → UC mapping table

Legend: **E** exact · **A** equivalent under stated assumption · **R** redesign required (no
faithful translation: the compiler refuses the construct and emits a redesign item in the
migration report instead; `cls="redesign"` in the code, "Refused constructs" in the report).

A slashed class (**A/R**, **E/A**) means the row splits by case. Where the Notes column names
a discriminator the classifier applies it, and every derived edge then carries exactly ONE
class. Rows without an implemented discriminator keep the slash as a statement of what the
migration can cost. M12, for one, produces an OR-shape record with a suggested lowering
instead of an edge class (U1-gated), and M17 derives no edge at all: the report names the
construct and leaves the class to a human (DL-151).

Every A/R row that becomes a derived edge becomes a migration-report entry. The non-edge
entries the report carries are M07, M12, M17, M24, M27 and M33; a row outside both sets has no
report path. Only seven rows also have a dedicated linter rule: L008 (M16), L009 (M01/M02),
L012 (M07), L016 (M34), L018 (M24), L019 (M02), L020 (M19). The rule inventory is
`ir-design.md` §9.

| # | AutoSys construct (SEM) | UC target | Class | Notes / assumption |
|---|---|---|---|---|
| M01 | `s(A)` within one stream, producer+consumer same schedule cycle (SEM-01) | edge A→X (Success) | **A** | Assumption: no reliance on cross-run staleness. Detector (DL-12): the same top-level box, or — when BOTH jobs are unboxed — one derived trigger cadence. Two identically scheduled boxes are two UC workflows, so a cadence collision alone is not one stream |
| M02 | `s(A)` cross-stream / relying on latching (SEM-01) | Task Monitor task/trigger with Time Scope | **A/R** | A when the producer exists: Time Scope has no documented maximum (U5, DL-53), but it is launch-relative and retention-bound — the anchoring still differs from an indefinite latch. Flag each. R when the producer is not defined in the compilation set (DL-12): the atom is permanently false (SEM-06), latching cannot be assessed, and L001 carries the loud error |
| M03 | any lookback-qualified local atom in `condition` — `s(A, hhhh.mm)` and the same shape on `f`/`d`/`t`/`n`/`e` (SEM-04) | Task Monitor with Time Scope ≈ window | **A** | Within `condition`, and for a producer the set defines, the lookback decides the row before the atom kind does. An undefined producer is M02 first; a lookback under a box override is M15 or M16 first. Window anchoring differs per case. Zero-lookback (Q2a, DL-54) anchors to the consumer's own last end — relational, NO fixed Time Scope expresses it. Flag every use |
| M04 | `f(A)` (SEM-01/02) | edge A→X (Failure); cross-stream: Task Monitor watching Failed, with Time Scope | **A** | The M01/M02 same-cycle detector (DL-12) picks the assumption, never the class — a stale FAILURE from a previous cycle satisfies f() in AutoSys (SEM-01) and no UC edge does, so f() is exactly as latched as s(). Inside one top-level box the box start resets every member to INACTIVE (SEM-10, DL-242), so there a stale FAILURE no longer survives into the next box run; the row's class is unchanged pending a separate UC decision. Same stream: the edge compiles with M01's staleness assumption. Cross-stream with a defined producer: Task Monitor; Time Scope anchoring differs from an indefinite latch (UCS-06); flag each. An undefined producer is M02-R first (DL-153) |
| M05 | `d(A)` (SEM-01/02) | edge A→X (Success/Failure); cross-stream: Task Monitor watching any completion, with Time Scope | **A** | Same split as M04: the detector picks between M01's staleness assumption (same stream) and the Task-Monitor one (cross-stream, defined producer) — the terminal-status latch outlives the run (SEM-01) either way. An undefined producer is M02-R first (DL-153) |
| M06 | `t(A)` | Failure-ish: UC Cancelled/Failed distinction | **A** | UC separates Cancelled from Failed. Decided (DL-16/DL-55): `t()` gets its own `cancelled` edge condition in the twin, and a `failure` edge does NOT fire on Cancelled. The base record schema has no Cancelled wire token, so such an edge quarantines its whole workflow at emission (`docs/uc-edge-schema.md`) |
| M07 | `n(A)` mutual exclusion (SEM-02, R6) | Mutually Exclusive Tasks or Virtual Resource; `n(self)` → Instance Wait | **A** | NOT an edge. Detector (DL-12): a LOCAL UNQUALIFIED `n()` in `condition` becomes a mutex candidate PAIR. A lookback-qualified `n()` stays an edge (M03), and so does an `n()` under a box override or naming a cross-instance job — it is a completion predicate there, not a start gate. `n(self)` is a one-element group. Under SEM-32 arm-and-wait a *scheduled* n() job queues until the peer completes, which converges with UC's ExclusiveWait (P-M07 pins alignment at milestone level; DL-54, confirmed DL-58). Residual ordering divergence: in AutoSys, several armed jobs that wait on one peer wake in catalog order, but UC releases ExclusiveWait FIFO by arrival — flag when >1 waiter shares a peer |
| M08 | `exitcode(A) op k` (SEM-02) | edge variable condition on exit-code variable, or task-level exit-code→status mapping | **A** | mechanism (U4, DL-53): per-task "Exit Code Processing" field, default method Success Exitcode Range. The exit-code range value itself is required, with no documented default — record the configured range per task in the report |
| M09 | `value(G) op k` (SEM-08) | edge variable condition / Variable Monitor trigger | **A** | Re-eval-on-set: AutoSys re-evaluates on the SET_GLOBAL event, UC edge conditions evaluate at predecessor completion — the timing differs. A global in `condition` is always A at the edge; a global in `box_success`/`box_failure` is M16/R instead. A global has no producer vertex, so the twin attaches ONE usable variable gate to the consumer's incoming edges and records the rest — a consumer with no compiled predecessor edge, a gate that reaches only some paths, a second global on the same consumer — as an exclusion that names the redesign. A JIL that used globals as async gates lands there |
| M10 | `$$VAR` substitution (SEM-08) | `${var}` resolution | **A** | Resolution timing + UCS-08 ordering property |
| M11 | AND `&` | multiple incoming edges | **E** | with UCS-02 skip caveat |
| M12 | OR `\|` | conditional-path restructure / Task Monitor / duplication (UCS-03) | **A/R** | per-case lowering decision; the compiler's hardest open lowering |
| M13 | box, members with no conditions (SEM-10) | workflow, parallel start vertices | **E** | |
| M14 | box with member conditions (SEM-10) | workflow with edges | **A** | assumes member conditions reference siblings. A member's unqualified `s()` naming a job outside its top-level box → M02. Other atom kinds keep their own row (M03–M08); the twin's cross-workflow gate is what catches them, and it excludes them by record |
| M15 | box_success/box_failure internal ref (SEM-12) | restructure: terminal vertex placement / workflow status by path design | **A** | early-exit semantics needs explicit Skip paths. Membership is transitive (SEM-12 "inside"), so any descendant lands here; a non-member, global or cross-instance reference is M16 instead |
| M16 | box_success external ref, hung-RUNNING gate (SEM-12) | — | **R** | no analog; redesign. Estates rely on this pattern as a feature |
| M17 | box_terminator/job_terminator (SEM-14) | task-level failure handling + workflow Cancel actions | **A/R** | UC has no auto "kill siblings on my failure" edge. Emulate with actions/monitors — per-case. No derived edge carries it: the migration report lists each declaring job under its own section, with the class left open because this row states no discriminator |
| M18 | nested boxes (SEM-17) | sub-workflows | **E** | UC sub-workflows are the exact analog. The backend does not use them: nested boxes flatten into the top-level workflow record and the nested box names get no record of their own, travelling as an apply note instead (DL-16) |
| M19 | ON_ICE (SEM-20) | Skip task (definition-level Skip flag / instance Skip) | **A** | Verified: a skipped predecessor does not block successors (UCS-02) — downstream-satisfied matches for an ordinary success/done/notrunning atom, or any lookback-qualified atom (DL-243). BUT the all-skipped cascade differs from AutoSys in two directions: (1) when every immediate predecessor is iced and each is read as satisfied (the case above), the consumer runs in AutoSys while UC's all-predecessors-skipped cascade skips it; (2) an iced predecessor referenced ONLY by an ordinary failure/terminated/exitcode atom reads FALSE there (DL-243), so AutoSys can never run that consumer through it, while UC's skip cascade — blind to which AutoSys atom kind an edge stands for — may still resolve the dependency and run it; this is the opposite-direction divergence. Linter: L020 flags both directions: a consumer whose immediate predecessors ALL translate to Skip and all read as satisfied (ON_ICE here, or ON_NOEXEC under M21; one live predecessor converges), and separately a consumer with an iced predecessor gated only by an ordinary failure/terminated/exitcode atom. The lookback-qualified reading above is ON_ICE only: against an ON_NOEXEC predecessor (M21) L020 reads every failure/terminated/exitcode atom false, lookback or not (SEM-22, DL-281). A box-override reference is not a start gate |
| M20 | ON_HOLD (SEM-21) | Hold task/instance | **E** | downstream blocked in both. The twin does not model definition-time state: each status lands in the `dsl41 uc` exclusion ledger under its OWN row — ON_HOLD here, ON_ICE under M19, ON_NOEXEC under M21 — with the UC control it maps to at cutover |
| M21 | ON_NOEXEC (SEM-22) | Skip (path-level) | **A** | close, but the M19 skip-cascade caveat applies |
| M22 | FORCE_STARTJOB (SEM-23) | Launch task / Clear Dependencies | **A** | forced runs do not satisfy latches in UC (no latches) — ops retraining, R8 |
| M23 | CHANGE_STATUS | Force Finish / Set status via API | **A** | Force Finish does not stop the underlying process **[V]** — runbook warning |
| M24 | date_conditions + start_times/days/calendars (SEM-30–32) | Time trigger + UC Calendars | **E/A** | calendar algebra (exclude_calendar) → UC calendar with non-business days. Make sure that custom calendar parity holds |
| M25 | start_mins (SEM-32) | Cron trigger (`m * * * *`) or Time trigger interval | **E** | |
| M26 | timezone (SEM-35) | trigger-level time zone | **E** | per-trigger Time Zone field documented (U6a, DL-53; the quote's provenance is hedged in Part III). Calendar parity is open (U6b) |
| M27 | run_window (SEM-33) | no direct analog; Time Wait on task + trigger restrictions | **R** | the closer-edge rule (R5) is unreproducible. Redesign and document each job |
| M28 | must_start/must_complete (SEM-34) | Late Start / Late Finish flags (UCS-10) | **E** | a direct mapping |
| M29 | term_run_time (§5) | Late Finish (Time/Duration) + Abort Action (Force Finish / Force Finish-Cancel) | **A** | mechanism (U7, DL-53): Late Finish flags the overrun, and an Abort Action on late finish kills. "maximum-runtime equivalent" is our interpretive composite, not a vendor-labeled single control |
| M30 | n_retrys (§5) | task Retry options (max retries, retry indefinitely, retry interval, retry exit codes) | **A** | both sides auto-retry on failure only (Q4 and U7, DL-53): n_retrys fires on application-failure exits, UC retry fires on Failed status. AutoSys system-level failures restart through MaxRestartTrys (scheduler configuration), with no per-task UC analog — flag estates that rely on it. "Suppress Intermediate Failures" is a Re-run command modifier (UCS-11), not a Retry option |
| M31 | max_exit_success + success_codes/fail_codes (SEM-09/DL-33) | task exit-code / output success criteria | **A** | mechanism per M08 (U4, DL-53). The twin shares ir.exit_is_success on both sides; the composition follows Q7, closed by citation (DL-58, KB 408778: a present fail_codes decides alone) |
| M32 | FW jobs (watch_file) (§5) | Agent File Monitor task/trigger (UCS-07) | **A** | steady-state versus existence modes, trigger-disable traps **[V]** |
| M33 | cross-instance `job^INST` (SEM-07) | Task Monitor across... or UC agent/remote — depends on target topology | **R** | consolidation of instances is a migration design decision, not a translation |
| M34 | job_load/priority/QUE_WAIT, and `resources:` requirements (DL-21) | Virtual Resources + Agent task limits | **A** | model mapping per machine definition. A job's declared resource units and their FREE disposition are not expressible in a workflow record: they go into the `dsl41 uc` bundle's exclusion ledger and are configured per task at cutover. L016 warns when a `resources:` name has no `insert_resource` in the set; `dsl41 lint --strict` turns that warning into a failure |
| M35 | machine (real/virtual) | Agent / Agent Cluster | **A** | broadcast versus any-of semantics — make sure that you know the cluster distribution rules |
| M36 | alarms (alarm_if_fail, max_run_alarm…) | Email/SNMP notifications, System Operations actions | **A** | observability rework, mechanical |

### Mapping-driven compiler requirements

1. **Every Layer-G edge carries its M-row.** The UC backend refuses to compile R rows (it
   emits migration report items instead). A rows compile and emit assumption records. Only E
   rows compile silently. A failed translation is a loud, classified error, never a silent
   loss (DL-04); the per-row classes make it granular. Two narrower gates sit after it, and
   each records what it drops. (i) The
   twin also excludes edge shapes it cannot hold: a `notrunning` edge (no UC condition reads
   "not running"), a global gate it cannot attach, an M15 member-to-box override (the box IS
   the workflow, never a task vertex, so the early exit needs explicit Skip-path
   restructuring), and every edge that spans two workflows, including M04/M05 ones, which is
   Task Monitor work (a cross-workflow f()/d() edge is cross-stream by construction,
   DL-153). (ii) Record emission quarantines a WHOLE workflow for either of two causes: the
   base record schema cannot spell one of its edges (a `cancelled` condition, any variable
   condition), or two workflows serialize to one record name, compared the way UC addresses
   names, case folded (U3a; `docs/uc-edge-schema.md`).
2. **The migration report is a first-class output artifact** (per-catalog markdown): every
   A-classified edge with its assumption, every R-classified edge, the non-edge constructs
   this table routes to a human (M07 mutex groups, M12 OR shapes, M27 run_window flags, M33
   external refs, M24 calendars, M17 terminators), the quarantine ledger, both bundle ledgers,
   and the open questions the catalog's rows depend on. The record bundle carries its OWN
   ledgers next to the records: what the twin excluded, and what a workflow record cannot
   hold (M19/M20/M21 definition-time status, M34 resources, M31 exit-code boundaries with
   their configured values, an M03 lookback window per edge), so records can never be
   applied without them.
3. **Detectors needed in analysis passes:** same-cycle detector (M01 versus M02), `n()`-mutex
   detector (M07), OR-shape classifier (M12), box-reference detector (a member's unqualified
   `s()` naming a job outside its top-level box → M02; a box override naming a transitive
   member → M15, naming anything else → M16), iced-consumer detector (M19, L020).

## Part III — Open questions (live UC instance / OpenAPI dive)

This part is the register of the U series. A closed question names where its rule lives. U2,
U4, U5, U6a, U7 and U8 were closed from vendor documentation (DL-53). An open question carries
a `# PENDING: Un` marker where the code holds a provisional default.

- U1: OPEN. Native OR-join / "Any" completion criteria in 7.x workflows (UCS-03). It decides
  the M12 lowering (`# PENDING: U1`, derive.py).
- U2: closed (DL-53). Workflow-status derivation is in UCS-04: member failure →
  Running/Problems, and the workflow continues to run; Success if and only if all members are
  Success/Finished/Skipped; a workflow instance is never Failed ("Displaying Task Instance
  Status", UC 7.4).
- U3: split (DL-55). **U3a, the base record schema: closed.** The CREATE-ONLY whole-record
  shape (workflowVertices with explicit string vertexIds and value-wrapper task refs ·
  workflowEdges with condition/sourceId/targetId value wrappers · condition tokens verbatim
  `Success` / `Failure` / `Success/Failure` · retainSysIds a record attribute, pinned false)
  is doc-frozen in `docs/uc-edge-schema.md` from the docs site (UC 8.0), cross-verified
  against two OSS clients. `dsl41 uc` emits exactly this subset and quarantines WHOLE any
  workflow with an inexpressible edge (a `Cancelled` edge condition does not exist, so M06
  t() edges quarantine). **U3b: OPEN.** Rich condition forms (Exit Code / Step Condition /
  Variable + variableCondition, vertex conditionExpression), the live openapi.json pull, and
  write-path verification (one live POST + GET readback). The API client stays
  generated-from-OpenAPI (DL-08). `# PENDING: U3b`, backend_uc.py.
- U4: closed (DL-53; M08, M31). The mechanism is the per-task "Exit Code Processing" field
  (Success/Failure Exitcode Range, Success/Failure Output Contains), default Success Exitcode
  Range ("Linux Unix Task Properties"). The exit-code range value is a required field with NO
  documented default. Record the per-task configured ranges at migration time.
- U5: closed (DL-53). No documented maximum on Task Monitor Time Scope. The window is
  launch-relative, hours are uncapped ("hh must be a positive integer", ±124:00 example), and
  it is retention-bound in practice (UCS-06).
- U6: split (DL-53). U6a, trigger timezone: closed (M26). A per-trigger Time Zone / Trigger Time
  Zone field is documented ("Triggering by Date and Time"; the quote sits in the Trigger-Now
  execution section, and the equivalent field-table text is unretrieved, a cosmetic residue).
  U6b, calendar parity with AutoSys extended calendars (M24), including any multi-calendar
  AND/OR algebra for run/exclude combinations: OPEN. No documented algebra found. U6b lives
  in the migration report's question table; it has no code switch.
- U7: closed (DL-53). The M29 overrun mechanism is Late Finish + Abort Action (a composite;
  see the M29 caveat). Auto-retry applies to Failed status only ("auto-retry of tasks in
  FAILED status", Retry Exit Codes field). "Suppress Intermediate Failures" is a Re-run
  command modifier that suppresses failure propagation (actions, failure-path successors,
  Task Monitor notification, Running/Problems rollup), NOT an auto-retry control.
- U8: closed (DL-53). `uc.perform_actions.before_wf_dependency_evaluation` defaults to
  `true` (System Properties; see UCS-08): Set Variable actions run before downstream
  path-condition evaluation on a default-configuration controller. This is per-controller
  configurable; the migration report records `true` as the assumed value.

## Part IV — Trace tests (oracle-pair set)

A minimal UC workflow interpreter, `tests/uc_oracle.py`, runs the in-memory twin: what the
backend compiled, before the U3a emission gate, so a script may exercise a workflow that
emission would later quarantine. It implements UCS-01, UCS-02, UCS-03 and UCS-13, UCS-09's
mutual exclusion, and UCS-0 workflow addressing (a launch on any task name opens the
containing workflow). Script analogs of the UCS-11 run-time overrides (hold/release, skip,
kill, forced start) exist so a pair can be driven; nothing else in Part I is modeled.

Its own comparator normalizes UC statuses into the AutoSys vocabulary and compares per-job
outcome sequences against the AutoSys oracle on one shared script. `SKIPPED` is dropped from
BOTH sides before the equality test: an explicit UC Skip and an AutoSys job that was never
evaluated are one observable outcome, "did not run", while a SKIPPED-versus-ran mismatch
still diverges. The reported divergence keeps the normalized sequences with `SKIPPED` still in
them, so a reader sees which side skipped. This is a SEPARATE comparison from the equivalence
validator's tier (c), which compares two AutoSys catalogs; the two share the event and trace
models, not the entry point.

The seed set is hand-written and non-exhaustive. It covers seven rows, M01, M04, M07, M09,
M12, M19, M27, with one or more scripts each:
- P-M01: a same-cycle pair that nonetheless relies on cross-run staleness → traces MUST
  diverge; the test pins the divergent job and both outcome sequences.
- P-M04 (DL-153, DL-242): a same-box f() whose producer does not re-run in cycle two. DL-153
  pinned a divergence: AutoSys re-started the consumer on the stale FAILURE. Since DL-242 a box
  start resets every member to INACTIVE (SEM-10), so the consumer waits on both sides and the
  pair converges on it. The producer's own divergence remains: the twin restarts it as a
  source task while AutoSys holds it blocked.
- P-M07: n() overlap, an ALIGNMENT pin under the arm-and-wait default (DL-54, confirmed DL-58).
- P-M09: SET_GLOBAL mid-run.
- P-M12: the naive lowering only; one independent-branch script that diverges into an AND
  join, one common-ancestor diamond that converges. The restructure, Task-Monitor and
  duplicate-successor lowerings stay U1-gated.
- P-M19: ice; one all-iced script that diverges, one mixed-predecessor contrast that
  converges.
- P-M27: run_window closer-edge divergence. M27 is an R row, present here because absence is
  what the pair shows.

Where a pair "converges" it is the compared consumer path that converges. The untaken OR
branch and the iced job itself are held out of the equality list and asserted separately, so
the claim stays about the path under test: their AutoSys side was never touched and their UC
side is Skipped. Including them would not change the verdict, since the comparator drops
`SKIPPED`, but the asserted contrast is what makes the label difference visible. These
expected-divergence and alignment tests document what the migration changes.

Two interpreter approximations are deliberate and known-divergent from Part I. Read every pair
result against them:
- Workflow rollup: the interpreter marks an instance Failed when a member ended Failed or
  Cancelled. Real UC goes Running/Problems and is never Failed (UCS-04, U2).
- Instance Wait: the interpreter allows one open instance per workflow and records-and-ignores
  a later launch, instead of queuing it as a waiting instance (UCS-09).

## Sources
Primary: docs.stonebranch.com and Stonebranch Confluence, Universal Controller 7.2–7.9:
Creating and Maintaining Workflows (edge conditions, skip rules, step/variable conditions),
Manually Running and Controlling Tasks (dependency clearing, re-run including Suppress
Intermediate Failures, force finish), Setting Mutually Exclusive Tasks, Creating Task
Virtual Resources, Triggers Overview, Agent File Monitor Task, Workflows PDF (7.4/7.9:
instance wait, late start, wait/delay), Task Web Services & RESTful Web Services API
(XML/JSON records, OpenAPI endpoint), Workflow Task Instance Web Services (vertex addressing
errors), Displaying Task Instance Status (UC 7.4), Linux Unix Task Properties / Windows Task
(Exit Code Processing, Retry fields), Task Monitor Task (Time Scope spec), Triggering by Date
and Time (trigger time zones), Abort Actions (UC 7.8), System Properties
(`uc.perform_actions.before_wf_dependency_evaluation`). The U3a record shape is verified
against the docs site at UC 8.0 and two OSS clients; see `docs/uc-edge-schema.md`.
