# File processing: design and implementation handover

This is the historical handover, written before slice 1. CONTRACT.md and README.md
describe the current state.

This is an experimental design expressed in Python. It is not a qualified release.
The intended caller is an estate author using `dsl41.dsl.CatalogBuilder`.
The next phase is to settle the remaining protocol choices and implement bounded
production slices. Do not infer deployment readiness from the focused tests.

## Scope

Provide a reusable archiver and processor that can also run independently.
The producer finishes its transfer and publishes an immutable delivery under a
unique inbox basename before our work begins. Repeated original filenames are
allowed; the delivery basename is unique and is never reused.

The normal estate has one existing inbox file watcher and two command jobs.
The watcher starts the archiver. The archiver durably prepares processing work
before returning success. The processor condition is `s(archiver,0)`.
The TODO file is the data handover, not another file-watcher trigger.
Do not introduce a second watcher or replace the condition with bare `s(archiver)`.

The archiver owns receipt identity, byte hashing, content deduplication, archival,
TODO preparation and inbox cleanup. The processor owns work selection, claims,
business effects, reconciliation and completion evidence. Business-invalid input
is still archived. Processing must not modify the archive object.

The deferred task in [TODO.md](../../TODO.md) is to improve decompiled Python
estates as runnable, maintainable migration starting points. Do not implement
decompiler recognition or a new Python catalog loader in this slice.
DL-03, DL-17 and DL-38 continue to govern the DSL and its closed fold registry.

## Proposed implementation boundary

Use the existing `Statechart` and `Executor` with flat declarations.
The declaration selects every state transition that is persisted. Do not keep
a parallel transition table or imperative status switch beside it.
The executor is independent of the runner and imports only the standard library.
Its `restore()` method reinstates an explicit configuration without entry actions.
It does not make application writes durable or transactional.

Do not revive the stashed generator FSM. Its in-process continuation state is
unnecessary here. If syntax later needs shortening, produce the existing
declarations through a small authoring helper rather than another interpreter.

Keep application worker functionality separate from compiler and scheduler
semantics. Python composition uses ordinary explicit `CatalogBuilder.job()` calls.
The command chooses an application dependency on the worker library. The compiler
must not inject a runtime dependency into unrelated emitted artifacts.

The proposed Python surface has two separate imports:

```python
from dsl41.file_processing import Policy, archive, process
from dsl41.file_processing_dsl import add_file_processing_jobs
```

`archive(root, processor_id, policy)` and
`process(root, processor_id, handler, policy)` are ordinary callable workers.
The handler supplies `process(WorkItem)` and optionally `reconcile(WorkItem)`.
`Completed`, `RetrySafe` and `Unknown` make the outcome contract explicit.
`WorkItem` supplies `input_path`, `processing_key`, `content_hash`,
`receipt_sequence` and immutable metadata. Result evidence is currently a nonempty
string; the handler is responsible for its truth and durable meaning.

`add_file_processing_jobs(builder, ...)` takes an existing watcher name, two
command argv sequences, machine and optional job names. It adds two ordinary CMD
jobs. It does not create a watcher or invent a compiler primitive. The command
argv selects the interpreter and application worker explicitly.

The sample estate demonstrates this composition and can emit JIL for the current
runner CLI. A future direct Python execution interface is deliberately deferred.

## Identity and storage proposal

The prototype uses a single local root, a SQLite ledger, immutable archive
objects and JSON TODO files. No external database or new dependency is required.
This is a proposed implementation, not a frozen public API or storage format.

| Identity | Scope and purpose |
| --- | --- |
| Delivery identity | Unique published inbox basename; repeated scans find the same receipt |
| Content identity | SHA-256 of the complete raw bytes; one archive object per content |
| Processing identity | Processor identity plus content identity; one logical effect |

Processor identity may contain an explicit business revision when reprocessing
is intended. A root must not silently change processor identity. Hash-only
deduplication assumes filenames do not affect business meaning. Preserve supplied
names as receipt metadata; never reconstruct a lost original name by guessing.

SQLite owns receipt and processing progress. TODO files are the recoverable
publication of committed work. This duplication has one purpose: the user-visible
file handover must survive independently of a scheduler attempt, while related
metadata can commit together.

The archive commitment order is:

1. Register a delivery's stable admission order before selecting work.
2. Copy to private staging while hashing the same stream.
3. Flush and publish an immutable object. Verify an existing object before reuse.
4. Commit the archived receipt and insert processing work only if absent.
5. Publish a complete TODO atomically from that committed work.
6. Verify the archived object again, then retire the inbox delivery after handover
   publication. If the object is missing or corrupt, retain the inbox copy.

A later archiver invocation must repair missing TODOs even with an empty inbox.
Publication and processor completion must coordinate so a stale publisher cannot
recreate completed work. Completion evidence outlives TODO removal. Duplicate
deliveries preserve all receipts without resetting an existing work item.
Symlinks, if added, are rebuildable views and never the authoritative queue.
Both workers refuse to initialize a missing ledger when objects, TODOs, staging
files or SQLite sidecars remain. This detects a lost ledger; it cannot reconstruct
completion evidence. If all recognizable evidence is also lost, the prototype
cannot distinguish that root from a new one.

## State vocabulary

The prototype declares these transitions:

```text
FILE_ARCHIVED = FILE_RECEIVED + archive_committed // object and metadata are durable

RUNNING = READY + claim // persist ownership before invoking business code
DONE = RUNNING + completed // confirmed durable business result
READY = RUNNING + retry_safe // an explicit claim that replay is safe
UNKNOWN = RUNNING + uncertain // effect may have occurred
DONE = UNKNOWN + completed // reconciliation verifies the result
READY = UNKNOWN + retry_safe // reconciliation permits another attempt
UNKNOWN = UNKNOWN + uncertain // no blind replay
```

Repeated invocations find terminal work in the ledger and skip the business
operation. They may repair derived publication or cleanup. The charts do not need
another move to represent that skip. `FILE_ARCHIVED` becomes durable with the
receipt/work transaction; TODO publication follows and can be retried separately.

Copying, hashing and linking do not each need a state. Add a durable phase only
when recovery requires a different decision. Per-process variables may track
temporary operations, but they cannot be the only evidence needed after a crash.

## Processing contract

The handler receives an immutable archive path, content identity, processing
identity and stable work metadata. The generic wrapper owns selection and claims.

A normal completion must supply durable result evidence. An unexpected exception
does not prove that no effect occurred. Recover interrupted attempts through the
handler's reconciliation method. With no evidence or safe-retry contract, retain
UNKNOWN. An arbitrary command and a local done marker cannot provide exactly-once
external effects.

The synthetic handler can safely reconcile because its only public effect is an
immutable result artifact under the processing key. A database or remote-service
handler needs its own transaction, destination idempotency or reconciliation
contract. Do not generalize the file-output demonstration to those destinations.

## Selection and activation

Each job has independent `order: oldest|newest` and `batch: one|all` settings.
Archive one means one delivery. Process one means one processing item.
All means a finite set selected at invocation start, processed sequentially.
New arrivals wait for the next invocation. Receipts receive a durable admission
sequence; names sort within each discovery snapshot. This is not physical arrival
time. The prototype preserves the sequence of the receipt that first creates
the work row. Under newest-first archival, an older duplicate discovered later
does not retroactively reorder that work. Duplicates must not refresh queue age.

Recovery of a selected uncertain processing item counts towards its batch limit.
Do not reconcile one item and then silently process another under `batch: one`.
The prototype reconciles uncertain work before selecting fresh work and stops on
the first non-completion. It has no terminal rejection state or quarantine policy.
Decide whether that behavior fits production failure handling. Newest-first can starve
old work under sustained input; a fairness guarantee needs an additional policy.
`Summary.remaining` counts admitted ledger items. Arrivals outside the snapshot
can be unregistered, so zero does not prove that the inbox is empty.

The scheduler condition is a freshness predicate, not a queued notification:

```text
condition: s(archiver,0)

archiver.status == SUCCESS
archiver.last_end_at >= processor.last_end_at
```

On the processor's first run there is no end anchor. A prior archiver success can
qualify. If archiving succeeds while the processor runs, that success can predate
the processor's eventual end. A later evaluation then sees a stale condition.
A failed processor likewise needs an explicit safe retry activation.

**Required production decision:** define recurrent activation that drains pending
work and retries recoverable failures while retaining these two command jobs and
one existing watcher. A recurring archiver sweep that may succeed with no new
delivery can refresh the condition after the processor ends. One candidate is
to use the existing watcher once to admit the service, leave its success latched,
and schedule archiver sweeps thereafter. This trades per-arrival activation for
the sweep interval. Confirm that tradeoff before adopting it. A timer on the
processor alone still cannot satisfy a false zero-lookback condition.

Prove the selected schedule through the oracle and actual worker invocations,
including resource-queued and held jobs. Do not replace the wakeup gap with a
polling loop that hides multiple invocations or disregards the configured batch
limit. The prototype does not implement or qualify this recurrent mode.

Current FW support checks a literal pathname and size stability. It does not
enumerate unique directory arrivals. The producer signal and rearming protocol
also need an explicit decision for continuous service. The prototype's single
cycle must not be presented as a continuously rearmed watcher.

## Production plan

| Slice | Work and acceptance | Allocation |
| --- | --- | --- |
| 1. Confirm contract | Set activation, failure eligibility, work age and supported filesystem assumptions. Declare invariants and unresolved limits. | Fable leads decisions; Astra advises on the hard cases |
| 2. Archive protocol | Qualify copy/publication/ledger/TODO/cleanup ordering. Verify duplicates, concurrent roles and interruption at every durable boundary. | Bounded Python implementation; required independent review under repository policy |
| 3. Processing contract | Qualify claim, evidence, UNKNOWN and reconciliation. Add an independent side-effect checker for each supported handler class. | Implementation plus semantic review appropriate to the final scope |
| 4. Python authoring | Set the supported module/API location, command deployment and two-job composition. Preserve the existing compiler path and condition semantics. | Python edge implementation; parent reviews API and usability |
| 5. Scheduler composition | Exercise repeated arrivals, batch limits, long processing, failures, holds, resource waits and missed wakes through the real runner. | Integration implementation and independent composition review |
| 6. Qualification | Run full repository gates and required adversarial reviews. Bind results to exact code and artifacts; document operational limits. | Normal repository workflow, unless the user changes it |

The prototype's lightweight workflow exception does not waive production gates.
Use the current repository instructions when implementing these slices. The user
will launch Fable CLI and use Astra as a co-advisor/co-reviewer. Do not infer that
an external reviewer has run from this handover.

## Prototype evidence

The user authorized Sol-6.1 at xhigh as coder and Astra as design owner/reviewer,
using focused checks and fast iteration. No cross-vendor production review was
requested for this code-as-documentation branch.

The branch is `prototype/file-processing-python`, based on the already-published
`supervisor-ops` commit `81479cc742ec77fcf556a08d9c4fb35fc3fca10c`. It has not been
merged into another branch. The parent selected `gpt-6.1-sol` with `xhigh` through
`codex exec`; the CLI output does not independently identify the served model.

Implementation entry points:

- [Worker API](../../src/dsl41/file_processing.py): archive, process and handler contracts.
- [Charts](../../src/dsl41/file_processing_charts.py): flat declarations using the existing executor.
- [DSL helper](../../src/dsl41/file_processing_dsl.py): two ordinary CMD jobs on an existing FW.
- [Python estate](estate.py), [sample handler](worker.py) and [demo](demo.py).
- [Protocol and commands](README.md), [worker tests](../../tests/test_file_processing.py)
  and [composition tests](../../tests/test_file_processing_estate.py).

Sol ran the following checks in the locked worktree environment on 2026-10-25.
Astra inspected the code, tests and command results without repeating the builder's gates.

```sh
.venv/bin/python -m pytest -q tests/test_file_processing.py tests/test_file_processing_estate.py
.venv/bin/ruff check src/dsl41/file_processing*.py examples/file_processing/*.py tests/test_file_processing*.py
.venv/bin/ruff format --check src/dsl41/file_processing*.py examples/file_processing/*.py tests/test_file_processing*.py
MYPYPATH=src:. .venv/bin/mypy --explicit-package-bases src/dsl41/file_processing.py src/dsl41/file_processing_charts.py src/dsl41/file_processing_dsl.py examples/file_processing
```

Results: **47 tests passed**, Ruff passed, all nine Python files were formatted,
and mypy passed for seven runtime/example files. The Ruff globs above expand to
the same explicit paths used in the recorded run. The final suite executes the
demo and checks four receipts, three archive objects, three completed outputs and
zero TODOs. It separately verifies object hashes and output identity/counts.
A one-item processor invocation leaves two items; a later explicit call drains them.

Generating JIL through `examples.file_processing.estate` and running
`dsl41 decompile --check` also passed after the helper moved into the package.
The zero-lookback edge remains explicit. The worker-import test verifies that
compiler and scheduler modules are absent from the worker process.

Astra's initial review reproduced two recovery defects: loss of the ledger could
repeat a completed effect, and cleanup could discard the last correct source
when its committed object was missing. Sol fixed both. Astra confirmed the fixes
by inspecting the refusal paths and the passing regression evidence. The tests
check actual effect count, retained source bytes and absence of a replacement
ledger. Astra also required the reusable DSL helper to live in the package.
No material finding remains open within this experimental scope.

Archive interruption tests use child `os._exit` at object publication,
receipt/work commitment and TODO publication. Lock-release tests also use an
abrupt child exit. The effect-before-completion test uses `SystemExit` after a real
sample output appears; it verifies reconciliation without replacing that output.
These are distinct probes. They do not qualify power loss or arbitrary effects.

The full repository suite, production review gates and actual runner/supervisor
integration were not run under this prototype-only authorization. Since then,
slices 1 to 4 were built (DL-404, DL-411). DL-417 runs the example estate under
the real engine and supervisor through the README's exercise, with REARM as the
example's activation; batch limits, resource waits and missed wakes stay
unexercised there, and the operator interface beyond list, show and rule stays
manual. Other limits are one local POSIX host, fixed processor
identity/configuration per root, no schema migration and no retention/GC policy.
