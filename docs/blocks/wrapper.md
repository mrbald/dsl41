# Wrapper

## Purpose

The [wrapper](../glossary.md#wrapper) is the direct parent of one attempt's command.
Unix gives one `wait()` observation of an exit, so the direct parent is the one process that cannot miss it.
The wrapper writes that status durably, and it kills the command's group when its own parent dies.

## Fate

The code and the contract carry over unchanged. It needs none of the storage capabilities.
Reason: `runner_wrapper.py` imports only the standard library and `runner_procid.py`, and it writes two files in the attempt directory; it never touches the WAL or the ledger.
It does need the run directory on a local filesystem ([wrapper-protocol §3](../wrapper-protocol.md#3-spool-format-frozen)), which is a filesystem property, not a row of the storage table.

## Interface

Bare section numbers below refer to [wrapper-protocol](../wrapper-protocol.md).

- Run as `sys.executable <path>/runner_wrapper.py`, never with `-m`.
- Input: one JSON object on stdin, the frozen spec of [wrapper-protocol §2](../wrapper-protocol.md#2-wrapper-input-spec-frozen). `lifeline_fd` is the read end of a pipe whose write end the spawner holds.
- Output: `spawn.json`, then `status.json`, in `run_dir` ([wrapper-protocol §3](../wrapper-protocol.md#3-spool-format-frozen)). The outcome vocabulary is `WrapperOutcome`.
- Exit code: a notification only, never the outcome ([wrapper-protocol §4](../wrapper-protocol.md#4-wrapper-behavior-frozen-semantics) step 6).
- Spawner: `LocalCommandAdapter` holds the lifeline write end and builds the input with `_build_run_spec` in `runner_adapters.py`. Supervisor spawning and detached mode are outside the design (DL-424).
- Readers: `load_json`, `spool_record` and `outcome_from_status` in `src/dsl41/runner_adapters.py`, and the attempt tier (`src/dsl41/runner_attempts.py`).
- `DSL41_WRAPPER_TEST_PAUSE` names pause points for the kill-matrix tests. It is inert when unset.

## States

The wrapper is a straight-line procedure, not a declared machine; [the policies](policies.md#wrapper-outcome) say why.
The flowchart shows its steps:

```mermaid
flowchart TD
    read["read the spec on stdin"] --> valid{"the spec is valid?"}
    valid -->|"no: bad version or field type, unreadable JSON, missing key"| refused(["exit 2 or 1, no record"])
    valid -->|"yes"| session["own session, operator signals ignored"]
    session --> spawn{"spawn the command in its own process group"}
    spawn -->|"stdio open or the /bin/sh spawn fails"| spawnfail(["status spawn_failed"])
    spawn -->|"spawned"| record{"write spawn.json"}
    record -->|"the write fails"| killed(["kill the group, best-effort status, exit 3"])
    record -->|"durable"| wait{"wait; on each wakeup the exit check runs first"}
    wait -->|"exit observed"| exited["write the observed outcome, then reap"]
    wait -->|"lifeline EOF, no exit observed"| lost["SIGTERM the group, SIGKILL after the grace"]
    lost --> term["write terminated, parent lost"]
    exited --> ok(["exit 0"])
    term --> ok
```

## Invariants

- Stdlib-only, run by file path and parent-agnostic ([runner-design §6a](../runner-design.md#6a-process-lifecycle-tiers-dl-41a); DL-42).
- The command has its own process group, apart from the wrapper's, so a group kill never kills the recorder (DL-41a item 2; [wrapper-protocol §3, spawn.json](../wrapper-protocol.md#spawnjson--written-by-the-wrapper-immediately-after-spawning)).
- The lifeline's write end lives in exactly one process, the spawner. EOF means the parent died, `kill -9` included (DL-41a item 3; runner-design §6a, "Lifeline fd hygiene").
- On every wakeup the exit check runs before the lifeline-EOF check ([wrapper-protocol §4](../wrapper-protocol.md#4-wrapper-behavior-frozen-semantics) step 3).
- Record before reap, with one stated exception: the path where the `spawn.json` write failed ([wrapper-protocol §3, status.json](../wrapper-protocol.md#statusjson--written-by-the-wrapper-before-reaping); DL-229 item 1).
- Every record uses the durability liturgy on a local filesystem ([wrapper-protocol §3](../wrapper-protocol.md#3-spool-format-frozen); DL-41a item 4).
- `status.json` is the sole data channel and appears at most once. Its absence gives an `unobservable` attempt outcome; the interruption rule chooses the follow-up (§3, status.json; DL-424, DL-426).
- A pid whose start-time token does not match the record is never signalled (§3, spawn.json; DL-41a item 5).
- The `DSL41_RUN` tag is forensics only, never identity ([wrapper-protocol §3](../wrapper-protocol.md#3-spool-format-frozen)).
- The wrapper ignores a fixed set of signals, and the command restores their defaults before exec (§4 steps 1 and 2; DL-229 item 6).

## Failure and recovery

- The engine dies: the wrapper kills the group and records `terminated` with cause `parent lost` (§4 step 5). The engine derives `stopped{lifeline}` and applies the interruption rule (DL-426) ([runner-design §6a](../runner-design.md#6a-process-lifecycle-tiers-dl-41a)).
- The wrapper alone, or the whole tree, is killed with -9, and no `status.json` lands. The restart ends the attempt `unobservable{engine_death}` and its cleanup stops what is left of the command; it never waits for a wrapper, and a record a live wrapper writes after the restart's reading is read by nobody ([runner-design §7](../runner-design.md#7-journal-and-recovery-e1-prod-grade); §3, status.json; DL-424, DL-433).
- A reboot: a `boot_id` mismatch voids liveness checks and proves nothing survived (§3, spawn.json; DL-42 item 5).
- A full disk: the record write fails and the wrapper exits 3. The missing record gives an `unobservable` attempt outcome (§3; §4 step 6; DL-426).
- An oracle kill in tethered mode: the engine sends TERM to the group, waits the grace, then sends KILL, and the wrapper records the death ([runner-design §6](../runner-design.md#6-adapters)).
- A command that escapes its group with `setsid` or a double fork is not contained. That is a stated limit (runner-design §6a, "Containment honesty").

## Owning modules

- `src/dsl41/runner_wrapper.py`: the shim.
- `src/dsl41/runner_procid.py`: the liturgy, the boot id, start-time tokens and the quiet group kill.
- `src/dsl41/runner_adapters.py`: the spawner, the spec builder and the natural-outcome mapping.
- `src/dsl41/engine_charts.py`: attempt-outcome derivation.
- `src/dsl41/runner_attempts.py`: record observations and follow-up plans; it calls the stop ladder.
- `src/dsl41/runner_adapters.py`: the stop ladder.

## Tests

- Boundary: `test_wrapper_imports_are_stdlib_only`, `test_wrapper_records_under_pythonsafepath`.
- Recording: `test_wrapper_records_natural_exit_and_appends_stdout`, `test_wrapper_survives_external_group_kill_and_records_signal`, `test_wrapper_survives_sigusr1_and_sigusr2_and_still_records`, `test_wrapper_refuses_mistyped_spec_field_before_spawning`.
- Parent loss: `test_wrapper_graceful_sigterm_reaches_command_on_parent_loss`, `test_lifeline_write_end_leaks_nowhere`, `test_sigkill_engine_midrun_then_resume`.
- The restart's reading of the spool: `test_a_record_a_live_wrapper_writes_after_the_observation_is_read_by_nobody`, `test_a_live_wrapper_that_never_records_gets_the_ladder`, `test_the_spool_record_names_how_the_attempt_ended`, `test_a_crash_while_draining_is_reported_at_the_restart`.
- Identity: `test_a_kill_refuses_a_spawn_record_naming_a_stranger`, `test_a_record_that_names_another_run_tuple_is_read_as_absent`, `test_a_group_recorded_under_another_boot_is_dead`.
- Write failures: `test_rc3_a_spawn_that_cannot_be_recorded_kills_what_it_started`, `test_rc3_a_status_that_cannot_be_written_never_reports_an_outcome`, `test_rc3_a_full_disk_at_spawn_leaves_no_record_and_exits_three`.
- Outcome mapping: `test_m3_malformed_status_records_map_truthfully`.

## Open findings

See the row "Wrapper and command" in [the risk map](../risk-map.md).
`runner_wrapper.py`, `runner_procid.py` and `runner_adapters.py` are in the 100% gate ([DL-294, DL-299](../risk-map.md#closed-by-dl-289dl-306)), and the row lists no open finding.
The wrapper runs as a subprocess, and a process a test ends with SIGKILL loses its coverage data, so its numbers are floors.

## Gaps found

None.
