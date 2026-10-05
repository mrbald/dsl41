# Gateway — HTTP and WebSocket in front of the control socket

Status: **proposed.** Not normative until a decision-log entry accepts it.
Nothing is built. The control error codes it maps (§3) are built (DL-272).

The gateway puts one engine behind a corporate proxy. It adds no meaning:
[control-protocol](control-protocol.md) and [access-model](access-model.md)
stay the contracts.

## 1. Process model

- One gateway process per exposed tier, read and ops, each under its own
  OS service account (`svc-dsl41-gw-read`, `svc-dsl41-gw-ops`). Adm has no
  socket verbs ([access-model §1](access-model.md#1-the-model)), so it has
  no gateway.
- The gateway is a client of `control.sock`, never in-process with the
  engine. The engine's perimeter tells tiers apart by kernel credential;
  an in-process gateway would bypass the per-tier OS account
  ([runner-design §11](runner-design.md#11-ui--one-textual-app-terminal-and-web-e3)).
- The estate is armed group-open: each gateway account is in
  `socket_group` ([access-model §8](access-model.md#8-filesystem-modes))
  and the role map grants it its tier, as for the web tier
  ([access-model §9](access-model.md#9-the-web-tier)). Otherwise the
  gateway refuses to start (§7, criterion 11).
- The proxy authenticates the browser and routes the session to its
  tier's gateway (§7). Every route is mounted in every tier: the engine's
  perimeter is the only authority, not a second route table.
- An optional extra, `dsl41[gateway]`; the core keeps its three runtime
  dependencies. Intended stack: FastAPI on Starlette, on uvicorn.

## 2. Resources

One route per control `cmd`. The set is the `cmd` column of the verb table
([access-model §10](access-model.md#10-the-verb-table)). The gateway stamps
`"v": 3` and `cmd` on every upstream request.

| `cmd` | route | arguments |
| --- | --- | --- |
| `status` | `GET /api/v1/status` | `job`, optional |
| `trace` | `GET /api/v1/trace` | `since`, optional integer |
| `explain`, `spec`, `deps` | `GET /api/v1/explain` (and `/spec`, `/deps`) | `job` |
| `timers`, `plan` | `GET /api/v1/timers`, `GET /api/v1/plan` | none |
| `global`, `globals` | `GET /api/v1/global`, `GET /api/v1/globals` | `name`; `names`, repeated |
| `hosts` | `GET /api/v1/hosts` | `ids`, repeated, optional |
| `subscribe` | `GET /api/v1/subscribe`, WebSocket upgrade (§5) | `since`, optional integer |
| `sendevent`, `host`, `seal` | `POST /api/v1/sendevent` (and `/host`, `/seal`) | JSON body |

- `GET /api/v1/health`, the one route without a `cmd`, touches no engine
  state. It runs criterion 11's socket check and answers `503` when that
  check would refuse, `200` otherwise.
- Arguments are query-string parameters, never path segments: names can
  hold `/`, `#` and `^`. Each converts to the JSON type control names
  (`since` an integer, a repeated parameter a list of strings). A value
  that does not convert, or an unknown parameter, is refused.
- A `POST` body is the control request as an `application/json` object.
  A body naming `cmd`, `v` or `claimed_actor` is refused, not overwritten:
  identity belongs to the gateway's layer (§6). Every other field passes
  through unchanged, `request_id`, `expect`, `epoch` and `baseline_id`
  included. Validation stays with the engine.
- `request_id` must be a canonical UUID version 4. Every human of a tier
  shares one principal, so a guessable id could collide with another
  person's command (control-protocol §3). Other forms are refused.
- `routes`, specified and not built (control-protocol §3), gets
  `GET /api/v1/routes` when built.
- `POST /api/v1/seal` commits an existing stage. Staging writes under the
  run root, so it stays an adm act on the host
  ([access-model §10](access-model.md#10-the-verb-table)). The adm hands
  the browser the staged `next_period` and `stage_digest` out of band.
- A success is `200` with the control answer as the body, unchanged. The
  read header (`baseline_id`, `epoch`, `applied_index`) stays in the body,
  and no HTTP header copies it: two copies could disagree.

**OpenAPI is the contract artifact.** An OpenAPI 3.1 document is checked
in; a test fails when the generated one differs. Schemas follow
control-protocol [§3](control-protocol.md#3-mutating-verbs-sendevent-host-seal)
and [§4](control-protocol.md#4-query-verbs-frozen-response-shapes) and
allow unknown fields; prose covers the WebSocket route.

**Evolution.** `/api/v1` changes only by addition. A removed or changed
route, shape, gateway code or close code cuts `/api/v2`. A gateway release
speaks one control version; a newer engine answers `unsupported_version`
until the gateway is upgraded. Acceptance adds a row to
[protocol-evolution §1](protocol-evolution.md#1-the-matrix).

## 3. Errors

Every `ok: false` answer becomes `application/problem+json`
([RFC 9457](https://www.rfc-editor.org/rfc/rfc9457)). `type` is
`about:blank`, `title` the reason phrase, `detail` the control `error`
prose; clients branch on `code`. Every other answer field is an extension
member: `code`, the markers, `index`, `request_id`, `original_decision`,
the read header.

**Mutations: the markers decide the class.** For `sendevent`, `host` and
`seal`, the outcome table of
[control-protocol §3](control-protocol.md#3-mutating-verbs-sendevent-host-seal)
rules: `refused: true`, `decision: "rejected"`, or neither, which is
unknown. The code picks the status inside the class:

1. A known `code` maps by the table below.
2. An answer with no `code` maps from its marker alone: refused to `400`,
   rejected to `409`, unknown to `500`. A decision recorded before codes
   existed is replayed without one.
3. An unknown outcome always maps to a 5xx. If its code maps below `500`,
   the gateway answers `502`. An unknown outcome may have applied.
4. A collision's nested `original_decision` belongs to the earlier
   command. Its own fields never set this response's status.

A 4xx says this request was not admitted, or a decision went against it.
A refused retry says nothing about its original, which may have applied
(DL-217). **Queries: the code alone picks the status.** A query's marker
is not consulted; without a known code its error is `500`.

The code table of [control-protocol.md](control-protocol.md) holds classes
and client actions (DL-272). The HTTP column is the gateway's;
the control protocol stays transport-neutral:

| HTTP | codes |
| --- | --- |
| 400 | `invalid_argument`, `unknown_verb`, `unknown_status` |
| 403 | `access_denied` |
| 404 | `unknown_job`, `unknown_host` |
| 409 | `baseline_mismatch`, `request_id_reused`, `stale_epoch`, `host_quarantined`, `host_evicted`, `host_already_evicted`, `host_not_quarantined`, `host_no_deadman`, `host_never_contacted`, `eviction_bound_pending`, `seal_retry_mismatch`, `seal_in_flight`, `no_lineage`, `nothing_staged`, `seal_input_after_cutoff`, `seal_not_quiescent`, `seal_run_unaccounted`, `seal_retry_horizon`, `clock_regressed`, `seal_refused` |
| 412 | `precondition_failed` |
| 422 | `plan_cycle`, `status_not_injectable`, `force_needs_actor`, `stage_digest_mismatch` |
| 428 | `expect_required` |
| 500 | `internal_error`, `engine_error` |
| 502 | `peer_unauthenticated`, `malformed_request`, `unsupported_version`, `unknown_cmd` |
| 503 | `lineage_lost`, `period_sealing`, `engine_shutting_down`, `seal_not_settling`, `seal_supervisor_unproven` |
| 504 | `decision_timeout`, `seal_timeout` |

An unknown code on a mutation maps by rules 2 and 3. `no_journal` and
`backfill_refused` arrive only as stream frames (§5); `stale_completion` is
stored only. `eviction_bound_pending` carries no `Retry-After`: the engine
states the wait in prose only. The four `502` codes are the gateway's own
identity or framing, which a browser cannot cause.

**Gateway codes** use the same member, with a `gateway_` prefix.

| code | HTTP | class | when |
| --- | --- | --- | --- |
| `gateway_bad_request` | 400 | refused | the body, its media type or a parameter fails §2's rules |
| `gateway_unauthenticated` | 401 | refused | the proxy's trusted headers are missing (§7) |
| `gateway_forbidden` | 403 | refused | an Origin or CSRF check fails (§7) |
| `gateway_not_found`, `gateway_method_not_allowed` | 404, 405 | refused | no such route, or the wrong method |
| `gateway_request_too_large` | 413 | refused | a size limit is exceeded (§7) |
| `gateway_rate_limited` | 429 or 503 | refused | a per-client limit (`429`) or the global upstream cap (`503`); carries `Retry-After` |
| `gateway_upstream_unavailable` | 503 | refused | the connect to `control.sock` failed before any write |
| `gateway_outcome_unknown` | 502 or 504 | unknown | the exchange failed after a write was attempted: EOF, a torn answer, a framing failure (`502`), or the gateway's own deadline (`504`) |

A refused gateway problem carries `refused: true`. The unknown one carries
no marker: a failure after a write attempt is `delivered` (control-protocol §2).

## 4. Retries and isolation

- **`request_id` passes through**, created by the browser. The gateway
  never rewrites one and retries nothing: the retry policy is the browser's.
- **One upstream connection per HTTP request**, shared with nothing,
  read with `LINE_LIMIT`, and dropped after its answer or any error
  ([control-protocol §6](control-protocol.md#6-client-obligations)).
- **A browser disconnect cancels nothing upstream**, its own request
  included. The gateway reads the answer and logs it (§6); a cancel would
  only lose the outcome.
- **Deadlines are ordered engine < gateway < proxy, per route**: the
  engine's are `DECISION_TIMEOUT_S` (5 s) and `SEAL_TIMEOUT_S` (180 s),
  above many proxy defaults. After its own `504` the gateway still reads
  and logs the late answer.

**Seal and roll.** After a committed seal the engine exits, and the next
period's engine answers the seal's exact retry, one seal back
(control-protocol §3). A physical roll opens a new run root: the gateway
is repointed at its `control.sock` and restarted. Until then
`gateway_upstream_unavailable`, like `period_sealing` before the exit,
refuses that request only and says nothing about the seal.

**Browser obligations.**

1. Save the `request_id` and the pins (`expect`, `epoch`, `baseline_id`;
   a seal's whole body) before the first send, where a reload keeps them.
2. For a mutation the body decides, never the status. Only a `200`
   answer or a problem with a marker is a known outcome. All else after
   the send is unknown: a proxy's `502` or `504`, a network error.
3. After an unknown outcome, re-read and retry only under the same id and
   pins (control-protocol §3, "Recovering a lost answer"). A refused retry
   says nothing about the original, which may have applied (DL-217).
4. Retry a seal whose answer was lost until an engine gives a known
   outcome. On the engine's refusal, stop and re-read; never re-send a
   seal the engine refused. A `gateway_` refusal is not the engine's.

## 5. The stream

`GET /api/v1/subscribe` upgrades after §7's checks and limits, before the
upstream connect: a browser hides handshake statuses from script. Each
browser socket gets its own upstream `subscribe`, with its `since`.

- **Passthrough.** Each complete upstream line is one text frame, byte for
  byte; the gateway parses no record and drops a torn last line
  ([control-protocol §6](control-protocol.md#6-client-obligations)). An
  upstream refusal, as the only line or after the ack, is a frame followed
  by `4002`, never an HTTP status.
- **Upstream.** The gateway reads lines up to `4 × LINE_LIMIT`
  continuously, so the engine-side backlog stays near empty. It is still
  subject to the engine's budget
  ([control-protocol §5](control-protocol.md#5-streaming-verb-subscribe),
  DL-267); a removal ends the stream with `4002`.
- **A bounded queue per browser**: a configurable byte budget, at least
  `4 × LINE_LIMIT`, for frames not yet handed to the transport. A frame
  enters an empty queue; otherwise only if it fits (DL-267's rule). It
  also covers the backfill, so a slow browser can be closed mid-backfill.
- **Overflow** closes that browser and its upstream with `4001`, only.
- **Resume** with `since` set to the last `seq` the browser received, or
  the ack's `since` when it received none. Frames in flight at a close are
  lost, so the browser's count is the cursor. `seq`'d records arrive
  exactly once; others may repeat, and a `decision` dedups by `index`.
- **The gap marker** passes through unchanged and means retained history
  is missing ([control-protocol §5](control-protocol.md#5-streaming-verb-subscribe)).
  The gateway never creates one, for overflow or otherwise.
- The gateway pings within the proxy's idle timeout. The stream is
  one-way: an inbound data frame closes it with `1008`.

| close code | meaning | the browser's next move |
| --- | --- | --- |
| `4001` | this browser fell behind the gateway's queue budget | resume with `since` |
| `4002` | the upstream connect failed, or the upstream stream ended: EOF, a removal by the engine, or after an `ok: false` frame | read the last frame's `code`; otherwise resume with `since` after a delay |
| `4003` | an upstream line exceeded `4 × LINE_LIMIT` | alert; resuming at the same cursor meets the same line |
| `4004` | the browser's session ended or reached the maximum socket lifetime (§7) | authenticate again, then resume with `since` |
| `1001`, `1006` | the gateway is shutting down; an abnormal close, observed by the browser and never sent | resume with `since` after a delay |
| `1008` | the browser sent a data frame | fix the client; do not loop |

## 6. Identity

With a map armed, the engine overwrites `claimed_actor` with the gateway's
account ([access-model §3](access-model.md#3-local-authentication-kernel-peer-credentials)).
The WAL names `os/svc-dsl41-gw-ops`; a perimeter receipt names realm `os`,
principal `svc-dsl41-gw-ops`. **Neither names the human: a known limit of
this version.** Per-human attribution by the engine needs the deferred
seam `web-session-principal-v2`
([access-model §9](access-model.md#9-the-web-tier),
[§11](access-model.md#11-non-goals-and-deferred-seams)).

- **The gateway access log** joins a human to a command. It is
  append-only, one line per request: time, proxy user, tier, route,
  `request_id`, outcome class, `code`, and `index` when present; no body
  or credential. It is kept at least as long as the WAL segments it joins.
- The join is by `request_id` to WAL `input` and `decision` records, so it
  covers journaled mutations only: perimeter receipts carry no id, read
  admissions are not receipted, and queries carry none. The engine
  attests none of the log.
- The engine cannot revoke one human inside a tier. The proxy stops
  routing the session, and the gateway closes its sockets (§7).

## 7. Security acceptance criteria (requirements, not findings)

1. **The proxy contract.** The proxy validates the browser's credential
   and sends three configured trusted headers, stripping browser copies:
   user, session id, session expiry. The gateway trusts them only because
   only the proxy reaches it (criterion 5), and refuses any request or
   upgrade without them, except `/api/v1/health`.
2. **Origin allow-list** on every upgrade and `POST`; a missing or
   unlisted `Origin` is refused. No CORS access by default.
3. **CSRF for cookie auth.** A cookie-authenticated `POST` carries a
   header token derived from the session id with a gateway-held key,
   checked before forwarding.
4. **No tokens in URLs or logs**, the WebSocket URL included. Logs scrub
   `Authorization`, `Cookie` and `Set-Cookie`, and hold no body: a
   `SET_GLOBAL` value or a `spec` block can hold a secret.
5. **Backend reachable only through the proxy**: a Unix socket whose mode
   admits only the proxy's account when the proxy is on the host;
   otherwise TLS with a client certificate the gateway checks. Bare
   loopback TCP is not enough
   ([access-model §9](access-model.md#9-the-web-tier)).
6. **Limits.** Concurrent WebSockets, in-flight requests and request rate
   are capped per proxy user, with a global upstream cap. They apply
   before forwarding, so they also bound a client's rate of synced denial
   receipts at the engine.
7. **Size limits** on the body (never above `LINE_LIMIT` once encoded as a
   control line), the headers and inbound WebSocket messages.
8. **TLS for the browser terminates at the proxy.**
9. **Session end closes sockets**, with `4004`: at the named expiry, when
   the proxy closes its connection, or at a configured maximum lifetime.
   Revocation reaches an open socket by one of these.
10. **`Cache-Control: no-store`** on every response: read is
    disclosure-grade ([access-model §1](access-model.md#1-the-model)).
11. **Fail closed.** The gateway refuses to start when its effective uid
    is 0. Before each upstream connect it checks that it does not own
    `control.sock` and that the socket mode has group bits; a failure
    refuses the request as `gateway_upstream_unavailable` and is logged.
    A socket missing between periods is the same refusal, not a start
    failure. An unarmed or owner-run gateway would admit every verb.

## 8. Out of scope

The web application and its seal flow, several engines per gateway, HA,
and log file serving, which access-model §9 grants to the TUI only.

## 9. Acceptance tests

On real sockets and a real engine, behind a stub proxy; browser-driven
WebSocket tests run in Chromium and WebKit. The reference client is a test
fixture that implements §4's browser obligations and §5's resume.

1. Each `cmd` has one route, each route but `health` a `cmd`, and the
   generated OpenAPI equals the checked-in one.
2. `request_id`, `expect`, `epoch` and `baseline_id` reach the engine
   unchanged; an exact retry is answered from the original decision with
   no second index. A non-UUID id and a `claimed_actor` field are refused.
3. Each mutation code maps to its status. A decision timeout, an internal
   error and an EOF after the write answer a 5xx with no marker.
   Code-less answers map by rule 2, and an unknown code by rules 2 and 3.
   A collision ignores its
   `original_decision`. A query's `unknown_job` is `404`, `plan_cycle`
   `422`, a code-less query error `500`. No control code starts with
   `gateway_`. The reference client reads a bodiless proxy `504` as
   unknown.
4. A mutation whose upstream dies after the write applies once and
   answers `gateway_outcome_unknown`. Of two browsers in flight, one
   disconnects: the other gets its full answer, and the first one's
   mutation completes and is logged, as is a late answer after a `504`.
5. A stalled browser closes with `4001`, also mid-backfill, while another
   keeps receiving. Its resume delivers each `seq`'d record once, with no
   gap marker, and the reference client drops a repeated `decision`. A
   rolled root's gap marker passes intact. An upstream refusal and an
   engine EOF close with `4002`, an over-long line with `4003`, an inbound
   data frame with `1008`.
6. Missing trusted headers, an unlisted `Origin` on a `POST` and on an
   upgrade, a cookie `POST` without its token, and an oversized body,
   header or message are refused before any upstream connect. Per-user
   and global limits refuse. An expired session closes with `4004`. Every
   response carries `no-store`. A log scan finds no credential.
7. With a map armed, an ops mutation is journaled with the gateway's `os/`
   account, and its access log line joins the WAL decision by
   `request_id`.
8. A read-gateway `POST` answers `403` `access_denied`; the perimeter
   journal holds the denial and the WAL is untouched. A deployment test
   maps each account to its tier. Criterion 11 refuses root at start, and
   per request an owner-run gateway, a socket without group bits, or none.
9. An adm-staged seal commits, and one slower than the mutation window
   still returns the engine's answer. A seal whose answer is dropped,
   retried after a physical roll with the gateway repointed, answers `200`
   `applied` from the record.
