# AutoSys Semantics Dossier

Status: draft v0.1 · Session A deliverable · verified against Broadcom TechDocs (AE 12.x) where noted
Purpose: normative reference for the IR design, the linter rule set, the semantics oracle
(discrete-event interpreter), and the AutoSys→Stonebranch mapping table (Session B).

Every numbered SEM entry implies at least one trace test against the semantics oracle.
Confidence levels: **[V]** verified against Broadcom TechDocs 12.x · **[C]** corroborated by
multiple secondary sources · **[?]** open question, verify against a live instance before relying on it.

---

## 0. Execution model (the frame everything else hangs on)

AutoSys is **not** a DAG engine. It is an event-driven state machine engine:

- Every job is a state machine: `INACTIVE → STARTING → RUNNING → {SUCCESS, FAILURE, TERMINATED}`,
  plus out-of-band states `ON_HOLD`, `ON_ICE`, `ON_NOEXEC`, `QUE_WAIT`, `ACTIVATED`, `RESTART`.
- The event processor (scheduler) reacts to events (status changes, timers, sendevent commands,
  global-variable sets). On each relevant event it re-evaluates the starting conditions of
  potentially affected jobs.
- A job starts when ALL of the following hold simultaneously **[C]**:
  1. date/time conditions are met (if `date_conditions` is set),
  2. the `condition` expression evaluates true,
  3. if the job is in a box, the box is in `RUNNING` state,
  4. the job itself is not `ON_HOLD` / `ON_ICE`,
  5. (`run_window`, if present, additionally gates the actual start — see SEM-40).

**IR consequence:** the faithful layer of the IR models jobs as state machines and conditions as
predicates over a status store. The DAG is a *derived* artifact with per-edge confidence
annotations, never the primary representation.

---

## 1. Conditions: predicate algebra over the status store

### SEM-01 · Conditions are latching state predicates, not edges **[V]**
`condition: s(JobA)` is satisfied if JobA's *current recorded status* is SUCCESS — regardless of
when that status was set. A JobA success from last Tuesday satisfies `s(JobA)` today unless a
lookback qualifier restricts it. This is the single most important divergence from run-scoped
DAG engines (Stonebranch workflow edges are per-run).
*Trace test:* JobA succeeds at T0; JobB defined later with `condition: s(JobA)` and a
FORCE-triggered evaluation at T0+72h → JobB starts.

### SEM-02 · Condition atoms **[V]**
- `success(j)` / `s(j)` — status == SUCCESS
- `failure(j)` / `f(j)` — status == FAILURE
- `done(j)` / `d(j)` — terminal: SUCCESS, FAILURE, or TERMINATED
- `terminated(j)` / `t(j)` — status == TERMINATED
- `notrunning(j)` / `n(j)` — status is anything except STARTING, RUNNING, WAIT_REPLY, RESTART,
  SUSPENDED (i.e., true also for never-run/INACTIVE jobs) **[V]** — commonly used for mutual
  exclusion, not sequencing.
- `exitcode(j) OP value` / `e(j) OP value` — comparison operators against the last exit code.
- `value(GLOBAL) OP value` / `v(...)` — global-variable comparison.
Identifiers are case-insensitive; one-letter abbreviations are canonical short forms.

### SEM-03 · Operators and grouping **[V]**
`AND`/`&`, `OR`/`|`, parentheses for precedence; evaluation is strictly left-associative flat
— `&` does **not** bind tighter than `|`; C-style precedence is wrong for JIL. *(Resolved
2026-07-28, Q1 close, DL-53: TechDocs 12.1 "condition Attribute — Define Starting Conditions
for a Job" states "The parentheses force precedence, and the equation is evaluated from left
to right." Encoded as pinning tests `test_sem03_flat_left_to_right_precedence_pinned`
(grammar shape) and `test_sem03_precedence_pinned_model_level` (Cond model); the C-style
candidate grammar rule is deleted.)*
`NOT` does not exist as an operator; negation is expressed via status atoms (`n()`, `f()` etc.).

### SEM-04 · Lookback qualifiers **[V]**
Syntax: `s(job, hhhh.mm)` (or escaped colon `hhhh\:mm`).
- `s(job, 2)` — satisfied only if the status was reached within the last 2 hours. Sub-hour
  windows require leading `00`: `00.30` = 30 min; bare `30` = 30 *hours*; `.30` is invalid. **[V]**
- `s(job, 0)` — "zero lookback": satisfied iff the condition job ended at-or-after the
  **dependent job's own last end**. **[V]** *(Q2a, resolved 2026-07-28, DL-54 doc sweep;
  TechDocs 12.0.01, condition attribute page: "When you specify 0, AutoSys Workload
  Automation examines the last end time of the job first. It then examines the last end
  time of the condition job. If the condition job has run since the last run of the job for
  which the condition is coded for, the job is allowed to start. If the condition job has
  not run since the last run of the job for which the condition is coded for, the job is
  not allowed to start." The page's own phrase "the job for which the condition is coded
  for" disambiguates the anchor as the dependent. The superseded midnight reading is
  discriminated both directions by the `test_sem04_zero_lookback_*` pinning tests; its
  switch is deleted per the DL-06 protocol.)* **[?]** Q2b residue: the first-run case (the
  dependent has never ended — no anchor exists) is undocumented; pinned as unbounded
  (satisfied), needs a live test. For box overrides the box itself is the evaluator/anchor.
- `s(job, 9999)` — explicit "indefinite lookback", equivalent to no qualifier (legacy 4.5.1
  default). **[V]**
- Lookback applies to **status, cross-instance/external status, and exitcode atoms only —
  never to `value()` global-variable atoms**. **[V]** (Linter: lookback on `v()` = error.)
- Max lookback ≈ 416.58 days (9999.59). **[V]**

### SEM-05 · ON_ICE predecessors inside lookback conditions **[V]**
If the predecessor job referenced in a lookback condition is currently ON_ICE, the atom
evaluates **true** and the lookback is ignored entirely. (Interacts with SEM-20.)

### SEM-06 · Undefined jobs in conditions **[V]**
A condition atom referencing a job that does not exist in the database evaluates **false,
permanently and silently** — the dependent job simply never auto-starts. AutoSys ships
`job_depends` to detect this. This is linter rule #1 (dangling reference), severity: error.

### SEM-07 · Cross-instance atoms **[V]**
`s(jobB^PRD)` — same predicate algebra against a job on external instance `PRD` (declared via
`insert_machine`/external-instance JIL with `xtype`). Lookback applies. For the migration these
become boundary markers in the IR: dependencies whose producer is outside the modeled universe.

### SEM-08 · Global variables **[V]**
- Set via `sendevent -E SET_GLOBAL -G NAME=value` or `insert_global` JIL.
- In conditions: `value(NAME) = X` (also `>`, `<`, `!=` comparisons).
- In attribute strings (command, std_out_file, …): `$$NAME` or `$${NAME}` substitution at
  runtime. Single-`$` is shell/environment, double-`$$` is AutoSys global — the parser must
  keep these distinct. Setting a global is an event that triggers condition re-evaluation.

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
single source: `ir.exit_is_success`, shared with the UC twin (M31). The docs do NOT state
the composition when several are present — that is **Q7** (§9); the implemented default is
the conservative direction (never invent a SUCCESS): fail_codes wins; a present
success_codes replaces the success rule entirely (unmatched code → FAILURE, threshold
ignored); fail_codes alone falls through to the threshold for unmatched codes.

---

## 2. Boxes

### SEM-10 · Box membership and start rule **[V]**
`box_name: B` puts a job in box B. Members start when: box is RUNNING **and** the member's own
conditions hold. Members with no conditions start immediately when the box starts. A member runs
**at most once per box execution**. **[V]**

### SEM-11 · Box RUNNING/completion **[V]**
The box stays RUNNING while any member is running; it cannot complete until all members have
run (or been bypassed). Default: box SUCCESS iff all members ended SUCCESS; box FAILURE if at
least one member failed (evaluated after all members complete).

### SEM-12 · box_success / box_failure override — with evaluation gating **[V]**
`box_success: <condition expr>` (same predicate language). Subtle, verified semantics:
- If the referenced job is **inside** the box: the box status is evaluated the moment that job
  enters the specified state, regardless of other members.
- If the referenced job is **outside** the box (or an external job, or a global): the box
  status is evaluated when *some member completes after* the external condition became true.
  If all members complete *before* the external condition is met, the box does **not** get
  evaluated and stays RUNNING — a classic hung-box production incident. Linter: warn on
  box_success/box_failure referencing non-member jobs.
- If box_success specified but not met, and box_failure unspecified → default failure logic
  applies after all members complete (and vice versa); if neither fires, the box remains
  RUNNING indefinitely. **[V]**

### SEM-13 · Box TERMINATED is sticky **[V]**
A box moved to TERMINATED (e.g., KILLJOB) stays TERMINATED regardless of later member state
changes, until the next box start.

### SEM-14 · box_terminator / job_terminator **[V/C]**
Control flow, not alarms:
- `box_terminator: 1` on a member — if this member FAILs, terminate the containing box.
- `job_terminator: 1` on a member — if the containing box terminates/fails, terminate this member.
Members killed this way get status TERMINATED (which matters for `d()`/`t()` consumers).

### SEM-15 · Member status changes can ripple upward **[C]**
A CHANGE_STATUS/FORCE_STARTJOB on a member of a *non-running* box can change the box's derived
status and thereby trigger downstream jobs conditioned on the box. The oracle must model box
status as derived state re-evaluated on member events.

### SEM-16 · Jobs added to a RUNNING box **[V]**
Inserting/moving a job into a running box: ALERT event; the job's run number is set to the
box's; a STARTJOB is issued if the job isn't STARTING/RUNNING/ON_ICE and its run number does
not exceed the box's. Not migration-critical (definition-time mutation), but the AST layer must
not assume static membership; note as out-of-scope for the oracle v1.

### SEM-17 · Deep nesting **[C]**
Boxes nest arbitrarily (practical guidance: ≤ 1000 members, avoid organizational grouping —
Broadcom's own guidance is boxes for *shared starting conditions*). ACTIVATED state = "top-level
box is RUNNING, member not yet started."

---

## 3. Out-of-band status manipulation

### SEM-20 · ON_ICE **[V]**
- Job will not run; removed from all conditions/logic.
- **Downstream conditions treat the iced job as satisfied** (runs "as though it succeeded");
  inside a box, a member depending on an iced sibling starts immediately when the box runs. **[V]**
- OFF_ICE: the job does **not** run even if its starting conditions currently hold; it waits for
  conditions to *reoccur*. **[V]**
- IR: on_ice ≙ graph rewrite "excise node, short-circuit its outgoing dependency edges to true".

### SEM-21 · ON_HOLD **[V]**
- Job will not run; **downstream is blocked** (conditions on it do not become true).
- OFF_HOLD: if starting conditions are *already satisfied*, the job runs immediately (missed
  runs during hold collapse to at most one run). **[V]** *(Re-verified verbatim 2026-07-28,
  DL-54; TechDocs 12.0, Start Conditions: "When you take jobs off hold, the scheduler does
  not re-evaluate date and time conditions. Jobs that meet their date and time conditions
  while they are on hold start immediately after they are taken off hold unless other
  starting conditions apply and are not satisfied." Oracle: a scheduled tick landing on a
  held job ARMS it — the same Q3 latch as SEM-32 — so OFF_HOLD starts it through the
  schedule gate. The page's run-window exception — off-hold outside the run window
  reschedules "to their next start time" — stays governed by SEM-33's verified closer-edge
  rule; the two sources are not fully reconciled, noted, not modeled apart.)*
- In a box: a held member prevents box completion — holds the whole stream.
- IR: on_hold ≙ pause node, edges intact.

### SEM-22 · ON_NOEXEC **[V]**
Bypass-execution mode: scheduler processes the job through its lifecycle but does not execute;
the job (and boxes containing it) evaluate as SUCCESS; downstream runs normally. Box in
ON_NOEXEC scheduled to run → goes RUNNING, members are bypassed to SUCCESS as their conditions
are met, box returns to ON_NOEXEC afterward. Manual status changes to members while the box is
ON_NOEXEC are overridden by the bypass. This is the "dry-run wiring" state — useful target
concept for our own simulator semantics.

### SEM-23 · FORCE_STARTJOB vs STARTJOB **[C]**
STARTJOB honors nothing extra (it *is* the normal start event); FORCE_STARTJOB starts the job
regardless of conditions. Force-started runs still emit normal status events → downstream
latching conditions get satisfied by forced runs. The oracle needs both as injectable events.

### SEM-24 · `status:` at definition time **[V]** (existence) / **[?]** (full value set)
Estate-shaped JIL carries `status: ON_HOLD` on `insert_job` (observed 2026-07-09 in
migration-input samples, incl. on box jobs): the job is created already in an out-of-band
state, equivalent to inserting it and immediately sendevent-ing the state.
*(Upgraded 2026-07-10, 12.x doc sweep: TechDocs 12.0.01 documents "status Attribute — Set
an Initial Status for a Job During Insertion", with the constraint that it cannot be used
with update_job/override_job — existence is now **[V]**. The page's exact documented value
list is still unretrieved **[?]**; the modeled set stays `INACTIVE` (the implicit default)
plus the SEM-20/21/22 states `ON_HOLD` / `ON_ICE` / `ON_NOEXEC`, and anything else remains
a loud lowering error — extend deliberately when the page or an estate shape shows more.)*
- Lowering: `Semantics.initial_status`; any other value (in particular run states like
  `SUCCESS`, which would interact with the SEM-01 latch) is a loud lowering error — extend
  deliberately when an estate shape shows one, never guess.
- Oracle: seeds the SEM-20/21/22 flags before the first event; no trace entry (definition
  state, not a transition).
- UC mapping: M20 (Hold, E-class) covers `ON_HOLD`; ice/noexec follow their SEM-20/22 rows.
  The compile twin does not model definition-time state v1 and records it in the exclusion
  ledger instead (DL-18).

---

## 4. Date/time scheduling

### SEM-30 · date_conditions is the master switch **[V]**
Time attributes (`days_of_week`, `run_calendar`, `exclude_calendar`, `start_times`,
`start_mins`, `must_start_times`, `must_complete_times`, `timezone`) are honored only when
`date_conditions: 1`. Without it, the job runs purely on conditions/manual events, and the
time attributes are ignored (linter: warn on time attributes present with date_conditions
absent/0 — dead configuration).

### SEM-31 · Mutual exclusivity **[V]**
- `start_times` XOR `start_mins` (both → JIL error, both ignored).
- `days_of_week` XOR `run_calendar` (cannot combine). `exclude_calendar` subtracts days from
  whichever is active.
Time attributes on a job inside a box: the member still needs the box RUNNING; a scheduled
member of a non-running box does not fire (schedule + box gate compose with AND).

### SEM-32 · start_times / start_mins **[V]**
`start_times: "10:00, 11:00"` — absolute times of day (24h). `start_mins: 10,20,30` — minutes
past *every* hour. Each firing inserts a STARTJOB event; time and condition compose as AND.
Default reading since DL-54 (2026-07-28): **time-gated arm then wait** — a tick whose
`condition` is still false ARMS the job; the condition edge later starts it, the start
consumes the arm (at most one run per tick). The doc lean, assembled 2026-07-28 (an
entailment, not one dispositive sentence — the inference is stated deliberately, Q5-style):
box members explicitly wait ("When a box starts running, the status of all the jobs it
contains ... changes to ACTIVATED"; "Maintains jobs with additional starting conditions in
the ACTIVATED state until those additional dependencies are met" — TechDocs 12.0, Basic Box
Job Concepts; Job States: ACTIVATED "the job itself is waiting to start"); the governing
rule is a continuously evaluated AND ("All defined starting conditions must be true for a
job to start" — Start Conditions, 12.0); and the held-across-tick case is documented as
start-on-release (SEM-21). **[?]** Q3 residue: no page narrates the *standalone*
time+condition case in one sentence, and the disarm boundary (does an unconsumed arm expire
at day end / next tick?) is undocumented — pinned as latch-until-consumed. The superseded
abandon reading stays live behind `ORACLE_SCHEDULED_FALSE_CONDITION` (PENDING: Q3). Ticks
blocked at ON_ICE (SEM-20 reoccurrence) or at box-not-RUNNING do NOT arm — including a
HELD member of a not-running box — and a tick on an already-live job does not arm
(pinned). Review-hardened pins (DL-54 adversarial round): a member's arm is scoped to the
box run that armed it — an unconsumed member arm dies at box completion (`SCHED_DISARM`
trace marker), so no member ever auto-starts a later box run from a stale tick; a
pre-existing arm survives ON_ICE/OFF_ICE untouched (ice neither arms nor disarms — the
latched tick predates the ice); the ACTUAL start consumes the arm (a DL-50 QUE_WAIT
enqueue keeps it latched, so a cancelled queue attempt does not eat the tick; KILLJOB on
the queued job consumes it — the kill happened); an armed job re-blocked by run_window
re-uses one pending defer timer per opening instant. Accepted consequence of
latch-until-consumed: an armed run may land in a LATER run_window cycle than its tick
(the SEM-33 closer-edge rule applies at the armed start's own moment).

### SEM-33 · run_window is a gate, not a trigger **[V]**
`run_window: "02:00-04:00"` — not a starting condition; an additional constraint on when a
start may actually occur. If conditions become true outside the window, AutoSys picks: closer
to next window opening → schedule STARTJOB at window open; closer to previous window's end →
do not run, set INACTIVE. **[V]** Max span 24h; may cross midnight. The "closer edge" rule is a
prime migration hazard (no direct Stonebranch analog) — always flag in mapping.
Box interaction (verified example): member with run_window + start time inside a box started
after the window → member INACTIVE so the box can complete, or STARTJOB queued for next window
keeping the box RUNNING overnight, depending on which edge is closer.

### SEM-34 · must_start_times / must_complete_times are alarms only **[V]**
They emit MUST_START_ALARM / MUST_COMPLETE_ALARM; they do not affect control flow. Absolute or
relative (`+n` minutes from each start time) — not mixed; count must match the number of
start_times (JIL insert error otherwise); relative may cross ≤ 2 calendar days; each
must_complete must precede the next start. IR: model as SLA annotations, not semantics.
(Contrast `term_run_time`: that one *is* control flow — auto-TERMINATE after n minutes.)

### SEM-35 · timezone **[V]**
Per-job `timezone:` re-bases all time attributes of that job. IR must carry tz per schedule
block; equivalence of schedules is tz-aware.
Scope re-verified 2026-07-09 (DL-23): TechDocs' own `date_conditions` page lists `timezone`
(with `run_window` and `must_*_times`) among the attributes date_conditions gates, and the
`timezone` page describes only "the job's time settings" — so timezone without truthy
date_conditions is dead configuration (SEM-30/L005 stands). The formerly-open Q2-adjacent
corner (does timezone re-base the zero-lookback midnight anchor?) DISSOLVED with Q2a
(DL-54): the anchor is the dependent's own last end — a comparison of two instants that no
timezone re-basing can affect — so timezone on a condition-only job is unconditionally dead
and L005 fires without the old caveat. Estates that carry timezone as convention can
`dsl41 lint --suppress L005`.

---

## 5. Attributes with control-flow teeth (quick inventory)

| attribute | semantics class |
|---|---|
| `condition` | predicate algebra (§1) |
| `box_name`, `box_success`, `box_failure`, `box_terminator`, `job_terminator` | container semantics (§2) |
| `date_conditions` + time cluster | scheduling (§4) |
| `max_exit_success` | success-boundary shift (SEM-09) |
| `term_run_time` | auto-terminate after n minutes → TERMINATED **[V]** |
| `n_retrys` | auto-restart on FAILURE only: application failures (vendor's examples: "cannot find a file or a command, permissions are not properly set"). A TERMINATED job "does not restart"; system/network failures restart via the scheduler's `MaxRestartTrys` config parameter instead **[V]** (Q4 resolved, DL-53) |
| `auto_hold` | box member enters ON_HOLD automatically when box starts **[C/?]** |
| `auto_delete` | definition lifecycle, not runtime — AST passthrough |
| `status` (on insert) | definition-time out-of-band state (SEM-24) **[V]** existence / **[?]** full value set |
| `job_load`/`priority`/`machine_method`/QUE_WAIT, `machine` lists | LEGACY (pre-11.3) load-balancing model — IR carries opaquely; the runner's oracle NOW honors `job_load` vs machine `max_load` as a capacity bucket and `priority` as QUE_WAIT waiter ordering (DL-50); `machine_method`/`machine` lists stay opaque placement |
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
   from Layer F, each edge annotated `exact | equivalent-under-assumptions | needs-human`,
   with the assumption named (e.g., "assumes producer and consumer share one schedule cycle,
   so latching ≙ run-scoped").
2. **Condition AST node set:** `StatusAtom(job, status, lookback?)`,
   `ExitCodeAtom(job, op, value, lookback?)`, `GlobalAtom(name, op, value)`,
   `And`, `Or`, `Paren` (kept for round-trip fidelity, erased in canonical form).
   Cross-instance = `StatusAtom` with `instance` field.
3. **Status store in the oracle:** map job → (status, timestamp, exit_code, run_number),
   plus global map. Lookback = timestamp comparison. Box status = derived fold with
   SEM-11/12 gating rules.
4. **on_ice/on_hold/on_noexec are events in the oracle**, and *rewrites* in static analysis.
5. **Success is per-job** (max_exit_success) — never hardcode exit 0.
6. Everything in §5 marked "annotation/opaque" goes into the AST passthrough bag and survives
   round-trips untouched.

## 7. Migration risk register (seed — completed in Session B mapping table)

| # | AutoSys behavior | Risk when mapping to run-scoped DAG (Stonebranch) |
|---|---|---|
| R1 | Latching conditions, no lookback (SEM-01) | Stale success satisfies dependency across days; naive edge translation *tightens* semantics — may block flows that relied on latching, or the reverse: AutoSys flow relied on staleness as a feature |
| R2 | Lookback windows (SEM-04) | No native equivalent; needs time-window guard tasks or acceptance of changed semantics |
| R3 | on_ice downstream-satisfied (SEM-20) | Stonebranch skip semantics must be checked for "skipped counts as satisfied" per edge type |
| R4 | Box success gating on external refs (SEM-12) | Hung-RUNNING pattern has no analog; must be redesigned, not translated |
| R5 | run_window closer-edge rule (SEM-33) | Behavioral cliff at window midpoint; no analog |
| R6 | `n()` mutual-exclusion conditions | These are *not* dependencies; translating them as edges creates false ordering — map to resource/mutex constructs |
| R7 | Global-variable conditions (SEM-08) | Needs UC variable + event-trigger equivalent; re-evaluation-on-set semantics must match |
| R8 | FORCE start satisfying downstream latches (SEM-23) | Operational muscle memory changes |

## 8. Trace test index (oracle regression set, one per SEM unless noted)

T01 latching across days (SEM-01) · T02 each atom type truth table (SEM-02) · T03 precedence
pinning (SEM-03: pinned at parse time by the `test_sem03_*` tests, DL-53 — the oracle layer
has no precedence concept of its own) · T04a/b/c lookback window in/out/9999 (SEM-04) ·
T05 iced predecessor in lookback (SEM-05) · T06 undefined job never fires (SEM-06) ·
T08 SET_GLOBAL triggers re-eval (SEM-08) · T09 max_exit_success boundary, T09b fail_codes
carve-out, T09c success_codes replacement, T09d fail-wins overlap (SEM-09/DL-33, Q7 corners) ·
T10 unconditioned member starts with box (SEM-10) · T11 default box fold (SEM-11) ·
T12a internal box_success early-exit, T12b external box_success hung-RUNNING (SEM-12) ·
T13 sticky TERMINATED box (SEM-13) · T14 terminator cascade both directions (SEM-14) ·
T20a ice downstream fires, T20b off-ice does not immediately run (SEM-20) ·
T21a hold blocks downstream, T21b off-hold immediate run (SEM-21) · T22 noexec bypass (SEM-22) ·
T23 force start satisfies latch (SEM-23) ·
T24a initial ON_HOLD blocks then OFF_HOLD releases, T24b initial ON_ICE satisfies downstream
(SEM-24) · T04 zero-lookback since-last-end anchor pinned both directions + Q2b first-run
corner (SEM-04, DL-54: `test_sem04_zero_lookback_*`) · T32 arm-and-wait: tick arms, edge
starts, start consumes; abandon kept under the Q3 switch (SEM-32, DL-54:
`test_sem32_*`) · T33a/b run_window closer-edge both sides + box variant (SEM-33) ·
T34 must_* emit alarms only (SEM-34).

## 9. Open questions — resolve against a live instance or deeper doc dive before oracle v1

- Q1 (SEM-03): RESOLVED 2026-07-28 (DL-53, doc sweep) — flat left-to-right, no `&`-over-`|`
  precedence: "The parentheses force precedence, and the equation is evaluated from left to
  right" (TechDocs 12.1, condition attribute page). Pinned by the `test_sem03_*` pinning
  tests; the C-precedence candidate grammar is deleted.
- Q2 (SEM-04): SPLIT by DL-54 (2026-07-28, doc sweep). **Q2a RESOLVED** — lookback `0`
  anchors to the dependent job's own last end time ("examines the last end time of the job
  first. It then examines the last end time of the condition job ... has run since the last
  run of the job for which the condition is coded for" — TechDocs 12.0.01, condition
  attribute page; full quote in SEM-04). The anchor switch is deleted per the DL-06
  protocol; `test_sem04_zero_lookback_*` pin the reading against midnight both directions.
  **Q2b OPEN** — the first-run case (dependent never ended: no anchor) is undocumented;
  pinned as unbounded (satisfied), `# PENDING: Q2b` in oracle.py; needs one live test
  (define A→B with s(A,0), run A once, never run B, check whether B fires).
- Q3 (SEM-32): still open, NARROWED by DL-54 — default flipped to arm-and-wait (the doc
  lean: ACTIVATED wait state for box members, the continuously-evaluated AND rule, the
  SEM-21 hold-across-tick start-on-release; see SEM-32 for the stated entailment). Residue
  for a live instance: the standalone time+condition case has no dispositive sentence, and
  the disarm boundary (arm expiry at day end / next tick vs latch-until-consumed — pinned
  the latter) is undocumented. The abandon reading stays behind
  `ORACLE_SCHEDULED_FALSE_CONDITION` (`# PENDING: Q3`).
- Q4 (§5): RESOLVED 2026-07-28 (DL-53, doc sweep) — FAILURE only. TechDocs 12.0.01 n_retrys:
  "specifies how many times to attempt to restart the job after it exits with a FAILURE
  status. If a job exits with a TERMINATED status, it does not restart."; "This attribute
  applies to application failures (for example, AutoSys Workload Automation cannot find a
  file or a command, permissions are not properly set, and so on). It does not apply to
  system or network failures", which restart under the scheduler's `MaxRestartTrys` config
  parameter (cross-confirmed on the 12.1 MaxRestartTrys page: "governs retries due to system
  or network problems ... different from the n_retrys job definition attribute"). Modeling
  retries in the oracle/runner remains deliberately out of scope for v1 (DL-53 scope note;
  the runner preflight still WARNs on `n_retrys > 0`).
- Q5: RESOLVED 2026-07-28 (DL-53, doc sweep) — yes, by architectural entailment (no single
  explicit "survives restart" sentence exists in TechDocs; the inference step is deliberate):
  `ujo_event` "Records events that the scheduler has not yet processed" (Events reference,
  12.1); "The event server (database) stores all the objects" and on start the scheduler
  "continually scans the database for events to process" (Architecture, 12.1). There is no
  in-memory-only event queue to lose; Broadcom KB 11013 corroborates operationally (queued
  events execute after a multi-hour outage on plain restart). The oracle's event-queue model
  needs no change.
- Q6 (SEM-12): box_success referencing a member that is ON_ICE — does "not scheduled" clause
  apply ("condition not met if the specified job is not scheduled")?
- Q7 (SEM-09/DL-33): composition of `success_codes`/`fail_codes`/`max_exit_success`. The
  docs give formats and each attribute's absence-default but no precedence. Implemented
  default (`# PENDING: Q7` in `ir.exit_is_success`): fail_codes wins over success_codes;
  under a present success_codes an unmatched code is FAILURE and the threshold is ignored;
  fail_codes alone falls through to the threshold. Pin all four corners with tiny throwaway
  jobs on a live instance (a code in both lists · in neither list with success_codes set ·
  success_codes + max_exit_success both set · fail_codes alone with an unmatched code).

## Sources
Primary: Broadcom TechDocs, AutoSys Workload Automation 12.0/12.0.01/12.1/12.1.01 — JIL
reference pages (`condition`, `box_success`, `box_failure`, `run_window`, `start_mins`,
`must_complete_times`, `date_conditions`, `n_retrys`), Scheduling guides (Basic Box Job
Concepts, Box Job Completion State, Must Start/Complete Times, Manage Common Job Properties,
Start Conditions, Job States — the DL-54 Q2a/Q3/SEM-21 quotes), the monitoring guide's
Manage Job Events pages (off-hold/STARTJOB event semantics, 12.1.01),
administration pages (`MaxRestartTrys`, `KillSignals`), system-states reference (Events),
Getting Started (AutoSys Architecture), Broadcom KB 186248 (global variables), KB 11013
(scheduler-outage event recovery, Q5 corroboration). Secondary corroboration: legacy CA User
Guide excerpts and practitioner references (on_hold/on_ice operational behavior, state
definitions).
