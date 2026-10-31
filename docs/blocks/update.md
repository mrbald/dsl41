# Update: reread and update

## Purpose

`dsl41 reread` diffs the estate on disk against the catalog the engine runs and stores a plan.
`dsl41 update` applies that plan to the running engine: it stops the running jobs the plan changes or removes, swaps the catalog, the settings and the marked set, and keeps the engine running throughout ([DL-427](../decision-log.md)).

## Fate

The update needs an atomic multi-record commit for its records and decision lookup by `request_id` for an exact retry.
Its intent and its application are store records, so a crash between them resumes it.

## Interface

- `dsl41 reread ESTATE_DIR --run-root R` reads the engine through the socket's `catalog` verb (read tier) and writes `R/plans/<token>/`; `dsl41 update TOKEN --run-root R [--stop-running] [--wait]` sends the socket's `update` (adm tier) ([control protocol §2](../control-protocol.md#2-transport-and-framing-frozen)).
- `reload_plan.reread` computes and stores the plan; `reload_update.admission_check` re-checks it against the engine's state now; `reload_update.plan_application` and `apply_application` compute and apply the swap.
- `runner_update.EngineUpdate` binds the update chart's tier to the engine: the admission as one step of the single writer, the stops through the attempt tier, the dispatch filter, the application and its records.
- The store records `config intent` (the token, the stop set with the attempt each stop interrupts, the dispatch filter, the request's id and fingerprint) and `config applied` (the new rev, its segment fields and the job sets). A configuration row holds each rev's catalog, profile and marked set.
- `status` shows `config_rev`, `update_window_s` and, while an update stops, the update and the jobs it waits for; the TUI shows the rev and the update in its subtitle. Once applied, `spec` and `spec_drift` follow the applied plan.

## States

One update request's states are [update](../state-machines.md#update): IDLE, STOPPING, APPLIED and REFUSED, with the token and the stop set as the instance's data.
The anchor holds the generated diagram and transition table.
The [chart lifecycles](../chart-lifecycles.md) view lists each target state with the ways into it.

## Invariants

- One update is in flight at a time; a second is refused with `update_in_progress` ([DL-427](../decision-log.md)).
- The admission re-reads the stop set and the running boxes from the engine's state now; a plan diffed against another rev is `stale_plan`, a structural edit of a running box `box_running`, and a stop without consent `would_stop_running`. A refusal writes nothing.
- The engine loads the candidate from the plan's own bytes, with the environment and the site layers `invocation.json` names, and runs it under the settings it read itself.
- While the update stops, no job of the plan's added, changed and removed sets is dispatched: a start the oracle decides for one waits as `dispatch_pending` and goes after the application.
- An attempt the update stops keeps its outcome back until the application, so its follow-up plan is chosen under the applied catalog and marked set, once, and the `config applied` record names it: a removed job gets none, a marked one is held, any other reruns the same run under its new definition ([DL-424](../decision-log.md)).
- A stop is finished when its job's next run could go: its ladder done and a reading finding nothing of it left, not at its record ([DL-431](../decision-log.md)). The update applies only once every stop is finished, an attempt that ended before the admission while its cleanup still runs included.
- An interrupted attempt of a job whose `non-idempotent.toml` mark the plan changes keeps its outcome back too, so its plan follows the applied mark; the update neither stops nor waits for it.
- The intent records each attempt with the cause of its first stop: `update_stop`, or the cause of a stop it already takes, which a restart keeps.
- The application is one `config applied` record and one configuration row; the oracle swaps its catalog between inputs, the scheduler rebuilds only the changed and added plans and drops the removed ones, and one time advance follows at once so the queue scan the swap owes runs first. The oracle's own timers (run windows, `term_run_time`) are untouched: a changed job's deadline keeps counting across the update.
- A run's configuration rev is recorded on its run row when it opens: the rev in force where it opened in record order, which a snapshot's retained run history keeps as a required key (state version 26). `dsl41 runs` reads each run under that rev and nothing else: its catalog hash, its job fingerprint, its box and its exit-code verdict. A `dispatch` record's `rev` is that attempt's own (the definition it ran), never read as the run's. The `CONFIG` trace marker places each application between two inputs.
- `spec` and the `spec_drift` baseline follow the applied plan, each file at the path `reread` read it from (site layers included), never the plan directory's copy.
- A snapshot carries the tombstones, the owed scan, the ghost resources and the update in STOPPING; replay swaps the configuration where each `segment` stands.
- A restart runs the store's last applied configuration; the estate it is launched with must equal it, or the open update's candidate, job by job.

## Failure and recovery

- A crash between `config intent` and `config applied` is resumed by startup step 4, which restores the update in STOPPING with its dispatch filter holding; nothing waits for it. Step 5 settles its attempts like any open one, stopping each as a live engine does (ask to stop, TERM; ask to abort, KILL; the reading after), and the engine starts. The update applies as a step of the loop once its stops are finished, wherever the last one finishes. No job of the plan is dispatched and no kept outcome is committed before the application.
- A plan an older `reread` wrote (no estate directory) is refused `stale_plan` before anything is written.
- A plan directory removed while an update stops refuses the restart by name.
- The admission checks the plan's revision and the catalog hash `reread` saw before it writes anything, so the application's own check of them cannot fail after the intent. Any other raise while applying is a crash, not a state: the engine dies, and the restart resumes the update; a raise the same input meets every time stops every restart, and the operator restores or removes the run root.
- An update whose stop cannot be carried out (a command that outlives its KILL) waits for it as a drain does, and a restart starts with the update still waiting; no verb cancels an update ([DL-427](../decision-log.md)).
- An exact retry of an admitted request is answered from its records, also while the engine drains; a refused one is admitted afresh.

## Owning modules

- `src/dsl41/reload_plan.py`, `src/dsl41/reload_diff.py`: `reread`.
- `src/dsl41/reload_update.py`: the chart, the admission and the application.
- `src/dsl41/runner_update.py`, `src/dsl41/runner.py`, `src/dsl41/runner_startup.py`: the engine's side and step 4.
- `src/dsl41/cli_reload.py`, `src/dsl41/runner_control.py`: the verbs.
- `src/dsl41/runner_history.py`: `dsl41 runs` across revs.

## Tests

- `test_two_running_jobs_are_stopped_and_eighteen_untouched_with_the_engine_running`
- `test_a_plan_job_the_oracle_starts_while_the_update_stops_waits_for_the_application`
- `test_a_client_that_gave_up_on_the_decision_leaves_the_loop_running`
- `test_a_retry_while_a_resumed_update_still_stops_waits_for_its_application`
- `test_a_stale_token_and_an_open_update_are_refused_and_a_retry_is_answered_once`
- `test_a_box_that_started_since_reread_refuses_the_plan`
- `test_a_changed_job_gets_a_rebuilt_tick_and_an_unchanged_one_keeps_its_own`
- `test_the_update_applies_only_once_each_stop_is_finished`
- `test_an_interruption_of_a_newly_marked_job_waits_for_the_applied_marked_set`
- `test_an_attempt_already_stopping_keeps_its_first_cause_through_intent_and_snapshot`
- `test_a_changed_jobs_term_run_time_keeps_counting_across_the_update`
- `test_a_retry_is_answered_from_its_admission_while_the_engine_drains`
- `test_runs_reads_each_run_under_the_configuration_it_ran_under`
- `test_a_box_run_keeps_the_rev_it_ran_under_at_an_equal_instant_and_after_a_prune`
- `test_an_attempt_ended_before_the_admission_still_cleaning_holds_the_update`
- `test_a_restart_starts_with_the_update_stopping_and_applies_it_as_a_loop_step`
- `test_a_run_keeps_the_rev_it_opened_under_when_its_replacement_runs_under_the_next`
- `test_a_box_run_keeps_its_rev_through_snapshot_prune_resume_update_snapshot_prune`
- `test_the_spec_drift_baseline_names_the_original_paths_of_every_part`
- `test_a_crash_between_intent_and_application_resumes_to_applied_before_any_dispatch`
- `test_a_restart_after_an_update_runs_the_applied_configuration_and_replays_across_it`
- `test_a_snapshot_taken_while_the_update_stops_carries_it`
- `test_reread_and_update_over_the_control_socket`
- `test_every_coverage_item_of_the_chart_is_hit`

## Open findings

The [risk map](../risk-map.md) has no row for this block yet.
