# Find a job's cause and send the intervention

This page is for an operator on shift with AutoSys operations experience.
It lets them find a job's cause, send an intervention and hand a dsl41 fault to the maintainers.
The rest of the track is under **Operator** in the
[README's Documents section](../README.md#documents).

## Set the variables and the account

Read this page at a terminal on the run host. Select the executable version
from the installed unit:

```sh
systemctl cat dsl41-engine.service
```

Use its `ExecStart` version in the variables below:

```sh
DSL41=/opt/dsl41/releases/<version>/bin/dsl41
RUN_ROOT=/srv/dsl41/runs/nightbank
S="$RUN_ROOT/control.sock"
```

Replace `<version>` with the installed version before running commands.
These paths match the [single-engine installation](deploy.md#install-the-package-and-unit).
Run the commands below as the service account for its owner-only socket and
run-root files. Use root or the site's journal-reading account for systemd
output. With an access map, your own account needs `read` for queries and
`ops` for interventions; a socket tier grants no filesystem access.
[Stop and update require adm](access-model.md#10-the-verb-table).
Replace `NAME`, `REV`, `EVENT` and `N` with the job, its revision, the chosen
verb and the last trace sequence read. Use `0` for a fresh trace read.

<a id="check-the-engine-and-the-period-first"></a>
## Check the engine and stored revision first

1. Read the engine unit's state and exit status:

   ```sh
   systemctl show -p ActiveState -p ExecMainStatus -p NRestarts dsl41-engine.service
   ```

2. Read its output:

   ```sh
   journalctl -u dsl41-engine.service -n 100 --no-pager
   ```

3. Query the engine, including its lifecycle and configuration revision:

   ```sh
   "$DSL41" query status -S "$S"
   ```

Query exit `0` means the engine answered, even if a job failed.
Exit `2` means refusal or a communication failure; read the error text.
`engine` is STARTING during catch-up, RUNNING while serving, or DRAINING
during stop. `config_rev` identifies the applied estate; `update` identifies
an update still stopping jobs. Follow the [status contract](control-protocol.md#4-query-verbs-frozen-response-shapes)
for the response fields.

If the engine cannot answer, continue with the
[engine-down evidence collection](#escalate-a-dsl41-fault).
Run exit `2` is a startup refusal; exit `6` is a time-half fault; exit `7`
is a statechart fault. Do not loop restarts of exits `6` or `7`.
[Recovery](deploy.md#recover-after-the-engine-or-box-dies) explains what to
preserve and which checks can refuse a restart.

## Read the job's state

1. Read the job's JSON:
   ```sh
   "$DSL41" query status -S "$S" --job NAME
   ```

The engine prints full status names, not two-letter codes. Use the
[status fields](control-protocol.md#4-query-verbs-frozen-response-shapes)
and [generated job lifecycle](job-chart-lifecycle.md) to distinguish queued,
live and terminal jobs. `SUCCESS` can also follow an operator's
`CHANGE_STATUS` or a noexec bypass; it does not alone prove a command ran.

The `--brief` line: `name  status  status_at  run N  exit N  rev N  FLAGS`.
Its status label can include attempt or cleanup notes. Use JSON for evidence.
`status_at` and `exit` use `-` when absent.
Flags: `I` means `on_ice`, `H` `on_hold`, `N` `on_noexec`, `A` `armed`; they join without separators, and a job with none has no flags cell.

Hold, ice and noexec are modes; a job on hold or on ice keeps its status, except that `ON_ICE` on a `QUE_WAIT` job dequeues it and settles it `INACTIVE` (DL-50).
`log_out` and `log_err` name the current run's output targets for a `CMD` job, which exist once the run has written; they are null for a `BOX` or `FW` job.
`state_rev` is the value that `sendevent --expect` takes; the [query contract](control-protocol.md#4-query-verbs-frozen-response-shapes) defines the fields.

| AutoSys habit | dsl41 command |
| --- | --- |
| `autorep -J job` (the default `-s` summary) | `"$DSL41" query status --brief --job NAME -S "$S"` |
| `autorep -J job -d` | `"$DSL41" query status --job NAME -S "$S"` |
| `job_depends -c` | `"$DSL41" query deps --job NAME -S "$S"`; `"$DSL41" query explain --job NAME -S "$S"` |
| `autosyslog` | `"$DSL41" query trace -S "$S"`; `"$DSL41" journal "$RUN_ROOT"` as the service account |
| `sendevent -E X -J job` | `"$DSL41" sendevent EVENT -J NAME -S "$S"` |

## The job has not started

1. Read the condition:
   ```sh
   "$DSL41" query explain -S "$S" --job NAME
   ```
2. Read the upstream jobs, globals, downstream jobs and box membership:
   ```sh
   "$DSL41" query deps -S "$S" --job NAME
   ```
3. Read pending timers and the next calendar tick:
   ```sh
   "$DSL41" query timers -S "$S"
   ```
4. Read the planned dependency waves:
   ```sh
   "$DSL41" query plan -S "$S"
   ```

`plan` covers the AND-success skeleton; `plan_cycle` names a cycle.

**Condition false.** `query explain` names each atom and its truth value.
For a global atom, `actual` gives its effective value; `null` means never set.
`SET_GLOBAL` can correct a global; `FORCE_STARTJOB` bypasses the condition (SEM-23).

**On hold (`H`).** `query status --brief` shows the flag.
`OFF_HOLD` clears it; read [its start effect](#send-the-intervention) first (SEM-21).

**On ice (`I`).** `query status --brief` shows the flag.
`OFF_ICE` clears it; [the intervention differences](#send-the-intervention) cover re-evaluation.

**Armed (`A`).** `query status --brief` shows the latch on an `INACTIVE` job.
A queued start keeps the latch until the job starts (SEM-32).
Before `OFF_HOLD`, read [its start effect](#send-the-intervention) (SEM-21).
`DISARM` clears only the latch; it changes no status and starts nothing (DL-158).

**Resource queue (`QUE_WAIT`).** `query status` identifies the queue wait.
If an ended job still holds units, `RELEASE_RESOURCE` targets that holder to free them.

**Box not `RUNNING`.** `query deps` names the box; a plain start of the member writes `START_REFUSED` to `query trace`.
`STARTJOB` on the box starts a new box run and resets the jobs it contains to `INACTIVE`, except a `STARTING`, `RUNNING` or `QUE_WAIT` job and everything inside a subbox in one of those states (SEM-10).
`FORCE_STARTJOB` on the member bypasses the box gate instead (SEM-23).

**Tick missed during downtime.** Restart skips and records missed ticks;
nothing fires late. Read the unit output from the incident window:

```sh
journalctl -u dsl41-engine.service --since "YYYY-MM-DD HH:MM:SS" --no-pager
```

The engine reports `dropped KIND JOB @ AT: REASON`.
The durable `drop` evidence lives in `engine.db`; `wal/*.jsonl` resolves to
the [SQLite store rule (DL-428)](decision-log.md).
Catch-up needs an explicit `FORCE_STARTJOB` after checking downstream effects.

**Outside `run_window`.** `"$DSL41" query spec -S "$S" --job NAME` shows the loaded window.
`FORCE_STARTJOB` still respects it; the next start follows SEM-33.

## The job is late

1. Read the trace after the last sequence read:
   ```sh
   "$DSL41" query trace -S "$S" --since N
   ```

A run that has not started by its `must_start_times` deadline writes `MUST_START_ALARM` to the trace, and one not complete by its `must_complete_times` deadline writes `MUST_COMPLETE_ALARM`; the deadlines hang on the schedule slot, not on elapsed run time, and neither alarm changes control flow (SEM-34).
`"$DSL41" query timers -S "$S"` lists the job's pending `must_start`, `must_complete` and `term_run_time` timers.
When `term_run_time` passes, the engine ends the run `TERMINATED`.
For a synthetic hung-job exercise, use [Nightbank](../examples/nightbank/README.md#quick-start)
and its incident settings; `RUNBOOK.md` exercise 5 resolves to that preparation.

## The job failed or was terminated

Job logs can hold credentials. Check their contents before sharing them.

1. Read the files named by `log_out` and `log_err`, as the service account for files under `$RUN_ROOT`; replace the path placeholders:
   ```sh
   tail -n 100 -- LOG_OUT LOG_ERR
   ```
2. Read the trace after the last sequence read; use `0` for all trace entries the engine currently serves:
   ```sh
   "$DSL41" query trace -S "$S" --since N
   ```
3. Read this job's run history as the service account:
   ```sh
   "$DSL41" runs --job NAME "$RUN_ROOT"
   ```

Default logs are `$RUN_ROOT/logs/<job>.<run>.out` and `.err`.
The runner appends to `std_out_file` and `std_err_file` when the JIL sets them.
The site rotates those external files between runs.

The trace's `transition` and `cause` name what happened.
Reset its cursor to `0` when `baseline_id` changes or `last_seq` falls below
the cursor. A restart from a snapshot can begin with `RESTART`; `CONFIG`
marks a configuration change. Trace is a view, not complete retained history.
DL-210's period-roll instructions resolve to the
[current trace contract](control-protocol.md#4-query-verbs-frozen-response-shapes).
`RUNBOOK.md` exercise 4 resolves to [Nightbank preparation](../examples/nightbank/README.md#quick-start);
use the intervention below only after correcting the application's fault.

## The job shows RUNNING or STARTING and nothing runs

1. Read the job's full status, including attempt and interruption evidence:

   ```sh
   "$DSL41" query status -S "$S" --job NAME
   ```

2. Read its stored run history:

   ```sh
   "$DSL41" runs --job NAME --format json "$RUN_ROOT"
   ```

A `BOX` has no process; `query deps` names its members.
A live `FW` polls a file; its `watching` field identifies the watch.
For a `CMD`, read `attempt.state`, `after`, `cause` and `note`.
A stop ladder, failed process scan or surviving process can keep a start
held even after an outcome. Do not infer a missing process from status alone.
Command evidence is under `runs/<job>.<run>/<ordinal>/` in the run root;
`spawn.json` and `status.json` identify that attempt.
Do not signal an unverified PID or edit those records.
`wrapper_alive` inspection resolves to this attempt and cleanup evidence.

An engine death ends the attempt. A restart can launch a replacement of the
same run; marked non-idempotent jobs, quarantined records and the attempt
limit can hold instead. Read `interrupted` before releasing a hold.
The [recovery procedure](deploy.md#recover-after-the-engine-or-box-dies)
explains natural completions, interruptions and residual cleanup.
A quarantined engine input or repeated cleanup fault needs
[escalation](#escalate-a-dsl41-fault).

## Send the intervention

Read the [intervention contract](control-protocol.md#3-mutating-verb-sendevent)
and the [job's interruption requirements](authoring.md#62-write-every-job-to-survive-interruption).
The `deployment-runbook.md` Manual intervention table resolves to those rules.
An intervention can start or end work; choose it from the cause before sending it.
For `CHANGE_STATUS`, add `-s STATUS` to the form below.
For `SET_GLOBAL`, use `-G KEY=value` instead of `-J NAME`; read `REV` with `"$DSL41" query global -N KEY -S "$S"`.

1. Send the chosen verb with the revision from the read:
   ```sh
   "$DSL41" sendevent EVENT -J NAME -S "$S" --expect REV
   ```
2. Read back the effect; for `SET_GLOBAL`, repeat the global query above instead:
   ```sh
   "$DSL41" query status -S "$S" --job NAME
   ```

| Exit | Meaning and next action |
| --- | --- |
| `0` | Applied; read back the effect. |
| `2` | Refused: the request was not admitted to the engine's store; a perimeter receipt may exist. |
| `3` | Rejected; the engine logs the rejection. `precondition_failed` means the target moved: re-read, then decide again. `engine_apply_faulted` means [escalate](#escalate-a-dsl41-fault). |
| `4` | Unknown; the event may still apply. Re-read before deciding on a retry. |

For exit `4`, any retry repeats the same arguments with the printed `--request-id`, `--expect`, `--baseline` and `--claimed-actor`; stderr prints them before the first write.
Keep the same user, host and actor envelope (DL-391).
Retry only while the store retains the original request; the default journal
retention is 90 days, and pruning also depends on snapshots and open work.
An unchanged baseline does not prove the request remains retained.
The [retry contract](control-protocol.md#3-mutating-verb-sendevent) rules this limit.
A refused retry says nothing about the original; re-read the target.

- `FORCE_STARTJOB` clears a non-live job's hold or ice first; the flag stays cleared (SEM-23).
  A force on `STARTING`, `RUNNING` or `QUE_WAIT` starts nothing: the command exits `0` and the trace shows `START_REFUSED`.
- `OFF_ICE` re-evaluates nothing; the condition must recur (SEM-20).
  A member of a `RUNNING` box that has not run in that run sits the run out; `FORCE_STARTJOB` still starts it.
  That is the default `next-run` value of the [`off-ice-in-running-box` switch](runner-design.md#8a-semantic-switches).
- `OFF_HOLD` re-evaluates the job's start: a job whose start conditions hold starts at once, and an armed job starts through the schedule gate (SEM-21).
- The engine ignores `ON_HOLD` or `ON_ICE` on a `STARTING` or `RUNNING` job (SEM-21, SEM-20).
- Send `KILLJOB` to members, not boxes.
- `CHANGE_STATUS` cannot set `QUE_WAIT`; the engine reports `status_not_injectable`.
- `"$DSL41" off-hold-all -S "$S"` sends one `OFF_HOLD` per job on hold at its status read.
  Exit `1` means at least one did not report applied; read each job's line, because an unknown one may still apply. `--dry-run` lists the jobs and sends nothing.
  The sweep cannot be retried as a whole.

## When the socket refuses

For query exit `2`, read the command's error text.
A missing socket reports `control socket PATH: REASON` with `No such file or directory`.
A missing socket alone does not identify a cause;
[check the unit and engine](#check-the-engine-and-stored-revision-first).
`access_denied` says `PRINCIPAL holds HAVE tier; ACTION needs NEED tier`.
An armed access map writes the denial to [`$RUN_ROOT/perimeter.jsonl`](access-model.md#6-receipts-the-perimeter-journal) and syncs it; a storage failure can leave no receipt, and the request stays denied.

## Escalate a dsl41 fault

Escalate `dsl41: statechart error`, `dsl41: input quarantined`,
`engine failed:`, unexplained shutdowns, or engine exit `6` or `7`.
A normal `engine stopped: clean_shutdown` is a stop record, not a fault.
Keep statechart report lines whole. A job's own exit code and logs belong
to the application. `SPEC DRIFT: estate files changed on disk` and
`N pending source changes; run dsl41 reread` report pending sources;
[review an update](deploy.md#reload-a-changed-estate) to adopt them.

Evidence can contain credentials or private estate data. Check every file
before sharing. Preserve the original run root; do not delete or edit its
store, plans or spool to bypass a refusal.

1. Collect the engine unit's journal from the incident start:

   ```sh
   journalctl -u dsl41-engine.service --since "YYYY-MM-DD HH:MM:SS" --no-pager
   ```

2. Collect the unit definition and its executable selection:

   ```sh
   systemctl cat dsl41-engine.service
   ```

3. Record the selected executable's version:

   ```sh
   "$(dirname "$DSL41")/python" -c 'from importlib.metadata import version; print(version("dsl41"))'
   ```

4. With the engine up, collect the job status:

   ```sh
   "$DSL41" query status -S "$S" --job NAME
   ```

5. With the engine up, collect its available trace:

   ```sh
   "$DSL41" query trace -S "$S" --since 0
   ```

6. With the engine down, replay the retained store as the service account:

   ```sh
   "$DSL41" journal "$RUN_ROOT"
   ```

7. Collect run history whether the engine is up or down:

   ```sh
   "$DSL41" runs --format json "$RUN_ROOT"
   ```

   These offline readers need no socket or source estate files. Exit `0`
   means read; exit `2` means refusal. Preserve the refusal text if replay
   fails. Retention can limit history; `partial` or `UNKNOWN` is evidence of
   missing history, not an inferred application result.

8. Attach the selected `/etc/dsl41/dsl41.toml`, `engine.toml`, relevant
   `plans/` and attempt spool records after checking their contents.
   Record the `config_rev`, plan token and request envelope when available.
9. Attach the job's `log_out` and `log_err` after checking for credentials.

## What this page does not cover

- Installation, startup, drain and recovery: [deploy.md](deploy.md).
- JIL edits and rollback by a reviewed candidate: [reload a changed estate](deploy.md#reload-a-changed-estate).
- Application idempotence and manual hold release: [authoring contract](authoring.md#62-write-every-job-to-survive-interruption).
- Read access at a desk: [access model](access-model.md#1-the-model).

The `deployment-runbook.md` installation, job recipe, Which move and What to
watch references resolve to these procedures. Its section 6a sealing and
opening instructions resolve to the [v2 charter (DL-424)](decision-log.md).

`anchor.json` resolves to engine status and the stored revision (DL-424).
Periods, seals, detached runs and `run` exit `3` resolve to DL-424 in the [decision log](decision-log.md).
`supervise` resolves to the engine queries and offline readers above (DL-424).
`host` resolves to the one-box scope in DL-424.
`release status` resolves to the applied `config_rev` in engine status (DL-424).
`dsl41-supervisor.service` resolves to the engine unit's journal (DL-424).
`supervisor.log` resolves to that same journal (DL-424).
`$ESTATE_ANCHOR` resolves to the run-root evidence above (DL-424).
`.venv` and `.venvs` executable references resolve to the unit's versioned `ExecStart` path.
