# Live-instance runbook — probes for the open questions

This runbook holds the probe protocols for the open AutoSys behavior
questions. Each protocol runs on a Workload Automation AE instance. The
runbook also lists the sources behind the dossier's citations. It is a
companion to the §9 register of the dossier (docs/autosys-semantics.md)
and to the live-instance route in CLAUDE.md.

Scope: the questions here are the AutoSys ones. Q10 and Q11 have no
protocol here; §9 names the observation that would settle each. Two
groups sit outside the scope. U1, U3b and U6b are descoped (DL-288); they
would need a Universal Controller instance, not an AE box.
docs/stonebranch-semantics.md Part III states all three, and
docs/uc-edge-schema.md states U3b's write-path test. The runner's own
parity defaults E5, E6, E7, E9 and E10 have no probe here
(docs/runner-design.md ss15 states each default).

Ground rules (corpus hygiene per CLAUDE.md and LICENSING.md):
- Give each object that you create on the instance the prefix `dsl41_`.
  If a schedule is involved, use a future date. Delete each object the
  same day.
- A calendar that no job references is inert. A job with no date
  conditions and no satisfied condition cannot self-start. A job WITH
  date conditions can start without `start_times`: a run_calendar job
  fires at each row's own time, and at 00:00 when the row carries none
  (E11, DL-58). An extended calendar carries no row times, so assume
  00:00 for it. ON_HOLD blocks starts, but a scheduled tick can latch
  (SEM-21/32). So give each probe an explicit future `start_times`, or
  delete the probe before its first tick.
- You can inspect captured outputs (autorep, autocal_asc exports,
  job_depends reports) to answer the questions and to shape synthetic
  fixtures. Never commit these outputs to the repo (corpus hygiene,
  LICENSING.md).
- Record each answer as a fact with its evidence tier: a dossier SEM
  amendment, a DL entry, and trace or fixture tests. Do not record where,
  when or by whom the probe ran, or what access it needed.
- A resolution retires the item's `# PENDING:` marker, if it has one
  (DL-06). The items with a marker are Q3c, Q3d, Q8b, Q8c, Q8d, Q15, Q16
  and E8. Q6, Q12, Q13, Q14, the SEM-20 box clause and rule 5 carry none.
- A semantic switch stays selectable after its question is settled
  (DL-252). The answer sets the switch's `autosys` value in the registry
  (`src/dsl41/semantics.py`). The default then follows DL-252's rule
  (runner-design §8a): it is the vendor's behavior where that is as safe
  as dsl41's reading, and dsl41 keeps its own default where its reading
  is safer or more useful, as `queued-recheck` and `fw-existence` do. Either way the
  other value stays selectable. Keeping a default that differs from the
  vendor is an owner ruling, recorded in a DL entry. A default change
  moves `STATE_MACHINE_VERSION`.

## 1. Source catalog (re-fetch before relying on one)

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
| KB 230562 | E8, spawn-time only, not a mid-run kill (DL-226): spawn-path signal-9 abort reported as agent `State FAILED … Status(Aborted, Signal 9)` |
| KB 186017 | Calendar regeneration mechanics (DL-56/57 dormancy corroboration) |
| Thread 760251 | Q2b: CA support — newly inserted job has no previous end time; s(A,0) satisfied |
| Thread 734033 | Q3a + E11: CA support — STARTJOB satisfies the time dependency, a start resets it; run_calendar row-time firing worked examples |
| Thread 801986 | Q3b: no-expiry latch; the Q3c box aside ("starts after the next time its parent box starts") |
| Thread 778062 | Q8c: non_workday O empirical preview + CA "behavior is not changed … documentation was wrong" |
| Thread 825395 | Q8c: 2012 consecutive-holiday-N community report (second holiday missing from output) |
| TechDocs 12.1 Define Extended Calendars | Q8a holiday-action precedence quote; action code definitions; adjust prompt wording; preview flow |
| TechDocs 24.2 Manage Job Status | Stale-status philosophy ("most recent completion … regardless of when"), ACTIVATED-on-box-start, INACTIVE-at-insert |
| TechDocs 24.2 job_depends | `-c\|-d\|-r\|-t` report modes; `-e` ("all start times", -t only) with `-F/-T` |
| TechDocs 12.1 timezone attribute | SEM-35 name resolution: ujo_timezones entry / OS name / POSIX value; not case-sensitive; OS matched first, table read up to five times (DL-62) |
| TechDocs 12.1 autotimezone command | ujo_timezones entry types (Zone/Alias/City), `-l/-q/-a/-c/-t/-d` verbs, POSIX TZ west-positive offset syntax (DL-62) |
| TechDocs 12.1 run_window attribute | SEM-33 box skip: "the job's status changes to INACTIVE. The box job can still run to completion."; the Box1 worked example, both closer-edge outcomes (DL-154) |
| TechDocs 12.0.01 timezone attribute | E10 no-timezone clock: start events "scheduled based on the time zone under which the scheduler is running" (DL-155) |

Before you move a pin on a citation, re-fetch the source and check the
quoted text against the fetched bytes. A summary is not a source.

## 2. Protocols, cheapest first

Replace `<M>` with the name of an existing machine (`autorep -M ALL`,
read-only). Run the commands in bash: the cleanup lines use here-strings
(`<<<`). All `jil` blocks are heredocs: `jil <<'EOF' … EOF`. Cleanup
for each protocol: for each job, run `jil <<< "delete_job: <name>"`. For
each box, use `delete_box:` (this command deletes the box and its member
jobs). "The scheduler log" is the event_demon log of the instance.

### Timezone map — read-only, run on ANY estate you migrate (DL-62)

`timezone:` values resolve through the instance's ujo_timezones table
(SEM-35 name-resolution note). Capture it once, read-only:

```
autotimezone -l > ujo_timezones.txt
```

Feed the file to the runner verbatim: `dsl41 run|rehearse --timezone-map
ujo_timezones.txt …`. Without it, a city name falls back to the unique
zoneinfo city match, with a preflight WARN per job. With it, an OS zone
name still resolves first, and a name that neither the OS nor the map
knows is refused. If an estate carries admin-added entries
(`autotimezone -a/-c`), only the export knows them.

### Q9 — export bytes — **CLOSED (DL-60, [F])**

Q9 is closed from one export sample (DL-60). SEM-36/37 carry the
verdicts: `extended_calendar:` spelling, fixed attribute order with
empty-valued keys, `workday: all`, braces as grouping, `WORKD#L`,
`holiday: S` without holcal, and `HH:MM:SS` row tails. They sit at the
**[F]** tier, so a byte-exact check is still worth running. It is
read-only:

```
autocal_asc -s ALL -E dsl41_std_export.txt
autocal_asc -e ALL -E dsl41_ext_export.txt
autocal_asc -c ALL -E dsl41_cyc_export.txt
```

Diff the exports against the pinned facts. Record anything new as a DL
amendment, not as a relitigation.

### Q8b / Q8c / Q8d — calendar sandbox (inert; no job ever runs)

The probe calendars are defined in **`docs/probes/dsl41_q8_cals.txt`** in
this repo. The weekday facts in it are correct for 2026. Day tokens in a
`condition:` line are the SEM-37 three-letter forms (`mon`, `wed`), not
the two-letter codes that `workday:` takes. A two-letter token is
refused, and that refusal answers nothing. Copy the file to the box
(`scp docs/probes/dsl41_q8_cals.txt <box>:`). Then run
`autocal_asc -I dsl41_q8_cals.txt`. A refusal at import is itself an
answer: record the message verbatim. A batch refusal names no
hypothesis. On any refusal, import the two holiday calendars first, then
each extended calendar on its own, and attribute the message only to the
calendar that was alone in the file.

Then read the generated dates of each calendar in two ways:

(a) Interactive: run `autocal_asc -e <name>`. Press Enter at each prompt
to keep the values. Answer `1` at the preview prompt. Capture the listed
dates.

(b) Scheduler cross-check, optional: it checks that the dates
materialize into ujo_calendar. Use one inert probe job, repointed at
each calendar in turn. Create it already held (`status: ON_HOLD`,
SEM-24): an insert followed by a `JOB_ON_HOLD` event leaves a window in
which a due tick can fire.

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

Repoint `run_calendar` to the next calendar and run `job_depends` again:

```
jil <<'EOF'
update_job: dsl41_q8_probe
run_calendar: dsl41_q8b_2
EOF
```

Delete the probe before a tick becomes due.

Dates: the fixture is written for August and December 2026. Roll it
forward if that window is past. Pick a month whose 1st is a Saturday
(May 2027 is the next one), so that every day-of-month number in this
section stays the same. Move the holiday rows and the `job_depends`
`-F`/`-T` bounds with it. Q8c_2 needs a different shape: two consecutive
holidays whose day after the second one is a Saturday.

What each August/December 2026 observation means:

- **Q8b** (adjust 1 + W — Aug 14 = Fri, Aug 15 = Sat): read the August
  date of each calendar as a pair (q8b_1, q8b_2):
  (17, 17) = shift-then-replace · (15, 18) = replace-then-shift ·
  (15, 16) = action ignored · (14, 17) = adjust ignored ·
  import/definition refused = vendor refuses the combination.
  dsl41 implements replace-then-shift, (15, 18), as its pinned default
  (DL-59). The probe checks that the vendor and dsl41 agree; it does not
  choose dsl41's behavior. If the pair is different, record the
  divergence, then correct it.
- **Q8c_1** (does the N walk skip holidays? Mon Aug 17 is a holiday, and
  holiday action S keeps holcal dates out of non-workday treatment):
  Saturdays map to Mondays 3, 10, ?, 24, 31. The `?` decides:
  Aug 18 = holiday-free walk (dsl41's pin) · Aug 17 = plain next-workday,
  holidays not skipped.
- **Q8c_2** (holiday-N chaining — Dec 24+25 are both holidays): output
  Dec 25 = verbatim one-shot (dsl41's pin, the current doc text) · Dec 26 =
  the target is re-processed by N (the 825395 hint); this result flips the
  single-shot corner. Any other date (Dec 28, for example) means N walks
  like W. Record it verbatim; it is a third behavior.
- **Q8d_1** (`mon | wed & fri`): Mondays in the output = `&` binds
  tighter · empty/no-valid-dates = flat left-to-right (dsl41's pin).
- **Q8d_2** (`NOT mon` line then `mon` line): empty = order-free
  union-minus-exclusions (dsl41's pin) · Mondays = sequential accumulation
  (a later include resurrects).
- **Q8d_3** (`xtue | xwed`): record verbatim the vendor refusal or the
  generated dates (every day vs nothing). dsl41 evaluates it literally
  as an include (every day) as its pinned default (DL-59).
- **Q8d_4** (`mon OR wed`): every Monday and every Wednesday in the
  output shows that the OR word unions like `|` (AND is already cited,
  KB 442457).
- **Q8d_5** (`NOT mon` alone, the only rule): every non-Monday = an
  exclusion-only rule list subtracts from the DAILY default (dsl41's pin) ·
  empty = it subtracts from nothing. This is a separate pinned default
  from Q8d_2, which mixes an exclusion with an include.

Also diff the full date list of EVERY calendar against the `dsl41`
generator. The whole-set diff catches surprises outside the aimed
corner. `test_q8_probe_calendars_compile_to_the_runbook_pins` holds
dsl41's August and December 2026 sets. For another window, set `lo`
and `hi` to that window and run the generator from the repo root:

```
uv run python -c '
from datetime import date
from pathlib import Path
from dsl41.autocal import compile_calendar
from dsl41.ir import lower_source
c = lower_source(Path("docs/probes/dsl41_q8_cals.txt").read_text(), file="q8")
lo, hi = date(2026, 8, 1), date(2026, 12, 31)
for name, cal in sorted(c.calendars.items()):
    if cal.kind == "extended":
        print(name, sorted(compile_calendar(cal, c).days_between(lo, hi)))
'
```

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

If the final box status is SUCCESS, ice satisfies box_success (dsl41's
SEM-05/DL-13 pin), and Q6 closes as pinned. dsl41 also completes the box
at the ice itself when the iced member has not run in a RUNNING box
(DL-285), and at a box start that leaves no member in the run (SEM-11,
Q15). Both completions rest on this pin when box_success names an iced
member, so a flip moves them too. If the box stays RUNNING after
`dsl41_q6_n` completes, box_success does NOT read the iced member as
success (flip: the "not scheduled" clause wins). If the final box status
is FAILURE, the flip is the same, in a harder form. Capture `autorep -J
dsl41_q6_box -d` in each case. Cleanup: run
`jil <<< "delete_box: dsl41_q6_box"`.

### Q12 — FREE on a depletable resource (~3 minutes, runs up to four short jobs)

Q12 is descoped (DL-288). The protocol stays for anyone who needs the
case. "resources Attribute" (AutoSys 24.2) documents FREE as "Optional
for renewable virtual resources only". dsl41's preflight refuses FREE=Y
and FREE=A on a depletable and accepts FREE=N (DL-287). The oracle
applies the code. This probe reads what the vendor does from job
behavior. Each resource has 2 units. Each `_b` job asks for both, so it
runs only when no unit is spent.

```
jil <<'EOF'
insert_resource: dsl41_q12_res
res_type: D
amount: 2
insert_resource: dsl41_q12y_res
res_type: D
amount: 2
insert_job: dsl41_q12
job_type: c
machine: <M>
command: sleep 45
resources: (dsl41_q12_res, QUANTITY=1, FREE=A)
insert_job: dsl41_q12_b
job_type: c
machine: <M>
command: /bin/true
resources: (dsl41_q12_res, QUANTITY=2)
insert_job: dsl41_q12_y
job_type: c
machine: <M>
command: /bin/false
resources: (dsl41_q12y_res, QUANTITY=1, FREE=Y)
insert_job: dsl41_q12_yb
job_type: c
machine: <M>
command: /bin/true
resources: (dsl41_q12y_res, QUANTITY=2)
insert_job: dsl41_q12_n
job_type: c
machine: <M>
command: /bin/true
resources: (dsl41_q12_res, QUANTITY=1, FREE=N)
EOF
sendevent -E FORCE_STARTJOB -J dsl41_q12
sleep 10; sendevent -E FORCE_STARTJOB -J dsl41_q12_b
sleep 5; autorep -J dsl41_q12%        # mid-run: dsl41_q12_b in RESWAIT
autorep -V dsl41_q12_res -d           # secondary capture, flag unverified
sleep 60; autorep -J dsl41_q12%       # after dsl41_q12 ends
sendevent -E FORCE_STARTJOB -J dsl41_q12_y
sleep 15; sendevent -E FORCE_STARTJOB -J dsl41_q12_yb
sleep 15; autorep -J dsl41_q12_y%
```

Read jil's output first. If it rejects a FREE=A or FREE=Y line, the
refusal stands and becomes [V]. If it rejects the FREE=N line, the
refusal extends to FREE=N. `dsl41_q12_n` is never started; it only tests
the definition. Mid-run, `dsl41_q12_b` in RESWAIT confirms that
`dsl41_q12` took one unit (1 free). If `dsl41_q12_b` ran, the probe read
nothing. After `dsl41_q12` ends:

- `dsl41_q12_b` runs: FREE=A returned the unit, as on a renewable. The
  oracle's reading stands, and the refusal could go.
- `dsl41_q12_b` stays in RESWAIT: the unit is spent, and FREE is ignored
  on a depletable. Preflight could accept Y and A, and `release_policy`
  would ignore the code there.

`dsl41_q12_y` fails. If `dsl41_q12_yb` then stays in RESWAIT (1 free),
FREE=Y freed nothing on FAILURE: success-only, the oracle's Y, unless the
FREE=A run showed FREE ignored. If it runs (2 free), the unit came back
unconditionally. Capture jil's output and every `autorep` output. The
`-V` read is secondary, since its flag is not checked against the 24.2
autorep page. Cleanup: run `sendevent -E KILLJOB -J <job>` for a job
still in RESWAIT (if it stays queued,
`sendevent -E CHANGE_STATUS -s INACTIVE -J <job>`), then
`jil <<< "delete_job: <job>"` for each of the five jobs, then
`jil <<< "delete_resource: dsl41_q12_res"` and
`jil <<< "delete_resource: dsl41_q12y_res"`.

### Q13 — CHANGE_STATUS RUNNING on a box (~3 minutes, runs /bin/true up to four times)

The vendor documents a permission check for this event on a box, and
that it starts a box in non-execution mode. dsl41 writes RUNNING and
starts nothing (dossier §9, Q13). Q13 has no switch. Box `_p` has a
member with no condition and one gated on it. Box `_q` has a condition
that never holds.

```
jil <<'EOF'
insert_job: dsl41_q13_p
job_type: b
insert_job: dsl41_q13_p1
job_type: c
box_name: dsl41_q13_p
machine: <M>
command: /bin/true
insert_job: dsl41_q13_p2
job_type: c
box_name: dsl41_q13_p
machine: <M>
command: /bin/true
condition: s(dsl41_q13_p1)
insert_job: dsl41_q13_never
job_type: c
machine: <M>
command: /bin/true
insert_job: dsl41_q13_q
job_type: b
condition: s(dsl41_q13_never)
insert_job: dsl41_q13_q1
job_type: c
box_name: dsl41_q13_q
machine: <M>
command: /bin/true
EOF
autorep -J dsl41_q13_p -d             # the run number before
sendevent -E CHANGE_STATUS -s RUNNING -J dsl41_q13_p
sleep 5;  autorep -J dsl41_q13_p% -d
sleep 30; autorep -J dsl41_q13_p% -d
sendevent -E CHANGE_STATUS -s RUNNING -J dsl41_q13_q
sleep 30; autorep -J dsl41_q13_q% -d
```

For `_p`, read whether `_p1` and `_p2` go ACTIVATED, whether `_p1`
starts, whether the box's run number moves, and whether the box
completes. If `_p1` runs, `_p2` follows and the box ends SUCCESS, the
event starts the box like a STARTJOB, and the oracle's status write is
the gap to fix. If nothing moves and the box stays RUNNING, the oracle
matches. For `_q`, a box that runs anyway means the event acts as a
force; a box that does not means its condition is honored.

Then send the event to a box that a real start made RUNNING, and read
whether the run number moves:

```
sendevent -E CHANGE_STATUS -s INACTIVE -J dsl41_q13_p   # only if _p is still RUNNING
sendevent -E JOB_ON_HOLD -J dsl41_q13_p1
sendevent -E STARTJOB -J dsl41_q13_p
sleep 10; autorep -J dsl41_q13_p% -d  # _p RUNNING, _p1 ON_HOLD; note the run number
sendevent -E CHANGE_STATUS -s RUNNING -J dsl41_q13_p
sleep 10; autorep -J dsl41_q13_p% -d
```

Read the scheduler log for a STARTJOB or FORCE_STARTJOB the scheduler
generated. Capture every `autorep` output. Cleanup: run
`jil <<< "delete_box: dsl41_q13_p"`,
`jil <<< "delete_box: dsl41_q13_q"` and
`jil <<< "delete_job: dsl41_q13_never"`.

### Q14 — CHANGE_STATUS FAILURE or TERMINATED on a running box (~6 minutes, runs up to six sleeps)

The question: does an operator's terminal status on a RUNNING box kill
its job_terminator members? dsl41 kills none (dossier §9, Q14). Q14 has
no switch.

```
jil <<'EOF'
insert_job: dsl41_q14_r
job_type: b
insert_job: dsl41_q14_jt
job_type: c
box_name: dsl41_q14_r
machine: <M>
command: sleep 603
job_terminator: 1
insert_job: dsl41_q14_plain
job_type: c
box_name: dsl41_q14_r
machine: <M>
command: sleep 603
EOF
sendevent -E STARTJOB -J dsl41_q14_r
sleep 20; autorep -J dsl41_q14_r%     # both members RUNNING
sendevent -E CHANGE_STATUS -s TERMINATED -J dsl41_q14_r
sleep 20; autorep -J dsl41_q14_r%
```

If `_jt` ends TERMINATED and `_plain` keeps RUNNING, the status write
triggers the job_terminator kill; the scheduler log shows a KILLJOB for
`_jt`. If both keep RUNNING, the oracle matches. Then kill both members
(`sendevent -E KILLJOB -J <job>`), wait for the box to settle, and run
the same steps again from `sendevent -E STARTJOB -J dsl41_q14_r`, with
`-s FAILURE` in place of `-s TERMINATED`. As the control, run them once
more with `sendevent -E KILLJOB -J dsl41_q14_r` in place of the
CHANGE_STATUS. That kills `_jt` by SEM-14's documented rule. Capture
every `autorep` output and the scheduler log lines for each step.
Cleanup: run `sendevent -E KILLJOB -J <job>` for a member still running,
then `jil <<< "delete_box: dsl41_q14_r"`.

### SEM-20 box clause — OFF_ICE inside a running box (~7 minutes, runs up to six short jobs)

"Start Conditions" (AutoSys 24.2 and 12.0) and "Job States" (24.2) say a
job taken off ice inside a running box waits for the box's next run. The
Web UI help (24.0) says the scheduler attempts to start it. dsl41's
default `off-ice-in-running-box=next-run` follows the guides: the member
sits the run out and the box completes without it. `same-run` follows
the Web UI help. Member `_c` waits on `_a`; `_k` keeps the box running
past the OFF_ICE.

```
jil <<'EOF'
insert_job: dsl41_oi
job_type: b
insert_job: dsl41_oi_a
job_type: c
box_name: dsl41_oi
machine: <M>
command: sleep 20
insert_job: dsl41_oi_c
job_type: c
box_name: dsl41_oi
machine: <M>
command: /bin/true
condition: s(dsl41_oi_a)
insert_job: dsl41_oi_k
job_type: c
box_name: dsl41_oi
machine: <M>
command: sleep 120
EOF
sendevent -E JOB_ON_ICE -J dsl41_oi_c
sendevent -E STARTJOB -J dsl41_oi
sleep 30; autorep -J dsl41_oi%        # _a SUCCESS, _c ON_ICE, _k RUNNING
sendevent -E JOB_OFF_ICE -J dsl41_oi_c
sleep 5;  autorep -J dsl41_oi%
sendevent -E FORCE_STARTJOB -J dsl41_oi_a
sleep 30; autorep -J dsl41_oi%        # _a SUCCESS again: _c's condition recurs
sleep 90; autorep -J dsl41_oi% -d     # after _k ends
```

Readings, after `_k` ends:

- `_c` never ran and the box is SUCCESS: the guides hold, and so does
  the default `next-run`.
- `_c` ran after `_a`'s second SUCCESS: the Web UI reading holds in its
  recurrence form, which `same-run` implements. The registry's `autosys`
  value becomes `same-run`, and the default follows DL-252's rule (see
  the ground rules).
- `_c` ran at the OFF_ICE, before `_a` ran again: the Web UI sentence
  holds read literally, and the scheduler attempts the start at the
  OFF_ICE. `same-run` does not do that: it waits for the condition to
  recur (SEM-20). Matching this would need a third switch value.
- `_c` never ran and the box stays RUNNING: the member is kept out of
  the run but still blocks the box. Neither switch value matches; the
  box-completion half of the default is wrong.

Then start the box again (`sendevent -E STARTJOB -J dsl41_oi`) and
check that `_c` runs after `_a` in that run. Capture every `autorep`
output and the scheduler log lines for `_c`. Cleanup: run
`sendevent -E KILLJOB -J <job>` for a member still running, then
`jil <<< "delete_box: dsl41_oi"`.

Nested variant (~3 minutes). dsl41 marks a member at its direct box
only. A member of a subbox that has not started yet, inside a running
box, is not marked when it is taken off ice, and it runs once the subbox
starts. Whether "contained in a running box" reaches through the subbox
is open.

```
jil <<'EOF'
insert_job: dsl41_oin
job_type: b
insert_job: dsl41_oin_g
job_type: c
box_name: dsl41_oin
machine: <M>
command: sleep 40
insert_job: dsl41_oin_s
job_type: b
box_name: dsl41_oin
condition: s(dsl41_oin_g)
insert_job: dsl41_oin_c
job_type: c
box_name: dsl41_oin_s
machine: <M>
command: /bin/true
EOF
sendevent -E JOB_ON_ICE -J dsl41_oin_c
sendevent -E STARTJOB -J dsl41_oin
sleep 10; autorep -J dsl41_oin%       # outer RUNNING, _s waiting on _g
sendevent -E JOB_OFF_ICE -J dsl41_oin_c
sleep 60; autorep -J dsl41_oin% -d    # after _g ends and _s starts
```

If `_c` runs once `_s` starts, dsl41's direct-box reading holds. If `_c`
never runs and `_s` ends SUCCESS without it, the vendor reads the rule
through the subbox; the mark would then belong on every running box
above the member. Cleanup: run `jil <<< "delete_box: dsl41_oin"`.

### Q15 — a box whose start leaves no member in the run (~6 minutes, runs no job)

No vendor sentence names a box whose members are all on ice when it
starts, or a box with no members. dsl41's default
`box-start-all-members-out=complete` completes such a box at its start
(dossier SEM-11, §9 Q15); `wait` keeps it RUNNING. Box `_q15` has two
iced members; box `_q15_e` has none.

```
jil <<'EOF'
insert_job: dsl41_q15
job_type: b
insert_job: dsl41_q15_a
job_type: c
box_name: dsl41_q15
machine: <M>
command: /bin/true
insert_job: dsl41_q15_c
job_type: c
box_name: dsl41_q15
machine: <M>
command: /bin/true
condition: s(dsl41_q15_a)
insert_job: dsl41_q15_e
job_type: b
EOF
sendevent -E JOB_ON_ICE -J dsl41_q15_a
sendevent -E JOB_ON_ICE -J dsl41_q15_c
sendevent -E STARTJOB -J dsl41_q15
sendevent -E STARTJOB -J dsl41_q15_e
sleep 30;  autorep -J dsl41_q15% -d; autorep -J dsl41_q15_e -d
sleep 270; autorep -J dsl41_q15% -d; autorep -J dsl41_q15_e -d
```

Readings, for each box on its own:

- SUCCESS: the default `complete` holds. Q15 closes as pinned, and the
  registry's `autosys` value becomes `complete`.
- RUNNING after 30 seconds and still after five minutes: the vendor
  keeps the box running. The registry's `autosys` value becomes `wait`,
  and the default follows DL-252's rule: `wait` leaves the box RUNNING
  until an operator acts, so the owner may keep `complete` (DL-59).
  Either value stays selectable.

The two boxes may differ; record each. Capture the scheduler log lines
for both boxes. Cleanup: run `jil <<< "delete_box: dsl41_q15"` and
`jil <<< "delete_box: dsl41_q15_e"`.

### Q16 — an iced member when a completed box re-derives its status (~2 minutes, runs three short jobs)

Basic Box Job Concepts lets a member's FORCE_STARTJOB or CHANGE_STATUS
change the status of a box that is not running, and ignores only
INACTIVE members. No vendor sentence names an iced member there. dsl41's
default `idle-box-iced-member=ignore` drops an iced member that is out
of the run, so a completed box does not flip through it (dossier SEM-15,
§9 Q16); `vote` reads its status. Box `_q16` has an iced direct member
`_m`. Box `_q16s` has an iced subbox `_q16s_s` that holds `_q16s_n`,
which fails.

```
jil <<'EOF'
insert_job: dsl41_q16
job_type: b
insert_job: dsl41_q16_a
job_type: c
box_name: dsl41_q16
machine: <M>
command: /bin/true
insert_job: dsl41_q16_m
job_type: c
box_name: dsl41_q16
machine: <M>
command: /bin/true
insert_job: dsl41_q16s
job_type: b
insert_job: dsl41_q16s_a
job_type: c
box_name: dsl41_q16s
machine: <M>
command: /bin/true
insert_job: dsl41_q16s_s
job_type: b
box_name: dsl41_q16s
insert_job: dsl41_q16s_n
job_type: c
box_name: dsl41_q16s_s
machine: <M>
command: /bin/false
EOF
sendevent -E JOB_ON_ICE -J dsl41_q16_m
sendevent -E JOB_ON_ICE -J dsl41_q16s_s
sendevent -E STARTJOB -J dsl41_q16
sendevent -E STARTJOB -J dsl41_q16s
sleep 30; autorep -J dsl41_q16% -d     # both boxes SUCCESS, _m and _q16s_s ON_ICE
sendevent -E CHANGE_STATUS -s FAILURE -J dsl41_q16_m
sendevent -E FORCE_STARTJOB -J dsl41_q16s_n
sleep 30; autorep -J dsl41_q16% -d
```

First read `_q16s_s` after `_q16s_n` fails. dsl41 re-derives an iced
subbox like any idle box, so it shows FAILURE there. If AutoSys leaves
`_q16s_s` unchanged, `_q16s` decides nothing yet. Record that
divergence. Then send `sendevent -E CHANGE_STATUS -s FAILURE -J
dsl41_q16s_s`, wait 30 seconds and run `autorep -J dsl41_q16% -d` again,
so that the iced subbox carries a FAILURE status, as `_m` does.

Readings, for each box on its own, once its iced member shows FAILURE:

- The box stays SUCCESS: the default `ignore` holds. Q16 closes as
  pinned, and the registry's `autosys` value becomes `ignore`.
- The box changes to FAILURE: the vendor reads the iced member's status.
  The registry's `autosys` value becomes `vote`, and the default
  follows DL-252's rule. Either value stays selectable.

For `_q16` also record what the CHANGE_STATUS did to `_m`: whether the
event was refused, whether `_m` shows FAILURE, and whether it still shows
ON_ICE. If `_m` shows FAILURE and is no longer on ice, CHANGE_STATUS
clears the ice; then `vote` is the vendor's reading for this shape, and
dsl41 must clear the ice too. If `_m` does not show FAILURE, `_q16`
decides nothing; record the refusal. The two boxes may differ; record
each. Capture the scheduler log lines for both boxes. Cleanup: run
`jil <<< "delete_box: dsl41_q16"` and `jil <<< "delete_box: dsl41_q16s"`.

### Q3c — does a member's latched tick survive into the next box run

This protocol is timing-sensitive. Pick an `HH:MM` value about 3 minutes
in the future, in server time (run `date` on the server).

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
box runs. This result flips the DL-54 box-scoped arm, and Q3c closes
flipped. If `dsl41_q3c_m` does not run, the arm dies with its box run
(dsl41's pin holds). Delete all three jobs the same day, because the
schedule ticks daily: `jil <<< "delete_box: dsl41_q3c_box"` and
`jil <<< "delete_job: dsl41_q3c_gate"`.

### Q3d — does ON_ICE discard a latched tick (arm × ice)

The pin "a pre-existing arm survives ON_ICE/OFF_ICE untouched" (SEM-32,
DL-54) is uncited. Q3d (DL-69) is the open question (`# PENDING: Q3d`,
oracle.py), and the pin is the deterministic default until this probe
runs. If the vendor instead discards the queued start on ice, ON_ICE is
the vendor's latch-*discharge* verb. The vendor sendevent set has no
other, and dsl41's own `DISARM` control (DL-158) has no vendor
counterpart. Same shape as Q3c, standalone job, about 3 minutes:

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
and dsl41's pin holds. Note the tension with SEM-20's "conditions must
reoccur": the tick, not the condition, is what carried over. If it does
not run, ice discards the queued start. Then amend SEM-20/SEM-32, clear
`armed` in the oracle's ON_ICE handler (`SCHED_DISARM` trace record),
and rewrite the note on discharge verbs in step 4 of exercise 13 in
`examples/nightbank/RUNBOOK.md`: ON_ICE becomes the vendor's discharge,
with its downstream-satisfaction cost stated. Delete both jobs the same
day, because the schedule ticks daily: `jil <<< "delete_job: dsl41_q3ice"`
and `jil <<< "delete_job: dsl41_q3ice_gate"`.

### E8 — external kill verdict (+ the mechanism discriminator)

E8's default maps an external signal death to TERMINATED. The vendor
documents TERMINATED for an operator's UNIX kill, and the scheduler's
KillSignals default `2,9` "usually" returns TERMINATED (DL-226,
runner-design ss15). Two parts stay open under the `# PENDING: E8`
marker: the mechanism, recorded intent versus wait status, which e8b
discriminates; and signal deaths no operator sent (segfault, OOM kill),
which the read-only archaeology below can show. e8 checks the documented
case. Each probe sleeps for its own number of seconds, so that its
process can be found by its exact command line.

```
jil <<'EOF'
insert_job: dsl41_e8
job_type: c
machine: <M>
command: sleep 601
EOF
sendevent -E FORCE_STARTJOB -J dsl41_e8
# on the AGENT machine:
pgrep -f '(^|/)sleep 601$'                 # the PID
ps -o ppid=,comm= -p <PID>             # its parent: the agent, or a shell?
kill -9 <PID>
# back on the client:
autorep -J dsl41_e8        # ST column: FA vs TE — the E8 verdict
autorep -J dsl41_e8 -d     # run detail + exit code
```

Reading e8: TE confirms the documented case, and the mapping stands. FA
means an external signal death is FAILURE, but only if the killed
process was the agent's direct child. If its parent is a shell and the
run shows exit code 137, the shell exited normally with 128+9, and the
run decides nothing about E8: run it again and kill the shell, the
agent's direct child. On a FA that decides, flip the TERMINATED default
for the EXTERNAL death ONLY. A `signaled` record produced by a kill the
engine itself asked for must stay TERMINATED: `_kill_outcome_from_spool`
(runner_startup.py) reads `outcome_from_status`'s `Terminated` as the
proof that a recorded kill landed, so a blanket flip in
`outcome_from_status` (runner_adapters.py) would retire live kills.

Variant e8b is the trap test of DL-226 and runner-design ss15: does the
KILLJOB verdict come from recorded intent or from the wait status? The
command exits 0 on the first INT or TERM it receives. The sleep runs in
the background under `wait`, so the trap runs at once rather than after
the sleep ends.

The probe reads the mechanism only when the first signal the agent sends
is 2 (INT) or 15 (TERM). The System Agent kills a job's process group
with the signals in `oscomponent.killsignals`, default `9`, optionally
preceded by a SIGTERM when `oscomponent.terminate.wait` is set (DL-226,
from the agent parameters page). With the agent default, the first
signal is 9, the trap cannot run, and a TE reads nothing. So first
record the scheduler's KillSignals and the agent's
`oscomponent.killsignals` and `oscomponent.terminate.wait`. If the first
signal the agent sends is 9, set `oscomponent.terminate.wait`, or put 15
first in `oscomponent.killsignals`, on a sandbox agent for the probe, in
the way the agent parameters page gives, and restore the values after.
If neither can be changed, do not run e8b.

```
jil <<'EOF'
insert_job: dsl41_e8b
job_type: c
machine: <M>
command: sh -c 'trap "exit 0" INT TERM; sleep 602 & wait'
EOF
sendevent -E FORCE_STARTJOB -J dsl41_e8b
sleep 10; sendevent -E KILLJOB -J dsl41_e8b
sleep 30; autorep -J dsl41_e8b
```

Reading e8b, with a trappable first signal: TE means the scheduler marks
TERMINATED from its own kill bookkeeping, recorded intent. SU means the
trapped exit won the race, or the wait status decides. Retire the
`# PENDING: E8` marker only when both open parts are answered. Capture
the agent and scheduler log lines around each kill
(`autosyslog -J dsl41_e8` if available). Cleanup: run
`jil <<< "delete_job: dsl41_e8"` and `jil <<< "delete_job: dsl41_e8b"`.
On the agent machine, kill only a process whose command line is exactly
`sleep 601` or `sleep 602`: `pkill -f '(^|/)sleep 60[12]$'`. e8b's background
sleep ignores INT, so it can outlive its shell.

### Rule 5 — does the vendor open a multi-line trailing comment (DL-161)

dsl41 follows the `/*`-format majority: a whitespace-preceded `/*` with
no `*/` on its line opens a comment that spans lines
(jil-statement-syntax.md rule 5, [?] against the live binary). The
vendor's answer is one insert away. The job has no start condition, so
it never runs:

```sh
jil <<'EOF'
insert_job: dsl41_r5   job_type: c
machine: <M>
command: echo hi /* watch
owner: prose about bob
end: */
EOF
autorep -q -J dsl41_r5
```

Reading: if the readback shows `command: echo hi` and NO owner
attribute, the vendor opened the comment. Then retire rule 5's [?] and
lift DL-161's default to [V]. If `owner` landed as an attribute, or the
insert refused, record the vendor behavior as a SEM amendment and
revisit DL-161. Cleanup: run `jil <<< "delete_job: dsl41_r5"`.

### Optional read-only archaeology (no writes at all)

- `autorep -q -J ALL` — the estate JIL dump. Inspect it only. Never
  commit it.
- `job_depends -c -J <job>` — current condition satisfaction. It gives
  spot checks for lookback shapes, the zero-lookback anchor (Q2a) and the
  never-ended case (Q2b).
- The scheduler log around an OOM kill or another signal death that no
  operator sent. If the estate had such an incident, its log answers
  E8's second open part at no cost.
