# Operations model — periods, the seal, and the carry

`docs/ha-deployment.md` answers *where the engine runs*. This document answers
*what an operator does to it, on an ordinary Tuesday and on a bad one*. It is
the same estate, read from the other side.

Status: **plan, not frozen — and the ops view only.** The *mechanism* this
document proposed — the period, the seal, the carry, the lineage fence, the
optional run root — is now normative-in-waiting in **`docs/period-model.md`**,
converged with `gpt-5.6-sol` after 33 adversarial rounds. Where this document
and period-model disagree, **period-model wins**. §1–§3 and §8a–§8b below are
kept as the *argument* that got there and are marked *superseded*; the
scenario catalogue (§5), the closed book (§6), run history (§6a), retention
(§7), authority (§8) and the deployment shapes (§4a) remain this document's.

**Peer-reviewed 2026-08-18** (`gpt-5.6-sol`, xhigh, read-only over the repo;
four rounds, converged). Round 4 acted on the repo owner's objection that
rolling the run root is clumsy and counter-intuitive: §8a.0 is the result and it
supersedes this document's original framing of the run root as the period
boundary. Four claims were withdrawn and are marked at the point
they were made rather than deleted: §1.3's "a seal does not require a quiesced
estate", §2.2's per-job release window, §8a.1's "only the last stage waits on
the store", and §1.1's IR-G analogy. The sweep gained four findings (G7–G10) and
the capacity fix gained its shape. Where a paragraph says *"corrected after peer
review"*, the older reading was wrong, not merely weaker.

## 0. The finding this document starts from

One run root today carries four different lifetimes, and they are forced to end
at the same instant:

| lifetime | what it holds | should end when |
| --- | --- | --- |
| **run** | one job execution: spool, logs, `run_id`, effect ids | the job ends |
| **period** | the open ledger tail: inputs, decisions, outbox | the books are closed |
| **baseline** | the catalog: JIL bytes, `catalog_hash`, manifest, roles | the estate changes |
| **estate** | job rows, globals, host rows, generations | the estate is retired |

`deployment-runbook.md` §6 collapses all four into one act: an estate change is
**stop → swap → new run root**, and a new run root is a new log, a new baseline
*and* a fresh oracle. Genesis seeds definition-time state only —
`initial_status` flags per SEM-24 and the catalog's declared globals
(`src/dsl41/oracle.py:261`). So every estate change silently resets:

- every runtime global back to its declared value, or to absent;
- every operator `ON_HOLD` / `ON_ICE` / `ON_NOEXEC` placed since the last change;
- every `last_end_at` and `status_at`, which is what lookback conditions read
  (`src/dsl41/oracle.py:608`);
- every `armed` latch (SEM-32, DL-54) and every box's `ran_members` (SEM-10);
- every `run_number` back to 0, so `runs/<job>.1` means a different run in each
  baseline and an investigator cannot tell them apart from the path.

**That reset is correct at a cycle boundary and wrong in the middle of one.**
For a nightly estate that starts INACTIVE every night, closing the books and
opening fresh *is* the semantics. For a booking center that carries state across
days — a global set by yesterday's reconciliation, a `s(job, "24.00")` lookback,
a hold placed on Friday for Monday's release — it is silent loss, which is the
one thing this project refuses everywhere else (DL-07).

Today nothing in the model says which of the two you are doing. This document
adds the concept that says it.

## 1. The period, the seal, and the carry

*Superseded by `period-model.md` §1–§3 (identities, records, the seal artifact
and its canonical form, the carried inventory). Kept as the argument.*

Accountants solved this. A ledger is not replayed from the founding of the
company to compute today's balance: periods are **closed**, a closing entry
becomes the next period's **opening balance**, closed periods are kept for audit
and never re-posted to, and a correction is an **adjusting entry** in the open
period rather than an edit to a closed one.

The mapping is exact enough to use as the design:

| accounting | dsl41 |
| --- | --- |
| journal entry | one admitted input (`input` / `advance` / `host` records) |
| ledger balance | the §3 authoritative state: job, global and host rows, the timer heap |
| trial balance | the cutoff report (`ha-deployment.md` §5) |
| closing the books | the **seal** |
| opening balance | the same seal, read forward |
| adjusting entry | `CHANGE_STATUS`, or any operator input, in the open period |
| restatement | **forbidden** — there is no verb that edits a closed period |
| the bound ledger book | a closed period: its seal, its inputs, its catalog |

**One record, two readings.** A seal closes period N and opens period N+1. There
is no second concept for "checkpoint" and no snapshot beside it — one record, or
this becomes the parallel model DL-91 exists to catch.

```
seal: {period_id, prev_seal_digest, closes_at_index, at,
       catalog_hash, state_machine_version, epoch, principal,
       state: {jobs, globals, hosts, timers, timer_seq, waiters},
       in_flight: [{job, run_number, effect_id, executor_id, generation}],
       unresolved: [...],        # the cutoff report, carried
       digest}
```

It is written by the leader, as one admitted input, in one transaction, under
the term that admits it — the same path every other input takes (§4 of
`concurrency-model.md`, frozen). It is not a background job and it is not a
maintenance script.

### 1.1 Four rules that make a seal safe

1. **A seal is an authoritative checkpoint, reproducible from its opening
   checkpoint and its period's inputs.** Say it that way rather than reaching
   for IR-G. IR-G is safe because nothing ever uses it as authority; a seal *is*
   used as authority — the next engine opens on it without replaying the closed
   period — so the analogy was cover, not argument. What keeps it honest is
   reproduction, not derivation: `dsl41 audit --period N` replays period N from
   its opening seal and refuses if the canonical form's digest differs. **That
   is why `audit` ships in stage 1** (§8a.2) and not later: a checkpoint nothing
   can reproduce is authoritative state with no proof, and shipping the carry
   first would mean three stages of exactly that.
2. **A closed period is never replayed.** Resume, takeover and audit all start at
   the last seal. Inputs below `closes_at_index` are archive.
3. **Nothing may be posted to a closed period.** An operator correcting history
   posts an adjusting entry in the open period. This needs no new verb: it is
   what `CHANGE_STATUS` already is.
4. **A seal names its catalog.** `catalog_hash` and `state_machine_version` ride
   in the record, so a closed period knows which JIL and which semantics produced
   it, and a carry across a catalog change is expressible rather than implied.

`prev_seal_digest` chains the periods. It costs one field and it makes the
archive **self-consistent** — which is not the same as tamper-evident, and the
earlier draft claimed the stronger word. Anyone who can rewrite a seal can
rewrite the chain head after it. Tamper-evidence needs an external anchor:
a signature, immutable retention, or an off-box copy of the head. The chain is
still worth its one field, because it detects the accident and the partial
restore; it does not detect the adversary.

**Canonical form.** The digest is computed over a canonical serialization, not
over incidental JSON bytes, or `audit` reports mismatches for a re-serialization
that changed nothing — which discredits the audit exactly when it must be
trusted. Normative, not an implementation note: maps sorted by a specified key
order; semantically unordered collections sorted by specified field tuples;
semantically ordered ones keeping their order; the timer heap serialized by
`(due, token)` rather than its incidental heap-array layout; `reservations`
sorted by bucket after duplicate-bucket rejection; sets such as `ran_members`
sorted; and exact encodings pinned for datetimes, numbers, Unicode and
whitespace. The record carries its own `seal_format_version`; nothing here may
rest on Pydantic's or Python's current serialization behaviour.

### 1.2 What the seal must carry, and one thing that is not obvious

The rows are easy: `JobRuntime`, `GlobalRuntime` and `HostRuntime` are frozen
Pydantic models (`src/dsl41/oracle_state.py:111`) and serialize as themselves.
The timer heap carries its ordering token and `_timer_seq`
(`src/dsl41/oracle_state.py:284`), which §3 already requires to be authoritative.

**The capacity pool's waiter order does not survive a seal, and today's argument
that it needs no token depends on replaying from genesis.** DL-86 left
`_CapacityPool` outside `RuntimeState` under two tested invariants, and defended
the missing ordering token like this: *"a waiter's rank is fixed at its QUE_WAIT
transition, which is itself a projected change, so replaying the transitions
replays the order."* That is true, and it is true **only while replay starts at
genesis**. A seal cuts the transitions away and leaves the rows, and the rows say
who is QUE_WAIT without saying in what order they queued. Two jobs waiting on
one pool would resume in an arbitrary order — which decides which one starts.

So the seal carries `waiters` explicitly, as an ordered list. The alternative —
a rank field on the row — is worse: it adds a projected field that moves on every
queue change, for a fact only the seal needs.

This is the general shape of the risk and worth stating once: **any invariant
whose proof is "replay reconstructs it" must be re-checked against a boundary
that does not replay.** The timer heap passed because DL-86 already gave it a
token. The waiter queue did not.

### 1.3 What a seal cannot do

*(Corrected after peer review. The first bullet said the opposite and was
store-era reasoning applied to a file substrate; the second contradicted §8b.1;
the third persisted a projection this project's sibling plan forbids
persisting.)*

- **On the file substrate it CANNOT cross a live run, and therefore requires a
  quiesced estate.** The period boundary is also a *process* boundary: the
  supervisor is one per run root, `SupervisorClient` connects to
  `<run_root>/supervisor.sock`, and `reattach` is keyed within one root. A
  new-root engine cannot LIST, signal or await work the old root's supervisor
  owns. So an `in_flight` summary does not help and the outbox is not the whole
  problem — there is no channel to the process. Quiescence is the stage-1
  contract (§8a.2), and the multi-root bridge that lifts it is stage 4. This
  costs less than it reads: `deployment-runbook.md` §6 already drains before a
  re-baseline, so stage 1 imposes no new release-window tax.
- **It cannot carry the decision index, so it must not seal inside the retry
  horizon.** The earlier draft said the index cannot be pruned and then listed
  it as not carried — a straight contradiction. Carrying it does not work
  either: `_by_index` is log-local and a new root resets indices, so old and new
  decisions collide. And a retry cannot survive a seal in any case, because
  `parse_envelope` rejects a foreign `baseline_id` before dedup is ever
  consulted, while `baseline_id` is inside the fingerprint. The resolution is a
  precondition, not a protocol change: **`retry_horizon = N`**, measured since
  the last admitted externally requested mutation, with every admitted attempt
  decided and nothing awaiting admission or a response. N must be named
  normatively and clients told that retry guarantees expire after it — which
  weakens an unbounded promise `control-protocol.md` §3 makes today, so it takes
  its own decision-log entry.
- **It cannot resolve an unknown, and it does not carry one either.** A run
  whose executor went dark stays unknown until evidence or an operator resolves
  it. But `unresolved` is a **projection** and persisting it as opening truth
  would break the rule `ha-deployment.md` §5 states for exactly this — derived,
  never persisted as truth. It does not need carrying: `outcome_unknown` is
  derived from the bound executor's quarantine plus the absence of evidence, and
  both of those are in state the seal already carries. The cutoff report is
  regenerated in the new period, not copied into it.

## 2. Re-baselining is a seal with a catalog change

*Superseded by `period-model.md` §10 (classification: three tiers, the graph,
the named cases) and §6–§8 (cutoff, the seal operation, preconditions). Note
§2.2 below was withdrawn and stays withdrawn.*

This is the payoff. Once a period boundary exists, an estate change stops being
its own procedure:

| operation | what it is |
| --- | --- |
| initial JIL release | open period 1 with an empty seal, under catalog C1 |
| incremental JIL change | seal under C1; open the next period under C2, from the carry |
| rollback | the same, with C1 again — *forward* from the current carry, never back to an old run root |
| engine version upgrade | seal; open under the same catalog at a new `state_machine_version` |
| routine restart / crash | not a seal at all: resume inside the open period |

Rollback deserves its sentence. Today rollback means "the previous tag and
another fresh run root" (`deployment-runbook.md` §6), which throws away
everything that happened under the bad release. Under the period model, rollback
carries forward from the state the bad release actually produced, which is the
only correct reading — the jobs that ran, ran.

### 2.1 The re-baseline report

A catalog change needs the same discipline the UC backend already applies to a
mapping: classify every row, refuse what cannot be carried, and record an
assumption for what can be carried only under one. The vocabulary exists — reuse
it rather than invent a second one.

| class | case | what happens |
| --- | --- | --- |
| **carry** | definition semantically unchanged | the row carries as-is |
| **new** | no carried row | genesis seeding applies (SEM-24 flags, declared globals) |
| **removed** | carried row, no catalog entry | retained as a ghost, listed, never silently dropped. L001 already refuses a *condition* that references it (`src/dsl41/lint.py:15`), so retention is for audit, not for truth |
| **A** | changed, carryable under a stated assumption | carried, assumption recorded: condition changed (boundary truth may move), schedule changed (timers are recomputed, not carried), `initial_status` changed while the carried row disagrees |
| **R** | cannot be carried | refuses the re-baseline while it is live: the job is RUNNING and its definition changed; its box membership changed while the box is running; its machine or affinity role changed while an effect is bound to the old one |

**Genesis seeding must not overwrite the carry.** `oracle.py:261` seeds
`initial_status` and declared globals at construction. Under carry-forward it
applies to **new** rows only, or every re-baseline silently clears every operator
hold — the §0 failure with extra steps. A job whose `initial_status` changed
while its carried row disagrees is an A-row, and the operator is told which won.

**The boundary truth diff is computable, and the machinery exists.** Evaluate
every condition against the carried state under C1 and under C2, and report the
jobs whose readiness flips at the boundary. That is `equiv` tier a/b over two
catalogs at one state (implementation order §8) — not a new evaluator, which is
the only acceptable answer here.

### 2.2 The window does not get smaller — the loss does

*(Withdrawn after peer review.)* An earlier draft claimed the gate becomes
per-job — *"a weekly release touching twelve jobs of eight hundred needs those
twelve quiet, not the estate"*. **That is false on the file substrate**, for
§1.3's reason: the supervisor handoff forces a full drain whatever the diff
says. The window shrinks only in what must be *verified*, not in what must be
*drained*.

Two independent things also killed it as stated. The R-gate as drafted does not
catch a box run spanning two catalogs: box B RUNNING under C1, member M not yet
started because its condition is false, C2 changes M's command but not its
membership — M is INACTIVE so "changed job is live" does not fire, membership is
unchanged so the box rule does not fire, and M later starts under C2 inside B's
C1 execution. **E19 is open about exactly this**, so §2 cannot claim the gate is
safe while it is. And "live" cannot mean RUNNING: QUE_WAIT, armed latches,
deferred timers and pending effects are all latent execution intent and each
needs its own classification.

*(Partially restored in round 4.)* The per-job gate returns once the run root
stops rolling, because the drain it was killed by was the supervisor handoff.
It returns **narrower than first claimed**, and the wording matters:

> In **detached** mode, a catalog transition need not drain jobs outside the
> transition's transitive R-classified live closure.

Three qualifications ride with it. Tethered mode is excluded — the restart kills
live commands regardless. Live FW runs are restarted in-engine rather than
externally reattached, so a change touching one stays R. And the closure is
*transitive*: live boxes, reservations, global declarations, success-code
interpretation, dependencies, calendars, routing and pending effects all
participate, which is why this can never be stated as "only the changed job must
be quiet".

What survives regardless of mode is the part that mattered: **the release stops
resetting the estate.** Globals, holds, `last_end_at`, `armed`, `ran_members` and `run_number`
cross the window. That was always the value; the smaller window was a bonus that
turned out not to exist.

The pre-window rehearsal does improve: `rehearse` can start from the real carry
instead of from cold, which is the difference between rehearsing this estate and
rehearsing a fresh one.

E9 still applies to a window that spans a tick. The seal's own cutoff is a
different mechanism and is specified in §8a.2.

## 3. The seal is the only place a version may move

*Narrowed by `period-model.md` §2.1: a transition may change the catalog and
the runtime profile but **not** `state_machine_version` — an SM bump stays a
new estate, as today. "An upgrade keeps estate state" holds for the common
upgrade that does not move the SM version.*

`deployment-runbook.md` §7 says to treat an engine upgrade like an estate change,
because leader eligibility is an exact match on `state_machine_version` and
replay would otherwise cross a semantic change. That rule is right, and the
period model turns it from a caution into a boundary:

> Replay never crosses a seal, so a seal is the only point at which
> `state_machine_version` may change.

Two consequences an operator can act on:

- An upgrade is **drain → seal → upgrade → open**, and the estate keeps its
  state. Today it keeps nothing.
- The seal's `state` becomes the **only cross-version compatibility surface** —
  one schema, versioned, with a one-way migration per bump. That is a far smaller
  contract than "every historical input must stay re-interpretable under the new
  semantics", which is what resume-compatibility means today and why the runbook
  declines to promise it.

Wrapper and supervisor skew stays a separate question: a detached supervisor
outlives the engine by design (DL-79), so an upgrade window must still confirm
nothing it is about to redefine is running under the old wrapper.

## 4. Leadership is an ops act, not only an election

`ha-deployment.md` §2 allocates the term by appending it:

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
near zero. It is not proposed here. `ha-deployment.md` §6 declines snapshots
until a *measured* takeover time demands them, and the same discipline applies:
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

One `flock` does two different jobs today:

- it excludes a second process from the **run root's filesystem artifacts** —
  spool directories, `control.sock`, `supervisor.sock`, the journal file;
- it allocates the **estate's term**, because the epoch is written under it
  (`concurrency-model.md` §1, DL-100).

Under a shared ledger those separate cleanly. The run root stays local and keeps
its lock. The term moves to the store. On one box both apply; over the wire only
the term is shared, because each host has its own run root.

**That separation is what protects the simple setup.** The flock is kernel-
released when the holder dies, `kill -9` included, with no expiry to renew — §1
is explicit that it is *"not a lease"*. A store-backed term has no such property:
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

1. `LOCAL_EXECUTOR_ID` is a constant and `seed_local_executor` is the table's only
   writer (`src/dsl41/runner_hosts.py:56`), so the routing table holds exactly one
   row. `concurrency-model.md` §8 says so about itself in its DL-111 amendment:
   *"what is missing is a second row to point them at."*
2. **`--as-machine` and `executor_id` are different identities and only the first
   is settable.** `--as-machine` is preflight identity — which `machine:` values
   this runner answers to (DL-52). `executor_id` is routing identity — which
   table row an effect binds to. Two executors on one box need both, and
   `dsl41 run` has no flag for the second.
3. `dsl41 journal` seeds genesis with `LOCAL_EXECUTOR_ID` hardcoded
   (`src/dsl41/cli_run.py:855`). The comment there is guarding the right thing — a
   replay onto a table without this engine's own executor decides *"no such
   host"* where the run decided otherwise — one step before the case that breaks
   it. The moment a run can name a different executor, offline replay must read
   it from the opening `segment` record.

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
  and a test rig — never a resilience story. The pair in `ha-deployment.md` §1 is
  what supplies resilience.
- **One clock.** `T_skew` in §8's eviction bound is inert locally and real over
  the wire.
- **One filesystem.** A quarantined local executor is still inspectable; a dark
  remote one is not, which is the whole of `ha-deployment.md` §5.
- **No partition.** The hardest class — E12, split brain, STONITH — is precisely
  the one the local rig cannot produce.

Ops details that follow from co-tenancy and are worth one line each: run roots,
control sockets and supervisor sockets are per run root, so nothing collides by
construction; `dsl41 serve` needs one port per estate; and `_CapacityPool` is
per engine, so nothing arbitrates a box-wide resource across estates. That last
one is identical over the wire — a standing scope boundary (the `Qr` series), not
a break in the isomorphism.

## 5. The scenario catalogue

What "support clean" means per row: there is a written procedure, its
preconditions are machine-checked rather than remembered, every step is recorded
in the ledger, and an operator can tell success from partial success without
reading a WAL.

### A. Installation and lifecycle

| # | scenario | today | under the period model | gap |
| --- | --- | --- | --- | --- |
| A1 | initial install, one host | `deployment-runbook.md` §1–§3 | unchanged | — |
| A2 | provision the standby | not covered | same install, boots as follower (§4) | follower mode |
| A3 | standby readiness verification | not covered | `standby check`, run continuously | the verb, and what BCP wants in it (E17) |
| A4 | engine version upgrade | fresh run root, state lost (§7) | drain → seal → upgrade → open (§3) | seal-state schema + migration |
| A5 | OS patching / host maintenance | stop the engine | `host drain` the executor, work finishes, engine keeps leading | — (frozen, §8 of concurrency-model) |
| A6 | estate decommission | delete run roots | final seal, archive the chain, retire the estate row | retention policy (§7) |

### B. Estate content

| # | scenario | today | under the period model | gap |
| --- | --- | --- | --- | --- |
| B1 | initial JIL release | fresh run root | period 1, empty seal | — |
| B2 | incremental change (add / remove / modify) | full quiesce, all state lost | seal → classified diff → open under C2 | the diff classifier, the carry |
| B3 | emergency hotfix, mid-cycle | not supportable without losing the night | per-job R-gate; refuse only if the touched jobs are live | the R-gate |
| B4 | rollback | previous tag, fresh root, night discarded | forward from the carry under C1 (§2) | — |
| B5 | calendar / holiday change | a catalog change, but easy to think of as config | it *is* a catalog change: firing dates move | say so in the runbook |
| B6 | properties / placeholder change | changes post-placeholder JIL, so changes the hash | same as B2 — this surprises people | say so in the runbook |
| B7 | affinity role remap | — | route-table change under epoch/CAS; visible to later runs only (CM-18) | `ha-deployment.md` S8b |

### C. Running the cycle

| # | scenario | today | under the period model | gap |
| --- | --- | --- | --- | --- |
| C1 | ordinary night | frozen | unchanged | — |
| C2 | closing the books | does not exist | the seal, at the estate's own cutoff, in the estate's own zone (SEM-35) | the verb and the schedule |
| C3 | cutoff with unresolved runs | does not exist | the cutoff report is the trial balance; carried in the seal | E13 |
| C4 | missed ticks over downtime | E9 skip-and-report, journaled | unchanged | — |
| C5 | deliberate catch-up after downtime | explicit `FORCE_STARTJOB`s | unchanged; the seal makes "what did we skip" answerable from the `drop` records in one period | — |

### D. Investigation

| # | scenario | today | under the period model | gap |
| --- | --- | --- | --- | --- |
| D1 | "why has X not started?" | `explain`, `deps`, `timers`, `plan` — frozen | unchanged | — |
| D2 | post-hoc, current period | `dsl41 journal` replay | unchanged | — |
| D3 | post-hoc, closed period | run root may be gone or archived | the closed book: seal + inputs + catalog (§6) | manifest bytes in the store |
| D4 | across a failover | the record splits across run roots | one ledger per estate: the trace is continuous across hosts | — |
| D5 | regulator: what ran, when, under which definition, on whose authority | partially answerable; authority is a *claim* | the chained seal answers three of four | authentication (§8) |
| D6 | "is this job degrading?" | facts exist per run root; no key, no index, no query | the run table, segmented by `catalog_hash` (§6a) | the projection and the `runs` verb |
| D7 | "what did last night cost us?" — elapsed per box, per wave | walk the spools | one query over the period's run rows | as D6 |

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
| E8 | **break glass**: `evict --force` | `host evict --force` | no | the one path that can double-run; attributed, not authenticated |
| E9 | **break glass**: supervisor shutdown | `supervise shutdown` | no | needs the engine stopped and the lease lapsed |
| E10 | resolve an unknown outcome | operator STATUS with evidence | no | `ha-deployment.md` §5, CM-22 |
| E11 | bulk-resolve at cutoff | over the cutoff report | no | E13 |

**Break-glass must survive the seal.** An incident that can be conflated away is
an incident that will be. A seal carries a non-zero count of forced actions and
unresolved runs into the next period's opening state until each is explicitly
acknowledged — the accountant's rule that you do not close over an unreconciled
item, applied literally.

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

1. the **seal** that opened it and the seal that closed it, chained by digest;
2. every **input** between them;
3. the **catalog** it ran under — the post-placeholder JIL bytes, not a tag;
4. the **principals** who asked for each externally requested input.

Item 3 is the one that does not work today. Offline replay is hash-gated against
the estate *at its recorded paths* (`deployment-runbook.md` §4), so the byte-exact
copies in the run root's own input bundle do not pass from anywhere else — a deliberate defer,
because relocation-independent hashing would orphan every existing journal.

The store fixes this without touching the hashing rule: **store the manifest
bytes, content-addressed by `catalog_hash`**, and materialize them at the
recorded paths in a scratch directory at replay time. The gate is satisfied
because the bytes and the paths are both what it expects. Nothing about
`SourceSpan.file` changes.

Item 4 does not work today either, and that is §8.

## 6a. Run history — the fact table the ledger already contains

**Today the facts survive and the history does not.** Every timing an operator
would want is written, in three places, and none of them is queryable across a
baseline:

- the `dispatch` record — `{job, run_number, wrapper_pid, run_dir, started_at}`;
- `spawn.json` — `started_at`, written by the process that spawned;
- `status.json` — `ended_at`, `outcome`, `exit_code`, written by the process that
  waited (`supervisor-protocol.md`, frozen).

What is missing is not the data. It is a **key, an index and a query**:

- `run_number` resets to 0 at every re-baseline (§0), so `runs/job.1` names a
  different run in every run root and nothing identifies a run across them;
- run roots are not indexed, so "this job's last twenty runs" means walking N
  directories, each hash-gated to its own catalog;
- `JobRuntime` holds the latest status only — one row, no history
  (`src/dsl41/oracle_state.py:111`);
- `trace` is the current run's, in memory.

So "is this job degrading?" is answerable in principle and unanswerable in
practice, and it gets worse with every JIL release — which is exactly the
frequency at which an operator asks it.

**The carry is the precondition.** Once `run_number` survives a re-baseline (§2),
`(estate, job, run_number)` is a stable primary key for the life of the estate,
and the spool path becomes globally unique. Run history is not a separate feature
from the period model; it is the second thing the period model makes possible.

### 6a.1 Derived, never authoritative

The ledger already holds every fact. So the run table is a **projection**, on the
same rule as IR-G and as the seal itself: regenerate it, never edit it, and never
treat it as the source of a truth the inputs disagree with.

**It materializes at the seal**, before the period's inputs become archivable
(§7). A seal therefore produces two derived things — the **carry**, which is
state, and the period's **run rows**, which are history. Both are verifiable by
replay while the inputs still exist, which is the only window in which either can
be checked.

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

*(Amended by DL-113, at build.* Built without waiting for §1–§4's seal:
`dsl41 runs` is a plain offline CLI verb over one or more run roots' existing
`journal.jsonl` + estate files + spool, computed on demand rather than
materialized at any write time — there is no seal to materialize it at, and
no writer of a new record kind. The row therefore carries neither `estate`
nor `period_id`: `run_number` resets at every re-baseline exactly as §0
already says, so the caller names which run roots to combine and
`catalog_hash` — not `run_number` — is what actually tells two runs of the
same job apart across a baseline change. `RunRow` adds one field this sketch
did not have, `clock_source`, naming whether a row's timing came from the
wrapper's own spool or fell back to the journal (decision 1 of
`src/dsl41/runner_history.py`'s module docstring). "The oracle already
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
every run row carries the `catalog_hash` it ran under, and the history query
**segments the series at every baseline boundary**. This is the same error class
`equiv` exists for on the semantic side — a comparison that crosses a catalog
change is not a comparison — applied to the operational side, where nothing
guards it today.

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

## 7. Retention — four clocks, not one

| what | retained for | why |
| --- | --- | --- |
| sealed periods (inputs + seals) | the audit horizon, years | the closed book |
| run rows (§6a) | longer than the spools they summarize | one row per run is small; the trend is the point, and it outlives the logs |
| run spools and job output | the operational horizon, weeks | investigation, and they are large and `0600` |
| the decision index | the client retry horizon, hours | dedup only matters while a retry can arrive (§1.3) |

Pruning the decision index at a seal is the obvious bug and this table exists to
prevent it.

Retention remains a business decision, as `deployment-runbook.md` §2 says. What
changes is that there are four of them and they are not interchangeable. The run
rows deliberately outlive the spools they summarize: a trend is worth keeping
after the logs it was computed from are gone.

## 8. Authority — the gap ops will hit first

`control-protocol.md` §7 gap 2 is honest: there is no authentication. The socket
mode plus filesystem ownership is the whole access-control model, `claimed_actor`
is a claim, and `forced_by` records who *said* they were asking (DL-111).

For a laptop and a single host that is a documented limit. For a booking center
under BCP governance it is the finding an audit opens with, because the two
questions an auditor asks about a manual intervention are *who* and *by what
authority*, and today the answer to both is "whoever had the uid".

`concurrency-model.md` §6 already promises the resolution — *"the leader stamps
the authenticated principal"* — and states its own blocker: there is no leader
that can authenticate one. The store-backed term (S8a) and the relay's principals
(E15) are what unblock it. The ops consequence is worth pinning now, before the
relay's principal scheme is designed around a different requirement:

> Break-glass verbs (`evict --force`, `supervise shutdown`, bulk resolution of
> unknown outcomes) require an authenticated principal, and their records survive
> every seal.

Whether that also means four-eyes on break-glass is the client's control
framework's call, not dsl41's — but the record has to be able to carry a second
principal if the answer is yes, and that is a schema decision, not a policy one.

## 8a. The seal programme — stage order

*Superseded. `period-model.md` is one unit with no staged exposure: nothing
ships incrementally, and its §13 obligations and §14 worked estate are the risk
control. §8a.0's conclusion — the directory is not the baseline — survives
verbatim as period-model §1.1; §8a.1's "does not need the store" survives as
the mathematical claim only.*

### 8a.0 The directory is not the baseline

*(Rewritten after round 4 of peer review, which started from the repo owner's
objection: rolling the run root daily and at every release is clumsy and
counter-intuitive, and folder structure should be optional hygiene rather than a
limiting feature of the scheduler. The objection is correct. This document had
accepted the constraint instead of attacking it.)*

**What enforces one catalog per log is a value at position zero, not a
directory.** Two sites: `start_run` refuses a run root that already holds a
journal, and `check_leader_eligibility` compares the **header's** `catalog_hash`
and `state_machine_version` against the current build. The header is written once
at genesis. Everything else about the folder is incidental.

The landing position separates five identities this design had conflated:

| concept | lifetime and purpose |
| --- | --- |
| **estate root** | stable sockets, supervisor, locks, `runs/`, the operator's path |
| **catalog period** | a fresh `baseline_id`, catalog hash, machine version, cutoff |
| **WAL segment** | the corruption, retention, backup and archive unit |
| **seal** | the verified recovery state between segments |
| **execution** | `run_id`, monotone `run_number`, and its start-period identity |

> The directory is an operational container. Catalog periods are semantic
> boundaries. WAL segments are retention and corruption boundaries. **Rolling the
> directory is optional archival hygiene.**

**`baseline_id` must rotate per period, and this is load-bearing.** Keeping one
because the physical ledger is continuous opens a correctness hole: a client
reads job revision 7 under C1; C2 opens in the same root under the same baseline;
the catalog change need not touch that row or move its revision; the client then
submits `(baseline=B, expect=7)` and it is accepted against C2 semantics. Today
the engine takes its baseline from the header and keeps it for life. Frozen v2's
wire shape does not change — the *definition* does, from "physical log identity"
to "semantic period identity". The retry horizon (§1.3) is what makes refusing a
late C1 retry after that rotation coherent rather than arbitrary.

**One immortal `journal.jsonl` is not the answer**, and the reasons are in
shipped code rather than in scale anxiety: `read_journal` reads the whole file
into memory and refuses the entire journal on one corrupt interior line; decision
and outbox reconstruction scan every record; reconciliation scans every
historical dispatch plus the whole `runs/` directory; and the supervisor keeps
every completed run in memory and returns all of them from `LIST`, with no
eviction (G11). Under rolling roots each of those is bounded by one night. Remove
the rolling without segmenting and they are unbounded from day one.

**What makes rolling genuinely optional** rather than nominally optional is one
condition: the seal and opening format must be *identical* for both
continuations — continue under the same root, or open a fresh root from that
seal. Two formats would be two semantic paths and the option would be a fiction.
A physical roll taken while jobs are live still needs the stage-4 bridge or
quiescence; an ordinary same-root transition needs neither.

### 8a.1 Neither does it need the store

Two facts, both already true, decide this.

**The run root boundary is already a period boundary.** A new run root is a new
log, replayed from its own header. So "replay starts at the last seal rather
than at the beginning of time" — the thing §1.1's rule 2 asks for — is *already
what happens* on the file substrate. What a new run root loses is not the bound;
it is the state (§0). The seal has to supply the state, not the bound.

**The catalog seed is already the genesis input.** `Oracle.__init__` opens an
input transaction, seeds SEM-24 flags and declared globals, and commits it —
DL-87's comment says so in as many words: *"the catalog seed IS an input -- the
genesis one."* A carried opening is therefore not a new concept in the oracle.
It is the same genesis input, seeded from a seal for rows that have one and from
the catalog for rows that do not.

So the seal, on one host, is **the handoff artifact between two run roots**:

```
dsl41 seal --run-root <old>        # closes it: sidecar state + a `seal` record
dsl41 run  --from-seal <old>/seals/<id>.json --run-root <new> <estate>...
```

No store, no transaction manager, no HA. `docs/ha-deployment.md`'s S8a is not a
prerequisite of any of this.

*(Narrowed after peer review.)* What survives is the **mathematical** claim: no
shared database is required for a file-based checkpoint, because the missing
state can be carried locally. What does **not** survive is the staging claim
that only the last stage waits on the store — stage 1 has substantial
prerequisites of its own (the quiescence predicate, the cutoff barrier, the
retry horizon, and stitching the shipped run-history projection across the
boundary), and none of them is a store, but none of them is free either.

**Durability without a transaction.** The state goes in a sidecar written with
the liturgy the spool already uses (same-directory temp, fsync, rename, fsync
dir), and *then* the `seal` record is appended to the journal. Crash between the
two and the sidecar is orphaned, which is harmless because no record names it.
The record landing means the sidecar is already durable. That is the same
write-ahead ordering `dispatch` and `spawn.json` already have, not a new one.

### 8a.2 Stages

| # | stage | delivers | needs |
| --- | --- | --- | --- |
| 1 | **the period transition, in place** — schema + canonical form, rotating `baseline_id`, content-addressed catalogs, segmented WAL, resume-from-latest-seal, the cutoff barrier, the retry horizon, **`dsl41 audit`**, and DL-113 made period-aware | the provable core, with its proof | — |
| 2 | **the carry across a catalog change** — the classifier over a transitive blast radius, genesis-for-new-rows-only, the boundary truth diff | **a JIL release stops resetting the estate** | 1 |
| 3 | **the version boundary** — `state_machine_version` may move only across a seal | an upgrade keeps estate state (§3) | 1 |
| 4 | **the physical roll while live** — the multi-root execution bridge: each live run's old supervisor endpoint and spool root carried until it drains | rolling the directory without draining. Needed ONLY for a physical roll: an in-place transition (stage 1) never crosses a supervisor | 1 |
| 5 | **the seal in the store** — many periods in one ledger, so "replay starts at the last seal" becomes a mechanism rather than a side effect of a new run root | bounded replay across a takeover | S8a, and it gates S8e |

`audit` moved into stage 1 and the closed book's remaining parts (chain
retention, archive policy) ride with it. Stages 2, 3 and 4 are independent of
each other and all three only need 1.

**Quiescence is now mode-dependent, not universal.** The multi-root supervisor
problem (§1.3) dissolves for an in-place transition: on a stable root the
supervisor never rolls, detached commands stay addressable, and resume reattaches
them from `LIST`. But *"live runs would never notice"* is false in **tethered**
mode, which is the CLI default — stopping the engine cancels live jobs, and only
detached deliberately leaves them running. So:

- **detached**: no full drain. A catalog transition need only quiesce the
  transition's transitive R-classified live closure (§2.2).
- **tethered**: a restart still kills live commands, so a full drain remains
  until there is a hot reload or a mode change.
- **both**: a short admission and cutoff barrier is still required. "No full
  drain" does not license appending a period record concurrently with controls
  and ticks.

The predicate below is therefore the **physical-roll-while-live** gate (stage 4)
and the tethered gate, not a universal precondition. Each item is mechanically
checkable, and a failure refuses rather than proceeds:

- `outbox.pending() == []` — pending, not an unused outbox; resolved effects
  stay in the closed journal;
- no job STARTING or RUNNING, boxes and FW included;
- no live or reaping adapter task;
- no pending SPAWN or KILL, and no indeterminate KILL whose target might exist;
- every supervisor's `LIST` is empty — and **if a supervisor is unreachable,
  quiescence is unprovable and the seal refuses**, which is what forecloses the
  terminal-row/pending-KILL orphan;
- the reconciliation sweep over journal dispatches, spool directories and
  supervisor `LIST` finds no surviving execution;
- `retry_horizon` elapsed (§1.3).

An **offline** sealer, after a crash, must first run the existing same-root
recovery barrier. It may not replay rows, observe "terminal", and seal — a
recorded kill still has to be re-driven first.

**Stage 1's cutoff barrier.** Quiescence does not settle which same-instant
ticks were consumed, and `Scheduler._next` cannot answer it (G7). The barrier
is an ordering, and it carries a watermark rather than a snapshot:

1. quiesce scheduled triggers and drain executions;
2. stop accepting new external mutations;
3. choose the cutoff instant **T**;
4. admit every scheduler tick due at or before T;
5. advance the oracle through T, firing every due semantic timer;
6. drain the resulting synchronous inputs and effects;
7. re-check the full quiescence predicate — if steps 4–5 started work despite
   the holds, **refuse** and let the operator drain again;
8. append the seal as the final record at T;
9. open the next period's scheduler strictly after T.

The only carried evidence is `scheduler_admitted_through: T`. It states what
happened instead of snapshotting derived state, and it settles stage 2's
ownership question in one line: **C1 owns every tick ≤ T, C2 owns every tick
> T**, so a schedule newly introduced by C2 cannot retroactively fire at T.

### 8a.3 What must be frozen before stage 1 writes code

1. **The seal record schema.** §3 makes it the only cross-version compatibility
   surface, so it is the one thing here that is expensive to get wrong.
2. **The carried-state inventory** — swept in §8b, which found five more gaps
   than §1.2's one.
3. **Where the state lives**: sidecar plus record, write-ahead ordered (§8a.1).
4. **`run_number` continuity.** Carrying it makes `runs/<job>.<n>` unique for
   the life of the estate, which is a real gain for investigation. It is still a
   decision, because it changes what a spool path means.

### 8a.4 The classifier needs more than the fingerprint

Stage 2 needs "did *this job's* definition change", and DL-113 built part of
that for the run-history break line: `period.job_fingerprints` — written in
`runner_history` as `_job_fingerprints` and lifted by DL-131 — sha256 over one
job's lowered IR with `span` keys stripped.

*(Narrowed after peer review.)* It is **not sufficient**, and the primitive's
own docstring says why — it is "NOT a definition diff". It hashes
`catalog.jobs[name]` and nothing that job depends on, so a changed resource
amount, calendar, global declaration or machine mapping alters a job's behaviour
while leaving its serialized `JobIR` byte-identical. The classifier needs a
**transitive blast radius**, not a per-job hash.

That is not new machinery either: `derive.py` already builds the graph and the
`deps` verb already serves a blast-radius view (upstream, globals, downstream,
box containment). The fingerprint stays useful as the leaf test inside it.

It also inherits that primitive's stated limit — it fingerprints the
post-placeholder definition — but stage 2 is comparing two catalogs the operator
is deliberately swapping between, which is the case where placeholders are
stable and the fingerprint says what it claims.

What the classifier adds on top is the part a hash cannot give: *how* it
changed, and therefore whether the carry is a **carry**, an **A** or an **R**
(§2.1).

### 8a.5 Two seals, one record

A seal with no catalog change and a seal with one have the same record and
different preconditions, and collapsing that distinction would hide the only
precondition that matters:

- **no catalog change** — no precondition. It is a handoff, and on the file
  substrate an operator only reaches for one at an upgrade (stage 3).
- **catalog change** — refused while any R-classified job is live (§2.2). Not
  the whole-estate quiesce the runbook has today.

The daily "close the books" cadence E16 asks about is a **store-era** trigger:
on the file substrate a period ends when a run root does, so sealing daily would
mean restarting daily. That is worth saying before someone builds a timer for
it.

## 8b. The state-inventory sweep

*Superseded by `period-model.md` §3.3 (carried / not carried, with the
reconstruction rule for each derived item) and §5 (the capacity decomposition,
which is §8b.4's "right fix"). G1–G11 below are the findings as made; period-
model carries their resolutions.*

Every piece of live state, walked against one question: **is this
reconstructible from the rows a seal carries, or only from the transitions a
seal cuts away?** Read against the code, not the docs
(`oracle_state.py`, `oracle.py`, `capacity.py`, `runner.py`,
`runner_admission.py`, `runner_effects.py`, `runner_scheduler.py`).

§1.2 found one gap by inspection. The sweep found five more, and two of them
are worse than the one that prompted it.

### 8b.1 The result

| state | where | verdict |
| --- | --- | --- |
| `_jobs`, `_globals` | `RuntimeState` | **carry** — frozen models, in canonical form (§1.1) |
| `_hosts` | `RuntimeState` | **carry the durable routing fields, reset `last_contact` — G8** |
| `_timers`, `_timer_seq` | `RuntimeState` | **carry** — already authoritative, and §3's ordering token means the heap's cross-job firing order survives |
| `_snapshots`, `_in_input` | `RuntimeState` | transaction scratch, empty between inputs |
| `_bucket_cap` | `CapacityPool` | derived from the catalog — but see **G6** |
| `_bucket_used` | `CapacityPool` | **carry — G1** |
| `_held` | `CapacityPool` | **carry — G2** |
| `_waiters`, `_waiter_seq`, `_enqueue_counter` | `CapacityPool` | **carry — G3**, and **G4** |
| `_referencers` | `Oracle` | derived from the catalog |
| `_trace`, `_emitted`, `_queue`, `_in_wake` | `Oracle` | transient or derived; the trace is audit, not state |
| `_now` | `Oracle` | **carry — G5** |
| `frontiers` | `Engine` | per-log: a new log starts at 0. Its `at` is G5's fact, not a second one |
| `outbox` | `Engine` | **empty by precondition in stages 1–3 — G9**; carried in full at stage 4 |
| `decisions` | `Engine` | not carried, and not carryable: `_by_index` is log-local. The retry horizon is the answer (§1.3) |
| `drops`, `deduped`, `refusals` | `Engine` | per-run audit lists |
| `_live`, `_dispatched`, `_reaping`, `_activity` | `Engine` | live process bookkeeping, rebuilt by the §7 reconciliation ladder |
| `Scheduler._next`, `_CalCache` | `runner_scheduler` | **not fully derived — G7.** The cutoff watermark replaces it |
| `_trace` | `Oracle` | transient *for the oracle*, and **not** for its shipped consumer — G10 |

### 8b.2 The findings

**G1 — consumed capacity units are held by no row, and a seal would refund
them.** `CapacityPool.release` decrements `_bucket_used` only under policy
`completion`, or under `success` when the job actually succeeded. Units under
`never` — `res_type: D`, or `FREE: N` — and `success` units on a failed
terminal stay in `_bucket_used` while `_held.pop(job)` has already dropped the
job. So a depletable resource's spent units are recorded **nowhere in the
rows**. Recomputing `_bucket_used` at a seal by summing the demand of RUNNING
jobs, which is the obvious implementation and the one DL-86's invariant
invites, would silently **refill every depletable resource in the estate** —
SEM-16's entire meaning, inverted, quietly. This is the most dangerous item in
the sweep, and it is invisible in any test whose estate has no depletables.

Reproduced rather than reasoned — a `res_type: D` resource of 10, one job
taking `(FUEL, QUANTITY=3)`, acquired and then released on **SUCCESS**:

```
demand vector: [('r:FUEL', 3, 'acquire', 'never')]
after acquire: used={'r:FUEL': 3} held={'j1': [('r:FUEL', 3, 'never')]}
after release: used={'r:FUEL': 3} held={}          <- spent, and no row says so
```

A seal recomputing from holders sees `held={}` and writes `used={}`.

**G2 — `_held` cannot be recomputed across a catalog change.** For an
unchanged catalog, `demand_vector(job_ir)` reproduces what a RUNNING job
acquired. Across a re-baseline it does not: it reads the job's *current* IR, so
a job whose `resources:` or `job_load` changed would release a different vector
than it acquired, leaking or double-freeing units. `_held` must be carried for
every in-flight job rather than recomputed. Invisible in stage 1 by
construction; it appears the moment stage 2 does.

**G3 — the waiter order, confirmed and made precise.** §1.2 named it; the code
says exactly what must be carried. `sorted_waiters` keys on
`(priority_value, _waiter_seq[j], j)` — the first term comes from the catalog
and the third from the name, so **only `_waiter_seq` and `_enqueue_counter`
need carrying**, and they must be carried as the sequence numbers rather than
as an already-sorted list, because the new period may re-sort them against a
new catalog's priorities.

**G4 — a removed waiter crashes the new period.** `sorted_waiters` does
`self.catalog.jobs[j]` with no guard, so a job that is QUE_WAIT at seal time
and absent from the new catalog raises `KeyError` on the first admission the
new period attempts (reproduced: `sorted_waiters()` over a pool holding one
name the catalog does not have raises immediately). Unreachable today — a
waiter is always a catalog job,
because job verbs are catalog-only — and reachable the day the carry meets a
catalog that dropped it. It is stage 2's, and the §2.1 classifier should
R-classify a removed job that is currently QUE_WAIT rather than leave the
lookup to fail.

**G5 — the clock position.** `Oracle._now` and `Frontiers.at` are the same
fact: feed times must be non-decreasing. A new period must open at or after
it, and the seal must carry the **clock domain** beside it, for the reason the
resume gate already refuses a domain mismatch — a real-clock estate reopened
under a virtual one would accept an earlier time and break monotonicity.

**G6 — carried usage against a re-baselined capacity.** `_bucket_cap` is
derived from the catalog, and `_bucket_used` is carried (G1). A re-baseline
that *lowers* a machine's `max_load` or a resource's amount can therefore open
a period with `used > cap`. That is not corruption — admission simply refuses
until releases catch up, which is arguably the correct reading of "you shrank
the pool while it was full" — but it is a state no genesis can produce, so it
must be an explicit A-row in the §2.1 classification rather than something an
operator meets by surprise.

**G7 — the scheduler cutoff cannot be re-derived.** §8b.1 first called
`Scheduler._next` "derived from catalog + clock". It is not, at a boundary.
`runner_startup.py` re-anchors **inclusive** of `last_at` and dedups against
`replayed_ticks` — "the ticks the journal actually holds" — and its own comment
says an exclusive re-anchor would lose a sibling silently, with no drop record.
A seal cuts exactly that evidence away, so the clock cannot say which
same-instant ticks were already consumed. Reset exclusive of T and an
unconsumed tick vanishes; reset inclusive and a consumed one fires twice. The
answer is §8a.2's barrier and its `scheduler_admitted_through: T` watermark.

**G8 — `last_contact` must not cross the seal.** §8b.1 first said host rows
"serialize as themselves". That inverts DL-95: `last_contact` is deliberately
outside the semantic projection, and a replay re-seeds it at resume so a new
leader **over-waits** rather than evicting early. Carry a stale one and the new
period can conclude a quarantined host's deadman has already expired — which
reaches the one state that permits a double run. Carry the durable routing
fields; let takeover reconciliation re-stamp the liveness evidence.

**G9 — the outbox is not an `in_flight` summary.** An earlier draft replaced the
outbox with a RUNNING-oriented list. That drops the case
`concurrency-model.md` §5 exists to explain: KILLJOB does not advance
`run_number`, so a job can be TERMINATED at run 7 with a pending KILL for run 7
still undelivered. A RUNNING-oriented summary omits it, the new period's
reconciliation skips terminal rows, and the process survives forever. Stages 1–3
dodge this by requiring an empty pending outbox; stage 4 must carry the outbox
whole — kind, index, `at`, outcomes and admission order.

**G10 — the trace is transient for the oracle and load-bearing for a shipped
consumer.** DL-113's run history folds each run root independently and builds
leaf rows only from `dispatch` records, and box rows only from a `STARTING`
trace transition. A run dispatched before a seal and completed after it
therefore leaves a permanently RUNNING row in the old root and **no row at all**
in the new one; a carried RUNNING box is worse, because its later terminal
transition closes a run the new root never saw open. Quiescence makes this
rare — a quiesced seal has no open runs — but not impossible, since a box's own
run can be open across a boundary at which no member is running. The opening
seal has to become an input to the history fold. This is shipped code, so it is
a stage-1 obligation rather than a future concern.

**G11 — the supervisor leaks completed runs, and rolling roots were hiding it.**
`Supervisor` keeps every completed `_Run` in memory and returns all of them from
`LIST`; there is no eviction. Under a rolling root the supervisor dies with the
root, so the leak is bounded by one night and nobody notices. Stop rolling and it
is unbounded for the life of the estate, and `LIST` — which the takeover barrier
reconciles against — grows without limit. This is shipped code and a real bug
today; it is only *latent* because of the very constraint §8a.0 removes.

### 8b.3 What the sweep changes

The general rule §1.2 stated holds, and the sweep shows its reach. Every gap
here is an invariant whose proof is *"replay reconstructs it"*, and every one
of them was sound while replay started at genesis. Two of them (G1, G2) are
not the ordering nuisance the waiter queue is — they are unit accounting, and
getting them wrong spends or invents resource capacity silently.

So the pool is the piece of this system least ready for a seal, and it is the
one that looked most settled. `capacity.py`'s own docstring says the pool
*"carries tested invariants instead"* of living under the state owner. Those
invariants are true and they are not sufficient for a boundary that does not
replay: they establish that no pool change happens without a row change, which
is what optimistic locking needs, and say nothing about whether the pool is
**reconstructible** from those rows, which is what a seal needs. Two different
properties, and the first does not imply the second.

### 8b.4 The cheap fix, and the right one

**Cheap:** carry the pool — `_bucket_used`, `_held`, `_waiter_seq`,
`_enqueue_counter` — as four maps in the seal record. It removes G1, G2 and G3
today. Three reasons not to.

It **freezes private internals into the one schema that must survive a version
bump** (§3). `_held`'s entries are tuples of `(bucket_key, units, policy)`
chosen for a mutator's convenience, never for persistence. Put them in the seal
and the pool can never be refactored without a migration.

It **keeps two owners, so the next field repeats G1 silently.** G1 was found by
a human reading `release()`. Nothing would find the next one: `arch_check.py`'s
DL-83 gate derives its watched set from the state models' AST, but it watches
*write escape* — assignments to `JobRuntime`/`HostRuntime` fields outside the
owner — not *carry completeness*. `CapacityPool` is outside both its model list
and its map list, so the gate has nothing to say about it. That is precisely
why G1 could exist.

And it **leaves the modelling error in place.** Look at what G1 actually is:
`_bucket_used` sums two facts with different lifetimes and different truth
conditions — units **held by live runs** (transient, and a function of which
jobs are running) and units **permanently spent** (authoritative, irreversible,
SEM-16). Recomputation is not wrong; the *sum is not decomposable*. Any fix
that keeps them added together is carrying a number nobody can explain.

**The right fix: move the pool's state onto the entities it describes, and the
pool becomes derived.**

| today | belongs |
| --- | --- |
| `_held[job]` — the vector this run acquired | `JobRuntime` — it is a per-run fact with exactly `run_number`'s lifetime |
| `_waiter_seq[job]` — this job's queue rank | `JobRuntime` — non-null iff QUE_WAIT |
| the spent half of `_bucket_used` | `RuntimeState`, as an authoritative `consumed` map |
| the held half of `_bucket_used` | nowhere: `sum(row.held for running rows)` |
| `_bucket_cap` | nowhere: already derived from the catalog |

Then `CapacityPool` is a pure function of (catalog, rows, consumed) and holds no
mutable state at all. G4 becomes a catalog lookup with a documented default,
because a removed job's rank rides on its own row. G6 is untouched, being a
genuine semantic question rather than a modelling error.

*(Two overclaims withdrawn after peer review.)* G1–G3 do not "stop existing" —
their facts become **explicit and reconstructible**, which is the real and
smaller claim. And the architecture gate cannot "close the class": it can
enforce where state *lives*, and it cannot prove a decomposition is
**semantic**. `_bucket_used` could be moved unchanged under `RuntimeState` and
pass a single-owner gate while still summing two facts. The gate is worth
adding; it is a guard against escape, not against bad modelling, and only
review catches the second.

**The settled shape.** Placement follows ownership and mutation boundaries
rather than the schema argument, which does not discriminate — a tuple frozen
into the seal is frozen there wherever it lives. The answer to that is a public,
typed, canonically-serialized model; the answer to *where* is the row:

```python
class CapacityReservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    bucket: str
    units: int
    release_policy: Literal["completion", "success", "never"]
```

with `JobRuntime` carrying `reservations: tuple[CapacityReservation, ...] = ()`
and `waiter_seq: int | None = None`. A reservation belongs to exactly one
`(job, run_number)`; acquisition coincides with that row's start transition and
release with its terminal one; it must participate in that row's optimistic-lock
projection; the model already forbids overlapping runs of one job, so no
separate table is implied; and a future non-quiesced re-baseline needs the old
run's acquired vector beside the old run number, independent of C2's definition.

`RuntimeState` enforces what a row cannot see on its own: `reservations` is
non-empty only while STARTING or RUNNING; `waiter_seq` is non-null iff QUE_WAIT;
a terminal transition clears reservations and **atomically** moves the
non-released units into `consumed`; a start may not overwrite non-empty
reservations; and the acquired vector is frozen at acquisition, never recomputed
from the current catalog. `consumed` stays a top-level `RuntimeState` map,
because its lifetime belongs to the bucket rather than to any completed job.

`_enqueue_counter` is **carried**, under `RuntimeState`, non-negative and at
least every row's non-null `waiter_seq`. The considered alternative — redefine
the rank as `1 + max(active waiter_seq)` and reset when the queue empties — is
semantically sufficient, and it buys one integer of storage in exchange for a
normalization step whose equivalence to genesis replay must itself be proven.
That is a strictly larger correctness obligation for a rounding error.

Two fields enter the semantic projection by doing this, and that is correct
rather than a cost: `_PROJECTED_JOB_FIELDS` is derived as "everything except
`state_rev`", so both are projected by default, and both change at exactly the
moments `status` already does — acquire at a start, release at a terminal. No
new revision churn.

`consumed` deliberately does **not** become a fourth `expect` namespace. It is
authoritative state under the owner, like the timer heap, and no operator holds
a revision on it today. It gains a namespace on the day SEM-16 replenishment
(`update_resource`, out of scope per `runner-design.md` §12) needs one, which is
also the day an operator has a reason to address it.

**This is DL-86's own move, finished.** That entry moved `_box_ran` onto
`JobRuntime.ran_members` — *"projected with the entity it describes"* — deleted
`_run_started_at`, and kept `_CapacityPool` because it could prove the
**revision** invariant: no pool change is constructible without a row change.
That invariant is true. It is not the property a seal needs, which is
**reconstructibility from those rows**, and DL-86 never had to answer that
question because replay always started at genesis. Doing this closes
`concurrency-model.md` §3's state inventory, which has been down to one open
item since.

**Why before stage 1, not after.** The seal schema is the version boundary (§3).
Shipping the cheap fix and then this one means a schema migration bought
nothing. This is the irreversible-commit case where the carpenter's rule
applies.

**And it lets the gate close the class.** Once the pool holds no state, the
DL-83 gate can assert the stronger property it cannot assert today: that
`RuntimeState` is the *only* holder of authoritative oracle state, by naming the
classes permitted instance attributes at all. Then the next G1 fails CI instead
of waiting for someone to read `release()` again.

**What would change this answer.** If the pool were about to be replaced — the
cross-node resource coordination DL-49 defers — freezing its internals briefly
would be defensible. It is not: that track builds on this model rather than
replacing it, and it would inherit the same undecomposable sum.

## 9. Obligations

*Superseded.* The CM-24…CM-38 rows drafted here became `period-model.md` §13's
**PR-** series (109 obligations, namespace `PR-\d{2}[a-z]?`), rewritten
against "what would a plausible-but-wrong implementation still pass?". The
run-history pair landed without a namespace under DL-113. Nothing here is
cited by code.

## 10. Decision-log entries this implies

*Superseded by `period-model.md` §15 and §16 for the mechanism.* What landed:
**DL-113** (run history, committed). Still proposed, in order, numbers
provisional: the ha-deployment set (topology, leadership, RPO=0, affinity,
uncertainty, no-auto-reroute); then the period-model set — the period and the
seal; the lineage fence and the optional run root; `baseline_id` per period;
the atomic `decision` record and control-protocol v3; the capacity
decomposition; the classification tiers; `catalog_hash` v2; the retry horizon
as a profile field; armed latches crossing a release; the SPAWN idempotency
protocol; the FW spool. Adoption was on that list and is retired unbuilt
(DL-138). Two from this document survive on their own:
**run-root exclusion and estate leadership are two mutexes** (§4a) and **the
local multi-executor rig is a third proving tier** (§4a.5).

## 11. Open questions

Continuing the runner E-series (`runner-design.md` §15, extended by
`ha-deployment.md` §11 to E15).

- **E16** — seal cadence, and it is a **store-era** question only (§8a.5): on the
  file substrate a period ends when a run root does, so the cadence is the
  re-baseline cadence and nothing is left to choose. Once periods live in one
  ledger, daily at the cutoff is the proposal — but is the boundary per estate,
  per booking center, or per regulatory period? The answer sets the replay
  bound, so it also sets the takeover time. Client question.
- **E17** — what `standby check` must assert before BCP governance will accept a
  green. Client question, and the answer probably has a form to fill.
- **E18** — does an A-classified boundary truth flip need an operator
  acknowledgement before the period opens, or is reporting it enough?
  *Narrowed by period-model §3.1: the classification and every A assumption
  are carried in the seal, so the record exists whatever the answer.*
- ~~**E19**~~ — *closed by period-model §10.3: a member changed while its box
  is executing is **R**, even when the member is INACTIVE; a box run never
  observes two versions of anything in its closure (PR-42).*
- **E20** — retention of the seal chain vs. the inputs. *Carried as period-model
  PR-Q3; it gates pruning and the meaning of "verified".*
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

*Superseded by `period-model.md` §15, which is the authoritative list.* Two rows
from the earlier table survive as this document's own: `ha-deployment.md` §9's
amendment table omits the `citation-index` row for `E\d{1,2}`, whose prose
still reads "E1–E11" while §11 there opens E12–E15; and `runner_hosts.py`'s
*"One host, for now"* docstring scopes to "until there is a relay", while the
local two-row rig (§4a.5) arrives before the relay does.
