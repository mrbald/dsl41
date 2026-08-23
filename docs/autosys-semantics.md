# AutoSys Semantics Dossier

Status: draft v0.1 · verified against Broadcom TechDocs (AE 12.x) where noted
Purpose: normative reference for the IR design, the linter rule set, the semantics oracle
(discrete-event interpreter), and the AutoSys→Stonebranch mapping table (stonebranch-semantics.md).

Every numbered SEM entry has a behavior pin unless §8 records it as a non-goal. Most pins are
trace tests against the semantics oracle; §8's layer note names the entries pinned elsewhere.
Confidence levels: **[V]** verified against Broadcom TechDocs 12.x · **[C]** corroborated by
multiple secondary sources · **[F]** one field observation, not verified against TechDocs and
not re-verified — treat as the weakest tier · **[?]** open question — before you rely on it,
make sure that a live instance agrees.

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
  5. (`run_window`, if present, additionally gates the actual start — see SEM-33).

**IR consequence:** the faithful layer of the IR models jobs as state machines and conditions as
predicates over a status store. The DAG is a *derived* artifact with per-edge confidence
annotations, never the primary representation.

---

## 1. Conditions: predicate algebra over the status store

### SEM-01 · Conditions are latching state predicates, not edges **[V]**
`condition: s(JobA)` is satisfied if JobA's *current recorded status* is SUCCESS — the time
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
`AND`/`&`, `OR`/`|`, parentheses for precedence. Evaluation is strictly left-associative flat
— `&` does **not** bind tighter than `|`, and C-style precedence is wrong for JIL. *(Resolved
2026-07-28, Q1 close, DL-53: TechDocs 12.1 "condition Attribute — Define Starting Conditions
for a Job" states "The parentheses force precedence, and the equation is evaluated from left
to right." Encoded as pinning tests `test_sem03_flat_left_to_right_precedence_pinned`
(grammar shape) and `test_sem03_precedence_pinned_model_level` (Cond model). The C-style
candidate grammar rule is deleted.)*
`NOT` does not exist as an operator. Negation is expressed via status atoms (for example,
`n()` and `f()`).

### SEM-04 · Lookback qualifiers **[V]**
Syntax: `s(job, hhhh.mm)` (or escaped colon `hhhh\:mm`).
- `s(job, 2)` — satisfied only if the status was reached within the last 2 hours. Sub-hour
  windows require a leading `00`: `00.30` = 30 min, bare `30` = 30 *hours*, `.30` is
  invalid. **[V]**
- `s(job, 0)` — "zero lookback": satisfied if and only if the condition job ended at or after
  the **dependent job's own last end**. **[V]** *(Q2a, resolved 2026-07-28, DL-54 doc sweep —
  TechDocs 12.0.01, condition attribute page: "When you specify 0, AutoSys Workload
  Automation examines the last end time of the job first. It then examines the last end
  time of the condition job. If the condition job has run since the last run of the job for
  which the condition is coded for, the job is allowed to start. If the condition job has
  not run since the last run of the job for which the condition is coded for, the job is
  not allowed to start." The page's own phrase "the job for which the condition is coded
  for" disambiguates the anchor as the dependent. The superseded midnight reading is
  discriminated both directions by the `test_sem04_zero_lookback_*` pinning tests. Its
  switch is deleted per the DL-06 protocol.)* **[V]** Q2b resolved *(2026-07-30, DL-58 —
  Broadcom community thread 760251, CA support best answer: "This is working as designed.
  When a new job is inserted it has no initial/previous end time" — a dependent that
  never ended has no anchor and the atom is satisfied. The thread's reporter observed the
  epoch-0 effect exactly as modeled)*. For box overrides, the box itself is the
  evaluator/anchor.
- `s(job, 9999)` — explicit "indefinite lookback", equivalent to no qualifier (legacy 4.5.1
  default). **[V]**
- Lookback applies to **status, cross-instance/external status, and exitcode atoms only —
  never to `value()` global-variable atoms**. **[V]** (Linter: lookback on `v()` = error.)
- Max lookback ≈ 416.58 days (9999.59). **[V]**

### SEM-05 · ON_ICE predecessors inside lookback conditions **[V]**
If the predecessor job referenced in a lookback condition is currently ON_ICE, the atom
evaluates **true** and the scheduler ignores the lookback entirely. (Interacts with SEM-20.)
The rule is blanket over atom kinds (DL-13): `f()`, `t()`, `d()` and `e()` on an iced
predecessor are all true, not only `s()`. Ice on a job that is STARTING or RUNNING takes effect
when that run ends — atoms read the real in-flight status until then **[?]** (unverified corner,
modeled deliberately).
*(Citation upgraded 2026-07-30, DL-58 — KB 438836, 12.1.01: with a local ON_ICE
predecessor "the system ignores the look-back condition" and "continuously evaluate[s]
the dependency as true". Same KB, cross-instance caveat: ON_ICE is NOT transmitted to a
remote instance — the remote sees SUCCESS with its real timestamp, and lookback DOES
apply against it. XinstIR is an opaque boundary for us (SEM-07), so this is a dossier
note, not model behavior.)*

### SEM-06 · Undefined jobs in conditions **[V]**
A condition atom that references a job that does not exist in the database evaluates **false,
permanently and silently** — the dependent job never auto-starts. AutoSys ships
`job_depends` to detect this. This is linter rule #1 (dangling reference), severity: error.

### SEM-07 · Cross-instance atoms **[V]**
`s(jobB^PRD)` — same predicate algebra against a job on external instance `PRD` (declared via
`insert_xinst: PRD` with `xtype:`, not `insert_machine`). Lookback applies. For the migration
these become boundary markers in the IR: dependencies whose producer is outside the modeled universe.

### SEM-08 · Global variables **[V]**
- Set via `sendevent -E SET_GLOBAL -G NAME=value` or `insert_global` JIL.
- In conditions: `value(NAME) = X` (also `>`, `<`, `!=` comparisons). *Model note:* the
  comparand is a string in JIL. The oracle compares numerically when both sides parse as
  base-10 integers, and lexicographically when either does not. The rule covers all six
  operators, so `value(N) = 5` is true against a stored `05`. The UC twin and the equivalence
  checker call the same comparison function, so the three layers cannot disagree.
- In attribute strings (command, std_out_file, …): `$$NAME` or `$${NAME}` substitution at
  runtime. Single-`$` is shell/environment, double-`$$` is AutoSys global — the parser must
  keep these distinct. A global-variable set is an event that triggers condition re-evaluation.

### SEM-09 · max_exit_success shifts SUCCESS/FAILURE boundary **[V]**
A job with `max_exit_success: 2` records SUCCESS for exit codes ≤ 2. Therefore `s(j)` on a
consumer is only meaningful relative to the producer's `max_exit_success`. IR: success predicate
is per-job-configurable, not a constant. Equivalence checking must normalize this.

*(Amended 2026-07-10, DL-33 / 12.x doc sweep.)* Two further attributes shape the boundary,
valid on Command, i5/OS, Micro Focus, and z/OS jobs (of our scope: **CMD only** — a loud
error on BOX/FW):
- `success_codes` — explicit success codes: single code, `lo-hi` range, or comma list of
  both (`1,3,20-30`). Absence-default: "exit code 0 is success". **[V]**
- `fail_codes` — explicit failure codes, same format. Absence-default: "any non-zero exit
  code is failure". **[V]**
The verdict is therefore `f(exit_code; max_exit_success, success_codes, fail_codes)` —
single source: `ir.exit_is_success`, shared with the UC twin (M31).

*(Amended 2026-07-30, DL-58: Q7 resolved by citation.)* KB 408778 states the composition:
a present `fail_codes` decides **alone** — listed codes are FAILURE and "Any other exit
code … will be interpreted as a success", so `success_codes` and the threshold are ignored
alongside it (a code in both lists is FAILURE — the old fail-wins pin, now for the cited
reason). If `fail_codes` is absent, a present `success_codes` replaces the success rule
entirely (unmatched code → FAILURE, threshold ignored). If neither list is present,
`max_exit_success` decides. The superseded DL-33 pin routed fail_codes-unmatched codes to
the threshold — it invented FAILUREs that the vendor records as SUCCESS — and consulted
success_codes after a fail_codes miss.

---

## 2. Boxes

### SEM-10 · Box membership and start rule **[V]**
`box_name: B` puts a job in box B. Members start when: box is RUNNING **and** the member's own
conditions hold. Members with no conditions start immediately when the box starts. A member runs
**at most once per box run**. **[V]**

### SEM-11 · Box RUNNING/completion **[V]**
The box stays RUNNING while any member is running. The box cannot complete before all members
run (or are bypassed). Default: box SUCCESS if and only if all members ended SUCCESS. Box
FAILURE if at least one member failed (evaluated after all members complete). A member that
ended TERMINATED counts as failed for this fold — SEM-14 kills land here.

### SEM-12 · box_success / box_failure override — with evaluation gating **[V]**
`box_success: <condition expr>` (same predicate language). Subtle, verified semantics:
- If the referenced job is **inside** the box: the scheduler evaluates the box status the
  moment that job enters the specified state, regardless of other members. "Inside" is
  **transitive** (DL-12): a job in a nested box is inside every box above it, so an outer
  box's override fires on a grandchild's transition while the inner box is still RUNNING.
- If the referenced job is **outside** the box (or an external job, or a global): the
  scheduler evaluates the box status when *some member completes after* the external
  condition became true. If all members complete *before* the external condition is met, the
  box is **not** evaluated and stays RUNNING — a classic hung-box production incident.
  Linter: warn on box_success/box_failure that reference non-member jobs.
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

### SEM-15 · Member status changes can ripple upward **[C]**
A CHANGE_STATUS/FORCE_STARTJOB on a member of a *non-running* box can change the box's derived
status and thereby trigger downstream jobs conditioned on the box. The oracle must model box
status as derived state re-evaluated on member events.

### SEM-16 · Jobs added to a RUNNING box **[V]**
When a job is inserted/moved into a running box: an ALERT event occurs, and the job's run
number is set to the box's. If the job is not STARTING/RUNNING/ON_ICE and its run number does
not exceed the box's, a STARTJOB is issued. Not migration-critical (definition-time mutation),
but the AST layer must not assume static membership. Noted as out of scope for the oracle v1.
`SEM-16` is also the house class name for mid-run catalog-object mutations ruled out of scope
v1 — mid-run `update_resource` replenishment of a depletable included (DL-50).

### SEM-17 · Deep nesting **[C]**
Boxes nest arbitrarily (practical guidance: ≤ 1000 members, avoid organizational grouping —
Broadcom's own guidance is boxes for *shared starting conditions*). ACTIVATED state = "top-level
box is RUNNING, member not yet started."
*Model note:* lowering accepts at most 64 containment links as a compiler sanity limit; a deeper
chain is a loud finding, not a silent truncation.

---

## 3. Out-of-band status manipulation

### SEM-20 · ON_ICE **[V]**
- The job will not run. It is removed from all conditions/logic.
- **Downstream conditions treat the iced job as satisfied** (runs "as though it succeeded") —
  every atom kind, per SEM-05.
  Inside a box, a member that depends on an iced sibling starts immediately when the box
  runs. **[V]**
- OFF_ICE: the job does **not** run even if its starting conditions currently hold. It waits
  for conditions to *reoccur*. **[V]**
- IR: on_ice ≙ graph rewrite "excise node, short-circuit its outgoing dependency edges to true".

### SEM-21 · ON_HOLD **[V]**
- The job will not run. **Downstream is blocked** (conditions on it do not become true).
- OFF_HOLD: if starting conditions are *already satisfied*, the job runs immediately (missed
  runs during hold collapse to at most one run). **[V]** *(Re-verified verbatim 2026-07-28,
  DL-54 — TechDocs 12.0, Start Conditions: "When you take jobs off hold, the scheduler does
  not re-evaluate date and time conditions. Jobs that meet their date and time conditions
  while they are on hold start immediately after they are taken off hold unless other
  starting conditions apply and are not satisfied." Oracle: a scheduled tick landing on a
  held job ARMS it — the same Q3 latch as SEM-32 — so OFF_HOLD starts it through the
  schedule gate. The page's run-window exception — off-hold outside the run window
  reschedules "to their next start time" — stays governed by SEM-33's verified closer-edge
  rule. The two sources are not fully reconciled — noted, not modeled apart.)*
- In a box: a held member prevents box completion — holds the whole stream.
- IR: on_hold ≙ pause node, edges intact.

### SEM-22 · ON_NOEXEC **[V]**
Bypass-execution mode: the scheduler processes the job through its lifecycle but does not run
it. The job (and boxes that contain it) evaluate as SUCCESS, and downstream runs normally. Box
in ON_NOEXEC scheduled to run → goes RUNNING, members are bypassed to SUCCESS as their
conditions are met, box returns to ON_NOEXEC afterward. The bypass overrides manual status
changes to members while the box is ON_NOEXEC. This is the "dry-run wiring" state — a useful
target concept for our own simulator semantics.
*Model note:* the box sentence is applied at **each box level**, so a member box of an
ON_NOEXEC box also goes RUNNING and bypasses its own members — the dry run walks the whole
tree. A member bypasses on its own flag or on any containing box's, and the bypass counts as
that member's start for the run: it joins the box's ran set, so the SEM-11 fold waits for
every member's bypass and a member whose condition never fires keeps the box RUNNING like any
member that never ran. The bypass is also the tick's run for SEM-34, so a bypassed job raises
no MUST_START_ALARM. The vendor text states one box level; applying it per level is this
project's pin. **[?]**

### SEM-23 · FORCE_STARTJOB vs STARTJOB **[C]**
STARTJOB honors nothing extra (it *is* the normal start event). FORCE_STARTJOB starts the job
regardless of conditions. Force-started runs still emit normal status events → forced runs
satisfy downstream latching conditions. The oracle needs both as injectable events.
Which gates a force bypasses, made explicit (DL-13): the `condition` expression, `ON_HOLD`, the
schedule gate (SEM-30/32), the box-RUNNING gate and the once-per-box-run gate (SEM-10). Which
gates still hold: `ON_ICE` (SEM-20 removes the job from all logic), a job that is already
STARTING/RUNNING/QUE_WAIT, and `run_window` — the SEM-33 closer-edge rule applies to a forced
start like any other.

### SEM-24 · `status:` at definition time **[V]** (existence) / **[?]** (full value set)
Estate-shaped JIL carries `status: ON_HOLD` on `insert_job` (including on box jobs): the job is created already in an out-of-band
state, equivalent to an insert plus an immediate sendevent of the state.
*(Upgraded 2026-07-10, 12.x doc sweep: TechDocs 12.0.01 documents "status Attribute — Set
an Initial Status for a Job During Insertion", with the constraint that it cannot be used
with update_job/override_job — existence is now **[V]**. The page's exact documented value
list is still unretrieved **[?]**. The modeled set stays `INACTIVE` (the implicit default)
plus the SEM-20/21/22 states `ON_HOLD` / `ON_ICE` / `ON_NOEXEC`, and anything else remains
a loud lowering error — extend deliberately when the page or an estate shape shows more.)*
- Lowering: `Semantics.initial_status`. Any other value (in particular run states like
  `SUCCESS`, which can interact with the SEM-01 latch) is a loud lowering error — extend
  deliberately when an estate shape shows one, never guess.
- Oracle: seeds the SEM-20/21/22 flags before the first event. No trace entry (definition
  state, not a transition).
- UC mapping: M20 (Hold, E-class) covers `ON_HOLD`. Ice/noexec follow their SEM-20/22 rows.
  The compile twin does not model definition-time state v1 and records it in the exclusion
  ledger instead (DL-18).

---

## 4. Date/time scheduling

### SEM-30 · date_conditions is the master switch **[V]**
The scheduler honors the time attributes (`days_of_week`, `run_calendar`, `exclude_calendar`,
`start_times`, `start_mins`, `run_window`, `must_start_times`, `must_complete_times`,
`timezone`) only when `date_conditions: 1`. Without it, the job runs purely on
conditions/manual events, and the scheduler ignores the time attributes (linter: warn on
time attributes present with
date_conditions absent/0 — dead configuration).

### SEM-31 · Mutual exclusivity **[V]**
- `start_times` XOR `start_mins` (both → JIL error, both ignored).
- `days_of_week` XOR `run_calendar` (cannot combine). `exclude_calendar` subtracts days from
  whichever is active.
Time attributes on a job inside a box: the member still needs the box RUNNING. A scheduled
member of a non-running box does not fire (schedule + box gate compose with AND).

### SEM-32 · start_times / start_mins **[V]**
`start_times: "10:00, 11:00"` — absolute times of day (24h). `start_mins: 10,20,30` — minutes
past *every* hour. Each firing inserts a STARTJOB event. Time and condition compose as AND.
Default reading since DL-54 (2026-07-28): **time-gated arm then wait** — a tick whose
`condition` is still false ARMS the job. The condition edge later starts it, and the start
consumes the arm (at most one run per tick). The doc lean, assembled 2026-07-28 (an
entailment, not one dispositive sentence — the inference is stated deliberately, Q5-style):
box members explicitly wait ("When a box starts running, the status of all the jobs it
contains ... changes to ACTIVATED"; "Maintains jobs with additional starting conditions in
the ACTIVATED state until those additional dependencies are met" — TechDocs 12.0, Basic Box
Job Concepts; Job States: ACTIVATED "the job itself is waiting to start"). The governing
rule is a continuously evaluated AND ("All defined starting conditions must be true for a
job to start" — Start Conditions, 12.0). Also, the held-across-tick case is documented as
start-on-release (SEM-21). **[V]** Q3 resolved *(2026-07-30, DL-58 — community-thread citations, both
Broadcom/CA staff)*: the standalone case is narrated directly — "There is a STARTJOB event
associated with the start_time or run_calendar … The STARTJOB event being processed
satisfies the start_times/run_calendar dependency", and a start "resets" it (thread 734033,
CA's Mark Hanson, with reproduced tests for both start_times and run_calendar. The
`CAUAJM_I_40162 starting conditions have not been met` log line is the armed-tick trace
signature.) The disarm boundary is no-expiry: "no set limit to how long [the job] would
wait … it will run immediately after the next time [the predecessor] completes
successfully, regardless of how far in the future that is", reset only by a (force-)start
or a JIL update (thread 801986, Broadcom employee) — latch-until-consumed as pinned. The
superseded abandon switch `ORACLE_SCHEDULED_FALSE_CONDITION` is deleted per the DL-06
protocol. **[?]** Q3c residue (new, DL-58): the same 801986 aside — "JobB would start
immediately after the next time its parent box starts" — hints that a MEMBER's latch can
survive into the next box run, in tension with the DL-54 box-run-scoped arm pin below.
The scoped pin stands until a live test (`# PENDING: Q3c`, oracle.py). Ticks
blocked at ON_ICE (SEM-20 reoccurrence) or at box-not-RUNNING do NOT arm — including a
HELD member of a not-running box. A tick on an already-live job does not arm
(pinned). Review-hardened pins (DL-54 adversarial round): a member's arm is scoped to the
box run that armed it — an unconsumed member arm dies at box completion (`SCHED_DISARM`
trace marker). As a result, no member ever auto-starts a later box run from a stale tick. A
pre-existing arm survives ON_ICE/OFF_ICE untouched (ice neither arms nor disarms — the
latched tick predates the ice). **[?]** Q3d residue (DL-69): that survive-pin is uncited,
and its consequence is that a stale tick can start the job on a condition edge after
OFF_ICE, in tension with the reoccurrence rule — pin stands until the live discriminator
runs (`# PENDING: Q3d`, oracle.py; protocol in the live-instance runbook). The ACTUAL start consumes the arm (a DL-50 QUE_WAIT
enqueue keeps it latched, so a canceled queue attempt does not eat the tick, while KILLJOB
on the queued job consumes it — the kill happened). An armed job re-blocked by run_window
re-uses one pending defer timer per opening instant. Accepted consequence of
latch-until-consumed: an armed run can land in a LATER run_window cycle than its tick
(the SEM-33 closer-edge rule applies at the armed start's own moment).

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
close and the next opening resolves to the next opening. Both readings are **[?]** — undocumented
ties, pinned this way, revisit with live access. The "closer edge" rule is a prime migration
hazard (no direct Stonebranch analog) — always flag it in mapping.
Box interaction (verified example): member with run_window + start time inside a box started
after the window → member INACTIVE so the box can complete, or STARTJOB queued for the next
window. The queued STARTJOB keeps the box RUNNING overnight. The closer edge decides which
outcome occurs.
**[?]** Open tension: that "so the box can complete" half is in conflict with SEM-11 as DL-13
pinned it (a member that never ran keeps the box RUNNING). The oracle follows the DL-13 pin on
both edges — a skipped member is left INACTIVE and never enters the box's ran set, so the box
stays RUNNING rather than folding. Which rule wins needs one live box test; do not read either
sentence as settled.

### SEM-34 · must_start_times / must_complete_times are alarms only **[V]**
They emit MUST_START_ALARM / MUST_COMPLETE_ALARM. They do not affect control flow. Absolute or
relative (`+n` minutes from each start time) — not mixed. The count must match the number of
start_times (JIL insert error otherwise). **[?]** One exception is implemented: a single
relative offset is accepted against any number of start_times and broadcasts to all of them.
The doc-derived corpus fixture uses one `+3` against three start_times, from TechDocs' own
example, so the strict count rule and the vendor's example disagree. Pin the exact rule on a
live instance. Relative can cross ≤ 2 calendar days. Each must_complete must precede the next
start. Both of those last two are recorded vendor constraints, not loader validation: lowering
checks the form and the count, never the span or the ordering. IR: model as SLA annotations,
not semantics.
*Model note:* only the relative forms arm an alarm. Absolute `must_start_times` /
`must_complete_times` lower to IR and are carried, but the oracle owns no calendar, so no
absolute deadline is armed v1. Under the strict count match the offsets pair with the
start_times **by position**: the oracle reads the tick's own time of day, in the job's
timezone, to name the slot. A start at an instant that is no start_time — an operator's
sendevent, or a start a condition edge released after the tick — cannot be paired and takes
the first offset. **[?]**
(Contrast `term_run_time`: that one *is* control flow — auto-TERMINATE after n minutes.)

### SEM-35 · timezone **[V]**
Per-job `timezone:` re-bases all time attributes of that job. IR must carry tz per schedule
block. Equivalence of schedules is tz-aware.
Scope re-verified 2026-07-09 (DL-23): TechDocs' own `date_conditions` page lists `timezone`
(with `run_window` and `must_*_times`) among the attributes date_conditions gates, and the
`timezone` page describes only "the job's time settings". As a result, timezone without
truthy date_conditions is dead configuration (SEM-30/L005 stands). The formerly-open Q2-adjacent
corner (does timezone re-base the zero-lookback midnight anchor?) DISSOLVED with Q2a
(DL-54): the anchor is the dependent's own last end — a comparison of two instants that no
timezone re-basing can affect. Thus timezone on a condition-only job is unconditionally
dead, and L005 fires without the old caveat. Estates that carry timezone as convention can
run `dsl41 lint --suppress L005`.

**Name resolution [V]** (added 2026-08-07, DL-62; 12.1 `timezone` attribute page + the
`autotimezone` command page): the value is "a string that corresponds to an entry in the
ujo_timezones table or is recognized by the operating system or is any valid POSIX value",
**not case-sensitive**, up to 50 chars of `a-zA-Z0-9/_-` (quote values containing colons,
e.g. `"IST-5:30"`). The scheduling manager matches the string against the OS **first**;
only if not found is the ujo_timezones table read, **up to five times**, to chase a chain
down to a resolvable zone; unresolved after five reads, the job fails. ujo_timezones is a
vendor-shipped, admin-editable (`autotimezone -a/-c/-t/-d`) table of three entry types --
Zone, Alias, City -- mapping names to POSIX TZ variables; `autotimezone -l` lists it, and
city entries such as `Vancouver City Canada/Pacific` (the docs' own excerpt) name the
matching region zone. POSIX TZ offsets are **west-positive** (`GMT+5` = 5h west of GMT).
The runner's port of this ladder, including the no-map unique-city default and its WARN,
is DL-62 / runner-design ss5. Two narrowings of the vendor's text are deliberate there: the
50-character `a-zA-Z0-9/_-` set is the vendor's statement about *names*, while a POSIX value may
also carry `+` and `:` (the page's own `"IST-5:30"`), and the resolver accepts those; and only
fixed-offset POSIX forms resolve — a POSIX string with DST rules is refused, because
approximating vendor DST rules would silently shift ticks.

### SEM-36 · Calendar definitions: the autocal_asc record model **[V]**
The scanner/IR *carry* of calendar exports is DL-36's. This entry pins what the records mean.
A standard `calendar:` is a literal day list (bare `MM/DD/YYYY [HH:MM[:SS]]` rows — the export
sample writes the seconds tail, and both widths are accepted; seconds are truncated). An extended
calendar generates its day set from rules. The 12.x file-format syntax block (Manage
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
  file-record examples on the `autocal_asc` command page use a *second* serialization —
  `workday: mo,tu,we,th,fr` (comma-separated two-letter days) — alongside the positional
  `{X|.}` form above. Both are Broadcom-primary.
- `holcal`: "Specifies the standard calendar containing dates to treat as holidays. The
  utility applies the holiday action to the dates in this calendar. Ensure that you specify
  this argument when you specify a holiday action." Standard calendars only — no doc instance
  permits an extended calendar as holcal.
- `cyccal`: "Specifies the cycle that any cycle-related conditions apply to. Ensure that you
  specify this argument when you specify any cycle-related conditions."
- **No timezone and no active-window attribute exist on calendar records** — the syntax block
  enumerates every field (brackets marking optional) and contains neither. `timezone` is a
  job attribute (SEM-35). Absence-of-evidence, but against a complete enumeration.
- Record-keyword spelling: one observed export sample (Q9 resolved, DL-60) writes
  **`extended_calendar:`**. `ext_calendar:` (the Manage Calendars syntax-block spelling)
  stays accepted as input leniency. The same sample pins the rest of the export format
  **[F]** (one sample, AE version unpinned, not re-verified): fixed
  attribute order `extended_calendar, description, workday, non_workday, holiday, holcal,
  cyccal, adjust, condition` with **empty-valued keys emitted** (the SEM-36 empty-value
  convention is the export's own habit) and `adjust: 0` always present. Workday as comma
  day codes plus the literal **`all`** (= every day). Condition token case preserved as
  authored (`{MNTHD#1}` next to `workd#1` in one file — no normalization). Condition
  grouping written with **braces** `{…}` where TechDocs shows parens (both accepted as
  synonyms). The **`WORKD#L`** last-ordinal in use (#L = from-end-1, extended
  uniformly across the ordinal families). `holiday: S` carried **without** holcal
  (against the 12.1 prompt-flow wording — S consumes no holiday set, but O/N/W/P keep the
  requirement). Standard-calendar rows stamped `mm/dd/yyyy 00:00:00` (**HH:MM:SS** tails).
  Cycle records as name/description plus repeated `start_date:`/`end_date:` pairs.
- Both calendar kinds are first-class on jobs **[V]**: "You can use a standard calendar or an
  extended calendar as the run calendar" (run_calendar page, 12.0) and identically for
  exclude_calendar, whose page adds the operational wording: "the scheduler inspects the
  calendar before starting the job. If the current date is on the calendar, the job does not
  start and its status changes to INACTIVE... if the job is a box job and its status changes
  to INACTIVE, all the jobs in the box change to INACTIVE."
  *Model note:* the runner narrows exclusion to tick suppression — an excluded day is simply not
  eligible, so no event is emitted. It does not synthesize the vendor's INACTIVE transition or
  the box-member cascade.

### SEM-37 · Extended-calendar date-condition keyword grammar **[V]** (defective tokens **[?]**)
Source: "Date Condition Keywords" — byte-identical between the 12.0.01 and 12.1 renders
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
prefix/infix exclusion, `#L` last). *(Amended 2026-07-30, DL-60: the doc page spells `#L`
only in the cycle families, but an observed export sample uses `WORKD#L` **[F]** — `#L`
(= from-end-1) is accepted uniformly across every ordinal family below.)*

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
  that year" — overridable per keyword via the `WEKRddd` forms (`WEKRddd#nn` / `WEKRdddXnn` /
  `WEKRdddMnn`), for example `WEKRMon#nn` for Monday-anchored weeks (the page's 2014 example).
- `WEEKDAYS` auto-subtracts holidays **[V]**: "The utility automatically excludes all dates
  that are listed in the calendar that you specify in the holiday calendar field."
- Operators: "Use AND when you want to specify only dates that meet both conditions. Use OR
  to specify dates that meet at least one of the conditions." *Model note:* the rule parser
  caps grouping at 100 nested levels (DL-57) and refuses a deeper rule loudly. The only full
  worked condition string in the docs (the federal-holiday example, Define Extended Calendars 12.0) uses `&`,
  `|`, and parentheses exclusively — for example `(jan&workd#1)|((jan|feb)&mon#3)|(may&monm1)|…` —
  the literal words `AND`/`OR` are described but never demonstrated **[?]**. (That example
  string also contains an unbalanced parenthesis in the site's own render — quote with care.)
- `NOT` **[V]**: "If you want to exclude dates for which there is no exclusive date condition
  keyword, enter NOT before the inclusive date condition keyword."
- Multiple rules: "To specify multiple rules, use a comma-separated list" — and the record
  format repeats `condition:` lines. The boolean combination of list entries/lines is never
  stated → Q8d (default: union/OR).
- **Defective tokens [?]** (doc text provably broken, refused by any implementation until a
  live instance decides): `WORKDXnn` — "excludes the nn th workday of the week", a scope that
  contradicts its month-scoped siblings. `CWEK#n`/`CWEK#L`/`CWEKMn` — garbled render ("the
  n th of weekday of each week in each period"). `CWEKXn` — an X-named token whose text says
  "includes the n th day of each period", which duplicates CYCL# semantics.
- **Folk tokens that do not exist**: `DAY#n`, `MONTH#n`, `YEAR#n`, `CYCLE#L` are attested
  nowhere (there is no generic year-scoped token at all). Two synthetic corpus fixtures
  carried invented tokens from before this entry (`CYCLE#L`, `MONTH#L`) — corrected to
  `CWRK#L`/`EOMWORK` when this entry landed.
- Pre-12.x lineage: the public 4.5 user guide has *no* keyword grammar (GUI-only rule dialog,
  rules "not saved after the calendar has been created"). Public 11.3.6 guides lack the
  table (Reference Guide is login-walled). Everything here rests on 12.x **[V for 12.x]**.

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
  filter, not a replacement — the non_workday `O` reads the same for non-workdays).
- `S` — include regardless (no transformation).
- `N` — **one-shot, one calendar day, no re-check**: "Excludes the holiday and includes the
  next day. This applies even if the next day is a holiday or non-workday." (Worked example:
  Dec 25 → Dec 26 "even if December 26th is a holiday".)
- `W` — **iterates to validity**: "Excludes the holiday and includes the next non-holiday
  workday." (Worked example walks Dec 25 → 26 (holiday) → 27 (non-workday) → lands Dec 28.)
- `P` — mirror of W, backward (worked example Dec 25 → 24, 23 (non-workdays) → lands Dec 22).

*Model note:* a W/P walk is capped at 366 days (DL-57). A calendar whose walk finds no valid
target inside that bound is a generation error, which is the degenerate-walk case DL-59 reserves
`CalendarRuleError` for.

The pipeline is **filter-then-replace, single-shot**: O filters restrict the candidate set, and
N/W/P replace excluded candidates with a target day. The two categories are ordered, not
commutative: a specified holiday action runs first and settles every holcal date, so the
non_workday code — O included — never sees one. A weekday holcal date under `holiday: O` plus
`non_workday: O` is therefore kept, where two commuting filters would drop it. A replacement
result is **final** — never re-processed by the other category (holiday-N's "applies even if the next day is a holiday or non-workday" is the direct
evidence). The worked examples exist only under `holiday:`, and the wordings under
`non_workday:` are NOT parallel: W/P say "run on the next work day" / "previous work day"
(a walk to a workday — holiday-ness of the target unstated), while N says "**Specifies to include
the next workday** that also meets all other criteria" — a workday target, not a one-day
shift. Pinned as the next non-holiday workday with the date-conditions not re-checked →
Q8c. A date that is simultaneously a holiday and a non-workday takes the **holiday
action** when one is specified — Q8a resolved *(2026-07-30, DL-58 — Define Extended
Calendars 12.1: "When you specify an action at the Holiday Action prompt, the utility
applies that action to all of the dates listed in the [holiday] calendar. When you do not
specify an action at the Holiday Action prompt, the utility treats the dates listed [in
that calendar] as non-workdays according to the value that you specify at the Non-workday
Action prompt." — an either/or dispatch: with a holiday action present the non_workday
code, filter or replacement, never sees a holcal date)*. **[?]** Q8a residue: whether a
replacement target re-enters the other stage stays unverified (kept single-shot per the
holiday-N wording — one 2012 community report of consecutive holidays under holiday-N hints at
re-processing in 11.x, Q8c's re-entry corner).

`adjust` **[V]** is an *alternative* to the action codes, not a layer above them: "Specify
this option when none of the non-work action and holiday action options indicate the desired
adjustment." It is a **blind fixed offset with no landing-day re-validation** — the docs'
collision example: "the 14th is a holiday and the 15th and 16th are both non-workdays. The
Holiday Action and Non-workday Action prompts do not provide the option to specify a valid
date adjustment, so the job runs according to the value that you specified at the Date
Adjustment prompt, as long as that value is not -1, 0, or +1" — that is, the operator, not
the engine, must pick an offset that avoids excluded days. The offset is **uniform over all
candidates**, not scoped to excluded ones: "The WED#1 date condition indicates the first
Wednesday of the month and the -1 date adjustment sets it to the day before that Wednesday"
(Define Extended Calendars, 12.0) — plain Wednesdays, nothing excluded, still shifted. The
attribute definition's "adjustment to apply to holidays and non-workdays" reads as typical
motivation, not a scope restriction, and the worked examples cover both cases. Whether a
nonzero adjust composes with a non-O/S action code on the same calendar is undocumented →
Q8b. *(Sharpened 2026-07-30, DL-58: the 12.1 adjust prompt — "If you do not need to
replace the excluded days or if you specified replace days using non-workday action or
holiday action values, enter 0" — frames adjust and N/W/P replacement as alternatives.
Also, KB 280764 gives a vendor-worked nonzero-adjust-with-**S** vector — WORKD#1 +
adjust 1 + non_workday S = "the day after the first workday", kept even on a Saturday —
which our pipeline reproduces as-is:
`test_sem38_adjust_composes_with_s_the_vendor_worked_example`. Since DL-59 the
undocumented N/W/P composition runs on the pinned pipeline order — replace, then
shift — instead of refusing (see §9 Q8b).)*

### SEM-39 · Cycles **[V]**
A `cycle:` record is a name, an optional description, and repeated `start_date:`/`end_date:`
`MM/DD/YYYY` pairs — each pair one *period*. Bounds **[V]** (Manage Calendars 12.1): "A cycle
is a list of date ranges or periods. A period is between two and 365 days. A cycle contains a
minimum of two periods and a maximum of 30." (The 12.1.01 WebUI page instead says "You must
add at least one period" — a Broadcom-internal contradiction. Accept ≥1 on input, and never
emit a diagnostic that calls one-period cycles illegal.) Both bounds are recorded as vendor
facts, not as loader validation. A cycle record is refused only when a period is unparseable,
ends before it starts, or the record carries no period at all. Period length and period count
are never refused.

Two indexing modes **[V]**: `CYCLE` is union membership — "includes dates that fall within
any of the periods defined in the cycle" — while the `#`-forms index **each period
independently**: "CYCL#nnn — includes the nnn th day of each period in the cycle" (likewise
CWEEK/CWRK/Cddd families, SEM-37). "Week of a period" anchors to **consecutive 7-day
chunks from each period's first day** — Q8e resolved *(2026-07-30, DL-58 — Broadcom
community worked example, Broadcom staff: a quarterly cycle with `condition: CWEEK#01 |
CWEEK#02` schedules "the first 14 days in every quarter" — period-start chunks, not
calendar weeks. The ragged last chunk is the arithmetic consequence, not separately
worked)*.

Cycles in the text format are literal and non-recurring — no record attribute repeats them.
The only recurrence control found is a WebUI-only "Repeat every year check box" (12.1.01).
The community "set the year to 1972 to recur" lore is contradicted by that page and attested
nowhere — disregard. Vendor exhaustion mechanics **[C]** (KB 186017 + community
corroboration, no TechDocs page): the engine materializes ~a year of dates and "on the last
day in the existing calendar when the job runs autosys will see that there are no future
dates and automatically generate one year worth of new dates". Command-line autocal_asc has
no regeneration verb. Runner note: dsl41's scheduler evaluates eligibility lazily per day
(runner-design §5), which is equivalent to an always-regenerated schedule — the
materialization horizon does not exist for us. Only cycle-bound calendars genuinely exhaust
(dormancy per DL-56).

---

## 5. Attributes with control-flow teeth (quick inventory)

| attribute | semantics class |
|---|---|
| `condition` | predicate algebra (§1) |
| `box_name`, `box_success`, `box_failure`, `box_terminator`, `job_terminator` | container semantics (§2) |
| `date_conditions` + time cluster | scheduling (§4) |
| `max_exit_success`, `success_codes`, `fail_codes` | success-boundary shift (SEM-09); `fail_codes` decides alone, else `success_codes` replaces the rule, else the threshold |
| `term_run_time` | auto-terminate after n minutes → TERMINATED **[V]** |
| `n_retrys` | auto-restart on FAILURE only: application failures (vendor's examples: "cannot find a file or a command, permissions are not properly set"). A TERMINATED job "does not restart"; system/network failures restart via the scheduler's `MaxRestartTrys` config parameter instead **[V]** (Q4 resolved, DL-53) |
| `auto_hold` | box member enters ON_HOLD automatically when box starts **[C/?]** |
| `auto_delete` | definition lifecycle, not runtime — carried in IR-F `JobIR.passthrough` |
| `status` (on insert) | definition-time out-of-band state (SEM-24) **[V]** existence / **[?]** full value set |
| `job_load`/`priority`/`machine_method`/QUE_WAIT, `machine` lists | LEGACY (pre-11.3) load-balancing model — IR carries opaquely; the runner's oracle NOW honors `job_load` vs machine `max_load` as a capacity bucket and `priority` as QUE_WAIT waiter ordering (DL-50); pool `machine:` lines are typed `MachineMember` rows since DL-49 and preflight resolves their locality, but `machine_method`, member selection/routing and per-member `factor`/`max_load` stay opaque placement |
| `std_in_file`, `envvars` | CMD exec cluster (12.x sweep, DL-32): stdin redirect (may reference a blob) + NAME=value environment list; typed carry on ExecSpec, `$$VAR` sites indexed (SEM-08) |
| `ulimit`, `elevated`, `interactive`, `job_class` | OS/agent-side exec tuning **[V]** (TechDocs 12.x) — inert carry (DL-32) |
| `chk_files` | pre-start disk-space gate **[V]**: agent checks required space; unmet → alarm and the job does NOT start — Resource-Wait class; opaque carry v1, NO oracle gate (a real disk level is out of a pure simulator's reach; distinct from `resources:`, which the oracle now honors as capacity semaphores, DL-50) |
| `heartbeat_interval` | MISSING_HEARTBEAT alarm only **[V]** — observability (DL-32) |
| `avg_runtime` | statistics seed at insert **[V]** — inert carry (DL-32) |
| `resources` + `insert_resource`/`update_resource`/`delete_resource` | 11.3+ resource objects **[V]** (TechDocs 12.x): `resources: (name, QUANTITY=n[, FREE=Y\|N\|A]) AND (...)`; FREE: Y=free on success, N=never, A=unconditionally; `res_type: D\|R\|T` (depletable/renewable/threshold), `amount` required, optional agent-level `machine`. Typed carry (DL-21); the runner's oracle NOW honors these as capacity semaphores (DL-50): `amount` is the bucket size, QUANTITY the demand, res_type sets the default release (R free-on-completion / D depletable-never / T level-gate) and FREE overrides it; UCS-09 → UC Virtual Resources |
| `alarm_if_fail`, `alarm_if_terminated`, `min/max_run_alarm`, `send_notification` + `notification_*` family (msg, template, alarm_types, emailaddress[_on_alarm/_on_failure/_on_success/_on_terminated]), `must_*_times` | observability annotations, no control flow (family completed per 12.x notification services, DL-32) |
| `std_out_file` etc. with `$$VAR` | string substitution sites (SEM-08) |
| `watch_file`, `watch_interval`, `watch_file_min_size` (FW jobs) | file-watcher job type: terminal SUCCESS when file condition met — a *source* node in derived graphs |

---

## 6. IR implications (decisions this dossier forces)

1. **Two-layer IR.** Layer F (faithful): jobs as attribute records + parsed condition ASTs +
   box tree, semantics exactly per SEM entries. Layer G (derived): dependency graph extracted
   from Layer F, each edge annotated `exact | assumed | redesign` (E/A/R: exact,
   equivalent-under-assumptions, needs-human), with the assumption named (for example, "assumes producer and consumer share one schedule cycle,
   so latching ≙ run-scoped").
2. **Condition AST node set:** `StatusAtom(job, status, lookback?)`,
   `ExitCodeAtom(job, op, value, lookback?)`, `GlobalAtom(name, op, value)`,
   `And`, `Or`, `Paren` (kept for round-trip fidelity, erased in canonical form).
   Both `StatusAtom` and `ExitCodeAtom` hold a `JobRef`, and cross-instance is that ref's
   `instance` field — so either atom kind can be cross-instance.
3. **Status store in the oracle:** one `JobRuntime` row per job, plus a global map. The row
   carries **two clocks**, not one: `status_at` (the last transition of any kind) and
   `last_end_at` (the last transition into a terminal status). A window lookback reads
   `status_at`; zero lookback (SEM-04, Q2a) compares both jobs' `last_end_at`. One timestamp
   would get SEM-04 wrong after a non-terminal status change. The row also carries
   `exit_code`, `run_number`, the SEM-20/21/22 flags and the SEM-32 arm. Box status = derived
   fold with SEM-11/12 gating rules.
4. **on_ice/on_hold/on_noexec are events in the oracle**, and *rewrites* in static analysis.
5. **Success is per-job** (`max_exit_success`, `success_codes`, `fail_codes` — SEM-09's cited
   precedence) — never hardcode exit 0.
6. §5's non-control-flow attributes take four IR-F lanes, not one. Observability attributes
   land in `JobIR.annotations` and known-inert attributes in `JobIR.passthrough`, both as
   whitespace-trimmed semantic text. `must_start_times`/`must_complete_times` lower to a typed
   `SlaSpec`, and `resources:` to typed `ResourceRef` rows — normalized values, not source
   text. Time attributes without truthy `date_conditions` join `passthrough` as dead
   configuration (SEM-30). Nothing is dropped on any lane; byte-exact text is the AST's job,
   not IR-F's.

## 7. Migration risk register (seed — completed in the stonebranch mapping table)

| # | AutoSys behavior | Risk when mapping to run-scoped DAG (Stonebranch) |
|---|---|---|
| R1 | Latching conditions, no lookback (SEM-01) | Stale success satisfies dependency across days. Naive edge translation *tightens* semantics — it can block flows that relied on latching, or the reverse: the AutoSys flow relied on staleness as a feature |
| R2 | Lookback windows (SEM-04) | No native equivalent. Needs time-window guard tasks or acceptance of changed semantics |
| R3 | on_ice downstream-satisfied (SEM-20) | For each edge type, make sure that "skipped counts as satisfied" holds in Stonebranch skip semantics |
| R4 | Box success gating on external refs (SEM-12) | The hung-RUNNING pattern has no analog — redesign it, do not translate it |
| R5 | run_window closer-edge rule (SEM-33) | Behavioral cliff at the window midpoint. No analog |
| R6 | local unqualified `n()` atoms in `condition` | These are *not* dependencies. Translation as edges creates false ordering — map them to resource/mutex constructs. Only the bare local form reads as mutual exclusion: a lookback-qualified, cross-instance, or `box_success`/`box_failure` `n()` stays an ordinary edge (DL-12) |
| R7 | Global-variable conditions (SEM-08) | Needs a UC variable + event-trigger equivalent. The re-evaluation-on-set semantics must match |
| R8 | FORCE start satisfying downstream latches (SEM-23) | Operational muscle memory changes |

## 8. Trace test index (oracle regression set, one per SEM unless noted)

T01 latching across days (SEM-01) · T02 each atom type truth table (SEM-02) · T03 precedence
pinning (SEM-03: pinned at parse time by the `test_sem03_*` tests, DL-53 — the oracle layer
has no precedence concept of its own) · T04a/b/c lookback window in/out/9999 (SEM-04) ·
T05 iced predecessor in lookback (SEM-05) · T06 undefined job never fires (SEM-06) ·
T08 SET_GLOBAL triggers re-eval (SEM-08) · T09 max_exit_success boundary, T09b fail_codes
decide alone (unlisted → SUCCESS), T09c success_codes replacement, T09d success_codes
ignored beside fail_codes (SEM-09, DL-58 cited composition) ·
T10 unconditioned member starts with box (SEM-10) · T11 default box fold (SEM-11) ·
T12a internal box_success early-exit, T12b external box_success hung-RUNNING,
T12c box_success over a grandchild fires transitively (SEM-12) ·
T13 sticky TERMINATED box (SEM-13) · T14 terminator cascade both directions (SEM-14) ·
T20a ice downstream fires, T20b off-ice does not immediately run (SEM-20) ·
T21a hold blocks downstream, T21b off-hold immediate run (SEM-21) · T22 noexec bypass,
T22b an ON_NOEXEC box goes RUNNING and every member bypasses (SEM-22) ·
T23 force start satisfies latch (SEM-23) ·
T24a initial ON_HOLD blocks then OFF_HOLD releases, T24b initial ON_ICE satisfies downstream
(SEM-24) · T04 zero-lookback since-last-end anchor pinned both directions + Q2b first-run
corner, both cited (SEM-04, DL-54/DL-58: `test_sem04_zero_lookback_*`) · T32 arm-and-wait:
tick arms, edge starts, start consumes — cited, abandon switch deleted (SEM-32,
DL-54/DL-58: `test_sem32_*`) · T33a/b run_window closer-edge both sides + box variant,
T33c the window read in the job's timezone (SEM-33, with SEM-35) · T34a/b must_* emit alarms
only, T34c each start_time arms its own relative offset (SEM-34).

Layer note: not every SEM entry lands in the oracle suite. SEM-07 (cross-instance atoms) is
pinned by the condition, derive and control-plane suites, not by an oracle trace. SEM-15 has its
own oracle test (`test_sem15_idle_box_recompute_derives_status_from_member_changes`). SEM-30 and
SEM-31 are lowering rules, pinned in the IR suite (`test_sem30_*`, `test_sem31_*`). SEM-35 is
pinned by the scheduler suite's `test_resolve_timezone_*` and `test_preflight_timezone_*`
families, which carry no `sem35` in their names, and by T33c in the oracle suite for the
re-basing of `run_window`. SEM-36..39 are calendar rules, pinned in the autocal suite
(`test_sem36_*`..`test_sem39_*`). T34a/b's own `test_sem34a/b_*` cover must_complete only; the
must_start half is pinned by `test_must_start_alarm_fires_when_no_run_began_by_deadline` and
`test_must_start_alarm_quiet_when_the_run_began_in_time`. SEM-16 (definition-time mutation of a
running box) and SEM-17's ACTIVATED state are recorded oracle non-goals v1 and have no trace
test.

## 9. Open questions — resolve against a live instance or deeper doc dive before oracle v1

- Q1 (SEM-03): RESOLVED 2026-07-28 (DL-53, doc sweep) — flat left-to-right, no `&`-over-`|`
  precedence: "The parentheses force precedence, and the equation is evaluated from left to
  right" (TechDocs 12.1, condition attribute page). Pinned by the `test_sem03_*` pinning
  tests. The C-precedence candidate grammar is deleted.
- Q2 (SEM-04): SPLIT by DL-54 (2026-07-28, doc sweep). **Q2a RESOLVED** — lookback `0`
  anchors to the dependent job's own last end time ("examines the last end time of the job
  first. It then examines the last end time of the condition job ... has run since the last
  run of the job for which the condition is coded for" — TechDocs 12.0.01, condition
  attribute page, full quote in SEM-04). The anchor switch is deleted per the DL-06
  protocol. `test_sem04_zero_lookback_*` pin the reading against midnight both directions.
  **Q2b RESOLVED** 2026-07-30 (DL-58, citation sweep) — the first-run case: a dependent
  that never ended has no anchor and the atom is **satisfied**. Broadcom community thread
  760251, CA support best answer: "This is working as designed. When a new job is inserted
  it has no initial/previous end time". The thread reporter observed the epoch-0
  effect. The `# PENDING: Q2b` marker is retired. `test_sem04_zero_lookback_first_run_*`
  keeps the pin.
- Q3 (SEM-32): RESOLVED 2026-07-30 (DL-58, citation sweep — DL-54 flipped the default, this
  closes it) — arm-and-wait is confirmed for the standalone case: "There is a
  STARTJOB event associated with the start_time or run_calendar … The STARTJOB event being
  processed satisfies the start_times/run_calendar dependency", a start "resets" it
  (thread 734033, CA's Mark Hanson, reproduced tests for both start_times and
  run_calendar). The disarm boundary is no-expiry latch-until-consumed: "no set limit
  to how long [it] would wait … regardless of how far in the future", reset only by
  (force-)start or JIL update (thread 801986, Broadcom employee). The abandon switch
  `ORACLE_SCHEDULED_FALSE_CONDITION` is deleted per the DL-06 protocol. **Q3c OPEN**
  (new residue, DL-58): 801986's box aside ("JobB would start immediately after the next
  time its parent box starts") hints that a member's latch can survive into the NEXT box
  run — tension with the DL-54 box-run-scoped arm (`SCHED_DISARM`). The scoped pin stands
  (`# PENDING: Q3c`, oracle.py) and needs one live box test.
- Q4 (§5): RESOLVED 2026-07-28 (DL-53, doc sweep) — FAILURE only. TechDocs 12.0.01 n_retrys:
  "specifies how many times to attempt to restart the job after it exits with a FAILURE
  status. If a job exits with a TERMINATED status, it does not restart."; "This attribute
  applies to application failures (for example, AutoSys Workload Automation cannot find a
  file or a command, permissions are not properly set, and so on). It does not apply to
  system or network failures", which restart under the scheduler's `MaxRestartTrys`
  configuration parameter (cross-confirmed on the 12.1 MaxRestartTrys page: "governs retries
  due to system or network problems ... different from the n_retrys job definition
  attribute"). Modeling
  retries in the oracle/runner remains deliberately out of scope for v1 (DL-53 scope note —
  the runner preflight still WARNs on `n_retrys > 0`).
- Q5: RESOLVED 2026-07-28 (DL-53, doc sweep) — yes, by architectural entailment (no single
  explicit "survives restart" sentence exists in TechDocs — the inference step is deliberate):
  `ujo_event` "Records events that the scheduler has not yet processed" (Events reference,
  12.1); "The event server (database) stores all the objects" and on start the scheduler
  "continually scans the database for events to process" (Architecture, 12.1). There is no
  in-memory-only event queue to lose. Broadcom KB 11013 corroborates operationally (queued
  events run after a multi-hour outage on plain restart). The oracle's event-queue model
  needs no change.
- Q6 (SEM-12): box_success referencing a member that is ON_ICE — does "not scheduled" clause
  apply ("condition not met if the specified job is not scheduled")? NARROWED 2026-07-30
  (DL-58): the `condition:`-atom half is now cited (KB 438836 — local ON_ICE predecessor →
  atom continuously true, lookback ignored — the SEM-05 upgrade), and KB 92872 adds the
  evaluation-trigger nuance (a box_success over global-only terms is re-evaluated at member
  completion moments, not on SET_GLOBAL — consistent with SEM-12's gating). The
  box_success-referencing-an-iced-MEMBER case itself remains uncited. The shared-evaluator
  pin (atom true) stands.
- Q7 (SEM-09/DL-33): RESOLVED 2026-07-30 (DL-58, citation sweep) — KB 408778 states the
  composition: a present `fail_codes` decides alone (listed → FAILURE, "Any other exit
  code … will be interpreted as a success" — success_codes and threshold ignored). Absent
  fail_codes, a present `success_codes` alone decides (unlisted → FAILURE). Neither →
  `max_exit_success`. Three of the four DL-33 corner pins were confirmed. The fourth
  (fail_codes alone, unmatched code → threshold) was WRONG — vendor says SUCCESS — and is
  flipped in `ir.exit_is_success` (shared with the UC twin, M31). The `# PENDING: Q7`
  marker is retired. `test_dl33_exit_is_success_*` / `test_sem09*` pin the cited rules.
- Q8 (SEM-37/38/39, DL-57): extended-calendar generation corners. Unlike the trace-test
  questions, every Q8 item closes mechanically once any live instance exists: define the
  calendar, let autocal materialize the schedule, and diff the vendor date set against
  `dsl41`'s generator — an afternoon for the whole batch.
  - **Q8a** — RESOLVED 2026-07-30 (DL-58, citation sweep): a specified **holiday action
    governs every holcal date outright**. Holcal dates receive non-workday treatment only
    "when you do not specify an action at the Holiday Action prompt" (Define Extended
    Calendars 12.1 — the either/or dispatch quoted in SEM-38). The pre-DL-58 compile-time
    disagreement gate over-refused vendor-valid calendars and is deleted. **[?]** residue:
    whether a replacement target re-enters the other stage (folded into Q8c's re-entry
    corner).
  - **Q8b** — nonzero `adjust` composed with an N/W/P replacement code: undocumented (the
    docs frame adjust as the alternative — the 12.1 prompt says "enter 0 … if you specified
    replace days using non-workday action or holiday action values"). Pinned default since
    DL-59 (deterministic over the earlier fail-closed refusal — an estate calendar must
    schedule): the SEM-38 pipeline order as-is — disposition replaces first, then the
    uniform blind adjust shifts every survivor (**replace-then-shift** — probe signature
    (Aug 15, Aug 18) pinned in `test_q8b_*`). `adjust: 0` alongside any action is inert
    and accepted. Nonzero adjust with **S** is vendor-worked (KB 280764, DL-58) and
    reproduced as-is. Still open on vendor parity (`# PENDING: Q8b`). If instance access
    appears, the runbook's probe pair decides it.
  - **Q8c** — the `non_workday:` replacement targets: N is documented as "include the next
    workday that also meets all other criteria" — pinned as the next **non-holiday
    workday**, with "all other criteria" read as the workday/holiday prompts, NOT the
    date-conditions (`# PENDING: Q8c`). W/P walk to a workday without a re-check of the
    target's holiday-ness (their worked examples exist only under `holiday:`).
    Sharpened 2026-07-30 (DL-58): the O filters are verified (thread 778062 — an
    autocal preview under non_workday O excluded the workday dates, with CA confirming
    "Behavior is not changed … production documentation was wrong", that is, the current
    "workdays only" rendering of O's text in some pages is the KNOWN-BAD one). N's doc
    text churned across eras (11.3.5: "next day, even when also a non-workday" vs current
    12.1: "next workday…" — the implemented pin). Also, a 2012 community report (thread 825395:
    consecutive holidays under holiday-N, the second holiday missing from the output)
    hints that the holiday-N target can be re-processed in 11.x — the cross-stage re-entry
    corner (shared with Q8a's residue) stays open against the current-doc single-shot pin.
  - **Q8d** — rule combination beyond the documented `&`/`|`/parens: (i) comma-separated
    list entries / repeated `condition:` lines — boolean semantics unstated, default:
    inclusive rules union, exclusive (`X`/`NOT`) rules subtract from that union. (ii)
    unparenthesized `&` vs `|` precedence inside one expression — unstated, default flat
    left-to-right (the SEM-03 house style, an analogy not a citation). (iii) the literal
    words `AND`/`OR` — accepted as synonyms of `&`/`|`. Upgraded 2026-07-30 (DL-58): an
    estate calendar demonstrates the literal word (`condition: **/04/** AND
    workd#04`, KB 442457 — the same KB's next-run narrative reads that mask as APRIL,
    which a mm/dd field order cannot produce, so the mask-field reading in that estate is
    under-determined — the AND acceptance is the only part taken). (iv) a compound rule
    with NO inclusive leaf (for example `xtue|xwed`) — neither a recognized exclusion form
    nor a likely authorial intent. Pinned default since DL-59 (deterministic over the
    earlier refusal): literal boolean evaluation as an include — the same complement
    algebra that mixed-polarity rules already use — which accepts that `xtue|xwed` reads
    near-universal (`# PENDING: Q8d`, pinned in `test_q8d_*`). An empty/whitespace
    `condition:` value reads as absent (the SEM-36 empty-value convention) and falls back
    to the DAILY default.
  - **Q8e** — RESOLVED 2026-07-30 (DL-58, citation sweep): CWEEK anchors to consecutive
    7-day chunks from each period's first day — Broadcom staff worked example: quarterly
    cycle + `CWEEK#01 | CWEEK#02` = "the first 14 days in every quarter" (SEM-39). CWRK is
    the nth workday of a period (a count, not a week selector) — already implemented so.
    The `# PENDING: Q8e` marker is retired. The ragged last chunk is the arithmetic
    consequence, not separately worked (accepted implication, not a new question).
  - The defective tokens (SEM-37: `WORKDXnn`, `CWEK#n`/`#L`/`Mn`/`Xn`) are refused outright
    with no default and no switch — nothing to implement until the doc text is fixed or a
    live instance defines them.
- Q9 (SEM-36, DL-57): RESOLVED 2026-07-30 (DL-60) from one observed `autocal_asc` export
  sample. Verdicts, all folded into SEM-36/37 at **[F]**: the export writes
  `extended_calendar:` (ext_calendar: stays accepted). Fixed attribute order with
  empty-valued keys emitted. Comma day codes plus `workday: all`. Braces as condition
  grouping. Case preserved as authored. `WORKD#L` in use. `holiday: S` without holcal.
  `mm/dd/yyyy 00:00:00` standard rows. Repeated-pair cycle records. Five
  interpreter/scanner gaps the sample exposed were fixed the same day (DL-60). A
  synthetic clone of the observed shapes is pinned end-to-end (`test_q9_*`, including
  byte-identical F1). Caveats: one sample, AE version unpinned, not re-verified — the
  weakest evidence tier in this dossier. KB 29387's `autocal_asc -e ALL -E file` remains
  the byte-exact re-verification if a live instance becomes available; parens are
  accepted alongside braces, so no behavior rides on the grouping read.

## Sources
Primary: Broadcom TechDocs, AutoSys Workload Automation 12.0/12.0.01/12.1/12.1.01 — JIL
reference pages (`condition`, `box_success`, `box_failure`, `run_window`, `start_mins`,
`must_complete_times`, `date_conditions`, `n_retrys`), Scheduling guides (Basic Box Job
Concepts, Box Job Completion State, Must Start/Complete Times, Manage Common Job Properties,
Start Conditions, Job States — the DL-54 Q2a/Q3/SEM-21 quotes), the monitoring guide's
Manage Job Events pages (off-hold/STARTJOB event semantics, 12.1.01),
administration pages (`MaxRestartTrys`, `KillSignals`), system-states reference (Events),
Getting Started (AutoSys Architecture), Broadcom KB 186248 (global variables), KB 11013
(scheduler-outage event recovery, Q5 corroboration). Calendar entries (SEM-36..39, DL-57):
Manage Calendars (12.0.01/12.1), Date Condition Keywords (12.0.01/12.1, byte-identical),
Define Extended Calendars (12.0 scheduling guide — the federal-holiday worked example;
12.1.01 menu variant), autocal_asc Command (12.1 reference — renders to raw fetch despite
the nav-shell pattern), Define Standard Calendars (12.1.01), Define a Cycle WebUI (12.1.01),
run_calendar/exclude_calendar attribute pages (12.0), KB 186017 (calendar regeneration),
KB 142758 (bi-weekly extended calendar). DL-58 citation sweep (2026-07-30; raw fetches
saved during the sweep session): KB 408778 (Q7 exit-code precedence), KB 438836 (SEM-05
ON_ICE-lookback citation + cross-instance caveat), KB 92872 (box_success
evaluation-trigger), KB 280764 (adjust+S worked vector), KB 442457 (literal AND in an
estate calendar; CAUAJM_W_10119/10120 exhausted-calendar warnings), KB 29387 (autocal_asc
export/import commands), KB 14195 (ujo_calendar materialization, 365 days),
KB 135770 (job_depends via the Application Server); community threads 760251 (Q2b, CA
support), 734033 (Q3a/E11, CA's Mark Hanson worked examples), 801986 (Q3b no-expiry +
the Q3c box aside, Broadcom staff), 778062 (non_workday-O preview + "documentation was
wrong" correction), 825395 (2012 consecutive-holiday-N community report), and the CWEEK
quarterly worked example (Q8e, Broadcom staff). Secondary corroboration: legacy CA User
Guide excerpts and practitioner references (on_hold/on_ice operational behavior, state
definitions); the public 4.5 user guide and 11.3.6 user/admin guides were full-text checked
and contain no keyword grammar (SEM-37 lineage note).
