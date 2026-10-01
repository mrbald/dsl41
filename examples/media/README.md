# Media processing and publication

This example runs real FFmpeg work under the dsl41 engine.
It generates a two-second synthetic video with audio.
The fixed JIL catalog makes two H.264/AAC renditions and a PNG poster.
An HTTP client retrieves and checks the published files.
All identities and inputs are synthetic.

Run from the repository root in the [workflow runtime](../workflows/README.md).
The runtime needs Python, installed dsl41, FFmpeg, and ffprobe.
The FFmpeg build must include `libx264`, AAC, PNG, and the `lavfi` input.
There is no external media download or database.

```sh
python examples/media/demo.py --scenario happy
python examples/media/demo.py --scenario failed-rendition
```

Each invocation creates a fresh private directory and prints its path.
`--run-dir /runs/media-example` selects a path that must not already exist.
The default scenario is `failed-rendition`.
The launcher stops its engines and loopback HTTP server on exit.
It retains inputs, work files, immutable releases, engine journals, command
responses, process histories, tool versions, and HTTP check evidence.
The printed HTTP URL is local to the runtime and lasts only for that invocation.

## Workflow

```text
MEDIA_INPUT
  ├── MEDIA_LOW ────┐
  ├── MEDIA_HIGH ───┼── MEDIA_STAGE → MEDIA_PUBLISH → MEDIA_VERIFY
  └── MEDIA_POSTER ─┘
```

`MEDIA_STAGE`'s `s(JOB,0)` zero lookback asks for a rendition success since
staging's own last end (SEM-04); each revision runs in a fresh engine root where
staging has never ended, so the qualifier is plain success there (Q2b).

`MEDIA_ENCODE` has one renewable admission slot.
The three media jobs compete for it.
The demo checks engine transitions for queued work and nonoverlapping admission.
It retains the trace and wrapper execution history.
These are scheduler admission limits.
They do not establish CPU or memory quotas, fairness, or preemption.
FFmpeg also receives an explicit one-thread encoder setting.

The input request binds a business revision to the source file's SHA-256.
Workers require a 320×180 source and produce 160×90 and 320×180 videos.
They check stream codecs, dimensions, duration within 0.15 seconds of two seconds,
and complete decoding before accepting an output.
Each encode writes to its own unique partial file, so overlapping attempts never
share one, and moves the checked file into place atomically.
Beside each accepted output the encoder writes a `.binding.json` sidecar with the
admitted input's SHA-256 and the output's SHA-256.
A rerun reuses an existing output, and staging copies one, only when its sidecar
names the admitted input and the file's current hash; otherwise it refuses.
The HTTP checker independently checks the three required manifest members,
downloaded bytes and hashes, metadata, 48 decoded video frames, and full decoding.
It decodes the whole audio stream of the source and both served videos and
requires 1.99 to 2.1 seconds, which allows for AAC padding and rejects truncation.
It also decodes audio from 0.5 to 1.0 seconds in the source and both served videos.
That segment must be non-silent and contain the fixture's `440 × revision` Hz tone.
Frequency estimates use interpolated positive zero crossings with a 5 Hz tolerance.
The served tones must also match the source within 5 Hz.
This rejects an older fixture rendition even if its manifest hashes were recomputed.
It imports no worker code.

Staging copies validated outputs into a directory outside the HTTP document root.
A completed directory is renamed into `public/releases/rN`.
The example makes its files read-only and never modifies that release again.
Under an exclusive publication lock, a worker checks the expected previous
revision and atomically replaces `public/current.json`.
The pointer names one immutable manifest and its hash.
Each manifest lists the file names within that revision's immutable release.
Clients fetch the pointer once and use that release for the whole retrieval.
The application enforces this rule; scheduler success alone cannot authorize it.

## Failed rendition drill

The drill first publishes revision 1.
Revision 2's high rendition runs FFmpeg for half a second of output, records
the fault, and deliberately exits 23 before accepting the file.
This is an injected worker failure, not a process-kill or restart demonstration.

The launcher waits for that failure and the other two jobs' completion.
It checks that staging and publication have never started.
It fetches and decodes revision 1 through HTTP while revision 2 is incomplete.
It then issues one explicit `FORCE_STARTJOB` for `MEDIA_HIGH`.
The engine starts the remaining dependency chain after that job succeeds.
The launcher checks that low and poster bytes remain unchanged and that their
jobs ran once while high ran twice.
Each worker writes a unique invocation record on entry.
The launcher checks those counts separately from scheduler run numbers.
The failed partial file remains outside the published tree.

Finally, a revision-1 worker attempts publication after revision 2 is current.
The application refuses the stale revision.
The demo fetches revision 2 again and checks that revision 1's files did not change.
Each revision uses a fresh engine root.
This does not demonstrate a dsl41 period roll.

## Inspection and manual recovery

`result.json` is written only after the selected scenario passes.
`r2/failure-status.json` records the blocked graph.
`r2/commands.jsonl` records the explicit rerun and its control response.
`business/faults/r2-high.fired` names and hashes the failed partial file.
`r1/runs.json` and `r2/runs.json` contain engine execution histories.
Each wave's `attempts/` records actual worker entries and `attempt-counts.json`
records the checked counts before the later stale-publication probe.
After stopping the engine, the demo checks every offline history row against
those counts and the expected statuses and exit codes.
The failed high rendition must appear as run 1 with FAILURE and exit 23.
Every row must have full fidelity and a recorded completion.
`history-check.json` records that comparison.
HTTP evidence directories retain each independent check attempt separately.

During an active engine, use its retained run path to select the socket:

```sh
dsl41 query status --socket /runs/media-example/r2/engine/control.sock
dsl41 sendevent FORCE_STARTJOB --job MEDIA_HIGH --socket /runs/media-example/r2/engine/control.sock
```

Do not recompose a control request whose reply was lost.
Use the original request pins printed by the CLI and the
[control recovery contract](../../docs/control-protocol.md).
An application rerun is a separate operation from replaying that control request.

The manual procedure below is not exercised by the demo.
The launcher completes the targeted rerun automatically.
It has no mode that leaves an incomplete release for manual completion.
For an actual engine outage, use these steps only after proving the process fence:

1. Stop or fence the engine that owns this root. These engines are tethered.
   Wait for all recorded wrappers and child processes to exit before proceeding.
   Inspect the engine logs and `dsl41 runs <engine-root> --format json`.
   Do not start manual work while any process ownership is uncertain.
2. Read `r2/config.json`, `business/inputs/request-r2.json`, the fault record,
   `public/current.json`, and existing release manifests.
   `current.json` records the publication decision.
   Independently validate its referenced release before accepting it.
   Keep all partial files and logs.
3. Run the missing commands with the retained business request:

   ```sh
   python examples/media/worker.py --run /runs/media-example/r2 high
   python examples/media/worker.py --run /runs/media-example/r2 stage
   python examples/media/worker.py --run /runs/media-example/r2 publish
   ```

   Completed renditions are reused only when their binding sidecar names the
   admitted input and their current hash, and are validated again.
   Existing releases must match the proposed manifest exactly.
   Publishing the same current release is idempotent.
   Publishing an older revision refuses.
4. In a separate terminal, serve only the public directory:

   ```sh
   python -m http.server 8080 --bind 127.0.0.1 --directory /runs/media-example/business/public
   ```

   Wait for the server's startup message. In another terminal, run the checker:

   ```sh
   python examples/media/check.py --url http://127.0.0.1:8080 --revision 2 --source /runs/media-example/business/inputs/clip-r2.mp4 --evidence /runs/media-example/manual-check
   ```

   Stop that server with Ctrl-C after inspection.
5. Archive the old scheduler root. Do not resume it after manual changes.
   Use a fresh engine root and a new admitted business revision for later work.
   The demo's next invocation starts a separate isolated business run.

The injected fault is one-shot.
Its `.armed` marker becomes `.fired` before FFmpeg starts the partial encode.
If that encode itself fails, the fault record is incomplete and the demo fails.
Inspect the worker log before any manual retry.

## Limits and further drills

The runtime is one Linux host with a local filesystem and trusted local files.
The publication lock and rename rule give cooperating readers a complete release.
Read-only permissions are not protection against an owner rewriting files.
This example does not prove machine-power-loss durability or network-filesystem
rename behavior.
The HTTP service has no authentication and binds only to loopback.
The audio check distinguishes these synthetic revisions, whose video is identical.
It does not prove transform provenance for arbitrary media or compare image content.

Further work should kill an encoder at a confirmed output boundary, exercise
detached survival and reattachment with process identity evidence, and kill the
engine during publication.
Lease/deadman limits, long encodes, concurrent readers during replacement,
physical resource limits, and throughput need separate drills.
The opt-in integration lane in `examples/workflows/tests` runs this
launcher and its checker; see the Integration lane section of
`../workflows/README.md`.
