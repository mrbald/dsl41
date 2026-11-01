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

Each run takes about two minutes once the image is built; the first build
takes several more. The script prints `ok:` per check, a summary with each
row's seconds, and exits 0 only when every row passed.

| Option | Effect |
| --- | --- |
| `ROW...` instead of `all` | Run these rows, after `install` and `start`, in the drill's order. |
| `DRILL_KEEP=1` | Keep the container. `scripts/wargame.sh --base BASE join NAME` opens a shell in it; the script prints the command. |
| `PODMAN='sudo podman'` | Use a rootful engine. The workflow sets it. |

The workflow [`service-drill.yml`](../../../.github/workflows/service-drill.yml)
runs the same script for both images on a GitHub runner. Dispatch it by hand;
it is not part of the default gate.

## What each row checks

Every row reads what an operator reads: `systemctl`, `journalctl -u`,
`dsl41 query status` and `dsl41 runs`. A wait polls with a deadline.

| Row | What it does | What it checks |
| --- | --- | --- |
| `install` | Installs this tree's wheel into `/opt/dsl41/venv`, the small Nightbank estate, the drill's jobs, the `dsl41` user and the unit | The estate lints clean; the unit is enabled |
| `start` | `systemctl start` | `engine up` in the journal, `engine.db` exists, the scheduled jobs are held |
| `restart` | `systemctl restart` | A new invocation, `engine stopped: clean_shutdown`, the holds are still there |
| `reboot` | Restarts the container | The enabled unit starts at boot and resumes the same root |
| `kill-engine` | SIGKILL of the engine with a job running | The unit restarts it; attempt 1 ended (`lifeline` or `engine_death`) and attempt 2 of the same run runs |
| `drain` | `systemctl stop` with a job that ignores TERM | The stop waits the TERM grace, `ExecStop` exits 0, `clean_shutdown`; after the start the rerun follows `engine_stop` |
| `reload` | `dsl41 reread`, then `dsl41 update` with a running job and a changed definition | Exit 1 from reread, a refusal without `--stop-running`, `config_rev` + 1, a rerun after `update_stop` on the new command |
| `leak` | A job leaves a child in its own session | The engine TERMs it at the job's exit, and at a restart (DL-431, DL-433) |
| `refusal` | Changes the estate without an update, then restarts | Exit 2, the unit stays failed, the journal names the changed estate |
| `start-limit` | SIGKILL of the engine until the unit gives up | Failed at the sixth kill, `repeated too quickly`; `reset-failed` and a start recover |
| `upgrade` | Installs the same tree as version `X.post1` into the same venv, restarts | The new version runs; the root, its revision and the holds are kept |

After a failed row the script prints the diagnostics: `systemctl status`, the
unit's journal, `dsl41 runs` and `dsl41 query status`. It stops at the first
failure.

## Where the drill differs from a site

- The drill's manifest drops the shipped `[environments]` tables, because the
  unit's `ExecStart` names no `--environment`, and a manifest that declares
  environments refuses a start without one (DL-407). It adds the night's
  properties as a layer and the drill's jobs in `drill.jil`.
- The region anchors are six hours out, and the `start` row holds every job
  the calendar starts, so only the drill's requests start a job.
- The drill's jobs wait on a global nobody sets. Without that gate, lint's
  L011 flags them as dangling, and `dsl41 reread` refuses every warning.
- The restart half of `leak` adds a drop-in, `KillMode=process`, under
  `/run`. The shipped `KillMode=control-group` ends every process of the unit
  when the engine dies, so the restart would find nothing left.
- The `leak` row takes its expectation from the image's `/etc/os-release`,
  never from the engine's log. On Ubuntu 24.04 the cookie scan's TERM is
  required, and a journal line `cookie scan unavailable` fails the row. On
  Rocky Linux 8, Python 3.12 has no `os.pidfd_open`, so the engine keeps
  DL-429's checks (DL-431). There the row requires that line in the journal,
  checks that the child outlives its attempt, and checks that the unit's stop
  ends it. Another image fails the row until it gets an expectation.
- A container restart keeps the kernel's boot identity, so `reboot` does not
  test a changed boot identity.

## The wargame host

[`scripts/wargame.sh`](../../../scripts/wargame.sh) builds and runs the
container from [`scripts/wargame.Containerfile`](../../../scripts/wargame.Containerfile).
Use it by hand for a service exercise:

```sh
scripts/wargame.sh --base el8 build
scripts/wargame.sh --base el8 start
scripts/wargame.sh --base el8 join
```

`deliver PATH` copies a file or directory to `/delivery/`; `stop` keeps the
container's state; `dispose` removes the container. `start` copies the
checkout to `/work`, owned by `runner`, a user with passwordless sudo. The
image stays until you remove it with `podman image rm`.

The drill does not cover a release, a supervisor, a seal, detached jobs or an
offline install.
