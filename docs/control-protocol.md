# Control protocol — the engine's public contract

Status: frozen at **v2** (2026-08-15, DL-90; first frozen 2026-08-13,
DL-78). This document is normative for the runner's §10 control plane in
the same way `docs/supervisor-protocol.md` is normative for the §6a
lifecycle tier. Each change to a frozen item requires a decision-log
entry.

**v1 is gone, not deprecated.** `docs/concurrency-model.md` §0 refuses a
caller that does not name a version, and accepting unversioned requests
"for compatibility" is exactly the opt-out it forbids. An engine at this
version answers a v1 client with a refusal naming the version it speaks.

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
the wire vocabulary, and both clients.

## 1. Roles

- **server** (`ControlServer`): one per run root, owned by the engine
  process. Mutating verbs inject into the engine's single-writer loop;
  query verbs are pure projections of oracle state.
- **async client** (`ControlClient`): one persistent request/response
  connection plus separate connections for `subscribe`. Drives the §11
  TUI (`dsl41 run --ui`, `dsl41 serve`).
- **sync client** (`roundtrip`): one-shot blocking request/response for
  callers outside an event loop. Drives `dsl41 sendevent` and
  `dsl41 query`.

Both clients raise `ControlClientError` on any transport or decode
failure. Neither maps errors to exit codes — that is the CLI's job.

## 2. Transport and framing (frozen)

- A **unix domain socket** at `<run_root>/control.sock`, mode `0600`
  (set via umask at bind, re-chmod'd after, because some platforms ignore
  umask on bind).
- **JSON lines**, both directions. One request object per line; one
  response object per line. The stream buffer limit is `LINE_LIMIT`
  (16 MiB): one `status` response covers every job on a single line and
  overruns asyncio's 64 KiB default at roughly 300 jobs.
- **Every request carries `"v": 2`**, including queries and `subscribe`.
  The check runs before the request is routed at all, so `subscribe` —
  which owns its connection and never reaches the response path — is not
  the one unversioned door left open. A refusal does not close the
  connection: the next line on it may be well-formed.
- Responses are `{"ok": true, …}` or `{"ok": false, "error": "<message>"}`.
  Errors are human-readable strings, **not** stable codes.
- Every response also carries the **read header**: `baseline_id`, `epoch`
  and `applied_index` (`docs/concurrency-model.md` §6). A revision means
  nothing without the log it was read from, and a client that cannot name
  the log cannot be told it is holding a stale one.
- A malformed line answers `{"ok": false, "error": "bad request: …"}` and
  the stream stays in sync. A handler that raises answers
  `{"ok": false, "error": "internal error: …"}` rather than dying
  unreplied — a client must never see a bare timeout for a query bug.
- Consumers must ignore unknown fields (forward compatibility).

**Socket lifecycle.** On start the server probes an existing socket file
by connecting: a successful connect means a LIVE engine already serves
this run root and the second engine refuses; a refused connect means a
crashed run's leftover, which is unlinked and claimed. Two engines racing
past the probe are separated by the bind itself. This probe is the *only*
mechanism enforcing one engine per run root — see §7.

**The version handshake** is `"v": 2` on every request (DL-90). This was
listed here as a known gap through v1; it closed in the same break that
made preconditions mandatory, because two wire breaks would have cost
every client twice.

**No controller lease.** Deliberate (DL-41a). `sendevent` is multi-writer
by AutoSys nature, and the engine's single-writer loop serializes every
injection. The lease guards the supervisor tier, which spawns without
semantics.

## 3. Mutating verbs: sendevent, host

```json
{"cmd": "sendevent", "v": 2, "baseline_id": "…", "epoch": 0,
 "request_id": "…", "verb": "CHANGE_STATUS",
 "payload": {"job": "nightly", "status": "SUCCESS"},
 "expect": {"job:nightly": 12}, "claimed_actor": "alice@host"}
```

The `verb` set maps 1:1 onto oracle `EventKind`. Every admitted command is
journaled with `source=control`; the WAL is the audit trail, and there is
no second log.

| verb | `payload` | notes |
|---|---|---|
| job verbs | `job` | `STARTJOB`, `FORCE_STARTJOB`, `KILLJOB`, `ON_ICE`, `OFF_ICE`, `ON_HOLD`, `OFF_HOLD`, `ON_NOEXEC`, `OFF_NOEXEC` |
| `SET_GLOBAL` | `name`, `value` (both non-empty strings) | |
| `CHANGE_STATUS` | `job`, `status`, optional int `exit_code` | injected as `STATUS`, keeping overwrite parity |

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
decision and takes no second index. A reused id under a *different*
command is refused as a collision. `expect` participates in the
fingerprint, so the same verb at two revisions is two commands.

`baseline_id` must match the engine's. A revision read from another
baseline names nothing here.

`epoch` is required and inert on a single host; it is checked **after**
deduplication, so an exact old-epoch retry recovers its original result
while an unseen old-epoch request is refused.

`claimed_actor` is optional and is exactly what it says: there is no
authentication at this tier (§7), so it is recorded as the caller's claim
about itself and is never treated as a principal.

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
| **refused** | `ok: false`, `refused: true` | nothing admitted, no index consumed, **nothing in the log** | fix and re-send; unchanged is safe, since it never happened once |
| **rejected** | `ok: false`, `decision: "rejected"`, an `index` | a decision went against it — over this verb, always the precondition losing its race. Journaled, and its batch's time observation applied | re-**read** and re-decide; the same envelope loses the same race, because `expect` is in it |
| **unknown** | `ok: false`, and neither marker | no decision arrived within the window below | re-read. Retry **only** under the same `request_id` |

`refused: true` is therefore load-bearing in its absence: it appears on
every `ok: false` a mutation can meet — including the shared doors, a
malformed frame and a wrong `v` — so that the one answer without it means
uncertainty rather than a fourth kind of no. `dsl41 sendevent` spends a
distinct exit code on each (0/2/3/4) and prints its `request_id` on
stderr when the answer is `unknown`, which is the only thing that makes
that retry safe.

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

`status` must be one of the oracle's `JobStatus` values; the refusal
lists them.

### `host` (S5a, DL-94)

```json
{"cmd": "host", "v": 2, "baseline_id": "…", "epoch": 0,
 "request_id": "…", "verb": "drain", "payload": {"id": "local"},
 "expect": {"host:local": 1}, "claimed_actor": "alice@host"}
```

One change to `docs/concurrency-model.md` §8's routing table: which
execution hosts take new work. `verb` is `activate` | `drain` | `evict`,
and `payload` is `{id, force?}`.

A **separate `cmd`** because the verb sets are separate things. `sendevent`'s
map 1:1 onto oracle `EventKind`; a host verb deliberately maps onto none,
because a job's condition truth cannot depend on where its machine routes
(DL-93). The **envelope is the same envelope**, parsed by the same
function: §0's mandate is on externally requested mutations, not on a
particular vocabulary, so `expect` is mandatory here too and names
`host:<id>` — the third namespace, beside `job:` and `global:`. The answer
is the same decision shape and the same four outcomes, with `kind` carrying
the host verb.

`quarantine` is **not** an operator verb. §8 assigns that state to the
leader, automatically, on unreachability; an operator verb for it would
blur it with `drain`, which asserts nothing about reachability.

What is refused and what is rejected divides on one line — whether the
check reads mutable state:

| refused (nothing in the log) | rejected (a decision, at an index) |
|---|---|
| an unknown verb, a missing or non-string `id`, a non-boolean `force`, a bad envelope | the host is not in the table; the state forbids this verb; an `evict` whose §8 preconditions do not hold |

The eviction preconditions are §8's three, and the refusal reports the
remaining wait so the operator waits rather than guesses. `force: true`
skips them, is recorded with the caller's claimed actor on the row as well
as in the log, and is the one path in the concurrency model that can
produce a double run. Every one of these checks is a pure function of the
row: replay has no live host to probe, so a gate that probed one would
decide differently the second time.

## 4. Query verbs (frozen response shapes)

Every query is a **pure projection**. None mutates, none inserts a store
row. They are safe to serve from the engine's task because `feed()` never
yields, so a handler can never observe a half-applied event.

Every response carries the read header (§2) in addition to the fields
listed below. These are the reads an `expect` is composed from:
per-job `state_rev` in `status`, `global`/`globals` for a named
global, and `hosts` for a routing row. Revision-bearing reads are
leader-only in v2 — one engine per run root *is* the leader until S6
introduces election, and the socket probe in §2 is what enforces it.

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
(`docs/concurrency-model.md` §8). Derived, never stored. It is published
because a held job reads RUNNING — the oracle walks a start through
STARTING to RUNNING in one feed — so status alone cannot tell a drained
estate from a working one, and a drain is an operation an operator has to
be able to watch.

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
edge-trigger index. Condition edges are not the whole blast radius of a
box, so containment is served alongside: `box_name` upward, `members`
downward.

### `timers`

`{"ok": true, "timers": [{due, job, kind, detail?}]}` — every pending
oracle timer plus each scheduled job's next calendar tick, due-ordered
(DL-65). `due` is **nullable** since DL-68: live filewatches join as
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

`{"cmd": "subscribe", "since": <int>?}`. A subscription **owns its
connection** until hangup, so a client opens a separate connection for it.

The server answers `{"ok": true, "subscribed": true}`, then streams
journal records (`docs/runner-design.md` §7 record kinds) as raw lines. A
run with no journal is refused with `{"ok": false, "error": "this run has
no journal"}`.

Delivery guarantees, exactly as implemented:

- seq'd records (`input`, `advance`, `host`) are **exactly once** across the
  backfill/live seam. The seam is sampled *before* the ack is written,
  because a record appended during the send would otherwise be skipped as
  "covered" despite never being backfilled.
- unsequenced records (`dispatch`, `drop`, `result`) are
  **at-least-once** inside the backfill race window. `result` carries its
  attempt's number under `index` rather than `seq` for exactly this
  reason: two records sharing one cursor value would leave the second
  undeliverable to a resuming subscriber (DL-89).
- `since` cuts positionally: everything after the last record whose seq is
  at or below it.

## 6. Client obligations

- Any transport error must drop the connection. Reusing a connection after
  a **cancelled** exchange hands the unread response line to the next
  request and offsets every reply after it (DL-46). `ControlClient` drops
  on `OSError` and on any `BaseException` including `CancelledError`.
- Open every connection with an explicit `LINE_LIMIT`; the default
  readline buffer fails on real estates.
- A torn line in a `subscribe` stream is skippable — records are a
  wake-up signal, and the WAL on disk is the truth.

## 7. Known gaps (recorded, not fixed — DL-78)

These are honest limits of the frozen protocol, listed because the
multihost track (`docs/decision-log.md` DL-78) has to address each one.

1. ~~No version handshake~~ — **closed** by v2 (DL-90, §2).
2. **No authentication or authorization.** The socket's `0600` mode plus
   filesystem ownership is the entire access-control model. Any process
   running as the invoking user has full `sendevent` authority. This is
   the §12 RBAC non-goal made concrete, and it is why the envelope's
   actor field is named `claimed_actor`: the log records an assertion, and
   `docs/concurrency-model.md` §6's "the leader stamps the authenticated
   principal" waits on a leader that can authenticate one.
3. **Unix-domain only.** No network transport, so the single-engine
   guarantee rests on a local `bind()`. A non-local controller would need
   both a transport and a replacement for that guarantee.
4. **Errors are prose, not codes.** Fine for a human at a terminal and for
   the TUI; a programmatic client cannot branch on them reliably.
