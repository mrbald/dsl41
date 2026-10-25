# Experimental file processing

This is code as documentation for one host and a local POSIX filesystem.
It is not a frozen dsl41 API. It adds no compiler or scheduler behavior.
The worker imports the existing statechart executor, but no scheduler.
The Python estate uses ordinary `CatalogBuilder.job` calls.

The producer must publish complete, immutable files under unique inbox basenames.
Its scp/ftp transfer and atomic rename are preconditions. The archiver starts at
`FILE_RECEIVED`. A basename must never be reused, even for identical content.
Names such as `001-report.txt` and `002-report.txt` can represent two deliveries
of the same original filename. The root and all its parents must be privately
managed. Symlinks, directories and other nonregular inbox entries are refused.
Concurrent modification by other users or a handler is outside this contract.

## Storage and identities

```text
root/
  inbox/                 complete producer deliveries
  objects/<sha256>       immutable input bytes
  todo/<work_key>.json   actual JSON handover read by the processor
  ledger.sqlite3        authoritative receipts, work, completion evidence
  archive.lock          one archiver; OS releases the lock on death
  process.lock          one processor; independent from archive.lock
  tmp/                  private archive and TODO staging
```

An empty root or inbox-only initial root can be initialized. If the ledger is
missing while objects, TODOs, staging files or SQLite sidecars remain, both
roles refuse before creating a replacement ledger. They cannot reconstruct
lost identity or completion evidence from those artifacts.

Keep runtime files outside the checkout. One processor identity owns a root.
A different identity is refused. Include a business revision in the identity
when appropriate, and use a new root for a changed identity.
Keep handler configuration and its output namespace fixed for that identity.
The processing key is SHA-256 of the canonical JSON pair
`[processor_id, content_hash]`. Dedupe assumes filenames have no business meaning.
Different basenames with identical bytes retain separate receipts and share one
object, work row and effect. Different bytes create separate work.

Each newly discovered basename receives a durable sequence before selection.
Names sort deterministically within one discovery snapshot. This is admission
order, not physical arrival time. Mtime is ignored. A work item's
`receipt_sequence` is the admission sequence of the delivery that creates its
work row. Its filename metadata comes from that delivery too. If newest-first
archival hashes an admitted duplicate at sequence 3 before the identical input
at sequence 1, the work retains sequence 3. Duplicates never change that work
sequence, metadata, state or completion evidence. This is not guaranteed earliest
physical arrival or earliest arrival of that content.

## Declared state changes

These equations name the events in `file_processing_charts.py`:

```text
FILE_ARCHIVED = FILE_RECEIVED + archive_committed // object and receipt/work committed; TODO follows
RUNNING = READY + claim                       // commit before calling the handler
DONE = RUNNING + completed                    // durable completion evidence; terminal
READY = RUNNING + retry_safe                  // explicit proof that another attempt is safe
UNKNOWN = RUNNING + uncertain                 // unexpected exception or interrupted claim
DONE = UNKNOWN + completed                    // reconciliation verified the effect; terminal
READY = UNKNOWN + retry_safe                  // reconciliation proved safe retry
UNKNOWN = UNKNOWN + uncertain                 // insufficient evidence; no process call
```

Every SQL state write uses the executor's resulting leaf. The charts do not
perform I/O. Restoring a durable state runs no entry actions. The executor
provides no durability itself. No extra states model individual syscalls.

The archiver snapshots inbox names, repairs previous handovers, then admits and
selects deliveries. It copies and hashes the same open input stream into a
private temp file. It flushes the file, publishes the object with a nonreplacing
hard link, verifies any existing object, and syncs the object directory. One SQL
transaction marks the receipt `FILE_ARCHIVED` and inserts a `READY` work row only
if absent. Only then does it atomically publish the TODO and remove the source.
Sources remain until handover succeeds. Short SQL write transactions coordinate
TODO eligibility and publication with processor completion. A stale archiver
read cannot recreate a TODO for `DONE` work.

A later archive invocation repairs missing TODOs even with no new arrivals,
finishes inbox cleanup, and removes its own stale staging files. An object
published before a crash but without a receipt commit is reused on recovery.
Before retiring an uncleaned archived receipt, cleanup verifies the archived
object's identity and bytes as well as any retained inbox copy. A missing or
corrupt object causes refusal with the inbox copy retained. This detects
inconsistent persisted state; it does not repair corruption.
There is no cross-filesystem transaction and no general object garbage collector.

The processor obtains work from complete TODO files. It validates each TODO
against SQLite and verifies input SHA-256 before handing over the immutable path.
Its callback also receives the key, hash, stable receipt sequence and metadata.
It never discovers inbox deliveries or repairs archive projections. A missing
TODO requires an archiver invocation; the processor reports ledger backlog in
`remaining`. Malformed JSON, tampering and missing/corrupt objects fail loudly.
A leftover TODO for `DONE` is validated and removed without processing again.

## Callbacks, recovery and selection

`process(item)` returns `Completed(evidence)`, `RetrySafe(reason)` or
`Unknown(reason)`. Evidence and reasons are nonempty strings. The handler owns
the truth of its evidence. Optional `reconcile(item)` uses the same result types.
Unexpected callback exceptions become `UNKNOWN`. Interruptions leave a durable
`RUNNING` claim. On the next selected recovery, that becomes `UNKNOWN` before
reconciliation. Without a reconciler it remains `UNKNOWN`. There is no automatic
retry for arbitrary effects and no exactly-once guarantee.

The sample `CountsHandler` routes JSON arrays to record counting and UTF-8 text
to line counting. It atomically publishes immutable JSON output keyed by the
processing key. Reconciliation recomputes expected bytes and verifies the whole
output, including input identity. Only this handler's sole public effect makes
absence of output proof of `RetrySafe`. Other business effects need their own
proof. A crash may leave private `.count-*` staging files; they are not effects.

Each role accepts `Policy(order='oldest'|'newest', batch='one'|'all')`, validated
before I/O. `all` is a finite snapshot, sequential, each item at most once.
New arrivals wait for a later invocation. One archive item is a delivery.
One process item is a processing key. Recovery candidates precede fresh work;
each group follows the requested order. Reconciliation consumes the selected
item even if it proves `RetrySafe`. It never calls `process` again in that run.
The first failure stops the invocation. `newest` does not guarantee fairness.

Both roles return a `Summary` with selected/completed/duplicates/remaining/unknown,
reconciled and stopped fields. Archive `remaining` counts admitted FILE_RECEIVED
receipts. Processor `remaining` counts non-DONE ledger work, including missing
TODO projections. New arrivals after the snapshot may not yet be registered.
`remaining=0` therefore proves neither an empty inbox nor end-to-end draining.
Archive failures raise. Processor callback
failures return a stopped summary and retain the reason in SQLite. The sample
CLI exits 1 for a stopped summary. OS role locks reject overlap immediately;
short SQLite transactions arbitrate between roles. There are no HA leases.

## Run the synthetic demo

From the repository root, with the locked environment:

```sh
root=$(mktemp -d /private/tmp/dsl41-file-demo.XXXXXX)
.venv/bin/python -m examples.file_processing.demo --root "$root"
```

The demo writes four synthetic deliveries. It verifies four receipts, three
objects, three immutable outputs, three DONE records and no TODOs. It invokes
the processor first as a no-op, then archives all, processes one, explicitly
drains the remaining two, and checks subsequent no-ops. Output counts are JSON
records = 3, text lines = 1 and text lines = 2.

Workers can also run independently in either order:

```sh
.venv/bin/python -m examples.file_processing.worker archive --root "$root"
.venv/bin/python -m examples.file_processing.worker process --root "$root" --batch one
.venv/bin/python -m examples.file_processing.worker process --root "$root" --batch all
```

## Python estate and one-cycle watcher

Import `add_file_processing_jobs` from `dsl41.file_processing_dsl` to add exactly
two command jobs to an existing builder. This helper is explicitly experimental
and separate from the worker's imports.
The caller supplies the existing FW name, machine and trusted command argv.
`shlex.join` quotes argv. Commands explicitly invoke the chosen Python worker.
There is no implicit runtime dependency in compiled artifacts.

`build_estate` demonstrates exactly one literal-path FW plus ARCHIVER and
PROCESSOR. ARCHIVER depends on watcher success. PROCESSOR uses
`s(ARCHIVER,0)`. There is no TODO watcher. The ready signal path is an explicit
one-cycle demonstration input. Publish the complete inbox deliveries before
publishing that signal. The FW watches a literal file and size stability; it
does not discover a glob or directory queue. Continuous rearming and safe
recurrent signalling remain unsolved by this example.

The CLI catalog loader accepts JIL. Generate a temporary JIL artifact through
the normal Python builder pipeline; direct `.py` CLI loading is not supported:

```sh
.venv/bin/python -m examples.file_processing.estate \
  --root "$root" --ready-signal "$root/ready.signal" \
  --python "$PWD/.venv/bin/python" > "$root/estate.jil"
.venv/bin/dsl41 run "$root/estate.jil"
```

The command jobs use `python -m examples.file_processing.worker`. The repository
root must therefore be importable by those processes. Supply absolute script
paths or another installed application entry point in a real caller's argv.
The generated catalog command above is illustrative; the demo does not run a
supervisor or assert runner qualification.

Zero lookback compares the archiver END with the processor's last END (SEM-04).
A first run can match an old archiver success. An archiver completion during a
processor run can become stale before that processor ends. A one-item batch
can leave backlog without a new scheduler wakeup. The oracle test demonstrates
that overlap, then explicitly invokes the processor later to drain the backlog.
Use subsequent explicit invocations as shown above. Reliable recurrent wakeups
are separate work. This example claims neither automatic retry nor `n_retrys`
behavior.

Focused tests use abrupt subprocess exits at all three archive boundaries and
an injected `SystemExit` after real sample output publication. They also cover
lock release on process death, immutable dedupe, selection, outbox recovery,
UNKNOWN refusal, handover corruption, DSL round trips and the oracle backlog gap.
They do not qualify power-loss behavior, network filesystems, distributed use,
continuous signalling or arbitrary external-effect reconciliation.
