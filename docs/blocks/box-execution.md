# Box execution

## Purpose

This block runs a BOX job and its members.
It starts members when the box runs, decides when the box completes and with which status, and applies the operator events that move a whole tree.
It lives inside the oracle; the engine dispatches nothing for a box ([runner-design §4](../runner-design.md#4-engine-loop--single-writer)).

## Fate

The code and its contract carry over unchanged if the storage under the runner changes.
It needs none of the storage capabilities.
Reason: a box's run state is three fields on its own `JobRuntime` row, `ran_members`, `window_skipped_members` and `iced_out_members`, held in the same in-memory `RuntimeState` as every other row.
A live box has no effect and no execution entry ([period-model §3.5](../period-model.md#35-executions--a-discriminated-lifecycle-not-one-row)).

## Interface

- Inputs: the same `Oracle.feed` inputs as any job, aimed at a box or a member: STARTJOB, FORCE_STARTJOB, KILLJOB, STATUS, ON_ICE, OFF_ICE, ON_NOEXEC, OFF_NOEXEC.
- Box row: `JobRuntime.ran_members` holds the members started in this run; `JobRuntime.window_skipped_members` holds the members resolved in it; `JobRuntime.iced_out_members` holds the members taken off ice in it before they ran, which sit the run out. `RuntimeState.start_run`, `record_resolution`, `void_resolution` and `record_iced_out` write them. A box's own start resets all three; a member's start drops it from the second and third. The fold waits for a marked member while a forced start of it is queued or live; a forced attempt that leaves the queue unstarted keeps the mark.
- Oracle steps: `_reset_box_cycle`, `_on_box_started`, `_decide_windows_at_box_start`, `_on_member_transition`, `_box_terminator_fires`, `_resolves`, `_completion_door`, `_complete_starts_with_no_member_in_the_run`, `_out_of_the_run`, `_ice_resolves_member`, `_holders_may_leave_the_run`, `_off_ice_in_running_box`, `_on_descendant_transition`, `_apply_box_overrides`, `_all_members_done`, `_fold_box_default`, `_idle_box_recompute`, `_inject_inactive`, `_noexec_box`.
- Outputs: box STATUS events and trace lines. Member starts and kills reach the engine as the members' own events.

## States

A box has no machine of its own.
Its moves are the [job_status](../state-machines.md#job_status) transitions whose trigger or guard concerns a box or its members.
That anchor holds the generated diagram and transition table.
The idle re-derivation rows follow the vendor table in [SEM-15](../autosys-semantics.md#sem-15--member-status-changes-can-ripple-upward-vc).

## Invariants

- A box start resets every contained job that is not STARTING, RUNNING or QUE_WAIT, and nothing inside a live subbox, before its own STARTING transition ([SEM-10](../autosys-semantics.md#sem-10--box-membership-and-start-rule-v), [DL-242](../decision-log.md)).
- A member runs at most once per box run unless forced ([SEM-10](../autosys-semantics.md#sem-10--box-membership-and-start-rule-v), [SEM-23](../autosys-semantics.md#sem-23--force_startjob-vs-startjob-c)).
- The default fold waits for every member; a member that never starts hangs the box ([SEM-11](../autosys-semantics.md#sem-11--box-runningcompletion-v), [DL-13](../decision-log.md)).
- Under `box-start-all-members-out=complete`, the default, a box start whose pass leaves no direct member in the run (every direct member out on ice, or no members) runs the completion check once after the start's window decisions, so such a box completes at its start; `wait` keeps it RUNNING ([SEM-11](../autosys-semantics.md#sem-11--box-runningcompletion-v), Q15, [DL-304](../decision-log.md)).
- An operator's INACTIVE and a run_window skip resolve a member and run the full completion door ([DL-154](../decision-log.md), [DL-242](../decision-log.md)).
- An iced member is out of its box's run only when neither it nor any job inside it is live or queued; until then the box waits for it ([DL-304](../decision-log.md)).
- The moment a member that has not run in a RUNNING box is out on ice is a completion moment for that box's run in progress and its RUNNING ancestors, as a resolved member is. A member that ran keeps its vote, a second ice or an ice on a resolved member does nothing, and an idle box re-derives nothing ([SEM-20](../autosys-semantics.md#sem-20--on_ice-v), [DL-285](../decision-log.md)).
- An OFF_ICE on a member that was iced and has not run in a RUNNING box keeps it out of that run: the fold skips it, a plain start of it is refused, and FORCE_STARTJOB still starts it. The `off-ice-in-running-box` switch selects this (`next-run`, the default) or the member's return to the run (`same-run`) ([SEM-20](../autosys-semantics.md#sem-20--on_ice-v), [runner-design §8a](../runner-design.md#8a-semantic-switches)).
- A box start decides each waiting run_window member at once ([SEM-33](../autosys-semantics.md#sem-33--run_window-is-a-gate-not-a-trigger-v), [DL-246](../decision-log.md)).
- Override gating, with "inside" read transitively ([SEM-12](../autosys-semantics.md#sem-12--box_success--box_failure-override--with-evaluation-gating-v), [DL-12](../decision-log.md)).
- TERMINATED is sticky ([SEM-13](../autosys-semantics.md#sem-13--box-terminated-is-sticky-v)). Terminators cascade both ways; a box_terminator member ending TERMINATED counts under the default `box-terminator-on-terminated=true` ([SEM-14](../autosys-semantics.md#sem-14--box_terminator--job_terminator-vc)).
- An idle box re-derives with INACTIVE members ignored ([SEM-15](../autosys-semantics.md#sem-15--member-status-changes-can-ripple-upward-vc), [DL-242](../decision-log.md)).
- CHANGE_STATUS INACTIVE on a box cascades as one batch ([SEM-18](../autosys-semantics.md#sem-18--change_status-inactive-on-a-box-cascades-v), [DL-242](../decision-log.md)).
- ON_NOEXEC on a box: the dry run and the cascade ([SEM-22](../autosys-semantics.md#sem-22--on_noexec-v), [DL-254](../decision-log.md)).
- Unconsumed member arms die with the box run ([DL-54](../decision-log.md)).
- A start of a job that already has two starts in progress in one nested cascade is refused as a re-trigger loop; starts that follow one another are never refused ([DL-304](../decision-log.md)).

## Failure and recovery

- A hung box is modeled behavior, not a fault. It stays RUNNING until an operator resolves the member, forces it or kills the box ([SEM-11](../autosys-semantics.md#sem-11--box-runningcompletion-v), [SEM-12](../autosys-semantics.md#sem-12--box_success--box_failure-override--with-evaluation-gating-v)).
- A queued member keeps the box RUNNING until it is admitted. A member queued in a box that stopped running is cancelled later ([DL-50](../decision-log.md), [DL-247](../decision-log.md)).
- A live member cascaded to INACTIVE gets no kill, and its later exit is rejected ([DL-235](../decision-log.md), [DL-242](../decision-log.md)).
- Box state replays with the rest of the oracle. A seal carries a live box on its row ([period-model §3.5](../period-model.md#35-executions--a-discriminated-lifecycle-not-one-row)).

## Owning modules

- `src/dsl41/oracle.py`: box start, folds, overrides, cascades.
- `src/dsl41/oracle_state.py`: the box row fields and their verbs.

## Tests

- `test_sem10_second_box_run_starts_only_the_head_of_a_plain_chain`
- `test_sem10_box_start_reset_leaves_a_live_member_running`
- `test_sem11_default_fold_all_success`
- `test_sem11_member_set_inactive_completes_a_running_box`
- `test_sem11_waiting_member_still_hangs_the_box`
- `test_sem20_ice_on_the_last_waiting_member_completes_a_running_box`
- `test_sem20_off_ice_in_a_running_box_completes_the_box_without_the_member`
- `test_sem14_a_terminated_box_terminator_member_terminates_its_box`
- `test_sem12b_external_box_success_hung_running_then_fires_when_member_completes_after`
- `test_sem12c_box_success_over_a_grandchild_fires_transitively`
- `test_sem13_terminated_box_is_sticky_then_restarts_fresh`
- `test_sem14_terminator_cascade_both_directions`
- `test_sem15_single_member_table_follows_the_vendor_rule`
- `test_sem18_cascade_writes_every_row_before_it_wakes_anything`
- `test_sem22_noexec_box_goes_running_and_every_member_bypasses`
- `test_sem33_vendor_box1_started_at_0405_skips_joba_and_completes`
- `test_sem33_vendor_box1_started_at_1605_defers_joba_to_0200_next_day`
- `test_sem32_member_arm_dies_with_its_box_run`
- `test_dl50_queued_box_member_keeps_the_box_running_until_admitted`

## Open findings

See the row "Box execution" in [the risk map](../risk-map.md).
Every machine shares the [no transition inventory](../risk-map.md#no-transition-inventory) finding.

## Gaps found

None.
