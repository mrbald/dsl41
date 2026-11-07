# TODO

For maintainers choosing outstanding work; implementation history stays in Git.
Items stay open until evidence and independent acceptance are recorded.

## Known bugs

Severity follows accepted triage. Within each severity, bugs moved from Maintenance come last.

### High

- [ ] **High**: "Nothing bounds a backward gap": a store stamped far ahead holds every tick and timer until wall time catches up, with only one warning. See DL-435 N2; owner priority 5 (2026-11-05).

### Medium

- [ ] **Medium**: `runs` refuses the whole report when an older open attempt pins pruning over an expired completed run whose revision is missing. See DL-432.
- [ ] **Medium**: `config applied.stopped` reports `none` and `update --wait` misreports a stop-set job rerun under its new definition by an earlier committed plan. No unplanned job runs (DL-432).
- [ ] **Medium**: During recovery, update takes the latest queued timestamp, so a future virtual-clock input delays application. See DL-433.
- [ ] **Medium**: Restart raises admission to the last store timestamp even when wall time is ahead, giving recovered completions the last dispatch time instead of their recorded end. No reordering was found (DL-435).
- [ ] **Medium**: After a backward clock step, raw wall timestamps on `dispatch`, `outcome` and `engine` records can precede an earlier `input`. DL-435 floors admission timestamps only.
- [ ] **Medium**: Darwin `stop_ladder` can block the engine loop with `ps` for up to 5 s after EPERM. The engine path lacks coverage (DL-431).
- [ ] **Medium**: A run root started by the legacy unit cannot resume under the main-config unit, with no documented migration path. See DL-437.
- [ ] **Medium**: Restart scans pending sources synchronously on the engine loop. See DL-437.
- [ ] **Medium**: Restart ignores unknown `[engine]` keys such as `machine_polcy`, using defaults until reread refuses them. See DL-437.
- [ ] **Medium**: Barrier cleanup kills a hung wrapper without killing its command group, leaving commands orphaned. Kill the group through the live wrapper first (`tests/update_wiring_barrier.py`, `kill_wrappers`).
- [ ] **Medium**: Both real-terminal TUI cases held `recovery_other` as "interrupted attempt held" in the Codex macOS sandbox despite correct geometry; the cause is unknown and the earlier driver was not tried. Report a supported-environment reproduction to the boss (boss ruling 2026-11-02; DL-424).
- [ ] **Medium**: The engine prints `clean_shutdown` before process exit; TERM in that window ends it with -15. `engine stop --wait` does not wait for process exit, exposing systemd ExecStop (boss ruling 2026-11-03; separate fix).
- [ ] **Medium**: If `/proc/sys/kernel/random/boot_id` is unreadable on two boots, both read "unknown" and compare equal, leaving only the PID and start-tick match. A fix needs a boss ruling.
- [ ] **Medium**: Remove stale captured-byte recompilation refusal, main-config restart preflight differences and decoded equality ignoring job order from `docs/integration-testing-inventory.md:542-543`. Follow DL-468 and the owner’s 2026-11-04 restart-equals-start principle.
- [ ] **Medium**, unproved race: Reread finishing during update can store stale status marks. See `src/dsl41/runner_control.py:683-684`.
- [ ] **Medium**, unproved race: An unchanged member can start while update stops other members of its restarting box. See `src/dsl41/runner_update.py:354-440`.
- [ ] **Medium**: A moved-out `job_terminator` can be killed by its old box’s KILLJOB cascade without a rerun. See `src/dsl41/runner_update.py:640-664`.
- [ ] **Medium**, unproved race: A box with only membership edits can start after admission and have its running membership replaced without restart. See `src/dsl41/runner_update.py:354-440`.
- [ ] **Medium**: Qualify bounds and per-input scan cost for the live oracle’s `_emitted` and `_kill_emissions` histories. See `src/dsl41/oracle_writers.py:135` and `src/dsl41/oracle.py:361-365`.
- [ ] **Medium**: The peer alarm does not reliably stop Codex at its deadline; one build ran 4.5 h. See `scripts/peer.sh:67-73`.

### Low

- [ ] **Low**: `test_socket_commands_report_plain_connect_errors[missing-serve]` exceeded its 5 s child timeout without output; cause UNKNOWN, original source capture incomplete, later passes inconclusive. Keep it open and continue, stopping for diagnosis on recurrence (owner 2026-11-05; `tests/test_cli_control_read_errors.py`; timeout reports elapsed time and process group).
- [ ] **Low**: Merge-train tests run real setup commands with `production=True` and `--command-timeout 5`, so slow setup can stop the train early. The nonzero-exit assertion at `tests/test_merge_train_rework.py:567` silently accepts timeout; also see :66,271.
- [ ] **Low**: TERM or INT after process creation but before the `try` at `scripts/merge_train.py:157` skips timeout and group cleanup. `Popen.__exit__` then waits until the command ends on its own; the signal-test driver would wait out the fake’s 300 s lifetime.
- [ ] **Low**: The equal-stamp test’s `first-look` case passes without the fix; only `last-stop` proves update takes no step during catch-up. See DL-433.
- [ ] **Low**: Operator docs omit `dsl41: cookie scan signalling: <primitive>`, and the file-processing README hides it with `tail -n 2`. See DL-436 and [the example](examples/file_processing/README.md).
- [ ] **Low**: Darwin EPERM tests fake both killpg and `ps`, leaving real `ps` output for zombie groups untested. See DL-431.
- [ ] **Low**: Installed-CLI help coverage expects retired `host` and omits `nav`, `reread`, `update` and `engine stop`. Align `tests/test_installed_cli.py` with `cli.py` and test installed core-only and UI-extra candidates.
- [ ] **Low**: Workflow teardown raises tag-removal errors in `finally`, hiding a simultaneous setup failure except in `session.json`. See `examples/workflows/tests/conftest.py`.
- [ ] **Low**: The TUI terminal model clears the screen on unlisted non-painting controls, including autowrap `ESC[?7l` and kitty keyboard codes. Textual sends these at start and exit; later occurrences cause labelled timeouts, never false passes (`tests/tui_terminal.py`).
- [ ] **Low**: A CI Python 3.12 worker inherited `COLUMNS=80` and `LINES=24` from an unknown setter; the TUI driver drops both at spawn, masking the leak. Any leaking test must restore them (boss ruling 2026-11-02; `tests/tui_terminal.py`).
- [ ] **Low**: The engine process scan can miss a process during exec, such as setsid then exec, though it still finds group members. See PR #311.
- [ ] **Low**: A hook exiting cleanly near its 5 s deadline with a TERM-resistant child is logged as delivery failed. A full-length hook also triggers about 500 macOS `ps` runs (PR #315).
- [ ] **Low**: Before a stop, `engine stop --wait` reports socket permission when `leader.lock` is unreadable. See PR #314 R3-1.
- [ ] **Low**: Restart-faults evidence relies on CI rather than drill-local’s `readable_evidence`, with no assertion that export precedes `podman rm`. Errno tests also sit under a Linux-only module skip (PR #311).
- [ ] **Low**: The restore drill deletes its hook ledger without restoring it, and a fake `podman rm` branch cannot fail. Regular-file `wal` and dangling `journal.jsonl` links get generic genesis messages (PR #316; `tests/test_rehearsal_ci.py`).
- [ ] **Low**: Linux test containers need `--init` to avoid zombies in three cleanup tests. Two after-edit hook tests also require a Git checkout (PR #317).
- [ ] **Low**: DL-447 lacks a blank separator because the line-ceiling test counts it against DL-446. See PR #317.
- [ ] **Low**: `dsl41 lint --timezone-map` treats an empty file as no map, without coverage. See PR #313.
- [ ] **Low**: Make the semantic-review trigger’s "the rules DL-225 names" and the "reader-docs drafting exception" understandable without opening decisions. See `docs/agents/review.md`, `docs/agents/coordination.md` and DL-457.
- [ ] **Low**: PID-file tests assume TERM-trap readiness after `sleep(0.2)`. Make the child ignore TERM before writing its PID (`tests/test_engine_stops.py`).
- [ ] **Low**: Rename `test_b2_changed_host_requires_restart_before_reread`: reread only reports, while update refuses. See `tests/test_main_config_restart.py`.
- [ ] **Low**: Add a `store_config` test refusing reordered jobs at the same retained revision. Only `src/dsl41/runner_update.py:867` guards this because `catalog_hash_v2` sorts keys (`src/dsl41/canon.py:91`).
- [ ] **Low**: Clock-drill timeout measurement starts at the unit assert, about 4 ms before process start, but `timeout.txt` calls it `actual_us`. See `examples/nightbank/deploy/clock-drill.sh:236`.
- [ ] **Low**: Diagnose PR-head DST failures: clock `newyork-spring` raises `KeyError: 'ended_at'` ([41924127515](https://github.com/mrbald/dsl41/actions/runs/41924127515)); restart `london-spring` DD_KILL at +8.8 s misses rehearse’s +0..6 s ([41817527860](https://github.com/mrbald/dsl41/actions/runs/41817527860)). Later main runs are green (`.github/workflows/dst-drill.yml`).
- [ ] **Low**: Unchanged members of a nested box moved out of a restarting box receive no status marker. See `src/dsl41/reread_worker.py:75-89`.
- [ ] **Low**: Failed preparation workers discard stderr and report only exit code; retain bounded failure detail. See `src/dsl41/runner_control.py:635-651`.
- [ ] **Low**: Update plan directories accumulate even after stale refusals; define retention separately from attempt records. See `src/dsl41/reread_worker.py:158` and `src/dsl41/runner_update.py:91`.
- [ ] **Low**: Explain known-unsupported warning volume and `lint.ignore`/`lint.refuse` effects in pilot guidance. See `src/dsl41/lint.py` (`rule_known_unsupported`) and `src/dsl41/runner_preflight.py:903-915`.
- [ ] **Low**: Define and verify retention for per-attempt `runs/<job>.<run>/<n>/` records (`spawn.json`, `status.json`), which consume inodes. Keep this separate from job-output archiving.
- [ ] **Low**: Fix commits and replacement PRs require fresh train records. See `scripts/merge_train.py:469-528`.
- [ ] **Low**: A closed PR blocks reuse of its branch name with `pr-head-stale`. See `scripts/merge_train.py:710-740,764`.
- [ ] **Low**: An open PR on a queued non-head branch stops the train with `other-queued-pr`. See `scripts/merge_train.py:806-812`.
- [ ] **Low**: GitHub API connection failure stops the train without bounded read retry. See `scripts/merge_train.py:201-233`.
- [ ] **Low**: Diagnose and qualify the local train pytest budget, which exceeds 1800 s under reported machine load. See `scripts/merge_train.py:1065`.

### Linux investigation

Unranked; not approved until its plan is accepted (owner 2026-11-07 "add linux investigation in todo, not approved until we have a plan"):

- [ ] Linux investigation: when the engine or a wrapper dies on Linux, are job processes lost or left behind? Main measured clean on macOS on 2026-11-07 in a normal suite and suites killed at 3, 20 and 45 s; Linux was not measured.

## Features

### Release 2.0.0: on hold

One item = one branch = one PR to main; the owner pilots main after each merge.
Item text is the owner's request; only the owner adds, splits, merges or renames release items (owner 2026-11-04).
Plan and review each item before building and reviewing it.
The `v2-jil-3` worktree is reference only; nothing merges from it wholesale.

- [ ] Release 2.0.0: clean packages, rehearse the release workflow, check the installed package and get the owner’s tag approval. On hold (owner 2026-11-05 "ok to reorder 10 hold off").

### Next, in priority order (owner 2026-11-05)

1. [ ] Select and act on jobs by `application` and `group` in the CLI and TUI.
2. [ ] "we plan to spec and implement the clean (QUEUE)": a running affected job finishes on its old definition; the new one applies at its next start (owner D9, 2026-11-04).
3. [ ] Provide `dsl41 backup` (J step 2) to capture the store and open-attempt records consistently online, with accepted restoration evidence.
4. [ ] Support `update NAME...` for partial apply (owner passive-reread ruling, 2026-11-04).
5. The **High** backward-gap bug, "Nothing bounds a backward gap" (DL-435 N2), is tracked under [High](#high).

### After shipping

#### Reliability

- [ ] Close the remaining **High** and **Medium** bugs. See [Known bugs](#known-bugs).
- [ ] Close G-config, G-semantic/clock, G-update, G-soak, G-history and G-upgrade. See the [integration test inventory](docs/integration-testing-inventory.md).

#### Qualification

- [ ] Close G-artifact, G-access, G-recipes, G-retire and G-package. See the [integration test inventory](docs/integration-testing-inventory.md).
- [ ] Test recovery across an actual reboot in a throwaway VM (K2). Injected boot-ID tests close G-boot for this release (PR #318; DL-433/DL-436).

#### Maintenance

- [ ] Close **Low** bugs and G-correction and G-wargame gaps. See [Known bugs](#known-bugs) and the [integration test inventory](docs/integration-testing-inventory.md).
- [ ] **Low**: Arrange acceptance of the main 071c312 architecture review, with 22 advisory findings. See `scripts/arch_check.py`.
- [ ] Cap concurrent container slices at two.
- [ ] **Low**: Complete due spec reviews for 20 of 20 documents at main 071c312. See `scripts/arch_check.py` (`--spec-status`).
- [ ] **Low**: Remove UpdateTier’s unused production settle default; engine callers pass false and settle externally. See `src/dsl41/reload_update.py:476` and `src/dsl41/runner_update.py:413`.
- [ ] **Low**: Replace placeholder update identities and client replies with types needing no fake token, revision or catalog. See `src/dsl41/runner_control.py:724-732` and `src/dsl41/cli_reload.py:193-231`.
- [ ] **Low**: Emit known-unsupported findings from one pipeline instead of filtering duplicates at consumers. See `src/dsl41/runner_preflight.py:903-915` and `src/dsl41/reload_plan.py:292`.
- [ ] **Medium**: Share one pure per-job effect calculation between reread forecasts and update execution. See `src/dsl41/reread_worker.py:25-133` and `src/dsl41/runner_update.py:437-687`.
- [ ] **Medium**: Declare box stop, apply, hold and restart in the UPDATE chart, deciding its recovery window. See `src/dsl41/runner.py:803-818` and `src/dsl41/reload_update.py:8-13`.
- [ ] **Low**: Remove dead client `PLANS` and move `live_of_answer` to test support. See `src/dsl41/cli_reload.py:18,166`.
- [ ] Triage Dependabot updates in one batch (H).
- [ ] Collect hosted evidence for a code PR, a docs-only PR, each workflow dispatch and the first nightly runs with notification. Then activate `workflow-examples-required` with owner approval.
- [ ] Make every container rehearsal and wargame green on main. Cover `scripts/wargame.sh`, `examples/nightbank/deploy/drill-*.sh`, `.github/workflows/service-drill.yml`, `examples/dst-drill/` and `.github/workflows/dst-drill.yml`.
- [ ] Make every documentation page state the built v2 design in one voice, without retired terms or contradictions.
- [ ] Rework control and TUI views to show drain, deadline, stored plan and interruption evidence. Until then use `dsl41 query status` and `dsl41 engine stop` exit status.
- [ ] Shorten `docs/runner-design.md`, `docs/control-protocol.md`, `docs/access-model.md`, the second deployment page and the nav verb page, removing history and preserving rules.
- [ ] Improve `dsl41 decompile` output for import, configuration, execution and maintenance as a JIL migration starting point; use synthetic estates for a migration guide. Preserve canonical equivalence, explicit conditions and the closed fold registry, starting with [file processing](examples/file_processing/README.md).
- [ ] Discuss whether main-config restart should apply pending estate edits, covering review, stop consent, running boxes, interruption decisions and audit records. Restart restores the stored catalog and socket `update` applies edits separately (boss ruling 2026-11-01; DL-437, DL-463).

## Known limits

- A: Standby starts empty; acceptable for the first system’s idempotent jobs, with state continuity deferred.
- F: All jobs run under the engine account; per-job accounts are unsupported and deferred.
- Cross-instance conditions `s(JOB^INST)` are refused: this release schedules one instance and cannot resolve another instance's status.
- `permission` is refused: per-job edit/run rights would duplicate control-socket authorization ([access-model.md](docs/access-model.md)).
- `auto_delete` is refused: it would edit the site’s configured estate, and reload would restore the job.
- `job_class` is refused: concurrency limits come from AutoSys agent configuration, not JIL; a site setting is deferred.
- `heartbeat_interval > 0` is refused: AutoSys documents no exact heartbeat producer or re-arm contract to copy; process liveness is not a heartbeat.
- `notification_template` and `notification_alarm_types` are refused: their lookup/filter grammar and defaults are not in the vendor documentation available; `send_notification` adds no per-job delivery in this release.
- `n_retrys` has no effect on CMD, BOX or FW jobs in this release: a failed job stays failed; 1–20 gives a known-unsupported warning at startup and reread, zero is quiet, other values are refused (DL-469).
- CMD uses `engine.job_shell` or supported effective-account passwd shell; unavailable/unsupported passwd shells warn and fall back to `/bin/sh`; per-job JIL `shell` is refused (`src/dsl41/runtime_policy.py:120`; `src/dsl41/runner_adapters.py:363`).
- `profile` and `std_*` files on FW jobs are refused: file watchers launch no job process to consume them.
- AutoSys command syntax has no shim; job scripts must call `dsl41` commands.
- Real host reboot recovery remains unqualified; see [Qualification](#qualification).
- Run roots updated by a binary from before f115ec7 with changed `job_shell`, `job_profile`, `std_file_mode` or `set_job_attribute_environmentals` retain the old stored profile and refuse restart; no code change follows.
