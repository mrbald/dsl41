# Release 2026-11-02: one box

The target is one box: restart, reload and drain as built and tested. Builds run in
parallel; merges go through the train in the order below. Slices run in parallel only on
disjoint files.

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

The sweeps edit frozen contracts, so they are semantic class: one reviewer of the other
vendor than the implementer. Review items 3 and 4 in one batch each.

Deferred to the next milestone, active/standby on HA Oracle and the 200-job estate:

- The control and TUI re-cut. Gap: the TUI does not show the drain, its deadline, the
  stored plan or the interruption evidence. Workaround: `dsl41 query status` and the
  `dsl41 engine stop` exit status.
- Short rewrites of `docs/runner-design.md`, `docs/control-protocol.md` and
  `docs/access-model.md`, the second deployment page and the nav verb page. Gap: the
  contracts are long and keep history. Workaround: items 3 and 4 remove the retired terms
  without changing a rule.

# Next priority after the release (owner 2026-11-01)

- [ ] Clean, coherent docs: every page states the v2 design as built, in one voice, with no
  retired terms or contradictions between pages.
- [ ] Every container-based rehearsal and wargame runs green on v2. The v2 cut removed them;
  on `main` they are `scripts/wargame.sh`, the service drill
  (`examples/nightbank/deploy/drill-*.sh`, `.github/workflows/service-drill.yml`) and the
  DST drill (`examples/dst-drill/`, `.github/workflows/dst-drill.yml`).

# Next work

- [ ] After the file-processing building block: improve the Python estate interface
  produced by `dsl41 decompile`. Make the output easy to import, configure, execute
  and maintain as the starting point for migration from JIL. Use synthetic estates
  to develop a migration guide. Preserve canonical equivalence, explicit conditions
  and the closed fold registry. The file-processing prototype in
  `examples/file_processing/` is the first authoring use case.
  - [x] Decide a supported Python execution entry point before adding
    domain-specific decompiler recognition: `estate()` and `dsl41 build` (DL-410).

# Known bugs

Found after an area was sealed as good enough. Addressed once the scope runs end to end.

- [ ] `runs` refuses the whole report when an older open attempt holds the journal's pruning
  floor over a completed run whose history the snapshot has expired by age; the leaf reader
  finds no rev for that run (Astra, slice 3b-b wiring round 5, M11; history, DL-432).
- [ ] `config applied.stopped` says `none` for a stop-set job whose rerun plan was committed
  before the admission and that then reruns under the new definition; the record and
  `update --wait` are wrong, nothing runs that the earlier plan did not choose (Opus, slice 3b-b
  wiring round 5, R5-2; update, DL-432).
- [ ] A quarantine after the first reading proved and TERMed a recorded group reads the cookie
  alone at its cleanup, so a cookie-less member that ignored the TERM is never KILLed and the
  attempt closes with the note naming the cookie pids only (Opus, slice restart round 2, N1;
  restart, DL-433).
- [ ] While recovering, the resumed update's look takes the latest stamp of any queued item, so
  an input injected for later on a virtual clock pushes the application behind it (Opus, slice
  restart round 2, N2; update, DL-433).
- [ ] The equal-stamp test's `first-look` case passes without the fix; only `last-stop` pins
  the update taking no step during the catch-up (Opus, slice restart round 2, N3; test gap,
  DL-433).
- [ ] A real restart raises the admission frontier to the store's last stamp even when the wall
  is ahead of it, so recovered completions take the last dispatch's time instead of their
  recorded end; no reordering found (Opus, slice clock round 2, N1; clock, DL-435).
- [ ] Nothing bounds a backward gap: a store stamped far ahead starts and holds every tick and
  timer until the wall catches up, with one warning line as the only trace (Opus, slice clock
  round 2, N2; clock, DL-435).
- [ ] `tests/test_update_wiring.py::test_two_running_jobs_are_stopped_and_eighteen_untouched_with_the_engine_running`
  races: a drain that closes the lifeline before the wrapper writes `spawn.json` records the
  stop as `lifeline`, not `engine_stop`. Failed once in a Linux cell; reproduces on 7be633b with
  a paused wrapper (Sol and Opus, slice clock; update, DL-432).
- [ ] After a backward wall-clock step, `dispatch`, `outcome` and `engine` records carry the raw
  wall stamp, so they can sit below an earlier `input` in the store; DL-435 floors admission
  stamps only (W2 drill builder, clock-step scenario; clock, DL-435).
- [ ] The pidfd probe tests `pidfd_open` but never `pidfd_send_signal`: a seccomp profile that
  allows only the first, or an EMFILE from `pidfd_open`, sends no signal with no fallback; the
  attempt stays held, but its note says it survived a KILL that was never sent (Opus, slice
  proc-cookie round 1, F2; restart, DL-436).
- [ ] No operator doc names the `dsl41: cookie scan signalling: <primitive>` start line, and
  `examples/file_processing/README.md` trims output with `tail -n 2`, which hides it (Opus, slice
  proc-cookie round 2; docs, DL-436).
