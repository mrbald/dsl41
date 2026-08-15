# Control protocol — the engine's public contract

Status: frozen (2026-08-13, DL-78). This document is normative for the
runner's §10 control plane in the same way `docs/supervisor-protocol.md`
is normative for the §6a lifecycle tier. Each change to a frozen item
requires a decision-log entry.

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
- Responses are `{"ok": true, …}` or `{"ok": false, "error": "<message>"}`.
  Errors are human-readable strings, **not** stable codes.
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

**No version handshake.** Unlike the supervisor protocol's `"v": 1`, a
control request carries no version field. Recorded as a known gap, not an
oversight (DL-78): adding a required field would break every deployed
client, and the natural place to introduce a handshake is the first
non-local transport, which needs one anyway.

**No controller lease.** Deliberate (DL-41a). `sendevent` is multi-writer
by AutoSys nature, and the engine's single-writer loop serializes every
injection. The lease guards the supervisor tier, which spawns without
semantics.

## 3. Mutating verb: sendevent

`{"cmd": "sendevent", "event": "<VERB>", …}` — the parity set maps 1:1
onto oracle `EventKind`. Every injection is journaled with
`source=control`; the WAL is the audit trail, and there is no second log.

| shape | fields | notes |
|---|---|---|
| job verbs | `job` | `STARTJOB`, `FORCE_STARTJOB`, `KILLJOB`, `ON_ICE`, `OFF_ICE`, `ON_HOLD`, `OFF_HOLD`, `ON_NOEXEC`, `OFF_NOEXEC` |
| `SET_GLOBAL` | `name`, `value` (both non-empty strings) | |
| `CHANGE_STATUS` | `job`, `status`, optional int `exit_code` | injected as `STATUS`, keeping overwrite parity |

Response: `{"ok": true, "kind": "<EventKind>", "at": "<iso8601>"}`.

Job arguments are validated against the catalog — vendor `sendevent`
errors on an unknown job rather than queueing it. `CHANGE_STATUS` carries
one deliberate exception (SEM-07): `"JOB^INST"` where `INST` is a declared
`insert_xinst` is a legal target even though no such job is in the
catalog. Overwriting the store's pseudo-entity is exactly how an operator
satisfies a cross-instance atom the sandbox cannot see. Other job verbs
stay catalog-only — starting or killing a ghost is meaningless.

`status` must be one of the oracle's `JobStatus` values; the refusal
lists them.

## 4. Query verbs (frozen response shapes)

Every query is a **pure projection**. None mutates, none inserts a store
row. They are safe to serve from the engine's task because `feed()` never
yields, so a handler can never observe a half-applied event.

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

## 5. Streaming verb: subscribe

`{"cmd": "subscribe", "since": <int>?}`. A subscription **owns its
connection** until hangup, so a client opens a separate connection for it.

The server answers `{"ok": true, "subscribed": true}`, then streams
journal records (`docs/runner-design.md` §7 record kinds) as raw lines. A
run with no journal is refused with `{"ok": false, "error": "this run has
no journal"}`.

Delivery guarantees, exactly as implemented:

- seq'd records (`input`, `advance`) are **exactly once** across the
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

1. **No version handshake** (§2).
2. **No authentication or authorization.** The socket's `0600` mode plus
   filesystem ownership is the entire access-control model. Any process
   running as the invoking user has full `sendevent` authority. This is
   the §12 RBAC non-goal made concrete.
3. **Unix-domain only.** No network transport, so the single-engine
   guarantee rests on a local `bind()`. A non-local controller would need
   both a transport and a replacement for that guarantee.
4. **Errors are prose, not codes.** Fine for a human at a terminal and for
   the TUI; a programmatic client cannot branch on them reliably.
