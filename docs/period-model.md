# Period model — the seal, the segment, and the optional run root

Status: **frozen (DL-114).** This document is normative in the same way as
`docs/concurrency-model.md` and `docs/control-protocol.md`: each change to a
frozen item needs a decision-log entry. It is the only home of the boundary
mechanism. The operator's view of it is `docs/deployment-runbook.md` §6a and
§8 (DL-230).

§13's obligations and §14's worked estate carry correctness. Staged exposure
does not. An obligation weak enough to let a broken implementation pass is a
defect of the same rank as a wrong mechanism.

## 0. The problem

Without periods, a run root carries four lifetimes (§1 names them) and makes
them end together. An estate change is then stop, swap, and a **new run
root**. A new run root is a new log, a new baseline and a fresh oracle. So
every release silently resets every runtime global, every operator hold,
every `last_end_at` that lookback reads, every `armed` latch, every box's
`ran_members` and every `run_number`. That reset is correct at a cycle
boundary and wrong in the middle of one. Nothing in that model says which of
the two an operator is doing. A directory would be doing a job that belongs
to a record.

## 0a. Revision history

The git history holds the drafts before DL-114. Each change after the
freeze is a decision-log entry, cited where it applies.

## 1. Identities

| concept | unit | lifetime | identity |
| --- | --- | --- | --- |
| **estate** | one lineage of periods | until retired | `estate_id` (uuid4, minted once at genesis) |
| **estate root** | one directory | operational | a path — and **only** a path; it identifies nothing |
| **period** | one catalog + one runtime profile + one state-machine version | a release cycle | `period_id`, `baseline_id` |
| **segment** | one WAL file | a retention/corruption unit | `segment_no` |
| **seal** | one period boundary | forever (archive) | `period_id`, `digest` |
| **execution** | one job run | the run | `run_id`, `(job, run_number)` |

Two invariants tie them together:

> **I1.** A period is exactly one segment, and a segment is exactly one period.
> Segment N is period N.
>
> **I2.** Indices, epochs and run numbers are monotone across the **estate**,
> not across the segment or the period.

I1 keeps replay simple. One file has one catalog and one state-machine
version, so a reader never switches semantics mid-file. I1 also removes a
recovery sub-protocol: a segment that rolled for size would need a durable
active-segment pointer to choose between two candidates. There is no size
roll. **To roll a segment, seal.** A transition with an unchanged catalog is
a legal period (§2.1), and it is how an operator bounds the file of a
long-lived period. I2 makes an effect id, a spool path and a revision mean
one thing for the life of the estate.

### 1.1 Layout

```
<estate-root>/
  control.sock  supervisor.sock  leader.lock
  perimeter.jsonl     access receipts (DL-146) — never an engine input,
                      never replayed; contract in docs/access-model.md §6
  wal/
    000001.jsonl        period 1, closed
    000002.jsonl        period 2, closed
    000003.jsonl        period 3, ACTIVE
  seals/
    000001.json         the seal that closed period 1
    000001.audit.json   its attestation (§11)
    000001.archive.json its archive receipt, if its inputs were archived (§12a)
  catalogs/
    <source_bundle_hash>/   content-addressed BY BYTES, immutable
      *.jil                 the post-placeholder JIL, byte-exact (F1)
      sources.json          input sha256s and original paths
  periods/
    000002/manifest.json   catalog_hash + source_bundle_hash + runtime profile
    .staging/<stage_digest>/    a staged candidate, before its seal commits — §7
    .quarantine/<stage_digest>/<manifest digest>/   a superseded one — §7
  runs/<job>.<run_number>/    spool; run_number monotone for the estate's life
  runs/.by_run_id/<run_id>    the SPAWN idempotency index — §11a
  logs/

<anchor-dir>/            NOT inside any archivable root — §1.3
  anchor.json            estate_id + lineage head
  anchor.lock            the lineage lock
  claims/<claim_id>.json the durable successor claim
```

**Every periodized root carries a permanent `journal.jsonl` sentinel.** It
is one line: `{"rec": "period_root", "artifact_format_version": 1,
"estate_id": …, "see": "wal/", "claim_id": null}`. Genesis writes it, and so
does the opener of a physical roll. A roll's sentinel differs only in
`claim_id`, which names the claim that **first opened this root**. So the
sentinel proves "this very claim"; it does not infer it from `estate_id`
alone. The claim-equality rule applies when a physical roll creates a root
that nobody owned. An in-place opener takes a new claim every period, and its
root's sentinel keeps the claim that created the root. For an in-place claim
the sentinel proves only that this estate owns this root, which is all it
needs to prove. There is one record kind and one schema.

Every root has a sentinel from its first instant. Without it, a native root
that sealed and exited with code 3 would release `leader.lock` over a
directory with no `journal.jsonl`. A build that found none would read the
root as *unused*, start a new estate beside the lineage, ignore the anchor,
and admit work while detached C1 executions are still alive. The sentinel
is never absent, so a root that sealed never reads as unused. This is also
why the file keeps the name `journal.jsonl`.

A `period_root` line that cannot be read is refused by name (DL-151). A
first line of another kind is not a sentinel.

**One ownership rule governs every root and every anchor.** It generalizes
the runner's rule that refuses any root with an existing `journal.jsonl`:

> **absent → create. Exact same estate and exact same incomplete transaction →
> resume. Anything else → refuse.**

For a **root**: creating the sentinel refuses a target that already holds a
`journal.jsonl` of any kind. That includes another estate's, an earlier
period of this estate's, and a concurrent opener's. The one exception is
this estate's sentinel for this very claim, left by our own crash. "Fresh
root" alone is not a rule that can be checked. Estate E1 could take the free
`leader.lock` of a dormant estate E2's root R, overwrite R's sentinel and
install its imports while E2's anchor still named R. Two estates racing for
R could leave one anchor `claimed(R)` while the other replaced R's sentinel
(PR-01c).

**Absence of the sentinel is not by itself absence of an estate.** A root
that lost only its `journal.jsonl` but still holds `wal/`, `seals/`, a
committed `periods/<N>/` or a non-empty `runs/` is somebody's work. A
genesis there would relabel foreign history, or run beside detached
processes that still write into it. So genesis refuses there too, and names
what it found. Two directories are excluded, because the launcher writes
them before genesis: `catalogs/` is content-addressed bundle storage and
`periods/.staging/` holds candidates.

For an **anchor**: creating it refuses an existing `anchor.json`, *even if
its incumbent is dead*. An existing anchor is an existing estate whose
detached work may still be alive. "Two geneses are two estates" never let two
estates share one anchor (PR-01b). The sole recovery exception is
`open(1, this root, this estate_id)` with a matching sentinel and no
committed segment: that is our own genesis, interrupted. Once a segment
exists, ordinary `--resume` owns recovery.

**Native genesis is an ordered transaction** under that rule:

1. `flock` `leader.lock`;
2. write the sentinel by the liturgy (create-only);
3. take `anchor.lock` and do the create-only CAS `absent → open(1, root)`;
4. materialize the bundle and `periods/000001/manifest.json`;
5. write `wal/000001.jsonl` with its `segment` record;
6. finalize: an anchor CAS that sets `periods[1].segment_durable = true`.

A crash after the sentinel and before the segment leaves a root that no old
binary can use. A re-run of genesis completes it idempotently. It reads
`estate_id` back from the sentinel and does not mint a second one (PR-01a).
A physical roll's opener writes the sentinel as its first durable act in the
new root, before any import.

`catalogs/` is addressed by **`source_bundle_hash`**. Its normative
definition: take the input files **in command-line order**. For each, frame
`len(path) ‖ path ‖ len(bytes) ‖ bytes`. Here `path` is the original path as
given, in UTF-8. `bytes` is the post-placeholder UTF-8 text. Both lengths
are 8-byte big-endian counts of UTF-8 bytes. Take sha256 of the
concatenation, spelled `sha256:…`. Length framing stops `["ab","c"]` from
colliding with `["a","bc"]`.

**Order is included, not sorted away.** The loader keeps command-line order,
and `CatalogMeta.source_files` records it. The pinned `catalog_hash` covers
the raw model, so reversing two files changes the runner hash. A bundle
address that ignored order would map one directory to two catalog hashes,
and one `sources.json` could not rebuild both. `sources.json` stores the
ordered vector, and reopening uses it verbatim.

`catalog_hash` is a **different** thing, and the code has **two** of them.
One is the runner hash, `period.catalog_hash_v2`, which the journal reaches
through `catalog_hash_at`. The other is `equiv.catalog_hash`, which strips
spans and annotations first. The runner hash does not collide across sources
whose bytes differ. The *equivalence* hash does, by design.

This spec pins the runner hash **with one exclusion, and versions it**.
`catalog_hash` v2 is sha256 over the §3.2 canonical form of `CatalogIR`,
with `meta` projected to `{source_files}` only. `tool_version` **and**
`parsed_at` are diagnostic and leave the hash. Spans stay in. The version
rides explicitly as `catalog_hash_version: 2` on `segment`, seal and period
manifest. A hash-v2 golden vector ships with PR-08. Version 1 is retired and
is refused by name (DL-138, `docs/protocol-evolution.md`). No record carries
a v1 value beside a v2 one. `catalog_hash_at` checks the version before it
hashes: an unknown version is refused too.

The reason for the exclusion: `CatalogMeta.tool_version` is the installed
package version. A hash over it changes when only the version string moves
from 1.2.3 to 1.2.4. A seal committed by 1.2.3 could then never be opened by
1.2.4, and PR-07's byte-identical openings across a patch release would be
impossible. It would be the same *"outage manufactured by bookkeeping"* that
DL-100 names for the state-machine version. A change to this hash is a
change to a frozen identity (leader eligibility) and needs its own
decision-log entry.

The period manifest binds `catalog_hash` and `source_bundle_hash` together.
A period that reverts to earlier bytes references the directory already
there. Launch options live under `periods/`, never under `catalogs/`.

**Rolling the estate root is optional archival hygiene.** The seal and the
opening have the same format whether the next period continues in place or
opens a fresh root (PR-07). So rolling is a deployment choice, never a second
semantic path.

### 1.2 Estate identity

`estate_id` is carried in every `segment` record, every seal and the anchor.
A root whose `estate_id` does not match the seal it is opening refuses. Two
geneses are two estates.

### 1.3 The successor fence

The lineage forks unless exactly one root can succeed a seal:

1. root A seals period 2;
2. root B opens period 3 from that seal;
3. root A still holds a `seal` record with no following `segment`;
4. A's own recovery opens period 3 there too;
5. A and B have different `leader.lock` files, so neither excludes the other;
6. both allocate the same next index and run numbers;
7. the same `(job, run_number)` executes twice — `concurrency-model.md` §0's
   safety property, violated.

**The contract** does not depend on the substrate. It has three head states:

```
open(period_id, root)                          a period is live in `root`
closed(seal_digest, closing_root, period_id)   it ended; nobody has opened the next
claimed(claim_id, target_root)                 one root is opening the next

close_period(estate_id, period_id, root, seal_digest)          open → closed
claim_id = sha256(canonical{prev_seal_digest, next_period, target_root})   # target_root: absolute, normalized
claim_successor(estate_id, seal_digest, next_period, target_root) → claimed
first segment record durable                                    claimed → open
genesis (§1.1)                                                  absent → open(1, root)
```

Two more moves exist, both from `claimed`. The break-glass reclaim below
returns the head to `closed`. A claim whose file is gone, while the head
already names it, writes the claim file again and keeps the head `claimed`.
The head and each period's registry row are state machines. A move that the
machine does not declare refuses the operation before any write (DL-290).

`open → closed` is a transition of its own. Without it, a committed seal
would leave the head `open(2, A)`, and no opener could ever claim it. The
closing transition is the **third write** of the seal sequence (§3): sidecar
durable → `seal` record durable → anchor CAS `open(N, root) → closed(digest,
root, N)`. This opens one window: the seal record landed and the head is
still `open`. Resume repairs it by doing that CAS (§11 matrix, PR-45).

`closed` carries **`closing_root`** because a physical roll's opener needs to
find the sidecar, the C2 bundle and the period manifest.

**A physical roll requires the closing period to be attested first.**
`run --open-from` refuses unless `seals/<N>.audit.json` exists in
`closing_root`. Otherwise B would import a seal it could never verify, and
would audit C1 with none of C1's inputs. The opener imports `seals/<N>.json`,
`seals/<N>.audit.json`, `catalogs/<bundle>/` and `periods/<N+1>/` into the
new root with the liturgy. It writes its first `segment` record. **Then**,
in the `claimed → open` write, it registers `periods[N+1].root = B`. Nothing
registers a successor before its segment is durable.

B is then resumable on its own, and it **`verify`**s C1. `verify` is a
different verb from `audit`. `audit` is full re-derivation and needs C1's
WAL, spool and manifests (§11). `verify` validates an attestation: its own
digest, its binding to the seal digest, and its place in the chain. B has
what `verify` needs and not what `audit` needs. Calling one by the other's
name would quietly weaken §11's definition (PR-02a).

**An attestation is a chain checkpoint, by induction and not by assertion.**
Auditing period 1 re-derives from genesis. Auditing period N first
**verifies attestation N−1**. Attestation N then records
`chain_through_period: N` and `prev_attestation_digest`.

**Producing and consuming an attestation are two acts with two rules.**

- *Producing* N (`audit`) requires attestation N−1 **present and verified**.
  Period 1 is the base case, with `prev_attestation_digest: null`. There is
  deliberately no "or re-derive everything below" alternative: it would
  leave `prev_attestation_digest` undefined when no predecessor artifact
  exists. Without this rule, a wrong implementation checks only its own
  digest and seal binding, emits a "checkpoint" over an unaudited opening
  seal, and earlier roots get deleted on a chain that was never established.
- *Consuming* N as a checkpoint (`verify`) accepts N **alone**: its own
  digest, its seal binding, and its `chain_through_period`. The producing
  audit already established the induction, and a physical roll imports only
  the current seal and attestation. One rule for both would make a second
  roll impossible.

So root C, importing seal 2 and attestation 2 while A and B are gone,
verifies the chain below seal 2 *because attestation 2 proves it*. The
recovery matrix's "broken `prev_seal_digest` chain refuses" applies exactly
where no attestation covers the break (PR-02e: producer-negative and
consumer-positive, separately). Re-deriving C1 in B would need C1's whole
proof set. Importing that on every roll is retention policy, not a boundary
mechanism. The registry is how a caller who kept it finds it.

`audit` **owns** the `attested` transition. It writes `audit.json` by the
liturgy. Then, under the anchor lock, it sets `periods[N].attested = true`
in one write. The registry entry for a new period is written **in the same
anchor write as `claimed → open`**, after the first segment is durable. It
is never written before: a crash would leave an authoritative registry row
for a period with no segment (PR-02c).

`claim_successor` moves the head `closed → claimed(claim_id, target_root)`
in one compare-and-swap. `target_root` is the **absolute, normalized** path
(`os.path.realpath`), and it is stored that way. A claimant started with
`--run-root ./r` and restarted with `/abs/r` must compute the same
`claim_id`, or an ordinary restart would need break-glass. Execution
`run_dir`s are relative to the estate root for the same reason.

`claim_successor` is **idempotent on `claim_id`**. The same
`(seal, next_period, root)` may resume its own claim after a crash. The identity is
the claim, not the process. PID, start time and `boot_id` ride on the claim
file for diagnostics only, because a crashed claimant's replacement always
has a different PID. A different `claim_id` against the same `seal_digest`
refuses and names the holder. The head moves `claimed → open` when the first
`segment` record of the new period is durable.

The head is never "`next_period`'s seal digest": that digest does not exist
until the next period ends. The claimant is never keyed on process identity.
Either choice would make an ordinary crash between claim and head move
recoverable only by `--force`, the one operation allowed to fork a lineage.

**The local implementation** is `LeaderLock`'s pattern on the anchor
directory. That pattern solves what a bare `O_EXCL` does not: replacement
and lifetime.

- `anchor.lock` is `flock`ed for the **process lifetime** of whoever leads
  the lineage: the engine in live mode, the sealer in offline mode. The lock
  is re-checked (inode-under-pathname, `LeaderLock.check`) before every
  append, every dispatch, **and every revision-bearing read or subscription
  response**. The control protocol makes those reads leader-only and stamps
  lineage coordinates on each answer (`control-protocol.md`). A displaced
  leader that kept answering `status`, `routes` and backfill until its next
  mutation would serve revisions from a
  lineage it no longer leads (PR-03). Such a read is refused with code
  `lineage_lost`. If the directory is deleted or replaced, the incumbent
  stops on its next act. That is DL-101's bargain: it cannot un-run what
  happened, but it turns a divergence into a recorded stop.
- `claims/<claim_id>.json` is the durable claim. It is written under the
  held lock by the **spool liturgy**: temp, `fsync(file)`, `rename`,
  `fsync(dir)`. Its shape: `{artifact_format_version, claim_id, estate_id,
  prev_seal_digest, next_period, target_root, claimed_at, diag: {pid,
  start_time, boot_id}}`.
- `anchor.json` is written by the same liturgy under the held lock. Its
  shape: `{artifact_format_version, estate_id, head: open|closed|claimed,
  periods: {N: {root, segment_durable, seal_digest, attested}}, reclaimed:
  [{claim_id, target_root, next_period, claimed_actor, at}]}`.

  `reclaimed` is the break-glass ledger. It is append-only and never
  consumed. It is copied into the next opening `segment`'s own `reclaimed`
  field, so the fork is recorded in the lineage's log as well as in the
  fence that permitted it.

  **A period's registry row is inserted when a root first owns it. It is
  provisional until that period's first segment is durable.** Genesis writes
  `periods[1]` in its `absent → open(1, root)`, before any segment exists.
  So that row carries `segment_durable: false`, and every cross-period
  reader ignores a row until it reads `true`. A successor's row is written
  in `claimed → open`, after its segment is durable, and is never
  provisional. Period 1's row flips to `segment_durable: true` in a
  **finalize CAS done right after the segment lands**. This is genesis's
  sixth step. A recovery row covers a crash between segment and finalize:
  resume does the finalize. The finalize is never deferred to the close or
  the first resume of a *running* period; that would leave an engine
  running a durable period that estate-wide readers are told to ignore.
  `seal_digest` is filled at close and `attested` at audit. Period 1 has a
  row too. A registry written only at `claimed → open` would have none for
  it, and estate-wide `audit`, `journal` and `runs` could lose it after a
  physical roll (PR-02f).

  Every head move (the six of the anchor_head machine: the contract
  block's four, which include genesis, and the two moves from `claimed`)
  is one liturgy write. The CAS is
  read-compare-write under the lock. A rename without `fsync(dir)` is not
  durable: a power loss could keep B's first segment and revert the head,
  and a second target would then claim the same seal. Process-kill tests do
  not prove directory-entry durability; §13 says so where it matters
  (PR-02c).
- **`periods` is the archive registry.** The head's `closing_root` is
  overwritten as soon as the head moves on. Estate-wide `journal`, `audit`
  and `runs` still need to find which root holds period N. The registry maps
  every period to the root that holds its segment, seal and attestation.
  Every cross-period reader takes its roots from there.

  **One walk reads it.** `audit`, `journal`, `runs` and `estate prune` each
  address the whole estate the same way: the operator names the lineage
  ANCHOR where a run root would go. Each takes its roots from that one walk,
  in period order. A row is
  passed over only while it is provisional, and the walk reports it. A root
  that the registry names is refused BY NAME when it is missing, holds no
  sentinel, holds one that cannot be read, belongs to another estate, or
  lacks the segment of the period it is registered for. Four private walks
  would be four opinions about what a missing root means. A reader that
  decided "skip it" would answer with a smaller estate and no way to tell.
- Local filesystem only. `runner_ledger.py` says the flock fence is not one
  on NFS. An NFS anchor is refused at startup (PR-04). The check refuses
  every network filesystem type the code lists
  (`boundary.NETWORK_FILESYSTEMS`), NFS among them. When the type cannot be
  read, the check refuses nothing. That is a stated limit on PR-04's
  guarantee (DL-312): local storage for the anchor is the operator's
  rule, and the check is a guard.

**A stale claim is break-glass, not garbage.** A `claimed` head whose root
is unreachable cannot be told apart from one whose root is paused.
Overriding it is `dsl41 estate reclaim --force`. It returns the head to
`closed`. It is recorded in the anchor and in the next `segment` record's
`reclaimed` field, with the claimed actor. It is loud, durable and
attributable, and it is the one path here that can fork a lineage.

**Resume refuses a root the anchor does not name (DL-224).** A recorded root
is this root when the two normalized paths are equal (`os.path.realpath`),
or when the recorded path exists and is the same directory
(`os.path.samefile`). The rule has two parts:

1. **NAMED.** Some registry row's `root`, the head's `root` or
   `closing_root`, or a `claimed` head's `target_root` is this root. A root
   the anchor does not name is refused. This is the early filter.
2. **OWNED.** Let P be the period of this root's newest opened segment, read
   from its opening record. If the registry has a row for P, that row's
   `root` is this root. If it has none, the head is `claimed` with this root
   as `target_root`. This is the authoritative half. Root B's tree, restored
   at registered root A's path, passes NAMED through A's row and fails here,
   because the row for the period its segment holds names B.

The usual causes of a refusal are a copy or a restore at another path, an
abandoned roll target whose claim was reclaimed, and a roll that stopped
before its claim. The rule refuses all of them and does not tell them apart.
A restore must land at the recorded path (`deployment-runbook.md` §2b). A
roll that stopped before its claim is finished by running the opener
(`--open-from`) again. Each of these is the same directory and satisfies the
rule: a symlink at the recorded path that leads to this root, a case-variant
spelling on a case-insensitive filesystem, and a bind mount of the recorded
path. A copy is another directory. NAMED stats every recorded root, so a
stale network path among them can stall a resume. The foreign-estate refusal
(§11 step 2) comes first. There is no override. This model has no verb that
moves a lineage to another path.

**Re-find trigger: a shared store** (the withdrawn HA plan's S8a, DL-189).
If one is built, it **replaces** both this anchor and root leadership as the
sole authority. One transaction consumes `expected_head_digest`, advances
the head and allocates the term. Keeping the file anchor beside the store
would be two leadership truths. The withdrawn HA plan's §2 ACQUIRE (DL-189)
then gains a lineage-head predicate.

## 2. Records

This model adds three record kinds to `docs/runner-design.md` §7's list:
`segment`, `seal` and `decision`. Three kinds are retired: `header` (a
once-per-log header cannot describe a log made of segments), and `result`
plus standalone `effect` (§2.3).

**Retired means refused by name (DL-138).** Nothing writes one and nothing
reads one. A journal that opens with a `header`, or that carries a `result`
or a standalone `effect`, is refused, naming the kind and that entry
(`docs/protocol-evolution.md` §6). The current kinds are `segment`, `seal`,
`decision`, `leader`, `input`, `advance`, `host`, `effect_result`,
`dispatch`, `drop` and `preflight`. `host` is among them: it is current, not
retired. Each keeps the shape `docs/runner-design.md` §7 gives it.

An **unknown** `rec` is refused too, naming the kind, as its own distinct
error. Version gating happens at the opening `segment`. So an unknown kind
inside a version-matched segment is corruption, not a dialect this reader is
too old to see. Skipping it would let a reader walk past evidence and report
a complete replay.

### 2.1 `segment` — the first record of every segment

```json
{"rec": "segment", "segment_no": 2, "estate_id": "…", "period_id": 2,
 "baseline_id": "…", "catalog_hash": "…", "catalog_hash_version": 2,
 "source_bundle_hash": "…", "runtime_hash": "…", "state_machine_version": 1,
 "clock_domain": "real", "first_index": 4187,
 "opens_from_seal": {"period_id": 1, "digest": "sha256:…"},
 "reclaimed": null, "trust_unaudited": null,
 "at": "2026-08-18T02:00:00.000000"}
```

The record has exactly these keys. A reader refuses an unknown key, then a
missing one, then a mistyped one. `first_index` is at least 1.
`opens_from_seal` is null or exactly `{period_id, digest}` (DL-132). The
reader checks `catalog_hash_version` before the rest of the schema
(DL-138).

There is **no** `catalog_hash_v1` field. Version 1 is a retired dialect
(DL-138), and no `segment` records one.

`dsl41_version` is **not** on `segment`. It is per process and already rides
on `leader` (`runner-design.md` §7). Keeping it here would break PR-07's
byte-identical openings on a retry by a later patch version.

`reclaimed` and `trust_unaudited` are `null` or `{claimed_actor, at, …}`.
They are the two break-glass paths that must leave a durable mark on the
period they opened (§1.3, §11). The trust path is not built (§11), so
`trust_unaudited` is always written `null`.

Every segment is **self-describing**. A reader that opens any segment knows
the period, the catalog and the semantics without reading an earlier file.
`opens_from_seal` is null on segment 1 and non-null on every later segment
(I1: every later segment opens a period). `at` on an opening segment **is
T**, the seal's cutoff instant, not the wall time of the restart. That, plus
`next_period` committing every non-derived opening field, lets two openings
of one seal be byte-identical (PR-07).

**`runtime_hash`** is sha256 over the canonical form (§3.2) of
**`RuntimeProfile`**, a typed frozen model, not an open list:

```python
class RuntimeProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    default_tz: str                       # IANA name or "UTC"; never null
    tz_aliases: dict[str, str]            # resolved contents; {} when none, never null
    as_machine: tuple[str, ...]           # sorted, de-duplicated; () when none
    machine_policy: Literal["strict", "local-eligible"]   # the shipped contract
    execution_mode: Literal["tethered", "detached"]
    deadman_us: int | None                # None = no deadman; > 0 otherwise
    fw_default_interval_us: int           # default 60_000_000 (E6); > 0
    cmd_grace_us: int                     # default 10_000_000; > 0
    reconcile_settle_us: int              # default 5_000_000; >= 0
    spawn_window_us: int                  # default 5_000_000; >= 0
    retry_horizon_us: int                 # the §9 soft gate; default 60_000_000; > 0; carried here so audit can read it
    semantics: dict[str, str]             # semantic-switch overrides only (DL-252); {} when none, never absent
```

`semantics` holds only the explicit overrides of the semantic switches
(runner-design §8a). Each name and value must be in the registry. An
explicit default is normalized away, and the defaults live in code. The
field is always written, `{}` when empty (§3.2). A manifest whose profile
lacks it is refused like any other missing field. Replay, audit and the
classifier read the switches from the pinned profile, as they read
`tz_aliases`. Replay refuses a period whose manifest is missing or not bound
to its segment.

Seconds from the CLI convert to microseconds by
`round(seconds * 1_000_000)`. Every duration is present with its resolved default and is
validated to the constraint shown. A negative grace or a zero poll interval
is refused at construction. Nothing is null except `deadman_us`. An absent
`default_tz` means `"UTC"`. `as_machine` is de-duplicated and sorted by the
model itself.

The `artifact_format_version` lives on the **manifest** that carries the
profile, not inside the profile. Two version fields with no equality rule
would be two authorities.

PR-15a is the obligation for the CLI-to-profile normalization. It covers
omitted and explicit defaults, `None → UTC`, `local-eligible`, a duplicate
`as_machine`, fractional seconds, and each duration tested against **its own
stated bound**. Zero is legal for the two `>= 0` windows and refused for the
`> 0` intervals.

There is **no** `machine_map` field. It could only be mistaken for the
mutable role→executor route table, which is carried state and not identity.

PR-15's cases are **derived from the model's fields**, the DL-83
discipline. They are not taken from a prose list of "every launch option
that changes interpretation or dispatch". Under a prose list, a hash that
omitted `grace_seconds` would pass while a patch quietly changed how a live
C1 command is killed under C2. A field added later is tested by default. A
runtime-hash golden vector (PR-08c) pins the bytes.

There are **two manifest models**, in the same way as `StagedNextPeriod` and
`CommittedNextPeriod`:

- The CLI stages `staged_manifest.json`: `{artifact_format_version,
  catalog_hash, catalog_hash_version, source_bundle_hash, runtime_profile:
  RuntimeProfile, runtime_hash, state_machine_version}`. It holds nothing
  the engine owns.
- The engine writes `manifest.json` at install: the staged fields plus
  `{period_id, baseline_id, clock_domain, segment_no, first_index}`. It
  keeps the staged file beside it.

"Validates exactly the staged bytes" is about the bundle and the staged
manifest. The committed manifest is the engine's own output. Resume checks
it against the committed `next_period` (PR-22).

Identical JIL launched with `--timezone UTC` and then with
`--timezone Europe/Zurich` has an unchanged `catalog_hash` and different UTC ticks.
Without `runtime_hash`, classification would report that nothing changed.

The affinity route table is **not** in `runtime_hash`. The withdrawn HA
plan's §4 (DL-189) makes role→executor a mutable authoritative row, revised
under epoch and CAS, and says a remap is *not* a re-baseline. It is carried
state (§3.3), not period identity.

A period's semantics are
`(catalog_hash, runtime_hash, state_machine_version)`. A move in either of the first two is a new period.
So a runtime-profile change with no catalog change is a transition.

**`state_machine_version` may not change across a transition.** A sealer
cannot dry-run `open_from_seal(C2)` under a new version. One executable
implements exactly one `STATE_MACHINE_VERSION` and refuses any other. So a
v1 engine cannot dry-run v2, and a v2 binary cannot lead or replay C1.
Draining every executing job does not create a state translator. Nothing
proves that carried timers, latches, globals or capacity are valid under v2
semantics. So `next_period.state_machine_version ==
seal.state_machine_version`, and the readiness gate enforces it (PR-17). An
SM bump is a full drain and a new estate. SM bumps are deliberately rare
(DL-100): the package version moves for a typo, and the SM version moves
only when the derivation does. DL-138's evolution contract makes the rule
explicit and permanent: a semantics change is a full drain and a new-estate
genesis, and no extension lifts it (`docs/protocol-evolution.md` §1).

### 2.2 `seal` — the last record of a period's last segment

```json
{"rec": "seal", "estate_id": "…", "period_id": 2, "closes_at_index": 5310,
 "at": "2026-08-19T02:00:00.000000", "digest": "sha256:…",
 "next_period_id": 3, "next_baseline_id": "…", "catalog_hash_version": 2,
 "source": "request", "request_id": "…", "request_fingerprint": "…",
 "claimed_actor": "alice@ops-laptop", "force_seal": false}
```

`source` has one value, `"request"`.

The record is the **commit point**. It is also the boundary's **decision**,
which is what makes a live seal request answerable at all. An ordinary
`decision` record cannot decide a `seal` request over the control socket:

- written before the seal, a crash leaves a durable "applied" for a boundary
  that never happened;
- written after it, it breaks the rule that no record follows a seal;
- not written at all, a lost response cannot be retried despite the
  promised `request_id`.

So the `seal` record carries the request's `request_id`, the `claimed_actor`
and a **fingerprint over the complete envelope**: `source`, `baseline_id`,
`epoch`, `next_period`, `force_seal` and `claimed_actor`. This follows
`concurrency-model.md` §6's rule that the fingerprint is the whole semantic
envelope. Two requests with one `request_id` and one `next_period` but a
different `force_seal` or actor **collide**. Force is an authorization and
the actor is attribution; a retry may swap neither. An exact retry is
answered from the committed seal, in the new period, without touching the
decision index.

**A committed seal's retry has its own route, ahead of the baseline gate.**
The generic v3 parser rejects a foreign `baseline_id` before it reads
`request_id`. A retry of the seal that closed C1 carries B1, while C2
answers under B2. Without a dedicated route the promise above could not be
kept. The engine of period N+1 keeps the `seal` record it opened from. It
checks an incoming `seal` request's `(request_id, fingerprint)` against that
record **before** the current-baseline check, and answers a match from the
record. A collision is refused with code `seal_retry_mismatch`. The lookup
reaches exactly one seal back. A retry of an older seal is refused as a
stale baseline; that is a loss of liveness, not of safety (PR-30e). The
`dsl41 seal` CLI, live or offline, composes its own request under the same
rule. It reads the root's lineage fresh with `boundary.select_seal`. It
never goes by which sidecar FILE happens to exist under `seals/` (DL-169).

**An uncommitted seal request is unseen.** The `seal` record is the
boundary's commit, and the retry route consults only a *committed* seal. The
sidecar also carries the request identity, in `boundary_request`. It does so
because a physical roll imports the sidecar and not the old WAL, and because
an attested predecessor WAL may be pruned lawfully: retry evidence kept only
in the WAL would vanish exactly when PR-02a says it may (PR-30e). But an
orphan sidecar is ignored by rule, so its copy of the identity names nothing
until the record lands. A request that crashed before its record left
nothing behind. Its retry is a fresh request that attempts the seal again.
Only a committed seal is ever deduplicated. Sealing twice is impossible: a
second attempt finds the first one's record if it landed. A retry before the
commit is not "refused as unknown": no record could support that refusal,
and it would confuse two protocol outcomes.

While a seal request is in flight, another seal request is refused with code
`seal_in_flight`. The answer names the request in flight (DL-295).

The v3 request and answer:

```json
{"cmd": "seal", "v": 3, "baseline_id": "…", "epoch": 7, "request_id": "…",
 "next_period": {…StagedNextPeriod, §3.4…}, "stage_digest": "…",
 "force_seal": false, "claimed_actor": "…"}

{"ok": true, "kind": "seal", "decision": "applied", "period_id": 2,
 "digest": "sha256:…", "next_period_id": 3, "next_baseline_id": "…", …header…}
```

**The route wire is frozen with v3 and is not built (below).**
`Attempt.host` becomes a discriminated union `HostCommand | RouteCommand`.
On the wire it is the existing `host` cmd with a fourth verb:

```json
{"cmd": "host", "v": 3, "baseline_id": "…", "epoch": 7, "request_id": "…",
 "verb": "route", "payload": {"id": "<role>", "executor_id": "…"},
 "expect": {"route:<role>": 3}, "claimed_actor": "…"}

{"cmd": "routes", "v": 3, "roles": ["<role>", …]}
→ {"ok": true, "routes": {"<role>": {"present": true, "executor_id": "…",
    "state_rev": 3}}, …header…}
```

The `routes` query keeps the `hosts` query's corner cases. An absent role
answers `{"present": false, "state_rev": 0}`. Omitting `roles` answers the
whole table, because the takeover barrier reconciles every route as it
reconciles every host. The subscription gap record (§11) is
`{"gap": true, "earliest_retained": <index>}`.

The WAL record is `host: {verb: "route", id, executor_id}` (§3.3). The
answer has the same decision shape and four outcomes as every host verb.
The wire is fixed here so that two competent implementations cannot choose
incompatible JSON for one fact. **The wire is specified and not built.** The
`RuntimeState` storage, the `route` verb, the `routes` query and the
`route:` `expect` namespace do not exist. The table in use is the one
implicit row §3.3 describes. This section is what the unit that adds the
storage implements. It changes the producer, never the seal artifact.

`expect` is absent by design on the **seal** request: a seal addresses no
row.

**`request_id` collides across the whole period, not only seal to seal.**
Readiness checks the seal request's `request_id` against the current
period's `DecisionIndex`. It refuses a reuse under a different fingerprint,
on `control-protocol.md` §3's rule: one `request_id`, one command. So an
ordinary `STARTJOB` R and a `seal` R cannot both name authoritative
decisions (PR-30c). Refused, rejected and unknown keep `control-protocol.md`
§3's meanings. The fourth outcome, a retry that finds the seal committed, is
`applied` from the new period. This closes a window: the seal commits, the
socket drops as the engine exits, and the client holds `unknown` with no
durable key to ask about (PR-30a).

The record duplicates the fields recovery needs to select the sidecar and to
refuse a wrong one. `digest`, `period_id`, `closes_at_index`, `at`,
`next_period_id` and `next_baseline_id` are among them. §11 requires
**every** duplicated field to agree. The comparison is derived from the
record shape above, not from a list kept beside it (DL-145). So a field
added here is compared without extra work.

### 2.3 `decision` — one atomic batch

```json
{"rec": "decision", "index": 5310, "request_id": "…", "decision": "applied",
 "reason": null, "code": null, "revisions": {"job:nightly": 13},
 "legacy_batch": false,
 "effects": [{"effect_id": "e5310:KILL:nightly.7", "kind": "KILL",
              "job": "nightly", "run_number": 7, "run_id": "…", "index": 5310,
              "executor_id": "local", "generation": 0, "at": "…"}]}
```

`concurrency-model.md` §4 step 7 requires the decision, revisions, outbox
entries and `applied_index` to commit **atomically**. Separate `_write`
calls for `result` and for each `effect`, each with its own fsync, do not
meet that (DL-118). This is the answer on a file substrate: one line, one
fsync, on the same argument `Journal.admit` makes for the input side.
Without it, a real window is invisible to every precondition. The result is
fsynced, and the process dies before the KILL effect is written. Recovery
then finds every attempt decided and an empty outbox, and the terminal row's
command is still alive.

`effects` is written in **admission order**. `Outbox` treats insertion order
as admission order, and a SPAWN must precede a later KILL for the same run.
Every effect carries `{executor_id, generation}` from birth. The withdrawn
HA plan's §4 (DL-189) resolves affinity **inside** the effect-intent
transaction and forbids a remap from moving an existing effect. An effect
without a generation cannot prove at dispatch that it did not read a newer
one (PR-16).

**Every SPAWN effect also carries `run_id`, minted in the same transaction
(DL-118).** Suppose an adapter minted it when its task started, after the
durable effect. If the engine died between the supervisor writing R1's index
and the engine recording the outcome, the engine would resume with a pending
effect and no memory of R1. Re-dispatch would mint R2, unless recovery
invented a spool-lookup rule. `concurrency-model.md` §5 binds `run_id`
before the attempt; the seal needs it first (PR-36a). One key then runs
through the WAL, the supervisor index, the receipt and the retry, and
`(job, run_number) ↔ run_id` is one-to-one by construction. The writer
refuses an effect without a generation, a SPAWN without a `run_id`, and a
`run_id` outside the uuid4 grammar.

`legacy_batch` is on **every** decision and is **required false**. `true` is
a retired dialect: it named a batch folded from separate legacy fsyncs. A
record that carries it is refused, naming DL-138. A missing flag, or one
with a non-boolean value, is **malformed** and refused as its own distinct
error. An absent flag is not a false one; a reader that defaulted it would
accept a record no writer of this estate wrote. One validator decides the
three cases, so every consumer that parses a `decision` inherits them.

The record carries `index`, not `seq`, as the retired `result` did. `seq` is
the subscribe cursor, and a decision shares its attempt's number (DL-89).
`decision` is an unsequenced, at-least-once record on the subscribe stream.

**This is a wire break.** `control-protocol.md` §5 promises raw `result`,
`effect` and `effect_result` records to subscribers. A v2 client waiting on
`rec == "effect"` would silently stop seeing intents. That is not additive.
So the protocol is **v3**, on the precedent DL-90 set (v1 was removed, not
deprecated). There is no compatibility projection, because it would be a
second record shape for one fact. `effect_result` is unchanged.

A decision carries `code` (DL-272). It is null on an application. On a
rejection it is the code of the reason (`control-protocol.md` §2), written
beside the prose `reason`. The writer refuses a rejection without one. The
codes a rejection may store are an append-only set
(`runner_codes.STORED_CODES`). A decision without `code` reads as null. The
reader treats a stored code as opaque: it never requires one and never
checks it against the registry. A replay derives nothing from it, so it
changes no state and needs no `state_machine_version` bump. An exact retry
and `original_decision` answer the stored code. Unlike the wire break above,
this field is additive.

There is deliberately **no transition record**. A period opens because a
`segment` says so, and closes because a `seal` says so. The seal's
`next_period` (§3.4) commits the opening.

### 2.4 `leader` and the epoch

The `leader` record's shape is as `runner-design.md` §7 gives it.
Allocation reads the log and the seal together. The next term is one past
the highest `leader` epoch in the segments after the seal, and never below
`seal.epoch + 1`. I2 makes the epoch estate-monotone, so a new period's
first term is `seal.epoch + 1`.

## 3. The seal artifact

There are three writes, in this order. The order is the whole durability
argument:

1. `seals/<period_id>.json`, with the liturgy the spool uses: same-directory
   temp file, `fsync(file)`, `rename`, `fsync(dir)`.
2. the `seal` record, appended and fsynced as the final record of the segment.
3. the anchor CAS `open → closed` (§1.3).

A crash between 1 and 2 leaves the sidecar **orphaned**. No record names it,
recovery ignores it, and the period is still open. A crash between 2 and 3
leaves the seal committed while the head still says `open`; resume does the
CAS. When the record lands, the sidecar is already durable. When the head
moves, the record is durable.

The `seal` record is always fsynced, even in a journal of the virtual clock
domain. An fsync error on it is an unknown outcome, and the engine
fail-stops.

### 3.1 Shape

```json
{
  "artifact_format_version": 1,
  "estate_id": "…",
  "period_id": 2,
  "baseline_id": "…",
  "catalog_hash": "…",
  "catalog_hash_version": 2,
  "source_bundle_hash": "…",
  "runtime_hash": "…",
  "state_machine_version": 1,
  "closes_at_index": 5310,
  "closed_at": "2026-08-19T02:00:00.000000",
  "clock_domain": "real",
  "epoch": 7,
  "prev_seal_digest": "sha256:…",
  "scheduler_admitted_through": "2026-08-19T02:00:00.000000",
  "boundary_request": {"source": "request", "request_id": "…",
                       "claimed_actor": "alice@ops-laptop", "force_seal": false},
  "request_fingerprint": "…",
  "forced_gate": null,
  "state": {
    "jobs":    {"<name>": { …JobRuntime, incl. reservations, waiter_seq,
                            ran_members, start_period, window_skipped_members… }},
    "globals": {"<name>": { …GlobalRuntime… }},
    "hosts":   {"<id>":   { …HostRuntime, no last_contact, deadman_us: null… }},
    "routes":  {"<role>": {"executor_id": "…", "state_rev": 3}},
    "timers":  [[ "<due>", 41, { …Event… } ]],
    "timer_seq": 41,
    "consumed": {"r:FUEL": 3},
    "enqueue_counter": 12,
    "now": "2026-08-19T02:00:00.000000"
  },
  "outbox_pending": [ { …Effect… } ],
  "executions": [ { …§3.5 row… } ],
  "classification": { "<job>": {"class": "A", "assumption": "…"} },
  "next_period": {
    "catalog_hash": "…", "catalog_hash_version": 2, "source_bundle_hash": "…",
    "runtime_hash": "…", "state_machine_version": 1, "artifact_format_version": 1,
    "period_id": 3, "segment_no": 3, "baseline_id": "…", "clock_domain": "real",
    "first_index": 5311             # the last five: engine-derived; not in stage_digest
  },
  "digest": "sha256:…"
}
```

`epoch` is at least 1. `prev_seal_digest` is null on period 1.

**`next_period` commits every non-derived opening field.** That includes
`clock_domain` (resume refuses a domain change, and the opening must be able
to refuse one too), `segment_no`, and the target period's
`artifact_format_version`. So `stage_digest` binds that version, and two
staged manifests that differ only in it cannot share a digest. An in-place
opening and a fresh-root opening therefore cannot choose differently
(PR-07).

There are **two frozen models**, not one. The line between them is **who may
say it**:

- `StagedNextPeriod` is what a client may propose: `catalog_hash`,
  `catalog_hash_version`, `source_bundle_hash`, `runtime_hash`,
  `state_machine_version` and `artifact_format_version`. It is the identity
  of *what* opens next.
- `CommittedNextPeriod` adds what only the engine may derive:
  `period_id = current + 1`, `segment_no = period_id`,
  `baseline_id = sha256(canonical{estate_id, period_id, stage_digest})`,
  `clock_domain = current` (a domain change is refused, as resume refuses
  it), and `first_index = closes_at_index + 1`.

`stage_digest`, `candidate.json` and the request `fingerprint` are over the
first model. The sidecar's `next_period`, the `claim_id` and the opening
`segment` carry the second. One type would force `first_index` to be
omitted (which breaks the every-field-present rule), null (which is not what
"excluded" means) or guessed (the staged-`first_index` fault below). PR-08e
is the golden vector for both.

**`baseline_id` is derived, not minted.** Audit must reproduce every seal
field from the opening seal, the WAL, the spool and the manifests. A random
UUID appears in none of them. A wrong audit could only copy it from the seal
under audit and check its shape, so a consistent mutation across sidecar and
record would pass. Derived from evidence that exists before the boundary, it
is reproducible and still unique per boundary (PR-47d). The request
fingerprint is not in it. The fingerprint includes the epoch and the actor.
A same-stage retry after a crash runs under epoch+1, so it would have a
different fingerprint and a different required baseline, while the installed
candidate's committed manifest carried the old one. The result would be
unopenable or inconsistent. `{estate_id, period_id, stage_digest}` names the
only boundary that can open there, and nothing else belongs in it.

The client does not stage `period_id` or `segment_no`. If it could, period 2
could open period 4. Attestation 3, which the induction requires, could then
never exist: an unauditable lineage by construction (PR-05c).

**`first_index` is not staged.** It is *derived boundary output*:
`closes_at_index + 1`. `closes_at_index` is unknown until the cutoff barrier
has admitted every tick due at T and fired every timer through it. Suppose a
client staged `first_index = 101` before the barrier ran. A cutoff tick
would take index 101, the seal would close at 101, and C2 would open reusing
it. I2 would break, and every cursor and decision lookup would be ambiguous.
The engine computes `first_index` after §6 step 6. It writes it into the
sidecar's `next_period` and the opening `segment`. `stage_digest` excludes
it (PR-05b).

**`boundary_request` is authoritative input in three of its four fields.**
Its fields are `{source, request_id, claimed_actor, force_seal}`.
`request_id`, `claimed_actor` and `force_seal` come from the request and
nowhere else. On an access-armed estate, the perimeter has already
overwritten `claimed_actor` with the authenticated spelling before the
request reaches this tier (`docs/access-model.md` §3, DL-148). This tier
still reads the request and nothing else. `source` is `"request"`, the
field's one value (DL-138). Audit checks it for equality between the record
and the sidecar (§11). A live seal through the control socket and an offline
seal from the CLI are the same kind of boundary: a request that carries an
id its caller minted. The field does not tell them apart. Audit could do so
only through a `leader` record, and that record carries epoch, time, pid,
host and version, and nothing that names the process's mode. There is no
`adopt` value: the estate-adoption path is retired (DL-138), and the derived
adoption `request_id` with it.

`request_fingerprint` is **derived** over the envelope, and audit recomputes
it.

`forced_gate` is **gate output**: `null`, or `{"gate": "retry_horizon",
"horizon_us": …, "observed_age_us": …}`. Audit re-derives it from three
inputs:

- the profile's `retry_horizon_us` **in the closing period's committed
  manifest**. That is C1's, because the gate protects retries of requests
  admitted under C1. The staged C2 profile has no say;
- the WAL's last admitted **externally requested attempt with a durable
  decision**;
- T.

The rule says "attempt", not "mutation". The frozen exact-retry promise
covers a journaled *rejection* and an applied no-op as much as a state
change. So a `rejected` CAS loser two seconds ago must hold the gate exactly
as an applied `STARTJOB` would.

The truth table:

| observed age | `force_seal` | result |
| --- | --- | --- |
| ≥ horizon, or no externally requested attempt with a durable decision in the period (age = ∞) | either | gate passes; `forced_gate: null` |
| < horizon | `false` | refuse |
| < horizon | `true` | commit with `forced_gate` populated |

An unnecessary `--force-seal` is recorded in `boundary_request.force_seal`
and engages no gate. The three fields are not one excluded block. Excluded
together, a consistent rewrite of the fingerprint or the observed age would
pass audit (PR-47b).

The name is "claimed actor", not "principal". This tier does no
authentication of its own. The name records what the request carried, and
the seal must not spell that claim as if this tier had proved it. On an
armed estate the carried value is the perimeter's authenticated spelling. On
an unconfigured estate it stays the caller's bare claim (DL-146, DL-148).

`classification` records the §10 verdict and every A assumption. It is where
"assumption recorded" lives. Each entry's wire key is `class`. Its
`assumption` is non-null exactly when the class is A.

### 3.2 Canonical form (normative)

The digest is computed over a canonical serialization, never over incidental
JSON bytes. Otherwise `audit` would report a mismatch for a re-serialization
that changed nothing.

- JSON, UTF-8, `ensure_ascii=false`, separators `(",", ":")`.
- **Strings are Unicode scalar values.** Python's decoder accepts
  `"\ud800"`, an unpaired surrogate, as a string. A control server that
  accepted any string as a global value would let one in. The journal writes
  it safely under ASCII escaping, but encoding it later with
  `ensure_ascii=false` raises. So one legal control input could make the
  estate unsealable. Every ingress (the control socket, catalog loading,
  spool decode) therefore refuses a non-scalar string, and
  canonicalization never meets one (PR-10a).
- Every object's keys are sorted by Unicode code point, at every depth.
- The value grammar is object, array, string, integer, boolean, null. **No
  floats at any depth.** `deadman_s` is a float in `HostRuntime`. Its
  canonical form is `deadman_us: int | null`, which is also what
  `RuntimeProfile` stores.
- Datetimes are ISO-8601 naive UTC with **exactly six fractional digits**,
  zero microseconds included. The rule governs the values this form
  encodes. A schema field that stores a timestamp as a **string** carries the
  same spelling: `claimed_at`, `audited_at`, `archived_at`, and a
  `reclaimed` entry's `at`. The rule does not reach the WAL, which is not one
  of the artifacts below.
- **Typed schema fields are always present**: explicit `null` for an unset
  optional, `[]` or `{}` for an empty collection. Default-filling happens
  **only at typed schema boundaries** (`JobRuntime`, `HostRuntime`,
  `Effect`, the seal's own top level) and **never inside opaque JSON**.
  `Event.payload` is opaque: `{}` and `{"x": null}` are different values and
  must digest differently. A canonicalizer that "drops empty or default
  values recursively" does not conform.
- Collections without a semantic order sort by a stated key:
  `reservations` by `bucket` (after duplicate buckets are rejected), and
  `ran_members` and every other set by value. Ordered collections keep their
  order: `timers` by `(due, token)`, never by heap-array layout;
  `outbox_pending` and `executions` by `(index, effect_id)`.
- Duplicate object keys are **rejected at decode**.
- Escaping is pinned. `"` and `\` are escaped. `\b \f \n \r \t` use the
  short form. Every other **Unicode Cc** character (U+0000–U+001F, U+007F,
  U+0080–U+009F) is written as `\u00xx` in lower case. DL-128 pins the set,
  because "control character" alone reads two ways. `/` is never escaped.
  Nothing else is escaped.
- `digest` is `"sha256:" + hexdigest` over the canonical bytes with the
  **top-level** `digest` key removed, and only that one. A nested opaque
  payload key named `"digest"` is data and stays. A recursive "strip every
  digest key" implementation would collide documents that differ there
  (PR-13).
- **A digested artifact's stored bytes ARE its canonical bytes.** Its reader
  checks that first. This holds for the seal sidecar, the attestation and
  the archive receipt. A whitespace-padded copy, a key-reordered copy and a
  copy that omits a defaulted key all carry the real artifact's digest, and
  each would pass every later check. This rule separates the artifact from
  its look-alikes. Each reader names its own artifact when it refuses.
- **A typed field is read as the type it was written, never coerced into
  it.** `docs/protocol-evolution.md` §1's closed-artifact row states the
  rule and the mechanism per reader (DL-168).

**A golden vector ships with the spec.** It is one document that exercises
control characters, `/`, non-ASCII, nulls, defaults, empty and non-empty
nested payloads, an array whose order matters, and six-digit datetimes. Its
canonical bytes and digest are fixed in the test suite (PR-08). Equality and
sensitivity tests alone would pass a canonicalizer that is consistently
wrong.

**Canonicalizability is a liveness property.** The grammar refuses floats at
write time. If any `Event` payload that the oracle can enqueue as a timer
contained one, the estate could not be sealed while that timer was armed.
Every timer payload the oracle constructs is canonicalizable. That is an
obligation (PR-09), not an assumption.

**One shared `artifact_format_version`** governs this section and every
artifact this spec defines: the seal sidecar, the attestation, the period
manifest, `staged_manifest.json`, `candidate.json`, `sources.json`,
`receipt.json`, `reply.json`, every `watch.jsonl` line, the `run_id` index
entry, `anchor.json`, the claim file, the sentinel, and the archive receipt
(§12a). Each carries the field. Each is refused when it names a version this
binary does not implement (PR-08d). Each digested one is digested over its
canonical bytes with only its top-level `digest` removed. There is one
version, not one per artifact: a change to canonicalization moves the bytes
of all of them at once, so one version is the honest count. There is no
separate `seal_format_version`. `docs/protocol-evolution.md` §1 puts every
artifact named here on a compatibility row and states its lifetime. An
artifact serialized by incidental `json.dumps` settings passes a test on one
binary and fails after a patch changes serialization. The golden vectors
(PR-08, PR-08a, PR-08b) exist for exactly that.

### 3.3 Carried and not carried

| carried | why it cannot be reconstructed |
| --- | --- |
| `jobs` (incl. `reservations`, `waiter_seq`) | authoritative rows |
| `globals` | authoritative rows |
| `hosts`, with `last_contact` **omitted from the shape** and `deadman_us` **present and null** | durable routing state (`concurrency-model.md` §8). See the not-carried rows for the two exclusions, and the note on host return below |
| `routes` | the role→executor table, authoritative under CAS (the withdrawn HA plan's §4, DL-189). See the note below |
| `timers` + `timer_seq` | an armed deadline is state that no status field records; the token carries the firing order across jobs |
| `consumed` | irreversible depletion (DL-50) that no row holds — §5. **Keys survive their resource.** A `consumed["r:FUEL"]` whose resource C2 removes is kept as a ghost bucket. If C3 brings `FUEL` back, its consumption is still spent. A loader that rebuilt capacity from the catalog alone would silently refund it (PR-19a) |
| `enqueue_counter` | the high-water mark of the waiter-rank allocator |
| `now` + `clock_domain` | feed times must not decrease across the boundary |
| `scheduler_admitted_through` | which same-instant ticks were consumed — §6 |
| `outbox_pending` | intents recorded and not delivered |
| `executions` | the lifecycle state of every non-terminal run — §3.5 |
| `classification` | the transition's verdicts and A assumptions |
| `next_period` | the opening this boundary commits — §3.4 |
| `estate_id`, `epoch`, `prev_seal_digest` | lineage |

**Host return is not this spec's.** It names no admitted
`host{verb: register}` input. A returning host must present its generation and prove it
self-fenced (CM-12). It must be reconciled against two frozen rules: a stale
generation is refused, and ordinary re-registration preserves operator
state. None of that has a producer before the relay exists (DL-97). On one
host an evicted row is a dead end. `evict local` leaves `local` routing
nothing, and nothing brings it back, because the un-evict is the relay's
act. So this spec records **nothing** for a host's return. It carries an
evicted row as it stands. It leaves the register record, its proof and its
transition table to the HA track, where the relay is built. Registration
stays unjournaled here: the genesis seed is identical on every replay, and a
deadman refresh is unprojected (PR-24c).

**The `routes` row is a row like the other three.** It is
`RouteRuntime {executor_id, state_rev}`, frozen, owned by `RuntimeState`,
and projected on the same rule. It is read through a v3 `routes [roles]`
verb that answers `{present, executor_id, state_rev}` per role. It is
addressed by the fourth `expect` namespace, `route:<role>`. **The storage
and that verb are specified and not built (§2.2).** So the table is
projected as one row whose role IS the local executor's id, at revision 0,
and the seal carries it in the frozen shape.

**A route names an executor and nothing else.** A route with a `generation`
of its own would raise the question of what a stale route means. The answer
is always "the evicted-host case", which §8 defines, the HA track builds,
and this spec does not redefine. So the generation is **not** on the route.
At effect birth, `executor_id` comes from the route and `generation` from
the host row's **current** value, exactly as `plan_effects` binds it. A
stale route cannot exist. An evicted host routes nothing, so the routing
gate holds an effect born for one as pending. §8's re-drive as a new run
stays where it is: not built until HA's relay, and out of scope here.

**A remap is an admitted input** on the `host` record's pattern (DL-94):
`host: {verb: "route", id: <role>, executor_id}`. It is applied to the
owner and makes no oracle event. It is rejected if `executor_id` names no
host row. A→B→A moves the revision twice, and the seal carries it (PR-16b).

| not carried | reason |
| --- | --- |
| `last_contact` | outside the semantic projection (DL-95). Replay re-seeds it, so a new leader **over-waits** and does not evict early. A stale one would let the new period conclude that a quarantined host's deadman expired: the one state that permits a double run |
| `deadman_us` on a host row | DL-95's other half. See the note below |
| the decision index | `_by_index` is local to the log; §9 handles retries |
| `unresolved` / `outcome_unknown` | a projection. It derives from the bound executor's quarantine (`hosts`) plus the absence of evidence (`executions`), and both are carried |
| `_trace`, `_emitted`, `_queue`, `_in_wake` | transient or derived |
| `_dispatched` | **derived, and its reconstruction is normative.** See the note below |
| `_referencers`, `_bucket_cap` | derived from the catalog |
| `Scheduler._next`, `_CalCache` | replaced by the §6 watermark |
| `spec_drift` | disk state |

**The host row's `deadman_us`.** DL-95: *"read back from the host, never
declared by the leader."* If the seal carried it, C2 could restart the
supervisor at 120s while the row still said 60s. Eviction would then be
permitted 60s before the supervisor's real kill bound: a double run. The
row's deadman is **null until the host registers again in the new period**.
A host with a null deadman cannot be evicted except by force, which is the
safe direction. `runtime_hash` carries the *requested* value. The row
carries the *observed* one, and only the observed one may enter the bound
(PR-24a).

**`deadman_s` also leaves the host's semantic projection**, beside
`last_contact`, in `_UNPROJECTED_HOST`. `register_host` changes
`deadman_s`, and startup registers with no journal record. A projected
`deadman_s` would move the row's `state_rev` on re-registration. Audit,
replaying from a seal that says revision 5, could then not derive the 6 that
the next seal carries. The value is observed liveness configuration, not
semantic state. Nothing an operator holds an `expect` against depends on it,
and the eviction gate reads the current row value whatever the revision.
The exclusion is DL-126's, and `concurrency-model.md` §3 states it too
(PR-24b).

**`_dispatched` is rebuilt, never carried.** Its value is
`{job: run_number for every row with run_number > 0}`, exactly as `runner_startup.py` seeds it
at resume. It is not a cache. `plan_effects` plans a SPAWN only when
`run_number > _dispatched[job]`. An opener that left it empty would let a
legal `CHANGE_STATUS STARTING` on a job that completed run 7 plan run 7
**again**, and, once the SPAWN tombstone is lawfully pruned, execute it
(PR-18a). `open_from_seal` does not hold it. Resume rebuilds it over the
oracle's rows after the segment's replay, and those rows include the
carried ones.

### 3.4 `next_period` — the seal commits the opening

Suppose a seal named only the closing period's facts, and the process died
between the `seal` record and the next `segment`. Recovery could not know
**which** period to open: the operator restarts with C3, and recovery opens
C3 from a boundary that committed C2. So `next_period` is chosen and durable
**before** the seal commits, and recovery opens exactly that. A `segment`
whose period identity disagrees with the preceding seal's `next_period` is
refused.

### 3.5 `executions` — a discriminated lifecycle, not one row

`outbox_pending` holds intents not delivered. An applied SPAWN for a run
that is still live is not pending. A seal that carried only the RUNNING row
would lose `run_id`, `executor_id`, `generation` and the spool binding, and
resume could not say which executor owns the run. One row shape does not
describe the states the code has, so `executions` is a **discriminated
union**:

| kind | when | fields |
| --- | --- | --- |
| `pending_spawn` | SPAWN recorded, not delivered | `{job, run_number, effect_id, index, run_id, executor_id, generation}` — `run_id` from the effect (§2.3), not from an adapter |
| `bound` | SPAWN applied and the spool binding known | `{job, run_number, effect_id, index, run_id, executor_id, generation, run_dir}` — `run_dir` **relative to the estate root** |
| `fw_watch` | a live FW run | `{job, run_number, effect_id, index, run_id, watch_seq, previous_size, stable_polls, next_poll_at}` — `run_id` from the effect like every execution; no process behind it. Its run directory holds only `watch.jsonl` |

Every execution's `run_id` is the effect's (§2.3). Every `effect_result`
that carries a `run_id` **must equal** it, and `open_from_seal` refuses a
disagreement (PR-22). `start_period` lives on `JobRuntime` (below) and on
**no** execution entry: having it in both places would make two authorities
for one fact.

**`start_period` is on the row.** `start_run` sets `JobRuntime.start_period`
beside `run_number` and `started_by`. So it covers CMD, FW, a SPAWN pending
across several periods, and a **box**. A box has no execution entry at all
and can be live across several unchanged periods.

**There is no applied-but-unbound kind, and the gate forbids the state.**
`_apply_spawn` creates the adapter task and records
`effect_result{applied}` at once. The task creates `run_dir`, and the
supervisor writes the binding afterwards. (The `run_id` itself is already on
the effect, §2.3.) In the real-domain loop `_settle` returns without
yielding, so a queued seal can observe a SPAWN that is applied and not yet
bound. This model does not add a fourth kind with its own recovery. §8
requires **every applied CMD SPAWN to be bound or terminal** before the seal
commits. That takes milliseconds, and the sealer waits (PR-27).

**There is no `terminating` kind.** Carrying one, while the seal refuses
under a KILL ladder without proof, would set two obligations that no
implementation could both pass. The gate wins. An unresolved KILL ladder is
a few seconds of `grace_seconds` plus a signal. The sealer **waits it out**,
within §8's bound (DL-312).
It does not snapshot a half-run ladder whose remaining grace deadline it
would then have to carry.

The gate does not cover a **resume gap** that a seal makes easier to reach.
`_apply_kill` records `applied` when the cancellation is delivered, and the
TERM/grace/KILL ladder runs on the way out of the task. So an engine that
dies mid-ladder leaves a live wrapper under a terminal row. A resume that
re-drove only *pending* KILLs would miss it. So `runner-design.md` §7 has
its own obligation (PR-33): at resume, a live wrapper under a terminal row
is re-driven **whatever the KILL effect's recorded state**.

`fw_watch` exists because the FW adapter's progress (last observed size and
stable-poll count) decides when the watch completes, and a restart resets
both. Carrying it keeps an unchanged watch's behaviour identical across the
boundary (PR-34).

**But the progress must be evidence, not memory.** Progress held in a local
variable, fed by unjournaled `os.stat` calls, would leave an audit that
replays the START input unable to derive whether the seal should say
`previous_size=10`, `null`, or a completed watch. So the FW adapter has a
**spool**, and it is **append-only**: `runs/<job>.<run_number>/watch.jsonl`.
It has one line per poll, `{artifact_format_version, kind: "poll", at,
run_id, exists, size, qualifying, stable_polls}`, fsynced per line,
including polls that changed nothing.

A single overwritten `watch.json` would fail twice. `next_poll_at` moves on
every poll while the file does not, so audit could not reproduce it. And a
C2 observation would overwrite the value at T, so a later audit of C1 would
see C2's evidence. With a log, the seal's `fw_watch` is a pure function of
a **prefix**. `watch_seq` names the prefix: it is the count of durable lines
at T. Wall time cannot name it, because `at ≤ T` is not a unique log
position.

Three rules make the log evidence:

- **write-ahead per poll, under the fence.** Observe → **re-check the anchor
  fence** → append the line and fsync → *then* update progress or emit
  completion. Audit cannot see an observation that changed progress before
  it was durable. An append after leadership was lost is evidence written by
  a non-leader. The fence otherwise lives in the journal writer, so
  `AdapterContext` carries one for the FW adapter. PR-03 replaces the anchor
  between an observation and its append.
- **a seal barrier.** At §6 step 2, before T is chosen, the engine parks
  every FW task at a poll boundary. It awaits any poll in flight and
  forbids further C1 appends. Otherwise a second qualifying poll could land
  after the snapshot, its completion would never be admitted because the
  engine exits, and audit would derive a completed watch where the seal
  carries a live one.
- **a torn final line truncates**, exactly as the WAL's does.

The adapter's **first durable record on dispatch is a `start` line**,
`{artifact_format_version, kind: "start", at, run_id}`, written before its
first poll. So a dispatched watch always has `watch_seq ≥ 1`. A watch not
yet dispatched is a `pending_spawn`, not an `fw_watch`. The run directory is
made, and its parent fsynced, before the `start` line. A crash between the
two leaves a directory with no `start` line, and resume dispatches the watch
again under its bound `run_id`.

`next_poll_at` is then exactly:

- **after `start` and no poll line, `start.at`** (the first poll is
  immediate);
- **after a poll line, `poll.at + interval`**.

PR-34 asserts those two timestamps directly, not through the helper that
computes them. The first poll is not derived from the STARTING row's
`status_at`. For a SPAWN pending on a passive host, that time precedes the
actual dispatch by hours.

**The `start` line is also FW's resume evidence.** §11's ladder has the
rule: a pending FW SPAWN whose run directory holds a `start` line with the
effect's `run_id` is **resolved as applied by that line**. The watch is
rebuilt exactly once from the log, and no second `start` is ever appended.
Without this rule, the ladder would treat a pending SPAWN with a run
directory as an applied-SPAWN candidate. It would look for `spawn.json`,
find none, and launch the watch again, from its directory or as an untraced
start. That gives two `start` lines, an undefined fold, and a seal nothing
can reproduce. The window is real: the decision commits, the adapter
appends `start`, and the engine dies before `effect_result{applied}`.

Its sibling: a completing poll is appended, and the engine dies before the
STATUS input is durable. Resume then finds a log whose last line is a
completing observation, and a row still RUNNING. The ladder injects the
completion from the log, exactly as it injects a CMD's from `status.json`
(PR-34a).

C2's lines append after `watch_seq`. `spawn.json` and `status.json` are
immutable by construction. `watch.jsonl` is immutable by being append-only.

That settles what audit reproduces from what, and §11 states it. `state`
reproduces from the opening seal and the period's **inputs**. `executions`
and `outbox_pending` reproduce from inputs **plus spool evidence**:
`spawn.json`, `status.json` and `watch.jsonl`. `dispatch` records carry no
`run_id`; the spool does.

`start_period` on the row lets run history keep a run that spans a boundary
under the catalog it started in (PR-50).

**Dry-run loading validates the join, one way, over dispatchable rows
only.**

- Every `executions` entry has a RUNNING or STARTING **CMD or FW** row.
- A RUNNING or STARTING row **may** lack an entry only when reconciliation
  proves there is no intent, no spool evidence and no live process behind
  it. A `CHANGE_STATUS STARTING` overwrite produces exactly that: frozen
  parity lets it rewrite the row without launching anything, a test pins
  "stays STARTING forever, no live task", and such a row is safe to carry.
  A two-way join would refuse a legal estate (PR-22a).
- Every `outbox_pending` SPAWN has a `pending_spawn` counterpart.
- The `reservations` on a row agree with its entry's `run_number`.

A RUNNING **box** has no adapter, no effect and no entry. Boxes are
deliberately outside `dispatchable`, and the loader must not reject an
estate for having one live.

Which rows are dispatchable is a question about C2 (DL-151), and the seal
artifact carries no catalog. So the sidecar's own validation refuses only
what the artifact can refute: a missing row, a row that is not live, a run
number that disagrees. The **resume path** asks the rest, over the seal it
is about to open from, before the successor's segment is written. An entry
that names a box, or a job the opening catalog does not define, refuses
there. A check with no caller would let a forged sidecar that names a live
BOX row pass every gate on the way in.

## 4. `baseline_id` rotates per period

This is load-bearing. With one baseline across a continuous log, the
following would happen. A client reads job revision 7 under C1, baseline B.
C2 opens under B. The change does not touch that row. The client submits
`(baseline=B, expect=7)`, and it is accepted against C2 semantics. So every
transition **derives** a fresh `baseline_id` (§3.4).

`control-protocol.md`'s wire shape is the same. Its **definition** of
`baseline_id` is "the period's identity", not "the log's identity". A
superseded `baseline_id` is refused, naming the current one. The check
refuses a stale *period*, not a stale *log*.

## 5. Capacity, decomposed

Capacity in use is two facts: units **held by rows** and units
**permanently spent**. DL-50: a depletable drains. Replenishing one is
`update_resource`, a definition-time mutation of the SEM-16 class, and a
non-goal here. A depletable's spent units are in no row. A seal that
recomputed usage from holders alone would refill every depletable. So the
facts are separate:

```python
class CapacityReservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    bucket: str
    units: int                            # > 0
    release_policy: Literal["completion", "success", "never"]
```

- `JobRuntime.reservations: tuple[CapacityReservation, ...] = ()`
- `JobRuntime.waiter_seq: int | None = None`
- `RuntimeState.consumed: dict[str, int]`
- `RuntimeState.enqueue_counter: int`
- `CapacityPool` is a pure function of (catalog, rows, consumed).

`consumed` values are `>= 0`. A self-consistent seal with
`consumed["r:FUEL"] = -3` would open with invented capacity. The loader
refuses a negative value and a reservation with zero or negative units
(PR-22).

Placement follows ownership: a reservation belongs to the job's row. It is
acquired at a start transition, by the run that start begins. It is settled
at the edge that leaves STARTING or RUNNING. While the run is live, the
reservation belongs to that `(job, run_number)`. Held units (below) outlive
the run. So on a row that is not live they belong to the job, not to its
current `run_number`; an ON_NOEXEC bypass moves the run number and leaves
them.

Both fields are part of the row's optimistic-lock projection. They change
at the moments `status` changes, with two exceptions that move only the
reservations: `RELEASE_RESOURCE`, and the opening release of a removed
job's units (below). Each is an input that touches that row, so it moves
that row's revision once, and nothing else.

`RuntimeState` enforces these rules:

- a row that is not STARTING or RUNNING keeps only **held** units (below);
- `waiter_seq` is non-null exactly when the row is QUE_WAIT;
- the edge that leaves STARTING or RUNNING frees what the policy frees,
  keeps a renewable resource's other units on the row as held, and moves a
  depletable's units into `consumed` atomically;
- a start may not overwrite non-empty `reservations`, except a start of a
  job that holds units, which replaces them;
- the acquired vector is frozen at acquisition;
- `enqueue_counter ≥` every non-null `waiter_seq`.

**Held units** (DL-256). A renewable resource's units that a run's FREE
policy does not free stay on the job's row after the run. That is FREE=N
always, and FREE=Y, or an omitted FREE under `renewable-free=Y`, after
FAILURE or TERMINATED. They are owed to the job, not spent, because the
operator can give them back. `RELEASE_RESOURCE` clears them. The job's next
start re-uses them: its admission credits them to it, and its new vector
replaces them. A held reservation is an `r:` bucket whose policy is
`success` or `never`. A machine load and a `completion` reservation never
outlive the run.

The seal carries held units on the row like any other reservation. The
loader accepts them on a row that is not live under that rule. The opening
installs them verbatim, so they cross a boundary until released.
`CapacityPool.used` counts them, and so does §10.3's oversubscription
check. §10 reads the resources a row holds, live or held, as dependencies
of the job beside its declared ones. A FORCE_STARTJOB can re-use units a job
no longer declares, and a held unit's fate at the run's end reads the
resource's type. A change to a held resource is R for an executing holder,
and A (with its own sentence) for an armed or holding one.

A carried row whose job the opening catalog no longer defines (deleted or
renamed) cannot be addressed. So no `RELEASE_RESOURCE` reaches its units. If
it holds units while not live, the period's first input gives them back at
the opening instant. It records `RELEASE_RESOURCE` with the reason and wakes
the waiters in DL-50's order. A fresh opening admits a time observation for
it, so the waiters do not wait for an unrelated input. Replay applies the
same input. A live ghost is refused at the boundary (§10.1), so it never
reaches an opening.

`sorted_waiters` gives a waiter absent from the catalog the unset priority.
It sorts behind every declared priority, then by enqueue order and name. §10
classifies as R a QUE_WAIT job that the next catalog removes (PR-40), so the
boundary refuses that state first. The default is the floor under that gate:
a classifier bug misorders the queue and does not crash admission.

## 6. The cutoff barrier

`Scheduler._next` cannot be re-derived at a boundary. Resume re-anchors
**inclusive** of the scheduler frontier and dedups against the ticks the
journal holds, and a seal cuts that evidence away. An anchor exclusive of
the cutoff would lose an unconsumed tick. An anchor inclusive of it, with
nothing else to dedup against, would fire a consumed tick twice. So the
anchor is always inclusive (DL-166), and the sweep carries both dedup
sources: the ticks this segment journaled, and the cutoff the seal records.

"Journaled" covers admitted and dropped ticks alike. `scheduler_frontier`
counts a `drop` record too (PR-25a), so a tick this segment already dropped
is journaled evidence exactly as an admitted one is. A first dedup source
that read only the admitted ticks would re-derive a drop-set frontier and
drop the same tick again on every later resume of the segment (DL-174).

1. The **operator** holds the runbook's set with `ON_HOLD`: every scheduled
   top-level job or box with a future tick (`deployment-runbook.md` §6 step
   1). The barrier places no holds of its own (below). The set is not "the
   §10 R-closure", which is a verdict on executing work, not a hold set.
2. Stop admitting **every** externally requested attempt, rejected and no-op
   ones included, since each takes a durable decision. Drain every
   attempt already admitted, **except the active seal request itself**, to
   its durable decision before any sidecar byte is written. The seal
   request's decision *is* the `seal` record (§2.2) and cannot precede the
   sidecar. An attempt admitted after the cut would have its decision land
   after the seal, or not at all.
3. Choose the cutoff instant **T**.
4. Admit every scheduler tick due at or before T.
5. Advance the oracle through T, firing every due semantic timer.
6. Drain the resulting synchronous inputs and effects.
7. Re-check §8. If steps 4–5 started work despite the holds, **refuse**.
   An input stamped after T that lands during the barrier also refuses the
   boundary, with `seal_input_after_cutoff` (DL-133). The input belongs
   after T, and T is not chosen again. The refusal comes before any
   sidecar byte: C1 reopens and admits the late input, and the operator
   retries the seal.
8. Write the sidecar, then append the `seal` record at T.
9. Open the next segment with `first_index = closes_at_index + 1`, `at = T`,
   and its scheduler strictly after T. "Strictly after" is a guarantee, not
   a mechanism. The resume anchor is INCLUSIVE of T, because an exclusive
   one loses a same-instant sibling that the crash left unjournaled (DL-45).
   The missed-tick sweep skips every re-derived tick that the cutoff already
   admitted. C2 fires or drops no tick at or before T (DL-166).

The engine runs steps 2 to 8. Step 1 is the operator's.

**There are no boundary holds.** The code has one hold bit, `on_hold`, and
`ON_HOLD`/`OFF_HOLD` set it. A tick arms only because it is set. There is no
second, separate "boundary hold". An abort that "removed the boundary's
holds while keeping the operator's" could not tell the two apart. So the barrier **never touches `on_hold`**. Step 1's holds are the
operator's. They are placed before the seal, as `deployment-runbook.md` §6
instructs, carried across the boundary as placed, and released by the
operator's `OFF_HOLD` in C2. That is the "precise C2 event" that releases an
armed latch (PR-26). The barrier freezes *admission* (an engine flag, not a
row field). It holds no job. So an abort restores nothing on any row, and a
successful seal carries every `on_hold` exactly as the operator left it
(PR-28c).

The only carried evidence is `scheduler_admitted_through: T`. **C1 owns
every tick ≤ T, and C2 owns every tick > T.** A schedule new in C2 cannot
fire at T.

**The scheduler's durable frontier is semantic, not "the newest timestamp
in the file."** `last_journal_at` takes the maximum `at` over every record,
`leader` and `dispatch` included. Consider: T is 02:00. C2 opens at 02:10
and appends `leader.at = 02:10`. The process dies before the missed-tick
sweep. The next resume would anchor at 02:10, and a 02:05 tick would be
neither admitted nor recorded as dropped. The watermark must not take its
value from `last_journal_at`. The frontier is `max(opening watermark,
admitted scheduler ticks, drop records, advance records)` and **nothing
else** (PR-25a).

## 7. The seal operation

This section says who performs a seal.

**`dsl41 seal`** is one command with two entry modes and one body:

```
dsl41 seal --run-root <root> [--estate-anchor <dir>] \
           --next <estate files>... [-p site.properties] [--next-timezone …] \
           [--force-seal] [--claimed-actor …] [--request-id …]
```

`--estate-anchor` defaults to `<run-root>.anchor`, a sibling of the root. A
rolled root's anchor is the lineage's and must be named. `--next` is
repeatable, and its order is part of the bundle hash. `--claimed-actor`
defaults to `<user>@<host>`. `--request-id` reuses an earlier attempt's id
to retry it exactly. The lock decides the mode, not a flag:

- **Live mode**: a leading engine is running on `<root>`.

  The CLI **stages** C2 first. It does so in two steps, because two sibling
  destinations cannot be renamed into place at once. First it materializes
  the immutable bundle under `catalogs/<source_bundle_hash>/` by the
  liturgy. The bundle is content-addressed, so a repeat is idempotent and a
  concurrent client writing the same bytes is harmless. Then it writes only
  `staged_manifest.json` and `candidate.json` under
  `periods/.staging/<stage_digest>/`. **`stage_digest`** is sha256 over the
  canonical `StagedNextPeriod`: the staged fields alone, never the five the
  engine derives (§3.4). The request carries it beside, not instead of, the
  request's own `fingerprint` over the whole envelope (§2.2). The two are
  distinct: they differ whenever `force_seal` or the actor differs.

  Then the CLI speaks to the engine over the control socket with a `seal`
  verb. It is a v3 mutating verb. It names an `expect` on nothing, because
  it is a boundary, not a row mutation. It carries `request_id` like every
  command.

  The engine validates **exactly the staged bytes the fingerprint names**.
  It performs §6 steps 2–8 in its single-writer loop. Inside step 8, before
  the `seal` record, it **writes the committed `manifest.json` into the
  staged directory by the liturgy**: temp, `fsync(file)`, `rename`,
  `fsync(staged_dir)`. That manifest holds the staged fields plus the ones
  the engine derives (§2.1). The engine runs phase 2 against both in-memory
  models and those bytes. Only then does it **atomically rename the staged
  directory to `periods/N+1/` and fsync both `periods/.staging/` and
  `periods/`**. So the artifacts the boundary names are the ones it
  validated, and they are durable before the record that names them.
  `staged_manifest.json` is kept beside `manifest.json`. The directory
  fsyncs are the ones this spec demands of every other artifact. Without
  them, a power loss after the committed seal could lose `periods/N+1/` and
  leave a seal that names a manifest that does not exist (PR-30g). The
  engine writes `manifest.json`. A CLI-written one renamed unchanged would
  leave the committed fields nowhere.

  A crash before the committed-manifest write leaves the staged directory as
  a candidate that the retry validates again. A crash after it and before
  the rename does the same, and the retry overwrites the engine-written file
  with its own (PR-30f).

  The staged directory carries a **`candidate.json`**:
  `{artifact_format_version, stage_digest, next_period}`. It exists because
  the rename to `periods/N+1/` drops the digest from the path. A byte
  comparison of staged manifests cannot stand in for it: the candidate
  identity also binds the request's staged fields one by one, and a later
  retry must be told *which* field differed.

  Suppose the engine dies **after the rename and before the `seal` record**.
  The request is unseen (§2.2), and `periods/N+1/` already holds a
  candidate. Then:

  - A retry whose `stage_digest` equals `candidate.json`'s reuses the
    **staged identity**: the bundle, `staged_manifest.json` and
    `candidate.json`. It **regenerates `manifest.json` from its own
    cutoff**, in place, by the liturgy: temp in `periods/N+1/`,
    `fsync(file)`, `rename` over the old, `fsync(periods/N+1/)`, all before
    the sidecar. This is needed because `first_index` is output of the
    attempt. The first attempt closed at 100 and wrote 101; C1 resumed and
    admitted 101; the retry's truth is 102. The fresh path's four fsyncs do
    not apply to a directory already in place, so the reuse path has its own
    (PR-30d, PR-30g).
  - A retry with a **different** `stage_digest` moves the installed
    candidate to
    `periods/.quarantine/<old stage_digest>/<sha256 of its manifest.json>/`
    and installs its own. That path cannot collide when candidates alternate
    S1 → S2 → S1, and it is idempotent when the same bytes are quarantined
    twice. So a stale candidate is never silently selected, and a
    `periods/N+1/` that exists is never blindly reused (PR-30d).

  Two CLI clients racing on one root stage under two fingerprints, and the
  engine commits exactly the one its request names. Under a manifest path
  that was not content-addressed, a second client could overwrite it between
  validation and commit and leave a committed boundary that could not open.

  The engine then **exits with code 3** ("sealed; period N+1 is ready to
  open"). Step 9 is `dsl41 run --resume` on the same root, which opens from
  the seal (§11). The engine does not load C2 into itself: a transition is a
  restart, not a reload (DL-65). The sealer has C2 in hand for the readiness
  gate; that is not the engine adopting it.
- **Offline mode**: no engine is running. `seal` acquires `leader.lock`
  **and `anchor.lock`**: it will append, and the anchor fence applies to
  every appender. It appends a `leader` record at epoch+1. It runs the
  same-root recovery barrier in full: replay, reconcile, and **re-drive
  recorded kills**. Then it performs §6 steps 2–8 as that offline leader,
  **staging, validating and installing C2 exactly as live mode does**,
  `candidate.json` included, because two install paths would be two places
  for the same crash window. Then it exits. It may not replay rows, observe
  "terminal" and seal.

**Exit codes.** The `seal` command exits:

- 0 when the boundary committed, in either mode;
- 2 when it did **not** commit. The period is still open, and C1 may have
  advanced first, legitimately: an offline sealer's `leader` record and
  reconciliation decisions, and a live cutoff's admitted ticks, are C1
  activity, not damage. A live `seal_in_flight` refusal that names another
  boundary also exits 2: this request did nothing, and that boundary may
  still commit;
- 4 when the answer was `unknown`, or when a `seal_in_flight` refusal names
  this request's own `request_id` (its boundary is still running). It prints
  the `request_id`, exactly as `sendevent` does.

The live *engine* exits 3 ("sealed; period N+1 is ready to open"), distinct
from its 0/1/2, so an init system does not restart a sealed engine in a loop
(PR-30b).

In both modes the sealer holds **both** catalogs: C1 to run the barrier, and
C2 for the readiness gate (§8). Both modes then hand off to one of two
openers:

- `dsl41 run --resume --run-root <root> --estate-anchor <dir>`: **in place**;
- `dsl41 run --open-from <anchor-dir> --run-root <new-root>`: **a physical
  roll**. It reads the lineage head and requires it `closed`. It requires
  the closing period to be fully quiescent (§8: no live executions at all)
  **and attested** (§1.3: `audit.json` present in `closing_root` and
  passing `verify`). It opens `next_period` into a fresh root that satisfies
  §1.1's ownership rule. A read-only preflight checks this before the CLI
  creates the new root or takes its lock. `--estate-anchor`, if given, must
  equal `--open-from`. A target that is the root the lineage just closed in
  is refused: that is `--resume`.

The physical roll is the only way to roll. `run` without `--resume` is a new
genesis, and so a different estate.

The in-place opener takes the successor claim (§1.3) as its first act after
`leader.lock` and `anchor.lock`. The **physical roll's** order is
`new-root leader.lock → sentinel durable → anchor.lock and claim → import →
segment → open`: the sentinel comes **before** the claim. With the claim
first, B could move the head to `claimed(B)`, die before its sentinel, and
leave a root that an old binary treats as unused and runs a genesis in.
After a `reclaim`, that is a fork. No state may exist in which the head is
`claimed(target_root)` while `target_root` lacks a valid sentinel (PR-01a).

Live-mode exit is **code 3, without touching detached work**. An
engine-loop return is otherwise failure code 1, and detached-stop is
otherwise set before ordinary teardown. So this exit path is its own
obligation (PR-30b).

The seal runs three pure functions, each over its own inputs. Each runs at a
moment when a different subset of the facts exists. The first is not "the
second minus one check". The second's checks need a seal to parse, a
digest, record-against-sidecar agreement and `T`, and none of these exists
at readiness.

The first two phases take a **typed context** that names every fact they
read, and they read nothing else. "Pure" means exactly that. A signature
that names two parameters for a function that reads seven things invites an
implementation built on filesystem lookups and engine globals, which passes
every functional case and races.

- `StagedContext {staged, staged_bytes, boundary_request,
  request_fingerprint, c1: closing catalog + profile, c2: CatalogIR,
  carried_state, decision_index: read view, state_machine_version, at}`.
- `BoundaryContext` = `{staged: StagedContext, committed,
  committed_manifest, at, post_barrier_state}`. Here `at` is T, spelled as
  the field is.

The candidate sidecar and the candidate record are **not** in the context.
Phase 2 splits at the sidecar, because the sidecar's `classification` field
IS phase 2's output, and the classifier has to run before there is a
sidecar to put it in (below). Phase 3 takes no context type. It takes the
sidecar, the digest the naming record carries, and the opening period's
committed manifest. The `RuntimeProfile` is **inside** that manifest and is
read from there, not passed beside it. `OpenedRuntime` carries the catalog
identity the period opens under, and never a profile of its own.

Three rules govern the engine that an opener then assembles:

- A setting the wiring CAN express, and that disagrees with the pin,
  **refuses the resume** and names the fields that moved. A runtime-profile
  change is a new period (§2.1).
- A setting the wiring cannot express takes the pin as its default. The
  reconciliation and grace windows have no wire flag. An opener that
  assembled with ambient CLI defaults instead would pass every functional
  case (PR-22b).
- `as_machine` and `machine_policy` are neither (DL-151). They change what
  the runner ANSWERS TO, and no wired component reports them: they act in
  preflight, over the catalog. An opener that DECLARES them is held to the
  pin like any setting the wiring can express. An opener that declares
  nothing inherits the pin, because it said nothing to be held to.
  Inheriting without condition would let a boundary that staged a new
  machine identity open silently under the old one: the process would still
  answer to the names it was started with, while the manifest pinned
  others.

**The runtime gate runs before the successor's segment is written.** A
refusal at the end of the ladder would refuse the process but leave period
N+1 open on the disk. The next attempt with the same wrong wiring would then
meet an ordinary open period and succeed. A boundary refuses while it is
still a boundary.

**Phase 1 — `validate_staged(StagedContext)`**, at readiness, before the
barrier. Inputs: the context above; no seal, no T. Checks:

- the candidate parses under a supported `artifact_format_version`;
- `catalog_hash` v2 and `source_bundle_hash` match the staged bytes;
- `RuntimeProfile` constructs and hashes to the staged `runtime_hash`;
- `state_machine_version` equals the current one;
- preflight passes;
- the request's `request_id` is absent from the current period's
  `DecisionIndex` under another fingerprint;
- the **classifier** (§10) runs over the carried state's live closure, and
  the R gate passes.

Nothing here touches a seal.

**Phase 2**, after §6 step 6 and before the `seal` record, over
**in-memory** candidates. It is **two functions**, because its checks
straddle the sidecar. `validate_boundary(BoundaryContext)` runs first and
returns the map. `check_candidate(BoundaryContext, sidecar, record)` runs
over the document built from that map. The checks, across the two:

- the committed form's `first_index == closes_at_index + 1`;
- every field duplicated between the candidate record and the sidecar
  agrees;
- the **classifier runs again** over the post-barrier live closure, **and
  its output is the committed classification**. The barrier's own
  admissions and any reconciliation injections may have created executions
  or latent intent that phase 1 never saw. An offline seal's recovery
  barrier can reconcile a FAILURE that leaves a `pending_spawn` for a job C2
  changes. The sidecar's `classification` field equals phase 2's result
  byte for byte, never phase 1's. An implementation that re-ran only enough
  to reject R, and committed the stale phase-1 map, would carry a seal whose
  A assumptions omit a latent case the barrier created, and would fail audit
  (PR-28a);
- `now == scheduler_admitted_through == T`;
- the full **load** below succeeds against the in-memory sidecar.

A failure here refuses the commit. C1 has advanced and is still open,
**and `abort_boundary` runs**. It clears the sealing flag, reopens control
admission, restarts scheduler admission and unparks FW tasks. It touches no
row, because the barrier held no job (§6). "Refuses, C1 still open" is not
enough on its own: a literal implementation would return exit 2 with the
engine frozen behind §6 step 2. After an abort, a command, a tick and an FW
poll all proceed (PR-28b).

**The reversible interval runs from §6 step 2's freeze to the instant
before the `seal` append begins, and it is exception-safe.** Every exit
inside it that does not commit and is not named below runs `abort_boundary`
while the fence is still valid. Such exits include a phase-2 refusal, a
failure to write or fsync the committed manifest, a rename or
directory-fsync failure, a sidecar write failure, and any other unexpected
exception. A fence loss inside the interval **fail-stops** and does not
reopen admission, on DL-101's rule. An abort that ran only on validation
failure would leave a live engine frozen, for good, behind a freeze after
an `ENOSPC` on the sidecar.

**Three more kinds of exception fail-stop** (DL-274):

- An exception while an attempt admitted during the seal is not fully
  applied. That attempt is a drained one or the cutoff's own time
  observation. The window opens when the attempt takes its index. It closes
  when its admission line, decision, outbox entries and answer are all
  done. Inside it, the engine's memory may disagree with the WAL.
- A WAL append that fails anywhere in the interval, such as an effect
  outcome that dispatch writes. The failed line may be torn or whole. An
  abort would reopen C1 over either state, and the next record would land
  behind it.

  For these two, no abort runs. The seal request is not answered, and
  neither is an attempt whose answer was still owed. Recovery repairs the
  WAL tail and rebuilds from the WAL, and the period stays open. An attempt
  whose decision is missing is applied through the gate (DL-156). Resume
  then writes the missing decision, with the effects the verdict implies,
  before it seeds the ghost-run gate (§11 step 6a, DL-315). A
  start recovered this way is a pending SPAWN: dispatch applies it, or a
  drained or quarantined host holds it.
- A `clock_regressed` before the index on an engine-made input, such as an
  adapter completion, a tick or a routing observation. That input has no
  one to answer, and a refusal would lose it while C1 reopens. So the seal
  stops the engine, and resume observes the input again. A request that
  hits `clock_regressed` is answered refused with that code, and the seal
  refuses. The cutoff's own time observation belongs to the seal, so the
  seal refuses on it too.

**The transition-violation stop also stops without an abort** (DL-292,
DL-295). Under the run option `--on-transition-violation stop`, an input
committed inside the interval that records a violation stops the engine
after its decision is durable. Such an input is a drained attempt, a cutoff
tick or the cutoff's own time observation. That
stop is the operator's chosen halt. An abort would reopen C1 and carry on.
The engine exits 5 (`concurrency-model.md` §4).

Every other exception still aborts and refuses. Among them are the drain's
own timeout, and a failure in settling with no unfinished append.

**The `seal` append is the point of no return. A failure there is an
unknown outcome, not an abort.** The writer flushes the whole line before
`fsync`. An `fsync` error does not prove the line absent or not durable, and
a partial append may have left a torn final line. That case must not abort
and reopen C1. C1 would then append commands, ticks and completions
**after** a seal line that might survive a crash; that is records after a
seal, which recovery rightly refuses. Or it would append after a torn line,
turning recoverable final-line damage into interior corruption. So once any
seal bytes may have been written, the engine **fail-stops** and reports the
outcome unknown (exit 4, `request_id` printed). Recovery decides:

- a complete matching seal line → **`fsync` the WAL first**. Only a
  successful `fsync` promotes it to committed. A complete line in the page
  cache proves visibility, not durability. A recovery that closed the anchor
  and opened C2 on a line that then never reached the disk would leave
  durable successors that depend on a seal that vanished. If the `fsync`
  fails, stay stopped;
- an absent or torn final line → truncate durably and reopen C1;
- a seal line with records after it, or interior corruption → refuse
  (PR-28d).

**Phase 3 — `open_from_seal(sidecar, expected_digest, manifest) ->
OpenedRuntime`**, at resume and at the tail of phase 2.

- `sidecar` is the durable artifact at resume, and the in-memory candidate
  in phase 2.
- `expected_digest` is the digest that the naming record carries: the
  committed `seal` record at resume, or the opening `segment`'s
  `opens_from_seal` in a rolled root.
- `manifest` is the opening period's committed manifest. Every field it
  shares with `next_period` must agree.

Both facts are required. An opening that skipped either would seed an
engine from a self-consistent sidecar that is not the one the lineage
names, or under a manifest that is not this boundary's.

It returns an **`OpenedRuntime`**: the carried `state`, the outbox, the
executions, the classification and the opening identity. It does **not**
return an `Engine`. `Engine.__init__` takes a clock and adapters, calls
`clock.now()` and seeds the host row, and a pure function may do none of
these. The half derived from the catalog (referencers, the capacity pool,
the scheduler frontier and genuinely new rows) belongs to the impure loader
that holds C2 and builds the engine from this. A function that touched no
clock could not return an `Engine` without reaching past its context. The
load:

1. parse through the versioned seal schema; refuse an unknown
   `artifact_format_version`;
2. at resume only: verify the sidecar's recomputed digest against the digest
   in the naming record, and every duplicated field (§11 step 3);
3. install carried rows **without applying mutations or bumping revisions**.
   This is `Oracle.__init__`'s genesis input, seeded from the seal for rows
   that have one. A naive "construct C2 then overwrite" would seed carried
   entities first and move revisions;
4. seed only genuinely new rows (SEM-24 flags, declared globals);
5. leave the ghost-run gate `_dispatched` to resume. Resume rebuilds it from
   every row with `run_number > 0` after the segment's replay (§3.3), the
   carried rows included. Referencers, capacity, the scheduler and adapter
   routing belong to the loader too, because they derive from C2 and not
   from the seal;
6. validate:
   - timer tokens are unique, positive and ≤ `timer_seq`. Two equal
     `(due, token)` entries would force the heap to compare two `Event`
     objects that have no order;
   - `waiter_seq` values are unique and positive;
   - the reservation and status invariants hold (§5), every reservation
     has `units > 0`, and every `consumed` value is `>= 0`;
   - the join of `executions`, `outbox_pending` and rows holds (§3.5);
   - `first_index`, `epoch` and `run_number` are within I2's bounds;
   - `now == scheduler_admitted_through == T`;
   - every route's `executor_id` names a host row;
7. touch **no** adapter, supervisor, socket, clock or filesystem.

Each of phase 3 step 6's checks is an injected failure in PR-22. Each
phase-1 check is one in PR-28. Each phase-2 check is one in PR-28a.

## 8. Preconditions

A seal **refuses**, and does not proceed, when any check fails.

**Readiness — before the current period closes.** It is the same in live and
offline mode:

- C2 is loaded from exactly the staged bytes that `stage_digest` names;
- `catalog_hash` v2 and `source_bundle_hash` are computed and bound in the
  candidate manifest;
- `RuntimeProfile` is constructed and hashed;
- `next_period.state_machine_version == seal.state_machine_version` (§2.1),
  and `next_period.artifact_format_version` is one this binary implements;
- the catalog is preflight-valid;
- it is classified against the carried state (§10) and accepted by the R
  gate;
- the seal request's `request_id` is absent from the current period's
  `DecisionIndex` under any other fingerprint (§2.2);
- the staged directory is fsynced with its `candidate.json`;
- **phase 1** `validate_staged` succeeds over its `StagedContext` (§7).

A failure here refuses while C1 is still open and correct.

**Then, after the cutoff and before the record**, **phase 2** succeeds:
`validate_boundary(BoundaryContext)` for the classifier half, and
`check_candidate` over the sidecar and record built from its output. A
failure there also refuses. The cutoff work already admitted stays as
legitimate C1 activity (§7 exit codes).

**Always:**

- every admitted attempt has a decision, and no request is awaiting a
  response, **except the seal request itself**, whose decision is the `seal`
  record (§2.2);
- the engine input queue is **empty**, including scheduler events, adapter
  completions, reconciliation injections and time observations;
- no `RuntimeState` transaction is open, and no admission, application or
  effect delivery is in progress. "Delivery in progress" **includes** a
  KILL whose TERM/grace/KILL ladder has not resolved to a spool proof, an
  applied CMD SPAWN whose adapter task has not yet written `spawn.json`,
  **and** an FW poll between its observation and its durable line. The
  sealer waits all three out. It parks FW tasks at a poll boundary before T
  is chosen (§3.5). It never snapshots a half-run ladder, an unbound spawn
  or a half-recorded poll. Each wait is bounded by a fixed 30 seconds
  (`QUIESCE_WAIT_S`), not derived from the profile. When a wait runs out,
  the seal refuses with `seal_not_settling` or `seal_not_quiescent`, before
  any sidecar byte, and C1 carries on. So a KILL ladder longer than that
  (a `cmd_grace_us` above about 30 seconds) is refused, not waited out; the
  operator retries after the ladder ends (DL-312);
- scheduler admission is frozen. Control admission is **permanently closed**
  once the seal commits: the old period admits nothing after its boundary;
- `outbox_pending` and `executions` account for every intent and every live
  run that the reconciliation sweep can find. The sweep (journal
  dispatches, spool directories, supervisor `LIST`) finds nothing they do
  not;
- no indeterminate KILL exists whose target might still exist;
- every supervisor **that owns unresolved execution evidence** is reachable.
  That is one with a carried non-terminal execution, a pending or
  indeterminate effect, or a spool candidate the sweep could not close. An
  unreachable one of those makes quiescence **unprovable**, and the seal
  refuses. A host that was evicted, whose held work was re-driven or
  retired, and whose spool candidates are all resolved owns nothing a seal
  needs. Requiring *its* supervisor would let a permanently dead machine
  block every future seal (PR-27a).

**A reachable supervisor with an empty `LIST` is not proof.** `LIST` shows
what *this incarnation* spawned. A restarted supervisor has a new
incarnation and an empty history. Proof needs the `LIST` from the
incarnation whose lease the sealer holds, reconciliation against every
carried non-terminal row, the spool per candidate, `boot_id` and (pid,
start-time), and a refusal for any candidate left unresolved.

**Mode:**

| transition | drain required |
| --- | --- |
| in place, detached | none beyond the §10 R-closure |
| in place, tethered | full — stopping the engine cancels live commands; tethered is the CLI default |
| physical roll, every execution terminal, closing period attested | permitted — the closing root's supervisor `LIST` is empty from the sealer's own incarnation and the spool sweep finds nothing live |
| physical roll while jobs are live | **refused** — the supervisor is one per run root and a new-root engine cannot reach the old root's work; the bridge that lifts this is a non-goal (§12) |

## 9. Retries across a boundary

`parse_envelope` rejects a foreign `baseline_id` **before** it consults the
decision index, and `baseline_id` is in the fingerprint. So a retry composed
under C1 cannot be answered after C2 opens. With §4 that is a **liveness**
loss, not a safety failure: the retry is refused, naming the current
baseline, and never mis-applied.

- **Hard** (§8): nothing is admitted without a decision, and nothing is
  awaiting a response. The condition that makes this argument hold: the old
  period admits nothing after its seal.
- **Soft**: the closing manifest's `retry_horizon_us`, measured from the
  last admitted externally requested **attempt with a durable decision**.
  That is applied, rejected, or an applied no-op, `host` commands included.
  Below the horizon the seal warns and requires `--force-seal`. The force is
  recorded as `force_seal: true` in the sidecar's `boundary_request` and on
  the `seal` record, with the gate's output in `forced_gate`. So the log
  alone shows a forced boundary. Below the horizon without the flag, the
  seal is refused with code `seal_retry_horizon`.

Naming a horizon weakens `control-protocol.md` §3's unbounded exact-retry
promise. It is a contract change with no wire change (DL-123). Clients are
told that retries expire.

## 10. Classification

### 10.1 Three tiers, not one

"R when live and changed" is not the whole rule. With `armed` defined as
live, and R beating A, every A case for an `armed` job would be unreachable.
The tiers are:

| tier | a job is here when | changed closure ⇒ |
| --- | --- | --- |
| **executing** | RUNNING or STARTING; a `pending_spawn`, `bound` or `fw_watch` execution; a member of an executing box | **R** |
| **latent intent** | `armed`; QUE_WAIT; a non-stale authoritative timer | **A**, naming the assumption — except **removed ⇒ R** |
| **not live** | otherwise | **carry**, listed as changed in the report |

**`pending_spawn` is executing, not latent.** The oracle reaches RUNNING
before the shell plans the SPAWN, so a pending SPAWN's row is already
RUNNING and the sets overlap. The effect carries no frozen command:
`_apply_spawn` reads the **current** catalog's `JobIR` at dispatch. If it
were classified A, this would happen. C1 starts `j` on a passive host, and
the SPAWN stays pending. C2 changes `j.command`. C2 opens and the host is
activated. The C1 effect executes **C2's command under C1's run number and
reservations**. The choices are R, or freezing the whole dispatch spec into
the effect. R is the smaller change and the honest one (PR-39a).

**Removed ∧ executing ⇒ R** as well. A removed job is not in
`dispatchable`, so a KILL for it plans no effect, and `KILLJOB` would stop
nothing. Removed ∧ not live ⇒ ghost, kept and listed. L001 refuses a
*condition* that names it.

**Precedence: R beats A** only where an executing rule and a named A rule
both fire: a running holder of a resource whose capacity C2 lowers.

### 10.2 The classification graph

Neither the per-job fingerprint nor IR-G computes the blast radius. IR-G's
edges are producer→consumer over job and global nodes only. No resource,
machine, calendar or timezone node exists there. This graph is built for
the purpose, with IR-G as one **reversed** input.

Nodes and what moves them:

| node | changed when |
| --- | --- |
| job | its `JobIR` fingerprint moves (`period.job_fingerprints`, the leaf test; DL-131, §15) |
| box containment | `box_name` on any member moves, at any nesting depth |
| global | declared default moves; added; removed |
| external instance (`name^INST`) | the `insert_xinst` declaration moves |
| resource | `amount`, `res_type`, or a release-policy default moves |
| machine | `max_load`, `type`, `node_name`, or membership moves — the fields resolution actually reads |
| calendar / cycle | a referenced date set moves |
| timezone basis | `default_tz` or `tz_aliases` contents move |
| runtime profile, **per field** | See the list below |

The runtime-profile fields reach jobs as follows:

- `default_tz` and `tz_aliases` reach every job with `start_times`,
  `start_mins`, a calendar or a `run_window`. `run_window` is included
  because the oracle reads it, and absolute must times (which need
  `start_times`), in the base zone (DL-253).
- `as_machine`, `machine_policy`, `execution_mode`, `deadman_us`,
  `cmd_grace_us`, `reconcile_settle_us` and `spawn_window_us` reach every
  CMD job.
- `fw_default_interval_us` reaches every FW job.
- **`retry_horizon_us` reaches no job.** It is boundary policy. A field that
  reached every job would turn a change to the horizon into a full drain of
  live work.
- **`semantics` is per switch.** There is one node per semantic switch,
  valued at its effective value. The jobs that the switch's registry entry
  names reach it (DL-252). A switch may instead name calendars: the calendar
  depends on the switch, and its jobs reach it through the calendar edge.
  Examples from the registry:
  - for `ice-lookback`, every job with a lookback-qualified atom in its
    `condition`, `box_success` or `box_failure`;
  - for `renewable-free`, every job with a renewable resource request that
    states no FREE (DL-256);
  - for `wekr-first-week`, every extended calendar with a WEKR token
    (DL-259);
  - for `dst-start-times`, every job with `start_times` or `start_mins`,
    whatever its zone (DL-260).

  A flip can change a run in flight: a running box whose `box_success`
  reads such an atom completes under one reading and not the other. So
  those jobs classify as changed. A running job that a flip reaches is
  refused (R), and a latent one carries the recorded assumption (A). Each
  side's condition truth in the boundary-truth diff is read under that
  side's switches.

Edges run **from a job to what it depends on**. Every one of them, the
profile fields included:

- its condition's job, global and `name^INST` atoms. They are walked
  directly off `JobIR.iter_conditions()` (condition, `box_success`,
  `box_failure`), never off IR-G's edge list. IR-G diverts a local
  unqualified `n()` into `mutex_groups` (M07) and keeps no edge for it. IR-G
  stays the box-topology input (DL-131);
- its box, and a box to each member (both directions, nested);
- its `resources:` entries;
- its `machine:` and that machine's members;
- its calendars and cycles;
- the timezone basis, for every job with `start_times`, `start_mins`, a
  calendar or a `run_window` (DL-253);
- **each runtime-profile field** that the list above names for its kind;
- each semantic switch whose registry entry names the job;
- and, from a calendar, each semantic switch whose registry entry names the
  calendar.

So a live CMD's forward closure reaches `cmd_grace_us`. A C2 that changes
only the grace cannot commit over the CMD and then kill the C1 run with
C2's ladder. The profile edges run in the same direction as every other
edge, job → field. An implementation with reversed edges reached no profile
field from any job and still passed every listed obligation (PR-37a).
`retry_horizon_us` has no incoming edge from any job.

**Two questions, two directions.** The R gate asks *"is anything live job J
depends on changed?"*: J's **forward** closure. The boundary-truth diff
asks *"whose readiness flips because X changed?"*: X's **reverse** closure,
then condition truth under C1 and C2 at the carried state. Both are
computed. Neither substitutes for the other.

### 10.3 Named cases

- QUE_WAIT and removed in C2 → R (PR-40)
- live FW whose watch parameters changed → R (an FW run is in-engine)
- box membership changed while the box is executing → R
- member changed while its box is executing, member INACTIVE → **R**. A would
  let the member start under C2 inside the box's C1 execution. E19 closes here.
- `armed` with changed schedule or condition → A: "the C1 trigger survives under
  C2 gating"
- a resource C2 lowers below carried `consumed + held` → A: "admission refuses
  until releases catch up"; R if a holder is executing
- `initial_status` changed while the carried row disagrees → A; genesis seeding
  applies to **new rows only**

### 10.4 Armed latches cross a release

Armed latches survive a seal, deliberately. They die with the run root, not
with a seal (`deployment-runbook.md` §6). Dropping one at the boundary would
be an implicit transition with no admitted input. If a latch is unwanted,
the honest alternative is an explicit journaled disarm **before** the seal.
Obligation: one tick under C1 while held → **exactly one** start after C2
opens (PR-26).

That disarm is the control plane's `DISARM` job verb (`control-protocol.md`
§3, DL-158). It clears the latch and does nothing else. An unarmed target is
an accepted, journaled no-op. It is legal at any time, not only before a
seal: the pre-seal timing above is when it changes what C2 does, not when it
is admissible. It drops only the latch visible at application time.
`applied` does not inhibit a later arm, and it cancels no start already out
of the latch: a QUE_WAIT attempt stays queued, and a deferred run-window
start still fires.

Across the boundary, an old-baseline `DISARM` is refused exactly as every
stale-baseline command is. A newly composed C2 command may drop a carried C1
latch. The WAL shows who did (the input's source and actor), and the trace
shows which: `sendevent DISARM` for a drop, `sendevent DISARM (no latch)`
for a no-op (DL-233). So PR-26 reads: one held tick under C1 → exactly one
start after C2, or none if an admitted `DISARM` dropped the latch in
between.

## 11. Resume, replay and recovery

**Resume** starts from the latest committed seal. In period 1, before any
seal exists, it starts from the genesis segment; otherwise an estate that
crashed before its first seal would have no path back.

1. `flock` `leader.lock`, before any side effect. Read the sentinel, and
   refuse unless it is a `period_root` record that names this estate.
   §1.1's ownership rule applies to resume as it does to creation.
2. `flock` `anchor.lock` and read `anchor.json`. Refuse on an `estate_id`
   mismatch. Then refuse a root the anchor does not name, or one that does
   not own the period of its newest opened segment (§1.3's resume rule,
   DL-224). This runs before any repair below. No torn tail is cut, no
   segment that never opened is removed, and no seal is selected before it.
3. **Select the seal by lineage, from what this root holds.**
   - If no successor segment exists yet, the newest **committed** `seal`
     record in this root's last segment names the seal.
   - Otherwise the active segment's `opens_from_seal` names the sidecar this
     period opened from. In a rolled root, that is the imported one.
   - If neither exists, this is period 1 before any seal, and replay starts
     at genesis.

   In every case, verify the sidecar's recomputed digest against the digest
   the naming record carries, and verify every duplicated field. A rolled
   root never holds the predecessor's WAL or `seal` record, so the local
   `segment` is the proof there. A sidecar newer than the last committed
   record is an orphan and is never selected.
4. Act on the head:
   - `open(N, this root)` with N's `seal` record present → do the
     `open → closed` CAS that the crashed sealer did not (§1.3);
   - `closed` and no following `segment` →
     `claim_successor(estate_id, seal.digest, seal.next_period, target_root)`;
   - `claimed` with our `claim_id` and our first segment **already
     durable** → do `claimed → open` (the crash was between segment and
     head);
   - `claimed` with our `claim_id` and no segment → resume the claim;
   - `claimed` with another `claim_id` → refuse, naming the holder;
   - `open(1, this root)` with `segment_durable: false` and a durable
     segment → finalize.
5. `open_from_seal` (§7 phase 3) over that seal, under the digest the naming
   record carries and this period's committed manifest. With no seal in the
   lineage, `Oracle(catalog)` genesis from segment 1.
6. Replay the segments after the seal in order, each in its own period
   context.

   **6a. Commit the recovered verdict.** For each attempt that replay
   found with no `decision` and applied through the gate (DL-156), write
   the missing `decision` record, with the effects that `plan_effects`
   plans for it and a SPAWN's `run_id` minted in that record
   (DL-315). The plan
   reads each job's run number just before the attempt as dispatched, and
   just after it as current, and holds no live run. The decision lands in
   the active segment, after this incarnation's `leader` record; readers
   pair a decision with its attempt by `index`, never by position. The
   verdict is not recorded in the decision index a second time, and
   `--on-transition-violation stop` does not run for it. A second resume
   finds the attempt decided and commits nothing.

   Every recovered attempt is planned, and the identity preflight of the
   reconciliation ladder runs against the WAL's SPAWNs and the planned
   ones, before any of these decisions is written. A resume refused there
   writes no decision and mints no id; the only record it has appended is
   its `leader` record, as any refused resume does.

   A run root that an earlier build resumed may hold a watch it relaunched
   with no identity: a `watch.jsonl` start line with `run_id: null` for a
   start whose decision was missing. At this build's first resume of such
   a root, while that run directory is present, the plan mints an id for
   the run and resume refuses the root by name as a WAL/spool identity
   split (DL-118). The refusal fires whether the watch completed or not.
   It writes no decision, so the build that wrote the log can still open
   it. Such roots are reset, not migrated, under the pre-production
   reset clause (`protocol-evolution.md` §5, DL-138).
7. Run the reconciliation ladder (`runner-design.md` §7). It includes the
   re-drive of a live wrapper under a terminal row, whatever its KILL
   effect's recorded state (PR-33).
8. Dispatch.

**Replay across periods** (`dsl41 journal`, `dsl41 audit`) walks segments
and switches catalogs at each `segment` record (DL-142). The crossing is the
opening above, not a second path. State folds through the seal by
`open_from_seal`, exactly as an engine's does. The next period's catalog is
loaded from the content-addressed bundle that the opening `segment` pins
(§1.1): like for like by hash, never a catalog the reader was handed.

A boundary is crossed only over a seal that proves out: the digest the
naming record carries, the chain from the `seal` record that closes the
predecessor, and `next_period` agreement with the opening segment. An
unprovable seal is refused by name. Refuse-don't-degrade is not weaker on a
diagnosis surface. A read-only replay across a forged seal narrates a forged
continuation with exactly the confidence of a true one.

The seal itself must be **re-derived, not merely self-consistent**, before
the crossing. Rewrite a sidecar canonically, recompute its digest, and copy
that digest into the closing `seal` record and the successor's opening:
every binding above still agrees, because all four were forged together.
Two proofs close that gap, chosen by what the read is holding:

- When the predecessor's evidence IS being replayed (the ordinary lineage
  walk, in place or across a roll), the predecessor seal is **re-derived
  from the period's own WAL, spool and manifests, in the root that holds
  them**. A stored sidecar that is not what they produce is refused, naming
  the fields. That re-derivation also compares the `seal` RECORD with the
  sidecar field by field (§2.2), so a rewritten record over an honest
  sidecar is refused there too.
- When a later segment is named **alone**, the predecessor's inputs are not
  read, and nothing re-derives anything. The argument that lets a replay
  cross without an attestation does not hold. So the predecessor's
  **attestation is required**, and its absence is a refusal that names it.

The cost is real: an unpruned lineage replays each period twice, once to
re-derive its seal and once to narrate it.

**"Verified" means re-derived, not self-consistent, and it has two named
tiers (DL-144).** A sidecar whose digest matches its own canonical form
proves integrity, not derivation. An estate can stand behind a closed
period in exactly two ways. They are not the same strength, and they are
**spelled differently everywhere**: a reader given one word for both could
not tell which periods the estate can still re-derive.

- **derivation-verified**: the period's own inputs are present, and `audit`
  reproduced the seal from them. This is the tier defined field by field
  below. It is the only tier at which a checkpoint may be *produced*.
- **attestation-verified**: **seal-only**. The period's inputs were archived
  under §12's retention class. What stands for the period is its
  attestation, accepted by PR-02e's **consumer** rule and by nothing else:
  the checkpoint's own digest, its binding to the seal it names, and its
  `chain_through_period`. It is **not** a recursive walk; the induction was
  established when the checkpoint was produced. It is not a weaker reading
  of the rule below. It is the other rule, the one a rolled root uses for
  an imported seal. A period at this tier can never return to the first
  tier: the archive cannot be undone (§12), so restoring the files does not
  restore the claim.

Every reader reports the tier by name. Nothing reports a shorter answer
silently.

A seal is *derivation-verified* when `audit` has reproduced **every
digest-covered field** of it, field by field, **except the scalars of
`boundary_request`** (`claimed_actor`, `force_seal` and `request_id`). Those
are authoritative boundary *input*, from a request that no WAL record holds
on its own. Audit checks them for exact equality between the sidecar and
the `seal` record, and carries them. `source` rides on both and in the
request fingerprint.

**`source` is `request` on every boundary (DL-138).** It has one legal value,
so there is nothing to derive. Audit checks that the record and the sidecar
agree on it, and refuses a disagreement (PR-47b). There is no `adopt` value,
no `catalog_hash_v1` on the period-1 `segment` and no `adopted_from` on the
sentinel; they belonged to the retired estate-adoption path. So the sentinel
is not an audit input.

Everything else is re-derived. `request_fingerprint` comes from the
envelope. `forced_gate` comes from the pinned horizon, the WAL and T. The
state, executions, outbox, classification and every lineage field
(`baseline_id` included) come from exactly four things:

- the opening seal;
- the complete ordered WAL of the period: inputs, `advance`, `host`,
  `drop`, `decision`, `effect_result` and `leader`;
- the immutable spool evidence: `spawn.json`, `status.json` and
  `watch.jsonl`;
- the C1 and C2 manifests.

`outbox_pending` needs `decision` and `effect_result`. Pending, applied,
indeterminate and retired are the WAL's distinction, and the spool does not
encode it. The scheduler frontier needs `drop`.

**Period ownership of spool evidence comes from the WAL, not from a
timestamp.** A `status.json` makes a C1 execution terminal only when C1's
WAL holds the matching admitted completion input. Without that input, the
file is evidence about an execution live at T, whatever its `ended_at`
says. A completion at `ended_at == T` by clock resolution is exactly the
case a timestamp rule gets wrong. A `watch.jsonl` line past `watch_seq` is
C2's by position. A CMD live at T that completes in C2 audits as live in C1
(PR-47c).

"Full" is not `state` and `executions`. It is the whole document. The proof
is durable: `seals/<period_id>.audit.json`, written by the liturgy. It
carries `{artifact_format_version: 1, seal_digest, period_id,
chain_through_period, prev_attestation_digest, state_machine_version,
dsl41_version, audited_at, scope: "full", digest}`. It is canonicalized by
§3.2, with `digest` over the canonical bytes minus only the top-level
`digest`. Its own golden vector pins it (PR-08b), so a producer and a
consumer on two patch versions agree byte for byte. It is bound to the seal
it attests and to the interpreter that produced it.

Resume from a seal with no attestation, whose period inputs are corrupt or
pruned, is **refused by default**. `--trust-unaudited-seal` would override
it, recorded in the opening `segment`'s `trust_unaudited` field with the
claimed actor. Availability is sometimes worth more than proof; that is the
operator's call, made in writing. **The switch is specified and not built**
(DL-133: it is resume's switch, not an estate verb's). The `segment` field
exists, and every opener writes it null, so the artifact does not move when
the switch is built. Until then there is no override, and PR-47's third
clause is not discharged.

**Auditing an old period runs the interpreter that produced it.** `audit`
refuses a period whose `state_machine_version` it does not implement. It
names the version and the `dsl41_version` that the attestation or `leader`
record names. The operator installs that version and audits with it; the
runbook's venv-per-version upgrade pattern serves exactly this.
Cross-version audit inside one binary is a non-goal. The release discipline
this implies is keeping old versions installable (PR-Q4, §16).

**Recovery matrix.** Every row is a crash-injection obligation (PR-45):

| situation | behaviour |
| --- | --- |
| sidecar present, `seal` record absent | orphan; period still open |
| `seal` record present, anchor head still `open` | committed; resume performs the `open → closed` CAS, then proceeds as the next row |
| `seal` record present, head `closed`, no following `segment` | claim the successor and open `next_period` |
| physical roll: crash after import, before the first `segment` in the new root | the import is idempotent by content address; re-import, then open |
| physical roll: closing period has no `audit.json` | refuse: attest first |
| head `claimed(claim_id, root)`, crash before the first `segment` record | the same `(seal, next_period, root)` recomputes the same `claim_id`, resumes it, opens; a different one refuses naming the holder |
| head `claimed`, first `segment` durable, crash before head moved to `open` | resume finds the segment, moves the head to `open`, continues |
| crash in period 1 before any seal | replay from the genesis segment |
| anchor directory deleted or replaced under a live incumbent | the incumbent stops on its next append/dispatch (`anchor.lock` re-check) |
| a logged input raises when this build replays it | refuse, naming the input's index, kind, source, `at` and `request_id` (DL-292). A logged effect outcome the outbox refuses (such as a second outcome for one effect, or one for an effect it never saw) stops resume the same way, naming the effect instead of an input (DL-295). Every resume of the period refuses the same way until a build that replays the input runs: a fix, or the release that wrote the log. Nothing skips the input |
| torn final line in the active segment | truncate to the last complete record |
| torn or empty **first** line of a new segment | the segment never opened; the file is removed and re-opened from the boundary, which is byte-identical (PR-07). The repair needs an **earlier segment in this root** to re-open from: `select_seal` falls back to the previous segment and reads the `seal` record there. A root holding exactly that one segment — a rolled root, or a fully archived one — **refuses** instead, naming the missing segment record. That is a refusal and not damage. Lifting it means teaching seal selection to open from the anchor head, which is a unit of its own (DL-144) |
| corrupt line inside a **closed** segment | that period is unauditable; later periods resume from a **verified** seal only, else refused (above) |
| a closed period's WAL absent, its **archive receipt** present and licensing it | archived (§12): the period stands at the attestation-verified tier; `audit` verifies the checkpoint, `journal` narrates an unreplayable gap and crosses on that checkpoint, `runs` names the missing coverage, `estate prune` re-plans the root |
| a closed period's WAL absent, **no receipt** | **loss, not an archive**: refused by name at the walk, at the replay and at the plan. The receipt is written before any deletion so that the two can never be confused |
| an archive receipt present and its attestation or sidecar absent | refuse: the three are one permanent floor, and a period with neither inputs nor proof is loss |
| seal digest mismatch | refuse |
| committed `seal`, sidecar **missing** | refuse; the boundary is unrecoverable |
| sidecar self-consistent but ≠ record's digest | refuse |
| `prev_seal_digest` chain broken | refuse |
| catalog directory missing | refuse naming the hash |
| catalog directory partial or un-fsynced | refuse on `sources.json` mismatch |
| two candidate active segments | impossible by I1 once `segment_no == period_id`; a second file for one period is refused as foreign |
| `segment` pins ≠ preceding seal's `next_period` | refuse |
| any record after a `seal` in the same segment | refuse |
| legacy `header` journal, no `segment` | **refused** — a retired dialect, named with DL-138 (below) |
| resume of a root the anchor does not name, or whose newest period's row names another root (DL-224) | **refused** by §1.3's resume rule, before any row above repairs anything. A refusal by this rule may create or take `leader.lock` and `anchor.lock`, may tighten the root and the anchor directory to `0700`, and may fsync the directories those imply; it creates, changes or removes nothing else |

**Refusal precedence at resume (DL-224).** The foreign-estate refusal (step
2) comes first. §1.3's resume rule comes next, before every row of the
matrix above that repairs or acts. A root that the anchor names, and that
owns its newest period, reaches the same refusals as without the rule, in
the same words. A historical registered root after a roll still meets the
successor claim's refusal. A claimed head held by another root still names
the holder.

`dsl41 run --resume`, and the offline `dsl41 seal` (which resumes the root
it seals), also check before they stage anything or wire a supervisor. That
first read takes no anchor lock. So its refusal is confirmed under both
locks before the command refuses: exit 2 tells the units never to restart,
and a stale snapshot must not cause one. The first read reports only this
rule's refusal. Any other error, a missing anchor, and another estate's
anchor pass it, so a root that passes the rule meets them in their usual
order and words. The confirmation swallows nothing. It runs `resume_run`'s
own steps, and a busy anchor lock, a missing, corrupt or foreign anchor, or
this rule is the command's refusal, in its own words. So a root that fails
this rule when the command starts is refused with no supervisor started and
nothing staged, even when its message is another engine's hold on the
anchor. If the anchor changes between that first read and the locks,
admission is still refused under both locks, and what the command staged or
wired before it may remain. `resume_run` repeats the rule under both locks
in every case.

**There is no legacy adoption (DL-138).** No `dsl41 estate adopt` verb
fences a run root written before this model, translates its `header`
journal into `wal/000001.jsonl`, or seals period 1 in one step. The path has no
producer and no estate to consume. There is likewise no `adopting` head state, no
`adopt` seal source, no `catalog_hash_v1`, no `adopted_from` on the sentinel
and no `legacy_batch: true` fold.

What stands in its place is a refusal, not a repair. Each of these is
refused **by name**, citing DL-138, by the owner that meets it: a
`journal.jsonl` that opens with a `header`, a `catalog_hash_version` of 1, a
`result` or standalone `effect` record, a `manifest/manifest.json` layout,
and an on-disk head state of `adopting`. So a run root written before the
period model is neither adoptable nor readable. There is no supported path
from one into a lineage.

`docs/protocol-evolution.md` is the contract for this retirement: the
compatibility matrix per protocol, the lifecycle by which a new dialect
enters service, the absence gate a retirement normally has to meet, and the
pre-production reset clause that let this one meet it trivially.

**Subscribers** (`control-protocol.md` §5, v3): a client that asks for an
index below the earliest retained record receives an explicit gap marker.
The seam between backfill and live is the same. `decision` replaces
`result`+`effect` in the stream.

## 11a. SPAWN idempotency that outlives the supervisor

An in-memory `self.runs` lookup cannot be the supervisor's SPAWN dedup. An
estate root may live unrolled for the life of the estate. So `LIST` must be
bounded, and completed entries must leave memory. The moment they leave, a
delayed duplicate SPAWN would become a fresh execution. `self.runs` is the
bounded `LIST` window and is **not** the idempotency store. The store is the
directory below. This is the tombstone protocol (DL-129).
`supervisor-protocol.md` §3 and §5 are its other home.

The tombstone is the run directory, made crash-safe by two extra files and
one ownership rule. The **supervisor** creates a detached run's directory on
receipt. The engine does not create it before it sends SPAWN. The engine
owns the directory only for tethered runs. Otherwise the engine could create
the directory and die before sending. The retry would reach the supervisor,
"directory exists, no receipt" would read as indeterminate, and a run that
provably never reached the supervisor would be lost.

1. `mkdir runs/<job>.<run_number>`. The directory can exist already in one
   case only: the orphan that the last row of the table below cleared for
   reuse, because the replay resolution runs first.
2. Write the **`run_id` index** entry by the liturgy:
   `runs/.by_run_id/<run_id>`, holding
   `{artifact_format_version, run_id, job, run_number}`.

   **Index before receipt.** The frozen idempotency key is `run_id`, and
   every later lookup goes through the index. So the first durable thing
   that names the `run_id` must be the index. With the receipt first, a
   crash between the two would leave a receipt that nothing could find. A
   retry of the same `run_id` against another `(job, run_number)` would see
   no index and no directory at its own path, and would spawn again. With
   the index first, a crash after `mkdir` and before the index has made
   nothing durable that names the run, and the retry's own path is the first
   application: one process. A crash after the index and before the receipt
   resolves through the index to a directory with no receipt: indeterminate,
   no process.

   `run_id` is constrained to a **filename-safe grammar** at the wire: the
   canonical uuid4 string form that the adapter mints,
   `^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`.
   Any other form is refused, at the wire and again when the index is read.

   **Ownership is one-to-one in both directions.** One `run_id` maps to one
   `(job, run_number)`, and one `(job, run_number)` maps to one `run_id`.
   With `run_id` minted in the effect (§2.3), that holds by construction on
   the engine side. On the supervisor side, a directory that already carries
   a receipt or an index for a *different* `run_id` is a **collision**. It
   is refused, never reused, and never given a second index.
3. Write `receipt.json` by the liturgy, **before** the wrapper is spawned:
   `{artifact_format_version, run_id, spec_fingerprint, received_at}`.
   `spec_fingerprint` covers the frozen wrapper input spec
   (`supervisor-protocol.md` §2) in three steps:
   - remove `lifeline_fd`. The fd is ours to fill, so a retry that carries
     one would fingerprint differently from the receipt we wrote;
   - replace every float, at any depth, with `"float:" + float.hex()`.
     §3.2's grammar has no floats, and the frozen spec carries one in
     `grace_seconds`. So the exact bits go in, tagged so that no plausible
     string field can collide with them;
   - take sha256 of the §3.2 canonical form of the result. It is hashed over
     the whole body, not through the `digest` helper, which strips a
     top-level `digest` key by design.
4. Spawn the wrapper. The wrapper writes `spawn.json` (frozen, unchanged).
5. Write `reply.json` by the liturgy: `{artifact_format_version, run_id,
   wrapper_pid, spawned_at}`, the answer as first given.
6. Answer the engine.

A replayed SPAWN resolves the directory **through the index** and answers
from the directory, not from memory. The incoming path is read in one case
only: no index entry (DL-150, DL-151).

| directory state | answer |
| --- | --- |
| index entry, `receipt.json` with an equal `spec_fingerprint`, `reply.json` present | duplicate: the **original result fields** from `reply.json` inside the frozen duplicate envelope — `{ok, run_id, wrapper_pid, spawned_at, "duplicate": true}` (`supervisor-protocol.md` §5) — no process |
| equal fingerprint, `spawn.json` present, no `reply.json` | duplicate: result fields rebuilt from `spawn.json` (`wrapper_pid`, `spawned_at := started_at`) in the same envelope — equivalent, and the protocol says so rather than promising bytes it did not keep |
| directory at the incoming path holds a receipt or index for a **different** `run_id` | collision: refused, never reused |
| equal fingerprint, no `spawn.json`, wrapper alive | in progress: no second spawn |
| equal fingerprint, no `spawn.json`, nothing alive | **indeterminate** — the crash landed between receipt and spawn; nothing may re-spawn; the engine's E7 policy decides the run |
| `receipt.json` with a different fingerprint | collision: refused |
| index entry → directory with no `receipt.json` | crash between index and receipt: indeterminate, same rule |
| index entry names a directory that does not exist | impossible by write order (`mkdir` precedes the index); treated as indeterminate if ever seen |
| index entry unreadable, or naming a `run_id` that is not its own filename | **indeterminate**. Corruption is not absence: "no index entry" AUTHORIZES a spawn, so an entry that cannot be read must never answer as one that is not there |
| no index entry, and a `receipt.json` at the incoming path names **this** `run_id` | answered from that directory, by the rows above — the index was lost under a live receipt, and losing an index never authorizes a second process (DL-151) |
| no index entry, and **another** `run_id`'s index names this `(job, run_number)` | collision: the same crash under a different id, refused. The index is scanned for that owner here and only here — after a crash, never on a healthy spawn |
| no index entry, no receipt, and the directory holds a `spawn.json` or a `status.json` | **indeterminate** — a run whose directory the engine owned, so no receipt was ever written. It holds that run's evidence, and forking into it would overwrite it |
| nothing else | first application — an orphan directory at the incoming path with no index and no receipt is a crash between `mkdir` and index and is reused, because nothing durable names its run |

Writing the receipt *before* the spawn is the safe direction. Its failure
mode is a run that never happened being reported unknown, which E7 handles.
Writing it after would let a crash between spawn and receipt make the retry
spawn twice, which nothing handles. `LIST` may then evict completed runs
freely, because it was never the idempotency store.

**The store itself has a retention floor, and it is a safety rule, not
housekeeping.** "No index entry" means "first application", so deleting an
index entry or a run directory *authorizes a spawn*. A run directory and its
`.by_run_id` entry may not be pruned while the SPAWN effect that names them
can still be replayed. That lasts until the period that holds the effect is
attested and its executions are terminal. Any compaction must keep the
`run_id ↔ (job, run_number)` mapping and the original reply. Likewise, the
spool evidence that an unattested period's audit needs (`spawn.json`,
`status.json`, `watch.jsonl`) and a live or carried execution's spool cannot
be pruned. §12 states the retention rules these floors sit under.

Every row above is a crash point in PR-36. So are: the engine crashing after
`mkdir` and before SPAWN (tethered, the only case where the engine owns the
directory), the same `run_id` presented against a different
`(job, run_number)`, and a fingerprint collision.

## 12. Non-goals

- a physical roll while jobs are live (the multi-root execution bridge);
- a `state_machine_version` change across a transition (§2.1). This is not
  an extension point: a semantics change is a full drain and a new-estate
  genesis (`docs/protocol-evolution.md` §1, DL-138);
- the shared store. It is orthogonal; if built, it replaces the anchor
  (§1.3);
- mid-run catalog reload (DL-65);
- automatic sealing on a timer;
- cross-node resource coordination (DL-49);
- a retention or compaction **policy**, but not its floors. Which periods,
  spools and tombstones may be pruned, and when, is a business decision
  (`deployment-runbook.md` §2).

  **There are three verdicts, because there is a middle.** An artifact is
  **floored** (reachable from the head; refused), **held** (the head has
  moved past it and no class licenses removing it, so it stays, and the
  verdict names the dependency in the way) or **prunable** (licensed by
  name).

  What may **never** be pruned is stated here. It is *everything reachable
  from the lineage head*:
  - the sentinel;
  - the anchor and any active claim;
  - the seal sidecar the current period opened from, and the one it will
    close with;
  - the current and committed-next period manifests;
  - **every installed but uncommitted candidate's `staged_manifest.json` and
    `candidate.json`, until its seal commits, or until it is quarantined and
    no recovery references it**. Recovery after install-before-seal is
    decided by those two files;
  - their catalog bundles and `sources.json`;
  - the latest attestation chain checkpoint and every attestation after it;
  - the WAL and spool of any unattested period;
  - the spool of any live or carried execution;
  - any SPAWN tombstone whose effect can still be replayed (§11a).

  Recovery refuses without a sidecar or a catalog directory. A retention
  rule that could delete them while obeying the tombstone floor could
  delete the only artifacts able to open the head (PR-36c).

  The DL-146 perimeter journal (`perimeter.jsonl`, `docs/access-model.md`
  §6, DL-147) sits outside this floor. No replay reads it, and nothing in
  the lineage reaches it. Its one physical rule: it is pruned only with its
  whole root, never truncated in place. `access_seq` is recovered from its
  tail, and a truncation would forge duplicate keys.

### 12a. The archive — PR-Q3's answer (DL-144)

**Yes, conditionally, by explicit policy.** A seal-only archive may stand in
for pruned inputs. This is a decision, not a deduction: the text above
allows either answer, and "never" is rejected as policy rather than argued
away. An archived period drops to §11's **attestation-verified** tier and
stays there.

**The receipt is the point of no return.** `seals/<period_id>.archive.json`
is a §3.2-family artifact: `artifact_format_version`, canonical
serialization, and `digest` over the canonical bytes with only the
top-level `digest` removed. It carries `{estate_id, period_id, seal_digest,
attestation_digest, chain_through_period, retention_class, archived,
archived_at, dsl41_version}`.

`archived` is the **exact licensed artifact list**. It is relative to the
run root, sorted, without repeats, and of exactly one of **two shapes**: the
period's segment alone, or its segment together with its committed
candidate's two files. The all-or-nothing rule lives in the artifact, not
only in the verb that writes it. Any other list describes a state this class
never produces, and a reader that weighed such a receipt would report a tier
for a period that is half-archived.

The receipt is written **durably before the first deletion** of the period
it licenses. It is the **recovery authority**: a later plan lists what is
left to delete FROM the receipt, never from what the disk happens to look
like. A crash between the receipt and the deletions leaves an estate that
says what was licensed to go, and a re-plan completes it from the receipt
alone. A crash **before** the receipt leaves an estate where nothing
happened.

**Missing evidence with no receipt is LOSS and refuses.**
Refuse-don't-degrade: accidental loss must never read as archiving.
Ownership decides which absence is even a question. There are two tests of
it, because there are two kinds of reader:

- A reader that holds the **anchor** (the estate walk, the planner) reads
  §1.3's registry row. A period whose row names *this* root owes this root
  its segment.
- A reader that holds only a **root** (`dsl41 journal ROOT`,
  `dsl41 audit --run-root`) reads `periods/<N>/manifest.json`. Genesis and
  every in-place opening install it in the root that runs the period. A
  physical roll never imports it for a predecessor.

Both answer the same question, and neither guesses. A rolled root lawfully
holds a predecessor's sidecar and attestation and none of that period's
WAL, and both tests say so.

**A receipt is PROVED before any reader acts on it, through one shared
door.** There are seven bindings:

1. the receipt's own canonical bytes, digest, class and filename;
2. its `estate_id` against this root's **sentinel**;
3. the sidecar's **own** `estate_id` and `period_id`. Reading a sidecar
   parses it and never asks whose it is. Without this binding, a foreign
   seal-and-attestation pair with the receipt restamped onto it would
   satisfy every other check;
4. its `seal_digest` against that sidecar;
5. the **attestation**, by PR-02e's consumer rule (`verify_attestation`
   exactly, not a recursive walk);
6. its `attestation_digest` and `chain_through_period` against that
   checkpoint;
7. where a specific absent file is being excused, that the list names
   **that** path.

Every consumer applies all seven: the estate walk, the retention plan and
its live re-check, the tier, `audit` and its re-derivation, `journal` and
`runs`. Readers that bound a receipt differently would make the estate's
answer depend on which verb an operator typed. A function that is safe only
behind one of its callers is not safe. A receipt that is present and does
not prove out is never treated as absent: it refuses.

**Three artifacts per archived period join the floor PERMANENTLY.** No class
may ever prune them: the **receipt**, the period's **attestation**, and its
**seal sidecar**. Without the receipt, the archive reads as loss. Without
either of the other two, the period has neither inputs nor proof.

**Eligibility is itemized per artifact dependency, not "everything the head
has moved past".** The class is named **`archive-inputs`** and is selected
**per period**. DL-135's default stands: no class named, nothing deleted.
Exactly two kinds are in the class. The act is **all-or-nothing per
period**, so "archived" is one state that a reader can report a tier from:

| artifact | in the class when |
| --- | --- |
| `wal/<period>.jsonl` | the period is **attested** *in this root*; a **later** chain checkpoint covers it *anywhere in the estate*; every run **born** in it has had its directory, `.by_run_id` entry and default logs pruned already; every older period this root retains is archived, or is archived ahead of it by this same sweep, oldest first; and it is below the estate's **head** period |
| a **committed** candidate's `staged_manifest.json` + `candidate.json` | that period's WAL is in the class — the same cover, in the same receipt |

The cover is an **estate** fact, and the period's own attestation is a
**root** fact. The difference is the roll: a rolled root's last period is
covered by a checkpoint that the SUCCESSOR root holds, so a cover per root
would floor that period forever. §1.3's registry row says **where to look
and which seal the lineage committed**. Both halves are load-bearing. The
path alone would let an edited row fetch a cover from somewhere else. The
row's `seal_digest` makes a branch *this* lineage's, not merely *a* branch.

Four bindings stand between a row and a cover:

- a `period_root` sentinel of this estate;
- a sidecar that is this estate's and that period's;
- that sidecar's digest equal to the digest **the row committed**;
- `verify_attestation` binding the checkpoint to that sidecar and to its own
  `chain_through_period`.

**Disagreement refuses; absence only fails to prove.** A present root
REFUSES the plan when its sentinel is missing or names another estate, when
its sidecar attests another estate or another period, or when its sidecar
digests to something other than the digest the row committed. Otherwise an
edited row could point at a stranger's root, or at a same-estate root that
holds a second valid pair for that period, and release WAL that belongs to
the branch that actually ran. Missing proof is the other case, and it is
**skipped**: a root that is off-line, a row with no `seal_digest`, an absent
checkpoint, a sidecar that will not parse, an attestation that does not
verify. None of them says anything false. Each supplies no cover, and the
walk keeps looking further down. Skipping only ever holds more. The live
re-check before the receipt reads the anchor again, not a snapshot the plan
carried, because the window it exists to close is exactly a row moved in
between.

Ruled **out** in this class, and stated so that a later version knows what
it changes: **content-addressed bundles** (they are shared by reference, and
deciding reachability across an archived period is a race this class does
not take), and the period **manifest** (a later period's opening folds
against it). Anything whose attestation or checkpoint cover is absent stays
floored or held.

**Two ordering rules, each a real dependency:**

1. **The spool goes first** (PR-36b's order). The tombstone floor resolves a
   run directory to a period *through the SPAWN effect in that period's
   WAL*. If the WAL were archived first, every tombstone it explains would
   become of unknown provenance and floored forever, a floor nothing can
   lift. So archiving a period's WAL **refuses** while any run directory or
   index entry of that period survives, and the refusal **names what
   remains**.
2. **Oldest first, and the deletions run in that order too.** The archived
   periods are a **prefix** of what a root retains, so the retained segments
   stay a contiguous suffix at every instant, including inside a crash
   window. That keeps §11's subscriber contract word for word. The gap
   marker is defined at the *oldest retained record*, and the backfill's
   contiguity and adjacency proofs never meet a hole. A deletion that the
   filesystem refuses stops every later period's deletion, for the same
   reason.

**Eligibility is RE-CHECKED against the live disk immediately before the
receipt is written**, independently of the plan: the period's attestation,
the covering checkpoint, the spool, and the prefix. A period whose cover was
questioned in between refuses and is named. Every period **below** it still
goes. Every period **above** it refuses too, naming the one below rather
than repeating its reason. That is the prefix rule. A sweep that stepped over
the refusal would open the hole the rule exists to prevent.

**The archive is IRREVERSIBLE.** Restored files beside a receipt do not
remove the archived state. The receipt governs: every reader reports
*attestation-verified* for that period, whatever is on disk. The restored
inputs may still be **read**; nothing forbids looking at them. But they are
not a claim the estate makes, because the weaker claim was already
published. A tier that changed with the contents of a directory would be no
tier at all.

**Readers name the gap; none answers shorter in silence** (PR-02f's
family):

- the estate walk accepts an archived row and refuses an unreceipted one;
- `audit` verifies the checkpoint and reports the tier **by name**, in
  wording it shares with no derivation-verified line;
- `journal` prints an explicit unreplayable-gap notice **on stdout with the
  trace**. It crosses the next boundary by the attestation-gated route that
  §11 defines for a segment named alone;
- `runs` names the coverage it does not have;
- `estate prune` re-plans an archived root without refusing, including a
  root whose every period is archived.

**The closed book** (DL-230). For a closed period, an investigator or an
auditor is handed:

- the seal that closed it and, for every period but the first, the seal that
  opened it, chained by digest;
- every input between them, unless the period's inputs were archived under
  `archive-inputs`;
- the catalog it ran under, as the post-placeholder JIL bytes stored in the
  period's own root under `catalogs/<source_bundle_hash>/`, content-addressed
  (DL-130);
- the principal who asked for each externally requested input: the
  authenticated one when the access map was armed, and a claim otherwise
  (`docs/access-model.md`).

`dsl41 journal` loads the stored bundle when the caller supplies no estate
files. It gates the bundle against the `catalog_hash` that the period's
`segment` record pins. It parses each file under the path that
`sources.json` recorded, because the hash covers spans and a span names its
file. A bundle that no longer reproduces the pinned hash refuses as
corruption. A supplied catalog that disagrees refuses as a checkout at the
wrong revision. The two messages differ. An archived period whose inputs are
gone contributes nothing to a replay, and `dsl41 runs` and `journal` name
it rather than answering shorter. A segment still on disk under a receipt
(the crash window before the deletions, or a restored file) is read at the
attestation-verified tier, and `journal` names it before it replays it.

## 13. Obligations

Test names follow the house convention `test_prNN_*`, and `test_prNNx_*` for
a suffixed id. The token shape is **`PR-\d{2}[a-z]?`**. The namespace is
`PR-`, not `PM-`, because `P-M\d{2}` is the dossier's mapping-trace pair and
one hyphen is not a namespace. `docs/citation-index.md` holds the row with
that regex. Suffixed ids (`PR-02a`) are citations like any other, and the
gate must resolve them.

Each obligation is written against the question "what would a
plausible-but-wrong implementation still pass?"

**Every row has a STATE: `active` or `retired`.** An active row is a
property that a test holds the code to. Every row below is active unless its
own cell says otherwise. A **retired** row names the decision-log entry that
retired it and the refusal tests that replaced it. It stays in the table:
the citations that point at it must still resolve, and a reader of an older
commit has to be able to find what the obligation was. A retired row is
never deleted, never renumbered and never re-used for a different property.

An active row may name a clause whose producer does not exist: PR-16's remap
half, and PR-47's `--trust-unaudited-seal` half. The row stays active, the
clause is named as not discharged where it appears, and the unit that builds
the producer discharges it. Silence there would read as coverage.

### 13.1 Lineage

| # | obligation |
| --- | --- |
| PR-01 | two roots concurrently claiming one seal: exactly one succeeds; the loser refuses, names the holder, appends and dispatches nothing |
| PR-01b | genesis against an **existing** anchor refuses, even when its incumbent is dead and detached work is alive under it; two roots racing genesis on one anchor: exactly one estate exists afterwards; genesis's own interrupted `open(1, root)` with a matching sentinel and no segment is the sole resume |
| PR-01c | a target root that already holds a `journal.jsonl` — another estate's, this estate's earlier period, a concurrent opener's, **or this estate's own sentinel from an older abandoned claim** — refuses a physical-roll opener; a root holding a **retired `header` journal** refuses naming DL-138, while an unrecognised non-estate root keeps the generic refusal; two estates racing one fresh root: no anchor is ever `claimed(R)` while R's sentinel is not this estate's for this `claim_id`; an in-place opener on its own root proceeds with a sentinel whose `claim_id` is the root's creating claim |
| PR-01a | **native genesis** as a crash matrix: killed after the sentinel, after the anchor, after the manifest, before the first `segment`, **after the segment and before the finalize CAS** — each re-run completes idempotently, `estate_id` is read back not re-minted, and an **old binary launched at every point refuses both `run` and `run --resume`**; the same for a physical roll's new root — with power loss after the sentinel and after the claim, and the assertion that no state has `claimed(target_root)` while the target lacks a valid sentinel |
| PR-02 | the winner crashes with the head `claimed`: a resume from the **same** `(seal, next_period, root)` — a new PID, and the root given as `./r` the first time and `/abs/r` the second — recomputes the `claim_id`, resumes it and opens; a different root still refuses |
| PR-02a | a quiet **physical roll** end to end: `audit` period 1 in A, seal, `run --open-from` into root B, **A is made unavailable**, B is crashed and resumes from its own imported artifacts, `verify` of period 1 passes in B (full `audit` is impossible there and is not asked), and A (restored) refuses to open the same seal |
| PR-02b | the `open → closed` CAS: crash after the `seal` record and before the head moves; resume performs the CAS and the successor claim then proceeds |
| PR-02c | anchor durability under **power loss**, not process kill: with `fsync(dir)` removed from any one head transition the test fails; a successor's registry row appears in the same write as `claimed → open`; period 1's row is provisional (`segment_durable: false`) and flips **in genesis's finalize CAS immediately after its segment** — an implementation that flips it at period 1's close after a running engine, or never, fails; cross-period readers ignore it until it flips |
| PR-02d | `run --open-from` refuses a closing period with no `audit.json`, **and** one whose `audit.json` fails `verify` — a file that merely exists is not enough |
| PR-02f | estate-wide `audit`, `journal`, `runs` and `estate prune` find period 1's root through the registry after native genesis, and still after a physical roll of period 2; ONE walk serves all four, a root that holds two periods is read once, and a provisional row is ignored and named. A registry root that is **missing** or **foreign** refuses BY NAME in every one of the four — that is what proves each verb consumes the walk; sentinel-less, unreadable, short-of-segment, a registry hole, and a run root named where the anchor goes refuse at the walk they all share. The estate-wide readers report the whole estate or stop: `audit` treats a busy lineage lock as one period's outstanding row and audits the rest, `journal` names every segment before it replays any of one, and `estate prune` reports the roots it already swept when a later one refuses |
| PR-02e | **producer-negative**: `audit` of period 2 refuses to emit an attestation over a missing, invalid or mismatched attestation 1. **consumer-positive**: two consecutive physical rolls with both earlier roots unavailable — C `verify`s attestation 2 alone and accepts the chain below it |
| PR-03 | the anchor directory is deleted under a live incumbent: it stops on its next append, its next dispatch, its next revision-bearing read — a `status` immediately after replacement is refused, not answered — **and its next FW `watch.jsonl` append**, with the replacement injected between the observation and the line |
| PR-04 | an NFS anchor path is refused at startup |
| PR-05 | `estate_id` mismatch refuses |
| PR-05c | a staged request cannot choose `period_id`, `segment_no`, `baseline_id` or `clock_domain`: the opened period is `current + 1` with `segment_no == period_id`, a fresh `baseline_id`, and the current clock domain — and a `--next-clock-domain` differing from the current refuses |
| PR-05b | staging → cutoff → opening: a tick admitted at T after the request was staged, then C2's first admission — its index is `closes_at_index + 1`, never a reuse; `stage_digest` is unchanged by `first_index` |
| PR-05a | I2 directly: index, epoch, `segment_no` and per-job `run_number` are monotone across a transition in **both** opening modes; a physical roll that resets the epoch fails |
| PR-06 | `baseline_id` rotates; a command composed under C1 is refused after C2 opens even when the row never moved |
| PR-07 | a `segment` whose pins disagree with the preceding seal's `next_period` is refused; two openings of one seal — in place and fresh root, under two patch versions of dsl41 — produce byte-identical `segment` records, which requires `catalog_hash` v2 to ignore `tool_version` |
| PR-07a | `source_bundle_hash`: `["ab","c"]` ≠ `["a","bc"]`; **reversing command-line order moves it, and both orderings reopen to their own `catalog_hash` from their own `sources.json`**; the same bytes from two original paths are two bundles |
| PR-57 | *(DL-224)* resume refuses a root the anchor does not name: a relocated copy with its **copied** anchor and against the **original** anchor, each with the head `open`, `closed` and `claimed`, refuses naming both paths, and every file, mode and directory entry under the copy and the anchor it named is unchanged but the two lock files; a copy with a **torn tail** and one with an **empty successor segment** refuse before either is repaired; root B's tree restored at registered root A's path refuses by OWNED, naming the period and B; a historical registered root keeps its earlier refusal and words, and a reclaimed roll target refuses by the rule; the same root through a symlink and a `..` detour resumes with the anchor named, and a symlink with the DEFAULT anchor keeps its earlier refusal; both locks can be taken after a refusal; a detached `dsl41 run --resume` on a copy exits 2 and starts no supervisor; a whole lineage restored at another path refuses the resume as well as the estate-wide read; an opening that crashed between its segment and the head CAS resumes through its own claim, and the same opening with its claim reclaimed refuses by OWNED; a misplaced restore whose newest segment is empty, torn or nested too deep to parse refuses by OWNED through the segment before it, exit 2 through the CLI; a case-variant spelling of the recorded root on a case-insensitive filesystem resumes, and on any filesystem two spellings of one directory are the same root while two directories are not; a copy resumed or sealed offline against the original's anchor while another holder has its lock exits 2 and starts no supervisor; a refusal read from a stale anchor is overturned under the locks and the command proceeds, on both CLI routes, while a reclaim between the first read and the locks is refused under them; and a corrupt anchor on a root another engine holds still reports the holder first |

### 13.2 Canonical form

| # | obligation |
| --- | --- |
| PR-08 | the **golden vector**: fixed bytes and digest, covering control chars, `/`, non-ASCII, nulls, defaults, nested payloads empty and non-empty, ordered arrays, six-digit datetimes |
| PR-08b | the **attestation golden vector**: fixed canonical bytes and digest for one `audit.json`, produced under one patch version and verified under another |
| PR-08c | the **runtime-hash golden vector**: one fully populated `RuntimeProfile`, its canonical bytes and hash |
| PR-08d | every artifact refuses an `artifact_format_version` this binary does not implement, naming it |
| PR-08e | golden vectors for `StagedNextPeriod` and `CommittedNextPeriod`, and the `stage_digest`/`fingerprint`/`claim_id` each is computed over |
| PR-08a | the **hash-v2 golden vector**: a `CatalogIR` with `source_files`, a non-null `tool_version`, a non-null `parsed_at` and at least one span — the exact canonical bytes and `catalog_hash` v2, and the same value with `tool_version` and `parsed_at` changed |
| PR-09 | every timer `Event` the oracle can enqueue canonicalizes — enumerated by kind |
| PR-10 | typed-schema `null` vs absent canonicalize identically; opaque-payload `{}` vs `{"x":null}` digest **differently**; array order changes digest |
| PR-10a | an unpaired surrogate arriving as a `SET_GLOBAL` value, a JIL attribute or a spool field is refused at that ingress; the seal never meets one |
| PR-11 | a float at any depth is refused at write; `deadman_us` round-trips |
| PR-12 | duplicate keys rejected at decode |
| PR-13 | only the top-level `digest` key is excluded; a nested opaque `"digest"` key changes the digest |
| PR-14 | `outbox_pending`/`executions` order is `(index, effect_id)`; a SPAWN precedes its run's later KILL |

### 13.3 Period identity

**Every obligation that needs a REMAP lands with the storage.** The `route`
verb, the `routes` query and the `route:` `expect` namespace are specified
and not built (§2.2). So PR-16, PR-16a and PR-16b are discharged only in
their carry and hash halves: `runtime_hash` ignores the table, the seal
carries a route in its frozen shape, and audit derives it. Their remap
halves are not discharged until the producer exists. PR-16c needs no remap
and is active whole.

| # | obligation |
| --- | --- |
| PR-15 | `runtime_hash` moves for **every field of `RuntimeProfile`**, with the case list derived from the model's own fields — a field added later is tested by default, and hashing a named subset cannot pass |
| PR-15a | CLI → `RuntimeProfile` normalization: omitted options resolve to the stated defaults, `--timezone` absent → `UTC`, `local-eligible` round-trips, duplicate `--as-machine` collapses, fractional seconds round to µs, and each duration is tested against its own bound — zero legal for `>= 0` fields, refused for `> 0` fields |
| PR-16 | a route-table remap does **not** move `runtime_hash`, is carried in `routes`, and a `pending_spawn` effect dispatched after the remap keeps its birth `{executor_id, generation}` |
| PR-16a | remap → crash → resume reproduces the new route from the `host{verb: route}` record; then seal, mutate the seal's `routes` field, and `audit` **fails** — proving it derives rather than copies |
| PR-16b | `routes` read answers `state_rev`; a remap composed against a stale revision is rejected; a remap naming no host row is rejected; A→B→A moves the revision twice; **A→B→A → seal → open → `routes` reads the same revision**, and audit fails when only that revision is mutated in the seal |
| PR-16c | a start through a role whose executor is **evicted** births a pending effect bound to the host row's current generation, held by the routing gate; crash and resume keep it pending and `_dispatched` agrees; the §8 re-drive of that held work is the HA track's and is **not** asserted here |
| PR-17 | a runtime-profile change with no catalog change is a transition, and so is a seal with nothing changed; a `next_period` whose `state_machine_version` differs from the seal's is **refused** at readiness |

### 13.4 Carry fidelity

| # | obligation |
| --- | --- |
| PR-18 | replay from a seal ≡ replay from genesis over the same inputs, over an estate exercising every carried item |
| PR-18a | job completes run N in C1 → seal → open → `CHANGE_STATUS STARTING` on it: no effect, no adapter call, and the next real start is N+1 |
| PR-19 | a depletable's spent units survive, not refunded |
| PR-19a | C2 removes the resource, C3 reintroduces it: the units are still spent |
| PR-20 | an in-flight job releases the vector it acquired |
| PR-21 | waiter order survives |
| PR-22a | a `CHANGE_STATUS STARTING` row with no execution entry seals and opens; an execution entry with no non-terminal row refuses. At the resume loader, which holds C2 (DL-151): an entry behind a live **box** row refuses and writes no segment; the same entry behind the box's dispatchable MEMBER opens |
| PR-22 | `open_from_seal` refuses each of §7 step 6's invariants when violated — one injected failure per invariant, duplicate timer tokens and **every shared-field disagreement** (`run_id` between effect and `effect_result`, `run_number` between row and execution, `artifact_format_version` between manifest and the seal that names it) included — and accepts an estate with a live **box** and no execution entry for it |
| PR-23 | genesis seeding never clears a carried operator hold |
| PR-24 | deadman bound is measured from the new period's takeover, not a carried `last_contact` |
| PR-24b | the supervisor is restarted with a deadman different from the requested value, the estate seals, and **offline audit without contacting that supervisor** reproduces the seal — host `state_rev` included |
| PR-24a | C2 restarts the supervisor with a longer deadman than C1's: the host is **not evictable** until it re-registers, and after it does the bound is the supervisor's observed value, never the carried or requested one |

### 13.5 The boundary

| # | obligation |
| --- | --- |
| PR-25 | no tick due ≤ T lost; none admitted twice |
| PR-25a | crash immediately after the opening `leader` record and before the missed-tick sweep: a tick between T and the leader's `at` is admitted or dropped-and-recorded, never silently consumed by `leader.at` |
| PR-25b | a missed tick, once dropped-and-recorded, is never re-dropped by a later resume of the same segment — exactly one `drop` record per tick, across any number of resumes (DL-174) |
| PR-26 | one held tick under C1 → exactly one start after C2 — unless an admitted `DISARM` dropped the latch in between: then none (DL-158) |
| PR-27 | **table-driven over every §8 gate**: non-empty input queue; open transaction; effect delivery in progress; a KILL ladder unresolved; an applied SPAWN with no `spawn.json` yet; unreconciled candidate; unreachable supervisor; restarted supervisor with empty `LIST`; pending outbox on a physical roll; indeterminate KILL — each refuses |
| PR-28 | phase-1 readiness, one injected failure per check — unsupported format version, hash mismatch, profile mismatch, SM-version mismatch, preflight, `request_id` collision, R gate — each refuses while C1 is open and untouched; **two live seal clients** staging different C2s — the engine commits exactly the one its request's fingerprint names and the committed boundary opens |
| PR-28a | phase-2 boundary validation, one injected failure per check — `first_index` mismatch, record/sidecar disagreement, a post-barrier live-closure change the phase-1 classifier did not see, `now ≠ T`, a load invariant — each refuses the commit while C1 stays open; **and a post-barrier latent A case appears in the committed seal's `classification`** — a seal carrying phase 1's map is refused by audit |
| PR-29 | the old period admits nothing after its seal |
| PR-30 | `--force-seal` records `force_seal: true` in `boundary_request`, and `forced_gate` populated iff the gate was engaged, per §3.1's truth table — including an unnecessary force (no gate), a period with no prior externally requested attempt (age ∞, gate passes), **a recent `rejected` attempt and a recent applied no-op (both hold the gate)** |
| PR-30c | two `seal` requests with one `request_id` and one `next_period` but different `force_seal` or `claimed_actor` collide and refuse; **an ordinary command and a `seal` sharing one `request_id`** collide and the seal refuses at readiness |
| PR-30e | a committed seal's exact retry arriving under the new baseline is answered before the baseline gate — **after a physical roll, a B restart, A's removal, and lawful pruning of A's WAL**, from the imported sidecar's `boundary_request`; the same retry two periods later is refused as stale |
| PR-30g | power loss **after** the committed seal: `periods/N+1/` and its `manifest.json` survive — on **both** the fresh-install path (four fsyncs) and the same-stage reuse path (its in-place liturgy); with any one fsync removed the test fails |
| PR-28e | a `rejected` and an applied-no-op control attempt arriving after §6 step 2 are refused at admission; one admitted just before the cut has its `decision` durable before the sidecar is written; the active seal request is **not** waited on and the seal commits |
| PR-28b | after **every** non-commit exit **before the seal append** — phase-2 refusal, and fault injection at each manifest/sidecar write, rename, fsync and pre-commit fence check — `abort_boundary` has run: a control command is admitted, a scheduled tick fires, an FW poll appends; a fence loss inside the interval fail-stops instead; so do an exception while an attempt admitted during the seal is not fully applied, a failed WAL append, and a `clock_regressed` on an engine-made input, which leave no `seal` record; a request that hits `clock_regressed` is answered refused; resume rebuilds from the WAL with the period open, and a start whose decision was missing gets that decision with its SPAWN and is dispatched once (DL-274, DL-315) |
| PR-28d | fault injection **on the seal append itself** — write error mid-line, `fsync` error after a complete line, power loss after flush before fsync: the engine fail-stops with an unknown outcome, never reopens admission; recovery then finds a complete line → `fsync`s the WAL and only then promotes it, **with power loss injected before and after that confirming `fsync`, and with the confirming `fsync` itself raising** — before it the seal may vanish and no successor exists; after it the seal is durable; when it raises, no anchor transition, no successor segment, admission stays closed, and a repeated recovery stays fail-stopped — a torn or absent line → truncated and C1 reopened, a line with records after it → refused |
| PR-28c | one operator hold, one **pre-armed** job and one held, **initially unarmed** job, a tick at T for the latter, then both a refused and a committed boundary: the pre-armed row is exactly as the operator left it; the initially unarmed row is `armed: true` with exactly the one legitimate C1 revision increment the tick caused — in **both** outcomes, so an abort that restored a pre-freeze snapshot fails; after the commit the operator's `OFF_HOLD` in C2 produces exactly one start |
| PR-30f | crash before and after the engine's committed-manifest write, before the rename: the retry re-validates, overwrites with its own, and the installed `periods/N+1/` holds both files |
| PR-22b | resume never runs a profile the period did not pin: a launch option that disagrees with the committed manifest's `RuntimeProfile` **refuses the resume**, naming the fields that moved, and the settings the wiring cannot express resolve from the pin rather than from an ambient default. Both halves, one case each — including the deadman, which compares at its OBSERVED value and not the asked one. A DECLARED `as_machine`/`machine_policy` that disagrees with the pin refuses and an undeclared one inherits it; and a refused open over a COMMITTED boundary leaves no segment and an unmoved head, so the corrected retry opens the same boundary (DL-151) |
| PR-30d | the engine dies after installing `periods/N+1/` and before the `seal` record, under power loss: a retry with the same `stage_digest` — **after an intervening indexed C1 admission** — reuses the staged identity and regenerates `manifest.json` with the new `first_index`; a retry differing in **each staged field** (`catalog_hash`, `catalog_hash_version`, `source_bundle_hash`, `runtime_hash`, `state_machine_version`, `artifact_format_version`) quarantines it and installs its own; alternating S1 → S2 → S1 → S2 quarantines without collision; and the engine-derived committed fields never alter `stage_digest`; the committed boundary opens either way |
| PR-30a | the live `seal` request: a lost response **before** the seal record → the retry is a fresh request that seals (the period was still open, nothing named the first attempt); **after** it → the exact retry is answered from the committed seal in the new period; a collision refuses |
| PR-30b | live-mode seal exits code 3 and no detached command is signalled |

### 13.6 Live execution

| # | obligation |
| --- | --- |
| PR-31 | a detached command live at T is reattached, executes exactly once |
| PR-32 | resume from the seal alone, supervisor answering nothing, names executor/`run_id`/generation of every live run |
| PR-33 | at ordinary resume, a live wrapper under a **terminal** row is re-driven and does not outlive the resume — **table-driven over the KILL effect state**: `applied`, `indeterminate`, `retired`, pending, and *no matching KILL at all* |
| PR-33a | the seal **waits** for an unresolved KILL ladder and refuses to snapshot one |
| PR-34a | FW resume: the engine dies after the `start` line and before `effect_result{applied}` — resume resolves the pending SPAWN by the line, appends **no** second `start`, and the reconstructed watch is one; and after a completing poll before its STATUS is durable — resume injects the completion from the log |
| PR-34 | an unchanged FW watch, table-driven over the poll phase at T — after the `start` line and before the first poll (`next_poll_at == start.at`), after a poll (`next_poll_at == poll.at + interval`), before observe, between observe and append, after append — plus several no-progress polls, a seal, another poll in C2, then audit C1: the entry is reproduced from the first `watch_seq` lines, the watch completes at the same poll it would have without a boundary, and no C1 line lands after `watch_seq` |
| PR-35 | a decision and its effects survive a crash together or not at all (CM-17) |
| PR-36 | §11a as a crash matrix: killed after `mkdir`, after the index entry, after `receipt.json`, after the wrapper spawn, after `spawn.json`, after `reply.json` and after the answer — then the supervisor is restarted and the SPAWN replayed, **both against the same path and against a different `(job, run_number)`**, and **a different `run_id` against the same path**; each row answers as the §11a table says, no row spawns twice, no directory ever carries two keys; plus the engine dying between a tethered `mkdir` and SPAWN, a fingerprint collision, a duplicate answered in the frozen `duplicate: true` envelope, and a `run_id` outside the grammar refused at the wire |
| PR-36b | deleting a run directory or index entry for a replayable SPAWN is refused by the retention floor; after the period is attested and the run terminal, it may go |
| PR-36c | pruning refuses each artifact reachable from the head — sentinel, anchor, claim, opening and closing sidecars, current and next manifests, an uncommitted candidate's `staged_manifest.json` and `candidate.json`, bundles, `sources.json`, the latest attestation checkpoint — one case each. Once the head has moved past them and a later checkpoint covers them, each becomes **`held`** and is released only where §12a's class names it: an attested period's WAL and a **committed** candidate's two files may go under `archive-inputs`; a sidecar, a period manifest, a superseded checkpoint and a bundle stay held, each verdict naming the rule that decided it and never a retired open question (DL-144) |
| PR-36a | **engine-side, from the durable effect**: the engine dies after the supervisor wrote R1's index and before the engine recorded the outcome; resume replays the SPAWN effect — which carries R1 — and the supervisor answers duplicate; no R2 is ever minted. The test starts from replay of the WAL, never from a variable holding R1 |

### 13.7 Classification

| # | obligation |
| --- | --- |
| PR-37a | **table-driven over every `RuntimeProfile` field**: each field's change classifies exactly the jobs §10.2's table names as changed (positive) and no others (negative); `retry_horizon_us` moves `runtime_hash` and classifies **no** job; a live CMD with only `cmd_grace_us` changed is R |
| PR-37 | each of a changed resource amount, calendar set, machine field, declared global default, `insert_xinst`, timezone map classifies dependents as changed — none moves a `JobIR` or an IR-G edge |
| PR-38 | two-hop condition and nested-box containment both reach the closure |
| PR-39 | `armed` + changed schedule → A, and the A is **reachable** (not shadowed by an R rule) |
| PR-39a | a `pending_spawn` whose closure changed → R; opened without the R gate, it would execute C2's command under C1's run number |
| PR-39b | a `next_period` with a different `state_machine_version` never reaches the classifier: readiness refuses it first (§2.1) |
| PR-40 | QUE_WAIT + removed → R, no `KeyError` |
| PR-41 | INACTIVE + carried timer → latent intent |
| PR-42 | member changed while box executing → R; no box run observes two versions of anything in its closure |
| PR-43 | executing rule ∧ named A rule → R |
| PR-44 | the reverse closure produces the boundary-truth diff |

### 13.8 Recovery and stream

| # | obligation |
| --- | --- |
| PR-45 | every §11 matrix row as its own crash-injection test, the three claim-state rows and the period-1 row included |
| PR-46 | an orphan sidecar is never selected |
| PR-47 | resume from an unattested seal with corrupt inputs refuses; a self-consistent digest alone is **not** accepted; `--trust-unaudited-seal` proceeds and the opening `segment` records it. The third clause lands with the switch (§11, DL-133) and is undischarged until then; the first two are active |
| PR-47a | `audit` refuses a period whose `state_machine_version` it does not implement, naming the version and the `dsl41_version` that produced it |
| PR-47d | `baseline_id` of the successor is reproduced by audit from `{estate_id, period_id, stage_digest}`; mutated **consistently in every artifact that carries it** — sidecar, `seal` record, `manifest.json`, and the successor `segment` if one exists — with every incidental digest recomputed, audit fails **solely** because the value ≠ the derivation |
| PR-24c | an evicted host row carries across a seal as evicted; nothing in this spec un-evicts it; a re-registration of a non-evicted host that changes only `deadman_us` or `last_contact` writes no record and moves no revision |
| PR-27a | a host evicted, its work re-driven or retired, its spool reconciled, its supervisor gone for good: the seal **commits** |
| PR-47c | a CMD live at T whose `status.json` lands in C2 audits as live in C1 — including `ended_at == T`, because ownership comes from the WAL's admitted completion, not the timestamp |
| PR-47b | `audit` reproduces **every** digest-covered field except the `boundary_request` input scalars, which it checks record-vs-sidecar and carries; a consistent rewrite of `request_fingerprint`, `forced_gate.horizon_us`, `forced_gate.observed_age_us`, a top-level/nested actor disagreement, **or a record and sidecar that disagree on `source`** **fails** |
| PR-47e | seal under `retry_horizon_us` = H1, audit under an ambient setting H2 ≠ H1: audit derives `forced_gate` from H1 read out of the **closing** period manifest; and C1 = 60 s / staged C2 = 1 s with a 10-second-old attempt refuses unforced, while the reverse commits; an effect with no `effect_result` is in `outbox_pending`; one with `applied`, `indeterminate` or `retired` is not, and each of those still shapes the reconstruction; and one dropped scheduler tick reaches the frontier |
| PR-48 | **RETIRED by DL-138.** It named the `estate adopt` crash matrix, and neither the verb nor the transaction exists. Its replacements are the refusal tests DL-138 owes (`docs/protocol-evolution.md` §7), one set per owner: a journal opening with a `header`, a `result` mid-journal and a standalone `effect` each refuse naming the kind and DL-138, while a `host` record is accepted and an **unknown** kind refuses naming itself as its own error; `legacy_batch` false proceeds, true refuses naming DL-138, missing or non-boolean refuses as malformed — the true case driven through a history and a retention consumer as well as through the central validator; `catalog_hash_version` 1 refuses naming DL-138 through **both** the journal reader and journal creation, and an unknown version refuses generically; a root holding `manifest/manifest.json` where the period manifest is absent refuses naming the retired layout, while a `manifest/` directory without that file refuses generically; `claim_root` and `plan_retention` on a `header` root refuse naming DL-138 and on garbage refuse generically; an on-disk anchor whose head state is `adopting` refuses **before parse**, naming DL-138; and `estate adopt` is not a command |
| PR-49 | subscribe: pruned cursor → gap marker; `decision` across the backfill/live seam; exact-retry cursor |
| PR-50 | run history spans a boundary keeping `start_period` |

### 13.9 Regression

| # | obligation |
| --- | --- |
| PR-51 | every existing test — `test_cm*`, `test_sem*`, subscriber, journal, history, supervisor, TUI — stays green |
| PR-53 | **the receipt is the point of no return.** No artifact of a period is deleted before its `seals/<period>.archive.json` is durable; the receipt is a §3.2 artifact whose stored bytes are its canonical serialization and whose digest is its own, and whose `archived` list validates in exactly the two shapes above and no other; a crash **between the receipt and the deletions** re-plans and COMPLETES from the receipt — **including a crash between the two candidate files**, where the ordinary derivation can no longer see the pair at all and only the receipt still names the survivor. A crash **before** the receipt leaves an estate where nothing happened, and a second sweep never rewrites a receipt already there |
| PR-54 | **eligibility is itemized and re-checked.** One case per §12a condition: unattested (the ss12 floor answers first, and the archive is never asked), no LATER checkpoint, an unarchived older period, and — one at a time — a surviving run directory, `.by_run_id` entry or default log of a run born in the period. Each holds the WAL at `held` with the blocking dependency NAMED, and the spool cases name the artifact that remains. A period eligible at plan time whose covering checkpoint is invalidated before the receipt REFUSES and is reported; every period above it refuses with it, by the prefix rule. A committed candidate's two files ride the same cover; bundles and period manifests stay held |
| PR-55a | **one door, seven bindings.** Table-driven over a receipt whose integrity is intact and whose BINDING is not — a wrong `seal_digest`, a wrong `attestation_digest`, a wrong `chain_through_period`, a foreign `estate_id`, a **correlated foreign seal-and-attestation pair** with the receipt restamped onto it, and bytes that do not parse: each refuses in the walk, the plan, the tier, `audit`, `journal`, `runs` and `estate prune`, with the same reason, and **none of them answers shorter instead**. `audit_period` and the re-derivation refuse it as FUNCTIONS, with no CLI in front of them. A receipt naming a file it does not license excuses nothing. The cover, likewise: a registry row redirected to a **present** root of another estate, **or to a same-estate root holding a second valid pair for that period**, refuses the plan and the live re-check, and releases nothing |
| PR-55 | **permanent floors and irreversibility.** The receipt, the archived period's attestation and its seal sidecar are unreachable by `prune` — one case each, `_remove` refused rather than merely not asked. Restoring the archived inputs beside the receipt leaves every reader at the **attestation-verified** tier. A receipt whose attestation or sidecar is absent refuses. Deleting a WAL over an older period whose deletion failed does not happen: the retained segments are a contiguous suffix after every partial sweep |
| PR-56 | **no reader answers shorter in silence.** Over a multi-period archive, in place and across a physical roll: the estate walk resolves an archived registry row and refuses an unreceipted absence BY NAME; `audit` reports the archived period at the attestation-verified tier in wording it shares with no derivation-verified line, and still audits the rest; `journal` prints the unreplayable gap on STDOUT and crosses the next boundary by the predecessor's attestation; `runs` names the coverage it lacks; `estate prune` re-plans an archived root, including one whose every period is archived. The subscriber's backfill answers a cursor below the archive with §11's gap marker at the oldest RETAINED record — unchanged, and only because the archive is a prefix — and a live engine still resumes, because recovery selects its seal by the sidecar. Accidental loss with no receipt refuses in the walk, in `audit` and in `journal`, each naming the receipt it did not find |
| PR-52 | `scripts/arch_check.py`'s ownership gate covers `RuntimeState`'s own state — the row models (`JobRuntime`, `HostRuntime`, `CapacityReservation`, so `start_period`, `reservations` and `waiter_seq` with them), the private maps, and the scalars `consumed`, `enqueue_counter` and `timer_seq`: a mutable one reachable outside its owner fails the build. `routes` joins the gate with its storage (§2.2); the seal's own frozen artifact models are not runtime state and are not in it |

## 14. The worked estate

`examples/nightbank` carries three scenarios.

**A — the quiet boundary** (smoke). The night runs to completion under C1. A
global is set, an operator hold is placed, and a depletable is consumed.
Seal. C2 changes three jobs. Open in place. Assert the carry, the ghost, the
A rows, the truth diff, and `audit` reproducing the digest.

**B1 — the live boundary that commits.** Seal mid-night, detached, with C2
touching **none** of the live closure, and with all of:

- a long command live and reattached (PR-31);
- a KILL ladder in flight that the sealer waits out (PR-33a);
- an unchanged FW watch crossing, reproduced by audit (PR-34);
- a live box with an INACTIVE member, unchanged (PR-42's carry half);
- a QUE_WAIT pair (PR-21);
- an INACTIVE job with a semantic timer (PR-41);
- a `pending_spawn` on a passive host, unchanged (PR-32);
- two timers due at exactly T;
- a `--force-seal` and a late C1 retry (PR-06, PR-30).

B1 is in `tests/test_nightbank_boundary.py` (DL-143) as two scenarios, not
one. `test_b1_the_boundary_commits_over_a_night_in_flight` takes the whole
live closure at once, detached under a real supervisor, with real commands
and a real watch.
`test_b1_two_timers_due_at_exactly_t_are_c1s_and_the_next_one_is_c2s` takes
the exactly-T row alone. T is `clock.now()` at the barrier, so the instant
is a CHOICE only in the virtual domain. There, the estate's own
`must_complete_times: "+20"` region boxes are armed to fall on T exactly,
with a third one minute later. The test pins §6's rule: the two fire inside
C1, and the third is carried unfired and fires in C2.

"Touching none of the live closure" means C2 touches **something**. An
identical C2 moves no graph node, so the R gate would pass with nothing to
classify, and §10.2's closure would never be computed. C2 changes one job,
the estate's iced, decommissioned report. On the small nightbank estate the
jobs outside every live forward closure are a **short list**, because a
shared machine or a shared box reaches almost everything.

**B2 — the boundary that refuses.** The same estate, one change at a time.
Each is a separate seal attempt that must refuse:

- C2 changes the `pending_spawn`'s command (PR-39a);
- C2 changes the live box's INACTIVE member (PR-42's R half);
- the supervisor is restarted before the seal, so `LIST` is empty (PR-27);
- an applied SPAWN has not yet written `spawn.json` (PR-27).

These are refusal scenarios, not end-to-end evidence, and each stands alone.

B2 is in `tests/test_nightbank_boundary.py` (DL-143): one `test_b2_*` per
row, each over B1's live closure. Each asserts the refusal **by name**, and
each names WHICH of §8's two refusal points answered, because they leave
different logs. Readiness refuses before the barrier and appends nothing.
A refusal after the cutoff leaves the cutoff's own admitted work, which is
legitimate C1 activity and not damage. A row that asked only for "no `seal`
record" could not tell the two apart, and an R gate that moved from phase 1
to phase 2 would pass it.

Three of the four rows then assert the live closure that the refusal left
alone: the long command still running as a **process**, not just as a row.
The fourth row cannot, and the reason is the row itself. Restarting the
supervisor takes its wrappers with it, which is exactly why the boundary
must not commit over their carried rows. That row asserts the estate and
the FW watch instead. It is the one that pays for the literal reading of
"the watch still watches": a new durable `watch.jsonl` line after the
refusal.

**C — the lineage** (the fence):

- a quiet physical roll after attestation (PR-02a, PR-02d);
- a fork attempt from a second root (PR-01);
- the winner crashed with the head `claimed` (PR-02);
- the anchor deleted under the incumbent (PR-03);
- a crash in period 1 before any seal (PR-45);
- a lost `seal` response on both sides of the record (PR-30a).

## 15. Amendments

Where the period model lives outside this document:

| document or module | what it holds from this model |
| --- | --- |
| `concurrency-model.md` §2 | the log is one **estate** of period-bounded segments, not one run root; `baseline_id` is per period |
| `concurrency-model.md` §4/§5 | the atomic `decision` record in place of `result` + `effect`; CM-17 closes on the file substrate |
| `concurrency-model.md` §7 | leader eligibility reads the current period's pins; `next_epoch` reads the seal; **`catalog_hash` is v2, with `meta.tool_version` excluded** |
| `concurrency-model.md` §11 | the catalog is immutable **per period** |
| `control-protocol.md` | **v3**: `baseline_id` is the period's; the `seal` verb; the `host` cmd's `route` verb and the `routes` query with the `route:` namespace *(pending — §2.2)*; `decision` in the subscribe stream; the gap marker; exact-retry expiry |
| `runner_admission.py` | `Attempt.host` as `HostCommand \| RouteCommand`; the `route:` namespace in `RuntimeState.revision()` *(pending — §2.2)* |
| `supervisor-protocol.md` §3/§5 | `receipt.json` `{artifact_format_version, run_id, spec_fingerprint, received_at}`, `reply.json` `{artifact_format_version, run_id, wrapper_pid, spawned_at}` and the `run_id` index entry `{artifact_format_version, run_id, job, run_number}` in the spool, each §3.2-canonical and liturgy-written; the supervisor creates a detached run's directory; SPAWN idempotency is directory-backed (§11a) and outlives `LIST` presence and supervisor restart; the `run_id` grammar is enforced at the wire |
| `runner_adapters.py` FW | append-only `watch.jsonl`: a `start` line on dispatch, then one line per poll |
| `runner_adapters.py` CMD / `runner_effects.py` | `run_id` is minted in `plan_effects` and carried on the SPAWN effect; the adapter reads it from the effect and does not mint it |
| `concurrency-model.md` §5 | `run_id` is bound before the attempt (DL-96's deviation does not apply) |
| `runner-design.md` §7 | record kinds; resume from a seal; the ladder re-drives a live wrapper under a terminal row, whatever the KILL effect's state (PR-33) |
| `deployment-runbook.md` §6/§7 | seal→swap→open in place; latches cross a seal; an upgrade keeps state |
| the withdrawn HA plan (DL-189) | its requirements from this model: ACQUIRE gains a lineage-head predicate, the store replaces the anchor, and routes are carried state. The plan was withdrawn before it took them; the requirements stand wherever a shared store is built |
| `runner_ledger.py` | `LeaderLock` generalized to the anchor |
| `capacity.py`, `oracle_state.py` | §5 |
| `runner_supervisor.py` | completed-run tombstones (PR-36) |
| `runner_history.py`, `cli.py journal` | period-aware |
| `period.py` | `job_fingerprints`, §10.2's leaf test (DL-131); a pure analysis pass may not import a private name out of a runner module |
| `protocol-evolution.md` | (DL-138) the compatibility matrix per protocol, the lifecycle by which a dialect enters service, the retirement gate, the pre-production reset clause, and the tombstone-registry rule |
| `retention.py` | the `archive-inputs` class, the receipt, the permanent floors and the itemized eligibility (§12a, DL-144); `estate prune --archive-inputs` |
| `period.py` | `ArchiveReceipt`, `seals/<period>.archive.json` and its readers (§12a) |
| `deployment-runbook.md` §2a | the archive is an operator verb with an order in front of it: attest, prune tombstones, then archive; and it cannot be undone |
| `citation-index.md` | the `PR-\d{2}[a-z]?` row, and a `PR-Q\d` row for §16's open questions (DL-135); the PR row states what a retired row cites (DL-138) |
| `CLAUDE.md` | read-first list |

## 16. Open questions

- **PR-Q5** (open): the anchor in a paired-site deployment. It stays
  single-site until a store replaces the anchor. The HA plan that would have
  cited this question was withdrawn (DL-189).

Closed, kept because the decision log and the code cite them:

- **PR-Q1**: `retry_horizon_us` is a `RuntimeProfile` field, durable in the
  period manifest, so audit re-derives `forced_gate` under any later ambient
  setting (PR-47e). The gate is soft.
- **PR-Q2**: there is no size roll; to roll, seal (I1).
- **PR-Q3**: closed by policy, not by deduction (DL-144). A seal-only
  archive may stand in for pruned inputs under §12a's `archive-inputs`
  class, with a durable receipt before any deletion, an itemized eligibility
  list, three artifacts on a permanent floor, and readers that name the gap.
  §11's "verified" is two named tiers, and an archived period stands at
  *attestation-verified*. E20 (`docs/runner-design.md` §15) closes with it.
- **PR-Q4**: audit runs the interpreter that produced the period, and old
  versions stay installable (§11).
