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
dsl41 query status --socket $S              # every job, one line each
dsl41 query status --socket $S --job EMEA_MKT_MARKS_C
dsl41 query trace  --socket $S | tail -20   # the transition log
dsl41 query explain --socket $S --job SOD_B # per-atom condition truth
dsl41 query spec   --socket $S --job SOD_B  # the loaded JIL block, verbatim
dsl41 query deps   --socket $S --job EMEA_ACC_GL_CLOSE_C  # needs / blocks
dsl41 query timers --socket $S              # everything due next, estate-wide
dsl41 query plan   --socket $S              # topological waves
dsl41 query subscribe --socket $S           # live journal stream (Ctrl-C)
dsl41 query is-success --socket $S -J APAC_EOD_B && echo done  # shell glue

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
3. Watch `SOD_FLIP_C` swap `data/pending/` → `data/current/` (the
   transactional new-day activation) and publish `SOD_DATE`;
   `SOD_WARMUP_C` proves it by reading from `current/`.

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
read a global's effective truth. (SET_GLOBAL events are edge-triggered —
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

Try it: after a night completes, change `EMEA_ACC_CASH_C`'s sleep and
start a new night. The point of the hash gate: a journal only replays
against the estate that wrote it.

## 12. Ice, hold, noexec

- `OPS_LEGACY_REPORT_C` is born ON_ICE (`status: ON_ICE` in JIL). Its
  upstream succeeds, it never runs — and any status atom naming an iced
  job evaluates TRUE, so nothing downstream would wait for it. Revive:
  `dsl41 sendevent OFF_ICE -J OPS_LEGACY_REPORT_C --socket $S`.
- `ON_HOLD`/`OFF_HOLD`: park a healthy job before it starts (try it on
  `OPS_ARCHIVE_C` early).
- `ON_NOEXEC`: the job "runs" without spawning anything (status flows,
  no process) — vendor bypass semantics; try it on a shard.
- `OPS_XINST_DEMO_C` waits on `s(GROUP_TREASURY_EOD^PRD, 9999)` — a job
  in another (production) instance this sandbox can't see. `explain`
  shows the atom; release it with
  `dsl41 sendevent CHANGE_STATUS -J "GROUP_TREASURY_EOD^PRD" -s SUCCESS --socket $S`.

## Day-shift extras

- Browser UI: `dsl41 serve --socket $S` (then `dsl41 ui` per session).
- Whole-night dry run in seconds: `dsl41 rehearse` with the same files
  and a properties file from `nightbank props` (virtual clock, scripted
  adapters — see tests/test_nightbank_example.py for a scenario).
- Pictures: `dsl41 viz --whole-graph <estate files> -p <props>`.
- The migration angle: `dsl41 lint`, `dsl41 report`, `dsl41 uc` all run
  on this estate — it is a full-pipeline fixture, not just a runner toy.

## Bank scale

`nightbank up --estate bank` runs the ~520-job profile (same topology,
8 asset classes × 8 shards, per-region valuation grids). Same incidents
(plus per-asset-class variants), same runbook — but now the TUI earns its
keep. Regenerate with different knobs: `uv run python generate.py
--asset-classes 4 --shards 6`.
