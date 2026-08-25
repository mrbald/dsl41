# Period model — the seal, the segment, and the optional run root

Status: **frozen (2026-08-20, DL-114).** Draft 31, converged. Intended to become normative in the way
`docs/concurrency-model.md` and `docs/control-protocol.md` are: once frozen,
each change to a frozen item requires a decision-log entry. It supersedes
`docs/ops-model.md` §1–§3 and §8a–§8b as the *mechanism*; that document stays
the ops-view plan and points here.

Nothing ships incrementally. Correctness is carried by §13's obligations and
§14's worked estate, not by staged exposure. An obligation weak enough to let a
broken implementation pass is a defect of the same rank as a wrong mechanism.

## 0. The problem

A run root today carries four lifetimes and forces them to end together
(`ops-model.md` §0). An estate change is stop → swap → **new run root**, and a
new run root is a new log, a new baseline and a fresh oracle. So every release
silently resets every runtime global, every operator hold, every `last_end_at`
that lookback reads, every `armed` latch, every box's `ran_members` and every
`run_number`. That reset is correct at a cycle boundary and wrong in the middle
of one, and nothing in the model says which one an operator is doing. The
directory is doing a job that belongs to a record.

## 0a. Revision history

- **Draft 1** — the frame. Adversarial review found six critical and six high
  defects: a forkable lineage, a seal that did not commit its successor, orphan
  sidecars selected as authoritative, an incomplete period identity, lost
  execution bindings, and a known atomicity violation left in place.
- **Draft 2** — fixed those, and review found the fixes had defects of their
  own: the anchor was a one-time claim rather than a fence, `executions` was one
  overstuffed row for four lifecycle states, the atomic `decision` record broke
  frozen v2 subscribers, the classification graph put a mutable route table into
  period identity, and named obligations still let a specific wrong anchor
  implementation pass all forty. My own read added: the live-closure rule made
  every named A case unreachable, the seal *operation* had no owner and no CLI,
  and canonicalization strictness was a liveness risk.
- **Draft 3** — a rewrite rather than a patch. Review found three impossible
  state machines it had introduced: `terminating` was both carried and refused
  by the gate; the live `seal` request had no valid commit point; and the
  successor claim moved the head to a digest that did not exist yet, keyed the
  claimant on a PID that a crash necessarily changes, and had no durable claim
  artifact. Plus two classification holes (a pending SPAWN executing C2's
  command under C1's run; a state-machine bump reaching no graph node), no
  resume path before the first seal, and `catalog_hash` mis-sold as a byte
  address.
- **Draft 4** — fixed those. Review found the lineage protocol never defined
  the *closing* transition (`open → closed`), so no opener could ever run; a
  physical roll had no way to find the artifacts in the old root; live FW
  progress was carried but not re-derivable, so audit could not reproduce a
  seal taken over a watch; an applied-but-not-yet-bound SPAWN fit no
  `executions` kind; a carried `deadman` could authorize an eviction shorter
  than the supervisor's real bound; `catalog_hash` was mis-described; and
  legacy adoption was a sentence, not a transaction.
- **Draft 5** — one simplification fell out: **a period is one segment.**
  Review found the physical roll still could not audit its own past; a
  state-machine bump had no executable readiness path in a one-version binary;
  legacy adoption did not fence the old runner and left a header journal an old
  binary would happily append to; `watch.json` was mutable and could not prove a
  historical seal; the bundle hash erased the source order `catalog_hash`
  depends on; supervisor tombstones were a word; `consumed` had no story for a
  removed-then-reintroduced resource; the anchor CAS said rename, not durable
  rename; and the seal fingerprint was narrower than the command.
- **Draft 6** — the state-machine scope cut. Review found the pinned
  `catalog_hash` includes `meta.tool_version`, so byte-identical openings across
  a patch release were impossible; adoption still had crash windows an old
  binary could lead through; the FW log had no poll barrier and no durable
  cutoff position; the SPAWN receipt left directory ownership, `run_id` lookup
  and the durable reply undefined; adoption's "segment 1" was header-first with
  retired record kinds; and the physical roll used "audit" for two different
  things.
- **Draft 7** — `catalog_hash` versioned; adoption translates; FW log gets
  write-ahead order and a position; SPAWN gets an index; `verify` ≠ `audit`.
  Review: the core model is *"ready to implement in parts"*; the criticals left
  are three sub-protocols — adoption still wrote authority before its fence and
  did not run the recovery barrier or require a drained estate; the attestation
  chain had no induction rule; the SPAWN write order left a state after
  `receipt.json` and before the index that a cross-path retry could double-run.
- **Draft 8** — adoption fences before authority on a drained estate;
  attestation induction; index-before-receipt. Review: *"no new lineage-fork
  path remains in the normal transition."* Left: adoption could not safely
  recover an admitted input with no `result` (a SPAWN it should have planned
  either dispatched before its decision was durable or was silently failed);
  `run_id` was still minted by the adapter, after the durable effect, so a
  crashed engine could re-mint it — double-run class; the attestation rule
  required N−1 for `verify` and then let a second roll pass without it —
  unauditable-checkpoint class; and several newly normative fields were absent
  from their own schemas.
- **Draft 9** — adoption refuses result-less inputs; `run_id` minted in the
  effect; producer/consumer attestation rules. Review: *"no hidden normal-path
  lineage fork remains."* Left: a seal request that crashed before its record
  could not be recognised on retry, and PR-30a demanded an outcome the records
  could not produce; adoption's barrier could still dispatch a reconciliation
  SPAWN before its decision was durable — double-run class; a legitimately
  retired legacy SPAWN has no `spawn.json` and so no `run_id` to translate; the
  attestation had no versioned byte contract — unauditable-checkpoint class;
  live mode let two CLI clients race on one non-content-addressed C2 manifest
  path; `runtime_hash` was an open list; and the execution schemas contradicted
  each other on `start_period` and `run_id`.
- **Draft 10** — uncommitted seal requests are unseen; dispatch-free adoption;
  §3.2 governs every artifact; typed `RuntimeProfile`; C2 staging. Review found
  the old-binary fence existed **only** on adopted roots — a native or rolled
  root has no `journal.jsonl`, and an old binary treats that as an unused root
  and starts a fresh genesis in it (fork class); FW resume had no protocol for
  the window between the durable `start` line and `effect_result`; staging
  used one name for two different fingerprints and could not recover an
  installed-but-uncommitted candidate; `RuntimeProfile` had names but no types;
  a legal control input containing an unpaired surrogate could make the estate
  unsealable; the "every artifact is versioned" claim was false against the
  schemas; and tombstone retention was implicit.
- **Draft 11** — the sentinel on every root; FW resume by `start` line; two
  staging fingerprints; typed `RuntimeProfile`; scalar strings; **one shared**
  `artifact_format_version` for every artifact; retention floors. Review: native genesis could **overwrite** an
  existing anchor (fork and double-run class); the physical roll took its claim
  before its sentinel (fork class); a committed seal's retry was refused as a
  stale baseline before the seal was consulted; `RuntimeProfile` had the wrong
  `machine_policy` enum, unnamed defaults and no positivity; the literal
  schemas for the sentinel, FW lines, anchor and claim disagreed with the rules
  about them; and the retention floor omitted the artifacts recovery itself
  refuses without.
- **Draft 12** — create-only genesis; sentinel-before-claim; seal retry route;
  corrected profile; exact schemas; head-reachable retention floor. Review:
  adoption still wrote its anchor without requiring absence (the D11-1 fork,
  through the other door); a target root's sentinel had no create-only
  ownership rule, so an estate could take over another estate's dormant root
  (fork class); genesis recovery both required and forbade reopening its own
  anchor; the seal-retry evidence lived only in the old WAL and vanished after
  a physical roll or lawful pruning; `next_period` did not bind the target
  manifest's format version; and the profile's zero bounds contradicted their
  obligation.
- **Draft 13** — one ownership rule for roots and anchors; retry identity in
  the sidecar; `next_period` binds the format version. Review: *"no remaining
  normal-path lineage fork and no new execution double-run path."* Left: host
  re-registration moved a projected `state_rev` with no journal evidence, so
  audit could not reproduce it (unauditable-checkpoint class); `first_index`
  was staged by the client before the cutoff barrier consumed indices, so C2
  could reuse C1's last index; the installed candidate lost its `stage_digest`;
  physical-root resume selected a `seal` record the new root never imported; a
  seal `request_id` did not collide against ordinary decisions; and the roll
  sentinel could not name "this very claim".
- **Draft 14** — D13 fixes plus a consolidation read. Review: `_dispatched`
  — the ghost-run gate that decides whether a STARTING plans a SPAWN — was in
  no inventory, so a `CHANGE_STATUS STARTING` after opening could plan run N
  again (double-run class); `routes` was carried with no WAL record able to
  reproduce a remap (unauditable-checkpoint class); the staged and committed
  `next_period` were one type although `first_index` exists only in the
  latter; the claim-bound sentinel rule was impossible for an in-place claim;
  adoption had two recovery owners after its segment was written and allocated
  no leader term for its own inputs; the execution join refused a legal
  `CHANGE_STATUS STARTING` row; the staging rename could not place the bundle
  and the manifest at two sibling paths at once.
- **Draft 15** — `_dispatched` derived with its reconstruction; route remap as
  a `host{verb: route}` input; `StagedNextPeriod` vs `CommittedNextPeriod`;
  the `adopting` head state; the adopter's own term; a one-way execution
  join; two-step staging. Review: **no CRITICAL findings for the first time**
  — no new fork, no concrete double-run. Left: the route row had no
  `state_rev`, no readable CAS token and no generation check (an ABA remap
  serialized identically before and after — unauditable-checkpoint class);
  adoption did not run the common C2 readiness body, so it could fence the
  legacy root and commit a boundary whose C2 could not open; `adopting` was
  named but absent from the anchor schema and transition table, and adoption's
  final CAS still said `open → closed`; period 1 could vanish from the
  registry; readiness ran a loader that validates `first_index` before it
  exists; adoption's synthesized segment lacked `catalog_hash` v2.
- **Draft 16** — `RouteRuntime`; adoption through the common seal body; the
  `adopting` state in the schema; registry rows at first ownership; staged vs
  committed loaders. Review: all HIGH or below; *"no remaining normal-path
  lineage fork and no concrete execution double-run."* Left: the seal shape
  still serialized routes without `state_rev` (unauditable-checkpoint class);
  the two loader phases were defined as "the same steps minus one" while the
  first phase has no seal, no digest, no record and no T to check; adoption's
  readiness needed `estate_id` before the step that minted it; PR-48 did not
  pin the reclassification after the adoption barrier creates new executions;
  the registry rules said both "only at `claimed → open`" and "at first
  ownership"; a route could go stale when its host's generation moved; and the
  route wire had no frozen envelope.
- **Draft 17** — three loader phases; route revision in the seal; route
  generation at effect birth; the route wire; provisional registry rows.
  Review: all HIGH or below; *"no remaining lineage-fork or concrete double-run
  defect."* Left: a route-blocked start had no durable representation; the
  loader "pure functions" did not receive the facts they validate; adoption
  classified before it knew the legacy state; a client could stage period 4
  after period 2, and attestation 3 could then never exist
  (unauditable-checkpoint class); phase 2's reclassification was not bound
  into the committed seal (same class); registry creation still had two
  orders; a displaced leader could serve stale reads; and the route wire left
  corners open.
- **Draft 18** — stale route as evicted host; typed contexts; engine-derived
  lineage fields; committed classification is phase 2's; adoption learns the
  legacy state first; immediate finalize; the fence covers reads. Review: no
  fork, no double-run; one unauditable-checkpoint item — the engine **minted**
  the successor `baseline_id` at random, so no audit input could reproduce it;
  the staged manifest needed a `period_id` the engine had not derived; the
  stale-route "re-drive as a new run" named a transaction nothing defines;
  phase 3 could not build an `Engine` purely; an evicted host's return was
  unjournaled; the reachability gate made a permanently dead, fully reconciled
  host block every future seal; PR-30d varied fields the client can no longer
  supply; and `catalog_hash_v1` had no schema field.
- **Draft 19** — derived `baseline_id`; two manifests; routes without
  generation; pure `OpenedRuntime`; scoped reachability. Review: no fork; the
  derived baseline depended on the request fingerprint, so a same-stage retry
  under a new epoch demanded a different baseline than the installed
  candidate carried; §7's manifest write order still contradicted §2.1; the
  `host{register}` input I had named was not an implementable contract (two
  frozen rules to reconcile and no producer) — double-run class if guessed;
  PR-47d could pass without evaluating the derivation; `BoundaryContext`
  lacked the manifest its own load needs; candidate artifacts were outside
  the retention floor.
- **Draft 20** — stable baseline derivation; one manifest transaction; no
  `register` record; closed contexts. Review: no fork, no double-run; two
  unauditable-checkpoint items — the seal's `boundary_request` (`request_id`,
  actor, `forced`) had no audit source but the seal itself, so "reproduce
  every field" was impossible for those; the committed-manifest write lacked
  the directory fsyncs the spec demands of every other artifact — plus a
  same-stage retry reusing an obsolete `first_index`, no defined unwind after
  a post-cutoff refusal, FW appends outside the fence, a quarantine path
  collision, and capacity values without sign invariants.
- **Draft 21** — `boundary_request` as input; manifest liturgy;
  `abort_boundary`; FW fence; sign invariants. Review: no fork, no double-run;
  two unauditable-checkpoint items — the same-stage **reuse** path rewrote
  `manifest.json` without the liturgy the fresh path has, and
  `boundary_request` lumped authoritative input with a derived fingerprint and
  gate outputs, excluding all three from audit; plus the "boundary holds" the
  text leaned on had no owner (the code has one hold bit), `abort_boundary`
  ran only on validation failure and not on every non-commit exit, and an
  adoption refused after its fence had no retry path.
- **Draft 22** — reuse-path liturgy; `boundary_request` split; no boundary
  holds; exception-safe interval; adoption retry. Review: no fork, no
  double-run; three unauditable-checkpoint items — a failed seal-record fsync
  was told to abort and reopen C1, though the line may already be durable;
  `retry_horizon_us` was "pinned" nowhere audit could read it; adoption's
  derived `request_id` was excluded from audit as if client-minted — plus a
  refused-path latch loss PR-28c could not see, and "hold the R-closure" named
  a set §10 does not define.
- **Draft 23** — the seal append as point of no return; horizon in the
  profile; `source` discriminator; the runbook's hold set. Review: no fork, no
  double-run; two unauditable-checkpoint items — recovery promoted a complete
  seal line it could *see* without proving it *durable*, and the `seal` record
  lacked `source` while audit trusted the sidecar's `source` to decide whether
  the request id was derived (a consistent `adopt → offline` downgrade passed)
  — plus the horizon's period authority (C1 vs staged C2) was ambiguous, the
  gate counted mutations rather than admitted attempts, and the horizon field
  made every live job R.
- **Draft 24** — recovery `fsync`s first; `source` on the record and derived;
  closing manifest's horizon; attempts not mutations; per-field profile edges.
  Review: no fork, no double-run; `source: offline` could not be derived —
  nothing in a `leader` record distinguishes an offline sealer from an engine;
  the per-field profile edges were written in both directions at once; the
  confirming-`fsync` *failure* was untested; one "mutation" sentence survived.
- **Draft 25** — `source ∈ {request, adopt}`; job→field edges; PR-28d
  confirming-fsync failure. Review (D25): adoption-evidence agreement refused
  every in-place period after an adopted period 1 (sentinel `adopted_from`
  is permanent, later `catalog_hash_v1` is null); step 2 froze "mutations"
  and let a rejected/no-op attempt in after the cut.
- **Draft 26** — evidence agreement scoped to period 1; step 2 freezes every
  attempt. Review (D26): step 2's drain included the seal request itself,
  whose decision is the later `seal` record — deadlock.
- **Draft 27** — step 2 excludes the seal attempt. Review (R31): **YES —
  end-to-end implementable.** Confirmation pass (R32): zero fork /
  double-run / unauditable-checkpoint defects; four text residuals.
- **Draft 28** — the four residuals. Review (R33): zero class defects; one
  MEDIUM — `PR-28a1` fit neither the regex nor the test convention; **YES**.
- **Draft 29** — `PR-28a1` → `PR-28e`, and the text converged after 33
  adversarial rounds. §1.3 and §11 agree that
  adoption's finalize is folded into `adopting → closed`; PR-02c says exactly
  when period 1's row flips; `PR-\d{2}[a-z]?` is the namespace regex and
  suffixed ids are cited; §3.2 says `deadman_us` once; the SPAWN replay
  lead-in appears once.
- **Draft 30** — the legacy read dialects retire (DL-138). Adoption from a
  pre-period estate is ruled out: no dsl41 estate runs in production, so the
  `header` journal, `catalog_hash` version 1, the `result` and standalone
  `effect` records, the `manifest/` layout and the whole `estate adopt` path
  have no producer and no estate left to consume. Each retired artifact is
  refused by name; the `estate adopt` command is removed outright — an unknown
  command, not a tombstone.
  Out of the schemas go the `adopting` head state, `catalog_hash_v1`, the
  sentinel's `adopted_from` and the `adopt` seal source; `legacy_batch` stays
  on `decision`, required and false. `docs/protocol-evolution.md` is the
  contract the retirement ran under and records it as the first executed one.
- **Draft 31** — PR-Q3 closes, as a POLICY DECISION and not a deduction
  (DL-144). A seal-only archive **may** stand in for pruned inputs,
  conditionally. §11's "verified" splits into two named tiers —
  *derivation-verified* and *attestation-verified* — because one word for two
  proofs of two strengths is how an estate stops being able to say which
  periods it can still re-derive. §12 gains the archive: a receipt that is the
  point of no return, a permanent floor of three artifacts per archived
  period, an itemized eligibility list, and readers that name the gap rather
  than answering shorter. Nothing here was deduced from the earlier text —
  §12's own words allowed either answer, which is why the question was open —
  and no live estate was needed to close it.

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

I1 keeps replay simple: one file, one catalog, one state-machine version, so a
reader never switches semantics mid-file — and it removes a whole recovery
sub-protocol. Draft 4 allowed segments to roll for size and then had no durable
active-segment pointer to recover two candidates by. There is no size roll:
**to roll a segment, seal** — a transition with an unchanged catalog is a legal
period (§2.1), and it is how an operator bounds a long-lived period's file. I2 makes an effect id, a spool path
and a revision mean one thing for the life of the estate.

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

**Every periodized root carries a permanent `journal.jsonl` sentinel** — one
line, `{"rec": "period_root", "artifact_format_version": 1, "estate_id": …,
"see": "wal/", "claim_id": null}` — whether it was created by genesis or
opened by a physical roll; a roll's differs in `claim_id` naming the
claim that **first opened this root**, so "this very claim" is a fact the
sentinel can prove rather than infer from `estate_id` alone. The claim-equality
rule applies to a physical roll creating a previously unowned root; an in-place
opener takes a new claim every period and its root's sentinel keeps the claim
that created the root, so for an in-place claim the sentinel proves only that
this estate owns this root — which is what it needs to prove. One record kind,
one schema. Draft
10 created the sentinel only on the adoption path DL-138 retired, so a native
root that sealed and exited code 3 released `leader.lock` over a directory with
no `journal.jsonl`; a build that finds no journal there reads the root as
*unused*, starts a new estate beside the lineage, ignores the anchor, and can
admit work while detached C1 executions are still alive. The sentinel closes
that window by never being absent: a root that sealed never reads as unused,
which is also why the file keeps the old name.

**One ownership rule governs every root and every anchor**, and it is the
current runner's own rule — it refuses any root with an existing
`journal.jsonl` — generalized:

> **absent → create. Exact same estate and exact same incomplete transaction →
> resume. Anything else → refuse.**

For a **root**: creating the sentinel refuses a target that already holds a
`journal.jsonl` of any kind — another estate's, an earlier period of this
estate's, or a concurrent opener's — unless it is this estate's sentinel for
this very claim, left by our own crash. "Fresh root" was not a checkable rule:
E1 could take the free `leader.lock` of a dormant estate E2's root R, overwrite
R's sentinel and install its imports while E2's anchor still named R; and two
estates racing for R could leave one anchor `claimed(R)` while the other
replaced R's sentinel (PR-01c). **Absence of the sentinel is not by itself
absence of an estate.** A root that lost only its `journal.jsonl` but still
holds `wal/`, `seals/`, a committed `periods/<N>/` or a populated `runs/` is
somebody's work. A genesis there would relabel foreign history, or run beside
detached processes still writing into it, so it refuses too and names what it
found. Two directories are excluded, because the launcher legitimately writes
them before genesis: `catalogs/` is content-addressed bundle storage and
`periods/.staging/` holds candidates. For an **anchor**: creating it refuses an
existing `anchor.json`, *even if its incumbent is dead* — an existing anchor is
an existing estate whose detached work may still be alive, and "two geneses
are two estates" never licensed two estates to share one anchor (PR-01b) —
unless it is `open(1, this root, this estate_id)` with a matching sentinel and
no committed segment, which is our own genesis interrupted, and the sole
recovery exception; once a segment exists, ordinary `--resume` owns recovery.

**Native genesis is an ordered transaction** under that rule: `flock`
`leader.lock` → sentinel by the liturgy (create-only) → `anchor.lock` and the
create-only CAS `absent → open(1, root)` → materialize the bundle and
`periods/000001/manifest.json` → write `wal/000001.jsonl` with its `segment`
record → finalize: anchor CAS setting `periods[1].segment_durable = true`. A crash after the sentinel and before the segment leaves a
root that no old binary can use and that a re-run of genesis completes
idempotently, reading `estate_id` back from the sentinel rather than minting a
second (PR-01a). A physical roll's opener writes the sentinel as its first
durable act in the new root, before any import.

`catalogs/` is addressed by **`source_bundle_hash`**, defined normatively:
take the input files **in command-line order**; for each, frame
`len(path) ‖ path ‖ len(bytes) ‖ bytes` where `path` is the original path as
given, UTF-8, `bytes` is the post-placeholder UTF-8 text, and both lengths are
8-byte big-endian counts of UTF-8 bytes; sha256 the concatenation.
Length-framing stops `["ab","c"]` colliding with `["a","bc"]`. **Order is
included, not sorted away**: the loader preserves command-line order,
`CatalogMeta.source_files` records it, and the pinned `catalog_hash` covers the
raw model — reversing two files changes the runner hash — so a bundle address
that ignored order would map one directory to two catalog hashes and one
`sources.json` could not reconstruct both. `sources.json` stores the ordered
vector, and reopening uses it verbatim.

`catalog_hash` is a **different** thing and there are **two** of them in the
code: the runner hash — `period.catalog_hash_v2`, which the journal reaches
through `catalog_hash_at` — and `equiv.catalog_hash`, which strips spans and
annotations first.
Draft 4 said the runner hash collides across byte-different sources; it does
not — the *equivalence* hash does, by design. This spec pins the runner
hash **with one exclusion, and versions it**: `catalog_hash` v2 is sha256 over
the §3.2 canonical form of `CatalogIR` with `meta` projected to
`{source_files}` only — `tool_version` **and** `parsed_at` are diagnostic and
leave; spans stay. The version rides explicitly as `catalog_hash_version: 2` on
`segment`, seal and period manifest, and a hash-v2 golden vector ships with
PR-08. Version 1 is retired: it is refused by name (DL-138,
`docs/protocol-evolution.md`), and no record carries a v1 value beside a v2 one.
The version-1 hash
serialized the whole model, and `CatalogMeta.tool_version` is the installed
package version — reversing nothing but the version string from 1.2.3 to 1.2.4
changes the hash. Under that hash a seal committed by 1.2.3 could never be
opened by 1.2.4, and PR-07's byte-identical openings across a patch release
were impossible. It is also the resume-refusal DL-100 already called *"an
outage manufactured by bookkeeping"* for the SM version, and the argument is
the same. Spans stay in; only diagnostic metadata leaves. This is a change to a
frozen identity (leader eligibility) and takes its own DL entry. The period
manifest binds
`catalog_hash` and `source_bundle_hash` together. A period that reverts to
earlier bytes references the directory already there. Launch options live under
`periods/`, never `catalogs/`.

**Rolling the estate root is optional archival hygiene.** The seal and opening
format are identical whether the next period continues in place or opens a
fresh root (PR-07), so rolling is a deployment choice and never a second
semantic path.

### 1.2 Estate identity

`estate_id` is carried in every `segment` record, every seal and the anchor. A
root whose `estate_id` does not match the seal it is opening refuses. Two
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

**The contract**, substrate-independent, with three head states:

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

Draft 4 defined `closed → claimed → open` and never `open → closed`, so a
committed seal left the head `open(2, A)` and no opener could ever claim it.
The closing transition is the **third write** of the seal sequence (§3):
sidecar durable → `seal` record durable → anchor CAS `open(N, root) →
closed(digest, root, N)`. Recovery repairs the one window this opens — seal
record landed, head still `open` — by performing that CAS on resume (§11 matrix,
PR-45).

`closed` carries **`closing_root`** because a physical roll's opener needs to
find the sidecar, the C2 bundle and the period manifest. **A physical roll
requires the closing period to be attested first**: `run --open-from` refuses
unless `seals/<N>.audit.json` exists in `closing_root` — draft 5 let B import a
seal it could never verify, then required it to audit C1 with none of C1's
inputs. The opener imports `seals/<N>.json`, `seals/<N>.audit.json`,
`catalogs/<bundle>/` and `periods/<N+1>/` into the new root with the liturgy,
writes its first `segment` record, and **then** — in the `claimed → open`
write — registers `periods[N+1].root = B`; nothing registers a successor
before its segment is durable. B is then resumable on its own, and **`verify`**s C1 — a different verb from
`audit`. `audit` is full re-derivation and needs C1's WAL, spool and manifests
(§11); `verify` validates an attestation: its own digest, its binding to the
seal digest, and its place in the chain. B has the second and not the first,
and calling one by the other's name would quietly weaken §11's definition
(PR-02a). **An attestation is a chain checkpoint, by induction and not by assertion.**
Auditing period 1 re-derives from genesis. Auditing period N first **verifies
attestation N−1** and attestation N then records `chain_through_period: N` and
`prev_attestation_digest`. **Producing and consuming an attestation are two acts with two rules.**
*Producing* N (`audit`) requires attestation N−1 **present and verified**;
period 1 is the base case with `prev_attestation_digest: null`. There is
deliberately no "or re-derive everything below" alternative — it left
`prev_attestation_digest` undefined when no predecessor artifact existed;
without that a wrong implementation checks only its own digest and seal
binding, emits a "checkpoint" over an unaudited opening seal, and earlier roots
get deleted on a chain that was never established. *Consuming* N as a
checkpoint (`verify`) accepts N **alone** — its own digest, its seal binding,
and its `chain_through_period` — because the producing audit already
established the induction, and a physical roll imports only the current seal
and attestation. Draft 8 wrote one rule for both and made a second roll
impossible. So C importing seal 2 and attestation 2 while A and B are gone
verifies the chain below seal 2 *because attestation 2 proves it*, and the
recovery matrix's "broken `prev_seal_digest` chain refuses" applies exactly
where no attestation covers the break (PR-02e: producer-negative and
consumer-positive, separately). Re-deriving C1 in B would need C1's whole proof
set; importing that on every roll is retention policy, not a boundary
mechanism; the registry is how a caller who retained it finds it.

The `attested` transition is **owned by `audit`**: it writes `audit.json` by
the liturgy and then, under the anchor lock, sets `periods[N].attested = true`
in one write. And the registry entry for a new period is written **in the same
anchor write as `claimed → open`**, after the first segment is durable — not
before, or a crash leaves an authoritative registry row for a period that has
no segment (PR-02c).

`claim_successor` moves the head `closed → claimed(claim_id, target_root)` as
one compare-and-swap. `target_root` is the **absolute, normalized** path
(`os.path.realpath`) and is persisted that way: a claimant started with
`--run-root ./r` and restarted with `/abs/r` must compute the same `claim_id`,
or an ordinary restart needs break-glass. Execution `run_dir`s are relative to
the estate root for the same reason. It is **idempotent on `claim_id`**: the same
`(seal, next_period, root)` may resume its own claim after a crash — identity is
the claim, not the process; PID, start-time and `boot_id` ride on the claim
file for diagnostics only, because a crashed claimant's replacement necessarily
has a different PID. A different `claim_id` against the same `seal_digest`
refuses, naming the holder. The head moves `claimed → open` when the first
`segment` record of the new period is durable.

Draft 3 moved the head to "`next_period`'s seal digest" — a digest that does
not exist until the next period ends — and keyed the claimant on process
identity, so an ordinary crash between claim and head-move could only be
recovered by `--force`, which is the one operation permitted to fork a lineage.
Both are gone.

**The local implementation** is `LeaderLock`'s pattern on the anchor directory,
because that pattern already solves what a bare `O_EXCL` does not —
replacement and lifetime:

- `anchor.lock` is `flock`ed for the **process lifetime** of whoever leads the
  lineage — the engine in live mode, the sealer in offline mode — and re-checked
  (inode-under-pathname, `LeaderLock.check`) before every append, every
  dispatch, **and every revision-bearing read or subscription response** —
  frozen v2 makes those reads leader-only and stamps lineage coordinates on
  each answer, and a displaced leader that kept answering `status`, `routes`
  and backfill until its next mutation would be serving revisions from a
  lineage it no longer leads (PR-03). Delete or replace the directory and the
  incumbent stops on its next act, on DL-101's bargain: it cannot un-run what happened, it turns a
  divergence into a recorded stop.
- `claims/<claim_id>.json` is the durable claim, written under the held lock
  by the **spool liturgy** — temp, `fsync(file)`, `rename`, `fsync(dir)`:
  `{artifact_format_version, claim_id, estate_id, prev_seal_digest,
  next_period, target_root, claimed_at, diag: {pid, start_time, boot_id}}`.
- `anchor.json` is written by the same liturgy under the held lock and carries
  `{artifact_format_version, estate_id, head: open|closed|claimed,
  periods: {N: {root, segment_durable, seal_digest, attested}},
  reclaimed: [{claim_id, target_root, next_period, claimed_actor, at}]}`.
  `reclaimed` is the break-glass ledger: append-only, never consumed, and
  copied into the next opening `segment`'s own `reclaimed` field, so the fork
  is recorded in the lineage's log as well as in the fence that permitted it. **A period's registry row is
  inserted when a root first owns it, and is provisional until that period's
  first segment is durable**: genesis writes `periods[1]` in its `absent →
  open(1, root)` — before any segment exists, so that row carries
  `segment_durable: false` and every cross-period reader ignores a row until it
  reads `true`; a successor's row is
  written in `claimed → open`, after its segment is durable, and is never
  provisional. Period 1's row flips to `segment_durable: true` in a **finalize
  CAS performed immediately after the segment lands** — a sixth step of
  genesis's own, with a recovery row for the crash between segment and
  finalize (resume performs it) — never deferred to "the close or the first
  resume" of a *running* period, which left an
  uninterrupted engine running a durable period that estate-wide readers were
  told to ignore. `seal_digest` is filled at close and `attested` at audit.
  Draft 15 inserted rows only at `claimed → open`, so period 1 had none and
  estate-wide `audit`, `journal` and `runs` could lose it after a physical roll
  (PR-02f); draft 16 then said both "only at `claimed → open`" and "at first
  ownership". Every head transition — `absent → open`, `open → closed`,
  `closed → claimed`, `claimed → open` — is one liturgy write; the CAS is read-compare-write under the lock. Rename without `fsync(dir)` is not durable: a power loss could keep
  B's first segment and revert the head, and a second target would then claim
  the same seal. Process-kill tests do not prove directory-entry durability;
  §13 says so where it matters (PR-02c).
- **`periods` is the archive registry.** Draft 5 lost `closing_root` the moment
  the head moved on, so estate-wide `journal`, `audit` and `runs` could not
  discover which root holds period N. The registry maps every period to the
  root that holds its segment, seal and attestation, and every cross-period
  reader takes its roots from there. **One walk reads it.** `audit`,
  `journal`, `runs` and `estate prune` each address the whole estate the same
  way — the lineage ANCHOR named where a run root would go — and each takes
  its roots from that one walk, in period order. A row is passed over only
  while it is provisional, and then said out loud; a root the registry names
  that is missing, holds no sentinel, holds one that cannot be read, belongs
  to another estate, or lacks the segment of the period it is registered for
  refuses BY NAME. Four private walks would be four opinions about what a
  missing root means, and the reader that decided "skip it" would answer with
  a smaller estate and no way to tell.
- Local filesystem only. `runner_ledger.py` already says the flock fence is not
  one on NFS; an NFS anchor is refused at startup (PR-04).

**A stale claim is break-glass, not garbage.** A `claimed` head whose root is
unreachable cannot be told from one whose root is paused. Overriding it is
`dsl41 estate reclaim --force`, recorded in the anchor and in the next
`segment` record's `reclaimed` field with the claimed actor — loud, durable,
attributable, and the one path here that can fork a lineage.

**When the shared store arrives** (`ha-deployment.md` S8a) it **replaces** both
this anchor and root leadership as the sole authority: one transaction consumes
`expected_head_digest`, advances the head and allocates the term. Keeping the
file anchor beside the store would be two leadership truths, and
`ha-deployment.md` §2's ACQUIRE gains a lineage-head predicate at that point.

## 2. Records

Three record kinds join `docs/runner-design.md` §7's list — `segment`, `seal`,
`decision` — and three are retired: `header` (a once-per-log header cannot
describe a log made of segments), and `result` plus standalone `effect` (§2.3).

**Retired means refused by name since DL-138.** Nothing writes one and nothing
reads one: a journal that opens with a `header`, or that carries a `result` or
a standalone `effect`, is refused naming the kind and that entry
(`docs/protocol-evolution.md` §6). The current kinds are `segment`, `seal`,
`decision`, `leader`, `input`, `advance`, `host`, `effect_result`, `dispatch`,
`drop` and `preflight` — `host` among them, current and not retired — and each
keeps the shape `docs/runner-design.md` §7 gives it. An **unknown** `rec`
refuses too, naming the kind and as its own distinct error: version gating
happens at the opening `segment`, so an unrecognised kind inside a
version-matched segment is corruption and not a dialect this reader is too old
to see. Skipping it would let a reader walk past evidence and report a complete
replay.

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

There is **no** `catalog_hash_v1` field. It carried an adopted period 1's
legacy hash beside the v2 one; version 1 is a retired dialect (DL-138) and no
`segment` records one.
`dsl41_version` is **not** on `segment`: it is per-process and already rides on
`leader` (`runner-design.md` §7), and keeping it here would break PR-07's
byte-identical openings on a patch-version retry. `reclaimed` and
`trust_unaudited` are `null` or `{claimed_actor, at, …}` — the two break-glass
paths that must leave a durable mark on the period they opened (§1.3, §11).

Every segment is **self-describing**: a reader that opens any segment knows the
period, the catalog and the semantics without reading an earlier file.
`opens_from_seal` is null on segment 1 and non-null on every later segment
(I1: every later segment opens a period). `at` on an opening segment **is T** — the seal's cutoff
instant — not restart wall time; that plus `next_period` committing every
non-derived opening field is what lets two openings of one seal be
byte-identical (PR-07).

**`runtime_hash`** is sha256 over the canonical form (§3.2) of
**`RuntimeProfile`**, a typed frozen model — not an open list:

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
```

Seconds from the CLI convert to microseconds by `round(seconds * 1_000_000)`;
every duration is present with its resolved default and validated to the
constraint shown — a negative grace or a zero poll interval is refused at
construction; nothing is null except `deadman_us`; `default_tz` absent means
`"UTC"`; `as_machine` is de-duplicated and sorted. The `artifact_format_version`
lives on the **manifest** that carries the profile, not inside the profile —
two version fields with no equality rule were two authorities. PR-15a is the
CLI→profile normalization obligation: omitted and explicit defaults, `None →
UTC`, `local-eligible`, duplicate `as_machine`, fractional seconds, and each
duration tested against **its own stated bound** — zero is legal for the two
`>= 0` windows and refused for the `> 0` intervals. There is **no** `machine_map`: draft 10 named one and it could
only be mistaken for the mutable role→executor route table, which is carried
state and not identity. PR-15's cases derive from these fields, and a
runtime-hash golden vector (PR-08c) pins the bytes. Draft 9 said "every other launch option that changes
interpretation or dispatch" and PR-15 tested the ones it named — so a hash
that omitted `grace_seconds` passed while a patch quietly changed how a live
C1 command is killed under C2. PR-15's cases are **derived from the model's
fields**, the DL-83 discipline: a field added later is tested by default. There
are **two manifest models**, on the same line as `StagedNextPeriod` /
`CommittedNextPeriod`: the CLI stages `staged_manifest.json` —
`{artifact_format_version, catalog_hash, catalog_hash_version,
source_bundle_hash, runtime_profile: RuntimeProfile, runtime_hash,
state_machine_version}`, nothing the engine owns — and the engine writes
`manifest.json` at install — the staged fields plus `{period_id, baseline_id,
clock_domain, segment_no, first_index}` — keeping the staged file beside it.
"Validates exactly the staged bytes" is about the bundle and the staged
manifest; the committed manifest is the engine's own output and is checked
against the committed `next_period` at resume (PR-22). Identical JIL launched
`--timezone UTC` and then `--timezone Europe/Zurich` has an unchanged
`catalog_hash` and different UTC ticks; without `runtime_hash` classification
would report nothing changed.

**Not** in `runtime_hash`: the affinity route table. `ha-deployment.md` §4
makes role→executor a mutable authoritative row revised under epoch/CAS, and
says a remap is *not* a re-baseline. It is carried state (§3.3), not period
identity. Draft 2 put it in the hash and contradicted the HA plan.

A period's semantics are `(catalog_hash, runtime_hash, state_machine_version)`.
Either of the first two moving is a new period — so a runtime-profile change
with no catalog change is a transition.

**`state_machine_version` may not change across a transition in this spec.**
Draft 5 allowed it and required the sealer to dry-run `open_from_seal(C2)`
under the new version — but one executable implements exactly one
`STATE_MACHINE_VERSION` and refuses any other, so a v1 engine cannot dry-run v2
and a v2 binary cannot lead or replay C1. Draining every executing job does not
create a state translator, and nothing proves carried timers, latches, globals
or capacity are valid under v2 semantics. So: `next_period.state_machine_version
== seal.state_machine_version`, enforced by the readiness gate (PR-17), and an
SM bump remains what it is today — a full drain and a new estate. That is no
regression: it is the status quo, and SM bumps are deliberately rare (DL-100:
the package version moves for a typo; the SM version moves only when the
derivation does). DL-138's evolution contract makes the rule explicit and
permanent: a semantics change is a full drain and a new-estate genesis, and no
extension lifts it (`docs/protocol-evolution.md` §1).

### 2.2 `seal` — the last record of a period's last segment

```json
{"rec": "seal", "estate_id": "…", "period_id": 2, "closes_at_index": 5310,
 "at": "2026-08-19T02:00:00.000000", "digest": "sha256:…",
 "next_period_id": 3, "next_baseline_id": "…", "catalog_hash_version": 2,
 "source": "request", "request_id": "…", "request_fingerprint": "…",
 "claimed_actor": "alice@ops-laptop", "force_seal": false}
```

The record is the **commit point** — and it is the boundary's **decision**,
which is what makes a live seal request answerable at all. A `seal` request
over the control socket cannot be decided by an ordinary `decision` record:
before the seal, a crash leaves a durable "applied" for a boundary that never
happened; after it, records after a seal are forbidden; and not at all leaves a
lost response unretryable despite the promised `request_id`. So the `seal`
record carries the request's `request_id`, its **fingerprint over the complete
envelope** — `source`, `baseline_id`, `epoch`, `next_period`, `force_seal`,
`claimed_actor` — and the `claimed_actor`, on `concurrency-model.md` §6's own
rule that the fingerprint is the whole semantic envelope. Two requests with one
`request_id` and one `next_period` but different `force_seal` or actor
**collide**; force is an authorization and the actor is attribution, and
neither may be swapped under a retry. An exact retry is answered from the
committed seal, in the new period, without touching the decision index. **A committed
seal's retry has its own route, ahead of the baseline gate.** The generic v3
parser rejects a foreign `baseline_id` before it reads `request_id`, and a
retry of the seal that closed C1 necessarily carries B1 while C2 answers under
B2 — so without a dedicated route the promise above is unreachable. The engine
of period N+1 keeps the `seal` record it opened from and checks an incoming
`seal` request's `(request_id, fingerprint)` against it **before** the
current-baseline check; a match is answered from that record. The lookup
reaches exactly one seal back: a retry of an older seal is refused as a stale
baseline, which is a liveness loss and not a safety one (PR-30e). **An
uncommitted seal request is unseen.** The `seal` record is the boundary's
commit, and only a *committed* seal is consulted by the retry route; the sidecar
also carries the request identity in `boundary_request` — because a physical
roll imports the sidecar and not the old WAL, and because an attested
predecessor WAL may lawfully be pruned, so retry evidence that lived only in
the WAL vanished exactly when PR-02a said it might (PR-30e) — but an orphan
sidecar is ignored by rule, so its copy of the identity names nothing until the
record lands. So a request that crashed before its record
left nothing behind, its retry is a fresh request that attempts the seal again,
and only a committed seal is ever deduplicated. Sealing twice is impossible: a
second attempt finds the first's record if it landed. Draft 9's PR-30a asked
for a pre-commit retry to be "refused as unknown", which no record could
support and which confused two protocol outcomes.

The v3 request and answer:

```json
{"cmd": "seal", "v": 3, "baseline_id": "…", "epoch": 7, "request_id": "…",
 "next_period": {…StagedNextPeriod, §3.4…}, "stage_digest": "…",
 "force_seal": false, "claimed_actor": "…"}

{"ok": true, "kind": "seal", "decision": "applied", "period_id": 2,
 "digest": "sha256:…", "next_period_id": 3, "next_baseline_id": "…", …header…}
```

**The route wire, frozen with v3 — and not yet built (below).** `Attempt.host` becomes a discriminated
union `HostCommand | RouteCommand`; on the wire it is the existing `host` cmd
with a fourth verb:

```json
{"cmd": "host", "v": 3, "baseline_id": "…", "epoch": 7, "request_id": "…",
 "verb": "route", "payload": {"id": "<role>", "executor_id": "…"},
 "expect": {"route:<role>": 3}, "claimed_actor": "…"}

{"cmd": "routes", "v": 3, "roles": ["<role>", …]}
→ {"ok": true, "routes": {"<role>": {"present": true, "executor_id": "…",
    "state_rev": 3}}, …header…}
```

The `routes` query keeps the `hosts` query's corners: an absent role answers
`{"present": false, "state_rev": 0}`; omitting `roles` answers the whole
table, because the takeover barrier reconciles every route as it reconciles
every host. The
subscription gap record (§11) is `{"gap": true, "earliest_retained": <index>}`.

The WAL record is `host: {verb: "route", id, executor_id}` (§3.3),
and the answer is the same decision shape and four outcomes as every host
verb. Two competent implementations could otherwise choose incompatible JSON
for one fact. **The wire is specified and not yet built**: neither the
`RuntimeState` storage, the `route` verb, the `routes` query nor the `route:`
`expect` namespace exists today, and the shipped table is the one implicit
row §3.3 describes. This section is what the unit that adds the storage
implements; it changes the producer, never the seal artifact.

`expect` is absent by design on the **seal** request: a seal addresses no row. **`request_id`
collides across the whole period, not only seal-to-seal**: readiness checks
the seal request's `request_id` against the current period's `DecisionIndex`
and refuses a reuse under a different fingerprint, on `control-protocol.md`
§3's own rule — one `request_id`, one command — so an ordinary `STARTJOB` R and
a `seal` R cannot both name authoritative decisions (PR-30c). Refused / rejected /
unknown keep `control-protocol.md` §3's meanings; the fourth outcome, a retry
that finds the seal committed, is `applied` from the new period. This is what closes the window where the
seal commits, the socket drops as the engine exits, and the client holds
`unknown` with no durable key to ask about (PR-30a).

The record duplicates the fields recovery needs to select the sidecar and
refuse a wrong one — `digest`, `period_id`, `closes_at_index`, `at`,
`next_period_id`, `next_baseline_id` among them — and §11 requires **every**
duplicated field to agree, derived from the record shape above rather than
from a list kept beside it, so a field added here is compared for free.

### 2.3 `decision` — one atomic batch

```json
{"rec": "decision", "index": 5310, "request_id": "…", "decision": "applied",
 "reason": null, "revisions": {"job:nightly": 13},
 "legacy_batch": false,
 "effects": [{"effect_id": "e5310:KILL:nightly.7", "kind": "KILL",
              "job": "nightly", "run_number": 7, "run_id": "…", "index": 5310,
              "executor_id": "local", "generation": 0, "at": "…"}]}
```

`concurrency-model.md` §4 step 7 requires the decision, revisions, outbox
entries and `applied_index` to commit **atomically**, and before DL-118 the
code did not: `result` and each `effect` were separate `_write` calls, each
fsyncing on its own (`ha-deployment.md` §2 recorded the violation). This is the file-substrate answer:
one line, one fsync, on the argument `Journal.admit` already makes for the input
side. Without it a real window is invisible to every precondition: the result
is fsynced, the process dies before the KILL effect is written, recovery finds
every attempt decided and an empty outbox, and the terminal row's command is
still alive.

`effects` is emitted in **admission order** — `Outbox` treats insertion order as
admission order, and a SPAWN must precede a later KILL for the same run. Every
effect carries `{executor_id, generation}` from birth: `ha-deployment.md` §4
resolves affinity **inside** the effect-intent transaction and forbids a remap
from moving an existing effect, and an effect without a generation cannot prove
at dispatch that it did not read a newer one (PR-16). **Every SPAWN effect also
carries `run_id`, minted in the same transaction.** Before DL-118 the adapter
minted it when its task started — after the durable effect — so an engine that
died between the supervisor writing R1's index and the engine recording the
outcome resumed with a pending effect and no memory of R1, and re-dispatch
minted R2 unless recovery invented a spool-lookup rule. `concurrency-model.md` §5 always
said `run_id` is bound before the attempt; DL-96 deferred it *"until the relay
needs it"*; the seal needs it first (PR-36a). One key then runs through the WAL,
the supervisor index, the receipt and the retry, and `(job, run_number) ↔
run_id` is one-to-one by construction. `legacy_batch` is on **every** decision
and is **required false**. `true` is a retired dialect — it named a batch
folded from separate legacy fsyncs — and a record carrying it is refused
naming DL-138. Missing, or present with a non-boolean value, is **malformed**
and refused as its own distinct error: an absent flag is not a false one, and a
reader that defaulted it would accept a record no writer of this estate wrote.
The three cases are decided at one validator, so every consumer that parses a
`decision` inherits them.
`index`, not `seq`, exactly as the retired `result` carried it: `seq` is the
subscribe cursor and a decision shares its attempt's number (DL-89). `decision` is an unsequenced,
at-least-once record on the subscribe stream, as `result` was.

**This is a wire break.** `control-protocol.md` §5 promises raw `result`,
`effect` and `effect_result` records to subscribers, and a v2 client waiting on
`rec == "effect"` silently stops seeing intents. That is not additive. The
protocol goes to **v3**, on the precedent DL-90 set — v1 was gone, not
deprecated — because a compatibility projection would be a second record shape
for one fact. `effect_result` is unchanged.

There is deliberately **no transition record**. A period opens because a
`segment` says so and closes because a `seal` says so; the seal's
`next_period` (§3.4) is what commits the opening.

### 2.4 `leader` and the epoch

Unchanged in shape. Allocation reads the log, and now the seal with it: the
next term is one past the highest `leader` epoch in the segments after the
seal, and never below `seal.epoch + 1`. I2 makes
the epoch estate-monotone, so a new period's first term is `seal.epoch + 1`.

## 3. The seal artifact

Three writes, in this order — the order is the whole durability argument:

1. `seals/<period_id>.json`, with the liturgy the spool uses: same-directory
   temp file, `fsync(file)`, `rename`, `fsync(dir)`.
2. the `seal` record, appended and fsynced as the final record of the segment.
3. the anchor CAS `open → closed` (§1.3).

Crash between 1 and 2 and the sidecar is **orphaned**: no record names it,
recovery ignores it, the period is still open. Crash between 2 and 3 and the
seal is committed but the head still says `open`: resume performs the CAS. The
record landing means the sidecar is already durable; the head moving means the
record is.

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

`next_period` commits **every non-derived opening field** — including
`clock_domain` (resume already refuses a domain change and the opening must be
able to as well), `segment_no`, and the target period's
`artifact_format_version`, so `stage_digest` binds it and two staged manifests
differing only in that field cannot share a digest — so an in-place opening
and a fresh-root opening cannot choose differently (PR-07). There are **two frozen
models**, not one, and the line between them is **who may say it**:
`StagedNextPeriod` is what a client may propose — `catalog_hash`,
`catalog_hash_version`, `source_bundle_hash`, `runtime_hash`,
`state_machine_version`, `artifact_format_version` — the identity of *what*
opens next; `CommittedNextPeriod` adds what only the engine may derive —
`period_id = current + 1`, `segment_no = period_id`, `baseline_id =
sha256(canonical{estate_id, period_id, stage_digest})`,
`clock_domain = current` (a domain change is refused, as resume refuses it
today), and `first_index = closes_at_index + 1`. **`baseline_id` is derived,
not minted**: audit must reproduce every seal field from the opening seal, the
WAL, the spool and the manifests, and a random UUID appears in none of them —
a wrong audit could only copy it from the seal being audited and check its
shape, so a consistent mutation across sidecar and record would pass. Derived
from pre-boundary evidence it is reproducible and still unique per boundary
(PR-47d). Draft 19 also folded in the request fingerprint, which includes the
epoch and actor: a same-stage retry after a crash runs under epoch+1, so its
fingerprint differs, its required baseline differs, and the installed
candidate's committed manifest carries the old one — unopenable or
inconsistent. `{estate_id, period_id, stage_digest}` already names the only
boundary that can open there; nothing else belongs in it. Draft 17 let the
client stage `period_id` and `segment_no`: period 2 could open period 4, and
attestation 3 — which the induction requires — could then never exist, an
unauditable lineage by construction (PR-05c). `stage_digest`, `candidate.json` and the request `fingerprint`
are over the first; the sidecar's `next_period`, the `claim_id` and the opening
`segment` carry the second. One type would force `first_index` to be omitted
(breaking the every-field-present rule), null (not what "excluded" means) or
guessed (D13's bug back again); PR-08e is the golden vector for both.
**`first_index` is not staged.** It is *derived boundary output*: `closes_at_index + 1`, and
`closes_at_index` is unknown until the cutoff barrier has admitted every tick
due at T and fired every timer through it. Draft 13 let the client stage
`first_index = 101` before the barrier ran; a cutoff tick then took index 101,
the seal closed at 101, and C2 opened reusing it — I2 broken and every cursor
and decision lookup ambiguous. The engine computes `first_index` after §6 step
6, writes it into the sidecar's `next_period` and the opening `segment`, and
`stage_digest` excludes it (PR-05b). **`boundary_request` is
authoritative input in three of its four fields** — `{source, request_id,
claimed_actor, force_seal}`, where `request_id`,
`claimed_actor` and `force_seal` originate in the request and nowhere else
*(DL-148: on an access-armed estate the perimeter has already overwritten
`claimed_actor` with the authenticated spelling before the request reaches
this tier — `docs/access-model.md` §3; this tier still reads the request
and nothing else)*,
and `source` is `"request"` — **one value since DL-138** — which audit checks
for equality between the record and the sidecar (§11). A live seal through the
control socket and an offline seal from the CLI are the same kind of boundary —
a request carrying an id its
caller minted — and draft 24's `control | offline` split asked audit to tell
them apart by a `leader` record that carries epoch, time, pid, host and
version and nothing that names the process's mode. The second value the field
once had, `adopt`, went with the estate-adoption path, and with it the derived
adoption `request_id`. `request_fingerprint`
is **derived** over the envelope and audit recomputes it; `forced_gate` is
**gate output** — `null`, or `{"gate": "retry_horizon", "horizon_us": …,
"observed_age_us": …}` — and audit re-derives it from the profile's
`retry_horizon_us` **of the closing period's committed manifest** — C1's,
because the gate protects retries of requests admitted under C1; the staged
C2 profile has no say — the WAL's last admitted **externally requested
attempt with a durable decision**, and T. "Attempt", not "mutation": the
frozen exact-retry promise covers a journaled *rejection* and an applied no-op
as much as a state change, so a `rejected` CAS loser two seconds ago must hold
the gate exactly as an applied `STARTJOB` would. The truth table: observed age ≥ horizon, or no externally requested attempt with a
durable decision in the period (age = ∞) → gate passes, `forced_gate: null` whatever `force_seal` says; age <
horizon and `force_seal: false` → refuse; age < horizon and `force_seal: true`
→ commit with `forced_gate` populated. An unnecessary `--force-seal` is
recorded in `boundary_request.force_seal` and engages no gate. Draft 21 put all three in one
excluded block, so a consistent rewrite of the fingerprint or the observed age
passed audit (PR-47b). "Claimed actor", not "principal": this tier does no
authentication of its own — the name records what the request carried, and
the seal must not spell that claim as if this tier had proved it. *(Amended
by DL-148: the local-authentication half of `control-protocol.md` §7 gap 2
closed with DL-146 — on an armed estate the carried value is the
perimeter's authenticated spelling; on an unconfigured estate it stays the
caller's bare claim.)* `classification` records the
§10 verdict and every A assumption; draft 2 promised "assumption recorded" and
gave it nowhere to live.

### 3.2 Canonical form (normative)

The digest is computed over a canonical serialization, never incidental JSON
bytes; otherwise `audit` reports mismatches for a re-serialization that changed
nothing.

- JSON, UTF-8, `ensure_ascii=false`, separators `(",", ":")`. **Strings are
  Unicode scalar values.** Python's decoder accepts `"\ud800"` — an unpaired
  surrogate — as a string, the control server **once** accepted any string as a
  global value, and the journal writes one safely under ASCII escaping;
  encoding it later with `ensure_ascii=false` raises. One legal control input could make
  the estate unsealable. So every ingress — the control socket, catalog
  loading, spool decode — refuses a non-scalar string, and canonicalization
  never meets one (PR-10a).
- Every object's keys sorted by Unicode code point, at every depth.
- The value grammar is object, array, string, integer, boolean, null. **No
  floats at any depth.** `deadman_s` is a float in `HostRuntime`; its
  canonical form is `deadman_us: int | null`, which is also what
  `RuntimeProfile` already stores.
- Datetimes as ISO-8601 naive UTC with **exactly six fractional digits**,
  zero microseconds included. The rule governs the values this form encodes,
  and a schema field that stores a timestamp as a **string** carries the same
  spelling — `claimed_at`, `audited_at`, `archived_at`, a `reclaimed` entry's
  `at`. It does not reach the WAL, which is not one of the artifacts below.
- **Typed schema fields are always present** — explicit `null` for an unset
  optional, `[]`/`{}` for an empty collection. Default-filling happens **only at
  typed schema boundaries** (`JobRuntime`, `HostRuntime`, `Effect`, the seal's
  own top level) and **never inside opaque JSON**. `Event.payload` is opaque:
  `{}` and `{"x": null}` are different values and must digest differently. A
  canonicalizer that "drops empty or default values recursively" is
  non-conformant.
- Semantically unordered collections sort by a stated key: `reservations` by
  `bucket` (after duplicate-bucket rejection), `ran_members` and every other
  set by value. Ordered collections keep their order: `timers` by
  `(due, token)`, never heap-array layout; `outbox_pending` and `executions` by
  `(index, effect_id)`.
- Duplicate object keys are **rejected at decode**.
- Escaping is pinned: `"` and `\` escaped; `\b \f \n \r \t` by short form;
  every other **Unicode Cc** character — U+0000–U+001F, U+007F, U+0080–U+009F —
  as `\u00xx` lower-case (DL-128 fixed the set; "control character" alone was
  read two ways at build); `/` never escaped; nothing else escaped.
- `digest` is `"sha256:" + hexdigest` over the canonical bytes with the
  **top-level** `digest` key removed — only that one. A nested opaque payload
  key named `"digest"` is data and stays; a recursive "strip every digest key"
  implementation would collide documents that differ there (PR-13).
- **A digested artifact's stored bytes ARE its canonical bytes**, and its
  reader asks that first — the seal sidecar, the attestation and the archive
  receipt. A whitespace-padded copy, a key-reordered copy and a copy that omits
  a defaulted key all carry the real artifact's digest. Each one passes every
  later check. This rule is what separates the artifact from its look-alikes,
  and each reader names its own artifact when it refuses.

**A golden vector ships with the spec** — one document exercising control
characters, `/`, non-ASCII, nulls, defaults, empty and non-empty nested
payloads, an array whose order matters, and six-digit datetimes — with its
canonical bytes and digest fixed in the test suite (PR-08). Equality and
sensitivity tests alone would pass a canonicalizer that is consistently wrong.

**Canonicalizability is a liveness property.** The grammar refuses floats at
write time. If any `Event` payload the oracle can enqueue as a timer contained
one, the estate could not be sealed while that timer was armed. Every timer
payload the oracle constructs is canonicalizable, and that is an obligation
(PR-09), not an assumption.

**One shared `artifact_format_version`** governs this section and every
artifact this spec defines — the seal sidecar, the attestation, the period
manifest, `staged_manifest.json`, `candidate.json`, `sources.json`,
`receipt.json`, `reply.json`, every `watch.jsonl`
line, the `run_id` index entry, `anchor.json`, the claim file, the sentinel,
and the archive receipt (§12a)
— each carrying the field, each refused when it names a version this binary
does not implement (PR-08d), and each digested, where digested, over its
canonical bytes with only its top-level `digest` removed. Draft 10 promised a
version per artifact and gave several none; a canonicalization change moves
the bytes of all of them at once, so one version is the honest count.
`seal_format_version` is retired into it. `docs/protocol-evolution.md` §1 puts
every artifact named here on a compatibility row and states its lifetime.
An artifact serialized by incidental
`json.dumps` settings passes a same-binary test and fails after a patch changes
serialization; the golden vectors (PR-08, PR-08a, PR-08b) exist for exactly
that.

### 3.3 Carried and not carried

| carried | why it cannot be reconstructed |
| --- | --- |
| `jobs` (incl. `reservations`, `waiter_seq`) | authoritative rows |
| `globals` | authoritative rows |
| `hosts`, with `last_contact` **omitted from the shape** and `deadman_us` **present and null** | durable routing state (`concurrency-model.md` §8) — see the not-carried row for the two exclusions. **An evicted host's return is not this spec's.** Draft 19 named an admitted `host{verb: register}` input for it, and naming it was the mistake: a returning host must present its generation, prove it self-fenced (CM-12), and be reconciled against two frozen rules — a stale generation is refused, ordinary re-registration preserves operator state — and none of that has a producer before the relay exists (DL-97). On one host today an evicted row is a dead end: `evict local` leaves `local` routing nothing and nothing brings it back, because the un-evict is the relay's act. So this spec records **nothing** for a host's return, carries an evicted row as it stands, and leaves the register record, its proof and its transition table to the HA track where the relay is built. Registration stays unjournaled here: the genesis seed is identical on every replay, and a deadman refresh is unprojected (PR-24c) |
| `routes` | the role→executor table, authoritative under CAS (`ha-deployment.md` §4); today one row — and a **row like the other three**: `RouteRuntime {executor_id, state_rev}`, frozen, owned by `RuntimeState`, projected on the same rule, read through a v3 `routes [roles]` verb answering `{present, executor_id, state_rev}` per role, addressed by the fourth `expect` namespace `route:<role>` — **the storage and that verb are specified and unbuilt (§2.2)**, so today the table is projected as one row whose role IS the local executor's id, at revision 0, and the seal carries it in the frozen shape. **A route names an executor and nothing else.** Drafts 15–18 gave the route a `generation` and then spent four rounds on what a route whose generation had gone stale meant — and the answer was always "the evicted-host case", which §8 defines and the HA track builds, and which this spec has no business re-defining. So the generation is **not** on the route: at effect birth `executor_id` comes from the route and `generation` from the host row's **current** value, exactly as `plan_effects` binds today; a stale route cannot exist; an evicted host routes nothing, so an effect born for one is held pending by the routing gate as today; and §8's re-drive-as-new-run stays where it is, unbuilt until HA's relay, named here as out of scope. **A remap is an admitted input** on the `host` record's pattern (DL-94): `host: {verb: "route", id: <role>, executor_id}`, applied to the owner, no oracle event, rejected if `executor_id` names no host row. A→B→A moves the revision twice and the seal carries it (PR-16b) |
| `timers` + `timer_seq` | an armed deadline is state no status field records; the token carries cross-job firing order |
| `consumed` | irreversible depletion (DL-50) that no row holds — §5. **Keys survive their resource**: a `consumed["r:FUEL"]` whose resource C2 removes is retained as a ghost bucket, and if C3 reintroduces `FUEL` its consumption is still spent — a loader that rebuilt capacity from the catalog alone would silently refund it on reintroduction (PR-19a) |
| `enqueue_counter` | the waiter-rank allocator's high-water mark |
| `now` + `clock_domain` | feed times must be non-decreasing across the boundary |
| `scheduler_admitted_through` | which same-instant ticks were consumed — §6 |
| `outbox_pending` | intents recorded and not delivered |
| `executions` | the lifecycle state of every non-terminal run — §3.5 |
| `classification` | the transition's verdicts and A assumptions |
| `next_period` | the opening this boundary commits — §3.4 |
| `estate_id`, `epoch`, `prev_seal_digest` | lineage |

| not carried | reason |
| --- | --- |
| `last_contact` | outside the semantic projection (DL-95); replay re-seeds it so a new leader **over-waits** rather than evicting early. A stale one lets the new period conclude a quarantined host's deadman expired — the one state that permits a double run |
| `deadman_us` on a host row | DL-95's other half: *"read back from the host, never declared by the leader."* Carry it and C2 can restart the supervisor at 120s while the row still says 60s, and eviction is permitted 60s before the supervisor's real kill bound — a double run. The row's deadman is **null until the host re-registers in the new period**, and a host with a null deadman is not evictable except by force, which is the safe direction. `runtime_hash` carries the *requested* value; the row carries the *observed* one, and only the observed one may enter the bound (PR-24a). **And it leaves the host semantic projection**, joining `last_contact` in `_UNPROJECTED_HOST`: today `register_host` changes `deadman_s`, which is projected, and startup registers with no journal record — so re-registration moves the row's `state_rev` and audit, replaying from a seal that says revision 5, cannot derive the 6 the next seal carries. It is observed liveness configuration, not semantic state; nothing an operator holds an `expect` against depends on it; and the eviction gate reads the current row value regardless of revision. A `concurrency-model.md` §3 change, with its DL entry (PR-24b) |
| the decision index | `_by_index` is log-local; §9 handles retries |
| `unresolved` / `outcome_unknown` | a projection; derives from the bound executor's quarantine (`hosts`) plus the absence of evidence (`executions`), both carried |
| `_trace`, `_emitted`, `_queue`, `_in_wake` | transient or derived |
| `_dispatched` | **derived, and its reconstruction is normative**: `{job: run_number for every row with run_number > 0}`, exactly as `runner_startup.py` seeds it at resume. It is not a cache — `plan_effects` plans a SPAWN only when `run_number > _dispatched[job]`, so an opener that left it empty would let a legal `CHANGE_STATUS STARTING` on a job that completed run 7 plan run 7 **again**, and once the SPAWN tombstone is lawfully pruned, execute it (PR-18a). `open_from_seal` step 5 rebuilds it |
| `_referencers`, `_bucket_cap` | derived from the catalog |
| `Scheduler._next`, `_CalCache` | replaced by the §6 watermark |
| `spec_drift` | disk state |

### 3.4 `next_period` — the seal commits the opening

A seal that named only the closing period's facts left recovery unable to know
**which** period to open when the process died between the `seal` record and
the next `segment`: the operator restarts with C3, and recovery opens C3 from a
boundary that committed C2. So `next_period` is chosen and durable **before**
the seal commits, and recovery opens exactly that. A `segment` whose period
identity disagrees with the preceding seal's `next_period` is refused.

### 3.5 `executions` — a discriminated lifecycle, not one row

`outbox_pending` holds intents not delivered. An applied SPAWN for a still-live
run is not pending, so a seal that carried only the RUNNING row lost `run_id`,
`executor_id`, `generation` and the spool binding, and resume could not say
which executor owned the run. One row shape does not describe the states the
code has, so `executions` is a **discriminated union**:

| kind | when | fields |
| --- | --- | --- |
| `pending_spawn` | SPAWN recorded, not delivered | `{job, run_number, effect_id, index, run_id, executor_id, generation}` — `run_id` from the effect (§2.3), not from an adapter |
| `bound` | SPAWN applied and the spool binding known | `{job, run_number, effect_id, index, run_id, executor_id, generation, run_dir}` — `run_dir` **relative to the estate root** |
| `fw_watch` | a live FW run | `{job, run_number, effect_id, index, run_id, watch_seq, previous_size, stable_polls, next_poll_at}` — `run_id` from the effect like every execution; no process behind it. Its run directory holds only `watch.jsonl` |

Every execution's `run_id` is the effect's (§2.3); every `effect_result` that
carries a `run_id` **must equal** it, and `open_from_seal` refuses a
disagreement (PR-22). `start_period` lives on `JobRuntime` (below) and on
**no** execution entry — draft 9 had it in both places, which is two
authorities for one fact.

**`start_period` is on the row.** `JobRuntime.start_period` is set by
`start_run` beside `run_number` and `started_by`, so it covers CMD, FW, a SPAWN
pending across several periods, and a **box** — which has no execution entry
at all and can be live across several unchanged periods.

**There is no applied-but-unbound kind, and the gate forbids the state.**
`_apply_spawn` creates the adapter task and records `effect_result{applied}`
immediately; the task creates `run_dir` and the supervisor writes the binding
afterwards (the `run_id` itself is already on the effect, §2.3), and in the
real-domain loop `_settle` returns without yielding, so a queued seal can
observe a SPAWN that is applied but not yet bound. Rather than invent a fourth
kind with a defined recovery, §8 requires **every applied CMD SPAWN to be bound
or terminal** before the seal commits — it is milliseconds, and the sealer waits
(PR-27).

**There is no `terminating` kind.** Draft 3 carried one and simultaneously
required the seal to refuse while a KILL ladder lacked proof — two obligations
no implementation could both pass. The gate wins: an unresolved KILL ladder is
a few seconds of `grace_seconds` plus a signal, and the sealer **waits it out**
rather than snapshotting a half-run ladder whose remaining grace deadline it
would then have to carry. What survives of the finding is a **pre-existing
resume gap** this spec merely made easier to reach: `_apply_kill` records
`applied` when the cancellation is delivered and the TERM/grace/KILL ladder runs
on the way out of the task, so an engine that dies mid-ladder leaves a live
wrapper under a terminal row, and resume re-drives only *pending* KILLs. That is
a `runner-design.md` §7 amendment with its own obligation (PR-33): at resume, a
live wrapper under a terminal row is re-driven **regardless of the KILL effect's
recorded state**.

`fw_watch` exists because the FW adapter's progress — last observed size and
stable-poll count — decides when the watch completes, and a restart resets
both. Carrying it keeps an unchanged watch's behaviour identical across the
boundary (PR-34). **But it must be evidence, not memory.** Draft 4 carried
progress held in a local variable fed by unjournaled `os.stat` calls, so an
audit replaying the START input could not derive whether the seal should say
`previous_size=10`, `null`, or a completed watch. So the FW adapter gains a
**spool**, and it is **append-only**: `runs/<job>.<run_number>/watch.jsonl`,
one line per poll — `{artifact_format_version, kind: "poll", at, run_id,
exists, size, qualifying, stable_polls}` — fsynced per line, including polls
that changed nothing. Draft 5's single overwritten
`watch.json` failed twice: `next_poll_at` moves on every poll while the file
did not, so audit could not reproduce it; and a C2 observation overwrote the
value at T, so a later audit of C1 saw C2's evidence. With a log, the seal's
`fw_watch` is a pure function of a **prefix**, and the prefix is named by
`watch_seq` — the count of durable lines at T — not by wall time, because
`at ≤ T` is not a unique log position. Three rules make the log evidence:

- **write-ahead per poll, under the fence**: observe → **re-check the anchor
  fence** → append the line and fsync → *then* update progress or emit
  completion. An observation that changed progress before it was durable is
  one audit cannot see; and an append after leadership was lost is evidence
  written by a non-leader. The fence otherwise lives in the journal writer,
  so `AdapterContext` carries one for the FW adapter, and PR-03 replaces the
  anchor between an observation and its append;
- **a seal barrier**: at §6 step 2 the engine parks every FW task at a poll
  boundary — it awaits any poll in flight and forbids further C1 appends —
  before T is chosen. Otherwise a second qualifying poll can land after the
  snapshot and its completion is never admitted because the engine exits, and
  audit derives a completed watch where the seal carries a live one;
- **a torn final line truncates**, exactly as the WAL's does.

The adapter's **first durable act on dispatch is a `start` line** —
`{artifact_format_version, kind: "start", at, run_id}` — before its first poll, so a dispatched watch always has
`watch_seq ≥ 1`; a watch not yet dispatched is a `pending_spawn`, not an
`fw_watch`. `next_poll_at` is then exactly: **after `start` and no poll line,
`start.at`** (the first poll is immediate); **after a poll line, `poll.at +
interval`**. PR-34 asserts those two timestamps directly, not through the
helper that computes them.

**The `start` line is also FW's resume evidence**, and §11's ladder gains the
rule: a pending FW SPAWN whose run directory holds a `start` line carrying the
effect's `run_id` is **resolved as applied by that line** — the watch is
reconstructed exactly once from the log, and no second `start` is ever
appended. Without this the shipped ladder treats a pending SPAWN with a run
directory as an applied-SPAWN candidate, looks for `spawn.json`, finds none,
and re-launches the watch from its directory or as an untraced start — two
`start` lines, an undefined fold, and a seal nothing can reproduce. The
window is real: the decision commits, the adapter appends `start`, the engine
dies before `effect_result{applied}`. Its sibling — a completing poll appended,
engine dies before the STATUS input is durable — resumes to a log whose last
line is a completing observation and a row still RUNNING; the ladder injects
the completion from the log exactly as it injects a CMD's from `status.json`
(PR-34a). Draft 8 derived
the first poll from the STARTING row's `status_at`, which for a SPAWN pending
on a passive host precedes actual dispatch by hours. C2's lines append after
`watch_seq`. `spawn.json` and `status.json` are immutable by
construction; `watch.jsonl` is immutable by being append-only.

That settles what audit reproduces from what, and §11 says it plainly: `state`
reproduces from the opening seal and the period's **inputs**; `executions` and
`outbox_pending` reproduce from inputs **plus spool evidence** — `spawn.json`,
`status.json`, `watch.jsonl`. `dispatch` records carry no `run_id`; the spool
always did.

`start_period` on the row is what lets run history keep a boundary-spanning run
under the catalog it started in (PR-50).

**Dry-run loading validates the join, one way, over dispatchable rows only.**
Every `executions` entry has a RUNNING or STARTING **CMD or FW** row; a
RUNNING or STARTING row **may** lack an entry only when reconciliation proves
there is no intent, no spool evidence and no live process behind it — which is
exactly what a `CHANGE_STATUS STARTING` overwrite produces: frozen parity lets
it rewrite the row without launching anything, the shipped test pins "stays
STARTING forever, no live task", and such a row is safe to carry. Draft 14
demanded a two-way join and would have refused a legal estate (PR-22a); every `outbox_pending` SPAWN has a `pending_spawn`
counterpart; `reservations` on a row agree with its entry's `run_number`. A
RUNNING **box** has no adapter, no effect and no entry — boxes are deliberately
outside `dispatchable` — and the loader must not reject an estate for having
one live.

*(Amended by DL-151: where the CMD-or-FW half is asked.)* Which rows are
dispatchable is a question about C2, and the seal artifact does not carry a
catalog: the sidecar's own validation therefore refuses only what the
artifact can refute — a missing row, a row that is not live, a run number
that disagrees — and the **resume path** asks the rest, over the seal it is
about to open from, before the successor's segment is written. An entry
naming a box, or a job the opening catalog does not define, refuses there.
Deferring it without an asker is what let a forged sidecar naming a live
BOX row pass every gate on the way in.

## 4. `baseline_id` rotates per period

Load-bearing: keep one because the log is continuous and this happens — a
client reads job revision 7 under C1/baseline B; C2 opens under B; the change
does not touch that row; the client submits `(baseline=B, expect=7)` and it is
accepted against C2 semantics. Every transition **derives** a fresh `baseline_id` (§3.4).
`control-protocol.md`'s wire shape does not change; its **definition** does,
from "the log's identity" to "the period's identity". A superseded
`baseline_id` is refused naming the current one — the existing check, refusing
a stale *period* rather than a stale *log*.

## 5. Capacity, decomposed

`_bucket_used` sums units **held by live runs** and units **permanently spent**
(DL-50: a depletable drains, and replenishing one is `update_resource` — a
definition-time mutation of the SEM-16 class, and a non-goal here). `release()` decrements only for `completion`, or `success` on a
succeeded job, while `_held.pop` drops the job unconditionally, so a
depletable's spent units are in no row. A seal recomputing usage from holders
would refill every depletable:

```
demand vector: [('r:FUEL', 3, 'acquire', 'never')]
after acquire: used={'r:FUEL': 3} held={'j1': [('r:FUEL', 3, 'never')]}
after release: used={'r:FUEL': 3} held={}          <- spent, and no row says so
```

The fix separates the facts:

```python
class CapacityReservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    bucket: str
    units: int                            # > 0
    release_policy: Literal["completion", "success", "never"]
```

`consumed` values are `>= 0`. A self-consistent seal with `consumed["r:FUEL"]
= -3` would open with invented capacity; the loader refuses both violations
(PR-22).

- `JobRuntime.reservations: tuple[CapacityReservation, ...] = ()`
- `JobRuntime.waiter_seq: int | None = None`
- `RuntimeState.consumed: dict[str, int]`
- `RuntimeState.enqueue_counter: int`
- `CapacityPool` becomes a pure function of (catalog, rows, consumed).

Placement follows ownership: a reservation belongs to one `(job, run_number)`,
is acquired at that row's start transition and released at its terminal one,
and participates in its optimistic-lock projection. Both fields enter the
projection by default and change at the moments `status` already does — no new
revision churn. `RuntimeState` enforces: `reservations` non-empty only while
STARTING or RUNNING; `waiter_seq` non-null iff QUE_WAIT; a terminal transition
clears `reservations` and atomically moves non-released units into `consumed`;
a start may not overwrite non-empty `reservations`; the acquired vector is
frozen at acquisition; `enqueue_counter ≥` every non-null `waiter_seq`.

`sorted_waiters` does an unguarded `self.catalog.jobs[j]`; a waiter absent from
the catalog raises `KeyError`. §10 classifies that R, and the lookup takes a
documented default so the classifier is a gate rather than the only thing
between an operator and a crash.

## 6. The cutoff barrier

`Scheduler._next` cannot be re-derived at a boundary: resume re-anchors
**inclusive** of the scheduler frontier and dedups against the ticks the
journal holds, and a seal cuts that evidence away. Anchor exclusive of the
cutoff and an unconsumed tick vanishes; anchor inclusive with nothing else to
dedup against and a consumed one fires twice. Since DL-166 the anchor is
unconditionally inclusive and the sweep carries both dedup sources: the ticks
this segment journaled, and the cutoff the seal records.

1. the **operator** holds the runbook's set — every scheduled top-level job
   or box with a future tick (`deployment-runbook.md` §6 step 1) — with
   `ON_HOLD`; the barrier places no holds of its own (below). Draft 22 said
   "the §10 R-closure", which is a verdict on executing work, not a hold set;
2. stop admitting **every** externally requested attempt — rejected and no-op
   ones included, since each takes a durable decision — and drain every
   already-admitted attempt **except the active seal request itself** to its
   durable decision before any sidecar byte is written; the seal request's
   decision *is* the `seal` record (§2.2) and cannot precede the sidecar. An
   attempt admitted after the cut would have its decision land after the seal
   or not at all;
3. choose the cutoff instant **T**;
4. admit every scheduler tick due at or before T;
5. advance the oracle through T, firing every due semantic timer;
6. drain the resulting synchronous inputs and effects;
7. re-check §8 — if steps 4–5 started work despite the holds, **refuse**;
8. write the sidecar, then append the `seal` record at T;
9. open the next segment with `first_index = closes_at_index + 1`, `at = T`,
   and its scheduler strictly after T *(amended by DL-166: a statement of
   guarantee, not of mechanism. The resume anchor is INCLUSIVE of T — an
   exclusive one loses a same-instant sibling the crash left unjournaled,
   DL-45 — and the missed-tick sweep skips every re-derived tick the cutoff
   already admitted. No tick at or before T is fired or dropped by C2, which
   is what this clause is for; the exclusive anchor it once described is
   gone)*.

**There are no boundary holds.** The code has one hold bit, `on_hold`, and
`ON_HOLD`/`OFF_HOLD` set it; a tick arms only because it is set. Drafts 20–21
spoke of "the boundary's own holds" as if a second, distinguishable hold
existed, and an abort that "removed the boundary's holds while preserving the
operator's" could not have told them apart. So the barrier **never touches
`on_hold`**: step 1's holds are the operator's, placed before the seal exactly
as `deployment-runbook.md` §6 already instructs, carried across the boundary
as placed, and released by the operator's `OFF_HOLD` in C2 — which is the
"precise C2 event" that releases an armed latch (PR-26). The barrier freezes
*admission* (an engine flag, not a row field); it holds no job. An abort
therefore restores nothing on any row, and a successful seal carries every
`on_hold` exactly as the operator left it (PR-28c).

The only carried evidence is `scheduler_admitted_through: T`. **C1 owns every
tick ≤ T, C2 owns every tick > T**; a schedule new in C2 cannot fire at T.

**The scheduler's durable frontier is semantic, not "the newest timestamp in
the file."** `last_journal_at` today takes the maximum `at` over every record,
`leader` and `dispatch` included. So: T is 02:00, C2 opens at 02:10 and appends
`leader.at = 02:10`, the process dies before the missed-tick sweep, and the next
resume anchors at 02:10 — a 02:05 tick is neither admitted nor recorded as
dropped. That is latent today; the watermark must not inherit it. The frontier
is `max(opening watermark, admitted scheduler ticks, drop records, advance
records)` and **nothing else** (PR-25a).

## 7. The seal operation

Draft 2 never said who performs a seal. This section does.

**`dsl41 seal`** is one command with two entry modes and one body:

```
dsl41 seal --run-root <root> --estate-anchor <dir> \
           --next <estate files>... [-p site.properties] [--next-timezone …] \
           [--force-seal] [--claimed-actor …]
```

- **Live mode** — a leading engine is running on `<root>`. The CLI **stages**
  C2 first, in two steps because two sibling destinations cannot be renamed
  into place at once: it materializes the immutable bundle under
  `catalogs/<source_bundle_hash>/` by the liturgy — content-addressed, so a
  repeat is idempotent and a concurrent client writing the same bytes is
  harmless — and then writes only `staged_manifest.json` and `candidate.json`
  under `periods/.staging/<stage_digest>/`, where **`stage_digest`** is
  sha256 over the canonical `StagedNextPeriod` — the staged fields alone, never
  the engine-derived five (§3.4) — and is carried in the request beside
  — not instead of — the request's own `fingerprint` over the whole envelope
  (§2.2). Draft 10 used one name for both; they differ whenever `force_seal` or
  the actor differs. Then it speaks to the engine over the control socket
  with a `seal` verb (a v3 mutating verb; it names an
  `expect` on nothing, because it is a boundary, not a row mutation, and it
  carries `request_id` like every command). The engine validates **exactly the staged bytes the fingerprint names**,
  performs §6 steps 1–8 in its single-writer loop — and inside step 8, before
  the `seal` record, **writes the committed `manifest.json` into the staged
  directory by the liturgy** — temp, `fsync(file)`, `rename`,
  `fsync(staged_dir)` — (the staged fields plus the engine-derived ones,
  §2.1), runs phase 2 against both in-memory models and those bytes, and only
  then **atomically renames the staged directory to `periods/N+1/` and fsyncs
  both `periods/.staging/` and `periods/`** so the artifacts the boundary
  names are the ones it validated and are durable before the record that names
  them — `staged_manifest.json` retained beside `manifest.json`. Draft 20
  said "writes and fsyncs" and renamed without the directory fsyncs this spec
  demands of every other artifact; a power loss after the committed seal could
  then lose `periods/N+1/` and leave a seal naming a manifest that does not
  exist (PR-30g). Draft 19 had the CLI write `manifest.json`
  and the engine rename it unchanged, which left the committed fields nowhere.
  Crash before the committed-manifest write: the staged directory is a
  candidate the retry validates again; crash after it and before the rename:
  the same, and the engine-written file is overwritten by the retry's own
  (PR-30f). The staged directory carries a
  **`candidate.json`** — `{artifact_format_version, stage_digest, next_period}`
  — because the rename to `periods/N+1/` drops the digest from the path, and a
  staged-manifest byte comparison cannot stand in for it, because the
  candidate identity also binds the request's staged fields one by one, and a
  later retry must be told *which* differed. If the
  engine dies **after the rename and before the `seal` record**, the request
  is unseen (§2.2) and `periods/N+1/` already holds a candidate: a retry whose
  `stage_digest` equals `candidate.json`'s reuses the **staged identity** —
  bundle, `staged_manifest.json`, `candidate.json` — and **regenerates
  `manifest.json` from its own cutoff** — in place, by the liturgy: temp in
  `periods/N+1/`, `fsync(file)`, `rename` over the old, `fsync(periods/N+1/)`,
  all before the sidecar — because `first_index` is attempt output: the first
  attempt closed at 100 and wrote 101, C1 resumed and admitted 101, and the
  retry's truth is 102. The fresh path's four fsyncs do not apply to a
  directory already in place, so the reuse path has its own (PR-30d, PR-30g); a retry with a
  **different** one moves the installed candidate to
  `periods/.quarantine/<old stage_digest>/<sha256 of its manifest.json>/` —
  a path that cannot collide when candidates alternate S1 → S2 → S1, and that
  is idempotent when the same bytes are quarantined twice — and installs its
  own, so a stale
  candidate is never silently selected and a `periods/N+1/` that exists is
  never blindly reused (PR-30d). Two CLI clients racing on one root stage under two fingerprints,
  and the engine commits exactly the one its request names; draft 9 let a
  second client overwrite a non-content-addressed manifest path between
  validation and commit, leaving a committed boundary that could not open. The
  engine then **exits with code 3** ("sealed; period
  N+1 is ready to open"). Step 9 is `dsl41 run --resume` on the same root, which
  opens from the seal (§11). The engine does not load C2 into itself: a
  transition is a restart, not a reload (DL-65), and the sealer having C2 in
  hand for the readiness gate is not the same as the engine adopting it.
- **Offline mode** — no engine is running. `seal` acquires `leader.lock` **and
  `anchor.lock`** (it will append; the anchor fence applies to every appender),
  appends a `leader` record at epoch+1, runs the same-root recovery barrier in
  full (replay, reconcile, **re-drive recorded kills**), then performs §6 steps
  1–8 as that offline leader — **staging, validating and installing C2 exactly
  as live mode does**, `candidate.json` included, because two install paths
  would be two places for the same crash window — and exits. It may not replay
  rows, observe "terminal" and seal.

**Exit codes.** The `seal` command exits 0 when the boundary committed (either
mode), 2 when it did **not** commit — the period is still open, and C1 may
legitimately have advanced first: an offline sealer's `leader` record and
reconciliation decisions, a live cutoff's admitted ticks, are C1 activity, not
damage — 4 when the answer was
`unknown` — printing the `request_id`, exactly as `sendevent` does — and the
live *engine* exits 3 ("sealed; period N+1 is ready to open"), distinct from
its 0/1/2, so an init system does not restart-loop a sealed engine (PR-30b).

In both modes the sealer holds **both** catalogs: C1 to run the barrier and C2
for the readiness gate (§8). Both modes then hand off to one of two openers:

- `dsl41 run --resume --run-root <root> --estate-anchor <dir>` — **in place**;
- `dsl41 run --open-from <anchor-dir> --run-root <new-root>` — **a physical
  roll**: reads the lineage head, requires it `closed`, requires the closing
  period fully quiescent (§8: no live executions at all) **and attested**
  (§1.3: `audit.json` present and passing `verify` in `closing_root`), and
  opens `next_period` into a fresh root that satisfies §1.1's ownership rule. Draft 3 said rolling was optional and defined
  no way to do it; `run` without `--resume` is a new genesis and therefore a
  different estate.

The in-place opener takes the successor claim (§1.3) as its first act after
`leader.lock` and `anchor.lock`. The **physical roll's** order is
`new-root leader.lock → sentinel durable → anchor.lock and claim → import →
segment → open` — the sentinel **before** the claim. Draft 11 said
claim-first, which let B move the head to `claimed(B)`, die before its
sentinel, and leave a root an old binary treats as unused and geneses into;
after a `reclaim` that is a fork. No state may exist in which the head is
`claimed(target_root)` while `target_root` lacks a valid sentinel (PR-01a). Live-mode exit is **code 3, without touching detached work** — an
engine-loop return is otherwise failure code 1, and detached-stop is otherwise
set before ordinary teardown, so this exit path is its own obligation
(PR-30b), not a footnote.

Three pure functions, each over its own inputs. Each runs at a moment when a
different subset of the facts exists. Draft 16 defined the
first as "the second minus one check", and the second's checks — a seal to
parse, a digest, record-vs-sidecar agreement, `T` — do not exist at readiness.

The first two phases take a **typed context** naming every fact they read,
and read nothing else — "pure" means exactly that, and draft 17's signatures named two
parameters for functions that had to read seven things, which invited an
implementation on filesystem lookups and engine globals that passes every
functional case and races. `StagedContext {staged, staged_bytes, boundary_request,
request_fingerprint, c1: closing catalog + profile, c2: CatalogIR,
carried_state, decision_index: read view, state_machine_version, at}`;
`BoundaryContext` = `{staged: StagedContext, committed, committed_manifest,
at, post_barrier_state}` — `at` is T, spelled as the field is. The candidate sidecar and the candidate record are
**not** in it: phase 2 splits at the sidecar, because the sidecar's
`classification` field IS phase 2's output and the classifier has to run
before there is a sidecar to put it in (below). Phase 3 takes no context
type: it takes the sidecar, the digest the naming record carries, and the
opening period's committed manifest. The `RuntimeProfile` is **inside** that
manifest and is read from there, not passed beside it — `OpenedRuntime`
carries the catalog identity the period opens under and never a profile of
its own. Two rules govern the engine an opener then assembles. A setting the wiring
CAN express and that disagrees with the pin **refuses the resume**, naming the
fields that moved: a runtime-profile change is a new period (§2.1). A setting
the wiring cannot express — the reconciliation and grace windows have no wire
flag — takes the pin as its default. An opener that assembled with ambient CLI
defaults instead would pass every functional case (PR-22b).

*(Amended by DL-151: a third rule, for the two fields that are neither.)*
`as_machine` and `machine_policy` change what the runner ANSWERS TO, and no
wired component reports them — they act in preflight, over the catalog. An
opener that DECLARES them is held to the pin like any expressible setting;
one that declares nothing inherits the pin, having said nothing to be held
to. Inheriting unconditionally is what let a boundary that staged a new
machine identity open silently under the old one, the process still
answering to the names it was started with while the manifest pinned
others. **And the runtime gate runs before the successor's segment is
written**: a refusal at the end of the ladder refuses the process but
leaves period N+1 open on the disk, so the next attempt with the same
wrong wiring meets an ordinary open period and succeeds. A boundary
refuses while it is still a boundary.

**Phase 1 — `validate_staged(StagedContext)`**, at
readiness, before the barrier. Inputs: the context above; no seal, no T. Checks: the candidate parses under a supported
`artifact_format_version`; `catalog_hash` v2 and `source_bundle_hash` match
the staged bytes; `RuntimeProfile` constructs and hashes to the staged
`runtime_hash`; `state_machine_version` equals the current; preflight passes;
the request's `request_id` is absent from the current period's `DecisionIndex`
under another fingerprint; and the **classifier** (§10) runs over the
carried state's live closure and the R gate passes. Nothing here touches a
seal.

**Phase 2**, after §6 step 6 and before the `seal` record, over **in-memory**
candidates. It is **two functions**, because its checks straddle the sidecar.
`validate_boundary(BoundaryContext)` runs first and returns the map;
`check_candidate(BoundaryContext, sidecar, record)` runs over the document
built from that map. Checks, across the two: the committed form's
`first_index == closes_at_index + 1`; every field duplicated between the
candidate record and sidecar agrees; the **classifier runs again** over the
post-barrier live closure — the barrier's own admissions and any reconciliation injections may
have created executions or latent intent that phase 1 never saw, and an
offline seal's recovery barrier can reconcile a FAILURE that leaves a
`pending_spawn` for a job C2 changes — **and its output is the committed
classification**: the
sidecar's `classification` field equals phase 2's result byte for byte, never
phase 1's. An implementation that re-ran only enough to reject R and committed
the stale phase-1 map would carry a seal whose A assumptions omit a latent case
the barrier created, and fail audit (PR-28a); `now == scheduler_admitted_through == T`; and the full
**load** below succeeds against the in-memory sidecar. A failure here refuses
the commit; C1 has advanced and is still open — **and `abort_boundary` runs**:
it clears the sealing flag, reopens control admission, restarts scheduler
admission and unparks FW tasks; it touches no row, because the barrier held no
job (§6). Draft 20 said "refuses, C1 still open" and a
literal implementation returned exit 2 with the engine frozen behind §6 step
2. After an abort a command, a tick and an FW poll all proceed (PR-28b). **The
reversible interval runs from §6 step 2's freeze to the instant before the
`seal` append begins, and is exception-safe**: every non-commit exit inside it
— a phase-2 refusal, a committed-manifest write or fsync failure, a rename or
directory-fsync failure, a sidecar write failure, any unexpected exception —
runs `abort_boundary` while the fence is still valid; a fence loss inside the
interval **fail-stops** rather than reopening admission, on DL-101's rule.
Draft 21 ran the abort only on validation failure, and an `ENOSPC` on the
sidecar left a live engine frozen behind a freeze it would never lift.

**The `seal` append is the point of no return, and a failure there is an
unknown outcome, not an abort.** The writer flushes the whole line before
`fsync`; an `fsync` error does not prove the line absent or non-durable, and a
partial append may have left a torn final line. Draft 22 told that case to
abort and reopen C1, which would append commands, ticks and completions
**after** a seal line that then survives a crash — records after a seal, which
recovery rightly refuses — or after a torn line, turning recoverable
final-line damage into interior corruption. So once any seal bytes may have
been written the engine **fail-stops** and reports the outcome unknown (exit
4, `request_id` printed), and recovery decides: a complete matching seal line → **`fsync` the WAL
first**, and only a successful `fsync` promotes it to committed — a complete
line in the page cache proves visibility, not durability, and a recovery that
closed the anchor and opened C2 on a line that then never reached the disk
would leave durable successors depending on a seal that vanished; if the
`fsync` fails, stay stopped; an absent or torn final line → truncate durably
and reopen C1; a seal line with records after it, or interior corruption →
refuse (PR-28d).

**Phase 3 — `open_from_seal(sidecar, expected_digest, manifest) ->
OpenedRuntime`**, at resume and at the tail of phase 2. `sidecar` is the
durable artifact at resume and the in-memory candidate in phase 2;
`expected_digest` is the digest the naming record carries — the committed
`seal` record at resume, the opening `segment`'s `opens_from_seal` in a
rolled root; `manifest` is the opening period's committed manifest, and
every field it shares with `next_period` must agree. Both facts are
required: an opening that skipped either would seed an engine from a
self-consistent sidecar that is not the one the lineage names, or under a
manifest that is not this boundary's. It returns an **`OpenedRuntime`** —
the carried `state`, the outbox, the executions, the classification, the
opening identity, and the ghost-run gate `_dispatched` — and **not** an
`Engine`: `Engine.__init__` takes a clock and adapters, calls `clock.now()`
and seeds the host row, none of which a pure function may do. The
catalog-derived half — referencers, the capacity pool, the scheduler
frontier and genuinely new rows — belongs to the impure loader that holds C2
and builds the engine from this. Draft 18 promised an `Engine` from a function that touched
no clock, which no implementation could honour without reaching past its
context. The load:

1. parse through the versioned seal schema; refuse an unknown
   `artifact_format_version`;
2. at resume only: verify the sidecar's recomputed digest against the digest
   in the naming record, and every duplicated field (§11 step 3);
3. install carried rows **without applying mutations or bumping revisions** —
   `Oracle.__init__`'s genesis input, seeded from the seal for rows that have
   one; a naive "construct C2 then overwrite" would seed carried entities
   first and move revisions;
4. seed only genuinely new rows (SEM-24 flags, declared globals);
5. rebuild the ghost-run gate `_dispatched` from every row with
   `run_number > 0` (§3.3) — the pure function's own output; referencers,
   capacity, the scheduler and adapter routing are the loader's, because they
   are derived from C2 and not from the seal;
6. validate: timer tokens unique, positive and ≤ `timer_seq` — two equal
   `(due, token)` entries would force the heap to compare two non-orderable
   `Event` objects; unique positive `waiter_seq`;
   reservation/status invariants (§5), reservation `units > 0` and every
   `consumed` value `>= 0`; the `executions`/`outbox_pending`/rows
   join (§3.5); `first_index`, `epoch` and `run_number` bounds against I2;
   `now == scheduler_admitted_through == T`; every route's `executor_id` names
   a host row;
7. touch **no** adapter, supervisor, socket, clock or filesystem.

Every one of phase 3 step 6's checks is an injected failure in PR-22; every
phase-1 check is one in PR-28; every phase-2 check is one in PR-28a.

## 8. Preconditions

A seal **refuses** rather than proceeds when any check fails.

**Readiness — before the current period closes**, and identical in live and
offline mode: C2 loaded from exactly the staged bytes `stage_digest` names;
`catalog_hash` v2 and `source_bundle_hash` computed and bound in the candidate
manifest; `RuntimeProfile` constructed and hashed; `next_period.
state_machine_version == seal.state_machine_version` (§2.1) and
`next_period.artifact_format_version` one this binary implements;
preflight-valid; classified against the carried state (§10) and accepted by the
R gate; the seal request's `request_id` absent from the current period's
`DecisionIndex` under any other fingerprint (§2.2); the staged directory
fsynced with its `candidate.json`; and **phase 1** `validate_staged` succeeds
over its `StagedContext` (§7). A failure here refuses while C1 is still open
and correct. **Then, after the cutoff and before the record**, **phase 2** succeeds —
`validate_boundary(BoundaryContext)` for the classifier half, and
`check_candidate` over the sidecar and record built from its output — a second failure there also refuses, and the cutoff work already
admitted stays as legitimate C1 activity (§7 exit codes).

**Always:**

- every admitted attempt has a decision, and no request is awaiting a
  response — **except the seal request itself**, whose decision is the `seal`
  record (§2.2);
- the engine input queue is **empty** — scheduler events, adapter completions,
  reconciliation injections and time observations included;
- no open `RuntimeState` transaction; no admission, application or effect
  delivery in progress — where "delivery in progress" **includes** a KILL whose
  TERM/grace/KILL ladder has not resolved to a spool proof, an applied CMD
  SPAWN whose adapter task has not yet written `spawn.json`, **and** an FW poll
  between its observation and its durable line. The sealer waits all three
  out — it parks FW tasks at a poll boundary before T is chosen (§3.5) — and
  never snapshots a half-run ladder, an unbound spawn or a half-recorded poll;
- scheduler admission frozen; control admission **permanently closed** once the
  seal commits — the old period admits nothing after its boundary;
- `outbox_pending` and `executions` account for every intent and every live
  run the reconciliation sweep can find, and the sweep — journal dispatches,
  spool directories, supervisor `LIST` — finds nothing they do not;
- no indeterminate KILL whose target might still exist;
- every supervisor **that owns unresolved execution evidence** reachable —
  one with a carried non-terminal execution, a pending or indeterminate
  effect, or a spool candidate the sweep could not close. An unreachable one
  of those makes quiescence **unprovable**, and the seal refuses. A host that
  was evicted, whose held work was re-driven or retired, and whose spool
  candidates are all resolved, owns nothing a seal needs; requiring *its*
  supervisor would make a permanently dead machine block every future seal
  (PR-27a).

**A reachable supervisor with an empty `LIST` is not proof.** `LIST` shows what
*this incarnation* spawned; a restarted supervisor has a new incarnation and an
empty history. Proof needs the `LIST` from the incarnation whose lease the
sealer holds, reconciliation against every carried non-terminal row, the spool
per candidate, `boot_id` and (pid, start-time), and refusal for any candidate
left unresolved.

**Mode:**

| transition | drain required |
| --- | --- |
| in place, detached | none beyond the §10 R-closure |
| in place, tethered | full — stopping the engine cancels live commands; tethered is the CLI default |
| physical roll, every execution terminal, closing period attested | permitted — the closing root's supervisor `LIST` is empty from the sealer's own incarnation and the spool sweep finds nothing live |
| physical roll while jobs are live | **refused** — the supervisor is one per run root and a new-root engine cannot reach the old root's work; the bridge that lifts this is a non-goal (§12) |

## 9. Retries across a boundary

`parse_envelope` rejects a foreign `baseline_id` **before** the decision index
is consulted, and `baseline_id` is in the fingerprint. A retry composed under
C1 cannot be answered after C2 opens. With §4 that is a **liveness** loss, not
a safety failure: refused naming the current baseline, never mis-applied.

- **Hard** (§8): nothing admitted-without-decision, nothing awaiting a
  response, and — the condition that makes this argument hold — the old period
  admits nothing after its seal.
- **Soft**: the closing manifest's `retry_horizon_us` since the last admitted
  externally requested **attempt with a durable decision** — applied,
  rejected, or an applied no-op — `host` commands included. Below it the seal warns and requires
  `--force-seal`, recorded as `force_seal: true` in the sidecar's `boundary_request` and on
  the `seal` record, with the gate's output in `forced_gate` — so the log alone
  shows a forced boundary.

Naming a horizon weakens `control-protocol.md` §3's unbounded exact-retry
promise. Contract change, no wire change, own decision-log entry; clients are
told retries expire.

## 10. Classification

### 10.1 Three tiers, not one

Draft 2 said "R when live and changed" and then listed A cases for `armed`
jobs — while defining `armed` as live and ruling that R beats A. Every named A
case was unreachable. The tiers are:

| tier | a job is here when | changed closure ⇒ |
| --- | --- | --- |
| **executing** | RUNNING or STARTING; a `pending_spawn`, `bound` or `fw_watch` execution; a member of an executing box | **R** |
| **latent intent** | `armed`; QUE_WAIT; a non-stale authoritative timer | **A**, naming the assumption — except **removed ⇒ R** |
| **not live** | otherwise | **carry**, listed as changed in the report |

**`pending_spawn` is executing, not latent.** The oracle reaches RUNNING before
the shell plans the SPAWN, so a pending SPAWN's row is already RUNNING and the
sets overlap; and the effect carries no frozen command — `_apply_spawn` reads
the **current** catalog's `JobIR` at dispatch. Classify it A and this happens: C1
starts `j` on a passive host, SPAWN stays pending; C2 changes `j.command`; C2
opens and the host is activated; the C1 effect executes **C2's command under
C1's run number and reservations**. R, or freeze the whole dispatch spec into
the effect; R is the smaller change and the honest one (PR-39a).

**Removed ∧ executing ⇒ R** as well: a removed job is not in `dispatchable`, so
a KILL for it plans no effect and `KILLJOB` would stop nothing. Removed ∧ not
live ⇒ ghost, retained and listed; L001 already refuses a *condition* naming it.

**Precedence: R beats A** only where an executing rule and a named A rule both
fire — a running holder of a resource whose capacity C2 lowers.

### 10.2 The classification graph

Neither the per-job fingerprint nor IR-G computes the blast radius. IR-G's
edges are producer→consumer over job and global nodes only: no resource,
machine, calendar or timezone node exists there. This graph is built for the
purpose, with IR-G as one **reversed** input.

Nodes and what moves them:

| node | changed when |
| --- | --- |
| job | its `JobIR` fingerprint moves (`period.job_fingerprints`, the leaf test — renamed out of `runner_history` by DL-131, §15) |
| box containment | `box_name` on any member moves, at any nesting depth |
| global | declared default moves; added; removed |
| external instance (`name^INST`) | the `insert_xinst` declaration moves |
| resource | `amount`, `res_type`, or a release-policy default moves |
| machine | `max_load`, `type`, `node_name`, or membership moves — the fields resolution actually reads |
| calendar / cycle | a referenced date set moves |
| timezone basis | `default_tz` or `tz_aliases` contents move |
| runtime profile, **per field** | `default_tz`, `tz_aliases` → every job with `start_times`, `start_mins` or a calendar; `as_machine`, `machine_policy`, `execution_mode`, `deadman_us`, `cmd_grace_us`, `reconcile_settle_us`, `spawn_window_us` → every CMD job; `fw_default_interval_us` → every FW job; **`retry_horizon_us` → no job** — it is boundary policy, and a field that reached every job would turn a horizon tweak into a full live-work drain |

Edges, **from a job to what it depends on**, every one of them, the profile
fields included: its condition's job, global and `name^INST` atoms —
*(amended by DL-131, at build:* walked directly off `JobIR.iter_conditions()`
(condition, `box_success`, `box_failure`), never off IR-G's edge list, which
diverts a local unqualified `n()` into `mutex_groups` (M07) and keeps no edge
for it; IR-G remains the box-topology input*)*; its box, and a box to each member (both directions, nested); its
`resources:` entries; its `machine:` and that machine's members; its calendars
and cycles; the timezone basis for every job with `start_times`, `start_mins`
or a calendar; and **from each job to each runtime-profile field** the table
names for its kind — so a live CMD's forward closure reaches `cmd_grace_us`,
and a C2 that changes only the grace cannot commit over it and then kill the
C1 run with C2's ladder. Draft 24 wrote "field → job" for the profile and
"job → dependency" for everything else in one sentence; a reversed-edge
implementation reached no profile field from any job and passed every listed
obligation (PR-37a). `retry_horizon_us` has no incoming edge from any job.

**Two questions, two directions.** The R gate asks *"is anything live job J
depends on changed?"* — J's **forward** closure. The boundary-truth diff asks
*"whose readiness flips because X changed?"* — X's **reverse** closure, then
condition truth under C1 and C2 at the carried state. Both are computed;
neither substitutes for the other.

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

`deployment-runbook.md` §6 says latches "die with the old baseline". Under the
carry they survive, deliberately: dropping one at the boundary is an implicit
transition with no admitted input. If unwanted, the honest alternative is an
explicit journaled disarm **before** the seal. Obligation: one tick under C1
while held → **exactly one** start after C2 opens (PR-26).

*(Amended by DL-158:)* that disarm exists: the control plane's `DISARM` job
verb (`control-protocol.md` §3). It clears the latch and does nothing else;
an unarmed target is an accepted, journaled no-op; and it is legal at any
time, not only before a seal — the pre-seal timing above is when it changes
what C2 does, not when it is admissible. It drops only the latch visible at
application time: `applied` does not inhibit a later arm, and it cancels no
start already out of the latch — a QUE_WAIT attempt stays queued and a
deferred run-window start still fires. Across the boundary an old-baseline
`DISARM` is refused exactly as every stale-baseline command is; a newly
composed C2 command may drop a carried C1 latch, and the WAL shows who did
(the input's source and actor, and the decision's moved revisions — an
empty revisions map is the audit mark of the no-op). PR-26 reads
accordingly: one held tick under C1 → exactly one start after C2, or none
if an admitted `DISARM` dropped the latch in between.

## 11. Resume, replay and recovery

**Resume**, from the latest committed seal — or, in period 1 before any seal
exists, from the genesis segment. Draft 3 said "never from genesis" and left a
new-format estate that crashes before its first seal with no path back:

1. `flock` `leader.lock` (before any side effect); read the sentinel and
   refuse unless it is a `period_root` record naming this estate (§1.1's
   ownership rule applies to resume as to creation);
2. `flock` `anchor.lock`; read `anchor.json`; refuse on `estate_id` mismatch;
3. **select the seal by lineage, from what this root holds**: if the active
   segment exists, its `opens_from_seal` names the sidecar this period opened
   from — the imported one, in a rolled root — and that is the seal; if no
   successor segment exists yet, the newest **committed** `seal` record in this
   root's last segment names it; if neither exists, this is period 1 before
   any seal, and replay starts at genesis. In every case verify the sidecar's
   recomputed digest against the digest the naming record carries, and every
   duplicated field. A rolled root never holds the predecessor's WAL or `seal`
   record, so a rule that walked to "the newest committed `seal` record"
   selected evidence it did not have; the local `segment` is the proof.
   A sidecar newer than the last committed record is an orphan and is never
   selected;
4. act on the head: `open(N, this root)` with N's `seal` record present →
   perform the `open → closed` CAS the crashed sealer did not (§1.3); `closed`
   and no following `segment` → `claim_successor(estate_id, seal.digest,
   seal.next_period, target_root)`; `claimed` with our `claim_id` and our
   first segment **already durable** → perform `claimed → open` (the crash was
   between segment and head); `claimed` with our `claim_id` and no segment →
   resume the claim; `claimed` with another → refuse naming the holder;
   `open(1, this root)` with `segment_durable: false` and a durable segment →
   finalize;
5. `open_from_seal` (§7 phase 3) over that seal, under the digest the naming
   record carries and this period's committed manifest — or, with no seal in
   the lineage, `Oracle(catalog)` genesis from segment 1 exactly as today;
6. replay the segments after the seal in order, each in its own period context;
7. run the reconciliation ladder (`runner-design.md` §7), amended so that a
   live wrapper under a terminal row is re-driven regardless of its KILL
   effect's recorded state (PR-33);
8. dispatch.

**Replay across periods** (`dsl41 journal`, `dsl41 audit`) walks segments and
switches catalogs at each `segment` record. *(Built as DL-142.)* The
crossing is the opening above and not a second path: state folds through
the seal by `open_from_seal` exactly as an engine's does, and the next
period's catalog is loaded from the content-addressed bundle the opening
`segment` pins (§1.1) — like for like by hash, never a catalog the reader
was handed. A boundary is crossed only over a seal that proves out — the
digest the naming record carries, the chain from the seal record that
closes the predecessor, and `next_period` agreement with the opening
segment — and an unprovable one refuses by name. Refuse-don't-degrade is
not weaker on a diagnosis surface: a read-only replay across a forged seal
narrates a forged continuation with exactly the confidence of a true one.
And the seal itself must be **re-derived, not merely self-consistent**,
before the crossing: rewrite a sidecar canonically, recompute its digest,
and copy that digest into the closing `seal` record and the successor's
opening, and every binding above still agrees — all four were forged
together. Two proofs close that, chosen by what the read is holding. When
the predecessor's evidence IS being replayed — the ordinary lineage walk,
in place or across a roll — the predecessor seal is **re-derived from the
period's own WAL, spool and manifests, in the root that holds them**, and
a stored sidecar that is not what they produce is refused naming the
fields; that re-derivation also compares the `seal` RECORD with the
sidecar field by field (§2.2), so a rewritten record over an honest
sidecar refuses there too. When a later segment is named **alone**, the
predecessor's inputs are not read, nothing re-derives anything, and the
argument that lets a replay cross without an attestation is false — so the
predecessor's **attestation is required** and its absence is a refusal
that names it. The cost is real and stated: an unpruned lineage replays
each period twice, once to re-derive its seal and once to narrate it.

**"Verified" means re-derived, not self-consistent — and it has two named
tiers (DL-144).** A sidecar whose digest matches its own canonical form proves
integrity, not derivation. There are exactly two ways an estate can stand
behind a closed period, they are not the same strength, and they are **spelled
differently everywhere**, because a reader handed one word for both cannot tell
which periods the estate can still re-derive:

- **derivation-verified** — the period's own inputs are present and `audit`
  reproduced the seal from them. This is the tier defined field by field
  below, and the only tier a checkpoint may be *produced* at.
- **attestation-verified** — **seal-only**. The period's inputs were archived
  under §12's retention class; what stands for the period is its attestation,
  accepted by PR-02e's **consumer** rule and by nothing else: the checkpoint's
  own digest, its binding to the seal it names, and its `chain_through_period`.
  It is **not** a recursive walk — the induction was established when the
  checkpoint was produced — and it is not a weaker reading of the rule below;
  it is the other rule, the one a rolled root has always used for an imported
  seal. A period at this tier can never return to the first tier: the archive
  is irreversible (§12), so restoring the files does not restore the claim.
  Every reader reports the tier by name; nothing reports a shorter answer
  silently.

A seal is *derivation-verified* when `audit` has reproduced **every
digest-covered field** of it,
field by field — **except the scalars of `boundary_request`** (`claimed_actor`,
`force_seal` and `request_id`), which are
authoritative boundary *input* originating in a request no WAL record
independently holds; audit checks those for exact equality between sidecar
and `seal` record — `source` rides on both and in the request fingerprint —
and carries them. **`source` is `request` on every boundary since DL-138**:
it has one legal value, so there is nothing left to derive, and audit checks
the record and the sidecar agree on it and refuses a disagreement (PR-47b).
The second value, `adopt`, and the evidence pair that used to decide between
them — `catalog_hash_v1` on the period-1 `segment` and `adopted_from` on the
sentinel — went with the estate-adoption path. The sentinel is therefore not
an audit input. Everything else is re-derived: `request_fingerprint`
from the envelope, `forced_gate` from the pinned horizon, the WAL and T, and
the state, executions, outbox, classification and every lineage field
(`baseline_id` included) — from exactly four things: the opening seal; the complete
ordered WAL of the period — inputs, `advance`, `host`, `drop`, `decision`,
`effect_result`, `leader`; the immutable spool evidence (`spawn.json`,
`status.json`, `watch.jsonl`); and the C1 and C2 manifests. `outbox_pending`
needs `decision` and `effect_result`, because pending-vs-applied-vs-indeterminate-
vs-retired is the WAL's distinction and the spool does not encode it; the
scheduler frontier needs `drop`. And **period ownership of spool evidence comes from the WAL, not from a
timestamp**: a `status.json` terminalizes a C1 execution only when C1's WAL
holds the matching admitted completion input; without it the file is evidence
about an execution live at T whatever its `ended_at` says — a completion at
`ended_at == T` by clock resolution is exactly the case a timestamp rule gets
wrong. A `watch.jsonl` line past `watch_seq` is C2's by position. A CMD live at
T that completes in C2 audits as live in C1 (PR-47c). "Full" is not `state` and `executions`; it is
the whole document — and the proof is durable: `seals/<period_id>.audit.json`, written by the liturgy,
carrying `{artifact_format_version: 1, seal_digest, period_id,
chain_through_period, prev_attestation_digest, state_machine_version,
dsl41_version, audited_at, scope: "full", digest}` — canonicalized by §3.2,
`digest` over the canonical bytes with only the top-level `digest` removed,
and pinned by its own golden vector (PR-08b), so a producer and consumer on
two patch versions agree byte for byte — bound to the seal it attests and to the interpreter
that produced the attestation. Resume from a seal with no attestation whose
period inputs are corrupt or pruned is **refused by default**;
`--trust-unaudited-seal` overrides it, recorded in the opening `segment`'s
`trust_unaudited` field with the claimed actor. Availability is sometimes worth
more than proof; that is the operator's call, made in writing. **The switch is
specified and not yet built** (deferred by DL-133: it is resume's switch, not
an estate verb's). The `segment` field is there and every opener writes it
null, so the artifact does not move when the switch lands; until it does,
there is no override and PR-47's third clause is undischarged.

**Auditing an old period runs the interpreter that produced it.** `audit`
refuses a period whose `state_machine_version` it does not implement, naming
the version and the `dsl41_version` the attestation or `leader` record names.
The operator installs that version — the runbook's venv-per-version upgrade
pattern already exists for exactly this — and audits with it. Cross-version
audit inside one binary is a non-goal; keeping old versions installable is the
release discipline this implies, and it closes what draft 3 left open as PR-Q4.

**Recovery matrix** — every row is a crash-injection obligation (PR-45):

| situation | behaviour |
| --- | --- |
| sidecar present, `seal` record absent | orphan; period still open |
| `seal` record present, anchor head still `open` | committed; resume performs the `open → closed` CAS, then proceeds as the next row |
| `seal` record present, head `closed`, no following `segment` | claim the successor and open `next_period` |
| physical roll: crash after import, before the first `segment` in the new root | the import is idempotent by content address; re-import, then open |
| physical roll: closing period has no `audit.json` | refuse: attest first |
| head `claimed(claim_id, root)`, crash before the first `segment` record | the same `(seal, next_period, root)` recomputes the same `claim_id`, resumes it, opens; a different one refuses naming the holder |
| head `claimed`, first `segment` durable, crash before head moved to `open` | resume finds the segment, moves the head to `open`, continues |
| new-format estate, crash in period 1 before any seal | replay from the genesis segment |
| anchor directory deleted or replaced under a live incumbent | the incumbent stops on its next append/dispatch (`anchor.lock` re-check) |
| torn final line in the active segment | truncate to the last complete record |
| torn or empty **first** line of a new segment | the segment never opened; the file is removed and re-opened from the boundary, which is byte-identical (PR-07). The repair needs an **earlier segment in this root** to re-open from: `select_seal` falls back to the previous segment and reads the `seal` record there. A root holding exactly that one segment — a rolled root, or a fully archived one — **refuses** instead, naming the missing segment record. That is a refusal and not damage, and lifting it means teaching seal selection to open from the anchor head, which is a unit of its own (DL-144) |
| corrupt line inside a **closed** segment | that period is unauditable; later periods resume from a **verified** seal only, else refused (above) |
| a closed period's WAL absent, its **archive receipt** present and licensing it | archived (§12): the period stands at the attestation-verified tier; `audit` verifies the checkpoint, `journal` narrates an unreplayable gap and crosses on that checkpoint, `runs` names the missing coverage, `estate prune` re-plans the root |
| a closed period's WAL absent, **no receipt** | **loss, not an archive**: refused by name at the walk, at the replay and at the plan. The receipt is written before any deletion precisely so the two can never be confused |
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

**Legacy adoption is retired (DL-138).** Drafts 4–29 defined `dsl41 estate
adopt`: a transaction that fenced a run root written before this model,
translated its `header` journal into `wal/000001.jsonl` and sealed period 1 in
one step. No dsl41 estate runs in production, so the path had no producer and
no estate to consume. It is gone, and with it the `adopting` head state, the
`adopt` seal source, `catalog_hash_v1`, the sentinel's `adopted_from` and the
`legacy_batch: true` fold.

What stands in its place is a refusal, not a repair. A `journal.jsonl` opening
with a `header`, a `catalog_hash_version` of 1, a `result` or standalone
`effect` record, a `manifest/manifest.json` layout and an on-disk head state of
`adopting` are each refused **by name**, citing DL-138, by the owner that meets
them. A run root written before the period model is therefore neither adoptable
nor readable, and there is no supported path from one into a lineage.

`docs/protocol-evolution.md` is the contract this retirement ran under: the
per-protocol compatibility matrix, the lifecycle a new dialect enters service
by, the absence gate a retirement normally has to meet, and the pre-production
reset clause that let this one meet it trivially. It records this strip as the
first executed retirement.

**Subscribers** (`control-protocol.md` §5, v3): a client asking for an index
below the earliest retained record receives an explicit gap marker; the seam
between backfill and live is unchanged; `decision` replaces `result`+`effect`
in the stream.

## 11a. SPAWN idempotency that outlives the supervisor

The supervisor's SPAWN dedup **was** an in-memory `self.runs` lookup, and a
run's entry was what made a replayed `run_id` a duplicate. Once an estate root
never rolls, `LIST` must be bounded, so completed entries must leave memory —
and the moment they do, a delayed duplicate SPAWN becomes a fresh execution.
`self.runs` survives as the bounded `LIST` window and is **not** the
idempotency store; the store is the directory below. "Tombstones"
was a word in draft 5's amendment table; this is the protocol, and it is a
`supervisor-protocol.md` §5 amendment with its own decision-log entry.

The tombstone is the run directory, made crash-safe by two extra files and one
ownership change. Before this protocol the **engine** created the run
directory before it sent SPAWN; under it a detached run's directory is created
by the **supervisor** on receipt, and the engine keeps ownership only for
tethered runs. Otherwise the engine creates the directory, dies before sending, the
retry reaches the supervisor, "directory exists, no receipt" reads as
indeterminate, and a run that provably never reached the supervisor is lost.

1. `mkdir runs/<job>.<run_number>` — the directory can exist already in one
   case only, the orphan the last row of the table below cleared for reuse,
   because the replay resolution runs first;
2. write the **`run_id` index** entry by the liturgy:
   `runs/.by_run_id/<run_id>` — `{artifact_format_version, run_id, job,
   run_number}`. **Index before receipt**:
   the frozen idempotency key is `run_id`, and every later lookup goes through
   the index, so the first durable thing that names the `run_id` must be the
   index. Draft 7 wrote the receipt first, and a crash between the two left a
   receipt nothing could find — a retry of the same `run_id` against another
   `(job, run_number)` saw no index, no directory at its own path, and spawned
   again. With the index first, a crash after `mkdir` and before the index has
   made nothing durable that names the run, and the retry's own path is the
   first application — one process; a crash after the index and before the
   receipt resolves through the index to a directory with no receipt —
   indeterminate, no process. `run_id` is constrained to a **filename-safe
   grammar** at the wire — the canonical uuid4 string form the shipped adapter
   already mints, `^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`
   — refused otherwise, at the wire and again when the index is read.
  **Ownership is
   one-to-one in both directions**: one `run_id` maps to one `(job,
   run_number)`, and one `(job, run_number)` maps to one `run_id`. With
   `run_id` minted in the effect (§2.3) that holds by construction on the
   engine side; on the supervisor side a directory that already carries a
   receipt or an index for a *different* `run_id` is a **collision**, refused —
   never reused, never given a second index;
3. write `receipt.json` by the liturgy: `{artifact_format_version, run_id,
   spec_fingerprint, received_at}` — **before** the wrapper is spawned. `spec_fingerprint` covers the
   frozen wrapper input spec (`supervisor-protocol.md` §2) in three steps:
   remove `lifeline_fd` — the fd is ours to fill, so a retry carrying one
   would fingerprint differently from the receipt we wrote; replace every
   float, at any depth, with `"float:" + float.hex()` — §3.2's grammar has no
   floats and the frozen spec carries one in `grace_seconds`, so the exact
   bits go in, tagged so no plausible string field can collide with them;
   then sha256 the §3.2 canonical form of the result. It is hashed over the
   whole body and not through the `digest` helper, which strips a top-level
   `digest` key by design;
4. spawn the wrapper; the wrapper writes `spawn.json` (frozen, unchanged);
5. write `reply.json` by the liturgy: `{artifact_format_version, run_id,
   wrapper_pid, spawned_at}` — the answer as first given;
6. answer the engine.

A replayed SPAWN resolves the directory **through the index**, never through
the incoming path, and answers from the directory, not memory:

| directory state | answer |
| --- | --- |
| index entry, `receipt.json` with an equal `spec_fingerprint`, `reply.json` present | duplicate: the **original result fields** from `reply.json` inside the frozen duplicate envelope — `{ok, run_id, wrapper_pid, spawned_at, "duplicate": true}` (`supervisor-protocol.md` §5) — no process |
| equal fingerprint, `spawn.json` present, no `reply.json` | duplicate: result fields reconstructed from `spawn.json` (`wrapper_pid`, `spawned_at := started_at`) in the same envelope — equivalent, and the protocol says so rather than promising bytes it did not keep |
| directory at the incoming path holds a receipt or index for a **different** `run_id` | collision: refused, never reused |
| equal fingerprint, no `spawn.json`, wrapper alive | in progress: no second spawn |
| equal fingerprint, no `spawn.json`, nothing alive | **indeterminate** — the crash landed between receipt and spawn; nothing may re-spawn; the engine's E7 policy decides the run |
| `receipt.json` with a different fingerprint | collision: refused |
| index entry → directory with no `receipt.json` | crash between index and receipt: indeterminate, same rule |
| index entry names a directory that does not exist | impossible by write order (`mkdir` precedes the index); treated as indeterminate if ever seen |
| index entry unreadable, or naming a `run_id` that is not its own filename | **indeterminate**. Corruption is not absence: "no index entry" AUTHORIZES a spawn, so an entry that cannot be read must never answer as one that is not there |
| no index entry | first application — an orphan directory at the incoming path with no index and no receipt is a crash between `mkdir` and index and is reused, because nothing durable names its run |
| no index entry, and **another** `run_id`'s index names this `(job, run_number)` | collision: the same crash under a different id, refused. The index is scanned for that owner here and only here — after a crash, never on a healthy spawn |
| no index entry, no receipt, and the directory holds a `spawn.json` or a `status.json` | **indeterminate** — a run made under the old rule, where the engine owned the directory and no receipt was ever written. It holds that run's evidence, and forking into it would overwrite it |

Writing the receipt *before* the spawn is the safe direction: the failure mode
is a run that never happened being reported unknown, which E7 already handles;
writing it after would let a crash between spawn and receipt make the retry
spawn twice, which nothing handles. `LIST` may then evict completed runs freely,
because it was never the idempotency store. **The store itself has a retention
floor**, and it is a safety rule rather than housekeeping: "no index entry" is
"first application", so deleting an index entry or a run directory *authorizes
a spawn*. A run directory and its `.by_run_id` entry may not be pruned while
the SPAWN effect that names them can still be replayed — which is until the
period holding that effect is attested and its executions are terminal — and
any compaction must preserve the `run_id ↔ (job, run_number)` mapping and the
original reply. Likewise the spool evidence an unattested period's audit needs
(`spawn.json`, `status.json`, `watch.jsonl`) and a live or carried execution's
spool cannot be pruned. §12 states the retention story these floors sit under. Every row above is a crash point in
PR-36, and so are: the engine crashing after `mkdir` and before SPAWN (tethered
— the only case where the engine still owns the directory), the same `run_id`
presented against a different `(job, run_number)`, and a fingerprint collision.

## 12. Non-goals

- a physical roll while jobs are live (the multi-root execution bridge);
- a `state_machine_version` change across a transition (§2.1) — and not an
  extension point: a semantics change is a full drain and a new-estate genesis
  (`docs/protocol-evolution.md` §1, DL-138);
- the shared store — orthogonal; when it lands it replaces the anchor (§1.3);
- mid-run catalog reload (DL-65);
- automatic sealing on a timer;
- cross-node resource coordination (DL-49);
- a retention/compaction **policy** — but not its floors. Which periods, spools
  and tombstones may be pruned and when is a business decision
  (`deployment-runbook.md` §2). **Three verdicts, because there is a middle.**
  An artifact is **floored** (reachable from the head; refused), **held** (the
  head has moved past it and no class licenses removing it, so it stays and
  the verdict names the dependency in the way) or **prunable** (licensed by
  name). What may **never** be pruned is stated here,
  and it is *everything reachable from the lineage head*: the sentinel; the
  anchor and any active claim; the seal sidecar the current period opened
  from and the one it will close with; the current and committed-next period
  manifests, **and every installed-but-uncommitted candidate's
  `staged_manifest.json` and `candidate.json` until its seal commits or it is
  quarantined and no recovery references it** — recovery after
  install-before-seal is decided by those two files; their catalog bundles and `sources.json`; the latest attestation
  chain checkpoint and every attestation after it; the WAL and spool of any
  unattested period; the spool of any live or carried
  execution; and any SPAWN tombstone whose effect can still be replayed
  (§11a). Recovery refuses without a sidecar or a catalog directory; a
  retention rule that could delete them while obeying the tombstone floor was a
  rule that could delete the only artifacts able to open the head (PR-36c).
  The DL-146 perimeter journal (`perimeter.jsonl`, `docs/access-model.md`
  §6; added here by DL-147) sits outside this floor: no replay reads it,
  nothing in the lineage reaches it. Its one physical rule: pruned only
  with its whole root, never truncated in place — `access_seq` is
  recovered from its tail, and a truncation would forge duplicate keys.

### 12a. The archive — PR-Q3's answer (DL-144)

**Yes, conditionally, by explicit policy.** A seal-only archive may stand in
for pruned inputs. This is a decision, not a deduction: the text above allowed
either answer, and "never" was rejected as policy rather than argued away. The
period drops to §11's **attestation-verified** tier and stays there.

**The receipt is the point of no return.** `seals/<period_id>.archive.json` is
a §3.2-family artifact — `artifact_format_version`, canonical serialization,
`digest` over the canonical bytes with only the top-level `digest` removed —
carrying `{estate_id, period_id, seal_digest, attestation_digest,
chain_through_period, retention_class, archived, archived_at, dsl41_version}`.
`archived` is the **exact licensed artifact list**, relative to the run root,
sorted, no repeat, and of exactly one of **two shapes** — the period's segment
alone, or its segment together with its committed candidate's two files. The
all-or-nothing rule lives in the artifact and not only in the verb that writes
it: any other list describes a state this class never produces, and a reader
that weighed such a receipt would be reporting a tier for a period that is
half-archived. It is written **durably before the first deletion** of the
period it licenses, and it is the **recovery authority** — a later plan
enumerates what is left to delete FROM the receipt, never from what the disk
still happens to look like. A crash between the receipt and the deletions leaves an
estate that says what was licensed to go, and a re-plan completes it from the
receipt alone. A crash **before** the receipt is an estate where nothing
happened.

**Missing evidence with no receipt is LOSS and refuses.** Refuse-don't-degrade:
accidental loss must never read as archiving. Ownership decides which absence
is even a question, and there are two tests of it because there are two kinds
of reader. A reader holding the **anchor** — the estate walk, the planner —
reads §1.3's registry row: a period whose row names *this* root owes this root
its segment. A reader holding only a **root** — `dsl41 journal ROOT`, `dsl41
audit --run-root` — reads `periods/<N>/manifest.json`, which genesis and every
in-place opening install in the root that runs the period and a physical roll
never imports for a predecessor. Both answer the same question and neither
guesses; a rolled root legitimately holds a predecessor's sidecar and
attestation and none of that period's WAL, and both tests say so.

**A receipt is PROVED before any reader acts on it, through one shared door.**
Seven bindings: the receipt's own canonical bytes, digest, class and filename;
its `estate_id` against this root's **sentinel**; the sidecar's **own**
`estate_id` and `period_id` — reading a sidecar parses it and never asks whose,
so without this a foreign seal-and-attestation pair with the receipt restamped
onto it satisfies every other check; its `seal_digest` against that sidecar;
the **attestation**, by PR-02e's consumer rule (`verify_attestation` exactly —
not a recursive walk); its `attestation_digest` and `chain_through_period`
against that checkpoint; and, where a specific absent file is being excused,
that the list names **that** path. Every consumer applies all seven — the
estate walk, the retention plan and its live re-check, the tier, `audit` and
its re-derivation, `journal` and `runs` — because readers that bound a receipt
differently would make the estate's answer depend on which verb an operator
typed, and a function that is safe only behind one of its callers is not safe.
A receipt that is present and does not prove out is never treated as absent: it
refuses.

**Three artifacts per archived period join the floor PERMANENTLY** and may
never be pruned by any class, now or later: the **receipt**, the period's
**attestation**, and its **seal sidecar**. Delete the receipt and the archive
reads as loss; delete either of the other two and the period has neither inputs
nor proof.

**Eligibility is itemized per artifact dependency, not "everything the head has
moved past".** The class is named **`archive-inputs`** and is selected **per
period**. DL-135's default stands: no class named, nothing deleted. Exactly two
kinds are in it, and the act is **all-or-nothing per period**, so "archived" is
one state a reader can report a tier from:

| artifact | in the class when |
| --- | --- |
| `wal/<period>.jsonl` | the period is **attested** *in this root*; a **later** chain checkpoint covers it *anywhere in the estate*; every run **born** in it has had its directory, `.by_run_id` entry and default logs pruned already; every older period this root retains is archived, or is archived ahead of it by this same sweep, oldest first; and it is below the estate's **head** period |
| a **committed** candidate's `staged_manifest.json` + `candidate.json` | that period's WAL is in the class — the same cover, in the same receipt |

The cover is an **estate** fact and the period's own attestation is a **root**
fact, and the difference is the roll: a rolled root's last period is covered by
a checkpoint the SUCCESSOR root holds, so a per-root cover would floor that
period forever. §1.3's registry row says **where to look and which seal the
lineage committed**, and both halves are load-bearing: the path alone would let
an edited row fetch a cover from somewhere else, and the row's `seal_digest` is
what makes a branch *this* lineage's rather than merely *a* branch. Four
bindings stand between a row and a cover: a `period_root` sentinel of this
estate; a sidecar that is this estate's and that period's; that sidecar's
digest equal to the digest **the row committed**; and `verify_attestation`
binding the checkpoint to that sidecar and to its own `chain_through_period`.
**Disagreement refuses; absence only fails to prove.** A present root whose
sentinel is missing or names another estate, whose sidecar attests another
estate or another period, or whose sidecar digests to something other than the
digest the row committed, REFUSES the plan — otherwise an edited row could
point at a stranger's root, or at a same-estate root holding a second valid
pair for that period, and release WAL belonging to the branch that actually
ran. Missing proof is the other case and it is **skipped**: a root that is
off-line, a row with no `seal_digest`, an absent checkpoint, a sidecar that
will not parse, an attestation that does not verify. None of them says
anything false; each supplies no cover, and the walk keeps looking further
down. Skipping only ever holds more. The
live re-check before the receipt reads the anchor again rather than a snapshot
the plan carried, because the window it exists to close is exactly a row moved
in between.

Ruled **out** in v1, and stated so a later version knows what it is changing:
**content-addressed bundles** (shared by reference; deciding reachability
across an archived period is a race this class does not take) and the period
**manifest** (a later period's opening folds against it). Anything whose
attestation or checkpoint cover is absent stays floored or held exactly as
before.

**Two ordering rules, each a real dependency:**

1. **The spool goes first** (PR-36b's order). The tombstone floor resolves a
   run directory to a period *through the SPAWN effect in that period's WAL*.
   Archive the WAL first and every tombstone it explains becomes
   provenance-unknown and floored forever — a floor nothing can lift. So
   archiving a period's WAL **refuses** while any run directory or index entry
   of that period survives, and the refusal **names what remains**.
2. **Oldest first, and the deletions run in that order too.** The archived
   periods are a **prefix** of what a root retains, so the retained segments
   stay a contiguous suffix at every instant — including inside a crash window.
   That is what keeps §11's subscriber contract word for word: the gap marker
   is defined at the *oldest retained record*, and the backfill's contiguity
   and adjacency proofs never meet a hole. A deletion that the filesystem
   refuses stops every later period's deletion for the same reason.

**Eligibility is RE-CHECKED against the live disk immediately before the
receipt is written**, independently of the plan: the period's attestation, the
covering checkpoint, the spool, and the prefix. A period whose cover was
questioned in between refuses and is named. Every period **below** it still
goes; every period **above** it refuses too, naming the one below rather than
repeating its reason — that is the prefix rule, and a sweep that stepped over
the refusal would open the hole the rule exists to prevent.

**The archive is IRREVERSIBLE.** Restored files beside a receipt do not remove
the archived state. The receipt governs: every reader reports
*attestation-verified* for that period, whatever is on disk. The restored
inputs may still be **read** — nothing forbids looking at them — but they are
not a claim the estate makes, because the weaker claim was already published
and a tier that flickered with the contents of a directory would be no tier at
all.

**Readers name the gap; none answers shorter in silence** (PR-02f's family):
the estate walk accepts an archived row and refuses an unreceipted one;
`audit` verifies the checkpoint and reports the tier **by name**, in wording it
shares with no derivation-verified line; `journal` prints an explicit
unreplayable-gap notice **on stdout with the trace** and crosses the next
boundary by the attestation-gated route §11 already defines for a segment named
alone; `runs` names the coverage it does not have; `estate prune` re-plans an
archived root without refusing, including a root whose every period is
archived.

## 13. Obligations

House convention `test_prNN_*` and `test_prNNx_*` for a suffixed id — the
token shape is **`PR-\d{2}[a-z]?`**, the namespace is `PR-` and not `PM-`,
because `P-M\d{2}` is already the dossier's mapping-trace pair and one hyphen
is not a namespace. `docs/citation-index.md` gains the row, with that regex,
before the first citation; suffixed ids (`PR-02a`) are citations like any
other and the gate must resolve them.

Written against "what would a plausible-but-wrong implementation still pass?"

**Every row has a STATE: `active` or `retired`.** An active row is a property
a test holds the code to, and every row below is active unless its own cell
says otherwise. A **retired** row names the decision-log entry that retired it
and the refusal tests that replaced it. It stays in the table: the citations
that point at it must still resolve, and a reader of an older commit has to be
able to find what the obligation was. A retired row is never deleted, never
renumbered and never re-used for a different property. An active row may still
name a clause whose producer does not exist yet — PR-16's remap half, PR-47's
`--trust-unaudited-seal` half. The row stays active, the clause is named as
undischarged where it appears, and it is discharged by the unit that builds
the producer. Silence there would read as coverage.

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
verb, the `routes` query and the `route:` `expect` namespace are specified and
unbuilt (§2.2), so PR-16, PR-16a and PR-16b are discharged today only in their
carry and hash halves: `runtime_hash` ignores the table, the seal carries a
route in its frozen shape, and audit derives it. Their remap halves are
undischarged until the producer exists. PR-16c needs no remap and is active
whole.

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
| PR-22a | a `CHANGE_STATUS STARTING` row with no execution entry seals and opens; an execution entry with no non-terminal row refuses. *(Amended by DL-151: the CMD-or-FW half.)* At the resume loader, which holds C2: an entry behind a live **box** row refuses and writes no segment; the same entry behind the box's dispatchable MEMBER opens |
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
| PR-26 | one held tick under C1 → exactly one start after C2 — unless an admitted `DISARM` dropped the latch in between: then none *(Amended by DL-158)* |
| PR-27 | **table-driven over every §8 gate**: non-empty input queue; open transaction; effect delivery in progress; a KILL ladder unresolved; an applied SPAWN with no `spawn.json` yet; unreconciled candidate; unreachable supervisor; restarted supervisor with empty `LIST`; pending outbox on a physical roll; indeterminate KILL — each refuses |
| PR-28 | phase-1 readiness, one injected failure per check — unsupported format version, hash mismatch, profile mismatch, SM-version mismatch, preflight, `request_id` collision, R gate — each refuses while C1 is open and untouched; **two live seal clients** staging different C2s — the engine commits exactly the one its request's fingerprint names and the committed boundary opens |
| PR-28a | phase-2 boundary validation, one injected failure per check — `first_index` mismatch, record/sidecar disagreement, a post-barrier live-closure change the phase-1 classifier did not see, `now ≠ T`, a load invariant — each refuses the commit while C1 stays open; **and a post-barrier latent A case appears in the committed seal's `classification`** — a seal carrying phase 1's map is refused by audit |
| PR-29 | the old period admits nothing after its seal |
| PR-30 | `--force-seal` records `force_seal: true` in `boundary_request`, and `forced_gate` populated iff the gate was engaged, per §3.1's truth table — including an unnecessary force (no gate), a period with no prior externally requested attempt (age ∞, gate passes), **a recent `rejected` attempt and a recent applied no-op (both hold the gate)** |
| PR-30c | two `seal` requests with one `request_id` and one `next_period` but different `force_seal` or `claimed_actor` collide and refuse; **an ordinary command and a `seal` sharing one `request_id`** collide and the seal refuses at readiness |
| PR-30e | a committed seal's exact retry arriving under the new baseline is answered before the baseline gate — **after a physical roll, a B restart, A's removal, and lawful pruning of A's WAL**, from the imported sidecar's `boundary_request`; the same retry two periods later is refused as stale |
| PR-30g | power loss **after** the committed seal: `periods/N+1/` and its `manifest.json` survive — on **both** the fresh-install path (four fsyncs) and the same-stage reuse path (its in-place liturgy); with any one fsync removed the test fails |
| PR-28e | a `rejected` and an applied-no-op control attempt arriving after §6 step 2 are refused at admission; one admitted just before the cut has its `decision` durable before the sidecar is written; the active seal request is **not** waited on and the seal commits |
| PR-28b | after **every** non-commit exit **before the seal append** — phase-2 refusal, and fault injection at each manifest/sidecar write, rename, fsync and pre-commit fence check — `abort_boundary` has run: a control command is admitted, a scheduled tick fires, an FW poll appends; a fence loss inside the interval fail-stops instead |
| PR-28d | fault injection **on the seal append itself** — write error mid-line, `fsync` error after a complete line, power loss after flush before fsync: the engine fail-stops with an unknown outcome, never reopens admission; recovery then finds a complete line → `fsync`s the WAL and only then promotes it, **with power loss injected before and after that confirming `fsync`, and with the confirming `fsync` itself raising** — before it the seal may vanish and no successor exists; after it the seal is durable; when it raises, no anchor transition, no successor segment, admission stays closed, and a repeated recovery stays fail-stopped — a torn or absent line → truncated and C1 reopened, a line with records after it → refused |
| PR-28c | one operator hold, one **pre-armed** job and one held, **initially unarmed** job, a tick at T for the latter, then both a refused and a committed boundary: the pre-armed row is exactly as the operator left it; the initially unarmed row is `armed: true` with exactly the one legitimate C1 revision increment the tick caused — in **both** outcomes, so an abort that restored a pre-freeze snapshot fails; after the commit the operator's `OFF_HOLD` in C2 produces exactly one start |
| PR-30f | crash before and after the engine's committed-manifest write, before the rename: the retry re-validates, overwrites with its own, and the installed `periods/N+1/` holds both files |
| PR-22b | resume never runs a profile the period did not pin: a launch option that disagrees with the committed manifest's `RuntimeProfile` **refuses the resume**, naming the fields that moved, and the settings the wiring cannot express resolve from the pin rather than from an ambient default. Both halves, one case each — including the deadman, which compares at its OBSERVED value and not the asked one. *(Amended by DL-151: two more cases.)* A DECLARED `as_machine`/`machine_policy` that disagrees with the pin refuses and an undeclared one inherits it; and a refused open over a COMMITTED boundary leaves no segment and an unmoved head, so the corrected retry opens the same boundary |
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
| PR-48 | **RETIRED by DL-138.** It was the `estate adopt` crash matrix over the seven steps of §11's legacy-adoption transaction, and neither the verb nor the transaction exists. Its replacements are the refusal tests DL-138 owes (`docs/protocol-evolution.md` §7), one set per owner: a journal opening with a `header`, a `result` mid-journal and a standalone `effect` each refuse naming the kind and DL-138, while a `host` record is accepted and an **unknown** kind refuses naming itself as its own error; `legacy_batch` false proceeds, true refuses naming DL-138, missing or non-boolean refuses as malformed — the true case driven through a history and a retention consumer as well as through the central validator; `catalog_hash_version` 1 refuses naming DL-138 through **both** the journal reader and journal creation, and an unknown version refuses generically; a root holding `manifest/manifest.json` where the period manifest is absent refuses naming the retired layout, while a `manifest/` directory without that file refuses generically; `claim_root` and `plan_retention` on a `header` root refuse naming DL-138 and on garbage refuse generically; an on-disk anchor whose head state is `adopting` refuses **before parse**, naming DL-138; and `estate adopt` is not a command |
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

**A — the quiet boundary** (smoke). Night to completion under C1; a global set;
an operator hold; a depletable consumed. Seal. C2 changes three jobs. Open in
place. Assert the carry, the ghost, the A rows, the truth diff, and `audit`
reproducing the digest.

**B1 — the live boundary that commits.** Seal mid-night, detached, C2
touching **none** of the live closure, with all of: a long command live and
reattached (PR-31); a KILL ladder in flight the sealer waits out (PR-33a); an
unchanged FW watch crossing, reproduced by audit (PR-34); a live box with an
INACTIVE member, unchanged (PR-42's carry half); a QUE_WAIT pair (PR-21); an
INACTIVE job with a semantic timer (PR-41); a `pending_spawn` on a passive
host, unchanged (PR-32); two timers due at exactly T; a `--force-seal` and a
late C1 retry (PR-06, PR-30).

Built in `tests/test_nightbank_boundary.py` (DL-143), as two scenarios and
not one: `test_b1_the_boundary_commits_over_a_night_in_flight` takes the
whole live closure at once, detached under a real supervisor, with real
commands and a real watch; `test_b1_two_timers_due_at_exactly_t_are_c1s_and_the_next_one_is_c2s`
takes the exactly-T row alone. T is `clock.now()` at the barrier, so the
instant is a CHOICE only in the virtual domain — there the estate's own
`must_complete_times: "+20"` region boxes are armed to fall on T exactly,
with a third one minute later, and §6's rule is what the test pins: the two
fire inside C1 and the third is carried unfired and fires in C2.

"Touching none of the live closure" means C2 touches **something**: an
identical C2 moves no graph node, so the R gate would pass with nothing to
classify and §10.2's closure would never be computed. C2 changes one job —
the estate's iced, decommissioned report — and on an 81-job estate the
jobs outside every live forward closure are a **short list**, because a
shared machine or a shared box reaches almost everything.

**B2 — the boundary that refuses.** The same estate, one change at a time,
each a separate seal attempt that must refuse: C2 changes the `pending_spawn`'s
command (PR-39a); C2 changes the live box's INACTIVE member (PR-42's R half);
the supervisor is restarted before the seal so `LIST` is empty (PR-27); an
applied SPAWN has not yet written `spawn.json` (PR-27). Draft 4 put all of these
into one scenario and called it end-to-end evidence; it was a refusal scenario
mislabelled.

Built in `tests/test_nightbank_boundary.py` (DL-143): one `test_b2_*` per
row, each over B1's live closure, each asserting the refusal **by name**,
and each naming WHICH of §8's two refusal points answered — because they
leave different logs. Readiness refuses before the barrier and appends
nothing at all; a refusal after the cutoff leaves the cutoff's own admitted
work, which is legitimate C1 activity and not damage. A row that asked only
for "no `seal` record" could not tell the two apart, and an R gate that
moved from phase 1 to phase 2 would pass it.

Three of the four rows then assert the live closure the refusal left alone —
the long command still running as a **process**, not just as a row. The
fourth cannot, and the reason is the row itself: restarting the supervisor
takes its wrappers with it, which is exactly why the boundary must not
commit over their carried rows. That row asserts the estate and the FW
watch instead, and it is the one that pays for the literal reading of "the
watch still watches" — a new durable `watch.jsonl` line after the refusal.

**C — the lineage** (the fence). A quiet physical roll after attestation
(PR-02a, PR-02d); a fork attempt from a second root (PR-01);
the winner crashed with the head `claimed`
(PR-02); the anchor deleted under the incumbent (PR-03); a crash in period 1
before any seal (PR-45); a lost `seal` response on both sides of the record
(PR-30a).

## 15. Amendments

| document | change |
| --- | --- |
| `concurrency-model.md` §2 | the log is one **estate** of period-bounded segments, not one run root; `baseline_id` is per period |
| `concurrency-model.md` §4/§5 | `result` + `effect` → atomic `decision`; CM-17 closes on the file substrate |
| `concurrency-model.md` §7 | leader eligibility reads the current period's pins; `next_epoch` reads the seal; **`catalog_hash` becomes v2 — `meta.tool_version` excluded** |
| `concurrency-model.md` §11 | the catalog is immutable **per period** |
| `control-protocol.md` | **v3**: `baseline_id` is the period's; `seal` verb; the `host` cmd's `route` verb and the `routes` query with the `route:` namespace *(pending — §2.2)*; `decision` in the subscribe stream; the gap marker; exact-retry expiry |
| `runner_admission.py` | `Attempt.host` becomes `HostCommand \| RouteCommand`; `RuntimeState.revision()` gains the `route:` namespace *(pending — §2.2)* |
| `supervisor-protocol.md` §3/§5 | `receipt.json` `{artifact_format_version, run_id, spec_fingerprint, received_at}`, `reply.json` `{artifact_format_version, run_id, wrapper_pid, spawned_at}` and the `run_id` index entry `{artifact_format_version, run_id, job, run_number}` join the spool, each §3.2-canonical and liturgy-written; a detached run's directory is created by the supervisor; SPAWN idempotency is directory-backed (§11a) and outlives `LIST` presence and supervisor restart; `run_id` grammar enforced at the wire |
| `runner_adapters.py` FW | append-only `watch.jsonl`: a `start` line on dispatch, then one line per poll |
| `runner_adapters.py` CMD / `runner_effects.py` | `run_id` minted in `plan_effects`, carried on the SPAWN effect; the adapter reads it from the effect instead of minting |
| `concurrency-model.md` §5 | DL-96's "`run_id` is not bound before the attempt" deviation is lifted |
| `runner-design.md` §7 | record kinds; resume from a seal; the ladder re-drives a live wrapper under a terminal row regardless of KILL effect state (PR-33) |
| `deployment-runbook.md` §6/§7 | seal→swap→open in place; "latches die" false; upgrade keeps state |
| `ha-deployment.md` §2/§4 | ACQUIRE gains a lineage-head predicate; the store replaces the anchor; routes are carried state |
| `runner_ledger.py` | `LeaderLock` generalized to the anchor |
| `capacity.py`, `oracle_state.py` | §5 |
| `runner_supervisor.py` | completed-run tombstones (PR-36) |
| `runner_history.py`, `cli.py journal` | period-aware |
| `runner_history.py` | `_job_fingerprints` becomes `period.job_fingerprints` (DL-131): §10.2's leaf test is named here under its original home, and a pure analysis pass may not import a private name out of a runner module |
| `protocol-evolution.md` | **new** (DL-138): the per-protocol compatibility matrix, the lifecycle a dialect enters service by, the retirement gate, the pre-production reset clause, and the tombstone-registry rule |
| `retention.py` | the `archive-inputs` class, the receipt, the permanent floors and the itemized eligibility (§12a, DL-144); `estate prune --archive-inputs` |
| `period.py` | `ArchiveReceipt`, `seals/<period>.archive.json` and its readers (§12a) |
| `deployment-runbook.md` §2a | the archive is an operator verb with an order in front of it: attest, prune tombstones, then archive — and it cannot be undone |
| `citation-index.md` | `PR-\d{2}[a-z]?` row, and a `PR-Q\d` row for §16's open questions (DL-135); the PR row states what a retired row cites (DL-138) |
| `CLAUDE.md` | read-first list |

## 16. Open questions

- ~~**PR-Q1**~~ — closed: `retry_horizon_us` is a `RuntimeProfile` field, so its
  value is a deployment choice **durable in the period manifest**, which is
  what lets audit re-derive `forced_gate` under any later ambient setting
  (PR-47e). The gate stays soft.
- ~~**PR-Q2**~~ — closed by I1: there is no size roll; to roll, seal.
- ~~**PR-Q3**~~ — closed **by policy** on 2026-08-21 (DL-144), and recorded as a
  decision rather than as a deduction: nothing in this document implied either
  answer, which is why it was open, and no live estate could have settled it —
  it was a design question, not an observation question. **Yes, conditionally.**
  A seal-only archive may stand in for pruned inputs under §12a's
  `archive-inputs` class: a durable receipt before any deletion, an itemized
  eligibility list, three artifacts on a permanent floor, and readers that name
  the gap. §11's "verified" is now two named tiers, and an archived period
  stands at *attestation-verified*. E20 in `ops-model.md` §11 closes with it.
- ~~**PR-Q4**~~ — closed in §11: audit runs the interpreter that produced the
  period, and old versions stay installable.
- **PR-Q5** — the anchor in a paired-site deployment: single-site until the
  store, and `ha-deployment.md` must say so where it cites this.
