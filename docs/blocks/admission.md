# Admission and idempotency

## Purpose

Every input takes one ordered path from arrival to a durable [decision](../glossary.md#decision).
That covers operator commands, calendar ticks, adapter completions, reconciliation injections and bare time observations.
The block tells a refusal from a rejection, answers an exact retry from the earlier decision, and leaves the records that make replay two-pass.

## Fate

The contract carries over: concurrency-model §1 (`concurrency-model.md`, retired in 2.0.0) names capabilities, not a file format.
Most of `runner_admission.py` carries over as it is: the envelope, the fingerprint, the frontiers and `apply_attempt` hold no storage.
`DecisionIndex` is the part that moves with the store. Concurrency-model §1 (`concurrency-model.md`, retired in 2.0.0) says decision lookup by `request_id` is the `DecisionIndex`, so a store that answers the lookup itself would replace or reduce it.
The writes and the rebuild in `runner_journal.py` would change too.
Storage capabilities needed:

- Decision lookup by `request_id`: step 2 calls `DecisionIndex.lookup`, an in-memory map that `read_decisions` rebuilds from the WAL.
- Atomic multi-record commit: `Journal.admit` writes the step-4 batch as one line, and `Journal.decision` writes the step-7 batch as one line.
- Fenced append: both writes go through `Journal._write`, which re-proves the run root's lock before it appends.

## Interface

- `parse_envelope(request, addressed=..., baseline_id=...) -> Envelope` is the framing half of steps 1 and 2 for every external transport. It raises `EnvelopeError` naming the field.
- `addressed_key(kind, payload)` names the one entity a verb addresses, as a `job:` or `global:` key.
- `fingerprint(...)` hashes the envelope a retry must repeat. `at` is not in it.
- `Attempt` is one admitted input; `ApplyResult` is its decision; `Frontiers` holds `committed_index`, `applied_index` and the stamp frontier.
- `DecisionIndex(machines)` holds one `ADMISSION` instance per request id in flight, keyed by the index and the id, on the executor it is given: the engine's, or its own when read offline. `note` starts it at step 4; `record` posts the decision at step 7 and forgets it.
- `DecisionIndex.lookup(request_id, fingerprint)` returns the prior decision, `None`, or raises `RequestCollision` carrying the earlier decision.
- `apply_attempt(oracle, attempt, decided=...) -> Applied` runs steps 5 to 7 for a live input and for a replayed one.
- `dispatch_plan(emitted, ...)` names the starts and kills an applied input implies; `spawn_superseded` says why a start must not launch now.
- `precondition_reason` checks [`expect`](../glossary.md#expect); `stale_reason` is the stale-completion gate.
- `Engine._admit_and_apply` in `runner.py` strings the steps together and then dispatches. `Engine.submit` returns an awaitable decision.
- WAL records: `input` or `advance` at step 4, `decision` at step 7, and a `dispatch` record before each start.
- On the wire, the four outcomes are those of [control-protocol §3](../control-protocol.md#3-mutating-verb-sendevent).
- A refusal raises `AdmissionRefused`, or `EnvelopeError` from `parse_envelope`; each carries a required [code](../glossary.md#code). A rejection's `ApplyResult` carries its code too. The codes are in `runner_codes.py`, and the table is [control-protocol §2](../control-protocol.md#2-transport-and-framing-frozen)'s.

## States

One request id's in-flight window is [admission](../state-machines.md#admission), a statechart on the engine's executor ([DL-334](../decision-log.md)); the anchor holds the generated diagram and transition table.
An instance starts in `admitted` when the attempt line is durable and ends in `applied` or `rejected`, both final, when the decision is.
An id before its attempt line has no instance: deduplication and every refusal before admission post nothing.

## Invariants

- The steps and their order are concurrency-model §4 (`concurrency-model.md`, retired in 2.0.0)'s. This card does not restate them.
- Every external mutation names the revision it read. There is no opt-out, and `parse_envelope` is the one enforcement point (concurrency-model §0 (`concurrency-model.md`, retired in 2.0.0); [DL-90](../decision-log.md)).
- A refusal leaves nothing in the log. A rejection is a decision at an index ([DL-90](../decision-log.md); [control-protocol §3](../control-protocol.md#3-mutating-verb-sendevent)).
- `expect` is in the fingerprint, so one verb at two revisions is two commands ([DL-90](../decision-log.md); [control-protocol §3](../control-protocol.md#3-mutating-verb-sendevent)).
- A collision refusal carries the decision the id already holds ([DL-217](../decision-log.md)).
- A decided id has no instance. The index answers its exact retry from the stored decision, and refuses its reuse carrying that decision, before any post ([DL-217](../decision-log.md); CM-05).
- Every `ok: false` answer carries a stable code. A rejected `decision` stores its code, so an exact retry answers the same code. `Journal.decision` refuses a rejection whose code is outside `STORED_CODES` ([DL-272](../decision-log.md); period-model §2.3 (`period-model.md`, retired in 2.0.0)).
- The step-4 batch is one line ([DL-111](../decision-log.md)), and so is the step-7 batch ([DL-118](../decision-log.md)).
- A timer that fires inside an input's own batch does not invalidate its precondition. One due strictly earlier fires first and does (concurrency-model §0 (`concurrency-model.md`, retired in 2.0.0); [DL-232](../decision-log.md)).
- The stale-completion gate judges only engine-made completions ([runner-design §4](../runner-design.md#4-engine-loop--single-writer); [DL-235](../decision-log.md)).
- A durable decision is never re-decided on replay; an admitted input with no decision is (concurrency-model §4 (`concurrency-model.md`, retired in 2.0.0), "Replay is two-pass").

## Failure and recovery

- Crash after step 4 and before step 7: replay applies the attempt through the gate (concurrency-model §4 (`concurrency-model.md`, retired in 2.0.0)). Nothing it implies was dispatched, so resume writes the decision and dispatches the starts replay derives ([DL-102](../decision-log.md); `_resume_untraced_starts`).
- An id admitted but undecided at lookup raises `EngineError`, and the engine stops (concurrency-model §4 (`concurrency-model.md`, retired in 2.0.0) step 2).
- A gap in the chart is a `StatechartError`. Met at an attempt or a decision in the loop or a seal's drain, or at the seal's readiness lookup, it stops the engine with exit 7 before any further write; in replay its report names the WAL record. So are three faults the index refuses before any post, each naming `admission` and the request id: a decision for an id with no attempt in flight, a second attempt for an id in flight, and an attempt line for an id that already holds a decision. Only an engine bug or a corrupt log produces them; in replay the report names the WAL record ([statechart-engine §5](../statechart-engine.md#5-the-step-and-the-worklist)).
- A stamp earlier than the frontier, or a decision out of index order, raises `EngineError` from `Frontiers`, and the engine stops. During a seal, a request whose stamp is behind the frontier is answered refused with `clock_regressed` instead. An engine-made input in that case still stops the engine, so resume observes it again (concurrency-model §4 (`concurrency-model.md`, retired in 2.0.0); [DL-274](../decision-log.md)).
- An exception after the index is taken and before its decision, dispatch records and answer are all done stops the engine, and so does a failed WAL append ([DL-274](../decision-log.md)).
- A replay whose revisions differ from the logged decision raises "replay diverged" from `apply_attempt`. A changed gate re-decides a crash-window attempt differently, which is why [DL-235](../decision-log.md) moved the state-machine version.
- An engine that lost its lock appends nothing: `Journal._write` refuses first ([DL-101](../decision-log.md)).
- A caller with no decision after 5 s is told the outcome is unknown, and retries under the same id ([control-protocol §3](../control-protocol.md#3-mutating-verb-sendevent)).

## Owning modules

- `src/dsl41/runner_admission.py`: the envelope, the fingerprint, the decision index, the frontiers, `apply_attempt` and the dispatch plan.
- `src/dsl41/runner_codes.py`: the control codes and the stored-code set.
- `src/dsl41/runner.py`: `Engine._admit_and_apply`, `Engine.submit`, `Engine._dispatch`, `Engine._refuse`.
- `src/dsl41/runner_journal.py`: `Journal.admit`, `Journal.decision`, `read_attempts`, `read_decisions`, `replay_inputs`.

## Tests

- `test_cm04_the_deadline_fires_before_the_gate_reads_the_status_it_gates_on`
- `test_cm05_an_exact_retry_takes_no_index_and_moves_no_time`
- `test_one_request_id_for_two_different_commands_is_refused_not_applied`
- `test_an_admitted_but_undecided_id_is_refused_rather_than_re_admitted`
- `test_results_out_of_order_are_refused`
- `test_admission_time_never_goes_backwards`
- `test_cm07_a_durably_rejected_attempt_is_not_applied_on_replay`
- `test_cm07_the_time_half_of_a_rejected_attempt_still_applies`
- `test_a_log_this_build_derives_differently_from_is_refused`
- `test_a_mutation_that_names_no_revision_is_refused`
- `test_there_is_no_any_escape`
- `test_cm06_a_command_composed_against_a_stale_revision_is_rejected`
- `test_a_timer_inside_this_inputs_own_batch_does_not_invalidate_it`
- `test_a_refusal_leaves_nothing_in_the_log_and_a_rejection_leaves_a_decision`
- `test_a_collision_carries_the_ids_earlier_decision_applied_or_rejected`
- `test_the_stale_gate_rejects_a_completion_for_a_row_an_operator_set_inactive`
- `test_a_rejection_is_written_with_its_code_and_one_without_is_refused`
- `test_a_rejected_retry_answers_the_stored_code_and_a_record_without_one_omits_it`
- `test_the_completeness_table_is_section_4`
- `test_a_decided_id_never_reaches_the_chart`
- `test_every_event_a_poster_sends_is_in_the_alphabet`
- `test_replay_keeps_one_instance_open_where_the_lines_interleave`
- `test_dsl41_run_exits_7_when_a_decision_meets_a_gap_before_it_is_recorded`
- `test_a_decision_with_no_open_instance_raises_and_writes_nothing`
- `test_a_second_attempt_for_an_id_in_flight_carries_the_instance_s_facts`
- `test_a_second_attempt_for_a_decided_id_is_refused_before_any_start`
- `test_dsl41_run_exits_7_when_resume_meets_a_second_attempt_for_a_decided_id`

## Open findings

The [risk map](../risk-map.md) row "Admission and idempotency" lists [a replayed oracle fault](../risk-map.md#a-replayed-oracle-fault-stops-every-resume) and [instant cascades that do not end](../risk-map.md#instant-cascades-that-do-not-end).

## Gaps found

None.
