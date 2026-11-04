# Before shipping

For maintainers tracking the one-box release and its acceptance (owner 2026-11-02).
Items remain open until their evidence and independent acceptance are recorded.

## Release 2.0.0: pilot fixes (owner 2026-11-04)

One item = one branch = one PR to main; the owner pilots main after each merge.
Item text is the owner's request. Only the owner adds, splits, merges or renames items.
Each item is planned, the plan is reviewed, then it is built and reviewed.
The `v2-jil-3` worktree is reference only; nothing merges from it wholesale.

- [ ] File watcher with `*` or `?` completes when a matching file arrives (pilot blocker).
- [ ] KILLJOB sends INT, then KILL, as AutoSys does; `sendevent -k` overrides it; engine stop keeps TERM.
- [ ] Reread works when `who am i` and `whoami` differ (check the JIL1 fix first; fix only if still broken).
- [ ] Jobs get `DSL41_SOCKET`, and the `dsl41` CLI uses it by default, so job scripts can call their engine.
- [ ] `dsl41 reread` takes only the socket path; the engine re-reads its launch config and every JIL file its globs match.
- [ ] Select and act on jobs by `application` and `group` in the CLI and the TUI.
- [ ] A leading `>` in `std_out_file` or `std_err_file` truncates the file and `>>` appends.
- [ ] Every JIL attribute that is parsed but has no effect gives a "known but unsupported" warning at startup and reread.
- [ ] Manual: log-name placeholders `${AUTO_JOB_NAME}` and `${AUTORUN}`; the `dsl41` replacements for `sendevent`, `autorep` and `autostatus`; the unsupported attributes.
- [ ] Our locked environment stays a few days behind the newest releases, and Dependabot is configured with the same delay (about 5 days).
- [ ] Release 2.0.0: clean packages, release-workflow rehearsal, installed check, and the owner's approval of the tag.

## Release 2026-11-02: one box

- [x] Restart gaps (G-restart, #311, DL-444): installed fault evidence covers status/spawn loss, residue, quarantine, deadlines and stable invocation; pidfd signalling and standalone wrapper refusals are covered.
- [x] Storage gaps (G-storage, #308, DL-99/DL-428): real admission ENOSPC preserves UNKNOWN without an uncommitted launch and recovers after space returns; a frozen and resumed installed engine retains exclusive ownership.
- [x] Restore gaps (G-restore, #316, DL-443/DL-445): drained, writer-free full-root restore drills and fresh readers cover recovery identity and effects; all genesis witnesses refuse repeatedly without mutation; G-boot is covered by #318.
- [x] Status hook (C, #315, DL-446): committed live transitions reach a bounded best-effort site command; delivery, cleanup and engine-down integration are covered.
- [x] `docs/deploy.md` prerequisites and boundaries (E/I/J/D, #312, DL-439): clock-sync waiter enable/verify commands, production Python 3.12.x, drained full-root backup or consistent snapshot, output paths, site archiving and host disk monitoring are documented.
- [x] Boot-ID coverage (G-boot, #318, DL-433/DL-436): injected changed-boot tests prove no liveness check or signal against old-boot attempts and preserve holds and run identity; a real host reboot stays after shipping (K2).
- [x] JIL slice 1 (#322): config and checks, plus K1/K2 restart refusals.
- [x] JIL slice 2 (#323): process launch preparation for P, Q, R, S, AA, AE and the AD signal list; not yet wired into dispatch.
- [x] Clock-sync drill check (E, #321): native x86_64 Ubuntu Python 3.12.3 and EL8 Python 3.12.15 qualify the waiter prerequisite, bounded startup and clock behavior ([service-drill evidence](https://github.com/mrbald/dsl41/actions/runs/41597099395)).
- [x] Train cutover (#320): the accepted synthetic run through `scripts/merge_train.py` verifies pinned-head merge, strict/admin protection, cleanup and an idle empty queue; #309 is complete.

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
- Jobs run under `/bin/sh` in this release. `engine.job_shell` is validated (it must name `sh`, `dash`, `bash` or `ksh`) but not yet used, and a JIL `shell` attribute is refused.
- `profile` and `std_*` files on FW jobs are refused: file watchers launch no job process to consume them.
- No AutoSys-syntax command shim: job scripts call `dsl41` commands; AutoSys command syntax is not implemented.
- A real host reboot test is after shipping (K2): injected boot-ID coverage qualifies G-boot for this release, not host reboot behavior.

# Known bugs

Severity follows the accepted triage. In-flight targets remain open until accepted.

- [ ] **Untriaged**: Production file watchers treat wildcard `watch_file` paths literally. A job watching `*.csv` can remain RUNNING when a matching file arrives after startup, in both stable and immediate modes. Exact-path controls complete. Fix: release item "File watcher with `*` or `?` completes when a matching file arrives".

- [ ] **Untriaged**: `test_socket_commands_report_plain_connect_errors[missing-serve]`
  in `tests/test_cli_control_read_errors.py` exceeded its 5-second child timeout
  without output (JIL1 full Python 3.12 gate, operator triage). Cause UNKNOWN;
  the original failed source set was incompletely captured. Later passes do not
  close this bug. The owner permits continuation; recurrence stops for diagnosis.
- [ ] **Untriaged**: `test_stopped_command_kills_its_grandchild[timeout]`
  in `tests/test_merge_train_rework.py` failed to observe its ready file within
  15 seconds in the 1-second command-timeout case (JIL1 full Python 3.12 gate,
  operator triage). Cause UNKNOWN; the original failed source set was incompletely
  captured. Later passes do not close this bug. The owner permits continuation;
  recurrence stops for diagnosis.

- [ ] **Medium**: `runs` refuses the whole report when an older open attempt holds the journal's pruning
  floor over a completed run whose history the snapshot has expired by age; the leaf reader
  finds no rev for that run (Astra, slice 3b-b wiring round 5, M11; history, DL-432).
- [ ] **Medium**: `config applied.stopped` says `none` for a stop-set job whose rerun plan was committed
  before the admission and that then reruns under the new definition; the record and
  `update --wait` are wrong, nothing runs that the earlier plan did not choose (Opus, slice 3b-b
  wiring round 5, R5-2; update, DL-432).
- [x] **Critical** (closed: #311, DL-444): A quarantine after the first reading proved and TERMed a recorded group reads the cookie
  alone at its cleanup, so a cookie-less member that ignored the TERM is never KILLed and the
  attempt closes with the note naming the cookie pids only (Opus, slice restart round 2, N1;
  restart, DL-433).
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
- [x] **High** (closed: #311, DL-444; `tests/test_procid_cookie.py`): The pidfd probe tests `pidfd_open` but never `pidfd_send_signal`: a seccomp profile that
  allows only the first, or an EMFILE from `pidfd_open`, sends no signal with no fallback; the
  attempt stays held, but its note says it survived a KILL that was never sent (Opus, slice
  proc-cookie round 1, F2; restart, DL-436).
- [ ] **Low**: No operator doc names the `dsl41: cookie scan signalling: <primitive>` start line, and
  `examples/file_processing/README.md` trims output with `tail -n 2`, which hides it (Opus, slice
  proc-cookie round 2; docs, DL-436).
- [ ] **Medium**: On Darwin, the engine's `stop_ladder` takes the same EPERM path and runs `ps` for up to
  5 s on the event loop, blocking it; no test covers the engine path (Opus, slice killpg-eperm
  round 1, Low 2; wrapper, DL-431).
- [ ] **Low**: The Darwin EPERM tests fake both killpg and `ps`; no test reads real `ps` output on a real
  zombie group (Opus, slice killpg-eperm round 1, Low 3; test gap, DL-431).
- [ ] **High**: Main-config restart recompiles the captured bytes and refuses when the current binary
  rejects them (Opus, slice main-config round 1, K1; DL-437).
- [ ] **High**: Main-config restart skips preflight; legacy restart runs it (Opus, slice main-config
  round 1, K2; DL-437).
- [ ] **Medium**: A root started with the legacy unit cannot resume under the main-config unit, and nothing
  documents a path (Opus, slice main-config round 1, K4; DL-437).
- [ ] **Medium**: The pending-source scan at restart runs synchronously on the engine loop (Opus, slice
  main-config round 1, K5; DL-437).
- [ ] **Medium**: Restart ignores unknown `[engine]` keys, so a typo such as `machine_polcy` silently runs the
  default until the next reread refuses it (Opus, slice main-config round 2, K7; DL-437).
- [x] **Medium** (closed: #311, DL-444; `tests/test_wrapper_spec.py`): The standalone wrapper's spec validator (`runner_wrapper.py` `_spec_refusal`) checks the
  version, optional env, lifeline type and grace, but not frozen wrapper-protocol §2's unknown-field,
  run-id grammar, job-component and NUL refusals; a probe passed an unknown key, a non-uuid
  run_id, `job=../outside` and a NUL command. The engine's adapter supplies valid values, so this
  is the public input contract only (Sol, docs batch 1A; wrapper, DL-428).
- [ ] **Low**: The opt-in installed-CLI help test (`tests/test_installed_cli.py`) still expects the retired
  `host` command and omits `nav`, `reread`, `update` and `engine stop`; align it with `cli.py` and
  run it on installed core-only and ui-extra candidates (docs batch 2F, Opus).
- [ ] **Low**: An interrupted update keeps an equal decoded configuration row at its revision, but the
  equality ignores job order, so a row with a different `job_order` would be kept and replay
  would diverge; the token-bound plan cannot produce one today (`runner_update.py` `store_config`;
  Opus, slice config-order; DL-438).
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
- [x] **Critical** (closed: #316, DL-443/DL-445; `tests/test_genesis_eligibility.py`): A missing `engine.db` with surviving runtime evidence permits genesis and loses recovery continuity; refuse repeatedly with exit 2 and no mutation before any write, and revalidate under exclusive ownership, including the additional genesis witnesses (Sol, slice gap-restore; DL-443).

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

# After shipping

## Reliability

- [ ] Close the remaining High and Medium known bugs.
- [ ] Close G-config, G-semantic/clock, G-update, G-soak, G-history and G-upgrade gaps from the [integration test inventory](docs/integration-testing-inventory.md).
- [ ] `dsl41 backup` (J step 2): provide an online backup command that captures the store and open attempts' records consistently; accept restoration evidence.

## Qualification

- [ ] Close G-artifact, G-access, G-recipes, G-retire and G-package gaps from the [integration test inventory](docs/integration-testing-inventory.md).
- [ ] Real VM reboot test (K2): test recovery across an actual reboot in a throwaway VM; injected boot-ID coverage closes G-boot for this release (#318, DL-433/DL-436).

## Maintenance

- [ ] Close Low known bugs and G-correction and G-wargame gaps.
- [ ] Low: the engine's per-attempt `runs/<job>.<run>/<n>/` folders accumulate small records (`spawn.json`, `status.json`) and consume inodes; define and verify retention separately from job-output archiving.
- [ ] Architecture review (H); cap concurrent container slices at two.
- [ ] Triage the Dependabot updates in one batch (H).
- [ ] Required-check activation: collect hosted evidence for a code PR, a docs-only PR, each workflow's dispatch and the first nightly runs with notification, then activate `workflow-examples-required` under the owner's approval.
- [ ] Every container-based rehearsal and wargame runs green on v2: `scripts/wargame.sh`, the service drill (`examples/nightbank/deploy/drill-*.sh`, `.github/workflows/service-drill.yml`) and the DST drill (`examples/dst-drill/`, `.github/workflows/dst-drill.yml`).
- [ ] Clean, coherent docs: every page states the v2 design as built, in one voice, with no retired terms or contradictions between pages.
- [ ] Control and TUI re-cut: show the drain, its deadline, the stored plan and interruption evidence. Workaround: `dsl41 query status` and the `dsl41 engine stop` exit status.
- [ ] Short rewrites of `docs/runner-design.md`, `docs/control-protocol.md` and `docs/access-model.md`, the second deployment page and the nav verb page. The contracts are long and keep history; the completed terms sweeps removed retired terms without changing rules.
- [ ] After the file-processing building block: improve the Python estate interface produced by `dsl41 decompile`. Make the output easy to import, configure, execute and maintain as the starting point for migration from JIL. Use synthetic estates to develop a migration guide. Preserve canonical equivalence, explicit conditions and the closed fold registry. The file-processing prototype in `examples/file_processing/` is the first authoring use case.
- [ ] Discuss an explicit restart-and-apply policy for main config: who reviews, stop consent, running boxes, interruption decisions and audit records (boss ruling 2026-11-01; the owner liked force-accept; restart today resumes on the stored catalog, DL-437).

# Completed history

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
