# Energy interval corrections

This example publishes 10 + 20 = 30 for one synthetic UTC interval.
It seals the dsl41 period, resumes the same estate, and publishes 12 + 20 = 32.
The original publication stays available. One adjustment records +2.
A neighboring interval stays at 5 + 7 = 12.

Use the [shared container instructions](../workflows/README.md) to start PostgreSQL
and build the runner. Run fulfilment first when qualifying the environment.
Then run from the repository root:

```sh
docker --context "$WORKFLOW_CONTEXT" compose -f examples/workflows/compose.yaml run --rm runner python examples/energy/demo.py
```

The default waits for the committed control retry horizon before sealing.
This takes about one extra minute. `--force-seal` skips that wait by requesting
the documented override. The seal records it. The option is never implicit.
`--run-dir /runs/energy-chosen` selects a new directory and refuses an existing one.
Each run creates its own PostgreSQL schema. No existing schema is reset.

The visible catalog contains two boxes. Each has ingest, calculate, and publish
jobs. The launcher starts the first box through `sendevent`, waits for its result,
seals through `dsl41 seal`, and opens period 2 through `dsl41 run --resume`.
It then starts the correction box. The engine uses real time and real processes.
The HTTP feed binds only to loopback in the runner container.

## What the run demonstrates

| Obligation | Enforcer and observable evidence |
| --- | --- |
| Incomplete input must not authorize publication | A stable one-row JSON file fails the full manifest's byte count and SHA256. The ingest job fails. Calculate and publish remain at run number zero. No reading or publication reaches PostgreSQL. |
| A failed member can be rerun | The launcher supplies the complete file and uses `FORCE_STARTJOB ENERGY_INITIAL_INGEST`. Its box remains running while the member is failed. Rerunning the member lets the JIL dependency chain complete. |
| Duplicate data changes nothing | Repeated ingest keeps the same readings, snapshots, and row counts. |
| Different values under one meter revision are refused | A separately hashed feed attempts 11 under the accepted revision for 10. The worker refuses the transaction. |
| An old revision cannot roll the current result back | After correction, the launcher replays the original ingest and publication. The current result remains 32. |
| A historical correction crosses a real period | The checker observes publications in periods 1 and 2, distinct baselines, one estate ID, and a successor segment naming the committed seal. |
| Publication and adjustment are one logical effect | One PostgreSQL transaction inserts both. Repeating correction publication adds no row. |

The first incomplete file is replaced before it is accepted. Accepted reading
revisions and snapshots are never updated by these workers. PostgreSQL keys,
content comparisons, and a transaction lock serialize repeated operations.
The manifest cannot lower the completeness requirement: the worker independently
requires exactly `meter_a` and `meter_b`. Calculations read the pinned revisions.
Publication reads the stored calculation and appends an explicit adjustment.
The database owner can still alter tables; this example is not a tamper barrier.

Business identity is a UTC start plus a 900-second duration. A reading has its
own meter revision. A snapshot has a separate revision for that interval.
The scheduler period is neither of those. The publication worker reads the
live control baseline and matches it to one committed period manifest.
The launcher quiesces the workload before the boundary. Concurrent sealing while
a business transaction runs is outside this example's attribution guarantee.

## Inspect the result

The launcher prints its retained run path. It stops the engine and feed before
exiting. `initial-check.json`, `correction-check.json`, and `final-check.json`
contain independent SQL observations. `check.py` imports no worker code and uses
literal expected rows and totals. It checks every reading's duration and revision,
each snapshot's interval and revision, its exact meter membership, and all stored
calculations. It also checks both publications, the adjustment, row counts, and
the neighboring interval.

`incomplete-refusal.json` records the failed job and blocked descendants.
`worker-probes.jsonl` contains duplicate, conflicting, and stale delivery results.
`commands.jsonl` contains control replies and retry pins.
`boundary.json`, `engine/wal/`, `engine/seals/`, and `engine/periods/` retain the
actual period evidence. `run-history.json` records scheduler attempts after stop.
The final checker compares that history with seven real worker invocations in
`attempts/`, using the documented `DSL41_RUN` forensic tag. The seven direct
application probes are counted separately. The tag is evidence, not authority.
The private `profile.sh` contains the database URL. Keep run data outside Git.

With the retained PostgreSQL volume still available, run the independent checker:

```sh
python examples/energy/check.py --run /runs/energy-REPLACE
```

Use `--phase initial` for a run stopped before the correction. A checker failure
is a failed observation. Do not reinterpret it as a successful scheduler run.

## Manual continuity

This is an unqualified procedure. The demo does not pause with an incomplete run
for a manual recovery exercise. Commands against a completed run are duplicate
probes; they do not demonstrate recovery. `--phase corrected` can recheck its
business result without requiring the demo's original attempt count.

Begin only after the launcher and its engine have stopped and their command
workers have exited. If a process was killed outside the launcher's cleanup,
inspect the process list and terminate or wait for surviving workers first.
Keep automatic starts disabled throughout the manual work. The worker's
`--manual` option takes the documented engine leader lock for its invocation,
so a live engine causes refusal. It cannot fence an already orphaned worker.

Read the retained manifests and SQL rows before acting. Publication is keyed by
snapshot. An existing matching publication needs no replacement. A missing row
after a lost PostgreSQL reply can be reconciled by rerunning the same snapshot;
the transaction either committed or rolled back. Conflicting input requires a
new declared revision, never an in-place rewrite of an accepted row.

In a shell in the runner environment, serve the retained feed at its recorded
loopback port:

```sh
R=/runs/energy-REPLACE
PORT=$(python -c 'import json,sys,urllib.parse; print(urllib.parse.urlsplit(json.load(open(sys.argv[1]))["feed_url"]).port)' "$R/config.json")
python -u -m http.server "$PORT" --bind 127.0.0.1 --directory "$R/feed"
```

Wait for the server's startup message. If another process owns that port, stop
and resolve it before proceeding. In another shell, load the database profile
and run the manual operations:

```sh
R=/runs/energy-REPLACE
. "$R/profile.sh"
python examples/energy/worker.py --run "$R" --manual ingest correction
python examples/energy/worker.py --run "$R" --manual calculate correction
python examples/energy/worker.py --run "$R" --manual publish correction
```

Stop the feed with Ctrl-C after the manual sequence.
For the initial wave, use `initial` in all three commands. A manually published
row has null scheduler attribution. The automated checker deliberately rejects
that row as evidence of a dsl41 period crossing. Inspect the business totals
with SQL and retain the manual command log instead.

The proposed handback is retirement: retain this stopped engine lineage and
its schema, then run a fresh example with new identities. Do not resume the old
root after manual business changes and assume its scheduler history recorded
them. Same-lineage handback needs a separate reconciliation procedure and drill.
For a control command with an unknown outcome, stop and inspect the retained
request pins and WAL. Do not issue a newly composed retry automatically.

## Limits and later drills

This is a synthetic integer calculation, not market settlement or billing.
It uses fixed UTC quarter-hours. It does not prove local daylight-saving trigger
counts, repeated-hour completeness, or an industry settlement policy.

An unseen stale snapshot containing old reading revisions needs a separate
refusal drill. The present replay reuses an already accepted snapshot.
Worker death between calculation and publication, database restart, concurrent
corrections, engine loss, detached reattachment, control reply loss, and manual
handback are future drills. Compiler migration and UC emission are also outside
this demonstration. The opt-in integration lane in `examples/workflows/tests`
runs this launcher and its checker; see the Integration lane section of
`../workflows/README.md`.
