# Attempts and the engine stop

## Purpose

An attempt is one launch of a run's command.
The attempt tier follows each attempt from its dispatch to its outcome and stops it on request.
The engine tier stops the engine: it stops every unsettled attempt, waits for them up to a deadline, and records how the engine ended.
Both tiers are built and tested against fakes; the engine does not call them yet ([DL-424](../decision-log.md)).

## Fate

The tiers hold no storage, so they need none of the storage capabilities.
The wiring journals each outcome and its follow-up plan in one transaction, which needs an atomic multi-record commit.

## Interface

- `AttemptTier(processes, grace_term_s=..., grace_kill_s=..., machines=...)` keeps one attempt instance per attempt id on the engine's executor.
- Its observations: `dispatch(job, run, n)` before anything spawns, `spawn_recorded`, `status_recorded`, `spawn_failed`, `wrapper_exited`, `stop`, `stop_all`, `record_timeout`, and `forget` for an ended attempt.
- `Processes` is what the attempt tier asks of the world: `ladder` (TERM, the grace, KILL), `kill_wrapper`, `arm_timer`, `cancel_timer`, `read_spool`, `record_outcome`, `ladder_failed` and `command_survived`.
- `EngineTier(effects, attempts)` keeps the one lifecycle instance. Its events: `replay_done`, `stop_requested`, `abort_deadline`; it posts `all_attempts_settled` itself.
- `EngineEffects` journals the drain and the shutdown records, cancels the file watchers, arms the deadline and exits the process.
- `exit_record_from_status` turns one `status.json` into the chart's exit record. `missing_record_verdict` says what a missing `status.json` means.
- `oracle_input_for` says what an outcome feeds the oracle, through the adapter's own `outcome_from_status`.
- `follow_up_plan` is the interruption rule: the plan committed with an outcome, as ordered steps.
- An attempt id is `<job>.<run>.<n>`. Its spool is `runs/<job>.<run>/<n>/`; the run's log files stay one level up.

## States

One attempt's states are [attempt](../state-machines.md#attempt): SPAWNING, RUNNING, STOPPING and ENDED, with the outcome as ENDED's data.
The engine's states are [engine_lifecycle](../state-machines.md#engine_lifecycle): STARTING, RUNNING, DRAINING and STOPPED.
Each anchor holds the generated diagram and transition table.
The [chart lifecycles](../chart-lifecycles.md) view lists each target state with the ways into it.

## Invariants

- A stop returns at once. Each ladder runs as a task of its own, so the ladders of many attempts run together ([DL-424](../decision-log.md)).
- Before `spawn.json` exists, a stop kills the wrapper, which has launched nothing ([DL-359](../decision-log.md)).
- An exit or a signal counts as the stop's while the attempt is STOPPING; an exit is natural only when its record is seen before the stop. The wrapper writes `lifeline` and `wrapper` causes itself ([wrapper protocol](../wrapper-protocol.md)).
- `record_timeout` carries the generation the tier read right after the macrostep that armed it. An older generation is discarded.
- Before a current timeout reaches the chart, the tier reads the spool once more. A `spawn.json` not yet seen runs the ladder on its group under a new generation; a `status.json` ends the attempt from the record.
- An ended attempt ignores every later observation. A record that comes after the timeout is kept as evidence, and the attempt stays ended.
- The ladder arms the timer at its KILL. An ended attempt's ladder sends nothing more.
- A ladder that raises is reported through `ladder_failed` and arms no timer: the attempt stays STOPPING and unsettled, so no replacement is dispatched and a drain ends at its deadline.
- A record timeout whose command is still alive is reported through `command_survived`, and the attempt stays STOPPING.
- `wrapper_gone` is posted only when the command is dead or was never named. A wrapper that died while its command lives gets a stop with cause `wrapper_lost`.
- An attempt is settled only once it ended and the wiring calls `plan_done` for its follow-up plan. The engine tier posts `all_attempts_settled` only in DRAINING, when no attempt is unsettled.
- An interrupted attempt of an idempotent job reruns the same run under the next attempt id. One of a job named in `non-idempotent.toml` is FAILURE with cause `interrupted`, then ON_HOLD with the evidence. A removed job, or a run the oracle ended, gets nothing ([DL-424](../decision-log.md)).
- The engine replaces an interrupted attempt at most four times: attempt `MAX_ATTEMPTS_PER_RUN`, 5, is held like a marked job's, and its evidence names the limit.

## Failure and recovery

- A command that ignores TERM gets KILL after `grace_term_s`; no record within `grace_kill_s` after that ends the attempt `unobservable`.
- A drain that does not settle by the deadline ends `unclean_shutdown` with the unsettled attempts named, and the engine exits 0.
- A second stop request moves the deadline to now.
- A stop request during the replay journals `shutdown_during_startup` and exits 0.
- A gap in either chart is a statechart error, and the engine exits 7 ([statechart-engine §5](../statechart-engine.md#5-the-step-and-the-worklist)).

## Owning modules

- `src/dsl41/runner_attempts.py`: both tiers, the translation, the verdict, the oracle mapping and the plans.
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

## Open findings

The [risk map](../risk-map.md) has no row for this block yet: the tiers are not wired.

## Gaps found

- A wrapper killed after it launched the command and before it wrote `spawn.json` leaves a command the engine cannot name. Under the systemd unit the cgroup ends it at the next stop; elsewhere it runs to completion, and the attempt ends `unobservable`. This is a stated limit of the design, not yet in the decision log.
- A process group whose leader died while members live is read as dead, so the attempt can be replaced beside the live members. It needs a job that daemonizes, an unsupported job shape. Under the systemd unit the cgroup ends the members at the next engine stop. This is a stated limit.
