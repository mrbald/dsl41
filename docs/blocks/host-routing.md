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
Replay does not run the gate again. It applies the stored decision, and `host_move` without a gate gives the same transition.

## Interface

- Wire: the `host` verb with `activate`, `drain` and `evict`, and `--force` on `evict` ([control-protocol §3, `host`](../control-protocol.md#host-s5a-dl-94)). The read is `hosts [ids]` ([§4](../control-protocol.md#hosts-ids)).
- Verbs: `HOST_VERBS` are the operator's and reach the wire. `LEADER_VERBS`, `quarantine` and `reinstate`, are the leader's and enter through `Engine.inject_host` with no `expect`.
- `HostCommand` is the input. `Engine.submit_host` admits an operator's command and returns its decision.
- `host_move(row, cmd, gate)` returns the declared transition a verb takes, or the `Rejection` that stops it. `host_rejection_reason` is the gate half and `apply_host_command` the apply half. Both ask `host_move`.
- `routes_new_effects(row)` is the routing column as one predicate. Dispatch holds a SPAWN on a host it is false for ([effect outbox](effect-outbox.md)).
- `seed_local_executor` puts this engine's own executor in the table at genesis, without a log index.
- `kill_allowance` and `skew_allowance` derive the eviction bound's two allowances.
- Row: `HostRuntime` in `oracle_state.py`, with its state, `generation`, `deadman_s`, `last_contact` and `forced_by`.

## States

One host row's states are [host](../state-machines.md#host). The anchor holds the generated diagram and transition table.
A rejection takes no transition and moves nothing.

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
- A decided host command whose verb takes no declared transition stops the apply. Live, the gate makes this unreachable. On replay it stops resume with an error naming the input ([DL-295](../decision-log.md)).
- An operator verb that breaks the table is refused before it is logged, under the default `--on-transition-violation refuse`. Under `continue` it is admitted and the violation is traced. A leader verb that breaks it is always taken and traced ([DL-292](../decision-log.md)).
- A command naming a host that is not in the table is rejected.
- A drain and a quarantine survive the engine that set them: resume replays the admitted inputs ([period-model §11](../period-model.md#11-resume-replay-and-recovery)).

## Owning modules

- `src/dsl41/runner_hosts.py`: the table, the verbs, the gate, the bound and the routing predicate.
- `src/dsl41/oracle_state.py`: `HostRuntime` and the owner's host verbs.
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
- `test_a_replayed_host_decision_with_no_transition_stops_resume_naming_the_input`

## Open findings

See the row "Host routing" in [the risk map](../risk-map.md).
Its own finding is the one-host limit under [stated limits](../risk-map.md#stated-limits).

## Gaps found

None.
