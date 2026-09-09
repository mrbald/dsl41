# Operations model — periods, the seal, and the carry

This document answers *what an operator does to it, on an ordinary Tuesday
and on a bad one*. A companion document once answered *where the engine
runs*; it was withdrawn (DL-189), and the surviving multihost design is
`docs/concurrency-model.md` §7.

Status: **plan, not frozen — and the ops view only.** The *mechanism* this
document proposed — the period, the seal, the carry, the lineage fence, the
optional run root — is **frozen in `docs/period-model.md`** (DL-114, with its
rulings DL-115…DL-129) and **built** (DL-130…DL-136, DL-141…DL-144). Where
this document and period-model disagree, **period-model wins**. §1–§3 and
§8a–§8b, the argument that got there, were removed at DL-189; `docs/period-model.md`
is the only home of the mechanism now. The
scenario catalogue (§5), the closed book (§6), run history (§6a), retention
(§7), authority (§8) and the deployment shapes (§4a) remain this document's.

**What shipped since this document was written.** Read every "today", "does
not exist" and "gap" below against this list:

- the period identity, the content-addressed catalog bundle, the
  `RuntimeProfile` and the manifests (DL-130);
- the boundary classifier — R / A / carry over a transitive closure (DL-131);
- the seal artifact and the boundary operation, with the cutoff barrier and
  the retry-horizon gate (DL-132, DL-133);
- the operator verbs `dsl41 seal`, `dsl41 audit`, `dsl41 verify` and
  `estate reclaim`, and the physical roll `dsl41 run --open-from` (DL-134);
- retention floors and `estate prune`, and the archive class that closed
  E20 (DL-135, DL-144);
- run history, `dsl41 runs` (DL-113), made boundary-aware (DL-136, DL-141);
- the capacity decomposition this document argued for (DL-120; removed at
  DL-189);
- the access perimeter — three tiers and local peer authentication —
  which closes most of §8 (DL-146…DL-148, `docs/access-model.md`).

What is still a plan: follower mode and `standby check` (§4) and the
multi-executor rig (§4a.5). The store-backed term and the store-era seal
went with the withdrawn HA plan (DL-189).

**Peer-reviewed 2026-08-18** (four rounds, converged). Round 4 acted on the
repo owner's objection that rolling the run root is clumsy and
counter-intuitive: the conclusion that the run root is not the period
boundary was the result (removed at DL-189), superseding this document's
original framing of the run root as the period boundary. Four claims were
withdrawn during review: "a seal does not require a quiesced estate", the
per-job release window, "only the last stage waits on the store", and the
IR-G analogy (removed at DL-189). The sweep gained four findings and the
capacity fix gained its shape.

## 0. The finding this document starts from

*(The problem as it stood. It is solved: `period-model.md` §0 restates it and
the boundary that fixes it is built. The tense below is the tense of the
argument.)*

One run root carried four different lifetimes, and they were forced to end
at the same instant:

| lifetime | what it holds | should end when |
| --- | --- | --- |
| **run** | one job execution: spool, logs, `run_id`, effect ids | the job ends |
| **period** | the open ledger tail: inputs, decisions, outbox | the books are closed |
| **baseline** | the catalog: JIL bytes, `catalog_hash`, manifest, roles | the estate changes |
| **estate** | job rows, globals, host rows, generations | the estate is retired |

`deployment-runbook.md` §6 collapsed all four into one act: an estate change was
**stop → swap → new run root**, and a new run root is a new log, a new baseline
*and* a fresh oracle. Genesis seeded definition-time state only —
`initial_status` flags per SEM-24 and the catalog's declared globals
(`Oracle.__init__` in `src/dsl41/oracle.py`). So every estate change silently
reset:

- every runtime global back to its declared value, or to absent;
- every operator `ON_HOLD` / `ON_ICE` / `ON_NOEXEC` placed since the last change;
- every `last_end_at` and `status_at`, which is what lookback conditions read
  (`Oracle._lookback_ok` in `src/dsl41/oracle.py`);
- every `armed` latch (SEM-32, DL-54) and every box's `ran_members` (SEM-10);
- every `run_number` back to 0, so `runs/<job>.1` means a different run in each
  baseline and an investigator cannot tell them apart from the path.

**That reset is correct at a cycle boundary and wrong in the middle of one.**
For a nightly estate that starts INACTIVE every night, closing the books and
opening fresh *is* the semantics. For a booking center that carries state across
days — a global set by yesterday's reconciliation, a `s(job, "24.00")` lookback,
a hold placed on Friday for Monday's release — it is silent loss, which is the
one thing this project refuses everywhere else (DL-07).

Nothing in the model said which of the two you were doing. This document
adds the concept that says it.

*(Closed.* A boundary now carries all five: `Oracle.__init__` takes the
carried rows and installs them before the genesis seed, and the seed skips
every row and every global the carry supplies, so only genuinely new rows are
seeded (period-model §7). `run_number` is monotone across the estate
(period-model I2), so `runs/<job>.<n>` names one run within a lineage.
A fresh run root still resets everything on this list — that is what a NEW
estate is.*)

## 4. Leadership is an ops act, not only an election

The withdrawn HA plan (DL-189) allocated the term by appending it:

```sql
UPDATE estate_control SET epoch = epoch + 1, leader_id = :incarnation
 WHERE estate_id = :estate AND catalog_hash = … AND state_machine_version = …
```

**That statement has no incumbent guard.** On one host the `flock` excludes the
second engine before it can reach the store (`concurrency-model.md` §1, DL-100).
Across two hosts nothing does. A standby that reboots after OS patching, or a
systemd unit that starts on boot at the standby site, runs ACQUIRE, bumps the
epoch, and fences a perfectly healthy primary. The primary stops correctly — the
fence works — but the estate has just failed over because a machine rebooted.

The ops model needs the missing state, and the client's own operating model
supplies it: **BCM already decides which site is live.** So:

- An engine **boots as a follower**. A follower appends nothing and is not the
  leader. *(Corrected after peer review:* it does **not** "serve queries" in any
  useful sense — revision-bearing reads are leader-only in frozen v2, and a
  follower that does not continuously replay has no current state to answer
  from. It serves health and compatibility metadata, which is exactly what
  `standby check` needs and nothing more.*)*
- Leadership requires an **act**. *(Sharpened after peer review:* an expired
  lease is not authorization. Expiry proves only that a renewal was not
  observed — a paused database or a partition expires it while the old site's
  detached work continues. The safe condition is `expired lease AND a durable
  desired-site authorization`, with incumbent renewal handled separately.*)*
  Given that BCM owns the site decision, the explicit promotion act is the
  primary path and the lease is a floor under the same-site crash case.
- `dsl41 standby check` is then meaningful and is what BCP governance will ask
  for: the store is reachable, `catalog_hash` and `state_machine_version` match
  what is installed here, every affinity role resolves to exactly one eligible
  executor, and the replay distance from the last seal is *this many* inputs.
  Green or red, per pair, without taking leadership.

A follower that also **replays continuously** would collapse takeover time to
near zero. It is not proposed here. The withdrawn HA plan (DL-189) declined
snapshots until a *measured* takeover time demanded them, and the same discipline applies:
seal frequency is already the knob. Seal daily and cold replay covers one day of
inputs. If measurement says that misses the cutoff, the warm follower is the
escalation, and it is a smaller step once the follower state exists at all.

## 4a. Deployment shapes — the same model at three scales

The simple single-host setup is a requirement, not a legacy case: one venv, one
foreground process, no services, **no runtime network dependency**
(`deployment-runbook.md` §1). Nothing here may make it pay for HA.

It does not have to. The multi-engine and multi-site cases are the same model
with the same code path, and the property to demand is exactly the one that makes
that true.

### 4a.1 Two mutexes, not one

One `flock` did two different jobs:

- it excludes a second process from the **run root's filesystem artifacts** —
  spool directories, `control.sock`, `supervisor.sock`, the journal file;
- it allocates the **estate's term**, because the epoch is written under it
  (`concurrency-model.md` §1, DL-100).

Under a shared ledger those separate cleanly. The run root stays local and keeps
its lock. The term moves to the store. On one box both apply; over the wire only
the term is shared, because each host has its own run root.

*(Half of this landed locally.* The period model added a second file mutex:
`leader.lock` still excludes a second writer from one run root
(`src/dsl41/runner_ledger.py`), and `anchor.lock` serializes the estate's
**lineage** — which root may claim the next period (`src/dsl41/boundary.py`).
So the run-root lock and the estate-level lock are already two locks on one
box. What has not moved is the term itself: the epoch is still written under
the run-root lock, and the store is still what a second host would need.*)

**That separation is what protects the simple setup.** The flock is kernel-
released when the holder dies, `kill -9` included, with no expiry to renew —
it is explicitly *"not a lease"* (removed at DL-189). A store-backed term has no such property:
a dead engine's row still claims leadership, so it needs a lease or an explicit
promotion (§4). Making the store the only substrate would trade instant local
crash recovery for a timeout, on the deployment that needs it least.

### 4a.2 The substrate is chosen by the kernel, not by the count

| shape | candidates share a kernel | term substrate |
| --- | --- | --- |
| N engines, N estates, one box | yes, and they never contend | file, one per run root |
| 2 engines, 1 estate, one box | yes | file — the flock already excludes correctly |
| 2 engines, 1 estate, two boxes | no | the store |

So the answer to "several engines on one box" is **yes, and without a database**.
The store becomes necessary at the moment two candidates cannot share a
filesystem lock — two hosts, or two containers with no shared mount — and not
before.

This is not a new abstraction. `concurrency-model.md` §1 already writes the
ledger as a contract — *"the required contract, whatever provides it"*, five
capabilities — with `flock` + fsync named as today's implementation. The port is
in the frozen text; it has one implementation and will have two.

### 4a.3 What must be identical, and what may differ

Identical, or the shapes are two models wearing one name: admission order and
dedup (§4 of `concurrency-model.md`), `expect` evaluation, the refused /
rejected / unknown classification, the binding of every effect to
`{effect_id, executor_id, generation}`, §8's four routing states and its eviction
gate, and the takeover barrier's four steps.

May differ: **the transport, and only the transport.**

### 4a.4 Three things block it today

1. `LOCAL_EXECUTOR_ID` is a constant and `seed_local_executor` is the only
   writer that creates a host row (`src/dsl41/runner_hosts.py`), so §8's host
   table holds exactly one row. `concurrency-model.md` §8 says so about itself
   in its DL-111 amendment: *"what is missing is a second row to point them
   at."* The **role → executor** map beside it is a separate thing and is
   thinner still: `implicit_routes` in `src/dsl41/seal.py` projects exactly one
   route from that one executor, at `state_rev` 0, because no verb that could
   move it exists yet.
2. **`--as-machine` and `executor_id` are different identities and only the first
   is settable.** `--as-machine` is preflight identity — which `machine:` values
   this runner answers to (DL-52). `executor_id` is routing identity — which
   table row an effect binds to. Two executors on one box need both, and
   `dsl41 run` has no flag for the second.
3. `dsl41 journal` seeds every replayed period with `LOCAL_EXECUTOR_ID`
   hardcoded (`src/dsl41/cli_run.py`, `_run_period`). The comment there is
   guarding the right thing — a replay onto a table without this engine's own
   executor decides *"no such host"* where the run decided otherwise — one step
   before the case that breaks it. The moment a run can name a different
   executor, offline replay must take the identity from the period's **opening
   seal**, which carries the host and route rows; the `segment` record names
   that seal in `opens_from_seal` and carries no executor field of its own.
   Period 1 has no opening seal, so genesis still needs an authority this
   section does not name.

### 4a.5 The local rig is the cheapest proving ground the relay has

Two engines and two supervisors on one box, each with its own run root and its
own `executor_id`, produce **a two-row routing table with no second machine**.
That is exactly what the frozen §8 rules have never been pointed at:

- an effect bound to executor A while B is active and eligible (CM-18);
- an affinity role that resolves to two executors, which preflight must refuse
  (CM-19);
- a drain that holds work on one row while the other dispatches (CM-13);
- a returning evicted supervisor at a stale generation (CM-12) — which today
  *"waits with the relay"*.

This is the same split DL-112 drew between the model harness and the process
tier: *"Neither tier can be asked the other's question."* The model harness
answers which interleavings are safe; the process tier answers whether the
mechanism is the one described. The local multi-executor rig answers a third —
**does the routing table behave the same when there are genuinely two rows?** —
and it is answerable now, without waiting for a network.

### 4a.6 Same semantics, not same failure modes

The isomorphism is over decisions, not over disasters. Saying otherwise would be
the more dangerous error:

- **One box is correlated failure.** Both engines die with the box. Co-tenancy
  and a test rig — never a resilience story. A genuine multihost pair, designed
  in the withdrawn HA plan (DL-189), is what would supply resilience.
- **One clock.** `T_skew` in §8's eviction bound is inert locally and real over
  the wire.
- **One filesystem.** A quarantined local executor is still inspectable; a dark
  remote one is not — that was the whole argument of the withdrawn HA plan
  (DL-189).
- **No partition.** The hardest class — E12, split brain, STONITH — is precisely
  the one the local rig cannot produce.

Ops details that follow from co-tenancy and are worth one line each: run roots,
control sockets and supervisor sockets are per run root, so nothing collides by
construction; `dsl41 serve` needs one port per estate; and `CapacityPool` is
per engine, so nothing arbitrates a box-wide resource across estates. That last
one is identical over the wire — a standing scope boundary (the `Qr` series), not
a break in the isomorphism.

## 5. The scenario catalogue

What "support clean" means per row: there is a written procedure, its
preconditions are machine-checked rather than remembered, every step is recorded
in the ledger, and an operator can tell success from partial success without
reading a WAL.

Two readings to keep straight. The **today** column is the pre-boundary
engine and is kept as the before-picture; the **gap** column is current, and
a gap the build has closed says so with its DL entry. And the row labels
`A1`…`F6` are this table's own: `E1`–`E11` in section E are intervention
rows, not the runner open questions spelled the same way — §11's E16–E23 and
C4's E9 are the open-question namespace (`docs/citation-index.md`), and B1/B2,
C1/C2, D1–D4 and F1–F4 each collide with a namespace too.

### A. Installation and lifecycle

| # | scenario | today | under the period model | gap |
| --- | --- | --- | --- | --- |
| A1 | initial install, one host | `deployment-runbook.md` §1–§3 | unchanged | — |
| A2 | provision the standby | not covered | same install, boots as follower (§4) | follower mode |
| A3 | standby readiness verification | not covered | `standby check`, run continuously | the verb, and what BCP wants in it (E17) |
| A4 | engine version upgrade | fresh run root, state lost | seal → upgrade → resume, and the estate keeps its state, for every upgrade that does NOT move `state_machine_version` (§3) | — for that case (DL-133; `deployment-runbook.md` §7). An SM bump is still a full drain and a new estate: period-model §2.1 refuses a `next_period` whose SM version differs |
| A5 | OS patching / host maintenance | stop the engine | `host drain` the executor, work finishes, engine keeps leading | the drain is frozen and built (§8 of concurrency-model), but with one executor row it is the engine's own host: keeping the engine up while its host is patched needs the second executor of §4a.4 |
| A6 | estate decommission | delete run roots | final seal, archive the chain, retire the estate row | not covered end to end. The floors, `estate prune` and the archive class ship (DL-135, DL-144); there is no decommission procedure and no retire verb |

### B. Estate content

| # | scenario | today | under the period model | gap |
| --- | --- | --- | --- | --- |
| B1 | initial JIL release | fresh run root | native genesis opens period 1; no opening seal | — |
| B2 | incremental change (add / remove / modify) | full quiesce, all state lost | seal → classified diff → open under C2 | — (DL-131, DL-133) |
| B3 | emergency hotfix, mid-cycle | not supportable without losing the night | the R-gate over the transitive closure; refuse only while something in it is live | — (DL-131). Tethered mode still drains: a transition is a restart |
| B4 | rollback | previous tag, fresh root, night discarded | forward from the carry under C1 (removed at DL-189) | — |
| B5 | calendar / holiday change | a catalog change, but easy to think of as config | it *is* a catalog change: firing dates move | say so in the runbook |
| B6 | properties / placeholder change | changes post-placeholder JIL, so changes the hash | same as B2 — this surprises people | say so in the runbook |
| B7 | affinity role remap | — | route-table change under epoch/CAS; visible to later runs only (CM-18) | the withdrawn HA plan, DL-189 |

### C. Running the cycle

| # | scenario | today | under the period model | gap |
| --- | --- | --- | --- | --- |
| C1 | ordinary night | frozen | unchanged | — |
| C2 | closing the books | does not exist | the seal, at the estate's own cutoff, in the estate's own zone (SEM-35) | `dsl41 seal` ships (DL-134). The operator chooses each boundary: automatic sealing on a timer is a period-model §12 non-goal. The cadence question is E16 |
| C3 | cutoff with unresolved runs | does not exist | the cutoff report is the trial balance. It is a **projection**: `unresolved` derives from the carried host and execution rows and is regenerated in the new period, never copied into the seal (period-model §3.3; removed at DL-189) | E13 |
| C4 | missed ticks over downtime | E9 skip-and-report, journaled | unchanged | — |
| C5 | deliberate catch-up after downtime | explicit `FORCE_STARTJOB`s | unchanged; the seal makes "what did we skip" answerable from the `drop` records in one period | — |

### D. Investigation

| # | scenario | today | under the period model | gap |
| --- | --- | --- | --- | --- |
| D1 | "why has X not started?" | `explain`, `deps`, `timers`, `plan` — frozen | unchanged | — |
| D2 | post-hoc, current period | `dsl41 journal` replay | unchanged | — |
| D3 | post-hoc, closed period | run root may be gone or archived | the closed book: seal + inputs + catalog (§6) | — the period's own post-placeholder bytes are stored in its root, content-addressed (DL-130). An archived period is named, not silently short (DL-144) |
| D4 | across a failover or a physical roll | the record splits across run roots | one ledger per estate: the trace is continuous | on one host it is built — the lineage registry names every root and the readers cross them (DL-141). Across hosts it still needs the store |
| D5 | regulator: what ran, when, under which definition, on whose authority | partially answerable; authority is a *claim* | the closed book and the run rows answer what ran, when and under which definition — the seal chain alone holds no run history; an armed access perimeter answers local authority | arm the access map, or the actor stays a claim. Authentication for a non-local principal is open (§8) |
| D6 | "is this job degrading?" | facts exist per run root; no key, no index, no query | the run table, segmented per job (§6a) | — `dsl41 runs` ships (DL-113), and reads a whole lineage from its anchor (DL-141) |
| D7 | "what did last night cost us?" — elapsed per box, per wave | walk the spools | export the run rows and aggregate them outside dsl41 | no period key and no wave key: `RunRow` carries neither, and `dsl41 runs` filters by job and time only |

### E. Manual intervention

Every one of these is an adjusting entry: recorded, attributed, replayed
identically, and never an edit to what is already written.

| # | intervention | verb | reversible | note |
| --- | --- | --- | --- | --- |
| E1 | status correction | `CHANGE_STATUS` | no — it is history | needs `expect`; ghosts legal for `JOB^INST` (SEM-07) |
| E2 | force a run | `FORCE_STARTJOB` | no | |
| E3 | hold / ice for a window | `ON_HOLD` / `ON_ICE` | yes | ice satisfies downstream, hold does not — the §6 trap |
| E4 | kill a runaway | `KILLJOB` | no | kill members, not boxes |
| E5 | set a global | `SET_GLOBAL` | by another set | now survives a re-baseline (§0) |
| E6 | drain / activate an executor | `host drain` / `activate` | yes | asserts nothing about reachability |
| E7 | evict an executor | `host evict` | no | gated on §8's three preconditions |
| E8 | **break glass**: `evict --force` | `host evict --force` | no | the one path that can double-run; authenticated when the access map is armed, attributed otherwise (§8) |
| E9 | **break glass**: supervisor shutdown | `supervise shutdown` | no | needs no live leaseholder: an expired lease, or an unexpired one whose holder's connection is gone, is grantable (DL-79) |
| E10 | resolve an unknown outcome | operator STATUS with evidence | no | the withdrawn HA plan, DL-189; CM-22 |
| E11 | bulk-resolve at cutoff | over the cutoff report | no | E13 |

**Break-glass must survive the seal.** An incident that can be conflated away is
an incident that will be. What the build does, and it is not what this
paragraph first proposed: the seal carries the **facts** — a forced eviction's
`forced_by` rides on the carried host row, a forced boundary is `force_seal`
on the `seal` record with the gate's own numbers in `forced_gate`, and every
undelivered effect is in `outbox_pending`. The perimeter's own ledger is
separate and is not carried: the perimeter makes a **best-effort, unsynced**
`privileged_admitted` write in `<run_root>/perimeter.jsonl` for each admitted
ops-or-higher control request, and that file never enters the WAL, because a
policy decision is not an engine input (`access-model.md` §6). The admission
stands when the receipt write fails. `supervise shutdown` goes to the
owner-only supervisor socket and emits no perimeter receipt at all.
There is no acknowledgement latch, and the accountant's rule is not enforced
mechanically. `unresolved` is not carried at all — it is derived (removed at DL-189).

### F. Failover

The word covers six different days. They need six procedures.

| # | scenario | who decides | procedure | what is at risk |
| --- | --- | --- | --- | --- |
| F1 | planned site switch (drill) | BCM, scheduled | quiesce → drain → seal → stop → promote DB → promote standby → reconcile → dispatch | nothing, if the drill is real |
| F2 | unplanned primary loss | BCM | promote DB → promote standby → ACQUIRE → replay from last seal → reconcile → dispatch | every run bound to the lost site is unknown → cutoff report |
| F3 | DB failover only, engine alive | the HA layer | the engine loses proof on its next append and **stops**; restart re-acquires; one epoch bump | an engine that continued on stale proof — the fence exists for this |
| F4 | engine crash, same host | nobody | resume inside the open period | frozen (`runner-design.md` §7) |
| F5 | partition / split brain | infrastructure (STONITH) | out of scope for dsl41 by design | E12 — stated, not solved |
| F6 | **failback** | BCM | *not symmetric*: the returning site's executors re-register at the new generation and self-fence first (CM-12) | the step everyone forgets |

F3 and F6 are the two that are missing from every draft so far, and both are
routine rather than exotic.

## 6. The closed book

What an investigator or an auditor is handed for a closed period:

1. the **seal** that closed it, and — for every period but the first — the
   seal that opened it, the two chained by digest. Period 1 has no opening
   seal: native genesis opens it from nothing;
2. every **input** between them — unless the period's inputs were archived
   under the `archive-inputs` class, which deletes the WAL after a durable
   receipt and drops the period to the attestation-verified tier
   (period-model §12, DL-144);
3. the **catalog** it ran under — the post-placeholder JIL bytes, not a tag;
4. the **principals** who asked for each externally requested input — the
   authenticated ones when the access map was armed, a claim otherwise.

Item 3 was the one that did not work, and it works now. It needed no store.
DL-130 put the period's post-placeholder JIL in the run root itself, under
`catalogs/<source_bundle_hash>/`, addressed by content. `dsl41 journal` loads
that bundle when the caller supplies no estate files, and gates it against the
`catalog_hash` the period's own `segment` record pins — so a closed period
replays from its own stored bytes and needs no checkout
(`_period_catalog` in `src/dsl41/cli_run.py`). The recorded paths are kept and
used: `sources.json` holds each file's original path and command-line order,
and the bundle is parsed **under those paths**, because `catalog_hash` covers
spans and a span names its file (`load_bundle_catalog` in
`src/dsl41/boundary.py`). Nothing is materialized in a scratch directory. The
shape this section proposed survives; the address is `source_bundle_hash`
rather than `catalog_hash`, and the artifact lives in the estate's root rather
than in a store.

Two consequences an operator should know. A bundle that no longer reproduces
the pinned hash refuses, and it refuses with a different sentence than a
supplied catalog that disagrees: one is corruption, the other is a checkout at
the wrong revision. And a period whose inputs were archived under §12 of
period-model contributes nothing to a replay — `dsl41 runs` names it rather
than answering shorter (DL-144).

Item 4 is now answered for a local peer **when the access map is armed**, and
that is §8.

## 6a. Run history — the fact table the ledger already contains

**The facts survived and the history did not.** Every timing an operator
would want is written, in three places, and none of them was queryable across a
baseline:

- the `dispatch` record — `{job, run_number, wrapper_pid, run_dir, started_at}`;
- `spawn.json` — `started_at`, written by the process that spawned;
- `status.json` — `ended_at`, `outcome`, `exit_code`, written by the process that
  waited (`supervisor-protocol.md`, frozen).

What is missing is not the data. It is a **key, an index and a query**:

- `run_number` reset to 0 at every re-baseline (§0), so `runs/job.1` named a
  different run in every run root and nothing identified a run across them;
- run roots were not indexed, so "this job's last twenty runs" meant walking N
  directories, each hash-gated to its own catalog;
- `JobRuntime` holds the latest status only — one row, no history
  (`JobRuntime` in `src/dsl41/oracle_state.py`);
- `trace` is the current run's, in memory.

So "is this job degrading?" was answerable in principle and unanswerable in
practice, and it got worse with every JIL release — which is exactly the
frequency at which an operator asks it. `dsl41 runs` is the answer and it
ships (DL-113); the rest of this section is what it was designed against, and
the amendment at the end of §6a.1 says where the build differs.

**The carry is the precondition.** Once `run_number` survives a re-baseline (removed at DL-189),
`(estate, job, run_number)` is a stable primary key for the life of the estate,
and the spool path becomes globally unique. Run history is not a separate feature
from the period model; it is the second thing the period model makes possible.

### 6a.1 Derived, never authoritative

The ledger already holds every fact. So the run table is a **projection**, on the
same rule as IR-G: regenerate it, never edit it, and never treat it as the
source of a truth the inputs disagree with. *(Not "and as the seal itself" —
the rule removed at DL-189 settles that the other way: a seal IS used
as authority, and what keeps it honest is reproduction rather than
derivation.)*

**It materializes at the seal**, before the period's inputs become archivable
(§7). A seal therefore produces two derived things — the **carry**, which is
state, and the period's **run rows**, which are history. Both are verifiable by
replay while the inputs still exist, which is the only window in which either can
be checked.

*(Withdrawn at build. The seal materializes the carry and nothing else; the
run rows are folded on demand from the WAL, so they cannot outlive it. See the
amendment below, and §7's second clock, which this paragraph was the reason
for.)*

One row per completed run:

```
{estate, job, run_number, period_id, catalog_hash,
 started_at, ended_at, duration_s, status, exit_code,
 started_by, executor_id, run_dir, box_name}
```

Boxes get rows too — a box's elapsed time is a real operational number and the
oracle already knows both ends of it.

**What is not a run row**: a start that produced no run. A dropped tick (E9), a
held job, a refused command — these are `drop` records and trace entries, and
folding them into the run table would make it answer two questions badly. "Why
did it not run" and "how long did it take" are different queries.

*(Amended by DL-113, at build; extended by DL-136 and DL-141.* Built without
waiting for the seal (`period-model.md` is the mechanism; §1–§3 were removed
at DL-189): `dsl41 runs` is a plain offline CLI verb over the
WAL segments a run root still retains — one per period — plus that period's
own catalog bundle and its spool, computed on demand rather than materialized
at any write time. There is no writer of a new record kind, and **it does not
materialize at the seal**: the run rows exist only while the WAL they are
folded from does. The row therefore carries neither `estate` nor `period_id`;
the caller names the run roots, or names the lineage **anchor** alone and the
registry supplies every root in period order (DL-141). `RunRow` adds three
fields this sketch did not have: `clock_source`, naming whether a row's timing
came from the wrapper's own spool or fell back to the journal; `job_hash`,
this job's own definition fingerprint; and `fidelity`, saying how much of the
row could be established when the period's manifest is gone (decisions 1, 4
and 5 of `src/dsl41/runner_history.py`'s module docstring). An open run gets a
row too, with a null `ended_at` and a null duration — never a fabricated one.
The stable key across a lineage is `(estate, job, run_number)` as this section
argued, but `estate` is supplied by the lineage the caller named and is not a
field on the row. Two limits are stated where a reader meets them: a run that
SPANS a boundary keeps its row in the period that dispatched it and its status
stays RUNNING, because the terminal input is in the next segment and the fold
reads one segment at a time; and a full-fidelity period costs one replay each,
while a period whose manifest is gone folds from its records with no replay at
all. "The oracle already
knows both ends" of a box's run turned out to mean that literally: a box
gets no `dispatch` record and its fold is emitted, never journaled, so its
row is only recoverable by replaying the journal through a fresh Oracle —
the same replay `dsl41 journal` already does, with the catalog rebuilt from
the root's own stored inputs rather than supplied on the command line. The
same replay also
closes a leaf run that KILLJOB or a `term_run_time` auto-TERMINATE ended:
both are decided by the oracle synchronously while processing the KILLJOB
or timer input itself, so neither produces the adapter-completion `STATUS`
record a pure record read would need.*)*

### 6a.2 What dsl41 owns, and what it must not

Jenkins is the right instinct and the wrong scope boundary to copy wholesale. The
half worth taking is already half-built here:

| Jenkins | dsl41 |
| --- | --- |
| build history per job | the run table above |
| build cause ("started by user X / upstream Y") | **already exists** — `started_by` (DL-68) |
| build → the source changes it ran | run → `catalog_hash` → the sealed baseline |
| duration trend, weather, "longer than usual" | the site's monitoring, not dsl41 |
| retention per job | §7's clocks |

`runner-design.md` §12 already fences the second half out: *"alarm delivery beyond
journal + UI (no mail/pager integrations)"*. That fence is right and this does not
breach it. dsl41 **emits facts**; trends, thresholds, dashboards and paging belong
to the monitoring the site already runs — `deployment-runbook.md` §4 already
points `query subscribe` at it. Concretely: a `runs` query verb and an export
(`dsl41 runs --job X --since ...`), and nothing that decides what is too slow.

### 6a.3 The one piece nobody else can compute

A duration series that silently spans a definition change is worthless, and worse
than worthless if someone acts on it. "This job got 40% slower on Tuesday" and
"Tuesday's release changed its command" are the same fact, and only one of them
is actionable.

Grafana will happily draw one line through that change. dsl41 will not, because
every run row carries the definition it ran under, and the history query
**segments the series where that definition moved**. This is the same error class
`equiv` exists for on the semantic side — a comparison that crosses a catalog
change is not a comparison — applied to the operational side, where nothing else
guards it.

*(Sharpened at build.* The break is drawn on the row's own `job_hash`, not on
`catalog_hash`, and falls back to `catalog_hash` only when either row lacks a
job hash. `catalog_hash` is deliberately conservative — an estate that changed
in any way re-baselines — so a release touching twelve jobs of eight hundred
moves it for all eight hundred, and a break on it would mark every job in the
estate as changed. Both hashes ride on every row in every format, so a JSON or
CSV consumer can segment either way (decision 4 of `runner_history.py`).*)

That segmentation is small, and it is the whole differentiator. Everything else in
this section is a table and a SELECT.

### 6a.4 It feeds a mechanism that already exists

`term_run_time` is the hard deadline and it is frozen. Run history is what tells
an operator where to set it, and which jobs have drifted under one. Degradation
detection is not a new concept here — it is the soft half of a mechanism the
oracle already implements, and it has been unusable only because the numbers were
scattered across run roots.

Whether AutoSys's own run-history surface (`autorep -r` for prior runs, and the
vendor's average-run-time notion behind its runtime alarms) sets a parity
expectation for a migrated estate is a dossier question, not a runner decision —
**[?]**, and it needs a citation sweep before anything is built to match it.

## 7. Retention — more than one clock

| what | retained for | why |
| --- | --- | --- |
| closed-period evidence: seals, attestations, receipts | the reachable seals and the newest attestation are floored; older ones are *held*; an archived period's receipt, attestation and sidecar are floored permanently | the closed book. `archive-inputs` deletes the period's WAL and a committed candidate's `staged_manifest.json` and `candidate.json`, and drops that period to the attestation-verified tier |
| run rows (§6a) | *as long as the period's WAL* — see below | they are folded on demand, not stored |
| run spools and job output | floored until the run is terminal and the last period that can reference it is attested; site policy after that | investigation, and they are large and `0600` |
| the decision index | not a retention clock — see below | |

Retention remains a business decision, as `deployment-runbook.md` §2 says, and
the operator flags are in its §2a. What changes is that there is more than one
clock and they are not interchangeable.

*(Corrected at build.)*

**Run rows do not outlive the WAL they are folded from.** This section wanted
them to, on the argument that a trend is worth keeping after the logs it was
computed from are gone. That argument needed §6a.1's materialize-at-the-seal,
which was withdrawn: `dsl41 runs` folds a row out of a period's own WAL every
time it is asked. So an archived period contributes no rows, and the tool says
so by name rather than answering shorter (`archived_coverage` in
`src/dsl41/runner_history.py`, DL-144). A pruned **spool** is the softer case:
the row survives on the journal's clock, and `clock_source` says so. Keeping
the trend past the WAL would need a materializer this build does not have.

**The decision index is not pruned on a clock at all.** The retry horizon
landed as a **soft gate on sealing**, not as a retention period: a boundary
committed within `retry_horizon_us` of the last admitted externally requested
attempt warns and needs `--force-seal`, and the override is recorded
(period-model §9; `retry_horizon_us` on `RuntimeProfile` in
`src/dsl41/period.py`, default 60 s). The index itself is log-local and a new
period opens with a new one, so nothing sweeps it. What the text removed at
DL-189 was guarding against — pruning dedup state while a retry can still
arrive — is answered by the gate rather than by a clock.

What may never be deleted is not a business decision and is itemized:
`src/dsl41/retention.py` computes the floor from one root, and `estate prune`
can reach only what a named rule licenses (period-model §11a, §12).

## 8. Authority — the gap ops would have hit first

*(Mostly closed. `docs/access-model.md` and DL-146…DL-148 are the answer, and
they arrived by a route this section did not predict. The argument is kept and
the outcome is stated after it.)*

The problem, as it stood. `control-protocol.md` §7 gap 2 was honest: there was
no authentication. The socket mode plus filesystem ownership was the whole
access-control model, `claimed_actor` was a claim, and `forced_by` recorded who
*said* they were asking (DL-111).

For a laptop and a single host that is a documented limit. For a booking center
under BCP governance it is the finding an audit opens with, because the two
questions an auditor asks about a manual intervention are *who* and *by what
authority*, and the answer to both was "whoever had the uid".

`concurrency-model.md` §6 already promised the resolution — *"the leader stamps
the authenticated principal"* — and stated its own blocker: there was no leader
that could authenticate one. This section predicted the store-backed term (S8a)
and the relay's principals (E15) would unblock it. **That prediction was
wrong, and the correction is worth keeping.** Neither was needed. The kernel
already authenticates a local peer, so DL-146 read the peer credential at
accept (`SO_PEERCRED` / `LOCAL_PEERCRED`), resolved it to a principal
`(realm, name, groups)`, and mapped it through one role map to three nested
tiers — read < ops < adm. When access is configured the server **overwrites**
`claimed_actor` with the canonical authenticated spelling before anything is
fingerprinted or logged, so `forced_by` stops being a claim.

Three qualifications an operator needs:

- **It is opt-in.** With no role map installed, the old model still stands and
  the claim passes through untouched.
- **It covers local peers only.** A web tier runs one service account per
  exposed tier behind the corporate proxy, so a receipt names the **service
  account, not the human in the browser**; the deferred seam is named
  `web-session-principal-v2` (`access-model.md` §9). A non-local principal is
  still open, and that is what D5's remaining gap is.
- **`supervisor.sock` is governed but not tiered** by v1 ruling: owner-`0600`
  plus a same-uid peer check, on the argument that anyone with the filesystem
  is adm by definition.

This section's pin does not survive as written:

> ~~Break-glass verbs (`evict --force`, `supervise shutdown`, bulk resolution of
> unknown outcomes) require an authenticated principal, and their records survive
> every seal.~~

Half of it landed and half of it was ruled against. Break-glass is **not** an
authorization tier: `FORCE_STARTJOB`, `CHANGE_STATUS`, forced eviction and
`force_seal` are all ops, and there is no fourth tier and no per-verb deny
overlay, because that would be a second policy axis. Break-glass is a
**receipt category** instead: the perimeter writes `privileged_admitted` for
each admitted ops-or-higher control request, **best effort and unsynced**, so
the admission stands when the receipt does not. That journal is deliberately
NOT in the WAL and therefore not carried by any seal — a policy decision is
not an engine input, and replay must not see policy. `supervise shutdown` is
not in it either: it goes to the owner-only supervisor socket. What survives
the seal is the *effect* of the act, on the rows: `forced_by` on an evicted
host row, `force_seal` and `forced_gate` on the `seal` record.

Whether four-eyes is required on break-glass remains the client's control
framework's call, not dsl41's.

## 9. Obligations

*Superseded.* The CM-24…CM-38 rows drafted here became `period-model.md` §13's
**PR-** series (namespace `PR-\d{2}[a-z]?`), rewritten against "what would a
plausible-but-wrong implementation still pass?". The table has grown since;
period-model §13 is the count. The run-history pair landed without a namespace
under DL-113. No number here is cited by code any more: `runner_history.py`'s
pure run-history fold cited **CM-37** and now cites DL-113, the entry that
landed that pair with no obligation row.

## 10. Decision-log entries this implies

*Superseded by `period-model.md` §15 and §16 for the mechanism.* The whole
period-model set landed: the period and the seal, the lineage fence and the
optional run root, `baseline_id` per period, the atomic `decision` record, the
capacity decomposition, the classification tiers, `catalog_hash` v2, the retry
horizon as a profile field, armed latches crossing a release, the SPAWN
idempotency protocol and the FW spool — DL-114…DL-136, with run history under
DL-113. Adoption was on that list and is retired unbuilt
(DL-138). Withdrawn rather than adopted: the HA plan's set (topology,
leadership, RPO=0, affinity, uncertainty, no-auto-reroute), DL-189. Two from
this document survive on
their own: **run-root exclusion and estate leadership are two mutexes** (§4a)
and **the local multi-executor rig is a third proving tier** (§4a.5).

## 11. Open questions

Continuing the runner E-series (`runner-design.md` §15, extended by
the withdrawn HA plan (DL-189) to E15).

- **E16** — seal cadence. *(Reframed. The text removed at DL-189 called this
  store-era only, on the reading that a period ends when a run root does.
  Period-model §1.1 removed
  that: one root holds many periods, so a daily seal is available on the file
  substrate now.)* Every boundary is an operator act — automatic sealing on a
  timer is a period-model §12 non-goal — and it costs a **restart**, not a new
  root. So the cadence is a real choice: is the boundary per estate, per
  booking center, or per regulatory period? The answer sets the replay bound,
  so it also sets the takeover time. Client question.
- **E17** — what `standby check` must assert before BCP governance will accept a
  green. Client question, and the answer probably has a form to fill.
- **E18** — does an A-classified boundary truth flip need an operator
  acknowledgement before the period opens, or is reporting it enough?
  *Narrowed by period-model §3.1: the classification and every A assumption
  are carried in the seal, so the record exists whatever the answer.*
- ~~**E19**~~ — *closed by period-model §10.3: a member changed while its box
  is executing is **R**, even when the member is INACTIVE; a box run never
  observes two versions of anything in its closure (PR-42).*
- ~~**E20**~~ — *closed by period-model §12a (DL-144, 2026-08-21), as a POLICY
  decision and not an observation: a seal-only archive **may** stand in for
  pruned inputs, under a named `archive-inputs` class with a durable receipt in
  front of every deletion, an itemized eligibility list and a permanent floor
  of three artifacts per archived period. "Verified" is now two named tiers,
  and an archived period stands at attestation-verified. It was never a live-
  instance question, and it is out of the runbook's list.*
- ~~**E23**~~ — *closed by period-model §11: `audit` runs the interpreter that
  produced the period and refuses otherwise, naming the version; old versions
  stay installable. And by §2.1's scope cut: an SM bump is not a transition.*
- **E21** — can a follower run on the file substrate, by tailing the WAL another
  process is appending? If yes, the simple single-host setup can have a warm
  standby and a readiness check without a database. If no, follower mode is
  store-only and §4a.2's table gains a fourth row. The WAL is append-only and
  line-framed, so the proposal is yes.
- **E22** — does a migrated estate expect parity with AutoSys's own run-history
  surface (`autorep -r`, and the average-run-time notion behind its runtime
  alarms)? A dossier question with a citation sweep in front of it, not a runner
  decision. **[?]**

## 12. Amendments this requires

*Superseded by `period-model.md` §15, which is the authoritative list.* One row
from the earlier table survives as this document's own: `runner_hosts.py`'s
*"One host, for now"* docstring scopes to "there is no relay", while the
local two-row rig (§4a.5) arrives before the relay does.

The other row is done: the `citation-index` row for `E\d{1,2}` now names
E1–E23.
