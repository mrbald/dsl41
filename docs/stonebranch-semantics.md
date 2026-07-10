# Stonebranch Universal Controller Semantics Dossier + AutoSys→UC Mapping

Status: draft v0.1 · Session B deliverable · verified against docs.stonebranch.com /
Stonebranch Confluence (UC 7.2–7.9) where noted
Companion to `autosys-semantics.md`. Same confidence markers: **[V]** verified · **[C]**
corroborated · **[?]** pin against the live UC instance (we will have API access; most [?]
items here are cheaply resolvable via `/resources/openapi.json` and a test workflow).

---

## Part I — UC semantics (UCS entries)

## 0. Execution model

UC's model is the near-inverse of AutoSys:

- **Task** = unit of work (typed: Windows/Linux, Universal, SQL, File Monitor, Timer, Manual,
  Email, z/OS, …). Tasks are standalone definitions; the same task can appear in multiple
  workflows.
- **Workflow** = explicit directed graph: vertices reference tasks; edges carry dependency
  conditions. Workflows are themselves tasks (sub-workflows nest).
- **Trigger** = the *only* scheduled entry point. Triggers launch tasks/workflows; they carry
  no inter-task dependency logic — Stonebranch's own docs say: dependencies belong in
  workflows, triggers just fire. **[V]**
- **Task instance** = per-run object with its own lifecycle:
  `Defined → Waiting → (Time Wait | Resource Wait | Exclusive Wait | Instance Wait | Held)
  → Queued → Running → {Success, Failed, Cancelled, Finished, Skipped, Start Failure}`.
  Dependencies are evaluated **within the enclosing workflow instance run** — no cross-run
  latching, no lookback. This is the fundamental semantic gap vs. AutoSys (SEM-01).

### UCS-01 · Edge conditions **[V]**
Each edge (predecessor → successor) carries a condition: **Success**, **Failure**,
Success/Failure (i.e., done), and optionally a **variable condition expression** (first value /
operator / second value, resolved at evaluation time). Separate conditional paths per outcome
are the idiom for branch logic. Straight edges only — no boolean algebra on edges; conjunction
is expressed by multiple incoming edges (AND), disjunction by... (see UCS-03).

### UCS-02 · Skip propagation **[V]**
When a predecessor finishes, edges whose condition doesn't match put their successor paths
into **Skipped**. Rules (verified verbatim semantics):
- If ALL immediate predecessors of a task are Skipped → the task is Skipped (skip cascades).
- If at least one immediate predecessor ran to completion and its edge condition matches →
  the successor runs; **skipped predecessors do not block it**.
So UC's default join is: "AND over non-skipped incoming edges, skip if everything skipped."

### UCS-03 · Join semantics — the OR problem **[V/?]**
Consequence of UCS-02: multiple incoming edges are conjunctive over the edges that *can* still
fire, but because skipped branches drop out, a diamond (A→B, A→C, B→D, C→D with conditional
split at A) gives D "whichever branch ran" — effectively OR-via-skip. True OR of two
independent (non-sibling) predecessors has no direct edge encoding; the standard workaround
patterns: (a) restructure so alternatives are conditional paths from a common ancestor,
(b) Task Monitor task as an OR-listener, (c) duplicate the successor per branch.
**[?]** Verify on live instance whether 7.x has native OR join (there are "All/Any" complete
criteria on some constructs); the compiler needs a decided, tested lowering for `Or` nodes.

### UCS-04 · Workflow-level conditions **[C/?]**
Workflow completion status derives from member instances (Success when all terminal paths
complete successfully; Running/Problems status exists for contained failures). No direct
analog of box_success/box_failure predicate override — external-reference gating (SEM-12)
does not exist at all. Pin exact workflow-status derivation rules on live instance.

### UCS-05 · Triggers **[V]**
Types include Time (calendar-based), Cron, Task Monitor (fires on another task's status —
the latching-adjacent primitive!), File Monitor, Variable Monitor, Email Monitor, Composite,
Manual, Application Monitor. A trigger launches one or more tasks; all listed tasks launch on
each satisfaction. Built-in trigger variables are injected. Calendars/Business Days handle
run/exclude date logic.

### UCS-06 · Task Monitor tasks & triggers **[V/C]**
A Task Monitor watches for other task instances reaching specified statuses, with a **Time
Scope** (can look into the past within bounds). This is the closest UC gets to AutoSys's
status-store queries and the main tool for cross-workflow dependencies. A Task Monitor
*trigger* + task pair replaces "condition on a job in another stream."

### UCS-07 · File monitors **[V]**
Agent File Monitor task: watches create/change/delete (blocking, goes Success on event) or
exists/missing (immediate Success/Finished — where Finished = condition not met, and
trigger-launched tasks then don't fire). Trigger + monitor-task composition rules are fiddly
and verified: monitor-type Exists under a File Monitor trigger disables the trigger — use
Create + "Trigger on Existence". Maps from AutoSys FW jobs.

### UCS-08 · Variables **[V/C]**
Global variables + workflow/task-level variables; `${variable}` resolution; Set Variable
actions on task events; Variable Monitor triggers fire on value changes. Ordering subtlety
(verified): edge variable-condition evaluation can occur before or after Set Variable Actions
on Success/Failed depending on the system property `Perform Set Variable Actions Before
Workflow Dependency Evaluation` — the compiler must not assume either default; emit
configuration requirement notes.

### UCS-09 · Mutual exclusion & resources **[V]**
- **Mutually Exclusive Tasks**: declared per-task list; instances wait in Exclusive Wait.
- **Virtual Resources**: counted semaphores; task declares required units; Resource Wait
  otherwise. Direct targets for AutoSys `n()`-style exclusion (R6) and job_load/max_load.
- **Instance Wait** ("wait for previous instance(s) of same task/workflow"): serialization of
  successive runs. **[V]**

### UCS-10 · SLA / lateness **[V]**
Late Start / Late Finish flags (absolute time or duration relative to programmed start, with
day-advance rules); these *flag*, they don't kill — matches must_start/must_complete (SEM-34)
almost 1:1. Separate: workflow Critical Path calculation exists as a system property.

### UCS-11 · Run-time overrides **[V]**
Per-instance commands: Clear Predecessor Dependencies (≙ satisfying them; does NOT clear
resource/exclusive deps — those have separate commands), Satisfy/Evaluate single edge,
Force Finish (marks Finished, releases successors; underlying process keeps running!),
Force Finish/Cancel, Skip, Hold/Release, Re-run (with Suppress Intermediate Failures variant
that deliberately does NOT release failure-path successors **[V]**). Insert-task-into-running-
workflow exists via API. Operational mapping table for runbooks: sendevent ↔ these commands.

### UCS-12 · Definition format & API **[V]**
- Full RESTful API; OpenAPI spec served at `<controller>/resources/openapi.json|yaml` →
  **generate the backend client from OpenAPI; do not hand-roll**.
- Task/workflow/trigger definitions round-trip as XML or JSON records with system attributes
  (`exportTable`, `exportReleaseLevel`, `retainSysIds`, `version`, `sys_id`s). Workflows carry
  vertex and edge lists; API operations address vertices by task name (ambiguous-name and
  missing-name operations fail loudly with defined errors **[V]**).
- Bulk import/export exists for whole-controller definition sets.
**IR consequence:** the UC backend compiles Layer-G graphs to these record sets; `retainSysIds`
/ name-addressing strategy must be decided (names as primary keys → enforce unique task names
per workflow in the linter).

### UCS-13 · No status latching, no lookback — restated as target constraint **[V]**
All predecessor evaluation is within the workflow instance. Anything in the JIL corpus that
relies on cross-run latching (SEM-01/04) must compile to Task Monitors (with Time Scope) or be
re-expressed — never silently to plain edges.

---

## Part II — AutoSys → UC mapping table

Legend: **E** exact · **A** equivalent under stated assumption · **R** redesign required
(no faithful translation; compiler must emit a `needs-human` item) · each A/R row is a linter
rule and a migration-report entry.

| # | AutoSys construct (SEM) | UC target | Class | Notes / assumption |
|---|---|---|---|---|
| M01 | `s(A)` within one stream, producer+consumer same schedule cycle (SEM-01) | edge A→X (Success) | **A** | Assumption: no cross-run staleness relied upon. Detector: producer and consumer share one derived subgraph + one trigger cadence |
| M02 | `s(A)` cross-stream / relying on latching (SEM-01) | Task Monitor task/trigger with Time Scope | **A/R** | Time Scope bounds differ from indefinite latch; flag each |
| M03 | `s(A, hhhh.mm)` lookback (SEM-04) | Task Monitor with Time Scope ≈ window | **A** | Window anchoring differs (SEM-04 Q2); verify per case |
| M04 | `f(A)` | edge A→X (Failure) | **E** | within-run |
| M05 | `d(A)` | edge A→X (Success/Failure) | **E** | within-run |
| M06 | `t(A)` | Failure-ish: UC Cancelled/Failed distinction | **A** | UC separates Cancelled from Failed; choose mapping, document |
| M07 | `n(A)` mutual exclusion (SEM-02, R6) | Mutually Exclusive Tasks or Virtual Resource | **A** | NOT an edge. Detector: `n()` atoms → mutex candidates |
| M08 | `exitcode(A) op k` (SEM-02) | edge variable condition on exit-code variable, or task-level exit-code→status mapping | **A/?** | UC tasks map exit codes to Success/Failed at task level (output conditions); pin exact mechanism per task type |
| M09 | `value(G) op k` (SEM-08) | edge variable condition / Variable Monitor trigger | **A** | Re-eval-on-set: AutoSys re-evaluates on SET_GLOBAL event; UC edge conditions evaluate at predecessor completion — timing differs → R if the JIL used globals as async gates |
| M10 | `$$VAR` substitution (SEM-08) | `${var}` resolution | **A** | Resolution timing + UCS-08 ordering property |
| M11 | AND `&` | multiple incoming edges | **E** | with UCS-02 skip caveat |
| M12 | OR `\|` | conditional-path restructure / Task Monitor / duplication (UCS-03) | **A/R** | per-case lowering decision; the hard compiler problem |
| M13 | box, members with no conditions (SEM-10) | workflow, parallel start vertices | **E** | |
| M14 | box with member conditions (SEM-10) | workflow with edges | **A** | assumes member conditions reference siblings; conditions referencing jobs *outside* the box → M02 |
| M15 | box_success/box_failure internal ref (SEM-12) | restructure: terminal vertex placement / workflow status by path design | **A/R** | early-exit semantics needs explicit Skip paths |
| M16 | box_success external ref, hung-RUNNING gate (SEM-12) | — | **R** | no analog; redesign (this is a bug-as-feature pattern) |
| M17 | box_terminator/job_terminator (SEM-14) | task-level failure handling + workflow Cancel actions | **A/R** | UC has no auto "kill siblings on my failure" edge; emulate via actions/monitors — per-case |
| M18 | nested boxes (SEM-17) | sub-workflows | **E** | |
| M19 | ON_ICE (SEM-20) | Skip task (definition-level Skip flag / instance Skip) | **A** | Verified: skipped predecessor doesn't block successors (UCS-02) — downstream-satisfied matches; BUT all-skipped-cascade differs from AutoSys (iced job's consumers still run if *other* conds hold; in UC all-preds-skipped → skip cascades). Linter: flag consumers whose only predecessor is iced-translated |
| M20 | ON_HOLD (SEM-21) | Hold task/instance | **E** | downstream blocked in both |
| M21 | ON_NOEXEC (SEM-22) | Skip (path-level) | **A** | close but skip-cascade caveat as M19 |
| M22 | FORCE_STARTJOB (SEM-23) | Launch task / Clear Dependencies | **A** | forced runs don't satisfy latches in UC (no latches) — ops retraining, R8 |
| M23 | CHANGE_STATUS | Force Finish / Set status via API | **A** | Force Finish leaves process running **[V]** — runbook warning |
| M24 | date_conditions + start_times/days/calendars (SEM-30–32) | Time trigger + UC Calendars | **E/A** | calendar algebra (exclude_calendar) → UC calendar with non-business days; verify custom calendar parity |
| M25 | start_mins (SEM-32) | Cron trigger (`m * * * *`) or Time trigger interval | **E** | |
| M26 | timezone (SEM-35) | trigger-level time zone | **E/?** | pin per-trigger tz support on live instance |
| M27 | run_window (SEM-33) | no direct analog; Time Wait on task + trigger restrictions | **R** | closer-edge rule (R5) unreproducible; must redesign & document per job |
| M28 | must_start/must_complete (SEM-34) | Late Start / Late Finish flags (UCS-10) | **E** | cleanest mapping in the whole table |
| M29 | term_run_time (§5) | task Maximum Run Time with Cancel action | **A/?** | pin exact UC auto-cancel config |
| M30 | n_retrys (§5) | task Retry options (max retries, interval, suppress intermediate failures) | **A** | retry trigger sets differ (Q4) |
| M31 | max_exit_success + success_codes/fail_codes (SEM-09/DL-33) | task exit-code / output success criteria | **A/?** | per task type; pin — twin shares ir.exit_is_success on both sides, Q7 corners included |
| M32 | FW jobs (watch_file) (§5) | Agent File Monitor task/trigger (UCS-07) | **A** | steady-state vs existence modes; trigger-disable gotchas **[V]** |
| M33 | cross-instance `job^INST` (SEM-07) | Task Monitor across... or UC agent/remote — depends on target topology | **R** | consolidating instances is a migration design decision, not a translation |
| M34 | job_load/priority/QUE_WAIT | Virtual Resources + Agent task limits | **A** | model mapping per machine definition |
| M35 | machine (real/virtual) | Agent / Agent Cluster | **A** | broadcast vs. any-of semantics — verify cluster distribution rules |
| M36 | alarms (alarm_if_fail, max_run_alarm…) | Email/SNMP notifications, System Operations actions | **A** | observability re-plumbing, mechanical |

### Mapping-driven compiler requirements

1. **Every Layer-G edge carries its M-row.** The UC backend refuses to compile R rows (emits
   migration report items instead); A rows compile + emit assumption records; only E rows
   compile silently. This is dsl42's "failed translation is a compile error" made granular.
2. **The migration report is a first-class output artifact** (per-catalog markdown): all A
   assumptions, all R redesigns, all [?]-dependent mappings.
3. **Detectors needed in analysis passes:** same-cycle detector (M01 vs M02), `n()`-mutex
   detector (M07), OR-shape classifier (M12), external-ref-in-box detector (M14/M16),
   iced-consumer detector (M19).

## Part III — Open questions (live UC instance / OpenAPI dive)

- U1: native OR-join / "Any" completion criteria in 7.x workflows (UCS-03) — decides M12 lowering.
- U2: exact workflow-status derivation incl. Running/Problems transitions (UCS-04).
- U3: edge record JSON schema (vertex ids, condition enum, variable-condition fields) — pull
  from openapi.json, freeze as `docs/uc-edge-schema.md`; foundation of the UC backend.
- U4: per-task-type exit-code→status configuration (M08/M31).
- U5: Time Scope bounds on Task Monitors (M02/M03) — max lookback window.
- U6: trigger timezone + calendar parity with AutoSys extended calendars (M24/M26).
- U7: Maximum Run Time auto-cancel config (M29); retry trigger set (M30).
- U8: default of `Perform Set Variable Actions Before Workflow Dependency Evaluation` on the
  target controller (UCS-08) — record in migration assumptions.

## Part IV — Trace tests (oracle-pair set)

The equivalence validator tier (c) runs *both* oracles (AutoSys semantics oracle; a minimal UC
workflow interpreter) on generated event streams and compares. Seed pairs, one per A-row where
the assumption can be violated: P-M01 (staleness event stream violating same-cycle assumption
→ traces MUST diverge; test asserts the divergence is detected & classified), P-M07 (n()
overlap), P-M09 (SET_GLOBAL mid-run), P-M12 (each OR lowering vs. AutoSys `|` truth table),
P-M19 (ice with multi-predecessor consumers), P-M27 (run_window closer-edge divergence).
These "expected-divergence" tests document precisely what the migration changes — they are the
honest core of the whole project.

## Sources
Primary: docs.stonebranch.com and Stonebranch Confluence, Universal Controller 7.2–7.9 —
Creating and Maintaining Workflows (edge conditions, skip rules, step/variable conditions),
Manually Running and Controlling Tasks (dependency clearing, re-run, force finish),
Setting Mutually Exclusive Tasks, Creating Task Virtual Resources, Triggers Overview,
Agent File Monitor Task, Workflows PDF (7.4/7.9: instance wait, late start, wait/delay),
Task Web Services & RESTful Web Services API (XML/JSON records, OpenAPI endpoint),
Workflow Task Instance Web Services (vertex addressing errors).
