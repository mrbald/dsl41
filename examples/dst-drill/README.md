# DST drill

This drill runs the real runner on a fake clock that runs 60 times as fast.
It crosses the DST changes of Europe/London and America/New_York, and a local midnight.
`dsl41 run` starts its engine, wrappers and real commands, as in production.
The clock comes from libfaketime in a Linux container.
The verdict compares each run with `dsl41 rehearse` over the same window.
The rehearsal plays the same estate on a virtual clock and is the reference.

The rehearsal runs the same zone arithmetic, and the runner reads only UTC,
so the container needs no TZ. `dsl41 rehearse` alone cannot test these parts:

- the engine waking for each tick in real sleep slices, and launching on time;
- real wrappers launching real commands;
- a real wrapper killing a real process by `term_run_time`;
- a stop, a resume and a live seal on one timeline.

## Files

| File | What it is |
| --- | --- |
| `estate.jil` | An invented estate. Its times sit in and around the gap and overlap hours, and around midnight. |
| `ft.sh` | Runs a command on a fake clock: `ft.sh 2026-03-29T00:20:00 60 dsl41 run ...`. |
| `drill.py` | The driver. It runs each scenario and prints a short table. |
| `compare.py` | The verdict: pure functions over traces, journals and lint output. |
| `Dockerfile` | dsl41, libfaketime and the drill on Python 3.12. |

The estate has start_times inside and around both zones' change hours, two
start_mins jobs with a run_window each, one start_mins job without one, a must
time, a `term_run_time` kill and a condition job.
`tests/test_dst_drill.py` tests the comparator and the estate without a container.

## Run it

Build from the repository root and run with an init process:

```sh
docker build -f examples/dst-drill/Dockerfile -t dsl41-dst-drill .
docker run --rm --init dsl41-dst-drill                       # every scenario
docker run --rm --init dsl41-dst-drill python examples/dst-drill/drill.py london-spring
```

`--init` is required.
Without it, PID 1 in the container does not reap orphaned processes.
A killed command then stays a zombie, and a zombie still answers `kill -0`.
`--all-rows` prints every row, not only the differences.
Each run leaves its run root, both traces and the lint output under `/tmp/dst-drill/<scenario>`.
Mount a directory there to keep them, as the workflow does.

The workflow `.github/workflows/dst-drill.yml` runs each scenario as one step.
It runs only when dispatched by hand.
When a step fails, it uploads the work directories as the `dst-drill-evidence` artifact.

## Scenarios

Times are local. Real durations were measured at x60 on a 4-CPU arm64 container.

| Scenario | Window | What it adds | Real time |
| --- | --- | --- | --- |
| `london-spring` | 2026-03-29, 00:20 GMT to 03:25 BST | the clock skips 01:00-01:59 | 127 s |
| `london-fall` | 2026-10-25, 00:20 BST to 03:25 GMT | the clock repeats 01:00-01:59 | 247 s |
| `newyork-spring` | 2026-03-08, 00:20 EST to 03:25 EDT | the clock skips 02:00-02:59 | 127 s |
| `newyork-fall` | 2026-11-01, 00:20 EDT to 03:25 EST | the clock repeats 01:00-01:59 | 247 s |
| `london-midnight-seal` | 2026-07-15 23:40 to 00:52 BST | a live seal at 00:04, `dsl41 audit`, then `dsl41 run --resume` into period 2 at 00:07 | 73 s |
| `london-fall-restart` | as `london-fall` | a clean stop at 01:55 BST, before the change, and `dsl41 run --resume` at 01:05 GMT, after it | 247 s |

## Verdict

The driver reads each run's trace with `dsl41 journal`.
It compares the trace with `dsl41 rehearse` over the same window and zone.
The rehearsal is told that `DD_KILL` and `DD_MUST` run 600 seconds, as their commands do.
Events within 2 minutes of either end of the window are not compared.

| Check | Tolerance |
| --- | --- |
| A start the scheduler made, at its stamped tick | the same second |
| Its `dispatch` record, at the engine's clock reading | 0 to 45 s after the tick |
| The median of those launch lags | 0 to 6 s |
| A start the engine made from another job's status | a lag of 0 to 15 s |
| A must alarm, a `term_run_time` kill, at their stamped deadline | the same second |
| The kill's KILL effect, and the wrapper's status.json | applied; signaled 0 to 6 s after the deadline |
| Each run's end status | equal |
| Each scheduler tick in the two journals | admitted in both, or dropped at resume |

The engine stamps a scheduled start at its computed tick, and a must alarm or
a kill at its computed deadline, not at the moment it reads its clock.
So those rows match whatever the timing, and they check the DST computation.
The timing is checked by the launch rows: the `dispatch` record carries the
engine's clock reading when it launched the wrapper.
A late wake, an overshot sleep slice or a slow launch shows there.

Each bound is a real time times the speed:

- A follow-on start: 0.25 s (15 s at x60). The runs measured at most 4 s.
  A follow-on waits for the real producer command to end, so it cannot
  match to the second.
- A launch: 0.75 s (45 s at x60). The dispatch record is
  taken after the wrapper process starts, so it carries the cost of that
  start. Most launches measured 2 to 4 s. The largest of about 250, with
  three containers running at once, was 28 s.
- The median launch: 0.1 s (6 s at x60). A slow clock or a sleep that
  overshoots makes every launch late and moves the median. A slow launch now
  and then moves only the largest one.
- A kill's signal: 0.1 s (6 s at x60). The runs measured 0.7 to 1.5 s.
  A kill starts no process, so a kill one grace period or one drain late fails.

A tick the rehearsal admits while the real engine was down must be a `drop`
record in the real journal, at the same instant.
The runner drops a missed tick at resume and never fires it late
(`docs/runner-design.md`, E9).
A follow-on of a dropped start does not happen, so it is left out.
The drill compares ticks from the journals, not from the trace.
A tick that finds the job's start already deferred to its run_window prints no trace line.

The driver exits 1 on any difference, or when a step's exit code is not the
expected one: 0 for an engine stop, 3 for the engine at the seal, 0 for the
seal and the audit.

## L023 cross-check

For each DST scenario the driver runs `dsl41 lint --timezone <zone>` on the estate.
It reads each L023 finding's clause for the scenario's change day.
It checks that the real run does what the clause states:

- a start time runs once at the stated instant, in the stated pass, or not at all;
- a must alarm fires at the stated instant, or not at all;
- every start of a run_window job falls in a stated opening, at the stated
  day and pass, and each opening holds one;
- a start_mins tick that moves or runs twice has exactly one start at each stated instant;
- a start_mins tick that runs once in one pass has no start in the other;
- a start_mins tick in a gap, moved or not run, leaves no start between the
  job's last ordinary tick before the change and its first one after it.

A predicted start inside a downtime must be a drop instead.
`tests/test_dst_drill.py` holds, for each kind of claim, a run that passes and a run that fails.
A start_mins finding states the scheduler's ticks, before a run_window defers or skips them.
So it is checked only for jobs without a run_window; the window finding covers the others.

## Limits

- Kernel times stay real: file mtimes written by an unpreloaded tool, `ps`, `/proc`.
- libfaketime does not scale timed thread waits (`Event.wait`, `Lock.acquire`)
  or `setitimer` and `alarm`. dsl41 does not use them today.
- Process start-up costs about 9 fake seconds at x60. The scenarios keep
  every stop, seal and resume away from scheduled times.
- A moved start_mins tick that lands on an ordinary tick merges into it.
  One start there is right, but the run cannot show which of the two ran.
- One restart and one seal are tried. The drill is not a soak.
- Run it as an ordinary user. The image's user is `drill` (uid 1001).
