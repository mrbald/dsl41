# Run one engine on one Linux box

For the operator installing, starting, watching and recovering one engine on
one Linux box. The rest of the [operator track](../README.md#documents)
covers incidents and worked estates. This page leaves application authoring
to the [job author guide](authoring.md) and site qualification to the
[qualification items](../TODO.md#qualification).
A Packager uses the install steps as the reference implementation (DL-439).

## Install the package and unit

For the Packager: the supported production interpreter is Python 3.12.x.

On Debian 13, Ubuntu 24.04 and later, install `tzdata-legacy` if the estate uses legacy zone
names such as `US/Eastern` (DL-477). Older releases include these names in `tzdata` itself.

On a VM or host, install its clock synchronization service and enable its waiter
before starting the engine: `chrony-wait.service` with chrony (as on RHEL 8), or
`systemd-time-wait-sync.service` with systemd-timesyncd.
The engine unit orders after `time-sync.target`, but that target waits for
real synchronization only when an enabled waiter runs successfully.
A container cannot synchronize its own clock. Its host or hypervisor must keep
the clock synchronized. Before starting the engine in a container, run the
host checks in step 7 on the host that supplies its clock.
The [chrony waiter](https://github.com/mlichvar/chrony/blob/4.2/examples/chrony-wait.service)
sets `TimeoutStartSec=180`; if it times out, the engine can still start
without synchronization.
The upstream [timesyncd waiter](https://github.com/systemd/systemd/blob/main/units/systemd-time-wait-sync.service.in)
sets `TimeoutStartSec=infinity`. With that default, engine startup waits
until synchronization and can be delayed indefinitely if synchronization fails.
Check the host's installed unit and overrides for its effective timeout.
Without it, the engine can start with an unsynchronized clock.
A clock set ahead stamps the store ahead; after the clock is corrected,
the engine holds ticks until the wall clock catches up.
On a backward gap, the engine reports `dsl41: WARNING:` with the wall reading,
admission frontier and gap in seconds in the [engine's output](#read-the-engines-output).

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
   Copy the [failure unit](../examples/nightbank/deploy/dsl41-engine-down.service)
   to `/etc/systemd/system/dsl41-engine-down.service`.
   Install the site's executable hook at `/etc/dsl41/status-hook`, executable
   by `dsl41`. Set that same absolute path in TOML and the failure unit's
   literal arguments. Set its run root and engine-unit arguments to match.
   Substitute `<version>` in all three executable paths. Set `RequiresMountsFor`
   to the configured run root and every required input filesystem.
   Set `ExecStop`'s socket to that root's `control.sock`.
   Include the profile, worker and data filesystems in the mount dependencies.
   Keep the unit's time-sync ordering. On a VM or host, run the following
   checks there as root. For an engine in a container, run them on its host
   before starting the engine. A waiter's `ConditionVirtualization=!container`
   or `ConditionCapability=CAP_SYS_TIME` can skip it inside a container or
   under a manager without that capability, even when `is-enabled` says
   `enabled`. With chrony on RHEL 8:

   ```sh
   systemctl enable --now chronyd.service
   systemctl enable --now chrony-wait.service
   systemctl is-enabled chrony-wait.service
   systemctl status chrony-wait.service
   systemctl show -p ActiveState,Result,ConditionResult chrony-wait.service
   chronyc tracking
   ```

   With systemd-timesyncd instead:

   ```sh
   systemctl enable --now systemd-timesyncd.service
   systemctl enable --now systemd-time-wait-sync.service
   systemctl is-enabled systemd-time-wait-sync.service
   systemctl status systemd-time-wait-sync.service
   systemctl show -p ActiveState,Result,ConditionResult systemd-time-wait-sync.service
   timedatectl show -p NTPSynchronized
   ```

   `is-enabled` must print `enabled`. Require `ActiveState=active`,
   `Result=success` and `ConditionResult=yes` from `show`: the waiter must be
   active after exiting successfully, not skipped. Check `status` for a
   timeout or failure. With chrony, require `Leap status` to be `Normal` in
   `tracking`; with timesyncd, require `NTPSynchronized=yes`.
   Enabling the waiter alone does not prove synchronization succeeded.
   Verify the installed engine unit as root:

   ```sh
   systemd-analyze verify /etc/systemd/system/dsl41-engine.service /etc/systemd/system/dsl41-engine-down.service
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
| `Wants`, `After` | Order startup after `time-sync.target`; the host must enable the synchronization waiter described above. |
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
For job environment values, add a literal string map to that config:

```toml
[engine.job_env]
AUTOSERV = "synthetic"
LOG_DIR = "/var/log/dsl41"
```

Apply edits with `reread` and `update`; a restart uses the applied revision.
`engine.fw_default_interval_s` sets the default poll interval for FW jobs that omit
`watch_interval`. Use a positive whole number of seconds; omission means 60 (DL-469).
The setting is revisioned and excluded from job-definition hashes. Its update stops no
watch: an in-flight watch keeps its interval until it ends or the engine restarts.
A resumed or new watch uses the applied default. Explicit job intervals take precedence.
A restart reads `engine.kill_signals` from the current configuration on disk.
An edit only to `job_env` advances the revision without stopping jobs,
including while boxes run (DL-447). Later dispatches and replacements use
the new map; attempts already running keep their recorded environment.
`reread` and `update` require ADM and prepare inputs under the engine identity (DL-463).
`AUTOSERV`, `AUTOSYS`, `AUTOUSER` and `AUTOROOT` are unset unless this map
supplies them. Other names inherit the engine's startup environment, except
generated invocation values and `DSL41_SOCKET` (DL-447, DL-459).
Map values are literal strings. Names use `[A-Za-z_][A-Za-z0-9_]*`.
NULs and overrides of `DSL41_*`, `AUTO_JOB_NAME` or `AUTORUN` refuse.
Set path variables here; the author follows [Set stream paths](authoring.md#set-stream-paths).

These accepted engine settings request behavior this release does not apply
and produce one `known-unsupported` warning per setting (DL-466):

| Setting | Warns when |
| --- | --- |
| `engine.job_shell` | Configured. |
| `engine.job_profile` | Configured. |
| `engine.std_file_mode` | Set to `"truncate"`. |

Omitting these settings, or using the default `std_file_mode = "append"`,
creates no warning. JIL stream prefixes remain effective independently of
`std_file_mode`. See the author's [unsupported JIL inventory](authoring.md#2-check-which-constructs-dsl41-accepts)
for record attributes. Both use the configured `lint.ignore` and `lint.refuse`
policy; ignoring a warning does not activate the setting.

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
First start requires a root without surviving runtime evidence (DL-443, DL-445).
Settings and inputs may be provisioned first. A missing store beside attempt
spools, update material, root-local logs or receipts, legacy state, a nonempty
lock, SQLite sidecars or a control socket
refuses with exit 2 before changing the root or evaluating estate Python.
Unreadable evidence also refuses. See the [genesis and restart contract](runner-design.md#7-journal-and-recovery-e1-prod-grade).
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

For the operator, read the engine's journal with:

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

On restart, startup stderr reports scheduled starts missed while the engine
was down (DL-476):

```text
engine was down 2026-07-01T09:00:00 to 2026-07-01T11:05:00; 4 scheduled starts were missed and will not run late
  poll_30m: 4 missed, last 2026-07-01T11:00:00
```

One total precedes one line per affected job, sorted by job name.
A first start or a restart with no missed starts prints no such report.
Times use the engine host's local clock display (DL-473).
The report appears only in engine start output, including `dsl41 run --ui`
before the terminal UI attaches. Under systemd, read it with `journalctl`
as above. [Recover after an outage](#recover-after-the-engine-or-box-dies)
before choosing any manual catch-up.

Job output goes to each job's `std_out_file` and `std_err_file` when set.
Each unset path falls back to `<run_root>/logs/<job>.<run>.{out,err}`.
For stream placeholders, output prefixes and their limits, see [Set stream paths](authoring.md#set-stream-paths).
The engine does not archive or delete job output, or monitor disk space.
The site's archiver may move only finished job output.
It must leave everything else in the run root untouched, including `runs/`,
`plans/`, the store (`engine.db` and its sidecars), `leader.lock`,
`control.sock` and `engine.toml`.
The host's monitoring must watch disk space.

## Watch the engine and jobs

The site's optional status hook receives JSON on stdin for job failure, termination,
hold, SEM-34 deadline alarms and configured long-running intervals (DL-469). Startup refusal
attempts delivery after valid hook settings load. The failure unit reports unit
failure even when TOML or the store cannot be read. A normal systemd stop does
not activate it. A crash may restart without a failure-unit activation;
the service drills check refusal and restart exhaustion on both supported systems.

Omitting `[engine] status_hook` disables engine notifications. If the site
disables notifications altogether, also remove the engine unit's `OnFailure`
setting. `status_hook_long_running_s` sets the global runtime-length threshold in seconds,
default 0. The hook path and global threshold are host settings; restart after editing
them. Positive job `max_run_alarm` overrides the global threshold in minutes × 60; zero or absence
keeps the global threshold. An effective zero disables that job's length notifications.
Failure and termination events carry resolved `alarm_if_fail` and `alarm_if_terminated`
Booleans. Both default to true; the termination default is unverified against AutoSys.
False retains the event; the site hook decides whether to alert. Rehearsal never invokes
the hook.

Delivery is best effort: one worker, 64 queued events, 16 KiB per payload,
no retry or spool. New events are dropped on overflow or oversize. Shutdown
drops the backlog. Process creation runs off the engine's event loop. Each
invocation has five seconds. After every invocation, including exit 0, the worker
TERMs its group, allows 0.5 seconds, then KILLs it and uses the remaining
0.5 seconds to verify cleanup and reap. The slot stays occupied until cleanup
finishes; unconfirmed cleanup disables delivery and always logs that transition.
Other errors are counted in rate-limited journal diagnostics. Hooks must not
detach: group cleanup cannot contain `setsid` escapees.
Site hooks must not write inside the run root; that root belongs to the engine.
The hook runs as `dsl41` from `/` with
umask 077, closed inherited descriptors, only `PATH=/usr/bin:/bin` and
`LANG=C`, and discarded stdout/stderr. It is trusted code with the service
account's permissions. See the [payload and event contract](runner-design.md#4-engine-loop--single-writer)
before writing a consumer; consumers must ignore unknown fields and unknown events (DL-446, DL-469).
The hook protocol is `v=1`. Every key appears in every payload, null where it does not
apply, including both alarm flags.

```sh
/opt/dsl41/releases/<version>/bin/dsl41 query status --json \
  --socket /srv/dsl41/runs/nightbank/control.sock
```

The command prints complete UTC JSON. Omit `--json` for text with full local
timestamps (`YYYY-MM-DDTHH:MM:SS`). Local means the host running the CLI or
TUI; a served TUI uses the server host, not the browser timezone. A container
with no zone database shows UTC. Install zone data and configure the host
timezone before relying on local display (DL-473).

`engine` reports the engine state, including
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
stop, reread and update require its `adm` tier.
For AutoSys task equivalents, use the [operator command table](incident-triage.md#read-the-jobs-state).

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

## Take a backup for restore

Never copy `engine.db` and its sidecars from a running engine.
Draining stops running commands with TERM, waits the configured grace, then
sends KILL where needed. It cancels file watchers and records attempt outcomes.
It does not wait for jobs to finish their work naturally.
Choose a quiet window for a drained backup, or use an atomic snapshot.
See [drain the engine](#drain-the-engine) for deadlines and shutdown records.

For the operator, use one of these backup methods:

- Drain the engine, then copy the whole run root while it remains stopped.
- Take an atomic filesystem snapshot of the whole run root, even while the
  engine runs. A restore from a live snapshot recovers as after a crash.

Keep a copy of the main config (`/etc/dsl41/dsl41.toml` in this example)
with the backup and record the installed release version.
Retain that release so restore can use the same interpreter and package.
A different release may refuse the captured estate bytes.

For a drained backup of the systemd service:

1. As root, stop the unit:

   ```sh
   systemctl stop dsl41-engine.service
   ```

   The unit drains through `dsl41 engine stop --wait`.
   For a foreground engine, use the [direct drain command](#drain-the-engine)
   instead. Keep the engine stopped until the copy finishes.

2. As root, copy the whole run root to a fresh backup destination.
   Choose a destination that does not exist:

   ```sh
   install -d -m 0700 /srv/backups
   cp -a /srv/dsl41/runs/nightbank /srv/backups/nightbank-backup
   cp -a /etc/dsl41/dsl41.toml /srv/backups/nightbank-backup.toml
   ```

   Keep `engine.db`, its sidecars and all other root contents together.
   Copying only the database omits attempt records and captured plans needed
   for recovery.

3. As root, restart the service:

   ```sh
   systemctl start dsl41-engine.service
   ```

For an atomic snapshot, use the host filesystem's snapshot tools.
The snapshot must capture the whole run root at one instant.
Job output outside that root needs the site's separate backup policy.
Restore the whole root together, with its main config and recorded release.
If `engine.db` is missing, the engine starts fresh even when other runtime
evidence survives. That loses recovery continuity; backing up and restoring
the whole run root avoids this loss.

## Reload a changed estate

Use the engine's socket. Every engine command takes `-S`/`--socket`, with
`DSL41_SOCKET` as its default (DL-459, DL-463).

```sh
dsl41 reread -S /path/to/control.sock
dsl41 update -S /path/to/control.sock --wait
```

Reread only reports current launch-config and glob inputs: added, changed,
removed and unchanged jobs, findings, and what update would stop, rerun, hold
or remove. A running box does not refuse a preview. Broken input explains why
no diff is available. Exit 0 means no running stop, 1 means running stops,
and 2 means no diff or failure to reach the engine.

Status in the CLI and TUI annotates changed/removed jobs and unchanged members
of boxes update would restart with this forecast.
It describes reread time. Another reread replaces it; failed reread, applied
update and engine restart clear it. It is held only in engine memory.

Update rereads the files itself. Edits made after preview are included.
It prints a request ID before sending. Use `--request-id ID` only to look up
that original work after an unknown outcome. An unknown or expired ID submits
nothing. Do not blindly submit a fresh update after a lost reply.
With `--wait`, exit 0 means applied and 1 means still pending. Exit 2 means
refusal/failure. No token or stop-consent argument is used.

Affected ordinary jobs stop through TERM, grace and KILL. A retained idempotent
job gets another attempt of the same run under the new definition; a marked
job is held; a removed job gets no replacement (DL-424, DL-432).
A structural edit of a running box stops its running members, ends its old
rows with KILLJOB, applies, and force-starts the retained outermost box.
The KILLJOBs commit before application so members get no old-run replacement.
Before FORCE_STARTJOB, every marked member whose row is not INACTIVE is put
ON_HOLD, including members that had already finished in that box run (D11).
Unmarked members run through normal box-start semantics. Removed boxes stay
stopped. The new box start opens a new run, and normal successors and
terminators can observe the old run's end (DL-463).

A crash during box restart can leave the box stopped. Before operator
force-start, put its marked non-INACTIVE members on hold, including members
that finished earlier. Then force-start the retained unmarked box. There is
no separate automatic completion protocol for this box restart.

Preparation runs under the engine account and startup cwd/environment, in a
separate interpreter. Its deadline is 120 seconds; cancellation terminates,
allows two seconds, then kills and reaps. The client allows preparation and
update application time. Findings retain their actual source diagnostics.
Artifacts stay under the engine's actual run root.
Host-setting edits require restart before update. Restart restores the applied
capture, but future reload uses the config of that latest engine start.
The drift flag and startup pending count still compare against the applied
capture's inputs, which may differ from that launch file.
JIL-only engines refuse reload: drain and start a fresh run root to change
those definitions; the old root's state is not migrated.

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
3. Select that release in the engine's `ExecStart` and `ExecStop` and the
   failure unit's helper `ExecStart`.
4. Run `systemctl daemon-reload`.
5. Run `systemctl start dsl41-engine.service` and check status.

Application inputs and reviewed updates are independent of that selection.

For one package that replaces the engine at the unit's fixed executable path,
use the following variant (DL-474). It also covers a package that carries estate
files. The unit's `ExecStart` must use `run --config`.
Replace `ENGINE` in every command with the absolute executable named by
the installed unit's `ExecStart`. Replace `PACKAGE.rpm` with the new package.
The repo's reference RPM prepares a runtime without selecting it (DL-389).
With that package, select the new runtime after installation and before starting;
on rollback, select the old runtime again. Installing or downgrading that RPM alone
does not select the runtime.

Choose a quiet window: restart serves the stored estate until an update applies.
Keep the release that wrote the root and a whole-root backup available.
Do not replace engine files while the service runs.

1. As root, drain and stop the service:

   ```sh
   systemctl stop dsl41-engine.service
   ```

   Check the [shutdown record](#drain-the-engine) and confirm no engine, wrapper
   or job writer remains before copying the root.

2. As root, back up the stopped root and main config. Choose unused destinations:

   ```sh
   install -d -m 0700 /srv/backups
   cp -a /srv/dsl41/runs/nightbank /srv/backups/nightbank-before-upgrade
   cp -a /etc/dsl41/dsl41.toml /srv/backups/nightbank-before-upgrade.toml
   ```

3. As root, install the package. Select its runtime if the package requires it:

   ```sh
   rpm -Uvh PACKAGE.rpm
   ```

   For the reference RPM's default prefix, the unit's executable must follow
   `/opt/dsl41/.venv/bin/dsl41`. As root, replace `<version>-<release>` with
   the installed delivery directory and use `ui` instead of `base` if the RPM
   was built with that profile. Use the package's system Python 3.12; these
   commands show its default path. Stop on any failure. The `.venv.next` path
   must be unused.

   ```sh
   delivery=/opt/dsl41/delivery/<version>-<release>
   profile=base
   candidate="$(/usr/bin/python3.12 -I "$delivery/tools/bundle.py" candidate "$delivery" /opt/dsl41 "$profile")"
   /usr/bin/python3.12 -I "$delivery/tools/bundle.py" verify "$delivery" "$candidate" "$profile"
   ln -s ".venvs/$(basename "$candidate")" /opt/dsl41/.venv.next
   mv -Tf /opt/dsl41/.venv.next /opt/dsl41/.venv
   ```

   The atomic replacement is manual: the repo has no activation command or
   regression test for replacing an existing active link.
   On rollback, reinstall the old RPM first: an upgrade removes its delivery
   directory. Then repeat these commands with the old delivery and its profile.

4. As root, start the service:

   ```sh
   systemctl start dsl41-engine.service
   ```

5. As the service user, repeat this query until it prints `RUNNING`.
   It needs `jq`. A successful query can still report `STARTING` during catch-up.
   If startup refuses, read the journal and follow the refusal guidance below.

   ```sh
   sudo -u dsl41 ENGINE query status --json \
     --socket /srv/dsl41/runs/nightbank/control.sock | jq -r .engine
   ```

6. Only when the package changed estate files, preview them as the service user:

   ```sh
   sudo -u dsl41 ENGINE reread \
     --socket /srv/dsl41/runs/nightbank/control.sock
   ```

   Restart keeps the applied revision. The TUI's `spec drift: estate files changed
   on disk` line directs reread and update. Review the preview's findings and
   affected jobs before applying it. Then run:

   ```sh
   sudo -u dsl41 ENGINE update --wait \
     --socket /srv/dsl41/runs/nightbank/control.sock
   ```

   Exit 0 prints `applied: catalog revision N`; status reports that applied
   `config_rev`, and the drift indication clears when disk inputs match it.
   Completed statuses and existing holds are kept for retained jobs whose runs
   the update does not restart. Affected running jobs follow the
   [update interruption and box rules](#reload-a-changed-estate).

A state-version refusal leaves the root unchanged. Keep it and run the release
that wrote it. For a fixed-path replacement package, stop the unit, reinstall that
release with `rpm -Uvh --oldpackage COMPATIBLE_PACKAGE.rpm`, select its runtime
if required, then start and repeat the `RUNNING` check. A drain and snapshot do
not convert state (DL-424).

A unit that launches JIL files without a main config refuses a changed estate
at start with a catalog-hash message. For any refusal that names a fresh run
root, use the fresh-root steps below if choosing that reset.

A fresh root is a deliberate reset. It loses the active engine's:

- Job statuses.
- Hold, ice and noexec flags.
- Run and attempt identities and numbers.
- Exit codes.
- Timestamps.
- History.
- Captured invocations and log associations.
- Configuration revisions.
- Interruption evidence.
- Pending updates and request IDs.
- Globals.
- Scheduler frontier and deduplication.
- Armed latches.
- Timers.
- Capacity reservations.

The archived root keeps that evidence. External job effects remain.
Do not delete only `engine.db` or edit its version metadata.

To choose a fresh root, use an unused archive path. As root:

1. Keep the service stopped:

   ```sh
   systemctl stop dsl41-engine.service
   ```

2. Archive the whole root without nesting it inside an existing directory:

   ```sh
   mv -T /srv/dsl41/runs/nightbank /srv/dsl41/runs/nightbank-old
   ```

3. Create the replacement root and preserve its stop graces, snapshot interval
   and retention settings:

   ```sh
   install -d -o dsl41 -g dsl41 -m 0700 /srv/dsl41/runs/nightbank
   cp -a /srv/dsl41/runs/nightbank-old/engine.toml /srv/dsl41/runs/nightbank/
   ```

4. Clear any start limit and start the service:

   ```sh
   systemctl reset-failed dsl41-engine.service
   systemctl start dsl41-engine.service
   ```

   Repeat the `RUNNING` check above. The engine starts from the current configured
   estate with fresh state.

A `CHANGE_STATUS` can start dependent jobs, including through a member's change
to its box's derived status ([SEM-15](autosys-semantics.md#sem-15--member-status-changes-can-ripple-upward-vc)).
Never use a blanket hold-and-release loop as a neutral status restore:
releasing holds can start eligible jobs.
Per-job `sendevent CHANGE_STATUS --job JOB --status STATUS --exit-code CODE`
and `sendevent ON_HOLD --job JOB` can set a chosen status and hold flag.
Run them as `sudo -u dsl41 ENGINE` with
`--socket /srv/dsl41/runs/nightbank/control.sock`.
These are new events; they do not recover history or scheduler state.

## Recover after the engine or box dies

Keep the durable run root, including `engine.db`, `runs/` and `plans/`.
For backup, stop the unit and independently prove no engine, wrapper or job
writer remains. Copy the entire stopped root, including any SQLite sidecars,
spools and settings, together with the main config and required inputs.
Restore the complete copy at the same path before starting. A missing store
with surviving runtime evidence refuses; preserve that damaged root and
recover the missing backup material (DL-443). Scheduler restoration does not
restore a business database or undo job effects. Back those up separately.
Startup validation uses Python's temporary directory selection (`TMPDIR`
when usable). Allow space for one complete
copy of `engine.db` and its present `-wal`, `-shm` and `-journal` files,
plus filesystem overhead. Validation can run more than once; copies are
removed after each pass. An uncatchable kill can leave a private
`dsl41-inspect-*` directory there. A copy or space failure refuses with
exit 2 and names the source evidence; it leaves that evidence unchanged.
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

Read the missed-start report in [engine start output](#read-the-engines-output)
before choosing catch-up (DL-476). Its `from` is the largest durable record
timestamp, or the snapshot's saved `last_at` when no later records exist.
It is clamped to the earliest reported tick if that tick is earlier.
After an unclean stop, the time of death is unknown; an idle engine's last
record can precede its death by hours. `to` is the captured scheduler sweep
cutoff, at least the durable scheduler frontier after a backward clock step.
Missed ticks are recorded and never fired late. For a required manual start,
check downstream effects and follow the
[missed-tick intervention](incident-triage.md#the-job-has-not-started).

A refused resume leaves the unit down on exit 2. Read its named refusal;
do not erase the root to bypass it. The restart checks the lock, store
version and identity, clock domain and any pending update's captured plan.
A missing or unreadable interrupted-update plan refuses before a journal
write or process signal.

Main-config restart restores the last applied catalog, profile and job order
from the store. Estate and lint edits remain pending, including invalid
`engine.environment` selections. Restart still reads the selected config
as TOML and requires its supported format. It applies current `as_machine`,
`machine_policy`, access-map and status-hook settings; invalid host settings refuse with
exit 2. An omitted access map selects owner-only access. These settings stay
effective while an interrupted update applies its captured plan.
Other runtime-profile fields retain their stored values and checks (DL-437).

Restart runs launch preflight against the stored catalog and current host
settings without applying lint refusal. It does not lower the applied v3
capture or import Python. Captured JIL is parsed only for `spec` views; if the
scanner rejects it, those views return `jil: null` and the engine still runs.
After startup a synchronous source scan reports
`N pending source changes; run dsl41 reread -S SOCKET` once, including a zero count.
That count measures source drift, not compiler behavior. After a compiler
fix, restart keeps the applied IR. The next `reread` lists jobs whose lowered
definitions change, even if their sources did not change. `update` applies those
definitions, stopping and rerunning affected running jobs; non-idempotent jobs
follow the existing hold rule (DL-468). An interrupted update still uses its
captured plan; if the new binary rejects that candidate or changes its recorded
job diff, restart refuses before writes or signals. The drift refusal says:
"Finish the update using the binary that began it, then upgrade."
Patch compatibility requires the same state and format versions. Preserving
earlier behavior for absent settings is a release obligation. Recovery cannot
verify it from the retained record.
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
process leaks, destructive restore, startup refusal, start limit, malformed TOML and executable upgrade.
The refusal row requires `engine_refused` and `engine_down`; crash-loop
exhaustion requires `engine_down`; normal stop requires no `engine_down`.
Malformed TOML requires the independent helper's `engine_down` and no
`engine_refused`. The rows check delivered payloads, not only journal text.
The `refusal` row verifies that a pending source edit leaves the stored
revision active after restart, then tests a foreign store version refusal.
The `leak` row checks that a restart TERMs a job's leftover process through
the cookie scan. A container reboot retains the underlying kernel boot ID;
the runtime may replace the container-visible ID. The drill records both
boundaries separately and does not qualify an actual host reboot.

[DRILL.md](../examples/nightbank/deploy/DRILL.md) supplies maintainer setup
and diagnostics; [drill-steps.sh](../examples/nightbank/deploy/drill-steps.sh)
defines the executable row checks. Use the row behavior above when reading
its refusal, manifest and upgrade descriptions. The scripts install a
main config and versioned executable, independently of application inputs.
