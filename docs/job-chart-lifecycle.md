# Job chart lifecycle

This file is generated from `dsl41.job_chart`;
regenerate it with `uv run python scripts/render_job_lifecycle.py`.
It is a view of the `job` chart, not the chart. [state-machines.md](state-machines.md)
lists every row.

One line per transition: `Target = Source + Event [guard] // note`. A line that starts
with `||` is another way into the same target. A source may be a group of states.
Where a state inside a group source declares its own row on the same event, that row
comes first, and it is among the folded rows.

## Lifecycle

```text
INACTIVE <- initial
INACTIVE = lifecycle + STATUS [injects; injected_inactive]
        || lifecycle + STATUS [injects_inactive_box]
        || lifecycle + STATUS [injects_inactive_job]
        || lifecycle + box set INACTIVE
        || lifecycle + noexec moved [noexec_settles_box]
        || TERMINAL + box start
        || TERMINAL + start [start_skips]
        || TERMINAL + start [start_skips_member]
        || FAILED + noexec moved [noexec_settles_job]
        || QUE_WAIT + ice moved
        || QUE_WAIT + noexec moved [noexec_settles_job]
        || QUE_WAIT + readmit [readmit_cancels]
        || QUE_WAIT + readmit [readmit_leaves_armed]
        || QUE_WAIT + readmit [readmit_leaves_unarmed]
QUE_WAIT = IDLE + start [start_queues]
ADMITTED = lifecycle + start admitted [admitted_admit]
        || lifecycle + start admitted [admitted_on_held]
        || QUE_WAIT + readmit [readmit_admits]
STARTING = lifecycle + STATUS [injects; injected_starting]
        || ADMITTED + (completion) [admitted_by_readmit]
        || ADMITTED + (completion) [admitted_by_start]
RUNNING = lifecycle + STATUS [injects; injected_running]
       || STARTING + started [started_box]
       || STARTING + started [started_job]
overriding = settled + member moves [door_reads_overrides]
folding = settled + member moves [door_folds_only]
       || overriding + (completion) [override_unmet_folds]
settled = overriding + (completion) [override_unmet_settles]
       || folding + (completion) [fold_decides_nothing]
SUCCESS = lifecycle + STATUS [exit_success]
       || lifecycle + STATUS [injects; injected_success]
       || lifecycle + box recompute [derives_success]
       || lifecycle + box recompute [overrides_success]
       || IDLE + start [start_bypasses]
       || overriding + (completion) [override_success]
       || folding + (completion) [fold_success]
FAILURE = lifecycle + STATUS [exit_failure]
       || lifecycle + STATUS [injects; injected_failure]
       || lifecycle + box recompute [derives_failure]
       || lifecycle + box recompute [overrides_failure]
       || overriding + (completion) [override_failure]
       || folding + (completion) [fold_failure]
TERMINATED = lifecycle + STATUS [injects; else]
          || QUE_WAIT + KILLJOB [armed] // The kill drops the arm, then terminates the waiter
          || QUE_WAIT + KILLJOB [unarmed]
          || LIVE + KILLJOB
          || LIVE + box ends FAILURE or TERMINATED [is_terminator]
          || RUNNING + TIMER term_run_time [current_run]
          || RUNNING + member ends FAILURE or TERMINATED [terminator_fires]
```

## Modes

### ice

```text
ice_off <- initial
ice_off = ice_on + FORCE_STARTJOB [force_clears]
       || ice_on + OFF_ICE // no re-evaluation
ice_on = ice_off + ON_ICE [not_live_or_queued]
      || ice_off + ON_ICE [queued]
```

### hold

```text
hold_off <- initial
hold_off = hold_on + FORCE_STARTJOB [force_clears]
        || hold_on + OFF_HOLD [not_queued]
        || hold_on + OFF_HOLD [queued]
        || hold_on + ON_NOEXEC [noexec_applies]
        || hold_on + ON_NOEXEC on its box
hold_on = hold_off + ON_HOLD [not_live_or_queued]
       || hold_off + ON_HOLD [queued]
       || hold_off + STATUS [injects_hold]
       || hold_off + box running [has_auto_hold]
```

### noexec

```text
noexec_off <- initial
noexec_off = noexec_on + OFF_NOEXEC
          || noexec_on + OFF_NOEXEC on its box
noexec_on = noexec_off + ON_NOEXEC [noexec_applies]
         || noexec_off + ON_NOEXEC on its box
```

## Holding

```text
none <- initial
none = reserved + leave STARTING or RUNNING [frees_all]
    || reserved + leave STARTING or RUNNING [spends]
    || held + RELEASE_RESOURCE
    || held + admitted start [takes_over; else]
    || held + first input of a period [left_catalog]
reserved = none + admitted start [has_demand]
        || held + STATUS [injects_live]
        || held + admitted start [on_held_units]
        || held + admitted start [takes_over; has_demand]
held = reserved + leave STARTING or RUNNING [keeps_held]
```

## Folded away

- Ignored rows: 18. The event is consumed and nothing moves.
- Rows marked unreachable: 19. No input reaches the cell.
- Internal rows: 35. The configuration does not move: relays, moments and the arm's writes.
- Self-transitions: 9. A state is left and entered again.

Shown: 70. Folded: 81. Rows in the chart: 151.
