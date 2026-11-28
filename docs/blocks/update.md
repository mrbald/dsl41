# Update: reread and update

## Purpose

`dsl41 reread` reads current launch-config sources against the engine catalog (DL-463), and the engine stages that read ([DL-569](../decision-log.md)).
`dsl41 update NAME` applies the staged read, and `dsl41 update --reread` prepares and applies current sources, to the running engine: it stops every process the engine owns at admission for a job the plan changes or removes ([DL-525](../decision-log.md)), swaps the catalog, the settings and the marked set, and keeps the engine running throughout ([DL-427](../decision-log.md)). It never restarts a box: an update that changes box membership while a touched tree is active is refused `tree_active` ([DL-559](../decision-log.md)).

## Fate

The update needs an atomic multi-record commit for its records and its configuration row, and decision lookup by `request_id` for an exact retry.
Its intent and its application are store records. With nothing to stop they commit in one transaction (DL-560); with a stop, a crash between them resumes it.

## Interface

- `dsl41 reread -S SOCK` reads and stages; `dsl41 update -S SOCK NAME [--wait]` applies the staged read and `dsl41 update -S SOCK --reread [--wait]` prepares and applies current inputs; a plain `dsl41 update` exits 2 and names both. Both default to DSL41_SOCKET. They use v5 OPS requests (DL-498, DL-567); the staged read's name is the first 12 hex digits of its plan token (DL-569).
- `reload_plan.reread` computes and stores the plan; `runner_update.EngineUpdate.admit` checks it against the engine's state now; `reload_update.admission_data` builds the admitted data; `reload_update.plan_application` and `apply_application` compute and apply the swap.
- `runner_update.EngineUpdate` binds the update chart's tier to the engine: the admission as one step of the single writer, the stops through the attempt tier, the dispatch filter, the application and its records.
- The store records `config intent` (the token, the stop set with the attempt each stop interrupts, the dispatch filter, `restart_boxes: []`, the request's id and fingerprint) and `config applied` (the new rev, its segment fields, the job sets and the members it woke, `woken`). A configuration row holds each rev's catalog, profile and marked set (DL-489).
- `status` shows `config_rev`, `update_window_s`, the staged read and whether the disk changed since it (`staged_read`, DL-569) and, while an update stops, the update and the jobs it waits for; the TUI shows the rev in its subtitle, and the update, the staged read and the drift in its banner (DL-570). The TUI's update pane (`u`) shows one `reread` answer and sends `update` with the read it shows (DL-571). Once applied, `spec` and `spec_drift` follow the applied plan.

## States

One update request's states are [update](../state-machines.md#update): IDLE, STOPPING, APPLIED and REFUSED, with the token and the stop set as the instance's data.
An admission with nothing to stop goes from IDLE to APPLIED at once (update.07): apply, journal the intent with the application, advance time, finish (DL-560).
STOPPING waits for the stop set. The look that finds none open goes to APPLIED (update.08): apply, journal the application, advance time, finish (DL-562).
The update step awaits the intents the synchronous tier hands back (DL-488).
The anchor holds the generated diagram and transition table.
The [chart lifecycles](../chart-lifecycles.md) view lists each target state with the ways into it.

## Invariants

- One update is in flight at a time; a second is refused with `update_in_progress` ([DL-427](../decision-log.md)).
- Admission rechecks revision and current stops. An update never restarts a box (DL-559). One that changes box membership (a job added, removed, moved to another box or retyped that is a BOX or has a box in either catalog) is refused `tree_active` when it would stop a job or a job of a touched tree is active: its row is QUE_WAIT, STARTING or RUNNING, or the engine owns a process of it. The touched trees are each such job's top-level ancestor in each catalog, with every descendant in either catalog. The refusal names the stops and the active jobs of those trees, writes nothing, and goes through the chart's REFUSED row. An admitted one records `restart_boxes: []` and applies in its admission step, its intent, configuration row and application in one transaction (DL-560).
- The engine loads the main-config candidate from the version 2 plan's captured inputs and runs it under the captured settings (DL-486).
- A kept job whose definition differs only in its `status:` line is unchanged and seed-only: the edit adds no reason to stop, filter or mark it, and removes none ([DL-533](../decision-log.md)).
- While the update stops, no job of its recorded dispatch filter is dispatched. The filter is the plan's added, changed and removed jobs, and a restart holds exactly the filter the intent recorded ([DL-527](../decision-log.md), DL-562). A start the oracle decides for one waits as `dispatch_pending`. After the application it goes if its run is still wanted, or it is discharged with no launch when its run ended.
- An attempt the update stops keeps its outcome back until the application, so its follow-up plan is chosen under the applied catalog and marked set, once, and the `config applied` record names it: a removed job gets none, a marked one is held, any other reruns the same run under its new definition ([DL-424](../decision-log.md)). An interrupted attempt that at the application is not of a live row's current run gets none ([DL-525](../decision-log.md)).
- A stop is finished when its job's next run could go: its ladder done and a reading finding nothing of it left, not at its record ([DL-431](../decision-log.md)). The update applies only once every stop is finished, an attempt that ended before the admission while its cleanup still runs included.
- An interrupted attempt of a job whose `non-idempotent.toml` mark the plan changes keeps its outcome back too, so its plan follows the applied mark; the update neither stops nor waits for it.
- The intent records each attempt with the cause of its first stop: `update_stop`, or the cause of a stop it already takes, which a restart keeps.
- The application is one `config applied` record and one configuration row; the oracle swaps its catalog between inputs, the scheduler rebuilds only the changed and added plans and drops the removed ones, and one time advance follows at once so the queue scan the swap owes runs first. The oracle's own timers (run windows, `term_run_time`) are untouched: a changed job's deadline keeps counting across the update.
- A changed job in a box with no date conditions, whose lowered `condition:` the update changed, is owed one plain attempt: the one a referenced job's move gives it. The time advance makes it after the queue's scan, in catalog order, if the member still waits in its box's run: INACTIVE, not run in this run of the box, and not resolved by an operator's verdict or a window skip. The chart's gates decide the rest. Its cause names the configuration rev. The application selects the names on both apply rows and records them as `woken`; a replay reads them from the record's segment alone, and a snapshot taken before the next input carries them ([DL-564](../decision-log.md)). A changed job outside every box, a changed dated member, a member whose condition did not change and an unchanged job whose condition names a changed one are read at the next event that concerns them.
- A run's configuration rev is recorded on its run row when it opens: the rev in force where it opened in record order, which a snapshot's retained run history keeps as a required key (state version 26). `dsl41 runs` reads each run under that rev and nothing else: its catalog hash, its job fingerprint, its box and its exit-code verdict. A `dispatch` record's `rev` is that attempt's own (the definition it ran), never read as the run's. The `CONFIG` trace marker places each application between two inputs.
- `spec` and the `spec_drift` baseline follow the applied plan, each file at its applied capture path (site layers included), never the plan directory's copy.
- A snapshot carries the tombstones, the owed scan, the owed attempts, the ghost resources and the update in STOPPING; replay swaps the configuration where each `segment` stands.
- A restart runs the store's last applied configuration; the estate it is launched with must equal it, or the open update's candidate, job by job.

## Failure and recovery

- An update with nothing to stop leaves no intent alone: after a crash the store holds the old revision and no trace of the request, or the whole application, and a retry under its id is admitted afresh or answered from the store (DL-560).
- A crash between `config intent` and `config applied` of an update that stops jobs is resumed by the restart's fold, which restores the update in STOPPING with its dispatch filter holding; nothing waits for it. The catch-up observes its attempts like any open one, and what is left of each gets the live cleanup (the ladder, the reading after). The update applies as a step of the loop once its stops are finished, wherever the last one finishes: in the catch-up, at the first look the catch-up queues when every stop was finished before the crash, or later. The recovered outcomes of jobs outside its stop set are fed under the old catalog before it. No job of the plan is dispatched and no kept outcome is committed before the application (DL-433).
- An intent's `restart_boxes` is `[]`, and an absent key reads the same. One that names box roots is refused at start by name as retired, and a malformed one as malformed, before any write or signal; neither is completed (DL-562, DL-489).
- A plan an older `reread` wrote (no estate directory) is refused `stale_plan` before anything is written.
- A plan directory removed while an update stops refuses the restart by name.
- The admission checks the plan's revision and the catalog hash preparation saw before it writes anything, so the application's own check of them cannot fail after the intent. Any other raise while applying is a crash, not a state: the engine dies, and replay installs what the store holds (DL-560). The restart resumes an update that stops jobs; a raise such a resumed intent meets every time stops every restart, and the operator restores or removes the run root. An update with nothing to stop leaves nothing to resume: before its commit no request is stored, after it the whole application is.
- An update whose stop cannot be carried out (a command that outlives its KILL) waits for it as a drain does, and a restart starts with the update still waiting; no verb cancels an update ([DL-427](../decision-log.md)).
- An exact retry of an admitted request is answered from its records, also while the engine drains; a refused one is admitted afresh.
- An update that stops a file watch cancels it and, after the application, takes it up or closes it with no plan (DL-526). Taking up a watch the update changed replaces it, as a command it stops is rerun: the attempt is closed once with plan `rerun`, and a new attempt of the run watches under the applied definition from an empty log; at the attempt limit it is held, and `config applied.stopped` says `hold`. A restart that finds the watch's attempt open after the application replaces it the same way, since a committed `config applied` after its `dispatch` names its job changed (DL-566). An application that finishes while the engine drains launches no watch. One it takes up keeps its open attempt and its log, and the next restart takes it up. One it closes, or whose run its time advance ends, is closed once before the shutdown record. A watch left open is recovered as one the drain cancelled is: a watch's recovery does not read the open attempt's `stop` cause, and a deadline that ends its run first closes it with no launch ([DL-565](../decision-log.md)).

## Owning modules

- `src/dsl41/reload_plan.py`, `src/dsl41/reload_diff.py`: `reread`.
- `src/dsl41/reload_update.py`: the chart, the admission and the application.
- `src/dsl41/runner_update.py`, `src/dsl41/runner.py`, `src/dsl41/runner_startup.py`: the engine's side and the restart's resume of an update.
- `src/dsl41/cli_reload.py`, `src/dsl41/runner_control.py`: the verbs.
- `src/dsl41/runner_history.py`: `dsl41 runs` across revs.

## Tests

- `test_two_running_jobs_are_stopped_and_eighteen_untouched_with_the_engine_running`
- `test_a_plan_job_the_oracle_starts_while_the_update_stops_waits_for_the_application`
- `test_a_client_that_gave_up_on_the_decision_leaves_the_loop_running`
- `test_an_exact_update_retry_waits_while_the_restarted_engine_still_stops`
- `test_while_an_update_stops_a_retry_waits_for_it_and_another_update_is_refused`
- `test_a_plan_whose_catalog_hash_is_not_the_engines_is_refused`
- `test_a_stale_token_and_an_open_update_are_refused_and_a_retry_is_answered_once`
- `test_a_box_that_started_since_preparation_refuses_the_membership_change`
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
- `test_the_look_that_finds_no_stop_open_applies_and_hands_back_the_rest`
- `test_dl562_a_stored_intent_with_box_roots_is_refused_at_start`
- `test_a_status_only_update_applies_live_with_no_stop_and_restart_keeps_the_rows`
- `test_feat31_a_member_added_removed_or_moved_under_a_running_box_is_refused`
- `test_dl559_a_membership_change_in_quiet_trees_applies_in_its_admission_step`
- `test_dl559_a_refusal_writes_nothing_and_its_retry_is_decided_on_the_state_then`
- `test_dl560_a_crash_at_any_write_of_an_update_with_nothing_to_stop_leaves_old_or_whole`
- `test_bug19_a_box_an_update_gives_a_member_never_runs_a_member_set_it_was_not_given`
- `test_dl564_bug116_a_member_whose_condition_is_removed_starts_and_its_box_completes`
- `test_dl564_the_rule_selects_only_boxed_undated_jobs_whose_condition_changed`
- `test_dl564_a_member_an_operator_resolved_gets_no_attempt`
- `test_dl564_the_queue_scan_goes_first_so_an_older_waiter_takes_the_freed_unit`
- `test_dl564_a_snapshot_between_the_swap_and_the_next_input_carries_the_attempt_once`
- `test_dl564_bug116_the_engine_starts_the_member_in_the_input_after_the_application`
- `test_dl564_a_config_applied_record_without_the_field_replays_with_no_attempt`
- `test_dl564_an_update_that_stops_a_member_records_and_makes_the_attempt_at_its_look`
- `test_dl564_a_death_after_the_application_makes_the_attempt_at_the_first_input_after_restart`

The staged read is one object in engine memory, owned by the update's engine side (`runner_update.EngineUpdate.staged`), and the CLI/TUI status annotation is its forecast. A reread of the disk stages its read when the candidate passes the preparation checks, is not wholly the applied configuration, the revision it read is still applied, no update stops and the engine is not draining; otherwise it stages nothing. A reread of the disk and an `update --reread` replace it; an applied update and a restart drop it; a refused update keeps it. `update NAME` applies it from its captured bytes, checked against the engine's state then; another name is refused `stale_plan`. One reread or update prepares at a time. Retention keeps the staged plan (DL-569).
Forecast labels follow the first-match rule in the [control protocol](../control-protocol.md#2-transport-and-framing-frozen) (DL-559): `hold`, `remove` or `rerun`, each for a stop; a kept file watch's label comes from the interruption rule the application replaces it with, read for its current run's open attempt, so one at the limit forecasts `hold` (DL-566).
The forecast reads statuses, not flags. `preview.update_refusal` forecasts the `tree_active` refusal from the same function and inputs as the admission, the jobs the engine owns a process of included, and is `null` when the update's preparation would refuse first. Both read their stops from one rule, `reload_plan.stop_runs`. `dsl41 reread` prints the refusal as one line, offers no apply command, and exits 1. A job the plan retypes prints as replaced by a new job of its name; a removed one keeps "no replacement".

## Open findings

The [risk map](../risk-map.md) has no row for this block yet.
