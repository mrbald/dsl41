# File processing: slice 1 contract

Slice 1 of [HANDOVER.md](HANDOVER.md) confirms the contract that slices 2 to 6
implement. The rulings below are the owner's where marked, otherwise Fable's,
with Astra as co-advisor. Nothing here changes the prototype code. A ruling
that later proves wrong is superseded here, not edited away.

## Rulings

### Activation is external (owner)

The two command jobs and the existing watcher are the whole scheduler surface.
How the jobs run again is the estate author's choice: a timed archiver sweep,
a rearm, or an operator's manual run. The workers and `add_file_processing_jobs`
assume none of them. The helper keeps `s(watcher)` on the archiver and
`s(archiver,0)` on the processor and documents the zero-lookback edge; it adds
no timer and no rearm.

What the workers promise to any schedule:

- Every invocation is idempotent on a committed ledger. Repeated runs with no
  new input exit 0 and change nothing except repairs of derived files.
- The same role never runs twice at once: `flock` refuses the second run with
  `LockBusy`, exit nonzero. The refusal is never hidden as success.
- Archiver and processor may overlap. Short SQLite write transactions
  arbitrate every shared row; no durable invariant depends on their order.
- Exit 0 means the selected snapshot completed. Exit 1 means the run stopped;
  the ledger holds the reason, except for a ledger busy past its timeout,
  whose reason is on stderr only and whose earlier items stay committed.
  Exit 75 (`EX_TEMPFAIL`) means the role's lock was busy; nothing ran.
  Exit 65 (`EX_DATAERR`) means a refusal: corrupt, inconsistent or
  unsupported state that an operator must look at, whatever Python error
  surfaced it. The next run, however it is started, resumes.
  (Exit codes and the `tmp/` removal below are Fable's slice-2 rulings, not
  the owner's.)
  Exit 2 means the command line or the `--handler` spec could not be loaded, or
  the handler has no `process`; the worker exits before any claim. This line is
  DL-411's, the slice-4 ruling, neither Fable's nor the owner's.
- A dead process leaves at most a stale `RUNNING` claim; the next processor run
  treats it as uncertain before anything else.

The production policy is `Policy(order="oldest", batch="all")` for both roles.
`newest` and `one` are diagnostic settings; they can starve or strand work
under a schedule the contract does not know.

### Failure disposition is `stop` (owner)

The first non-completion ends the processor run. Recovery candidates come
before fresh work. A reconciler-less `UNKNOWN` head, or a handler that answers
`RetrySafe` on every attempt, blocks the queue until an operator rules. That
is deliberate: the queue is ordered, and skipping a stuck item would make the
order a lie.

The operator rules through the existing rows where they reach:
`UNKNOWN + completed -> DONE` with the operator's evidence, or
`UNKNOWN + retry_safe -> READY` with the operator's reason. Two stuck shapes
are out of their reach (Astra F1): a `READY` row whose handler answers
`RetrySafe` on every attempt, and a `RUNNING` row whose object is missing,
because the processor verifies the object before it converts the stale claim.
Slice 3 therefore adds one terminal state (owner-approved), `REJECTED =
READY + rejected` and `REJECTED = UNKNOWN + rejected`, operator-only, and
converts a stale `RUNNING` through row `.04` before it verifies the object.
Without it one toxic delivery would stop the pipeline for good.

Rejection is the operator's decision to make no further attempt. It proves
nothing about the business effect: an `UNKNOWN` row may have completed before
its process died (Astra F7). The row keeps every earlier outcome and its
uncertainty. `RUNNING` is never rejected directly: the operator takes the
lock, converts it through `.04` without requiring readable input, then rules
the `UNKNOWN` row. Neither terminal state has an outgoing move.

`REJECTED` is terminal like `DONE` in every path that today names `DONE`
alone (Astra F6): the rejection and its decision record commit before the TODO
is removed; either worker finishes an interrupted removal; repair never
republishes a terminal row; selection and `remaining` exclude it; a duplicate
delivery preserves it; and a duplicate source may be cleaned once the object
is verified and the row is terminal.

Slice 3 adds the minimal interface: list work with receipt and storage
diagnostics, show one row, rule one row. Every operator mutation takes
`process.lock` and checks the expected state and attempt in the same
transaction, because reconciliation calls the handler outside SQL. Each
transition appends a decision record: work key, attempt, prior and resulting
state, chart row, origin (`handler`, `reconciler`, `operator`), actor, time,
reason and evidence (Astra F4). `_move` stops overwriting earlier outcomes.
The attempt count increments with the claim.

The archiver's refusals are not work states and no ruling repairs them:
a reused basename, a missing or corrupt committed object, a damaged TODO,
a lost ledger. The interface documents the procedure for each (quarantine the
delivery, restore the object from backup and verify, delete and let the
archiver republish, restore the ledger); it does not automate them. A storage
repair holds both `archive.lock` and `process.lock` for its whole duration,
because either role can read what it changes (Astra F1).

### Work age

Admission sequence is the only age. Nothing expires, nothing is reclaimed:
objects, receipts and DONE rows persist. Retention and garbage collection are
outside this contract and need their own policy input before they exist.

### Filesystem (owner: do not assume one filesystem)

`inbox/`, `objects/`, `todo/` and the ledger may be separate mounts.
Therefore:

- `inbox/` must be readable, traversable and deletable by the worker UID, and
  its directory must accept `fsync` (Astra F3). The archiver copies from it
  and unlinks the delivery after handover. It never renames or links out of it,
  and it never writes a file into it.
- Each published directory stages inside itself. Slice 2 moves object staging
  to `objects/.tmp` and TODO staging to `todo/.tmp`, so every publication is a
  hard link within one directory. `tmp/` stops being a shared staging area.
  The move changes enumeration, abandoned-stage cleanup and lost-ledger
  detection together (Astra F2): the processor skips the reserved `.tmp` name,
  `_repair` cleans both staging directories, and an empty staging directory
  is not evidence of an established root.
- The ledger and the two lock files live together on a filesystem with working
  SQLite locking and `flock`. Local storage is the qualified case. NFS and SMB
  are not qualified; the contract does not forbid them, it refuses to vouch.
- Required of the object and TODO filesystems: `mkstemp`, hard links within
  a directory, file and directory `fsync`, `O_NOFOLLOW`, `O_DIRECTORY`,
  `lstat`/`fstat` with coherent regular-file reads. Mode `0o444` on objects is
  a convention, not protection; the owning UID can change it.
- Required of the ledger directory: writable for SQLite sidecars, stable lock
  files, reliable `flock` and SQLite commit semantics. A configurable ledger
  location syncs its real parent; `fsync` of the root does not reach a mounted
  child.
- The handler's output location is the handler's contract.

One host, one UID, a privately managed root. Nothing is shared with another
host.

## Invariants

The code enforces these; slices 2 and 3 keep them and test each at its
durable boundary.

1. A delivery basename is unique forever. Reuse after cleanup is refused.
2. One object per content hash. Objects are immutable and verified before any
   reuse or cleanup.
3. One work row per `(processor_id, content_hash)`. Duplicate deliveries add
   receipts and never reset the row, its sequence, metadata, state or evidence.
4. Every persisted state is the executor's leaf for the declared chart. No
   parallel transition table exists.
5. The `RUNNING` claim is durable before the handler is called.
6. `DONE` is terminal. Its evidence outlives the TODO file.
7. The inbox source stays until the object is verified and the handover is
   complete: a published TODO, or a durable terminal row (`DONE` or
   `REJECTED`) for an already decided duplicate.
8. `UNKNOWN` work is never handed to `process`. Without a reconciler it stays
   `UNKNOWN`.
9. A missing ledger in an established root is refused, never recreated.

## Stated limits

- A reused basename stops the archiver until an operator intervenes. A
  missing or corrupt object is detected at three points only: archiver cleanup
  of an uncleaned receipt, archival of a duplicate that finds the object in
  place, and processor selection of its work (Astra F5). An idle archiver does
  not scan objects; its `remaining=0` says nothing about them. The code
  detects; it does not repair.
- Exit 0 can coexist with backlog. Exit 1 says the run stopped, not that a
  retry is safe. Transactions hold filesystem I/O; "short" is a design rule,
  not a latency bound.
- A stuck item blocks the queue under `stop` until an operator rules.
- No retention, no garbage collection, no schema migration.
- One processor identity per root, fixed for the root's life.
- No power-loss qualification beyond `fsync` at each durable boundary.
- No HA, no leases, no second host.
- The zero-lookback edge (SEM-04) is the estate author's to close with the
  activation they choose.

## Review record

Astra co-advised on 2026-10-25: R1 agreed; R2, R3 and R4 amended by F1 to F5,
all reproduced and all accepted above; the confirmation round added F6 and F7
on `REJECTED`, both accepted. The brief, report and reconciliation
are kept with the programme's review records, outside the repository.

Slice 3 review (2026-10-26): one Opus reviewer and one Astra pass, three
rounds. Round 1: Opus O1 to O10 and Astra F1 to F3 (F1 = O2, F3 = O6), all
fixed; F2 (an unsupported ledger schema was mutated, then crashed) became the
schema check in `_open`. Round 2: all confirmed by their raisers; Opus R2-1
(a hot journal made operator reads refuse with the generic message) fixed with
a distinct refusal. Round 3: R2-1 confirmed. No stated limit was added; the
slice 2 ledger-corruption limit is closed. Material findings open: 0.
