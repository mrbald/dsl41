# Protocol evolution — how a dialect enters service, and how it leaves

Status: **normative (2026-08-21, DL-138).** This document is the contract that
every versioned protocol and every durable artifact in the runner is held to.
It answers four questions: what each protocol tolerates, how long an instance
of it can still arrive, how a new dialect enters service, and what must be true
before an old one is retired. *(Amended by DL-150, a conformance round against
the shipped readers, and by DL-151, which paid the code debts that round
recorded; every amendment below carries its own marker.)*

It invents no rule for any protocol. Each tolerance rule lives in the document
that defines it — `docs/control-protocol.md` §2, `docs/supervisor-protocol.md`
§2, §3 and §5, `docs/period-model.md` §1.1, §2, §3.2, §3.5 and §12a,
`docs/access-model.md` §4 and §6 (DL-147). This document collects them per
protocol, so that a change to one can be argued against the whole set instead
of against the one reader the author had open.

## 0. Why a contract and not a habit

A version field is not a compatibility promise. It is a place to record which
promise was made. Two questions decide every evolution step, and they are not
the same question:

- **Compatibility** — what a reader does with an input it does not fully
  understand. An unknown **field** and an unsupported **version** are separate
  cases. A protocol may answer them differently, and most do: tolerant rows
  ignore an unknown field and still refuse an unsupported version.
- **Lifetime** — how long an instance of the dialect can still be met. A wire
  request ends when it is answered. A durable artifact ends when the last
  **retained** copy is gone (§3 scopes that word), and under
  `deployment-runbook.md` §2a that may be never.

Retirement needs both answers. A dialect may be retired only when no
instance of it remains (§3), and the lifetime column is what decides when that
is true. For a wire row that is true as soon as the door refuses it. For a
durable row it is true only when no retained root holds one. A copy arriving
later from outside the retained set meets a tombstone, not silence (§6).

The columns are separate for the same reason. A tolerant reader is not a
long-lived one, and a long-lived artifact is not automatically a tolerant one.
Collapsing the two produces the two classic errors: dropping a reader while
instances are still on disk, and keeping a reader forever for a wire that
closed years ago.

## 1. The matrix

**One row per tolerance rule, not one row per file.** Two artifacts that a
reader treats identically share a row. Two artifacts in one directory that a
reader treats differently are two rows. The row is what a dispatcher test is
written against.

| protocol / artifact set | discriminator | unknown FIELDS | unsupported VERSIONS | lifetime | retirement precondition |
| --- | --- | --- | --- | --- | --- |
| **WAL journal records** — `docs/period-model.md` §2, `docs/runner-design.md` §7 | the record's `rec` kind, plus the `catalog_hash_version` and `state_machine_version` of the `segment` that opens the file. Those two ride on closed artifacts as well, and their own gates are wider than this row's — see the two notes below | as each record's own schema declares; this contract does not change any of them. The **kind** is dispatched strictly: a current kind proceeds, a retired kind refuses by name, an unknown kind refuses by name | refused | the retention lifetime of the root that holds the segment | no retained segment holds one |
| **Closed estate artifacts** — the seal sidecar, the attestation, the period manifest, `staged_manifest.json`, `candidate.json`, `anchor.json`, the claim file, the sentinel (`docs/period-model.md` §3.2), and the archive receipt `seals/<period>.archive.json` (§12a, DL-144) | `artifact_format_version` | refused. §3.2 puts every typed field on the wire, so an unknown field is corruption and not an extension | refused, naming the version (PR-08d) | the retention lifetime of the estate; period-model §12's floor keeps several of them for the life of the lineage, and §12a's three — the archive receipt, the attestation and the seal sidecar of an archived period — may never be pruned by any class | no retained instance |
| **Tolerant estate files** — `sources.json` (`docs/period-model.md` §1.1), and every `watch.jsonl` line written by the FW adapter (§3.5) | `artifact_format_version` | ignored — the reader takes the fields it needs; §3.2's canonical form binds the writer, not the reader | refused, naming the version (PR-08d) | `sources.json` as durable as its catalog bundle; a watch line as durable as the run spool that holds it | no retained instance |
| **Tolerant supervisor artifacts** — `receipt.json`, `reply.json`, the `run_id` index entry (`docs/supervisor-protocol.md` §3) | `artifact_format_version` | ignored — the section's own forward-compatibility rule | refused | as durable as the spool; the `run_id` index is additionally held by the retention floor while its SPAWN can still be replayed | no retained instance |
| **Wrapper-owned spool files** — `spawn.json` and `status.json`, and those two only (`docs/supervisor-protocol.md` §3) | their own `version` field | ignored | refused | as durable as the run directory | no retained spool holds one |
| **Wrapper input spec** — the one JSON object on the wrapper's stdin (`docs/supervisor-protocol.md` §2) | its own `version` field | **refused**, unlike the two spool files the same wrapper writes: §2 is frozen and the whole object is fingerprinted, so a key the schema does not pin is a key whose type is not pinned either, and the fingerprint is injective only over pinned types | refused **by the wrapper, after the fork**. The supervisor's gate pins the types; the version is the wrapper's own check. An absent or unimplemented `version` exits 2 without a spawn record; a spec that is not readable JSON exits 1, before the version is reached | the fork it is passed to; the wrapper repoints stdin at `/dev/null` after the read | none beyond the door |
| **Perimeter journal** — `perimeter.jsonl` (`docs/access-model.md` §6, DL-146/DL-147) | the record's `rec` kind; no per-record version field | ignored — evolution is additive; an incompatible change takes a NEW kind name, the WAL's move | not applicable by construction: no engine dispatches this journal (seq recovery reads only `access_seq`, the rest is audit), so an unknown kind is skipped, not refused | as durable as its run root; pruned only whole-root — an in-place truncation would restart `access_seq` and forge duplicate keys | no retained root holds one |
| **Access role map** — the `--access-map` file (`docs/access-model.md` §4) | `format_version` | refused — the map is a closed table, and a key the loader does not pin is a key whose meaning it cannot check | refused, naming the integer this loader implements | the operator's own file. It lives outside the estate: it is policy, not evidence, and a reload replaces it whole | no operator's map still names it |
| **Control socket** — `docs/control-protocol.md` §2 | `"v"` on every request, queries and `subscribe` included | ignored | refused, and the refusal does not close the connection | the request that carries it. The version is per request, not per connection, so one connection may carry several and a refusal ends none of them. `subscribe` is the exception: its request opens a stream that owns the connection until hangup (§5), so its instance lasts as long as the connection does | none beyond the door: nothing durable holds the dialect |
| **Supervisor socket** — `docs/supervisor-protocol.md` §5 | `"v"` on every request, plus `incarnation` on every mutating verb except `ACQUIRE`, which grants a free lease without one (§5) | ignored | refused as `unsupported_version`, and the refusal does not close the connection | the request that carries it, as the control socket | as the control socket |
| **`state_machine_version`** — `docs/period-model.md` §2.1 | the field itself, on `segment`, on the seal, on `staged_manifest.json`, on `candidate.json`, on the committed period manifest and on the attestation | not a format question: one executable implements exactly one version and refuses every other | refused | the estate | not retired — **replaced**: a full drain and a new-estate genesis (the last note below; `docs/period-model.md` §2.1) |

*(Amended by DL-150.* Two rows added — the **wrapper input spec** and the
**access role map**, both versioned surfaces the matrix did not cover. The
archive receipt joined the closed row. Both socket rows' lifetime was corrected
from the connection to the request. The carriers and gates of
`catalog_hash_version` and `state_machine_version` were widened to the closed
artifacts that also hold them.*)*

### Notes on the rows

**The WAL row — the kind is the discriminator, and it is strict.** A record kind is
not an optional field. Version gating happens at the opening `segment`, so an
unknown `rec` inside a version-matched segment is corruption, not an extension
this reader is too old to see. The dispatch is three-way at one place: current,
retired, unknown. Silently skipping an unrecognised kind would let a reader
walk past evidence and report a complete replay.

**`catalog_hash_version` outlives the row it is discriminated on.** It rides on
the `segment`, on the seal, on the period manifest (`docs/period-model.md`
§1.1), and on the two staging artifacts that feed a manifest —
`staged_manifest.json` and `candidate.json`, both retained by period-model §12
while the candidate is uncommitted. So an instance can still sit in a closed
artifact after every segment naming it is gone, and its gate is the union of
all five. `state_machine_version` is wider again: the attestation carries it
too, and the last note below says why that one is not retired at all. **The row
a version is listed under is where a reader dispatches on it, not the whole set
of places it can be found.**

**The closed-artifact row is strict on both counts.** §3.2's canonical form
puts every typed field on the wire with an explicit `null` for an unset
optional. That rule is what makes a digest reproducible, and for these readers
an unknown field is corruption: nothing legitimate can produce one. The
tolerant estate files share the writer-side canonical form, but their readers
take the fields they need — a writer rule is not a reader refusal.

**The two spool rows are tolerant on fields and strict on versions.** These files
are written by the supervisor and the wrapper, which may be a different build
from the engine that reads them. A field added by a newer writer must not stop
an older reader. A `version` the reader does not implement must stop it: the
version exists to say the meaning changed.

*(Amended by DL-151, at the build of the refusal this row had always asked
for.)* Until then no reader looked at the `spawn.json` / `status.json`
`version` at all. Both now do, each in its own vocabulary: the engine reads
an unsupported version as an UNREAD record, which costs a `status.json` its
outcome and lands the run on `exit_status_unobservable` rather than letting
a record whose meaning changed decide a verdict; the supervisor reads it as
PRESENT AND UNREADABLE, never as absence, because in its §11a table absence
authorizes a spawn. `true` and `1.0` are not the integer 1 on either side.
An **absent** `version` is refused by neither, and that is this contract
declining to rule rather than ruling: the columns above cover an unknown
FIELD and an unsupported VERSION and not a MISSING one, so a rule invented
here would settle by guess the open question DL-150 recorded and left
open.

**The wrapper input spec is strict on fields, beside a spool it writes
tolerantly, and the fingerprint is why.** A spool file is read for the fields a
reader needs. The input spec is hashed **whole**: a replayed SPAWN is answered
from `receipt.json`'s `spec_fingerprint`, a sha256 over the canonical form of
the §2 object with `lifeline_fd` removed (`docs/supervisor-protocol.md` §3,
period-model §11a). That hash is injective only over pinned types. An unpinned
key would let two specs that differ compare equal, and the answer to a replay
would be a stranger's. Tolerance is safe on a record read field by field and
unsafe on one hashed as a whole.

The version on this row is paid for **after** the fork. The wrapper exits,
writes no spawn record, and the engine reads the absence — supervisor-protocol
§3's E7 case. The two ends can be different builds: the engine composes the
spec, and the supervisor forks the wrapper file beside its own module, across
engine restarts it was started before. So a spec-version change is a
coordinated deploy of both, never a rolling one.

**The access map is the one row outside the estate.** No engine writes it; it is
the operator's file. So its lifetime is whatever the operator keeps, and its
gate is met when no map still names the version. It is strict on both counts
for the reason a frozen table always is: a key the loader does not pin has no
meaning it can check, and a version it does not implement describes a policy it
cannot enforce.

**The two socket rows have no durable instance.** A wire dialect is retired the day
the door refuses it. `control-protocol.md` §2 states the pattern for a breaking
change: a new version number, and the door refuses the old one by name — a
v2 client is refused naming v3. There is nothing to wait for and nothing to
sweep. The version rides on every request, so the instance is a **request** and
not a connection: a refusal answers one line, and the next line on the same
connection is read normally. `subscribe` is the one request that outlives its
answer — it owns its connection until hangup (`control-protocol.md` §5) — so
that one instance ends with the connection.

**The last row is semantics, not format.** `state_machine_version` names how the
interpreter derives, not how a byte is laid out. §2.1 freezes it across a
transition: one binary implements one version and can neither lead nor replay
another. So it does not evolve in place. A semantics change is a full drain and
a new-estate genesis, and the old estate is audited by the binary that produced
it (`docs/period-model.md` §11).

## 2. Entering service

A new dialect enters in four steps, in this order.

1. **Introduce.** The version is defined and its readers gain **dual-read**:
   they accept both the current dialect and the new one. Nothing writes the
   new one yet.
2. **Dual-read overlap, with positive compatibility tests.** Both versions
   read, and each round-trip is pinned. The tests are positive — they assert
   that the old dialect is still read correctly, not only that the new one is.
   A negative-only suite passes an implementation that has already dropped the
   old reader.
3. **Writer switch.** The writer emits the new dialect. **New instances only**:
   nothing rewrites an instance that already exists.
4. **Retire.** The old dialect leaves under §3's gate, which is usually much
   later, and sometimes never.

*(Amended by DL-150: the paragraph below used to state the durable-row rule as
if it held everywhere, which contradicts §3's socket paragraph.)*

Steps 1 to 3 may land in one release. **On a durable row, step 4 may not join
them**: at step 3 every old instance still exists, so the retirement gate is not
met by construction.

**On a wire row it may.** Nothing durable holds a wire dialect, so the gate is
met by construction (§3) and the only question left is whether any client still
sends the old version. That is a deployment question, not a retention one, and
it is answerable in one release when the clients ship with the server. It is
not answerable at all when they do not, which is why the four steps stay the
default here too.

The reset clause (§5) is the only way to bypass this lifecycle, and only under
its stated condition.

## 3. Retiring

**The gate is actual absence, not eligibility.**

> A dialect may be retired when **no instance of it exists** — not when every
> instance has become eligible for deletion.

A socket row meets the gate **by construction**. Nothing durable carries a wire
dialect: an instance is one request, gone once it is answered, or one
`subscribe` stream, gone at hangup. There is no instance to be absent, so the
only act retirement needs is the door refusing the version. For a durable row the gate means every instance is gone
from every root the operator keeps, whatever its retention verdict:

- an artifact **floored** by `docs/period-model.md` §12 may never be deleted,
  so its dialect can never be retired while the floor holds it;
- an artifact **held** by an operator's own policy is present, so its dialect
  is not retired;
- an artifact that is **prunable but present** is present. DL-135 made pruning
  optional and policy-driven — a run with no class named deletes nothing — so a
  prunable artifact can stay readable indefinitely. "Prunable" is a verdict,
  not a deletion.

*(Amended by DL-150.)* **"Exists" is scoped to what the operator keeps.** A copy that has left the
retained set — an archive tape, a colleague's laptop, a root restored from a
backup years later — is not an instance this gate can see, and waiting for it
would mean never retiring anything. That is exactly why §6 exists: the gate
covers the retained set, and the tombstone covers everything else.

Retirement is then implemented as **refusal by name**, never as deletion of the
reader alone. §6 says how.

## 4. Migration is not this contract's mechanism

This contract has two operations: dual-read and retire. Migration is neither.

If a transformation is ever needed, it is **its own decision-log entry**, with
its own lineage record and its own verification proof — the retired adoption
path is the shape such a thing takes: a fenced source, a translated target, a
proof that the translation is lossless against the retained original, and a
recovery matrix over every crash point.

**An in-place rewrite of an immutable digest-bound artifact is never
permitted.** The WAL, the seal sidecar and the attestation are bound by digests
that other artifacts carry. Rewriting one breaks every proof that names it,
including proofs held in roots the rewriter cannot reach.

## 5. The reset clause

> While **nothing runs in production**, a decision-log entry may declare a
> **pre-production reset**: a dialect is retired without §2's lifecycle, and
> every artifact written before the reset becomes unreadable.

*(Amended by DL-150: the clause used to read "without the §3 gate", which
contradicts DL-138's own classification — the gate is MET, trivially. What a
reset skips is the lifecycle, not the gate.)*

The clause is honest only because of its condition. With no estate anywhere,
the §3 gate is met trivially — there is no instance to be absent — and the
lifecycle of §2 has nothing to protect. §2 exists to keep an old reader working
while old instances are still met; with none, its four steps protect nobody and
collapse to the last one.

**"Nothing runs in production" is the licence; actual absence is still what
the gate wants.** The two are not the same sentence, and the entry has to
supply both. No production estate is what makes it credible that no instance
was ever written; it is not by itself proof of it. An entry that shows only
the first has shown that nobody is watching, which is a different claim from
"there is nothing to find".

**DL-138 is the first use of this clause, and once production exists it is the
last.** An entry claiming it must state the condition it is claiming, so that a
later reader can check the claim rather than infer it. DL-138 states both
halves (§8): no dsl41 estate existed in production, and therefore no instance
of any of the seven could exist anywhere.

A reset makes pre-reset roots unreadable. That is the cost, and it is stated in
the entry that takes it.

## 6. Tombstone registries

A retired dialect gets a **tombstone**, not a deleted reader. The difference is
what the operator sees: a tombstone names the dialect and the decision-log
entry that retired it; a deleted reader produces a generic parse error, or
worse, silence.

The rules:

- **Owner-local.** The registry lives with the reader that owns the question.
  There is no central table of retired things: a central table has to be
  consulted by readers that have no other reason to know about it, and it
  drifts from the readers that do.
- **Append-only.** A dialect is added when it is retired. A row is never
  removed and never re-used, because a root written before the retirement can
  still arrive on an operator's disk.
- **Refusal by name, with the decision-log citation.** The message names the
  dialect and the entry. "Unknown record kind" is not a tombstone.
- **A tombstone is not the unknown case.** An unrecognised value that is not in
  the registry refuses too, but as an unknown, with its own distinct error.
  Merging the two loses the difference between "this used to be legal" and
  "this was never legal", which is the difference between an old root and a
  corrupt one.

*(Amended by DL-150.)* A **registry** is the shape a tombstone takes when the
discriminator has many values: a table beside the reader, one row per retired value. Where the
discriminator has two — a boolean field whose one legal value is now `false` —
the refusal lives at that field's validator and no table is built. A table over
one row is a table nobody consults. The four rules above still bind it: the
message names the value and its entry, and the malformed case keeps its own
distinct error.

## 7. What every evolution event owes

One decision-log entry, plus **dispatcher tests on every affected row**:

1. the current dialect is accepted;
2. every retired dialect is refused **by name**, naming its retiring entry;
3. an unknown **field** follows the row's tolerance rule — ignored on a
   tolerant row, refused on a strict one;
4. an unsupported **version** is refused, on every affected row that has a
   version.

Cases 3 and 4 are tested separately. A single test that feeds a message which
is both unknown-field and unknown-version proves neither.

*(Amended by DL-150: case 4 used to read "on every row without exception",
which DL-147's perimeter row had already made untrue.)* One row has no version,
and it is the only exception to case 4: the perimeter
journal, by construction, because no engine dispatches it (DL-147). A row may
join that exception only the way this one did — by an entry showing that
nothing reads the artifact for a decision. "We did not add one" is not a
construction.

## 8. The first executed retirement

DL-138 retired seven dialects at once, under the reset clause of §5.
*(Amended by DL-150: this table said six. `legacy_batch: true` was missing, and
it is refused by name, naming DL-138, like the other six.)*

| dialect | row | replaced by |
| --- | --- | --- |
| the `header` journal record | WAL | `segment` (`docs/period-model.md` §2.1) |
| the `result` record | WAL | `decision` (`docs/period-model.md` §2.3) |
| the standalone `effect` record | WAL | the `effects` list nested in `decision` (`docs/period-model.md` §2.3) |
| `legacy_batch: true` on a `decision` | WAL | nothing — it marked a batch folded from a legacy estate's separate fsyncs, and the path that folded one went with the estate-adoption path. The field stays, required and `false` (`docs/period-model.md` §2.3) |
| `catalog_hash` version 1 | WAL, and the closed artifacts that carry the field — the seal, the period manifest, `staged_manifest.json` and `candidate.json` | version 2 (`docs/concurrency-model.md` §7) |
| the `manifest/` run-root layout | closed artifacts | `catalogs/<bundle>/` + `periods/<id>/` (`docs/period-model.md` §1.1) |
| the `adopting` lineage head state | closed artifacts | nothing — the estate-adoption path went with it |

Each is refused by name, and each names DL-138. Four sit in an owner-local
registry — the retired record kinds, the retired `catalog_hash` recipe, the
retired head state and the retired layout. `legacy_batch: true` is refused at
the same record validator as the kinds, without a registry of its own: it is
one value of one field of one record, and a registry over a single row is a
table nobody consults.

The entry states the condition the clause requires: no dsl41 estate existed in
production on 2026-08-21, so no instance of any of the seven could exist
anywhere.
