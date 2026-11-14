# TODO

Outstanding work only; history stays in Git. An item stays open until its evidence and independent acceptance are recorded.
A `Next:` line under a blocker or a High item names its first step.

## Blockers (step 0)

None open.

## High

State handled outside a state machine and duplicated logic make the whole system fragile, so they rank here, not under maintenance (owner, 2026-11-10).

- **FEAT-37**: Audit for code and tests that rest on state built by hand (DL-491). A branch that only such tests reach is deleted, stated as an assertion, or covered from validated input. Left: `runner_tui.py`, `file_processing.py` and the small modules are not audited; the engine, restart, update, stops, adapters, process identity, control and CLI modules are. Three engine arms are undecided, each with a hand-built witness: `_release` with a held start during recovery, a KILL for a run that is no longer live within one dispatch pass, and `_close_killed` on an attempt that is already closed. `test_a_held_group_keeps_its_job_s_starts_back_until_it_is_gone` and `test_a_retry_while_a_resumed_update_still_stops_waits_for_its_application` still build state by hand, because each is the only macOS run of a branch that stays; `resume`'s optional `opened` exists for the second. The suite was not measured again after the slices, so the number of arms that only forged tests reach is not known.
  Next: measure which test covers each branch again and list what is left.

## Known bugs

### Medium

In fix order. BUG-20, BUG-48 and BUG-50 are measured first. BUG-16 to BUG-19 get one pass that proves or dismisses each.

- **BUG-10**: Restart ignores unknown `[engine]` keys such as `machine_polcy` and uses defaults until reread refuses them (DL-437).
- **BUG-13**: The engine prints `clean_shutdown` before it exits; TERM in that window ends it with -15. With no socket while the lock is held, or with no peer pid from the socket, `engine stop --wait` waits for lock release rather than process exit, which exposes systemd ExecStop (DL-496).
- **BUG-20**: Bounds and per-input scan cost of the live oracle's `_emitted` and `_kill_emissions` histories are unqualified (`src/dsl41/oracle_writers.py:135`, `src/dsl41/oracle.py:361-365`).
- **BUG-48**: Update plan directories accumulate, even after stale refusals. Define their retention separately from attempt records (`src/dsl41/reread_worker.py:158`, `src/dsl41/runner_update.py:91`).
- **BUG-50**: Retention for per-attempt `runs/<job>.<run>/<n>/` records (`spawn.json`, `status.json`) is undefined and unverified; they consume inodes. Keep it separate from job-output archiving.
- **BUG-16**: Unproved race: reread finishing during update can store stale status marks (`src/dsl41/runner_control.py`, `_reread`, the `reread_marks` assignment).
- **BUG-17**: Unproved race: an unchanged member can start while update stops other members of its restarting box (`src/dsl41/runner_update.py:354-440`).
- **BUG-18**: A moved-out `job_terminator` can be killed by its old box's KILLJOB cascade without a rerun (`src/dsl41/runner_update.py:640-664`).
- **BUG-19**: Unproved race: a box with only membership edits can start after admission and have its running membership replaced without restart (`src/dsl41/runner_update.py:354-440`).
- **BUG-45**: The DST drill runs at 60 times real speed. Above speed 1 it scales the engine's `grace_term_s` and `grace_kill_s` by the speed, so the wait for a wrapper's record after a kill is 5 s real; runs [43324516341](https://github.com/mrbald/dsl41/actions/runs/43324516341) and [41924127515](https://github.com/mrbald/dsl41/actions/runs/41924127515) missed that wait when it was 83 ms real (an `unobservable` kill with the wrapper's record on disk). Open: a kill's signal came at +8.8 s ([41817527860](https://github.com/mrbald/dsl41/actions/runs/41817527860)) and +6.8 s ([43195162599](https://github.com/mrbald/dsl41/actions/runs/43195162599)) of engine time, beyond the drill's own `KILL_S` bound of 0.1 s real (6 s at speed 60); their cause is not shown. Also open: three fixed waits stay at 5 engine seconds, about 83 ms real at speed 60: between selected signals, `DRAIN_MARGIN_S` and `SPOOL_EXIT_MARGIN_S`. No run has shown one of them missed (`examples/dst-drill/drill.py`, `src/dsl41/runner_adapters.py`, `src/dsl41/engine_charts.py`).
- **BUG-2**: `runs` refuses the whole report when an older open attempt pins pruning over an expired completed run whose revision is missing (DL-432).
- **BUG-3**: `config applied.stopped` reports `none` and `update --wait` misreports a stop-set job that an earlier committed plan reran under its new definition. No unplanned job runs (DL-432).
- **BUG-8**: A run root started by the legacy unit cannot resume under the main-config unit; no migration path is documented (DL-437).
- **BUG-58**: A reread or update reply can exceed the engine's 16 MiB one-line limit: the reasons in `lint_messages` and `non_idempotent` are unbounded (`src/dsl41/reread_worker.py`). The DL-482 budget covers the definition differences only.
- **BUG-54**: A GitHub API connection failure stops the train; reads have no bounded retry (`scripts/merge_train.py:201-233`). It stopped the train four times on one day.

### Low

#### Product

- **BUG-7**: Darwin `stop_ladder` can block the engine loop with `ps` for up to 5 s after EPERM. The engine path is not covered (DL-431).
- **BUG-9**: Restart scans pending sources synchronously on the engine loop (DL-437).
- **BUG-14**: If `/proc/sys/kernel/random/boot_id` is unreadable on two boots, both read "unknown" and compare equal, leaving only the PID and start-tick match. A fix needs a boss ruling.
- **BUG-26**: Operator docs omit `dsl41: cookie scan signalling: <primitive>`, and the [file-processing README](examples/file_processing/README.md) hides it with `tail -n 2` (DL-436).
- **BUG-32**: The engine process scan can miss a process during exec, such as setsid then exec; it still finds group members (PR #311).
- **BUG-33**: A hook exiting cleanly near its 5 s deadline with a TERM-resistant child is logged as delivery failed. A full-length hook also runs `ps` about 500 times on macOS (PR #315).
- **BUG-39**: `dsl41 lint --timezone-map` treats an empty file as no map, untested (PR #313).
- **BUG-46**: Unchanged members of a nested box moved out of a restarting box get no status marker (`src/dsl41/reread_worker.py:75-89`).
- **BUG-49**: Pilot guidance does not explain the effects of `lint.ignore` and `lint.refuse` (`src/dsl41/lint.py` `rule_known_unsupported`, `src/dsl41/runner_preflight.py:903-915`).
- **BUG-73**: At a restart, a recovered completion whose recorded end falls between two engine clock readings microseconds apart is queued above engine time. A backward step below it during the catch-up then holds it until the wall returns, and `engine started` is recorded first (`src/dsl41/runner.py` `perform_plan`, DL-493).
- **BUG-74**: A snapshot's `enqueue_counter` and `waiter_seq` are installed without range checks at a restart. An inconsistent waiter rank is checked only when an input touches its job (`src/dsl41/oracle_state.py:1299`, `src/dsl41/oracle_state.py:953`).
- **BUG-75**: A plan step's run id read back at a restart is first checked in `dispatch_record`, after `spawn` changed memory. `_check_steps` checks the step count and `DSL41_SCHEDULED_AT`, but not the run id (`src/dsl41/runner_journal.py:1377`, `src/dsl41/runner_journal.py:247`).
- **BUG-76**: The engine's `stale_plan` and `update_in_progress` refusals cannot fire through the server, whose one preparation slot and STOPPING check exclude both. Only tests that call the engine reach them (`src/dsl41/runner_control.py`, `_update`).
- **BUG-77**: Lowering does not enforce the vendor's character set or its length limit for a job name; its directory-component check only refuses names containing `/` or a NUL byte, or equal to `.` or `..`; a catalog read from saved JSON or from a store is not checked (DL-494).
- **BUG-80**: Canonical rendering is not a fixpoint for a subcommand line whose subject is only a block comment before an inline `job_type:` (`insert_job: /* c */ job_type: c`): the first pass prints two spaces after the colon and the second prints one (F2). The line has no job name and lowering refuses it.
- **BUG-81**: The status hook observes store rows that have no catalog job. `sendevent CHANGE_STATUS --job other^INS` creates a row for a cross-instance name; with `status_hook_long_running_s` set, the long-running check then sends `job_long_running` for it, and a FAILURE status sends `job_failure`, though no process runs. Decided: hook observations skip a row with no catalog job (owner, 2026-11-13). Not built.
- **BUG-82**: On macOS the wrapper reads its own and the command's start token with `ps` after it has started the command and before it writes the spawn record (`src/dsl41/runner_wrapper.py:462`). Where `ps` cannot be executed, the wrapper ends with an uncaught `PermissionError`, the attempt is `unobservable`, and five such attempts hold the job. Seen only inside a sandbox that denies `ps`; Linux reads `/proc`.
- **BUG-83**: The setting `engine.set_job_attribute_environmentals` changes no launch. `runtime_policy.attribute_environment` (`src/dsl41/runtime_policy.py:485`) has no caller under `src/`, so no command receives `__job_name`, `__machine`, `__run_machine`, `__box_name` or `__max_exit_success`; `inherited_environment` does not remove those names from the engine's own environment; `docs/simulation-coverage.md` marks the setting `supported`. No decision entry rules the setting.
- **BUG-84**: Lowering's `[n-retrys]` text says "expected an integer from 0 through 20" but checks digits only (`src/dsl41/ir.py:1122`), so `n_retrys: 21` passes `build` and `lint`; only the runner's preflight enforces the range (`src/dsl41/runtime_policy.py:420`).

#### Tests, tooling and docs

- **BUG-11**: Barrier cleanup kills a hung wrapper but not its command group, orphaning the commands. Kill the group through the live wrapper first (`tests/update_wiring_barrier.py`, `kill_wrappers`).
- **BUG-12**: In the Codex macOS sandbox both real-terminal TUI cases held `recovery_other` as "interrupted attempt held" despite correct geometry. Cause unknown; reproduce in a supported environment (DL-424).
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
- **BUG-55**: The local train pytest budget exceeds 1800 s under machine load; cause undiagnosed (`scripts/merge_train.py:1065`).
- **BUG-62**: The Ubuntu service drill's storage row failed once: `test_frozen_unit` compares a run root that a live engine is writing (`examples/nightbank/deploy/storage_probe.py:271`).
- **BUG-63**: `test_kill_signals_update_affects_later_kills_only` left its ten child processes running after three runs on one day; cause unknown (`tests/test_main_config_update.py:1054`).
- **BUG-64**: After an engine restart whose new trace is already longer than the terminal UI's cursor, the UI does not see the restart: it skips the earlier entries, and a job's count of scheduler refusals can include entries twice (`src/dsl41/runner_tui.py`, the cursor check; DL-484).
- **BUG-65**: A start that a run window postponed carries no origin when it is refused at the opening, so it prints every time and `rehearse` does not list it. The timer would have to carry the source, which changes derived state: do it with the next state-machine version step (DL-484).
- **BUG-66**: Three tests pin a start's whole stderr and take out only the pidfd notice (`tests/test_main_config.py`, `without_cookie_scan_notice`). On a Linux without pidfd, such as EL7's kernel 3.10, the notice reads `os.kill (start-token check)` and the tests fail there.
- **BUG-69**: A running `job_terminator` member whose own box an update ends, moved to outside every box it restarts, is ended with the old box run (SEM-14) and is not started again. `reread` forecasts `rerun` for a command and nothing for a sub-box (`src/dsl41/reread_worker.py`, `preview`; DL-490's limits). Decide first whether the update should spare a job that leaves the box; a forecast label for the end must not copy the oracle's cascade.
- **BUG-70**: `restart_boxes` reads nesting from the live catalog, so a running sub-box that an update edits and moves out of its running box is not a restart root: it keeps its run and a member added to it stays INACTIVE, where the same edit to a top-level box restarts it (`src/dsl41/reload_plan.py`, DL-463). The forecast agrees with the update.
- **BUG-71**: An update that turns a running command into a box with members treats the new box as a restart root, because `running_boxes` tests the status and not the live type: it stops the command, then force-starts the box (`src/dsl41/reload_diff.py`; DL-463). `reread` shows it as `restart_box` (DL-490). Decide whether a retyped job should be force-started.
- **BUG-72**: `reread` prints "would be removed; no replacement" for a job an update retypes outside a restarting box, though a new job of the same name replaces it (`src/dsl41/cli_output.py`, the `remove` effect text; DL-490).
- **BUG-85**: Five texts state what the code or the contract does not: the module docstring of `src/dsl41/file_processing_charts.py` cites statechart-engine §11 for "restoring a durable state runs no actions", which §11 does not state; the docstring of `_execution_input_preflight` (`src/dsl41/runner_preflight.py`) names three inputs and the function checks two, `envvars` and `chk_files`; the docstring of `test_manual_known_unsupported_inventory` (`tests/test_simulation_register.py`) names `deploy.md` for a table the test reads from `authoring.md`; `docs/blocks/attempts.md` says "an attempt dispatched before the cookie" in two places, and the `process-leak` row of `docs/runner-design.md` says "decide as before the cookie", where wrapper-protocol §3 says "without the cookie" (DL-436).

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

### Unranked

- **FEAT-31**: The update answer names the boxes it restarted and the boxes and members it held. `dsl41 update` prints only what its answer proves, so today it names neither (DL-482).

### After shipping

#### Reliability

- **FEAT-6**: Close G-config, G-semantic/clock, G-update, G-soak, G-history and G-upgrade ([integration test inventory](docs/integration-testing-inventory.md)).

#### Qualification

- **FEAT-7**: Close G-artifact, G-access, G-recipes, G-retire and G-package ([integration test inventory](docs/integration-testing-inventory.md)).
- **FEAT-8**: Test recovery across a real reboot in a throwaway VM. Injected boot-ID tests close G-boot for this release (PR #318; DL-433, DL-436).

#### Maintenance

- **FEAT-11**: Close the G-correction and G-wargame gaps ([integration test inventory](docs/integration-testing-inventory.md)).
- **FEAT-12**: Arrange acceptance of the main 071c312 architecture review and its advisory findings; `scripts/arch_check.py` reports the current count.
  The proposed follow-ups are unimplemented; no new runtime bug is demonstrated.

  - Inspect update ingress typing and completion delivery ([runner](src/dsl41/runner.py), [update](src/dsl41/runner_update.py)). Require a concrete locality gain and unchanged recovery, commit and reply order. Current evidence does not justify a completion coordinator.
  - Trace one job consequence through the [chart](src/dsl41/job_chart.py), [cascade](src/dsl41/oracle_cascade.py), [writers](src/dsl41/oracle_writers.py) and [intents](src/dsl41/oracle_intents.py). Preserve lazy reads and event/write/effect order; refactor only for a demonstrated reduction in required context.
  - **Proposed alert requirements**: Agree job and engine/host conditions, independent detectors, deadlines, delivery owners, receipt evidence and allowed loss, including an engine hang or unavailable host helper. Define these before designing durable delivery. Current [best-effort delivery](docs/runner-design.md#4-engine-loop--single-writer) stays unchanged.
  - **Parked**: Prove replay representation savings without duplicating validation ([journal](src/dsl41/runner_journal.py)). Package moves also wait for a proven ownership seam and navigation gain.
  - Improve the existing document routes for [admission](docs/blocks/admission.md) between current authority, component explanation and how-to, without copying contracts.

- **FEAT-38**: Rule the behaviour that the code has and no decision entry rules. The documents state these two and cite no entry: the preflight refusal codes `job-class`, `permission`, `auto-delete`, `shell`, `external-instance`, `fw-execution`, `notification-unsupported`, `notification-hook`, `heartbeat-unsupported` and `runtime-attribute` (`src/dsl41/runtime_policy.py`, `job_policy`; `docs/runner-design.md`); the join of repeated `ulimit` lines in lowering (`src/dsl41/ir.py`; `docs/ir-design.md`, `docs/authoring.md`). The frozen contracts leave these four out because no entry rules them: the `; limits=<json>` suffix of a `spawn_failed` text (`src/dsl41/runner_wrapper.py`); the reread answer's `settings_sources` (`src/dsl41/reread_worker.py`); the text of the note `leaked: <n>, left running` (`src/dsl41/runner_stops.py`; DL-431 rules the behaviour, not the text); the answer to a subscription's non-integer `since`, where `null` is accepted (`src/dsl41/runner_control.py`).
- **FEAT-15**: Replace placeholder update identities and client replies with types that need no fake token, revision or catalog (`src/dsl41/runner_control.py`, the `UpdateRequest` in `_update`; `src/dsl41/cli_reload.py`, `update_reply_of`).
- **FEAT-33**: Check that a difference label is unique within a job record. Labels such as `passthrough.description` and `kind` are unclear (DL-482).
- **FEAT-34**: Put the job name last in the neutral `query status` table, so no column is padded to the longest name; long names make the table about 140 columns wide (DL-482).
- **FEAT-35**: Give `query status JOB` a readable layout; it prints a raw field list with engine-wide fields mixed in (DL-482).
- **FEAT-36**: Land the operating-rule changes the owner approved on 2026-11-10: the gate runs once, after the reviewers accept a rework round; the gate is one script in git, with `ps` installed in the Linux run; after an owner ruling a wording-only finding may take two more rounds without asking; an owner "do now" waives the plan round of rule 53 for a change in one module that touches no contract, stored format or state machine.
- **FEAT-18**: Cap concurrent container slices at two.
- **FEAT-19**: Triage Dependabot updates in one batch.
- **FEAT-20**: Collect hosted evidence for a code PR, a docs-only PR, each workflow dispatch and the first nightly runs with notification; then activate `workflow-examples-required` with owner approval.
- **FEAT-21**: Make every container rehearsal and wargame green on main: `scripts/wargame.sh`, `examples/nightbank/deploy/drill-*.sh`, `.github/workflows/service-drill.yml`, `examples/dst-drill/` and `.github/workflows/dst-drill.yml`.
- **FEAT-22**: Make every documentation page state the built v2 design in one voice, without retired terms or contradictions.
- **FEAT-23**: Show drain, deadline and interruption evidence in control and TUI views. The TUI subtitle already shows the plan token and the jobs still stopping. Until then use `dsl41 query status` and the `dsl41 engine stop` exit status.
- **FEAT-24**: Shorten `docs/runner-design.md`, `docs/control-protocol.md` and `docs/access-model.md`. Keep every rule.
- **FEAT-25**: Improve `dsl41 decompile` output for import, configuration, execution and maintenance as a JIL migration starting point, with a migration guide on synthetic estates. Keep canonical equivalence, explicit conditions and the closed fold registry. Start with [file processing](examples/file_processing/README.md).
- **FEAT-26**: Apply pending estate edits at engine start. Deferred: "defer now, until we have cleaned up state machines, then reassess" (owner, 2026-11-10). It was sized at eleven source files and 700 to 1,000 lines. The UPDATE chart step it waited for is built (DL-488). Today restart restores the stored catalog and socket `update` applies edits (DL-437, DL-463, DL-468).

## Known limits

- A wall clock below engine time holds ticks and timers until it catches up, with no bound; `status` shows `clock_hold` (DL-493).

- Standby starts empty; acceptable for the first system's idempotent jobs. State continuity is deferred.
- All jobs run under the engine account; per-job accounts are unsupported and deferred.
- On an estate with an access file, every process under the engine's account, a batch job included, can state any person and any role through `as` (DL-499; [deploy.md](docs/deploy.md), "Serve people behind the authenticating proxy", L1).
- Cross-instance conditions `s(JOB^INST)` are refused: one instance is scheduled and another instance's status cannot be resolved.
- `permission` is refused: per-job edit and run rights would duplicate control-socket authorization ([access-model.md](docs/access-model.md)).
- `auto_delete` is refused: it would edit the site's configured estate, and reload would restore the job.
- `job_class` is refused: concurrency limits come from AutoSys agent configuration, not JIL; a site setting is deferred.
- `heartbeat_interval > 0` is refused: AutoSys documents no exact heartbeat producer or re-arm contract; process liveness is not a heartbeat.
- `notification_template` and `notification_alarm_types` are refused: their lookup and filter grammar and defaults are undocumented; `send_notification` adds no per-job delivery.
- `n_retrys` has no effect on BOX or FW jobs: 1–20 warns as known-unsupported at startup and reread, zero is quiet, other values are refused (DL-469). A CMD job retries in the live engine only: `play` and `rehearse` do not retry, and rehearse marks such a producer `inconclusive_retries` (DL-485).
- A per-job JIL `shell` attribute is refused: `engine.job_shell` selects the shell for every CMD job (DL-492).
- `profile` and `std_*` files on FW jobs are refused: file watchers launch no job process to consume them.
- AutoSys command syntax has no shim; job scripts must call `dsl41` commands.
- Run roots that a binary from before f115ec7 updated with a changed `job_shell`, `job_profile`, `std_file_mode` or `set_job_attribute_environmentals` keep the old stored profile and refuse restart.
