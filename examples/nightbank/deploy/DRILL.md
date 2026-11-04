# Run the service drill

For the maintainer who changes the engine's start, stop or restart, the unit
[`dsl41-engine.service`](dsl41-engine.service), or the drill itself, and needs
proof that the unit still behaves as [Run one engine on one Linux box](../../../docs/deploy.md)
tells the operator. The drill installs dsl41 under that unit in a container with
systemd as PID 1 and runs one row per operator procedure. The rest of the
maintainer track is in the [README's documents](../../../README.md#documents).

## Run it

1. Check that the host has `podman`, `git` and `uv`.
2. From the checkout, run every row on Ubuntu 24.04:

   ```sh
   examples/nightbank/deploy/drill-local.sh --image ubuntu all
   ```

3. Run every row on Rocky Linux 8:

   ```sh
   examples/nightbank/deploy/drill-local.sh --image el8 all
   ```

Each run creates a fresh container and builds both current and `.post1` wheels.
Podman reuses the host and dependency wheelhouse layers.
Container Python 3.12 builds the wheelhouse from frozen exported runtime requirements.
Install and upgrade each create a fresh release venv and run pip against that wheelhouse.
No tested venv is cached. Warm reuse is not an OS update.
`DRILL_EVIDENCE` selects the retained evidence directory; its default is a fresh
`/tmp` directory holding build seconds, exit status, the build log, container logs,
inspection and service diagnostics exported before container cleanup.
The script prints `ok:` per check, a summary with each row's seconds, and exits 0
only when every row passed.

| Option | Effect |
| --- | --- |
| `ROW...` instead of `all` | Run these rows, after `install` and `start`, in the drill's order. |
| `DRILL_KEEP=1` | Keep the container. `scripts/wargame.sh --base BASE join NAME` opens a shell in it; the script prints the command. |
| `PODMAN='sudo podman'` | Use a rootful engine. The workflow sets it. |

[Integration testing §3](../../../docs/integration-testing.md#3-choose-the-lane-that-exercises-the-boundary)
owns the service CI matrix, triggers, timeouts and evidence uploads.
Its [failure procedure](../../../docs/integration-testing.md#4-run-a-selected-scenario-and-keep-its-evidence)
owns nightly triage and merge blocking.

## What each row checks

The rows check the installed service through `systemctl`, `journalctl -u`,
`dsl41 query status` and `dsl41 runs`. The `restart-faults` row also reads
synthetic stores and wrapper spools. A wait polls with a deadline.

| Row | What it does | What it checks |
| --- | --- | --- |
| `install` | Installs this tree's wheel into `/opt/dsl41/releases/<version>`, the small Nightbank estate, the drill's jobs, the `dsl41` user and the unit | The estate lints clean; the unit is enabled |
| `start` | `systemctl start` | `engine up` in the journal, `engine.db` exists, the scheduled jobs are held |
| `clock` | Runs the documented waiter enable and verify commands, queues an engine start behind an unsynchronized waiter, then supplies synchronization or lets a drill timeout expire | The vendor waiter orders before `time-sync.target` and the engine; synchronization releases startup; timeout releases it with waiter `Result=timeout`; the running engine's exact interpreter version is recorded and must be 3.12.x |
| `restart` | `systemctl restart` | A new invocation, `engine stopped: clean_shutdown`, the holds are still there |
| `reboot` | Restarts the container | The enabled unit starts at boot and resumes the same root |
| `restore` | Drains an active writer, copies the complete root and required sidecars, inputs and settings, deletes originals and restores them | No service-account/cgroup writers; unchanged-tree refusal for a missing store; fresh history, holds, ice and globals; same-run continuation with exact business effects and distinct attempts |
| `kill-engine` | SIGKILL of the engine with a job running | The unit restarts it; attempt 1 ended (`lifeline` or `engine_death`) and attempt 2 of the same run runs |
| `drain` | `systemctl stop` with a job that ignores TERM | The stop waits the TERM grace, `ExecStop` exits 0, `clean_shutdown`, no `engine_down`; after the start the rerun follows `engine_stop` |
| `reload` | `dsl41 reread -S SOCK`, then `dsl41 update -S SOCK --wait` with a changed running job | Preview exit 1, config_rev + 1, same-run rerun after update_stop, new definition |
| `leak` | A job leaves a child in its own session | Both images require the engine's cookie-scan TERM at the job's exit and at restart; the startup log names the image's expected primitive (DL-431, DL-433, DL-436) |
| `restart-faults` | Runs the [listed synthetic fault cases](restart_faults.py) under the installed interpreter in transient systemd units | Independent process witnesses, effect counts and record order pin interrupted attempts, quarantine, deadlines, holds and FW progress |
| `refusal` | Restarts with pending source edits, then with an unsupported store version | Source edits stay pending at the same revision; the store mismatch exits 2 and stays failed; one `engine_refused` and one `engine_down` are delivered; restoring the version recovers |
| `start-limit` | SIGKILL of the engine until the unit gives up | Failed at the sixth kill, `repeated too quickly`, `engine_down` delivered; `reset-failed` and a start recover |
| `malformed-toml` | Starts with unreadable TOML syntax | Exit 2 stays failed; the independent helper delivers `engine_down` with literal root and unit, without `engine_refused`; restoring TOML recovers |
| `upgrade` | Installs the same tree as version `X.post1` into a separate release venv, selects it in the unit and restarts | The engine runs from the new venv; the root, its revision and the holds are kept |

The `restart-faults` cases print hash and environment receipts. The evidence
directory retains their stores, spools and receipts together under `restart-faults/`.

The `clock/` evidence holds the vendor unit, drill drop-ins, status, journal,
monotonic ordering timestamps and `python.txt` from the running engine's executable.
The row checks the installed waiter's timeout before applying its test override.
See the [deployment prerequisite](../../../docs/deploy.md#install-the-package-and-unit)
for the operator commands and timeout consequences.

After a failed row the script prints the diagnostics: `systemctl status`, the
unit's journal, `dsl41 runs` and `dsl41 query status`. It stops at the first
failure.

## Where the drill differs from a site

- The `clock` row runs the vendor `chrony-wait.service` on EL8 and
  `systemd-time-wait-sync.service` on Ubuntu. On EL8, a container-only drop-in
  clears `chronyd.service`'s capability condition. On Ubuntu, a drop-in clears
  the waiter's capability and virtualization conditions. EL8 runs a chronyd
  client and a separate loopback chronyd source with `-x`. It starts the source
  to supply synchronization; it stops the source and resets the client to
  remove synchronization. Ubuntu preloads `clock-unsynced.c` into
  the waiter to report an unsynchronized kernel, then creates timesyncd's
  synchronization marker. These controlled inputs leave the shared VM clock
  unchanged; they do not qualify an external time source. Both waiters and
  the engine start in one transaction, as their enabled boot targets would
  arrange. The timeout case uses a separate `/run` drop-in with
  `TimeoutStartSec=8s`, shortening chrony's 180-second bound and Ubuntu's
  infinite bound only for the drill. The row's work has a 180-second deadline
  with a 10-second kill grace. After that process exits, cleanup has a separate
  120-second budget with a 5-second kill grace, including after a work timeout.
  Cleanup verifies that its overrides are absent and the waiter is disabled
  before later rows. `chrony-yes.txt` records tracking after the waiter is
  active, with `Result=success` and `ConditionResult=yes`.
- The unit runs with `/etc/dsl41/dsl41.toml`. Its main config selects
  `/opt/nightbank/cfg/*.jil`, including `drill.jil`, and loads
  `night.properties`. It sets UTC and `lint.refuse = "none"`.
  The shipped `estate.toml` remains a rehearsal input (DL-407); the service
  does not load its environment tables. See the [main-config deployment
  procedure](../../../docs/deploy.md).
- The night's profile supplies `NIGHTBANK_SOCKET` for the estate's own
  `SET_GLOBAL` commands.
- The drill installs both units and a hook at `/etc/dsl41/status-hook`.
  It appends stdin to `/srv/dsl41/nightbank/night/status-hooks.jsonl`,
  which the drill owns outside the engine's run root.
  Production delivery remains the site's responsibility (DL-446).
- The region anchors are six hours out, and the `start` row holds every job
  the calendar starts, so only the drill's requests start a job.
- The drill's jobs wait on a global nobody sets. That gate avoids lint's
  L011 dangling-job warning; the install row requires clean lint output.
- The restart half of `leak` adds a drop-in, `KillMode=process`, under
  `/run`. The shipped `KillMode=control-group` ends every process of the unit
  when the engine dies, so the restart would find nothing left.
- The `leak` row takes its expectation from the image's `/etc/os-release`,
  never from the engine's log. Both images require the cookie scan's TERM
  live and at restart. For the current and restarted serving invocations,
  the row checks `cookie scan signalling: <primitive>` in the journal:
  `pidfd` on Ubuntu 24.04 and `os.kill (start-token check)` through `/proc`
  on Rocky Linux 8. Another image fails until it gets an expectation.
- A container restart keeps the underlying Linux kernel's boot identity.
  The driver compares that identity outside the container; on macOS it reads
  the Podman VM. It records the container-visible IDs separately because
  Podman may replace them. This does not qualify an actual host reboot.

## The wargame host

[`scripts/wargame.sh`](../../../scripts/wargame.sh) builds and runs the
container from [`scripts/wargame.Containerfile`](../../../scripts/wargame.Containerfile).
The default host target has no dependency wheelhouse. For the service drill's
install and upgrade rows, use `drill-local.sh`, which exports requirements and sets
`WARGAME_REQUIREMENTS`. Setting that variable by hand selects the `service` target
and the `-service` image suffix, which includes `/wheelhouse`.
Use the ordinary host by hand for other exercises:

```sh
scripts/wargame.sh --base el8 build
scripts/wargame.sh --base el8 start
scripts/wargame.sh --base el8 join
```

`deliver PATH` copies a file or directory to `/delivery/`; `stop` keeps the
container's state; `dispose` removes the container. `start` copies the
checkout to `/work`, owned by `runner`, a user with passwordless sudo. The
image stays until you remove it with `podman image rm`.
Stop/start preserves `/work` and installed state. After checkout edits, use a fresh
named container to copy the current checkout; a restart keeps the retained copy.

The drill does not cover a release, a supervisor, a seal, detached jobs or an
offline install.
