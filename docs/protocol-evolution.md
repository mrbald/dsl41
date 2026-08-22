# Protocol evolution — how a dialect enters service, and how it leaves

Status: **normative (2026-08-21, DL-138).** This document is the contract that
every versioned protocol and every durable artifact in the runner is held to.
It answers four questions: what each protocol tolerates, how long an instance
of it can still arrive, how a new dialect enters service, and what must be true
before an old one is retired.

It invents no rule for any protocol. Each tolerance rule lives in the document
that froze it — `docs/control-protocol.md` §2, `docs/supervisor-protocol.md`
§3 and §5, `docs/period-model.md` §2 and §3.2, `docs/access-model.md` §6
(DL-147). This document collects them per
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
- **Lifetime** — how long an instance of the dialect can still be met. A live
  connection ends when it closes. A durable artifact ends when the last copy
  is gone, and under `deployment-runbook.md` §2a that may be never.

Retirement needs both answers. A dialect may be retired only when no
instance of it remains in any retained root (§3), and the lifetime column is
what decides when that is true. A copy arriving later from outside the
retained set meets a tombstone, not silence (§6).

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
| **WAL journal records** — `docs/period-model.md` §2, `docs/runner-design.md` §7 | the record's `rec` kind, plus the `catalog_hash_version` and `state_machine_version` of the `segment` that opens the file | as each record's own schema declares; this contract does not change any of them. The **kind** is dispatched strictly: a current kind proceeds, a retired kind refuses by name, an unknown kind refuses by name | refused | the retention lifetime of the root that holds the segment | no retained segment holds one |
| **Closed estate artifacts** — the seal sidecar, the attestation, the period manifest, `staged_manifest.json`, `candidate.json`, `anchor.json`, the claim file, the sentinel (`docs/period-model.md` §3.2) | `artifact_format_version` | refused. §3.2 puts every typed field on the wire, so an unknown field is corruption and not an extension | refused, naming the version (PR-08d) | the retention lifetime of the estate; period-model §12's floor keeps several of them for the life of the lineage | no instance on disk |
| **Tolerant estate files** — `sources.json`, and every `watch.jsonl` line written by the FW adapter (`docs/period-model.md` §3.5) | `artifact_format_version` | ignored — the reader takes the fields it needs; §3.2's canonical form binds the writer, not the reader | refused, naming the version (PR-08d) | `sources.json` as durable as its catalog bundle; a watch line as durable as the run spool that holds it | no retained instance |
| **Tolerant supervisor artifacts** — `receipt.json`, `reply.json`, the `run_id` index entry (`docs/supervisor-protocol.md` §3) | `artifact_format_version` | ignored — the section's own forward-compatibility rule | refused | as durable as the spool; the `run_id` index is additionally held by the retention floor while its SPAWN can still be replayed | no instance on disk |
| **Wrapper-owned spool files** — `spawn.json` and `status.json`, and those two only (`docs/supervisor-protocol.md` §3) | their own `version` field | ignored | refused | as durable as the run directory | no retained spool holds one |
| **Perimeter journal** — `perimeter.jsonl` (`docs/access-model.md` §6, DL-146/DL-147) | the record's `rec` kind; no per-record version field | ignored — evolution is additive; an incompatible change takes a NEW kind name, the WAL's move | not applicable by construction: no engine dispatches this journal (seq recovery reads only `access_seq`, the rest is audit), so an unknown kind is skipped, not refused | as durable as its run root; pruned only whole-root — an in-place truncation would restart `access_seq` and forge duplicate keys | no retained root holds one |
| **Control socket** — `docs/control-protocol.md` §2 | `"v"` on every request, queries and `subscribe` included | ignored | refused, and the refusal does not close the connection | the live connection | none beyond the door: the version is refused at the handshake, and a closed connection leaves nothing behind |
| **Supervisor socket** — `docs/supervisor-protocol.md` §5 | `"v"` on every request, plus `incarnation` on every mutating verb | ignored | refused as `unsupported_version` | the live connection | as the control socket |
| **`state_machine_version`** — `docs/period-model.md` §2.1 | the field itself, on `segment` and on the seal | not a format question: one executable implements exactly one version and refuses every other | refused | the estate | not retired — **replaced**: a full drain and a new-estate genesis (the last note below; `docs/period-model.md` §2.1) |

### Notes on the rows

**The WAL row — the kind is the discriminator, and it is strict.** A record kind is
not an optional field. Version gating happens at the opening `segment`, so an
unknown `rec` inside a version-matched segment is corruption, not an extension
this reader is too old to see. The dispatch is three-way at one place: current,
retired, unknown. Silently skipping an unrecognised kind would let a reader
walk past evidence and report a complete replay.

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

**The two socket rows have no durable instance.** A wire dialect is retired the day
the door refuses it. `control-protocol.md` §2 states the pattern for a breaking
change: a new version number, and the door refuses the old one by name — a
v2 client is refused naming v3. There is nothing to wait for and nothing to
sweep.

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

Steps 1 to 3 may land in one release. Step 4 may not join them: at step 3 every
old instance still exists, so the retirement gate is not met by construction.

The reset clause (§5) is the only way to bypass this lifecycle, and only under
its stated condition.

## 3. Retiring

**The gate is actual absence, not eligibility.**

> A dialect may be retired when **no instance of it exists** — not when every
> instance has become eligible for deletion.

For a socket row this is met the moment the version is refused at the
handshake. For a durable row it means every instance is gone from every root
the operator keeps, whatever its retention verdict:

- an artifact **floored** by `docs/period-model.md` §12 may never be deleted,
  so its dialect can never be retired while the floor holds it;
- an artifact **held** by an operator's own policy is present, so its dialect
  is not retired;
- an artifact that is **prunable but present** is present. DL-135 made pruning
  optional and policy-driven — a run with no class named deletes nothing — so a
  prunable artifact can stay readable indefinitely. "Prunable" is a verdict,
  not a deletion.

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
> **pre-production reset**: a dialect is retired without the §3 gate, and every
> artifact written before the reset becomes unreadable.

The clause is honest only because of its condition. With no estate anywhere,
the §3 gate is met trivially — there is no instance to be absent — and the
lifecycle of §2 has nothing to protect.

**DL-138 is the first use of this clause, and once production exists it is the
last.** An entry claiming it must state the condition it is claiming, so that a
later reader can check the claim rather than infer it.

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

## 7. What every evolution event owes

One decision-log entry, plus **dispatcher tests on every affected row**:

1. the current dialect is accepted;
2. every retired dialect is refused **by name**, naming its retiring entry;
3. an unknown **field** follows the row's tolerance rule — ignored on a
   tolerant row, refused on a strict one;
4. an unsupported **version** is refused, on every row without exception.

Cases 3 and 4 are tested separately. A single test that feeds a message which
is both unknown-field and unknown-version proves neither.

## 8. The first executed retirement

DL-138 retired six dialects at once, under the reset clause of §5:

| dialect | row | replaced by |
| --- | --- | --- |
| the `header` journal record | WAL | `segment` (`docs/period-model.md` §2.1) |
| the `result` record | WAL | `decision` (`docs/period-model.md` §2.3) |
| the standalone `effect` record | WAL | the `effects` list nested in `decision` (`docs/period-model.md` §2.3) |
| `catalog_hash` version 1 | WAL | version 2 (`docs/concurrency-model.md` §7) |
| the `manifest/` run-root layout | closed artifacts | `catalogs/<bundle>/` + `periods/<id>/` (`docs/period-model.md` §1.1) |
| the `adopting` lineage head state | closed artifacts | nothing — the estate-adoption path went with it |

Each is refused by name in its owner's registry, and each names DL-138. The
entry states the condition the clause requires: no dsl41 estate existed in
production on 2026-08-21, so no instance of any of the six could exist
anywhere.
