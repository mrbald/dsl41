# Nightbank runbook — operator training exercises

Every exercise runs against a live night. Start one (leave `--headless` off
to get the TUI in this terminal; add it to drive everything from a second
terminal instead):

```sh
uv run examples/nightbank/bin/nightbank up
```

The launcher prints the run directory, the region anchors, and the control
socket path. Below, `$S` stands for that socket path, and the `dsl41`
commands are `uv run dsl41 ...` when you are not inside the venv. The
default night includes the scripted incidents (`incidents.conf`); exercises
3–6 depend on them. A clean night: `--no-incidents`.

What a normal night looks like: regions fire APAC → EMEA → AMER a few
minutes apart; each region's extract/close jobs fan out, the universe
demand job fires once positions + macros + open orders exist, market data
and refdata load, valuation shards run on the grid, recon clears, and each
region's gate job sets its `RECON_*` global. When all three are CLEAN and
group risk is done, the SOD box arms, preflight runs — and the night then
**waits for you** (exercise 7) before the flip.

## 1. Watch the night — TUI tour

Goal: read the estate the way an operator does.

- The jobs table is the estate's box TREE: members indent under their
  box (EMEA's department boxes nest two deep). `space` folds the
  selected box shut — its row shows the hidden count, red with a `!`
  tally if the fold hides a FAILURE — and `z` folds/unfolds everything.
  Watch statuses flow INACTIVE → STARTING → RUNNING → SUCCESS; box rows
  fold their status from members (SEM-11).
- `/` opens the name filter (substrings, AND'd: try `emea rec`); Enter
  keeps it, Esc clears. `v` cycles the view: all → problems → active.
  Filtered views are flat — a match never hides inside a folded box.
- The header clock is UTC — the engine's time basis, and the base of
  every timestamp on screen. Your local wall time is deliberately absent.
- Select a row and press `d` (or Enter): the job-details popup shows the
  post-placeholder JIL block **this engine loaded** (not the template on
  disk), current status, `needs:`/`blocks:` dependency lines (blast
  radius before you hold or kill anything), and the log tail. Escape
  closes.
- Pane geometry, keyboard only: `m` maximizes the log tail (`m` or
  escape restores); `]`/`[` grow/shrink the log against the explain
  pane, `}`/`{` the jobs table against the side column. The event
  console focuses with `:`.
- Find the two file-watcher jobs per region (`*_F`): they RUN from box
  start, polling every 5s, and complete once the vendor file lands and
  its size is stable across two polls.
- `OPS_HEARTBEAT_C` fires every quarter hour inside its window: the
  calendar scheduler is alive; outside the window you'll see
  RUN_WINDOW_SKIP in the trace.

## 2. The query surface

```sh
dsl41 query status --socket $S --brief      # every job, one line each
dsl41 query status --socket $S --job EMEA_MKT_MARKS_C
dsl41 query trace  --socket $S | tail -20   # the transition log
dsl41 query explain --socket $S --job SOD_B # per-atom condition truth
dsl41 query spec   --socket $S --job SOD_B  # the loaded JIL block, verbatim
dsl41 query deps   --socket $S --job EMEA_ACC_GL_CLOSE_C  # needs / blocks
dsl41 query timers --socket $S              # everything due next, estate-wide
dsl41 query plan   --socket $S              # topological waves
dsl41 query subscribe --socket $S           # live journal stream (Ctrl-C)
dsl41 query is-success --socket $S -J APAC_EOD_B && echo done  # shell glue
dsl41 query global --socket $S -N RECON_EMEA   # value + state_rev of one global
dsl41 query status --socket $S --brief      # estate skim, with each job's rev

```

Every mutation below names the revision it was composed against
(`docs/concurrency-model.md` §0). `dsl41 sendevent` reads it for you
immediately before writing, so nothing here needs an extra flag — but if
you read a status page, thought about it, and then acted, pass the
`state_rev` you actually looked at:

```sh
dsl41 query status --socket $S --job EMEA_MKT_MARKS_C   # note state_rev
dsl41 sendevent KILLJOB -J EMEA_MKT_MARKS_C --expect 41 --socket $S
```

If the job moved in between — it completed, a deadline killed it, someone
else acted — the command exits 3 with `precondition failed` and changes
nothing. That is the intended outcome: re-read and decide again.

A `sendevent` fails in three different ways and says which by exit code,
because the right next move differs (`docs/control-protocol.md` §3):

| code | meaning | what to do |
|---|---|---|
| 0 | applied | — |
| 2 | **refused** — nothing admitted, nothing in the log | fix it and send again; unchanged is safe too |
| 3 | **rejected** — a decision, at an index, against you | re-read and decide again |
| 4 | **no decision** — it may yet apply | re-read. If you must send it again, send it with the `--request-id` printed on stderr; a retry under that id is answered from the original decision and cannot apply twice |

Exit 4 is the one to slow down on. It is not a failure — the engine may be
applying the command right now — and re-issuing it without its id would be
a second command, not a retry.

For a global, the revision to name comes from `query global`, and `0` is a
real answer meaning "still unset" — which is how a conditional create is
expressed:

```sh
dsl41 query global --socket $S -N RECON_EMEA           # {"present": false, "state_rev": 0}
dsl41 sendevent SET_GLOBAL --global RECON_EMEA=CLEAN --expect 0 --socket $S
```

`explain` is the money view: it shows each atom of a condition and whether
it is currently true. Use it on `SOD_B` early in the night — you'll see
the three `v(RECON_*)` atoms false until the recon gates run.

## 3. Incident: the custodian file that never arrives

EMEA recon stalls: `EMEA_REC_CUST_F` keeps RUNNING long after the region's
other feeds landed.

1. Notice `EMEA_REC_B` members waiting; `dsl41 query explain --socket $S
   --job EMEA_REC_TRADES_C` shows `s(EMEA_REC_CUST_F)` false.
2. Check the watcher: `dsl41 query status --socket $S --job
   EMEA_REC_CUST_F` — RUNNING, i.e. still polling. The upstream SFTP
   simulator "succeeded" but delivered nothing (its log says so:
   `runs/<night>/logs/EMEA_REC_CUST_SFTP_SIM_C.out`).
3. Deliver the file by hand, as ops would:
   `uv run examples/nightbank/bin/nightbank drop-file emea-custody`
4. Within ~10s (two stable polls) the watcher completes and EMEA recon
   proceeds.

## 4. Incident: hard failure, rerun after fix

`AMER_MKT_FX_C` fails (exit 5) on its first attempt; AMER valuation blocks
on `s(AMER_MKT_FX_C)`.

1. Spot the FAILURE row; read the job log
   (`runs/<night>/logs/AMER_MKT_FX_C.out`).
2. Rerun it: `dsl41 sendevent FORCE_STARTJOB -J AMER_MKT_FX_C --socket $S`
   ("the fix was deployed"), or select the row in the TUI and press `f`.
   The second attempt succeeds and the region resumes. Plain `STARTJOB`
   would be REFUSED here: the job already ran in this box execution
   (SEM-10, at-most-once per box run), and after the box folds it is not
   RUNNING either. The refusal is visible — `START_REFUSED` in
   `query trace` and the TUI events console — but it starts nothing.
   FORCE is not just the habit; it is the rerun verb.

## 5. Incident: the hung job and term_run_time

`EMEA_MKT_MARKS_C` hangs on first attempt. Its `term_run_time: 4` lets the
engine kill it after 4 minutes: status TERMINATED without any operator
action. Then:

1. `EMEA_MKT_B` folds FAILURE (its box_success needs the marks), and EMEA
   valuation won't arm.
2. Recover by rerunning the whole box:
   `dsl41 sendevent FORCE_STARTJOB -J EMEA_MKT_B --socket $S`
   — members re-run (the price file is already there, so the watcher
   completes fast), the marks' second attempt behaves, the box refolds
   SUCCESS, valuation proceeds.
3. Impatient variant: instead of waiting the 4 minutes, kill it yourself
   first — `dsl41 sendevent KILLJOB -J EMEA_MKT_MARKS_C --socket $S`.

Note what did NOT need recovery: `EMEA_MKT_VENDOR2_C` fails every night
(chronically broken secondary feed) and the box_success override ignores
it. Failures and tolerated failures are different things.

## 6. Exit-code bands: breaks within tolerance

`APAC_REC_TRADES_C` exits 1 ("7 breaks within tolerance"). Its
`max_exit_success: 1` classifies that as SUCCESS — the TUI shows SUCCESS
with exit code 1. Compare with exercise 4's exit 5. No action; just find
it and read the log.

## 7. The approval gate

The night ends at a human: `SOD_APPROVE_C` has `auto_hold: 1` and parks
ON_HOLD every time its box starts.

1. When preflight is green, check what the box waited for:
   `dsl41 query explain --socket $S --job SOD_B` — all v(RECON_*) CLEAN.
2. Release: `dsl41 sendevent OFF_HOLD -J SOD_APPROVE_C --socket $S`.
3. Watch `SOD_FLIP_C` swap `data/pending/` → `data/current/` (the new-day
   activation) and publish `SOD_DATE`; `SOD_WARMUP_C` proves it by
   reading from `current/`.

The flip is safe to RERUN: an empty `pending/` beside a populated
`current/` is recognized as already-flipped (idempotent no-op), so a
FORCE_STARTJOB after an engine outage between the flip and the
`SOD_DATE` publish cannot rotate the fresh day away. Destructive cleanup
happens last — a crash mid-flip loses no directory.

## 8. Resource contention and QUE_WAIT

Two places, same mechanism (oracle-enforced, DL-50):

- `OPS_HOUSEKEEP_C` vs `OPS_ARCHIVE_C`: one TAPE_DRIVE, both start with
  their box; the loser sits QUE_WAIT "waiting for resources" until the
  winner releases.
- EMEA's four valuation shards (`job_load: 40` on a `max_load: 100`
  grid): two run, two queue. Priority orders the *waiters* only — no
  preemption — so shard 4 (priority 10) is admitted ahead of shard 3.

Find both in the TUI; `query status` shows QUE_WAIT and `query trace`
shows the admission order.

## 9. Globals as gates

The recon gate jobs drive the control plane themselves — each runs
`dsl41 sendevent SET_GLOBAL --global RECON_<region>=CLEAN` against the
engine's own socket. Operator's version:

```sh
dsl41 sendevent SET_GLOBAL --global RECON_EMEA=WAIVED --socket $S
dsl41 query explain --socket $S --job SOD_B    # atom now false: WAIVED != CLEAN
dsl41 sendevent SET_GLOBAL --global RECON_EMEA=CLEAN --socket $S
```

There is no "show globals" query verb: `explain` on a consumer is how you
read a global — each `v()` atom line carries its effective value
(`= 'WAIVED'`, or `(unset)`). (SET_GLOBAL events are edge-triggered —
setting a global wakes exactly the jobs whose conditions reference it.)

## 10. Restart drills

Tethered (default): Ctrl-C the engine mid-night — running jobs are killed
and that is durably recorded. Restart with the SAME estate and run root:

```sh
uv run dsl41 run <the exec line the launcher printed, minus --ui> --resume
```

Replay + reconcile brings every job to a truthful state; scheduler ticks
that came due while it was down are *dropped and journaled* (printed as
`dropped STARTJOB ...`), never fired late.

Detached: start the night with `nightbank up --detached`. Jobs now run
under a per-run-root supervisor; stopping the engine leaves them running.

```sh
uv run dsl41 supervise list --run-root <run>/engine   # what's still alive
# restart with --resume --detached: the engine reattaches to live jobs
```

**Planned maintenance — drain instead of Ctrl-C.** A restart drill kills
running work. A drain does not: it stops *new* work being dispatched to
this execution host and lets what is running finish.

```sh
uv run dsl41 host list -S <run>/engine/control.sock       # note the state_rev
uv run dsl41 host drain local -S <run>/engine/control.sock
uv run dsl41 query status --brief -S <run>/engine/control.sock  # watch it empty
uv run dsl41 host activate local -S <run>/engine/control.sock   # work resumes
```

Start a job while drained and watch what does *not* happen: the oracle
starts it — a job's semantics do not depend on where its machine routes —
and `query status` reports it RUNNING with `"held": true` and no process
behind it. It is held, not failed and not moved: a job is only ever rerun
somewhere else after its host is **evicted**, and eviction needs proof the
old executor is dead. Try it and read the refusal:

```sh
uv run dsl41 host evict local -S <run>/engine/control.sock   # exit 3, and why
```

`activate` re-dispatches everything the drain held, which is what makes a
drain reversible and a Ctrl-C not.

**The deadman — what makes eviction possible at all.** `host list` shows
`"deadman_s": null` by default: nothing bounds when this host's jobs would
die if the engine vanished, so nothing can prove it is safe to run them
somewhere else. Start a detached night with an interval and watch the whole
chain:

```sh
uv run dsl41 run <exec line> --detached --deadman 30
uv run dsl41 host list -S <run>/engine/control.sock   # deadman_s: 30.0
# now SIGKILL the engine -- no goodbye, the way a real crash arrives
pkill -9 -f 'dsl41 run'
# the jobs keep running: that is what --detached buys...
# ...for 30 seconds. Then the supervisor exits and takes them with it:
tail -1 <run>/engine/supervisor.log          # "deadman fired -- no live leaseholder"
cat <run>/engine/runs/*/status.json          # "cause": "parent lost"
```

Restart the engine inside the interval and nothing dies — the clock restarts
whenever a controller is watching. That is the trade in one line: a deadman
costs you the outage window `--detached` was for, and buys the only proof an
operator can offer that a dead host's work may be rerun elsewhere.

## 11. Changing a job spec

There is no mid-run reload, by design: resume gates on the exact catalog
hash. Try editing a JIL file while the night runs: within ~15s the TUI
subtitle flags **SPEC DRIFT** — the files changed, the running catalog is
still the truth. The operator flow is cold:

1. Stop the engine (Ctrl-C).
2. Edit the JIL (e.g. bump a `--sleep`, add a condition atom).
3. Start a FRESH run root: `nightbank up ...` again (a used run root
   refuses re-baselining; the old night's journal stays intact as the
   record of what happened).

Every run root is self-contained: `<run>/engine/manifest/` holds the
post-placeholder JIL this run actually loaded plus `manifest.json`
(tool version, catalog hash, input hashes, launch options) — the audit
artifact outlives the estate files it was launched from.

Try it: after a night completes, change `EMEA_ACC_CASH_C`'s sleep and
start a new night. The point of the hash gate: a journal only replays
against the estate that wrote it.

## 12. Ice, hold, noexec

- `OPS_LEGACY_REPORT_C` is born ON_ICE (`status: ON_ICE` in JIL). Its
  upstream succeeds, it never runs — and any status atom naming an iced
  job evaluates TRUE, so nothing downstream would wait for it. Revive:
  `dsl41 sendevent OFF_ICE -J OPS_LEGACY_REPORT_C --socket $S` — and note
  it STILL does not run: OFF_ICE does not re-evaluate, the condition must
  REOCCUR (SEM-20). Either retrigger its upstream or
  `FORCE_STARTJOB` it; watch `explain` before and after. Ice is a
  skip-*and-satisfy*, never a quiesce: icing a job wakes everything
  downstream of it immediately (exercise 13 makes this bite).
- `ON_HOLD`/`OFF_HOLD`: park a healthy job before it starts (try it on
  `OPS_ARCHIVE_C` early).
- `ON_NOEXEC`: the job "runs" without spawning anything (status flows,
  no process) — vendor bypass semantics; try it on a shard.
- `OPS_XINST_DEMO_C` waits on `s(GROUP_TREASURY_EOD^PRD, 9999)` — a job
  in another (production) instance this sandbox can't see. `explain`
  shows the atom; release it with
  `dsl41 sendevent CHANGE_STATUS -J "GROUP_TREASURY_EOD^PRD" -s SUCCESS --socket $S`.

## 13. Skip the day

The compounding-failure ending: three incidents deep, the batch window is
gone, and the decision comes down — no SOD today. There is deliberately
no "skip day" button; the drill is choosing the right verbs, in the
right order, and knowing which ones lie.

**Variant A — abandon the night (the honest skip).**

1. Survey what is still live and due: `dsl41 query timers --socket $S`
   for the upcoming ticks, `query status --brief` for RUNNING rows and
   flag `A` — latched ticks that WILL fire on release. The TUI shows
   both at once: `t` (triggers view) and the flags column.
2. Quiesce with HOLD, not ICE: `dsl41 sendevent ON_HOLD -J <box>
   --socket $S` (TUI: `h`) on every top-level box that has not fired
   yet. A held job starts nothing and satisfies nothing. ON_ICE would be
   wrong here: exercise 12's rule means icing a broken region box fires
   `GLOBAL_RISK_B` on the spot. Ice skips, hold parks.
3. Kill the running work: `v` to the active view, `k` (KILLJOB) each
   RUNNING command job. Kill leaves, not boxes: KILLJOB on a box
   terminates the box row but only `job_terminator` members die with it
   (SEM-14) — the rest keep running to completion.
4. The trap: a tick that lands on a held job arms the latch (flag `A`),
   and the latch has no expiry — OFF_HOLD tonight fires the missed run
   at once. After the skip decision, nobody releases holds. There is no
   discharge verb; the latch dies with the night (step 6).
5. Leave `SOD_APPROVE_C` parked. The auto_hold approval gate IS the
   skip-day veto: the night never flips, `current/` keeps whatever the
   last flip left there (empty, in a fresh sandbox run root — the
   production reading is "yesterday stays live"). The flip would refuse
   anyway — nothing produced
   `pending/sod_ready.flag`, and an empty `pending/` beside an empty
   `current/` is "NOTHING TO FLIP", exit 4. The estate cannot fabricate
   a day.
6. End the night: Ctrl-C. The journal and `manifest/` are the audit
   record of the decision; tomorrow is a fresh `nightbank up`.
7. Calendar residue: dropped ticks never fire late. If tonight was the
   `EOM_BUSINESS` date, `OPS_MONTHLY_ATTRIB_C`'s month-end run is
   *lost*, not deferred — tomorrow's catch-up is an explicit
   `FORCE_STARTJOB`, journaled as an operator action like everything
   else.

**Variant B — the business insists the date rolls.** Possible, and every
fake step is a distinct, sourced, journaled operator action — the system
makes lying expensive and attributable, not impossible:

```sh
dsl41 sendevent SET_GLOBAL --global RECON_APAC=CLEAN --socket $S   # recorded waiver
dsl41 sendevent SET_GLOBAL --global RECON_EMEA=CLEAN --socket $S   # (x3)
dsl41 sendevent SET_GLOBAL --global RECON_AMER=CLEAN --socket $S
dsl41 sendevent ON_NOEXEC -J SOD_PREFLIGHT_C --socket $S   # bypass BEFORE the box
dsl41 sendevent ON_NOEXEC -J SOD_FLIP_C      --socket $S
dsl41 sendevent ON_NOEXEC -J SOD_WARMUP_C    --socket $S
dsl41 sendevent FORCE_STARTJOB -J SOD_B --socket $S        # overrides s(GLOBAL_RISK_B)
dsl41 sendevent OFF_HOLD -J SOD_APPROVE_C --socket $S      # the sign-off, real
until dsl41 query is-success -J SOD_B --socket $S; do sleep 2; done
dsl41 sendevent SET_GLOBAL --global SOD_DATE=$(date +%F) --socket $S
```

`sendevent` returns when the engine accepts the event, not when the job
finishes — hence the `is-success` wait: the date is published after the
sign-off actually succeeded, never before (or despite) it.

Set the NOEXEC bypasses before force-starting the box — preflight has no
condition and fires the moment the box runs. The bypassed flip never runs
its command, so its `SOD_DATE` publish never happens: the operator does
it by hand, last. Read `query trace` afterwards: the whole variant is
five lies and a signature, each a sourced control-plane event in the
journal (DL-68). The WAL records that an operator did it and when — not
which human held the socket; the *who* is your access controls' story,
so don't share the service account if that distinction matters. Note
what stayed true: `current/` still holds yesterday's data — this
variant rolls the *date*, not the day.

## 14. After the night — run history

Everything above reads one run root while it is still live, over the
socket. `dsl41 runs` reads it cold, afterwards — no engine, no socket:

```sh
dsl41 runs <run>/engine
```

One row per job run, folded from the journal, the manifest, and each job's
spool. `AMER_MKT_FX_C` (exercise 4's failure-then-retry) shows up as two
rows — run 1 FAILURE, run 2 SUCCESS, `started_by` naming the
`FORCE_STARTJOB` that fixed it. Box rows (`EMEA_MKT_B`, `AMER_EOD_B`, ...)
show up too, timed from the replayed trace rather than a spool — a box
never spawns a process, so there is nothing else to time it from.

```sh
dsl41 runs <run>/engine --job EMEA_MKT_B --format json
```

`--format json`/`csv` for scripting, `--since ISO8601` for a window,
`--job NAME` to narrow to one job across everything below.

**Two run roots, one series.** Exercise 11 already produces a second run
root under a changed catalog — bump `EMEA_ACC_CASH_C`'s `--sleep` and start
a fresh night, exactly as that exercise says. Point `dsl41 runs` at both
run roots on one command line:

```sh
dsl41 runs <first-run>/engine <second-run>/engine --job EMEA_ACC_CASH_C
```

The table comes back as one series, sorted by start time, with a labelled
break where *that job's own* definition changed — never blended into one
misleading line, and never a refusal to print just because the definition
moved underneath the job.

Drop `--job` and the point sharpens: only `EMEA_ACC_CASH_C` gets a break,
even though the estate-wide `catalog_hash` moved for every job in the
estate. That is why the break is drawn from the per-job fingerprint and
not from the catalog hash (DL-113 decision 4) — a release touching one job
of hundreds should mark one job, not all of them.

## The boundary era — exercises 15 to 21

Exercises 1 to 14 all live inside ONE night. The rest of an estate's life
is the boundary: a night ends at an instant you choose, the next period
opens with the state still on it, and last night's record becomes an
archive somebody may one day have to prove. Those are the verbs below.

Set three things once. `$R` is the engine's run root — the `engine`
directory *inside* the night, not the night itself — `$P` is the
properties file the night was launched with, and `$NEXT` is the estate the
NEXT period will run:

```bash
R=<run>/engine
P=<run>/night.properties
NEXT=(); for f in examples/nightbank/estate/small/*.jil; do NEXT+=(--next "$f"); done
```

(`NEXT` is a shell ARRAY: bash or zsh. In a POSIX shell, type the six
`--next <file>` pairs out.)

`$NEXT` is one `--next` per file, and the file ORDER is part of the
address the estate is stored under (`docs/period-model.md` §1.1) — so
build it from the same sorted glob the launcher uses, and keep using it.
Here the next estate is the same estate: an unchanged catalog is a legal
new period, and sealing is also how you bound a long-running night's WAL
file.

The lineage anchor is `$R.anchor` — a SIBLING of the run root, never
inside it, so `tar`ing the night never carries the fence away with it.

## 15. Seal the night

Goal: close period 1 at a chosen instant and commit period 2.

The engine is still running (variant B below is the other case).

1. Quiesce first, exactly as exercise 10's drain does: `dsl41 query
   timers --socket $S` for what is about to fire, `ON_HOLD` the top-level
   boxes that have not fired yet, and let running work finish
   (`dsl41 query status --brief --socket $S` until nothing is RUNNING).
2. Seal:

```sh
uv run dsl41 seal --run-root $R "${NEXT[@]}" -p $P --claimed-actor you@host
```

3. Read the refusal. If you sent any `sendevent` in the last minute you
   get exit 2 and this:

```
the last externally requested attempt was 9.128s ago and the closing period's
retry_horizon_us is 60000000: a retry composed under this baseline can no
longer be answered after the boundary. Wait it out, or seal with
--force-seal (period-model ss9)
```

   The engine is untouched — check with `dsl41 query status --brief
   --socket $S`. A refused boundary is the closing period's business and it
   keeps running.
4. Wait the minute out and seal again. Now you get one JSON line and exit
   0:

```json
{"digest": "sha256:a0b4…", "kind": "seal", "next_period_id": 2, "ok": true, "period_id": 1}
```

   The ENGINE then exits with code **3** — "sealed; period 2 is ready to
   open". That is not a failure. Under an init system it needs
   `RestartPreventExitStatus=2 3`, or the unit restart-loops a sealed
   engine.
5. The impatient variant: add `--force-seal`. It commits inside the
   horizon and writes the gate's own numbers into the seal, so the record
   alone shows that somebody forced it:

```sh
uv run dsl41 seal --run-root $R "${NEXT[@]}" -p $P --force-seal
grep -o '"forced_gate":[^}]*}' $R/seals/000001.json
```

**Variant B — the night is already stopped.** Same command, no engine.
`seal` tries to take `leader.lock` itself; nothing holds it, so this
process becomes the leader for exactly one boundary — it replays the
journal, reconciles what it finds, and performs the same cutoff a live
engine would. You see a sentence instead of JSON:

```
sealed period 1 at sha256:a0b4…; period 2 is ready to open
open it with `dsl41 run --resume --run-root <run>/engine <new estate files>`, …
```

Which mode you get is decided by the LOCK and never by a flag. There is no
`--offline`: an engine that holds `leader.lock` IS a live engine, and a
flag would let you assert something the estate can prove.

*Why.* A boundary is a RECORD, not a directory (`docs/period-model.md`
§6, §7). The cutoff freezes admission at one instant T, drains every
command already admitted to its durable decision, admits every scheduler
tick due at or before T, and only then writes three things in order: the
seal sidecar, the `seal` record, and the lineage head. Anything that fails
before the sidecar leaves the period open and correct — which is what exit
2 means. The retry horizon (§9) is the soft gate: a command you sent
seconds ago can still be retried under the closing baseline, and once
period 2 opens that retry is refused as stale. Sixty seconds is the
default and it is a `RuntimeProfile` field, pinned in the period's own
manifest so an audit can re-derive the gate later.

## 16. Open period 2 — what crossed, and what did not

Goal: see that the night's state survived the boundary.

1. Open the next period in place. It is an ordinary resume of the same run
   root:

```sh
uv run dsl41 run --resume --run-root $R examples/nightbank/estate/small/*.jil -p $P
```

2. Ask the new period for the old period's state:

```sh
dsl41 query global --socket $S -N RECON_APAC     # still CLEAN
dsl41 query status --socket $S --job OPS_B       # still "on_hold": true
dsl41 query status --socket $S --job AMER_MKT_FX_C   # still SUCCESS (run 2, if you reran it in exercise 4)
```

3. Note what MOVED. Every answer now carries a different `baseline_id` —
   period 2's, derived at the boundary. An `--expect` you composed against
   a revision you read before the seal still holds, because the revisions
   themselves crossed verbatim; a whole COMMAND composed under the old
   baseline does not, and is refused as stale.
4. Note the trap. An armed latch (flag `A` in `query status --brief`)
   crosses too. If you held a box in step 1 of exercise 15 and its tick
   landed while it was held, your `OFF_HOLD` in period 2 fires that run at
   once — which is the whole point of a hold, and a surprise if you
   expected the new period to start clean. To NOT have it, disarm before
   the seal, with a journaled command.

*Why.* Runtime globals, operator holds, `last_end_at`, armed latches,
every box's `ran_members` and every `run_number` cross the boundary,
because the boundary is a record and not a new directory
(`docs/period-model.md` §3.3). What does not cross is the catalog and the
launch options — those ARE the new period. The old
`deployment-runbook.md` §6 sentence "latches die with the old baseline" is
true only of the fresh-run-root cycle and is corrected there.

## 17. The morning after — audit, verify, the attested row

Goal: turn last night into evidence somebody can check without it.

1. Re-derive the closed period from its own inputs:

```sh
uv run dsl41 audit --run-root $R
```

```
period 1 attested: sha256:b720… (seal sha256:a0b4…, chain through 1)
```

2. Check the checkpoint on its own:

```sh
uv run dsl41 verify --run-root $R
```

```
<run>/engine/seals/000001.audit.json verifies: seal sha256:a0b4…, chain
through period 1, produced by dsl41 0.9.0
```

3. Find the attested row. It lives in the anchor, and the shortest way to
   read it is the footer of a retention survey (exercise 21):

```sh
uv run dsl41 estate prune --run-root $R --dry-run | tail -1
# … estate <uuid>, period 2, attested [1]
```

4. Run `audit` twice. The second run returns the same checkpoint and
   changes nothing; it is idempotent by design, and it also finishes the
   anchor row if a crash left it outstanding.
5. Try `audit` while an engine leads the root and the row cannot be set.
   The message says the checkpoint IS written and durable, and only the
   anchor row is outstanding — re-run it when the lock is free.

*Why.* `audit` and `verify` answer two different questions and the
difference matters (`docs/period-model.md` §1.3). `audit` REBUILDS the
seal from four things — the opening seal, the period's whole WAL, its
immutable spool, and the two period manifests — and refuses if what it
rebuilds differs from what is on disk. (It read the sentinel as a fifth
until DL-138, for one derivation that now has a single answer.) It needs
all four, so only the root that HOLDS the period can do it. `verify` checks an attestation: its
own digest, its binding to the seal it names, and the chain it claims.
That is what a rolled root can do with an imported pair, and it is why
producing a checkpoint requires the one below it while consuming one
accepts it alone. Attesting is also what unlocks retention: until a period
is attested, its whole spool is floored, because that spool is what
`audit` reads.

## 18. Retired: a night from before the boundary era

There is no exercise here any more. DL-138 retired every read dialect a
pre-boundary run root was written in — the `header` journal, `catalog_hash`
version 1, the `manifest/` layout — and the `estate adopt` verb that
translated one. The `estate` group keeps `reclaim` and `prune`.

Point any of the verbs above at such a root and it refuses BY NAME,
naming the dialect and DL-138 rather than producing a parse error:

```
<old>/engine/journal.jsonl: opens with `header`, a RETIRED record dialect
refused by name since DL-138 -- there is no path from it into a period
lineage, and this root cannot be claimed
```

*Why.* `docs/protocol-evolution.md` is the contract: §3 says a dialect may
be retired only when no instance of it exists anywhere, §5 states the
pre-production reset clause DL-138 used, and §6 says a retired reader is
replaced by a tombstone that names what it found — never deleted into
silence.

## 19. Roll to a fresh run root

Goal: open the next period in a NEW directory, so the old one can be
archived. This is optional hygiene, not a second kind of boundary.

1. Seal (exercise 15). Then try the roll before attesting, and read the
   refusal:

```sh
uv run dsl41 run --open-from $R.anchor --run-root <run>/engine-p2 \
    examples/nightbank/estate/small/*.jil -p $P
```

```
<run>/engine/seals/000001.audit.json: period 1 is not attested -- `dsl41
audit` produces the checkpoint (period-model ss1.3)
```

   Nothing was written at all — the target directory does not even
   exist. Check it: the roll's preflight runs before anything is
   created, so a refused roll leaves no residue to clean up.
2. Attest, then roll for real:

```sh
uv run dsl41 audit --run-root $R
uv run dsl41 run --open-from $R.anchor --run-root <run>/engine-p2 \
    examples/nightbank/estate/small/*.jil -p $P
```

   The new root comes up serving period 2. It holds an imported
   `seals/000001.json` and `seals/000001.audit.json`, its own
   `wal/000002.jsonl`, and a sentinel naming the claim that created it.
   Ctrl-C it when you have looked around; the checks below read files.
3. Prove what the new root can and cannot do:

```sh
uv run dsl41 verify --run-root <run>/engine-p2 --period 1   # passes
uv run dsl41 audit  --run-root <run>/engine-p2 --estate-anchor $R.anchor --period 1
```

```
<run>/engine.anchor/anchor.json: period 1 lives in <run>/engine, not
<run>/engine-p2 -- this attestation was produced in another estate's root
(period-model ss1.3)
```

4. LINEAGE-FENCED operations on the new root — resuming it, sealing it,
   rolling it again, setting its attested rows — need the lineage's
   anchor, which still lives beside the FIRST root. (`verify` above
   needed no anchor: it reads the artifact alone.) Leave the flag off
   and the refusal says so before anything else does:

```sh
uv run dsl41 run --resume --run-root <run>/engine-p2 \
    examples/nightbank/estate/small/*.jil -p $P
```

```
<run>/engine-p2.anchor: this lineage has no anchor -- ...
```

   The corrected command names the original:

```sh
uv run dsl41 run --resume --run-root <run>/engine-p2 --estate-anchor $R.anchor \
    examples/nightbank/estate/small/*.jil -p $P
```

*Why.* A roll leaves the closing period's WAL, spool and manifests behind
in the old root, so the new root can never re-derive period 1 — it holds
none of period 1's inputs. What it imports instead is the attestation, and
requiring that BEFORE the roll is what stops an operator importing a seal
nobody can verify (`docs/period-model.md` §1.3). The refusal also protects
the other direction: a roll while any job is still live is refused
outright, because a supervisor is one per run root and a new-root engine
cannot reach the old root's work.

## 20. Reclaim after a crashed roll

Goal: unblock a lineage whose successor claim points at a root that is
gone. This is the one verb here that can FORK a lineage. Read the whole
exercise before you type it.

1. Simulate the crash: start the roll of exercise 19 into a directory on
   removable storage, kill it after it has claimed, and take the storage
   away. The lineage head is now `claimed(<gone>)`.
2. Try to roll into a fresh directory instead:

```sh
uv run dsl41 run --open-from $R.anchor --run-root <run>/engine-p2b \
    examples/nightbank/estate/small/*.jil -p $P
```

```
the head is claimed and this root does not hold it: a physical roll opens the
period a committed seal left unopened, or resumes its own claim, and nothing
else (period-model ss7)
```

3. Prove the claimant is gone. Nothing in the tool can do this for you: a
   claimed head whose target is unreachable looks exactly like one whose
   target is merely paused, and if the claimant is alive this next command
   makes two roots open one period and run the same job twice.
4. Then, and only then:

```sh
uv run dsl41 estate reclaim --estate-anchor $R.anchor --force \
    --claimed-actor duty-manager@bank
```

```
reclaimed claim sha256:2e41… from /mnt/gone/engine-p2: period 2 may be opened again,
and the next opening `segment` will record that duty-manager@bank said so
```

5. Roll again into the fresh directory. It works now. Read the record it
   left:

```sh
head -1 <run>/engine-p2b/wal/000002.jsonl | grep -o '"reclaimed":[^}]*}'
```

6. Try `reclaim --force` on a head that is `open` or `closed`. It refuses:
   the verb moves a CLAIM and nothing else, because forcing a live period
   is not break-glass, it is vandalism.

*Why.* Exactly one root may succeed a seal, or the lineage forks and the
same `(job, run_number)` runs twice — the safety property the whole fence
exists to hold (`docs/period-model.md` §1.3). A claim is durable and
idempotent on its own id, so an ordinary crash of the claimant is
recovered by re-running the same roll. What cannot be recovered
automatically is a claimant that will never come back, and that judgement
is the operator's. The estate does not stop you; it records that you did
it, twice — in the anchor and in the next `segment` record — with the
actor you claimed to be.

## 21. Retention — what may go, and what may never

Goal: stop the run root growing, without deleting anything the model needs.

1. Survey first. `--dry-run` deletes nothing and gives every artifact a
   verdict:

```sh
uv run dsl41 estate prune --run-root $R --dry-run
```

```
would remove (0):
prunable, outside the flags given (0):
held (floor lifted, PR-Q3/E20 open) (4):
  held      wal    <run>/engine/wal/000001.jsonl  [period 1, PR-Q3] attested, …
floored (the model refuses) (8):
  floored   sentinel  <run>/engine/journal.jsonl  [estate, ss1.1] the one file …
  floored   anchor    <run>/engine.anchor/anchor.json  [estate, ss1.3] …
…
would remove 0 artifact(s), 0 byte(s); 8 floored, 4 held -- estate <uuid>,
period 2, attested [1]
```

2. Run it with no class named and read the refusal:

```
nothing selected: name at least one class (--tombstones, --quarantine) or ask
for --dry-run. A prune verb with a default set would be a retention policy,
and that is the operator's (period-model ss12)
```

3. Attest (exercise 17), then delete the class you chose:

```sh
uv run dsl41 estate prune --run-root $R --tombstones
```

   `--keep-runs N` and `--older-than-days D` narrow it further, and both
   are your policy rather than the model's. Neither bites on a training
   night: `--keep-runs` keeps the N newest spools OF EACH JOB, and no
   nightbank job has run more than a handful of times, so
   `--keep-runs 20` here would keep everything and delete nothing.

4. See what it cost. The run's ROW survives in `dsl41 runs` — that row is
   the journal's, not the spool's — and its TIMINGS do not: `clock_source`
   flips from `spool` to `journal`, because start and end came from
   `spawn.json` and `status.json` and those are now absent.

```sh
uv run dsl41 runs $R --job AMER_MKT_FX_C
```

5. Try to prune an unattested period's spool. You cannot: it is floored,
   and no flag reaches it.

*Why.* Three verdicts, not two (`docs/period-model.md` §12).
**floored** is the model's refusal — everything reachable from the
lineage head: the sentinel, the anchor and any live claim, the sidecars
this period opened from and will close with, the current and next
manifests, their catalog bundles, the newest attestation, and the WAL and
spool of any period that has not been attested. **prunable** is licensed
by name: a SPAWN tombstone whose period is attested and whose run has
ended, and a quarantined candidate. **held** is everything between — a
closed period's WAL, an older manifest, a superseded checkpoint. The floor
has lifted on those and one question is still open (may a seal-only
archive stand in for pruned inputs?), so nothing deletes them yet.
Pruning a tombstone only goes one way: after it, that period can no longer
be re-derived from its own evidence, and its attestation is the proof that
stands for it.

## Day-shift extras

- Browser UI: `dsl41 serve --socket $S` (then `dsl41 ui` per session).
- Whole-night dry run in seconds: `dsl41 rehearse` with the same files
  and a properties file from `nightbank props` (virtual clock, scripted
  adapters — see tests/test_nightbank_example.py for a scenario).
- Pictures: `dsl41 viz --format chart <estate files> -p <props>`, or
  `dsl41 viz --format html <estate files> -p <props> -o graph.html` for a
  self-contained page that renders offline in any browser —
  `--format html-chart` for that page holding the whole estate as one
  chart instead of the per-workflow report.
- Navigation: `dsl41 viz --format explore <estate files> -p <props> -o lens.html`
  — the whole estate as an interactive offline map: search a job, then
  right-click it to see only what feeds it (fan-in/fan-out, direct or
  tree); click any edge for its full annotation. Edges run orthogonally
  along the layout axis, so the picture keeps the layering the layout
  computed — at bank scale (523 nodes / 420 edges) that is the difference
  between a map and a hairball, and this is the answer to "what does APAC
  settlement actually wait for". Chrome, Safari and Firefox all drive it
  (the right-click menu needs a polyfill in Safari, vendored into the
  page, DL-77); if a browser still refuses the menu, the status line
  under the toolbar says so and every other control keeps working.
- The migration angle: `dsl41 lint`, `dsl41 report`, `dsl41 uc` all run
  on this estate — it is a full-pipeline fixture, not just a runner toy.

## Bank scale

`nightbank up --estate bank` runs the ~520-job profile (same topology,
8 asset classes × 8 shards, per-region valuation grids) — now the TUI's
tree, filter, and views earn their keep. Each estate ships its OWN
`incidents.conf` (the launcher copies the one next to the estate): the
bank names are per-asset-class, so the marks hang lands on
`EMEA_MKT_EQ_MARKS_C` and the recon breaks on `APAC_REC_EQ_TRADES_C`
(custody no-show and FX fail_once keep their small-estate names). The
contention exercise is 8 shards per asset class (`job_load: 40`,
shard 8 low priority) instead of 4. Everything else in this runbook
applies unchanged. Regenerate with different knobs:
`uv run python generate.py --asset-classes 4 --shards 6`.
