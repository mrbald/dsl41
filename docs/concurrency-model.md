# Concurrency model — the frozen contract

Status: frozen (2026-08-14, DL-84). Normative for every input that reaches
the oracle and every effect that leaves the engine, in the same way
`docs/control-protocol.md` is normative for the control plane and
`docs/supervisor-protocol.md` for the lifecycle tier. Each change to a
frozen item requires a decision-log entry.

This document is stage S0 of the programme in §10. It exists because §4
and §5 cannot be built against an open storage or identity question
without being rebuilt when it closes.

Proving ground: `examples/nightbank`. Its own test file recorded the gap
this programme closes — *"the live-engine path is exercised manually via
the RUNBOOK; these tests pin what CI can pin."* Every property here is a
property of that estate under injected faults, not an assertion in prose.

*(Amended by DL-109, at build — stage S7b.* The virtual-clock half of that
sentence is now false, which was the point of writing it down. CM-14 and
CM-09 are checked over the real 81-job night under seeded interleavings of
every fault one host can suffer — §9's "property of that estate under
injected faults", held rather than promised. The real-PROCESS half stands:
§9 calls it a separate tier and it is S7c.*)*

*(Amended by DL-112, at build — stage S7c.* That half is now held for the
layers that had none. S0–S4 each got a process tier as they landed; S5 and
S6 did not, so the mutex, the fence, the barrier's re-drive and the routing
table were all proved by one interpreter talking to itself. They are now
proved by two OS processes, or by one and the kernel:
`tests/test_runner_leadership.py`. What stays deliberately virtual is
arithmetic — waiting out §8's real `T_kill` proves nothing a controlled
clock does not.*)*

*(Amended by DL-150 — a conformance round against the shipped runner.*
Thirty-six findings held. Every one was a defect in this document, not in
the code, and no code changed. The corrections are folded **in place**
rather than added as blocks below: they repair sentences that had gone
stale, and a block per repair would bury the rule it repairs. §0 through
§11 all moved. The four largest: the log's unit is one estate of
period-bounded segments, not one run root (§2); the semantic projection is
stated for all three entity kinds (§3); the wire is v3 and `expect` has a
third namespace (§6); the catalog is immutable per PERIOD (§11). The code
follow-ups the round found are recorded in that entry, not here.*)*

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
re-implements — the §7 relay must reach the same verdict as the control
socket (`docs/runner-design.md` §10). In-process callers holding the
`Engine` object are not external and do not pass through it; they are the
trust domain the scheduler and the adapters inject from.

**The safety property, stated once:**

> No `(job, run_number)` ever executes twice, under any interleaving of
> leader failover, host reroute, message loss and duplication.

There is exactly one documented exception and §8 names it. `evict
--force` skips the proof that the old executor is dead, so it can produce
a double run. It is an opt-out an operator takes deliberately, and the
row records who took it.

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
| the log | one estate of period-bounded segments | `baseline_id` (per period), `epoch`, `committed_index`, `applied_index` |
| oracle state | one job / one global / one host | `state_rev` |
| effects | one effect | `effect_id` **bound to** `executor_id` and `generation` |
| execution host | one relay | `host_id`, `generation` (§8) |

The log's unit was one run root before the period model. It is one
estate now: a lineage of period-bounded segments that may share a run
root, each period carrying its own `baseline_id` (`docs/period-model.md`
§1, §2.1). `epoch` and the two indices stay monotone across a boundary.

`run_id` is the run's process identity, a uuid4. Since DL-118 it is
minted inside the step-7 decision transaction and rides on the durable
effect, so the WAL, the wrapper spec and the spool name one key
(`docs/period-model.md` §2.3, PR-36a).

## 3. State ownership and `state_rev`

**Owner.** `RuntimeState`: frozen `JobRuntime`, `GlobalRuntime` and
`HostRuntime` rows; private job, global, host, timer and capacity state;
typed operations for every one of them (`transition`, `start_run`,
`set_flags`, `set_armed`, `set_global`, `enqueue_timer`, the §8 host verbs
and the DL-120 capacity five). No mutable map escapes and no
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

**The semantic projection.** Three entity kinds carry a revision, so the
projection has three parts.

Include: a global's value; every `JobRuntime` field, plus that job's own
timers **with their ordering tokens**; every `HostRuntime` field.

Exclude: `state_rev` itself (else it justifies itself), `watching`
(effect state that moves with adapter activity and no committed input),
log locations, catalog metadata, and `spec_drift` (disk state). On the
host row, exclude `last_contact` (§8, DL-95) and `deadman_s`. Both are
observed liveness that the lease exchange moves with no committed input,
and a revision moved by either is one an audit cannot derive
(`docs/period-model.md` §3.3, PR-24b).

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

*(Amended by DL-120, at build — U1.* The pool did not stay. DL-86's
argument — "replaying the transitions replays the order" — is true only while
replay starts at genesis, and a seal does not; and `_bucket_used` summed units
held by live runs with units permanently spent (DL-50), so a checkpoint
recomputing usage from holders would have refunded every depletable. The
inventory is now closed: `reservations` and `waiter_seq` are `JobRuntime`
fields, projected with the row; `consumed` and `enqueue_counter` are under
`RuntimeState`; `CapacityPool` is a pure function of (catalog, rows,
consumed). `docs/period-model.md` §5 is normative.*)*

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
   the prior durable decision. An id that is admitted and undecided cannot
   occur while one writer owns the oracle and steps 5–7 do not yield, so
   meeting one is an error and fail-stops; the crash case is answered by
   replay, not by attaching to an attempt. *Only now* reject an unseen
   stale epoch — so an exact old-epoch retry recovers its original result,
   while an unseen old-epoch request is refused.
3. Assign the leader timestamp, monotone across inputs already admitted
   but not yet applied.
4. Atomically append one ordered batch: `TimeAdvanced(at)` +
   `InputAttempt`, and publish that record to subscribers. The envelope
   is durable, which is not the same as a decision, so the socket
   deliberately does not answer here.
5. Apply: earlier committed inputs, then this batch's time observation,
   firing timers through it. Timers fire **before** the precondition
   gate, because `feed()` fires timers due at or before the event's
   timestamp before applying it, and a `term_run_time` firing between
   gate and apply would defeat the precondition it just passed.
6. Evaluate `expect`; feed, or record the rejection.
7. Atomically record `ApplyResult`, revisions, outbox entries and the new
   `applied_index` as one `decision` record, publish it, and resolve the
   caller's result.

Steps 5–7 must not yield to another state-changing input.

**Worked example — one operator kill, three lines.** A job running at run 1,
revision 1. An operator sends `KILLJOB` naming that revision. This is what
the log gets from a run of the shipped code, with the hashes elided and
the keys in reading order rather than the sorted order the writer emits:

```jsonl
{"rec":"input","seq":2,"at":"…T02:00:30","kind":"KILLJOB","payload":{"job":"nightly"},
 "source":"control","fingerprint":"…","expect":{"job:nightly":1},"epoch":1,
 "request_id":"k1"}
{"rec":"decision","index":2,"request_id":"k1","decision":"applied","reason":null,
 "revisions":{"job:nightly":2},"legacy_batch":false,
 "effects":[{"effect_id":"e2:KILL:nightly.1","kind":"KILL","job":"nightly",
 "run_number":1,"run_id":"…","executor_id":"local","generation":0,
 "index":2,"at":"…T02:00:30"}]}
{"rec":"effect_result","effect_id":"e2:KILL:nightly.1","state":"applied",
 "run_id":null,"detail":null}
```

Two of those are step 4 and step 7 — the attempt, then the decision and
what it implies, committed together. The third arrives on the dispatch
that follows in the same loop iteration. Read them as the answers to four
different questions: *what was asked* (with the revision it was asked
against), *what was decided* (and which revisions moved), *what that
implied* (before it was attempted), and *what came of it*.

*(Amended by DL-118, period-model §2.3.* The decision and its effects were
two records here — `result`, then one `effect` per intent — each its own
fsync. Step 7 says "atomically"; two fsyncs are not that (CM-17). They are
now one `decision` line. The effect also gained two fields: `run_id` — the
KILL names the identity its run's SPAWN minted in its own decision
transaction (PR-36a), which closes the "`run_id` is not bound before the
attempt" deviation DL-96 recorded in §5 below for the local engine — and
`generation`, the host row's value at birth (PR-16). Nothing else in the
example moved.*)

Now change one thing at a time:

- **The same envelope again.** `request_id` and fingerprint both match, so
  step 2 answers from the decision index: no index consumed, no clock
  moved, the original `applied` returned
  (`test_cm05_an_exact_retry_takes_no_index_and_moves_no_time`).
- **The same `request_id`, a different command.** A fingerprint collision,
  refused — and a refusal leaves *nothing* in the log, which is what makes
  it different from a rejection
  (`test_a_refusal_leaves_nothing_in_the_log_and_a_rejection_leaves_a_decision`).
- **`expect` naming revision 1 after something moved it to 2.** Rejected —
  a decision, at an index, with `reason` set. It happened; it is in the
  log; replay honours it rather than re-deciding
  (`test_cm06_a_command_composed_against_a_stale_revision_is_rejected`).
- **A `term_run_time` deadline due one second earlier.** It fires as its
  own verbless input, takes the index, moves the revision — and the
  operator's command, composed against the old one, is rejected on
  arrival. Due at the *same* instant instead, it fires inside this input's
  own batch and does not invalidate it (§0's amendment; both halves are
  pinned).

*(Amended by DL-111, at build.* Step 4 above says the batch is
"`TimeAdvanced(at)` + `InputAttempt`" — two records. It is one: the `input`
line's own `at` IS the time observation, and `Journal.admit` makes a single
`_write` call. That is the stronger form of what step 4 asks for, and the
code says so where it lives ("one line, so the batch it carries cannot be
torn in half by a crash"); §1's own DL-100 amendment already describes it
that way — "atomic multi-record commit IS the one-line attempt record".
The `advance` record is a different thing and still exists: a time
observation with no verb, which is the other half of the input alphabet
(DL-44). The code comments this amendment called wrong have been
corrected: they now describe `TimeAdvanced` and the attempt as two
logical halves carried by one record.*)*

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
  `{effect_id, run_id, job, run_number, executor_id, generation}` — the
  host row's generation at birth, so an effect born before an eviction
  cannot pass for one born after it (PR-16).
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
  desired running and not terminal. The dispatch-time test is the row's
  status and its `run_number`; the `run_id` half is held by the outbox's
  one-to-one binding, which refuses a second identity for one run and a
  second run for one identity (DL-118).
- TERM and KILL apply only against exact run identity and the expected
  kill stage; they are **distinct effect stages with distinct ids**.
- SHUTDOWN binds to the intended supervisor incarnation and scheduler
  epoch.

Per-run effect ordering is mandatory. The SPAWN→SIGNAL race this depends
on is already closed (DL-83): a live wrapper with no spawn record answers
`not_ready`, so a kill can no longer be persisted as an applied no-op.

**Worked example — the delayed spawn that outlives its own run.**
Supersession by exact desired state reads as an arbitrary choice until you
see the run where the obvious guard fails, so here is that run. Held by
`test_a_held_spawn_whose_run_has_since_ended_is_retired_not_applied`.

1. `drain local`. `routes_new_effects` is true only for `active`, so the
   host now routes no new effect.
2. `STARTJOB j` at 08:00:30. `start_run` takes `run_number` 0 → 1 and the
   job reaches RUNNING. One effect is planned:
   `e2:SPAWN:j.1` — the id is *derived*, `f"e{index}:{kind}:{job}.{run_number}"`,
   not minted, so replay reconstructs it rather than trusting a uuid.
3. Dispatch reaches `_apply_effect`, whose first gate is routing. The
   effect is left **pending** — no outcome recorded — and that pending
   entry *is* the held set. Nothing launched.
4. `KILLJOB j`. The job goes TERMINATED. **`run_number` does not move**:
   `transition` writes `status`, `status_at`, `last_end_at` and maybe
   `exit_code`, and the only writer of `run_number` in the whole store is
   `start_run`. The row is now TERMINATED at run 1 — the same 1 that
   `e2:SPAWN:j.1` names. The kill plans no effect of its own, because a
   terminal for a job with no *live* run needs no kill.
5. `activate local`. The held effect now gets past the routing gate and
   reaches `superseded_reason` — the first moment anything compares it to
   the world.

A guard that compared generations would be asked `1 != 1` and would say
"still current". It is never even asked: the terminal arm short-circuits
first, and *that* is the point — had the order been reversed, the
comparison would have licensed the spawn. The effect retires with
`j is already TERMINATED: the run this spawn was for has ended`, and the
estate ends where it began: `live_jobs()` empty, `run_number` still 1,
nothing re-ran.

**Worked example — the third state, and why two would not do.** A
different run root: an engine decided a kill, recorded the intent, and
died before recording what came of it. At resume `_redrive_recorded_kills`
finds the pending KILL, finds no live wrapper to ask, and falls through to
the spool. `runs/j.1/status.json` decides it three ways, and only three
ways (`test_a_recorded_kill_is_resolved_from_the_spool_three_ways`):

| `status.json` | outcome | why |
| --- | --- | --- |
| `outcome: terminated` / `signaled` | `applied` | the signal landed |
| `outcome: exited` | `retired` | the run ended on its own first |
| absent | `indeterminate` | nothing observed it (E7) |

The third row is the one two states cannot express. `state_of` reads back
`indeterminate`, and `result_for` answers the caller `outcome_unavailable`
rather than an outcome object — a retry is told "nobody can say", which is
neither the success nor the failure a two-state model would have to
invent. The effect leaves `pending()`, so no later drain re-drives it
blindly: *nothing was tried* and *something was tried and cannot be
reported on* stay different facts.

*(Amended by DL-111, at build.* Writing those examples put three of this
section's own sentences against the code, and the code won all three.

**The states are FOUR, not three.** `EffectOutcome.state` is
`applied | indeterminate | retired`, plus `pending` as the absence of one.
`retired` is not an omission: it is the recorded outcome of the
supersession rule this same section states three paragraphs above, and
DL-98 declined renaming it precisely because "safe to forget" and "must
not be forgotten" are different facts. The enumeration above was written
before the rule below it had an implementation, and never caught up.

**"Tombstones carry fingerprints and reject collisions" is not built.**
Neither `Effect` nor `EffectOutcome` carries a fingerprint. It is not
obviously needed either, which is why it went unnoticed: DL-96 made
`effect_id` *derived* from `(index, kind, job, run_number)`, so two different effects
cannot share an id unless the log itself is inconsistent, which is a
corruption case and not a client one. Left unbuilt, named here rather than
quietly dropped; it becomes real if an id is ever minted rather than
derived — which is exactly what the relay would need. *(This paragraph
also said `Outbox.record` overwrites a differing effect under one id.
Since DL-118 it does not: an exact repeat is a no-op, a differing record
is refused as a log that disagrees with itself, and the same method
refuses either direction of a broken `(job, run_number)`-to-`run_id`
binding.)*

**DL-96's amendment overstates the live path.** It defends dropping §5's
pre-attempt `run_id` binding with "the outbox records the process identity
the spool reports, when it reports it". On the live path it never does:
`_apply_spawn` and `_apply_kill` both resolve with `run_id` left at its
`None` default and never revise it. `run_id` is populated only on the two
*resume* paths. The deviation still holds for the reason DL-96 gave — the
run directory is the identity locally — but the sentence describes a
recording that does not happen. Since DL-118 the deviation itself is closed
— `run_id` is bound at effect birth; see the amendment in the DL-96 block
below.*)*

*(Amended by DL-96, at build — stage S5c.* Four deviations, each bounded and
each because the thing it defers against does not exist yet.

**`run_id` is not bound before the attempt.** This section binds it
atomically for a reason a RELAY has and a local engine does not: a relay
sees only ids. Locally `(job, run_number)` IS the identity —
`runs/<job>.<run_number>` is created with `mkdir()` and no `exist_ok`, so a
second spawn of one run fails loudly rather than doubling — so the outbox
records the process identity the spool reports, when it reports it. Binding
it earlier arrives with the relay that needs it. *(Closed by DL-118 before
any relay: the seal needed the binding first. A SPAWN's `run_id` is minted
in the step-7 decision transaction, rides in the durable effect, and the
wrapper spec carries it — period-model §2.3, PR-36a.)*

**TERM and KILL are one effect, not two staged ids.** The split exists so a
relay can tell a retried TERM from a retried KILL. The adapter's ladder
never yields to the engine between its stages, so there is no
engine-visible state between them for a second id to name; a re-driven kill
re-runs the whole ladder, and TERM to a dead group is a no-op.

**SHUTDOWN is not an effect yet.** It binds to a supervisor incarnation and
a scheduler epoch. Both are allocated now — the incarnation by the
supervisor at start (DL-80), the epoch by S6a — so what defers it is no
longer a missing identity. It is that nothing needs it: the shutdown path
speaks to the supervisor directly and leaves no intent for an outbox to
carry.

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
{"v": 3, "baseline_id": "…", "epoch": 7, "request_id": "…",
 "verb": "CHANGE_STATUS", "payload": {…}, "expect": {"job:nightly": 12},
 "claimed_actor": "alice@host"}
```

No client-supplied `at`: a future stamp is a timer fast-forward and a
backdated one breaks monotonicity. `epoch` shipped in v2 while it was
still inert on one host, because adding it after the CLI and TUI migrate
is a second wire break; it is required in v3 and S6 allocates it for
real. The wire is **v3** (`docs/control-protocol.md`); v1 and v2 are
retired and refused by version. `expect` names only the addressed entity,
with keys namespaced `job:` / `global:` / `host:` (DL-93).
`claimed_actor` is a client hint — **the leader stamps the authenticated
principal** into the admitted record where there is one to stamp (§8,
DL-147).

Fingerprint = the complete semantic envelope including `baseline_id` and
`epoch`, excluding transport framing.

Reads publish `baseline_id`, `epoch`, `applied_index` and `state_rev`.
`global {name}` / `globals {names: […]}` answer
`{present, value, state_rev}` for a *named* entity and insert nothing — a
map of existing globals cannot express the absence that a conditional
create must condition on. `hosts` answers the routing table the same
way, and with no `ids` it answers the WHOLE table, because a routing
table is a small inventory the §7 barrier walks in full.
Revision-bearing reads are leader-only. What a read re-proves at the door
is the LINEAGE — an engine that can no longer prove it leads the estate's
lineage refuses, without the header (`docs/period-model.md` §1.3,
DL-133). The run-root proof is re-checked before the next append and
before dispatch (§7), not per read.

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

**Worked example — one failover, in order.** Engine A is running three
jobs; `fast` has finished, `slow_one` and `slow_two` are mid-run. A dies by
`SIGKILL` — no shutdown, no records written for what was in flight. Engine
B starts on the same run root
(`test_sigkill_engine_midrun_then_resume`):

1. **ACQUIRE.** B `flock`s `leader.lock` and, after the lock is held,
   checks that the name still points at the inode it locked — a lock on an
   unlinked inode excludes nobody. It then reads the sentinel and, on a
   root that has one, acquires and validates the lineage anchor the same
   way, composing both into one `Fence` (`docs/period-model.md` §1.3); a
   root with no anchor holds a fence of one lock. Only then does it read
   the log. The epoch is allocated by being *appended*: a `leader` record
   naming epoch 2, so every input after it is attributable to B and every
   input before it to A.
2. **Reconcile every execution host.** The candidate set is the union of
   the log's `dispatch` records, the `runs/` directory, and what the host
   LISTs — three witnesses to one question, because concluding "never
   spawned" from a missing directory alone would re-drive a run the host is
   still holding.
3. **Retire superseded, re-drive pending.** `slow_one` and `slow_two` each
   have a run directory whose wrapper recorded `parent lost` when A's
   lifeline closed, so their real endings are injected as completions —
   through the §4 stale gate, like any adapter completion. Nothing is
   re-driven here, because nothing was left pending: A recorded each
   effect's outcome at dispatch.
4. **Dispatch.** The barrier ends where it says it does. On this run there
   is nothing left to drain; on the run where A died in the window *between*
   recording an intent and acting on it, this is the step that delivers it
   (`test_cm09_a_start_the_previous_leader_never_dispatched_is_re_driven`,
   and its contrast, `test_a_start_with_no_recorded_intent_is_still_failed`).

The ordering that matters is the one a reader is most likely to invert:
**an effect is recorded `applied` before the work starts.** `_launch`
creates a task and the outcome is written on the next statement, with no
await between, so the WAL order on a real run is `decision{effects:[…]}` →
`effect_result{applied}` → `dispatch`, and the wrapper's own `spawn.json`
— written by the process that spawned, not by the engine — arrives later
still. That is why a *pending* SPAWN means something so specific: not "a
spawn that may or may not have run", but "an intent nothing ever acted on",
which is the only reading under which step 3's re-drive is safe.

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

**Every append, and again before dispatch.** The re-check is one `stat`
at the top of the WAL append, before the write, so a leader that cannot
prove it leads admits nothing rather than admitting and then discovering
it had no right to. For the run-root lock that covers dispatch on its
own, because §5 puts the outbox record *before* the attempt: an append
this engine may not make is an effect it never applies. The period
model added a second proof, the lineage anchor, and an append and a
dispatch are two acts — so the whole `Fence` is re-proved once more
immediately before the outbox is drained (`docs/period-model.md` §1.3,
PR-03). There is deliberately no background prober: an engine with nothing
to append and nothing to dispatch is relying on no proof.

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

*(Amended by DL-102, at build — stage S6c.* The barrier's four steps, as
built, and the one question this section deferred to it.

**"Re-drive pending" is the answer DL-96 sent here.** `docs/runner-design.md`
§7 fails a start with no spool trace rather than re-running it, and that was
one rule because the log held one kind of evidence. It now holds two. A
start whose SPAWN is still *pending* is an intent the previous leader
recorded and never delivered — nothing anywhere ran — so the barrier
re-drives it at the run_number the oracle already decided. A start with no
pending intent is failed exactly as before: a journal written before the
outbox existed, or an effect already resolved whose spool has since gone.
The rule was not overturned; it was split at the seam the outbox put there.

**Re-driving needs no mechanism.** Leaving the effect pending is the whole
of it: dispatch drains the outbox through the same gates a fresh effect
passes, so a drained or quarantined host holds it (§8) and the sweep does
not have to know that. Two rules that each knew about routing would be one
too many.

**Reconcile every execution HOST is load-bearing, not a turn of phrase.**
What the host LISTs joins the sweep's candidate set beside what the disk
shows. "Never spawned" is concluded from absence, and absence that only
meant "the run directory is gone" would re-drive a start the host is still
running — the double run this document exists to prevent. The test asserts
no second process, and the guard is mutation-tested.

**The barrier ends in a dispatch,** as written. Without it the outbox is
drained only on the way out of the next admitted input, so a re-driven start
would wait on unrelated traffic to arrive: hours on a quiet estate, and
never on one whose only remaining work was the run that was lost.*)*

*(Amended by DL-130, at build.* **`catalog_hash` is v2, and it is
versioned.** v1 hashed the whole `CatalogIR`, `CatalogMeta.tool_version`
included, so a patch release that changed nothing else moved the hash and
this gate refused to resume a live estate — the outage DL-100 already
refused to manufacture for the state-machine version, arriving by the other
door. v2 is sha256 over the §3.2 canonical form of `CatalogIR` with `meta`
projected to `{source_files}` only: `tool_version` and `parsed_at` are
diagnostic and leave, and **spans stay** — a relocated or reordered estate
is still a different estate (`docs/period-model.md` §1.1). The version is
carried explicitly as `catalog_hash_version` on the opening record and on
the period manifest, never inferred, and **the gate recomputes under the
recipe the log itself names**: a `segment` pins v2. What eligibility means is
unchanged: two pins, both exact.*)*

*(Amended by DL-138.* **Version 1 is retired.** The sentence above used to add
"a legacy `header` pins v1 and is compared under v1 for the rest of its life",
because comparing across recipes would have refused every journal then in
existence. No such journal exists: the `header` record and `catalog_hash`
version 1 are retired dialects, refused by name at one dispatcher
(`docs/protocol-evolution.md`). The gate reads one recipe, and a record naming
version 1 is refused rather than compared. The same entry closes the DL-100
courtesy above: a `header` "that pins none" cannot arrive, so nothing reads
as state-machine version 1 by default. The record at position zero is a
`segment`, and it pins both.*)*

## 8. Host lifecycle: active, passive, quarantined, evicted

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

It is **opt-in per period**, because it costs something real: today a
supervisor tolerates an absent controller indefinitely, which is what
lets an engine crash and resume with its runs intact (DL-79).
`RuntimeProfile.deadman_us` pins what a period asks for, and the bound
uses the value read back into the host row, so successive periods in one
run root may differ. A host row with no observed deadman is never
reroutable except by force.

**Eviction preconditions.** `evict` is refused unless all hold:

1. the host is unreachable from the leader;
2. the host runs a deadman;
3. `now - last_contact > T_deadman + T_kill + T_skew`.

`T_skew` covers monotonic-clock **drift** between hosts (parts per
million over the interval it is added to, which is
`T_deadman + T_kill`), not clock synchronization — the argument is
the standard lease argument and depends only on bounded drift. A refusal
reports the remaining wait, so the operator waits rather than guesses.

*(Amended by DL-151.)* **`T_kill` is DERIVED from the grace the period
runs**, not a constant: `2 × cmd_grace + 10s`, over the grace the leader's
own CMD adapter is wired with — which resume holds to
`RuntimeProfile.cmd_grace_us`, so the wiring and the pin are the same number
on any estate this engine may lead. Two graces because two waits stack — the
wrapper's own TERM-to-KILL wait, then the supervisor's wait for its
wrappers — plus a margin for the supervisor's exit. The grace is per
period and unbounded above while `T_kill` was fixed at 30 s, so a period
running a grace over roughly 15 s had a bound that no longer covered the
kill it exists to cover, and `evict` could be permitted while the old
command was still inside its TERM grace: the double run this gate is for.
At the 10 s default the derivation is 30 s, so the worked example below
and every default estate are unmoved.

**Force is attributed, not forbidden.** `evict --force` skips the
precondition, is recorded with the claimed principal (§6: the leader stamps
an authenticated one when there is authentication to stamp), and is the
one path in this document that can produce a double run. It exists
because an operator with out-of-band knowledge — the machine is
physically powered off, the disk is out — is sometimes right, and waiting
out a deadman is then pure loss. It is loud, durable and attributable;
that is the whole of its safety story. *(Amended by DL-151.)* Which is why
a force that names NOBODY is refused: an unattributed force writes
`forced_by: null`, and the row then reads exactly like a proof-gated
eviction — the one thing that field exists to tell apart. `claimed_actor` is
required on `--force` and on nothing else. There is no flag for it and no
new operator step: `dsl41 host` always sends the claim, and on an armed
estate the perimeter replaces it with the authenticated principal (§6). The
refusal answers a client that sends none, and it is a rejection like any
other precondition.

**Eviction is fenced on return.** Eviction bumps the host's `generation`.
A returning relay presenting a stale generation is refused registration
and must self-fence — kill surviving wrappers — before it may re-register
as active. For a correct eviction this is a formality. For a mistaken
`--force` it is the detector: it cannot un-run the duplicate, but it
stops it continuing and it turns a silent divergence into a recorded
incident.

Re-driving an evicted host's held jobs issues **new runs with new effect
ids**, never retries of the old ones (§5).

**Worked example — the bound, in seconds.** A host running a 60-second
deadman, unreachable since 02:00:00, quarantined by the leader. `T_skew` is
`max(interval × 200ppm, 1.0)`, so for this interval the floor wins and the
bound is `60 + 30 + 1.0 = 91.0` seconds. An operator reaching for `evict`
at 02:01:00 is told, verbatim:

> host 'local' was in contact 60.0s ago and the ss8 bound is 91.0s (deadman
> 60.0s + kill 30.0s + skew): wait 31.0s more, or --force with proof it is dead

The bound and the wording are pinned by
`test_cm11_eviction_is_refused_before_the_bound_and_permitted_after`. That
is a **rejection**, not a refusal: it read mutable state, so it is a
decision at a real log index and replay reaches the same verdict from the
same row, which is pinned by
`test_a_rejected_host_command_replays_as_the_rejection_it_was`.
At 02:02:00 the same command returns no reason at all and applies — the row
becomes `evicted`, `generation` goes 0 → 1, and `forced_by` stays `None`,
because on the gated path the preconditions *are* the justification.

The other two preconditions fail with their own sentences, and which one an
operator sees depends on the order they are checked in — precondition 1
first, so a `passive` host with no deadman is told about the drain, not
about the deadman:

| row | what it is told |
| --- | --- |
| `passive`, no deadman | *"is passive: eviction needs the leader's own durable record that the host is unreachable (ss8 precondition 1)…"* |
| `quarantined`, no deadman | *"runs no deadman (ss8 precondition 2): nothing bounds when its wrappers die…"* |
| not in the table | *"a host joins it by registering, never by being addressed"* |

`--force` on the same host one second after contact — two of the three
preconditions unmet — returns no reason and writes
`forced_by="alice@ops-laptop"` into the row
(`test_cm11_the_force_that_skips_the_proof_carries_the_claim_that_asked_for_it`).

*(Amended by DL-111, at build.* Working that example through the shipped
code corrected this section twice.

**"Recorded with the authenticated principal" is not what happens, and
cannot be yet.** What `forced_by` holds is the envelope's `claimed_actor` —
whose own docstring calls it "a CLAIM: the control socket has no
authentication (control-protocol §7 gap 2) … never an authorization". So
force's safety story is *attributable* rather than *authenticated*: the row
records who said they were asking, durably and loudly, and a socket anyone
with the uid can reach is what stands behind that. §6 already says the
leader stamps an authenticated principal and that no leader can do that
yet; this section wrote the end state as though it were the current one.
The word here is now "claimed", and it goes back when there is an
authenticated principal to stamp.

**A one-host table makes half this section unreproducible today,** which is
worth saying plainly because the example above had to be written against
`local`. `register_host` has one caller, `seed_local_executor`, and every
caller of that passes `LOCAL_EXECUTOR_ID`: no CLI or startup path can set
`executor_id`. So the routing table holds exactly one row, and
`dsl41 host evict prod-a` answers "no host 'prod-a' in the routing table"
rather than anything about a bound. Every rule in this section is
implemented and tested; what is missing is a second row to point them at,
which is the relay's business (DL-97, DL-103).

**Two things are missing, not one.** Beside the second row there is the map
from a ROLE to an executor. `seal.RouteRuntime` freezes that row's shape and
`implicit_routes` projects the only honest value today: one route whose role
IS the local executor's id, at revision 0, because no verb that could move it
exists. The storage under §3's owner and the `host: {verb: "route", id,
executor_id}` wire record both arrive with the relay
(`docs/period-model.md` §3.3).

**And one latent bug, fixed rather than documented.** `evict_host` left
`state_before_quarantine` set, while the field documents itself as non-null
only while the row is `quarantined` — and a gated eviction can only start
FROM quarantined, so every gated eviction falsified the invariant. Harmless
today, because `reinstate_host` refuses to act on a row that is not
quarantined. It is a loaded gun for whoever writes the next transition, so
eviction now clears it.*)*

*(Amended by DL-147.* DL-146 built the missing principal for local peers:
with an access map configured, the control server authenticates the peer
by kernel credential and overwrites `claimed_actor` with the canonical
spelling before the row is written, so `forced_by` holds an authenticated
identity on an armed estate — and remains the bare claim on an
unconfigured one. The word "claimed" stays, because arming is optional;
the web session's per-user identity is still open under
`web-session-principal-v2` (`docs/access-model.md` §3, §9).*)*

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

*(Amended by DL-112, at build — S7c.* "A separate tier" understates what
separates them. The model harness answers *which interleavings are safe*,
which is a question about orderings and needs one interpreter that can
hold them all. The process tier answers *whether the mechanism is the one
described*, which is a question about the kernel: an `flock` is only a
mutex if a second process is refused it, a fence is only a fence if an
unlink is noticed, and a crash window is only a window if a process can
actually die inside it. Neither tier can be asked the other's question,
so neither substitutes. What the process tier must NOT do is re-derive
arithmetic: waiting out §8's real bound would add a minute per run and
prove a sum. `tests/test_runner_leadership.py` holds S5/S6; S0–S4's live
in the supervisor and lifecycle tiers.*)*

Obligations. Tests are named `test_cmNN_*`, on the house convention of
`test_semXX_*`.

| # | obligation | cost |
| --- | --- | --- |
| CM-01 | structural owner gate | landed (DL-82/83). Enforced by `scripts/arch_check.py`, not by a `test_cm01_*` -- a gate over the model's AST is the only form this obligation has |
| CM-02 | cardinality: one increment per entity per committed input | landed (DL-87) |
| CM-03 | corroborating property, generator widened | landed (DL-87) |
| CM-04 | timers fire before the gate (`term_run_time` fixture) | landed (DL-89) |
| CM-05 | dedup precedes admission: a retry advances no logical time | landed (DL-89) |
| CM-06 | retry / fingerprint / eviction, incl. `outcome_unavailable` | retry + fingerprint landed (DL-90); `outcome_unavailable` landed (DL-96); every eviction precondition landed with S5d (DL-97) and is pinned by the CM-11 tests |
| CM-07 | two-pass replay, incl. admitted-without-result | landed (DL-89) |
| CM-08 | bisimulation unchanged | landed: the phase-11a gate, which every SEM trace already runs through both interpreters. No `test_cm08_*` of its own -- the obligation is that the existing suite stays green |
| CM-09 | at-least-once delivery **and** at-most-once application; superseded effects retired; quarantine holds | application half + supersession landed (DL-96); quarantine holds landed (DL-97); local delivery landed (DL-102: the barrier re-drives a pending SPAWN and a pending KILL); both re-proved against real processes at S7c (DL-112) -- an engine that really died in the outbox window, and a quarantine set by renewals that really failed; remote delivery waits on the relay |
| CM-10 | the deadman fires: an unleased supervisor exits and its wrappers die | landed (DL-95) |
| CM-11 | `evict` refused before the bound, permitted after; `--force` recorded with its claimed principal | landed (DL-94/95/97): every precondition now produced rather than built by hand, and at S7c (DL-112) produced by real processes end to end -- a deadman read back off a live supervisor, a `last_contact` stamped by a real lease exchange, a quarantine no test wrote by hand |
| CM-12 | a returning evicted host is refused and self-fences | the refusal landed (DL-97); the self-fencing is the relay's act and waits with it |
| CM-13 | drain: `passive` routes nothing new and finishes what is running | landed (DL-94) |
| CM-14 | no `(job, run_number)` runs twice, over seeded interleavings | **single-host half landed** (S7a/S7b, DL-108/DL-109): 48 seeded interleavings over the four-job fixture and 16 over nightbank's real 81-job night, covering failover, a spawn decided and never acted on, duplicated and stale completions, quarantine and drain — every fault one host can suffer, each asserted to actually fire. S7c (DL-112) adds the half no interpreter can hold: the mutex is refused between two OS processes, and an engine that loses its lock file stops before the work rather than after it. The remaining half is §0's "host reroute", which needs a host to reroute TO; it closes with the relay (DL-97/DL-103), not before |

CM-01–CM-14 are this document's. `docs/ha-deployment.md` §7 drafts
CM-15–CM-23 for the second host and the second site. One of them landed
early: CM-17 — a decision and the effects it implies commit together or
not at all — is held on the file substrate by DL-118's single `decision`
record, and pinned by `docs/period-model.md` PR-35.

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
S3  mandatory preconditions + protocol v2 (retired at DL-118; wire is v3)
S4  CLI / TUI          ∥   S5  relay + host identity, effects, barrier,
                              deadman, host states + evict
                              (S5a-d landed; the relay and the barrier moved
                               to S6 by DL-97 -- both rest on election or on
                               a second host, and neither exists before it)
S6  ledger + election (S6a-c landed, DL-99: election, the fence, the
    barrier. The RELAY did not land here -- see below)
S7  failover / partition / double-run matrix over nightbank
```

*(Amended by DL-103, at build — closing S6.* DL-97 deferred the relay with
the trigger "build it when there is a second execution host to route to,
which in practice means alongside S6". S6 has landed and the relay has not,
so the distinction that sentence packed into "in practice" is worth
unpacking: the trigger is a **second execution host**, and S6 was the
expected occasion for one, not the condition. Nothing in election, the fence
or the barrier produces a second machine or answers §7's open question of
how mutually authenticated principals are named, issued and rotated. The
barrier that S6c did build is the local half — it reconciles every host in
the routing table, and today that table has one row.

What S6 does hand the relay is the thing it was missing: an epoch that is
allocated, monotone, and re-checked on every append, so "a relay rejects any
dispatch carrying an epoch below the highest it has seen" now names a value
that moves rather than a constant. The trigger stands unchanged.*)*

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

The supervisor tier's SEMANTICS: fencing plus effect idempotency is
enough, and it holds no job definitions to version. Its WIRE is versioned
like every other — `"v"` on each request and an `unsupported_version`
refusal (`docs/supervisor-protocol.md` §5,
`docs/protocol-evolution.md` §1).

The catalog, immutable per **period** by three guards — one read hashed
and parsed from the same bytes, no reload path, and a used period
refusing re-baselining. A catalog change is a sealed transition and a
restart, and the successor segment and manifest pin the new hash and the
recipe it was taken under (`docs/period-model.md` §2.1).

Time observations, which are consequences rather than commands.
