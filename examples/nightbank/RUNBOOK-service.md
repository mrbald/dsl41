# Nightbank service runbook — operator exercises 22 to 32

This page is for an operator who will install, run, recover and retire the engine as
a service. You type the service tasks of the
[deployment runbook's §0](../../docs/deployment-runbook.md#0-the-operator-path) by hand
on a disposable EL8 systemd host, before you do them on a real one. You are done when
the estate you install in exercise 24 is retired and the host is disposed. The page is
in the Operator track of the [README's Documents section](../../README.md#documents).
Work through the [RUNBOOK](RUNBOOK.md)'s exercises 1 to 21 first: this page assumes them.

The host is a Rocky Linux 8 container with systemd as PID 1 and no network, from
[`scripts/wargame.sh`](../../scripts/wargame.sh). Each exercise links the runbook blocks
it uses, named as in recipe `service-install`; type them from there. A comment after a
command gives what you see. The job recipe and §7's rows 3 and 4 are not trained here:
see the [operator curriculum](../../docs/operator-curriculum.md#4-read-through-the-tasks-the-course-does-not-train).
The RPM is the packager's ([site package](../../docs/site-package.md)).

## Shells on the wargame host

`scripts/wargame.sh join` opens a root shell in the host. From it run `su - runner`,
`cd /work` (the checkout's tracked files), `export PATH=/opt/dsl41/.venv/bin:$PATH`
(deployment-runbook §1), and recipe `operator-env` from
[§0](../../docs/deployment-runbook.md#0-the-operator-path). `runner` has passwordless
`sudo`. Three shells run the commands, as §0 assigns them; join again for a second one.
The recipe reads `ESTATE` from `active` when you type it: until exercise 24 writes
`active`, it prints a `cat` error, so type it again after exercise 24 and after any
release switch.

| Shell | Open it with | Runs |
| --- | --- | --- |
| yours | `su - runner`, as above | `query`, `sendevent`, `host`; one root command with `sudo` |
| root | `sudo -s` from `/work`; `exit` after the block | a multi-line root block |
| the service account's | `cd / && sudo -u dsl41 -H bash`, then the `PATH` line and recipe `operator-env` | `seal`, `audit`, `estate`, `supervise`, `journal`, and reads inside `RUN_ROOT` |

## 22. Build the wargame host and deliver the bundle

Goal: build the host class, see its capability check pass, and deliver the offline
bundle exercise 23 installs. `offline-local.sh el8` checks the same build.

1. On your machine, from the checkout, build the EL8 offline image (no `uv`) and the
   wargame image on it (with `uv`). Export the release inputs for a development
   rehearsal, [upstream](../../docs/offline-installation.md#prepare-release-inputs-upstream).
   Podman on macOS mounts only `$HOME`, so keep the checkout and `$out` under it. Build and qualify the bundle in the
   offline image, the [connected target](../../docs/offline-installation.md#build-and-qualify-on-a-connected-target):

```sh
scripts/wargame.sh build
out=$HOME/dsl41-delivery
uv build --wheel --out-dir "$out/dist"
bash scripts/export-release-inputs.sh "$out"/dist/dsl41-*.whl "$out/inputs"
wheel=$(basename "$out"/dist/dsl41-*.whl) base="localhost/dsl41-wargame-base:$(basename "$PWD")"
podman run --rm --volume "$PWD:/src:ro" --volume "$out:/out" "$base" env PYTHON=python3.12 \
    bash /src/scripts/offline/build-wheelhouse.sh "/out/dist/$wheel" /out/inputs /out/bundle
podman run --rm --network none --volume "$out:/out:ro" "$base" env PYTHON=python3.12 \
    bash /out/bundle/tools/qualify.sh /out/bundle /tmp/qualification   # ok: installed 1.12.0 ui …
```

2. Deliver before the host runs, and read the refusal. Then start the host (the start
   runs the capability check), deliver to `/delivery/bundle`, and join:

```sh
scripts/wargame.sh deliver "$out/bundle"   # exit 2: no container dsl41-wargame-<name>; run: … start <name>
scripts/wargame.sh start   # capability: PASS architecture=aarch64 systemd=systemd 239 (…) …
scripts/wargame.sh deliver "$out/bundle" && scripts/wargame.sh join
```

*Why.* A bundle must match its target's Python, OS and architecture (DL-389). On Apple
silicon that is `aarch64`, which does not qualify x86-64 ([offline rehearsal](../../docs/offline-rehearsal.md)).

## 23. Install the package

Goal: prepare a runtime at its final path, see that preparation activates nothing, and
activate it with [§1](../../docs/deployment-runbook.md#1-install)'s explicit link. The
drill's `install` step checks the same.

1. In a root shell, prepare the base profile and try the active runtime:

```sh
bundle=/delivery/bundle prefix=/opt/dsl41
PYTHON=python3.12 bash "$bundle/tools/prepare.sh" "$bundle" "$prefix" base   # …/.venvs/1.12.0-base-<hash>
/opt/dsl41/.venv/bin/dsl41 --help   # No such file or directory
```

2. In the same shell, type §1's activation block (it starts `set -eu`). It prepares
   again, which verifies the candidate, and replaces the link. Read it, and `exit`:

```sh
readlink /opt/dsl41/.venv                             # .venvs/1.12.0-base-<hash>
jq -r .profile /opt/dsl41/.venv/.dsl41/receipt.json   # base
/opt/dsl41/.venv/bin/python -c 'from dsl41.runner_ledger import STATE_MACHINE_VERSION as v; print(v)'   # 24
```

*Why.* Preparation changes no active link, launcher or unit, and package presence
proves nothing without the receipt (DL-389).

## 24. Install and start as a service

Goal: place the estate's first release, install the account, launcher, map and units;
see a start that systemd calls `active` and that refused; start the genesis. Follow
[the service recipe](../../docs/deployment-runbook.md#recipe-install-and-start-as-a-service).
The drill's `install` and `first-start` steps check the same.

1. In a root shell from `/work`, place the example, root-owned and read-only to the
   account. Install the small estate as the release `small-r1`, and name it in `active`.
   Recipe `service-install` ends with the launcher's `--print`, which reads that
   release's manifest, so the release comes first:

```sh
install -d -m 0755 /srv/dsl41 && cp -R examples/nightbank /srv/dsl41/nightbank
install -d -m 0755 /srv/dsl41/nightbank/releases
cp -R /srv/dsl41/nightbank/estate/small /srv/dsl41/nightbank/releases/small-r1
printf '%s\n' small-r1 >/srv/dsl41/nightbank/active
chown -R root:root /srv/dsl41/nightbank && chmod -R go-w,a+rX /srv/dsl41/nightbank
```

   In the same shell, type recipe `service-install`, and `exit`. Its last line, the
   engine's command, names the release's six files in its manifest's order, then
   `--timezone UTC` from the manifest, and ends
   `--access-map /etc/dsl41/nightbank-access.toml`.
2. Type recipe `service-start`, with `sudo` on each line. Both lines print `active`. Five
   seconds later:

```sh
systemctl show -p ActiveState -p ExecMainStatus dsl41-engine.service   # failed, 2
sudo journalctl -u dsl41-engine.service -o cat | tail -3   # [Errno 2] No such file … night.properties'
```

3. Prepare the night as the account with nightbank's own `prepare_night`, anchors six
   hours out, as the drill does. The last line makes one job run long:

```sh
sudo install -d -o dsl41 -g dsl41 -m 0700 /srv/dsl41/nightbank/night
cd / && sudo -u dsl41 -H /opt/dsl41/.venv/bin/python -c 'import datetime as d, importlib.util as u, pathlib
from importlib.machinery import SourceFileLoader as Loader
nb = Loader("nightbank", "/srv/dsl41/nightbank/bin/nightbank"); m = u.module_from_spec(u.spec_from_loader("nightbank", nb)); nb.exec_module(m)
m.prepare_night(pathlib.Path("/srv/dsl41/nightbank/night"), "small", anchor_utc=d.datetime.now(d.UTC) + d.timedelta(hours=6))'
echo "OPS_XINST_DEMO_C late 45" | sudo -u dsl41 -H tee -a /srv/dsl41/nightbank/night/incidents.conf
```

4. Type recipe `service-start` again, and recipe `wait-answers` in the service account's
   shell: it prints one `INACTIVE` line per job, and the journal ends `engine up; control
   socket: …`. Read the engine unit's cgroup:
   `sudo cat /sys/fs/cgroup/system.slice/dsl41-engine.service/cgroup.procs | xargs ps -o args= -p`
   names `dsl41 run` alone; the supervisor unit's names `runner_supervisor.py`.

*Why.* `active` is not readiness: a unit that refuses a second later printed it too.
The supervisor's own unit keeps its jobs through an engine stop (§0's diagram 1). A
release directory is never changed, and `active` is a plain file, never a symlink: the
opener must name the paths the seal named (DL-407).

## 25. Configure an estate

Goal: open the socket to a group, see each tier decide, read the receipts, and meet the
refusals only a restart or a boundary clears. Follow
[the configure recipe](../../docs/deployment-runbook.md#recipe-configure-an-estate). The
drill's `configure` step checks the same.

1. Check the identities and ask the socket as yourself. Create the groups: you join ops,
   and `dreadu` observes:

```sh
systemctl show -p User --value dsl41-engine.service dsl41-supervisor.service   # dsl41 (both)
sudo stat -c '%U %a %n' "$RUN_ROOT" "$ESTATE_ANCHOR" /etc/dsl41/nightbank-access.toml   # 700, 700, 600
dsl41 query status --brief -S "$S"   # exit 2: [Errno 13] Permission denied
for group in nightbank-socket nightbank-ops nightbank-observers; do sudo groupadd "$group"; done
sudo usermod -aG nightbank-socket,nightbank-ops runner
sudo useradd -M -s /usr/sbin/nologin -G nightbank-socket,nightbank-observers dreadu
```

2. Name the group in the map and restart: the account is not a member, so the start
   refuses with exit 2. Add it, start, wait for the engine as the service account, and
   grant the run roots' directory:

```sh
sudo systemctl stop dsl41-engine.service
sudo sed -i 's/^# socket_group = .*/socket_group = "nightbank-socket"/' /etc/dsl41/nightbank-access.toml
sudo systemctl start dsl41-engine.service
sleep 5   # wait 5 s for the start to refuse
sudo journalctl -u dsl41-engine.service -o cat | tail -3   # cannot open the run root to group 'nightbank-socket'
sudo usermod -aG nightbank-socket dsl41 && sudo systemctl start dsl41-engine.service
sudo -u dsl41 -H bash -c 'for try in $(seq 90); do /opt/dsl41/.venv/bin/dsl41 query status --brief -S "$1" >/dev/null 2>&1 && break; sleep 1; done' sh "$S"   # wait up to 90 s for an answer
sudo stat -c '%a %G' "$RUN_ROOT" "$S"   # 710 nightbank-socket, 660 nightbank-socket
sudo chgrp nightbank-socket /srv/dsl41/runs && sudo chmod 0710 /srv/dsl41/runs
```

3. Log in again, so your shell has the new groups: `exit`, then open your shell as above
   (`su - runner`, the `PATH` line, recipe `operator-env`). `id` lists `nightbank-socket`
   and `nightbank-ops`.
4. Let the bindings decide:

```sh
dsl41 sendevent ON_HOLD -J OPS_SPOOL_C -S "$S" && dsl41 sendevent OFF_HOLD -J OPS_SPOOL_C -S "$S"
sudo -u dreadu /opt/dsl41/.venv/bin/dsl41 query status --brief -S "$S"   # answers
sudo -u dreadu /opt/dsl41/.venv/bin/dsl41 sendevent ON_HOLD -J OPS_SPOOL_C -S "$S"
# exit 2: os/dreadu holds read tier; sendevent:ON_HOLD needs ops tier
sudo jq -r 'select(.rec == "access_denied") | .principal' "$RUN_ROOT/perimeter.jsonl"   # dreadu
```

5. Reload a map that names another group: the engine stays up and refuses it. Then
   change the runtime profile: the timezone is the release manifest's, and an edit of it
   in place stands for a release that changes it. The resume gate refuses with exit 2.
   Put both files back.
   The last start can fail: `systemctl` prints `Job for dsl41-engine.service failed`, and
   the journal says `Start request repeated too quickly`, because the start limit (five
   in 300 s) counts your starts. Then run `sudo systemctl reset-failed
   dsl41-engine.service` and start again ([stop recipe](../../docs/deployment-runbook.md#recipe-stop-restart-seal-and-recover)):

```sh
sudo sed -i 's/^socket_group = .*/socket_group = "nightbank-ops"/' /etc/dsl41/nightbank-access.toml
sudo systemctl kill -s HUP --kill-who=main dsl41-engine.service
sleep 3   # wait 3 s for the reload
sudo jq -r 'select(.rec == "policy_reload_failed") | .error' "$RUN_ROOT/perimeter.jsonl"   # socket_group is fixed at arming, …
sudo sed -i 's/^socket_group = .*/socket_group = "nightbank-socket"/' /etc/dsl41/nightbank-access.toml
sudo systemctl stop dsl41-engine.service && sudo cp -p "$ESTATE/estate.toml" /root/estate.toml.orig
sudo sed -i 's|^timezone = "UTC"$|timezone = "America/New_York"|' "$ESTATE/estate.toml"
sudo systemctl start dsl41-engine.service
sleep 5   # wait 5 s for the resume gate to refuse
sudo journalctl -u dsl41-engine.service -o cat | tail -3   # runtime-profile mismatch: … (default_tz) …
sudo cp -p /root/estate.toml.orig "$ESTATE/estate.toml" && sudo systemctl start dsl41-engine.service
sudo -u dsl41 -H bash -c 'for try in $(seq 90); do /opt/dsl41/.venv/bin/dsl41 query status --brief -S "$1" >/dev/null 2>&1 && break; sleep 1; done' sh "$S"   # wait up to 90 s for an answer
```

   Keep the socket group: the later exercises send control commands as you.

*Why.* Bindings, not the socket's mode, decide tiers (access-model §4, §7). A reload
cannot move `socket_group`, and a changed run option needs a boundary (DL-252).

## 26. Watch and measure

Goal: read every row of [what to watch](../../docs/deployment-runbook.md#what-to-watch)
once, find a failure, and take the five observations of
[capacity and limits](../../docs/deployment-runbook.md#capacity-and-limits) (DL-317). The
drill's `watch` step runs every row's command and checks its output.

1. Run each row's "Read it with" command in the shell it needs. Here the engine unit
   reads `active`, status 0, no restarts; recipe `watch-sealed`, in the service account's
   shell, prints `open`;
   `dsl41 supervise list` answers `"ok": true`, `"runs": []`, `"deadman_s": null`; the
   `pid` in `leader.lock` is the engine's `MainPID`.
2. In a second terminal, as yourself, run `dsl41 query subscribe -S "$S"`. Start the job
   the night's `fail_once` incident fails; the stream prints the input and its decision:

```sh
dsl41 sendevent FORCE_STARTJOB -J AMER_MKT_FX_C -S "$S"
sleep 15   # wait 15 s: the run fails about 9 s after its start
dsl41 query status --brief -S "$S" --job AMER_MKT_FX_C   # FAILURE … run 1 exit 5
dsl41 query trace --since 0 -S "$S" | jq -c '.entries[] | select(.transition | test("FAILURE"))'
# RUNNING->FAILURE for the job; INACTIVE->FAILURE for its box, AMER_EOD_B
sudo journalctl -u dsl41-engine.service -o cat | grep -c -e '^dsl41: input quarantined' -e '^dsl41: statechart error'   # 0
```

3. Take the five capacity observations. As yourself the WAL glob finds nothing: the
   root is `0710` to your group, which reaches the socket and cannot list the directory.
   Run the reads inside `RUN_ROOT` in the service account's shell. On this host:

| Observation | Read |
| --- | --- |
| engine resident set | 56832 KiB |
| open WAL segment | 8 KiB |
| trace length, `last_seq` | 9 |
| run directory: `runs`, `logs`, entries under `runs` | 40 KiB, 0 KiB, 14 |
| one `dsl41 query status --brief` | 0.21 s |

## 27. Back up and restore

Goal: stop the estate so nothing writes, copy it, lose it, and restore it at the same
paths. Follow [§2b](../../docs/deployment-runbook.md#2b-quiescent-backup-and-restore). The
drill's `quiesce` and `restore` steps check the same.

1. Hold every job that has a timer, so the calendar starts nothing:
   `for job in $(dsl41 query timers -S "$S" | jq -r '.timers[].job' | sort -u); do dsl41 sendevent ON_HOLD -J "$job" -S "$S"; done`.
2. Type §2b's `dsl41 estate quiesce` command as yourself; it runs as the service account
   through `sudo -u dsl41 -H`. Each refusal exits 2 and names what to do; do it, and run
   the verb again:

```
quiesce: hold: refused: dsl41-engine.service is enabled; … as root: systemctl disable …   (recipe hold-down)
quiesce: engine: refused: dsl41-engine.service is active/running; as root: systemctl stop dsl41-engine.service
quiesce: horizon: refused: the retry horizon runs until <time> UTC; rerun after it, or pass --force-seal
```

   The horizon counts from step 1's holds: wait until the time it names, about a minute.
   Then every stage ends `ok`, with `sealed period 1 …; period 2 is ready to open` and
   `period 1 attested`, and the verb exits 0. Recipe `watch-sealed`, in the service
   account's shell, prints `closed`; `pgrep -u dsl41` prints nothing.
3. Copy, lose both, restore the anchor alone, and audit. Restore the root at its
   recorded path, and audit again:

```sh
sudo install -d -m 0700 /srv/dsl41/backup
sudo cp -a "$RUN_ROOT" /srv/dsl41/backup/root && sudo cp -a "$ESTATE_ANCHOR" /srv/dsl41/backup/anchor
sudo rm -rf "$RUN_ROOT" "$ESTATE_ANCHOR" && sudo cp -a /srv/dsl41/backup/anchor "$ESTATE_ANCHOR"
sudo -u dsl41 -H /opt/dsl41/.venv/bin/dsl41 audit --estate-anchor "$ESTATE_ANCHOR"
# exit 2: period 1: registry root /srv/dsl41/runs/nightbank-01 is missing -- …
sudo cp -a /srv/dsl41/backup/root "$RUN_ROOT"
sudo -u dsl41 -H /opt/dsl41/.venv/bin/dsl41 audit --estate-anchor "$ESTATE_ANCHOR"   # period 1 in … attested
```

4. Start the engine unit, wait with recipe `wait-answers`, and release the reboot hold
   with recipe `hold-release`. The launch line ends `--resume`, `wal/` holds
   `000001.jsonl` and `000002.jsonl`, and the holds are on (`H` in
   `dsl41 query status --brief`).

*Why.* The verb never waits for a precondition (DL-398). The registry names each root by
its absolute path, so audit and resume refuse a restore elsewhere (DL-224).

## 28. Upgrade dsl41, row 1 and row 2

Goal: flip the runtime under a running job (row 1), open the next period in a fresh
root on the other runtime (row 2), and roll each back, by
[§7](../../docs/deployment-runbook.md#7-upgrading-dsl41-itself). The two runtimes are
profiles of one build and qualify no version pair. The drill's upgrade steps check this.

1. Prepare the second runtime, and answer §7's first question with its check in both
   venvs: 24 and 24. Start the long job, and note two processes:

```sh
prefix=/opt/dsl41; sudo env PYTHON=python3.12 bash /delivery/bundle/tools/prepare.sh /delivery/bundle "$prefix" ui
dsl41 sendevent FORCE_STARTJOB -J OPS_XINST_DEMO_C -S "$S"
sleep 3   # wait 3 s for the job's process
pgrep -f 'fakework OPS_XINST_DEMO_C'; systemctl show -p MainPID --value dsl41-supervisor.service
```

2. Run row 1's three commands. For the activation, set `bundle=/delivery/bundle` in a
   root shell and type §1's block with its `profile=base` line changed to `profile=ui`.
   After the start, wait with recipe `wait-answers`. The launch line ends `--resume`, the
   engine runs from `.venvs/1.12.0-ui-…`, and both processes live on. Roll back the same
   way, with §1's block as printed (`profile=base`); within two minutes the job reads
   `SUCCESS`.
3. Row 2: check that `dsl41 supervise list` shows no live wrapper, take recipe
   `hold-down`, stop the engine unit, and run exercise 27's `estate quiesce` command: it
   seals period 2. Keep a §2b copy of the anchor for exercise 29
   (`sudo rm -rf /srv/dsl41/backup/anchor-before-roll`, then
   `sudo cp -a "$ESTATE_ANCHOR" /srv/dsl41/backup/anchor-before-roll`), and activate as in
   step 2, with the `profile=ui` line.
4. Provision the new root, point the launcher and both units at it, and ask the launcher
   for its command. It refuses with exit 2:

```sh
new=/srv/dsl41/runs/nightbank-01b
sudo mkdir -m 0700 "$new" && sudo sh -c 'umask 077; set -C; : >"$1/leader.lock"' sh "$new"
sudo chown dsl41:dsl41 "$new" "$new/leader.lock"
sudo sed -i "s|^RUN_ROOT=.*|RUN_ROOT=$new|" /opt/dsl41/bin/dsl41-launch
sudo sed -i "s|^RequiresMountsFor=.*|RequiresMountsFor=$new|" \
    /etc/systemd/system/dsl41-engine.service /etc/systemd/system/dsl41-supervisor.service
sudo systemctl daemon-reload
sudo -u dsl41 -H /opt/dsl41/bin/dsl41-launch --print
# dsl41-launch: …/nightbank-01.anchor holds a lineage and …/nightbank-01b holds no journal.jsonl: a genesis
# would fork it. To open … create /srv/dsl41/runs/nightbank-01b.open-from and start again
```

5. Run row 2's commands from the trigger on, with `<new>` as `nightbank-01b`. With the
   trigger, `--print` ends `--open-from /srv/dsl41/runs/nightbank-01.anchor`. After the
   start, wait with recipe `wait-answers` in the service account's shell. The journal
   says `opened period 3 in /srv/dsl41/runs/nightbank-01b from seal sha256:…`, and the
   trigger is gone. Set `RUN_ROOT` and `S` to the new root in each shell. The holds
   crossed; row 2 releases them with `OFF_HOLD`, but keep them, so the calendar starts
   nothing later. A job whose tick fell while held shows `HA`, the armed latch of the
   [RUNBOOK's exercise 16](RUNBOOK.md#16-open-period-2--what-crossed-and-what-did-not).
6. Roll back: repeat steps 3 to 5 with §1's block as printed and `nightbank-01c`. The journal
   says `opened period 4 in /srv/dsl41/runs/nightbank-01c`.

*Why.* A release not marked resume-safe opens a fresh root with its own supervisor
(DL-266). The launcher refuses a genesis beside an anchor, so it cannot fork the lineage.

## 29. Recover a rolled root whose opening is torn

Goal: meet `missing segment record` on a rolled root, and follow
[the torn-opening recipe](../../docs/deployment-runbook.md#recipe-recover-a-rolled-root-whose-opening-is-torn)
through its `open` branch. `test_operator_recipes.py` runs the recipe; no drill step does.

1. Make the damage the recipe's test calls `damage`: stop both units, cut the root's only
   segment to half its first line, and start:

```sh
sudo systemctl stop dsl41-engine.service
sudo -u dsl41 -H /opt/dsl41/.venv/bin/dsl41 supervise shutdown --run-root "$RUN_ROOT"   # exit 0
sudo -u dsl41 -H bash -c 'seg=$1/wal/000004.jsonl; first=$(head -n 1 "$seg")
    printf %s "${first:0:$((${#first} / 2))}" >"$seg"' sh "$RUN_ROOT"
sudo systemctl start dsl41-engine.service
sleep 5   # wait 5 s for the start to refuse with exit 2
sudo journalctl -u dsl41-engine.service -o cat | tail -3   # …/wal/000004.jsonl: missing segment record
```

2. Step 1: recipe `torn-look`, in the service account's shell, prints
   `{"period_id":4,"root":"/srv/dsl41/runs/nightbank-01c","state":"open"}` and exits 0:
   the supervisor `Requires=` started lists no runs.
3. Step 2, in your shell: recipe `hold-down`, and this exercise's step 1 `supervise
   shutdown` again. Wait 6 s, past `RestartSec` (2 s): the supervisor unit stays
   `inactive`, and `pgrep -u dsl41` prints nothing.
4. Step 3: recipe `torn-check`, in the service account's shell, prints `open
   000004.jsonl`. An `open` head is damage, so the recipe goes to its step 6. In your
   shell, restore the anchor from exercise 28's copy (`sudo rm -rf "$ESTATE_ANCHOR"`,
   then `sudo cp -a /srv/dsl41/backup/anchor-before-roll "$ESTATE_ANCHOR"`). Its head
   reads `closed`, period 3. `nightbank-01b` did not change after the copy, so it needs
   no restore.
5. Step 7: provision `/srv/dsl41/runs/nightbank-01d` and point the launcher and units at
   it, as in exercise 28 step 4. Set `RUN_ROOT` and `S` to it, type recipe `torn-open` in
   the service account's shell, start the engine unit, and wait with recipe
   `wait-answers`. The journal says `opened period 4 in /srv/dsl41/runs/nightbank-01d`.
   Release the reboot hold with recipe `hold-release`.
6. Run recipe `torn-check` with `RUN_ROOT` set to the damaged `nightbank-01c`. It stops:
   `stop: the head names another root`.

No command on this host stops a roll between its segment and its head move, so the
recipe's `claimed` branch (its steps 4 and 5, and the reclaim) is not trained here.

*Why.* A rolled root has no earlier segment to reopen from, and no code repairs a torn
sole opening (DL-144). After step 7 the registry does not name the old root (DL-224).

## 30. Idle supervisor maintenance

Goal: see that only the service account's guarded verb stops the supervisor, that it
refuses live work, and what `--force=terminate` leaves. Follow
[§3's guarded maintenance](../../docs/deployment-runbook.md#3-starting-the-engine) and
[§8's rows](../../docs/deployment-runbook.md#manual-intervention). The offline
rehearsal's maintenance probe checks the same.

```sh
dsl41 sendevent FORCE_STARTJOB -J OPS_XINST_DEMO_C -S "$S"
sleep 3   # wait 3 s for the job to run
sudo systemctl stop dsl41-supervisor.service   # exit 4: … Operation refused … (restart too)
sudo -u dsl41 -H /opt/dsl41/.venv/bin/dsl41 supervise shutdown --run-root "$RUN_ROOT"
# exit 2: cannot acquire lease: {"error": "lease_held", … "holder": "engine:/srv/dsl41/runs/nightbank-01d#…"}
sudo systemctl stop dsl41-engine.service
sudo -u dsl41 -H /opt/dsl41/.venv/bin/dsl41 supervise shutdown --run-root "$RUN_ROOT"
# exit 2: {"detail": "active wrappers or residual service processes", "error": "maintenance_refused", …}
sudo -u dsl41 -H /opt/dsl41/.venv/bin/dsl41 supervise shutdown --run-root "$RUN_ROOT" --force=terminate
# exit 0: {… "force": "terminate", … "state": "completed", …}
systemctl show -p ActiveState -p Result -p ExecMainStatus dsl41-supervisor.service   # inactive, success, 8
```

`supervisor.log` ends `maintenance admitted force=terminate bound_s=50.0` and
`maintenance completed`. Start the engine unit, wait with recipe `wait-answers`, and
wait 5 s more for the reconciliation: the job reads `TERMINATED`,
still run 2, and `dsl41 journal "$RUN_ROOT"` prints
`OPS_XINST_DEMO_C RUNNING->TERMINATED [injected STATUS]`.

*Why.* `RefuseManualStop=yes` leaves the stop to the verb; exit 8 is completed
maintenance, never restarted (DL-390, DL-399). The engine reconciles from the spool.

## 31. Break glass: evict the executor

Goal: see why the gated eviction refuses here, take the forced one, and see what the
estate can do afterwards ([concurrency-model §8](../../docs/concurrency-model.md#8-host-lifecycle-active-passive-quarantined-evicted)).
No drill step evicts; `tests/test_hosts.py` checks the forced path in process.

```sh
dsl41 host list -S "$S"                 # local: active, generation 0, "deadman_s": null
dsl41 host evict local -S "$S"          # exit 3, host_not_quarantined: host 'local' is active: …
dsl41 host drain local -S "$S"          # applied
dsl41 host evict local -S "$S"
# exit 3: host 'local' is passive: eviction needs … (ss8 precondition 1), which is the `quarantined` state. …
dsl41 host evict local --force --claimed-actor runner@wargame -S "$S"   # applied
dsl41 host list -S "$S" | jq -c .hosts.local   # "forced_by":"os/runner", "generation":1, "state":"evicted"
dsl41 host activate local -S "$S"
# exit 3: host 'local' was evicted at generation 1: it returns by re-registering … and self-fencing first …
dsl41 sendevent FORCE_STARTJOB -J AMER_INV_MACROS_C -S "$S"   # applied
sleep 12   # wait 12 s, past the start's dispatch
dsl41 query status -S "$S" --job AMER_INV_MACROS_C | jq -c '.jobs[] | {status, held}'   # RUNNING, held
```

The leader alone sets `quarantined`, so the gated path ends at precondition 1 here.
Precondition 2 fails too: `deadman_s` is null, and a host with a null deadman cannot be
evicted except by force ([period-model §3.3](../../docs/period-model.md#33-carried-and-not-carried)).
`forced_by` names the authenticated principal, not the claim, because the access map is
armed. The forced start has no process: `dsl41 supervise list` shows no run.

This lineage has no path back to running jobs. On one host an evicted row is a dead
end: the un-evict, re-registration and self-fencing, is the relay's act, and the relay
is not built (period-model §3.3; concurrency-model §8; DL-97, DL-103). A new estate, a
genesis on a new root and anchor ([§7 row 4](../../docs/deployment-runbook.md#7-upgrading-dsl41-itself)),
runs jobs again. This lineage can still seal and audit, so this exercise ends in
exercise 32.

*Why.* `evicted` lets other hosts rerun this host's work, so it alone can cause a
double run. Force skips the proof and records who took it (DL-151).

## 32. Retire the estate

Goal: end the lineage, keep its history, and leave nothing that starts. Follow
[retiring an estate](../../docs/deployment-runbook.md#retiring-an-estate). The drill's
`retire` step checks the same.

1. Step 1: recipe `hold-down`; both units stay `active`.
2. Steps 2 and 3: the jobs are held, and exercise 31's start has no process. Stop the
   engine unit and run the `estate quiesce` command, after the horizon it names: it
   seals period 4. The seal carries the evicted row and the unlaunched start:

```sh
sudo jq -c '.state.hosts.local | {state, forced_by}' "$RUN_ROOT/seals/000004.json"
# {"state":"evicted","forced_by":"os/runner"}
sudo jq -c '.executions[] | {job, kind}' "$RUN_ROOT/seals/000004.json"
# {"job":"AMER_INV_MACROS_C","kind":"pending_spawn"}
```

3. Step 4: in a root shell, set `RUN_ROOT` and type recipe `retire-remove`. Both
   `reset-failed` lines print `Unit … not loaded`, and the `|| :` passes them.
4. Step 5: recipe `retire-check`, with `sudo`: each unit prints `LoadState=not-found`
   and `ActiveState=inactive`. A stray `sudo systemctl start dsl41-engine.service` exits
   5, `Unit dsl41-engine.service not found`, and `pgrep -u dsl41` prints nothing.
5. Steps 6 and 7, in the service account's shell: recipe `retire-audit` attests periods
   1 and 2 in `nightbank-01`, 3 in `-01b` and 4 in `-01d`. Recipe `retire-list` names
   those three roots, not exercise 29's damaged `nightbank-01c`.
6. Leave the host. On your machine, run `scripts/wargame.sh dispose` and
   `rm -rf "$HOME/dsl41-delivery"`.

*Why.* There is no retire verb: the final seal is the lineage's closing record, and the
site deletes only whole sets, under its retention decision (DL-268).
