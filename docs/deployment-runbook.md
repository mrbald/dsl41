# Deployment runbook — installing and operating dsl41 on a server

Scope: a single host running one engine per estate (the runner's machine
model, DL-49/DL-52 — jobs pinned elsewhere are refused as foreign). Four
components ship in one package:

| Component | Process | How it runs |
|---|---|---|
| engine (runner + calendar scheduler + control socket) | `dsl41 run` | one long-lived foreground process per estate |
| supervisor | spawned by `dsl41 run --detached` | one per run root, outlives the engine |
| TUI | `dsl41 ui` | thin client of the control socket, attach/detach at will |
| web UI | `dsl41 serve` | thin client; one `dsl41 ui` subprocess per browser session |

Everything below assumes a POSIX server with Python ≥ 3.12 on it.
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
you in its receipts ([configure](#recipe-configure-an-estate)).

The recipes name the launcher's configuration through these variables.
Set them once per shell. The values are the example launcher's:

<!-- recipe: operator-env -->
```sh
RUN_ROOT=/srv/dsl41/runs/nightbank-01
ESTATE_ANCHOR=/srv/dsl41/runs/nightbank-01.anchor
ESTATE=/srv/dsl41/nightbank/estate/small
PROPERTIES=/srv/dsl41/nightbank/night/night.properties
S=$RUN_ROOT/control.sock
```

Some blocks are marked as recipes in this file's source.
`tests/test_operator_recipes.py` runs the job recipe, the readiness wait
and the retirement's audit and list as written, against a synthetic
estate, with the engine unit's `ExecStart=` in place of `systemctl
start`. It runs the sealed check and the torn-opening recipe where `jq`
is installed. The service
drill (§3's worked example) runs the service and retirement recipes as
written; CI checks their content but does not run them. The variables
block is compared with the launcher.

### Task index

| Task | Where |
| --- | --- |
| install the package | §1 |
| install, configure and start as a service | [the service recipe](#recipe-install-and-start-as-a-service) |
| configure identities, the execution profile, socket exposure and access | [the configure recipe](#recipe-configure-an-estate) |
| add, change or remove a job | [the job recipe](#recipe-add-change-or-remove-a-job) |
| stop, restart, seal or recover | [the stop recipe](#recipe-stop-restart-seal-and-recover) |
| watch an estate | [what to watch](#what-to-watch) |
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
├── dsl41-supervisor.service    User=dsl41  Restart=always  RestartPreventExitStatus=2
│     ExecStart=/opt/dsl41/bin/dsl41-launch supervisor
│     ExecStartPost=/opt/dsl41/bin/dsl41-launch supervisor-ready
│     └── dsl41 supervise start --run-root RUN_ROOT                 the supervisor
│           ├── owns RUN_ROOT/supervisor.sock, supervisor.pid, supervisor.lock, supervisor.log
│           └── one wrapper per detached run, and the run's command, in this unit's cgroup;
│               they write RUN_ROOT/runs/ and the job's output
│
└── dsl41-engine.service        User=dsl41  Restart=on-failure  RestartPreventExitStatus=2 3 5
      Requires=dsl41-supervisor.service  After=dsl41-supervisor.service
      ExecStart=/opt/dsl41/bin/dsl41-launch engine
      └── dsl41 run ... --run-root RUN_ROOT --estate-anchor ESTATE_ANCHOR --detached   the engine
            ├── holds RUN_ROOT/leader.lock and ESTATE_ANCHOR/anchor.lock for its whole life
            ├── writes RUN_ROOT/wal/, seals/, periods/, catalogs/, perimeter.jsonl
            │   and ESTATE_ANCHOR/anchor.json
            ├── serves RUN_ROOT/control.sock to dsl41 query, sendevent, seal, ui and serve
            └── asks the supervisor to spawn and kill over RUN_ROOT/supervisor.sock

storage                               owner and mode              written by
/opt/dsl41/venv -> venv-<ver>         root                        the installer (§1, §7)
/opt/dsl41/bin/dsl41-launch           root, 0755                  the installer; reviewed like code
/etc/dsl41/nightbank-access.toml      dsl41, 0600                 the operator (§4)
ESTATE files                          root, read-only to dsl41    a checkout of a tag (§2, §6)
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
│     take the reboot hold, then seal and audit while the supervisor unit
│     runs, then stop that unit (§2b); release the hold at the end
│     (a retirement never does); stopping the supervisor unit ends every
│     running command
│
├── the engine unit failed with exit 3                               SEALED, NOT OPENED
│     the period is sealed and the next one waits; nothing runs until it opens
│     open it in place (start the engine unit) or in a fresh root (§7 row 2)
│     an enabled engine unit opens it in place at the next boot
│
├── the engine unit failed with exit 5                               TRANSITION STOP
│     --on-transition-violation stop halted it after a journaled decision (§3)
│     read the TRANSITION_VIOLATION line with dsl41 journal, then start the unit
│
├── new JIL, properties or run options, and the history continues   TRANSITION
│   ├── in the same run root                                         IN-PLACE TRANSITION
│   │     seal, then start the engine unit: it opens period N+1 (§6a)
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
them. §6's fresh-run-root cycle is a new estate too: its root gets an
anchor of its own.

### Recipe: install and start as a service

1. Install the package (§1). The launcher's `DSL41` is
   `/opt/dsl41/venv/bin/dsl41`, so install there, or follow §7's venv
   layout from the start.
2. From a checkout of the repository at the release's tag (the wheel does
   not ship the examples), create the service account and its writable
   directories, and install the launcher, the access map and the units:

<!-- recipe: service-install -->
```sh
useradd --system --user-group --create-home --home-dir /var/lib/dsl41 \
    --shell /usr/sbin/nologin dsl41
install -D -m 0755 examples/nightbank/deploy/dsl41-launch /opt/dsl41/bin/dsl41-launch
install -d -m 0755 /srv/dsl41
install -d -o dsl41 -g dsl41 -m 0700 /srv/dsl41/runs
install -d -m 0755 /etc/dsl41
install -o dsl41 -g dsl41 -m 0600 examples/nightbank/deploy/nightbank-access.toml \
    /etc/dsl41/nightbank-access.toml
install -m 0644 examples/nightbank/deploy/dsl41-engine.service \
    examples/nightbank/deploy/dsl41-supervisor.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable dsl41-supervisor.service dsl41-engine.service
sudo -u dsl41 /opt/dsl41/bin/dsl41-launch --print
```

3. The service account writes the run roots and their anchors under
   `/srv/dsl41/runs`, and whatever its jobs write. A job's data
   directories and its `std_out_file` targets must be the account's:
   nightbank's jobs write under `/srv/dsl41/nightbank/night`, which the
   drill creates owned by the account. Put the estate checkout where the launcher's
   `ESTATE` names it, owned by root and read-only to the account (§2). The
   access map must be owned by the account and writable by no one else,
   in a directory owned by root or the account and writable by no one
   else (access-model §4).
4. Edit the installed copies, not the checkout: the launcher's
   CONFIGURATION block, and the run root in both units'
   `RequiresMountsFor=`. Run `systemctl daemon-reload` after a unit edit.
   `dsl41-launch --print` shows the command the engine unit will run. It
   names every estate file, in order, every `-p` and every run option.
5. Start:

<!-- recipe: service-start -->
```sh
systemctl start dsl41-engine.service
systemctl is-active dsl41-supervisor.service dsl41-engine.service
```

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
   add every person who may reach the socket. The engine then opens the
   run root to `0710` and the socket to `0660` for that group
   (access-model §8); the bindings still decide each member's tier.
   Restart the engine unit after a `socket_group` change: a reload that
   names another group is refused (access-model §7). The web UI listens
   on loopback; front it with TLS and authentication (§4).
3. **Execution profile.** The launcher's run options are the runtime
   profile: `--detached`, `--as-machine`, `--machine-policy strict` and
   `--timezone`, plus `--timezone-map` and `--deadman` when used (§3, §5).
   `--as-machine` is the name the JIL's `machine:` values resolve to. The
   resume gate refuses a changed profile with exit 2. Change one only at
   a boundary, with the matching `--next-*` options (§6a).
4. **Access check.** After a start, as a member of the socket group with
   a binding, `dsl41 query status --brief -S "$S"` answers. As a member
   with no binding it is refused, and `perimeter.jsonl` gains an
   `access_denied` record. Each start's `policy_loaded` record names the
   map's digest (access-model §6).

### Recipe: add, change or remove a job

A job changes through a complete catalog and a boundary, never through an
edit of a running estate (§6). Every estate file holds whole
definitions: the `insert_` forms and the calendar statements. AutoSys
delta forms (`update_job`, `delete_job`, `override_job`) are refused by
lowering (DL-29): `dsl41 lint` exits 2 on them, and no estate can load
them. Add a job by adding
its `insert_job` block. Change one by editing its block. Remove one by
deleting its block. A job that a remaining condition still names is a
lint error (L001).

1. Commit the edit to the estate's repository and tag it. If it adds or
   removes a file, edit the launcher's file list and the `--next` list
   below to match, in the same order.
2. Before the window, check the new tag in a checkout of its own, with
   `ESTATE` pointing at that checkout. Fix every ERROR:

<!-- recipe: job-check -->
```sh
dsl41 lint "$ESTATE/amer.jil" "$ESTATE/apac.jil" "$ESTATE/calendars.jil" \
    "$ESTATE/emea.jil" "$ESTATE/global.jil" "$ESTATE/infra.jil" -p "$PROPERTIES"
dsl41 rehearse "$ESTATE/amer.jil" "$ESTATE/apac.jil" "$ESTATE/calendars.jil" \
    "$ESTATE/emea.jil" "$ESTATE/global.jil" "$ESTATE/infra.jil" -p "$PROPERTIES"
```

3. In the window, hold the scheduled jobs and let running work finish
   (§6, steps 1 and 2). `JOB` below is the job's name. A job you change
   or remove must not be live:
   `dsl41 query status --job "$JOB" -S "$S"` must show no `STARTING`,
   `RUNNING` or `QUE_WAIT`, `armed` false and no `pending_timers`.
   Otherwise the seal refuses a removed job that is executing or latent
   (armed, `QUE_WAIT`, a live timer) and a changed job that is
   executing. A changed latent job crosses with a recorded assumption
   (period-model §10.1, §10.3). A
   seal also refuses inside the retry horizon after the last operator
   request, a hold included (§2b); wait it out.
4. Set `ESTATE` back to the launcher's value, the live checkout: the
   seal records the paths it reads, and the opener reads the launcher's.
   Swap the live checkout to the new tag (`git -C "$ESTATE" checkout
   <tag>`) and seal at once. Between the swap and the seal, a crash
   restart refuses with exit 2, because the files no longer match the
   running catalog. The `--next-*` options restate the launcher's run
   options; they do not inherit them (§6a):

<!-- recipe: job-seal -->
```sh
dsl41 seal --run-root "$RUN_ROOT" --estate-anchor "$ESTATE_ANCHOR" \
    --next "$ESTATE/amer.jil" --next "$ESTATE/apac.jil" --next "$ESTATE/calendars.jil" \
    --next "$ESTATE/emea.jil" --next "$ESTATE/global.jil" --next "$ESTATE/infra.jil" \
    -p "$PROPERTIES" \
    --next-detached --next-as-machine localhost --next-machine-policy strict --next-timezone UTC
```

   The live engine exits 3, and the engine unit stays failed on purpose.
   Exit 4 is an unknown outcome: follow §6a's Day 2 before anything else.
5. Open the next period in place:

<!-- recipe: job-open -->
```sh
systemctl start dsl41-engine.service
```

   The launcher passes `--resume`, and the engine opens period N+1 in the
   same run root under the new catalog. Wait for it with the
   `wait-answers` block of the service recipe.
6. Verify. An added or changed job answers with the definition this
   period loaded:

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

7. Release the holds with `OFF_HOLD`. A rollback is the same recipe with
   the previous tag.

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
  reboot hold, then follow §2b's shape-1 steps: stop the engine unit, seal
  and audit while the supervisor unit runs, stop the supervisor unit, and
  check that neither comes back. To start again, start the engine unit
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
2. Stop both units (`systemctl stop dsl41-engine.service
   dsl41-supervisor.service`), or under shape 2 the supervisor (`dsl41
   supervise shutdown --run-root "$RUN_ROOT"`). Then run §2b's
   no-writers check, with its wait past `RestartSec` under shape 1.
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
| engine unit | `systemctl show -p ActiveState -p ExecMainStatus -p NRestarts dsl41-engine.service` | `active` runs. `failed` with status 2 is a configuration refusal; the journal names it. `failed` with status 3 is a sealed period (next row); it lasts until the next start, and an enabled unit starts at boot. `failed` with status 5 is `--on-transition-violation stop` (§3); the journal says `engine stopped:` and names the transition. A rising `NRestarts` is crash restarts (exit 1); the journal says `engine failed:` and why |
| sealed, not opened | the check below, on `ESTATE_ANCHOR/anchor.json` | `closed`: a period is sealed and the next is not open, so nothing runs. Alert when it stays `closed` past the window. `open` is normal. `claimed` is an opener at work, or one that crashed (§6a, Day 2) |
| supervisor unit | `systemctl show -p ActiveState -p NRestarts dsl41-supervisor.service`; `dsl41 supervise list --run-root "$RUN_ROOT"` | the list answers `"ok": true` while the supervisor is up, and shows each run's `wrapper_alive`. A restart of this unit ended the commands it ran |
| supervisor log | `$RUN_ROOT/supervisor.log`; `journalctl -u dsl41-supervisor.service` | the supervisor's own output, and the unit's starts and stops |
| leader | `$RUN_ROOT/leader.lock` holds the last leader's `pid`, `host`, `epoch` and `since`; compare `pid` with `systemctl show -p MainPID --value dsl41-engine.service` | the note stays after the engine exits, so it says who led, not who leads. Never probe the lock with `flock`: an engine that starts while a probe holds it refuses with exit 2. Never delete or replace the file: the engine re-checks it before every append and stops when it changed |
| control socket | `dsl41 query status --brief -S "$S"` | exit 0: the leader answers. Exit 2: no engine, or a refusal |
| failures and alarms | `dsl41 query subscribe -S "$S"` as the wake-up; then `dsl41 query trace --since N -S "$S"` with the last `last_seq` read. The trace is per period and its `seq` restarts at 1: set the cursor to 0 when the answer's `baseline_id` changes, or when `last_seq` is below the cursor, as the TUI does (DL-210) | the stream carries journal records: a run's end arrives as an `input` record of kind `STATUS` from the `adapter` source. The trace names what it did: a transition to `FAILURE` or `TERMINATED`, or a `MUST_START_ALARM` or `MUST_COMPLETE_ALARM` entry (SEM-34). Alert on any `TRANSITION_VIOLATION` entry: an applied input broke a declared state-machine transition, and its `cause` names the transition (concurrency-model §4) |
| violations off the trace | `journalctl -u dsl41-engine.service` | alert on any line that begins `dsl41: transition violation`. It covers both kinds of violation the trace cannot carry: one in the engine's own machines (admission, the subscribe feed, the seal boundary), and one noted outside an input, which is dropped. The line names the machine or entity, the transition and the reason; the engine goes on (concurrency-model §4) |
| free space | `df -P "$RUN_ROOT" "$ESTATE_ANCHOR"`, and every file system a job's `std_out_file` or `std_err_file` writes to | see "when a write fails" below |
| perimeter receipts | `$RUN_ROOT/perimeter.jsonl`, when the access map is armed | alert on `access_denied` and `policy_reload_failed` records. `stream_revoked` marks a stream that a reload closed (access-model §6, §7) |

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

### Retiring an estate

There is no retire verb. Retiring ends a lineage: nothing will open its
next period. The procedure keeps the history by default; what to delete,
and when, is the site's retention decision (§2a).

1. Take §2b's reboot hold (`hold-down`: both units disabled), so a reboot
   inside the procedure starts nothing. The units keep running. A
   retirement never releases it.

2. Hold the scheduled jobs, and let running work finish or end it (§6,
   steps 1 and 2).
3. Seal, audit and stop, as §2b's shape-1 steps 1 to 6 do. The final seal
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
| supervisor log | `RUN_ROOT/supervisor.log` | the site. The supervisor appends to it and nothing in dsl41 reads it. Rotate with copy and truncate, or with the supervisor unit stopped |
| perimeter journal | `RUN_ROOT/perimeter.jsonl` | nobody on its own. Never truncate or rotate `perimeter.jsonl` on its own: truncating it restarts `access_seq`, and later receipts reuse identities that already exist (access-model §6). It goes only with its whole root |
| system journal | journald, for both units | the site's journald retention. It holds each start's launcher line and the engine's output |
| run roots and anchors | `/srv/dsl41/runs` | the site's retention decision, applied to whole sets (above); `estate prune` for what §2a licenses inside a root |


## 1. Install

Dedicated venv, pinned version, `[ui]` extra only where humans look.
Set the pin and the profile first:

```sh
ver=1.8.0
profile=ui                                            # headless host: profile=base
```

Releases through 1.7.0 carry no release assets, so the asset install below
applies from the next release on. For 1.7.0 and earlier, install from PyPI
with the pin:

```sh
python3.12 -m venv /opt/dsl41/venv
/opt/dsl41/venv/bin/pip install "dsl41[ui]==$ver"     # headless host: "dsl41==$ver"
```

`uv tool install "dsl41[ui]==$ver"` is the one-liner where uv is the site
convention. Both resolve the dependencies at install time instead of
installing the tested closure; keep the pin there too.

From the next release on, install what the release tested (DL-215). Its
GitHub release carries the wheel, the locked dependency closure per profile
with hashes, and `SHA256SUMS`. The closure goes in first with
`--require-hashes`, then the wheel with `--no-deps`, so pip resolves nothing
of its own:

```sh
base=https://github.com/mrbald/dsl41/releases/download/v$ver
mkdir -p /opt/dsl41/dl && cd /opt/dsl41/dl
for f in SHA256SUMS "requirements-$profile.txt" "dsl41-$ver-py3-none-any.whl"; do
  curl -fsSLO "$base/$f"
done
sha256sum -c --ignore-missing SHA256SUMS              # macOS: shasum -a 256 -c --ignore-missing
python3.12 -m venv /opt/dsl41/venv
/opt/dsl41/venv/bin/pip install --require-hashes -r "requirements-$profile.txt"
/opt/dsl41/venv/bin/pip install --no-deps "dsl41-$ver-py3-none-any.whl"
```

Either way, link the command and smoke-test the install:

```sh
ln -s /opt/dsl41/venv/bin/dsl41 /usr/local/bin/dsl41  # or add the venv bin to PATH
dsl41 --help                                          # smoke test
/opt/dsl41/venv/bin/python -c 'from importlib.metadata import version; print(version("dsl41"))'
```

The package installs no services and has no runtime network dependencies —
the engine is a foreground process you place under your init system. It
writes into the run roots you name and their sibling anchor directories
(§2). The one thing it writes elsewhere is job output: a job's
`std_out_file`/`std_err_file` is used verbatim and lands wherever the JIL
says.

The `[ui]` extra floors (`textual>=8`, `textual-serve>=1.1`) are tested
pairs (DL-46/47); do not force older ones.

## 2. Filesystem layout

```
/opt/dsl41/venv-<ver>/    one pinned install per release (§7)
/opt/dsl41/venv          a symlink to the venv in use; §1's first
                          install is a directory here until §7
                          converts it
/srv/dsl41/estate/        JIL + properties files — a git checkout of a tag,
                          never hand-edited in place
/srv/dsl41/runs/<id>/     a run root holds one period per baseline and
                          gains another at every in-place boundary; a
                          lineage spans several roots once it has rolled.
                          A fresh root is genesis or a physical roll
                          (§6, §6a)
```

The run root is created by `dsl41 run` and is self-contained: `journal.jsonl`
(the one-line sentinel),
`catalogs/<source_bundle_hash>/` (the post-placeholder JIL this
run actually loaded + `sources.json` with the original paths and the
sha256 of the stored — post-placeholder — text, which is not the checksum
of the file you passed when `-p` resolved anything in it), `periods/000001/manifest.json` (catalog hash and its
version, bundle address, the runtime profile and its hash, state-machine
version, and the period's own `baseline_id`/`first_index`), `runs/` + logs,
`control.sock`, and `supervisor.sock` when detached. The records live in
`wal/<segment_no>.jsonl` and `journal.jsonl` is a one-line sentinel
(DL-133, DL-134); the root also holds `seals/<period>.json`,
`seals/<period>.audit.json` and `periods/<period>/manifest.json`, and the
LINEAGE anchor lives OUTSIDE it, at `<run-root>.anchor` by default,
deliberately, so `tar`ing a root never carries the fence away with it.
Back both up. A run root with `manifest/` (DL-66) instead of `catalogs/` +
`periods/` is a retired layout (DL-138): it is refused by name, not read,
and there is no path from such a root into a lineage
(`docs/protocol-evolution.md`).
Run roots are `0700`; journals and job output are created `0600` — the WAL
carries globals and every control input, so keep the service account's
home to itself. `0600` is a CREATE mode: a `std_out_file` that already
exists keeps the mode it has, because appending is the vendor's
semantics. One thing widens the root: an armed access map that names a
socket group tightens every direct child to owner-only and opens the root
itself to `0710` traversal (§4). A run root is the audit artifact of its
night: retention is a business decision, not a cleanup script's — see
§2a.

## 2a. Retention — the floors, and the prune verb

An estate root (DL-135; period-model §11a and §12) grows: one WAL segment
per period, one spool directory per dispatched CMD
or FW run (a box gets none), a `runs/.by_run_id/<run_id>` entry for each
DETACHED CMD run, logs beside them. Nothing here removes any of
it on a timer.

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
| an ARCHIVED period's receipt, attestation and sidecar | *(DL-144.)* Its inputs are gone by policy. Delete the receipt and the absence reads as accidental LOSS and every reader refuses; delete either of the other two and the period has neither inputs nor proof |
| the WAL and spool of any unattested period | its `audit` has not run yet, and this is what `audit` reads |
| the spool of any live or carried execution | the run is not over |
| a SPAWN tombstone whose effect can still be replayed | "no index entry" means "first application", so deleting one **authorizes a second spawn** of a job that already ran |

Backing up a root means backing up the anchor too. It is a sibling of the
root and not inside it, so `tar czf root.tgz /srv/dsl41/runs/<id>` takes
the estate and leaves the fence behind.

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
deletes nothing. With `--dry-run` and no class named it surveys ALL of
them, so you can pick from what is there.
Without `--dry-run` and with no class named it deletes nothing and exits 2
saying so: a default set would be a retention policy, and the policy is
yours. Add `--estate-anchor` wherever the rest of your commands need it.

*(DL-141.)* Name
`--estate-anchor` **alone**, with no `--run-root`, and the sweep covers
every root the registry names, in period order, as one result (§6a). Each
root is still planned on its own: the floors, the refusals and the
descriptor the removal walks are per root, and so is `--keep-runs`, which
then keeps N per job **per root** — more than asked, never less.

The three verdicts:

- **floored** — the model refuses. The verb cannot be made to delete these,
  by any flag.
- **held** — the head has moved past it and a later checkpoint covers it,
  and no class licenses deleting it. Every held row says WHICH dependency
  is in the way, so a WAL that reads "no chain checkpoint above period 3
  covers it" is telling you to attest a later period. Older sidecars,
  older manifests and unreferenced bundles live here permanently in this
  version — the archive class does not cover them.
- **prunable** — deletion is licensed by name. Three classes: a SPAWN
  tombstone whose period is attested and whose run has ended
  (`--tombstones`: the run directory, its `.by_run_id` entry and its
  default logs, always together), a quarantined candidate
  (`--quarantine`), and an archivable period's INPUTS
  (`--archive-inputs`, below).

**`--archive-inputs`: the one deletion you cannot undo.** *(DL-144, closing
period-model PR-Q3.)* It deletes an attested period's WAL segment and its
committed `staged_manifest.json` + `candidate.json`, after writing
`seals/<period>.archive.json` — the **receipt** — durably first. Afterwards
the period reads at the **attestation-verified** tier: `dsl41 audit` says
so by name, `dsl41 journal` narrates an unreplayable gap and crosses to
the next period on the checkpoint, `dsl41 runs` names the coverage it no
longer has, and nothing can ever re-derive that period again. Restoring
the files does not undo it — the receipt governs.

The order is fixed and the verb tells you where you are in it:

1. `dsl41 audit --run-root <root>` for the period **and a later one** — a
   chain checkpoint above it is what stands in for the inputs. With no
   `--period` it audits every closed period the root holds;
2. `dsl41 estate prune --run-root <root> --tombstones` for that period's
   runs. There is no period selector: the sweep takes every eligible run
   in the root it is addressed at. Until they
   are gone the archive refuses and names what remains: the tombstone
   floor resolves a run directory to a period through the SPAWN effect in
   that period's WAL, so archiving the WAL first would strand every
   tombstone it explains, floored forever;
3. archive the OLDEST unarchived period first. The verb enforces this — the
   archived periods are a prefix of what a root retains, so the segments
   that remain are always contiguous and every segment-spanning reader
   keeps working.

If a segment goes missing with **no** receipt, that is loss and not an
archive, and every reader refuses by name rather than replaying a lineage
that quietly starts later than it did. That is the whole reason the
receipt is written before the first deletion.

**"Attested" is what unlocks a tombstone.** Run `dsl41 audit` (§6a) first.
Until a period is attested, its whole spool is floored, because that spool
is what `audit` re-derives the period from. After it is attested, that
period's finished runs may go — and once they are gone, the period can no
longer be re-derived from its own evidence, and its attestation is the
proof that stands for it. That is the trade, and it only goes one way.

What you lose is the PROCESS clock in `dsl41 runs`. With a spool the row
times a run by `spawn.json` and `status.json`; with the spool gone it
falls back to the journal's own `dispatch` record and terminal transition,
and the row says which, in `clock_source`. Start and end usually survive
the prune; their source changes. The row itself always stays — it is the
WAL's, not the spool's. `dsl41 journal` reads no spool while it replays
one period, but it does read the CLOSING period's spool at every boundary
it crosses: re-deriving that seal reads the executions the seal carried.

`--keep-runs N` keeps the N newest run spools **of each job**, and
`--older-than-days D` keeps anything touched more recently than D days.
Both are your policy, not the model's. Both filter whole runs: a directory
is never removed while its index entry stays.

`--keep-runs` is per job because `run_number` is per job. One list ranked
by run number would compare numbers from different series — a busy job's
fifth run outranking a quiet job's first — and `--keep-runs 3` would then
delete the quiet job's whole history.

A removal the filesystem refuses — a permission, a directory that vanished
under the sweep — is reported and the sweep goes on, and the rest of that
run stays with it. The exit code is 2 and the report names each artifact
that did not go.

The verb reads the anchor and never locks it, so it runs against a live
engine. It cannot reach that engine's work: everything a running period
creates belongs to a period that is not attested, and is floored for that
reason alone.

## 2b. Quiescent backup and restore

Everything below (DL-219; rehearsed by `tests/test_restore_drill.py`) is a
rehearsal, not a new verb: there is no `dsl41 backup` or `dsl41
restore`. Back up and restore with your own file copier, against the
inventory, the precondition and the constraints below.

**The inventory.** Four things, all of them:

- **the anchor directory** — `<run-root>.anchor` by default (§2). It is a
  sibling of the run root, so a copy that stops at the root's own tree
  leaves the fence behind;
- **every run root the retained lineage needs** — one per period-model
  §1.1, or several once a physical roll has moved the lineage to a fresh
  root. The anchor's registry names them (§6a); back up each one the
  registry still points at, not just the newest;
- **the retained evidence inside those roots** — seals, attestations,
  archive receipts, and the WAL and spool of any period not yet archived
  (§2a's floor table). This is not a separate step: it is what "back up
  the run root" already means, because none of it lives outside the root
  the bullet above names;
- **the deployment inputs** — the estate's JIL, the properties file the
  night ran with, and (if the estate uses one) the `--timezone-map` and
  `--access-map` files (§1, §2, §4). A restored root's next opener still
  loads its catalog from the JIL and properties by path, the way every
  opener does, and a resumed profile re-reads `--timezone-map` the same
  way; a DR host needs its own copy of the same checkout your estate
  directory holds, plus whatever run-specific properties, timezone-map or
  access-map file is not part of that checkout.

**The quiescence precondition.** Nothing may still be writing into what you
copy. Stopping the engine is necessary and not sufficient: a DETACHED
period's commands run under the supervisor, which outlives the engine by
design (runner-design §6a), so an engine that has exited still leaves
`supervisor.sock` and `supervisor.pid` behind, held by a process that can
still write into the run root. **The order matters and does not commute.**
How you stop each process depends on the deployment shape (§3). The seal
between the two stops does not (DL-266).

**Hold the estate down across reboots: the reboot hold** (DL-268). Under shape 1 the units
are enabled (§0's service recipe), and an enabled engine unit resumes the
root at every boot: on a sealed root that opens the next period in place.
A window that a reboot must not end takes the reboot hold first, before any
stop or seal, live or offline:

<!-- recipe: hold-down -->
```sh
systemctl disable dsl41-engine.service dsl41-supervisor.service
```

It ends by releasing the reboot hold, once the estate may start again, on the
failure and recovery paths too:

<!-- recipe: hold-release -->
```sh
systemctl enable dsl41-supervisor.service dsl41-engine.service
```

Three procedures take the reboot hold: this backup, §7's rows 2 and 4, and a
retirement (§0), which never releases it. A stop for a host reboot does
not take it: there the enabled units are what bring the estate back (§0's
stop recipe).

The seal is the same in every shape. Seal the period (§6a) and
`dsl41 audit` it, so what you back up is closed and attested rather than
open. Do this WHILE a detached period's supervisor is still up. The
offline `seal` command wires a DETACHED period's supervisor client exactly
as a live engine does (`wire_from_profile`). That client reconnects to a
supervisor that is still there. If none is, it SPAWNS A FRESH ONE, with no
deadman. Sealing after the supervisor is down therefore leaves a second,
unaccounted-for supervisor behind. A seal, live or offline, refuses inside
the closing period's retry horizon (period-model §9). The horizon counts
from the last operator request, an `ON_HOLD` included; wait it out. A live seal
stops the engine itself with exit 3, so it takes the place of steps 1 to
3 below. It does not replace the reboot hold: take the reboot hold before a live seal
as before an offline one.

**Shape 1, a supervisor unit of its own** (the example units):

1. with the reboot hold taken (above), stop the engine unit:
   `systemctl stop dsl41-engine.service`. The supervisor unit keeps
   running;
2. if the period ran detached, confirm the supervisor is still there:
   `dsl41 supervise list --run-root <root>` answers `ok` while it is;
3. seal offline and audit, as above;
4. stop the supervisor unit: `systemctl stop dsl41-supervisor.service`.
   Do not use `dsl41 supervise shutdown` here. It exits the supervisor
   cleanly, and `Restart=always` starts it again `RestartSec` later.
   Stopping the unit ends every command still running, so do it only
   once you want them ended. The engine unit `Requires=` this unit, so
   this stop also stops the engine unit if it still runs;
5. wait longer than the longer `RestartSec` of the two units (5 s in the
   example units; wait 12 s). Then check that neither unit came back:
   `systemctl is-active dsl41-engine.service dsl41-supervisor.service`
   must print `inactive` or `failed` for each, never `active` or
   `activating`. `activating` is a unit waiting to restart;
6. run the no-writers check below.

**Shape 2, and an engine outside any service manager:**

1. stop the engine (`dsl41 run`'s own SIGINT, or its unit);
2. if the period ran detached, confirm the supervisor is still there:
   `dsl41 supervise list --run-root <root>` answers `ok` while it is;
3. seal and audit, as above;
4. NOW stop the supervisor — `dsl41 supervise shutdown --run-root
   <root>`. It TERM→grace→KILLs anything still running first, so run it
   only once you want every live command ended, not merely observed, and
   only once the seal above no longer needs it;
5. run the no-writers check below.

A tethered period has no supervisor. Under shape 1 it skips step 2; the
supervisor unit still runs, so step 4 still applies. Under shape 2 it
skips steps 2 and 4.

**The no-writers check**, in every shape, before you copy anything: no
`supervisor.pid`, no `supervisor.sock`, no engine holding `leader.lock`.
`tests/test_restore_drill.py` asserts both files are absent at exactly
this point, with a supervisor outside any service manager. The service
drill runs shape 1's steps (§3's worked example). It waits past
`RestartSec`, copies the root and the anchor, deletes them, and restores
them at the recorded paths. It then audits the lineage and opens the next
period. Under shape 1, start a restored estate with
`systemctl start dsl41-engine.service`. Its `Requires=` starts the
supervisor unit first. Release the reboot hold once the copy is done, or once
the restored estate runs. If the start fails, fix the cause, start
again, and then release it.

**The path-equality constraint.** The anchor's registry names each
period's run root by absolute path (§6a; period-model §1.3). Restoring
the whole lineage — the anchor and every root — at a DIFFERENT absolute
path does not make those rows repoint themselves: they still name the
ORIGINAL path, which after a restore elsewhere holds nothing. For every
**estate-wide read** — `dsl41 audit --estate-anchor`, `journal`, `runs`,
`estate prune` — that is a **missing registered root**, the identical
refusal an incomplete restore produces, not a distinct failure mode; this
is what the drill checks. Restore each root at the SAME absolute path it
was backed up from, mounts included: a DR host needs the same mount
layout the original host had, at least for every path a run root or the
anchor can sit at. `boundary.claim_id_for` does hash the target root's
realpath into a physical roll's successor-claim digest, but only an
INTERRUPTED roll's claim recovery ever recomputes and compares it
(`test_nightbank_boundary.py`'s
`test_reclaim_frees_a_lineage_a_crashed_roll_left_claimed`); an ordinary,
already-completed period's resume never revisits it. Resume opens the
`--run-root` it is given, then checks that the anchor names that root, and
refuses a root it does not name (DL-224).

**Resume on a relocated copy is refused (DL-224, closing DL-219's open
item).** Resume applies period-model §1.3's resume rule:
the anchor must name the `--run-root` given, and the registry row for the
period of the root's newest OPENED segment must name it too. With no row for
that period yet, the head must be this root's own claim: that is the window
an opening crashed in between its segment and the head move, and resume
finishes it. A copy or a restore at another path is refused with exit 2,
whether it is resumed against its copied anchor or against the original
one. The message names the anchor, the root and the recorded root. If the
original engine holds the original's anchor at that moment, the refusal
says that the anchor is held by another process instead.
`dsl41 run --resume` and an offline `dsl41 seal` refuse such a root before
they repair or stage anything or wire a supervisor, so no supervisor starts.
That holds for a root that fails the rule when the command starts; if the
lineage changes under it, for instance by an `estate reclaim` run at the
same moment, the refusal can come after the supervisor is wired. A missing
anchor or another estate's anchor keeps its refusal and order.
There is no override: restore at the recorded path. A roll that stopped
before its claim is also refused; run the opener (`--open-from`) again. The
recorded path, a symlink left there that leads to the restored root, a
case-variant spelling of it on a case-insensitive filesystem and a bind
mount of it are the same directory and are accepted.
`tests/test_restore_drill.py` checks this refusal on the lineage restored at
the wrong path.

**What archived inputs cannot get back.** `estate prune --archive-inputs`
(§2a) is irreversible by design: once it has run, restoring an old copy of
the deleted WAL beside the receipt does not move that period back to
DERIVATION-verified (period-model §12a). The receipt governs, and
`dsl41 audit` reports ATTESTATION-verified for that period regardless of
what is on disk beside it (period-model §12a states the same for every
other reader — `journal`, `runs`, the estate walk — though this drill
checks only `audit`). Back up an archived period's receipt, attestation
and sidecar like anything else the registry needs; do not expect backing
up a stray copy of its deleted WAL to buy back the stronger tier.

**What this does not prove.** Restoring a lineage does not decide who may
run it. Nothing here checks that the ORIGINAL host is actually stopped for
good, or arbitrates between two copies of one estate both claiming to
lead it — that is an operational discipline outside the model, the same
way a physical roll while jobs are live is a non-goal (period-model §12).
Nor does it reconcile business effects a job produced after the backup was
taken and before the restore: a job's `std_out_file`, its produced files,
anything it wrote outside the run root, are not part of this inventory and
this section says nothing about recovering or replaying them.

## 3. Starting the engine

```sh
dsl41 run /srv/dsl41/estate/*.jil \
    --run-root /srv/dsl41/runs/<id> \
    --detached \
    --as-machine <name-your-jils-use> \
    [--timezone-map tz-aliases.json] [-p site.properties]
```

In a unit or launcher, name the files explicitly rather than by glob: a
shell glob's order is the locale's, and the file order is part of the
catalog hash (the worked example below does this).

Decisions to make once, per site:

- **Tethered vs detached.** Tethered (default): engine death kills all
  jobs, durably recorded — simplest, right for dev and for estates where
  a dead engine should mean a dead night. `--detached`: jobs run under a
  per-run-root supervisor; engine restarts reattach (`--resume
  --detached`) instead of killing — the production default. Inspect with
  `dsl41 supervise list --run-root <root>`; `dsl41 supervise shutdown
  --run-root <root>` is the break-glass kill-everything — for a *stopped*
  engine: it must acquire the supervisor's fencing lease, and a live engine holds it (the
  refusal names the holder). Stop or kill the engine first. There is no
  TTL to wait out: the supervisor reads the closed connection as proof
  the holder is gone, so the lease is grantable at once.
- **Transition violations.** `--on-transition-violation` says what the
  engine does when an input breaks a declared state-machine transition
  (concurrency-model §4). `refuse`, the default, refuses such a command
  before it is journaled, with the code `transition_violation`. A command
  whose apply raises is refused with `apply_faulted` under every value.
  An input the engine made itself, a tick or a completion, is applied and
  leaves a `TRANSITION_VIOLATION` trace line (§0's "What to watch").
  `continue` also applies such a command, with its trace line. `stop`
  refuses like `refuse`, and exits 5 once an input that broke a
  transition has its decision journaled. The unit does not restart exit
  5 (below). Read the `TRANSITION_VIOLATION` line with `dsl41 journal`,
  then start the unit: resume replays that decision and the engine runs
  on until the next violation. Keep the default unless
  a refused command blocks work you need: `continue` is that escape until
  a release fixes the table.
- **Machine identity.** Pass `--as-machine` explicitly; the zero-config
  fallback (forward hostname) is for laptops. Jobs whose `machine:`
  resolves elsewhere are refused at preflight (`--machine-policy strict`,
  keep it).
- **Init system.** The engine runs until SIGINT/SIGTERM and shuts down
  cleanly on both. Under systemd: `Type=simple`, `Restart=on-failure`,
  `RestartPreventExitStatus=2 3 5` (exit 2 is a configuration refusal —
  see below — that a retry loop cannot fix, exit 3 is a sealed
  engine, and exit 5 is a transition stop), a sane `RestartSec`,
  and an `ExecStart` wrapper that passes `--resume` iff
  `<root>/journal.jsonl` exists — a crash-restart must resume the same
  run root, while the first start of a new baseline must not. Never
  automate the *choice* of run root: new baselines are operator actions
  (§6). **3** is in the list above (DL-134): a sealed engine exits 3 and
  the next period is opened by an operator, not by a restart loop (§6a).
  A unit that says `=2` alone restart-loops every boundary. **5** is
  there so that `--on-transition-violation stop` keeps the engine down
  until an operator has read the violation.

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
Do not add `PartOf=` or `BindsTo=` from the supervisor to the engine.
Keep the engine's existing restart and resume rules above. Set the
supervisor's `TimeoutStopSec` to cover its longest command grace plus the
documented shutdown waits (`supervisor-protocol.md` §5); the service
manager's final cgroup kill can otherwise silence a wrapper before it
records the command's ending.

Under shape 1, stop the supervisor with
`systemctl stop dsl41-supervisor.service`.
`dsl41 supervise shutdown` exits it cleanly,
then `Restart=always` starts it again. A deadman exit is also clean and is
restarted on purpose. An ownership refusal exits 1 and retries after two
seconds; `StartLimitIntervalSec=0` keeps that retry loop from hitting the
start limit. Exit 2 is a configuration refusal and is not restarted.
Stopping the supervisor ends its running jobs. Restarting it does not
resurrect them; the engine reconciles the spool. §2b's backup order and
§7's upgrade rows stop shape 1 this way.

The optional `supervise start --deadman-seconds N` sets a finite positive
unwatched interval. Omit it for no deadman. The engine reads the running
supervisor's actual value through PING/LIST. The option is refused on
`supervise list` and `supervise shutdown`. The upgrade rule for an old,
lockless supervisor binary is stated in `supervisor-protocol.md` §5.

**Shape 2: supervisor started by the engine.** Keep `run --detached` and
set `KillMode=process` on the engine unit. It limits service-stop signals
to the engine's main process, so the supervisor and jobs can survive that
stop in the same cgroup. This gives up the unit's normal whole-cgroup stop
containment. A killed or stopped engine can leave these processes running;
monitor the supervisor and its log separately. To stop the estate, stop
the engine first, then use
`dsl41 supervise shutdown --run-root /srv/dsl41/run`.
If that cannot reach the supervisor, inspect the remaining
processes before using a whole-cgroup kill; killing wrappers can leave
`exit_status_unobservable` outcomes. Keep the engine's restart exclusions
and resume rules above. This shape has no separate supervisor restart
service; a later engine start can start a replacement after proving the
old owner is absent.

Exit codes: 0 = clean stop, 1 = engine/estate failure, 2 = refused
before start (used run root, a resume gate — catalog hash, clock domain
or runtime profile — preflight ERROR, a root another engine already
leads), 3 = sealed; period N+1 is ready to open (§6a), 5 = stopped by
`--on-transition-violation stop` after a journaled decision. Treat 2 as "a human misconfigured something" — restarting
harder will not help, hence `RestartPreventExitStatus=2 3 5` above. (A
second engine on a live run root is refused by `leader.lock`, an
`flock` the leader holds for its whole process life; the kernel
releases it when that process dies, `kill -9` included. So even a
misconfigured restart loop cannot double-start — it just loops.)

Preflight ERRORs refuse the run; WARNs print, journal, and run — read
them on first deploy of a new estate, they are the lint findings that
survive into operation.

### A worked example: `examples/nightbank/deploy/`

The repository holds a complete shape-1 deployment of the nightbank
training estate (DL-218). The files are examples to copy. They are
not package data, and the wheel does not ship them. Every path and name in
them is synthetic.

- `dsl41-launch` is a POSIX sh launcher. It is the one place that
  names the run root, the lineage anchor, the estate files in their
  order, every `-p`, every run option and `--access-map`. Edit its
  configuration block and review the edit like code. Its header
  states its rules: when it passes `--resume`, when its one-shot open
  trigger makes it pass `--open-from` (§7), what it refuses, and why it
  execs `dsl41`. `dsl41-launch --print` prints the command,
  shell-quoted, and runs nothing.
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
and §7's upgrade rows (DL-266). It installs and first
starts the units with §0's service recipe, and ends with §0's retirement
procedure, both read from this file (DL-268). The access-map refusals,
their messages and what they leave
untouched are the claim of `tests/test_nightbank_deploy.py`, which runs in
CI; the drill's claim is that systemd does not restart a refusal.
The step bodies live in `drill-steps.sh`. The workflow runs them one
step at a time. `drill-local.sh` runs the same steps on a workstation, in
a podman container with systemd as PID 1 on Ubuntu 24.04, as an
unprivileged user with passwordless sudo, as the runner runs them
(DL-271). It runs the host's architecture, so on Apple silicon it is arm64, not the runner's
x86_64.
The drill passed every step on GitHub's Ubuntu 24.04 runner at c1e6b0c
(2026-10-04, DL-271), the `quiesce`, `restore`, `reboot`, upgrade and
`retire` steps and the recipe-driven `install` and `first-start`
included. Its first pass there was at d886679 (DL-223). No other
distribution or systemd version has been observed. It is not
part of the default gate: dispatch it again after a change to the units,
the launcher or the drill.

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
  with no engine. **The estate files are optional (DL-142).** Omit them
  and every period's catalog is loaded from that period's own bundle under `<root>/catalogs/<source_bundle_hash>/`,
  by the hash its opening `segment` pins — the bundle re-parses under the
  ORIGINAL paths `sources.json` records, so it reproduces that hash
  exactly. Give them and they are the FIRST replayed period's catalog,
  hash-gated against its pin as before; later periods still come from
  their own bundles, and a supplied catalog that disagrees with a pin
  refuses rather than winning. `--permit-unknown` and `-p` therefore apply
  to the files you SUPPLY and to nothing else: a bundle holds the exact
  post-placeholder bytes the period ran, already past the launch gate. What is still true is the *path*
  sensitivity: passing the stored copies yourself, from
  `<root>/catalogs/<hash>/`, parses them under the STORED names and will
  not match (runner-design §7, a deliberate defer) — let the verb load
  them instead.
  **The replay CROSSES boundaries.** A root argument replays every
  segment the root retains, in period order; at each boundary the state
  folds through the seal exactly as an engine opening the period does,
  the next period's catalog is loaded from its bundle, and the boundary is
  printed (`period N sealed at index I; period N+1 opens in <root>`). Name
  one `wal/NNNNNN.jsonl` to replay exactly that period — it opens from
  its own seal too, and because nothing re-derives that seal there, it
  needs the predecessor **attested** (`dsl41 audit`) and says so if it is
  not. `wal/000001.jsonl` is the exception: period 1 opens from no seal,
  so it needs nothing. Name the lineage ANCHOR directory instead of a root
  and the read is estate-wide: every period, its root and its segment, in
  registry order, replayed as one lineage across the roll. A boundary is crossed only over
  a seal that proves out — the digest the record names, the record's own
  fields against the sidecar, the chain, `next_period` agreement, and the
  seal **re-derived from the period's own evidence** (period-model §11),
  which is what catches a sidecar, record and opening forged consistently together.
  Anything less refuses by name. The re-derivation costs one extra replay
  per crossed boundary.
- Offline history: `dsl41 runs <root>... [--job NAME] [--since ISO8601]
  [--format table|json|csv]` folds one or more run roots' journal +
  manifest + spool into one row per job run — "how long did it take, run
  after run, and did it change" (DL-113). Like `dsl41 journal` (DL-142), it
  needs no estate-file argument: it rebuilds the catalog from the run
  root's own stored inputs, the DL-130 bundle and only that; the retired
  `manifest/` layout is refused rather than read (DL-138). Name several
  run roots on one command line to carry a series across a baseline
  change; the default table marks the break rather than blending two
  catalogs into one misleading line. A root that has crossed a boundary
  holds one WAL segment per period, and every retained one is read
  (DL-136): each period is folded under its own catalog, so a series
  crosses a seal exactly as it crosses a run root. Name the lineage
  ANCHOR directory in place of the roots and the list comes from the
  registry: one table
  across every root
  of the estate, in period order, and a root that holds two periods is
  folded once. Name it alone — mixing it with roots is refused.

## 5. Routine operations

Same-estate restart (patching the OS, moving the process, crash
recovery): stop the engine (SIGTERM; detached jobs keep running), start
again with the exact same command line + `--resume`. **The whole command
line, not only the files.** Every opener re-parses the JIL, so the file
list and its ORDER, every `-p`, and `--permit-unknown` all have to be
what they were, or the catalog hashes differently. The launch options have
to match too: the resume gate refuses on catalog-hash, clock-domain or
runtime-profile mismatch — no silent semantic drift — and the runtime
profile is `--timezone`, `--timezone-map`, `--as-machine`,
`--machine-policy`, `--detached` and `--deadman`. `--access-map` is not in
the profile and is not gated: omit it and the run comes back with no
perimeter (§4). `--on-transition-violation` is not in the profile either:
omit it and the run comes back with the default, `refuse` (§3). Keep the whole line in the launcher (§3's worked
example); the unit calls the launcher.
A detached engine prints that line on its way out (DL-218):
its own argv, shell-quoted, with `--resume` and `--detached` once each and
`--open-from X` turned into `--estate-anchor X`.

Scheduler ticks that came due while the engine
was down are dropped and journaled (`dropped STARTJOB ...`), never fired
late: schedule maintenance windows accordingly, and catch up specific
jobs afterwards with explicit, journaled `FORCE_STARTJOB`s.

## 6. JIL rollout — updating the estate

**There are two cycles. The one this model is built around is seal → swap →
open IN PLACE (DL-133, DL-134).** The window below applies to the
fresh-run-root cycle, which is also correct; a fresh run root is not the
only way to change an estate. A boundary closes the running period at a
chosen instant T and commits the next one, and `dsl41 run --resume` on
the SAME root opens it. The verbs are in §6a below. State does not reset:
runtime globals, operator holds, `last_end_at`, armed latches, every
box's `ran_members` and `run_number` all cross the boundary, because the
boundary is a record rather than a directory. Two steps below say where
the cycles differ: latches die with the run root, not with a seal (step
1), and step 6's "new run root" is one of two openers (period-model §7).

There is no mid-run reload, by design: the running catalog is the truth
until the engine stops, resume gates on the exact catalog hash, and a
used run root refuses re-baselining. An estate change is therefore a
restart, in one of two shapes: the **seal → swap → open in place** cycle
of §6a, or the **stop → swap → new run root** cycle below. Editing files
under a running engine only flips the TUI's SPEC DRIFT flag (an advisory fingerprint
re-check); it changes nothing live.

**Before the window** (off the production run, any checkout):

```sh
dsl41 lint new-estate/*.jil -p site.properties        # gate on exit code
dsl41 rehearse new-estate/*.jil -p rehearse.properties # whole night, virtual clock
dsl41 viz --format chart new-estate/*.jil -p site.properties  # review the diff visually
```

Rehearse is the cheap insurance: a full night in seconds, same oracle,
scripted adapters. Fix everything here; the production window is for
swapping files, not discovering problems.

**The window, in order:**

1. **Quiesce triggers**: `ON_HOLD` every scheduled top-level job/box
   that still has a future tick. `dsl41 query timers -S $S` is where the
   list comes from, and it is a SUPERSET: it holds every pending oracle
   timer, every scheduled job's next tick — box members included — and
   every live filewatch as a due-less row. Take the `kind=schedule` rows
   and drop the ones that name a box member. "Already fired today" is not
   an exemption (multiple `start_times` and `start_mins` jobs fire
   again). Holds satisfy nothing downstream;
   `ON_ICE` would — it marks the job satisfied immediately. Ticks
   landing on held jobs latch (flag `A` in `query status --brief`),
   which is fine: latches are run-root state and die with the old **run
   root** (DL-133), and a seal does not create one. Across a seal an armed
   latch **survives**, deliberately: dropping it at the boundary would be
   an implicit transition with no admitted input. So the operator's
   `OFF_HOLD` in the new period produces exactly one start, which is the
   whole point of the hold. In the fresh-run-root cycle below the state
   is genuinely thrown away. An operator who does NOT want that start has
   the verb for it (DL-158): `dsl41 sendevent DISARM -J job` drops the
   latch and does nothing else; send it before the `OFF_HOLD`, on either
   side of the seal.
2. **Drain**: let RUNNING work finish (`query status --brief -S $S`), or
   `KILLJOB` what the window cannot wait for — kill command jobs, not
   boxes (only `job_terminator` members die with a box).
3. **Stop the web UI**, if any (it is stateless; order only matters for
   tidy monitoring).
4. **Stop the engine** (SIGTERM). Detached: confirm nothing you are about
   to redefine is still alive under the supervisor (`dsl41 supervise list
   --run-root <root>`); wait it out or `dsl41 supervise shutdown
   --run-root <root>`. A job left running across a re-baseline is a
   process the new catalog knows nothing about.
5. **Swap the estate**: `git -C /srv/dsl41/estate checkout <new-tag>`.
6. **New run root**: `dsl41 run ... --run-root /srv/dsl41/runs/<new-id>`
   (fresh, no `--resume`). Name run roots after the baseline —
   date + estate tag serves well. The old run root stays untouched as the
   record of the old world.
7. **Verify**: preflight WARNs, `periods/000001/manifest.json` (hashes,
   versions, runtime profile) and `sources.json` (files),
   `dsl41 query plan -S $S` for the expected waves, then the first
   scheduled fire. Repoint `S` first: `S=/srv/dsl41/runs/<new-id>/control.sock`
   — the old root's socket went with its engine.

**Rollback** is the same procedure with the previous tag and another
fresh run root. If VCS is ever in doubt, the old run root's `catalogs/`
holds the post-placeholder JIL that baseline actually ran — byte-exact,
though with placeholders already resolved, so prefer the tag.

## 6a. The boundary — sealing a period and opening the next

The cycle §6 describes, as commands (DL-134; period-model §7 and §11). It
keeps the state: runtime globals, operator
holds, `last_end_at`, armed latches, every box's `ran_members` and
`run_number` all cross, because the boundary is a record rather than a
directory.

**Seal.** Steps 1–3 of §6's window are unchanged — quiesce triggers,
drain, stop the web UI. Then:

```sh
dsl41 seal --run-root /srv/dsl41/runs/<id> \
    --next /srv/dsl41/estate/first.jil --next /srv/dsl41/estate/second.jil \
    [-p site.properties] [--next-timezone …] [--claimed-actor you@host]
```

`--next` is an OPTION, not the positional file list `dsl41 run` takes:
name it once per file. A shell glob after one `--next` is refused as an
extra argument, so expand the estate yourself. The order is part of
`source_bundle_hash`, so use the order `dsl41 run` will open the period
with.

`seal` has two entry modes and **the lock decides which**, not a flag: an
engine holding `leader.lock` is a live engine, so the CLI stages C2 and
asks it over the control socket, and that engine then exits **code 3**
("sealed; period N+1 is ready to open") — set `RestartPreventExitStatus=2 3 5`
under systemd, or an init system restart-loops a sealed engine. With no
engine running, the same command takes the lock itself, replays and
reconciles, and performs the boundary as an offline leader. C1 comes from
the run root's own bundle in both modes, so the estate files the period was
launched from need not still exist.

Exit codes: 0 committed; 2 not committed and the period is still open (C1
may legitimately have advanced first — an offline sealer's `leader` record
and the cutoff's admitted ticks are C1 activity, not damage); 4 the outcome
is UNKNOWN — read the estate before you retry, and then retry only with
the printed `request_id` (Day 2, below).
`--force-seal` commits inside the closing period's retry horizon and is
recorded as such in the seal.

The `--next-*` options describe the period about to OPEN
(`--next-timezone`, `--next-as-machine`, `--next-machine-policy`,
`--next-detached`, `--next-deadman`, `--next-timezone-map`). A change to
any of them is a new period exactly as a catalog change is — the model's
rule, and the opener holds you to it: see "Open, in place" below.

**They do not inherit C1. State the whole profile every time.** An omitted
`--next-*` takes its own default, not the running period's: no
`--next-timezone` means UTC, no `--next-as-machine` means no declared
machine, no `--next-detached` means TETHERED. Sealing a detached period
with a bare `--next` therefore commits a tethered successor. And
`--next-deadman` needs `--next-detached`; alone it exits 2 before C2 is
staged, exactly as `--deadman` needs `--detached` on `dsl41 run`.

**Open, in place.** One command, whatever the boundary moved (DL-151).
The FILES are C2's and so are the OPTIONS: state every `--next-*` the
seal staged again on the opener, without the `--next-` prefix
(`--next-timezone Europe/Zurich` → `--timezone Europe/Zurich`).
A catalog-only boundary therefore repeats the closing period's options,
because that is what it staged.

Wrong options refuse and write NOTHING: the successor's segment is not
created, the lineage head does not move, and the refusal names the fields
that disagree (`runtime-profile mismatch on <field>`), so the corrected
command opens the same committed boundary. That holds for the fields the
engine wires and for the two it cannot see, `--next-as-machine` and
`--next-machine-policy`, alike (DL-151).

```sh
dsl41 run --resume --run-root /srv/dsl41/runs/<id> \
    --detached --as-machine <name> /srv/dsl41/estate/*.jil
```

**Attest.** Before a period's root can be archived or rolled away from,
audit it:

```sh
dsl41 audit  --run-root /srv/dsl41/runs/<id>     # re-derive + checkpoint
dsl41 verify --run-root /srv/dsl41/runs/<id>     # validate a checkpoint
```

`audit` rebuilds the seal from the period's own evidence and refuses if the
two disagree; it needs the period's WAL, spool and manifests, and the
predecessor checkpoint present and verified. Period 1 is the base case and
needs no predecessor. `verify` validates a checkpoint alone — its digest,
its binding to the seal it names, and the chain it claims — which is what
a rolled root can do and a full audit is not. Auditing a period whose STATE-MACHINE VERSION differs from this
binary's needs the dsl41 version that produced it (§7's venv-per-version
pattern); the refusal names the version. A period run by an older
release of the same state-machine version audits under the current
binary.

**Open, in a fresh root** — the physical roll, optional archival hygiene:

```sh
A=/srv/dsl41/runs/<first>.anchor                 # whatever genesis used
dsl41 audit --run-root /srv/dsl41/runs/<old> --estate-anchor $A
dsl41 run --open-from $A --run-root /srv/dsl41/runs/<new> \
    --detached --as-machine <name> -p site.properties \
    /srv/dsl41/estate/*.jil
```

The opener is a full launch line, exactly as §5 says: the C2 files in
their order, every `-p`, `--permit-unknown` if the estate needs it, and
the run options.

It refuses unless the head is `closed`, the closing period is quiescent
(no live executions at all) and **attested**. The anchor is the
LINEAGE's, not the root's: a roll creates no new one, so `$A` is the
anchor genesis used for every later roll — `<first>.anchor` when genesis
named none, and whatever `--estate-anchor` it did name otherwise. Never
`<old>.anchor` after the first roll. Every later `--resume` of the new
root needs `--estate-anchor $A` too. Put it in the unit file with the
run root.

**Reading the whole estate.** *(DL-141.)* After a roll the estate is more than one directory, and which root
holds which period is the anchor's registry to answer, not yours. Four
verbs read it, and all four are addressed the same way — **name the
lineage ANCHOR where you would name a run root**:

```sh
A=/srv/dsl41/runs/<first>.anchor
dsl41 audit --estate-anchor $A                  # every closed period, in its own root
dsl41 journal $A                                # every segment, replayed in period order
dsl41 runs $A                                   # one table across every root
dsl41 estate prune --estate-anchor $A --dry-run # one retention result
```

`audit` and `estate prune` already take `--estate-anchor`, so naming it
with **no `--run-root`** is their estate-wide form; `runs` and `journal`
take their root as an argument, so the anchor goes there instead. A verb
given neither address refuses rather than guessing.

Each of the four covers every period it can, and refuses rather than
guessing: a root the registry names
that is missing, holds no sentinel, holds one that cannot be read, belongs
to another estate, or has lost the segment it is registered for stops the
command by name. Two things are left out and SAID out loud instead — a
registry row whose first segment is not durable yet, which every
cross-period reader ignores, and, for `audit`, a period that is still
open. Nothing is skipped quietly — a total that silently left a
root out is worse than no total. If you have archived a root away on
purpose, use the single-root form for the roots you still have.

One limit, stated where you meet it: `estate prune` plans each root
separately — the floors, the refusals and `--keep-runs` are per root —
because a plan is bound to the root it was computed over. `dsl41 journal`
names every segment and **replays all of them** (DL-142): each boundary is
folded through its seal, the next period's catalog comes from its own
bundle, and the crossing is printed. It needs no estate-file argument, for the
same reason `runs` does not — the estate holds its own catalogs.

**There is no adoption verb (DL-138).** A `header` journal, a
`catalog_hash_version` of 1, a `result` or standalone `effect` record and a
`manifest/manifest.json` layout are each refused by name, citing DL-138.

**A run root written before the boundary era is not adoptable.** There is no
supported path from one into a lineage. Start a new estate with `dsl41 run`
and let the old root stand as the archive of the nights it holds.
`docs/protocol-evolution.md` is the contract that governs a retired
dialect: what each protocol tolerates, how long its instances live, and what
has to be true before a reader may drop a dialect.

The `estate` group's verbs are `reclaim` (below) and `prune` (§2a).

A roll that is refused **after** it wrote the target root's sentinel
leaves that directory owned by the claim it was attempting. Period-model
§1.1's ownership rule then admits exactly one thing: the SAME roll,
retried. Fix what it refused on and re-run the identical `dsl41 run
--open-from` — same anchor, same target root — and the claim resumes,
because a claim is idempotent on its id. Any OTHER roll into that
directory is refused. That is the rule working, not a bug.

Whether you may roll somewhere ELSE instead depends on the lineage HEAD,
not on the directory. Still `closed` — the roll died before it took its
claim — and a fresh target root is a normal roll. Already `claimed`, and
only that claim's own target is accepted: retry it, or prove the claimant
gone and use the break-glass below. Deleting the abandoned directory
clears no claim.

**Break-glass.** A `claimed` lineage head whose target root is gone blocks
every opener. Overriding it can FORK the lineage — two roots opening one
period, running the same `(job, run_number)` twice — so prove the claimant
is gone first:

```sh
dsl41 estate reclaim --estate-anchor $A --force
```

It is recorded in the anchor and again in the next `segment` record with
the actor who claimed to authorize it.

**Day 2 (DL-135).** The things that go wrong after the first boundary, and
the move for each.

*A seal exited 4.* The outcome is UNKNOWN — the seal may or may not have
committed. **Read the estate before you send anything.** A committed
boundary left `seals/<N>.json`, a `seal` record at the end of
`wal/<N>.jsonl` and a `closed` head in the anchor; if they are there, the
boundary is done and the next move is to OPEN it, never to seal again.
If they are not, re-send the SAME request with the `--request-id` the
command printed — a retry the still-open period recognises is answered
from its own decision and applies nothing twice. Never compose a fresh
`request_id` for a retry: a new id is a new command. A retry that finds
the boundary ALREADY committed is answered from the seal it committed
(DL-151): the same digest, the same next period, and no second boundary,
whether the root is live or offline. A live seal also exits 4 when the
engine answers that this same request is still in flight ("your seal
<id> is still in flight"): its boundary is running. Wait, read the estate as
above, and retry only under that `--request-id`. A seal that names another
boundary in flight exits 2: this request did nothing.

*The engine exited 3 and the init system restarted it.* It will loop.
Exit 3 is "sealed; period N+1 is ready to open", and the opening is an
operator action. Put `RestartPreventExitStatus=2 3 5` in the unit file.

*`audit` printed "the registry row could not be set".* The checkpoint IS
written and durable, and the checkpoint is what `verify` and `run
--open-from` read. Only the anchor's `attested` row is outstanding, and a
live engine holds the lineage lock for its whole process lifetime. Re-run
`dsl41 audit` when the lock is free; it is idempotent and finishes the row.
An estate-wide audit does not stop there: every other period is still
audited, and the last line says how many rows are outstanding.

*`dsl41 audit` does not name a period you expected.* With no `--period` it
names the periods this root holds evidence for — a WAL, or an archive
receipt where the inputs went under `--archive-inputs`. A rolled root holds the
seal it opened from and none of that period's evidence, by design — that
seal is this root's to `verify` and the closing root's to audit. The
anchor's registry says which root holds which period.

*`audit` refuses naming a version.* The period ran a different
STATE-MACHINE version, and auditing it runs the interpreter that produced
it. Keep the venv (§7's pattern) and run the audit from it; the refusal
names the version to use. A patch-release gap alone does not trigger
this.

*A roll was refused after it wrote the target root's sentinel.* That
directory is now owned by the claim that was attempting it. Re-run the
IDENTICAL `dsl41 run --open-from` and it resumes; period-model §1.1's
ownership rule refuses any other roll into it. To roll somewhere else,
read the head first: `closed` accepts a fresh target root, `claimed`
accepts only its own — retry it, or reclaim it after proving the claimant
is gone. A retry that refuses with `missing segment record` refuses
while the torn segment stays; follow [the torn-opening recipe](#recipe-recover-a-rolled-root-whose-opening-is-torn).

*A command on a rolled root refuses, naming an anchor.* The anchor is the
LINEAGE's, not the root's, and four verbs take it:
`--estate-anchor /srv/dsl41/runs/<first>.anchor`
on `run --resume`, `seal`, `audit` and `estate prune` alike. The other
readers are addressed by root or by socket and take no anchor. Put it in the unit
file beside the run root. To read the estate rather than one of its roots,
name that anchor and no root at all (§6a, "Reading the whole estate").

*A client subscription resumed across a boundary.* Nothing to do: `since`
is an estate-wide index and the backfill spans segments. A subscriber whose
cursor is below what the root still retains — a rolled root, for instance
— receives an explicit `{"gap": true, "earliest_retained": N}` line before
the backfill (`control-protocol.md` §5).

*The root is growing.* Attest, then prune (§2a). Nothing removes anything
on a timer.

## 7. Upgrading dsl41 itself

Each release gets its own venv beside the one in use:
`/opt/dsl41/venv-<ver>`. `/opt/dsl41/venv` is a symlink to the venv in
use, and the launcher's `DSL41` runs through it (§3's worked example).
Flipping the symlink is the upgrade's one switch, and flipping it back is
the rollback. Install the new venv with §1's commands, with
`/opt/dsl41/venv-<new>` in place of every `/opt/dsl41/venv`. Skip §1's
`ln -s` line: the existing link already leads through `/opt/dsl41/venv`.
Never run §1's commands against `/opt/dsl41/venv` itself. Through the
symlink they would install into the venv in use and leave nothing to flip
back to. Smoke test the new venv before the window: `dsl41 --help`, and a
`rehearse` of the current estate with the new venv's `dsl41`. Keep the old
venv until the new one has run a full cycle.

`ln -sfn` replaces a symlink, not a directory: given §1's directory, it
creates the link inside it. A venv cannot be moved either, because its
scripts name its own path. So convert §1's layout once, with both units
stopped:

```sh
/opt/dsl41/venv/bin/pip freeze >/opt/dsl41/running.txt
python3.12 -m venv /opt/dsl41/venv-<old>
/opt/dsl41/venv-<old>/bin/pip install --no-deps -r /opt/dsl41/running.txt
/opt/dsl41/venv-<old>/bin/pip check
mv /opt/dsl41/venv /opt/dsl41/venv.orig
ln -s /opt/dsl41/venv-<old> /opt/dsl41/venv
```

The frozen list keeps the versions that were running. For a release with
assets, install its closure and wheel instead, as §1 does. `venv.orig` is
not a usable venv after the move: its scripts still name
`/opt/dsl41/venv`, which now leads to `venv-<old>`. Delete it once both
units run from the link. A process started through the symlink runs from
the venv the link named when it started, so a kept supervisor keeps its
own build after a flip.

**What moves with a release.** `catalog_hash` v2 excludes
`meta.tool_version`, and `dsl41_version` is not on the `segment` record,
so a release does not move a period's pins (DL-133; period-model §1.1).
The `leader` record names the tool version, but resume gates on catalog
hash, clock domain and runtime profile, not on the version. So resume
does not refuse a new release. Only the release note says whether resuming
across the pair is safe. The **state-machine version** is different. One
executable implements exactly one. A seal whose `next_period` names
another is refused at readiness (period-model §2.1), and resume refuses a
segment pinned to another. Check it in both venvs, whatever the note says:

```sh
/opt/dsl41/venv-<ver>/bin/python -c \
    'from dsl41.runner_ledger import STATE_MACHINE_VERSION as v; print(v)'
```

**When resume stops on a replayed input.** Resume replays the period's
log through this build. If a logged input raises on replay, resume
refuses with exit 2 and names the input: `resume stopped: replay stopped
at input N (KIND from SOURCE at AT, request_id ID)`. The same build
raises at the same input on every resume, so a restart cannot clear it,
and nothing skips the input (period-model §11). Which case it is decides
the way out:

- **The same build wrote the log.** The fault is in this release. Deploy
  a release that fixes it, then resume. The estate stays down until that
  release exists. This is a stated limit: no tool skips or rewrites a
  logged input.
- **An upgrade is replaying an older release's log.** The new release
  cannot replay what the old one wrote. Flip the venv back to the release
  that wrote the log, and resume with it.

A command whose apply raises is refused before it is journaled
(`apply_faulted`, concurrency-model §4), so this case needs an
engine-made input, or a fault a code change introduced.

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
unit. Where a row stops the supervisor unit, run
`dsl41 supervise shutdown --run-root <root>` once the engine has stopped
(§2b, shape 2). A starting engine starts a supervisor from its own venv
when none is running.

**Row 1, resume-safe, with no wrapper or supervisor protocol change.**
Detached jobs stay alive under the supervisor unit, and the new engine
reattaches to them:

```sh
systemctl stop dsl41-engine.service
ln -sfn /opt/dsl41/venv-<new> /opt/dsl41/venv
systemctl start dsl41-engine.service     # the launcher passes --resume
```

The rollback is the same three commands with the old venv. No release
pair qualifies for this row today. The service drill
(`drill-steps.sh upgrade-resume-safe`) runs these commands with two
installs of one build. That proves the mechanics, not that any version
pair is resume-safe.

**Row 2, a note that does not say resume-safe.** Work at a boundary.
First hold the scheduled jobs and let running work finish (§6's window,
steps 1 and 2). Take the reboot hold (§2b). Then run §2b's shape-1 steps 1 to 6
with the old venv. A physical roll needs a closing period with no live execution and an
attested seal (§6a). Then open the next period in the new root:

```sh
ln -sfn /opt/dsl41/venv-<new> /opt/dsl41/venv
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
existing anchor; without the first the opener runs by hand, outside the
unit, and without the second a crashed opener restarts into a genesis. If the open fails, the unit stays failed. Fix the cause, create
the trigger again and start the unit. That reruns the identical opener,
which a roll that stopped after its sentinel needs (§6a). If the opener
died before its sentinel, the unit's restart finds no trigger and no
sentinel, and the launcher refuses a genesis against the existing anchor
with exit 2, so the unit stays down. Create the trigger again and start
the unit. On either path, release the reboot hold once the engine answers.

A supervisor serves one run root, so the new root gets its own, from the
new venv. The holds cross the roll; release them with `OFF_HOLD` once the
engine answers. The rollback is the same procedure with the old venv and
another fresh root. The service drill runs this row
(`upgrade-fresh-root`) with two installs of one build.

**Row 3, resume-safe, with a wrapper spec or supervisor protocol
change.** The engine and the supervisor are deployed together, never one
at a time (`docs/protocol-evolution.md`). Restarting only the engine
would leave the old supervisor launching the old wrapper. Hold the
scheduled jobs and let running work finish (§6's window, steps 1 and 2).
Wait until `dsl41 supervise list --run-root <root>` shows no run with
`"wrapper_alive": true`. Then:

```sh
systemctl stop dsl41-engine.service dsl41-supervisor.service
ln -sfn /opt/dsl41/venv-<new> /opt/dsl41/venv
systemctl start dsl41-engine.service     # Requires= starts the supervisor first
```

Release the holds with `OFF_HOLD` once the engine answers. The rollback is
the same with the old venv. The service drill runs these commands
(`upgrade-coordinated`) with two installs of one build, and checks that
both units run from the new venv and that the engine resumed.

**Row 4, a state-machine version change.** The new build cannot open the
old estate's periods, and only the old venv can audit them (§6a). Drain
as for row 3. Take the reboot hold (§2b). Then run §2b's shape-1 steps 1 to 6
with the old venv: stop
the engine unit, a final seal and audit while the supervisor runs, stop
the supervisor unit. Then start a new estate:

```sh
ln -sfn /opt/dsl41/venv-<new> /opt/dsl41/venv
# edit the launcher's RUN_ROOT and ESTATE_ANCHOR to the new estate's,
# and both units' RequiresMountsFor= to match
systemctl daemon-reload
systemctl start dsl41-engine.service     # a genesis, no --resume
systemctl enable dsl41-supervisor.service dsl41-engine.service   # release the reboot hold (§2b)
/opt/dsl41/venv-<old>/bin/dsl41 audit --estate-anchor <old-anchor>
```

If the genesis fails, fix the cause and start the unit again before you
release the reboot hold.

A new estate carries no state: holds, globals and latches start empty, as
in §6's fresh-run-root cycle. Keep the old venv for as long as you retain
the old estate's periods. The rollback is the old venv only, on the old
estate. Stop both units and flip back. Point the launcher's `RUN_ROOT`
and `ESTATE_ANCHOR` back at the old estate's, and both units'
`RequiresMountsFor=` to match; run `systemctl daemon-reload` if a unit
file changed. Then start the engine unit. `Requires=` starts the
supervisor unit first, and the engine opens the old estate's next
period. A rollback after a failed genesis still holds the estate down
across reboots: once the old estate answers, release the hold with
`hold-release` (§2b), or the next boot leaves it stopped. The service
drill runs
this row from v1.7.0, installed from PyPI, to the build under test
(`upgrade-old-release`, `upgrade-state-machine`).

## 8. Operator scenarios

One row per situation an operator meets, with the verbs that exist. A row
that says "not built" names where the plan is recorded. Every control-plane intervention is an adjusting entry: recorded
in the WAL, attributed to its actor, replayed identically, never an edit to
what is already written. `supervise shutdown` is the one exception: it
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
| initial JIL release | native genesis opens period 1; there is no opening seal (§3) |
| incremental change, emergency hotfix mid-cycle | seal, classified diff, open under the new catalog (§6a; DL-131, DL-133). The R-gate refuses only while something in the changed closure is live. Tethered mode drains, because a transition is a restart |
| rollback | a transition back to the previous catalog (§6a), or a fresh run root (§6) |
| calendar or holiday change | a catalog change: firing dates move, the hash moves, §6a applies |
| properties or placeholder change | a catalog change: it changes the post-placeholder JIL, so the hash moves and §6a applies |
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
| break glass: supervisor shutdown | `supervise shutdown` | no | needs no live leaseholder: an expired lease, or an unexpired one whose holder's connection is gone, is grantable (DL-79) |
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
can launch the same run. The documented restore (§2b) is not affected:
it starts from a quiescent backup on a stopped estate.
