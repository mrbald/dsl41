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

*(Amended by DL-90, at build.* The consequence sentence above has a
sharper edge than it reads. A timer firing inside an input's **own**
batch — a `term_run_time` deadline due at exactly that input's timestamp
— does not invalidate that input's precondition: §3 gives one input one
increment, applied at commit, so everything an input causes shares its
revision. That is the rule working, not a hole in it. A revision that
moved because of the caller's own input is not one they could have read
beforehand, and requiring them to name it would make every precondition
unsatisfiable. The same deadline due strictly earlier fires as its own
input, bumps the revision, and does invalidate them — which is the case
an operator meets. What stands between an operator and a job that ended a
moment ago is the semantics, not concurrency control. Both halves are
tested.*)*

**Enforcement point.** The mandate is one function
(`runner_admission.parse_envelope`), not a rule each transport
re-implements — the §7 relay must reach the same verdict as the §10
socket. In-process callers holding the `Engine` object are not external
and do not pass through it; they are the trust domain the scheduler and
the adapters inject from.

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

*(Amended by DL-100, at build — stage S6a.* What provides the contract
today, and where that stops.

**Two of these five landed with S2 without being named.** Decision lookup
by `request_id` IS the `DecisionIndex`; atomic multi-record commit IS the
one-line attempt record, a batch no crash can tear in half because it is a
single append. S6a is the other three, on the substrate that exists: an
`flock` on the run root held for the process lifetime, plus the fsync the
WAL already pays. Monotone allocation and the linearizable read are the
same act — the epoch is read from the log and written back to it under the
lock, so the allocation and the log's account of it cannot disagree.

**The leader record lives in the ledger,** which is this section's own
argument about the outbox applied one level up. The lock file is the mutex
and nothing else; the holder note it carries is what a refusal prints, and
is never read as the fence, because a note can be stale and a held lock
cannot.

**Not a lease.** It has no expiry to renew: the kernel releases it when the
holder dies, `kill -9` included. So nothing has to decide whether the
previous holder is alive — which the control socket's 0.2-second connect
probe did decide, and got wrong in both directions (an engine wedged past
the timeout lost its socket; a socket left by a crash was unlinked on a
guess). That probe stays, demoted to what it always was: cleanup of a stale
socket file, not an election.

**Where it stops is one host.** "Whatever provides it" is satisfied for one
host and for nothing else: a lock on a local filesystem is not a
linearizable leader record for a second machine, and on NFS it is not one
at all. The relay DL-97 deferred still waits on the shared store this
section describes. What it gets from S6a is a fencing token that moves.*)*

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

*(Amended by DL-96, at build — stage S5c.* Four deviations, each bounded and
each because the thing it defers against does not exist yet.

**`run_id` is not bound before the attempt.** This section binds it
atomically for a reason a RELAY has and a local engine does not: a relay
sees only ids. Locally `(job, run_number)` IS the identity —
`runs/<job>.<run_number>` is created with `mkdir()` and no `exist_ok`, so a
second spawn of one run fails loudly rather than doubling — so the outbox
records the process identity the spool reports, when it reports it. Binding
it earlier arrives with the relay that needs it.

**TERM and KILL are one effect, not two staged ids.** The split exists so a
relay can tell a retried TERM from a retried KILL. The adapter's ladder
never yields to the engine between its stages, so there is no
engine-visible state between them for a second id to name; a re-driven kill
re-runs the whole ladder, and TERM to a dead group is a no-op.

**SHUTDOWN is not an effect yet.** It binds to a supervisor incarnation and
a scheduler epoch, and neither is allocated until S6.

**A pending SPAWN is not re-driven at resume.** `docs/runner-design.md` §7
fails a start with no spool trace rather than re-running it, which DL-41a
decided deliberately. The outbox makes re-driving expressible; whether §7's
takeover barrier should re-drive rather than fail is that barrier's
question, and it belongs where leader election gives it a context. A
pending SPAWN that DOES have a spool trace is reconciled as applied — the
engine died in the window between launching and recording, and the spool is
the record.

A recorded KILL, by contrast, IS re-driven at resume, and that is not a new
licence: §7 of runner-design already permits exactly one side effect there,
and names it "recorded kills".*)*

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

*(Amended by DL-97, at build — stage S5d.* The **relay is not built**, and
the reason is worth recording rather than leaving as an omission.

Every remaining part of this section rests on one of two things that do not
exist yet. The barrier begins at ACQUIRE, and there is no election until S6.
The relay is a network transport with mutually authenticated principals, and
this section does not say — because it could not usefully — how those
principals are named, issued or rotated; that design wants one real
deployment to answer it, and freezing it now would freeze the least
informed version, which is DL-42's argument against premature extraction
applied to the same seam from the other side. There is also no second
machine to test one against, and a loopback relay proves the handshake, not
the thing the relay exists for.

**Trigger**: build it when there is a second execution host to route to —
which in practice means alongside S6, since a leader that can be superseded
is what makes a second host's fencing meaningful.

What DID land, because it is real on one host: quarantine with a producer,
and the fence stated as a refusal. A host the leader cannot reach is
quarantined, so new work is HELD rather than failing against a supervisor
that is not there — which is worth having on a single host on its own
account, and is what makes §8's first eviction precondition reachable. The
leader's two verbs (`quarantine`, `reinstate`) take the engine's own door
with no `expect`: §0's mandate is on externally *requested* mutations, and
an observation about reachability is not one. Quarantine remembers the state
it interrupted, so clearing it restores the operator's intent rather than
overriding it. And reaching an evicted host again does **not** un-evict it:
the returning host must re-register at the new generation and self-fence
first, which is the relay's act to perform — so what stands here is the rule
and the refusal that names it, not an engine that kills someone else's
wrappers on a hunch.*)*

**Timing is a bound, not a proof.** Safety is the successful ACQUIRE.
`T_barrier = T_acquire + T_list + T_reconcile + T_redrive` is an
operational SLO, and it is **not** a mandatory test until workload bounds
(spool size, backlog, batching) are specified.

**Ledger header** pins the state-machine version and `catalog_hash`;
leader eligibility requires an exact match on both. Mixed builds derive
different revisions from identical inputs, and the supervisor holds no
job definitions — a SPAWN spec is a resolved literal command string — so
nothing downstream can detect that two leaders disagree about the estate.

*(Amended by DL-100, at build — stage S6a.* Two things this section left
implicit, settled by the code that took its first ACQUIRE.

**ACQUIRE precedes every act, not merely every append.** The barrier begins
there for a reason that was live in this build: `dsl41 run --resume`
replayed the log, reconciled the estate, re-drove recorded kills and
appended — and only then claimed the control socket that was supposed to
exclude a second engine. Two of them therefore both acted, in full, before
either was refused. A mutex taken after the first side effect is not a
mutex. The rule reaches further than the engine's own entry points: the CLI
takes leadership before it starts a supervisor and takes its lease, because
that is an act on an estate this process may turn out not to lead.

**The state-machine version is a number of its own,** not `dsl41_version`.
The package version moves for a docs typo, and refusing to resume a live
estate after a patch release would be an outage manufactured by
bookkeeping. What this section means is the version of the derivation from
inputs to state — oracle transitions, condition evaluation, timer ordering,
the §3 projection — bumped deliberately when one of those moves, and
nothing a replay cannot see. A header that pins none was written before the
gate existed and reads as version 1, on the courtesy S2 gave a journal with
no `request_id`.*)*

*(Amended by DL-101, at build — stage S6b.* Where the re-check goes, and
what it can and cannot do.

**Every append, and that is enough for every dispatch.** The re-check is
one `stat` at the top of the WAL append, before the write, so a leader that
cannot prove it leads admits nothing rather than admitting and then
discovering it had no right to. It covers dispatch for free because §5 puts
the outbox record *before* the attempt: an append this engine may not make
is an effect it never applies. There is deliberately no background prober —
an engine with nothing to append dispatches nothing, so the only proof that
goes unchecked is proof nothing was about to rely on.

**What losing proof means on this substrate.** Not a lapsed lease: the lock
file was deleted or replaced under the holder. That is not hypothetical —
delete the name and the next engine creates a fresh inode, `flock`s it
happily, and two leaders run. The re-check does not prevent that; the
second engine really does acquire. It ensures the first one stops, which is
the same honest bargain §8 strikes for its sibling fence: it cannot un-run
the duplicate, it stops it continuing and turns a silent divergence into a
recorded incident.

**Stopping is stopping, not self-fencing.** The engine raises and its
existing tethered/detached contract decides what becomes of its wrappers.
An engine that killed processes on losing proof would be reaching for the
relay's act (§7, DL-97) from a position where it cannot know whether the
new leader has already adopted them.*)*

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

*(Amended by DL-94, at build — stage S5a.* Four things this section left
implicit, settled by the code that implements it.

**Precondition 1 is the `quarantined` state, not a probe.** "Unreachable
from the leader" has exactly one durable form, and it is the row the leader
writes when a host stops answering. The gate must be a pure function of the
row: replay has no live host to ask, so a gate that asked one would decide
differently the second time and make the log a record of nothing. A
`passive` host is therefore not evictable — draining asserts nothing about
reachability, which is why the two states are separate.

**A failed precondition is a rejection, not a refusal.** Every check here
reads mutable state, so it belongs where `expect`'s does — inside the
input's own batch, in log order. It consumed an index and it is in the log;
an operator who was told to wait 40 more seconds re-reads and re-decides,
which is what `rejected` means (control-protocol §3).

**Registration never resets a routing state.** "Durable so that a failover
does not undo a drain" has to bind the registering relay too, or it gives
back with one hand what it takes with the other. Registration creates a row
`active`; re-registration refreshes identity (`deadman`, `last_contact`)
and leaves the state alone.

**This engine's own executor is seeded at genesis, not admitted.** The
row's existence is a fact about how the process was launched, identical on
every replay of the same run root; admitting it would spend a log index per
start recording what every reader of that log already knows. What §8
requires to survive a failover is the STATE, and every change to that is an
admitted input.

**Held work survives the failover the drain survives.** Making the routing
state durable is only half of "a failover does not undo a drain". The other
half is reconciliation: a start with no spool trace is normally a crash
between feed and spawn, and §7 of `docs/runner-design.md` fails it rather
than silently re-running it. On a host that routes nothing that inference
is wrong — there was no crash — so those jobs stay held. A drain whose
state survived while its work was failed would be a drain in name only.

The three operator verbs are `activate`, `drain` and `evict`.
`quarantined` is the leader's, and its producer is S5d.*)*

*(Amended by DL-95, at build — stage S5b.* Two more, from wiring the
deadman.

**`last_contact` is outside §3's semantic projection.** An engine renews its
supervisor lease every twenty seconds. Admitting that as an input would move
every host row's revision three times a minute — no operator could hold an
`expect` on one, and the WAL would become a heartbeat log. It is the class
§3 already excludes for `watching`: state that moves with relay activity and
no committed input. Excluding it is safe in the one direction that matters,
because a **fresher** contact only ever delays an eviction; and a replay
re-seeds it at resume time, which is fresher still. A new leader that cannot
reach a host therefore starts the bound from its own takeover rather than
from a value it inherited — over-waiting, which is the safe way to be wrong.

**`T_deadman` is read back from the host, never declared by the leader.** A
reattaching engine meets a supervisor it did not start, possibly one launched
with a different interval or none at all. The bound has to describe the host,
so the row records what the supervisor reports over the lease exchange. A
wrong value here is not cosmetic: it is the length of the wait standing
between an operator and a double run.*)*

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
| CM-06 | retry / fingerprint / eviction, incl. `outcome_unavailable` | retry + fingerprint landed (DL-90); `outcome_unavailable` landed (DL-96); eviction's last precondition with S5d |
| CM-07 | two-pass replay, incl. admitted-without-result | landed (DL-89) |
| CM-08 | bisimulation unchanged | cheap |
| CM-09 | at-least-once delivery **and** at-most-once application; superseded effects retired; quarantine holds | application half + supersession landed (DL-96); quarantine holds landed (DL-97); remote delivery waits on the relay |
| CM-10 | the deadman fires: an unleased supervisor exits and its wrappers die | landed (DL-95) |
| CM-11 | `evict` refused before the bound, permitted after; `--force` recorded with its principal | landed (DL-94/95/97): every precondition now produced rather than built by hand |
| CM-12 | a returning evicted host is refused and self-fences | the refusal landed (DL-97); the self-fencing is the relay's act and waits with it |
| CM-13 | drain: `passive` routes nothing new and finishes what is running | landed (DL-94) |
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
                              (S5a-c landed; the relay and the barrier moved
                               to S6 by DL-97 -- both rest on election or on
                               a second host, and neither exists before it)
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
