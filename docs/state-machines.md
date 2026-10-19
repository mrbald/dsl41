# State machines

This file is generated from `dsl41.machines`; regenerate it with `uv run python scripts/render_state_machines.py`.

A state marked final declares no row. An instance whose active leaves are all final
ignores every event that no walk finds a row or a deferral for
([statechart-engine §10](statechart-engine.md)).

## job

Clock: logical.

```mermaid
stateDiagram-v2
    state "job" as s19
    state s19 {
        [*] --> s20
        state "lifecycle" as s20
        state s20 {
            [*] --> s3
            state "IDLE" as s3
            state s3 {
                [*] --> s4
                state "INACTIVE" as s4
                state "TERMINAL" as s10
                state s10 {
                    [*] --> s9
                    state "SUCCESS" as s9
                    state "FAILED" as s1
                    state s1 {
                        [*] --> s2
                        state "FAILURE" as s2
                        state "TERMINATED" as s11
                    }
                }
            }
            state "QUE_WAIT" as s6
            state "LIVE" as s5
            state s5 {
                [*] --> s8
                state "STARTING" as s8
                state "RUNNING" as s7
                state s7 {
                    [*] --> s26
                    state "settled" as s26
                    state "overriding" as s24
                    state "folding" as s13
                }
            }
            state "ADMITTED" as s0
        }
        --
        [*] --> s12
        state "flags" as s12
        state s12 {
            [*] --> s17
            state "ice_off" as s17
            state "ice_on" as s18
            --
            [*] --> s15
            state "hold_off" as s15
            state "hold_on" as s16
            --
            [*] --> s21
            state "noexec_off" as s21
            state "noexec_on" as s22
        }
        --
        [*] --> s23
        state "none" as s23
        state "reserved" as s25
        state "held" as s14
    }
    state c0 <<choice>>
    state c1 <<choice>>
    s3 --> s9 : job.101 start [start_bypasses] / extent_bypass
    s3 --> s6 : job.103 start [start_queues] / extent_queue
    s8 --> s7 : job.105 started [started_job] / moved
    s10 --> s4 : job.106 start [start_skips] / moved
    s10 --> s4 : job.107 start [start_skips_member] / moved
    s6 --> s0 : job.108 readmit [readmit_admits] / admit
    s6 --> s4 : job.109 readmit [readmit_cancels] / cancelled
    s6 --> s4 : job.112 ice moved / moved
    s6 --> s4 : job.113 noexec moved [noexec_settles_job] / injected
    s5 --> s11 : job.114 KILLJOB / terminated
    s7 --> s11 : job.115 TIMER term_run_time [current_run] / terminated
    s5 --> s11 : job.116 box ends FAILURE or TERMINATED [is_terminator] / terminated
    s20 --> s9 : job.117 STATUS [exit_success] / moved
    s20 --> s2 : job.118 STATUS [exit_failure] / moved
    s20 --> c0 : job.119 STATUS [injects]
    s20 --> s4 : job.120 STATUS [injects_inactive_job] / injected
    s1 --> s4 : job.121 noexec moved [noexec_settles_job] / injected
    s10 --> s4 : job.122 box start / batched
    s20 --> s4 : job.445 box set INACTIVE / batched
    s8 --> s7 : job.125 started [started_box] / runs_box
    s7 --> s11 : job.130 member ends FAILURE or TERMINATED [terminator_fires] / terminated
    s20 --> s9 : job.446 box recompute [overrides_success] / moved
    s20 --> s2 : job.447 box recompute [overrides_failure] / moved
    s20 --> s9 : job.448 box recompute [derives_success] / moved
    s20 --> s2 : job.449 box recompute [derives_failure] / moved
    s20 --> s4 : job.135 STATUS [injects_inactive_box] / cascades
    s20 --> s4 : job.452 noexec moved [noexec_settles_box] / cascades
    s26 --> s24 : job.454 member moves [door_reads_overrides]
    s26 --> s13 : job.455 member moves [door_folds_only]
    s24 --> s9 : job.126 [override_success] / completed
    s24 --> s2 : job.127 [override_failure] / completed
    s24 --> s13 : job.456 [override_unmet_folds]
    s24 --> s26 : job.457 [override_unmet_settles]
    s13 --> s9 : job.128 [fold_success] / completed
    s13 --> s2 : job.129 [fold_failure] / completed
    s13 --> s26 : job.458 [fold_decides_nothing]
    s17 --> s18 : job.201 ON_ICE [not_live_or_queued] / iced
    s18 --> s17 : job.202 OFF_ICE
    s18 --> s17 : job.203 FORCE_STARTJOB [force_clears]
    s15 --> s16 : job.204 ON_HOLD [not_live_or_queued]
    s16 --> s15 : job.205 OFF_HOLD [not_queued] / released_hold
    s16 --> s15 : job.206 FORCE_STARTJOB [force_clears]
    s16 --> s15 : job.207 ON_NOEXEC [noexec_applies]
    s15 --> s16 : job.208 box running [has_auto_hold]
    s21 --> s22 : job.209 ON_NOEXEC [noexec_applies] / noexec
    s22 --> s21 : job.210 OFF_NOEXEC / cleared_noexec
    s22 --> s21 : job.211 OFF_NOEXEC on its box
    s18 --> s18 : job.218 ON_ICE [not_live] / iced_again
    s17 --> s17 : job.219 OFF_ICE
    s16 --> s16 : job.220 ON_HOLD [not_live_or_queued]
    s15 --> s15 : job.221 OFF_HOLD [not_queued] / released_hold
    s22 --> s22 : job.222 ON_NOEXEC [noexec_applies] / noexec
    s21 --> s21 : job.223 OFF_NOEXEC / cleared_noexec
    s21 --> s22 : job.231 ON_NOEXEC on its box
    s16 --> s15 : job.232 ON_NOEXEC on its box
    s22 --> s22 : job.233 ON_NOEXEC on its box
    s17 --> s18 : job.234 ON_ICE [queued] / report_ice
    s15 --> s16 : job.235 ON_HOLD [queued] / held
    s16 --> s16 : job.236 ON_HOLD [queued] / held
    s16 --> s15 : job.237 OFF_HOLD [queued] / released_waiter
    s15 --> s15 : job.238 OFF_HOLD [queued] / released_waiter
    s23 --> s25 : job.301 admitted start [has_demand]
    s14 --> c1 : job.302 admitted start [takes_over]
    s14 --> s25 : job.303 admitted start [on_held_units]
    s25 --> s23 : job.304 leave STARTING or RUNNING [frees_all]
    s25 --> s14 : job.305 leave STARTING or RUNNING [keeps_held]
    s25 --> s23 : job.306 leave STARTING or RUNNING [spends]
    s14 --> s23 : job.307 RELEASE_RESOURCE / released_held
    s14 --> s23 : job.308 first input of a period [left_catalog]
    s14 --> s25 : job.319 STATUS [injects_live]
    s20 --> s0 : job.402 start admitted [admitted_admit]
    s20 --> s0 : job.426 start admitted [admitted_on_held]
    s0 --> s8 : job.427 [admitted_by_start] / starting_admitted
    s0 --> s8 : job.428 [admitted_by_readmit] / starting_readmitted
    s6 --> s11 : job.436 KILLJOB [armed] / moved
    s6 --> s11 : job.437 KILLJOB [unarmed] / moved
    s6 --> s4 : job.438 readmit [readmit_leaves_armed] / left_queue
    s6 --> s4 : job.439 readmit [readmit_leaves_unarmed] / left_queue
    c0 --> s4 : job.171 [injected_inactive] / moved
    c0 --> s8 : job.172 [injected_starting] / moved
    c0 --> s7 : job.173 [injected_running] / moved
    c0 --> s9 : job.174 [injected_success] / moved
    c0 --> s2 : job.175 [injected_failure] / moved
    c0 --> s11 : job.176 [else] / moved
    c1 --> s25 : job.309 [has_demand]
    c1 --> s23 : job.310 [else]
```

| Id | Source | Event | Guard | Action | Target | Kind | Cite |
| --- | --- | --- | --- | --- | --- | --- | --- |
| job.100 | IDLE | start | start_bounded | L010: a re-trigger loop inside one instant. A start that descends from MAX_NESTED_STARTS admitted starts of the same job, its `start admitted` items in the attempt's ancestry, is refused with a START_REFUSED line. |  | internal | DL-304 |
| job.101 | IDLE | start | start_bypasses | extent_bypass | SUCCESS | external | SEM-22, DL-54 |
| job.102 | IDLE | start | start_admits_job | extent_admit |  | internal | SEM-10, DL-120, DL-364 |
| job.103 | IDLE | start | start_queues | extent_queue | QUE_WAIT | external | DL-50, DL-247, DL-255 |
| job.104 | FAILED | start | start_on_held | extent_on_held |  | internal | DL-256, DL-364 |
| job.105 | STARTING | started | started_job | moved | RUNNING | external | SEM-10 |
| job.106 | TERMINAL | start | start_skips | moved | INACTIVE | external | SEM-33, DL-246 |
| job.107 | TERMINAL | start | start_skips_member | moved | INACTIVE | external | SEM-33, DL-154 |
| job.108 | QUE_WAIT | readmit | readmit_admits | admit | ADMITTED | external | DL-50, DL-257 |
| job.109 | QUE_WAIT | readmit | readmit_cancels | cancelled | INACTIVE | external | DL-50, DL-247 |
| job.112 | QUE_WAIT | ice moved |  | moved | INACTIVE | external | DL-50, DL-285 |
| job.113 | QUE_WAIT | noexec moved | noexec_settles_job | injected | INACTIVE | external | DL-254 |
| job.114 | LIVE | KILLJOB |  | terminated | TERMINATED | external | SEM-14 |
| job.115 | RUNNING | TIMER term_run_time | current_run | terminated | TERMINATED | external | autosys-semantics ss5 |
| job.116 | LIVE | box ends FAILURE or TERMINATED | is_terminator | terminated | TERMINATED | external | SEM-14 |
| job.117 | lifecycle | STATUS | exit_success | moved | SUCCESS | external | SEM-09 |
| job.118 | lifecycle | STATUS | exit_failure | moved | FAILURE | external | SEM-09 |
| job.119 | lifecycle | STATUS | injects |  | injected status | external | DL-13, DL-264 |
| job.120 | lifecycle | STATUS | injects_inactive_job | injected | INACTIVE | external | DL-242, DL-235 |
| job.121 | FAILED | noexec moved | noexec_settles_job | injected | INACTIVE | external | SEM-22, DL-243 |
| job.122 | TERMINAL | box start |  | batched | INACTIVE | external | SEM-10, DL-242 |
| job.445 | lifecycle | box set INACTIVE |  | batched | INACTIVE | external | SEM-18, DL-242 |
| job.124 | IDLE | start | start_admits_box | extent_admit |  | internal | SEM-10, DL-242, DL-364 |
| job.125 | STARTING | started | started_box | runs_box | RUNNING | external | SEM-10, SEM-33, DL-246 |
| job.130 | RUNNING | member ends FAILURE or TERMINATED | terminator_fires | terminated | TERMINATED | external | SEM-14 |
| job.446 | lifecycle | box recompute | overrides_success | moved | SUCCESS | external | SEM-15 |
| job.447 | lifecycle | box recompute | overrides_failure | moved | FAILURE | external | SEM-15 |
| job.448 | lifecycle | box recompute | derives_success | moved | SUCCESS | external | SEM-15 |
| job.449 | lifecycle | box recompute | derives_failure | moved | FAILURE | external | SEM-15 |
| job.135 | lifecycle | STATUS | injects_inactive_box | cascades | INACTIVE | external | SEM-18 |
| job.440 | IDLE | KILLJOB |  | EVENT_IGNORED |  | internal | DL-64, DL-81 |
| job.441 | lifecycle | start | start_refused |  |  | internal | DL-81, DL-246 |
| job.442 | LIVE | ON_ICE |  | EVENT_IGNORED |  | internal | DL-254 |
| job.443 | LIVE | ON_HOLD |  | EVENT_IGNORED |  | internal | DL-254 |
| job.444 | lifecycle | ON_NOEXEC | noexec_ignored |  |  | internal | DL-254 |
| job.452 | lifecycle | noexec moved | noexec_settles_box | cascades | INACTIVE | external | DL-254 |
| job.177 | lifecycle | STARTJOB |  | relay_start |  | internal | SEM-10 |
| job.178 | lifecycle | FORCE_STARTJOB |  | relay_start |  | internal | SEM-23 |
| job.179 | lifecycle | TIMER run_window |  | relay_start |  | internal | SEM-33 |
| job.180 | lifecycle | start | start_moves_nothing | A gate holds the attempt silently, or a postponed start waits. |  | ignored | SEM-20, SEM-21, SEM-33 |
| job.181 | QUE_WAIT | readmit | readmit_keeps | The waiter stays queued. |  | ignored | DL-50, DL-247 |
| job.182 | lifecycle | readmit | not_queued | A queue scan addresses a waiter. |  | ignored (unreachable) |  |
| job.183 | lifecycle | started |  | The admitted run is no longer STARTING: a wake of its STARTING ended it, or started the job again (`Started.current`). |  | ignored | SEM-10, DL-350 |
| job.184 | lifecycle | TIMER term_run_time |  | The deadline is an earlier run's, or the run is no longer RUNNING. |  | ignored | SEM-14 |
| job.186 | lifecycle | member moves |  | Only a RUNNING box's completion door reads its overrides. |  | ignored (unreachable) |  |
| job.189 | lifecycle | member ends FAILURE or TERMINATED | is_box | A terminator ends only a RUNNING box, the door or the recompute follows. |  | ignored | SEM-13, SEM-14 |
| job.190 | lifecycle | member ends FAILURE or TERMINATED | is_job | Only a box has members. |  | ignored (unreachable) |  |
| job.191 | lifecycle | box recompute | recomputes_nothing | No verdict, the box already has it, or it is ADMITTED. |  | ignored | SEM-13, SEM-15 |
| job.192 | lifecycle | box recompute | is_job | Only a box has members. |  | ignored (unreachable) |  |
| job.450 | lifecycle | box recompute | live_or_terminated_box | A live box's own end decides it, TERMINATED is sticky until the next box start (SEM-13). |  | ignored (unreachable) |  |
| job.195 | lifecycle | box start |  | INACTIVE loses its exit code by a plain write, a live or queued job keeps its run. |  | ignored | SEM-10, DL-242 |
| job.196 | INACTIVE | box set INACTIVE |  | Already INACTIVE: the cascade leaves it out. |  | ignored | SEM-18 |
| job.197 | lifecycle | box ends FAILURE or TERMINATED |  | Not live, or no job_terminator: nothing to terminate. |  | ignored | SEM-14 |
| job.451 | lifecycle | ice moved |  | Not queued: the member's completion moment and the wakes follow. |  | ignored | SEM-20 |
| job.198 | lifecycle | noexec moved | noexec_moves_nothing | Applied, and no box settles: the released jobs' start retries follow. |  | ignored | DL-254 |
| job.199 | lifecycle | noexec moved | noexec_ignored | An applied ON_NOEXEC reports it, an ignored ON_NOEXEC moves no flag. |  | ignored (unreachable) |  |
| job.453 | ADMITTED | noexec moved |  | ON_NOEXEC at ADMITTED is itself a marked cell (job.423), so its flag move never pops there. |  | ignored (unreachable) |  |
| job.454 | settled | member moves | door_reads_overrides |  | overriding | external | SEM-12, DL-371 |
| job.455 | settled | member moves | door_folds_only |  | folding | external | SEM-11, DL-371 |
| job.126 | overriding | (completion) | override_success | completed | SUCCESS | external | SEM-12 |
| job.127 | overriding | (completion) | override_failure | completed | FAILURE | external | SEM-12, SEM-14 |
| job.456 | overriding | (completion) | override_unmet_folds | No override holds: the default fold follows. | folding | external | SEM-11, SEM-12 |
| job.457 | overriding | (completion) | override_unmet_settles | No override holds, and the walk up reads no fold. | settled | external | SEM-12 |
| job.128 | folding | (completion) | fold_success | completed | SUCCESS | external | SEM-11 |
| job.129 | folding | (completion) | fold_failure | completed | FAILURE | external | SEM-11, SEM-14 |
| job.458 | folding | (completion) | fold_decides_nothing | A member is not done, or an unmet override suppresses the default. | settled | external | SEM-11, SEM-12 |
| job.459 | overriding | member moves |  | A second visit while a phase is open: the phase's entry only reads and the row that enters it sends nothing, so its completion pops before any other item. |  | ignored (unreachable) |  |
| job.460 | folding | member moves |  | A second visit while a phase is open: the phase's entry only reads and the row that enters it sends nothing, so its completion pops before any other item. |  | ignored (unreachable) |  |
| job.201 | ice_off | ON_ICE | not_live_or_queued | iced | ice_on | external | SEM-20, DL-254, DL-285 |
| job.202 | ice_on | OFF_ICE |  | no re-evaluation | ice_off | external | SEM-20 |
| job.203 | ice_on | FORCE_STARTJOB | force_clears |  | ice_off | external | SEM-23, DL-243 |
| job.204 | hold_off | ON_HOLD | not_live_or_queued |  | hold_on | external | SEM-21, DL-254 |
| job.205 | hold_on | OFF_HOLD | not_queued | released_hold | hold_off | external | SEM-21 |
| job.206 | hold_on | FORCE_STARTJOB | force_clears |  | hold_off | external | SEM-23, DL-243 |
| job.207 | hold_on | ON_NOEXEC | noexec_applies |  | hold_off | external | DL-254 |
| job.208 | hold_off | box running | has_auto_hold |  | hold_on | external | autosys-semantics ss5 |
| job.209 | noexec_off | ON_NOEXEC | noexec_applies | noexec | noexec_on | external | SEM-22, DL-254 |
| job.210 | noexec_on | OFF_NOEXEC |  | cleared_noexec | noexec_off | external | DL-243 |
| job.211 | noexec_on | OFF_NOEXEC on its box |  |  | noexec_off | external | DL-254 |
| job.218 | ice_on | ON_ICE | not_live | iced_again | ice_on | external | SEM-20 |
| job.219 | ice_off | OFF_ICE |  | as job.202 | ice_off | external | SEM-20 |
| job.220 | hold_on | ON_HOLD | not_live_or_queued |  | hold_on | external | SEM-21 |
| job.221 | hold_off | OFF_HOLD | not_queued | released_hold | hold_off | external | SEM-21 |
| job.222 | noexec_on | ON_NOEXEC | noexec_applies | noexec | noexec_on | external | SEM-22, DL-254 |
| job.223 | noexec_off | OFF_NOEXEC |  | cleared_noexec | noexec_off | external | DL-243 |
| job.227 | flags | box running |  | Not auto_hold, or already held. |  | ignored |  |
| job.229 | noexec_off | OFF_NOEXEC on its box |  | Only a flagged job clears. |  | ignored |  |
| job.231 | noexec_off | ON_NOEXEC on its box |  | as job.209 | noexec_on | external | SEM-22, DL-254, DL-347 |
| job.232 | hold_on | ON_NOEXEC on its box |  | As job.207: ON_NOEXEC supersedes ON_HOLD, and the job retries its start. | hold_off | external | SEM-22, DL-254, DL-347 |
| job.233 | noexec_on | ON_NOEXEC on its box |  | as job.231 | noexec_on | external | SEM-22, DL-254, DL-347 |
| job.234 | ice_off | ON_ICE | queued | report_ice | ice_on | external | DL-50, DL-285 |
| job.235 | hold_off | ON_HOLD | queued | held | hold_on | external | DL-247 |
| job.236 | hold_on | ON_HOLD | queued | held | hold_on | external | DL-247 |
| job.237 | hold_on | OFF_HOLD | queued | released_waiter | hold_off | external | DL-50 |
| job.238 | hold_off | OFF_HOLD | queued | released_waiter | hold_off | external | DL-50 |
| job.301 | none | admitted start | has_demand |  | reserved | external | DL-120 |
| job.302 | held | admitted start | takes_over |  | taken over | external | DL-256 |
| job.303 | held | admitted start | on_held_units |  | reserved | external | DL-256 |
| job.304 | reserved | leave STARTING or RUNNING | frees_all |  | none | external | DL-120 |
| job.305 | reserved | leave STARTING or RUNNING | keeps_held |  | held | external | DL-256 |
| job.306 | reserved | leave STARTING or RUNNING | spends |  | none | external | SEM-16, DL-120 |
| job.307 | held | RELEASE_RESOURCE |  | released_held | none | external | DL-256 |
| job.308 | held | first input of a period | left_catalog |  | none | external | DL-256 |
| job.311 | none | admitted start | no_demand | No units. |  | ignored |  |
| job.312 | reserved | admitted start | takes_over | settle_ended_run |  | internal (unreachable) | DL-256 |
| job.313 | none | leave STARTING or RUNNING |  | The run held nothing. |  | ignored |  |
| job.314 | held | leave STARTING or RUNNING |  | An earlier run's units stay held. Unreachable: a live status never stands on held units (an admission takes them over first, job.302 and job.303, and an injected live status makes them the run's, job.319), and a run's settlement is sent once. |  | ignored (unreachable) |  |
| job.315 | none | RELEASE_RESOURCE |  | Nothing held. |  | ignored | DL-256 |
| job.316 | reserved | RELEASE_RESOURCE |  | The run's units go back when it ends. |  | ignored | DL-256 |
| job.317 | none | first input of a period |  | The opening addresses only a removed job that holds units. |  | ignored (unreachable) |  |
| job.318 | reserved | first input of a period |  | The boundary refuses a live removed job (period-model ss10.1). |  | ignored (unreachable) |  |
| job.319 | held | STATUS | injects_live | Units on a live row are the run's: they settle when it ends. | reserved | external | DL-120, DL-256 |
| job.320 | reserved | admitted start | on_held_units | A forced start on held units starts from FAILED, never mid-cascade. |  | ignored (unreachable) |  |
| job.321 | held | first input of a period | in_catalog | The opening addresses only a job the catalog no longer defines. |  | ignored (unreachable) |  |
| job.401 | IDLE | start | start_settles_first | settles_first |  | internal | DL-340, DL-349 |
| job.402 | lifecycle | start admitted | admitted_admit | An admission enters ADMITTED, whose entry sends its extent. | ADMITTED | external | DL-304, DL-349, DL-364 |
| job.404 | INACTIVE | start | start_skips_inactive_member | skips_in_box |  | internal | SEM-33, DL-154 |
| job.405 | lifecycle | box rules |  | moment |  | internal |  |
| job.406 | lifecycle | settle |  | moment |  | internal |  |
| job.407 | lifecycle | wake |  | moment |  | internal |  |
| job.408 | lifecycle | walk up |  | moment |  | internal |  |
| job.409 | lifecycle | holders may leave |  | moment |  | internal |  |
| job.411 | lifecycle | load taken |  | moment |  | internal |  |
| job.412 | lifecycle | box ends step |  | moment |  | internal |  |
| job.413 | lifecycle | notify step |  | moment |  | internal |  |
| job.414 | lifecycle | window pass |  | moment |  | internal |  |
| job.415 | lifecycle | after inject |  | moment |  | internal |  |
| job.416 | lifecycle | after leave |  | moment |  | internal |  |
| job.417 | lifecycle | retry step |  | moment |  | internal |  |
| job.418 | LIVE | settle |  | The job is live when its move's settlement pops: the move entered STARTING or RUNNING, or the job started again inside its move's cascade (DL-340). Nothing settles, a rank the move left still drops. |  | internal | DL-340, DL-350 |
| job.420 | ADMITTED | box set INACTIVE |  | An admitted start leaves ADMITTED inside its own extent, before any cascade reaches it. |  | ignored (unreachable) |  |
| job.421 | ADMITTED | start |  | An admitted start leaves ADMITTED inside its own extent, before any cascade reaches it. |  | ignored (unreachable) |  |
| job.422 | ADMITTED | KILLJOB |  | An admitted start leaves ADMITTED inside its own extent, before any cascade reaches it. |  | ignored (unreachable) |  |
| job.423 | ADMITTED | ON_NOEXEC |  | An admitted start leaves ADMITTED inside its own extent, before any cascade reaches it. |  | ignored (unreachable) |  |
| job.424 | lifecycle | start admitted | admitted_bypass | bypass_extent |  | internal | SEM-22, DL-349 |
| job.425 | lifecycle | start admitted | admitted_queue | queue_extent |  | internal | DL-50, DL-349 |
| job.426 | lifecycle | start admitted | admitted_on_held | A forced start on held units enters ADMITTED, whose entry sends its extent. | ADMITTED | external | DL-256, DL-349, DL-364 |
| job.427 | ADMITTED | (completion) | admitted_by_start | starting_admitted | STARTING | external | SEM-10, DL-242, DL-364 |
| job.428 | ADMITTED | (completion) | admitted_by_readmit | starting_readmitted | STARTING | external | DL-50, DL-242, DL-364 |
| job.429 | IDLE | start | start_arms |  |  | internal | SEM-32, DL-54, DL-365 |
| job.430 | lifecycle | actual start | armed |  |  | internal | DL-54 |
| job.431 | lifecycle | actual start | unarmed | as job.430 |  | internal | DL-54 |
| job.432 | lifecycle | DISARM | armed |  |  | internal | DL-158 |
| job.433 | lifecycle | DISARM | unarmed | As job.432, its line says there was no latch. |  | internal | DL-158 |
| job.434 | lifecycle | box run ends | armed | SCHED_DISARM |  | internal | DL-54 |
| job.435 | lifecycle | box run ends | unarmed | Only an armed member disarms. |  | ignored | DL-54 |
| job.436 | QUE_WAIT | KILLJOB | armed | moved | TERMINATED | external | DL-50, DL-54 |
| job.437 | QUE_WAIT | KILLJOB | unarmed | moved | TERMINATED | external | DL-50, DL-54 |
| job.438 | QUE_WAIT | readmit | readmit_leaves_armed | left_queue | INACTIVE | external | DL-257 |
| job.439 | QUE_WAIT | readmit | readmit_leaves_unarmed | left_queue | INACTIVE | external | DL-257 |

| Id | Choice | Guard | Action | Target |
| --- | --- | --- | --- | --- |
| job.171 | injected status | injected_inactive | moved | INACTIVE |
| job.172 | injected status | injected_starting | moved | STARTING |
| job.173 | injected status | injected_running | moved | RUNNING |
| job.174 | injected status | injected_success | moved | SUCCESS |
| job.175 | injected status | injected_failure | moved | FAILURE |
| job.176 | injected status | else | moved | TERMINATED |
| job.309 | taken over | has_demand |  | reserved |
| job.310 | taken over | else |  | none |

| State | In | Entry | Exit | Defers | Waits |
| --- | --- | --- | --- | --- | --- |
| job |  |  |  |  |  |
| lifecycle | job/status |  |  |  |  |
| IDLE | lifecycle/status |  |  |  |  |
| INACTIVE | IDLE/idle | writes_status |  |  |  |
| TERMINAL | IDLE/idle |  |  |  |  |
| SUCCESS | TERMINAL/terminal | writes_status |  |  |  |
| FAILED | TERMINAL/terminal |  |  |  |  |
| FAILURE | FAILED/failed | writes_status |  |  |  |
| TERMINATED | FAILED/failed | writes_status |  |  |  |
| QUE_WAIT | lifecycle/status | writes_status |  |  |  |
| LIVE | lifecycle/status |  |  |  |  |
| STARTING | LIVE/live | writes_status |  |  |  |
| RUNNING | LIVE/live | writes_status |  |  |  |
| settled | RUNNING/door |  |  |  |  |
| overriding | RUNNING/door | reads_overrides |  |  |  |
| folding | RUNNING/door | reads_fold |  |  |  |
| ADMITTED | lifecycle/status | starts_run |  |  |  |
| flags | job/flags |  |  |  |  |
| ice_off | flags/ice |  |  |  |  |
| ice_on | flags/ice |  |  |  |  |
| hold_off | flags/hold |  |  |  |  |
| hold_on | flags/hold |  |  |  |  |
| noexec_off | flags/noexec |  |  |  |  |
| noexec_on | flags/noexec |  |  |  |  |
| none | job/holding |  |  |  |  |
| reserved | job/holding |  |  |  |  |
| held | job/holding |  |  |  |  |

| Name | Kind | Prose |
| --- | --- | --- |
| start_refused | guard | `start_verdict` is `refused`: a START_REFUSED line, no move. |
| start_moves_nothing | guard | `start_verdict` is `ignored`: the status does not move, and nothing latches. |
| start_arms | guard | `start_verdict` is `arm`: the tick latches (SEM-32, DL-54). |
| start_bounded | guard | `start_verdict` is `bounded`: the item descends from `MAX_NESTED_STARTS` admitted starts of the job (DL-304), so a START_REFUSED line, whoever asked, and no move. |
| start_settles_first | guard | `start_verdict` is `settle_first`: past the bound, a run of the job ended and its settlement still waits behind the box rules (DL-340). The tier settles it, and the same attempt is made again. |
| start_bypasses | guard | `start_verdict` is `bypass`: ON_NOEXEC on the job or a box above it (SEM-22). |
| start_admits_job | guard | `start_verdict` is `admit`, and the data says the job is not a box. |
| start_admits_box | guard | `start_verdict` is `admit`, and the data says the job is a box. |
| start_queues | guard | `start_verdict` is `queue`: not admissible (DL-50). |
| start_on_held | guard | `start_verdict` is `on_held`: forced, FAILED, and `held` (DL-256). |
| start_skips | guard | `start_verdict` is `skip`: a job in no box, outside its window (SEM-33). |
| start_skips_member | guard | `start_verdict` is `skip_member`: a member of a RUNNING box (DL-154). |
| start_skips_inactive_member | guard | `start_verdict` is `skip_inactive_member`: an INACTIVE member of a RUNNING box it has not run in (DL-154): resolved with no move. |
| readmit_admits | guard | `readmit_verdict` is `admit`. |
| readmit_cancels | guard | `readmit_verdict` is `cancel`: the member's box stopped running. |
| readmit_leaves_armed | guard | `readmit_verdict` is `leave`: the queued recheck failed (DL-257), the payload: the waiter is armed. |
| readmit_leaves_unarmed | guard | `readmit_verdict` is `leave`, the payload: the waiter is not armed. |
| readmit_keeps | guard | `readmit_verdict` is `keep`: held, not admissible, or kept by an admit-only scan. |
| admitted_bypass | guard | The payload: the admitting row was the bypass (job.101). |
| admitted_queue | guard | The payload: the admitting row was the queue (job.103). |
| admitted_on_held | guard | The payload: the admitting row was the forced start on held units (job.104). |
| admitted_admit | guard | The payload: the admitting row was an admission (job.102, job.124). |
| admitted_by_start | guard | The payload that entered ADMITTED is a start's `start admitted` (job.402, job.426). |
| admitted_by_readmit | guard | The payload that entered ADMITTED is a waiter's `readmit` (job.108), the one other row that enters it. |
| exit_success | guard | The payload has no status, and its exit code is a success. |
| exit_failure | guard | The payload has no status, and its exit code is not a success. |
| injects | guard | The payload's status is injectable and not INACTIVE, or the data says the job has no catalog entry. |
| injects_inactive_job | guard | The payload's status is INACTIVE, the data: a catalog job, not a box. |
| injects_inactive_box | guard | The payload's status is INACTIVE, the data: a box. |
| injects_live | guard | The payload's status is STARTING or RUNNING: units the job holds are the run's. |
| injected_inactive | guard | The payload's status is INACTIVE. |
| injected_starting | guard | The payload's status is STARTING. |
| injected_running | guard | The payload's status is RUNNING. |
| injected_success | guard | The payload's status is SUCCESS. |
| injected_failure | guard | The payload's status is FAILURE. |
| not_live | guard | The configuration's status is not STARTING or RUNNING. |
| force_clears | guard | The data: a catalog job, the configuration's status is not live or queued (`start_verdict` refuses those first). |
| queued | guard | The configuration's status is QUE_WAIT. |
| not_live_or_queued | guard | The configuration's status is neither live nor QUE_WAIT: an IDLE status, or the transient ADMITTED, which no operator's input meets. |
| not_queued | guard | The configuration's status is not QUE_WAIT. |
| armed | guard | The payload: the job's arm is latched (`Arm`, SEM-32, DL-54). |
| unarmed | guard | The payload: the job's arm is not latched. |
| noexec_ignored | guard | ON_NOEXEC is ignored: the configuration has ice on, RUNNING, or STARTING on a job that is not a box (`noexec_own_reason`), or the data says a box and the payload's `tree_busy` holds. The flag move does not change what it reads. |
| noexec_applies | guard | Not `noexec_ignored`. |
| noexec_settles_job | guard | Not `noexec_ignored`, the data: a catalog job, not a box. |
| noexec_settles_box | guard | Not `noexec_ignored`, the data: a box, the configuration's status is not INACTIVE, or the payload's `change` holds (`NoexecFacts.change`). |
| noexec_moves_nothing | guard | Not `noexec_ignored`, and not `noexec_settles_box`: no box settles here. A job's settlement is an inner row's (job.113, job.121), which shadows this one (§3). |
| is_box | guard | The data says the job is a box. |
| is_job | guard | The data says the job is not a box. |
| is_terminator | guard | The data: a member with job_terminator (SEM-14). |
| has_auto_hold | guard | The data: a member with auto_hold. |
| in_catalog | guard | The data: the job has a catalog entry. |
| left_catalog | guard | The data: the job left the catalog (DL-256). |
| current_run | guard | The payload: the deadline's run is the row's run. |
| started_job | guard | The data: not a box, the payload: the admitted run is the row's run (`Started`). |
| started_box | guard | The data: a box, the payload: the admitted run is the row's run (`Started`). |
| door_reads_overrides | guard | The data: a box, the payload's visit reads the overrides (SEM-12): a member's terminal, RUNNING or resolving move, an ice's completion moment, a box start's, or the walk up. |
| door_folds_only | guard | The data: a box, the payload's visit does not read the overrides: a member's move they do not read, which the default fold alone follows (SEM-11). |
| override_success | guard | The payload's overrides decide SUCCESS: box_success holds (SEM-12). |
| override_failure | guard | The payload's overrides decide FAILURE: box_failure holds (SEM-12). |
| override_unmet_folds | guard | No override holds, and the payload's visit folds (SEM-11). |
| override_unmet_settles | guard | No override holds, and the payload's visit does not fold: the walk up (SEM-12). |
| fold_success | guard | The payload's fold decides SUCCESS: every member is done, none failed, and no box_success is declared (SEM-11). |
| fold_failure | guard | The payload's fold decides FAILURE: every member is done, one failed, and no box_failure is declared (SEM-11). |
| fold_decides_nothing | guard | The payload's fold decides nothing: a member is not done, or an unmet override suppresses the default (SEM-11). |
| overrides_success | guard | `_recomputes`, the payload: box_success decides SUCCESS (SEM-15). |
| overrides_failure | guard | `_recomputes`, the payload: box_failure decides FAILURE (SEM-15). |
| recomputes_nothing | guard | The data: a box, not `_recomputes` and not `live_or_terminated_box`: the payload names no verdict, the box already has it, or its status is the transient ADMITTED (SEM-15). |
| live_or_terminated_box | guard | The data: a box, the configuration's status is STARTING, RUNNING or TERMINATED. |
| derives_success | guard | `_recomputes`, the payload: no member failed, and no box_success suppresses the default SUCCESS (SEM-15). |
| derives_failure | guard | `_recomputes`, the payload: a member failed, and no box_failure suppresses the default FAILURE (SEM-15). |
| terminator_fires | guard | The data: a box, the payload's `fires` (SEM-14). |
| has_demand | guard | The payload's demand vector reserves units. |
| no_demand | guard | The payload's demand vector reserves nothing: reserving nothing is no move. |
| takes_over | guard | The payload: an admitted start that is not on held units. |
| on_held_units | guard | The payload: a FORCE_STARTJOB starts on the held units (DL-256). |
| frees_all | guard | The payload: the policy of the run's end frees every unit. |
| keeps_held | guard | The payload: the policy of the run's end keeps a renewable's unit. |
| spends | guard | The payload: the policy of the run's end spends a unit, and keeps none. |
| relay_start | action | STARTJOB, FORCE_STARTJOB or a fired run_window timer: the attempt itself, as `_dispatch` makes it: scheduled, its refusal traced, forced for FORCE_STARTJOB. The payload's facts pass on. |
| extent_bypass | action | A bypass (job.101) sends `start admitted` first, and the rest of the start follows from it (DL-349), the tier's handle gives it the record of the move to SUCCESS, whose box rules and wakes the extent sends. |
| extent_queue | action | A queued start (job.103) sends `start admitted`, which sends the move to QUE_WAIT's box rules and wakes: the waiter's units wait for its readmission. |
| extent_admit | action | An admission (job.102, job.124) sends `start admitted` first, with the units it reserves and the payload's handle on the job's units. |
| extent_on_held | action | A forced start on held units (job.104) sends `start admitted` first, with the held units it takes over. |
| bypass_extent | action | A bypass's `start admitted` (job.424): the run's start, which consumes the arm, then its move's box rules, settlement and wakes, as the tier's handle says (DL-350). |
| queue_extent | action | A queue's `start admitted` (job.425): its move's box rules, settlement and wakes, then the load mark of a machine's load it takes (DL-255), as the tier's handle says. |
| starts_run | action | ADMITTED's entry: what precedes STARTING, from the payload that entered it. The units it reserves or takes over, the run's start, which consumes the arm, then a box's resets of its jobs, as the tier's handle says (DL-256, DL-350, DL-364). |
| starting_admitted | action | ADMITTED's completion after a start (job.427): the move to STARTING, its box rules, settlement and wakes, a box's reset notices, `started`, then the queue's scan when the held units an admission took over exceed its demand, and the load mark, as the tier's handle says (DL-256, DL-350, DL-364). |
| starting_readmitted | action | ADMITTED's completion after a readmission (job.428): the move to STARTING, its box rules, settlement and wakes, a box's reset notices, then `started`, as the tier's handle says (DL-350, DL-364). |
| admit | action | A readmitted waiter (job.108) enters ADMITTED with its `readmit`, whose entry sends its extent: no `start admitted`, since a readmission is no start the bound counts (DL-350). The scan's pass hears that the waiter moved, as the tier's handle says. |
| settle_ended_run | action | A start meets the units of a run whose settlement is still owed (`OracleCascade._on_settles_first`): that run settles first, by the end it made, then the start is admitted again against what the settlement left. The settlement's policy is read from the admission's handle (DL-349). Its row, job.312, is marked unreachable: job.401 settles the run before the start is admitted, so the oracle's admission carries no previous end. |
| settles_first | action | An attempt whose run's settlement is owed (job.401, DL-340): that run settles by the end it made, and the same attempt is made again, so it reads the admission the settlement leaves, as the tier's handle says. |
| writes_status | action | A status state's entry: the status it is, which the hook writes with the line of the row that entered it, as the tier's handle says. The initial entry writes nothing: an instance starts in the status its row holds. |
| reads_overrides | action | `overriding`'s entry: the visit's overrides' facts are built now, from the payload that entered the phase (`Door.begin_override`), it sends nothing (DL-371). |
| reads_fold | action | `folding`'s entry: the visit's fold's facts are built now, after the overrides' phase when the visit read them (`Door.begin_fold`), it sends nothing (DL-371). |
| moved | action | A status move on its own (`OracleCascade._after`): the move's box rules, then its settlement, then its wakes, as the tier's handle says (DL-350). |
| terminated | action | A kill: as `moved`, then a box's job_terminators, for the run read before the move (DL-348). |
| completed | action | A RUNNING box's end by an override or the default fold: as `moved`, then its job_terminators if it ended FAILURE, for the run it has then (`OracleCascade._box_ends`, DL-348). |
| injected | action | An operator's INACTIVE on a job that is not a box (`_after_inject`): as `moved`, then the idle parent's recompute (SEM-15). |
| cascades | action | An operator's INACTIVE on a box (SEM-18, DL-242): the batch INACTIVE path, its own settlement first, every job it contains, the disarms, then each row's box rules and wakes, then the idle parent's recompute. |
| batched | action | A row of the batch INACTIVE path (a box start's reset, a box's cascade): it settles first, its box rules and wakes are the batch's second phase (`OracleCascade._notify`). |
| cancelled | action | A waiter of a stopped box cancelled by the scan (job.109): as `moved`, and the scan's pass hears that the waiter moved. |
| left_queue | action | A waiter that left the queue unstarted (job.438, job.439, DL-257): as `moved`, then its window or day disposition, and the scan's pass hears that the waiter moved. |
| runs_box | action | A box's RUNNING (job.125): as `moved`, then its members' auto_hold and their starts, then the window decisions of the box starts in progress (`OracleCascade._box_running`, DL-246). |
| skips_in_box | action | A window skip of an INACTIVE member (job.404, DL-154): no move to ride, so its box rules run for it, as an INACTIVE move's would. |
| report_ice | action | ON_ICE moved a waiter's flag (job.234): the status rule that takes it out of the queue follows as its own item (job.112), with the payload's handle on the units (DL-50). |
| iced | action | ON_ICE moved the flag of a job that is not a waiter (job.201): the status rule that reads it follows, then the box rules its ice may run and its referencers' wakes (SEM-20, DL-285), as the tier's handle says. |
| iced_again | action | ON_ICE on a job already on ice: its referencers wake again (SEM-20). |
| held | action | ON_HOLD on a waiter (job.235, job.236): a held waiter blocks no one, so the queue is scanned (DL-247). |
| released_waiter | action | OFF_HOLD on a waiter (job.237, job.238): its queue is scanned (DL-50). |
| released_hold | action | OFF_HOLD on a job that is not a waiter (job.205, job.221): it attempts its start (SEM-21). |
| noexec | action | ON_NOEXEC moved the flag: on a box, every job it contains takes it first (DL-347), the status rule that reads it follows, then the jobs it released retry their start (DL-254), as the tier's handle says. |
| cleared_noexec | action | OFF_NOEXEC on a box clears every job it contains (DL-254). |
| released_held | action | RELEASE_RESOURCE gave held units back: the queue is scanned (DL-256). |
| moment | action | A moment of a cascade (DL-350): the tier's step its payload carries (`Moment`), run now. |

## queue_scan

Clock: logical.

```mermaid
stateDiagram-v2
    state "queue_scan" as s7
    state s7 {
        [*] --> s3
        state "idle" as s3
        state "pass" as s6
        state s6 {
            [*] --> s4
            state "next_waiter" as s4
            state "awaiting_readmit" as s0
            state "done" as s1
            --
            [*] --> s5
            state "no_request" as s5
            state "full_requested" as s2
        }
    }
    state c0 <<choice>>
    s3 --> s6 : queue_scan.01 scan
    s5 --> s2 : queue_scan.02 scan
    s4 --> c0 : queue_scan.04
    s0 --> s1 : queue_scan.05 [waiter_moved]
    s0 --> c0 : queue_scan.06 [waiter_stayed]
    s1 --> s6 : queue_scan.07 [pass_upgrades] / upgrades
    s1 --> s6 : queue_scan.08 [pass_restarts]
    s1 --> s3 : queue_scan.09 [scan_ends]
    c0 --> s0 : queue_scan.10 [waiter_queued]
    c0 --> s1 : queue_scan.11 [else]
```

| Id | Source | Event | Guard | Action | Target | Kind | Cite |
| --- | --- | --- | --- | --- | --- | --- | --- |
| queue_scan.01 | idle | scan |  | A scan with no pass running begins one. | pass | external | DL-50, DL-247, DL-372 |
| queue_scan.02 | no_request | scan |  | A scan asked inside a pass sends nothing and asks a full pass of an admit-only one. | full_requested | external | DL-50, DL-247, DL-372 |
| queue_scan.03 | full_requested | scan |  | A full pass is asked already. |  | internal | DL-50, DL-247, DL-372 |
| queue_scan.04 | next_waiter | (completion) |  | The pass's first turn. | turn_choice | external | DL-50, DL-247, DL-372 |
| queue_scan.05 | awaiting_readmit | (completion) | waiter_moved |  | done | external | DL-50, DL-247, DL-372 |
| queue_scan.06 | awaiting_readmit | (completion) | waiter_stayed |  | turn_choice | external | DL-50, DL-247, DL-372 |
| queue_scan.07 | done | (completion) | pass_upgrades | upgrades | pass | external | DL-50, DL-247, DL-372 |
| queue_scan.08 | done | (completion) | pass_restarts |  | pass | external | DL-50, DL-247, DL-372 |
| queue_scan.09 | done | (completion) | scan_ends |  | idle | external | DL-50, DL-247, DL-372 |

| Id | Choice | Guard | Action | Target |
| --- | --- | --- | --- | --- |
| queue_scan.10 | turn_choice | waiter_queued |  | awaiting_readmit |
| queue_scan.11 | turn_choice | else |  | done |

| State | In | Entry | Exit | Defers | Waits |
| --- | --- | --- | --- | --- | --- |
| queue_scan |  |  |  |  |  |
| idle | queue_scan/scan |  |  |  |  |
| pass | queue_scan/scan | begins_pass |  |  |  |
| next_waiter | pass/turn |  |  |  |  |
| awaiting_readmit | pass/turn | readmits |  |  |  |
| done | pass/turn |  |  |  |  |
| no_request | pass/request |  |  |  |  |
| full_requested | pass/request |  |  |  |  |

| Name | Kind | Prose |
| --- | --- | --- |
| waiter_queued | guard | A waiter of the pass's list from its turn on is QUE_WAIT now: it takes the next turn. |
| waiter_moved | guard | The turn's readmission reported that it admitted, cancelled or dequeued its waiter. |
| waiter_stayed | guard | The turn's readmission left its waiter queued. |
| pass_upgrades | guard | An admit-only scan was asked for a full one inside the pass: a full pass follows, whether or not the pass ended at a move (DL-247). |
| pass_restarts | guard | The pass ended at a moved waiter and no upgrade is owed: a pass of the same mode follows. |
| scan_ends | guard | The pass ended with no waiter moved and no upgrade owed: the scan ends. |
| begins_pass | action | `pass`'s entry: the pass reads the waiters' order, from the first turn, and reports the bookkeeping the record gives (the owed scan's mark cleared, the latch cleared). |
| readmits | action | `awaiting_readmit`'s entry: the turn `turn_choice` found, its waiter's `readmit` sent. |
| upgrades | action | The admit-only scan becomes a full one before its next pass begins (DL-247). |

## runtime_assembly

Clock: logical.

```mermaid
stateDiagram-v2
    state "runtime_assembly" as s9
    state s9 {
        [*] --> s11
        state "unbuilt" as s11
        state s11 {
            [*] --> s2
            state "fresh" as s2
            state "installed" as s6
            state "genesis" as s3
        }
        state "open" as s8
        state s8 {
            [*] --> s4
            state "genesis_input" as s4
            state "input" as s5
        }
        state "built" as s0
        state s0 {
            [*] --> s1
            state "constructed" as s1
            state "seeded" as s10
            state "live" as s7
        }
    }
    s2 --> s6 : runtime_assembly.01 install
    s4 --> s3 : runtime_assembly.03 commit_input
    s11 --> s1 : runtime_assembly.04 finish_genesis
    s2 --> s10 : runtime_assembly.05 seed_period
    s5 --> s7 : runtime_assembly.07 commit_input
    s6 --> s10 : runtime_assembly.12 seed_period
    s1 --> s10 : runtime_assembly.13 seed_period
    s11 --> s4 : runtime_assembly.41 begin_input
    s0 --> s5 : runtime_assembly.42 begin_input
```

| Id | Source | Event | Guard | Action | Target | Kind | Cite |
| --- | --- | --- | --- | --- | --- | --- | --- |
| runtime_assembly.01 | fresh | install |  | Nothing is installed, committed or seeded. Carried rows land verbatim, revisions included. | installed | external | period-model ss7 |
| runtime_assembly.03 | genesis_input | commit_input |  | One revision per changed entity. | genesis | external | DL-87 |
| runtime_assembly.04 | unbuilt | finish_genesis |  | Construction ends. The genesis seed is not an input to the seed latch. | constructed | external | DL-132 |
| runtime_assembly.05 | fresh | seed_period |  | Set the period id. A period below one is the verb's own check. | seeded | external | period-model ss3.5, DL-132 |
| runtime_assembly.07 | input | commit_input |  | Check capacity. One revision per changed entity. | live | external | DL-87, DL-120 |
| runtime_assembly.12 | installed | seed_period |  | As runtime_assembly.05. | seeded | external | period-model ss3.5, DL-132 |
| runtime_assembly.13 | constructed | seed_period |  | As runtime_assembly.05. | seeded | external | period-model ss3.5, DL-132 |
| runtime_assembly.41 | unbuilt | begin_input |  | Construction is not finished. Open the genesis seed's input. | genesis_input | external | DL-87 |
| runtime_assembly.42 | built | begin_input |  | Snapshot on first touch. | input | external | DL-87, concurrency-model ss3 |
| runtime_assembly.16 | installed | install |  | Carried rows are assembly's first act: only a fresh state installs them. |  | refusal install_on_used_state | period-model ss7 |
| runtime_assembly.18 | genesis | install |  | Carried rows are assembly's first act: only a fresh state installs them. |  | refusal install_on_used_state | period-model ss7 |
| runtime_assembly.31 | constructed | finish_genesis |  | Construction ends once: a second call would launder a used state back to fresh. |  | refusal construction_finished | DL-132 |
| runtime_assembly.32 | seeded | finish_genesis |  | Construction comes before the seed. |  | refusal construction_after_seed | DL-132 |
| runtime_assembly.33 | genesis_input | finish_genesis |  | Construction ends after the genesis input commits. |  | refusal inside_genesis_input | DL-87, DL-293 |
| runtime_assembly.34 | input | finish_genesis |  | Construction ends once: a second call would launder a used state back to fresh. |  | refusal construction_finished | DL-132 |
| runtime_assembly.35 | live | finish_genesis |  | Construction ends once: a second call would launder a used state back to fresh. |  | refusal construction_finished | DL-132 |
| runtime_assembly.37 | genesis | seed_period |  | Seeding is assembly's first act: a live state never moves its period. |  | refusal seed_on_used_state | period-model ss3.5, DL-132 |
| runtime_assembly.38 | seeded | seed_period |  | Seeding is assembly's first act: a live state never moves its period. |  | refusal seed_on_used_state | period-model ss3.5, DL-132 |
| runtime_assembly.40 | live | seed_period |  | Seeding is assembly's first act: a live state never moves its period. |  | refusal seed_on_used_state | period-model ss3.5, DL-132 |
| runtime_assembly.43 | open | install |  | Carried rows are assembly's first act: only a fresh state installs them. |  | refusal install_on_used_state | period-model ss7 |
| runtime_assembly.44 | built | install |  | Carried rows are assembly's first act: only a fresh state installs them. |  | refusal install_on_used_state | period-model ss7 |
| runtime_assembly.45 | unbuilt | commit_input |  | A commit needs an input open. |  | refusal no_input_open | DL-87, DL-293 |
| runtime_assembly.46 | built | commit_input |  | A commit needs an input open. |  | refusal no_input_open | DL-87, DL-293 |
| runtime_assembly.47 | open | begin_input |  | Inputs do not nest. |  | refusal input_already_open | DL-87 |
| runtime_assembly.48 | open | seed_period |  | Assembly precedes inputs. |  | refusal seed_inside_input | DL-132 |

| State | In | Entry | Exit | Defers | Waits |
| --- | --- | --- | --- | --- | --- |
| runtime_assembly |  |  |  |  |  |
| unbuilt | runtime_assembly/phase |  |  |  |  |
| fresh | unbuilt/construction |  |  |  |  |
| installed | unbuilt/construction |  |  |  |  |
| genesis | unbuilt/construction |  |  |  |  |
| open | runtime_assembly/phase |  |  |  |  |
| genesis_input | open/inputs |  |  |  |  |
| input | open/inputs |  |  |  |  |
| built | runtime_assembly/phase |  |  |  |  |
| constructed | built/service |  |  |  |  |
| seeded | built/service |  |  |  |  |
| live | built/service |  |  |  |  |

## anchor_head

Clock: logical.

```mermaid
stateDiagram-v2
    state "anchor_head" as s0
    state s0 {
        [*] --> s3
        state "open" as s3
        state "closed" as s2
        state "claimed" as s1
    }
    s3 --> s2 : anchor_head.02 close_period
    s2 --> s1 : anchor_head.03 claim_successor
    s1 --> s3 : anchor_head.04 open_claimed
    s1 --> s2 : anchor_head.05 reclaim
```

| Id | Source | Event | Guard | Action | Target | Kind | Cite |
| --- | --- | --- | --- | --- | --- | --- | --- |
| anchor_head.02 | open | close_period |  | The head is open at this period (the method's check). Write it closed at the seal digest, with the period's row in the same write. | closed | external | period-model ss1.3, ss3 |
| anchor_head.03 | closed | claim_successor |  | The head is closed at this seal digest (the method's check). Write the claim file first, then the head. | claimed | external | period-model ss1.3 |
| anchor_head.04 | claimed | open_claimed |  | The head names this claim (the method's check). Write the successor's durable registry row in the same write. | open | external | period-model ss1.3, PR-02c |
| anchor_head.05 | claimed | reclaim |  | The claim file binds its body, the estate and the head, and the registry holds the previous seal as committed (the method's checks). Append a Reclaimed entry and return the head to the previous seal. | closed | external | period-model ss1.3 |
| anchor_head.06 | claimed | claim_successor |  | The head names this claim and the claim file is gone (the method's checks). Write the claim file again, the head is written unchanged. DL-290 leaves open whether this is reachable: `reclaim` and the resume step refuse the same state as unreachable. |  | internal | period-model ss1.3, DL-290 |
| anchor_head.07 | anchor_head | create_open |  | The method answers its own interrupted genesis before any post. |  | refusal anchor_exists | period-model ss1.1, PR-01b |
| anchor_head.08 | anchor_head | close_period |  | The method answers a repeat at this period's closed head before any post. |  | refusal head_not_open | period-model ss1.3 |
| anchor_head.09 | anchor_head | claim_successor |  |  |  | refusal head_not_closed | period-model ss1.3 |
| anchor_head.10 | anchor_head | open_claimed |  | The method answers a repeat at this period's open head before any post. |  | refusal head_not_claimed | period-model ss1.3 |
| anchor_head.11 | anchor_head | reclaim |  |  |  | refusal head_not_claimed | period-model ss1.3 |

| State | In | Entry | Exit | Defers | Waits |
| --- | --- | --- | --- | --- | --- |
| anchor_head |  |  |  |  |  |
| open | anchor_head/head |  |  |  |  |
| closed | anchor_head/head |  |  |  |  |
| claimed | anchor_head/head |  |  |  |  |

## period_row

Clock: logical.

```mermaid
stateDiagram-v2
    state "period_row" as s2
    state s2 {
        [*] --> s4
        state "unattested" as s4
        state s4 {
            [*] --> s3
            state "provisional" as s3
            state "durable" as s1
        }
        state "attested" as s0
        s0 --> [*]
    }
    s4 --> s1 : period_row.02 close_period
    s3 --> s1 : period_row.04 finalize
    s1 --> s0 : period_row.05 attest
```

| Id | Source | Event | Guard | Action | Target | Kind | Cite |
| --- | --- | --- | --- | --- | --- | --- | --- |
| period_row.02 | unattested | close_period |  | The head closes this period. Record the seal digest and mark the segment durable. `provisional` is a tolerated source, not reachable after a crash: the row is present and finalized before any run. With no row the close writes one, durable, and no instance existed to move. | durable | external | period-model ss1.3, ss3 |
| period_row.04 | provisional | finalize |  | The segment has landed. Mark it durable. | durable | external | period-model ss1.3 |
| period_row.05 | durable | attest |  | The row's seal digest and root match the audit (the method's checks). Mark the row attested. | attested | external | period-model ss1.3, ss11 |
| period_row.06 | period_row | create_open |  | The head refuses first: genesis meets an existing anchor. |  | refusal row_exists | period-model ss1.3, PR-02c |
| period_row.07 | period_row | open_claimed |  | A successor's row is written only in the write that opens it. |  | refusal row_exists | period-model ss1.3, PR-02c |
| period_row.08 | period_row | close_period |  | Only `attested` reaches this row. |  | refusal row_attested | period-model ss1.3 |
| period_row.09 | durable | finalize |  | The method answers this repeat before any post. |  | refusal row_durable | period-model ss1.3 |
| period_row.10 | period_row | finalize |  | Only `attested` reaches this row. The method answers a durable row before any post. It meets a forged row only: attested without its durable flag. |  | refusal row_attested | period-model ss1.3 |
| period_row.11 | provisional | attest |  |  |  | refusal row_not_durable | period-model ss1.3 |
| period_row.12 | period_row | attest |  | Only `attested` reaches this row. The method answers a repeat before any post. It meets a forged row only: attested without its durable flag. |  | refusal row_attested | period-model ss1.3 |

| State | In | Entry | Exit | Defers | Waits |
| --- | --- | --- | --- | --- | --- |
| period_row |  |  |  |  |  |
| unattested | period_row/row |  |  |  |  |
| provisional | unattested/segment |  |  |  |  |
| durable | unattested/segment |  |  |  |  |
| attested (final) | period_row/row |  |  |  |  |

## supervisor_process

Clock: monotonic. Time events: deadman.

```mermaid
stateDiagram-v2
    state "process" as s9
    state s9 {
        [*] --> s8
        state "open" as s8
        state s8 {
            [*] --> s13
            state "starting" as s13
            state "bound" as s0
            state "up" as s15
            state s15 {
                [*] --> s11
                state "serving" as s11
                state "shutting_down" as s12
                state "maintenance_checking" as s3
                state "maintenance_stopping" as s6
                state "maintenance_uncertain" as s7
                state "maintenance_completing" as s5
            }
        }
        state "ended" as s2
        state s2 {
            [*] --> s14
            state "stopped" as s14
            state "refused" as s10
            state "maintenance_completed" as s4
        }
        state "closed" as s1
        s1 --> [*]
    }
    s13 --> s10 : supervisor_process.01 start [lock_held] / refuse
    s13 --> s10 : supervisor_process.02 start [owner_live] / refuse
    s13 --> s0 : supervisor_process.03 start [free]
    s0 --> s11 : supervisor_process.04 publish
    s11 --> s12 : supervisor_process.05 SHUTDOWN [credentials]
    s11 --> s12 : supervisor_process.06 term / drain
    s12 --> s14 : supervisor_process.07 drained
    s11 --> s14 : supervisor_process.08 deadman
    s15 --> s1 : supervisor_process.31 teardown / unlink_published
    s14 --> s1 : supervisor_process.32 teardown / unlink_published
    s0 --> s1 : supervisor_process.33 teardown / unlink_private
    s13 --> s1 : supervisor_process.34 teardown
    s10 --> s1 : supervisor_process.35 teardown
    s11 --> s3 : supervisor_process.37 MAINTENANCE_STOP [credentials]
    s3 --> s11 : supervisor_process.40 maintenance_refused
    s3 --> s6 : supervisor_process.41 maintenance_stopping
    s3 --> s5 : supervisor_process.42 maintenance_idle
    s6 --> s5 : supervisor_process.43 maintenance_idle
    s6 --> s7 : supervisor_process.44 maintenance_uncertain
    s5 --> s7 : supervisor_process.45 maintenance_uncertain
    s5 --> s4 : supervisor_process.46 maintenance_finished
    s4 --> s1 : supervisor_process.47 teardown
    s7 --> s12 : supervisor_process.48 term / drain
    s7 --> s14 : supervisor_process.49 deadman
```

| Id | Source | Event | Guard | Action | Target | Kind | Cite |
| --- | --- | --- | --- | --- | --- | --- | --- |
| supervisor_process.01 | starting | start | lock_held | refuse | refused | external | supervisor-protocol ss5, DL-210 |
| supervisor_process.02 | starting | start | owner_live | refuse | refused | external | supervisor-protocol ss5, DL-210 |
| supervisor_process.03 | starting | start | free |  | bound | external | supervisor-protocol ss5, DL-210 |
| supervisor_process.04 | bound | publish |  | The private socket listens with mode 0600, supervisor.pid is written, and the socket is renamed to supervisor.sock. | serving | external | supervisor-protocol ss5, DL-210, DL-275 |
| supervisor_process.05 | serving | SHUTDOWN | credentials |  | shutting_down | external | supervisor-protocol ss5 SHUTDOWN, DL-80 |
| supervisor_process.06 | serving | term |  | drain | shutting_down | external | supervisor-protocol ss5 SHUTDOWN, DL-275, DL-300 |
| supervisor_process.07 | shutting_down | drained |  | The shutdown's wait ended: every wrapper reaped, or the last-resort bound passed and every survivor's group was killed. | stopped | external | supervisor-protocol ss5 SHUTDOWN, DL-48, DL-150 |
| supervisor_process.08 | serving | deadman |  | A deadman is set and no live leaseholder for that many seconds, the supervisor logs the reason first. | stopped | external | supervisor-protocol ss5 The deadman, DL-95 |
| supervisor_process.09 | stopped | term |  | A signal latched during a shutdown that already ran in this loop pass. Nothing runs. |  | internal | supervisor-protocol ss5 SHUTDOWN |
| supervisor_process.12 | shutting_down | pass_ended |  | retry |  | internal | supervisor-protocol ss5 SHUTDOWN, DL-300 |
| supervisor_process.13 | serving | SPAWN | credentials |  |  | internal | supervisor-protocol ss5 mutating verbs, DL-80 |
| supervisor_process.14 | serving | SIGNAL | credentials |  |  | internal | supervisor-protocol ss5 mutating verbs, DL-80 |
| supervisor_process.15 | serving | SPAWN | foreign |  |  | refusal wrong_incarnation | supervisor-protocol ss5 mutating verbs, DL-80, DL-229 |
| supervisor_process.16 | serving | SPAWN | stale |  |  | refusal stale_token | supervisor-protocol ss5 mutating verbs, DL-80, DL-229 |
| supervisor_process.17 | serving | SIGNAL | foreign |  |  | refusal wrong_incarnation | supervisor-protocol ss5 mutating verbs, DL-80, DL-229 |
| supervisor_process.18 | serving | SIGNAL | stale |  |  | refusal stale_token | supervisor-protocol ss5 mutating verbs, DL-80, DL-229 |
| supervisor_process.19 | serving | SHUTDOWN | foreign |  |  | refusal wrong_incarnation | supervisor-protocol ss5 mutating verbs, DL-80, DL-229 |
| supervisor_process.20 | serving | SHUTDOWN | stale |  |  | refusal stale_token | supervisor-protocol ss5 mutating verbs, DL-80, DL-229 |
| supervisor_process.31 | up | teardown |  | unlink_published | closed | external | supervisor-protocol ss5, DL-210 |
| supervisor_process.32 | stopped | teardown |  | unlink_published | closed | external | supervisor-protocol ss5, DL-210 |
| supervisor_process.33 | bound | teardown |  | unlink_private | closed | external | supervisor-protocol ss5, DL-210 |
| supervisor_process.34 | starting | teardown |  | An error ended startup before the bind: teardown closes the lock if it was taken and unlinks nothing, main exits 1, or 7 for a statechart error. | closed | external | supervisor-protocol ss5, DL-210 |
| supervisor_process.35 | refused | teardown |  | Another supervisor owns the root: teardown closes the lock if it was taken and unlinks nothing, main exits 1. | closed | external | supervisor-protocol ss5, DL-210 |
| supervisor_process.36 | shutting_down | term |  | retry |  | internal | supervisor-protocol ss5 SHUTDOWN, DL-300 |
| supervisor_process.37 | serving | MAINTENANCE_STOP | credentials |  | maintenance_checking | external | supervisor-protocol ss5, DL-390 |
| supervisor_process.38 | serving | MAINTENANCE_STOP | foreign |  |  | refusal wrong_incarnation | supervisor-protocol ss5, DL-390 |
| supervisor_process.39 | serving | MAINTENANCE_STOP | stale |  |  | refusal stale_token | supervisor-protocol ss5, DL-390 |
| supervisor_process.40 | maintenance_checking | maintenance_refused |  |  | serving | external | supervisor-protocol ss5, DL-390 |
| supervisor_process.41 | maintenance_checking | maintenance_stopping |  |  | maintenance_stopping | external | supervisor-protocol ss5, DL-390 |
| supervisor_process.42 | maintenance_checking | maintenance_idle |  |  | maintenance_completing | external | supervisor-protocol ss5, DL-390 |
| supervisor_process.43 | maintenance_stopping | maintenance_idle |  |  | maintenance_completing | external | supervisor-protocol ss5, DL-390 |
| supervisor_process.44 | maintenance_stopping | maintenance_uncertain |  |  | maintenance_uncertain | external | supervisor-protocol ss5, DL-390 |
| supervisor_process.45 | maintenance_completing | maintenance_uncertain |  |  | maintenance_uncertain | external | supervisor-protocol ss5, DL-390 |
| supervisor_process.46 | maintenance_completing | maintenance_finished |  |  | maintenance_completed | external | supervisor-protocol ss5, DL-390 |
| supervisor_process.47 | maintenance_completed | teardown |  |  | closed | external | supervisor-protocol ss5, DL-390 |
| supervisor_process.48 | maintenance_uncertain | term |  | drain | shutting_down | external | supervisor-protocol ss5, DL-390 |
| supervisor_process.49 | maintenance_uncertain | deadman |  |  | stopped | external | supervisor-protocol ss5, DL-390 |
| supervisor_process.50 | maintenance_completed | term |  |  |  | internal | supervisor-protocol ss5, DL-390 |
| supervisor_process.51 | maintenance_uncertain | SPAWN |  |  |  | refusal maintenance_uncertain | supervisor-protocol ss5, DL-390 |
| supervisor_process.52 | maintenance_uncertain | SIGNAL |  |  |  | refusal maintenance_uncertain | supervisor-protocol ss5, DL-390 |
| supervisor_process.53 | maintenance_uncertain | SHUTDOWN |  |  |  | refusal maintenance_uncertain | supervisor-protocol ss5, DL-390 |
| supervisor_process.54 | maintenance_uncertain | MAINTENANCE_STOP |  |  |  | refusal maintenance_uncertain | supervisor-protocol ss5, DL-390 |

| State | In | Entry | Exit | Defers | Waits |
| --- | --- | --- | --- | --- | --- |
| process |  |  |  |  |  |
| open | process/phase |  |  |  |  |
| starting | open/stage |  |  |  |  |
| bound | open/stage |  |  |  |  |
| up | open/stage |  |  |  |  |
| serving | up/service |  |  |  |  |
| shutting_down | up/service |  |  |  |  |
| maintenance_checking | up/service |  |  |  |  |
| maintenance_stopping | up/service |  |  |  |  |
| maintenance_uncertain | up/service |  |  |  |  |
| maintenance_completing | up/service |  |  |  |  |
| ended | process/phase |  |  |  |  |
| stopped | ended/outcome |  |  |  |  |
| refused | ended/outcome |  |  |  |  |
| maintenance_completed | ended/outcome |  |  |  |  |
| closed (final) | process/phase |  |  |  |  |

| Name | Kind | Prose |
| --- | --- | --- |
| lock_held | guard | supervisor.lock is held: another supervisor owns this root (DL-210). |
| owner_live | guard | The lock is taken, and the published socket answers PING within one of three attempts across a second or the pid record does not prove its owner absent (DL-210). |
| free | guard | The lock is taken and no other owner is proven present: private sockets are swept, the published path is reclaimed and the private socket is bound (DL-210). |
| credentials | guard | This incarnation and the current token of an unexpired lease, an integer on the wire (DL-151). Any connection may present them. The one fence of RENEW, RELEASE, SPAWN, SIGNAL and SHUTDOWN (ss5). |
| foreign | guard | The request names no incarnation or another one: the supervisor it knew is gone, so it must re-acquire and reconcile (DL-80). |
| stale | guard | This incarnation without credentials: the token is stale or the lease is not held, so the request is refused stale_token (ss5). With `foreign` and `credentials` this partitions every request. |
| refuse | action | Exit 1: another supervisor owns this root, teardown releases only what this process took. |
| drain | action | Run the shutdown's wait: up to 5 s for missing spawn records, TERM each live command group, KILL each after its grace, and KILL every survivor after the longest grace plus 2 s. Lifelines stay open until each wrapper exits (DL-48, DL-150). |
| retry | action | An error ended the SHUTDOWN's wait: send the queued replies first, that SHUTDOWN's `internal:` answer among them, then run the wait once more (DL-300). |
| unlink_published | action | This incarnation published supervisor.sock. Teardown sends the queued replies for up to 2 s, drops every connection and closes the listener, it unlinks supervisor.sock if its inode is still the one renamed into place and supervisor.pid if it names this incarnation, then closes the open lifelines and the lock. From `up` an error ended the loop, and main exits 1, or 7 for a statechart error (DL-210). |
| unlink_private | action | This incarnation bound its private socket and an error ended startup before the publish. Teardown closes the listener, unlinks supervisor.pid if it names this incarnation and the private socket, then closes the lock, main exits 1, or 7 for a statechart error (DL-210). |

## supervisor_lease

Clock: monotonic. Time events: expire.

```mermaid
stateDiagram-v2
    state "lease" as s3
    state s3 {
        [*] --> s6
        state "unheld" as s6
        state s6 {
            [*] --> s1
            state "free" as s1
            state "expired" as s0
        }
        state "held" as s2
        state s2 {
            [*] --> s4
            state "live" as s4
            state "orphaned" as s5
        }
    }
    s6 --> s4 : supervisor_lease.01 ACQUIRE [grantable] / grant
    s5 --> s4 : supervisor_lease.02 ACQUIRE [grantable] / grant
    s2 --> s1 : supervisor_lease.11 RELEASE [credentials] / release
    s4 --> s5 : supervisor_lease.14 disconnect [holder] / forget
    s2 --> s0 : supervisor_lease.17 expire
```

| Id | Source | Event | Guard | Action | Target | Kind | Cite |
| --- | --- | --- | --- | --- | --- | --- | --- |
| supervisor_lease.01 | unheld | ACQUIRE | grantable | grant | live | external | supervisor-protocol ss5 lease verbs, DL-79, DL-150 |
| supervisor_lease.02 | orphaned | ACQUIRE | grantable | grant | live | external | supervisor-protocol ss5 lease verbs, DL-79, DL-150 |
| supervisor_lease.03 | live | ACQUIRE | incumbent | grant |  | internal | supervisor-protocol ss5 lease verbs, DL-79, DL-80 |
| supervisor_lease.04 | live | ACQUIRE | challenger |  |  | refusal lease_held | supervisor-protocol ss5 lease verbs, DL-79 |
| supervisor_lease.05 | lease | ACQUIRE | unnamed |  |  | refusal bad_controller_id | supervisor-protocol ss5 lease verbs, DL-150 |
| supervisor_lease.06 | lease | ACQUIRE | untimed |  |  | refusal internal | supervisor-protocol ss5 lease verbs, DL-300 |
| supervisor_lease.07 | held | RENEW | renewable | extend |  | internal | supervisor-protocol ss5 lease verbs, DL-150 |
| supervisor_lease.08 | held | RENEW | renewal_untimed |  |  | refusal internal | supervisor-protocol ss5 lease verbs, DL-300 |
| supervisor_lease.09 | lease | RENEW | foreign |  |  | refusal wrong_incarnation | supervisor-protocol ss5 incarnation, DL-80, DL-150 |
| supervisor_lease.10 | lease | RENEW | this_incarnation |  |  | refusal stale_token | supervisor-protocol ss5 incarnation, DL-150 |
| supervisor_lease.11 | held | RELEASE | credentials | release | free | external | supervisor-protocol ss5 lease verbs, DL-150 |
| supervisor_lease.12 | lease | RELEASE | foreign |  |  | refusal wrong_incarnation | supervisor-protocol ss5 incarnation, DL-80, DL-150 |
| supervisor_lease.13 | lease | RELEASE | this_incarnation |  |  | refusal stale_token | supervisor-protocol ss5 incarnation, DL-150 |
| supervisor_lease.14 | live | disconnect | holder | forget | orphaned | external | supervisor-protocol ss5 lease verbs, DL-150 |
| supervisor_lease.15 | expired | disconnect | holder | forget |  | internal | supervisor-protocol ss5 lease verbs |
| supervisor_lease.16 | lease | disconnect |  |  |  | ignored | supervisor-protocol ss5 lease verbs |
| supervisor_lease.17 | held | expire |  |  | expired | external | supervisor-protocol ss5 lease verbs |
| supervisor_lease.18 | lease | expire |  |  |  | ignored | supervisor-protocol ss5 lease verbs |

| State | In | Entry | Exit | Defers | Waits |
| --- | --- | --- | --- | --- | --- |
| lease |  |  |  |  |  |
| unheld | lease/phase |  |  |  |  |
| free | unheld/record |  |  |  |  |
| expired | unheld/record |  |  |  |  |
| held | lease/phase |  |  |  | expire |
| live | held/connection |  |  |  |  |
| orphaned | held/connection |  |  |  |  |

| Name | Kind | Prose |
| --- | --- | --- |
| grantable | guard | controller_id is a non-empty string and ttl_s gives a representable expiry. A zero or negative ttl_s is grantable: the lease is due at once and the next sample expires it (ss5). |
| incumbent | guard | Grantable, with this incarnation and the current token: the incumbent re-keys and its old token dies (DL-79, DL-80). controller_id is a label and the token proves incumbency, not authenticity, the same-uid peer-cred gate on accept authenticates (supervisor-protocol ss5). |
| challenger | guard | controller_id is a non-empty string and ttl_s is a number, without the current token: a live lease yields only to its incumbent (DL-79). The expiry is not computed. |
| unnamed | guard | controller_id is not a non-empty string, checked before ttl_s and any lease state (DL-150). |
| untimed | guard | controller_id is a non-empty string and ttl_s gives no representable expiry (not a number, inf, nan, 1e20). Nothing changes, not even the token counter (DL-300). |
| renewable | guard | This incarnation and the current token, and ttl_s gives a representable expiry (DL-150, DL-300). |
| renewal_untimed | guard | This incarnation and the current token, and ttl_s gives no representable expiry, nothing changes (DL-300). |
| foreign | guard | The request names no incarnation or another one: the supervisor it knew is gone, so it must re-acquire and reconcile (DL-80). |
| this_incarnation | guard | The request names this incarnation (DL-80). |
| credentials | guard | This incarnation and the current token of an unexpired lease, an integer on the wire (DL-151). Any connection may present them. The one fence of RENEW, RELEASE, SPAWN, SIGNAL and SHUTDOWN (ss5). |
| holder | guard | The dropped connection is the one that holds the lease. |
| grant | action | Mint the next token and write a new record held on the claimant's connection with the request's deadline. A re-grant to the same controller_id keeps the dropped-push notice (DL-210). Answer the token, expiry and incarnation (DL-80). |
| extend | action | Move the deadline and expires_at to the request's. The connection is not adopted, so an orphaned lease stays orphaned and its pushes keep dropping until an ACQUIRE. |
| release | action | Drop the record and answer ok. |
| forget | action | Forget the holder's connection: pushes drop until the holder re-ACQUIREs, and any controller may ACQUIRE an orphaned lease, so a crashed engine's resume need not wait out the TTL. This is sound because the kernel closes the AF_UNIX fd only when the holder is gone or closes it, a non-local transport would break that (ss5). |

## supervisor_client

Clock: logical.

```mermaid
stateDiagram-v2
    state "client" as s1
    state s1 {
        [*] --> s0
        state "alive" as s0
        state s0 {
            [*] --> s5
            state "down" as s5
            state s5 {
                [*] --> s4
                state "disconnected" as s4
                state "lost" as s6
            }
            state "connected" as s3
        }
        state "closed" as s2
        s2 --> [*]
    }
    s5 --> s3 : supervisor_client.01 connect
    s3 --> s6 : supervisor_client.03 eof
    s3 --> s6 : supervisor_client.04 cancel
    s0 --> s2 : supervisor_client.05 close
```

| Id | Source | Event | Guard | Action | Target | Kind | Cite |
| --- | --- | --- | --- | --- | --- | --- | --- |
| supervisor_client.01 | down | connect |  | A new epoch: the tier closes a writer the lost connection kept and cancels a reader still running, installs the new writer, a fresh lost event and its reader, then PINGs. A reconnect re-ACQUIREs when a token is held. | connected | external | supervisor-protocol ss5, DL-48, DL-79 |
| supervisor_client.02 | connected | connect |  | Supersede: the tier closes the live writer and cancels its reader, then installs the new epoch as for row .01. |  | internal | DL-48 |
| supervisor_client.03 | connected | eof |  | The tier has set the epoch's lost event and failed the pending request, the next request reconnects. | lost | external | supervisor-protocol ss5, runner-design ss7, DL-48 |
| supervisor_client.04 | connected | cancel |  | Poisoned: the tier has failed the pending request and closed the writer, since the stream has no correlation ids and the reply in flight could reach the next request. | lost | external | DL-48 |
| supervisor_client.05 | alive | close |  | From disconnected, connected or lost, the tier cancels the renewal, reader and LIST tasks, then closes the writer. | closed | external | DL-48 |
| supervisor_client.06 | lost | cancel |  | Poisoned after the loss: EOF failed the pending request before the cancelled request resumed. The tier closes the writer the loss kept, so nothing more is written to that stream. |  | internal | DL-48 |
| supervisor_client.07 | lost | eof |  | The reader of a poisoned epoch meets the EOF its own poison caused: the epoch is already lost. |  | ignored | statechart-engine ss10 |

| State | In | Entry | Exit | Defers | Waits |
| --- | --- | --- | --- | --- | --- |
| client |  |  |  |  |  |
| alive | client/phase |  |  |  |  |
| down | alive/connection |  |  |  |  |
| disconnected | down/epoch |  |  |  |  |
| lost | down/epoch |  |  |  |  |
| connected | alive/connection |  |  |  |  |
| closed (final) | client/phase |  |  |  |  |

## host

Clock: logical.

```mermaid
stateDiagram-v2
    state "host" as s2
    state s2 {
        [*] --> s5
        state "routed" as s5
        state s5 {
            [*] --> s0
            state "active" as s0
            state "passive" as s3
        }
        state "quarantined" as s4
        state "evicted" as s1
    }
    state c0 <<choice>>
    s0 --> s3 : host.02 drain
    s3 --> s0 : host.03 activate
    s5 --> s4 : host.05 quarantine / remembers
    s4 --> c0 : host.07 reinstate
    s4 --> s1 : host.10 evict / evicts
    s2 --> s1 : host.11 force_evict [not_evicted] / evicts_forced
    s1 --> s0 : host.12 register
    c0 --> s3 : host.23 [remembered_passive]
    c0 --> s0 : host.24 [else]
```

| Id | Source | Event | Guard | Action | Target | Kind | Cite |
| --- | --- | --- | --- | --- | --- | --- | --- |
| host.01 | active | activate |  | none: the row does not move and no revision moves |  | internal | concurrency-model ss8 |
| host.02 | active | drain |  | new effects are held, running work continues | passive | external | concurrency-model ss8, CM-13 |
| host.03 | passive | activate |  | held starts dispatch again | active | external | concurrency-model ss8 |
| host.04 | passive | drain |  | none: the row does not move and no revision moves |  | internal | concurrency-model ss8 |
| host.05 | routed | quarantine |  | remembers | quarantined | external | concurrency-model ss7, ss8, DL-97, DL-291 |
| host.06 | quarantined | quarantine |  | none: repeated unreachability is one fact |  | internal | DL-97 |
| host.07 | quarantined | reinstate |  | The host answered again: put back the state quarantine interrupted, and forget it. | remembered | external | concurrency-model ss8, DL-97 |
| host.25 | routed | reinstate |  | none: nothing to put back |  | internal | concurrency-model ss8, DL-111 |
| host.10 | quarantined | evict |  | evicts | evicted | external | concurrency-model ss8 preconditions 1-3, DL-151 |
| host.11 | host | force_evict | not_evicted | evicts_forced | evicted | external | concurrency-model ss8, DL-111, DL-151 |
| host.12 | evicted | register |  | The relay presents the current generation and has self-fenced. Its held jobs run as new runs with new effect ids. No code posts it. | active | external (spec-only) | concurrency-model ss8 (eviction is fenced on return), period-model ss3.3 |
| host.13 | quarantined | activate |  |  |  | refusal host_quarantined | concurrency-model ss8, concurrency-model ss7 |
| host.14 | quarantined | drain |  |  |  | refusal host_quarantined | concurrency-model ss8, concurrency-model ss7 |
| host.15 | routed | evict |  |  |  | refusal host_not_quarantined | concurrency-model ss8 precondition 1, DL-94 |
| host.16 | evicted | activate |  |  |  | refusal host_evicted | concurrency-model ss8 |
| host.17 | evicted | drain |  |  |  | refusal host_evicted | concurrency-model ss8 |
| host.18 | evicted | quarantine |  |  |  | refusal host_evicted | concurrency-model ss8, CM-12 |
| host.19 | evicted | reinstate |  |  |  | refusal host_evicted | concurrency-model ss8, CM-12 |
| host.20 | evicted | evict |  |  |  | refusal host_already_evicted | concurrency-model ss8 |
| host.21 | evicted | force_evict |  |  |  | refusal host_already_evicted | concurrency-model ss8 |
| host.22 | host | register |  | Re-registration refreshes identity and keeps the state. No tier path posts it: `register_host` on an existing row starts nothing. |  | ignored | concurrency-model ss8, DL-94 |

| Id | Choice | Guard | Action | Target |
| --- | --- | --- | --- | --- |
| host.23 | remembered | remembered_passive |  | passive |
| host.24 | remembered | else |  | active |

| State | In | Entry | Exit | Defers | Waits |
| --- | --- | --- | --- | --- | --- |
| host |  |  |  |  |  |
| routed | host/routing |  |  |  |  |
| active | routed/routing | writes_state |  |  |  |
| passive | routed/routing | writes_state |  |  |  |
| quarantined | host/routing | writes_state |  |  |  |
| evicted | host/routing | writes_state |  |  |  |

| Name | Kind | Prose |
| --- | --- | --- |
| not_evicted | guard | The host is not evicted (ss8: force skips the proof, not the state). At `evicted` host.21 takes the event first (statechart-engine ss3). The guard makes a missing host.21 a gap, not a forced eviction of an evicted host (DL-375). |
| remembered_passive | guard | Quarantine interrupted `passive`: a drained host that answers again stays drained (DL-97). |
| writes_state | action | A leaf's entry reports the row's state, the leaf entered. The initial entry reports nothing: `register_host` writes the row before the instance starts. |
| remembers | action | host.05 reports the state quarantine interrupts, read from the row, which the tier writes only after the dispatch (DL-97). |
| evicts | action | host.10 reports an eviction on the gate's proof of the bound, attributed to nobody (ss8, DL-151). |
| evicts_forced | action | host.11 reports an eviction by force, attributed to the actor the event carries: the input's `claimed_actor`, which the gate requires (ss8, DL-151). |

## admission

Clock: logical.

```mermaid
stateDiagram-v2
    state "admission" as s0
    state s0 {
        [*] --> s1
        state "admitted" as s1
        state "applied" as s2
        state "rejected" as s3
        s2 --> [*]
        s3 --> [*]
    }
    s1 --> s2 : admission.02 decide [applied]
    s1 --> s3 : admission.03 decide [rejected]
```

| Id | Source | Event | Guard | Action | Target | Kind | Cite |
| --- | --- | --- | --- | --- | --- | --- | --- |
| admission.02 | admitted | decide | applied | The decision line, with the outbox entries it implies. | applied | external | concurrency-model ss4 steps 5-7, DL-118 |
| admission.03 | admitted | decide | rejected | The decision line, with its stored code. | rejected | external | concurrency-model ss4, DL-235, DL-272, DL-320 |
| admission.09 | admitted | input | other | Refuse request_id_reused, with no decision to carry. |  | refusal request_id_reused | concurrency-model ss4 step 2, DL-217 |
| admission.10 | admitted | input | same | The index raises an EngineError: a second writer is applying inputs, or steps 5-7 yielded. The engine stops. |  | internal | concurrency-model ss4 step 2 |

| State | In | Entry | Exit | Defers | Waits |
| --- | --- | --- | --- | --- | --- |
| admission |  |  |  |  |  |
| admitted | admission/window |  |  |  |  |
| applied (final) | admission/window |  |  |  |  |
| rejected (final) | admission/window |  |  |  |  |

| Name | Kind | Prose |
| --- | --- | --- |
| applied | guard | The decision is `applied`: the gate passed, or a replayed durable decision says applied. |
| rejected | guard | The decision is `rejected`: a precondition, the stale-completion gate or a host guard rejected the input, the input was quarantined, or a replayed durable decision says rejected. |
| same | guard | The input carries the fingerprint the attempt was admitted under. |
| other | guard | The input carries another fingerprint than the attempt's. |

## effect

Clock: logical.

```mermaid
stateDiagram-v2
    state "effect" as s1
    state s1 {
        [*] --> s3
        state "pending" as s3
        state "applied" as s0
        state "retired" as s4
        state "indeterminate" as s2
        s0 --> [*]
        s4 --> [*]
        s2 --> [*]
    }
    s3 --> s0 : effect.02 resolve [applied]
    s3 --> s4 : effect.03 resolve [retired]
    s3 --> s2 : effect.04 resolve [indeterminate]
```

| Id | Source | Event | Guard | Action | Target | Kind | Cite |
| --- | --- | --- | --- | --- | --- | --- | --- |
| effect.02 | pending | resolve | applied | Dispatched, or resume found the run on the host, or a kill landed, or a replayed effect_result, or a carried execution. | applied | external | concurrency-model ss5, DL-96, period-model ss3.5 |
| effect.03 | pending | resolve | retired | Superseded at dispatch, or resume finds that the run it would kill has exited. | retired | external | concurrency-model ss5, DL-111, DL-232 |
| effect.04 | pending | resolve | indeterminate | Resume finds no status and no live wrapper for a kill. An exact retry answers outcome_unavailable. | indeterminate | external | concurrency-model ss5, CM-06, DL-111 |

| State | In | Entry | Exit | Defers | Waits |
| --- | --- | --- | --- | --- | --- |
| effect |  |  |  |  |  |
| pending | effect/outcome |  |  |  |  |
| applied (final) | effect/outcome |  |  |  |  |
| retired (final) | effect/outcome |  |  |  |  |
| indeterminate (final) | effect/outcome |  |  |  |  |

| Name | Kind | Prose |
| --- | --- | --- |
| applied | guard | The outcome's state is `applied`. |
| retired | guard | The outcome's state is `retired`. |
| indeterminate | guard | The outcome's state is `indeterminate`. |

## subscription

Clock: logical.

```mermaid
stateDiagram-v2
    state "feed" as s2
    state s2 {
        [*] --> s4
        state "open" as s4
        state s4 {
            [*] --> s0
            state "backfill" as s0
            state "live" as s3
        }
        state "removed" as s5
        state "closed" as s1
        s5 --> [*]
        s1 --> [*]
    }
    s0 --> s3 : subscription.02 backfill_sent
    s4 --> s5 : subscription.03 append [overflows] / drop_backlog
    s4 --> s1 : subscription.04 stream_ends
```

| Id | Source | Event | Guard | Action | Target | Kind | Cite |
| --- | --- | --- | --- | --- | --- | --- | --- |
| subscription.02 | backfill | backfill_sent |  | The handler posts it once the backfill went out without a refusal, with no `since` there is nothing to send. The handler then sends the queued live records past the seam. | live | external | control-protocol ss5, DL-45, DL-135 |
| subscription.03 | open | append | overflows | drop_backlog | removed | external | control-protocol ss5, DL-267 |
| subscription.04 | open | stream_ends |  | The client is gone, a refusal went on the stream, the lineage was lost, or the handler was cancelled. The journal has already taken the feed out of its fan-out. | closed | external | control-protocol ss5, PR-03, period-model ss11 |
| subscription.05 | open | append | fits | enqueue |  | internal | control-protocol ss5, DL-267 |

| State | In | Entry | Exit | Defers | Waits |
| --- | --- | --- | --- | --- | --- |
| feed |  |  |  |  |  |
| open | feed/phase |  |  |  |  |
| backfill | open/phase |  |  |  |  |
| live | open/phase |  |  |  |  |
| removed (final) | feed/phase |  |  |  |  |
| closed (final) | feed/phase |  |  |  |  |

| Name | Kind | Prose |
| --- | --- | --- |
| fits | guard | The record fits: the backlog is empty, or the backlog plus the record stays within the budget. An empty backlog takes one record of any size, so one record larger than the budget delays the feed and does not remove it (DL-267). |
| overflows | guard | The record does not fit: the backlog holds a record, and the backlog plus this one would pass the budget (DL-267). |
| enqueue | action | Queue the record and count its bytes against the budget. Memory only: the queue has no count limit, so this never blocks. |
| drop_backlog | action | Record why, and drop the backlog. The journal then takes the feed out of its fan-out and, once every other feed has the record, tells the owner, which ends the stream (DL-267). |

## seal_boundary

Clock: logical.

```mermaid
stateDiagram-v2
    state "boundary" as s1
    state s1 {
        [*] --> s4
        state "in_flight" as s4
        state s4 {
            [*] --> s5
            state "requested" as s5
            state "frozen" as s3
            state "committing" as s2
        }
        state "sealed" as s6
        state "aborted" as s0
        state "stopped" as s7
        s6 --> [*]
        s0 --> [*]
        s7 --> [*]
    }
    s5 --> s3 : seal_boundary.01 parked
    s3 --> s2 : seal_boundary.02 quiesced
    s2 --> s6 : seal_boundary.03 committed
    s4 --> s0 : seal_boundary.04 exception [reversible]
    s4 --> s7 : seal_boundary.13 exception [stops]
    s4 --> s7 : seal_boundary.06 exception [in_doubt]
```

| Id | Source | Event | Guard | Action | Target | Kind | Cite |
| --- | --- | --- | --- | --- | --- | --- | --- |
| seal_boundary.01 | requested | parked |  | The barrier parked: admission is frozen and FW polls are parked (ss6 step 2). | frozen | external | period-model ss6 step 2, ss8, PR-28c |
| seal_boundary.02 | frozen | quiesced |  | The cutoff is chosen, drained and quiescent, and the supervisor proof holds (ss6 steps 3-7, ss8). `commit_boundary` runs next (step 8). | committing | external | period-model ss6 steps 3-8, PR-27 |
| seal_boundary.03 | committing | committed |  |  | sealed | external | period-model ss7 |
| seal_boundary.04 | in_flight | exception | reversible |  | aborted | external | period-model ss7, PR-28b |
| seal_boundary.13 | in_flight | exception | stops |  | stopped | external | period-model ss7, DL-101, PR-28b |
| seal_boundary.06 | in_flight | exception | in_doubt |  | stopped | external | DL-274 |

| State | In | Entry | Exit | Defers | Waits |
| --- | --- | --- | --- | --- | --- |
| boundary |  |  |  |  |  |
| in_flight | boundary/phase |  |  |  |  |
| requested | in_flight/progress |  |  |  |  |
| frozen | in_flight/progress |  |  |  |  |
| committing | in_flight/progress |  |  |  |  |
| sealed (final) | boundary/phase | answer |  |  |  |
| aborted (final) | boundary/phase | abort |  |  |  |
| stopped (final) | boundary/phase | stop |  |  |  |

| Name | Kind | Prose |
| --- | --- | --- |
| stops | guard | Past the point of no return, or the fence lost. Past it, the `seal` append may have begun, so the outcome is unknown and recovery decides (PR-28d): the engine exits 4. A leader that cannot prove it leads does not reopen admission (DL-101, PR-28b). Either stops with no abort and no note, and takes precedence over DL-274's flags. |
| in_doubt | guard | Not `BoundaryFailStop`, the fence intact, and memory or the WAL in doubt: an attempt admitted during the seal is not fully applied, an engine-made input the drain could not admit would be lost, or a WAL append is unfinished (DL-274). |
| reversible | guard | Not `BoundaryFailStop`, the fence intact, and nothing in doubt: every other exception before the `seal` append. A refusal before the freeze, a phase-2 refusal, the drain's timeout, a late input (`seal_input_after_cutoff`) and a failed sidecar write are among them (PR-28b). |
| answer | action | `sealed`: answer the request with the committed boundary, then raise `PeriodSealed`: the engine exits 3 (PR-30b). |
| abort | action | `aborted`: run `abort_boundary` and fail the request with the exception. The abort clears the sealing flag, reopens control admission, restarts scheduler admission and unparks FW polls. It touches no row, and C1 carries on (PR-28b). |
| stop | action | `stopped`: re-raise without an abort. The request is not answered. In doubt, a note names the first of DL-274's flags that is set. An attempt whose answer was owed is not answered either, and resume repairs the WAL tail and rebuilds from the WAL. Past the point of no return, or with the fence lost, there is no note. |
