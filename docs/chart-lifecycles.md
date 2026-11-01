# Chart lifecycles

This file is generated from `dsl41.machines`;
regenerate it with `uv run python scripts/render_job_lifecycle.py`.
It is a view of every registered chart but `job`, not the charts.
[job-chart-lifecycle.md](job-chart-lifecycle.md) is the `job` chart's view, and
[state-machines.md](state-machines.md) lists every row.

One line per transition: `Target = Source + Event [guard] // note`. A line that starts
with `||` is another way into the same target. A source may be a group of states.
A source with the chart's own name is the chart's root. Its row leaves every region
and enters each again, and its line is in the block of its target's top-level region.
`State <- initial` names the state a region's entry rests in.

A chart's section opens with its counts. Five kinds of row are folded into them.
An ignored row consumes its event and moves nothing.
No input reaches a row marked unreachable.
A refusal row refuses its event and moves nothing.
An internal row runs its action and moves nothing.
A self-transition leaves its state and enters it again.
`--full --chart NAME` prints the folded rows.

A top-level region has a block. A composite with two or more regions has a block of
its own under its name, after each region's initial state. A composite with one region
groups states inside its parent's block. The block names its members on one line,
`group = {member, member}`, after the initial states.

## queue_scan

Shown: 8. Folded: 1 (internal 1). Rows in the chart: 9.

### scan

```text
idle <- initial
idle = done + (completion) [scan_ends]
pass = idle + scan // A scan with no pass running begins one
    || done + (completion) [pass_restarts]
    || done + (completion) [pass_upgrades]
```

#### pass

```text
next_waiter <- initial
no_request <- initial
awaiting_readmit = next_waiter + (completion) [waiter_queued] // The pass's first turn
                || awaiting_readmit + (completion) [waiter_stayed; waiter_queued]
done = next_waiter + (completion) [else] // The pass's first turn
    || awaiting_readmit + (completion) [waiter_moved]
    || awaiting_readmit + (completion) [waiter_stayed; else]
full_requested = no_request + scan
```

## runtime_assembly

Shown: 9. Folded: 16 (refusals 16). Rows in the chart: 25.

### phase

```text
fresh <- initial
unbuilt = {fresh, installed, genesis}
open = {genesis_input, input}
built = {constructed, seeded, live}
installed = fresh + install
genesis = genesis_input + commit_input // One revision per changed entity
genesis_input = unbuilt + begin_input // Construction is not finished. Open the genesis seed's input
input = built + begin_input // Snapshot on first touch
constructed = unbuilt + finish_genesis
seeded = fresh + seed_period // Set the period id. A period below one is the verb's own check
      || installed + seed_period
      || constructed + seed_period
live = input + commit_input // Check capacity. One revision per changed entity
```

## admission

Shown: 2. Folded: 2 (refusals 1, internal 1). Rows in the chart: 4.

### window

```text
admitted <- initial
applied = admitted + decide [applied] // The decision line
rejected = admitted + decide [rejected] // The decision line, with its stored code
```

## subscription

Shown: 3. Folded: 1 (internal 1). Rows in the chart: 4.

### phase

```text
backfill <- initial
open = {backfill, live}
live = backfill + backfill_sent
removed = open + append [overflows]
closed = open + stream_ends
```

## engine_lifecycle

Shown: 5. Folded: 1 (internal 1). Rows in the chart: 6.

### phase

```text
STARTING <- initial
RUNNING = STARTING + replay_done // The WAL replay is done and the engine serves
DRAINING = RUNNING + stop_requested
STOPPED = STARTING + stop_requested
       || DRAINING + abort_deadline
       || DRAINING + all_attempts_settled
```

## attempt

Shown: 15. Folded: 2 (ignored 1, internal 1). Rows in the chart: 17.

### phase

```text
SPAWNING <- initial
RUNNING = SPAWNING + spawn_recorded // `spawn.json` was seen
STOPPING = SPAWNING + stop
        || RUNNING + stop
ENDED = SPAWNING + engine_died
     || SPAWNING + exit_recorded
     || SPAWNING + spawn_failed
     || SPAWNING + wrapper_gone
     || RUNNING + engine_died
     || RUNNING + exit_recorded
     || RUNNING + wrapper_gone
     || STOPPING + engine_died
     || STOPPING + exit_recorded
     || STOPPING + record_timeout
     || STOPPING + spawn_failed
     || STOPPING + wrapper_gone
```

## update

Shown: 3. Folded: 1 (internal 1). Rows in the chart: 4.

### phase

```text
IDLE <- initial
STOPPING = IDLE + update_admitted [admitted]
APPLIED = STOPPING + all_stopped_or_none
REFUSED = IDLE + update_admitted [refused]
```
