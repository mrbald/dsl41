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
