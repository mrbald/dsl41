# Control protocol — the engine's public contract

Status: frozen at **v3** (DL-118; v2 was DL-90, v1 DL-78; amended by
DL-133, DL-135, DL-146, DL-147, DL-148, DL-150, DL-151, DL-158, DL-189,
DL-216, DL-217, DL-256, DL-264, DL-267 and DL-272). This
document is normative for the runner's §10 control plane in the same way
`docs/supervisor-protocol.md` is normative for the §6a lifecycle tier. Each
change to a frozen item requires a decision-log entry, and each amendment
is cited where it applies.

**An older version is gone, not deprecated.** `docs/concurrency-model.md`
§0 refuses a caller that does not name a version, and accepting unversioned
requests "for compatibility" is exactly the opt-out it forbids. An engine at
this version answers a v1 or v2 client with a refusal naming the version it
speaks (DL-118).

The two protocols sit on opposite sides of the engine:

```
operators / TUI / CLI  ──[ this document ]──▶  engine  ──[ supervisor-protocol.md ]──▶  wrappers
```

The supervisor protocol is the **inner** contract — a deliberately dumb
tier that records process lifecycle facts. This one is the **outer**
contract: it speaks the vocabulary of *meaning* (conditions, boxes,
statuses, sendevent verbs), which runner-design §11's scope fence assigns
to dsl41 permanently.

Implementation: `src/dsl41/runner_control.py` owns both ends — the server,
the wire vocabulary, and the three clients.

## 1. Roles

- **server** (`ControlServer`): one per run root, owned by the engine
  process. Mutating verbs inject into the engine's single-writer loop;
  query verbs are pure projections — see §4 for what that claims.
- **async client** (`ControlClient`): one persistent request/response
  connection plus separate connections for `subscribe`. Drives the §11
  TUI (`dsl41 run --ui`, `dsl41 ui`, `dsl41 serve`).
- **sync client** (`roundtrip`): one-shot blocking request/response for
  callers outside an event loop. Drives `dsl41 sendevent`, `dsl41 host`
  and `dsl41 query`.
- **blocking subscription client** (`subscribe_lines`, DL-172): one
  connection held open for the life of a `subscribe`, yielding the ack
  and then journal records. Drives `dsl41 query subscribe`. It lets a
  transport `OSError` propagate and raises `ControlClientError` for a
  protocol failure.

The async and sync clients raise `ControlClientError` on any transport or
decode failure. None of the three maps errors to exit codes — that is the
CLI's job.

## 2. Transport and framing (frozen)

- A **unix domain socket** at `<run_root>/control.sock`, mode `0600`
  (set via umask at bind, re-chmod'd after, because some platforms ignore
  umask on bind). When an access map names a `socket_group`, the socket
  keeps its owner, takes that group, and is re-set to `0660`; the run root
  opens to `0710` traversal (`docs/access-model.md` §8, DL-146, DL-147).
  Estates without a configured map keep `0600` exactly.
- **JSON lines**, both directions. One request object per line; one
  response object per line. The stream buffer limit is `LINE_LIMIT`
  (16 MiB): one `status` response covers every job on a single line and
  overruns asyncio's 64 KiB default at roughly 300 jobs. A `subscribe`
  stream's record lines can be longer, up to the §5 budget
  (`4 × LINE_LIMIT`, 64 MiB), and a subscriber reads them at that limit.
  A line ends at its `\n` (DL-216). A request fragment that reaches EOF
  without one is dropped unanswered and never parsed: its client died
  mid-write, and a truncated command must not run.
- **A client that attempted a write cannot prove the request did not
  arrive** (DL-216). A write can fail after the peer took
  every byte, or part of them. So a client treats any failure from its
  first write attempt on as an unknown outcome (`delivered`), never as
  "not sent". Only a failure before any write, such as a refused
  connect, is undelivered.
- **Every request carries `"v": 3`**, including queries and `subscribe`.
  The check runs before the request is routed at all, so `subscribe` —
  which owns its connection and never reaches the response path — is not
  the one unversioned door left open. A refusal does not close the
  connection: the next line on it may be well-formed.
- The answer to a request is `{"ok": true, …}` or
  `{"ok": false, "code": "<code>", "error": "<message>"}` (DL-272).
  `code` is stable and names the reason; the code table below lists
  every one. `error` is human-readable prose and may change. The other
  lines a server writes are the §5 shapes — journal records and the gap
  marker — and carry no `ok`.
- Responses carry the **read header**: `baseline_id`, `epoch` and
  `applied_index` (`docs/concurrency-model.md` §6). A revision means
  nothing without the log it was read from, and a client that cannot
  name the log cannot be told it is holding a stale one. The header is
  stamped (DL-148)
  in one place: on the answer of a request that passed routing and the
  §4 lineage proof. Everything else is headerless — an answer sent
  before routing (the malformed line, the version refusal, the DL-146
  perimeter's credential refusal and denial), the lineage refusal
  itself (§4, PR-03), the internal-error answer of a handler that
  raised, and every line `subscribe` writes on its own connection
  (§5). None of them names a revision.
- A malformed line answers `{"ok": false, "code": "malformed_request",
  "error": "bad request: …"}` and the stream stays in sync. That holds for
  invalid JSON **within** `LINE_LIMIT`. A line over the limit is a framing
  failure the reader cannot answer past: the connection closes with no
  answer, and the caller reads it as a transport error. A handler that
  raises answers `{"ok": false, "code": "internal_error", "error":
  "internal error: …"}` rather than dying unreplied — a client must never
  see a bare timeout for a query bug.
- Consumers must ignore unknown fields (forward compatibility).

**Socket lifecycle.** On start the server probes an existing socket file
by connecting: a successful connect means a LIVE engine already serves
this run root and the second engine refuses; a refused connect means a
crashed run's leftover, which is unlinked and claimed. Two engines racing
past the probe are separated by the bind itself. The probe is not the
election. One engine per run root is enforced by the `leader.lock` flock the
engine takes before it opens anything (`docs/concurrency-model.md` §1,
DL-99); this probe is cleanup of a stale socket file, and a second refusal
door in front of it — see §7.

**The version handshake** is `"v": 3` on every request (DL-90, DL-118).

**No controller lease.** Deliberate (DL-41a). `sendevent` is multi-writer
by AutoSys nature, and the engine's single-writer loop serializes every
injection. The lease guards the supervisor tier, which spawns without
semantics.

**Error codes** (DL-272). Every `ok: false` answer carries a `code`:
headerless answers, query errors and the `ok: false` lines of a
`subscribe` stream included. A code names the reason, never the outcome.
`refused` and `decision` keep their meaning, and one code can appear
under more than one outcome: `unknown_job` is a query error under
`status` and a refusal under `sendevent`. An `unknown` outcome never
carries `refused`, whatever its code.

A rejected answer's `code` is the one its `decision` record stores
(`docs/period-model.md` §2.3). An exact retry is answered from that
record, so it answers the same code as the first answer. A `decision`
written before DL-272 stores no code, and an answer replayed from it
omits the field. A client must tolerate a missing `code`.

`code` is an additive field: consumers ignore what they do not know
(above), so a client written before it keeps working. A code is never
renamed in place; a new code, or a retired one, is a decision-log entry.

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
| `access_denied` | refused | the perimeter denies this principal the command or verb (`docs/access-model.md` §5) | operator |
| `internal_error` | unknown | a handler raised | re-read; retry a mutation only under the same request_id |
| `lineage_lost` | refused | this engine can no longer prove it leads the estate's lineage (§4) | operator |
| `unknown_cmd` | refused | `cmd` names no command | fix the request |
| `invalid_argument` | refused, query or stream | a field is absent or has the wrong shape or type; `error` names the field | fix the request |
| `plan_cycle` | query | the AND-success skeleton has a cycle, so `plan` is disabled | operator |
| `unknown_job` | query, or refused on a mutation | the job is not in the catalog (for `status`: in neither the catalog nor the store) | fix the request |
| `no_journal` | stream | the run has no journal to stream | operator |
| `backfill_refused` | stream | the backfill met a journal file it cannot read as this estate's | operator |
| `unknown_verb` | refused | `verb` names no `sendevent` verb or no `host` verb | fix the request |
| `unknown_status` | refused | `CHANGE_STATUS` names no status | fix the request |
| `status_not_injectable` | refused | `CHANGE_STATUS` names a status an operator may not send (`QUE_WAIT`) | fix the request |
| `baseline_mismatch` | refused | `baseline_id` is not this run's | re-read then decide |
| `expect_required` | refused | a mutation that addresses a row carries no `expect` | fix the request |
| `request_id_reused` | refused | the `request_id` was admitted for a different command | fix the request |
| `stale_epoch` | refused | `epoch` is not the current leader's | re-read then decide |
| `period_sealing` | refused | the period is sealing and admits no external request | wait |
| `engine_shutting_down` | refused | the engine shut down before the input was admitted | wait |
| `decision_timeout` | unknown | no decision arrived within the window | re-read; retry a mutation only under the same request_id |
| `precondition_failed` | rejected | the addressed entity is not at the revision `expect` names | re-read then decide |
| `unknown_host` | rejected | no host with this id is in the routing table | fix the request |
| `host_quarantined` | rejected | the host is quarantined, which the leader sets and clears | wait |
| `host_evicted` | rejected | the host was evicted; it returns by re-registering. The leader's own reachability observation against an evicted host stores this code too, and answers no client | operator |
| `host_already_evicted` | rejected | `evict` addresses a host that is already evicted | re-read then decide |
| `force_needs_actor` | rejected | a forced eviction names no actor | fix the request |
| `host_not_quarantined` | rejected | a gated eviction addresses a host that is not quarantined | operator |
| `host_no_deadman` | rejected | a gated eviction addresses a host that runs no deadman | operator |
| `host_never_contacted` | rejected | a gated eviction addresses a host never in contact | operator |
| `eviction_bound_pending` | rejected | the eviction bound has not passed; `error` names the remaining wait | wait |
| `seal_retry_mismatch` | refused | a `seal` names the committed boundary's `request_id` under a different envelope | fix the request |
| `seal_in_flight` | refused | another boundary is in flight | wait |
| `no_lineage` | refused | the engine leads no lineage, so there is no boundary to close | operator |
| `stage_digest_mismatch` | refused | `stage_digest` does not match the digest of `next_period` | fix the request |
| `nothing_staged` | refused | nothing is staged at `stage_digest` | fix the request |
| `seal_input_after_cutoff` | refused | an input arrived stamped after the cutoff | wait |
| `seal_not_settling` | refused | inputs keep arriving while the boundary drains or proves | wait |
| `seal_not_quiescent` | refused | the estate is not quiescent at the cutoff | wait |
| `seal_supervisor_unproven` | refused | the supervisor clauses cannot be proved: no client, unreachable, a refused `LIST`, or another incarnation | operator |
| `seal_run_unaccounted` | refused | the supervisor's `LIST` and the carried executions disagree | operator |
| `seal_retry_horizon` | refused | an external attempt is younger than the closing period's retry horizon | wait |
| `clock_regressed` | refused | admission time moved backwards during the cutoff | operator |
| `seal_refused` | refused | one of the boundary's own pre-commit refusals: the candidate, its validation, the snapshot or the install. A check on the same path that names no code answers `engine_error` | operator |
| `seal_timeout` | unknown | no boundary outcome arrived within the window | re-read; retry a mutation only under the same request_id |
| `stale_completion` | rejected, stored only | the stale-completion gate rejected an engine-made completion; it is stored, and no client request is answered with it | none |
| `engine_error` | refused | an engine refusal whose raise site names no code | operator |

## 3. Mutating verbs: sendevent, host, seal

```json
{"cmd": "sendevent", "v": 3, "baseline_id": "…", "epoch": 0,
 "request_id": "…", "verb": "CHANGE_STATUS",
 "payload": {"job": "nightly", "status": "SUCCESS"},
 "expect": {"job:nightly": 12}, "claimed_actor": "alice@host"}
```

Each `verb` names exactly one oracle `EventKind`, but the set is not the
whole alphabet: `CHANGE_STATUS` is the wire name of `STATUS`, and `TIMER`,
`MUST_START_ALARM` and `MUST_COMPLETE_ALARM` are the engine's own and have
no wire door. Every admitted command is journaled with `source=control`; the
WAL is the audit trail of engine decisions, and no second log carries them.
A run started without a journal has no WAL and therefore no audit trail —
the same run `subscribe` refuses in §5, and not a configuration an operator
meets.
Access decisions at the DL-146 perimeter (admissions and denials at the
boundary) go to the perimeter journal (DL-148) (`docs/access-model.md` §6) and
never enter the WAL.

| verb | `payload` | notes |
|---|---|---|
| job verbs | `job` | `STARTJOB`, `FORCE_STARTJOB`, `KILLJOB`, `ON_ICE`, `OFF_ICE`, `ON_HOLD`, `OFF_HOLD`, `ON_NOEXEC`, `OFF_NOEXEC`, `DISARM` *(DL-158)*, `RELEASE_RESOURCE` *(DL-256)* |
| `SET_GLOBAL` | `name` (a non-empty string), `value` (a string) | the empty string is a legal value: `v(G) = ""` is a legal condition, so an operator must be able to satisfy it |
| `CHANGE_STATUS` | `job`, `status`, optional int `exit_code` | injected as `STATUS`, keeping overwrite parity |

Every string a payload carries must be a **Unicode scalar string**: an
unpaired surrogate is refused at the door, because one admitted would leave
the estate unsealable (PR-10a, period-model §3.2). The rule covers the
strings a verb reads: a field the verb does not read is never copied, never
journaled, and cannot reach a seal (DL-228).

`DISARM` (DL-158) is the explicit journaled disarm
period-model §10.4 names. It clears the job's SEM-32 armed latch and does
nothing else: no status move, no start, no wake, no timer touched. A target
with no latch is an accepted, journaled no-op — the `OFF_HOLD` shape — and
the verb is legal at any time, not only before a seal. It drops only the
latch visible at application time: `applied` does not inhibit a later arm,
and it cancels no start already out of the latch. The trace marker is
`DISARM`; `SCHED_DISARM` stays the engine's own marker for scheduler-caused
drops (the Q3c box fold). The durable audit distinction is the trace
reason, not the decision's revisions map: the oracle records `sendevent
DISARM` for a real drop and `sendevent DISARM (no latch)` for a no-op. The
revisions map is not the discriminator, because it carries every revision
the batch moved, the target's own timers included — a timer of the
target's due at the DISARM's instant fires in the same batch and moves the
target's revision whether or not the latch was set (DL-233).

`RELEASE_RESOURCE` (DL-256) is the vendor's `sendevent -E RELEASE_RESOURCE
-J job`. It joins the job verbs and takes their whole contract: the
payload, the mandatory `expect` on `job:<name>`, journaling at admission,
and replay. No frame, field or record kind changes; the event alphabet
grows by one kind, which protocol-evolution §1 already covers (readers
upgrade first). It gives back every resource unit a job that is not
running still holds, and wakes the queue. No status moves. The units are
on the job's row, so a release moves the job's revision. A job holding
nothing, and a running job, are accepted, journaled no-ops; the trace
marker is `RELEASE_RESOURCE`, and its reason names the outcome:
`sendevent RELEASE_RESOURCE (frees …)`, `(nothing held)`, or
`(no effect: RUNNING; …)`.

**`expect` is mandatory** (`docs/concurrency-model.md` §0). It names the
addressed entity — `job:<name>` for a job verb, `global:<name>` for
`SET_GLOBAL` — and nothing else, at the revision the caller read from a
§4 query. `0` means the entity has no row yet, which is how a conditional
create is expressed. A command with no `expect`, a malformed one, or one
naming any other key is **refused**: nothing is admitted, no index is
consumed, and the log says nothing about it.

`request_id` is required. Without one a timed-out command cannot be
retried safely, because nothing could recognise the retry as one. An
exact retry — same id, same fingerprint — is answered from its original
decision and takes no second index. With an access map configured
(DL-147), that is what happens **after** the perimeter admits the
request: admission decides under the current policy, so a caller who has
since lost the tier is denied at the boundary before the retry route is
reached (`docs/access-model.md` §5, §7). A reused id under a *different*
command is refused as a collision. `expect` participates in the
fingerprint, so the same verb at two revisions is two commands.

**A `sendevent` or `host` collision refusal carries the id's earlier
decision** (DL-217). When the id already holds one, the refusal adds
`"original_decision": {index, request_id, decision, reason, code, revisions}`,
the decision's own fields. `code` is the stored one, null on
an application and on a decision written before DL-272. The answer stays a
refusal of this request: `ok: false`, `refused: true`, and the read header
of the engine that answered. The nested decision belongs to the earlier
command and is never this request's outcome. An id admitted but not yet
decided carries no `original_decision`. The `seal` verb keeps its own
retry route (below) and never carries the field.

**Recovering a lost answer.** The fingerprint covers `baseline_id`,
`epoch` and `expect`. A retry must place the values its original placed,
not values re-read after the loss. `dsl41 sendevent` and `dsl41 host`
print the id and those three values on stderr before the first write, as
the flags `--request-id`, `--expect`, `--epoch` and `--baseline`, and
repeat them in the exit-4 advice. Re-running the original arguments with
them gives these answers:

- the original was decided: the retry is answered from that decision and
  takes no index, in the same period, after the entity moved, and after a
  same-period restart that rebuilt the index from the WAL;
- the original was never admitted, and the engine has since restarted:
  the retry names a superseded epoch and is refused as stale, "re-read
  and re-compose". Nothing applies;
- the original was never admitted, and the same engine still leads: the
  retry is a fresh command at the pinned `expect`. It applies if the
  entity is still at that revision and is rejected if it moved;
- the actor claim changed, because it ran as another user or host, or an
  access map now stamps a different principal: the envelope differs, and
  the answer is a collision carrying `original_decision`, not a replay.

The route is bounded. It holds within one protocol version, and within the
period whose decision index holds the id.

`baseline_id` must match the engine's. A revision read from another
baseline names nothing here.

`epoch` is required; it is checked **after** deduplication, so an exact
old-epoch retry recovers its original result while an unseen old-epoch
request is refused as stale (DL-99: with no election, an engine accepts
epoch 0 as inert).

`claimed_actor` is optional and is exactly what it says: there is no
authentication at this tier (§7), so it is recorded as the caller's claim
about itself and is never treated as a principal. With an access map
configured (DL-147), the server authenticates the peer by
kernel credential and OVERWRITES this field with the canonical spelling
`os/<name>` before anything is fingerprinted or logged —
`docs/access-model.md` §3, DL-146. On unconfigured estates the sentence
above stands unchanged.

**The response is the decision, not the receipt:**

```json
{"ok": true, "kind": "ON_HOLD", "decision": "applied", "index": 12,
 "request_id": "…", "revisions": {"job:nightly": 13}, …header…}
```

`ok` is true only for `decision: "applied"`. The other three outcomes are
three different facts, not one failure, and a client must be able to tell
them apart from the answer alone:

| outcome | on the wire | what happened | the caller's next move |
|---|---|---|---|
| applied | `ok: true` | the oracle applied it | — |
| **refused** | `ok: false`, `refused: true` | nothing admitted, no index consumed, **nothing in the WAL** (a perimeter denial attempts its own synced receipt first, and denies whether or not it lands, §7) | this request never happened: fix and re-send. A refused retry says nothing about its original, which may have applied (DL-217) |
| **rejected** | `ok: false`, `decision: "rejected"`, an `index` | a decision went against it — over this verb, always the precondition losing its race. Journaled, and its batch's time observation applied | re-**read** and re-decide; the same envelope loses the same race, because `expect` is in it |
| **unknown** | `ok: false`, and neither marker | admission is uncertain: no decision arrived within the window below, or a handler raised | re-read. Retry **only** under the same `request_id` |

`refused: true` is therefore load-bearing in its absence: it appears on
every `ok: false` that says nothing was admitted — including the shared
doors, a malformed frame and a wrong `v` — so that an `ok: false` carrying
neither marker means uncertainty rather than a fourth kind of no. Two
answers land there: the no-decision timeout below, and the internal-error
answer of a handler that raised (§2). A client reads both as `unknown`.
`dsl41 sendevent` spends a
distinct exit code on each (0/2/3/4). It prints its `request_id` and pins
on stderr before every write (DL-217), and repeats them as
retry advice when the answer is `unknown`; they are the only thing that
makes that retry safe.

The server waits for the decision rather than acknowledging admission,
because a precondition whose outcome the caller cannot see is not a
precondition. If none arrives within 5 s it answers that it does not
know, and says to re-read before retrying — the command may be durably
admitted and about to apply.

Job arguments are validated against the catalog — vendor `sendevent`
errors on an unknown job rather than queueing it — and that check
necessarily precedes the `expect` check, since the payload is what says
which entity is addressed. `CHANGE_STATUS` carries one deliberate
exception (SEM-07): `"JOB^INST"` where `INST` is a declared
`insert_xinst` is a legal target even though no such job is in the
catalog. Overwriting the store's pseudo-entity is exactly how an operator
satisfies a cross-instance atom the sandbox cannot see. Other job verbs
stay catalog-only — starting or killing a ghost is meaningless.

Composing that command's `expect` has one corner: before the row exists,
`status JOB^INST` **refuses**, because the name is in neither the catalog
nor the store. A refused read is revision `0`, which is exactly what the
conditional create names, so the bundled composer reads it that way. Once
`CHANGE_STATUS` has invented the row, `status` answers it like any job.

`status` must be one of the injectable statuses: `INACTIVE`, `STARTING`,
`RUNNING`, `SUCCESS`, `FAILURE` or `TERMINATED` (DL-264). `QUE_WAIT` is
refused. It is an internal state of the capacity owner (DL-50), and the
oracle gives an injected `QUE_WAIT` no meaning. The refusal comes before
the journal append, so it consumes no log index, and it lists the
injectable set. Whether the vendor's `sendevent -s QUE_WAIT` is legal is
not known; this is a stated support limit, not a claim about the vendor.

### `host` (S5a, DL-94)

```json
{"cmd": "host", "v": 3, "baseline_id": "…", "epoch": 0,
 "request_id": "…", "verb": "drain", "payload": {"id": "local"},
 "expect": {"host:local": 1}, "claimed_actor": "alice@host"}
```

One change to `docs/concurrency-model.md` §8's routing table: which
execution hosts take new work. `verb` is `activate` | `drain` | `evict`,
and `payload` is `{id, force?}`.

**v3 carries a fourth host verb, `route`, and its query; both are
specified and neither is built** (DL-118; period-model §2.2). They sit in
v3 beside the `seal` verb and the gap marker, and the shape is frozen so
that two implementations cannot choose incompatible JSON
for one fact:

```json
{"cmd": "host", "v": 3, "baseline_id": "…", "epoch": 7, "request_id": "…",
 "verb": "route", "payload": {"id": "<role>", "executor_id": "…"},
 "expect": {"route:<role>": 3}, "claimed_actor": "…"}

{"cmd": "routes", "v": 3, "roles": ["<role>", …]}
→ {"ok": true, "routes": {"<role>": {"present": true, "executor_id": "…",
    "state_rev": 3}}, …header…}
```

A remap moves one role→executor row. It is applied to the owner, carries no
oracle event, and is **rejected** — not refused — when `executor_id` names no
host row, because that check reads mutable state like every other one in the
table above (period-model §3.3). `route:<role>` is the fourth `expect`
namespace, beside `job:`, `global:` and `host:`. The `routes` query keeps the
`hosts` query's corners: an absent role answers
`{"present": false, "state_rev": 0}`, and omitting `roles` answers the whole
table. The WAL record is `host: {verb: "route", id, executor_id}`, and the
answer is the same decision shape and the same four outcomes as every host
verb.

**None of it is built.** There is no `route` verb, no `routes` query, no
`RuntimeState` route storage and no `route:` namespace; the shipped table is
the one implicit row period-model §3.3 describes, projected from the single
local executor at revision 0. The unit that adds the storage builds this
(designed in the withdrawn HA plan (DL-189), S8b); the wire is written here
first so that unit implements a shape rather than inventing one.

A **separate `cmd`** because the verb sets are separate things. Each
`sendevent` verb names one oracle `EventKind`; a host verb deliberately
names none,
because a job's condition truth cannot depend on where its machine routes
(DL-93). The **envelope is the same envelope**, parsed by the same
function: §0's mandate is on externally requested mutations, not on a
particular vocabulary, so `expect` is mandatory here too and names
`host:<id>` — the third namespace, beside `job:` and `global:`. The answer
is the same decision shape and the same four outcomes, with `kind` carrying
the host verb.

`quarantine` and `reinstate` are **not** operator verbs, and the wire
refuses them by name. §8 assigns that state to the leader, automatically,
from what it can and cannot reach; those two arrive through the engine's own
door with no `expect`, because §0's mandate is on externally *requested*
mutations and an observation about reachability is not one. An operator verb
for quarantine would also blur it with `drain`, which asserts nothing about
whether the host is answering.

What is refused and what is rejected divides on one line — whether the
check reads mutable state:

| refused (nothing in the WAL) | rejected (a decision, at an index) |
|---|---|
| an unknown verb, a `payload` that is not an object, a missing, empty or non-string `id`, an `id` carrying an unpaired surrogate (PR-10a), a non-boolean `force`, a bad envelope | the host is not in the table; the state forbids this verb; an `evict` whose §8 preconditions do not hold |

The eviction preconditions are §8's three. They read mutable state, so
their failure is a **rejection** at an index and not a refusal, and it
reports the remaining wait so the operator waits rather than guesses.
`force: true` skips them, is recorded with the caller's claimed actor on the
row as well as in the log, and is the one path in the concurrency model that
can produce a double run. Every one of these checks is a pure function of
the row: replay has no live host to probe, so a gate that probed one would
decide differently the second time.

### `seal` (DL-133, period-model §2.2)

A third mutating verb (DL-133; period-model §7), and it is unlike the two
above in three ways, each one a consequence of what a boundary is.

```json
{"cmd": "seal", "v": 3, "baseline_id": "…", "epoch": 7, "request_id": "…",
 "next_period": {…StagedNextPeriod…}, "stage_digest": "…",
 "force_seal": false, "claimed_actor": "alice@ops-laptop"}

{"ok": true, "kind": "seal", "decision": "applied", "period_id": 2,
 "digest": "sha256:…", "next_period_id": 3, "next_baseline_id": "…",
 "request_id": "…", …header…}
```

**It names an `expect` on nothing.** A seal addresses no row, so `expect`
is not merely optional here — it is **refused**. §0's mandate is about a
caller asserting what they read about an entity; a boundary reads no
entity. Making it optional would turn the one command with no precondition
into the door every other command could slip through, so the envelope
parser takes "this command addresses no row" as an explicit input and
rejects an `expect` that arrives anyway. What arrives is the KEY (DL-151):
`"expect": null` is an `expect` and not its absence, and the seal door
refuses it, just behind the retry route below, which answers a committed
seal whatever rides beside it.

**Its decision is a `seal` record, not a `decision` record.** Before the
seal, a durable "applied" would name a boundary that never happened; after
it, records after a seal are forbidden; and no record at all would leave a
lost response unretryable despite the promised `request_id`. So the record
IS the commit point, and it carries the request's identity — `request_id`,
the fingerprint over the whole envelope, the claimed actor and
`force_seal`.

**A committed seal's exact retry is answered ahead of the baseline gate.**
The generic v3 parser rejects a foreign `baseline_id` before it reads
`request_id`, and a retry of the boundary that closed C1 necessarily
carries B1 while C2 answers under B2 — so without a dedicated route the
exact-retry promise would be unreachable at exactly the moment it matters.
The engine of period N+1 keeps the `seal` record it opened from and checks
an incoming `(request_id, fingerprint)` against it first. That check sits
ahead of the whole generic parser, not the baseline gate alone: a matched
retry is answered from the record without the `expect` refusal above ever
running, because a retry of a boundary that already committed is the same
command whatever else rides beside it. a match is
answered `applied` from that record, and a match on the id under a
different envelope is a **collision**, refused. Force is an authorization
and the actor is attribution, and neither may be swapped under a retry.
The lookup reaches exactly one seal back; an older seal's retry is refused
as a stale baseline, which is a liveness loss and not a safety one.

**An uncommitted seal request is unseen.** A request that crashed before
its record left nothing behind, so its retry is a fresh request that
attempts the boundary again. Only a committed seal is ever deduplicated,
which is also why sealing twice is impossible: a second attempt finds the
first's record if it landed.

The client stages C2 first — the immutable bundle under `catalogs/`, then
`staged_manifest.json` and `candidate.json` under
`periods/.staging/<stage_digest>/` — and the engine validates **exactly
those bytes**. On success the engine **exits with code 3** ("sealed;
period N+1 is ready to open"), distinct from its 0/1/2 so an init system
does not restart-loop a sealed engine, and without signalling detached
work. It does not load C2 into itself: a transition is a restart, not a
reload (DL-65). The answer's timeout is longer than a mutation's, because
a boundary drains every admitted attempt and waits an unbound spawn and an
unresolved KILL ladder out; a timeout is `unknown`, never a refusal. If the
engine stops during a seal because an attempt admitted there is not fully
applied, because a WAL append failed, or because an engine-made input could
not be admitted, the seal gets no answer, and neither
does an attempt whose answer was still owed. The connection drops, and the
client reads those outcomes as `unknown`. The seal did not commit: recovery
rebuilds from the WAL, and the period stays open (DL-274, `period-model.md`
§7).

## 4. Query verbs (frozen response shapes)

**A read is refused when this engine cannot prove it leads the estate's
LINEAGE** (DL-133; period-model §1.3). Every
answer below carries the §2 read header (headerless exceptions enumerated
there, DL-148) — a baseline, an epoch and a log
position — and those are exactly the coordinates a displaced leader may no
longer speak for, so a `status` immediately after the anchor directory is
deleted or replaced is refused rather than answered, and the refusal
carries no header (PR-03). Two proofs, two rules: losing the RUN ROOT's
proof is `concurrency-model.md` §7's, and it stops dispatch on the way into
admission's first append rather than at this door — refusing there would
leave a displaced leader answering nothing and stopping never, and would
also refuse the read a client composes its `expect` from, so the very
mutation that stops the engine could never be sent (CM-14). A `subscribe`
response is a read and is refused on the same rule (DL-148): the same
lineage proof, not the same header: no `subscribe` line — the ack, a
refusal, a marker or a record — carries the read header (§2), and a
subscription is not a `concurrency-model.md` §6 revision-bearing read.

Every query is a **pure projection** of (oracle, catalog, scheduler,
outbox, spool paths). None changes protocol-visible engine state and none
inserts a store row; `status` does refresh one private cache, the
`spec_drift` re-hash below. They are safe to serve from the engine's task
because `feed()` never yields, so a handler can never observe a
half-applied event.

Every §4 query answer that is answered — not the lineage refusal, not
an internal-error answer (both headerless, §2 DL-148) — carries the
read header in addition to the fields listed below. These are the reads an
`expect` is composed from: per-job `state_rev` in `status`,
`global`/`globals` for a named global, and `hosts` for a routing row.
Revision-bearing reads are leader-only (v2, DL-90): one engine per run
root *is* the leader, there is no election, and `leader.lock` is what
enforces it (`docs/concurrency-model.md` §1).

### `status [job]`

`{"ok": true, "jobs": {<name>: {…}}, "spec_drift": bool|null}`. Omitting
`job` returns every catalog job unioned with every store row (so
`CHANGE_STATUS`-invented ghosts appear).

Per job: `status`, `status_at`, `run_number`, `exit_code`, `on_ice`,
`on_hold`, `on_noexec`, `armed` (the SEM-32 latch — an armed job looks
INACTIVE but the next condition edge starts it, DL-54), `started_by` (the
trace cause of the most recent start; null = never started, DL-68),
`pending_timers` (a list of `{due, kind}`), `log_out` / `log_err` (the §6
append targets of the CURRENT run, resolved by the same `job_log_paths`
the wrapper spec uses, so the tail and the writer can never diverge; CMD
only), `job_type` and `box_name` (catalog placement, so a tree renders
without a second query; null for ghosts).

`held` (DL-94) is true when the oracle started the job and this engine
dispatched no process, because its executor routes no new effects
(`docs/concurrency-model.md` §8). Derived from the outbox's pending SPAWN
intents (S5c) rather than stored as a job field, so it survives a restart.
It is published because a held job reads RUNNING — the oracle walks a start
through STARTING to RUNNING in one feed — so status alone cannot tell a
drained estate from a working one, and a drain is an operation an operator
has to be able to watch.

`watching` — `{file, interval, min_size}` — is present **only** for a
live FW run. Absence of the key is itself the "not watching" signal
(DL-68).

`spec_drift` is a lazy re-hash of the input files the run loaded, at most
every 15 s. `null` means the server holds no fingerprint. There is
deliberately no reload: the flag tells an operator the running catalog no
longer matches disk, and adopting it takes a cold restart (DL-65).

### `trace [since]`

`{"ok": true, "last_seq": int, "entries": [{seq, at, job, transition,
cause}]}`. `seq` is a 1-based position in the oracle's trace; `since` is
exclusive.

### `explain <job>`

`{"ok": true, "job", "condition": <source>|null, "satisfied": bool,
"atoms": [{atom, true, actual?}]}`. `actual` appears for global atoms and
carries the effective value (null = never set) — atom truth alone hides
why (DL-66). A job with no condition answers `condition: null`,
`satisfied: true`, `atoms: []`.

Truth comes from the oracle itself (ice bypass, lookback, instances), never
a second evaluator.

### `spec <job>`

`{"ok": true, "job", "job_type", "box_name", "jil": <text>|null}` — the
preserve-rendered, post-placeholder JIL block **this run loaded**, not the
template on disk (DL-64). `jil: null` when the server was started without
source texts (embedders).

### `deps <job>`

`{"ok": true, "job", "upstream": [...], "globals": [...], "downstream":
[...], "box_name", "members": [...]}` (DL-65, the blast-radius view).
`upstream`/`globals` are split by **atom type**, never by sniffing key
strings — a job legally named `g:x` (DL-39 escapes) would otherwise
collide with global `x`. `downstream` comes from the oracle's
edge-trigger index, which is keyed by those strings and therefore does
inherit the collision for such a name — a limit, not a guard. Condition
edges are not the whole blast radius of a box, so containment is served
alongside: `box_name` upward, `members`
downward.

### `timers`

`{"ok": true, "timers": [{due, job, kind, detail?}]}` — every pending
oracle timer plus each scheduled job's next calendar tick, due-ordered
(DL-65). `due` is **nullable** (DL-68): live filewatches join as
trailing rows with `due: null` and a `detail` string, because they fire on
a file, not a clock.

### `plan`

`{"ok": true, "waves": [[...]]}` — wave-by-wave topological batches over
the AND-success skeleton. Refuses with `ok: false` naming the cycle when
the skeleton is cyclic; cycles are legal AutoSys (DL-13), so this is a
query refusal, not a run refusal.

### `global <name>` / `globals <names>`

`{"ok": true, "globals": {<name>: {"present": bool, "value": <string>|null,
"state_rev": int}}}` — the read a `SET_GLOBAL` precondition is composed
from (`docs/concurrency-model.md` §6). Named entities only, and an unset
name is **answered**, at revision `0`, rather than omitted: absence you
cannot name is absence you cannot lock against, and `0` is exactly what a
conditional create conditions on. Neither verb inserts a row.

A map of every global would not do instead — it can report what exists,
never that a particular name does not.

### `hosts [ids]`

`{"ok": true, "executor": "<this engine's host id>", "hosts": {<id>:
{"present": bool, "state", "generation", "deadman_s", "last_contact",
"forced_by", "state_rev"}}}` — `docs/concurrency-model.md` §8's routing
table, and the read a `host` command's `expect` is composed from. An
absent row answers `{"present": false, "state_rev": 0}`.

Unlike `globals`, omitting `ids` answers the **whole table**. That is not
an inconsistency: §7's takeover barrier has to reconcile every host in the
table, so a complete enumeration is a meaningful answer here in a way it
is not for globals, where a map of what exists can never express the
absence a conditional create conditions on. Named `ids` are still answered
individually, for exactly that case.

`forced_by` non-null is an incident marker, not decoration: this host's
work was declared rerouteable without proof its executor was dead (§8's
`--force`).

## 5. Streaming verb: subscribe

`{"cmd": "subscribe", "v": 3, "since": <int>?}`. A subscription **owns its
connection** until hangup, so a client opens a separate connection for it.

The server answers `{"ok": true, "subscribed": true, "since": <int>}`,
then streams journal records (`docs/runner-design.md` §7 record kinds) as
raw lines. The ack's `since` is the stream's cursor (DL-267): the request's
`since` when it named one, otherwise the admission frontier sampled for
the seam below. Every seq'd record the stream owes is above it. It is an
additive answer field, which a v3 client ignores (`docs/protocol-evolution.md`
§1, DL-217). A run with no journal is refused with `{"ok": false, "code":
"no_journal", "error": "this run has no journal"}`.

Delivery guarantees, exactly as implemented:

- seq'd records (`input`, `advance`, `host`) are **exactly once** across the
  backfill/live seam. The seam is sampled *before* the ack is written,
  because a record appended during the send would otherwise be skipped as
  "covered" despite never being backfilled.
- every other record kind carries no `seq` and is **at-least-once** inside
  the backfill race window: `dispatch`, `drop`, `decision`, `effect_result`
  and `seal` on the live stream, plus `segment`, `leader` and `preflight`,
  which a backfill can carry but which no live subscription meets.
  `decision` carries its attempt's number under `index` rather than `seq`
  for exactly this reason: two records sharing one cursor value would leave
  the second undeliverable to a resuming subscriber (DL-89).
- `since` cuts positionally: everything after the last record whose seq is
  at or below it.

The stream carries `decision` records, one line carrying the decision, its
revisions and the effects it planned (DL-118, the v3 wire break: `decision`
replaces `result` and the standalone `effect` record). The seam keys on the
presence of `seq`, not on a record name, so `decision` carries the
at-least-once guarantee. `effect_result` is a stream record too.

**The backfill spans segments** (DL-135; period-model §11). An estate's
records live in `wal/<segment_no>.jsonl`, one segment per period, and the
backfill covers every retained segment from the one holding the cursor
onward, delivering records oldest first, so a subscriber that resumes with
a cursor taken before a boundary sees the records before it (the walk
itself is bounded; see below). `since` is an index, the cut is positional,
and the seam
keys on the presence of `seq`, so the exactly-once and at-least-once
guarantees above hold across segments.

**A cursor below the earliest retained record gets a gap marker.** The
server sends one line, `{"gap": true, "earliest_retained": <index>}`,
before the backfill, and then backfills everything it has. `earliest_retained`
is the first index the oldest retained segment may allocate. A physical
roll is the reachable case: the new root holds the seal it opened from and
none of the closing period's WAL, by design (period-model §1.3), so a
subscriber resuming there cannot be given what it asked for and is told
so. The rule is exact: a marker goes out when no retained segment holds the
cursor **and** `max(since, 0) < earliest_retained - 1`. A cursor at
`earliest_retained - 1` is contiguous with what the root holds and gets
none; one below it is missing a record and gets one. A negative cursor
reads as 0, so on a root that still retains from index 1 no cursor is ever a
gap — but on a rolled root `since: 0` is one. A client that does not read
the marker sees an ordinary record it does not recognise and skips it, which
is what it already does with any record kind it has no case for. The marker
is a response like any
other, so the leader re-proves the lineage in front of it (PR-03).

**The backfill can refuse on the stream** (DL-135). It reads files this
subscription's own period did not write, so it can meet a foreign name
under `wal/` or a closed segment whose tail is missing. Either is
`{"ok": false, "code": "backfill_refused", "error": …}` sent *after* the
ack and then a hangup — never a hangup with no answer, and never a stream
that silently skips the records it could not read. The read is bounded:
it walks segments newest first and stops at the one holding the cursor,
so a cursor inside the live period costs one segment however long the
lineage is.

**A subscriber that stops reading is removed** (DL-267). Each subscription
has a backlog: the live records appended for it and not yet written to its
socket, counted in the bytes of their stream lines. The budget is
`4 × LINE_LIMIT`, 64 MiB (`SUBSCRIBER_BACKLOG_BYTES`). It is three request
lines for an input record, because the input carries the request's payload
and the stream escapes every non-ASCII character, plus one request line
for the largest decision a bundled client can read. Both bundled readers,
`ControlClient.subscribe` and the CLI's `subscribe_lines`, read stream
lines up to the same bound, so every record that fits the budget is one
they can read.

A record enters an empty backlog. Otherwise it enters only if the backlog
plus the record stays within the budget. So a reading subscriber whose
backlog is empty is not removed by one admitted command whose records fit
the budget, and one record larger than the budget delays a subscriber and
does not remove it. A larger burst removes even a subscriber that was
keeping up: the engine applies queued commands without yielding
(`docs/concurrency-model.md` §4), so its handler cannot drain between
them. That client resumes from its cursor (§6) and loses nothing. A
refused record removes the subscription:

- the journal drops the backlog and removes the subscription, after every
  other subscription has the record;
- the server then cancels the handler and aborts the transport at once,
  and reports the reason on its stderr, best effort;
- the journal append has already succeeded. Nothing from the removal, an
  owner that raises or a stderr that cannot be written included, reaches
  the append, the engine loop or any other subscription;
- the stream ends with EOF, perhaps after a torn last line. It never
  continues past a missing record. It carries no terminal line: EOF and
  the §6 cursor are the signal, and the peer that stopped reading could
  not receive a line anyway.

A subscription's queued backlog is at most the budget or one record,
whichever is larger, plus one record in the transport beyond its
high-water mark. The budget counts encoded bytes; the queued records are
shared Python objects and take more memory than that.

A stream line past `SUBSCRIBER_BACKLOG_BYTES` is unreadable to the bundled
clients, and resubscribing at the same cursor meets it again. They raise a
distinct error, `StreamLineTooLong`, and the CLI names no `--since` for
it.

**The backfill phase is outside the budget.** A subscription with `since`
holds the backfill records it has read and not yet sent: the retained WAL
from the segment holding the cursor onward. The retained WAL bounds it,
not the budget, and each subscription reads its own copy. Each record is
released once it is sent, and the phase holds nothing once the live
stream starts. Live records queue against the budget meanwhile, so a
client that stalls inside its backfill is removed there like any other.

**How the server ends a connection** (DL-267). This covers every
connection on the control socket, request/response and `subscribe` alike.
An overflow or an access revocation (`docs/access-model.md` §7) aborts
the connection at once and discards its unsent bytes. Every other end the
server makes closes the connection and gives the peer
`HANGUP_GRACE_S`, 2 s, to take the unsent bytes; then it aborts. These
ends are a shutdown, the end of a request/response connection after the
client's EOF, and a stream that ends on a refusal. A reading client
therefore still receives an answer already queued when the engine shuts
down, and a peer that stopped reading delays a shutdown by at most the
grace. A close alone would wait until the peer read, for as long as it
stayed stalled.

There is no second durable feed. A removed client reconnects with a
cursor (§6), and the backfill, the seam and the gap marker above give it
what it missed.

## 6. Client obligations

- Any transport error must drop the connection. Reusing a connection after
  a **cancelled** exchange hands the unread response line to the next
  request and offsets every reply after it (DL-46). `ControlClient` drops
  on `OSError` and on any `BaseException` including `CancelledError`.
- A failure after a write was attempted is `delivered` (§2, DL-216). Both
  clients serialize the request before they touch
  the connection, so an unencodable request is the caller's error and
  never a transport outcome.
- Open every connection with an explicit `LINE_LIMIT`; the default
  readline buffer fails on real estates. A `subscribe` connection reads
  its record lines up to the §5 budget, `4 × LINE_LIMIT`, because an
  escaped input record can be three times a request line (DL-267).
- A torn line in a `subscribe` stream is skippable — records are a
  wake-up signal, and the WAL on disk is the truth.
- A `subscribe` stream can end at the engine: EOF, a torn last line, or an
  `{"ok": false}` line (§5). A client that must not miss records
  reconnects with a cursor (DL-267): the last `seq` it read, or, when it
  read none, the ack's `since`. `seq`'d records then arrive exactly once
  across the two connections; records without a `seq` after that cursor
  may arrive twice. `dsl41 query subscribe` exits 2 when the engine ends
  its stream and names the exact `--since` on stderr: the last `seq` it
  printed, or the ack's `since`. Its records are on stdout, so a
  monitoring wrapper restarts it with that `--since`. The TUI reconnects
  after one second and repolls; it needs no cursor.

## 7. Known gaps (recorded, not fixed — DL-78)

These are honest limits of the frozen protocol, listed because the
multihost track (`docs/decision-log.md` DL-78) has to address each one.

1. ~~No version handshake~~ — **closed** by v2 (DL-90, §2); v3 is DL-118.
2. **No authentication or authorization — on an estate with no access
   map.** There the socket's `0600` mode plus filesystem ownership is the
   entire access-control model, and any process running as the invoking
   user has full `sendevent` authority. That is why the envelope's actor
   field is named `claimed_actor`: the log records an assertion, and
   `docs/concurrency-model.md` §6's "the leader stamps the authenticated
   principal" applies only where a leader can authenticate one.
   **Closed for a configured estate (DL-146):** authorization and
   *local* authentication close with `docs/access-model.md` — kernel
   peer credentials, one principal→tier map, a closed verb table gated
   in `_handle`, denials answered in the existing `refused` vocabulary
   with attempted receipts in a perimeter journal. No envelope change.
   The web session's per-user identity stays open under the named seam
   `web-session-principal-v2` (access-model §9).
3. **Unix-domain only.** No network transport, so the single-engine
   guarantee rests on a local `flock` and a local `bind()`. A non-local
   controller would need both a transport and a replacement for that
   guarantee.
4. ~~Errors are prose, not codes~~ — **closed** by DL-272 (§2): every
   `ok: false` answer carries a stable `code` beside the prose `error`.
