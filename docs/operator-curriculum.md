# Prepare for the operator's first shift

This page is for an operator before their first shift and the person who trains them.
It lets them work through the training for running, watching, recovering and retiring the engine.
The rest of the track is under **Operator** in the
[README's Documents section](../README.md#documents).
This page adds no drill and restates no recipe; it orders and maps what exists.

## 1. Prepare the sandbox

You need shell skills, systemd basics, and the AutoSys terms job, box and condition.
Nightbank assumes a repository checkout, `uv` and a terminal.
Nightbank runs from the repository; the published package does not include it.

Read the glossary entries for [estate](glossary.md#estate),
[period](glossary.md#period), [run root](glossary.md#run-root),
[seal](glossary.md#seal) and [supervisor](glossary.md#supervisor).
Read [anchor](glossary.md#anchor) before the boundary exercises.

Start the sandbox from the checkout:

```sh
uv run examples/nightbank/bin/nightbank up
```

Keep the TUI in the first terminal.
Run the exercise's `dsl41` commands in a second terminal.
The [TUI interaction inventory](cli-tui-qualification.md#tui-interaction-inventory)
lists the controls and help keys.

One night takes about 15 real minutes.

The default night needs operator intervention before it can finish.
Keep incidents enabled for exercises 3–6.
Read [the normal night](../examples/nightbank/RUNBOOK.md#nightbank-runbook--operator-training-exercises)
before investigating a delay.
The regions start in APAC, EMEA, AMER order.
The launcher sets their clock anchors a few minutes ahead at launch.

Use these launcher forms for the other modes and subcommands:

| Invocation | What it does |
| --- | --- |
| `uv run examples/nightbank/bin/nightbank up --headless --no-incidents` | Starts a clean night without the TUI. |
| `uv run examples/nightbank/bin/nightbank up --estate bank` | Starts the bank estate, about 520 jobs. |
| `uv run examples/nightbank/bin/nightbank up --detached` | Runs jobs under a supervisor for this run root. |
| `uv run examples/nightbank/bin/nightbank props` | Prints the computed properties without starting a night. |
| `uv run examples/nightbank/bin/nightbank drop-file emea-custody` | Delivers the custodian-feed file; exits 2 if no night or several nights are live, then needs `--run` to select the run. |

The default estate is `small`, with about 80 jobs.

The launcher prints the run directory, region anchors and control socket.
The RUNBOOK uses `$S` for that socket path.
Outside the virtual environment, run its `dsl41` commands as `uv run dsl41 ...`.
For service work, follow the [runbook's account roles](deployment-runbook.md#0-the-operator-path).
They distinguish root, the service account and the operator's own account.

The sandbox notes say not to train on Christmas.
Choose another date for training.
Follow the [sandbox's cleanup rules](../examples/nightbank/README.md#layout).
Unmanaged supervisors refuse guarded maintenance; follow the [§3 incident procedure](deployment-runbook.md#3-starting-the-engine) before deleting an unmanaged training night.

## 2. Work through the course in RUNBOOK order

Live numbered exercises: 20 in the RUNBOOK, and 12 in the
[service runbook](../examples/nightbank/RUNBOOK-service.md).

The RUNBOOK names [18. Retired: a night from before the boundary era](../examples/nightbank/RUNBOOK.md#18-retired-a-night-from-before-the-boundary-era).

Plan the nights:

1. Share the first night across exercises 1–9, beginning with the TUI tour.
2. Start a fresh night for [exercise 10](../examples/nightbank/RUNBOOK.md#10-restart-drills): its tethered variant needs a night in progress, and its detached variant starts its own with `up --detached`.
3. Start a fresh night after the cold edit in [exercise 11](../examples/nightbank/RUNBOOK.md#11-changing-a-job-spec), then use it for exercise 12.
4. Give [exercise 13](../examples/nightbank/RUNBOOK.md#13-skip-the-day) its own night with the approval gate still held.
5. Read the recorded run history with [exercise 14](../examples/nightbank/RUNBOOK.md#14-after-the-night--run-history) after exercise 13 ends the night.
6. Start a fresh night after exercise 13 for the boundary exercises, then follow their seal, open and roll sequence.
7. Run exercises 22–33 in order on one wargame host (§3 below), after exercise 21: each one starts from the state the one before it left.

Each `nightbank up` creates a new night directory and refuses an existing directory.
In exercises 15–21, learn to recognise the refusal before running the command that works.
In exercise 15, the engine's exit 3 means that it has sealed the period and the next period is ready to open.

Follow each exercise's linked instructions.
An empty task cell marks training without a runbook recipe.

| Exercise | What you practise | Runbook task or recipe |
| --- | --- | --- |
| [1. Watch the night — TUI tour](../examples/nightbank/RUNBOOK.md#1-watch-the-night--tui-tour) | Read the box tree, views, details, panes and UTC clock. | [Watch an estate](deployment-runbook.md#what-to-watch) |
| [2. The query surface](../examples/nightbank/RUNBOOK.md#2-the-query-surface) | Query the socket; use `--expect`, read `sendevent` exit codes and retry by `--request-id`. | [Investigate a night](deployment-runbook.md#investigation); [recover a lost answer](deployment-runbook.md#recipe-stop-restart-seal-and-recover) |
| [3. Incident: the custodian file that never arrives](../examples/nightbank/RUNBOOK.md#3-incident-the-custodian-file-that-never-arrives) | Explain a waiting box and deliver its missing file. | [Investigate a night](deployment-runbook.md#investigation) |
| [4. Incident: hard failure, rerun after fix](../examples/nightbank/RUNBOOK.md#4-incident-hard-failure-rerun-after-fix) | Rerun a failed job and recognise a refused start. | [Manual intervention](deployment-runbook.md#manual-intervention) |
| [5. Incident: the hung job and term_run_time](../examples/nightbank/RUNBOOK.md#5-incident-the-hung-job-and-term_run_time) | Recognise termination by the engine and rerun the box. | [Manual intervention](deployment-runbook.md#manual-intervention) |
| [6. Exit-code bands: breaks within tolerance](../examples/nightbank/RUNBOOK.md#6-exit-code-bands-breaks-within-tolerance) | Observe SUCCESS for an exit code within the configured band. | |
| [7. The approval gate](../examples/nightbank/RUNBOOK.md#7-the-approval-gate) | Release an auto-hold gate and watch the start-of-day flip. | [Manual intervention](deployment-runbook.md#manual-intervention) |
| [8. Resource contention and QUE_WAIT](../examples/nightbank/RUNBOOK.md#8-resource-contention-and-que_wait) | Recognise resource waiters and their priority order. | [Watch an estate](deployment-runbook.md#what-to-watch) |
| [9. Globals as gates](../examples/nightbank/RUNBOOK.md#9-globals-as-gates) | Set a waiver global and inspect its condition. | [Manual intervention](deployment-runbook.md#manual-intervention) |
| [10. Restart drills](../examples/nightbank/RUNBOOK.md#10-restart-drills) | Practise restart, detached resume, drain and deadman; read the eviction refusal. | [Stop, restart, seal and recover](deployment-runbook.md#recipe-stop-restart-seal-and-recover) |
| [11. Changing a job spec](../examples/nightbank/RUNBOOK.md#11-changing-a-job-spec) | Recognise spec drift and use a cold edit with a fresh run root. | [Roll out JIL with a fresh run root](deployment-runbook.md#6-jil-rollout--updating-the-estate) |
| [12. Ice, hold, noexec](../examples/nightbank/RUNBOOK.md#12-ice-hold-noexec) | Compare ice, hold, noexec and manual status changes. | [Manual intervention](deployment-runbook.md#manual-intervention) |
| [13. Skip the day](../examples/nightbank/RUNBOOK.md#13-skip-the-day) | Choose interventions to abandon a night or force its date. | [Manual intervention](deployment-runbook.md#manual-intervention) |
| [14. After the night — run history](../examples/nightbank/RUNBOOK.md#14-after-the-night--run-history) | Read run history without a running engine. | [Investigate a night](deployment-runbook.md#investigation) |
| [Set up the boundary exercises](../examples/nightbank/RUNBOOK.md#the-boundary-era--exercises-15-to-21) | Set `$R`, `$P` and `$NEXT` before exercise 15. | |
| [15. Seal the night](../examples/nightbank/RUNBOOK.md#15-seal-the-night) | Close a period and recognise seal refusals. | [Seal and open the next period](deployment-runbook.md#6a-the-boundary--sealing-a-period-and-opening-the-next) |
| [16. Open period 2 — what crossed, and what did not](../examples/nightbank/RUNBOOK.md#16-open-period-2--what-crossed-and-what-did-not) | Inspect the state and latches that cross a boundary. | [Seal and open the next period](deployment-runbook.md#6a-the-boundary--sealing-a-period-and-opening-the-next) |
| [17. The morning after — audit, verify, the attested row](../examples/nightbank/RUNBOOK.md#17-the-morning-after--audit-verify-the-attested-row) | Audit and verify a closed period and inspect its attestation. | [Retire an estate](deployment-runbook.md#retiring-an-estate) |
| [19. Roll to a fresh run root](../examples/nightbank/RUNBOOK.md#19-roll-to-a-fresh-run-root) | Open the next period in a new directory after attestation. | [Seal and open the next period](deployment-runbook.md#6a-the-boundary--sealing-a-period-and-opening-the-next) |
| [20. Reclaim after a crashed roll](../examples/nightbank/RUNBOOK.md#20-reclaim-after-a-crashed-roll) | Reclaim a lineage that a lost root still claims, using a directory on removable storage. | [Manual intervention](deployment-runbook.md#manual-intervention) |
| [21. Retention — what may go, and what may never](../examples/nightbank/RUNBOOK.md#21-retention--what-may-go-and-what-may-never) | Survey retained history and prune only licensed classes. | [Keep or prune history](deployment-runbook.md#2a-retention--the-floors-and-the-prune-verb) |
| [Day-shift extras](../examples/nightbank/RUNBOOK.md#day-shift-extras) | Inspect browser views, graphs, reports and a dry run. | |
| [Bank scale](../examples/nightbank/RUNBOOK.md#bank-scale) | Repeat the exercises on the larger estate. | |
| [22. Build the wargame host and deliver the bundle](../examples/nightbank/RUNBOOK-service.md#22-build-the-wargame-host-and-deliver-the-bundle) | Build the EL8 host, pass its capability check, and deliver an offline bundle. | [Offline installation](offline-installation.md#build-and-qualify-on-a-connected-target) |
| [23. Install the package](../examples/nightbank/RUNBOOK-service.md#23-install-the-package) | Prepare a runtime at its final path, then activate it with the explicit link. | [Install the package](deployment-runbook.md#1-install) |
| [24. Install and start as a service](../examples/nightbank/RUNBOOK-service.md#24-install-and-start-as-a-service) | Install the units; recognise an `active` unit that refused; prepare the first release; start the genesis. | [Install and start as a service](deployment-runbook.md#recipe-install-and-start-as-a-service) |
| [25. Configure an estate](../examples/nightbank/RUNBOOK-service.md#25-configure-an-estate) | Open the socket to a group, read tiers and receipts, and recognise the reload and profile refusals. | [Configure an estate](deployment-runbook.md#recipe-configure-an-estate) |
| [26. Watch and measure](../examples/nightbank/RUNBOOK-service.md#26-watch-and-measure) | Read every monitoring signal, find a failure, and take the capacity observations. | [What to watch](deployment-runbook.md#what-to-watch); [capacity and limits](deployment-runbook.md#capacity-and-limits) |
| [27. Back up and restore](../examples/nightbank/RUNBOOK-service.md#27-back-up-and-restore) | Quiesce through each refusal, copy, and restore at the recorded paths. | [Back up and restore](deployment-runbook.md#2b-quiescent-backup-and-restore) |
| [28. Upgrade dsl41, row 1 and row 2](../examples/nightbank/RUNBOOK-service.md#28-upgrade-dsl41-row-1-and-row-2) | Flip the runtime under a live job; roll to a fresh root with the open trigger; roll back. | [Upgrade dsl41](deployment-runbook.md#7-upgrading-dsl41-itself) |
| [29. Recover a rolled root whose opening is torn](../examples/nightbank/RUNBOOK-service.md#29-recover-a-rolled-root-whose-opening-is-torn) | Recognise `missing segment record` and recover through the recipe's `open` branch. | [Recover a torn opening](deployment-runbook.md#recipe-recover-a-rolled-root-whose-opening-is-torn) |
| [30. Release a job change and roll it back](../examples/nightbank/RUNBOOK-service.md#30-release-a-job-change-and-roll-it-back) | Prepare, apply and roll back a release with the release helper; read the receipt and the ghost job. | [Add, change or remove a job](deployment-runbook.md#recipe-add-change-or-remove-a-job) |
| [31. Idle supervisor maintenance](../examples/nightbank/RUNBOOK-service.md#31-idle-supervisor-maintenance) | Recognise the refused manager stop and live-work refusal; terminate deliberately. | [Manual intervention](deployment-runbook.md#manual-intervention) |
| [32. Break glass: evict the executor](../examples/nightbank/RUNBOOK-service.md#32-break-glass-evict-the-executor) | Read the gated eviction's refusal, force it with attribution, and see what remains. | [Manual intervention](deployment-runbook.md#manual-intervention) |
| [33. Retire the estate](../examples/nightbank/RUNBOOK-service.md#33-retire-the-estate) | Seal, remove the units, check that nothing starts, and audit the kept history. | [Retire an estate](deployment-runbook.md#retiring-an-estate) |

## 3. Run the service drills on a systemd host

The service drills cover the installed engine, supervisor and service lifecycle.
Use a host with Podman.

1. Run the service drill from the checkout:

   ```sh
   examples/nightbank/deploy/drill-local.sh
   ```

2. Run the [offline rehearsal](offline-rehearsal.md) from the matching release checkout.
   Follow that procedure to obtain the wheel and set `DSL41_RELEASE_WHEEL` and `DSL41_RELEASE_INPUTS`.
   Run the two lanes with the commands the [offline rehearsal](offline-rehearsal.md) gives, each with a new evidence directory.

Use the [worked service example](deployment-runbook.md#a-worked-example-examplesnightbankdeploy) for the service setup.
The table is a script reference.
The drill entry points call scripts marked **Internal**; do not run those scripts by hand.

| Script | What it exercises | Invocation | Host or container needed |
| --- | --- | --- | --- |
| [wargame.sh](../scripts/wargame.sh) | Supplies a disposable EL8 systemd host with `uv` for exercises by hand and for the service drill. | `scripts/wargame.sh build`, then `start`, `deliver DIR`, `join`, `stop` or `dispose`; each accepts an optional `NAME`. | Podman; a connected host for the build; Rocky Linux 8 container with systemd as PID 1 and `--network none`. |
| [drill-local.sh](../examples/nightbank/deploy/drill-local.sh) | Runs every service-drill step and collects diagnostics. | `examples/nightbank/deploy/drill-local.sh`; prefix with `DRILL_KEEP=1` to keep its container. | Podman; the script supplies an Ubuntu systemd container and runs steps as `runner` with sudo. |
| [drill-steps.sh](../examples/nightbank/deploy/drill-steps.sh) | Checks start, restart, detached jobs, refusals, sealing, restore, monitoring, configuration, recovery, upgrades and retirement. | `examples/nightbank/deploy/drill-steps.sh --list`; `examples/nightbank/deploy/drill-steps.sh STEP`. | Real or container systemd host; sudo, `uv`, `jq` and `python3-venv`. |
| [drill-lib.sh](../examples/nightbank/deploy/drill-lib.sh) | Supplies paths and waits to the drill steps. | **Internal**: the drill scripts source it. | The same systemd host and sudo environment as its caller. |
| [offline-local.sh](../examples/nightbank/deploy/offline-local.sh) | Builds a delivery, then probes services without networking and keeps evidence. | `examples/nightbank/deploy/offline-local.sh ubuntu NEW_EVIDENCE_DIRECTORY`; use `el8` for the EL8 lane. | Podman; a connected build container and a separate systemd container without networking; release inputs from the offline procedure. |
| [offline-target.sh](../examples/nightbank/deploy/offline-target.sh) | Builds the bundle or runs capability, bundle-fault and installed-service checks. | **Internal**: rehearsal container entry point for `build`, `qualify` and `archive`. | Rehearsal containers; qualification needs systemd. |
| [offline-scenarios.sh](../examples/nightbank/deploy/offline-scenarios.sh) | Probes maintenance, concurrent or frozen engines, residual cgroups, definitions, backup, access and storage. | **Internal**: `offline-target.sh qualify` runs the prepared scenarios. | Installed service in the rehearsal's systemd container; runs as `runner`. |
| [offline-faults.sh](../examples/nightbank/deploy/offline-faults.sh) | Checks refusals for missing or changed bundle inputs and concurrent preparation. | **Internal**: `offline-target.sh` supplies the bundle. | Python 3.12 and the delivered bundle; the bundle checks do not need systemd. |
| [capability-check.sh](../examples/nightbank/deploy/capability-check.sh) | Checks the host and proves that a service cgroup works. | `examples/nightbank/deploy/capability-check.sh`; optional `--native-x86_64`. | Root in a disposable Linux systemd host or container; `systemctl`, `busctl`, `timeout`, `flock` and Python 3.12 with venv and ensurepip. |

`drill-steps.sh` and its helpers need a real or container systemd host.
The installed-service probes and capability check need that host too.
`drill-local.sh` and `offline-local.sh` create their own containers.

`wargame.sh` runs no drill itself.
The [service runbook](../examples/nightbank/RUNBOOK-service.md)'s exercises 22 to 33 run on its host by hand.
Its image has `uv`, so it can serve as `DRILL_BASE_IMAGE`.
`DRILL_BASE_IMAGE=localhost/dsl41-wargame:NAME examples/nightbank/deploy/drill-local.sh` runs the service drill on EL8.
`NAME` defaults to the checkout directory name.
`wargame.sh start` copies the checkout's tracked files to `/work` when the container has none, owned by `runner`.
`start` of an existing container keeps `/work`; remove `/work` inside it, or `dispose` it, for a fresh copy.
`wargame.sh deliver DIR` copies a built delivery bundle to `/delivery/bundle`.
The wargame image is built on the offline rehearsal image, which has no `uv`, because the rehearsal proves an install that needs none.

## 4. Read through the tasks the course does not train

The numbered course has no exercise for the tasks below.
The trainer can walk each linked procedure with the operator by reading it.
The drill column records coverage outside the numbered course.

| Task to read through | Existing drill coverage |
| --- | --- |
| [Upgrade dsl41, rows 3 and 4](deployment-runbook.md#7-upgrading-dsl41-itself) | Drill only: the `upgrade-coordinated*`, `upgrade-old-release` and `upgrade-state-machine*` steps. |
| [Recover a torn opening whose head is `claimed`](deployment-runbook.md#recipe-recover-a-rolled-root-whose-opening-is-torn) | No drill; `tests/test_operator_recipes.py` runs the branch. Exercise 29 trains the `open` branch; [exercise 20](../examples/nightbank/RUNBOOK.md#20-reclaim-after-a-crashed-roll) trains the reclaim the branch falls back to. |

## 5. On shift, choose the source for the situation

Use the [runbook's §0 task index](deployment-runbook.md#task-index)
to find the task's recipe, monitoring guidance or retirement procedure.

Use [§8 operator scenarios](deployment-runbook.md#8-operator-scenarios)
to find the intervention for a lifecycle, estate, cycle or investigation situation.

Use the [file-processing operator procedures](../examples/file_processing/README.md#operator-procedures)
for archiver refusals involving a reused basename, committed object, TODO or ledger.

Use the [DST drill](../examples/dst-drill/README.md) in its Docker Linux container
to compare the real runner with rehearsal across London and New York clock changes and local midnight.

## 6. Follow the other tracks for installation, authoring and design

This page leaves package installation to [site-package.md](site-package.md),
estate authoring to [authoring.md](authoring.md), and the design to the
**Architect** track in the [README's Documents section](../README.md#documents).
