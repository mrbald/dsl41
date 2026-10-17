# Host routing

## Purpose

This block keeps one routing row per execution host and decides whether new work may go to it.
An operator drains a host before maintenance and activates it after. The leader quarantines a host it cannot reach and reinstates it when it answers.
Eviction is the one move that lets work bound to a host run elsewhere, so it is gated on proof or on an attributed force.

## Fate

The code and its contract carry over unchanged if the storage under the runner changes.
It needs no storage capability of its own.
Reason: the row is a `HostRuntime` in the in-memory `RuntimeState`, under the state owner ([concurrency-model §3](../concurrency-model.md#3-state-ownership-and-state_rev); [DL-93](../decision-log.md)).
A host command is an admitted input, so its durability is the [admission](admission.md) block's: replay rebuilds the row, and a drain survives a failover.
Replay does not run the gate again. It applies the stored decision: the chart is dispatched for an applied command, and the rows are enough to rebuild its instance.

## Interface

- Wire: the `host` verb with `activate`, `drain` and `evict`, and `--force` on `evict` ([control-protocol §3, `host`](../control-protocol.md#host-s5a-dl-94)). The read is `hosts [ids]` ([§4](../control-protocol.md#hosts-ids)).
- Verbs: `HOST_VERBS` are the operator's and reach the wire. `LEADER_VERBS`, `quarantine` and `reinstate`, are the leader's and enter through `Engine.inject_host` with no `expect`.
- `HostCommand` is the input. `Engine.submit_host` admits an operator's command and returns its decision.
- `HOST` is a statechart ([statechart-engine](../statechart-engine.md), DL-333), declared in `oracle_state.py`. The store owns its executor, `RuntimeState.machines`, with one instance per host row under the key `host:<id>`. The instance's configuration is the host's state, and `HostRuntime.state` is its projection. Each leaf's entry action reports the state write as an intent; the moving rows' actions report the rest: host.05 remembers the interrupted state, and host.10 and host.11 evict, unforced or forced by the actor the event carries. Every leaf but `quarantined` clears the memory as it writes the state (DL-376). A new row starts its instance in `active`. A fork copies the executor and binds each host instance's data to the fork's rows (`Executor.fork`, DL-339); an install rebuilds it from the rows (`rebuild_machines`). A state write that no microstep made raises a `StatechartError`; a test's witness writes through `witness_host`, which rebuilds the executor.
- `host_rejection_reason` is the gate. It reads the chart through `host_move`, which is `Executor.resolve` on the store's instance, so nothing moves. A refusal row's code is the rejection. On the gated eviction (host.10) and the forced one (host.11) the gate adds the checks that read the input's own facts: the bound, and the attribution.
- `apply_host_command` dispatches an applied command to the store's instance and carries out the outcome's intents in order, through the store's verbs. An internal row reports nothing and writes nothing.
- `routes_new_effects(row)` is the routing column as one predicate. Dispatch holds a SPAWN on a host it is false for ([effect outbox](effect-outbox.md)).
- `seed_local_executor` puts this engine's own executor in the table at genesis, without a log index.
- `kill_allowance` and `skew_allowance` derive the eviction bound's two allowances.
- Row: `HostRuntime` in `oracle_state.py`, with its state, `generation`, `deadman_s`, `last_contact` and `forced_by`.

## States

One host row's states are [host](../state-machines.md#host). The anchor holds the generated diagram and transition table.
A rejection moves nothing: the gate reads the chart with `Executor.resolve`, and a refusal row is the cell with no move.

## Invariants

- The four states and what each routes are [concurrency-model §8](../concurrency-model.md#8-host-lifecycle-active-passive-quarantined-evicted)'s table. This card does not restate it.
- The oracle never reads a host row, and `HostCommand` is not an `EventKind` ([DL-93](../decision-log.md)).
- Only an operator sends `activate`, `drain` and `evict`. Only the leader sends `quarantine` and `reinstate` ([concurrency-model §8](../concurrency-model.md#8-host-lifecycle-active-passive-quarantined-evicted), the "set by" column).
- Quarantine follows five failed lease renewals in a row, and reinstatement puts back the state quarantine interrupted ([DL-97](../decision-log.md); [DL-291](../decision-log.md)).
- Eviction without force needs the three preconditions of §8. Its kill allowance is derived from the period's command grace ([DL-151](../decision-log.md)).
- A forced eviction names who asked, and an unattributed force is refused ([DL-151](../decision-log.md)).
- Eviction bumps the generation. An evicted host returns only by re-registering at that generation after it self-fences, which is the relay's act and is not built ([concurrency-model §8](../concurrency-model.md#8-host-lifecycle-active-passive-quarantined-evicted); [DL-97](../decision-log.md)).
- A host command moves its row's revision once ([concurrency-model §3](../concurrency-model.md#3-state-ownership-and-state_rev)).

## Failure and recovery

- A rejected host command is a decision with a stored code, and it replays as the rejection it was ([concurrency-model §4](../concurrency-model.md#4-admission-and-application); [DL-272](../decision-log.md)).
- A decided host command that the chart refuses, or one for a host with no instance, is a `StatechartError`. Live, the gate makes this unreachable. On replay the error names the WAL input, and `dsl41 run` exits 7 ([DL-295](../decision-log.md), DL-333; it was a replay fault, exit 2).
- A gap in the chart is a `StatechartError`: met in the loop, in a control input's dry apply or in replay, it stops the engine with exit 7 and nothing is logged for the input ([statechart-engine §5](../statechart-engine.md#5-the-step-and-the-worklist)).
- A command naming a host that is not in the table is rejected.
- A drain and a quarantine survive the engine that set them: resume replays the admitted inputs ([period-model §11](../period-model.md#11-resume-replay-and-recovery)).

## Owning modules

- `src/dsl41/runner_hosts.py`: the verbs, the gate, the apply, the bound and the routing predicate.
- `src/dsl41/oracle_state.py`: `HostRuntime`, the `HOST` chart, the owner's executor and its host verbs.
- `src/dsl41/runner.py`: `Engine.submit_host`, `Engine.inject_host` and the quarantine on failed renewals.

## Tests

- `test_cm02_a_host_command_moves_its_row_exactly_one_revision`
- `test_a_re_registration_does_not_undo_a_drain`
- `test_cm11_a_host_with_no_deadman_can_never_be_evicted`
- `test_cm11_eviction_is_refused_before_the_bound_and_permitted_after`
- `test_cm11_an_unattributed_force_is_refused`
- `test_cm11_the_kill_half_of_the_bound_follows_the_periods_grace`
- `test_cm11_eviction_needs_the_leaders_own_record_of_unreachability`
- `test_an_evicted_host_is_not_evicted_again_and_not_activated_back`
- `test_a_quarantined_host_refuses_an_operator_state_change`
- `test_cm13_a_drain_routes_nothing_new_and_finishes_what_is_running`
- `test_activating_re_dispatches_what_the_drain_held`
- `test_a_drain_survives_a_resume`
- `test_a_rejected_host_command_replays_as_the_rejection_it_was`
- `test_quarantine_is_the_leaders_door_not_the_operators`
- `test_clearing_quarantine_puts_back_what_it_interrupted`
- `test_cm12_reaching_an_evicted_host_again_does_not_un_evict_it`
- `test_the_oracle_never_names_a_host_row`
- `test_each_routing_verb_takes_its_declared_transition`
- `test_a_rejected_verb_takes_no_transition_and_moves_nothing`
- `test_a_decided_host_command_with_no_transition_stops_the_apply`
- `test_a_replayed_host_decision_the_chart_refuses_stops_resume_naming_the_input`
- `test_the_completeness_table_is_section_8`
- `test_reinstate_at_a_routed_host_is_one_internal_row_and_its_old_ids_stay_retired`
- `test_without_host_21_a_forced_eviction_of_an_evicted_host_is_a_gap`
- `test_the_gate_s_read_is_the_one_it_was`
- `test_the_host_gate_reads_the_store_s_instance_and_moves_nothing`
- `test_every_event_a_poster_sends_is_in_the_alphabet`
- `test_a_fork_rebuilds_its_own_executor_from_its_rows`
- `test_a_state_write_no_microstep_made_raises_and_writes_nothing`
- `test_a_second_quarantine_keeps_what_the_first_interrupted`
- `test_each_cell_s_outcome_carries_its_row_s_intents_then_its_entry_s`
- `test_the_initial_entry_reports_nothing`
- `test_the_gate_s_read_reports_no_intent_and_applies_nothing`
- `test_an_eviction_bumps_the_generation_once_and_a_forced_one_records_its_actor`
- `test_a_refusal_code_the_gate_has_no_sentence_for_raises`
- `test_a_dry_host_command_leaves_the_rows_revisions_and_instance_alone`
- `test_a_carried_drain_is_the_configuration_the_next_period_opens_in`
- `test_every_host_input_moves_the_revisions_the_table_implementation_moved`
- `test_a_leader_s_quarantine_that_meets_a_gap_stops_the_loop_and_logs_nothing`
- `test_an_operator_s_drain_whose_dry_apply_meets_a_gap_stops_the_loop_unrefused`
- `test_replay_names_the_host_input_a_gap_meets`
- `test_dsl41_run_exits_7_when_resume_replays_a_host_decision_the_chart_refuses`

## Open findings

See the row "Host routing" in [the risk map](../risk-map.md).
Its own finding is the one-host limit under [stated limits](../risk-map.md#stated-limits).

## Gaps found

None.
