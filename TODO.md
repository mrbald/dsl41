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
- [ ] No test fails when the restart's queued first look at the stop set is removed; without it
  an update whose stops were all finished before the crash stays STOPPING (Opus, slice 3b-b
  wiring round 5, R5-1; test gap, DL-432).
