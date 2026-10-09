# Protocol evolution — how a dialect enters service, and how it leaves

Status: **normative (DL-138; amended by DL-147, DL-150, DL-151, DL-157,
DL-158, DL-168, DL-170, DL-217, DL-227 and DL-263).** A change to what this
document fixes requires a decision-log entry. Each amendment is cited where
it applies.

This document is the contract for every versioned protocol and every durable
artifact in the runner. It answers four questions:

- what each protocol tolerates;
- how long an instance of it can still arrive;
- how a new dialect enters service;
- what must be true before an old dialect is retired.

It invents no tolerance rule. Each tolerance rule lives in the document
that defines it: `docs/control-protocol.md` §2, `docs/supervisor-protocol.md`
§2, §3 and
§5, `docs/period-model.md` §1.1, §2, §3.2, §3.5 and §12a, and
`docs/access-model.md` §4 and §6 (DL-147). This document collects them per
protocol. A change to one rule is then argued against the whole set.

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
  **retained** copy is gone (§3 scopes that word). Under
  `deployment-runbook.md` §2a that may be never.

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

| protocol / artifact set | discriminator | unknown FIELDS | unsupported VERSIONS | absent VERSION (DL-157) | lifetime | retirement precondition |
| --- | --- | --- | --- | --- | --- | --- |
| **WAL journal records** — `docs/period-model.md` §2, `docs/runner-design.md` §7 | the record's `rec` kind, plus the `catalog_hash_version` and `state_machine_version` of the `segment` that opens the file. Those two also ride on closed artifacts, where their gates are wider than this row's (see the two notes below) | as each record's own schema declares; this contract changes none of them. The **kind** is dispatched strictly: a current kind proceeds, a retired kind refuses by name, an unknown kind refuses by name | refused | not applicable: the row's own discriminator is a kind, not a version | the retention lifetime of the root that holds the segment | no retained segment holds one |
| **Closed estate artifacts** — the seal sidecar, the attestation, the period manifest, `staged_manifest.json`, `candidate.json`, `anchor.json`, the claim file, the sentinel (`docs/period-model.md` §3.2), and the archive receipt `seals/<period>.archive.json` (§12a, DL-144, DL-150) | `artifact_format_version` | refused. §3.2 puts every typed field on the wire, so an unknown field is corruption, not an extension | refused, naming the version (PR-08d) | refused, naming it absent, on every member | the retention lifetime of the estate. period-model §12's floor keeps several of them for the life of the lineage. §12a's three (the archive receipt, and the attestation and seal sidecar of an archived period) may never be pruned by any class | no retained instance |
| **Tolerant estate files** — `sources.json` (`docs/period-model.md` §1.1), and every `watch.jsonl` line written by the FW adapter (§3.5) | `artifact_format_version` | ignored: the reader takes the fields it needs. §3.2's canonical form binds the writer, not the reader | refused, naming the version (PR-08d) | refused, the same as an unsupported one | `sources.json`: as durable as its catalog bundle. A watch line: as durable as the run spool that holds it | no retained instance |
| **Tolerant supervisor artifacts** — `receipt.json`, `reply.json`, the `run_id` index entry (`docs/supervisor-protocol.md` §3) | `artifact_format_version` | ignored, by that section's forward-compatibility rule | refused | refused: the evidence schema requires the field | as durable as the spool. The retention floor also holds the `run_id` index while its SPAWN can still be replayed | no retained instance |
| **Wrapper-owned spool files** — `spawn.json` and `status.json`, and only those two (`docs/supervisor-protocol.md` §3) | their own `version` field | ignored | refused | passed (`runner_procid.spool_version_supported`) | as durable as the run directory | no retained spool holds one |
| **Wrapper input spec** — the one JSON object on the wrapper's stdin (`docs/supervisor-protocol.md` §2, DL-150) | its own `version` field | **refused**, unlike the two spool files the same wrapper writes. §2 is frozen and the whole object is fingerprinted. A key the schema does not pin has no pinned type, and the fingerprint is injective only over pinned types | refused **by the wrapper, after the fork**. The supervisor's gate pins the types; the value is the wrapper's own check. An unimplemented `version` exits 2 with no spawn record. A spec that is not readable JSON exits 1, before the version is read | refused. The supervisor's gate refuses a spec with no `version` key before the fork, as a missing key (supervisor-protocol §2, DL-150). A spec that reaches the wrapper without one exits 2, the same as unimplemented (supervisor-protocol §4 step 6) | the fork it is passed to. The wrapper points stdin at `/dev/null` after the read | none beyond the door |
| **Perimeter journal** — `perimeter.jsonl` (`docs/access-model.md` §6, DL-146, DL-147) | the record's `rec` kind; no per-record version field | ignored. Evolution is additive: an incompatible change takes a NEW kind name, as in the WAL | not applicable by construction. No engine dispatches this journal: seq recovery reads only `access_seq`, and the rest is audit. An unknown kind is skipped, not refused | not applicable: there is no version field | as durable as its run root. Pruned only with the whole root: truncating it in place would restart `access_seq` and forge duplicate keys | no retained root holds one |
| **Access role map** — the `--access-map` file (`docs/access-model.md` §4, DL-150) | `format_version` | refused. The map is a closed table, and the loader cannot check the meaning of a key it does not pin | refused, naming the integer this loader implements | refused, the same as unsupported | the operator's own file. It lives outside the estate: it is policy, not evidence, and a reload replaces it whole | no operator's map still names it |
| **Control socket** — `docs/control-protocol.md` §2 | `"v"` on every request, queries and `subscribe` included | ignored | refused. The refusal does not close the connection | refused, the same as unsupported | the request that carries it (DL-150). The version is per request, not per connection: one connection may carry several requests, and a refusal ends none of them. `subscribe` is the exception. Its request opens a stream that owns the connection until hangup (§5), so its instance lasts as long as the connection | none beyond the door: nothing durable holds the dialect |
| **Supervisor socket** — `docs/supervisor-protocol.md` §5 | `"v"` on every request, plus `incarnation` on every mutating verb except `ACQUIRE`, which grants a free lease without one (§5) | ignored | refused as `unsupported_version`. The refusal does not close the connection | refused, the same as unsupported | the request that carries it, as on the control socket | as on the control socket |
| **`state_machine_version`** — `docs/period-model.md` §2.1 | the field itself, on `segment`, on the seal, on `staged_manifest.json`, on `candidate.json`, on the committed period manifest and on the attestation | not a format question: one executable implements exactly one version and refuses every other | refused | refused by construction: no carrier defaults it | the estate | not retired but **replaced**: a full drain and a new-estate genesis (the last note below; `docs/period-model.md` §2.1) |

An **absent version** (DL-157) is refused on every row that carries a
version, except one (DL-227). A row that is strict on an unknown field is
strict on a missing version too. The closed row shows why: §3.2 puts every
typed field on the wire, so an unknown field is corruption, and an absent
field is corruption for the same reason, because no retained instance omits
one. The one row that passes an absent version is the wrapper-owned spool
files. Its reader is `runner_procid.spool_version_supported`. It passes an
absent `version` because the Tier-0 wrapper writes the file before any
reader exists to require one (DL-227). Tolerance of unknown fields decides
nothing about the version column: the supervisor's evidence files,
`sources.json` and `watch.jsonl` all ignore an unknown field and still
require the version. The closed row refuses an absent version on every
member, `staged_manifest.json` included.

### Notes on the rows

**The WAL row: the kind is the discriminator, and it is strict.** A record
kind is not an optional field. The version is gated at the opening
`segment`. An unknown `rec` inside a version-matched segment is therefore
corruption, not an extension this reader is too old to see. The dispatch has
three outcomes in one place: current, retired, unknown. Skipping an unknown
kind would let a reader walk past evidence and report a complete replay.

The event alphabet inside an `input` record's `kind` field is a second
strict discriminator under the same rule (DL-158). An event kind outside the
reading build's alphabet refuses by name at replay. The alphabet grows only
by §2's order: reader and writer enter service in one release.

**`catalog_hash_version` outlives the row it is dispatched on.** It rides on
the `segment`, on the seal, on the period manifest (`docs/period-model.md`
§1.1), and on the two staging artifacts that feed a manifest:
`staged_manifest.json` and `candidate.json`. period-model §12 retains both
while the candidate is uncommitted. An instance can therefore sit in a closed
artifact after every segment that names it is gone, and its retirement gate
is the union of all five (DL-150). `state_machine_version` reaches further: the
attestation carries it too, and the last note below says why it is never
retired. **The row a version is listed under is where a reader dispatches on
it, not every place it can be found.**

**The closed-artifact row is strict on both counts.** §3.2's canonical form
puts every typed field on the wire, with an explicit `null` for an unset
optional. That rule makes a digest reproducible. For these readers an
unknown field is corruption: nothing legitimate can produce one. The tolerant
estate files share the writer-side canonical form, but their readers take
only the fields they need. A writer rule is not a reader refusal.

The same rule covers an absent field (DL-157). §3.2's canonical form has no
optional `artifact_format_version`. A retained instance without the key is
not a narrower dialect that this binary declines to read; it is not this
artifact at all. Each reader requires the key before it validates the rest
of the document:

- `period.py`'s `read_sentinel`;
- `boundary.py`'s `EstateAnchor.read`, `EstateAnchor.read_claim`,
  `read_candidate` and `read_staged_manifest`;
- `seal.py`'s `Seal.from_payload` (DL-227);
- `period.py`'s `parse_sealed_preamble`, which the attestation and the
  archive receipt share;
- `period.py`'s `read_period_manifest`, which requires every field of the
  manifest, and `_read_artifact`, which applies the same check to
  `staged_manifest.json` (`require_manifest_fields`, DL-252).

A third gate stands beside unknown and absent (DL-168): a field of the WRONG
TYPE is refused, never coerced. **A closed artifact or staged identity is
validated strict in the JSON sense from its bytes. A wire ingress of one is
validated strict from its payload.** `true` never becomes `1`, and a numeric
string never becomes the integer it spells.

- `boundary._read_artifact`, behind `read_candidate` and
  `read_staged_manifest`, reads `model_validate_json(raw, strict=True)`. This
  is the mechanism `period.read_period_manifest` uses for the committed
  manifest.
- `StagedManifest` and `Candidate` also set `strict=True` in their model
  config. This second check is for a future caller that builds either model
  with a lax `model_validate`.
- That model-level check covers top-level fields only. Pydantic validates a
  nested-model field under that model's own config, whatever the outer
  model sets. Only a call-time override, as in `_read_artifact`, reaches
  nested fields. So `StagedManifest.runtime_profile`, a nested
  `RuntimeProfile`, is not covered by `StagedManifest`'s config.
  `Candidate.next_period` is covered, because `StagedNextPeriod` sets
  `strict=True` itself.
- `StagedNextPeriod` is also the wire's copy of a staged identity, validated
  at `runner_control.py`'s `_seal`. Every field in it is a scalar, so its
  config alone closes that wire ingress. No call-time override is needed.
- `EstateAnchor.read` and `EstateAnchor.read_claim` read
  `model_validate_json(raw, strict=True)` after the decode and version gates,
  and the anchor after its head-state check too (DL-263). Every field
  refuses a coerced value, nested fields included: `head.period_id`, a period
  row's `segment_durable`, and `next_period` in a claim or a reclaimed entry.

`SealRequest`, the wire's copy of the seal ENVELOPE that carries
`next_period`, is validated at the same `_seal` and also sets `strict=True`
(DL-170). That covers six more fields: `baseline_id`, `epoch`, `request_id`,
`stage_digest`, `force_seal` and `claimed_actor`.

- `runner_control._seal_wire_error` runs first on the live socket. It owns
  the pinned refusal PROSE, which this row's strictness rule does not
  promise: a `ValidationError` uses pydantic's words, not the operator's.
- The model's `strict=True` also covers `cli_estate.py`'s offline retry
  route. That route builds a `SealRequest` directly and passes no wire gate.
- One field is not fully covered by either check alone. `_seal` builds
  `claimed_actor=request.get("claimed_actor") or ""`, which turns a falsy
  non-string into a legal empty string before the model sees it. For that
  field `_seal_wire_error` is the only check.

**The two spool rows are tolerant on fields and strict on versions.** The
supervisor and the wrapper write these files, and they may be a different
build from the engine that reads them. A field added by a newer writer must
not stop an older reader. A `version` the reader does not implement must
stop it, because the version exists to say the meaning changed.

Both readers check the `spawn.json` and `status.json` `version` (DL-151),
each in its own terms:

- The engine treats an unsupported version as an UNREAD record. A
  `status.json` read that way loses its outcome, and the run lands on
  `exit_status_unobservable`. A record whose meaning changed does not decide
  a verdict.
- The supervisor treats it as PRESENT AND UNREADABLE, never as absent,
  because in period-model §11a's table absence authorizes a spawn.

`true` and `1.0` are not the integer 1 on either side.

Neither reader refuses an **absent** `version`. These two files are the one
row in the matrix that passes it (DL-157, DL-227). Tolerance of an unknown
field is not the reason: the supervisor's own evidence files ignore an
unknown field and still require the version. The reason is that the Tier-0
wrapper writes the file before any reader exists to require one.

**The wrapper input spec is strict on fields, while the spool files the same
wrapper writes are tolerant. The fingerprint is the reason.** A spool file is
read for the fields a reader needs. The input spec is hashed **whole**. A
replayed SPAWN is answered from `receipt.json`'s `spec_fingerprint`, a
sha256 over the canonical form of the §2 object with `lifeline_fd` removed
(`docs/supervisor-protocol.md` §3, period-model §11a). That hash is
injective only over pinned types. An unpinned key would let two different
specs compare equal, and a replay would be answered from another spec's
receipt. Tolerance is safe on a record read field by field. It is unsafe on
a record hashed as a whole.

The wrapper checks the version value **after** the fork. On refusal it exits
and writes no spawn record, and the engine reads the absence:
supervisor-protocol §3's E7 case. The two ends can be different builds. The
engine composes the spec. The supervisor forks the wrapper file that sits
beside its own module, and the supervisor may have started before the
current engine. A spec-version change is therefore a coordinated deploy of
both, never a rolling one.

**The access map is the one row outside the estate.** No engine writes it;
it is the operator's file. Its lifetime is whatever the operator keeps, and
its retirement gate is met when no map still names the version. It is strict
on both counts, as any frozen table is: the loader cannot check the meaning
of a key it does not pin, and a version it does not implement describes a
policy it cannot enforce.

**The two socket rows have no durable instance.** A wire dialect is retired
on the day the door refuses it. `control-protocol.md` §2 states the pattern
for a breaking change: a new version number, and the door refuses the old
one by name. A v2 client is refused with a message naming v3. Nothing waits
and nothing is swept. The version rides on every request, so an instance is
a **request**, not a connection. A refusal answers one line, and the next
line on the same connection is read normally. `subscribe` is the one request
that outlives its answer: it owns its connection until hangup
(`control-protocol.md` §5). That instance ends with the connection.

**An additive answer field is not a new dialect** (DL-217). The basis is
`control-protocol.md` §2: consumers must ignore unknown fields. Every current
client therefore reads an answer with an added field the same as before. The
change takes no version bump and none of §2's four steps. An example is
`original_decision` on a `sendevent` or `host` collision refusal
(`control-protocol.md` §3). It nests the earlier decision under its own key,
so a client that ignores it still reads the refusal as a refusal. Two tests
in `tests/test_recovery.py` hold this:
`test_a_collision_carries_the_ids_earlier_decision_applied_or_rejected`
checks that the answer still classifies as `refused`, and
`test_a_collision_with_an_undecided_original_carries_nothing_nested` checks
the answer without the field. A field that changes how an answer is
classified does not qualify; it is a new dialect.

**The last row is semantics, not format.** `state_machine_version` names how
the interpreter derives state, not how bytes are laid out. §2.1 freezes it
across a transition: one binary implements one version and can neither lead
nor replay another. It does not evolve in place. A semantics change is a
full drain and a new-estate genesis. The binary that produced the old estate
audits it (`docs/period-model.md` §11).

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

For a durable row, the gate means every instance is gone from every root the
operator keeps, whatever its retention verdict:

- an artifact **floored** by `docs/period-model.md` §12 may never be
  deleted, so its dialect cannot be retired while the floor holds it;
- an artifact **held** by an operator's own policy is present, so its
  dialect is not retired;
- an artifact that is **prunable but present** is present. Pruning is
  optional and policy-driven (DL-135): a run that names no class deletes
  nothing, so a prunable artifact can stay readable indefinitely.
  "Prunable" is a verdict, not a deletion.

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

A transformation, if one is ever needed, is **its own decision-log entry**,
with its own lineage record and its own verification proof. The retired
estate-adoption path shows the shape: a fenced source, a translated target,
a proof that the translation is lossless against the retained original, and
a recovery matrix over every crash point.

**An in-place rewrite of an immutable digest-bound artifact is never
permitted.** The WAL, the seal sidecar and the attestation are bound by
digests that other artifacts carry. Rewriting one breaks every proof that
names it, including proofs held in roots the rewriter cannot reach.

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

**DL-138 is the first use of this clause. Once production exists, it is the
last.** An entry that uses the clause must state the condition it claims, so
a later reader can check the claim rather than infer it. DL-138 states both
halves (§8): no dsl41 estate existed in production, so no instance of any of
the seven retired dialects could exist anywhere.

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
value is now `false`, the refusal lives in that field's validator and no
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

One row has no version and is the only exception to case 4 (DL-150): the
perimeter journal, by construction, because no engine dispatches it
(DL-147). A row may join that exception only the same way: by an entry
showing that nothing reads the artifact for a decision. "We did not add one"
is not a construction.

## 8. The first executed retirement

DL-138 retired seven dialects at once, under the reset clause of §5
(DL-150).

| dialect | row | replaced by |
| --- | --- | --- |
| the `header` journal record | WAL | `segment` (`docs/period-model.md` §2.1) |
| the `result` record | WAL | `decision` (`docs/period-model.md` §2.3) |
| the standalone `effect` record | WAL | the `effects` list nested in `decision` (`docs/period-model.md` §2.3) |
| `legacy_batch: true` on a `decision` | WAL | nothing. It marked a batch folded from a legacy estate's separate fsyncs, and the folding path was removed with the estate-adoption path. The field stays, required and `false` (`docs/period-model.md` §2.3) |
| `catalog_hash` version 1 | WAL, and the closed artifacts that carry the field: the seal, the period manifest, `staged_manifest.json` and `candidate.json` | version 2 (`docs/concurrency-model.md` §7) |
| the `manifest/` run-root layout | closed artifacts | `catalogs/<bundle>/` + `periods/<id>/` (`docs/period-model.md` §1.1) |
| the `adopting` lineage head state | closed artifacts | nothing. The estate-adoption path was removed with it |

Each is refused by name, and each refusal names DL-138. Four sit in an
owner-local registry: the retired record kinds, the retired `catalog_hash`
recipe, the retired head state and the retired layout. `legacy_batch: true`
is refused in the same record validator as the kinds, with no registry of
its own: it is one value of one field of one record, and a registry with a
single row is a table nobody consults.

DL-138 states the condition the clause requires: no dsl41 estate existed in
production on 2026-08-21, so no instance of any of the seven could exist
anywhere.
