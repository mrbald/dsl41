# DST drill

For the maintainer checking engine behavior across clock changes; follow the [maintainer track](../../README.md#documents).

This drill runs the real engine on a fake clock that runs 60 times as fast.
It crosses the DST changes of Europe/London and America/New_York, and a local midnight.
One scenario steps the wall clock backward on a clock that runs at the real rate (DL-435).
`dsl41 run` starts its engine, wrappers and real commands, as in production.
The clock comes from libfaketime in a Linux container.
The verdict compares each run with `dsl41 rehearse` over the same window.
The rehearsal plays the same estate on a virtual clock and is the reference.

The rehearsal runs the same zone arithmetic, and the runner reads only UTC,
so the container needs no TZ. `dsl41 rehearse` alone cannot test these parts:

- the engine waking for each tick in real sleep slices, and launching on time;
- real wrappers launching real commands;
- a real wrapper killing a real process by `term_run_time`;
- a clean stop and a resume on one timeline;
- a backward step of the wall clock under a running engine.

## Files

| File | What it is |
| --- | --- |
| `estate.jil` | An invented estate. Its times sit in and around the gap and overlap hours, and around midnight. |
| `estate-step.jil` | The clock-step scenario's estate: a job every minute that runs 25 seconds, and a job that follows it. |
| `ft.sh` | Runs a command on a fake clock: `ft.sh 2026-03-29T00:20:00 60 dsl41 run ...`. |
| `ft-step.sh` | Runs a command on a clock at the real rate whose offset is a file: `ft-step.sh SPEC dsl41 run ...`. Rewriting the file steps the wall clock and leaves the monotonic clock alone. |
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
The mount must allow `chmod` on a socket: a macOS bind mount through a Linux VM does not,
and the engine then fails to bind its control socket.
Copy the directory out with `docker cp` instead.

The workflow `.github/workflows/dst-drill.yml` runs each scenario as one step.
It runs only when dispatched by hand.
When a step fails, it uploads the work directories as the `dst-drill-evidence` artifact.

## Scenarios

Times are local. Real durations were measured on v2, one scenario at a time, on a 4-CPU arm64
container. Six scenarios run at x60. `clock-step` runs at x1.

| Scenario | Window | What it adds | Real time |
| --- | --- | --- | --- |
| `london-spring` | 2026-03-29, 00:20 GMT to 03:25 BST | the clock skips 01:00-01:59 | 127 s |
| `london-fall` | 2026-10-25, 00:20 BST to 03:25 GMT | the clock repeats 01:00-01:59 | 247 s |
| `newyork-spring` | 2026-03-08, 00:20 EST to 03:25 EDT | the clock skips 02:00-02:59 | 127 s |
| `newyork-fall` | 2026-11-01, 00:20 EDT to 03:25 EST | the clock repeats 01:00-01:59 | 247 s |
| `london-midnight` | 2026-07-15 23:40 to 00:52 BST | the scheduler's day rolls over with the engine running: a start at 23:59 and one at 00:01 | 74 s |
| `london-fall-restart` | as `london-fall` | `dsl41 engine stop --wait` at 01:55 BST, before the change, and `dsl41 run --resume` at 01:05 GMT, after it | 247 s |
| `clock-step` | 2026-07-15 00:11:30 to 00:14:15 UTC, at x1 | the wall clock steps back 90 s at 00:12:10, 10 s after a tick (DL-435) | 257 s |

## Verdict

The driver reads each run's trace with `dsl41 journal` and its records from `engine.db`.
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
| The kill's `outcome` record and the wrapper's status in it | an `oracle_kill` stop; signaled 0 to 6 s after the deadline |
| Each run's end status | equal |
| Each scheduler tick in the two journals | admitted in both, or dropped at resume |

The engine stamps a scheduled start at its computed tick, and a must alarm or
a kill at its computed deadline, not at the moment it reads its clock.
So those rows match whatever the timing, and they check the DST computation.
The timing is checked by the launch rows: the `dispatch` record carries the
engine's clock reading when it launched the wrapper.
A late wake, an overshot sleep slice or a slow launch shows there.

Each bound is a real time times the speed. The v2 runs, one scenario at a time at x60, measured:

- A follow-on start: bound 0.25 s (15 s at x60). The runs measured 0 to 5 s.
  A follow-on waits for the real producer command to end, so it cannot
  match to the second.
- A launch: bound 0.75 s (45 s at x60). The dispatch record is
  taken after the wrapper process starts, so it carries the cost of that
  start. The largest launch of each scenario was 1.7 to 5.9 s.
- The median launch: bound 0.1 s (6 s at x60). A slow clock or a sleep that
  overshoots makes every launch late and moves the median. A slow launch now
  and then moves only the largest one.
- A kill's signal: bound 0.1 s (6 s at x60). The five runs with a kill measured 0.7 to 1.9 s.
  A kill starts no process, so a kill one grace period or one drain late fails.
- `clock-step` at x1: the largest launch was 0.1 s.

A tick the rehearsal admits while the real engine was down must be a `drop`
record in the real journal, at the same instant.
The runner drops a missed tick at resume and never fires it late
(`docs/runner-design.md`, E9).
A follow-on of a dropped start does not happen, so it is left out.
The drill compares ticks from the journals, not from the trace.
A tick that finds the job's start already deferred to its run_window prints no trace line.

The driver exits 1 on any difference, or when a step's exit code is not the
expected one: 0 for `dsl41 engine stop --wait` and 0 for the engine after it.
A run's Nth start is its Nth dispatch record: the `dispatch` record names the
run number, and the trace does not.

## The clock step

The `clock-step` scenario runs at the real rate under `ft-step.sh`.
The driver rewrites the file that holds the clock's offset, and libfaketime reads the
new offset at the next clock call of every process.
Only the wall clock steps. The monotonic clock stays real, as after `date -s`.
At x60 libfaketime fakes the monotonic clock with the wall clock, so a step there would move both.
The estate has one job that starts every minute and runs 25 seconds, and one that follows it.
The engine starts at 00:11:30. The 00:12 tick is admitted.
At 00:12:10 the wall steps back 90 seconds, the driver sends `SET_GLOBAL`,
and the run goes on to 00:14:15 on the stepped clock, then stops.
The verdict follows DL-435:

| Check | Expected |
| --- | --- |
| `dsl41: WARNING:` lines on the engine's stderr | one, with `wall` below `frontier` and `gap_s` equal to their difference, at most the step |
| `SET_GLOBAL`, sent under the step | `sendevent` exits 0 with decision `applied`. This exit code is the only proof that none was refused: a refusal writes no record |
| Every decision in the store | `applied`; none `rejected` |
| The engine at the end | still running; `engine stop --wait` exits 0, the engine exits 0, `clean_shutdown` |
| The `input` records' stamps, in store order | none earlier than the one before |
| The trace's lines | none earlier than the one before |
| Each scheduler tick, against the rehearsal | admitted once; none repeated, none lost |
| Each scheduled start, against the rehearsal | the same job and second, as many times; none lost, none extra |
| The run the step lands in (the 00:12 run) | ends `SUCCESS`, and exactly one follow-on starts in that minute, by count and status |
| `dsl41 journal`, `dsl41 rehearse`, `dsl41 lint` | exit 0 in every scenario; an empty trace or a stamp list fails |
| Each scheduled start's `dispatch` record | 0 to 0.75 s after its tick |

Other record kinds (`dispatch`, `outcome`, `engine`) carry the wall reading and are not asserted:
after the step they can be stamped below an earlier `input`.
A follow-on start is checked by count and status, not by time.
A status that arrives while the wall is below the frontier is stamped at the frontier,
so it comes earlier than in the rehearsal.
A completion that the engine held until the wall reached the frontier would carry the same stamps,
so the drill cannot tell it from a correct run.

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
  every stop and resume away from scheduled times.
- A moved start_mins tick that lands on an ordinary tick merges into it.
  One start there is right, but the run cannot show which of the two ran.
- One restart and one step are tried. The drill is not a soak.
- The step is 90 seconds backward. A step far below the frontier, a step forward
  and a step during a restart are not tried.
- The control verbs of `clock-step` run without libfaketime: `engine stop --wait` sleeps with
  Python's `time.sleep`, which libfaketime's `clock_nanosleep` wrapper fails with `EINVAL`
  beside a real monotonic clock. A verb reads no clock.
- Run it as an ordinary user. The image's user is `drill` (uid 1001).
