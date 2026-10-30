# Find a job's cause and send the intervention

This page is for an operator on shift with AutoSys operations experience.
It lets them find a job's cause, send an intervention and hand a dsl41 fault to the maintainers.
The rest of the track is under **Operator** in the
[README's Documents section](../README.md#documents).

## Set the variables and the account

Read this page at a terminal on the run host.
Set the section 0 variables (`deployment-runbook.md`, retired in 2.0.0).
Run `supervise`, `journal`, `runs` and every read of a file under `$RUN_ROOT` or `$ESTATE_ANCHOR` as the service account.
Run `release status` as the deployment identity.
Run `query`, `sendevent`, `off-hold-all` and `host` as yourself; section 0 gives the reasons.
Queries and `host list` need the `read` tier; `sendevent`, `off-hold-all` and mutating `host` verbs need `ops` ([access model](access-model.md#1-the-model)).
In commands, replace `NAME` with the job, `HOST_ID` with the host, and `REV` with its revision.
Replace `EVENT` with the chosen verb and `N` with the last trace sequence read.

## Check the engine and the period first

1. Read the engine unit's state and exit status:
   ```sh
   systemctl show -p ActiveState -p ExecMainStatus -p NRestarts dsl41-engine.service
   ```
2. Read the state of the lineage head in `anchor.json` as the service account (`jq` must be installed):
   ```sh
   jq -r .head.state "$ESTATE_ANCHOR/anchor.json"
   ```
3. Read the supervisor's runs as the service account:
   ```sh
   dsl41 supervise list --run-root "$RUN_ROOT"
   ```
4. Query the leader:
   ```sh
   dsl41 query status --brief -S "$S"
   ```

The query exits `0` when the leader answers, or `2` for no engine or a refusal.
The engine exits `2` for a configuration refusal; read its journal under What to watch (`deployment-runbook.md`, retired in 2.0.0).
The engine exits `3` after a seal; follow **SEALED, NOT OPENED** in Which move (`deployment-runbook.md`, retired in 2.0.0).
The engine exits `6` for a time-half stop; follow **TIME-HALF STOP** there.
The engine or supervisor exits `7` for a statechart error; follow **STATECHART ERROR** there.

An `open` head is normal.
A `closed` head means no new run starts until the next period opens.
A detached run started before the seal keeps running under the supervisor.
A `claimed` head means an opener is at work or has crashed; read Day 2 in section 6a (`deployment-runbook.md`, retired in 2.0.0).

## Read the job's state

1. Read the job's JSON:
   ```sh
   dsl41 query status -S "$S" --job NAME
   ```

The engine prints full status names; there are no two-letter codes.

| Status | What it says |
| --- | --- |
| `INACTIVE` | The job's initial status. |
| `QUE_WAIT` | The job waits in a resource queue. |
| `STARTING` | The job is live and starting. |
| `RUNNING` | The job is live; check the JSON field `held` before assuming a process runs. |
| `SUCCESS` | The recorded outcome is success; `CHANGE_STATUS` and a noexec bypass record it too. |
| `FAILURE` | The recorded outcome is failure. |
| `TERMINATED` | The recorded outcome is termination: a kill, a timer, a terminator, a cancelled queued start or a `CHANGE_STATUS`; the [generated lifecycle view](job-chart-lifecycle.md) names the events. |

The `--brief` line: `name  status  status_at  run N  exit N  rev N  FLAGS`.
`status_at` and `exit` use `-` when absent.
Flags: `I` means `on_ice`, `H` `on_hold`, `N` `on_noexec`, `A` `armed`; they join without separators, and a job with none has no flags cell.

Hold, ice and noexec are modes; a job on hold or on ice keeps its status, except that `ON_ICE` on a `QUE_WAIT` job dequeues it and settles it `INACTIVE` (DL-50).
The JSON field `held` has no brief flag; [the routing check](#the-job-has-not-started) covers it.
`log_out` and `log_err` name the current run's output targets for a `CMD` job, which exist once the run has written; they are null for a `BOX` or `FW` job.
`state_rev` is the value that `sendevent --expect` takes; the [query contract](control-protocol.md#4-query-verbs-frozen-response-shapes) defines the fields.

| AutoSys habit | dsl41 command |
| --- | --- |
| `autorep -J job` (the default `-s` summary) | `dsl41 query status --brief --job NAME -S "$S"` |
| `autorep -J job -d` | `dsl41 query status --job NAME -S "$S"` |
| `job_depends -c` | `dsl41 query deps --job NAME -S "$S"`; `dsl41 query explain --job NAME -S "$S"` |
| `autosyslog` | `dsl41 query trace -S "$S"`; `dsl41 journal "$RUN_ROOT"` as the service account |
| `sendevent -E X -J job` | `dsl41 sendevent EVENT -J NAME -S "$S"` |

## The job has not started

1. Read the condition:
   ```sh
   dsl41 query explain -S "$S" --job NAME
   ```
2. Read the upstream jobs, globals, downstream jobs and box membership:
   ```sh
   dsl41 query deps -S "$S" --job NAME
   ```
3. Read pending timers and the next calendar tick:
   ```sh
   dsl41 query timers -S "$S"
   ```
4. Read the planned dependency waves:
   ```sh
   dsl41 query plan -S "$S"
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

**Host not routing (the JSON field `held` is true).** The engine started the job and dispatched no process.
The job's executor routes no new effects: its host is drained or quarantined.
Read the host's `state` in `dsl41 host list -S "$S"`: `passive` is a drain, `quarantined` is the leader's mark for a host it cannot reach.
A drain is the tool for planned maintenance; confirm the drain is not deliberate before `host activate`.
`dsl41 host activate HOST_ID -S "$S"` ends a drain and dispatches the held starts.
On a quarantined host, `host activate` is rejected with `host_quarantined`; wait for the leader to reinstate the host when it answers again.

**Box not `RUNNING`.** `query deps` names the box; a plain start of the member writes `START_REFUSED` to `query trace`.
`STARTJOB` on the box starts a new box run and resets the jobs it contains to `INACTIVE`, except a `STARTING`, `RUNNING` or `QUE_WAIT` job and everything inside a subbox in one of those states (SEM-10).
`FORCE_STARTJOB` on the member bypasses the box gate instead (SEM-23).

**Tick missed during downtime.** Resume writes one `drop` record per missed tick to the WAL; nothing fires late.
Read the records as the service account; `.payload.job` names the job, `.at` the tick and `.reason` the cause:
```sh
jq -c 'select(.rec == "drop")' "$RUN_ROOT"/wal/*.jsonl
```
At resume the engine also prints `dropped KIND JOB @ AT: REASON` on stderr, which the engine unit's journal keeps.
Catch-up needs an explicit `FORCE_STARTJOB`.

**Outside `run_window`.** `dsl41 query spec -S "$S" --job NAME` shows the loaded window.
`FORCE_STARTJOB` still respects it; the next start follows SEM-33.

## The job is late

1. Read the trace after the last sequence read:
   ```sh
   dsl41 query trace -S "$S" --since N
   ```

A run that has not started by its `must_start_times` deadline writes `MUST_START_ALARM` to the trace, and one not complete by its `must_complete_times` deadline writes `MUST_COMPLETE_ALARM`; the deadlines hang on the schedule slot, not on elapsed run time, and neither alarm changes control flow (SEM-34).
`dsl41 query timers -S "$S"` lists the job's pending `must_start`, `must_complete` and `term_run_time` timers.
When `term_run_time` passes, the engine ends the run `TERMINATED`.
Nightbank exercise 5 (`RUNBOOK.md`, retired in 2.0.0) works through a hung job and `term_run_time`.

## The job failed or was terminated

Job logs can hold credentials. Check their contents before sharing them.

1. Read the files named by `log_out` and `log_err`, as the service account for files under `$RUN_ROOT`; replace the path placeholders:
   ```sh
   tail -n 100 -- LOG_OUT LOG_ERR
   ```
2. Read the trace after the last sequence read; use `0` for the whole current period:
   ```sh
   dsl41 query trace -S "$S" --since N
   ```
3. Read this job's run history as the service account:
   ```sh
   dsl41 runs --job NAME "$RUN_ROOT"
   ```

Default logs are `$RUN_ROOT/logs/<job>.<run>.out` and `.err`.
The runner appends to `std_out_file` and `std_err_file` when the JIL sets them.
The site rotates those external files between runs.

The trace's `transition` and `cause` name what happened.
Reset its cursor to `0` when `baseline_id` changes or `last_seq` falls below the cursor (DL-210).
Nightbank exercise 4 (`RUNBOOK.md`, retired in 2.0.0) works through a failure and rerun after a fix.

## The job shows RUNNING or STARTING and nothing runs

1. Read the supervisor's list as the service account:
   ```sh
   dsl41 supervise list --run-root "$RUN_ROOT"
   ```

Read `job_type` in [the job's JSON](#read-the-jobs-state) first.
A `BOX` has no process of its own; `query deps` names its members.
A running `FW` watches a file and starts no process; its `watching` field names the file.
A job whose JSON field `held` is true goes to [the routing check](#the-job-has-not-started).

`wrapper_alive` is true while the command's wrapper process runs.
A restarted supervisor's list is empty; the restart ended the commands it ran.
A `CMD` job that stays `RUNNING` after its row shows `wrapper_alive: false` needs a look: a completion takes a moment to apply, and the gap alone does not say why it has not.
Read the engine journal: a `dsl41: input quarantined` line names a quarantined completion, and then you [escalate](#escalate-a-dsl41-fault).
For a live row with `held: false` and no run in the list, follow **A row left live with nothing behind it** under What to watch (`deployment-runbook.md`, retired in 2.0.0) to choose the correction.

## Send the intervention

Read the runbook's Manual intervention (`deployment-runbook.md`, retired in 2.0.0) table for reversibility.
An intervention can start or end work; choose it from the cause before sending it.
For `CHANGE_STATUS`, add `-s STATUS` to the form below.
For `SET_GLOBAL`, use `-G KEY=value` instead of `-J NAME`; read `REV` with `dsl41 query global -N KEY -S "$S"`.

1. Send the chosen verb with the revision from the read:
   ```sh
   dsl41 sendevent EVENT -J NAME -S "$S" --expect REV
   ```
2. Read back the effect; for `SET_GLOBAL`, repeat the global query above instead:
   ```sh
   dsl41 query status -S "$S" --job NAME
   ```

| Exit | Meaning and next action |
| --- | --- |
| `0` | Applied; read back the effect. |
| `2` | Refused: the request was not admitted to the engine's WAL; a perimeter receipt may exist. |
| `3` | Rejected; the engine logs the rejection. `precondition_failed` means the target moved: re-read, then decide again. `engine_apply_faulted` means [escalate](#escalate-a-dsl41-fault). |
| `4` | Unknown; the event may still apply. Re-read before deciding on a retry. |

For exit `4`, any retry repeats the same arguments with the printed `--request-id`, `--expect`, `--epoch`, `--baseline` and `--claimed-actor`; stderr prints them before the first write.
The same user sends the retry from the same host, within the same period (DL-391).
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
- `dsl41 off-hold-all -S "$S"` sends one `OFF_HOLD` per job on hold at its status read.
  Exit `1` means at least one did not report applied; read each job's line, because an unknown one may still apply. `--dry-run` lists the jobs and sends nothing.
  The sweep cannot be retried as a whole.

## When the socket refuses

For query exit `2`, read the command's error text.
A missing socket reports `control socket PATH: REASON` with `No such file or directory`.
After a seal the socket is absent; [check the engine and head](#check-the-engine-and-the-period-first).
`access_denied` says `PRINCIPAL holds HAVE tier; ACTION needs NEED tier`.
An armed access map writes the denial to [`$RUN_ROOT/perimeter.jsonl`](access-model.md#6-receipts-the-perimeter-journal) and syncs it; a storage failure can leave no receipt, and the request stays denied.
`period_sealing` refuses requests during the cutoff; wait for the seal to commit, then [check the head](#check-the-engine-and-the-period-first).
For `lineage_lost`, send nothing and [escalate](#escalate-a-dsl41-fault).

## Escalate a dsl41 fault

Escalate `dsl41: statechart error`, `dsl41: input quarantined`, `engine failed:`,
`engine stopped:`, engine exit `6` or `7`, or `lineage_lost`.
Keep statechart report lines whole.
`lineage_lost` means the engine cannot prove it leads the lineage, because someone deleted or replaced the anchor directory.
A job's own exit code and logs belong to the application, not dsl41.
`SPEC DRIFT: estate files changed on disk` is a notice that the estate files on disk changed, not a fault; report it with the bundle.

When the engine is down (exit `6` or `7`, or a `closed` head), steps 3 and 4 fail.
Collect the output of `dsl41 journal "$RUN_ROOT"`, run as the service account, instead.

1. Collect both units' journal; replace the timestamp with the incident's start:
   ```sh
   journalctl -u dsl41-engine.service -u dsl41-supervisor.service --since "YYYY-MM-DD HH:MM:SS"
   ```
2. Collect the supervisor's list as the service account:
   ```sh
   dsl41 supervise list --run-root "$RUN_ROOT"
   ```
3. Collect the job's status JSON:
   ```sh
   dsl41 query status -S "$S" --job NAME
   ```
4. Collect the current period's trace:
   ```sh
   dsl41 query trace -S "$S" --since 0
   ```
5. Collect the lineage head as the service account:
   ```sh
   jq -c .head "$ESTATE_ANCHOR/anchor.json"
   ```
6. Collect the active release and last apply's outcome as the deployment identity:
   ```sh
   dsl41 release status
   ```
7. Record the runtime the engine runs under; its interpreter path names the directory under `/opt/dsl41/.venvs/`:
   ```sh
   tr '\0' ' ' < /proc/"$(systemctl show -p MainPID --value dsl41-engine.service)"/cmdline
   ```
   When the engine is down, record `readlink /opt/dsl41/.venv` instead; the link may have moved since the engine started.
8. Attach `$RUN_ROOT/supervisor.log`, read as the service account; it holds the supervisor's statechart report.
9. Attach the run's `log_out` and `log_err` files after the credential check above.

## What this page does not cover

- Installation and startup: runbook sections 1 to 3 (`deployment-runbook.md`, retired in 2.0.0).
- JIL releases and rollback: the job recipe (`deployment-runbook.md`, retired in 2.0.0).
- Sealing and opening a period: section 6a (`deployment-runbook.md`, retired in 2.0.0).
- Engine-fault recovery: Which move (`deployment-runbook.md`, retired in 2.0.0) and What to watch (`deployment-runbook.md`, retired in 2.0.0).
- A read-only user at a desk: no page yet; the [access model](access-model.md#1-the-model) defines the read tier they use.
