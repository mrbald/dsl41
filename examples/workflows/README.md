# Local workflow examples

Three synthetic businesses run through the public dsl41 CLI:

| Example | Real work | Incident |
| --- | --- | --- |
| [Fulfilment](../fulfilment/README.md) | PostgreSQL inventory and an HTTP carrier backed by SQLite | Carrier commits a label, then drops the reply |
| [Energy](../energy/README.md) | Meter files, an HTTP feed, and PostgreSQL settlement records | A late correction crosses a sealed period |
| [Media](../media/README.md) | FFmpeg workers and files served over HTTP | One rendition fails before publication |

These are runnable examples with independent result checks. An opt-in pytest
lane runs them through Docker Compose (see "Integration lane"). They
complement [Nightbank](../nightbank/README.md), which covers a larger
operator estate. They do not establish production readiness.

## Container setup

Use a Linux container engine with Docker Compose support. Select its context
explicitly. These commands use the local `podman` context; substitute the context
you intend to use. Run them from the repository root.

```sh
export WORKFLOW_CONTEXT=podman
docker --context "$WORKFLOW_CONTEXT" context inspect "$WORKFLOW_CONTEXT"
docker --context "$WORKFLOW_CONTEXT" info
docker --context "$WORKFLOW_CONTEXT" compose -f examples/workflows/compose.yaml build runner
docker --context "$WORKFLOW_CONTEXT" compose -f examples/workflows/compose.yaml up -d --wait postgres
```

The Compose project is `dsl41-workflows`. It owns its database, network, and two
named volumes. It publishes no host ports. The credentials are synthetic and
belong only to this local database. Never point an example at a production database.

The runner image contains this checkout, its locked runtime dependencies,
FFmpeg, and an example-only PostgreSQL driver. The driver does not change the
package dependencies or root lockfile. Python and PostgreSQL image references
include digests. Each pin is the multi-platform index digest, never one
platform's manifest, so the pins resolve on amd64 and arm64 alike.
A root `.dockerignore` admits only the three example directories and the
files the Dockerfile copies.
Debian packages, including FFmpeg, resolve at build time.
Record the resulting image ID and package versions when retaining evidence:

```sh
docker --context "$WORKFLOW_CONTEXT" image inspect dsl41-workflow-examples:local
docker --context "$WORKFLOW_CONTEXT" compose -f examples/workflows/compose.yaml images
docker --context "$WORKFLOW_CONTEXT" compose -f examples/workflows/compose.yaml run --rm --no-deps runner sh -c 'python --version; ffmpeg -version; /usr/local/bin/python -m pip --version; /usr/local/bin/uv pip freeze --python /app/.venv/bin/python'
```

Python's locked environment need not contain pip; the command above reports
the image's system pip and uses uv to inspect the application environment.
The integration lane records this command's output in its `session.json`.
Exact replay needs the built image itself, retained separately; nothing here
retains it.

## Run

Each launcher creates a fresh run directory under `/runs`, prints its path,
and retains its evidence. It starts its own loopback HTTP service and engine.
It stops those processes on completion or failure. A second run never reuses
the first run's database schema or business files.

```sh
docker --context "$WORKFLOW_CONTEXT" compose -f examples/workflows/compose.yaml run --rm runner python examples/fulfilment/demo.py
docker --context "$WORKFLOW_CONTEXT" compose -f examples/workflows/compose.yaml run --rm runner python examples/energy/demo.py
docker --context "$WORKFLOW_CONTEXT" compose -f examples/workflows/compose.yaml run --rm runner python examples/media/demo.py
```

Read each example's README for its modes, evidence, and manual recovery steps.
Run fulfilment first when qualifying a new environment. It checks the shared
database, HTTP, worker, and engine path before the other workloads add their
own behavior.

Manual instructions that use two terminals need two shells in the same runner
container. Its loopback HTTP endpoints are not reachable from a second container.
Set `WORKFLOW_CONTEXT` to the selected context in both host terminals.
Start an interactive runner in one host terminal:

```sh
docker --context "$WORKFLOW_CONTEXT" compose -f examples/workflows/compose.yaml run --rm --name dsl41-workflows-manual runner sh
```

Open another shell in that same container from a second host terminal:

```sh
docker --context "$WORKFLOW_CONTEXT" exec -it dsl41-workflows-manual sh
```

Run the domain README's server and client commands in those shells. Stop the
manual services and exit the second shell before exiting the first shell.

Engine roots belong on the native `runs` volume. Do not bind-mount a macOS or
Windows host directory over `/runs`. The runner needs Unix sockets, cross-process
locks, atomic rename, and file and directory fsync. Short paths also avoid the
Unix socket path limit. A container-native volume was exercised on Podman on
macOS; other providers need their own verification.

For a native Linux run, install the project's locked environment, FFmpeg and
ffprobe, and `examples/workflows/requirements.txt` in that environment. Supply
`DATABASE_URL` for a dedicated PostgreSQL database. The default run location is
`/tmp/dsl41-examples`; set `EXAMPLE_RUNS` to another short native path if needed.
The container path is the maintained recipe for these examples.

## Retain and inspect evidence

The run volume outlives the disposable runner containers. Run directories hold
business files, engine logs and journals, control responses, and checker results.
The PostgreSQL volume holds one schema per database-backed run. The carrier's
SQLite database belongs to its fulfilment run directory.

After launchers have stopped, copy the run volume out for inspection:

```sh
docker --context "$WORKFLOW_CONTEXT" compose -f examples/workflows/compose.yaml run --rm --no-deps -T runner tar -C /runs -cf - . > /tmp/dsl41-workflow-evidence.tar
docker --context "$WORKFLOW_CONTEXT" compose -f examples/workflows/compose.yaml exec -T postgres pg_dump -U example examples > /tmp/dsl41-workflow-database.sql
```

The archive is evidence, not a portable resumed engine root. Keep the database
dump with it if the check needs SQL state. Profiles contain the example database
URL. Treat archives as local run data and keep them outside Git.

Stop the infrastructure without deleting evidence:

```sh
docker --context "$WORKFLOW_CONTEXT" compose -f examples/workflows/compose.yaml down
```

After all launchers have stopped and the evidence is no longer needed,
`docker --context "$WORKFLOW_CONTEXT" compose -f examples/workflows/compose.yaml down --volumes`
deletes this example project's database and run volumes. Do not remove a live
engine root. A run's lineage anchor and root must remain together.

## Integration lane

`examples/workflows/tests` runs each launcher and checker as an operator does
(DL-236). It sits outside the default `testpaths`, so `uv run pytest` never
collects it. Run it from the repository root:

```sh
WORKFLOW_CONTEXT=podman uv run pytest -q -o faulthandler_timeout=0 examples/workflows/tests
```

Leave `WORKFLOW_CONTEXT` unset to use the Docker CLI's current context.
The lane builds the runner image once. Each test owns a Compose project named
`dsl41-wf-<test>-<suffix>`. Teardown stops any runner container still running,
dumps the database, kills the project's containers, copies the run volume out,
and runs `down`. It deletes the volumes with `down --volumes` only when both the
dump and the copy succeeded. Otherwise it keeps them, and the test errors with
their names and the command that removes them.
A missing engine, Compose, or image build fails every test. Nothing is skipped.

For fulfilment and media the lane runs the happy path and the demonstrated
incident. Energy has one documented run; its two tests read that run from the
business side and the scheduler side.
Each test asserts the checker's result, the scheduler history, and the real
worker attempts separately. Damage tests rerun a checker after changing a result
it reads and require a failure: fulfilment's stock and one operation payload,
energy's adjustment, and media's served file and an older rendition served with
recomputed hashes. The energy tests pass `--force-seal`;
the seal records that override, and the correction still crosses a period.

Evidence goes to `WORKFLOW_EVIDENCE`, or to pytest's temporary directory when
that is unset. The summary prints the path. `session.json` records the context
endpoint, Docker and Compose versions, the runner image ID, the PostgreSQL
digest, and the versions inside the image. Each test directory holds
`command.json` with every command's exit code, duration, stdout, and stderr,
`database.sql` from `pg_dump`, plus the run volume as numbered
`runs-N.tar` archives and the extracted copy of the last one. The lane is
opt-in in CI too: the `workflow-examples` workflow
runs only on manual dispatch and uploads the evidence.

## Next regression slice

The fault drills are not built: engine loss, detached-worker recovery,
control-response loss, database restart, concurrent scarce-stock orders, and
concurrent publication. The present examples make no claims about those faults.
Each drill is a separate slice. Keep each fault at a named boundary with an
observable outcome and a bounded wait.

`support.py` contains CLI invocation, private properties, atomic JSON writes,
and owned-process lifecycle only. Business operations and checks stay in each
example. No example imports dsl41 internals. The examples use the public CLI and
documented run-root layout, including manifests, journals, and the leader lock.
This keeps the integration lane at the interfaces an operator uses.
