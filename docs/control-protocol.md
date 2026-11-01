# Control protocol — the engine's public contract

Status: frozen at **v3** (DL-118). Entries that rule parts of it include
DL-133, DL-135, DL-146, DL-147, DL-148, DL-150, DL-151, DL-158, DL-172,
DL-189, DL-216, DL-217, DL-228, DL-233, DL-256, DL-264, DL-267, DL-272,
DL-274, DL-292, DL-295, DL-297 and DL-353. This document is normative for
the control plane of `docs/runner-design.md` §10. A change to a frozen item
requires a decision-log
entry. Each entry is cited where it applies.

**The engine accepts v3 only.** Every request names the protocol version
(DL-90), and the engine accepts only v3 (DL-118). It answers a request
that names no version or any other version, v1 and v2 included, with a
refusal naming the version it speaks.

The control protocol speaks in semantic terms (conditions, boxes,
statuses, sendevent verbs), which runner-design §11's scope fence assigns
to dsl41 permanently. The engine starts wrappers under
[wrapper-protocol.md](wrapper-protocol.md).

```text
operators / TUI / CLI ──[ this document ]──▶ engine ──[ wrapper-protocol.md ]──▶ wrappers
```

This shows the control and wrapper boundaries, not the command processes.
The supervisor tier and its protocol are retired (DL-424).

Implementation: `src/dsl41/runner_control.py` owns both ends: the server,
the wire vocabulary, and the three clients.

## 1. Roles

- **server** (`ControlServer`): one per run root, owned by the engine
  process. Mutating verbs inject into the engine's single-writer loop.
  Query verbs are pure projections; §4 says what that claims.
- **async client** (`ControlClient`): one persistent request/response
  connection, plus a separate connection for each `subscribe`. Drives the
  TUI of runner-design §11 (`dsl41 run --ui`, `dsl41 ui`, `dsl41 serve`).
- **sync client** (`roundtrip`): one-shot blocking request/response for
  callers outside an event loop. Drives `dsl41 sendevent`, `dsl41 query`
  and `dsl41 off-hold-all` (DL-423).
- **blocking subscription client** (`subscribe_lines`, DL-172): one
  connection held open for the life of a `subscribe`. It yields the ack
  and then store records, and drives `dsl41 query subscribe`. It lets a
  transport `OSError` propagate and raises `ControlClientError` for a
  protocol failure.

The async and sync clients raise `ControlClientError` on any transport or
decode failure. None of the three maps errors to exit codes; that is the
CLI's job.

## 2. Transport and framing (frozen)

- A **unix domain socket** at `<run_root>/control.sock`, mode `0600`. The
  mode is set by umask at bind and set again by chmod after, because some
  platforms ignore umask on bind. When an access map names a
  `socket_group`, the socket keeps its owner, takes that group, and is set
  to `0660`; the run root opens to `0710` traversal
  (`docs/access-model.md` §8, DL-146, DL-147). Estates without a
  configured map keep `0600` exactly.
- **JSON lines**, both directions. One request object per line; one
  response object per line. The stream buffer limit is `LINE_LIMIT`
  (16 MiB). One `status` response covers every job on a single line, and
  it overruns asyncio's 64 KiB default at roughly 300 jobs. A `subscribe`
  stream's record lines can be longer, up to the §5 budget
  (`4 × LINE_LIMIT`, 64 MiB), and a subscriber reads them at that limit.
  A line ends at its `\n` (DL-216). A request fragment that reaches EOF
  without one is dropped unanswered and never parsed: its client died
  mid-write, and a truncated command must not run.
- **A client that attempted a write cannot prove the request did not
  arrive** (DL-216). A write can fail after the peer took every byte, or
  some of them. So a client treats any failure from its first write
  attempt on as an unknown outcome (`delivered`), never as "not sent".
  Only a failure before any write, such as a refused connect, is
  undelivered.
- **Every request carries `"v": 3`**, including queries and `subscribe`.
  The check runs before the request is routed. So `subscribe`, which owns
  its connection and never reaches the response path, is not an
  unversioned door. A refusal does not close the connection: the next
  line on it may be well-formed.
- The answer to a request is `{"ok": true, …}` or
  `{"ok": false, "code": "<code>", "error": "<message>"}` (DL-272).
  `code` is stable and names the reason. The code table below lists every
  code. `error` is human-readable prose and may change. The other lines a
  server writes, the §5 store records and the gap marker, carry no
  `ok`.
- Responses carry the **read header**: `baseline_id` and
  `applied_index` (`docs/concurrency-model.md` §6). A revision means
  nothing without the period it was read from (`baseline_id` names the
  period, DL-117). A client that cannot name the period cannot be told it
  holds a stale one. The header is stamped in one
  place (DL-148): on the answer to a request that passed routing and the
  §4 lineage proof. Everything else is headerless: an answer sent before
  routing (the malformed line, the version refusal, the DL-146
  perimeter's credential refusal and denial), the lineage refusal itself
  (§4, PR-03), the internal-error answer of a handler that raised, and
  every line `subscribe` writes on its own connection (§5). None of them
  names a revision.
- A malformed line answers `{"ok": false, "code": "malformed_request",
  "error": "bad request: …"}`, and the stream stays in sync. That holds
  for invalid JSON **within** `LINE_LIMIT`. A line over the limit is a
  framing failure the reader cannot answer past. The connection closes
  with no answer, and the caller reads it as a transport error. A handler
  that raises answers `{"ok": false, "code": "internal_error", "error":
  "internal error: …"}` rather than leaving the request unanswered. A
  client must never see a bare timeout for a query bug. A statechart error
  is not a query bug: it gets no answer, the connection hangs up, and the
  engine stops with exit 7 (`concurrency-model.md` §4).
- Consumers must ignore unknown fields (forward compatibility).

**Socket lifecycle.** On start the server probes an existing socket file
by connecting. A successful connect means a live engine already serves
this run root, and the second engine refuses. A refused connect means a
crashed run's leftover file, which is unlinked and claimed. The probe is
not the election, and neither is the bind: the bind replaces an existing
socket file rather than failing on it. The `leader.lock` flock is what
keeps two engines apart. It enforces one engine per run root
(`docs/concurrency-model.md` §1, DL-100). The engine takes it before it
opens anything, so the probe runs after the lock is held. The probe
cleans up a stale socket file and is a second refusal door: it refuses
to start when a live engine answers it.

**The version handshake** is `"v": 3` on every request (DL-90, DL-118).

**Stopping the engine** is `{"cmd": "engine_stop", "v": 3}`, the
`dsl41 engine stop` verb, at the `adm` tier (`docs/access-model.md` §10).
It is a step of the engine's single writer, as SIGTERM is: every mutation
is admitted wholly before it, or after it and rejected with
`engine_draining`. `status`, the other queries, `subscribe` and
`engine_stop` stay legal while the engine drains. The answer is
`{"ok": true, "engine": "DRAINING"}`, or `"STOPPED"` when nothing was left
to settle. A second stop moves the drain's deadline to now. The engine
then records `clean_shutdown` or `unclean_shutdown` and exits 0; its
socket goes with it. A stop the loop does not take within the decision
window answers `decision_timeout`.

**An addition to this section (`update`, spec-reload, DL-427).**
Applying a stored plan is `{"cmd": "update", "v": 3, "token", "stop_running",
"wait", "request_id", "baseline_id", "expect": {"config": N},
"claimed_actor"}`, the `dsl41 update` verb, at the `adm` tier
(`docs/access-model.md` §10). `token` is the 64-hex-digit plan token
`dsl41 reread` printed; the plan is read from `<run_root>/plans/<token>/`.
`expect` names the configuration rev the caller read (`status`'s
`config_rev`, or the plan's own); `request_id`, `baseline_id` and
`claimed_actor` are the mutation envelope of §3. The request is a step of
the engine's single writer. A refusal writes nothing and answers
`{"ok": false, "refused": true, "code", "error", "token", "names"}`, with
`names` the jobs or boxes it concerns, empty for a refusal about the
request itself (`invalid_argument`, an envelope code, `request_id_reused`,
`engine_draining`). An admission is recorded in the store before it
is answered: `{"ok": true, "decision": "admitted", "token", "request_id",
"stop": [jobs], "application": null}`. With `"wait": true` the answer comes
once the update applied, its `application` the applied record's `rev`,
`catalog_hash`, `token`, `added`, `changed`, `removed`, `restored` (each
re-added name with the flags its tombstone kept) and `stopped` (each job
of the stop set with what follows its stop, chosen once at the
application: the plan committed with its outcome, `complete`, `rerun`,
`hold` or `none`; `rerun` for a file watch taken up again; `none` for a
job with no attempt to stop, whose held start then goes under the new
definition). The engine waits for the application `update_window_s`
seconds, which `status` reports (the stop grace and the decision window);
past it the answer is `"pending": true`, and a client waits on the socket
at least that long. An exact retry under the same `request_id` with the
same token, consent, `expect`, baseline and actor is answered from the
first admission, and once applied with its application, also while a
restart finishes it; the same id with another request is
`request_id_reused`, as is an update under a `sendevent`'s id and the
reverse. While the update stops, the engine stays `RUNNING`, every job of
the plan's added, changed and removed sets that the oracle starts waits as
`dispatch_pending`, and a second `update` is refused with
`update_in_progress`. A new `update` while the engine drains is refused
with `engine_draining`; an exact retry of one admitted before is answered
from its admission.

**No controller lease,** by choice (DL-41a). `sendevent` is multi-writer
by AutoSys nature, and the engine's single-writer loop serializes every
injection. The supervisor tier and its lease are retired (DL-424).

**Error codes** (DL-272). Every `ok: false` answer carries a `code`:
headerless answers, query errors and the `ok: false` lines of a
`subscribe` stream included. A code names the reason, never the outcome.
The outcome markers `refused` and `decision` keep their meaning, and one
code can appear
under more than one outcome: `unknown_job` is a query error under
`status` and a refusal under `sendevent`. An `unknown` outcome never
carries `refused`, whatever its code.

A rejected answer's `code` is the one its `decision` record stores
(`docs/period-model.md` §2.3). An exact retry is answered from that
record, so it answers the same code as the first answer. A `decision`
record may store no code: one written before DL-272 has none. An answer
replayed from it omits the field, so a client must tolerate a missing
`code`.

`code` is an additive field: consumers ignore what they do not know
(above), so a client that does not read it keeps working. A code is
never renamed in place; a new code, or a retired one, is a decision-log
entry. `transition_violation` is a retired code, and this engine never
answers it (DL-353). In 2.0.0 the engine answers none of the 1.x election,
routing-table and seal codes: `stale_epoch`, `lineage_lost`,
`period_sealing`, the host codes (`unknown_host`, `host_quarantined`,
`host_evicted`, `host_already_evicted`, `force_needs_actor`,
`host_not_quarantined`, `host_no_deadman`, `host_never_contacted`,
`eviction_bound_pending`) and the seal codes (`seal_retry_mismatch`,
`seal_in_flight`, `no_lineage`, `stage_digest_mismatch`, `nothing_staged`,
`seal_input_after_cutoff`, `seal_not_settling`, `seal_not_quiescent`,
`seal_supervisor_unproven`, `seal_run_unaccounted`, `seal_retry_horizon`,
`seal_refused`, `seal_timeout`).

The client action says what a client does next:

- **re-read; retry a mutation only under the same request_id**: the
  outcome is unknown (§3). Re-read first. A mutation is retried only
  under its original `request_id`, with the same envelope. A query has
  no id to retry under.
- **re-read then decide**: the state moved, or the request named a
  stale view of it; read again before composing anything.
- **fix the request**: the request is wrong as sent; sending it again
  unchanged gets the same answer.
- **wait**: a condition clears on its own; send again later.
- **operator**: a person has to act on the estate or the engine.

| code | outcome class | reasons | client action |
|---|---|---|---|
| `peer_unauthenticated` | refused | access control is armed and the peer has no kernel credential | operator |
| `malformed_request` | refused | the line is not a JSON object | fix the request |
| `unsupported_version` | refused | `v` is absent or is not `3` | fix the request |
| `access_denied` | refused | the perimeter denies this principal the `cmd`; it classifies by `cmd` alone (`docs/access-model.md` §5, §10) | operator |
| `internal_error` | unknown | a handler raised | re-read; retry a mutation only under the same request_id |
| `unknown_cmd` | refused | `cmd` names no command | fix the request |
| `invalid_argument` | refused, query or stream | a field is absent or has the wrong shape or type, or a `request_id` begins with the reserved prefix `engine:` (§3); `error` names the field | fix the request |
| `plan_cycle` | query | the AND-success skeleton has a cycle, so `plan` is disabled | operator |
| `unknown_job` | query, or refused on a mutation | the job is not in the catalog (for `status`: in neither the catalog nor the store) | fix the request |
| `no_journal` | stream | the run has no store to stream | operator |
| `backfill_refused` | stream | the backfill met a store it cannot read as this estate's | operator |
| `unknown_verb` | refused | `verb` names no `sendevent` verb | fix the request |
| `unknown_status` | refused | `CHANGE_STATUS` names no status | fix the request |
| `status_not_injectable` | refused | `CHANGE_STATUS` names a status an operator may not send (`QUE_WAIT`) | fix the request |
| `baseline_mismatch` | refused | `baseline_id` is not this period's (DL-117) | re-read then decide |
| `expect_required` | refused | a mutation that addresses a row carries no `expect` | fix the request |
| `request_id_reused` | refused | the `request_id` was admitted for a different command | fix the request |
| `engine_shutting_down` | refused | the engine shut down before the input was admitted | wait |
| `decision_timeout` | unknown | no decision arrived within the window | re-read; retry a mutation only under the same request_id |
| `apply_faulted` | refused | applying the command on a fork of the engine's state raised, so it was refused before it was logged (`docs/concurrency-model.md` §4, DL-292) | operator |
| `precondition_failed` | rejected | the addressed entity is not at the revision `expect` names | re-read then decide |
| `clock_regressed` | engine invariant failure | a stamp below the admission frontier reached `Frontiers.admit`; real-domain admission floors stamps, so valid control requests cannot emit it; direct guard calls and backdated in-process virtual inputs can | engine bug |
| `stale_completion` | rejected, stored only | the stale-completion gate rejected an engine-made completion; it is stored, and no client request is answered with it | none |
| `engine_apply_faulted` | rejected | applying an input on a fork of the engine's state raised, and its time half applied alone, so the input was logged with this rejection and only its time half applied: it is quarantined (`docs/concurrency-model.md` §4). The live engine stores it for an engine-made input. Replay stores it for any input admitted with no decision whose apply raises the same way, a control input included. Like every stored code, an exact retry of that control input is answered with it | operator |
| `engine_error` | refused | an engine refusal whose raise site names no code | operator |
| `engine_draining` | rejected | the mutation was admitted after `stop_requested` (a SIGTERM or `engine_stop`, below): it is logged with this rejection and only its time half applied; an exact retry is answered with it (spec-drain) | wait |
| `stale_plan` | refused | the plan was diffed against another configuration rev or catalog than the engine runs (its revision or its catalog hash), `expect` names another rev, or the plan predates the engine (no estate directory) | re-read then decide |
| `box_running` | refused | the plan adds or removes a member of a box that is `RUNNING` now, or removes it | wait |
| `would_stop_running` | refused | the plan changes or removes a job that is `RUNNING` or `STARTING`, and the request did not consent with `stop_running` | fix the request |
| `update_in_progress` | refused | another update is stopping its jobs; `names` holds its token | wait |

The last four rows are an addition to this table (`update`, spec-reload,
DL-427). They are `update`'s refusals: each writes nothing, and the
answer's `names` lists the jobs or boxes it concerns. An `update` while the
engine drains is refused with `engine_draining`, and nothing is logged.

## 3. Mutating verb: sendevent

```json
{"cmd": "sendevent", "v": 3, "baseline_id": "…",
 "request_id": "…", "verb": "CHANGE_STATUS",
 "payload": {"job": "nightly", "status": "SUCCESS"},
 "expect": {"job:nightly": 12}, "claimed_actor": "alice@host"}
```

Each `verb` names exactly one oracle `EventKind`, but the set is not the
whole alphabet. `CHANGE_STATUS` is the wire name of `STATUS`. `TIMER`,
`MUST_START_ALARM` and `MUST_COMPLETE_ALARM` are the engine's own and have
no wire door. Every admitted command is recorded in the store with `source=control`
(DL-428).
The store (`engine.db`, DL-428) is the audit trail of engine decisions,
and no second log carries them. A run started without a store has no audit
trail. It is the same run `subscribe` refuses in §5, and not a
configuration an operator meets.
Access decisions at the DL-146 perimeter go to the perimeter journal
(DL-148, `docs/access-model.md` §6) and never enter the store. Every tier
denial (`access_denied`) attempts a receipt there, and a failed receipt
write still denies. Every admission of a request that required the ops
tier or higher writes a receipt there, best effort (DL-146).

| verb | `payload` | notes |
|---|---|---|
| job verbs | `job` | `STARTJOB`, `FORCE_STARTJOB`, `KILLJOB`, `ON_ICE`, `OFF_ICE`, `ON_HOLD`, `OFF_HOLD`, `ON_NOEXEC`, `OFF_NOEXEC`, `DISARM` *(DL-158)*, `RELEASE_RESOURCE` *(DL-256)* |
| `SET_GLOBAL` | `name` (a non-empty string), `value` (a string) | the empty string is a legal value: `v(G) = ""` is a legal condition, so an operator must be able to satisfy it |
| `CHANGE_STATUS` | `job`, `status`, optional int `exit_code` | injected as `STATUS`, keeping overwrite parity |

Every string a payload carries must be a **Unicode scalar string**. An
unpaired surrogate is refused at the door, because one admitted would
leave the estate unsealable (PR-10a, period-model §3.2). The rule covers
the strings a verb reads. A field the verb does not read is never copied,
never recorded in the store, and cannot reach a seal (DL-228).

`DISARM` (DL-158) is the explicit disarm that period-model §10.4 names,
recorded in the store. It clears the job's SEM-32 armed latch and does nothing else:
no status move, no start, no wake, no timer touched. A target with no
latch is an accepted no-op recorded in the store, the `OFF_HOLD` shape.
The verb is legal at any time, not only before a seal. It drops only the latch
visible at application time: `applied` does not inhibit a later arm, and
it cancels no start already out of the latch. The trace marker is
`DISARM`. `SCHED_DISARM` stays the engine's own marker for
scheduler-caused drops (the Q3c box fold). The durable audit distinction
is the trace reason, not the decision's revisions map. The oracle records
`sendevent DISARM` for a real drop and `sendevent DISARM (no latch)` for a
no-op. The revisions map cannot tell them apart, because it carries every
revision the batch moved, the target's own timers included. A timer of
the target's due at the DISARM's instant fires in the same batch and moves
the target's revision whether or not the latch was set (DL-233).

`RELEASE_RESOURCE` (DL-256) is the vendor's `sendevent -E RELEASE_RESOURCE
-J job`. It is a job verb and takes the job verbs' whole contract: the
payload, the mandatory `expect` on `job:<name>`, recording in the store
at admission, and replay. It adds no frame, field or record kind. It adds one kind to
the event alphabet, under protocol-evolution §1's rule: the alphabet
grows only as an entry-in-service, reader and writer in one release, in
protocol-evolution §2's order. The verb gives back every resource unit
that a job that is not live (not `STARTING` or `RUNNING`) still holds,
and wakes the queue (DL-256). No status moves. The units are on the
job's row, so a release moves the job's revision. A job holding
nothing, and a live job, whose units belong to its run, are accepted
no-ops recorded in the store. The trace marker
is `RELEASE_RESOURCE`, and its reason names the outcome:
`sendevent RELEASE_RESOURCE (frees …)`, `(nothing held)`, or
`(no effect: <status>; …)` naming the live status, such as
`(no effect: RUNNING; …)`.

**`expect` is mandatory** (`docs/concurrency-model.md` §0). It names the
addressed entity, `job:<name>` for a job verb and `global:<name>` for
`SET_GLOBAL`, and nothing else, at the revision the caller read from a §4
query. `0` means the entity has no row yet; that is how a conditional
create is expressed. A command with no `expect`, a malformed one, or one
naming any other key is **refused**: nothing is admitted, no index is
consumed, and the log says nothing about it.

`request_id` is required. Without one a timed-out command cannot be
retried safely, because nothing could recognise the retry as one. An id
that begins with `engine:` is refused as `invalid_argument` (DL-297): the
engine names the inputs it makes itself `engine:<index>`, so the prefix
is reserved.

An exact retry (same id, same fingerprint) is answered from its original
decision and takes no second index. With an access map configured
(DL-147), the retry route runs **after** the perimeter admits the
request. Admission decides under the current policy, so a caller who has
lost the tier since the original is denied at the boundary before the
retry route is reached (`docs/access-model.md` §5, §7). A reused id under
a *different* command is refused as a collision. `expect` is part of the
fingerprint, so the same verb at two revisions is two commands.

**A `sendevent` collision refusal carries the id's earlier decision**
(DL-217). When the id already holds one, the refusal adds
`"original_decision": {index, request_id, decision, reason, code, revisions}`,
the decision's own fields. `code` is the stored one. It is null on an
application and on a decision that stores no code (§2, DL-272). The
answer stays a refusal of this request: `ok: false`, `refused: true`, and
the read header of the engine that answered. The nested decision belongs to
the earlier command and is never this request's outcome. An id admitted but
not yet decided carries no `original_decision`.

**Recovering a lost answer.** The fingerprint covers `baseline_id` and
`expect`. A retry must place the values its original placed, not values
re-read after the loss. `dsl41 sendevent` prints the id and those two
values on stderr before the first write, as the flags `--request-id`,
`--expect` and `--baseline`, and repeats them in the exit-4 advice. Re-running the original arguments with
them gives these answers:

- the original was decided: the retry is answered from that decision and
  takes no index. This holds in the same period, after the entity moved,
  and after a same-period restart that rebuilt the index from the store;
- the original was never admitted: the retry is a fresh command at the
  pinned `expect`. It applies if the entity is still at that revision and
  is rejected if it moved;
- the actor claim differs, because the retry ran as another user or host,
  or an access map stamps a different principal than it stamped on the
  original: the envelope differs, and the answer is a collision carrying
  `original_decision`, not a replay.

This recovery holds within one protocol version, and within the period
whose decision index holds the id.

`baseline_id` must match the engine's. A revision read from another
baseline names nothing here.

One engine holds a run root (`leader.lock`, DL-99), so a request names
no leader term.

`claimed_actor` is optional and is exactly what its name says. There is
no authentication at this tier (§7), so it is recorded as the caller's
claim about itself and is never treated as a principal. With an access
map configured (DL-147), the server authenticates the peer by kernel
credential. It overwrites this field with the canonical spelling
`os/<name>` before anything is fingerprinted or logged
(`docs/access-model.md` §3, DL-146). Without an access map the field
stays a claim.

**The response is the decision, not the receipt:**

```json
{"ok": true, "kind": "ON_HOLD", "decision": "applied", "index": 12,
 "request_id": "…", "revisions": {"job:nightly": 13}, …header…}
```

`ok` is true only for `decision: "applied"`. The other three outcomes are
three different facts, not one failure. A client must be able to tell
them apart from the answer alone:

| outcome | on the wire | what happened | the caller's next move |
|---|---|---|---|
| applied | `ok: true` | the oracle applied it | — |
| **refused** | `ok: false`, `refused: true` | nothing admitted, no index consumed, **nothing in the store** (a perimeter denial attempts its own synced receipt first, and denies whether or not it lands, access-model §6) | this request never happened: fix and re-send. A refused retry says nothing about its original, which may have applied (DL-217) |
| **rejected** | `ok: false`, `decision: "rejected"`, an `index` | a decision went against it — over this verb, always the precondition losing its race. Recorded in the store, and its batch's time observation applied | re-**read** and re-decide; the same envelope loses the same race, because `expect` is in it |
| **unknown** | `ok: false`, and neither marker | admission is uncertain: no decision arrived within the window below, or a handler raised | re-read. Retry **only** under the same `request_id` |

So `refused: true` carries meaning by its absence. It appears on every
`ok: false` that says nothing was admitted, including the shared doors: a
malformed frame and a wrong `v`. An `ok: false` that carries neither
marker therefore means uncertainty, not a fourth kind of no. Two answers
have that shape: the no-decision timeout below, and the internal-error
answer of a handler that raised (§2). A client reads both as `unknown`.
`dsl41 sendevent` spends a distinct exit code on each outcome (0/2/3/4).
It prints its `request_id` and pins on stderr before every write
(DL-217), and repeats them as retry advice when the answer is `unknown`.
They are the only thing that makes that retry safe.

The server waits for the decision rather than acknowledging admission,
because a precondition whose outcome the caller cannot see is not a
precondition. If none arrives within 5 s, it answers that it does not
know and says to re-read before retrying. The command may be durably
admitted and about to apply.

Job arguments are validated against the catalog, because vendor
`sendevent` errors on an unknown job rather than queueing it. That check
precedes the `expect` check, since the payload is what says which entity
is addressed. `CHANGE_STATUS` carries one deliberate exception (SEM-07):
`"JOB^INST"`, where `INST` is a declared `insert_xinst`, is a legal
target even though no such job is in the catalog. Overwriting the store's
pseudo-entity is how an operator satisfies a cross-instance atom the
sandbox cannot see. Other job verbs stay catalog-only: starting or
killing a ghost is meaningless.

Composing that command's `expect` has one corner. Before the row exists,
`status JOB^INST` **refuses**, because the name is in neither the catalog
nor the store. A refused read is revision `0`, which is exactly what the
conditional create names, so the bundled composer reads it that way. Once
`CHANGE_STATUS` has created the row, `status` answers it like any job.

`status` must be one of the injectable statuses: `INACTIVE`, `STARTING`,
`RUNNING`, `SUCCESS`, `FAILURE` or `TERMINATED` (DL-264). `QUE_WAIT` is
refused. It is an internal state of the capacity owner (DL-50), and the
oracle gives an injected `QUE_WAIT` no meaning. The refusal comes before
the store append, so it consumes no log index, and it lists the
injectable set. Whether the vendor's `sendevent -s QUE_WAIT` is legal is
not known; this is a stated support limit, not a claim about the vendor.

## 4. Query verbs (frozen response shapes)

**A read is refused when this engine cannot prove it leads the estate's
LINEAGE** (DL-133; period-model §1.3). Every answer below carries the §2
read header (headerless exceptions are listed there, DL-148): a baseline
and a log position. Those are exactly the coordinates a
displaced leader cannot speak for. So a `status` immediately after the
anchor directory is deleted or replaced is refused rather than answered,
and the refusal carries no header (PR-03).

Two proofs have two rules. Losing the RUN ROOT's proof is
`concurrency-model.md` §7's rule, and it stops dispatch on the way into
admission's first append rather than at this door. Refusing at this door
would leave a displaced leader answering nothing and stopping never. It
would also refuse the read a client composes its `expect` from, so the
very mutation that stops the engine could never be sent (CM-14).

A `subscribe` response is a read and is refused on the same rule
(DL-148). It shares the lineage proof, not the header: no `subscribe`
line (the ack, a refusal, a marker or a record) carries the read header
(§2). A subscription is not a `concurrency-model.md` §6 revision-bearing
read.

Every query is a **pure projection** of (oracle, catalog, scheduler,
spool paths). None changes protocol-visible engine state, and
none inserts a store row. `status` does refresh one private cache, the
`spec_drift` re-hash below. Queries are safe to serve from the engine's
task because `feed()` never yields, so a handler can never observe a
half-applied event.

Every §4 query that is answered carries the read header in addition to
the fields listed below. The lineage refusal and the internal-error
answer are headerless (§2, DL-148). These are the reads an
`expect` is composed from: per-job `state_rev` in `status`,
and `global`/`globals` for a named global. Revision-bearing reads come
from the one engine that holds the run root's `leader.lock` (DL-90,
DL-99).

### `status [job]`

`{"ok": true, "jobs": {<name>: {…}}, "spec_drift": bool|null}`. Omitting
`job` returns every catalog job unioned with every store row, so
`CHANGE_STATUS`-created ghosts appear.

Per job: `status`, `status_at`, `run_number`, `exit_code`, `on_ice`,
`on_hold`, `on_noexec`, `armed` (the SEM-32 latch: an armed job looks
INACTIVE, but the next condition edge starts it, DL-54), `started_by` (the
trace cause of the most recent start; null = never started, DL-68),
`state_rev` (the revision an `expect` names for the job, DL-87), `held`
(below), `pending_timers` (a list of `{due, kind}`), `log_out` / `log_err` (the runner-design §6
append targets of the CURRENT run, resolved by the same `job_log_paths`
the wrapper spec uses, so the tail and the writer can never diverge; CMD
only), `job_type` and `box_name` (catalog placement, so a tree renders
without a second query; null for ghosts).

`held` (DL-94) keeps its place in the row and is always false: one engine
dispatches every start it decides, so no start is held.

`watching` (`{file, interval, min_size}`) is present **only** for a live
FW run. The absence of the key is itself the "not watching" signal
(DL-68).

`attempt` (`{run_id, n, state, after, cause, note}`) is present **only**
while the job's current run has an attempt that has not settled, or
while an attempt of the job keeps its starts back (DL-426). `run_id` is
the attempt's identity, `n` its ordinal within its run, `state` one of
`SPAWNING`, `RUNNING`, `STOPPING`, or `ENDED` for one whose outcome is
in, `after` the cause of the stop the attempt replaced (null for a run's
first attempt), `cause` the cause of the stop it is taking, and `note`
why a stop could not be carried out (`ladder failed: …`, `command
survived its KILL`). A job's next dispatch goes only when no attempt of
the job is unsettled. An attempt settles once its plan is done, which
comes after its record and its stop ladder; a held attempt, whose group
may still live with no stop left to end it, settles only once the group
is gone. A start decided meanwhile waits as `dispatch_pending` and is
launched once the attempt settles. The attempt of an earlier run that
keeps it back, or a held one, carries `held: true`, and its `note` says
why: `stop ladder running`, or `stop in progress` when that is all. Clients show it beside the status: "RUNNING (attempt 2, after
update_stop)", "STOPPING (ladder failed: …)", "STARTING (held: stop
ladder running)".

An addition to this section (the process cookie, wrapper protocol
`DSL41_COOKIE`, DL-431): the attempt of the job's current run that is `ENDED`
while its stop ladder still runs carries `held: true` too, since it
keeps the job's starts back. Its `note` is one the engine writes:
`leaked: n, stopping` while the ladder stops the n processes the
command left when it exited, `scan failed: <reason>` while a reading of
`/proc` fails and nothing is released, and `command survived its KILL`
for a process still there `grace_kill_s` after the ladder's KILL. The
keys and their types are unchanged. Example: "SUCCESS (held: leaked: 1,
stopping)".

`interrupted` is present **only** while a job is held for an interrupted
attempt, until its next start (spec-crash): the attempt's display name and
run id, the outcome's kind and cause, its start and end, the signal or exit
code the wrapper saw, the attempt limit when that is why it is held, and
the run number.

`spec_drift` is a lazy re-hash of the input files the run loaded, at most
every 15 s. `null` means the server holds no fingerprint. There is
deliberately no reload. The flag tells an operator that the running
catalog differs from disk, and adopting the disk version takes a cold
restart (DL-65).

An addition to this section (`update`, spec-reload, DL-427): the
`status` answer carries `config_rev`, the configuration rev the engine
runs, `update_window_s`, how long an `update` with `wait` waits for its
application (§2), and, only while an update stops its jobs, `update`:
`{"token", "stopping": [jobs]}`, the jobs whose attempts it still waits
for. An attempt an update stops shows its stop cause `update_stop`, and
its replacement `"after": "update_stop"`. Once an update applied, `spec`
serves the applied plan's JIL, and `spec_drift` compares the files on disk
with the applied plan's bytes, so it keeps its meaning: the running
catalog differs from disk. Adopting the files on disk while the engine
runs is `dsl41 reread` and `dsl41 update`.

An addition to this section (the restart, DL-433): the `status` answer
carries `engine`, the engine lifecycle's state: `STARTING` while a
restart catches up, then `RUNNING`, `DRAINING`. The control socket is
bound before the catch-up, so every verb is served from the restart's
first loop step, and `query` and `status` show the folded state as it
is, attempts still open included. An `interrupted` block carries
`note` when the restart quarantined the attempt: the reason its records
could not be trusted and the pids the restart's first reading found. The other keys and their types are
unchanged.

### `catalog`

An addition to this section (`update`, spec-reload, DL-427), the read
`dsl41 reread` diffs a candidate against, at the `read` tier:
`{"ok": true, "catalog": {…}, "catalog_hash", "settings": {…},
"revision": int, "status": {<job>: <status>}, "runs": {<job>: int},
"tombstones": {<job>: [flags]}}`. `catalog` is the running catalog's
canonical JSON value, `catalog_hash` its hash, `settings` the estate
settings it runs under (`timezone`, `timezone_aliases`, `semantics`,
`external_instances`), `revision` the configuration rev, `status` and
`runs` each row's status and run number, and `tombstones` the flags each
removed job's tombstone kept (`ON_HOLD`, `ON_ICE`, `ON_NOEXEC`), for the
removed names that kept any.

### `trace [since]`

`{"ok": true, "last_seq": int, "entries": [{seq, at, job, transition,
cause}]}`. `seq` is a 1-based position in the oracle's trace; `since` is
exclusive.

An addition to this section (`update`, spec-reload, DL-427): the `trace`
answer may hold lines that name no job (`"job": ""`) and move no state.
`RESTART` opens the trace of an engine restarted from a snapshot (the trace
covers what follows it). `CONFIG` marks where an update's application stands
between two inputs; its `cause` is `configuration rev N`, the rev the
entries after it run under.

### `explain <job>`

`{"ok": true, "job", "condition": <source>|null, "satisfied": bool,
"atoms": [{atom, true, actual?}]}`. `actual` appears for global atoms and
carries the effective value (null = never set), because atom truth alone
hides why (DL-66). A job with no condition answers `condition: null`,
`satisfied: true`, `atoms: []`.

Truth comes from the oracle itself (ice bypass, lookback, instances),
never from a second evaluator.

### `spec <job>`

`{"ok": true, "job", "job_type", "box_name", "jil": <text>|null}`: the
preserve-rendered, post-placeholder JIL block **this run loaded**, not
the template on disk (DL-64). `jil: null` when the server was started
without source texts (embedders).

### `deps <job>`

`{"ok": true, "job", "upstream": [...], "globals": [...], "downstream":
[...], "box_name", "members": [...]}` (DL-65, the blast-radius view).
`upstream`/`globals` are split by **atom type**, never by sniffing key
strings, because a job legally named `g:x` (DL-39 escapes) would
otherwise collide with global `x`. `downstream` comes from the oracle's
edge-trigger index, which is keyed by those strings. It therefore does
inherit the collision for such a name; this is a limit, not a guard.
Condition edges are not the whole blast radius of a box, so containment
is served alongside: `box_name` upward, `members` downward.

### `timers`

`{"ok": true, "timers": [{due, job, kind, detail?}]}`: every pending
oracle timer plus each scheduled job's next calendar tick, due-ordered
(DL-65). `due` is **nullable** (DL-68). Live filewatches join as trailing
rows with `due: null` and a `detail` string, because they fire on a file,
not a clock.

### `plan`

`{"ok": true, "waves": [[...]]}`: wave-by-wave topological batches over
the AND-success skeleton. It refuses with `ok: false`, naming the cycle,
when the skeleton is cyclic. Cycles are legal AutoSys (DL-13), so this is
a query refusal, not a run refusal.

### `global <name>` / `globals <names>`

`{"ok": true, "globals": {<name>: {"present": bool, "value": <string>|null,
"state_rev": int}}}`: the read a `SET_GLOBAL` precondition is composed
from (`docs/concurrency-model.md` §6). Named entities only. An unset name
is **answered**, at revision `0`, rather than omitted: absence you cannot
name is absence you cannot lock against, and `0` is exactly what a
conditional create conditions on. Neither verb inserts a row.

A map of every global would not do instead. It can report what exists,
never that a particular name does not.

## 5. Streaming verb: subscribe

`{"cmd": "subscribe", "v": 3, "since": <int>?}`. A subscription **owns
its connection** until hangup, so a client opens a separate connection
for it.

The server answers `{"ok": true, "subscribed": true, "since": <int>}`,
then streams the store's wire records (`docs/runner-design.md` §7 record kinds) as
raw lines. The ack's `since` is the stream's cursor (DL-267): the
request's `since` when it named one, otherwise the admission frontier
sampled for the seam below. Every seq'd record the stream owes is above
it. It is an additive answer field, which a v3 client ignores
(`docs/protocol-evolution.md` §1, DL-217). A run with no store is
refused with `{"ok": false, "code": "no_journal", "error": "this run has
no journal"}`.

Delivery guarantees:

- seq'd records (`input`, `advance`) are **exactly once** across
  the backfill/live seam. The seam is sampled *before* the ack is
  written. Otherwise a record appended during the send would be skipped
  as "covered" though it was never backfilled.
- every other record kind carries no `seq` and is **at-least-once**
  inside the backfill race window. These are `dispatch`, `drop` and
  `decision` on the live stream, plus `segment` and `preflight`, which a
  backfill can carry but no live subscription meets. `decision` carries its attempt's number under
  `index` rather than `seq` for this reason: two records sharing one
  cursor value would leave the second undeliverable to a resuming
  subscriber (DL-89).
- `since` cuts positionally: everything after the last record whose seq
  is at or below it.

The stream carries `decision` records (DL-118). One line carries the
decision and its revisions; there is no separate `result` record. The
seam keys on the presence of `seq`, not on a record name, so `decision`
has the at-least-once guarantee.

**The backfill reads the store** (DL-135, DL-424). An estate's records
live in its run root's store, `engine.db`, in seq order. The backfill
reads every retained record after the cursor and delivers them oldest
first. `since` is an index, the cut is positional, and the seam keys on
the presence of `seq`, so the exactly-once and at-least-once guarantees
above hold over the whole retained store.

**The `segment` record in 2.0.0** is the configuration the engine
applied (DL-424): `{"rec": "segment", "rev", "at", "catalog_hash",
"runtime_hash", "state_machine_version", "baseline_id",
"clock_domain"}`. `rev` is the configuration rev and is new in 2.0.0.
The 1.x period fields are retired, and a 2.0.0 engine sends none of
them: `segment_no`, `estate_id`, `period_id`, `catalog_hash_version`,
`source_bundle_hash`, `first_index`, `opens_from_seal`, `reclaimed` and
`trust_unaudited`.

An addition to this section (`update`, spec-reload, DL-427): an
update's application is a new configuration rev, and the stream carries
its `segment`, in the same shape, where the engine applied it: the
records before it ran under the rev before. The update's other records
(its intent while it stops) are not on the wire. Each `dispatch` carries
`rev`, the configuration rev that attempt was dispatched under: the
attempt's own, so a run's replacement after an update names the next rev.
A run's rev is the rev in force where the run opened. Off the wire, beside
the run history DL-428's snapshot keeps, each retained run carries it as
`rev`, required: a snapshot whose retained run lacks it does not read, and
the state version is 26. `dsl41 runs` reads every run under that rev and
never under a dispatch's.

**The fields 2.0.0 adds** (DL-426), each additive, so a v3 client that
does not read them keeps working (§2):

- `dispatch` carries `after` on a replacement attempt: the cause of the
  stop the attempt it replaces took (`engine_stop`, `lifeline`,
  `unobservable`, …). A run's first attempt has none.
- An addition (the restart, DL-433): an `unobservable` outcome carries
  cause `engine_death` when the engine that ran the attempt died and no
  record came, and the attempt took no stop first; a replacement of it
  carries `"after": "engine_death"`. It carries cause `quarantined`, with
  the reason and the pids found in its record's `note`, when a restart
  could not trust the attempt's records: a spool record naming another
  run id, or a spool or record that cannot be read. Its plan is the
  interruption rule's with `hold` forced wherever the rule would rerun or
  hold, whatever the job's marking; where the oracle already ended the run
  or the job left the catalog, the plan is `none` and the evidence stays
  in the outcome record. The outcome closes the attempt as any outcome
  does. A failed reading of what is left is never a quarantine: the
  record decides the outcome, and what is left is held as live. A restart
  neither awaits nor
  watches a previous engine's wrapper: a record that lands after the
  attempt's observation is read by nobody.
- An engine-made `input` that holds an interrupted attempt is a `STATUS`
  whose payload is `{"job", "run_number", "status": "FAILURE", "cause":
  "interrupted", "hold": true}`: one oracle transition sets the job's
  hold with its FAILURE (DL-424). No wire door accepts `hold`.
- A snapshot carries `evidence`: per held job, the interrupted attempt
  the `status` row shows as `interrupted` until the job's next start.
- The `status` rows' `attempt` and `interrupted` blocks are §4's.
- The store's `draining`, `clean_shutdown` and `unclean_shutdown` engine
  records carry `unsettled`, the run ids of the attempts the engine left
  unsettled, and `stops`, a map from each of those run ids to the cause of
  the stop it was taking: `engine_stop`, `update_stop` or `oracle_kill`.
  In `draining` the map names only the attempts already taking a stop;
  the rest take the drain's `engine_stop`. In a shutdown record an
  attempt taking no stop maps to null. Both are absent when nothing was
  unsettled. The first stop an attempt took is its cause, and a shutdown
  record's map is the engine's last word; the restart adopts the attempt
  with it (DL-426).
- The store's `outcome` record carries `"ladder": "running"` when the
  attempt's outcome is in but the cleanup of its group is not done: its
  stop ladder still runs, or its group is held. The attempt stays open
  until a `follow_up` record `{"event": "ladder_done", "attempt"}` says
  the group is gone. A reader tells it by `event` from the other two
  `follow_up` shapes: a plan (`"event": "plan"`) and a plan step's
  completion (`"event": "done"`, with `step` and `seq`). A restart that
  finds such an attempt open finishes its ladder before any replacement
  of it is launched. Neither record is on the stream (DL-426).
- An addition (the process cookie, DL-431): a snapshot's open attempt that is
  closing carries an optional `outcome`, the kind of its committed
  outcome (`natural`, `stopped`, …), in the snapshot DL-428 defines. A
  restart leaves a `natural` one's processes running under
  `process-leak=leak` and finishes every other one's stop. A snapshot
  written before the key has none, and its closing attempt is finished
  as a stop.

**A cursor below the earliest retained record gets a gap marker.** The
server sends one line, `{"gap": true, "earliest_retained": <index>}`,
before the backfill, and then backfills everything it has.
`earliest_retained` is the first index the store still holds: the first
retained input's, or, when retention has pruned every input, the index
after the last one the latest snapshot covers. Retention is the
reachable case: the store prunes its oldest records behind a snapshot
once they leave the retention window. So a subscriber resuming below
what is left cannot be given what it asked for, and is told so. The rule
is exact: a marker goes out when the store's oldest records were pruned
**and** `max(since, 0) < earliest_retained - 1`. A cursor at
`earliest_retained - 1` is contiguous with what the store holds and gets
none. One below it is missing a record and gets one. A negative cursor
reads as 0. So on a store that still retains from index 1 no cursor is
ever a gap, but on a pruned store `since: 0` is one. A client that does
not read the marker sees a record it does not recognise and skips it, as
it does with any record kind it has no case for. The marker is a
response like any other, so the leader re-proves the lineage in front of
it (PR-03).

**The backfill can refuse on the stream** (DL-135). It reads records
this subscription did not see appended, so it can meet one it cannot
read: a corrupt row, or a record kind this binary does not know. Any
such failure is
`{"ok": false, "code": "backfill_refused", "error": …}` sent *after* the
ack, and then a hangup. It is never a hangup with no answer, and never a
stream that silently skips the records it could not read. Retention
bounds the read.

**A subscriber that stops reading is removed** (DL-267). Each
subscription has a backlog: the live records appended for it and not yet
written to its socket, counted in the bytes of their stream lines. The
budget is `4 × LINE_LIMIT`, 64 MiB (`SUBSCRIBER_BACKLOG_BYTES`). It is
three request lines for an input record, because the input carries the
request's payload and the stream escapes every non-ASCII character, plus
one request line for the largest decision a bundled client can read.
Both bundled readers, `ControlClient.subscribe` and `subscribe_lines`
(which the CLI uses), read stream lines up to the same bound. So every
record that fits the budget is one they can read.

A record enters an empty backlog. Otherwise it enters only if the backlog
plus the record stays within the budget. So a reading subscriber whose
backlog is empty is not removed by one admitted command whose records fit
the budget, and one record larger than the budget delays a subscriber and
does not remove it. A larger burst removes even a subscriber that was
keeping up: the engine applies queued commands without yielding
(`docs/concurrency-model.md` §4), so its handler cannot drain between
them. That client resumes from its cursor (§6) and loses nothing. A
refused record removes the subscription:

- the store writer drops the backlog and removes the subscription, after
  every other subscription has the record;
- the server then cancels the handler and aborts the transport at once,
  and reports the reason on its stderr, best effort;
- the store append has already succeeded. Nothing from the removal, an
  owner that raises or a stderr that cannot be written included, reaches
  the append, the engine loop or any other subscription. A statechart
  error is the one exception: it stops the engine with exit 7;
- the stream ends with EOF, perhaps after a torn last line. It never
  continues past a missing record. It carries no terminal line: EOF and
  the §6 cursor are the signal, and the peer that stopped reading could
  not receive a line anyway.

A subscription's queued backlog is at most the budget or one record,
whichever is larger, plus one record in the transport beyond its
high-water mark. The budget counts encoded bytes. The queued records are
shared Python objects and take more memory than that.

A stream line past `SUBSCRIBER_BACKLOG_BYTES` is unreadable to the
bundled clients, and resubscribing at the same cursor meets it again.
They raise a distinct error, `StreamLineTooLong`, and the CLI names no
`--since` for it.

**The backfill phase is outside the budget.** A subscription with `since`
holds the backfill records it has read and not yet sent: the retained
records after the cursor. Retention bounds it, not the budget, and each
subscription reads its own copy. Each record is
released once it is sent, and the phase holds nothing once the live
stream starts. Live records queue against the budget meanwhile, so a
client that stalls inside its backfill is removed there like any other.

**How the server ends a connection** (DL-267). This covers every
connection on the control socket, request/response and `subscribe`
alike. An overflow or an access revocation (`docs/access-model.md` §7)
aborts the connection at once and discards its unsent bytes. Every other
end the server makes closes the connection and gives the peer
`HANGUP_GRACE_S`, 2 s, to take the unsent bytes; then it aborts. These
ends are a shutdown, the end of a request/response connection after the
client's EOF, and a stream that ends on a refusal. So a reading client
still receives an answer already queued when the engine shuts down, and
a peer that stopped reading delays a shutdown by at most the grace. A
close alone would wait until the peer read, for as long as it stayed
stalled.

There is no second durable feed. A removed client reconnects with a
cursor (§6), and the backfill, the seam and the gap marker above give it
what it missed.

## 6. Client obligations

- Any transport error must drop the connection. Reusing a connection
  after a **cancelled** exchange hands the unread response line to the
  next request and offsets every reply after it (DL-46). `ControlClient`
  drops on `OSError` and on any `BaseException`, `CancelledError`
  included.
- A failure after a write was attempted is `delivered` (§2, DL-216). Both
  clients serialize the request before they touch the connection, so an
  unencodable request is the caller's error and never a transport
  outcome.
- Open every connection with an explicit `LINE_LIMIT`; the default
  readline buffer fails on real estates. A `subscribe` connection reads
  its record lines up to the §5 budget, `4 × LINE_LIMIT`, because an
  escaped input record can be three times a request line (DL-267).
- A torn line in a `subscribe` stream is skippable: records are a wake-up
  signal, and the store on disk is the truth. `ControlClient` skips a line
  it cannot parse. `dsl41 query subscribe` instead treats a torn line as
  the end of the stream and exits 2, as below (DL-267).
- A `subscribe` stream can end at the engine: EOF, a torn last line, or
  an `{"ok": false}` line (§5). `subscribe_lines` raises
  `ControlClientError` with that line's `error`, or a fallback text when
  the line has none. `ControlClient.subscribe`
  yields the line to its caller as it yields a record, and then ends at
  the hangup. A client that must not miss records
  reconnects with a cursor (DL-267): the last `seq` it read or, when it
  read none, the ack's `since`. `seq`'d records then arrive exactly once
  across the two connections. Records without a `seq` after that cursor
  may arrive twice. `dsl41 query subscribe` exits 2 when the engine ends
  its stream, and names the exact `--since` on stderr: the last `seq` it
  printed, or the ack's `since`. Its records are on stdout, so a
  monitoring wrapper restarts it with that `--since`. The TUI reconnects
  after one second and repolls; it needs no cursor.

## 7. Known gaps (recorded, not fixed — DL-78)

Items 2 and 3 are limits of the frozen protocol. Items 1 and 4 are not
gaps: each names the rule that covers it, and keeps its number for
citations. Adding a second host or a non-local controller has to address
each open item (`docs/decision-log.md` DL-78).

1. **Version handshake.** Every request names its version (§2; DL-90,
   DL-118).
2. **No authentication or authorization — on an estate with no access
   map.** There the socket's `0600` mode plus filesystem ownership is the
   entire access-control model. Any process running as the invoking user
   has full `sendevent` authority. That is why the envelope's actor field
   is named `claimed_actor`: the log records an assertion.
   `docs/concurrency-model.md` §6's "the leader stamps the authenticated
   principal" applies only where a leader can authenticate one.
   **An estate with an access map is outside this gap (DL-146).**
   `docs/access-model.md` gives it authorization and *local*
   authentication: kernel peer credentials, one principal→tier map, a
   closed verb table (access-model §10) gated in `_handle`, and denials answered in the
   `refused` vocabulary, with attempted receipts in a perimeter journal.
   The envelope does not change. The web session's per-user identity
   stays open under the named seam `web-session-principal-v2`
   (access-model §9).
3. **Unix-domain only.** There is no network transport, so the
   single-engine guarantee rests on a local `flock` (§2, DL-100).
   A non-local controller would need both a transport and a replacement
   for that guarantee.
4. **Error codes.** Every `ok: false` answer carries a stable `code`
   beside the prose `error`, except a retry answered from a stored
   decision that holds no code (§2, DL-272).
