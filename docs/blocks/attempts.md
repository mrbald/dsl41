# Attempts and the engine stop

## Purpose

An attempt is one launch of a run's command.
The attempt tier follows each attempt from its dispatch to its outcome and stops it on request.
The engine tier stops the engine: it stops every unsettled attempt, waits for them up to a deadline, and records how the engine ended.
The engine runs both: every command attempt, every drain and a restart's catch-up of the attempts it finds open go through them ([DL-424](../decision-log.md), DL-433).

## Fate

The tiers hold no storage, so they need none of the storage capabilities.
The wiring journals each outcome and its follow-up plan in one transaction, which needs an atomic multi-record commit.

## Interface

- `AttemptTier(processes, grace_term_s=..., grace_kill_s=..., machines=...)` keeps one attempt instance per attempt on the engine's executor, keyed by the attempt's run id.
- Its observations: `dispatch(run_id)` before anything spawns, `spawn_recorded`, `status_recorded`, `spawn_failed`, `wrapper_exited`, `stop`, `stop_all`, `record_timeout`, and `forget` for an ended attempt; a restart's `restore_ended`, `restored_stop`, `engine_died` and `cleanup`.
- `Processes` is what the attempt tier asks of the world: `ladder` (TERM, the grace, KILL), `kill_wrapper`, `arm_timer`, `cancel_timer`, `read_spool`, `record_outcome`, `ladder_failed`, `command_survived`, `outlives` and `leaked`.
- `EngineTier(effects, attempts)` keeps the one lifecycle instance. Its events: `replay_done`, `stop_requested`, `abort_deadline`; it posts `all_attempts_settled` itself.
- `EngineEffects` journals the drain and the shutdown records, cancels the file watchers, arms the deadline and exits the process.
- `exit_record_from_status` turns one `status.json` into the chart's exit record. `missing_record_verdict` says what a missing `status.json` means.
- `oracle_input_for` says what an outcome feeds the oracle, through the adapter's own `outcome_from_status`.
- `follow_up_plan` is the interruption rule: the plan committed with an outcome, as ordered steps.
- An attempt's identity is the uuid4 `run_id` its `dispatch` record binds ([DL-118](../decision-log.md)). Its ordinal `n` names its spool, `runs/<job>.<run>/<n>/`, its display `<job>.<run>.<n>` and its `DSL41_ATTEMPT`; the run's log files stay one level up.
- The engine is both protocols (`runner_stops.EngineStops`). `LocalCommandAdapter.observe` reports the wrapper's spool to the attempt tier; `stop_ladder` is the ladder.
- `dsl41 engine stop` (the socket's `engine_stop`, adm tier) and SIGTERM post `stop_requested` as a step of the engine's single writer.

## States

One attempt's states are [attempt](../state-machines.md#attempt): SPAWNING, RUNNING, STOPPING and ENDED, with the outcome as ENDED's data.
The engine's states are [engine_lifecycle](../state-machines.md#engine_lifecycle): STARTING, RUNNING, DRAINING and STOPPED.
Each anchor holds the generated diagram and transition table.
The [chart lifecycles](../chart-lifecycles.md) view lists each target state with the ways into it.

## Invariants

- A stop returns at once. Each ladder runs as a task of its own, so the ladders of many attempts run together ([DL-424](../decision-log.md)).
- Before the wrapper has its spec, a stop kills it, since it has launched nothing ([DL-359](../decision-log.md)). After the spec and before `spawn.json`, the stop closes the wrapper's lifeline, so it ends whatever it launches and records it; once `spawn.json` exists the ladder runs on its group.
- A kill the oracle decides (KILLJOB, `term_run_time`) is a stop with cause `oracle_kill`; its plan is always `none`, whatever the row says when the record lands.
- The ladder's closing SIGKILL also goes once the leader is gone while the group still exists, and a group that exists after its ladder has survived its stop ([DL-226](../decision-log.md)).
- A mutation admitted after `stop_requested` is rejected with `engine_draining`, its time half applied; queries, `subscribe` and `engine_stop` stay legal. While draining, a start the oracle decides is journaled `dispatch_pending` for the restart.
- An exit or a signal counts as the stop's while the attempt is STOPPING; an exit is natural only when its record is seen before the stop. The wrapper writes `lifeline` and `wrapper` causes itself ([wrapper protocol](../wrapper-protocol.md)).
- `record_timeout` carries the generation the tier read right after the macrostep that armed it. An older generation is discarded.
- Before a current timeout reaches the chart, the tier reads the spool once more. A `spawn.json` not yet seen runs the ladder on its group under a new generation; a `status.json` ends the attempt from the record.
- An ended attempt ignores every later observation. A record that comes after the timeout is kept as evidence, and the attempt stays ended.
- The ladder arms the timer at its KILL. The record ends the attempt's observation, never its cleanup: the record is followed by a reading of what is left. A ladder already running runs on. Something left after a natural exit is a leak and starts one. Something left after a stop whose ladder already ended is the stop's, whatever `process-leak` says: it takes the survivor verdict, `grace_kill_s` after the KILL as the record timeout takes a stopping one's, and is held and re-KILLed until a reading finds nothing.
- A ladder that raises is reported through `ladder_failed` and arms no timer. The stop was not carried out, so the attempt is held: it stays unsettled, no `ladder_done` is written, and a drain ends at its deadline.
- A record timeout whose command is still alive is reported through `command_survived`, and the attempt stays STOPPING and held.
- A process left when the command exits is a leak, by the `process-leak` switch (DL-431). Under `terminate`, dsl41's default, the attempt is reported through `leaked` and the ladder runs on what is left before the outcome is journaled, so the outcome stays open (`ladder: running`) until `ladder_done`, and the job's next run waits. `status` notes `leaked: n, stopping`. The outcome stays what the command returned. Under `leak`, the vendor's reading, nothing is signalled, the attempt settles at once, and one warning names the job, the run and the pids left; the drain and the restart leave them too. The switch governs leaks only: a stop the crash cut short is finished at the restart whatever its value.
- On Linux, where a pidfd can signal it, every process of an attempt carries the process cookie, `DSL41_COOKIE=<root_id>.<run id>`, which its children inherit; `root_id` names the run root, so one scan reads every attempt of the root, whichever engine start launched it ([wrapper protocol](../wrapper-protocol.md)). One function says what is left of an attempt (`EngineStops.left`), and every check reads it: the recorded group's live members and every process carrying the cookie, whatever its group, the wrapper left out. A main thread that exited while a worker runs still counts. Both are taken as reliable: the engine is an orchestrator, not a jail. Only a reading proves a group the attempt's, never a record: its leader passes its start token or a member carries the cookie, and the live engine reads the group as soon as it sees `spawn.json`. The proof stays while the group has members; the first reading that finds it empty ends it. A command that exits before that first reading leaves its group unproven, as a restart finds one. An error on a `/proc` entry means a process gone (ENOENT, ESRCH) or not ours (EPERM, EACCES: under `hidepid=noaccess` another uid's entries are skipped, never a failed reading); any other error is a failed reading, `unknown`: what is known is still signalled, nothing is released, and the note says `scan failed: <errno>`. Without a pidfd the engine does not scan and says so once at start; then, and for an attempt dispatched before the cookie, the leader's start token and the group decide.
- Where the cookie decides, the ladder signals each process by itself through a pidfd after its start token is checked, from a fresh reading at the TERM and at the KILL; no group signal goes. A process that moves between groups gets one TERM and one KILL. The ladder ends at its KILL, where the record timeout is armed. The held poll KILLs, at each look, what it finds of an attempt that outlived a stop, and releases it at the first look that finds nothing.
- A job's next dispatch goes only when no attempt of the job is unsettled. An attempt settles once its plan is done, after its record and its ladder, so every stop still in progress keeps the next run back, whatever its shape. A held attempt, whose group may still live with no stop left to end it, settles only once the group is gone; the engine looks for it every half second. A start the oracle decides meanwhile is journaled `dispatch_pending` and launched once the attempt settles. A replacement waits only for its own attempt's ladder or held group, live and at the restart. `status` shows the attempt that keeps them back beside the job ([DL-426](../decision-log.md)).
- A start of a job whose earlier run is still live, after an injected INACTIVE ([DL-235](../decision-log.md)), first stops that run's attempt as an oracle kill, then waits for it to settle like any other start. No path stops one attempt and launches another in the same step.
- `wrapper_gone` is posted only when the command is dead or was never named. A wrapper that died while its command lives gets a stop with cause `wrapper_lost`. `engine_died` is a restart's only. It ends an attempt with no record from SPAWNING, RUNNING or STOPPING as `unobservable`, with the cause of the stop it took first, else `engine_death`. It runs no ladder and arms no timer; what is left of the command is cleanup, as at any record.
- An attempt is settled only once it ended and the wiring calls `plan_done` for its follow-up plan. The engine tier posts `all_attempts_settled` only in DRAINING, when no attempt is unsettled.
- An interrupted attempt of an idempotent job reruns the same run under the next ordinal, with a run id the plan names before it can run and the same `DSL41_JOB`, `DSL41_RUN_NUMBER` and `DSL41_SCHEDULED_AT`. While the engine drains, the rerun waits for the restart. One of a job named in `non-idempotent.toml` is one oracle transition: a FAILURE with cause `interrupted` that holds the job before its own start or a dependent's is evaluated, with the evidence. A removed job, or a run the oracle ended, gets nothing ([DL-424](../decision-log.md)).
- The engine replaces an interrupted attempt at most four times: attempt `MAX_ATTEMPTS_PER_RUN`, 5, is held like a marked job's, and its evidence names the limit.

## Failure and recovery

- A command that ignores TERM gets KILL after `grace_term_s`; no record within `grace_kill_s` after that ends the attempt `unobservable`.
- A drain that does not settle by the deadline ends `unclean_shutdown` with the unsettled attempts named, and the engine exits 0.
- A second stop request moves the deadline to now.
- A stop request during the replay journals `shutdown_during_startup` and exits 0.
- A crash while draining is found at the restart from the replayed lifecycle, which journals `drain_interrupted`; every attempt the `draining` record names keeps its stop, posted before any record, so an exit the drain's TERM caused stays `stopped`.
- Only the lifecycle's STOPPED writes a shutdown record, before the snapshot that covers it; a crash, or a failed write, exits 1 and leaves the record out.
- An engine death ends every running attempt, and the restart waits for nothing (DL-433). Every open attempt has its instance before the loop's first step: in SPAWNING, or in ENDED with its committed outcome when it is closing, never observed again. One cookie scan of the run root is the first reading, and it finishes what the engine's death ended: what it finds of an attempt with no outcome whose spool holds no natural record (none, a stop's, or one that does not read), or of a closing attempt but a `natural` one under `process-leak=leak`, is TERMed, the first rung of the attempt's ladder. What ended by its own record follows the live leak rule at its observation; a closing `natural` attempt's processes under `leak` are named and left running; a process of no open attempt is reported and left alone.
- Each attempt without an outcome is observed once, as a step of the loop, before any time advance: its spool read once, `spawn_recorded`, the recorded first stop, then its record or `engine_died`. A wrapper found alive is not awaited, and a record it writes later is read by nobody. Each closing attempt gets one cleanup step: a reading of what is left. Either way the live record path does the rest: something left gets the ladder (the leak ladder under `terminate` after a natural exit, the ladder whatever the switch otherwise), the outcome is journaled `ladder: running`, and `ladder_done` closes it once a reading finds nothing left. Something that outlives the KILL holds the attempt as `command survived its KILL`. A group whose leader fails its start token and none of whose members carries the cookie is a stranger's that took the id after a pid wrap (`pid_max`, 4194304 under systemd 243 and later): it is never signalled, and the attempt closes. Without the cookie the restart signals the group only while its leader is still the recorded process, and members whose leader is gone keep the attempt held for the operator.
- A restart quarantines an attempt whose records cannot be trusted (a spool record naming another run id than the dispatch bound, or a spool or record that cannot be read): one `unobservable` outcome with cause `quarantined`, the reason and the pids the first reading found as its note; the hold forced wherever the interruption rule would rerun or hold, `none` where the oracle ended the run or the job left the catalog. What is left of it is the live cleanup through its cookie alone, so the first reading's TERM gets its grace and KILL. A failed reading is never a quarantine, at the first reading, a cleanup or a ladder's end: the record decides the outcome and what is left is held as live, re-read at each look, never signalled under `process-leak=leak`.
- Stated limit: outside the recorded group the engine sees only what the scan can read. A process that leaves the group and drops its cookie (`setsid` and `env -i`, or `setsid` and a main thread that exits), one that leaves it and turns non-dumpable (`ssh-agent`), and one that leaves it under another uid are beyond the engine by design; their attempt closes while they live. A fork-and-exit chain that races every reading is a hostile payload, not a leak. Once its leader is gone, a group with no cookie member is read as a stranger's. The unit's cgroup is the stronger fence under systemd.
- A gap in either chart is a statechart error, and the engine exits 7 ([statechart-engine §5](../statechart-engine.md#5-the-step-and-the-worklist)).

## Owning modules

- `src/dsl41/runner_attempts.py`: both tiers, the translation, the verdict, the oracle mapping and the plans.
- `src/dsl41/runner_stops.py`: the engine's side of the tiers: the ladder, the timers, the spool reads.
- `src/dsl41/runner.py`, `src/dsl41/runner_startup.py`: the plans carried out, the drain, and the restart's fold, first reading and catch-up.
- `src/dsl41/engine_charts.py`: the two charts and their records.

## Tests

- `test_a_status_record_translates_to_its_exit_record_and_outcome`
- `test_a_gone_wrapper_with_a_dead_command_is_gone_and_with_a_live_one_is_lost`
- `test_each_outcome_feeds_the_oracle_what_the_adapter_maps`
- `test_an_interrupted_idempotent_job_reruns_the_same_run_under_the_next_attempt_id`
- `test_an_interrupted_marked_job_is_failure_interrupted_then_held_with_its_evidence`
- `test_a_run_the_oracle_ended_or_a_later_run_gets_no_follow_up`
- `test_a_drain_with_twenty_attempts_runs_their_ladders_together_and_awaits_none`
- `test_a_command_that_ignores_term_is_killed_and_the_timer_armed_after_the_kill`
- `test_a_wrapper_that_launched_after_the_stop_gets_a_ladder_and_a_new_generation`
- `test_a_second_stop_request_moves_the_deadline_to_now`
- `test_the_tiers_alone_reach_every_coverage_item_of_both_charts_but_one`
- `test_a_drain_stops_every_attempt_and_records_a_clean_shutdown`
- `test_a_second_stop_moves_the_deadline_to_now_and_the_restart_reruns`
- `test_a_crash_while_draining_is_reported_at_the_restart`
- `test_a_mutation_is_admitted_wholly_before_the_stop_or_refused_after_it`
- `test_an_attempt_and_its_replacement_keep_one_identity_through_the_store`
- `test_a_ladder_that_raises_after_the_record_leaves_the_attempt_held`
- `test_a_new_run_waits_while_its_job_s_group_survives`
- `test_a_new_run_waits_for_the_stop_ladder_of_the_run_before`
- `test_a_restart_after_an_injected_inactive_waits_for_the_earlier_run_s_ladder`
- `test_a_stop_before_the_spawn_is_observed_keeps_the_next_run_back`
- `test_a_crash_between_the_outcome_and_the_closing_kill_never_overlaps_a_rerun`
- `test_a_restart_finishes_the_ladder_of_a_closing_attempt`
- `test_a_leaked_child_is_termed_then_killed_and_the_next_run_waits`
- `test_under_leak_a_leaked_child_runs_on_and_the_next_run_launches_at_once`
- `test_a_restart_stops_a_leaked_child_whatever_the_crash_window`
- `test_the_restart_never_reads_the_previous_engine_s_wrapper`
- `test_a_stranger_in_the_recorded_group_is_never_signalled_and_the_attempt_closes`
- `test_a_child_that_leaves_the_group_and_drops_its_cookie_is_beyond_the_engine`

## Open findings

The [risk map](../risk-map.md) has no row for this block yet.

## Gaps found

- A wrapper killed after it launched the command and before it wrote `spawn.json` leaves a command no record names. On Linux the processes carrying its cookie are what is left, and the cleanup's ladder stops them, live and at a restart (DL-433). Elsewhere the engine cannot name it: under the systemd unit the cgroup ends it at the next stop, otherwise it runs to completion, and the attempt ends `unobservable`. This is a stated limit ([DL-426](../decision-log.md)).
- Without the cookie scan (another platform, no pidfd, an attempt dispatched before the cookie), a process group whose leader exited on its own while members live is not read as a leak, so the next run can start beside them. Under the systemd unit the cgroup ends the members at the next engine stop. This is a stated limit. On Linux the leak rule closes it, except for the stated limit under Invariants.
