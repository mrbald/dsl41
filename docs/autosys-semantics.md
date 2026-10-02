# AutoSys Semantics Dossier

Status: normative reference for the IR design, the linter rule set, the semantics oracle
(discrete-event interpreter), and the AutoSys→Stonebranch mapping table
(stonebranch-semantics.md). Verified against Broadcom TechDocs (AE 12.x) where marked.

Every numbered SEM entry has a behavior pin unless §8 records it as a non-goal. Most pins are
trace tests against the semantics oracle; §8's layer note names the entries pinned elsewhere.
Confidence levels: **[V]** verified against Broadcom TechDocs 12.x · **[C]** corroborated by
multiple secondary sources · **[F]** one field observation, not verified against TechDocs and
not re-verified, the weakest tier · **[?]** open question; before you rely on it, make sure
that a live instance agrees.

Vendor quotes in the entries are the evidence behind a **[V]** marker. The decision-log entry
cited beside a rule is where the rule was settled. §9 lists every Q-series question and its
pinned default; a **[?]** inside an entry is a local corner pinned where it stands.

---

## 0. Execution model (the frame everything else hangs on)

AutoSys is **not** a DAG engine. It is an event-driven state machine engine:

- Every job is a state machine: `INACTIVE → STARTING → RUNNING → {SUCCESS, FAILURE, TERMINATED}`,
  plus out-of-band states `ON_HOLD`, `ON_ICE`, `ON_NOEXEC`, `QUE_WAIT`, `ACTIVATED`, `RESTART`.
- The event processor (scheduler) reacts to events (status changes, timers, sendevent commands,
  global-variable sets). On each relevant event, it re-evaluates the starting conditions of
  the jobs that the event can affect.
- A job starts when ALL of the following hold simultaneously **[C]**:
  1. if `date_conditions` is set, the date/time conditions are met,
  2. the `condition` expression evaluates true,
  3. if the job is in a box, the box is in `RUNNING` state,
  4. the job itself is not `ON_HOLD` / `ON_ICE`,
  5. (`run_window`, if present, additionally gates the actual start; see SEM-33).

**IR consequence:** the faithful layer of the IR models jobs as state machines and conditions as
predicates over a status store. The DAG is a *derived* artifact with per-edge confidence
annotations, never the primary representation.

---

## 1. Conditions: predicate algebra over the status store

### SEM-01 · Conditions are latching state predicates, not edges **[V]**
`condition: s(JobA)` is satisfied if JobA's *current recorded status* is SUCCESS; the time
when that status was set does not matter. If no lookback qualifier restricts it, a JobA
success from last Tuesday satisfies `s(JobA)` today. This is the single most important
difference from run-scoped DAG engines (Stonebranch workflow edges are per-run).
*Trace test:* JobA succeeds at T0. JobB is defined later with `condition: s(JobA)`. A
FORCE-triggered evaluation at T0+72h → JobB starts.

### SEM-02 · Condition atoms **[V]**
- `success(j)` / `s(j)` — status == SUCCESS
- `failure(j)` / `f(j)` — status == FAILURE
- `done(j)` / `d(j)` — terminal: SUCCESS, FAILURE, or TERMINATED
- `terminated(j)` / `t(j)` — status == TERMINATED
- `notrunning(j)` / `n(j)` — status is anything except STARTING, RUNNING, WAIT_REPLY, RESTART,
  SUSPENDED (that is, also true for never-run/INACTIVE jobs) **[V]** — commonly used for mutual
  exclusion, not sequencing.
- `exitcode(j) OP value` / `e(j) OP value` — comparison operators against the last exit code.
- `value(GLOBAL) OP value` / `v(...)` — global-variable comparison.
Atom keywords are case-insensitive, and the one-letter abbreviations are the canonical short
forms. Job and global names are matched exactly: the parser preserves their case and the status
store keys on the name as written.
*Model note:* the oracle produces only `INACTIVE`, `QUE_WAIT`, `STARTING`, `RUNNING` and the
three terminal states. `WAIT_REPLY`, `RESTART` and `SUSPENDED` never occur in it, so its `n()`
is false for `STARTING` and `RUNNING` alone. `QUE_WAIT` stays outside that false set (DL-50): a
resource-queued job is not running, so `n()` is true for it.

### SEM-03 · Operators and grouping **[V]**
`AND`/`&`, `OR`/`|`, parentheses for precedence. Evaluation is strictly left-associative and
flat: `&` does **not** bind tighter than `|`, and C-style precedence is wrong for JIL (Q1,
DL-53). Source: TechDocs 12.1, "condition Attribute — Define Starting Conditions for a Job":
"The parentheses force precedence, and the equation is evaluated from left to right." One
grammar rule encodes it, pinned by `test_sem03_flat_left_to_right_precedence_pinned` (grammar
shape) and `test_sem03_precedence_pinned_model_level` (Cond model).
`NOT` does not exist as an operator. Negation is expressed via status atoms (for example,
`n()` and `f()`).

### SEM-04 · Lookback qualifiers **[V]**
Syntax: `s(job, hhhh.mm)` (or escaped colon `hhhh\:mm`).
- `s(job, 2)` — satisfied only if the status was reached within the last 2 hours. Sub-hour
  windows require a leading `00`: `00.30` = 30 min, bare `30` = 30 *hours*, `.30` is
  invalid. **[V]**
- `s(job, 0)` — "zero lookback": satisfied if and only if the condition job ended at or after
  the **dependent job's own last end** (Q2a, DL-54). **[V]** Source: TechDocs 12.0.01,
  condition attribute page: "When you specify 0, AutoSys Workload Automation examines the last
  end time of the job first. It then examines the last end time of the condition job. If the
  condition job has run since the last run of the job for which the condition is coded for,
  the job is allowed to start. If the condition job has not run since the last run of the job
  for which the condition is coded for, the job is not allowed to start." The page's phrase
  "the job for which the condition is coded for" names the dependent as the anchor. The
  `test_sem04_zero_lookback_*` tests pin the anchor in both directions. A dependent that
  never ended has no anchor, and the qualifier is satisfied (Q2b, DL-58). **[V]** Source:
  Broadcom community thread 760251, CA support best answer: "This is working as designed.
  When a new job is inserted it has no initial/previous end time."; the thread's reporter
  observed the epoch-0 effect as modeled. For box overrides, the box itself is the evaluator
  and the anchor.
- `s(job, 9999)` — explicit "indefinite lookback", condition-equivalent to a bare atom with no
  qualifier (the 4.5.1 default): both evaluate the same way against a non-iced predecessor.
  **[V]** Against an ICED predecessor the two diverge (DL-243, open question Q10): `9999`
  carries a lookback qualifier and keeps the blanket-true reading (SEM-05), while the bare
  atom follows the narrower vendor ON_ICE table (SEM-20). Canonicalization keeps `9999`
  distinct from the bare form for this reason.
- Lookback applies to **status, cross-instance/external status, and exitcode atoms only,
  never to `value()` global-variable atoms**. **[V]** (Linter: lookback on `v()` = error.)
- Max lookback ≈ 416.58 days (9999.59). **[V]**

### SEM-05 · ON_ICE predecessors inside lookback conditions **[V]**
If the predecessor job referenced in a lookback condition is currently ON_ICE, the atom
evaluates **true** and the scheduler ignores the lookback entirely. (Interacts with SEM-20.)
The rule is blanket over atom kinds (DL-13): `f()`, `t()`, `d()` and `e()` on an iced
predecessor are all true, not only `s()`. This blanket reading is scoped to a LOOKBACK-
qualified atom (`atom.lookback is not None`, any kind, the zero form included); an ORDINARY
atom (no lookback qualifier at all) on the same iced predecessor follows SEM-20's narrower
vendor truth table instead (DL-243). Ice on a job that is STARTING or RUNNING takes effect
when that run ends; atoms read the real in-flight status until then **[?]** (unverified corner,
modeled deliberately).
Source (DL-58): KB 438836, 12.1.01: with a local ON_ICE predecessor "the system ignores the
look-back condition" and "continuously evaluate[s] the dependency as true". The same KB's
cross-instance caveat: ON_ICE is NOT transmitted to a remote instance; the remote sees SUCCESS
with its real timestamp, and lookback DOES apply against it. XinstIR is an opaque boundary
here (SEM-07), so that caveat is a dossier note, not model behavior.

### SEM-06 · Undefined jobs in conditions **[V]**
A condition atom that references a job that does not exist in the database evaluates **false,
permanently and silently**; the dependent job never auto-starts. AutoSys ships `job_depends`
to detect this. This is linter rule L001 (dangling reference), severity error.

### SEM-07 · Cross-instance atoms **[V]**
`s(jobB^PRD)` — the same predicate algebra against a job on external instance `PRD` (declared
via `insert_xinst: PRD` with `xtype:`, not `insert_machine`). Lookback applies. For the
migration these become boundary markers in the IR: dependencies whose producer is outside the
modeled universe.

### SEM-08 · Global variables **[V]**
- Set via `sendevent -E SET_GLOBAL -G NAME=value` or `insert_global` JIL.
- In conditions: `value(NAME) = X` (also `>`, `<`, `!=` comparisons). *Model note:* the
  comparand is a string in JIL. The oracle compares numerically when both sides parse as
  base-10 integers, and lexicographically when either does not. The rule covers all six
  operators, so `value(N) = 5` is true against a stored `05`. The UC twin and the equivalence
  checker call the same comparison function, so the three layers cannot disagree.
- In attribute strings (command, std_out_file, …): `$$NAME` or `$${NAME}` substitution at
  runtime. Single-`$` is shell/environment, double-`$$` is AutoSys global; the parser keeps
  these distinct. A global-variable set is an event that triggers condition re-evaluation.
  Substitution itself is not implemented: real execution refuses an unsubstituted `$$NAME`
  site on an exec field at preflight (DL-240).

### SEM-09 · max_exit_success shifts SUCCESS/FAILURE boundary **[V]**
A job with `max_exit_success: 2` records SUCCESS for exit codes ≤ 2. Therefore `s(j)` on a
consumer is only meaningful relative to the producer's `max_exit_success`. IR: the success
predicate is per-job-configurable, not a constant. Equivalence checking must normalize this.

Two further attributes shape the boundary (DL-33). They are valid on Command, i5/OS, Micro
Focus, and z/OS jobs; in this project's scope, **CMD only**, and a loud error on BOX/FW:
- `success_codes` — explicit success codes: single code, `lo-hi` range, or comma list of
  both (`1,3,20-30`). Absence-default: "exit code 0 is success". **[V]**
- `fail_codes` — explicit failure codes, same format. Absence-default: "any non-zero exit
  code is failure". **[V]**
The verdict is `f(exit_code; max_exit_success, success_codes, fail_codes)`, with a single
source, `ir.exit_is_success`, shared with the UC twin (M31).

Composition (Q7, DL-58; KB 408778): a present `fail_codes` decides **alone**. Listed codes are
FAILURE and "Any other exit code … will be interpreted as a success", so `success_codes` and
the threshold are ignored beside it, and a code in both lists is FAILURE. If `fail_codes` is
absent, a present `success_codes` replaces the success rule entirely (unmatched code →
FAILURE, threshold ignored). If neither list is present, `max_exit_success` decides.

---
## 2. Boxes

### SEM-10 · Box membership and start rule **[V]**
`box_name: B` puts a job in box B. Members start when: box is RUNNING **and** the member's own
conditions hold. Members with no conditions start immediately when the box starts. A member runs
**at most once per box run**. **[V]**

A box start resets the statuses of the jobs it contains (DL-242). **[V]** TechDocs 24.2 and
12.x, scheduling guide, Basic Box Job Concepts: "When a box starts running, the status of all
the jobs it contains (including subboxes) changes to ACTIVATED ... Because of this status
change, jobs in boxes do not retain their statuses from previous box cycles." And: "When the
box is scheduled to run, the statuses of ON_NOEXEC jobs in the box change to ACTIVATED." The
oracle has no ACTIVATED status (SEM-17). At box start it sets every job the box contains,
transitively, to INACTIVE. A job that is STARTING, RUNNING or QUE_WAIT keeps its run, and so
does everything inside a live subbox. An ON_NOEXEC member is reset like any other and keeps
its flag. The reset clears `exit_code` with the status, on rows already INACTIVE too, so an
`e()` atom does not read the previous cycle's result. It keeps `last_end_at`, the flags and
the arm. The rows are written before the box's STARTING transition, so that transition's
wakes read the new cycle. Their wakes run after it, while the box is STARTING: no member can
start, no completion check runs, and the box cannot start again. A consumer outside the box
sees the change at once, for example an `n()` atom with a lookback, which reads the moved
status time. If those wakes end the box's run (a `job_terminator` cascade), the start stops
there: the box is not set RUNNING and no member starts.

### SEM-11 · Box RUNNING/completion **[V]**
The box stays RUNNING while any member is running. The box cannot complete before all members
run (or are bypassed). Default: box SUCCESS if and only if all members ended SUCCESS. Box
FAILURE if at least one member failed (evaluated after all members complete). A member that
ended TERMINATED counts as failed for this fold; SEM-14 kills land here.
Two carve-outs to the literal fold resolve a member as INACTIVE. Each is a completion moment
and runs the full completion door: overrides first, then the default fold.
- A run_window skip inside a live box run is an explicit INACTIVE verdict (DL-13, DL-154),
  including the one a box start decides (DL-246). The member leaves the run and casts no vote in the fold **[C]** (mechanism tier; SEM-33's vendor
  quotes pin the verdict and the completion, not the bookkeeping).
- An operator's INACTIVE on a member of a RUNNING box counts as SUCCESS (DL-242). **[V]**
  TechDocs 24.2 and 12.x, Basic Box Job Concepts: "Using the sendevent command to change the
  state of a job in a box to INACTIVE affects the box's completion status as if the INACTIVE
  job returned a status of SUCCESS." This holds for a member that ran, one that failed, and
  one that still waited. DL-235 still holds for a launched run: no kill.

The box row records both kinds in `window_skipped_members`; the member's own later start voids its
mark. A resolved member stays settled if an operator later gives it a status that is not
live; the fold still votes over members that ran. A resolved member is a completion moment
for every running ancestor's overrides too, since "inside" is transitive (SEM-12). A member
whose condition never fires inside the run, and that nobody resolves, still hangs the box:
waiting reads INACTIVE without the mark.

### SEM-12 · box_success / box_failure override — with evaluation gating **[V]**
`box_success: <condition expr>` (same predicate language). Verified semantics:
- If the referenced job is **inside** the box: the scheduler evaluates the box status the
  moment that job enters the specified state, regardless of other members. "Inside" is
  **transitive** (DL-12): a job in a nested box is inside every box above it, so an outer
  box's override fires on a grandchild's transition while the inner box is still RUNNING.
- If the referenced job is **outside** the box (or an external job, or a global): the
  scheduler evaluates the box status when *some member completes after* the external
  condition became true. If all members complete *before* the external condition is met, the
  box is **not** evaluated and stays RUNNING, the hung-box incident pattern. Linter: warn on
  box_success/box_failure that reference non-member jobs (L008).
- If box_success is specified but not met, and box_failure is unspecified → default failure
  logic applies after all members complete (and vice versa). If neither fires, the box stays
  RUNNING indefinitely. **[V]**

### SEM-13 · Box TERMINATED is sticky **[V]**
A box moved to TERMINATED (for example, KILLJOB) stays TERMINATED regardless of later member state
changes, until the next box start.

### SEM-14 · box_terminator / job_terminator **[V/C]**
Control flow, not alarms:
- `box_terminator: 1` on a member — if this member FAILs, terminate the containing box.
- `job_terminator: 1` on a member — if the containing box terminates/fails, terminate this member.
Members killed this way end with status TERMINATED (this matters for `d()`/`t()` consumers).

### SEM-15 · Member status changes can ripple upward **[V/C]**
A CHANGE_STATUS/FORCE_STARTJOB on a member of a *non-running* box can change the box's derived
status and thereby trigger downstream jobs conditioned on the box. The oracle models box
status as derived state re-evaluated on member events. **[C]**

INACTIVE members are ignored in that re-evaluation (DL-242). **[V]** TechDocs 24.2 and 12.x,
Basic Box Job Concepts: "Any jobs in the box with a status of INACTIVE are ignored when the
status of the box is being re-evaluated." The page's single-member table:

| Box status | Member changes to | Box becomes |
| --- | --- | --- |
| SUCCESS | TERMINATED or FAILURE | FAILURE |
| FAILURE | INACTIVE or SUCCESS | SUCCESS |
| FAILURE | FAILURE | no change |
| INACTIVE | INACTIVE or SUCCESS | SUCCESS |
| INACTIVE | TERMINATED or FAILURE | FAILURE |
| TERMINATED | anything | no change (SEM-13) |

The page's worked example has an INACTIVE box with four INACTIVE members. One is forced and
completes SUCCESS, so the box is SUCCESS. And: "if the status of the same job is being updated
to INACTIVE and all the other jobs inside the box are already in INACTIVE status, the box
status is re-evaluated and returns a SUCCESS status as it ignores all the jobs that are in
INACTIVE status." So the oracle re-derives once every member that is not INACTIVE is
terminal, and a box with only INACTIVE members derives SUCCESS. A terminal member transition
triggers the re-evaluation, and so does an injected INACTIVE on a member. A box-start reset,
a SEM-18 cascade and a window skip are internal transitions and do not trigger it. The box is
read again after the injected transition: a box that the transition's own wakes started is
not re-derived.

### SEM-16 · Jobs added to a RUNNING box **[V]**
When a job is inserted/moved into a running box: an ALERT event occurs, and the job's run
number is set to the box's. If the job is not STARTING/RUNNING/ON_ICE and its run number does
not exceed the box's, a STARTJOB is issued. Not migration-critical (definition-time mutation),
but the AST layer must not assume static membership. An oracle non-goal.
`SEM-16` is also the house class name for mid-run catalog-object mutations that are out of
scope, mid-run `update_resource` replenishment of a depletable included (DL-50).

### SEM-17 · Deep nesting **[C]**
Boxes nest arbitrarily (practical guidance: ≤ 1000 members, avoid organizational grouping;
Broadcom's own guidance is boxes for *shared starting conditions*). ACTIVATED state = "top-level
box is RUNNING, member not yet started." The oracle does not model the ACTIVATED label. Its
state effect is modeled (DL-242): a waiting member reads INACTIVE (SEM-10's reset), and an
explicit INACTIVE verdict carries the box row's resolution mark (SEM-11).
*Model note:* lowering accepts at most 64 containment links as a compiler sanity limit; a deeper
chain is a loud finding, not a silent truncation.

### SEM-18 · CHANGE_STATUS INACTIVE on a box cascades **[V]**
TechDocs 24.2 and 12.x, Basic Box Job Concepts: "Using the sendevent command to change the
state of a box to INACTIVE changes the state of all the jobs it contains to INACTIVE." The
oracle sets the box INACTIVE, then every job it contains, transitively and top-down: a subbox
before its own members (DL-242). Jobs already INACTIVE are skipped. Cascaded members are not
marked resolved. The cascade is one batch: every row is written and settled first, the box
runs it ends lose their unconsumed member arms (the Q3c pin, as at a terminal box
transition), and only then do the wakes run. So no member starts inside a half-moved subtree.
A wake may start the box again; that new run is left alone. A live member is treated as
DL-235 treats an injected INACTIVE: no kill is planned, its reservations release, its
process's later exit is rejected as "job not live: INACTIVE", and resume does not relaunch
it.

---

## 3. Out-of-band status manipulation

### SEM-20 · ON_ICE **[V]**
- The job will not run on a plain start. It is removed from all conditions/logic, except
  through FORCE_STARTJOB (SEM-23, DL-243), which returns a non-live iced job to an executable
  state first.
- **Downstream conditions on a non-live iced job split by lookback qualifier (DL-243).**
  An atom WITH a lookback qualifier (any kind, the zero form included) keeps the DL-13
  blanket-true pin (SEM-05): every atom kind reads satisfied, lookback ignored. An ORDINARY
  atom (`atom.lookback is None`, no qualifier at all) instead follows the vendor's own
  ON_ICE truth table. Source: "Start Conditions" (AutoSys Workload Automation 24.2
  documentation), the ON_ICE row of the downstream-conditions table: "success" TRUE,
  "failure" FALSE, "terminated" FALSE, "done" TRUE, "notrunning" TRUE, "exitcode" FALSE.
  This is a NARROWER reading than the pre-DL-243 blanket pin for f()/t()/exitcode() on an
  ordinary atom; s()/d()/n() are unaffected (both readings already say true). The two tables'
  disagreement over a lookback-qualified atom is not addressed by the vendor text and stays
  an open pin (Q10, section 9), not a citation.
  Inside a box, a member that depends on an iced sibling starts immediately when the box
  runs (an ordinary s() atom, true under both tables). **[V]**
- OFF_ICE: the job does **not** run even if its starting conditions currently hold. It waits
  for conditions to *reoccur*. **[V]**
- IR: on_ice ≙ graph rewrite "excise node, short-circuit its outgoing dependency edges to true".

### SEM-21 · ON_HOLD **[V]**
- The job will not run. **Downstream is blocked** (conditions on it do not become true).
- OFF_HOLD: if starting conditions are *already satisfied*, the job runs immediately (missed
  runs during hold collapse to at most one run). **[V]** Source (DL-54): TechDocs 12.0, Start
  Conditions: "When you take jobs off hold, the scheduler does not re-evaluate date and time
  conditions. Jobs that meet their date and time conditions while they are on hold start
  immediately after they are taken off hold unless other starting conditions apply and are
  not satisfied." Oracle: a scheduled tick landing on a held job ARMS it, the same Q3 latch as
  SEM-32, so OFF_HOLD starts it through the schedule gate. The page's run-window exception
  (off-hold outside the run window reschedules "to their next start time") stays governed by
  SEM-33's verified closer-edge rule. The two sources are not fully reconciled: noted, not
  modeled apart.
- In a box: a held member prevents box completion; it holds the whole stream.
- IR: on_hold ≙ pause node, edges intact.

### SEM-22 · ON_NOEXEC **[V]**
Bypass-execution mode: the scheduler processes the job through its lifecycle but does not run
it. The job (and boxes that contain it) evaluate as SUCCESS, and downstream runs normally. Box
in ON_NOEXEC scheduled to run → goes RUNNING, members are bypassed to SUCCESS as their
conditions are met, box returns to ON_NOEXEC afterward. The bypass overrides manual status
changes to members while the box is ON_NOEXEC. This is the "dry-run wiring" state.
*Model note:* the box sentence is applied at **each box level**, so a member box of an
ON_NOEXEC box also goes RUNNING and bypasses its own members; the dry run walks the whole
tree. A member bypasses on its own flag or on any containing box's, and the bypass counts as
that member's start for the run: it joins the box's ran set, so the SEM-11 fold waits for
every member's bypass, and a member whose condition never fires keeps the box RUNNING like any
member that never ran. The bypass is also the tick's run for SEM-34, so a bypassed job raises
no MUST_START_ALARM. The vendor text states one box level; applying it per level is this
project's pin. **[?]**

DL-243: a FAILURE or TERMINATED (non-live, non-BOX) job that is put ON_NOEXEC is moved to
INACTIVE, exit code cleared, through the same path an operator's `CHANGE_STATUS INACTIVE`
takes (DL-242) -- an EVENT-TIME transition, not a read-time projection off the stored status.
Sources: "Start Conditions" (AutoSys Workload Automation 24.2 documentation): "When you send
the JOB_ON_NOEXEC event and the job is in the INACTIVE, SUCCESS, or ACTIVATED status, the job
retains its current status; otherwise the effect is the same as if the job enters the INACTIVE
status." "Job States" (same documentation), the ON_NOEXEC state entry: "the scheduler places
the job in the ON_NOEXEC status and the effect is the same as sending the CHANGE_STATUS event
to INACTIVE for the job. The scheduler does not immediately schedule downstream jobs that have
dependency on the NOEXEC job nor does it evaluate their success conditions to success. Instead,
the scheduler evaluates the conditions of downstream dependent jobs as if the predecessor job
is set to the INACTIVE status." For a FAILURE or TERMINATED job this means f()/t()/d()/
exitcode() atoms read false and n() reads true, while s() stays false (it already was). SUCCESS
is the documented exception and is left alone. A job still live (STARTING/RUNNING/QUE_WAIT)
when ON_NOEXEC arrives is untouched -- a real failure that follows it later is not
retroactively hidden. A RUNNING box's member resolves the same way DL-242 rules; a BOX target
keeps the flag-only behavior (descendant propagation of this rule is not modeled). The vendor
text also says dependent jobs are not "immediately" scheduled, where this oracle wakes
referencers synchronously like any other transition; that gap is noted, not modeled. Because
the transition updates `status_at`, an ordinary (non-lookback) `n()` atom was already true
before it (FAILURE/TERMINATED already satisfies NOTRUNNING) and stays true; only a
LOOKBACK-qualified `n()` atom can newly turn true by this specific transition, from the
refreshed timestamp.

### SEM-23 · FORCE_STARTJOB vs STARTJOB **[C]**
STARTJOB honors nothing extra (it *is* the normal start event). FORCE_STARTJOB starts the job
regardless of conditions. Force-started runs still emit normal status events, so forced runs
satisfy downstream latching conditions. The oracle takes both as injectable events.
Which gates a force bypasses (DL-13): the `condition` expression, `ON_HOLD`, the schedule gate
(SEM-30/32), the box-RUNNING gate and the once-per-box-run gate (SEM-10). Which gates still
hold: a job that is already STARTING/RUNNING/QUE_WAIT, and `run_window`; the SEM-33 closer-edge
rule applies to a forced start like any other.

DL-243: FORCE_STARTJOB on a non-live job that is `ON_ICE` or `ON_HOLD` clears that flag
first, then starts the job through the normal FORCE path -- it is no longer an unconditional
refusal. Source: "sendevent -- Start Jobs" (AutoSys Workload Automation 24.2 documentation):
"When you force start a job that is in a non-executable state (ON_HOLD, ON_ICE), it returns to
an executable state, runs, and does not revert to the previous (non-executable) state." The
clearing is recorded the same way `_handle_oob` records an OFF_ICE/OFF_HOLD sendevent, with a
cause naming FORCE_STARTJOB, and the flag stays cleared after the run -- including when a
later gate (`run_window`) still refuses the start, because the return to an executable state is
the event's own effect, not conditioned on the start succeeding. `ON_NOEXEC` is not named in
the vendor sentence and is untouched by FORCE. A job that is already STARTING/RUNNING/QUE_WAIT
is still refused: the same page states concurrent runs of one job are unsupported.

### SEM-24 · `status:` at definition time **[V]** (existence) / **[?]** (full value set)
Estate-shaped JIL carries `status: ON_HOLD` on `insert_job` (including on box jobs): the job
is created already in an out-of-band state, equivalent to an insert plus an immediate sendevent
of the state. Source: TechDocs 12.0.01, "status Attribute — Set an Initial Status for a Job
During Insertion", which also states that the attribute cannot be used with
update_job/override_job. The page's exact documented value list is unretrieved **[?]**. The
modeled set is `INACTIVE` (the implicit default) plus the SEM-20/21/22 states `ON_HOLD` /
`ON_ICE` / `ON_NOEXEC`.
- Lowering: `Semantics.initial_status`. Any other value (in particular run states like
  `SUCCESS`, which can interact with the SEM-01 latch) is a loud lowering error; extend
  deliberately when the page or an estate shape shows one, never guess.
- Oracle: seeds the SEM-20/21/22 flags before the first event. No trace entry (definition
  state, not a transition).
- UC mapping: M20 (Hold, E-class) covers `ON_HOLD`. Ice/noexec follow their SEM-20/22 rows.
  The compile twin does not model definition-time state and records it in the exclusion
  ledger instead (DL-18).

---
## 4. Date/time scheduling

### SEM-30 · date_conditions is the master switch **[V]**
The scheduler honors the time attributes (`days_of_week`, `run_calendar`, `exclude_calendar`,
`start_times`, `start_mins`, `run_window`, `must_start_times`, `must_complete_times`,
`timezone`) only when `date_conditions: 1`. Without it, the job runs purely on
conditions/manual events, and the scheduler ignores the time attributes (linter: warn on
time attributes present with date_conditions absent/0, dead configuration).

### SEM-31 · Mutual exclusivity **[V]**
- `start_times` XOR `start_mins` (both → JIL error, both ignored).
- `days_of_week` XOR `run_calendar` (cannot combine). `exclude_calendar` subtracts days from
  whichever is active.
Time attributes on a job inside a box: the member still needs the box RUNNING. A scheduled
member of a non-running box does not fire (schedule + box gate compose with AND).

### SEM-32 · start_times / start_mins **[V]**
`start_times: "10:00, 11:00"` — absolute times of day (24h). `start_mins: 10,20,30` — minutes
past *every* hour. Each firing inserts a STARTJOB event. Time and condition compose as AND.

**Arm and wait (Q3, DL-54, DL-58).** A tick whose `condition` is still false ARMS the job. The
condition edge later starts it, and the start consumes the arm (at most one run per tick).
The arm has no expiry. The TechDocs support is an entailment, not one dispositive sentence;
the community threads below state it directly. TechDocs 12.0, Basic Box Job Concepts and
Job States: "When a box starts running, the status of all the jobs it contains ... changes
to ACTIVATED";
"Maintains jobs with additional starting conditions in the ACTIVATED state until those
additional dependencies are met"; ACTIVATED is "the job itself is waiting to start". The
governing rule is a continuously evaluated AND: "All defined starting conditions must be true
for a job to start" (Start Conditions, 12.0). The held-across-tick case is documented as
start-on-release (SEM-21). The standalone case (thread 734033, CA's Mark Hanson, with
reproduced tests for both start_times and run_calendar): "There is a STARTJOB event associated
with the start_time or run_calendar … The STARTJOB event being processed satisfies the
start_times/run_calendar dependency", and a start "resets" it; the log line
`CAUAJM_I_40162 starting conditions have not been met` is the armed tick's signature. The
no-expiry boundary (thread 801986, Broadcom employee): "no set limit to how long [the job]
would wait … it will run immediately after the next time [the predecessor] completes
successfully, regardless of how far in the future that is", reset only by a (force-)start or
a JIL update. **[V]**

Pins around the arm:
- Ticks blocked at ON_ICE (SEM-20 reoccurrence) or at box-not-RUNNING do NOT arm, including
  a HELD member of a not-running box. A tick on an already-live job does not arm.
- A member's arm is scoped to the box run that armed it: an unconsumed member arm dies at box
  completion (`SCHED_DISARM` trace marker), so no member ever auto-starts a later box run
  from a stale tick. **[?]** Q3c: thread 801986's aside, "JobB would start immediately after
  the next time its parent box starts", hints that a MEMBER's latch can survive into the next
  box run. The scoped pin stands until a live test (`# PENDING: Q3c`, oracle.py).
- A pre-existing arm survives ON_ICE/OFF_ICE untouched: ice neither arms nor disarms, because
  the latched tick predates the ice. **[?]** Q3d (DL-69): that pin is uncited, and its
  consequence is that a stale tick can start the job on a condition edge after OFF_ICE, in
  tension with the reoccurrence rule. The pin stands until the live discriminator runs
  (`# PENDING: Q3d`, oracle.py; protocol in the live-instance runbook).
- The ACTUAL start consumes the arm. A DL-50 QUE_WAIT enqueue keeps it latched, so a canceled
  queue attempt does not eat the tick; KILLJOB on the queued job consumes it, because the
  kill happened.
- The runner's operator `DISARM` verb (control-protocol §3, DL-158) drops an unconsumed arm on
  demand. It is an engine-side control with no vendor sendevent counterpart; the vendor reset
  list above is unchanged.
- An armed job re-blocked by run_window re-uses one pending defer timer per opening instant.
  Accepted consequence of latch-until-consumed: an armed run can land in a LATER run_window
  cycle than its tick (the SEM-33 closer-edge rule applies at the armed start's own moment).

### SEM-33 · run_window is a gate, not a trigger **[V]**
`run_window: "02:00-04:00"` — not a starting condition, but an additional constraint on when
a start can actually occur. Its endpoints are wall times in the job's own zone (SEM-35), so
the engine compares them in that zone and schedules the deferred start at the corresponding
absolute instant. If conditions become true outside the window, AutoSys picks the
closer edge. Closer to the next window opening → schedule STARTJOB at window open. Closer to
the previous window's end → do not run, set INACTIVE. **[V]** Max span 24h. The window can
cross midnight. Both endpoints are inclusive: an attempt at exactly the opening or the closing
minute is inside the window. Equal endpoints (`"02:00-02:00"`) are a zero-width window, not a
24-hour one: only that one instant is inside. An attempt exactly midway between the previous
close and the next opening resolves to the next opening. Both readings are **[?]**: undocumented
ties, pinned this way, revisit with live access. The "closer edge" rule is a prime migration
hazard (no direct Stonebranch analog); always flag it in mapping.
A standalone job that meets the previous-close branch moves to INACTIVE (DL-246). **[V]**
TechDocs 24.2, run_window attribute page (12.1 has the same text): "When the current time is
closer to the end of the previous run window, the product does not start the job and changes
its status to INACTIVE." A prior SUCCESS, FAILURE or TERMINATED becomes INACTIVE, so
downstream s()/f()/d() atoms read false. The transition wakes referencers as an injected
INACTIVE does. The exit code stays, as it does for an injected INACTIVE. A job already
INACTIVE records the skip and gets no transition.
Box interaction (verified example): a box start decides the window disposition of each
waiting member at that instant (DL-246). **[V]** Same page: "If the job is in a box, its
status changes to ACTIVATED when the box starts running. However, if the current time is not
in the specified run window for the job, its status changes to INACTIVE. When the current time
is closer to the end of the previous run_window, the job's status changes to INACTIVE. The box
job can still run to completion. When the current time is closer to the beginning of the next
run_window, the product issues a future STARTJOB event for the job for the next run_window."
Its Box1 example: "If Box1 starts at 04:05, JobB and JobC can run and JobA becomes INACTIVE so
that the box can complete that day. If Box1 instead starts at 16:05, JobA will have a STARTJOB
event set for 02:00 the next day, and the box continues running until the job starts the next
day." The decision runs before the member's own schedule gate, so no schedule tick is needed.
It runs after every start attempt in the subtree the box start began, including the
attempts that the box's RUNNING transition wakes, deferrals before skips. So a skip that
completes a box or an ancestor never precedes a sibling's or an outer member's attempt. That
removes the order dependence between window decisions and member attempts only: competing
sibling completions are still evaluated one at a time under SEM-12, and no rule for
simultaneous overrides is defined. Each decision is bound to its box run; a run that ended
or was replaced during the pass is not decided for.
Inside the window nothing is decided: the member waits for its own conditions and schedule.
Closer to the previous close, the member takes the skip bypass below. Closer to the next
opening, one deferred start is queued, and the box stays RUNNING overnight. The direct parent
decides at its own start, so a member of a subbox is decided when the subbox starts. A held or
iced member, a live one and one that already ran this execution are not decided there; the
held exclusion is this project's reading, since a held job does not take the box start's
status change. A member's deferred start belongs to its box and box run: if the box starts
again before the opening, or a rebaseline moves the member to another box, the old deferral
is refused when it fires, and the current run decides afresh. Only a deferred start dedups a deferral to the same opening; a deadline timer does not.
**[?]** The deferred STARTJOB's interaction with the member's own start_times is not
documented. Provisional (DL-246): the deferred STARTJOB is a start attempt with a schedule
tick's standing, through the normal gates at the window opening. A false condition arms it
(SEM-32). At most one start per box run still holds, so whichever of the deferred start and
the member's own tick runs first is the run, and a later one is refused (SEM-10).
**[V]** The "so the box can complete" half is cited (TechDocs 12.1, run_window attribute
page): "the job's status changes to INACTIVE. The box job can still run to completion." The
page's Box1 worked example: "JobA becomes INACTIVE so that the box can complete that day." The
modeled mechanism is **[C]** (DL-154): the quotes pin the INACTIVE verdict and the box
completing, not the bookkeeping. The 24.2 page has the same sentences. The skip inside a live box run is a bypass, the ON_ICE shape:
the member goes INACTIVE, stays out of the box's ran set (it casts no vote in the SEM-11 fold),
and the skip resolution is a completion moment. The box runs the full completion step, so a
satisfied box_success/box_failure fires there, the default fold runs only if none did, and a
specified-but-unmet override still hangs the box (SEM-12's own rule composed with the INACTIVE
verdict). The vendor anchors INACTIVE at box start, which DL-246 models. A mid-run attempt that lands on the skip
branch (a condition edge, or a FORCE_STARTJOB, which does not override run_window, SEM-23)
bypasses identically; that is this project's pin, not the vendor's text. The closer-edge rule
applies at the attempt's own moment. SEM-11's literal fold
carries the matching carve-out; a member whose condition never fires still hangs the box
(DL-13).

### SEM-34 · must_start_times / must_complete_times are alarms only **[V]**
They emit MUST_START_ALARM / MUST_COMPLETE_ALARM. They do not affect control flow. Absolute or
relative (`+n` minutes from each start time), not mixed. The count must match the number of
start_times (JIL insert error otherwise) for the absolute form. A single relative offset is
accepted against any number of start_times and broadcasts to all of them. **[V]** On every
captured edition's must_start_times and must_complete_times pages, the note that counts must
times against start times sits inside the absolute-format list item, and the relative syntax
is a single `+minutes` for each start time. TechDocs 24.2, must_complete_times page: "Each job
run must complete within 8 minutes after each start time". The doc-derived corpus fixture
uses one `+3` against three start_times, from that example. A list of several relative
offsets has no documented syntax. **[?]** It is accepted against start_times with the
positional pairing below, and refused against start_mins (DL-248). Relative can cross ≤ 2 calendar days. Each must_complete must precede the next
start. Those last two are recorded vendor constraints, not loader validation: lowering checks
the form and the count, never the span or the ordering. IR: model as SLA annotations, not
semantics.
The relative form also counts against `start_mins` (DL-248). **[V]** TechDocs 24.2,
must_complete_times attribute page: "The must complete times are calculated relative to the
start_mins or start_times attributes." Its start_mins example runs every 10 minutes with
`+7`: "the 2:10 p.m. job run must complete by 2:17 p.m." Only that documented form lowers: a
single relative offset, broadcast to every start_mins tick. A list of relative offsets or an
absolute form against start_mins is not specified by the vendor pages and stays open; lowering
refuses it.
**Relative must_complete is anchored to the schedule slot (DL-248). [V]** The same page: "Each
job run must complete within 8 minutes after each start time (10:08 a.m., 11:08 a.m., and
12:08 p.m.)". How Must Start Times and Must Complete Times Work (24.2): the CHK_COMPLETE event
for the next must complete time is inserted with the job; "The scheduler checks for the
SUCCESS, FAILURE, or TERMINATED events. If the job has not completed, a MUST_COMPLETE_ALARM is
issued." So the deadline is the tick's time plus that slot's offset. The STARTJOB tick arms
it, as it arms must_start, and a late start neither moves nor re-arms it. Elapsed run time is
`term_run_time`'s business, not this one's.
Recorded choices (DL-248), the smallest rule the vendor sentences allow. **[C]**
- The run a tick asks for is the first run to begin after that tick. Its deadline is met once
  that run is no longer STARTING or RUNNING, or once a later run has begun; runs of one job
  never overlap. Otherwise the deadline alarms, including when no run began at all.
- At most one relative must_complete deadline is pending per job. The vendor inserts the next
  CHK_COMPLETE only "after the job completes". A tick arms one only when the job is not live
  (STARTING, RUNNING or QUE_WAIT) and no earlier deadline is still pending, that is neither
  met nor fired. A slot that passes while a run is live or a deadline is pending gets none.
  must_start keeps one deadline per tick.
- A terminal status from before the tick does not meet the deadline.
- A run that ends without a SUCCESS, FAILURE or TERMINATED transition of its own counts as
  ended: an injected non-terminal status, say. A KILLJOB that dequeues a QUE_WAIT run sets
  TERMINATED without a new run number, so the oracle alarms where the vendor's check would see
  a TERMINATED event. Both corners stay open. **[?]**
- A held job's tick latches (SEM-21) and arms the deadline. An iced job's tick and a member's
  tick while its box is not RUNNING arm it too, and it alarms, because no run follows. The
  deadline belongs to the tick, as must_start's does.
- A FORCE_STARTJOB, a condition edge, an OFF_HOLD release and a run_window deferred start are
  not ticks and arm nothing. A run they begin can still meet an earlier tick's deadline. The
  ON_NOEXEC bypass is a run and meets it (SEM-22).
*Model note:* only the relative forms arm an alarm. Absolute `must_start_times` /
`must_complete_times` lower to IR and are carried, but the oracle owns no calendar, so no
absolute deadline is armed. Under the strict count match the offsets pair with the start_times
**by position**: the oracle reads the tick's own time of day, in the job's timezone, to name
the slot. A tick at an instant that matches no start time (an operator's STARTJOB at another
time) cannot be paired and takes the first offset. **[?]**
(Contrast `term_run_time`: that one *is* control flow, auto-TERMINATE after n minutes.)

### SEM-35 · timezone **[V]**
Per-job `timezone:` re-bases all time attributes of that job. IR carries tz per schedule
block. Equivalence of schedules is tz-aware.
Scope (DL-23): TechDocs' own `date_conditions` page lists `timezone` (with `run_window` and
`must_*_times`) among the attributes date_conditions gates, and the `timezone` page describes
only "the job's time settings". So timezone without truthy date_conditions is dead
configuration (SEM-30/L005). The zero-lookback anchor is the dependent's own last end (SEM-04,
Q2a), a comparison of two instants that no timezone re-basing can affect, so timezone on a
condition-only job is unconditionally dead and L005 fires without a caveat. Estates that carry
timezone as convention can run `dsl41 lint --suppress L005`.

**Name resolution [V]** (DL-62; 12.1 `timezone` attribute page and the `autotimezone` command
page). The value is "a string that corresponds to an entry in the ujo_timezones table or is
recognized by the operating system or is any valid POSIX value". It is **not case-sensitive**,
up to 50 chars of `a-zA-Z0-9/_-`; quote values containing colons, e.g. `"IST-5:30"`. The
scheduling manager matches the string against the OS **first**. Only if not found is the
ujo_timezones table read, **up to five times**, to chase a chain down to a resolvable zone;
unresolved after five reads, the job fails. ujo_timezones is a vendor-shipped, admin-editable
(`autotimezone -a/-c/-t/-d`) table of three entry types, Zone, Alias, City, mapping names to
POSIX TZ variables. `autotimezone -l` lists it, and city entries such as
`Vancouver City Canada/Pacific` (the docs' own excerpt) name the matching region zone. POSIX TZ
offsets are **west-positive** (`GMT+5` = 5h west of GMT).
The runner's port of this ladder, including the no-map unique-city default and its WARN, is
DL-62 / runner-design §5. Two narrowings of the vendor's text are deliberate there. The
50-character `a-zA-Z0-9/_-` set is the vendor's statement about *names*, while a POSIX value
may also carry `+` and `:` (the page's own `"IST-5:30"`), and the resolver accepts those. Only
fixed-offset POSIX forms resolve; a POSIX string with DST rules is refused, because
approximating vendor DST rules would silently shift ticks.

### SEM-36 · Calendar definitions: the autocal_asc record model **[V]**
The scanner/IR *carry* of calendar exports is DL-36's. This entry pins what the records mean.
A standard `calendar:` is a literal day list (bare `MM/DD/YYYY [HH:MM[:SS]]` rows; the export
sample writes the seconds tail, both widths are accepted, and seconds are truncated). An
extended calendar generates its day set from rules. The 12.x file-format syntax block (Manage
Calendars, 12.0.01/12.1) is, condensed:

```
ext_calendar: name
[description: text]
[workday: {X|.}{X|.}{X|.}{X|.}{X|.}{X|.}{X|.}]   # Monday-first; X workday, . non-workday
non_workday: {O|S|N|W|P}
[holiday: {O|S|N|W|P}]
[holcal: std_cal_name]
[cyccal: cycle_name]
[adjust: {+|-}n]                                  # n a single digit 1–9; 0 = no adjustment
[condition: keyword]                              # repeatable; grammar in SEM-37
```

- `workday` default: "By default, Mon-Fri are workdays and Sat-Sun are non-workdays." The
  file-record examples on the `autocal_asc` command page use a *second* serialization,
  `workday: mo,tu,we,th,fr` (comma-separated two-letter days), alongside the positional
  `{X|.}` form above. Both are Broadcom-primary.
- `holcal`: "Specifies the standard calendar containing dates to treat as holidays. The
  utility applies the holiday action to the dates in this calendar. Ensure that you specify
  this argument when you specify a holiday action." Standard calendars only; no doc instance
  permits an extended calendar as holcal.
- `cyccal`: "Specifies the cycle that any cycle-related conditions apply to. Ensure that you
  specify this argument when you specify any cycle-related conditions."
- **No timezone and no active-window attribute exist on calendar records**: the syntax block
  enumerates every field (brackets marking optional) and contains neither. `timezone` is a
  job attribute (SEM-35). Absence of evidence, but against a complete enumeration.
- Record-keyword spelling: one observed export sample (Q9, DL-60) writes
  **`extended_calendar:`**. `ext_calendar:` (the Manage Calendars syntax-block spelling) is
  accepted as input leniency. The same sample pins the rest of the export format **[F]**
  (one sample, AE version unpinned, not re-verified): fixed attribute order
  `extended_calendar, description, workday, non_workday, holiday, holcal, cyccal, adjust,
  condition` with **empty-valued keys emitted** (the SEM-36 empty-value convention is the
  export's own habit) and `adjust: 0` always present. Workday as comma day codes plus the
  literal **`all`** (= every day). Condition token case preserved as authored (`{MNTHD#1}`
  next to `workd#1` in one file, no normalization). Condition grouping written with
  **braces** `{…}` where TechDocs shows parens (both accepted as synonyms). The **`WORKD#L`**
  last-ordinal in use (#L = from-end-1, extended uniformly across the ordinal families).
  `holiday: S` carried **without** holcal (against the 12.1 prompt-flow wording; S consumes no
  holiday set, but O/N/W/P keep the requirement). Standard-calendar rows stamped
  `mm/dd/yyyy 00:00:00` (**HH:MM:SS** tails). Cycle records as name/description plus repeated
  `start_date:`/`end_date:` pairs. KB 29387's `autocal_asc -e ALL -E file` is the byte-exact
  re-verification if a live instance becomes available.
- Both calendar kinds are first-class on jobs **[V]**: "You can use a standard calendar or an
  extended calendar as the run calendar" (run_calendar page, 12.0) and identically for
  exclude_calendar, whose page adds the operational wording: "the scheduler inspects the
  calendar before starting the job. If the current date is on the calendar, the job does not
  start and its status changes to INACTIVE... if the job is a box job and its status changes
  to INACTIVE, all the jobs in the box change to INACTIVE."
  *Model note:* the runner narrows exclusion to tick suppression: an excluded day is simply not
  eligible, so no event is emitted. It does not synthesize the vendor's INACTIVE transition or
  the box-member cascade.

### SEM-37 · Extended-calendar date-condition keyword grammar **[V]** (defective tokens **[?]**)
Source: "Date Condition Keywords", byte-identical between the 12.0.01 and 12.1 renders
(diffed). Placeholders, verbatim: "Replace n with a 1-digit number between 1 and 9. Replace
nn with a 2-digit number between 01 and 31. Replace nnn with a 3-digit number between 001 and
365. Replace ddd with one of the following 3-letter abbreviations: mon, tue, wed, thu, fri,
sat, sun. Replace mmm with [jan … dec]." The worked example `Ctue#02` shows that two-digit
forms are zero-padded. "The below list of keywords uses all capital letters; however, the date
condition keywords are not case-sensitive." Zero padding is the documented canonical width, not
a parse requirement: the observed export sample writes `MNTHD#1` and `workd#1`, so unpadded
ordinals are accepted as input and mean the same day. `n`/`nn`/`nnn` are spelling widths; the
accepted range is per family, not global: `ddd` 1–5, `WEEKD`/`WEKRddd` 1–7, `WORKD` / day-of-month
/ `mmm` 1–31, `WEEK`/`CWEEK`/`Cddd` 1–53, `CYCP` 1–30, `CYCL`/`CWRK` 1–365. An ordinal outside its
family's range is a loud refusal.

Token inventory (naming conventions: `#nn` forward ordinal, `Mnn` backward ordinal, `X`
prefix/infix exclusion, `#L` last). The doc page spells `#L` only in the cycle families; an
observed export sample uses `WORKD#L` **[F]** (DL-60), so `#L` (= from-end-1) is accepted
uniformly across every ordinal family below.

| family | include | exclude | meaning |
|---|---|---|---|
| daily | `DAILY` | — | every day (the default) |
| workday-of-month | `WORKDAYS`, `WORKD#nn`, `WORKDMnn`, `FOMWORK`, `EOMWORK` | `XFOMWORK`, `XEOMWORK`, (`WORKDXnn` defective, below) | workdays; nnth workday of month fwd/back; first/last workday of month |
| weekday-of-month | `ddd`, `ddd#n`, `dddMn` | `Xddd#n`, `XdddMn` | every ddd; nth ddd of month fwd/back |
| weekday-of-week | `WEEKDAYS`, `WEEKD#n`, `WEEKDMn`, `FOMWEEK`, `EOMWEEK` | `WEEKDXn`, `XFOMWEEK`, `XEOMWEEK` | Mon–Fri; nth day of week fwd/back; first/last weekday of month |
| week-of-year | `WEEK#nn`, `WEEK#E`, `WEEK#O`, `WEEKMnn` | `WEEKXnn` | nnth/even/odd week of year, back from last |
| day-of-month | `MNTHD#nn`, `MNTHDMnn`, `FOM`, `EOM` | `MNTHDXnn`, `XFOM`, `XEOM` | nnth day of month fwd/back; first/last day of month |
| named month | `mmm`, `mmm#nn`, `mmmMnn` | `Xmmm#nn`, `XmmmMnn` | whole month; nnth day of mmm fwd/back |
| cycle day | `CYCLE`, `CYCL#nnn`, `CYCLMnnn`, `CYCP#nn` | `CYCLXnnn` | any period day; nnnth day of each period fwd/back; nnth period |
| cycle week | `CWEEK#nn`, `CWEEK#E`, `CWEEK#O`, `CWEEK#L`, `CWEEKMnn` | `CWEEKXnn` | nnth/even/odd/last week of each period |
| cycle workday | `CWRK#nnn`, `CWRK#L`, `CWRKMnnn` | `CWRKXnnn`, `CWRKXL` | nnnth/last workday of each period fwd/back |
| cycle weekday | `Cddd#nn`, `Cddd#L`, `CdddMnn` | `XCddd#nn`, `XCdddMnn` | nnth/last occurrence of ddd in each period |

- Week anchoring **[V]**: "All weeks in the year begin on the same weekday as January 1 of
  that year", overridable per keyword via the `WEKRddd` forms (`WEKRddd#nn` / `WEKRdddXnn` /
  `WEKRdddMnn`), for example `WEKRMon#nn` for Monday-anchored weeks (the page's 2014 example).
- `WEEKDAYS` auto-subtracts holidays **[V]**: "The utility automatically excludes all dates
  that are listed in the calendar that you specify in the holiday calendar field."
- Operators: "Use AND when you want to specify only dates that meet both conditions. Use OR
  to specify dates that meet at least one of the conditions." *Model note:* the rule parser
  caps grouping at 100 nested levels (DL-57) and refuses a deeper rule loudly. The only full
  worked condition string in the docs (the federal-holiday example, Define Extended Calendars
  12.0) uses `&`, `|`, and parentheses exclusively, for example
  `(jan&workd#1)|((jan|feb)&mon#3)|(may&monm1)|…`; the literal words `AND`/`OR` are described
  but never demonstrated there **[?]**. (That example string also contains an unbalanced
  parenthesis in the site's own render; quote with care.)
- `NOT` **[V]**: "If you want to exclude dates for which there is no exclusive date condition
  keyword, enter NOT before the inclusive date condition keyword."
- Multiple rules: "To specify multiple rules, use a comma-separated list", and the record
  format repeats `condition:` lines. The boolean combination of list entries/lines is never
  stated → Q8d (default: union/OR).
- **Defective tokens [?]** (doc text provably broken, refused by any implementation until a
  live instance decides): `WORKDXnn` — "excludes the nn th workday of the week", a scope that
  contradicts its month-scoped siblings. `CWEK#n`/`CWEK#L`/`CWEKMn` — garbled render ("the
  n th of weekday of each week in each period"). `CWEKXn` — an X-named token whose text says
  "includes the n th day of each period", which duplicates CYCL# semantics.
- **Folk tokens that do not exist**: `DAY#n`, `MONTH#n`, `YEAR#n`, `CYCLE#L` are attested
  nowhere (there is no generic year-scoped token at all).
- Lineage: the public 4.5 user guide has *no* keyword grammar (GUI-only rule dialog, rules
  "not saved after the calendar has been created"). Public 11.3.6 guides lack the table (the
  Reference Guide is login-walled). Everything here rests on 12.x **[V for 12.x]**.

### SEM-38 · Extended-calendar generation: dispositions and adjust **[V]** core, **[?]** corners
Pipeline **[V]** (Date Condition Keywords): "When you specify date conditions, they represent
additional criteria. The dates that are specified are included in the schedule that an
extended calendar generates only when the dates also meet all the criteria specified at other
criteria prompts. Dates that are excluded by workday or holiday-related criteria are replaced
with alternative dates according to the values specified at the Holiday Action prompt,
Workday Action prompt, or Date Adjustment prompt." So: `condition:`/`cyccal:` produce
candidates → workday/holiday criteria exclude → excluded dates are *replaced* per the action
codes or adjust.

Action codes (identical value set for `non_workday:` and `holiday:`), holiday wording:
- `O` — "Include only holidays that also meet all other criteria" (a category-restrictive
  filter, not a replacement; the non_workday `O` reads the same for non-workdays).
- `S` — include regardless (no transformation).
- `N` — **one-shot, one calendar day, no re-check**: "Excludes the holiday and includes the
  next day. This applies even if the next day is a holiday or non-workday." (Worked example:
  Dec 25 → Dec 26 "even if December 26th is a holiday".)
- `W` — **iterates to validity**: "Excludes the holiday and includes the next non-holiday
  workday." (Worked example walks Dec 25 → 26 (holiday) → 27 (non-workday) → lands Dec 28.)
- `P` — mirror of W, backward (worked example Dec 25 → 24, 23 (non-workdays) → lands Dec 22).

*Model note:* a W/P walk is capped at 366 days (DL-57). A calendar whose walk finds no valid
target inside that bound is a generation error, the degenerate-walk case DL-59 reserves
`CalendarRuleError` for.

The pipeline is **filter-then-replace, single-shot**: O filters restrict the candidate set, and
N/W/P replace excluded candidates with a target day. The two categories are ordered, not
commutative: a specified holiday action runs first and settles every holcal date, so the
non_workday code, O included, never sees one. A weekday holcal date under `holiday: O` plus
`non_workday: O` is therefore kept, where two commuting filters would drop it. A replacement
result is **final**, never re-processed by the other category (holiday-N's "applies even if
the next day is a holiday or non-workday" is the direct evidence). The worked examples exist
only under `holiday:`, and the wordings under `non_workday:` are NOT parallel: W/P say "run on
the next work day" / "previous work day" (a walk to a workday, holiday-ness of the target
unstated), while N says "**Specifies to include the next workday** that also meets all other
criteria", a workday target, not a one-day shift. Pinned as the next non-holiday workday with
the date-conditions not re-checked → Q8c. A date that is simultaneously a holiday and a
non-workday takes the **holiday action** when one is specified (Q8a, DL-58). Source: Define
Extended Calendars 12.1: "When you specify an action at the Holiday Action prompt, the utility
applies that action to all of the dates listed in the [holiday] calendar. When you do not
specify an action at the Holiday Action prompt, the utility treats the dates listed [in that
calendar] as non-workdays according to the value that you specify at the Non-workday Action
prompt." That is an either/or dispatch: with a holiday action present, the non_workday code,
filter or replacement, never sees a holcal date. **[?]** Q8a residue: whether a replacement
target re-enters the other stage stays unverified (kept single-shot per the holiday-N wording;
one 2012 community report of consecutive holidays under holiday-N hints at re-processing in
11.x, Q8c's re-entry corner).

DL-244 draws out a direct consequence of the either/or dispatch quoted above (Define Extended
Calendars 12.1) that the implementation missed: when no holiday action is specified, a holcal
date IS a non-workday for the `non_workday:` code's purposes, regardless of its own weekday --
the effective predicate the non_workday O/N/W/P codes test is weekday-non-workday OR holcal
member. A weekday holcal date under `non_workday: O` is kept outright (O is a filter, never a
replacement, so it behaves exactly like `holiday: S` would); under N/W/P it is replaced, exactly
as a weekend holcal date already was. This is not a new Q8 corner: the governing vendor text is
unambiguous, there was no default to pin, the old weekday-only check was a defect. The companion
keyword follows the same rule: "Specifies that the schedule that is generated by the extended
calendar includes all workdays. The utility automatically considers holidays to be non-workdays
if you specified a holiday calendar at the Holiday Calendar prompt" (Date Condition Keywords,
WORKDAYS) -- WORKDAYS excludes a holcal date exactly like WEEKDAYS already does when a holiday
calendar is named, and is unchanged with no holcal. A consequence: a `holiday:` action named on
a WORKDAYS calendar now never sees that date at all, because WORKDAYS removes it as a candidate
before any disposition runs -- O restricts to a set that no longer contains it, N has nothing
left to one-shot, and S has nothing left to keep; this is not a holiday-action regression, it is
the same "excluded as a candidate" effect WEEKDAYS already had. A weekday holcal date now
reaching the non_workday W/P walk also means more dates meet Q8c's open re-check pin: a walk can
land on another holcal date (for example a Friday holcal date crossing a weekend to the following
Monday) without checking it.

`adjust` **[V]** is an *alternative* to the action codes, not a layer above them: "Specify
this option when none of the non-work action and holiday action options indicate the desired
adjustment." It is a **blind fixed offset with no landing-day re-validation**; the docs'
collision example: "the 14th is a holiday and the 15th and 16th are both non-workdays. The
Holiday Action and Non-workday Action prompts do not provide the option to specify a valid
date adjustment, so the job runs according to the value that you specified at the Date
Adjustment prompt, as long as that value is not -1, 0, or +1". That is, the operator, not
the engine, must pick an offset that avoids excluded days. The offset is **uniform over all
candidates**, not scoped to excluded ones: "The WED#1 date condition indicates the first
Wednesday of the month and the -1 date adjustment sets it to the day before that Wednesday"
(Define Extended Calendars, 12.0): plain Wednesdays, nothing excluded, still shifted. The
attribute definition's "adjustment to apply to holidays and non-workdays" reads as typical
motivation, not a scope restriction, and the worked examples cover both cases. The 12.1 adjust
prompt, "If you do not need to replace the excluded days or if you specified replace days
using non-workday action or holiday action values, enter 0", frames adjust and N/W/P
replacement as alternatives, and whether a nonzero adjust composes with a non-O/S action code
on the same calendar is undocumented → Q8b. KB 280764 gives a vendor-worked nonzero-adjust
with **S** vector (WORKD#1 + adjust 1 + non_workday S = "the day after the first workday",
kept even on a Saturday), which the pipeline reproduces as-is:
`test_sem38_adjust_composes_with_s_the_vendor_worked_example`. The undocumented N/W/P
composition runs on the pinned pipeline order, replace then shift (DL-59; §9 Q8b).

### SEM-39 · Cycles **[V]**
A `cycle:` record is a name, an optional description, and repeated `start_date:`/`end_date:`
`MM/DD/YYYY` pairs, each pair one *period*. Bounds **[V]** (Manage Calendars 12.1): "A cycle
is a list of date ranges or periods. A period is between two and 365 days. A cycle contains a
minimum of two periods and a maximum of 30." (The 12.1.01 WebUI page instead says "You must
add at least one period", a Broadcom-internal contradiction. Accept ≥1 on input, and never
emit a diagnostic that calls one-period cycles illegal.) Both bounds are recorded as vendor
facts, not as loader validation. A cycle record is refused only when a period is unparseable,
ends before it starts, or the record carries no period at all. Period length and period count
are never refused.

Two indexing modes **[V]**: `CYCLE` is union membership, "includes dates that fall within any
of the periods defined in the cycle", while the `#`-forms index **each period independently**:
"CYCL#nnn — includes the nnn th day of each period in the cycle" (likewise CWEEK/CWRK/Cddd
families, SEM-37). "Week of a period" anchors to **consecutive 7-day chunks from each period's
first day** (Q8e, DL-58). Source: a Broadcom community worked example by Broadcom staff, a
quarterly cycle with `condition: CWEEK#01 | CWEEK#02` schedules "the first 14 days in every
quarter": period-start chunks, not calendar weeks. The ragged last chunk is the arithmetic
consequence, not separately worked.

Cycles in the text format are literal and non-recurring; no record attribute repeats them.
The only recurrence control found is a WebUI-only "Repeat every year check box" (12.1.01).
The community "set the year to 1972 to recur" lore is contradicted by that page and attested
nowhere; disregard. Vendor exhaustion mechanics **[C]** (KB 186017 + community corroboration,
no TechDocs page): the engine materializes about a year of dates and "on the last day in the
existing calendar when the job runs autosys will see that there are no future dates and
automatically generate one year worth of new dates". Command-line autocal_asc has no
regeneration verb. Runner note: dsl41's scheduler evaluates eligibility lazily per day
(runner-design §5), which is equivalent to an always-regenerated schedule; the materialization
horizon does not exist for it. Only cycle-bound calendars genuinely exhaust (dormancy per
DL-56).

---
## 5. Attributes with control-flow teeth (quick inventory)

| attribute | semantics class |
|---|---|
| `condition` | predicate algebra (§1) |
| `box_name`, `box_success`, `box_failure`, `box_terminator`, `job_terminator` | container semantics (§2) |
| `date_conditions` + time cluster | scheduling (§4) |
| `max_exit_success`, `success_codes`, `fail_codes` | success-boundary shift (SEM-09); `fail_codes` decides alone, else `success_codes` replaces the rule, else the threshold |
| `term_run_time` | auto-terminate after n minutes → TERMINATED **[V]**: 0 means no limit, the vendor default (DL-241) |
| `n_retrys` | auto-restart on FAILURE only: application failures (vendor's examples: "cannot find a file or a command, permissions are not properly set"). A TERMINATED job "does not restart"; system/network failures restart via the scheduler's `MaxRestartTrys` config parameter instead **[V]** (Q4, DL-53). Retries are not modeled in the oracle or the runner; preflight WARNs on `n_retrys > 0` |
| `auto_hold` | box member enters ON_HOLD automatically when box starts **[C/?]** |
| `auto_delete` | definition lifecycle, not runtime; carried in IR-F `JobIR.passthrough` |
| `status` (on insert) | definition-time out-of-band state (SEM-24) **[V]** existence / **[?]** full value set |
| `job_load`/`priority`/`machine_method`/QUE_WAIT, `machine` lists | the pre-11.3 load-balancing model; the IR carries these in `passthrough` and reads `job_load` and `priority` from there. The oracle honors `job_load` vs machine `max_load` as a capacity bucket and `priority` as QUE_WAIT waiter ordering, lower number first (DL-50). **[V]** TechDocs 24.2 (DL-247): only a positive priority checks machine load. The priority page: "If you do not set the priority attribute or the priority is set to 0, the job is not queued behind other jobs and runs immediately on a machine if resource dependencies permit ... The scheduler ignores any load unit values defined for the job or machine when the job has a priority value of zero." The queueing page: "even when jobs have a priority of 0, AutoSys Workload Automation tracks job loads on each machine so that jobs with non-zero priorities can be queued." So an unset or zero priority skips the load check but its load still counts against the machine for every other job; its `resources:` still gate it. The job_load page: a forced job "runs even if its load exceeds the machine's max_load value"; a FORCE_STARTJOB likewise skips the check, holds its load, and is gated by named resources. The queueing page: "A job in the QUE_WAIT state for one machine attribute value automatically blocks all the lower priority jobs that specify the same machine attribute value. It does not automatically block higher or equal priority jobs that specify the same machine attribute value or a job that specifies a different machine attribute value." The oracle applies that to a fresh start and to the readmission scan, for a waiter whose own load does not fit. A blocked job needs a positive priority on that machine, with or without a `job_load`. DL-50's greedy scan past such a waiter is gone. Open: named-resource blocking, the RESWAIT-after-load corner, a forced job's reuse of resources it holds, unset-priority order among resource waiters (Qr2), pools (Qr3); pool `machine:` lines are typed `MachineMember` rows (DL-49) and preflight resolves their locality; `machine_method`, member selection/routing and per-member `factor`/`max_load` are opaque placement |
| `std_in_file`, `envvars` | CMD exec cluster (DL-32): stdin redirect (may reference a blob) + NAME=value environment list; typed carry on ExecSpec, `$$VAR` sites indexed (SEM-08); real execution refuses `envvars` at preflight outright, and refuses a `$$NAME` site in either field, since global substitution is not implemented (DL-240) |
| `ulimit`, `elevated`, `interactive`, `job_class` | OS/agent-side exec tuning **[V]** (TechDocs 12.x); inert carry (DL-32) |
| `chk_files` | pre-start disk-space gate **[V]**: the agent checks required space; unmet → alarm and the job does NOT start; Resource-Wait class. Opaque carry, no oracle gate (a real disk level is out of a pure simulator's reach; distinct from `resources:`, which the oracle honors as capacity semaphores, DL-50); real execution refuses it at preflight (DL-240) |
| `heartbeat_interval` | MISSING_HEARTBEAT alarm only **[V]**; observability (DL-32) |
| `avg_runtime` | statistics seed at insert **[V]**; inert carry (DL-32) |
| `resources` + `insert_resource`/`update_resource`/`delete_resource` | 11.3+ resource objects **[V]** (TechDocs 12.x): `resources: (name, QUANTITY=n[, FREE=Y\|N\|A]) AND (...)`; FREE: Y=free on success, N=never, A=unconditionally; `res_type: D\|R\|T` (depletable/renewable/threshold), `amount` required, optional agent-level `machine`. Typed carry (DL-21); the oracle honors these as capacity semaphores (DL-50): `amount` is the bucket size, QUANTITY the demand, res_type sets the default release (R free-on-completion / D depletable-never / T level-gate) and FREE overrides it; UCS-09 → UC Virtual Resources |
| `alarm_if_fail`, `alarm_if_terminated`, `min/max_run_alarm`, `send_notification` + `notification_*` family (msg, template, alarm_types, emailaddress[_on_alarm/_on_failure/_on_success/_on_terminated]), `must_*_times` | observability annotations, no control flow (family per 12.x notification services, DL-32) |
| `std_out_file` etc. with `$$VAR` | string substitution sites (SEM-08); real execution refuses an unsubstituted site on an exec field at preflight (DL-240) |
| `watch_file`, `watch_interval`, `watch_file_min_size` (FW jobs) | file-watcher job type: terminal SUCCESS when the file condition is met; a *source* node in derived graphs |

---

## 6. IR implications (decisions this dossier forces)

1. **Two-layer IR.** Layer F (faithful): jobs as attribute records + parsed condition ASTs +
   box tree, semantics exactly per SEM entries. Layer G (derived): dependency graph extracted
   from Layer F, each edge annotated `exact | assumed | redesign` (E/A/R: exact,
   equivalent-under-assumptions, needs-human), with the assumption named (for example,
   "assumes producer and consumer share one schedule cycle, so latching ≙ run-scoped").
2. **Condition AST node set:** `StatusAtom(job, status, lookback?)`,
   `ExitCodeAtom(job, op, value, lookback?)`, `GlobalAtom(name, op, value)`,
   `And`, `Or`, `Paren` (kept for round-trip fidelity, erased in canonical form).
   Both `StatusAtom` and `ExitCodeAtom` hold a `JobRef`, and cross-instance is that ref's
   `instance` field, so either atom kind can be cross-instance.
3. **Status store in the oracle:** one `JobRuntime` row per job, plus a global map. The row
   carries **two clocks**, not one: `status_at` (the last transition of any kind) and
   `last_end_at` (the last transition into a terminal status). A window lookback reads
   `status_at`; zero lookback (SEM-04, Q2a) compares both jobs' `last_end_at`. One timestamp
   would get SEM-04 wrong after a non-terminal status change. The row also carries
   `exit_code`, `run_number`, the SEM-20/21/22 flags and the SEM-32 arm. Box status = derived
   fold with SEM-11/12 gating rules.
4. **on_ice/on_hold/on_noexec are events in the oracle**, and *rewrites* in static analysis.
5. **Success is per-job** (`max_exit_success`, `success_codes`, `fail_codes`; SEM-09's cited
   precedence). Never hardcode exit 0.
6. §5's non-control-flow attributes take four IR-F lanes, not one. Observability attributes
   land in `JobIR.annotations` and known-inert attributes in `JobIR.passthrough`, both as
   whitespace-trimmed semantic text. `must_start_times`/`must_complete_times` lower to a typed
   `SlaSpec`, and `resources:` to typed `ResourceRef` rows: normalized values, not source
   text. Time attributes without truthy `date_conditions` join `passthrough` as dead
   configuration (SEM-30). Nothing is dropped on any lane; byte-exact text is the AST's job,
   not IR-F's.

## 7. Migration risk register (seed; completed in the stonebranch mapping table)

| # | AutoSys behavior | Risk when mapping to run-scoped DAG (Stonebranch) |
|---|---|---|
| R1 | Latching conditions, no lookback (SEM-01) | Stale success satisfies dependency across days. Naive edge translation *tightens* semantics: it can block flows that relied on latching, or the reverse, where the AutoSys flow relied on staleness as a feature |
| R2 | Lookback windows (SEM-04) | No native equivalent. Needs time-window guard tasks or acceptance of changed semantics |
| R3 | on_ice downstream-satisfied (SEM-20) | For each edge type, make sure that "skipped counts as satisfied" holds in Stonebranch skip semantics |
| R4 | Box success gating on external refs (SEM-12) | The hung-RUNNING pattern has no analog; redesign it, do not translate it |
| R5 | run_window closer-edge rule (SEM-33) | Behavioral cliff at the window midpoint. No analog |
| R6 | local unqualified `n()` atoms in `condition` | These are *not* dependencies. Translation as edges creates false ordering; map them to resource/mutex constructs. Only the bare local form reads as mutual exclusion: a lookback-qualified, cross-instance, or `box_success`/`box_failure` `n()` stays an ordinary edge (DL-12) |
| R7 | Global-variable conditions (SEM-08) | Needs a UC variable + event-trigger equivalent. The re-evaluation-on-set semantics must match |
| R8 | FORCE start satisfying downstream latches (SEM-23) | Operators' habits change: a force start feeds downstream latches |

## 8. Trace test index (oracle regression set, one per SEM unless noted)

T01 latching across days (SEM-01) · T02 each atom type truth table (SEM-02) · T03 precedence
pinning (SEM-03: pinned at parse time by the `test_sem03_*` tests, DL-53; the oracle layer
has no precedence concept of its own) · T04a/b/c lookback window in/out/9999 (SEM-04) ·
T05 iced predecessor in lookback (SEM-05) · T06 undefined job never fires (SEM-06) ·
T08 SET_GLOBAL triggers re-eval (SEM-08) · T09 max_exit_success boundary, T09b fail_codes
decide alone (unlisted → SUCCESS), T09c success_codes replacement, T09d success_codes
ignored beside fail_codes (SEM-09, DL-58 cited composition) ·
T10 unconditioned member starts with box (SEM-10), T10 box-cycle reset: a second box run
starts only the head of a chain, nested, held and ON_NOEXEC variants (SEM-10, DL-242:
`test_sem10_second_box_run_*`) · T11 default box fold (SEM-11), T11 operator INACTIVE on a
member completes the box and a waiting member still hangs it (SEM-11, DL-242:
`test_sem11_member_set_inactive_*`, `test_sem11_failed_member_set_inactive_*`,
`test_sem11_waiting_member_*`) ·
T12a internal box_success early-exit, T12b external box_success hung-RUNNING,
T12c box_success over a grandchild fires transitively (SEM-12) ·
T13 sticky TERMINATED box (SEM-13) · T14 terminator cascade both directions (SEM-14) ·
T15 idle box ignores INACTIVE members, the single-member table (SEM-15, DL-242:
`test_sem15_*`) · T18 box INACTIVE cascades to every contained job (SEM-18, DL-242:
`test_sem18_*`) ·
T20a ice downstream fires, T20b off-ice does not immediately run (SEM-20) ·
ordinary vs lookback atoms on a non-live iced job follow different tables, DL-243
(SEM-20/SEM-05, Q10 residue: `test_sem20_ordinary_atoms_on_an_iced_job_follow_the_vendor_table`,
`test_sem20_lookback_atoms_on_an_iced_job_stay_true`,
`test_sem20_ordinary_atom_on_an_undefined_iced_lookalike_stays_false`,
`test_sem20_ordinary_atom_on_a_live_iced_job_reads_the_real_in_flight_status`,
`test_sem20_off_ice_later_reads_the_real_status_not_the_vendor_table`) ·
T21a hold blocks downstream, T21b off-hold immediate run (SEM-21) · T22 noexec bypass,
T22b an ON_NOEXEC box goes RUNNING and every member bypasses (SEM-22) ·
a completed FAILURE/TERMINATED job put ON_NOEXEC is moved to INACTIVE (exit code cleared)
through DL-242's operator-INACTIVE path, DL-243 (SEM-22:
`test_sem22_noexec_on_a_failed_job_transitions_to_inactive`,
`test_sem22_noexec_on_a_terminated_job_transitions_to_inactive`,
`test_sem22_noexec_keeps_a_success_visible`,
`test_sem22_noexec_off_noexec_then_release_a_held_f_consumer_stays_blocked`,
`test_sem22_noexec_while_running_then_real_failure_is_not_hidden`,
`test_sem22_noexec_on_a_failed_box_member_completes_the_box`) ·
T23 force start satisfies latch (SEM-23) ·
FORCE_STARTJOB on a non-live ON_ICE/ON_HOLD job clears the flag and runs, DL-243 (SEM-23:
`test_sem23_force_start_clears_ice_and_runs`, `test_sem23_force_start_clears_hold_and_runs`,
`test_sem23_force_start_on_a_live_job_is_still_refused`,
`test_sem23_after_force_clears_ice_a_later_plain_start_needs_no_off_event`,
`test_sem23_force_start_clears_ice_even_when_run_window_then_refuses`) ·
T24a initial ON_HOLD blocks then OFF_HOLD releases, T24b initial ON_ICE satisfies downstream
(SEM-24) · T04 zero-lookback since-last-end anchor pinned both directions + Q2b first-run
corner, both cited (SEM-04, DL-54/DL-58: `test_sem04_zero_lookback_*`) · T32 arm-and-wait:
tick arms, edge starts, start consumes (SEM-32, DL-54/DL-58: `test_sem32_*`) · T33a/b
run_window closer-edge both sides + box variants (incl. the DL-154 skip bypass and the SEM-11
carve-out contrast; since DL-242 a rerun member's INACTIVE edge comes from the box-start
reset, not the skip: `test_sem33_box_skip_on_a_rerun_member_follows_the_box_start_reset`,
`test_sem33_box_skip_after_the_box_start_reset_leaves_downstream_atoms_false`), T33 the
box-start disposition: the vendor's Box1 example at 04:05 and 16:05, an in-window box start,
a held member, a subbox member, both catalog orders, a deferral beside a deadline timer, a
stale deferral after a box restart (and after a rebaseline moves the member, in the
classification suite), RUNNING wakes in the same pass, a pass bound to its box run, and the
provisional deferred-start rule; T33b a standalone
skip moves a prior result to INACTIVE and does not cascade from a box (SEM-33, DL-246:
`test_sem33_vendor_box1_*`, `test_sem33_box_start_*`, `test_sem33_subbox_skip_*`,
`test_sem33_deferral_from_*`, `test_sem33_running_wakes_*`, `test_sem33_a_window_pass_*`,
`test_sem33_deferred_box_start_*`, `test_sem33_standalone_*`,
`test_sem33_force_start_on_a_held_standalone_*`),
T33c the window read in the job's timezone (SEM-33, with SEM-35) ·
T34a/b must_* emit alarms only, T34c each start_time arms its own relative offset, T34
relative must_complete anchored to the tick's slot (SEM-34, DL-248: `test_sem34_must_complete_*`).

Layer note: not every SEM entry lands in the oracle suite. SEM-07 (cross-instance atoms) is
pinned by the condition, derive and control-plane suites, not by an oracle trace. SEM-15's
oracle tests are listed as T15 above. SEM-30 and
SEM-31 are lowering rules, pinned in the IR suite (`test_sem30_*`, `test_sem31_*`). SEM-35 is
pinned by the scheduler suite's `test_resolve_timezone_*` and `test_preflight_timezone_*`
families, which carry no `sem35` in their names, and by T33c in the oracle suite for the
re-basing of `run_window`. SEM-36..39 are calendar rules, pinned in the autocal suite
(`test_sem36_*`..`test_sem39_*`). T34a/b's own `test_sem34a/b_*` cover must_complete only; the
must_start half is pinned by `test_must_start_alarm_fires_when_no_run_began_by_deadline` and
`test_must_start_alarm_quiet_when_the_run_began_in_time`. SEM-16 (definition-time mutation of a
running box) and SEM-17's ACTIVATED label are oracle non-goals and have no trace test. The
label's state effect is pinned under SEM-10 and SEM-11 (DL-242).

## 9. Open questions and their pinned defaults

This section is the register of the Q series. A closed question names the entry that holds
its rule. An open question runs on a documented default, marked `# PENDING: Qn` in the code
where the code holds a provisional default; [docs/live-instance-runbook.md](live-instance-runbook.md)
holds the probe that would settle it.

- Q1 (SEM-03): closed (DL-53). Flat left-to-right evaluation; the rule and its citation are
  in SEM-03, pinned by the `test_sem03_*` tests.
- Q2 (SEM-04): split into Q2a and Q2b, both closed. Q2a (DL-54): lookback `0` anchors to the
  dependent job's own last end; `test_sem04_zero_lookback_*` pin the anchor in both
  directions. Q2b (DL-58): a dependent that never ended has no anchor and the qualifier is
  satisfied; `test_sem04_zero_lookback_first_run_*` keep the pin. Citations in SEM-04.
- Q3 (SEM-32): closed (DL-54, DL-58). Arm-and-wait with no expiry; the rule, its citations
  and its pins are in SEM-32. Two residues stay open:
  - **Q3c** (DL-58): does a member's latch survive into the next box run? Thread 801986's
    aside says it may. The pin is box-run scope (`SCHED_DISARM`; `# PENDING: Q3c`,
    oracle.py). One live box test decides it.
  - **Q3d** (DL-69): does ON_ICE discard a latched tick? The pin is that a pre-existing arm
    survives ice (`# PENDING: Q3d`, oracle.py). The runbook's discriminator decides it.
- Q4 (§5 `n_retrys`): closed (DL-53). FAILURE only. TechDocs 12.0.01 n_retrys: "specifies
  how many times to attempt to restart the job after it exits with a FAILURE status. If a job
  exits with a TERMINATED status, it does not restart."; "This attribute applies to
  application failures (for example, AutoSys Workload Automation cannot find a file or a
  command, permissions are not properly set, and so on). It does not apply to system or
  network failures", which restart under the scheduler's `MaxRestartTrys` configuration
  parameter (12.1 MaxRestartTrys page: "governs retries due to system or network problems ...
  different from the n_retrys job definition attribute"). Retries are out of scope for the
  oracle and the runner; preflight WARNs on `n_retrys > 0`.
- Q5 (do queued events survive a scheduler restart): closed (DL-53), yes, by architectural
  entailment; no single "survives restart" sentence exists in TechDocs, and the inference is
  deliberate. `ujo_event` "Records events that the scheduler has not yet processed" (Events
  reference, 12.1); "The event server (database) stores all the objects", and on start the
  scheduler "continually scans the database for events to process" (Architecture, 12.1).
  There is no in-memory-only event queue to lose. KB 11013 corroborates operationally: queued
  events run after a multi-hour outage on a plain restart. The oracle's event-queue model
  needs no change.
- Q6 (SEM-12): open, narrowed (DL-58). The question: box_success referencing a member that
  is ON_ICE; does the "not scheduled" clause apply ("condition not met if the specified job
  is not scheduled")? The `condition:`-atom half is cited (KB 438836: a local ON_ICE
  predecessor makes the atom continuously true with lookback ignored, the SEM-05 rule), and
  KB 92872 adds the evaluation-trigger nuance: a box_success over global-only terms is
  re-evaluated at member completion moments, not on SET_GLOBAL, consistent with SEM-12's
  gating. The box_success-referencing-an-iced-MEMBER case itself is uncited. The
  shared-evaluator pin (atom true) stands. Q6 has no code switch.
- Q7 (SEM-09): closed (DL-58). KB 408778 states the composition; the rule is in SEM-09 and
  `ir.exit_is_success` implements it, shared with the UC twin (M31). Pinned by
  `test_dl33_exit_is_success_*` and `test_sem09*`.
- Q8 (SEM-37/38/39, DL-57): extended-calendar generation corners. Every Q8 item closes
  mechanically once any live instance exists: define the calendar, let autocal materialize
  the schedule, and diff the vendor date set against `dsl41`'s generator, an afternoon for the
  whole batch.
  - **Q8a**: closed (DL-58). A specified holiday action governs every holcal date outright;
    holcal dates receive non-workday treatment only "when you do not specify an action at
    the Holiday Action prompt" (Define Extended Calendars 12.1, quoted in SEM-38). **[?]**
    residue: whether a replacement target re-enters the other stage (folded into Q8c's
    re-entry corner).
  - **Q8b**: open, pinned default (DL-59). Nonzero `adjust` composed with an N/W/P
    replacement code is undocumented; the docs frame adjust as the alternative (the 12.1
    prompt: "enter 0 … if you specified replace days using non-workday action or holiday
    action values"). The default is the SEM-38 pipeline order as-is: disposition replaces
    first, then the uniform blind adjust shifts every survivor (**replace-then-shift**; probe
    signature (Aug 15, Aug 18) pinned in `test_q8b_*`). `adjust: 0` alongside any action is
    inert and accepted. Nonzero adjust with **S** is vendor-worked (KB 280764) and
    reproduced as-is. `# PENDING: Q8b`; the runbook's probe pair decides it.
  - **Q8c**: open, pinned default. The `non_workday:` replacement targets: N is documented as
    "include the next workday that also meets all other criteria", pinned as the next
    **non-holiday workday**, with "all other criteria" read as the workday/holiday prompts,
    NOT the date-conditions (`# PENDING: Q8c`). W/P walk to a workday without a re-check of
    the target's holiday-ness (their worked examples exist only under `holiday:`). The O
    filters are verified (thread 778062: an autocal preview under non_workday O excluded the
    workday dates, with CA confirming "Behavior is not changed … production documentation
    was wrong"; the "workdays only" rendering of O's text on some pages is the known-bad
    one). N's doc text differs across eras (11.3.5: "next day, even when also a non-workday";
    12.1: "next workday…", the implemented pin). A 2012 community report (thread 825395:
    consecutive holidays under holiday-N, the second holiday missing from the output) hints
    that the holiday-N target can be re-processed in 11.x; that cross-stage re-entry corner,
    shared with Q8a's residue, stays open against the current-doc single-shot pin.
  - **Q8d**: open, pinned defaults (DL-57; (iv) DL-59). Rule combination beyond the documented
    `&`/`|`/parens: (i) comma-separated list entries / repeated `condition:` lines, boolean
    semantics unstated; default: inclusive rules union, exclusive (`X`/`NOT`) rules subtract
    from that union. (ii) unparenthesized `&` vs `|` precedence inside one expression,
    unstated; default flat left-to-right (the SEM-03 house style, an analogy not a
    citation). (iii) the literal words `AND`/`OR`: accepted as synonyms of `&`/`|`; an estate
    calendar demonstrates the literal word (`condition: **/04/** AND workd#04`, KB 442457;
    the same KB's next-run narrative reads that mask as APRIL, which a mm/dd field order
    cannot produce, so the mask-field reading in that estate is under-determined, and the
    AND acceptance is the only part taken). (iv) a compound rule with NO inclusive leaf (for
    example `xtue|xwed`), neither a recognized exclusion form nor a likely authorial intent;
    default: literal boolean evaluation as an include, the same complement algebra that
    mixed-polarity rules already use, which accepts that `xtue|xwed` reads near-universal
    (`# PENDING: Q8d`, pinned in `test_q8d_*`). An empty or whitespace `condition:` value
    reads as absent (the SEM-36 empty-value convention) and falls back to the DAILY default.
  - **Q8e**: closed (DL-58). CWEEK anchors to consecutive 7-day chunks from each period's
    first day (SEM-39). CWRK is the nth workday of a period, a count, not a week selector.
    The ragged last chunk is the arithmetic consequence, not a new question.
  - The defective tokens (SEM-37: `WORKDXnn`, `CWEK#n`/`#L`/`Mn`/`Xn`) are refused outright
    with no default and no switch. There is nothing to implement until the doc text is fixed
    or a live instance defines them.
- Q9 (SEM-36, DL-57): closed (DL-60) from one observed `autocal_asc` export sample. The
  verdicts are folded into SEM-36/37 at **[F]**: the export writes `extended_calendar:`
  (`ext_calendar:` accepted); fixed attribute order with empty-valued keys emitted; comma
  day codes plus `workday: all`; braces as condition grouping; case preserved as authored;
  `WORKD#L` in use; `holiday: S` without holcal; `mm/dd/yyyy 00:00:00` standard rows;
  repeated-pair cycle records. A synthetic clone of the observed shapes is pinned end to end
  (`test_q9_*`, including byte-identical F1). Caveats: one sample, AE version unpinned, not
  re-verified, the weakest evidence tier in this dossier. KB 29387's
  `autocal_asc -e ALL -E file` is the byte-exact re-verification if a live instance becomes
  available; parens are accepted alongside braces, so no behavior rides on the grouping read.
- Q10 (SEM-05/SEM-20, DL-243): open, pinned default. The vendor's ON_ICE truth table ("Start
  Conditions", AutoSys Workload Automation 24.2) covers an ORDINARY downstream atom; it does
  not separately address a LOOKBACK-qualified atom against an iced predecessor. The default
  keeps the pre-DL-243 reading for that corner: every atom kind true, lookback ignored (the
  DL-13 blanket pin, SEM-05), rather than extending the narrower ordinary-atom table to a
  lookback-qualified atom. `# PENDING: Q10` marks the branch in `_atom_true`. A live instance
  icing a predecessor referenced by both an ordinary and a lookback-qualified atom on the same
  consumer job would settle it.

## Sources
Primary: Broadcom TechDocs, AutoSys Workload Automation 12.0/12.0.01/12.1/12.1.01 (Basic Box
Job Concepts also 24.2, same box-cycle wording: SEM-10, SEM-11, SEM-15, SEM-18; the
run_window page also 24.2, same wording: SEM-33; the 24.2 `priority` and `job_load`
attribute pages and How AutoSys Workload Automation Queues Jobs: the §5 load-balancing row,
DL-247; the 24.2 `must_complete_times` page and How Must Start Times and Must Complete Times
Work: SEM-34, DL-248): JIL
reference pages (`condition`, `box_success`, `box_failure`, `run_window`, `start_mins`,
`must_complete_times`, `date_conditions`, `n_retrys`), Scheduling guides (Basic Box Job
Concepts, Box Job Completion State, Must Start/Complete Times, Manage Common Job Properties,
Start Conditions, Job States: the Q2a/Q3/SEM-21 quotes), the monitoring guide's Manage Job
Events pages (off-hold/STARTJOB event semantics, 12.1.01), administration pages
(`MaxRestartTrys`, `KillSignals`), system-states reference (Events), Getting Started (AutoSys
Architecture), Broadcom KB 186248 (global variables), KB 11013 (scheduler-outage event
recovery, Q5 corroboration). Calendar entries (SEM-36..39, DL-57): Manage Calendars
(12.0.01/12.1), Date Condition Keywords (12.0.01/12.1, byte-identical), Define Extended
Calendars (12.0 scheduling guide, the federal-holiday worked example; 12.1.01 menu variant),
autocal_asc Command (12.1 reference), Define Standard Calendars (12.1.01), Define a Cycle
WebUI (12.1.01), run_calendar/exclude_calendar attribute pages (12.0), KB 186017 (calendar
regeneration), KB 142758 (bi-weekly extended calendar). The DL-58 citation sweep: KB 408778
(Q7 exit-code precedence), KB 438836 (SEM-05 ON_ICE-lookback citation + cross-instance
caveat), KB 92872 (box_success evaluation trigger), KB 280764 (adjust+S worked vector),
KB 442457 (literal AND in an estate calendar; CAUAJM_W_10119/10120 exhausted-calendar
warnings), KB 29387 (autocal_asc export/import commands), KB 14195 (ujo_calendar
materialization, 365 days), KB 135770 (job_depends via the Application Server); community
threads 760251 (Q2b, CA support), 734033 (Q3a/E11, CA's Mark Hanson worked examples), 801986
(Q3b no-expiry + the Q3c box aside, Broadcom staff), 778062 (non_workday-O preview +
"documentation was wrong" correction), 825395 (2012 consecutive-holiday-N community report),
and the CWEEK quarterly worked example (Q8e, Broadcom staff). Secondary corroboration: legacy
CA User Guide excerpts and practitioner references (on_hold/on_ice operational behavior,
state definitions); the public 4.5 user guide and 11.3.6 user/admin guides were full-text
checked and contain no keyword grammar (SEM-37 lineage note).
