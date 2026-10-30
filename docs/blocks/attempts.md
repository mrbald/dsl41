# Attempts and the engine stop

## Purpose

An attempt is one launch of a run's command.
The attempt tier follows each attempt from its dispatch to its outcome and stops it on request.
The engine tier stops the engine: it stops every unsettled attempt, waits for them up to a deadline, and records how the engine ended.
The engine runs both: every command attempt, every drain and the restart's settlement of open attempts go through them ([DL-424](../decision-log.md)).

## Fate

The tiers hold no storage, so they need none of the storage capabilities.
The wiring journals each outcome and its follow-up plan in one transaction, which needs an atomic multi-record commit.

## Interface

- `AttemptTier(processes, grace_term_s=..., grace_kill_s=..., machines=...)` keeps one attempt instance per attempt on the engine's executor, keyed by the attempt's run id.
- Its observations: `dispatch(run_id)` before anything spawns, `spawn_recorded`, `status_recorded`, `spawn_failed`, `wrapper_exited`, `stop`, `stop_all`, `record_timeout`, and `forget` for an ended attempt.
- `Processes` is what the attempt tier asks of the world: `ladder` (TERM, the grace, KILL), `kill_wrapper`, `arm_timer`, `cancel_timer`, `read_spool`, `record_outcome`, `ladder_failed` and `command_survived`.
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
- The ladder arms the timer at its KILL. An ended attempt's ladder sends nothing more.
- A ladder that raises is reported through `ladder_failed` and arms no timer. The stop was not carried out, so the attempt is held: it stays unsettled, no `ladder_done` is written, and a drain ends at its deadline.
- A record timeout whose command is still alive is reported through `command_survived`, and the attempt stays STOPPING and held.
- A job's next dispatch goes only when no attempt of the job is unsettled. An attempt settles once its plan is done, after its record and its ladder, so every stop still in progress keeps the next run back, whatever its shape. A held attempt, whose group may still live with no stop left to end it, settles only once the group is gone; the engine looks for it every half second. A start the oracle decides meanwhile is journaled `dispatch_pending` and launched once the attempt settles. A replacement waits only for its own attempt's ladder or held group, live and at the restart. `status` shows the attempt that keeps them back beside the job ([DL-426](../decision-log.md)).
- A start of a job whose earlier run is still live, after an injected INACTIVE ([DL-235](../decision-log.md)), first stops that run's attempt as an oracle kill, then waits for it to settle like any other start. No path stops one attempt and launches another in the same step.
- `wrapper_gone` is posted only when the command is dead or was never named. A wrapper that died while its command lives gets a stop with cause `wrapper_lost`.
- An attempt is settled only once it ended and the wiring calls `plan_done` for its follow-up plan. The engine tier posts `all_attempts_settled` only in DRAINING, when no attempt is unsettled.
- An interrupted attempt of an idempotent job reruns the same run under the next ordinal, with a run id the plan names before it can run and the same `DSL41_JOB`, `DSL41_RUN_NUMBER` and `DSL41_SCHEDULED_AT`. While the engine drains, the rerun waits for the restart. One of a job named in `non-idempotent.toml` is one oracle transition: a FAILURE with cause `interrupted` that holds the job before its own start or a dependent's is evaluated, with the evidence. A removed job, or a run the oracle ended, gets nothing ([DL-424](../decision-log.md)).
- The engine replaces an interrupted attempt at most four times: attempt `MAX_ATTEMPTS_PER_RUN`, 5, is held like a marked job's, and its evidence names the limit.

## Failure and recovery

- A command that ignores TERM gets KILL after `grace_term_s`; no record within `grace_kill_s` after that ends the attempt `unobservable`.
- A drain that does not settle by the deadline ends `unclean_shutdown` with the unsettled attempts named, and the engine exits 0.
- A second stop request moves the deadline to now.
- A stop request during the replay journals `shutdown_during_startup` and exits 0.
- A crash while draining is found at the restart from the replayed lifecycle, which journals `drain_interrupted`; every attempt the `draining` record names is adopted with its stop, so an exit the drain's TERM caused stays `stopped`.
- Only the lifecycle's STOPPED writes a shutdown record, before the snapshot that covers it; a crash, or a failed write, exits 1 and leaves the record out.
- The restart settles each open attempt through its chart from the spool: a live wrapper gets its window to record, a command that outlived its wrapper gets the ladder.
- The restart finishes the ladder of an attempt whose outcome was committed while its ladder ran. It signals the group only while its leader is still the recorded process. Members whose leader is gone get nothing, since another command may have taken the group id during the outage: the attempt stays held for the operator, and the unit's cgroup ends them at the next stop.
- A gap in either chart is a statechart error, and the engine exits 7 ([statechart-engine §5](../statechart-engine.md#5-the-step-and-the-worklist)).

## Owning modules

- `src/dsl41/runner_attempts.py`: both tiers, the translation, the verdict, the oracle mapping and the plans.
- `src/dsl41/runner_stops.py`: the engine's side of the tiers: the ladder, the timers, the spool reads.
- `src/dsl41/runner.py`, `src/dsl41/runner_startup.py`: the plans carried out, the drain, and the restart's step 5.
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

## Open findings

The [risk map](../risk-map.md) has no row for this block yet.

## Gaps found

- A wrapper killed after it launched the command and before it wrote `spawn.json` leaves a command the engine cannot name. Under the systemd unit the cgroup ends it at the next stop; elsewhere it runs to completion, and the attempt ends `unobservable`. This is a stated limit ([DL-426](../decision-log.md)).
- A process group whose leader died while members live is read as dead, so the attempt can be replaced beside the live members. It needs a job that daemonizes, an unsupported job shape. Under the systemd unit the cgroup ends the members at the next engine stop. This is a stated limit.
