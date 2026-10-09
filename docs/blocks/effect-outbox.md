# Effect outbox

## Purpose

The [outbox](../glossary.md#outbox) holds each [effect](../glossary.md#effect) the engine intends for a run, a `SPAWN` or a `KILL`, and what came of it.
An effect is written in the same `decision` line as the decision that implied it, before anything attempts it.
Dispatch applies pending effects in admission order. Resume reconciles or re-drives what the previous engine left pending.

## Fate

The contract carries over: [concurrency-model §1](../concurrency-model.md#1-storage--frozen) already puts the outbox in the ledger, whatever provides it.
`runner_effects.py` carries over as it is: `Outbox` is in memory, and `read_outbox` rebuilds it from the WAL.
The resume half reads the local spool, `runs/<job>.<run_number>`, and the supervisor's run list. Those belong to the lifecycle tier, not to the ledger.
Storage capabilities needed:

- Atomic multi-record commit: `Journal.decision` writes the decision and its effects as one line.
- Epoch-conditional append: each `effect_result` goes through `Journal._write`, which re-proves the leader lock before it appends. `Engine._dispatch` runs the same check before it applies anything.

## Interface

- `Effect`: `effect_id`, `kind`, `job`, `run_number`, `executor_id`, `index`, `at`, `run_id`, `generation`.
- `EffectOutcome`: `state` is `applied`, `indeterminate` or `retired`. An effect with no outcome is `pending`, the name `Outbox.state_of` gives it.
- `EFFECT` is the effect's transition table ([state machines](../state-machines.md#effect)).
- `effect_id_for(index, kind, job, run_number)` derives the id `e<index>:<KIND>:<job>.<run_number>`.
- `Outbox`: `record`, `resolve`, `pending`, `pending_for`, `effects`, `state_of`, `result_for`.
- `plan_effects(...)` is the planner at step 7, pure except the `run_id` mint. `superseded_reason(effect, row, live_run)` is asked at dispatch.
- In `runner.py`: `Engine._plan_effects`, `Engine._dispatch`, `Engine._apply_effect`, `Engine._apply_spawn`, `Engine._apply_kill`, `Engine._resolve_effect`.
- In `runner_journal.py`: `Journal.decision`, `Journal.effect_result`, `decision_effects`, `read_outbox`.
- In `runner_startup.py`, at resume: `_reconcile_applied_spawns`, `_resume_untraced_starts`, `_redrive_recorded_kills`, `_redrive_orphans`, then one `Engine._dispatch`.
- A segment opened from a seal starts with the carried outbox (`carried_outbox`) before its own records are read.

## States

One effect's states are [effect](../state-machines.md#effect); the anchor holds the generated diagram and transition table.

## Invariants

- Intent is durable before the attempt: effects ride in the step-7 `decision` line ([concurrency-model §4](../concurrency-model.md#4-admission-and-application); [DL-96](../decision-log.md); [DL-118](../decision-log.md)).
- Every effect is bound to its executor and the host row's generation at birth. A SPAWN's `run_id` is minted in the decision transaction ([concurrency-model §5](../concurrency-model.md#5-effects); [DL-118](../decision-log.md)).
- One run has one `run_id` and one `run_id` names one run. A second, different record under one id is refused ([concurrency-model §5](../concurrency-model.md#5-effects); [DL-118](../decision-log.md)).
- `effect_id` is derived, not minted ([DL-96](../decision-log.md)).
- Effects apply in admission order ([concurrency-model §5](../concurrency-model.md#5-effects)).
- Dispatch asks about supersession before the routing hold, on every pass ([DL-234](../decision-log.md)). A SPAWN applies only while its row is `STARTING` or `RUNNING` at its run number ([DL-232](../decision-log.md)).
- Routing holds a SPAWN only; a KILL is never held ([DL-96](../decision-log.md)).
- One planning call plans at most one KILL per run ([DL-232](../decision-log.md)).
- The [ghost-run](../glossary.md#ghost-run) gate decides at planning, and a run counts as dispatched from its plan ([DL-234](../decision-log.md)).
- The outcome states are four, and `indeterminate` is not `pending` ([concurrency-model §5](../concurrency-model.md#5-effects); [DL-111](../decision-log.md)).
- An outcome is final. `Outbox.resolve` refuses a second one through the table's check, live and on replay ([concurrency-model §5](../concurrency-model.md#5-effects)).
- `Outbox.result_for` gives the known result or `outcome_unavailable`. Nothing in the local engine asks for it; its caller would be the relay, which is not built ([DL-97](../decision-log.md); [DL-284](../decision-log.md)). Locally an indeterminate effect refuses the seal ([concurrency-model §5](../concurrency-model.md#5-effects) and the [CM-06 row](../concurrency-model.md#9-the-proving-ground)).
- At resume a pending SPAWN with a spool trace is applied, and one with none is re-driven ([DL-102](../decision-log.md)). A recorded KILL is re-driven, and so is a live wrapper under a terminal row ([period-model §11](../period-model.md#11-resume-replay-and-recovery) step 7).

## Failure and recovery

- Crash between the decision and the attempt: the effect stays pending in the WAL. Resume re-drives or resolves it as the invariants above say ([concurrency-model §5](../concurrency-model.md#5-effects)).
- Crash between a launch and its `effect_result`: the spool is the record, and resume resolves the SPAWN as applied ([DL-96](../decision-log.md)).
- A spool file or a supervisor row naming another `run_id` than the effect bound: resume refuses before it changes anything (`_preflight_identities`; [DL-118](../decision-log.md)).
- A log that records one effect id twice with different content: `Outbox` raises `EngineError`, and resume stops. An outcome for an unknown effect, one naming another `run_id`, or a second outcome for one effect: replay stops with an `OutcomeReplayFault` naming the effect, and resume stops as for any replay fault ([period-model §11](../period-model.md#11-resume-replay-and-recovery)).
- An engine that died during the kill ladder leaves a live wrapper under a terminal row. Resume kills it whatever the KILL effect says ([period-model §11](../period-model.md#11-resume-replay-and-recovery) step 7).
- A leader that lost its lock launches nothing: `_dispatch` re-proves the fence first ([concurrency-model §1](../concurrency-model.md#1-storage--frozen)).
- During a seal, an `outbox.record` that raises, or a failed `effect_result` append, [fail-stops](../glossary.md#fail-stop) the engine instead of refusing the seal. Resume rebuilds the outbox from the WAL's decision records ([period-model §7](../period-model.md#7-the-seal-operation); [DL-274](../decision-log.md)).
- An attempt whose decision never reached the WAL is applied through the gate at resume, and that application records no effect, so it launches nothing. Its row is left to the resume ladder and the untraced-start sweep ([concurrency-model §4](../concurrency-model.md#4-admission-and-application); [DL-274](../decision-log.md)).

## Owning modules

- `src/dsl41/runner_effects.py`: the effect model, the outbox, the planner and the supersession rule.
- `src/dsl41/runner.py`: planning at step 7 and the dispatch gates.
- `src/dsl41/runner_startup.py`: reconciliation and re-drive at resume.
- `src/dsl41/runner_journal.py`: the `decision` and `effect_result` records and the rebuild.
- `src/dsl41/boundary.py`: `carried_outbox`, the outbox a segment opens with after a seal.

## Tests

- `test_an_effect_is_pending_until_something_says_otherwise`
- `test_cm06_an_effect_that_cannot_be_reported_on_answers_outcome_unavailable`
- `test_the_outbox_holds_one_run_one_identity_both_directions`
- `test_a_reused_effect_id_with_different_content_refuses`
- `test_an_outcome_must_belong_to_its_effect`
- `test_the_outbox_keeps_admission_order`
- `test_an_effect_id_is_derived_not_minted`
- `test_two_terminal_transitions_of_one_live_run_plan_one_kill`
- `test_pr36a_a_spawn_mints_its_run_id_in_the_planning_transaction`
- `test_a_start_records_its_intent_in_the_decision_that_planned_it`
- `test_cm09_an_applied_effect_is_never_applied_twice`
- `test_a_drain_holds_spawns_and_lets_kills_through`
- `test_a_held_spawn_whose_run_has_since_ended_is_retired_not_applied`
- `test_a_held_spawn_is_retired_at_the_edge_that_ends_its_run_not_at_activation`
- `test_a_starting_overwrite_while_a_spawn_is_held_plans_no_second_spawn`
- `test_a_recorded_kill_is_resolved_from_the_spool_three_ways`
- `test_cm09_a_spawn_that_never_reached_the_host_is_redriven_at_resume`
- `test_a_run_the_host_admits_to_is_never_re_driven`
- `test_pr34_a_start_line_resolves_a_pending_spawn`
- `test_pr33_a_live_wrapper_under_a_terminal_row_is_re_driven`
- `test_the_fence_stops_the_spawn_and_not_only_the_record`
- `test_pr28b_an_exception_while_a_drained_attempt_applies_fail_stops`

## Open findings

The [risk map](../risk-map.md) row "Effect outbox" lists no open finding. The outbox refuses a second outcome for a resolved effect ([DL-295](../decision-log.md), [closed](../risk-map.md#closed-by-dl-289dl-306)).

## Gaps found

None.
