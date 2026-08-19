# HA deployment — the second host and the second site

`docs/concurrency-model.md` §1 ends with a sentence this document begins at:
*"Where it stops is one host."* Everything frozen there — the invariant, the
identity model, admission, effects, the takeover barrier, host lifecycle — holds
unchanged. What was missing was a store that two machines can share and a reason
to build the relay. A real deployment supplies both.

Status: **plan, not frozen.** Sections marked *frozen* below are frozen by the
documents they cite, not by this one. This document becomes normative when its
DL entries land.

**Reconciled 2026-08-20 with `docs/period-model.md`** (converged after 33
adversarial rounds; the mechanism for periods, seals, the lineage fence and the
optional run root). Where this document and period-model disagree, period-model
wins. The specific amendments it required here are marked *(period-model)*.

## 0. Scope

The target estate runs **N booking centers**. Each center schedules an isolated
graph — no job in one center depends on a job in another. Centers sit at
different UTC offsets. The client already operates BCM/BCP site switching, and
the authoritative database is replicated and highly available.

The bare requirement: all graph input data is present at primary and standby,
the primary executes the graph, the database is replicated, and a BCM switch
moves execution to the standby.

What this document adds to the frozen model is exactly one concept — **role** —
and one contract: what dsl41 requires of the database in return for not building
consensus.

## 1. Topology — N independent pairs, not a cluster

Isolated graphs mean no cross-site dependency, therefore no global scheduler and
no cross-site consensus. Each booking center is **one active engine plus one
standby, over its own estate, its own catalog and its own per-estate control
record**. This is the frozen machine model (one engine per estate, DL-49/DL-52)
deployed twice rather than a new topology.

Consequences worth stating because they are load-bearing:

- There is no quorum among engines. Two engines per estate is the shape;
  a third adds maintenance headroom and nothing else.
- Time domains stay per-estate. The SEM-35 timezone ladder and the resume
  gate's clock-domain check already carry this; a globally-spread estate does
  not need a global clock.
- A center's outage is a center's outage. Nothing about it is estate-wide.

## 2. Leadership — infrastructure election, application term

**Write access to the master is not leadership.** Both members of a pair hold
valid credentials, as does every other center's engine. Write permission
supplies neither a monotone epoch nor a linearizable leader record, which are
two of the five capabilities frozen at concurrency-model §1.

The correct split:

| concern | owner |
| --- | --- |
| which database copy is writable | the client's HA layer (BCM promotion) |
| which engine leads this estate | dsl41's per-estate term |

Database promotion may *trigger* a takeover. It does not *decide* one.

**ACQUIRE** — one short synchronous transaction on the writer endpoint:

```sql
UPDATE estate_control
   SET epoch = epoch + 1, leader_id = :incarnation
 WHERE estate_id = :estate
   AND catalog_hash = :catalog_hash
   AND state_machine_version = :sm_version
RETURNING epoch;
-- same transaction: INSERT the leader record into the ledger
```

The epoch is allocated *by being appended*, exactly as the flock implementation
allocates it today (concurrency-model §7's worked example) — the mechanism
changes, the rule does not.

*(period-model)* **Two things this statement lacks.** It has no incumbent
guard — a standby rebooting after OS patching runs it, bumps the epoch and
fences a healthy primary; `ops-model.md` §4 takes this up (boot as a follower;
leadership is an explicit act). And it has no **lineage-head predicate**:
period-model §1.3 makes the store, when it arrives, the sole authority for both
the estate term *and* the lineage head — one transaction consumes
`expected_head_digest`, advances the head to the opening period and allocates
the term. The file-substrate anchor it replaces must not survive beside it; two
leadership truths is the failure mode. ACQUIRE therefore also carries
`AND lineage_head = :expected_head` when S8a lands.

**Every admission** — one transaction, in this order:

```sql
-- 1. dedup FIRST: recover an exact retry even after its epoch was superseded
SELECT fingerprint, result FROM decisions
 WHERE estate_id = :estate AND request_id = :request_id;

-- 2. allocate an index only while this exact term still leads
UPDATE estate_control SET next_index = next_index + 1
 WHERE estate_id = :estate AND epoch = :epoch AND leader_id = :incarnation
RETURNING next_index;                       -- zero rows returned = fenced

-- 3. same transaction: input, state delta, result, and outbox rows,
--    with each affinity role already resolved to {executor_id, generation}
```

Step 1 before step 2 is the frozen dedup-before-stale-epoch rule
(concurrency-model §4). Step 3 is the frozen atomic-commit requirement, which
**the current code does not satisfy** — result and each effect are separate
`_write` calls, each fsyncing independently (`runner.py:711`,
`runner_journal.py:314`). The existing test proves ordering, not atomicity
(`tests/test_effects.py:249`). S8a fixes a live violation, not only a future one.

Dispatch re-checks the term, and the relay rejects any dispatch carrying an
epoch below the highest it has seen (concurrency-model §7, frozen).

**Operational rules** that ship with the contract:

- Never hold a transaction open across a relay RPC or payload execution.
- Promotion must abort old-primary sessions. If an old-primary transaction can
  still commit after promotion, failover is unsafe and no amount of application
  SQL repairs it.
- The connection pooler routes writer-only, pins each transaction to one
  backend, and purges old-primary connections on promotion.
- A stale *status* read is survivable — the server-side CAS rejects the write it
  would have justified. A stale *leader* read is not, and must never be served
  from a replica.
- A lost commit response is retried with an identical `request_id` and identical
  bytes, never as a new request.

**The limit, stated rather than discovered.** A partitioned old master can keep
accepting writes, and its `estate_control` row advances independently of the
promoted copy. No CAS across two diverged databases fences that. If it happens,
the database's single-writer guarantee has already failed and the repair is
fencing the isolated site (STONITH) — infrastructure, not scheduling. dsl41
states the requirement and does not pretend to satisfy it. See E12.

## 3. Durability — RPO=0 for safety transactions

The frozen model puts ledger, decision index and outbox in one transactional
store precisely so that one commit cannot tear. Replication must not reintroduce
the tear at promotion.

The **safety transaction** is the unit, and it contains: leader/epoch
allocation, attempts, results, the decision index, outbox rows and effect
outcomes, role routes and host generations, and any authoritative state not
reproducible by replaying the ledger.

Requirement, portable and vendor-neutral:

> **RPO=0 for every scheduler safety transaction, on every standby eligible for
> promotion.**

Two things this is *not*. It is not "make the ledger table synchronous" — on a
physical WAL stream a synchronous commit forces the whole WAL prefix through the
standby, business writes included, so table-scoped durability is not available
and the expected saving is smaller than it looks. And it is not satisfied by
synchronous replication to *some* standby if a different standby is the one
promoted.

Promotion is unsafe if the promoted copy did not acknowledge the safety
transaction, or completes before replaying the acknowledged WAL position, or if
an outbox row, route change, host generation or checkpoint is missing from it.

Whether the client's estate can provide this is E14, and it gates §8's S8e.

## 4. Affinity — the role is the only new concept

Today a job's `machine:` resolves to a physical host and strict preflight refuses
anything that resolves elsewhere (`runner_preflight.py:363`). Under site
failover the physical host changes, so the binding must move up one level.

- The catalog names a **logical role**. It is IR-F content, so the catalog hash
  covers it (`runner_journal.py:76`) and operational remapping does not change
  the hash — a remap is not a re-baseline.
- A **route table** maps role → `executor_id`, revised under epoch/CAS like
  any other authoritative row. *(period-model §3.3)*: the row is
  `RouteRuntime {executor_id, state_rev}`, owned by `RuntimeState`, addressed
  by the `route:<role>` `expect` namespace, remapped by the `host` cmd's
  `route` verb, and **carries no generation** — the generation is the host
  row's, read at effect birth. An earlier draft put a generation on the route
  and then needed four review rounds to define what a *stale* route meant; the
  answer was always "the evicted-host case", which §8 of the concurrency model
  already owns. It is carried across a seal as authoritative state and is not
  part of period identity (`runtime_hash`): a remap is not a re-baseline.
- **Resolution happens inside the effect-intent transaction** (§2 step 3), not
  at dispatch: `executor_id` from the route, `generation` from the host row's
  current value, and — since period-model lifts DL-96's deferral — `run_id`
  minted there too. An effect that already exists keeps the physical
  `{executor_id, generation, run_id}` it was born with. A remap is therefore visible to
  *later runs only*, and can never move a re-driven effect. That is the whole
  difference between an indirection and a reroute.
- **Preflight is rewritten**: from "does every machine resolve to this physical
  host" to "does every affinity role resolve to exactly one registered, eligible
  executor". Note the trap this closes — passing the same logical role through
  `--as-machine` at both sites would otherwise let both sites pass preflight
  simultaneously.
- **Resume replays the route table before host reconciliation**, so the takeover
  barrier reconciles against the roles as committed, not as configured.

What stays sealed, and must: the **oracle** (DL-93 already forbids it reading
host rows), the **supervisor protocol** (it accepts a fully resolved
`{job, run_number, command, run_id}` and knows nothing of roles), and the
**wrapper** (stdlib-only, DL-72). No block is opened to add this.

What must change, and was understated in earlier drafts: `runner_hosts.py:30`
holds one local executor and routes everything to it, and
`plan_effects()` takes one engine-wide `executor_id` rather than resolving per
job (`runner_effects.py:184`).

## 5. Uncertainty — derived, never a status

A job whose executor is unreachable has three indistinguishable fates: it
finished and we did not hear; it is stuck; it finished without recording. From
outside they are one class — **the outcome is unknown** — so they get one
mechanism, not three.

**No new `JobStatus` member.** An `UNKNOWN` status would reopen condition truth,
`n()`, box folds, resource accounting, timers and subsequent schedule ticks —
opening a sealed block to add incremental behaviour, which is the thing this
project refuses to do. `JobStatus` stays closed (`oracle_state.py:60`).

Instead:

- The job **stays `RUNNING`**, conservatively retaining its resources and
  satisfying nothing downstream. Conservative is correct here: the pessimistic
  reading of an unknown outcome is the safe one.
- `outcome_unknown` is a **shell-derived projection**, computed when the job's
  bound executor is quarantined and no evidence is available. Derived, like
  IR-G: regenerate it, never persist it as truth.
- **Evidence resolves it.** A returned host's spool yields the recorded terminal
  status, which is injected as an ordinary STATUS. No new path, no guess.
- **An operator can resolve it**, and does so by injecting an ordinary STATUS
  carrying `{job, run_number, verdict, reason, evidence_ref, expect}` — the
  verdict is a human's, recorded with its principal, and replays like any other
  input.

Note that `outcome_unavailable` is *not* this. It is an effect-result answer for
an indeterminate KILL (`runner_effects.py:159`), and for that case the oracle has
already moved the job terminal before the shell attempts the kill
(`runner.py:869`). The two must not be conflated.

**Hold-forever is a safe default, not an operating procedure.** A booking center
runs against a cutoff. So S8c ships a **cutoff report** — the bounded set of
in-flight unknown runs, with what is known about each — and an auditable **bulk
resolution** over that set. What it never ships is an automatic verdict.
*(period-model §3.3)*: the report is a **projection** and a seal does not carry
it; `outcome_unknown` derives from the bound executor's quarantine (`hosts`) and
the absence of evidence (`executions`), both of which the seal does carry, so
the report is regenerated in the next period, never copied into it.

## 6. Deliberately not built

The line, named precisely, because it is the one that decides whether this stays
a scheduler or becomes a second AutoSys:

> **Automatic timeout-driven retry or reroute of an outcome-unknown run to the
> remapped role.**

That single feature drags in per-job recovery policy, payload idempotency
declarations, retry timing, distributed resource ownership, duplicate
suppression and exception routing. Everything short of it — role lookup,
fencing, quarantine, evidence reconciliation, an explicit operator statement —
is an infrastructure boundary rather than scheduling policy.

Also not built: cross-site coordination of any kind; snapshots (§8 ships cold
full replay, and snapshots arrive only if a *measured* takeover time demands
them); whole-graph re-baselining as a failover mechanism; and *(period-model
§12)* the **multi-root execution bridge** — a physical roll of the run root
while jobs are live, which would need each live run's old supervisor endpoint
and spool root carried until it drains. That is a different thing from the
snapshots declined above: period-model's seal already bounds replay, so the
"cold full replay" of S8e becomes replay from the latest seal, and the bridge
is what would lift the quiescence a physical roll still requires.

That last one deserves its reason. Re-baselining at the standby is
implementation-cheap and looks attractive because all input data exists at both
sites — but inputs being present does not make outputs idempotent, and the
frozen §6 procedure it would reuse assumes quiesced triggers, drained work and
no surviving detached process (`deployment-runbook.md:159`). A network partition
denies all three facts. **Cold replay-and-hold preserves everything known and
leaves only the concurrently-active set for a human**, which is the smaller
problem and the one an operator can actually work under a cutoff.

The frozen wording is likewise not "never reroute" — gated eviction explicitly
permits a new run with a new effect id. The rule is:

> Never reroute an existing effect. Create a new run only after proof-bearing
> eviction, or attributed break-glass.

## 7. Obligations

House convention: tests named `test_cmNN_*`, continuing the frozen §9 series.

| # | obligation |
| --- | --- |
| CM-15 | the term fences: an engine whose epoch was superseded allocates no index (zero rows) and stops before the work, not after it |
| CM-16 | dedup precedes the term check: a retry carrying the original `request_id` returns the original decision even after its epoch was superseded |
| CM-17 | result and outbox commit atomically — no crash exposes one without the other (closes the live violation in §2) |
| CM-18 | a role remap does not move an existing effect: a re-driven effect keeps its birth `{executor_id, generation}` |
| CM-19 | preflight refuses a role resolving to zero or to more than one eligible executor, and refuses two sites claiming one role |
| CM-20 | a job whose bound executor is quarantined without evidence projects `outcome_unknown`, holds its resources, and satisfies nothing downstream |
| CM-21 | evidence from a returned host resolves the unknown to the *recorded* terminal status, never to a default |
| CM-22 | operator resolution is recorded with principal, verdict and reason, and replays identically |
| CM-23 | no `(job, run_number)` runs twice across a site takeover — the remaining half of CM-14 |

## 8. Stage order

```
S8a  store: schema, per-estate term/CAS, atomic safety transaction.
     Replaces flock. SINGLE HOST — no behaviour change, proves the store.
S8b  role indirection: route table, resolution inside effect intent,
     preflight rewrite. Still single host.
S8c  uncertainty: derived projection, operator resolution verb, cutoff report.
     Still single host.
S8d  relay: principals, epoch/generation fencing, run_id bound into the
     committed effect before the attempt. The second host appears.
S8e  site takeover: replay from the latest seal against the store
     (period-model §11; "cold full replay" is superseded); CM-23 over the
     proving ground.
S8f  process tier: two-site partition experiments against real processes.
```

*(period-model)* S8a **replaces** the file-substrate anchor of period-model
§1.3 rather than sitting beside it, and the period model itself is a
prerequisite of S8e — not of S8a–S8d — because a takeover that replays from
genesis would be rebuilt the day the seal lands.

The order is deliberate and is DL-97/DL-103's own reasoning applied again:
**everything provable on one host is proved before a second host exists.** S8a
through S8c change no observable behaviour and are individually testable; only
S8d needs a machine that does not exist yet.

§9's guardrail governs S8f: an unspecified chaos test is a flake generator, so
each experiment names its obligation, its fault and its pass criterion, or it is
not written.

## 9. Amendments this requires

| document | change |
| --- | --- |
| concurrency-model §0 | qualify the safety sentence — `evict --force` is a documented opt-out that can double-run |
| concurrency-model §1 | "where it stops is one host" superseded by S8a; the five capabilities gain the concrete SQL of §2 |
| concurrency-model §2 | `run_id` bound into the committed effect before the attempt; the local exception ends with the relay |
| concurrency-model §7 | the relay's stated trigger — a second execution host — has fired |
| concurrency-model §9 | CM-14's remaining half; the series extends past CM-14 |
| concurrency-model §10 | S8 added |
| deployment-runbook §0 | scope gains the paired-site deployment beside the single host |
| citation-index | `CM-\d{2}` range → CM-01–CM-23; the stage row needs both a range bump (S0–S8) and a regex widening — `S\d[a-d]?` does not match `S8e`/`S8f` |
| supervisor dedup | SPAWN is deduplicated by `run_id` today (`runner_supervisor.py:602`); the frozen model keys on `effect_id`. Fixed in S8d — and *(period-model §11a)* made durable: directory-backed, `run_id`-indexed, outliving `LIST` and supervisor restart |
| `docs/period-model.md` | its §15 lists every amendment the period model makes to the frozen contracts; this document inherits the `ha-deployment.md` rows of that table |

## 10. Decision-log entries this implies

Proposed, not yet appended. *(DL-113 was taken by run history; period-model §10
and this list both renumber when appended — read the numbers as an order.)*

- **DL-113** — HA topology is N independent per-estate pairs; no cross-site consensus.
- **DL-114** — leadership is infrastructure election *plus* an application term; write access is not leadership.
- **DL-115** — RPO=0 per safety transaction is the portable durability requirement; table-scoped synchrony is not available.
- **DL-116** — affinity binds to a role resolved inside the effect-intent transaction; role is the only new concept.
- **DL-117** — outcome uncertainty is a derived projection, never a `JobStatus` member.
- **DL-118** — no automatic timeout-driven reroute; the boundary between infrastructure and scheduling policy stated.

## 11. Open questions

Continuing the runner E-series (`docs/runner-design.md` §15).

- **E12** — the fencing contract. Who fences an isolated site, by what mechanism,
  and what does dsl41 require of it before permitting a proof-bearing eviction?
  Deployment requirement, not code.
- **E13** — cutoff-report semantics. What does the operator see, and is bulk
  resolution scoped per graph or per job?
- **E14** — does the client's database actually offer RPO=0 to every
  promotion-eligible standby? Gates S8e. Client question.
- **E15** — relay principal naming, issuance and rotation. Unchanged from
  concurrency-model §7; it wanted one real deployment to answer it, and now has
  one.
- *(period-model §16)* **PR-Q5** is this document's to answer: the local
  anchor is a single-site fence, and the paired-site deployment gets its
  lineage authority only from the S8a store.
