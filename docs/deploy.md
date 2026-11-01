# Run one engine on one Linux box

For the operator installing, starting, watching and recovering one engine on
one Linux box. The rest of the [operator track](../README.md#documents)
covers incidents and worked estates. This page leaves application authoring
to the [job author guide](authoring.md) and site qualification to the
[one-box milestone](../TODO.md#release-2026-11-02-one-box).

## Install the package and unit

1. As root, create the release environment and install the selected version.
   Substitute a concrete version for `<version>` in every command and in
   the unit. The [package instructions](../README.md#install) describe extras.

   ```sh
   python3.12 -m venv /opt/dsl41/releases/<version>
   /opt/dsl41/releases/<version>/bin/pip install dsl41==<version>
   ```

2. Prepare the `dsl41` system user and group using the site's account tools.
   Jobs run as this user; an `owner` naming another user refuses preflight.
   As root, create the service and application directories:

   ```sh
   install -d -m 0755 /etc/dsl41 /opt/nightbank /opt/nightbank/cfg
   install -d -m 0755 /srv/dsl41 /srv/dsl41/runs /srv/dsl41/nightbank
   install -d -o dsl41 -g dsl41 -m 0700 /srv/dsl41/runs/nightbank
   ```

3. From the checkout, install the synthetic small Nightbank inputs as root:

   ```sh
   cp examples/nightbank/estate/small/*.jil /opt/nightbank/cfg/
   cp -R examples/nightbank/bin /opt/nightbank/
   install -m 0644 examples/nightbank/deploy/dsl41.toml /etc/dsl41/dsl41.toml
   ```

   Use an empty application config directory so its glob cannot select stray
   JIL. The Python package does not install this repo-only estate.
   Read [what the main config selects](#what-the-main-config-selects).

4. Generate the night's properties and directories with the installed
   interpreter, from the same checkout. Replace `<version>` first.
   Choose a fresh night directory; preparing over an existing night's data
   would mix its incident counters and outputs.

   ```sh
   /opt/dsl41/releases/<version>/bin/python - <<'PY'
   import runpy
   import sys
   from datetime import UTC, datetime, timedelta
   from pathlib import Path

   night = Path("/srv/dsl41/nightbank/night")
   if night.exists():
       raise SystemExit("Choose a fresh night directory")
   prepare = runpy.run_path("tests/nightbank_props.py")["prepare_night"]
   props = prepare(night, "small", anchor_utc=datetime.now(UTC) + timedelta(minutes=15))
   props["OWNER"] = "dsl41"
   (night / "night.properties").unlink()
   Path("/opt/nightbank/cfg/night.properties").write_text(
       "".join(f"{key}={value}\n" for key, value in props.items())
   )
   (night / "profile.env").write_text(
       f'export NIGHTBANK_DATA="{night}/data"\n'
       f'export NIGHTBANK_STATE="{night}/data/state"\n'
       f'export NIGHTBANK_INCIDENTS="{night}/incidents.conf"\n'
       'export NIGHTBANK_SOCKET="/srv/dsl41/runs/nightbank/control.sock"\n'
       f'export PATH="/opt/nightbank/bin:{Path(sys.executable).parent}:$PATH"\n'
   )
   PY
   ```

5. Give the service user its night's files:

   ```sh
   chown -R dsl41:dsl41 /srv/dsl41/nightbank/night
   chmod 0600 /srv/dsl41/nightbank/night/profile.env
   chmod 0644 /opt/nightbank/cfg/night.properties
   ```

   Inspect the copied `incidents.conf`; it intentionally injects training
   failures. The profile puts `fakework` and the installed engine on PATH
   and supplies the socket used by the estate's `SET_GLOBAL` commands.
   The [Nightbank preparation](../examples/nightbank/README.md#quick-start)
   gives a checkout-only alternative.

6. Check the installed inputs as the service user before starting the unit:

   ```sh
   sudo -u dsl41 /opt/dsl41/releases/<version>/bin/dsl41 lint --config /etc/dsl41/dsl41.toml
   ```

   Inspect findings even after exit 0; the shipped lint policy is `none`.

7. Copy the [engine unit](../examples/nightbank/deploy/dsl41-engine.service)
   to `/etc/systemd/system/dsl41-engine.service`.
   Substitute `<version>` in both executable paths. Set `RequiresMountsFor`
   to the configured run root and every required input filesystem.
   Set `ExecStop`'s socket to that root's `control.sock`.
   Include the profile, worker and data filesystems in the mount dependencies.
   Keep the unit's time-sync ordering so a boot-time clock correction happens
   before engine timestamps. Verify the installed unit as root:

   ```sh
   systemd-analyze verify /etc/systemd/system/dsl41-engine.service
   ```
8. As root, reload systemd's unit definitions:

   ```sh
   systemctl daemon-reload
   ```

9. After the checks and unit edits, set the training start times last.
   As root, refresh only the properties using the same checkout:

   ```sh
   /opt/dsl41/releases/<version>/bin/python - <<'PY'
   import runpy
   from datetime import UTC, datetime, timedelta
   from pathlib import Path

   compute = runpy.run_path("tests/nightbank_props.py")["compute_properties"]
   props = compute(Path("/srv/dsl41/nightbank/night"), datetime.now(UTC) + timedelta(minutes=15), 3)
   props["OWNER"] = "dsl41"
   Path("/opt/nightbank/cfg/night.properties").write_text(
       "".join(f"{key}={value}\n" for key, value in props.items())
   )
   PY
   ```

10. As root, enable the unit and start it within ten minutes of step 9:

    ```sh
    systemctl enable --now dsl41-engine.service
    ```

    The [systemctl manual](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html)
    defines these unit-management commands.

If the first region's start time passes before the first start, remove the
training night directory and repeat steps 4–6 and 9 before starting.
Removing `/srv/dsl41/nightbank/night` discards its synthetic data and counters;
keep any evidence you need first. A root with `engine.db` keeps its stored
revision. For that root, use [reread and update](#reload-a-changed-estate)
while the engine runs to adopt refreshed properties, or select a fresh run
root in both the main config and the unit's `ExecStop` socket before starting.
A restart alone does not adopt the refreshed times.

Read the unit in these groups:

| Lines | What they do for the operator |
| --- | --- |
| `[Unit]`, `Description`, `RequiresMountsFor` | Name the service and require the run root's filesystem before starting. An absent mount must not look like a fresh root. |
| `Wants`, `After` | Order startup after `time-sync.target`; the host must provide clock synchronization. |
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

## What the main config selects

The main config selects these JIL files directly, not `estate.toml`.
Its properties path supplies schedule anchors, owner, data, log and
profile paths. It has no environment tables, so no environment selection
is needed. If a site's main config declares `[estate.environments.NAME]`,
set `engine.environment = "NAME"`; environment properties follow base
properties. Portable manifest fixtures select their environments with
`--environment NAME` instead.

Set `engine.run_root`, input patterns and properties in the main config.
Input paths may be absolute or relative to that file; the run root must
be absolute. Patterns support `*`, `?` and brackets; `**` is refused.
The config is a regular file, never a symlink. For exception jobs, set
`estate.non_idempotent` to the exceptions file's path; its default is
`non-idempotent.toml` beside the main config, not beside the JIL.
Python inputs may supply `estate()` returning `CatalogBuilder`, as with
`dsl41 build`; see [authoring](authoring.md) for that input form.
Keep `estate.toml` for explicit portable fixtures and legacy `--manifest`
invocations. These input rules are [DL-437](decision-log.md).

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
reports here. The [incident guide](incident-triage.md) covers collecting fault evidence.
The unit leaves output routing to systemd's defaults:
stdout goes to the journal and stderr follows it, unless the host overrides
those defaults. See [systemd output routing](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html#StandardOutput=)
and [journalctl's unit filter](https://www.freedesktop.org/software/systemd/man/latest/journalctl.html#-u).
The foreground run prints the same shutdown record to its terminal.
Output and refusal handling: [run CLI](../src/dsl41/cli_run.py) and
[shared CLI](../src/dsl41/cli_common.py).

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
   sudo -u dsl41 /opt/dsl41/releases/<version>/bin/dsl41 reread
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
   sudo -u dsl41 /opt/dsl41/releases/<version>/bin/dsl41 update TOKEN --wait
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

## Exercise the unit before site use

Run these only on a disposable drill host with Podman, Git and uv available,
from the checkout. The scripts create a systemd container and install a
synthetic estate; they do not qualify a production workload.

1. Run the Ubuntu service drill:

   ```sh
   examples/nightbank/deploy/drill-local.sh --image ubuntu all
   ```

2. Run the EL8 service drill:

   ```sh
   examples/nightbank/deploy/drill-local.sh --image el8 all
   ```

The scripts print a result for each row and stop on failure. They cover
install, start, restart, container reboot, engine kill, drain, reload,
process leaks, startup refusal, start limit and executable upgrade.
The `refusal` row verifies that a pending source edit leaves the stored
revision active after restart, then tests a foreign store version refusal.
The `leak` row checks that a restart TERMs a job's leftover process through
the cookie scan. A container reboot retains the kernel boot id.

[DRILL.md](../examples/nightbank/deploy/DRILL.md) supplies maintainer setup
and diagnostics; [drill-steps.sh](../examples/nightbank/deploy/drill-steps.sh)
defines the executable row checks. Use the row behavior above when reading
its refusal, manifest and upgrade descriptions. The scripts install a
main config and versioned executable, independently of application inputs.
