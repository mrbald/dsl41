# Concurrency model — the frozen contract

Status: frozen (2026-08-14, DL-84). Normative for every input that reaches
the oracle and every effect that leaves the engine, in the same way
`docs/control-protocol.md` is normative for the control plane and
`docs/supervisor-protocol.md` for the lifecycle tier. Each change to a
frozen item requires a decision-log entry.

This document is stage S0 of the programme in §10. It exists because §4
and §5 cannot be built against an open storage or identity question
without being rebuilt when it closes.

Proving ground: `examples/nightbank`. Its own test file records the gap
this programme closes — *"the live-engine path is exercised manually via
the RUNBOOK; these tests pin what CI can pin."* Every property here is a
property of that estate under injected faults, not an assertion in prose.

Already landed: DL-82 (state ownership), DL-83 (the spawn-window signal
fix and the derived-field gate).

## 0. The invariant

> No externally requested **direct** mutation of published oracle state is
> applied without checking the version the caller read.

Mandatory. There is no opt-out and no `"any"` escape: a caller that does
not name a version is refused, not admitted unchecked. Cascades and time
observations are ordered *consequences* of applying an input, not
independent writes, and are outside the rule by design — an operator
cannot hold a revision on a state that only the semantics may change.

**The safety property, stated once:**

> No `(job, run_number)` ever executes twice, under any interleaving of
> leader failover, host reroute, message loss and duplication.

§5 exists to make that true; §9 exists to make it testable. Everything
else is scaffolding around those two sentences.

## 1. Storage — frozen

**Postgres-class, single transactional store, with the outbox _in_ the
ledger.** This is neither an implementation detail nor deferrable: §4
requires two atomic multi-record writes, so the log, the decision index
and the outbox must be able to commit **in one transaction**. "Outbox
beside the ledger" is valid only under that condition, so it lives in it.

The required contract, whatever provides it:

| capability | why |
| --- | --- |
| epoch-conditional append | the leader fence |
| monotone epoch allocation | election |
| decision lookup by `request_id` | idempotency |
| atomic multi-record commit | admission and publication batches |
| linearizable read of the leader record | takeover proof (§7) |

Kafka alone does not satisfy this. It supplies the ordered log and leaves
election and the outbox transaction beside it — two consistency stories
where the model needs one.

## 2. Identity

| domain | unit | identity |
| --- | --- | --- |
| the log | one run root | `baseline_id`, `epoch`, `committed_index`, `applied_index` |
| oracle state | one job / one global | `state_rev` |
| effects | one effect | `effect_id` **bound to** `executor_id` |
| execution host | one relay | `host_id`, `generation` (§8) |

`run_id` keeps its existing meaning: the per-spawn uuid4 in the wrapper
spec.

## 3. State ownership and `state_rev`

**Owner.** `RuntimeState`: frozen `JobRuntime` / `GlobalRuntime` rows,
private maps, typed operations (`transition`, `start_run`, `set_flags`,
`set_armed`, `set_global`, `enqueue_timer`). No mutable map escapes and no
generic `setattr` reaches a row. `StatusStore` (DL-82) is the intermediate
form and is replaced here — this evolves it, it does not undo it. Note
that `model_copy(update=)` does **not** validate, so the owner needs a
validating construction path.

*(Amended by DL-86, at build.* This paragraph listed a `cancel_timer`
alongside `enqueue_timer`. There is no caller and cannot be one yet: the
oracle discards a superseded timer at FIRE time, and a fire advances the
clock and runs the lazy checks on its way past, so dropping the entry
early is not behaviour-preserving. It arrives if S2's decision index needs
it, with a trace test for the clock difference — not as dead code now.*)*

**Cardinality.** One increment per entity per committed input, and only
when that entity's semantic projection differs from its pre-input value.
Implemented as an input transaction: snapshot the touched entities,
compare at the boundary, replace each changed entity exactly once.
Over-approximating the touched set is safe; under-approximating is not.

**The semantic projection.**

Include: `JobRuntime`'s semantic fields, and the timer **ordering token**.

Exclude: `state_rev` itself (else it justifies itself), `watching`
(effect state that moves with adapter activity and no committed input),
log locations, catalog metadata, and `spec_drift` (disk state).

The timer heap is ordered globally by `(due, insertion seq)`. A per-job
set digest does **not** capture the relative order of equal-time timers
*across* jobs, and that order decides resource release, box cascades and
which job starts. The projection therefore carries the ordering token,
not a set digest.

**The state inventory.** Four pieces of authoritative state sit outside
`JobRuntime` and must each either move under `RuntimeState` or carry a
frozen and **tested** invariant that every change to them necessarily
accompanies a projected entity change: `_CapacityPool`, its waiter order,
`_box_ran`, and `_run_started_at`. An untested invariant here is the
thing this document exists to remove.

*(Settled by DL-86, at build.* `_box_ran` MOVED — it is
`JobRuntime.ran_members` on the box row, projected with the entity it
describes. `_run_started_at` was write-only: assigned on every start and
read nowhere, so it was never state and is deleted. `_CapacityPool` and
its waiter order STAYED, under two tested invariants — the waiter set is
exactly the QUE_WAIT jobs, and only a starting or running job holds units
— so no pool change is constructible without a row change to carry it.
The waiter ORDER needs no token of its own, unlike the timer heap: a
waiter's rank is fixed at its QUE_WAIT transition, which is itself a
projected change, so replaying the transitions replays the order. A timer
can be armed by an input that changes no row at all — a second schedule
tick arms a second `must_start` deadline and finds the job already armed —
which is exactly why the heap carries a token and the queue does not.*)*

**Completeness is structural, not statistical.** One feed mutates several
fields, so a missed site hides behind a sibling's write — `_run` sets
`armed`, `run_number` and `started_by` and then sets status twice, so one
touch inside `_set_status` masks every missed site in that feed and the
property test still passes. The guard is therefore the owner plus the
blocking gate in `scripts/arch_check.py` (landed; DL-83 derives its
watched set from the model's AST so it cannot silently narrow when
`state_rev` is added). The property test stays as corroboration, carrying
the safety direction and a cardinality assertion, with its generator
widened past its current STATUS / SET_GLOBAL focus.

## 4. Admission and application

**One admission rule for every input.** Scheduler ticks, adapter
completions, reconciliation injections and standalone time observations
feed the same state machine as operator commands and take the same path.
For every input, in log order:

1. Validate framing, protocol version, `baseline_id`.
2. **Deduplicate**: look up `request_id` and compare fingerprints. Return
   the prior decision, or attach to an in-flight attempt. *Only now*
   reject an unseen stale epoch — so an exact old-epoch retry recovers
   its original result, while an unseen old-epoch request is refused.
3. Assign the leader timestamp, monotone across inputs already admitted
   but not yet applied.
4. Atomically append one ordered batch: `TimeAdvanced(at)` +
   `InputAttempt`. Emit `command_committed` — the envelope is durable,
   which is not the same as a decision.
5. Apply: earlier committed inputs, then this batch's time observation,
   firing timers through it. Timers fire **before** the precondition
   gate, because `feed()` fires timers due at or before the event's
   timestamp before applying it, and a `term_run_time` firing between
   gate and apply would defeat the precondition it just passed.
6. Evaluate `expect`; feed, or record the rejection.
7. Atomically record `ApplyResult`, revisions, outbox entries and the new
   `applied_index`. Emit `oracle_applied`.

Steps 5–7 must not yield to another state-changing input.

**Replay is two-pass.** `ApplyResult` is appended *after* `InputAttempt`,
so replay cannot meet an attempt and skip it by kind: it builds the
decision index first, then applies. An attempt durably admitted with **no
result** — a crash between steps 4 and 7 — is replayed as an application,
because admission is the commit point.

## 5. Effects

**At-most-once is global, not host-local.** An effect that is merely
"at-most-once per durable `effect_id`" is unbound to any host: after an
uncertain SPAWN, takeover can route the same `effect_id` to a different
relay, each host's dedup store correctly reports "first application", and
two processes run. That is the failure this whole model exists to
prevent.

- Every SPAWN is atomically bound in the outbox to
  `{effect_id, run_id, job, run_number, executor_id}`.
- **Retries always go to that executor.** Rerouting requires a *new run
  and a new effect*, plus proof (§8) that the old executor cannot still
  apply the old one.
- `executor_id` and the complete effect payload participate in the
  fingerprint.
- Machine and pool names resolve to a relay through a frozen routing
  table; `insert_machine` resolution (DL-49/52) is the input to that
  resolution and remote routing is its output.

**Effect states are three, not two.** "Deduplicate and replay the
original result" is unimplementable as stated: persist tombstone →
`kill()` → crash before persisting the result, and nothing can know
whether the signal landed.

`pending` → `applied(result)` | `indeterminate`. An exact retry returns
the original result only when that result is *known*; otherwise it
reconciles, or answers `outcome_unavailable`. Tombstones carry
fingerprints and reject collisions. `effect_id` is the dedup identity;
`(incarnation, token)` is a **fencing precondition**, not part of the
dedup key.

**Supersession is by exact desired state.** The obvious guard — compare
run generations — does not fire for the scenario that motivates it,
because **KILLJOB does not advance `run_number`**: a delayed SPAWN for
run N is still "current" after run N has been TERMINATED.

- SPAWN applies only if exactly `(job, run_number, run_id)` is still
  desired running and not terminal.
- TERM and KILL apply only against exact run identity and the expected
  kill stage; they are **distinct effect stages with distinct ids**.
- SHUTDOWN binds to the intended supervisor incarnation and scheduler
  epoch.

Per-run effect ordering is mandatory. The SPAWN→SIGNAL race this depends
on is already closed (DL-83): a live wrapper with no spawn record answers
`not_ready`, so a kill can no longer be persisted as an applied no-op.

## 6. The envelope and reads

```json
{"v": 2, "baseline_id": "…", "epoch": 7, "request_id": "…",
 "verb": "CHANGE_STATUS", "payload": {…}, "expect": {"job:nightly": 12},
 "claimed_actor": "alice@host"}
```

No client-supplied `at`: a future stamp is a timer fast-forward and a
backdated one breaks monotonicity. `epoch` ships in v2 though it is inert
single-host, because adding it after the CLI and TUI migrate is a second
wire break. `expect` names only the addressed entity, with keys
namespaced `job:` / `global:`. `claimed_actor` is a client hint — **the
leader stamps the authenticated principal** into the admitted record.

Fingerprint = the complete semantic envelope including `baseline_id` and
`epoch`, excluding transport framing.

Reads publish `baseline_id`, `epoch`, `applied_index` and `state_rev`.
`global {name}` / `globals {names: […]}` answer
`{present, value, state_rev}` for a *named* entity and insert nothing — a
map of existing globals cannot express the absence that a conditional
create must condition on. Revision-bearing reads are leader-only in v2.

## 7. Leadership, relay, takeover

**Proof is positive and linearizable.** A quiet partition produces no
failed append, so "cannot confirm leadership" means *lack of fresh
positive proof*, never "no evidence against". A successful ACQUIRE is
proof only if the leader record is read linearizably and **every append
and every relay dispatch re-checks the resulting epoch**. Losing proof
stops dispatch, not merely renewal.

**The takeover barrier.**

```
ACQUIRE → reconcile every execution host → retire superseded, re-drive pending → dispatch
```

- The set to reconcile is **every host in the routing table with a
  non-empty outbox**, not merely the reachable ones.
- An unreachable host is **quarantined** (§8): no effect may be routed to
  it, and every job bound to it is held rather than rerouted. Rerouting
  without proof that the old executor is dead is exactly the double-run.
- Dispatch to *other* hosts proceeds. One unreachable host must not block
  the estate.
- A newly elected leader invalidates relay-cached proof by epoch: a relay
  rejects any dispatch carrying an epoch below the highest it has seen.

**Relay, frozen.** A host-local relay terminates remote identity; the
supervisor stays AF_UNIX + same-uid and never learns what a certificate
is. Principals are mutually authenticated; the relay owns TTL behaviour
and durable effect-id calls. This *protects* DL-42's extraction fence
rather than eroding it. Remote orphaned leases are TTL-gated —
"connection closed means the controller died" is a local-AF_UNIX
inference, and with the relay in place that is the only context in which
it is still used (supervisor-protocol §5's own constraint on any
non-local transport).

**Timing is a bound, not a proof.** Safety is the successful ACQUIRE.
`T_barrier = T_acquire + T_list + T_reconcile + T_redrive` is an
operational SLO, and it is **not** a mandatory test until workload bounds
(spool size, backlog, batching) are specified.

**Ledger header** pins the state-machine version and `catalog_hash`;
leader eligibility requires an exact match on both. Mixed builds derive
different revisions from identical inputs, and the supervisor holds no
job definitions — a SPAWN spec is a resolved literal command string — so
nothing downstream can detect that two leaders disagree about the estate.

## 8. Host lifecycle: active, passive, evicted

Quarantine (§7) is safe and it is not sufficient on its own: one dead
host would hold its jobs forever. The operator therefore owns an explicit
routing state per host, durable in the ledger so that a failover does not
undo a drain.

| state | new effects routed | running work | set by |
| --- | --- | --- | --- |
| **active** | yes | continues | operator, or registration |
| **passive** | no | continues to completion | operator (drain) |
| **quarantined** | no | held, not rerouted | the leader, automatically, on unreachability |
| **evicted** | no | rerouted as **new runs** | operator, under §8's precondition |

`passive` is a drain: reversible, asserts nothing, and is the correct
tool for planned maintenance. `evicted` is the only state that permits
another host to run work that was bound to this one, so it is the only
one that can cause a double run, and it is gated accordingly.

**The deadman makes eviction provable.** A host that wants to be
reroutable runs its supervisor with a deadman interval `T_deadman`: a
supervisor with **no live leaseholder** for `T_deadman` exits, which
kills every wrapper it owns by lifeline EOF — the mechanism
supervisor-protocol §5 already relies on ("supervisor death kills all
wrappers by lifeline"), not a new kill path. The deadman is one number
and one exit; it adds no policy to the tier and so does not breach
DL-42's counter-fence.

It is **opt-in per run root**, because it costs something real: today a
supervisor tolerates an absent controller indefinitely, which is what
lets an engine crash and resume with its runs intact (DL-79). A run root
without a deadman is never reroutable except by force.

**Eviction preconditions.** `evict` is refused unless all hold:

1. the host is unreachable from the leader;
2. the host runs a deadman;
3. `now - last_contact > T_deadman + T_kill + T_skew`.

`T_skew` covers monotonic-clock **drift** between hosts (parts per
million over the interval), not clock synchronization — the argument is
the standard lease argument and depends only on bounded drift. A refusal
reports the remaining wait, so the operator waits rather than guesses.

**Force is attributed, not forbidden.** `evict --force` skips the
precondition, is recorded with the authenticated principal, and is the
one path in this document that can produce a double run. It exists
because an operator with out-of-band knowledge — the machine is
physically powered off, the disk is out — is sometimes right, and waiting
out a deadman is then pure loss. It is loud, durable and attributable;
that is the whole of its safety story.

**Eviction is fenced on return.** Eviction bumps the host's `generation`.
A returning relay presenting a stale generation is refused registration
and must self-fence — kill surviving wrappers — before it may re-register
as active. For a correct eviction this is a formality. For a mistaken
`--force` it is the detector: it cannot un-run the duplicate, but it
stops it continuing and it turns a silent divergence into a recorded
incident.

Re-driving an evicted host's held jobs issues **new runs with new effect
ids**, never retries of the old ones (§5).

## 9. The proving ground

A **deterministic model harness** comes before the code it validates: N
engines, the ledger, relays and execution hosts over the existing virtual
clock, with injected partitions, pauses, message loss and duplication,
and every interleaving reproducible from a seed. The §0 safety property
is checked by **counting spawns per `(job, run_number)`** across the
whole interleaving — that is what makes the double-run failure testable
rather than argued. Real-process chaos is a separate tier, in which
nightbank's manual RUNBOOK path becomes automated.

Obligations. Tests are named `test_cmNN_*`, on the house convention of
`test_semXX_*`.

| # | obligation | cost |
| --- | --- | --- |
| CM-01 | structural owner gate | landed (DL-82/83) |
| CM-02 | cardinality: one increment per entity per committed input | landed (DL-87) |
| CM-03 | corroborating property, generator widened | landed (DL-87) |
| CM-04 | timers fire before the gate (`term_run_time` fixture) | landed (DL-89) |
| CM-05 | dedup precedes admission: a retry advances no logical time | landed (DL-89) |
| CM-06 | retry / fingerprint / eviction, incl. `outcome_unavailable` | cheap–medium |
| CM-07 | two-pass replay, incl. admitted-without-result | landed (DL-89) |
| CM-08 | bisimulation unchanged | cheap |
| CM-09 | at-least-once delivery **and** at-most-once application; superseded effects retired; quarantine holds | expensive |
| CM-10 | the deadman fires: an unleased supervisor exits and its wrappers die | cheap (virtual clock) |
| CM-11 | `evict` refused before the bound, permitted after; `--force` recorded with its principal | cheap |
| CM-12 | a returning evicted host is refused and self-fences | medium |
| CM-13 | drain: `passive` routes nothing new and finishes what is running | cheap |
| CM-14 | no `(job, run_number)` runs twice, over seeded interleavings | expensive — **the point** |

Pause, drift and thundering-herd tests are **not mandatory** until their
clock model, client count, attempt limits and pass criteria are
specified. An unspecified chaos test is a flake generator.

## 10. Stage order

```
S0  this document: storage, auth, envelope + ApplyResult types, effect
    contract, relay + barrier, admission order, projection + inventory
H   the model harness — before the code it validates
S1b RuntimeState: frozen rows, private maps, timers WITH ordering, inventory
S1c state_rev + input transaction + read verbs
S2  typed frontiers, atomic admission, decision index, two-pass replay
S3  mandatory preconditions + protocol v2
S4  CLI / TUI          ∥   S5  relay + host identity, effects, barrier,
                              deadman, host states + evict
S6  ledger + election
S7  failover / partition / double-run matrix over nightbank
```

Two dependencies were inverted in earlier drafts and are pinned here: S2
persists `InputAttempt`, so the envelope and `ApplyResult` types must be
frozen first (S0); and S2's admission and S5's effects both depend on
storage, election and relay contracts, which is why those are S0 text and
not S6 discoveries.

Single-owner rules: `oracle.py` through S1b→S1c, `runner.py`'s loop
through S2→S3. **S4 ∥ S5 is the only file-disjoint pair.** The `host`
verb ships in S5, not S4, because it is meaningless before the routing
table exists. Everything else is sequential.

## 11. Deliberately not versioned

The supervisor tier (fencing plus effect idempotency is enough; it holds
no semantics to version); the catalog, immutable per run root by three
guards — one read hashed and parsed from the same bytes, no reload path,
and a used run root refusing re-baselining; and time observations, which
are consequences rather than commands.
