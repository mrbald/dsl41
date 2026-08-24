# Live-instance runbook — closing the DL-58 residue

Purpose: this runbook gives anyone with shell access all that is
necessary to close the remaining AutoSys-behavior questions on a box that
has Workload Automation AE installed. It also gives the source catalog
that the DL-58 citation sweep used, so that future document research does
not start cold. This runbook is a companion to the §9 ledger of the
dossier (docs/autosys-semantics.md) and to the live-instance section of
CLAUDE.md.

Scope: the questions here are the AutoSys ones. Two groups sit outside
it. U1, U3b and U6b need a Universal Controller instance, not an AE box;
docs/stonebranch-semantics.md Part III states all three, and the
live-instance section of CLAUDE.md gives the work order for U3b only.
The runner's own parity defaults E5, E6, E7, E9 and E10 have no probe
here (docs/runner-design.md ss15 states each default).

Ground rules (constitutional, see CLAUDE.md):
- Give each object that you create on the instance the prefix `dsl41_`.
  If a schedule is involved, use a future date. Delete each object the
  same day.
- A calendar that no job references is inert. A job with no date
  conditions and no satisfied condition cannot self-start. A job WITH
  date conditions can start without `start_times`: a run_calendar job
  fires at each row's own time, and at 00:00 when the row carries none
  (E11, DL-58). An extended calendar carries no row times, so assume
  00:00 for it. ON_HOLD blocks starts, but a scheduled tick can latch
  (SEM-21/32). Thus give each probe an explicit future `start_times`, or
  delete the probe before its first tick.
- You can INSPECT captured outputs (autorep, autocal_asc exports,
  job_depends reports) to answer the questions and to shape synthetic
  fixtures. Never commit these outputs to the repo (corpus hygiene,
  LICENSING.md).
- Record each answer as: a dossier SEM amendment that quotes the
  observation, a DL entry, and trace/fixture tests. Then retire the
  `# PENDING:` marker if the item has one (DL-06 protocol — a resolution
  deletes its switch/marker). Q6 is dossier-only and carries no code
  marker.

## 1. Source catalog (verified in DL-58/DL-62/DL-154; re-fetch before relying on one)

Fetch technique: get TechDocs and KB pages with a raw `curl` that has a
browser User-Agent (`-A "Mozilla/5.0 ..."`). Then strip the HTML. A
fetcher that summarizes instead of returning the bytes sees nav-only
shells on TechDocs *reference* pages. Fetch Broadcom community threads
the same way.

URL patterns:
- KB: `https://knowledge.broadcom.com/external/article/<ID>`
- Community: `https://community.broadcom.com/communities/community-home/digestviewer/viewthread?MID=<ID>`
- TechDocs: `https://techdocs.broadcom.com/us/en/ca-enterprise-software/intelligent-automation/autosys-workload-automation/<version>/…`

| Source | What it settles or contains |
|---|---|
| KB 408778 | Q7: exit-code precedence — fail_codes decides alone, unlisted codes SUCCESS |
| KB 438836 | SEM-05: local ON_ICE predecessor → atom true, lookback ignored; cross-instance ON_ICE not transmitted |
| KB 92872 | box_success over globals re-evaluated at member completions, not on SET_GLOBAL |
| KB 280764 | Q8b: vendor-worked nonzero adjust + S vector — WORKD#1 + adjust 1 + non_workday S = "the day after the first workday", kept even on a Saturday |
| KB 442457 | Q8d: literal `AND` in an estate calendar; CAUAJM_W_10119/10120 exhausted-calendar behavior; the >366-day materialization drop |
| KB 29387 | Q9: `autocal_asc -s\|-e\|-c ALL -E file` export, `-I file` import |
| KB 14195 | Extended calendars materialize into ujo_calendar on save, ~365 days, regenerate when exhausted |
| KB 135770 | job_depends/forecast go through the Application Server (not client-side reimplementation) |
| KB 230562 | E8 lean: spawn-path signal-9 abort reported as agent `State FAILED … Status(Aborted, Signal 9)` |
| KB 186017 | Calendar regeneration mechanics (DL-56/57 dormancy corroboration) |
| Thread 760251 | Q2b: CA support — newly inserted job has no previous end time; s(A,0) satisfied |
| Thread 734033 | Q3a + E11: CA's Mark Hanson — STARTJOB satisfies the time dependency, a start resets it; run_calendar row-time firing worked examples |
| Thread 801986 | Q3b: no-expiry latch; the Q3c box aside ("starts after the next time its parent box starts") |
| Thread 778062 | Q8c: non_workday O empirical preview + CA "behavior is not changed … documentation was wrong" |
| Thread 825395 | Q8c: 2012 consecutive-holiday-N community report (second holiday missing from output) |
| TechDocs 12.1 Define Extended Calendars | Q8a holiday-action precedence quote; action code definitions; adjust prompt wording; preview flow |
| TechDocs 24.2 Manage Job Status | Stale-status philosophy ("most recent completion … regardless of when"), ACTIVATED-on-box-start, INACTIVE-at-insert |
| TechDocs 24.2 job_depends | `-c\|-d\|-r\|-t` report modes; `-e` ("all start times", -t only) with `-F/-T` |
| TechDocs 12.1 timezone attribute | SEM-35 name resolution: ujo_timezones entry / OS name / POSIX value; not case-sensitive; OS matched first, table read up to five times (DL-62) |
| TechDocs 12.1 autotimezone command | ujo_timezones entry types (Zone/Alias/City), `-l/-q/-a/-c/-t/-d` verbs, POSIX TZ west-positive offset syntax (DL-62) |
| TechDocs 12.1 run_window attribute | SEM-33 box skip: "the job's status changes to INACTIVE. The box job can still run to completion."; the Box1 worked example, both closer-edge outcomes (DL-154) |

A second-opinion pass helps: hand a brief that contains the pins and
the leans to an independent reviewer. Before you move any pin, RE-FETCH
each citation that the review returns. Make sure that each citation is
correct. In DL-58, two candidate claims did not match the re-fetched
sources.

## 2. Protocols, cheapest first

Replace `<M>` with the name of an existing machine (`autorep -M ALL`,
read-only). All `jil` blocks are heredocs: `jil <<'EOF' … EOF`. Cleanup
for each protocol: for each job, run `jil <<< "delete_job: <name>"`. For
each box, use `delete_box:` (this command deletes the box and its member
jobs).

### Timezone map — read-only, run on ANY estate you migrate (DL-62)

`timezone:` values resolve through the instance's ujo_timezones table
(SEM-35 name-resolution note). Capture it once, read-only:

```
autotimezone -l > ujo_timezones.txt
```

Feed the file to the runner verbatim: `dsl41 run|rehearse --timezone-map
ujo_timezones.txt …`. Without it, city names fall back to the unique
zoneinfo city match (a preflight WARN per job); with it, the listing is
complete estate truth and unknown names refuse. If an estate carries
admin-added entries (`autotimezone -a/-c`), only the export knows them.

### Q9 — export bytes — **CLOSED (DL-60, [F])**

One observed export sample resolved Q9 on 2026-07-30. SEM-36/37 carry
the verdicts: `extended_calendar:` spelling, fixed attribute order with
empty-valued keys, `workday: all`, braces as grouping, `WORKD#L`,
`holiday: S` without holcal, and `HH:MM:SS` row tails. They sit at the
**[F]** tier, so a byte-exact check is still worth running. If shell
access becomes available, one read-only run gives it:

```
autocal_asc -s ALL -E dsl41_std_export.txt
autocal_asc -e ALL -E dsl41_ext_export.txt
autocal_asc -c ALL -E dsl41_cyc_export.txt
```

Diff the exports against the pinned facts. Record anything new as a DL
amendment, not as a relitigation.

### Q8b / Q8c / Q8d — calendar sandbox (inert; no job ever runs)

Write one import file, `dsl41_q8_cals.txt`, and import it. Then read the
generated dates of each calendar in two ways:

(a) Interactive: run `autocal_asc -e <name>`. Press Enter at each prompt
to keep the values. Answer `1` at the preview prompt. Capture the listed
dates.

(b) Scheduler cross-check: use one inert probe job, repointed at each
calendar in turn. It is created already held (`status: ON_HOLD`, SEM-24),
because an insert followed by a `JOB_ON_HOLD` event leaves a window in
which a due tick can fire. This optional step makes sure that the dates
materialize into ujo_calendar:

```
jil <<'EOF'
insert_job: dsl41_q8_probe
job_type: c
machine: <M>
command: /bin/true
date_conditions: 1
run_calendar: dsl41_q8b_1
start_times: "23:00"
status: ON_HOLD
EOF
job_depends -t -e -J dsl41_q8_probe -F "08/01/2026 00:00" -T "12/31/2026 00:00"
```

(Repoint `run_calendar` to each calendar with `update_job`. Delete the
probe before a tick becomes due.)

The probe calendars are defined in **`docs/probes/dsl41_q8_cals.txt`** in
this repo. The weekday facts in it are correct for 2026. Day tokens in a
`condition:` line are the SEM-37 three-letter forms (`mon`, `wed`), not
the two-letter codes that `workday:` takes — a two-letter token is
refused, and that refusal answers nothing. Copy the file to the box
(`scp docs/probes/dsl41_q8_cals.txt <box>:`). Then run
`autocal_asc -I dsl41_q8_cals.txt`. A refusal at import is itself an
answer — record the message verbatim. A BATCH refusal names no
hypothesis: on any refusal, import the two holiday calendars first, then
each extended calendar on its own, and attribute the message only to the
calendar that was alone in the file.

Dates: the fixture is written for August and December 2026. Roll it
forward if that window is past. Pick a month whose 1st is a Saturday
(May 2027 is the next one) — every day-of-month number in this section
then stays the same — and move the holiday rows and the `job_depends`
`-F`/`-T` bounds with it. Q8c_2 needs a different shape: two consecutive
holidays whose day after the second one is a Saturday.

What each August/December 2026 observation means:

- **Q8b** (adjust 1 + W — Aug 14 = Fri, Aug 15 = Sat): read the August
  date of each calendar as a pair (q8b_1, q8b_2):
  (17, 17) = shift-then-replace · (15, 18) = replace-then-shift ·
  (15, 16) = action ignored · (14, 17) = adjust ignored ·
  import/definition refused = vendor refuses the combination.
  Since DL-59, dsl41 IMPLEMENTS replace-then-shift — (15, 18) — as its
  pinned default. Thus this probe makes sure that the vendor and dsl41
  agree. The probe does not choose our behavior. If the pair is
  different, record the divergence, then correct it.
- **Q8c_1** (does the N walk skip holidays? Mon Aug 17 is a holiday, and
  holiday action S keeps holcal dates out of non-workday treatment):
  Saturdays map to Mondays 3, 10, ?, 24, 31 — the `?` decides:
  Aug 18 = holiday-free walk (our pin) · Aug 17 = plain next-workday,
  holidays not skipped.
- **Q8c_2** (holiday-N chaining — Dec 24+25 are both holidays): output
  Dec 25 = verbatim one-shot (our pin, current doc text) · Dec 26 = the
  target is re-processed by N (the 825395 hint) — this result flips the
  single-shot corner. Any other date (Dec 28, for example) means N walks
  like W; record it verbatim, it is a third behavior.
- **Q8d_1** (`mon | wed & fri`): Mondays in the output = `&` binds
  tighter · empty/no-valid-dates = flat left-to-right (our pin).
- **Q8d_2** (`NOT mon` line then `mon` line): empty = order-free
  union-minus-exclusions (our pin) · Mondays = sequential accumulation
  (later include resurrects).
- **Q8d_3** (`xtue | xwed`): record verbatim the vendor refusal or the
  generated dates (every day vs nothing). Since DL-59, dsl41 evaluates
  it literally as an include (every day) as its pinned default.
- **Q8d_4** (`mon OR wed`): every Monday and every Wednesday in the
  output shows that the OR word unions like `|` (AND is already cited,
  KB 442457).
- **Q8d_5** (`NOT mon` alone — the only rule): every non-Monday = an
  exclusion-only rule list subtracts from the DAILY default (our pin) ·
  empty = it subtracts from nothing. This is a separate pinned default
  from Q8d_2, which mixes an exclusion with an include.

Also diff the full date list of EVERY calendar against the `dsl41`
generator (`autocal.compile_calendar(...).days_between(...)`). The
whole-set diff catches surprises outside the aimed corner.

### Q6 — box_success over an iced member (~1 minute, runs /bin/true once)

```
jil <<'EOF'
insert_job: dsl41_q6_box
job_type: b
box_success: success(dsl41_q6_m)
insert_job: dsl41_q6_m
job_type: c
box_name: dsl41_q6_box
machine: <M>
command: /bin/true
insert_job: dsl41_q6_n
job_type: c
box_name: dsl41_q6_box
machine: <M>
command: /bin/true
EOF
sendevent -E JOB_ON_ICE -J dsl41_q6_m
sendevent -E FORCE_STARTJOB -J dsl41_q6_box
sleep 60; autorep -J dsl41_q6_box%
```

If the final box status is SUCCESS, ice satisfies box_success (our
SEM-05/DL-13 pin — Q6 closes as pinned). If the box stays RUNNING after
`dsl41_q6_n` completes, box_success does NOT read the iced member as
success (flip: the "not scheduled" clause wins). If the final box status
is FAILURE, the flip is the same, in a harder form. Capture `autorep -J
dsl41_q6_box -d` in each case. Cleanup: run
`jil <<< "delete_box: dsl41_q6_box"`.

### Q3c — does a member's latched tick survive into the next box run

This protocol is timing-sensitive. Pick a `HH:MM` value ~3 minutes in
the future (server time — `autorep -x` shows it, or run `date` on the
server).

```
jil <<'EOF'
insert_job: dsl41_q3c_box
job_type: b
insert_job: dsl41_q3c_m
job_type: c
box_name: dsl41_q3c_box
machine: <M>
command: /bin/true
date_conditions: 1
days_of_week: all
start_times: "HH:MM"
condition: s(dsl41_q3c_gate)
insert_job: dsl41_q3c_gate
job_type: c
machine: <M>
command: /bin/true
EOF
sendevent -E FORCE_STARTJOB -J dsl41_q3c_box      # BEFORE HH:MM
# wait past HH:MM: the tick lands while the box is RUNNING and the
# condition is false (gate never ran); scheduler log shows CAUAJM_I_40162
autorep -J dsl41_q3c%                              # m blocked, box RUNNING
sendevent -E KILLJOB -J dsl41_q3c_box              # box run 1 ends TERMINATED
sendevent -E FORCE_STARTJOB -J dsl41_q3c_box       # box run 2
sendevent -E FORCE_STARTJOB -J dsl41_q3c_gate      # the condition edge
sleep 60; autorep -J dsl41_q3c%
```

If `dsl41_q3c_m` runs in box run 2 with NO new tick, the latch survives
box runs (this result flips our DL-54 box-scoped arm — Q3c closes
flipped). If `dsl41_q3c_m` does not run, the arm dies with its box run
(our pin holds). Delete all three jobs the same day (the schedule ticks
daily).

### Q3d — does ON_ICE discard a latched tick (arm × ice)

DL-54's adversarial round pinned "a pre-existing arm survives
ON_ICE/OFF_ICE untouched" without a citation; DL-69 registers the
residue as Q3d (`# PENDING: Q3d`, oracle.py — the survive-pin stands
as the deterministic default until this runs). If the vendor instead
discards the queued start on ice, ON_ICE is the latch-*discharge* verb —
the one thing the sendevent set otherwise lacks (nightbank exercise 13
step 4 documents the gap). Same shape as Q3c, standalone job, ~3 min:

```
jil <<'EOF'
insert_job: dsl41_q3ice
job_type: c
machine: <M>
command: /bin/true
date_conditions: 1
days_of_week: all
start_times: "HH:MM"
condition: s(dsl41_q3ice_gate)
insert_job: dsl41_q3ice_gate
job_type: c
machine: <M>
command: /bin/true
EOF
# wait past HH:MM: the tick lands, condition false (gate never ran) -- armed.
# The scheduler log shows CAUAJM_I_40162. Without that line nothing armed,
# and the rest of the probe reads nothing.
sendevent -E JOB_ON_ICE  -J dsl41_q3ice
sendevent -E JOB_OFF_ICE -J dsl41_q3ice
sendevent -E FORCE_STARTJOB -J dsl41_q3ice_gate    # the condition edge
sleep 60; autorep -J dsl41_q3ice%
```

If `dsl41_q3ice` runs on the edge, the arm survived the ice round-trip
(our pin holds — and note the tension with SEM-20's "conditions must
reoccur": the tick, not the condition, is what carried over). If it does
not run, ice discards the queued start: amend SEM-20/SEM-32, clear
`armed` in the oracle's ON_ICE handler (`SCHED_DISARM` trace record),
and exercise 13's "no discharge verb" caveat gets rewritten — ON_ICE
becomes the discharge, with its downstream-satisfaction cost stated.
Delete both jobs the same day (the schedule ticks daily).

### E8 — external kill verdict (+ the mechanism discriminator)

```
jil <<'EOF'
insert_job: dsl41_e8
job_type: c
machine: <M>
command: sleep 600
EOF
sendevent -E FORCE_STARTJOB -J dsl41_e8
# on the AGENT machine:  ps -ef | grep 'sleep 600'  →  kill -9 <PID>
autorep -J dsl41_e8        # ST column: FA vs TE — the E8 verdict
autorep -J dsl41_e8 -d     # run detail + exit code (128+9?)
```

Variant (does the KILLJOB verdict come from recorded intent or from wait
status):

```
jil <<'EOF'
insert_job: dsl41_e8b
job_type: c
machine: <M>
command: sh -c 'trap "exit 0" TERM; sleep 600'
EOF
sendevent -E FORCE_STARTJOB -J dsl41_e8b
sleep 10; sendevent -E KILLJOB -J dsl41_e8b
sleep 30; autorep -J dsl41_e8b
```

Reading: e8 decides E8 on its own. If e8=FA, an external signal death is
FAILURE. Flip our TERMINATED default for the EXTERNAL death ONLY: a
`signaled` record produced by a kill the engine itself asked for must
stay TERMINATED, because `_kill_outcome_from_spool` (runner_startup.py)
reads `outcome_from_status`'s `Terminated` as the proof that a recorded
kill landed — a blanket flip in `outcome_from_status`
(runner_adapters.py) would retire live kills. Retire the
`# PENDING: E8` marker with the flip. If e8=TE, the agent reports signal
deaths, TERMINATED is mechanism-agnostic, and our mapping stands.

e8b answers a second question — the KILLJOB mechanism — and never gates
the E8 verdict. TE means the scheduler marks TERMINATED from its own
kill bookkeeping. SU means the trapped exit won the race, or the wait
status decides. Record e8b either way for the DL-41a notes. Capture the
agent/scheduler log lines around the kill (`autosyslog -J dsl41_e8` if
available).

### Optional read-only archaeology (no writes at all)

- `autorep -q -J ALL` — the estate JIL dump. Inspect it only. Never
  commit it.
- `job_depends -c -J <job>` — current condition satisfaction. This
  command showed that the Q2b reasoning was correct. It also gives
  useful spot checks for lookback shapes.
- The scheduler log around a historical OOM/kill incident. If the estate
  already had such an incident, this log can answer E8 at no cost.
