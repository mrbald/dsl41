# Deployment runbook — installing and operating dsl41 on a server

Scope: a single host running one engine per estate (the runner's machine
model, DL-49/DL-52 — jobs pinned elsewhere are refused as foreign). Four
components ship in one package:

| Component | Process | How it runs |
|---|---|---|
| engine (runner + calendar scheduler + control socket) | `dsl41 run` | one long-lived foreground process per estate |
| supervisor | `dsl41 supervise start` in its own unit (§3, shape 1), or spawned by `dsl41 run --detached` (shape 2) | one per run root, outlives the engine |
| TUI | `dsl41 ui` | thin client of the control socket, attach/detach at will |
| web UI | `dsl41 serve` | thin client; one `dsl41 ui` subprocess per browser session |

Everything below assumes a POSIX server with Python ≥ 3.12 on it, and an
init that reaps orphaned processes (§3).
§0 is the short path for an operator: tasks, diagrams and recipes.

## 0. The operator path

Start here to run an estate (DL-268). This section lists the tasks, draws
the processes and the decisions, and gives the recipes. The sections after
it hold the detail, and the recipes link to them rather than repeat them.
The commands use the shape-1 example in `examples/nightbank/deploy/` (§3's
worked example) and its paths. Run `systemctl` and the install commands as
root. Run the `dsl41` commands that read or write the run root itself
(`seal`, `audit`, `estate`, `supervise`, `journal`, `runs`) as the service
account, for example with `sudo -u dsl41 -H`. Send control-socket commands
(`query`, `sendevent`, `host`, `ui`) as yourself: the access map then names
you in its receipts ([configure](#recipe-configure-an-estate)). Run
`dsl41 release` as the deployment identity
([the job recipe](#recipe-add-change-or-remove-a-job)).

The recipes name the launcher's configuration through these variables.
Set them once per shell. The values are the example launcher's. `ESTATE`
is the active release's directory, read from `ACTIVE` when you type the
block, so type it again after a release switch:

<!-- recipe: operator-env -->
```sh
RUN_ROOT=/srv/dsl41/runs/nightbank-01
ESTATE_ANCHOR=/srv/dsl41/runs/nightbank-01.anchor
ESTATE_RELEASES=/srv/dsl41/nightbank/releases
ACTIVE=/srv/dsl41/nightbank/active
ENVIRONMENT=wargame
ESTATE=$ESTATE_RELEASES/$(cat "$ACTIVE")
PROPERTIES=/srv/dsl41/nightbank/night/night.properties
S=$RUN_ROOT/control.sock
```

Some blocks are marked as recipes in this file's source.
`tests/test_operator_recipes.py` runs the job recipe's check block, its
hand path's seal and open, the readiness wait and the retirement's audit
and list as written, against a synthetic estate, with the engine unit's
`ExecStart=` in place of `systemctl start`.
`tests/test_release.py` runs `dsl41 release` against a stub
engine, and the service drill runs it on a host.
`tests/test_operator_recipes.py` also runs the sealed check and the
torn-opening recipe where `jq` is installed. The service drill (§3's worked example) runs the service, hold,
sealed-check and retirement recipes as written; CI checks their content
but does not run them. The variables block is compared with the launcher.

### Task index

| Task | Where |
| --- | --- |
| install the package | §1 |
| install, configure and start as a service | [the service recipe](#recipe-install-and-start-as-a-service) |
| configure identities, the execution profile, socket exposure and access | [the configure recipe](#recipe-configure-an-estate) |
| add, change or remove a job | [the job recipe](#recipe-add-change-or-remove-a-job) |
| stop, restart, seal or recover | [the stop recipe](#recipe-stop-restart-seal-and-recover) |
| watch an estate | [what to watch](#what-to-watch) |
| know the tested size and its limits | [capacity and limits](#capacity-and-limits) |
| back up and restore | §2b |
| upgrade dsl41 | §7 |
| keep or prune history | §2a |
| retire an estate, and who cleans what | [retiring an estate](#retiring-an-estate) |
| investigate a night | §4's offline readers, §8 |

### Processes, units and storage

Diagram 1. The names are the example's unit files, launcher modes and
paths. `RUN_ROOT` and `ESTATE_ANCHOR` are the launcher's values.

<!-- diagram: processes -->
```text
systemd
├── dsl41-supervisor.service    User=dsl41  Restart=always  RestartPreventExitStatus=2 7 8
│     ExecStart=/opt/dsl41/bin/dsl41-launch supervisor
│     ExecStartPost=/opt/dsl41/bin/dsl41-launch supervisor-ready
│     └── dsl41 supervise start --run-root RUN_ROOT                 the supervisor
│           ├── owns RUN_ROOT/supervisor.sock, supervisor.pid, supervisor.lock, supervisor.log
│           └── one wrapper per detached run, and the run's command, in this unit's cgroup;
│               they write RUN_ROOT/runs/ and the job's output
│
└── dsl41-engine.service        User=dsl41  Restart=on-failure  RestartPreventExitStatus=2 3 6 7
      Requires=dsl41-supervisor.service  After=dsl41-supervisor.service
      ExecStart=/opt/dsl41/bin/dsl41-launch engine
      └── dsl41 run ... --run-root RUN_ROOT --estate-anchor ESTATE_ANCHOR --detached --supervisor attach   the engine
            ├── holds RUN_ROOT/leader.lock and ESTATE_ANCHOR/anchor.lock for its whole life
            ├── writes RUN_ROOT/wal/, seals/, periods/, catalogs/, perimeter.jsonl
            │   and ESTATE_ANCHOR/anchor.json
            ├── serves RUN_ROOT/control.sock to dsl41 query, sendevent, seal, ui and serve
            └── asks the supervisor to spawn and kill over RUN_ROOT/supervisor.sock

storage                               owner and mode              written by
/opt/dsl41/.venv -> .venvs/<id>      root                        explicit activation (§1, §7)
/opt/dsl41/bin/dsl41-launch           root, 0755                  the installer; reviewed like code
/etc/dsl41/nightbank-access.toml      dsl41, 0600                 the operator (§4)
ESTATE_RELEASES/<name>                deployment identity; its    dsl41 release prepare (§0's job recipe)
                                      inputs read-only to all
ACTIVE                                deployment identity         dsl41 release apply, or the installer
                                                                  before the first start
PROPERTIES                            readable by dsl41           the operator
RUN_ROOT                              dsl41, 0700                 the engine, the supervisor, the wrappers
job data and output directories       dsl41                       the jobs (the JIL names them)
ESTATE_ANCHOR                         dsl41, 0700                 the engine, or an offline dsl41 seal
```

An offline `dsl41 seal` takes both locks itself while no engine runs (§6a).
Stopping the engine unit leaves the supervisor unit and its runs alone.
Stopping the supervisor unit ends every command it still runs, and the
engine unit's `Requires=` stops the engine unit with it.

### Which move

Diagram 2. Read it from the top and take the first branch that fits.

<!-- diagram: decisions -->
```text
What has to happen?
│
├── the engine process must go, and the estate keeps running        ENGINE STOP
│   (a crash, an engine restart, a dsl41 patch of row 1)
│     systemctl stop dsl41-engine.service: detached runs keep running
│     systemctl start dsl41-engine.service: the launcher passes --resume (§5)
│
├── the host must reboot (an OS patch)                               HOST REBOOT
│     hold the scheduled jobs and drain (§6 steps 1 and 2), then reboot
│     the enabled units start at boot and the engine resumes the same root
│     no seal and no reboot hold; nothing that still ran survives
│
├── nothing may run or write, even across a reboot                   ESTATE STOP
│   (a backup, a retirement, upgrade rows 2 and 4)
│     take the reboot hold and stop the engine unit, then run
│     dsl41 estate quiesce: it seals, audits and stops the supervisor
│     unit (§2b); release the hold at the end (a retirement never does);
│     it refuses while a command runs, unless --force terminate
│
├── the engine unit failed with exit 3                               SEALED, NOT OPENED
│     the period is sealed and the next one waits; nothing runs until it opens
│     open it in place (start the engine unit) or in a fresh root (§7 row 2)
│     an enabled engine unit opens it in place at the next boot
│
├── the engine unit failed with exit 6                               TIME-HALF STOP
│     the timers due at an engine-made input raised; nothing was journaled (§3)
│     deploy a release that fixes the fault, then start the unit (§7)
│
├── the engine or supervisor unit failed with exit 7                 STATECHART ERROR
│     a declared state machine hit an assertion failure; nothing of it was journaled (§3)
│     a supervisor's exit ended every command it ran, as its other exits do (§0)
│     report the `dsl41: statechart error` lines, deploy a fixed release, start the unit (§7)
│
├── new JIL, properties or run options, and the history continues   TRANSITION
│   ├── in the same run root                                         IN-PLACE TRANSITION
│   │     dsl41 release apply: hold, drain, seal, start, verify (§0, §6a)
│   └── in a fresh run root, same lineage and anchor                 PHYSICAL ROLL
│         seal, audit, the open trigger, start (§6a, §7 row 2)
│
└── a new state-machine version, or a lineage that must not continue   NEW ESTATE
      a final seal; a new RUN_ROOT and ESTATE_ANCHOR; a genesis (§7 row 4)
      holds, globals and latches start empty; the old lineage stays readable
```

An engine stop is not an estate stop. Detached commands outlive the
engine on purpose (runner-design §6a), so `systemctl stop
dsl41-engine.service` stops no job. A host reboot stops both units, and
with them every running command; drain before one (§8's OS-patching row).
The enabled units then resume the root at boot. An in-place transition is not a new
estate. It keeps the run root, the anchor and the state: holds, globals,
latches and run numbers cross the seal (§6a). A new estate keeps none of
them. §6's fresh run root, an infrastructure act, is a new estate too: its root gets an
anchor of its own.

### Recipe: install and start as a service

1. Prepare and explicitly activate the offline candidate (§1). The
   launcher's `DSL41` is `/opt/dsl41/.venv/bin/dsl41`.
2. Review the versioned deployment kit staged with the candidate (§1).
   The recipe below uses the matching release checkout's examples.
   Create the service account and its writable
   directories, and install the launcher, the access map and the units:

<!-- recipe: service-install -->
```sh
set -eu
useradd --system --user-group --create-home --home-dir /var/lib/dsl41 \
    --shell /usr/sbin/nologin dsl41
install -D -m 0755 examples/nightbank/deploy/dsl41-launch /opt/dsl41/bin/dsl41-launch
install -d -m 0755 /srv/dsl41
install -d -o dsl41 -g dsl41 -m 0700 /srv/dsl41/runs
mkdir -m 0700 /srv/dsl41/runs/nightbank-01
(umask 077; set -C; : >/srv/dsl41/runs/nightbank-01/leader.lock)
chown dsl41:dsl41 /srv/dsl41/runs/nightbank-01 /srv/dsl41/runs/nightbank-01/leader.lock
install -d -m 0755 /etc/dsl41
install -o dsl41 -g dsl41 -m 0600 examples/nightbank/deploy/nightbank-access.toml \
    /etc/dsl41/nightbank-access.toml
install -m 0644 examples/nightbank/deploy/dsl41-engine.service \
    examples/nightbank/deploy/dsl41-supervisor.service /etc/systemd/system/
install -m 0440 examples/nightbank/deploy/dsl41-release.sudoers /etc/sudoers.d/dsl41-release
visudo -cf /etc/sudoers.d/dsl41-release
systemctl daemon-reload
systemctl enable dsl41-supervisor.service dsl41-engine.service
```

3. The service account writes the run roots and their anchors under
   `/srv/dsl41/runs`, and whatever its jobs write. A job's data
   directories and its `std_out_file` targets must be the account's:
   nightbank's jobs write under `/srv/dsl41/nightbank/night`, which the
   drill creates owned by the account. `ESTATE_RELEASES` belongs to the
   deployment identity and its group, and a prepared release's inputs are
   read-only to everyone
   ([site-package §5](site-package.md#5-ship-jil-releases-on-the-applications-train)).
   The access map must be owned by the account and writable by no one else,
   in a directory owned by root or the account and writable by no one
   else (access-model §4).
4. Edit the installed copies, not the checkout: the launcher's
   CONFIGURATION block, and the run root in both units'
   `RequiresMountsFor=`. Run `systemctl daemon-reload` after a unit edit.
   Then prepare the first release as the deployment identity, with
   `/opt/dsl41/.venv/bin/dsl41 release prepare NAME DIR`, and write NAME to
   `ACTIVE`, a plain file the deployment identity owns. The launcher
   refuses to start without it (DL-407).
   `dsl41-launch --print` shows the command the engine unit will run. It
   names every estate file, in order, every `-p` and every run option:

<!-- recipe: service-print -->
```sh
sudo -u dsl41 /opt/dsl41/bin/dsl41-launch --print
```

5. Start:

<!-- recipe: service-start -->
```sh
systemctl start dsl41-engine.service
systemctl is-active dsl41-supervisor.service dsl41-engine.service
```

   The new root and empty lock are provisioned before either unit starts.
   A preexisting root makes the provisioning step refuse; inspect it.
   Never create a replacement lock in a running root (§3).
   `Requires=` starts the supervisor unit first, and both lines print
   `active`. That is not readiness: a unit that refuses with exit 2 a
   second later printed `active` too. The first start is a genesis: the
   launcher passes no `--resume`, and dsl41 creates the estate in the run
   root, and its anchor. Wait until the engine answers; the last line
   prints the status, or fails when no engine answered:

<!-- recipe: wait-answers -->
```sh
for try in $(seq 90); do
    dsl41 query status --brief -S "$S" >/dev/null 2>&1 && break
    sleep 1
done
dsl41 query status --brief -S "$S"
```

   Read the preflight WARNs in `journalctl -u dsl41-engine.service`.
6. `systemctl enable` makes both units start at boot, and the engine unit
   then resumes the root on every boot. That includes a sealed root: a
   boot opens its next period in place. A window that a reboot must not
   end takes §2b's reboot hold (both units disabled) and releases it at
   its end: §2b's backup and §7's rows 2 and 4. A retirement takes it for
   good. A plain host reboot takes no hold.

### Recipe: configure an estate

1. **Identities.** One service account, `dsl41`, runs both units and owns
   the run root, the anchor and the access map. People do not borrow it
   for the control socket. Each operator uses their own login, and the
   map gives each principal a tier (access-model §4): an exact `user:`
   row wins, then the highest matching `group:` row, then `unmapped`. The
   example map binds the service account at `adm`, `nightbank-ops` at
   `ops` and `nightbank-observers` at `read`, and denies everyone else.
   The commands that need the run root itself (`seal`, `audit`,
   `estate`, `supervise`) run as the service account, because the root is
   `0700`; `--claimed-actor you@host` on `seal` names you in its record,
   as a claim.
2. **Socket exposure.** With no `socket_group`, only the service account
   reaches the socket. To let people in, name a group in the map
   (`socket_group`; the example ships it commented out), create it, and
   add every person who may reach the socket. Add the service account
   too: the engine hands the run root to the group, and refuses to start
   with exit 2 (`cannot open the run root to group`) when the account is
   not a member. The engine then opens the run root to `0710` and the
   socket to `0660` for that group (access-model §8). The run roots'
   directory, `/srv/dsl41/runs` in the example, is `0700` for the
   account, so give the group execute on it too (`chgrp GROUP
   /srv/dsl41/runs; chmod 0710 /srv/dsl41/runs`); until then a member
   gets `Permission denied` on the socket. The bindings still decide each
   member's tier. Restart the engine unit after a `socket_group` change:
   a reload that names another group is refused (access-model §7). The
   web UI listens on loopback; front it with TLS and authentication (§4).
3. **Execution profile.** The runtime profile has two halves (DL-252,
   DL-407). The launcher's run options are the deployment half:
   `--detached`, `--as-machine` and `--machine-policy strict`, plus
   `--deadman` when used (§3, §5). The active release's manifest is the
   interpretation half: `--timezone`, plus `--timezone-map` and
   `--semantics` when used
   ([authoring §6.1](authoring.md#61-declare-the-estate-in-estatetoml)).
   `--as-machine` is the name the JIL's `machine:`
   values resolve to. `--timezone` is the base zone: the zone of every
   schedule that does not name its own. An estate that runs around the
   clock across regions is best run with UTC as its base zone. UTC has no
   DST changes, so no schedule in the base zone moves, is skipped or runs
   twice (DL-316). A job that names its own zone still follows that
   zone's changes: move its time out of the changing hour, or let the job
   name UTC itself (ir-design §9, L023).
   The resume gate refuses a changed profile with exit 2. Change one only
   at a boundary: a manifest change is a release
   ([the job recipe](#recipe-add-change-or-remove-a-job)), and a launcher
   change needs the matching `--next-*` options (§6a).
4. **Access check.** After a start, as a member of the socket group with
   a binding, `dsl41 query status --brief -S "$S"` answers. As a member
   with no binding it is refused, and `perimeter.jsonl` gains an
   `access_denied` record. Each start's `policy_loaded` record names the
   map's digest (access-model §6).

### Recipe: add, change or remove a job

A job changes through a complete catalog and a boundary, never through an
edit of a running estate (§6). Every estate file holds whole
definitions: the `insert_` forms and the calendar statements. Lowering
refuses the AutoSys delta forms (`update_job`, `delete_job`,
`override_job`; DL-29): `dsl41 lint` exits 2 on them, and no estate can
load them. Add a job by adding its `insert_job` block. Change one by
editing its block. Remove one by deleting its block. A job that a
remaining condition still names is a lint error (L001).

The change ships as a release on the application's train (§6; DL-407,
DL-408). `dsl41 release` prepares the release, applies it in one window
and rolls it back the same way (DL-412). Run it as the deployment
identity, `nbdeploy` in the example; the
[packager's contract](site-package.md#5-ship-jil-releases-on-the-applications-train)
gives that identity its grant. `sudo -u nbdeploy` gives no `PATH` to the
runtime, so the commands below name `/opt/dsl41/.venv/bin/dsl41`. The
verbs run their service-account part through `sudo` by the launcher's
`DSL41` path, which the grant names, whatever path you typed. Run the
window under `tmux` or `screen`, so a dropped connection does not end it.
Below, `NAME` is a release's name, one directory under `ESTATE_RELEASES`,
and `JOB` is a job's name.

1. Commit the edit to the estate's repository and tag it. If it adds or
   removes a file, edit the release's `estate.toml` to match: the launcher
   and `dsl41 release` take the file list from it (DL-407).
2. Before the window, check the new tag in a checkout of its own, with
   `ESTATE` pointing at that checkout. Fix every ERROR. The blocks take
   lint's and rehearse's `--timezone` and `--timezone-map` from the
   manifest, and rehearse's `--semantics`; lint takes no `--semantics`.
   Without a timezone, both work in UTC: rule L023 checks DST effects
   there (DL-316), and rehearse plays the schedules there. The example
   manifest sets `timezone = "UTC"` and no semantics:

<!-- recipe: job-check -->
```sh
args=$(dsl41 estate args "$ESTATE/estate.toml" --for lint --environment "$ENVIRONMENT")
eval "set -- $args"
dsl41 lint "$@" -p "$PROPERTIES"
args=$(dsl41 estate args "$ESTATE/estate.toml" --for rehearse --environment "$ENVIRONMENT")
eval "set -- $args"
dsl41 rehearse "$@" -p "$PROPERTIES"
```

   Then prepare the release from the tag's directory, `DIR` below, which
   holds `estate.toml` and which the deployment identity can read:

```sh
cd / && sudo -u nbdeploy -H /opt/dsl41/.venv/bin/dsl41 release prepare NAME DIR
```

   Prepare copies `DIR` to `ESTATE_RELEASES/NAME` and makes its inputs
   read-only. It lints the release with the installed runtime, the
   launcher's `ENVIRONMENT` and the site's `PROPERTIES`, then rehearses it
   one day from now. It refuses the release with exit 2 when lint or
   rehearse exits nonzero, when the release holds more jobs than the
   launcher's `MAX_JOBS`, or when the runtime's command grace is above
   `MAX_GRACE_S`. That ceiling is the packager's (site-package §5). A
   refused prepare installs nothing, so its name can be used again. A
   prepared name is installed once, so a fixed release takes a new name.
   After a runtime switch, apply refuses a release prepared under another
   dsl41 version until `dsl41 release prepare --requalify NAME` has run
   the same checks again. The same command checks a release installed
   by hand, where it is.
3. In the window, apply the release:

```sh
cd / && sudo -u nbdeploy -H /opt/dsl41/.venv/bin/dsl41 release apply NAME
```

   `dsl41 release apply` records each step before it acts, and takes them
   in this order:

   1. It refuses unless the engine answers and NAME's files are the ones
      prepare checked.
   2. It holds the scheduled jobs.
   3. It waits until no job is `STARTING`, `RUNNING` or `QUE_WAIT`, and
      prints the jobs it waits for once a minute.
   4. It refuses when a job the release changes or removes is still armed
      or has a pending timer. Wait until it is not, or `DISARM` it, and
      apply again. The seal itself would let a changed latent job cross
      with a recorded assumption (period-model §10.3); the apply does not.
   5. It seals from NAME's manifest, with the launcher's deployment
      options restated as `--next-*`.
   6. It writes NAME to `ACTIVE`.
   7. It waits for the sealed engine to exit, and starts the engine unit.
   8. It checks that the successor answers in the committed period, under
      a new leader, from the bundle the seal committed and NAME's files.
   9. It releases only the holds it applied itself, and keeps a hold an
      operator took or released since. It writes NAME's receipt.

   A hold the apply takes starts the retry horizon (period-model §9). The
   seal refuses inside it, so the apply waits it out and seals again under
   a new request id. It prints one line for the wait. Three options bound
   the waits, in seconds:

   | Option | Default | When it runs out |
   | --- | --- | --- |
   | `--drain-timeout` | 600 | nothing is sealed. Exit 2 when the apply has released its holds; exit 3, with the apply recorded for `resume`, when removing those holds fails |
   | `--horizon-timeout` | 300 | exit 2: the seal did not commit |
   | `--open-timeout` | 90 | exit 3: the apply stays recorded |

4. Act on the exit code. `apply`, `resume` and `rollback` print the cause
   and the next command, one line each.

   | Exit | What it means | What you do |
   | --- | --- | --- |
   | 0 | NAME is active in the next period, and every hold the apply took is released. | Verify (step 5). |
   | 1 | NAME is active, and some holds the apply took stay on. Each is printed with its reason and the `OFF_HOLD` command that releases it. | Verify (step 5). Release each printed hold once it is safe. A hold another operator changed may be theirs: agree with them first. |
   | 2 | Refused, or the seal did not commit. Nothing was sealed and `ACTIVE` is as it was. The holds the apply took are released, so a job a tick latched under one of them starts; no apply stays recorded, and NAME's `.release/` keeps a `refused-` receipt. | Fix the cause the line names, and apply again. Files that need a change go in a new release under a new name. When the line names an apply already recorded, that apply stays: run `dsl41 release status` and resume it. |
   | 3 | Stopped. An interrupt or a lost session stops it the same way. | Run `dsl41 release status` and do what it says. It shows a recorded apply and what `dsl41 release resume` will do; fix the cause, then resume. With no apply recorded, its `last apply` line says whether the last apply completed or sealed nothing. |
   | 4 | The seal's outcome is unknown. The apply is recorded with its request id, and the scheduled jobs stay held. | Run `dsl41 release resume`. It reads the estate for that request id first, and retries under it, and under a new id only after a retry-horizon refusal. Do not seal by hand. |

   A new apply, and a rollback, refuse while an apply is recorded. One
   case differs: when the new period opened and its check failed (exit 3
   at step 8), `dsl41 release resume` checks again, and
   `dsl41 release rollback` takes the apply over. It releases that
   apply's holds, writes a `rolled-back-` receipt with the problems,
   clears the record, and applies the release the apply replaced, with
   the exit codes above. `dsl41 release status` names both commands.
5. Verify. `dsl41 release status` names the active release, whether an
   engine answers, where a rollback goes, and how the last apply ended,
   with each hold it kept. An added or changed job answers with the
   definition this period loaded:

<!-- recipe: job-verify -->
```sh
dsl41 query spec -J "$JOB" -S "$S"
```

   A removed job stays as a ghost: its row and its history remain, with
   no definition (period-model §10). `query spec` answers `unknown job`,
   and the status row reads `"job_type": null`:

<!-- recipe: job-verify-removed -->
```sh
dsl41 query status --job "$JOB" -S "$S"
```

6. Roll back with the same window:

```sh
cd / && sudo -u nbdeploy -H /opt/dsl41/.venv/bin/dsl41 release rollback
```

   It applies the release that the active release's latest receipt names
   as its previous one, with the exit codes of step 4, and prints that
   target first. A second rollback therefore goes forward again: check the
   target `dsl41 release status` names before you type it. A release
   installed by hand, or applied without these verbs, has no receipt, so
   rollback has no target: apply the earlier release by name, after
   `dsl41 release prepare --requalify NAME` if it was never prepared.

A host whose runtime predates `dsl41 release` takes the same window by
hand, in these steps. The hand path checks neither the ceiling nor the
files prepare recorded.

1. Hold the scheduled jobs and let running work finish (§6, steps 1
   and 2).
2. Check each job you change or remove:
   `dsl41 query status --job "$JOB" -S "$S"` must show no `STARTING`,
   `RUNNING` or `QUE_WAIT`, `armed` false and no `pending_timers`. Wait
   out the retry horizon after the last operator request, a hold
   included (§2b).
3. Set `ESTATE` to the new release's directory, installed whole beside
   the active one, and seal. The `--next-*` options restate the
   launcher's run options; they do not inherit them (§6a):

<!-- recipe: job-seal -->
```sh
args=$(dsl41 estate args "$ESTATE/estate.toml" --for seal --environment "$ENVIRONMENT")
eval "set -- $args"
dsl41 seal --run-root "$RUN_ROOT" --estate-anchor "$ESTATE_ANCHOR" "$@" -p "$PROPERTIES" \
    --next-detached --next-as-machine localhost --next-machine-policy strict
```

   The live engine exits 3, and the engine unit stays failed on purpose.
   Exit 4 is an unknown outcome: follow §6a's Day 2 before anything else.
4. Write the new release's name to `ACTIVE`.
5. Start the engine unit. The launcher passes `--resume`, and the engine
   opens period N+1 in the same run root under the new catalog. Wait for
   it with the `wait-answers` block of the service recipe:

<!-- recipe: job-open -->
```sh
systemctl start dsl41-engine.service
```

6. Verify, as step 5 of the `dsl41 release` path does.
7. Release the holds with `OFF_HOLD`.

### Recipe: stop, restart, seal and recover

- **Engine stop and start.** `systemctl stop dsl41-engine.service`, then
  `systemctl start dsl41-engine.service`; or `systemctl restart
  dsl41-engine.service`. The launcher resumes the same root (§5).
  Detached runs keep running under the supervisor unit. Schedule ticks
  that fall while the engine is down are dropped and journaled, not fired
  late (§5). A host reboot is not an engine stop: it stops the supervisor
  unit too and ends every running command.
- **Host reboot.** Hold the scheduled jobs and let running work finish
  (§6, steps 1 and 2), then reboot. The enabled units start at boot, and
  the launcher resumes the same root. No seal, and no reboot hold.
- **Estate stop.** For a window that must outlast a reboot: take §2b's
  reboot hold, then follow §2b's shape-1 steps: stop the engine unit, then
  run `dsl41 estate quiesce` as the service account. It seals and audits
  while the supervisor unit runs, requests guarded maintenance, and checks
  that neither unit comes back and that nothing writes. A refusal names
  what to wait for; run it again. To start again, start the engine unit
  (it opens the sealed period's successor), then release the hold.
- **Seal.** Closing a period is an operator act at the estate's cutoff
  (§6a). A live seal stops the engine with exit 3. The next period opens
  when you start the engine unit, never through a restart loop.
- **Recover from a crash.** Exit 1 or a signal: the unit restarts the
  engine after `RestartSec`, and the launcher resumes the same root.
  After five failed starts in five minutes the unit stays failed
  (`StartLimitBurst=5`). Read `journalctl -u dsl41-engine.service`, fix
  the cause, run `systemctl reset-failed dsl41-engine.service` and start.
- **Recover from a refusal.** Exit 2 is a configuration refusal, and the
  unit does not retry it. The last lines of the engine unit's journal name
  the cause: a changed estate, a resume gate, an access map, a preflight
  ERROR, or the launcher's own refusal. Fix it and start the unit. A
  `resume stopped` refusal names a logged input this build cannot replay;
  §7 says what to deploy.
- **Recover from a time-half stop.** Exit 6: the timers due at an
  engine-made input raised, and the engine stopped before journaling it.
  The unit does not retry it, because every restart would stop the same
  way. The journal line `engine stopped: engine stopped before admitting
  input N` names the input and the fault. Deploy a release that fixes it
  (§7), then start the unit. The journal is clean, so the release resumes
  it.
  A refusal that names a watch's `watch.jsonl` reporting `run_id None`
  against a durable effect is an identity split on a root an earlier
  build resumed: that build launched a watch with no identity for a start
  whose decision was missing. It fires at this build's first resume while
  that run directory is present, whether the watch completed or not. The
  refused resume writes no decision, so the earlier build can still open
  the root. There is no migration: reset the root (period-model §11,
  step 6a; DL-315).
  A rolled root that refuses with `missing segment record` has
  [its own recipe](#recipe-recover-a-rolled-root-whose-opening-is-torn).
- **Recover a lost answer: replay the request.** `sendevent` and `host`
  exit 4 when the answer was lost. Re-send the same arguments with the
  `--request-id`, `--expect`, `--epoch` and `--baseline` the CLI printed,
  as the same user on the same host (§4). The engine answers from its
  first decision and applies nothing twice. A seal that exits 4 needs a
  read of the estate first (§6a, Day 2).
- **A new rerun is a different act.** `dsl41 sendevent FORCE_STARTJOB -J
  JOB -S "$S"` with a fresh request id starts a new run with the next run
  number. Never compose a fresh request id to retry a lost answer: a new
  id is a new command.
- **The supervisor unit restarted.** Its running commands ended with it,
  and a restart does not bring them back. The engine reconciles what the
  spool says (§3).

The service drill runs the configure recipe's socket exposure, access
check and refused profile change (`configure`), an engine stop, a crash
loop that the start limit ends, a replayed request and a new rerun
(`stop-recover`), a SIGKILL of the engine (`kill-engine`) and a SIGKILL
of the supervisor (`kill-supervisor`). The refusal, seal, reboot and
estate-stop bullets are the `refusal`, `sealed`, `reboot` and `quiesce`
steps'. After a SIGKILL of the supervisor the drill sees `Requires=`
restart the engine unit with it, which resumes, takes a lease and renews
it. It also drives the engine's own client through a supervisor outage
that the engine outlives. That part edits the installed units for the
drill only (`Wants=` for `Requires=` in the engine unit, `RestartSec=40`
in the supervisor unit); it is not a supported configuration.

### Recipe: recover a rolled root whose opening is torn

A physical roll writes the new root's first segment, and then moves the
lineage head from `claimed` to `open` (§6a). A crash in that write
leaves the segment empty or torn, and the head `claimed` by the new
root. In a root that holds an earlier segment, resume removes such a
segment and opens the period again. A rolled root holds no earlier
segment, so resume refuses (period-model §11's recovery matrix, its
torn-first-line row). A retry of the same roll refuses too, while the
torn file stays. The engine unit fails with exit 2, and its journal
ends with `<root>/wal/<N>.jsonl: missing segment record`. Set
`RUN_ROOT` and `S` to the torn root for steps 1 to 5.

1. Look before you stop anything. A stop of the supervisor unit ends
   every command it runs. This prints the head, and stops when the
   supervisor still lists a run:

<!-- recipe: torn-look -->
```sh
(
    set -eu
    jq -c .head "$ESTATE_ANCHOR/anchor.json"
    if [ -e "$RUN_ROOT/supervisor.sock" ]; then
        dsl41 supervise list --run-root "$RUN_ROOT" | jq -e '.runs == []' >/dev/null ||
            { echo "stop: the supervisor lists runs, or does not answer" >&2; exit 1; }
    fi
)
```

   If it stops, something runs under this root. This recipe does not
   cover that case, and no other supported path does.
2. Take §2b's reboot hold and stop the engine. Under shape 1, use §3's
   guarded maintenance as the service account against `"$RUN_ROOT"`.
   Require exit 0; a manager stop of the protected supervisor unit refuses.
   On refusal or uncertainty, preserve evidence and follow §3's explicit
   incident procedure. Under shape 2, guarded maintenance refuses; an
   incident stop needs an independently verified whole-process scope.
   Continue only after §2b's no-writers check, including its wait past
   `RestartSec` under shape 1, proves quiescence. An empty LIST alone is
   insufficient. Do not create or replace lock evidence to force admission.
3. Check the root. The check stops, and says why, at any sign that
   the root holds more than a torn opening:

<!-- recipe: torn-check -->
```sh
(
    set -eu
    stop() { echo "stop: $*" >&2; exit 1; }
    head=$ESTATE_ANCHOR/anchor.json
    here=$(cd "$RUN_ROOT" && pwd -P)
    state=$(jq -r .head.state "$head")
    case $state in
        claimed)
            [ "$(jq -r '.head["target_root"]' "$head")" = "$here" ] ||
                stop "the head's claim is not this root's"
            id=$(jq -r .head.claim_id "$head")
            [ -f "$ESTATE_ANCHOR/claims/${id##*:}.json" ] || stop "the claim file is missing"
            [ "$(jq -r .claim_id "$RUN_ROOT/journal.jsonl")" = "$id" ] ||
                stop "the head's claim did not open this root: it is not a rolled root"
            ;;
        open)
            [ "$(jq -r .head.root "$head")" = "$here" ] || stop "the head names another root"
            [ "$(jq -r .claim_id "$RUN_ROOT/journal.jsonl")" != null ] ||
                stop "no roll created this root: it is not a rolled root"
            ;;
        *) stop "the head is $state" ;;
    esac
    segment=$(ls -A "$RUN_ROOT/wal")
    case $segment in
        [0-9][0-9][0-9][0-9][0-9][0-9].jsonl) ;;
        *) stop "wal/ holds something other than one segment" ;;
    esac
    [ "$(wc -l <"$RUN_ROOT/wal/$segment")" -eq 0 ] || stop "the segment holds a complete line"
    [ -z "$(ls -A "$RUN_ROOT/runs" 2>/dev/null)" ] || stop "runs/ holds run evidence"
    [ ! -e "$RUN_ROOT/seals/${segment%.jsonl}.json" ] || stop "the period has a seal"
    [ ! -e "$RUN_ROOT/supervisor.pid" ] || stop "a supervisor may still run"
    echo "$state $segment"
)
```

   The check prints the head's state and the segment. Only a physical
   roll writes a `claim_id` into a root's sentinel. A genesis writes
   none, and an in-place opening leaves the sentinel unchanged. So that
   field tells a rolled root from any other, for either head. `claimed`
   is the crash: nothing ran in the root, because its only segment never
   held a complete record. Go on with step 4. `open` is damage: go to
   step 6.
4. With both units still stopped, remove the torn segment and create
   the launcher's one-shot open trigger, in one step, as the service
   account. Remove nothing else: not `leader.lock`, not the sentinel,
   and nothing in the anchor. Resume removes the same file itself when
   an earlier segment exists (period-model §11), and the opening that
   replaces it is a pure function of the seal:

<!-- recipe: torn-reopen -->
```sh
rm "$RUN_ROOT"/wal/[0-9][0-9][0-9][0-9][0-9][0-9].jsonl && touch "$RUN_ROOT.open-from"
```

   Do the two together. A start with the segment gone and no trigger
   resumes instead, and that resume fails with exit 1, so the unit
   restarts it in a loop.
5. Start the engine unit (`systemctl start dsl41-engine.service`), and
   wait for it with the `wait-answers` block. The trigger reruns the
   identical opener, and it resumes its own claim in this root
   (period-model §1.3). The engine's journal says `opened period N in
   <root>`. Nothing is reclaimed, and the opening carries no
   `reclaimed` stamp. If the start refuses for another cause, fix the
   cause if you can, create the trigger again (`touch
   "$RUN_ROOT.open-from"`), and start the unit. The launcher removes the
   trigger before it runs dsl41. Without it, the start resumes a root
   with no segment, which fails with exit 1, and the unit restarts it in
   a loop. If you cannot fix the cause, free the claim with the
   break-glass of §6a. The code does not repair a torn sole opening (DL-144 (9a)), so
   a claim it cannot finish is freed only with `--force`. The reclaim
   records the account that ran it, the service account under §0. Add
   `--claimed-actor you@host` to name yourself:

<!-- recipe: torn-reclaim -->
```sh
dsl41 estate reclaim --estate-anchor "$ESTATE_ANCHOR" --force
```

   Then open the period in a fresh root, as in step 7. The reclaimed
   root stays as it is.
6. `open` head: the segment was durable when the head moved, and was
   damaged later. No verb path exists for this case. Step 3 found no
   run evidence, so a restore is allowed. Restore the anchor at its
   recorded path from a §2b copy taken after the closing period's seal
   and audit, and before the roll. Restore the closing root too, but
   only if it changed since that copy. The restored head reads
   `closed`. Without such a copy, nothing recovers this case. What
   this does not prove: the fault that cut a durable first line can
   have cut other durable evidence too. Step 3 reads what is left. It is
   the operator's evidence that nothing ran, not proof. Leave the
   damaged root as it is, for that evidence.
7. Open the period in a fresh root. Edit the launcher's `RUN_ROOT` line
   and both units' `RequiresMountsFor=`, and run `systemctl
   daemon-reload`. Set `RUN_ROOT` and `S` in your shell to the new
   root. `ESTATE_ANCHOR` stays the lineage's. Create the trigger, as
   the service account, then start the engine unit and wait for it as
   in step 5:

<!-- recipe: torn-open -->
```sh
touch "$RUN_ROOT.open-from"
```

If the roll ran under §7's row 2, finish that row's tail once the engine
answers: release the holds with `OFF_HOLD`, and release the reboot hold
(§2b). After steps 6 or 7 the anchor's registry does not name the old
root, so no estate-wide reader reads it, and resume refuses it by
period-model §1.3's resume rule (DL-224). Remove it when the site no
longer wants it.

### What to watch

Every signal below is an existing command or file. There is no metrics
endpoint; feed these into the site's monitoring.

| Signal | Read it with | What it means |
| --- | --- | --- |
| engine unit | `systemctl show -p ActiveState -p ExecMainStatus -p NRestarts dsl41-engine.service` | `active` runs. `failed` with status 2 is a configuration refusal; the journal names it. `failed` with status 3 is a sealed period (next row); it lasts until the next start, and an enabled unit starts at boot. `failed` with status 6 is a time-half stop (§3); the journal says `engine stopped:` and names the input and the fault. `failed` with status 7 is a statechart error (§3; next rows). A rising `NRestarts` is crash restarts (exit 1); the journal says `engine failed:` and why |
| sealed, not opened | the check below, on `ESTATE_ANCHOR/anchor.json` | `closed`: a period is sealed and the next is not open, so nothing runs. Alert when it stays `closed` past the window. `open` is normal. `claimed` is an opener at work, or one that crashed (§6a, Day 2) |
| supervisor unit | `systemctl show -p ActiveState -p NRestarts dsl41-supervisor.service`; `dsl41 supervise list --run-root "$RUN_ROOT"` | the list answers `"ok": true` while the supervisor is up, and shows each run's `wrapper_alive`. A restart of this unit ended the commands it ran. `failed` with status 7 is a statechart error (§3) |
| supervisor log | `$RUN_ROOT/supervisor.log`; `journalctl -u dsl41-supervisor.service` | the supervisor's own output, and the unit's starts and stops |
| leader | `$RUN_ROOT/leader.lock` holds the last leader's `pid`, `host`, `epoch` and `since`; compare `pid` with `systemctl show -p MainPID --value dsl41-engine.service` | the note stays after the engine exits, so it says who led, not who leads. Never probe the lock with `flock`: an engine that starts while a probe holds it refuses with exit 2. Never delete or replace the file: the engine re-checks it before every append and stops when it changed |
| control socket | `dsl41 query status --brief -S "$S"` | exit 0: the leader answers. Exit 2: no engine, or a refusal |
| failures and alarms | `dsl41 query subscribe -S "$S"` as the wake-up; then `dsl41 query trace --since N -S "$S"` with the last `last_seq` read. The trace is per period and its `seq` restarts at 1: set the cursor to 0 when the answer's `baseline_id` changes, or when `last_seq` is below the cursor, as the TUI does (DL-210) | the stream carries journal records: a run's end arrives as an `input` record of kind `STATUS` from the `adapter` source. The trace names what it did: a transition to `FAILURE` or `TERMINATED`, or a `MUST_START_ALARM` or `MUST_COMPLETE_ALARM` entry (SEM-34). A trace an older build wrote may carry a `TRANSITION_VIOLATION` entry, a move that broke a declared transition table. The current build writes none (DL-353), and the TUI shows an old one as an alert |
| quarantined inputs | `journalctl -u dsl41-engine.service`; `dsl41 query subscribe -S "$S"`; `dsl41 journal "$RUN_ROOT"` | alert on any journal line that begins `dsl41: input quarantined`, and on a `decision` record with `"code": "engine_apply_faulted"` on the stream. Applying an engine-made input raised, so the engine logged it rejected and applied only its time half. The line names the input's kind, job, run and the fault. The input's own effect is lost: a quarantined completion leaves its run RUNNING, and a quarantined tick is a missed start. `dsl41 journal` prints a `rejected input` line for it. Report the fault, then settle the row with `sendevent` (concurrency-model §4) |
| statechart errors | `journalctl -u dsl41-engine.service` for the engine; `$RUN_ROOT/supervisor.log` for the supervisor, whose stderr `supervise start` points there | alert on any line that begins `dsl41: statechart error`. A declared state machine hit an assertion failure: two enabled rows, an event nobody handles, or a call into its executor from inside a step. The tier stopped at once and exits 7. A durable write already made stays, such as a `seal` record (period-model §7). The lines name the machine, the instance, its states, the item, its ancestry, the candidates, the deferred queue and the last microsteps; report them whole (docs/statechart-engine.md §5). A supervisor that exits 7 ends every command it ran, by lifeline EOF, as any stop of the supervisor unit does (§0), and stays down |
| free space | `df -P "$RUN_ROOT" "$ESTATE_ANCHOR"`, and every file system a job's `std_out_file` or `std_err_file` writes to | see "when a write fails" below |
| perimeter receipts | `$RUN_ROOT/perimeter.jsonl`, when the access map is armed | alert on `access_denied` and `policy_reload_failed` records. `stream_revoked` marks a stream that a reload closed (access-model §6, §7) |

The service drill (`watch`) runs the command of every row above and
checks the output against the row. `configure` reads the perimeter's
receipts, `sealed` the `closed` state, and `kill-supervisor` the
supervisor unit's journal. It does not reach these meanings:
exit 6; exit 7; a quarantined input; a
rising `NRestarts` from exit 1 with the `engine failed:` line (the drill's
crash is a signal, status 9); the `claimed` state; the
`MUST_START_ALARM` and `MUST_COMPLETE_ALARM` entries; a `TERMINATED` run
read from the trace; `stream_revoked`; the trace cursor reset when
`baseline_id` changes; the free space of a job's output file systems; and
a subscribe line over the budget.

**A row left live with nothing behind it.** After a restart, a job can
stand RUNNING or STARTING with no live run and nothing held: `dsl41 query
status` shows the row with `"held": false`, and no process or watch runs
for it. That is the state the engine was in before the restart, when the
run's start was retired and an operator then set the row live, for
example KILLJOB on a held start followed by `CHANGE_STATUS RUNNING`. A
restart launches nothing for such a run and does not fail it, a tethered
command included (runner-design §7, DL-315). End it with KILLJOB
or `CHANGE_STATUS`, or start a new run with FORCE_STARTJOB.

The sealed-not-opened check prints the lineage head's state. It needs
`jq` on the host (`apt-get install jq`); no dsl41 command prints the
head's state:

<!-- recipe: watch-sealed -->
```sh
jq -r .head.state "$ESTATE_ANCHOR/anchor.json"
```

**Keep the subscriber reading.** A `subscribe` client that stops reading
is removed once its backlog reaches the engine's budget, 64 MiB
(control-protocol §5, DL-267). Read the stream continuously, so a
burst does not remove a reader that is only slow. `dsl41 query subscribe`
exits 2 in three cases, and a monitoring wrapper handles each one:

- The stream ended after the ack: a removal, an engine stop or a
  backfill refusal. The command names the `--since` to resubscribe with
  on stderr. Restart it with that cursor. Nothing is lost: the backfill
  and gap rules of control-protocol §5 apply.
- The request was refused before the ack, or no engine answered. The
  stream never started, so the command names no `--since`. Keep the
  cursor the wrapper already had: the `--since` named at the last end,
  or none on a first start. Retry with it after a delay. A refusal names
  its cause on stderr; an access denial needs an operator, not a retry.
- A stream line was over the budget (`record line over the … limit`).
  The command names no `--since`, because the same cursor meets the same
  record again. Do not retry at that cursor; alert. The stream is only
  the wake-up, and the trace cursor in the table above is the record of
  transitions, so a restart with no `--since`, from the live frontier,
  misses no transition the trace reads.

**When a write fails.** These are the contracts' rules for a full disk or
an I/O error:

- The WAL. The engine applies an input only after its record is appended
  and fsynced (runner-design §7). If the append or the fsync fails, the
  engine does not apply the input, says `engine failed:` and exits 1. The
  unit restarts it, and resume reads the WAL as it is: it cuts a torn
  final line, and replays a complete one. A restart that cannot write
  fails the same way, and the start limit then leaves the unit failed.
- A seal. A failure before the `seal` record aborts the boundary: the
  period stays open and `dsl41 seal` exits 2. A failure on the `seal`
  record itself is an unknown outcome: the engine fail-stops, the seal
  exits 4, and recovery decides from the WAL (period-model §7; §6a, Day 2).
- A wrapper that cannot write its status record exits 3 (supervisor-protocol
  §4). The run's exit status can then be unobservable.
- Perimeter receipts. An `access_denied` receipt that cannot be written
  still denies. A `privileged_admitted` receipt is best effort, and the
  admission stands. A map whose `policy_loaded` receipt cannot be written
  does not arm (access-model §6).

### Capacity and limits

This section states the size dsl41 was measured at (DL-317), and what that
size costs. All of it was measured on one macOS host (Apple M3 Pro, Python
3.14); Linux is not measured. The compressed figures come from build
55541f3, measured on 2026-10-10, which includes DL-310. The real-clock
figures come from build b6b8a55, measured on 2026-10-09 and 2026-10-10.
`examples/soak/` holds the estate and the scripts, and its README says how
to repeat each measurement.

**The tested size.** 200 jobs on one machine whose `max_load` admits 20
runs at once. In a day the estate starts about 980 jobs: 900 command runs
and 78 box runs. 30 runs fail. At the 02:00 batch, 19 runs wait in
`QUE_WAIT`, and never more than 20 run at once. The commands are cheap
(`sleep`, `true`, `false`) and print nothing.

**Two kinds of run.** The compressed run plays the estate under a virtual
clock with a fake adapter: no processes, no supervisor and no spool. Its
CPU figures are the cost of the engine's decisions, not a real day's CPU.
The real-clock soak runs the estate with `dsl41 run --detached` and real
commands. Its CPU and disk figures are the real ones.

**Sealed every day.** The compressed run plays seven days and seals at each
midnight. Each day then costs the same:

| Per day | Measured |
| --- | --- |
| decision CPU, virtual clock | 0.46 s |
| engine resident set | 70 MiB |
| the day's WAL segment | 1.2 MB |
| oracle trace | 3,500 entries |
| decisions and outbox entries | 2,000 and 900 |
| open the next period, in process | 0.25 s |
| seal, in process | 0.25 s |
| audit, as a command | 1.0 s |
| one `trace` call past the end, one `status` call, in process | under 0.1 ms, 1.8 ms |

"In process" excludes process start. The `dsl41` command itself takes about
0.2 s to start on this host, so an opening through `dsl41 run --resume`
costs that much more. The audit was timed as a command and includes it.

A seal resets the trace, the decision index, the outbox and the engine's
three lists. A live seal stops the engine with exit 3, and the next period
starts in a new process. The soak line counts the three lists under the
keys `drops`, `deduped` and `refusals`. `drops` stayed empty: it counts rejected engine inputs, such as a stale
completion or a quarantined input. `deduped` and `refusals` count operator
traffic, not launches. A retried request adds one to `deduped`, and a
refused request adds one to `refusals` (the engine's `refused_inputs`). A
restart empties both, because the WAL does not hold them.

**One long period.** The same estate, with no seal for 30 days. The WAL row
is the open period's segment, which is the whole `wal/` here because there
is only one period:

| End of day | 1 | 7 | 30 |
| --- | --- | --- | --- |
| oracle trace, entries | 3,402 | 24,432 | 105,047 |
| decisions | 1,978 | 13,882 | 59,514 |
| outbox | 900 | 6,318 | 27,087 |
| open period's WAL segment | 1.2 MB | 8.6 MB | 37 MB |
| engine resident set | 69 MiB | 132 MiB | 556 MiB (peak 610) |
| decision CPU in that day, virtual clock | 0.45 s | 0.6 s | 1.4 s |
| one `trace` call past the end, in process | under 0.1 ms | under 0.1 ms | under 0.1 ms |
| one `trace` call from the start, in process | 6.4 ms | 68 ms | 0.31 s |
| replay on a restart, in process | 0.34 s | 2.0 s | 8.6 s |
| resident set during the replay | 82 MiB | 197 MiB | 655 MiB |
| seal, in process | 0.25 s | 0.33 s | 0.74 s |
| audit, as a command | 0.72 s | 2.4 s | 9.4 s |

The running peak includes the harness's own `trace` call from the start of
the period, once a day, which builds the whole trace as one answer. A TUI's
first connect does the same.

**The real clock.** Both real-clock runs used build b6b8a55, before
DL-310. At a day's age DL-310 saves about 0.15 s a day of planning and at
most about 2 s a day of the sampler's own `trace` calls, so the real-clock
CPU below is up to about 7% high for a build that includes DL-310. The
other real-clock figures do not depend on it. The first soak ran for 30
minutes from 14:21 UTC, a quiet part of the estate's day, and started 11
runs:

| 30 minutes, real clock | Measured |
| --- | --- |
| engine CPU, including its start | 0.31 s at start, 0.61 s at the end |
| engine resident set | 48 to 61 MiB |
| engine open files | 16 rows of `lsof -p`, which counts the working directory and mapped files besides descriptors |
| supervisor resident set | 21 to 31 MiB |
| one `status` call at the socket, from `sample.py` | 1.1 to 1.8 ms |
| live seal, then audit, as commands | 0.48 s, 0.44 s |
| run directory per command run | 5 spool files (20 KiB on disk) and 2 log files |

**A day on the real clock.** The 24-hour soak ran on the same host from
2026-10-09 15:01 UTC to 2026-10-10 16:30 UTC and started 717 command runs.
The host slept twice, for about 1.5 h and 9 h, so the engine was awake for
14.9 h. Each rate per day below is awake time scaled to 24 h.

| 24 hours, real clock | Measured | From |
| --- | --- | --- |
| engine CPU | 33 s a day (20.7 s in 14.9 h awake) | samples |
| supervisor CPU | 5.4 s a day | samples |
| engine resident set | 61 MiB at start, 57 MiB at the end, never above 61 MiB | samples |
| supervisor resident set | 31 MiB at start, 10 MiB at the end, never above 31 MiB | samples |
| engine open files | 16 `lsof` rows from start to end | samples |
| one `status` call at the socket | 2.4 ms median, 7.6 ms at the 99th percentile | samples |
| WAL of the one period | 0.98 MB for 717 runs | `engine/wal/` |
| run directory per command run | 7 files, 20 KiB on disk, 1.1 KB of content | `engine/runs/`, `engine/logs/` |
| live seal, engine exit, audit, as commands | 0.56 s, 1.6 s, 0.90 s | `stop.jsonl` |

The engine CPU includes the sampler's two control calls a minute. macOS
takes resident pages back while the host sleeps: just after the 9-hour
sleep the engine read 11 MiB and the supervisor 7 MiB, so the low values
are not use. The samples counted `lsof` rows, which include the working
directory and mapped files; `sample.py` counts descriptors only. A
constant count still shows that no descriptor leaked.

**A host suspend.** The host slept from 00:29 to 09:30 UTC, and the engine
process slept with it. No restart happened, so nothing was resumed. At the
wake the engine fired every tick that fell in the sleep: about 185 ticks of
some 66 jobs, each stamped at its own due time, and all applied. None was
dropped.
That is the rule for a live engine that stalls (E9, runner-design §15); a
restart drops missed ticks instead. Each of those jobs started once,
however many of its ticks fell in the sleep. About 115 runs started in the
first 10 minutes. 59 runs waited in `QUE_WAIT` at the wake, and 4 after 13
minutes. One `trace` call that began at the wake took 14 s; the next took
11 ms. The engine went on scheduling until the stop. The two short wakes
before it, at 17:48 and 18:32 UTC, also started runs. To drop the missed
ticks instead of running them late, stop the engine before a planned pause
of the host and resume it after.

**Disk.** Each command run leaves a spool directory under `runs/` with four
files, an entry in `runs/.by_run_id`, and its two log files. The 24-hour
run's 717 runs used 14 MiB on disk, 20 KiB a run. At 900 runs a day that is
about 6,300 files and 18 MiB a day. The logs grow by whatever the commands
print; the soak's commands print nothing. The WAL adds about 1.2 MB a day.
Nothing removes any of it until an attested period is pruned (§2a:
`dsl41 estate prune --tombstones`).

**The limits that follow.**

- Within a period, the trace, the decision index and the outbox grow by
  about 3,500, 2,000 and 900 entries a day, and nothing bounds them. A seal
  is the only reset. The seal cadence stays the estate's choice
  (E16, runner-design §15); the tables give its cost at this size.
- Planning an input's effects does not read the whole outbox (DL-310).
  The decision CPU of a day still grows with the age of the period, from
  0.45 s on day 1 to 0.6 s on day 7 and 1.4 s on day 30: about 0.7 ms per
  input on day 30, under the virtual clock. The largest remaining term is
  the dispatch step's scan of every effect the period recorded.
- A 30-day period at this size needs about 0.65 GiB for a restart (the
  replay peak, 655 MiB) and up to 0.6 GiB while it runs. That was measured on
  macOS with the fake adapter. Linux, a real-clock 30-day period, a longer
  period and a busier estate are not measured.
- A restart costs one replay of the open period, and only the open period:
  0.34 s after a day, 2 s after a week, 9 s after 30 days, plus the
  process start. The replay needs more memory than the running engine.
- A `trace` call copies only the entries after its `since` (DL-310), so
  the TUI's poll every 2 s costs what it returns: under 0.1 ms on day 30.
  A call from the start of the period, as a TUI's first connect makes,
  still builds the whole trace: 68 ms on day 7 and 0.31 s on day 30.
- Disk grows with every run until a prune. At this size the run directory
  takes about fifteen times the WAL's space.

**What to watch for capacity.** `examples/soak/sample.py` reads the first
three once a minute, and times the `status` call at the socket:

- The engine's resident set: `ps -o rss= -p "$(systemctl show -p MainPID
  --value dsl41-engine.service)"`. It should stay near its first day's size
  under a daily seal.
- The open period's WAL segment, the highest-numbered file:
  `ls "$RUN_ROOT"/wal/*.jsonl | tail -1 | xargs du -k`. Its size predicts
  the replay time. The whole `wal/` keeps every period's segment until a
  prune, so it is a retention figure (§2a), not a replay figure.
- The trace length: `last_seq` in the answer to `dsl41 query trace --since
  N -S "$S"`. It starts at 1 in each period.
- The run directory: `du -sk "$RUN_ROOT"/runs "$RUN_ROOT"/logs` and
  `find "$RUN_ROOT"/runs | wc -l`. Prune attested periods by §2a.
- The time of one `dsl41 query status --brief -S "$S"`. On the tested host
  it takes about 0.2 s, almost all of it the command's start; the engine's
  part is about 2 ms in process. A time well above 0.2 s means the engine
  is busy.

### Retiring an estate

There is no retire verb. Retiring ends a lineage: nothing will open its
next period. The procedure keeps the history by default; what to delete,
and when, is the site's retention decision (§2a).

1. Take §2b's reboot hold (`hold-down`: both units disabled), so a reboot
   inside the procedure starts nothing. The units keep running. A
   retirement never releases it.

2. Hold the scheduled jobs, and let running work finish or end it (§6,
   steps 1 and 2).
3. Seal, audit and stop, as §2b's shape-1 steps do. The final seal
   commits a successor that nobody opens; it is the lineage's closing
   record. The audit attests the last period.
4. Remove the estate's units, so that a stray `systemctl start` finds no
   unit. A start of the old units would resume the retired root and open
   its successor. Keep copies of the units, the launcher and the access
   map with the estate's other deployment inputs:

<!-- recipe: retire-remove -->
```sh
retained=/srv/dsl41/retained/$(basename "$RUN_ROOT")
install -d -m 0755 /srv/dsl41/retained
install -d -m 0700 "$retained"
cp -p /etc/systemd/system/dsl41-engine.service /etc/systemd/system/dsl41-supervisor.service \
    /opt/dsl41/bin/dsl41-launch /etc/dsl41/nightbank-access.toml "$retained"/
systemctl reset-failed dsl41-engine.service || :
systemctl reset-failed dsl41-supervisor.service || :
rm /etc/systemd/system/dsl41-engine.service /etc/systemd/system/dsl41-supervisor.service
```

   The launcher left in `/opt/dsl41/bin` starts nothing without a unit. A
   later estate on this host installs its own launcher and units (the
   service recipe).
5. Check that the estate stays stopped. These are read-only queries after
   the reload:

<!-- recipe: retire-check -->
```sh
systemctl daemon-reload
systemctl show -p LoadState -p ActiveState dsl41-engine.service
systemctl show -p LoadState -p ActiveState dsl41-supervisor.service
```

   Each unit prints `LoadState=not-found` and `ActiveState=inactive`:
   `reset-failed` above cleared a `failed` state, which a live seal's exit
   3 leaves on the engine unit and which would otherwise outlive the
   unit's file. It refuses a unit systemd has already unloaded, which has
   no failed state to clear; the `|| :` lets that pass. Then
   run §2b's no-writers check. With a dedicated service account,
   `pgrep -u dsl41` prints nothing. Drop the estate from monitoring: its
   head stays `closed` for good.
6. Keep, under the site's retention decision: every run root the anchor's
   registry names, the anchor, the deployment inputs (the estate's tag,
   the properties, the access map and any timezone map, and the copies
   above), and a venv of the state-machine version the periods ran (§7,
   row 4). The retained history still audits:

<!-- recipe: retire-audit -->
```sh
dsl41 audit --estate-anchor "$ESTATE_ANCHOR"
```

7. Delete only whole sets: one lineage's anchor together with every root
   its registry names, and only when no retained anchor names any of those
   roots. First list the roots each anchor on the host names. The command
   is read-only and takes no lock; run it once per anchor, with a venv of
   that lineage's state-machine version:

<!-- recipe: retire-list -->
```sh
dsl41 estate prune --estate-anchor "$ESTATE_ANCHOR" --dry-run |
    sed -n '/^roots planned/,/^would remove/p'
```

   A root on a kept anchor's list is not yours to delete. Deleting one
   would make every estate-wide reader of that lineage refuse it as a
   missing registered root (§2b). A `claimed` head also names its target
   root; finish or reclaim the claim first (§6a).

Who cleans what:

| What | Where | Owner |
| --- | --- | --- |
| default job logs | `RUN_ROOT/logs/<job>.<run>.out` and `.err` | dsl41: `dsl41 estate prune --tombstones` removes them with their run (§2a) |
| external job output | a job's `std_out_file` and `std_err_file` | the site's log rotation; dsl41 never touches them. Rotate between runs: a run appends |
| maintenance record | `RUN_ROOT/maintenance.json` | the supervisor. Each maintenance admission replaces it and nothing deletes it; it goes with its root (DL-401) |
| supervisor log | `RUN_ROOT/supervisor.log` | the site. The supervisor appends to it and nothing in dsl41 reads it. Rotate with copy and truncate, or with the supervisor unit stopped |
| perimeter journal | `RUN_ROOT/perimeter.jsonl` | nobody on its own. Never truncate or rotate `perimeter.jsonl` on its own: truncating it restarts `access_seq`, and later receipts reuse identities that already exist (access-model §6). It goes only with its whole root |
| system journal | journald, for both units | the site's journald retention. It holds each start's launcher line and the engine's output |
| run roots and anchors | `/srv/dsl41/runs` | the site's retention decision, applied to whole sets (above); `estate prune` for what §2a licenses inside a root |


## 1. Install

Use the [offline installation recipe](offline-installation.md) (DL-389).
The release kit contains the exact DSL41 wheel, locked base and UI wheel
closures, hashes, target provenance and versioned deployment templates.
Base is the default. Use UI on a host that serves terminal or browser users.

The connected build host fetches the inputs. The deployment host needs
system Python 3.12 with venv and ensurepip, plus the OS prerequisites listed
in that recipe. Preparation runs without networking and creates each venv
at its final absolute path. Its scripts must never be relocated.

```sh
prefix=/opt/dsl41
bundle=/srv/releases/dsl41-wheelhouse
PYTHON=python3.12 bash "$bundle/tools/prepare.sh" "$bundle" "$prefix" base
```

Read the final output line for the candidate path. A completed candidate
has `.dsl41/receipt.json`; package presence alone proves nothing.
An identical repeat verifies the candidate. An incomplete or conflicting
candidate refuses with recovery instructions. Never repair it in place or
install through `.venv`. Preparation changes no active link, launcher,
unit or site configuration and restarts nothing.

The reference RPM invokes this same recipe in `%post`. Its prefix is a
build macro, defaulting to `/opt/dsl41`. RPM owns delivery files only.
Generated candidates and the active link survive package upgrades and
erasure. A failed scriptlet does not roll back its effects; inspect its
candidate and receipt before deciding that it is ready.

Activation is explicit even on first installation. After the relevant
compatibility procedure in §7, set `candidate` to the verified final path:

```sh
set -eu
prefix=/opt/dsl41
profile=base
prepared=$(PYTHON=python3.12 bash "$bundle/tools/prepare.sh" "$bundle" "$prefix" "$profile")
candidate=$(printf '%s\n' "$prepared" | tail -n 1)
test -f "$candidate/.dsl41/receipt.json"
test ! -e "$prefix/.venv" || test -L "$prefix/.venv"
link="$prefix/.venv.next.$$"
ln -s ".venvs/${candidate##*/}" "$link"
mv -T "$link" "$prefix/.venv"
export PATH="$prefix/.venv/bin:$PATH"
```

Set `profile` to the candidate's original profile before verification.
Operator shells need the active runtime's `bin` directory on `PATH`.
The service launcher uses the absolute path.
The rename replaces one link atomically. A real directory at `.venv`
refuses. Keep the previous candidate and its release kit for rollback
and retained-estate audit. Copy staged templates into active locations
only after reviewing site changes. Run `systemctl daemon-reload` after
unit changes; preparation and RPM scriptlets do not do that for you.

The package has no runtime network dependencies. It writes into the run
roots and sibling anchors you name (§2), plus the job output paths in JIL.
The UI dependency floors are tested pairs; do not force older versions.

## 2. Filesystem layout

```
/opt/dsl41/.venvs/<runtime-id>/    one pinned install per release (§7)
/opt/dsl41/.venv         the explicitly activated symlink (§1)
/srv/dsl41/nightbank/releases/<name>/
                          one release: its JIL, properties layers and
                          estate.toml, prepared by dsl41 release and
                          never changed (§0's job recipe)
/srv/dsl41/nightbank/active
                          the plain file that names the active release
/srv/dsl41/runs/<id>/     a run root holds one period per baseline and
                          gains another at every in-place boundary; a
                          lineage spans several roots once it has rolled.
                          A fresh root is genesis or a physical roll
                          (§6, §6a)
```

`dsl41 run` creates the run root, and the root is self-contained. It
holds:

- `journal.jsonl`, a one-line sentinel (DL-133, DL-134);
- `wal/<segment_no>.jsonl`, the records, one segment per period;
- `catalogs/<source_bundle_hash>/`: the post-placeholder JIL the period
  loaded, and `sources.json` with the original paths and the sha256 of
  the stored text. When `-p` resolved a placeholder, that sha256 is not
  the checksum of the file you passed;
- `periods/<period>/manifest.json`: the catalog hash and its version, the
  bundle address, the runtime profile and its hash, the state-machine
  version, and the period's own `baseline_id` and `first_index`;
- `seals/<period>.json` and `seals/<period>.audit.json`;
- `runs/` and `logs/`;
- `control.sock`, and `supervisor.sock` when detached.

The lineage anchor is outside the root, at `<run-root>.anchor` by
default, so a `tar` of a root does not carry the fence with it. Back up
both. A run root with `manifest/` (DL-66) in place of `catalogs/` and
`periods/` is a retired layout (DL-138). It is refused by name and not
read, and no path leads from such a root into a lineage
(`docs/protocol-evolution.md`).

Run roots are `0700`. Journals and job output are created `0600`: the WAL
carries globals and every control input, so keep the service account's
home to itself. `0600` is a create mode. A `std_out_file` that already
exists keeps its mode, because a run appends to it, as AutoSys does. One
thing widens the root: an armed access map that names a socket group makes
every direct child owner-only and opens the root itself to `0710`
traversal (§4). A run root is the audit record of its night. Retention is
a business decision, not a cleanup script's (§2a).

## 2a. Retention — the floors, and the prune verb

An estate root (DL-135; period-model §11a and §12) grows. It gains one
WAL segment per period, one spool directory per dispatched CMD or FW run
(a box gets none), a `runs/.by_run_id/<run_id>` entry for each detached
CMD run, and logs beside them. Nothing removes any of it on a timer.

**What you keep is your decision. What you may never delete is not.** The
model states a floor, and the floor is everything reachable from the
lineage head:

| kept, always | why |
|---|---|
| `journal.jsonl` (the sentinel) | the one file that says this directory belongs to a lineage. Without it an older binary reads the root as unused and starts a second estate beside the live one |
| the anchor directory — `anchor.json`, `anchor.lock`, and the claim a `claimed` head names | the lineage head and the lock that fences it. A crashed opener resumes through its claim |
| the sidecar the current period opened from, and the one it will close with | recovery selects its seal by lineage and refuses without the sidecar |
| the current period's manifest, and the next one its seal committed | the pins the next opening reads first |
| an uncommitted candidate's `staged_manifest.json` and `candidate.json` | recovery after an install-before-seal crash is decided by exactly those two files |
| the catalog bundles and `sources.json` those manifests name | recovery refuses without a catalog directory |
| the newest attestation, and any after it | the chain checkpoint every later proof stands on |
| an archived period's receipt, attestation and sidecar | Its inputs are gone by policy (DL-144). Delete the receipt and the absence reads as accidental loss, and every reader refuses. Delete either of the other two and the period has neither inputs nor proof |
| the WAL and spool of any unattested period | its `audit` has not run yet, and this is what `audit` reads |
| the spool of any live or carried execution | the run is not over |
| a SPAWN tombstone whose effect can still be replayed | "no index entry" means "first application", so deleting one authorizes a second spawn of a job that already ran |

A backup of a root includes the anchor. The anchor is a sibling of the
root, not inside it, so `tar czf root.tgz /srv/dsl41/runs/<id>` takes the
estate and leaves the fence behind.

**Outside the floor, and outside the verb** (DL-268). Three kinds of file
are not lineage evidence, and `estate prune` never removes them.
`perimeter.jsonl` holds the access map's receipts (access-model §6).
Never truncate or rotate `perimeter.jsonl` on its own: truncating it in
place restarts `access_seq`, and later receipts then reuse identities that
already exist. It goes only with its whole root. `supervisor.log` is the
supervisor's own output, and the site rotates it. A job's `std_out_file`
and `std_err_file` are the site's as well. §0's cleanup table names the
owner of each.

**The verb.**

```sh
dsl41 estate prune --run-root /srv/dsl41/runs/<id> --dry-run
dsl41 estate prune --run-root /srv/dsl41/runs/<id> --tombstones --keep-runs 200
dsl41 estate prune --run-root /srv/dsl41/runs/<id> --quarantine
dsl41 estate prune --run-root /srv/dsl41/runs/<id> --archive-inputs   # irreversible
```

`--dry-run` names every artifact and the verdict retention gives it, and
deletes nothing. With `--dry-run` and no class named, it lists all of
them, so you can pick from what is there. Without `--dry-run` and with no
class named, it deletes nothing and exits 2 saying so: a default set would
be a retention policy, and the policy is yours. Add `--estate-anchor`
wherever the rest of your commands need it.

Name `--estate-anchor` alone, with no `--run-root`, and the sweep covers
every root the registry names, in period order, as one result (§6a;
DL-141). Each root is still planned on its own. The floors, the refusals
and the descriptor the removal walks are per root, and so is
`--keep-runs`: it keeps N per job per root, which is more than asked,
never less.

The three verdicts:

- **floored**: the model refuses. No flag makes the verb delete these.
- **held**: the head has moved past it and a later checkpoint covers it,
  but no class licenses deleting it. Every held row names the dependency
  in the way. A WAL that reads "no chain checkpoint above period 3 covers
  it" tells you to attest a later period. Older sidecars, older manifests
  and unreferenced bundles stay held: no class covers them, the archive
  class included.
- **prunable**: a class licenses the deletion by name. There are three
  classes: a SPAWN tombstone whose period is attested and whose run has
  ended (`--tombstones`: the run directory, its `.by_run_id` entry and its
  default logs, always together), a quarantined candidate
  (`--quarantine`), and an archivable period's inputs
  (`--archive-inputs`, below).

**`--archive-inputs`: the one deletion you cannot undo** (DL-144). It
deletes an attested period's WAL segment and its committed
`staged_manifest.json` and `candidate.json`. Before it deletes anything,
it writes `seals/<period>.archive.json`, the receipt, durably. The period
then reads at the attestation-verified tier. `dsl41 audit` says so by
name. `dsl41 journal` reports a gap it cannot replay and crosses to the
next period on the checkpoint. `dsl41 runs` names the coverage it lacks.
Nothing can re-derive that period again. Restoring the files does not undo
it: the receipt governs.

The order is fixed, and the verb tells you where you are in it:

1. `dsl41 audit --run-root <root>` for the period and a later one: a
   chain checkpoint above it stands in for the inputs. With no `--period`
   it audits every closed period the root holds;
2. `dsl41 estate prune --run-root <root> --tombstones` for that period's
   runs. There is no period selector: the sweep takes every eligible run
   in the root it is addressed at. Until they are gone, the archive
   refuses and names what remains. The tombstone floor resolves a run
   directory to a period through the SPAWN effect in that period's WAL,
   so archiving the WAL first would leave every tombstone it explains
   floored for good;
3. archive the oldest unarchived period first. The verb enforces this.
   The archived periods are a prefix of what a root retains, so the
   remaining segments stay contiguous and every reader that spans segments
   keeps working.

A segment missing with no receipt is loss, not an archive. Every reader
then refuses by name rather than replay a lineage that starts later than
it did. That is why the receipt is written before the first deletion.

**"Attested" is what unlocks a tombstone.** Run `dsl41 audit` (§6a)
first. Until a period is attested, its whole spool is floored, because
`audit` re-derives the period from that spool. Once it is attested, that
period's finished runs may go. After they are gone, the period cannot be
re-derived from its own evidence, and its attestation is the proof that
stands for it. The trade goes one way only.

What you lose is the process clock in `dsl41 runs`. With a spool, the row
times a run by `spawn.json` and `status.json`. Without it, the row uses
the journal's own `dispatch` record and terminal transition, and says
which in `clock_source`. Start and end usually survive the prune; their
source changes. The row itself always stays: it is the WAL's, not the
spool's. `dsl41 journal` reads no spool while it replays one period, but
it reads the closing period's spool at every boundary it crosses:
re-deriving that seal reads the executions the seal carried.

`--keep-runs N` keeps the N newest run spools of each job, and
`--older-than-days D` keeps anything touched more recently than D days.
Both are your policy, not the model's. Both filter whole runs: a directory
is never removed while its index entry stays.

`--keep-runs` is per job because `run_number` is per job. One list ranked
by run number would compare numbers from different series: a busy job's
fifth run would outrank a quiet job's first, and `--keep-runs 3` would
then delete the quiet job's whole history.

A removal the filesystem refuses, such as a permission error or a
directory that vanished under the sweep, is reported, and the sweep goes
on. The rest of that run stays. The exit code is 2, and the report names
each artifact that did not go.

The verb reads the anchor and never locks it, so it runs against a live
engine. It cannot reach that engine's work: everything a running period
creates belongs to a period that is not attested, and is floored for that
reason alone.

## 2b. Quiescent backup and restore

This section is a procedure, not a verb (DL-219; rehearsed by
`tests/test_restore_drill.py`). There is no `dsl41 backup` or `dsl41
restore`. Back up and restore with your own file copier, against the
inventory, the precondition and the constraints below.

**The inventory.** Four things, all of them:

- **the anchor directory**: `<run-root>.anchor` by default (§2). It is a
  sibling of the run root, so a copy that stops at the root's own tree
  leaves the fence behind;
- **every run root the retained lineage needs**: one per period-model
  §1.1, or several once a physical roll has moved the lineage to a fresh
  root. The anchor's registry names them (§6a). Back up each one the
  registry names, not just the newest;
- **the retained evidence inside those roots**: seals, attestations,
  archive receipts, and the WAL and spool of any period not yet archived
  (§2a's floor table). This is not a separate step. It is part of each
  run root, so the bullet above already covers it;
- **the deployment inputs**: the estate's JIL, the properties file the
  night ran with, and the `--timezone-map` and `--access-map` files if the
  estate uses them (§1, §2, §4). A restored root's next opener loads its
  catalog from the JIL and properties by path, as every opener does, and a
  resumed profile re-reads `--timezone-map` the same way. A DR host needs
  its own copy of the checkout your estate directory holds, plus every
  run-specific properties, timezone-map or access-map file that is not in
  that checkout.

**The quiescence precondition.** Nothing may still be writing into what
you copy. Stopping the engine is necessary but not enough. A detached
period's commands run under the supervisor, which outlives the engine by
design (runner-design §6a). After the engine exits, `supervisor.sock` and
`supervisor.pid` remain, held by a process that can still write into the
run root. The order of the steps matters. How you stop each process
depends on the deployment shape (§3). The seal between the two stops does
not (DL-266).

**Hold the estate down across reboots: the reboot hold** (DL-268). Under
shape 1 the units are enabled (§0's service recipe), and an enabled engine
unit resumes the root at every boot. On a sealed root, that opens the next
period in place. A window that a reboot must not end takes the reboot hold
first, before any stop or seal, live or offline:

<!-- recipe: hold-down -->
```sh
systemctl disable dsl41-engine.service dsl41-supervisor.service
```

The window ends by releasing the reboot hold, once the estate may start
again. The failure and recovery paths release it too:

<!-- recipe: hold-release -->
```sh
systemctl enable dsl41-supervisor.service dsl41-engine.service
```

Three procedures take the reboot hold: this backup, §7's rows 2 and 4, and
a retirement (§0), which never releases it. A stop for a host reboot does
not take it: there the enabled units are what bring the estate back (§0's
stop recipe).

The seal is the same in every shape. Seal the period (§6a) and
`dsl41 audit` it, so what you back up is closed and attested. Do this
while a detached period's supervisor is still up. The offline `seal`
command wires a detached period's supervisor client as a live engine does
(`wire_from_profile`), and attaches to the supervisor that is running.
With none running, it refuses with exit 2, forks nothing, and names the
remedy: start the supervisor service, or pass `--supervisor spawn`. Under
shape 1, the service is the unit: `systemctl start
dsl41-supervisor.service`. Do not type `dsl41 supervise start` by hand
under a unit: it runs the supervisor in your session, outside the unit's
cgroup, and guarded maintenance then refuses it. `--supervisor spawn`
forks a supervisor in the operator's session too, and that supervisor
runs the deadman the period pinned. It is a development path (DL-396). A seal, live or
offline, refuses inside the closing period's retry horizon (period-model
§9). The horizon counts
from the last operator request, an `ON_HOLD` included; wait it out. A
live seal stops the engine itself with exit 3. Under shape 1 the verb
below then finds the period sealed and skips its seal; under shape 2 the
live seal takes the place of steps 1 to 3. It does not replace the
reboot hold: take the reboot hold before a live seal as before an
offline one.

**Shape 1, a supervisor unit of its own** (the example units). Two root
steps, then one verb as the service account (DL-398):

1. with the reboot hold taken (above), stop the engine unit:
   `systemctl stop dsl41-engine.service`. The supervisor unit keeps
   running;
2. run `dsl41 estate quiesce` as the service account, with the seal's
   options for the next period (§6a; the job recipe's seal shows them):

```sh
args=$(dsl41 estate args "$ESTATE/estate.toml" --for seal --environment "$ENVIRONMENT")
eval "set -- $args"
sudo -u dsl41 -H /opt/dsl41/.venv/bin/dsl41 estate quiesce \
    --run-root "$RUN_ROOT" --estate-anchor "$ESTATE_ANCHOR" "$@" -p "$PROPERTIES" \
    --next-detached --next-as-machine localhost --next-machine-policy strict
```

The verb checks the two root steps and performs neither: a unit still
enabled (a masked unit counts as held), or an engine unit still active,
refuses and prints the root command to run. It then takes `leader.lock`,
so an engine started during the seal and the audit is refused. It seals
the period offline and audits it while the supervisor runs. It requests
guarded maintenance as `supervise shutdown` does (§3), and hands
`leader.lock` to the supervisor with that request. A process that takes
the lock in that one request makes the supervisor refuse maintenance:
the verb exits 2, the seal and the audit stand, and the lease goes back.
The supervisor answers at once and the verb watches the operation in
`maintenance.json` for its `bound_s` (DL-401).
It reads both units once after the supervisor's exit, and runs the
no-writers check below. Each stage ends with one line: `quiesce:
<stage>: ok`, `refused: <reason>` or `skipped: <reason>`.

The verb never waits for a precondition. A stage that cannot pass yet
refuses with exit 2 and names what to wait for: the end of the retry
horizon, or the runs that are still live. Wait, then run the same
command again. Its one bounded wait is the watch of an admitted
operation, `bound_s` plus two seconds at most. What the earlier stages
did is durable, and a stage already done is skipped, so a rerun on a
stopped estate exits 0. A rerun reads `maintenance.json` before it asks
for maintenance and never asks twice: a supervisor gone with its record
`completed` is skipped, an operation still running is watched for the
rest of its bound, and `uncertain` is exit 4. The supervisor holds
`leader.lock` meanwhile: on a sealed period the leadership stage is
skipped; on an unsealed one it refuses and names the operation, since
the seal needs the lock. Maintenance with live runs refuses and prints them; the
verb gives the lease and the lock back, and the runs go on. `--force
terminate` ends them instead, as `supervise shutdown --force=terminate`
does. Every other refusal names its own cause: the process that holds
`leader.lock`, the controller that holds the lease, or the containment
or unit policy §3 describes. A unit still deactivating is a refusal; run
the verb again once it is inactive. Exit 3 means maintenance is
admitted and still running when the verb's watch ends: it finishes on
its own; run the verb again, which watches it and asks nothing. Exit 4
is uncertain: an operation whose record does not prove completion, or a
unit its exit policy restarted. Read `maintenance.json`, the unit, the process and
`supervisor.log` before anything else (§3).

**Shape 2, and an engine outside any service manager:**

1. stop the engine (`dsl41 run`'s own SIGINT, or its unit);
2. if the period ran detached, confirm the supervisor is still there:
   `dsl41 supervise list --run-root <root>` answers `ok` while it is;
3. seal and audit, as above;
4. unmanaged containment has no qualified maintenance-idle proof
   (DL-390). The safe command refuses. Use §3's explicit incident stop
   after inspecting the complete process scope, or deploy the separate
   supervisor service before relying on this backup procedure;
5. run the no-writers check below.

A tethered period has no supervisor of its own. Under shape 1 the verb
seals it without one, and maintenance still stops the supervisor unit.
Under shape 2 it skips steps 2 and 4.

**The no-writers check**, in every shape, before you copy anything: no
`supervisor.pid`, no `supervisor.sock`, no engine holding `leader.lock`.
Under shape 1 it is the verb's last stage, which on Linux also finds no
other process of the service account. The verb's own parent chain, such
as the shell it was started from, does not count; any other process of
the account refuses, named by pid and command, and so does one the scan
cannot read. `tests/test_restore_drill.py`
asserts that both files are absent at this point, with a supervisor
outside any service manager. The service drill runs shape 1's steps
(§3's worked example) and checks what the verb printed. It waits past
`RestartSec`,
copies the root and the anchor, deletes them, and restores them at the
recorded paths. It then audits the lineage and opens the next period.
Under shape 1, start a restored estate with `systemctl start
dsl41-engine.service`. Its `Requires=` starts the supervisor unit first.
Release the reboot hold once the copy is done, or once the restored estate
runs. If the start fails, fix the cause, start again, and then release it.

**The path-equality constraint.** The anchor's registry names each
period's run root by absolute path (§6a; period-model §1.3). A restore of
the whole lineage, the anchor and every root, at a different absolute path
does not change those rows. They still name the original path, which
holds nothing after a restore elsewhere. Every estate-wide read
(`dsl41 audit --estate-anchor`, `journal`, `runs`, `estate prune`) then
meets a missing registered root. That is the same refusal an incomplete
restore produces, and the drill checks it. Restore each root at the same
absolute path it was backed up from, mounts included. A DR host needs the
mount layout of the original host, at least for every path a run root or
the anchor can sit at. `boundary.claim_id_for` hashes the target root's
realpath into a physical roll's successor-claim digest. Only the claim
recovery of an interrupted roll recomputes and compares it
(`test_nightbank_boundary.py`'s
`test_reclaim_frees_a_lineage_a_crashed_roll_left_claimed`). The resume of
a completed period does not. Resume opens the `--run-root` it is given,
checks that the anchor names that root, and refuses a root it does not
name (DL-224).

**Resume on a relocated copy is refused (DL-224).** Resume applies
period-model §1.3's resume rule. The anchor must name the `--run-root`
given, and the registry row for the period of the root's newest opened
segment must name it too. When that period has no row yet, the head must
be this root's own claim: an opening crashed between writing its segment
and moving the head, and resume finishes it. A copy or a restore at
another path is refused with exit 2, whether it is resumed against its
copied anchor or against the original one. The message names the anchor,
the root and the recorded root. If the original engine holds the
original's anchor at that moment, the refusal says instead that the
anchor is held by another process.
`dsl41 run --resume` and an offline `dsl41 seal` refuse such a root before
they repair or stage anything or wire a supervisor, so no supervisor starts.
That holds for a root that fails the rule when the command starts; if the
lineage changes under it, for instance by an `estate reclaim` run at the
same moment, the refusal can come after the supervisor is wired. A missing
anchor or another estate's anchor keeps its refusal and order.
There is no override: restore at the recorded path. A roll that stopped
before its claim is also refused; run the opener (`--open-from`) again.
These are the same directory as the recorded path, and are accepted: a
symlink left at that path that leads to the restored root, a case-variant
spelling of it on a case-insensitive filesystem, and a bind mount of it.
`tests/test_restore_drill.py` checks this refusal on a lineage restored
at the wrong path.

**What archived inputs cannot get back.** `estate prune --archive-inputs`
(§2a) cannot be undone. A restored copy of the deleted WAL beside the
receipt does not move that period back to derivation-verified
(period-model §12a). The receipt governs, and `dsl41 audit` reports
attestation-verified for that period whatever is on disk beside it.
Period-model §12a states the same for every other reader (`journal`,
`runs`, the estate walk); the restore drill checks only `audit`. Back up
an archived period's receipt, attestation and sidecar like anything else
the registry needs. A stray copy of its deleted WAL does not buy back the
stronger tier.

**What this does not prove.** A restore does not decide who may run the
lineage. Nothing here checks that the original host is stopped for good,
or arbitrates between two copies of one estate that both claim to lead
it. That is an operational discipline outside the model, as a physical
roll while jobs are live is a non-goal (period-model §12). Nor does a
restore reconcile business effects a job produced after the backup and
before the restore. A job's `std_out_file`, the files it produced and
anything else it wrote outside the run root are not in this inventory,
and this section does not cover recovering or replaying them.

## 3. Starting the engine

```sh
dsl41 run /srv/dsl41/nightbank/releases/<name>/*.jil \
    --run-root /srv/dsl41/runs/<id> \
    --detached \
    --as-machine <name-your-jils-use> \
    --timezone <base-zone> \
    [--timezone-map tz-aliases.json] [-p site.properties]
```

In a unit or launcher, name the files explicitly rather than by glob: a
shell glob's order is the locale's, and the file order is part of the
catalog hash (the worked example below does this).

Decisions to make once, per site:

- **Tethered or detached.** Tethered is the default: when the engine
  dies, its jobs are killed, and the journal records it. It is the
  simplest mode, right for development and for estates where a dead
  engine should mean a dead night. With `--detached`, jobs run under a
  per-run-root supervisor, and an engine restart (`--resume --detached`)
  reattaches to them instead of killing them. Detached is the production
  default. Inspect it with `dsl41 supervise list --run-root <root>`.
  For maintenance, stop the engine and follow §3's guarded procedure;
  for an estate stop, run `dsl41 estate quiesce` (§2b).
  Plain `supervise shutdown` refuses active or indeterminate work.
  Explicit `--force=terminate` permits TERM/grace/KILL only for validated
  command groups. Both require the existing leader lock and verified
  dedicated service containment. A free fencing lease alone proves
  neither engine exclusion nor idle work. Uncertainty never authorizes
  an automatic retry or signal fallback.
- **Faulting commands.** A command whose apply raises is refused with
  `apply_faulted` before it is journaled (concurrency-model §4).
  `--on-transition-violation` is inert (DL-353) and is removed at the
  next major release: every state machine is a declared statechart, and a
  gap in one is a statechart error (exit 7). Given, the option prints one
  deprecation line and changes nothing; remove it from the launcher.
- **Faults in engine-made inputs.** The engine applies each input it made
  itself on a copy of its state first (concurrency-model §4). If a tick, a
  completion or a routing observation raises there and the timers due at
  its stamp apply on their own, the input is quarantined: it is journaled
  with a rejected decision under `engine_apply_faulted`, only its timers
  apply, a `dsl41: input quarantined` line names it, and the engine runs on
  (§0's "What to watch"). If the timers raise too, or the input is a time
  observation, which is all timers, the engine stops before journaling the
  input and exits 6.
- **Machine identity.** Pass `--as-machine` explicitly; the zero-config
  fallback (forward hostname) is for laptops. Jobs whose `machine:`
  resolves elsewhere are refused at preflight (`--machine-policy strict`,
  keep it).
- **Init system.** The engine runs until SIGINT or SIGTERM and stops
  cleanly on both. Under systemd, use `Type=simple`,
  `Restart=on-failure`, `RestartPreventExitStatus=2 3 6 7`, a sane
  `RestartSec`, and an `ExecStart` wrapper that passes `--resume` if and
  only if `<root>/journal.jsonl` exists. Exit 2 is a configuration
  refusal (below) that a retry cannot fix. Exit 3 is a sealed engine: the
  next period is opened by an operator, not by a restart loop (§6a;
  DL-134). A unit that lists 2 alone restart-loops at every boundary.
  Exit 5 is retired and stays unused (DL-353), so a unit that still
  lists it is harmless. Exit 6 is a time-half stop: the timers due at an
  engine-made input raised, so every restart would stop the same way. Exit 7 is a
  statechart error: a bug in a declared state machine, which every
  restart of the same release may meet again. A
  crash restart must resume the same run root, and the first start of a
  new baseline must not. Never automate the choice of run root: a new
  baseline is an operator action (§6).
- **Reaping.** Run the engine and the supervisor under an init that reaps
  orphaned processes (DL-319). systemd as PID 1 does. In a container, use
  `docker run --init` or an equivalent. A live supervisor reaps its
  wrappers, and on Linux also the commands of wrappers that died. A live
  tethered engine reaps its own wrappers. Once the supervisor or a
  tethered engine is gone, its wrappers and their commands pass to PID 1,
  or to the nearest subreaper. A process that has exited and is not
  reaped stays a zombie and keeps its slot in the process table. When
  dsl41 finds a run with no status record, its liveness check counts a
  zombie as dead, as it counts a reaped process (DL-322). That check runs
  at resume, and in a live engine that has lost its supervisor. A command
  that has already exited is recorded `FAILURE` with an exit status dsl41
  could not observe, with or without a reaping init. dsl41 does not
  signal its process group, so a member still running there is left
  alone. The same check decides a kill, through the engine or through the
  supervisor: the group of a command that has already exited, a zombie
  included, gets no signal.

A detached supervisor stays in the cgroup of the process that started it
(DL-210). `setsid` does not move it out. Choose one of these
two service shapes before relying on engine restarts to preserve jobs.

**Shape 1: a separate supervisor unit.** Start the supervisor in its own
service before the engine. Its wrappers and commands then belong to that
service, so stopping the engine's cgroup leaves them running. Use the same
OS user and run root for both services. The supervisor command creates the
root with mode 0700 if it is missing. It leaves an existing root's mode alone.
It runs in the foreground with the same pid and appends
stdout and stderr to `<root>/supervisor.log`.

The example unit
`examples/nightbank/deploy/dsl41-supervisor.service` is a complete
shape-1 supervisor unit; copy it and edit its paths. Its
`ExecStartPost=` waits for a real LIST answer through the launcher's
`supervisor-ready` mode.

Order the engine unit after this unit and require its successful start:
`After=dsl41-supervisor.service` and `Requires=dsl41-supervisor.service`.
The engine attaches to this supervisor and never starts its own: pass
`run --detached --supervisor attach`, as the example launcher does
(DL-396). An engine that finds no supervisor answering forks nothing and
exits 1, `no supervisor answers on <root>`, and the engine unit retries it.
So does a supervisor that drops the connection before the engine holds
its lease. A supervisor that answers and refuses, an incompatible protocol
version or a lease another controller holds, is exit 2. To bring the
supervisor back, start its unit, `systemctl start dsl41-supervisor.service`.
Never run `dsl41 supervise start` by hand under shape 1: that supervisor
runs in your session, outside the unit's cgroup, and guarded maintenance
refuses it.
Do not add `PartOf=` or `BindsTo=` from the supervisor to the engine.
Keep the engine's existing restart and resume rules above. Set the
supervisor's `TimeoutStopSec` to cover its longest command grace plus the
documented shutdown waits (`supervisor-protocol.md` §5); the service
manager's final cgroup kill can otherwise silence a wrapper before it
records the command's ending. A host stop that arrives during forced
maintenance is held until the operation ends, so also cover the
operation's `bound_s`: 15 + 5 + the longest grace + 2 + 8 s per live run
+ 10 s (DL-401). The example unit's 900 s is the small estate's 82 jobs
all live at the default 10 s grace, 15 + 5 + 10 + 2 + 8 × 82 + 10 =
698 s, rounded up; size another estate by the same line.

**Guarded maintenance (DL-390).** Install and reload the supervisor unit
before using a new runtime: `[Unit] RefuseManualStop=yes`, and
`[Service] SuccessExitStatus=8` with `RestartPreventExitStatus=2 7 8`.
Keep `Restart=always`, `KillMode=control-group` and the stop timeout.
Manual manager stop and restart refuse. Routine engine restart remains
available. The supervisor checks the stop guard when it starts: as its
unit's main process without `RefuseManualStop=yes`, it exits 2, naming
the unit and the directive, and the unit stays down (DL-399). Maintenance
admission checks the unit's identity, containment and exit policy. Dependency shutdown during host shutdown still stops the unit.
Do not add an `ExecStop` safety command: its refusal does not veto a
systemd stop, which still kills remaining processes.

Stop the engine, then run as the service account:

```sh
systemctl stop dsl41-engine.service
sudo -u dsl41 /opt/dsl41/.venv/bin/dsl41 supervise shutdown --run-root <root>
```

That stops the supervisor and leaves the period open. To stop the whole
estate, sealed and audited, run `dsl41 estate quiesce` instead (§2b,
DL-398): it sends this request after the seal and the audit, then checks
both units and the writers. Both are the supervisor unit's stop, which
its `Description=` names.

Default shutdown refuses active or indeterminate work without signals.
The server holds the existing engine lock through teardown and requires
verified dedicated service-cgroup observation, including residual children.
Empty LIST is not idle proof. The supervisor answers an admitted request
at once with `bound_s`, its own bound on the operation, and runs the
operation in its loop; `supervise list` shows it in `process_state` and
`maintenance` meanwhile (DL-401). The CLI then watches
`RUN_ROOT/maintenance.json` and the supervisor's process for `bound_s`
plus two seconds. `bound_s` is the supervisor's estimate; the operation
is never cut short at it. Exit 0 means verified completion: the process
exited and the record says `completed`; supervisor exit 8 prevents
restart. Exit 2 means refusal before maintenance began, or a request
whose reply was lost that was never admitted. Exit 3 means the operation
is admitted and a PING shows it still running as the watch ends: it
finishes on its own; read `maintenance.json` or run the same command
again, which watches it and sends nothing. Exit 4 means the outcome
cannot be observed. A supervisor alive without a socket that shows a
running operation is 4: read `supervisor.log` before anything else. A record
proves nothing about a later supervisor: one that started after it and
ended outside maintenance makes the rerun answer 4, not 0.

On exit 4, read `maintenance.json` first. `uncertain` names the cause in
`detail`; the supervisor is still up, holding its lifelines and the lock.
`admitted` with the process gone means it ended without completing. Then
read the unit, the process identity and `supervisor.log`; never
automatically repeat or signal. A lost reply is not resent: the CLI
reconnects once, fences its own request with its lease, and reads PING
and the record. An uncertain teardown may have removed the socket or pid
record already; `maintenance.json`, the unit, the process identity and
the log remain.
An old supervisor refuses MAINTENANCE_STOP or its version 2, and the CLI
exits 2. The CLI never falls back to legacy SHUTDOWN. Coordinate that
upgrade using the old runtime's reviewed procedure and the explicit
incident stop below when needed.

For deliberate termination, use the same command with `--force=terminate`.
It invokes TERM/grace/KILL for validated known command groups. It bypasses
no ownership, engine-lock or observation check. Residual or unknown work
after that ladder yields 4 and retains the supervisor, lifelines and engine
exclusion. Mutations, including another maintenance attempt, then refuse.
Recovery is the explicit incident stop below. Orphaning is unsupported.

**Cold bootstrap.** Before enabling either unit, provision a genuinely new
run root and an empty regular `leader.lock`, owned by the service account
and mode 0600. §0's install recipe does this exclusively. For a physical
roll or new estate, do the same for its new root before starting its
supervisor. A used root's lock is never truncated or replaced. Missing,
replaced or unreadable lock evidence makes maintenance refuse. Creating a
lock in a running root can admit another engine; it is not recovery.

**Unresponsive supervisor or uncertain termination.** Preserve the root,
spool and logs. Take the reboot hold (§2b), stop the engine and inspect the
unit's MainPID, ControlGroup and remaining processes. Decide explicitly
that ending every process in this supervisor unit is acceptable. This is
an incident stop, with possible unobservable job outcomes, not maintenance
completion. For the example unit, a root operator can temporarily remove
the manager guard and restart policy:

```sh
install -d /run/systemd/system/dsl41-supervisor.service.d
cat >/run/systemd/system/dsl41-supervisor.service.d/90-incident-stop.conf <<'EOF'
[Unit]
RefuseManualStop=no
[Service]
Restart=no
EOF
systemctl daemon-reload
systemctl stop dsl41-supervisor.service
systemctl is-active dsl41-supervisor.service
```

Verify that the cgroup is empty and no writers remain (§2b). Preserve the
command outcomes from the spool; do not infer that all jobs succeeded.
Remove only that incident drop-in, reload, and keep both units disabled
until diagnosis and recovery finish. Do not remove or recreate lock files
while any previous process may remain. The same procedure applies when
new units protect an old supervisor without the maintenance verb.
Unmanaged shape 2 needs an independently verified whole-process scope;
there is no automatic signal fallback or claim of safe idle shutdown.

Crash, deadman and host-shutdown behavior is unchanged. A deadman exit
restarts on purpose. Ownership refusal exits 1 and retries after two
seconds without a start limit. Configuration refusal 2 and statechart
failure 7 do not restart. A legacy shutdown wait that fails twice exits 1.
Its wrappers then end as on a supervisor crash. Restarting a supervisor
never resurrects commands; the engine reconciles the spool.

The optional `supervise start --deadman-seconds N` sets the deadman: a
finite positive number of seconds with no live leaseholder, normally the
engine. Then the supervisor exits, and its wrappers end their commands
(supervisor-protocol §5). Omit it for no deadman. The engine reads the
running supervisor's actual value through PING/LIST. The option is refused
on `supervise list` and `supervise shutdown`. The upgrade rule for an old,
lockless supervisor binary is stated in `supervisor-protocol.md` §5.
The engine renews its lease from its event loop, and each input's dry
apply runs on that loop. An input whose cascade runs longer than 40 s, the
lease's TTL less its renewal interval, lets the lease lapse; with a
deadman of N seconds the supervisor exits N seconds later and every
running job on the host ends `parent lost`. The item cap (§7) keeps a
cascade that does not end under half that lapse at every measured size.
A cascade that loops across one input's root dispatches, or inside one
condition, is not bounded by it, as for any hung engine.

**Shape 2: supervisor started by the engine.** Keep `run --detached`,
whose default `--supervisor spawn` starts a supervisor when none answers,
and set `KillMode=process` on the engine unit. It limits service-stop signals
to the engine's main process, so the supervisor and jobs can survive that
stop in the same cgroup. This gives up the unit's normal whole-cgroup stop
containment. A killed or stopped engine can leave these processes running;
monitor the supervisor and its log separately. To stop the estate, stop
the engine first. Unmanaged containment refuses guarded maintenance.
Use the explicit
incident procedure above; killing wrappers can leave
`exit_status_unobservable` outcomes. Keep the engine's restart exclusions
and resume rules above. This shape has no separate supervisor restart
service; a later engine start can start a replacement after proving the
old owner is absent.

Exit codes:

- 0: a clean stop.
- 1: the engine or the estate failed, or `--supervisor attach` found no
  supervisor; a restart retries both.
- 2: refused before start: a used run root, a resume gate (catalog hash,
  clock domain or runtime profile), a preflight ERROR, or a root another
  engine already leads.
- 3: sealed; period N+1 is ready to open (§6a).
- 6: stopped before journaling an engine-made input whose time half
  raised: a timer due at its stamp, the release of a removed job's units,
  or a clock check. The journal stays clean. Every later input would meet
  the same timers, so the engine stays down until a release that fixes
  the fault is deployed (§7). A control command under the same fault is
  refused with `apply_faulted`.
- 7: stopped by a statechart error (docs/statechart-engine.md §5): a
  declared state machine hit an assertion failure, and the engine stopped
  before any durable write of the input. The `dsl41: statechart error`
  lines on stderr carry the report. The engine stays down until a release
  that fixes the bug is deployed (§7). The supervisor exits 7 for the same
  error, and its unit does not restart it either. Like any stop of the
  supervisor, it ends every command it still runs: "Stopping the
  supervisor unit ends every command it still runs" (§0).
- 8: supervisor only, successful guarded maintenance. The supervisor unit
  treats it as success and does not restart. The maintenance CLI reports
  its own completion/refusal/in-progress/uncertainty as 0/2/3/4 (§3,
  DL-401).

Exit 2 means a person misconfigured something, and a restart will not
help; hence `RestartPreventExitStatus=2 3 6 7` above. A second engine on a
live run root is refused by `leader.lock`, an `flock` the leader holds for
its whole process life. The kernel releases it when that process dies,
`kill -9` included. So a misconfigured restart loop cannot start a second
engine; it only loops.

Preflight ERRORs refuse the run. WARNs are printed and journaled, and the
run goes on. Read them on the first deploy of a new estate: they are the
lint findings that survive into operation.

### A worked example: `examples/nightbank/deploy/`

The repository holds a shape-1 deployment of the Nightbank training estate
(DL-218). The files are examples to review and copy. The wheel does not
ship them; the offline kit stages versioned copies with each candidate.
Every path and name in them is synthetic.

- `dsl41-launch` is a POSIX sh launcher. It is the one place that
  names the run root, the lineage anchor, the estate files in their
  order, every `-p`, every run option and `--access-map`. Edit its
  configuration block and review the edit like code. Its header
  states its rules: when it passes `--resume`, when its one-shot open
  trigger makes it pass `--open-from` (§7), what it refuses, and why it
  execs `dsl41`. `dsl41-launch --print` prints the command,
  shell-quoted, and runs no estate command. It does run the installed
  `dsl41` once, to probe whether `run` accepts `--supervisor` (DL-402),
  so print as a user who can run it, after the venv is installed.
- `dsl41-engine.service` and `dsl41-supervisor.service` are shape 1's
  two units. Both call the launcher. Each repeats the run root once,
  in `RequiresMountsFor=`; edit it with the launcher's `RUN_ROOT`.
  The exit codes each unit never restarts, and the engine's start
  limit, are stated in the unit files beside the directives. Manual
  starts count against the engine's start limit;
  `systemctl reset-failed dsl41-engine.service` clears it.
- `nightbank-access.toml` is the role map the launcher always
  configures. A missing or invalid map refuses the start with exit 2
  (§4).

Two checks stand behind them. CI runs `systemd-analyze verify` over both
units; that is a static check and starts nothing.
`.github/workflows/service-drill.yml` is a manual drill on a runner with
systemd. It covers the first start, a same-root restart, a detached job
that survives an engine stop, a changed estate refused without a restart
loop, and a sealed engine that stays stopped until the next period is
opened. It also covers §2b's shape-1 quiescence and a restore at the
recorded paths, a host reboot that the enabled units resume from (§0),
the signals of "What to watch", §0's configure and stop-and-recover
recipes, a SIGKILL of the engine and of the supervisor, and §7's four
upgrade rows with their rollbacks (DL-266). It installs and first
starts the units with §0's service recipe, and ends with §0's retirement
procedure, both read from this file (DL-268). The access-map refusals,
their messages and what they leave
untouched are the claim of `tests/test_nightbank_deploy.py`, which runs in
CI; the drill's claim is that systemd does not restart a refusal.
The step bodies live in `drill-steps.sh`. The workflow runs them one
step at a time. `drill-local.sh` runs the same steps on a workstation, in
a podman container with systemd as PID 1 on Ubuntu 24.04, as an
unprivileged user with passwordless sudo, as the runner runs them
(DL-271). It runs the host's architecture, so on Apple silicon it is
arm64, not the runner's x86_64.
The drill passed every step on GitHub's Ubuntu 24.04 x86_64 runner at
b6b8a55 (2026-10-09, DL-308). These steps also passed in
`drill-local.sh` on arm64 at a2db2ae (2026-10-09, Ubuntu 24.04, systemd
255): `watch`, `configure`, `kill-engine`, `kill-supervisor`,
`stop-recover`, the three rollback steps, `sealed`, `upgrade-fresh-root`,
`upgrade-coordinated`, `upgrade-old-release` and `upgrade-state-machine`
(DL-303). No other distribution or
systemd version has been observed. The drill is not part of the default
gate: dispatch it again after a change to the units, the launcher or the
drill.

## 4. UI surfaces

- `dsl41 ui --socket <root>/control.sock` — TUI in a terminal on the
  server (or over ssh). Quitting detaches; the run is untouched.
- `dsl41 serve --socket <root>/control.sock [--host 127.0.0.1 --port 8000]`
  — the same TUI in a browser. **textual-serve ships no auth, and an
  unarmed socket gives full sendevent control to anyone who reaches
  it**: keep the loopback default and
  front it with your reverse proxy (TLS + auth) or an ssh tunnel. It is
  a separate process: start it after the engine, restart it freely,
  systemd `After=`/`BindsTo=` the engine unit if you run it as a service.
- The socket's own perimeter is off unless you arm it. `dsl41 run
  --access-map <file>` loads a role map that gives each OS peer one of
  three tiers; a configured path that is missing or invalid refuses
  startup, and SIGHUP reloads it once the socket answers (access-model
  §7). A map that also names a socket group
  is what opens the run root to `0710` and the socket to `0660`; without
  one, the gate is live and the `0600` owner-only modes stand. Omit the
  option and nothing changes at all. `docs/access-model.md` is the
  contract — read it before exposing a socket to a second account.
- Headless glue (every control-plane command takes
  `--socket <root>/control.sock`, `-S` for short — set
  `S=<root>/control.sock` once in ops scripts):
  `dsl41 query status --brief -S $S`, `query is-success -J <job> -S $S`
  (shell exit codes), `query subscribe -S $S` (live journal stream) for
  feeding the site monitoring. §0's "What to watch" lists the signals.
- `sendevent` and `host` exit 4 when the answer was lost, and the recovery
  reference is the retained CLI stderr plus the original arguments: re-run
  those arguments with the `--request-id`, `--expect`, `--epoch` and
  `--baseline` the stderr lines printed, so keep stderr in your job logs
  (`docs/control-protocol.md` §3, DL-217). Retry as the same user on the
  same host: another user or host, `sudo`, or a newly armed access map
  changes the actor the envelope carries, and the retry is then refused as
  a collision that shows what the original decided, not replayed.
- Offline audit: `dsl41 journal <root> [estate files]` replays the WAL
  with no engine. The estate files are optional (DL-142). Without them,
  each period's catalog is loaded from that period's own bundle under
  `<root>/catalogs/<source_bundle_hash>/`, by the hash its opening
  `segment` record pins. The bundle re-parses under the original paths
  `sources.json` records, so it reproduces that hash exactly. Estate files
  you give are the first replayed period's catalog, checked against its
  pinned hash. Later periods still come from their own bundles. A supplied
  catalog that disagrees with a pin is refused. `--permit-unknown` and
  `-p` therefore apply only to the files you supply: a bundle holds the
  exact post-placeholder bytes the period ran, which already passed the
  launch gate. The replay is sensitive to paths. The stored copies under
  `<root>/catalogs/<hash>/`, passed by hand, parse under the stored names
  and do not match (runner-design §7, a deliberate defer). Let the verb
  load them instead.
  The replay crosses boundaries. A root argument replays every segment
  the root retains, in period order. At each boundary the state folds
  through the seal as an engine that opens the period does, the next
  period's catalog is loaded from its bundle, and the boundary is printed
  (`period N sealed at index I; period N+1 opens in <root>`). Name one
  `wal/NNNNNN.jsonl` to replay that period alone. It opens from its own
  seal too. Nothing re-derives that seal there, so it needs the
  predecessor attested (`dsl41 audit`), and says so if it is not.
  `wal/000001.jsonl` is the exception: period 1 opens from no seal, so it
  needs nothing. Name the lineage anchor directory instead of a root, and
  the read is estate-wide: every period, its root and its segment, in
  registry order, replayed as one lineage across the roll. A boundary is
  crossed only over a seal that proves out: the digest the record names,
  the record's own fields against the sidecar, the chain, `next_period`
  agreement, and the seal re-derived from the period's own evidence
  (period-model §11). The re-derivation catches a sidecar, record and
  opening forged to agree with each other. Anything less is refused by
  name. The re-derivation costs one extra replay per crossed boundary.
- Offline history: `dsl41 runs <root>... [--job NAME] [--since ISO8601]
  [--format table|json|csv]` folds one or more run roots' journal,
  manifest and spool into one row per job run: how long it took, run
  after run, and whether it changed (DL-113). Like `dsl41 journal`
  (DL-142), it needs no estate files. It rebuilds the catalog from the
  run root's own stored inputs, the DL-130 bundle and only that. The
  retired `manifest/` layout is refused, not read (DL-138). Name several
  run roots on one command line to carry a series across a baseline
  change. The default table marks the break rather than blend two
  catalogs into one line. A root that has crossed a boundary holds one WAL
  segment per period, and every retained one is read (DL-136). Each period
  is folded under its own catalog, so a series crosses a seal as it
  crosses a run root. Name the lineage anchor directory in place of the
  roots, and the list comes from the registry: one table across every
  root of the estate, in period order. A root that holds two periods is
  folded once. Name the anchor alone: a mix of an anchor and roots is
  refused.

## 5. Routine operations

Same-estate restart (an OS patch, a move of the process, crash
recovery): stop the engine with SIGTERM, and detached jobs keep running.
Start it again with the same command line plus `--resume`. The whole
command line must match, not only the files. Every opener re-parses the
JIL, so the file list and its order, every `-p`, and `--permit-unknown`
must be what they were, or the catalog hashes differently. The launch
options must match too. The resume gate refuses a mismatch of catalog
hash, clock domain or runtime profile, so the semantics cannot drift
silently. The runtime profile is `--timezone`, `--timezone-map`,
`--as-machine`, `--machine-policy`, `--detached`, `--deadman` and
`--semantics` (DL-252). `--access-map` is not in the profile and is not
gated: omit it and the run comes back with no perimeter (§4).
`--on-transition-violation` is inert (§3). Keep the whole line in
the launcher (§3's worked example); the unit calls the launcher. A
detached engine prints that line on its way out (DL-218): its own argv,
shell-quoted, with `--resume` and `--detached` once each, and
`--open-from X` turned into `--estate-anchor X`.

Scheduler ticks that came due while the engine was down are dropped and
journaled (`dropped STARTJOB ...`), never fired late. Plan maintenance
windows with that in mind, and catch up specific jobs afterwards with
explicit `FORCE_STARTJOB`s, which are journaled.

## 6. JIL rollout — updating the estate

An estate changes in one of two cycles (DL-408). The application train
is the common one: `dsl41 release apply` seals a release into the running
estate and opens it in place, in the same run root (DL-133, DL-134,
DL-407). [The job recipe](#recipe-add-change-or-remove-a-job) gives its
steps, and §6a gives the verbs under them. A boundary closes the running
period at a chosen instant T and commits the next one, and `dsl41 run
--resume` on the same root opens it. State does not reset: runtime
globals, operator holds, `last_end_at`, armed latches, every box's
`ran_members` and `run_number` all cross the boundary, because the
boundary is a record, not a directory.

The infrastructure acts are the other cycle: a fresh run root, a physical
roll (§6a, §7 row 2) and a new estate (§7 row 4). Each changes the
launcher's `RUN_ROOT` or `ESTATE_ANCHOR` and both units'
`RequiresMountsFor=`, which the site package owns
([site-package §5](site-package.md#5-ship-jil-releases-on-the-applications-train)).
The window below is written for the fresh run root. Its steps 1 and 2 are
also the hold and drain of the application train. Two steps below say
where the cycles differ: latches die with the run root, not with a seal
(step 1), and step 6's new run root is one of two openers (period-model
§7).

There is no reload while the engine runs, by design. The running catalog
is the truth until the engine stops, resume gates on the exact catalog
hash, and a used run root refuses a new baseline. An estate change is
therefore a restart, in one of the two cycles. An edit of the files under
a running engine only sets the TUI's SPEC DRIFT flag (an advisory
fingerprint check); it changes nothing live.

**Before the window** (off the production run): run the
[job recipe's check block](#recipe-add-change-or-remove-a-job) with
`ESTATE` set to the new release's directory. The manifest supplies the
timezone, its map and the semantic switches (DL-407); lint takes no
`--semantics`. Without a timezone, both work in UTC: rule L023 checks DST
effects there (DL-316), and rehearse plays the schedules there. Review the
diff visually with `viz`:

```sh
args=$(dsl41 estate args "$ESTATE/estate.toml" --for viz --environment "$ENVIRONMENT")
eval "set -- $args"
dsl41 viz --format chart "$@" -p "$PROPERTIES"
```

Rehearse costs little: it plays a full night in seconds, with the same
oracle and scripted adapters. Fix everything here. The production window
is for swapping files, not for finding problems.

**The window, in order:**

1. **Quiesce triggers.** `ON_HOLD` every scheduled top-level job or box
   that still has a future tick. Take the list from
   `dsl41 query timers -S $S`. It holds more than you need: every pending
   oracle timer, every scheduled job's next tick, box members included,
   and every live filewatch as a row with no due time. Take the
   `kind=schedule` rows and drop the ones that name a box member. "Already
   fired today" is not an exemption: a job with several `start_times`, or
   with `start_mins`, fires again. A hold satisfies nothing downstream;
   `ON_ICE` would, because it marks the job satisfied at once. A tick that
   lands on a held job latches (flag `A` in `query status --brief`).
   Latches are run-root state and die with the old run root (DL-133); a
   seal does not create one. An armed latch survives a seal on purpose:
   dropping it at the boundary would be a transition with no admitted
   input. So the operator's `OFF_HOLD` in the new period produces exactly
   one start, which is the point of the hold. In the fresh run root
   below, the state is thrown away. An operator who does not want that
   start sends `dsl41 sendevent DISARM -J job` (DL-158). It drops the
   latch and does nothing else. Send it before the `OFF_HOLD`, on either
   side of the seal.
2. **Drain.** Let RUNNING work finish (`query status --brief -S $S`), or
   `KILLJOB` what the window cannot wait for. Kill command jobs, not
   boxes: only `job_terminator` members die with a box.
3. **Stop the web UI**, if any. It is stateless; the order matters only
   for tidy monitoring.
4. **Stop the engine** (SIGTERM). If detached, follow §3's guarded
   maintenance procedure. Wait for active work to finish, or explicitly
   accept termination with `--force=terminate` under verified service
   containment. Require exit 0 and §2b's no-writers proof before continuing.
   Refusal, uncertainty or unmanaged containment requires §3's incident
   procedure; an empty LIST does not prove completion. A job left running
   across a new baseline is a process the new catalog knows nothing about.
5. **Name the release**: as the deployment identity, prepare it with
   `/opt/dsl41/.venv/bin/dsl41 release prepare NAME DIR`, and write NAME to
   `ACTIVE`, as the service recipe's step 4 does. The launcher reads only
   the release `ACTIVE` names.
6. **New run root**: `dsl41 run ... --run-root /srv/dsl41/runs/<new-id>`,
   a fresh root with no `--resume`. Name run roots after the baseline: a
   date and the estate tag serve well. The old run root stays untouched,
   as the record of the old estate.
7. **Verify.** Repoint `S` first:
   `S=/srv/dsl41/runs/<new-id>/control.sock`, because the old root's
   socket went with its engine. Then read the preflight WARNs,
   `periods/000001/manifest.json` (hashes, versions, runtime profile) and
   `sources.json` (files), run `dsl41 query plan -S $S` for the expected
   waves, and watch the first scheduled fire.

**Rollback** is the same procedure with the previous release and
another fresh run root. If that release is ever in doubt, the old run
root's `catalogs/` holds the post-placeholder JIL that baseline ran. It is
byte for byte what ran, but with placeholders already resolved, so prefer
the release.

## 6a. The boundary — sealing a period and opening the next

The application train's boundary (§6), as commands (DL-134; period-model §7 and
§11). It keeps the state: runtime globals, operator holds, `last_end_at`,
armed latches, every box's `ran_members` and `run_number` all cross,
because the boundary is a record, not a directory.

**Seal.** First take steps 1 to 3 of §6's window: quiesce triggers,
drain, stop the web UI. Then:

```sh
dsl41 seal --run-root /srv/dsl41/runs/<id> \
    --next /srv/dsl41/nightbank/releases/<name>/first.jil \
    --next /srv/dsl41/nightbank/releases/<name>/second.jil \
    [-p site.properties] [--next-timezone …] [--claimed-actor you@host]
```

`--next` is an option, not the positional file list `dsl41 run` takes:
name it once per file. A shell glob after one `--next` is refused as an
extra argument, so expand the estate yourself. The order is part of
`source_bundle_hash`, so use the order `dsl41 run` will open the period
with.

`seal` has two entry modes, and the lock decides which, not a flag. An
engine that holds `leader.lock` is a live engine. The CLI stages C2 and
asks that engine over the control socket, and the engine then exits with
code 3 ("sealed; period N+1 is ready to open"). Set
`RestartPreventExitStatus=2 3 6 7` under systemd, or the init system
restart-loops a sealed engine. With no engine running, the same command
takes the lock itself, replays and reconciles, and performs the boundary
as an offline leader. In both modes C1 comes from the run root's own
bundle, so the estate files the period was launched from need not still
exist.

Exit codes:

- 0: committed.
- 2: not committed, and the period is still open. C1 may have advanced
  first: an offline sealer's `leader` record and the cutoff's admitted
  ticks are C1 activity, not damage.
- 4: the outcome is unknown. Read the estate before you retry, and then
  retry only with the printed `request_id` (Day 2, below).

`--force-seal` commits inside the closing period's retry horizon, and the
seal records it.

The `--next-*` options describe the period about to open:
`--next-timezone`, `--next-as-machine`, `--next-machine-policy`,
`--next-detached`, `--next-deadman`, `--next-timezone-map` and
`--next-semantics` (DL-252). A change to any of them makes a new period,
as a catalog change does. That is the model's rule, and the opener
enforces it (see "Open, in place" below).

**They do not inherit C1. State the whole profile every time.** An omitted
`--next-*` takes its own default, not the running period's value: no
`--next-timezone` means UTC, no `--next-as-machine` means no declared
machine, and no `--next-detached` means tethered. A seal of a detached
period with a bare `--next` therefore commits a tethered successor.
`--next-deadman` needs `--next-detached`. Alone, it exits 2 before C2 is
staged, as `--deadman` needs `--detached` on `dsl41 run`.

**Open, in place.** One command, whatever the boundary moved (DL-151).
The files are C2's, and so are the options. State on the opener every
`--next-*` the seal staged, without the `--next-` prefix
(`--next-timezone Europe/Zurich` becomes `--timezone Europe/Zurich`). A
catalog-only boundary therefore repeats the closing period's options,
because those are what it staged.

Wrong options are refused, and nothing is written. The successor's
segment is not created, the lineage head does not move, and the refusal
names the fields that disagree (`runtime-profile mismatch on <field>`).
So the corrected command opens the same committed boundary. This holds
for the fields the engine wires, and for the two it cannot see,
`--next-as-machine` and `--next-machine-policy` (DL-151).

```sh
dsl41 run --resume --run-root /srv/dsl41/runs/<id> \
    --detached --as-machine <machine> /srv/dsl41/nightbank/releases/<name>/*.jil
```

**Attest.** Before a period's root can be archived or rolled away from,
audit it:

```sh
dsl41 audit  --run-root /srv/dsl41/runs/<id>     # re-derive + checkpoint
dsl41 verify --run-root /srv/dsl41/runs/<id>     # validate a checkpoint
```

`audit` rebuilds the seal from the period's own evidence and refuses if
the two disagree. It needs the period's WAL, spool and manifests, and the
predecessor checkpoint, present and verified. Period 1 is the base case
and needs no predecessor. `verify` validates a checkpoint alone: its
digest, its binding to the seal it names, and the chain it claims. A
rolled root can do that, and cannot do a full audit. An audit of a period
whose state-machine version differs from this binary's needs the dsl41
version that produced it (§7's venv-per-version pattern). The refusal
names the version only when the seal reads. A v1.7.0 seal does not read
under this build, and the refusal says `the bytes are not in ss3.2
canonical form (corrupt bytes, or a seal written by a build with a
different record shape -- DL-154)`, which names neither the version nor
the shape. To tell, audit the same root with the kept old venv: if that
passes, the old venv is the one to use. A period run by an older release
of the same state-machine version audits under the current binary.

**Open, in a fresh root**: the physical roll, optional archival hygiene:

```sh
A=/srv/dsl41/runs/<first>.anchor                 # whatever genesis used
dsl41 audit --run-root /srv/dsl41/runs/<old> --estate-anchor $A
dsl41 run --open-from $A --run-root /srv/dsl41/runs/<new> \
    --detached --as-machine <machine> -p site.properties \
    /srv/dsl41/nightbank/releases/<name>/*.jil
```

The opener is a full launch line, as §5 says: the C2 files in their
order, every `-p`, `--permit-unknown` if the estate needs it, and the run
options.

It refuses unless the head is `closed`, and the closing period is
quiescent (no live executions at all) and attested. The anchor is the
lineage's, not the root's. A roll creates no new one, so `$A` is the
anchor genesis used, for every later roll: `<first>.anchor` when genesis
named none, and the `--estate-anchor` it named otherwise. Never
`<old>.anchor` after the first roll. Every later `--resume` of the new
root needs `--estate-anchor $A` too. Put it in the unit file with the run
root.

**Reading the whole estate** (DL-141). After a roll the estate is more
than one directory, and the anchor's registry says which root holds which
period. Four verbs read it, and all four are addressed the same way: name
the lineage anchor where you would name a run root:

```sh
A=/srv/dsl41/runs/<first>.anchor
dsl41 audit --estate-anchor $A                  # every closed period, in its own root
dsl41 journal $A                                # every segment, replayed in period order
dsl41 runs $A                                   # one table across every root
dsl41 estate prune --estate-anchor $A --dry-run # one retention result
```

`audit` and `estate prune` take `--estate-anchor`, so naming it with no
`--run-root` is their estate-wide form. `runs` and `journal` take their
root as an argument, so the anchor goes there instead. A verb given
neither address refuses rather than guess.

Each of the four covers every period it can, and refuses rather than
guess. A root the registry names that is missing, holds no sentinel,
holds one that cannot be read, belongs to another estate, or has lost the
segment it is registered for stops the command by name. Two things are
left out and reported: a registry row whose first segment is not durable
yet, which every cross-period reader ignores, and, for `audit`, a period
that is still open. Nothing is skipped silently: a total that left a root
out without saying so is worse than no total. If you archived a root away
on purpose, use the single-root form for the roots you still have.

One limit: `estate prune` plans each root separately, because a plan is
bound to the root it was computed over. The floors, the refusals and
`--keep-runs` are per root. `dsl41 journal` names every segment and
replays all of them (DL-142): each boundary is folded through its seal,
the next period's catalog comes from its own bundle, and the crossing is
printed. It needs no estate-file argument, for the same reason `runs`
does not: the estate holds its own catalogs.

**There is no adoption verb (DL-138).** A `header` journal, a
`catalog_hash_version` of 1, a `result` or standalone `effect` record and
a `manifest/manifest.json` layout are each refused by name, citing DL-138.
There is no supported path from such a root into a lineage. Start a new
estate with `dsl41 run`, and keep the old root as the archive of the
nights it holds. `docs/protocol-evolution.md` is the contract for a
retired dialect: what each protocol tolerates, how long its instances
live, and what must be true before a reader may drop a dialect.

The `estate` group's verbs are `reclaim` (below) and `prune` (§2a).

A roll that is refused after it wrote the target root's sentinel leaves
that directory owned by the claim it was attempting. Period-model §1.1's
ownership rule then admits one thing only: the same roll, retried. Fix
what it refused on and re-run the identical `dsl41 run --open-from`, with
the same anchor and the same target root. The claim resumes, because a
claim is idempotent on its id. Any other roll into that directory is
refused. That is the rule working, not a bug.

Whether you may roll somewhere else depends on the lineage head, not on
the directory. If the head is still `closed`, the roll died before it
took its claim, and a fresh target root is a normal roll. If it is
`claimed`, only that claim's own target is accepted: retry it, or prove
the claimant gone and use the break-glass below. Deleting the abandoned
directory clears no claim.

**Break-glass.** A `claimed` lineage head whose target root is gone blocks
every opener. Overriding it can fork the lineage: two roots open one
period and run the same `(job, run_number)` twice. So prove the claimant
is gone first:

```sh
dsl41 estate reclaim --estate-anchor $A --force
```

It is recorded in the anchor and again in the next `segment` record with
the actor who claimed to authorize it.

**Day 2 (DL-135).** The things that go wrong after the first boundary, and
the move for each.

*A seal exited 4.* The outcome is unknown: the seal may or may not have
committed. Read the estate before you send anything. A committed boundary
left `seals/<N>.json`, a `seal` record at the end of `wal/<N>.jsonl` and
a `closed` head in the anchor. If they are there, the boundary is done,
and the next move is to open it, never to seal again. If they are not,
re-send the same request with the `--request-id` the command printed. A
retry that the still-open period recognises is answered from its own
decision and applies nothing twice. Never compose a fresh `request_id`
for a retry: a new id is a new command. A retry that finds the boundary
already committed is answered from the seal it committed (DL-151): the
same digest, the same next period, and no second boundary, whether the
root is live or offline. A live seal also exits 4 when the engine answers
that this same request is still in flight ("your seal <id> is still in
flight"): its boundary is running. Wait, read the estate as above, and
retry only under that `--request-id`. A seal that names another boundary
in flight exits 2: this request did nothing.

*The engine exited 3 and the init system restarted it.* It will loop.
Exit 3 is "sealed; period N+1 is ready to open", and the opening is an
operator action. Put `RestartPreventExitStatus=2 3 6 7` in the unit file.

*`audit` printed "the registry row could not be set".* The checkpoint is
written and durable, and the checkpoint is what `verify` and
`run --open-from` read. Only the anchor's `attested` row is outstanding,
because a live engine holds the lineage lock for its whole process life.
Re-run `dsl41 audit` when the lock is free; it is idempotent and finishes
the row. An estate-wide audit goes on past such a period: every other
period is still audited, and the last line says how many rows are
outstanding.

*`dsl41 audit` does not name a period you expected.* With no `--period`
it names the periods this root holds evidence for: a WAL, or an archive
receipt where `--archive-inputs` removed the inputs. A rolled root holds
the seal it opened from and none of that period's evidence, by design.
That seal is this root's to `verify` and the closing root's to audit. The
anchor's registry says which root holds which period.

*`audit` refuses, naming a version.* The period ran a different
state-machine version, and its audit runs the interpreter that produced
it. Keep that venv (§7's pattern) and run the audit from it; the refusal
names the version to use. A patch-release gap alone does not cause this.

*A roll was refused after it wrote the target root's sentinel.* That
directory is owned by the claim that was attempting it. Re-run the
identical `dsl41 run --open-from`, and it resumes; period-model §1.1's
ownership rule refuses any other roll into it. To roll somewhere else,
read the head first. `closed` accepts a fresh target root. `claimed`
accepts only its own: retry it, or reclaim it after you prove the
claimant is gone. A retry that refuses with `missing segment record`
keeps refusing while the torn segment stays; follow
[the torn-opening recipe](#recipe-recover-a-rolled-root-whose-opening-is-torn).

*A command on a rolled root refuses, naming an anchor.* The anchor is the
lineage's, not the root's. Five verbs take it as
`--estate-anchor /srv/dsl41/runs/<first>.anchor`: `run --resume`,
`seal`, `audit`, `estate prune` and `estate reclaim`, which requires it.
`run --open-from` takes it as its value. The other readers are
addressed by root or by socket and take no anchor option. Put it in the unit
file beside the run root. To read the estate rather than one of its
roots, name that anchor and no root (§6a, "Reading the whole estate").

*A client subscription resumed across a boundary.* Nothing to do: `since`
is an estate-wide index, and the backfill spans segments. A subscriber
whose cursor is below what the root still retains, as on a rolled root,
receives an explicit `{"gap": true, "earliest_retained": N}` line before
the backfill (`control-protocol.md` §5).

*The root is growing.* Attest, then prune (§2a). Nothing removes anything
on a timer.

## 7. Upgrading dsl41 itself

Each prepared runtime has its final path under `/opt/dsl41/.venvs/`.
`/opt/dsl41/.venv` is the active symlink. Prepare and verify the candidate
with §1 before the maintenance window. Rehearse the current estate with
its installed executable, using the launcher's ordered files, properties,
timezone, timezone map and semantics. Read the compatibility table below
before switching. Atomic activation uses §1's link replacement.

Never install into the active link or move a backing venv. To adopt this
layout from an existing `/opt/dsl41/venv` installation, leave that directory
at its original path. Prepare a new candidate from the exact old release
kit, or the qualified upgrade release. During the appropriate window,
change the reviewed launcher to `.venv`, activate the candidate, reload
changed units, and start. The original directory remains usable for
rollback until the retained-estate policy permits its separate removal.
A process started before activation keeps its original interpreter.

**What moves with a release.** `catalog_hash` v2 excludes
`meta.tool_version`, and `dsl41_version` is not on the `segment` record,
so a release does not move a period's pins (DL-133; period-model §1.1).
The `leader` record names the tool version, but resume gates on catalog
hash, clock domain and runtime profile, not on the version. So resume
does not refuse a new release. Only the release note says whether resuming
across the pair is safe. The **state-machine version** is different. One
executable implements exactly one. A seal whose `next_period` names
another is refused at readiness (period-model §2.1), and resume refuses a
segment pinned to another. Run any check against a retained runtime with
`python -B`. Check it in both venvs, whatever the note says:

```sh
/opt/dsl41/.venvs/<runtime-id>/bin/python -B -c \
    'from dsl41.runner_ledger import STATE_MACHINE_VERSION as v; print(v)'
```

**When resume stops on a replayed input.** Resume replays the period's
log through this build. If a logged input raises on replay, resume
refuses with exit 2 and names the input: `resume stopped: replay stopped
at input N (KIND from SOURCE at AT, request_id ID)`. The same build
raises at the same input on every resume, so a restart cannot clear it
(period-model §11). A replay stop may name an effect instead of an input
(a doubled or unknown outcome): it exits 2 with the same advice. Which
case it is decides the way out:

- **The same build wrote the log.** The fault is in this release. Deploy
  a release that fixes it, then resume. The estate stays down until that
  release exists. No tool skips or rewrites a logged input.
- **An upgrade is replaying an older release's log.** The new release
  cannot replay what the old one wrote. Flip the venv back to the release
  that wrote the log, and resume with it.

Most faults never reach this point. A command whose apply raises is
refused before it is journaled (`apply_faulted`), and an engine-made input
whose apply raises is quarantined or stops the engine with exit 6 before
it is journaled (concurrency-model §4). An input journaled with no
decision, the crash window, is quarantined by replay the same way when
its apply raises and its timers apply on their own; resume journals that
decision, prints the `dsl41: input quarantined` line and runs on. So this
case needs a journaled decision and a fault a code change introduced, or
an input journaled with no decision whose timers raise. A quarantine
journaled by one
release replays on any release with the same state-machine version, so
it does not block a rollback to a release without the fault.

One such fault has its own message: `an instant cascade reached the item
cap of C for an estate of N jobs; its root item was EVENT (CAUSE) on JOB`.
It appears in the quarantine line or the refusal, and after
`CascadeDepthError:` when resume stops. It means one input's cascade did
not end, or grew past what the engine can run. The oracle stops each root
dispatch of a cascade before it takes four times the square of the
estate's job count in items, never below 100,000 and never above 400,000
(DL-351): the floor binds up to 158 jobs, the square up to 316, the
ceiling above. The count is the same live and on replay. Up to 400 jobs
the largest legitimate cascade measured takes under a third of the cap;
near 720 jobs a dense one can reach it, and is then refused or
quarantined. A log that an older build wrote can hold such an input;
resume then stops with a replay fault that names it. The dry
apply runs on the engine's loop, so a long cascade also stops the lease's
renewal (§3, the deadman). On an M-series laptop a cascade that does not
end stops in about 4.5 s at 200 jobs and 15 to 17 s at 400 to 1,000 jobs,
under half the lease's lapse; a legitimate dense cascade takes about 7 s
at 400 jobs, 33 s at 700 and 90 s at 1,000. The log is not corrupt.
Report it with the message. A deep condition can still pass
Python's recursion limit, with the message `RecursionError: an instant
cascade passed the recursion limit of N; its first start was JOB (CAUSE)`.

**Pick one row (DL-266).** Read the release note: the annotated tag's
message (README "Release"). Apply these questions in order, and stop at
the first that picks a row:

1. Did the state-machine version change? Check both venvs as above,
   whatever the note says. Yes: row 4.
2. Does the note mark the release resume-safe? No, or the note is silent
   or unclear: row 2, the conservative default, whatever else changed.
3. Does the note say that neither the wrapper spec nor the supervisor
   protocol changed? Yes: row 1. The note names a change to either, or
   says nothing about them: row 3.

| Row | Use it when | Opener | Supervisor | Rollback |
| --- | --- | --- | --- | --- |
| 1 | resume-safe; the note says the wrapper spec and the supervisor protocol did not change; same state-machine version | stop the engine, flip, `run --resume` on the same root | kept; detached jobs keep running | flip back, resume |
| 2 | the note does not mark the release resume-safe, or is silent or unclear; same state-machine version | at a boundary, the next period opens in a fresh run root (§6a's physical roll); the lineage and its anchor are kept | a new one on the new root, from the new venv | flip back, plus another fresh root |
| 3 | resume-safe; the note names a wrapper spec or supervisor protocol change, or does not say; same state-machine version | drain detached work, stop both units, flip, start both; the engine resumes the same root | replaced | flip back, replace again |
| 4 | the state-machine version changed, whatever the note says | drain, a final seal, then a new estate (genesis on a new root and anchor) | replaced | the old venv only, on the old estate; keep it to audit the retained periods |

The commands below are for shape 1 and the example's paths (§3). Under
shape 2, stop and start the engine where a row stops and starts the engine
unit. Unmanaged shape 2 cannot prove safe maintenance and needs §3's incident
procedure where a row stops its supervisor. A starting engine starts a supervisor from its own venv
when none is running.

**Row 1, resume-safe, with no wrapper or supervisor protocol change.**
Detached jobs stay alive under the supervisor unit, and the new engine
reattaches to them:

```sh
systemctl stop dsl41-engine.service
# activate the verified candidate with the atomic link replacement in §1
systemctl start dsl41-engine.service     # the launcher passes --resume
```

The rollback is the same three commands with the old venv. Up to 1.8.0,
no release pair qualifies for this row. The service drill
(`drill-steps.sh upgrade-resume-safe`) runs these commands with two
installs of one build. That proves the mechanics, not that any version
pair is resume-safe.

**Row 2, a note that does not say resume-safe.** Work at a boundary.
First hold the scheduled jobs and let running work finish (§6's window,
steps 1 and 2). Take the reboot hold (§2b). Then run §2b's shape-1
steps with the old venv. An old venv without `estate quiesce` runs them
one by one: stop the engine unit, the offline seal and `dsl41 audit`
(§6a) while the supervisor runs, then `supervise shutdown` (§3), and
check that neither unit comes back. A physical roll needs a closing period
with no live execution and an attested seal (§6a). Then open the next
period in the new root:

```sh
# activate the verified candidate with the atomic link replacement in §1
# edit the launcher's RUN_ROOT to the new root, and both units'
# RequiresMountsFor= to match; ESTATE_ANCHOR stays the lineage's anchor
systemctl daemon-reload
sudo -u dsl41 touch /srv/dsl41/runs/<new>.open-from   # the one-shot open trigger
systemctl start dsl41-engine.service
systemctl is-active dsl41-engine.service              # active
dsl41 query status --brief -S /srv/dsl41/runs/<new>/control.sock
systemctl enable dsl41-supervisor.service dsl41-engine.service   # release the reboot hold (§2b)
```

`<RUN_ROOT>.open-from` is the launcher's one-shot open trigger. While it
exists, the engine mode passes `--open-from ESTATE_ANCHOR`. The launcher
removes the trigger just before it runs dsl41, so every later start of
the unit resumes the new root. The engine's journal says
`opened period N in <new root>`. The opener runs inside the engine unit,
and `Requires=` starts the supervisor unit on the new root first. Under
shape 2 the opener starts its supervisor inside the engine unit, as every
shape-2 start does. A shape-2 wrapper must carry the same one-shot open
mode as the example launcher, and its refusal of a genesis against an
existing anchor. Without the first, the opener runs by hand, outside the
unit. Without the second, a crashed opener restarts into a genesis. If
the open fails, the unit stays failed. Fix the cause, create the trigger
again and start the unit. That reruns the identical opener,
which a roll that stopped after its sentinel needs (§6a). If the opener
died before its sentinel, the unit's restart finds no trigger and no
sentinel, and the launcher refuses a genesis against the existing anchor
with exit 2, so the unit stays down. Create the trigger again and start
the unit. On either path, release the reboot hold once the engine answers.

A supervisor serves one run root, so the new root gets its own, from the
new venv. The holds cross the roll; release them with `OFF_HOLD` once the
engine answers. The rollback is the same procedure with the old venv and
another fresh root. The service drill runs this row and its rollback
(`upgrade-fresh-root`, `upgrade-fresh-root-rollback`) with two installs
of one build.

**Row 3, resume-safe, with a wrapper spec or supervisor protocol
change.** The engine and the supervisor are deployed together, never one
at a time (`docs/protocol-evolution.md`). Restarting only the engine
would leave the old supervisor launching the old wrapper. Hold the
scheduled jobs and let running work finish (§6's window, steps 1 and 2).
LIST can help monitor the drain; only guarded maintenance proves idle. Then:

```sh
set -eu
systemctl stop dsl41-engine.service
sudo -u dsl41 /opt/dsl41/.venv/bin/dsl41 supervise shutdown --run-root <root> || exit "$?"
# activate the verified candidate with the atomic link replacement in §1
systemctl start dsl41-engine.service     # Requires= starts the supervisor first
```

Release the holds with `OFF_HOLD` once the engine answers. The rollback is
the same with the old venv. The service drill runs these commands and the
rollback (`upgrade-coordinated`, `upgrade-coordinated-rollback`) with two
installs of one build, and checks that both units run from the flipped
venv and that the engine resumed.

**Row 4, a state-machine version change.** The new build cannot open the
old estate's periods, and only the old venv can audit them (§6a). Drain
as for row 3. Take the reboot hold (§2b). Then run §2b's shape-1 steps
with the old venv: stop the engine unit, a final seal and audit while the
supervisor runs, then guarded maintenance. An old venv without `estate
quiesce` runs them one by one: the offline seal and `dsl41 audit` (§6a),
then `supervise shutdown` (§3). Then start a new estate:

```sh
# activate the verified candidate with the atomic link replacement in §1
# edit the launcher's RUN_ROOT and ESTATE_ANCHOR to the new estate's,
# and both units' RequiresMountsFor= to match
systemctl daemon-reload
systemctl start dsl41-engine.service     # a genesis, no --resume
systemctl enable dsl41-supervisor.service dsl41-engine.service   # release the reboot hold (§2b)
/opt/dsl41/.venvs/<old-runtime-id>/bin/dsl41 audit --estate-anchor <old-anchor>
```

If the genesis fails, fix the cause and start the unit again before you
release the reboot hold.

A new estate carries no state: holds, globals and latches start empty, as
in §6's fresh run root. Keep the old venv for as long as you retain
the old estate's periods. The rollback is the old venv only, on the old
estate. Stop both units and flip back. Point the launcher's `RUN_ROOT`
and `ESTATE_ANCHOR` back at the old estate's, and both units'
`RequiresMountsFor=` to match; run `systemctl daemon-reload` if a unit
file changed. A rollback to a runtime without `dsl41 estate args`,
one before 1.13.0, reinstalls the site package qualified with that
release, launcher and units included, because this launcher refuses such
a runtime (DL-407). A rollback to 1.13.0 or later keeps the launcher.
Then start the engine unit. `Requires=` starts the
supervisor unit first, and the engine opens the old estate's next
period. A rollback after a failed genesis still holds the estate down
across reboots: once the old estate answers, release the hold with
`hold-release` (§2b), or the next boot leaves it stopped. The service
drill runs this row from v1.7.0, installed from PyPI, to the build under
test
(`upgrade-old-release`, `upgrade-state-machine`), and then the rollback
to the old estate and back to the new one
(`upgrade-state-machine-rollback`).

## 8. Operator scenarios

One row per situation an operator meets, with the verbs that exist. A
row that says "not built" names where the plan is recorded. Every
control-plane intervention is an adjusting entry: recorded in the WAL,
attributed to its actor, replayed identically, never an edit to what is
already written. `supervise shutdown` is the one exception: it
speaks to the supervisor, not the engine, and leaves no WAL record.

### Installation and lifecycle

| scenario | procedure |
| --- | --- |
| initial install, one host | §1 to §3 |
| engine version upgrade | §7's questions pick one row. A `state_machine_version` change is always a new estate (period-model §2.1). A note that does not mark the release resume-safe, or is silent or unclear, opens the next period in a fresh run root at a boundary. A resume-safe release resumes the same root; it keeps the supervisor only when the note says the wrapper spec and the supervisor protocol did not change, and otherwise replaces both units together |
| OS patching, host maintenance | `host drain` lets running work finish while the engine keeps leading (control-protocol §3). With one executor row the engine's own host is the executor, so patching that host is a stop and a resume (§3). A second executor is not built (DL-230): there is no flag for an `executor_id` (`--as-machine` names the machine identity only), and `journal` reads the one local executor |
| several estates on one host | each estate is its own run root, control socket, `serve` port and capacity pool; nothing is shared between them |
| standby provisioning, readiness check | not built: follower mode and `standby check` (DL-230) |
| estate decommission | §0's retirement procedure: disable the units, a final seal and audit, stop, keep the lineage under the site's retention decision, delete only whole sets (DL-268). There is no retire verb |

### Estate content

| scenario | procedure |
| --- | --- |
| initial JIL release | `dsl41 release prepare NAME DIR`, then write NAME to `ACTIVE` before the first start; native genesis opens period 1, and there is no opening seal (§3; DL-407) |
| incremental change, emergency hotfix mid-cycle | `dsl41 release prepare NAME DIR`, then `dsl41 release apply NAME` ([the job recipe](#recipe-add-change-or-remove-a-job)): seal, classified diff, open under the new catalog (§6a; DL-131, DL-133). The R-gate refuses only while something in the changed closure is live. Tethered mode drains, because a transition is a restart |
| rollback | `dsl41 release rollback`: a transition back to the previous release (§6a). A fresh run root is an infrastructure act (§6) |
| calendar or holiday change | `dsl41 release prepare NAME DIR`, then `dsl41 release apply NAME`: a catalog change, so firing dates move, the hash moves and §6a applies |
| properties or placeholder change | `dsl41 release prepare NAME DIR`, then `dsl41 release apply NAME`, for a release whose own layer changes: a catalog change, because it changes the post-placeholder JIL, so the hash moves and §6a applies |
| affinity role remap | not built: the `route` verb is specified in period-model §2.2 |

### Running the cycle

| scenario | procedure |
| --- | --- |
| ordinary night | §5 |
| closing the books | `dsl41 seal` at the estate's own cutoff, in the estate's own zone (§6a, SEM-35). Each boundary is an operator act; automatic sealing on a timer is a period-model §12 non-goal. The cadence is E16 (runner-design §15) |
| cutoff with live runs | the seal waits out an unresolved KILL ladder and carries every live execution; the opener reconciles them in the new period (period-model §3.3, §7) |
| missed ticks over downtime | skip-and-report (E9): resume drops each missed tick and journals a `drop` record; nothing fires late (runner-design §15) |
| deliberate catch-up after downtime | explicit `FORCE_STARTJOB`s; the period's `drop` records say what was skipped |

### Investigation

| question | procedure |
| --- | --- |
| why has X not started? | the `explain`, `deps`, `timers` and `plan` queries (§4; control-protocol §4) |
| post-hoc, current period | `dsl41 journal` replay (§4) |
| post-hoc, closed period | the closed book (period-model §12a): the seal chain, the inputs unless archived, and the period's own catalog bundle stored in its root. `dsl41 journal` replays from the stored bundle with no checkout |
| across a physical roll | one ledger per estate: the registry names every root and `journal`, `audit` and `runs` cross them (DL-141) |
| what ran, when, under which definition, on whose authority | the closed book and `dsl41 runs` answer the first three. Authority is the authenticated principal when the access map is armed (access-model), the caller's claim otherwise |
| is this job degrading? | `dsl41 runs --job X`: the series breaks where the job's definition moved (runner-design §7) |
| what did last night cost, per box or per wave? | export the `dsl41 runs` rows and aggregate them outside dsl41. A row carries no period key and no wave key |

### Manual intervention

| intervention | verb | reversible | note |
| --- | --- | --- | --- |
| status correction | `CHANGE_STATUS` | no: it is history | takes `expect`; a ghost is legal for `JOB^INST` (SEM-07) |
| force a run | `FORCE_STARTJOB` | no | |
| hold or ice for a window | `ON_HOLD`, `ON_ICE` | yes | an iced predecessor satisfies its dependents' atoms, a held one does not (SEM-05) |
| kill a runaway | `KILLJOB` | no | kill members, not boxes |
| set a global | `SET_GLOBAL` | by another set | survives a boundary |
| drain or activate an executor | `host drain`, `host activate` | yes | asserts nothing about reachability |
| evict an executor | `host evict` | no | gated on concurrency-model §8's preconditions |
| break glass: forced eviction | `host evict --force` | no | the one path that can double-run; authenticated when the access map is armed, attributed otherwise |
| idle supervisor maintenance | `supervise shutdown` | no | stopped engine, verified lock exclusion and dedicated service containment; active or indeterminate work refuses (DL-390) |
| deliberate supervisor termination | `supervise shutdown --force=terminate` | no | TERM/grace/KILL for validated command groups; uncertainty is not completion or permission to retry (§3) |
| break glass: reclaim a lineage | `estate reclaim --force` | no | the one path that can fork a lineage (period-model §1.3) |

Break-glass survives the seal as facts: a forced eviction's `forced_by`
rides on the carried host row, a forced boundary is `force_seal` on the
`seal` record with the gate's numbers in `forced_gate`, and every
undelivered effect is in `outbox_pending`. The perimeter's own receipts
(`privileged_admitted`, best effort, unsynced) go to
`<run_root>/perimeter.jsonl` and never enter the WAL, because a policy
decision is not an engine input (access-model §6); the admission stands when
the receipt write fails. `supervise shutdown` goes to the owner-only
supervisor socket and emits no perimeter receipt. There is no
acknowledgement latch, and whether a second pair of eyes is required on
break-glass is the site's control framework's call, not dsl41's.

### Failover

An engine crash on the same host resumes inside the open period
(runner-design §7). Everything across hosts or sites, the planned site
switch, an unplanned primary loss, a database failover under a live engine,
a partition, and failback, waits for the relay that concurrency-model §7
names and is not built (DL-189, DL-230).

Never activate a standby box on a live estate. Activation is unsafe
until each box has its own executor id in the ledger, every re-launch
path holds an effect whose executor is not its own, and the dead
executor is evicted before the resume dispatches. Until then, two boxes
can launch the same run. A standby will also need the active box's
exact `--as-machine`, declared at genesis on both boxes. A run root's
identity is its absolute path (DL-224), and the anchor names each root
by it, so a standby must see every run root at the same path and have
the anchor directory. A run root kept on the lost box's own disk takes
its spool with it: a period that spans the failover then cannot be
attested, physically rolled, or pruned past. Before a standby acts, it
must wait out the eviction bound of concurrency-model §8: the deadman
(DL-95) plus the kill allowance (DL-151) and the clock drift. So the
active box runs with a deadman (DL-307). The documented restore (§2b)
is not affected: it starts from a quiescent backup on a stopped estate.
