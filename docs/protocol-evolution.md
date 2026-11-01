# Protocol evolution — how a dialect enters service, and how it leaves

Status: **normative (DL-138).** Entries that rule parts of it include
DL-144, DL-146, DL-147, DL-150, DL-151, DL-157, DL-158, DL-168, DL-170,
DL-217, DL-227, DL-252, DL-263 and DL-401. A change to what this document
fixes requires a decision-log entry. Each entry is cited where it applies.

For the architect: this page fixes format tolerance, version entry and
retirement for runner protocols and durable evidence. The
[architect track](../README.md#documents) links the runtime contracts.
The [wrapper protocol](wrapper-protocol.md),
[control protocol](control-protocol.md) and [access model](access-model.md)
own their wire rules. [Runner design §7](runner-design.md#7-journal-and-recovery-e1-prod-grade)
owns recovery; this page does not define an upgrade procedure.

## 0. Why a contract and not a habit

A version field is not a compatibility promise. It records which promise was
made. Two separate questions decide every evolution step:

- **Compatibility**: what a reader does with an input it does not fully
  understand. An unknown **field** and an unsupported **version** are
  separate cases, and a protocol may answer them differently. Most do:
  tolerant rows ignore an unknown field and still refuse an unsupported
  version.
- **Lifetime**: how long an instance of the dialect can still be met. A wire
  request ends when it is answered. A durable artifact ends when the last
  **retained** copy is gone (§3 scopes that word). Operator policy can keep evidence indefinitely.

Retirement needs both answers. A dialect may be retired only when no
instance of it remains (§3), and the lifetime column decides when that is
true. For a wire row it is true as soon as the door refuses the dialect. For
a durable row it is true only when no retained root holds an instance. A
copy that arrives later from outside the retained set meets a tombstone, not
silence (§6).

The two columns stay separate. A tolerant reader is not necessarily a
long-lived one, and a long-lived artifact is not necessarily read
tolerantly. Merging the columns leads to two errors: dropping a reader while
instances are still on disk, and keeping a reader for a wire that closed
long ago.

## 1. The matrix

**One row per tolerance rule, not one row per file.** Two artifacts that a
reader treats the same share a row. Two artifacts in one directory that a
reader treats differently are two rows. A dispatcher test is written against
a row.

| Protocol / artifact set | Producer → reader | Discriminator | Unknown fields | Unsupported / absent version | Lifetime |
| --- | --- | --- | --- | --- | --- |
| SQLite store | `SqliteStore` → store readers | `meta.schema_version = 1` | Each record's own schema; this is not a field-extension promise | Foreign or missing schema refuses | Retained root |
| Engine state and snapshots | Engine → startup and replay | `meta.state_version`, snapshot `state_version`; `STATE_MACHINE_VERSION` | Each payload's schema | Foreign state refuses; no default or migration (DL-424, DL-433) | Retained store and snapshots |
| Store journal records | `Journal` → replay and history | `Record.kind`; input event `kind` | As the record reader declares | No per-record version; unknown record and event kinds refuse by name | Snapshot and retention floor (DL-428) |
| FW evidence | `FileWatcherAdapter` → `read_watch_log` | `artifact_format_version` on every `watch.jsonl` line | Ignored | Unsupported or absent refuses | Retained spool |
| Wrapper-owned spool files | Wrapper → adapter and startup (`spool_version_supported`) | `version = 1` on `spawn.json`, `status.json` | Ignored | Unsupported refuses; absent passes (DL-227) | Retained attempt directory |
| Wrapper input spec | Adapter → wrapper | `version = 1` | Refused by contract (§2 of wrapper protocol) | Unsupported refuses at the wrapper, exit 2; absent version also exits 2 | One launch |
| Perimeter journal | Access perimeter → audit; sequence recovery reads `access_seq` | `rec`; no version | Ignored | Not applicable; no engine decision dispatches it (DL-147) | Retained root |
| Access role map | Operator → `runner_access` loader | `format_version` | Refused | Unsupported or absent refuses | Operator's retained map |
| Stored configurations | Startup/update → `Configuration.from_bytes` | `configuration_format_version`, 1 or 2 | Refused for both | Unsupported refuses; absent selects supported version 1 (DL-437) | Retained `configs` rows |
| Reload plans | `reread` → `read_plan` and update | `plan_format_version`, 1 or 2; `artifact_format_version` (PR-08d) | Ignored | Unsupported plan version refuses by name; absent plan version selects supported version 1 (DL-437). Unsupported artifact version makes `read_plan` return no plan; absent artifact version passes | Retained plan; an interrupted update requires it |
| Main-config input mapping | `main_config.prepare` → `from_parts` | `format_version = 1` in `main-inputs.json` | Ignored | Unsupported or absent refuses (DL-437) | Retained plan or stored capture |
| Main config | Site → `load_config` | `format_version = 1` | Refused; restart host loading has the limit below | Unsupported or absent refuses (DL-437) | Site's config file |
| Control socket | Client → `ControlServer` | `v` on every request | Ignored | Unsupported or absent refuses, connection stays open | One request; `subscribe` lasts to hangup |

A durable format can retire only when no retained instance needs its reader
(§3). Wire retirement needs the door to refuse the old dialect. SQLite
schema and engine state are distinct gates: a schema-compatible store can
still carry state this executable cannot interpret. One executable handles
one state version (DL-424); drain with the old binary before changing that
version, then use a fresh root. No snapshot migration is provided.

`root_id` is mandatory store identity, not a format version (DL-433).
Startup refuses a store without it. Clock domain is also checked separately.
A new binary version alone does not change the catalog or runtime hashes.

### Notes on the rows

**Store and wire are different formats (DL-428).** Store kinds are
`input`, `decision`, `dispatch`, `dispatch_pending`, `outcome`, `follow_up`,
`drop`, `preflight`, `config` and `engine`. The stream projects `segment`
from an applied configuration and `advance` from a verbless input.
[Control protocol §5](control-protocol.md#5-streaming-verb-subscribe) defines the stream;
its admission `seq` is not the SQLite row sequence. `check_record` refuses
unknown store kinds, engine/config events, outcome kinds and plan kinds.
The input event alphabet is strict too (DL-158).

**Main-config captures and plans (DL-437).** A configuration with captured
sources uses version 2; the unversioned legacy row reads as version 1.
Both reject unknown top-level fields. Main-config plans use version 2;
the unversioned legacy plan reads as version 1. `main-inputs.json` binds
config bytes, environment, lint policy, source mapping, membership and part
names. Each Python part retains its generated JIL. Every part has a SHA-256
in `plan.json`, published last. Update validates the token and captured
bytes and executes generated JIL; it never rereads sources or imports Python.
Stored configuration rows retain the capture for replay and restart.

The main config selects one engine through `/etc/dsl41/dsl41.toml` or an
absolute `--config`. Paths anchor at that file. Ordinary globs expand in
sorted order; `**`, unreadable inputs, duplicate selections and duplicate
jobs refuse. JIL and `estate()` inputs compile together. `lint.refuse` is
`none` (default), `error` or `warn`; it considers unignored findings.
Input, lowering and preflight errors remain hard errors. A rehearsal root
must neither equal nor contain the engine root, nor lie inside it.

Main-config restart restores the last applied revision. Estate, lint and
`engine.environment` edits stay pending. Disk host identity,
`machine_policy` and the access map apply at launch; invalid values exit 2.
A missing interrupted-update plan refuses before writes or signals.
Implementation limits recorded by DL-437 remain: restart recompiles captured
bytes, runs no preflight, offers no legacy-root conversion, and ignores
unknown `[engine]` keys in its host-only load; the pending scan blocks the
loop. These are limits, not permission to change the rules in this pass.

**Wrapper evidence and input.** Spool readers ignore unknown fields but
reject a present version other than the integer 1; booleans and floats do
not count (DL-151). Only the two wrapper spool files allow an absent
`version` (DL-157, DL-227). Stored configurations and plans separately
support an absent discriminator as legacy version 1 (DL-437).
Unread or mismatched restart evidence is quarantined under DL-433;
missing evidence follows the interruption rule, not a guessed outcome.

The wrapper input schema is frozen (DL-150). Its field/type restrictions
remain [wrapper protocol §2](wrapper-protocol.md#2-wrapper-input-spec-frozen).
The adapter launches the wrapper by file path; the wrapper reads stdin
once and replaces it with `/dev/null`. A refused spec writes no spawn
record. A spec-version change requires coordinating the adapter and wrapper.

**Socket additions.** Unknown answer fields are ignored. An added field
that leaves reply classification unchanged needs no new dialect (DL-217).
A field that changes classification needs a new dialect. Version is per
request, not per connection: a refusal ends one request, then the next line
is read normally. `subscribe` owns its connection until hangup.

**Excluded artifact families.** DL-424 excludes closed estate artifacts,
supervisor receipts, maintenance records, leases and the supervisor socket.
DL-424's RETIRED list is the pointer for supervisor-protocol §§2, 3 and 5,
period-model §§1.1, 2, 3.2, 3.5 and 12a, and deployment-runbook §2a.
DL-401 defines the excluded maintenance-record dialect. These are
decision-log pointers, not required recovery inputs.

## 2. Entering service

A new dialect enters in four steps, in this order.

1. **Introduce.** The version is defined, and its readers gain
   **dual-read**: they accept both the current dialect and the new one.
   Nothing writes the new one yet.
2. **Dual-read overlap, with positive compatibility tests.** Both versions
   are read, and each round-trip is pinned. The tests are positive: they
   assert that the old dialect is still read correctly, not only that the
   new one is. A suite of negative tests alone still passes an
   implementation that has dropped the old reader.
3. **Writer switch.** The writer emits the new dialect. **New instances
   only**: nothing rewrites an existing instance.
4. **Retire.** The old dialect leaves under §3's gate. That is usually much
   later, and sometimes never.

Steps 1 to 3 may land in one release. **On a durable row, step 4 may not
join them** (DL-150). At step 3 every old instance still exists, so the
retirement gate cannot be met.

**On a wire row it may.** Nothing durable holds a wire dialect, so the gate
is met by construction (§3). The only remaining question is whether any
client still sends the old version. That is a deployment question, not a
retention one. It can be answered in one release when the clients ship with
the server. It cannot be answered when they do not, so the four steps stay
the default for wire rows too.

The reset clause (§5) is the only way to bypass this lifecycle, and only
under its stated condition.

## 3. Retiring

**The gate is actual absence, not eligibility.**

> A dialect may be retired when **no instance of it exists** — not when every
> instance has become eligible for deletion.

A socket row meets the gate **by construction**. Nothing durable carries a
wire dialect. An instance is one request, gone once it is answered, or one
`subscribe` stream, gone at hangup. There is no instance to be absent, so
retirement needs only the door refusing the version.

For a durable row, the gate means every instance is gone from every root
or file the operator keeps. Held evidence is present. Prunable but present
evidence is present too; eligibility to delete is not deletion (DL-150).

**"Exists" is scoped to what the operator keeps** (DL-150). A copy that has
left the retained set is not an instance this gate can see: an archive tape,
a colleague's laptop, a root restored from a backup years later. Waiting for
such copies would mean never retiring anything. §6 covers them: the gate
covers the retained set, and the tombstone covers everything else.

Retirement is implemented as **refusal by name**, never by deleting the
reader alone. §6 says how.

## 4. Migration is not this contract's mechanism

This contract has two operations: dual-read and retire. Migration is
neither.

A transformation needs its own decision-log entry and verification proof.
No in-place rewrite of immutable digest-bound evidence is permitted.
Engine state-version changes use the drain and fresh-root rule in §1
(DL-424), not a snapshot migration.

## 5. The reset clause

> While **nothing runs in production**, a decision-log entry may declare a
> **pre-production reset**: a dialect is retired without §2's lifecycle, and
> every artifact written before the reset becomes unreadable.

The clause skips the lifecycle, not the §3 gate (DL-150).

The clause is sound only under its condition. With no estate anywhere, the
§3 gate is met trivially: there is no instance to be absent. §2's lifecycle
then has nothing to protect. §2 keeps an old reader working while old
instances are still met; with none, its four steps protect nobody and reduce
to the last one.

**"Nothing runs in production" is the licence; actual absence is still what
the gate requires.** These are two separate claims, and the entry must
supply both. No production estate makes it credible that no instance was
ever written. It does not by itself prove it. An entry that shows only the
first has shown that no estate would notice, not that there is nothing to
find.

DL-138 supplies the retirement ruling for the dialects listed in §8.

A reset makes pre-reset roots unreadable. That is its cost, and the entry
that takes it states it.

## 6. Tombstone registries

A retired dialect gets a **tombstone**, not a deleted reader. The difference
is what the operator sees. A tombstone names the dialect and the
decision-log entry that retired it. A deleted reader produces a generic
parse error, or silence.

The rules:

- **Owner-local.** The registry lives with the reader that owns the
  question. There is no central table of retired things. A central table
  must be consulted by readers that have no other reason to know about it,
  and it drifts from the readers that do.
- **Append-only.** A dialect is added when it is retired. A row is never
  removed and never reused, because a root written before the retirement can
  still arrive on an operator's disk.
- **Refusal by name, with the decision-log citation.** The message names the
  dialect and the entry. "Unknown record kind" is not a tombstone.
- **A tombstone is not the unknown case.** A value that is neither current
  nor in the registry is refused too, but as an unknown, with its own
  distinct error. Merging the two loses the difference between "this used to
  be legal" and "this was never legal", which is the difference between an
  old root and a corrupt one.

A **registry** is the form a tombstone takes when the discriminator has many
values (DL-150): a table beside the reader, one row per retired value. When
the discriminator has two values, such as a boolean field whose one legal
value is `false`, the refusal lives in that field's validator and no
table is built. A table with one row is a table nobody consults. The four
rules above still apply: the message names the value and its entry, and the
malformed case keeps its own distinct error.

## 7. What every evolution event owes

One decision-log entry, plus **dispatcher tests on every affected row**:

1. the current dialect is accepted;
2. every retired dialect is refused **by name**, naming its retiring entry;
3. an unknown **field** follows the row's tolerance rule: ignored on a
   tolerant row, refused on a strict one;
4. an unsupported **version** is refused, on every affected row that has a
   version.

Cases 3 and 4 are tested separately. A single test with a message that has
both an unknown field and an unknown version proves neither.

The perimeter journal has no version and is exempt from case 4 by
construction: no engine dispatches it (DL-147, DL-150). A row may join
that exception only the same way: by an entry showing that nothing reads
the artifact for a decision. "We did not add one" is not a construction.

Store records meet case 4 through the store row's schema and state-version
tests (DL-428); they are not a case-4 exception.

## 8. The DL-138 retirement

DL-138 and DL-150 define the named refusals for `header`, `result`,
standalone `effect`, `legacy_batch: true`, catalog hash version 1,
the `manifest/` layout and the `adopting` anchor state.
DL-424 excludes the JSONL/period artifact families; DL-428 refuses legacy
roots with no SQLite store. These pointers preserve the dialect names;
they do not promise that the SQLite reader accepts a legacy root.

Citation pointers:

- DL-135 — retention eligibility is distinct from absence (§3, DL-150).
- E7 — interrupted attempts follow runner-design §7 (DL-424, DL-433).
- PR-08d — `canon.check_artifact_version` gates watch lines, reload plans and store payloads; see §1.
