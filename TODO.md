# TODO

Outstanding work only; history stays in Git. An item stays open until its evidence and independent acceptance are recorded.

## Blockers (step 0)

Found in the RPM pilot (dsl41 plus JIL files).

- **BLK-2**: Make the output of `query status`, `reread` and `update` clear on first read.
- **BLK-4**: The UI routinely reports "START_REFUSED [already RUNNING -- concurrent or repeated start request, no effect (STARTJOB event (scheduler))]".

## Known bugs

### High

- **BUG-1**: Nothing bounds a backward gap: a store stamped far ahead holds every tick and timer until wall time catches up, with one warning (DL-435 N2).

### Medium

In fix order. BUG-4, BUG-5 and BUG-6 go with BUG-1 as one clock pass. BUG-20, BUG-48 and BUG-50 are measured first. BUG-16 to BUG-19 get one pass that proves or dismisses each.

- **BUG-10**: Restart ignores unknown `[engine]` keys such as `machine_polcy` and uses defaults until reread refuses them (DL-437).
- **BUG-13**: The engine prints `clean_shutdown` before it exits; TERM in that window ends it with -15. `engine stop --wait` does not wait for process exit, which exposes systemd ExecStop.
- **BUG-4**: During recovery, update takes the latest queued timestamp, so a future virtual-clock input delays application (DL-433).
- **BUG-5**: Restart raises admission to the last store timestamp even when wall time is ahead, so recovered completions get the last dispatch time instead of their recorded end. No reordering found (DL-435).
- **BUG-6**: After a backward clock step, raw wall timestamps on `dispatch`, `outcome` and `engine` records can precede an earlier `input`. DL-435 floors admission timestamps only.
- **BUG-20**: Bounds and per-input scan cost of the live oracle's `_emitted` and `_kill_emissions` histories are unqualified (`src/dsl41/oracle_writers.py:135`, `src/dsl41/oracle.py:361-365`).
- **BUG-48**: Update plan directories accumulate, even after stale refusals. Define their retention separately from attempt records (`src/dsl41/reread_worker.py:158`, `src/dsl41/runner_update.py:91`).
- **BUG-50**: Retention for per-attempt `runs/<job>.<run>/<n>/` records (`spawn.json`, `status.json`) is undefined and unverified; they consume inodes. Keep it separate from job-output archiving.
- **BUG-16**: Unproved race: reread finishing during update can store stale status marks (`src/dsl41/runner_control.py:683-684`).
- **BUG-17**: Unproved race: an unchanged member can start while update stops other members of its restarting box (`src/dsl41/runner_update.py:354-440`).
- **BUG-18**: A moved-out `job_terminator` can be killed by its old box's KILLJOB cascade without a rerun (`src/dsl41/runner_update.py:640-664`).
- **BUG-19**: Unproved race: a box with only membership edits can start after admission and have its running membership replaced without restart (`src/dsl41/runner_update.py:354-440`).
- **BUG-45**: PR-head DST failures are undiagnosed: clock `newyork-spring` raised `KeyError: 'ended_at'` ([41924127515](https://github.com/mrbald/dsl41/actions/runs/41924127515)); restart `london-spring` DD_KILL at +8.8 s missed rehearse's +0..6 s ([41817527860](https://github.com/mrbald/dsl41/actions/runs/41817527860)) (`.github/workflows/dst-drill.yml`).
- **BUG-2**: `runs` refuses the whole report when an older open attempt pins pruning over an expired completed run whose revision is missing (DL-432).
- **BUG-3**: `config applied.stopped` reports `none` and `update --wait` misreports a stop-set job that an earlier committed plan reran under its new definition. No unplanned job runs (DL-432).
- **BUG-8**: A run root started by the legacy unit cannot resume under the main-config unit; no migration path is documented (DL-437).

### Low

#### Product

- **BUG-7**: Darwin `stop_ladder` can block the engine loop with `ps` for up to 5 s after EPERM. The engine path is not covered (DL-431).
- **BUG-9**: Restart scans pending sources synchronously on the engine loop (DL-437).
- **BUG-14**: If `/proc/sys/kernel/random/boot_id` is unreadable on two boots, both read "unknown" and compare equal, leaving only the PID and start-tick match. A fix needs a boss ruling.
- **BUG-26**: Operator docs omit `dsl41: cookie scan signalling: <primitive>`, and the [file-processing README](examples/file_processing/README.md) hides it with `tail -n 2` (DL-436).
- **BUG-32**: The engine process scan can miss a process during exec, such as setsid then exec; it still finds group members (PR #311).
- **BUG-33**: A hook exiting cleanly near its 5 s deadline with a TERM-resistant child is logged as delivery failed. A full-length hook also runs `ps` about 500 times on macOS (PR #315).
- **BUG-34**: Before a stop, `engine stop --wait` reports a socket permission error when `leader.lock` is unreadable (PR #314 R3-1).
- **BUG-39**: `dsl41 lint --timezone-map` treats an empty file as no map, untested (PR #313).
- **BUG-46**: Unchanged members of a nested box moved out of a restarting box get no status marker (`src/dsl41/reread_worker.py:75-89`).
- **BUG-49**: Pilot guidance does not explain known-unsupported warning volume or the effects of `lint.ignore` and `lint.refuse` (`src/dsl41/lint.py` `rule_known_unsupported`, `src/dsl41/runner_preflight.py:903-915`).

#### Tests, tooling and docs

- **BUG-11**: Barrier cleanup kills a hung wrapper but not its command group, orphaning the commands. Kill the group through the live wrapper first (`tests/update_wiring_barrier.py`, `kill_wrappers`).
- **BUG-12**: In the Codex macOS sandbox both real-terminal TUI cases held `recovery_other` as "interrupted attempt held" despite correct geometry. Cause unknown; reproduce in a supported environment (DL-424).
- **BUG-15**: `docs/integration-testing-inventory.md:542-543` still lists captured-byte recompilation refusal, main-config restart preflight differences and decoded equality ignoring job order. Remove them; restart equals start (DL-468).
- **BUG-21**: The peer alarm does not reliably stop Codex at its deadline; one build ran 4.5 h (`scripts/peer.sh:67-73`).
- **BUG-22**: `test_socket_commands_report_plain_connect_errors[missing-serve]` once exceeded its 5 s child timeout with no output. Cause unknown; diagnose on recurrence (`tests/test_cli_control_read_errors.py`).
- **BUG-23**: Merge-train tests run real setup commands with `production=True` and `--command-timeout 5`, so slow setup can stop the train early. The nonzero-exit assertion at `tests/test_merge_train_rework.py:567` also accepts a timeout; see also :66 and :271.
- **BUG-24**: TERM or INT after process creation but before the `try` at `scripts/merge_train.py:157` skips timeout and group cleanup. `Popen.__exit__` then waits for the command to end; the signal-test driver would wait out the fake's 300 s lifetime.
- **BUG-25**: The equal-stamp test's `first-look` case passes without the fix; only `last-stop` proves update takes no step during catch-up (DL-433).
- **BUG-27**: Darwin EPERM tests fake both killpg and `ps`, so real `ps` output for zombie groups is untested (DL-431).
- **BUG-28**: Installed-CLI help coverage expects the retired `host` and omits `nav`, `reread`, `update` and `engine stop`. Align `tests/test_installed_cli.py` with `cli.py`; test core-only and UI-extra installs.
- **BUG-29**: Workflow teardown raises tag-removal errors in `finally`, hiding a simultaneous setup failure except in `session.json` (`examples/workflows/tests/conftest.py`).
- **BUG-30**: The TUI terminal model clears the screen on unlisted non-painting controls, including autowrap `ESC[?7l` and kitty keyboard codes, which Textual sends at start and exit. Later ones cause labelled timeouts, never false passes (`tests/tui_terminal.py`).
- **BUG-31**: A CI Python 3.12 worker inherited `COLUMNS=80` and `LINES=24` from an unknown setter; the TUI driver drops both at spawn, masking the leak. Any leaking test must restore them (`tests/tui_terminal.py`).
- **BUG-35**: Restart-faults evidence relies on CI, not drill-local `readable_evidence`, and nothing asserts that export precedes `podman rm`. Errno tests sit under a Linux-only module skip (PR #311).
- **BUG-36**: The restore drill deletes its hook ledger without restoring it, and a fake `podman rm` branch cannot fail. A regular-file `wal` and a dangling `journal.jsonl` link get generic genesis messages (PR #316; `tests/test_rehearsal_ci.py`).
- **BUG-37**: Linux test containers need `--init` to avoid zombies in three cleanup tests. Two after-edit hook tests require a Git checkout (PR #317).
- **BUG-38**: DL-447 lacks a blank separator because the line-ceiling test counts it against DL-446 (PR #317).
- **BUG-40**: The semantic-review trigger's "the rules DL-225 names" and "reader-docs drafting exception" cannot be understood without opening decisions (`docs/agents/review.md`, `docs/agents/coordination.md`, DL-457).
- **BUG-41**: PID-file tests assume TERM-trap readiness after `sleep(0.2)`. Make the child ignore TERM before it writes its PID (`tests/test_engine_stops.py`).
- **BUG-42**: `test_b2_changed_host_requires_restart_before_reread` is misnamed: reread only reports, update refuses (`tests/test_main_config_restart.py`).
- **BUG-43**: No `store_config` test refuses reordered jobs at the same retained revision. Only `src/dsl41/runner_update.py:867` guards it, because `catalog_hash_v2` sorts keys (`src/dsl41/canon.py:91`).
- **BUG-44**: The clock drill's `timeout.txt` labels as `actual_us` a time measured from the unit assert, about 4 ms before process start (`examples/nightbank/deploy/clock-drill.sh:236`).
- **BUG-51**: Fix commits and replacement PRs require fresh train records (`scripts/merge_train.py:469-528`).
- **BUG-52**: A closed PR blocks reuse of its branch name with `pr-head-stale` (`scripts/merge_train.py:710-740,764`).
- **BUG-53**: An open PR on a queued non-head branch stops the train with `other-queued-pr` (`scripts/merge_train.py:806-812`).
- **BUG-54**: A GitHub API connection failure stops the train; reads have no bounded retry (`scripts/merge_train.py:201-233`).
- **BUG-55**: The local train pytest budget exceeds 1800 s under machine load; cause undiagnosed (`scripts/merge_train.py:1065`).

### Unranked

- **BUG-56**: When the engine or a wrapper dies on Linux, are job processes lost or left behind? macOS measured clean; Linux not measured. Not approved until its plan is accepted.

## Features

### Release 2.0.0 (on hold)

- **FEAT-1**: Release 2.0.0: clean packages, rehearse the release workflow, check the installed package and get the owner's tag approval.

### Next, in priority order

- **FEAT-2**: Select and act on jobs by `application` and `group` in the CLI and TUI.
- **FEAT-3**: "we plan to spec and implement the clean (QUEUE)": a running affected job finishes on its old definition; the new one applies at its next start.
- **FEAT-4**: Provide `dsl41 backup` to capture the store and open-attempt records consistently online, with accepted restoration evidence.
- **FEAT-5**: Support `update NAME...` for partial apply.
- **BUG-1**, the High backward-gap bug.

### After shipping

#### Reliability

- **FEAT-6**: Close G-config, G-semantic/clock, G-update, G-soak, G-history and G-upgrade ([integration test inventory](docs/integration-testing-inventory.md)).

#### Qualification

- **FEAT-7**: Close G-artifact, G-access, G-recipes, G-retire and G-package ([integration test inventory](docs/integration-testing-inventory.md)).
- **FEAT-8**: Test recovery across a real reboot in a throwaway VM. Injected boot-ID tests close G-boot for this release (PR #318; DL-433, DL-436).

#### Maintenance

- **FEAT-9**: Share one pure per-job effect calculation between reread forecasts and update execution (`src/dsl41/reread_worker.py:25-133`, `src/dsl41/runner_update.py:437-687`).
- **FEAT-10**: Declare box stop, apply, hold and restart in the UPDATE chart and decide its recovery window (`src/dsl41/runner.py:803-818`, `src/dsl41/reload_update.py:8-13`).
- **FEAT-11**: Close the G-correction and G-wargame gaps ([integration test inventory](docs/integration-testing-inventory.md)).
- **FEAT-12**: Arrange acceptance of the main 071c312 architecture review and its advisory findings; `scripts/arch_check.py` reports the current count.
  The proposed follow-ups are unimplemented; no new runtime bug is demonstrated.

  - Inspect update ingress typing and completion delivery ([runner](src/dsl41/runner.py), [update](src/dsl41/runner_update.py)). Require a concrete locality gain and unchanged recovery, commit and reply order. Current evidence does not justify a completion coordinator.
  - Trace one job consequence through the [chart](src/dsl41/job_chart.py), [cascade](src/dsl41/oracle_cascade.py), [writers](src/dsl41/oracle_writers.py) and [intents](src/dsl41/oracle_intents.py). Preserve lazy reads and event/write/effect order; refactor only for a demonstrated reduction in required context.
  - **Proposed alert requirements**: Agree job and engine/host conditions, independent detectors, deadlines, delivery owners, receipt evidence and allowed loss, including an engine hang or unavailable host helper. Define these before designing durable delivery. Current [best-effort delivery](docs/runner-design.md#4-engine-loop--single-writer) stays unchanged.
  - **Parked**: Prove replay representation savings without duplicating validation ([journal](src/dsl41/runner_journal.py)). Package moves also wait for a proven ownership seam and navigation gain.
  - Derive the [runtime-policy field inventory](src/dsl41/runtime_policy.py) from `RuntimePolicySettings`; preserve normalization, defaults, serialized values/order, hashes and missing-setting compatibility.
  - Give Engine ownership of update cancellation/reaping and share common live/resume update setup ([update](src/dsl41/runner_update.py)); preserve distinct live/recovery selection and existing ordering.
  - Establish ownership of the unintegrated [process preparation](src/dsl41/runner_adapters.py) before deletion or activation. If activation is authorized, share path resolution and trace a real dispatch through it.
  - Improve the existing document routes for [admission](docs/blocks/admission.md) between current authority, component explanation and how-to, without copying contracts.

- **FEAT-13**: Complete the 20 due spec reviews at main 071c312 (`scripts/arch_check.py --spec-status`).
- **FEAT-14**: Remove UpdateTier's unused production settle default; engine callers pass false and settle externally (`src/dsl41/reload_update.py:476`, `src/dsl41/runner_update.py:413`).
- **FEAT-15**: Replace placeholder update identities and client replies with types that need no fake token, revision or catalog (`src/dsl41/runner_control.py:724-732`, `src/dsl41/cli_reload.py:193-231`).
- **FEAT-16**: Emit known-unsupported findings from one pipeline instead of filtering duplicates at consumers (`src/dsl41/runner_preflight.py:903-915`, `src/dsl41/reload_plan.py:292`).
- **FEAT-17**: Remove the dead client `PLANS` and move `live_of_answer` to test support (`src/dsl41/cli_reload.py:18,166`).
- **FEAT-18**: Cap concurrent container slices at two.
- **FEAT-19**: Triage Dependabot updates in one batch.
- **FEAT-20**: Collect hosted evidence for a code PR, a docs-only PR, each workflow dispatch and the first nightly runs with notification; then activate `workflow-examples-required` with owner approval.
- **FEAT-21**: Make every container rehearsal and wargame green on main: `scripts/wargame.sh`, `examples/nightbank/deploy/drill-*.sh`, `.github/workflows/service-drill.yml`, `examples/dst-drill/` and `.github/workflows/dst-drill.yml`.
- **FEAT-22**: Make every documentation page state the built v2 design in one voice, without retired terms or contradictions.
- **FEAT-23**: Show drain, deadline and interruption evidence in control and TUI views. The TUI subtitle already shows the plan token and the jobs still stopping. Until then use `dsl41 query status` and the `dsl41 engine stop` exit status.
- **FEAT-24**: Shorten `docs/runner-design.md`, `docs/control-protocol.md` and `docs/access-model.md`: remove history, keep rules.
- **FEAT-25**: Improve `dsl41 decompile` output for import, configuration, execution and maintenance as a JIL migration starting point, with a migration guide on synthetic estates. Keep canonical equivalence, explicit conditions and the closed fold registry. Start with [file processing](examples/file_processing/README.md).
- **FEAT-26**: Decide whether main-config restart applies pending estate edits: review, stop consent, running boxes, interruption decisions and audit records. Today restart restores the stored catalog and socket `update` applies edits (DL-437, DL-463).

## Known limits

- Standby starts empty; acceptable for the first system's idempotent jobs. State continuity is deferred.
- All jobs run under the engine account; per-job accounts are unsupported and deferred.
- Cross-instance conditions `s(JOB^INST)` are refused: one instance is scheduled and another instance's status cannot be resolved.
- `permission` is refused: per-job edit and run rights would duplicate control-socket authorization ([access-model.md](docs/access-model.md)).
- `auto_delete` is refused: it would edit the site's configured estate, and reload would restore the job.
- `job_class` is refused: concurrency limits come from AutoSys agent configuration, not JIL; a site setting is deferred.
- `heartbeat_interval > 0` is refused: AutoSys documents no exact heartbeat producer or re-arm contract; process liveness is not a heartbeat.
- `notification_template` and `notification_alarm_types` are refused: their lookup and filter grammar and defaults are undocumented; `send_notification` adds no per-job delivery.
- `n_retrys` has no effect on CMD, BOX or FW jobs: a failed job stays failed. 1–20 warns as known-unsupported at startup and reread, zero is quiet, other values are refused (DL-469).
- Live CMD dispatch uses `/bin/sh -c` (`src/dsl41/runner_wrapper.py:480`). Startup validates configured/passwd-shell selection: it rejects an invalid configured shell and warns on passwd fallback (`src/dsl41/runner_preflight.py:697-705`, `src/dsl41/runtime_policy.py:120`). Configured `engine.job_shell` also produces a `known-unsupported` warning. Only the parked process preparation path would execute the selected shell (`src/dsl41/runner_adapters.py:323`). Per-job JIL `shell` is refused.
- `profile` and `std_*` files on FW jobs are refused: file watchers launch no job process to consume them.
- AutoSys command syntax has no shim; job scripts must call `dsl41` commands.
- Run roots that a binary from before f115ec7 updated with a changed `job_shell`, `job_profile`, `std_file_mode` or `set_job_attribute_environmentals` keep the old stored profile and refuse restart.
