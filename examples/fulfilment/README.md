# Fulfilment

A fixed synthetic warehouse wave runs under the real dsl41 engine.
PostgreSQL owns inventory. A separate local HTTP process owns carrier receipts
in SQLite. Their commits are separate.

The wave reserves three widgets. Order `SPLIT` needs parcels `P1` and `P2`.
Order `CANCEL` needs `P3` and cancels before packing, releasing one widget.
Packing is the cancellation cutoff. A later request to cancel `SPLIT` is recorded
as refused. Each label dispatches one parcel. Both parcels must dispatch before
the manifest marks the order fulfilled.

## Run

Use the Linux environment in [the shared instructions](../workflows/README.md).
It provides installed dsl41, psycopg 3, and a disposable PostgreSQL database.
Set `DATABASE_URL` to that database. The role needs permission to create a schema.
From the repository root:

```sh
python examples/fulfilment/demo.py --run-dir /runs/fulfilment-drop
python examples/fulfilment/demo.py --no-incident --run-dir /runs/fulfilment-normal
```

Each path must be new. An omitted `--run-dir` creates a unique short path under
`EXAMPLE_RUNS`, default `/tmp/dsl41-examples`. The scripts resolve their own source
paths and can be invoked from another working directory.

The default incident commits the `P2` label and a fault marker at the carrier,
then drops the HTTP connection. The worker exits with failure. Its warehouse
transaction rolls back. The demo observes that failure and checks the split
order is still incomplete. It then sends one explicit `FORCE_STARTJOB` for
`FF_LABEL_2_C`. That worker queries the existing business operation, records its
receipt, and dispatches the parcel. The dependent manifest job then runs.
There is no native retry attribute or worker retry loop.

`--no-incident` runs the ordinary path. `--leave-incident` stops the engine and
carrier after the incident check, retaining the uncertain warehouse operation
for the manual exercise below.

## Evidence

`check.py` imports no worker or carrier code. It queries PostgreSQL and reads the
carrier's HTTP ledger. Expected quantities and identities are fixed independently
of the worker calculations. At the incident boundary it requires one dispatched
parcel, one packed parcel, two external labels, and an unfulfilled split order.
At completion it requires initial stock 3, available 1, reserved 0, dispatched 2,
two labels, and one manifest. It also checks the cancellation decisions and
matching local and external receipt payloads.

Each worker commits a separate attempt row before its business transaction.
After the engine stops, `dsl41 runs --format json` supplies `runs.json`.
The checker compares its execution counts with the attempt rows and exact expected
counts. The incident has two executions of `FF_LABEL_2_C`; every other command
runs once. Database uniqueness alone cannot conceal an extra worker execution.
The demo also attempts conflicting reuse of a carrier identity and requires
HTTP 409. Final checks require the original payload to remain intact.

Retained files include `config.json`, the carrier database and log, the private
worker profile, generated properties, control commands and retry pins, engine
logs/WAL/spools, scheduler status snapshots, independent check results, and
offline run history. `carrier-ready.json` records the endpoint and SQLite version.
The profile contains `DATABASE_URL`. Treat the whole run directory as private.
Do not add these artifacts to this public repository.

The demo stops its processes in `finally`. It retains its unique PostgreSQL
schema and all run files. Recheck a finished run without restarting the carrier:

```sh
R=/runs/fulfilment-drop
. "$R/profile.sh"
python examples/fulfilment/check.py --run "$R" --offline-carrier
```

This reads the actual SQLite receipt ledger in read-only mode. It does not
substitute a cached successful check result. Run the checker without Python `-O`.

## Operator recovery

For a live engine, inspect the failed worker and external receipt before rerunning:

```sh
R=/runs/fulfilment-drop
python -m dsl41 query status --socket "$R/engine/control.sock" --job FF_LABEL_2_C
python examples/fulfilment/check.py --run "$R" --phase incident
python -m dsl41 sendevent FORCE_STARTJOB --socket "$R/engine/control.sock" --job FF_LABEL_2_C
```

These commands apply while the engine and carrier are running. The automatic demo
performs them before it stops. A control command that exits 4 has an unknown
outcome. Preserve its printed request ID, revision, epoch, and baseline with the
original command. An exact retry of that control request is distinct from a new
worker rerun. Do not treat a new request ID as recovery of the old request.

To exercise completion without dsl41, first create a stopped incident:

```sh
python examples/fulfilment/demo.py --leave-incident --run-dir /runs/fulfilment-manual
R=/runs/fulfilment-manual
. "$R/profile.sh"
python examples/fulfilment/check.py --run "$R" --phase incident --offline-carrier
```

The launcher has stopped its tethered engine and waited for it to exit. All worker
executions are terminal. Keep that engine stopped for the rest of this exercise.
Never run manual workers beside an automatic owner. In one terminal, restart only
the carrier on its retained ledger:

```sh
R=/runs/fulfilment-manual
python examples/fulfilment/carrier.py --ledger "$R/carrier.sqlite" --ready "$R/manual-carrier.json"
```

In another terminal, use its newly reported address, preserving the business wave
and schema. Then reconcile the failed operation and close the manifest:

```sh
R=/runs/fulfilment-manual
. "$R/profile.sh"
python - "$R" <<'PY'
import json, sys
from pathlib import Path
run = Path(sys.argv[1])
config = json.loads((run / "config.json").read_text())
config["carrier_url"] = json.loads((run / "manual-carrier.json").read_text())["url"]
(run / "config.json").write_text(json.dumps(config, indent=2) + "\n")
PY
python examples/fulfilment/worker.py --run "$R" --job MANUAL_P2 label P2
python examples/fulfilment/worker.py --run "$R" --job MANUAL_MANIFEST manifest WAVE
python examples/fulfilment/check.py --run "$R" --phase business
```

Stop the carrier with Ctrl-C. `--phase business` checks business completion only.
Manual attempt rows intentionally do not match the earlier scheduler history.
Retain both as separate evidence. Do not rewrite scheduler statuses to claim it
executed the manual work. Hand back at a clean boundary: archive this completed
training wave and launch a fresh wave in a fresh directory and schema. Same-root
resume after manual changes is outside this exercise.

For removal, stop all processes first. Drop only the unique schema named in this
run's `config.json`. Remove the whole stopped training directory, including its
engine anchor, only when its evidence is no longer needed. Do not delete a live
run or remove only the engine's journal and leave its anchor behind.

## Limits and later drills

This slice shows one successful wave and one carrier commit followed by a lost
reply and worker failure. The carrier is a local simulator, not a vendor adapter.
SQLite synchronous commits do not establish power-loss durability on every
filesystem. This is not a throughput or production workload qualification.

The visible JIL catalog owns ordering. Workers enforce inventory conservation,
business revision, cancellation cutoff, and operation identity. A scheduler
success is not independent proof that those business rules held.

Later drills remain unbuilt: competing orders for the last item; an external kill
at the commit boundary; post-dispatch cancellation and a corrective shipment;
supervisor reattachment; a business operation spanning scheduler periods;
repeated waves; hold/release and access-role refusals; UC migration evidence;
and installed-artifact, capacity, and soak qualification. Formal regression tests
and CI integration are a separate slice.
