# Experimental file processing

This is code as documentation for one host and a local POSIX filesystem.
Slices 1 to 4 of [HANDOVER.md](HANDOVER.md) are built. The authoring API is
`add_file_processing_jobs` and the worker command
`python -m dsl41.file_processing_worker`; the job author's guide shows both
([authoring §1.1](../../docs/authoring.md#11-add-file-processing-to-an-estate)).
The [exercise](#exercise-run-the-estate-under-the-runner) runs the example estate
under the real engine and supervisor on one host (DL-417): repeated arrivals, a
long handler, a supervisor stop and resume, a toxic delivery and an operator's
ruling. It does not exercise what slice 5 also names: batch limits, resource
waits and missed wakes. It adds no compiler or scheduler behavior.
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
  ledger.sqlite3        authoritative receipts, work and the decision record
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
A ledger whose existing tables have other columns than this release's is refused
with exit 65 before anything changes; it is never migrated.

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
REJECTED = READY + rejected                   // operator only: no further attempt; terminal
REJECTED = UNKNOWN + rejected                 // operator only; the uncertainty stays recorded
```

`RUNNING + rejected` is excluded: a stale claim is converted through `.04` first.
Every SQL state write uses the executor's resulting leaf. The charts do not
perform I/O. Restoring a durable state runs no entry actions. The executor
provides no durability itself. No extra states model individual syscalls.

Each work row carries an `attempt` count. Only the `claim` move increments it,
in the claim's own transaction; handlers see it as `WorkItem.attempt`. Each move
also appends one row to the `decisions` table in the same transaction: work key,
attempt, prior and resulting state, the id of the chart row the executor fired,
origin (`handler`, `reconciler` or `operator`), actor, ISO 8601 UTC wall-clock
time, reason and evidence. The record is the only store of outcomes; triggers
refuse an update or a delete. The processor's actor is its processor identity.
Its own conversion of a stale claim through `.04` has origin `reconciler`.
Admission of a `READY` row is not a move and has no decision.

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
again. `DONE` and `REJECTED` are terminal; `TERMINAL` in the worker module derives
the set from the chart's final states. Every path treats them alike: the move and
its decision commit before the TODO is removed, either role finishes an
interrupted removal, repair never republishes them, selection and `remaining`
exclude them, a duplicate delivery leaves them unchanged, and a duplicate source
is unlinked once the object is verified.

## Callbacks, recovery and selection

`process(item)` returns `Completed(evidence)`, `RetrySafe(reason)` or
`Unknown(reason)`. Evidence and reasons are nonempty strings. The handler owns
the truth of its evidence. Optional `reconcile(item)` uses the same result types.
Unexpected callback exceptions become `UNKNOWN`. Interruptions leave a durable
`RUNNING` claim. On the next selected recovery, that becomes `UNKNOWN` before the
processor verifies the object, so a claim whose object is missing still reaches
the operator as `UNKNOWN`. Without a reconciler it remains `UNKNOWN`. There is no
automatic retry for arbitrary effects and no exactly-once guarantee.

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
(`EX_DATAERR`) means a refusal of corrupt, inconsistent or unsupported input,
including a damaged ledger wherever the run reads it. It also covers a ledger the
run cannot write, such as a full disk or an I/O error; the stderr line names the
SQLite error.
Exits 75 and 65 print no summary and one line on stderr.

## Operator CLI

The library's `list_work`, `show_work` and `rule_work` back a sample operator
CLI with the worker's exit codes and JSON on stdout:

```sh
.venv/bin/python -m examples.file_processing.operate --root "$root" list
.venv/bin/python -m examples.file_processing.operate --root "$root" show --key "$key"
.venv/bin/python -m examples.file_processing.operate --root "$root" rule --key "$key" \
  --event rejected --expect-state UNKNOWN --expect-attempt 2 --reason "toxic input"
```

`list` shows every work row with its state, attempt, receipts and their cleaned
flags, whether the object is verified, missing or unverified, whether a TODO
exists, and the latest decision. `show` adds the whole decision record. Neither
takes a role lock, and both open the ledger read-only and create nothing. The SQL
rows are one snapshot; the object and TODO checks read the filesystem after that
snapshot ends, so hashing objects never delays a worker's commit. A read-only open
cannot roll back a hot journal left by a killed worker; `list` and `show` then
refuse with "ledger has a hot journal", and the next `archive` or `process` run
recovers it.

`rule` takes `process.lock`, so it never races a processor. Inside the
transaction that moves the row it checks `--expect-state` and `--expect-attempt`.
Read the expected attempt from `list` or `show` immediately before `rule`, because
a stuck item's attempt can rise on every processor run; a mismatch is refused with
exit 65. It records origin `operator` and `--actor`, by default the login name.
The rulings are:

- `UNKNOWN + completed` with `--evidence`: the operator verified the effect.
- `UNKNOWN + retry_safe` with `--reason`: the operator proved another attempt safe.
- `READY + rejected` or `UNKNOWN + rejected` with `--reason`: make no further attempt.
  This proves nothing about the business effect.
- `RUNNING + uncertain` with `--reason`: convert a stale claim without reading the
  object. Then rule the `UNKNOWN` row.

The chart decides whether the event moves the expected state. A ruling to a
terminal state removes the TODO after it commits. Under the `stop` disposition a
stuck head blocks the queue until an operator rules it.

## Operator procedures

Four archiver refusals are not work states, and no ruling repairs them. The
procedures below are manual. A storage repair holds both `archive.lock` and
`process.lock` for its whole duration, because either role can read what it
changes. With util-linux `flock(1)`, which takes the same `flock(2)` locks as
the workers:

```sh
flock --nonblock "$root/archive.lock" \
  flock --nonblock "$root/process.lock" \
  sh -c 'the repair commands'
```

Where `flock(1)` is absent, as on macOS, the same two locks from Python's
standard library:

```sh
python3 -c 'import fcntl,os,subprocess,sys; fds=[os.open(p,os.O_RDWR|os.O_CREAT,0o600) for p in sys.argv[1:3]]; [fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB) for fd in fds]; sys.exit(subprocess.call(sys.argv[3:]))' "$root/archive.lock" "$root/process.lock" sh -c 'the repair commands'
```

A busy lock makes either recipe exit nonzero without running the repair, as
`LockBusy` does for the workers; retry later.

- **Reused basename** (`delivery basename reused`). Move the delivery out of
  `inbox/` to a quarantine directory outside the root. The producer redelivers
  it under a new unique basename. Never rename it inside `inbox/`.
- **Missing or corrupt committed object** (`missing or unreadable archived
  object`, `corrupt archived object`, `corrupt existing object`). Remove a
  corrupt `objects/<sha256>`. Restore the file from backup, or from a retained
  inbox copy, verify that its SHA-256 equals its name, set mode `0444`, and
  sync. Then run the archiver.
- **Damaged TODO** (`unreadable TODO`, `TODO differs from ledger`, `invalid TODO
  filename`, `TODO has no ledger work`). Move the file out of `todo/`. The next
  archiver run republishes a TODO for nonterminal work from the ledger.
- **Lost ledger** (`missing ledger in an established worker root`). A refusal
  that names a hot journal is not a lost ledger; run a worker to recover it. Restore
  `ledger.sqlite3` from backup, with its own journal if it had one; never pair it
  with a sidecar left by the lost ledger. A backup older than the last decision
  forgets that decision, and the processor would claim that work again. Disable
  processor activation before releasing the locks, because `rule` takes
  `process.lock` itself. With no processor run pending, compare each nonterminal
  row in `list` with the handler's effects, and rule `rejected` where the effect
  is known to exist; then enable activation. Receipts admitted after the backup
  are lost from the ledger. Their TODOs become `TODO has no ledger work`
  refusals; move them out of `todo/`. Their objects stay as harmless orphans. The
  producer must redeliver those files under new basenames. If the estate uses the
  example's REARM, remove `ROOT/rearm.json` too: its counts belong to the lost
  ledger. Without a backup, the root cannot be recovered; start a new root.

## Python estate and its activation

Import `add_file_processing_jobs` from `dsl41.file_processing_dsl` to add exactly
two command jobs to an existing builder. The helper is separate from the
worker's imports. The caller supplies the existing FW name, machine and trusted
command argv. `shlex.join` quotes argv. Commands explicitly invoke the chosen
Python and the deployed worker, `python -m dsl41.file_processing_worker`.
There is no implicit runtime dependency in compiled artifacts.
[Authoring §1.1](../../docs/authoring.md#11-add-file-processing-to-an-estate)
lists the helper's refusals, the host command and the worker's exit codes.

`estate` in [estate.py](estate.py) demonstrates exactly one literal-path FW plus
ARCHIVER and PROCESSOR. ARCHIVER depends on watcher success. PROCESSOR uses
`s(ARCHIVER,0)`. There is no TODO watcher. The FW watches a ready signal, a
literal file, for size stability; it does not discover a glob or directory
queue. Publish the complete inbox deliveries before publishing that signal.
How the jobs run again is the estate author's choice: a timed archiver sweep, a
rearm, or an operator's manual run (CONTRACT.md, "Activation is external"). The
workers and the helper assume none of them.

The example's choice is a rearm (DL-417). With `--rearm` naming the engine's
control socket, the estate adds a fourth job, REARM, on
`s(ARCHIVER, 0) & d(PROCESSOR, 0)`: it runs after the archiver's success and the
processor's end, whatever that end was. It runs [rearm.py](rearm.py), which
removes the ready signal, publishes it again when work is pending, and sends
STARTJOB to INBOX_FW. Work is pending when a delivery waits in the inbox, when the
ledger holds more `RUNNING` claims than at REARM's previous run, or when it holds
`READY` or `RUNNING` work and no `UNKNOWN` work and the cycle made progress: a row
reached a terminal state, or a claim was resolved, since REARM's previous run. REARM keeps the counts it compares in
`ROOT/rearm.json`. So a delivery that lands while the processor runs, a processor
stopped mid-run, and a reconciliation that proved `RetrySafe` each start the next
cycle. The watch is down from the watcher's success to REARM's run; a signal
published then stays in place and the inbox check covers it.

After a manual processor run, such as the exercise's FORCE_STARTJOB, the
processor's end already satisfies REARM's condition. The next archiver success
then starts REARM beside the processor, and one extra cycle runs with a no-op
archiver. Nothing is lost; REARM runs again after the processor ends.

After a processor run without progress (a handler that never completes, an
unloadable `--handler`, a refusal, a busy lock, or an `UNKNOWN` row awaiting a
ruling), REARM starts the watch but publishes no signal. The waiting work starts
no cycle until the next delivery or an operator's run, so a failure never cycles
without bound. The counts in `rearm.json` also see no progress when two runs in a
row stop mid-run and the second returns one claim and takes another; that work
waits for the next delivery too. Nothing is lost.

These cases leave the watch down until an operator sends
`dsl41 sendevent STARTJOB -J INBOX_FW`:
- an archiver that fails, so no processor runs;
- a REARM that fails, for example because the engine was stopped while it ran and
  its STARTJOB found no socket;
- a REARM ended by a supervisor stop.

`--resume` starts nothing by itself. It re-arms only when the stopped processor's
end, recorded at resume, completes REARM's condition, as in the exercise. Without
`--rearm` the estate has three jobs and one cycle.

The CLI catalog loader accepts JIL, so the module prints the estate as JIL;
`dsl41 run` does not load a `.py` file. The process job names the sample handler
as `--handler examples.file_processing.worker:counts_handler`, so the repository
root must be importable by that process. A real estate names its own installed
handler module.

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
They interrupt processing the same way after the claim commit, after the handler
returns and after the outcome commit, with and without a reconciler. A test-only
checker recomputes the sample handler's outputs from the ledger and `objects/`
alone and compares them with the files and the recorded evidence.
A ledger damaged in pages read only after opening is refused with exit 65 by
every role and every operator command.
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

## Exercise: run the estate under the runner

This exercise is for an operator. It runs the example estate under `dsl41 run`
and its supervisor on one POSIX host, stops both while the processor is
mid-run, resumes, and rules one toxic delivery `REJECTED`. You are done when
every work row is terminal and the handler ran once for each row.

Work from the repository root after `uv sync --frozen --extra dev`, with
`.venv/bin` first on `PATH`, so `python` and `dsl41` are the locked ones. Set
`work` to a new, empty directory with a short path, for example
`work=$(mktemp -d /tmp/dsl41-fp.XXXXXX)`: a control socket path must fit in
104 bytes. Type each block into one shell, in order. A `#` line after a command
is what that command prints, standard output and error together. `…` stands for
text that changes from run to run, such as times, ids and paths. A command with
no `#` line prints nothing. Every command exits 0 unless its first `#` line says
`exit N:`. A block marked *repeat* waits for the runner: type it again until it
prints what is shown. A wait is a few seconds.
[test_file_processing_runner.py](../../tests/test_file_processing_runner.py)
runs every block as printed here.

### 1. Variables, the estate and the first deliveries

<!-- exercise: variables -->
```sh
root=$work/root
RUN_ROOT=$work/run
S=$RUN_ROOT/control.sock
```

The estate uses the held handler. Each call appends its work key to
`$root/calls`; after it publishes its output, it waits while `$root/hold` exists.
The hold keeps the first processor run alive until you stop it.

<!-- exercise: estate -->
```sh
mkdir "$root" "$root/inbox"
python -m examples.file_processing.estate --root "$root" --ready-signal "$root/ready.signal" \
  --handler examples.file_processing.worker:held_counts_handler --rearm "$S" >"$work/estate.jil"
printf 'one\n' >"$root/inbox/001-a.txt"
printf '[1,2]\n' >"$root/inbox/002-b.json"
touch "$root/hold"
```

Nothing runs yet, so nothing reads these files while you write them. Later
deliveries are written beside the inbox and renamed into it, as a producer must.

### 2. Start the engine and the first cycle

`--detached` runs the commands under a supervisor that the engine starts.
`DSL41_DRAIN_S=5` is in the environment that supervisor inherits. It is the
supervisor's drain bound (DL-416), set short here so that step 3's stop cuts
the held item within seconds; a service unit sets 840 s.

<!-- exercise: start -->
```sh
DSL41_DRAIN_S=5 dsl41 run "$work/estate.jil" --run-root "$RUN_ROOT" --detached \
  --as-machine localhost >"$work/engine.log" 2>&1 &
echo $! >"$work/engine.pid"
```

*Repeat* until the engine answers:

<!-- exercise: started repeat -->
```sh
dsl41 query status --brief -J INBOX_FW -S "$S"
# INBOX_FW … INACTIVE … run 0 …
```

Publish the signal and start the watcher. The `sending:` line holds the values a
safe retry needs.

<!-- exercise: first-cycle -->
```sh
echo ready >"$root/ready.signal"
dsl41 sendevent STARTJOB -J INBOX_FW -S "$S"
# sending: …
# {…"decision": "applied"…"kind": "STARTJOB"…}
```

*Repeat* until the first item's output exists. The processor is then waiting on
the hold, and the watch is down until REARM runs after it.

<!-- exercise: holding repeat -->
```sh
ls "$root/output"
# e2655ccff9fd0788f8676a41e9f7cb78cd1122fca579a099e8b340b4b702687f.json
dsl41 query status --brief -J PROCESSOR -S "$S"
# PROCESSOR … RUNNING … run 1 …
dsl41 query status --brief -J INBOX_FW -S "$S"
# INBOX_FW … SUCCESS … run 1 …
```

### 3. Stop the engine, then the supervisor

The engine goes first, as in `dsl41 estate quiesce`. Its detached jobs keep
running under the supervisor.

<!-- exercise: stop-engine -->
```sh
kill -TERM "$(cat "$work/engine.pid")"
```

*Repeat* until the engine has printed its reattach line:

<!-- exercise: engine-stopped repeat -->
```sh
cat "$work/engine.log"
# engine up; control socket: …/control.sock
# stopping: jobs continue under the supervisor (detached, ss6a)
# detached: reattach with `…`
```

Now stop the supervisor with SIGTERM. It drains (DL-416): it starts no new
command and lets the running ones end within its bound, `DSL41_DRAIN_S`. The
processor waits on the hold, so it outlives the bound: after 5 s the supervisor
sends it TERM, and the processor run ends `TERMINATED`. On a service host,
`dsl41 estate quiesce` stops the estate and seals the period, and its
maintenance stage needs Linux service containment; SIGTERM is the stop this
host has.

<!-- exercise: stop-supervisor -->
```sh
kill -TERM "$(dsl41 supervise list --run-root "$RUN_ROOT" | sed 's/.*"supervisor_pid": \([0-9]*\).*/\1/')"
```

*Repeat* until the supervisor is gone:

<!-- exercise: supervisor-stopped repeat -->
```sh
dsl41 supervise list --run-root "$RUN_ROOT"
# exit 2: no supervisor at …/supervisor.sock
```

The first item's output exists and the handler ran once, but the claim is still
`RUNNING`: the effect happened and the ledger does not know it. The second item
is `READY`. Release the hold, so the next run does not wait on it.

<!-- exercise: interrupted -->
```sh
python -m examples.file_processing.operate --root "$root" list
# [{"attempt": 1, …"name": "001-a.txt"…"state": "RUNNING", "todo": true…}, {"attempt": 0, …"name": "002-b.json"…"state": "READY", "todo": true…}]
cat "$root/calls"
# e2655ccff9fd0788f8676a41e9f7cb78cd1122fca579a099e8b340b4b702687f
rm "$root/hold"
```

### 4. Resume

<!-- exercise: resume -->
```sh
DSL41_DRAIN_S=5 dsl41 run --resume "$work/estate.jil" --run-root "$RUN_ROOT" --detached \
  --as-machine localhost >"$work/resume.log" 2>&1 &
echo $! >"$work/engine.pid"
```

The engine records the stopped processor run as `TERMINATED`. That end completes
REARM's condition. REARM finds work pending and publishes the signal again, and
the next cycle recovers the work. *Repeat* until the processor's second run has
succeeded and REARM has started the third watch:

<!-- exercise: resumed repeat -->
```sh
dsl41 query status --brief -J PROCESSOR -S "$S"
# PROCESSOR … SUCCESS … run 2 … exit 0 …
dsl41 query status --brief -J INBOX_FW -S "$S"
# INBOX_FW … RUNNING … run 3 …
```

The reconciler found the first output and recorded it as the completion: rows
`.04` and `.05` carry origin `reconciler`. The handler ran once more, for the
second item only.

<!-- exercise: recovered -->
```sh
cat "$RUN_ROOT/logs/PROCESSOR.2.out"
# {"completed": 2, "duplicates": 0, "reconciled": 1, "remaining": 0, "selected": 2, "stopped": false, "unknown": 0}
python -m examples.file_processing.operate --root "$root" show --key e2655ccff9fd0788f8676a41e9f7cb78cd1122fca579a099e8b340b4b702687f
# {"decisions": [{…"chart_row": "file_process.01"…"state": "RUNNING"…}, {…"chart_row": "file_process.04"…"origin": "reconciler"…"state": "UNKNOWN"…}, {…"chart_row": "file_process.05"…"origin": "reconciler"…"state": "DONE"…}], "work": {"attempt": 1, …"state": "DONE", "todo": false…}}
cat "$root/calls"
# e2655ccff9fd0788f8676a41e9f7cb78cd1122fca579a099e8b340b4b702687f
# 0f4e46db9c5790070c51a1033536d8a01a2efe460cae74be8194256fb3176509
```

If REARM had failed or been stopped, the watch would stay down: send
`dsl41 sendevent STARTJOB -J INBOX_FW -S "$S"` yourself.

### 5. A toxic delivery

`003-bad.json` is toxic: the handler counts JSON arrays, and this is an object.

<!-- exercise: second-cycle -->
```sh
printf '{"records": 1}\n' >"$work/003-bad.json"
mv "$work/003-bad.json" "$root/inbox/"
printf 'four\nlines\n' >"$work/004-c.txt"
mv "$work/004-c.txt" "$root/inbox/"
echo ready >"$root/ready.signal"
```

*Repeat* until the processor has stopped and REARM has run:

<!-- exercise: second-cycle-done repeat -->
```sh
dsl41 query status --brief -J PROCESSOR -S "$S"
# PROCESSOR … FAILURE … run 3 … exit 1 …
dsl41 query status --brief -J REARM -S "$S"
# REARM … SUCCESS … run 3 …
```

The run stopped at the toxic item, and the fourth waits behind it. REARM saw the
`UNKNOWN` row and published no signal: the watch waits for the next delivery.

<!-- exercise: stuck -->
```sh
cat "$RUN_ROOT/logs/PROCESSOR.3.out"
# {"completed": 0, "duplicates": 0, "reconciled": 0, "remaining": 2, "selected": 1, "stopped": true, "unknown": 1}
python -m examples.file_processing.operate --root "$root" list
# […"reason": "ValueError: sample JSON input must be a records array"…"name": "003-bad.json"…"state": "UNKNOWN", "todo": true, "work_key": "…"}, {"attempt": 0, …"name": "004-c.txt"…"state": "READY", "todo": true…}]
dsl41 query status --brief -J INBOX_FW -S "$S"
# INBOX_FW … RUNNING … run 4 …
```

### 6. Reject the toxic delivery and drain the queue

Read the attempt from `list` just before the ruling. Rejection records that no
further attempt will be made; it proves nothing about the business effect.

<!-- exercise: reject -->
```sh
python -m examples.file_processing.operate --root "$root" rule --key 25f9613fabfc0863bf754408d22f83f6353b4f6e91410c5042663634b440a803 \
  --event rejected --expect-state UNKNOWN --expect-attempt 1 --reason "toxic input"
# {…"chart_row": "file_process.09", …"origin": "operator", "prior_state": "UNKNOWN", "reason": "toxic input", "state": "REJECTED"…}
```

`s(ARCHIVER,0)` is false now: the processor ended after the archiver. Run the
processor by hand, one of the contract's activations:

<!-- exercise: drain -->
```sh
dsl41 sendevent FORCE_STARTJOB -J PROCESSOR -S "$S"
# sending: …
# {…"decision": "applied"…"kind": "FORCE_STARTJOB"…}
```

*Repeat* until the processor succeeds:

<!-- exercise: drained repeat -->
```sh
dsl41 query status --brief -J PROCESSOR -S "$S"
# PROCESSOR … SUCCESS … run 4 … exit 0 …
```

Every row is terminal, the handler ran once per row, and the inbox and the TODO
directory are empty:

<!-- exercise: done -->
```sh
python -m examples.file_processing.operate --root "$root" list
# [{"attempt": 1, …"state": "DONE", "todo": false…}, {"attempt": 1, …"state": "DONE", "todo": false…}, {"attempt": 1, …"state": "REJECTED", "todo": false…}, {"attempt": 1, …"state": "DONE", "todo": false…}]
cat "$root/calls"
# e2655ccff9fd0788f8676a41e9f7cb78cd1122fca579a099e8b340b4b702687f
# 0f4e46db9c5790070c51a1033536d8a01a2efe460cae74be8194256fb3176509
# 25f9613fabfc0863bf754408d22f83f6353b4f6e91410c5042663634b440a803
# 79d7f8ee70b83191500c18726ee4dae4fd46c09d75613f93c3ef4c8f14858381
ls "$root/output"
# 0f4e46db9c5790070c51a1033536d8a01a2efe460cae74be8194256fb3176509.json
# 79d7f8ee70b83191500c18726ee4dae4fd46c09d75613f93c3ef4c8f14858381.json
# e2655ccff9fd0788f8676a41e9f7cb78cd1122fca579a099e8b340b4b702687f.json
ls -A "$root/inbox"
ls "$root/todo"
```

### 7. Stop

Stop the engine, then the supervisor, as in step 3.

<!-- exercise: stop-engine-again -->
```sh
kill -TERM "$(cat "$work/engine.pid")"
```

*Repeat* until the engine has printed its reattach line:

<!-- exercise: engine-stopped-again repeat -->
```sh
tail -n 1 "$work/resume.log"
# detached: reattach with `…`
```

<!-- exercise: stop-supervisor-again -->
```sh
kill -TERM "$(dsl41 supervise list --run-root "$RUN_ROOT" | sed 's/.*"supervisor_pid": \([0-9]*\).*/\1/')"
```

*Repeat* until the supervisor is gone:

<!-- exercise: supervisor-stopped-again repeat -->
```sh
dsl41 supervise list --run-root "$RUN_ROOT"
# exit 2: no supervisor at …/supervisor.sock
```

Remove `$work` when you are done.
