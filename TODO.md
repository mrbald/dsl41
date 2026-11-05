# Before shipping

For maintainers tracking release work, known bugs and deferred work.
Items remain open until their evidence and independent acceptance are recorded.

## Release 2.0.0: pilot fixes (owner 2026-11-04)

One item = one branch = one PR to main; the owner pilots main after each merge.
Item text is the owner's request. Only the owner adds, splits, merges or renames items.
Each item is planned, the plan is reviewed, then it is built and reviewed.
The `v2-jil-3` worktree is reference only; nothing merges from it wholesale.

- [x] File watcher with `*` or `?` completes when a matching file arrives (pilot blocker). Done: main d9457f8.
- [x] KILLJOB sends INT, then KILL, as AutoSys does; `sendevent -k` overrides it; engine stop keeps TERM. Done: main f236509, cd1e7a2, c4f7d82, 11da8b9.
- [x] Reread works when `who am i` and `whoami` differ (check the JIL1 fix first; fix only if still broken). Done: JIL1 uses the effective UID's passwd account (main 0e7832d; `src/dsl41/runner_preflight.py:686`); socket reread runs in the engine (main c4d1433).
- [x] Jobs get `DSL41_SOCKET`, and the `dsl41` CLI uses it by default, so job scripts can call their engine. Done: main b704ff6, 463dec3.
- [x] `dsl41 reread` takes only the socket path; the engine re-reads its launch config and every JIL file its globs match. Owner D10: "extend the status to indicate the job has been reread ... so we know the damage of running update after reread". Done: main c4d1433, b516da3, 4ccb3a1.
- [x] A leading `>` in `std_out_file` or `std_err_file` truncates the file and `>>` appends. Done: main b37694e.
- [x] Every JIL attribute that is parsed but has no effect gives a "known but unsupported" warning at startup and reread. Done: main bef56e7.
- [x] Manual: log-name placeholders `${AUTO_JOB_NAME}` and `${AUTORUN}`; the `dsl41` replacements for `sendevent`, `autorep` and `autostatus`; the unsupported attributes. Done: main 1bad3a7, 399f38b.
  - [x] Before the tag: the manual says reread and update need ADM, not OPS, and drops the retired plan-token wording (owner 2026-11-05). Done: main ddadee1 (manual and contracts), 472e47e (update refusal text; owner MSG=NOW).
- [x] Our locked environment stays a few days behind the newest releases, and Dependabot is configured with the same delay (about 5 days). Done: main 81cf8f1.
- [ ] Release 2.0.0: clean packages, release-workflow rehearsal, installed check, and the owner's approval of the tag. On hold (owner 2026-11-05 "ok to reorder 10 hold off").
- [x] RC-1: a waiting wildcard file watcher changed to an exact path and taken up by update (or restart after it) must not stop the engine. Done: main 071c312, 601906d (owner 2026-11-05 RC1=FIX).
- [x] A patch release must not stop the engine from restarting: restart accepts the main config captured by an earlier 2.0.x binary (owner 2026-11-05 "add"; operator summary, the owner may reword). Done: main b120807 (DL-468; closes DL-437 K1, architecture section 2 finding 8 and retained-configuration job-order equality).

# Next, in priority order (owner 2026-11-05)

1. [ ] Select and act on jobs by `application` and `group` in the CLI and the TUI.
2. [ ] "we plan to spec and implement the clean (QUEUE)": a running affected job finishes on its
   old definition; the new one applies at its next start (owner D9, 2026-11-04).
3. [ ] `dsl41 backup` (J step 2): provide an online backup command that captures the store and open attempts' records consistently; accept restoration evidence.
4. [ ] `update NAME...` partial apply (owner passive-reread ruling, 2026-11-04).
5. [ ] The High backward-gap bug (DL-435 N2), "Nothing bounds a backward gap", under [Known bugs](#known-bugs).

# Known limits

- A: The standby starts empty. This is acceptable for the first system, whose jobs are all idempotent; improve state continuity later.
- F: All jobs run under the engine's account. Per-job accounts are unsupported; improve this later.
- Cross-instance conditions `s(JOB^INST)` are refused: this release schedules one instance and cannot resolve another instance's status.
- `permission` is refused: per-job edit and run rights would be a second authorization model beside the control-socket roles ([access-model.md](docs/access-model.md)).
- `auto_delete` is refused: deleting a job would make the engine edit the site's config-defined estate, and the next reload would restore it.
- `job_class` is refused: class concurrency limits come from the AutoSys agent's configuration, not JIL, so there is no limit to apply; a site setting can come later.
- `heartbeat_interval > 0` is refused: AutoSys documents no exact heartbeat producer or re-arm contract to copy, and process liveness is not a heartbeat.
- `notification_template` and `notification_alarm_types` are refused: their lookup and filter grammar and defaults are not in the vendor documentation available; `send_notification` adds no per-job delivery in this release.
- `n_retrys` on CMD jobs has no effect in this release: a failed job stays failed, and startup warns. A nonzero `n_retrys` on BOX and FW jobs is refused.
- CMD jobs use `engine.job_shell`, or a supported shell from the effective account's passwd entry; an unsupported or unavailable passwd shell falls back to `/bin/sh` with a warning. A per-job JIL `shell` attribute is refused (`src/dsl41/runtime_policy.py:120`; `src/dsl41/runner_adapters.py:363`).
- `profile` and `std_*` files on FW jobs are refused: file watchers launch no job process to consume them.
- No AutoSys-syntax command shim: job scripts call `dsl41` commands; AutoSys command syntax is not implemented.
- Real host reboot recovery remains unqualified; see [Qualification](#qualification).

# Known bugs

Severity follows the accepted triage. In-flight targets remain open until accepted.

- [ ] **Low**: `test_socket_commands_report_plain_connect_errors[missing-serve]`
  in `tests/test_cli_control_read_errors.py` exceeded its 5-second child timeout
  without output (JIL1 full Python 3.12 gate, operator triage). Cause UNKNOWN;
  the original failed source set was incompletely captured. Later passes do not
  close this bug. The test reports elapsed time and process group on timeout.
  It stays open; the owner permits continuation; recurrence stops for diagnosis
  (owner 2026-11-05).
- [ ] **Low**: `test_stopped_command_kills_its_grandchild[timeout]`
  in `tests/test_merge_train_rework.py` failed to observe its ready file within
  15 seconds in the 1-second command-timeout case (JIL1 full Python 3.12 gate,
  operator triage). Cause UNKNOWN; the original failed source set was incompletely
  captured. Later passes do not close this bug. The owner permits continuation;
  recurrence stops for diagnosis. This is a test race with a likely mechanism:
  the train runs every command with `--command-timeout 1`, so an earlier slow
  command or the fake uv's own start can end the train before the ready file exists.
  The wait loop exits when the train process does (`tests/test_merge_train_rework.py:639`).
  Fix after shipping (owner 2026-11-05).

- [ ] **Medium**: `runs` refuses the whole report when an older open attempt holds the journal's pruning
  floor over a completed run whose history the snapshot has expired by age; the leaf reader
  finds no rev for that run (Astra, slice 3b-b wiring round 5, M11; history, DL-432).
- [ ] **Medium**: `config applied.stopped` says `none` for a stop-set job whose rerun plan was committed
  before the admission and that then reruns under the new definition; the record and
  `update --wait` are wrong, nothing runs that the earlier plan did not choose (Opus, slice 3b-b
  wiring round 5, R5-2; update, DL-432).
- [ ] **Medium**: While recovering, the resumed update's look takes the latest stamp of any queued item, so
  an input injected for later on a virtual clock pushes the application behind it (Opus, slice
  restart round 2, N2; update, DL-433).
- [ ] **Low**: The equal-stamp test's `first-look` case passes without the fix; only `last-stop` pins
  the update taking no step during the catch-up (Opus, slice restart round 2, N3; test gap,
  DL-433).
- [ ] **Medium**: A real restart raises the admission frontier to the store's last stamp even when the wall
  is ahead of it, so recovered completions take the last dispatch's time instead of their
  recorded end; no reordering found (Opus, slice clock round 2, N1; clock, DL-435).
- [ ] **High**: Nothing bounds a backward gap: a store stamped far ahead starts and holds every tick and
  timer until the wall catches up, with one warning line as the only trace (Opus, slice clock
  round 2, N2; clock, DL-435).
- [ ] **Medium**: After a backward wall-clock step, `dispatch`, `outcome` and `engine` records carry the raw
  wall stamp, so they can sit below an earlier `input` in the store; DL-435 floors admission
  stamps only (W2 drill builder, clock-step scenario; clock, DL-435).
- [ ] **Low**: No operator doc names the `dsl41: cookie scan signalling: <primitive>` start line, and
  `examples/file_processing/README.md` trims output with `tail -n 2`, which hides it (Opus, slice
  proc-cookie round 2; docs, DL-436).
- [ ] **Medium**: On Darwin, the engine's `stop_ladder` takes the same EPERM path and runs `ps` for up to
  5 s on the event loop, blocking it; no test covers the engine path (Opus, slice killpg-eperm
  round 1, Low 2; wrapper, DL-431).
- [ ] **Low**: The Darwin EPERM tests fake both killpg and `ps`; no test reads real `ps` output on a real
  zombie group (Opus, slice killpg-eperm round 1, Low 3; test gap, DL-431).
- [ ] **Medium**: A root started with the legacy unit cannot resume under the main-config unit, and nothing
  documents a path (Opus, slice main-config round 1, K4; DL-437).
- [ ] **Medium**: The pending-source scan at restart runs synchronously on the engine loop (Opus, slice
  main-config round 1, K5; DL-437).
- [ ] **Medium**: Restart ignores unknown `[engine]` keys, so a typo such as `machine_polcy` silently runs the
  default until the next reread refuses it (Opus, slice main-config round 2, K7; DL-437).
- [ ] **Low**: The opt-in installed-CLI help test (`tests/test_installed_cli.py`) still expects the retired
  `host` command and omits `nav`, `reread`, `update` and `engine stop`; align it with `cli.py` and
  run it on installed core-only and ui-extra candidates (docs batch 2F, Opus).
- [ ] **Medium**: The update_wiring barrier cleanup's last-resort kill SIGKILLs a hung wrapper but not its
  command group, so the command is orphaned until it exits; kill the group through the live wrapper
  first (`tests/update_wiring_barrier.py` `kill_wrappers`; Opus, slice barrier-fix).
- [ ] **Low**: The workflow lane's session teardown raises the tag-removal error inside `finally`, so when
  setup and removal both fail pytest shows only the removal error; the setup failure stays in
  `session.json` (`examples/workflows/tests/conftest.py`; Opus, slice rehearsal-local-cache).
- [ ] **Low**: The TUI driver's terminal model still clears the screen on non-painting controls outside its
  allow-list (autowrap `ESC[?7l`, kitty keyboard codes); Textual sends them only at start and exit, and
  a later one makes a wait time out with its label, never pass (`tests/tui_terminal.py`; Opus, slice
  tui-driver).
- [ ] **Low**: On CI's `test (3.12)` job a pytest worker carried `COLUMNS=80` and `LINES=24`; no setter was found in the tests, the
  source, the scripts, the workflows or the installed packages. The TUI driver now drops both at the TUI's spawn, so the
  test passes either way; a test found leaking them must restore the environment (boss ruling 2026-11-02; Opus analysis, slice
  tui-driver).
- [ ] **Medium**: In the Codex macOS sandbox both real-terminal TUI cases failed on engine state, with correct geometry: `recovery_other`
  was held by "interrupted attempt held" (DL-424) instead of staying unheld. The cause is unexplained and the earlier driver was
  not tried there; a reproduction in a supported environment goes back to the boss (boss ruling 2026-11-02; Sol, with Opus
  analysis, slice tui-driver).

- [ ] **Medium**: The engine prints `clean_shutdown` before process exit; a TERM in that window ends it with -15. `engine stop --wait` does not wait for process exit, exposing systemd ExecStop (boss ruling 2026-11-03; separate fix).
- [ ] **Medium**: If `/proc/sys/kernel/random/boot_id` is unreadable on two boots, both read "unknown" and compare equal, leaving only the pid and start-tick match (Opus, slice boot-id; fix needs a boss ruling).
- [ ] **Low**: The engine's process scan can miss a process during exec, such as setsid then exec; group members are still found (Opus, #311).
- [ ] **Low**: A status hook that exits cleanly just before its 5 s deadline with a TERM-resistant child is logged as delivery failed; on macOS a full-length hook triggers about 500 ps runs (Opus, #315).
- [ ] **Low**: Before a stop, `engine stop --wait` with an unreadable `leader.lock` prints the socket-permission line (Opus, #314 R3-1).
- [ ] **Low**: Restart-faults evidence is made readable by a CI workflow step, not drill-local's `readable_evidence`; no test asserts the export runs before podman rm; the errno cases sit under a Linux-only module skip (Opus, #311).
- [ ] **Low**: The restore drill row deletes the drill hook ledger without restoring it; a fake podman rm branch in `test_rehearsal_ci.py` cannot fail; a regular-file `wal` or dangling `journal.jsonl` link gets the generic genesis message (Opus, #316).
- [ ] **Low**: Local Linux test containers need `--init` or three process-cleanup tests see zombies; two after-edit hook tests need a git checkout (Sol and Opus, #317).
- [ ] **Low**: DL-447 has no blank separator because the line-ceiling test counts a trailing blank against DL-446 (Opus, #317).
- [ ] **Low**: `dsl41 lint --timezone-map` with an empty file counts it as no map; no test covers it (Opus, #313).
- [ ] **Low**: Two guide sentences need a decision entry to be understood: `review.md`'s semantic-review trigger cites "the rules DL-225 names", and `coordination.md` names a "reader-docs drafting exception" without its conditions; guides must read standalone (Astra, DL-457 review).
- [ ] **Low**: tests/test_engine_stops.py keeps a sleep(0.2) TERM-trap readiness assumption in the pid-file tests; the child should ignore TERM before it writes its pid (Opus, 2026-11-05, manual-fix branch reviews).
- [ ] **Low**: tests/test_main_config_restart.py's test_b2_changed_host_requires_restart_before_reread is misnamed: reread only reports, and update refuses (Opus, 2026-11-05, manual-fix branch reviews).
- [ ] **Low**: No store_config test shows that a retained row whose jobs are reordered at the same
  revision is refused; `src/dsl41/runner_update.py:867` is the only guard, because
  catalog_hash_v2 sorts keys (`src/dsl41/canon.py:91`)
  (Opus, 2026-11-05, TODO plan review).

- [ ] **Medium**: Wrapper and process tests can leave stopped wrappers or children after teardown.
  Prove and fix owned-process cleanup in `tests/test_wrapper_branches.py:546`,
  `tests/test_engine_stops.py` (`child.py`) and `src/dsl41/runner_wrapper.py`
  (operator, wrapper/process tests 2026-11-05; tests).
- [ ] **Low**: DST drill failures on PR heads have unresolved causes:
  [41924127515](https://github.com/mrbald/dsl41/actions/runs/41924127515), clock lane,
  `newyork-spring` raises `KeyError: 'ended_at'`;
  [41817527860](https://github.com/mrbald/dsl41/actions/runs/41817527860), restart lane,
  `london-spring` DD_KILL at +8.8 s falls outside rehearse's +0..6 s.
  Subsequent main runs are green (Opus, TODO plan review round 1; hosted results,
  `.github/workflows/dst-drill.yml`).
- [ ] **Medium**: Remove the stale captured-byte recompilation refusal and main-config restart
  preflight difference claims from `docs/integration-testing-inventory.md:542` and the decoded
  equality ignoring job order claim at :543 (DL-468; Opus, TODO plan review round 1;
  owner 2026-11-04 restart-equals-start principle).
- [ ] **Medium**, unproved race: A reread finishing during update can store stale status marks
  (architecture review 2026-11-05, section 4; Opus, item 5 round 1;
  `src/dsl41/runner_control.py:683-684`).
- [ ] **Medium**, unproved race: An unchanged member can start while update stops other members
  of its restarting box (architecture review 2026-11-05, section 4; Opus, item 5 plan round 3;
  `src/dsl41/runner_update.py:354-440`).
- [ ] **Medium**: A moved-out `job_terminator` can be ended by its old box's KILLJOB cascade and
  receive no rerun (Opus, item 5 round 2; `src/dsl41/runner_update.py:640-664`).
- [ ] **Low**: Unchanged members of a nested box moved out of a restarting box receive no status
  marker (Opus, item 5 round 3; `src/dsl41/reread_worker.py:75-89`).
- [ ] **Medium**, unproved race: A box with only membership edits can start after admission and
  have its running membership swapped without restart (architecture review 2026-11-05,
  section 4; `src/dsl41/runner_update.py:354-440`).
- [ ] **Low**: A failed preparation worker discards stderr and reports only its exit code.
  Retain bounded failure detail (architecture review 2026-11-05, section 4;
  `src/dsl41/runner_control.py:635-651`).
- [ ] **Low**: Update plan directories accumulate, including stale-refused updates.
  Define retention separately from attempt records (architecture review 2026-11-05, section 4;
  `src/dsl41/reread_worker.py:158`, `src/dsl41/runner_update.py:91`).
- [ ] **Medium**: Qualify bounds on the live oracle's `_emitted` and `_kill_emissions` histories
  and their per-input scan cost (architecture review 2026-11-05, section 4;
  `src/dsl41/oracle_writers.py:135`, `src/dsl41/oracle.py:361-365`).
- [ ] **Low**: Document that a routine box update emits TERMINATED transitions and can trigger
  status-hook alerts (architecture review 2026-11-05, section 4;
  `src/dsl41/runner.py:803-818`).
- [ ] **Low**: Document known-unsupported warning volume, `lint.ignore` and `lint.refuse` effects
  in pilot guidance (architecture review 2026-11-05, section 4;
  `src/dsl41/lint.py` `rule_known_unsupported`, `src/dsl41/runner_preflight.py:903-915`).

# After shipping

## Reliability

- [ ] Close the remaining High and Medium known bugs.
- [ ] Close G-config, G-semantic/clock, G-update, G-soak, G-history and G-upgrade gaps from the [integration test inventory](docs/integration-testing-inventory.md).

## Qualification

- [ ] Close G-artifact, G-access, G-recipes, G-retire and G-package gaps from the [integration test inventory](docs/integration-testing-inventory.md).
- [ ] Real VM reboot test (K2): test recovery across an actual reboot in a throwaway VM; injected boot-ID coverage closes G-boot for this release (#318, DL-433/DL-436).

## Maintenance

- [ ] Close Low known bugs and G-correction and G-wargame gaps.
- [ ] Low: the engine's per-attempt `runs/<job>.<run>/<n>/` folders accumulate small records (`spawn.json`, `status.json`) and consume inodes; define and verify retention separately from job-output archiving.
- [ ] **Low**: Architecture review is due, with 22 advisory findings.
  Arrange acceptance of the current main review (`scripts/arch_check.py`, main 071c312).
- [ ] Cap concurrent container slices at two.
- [ ] **Low**: Spec review is due for 20 of 20 documents
  (`scripts/arch_check.py --spec-status`, main 071c312).
- [ ] **Low**: A fix commit or replacement PR needs fresh train records
  (`scripts/merge_train.py:469-528`)
  (operator, merge-train runs 2026-11-04/05; tooling).
- [ ] **Low**: A closed PR blocks its branch name with `pr-head-stale`
  (`scripts/merge_train.py:710-740,764`)
  (operator, merge-train runs 2026-11-04/05; tooling).
- [ ] **Low**: An open PR on a queued non-head branch stops the train with `other-queued-pr`
  (`scripts/merge_train.py:806-812`)
  (operator, merge-train runs 2026-11-04/05; tooling).
- [ ] **Low**: A GitHub API connection failure stops the train with no bounded read retry
  (`scripts/merge_train.py:201-233`)
  (operator, merge-train runs 2026-11-04/05; tooling).
- [ ] **Low**: The local train pytest gate exceeds 1800 s under reported machine load.
  Diagnose and qualify its budget (`scripts/merge_train.py:1065`)
  (operator, merge-train runs 2026-11-04/05; tooling).
- [ ] **Medium**: The peer alarm does not reliably stop Codex at its deadline; a build ran 4.5 h
  (`scripts/peer.sh:67-73`) (operator, peer.sh run 2026-11-04/05; tooling).
- [ ] **Low**: Remove UpdateTier's unused production settle default; engine callers pass false
  and settle externally (architecture review 2026-11-05, section 2 finding 1;
  `src/dsl41/reload_update.py:476`, `src/dsl41/runner_update.py:413`).
- [ ] **Low**: Replace placeholder update identities and client replies with types that need no
  fake token, revision or catalog (architecture review 2026-11-05, section 2 finding 2;
  `src/dsl41/runner_control.py:724-732`, `src/dsl41/cli_reload.py:193-231`).
- [ ] **Low**: Emit known-unsupported findings from one pipeline instead of filtering duplicates
  at consumers (architecture review 2026-11-05, section 2 finding 3;
  `src/dsl41/runner_preflight.py:903-915`, `src/dsl41/reload_plan.py:292`).
- [ ] **Medium**: Share one pure per-job effect calculation between reread forecasts and update
  execution (architecture review 2026-11-05, section 2 finding 4;
  `src/dsl41/reread_worker.py:25-133`, `src/dsl41/runner_update.py:437-687`).
- [ ] **Medium**: Declare the box stop, apply, hold and restart sequence in the UPDATE chart,
  with a decision on its recovery window (architecture review 2026-11-05, section 2 finding 5;
  `src/dsl41/runner.py:803-818`, `src/dsl41/reload_update.py:8-13`).
- [ ] **Low**: Remove dead client `PLANS` and move `live_of_answer` to test support
  (architecture review 2026-11-05, section 2 finding 7; `src/dsl41/cli_reload.py:18,166`).
- [ ] Triage the Dependabot updates in one batch (H).
- [ ] Required-check activation: collect hosted evidence for a code PR, a docs-only PR, each workflow's dispatch and the first nightly runs with notification, then activate `workflow-examples-required` under the owner's approval.
- [ ] Every container-based rehearsal and wargame runs green on main: `scripts/wargame.sh`, the service drill (`examples/nightbank/deploy/drill-*.sh`, `.github/workflows/service-drill.yml`) and the DST drill (`examples/dst-drill/`, `.github/workflows/dst-drill.yml`).
- [ ] Clean, coherent docs: every page states the v2 design as built, in one voice, with no retired terms or contradictions between pages.
- [ ] Control and TUI re-cut: show the drain, its deadline, the stored plan and interruption evidence. Workaround: `dsl41 query status` and the `dsl41 engine stop` exit status.
- [ ] Short rewrites of `docs/runner-design.md`, `docs/control-protocol.md` and `docs/access-model.md`, the second deployment page and the nav verb page. The contracts are long and keep history; the completed terms sweeps removed retired terms without changing rules.
- [ ] Improve the Python estate interface produced by `dsl41 decompile`. Make the output easy to import, configure, execute and maintain as the starting point for migration from JIL. Use synthetic estates to develop a migration guide. Preserve canonical equivalence, explicit conditions and the closed fold registry. The file-processing prototype in `examples/file_processing/` is the first authoring use case.
- [ ] Discuss whether main-config restart should apply pending estate edits, including review, stop consent, running boxes, interruption decisions and audit records. Restart restores the stored catalog; socket `update` applies edits separately (boss ruling 2026-11-01; DL-437, DL-463).

# Completed history

## Release 2026-11-02: one box

- [x] Restart gaps (G-restart, #311, DL-444): installed fault evidence covers status/spawn loss, residue, quarantine, deadlines and stable invocation; pidfd signalling and standalone wrapper refusals are covered.
- [x] Storage gaps (G-storage, #308, DL-99/DL-428): real admission ENOSPC preserves UNKNOWN without an uncommitted launch and recovers after space returns; a frozen and resumed installed engine retains exclusive ownership.
- [x] Restore gaps (G-restore, #316, DL-443/DL-445): drained, writer-free full-root restore drills and fresh readers cover recovery identity and effects; all genesis witnesses refuse repeatedly without mutation; G-boot is covered by #318.
- [x] Status hook (C, #315, DL-446): committed live transitions reach a bounded best-effort site command; delivery, cleanup and engine-down integration are covered.
- [x] `docs/deploy.md` prerequisites and boundaries (E/I/J/D, #312, DL-439): clock-sync waiter enable/verify commands, production Python 3.12.x, drained full-root backup or consistent snapshot, output paths, site archiving and host disk monitoring are documented.
- [x] Boot-ID coverage (G-boot, #318, DL-433/DL-436): injected changed-boot tests prove no liveness check or signal against old-boot attempts and preserve holds and run identity; a real host reboot stays after shipping (K2).
- [x] JIL slice 1 (#322): config and checks, plus K1/K2 restart refusals. Done: DL-437 K2; main 0e7832d. K1 closed: main b120807 (DL-468).
- [x] JIL slice 2 (#323): process launch preparation for P, Q, R, S, AA, AE and the AD signal list; not yet wired into dispatch.
- [x] Clock-sync drill check (E, #321): native x86_64 Ubuntu Python 3.12.3 and EL8 Python 3.12.15 qualify the waiter prerequisite, bounded startup and clock behavior ([service-drill evidence](https://github.com/mrbald/dsl41/actions/runs/41597099395)).
- [x] Train cutover (#320): the accepted synthetic run through `scripts/merge_train.py` verifies pinned-head merge, strict/admin protection, cleanup and an idle empty queue; #309 is complete.

## One-box implementation and terms sweeps

- [x] Restart (DL-433).
- [x] 1. Nav verb: rebase the built `v2-nav` branch onto the `v2` tip and train it. Edits
  `README.md` (the source map names the new modules).
- [x] 2. Backward clock step: the engine admits at `max(wall, frontier)` and records the
  step; one review. Edits the runner and its sections of `docs/runner-design.md`, and the
  `clock_regressed` row of `docs/control-protocol.md` §2 (owner 2026-11-01).
- [x] Blocker for the train: `tests/test_cascade_depth.py` sem10[box-chain] fails depending
  on test order. A stray asyncio loop's `__del__` raises an unraisable `RecursionError` under
  the test's lowered recursion limit. Fix: `gc.collect()` before each
  `sys.setrecursionlimit` in that test (owner 2026-11-01). The clock slice carries it.
- [x] Blocker for wrapper stop-path acceptance: Darwin killpg EPERM can crash the recorder and turn
  a lifeline stop into unobservable; suppress only after a successful group reread proves no live
  members, preserve observed status, and cover zombie-only, live-member and unreadable cases (boss
  ruling 2026-11-01; DL-431); docs-only train may proceed.
- [x] Blocker for code merges: update_wiring drained reruns before `spawn.json` was published,
  recording `lifeline` instead of the test's `engine_stop`; the normal case waits for each rerun's
  record and barriers force both orderings with exact causes and cleanup (boss ruling
  2026-11-01; main c8e8dfe; DL-359).
- [x] Blocker for code merges: CLI read-error coverage of `cli_control.py` `_control_roundtrip`
  depended on incidental socket-readiness failures; isolated tests now assert exit 2, the
  diagnostic and no traceback (boss ruling 2026-11-01; PR #292).
- [x] Blocker for code merges: the update_wiring barrier assumed `os.waitid` (absent on macOS
  Python 3.12) and could hang in unbounded teardown; it now uses portable acknowledgements, a
  persistent release, supervised waits and bounded cleanup, and six injected faults each end
  within their budget with diagnostics and no surviving process (boss ruling 2026-11-02; PR #293).
- [x] Blocker for code merges: the real-terminal TUI driver missed ON_HOLD on main d8c6b03 (CI
  41399896410): an `h` sent before Enter restored table focus became filter text; the driver now waits
  for fresh filter, selection, focus and geometry acknowledgements, and barriers prove `h` stays unsent
  until release (boss ruling 2026-11-02).
- [x] 3. Terms sweep of `docs/control-protocol.md` and `docs/access-model.md`: replace
  retired terms (the journal where DL-428 replaced it with the store, the old supervisor
  model); no rule changes. Builds now.
- [x] 4. Terms sweep of `docs/runner-design.md` and `README.md`, as item 3. Starts after
  items 1 and 2 merge.
- [x] 5. One deployment page: the operating commands (`dsl41 run`; `dsl41 engine stop`
  drains; `dsl41 reread` then `dsl41 update` reloads; `dsl41 query status`), the unit in
  `examples/nightbank/deploy/dsl41-engine.service`, and the TUI's missing drain view.
  Builds now.
- [x] 6. Merge `v2` into `main` (#277, no tag; owner: tag later).

## Rehearsal infrastructure

- [x] Local rehearsal images: each workflow-lane session runs its own image tag (no shared `:local`
  race), the service drill installs dependencies from a wheelhouse layer, and
  `scripts/dst-drill-local.sh` runs the DST drill on Podman. Podman's layer cache already reused
  the image; the lane's time is the tests (owner 2026-11-02).
- [x] CI: the workflow lane runs on code PRs through `classify` with an explicit docs-only pass, and
  nightly on main; the service and DST drills run nightly and on their change paths; the DST drill
  runs as three parallel lanes (about 8.5 minutes instead of 22); a DL entry supersedes DL-236's
  dispatch-only rule and shared runner tag (DL-441); nightly failures notify mrbald (owner 2026-11-01).

## Estate interface

- [x] Decide a supported Python execution entry point before adding
  domain-specific decompiler recognition: `estate()` and `dsl41 build` (DL-410).
