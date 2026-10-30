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
| admission.02 | admitted | decide | applied | The decision line. | applied | external | concurrency-model ss4 steps 5-7, DL-118 |
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
| rejected | guard | The decision is `rejected`: a precondition or the stale-completion gate rejected the input, the input was quarantined, or a replayed durable decision says rejected. |
| same | guard | The input carries the fingerprint the attempt was admitted under. |
| other | guard | The input carries another fingerprint than the attempt's. |

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

## engine_lifecycle

Clock: logical. Time events: abort_deadline.

```mermaid
stateDiagram-v2
    state "engine" as s4
    state s4 {
        [*] --> s2
        state "STARTING" as s2
        state "RUNNING" as s1
        state "DRAINING" as s0
        state "STOPPED" as s3
        s3 --> [*]
    }
    s2 --> s1 : engine_lifecycle.01 replay_done
    s1 --> s0 : engine_lifecycle.02 stop_requested / begin_drain
    s0 --> s3 : engine_lifecycle.04 all_attempts_settled / clean_shutdown
    s0 --> s3 : engine_lifecycle.05 abort_deadline / unclean_shutdown
    s2 --> s3 : engine_lifecycle.06 stop_requested / shutdown_during_startup
```

| Id | Source | Event | Guard | Action | Target | Kind | Cite |
| --- | --- | --- | --- | --- | --- | --- | --- |
| engine_lifecycle.01 | STARTING | replay_done |  | The WAL replay is done and the engine serves. | RUNNING | external |  |
| engine_lifecycle.02 | RUNNING | stop_requested |  | begin_drain | DRAINING | external |  |
| engine_lifecycle.03 | DRAINING | stop_requested |  | deadline_now |  | internal |  |
| engine_lifecycle.04 | DRAINING | all_attempts_settled |  | clean_shutdown | STOPPED | external |  |
| engine_lifecycle.05 | DRAINING | abort_deadline |  | unclean_shutdown | STOPPED | external |  |
| engine_lifecycle.06 | STARTING | stop_requested |  | shutdown_during_startup | STOPPED | external |  |

| State | In | Entry | Exit | Defers | Waits |
| --- | --- | --- | --- | --- | --- |
| engine |  |  |  |  |  |
| STARTING | engine/phase |  |  |  |  |
| RUNNING | engine/phase |  |  |  |  |
| DRAINING | engine/phase |  |  |  |  |
| STOPPED (final) | engine/phase |  |  |  |  |

| Name | Kind | Prose |
| --- | --- | --- |
| begin_drain | action | Set the deadline to now plus both graces plus five seconds. Journal the draining record, send `stop` to every unsettled attempt, cancel the file watchers and arm the deadline. |
| shutdown_during_startup | action | A stop request during the replay: no attempt is live, so there is nothing to drain. Journal `shutdown_during_startup` and exit 0. |
| deadline_now | action | A second stop request: the deadline is now. The attempts were already told to stop. |
| clean_shutdown | action | Every attempt settled: record `clean_shutdown` and exit 0. |
| unclean_shutdown | action | The deadline passed: record `unclean_shutdown` with the unsettled attempts and exit 0. |

## attempt

Clock: logical. Time events: record_timeout (generation).

```mermaid
stateDiagram-v2
    state "attempt" as s4
    state s4 {
        [*] --> s2
        state "SPAWNING" as s2
        state "RUNNING" as s1
        state "STOPPING" as s3
        state "ENDED" as s0
        s0 --> [*]
    }
    s2 --> s1 : attempt.01 spawn_recorded
    s2 --> s0 : attempt.02 spawn_failed / end_spawn_failed
    s2 --> s3 : attempt.03 stop / stop
    s1 --> s3 : attempt.04 stop / stop
    s1 --> s0 : attempt.05 exit_recorded / end_from_record
    s3 --> s0 : attempt.06 exit_recorded / end_from_record
    s3 --> s0 : attempt.07 record_timeout / end_unobservable
    s3 --> s0 : attempt.10 spawn_failed / end_spawn_failed
    s2 --> s0 : attempt.11 exit_recorded / end_from_record
    s2 --> s0 : attempt.12 wrapper_gone / end_unobservable
    s1 --> s0 : attempt.13 wrapper_gone / end_unobservable
    s3 --> s0 : attempt.14 wrapper_gone / end_unobservable
```

| Id | Source | Event | Guard | Action | Target | Kind | Cite |
| --- | --- | --- | --- | --- | --- | --- | --- |
| attempt.01 | SPAWNING | spawn_recorded |  | `spawn.json` was seen. | RUNNING | external |  |
| attempt.02 | SPAWNING | spawn_failed |  | end_spawn_failed | ENDED | external |  |
| attempt.03 | SPAWNING | stop |  | stop | STOPPING | external |  |
| attempt.04 | RUNNING | stop |  | stop | STOPPING | external |  |
| attempt.05 | RUNNING | exit_recorded |  | end_from_record | ENDED | external |  |
| attempt.06 | STOPPING | exit_recorded |  | end_from_record | ENDED | external |  |
| attempt.07 | STOPPING | record_timeout |  | end_unobservable | ENDED | external |  |
| attempt.08 | STOPPING | stop |  | The ladder already runs, the first cause stands. |  | ignored |  |
| attempt.09 | STOPPING | spawn_recorded |  | stop_attempt |  | internal |  |
| attempt.10 | STOPPING | spawn_failed |  | end_spawn_failed | ENDED | external |  |
| attempt.11 | SPAWNING | exit_recorded |  | end_from_record | ENDED | external |  |
| attempt.12 | SPAWNING | wrapper_gone |  | end_unobservable | ENDED | external |  |
| attempt.13 | RUNNING | wrapper_gone |  | end_unobservable | ENDED | external |  |
| attempt.14 | STOPPING | wrapper_gone |  | end_unobservable | ENDED | external |  |

| State | In | Entry | Exit | Defers | Waits |
| --- | --- | --- | --- | --- | --- |
| attempt |  |  |  |  |  |
| SPAWNING | attempt/phase |  |  |  |  |
| RUNNING | attempt/phase |  |  |  |  |
| STOPPING | attempt/phase |  |  |  |  |
| ENDED (final) | attempt/phase |  |  |  |  |

| Name | Kind | Prose |
| --- | --- | --- |
| stop | action | Take the stop and keep its cause. One `stop_attempt` intent runs the whole ladder or, before the wrapper launched anything, kills the wrapper. The `record_timeout` timer is armed after the KILL, under a new generation. |
| stop_attempt | action | The wrapper launched before the stop landed: run the ladder again, on the recorded pgid, with the cause the attempt already holds. The timer moves to a new generation, so the first ladder's timer is stale. |
| end_spawn_failed | action | The attempt never ran: outcome `spawn_failed`, with the error text. |
| end_from_record | action | The wrapper recorded the command's end: the outcome is derived from the record (`outcome_of`). A command that exited before the TERM is `natural`, a record that names a stop or the lifeline is `stopped`, a wrapper that aborted its own launch is `spawn_failed`. |
| end_unobservable | action | No record: the wrapper is gone, or none came `grace_kill` after the last KILL. Outcome `unobservable`, with the cause of the stop the attempt took, if any. A record that comes later is kept as evidence and does not reopen the attempt. |
