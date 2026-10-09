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

*Model note (DL-304):* the oracle runs one input's consequences inside that input, so a
re-trigger loop that closes in one instant -- a job whose own completion satisfies its
condition again, or a cycle of such jobs (L010's pattern) -- would recurse without end. A
start of a job may nest inside its own start's cascade once (a run or an ON_NOEXEC bypass
each): a box start's window pass can complete run one and its wakes start run two (DL-246).
A start nested inside two starts of the same job is refused with one `START_REFUSED` line
naming the loop, and nothing re-wakes the job until a status moves. Starts that follow one
another, such as a job that several predecessors start in turn inside one input, are never
refused.

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
Atom keywords are accepted in upper or lower case, and the one-letter abbreviations are the
canonical short forms. Job and global names are matched exactly: the parser preserves their case
and the status store keys on the name as written. The vendor forbids mixed case: "You can use
uppercase or lowercase characters to define conditions. You cannot mix case." ("condition
Attribute", AutoSys 24.2). dsl41 accepts mixed case in both senses: within one keyword, such
as `sUCCESS` or `AnD`, and across the keywords of one condition, such as
`SUCCESS(a) and failure(b)`. The second is the likelier reading of the vendor sentence. Both
inputs are accepted but undocumented.
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
- The largest finite lookback is `9998.59`, about 416.58 days. **[V]** Source: "condition
  Attribute", AutoSys 24.2: "0-9998 for hhhh when specifying hours and minutes (for example,
  9998.59)". A bare `9999` means indefinite. dsl41 also accepts `9999.00` to `9999.59`, and the
  same values in the `9999\:mm` spelling, as finite windows. That input is accepted but
  undocumented.

### SEM-05 · ON_ICE predecessors inside lookback conditions **[V]**
If the predecessor job referenced in a lookback condition is currently ON_ICE, the atom
evaluates **true** and the scheduler ignores the lookback entirely. (Interacts with SEM-20.)
The rule is blanket over atom kinds (DL-13): `f()`, `t()`, `d()` and `e()` on an iced
predecessor are all true, not only `s()`. This blanket reading is scoped to a LOOKBACK-
qualified atom (`atom.lookback is not None`, any kind, the zero form included); an ORDINARY
atom (no lookback qualifier at all) on the same iced predecessor follows SEM-20's narrower
vendor truth table instead (DL-243). ON_ICE sent to a STARTING or RUNNING job is ignored
(SEM-20, DL-254), so an iced job is never live and its atoms always read the ice rules.
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
- Set via `sendevent -E SET_GLOBAL -G NAME=value`. The condition page says global variables
  are "set using the sendevent command" ("condition Attribute", AutoSys 24.2).
- `insert_global` **[?]**: dsl41 also lowers an `insert_global: NAME` statement with a `value:`
  attribute, and the value seeds the global store. No vendor page documents an
  `insert_global` or `delete_global` JIL subcommand. The statement stays a dsl41 input form
  with its current behavior until vendor evidence settles it.
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
Support limit: lowering refuses a negative exit code in either list. The vendor allows one: "The
exit code must be an integer in the range -2147483647 to 2147483647" ("success_codes Attribute"
and "fail_codes Attribute", AutoSys 24.2). dsl41 refuses it because it can never match: on
POSIX a process exit status is 0-255, and the runner records a job killed by a signal as
TERMINATED with no exit code. The refusal names the negative code and gives this reason
(`ir._Lowerer._code_set_attr`, DL-252).

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
Three carve-outs to the literal fold take a member out of it. Each is a
completion moment and runs the full completion door: overrides first, then the default fold.
The first two resolve the member as INACTIVE.
- A run_window skip inside a live box run is an explicit INACTIVE verdict (DL-13, DL-154),
  including the one a box start decides (DL-246). The member leaves the run and casts no vote in the fold **[C]** (mechanism tier; SEM-33's vendor
  quotes pin the verdict and the completion, not the bookkeeping).
- An operator's INACTIVE on a member of a RUNNING box counts as SUCCESS (DL-242). **[V]**
  TechDocs 24.2 and 12.x, Basic Box Job Concepts: "Using the sendevent command to change the
  state of a job in a box to INACTIVE affects the box's completion status as if the INACTIVE
  job returned a status of SUCCESS." This holds for a member that ran, one that failed, and
  one that still waited. DL-235 still holds for a launched run: no kill.
- An ON_ICE on a member that has not run in this box run (DL-285). SEM-20 **[V]** removes an
  iced job from all conditions and logic, and SEM-11 **[V]** lets a box complete once every
  member ran or was bypassed, so the fold skips the iced member once it is out of the run:
  its own status is not live or QUE_WAIT, and no job it contains is (DL-304). Basic Box Job
  Concepts keeps a box RUNNING "as long as there are jobs in it with ACTIVATED or RUNNING
  status" **[V]**, so a member iced while it, or a job inside it, still runs or waits in the
  queue keeps the box waiting, and an ice on it is no completion moment. When that job stops
  being live or queued, the iced member is out, and its completion moment runs then; the
  job's result casts no vote. The ice moves no status, so
  the check runs on the event itself; an ice on a queued member settles it INACTIVE (DL-50),
  and that transition carries the check. That the check runs at the ice, rather than at the
  next member transition, is **[C]**: it composes the two vendor rules, and no vendor text
  states the moment. An override that reads the iced member reads it as satisfied, which is
  Q6's pin. A member that ran keeps its vote, so an ice on it is no completion moment;
  neither is an ice on a member already iced or resolved. A box that is not RUNNING
  re-derives nothing, since SEM-15 reads members' statuses.

A fourth way out is no completion moment of its own. A member taken off ice while the box
runs, before it ran in that run, stays out of the fold until the box's next run (SEM-20's box
clause, under the default `off-ice-in-running-box=next-run`). The ice already took it out, so
the OFF_ICE changes nothing the fold reads, and the box completes without it.

Under the default `box-start-all-members-out=complete`, a box start whose pass leaves no
direct member in the run is a completion moment (DL-304): every direct member is on ice and
out of the run as defined above, or the box has no members. **[C]** No vendor sentence names the
case. Basic Box Job Concepts (AutoSys 12.0 and 24.2) keeps a box RUNNING "as long as there
are jobs in it with ACTIVATED or RUNNING status", and an iced job is not executed "for the
entire run of the box" (Events, JOB_ON_ICE, 12.0 and 24.2), so neither sentence keeps such a
box running. The door runs once, after the start's window decisions (DL-246), for each box
run the start began, a subbox before its parent: overrides first, an external reference
included, then the default fold. With no member that ran, the fold is SUCCESS, the vacuous
vote SEM-15 gives an idle box whose members are all INACTIVE; the trace names the start
("default box fold at box start: no member in the run"). A subbox that completes this way
ends by its own terminal transition, which is a member transition of its parent. A start
that leaves some member in the run, one that is not iced, is no completion moment: the
box completes at a later member transition, as before. The moment resolves no member, so
it is not a carve-out. Under the default `box-start-all-members-out=complete` the box
completes at its start; `wait` keeps it RUNNING until an operator acts, as dsl41 did before
at a box start (runner-design §8a). The out-of-the-run rule above applies under both values.
Which one AutoSys does is open (Q15, section 9).

The box row records the first two kinds in `window_skipped_members`; the member's own later
start voids its mark. An iced member carries its own flag. A member taken off ice in the run
carries a mark in `iced_out_members`; a forced start of it voids the mark, and the forced run
votes. While a forced start of it is queued or live, the fold waits for it. A resolved member stays settled if
an operator later gives it a status that is not live; the fold still votes over members that
ran. A resolved or iced member is a completion moment
for every running ancestor's overrides too, since "inside" is transitive (SEM-12). A member
whose condition never fires inside the run, and that nobody resolves or ices, still hangs the box:
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
- Under the default `box-start-all-members-out=complete`, a box start that leaves no member in
  the run is a completion moment (SEM-11, Q15, DL-304), so it evaluates an external-reference
  override too: a met one fires at the start, an unmet one keeps the box RUNNING. That the
  start counts is **[C]**, as DL-285's ice moment is.
- The walk from a job's change up to the boxes above it is bound to each box's run. **[C]**
  The run numbers are read before the moment's first box rule. If the moment's own cascade
  completes a box above and starts it again, the walk skips that box: "Jobs in a box run only
  once for each box execution" (Basic Box Job Concepts, AutoSys 12.0 and 24.2), so the job's
  change belongs to the earlier run and is no moment of the later one. The walk goes on to the
  boxes above whose run did not move; they still evaluate their overrides. The same binding
  holds for the walk after an ice (DL-285) or a window skip (DL-154), and an ice's walk does not
  run at all when its own box was started again. An INACTIVE cascade (SEM-18) is one moment:
  each row's runs are read after every row is written and before any row's box rules run.

### SEM-13 · Box TERMINATED is sticky **[V]**
A box moved to TERMINATED (for example, KILLJOB) stays TERMINATED regardless of later member state
changes, until the next box start.

### SEM-14 · box_terminator / job_terminator **[V/C]**
Control flow, not alarms:
- `box_terminator: 1` on a member: if this member ends FAILURE or TERMINATED, terminate the
  containing box. **[V]** "Force the Job or the Box to Stop Running" (AutoSys 12.1 and 24.2,
  the same text): "This attribute specifies that if the job completes with a FAILURE or
  TERMINATED status, the box terminates." The reference page "box_terminator -- Terminate Box
  on Job Failure" (AutoSys 24.2) gives the mechanism: "The scheduler sends a KILLJOB event to
  terminate the parent box." Its value line names FAILURE only: "Instructs the scheduler to
  terminate the parent box when the job ends in FAILURE." Its own example "terminates the
  containing Box job if the job you are defining fails or is terminated". The default follows
  the scheduling guide and the example: a TERMINATED end counts. That covers a KILLJOB on a
  running member, an injected TERMINATED, and a KILLJOB on a queued member, which DL-50
  dequeues to TERMINATED. A `term_run_time` kill and a subbox with box_terminator that ends
  TERMINATED count the same. The `box-terminator-on-terminated` semantic switch keeps dsl41's
  earlier reading, FAILURE only, as `false` (runner-design §8a).
- `job_terminator: 1` on a member: if the containing box ends FAILURE or TERMINATED,
  terminate this member. **[V]** "job_terminator Attribute -- Kill a Job if Its Box Fails"
  (AutoSys 24.2): "The job_terminator attribute specifies whether to use a KILLJOB event to
  terminate the job if its containing box job completes with a FAILURE or TERMINATED status."
Members killed this way end with status TERMINATED (this matters for `d()`/`t()` consumers).
A box_terminator member killed by its own box's job_terminator cascade does not end the box
again: the box has already ended. Whether an operator's CHANGE_STATUS FAILURE or TERMINATED on
a RUNNING box kills its job_terminator members is open (Q14, section 9); the oracle kills
none.

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

A member that is not iced votes. **[V]** Basic Box Job Concepts (AutoSys 12.0 and 24.2): "If a
box that is not running contains a job that changes status because of a FORCE_STARTJOB or
CHANGE_STATUS event, the new job status could change the status of its containing box. A
status change for the box could then trigger the start of downstream jobs that depend on the
box." So a completed box flips when such a member fails later, and its failure consumers start
beside the success consumers that already ran.

Under the default `idle-box-iced-member=ignore`, an iced member that is out of the run is
ignored in the re-evaluation, as an INACTIVE member is. **[C]** "Out of the run" is SEM-11's
test: on ice, and neither it nor any job inside it is live or queued (DL-304). The page's table
does not name ON_ICE, and no vendor sentence says whether an iced member votes here. Job
States (AutoSys 12.0 and 24.2) says an ON_ICE job "is removed from the job stream but is still
defined", SEM-20 removes it from all conditions and logic, and SEM-11's fold already skips it.
So a completed box does not flip when a job inside an iced subbox, or an iced member given a
status by CHANGE_STATUS, ends later. Nor is such a member's own change a verdict: when the
member that changed is out of the run and no other member votes, the box keeps its status, so
a box that never ran stays INACTIVE. When the member that changed is in the run, DL-242's
vacuous SUCCESS stands even if every other member is iced: an operator's INACTIVE on the last
member in the run "returns a SUCCESS status as it ignores all the jobs that are in INACTIVE
status" **[V]**, and an iced member is ignored the same way. An iced member that is live, or
holds a live or queued job, is not out of the run, so it is not dropped: its own status votes
as any member's does. A live status blocks the re-evaluation, and an INACTIVE one is ignored.
A forced start clears the ice (SEM-23), and so does OFF_ICE, so such a member votes. `vote`
reads an iced member's status, as dsl41 did before (runner-design §8a). Which one AutoSys does
is open (Q16, section 9).

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
An iced subbox never starts, so its parent completes without it once nothing inside it is
live or queued, and under the default `box-start-all-members-out=complete` a parent whose
only members are iced completes at its start (SEM-11).
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

CHANGE_STATUS RUNNING on a box is open (Q13, section 9): the oracle writes the status and
nothing else.

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
  ordinary atom; s()/d()/n() are unaffected (both readings already say true). For a
  lookback-qualified atom the "condition Attribute" page (AutoSys 24.2) is explicit: "If the
  predecessor job being evaluated for the look-back condition is currently in an ON_ICE
  status, it always evaluates to true. That is, any look-back evaluation is ignored." The
  Start Conditions table does not separate lookback atoms, so the two pages read
  f()/t()/exitcode differently for that atom; which one a live instance follows is open
  (Q10, section 9). The `ice-lookback` semantic switch selects the table's reading (DL-252,
  runner-design §8a).
  Inside a box, a member that depends on an iced sibling starts immediately when the box
  runs (an ordinary s() atom, true under both tables). **[V]**
- OFF_ICE: the job does **not** run even if its starting conditions currently hold. It waits
  for conditions to *reoccur*. **[V]**
- OFF_ICE on a member of a RUNNING box that has not run in that run: the member sits the run
  out. **[V]** "Start Conditions" (AutoSys 24.2 and 12.0, the same sentence): "If a job is
  contained in a running box when it is taken off ice, the scheduler does not restart the job
  until the following run of the box, even if its starting conditions recur during the
  existing run of the box." "Job States" (AutoSys 24.2): "Jobs that are contained in running
  boxes start the next time their starting conditions recur and a new run of their containing
  box begins." The member stays out of the fold, so the box completes without it (SEM-11): the
  "following run of the box" cannot begin unless the current one ends. That holds for the
  default fold. A `box_success` or `box_failure` that names the member reads it as any
  override reads a member that has not run, so such a box may still wait for it (SEM-12); the
  vendor text does not address overrides. A plain start of it in that run is refused with one
  START_REFUSED line; FORCE_STARTJOB still starts it (SEM-23), and the forced run votes. A
  forced start that queues keeps the box waiting. The force is a one-shot override: if the
  forced attempt leaves the queue unstarted (`queued-recheck` 1 or 2, DL-257), the mark
  stays, the member is out of the fold again, and its recurring condition does not start it
  in this run; only another FORCE_STARTJOB does. The box's next start clears the mark. A member that ran in the run
  keeps its vote and is not marked. The rule applies at a subbox's own parent too. The OFF_ICE
  does not undo the ice's completion moment (DL-285). The Web UI help (AutoSys 24.0, the
  monitoring guide's job-command page) reads the other way: "If the specified job is in a box
  job with a RUNNING status, the scheduler attempts to start it, conditions permitting." The
  scheduling guide sets the default; the `off-ice-in-running-box` semantic switch keeps the
  Web UI reading, dsl41's earlier behavior, as `same-run` (runner-design §8a). The runbook's
  "SEM-20 box clause" protocol settles which reading a live instance follows.
- An iced member that has not run is out of its RUNNING box's fold, so the ON_ICE itself runs
  the box's completion check (DL-285, SEM-11's third carve-out), and under the default
  `box-start-all-members-out=complete` a box whose members are all on ice when it starts
  completes at its start (SEM-11, Q15). A box that is not running ignores an iced member that
  is out of the run when it re-derives its status, under the default
  `idle-box-iced-member=ignore` (SEM-15, Q16).
- ON_ICE sent to a STARTING or RUNNING job, box or not, is ignored (DL-254). **[V]** Source:
  "sendevent Command -- Change the Executable Status of a Job" (AutoSys 24.2), JOB_ON_ICE:
  "The event has no effect on jobs with a status of STARTING or RUNNING." The oracle sets no
  flag, wakes nothing and records one `EVENT_IGNORED` trace line; the run ends with its real
  status. This replaces the earlier **[?]** pin that ice on a running job took effect when the
  run ended. A QUE_WAIT job is not named by the vendor: ON_ICE still dequeues it and settles it
  INACTIVE (DL-50, Qr5).
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
- ON_HOLD sent to a STARTING or RUNNING job, box or not, is ignored (DL-254). **[V]** Source:
  "sendevent Command -- Change the Executable Status of a Job" (AutoSys 24.2), JOB_ON_HOLD:
  "The event has no effect on jobs with a status of STARTING or RUNNING." The oracle sets no
  flag and records one `EVENT_IGNORED` trace line, so the job's next start is not held. A
  QUE_WAIT job is not named by the vendor: ON_HOLD still holds it in the queue (DL-50).
- IR: on_hold ≙ pause node, edges intact.

### SEM-22 · ON_NOEXEC **[V]**
Bypass-execution mode: the scheduler processes the job through its lifecycle but does not run
it. The job (and boxes that contain it) evaluate as SUCCESS, and downstream runs normally. Box
in ON_NOEXEC scheduled to run → goes RUNNING, members are bypassed to SUCCESS as their
conditions are met, box returns to ON_NOEXEC afterward. This is the "dry-run wiring" state.
*Model note:* the box sentence is applied at **each box level**, so a member box of an
ON_NOEXEC box also goes RUNNING and bypasses its own members; the dry run walks the whole
tree. A member bypasses on its own flag or on any containing box's. No vendor text states
that inheritance **[?]**. Since a box ON_NOEXEC event flags every job in the tree (DL-254),
it decides only corners where a member lacks the flag under a flagged box: a box flagged at
definition time, whose members do not take the flag (SEM-24); a member taken OFF_NOEXEC alone
under a flagged box; and a job that a later period's catalog adds to, or moves under, a
flagged box. The bypass counts as
that member's start for the run: it joins the box's ran set, so the SEM-11 fold waits for
every member's bypass, and a member whose condition never fires keeps the box RUNNING like any
member that never ran. The bypass is also the tick's run for SEM-34, so a bypassed job raises
no MUST_START_ALARM. The vendor text states one box level; applying it per level is this
project's pin. **[?]**
*Not modeled (DL-254):* the same vendor page says CHANGE_STATUS has no effect on an ON_NOEXEC
job, and that a box CHANGE_STATUS INACTIVE leaves ON_NOEXEC members' status alone. The
oracle's injected STATUS has no flag check, so it still moves a flagged job. DL-254 left
that behavior for a separate decision, which is the owner's.

DL-254: the scheduler ignores some ON_NOEXEC events. **[V]** Source: "sendevent Command --
Change the Executable Status of a Job" (AutoSys 24.2): "The scheduler ignores the
JOB_ON_NOEXEC event, if sent to: A non-box job that is in the STARTING, RUNNING, or ON_ICE
status; A box job that is in the ON_ICE or RUNNING status; A box job with jobs (including the
jobs contained in lower level boxes) in a status other than the following status: ON_HOLD,
ON_NOEXEC, INACTIVE, SUCCESS, FAILURE, ACTIVATED, or TERMINATED", and "The JOB_ON_NOEXEC event
does not supersede the JOB_ON_ICE event and does not overwrite the ON_ICE status with the
ON_NOEXEC status." In this model the third case is a box containing, at any depth, a job that
is iced, STARTING, RUNNING or QUE_WAIT. The oracle sets no flag and records one
`EVENT_IGNORED` trace line. So a real failure of the running job reads normally, its next
start runs, a RUNNING box's waiting members run for real, and no noexec flag survives
OFF_ICE. A box in STARTING is not named and is not ignored.

DL-254 also applies the rest of the passage. **[V]** A queued job: "If the job is in the
QUEWAIT or RESWAIT status, the scheduler removes the job from the load balancing and resource
wait queues before placing it in the ON_NOEXEC status." The oracle dequeues it and moves it
to INACTIVE through DL-242's operator-INACTIVE path, exit code cleared, with the flag. It
holds no reservation and spawns nothing. A held job: "The JOB_ON_NOEXEC event supersedes the
JOB_ON_HOLD event effectively overwriting the ON_HOLD status with the ON_NOEXEC status." The
hold is cleared and recorded as an `OFF_HOLD` whose cause names ON_NOEXEC. A job released
from a hold or from the queue then retries its start, as OFF_HOLD does: the vendor bypasses a
NOEXEC job "When the NOEXEC job meets its starting conditions", so a job whose conditions
already hold bypasses to SUCCESS at once, unless the event's own wakes already started it.
A box: "If you send the JOB_ON_NOEXEC event to a box, the effect is the same as sending the CHANGE_STATUS event to INACTIVE for a box. The
box enters the ON_NOEXEC status and the scheduler sets the status of all jobs in the box
(including all jobs contained in lower level boxes within the box) at all levels to
ON_NOEXEC." Every job in the tree takes the flag first, held ones losing their hold. Then
the SEM-18 cascade runs as one batch. Every row in the tree loses its exit code, a row
already INACTIVE included. A failed member therefore reads INACTIVE, as DL-243 rules for a
job put ON_NOEXEC on its own. The box rule wins over the single-job SUCCESS exception: a
SUCCESS member moves to INACTIVE too. If the whole tree is already INACTIVE, only the flags
move, unless the box's parent is RUNNING: then the box's INACTIVE->INACTIVE transition runs,
so the parent resolves it and may complete, as under CHANGE_STATUS INACTIVE (DL-242). Jobs
whose hold was cleared retry their start afterwards. The ignore rules above mean the cascade never meets a
live job. JOB_OFF_NOEXEC on a box: "all jobs in the box (including all jobs that are
contained in lower level boxes within the box) are reset", so every flag in the tree
clears.

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
is the documented exception and is left alone. A STARTING or RUNNING job ignores the event
(DL-254), and a QUE_WAIT job leaves the queue for INACTIVE, so a real failure that follows is
not retroactively hidden. A RUNNING box's member resolves the same way DL-242 rules. A BOX
target takes the box path described under DL-254 above. The vendor
text also says dependent jobs are not "immediately" scheduled, where this oracle wakes
referencers synchronously like any other transition; that gap is noted, not modeled. Because
the transition updates `status_at`, an ordinary (non-lookback) `n()` atom was already true
before it (FAILURE/TERMINATED already satisfies NOTRUNNING) and stays true; only a
LOOKBACK-qualified `n()` atom can newly turn true by this specific transition, from the
refreshed timestamp.

DL-281: lint's L020 projects a definition-time ON_NOEXEC predecessor onto its bypass, which
ends in SUCCESS with no exit code, so a failure, terminated or exit-code atom reads false
whatever its lookback and every other atom reads true.

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

The refusal of a force on a STARTING or RUNNING job, box or not, models the
`RESTRICT_FORCE_STARTJOB` configuration. **[V]** "Events" (AutoSys 24.2), FORCE_STARTJOB: "The
product does not support concurrent runs of the same job. Errors may cause a job to stop
progressing after the agent executes it. To avoid simultaneous executions, do not force start
a job while it is in the STARTING or RUNNING state. In this case, change the status of the
job. The RESTRICT_FORCE_STARTJOB environment variable enables you to force the scheduler to
reject force start attempts on jobs that are executing." With the variable unset, the vendor
starts a second run that it calls unsupported, and the oracle's one status per job cannot
hold two runs. So there is no second reading to select and no switch. An estate whose
scheduler runs with the variable unset can diverge here: an operator's force on a live job
starts a second run there, and dsl41 refuses it.

### SEM-24 · `status:` at definition time **[V]**
Estate-shaped JIL carries `status: ON_HOLD` on `insert_job` (including on box jobs): the job
is created already in an out-of-band state, equivalent to an insert plus an immediate sendevent
of the state. Source: "status Attribute — Set an Initial Status for a Job During Insertion"
(TechDocs 12.0.01; AutoSys 24.2). The page states that the attribute cannot be used with
update_job/override_job. The 24.2 page gives the full value set: "The valid values are FAILURE,
INACTIVE, ON_HOLD, ON_ICE, ON_NOEXEC, SUCCESS, or TERMINATED." The default is INACTIVE. For an
initial FAILURE, SUCCESS or TERMINATED it adds that "the downstream jobs can start when other
jobs complete and utilize this job as a dependency."
- Support limit: dsl41 models `INACTIVE` (the implicit default) and the SEM-20/21/22 states
  `ON_HOLD` / `ON_ICE` / `ON_NOEXEC`. Lowering refuses `SUCCESS`, `FAILURE` and `TERMINATED`
  loudly. Each would seed a SEM-01 latch at definition time, which is not modeled. Supporting
  them is a separate decision.
- Lowering: `Semantics.initial_status`.
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
`start_times: "10:00, 11:00"` — absolute times of day (24h), 00:00-23:59. `start_mins:
10,20,30` — minutes past *every* hour. Each firing inserts a STARTJOB event. Time and
condition compose as AND.

Colons inside an unquoted value need an escape (DL-251). JIL syntax rule 6 (TechDocs 24.2,
condition-attribute page): "you must use escape characters (a backslash) or must enclose the
value in quotation marks with any colons that are used in the value of an attribute statement.
For example, to define the start time for a job, specify 10\:00 or "10:00"." The start_times
attribute page gives the same spelling: `start_times: 10\:00, 14\:00`. The vendor shows this
escape on start_times (and on condition lookbacks, SEM-04) but not on run_window or
must_*_times; rule 6 is stated as a general rule for any attribute value, so dsl41 applies it
to every `hh:mm` lane (`start_times`, `run_window`, `must_start_times`, `must_complete_times`)
rather than guessing a narrower one. The AST keeps the source bytes verbatim (preserve and
canonical fidelity, `docs/jil-statement-syntax.md` rule 2); only lowering unescapes. A
whole-value quote (`"10:00, 11:00"`) is also accepted, as before; quoting one item of a list
(`10:00, "11:00"`) is not a documented spelling and stays refused, the same as a per-item
quoted relative must-time offset (SEM-34). `run_window` shares this lane's 00:00-23:59 bound;
the absolute `must_*_times` run to 71:59 (SEM-34).

**Across a DST change (DL-260). [V]** TechDocs 12.1 and 24.2, same text. "Standard Time
Changes": "AutoSys Workload Automation only runs jobs for which the start_time attribute is set
to between 1:00 and 1:59 during the second (standard time) hour. Jobs for which the start_mins
attribute is set run in both hours." A Sunday 1:05 job "runs only at the second 1:05"; an
every-30-minutes job runs at "1:00 DT and 1:30 DT, then again at 1:00 ST and 1:30 ST".
"Daylight Time Changes": "a job that is scheduled to run on Sundays at 2:05 runs at 3:00:05
that day; a job that is scheduled to run every day at 2:45 runs at 3:00:45". "If you schedule a
job to run more than once during the missing hour (for example, at 2:05 and 2:25), only the
first scheduled job run occurs." Relative jobs "run as expected": start_mins 0, 20, 40 run at
"1:00 ST, 1:20 ST, 1:40 ST, 3:00 DT, 3:20 DT, and 3:40 DT". The scheduler applies these rules by
default, on the change shape DL-249 detects (`timezones.dst_change`): 02:00-02:59 missing for
one hour, or 01:00-01:59 repeated for one hour. The first missing-hour start time is the
earliest by wall time, whatever order `start_times` lists them in. Other shapes convert at PEP
495 fold=0, which is unverified **[?]**, and so do calendar row times. The
`dst-start-times=fold0` switch selects fold=0 for every change (runner-design §8a): a repeated
time runs once, in the first pass, and each missing time runs past the gap (2:05 at 3:05).

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
  kill happened. Under `queued-recheck` 1 or 2, a job that fails its recheck as it leaves the
  queue loses the arm and waits for its next tick (DL-257).
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
DST changes (DL-249). **[V]** TechDocs 12.1 and 24.2, "Daylight Time Changes" and
"Standard Time Changes" (same text in both). Near a change, each window is a concrete
interval of instants, and containment and the closer-edge rule run on that interval. The
endpoints follow the vendor's rules. Spring, missing 02:00-02:59: "When the specified end of
the run window falls during the missing hour, AutoSys Workload Automation recalculates its
end time, so that the effective duration of the run window remains the same. For example, the
product recalculates a run window of 1:00 - 2:30 so that the window ends at 3:30". "When the
specified start time of the run window falls during the missing hour, AutoSys Workload
Automation moves the start time to 3:00. The end time does not change ... a run window of
2:45 - 3:45 becomes 3:00 - 3:45". "When both the start time and the end time of the run
window, fall during the missing hour, AutoSys Workload Automation moves the start time to the
first minute after 3:00 and the end time to one hour later ... a run window of 2:15 - 2:45
becomes 3:00 - 3:45". Fall, repeated 01:00-01:59: "When the specified start of a run window
is before the time change and its specified end occurs during the repeated hour, the run
window closes during the daylight time period (the first hour). For example, a run window of
11:30 - 1:30 ends at 1:30 DT, not 1:30 ST". "When the specified opening of the run window
falls during the repeated hour, AutoSys Workload Automation moves its start time to the
second, standard time hour. The end time does not change ... a run window of 1:45 - 2:45
becomes 1:45 ST - 2:45 ST". "When both the specified start and end of the run window occur
during the repeated hour, the run window opens during the second, standard time hour".
Two readings are this project's pins, not the vendor's text **[?]**: in fall, when both
endpoints are in the repeated hour, the close follows the opening into the second pass; in
spring, the vendor's "first minute after 3:00" is pinned to exactly 03:00. The vendor's rules
describe two distinct endpoints, so an equal-endpoint window keeps the zero-width pin above:
it is the one instant its opening maps to (`"02:30-02:30"` on a spring change is 03:00).
Scope is the offset shape, not a list of zones: a spring change where 02:00-02:59 is missing
for one hour, or a fall change where 01:00-01:59 repeats for one hour, on the attempt's local
date or within two days of it. America/New_York has both. Europe/Berlin and Australia/Sydney
qualify in spring only; their fall changes repeat 02:00-02:59. Europe/London and
Europe/Dublin qualify in fall only; their spring changes skip 01:00-01:59. Other shapes
(those, half-hour changes, changes at other hours) keep the wall-time comparison, which is
unverified **[?]**. The same
page notes that a start time of 1:15 inside an 11:30 - 1:30 window "would be calculated for
1:15 ST and the job would not run". Under the default `dst-start-times=vendor` (SEM-32,
DL-260) the 1:15 tick is 1:15 ST, past the window's 1:30 DT close, and the closer-edge rule
skips it. Under `fold0` it ticks at 1:15 DT, inside the window, and runs.
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
positional pairing below, and refused against start_mins (DL-248). Relative can cross ≤ 2
calendar days; that span is a recorded vendor constraint, not loader validation. For the
absolute form lowering checks the vendor's two ordering rules (DL-253). A must time below its
own start time is refused: "If 10:00 a.m. is specified, the job issues an error message", and
the next day's time is written +24 hours. A must time not earlier than the next run's start
time is refused: "The must start time for a run must be earlier than the start times for the
next run", with 11:10 against an 11:00 run as the vendor's invalid example. Lowering checks
this against the next later start time of the same day only. The latest start time of the day
is not checked: its next run depends on the calendar, and a weekly job's next run may be days
away. With one pending check at a time, a last-slot must time that is still pending at the next
day's first tick keeps that tick from arming its own deadline (DL-253). A relative offset's span
and order stay unchecked. IR: model as SLA
annotations, not semantics.
The relative form also counts against `start_mins` (DL-248). **[V]** TechDocs 24.2,
must_complete_times attribute page: "The must complete times are calculated relative to the
start_mins or start_times attributes." Its start_mins example runs every 10 minutes with
`+7`: "the 2:10 p.m. job run must complete by 2:17 p.m." Only that documented form lowers: a
single relative offset, broadcast to every start_mins tick. The vendor refuses the absolute form
there: "If you specify the start_mins attribute in the job definition, you can only define
relative times. You will get an error if you define absolute times with start_mins." Lowering
refuses it (DL-253). A list of relative offsets against start_mins is not specified by the vendor
pages and stays open; lowering refuses it too.
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
- At most one must_complete and one must_start deadline is pending per job, in either form
  (DL-248, DL-253). The vendor inserts the next CHK_START and CHK_COMPLETE only "After the job
  completes". A tick arms one only when the job is not live (STARTING, RUNNING or QUE_WAIT)
  and no earlier deadline of that kind is still pending, that is neither met nor fired. A
  must_start deadline is met once a run began after its tick. A slot that passes while a run
  is live or a deadline is pending gets none.
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
**Absolute must times are armed (DL-253). [V]** The must_start_times page (must_complete_times
has the same text): absolute times in 24-hour format, "Limits: 00:00-71:59 (2 calendar days
ahead of the current calendar day)". "If a job has multiple start times, you must specify the
same number of must start times", "corresponding to each run of the job". A must time below its
start time is the next day's, written +24 hours: "you cannot specify 10:00 a.m. as the must
start time ... you must specify the must start time as 34:00". Lowering accepts 00:00-71:59,
the `\:` spelling included (SEM-32), and refuses 72:00 and above. IR-F holds the value as a
`MustTime`, whose hour runs to 71; `start_times` and `run_window` keep `Time`'s 00:00-23:59.
The STARTJOB tick arms the absolute deadline beside the relative ones. The tick's slot names
the must time by position. The deadline is that time on the tick's local calendar day, in the
job's zone as start_times are read, plus one day for each 24 hours past 23. The alarm rules,
the run the tick asks for and the one-at-a-time rule are the relative form's.
*Model note:* the times pair with start_times by position. The oracle names the slot by
instant (DL-260): it converts the start times of the tick's local day, in the job's zone,
through the scheduler's own conversion and under the same `dst-start-times` value (SEM-32),
and takes the latest start time at or before the tick, less than a minute earlier. So a start
moved by a DST change usually names its own slot. Under `fold0` a missing-hour start can share
an instant with a later start time; the scheduler ticks once, and the tick names the earlier
wall time. `vendor` can collide too: start_times "02:00, 03:00" in America/New_York on
2026-03-08 both convert to 07:00:00 UTC, so the scheduler ticks once there as well, naming
slot 0, and slot 1's run and its must time never arm. On a spring change day, a moved start
that lands on the instant of a listed start shares one tick, named for the earlier wall time;
the vendor pages do not say. **[?]**
For a relative offset, a tick at an instant that matches no start time (an operator's STARTJOB
at another time) cannot be paired and takes the first offset. **[?]** An absolute form arms
nothing for such an instant: the vendor ties the CHK events to the scheduled start times. A
deadline that falls before its tick is due at the tick. **[?]** Both `dst-start-times` values
reach that: `fold0` ticks a start in the missing hour past the gap, later than the vendor runs
it, while lowering refuses a must time below its own start time, the other way to write one.
`vendor` reaches it too on a change shape `dst_change` does not name (DL-249) -- Europe/London's
spring change skips 01:00-01:59, not 02:00-02:59, so it keeps the fold=0 conversion even under
`vendor`: a 01:45 start ticks at 01:45 UTC, and a must_start_times of "02:10" resolves to
01:10 UTC, before the tick.
(Contrast `term_run_time`: that one *is* control flow, auto-TERMINATE after n minutes.)

**Absolute must times across a DST change (DL-253). [V]** TechDocs 24.2. "Daylight Time
Changes": "When the specified must start or must complete times falls during the missing hour,
AutoSys Workload Automation re-evaluates the time the job must start or complete to a time
during the first minute of the next hour. For example, a job that must start by 2:05 and must
complete by 2:45 generates an alarm if the job does not start by 3:00:05 or if it does not
complete by 3:00:45." The special case: "Suppose a job is scheduled to run at 2:45 with a must
complete time of 3:00 ... the job that is scheduled at 2:45 runs at 3:00:45, a time that takes
place after the scheduled must complete time. To prevent scheduling a job after its must start
or must complete time, AutoSys Workload Automation also re-evaluates the time ... to the final
second of the first minute of the hour following the missing hour. In the previous example, the
job generates an alarm if it does not complete by 3:00:59." Relative must times "evaluate as
expected". "Standard Time Changes": "when the specified start of the job is before the time
change and either the must start or must complete times or both occur during the repeated hour,
CA Workload Automation raises alarms during the daylight time period (the first hour). For
example, a job that is scheduled to run at midnight and must complete at 1:30 generates an alarm
if the job has not completed by 1:30 DT, not 1:30 ST." "When the specified start of the job and
either the must start or must complete times or both occur during the repeated hour, CA
Workload Automation raises alarms during the second standard time hour." The oracle applies
these rules to changes of the shape DL-249 detects (`timezones.dst_change`): a one-hour gap at
02:00-02:59 or a one-hour repeat at 01:00-01:59. A start on an earlier day is before the change.
Other shapes keep the fold=0 conversion, as run_window does. Under the default
`dst-start-times=vendor` the scheduler ticks a start in either hour at the vendor's own instant
(SEM-32, DL-260), so these rules meet the start the vendor describes. Relative must times
"evaluate as expected": a 2:45 start with +5 and +15 runs at 3:00:45 and is due at 3:05:45 and
3:15:45.

### SEM-35 · timezone **[V]**
Per-job `timezone:` re-bases all time attributes of that job. IR carries tz per schedule
block. Equivalence of schedules is tz-aware. A job with no `timezone:` reads its time
attributes in the run's base zone (`--timezone`, pinned as the period's `default_tz`). The
scheduler ticks in it, and the engine's oracle and every replay read slots, absolute must times
and run_window in it too (DL-155, DL-253).
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
sat, sun. Replace mmm with [jan … dec]." The 24.2 render of the page says "Replace n with a
1-digit number between 1 and 7." The worked example `Ctue#02` shows that two-digit
forms are zero-padded. "The below list of keywords uses all capital letters; however, the date
condition keywords are not case-sensitive." Zero padding is the documented canonical width, not
a parse requirement: the observed export sample writes `MNTHD#1` and `workd#1`, so unpadded
ordinals are accepted as input and mean the same day. `n`/`nn`/`nnn` are spelling widths; the
accepted range is per family, not global: `ddd` 1–5, `WEEKD` 1–7, `WORKD` / day-of-month
/ `mmm` 1–31, `WEEK`/`WEKR`/`CWEEK`/`Cddd` 1–53, `CYCP` 1–30, `CYCL`/`CWRK` 1–365. A `WEKR`
anchor digit is 1–7. An ordinal or anchor outside its family's range is a loud refusal.

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
| anchored week-of-year | `WEKRn#nn`, `WEKRnMnn`, `WEKRddd#nn`, `WEKRdddMnn` | `WEKRnXnn`, `WEKRdddXnn` | nnth week of year with weeks starting on the anchor day, back from last |
| day-of-month | `MNTHD#nn`, `MNTHDMnn`, `FOM`, `EOM` | `MNTHDXnn`, `XFOM`, `XEOM` | nnth day of month fwd/back; first/last day of month |
| named month | `mmm`, `mmm#nn`, `mmmMnn` | `Xmmm#nn`, `XmmmMnn` | whole month; nnth day of mmm fwd/back |
| cycle day | `CYCLE`, `CYCL#nnn`, `CYCLMnnn`, `CYCP#nn` | `CYCLXnnn` | any period day; nnnth day of each period fwd/back; nnth period |
| cycle week | `CWEEK#nn`, `CWEEK#E`, `CWEEK#O`, `CWEEK#L`, `CWEEKMnn` | `CWEEKXnn` | nnth/even/odd/last week of each period |
| cycle workday | `CWRK#nnn`, `CWRK#L`, `CWRKMnnn` | `CWRKXnnn`, `CWRKXL` | nnnth/last workday of each period fwd/back |
| cycle weekday | `Cddd#nn`, `Cddd#L`, `CdddMnn` | `XCddd#nn`, `XCdddMnn` | nnth/last occurrence of ddd in each period |

- Week anchoring **[V]**: "All weeks in the year begin on the same weekday as January 1 of
  that year". The page lists the WEKR forms right after `WEEK#nn`, `WEEKXnn` and `WEEKMnn`:
  "you can specify a different start day by modifying the keywords as follows". So a WEKR
  token selects the nnth week of the year, with weeks that begin on the given day. The 12.x
  renders spell the anchor as a day name (`WEKRddd#nn` / `WEKRdddXnn` / `WEKRdddMnn`;
  `WEKRMon#nn` in the 2014 example). The 24.2 render spells it as a digit (`WEKRn#nn` /
  `WEKRnXnn` / `WEKRnMnn`, n from 1 to 7), and its 2014 example writes Monday as `WEKR1#nn`.
- The `WEEKDXn` entry says: "You can specify a different start day by using the
  WEEKDstartdayXn keyword." (12.1 and 24.2). That form re-anchors the day-of-week keywords.
  The page gives no entry for that form, and dsl41 refuses it as an unknown token.
- **WEKR reading** (DL-259): a WEKR token selects whole weeks of the year, and every week
  starts on the anchor day. It is not a recurring weekday. This is the vendor reading above;
  the `WEEKDstartdayXn` sentence names a different keyword and does not change it. The anchor
  is a digit or a day name, and both spellings name one anchor. The digits run 1 = Monday to
  7 = Sunday. The vendor shows only 1 = Monday; the rest follow the same order. `#nn` counts
  forward, `Mnn` counts back with 01 as the last week of the year, and `Xnn` excludes the
  week. `#L` is `M01`. A week counts in its own year: a week that runs past December 31 ends
  there, and its January days count in the next year. The last week is the week that holds
  December 31, even when it has fewer than seven days. The plain `WEEK` family treats its
  last week the same way.
- **The partial first week [?]** (DL-259): the vendor text does not say how the days before
  the first anchor day count. The `wekr-first-week` switch (runner-design §8a) selects the
  reading. `first-full`, the default: week 1 starts on the first anchor day on or after
  January 1, and the days before it are in no numbered week. This is the C library's `%U`/`%W`
  numbering. The vendor example asks for "full weeks as those that start on a Monday", which
  supports it. `partial`: week 1 runs from January 1 to the day before the first anchor day,
  so every later week number is one higher. When January 1 falls on the anchor day there is
  no partial week: both readings put week 1 at January 1–7, and a WEKR token with that anchor
  selects the same days as the `WEEK` token. Both readings count back from the same last
  week, so `Mnn` differs between them only when it reaches the partial first week. In a leap
  year that starts the day before the anchor day, `partial` numbers the last week 54; only
  `M01` reaches it, because `#54` is refused. Neither reading is verified on a live instance.
  Worked case, 2014 (January 1 a Wednesday), `WEKR1`: under `first-full`, `#01` is January
  6–12 and `#52` is December 29–31, and there is no week 53; under `partial`, `#01` is
  January 1–5, `#02` is January 6–12 and `#53` is December 29–31.
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
| `status` (on insert) | definition-time out-of-band state (SEM-24) **[V]**. Support limit: the vendor allows seven values; dsl41 models INACTIVE, ON_HOLD, ON_ICE and ON_NOEXEC, and lowering refuses SUCCESS, FAILURE and TERMINATED |
| `job_type` | selects the modeled job kind: CMD, BOX or FW (SEM-10); lowering refuses every other type. Support limit: the vendor default is CMD ("job_type Attribute", AutoSys 24.2: "If you do not specify the job_type attribute in your job definition, the job type is set to CMD (the default)"), but lowering requires the attribute and refuses its absence. That an exported definition always carries it is expected, not measured |
| `job_load`/`priority`/`machine_method`/QUE_WAIT, `machine` lists | the pre-11.3 load-balancing model; the IR carries these in `passthrough` and reads `job_load` and `priority` from there. The oracle honors `job_load` vs machine `max_load` as a capacity bucket and `priority` as QUE_WAIT waiter ordering, lower number first (DL-50). **[V]** TechDocs 24.2 (DL-247): only a positive priority checks machine load. The priority page: "If you do not set the priority attribute or the priority is set to 0, the job is not queued behind other jobs and runs immediately on a machine if resource dependencies permit ... The scheduler ignores any load unit values defined for the job or machine when the job has a priority value of zero." The queueing page: "even when jobs have a priority of 0, AutoSys Workload Automation tracks job loads on each machine so that jobs with non-zero priorities can be queued." So an unset or zero priority skips the load check but its load still counts against the machine for every other job; its `resources:` still gate it. The job_load page: a forced job "runs even if its load exceeds the machine's max_load value"; a FORCE_STARTJOB likewise skips the check, holds its load, and is gated by named resources. The queueing page: "A job in the QUE_WAIT state for one machine attribute value automatically blocks all the lower priority jobs that specify the same machine attribute value. It does not automatically block higher or equal priority jobs that specify the same machine attribute value or a job that specifies a different machine attribute value." The oracle applies that to a fresh start and to the readmission scan, for a waiter whose own load does not fit. A blocked job needs a positive priority on that machine, with or without a `job_load`. DL-50's greedy scan past such a waiter is gone. A job leaving QUE_WAIT does not re-check its starting conditions by default, which is the vendor's EvaluateQueuedJobStarts 0; the vendor's default is 1. Qr6 is decided: the owner kept dsl41's default, and the `queued-recheck` semantic switch selects 1 or 2 (DL-50, DL-257). Named-resource blocking, the RESWAIT-after-load corner and a forced job's reuse of the units it holds are in the `resources` row (DL-255, DL-256). Open: unset-priority order among resource waiters (Qr2), pools (Qr3); pool `machine:` lines are typed `MachineMember` rows (DL-49) and preflight resolves their locality; `machine_method`, member selection/routing and per-member `factor`/`max_load` are opaque placement |
| `std_in_file`, `envvars` | CMD exec cluster (DL-32): stdin redirect (may reference a blob) + NAME=value environment list; typed carry on ExecSpec, `$$VAR` sites indexed (SEM-08); real execution refuses `envvars` at preflight outright, and refuses a `$$NAME` site in either field, since global substitution is not implemented (DL-240). Support limit: lowering refuses a repeated `envvars` line as a duplicate attribute. The vendor allows several ("envvars Attribute", AutoSys 24.2: "You can use multiple entries of envvars to define different sets of environment variables"). The workaround is one `envvars` line with a comma-separated list |
| `ulimit`, `elevated`, `interactive`, `job_class` | OS/agent-side exec tuning **[V]** (TechDocs 12.x); inert carry (DL-32) |
| `chk_files` | pre-start disk-space gate **[V]**: the agent checks required space; unmet → alarm and the job does NOT start; Resource-Wait class. Opaque carry, no oracle gate (a real disk level is out of a pure simulator's reach; distinct from `resources:`, which the oracle honors as capacity semaphores, DL-50); real execution refuses it at preflight (DL-240) |
| `heartbeat_interval` | MISSING_HEARTBEAT alarm only **[V]**; observability (DL-32) |
| `avg_runtime` | statistics seed at insert **[V]**; inert carry (DL-32) |
| `resources` + `insert_resource`/`update_resource`/`delete_resource` | 11.3+ resource objects **[V]** (TechDocs 12.x): `resources: (name, QUANTITY=n[, FREE=Y\|N\|A]) AND (...)`; FREE: Y=free on success, N=never, A=unconditionally; `res_type: D\|R\|T` (depletable/renewable/threshold), `amount` required, optional agent-level `machine`. Typed carry (DL-21); the oracle honors these as capacity semaphores (DL-50): `amount` is the bucket size, QUANTITY the demand, res_type sets the default release (R free-on-success / D depletable-never / T level-gate) and FREE overrides it. FREE is "Optional for renewable virtual resources only" ("resources Attribute", AutoSys 24.2, quoted in DL-256); no vendor text says what FREE does on a depletable (Q12, descoped, DL-288). Preflight refuses FREE=Y and FREE=A on a depletable as unknown release semantics and accepts FREE=N, which matches the depletable default (DL-287); the oracle still applies the code to a direct caller (DL-50); UCS-09 → UC Virtual Resources. **[V]** TechDocs 24.2, How AutoSys Workload Automation Queues Jobs (DL-255): "A job in the RESWAIT state for one resource name automatically blocks all the lower priority jobs that specify the same resource name. It does not automatically block higher or equal priority jobs that specify the same resource name or a job that specifies a different resource name." dsl41 has no RESWAIT status; such a job is QUE_WAIT. The oracle applies the rule to a fresh start, forced or not, and to the readmission scan. The blocker has a positive priority, names a resource the blocked job names, and is short on any resource it names; the blocked job has a positive priority, since an unset or zero priority "is not queued behind other jobs". A blocker short on one resource blocks on every resource it names: "AutoSys jobs/resources issue: job stuck in RESWAIT" (Broadcom KB 240816, AutoSys 12.0) shows a job waiting for its second resource blocking lower-priority jobs that needed only its first, which was available, and gives the same queueing sentence as the cause. Resources are evaluated "after the load balancing attributes are evaluated and the machine has available load units", and jobs that "enter the QUE_WAIT state do not consume load units or resources. These jobs do not automatically block lower priority jobs that specify the same resource attribute and a different machine attribute value." So a waiter blocks on its resources only once its load fits and no higher-priority load waiter blocks it. Jobs "that enter the RESWAIT state after the load balancing attributes are successfully evaluated do not consume any load units" and do not block on the machine; a queued job holds no units for the run it waits to start (units held from an earlier run stay held, DL-256). Preflight refuses a QUANTITY above `amount` at every priority. A depletable or FREE=N resource can still leave a waiter that never fits, and it then blocks lower priorities naming that resource, as the vendor's rule implies. The vendor documents FREE's default as Y, free on success only ("resources Attribute", AutoSys 24.2: "Default: Y"). dsl41 follows it (Qr1 decided, DL-256); the `renewable-free=A` semantic switch selects free on every completion, the earlier reading. A renewable's units that FREE does not free (N; Y after FAILURE or TERMINATED) stay held by the job until `sendevent -E RELEASE_RESOURCE -J job` ("resources Attribute": "To free the resources, issue the following command") or the job's next run, which re-uses them. A FORCE_STARTJOB of a FAILURE or TERMINATED job that still holds units "schedules the job using the held virtual resources" and "does not re-evaluate other resource dependencies" ("Define Virtual Resource Types", AutoSys 24.2). Support limits, each a loud lowering refusal: `QUANTITY=ALL`, "all the units of the resource" (workaround for a renewable resource: write its `amount` as the quantity; for a depletable resource this is not exact, since its free units fall below `amount` after use); a real-resource group with `VALUEOP`/`VALUE` (the page's `SYSTEM_OS_TYPE` example; no workaround); and one resource name defined on several machines, which the vendor allows ("insert_resource Subcommand", AutoSys 24.2: "You can define the same virtual resource on multiple machines") and dsl41 refuses as a duplicate `insert_resource` (no workaround) |
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
`test_sem11_waiting_member_*`), T11 a box start that leaves no member in the run
completes the box, and `box-start-all-members-out=wait` keeps it RUNNING (SEM-11, Q15:
`test_sem11_box_start_*`) ·
T12a internal box_success early-exit, T12b external box_success hung-RUNNING,
T12c box_success over a grandchild fires transitively (SEM-12), and the walk up skips a box
that the same moment completed and started again
(SEM-12: `test_sem12_a_box_started_again_in_the_cascade_ignores_the_earlier_run_s_job`,
`test_sem12_the_walk_skips_a_restarted_box_and_reaches_the_one_above_it`,
`test_sem12_the_walk_goes_on_past_a_restarted_box_to_an_unmoved_one`,
`test_sem12_an_ice_whose_box_starts_again_does_not_walk_into_the_new_run`,
`test_sem12_an_inactive_cascade_is_bound_to_the_runs_before_its_rows_move`,
`test_sem33_a_window_skip_s_walk_skips_the_box_run_the_skip_restarted`) ·
T13 sticky TERMINATED box (SEM-13) · T14 terminator cascade both directions (SEM-14) ·
a box_terminator member ending TERMINATED, killed, injected or dequeued, ends its box under
`box-terminator-on-terminated=true` and leaves it RUNNING under `false` (SEM-14:
`test_sem14_a_terminated_box_terminator_member_terminates_its_box`,
`test_sem14_under_false_a_terminated_box_terminator_member_leaves_its_box_running`,
`test_sem14_a_failed_box_terminator_member_terminates_its_box_under_both_values`,
`test_sem14_a_terminated_member_without_box_terminator_leaves_its_box_running`,
`test_sem14_a_killed_queued_box_terminator_member_reads_the_switch`,
`test_sem14_a_box_terminator_member_ended_by_term_run_time_reads_the_switch`,
`test_sem14_a_box_terminator_subbox_that_ends_terminated_reads_the_switch`) ·
T15 idle box ignores INACTIVE members, the single-member table (SEM-15, DL-242:
`test_sem15_*`), and an iced member out of the run under `idle-box-iced-member=ignore`,
while a member that is not iced, taken off ice, forced, or iced but live still decides
(SEM-15, Q16:
`test_sem15_a_job_failing_inside_an_iced_subbox_leaves_a_completed_box_alone`,
`test_sem15_a_box_completed_at_its_start_stays_completed_through_an_iced_subbox`,
`test_sem15_an_iced_member_given_failure_leaves_a_completed_box_alone`,
`test_sem15_a_forced_member_that_is_not_iced_still_flips_a_completed_box`,
`test_sem15_a_member_taken_off_ice_and_forced_flips_a_completed_box`,
`test_sem15_a_forced_start_clears_the_ice_and_its_run_flips_a_completed_box`,
`test_sem15_an_iced_member_given_running_blocks_the_recompute_until_it_ends`,
`test_sem15_an_iced_member_given_failure_moves_no_box_that_never_ran`,
`test_sem15_a_job_failing_in_an_iced_subbox_moves_no_box_that_never_ran`,
`test_sem15_an_inactive_verdict_beside_an_iced_member_keeps_the_vacuous_success`,
`test_sem15_a_failed_member_set_inactive_beside_an_iced_success_gives_success`,
`test_sem15_an_iced_subbox_holding_a_live_job_keeps_its_vote`) · T18 box INACTIVE cascades to every contained job (SEM-18, DL-242:
`test_sem18_*`) ·
T20a ice downstream fires, T20b off-ice does not immediately run (SEM-20) ·
a member taken off ice in its running box sits the run out under
`off-ice-in-running-box=next-run` and may start under `same-run` (SEM-20:
`test_sem20_off_ice_in_a_running_box_completes_the_box_without_the_member`,
`test_sem20_under_same_run_off_ice_in_a_running_box_hangs_the_box`,
`test_sem20_off_ice_in_a_running_box_and_a_recurring_condition`,
`test_sem20_a_plain_start_of_a_member_taken_off_ice_in_its_running_box`,
`test_sem20_a_forced_start_runs_a_member_taken_off_ice_and_it_votes`,
`test_sem20_a_member_taken_off_ice_runs_in_the_box_s_next_run`,
`test_sem20_off_ice_on_a_member_that_ran_keeps_its_vote`,
`test_sem20_off_ice_while_the_box_is_idle_sets_no_mark`,
`test_sem20_off_ice_on_a_member_that_was_not_iced_sets_no_mark`,
`test_sem20_a_forced_member_queued_after_its_off_ice_keeps_the_box_waiting`,
`test_sem20_a_forced_member_that_leaves_the_queue_unstarted_keeps_sitting_out`,
`test_sem20_a_marked_member_set_running_keeps_the_box_waiting`,
`test_sem20_off_ice_in_an_idle_subbox_of_a_running_box_sets_no_mark`,
`test_sem20_ice_on_a_member_taken_off_ice_is_no_completion_moment`,
`test_sem20_a_subbox_taken_off_ice_in_its_running_parent_sits_the_run_out`) ·
ordinary vs lookback atoms on a non-live iced job follow different tables, DL-243
(SEM-20/SEM-05, Q10 residue: `test_sem20_ordinary_atoms_on_an_iced_job_follow_the_vendor_table`,
`test_sem20_lookback_atoms_on_an_iced_job_stay_true`,
`test_sem20_ordinary_atom_on_an_undefined_iced_lookalike_stays_false`,
`test_sem20_ordinary_atom_on_a_live_iced_job_reads_the_real_in_flight_status`,
`test_sem20_off_ice_later_reads_the_real_status_not_the_vendor_table`) ·
ON_ICE, ON_HOLD and ON_NOEXEC on a STARTING or RUNNING job, and ON_NOEXEC on an iced job, are
ignored with one EVENT_IGNORED line and no planned effect, as is ON_NOEXEC on a box holding
an iced, live or queued job; ON_NOEXEC dequeues a QUE_WAIT job to INACTIVE, clears a hold,
and on a box cascades INACTIVE and flags every level, DL-254 (SEM-20/21/22:
`test_sem20_on_ice_on_a_live_job_is_ignored`,
`test_ice_on_a_running_job_takes_effect_at_completion` (name kept from DL-13),
`test_sem21_on_hold_on_a_live_job_is_ignored`,
`test_sem21_events_on_a_completed_job_still_apply`,
`test_sem21_events_on_a_queued_job_keep_their_handling`,
`test_sem22_on_noexec_on_a_live_job_is_ignored`,
`test_sem22_on_noexec_on_a_starting_box_still_sets_the_flag`,
`test_sem22_on_noexec_on_an_iced_job_is_ignored`,
`test_sem22_on_noexec_on_a_running_box_does_not_bypass_its_waiting_members`,
`test_sem22_on_noexec_on_a_queued_job_takes_it_out_of_the_queue`,
`test_sem22_a_dequeued_noexec_job_whose_condition_went_false_waits_for_it`,
`test_sem22_on_noexec_on_a_held_member_bypasses_and_completes_the_box`,
`test_sem22_on_noexec_on_a_waiting_subbox_resolves_its_running_parent`,
`test_sem22_on_noexec_on_a_box_clears_the_exit_code_of_an_inactive_member`,
`test_sem22_the_release_retry_does_not_repeat_a_start_the_event_already_made`,
`test_sem22_on_noexec_supersedes_the_hold_on_a_queued_job`,
`test_sem22_on_noexec_supersedes_on_hold`,
`test_sem22_on_noexec_on_a_box_cascades_inactive_and_flags_every_level`,
`test_sem22_on_noexec_on_an_inactive_box_moves_no_status`,
`test_sem22_off_noexec_on_a_box_clears_every_level`,
`test_sem22_on_noexec_on_a_box_with_a_contained_job_in_another_status_is_ignored`) ·
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
tick arms, edge starts, start consumes (SEM-32, DL-54/DL-58: `test_sem32_*`), and start times
across a DST change under both `dst-start-times` values (SEM-32, DL-260: `test_sem32_dst_*`) ·
T33a/b
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
T33c the window read in the job's timezone (SEM-33, with SEM-35), and its endpoints across a
DST change (DL-249: `test_sem33_spring_*`, `test_sem33_fall_*`, `test_sem33_dst_*`,
`test_sem33_box_start_on_a_spring_change_*`, and with a DL-260 start time
`test_sem33_dst_a_fall_start_time_*`) ·
T34a/b must_* emit alarms only, T34c each start_time arms its own relative offset, T34
relative must_complete anchored to the tick's slot (SEM-34, DL-248: `test_sem34_must_complete_*`),
absolute must times armed over 00:00-71:59, one must_start deadline at a time, and the DST
rules (SEM-34, DL-253: `test_sem34_absolute_*`, `test_sem34_must_start_*`,
`test_sem34_spring_*`, `test_sem34_fall_*`), the tick naming its slot by instant (DL-260:
`test_sem34_dst_*`, `test_sem34_an_event_inside_*`).

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
its rule. An open question runs on a documented default or a preflight refusal, marked
`# PENDING: Qn` in the code where the code holds a provisional default;
[docs/live-instance-runbook.md](live-instance-runbook.md)
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
  shared-evaluator pin (atom true) stands. Q6 has no code switch. The pin decides what such
  an override reads at two completion moments: an ice on a member that has not run (DL-285),
  and a box start that leaves no member in the run (SEM-11, Q15). Under a flip, a box whose
  box_success names a member that is iced at its start stays RUNNING, the literal "not
  scheduled" reading.
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
- Q10 (SEM-05/SEM-20, DL-243): open, pinned default. Two vendor pages read a
  LOOKBACK-qualified atom against an iced predecessor differently. The "condition Attribute"
  page (AutoSys 24.2) addresses it directly: "If the predecessor job being evaluated for the
  look-back condition is currently in an ON_ICE status, it always evaluates to true. That is,
  any look-back evaluation is ignored." The ON_ICE truth table on "Start Conditions" (AutoSys
  Workload Automation 24.2) does not separate lookback atoms and reads f()/t()/exitcode
  false. The default follows the condition Attribute page: every atom kind true, lookback
  ignored (the DL-13 blanket pin, SEM-05). `# PENDING: Q10` marks the branch in
  `semantics.iced_atom_truth`.
  A live instance icing a predecessor referenced by both an ordinary and a lookback-qualified
  atom on the same consumer job would settle which page it follows. The table's reading is
  selectable without a code change (DL-252): `--semantics ice-lookback=ordinary` drops the
  qualifier and applies the ordinary ON_ICE table to the lookback atom too. The register row
  `event:ON_ICE#lookback atom` names the switch.
- Q11 (SEM-37, DL-250): closed (DL-259). A WEKR token selects a week of the year with weeks
  starting on the anchor day, as "Date Condition Keywords" lists it beside `WEEK#nn`,
  `WEEKXnn` and `WEEKMnn`. The numeric anchors run 1 = Monday to 7 = Sunday. How the days
  before the first anchor day count is an owner-approved choice, not a probed fact: the
  default `first-full` puts them in no week, and `--semantics wekr-first-week=partial` makes
  them week 1. SEM-37 states both readings. A live instance generating `WEKR1#02` for 2014
  would settle the choice: January 13–19 under `first-full`, January 6–12 under `partial`.
- Q12 (§5 `resources` row, DL-287): descoped (2026-10-07, DL-288). What the vendor does
  with FREE on a depletable resource. "resources Attribute" (AutoSys 24.2) documents FREE as
  "Optional for renewable virtual resources only" and says nothing of FREE=Y or FREE=A on a
  depletable. The page has the same text in every version from 11.3.6 to 24.2. A search of
  the vendor documentation, KB articles and community threads (2026-10-07) found nothing
  more on the case. The vendor might reject the definition, ignore the code, or free the
  units. No default is pinned: preflight refuses FREE=Y and FREE=A on a depletable and
  accepts FREE=N, which matches the depletable default. The oracle applies the code to a
  direct caller, as DL-50 states. Descoped: the refusal is loud, so no estate the runner
  runs depends on the answer, and the case is not pursued. If it is ever needed, the
  runbook's Q12 protocol settles it: whether jil accepts each FREE code, and whether a
  QUANTITY=2 job runs after the FREE=A job ends and after the FREE=Y job fails.
- Q13 (SEM-18): open. What CHANGE_STATUS RUNNING does to a box. "sendevent
  Command -- Change the Status of a Job" (AutoSys 24.2) states a permission check: "If you
  issue the sendevent command to change the status of a box to the RUNNING status, AutoSys
  Workload Automation verifies that the security policy grants you access to send both the
  CHANGE_STATUS and STARTJOB or FORCE_STARTJOB events", FORCE_STARTJOB "To change the status
  of a box that specifies a condition attribute value". For a box in non-execution mode the
  same page says: "Changing the status of a non-execution box job to the RUNNING status starts
  the box job." The next sentence draws the contrast for a job that is not a box: "Changing
  the status of a non-box job to RUNNING changes the status of the job in the database but
  does not cause the job to execute." That the event starts an ordinary box too is a
  reasonable inference from that contrast, not a vendor sentence. Three things stay unknown: whether the box-cycle reset runs (SEM-10),
  whether the box's own condition is honored, and whether members with no condition start or
  only wait. The oracle writes RUNNING and nothing else: no reset, no new run, no member
  start. A member with no condition then never starts, so the box cannot complete. Since no
  box start runs, the previous run's per-run marks stay on the box row, as its ran members
  do: a member taken off ice in that run is still refused a plain start (SEM-20). The
  documented rerun recipes use STARTJOB, FORCE_STARTJOB and CHANGE_STATUS INACTIVE, not this
  event. No switch: neither reading is defined well enough to implement. No code marker.
  The runbook's Q13 protocol settles it.
- Q14 (SEM-14): open. Whether CHANGE_STATUS FAILURE or TERMINATED on a
  RUNNING box kills its job_terminator members. "Events" (AutoSys 24.2), CHANGE_STATUS: "The
  scheduler initiates any action that depends on the status of the job." "job_terminator
  Attribute -- Kill a Job if Its Box Fails" (AutoSys 24.2) kills a member "if its containing
  box job completes with a FAILURE or TERMINATED status". Together they lean to the kill, but
  an operator's status write is not clearly a completion. The oracle writes the box's status
  and kills no member; the live members finish on their own and vote nowhere, and jobs
  downstream of the box read the written status. This cannot hang or wrongly complete a box,
  since the box is already terminal. No switch, no code marker. The runbook's Q14 protocol
  settles it.
- Q15 (SEM-11, DL-304): open, pinned default. What a box does when its start leaves no member in the
  run: every direct member is on ice, or the box has no members. No vendor sentence names the
  case. Two sentences compose to completion: Basic Box Job Concepts (AutoSys 12.0 and 24.2)
  keeps a box RUNNING "as long as there are jobs in it with ACTIVATED or RUNNING status", and
  Events (12.0 and 24.2), JOB_ON_ICE: "If the job is in a box, the scheduler does not execute
  the job for the entire run of the box." The default `box-start-all-members-out=complete`
  completes the box at its start; `wait` keeps it RUNNING until an operator acts, as dsl41
  did before at a box start (runner-design §8a). The rest of DL-304 -- an iced member that
  is live, or holds a live or queued job, is not out of the run -- applies under both
  values. `# PENDING: Q15` marks the read in the oracle. The
  runbook's Q15 protocol settles it.
- Q16 (SEM-15, SEM-20): open, pinned default. Whether an iced member votes when a box that is
  not running re-derives its status. Basic Box Job Concepts (AutoSys 12.0 and 24.2) lets a
  member's FORCE_STARTJOB or CHANGE_STATUS change a box that is not running, and ignores only
  INACTIVE members; its table does not name ON_ICE. Job States (12.0 and 24.2) says an ON_ICE
  job "is removed from the job stream but is still defined", and that "You cannot manually
  change the status of a job from ON_ICE to INACTIVE"; no vendor sentence says that
  CHANGE_STATUS to another status clears the ice. No text names a job inside an iced subbox.
  The default `idle-box-iced-member=ignore` drops an iced member that is out of the run, as
  SEM-11's fold does; `vote` reads its status, as dsl41 did before (runner-design §8a).
  `# PENDING: Q16` marks the read in the oracle. The runbook's Q16 protocol settles it.

## Sources
Primary: Broadcom TechDocs, AutoSys Workload Automation 12.0/12.0.01/12.1/12.1.01 (Basic Box
Job Concepts also 24.2, same box-cycle wording: SEM-10, SEM-11, SEM-15, SEM-18; the
run_window page also 24.2, same wording: SEM-33; the 24.2 `priority` and `job_load`
attribute pages and How AutoSys Workload Automation Queues Jobs: the §5 load-balancing row,
DL-247; the 24.2 `must_complete_times` page and How Must Start Times and Must Complete Times
Work: SEM-34, DL-248; the 24.2 `must_start_times` page and the Standard and Daylight Time
Changes pages: SEM-34, DL-253; the 24.2 `status`, `envvars`, `condition`, `resources`, `job_type`,
`success_codes` and `fail_codes` attribute pages, the insert_resource Subcommand page and Date
Condition Keywords 24.2: SEM-02/04/09/24/37 and §5, DL-250): JIL
reference pages (`condition`, `box_success`, `box_failure`, `run_window`, `start_mins`,
`must_complete_times`, `date_conditions`, `n_retrys`), Scheduling guides (Basic Box Job
Concepts, Box Job Completion State, Must Start/Complete Times, Manage Common Job Properties,
Start Conditions, Job States: the Q2a/Q3/SEM-21 quotes), the monitoring guide's Manage Job
Events pages (off-hold/STARTJOB event semantics, 12.1.01), administration pages
(`MaxRestartTrys`, `KillSignals`), system-states reference (Events), Getting Started (AutoSys
Architecture), the box terminator pages (Force the Job or the Box to Stop Running, 12.1 and
24.2; the 24.2 `box_terminator` and `job_terminator` attribute pages: SEM-14), Start
Conditions 12.0 and 24.2 and Job States 24.2 (the OFF_ICE box clause, SEM-20), sendevent
Command -- Change the Status of a Job 24.2 (Q13), the 24.0 Web UI help job-command page (the
conflicting OFF_ICE reading, SEM-20), Broadcom KB 186248 (global variables), KB 11013 (scheduler-outage event
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
