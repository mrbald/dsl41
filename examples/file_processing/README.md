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
  inbox/                 complete producer deliveries; read and unlinked only
  objects/<sha256>       immutable input bytes
  objects/.tmp/          private object staging
  todo/<work_key>.json   actual JSON handover read by the processor
  todo/.tmp/             private TODO staging
  ledger.sqlite3        authoritative receipts, work, completion evidence
  archive.lock          one archiver; OS releases the lock on death
  process.lock          one processor; independent from archive.lock
```

Each published directory stages inside itself, under the reserved name `.tmp`.
Publication is a nonreplacing hard link from that staging directory into its
parent, so it never crosses a filesystem. Only the archiver stages, under
`archive.lock`. Its repair removes abandoned `object-*` and `todo-*` stage files.
Any other entry in a staging directory is foreign and refused, never removed.
The processor skips `todo/.tmp` and refuses any other entry that is not a TODO.

An empty root or inbox-only initial root can be initialized. If the ledger is
missing while objects, TODOs, abandoned stage files or SQLite sidecars remain,
both roles refuse before creating a replacement ledger. Any entry in `objects/`
or `todo/` other than an empty staging directory counts. They cannot
reconstruct lost identity or completion evidence from those artifacts.

## Filesystem requirements

`CONTRACT.md` "Filesystem" governs. The inbox, objects, TODOs and the ledger
may be separate mounts.

- `inbox/` must be readable, traversable and writable by the worker UID, so the
  archiver can read and unlink deliveries. Its directory must accept `fsync`.
  The archiver copies each delivery through one read stream and unlinks it
  after handover. It never renames, links or writes a file in the inbox.
- `objects/` and `todo/` each need `mkstemp`, hard links within the directory
  tree, file and directory `fsync`, `O_NOFOLLOW`, `O_DIRECTORY`, and coherent
  `lstat`/`fstat` regular-file reads. Do not mount anything on `.tmp`.
- The ledger and both lock files need working SQLite locking and `flock`.
  Local storage is the qualified case; NFS and SMB are not qualified.
- Mode `0o444` on objects is a convention, not protection.

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
stage file in `objects/.tmp`. It flushes the file, publishes the object with a
nonreplacing hard link, verifies any existing object, and syncs the object
directory. One SQL transaction marks the receipt `FILE_ARCHIVED` and inserts a
`READY` work row only if absent. Only then does it stage the TODO in
`todo/.tmp` and publish it with a nonreplacing hard link. It unlinks the source
and syncs the inbox directory only after the object is verified again and the
handover is complete: a published TODO, or a terminal work row for a duplicate.
Sources remain until handover succeeds. Short SQL write transactions coordinate
TODO eligibility and publication with processor completion. A stale archiver
read cannot recreate a TODO for terminal work.

A later archive invocation repairs missing TODOs even with no new arrivals,
finishes inbox cleanup, and removes its own abandoned stage files. An object
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
A leftover TODO for a terminal row is validated and removed without processing
again. `DONE` is the only terminal state; `TERMINAL` in the worker module derives
the set from the chart's final states.

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
receipts. Processor `remaining` counts nonterminal ledger work, including missing
TODO projections. New arrivals after the snapshot may not yet be registered.
`remaining=0` therefore proves neither an empty inbox nor end-to-end draining.
Archive failures raise. Processor callback
failures return a stopped summary and retain the reason in SQLite. The sample
CLI exits 1 for a stopped summary. OS role locks reject overlap immediately;
the sample CLI reports `LockBusy` as exit 75 and a refusal as exit 65.
Short SQLite transactions arbitrate between roles. There are no HA leases.

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

The sample CLI prints one JSON summary and exits 0 when the selected snapshot
completed. Exit 1 means the run stopped; the ledger holds the reason. The
exception is a ledger busy past its timeout: its reason is on stderr only, and
items completed earlier in the run stay committed. Exit 75
(`EX_TEMPFAIL`) means the role's lock was busy and nothing ran. Exit 65
(`EX_DATAERR`) means a refusal of corrupt, inconsistent or unsupported input.
Exits 75 and 65 print no summary and one line on stderr.

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
does not discover a glob or directory queue. How the jobs run again is the
estate author's choice: a timed archiver sweep, a rearm, or an operator's manual
run (CONTRACT.md, "Activation is external"). The workers and the helper assume
none of them.

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
can leave backlog that only a later invocation drains. The oracle test
demonstrates that overlap, then invokes the processor explicitly to drain the
backlog. Closing this edge belongs to the activation the estate author chooses.
This example claims neither automatic retry nor `n_retrys` behavior.

Focused tests interrupt one archival with an abrupt subprocess exit after each
durable boundary: object staged, object published, receipt and work committed,
TODO staged, TODO published, and source unlinked before `cleaned=1`. Each case
converges on the next run in either role order, and later runs change nothing.
Cross-role probes pause one role on a pipe handshake while the other runs: the
processor completes work whose TODO the archiver just published, and the
archiver archives a duplicate while the processor holds a claim. The sample CLI
is shown to exit nonzero on `LockBusy` and to leave a root unchanged on repeated
no-op runs. Other tests inject a `SystemExit` after real sample output
publication and cover lock release on process death, immutable dedupe,
selection, outbox recovery, UNKNOWN refusal, handover corruption, foreign
staging entries, lost-ledger evidence, DSL round trips and the oracle backlog gap.
They do not qualify power-loss behavior, network filesystems, distributed use,
continuous signalling or arbitrary external-effect reconciliation.
