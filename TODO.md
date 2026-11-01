# Release 2026-11-02: one box

The target is one box: restart, reload and drain as built and tested. Builds run in
parallel; merges go through the train in the order below. Slices run in parallel only on
disjoint files.

- [x] Restart (DL-433).
- [ ] 1. Nav verb: rebase the built `v2-nav` branch onto the `v2` tip and train it. Edits
  `README.md` (the source map names the new modules).
- [ ] 2. Backward clock step: the engine admits at `max(wall, frontier)` and records the
  step; one review. Edits the runner and its sections of `docs/runner-design.md`.
- [ ] 3. Terms sweep of `docs/control-protocol.md` and `docs/access-model.md`: replace
  retired terms (the journal where DL-428 replaced it with the store, the old supervisor
  model); no rule changes. Builds now.
- [ ] 4. Terms sweep of `docs/runner-design.md` and `README.md`, as item 3. Starts after
  items 1 and 2 merge.
- [ ] 5. One deployment page: the operating commands (`dsl41 run`; `dsl41 engine stop`
  drains; `dsl41 reread` then `dsl41 update` reloads; `dsl41 query status`), the unit in
  `examples/nightbank/deploy/dsl41-engine.service`, and the TUI's missing drain view.
  Builds now.
- [ ] 6. Merge `v2` into `main`.

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
