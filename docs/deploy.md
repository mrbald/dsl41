# Run one engine on one Linux box

For the operator installing, starting, watching and recovering one engine on
one Linux box. The rest of the [operator track](../README.md#documents)
covers incidents and worked estates. This page leaves
[active/standby and the 200-job estate](../TODO.md#release-2026-11-02-one-box)
to the next milestone.

## Install the package and unit

1. Install the release using the [package instructions](../README.md#install).
   Install it in `/opt/dsl41/releases/<version>`, with its executable at
   `/opt/dsl41/releases/<version>/bin/dsl41`. Substitute the concrete installed
   version in the commands and unit below.
2. Prepare the `dsl41` user and group. Give that user a writable run root at
   `/srv/dsl41/runs/nightbank` and access to the estate and its job commands.
3. Install application inputs separately under `/opt/nightbank/cfg`.
   Install the [example main config](../examples/nightbank/deploy/dsl41.toml)
   as `/etc/dsl41/dsl41.toml`. Its include glob selects the application's JIL;
   its properties path selects the site's bindings. Python inputs may use an
   `estate()` function returning `CatalogBuilder`, as with `dsl41 build`.
   The [Nightbank example](../examples/nightbank/README.md) is synthetic and
   repo-only; installing the Python package does not install that estate.
   Set `engine.run_root`, input patterns and properties in the main config.
   Paths may be absolute or relative to that config. The engine root must be
   absolute. Patterns support `*`, `?` and brackets; `**` is refused.
   The config is a regular file, never a symlink. Keep `estate.toml` for
   explicit portable fixtures and legacy `--manifest` invocations.
4. Copy the [engine unit](../examples/nightbank/deploy/dsl41-engine.service)
   to `/etc/systemd/system/dsl41-engine.service`.
   Substitute `<version>` in both executable paths. Set `RequiresMountsFor`
   to the configured run root and every required input filesystem.
   Set `ExecStop`'s socket to that root's `control.sock`.
5. As root, reload systemd's unit definitions:

   ```sh
   systemctl daemon-reload
   ```

6. As root, enable the unit and start it:

   ```sh
   systemctl enable --now dsl41-engine.service
   ```

   The [systemctl manual](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html)
   defines these unit-management commands.

Read the unit in these groups:

| Lines | What they do for the operator |
| --- | --- |
| `[Unit]`, `Description`, `RequiresMountsFor` | Name the service and require the run root's filesystem before starting. An absent mount must not look like a fresh root. |
| `StartLimitIntervalSec`, `StartLimitBurst` | Allow at most five starts in 300 seconds, then leave a repeated crash failed. |
| `[Service]`, `Type`, `User`, `Group` | Run the engine directly as `dsl41:dsl41`. |
| `ExecStart` | Read the main config. A root containing `engine.db` resumes its stored active catalog. |
| `ExecStop` | Ask for a drain with `engine stop --wait`. Wait for the socket to stop answering and `leader.lock` to become free. |
| `TimeoutStopSec` | Give the stop 45 seconds with the default graces. Revisit this if you change the engine's grace settings. |
| `Restart`, `RestartSec` | Restart after any exit, including 0, after five seconds. A CLI stop outside systemd therefore comes back on the same root. A systemd stop keeps the service down. |
| `RestartPreventExitStatus` | Keep exits 2, 6 and 7 down: startup refusal, a time-half fault before an engine input was logged, or a statechart assertion failure. Read the error before starting again. |
| `KillMode` | End remaining processes in the unit's control group after `ExecStop`, including jobs that escaped their process group. |
| `LimitNOFILE` | Set the open-file limit to 65536. |
| `[Install]`, `WantedBy` | Attach the enabled unit to `multi-user.target` for boot. |

After the start limit is hit, read the [unit's journal](#read-the-engines-output)
and fix the reported fault before starting again. As root, clear the limit:

```sh
systemctl reset-failed dsl41-engine.service
```

This resets the failed state and start counter; it does not start the engine.
Then follow [recovery](#recover-after-the-engine-or-box-dies).
See [systemctl reset-failed](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html#reset-failed%20%5BPATTERN%E2%80%A6%5D).

## Start the estate

Do not launch a second engine on the service's root. For a foreground run
with the service stopped, run this as the service user:

```sh
/opt/dsl41/releases/<version>/bin/dsl41 run --config /etc/dsl41/dsl41.toml
```

The engine prints `engine up; control socket: ...` when startup finishes.
It keeps running and accepts commands from another terminal.
It stores inputs in the root's `engine.db` and creates `engine.toml` with
default settings on the first run. The same command resumes an existing root.
Keep stop graces, snapshot interval and retention in `RUN_ROOT/engine.toml`.
With the default graces, the unit allows 45 seconds for a stop.
Check that timeout before changing either grace.

The main config's `[lint]` defaults are `refuse = "none"` and `ignore = []`.
Findings stay visible. `error` blocks unignored errors; `warn` also blocks
warnings. Info never blocks. Only registered lint codes may be ignored.
This policy cannot suppress input, lowering, exception or preflight errors.
`dsl41 lint --config /etc/dsl41/dsl41.toml` exits 1 for blocking findings and
2 for refused inputs. Exit 0 under `none` may include findings.
`dsl41 rehearse --config /etc/dsl41/dsl41.toml` uses the same interpretation;
its optional output `--run-root` must neither equal, contain nor lie inside
the engine's root.
Python preparation executes authoring code. Captured generated JIL is the
authority for update and replay; imported dependencies are not fully tracked.
Use prebuilt JIL when the deployment requires reproducible builds.

| Exit | What it means when `run` returns |
| --- | --- |
| 0 | Operator stop, including a drain that ended uncleanly. |
| 1 | Engine or estate failure while running, including failure to record shutdown. |
| 2 | Startup refused: preflight, input or resume error. |
| 6 | The time-processing part of an engine input failed before the input was logged. |
| 7 | A declared state machine hit an assertion failure. |

## Read the engine's output

```sh
journalctl -u dsl41-engine.service
```

Under the unit, read `engine stopped: ...`, startup refusals and exit-6/7
reports here ([refusals](../src/dsl41/cli_common.py#L412),
[fault reports](../src/dsl41/cli_run.py#L559)).
The unit leaves output routing to systemd's defaults:
stdout goes to the journal and stderr follows it, unless the host overrides
those defaults. See [systemd output routing](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#StandardOutput=)
and [journalctl's unit filter](https://www.freedesktop.org/software/systemd/man/latest/journalctl.html#-u).
The engine prints its shutdown record in
[the run command](../src/dsl41/cli_run.py#L611).

## Watch the engine and jobs

```sh
/opt/dsl41/releases/<version>/bin/dsl41 query status \
  --socket /srv/dsl41/runs/nightbank/control.sock
```

The command prints JSON. `engine` reports the engine state, including
STARTING during restart catch-up and DRAINING during a stop.
`jobs` contains job status and revisions. An unsettled command can have an
`attempt` block; a held interruption has `interrupted` evidence.
`config_rev` names the applied configuration; `update` appears while an
update is in progress. See the [status contract](control-protocol.md#4-query-verbs-frozen-response-shapes)
for field meanings.

Exit 0 means the engine answered, even if a job failed.
Exit 2 means refusal or failure to reach the engine.
Run socket commands as the service user with this unit's owner-only socket.
For a configured access map, follow the [access model](access-model.md);
stop and update require its `adm` tier.

## Drain the engine

A CLI stop of this systemd service triggers its restart policy.
To keep it down, run this step as root:

1. Stop the unit:

   ```sh
   systemctl stop dsl41-engine.service
   ```

   Systemd runs the drain through the unit's
   [ExecStop](../examples/nightbank/deploy/dsl41-engine.service#L30).

For a foreground engine, request the drain directly:

```sh
/opt/dsl41/releases/<version>/bin/dsl41 engine stop --wait \
  --socket /srv/dsl41/runs/nightbank/control.sock
```

The command prints the engine's JSON answer. The engine refuses new
mutations with `engine_draining`, sends TERM to running commands, allows
the grace, then sends KILL where needed. It records how each attempt ended.
Queries remain available during the drain. A second stop moves the drain
deadline to the present; it does not grant more time.

| Default bound | Seconds |
| --- | --- |
| TERM to KILL (`grace_term_s`) | 10 |
| KILL to exit-record grace (`grace_kill_s`) | 5 |
| Engine drain deadline (both graces plus margin) | 20 |
| Unit stop timeout | 45 |

With `--wait`, exit 0 means the engine exited or no engine holds the root.
An already stopped engine prints `engine not running: ...`.
Without `--wait`, exit 0 means it took the stop request.
Exit 2 means refusal or a communication failure; a lost reply can leave
the outcome unknown. Read status before retrying.
Do not retry while status says DRAINING unless you intend to cut the grace:
a [second stop moves the deadline to now](../src/dsl41/engine_charts.py#L205).

Neither stop exit 0 nor `run` exit 0 proves a clean drain.
Read `engine stopped: ...` in the [unit's journal](#read-the-engines-output),
or in the foreground terminal. The stored record says how the stop ended:

| Record | Meaning |
| --- | --- |
| `clean_shutdown` | Every attempt settled. |
| `unclean_shutdown` | The drain deadline passed with unsettled attempts retained for restart. |
| `shutdown_during_startup` | The operator stopped restart catch-up; the next restart takes the remaining steps. This stop exits 0. |

The [shutdown actions](../src/dsl41/engine_charts.py#L213) define these
records; [catch-up handling](../src/dsl41/runner.py#L687) retains the work.

The TUI does not show the drain, its deadline, the stored plan or the
interruption evidence. Use `dsl41 query status` and the exit status of
`dsl41 engine stop` as the workaround. Status exposes the engine state and
attempt evidence; it does not provide a drain deadline or a full stored-plan
view. Once the engine exits, its socket cannot answer status.

## Reload a changed estate

1. Prepare the changed estate on disk, then build a plan:

   ```sh
   /opt/dsl41/releases/<version>/bin/dsl41 reread
   ```

   The command prints added, changed and removed jobs, the unchanged count,
   running jobs it would stop, non-idempotent jobs among them, warnings and
   `token: ...`. It stores the plan under the run root's `plans/` directory.
   It changes no live jobs. Exit 0 means no running job would stop; exit 1
   means at least one would stop; exit 2 means refusal.

2. Review the printed stops and replace `TOKEN` below with the printed token:

   If the reviewed plan stops running jobs, add `--stop-running` to consent
   before submitting.

   ```sh
   /opt/dsl41/releases/<version>/bin/dsl41 update TOKEN --wait
   ```

   The engine checks the plan against its current state.
   It can refuse a stale plan, changed running-box membership, unconsented
   stops or another update in progress. Reread after a stale-plan refusal.

   The command prints `admitted: plan ...` and, when applied,
   `applied: catalog revision ...` with the changed jobs and stop choices.
   The engine keeps running. It stops affected commands before swapping the
   catalog; interrupted jobs rerun under the new definition or remain held
   when marked in `non-idempotent.toml`.

   With `--wait`, exit 0 means admitted and applied; exit 1 means admitted
   but still pending when the engine's wait ended (`pending: ...`).
   Without `--wait`, exit 0 means admitted. Exit 2 means refusal or failure
   to reach the engine. For an unknown outcome, use the printed request id
   with `--request-id` rather than submit a new update blindly.

3. Run the status command above to check `config_rev`, any pending `update`
   and held interruption evidence.

Both reload commands use `/etc/dsl41/dsl41.toml`; use `--config PATH` for a
different host config. Reread prints job origins and retains findings with
the plan. A source move alone keeps jobs running. Update uses only the
reviewed plan's captured bytes and policy, even after sources are edited or
removed. Update reads only `engine.run_root` from the selected TOML file.
Changes to `as_machine`, `machine_policy` or access-map selection need restart.
Reread names any changed host settings and requests that restart first.
Changing `engine.run_root` selects another root; it does not migrate an engine.

## Upgrade the engine

Only select a release documented as compatible with the existing store and
state-machine version. The stored active estate must match what that release
can interpret. Compatibility is allocated per release; arbitrary upgrades
and rollbacks are not supported.

A root created through legacy files or a manifest has no main-config capture.
It cannot resume through `--config`. Keep its legacy invocation or select a
fresh root for the main-config service. There is no root migration command.

1. Drain the engine with `systemctl stop dsl41-engine.service`.
2. Install the new release in its own `/opt/dsl41/releases/<version>` directory.
3. Select that executable in both `ExecStart` and `ExecStop`.
4. Run `systemctl daemon-reload`.
5. Run `systemctl start dsl41-engine.service` and check status.

Application inputs and reviewed updates are independent of that selection.

## Recover after the engine or box dies

Keep the durable run root, including `engine.db`, `runs/` and `plans/`.
For the service, run this step as root:

1. Start the same unit on that root:

   ```sh
   systemctl start dsl41-engine.service
   ```

   For a foreground engine, use the [start command](#start-the-estate) instead.

An engine death ends its running attempts; restart does not adopt them
as continuing jobs. After a box reboot, a changed boot identity proves
the old processes did not survive.

The engine restores its snapshot and later records, then reads each open
attempt's records once. A recorded natural completion keeps its actual
result. Without an exit record, the outcome is `unobservable` with cause
`engine_death`, unless the attempt already had a stop cause.
Restart does not wait for an old wrapper or a late exit record.
When the wrapper records termination after losing its parent, the attempt
has `kind: stopped` and `cause: lifeline`. See the
[wrapper's parent-loss record](../src/dsl41/runner_wrapper.py#L39) and its
[outcome mapping](../src/dsl41/engine_charts.py#L369).

The interruption rule reruns an interrupted job as a new attempt of the
same run, holds a job marked in `non-idempotent.toml` or at the attempt limit,
or takes no action
when the run already ended or the job left the catalog.
The [attempt limit](../src/dsl41/runner_attempts.py#L79) is:

5 attempts per run.

An interruption at that limit or above holds even an unmarked job.
Its `interrupted` evidence carries `limit: 5`, as set by the
[interruption rule](../src/dsl41/runner_attempts.py#L349).
See the [job author's interruption contract](authoring.md#62-write-every-job-to-survive-interruption) before
deciding which jobs may rerun.
Unreadable or mismatched attempt records hold affected jobs with evidence.
Residual processes are cleaned up by the live engine; uncertain process
readings keep cleanup held and are checked again.

Status remains STARTING during catch-up. The engine takes up file watches,
owed plans, pending starts and any interrupted update before the final
time jump. It then admits deferred dispatches and announces startup.
A stop during catch-up leaves the remaining work for the next restart.

A refused resume leaves the unit down on exit 2. Read its named refusal;
do not erase the root to bypass it. The restart checks the lock, store
version and identity, clock domain and any pending update's captured plan.
A missing or unreadable interrupted-update plan refuses before a journal
write or process signal.

Main-config restart restores the last applied estate interpretation from
captured bytes. Estate and lint edits remain pending, including invalid
`engine.environment` selections. Restart still reads the selected config
as TOML and requires its supported format. It applies current `as_machine`,
`machine_policy` and access-map settings; invalid host settings refuse with
exit 2. An omitted access map selects owner-only access. These settings stay
effective while an interrupted update applies its captured plan.
Other runtime-profile fields retain their stored values and checks (DL-437).

Restart checks machine placement against the stored catalog and current
host settings. It does not repeat the general launch preflight or lint.
The captured JIL is recompiled without opening the original sources or
importing Python. After startup a synchronous source scan reports
`N pending source changes; run dsl41 reread` once, including a zero count.
Legacy file and manifest starts keep their matching-estate checks.
See [runner recovery](runner-design.md#7-journal-and-recovery-e1-prod-grade)
and DL-433 in the [decision log](decision-log.md) for the restart rule.

2. Read the engine state as the service user:

   ```sh
   /opt/dsl41/releases/<version>/bin/dsl41 query status \
     --socket /srv/dsl41/runs/nightbank/control.sock
   ```

   Look for `engine: STARTING` during catch-up, then `engine: RUNNING`.
   A fast restart may already report RUNNING on the first query.
   The [status response](../src/dsl41/runner_control.py#L860) reads the
   [engine lifecycle](../src/dsl41/engine_charts.py#L251).
