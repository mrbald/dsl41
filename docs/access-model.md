# Access model — three tiers at the perimeter

Status: **draft (2026-08-22, DL-146).** Designed in a three-way round: the
user's constraints, one Claude sketch, one codex-sol sketch, two adversarial
rounds to convergence; a post-build conformance round (codex-sol,
2026-08-22) folded by DL-147. Once frozen, each change to a frozen item requires a
decision-log entry, the same rule as `docs/control-protocol.md`. This document
retires the RBAC non-goal of `docs/runner-design.md` §0/§12 and closes the
authorization half of control-protocol §7 gap 2. The authentication half
closes only for local peers; the web session keeps a named seam (§9).

## 0. The problem

The control socket's `0600` mode is the entire access-control model today
(control-protocol §7 gap 2). Any process running as the invoking user holds
full `sendevent` authority. The envelope's actor field is named
`claimed_actor` because it is a claim: a breadcrumb in the log, never an
authorization. Operators want three grades of access: look, operate,
administer.

## 1. The model

Three tiers, strictly nested: **read < ops < adm**. One enum, no flag
matrix.

- **read** — every query verb, the journal stream, the observer TUI.
  Read is disclosure-grade, not harmless: `spec` exposes job commands,
  `globals` exposes runtime values, `subscribe` streams raw WAL records,
  job logs can carry credentials. Grant it deliberately.
- **ops** — read, plus every mutating socket verb: all `sendevent` verbs,
  `host` activate/drain/evict, `seal`. Ops is destructive by design:
  `FORCE_STARTJOB`, `CHANGE_STATUS`, forced eviction and `force_seal` are
  in it (user ruling, 2026-08-22). There is no fourth "break-glass" tier
  and no per-verb deny overlay — that would be a second policy axis and a
  sparse flag matrix. Break-glass is a receipt category (§6), not an
  authorization dimension.
- **adm** — ops, plus configuration. Every configuration surface today is
  filesystem: profiles, timezone maps, anchors, the role map, retention,
  `estate prune`/`reclaim`. So adm has **no socket verbs of its own** —
  over the wire, adm and ops admit the same set. The tier exists in the
  map and the model so that a mapping can say what a principal *is*, and
  so future adm-grade verbs have a home.

A **principal** is `(realm, name, groups)`. The realm names the identity
source: `os` for kernel-authenticated peers, anything else for a future
asserted source (§9). Realms prevent a web user named `root` from ever
matching the OS user `root`.

## 2. The boundary

Guarded: the control socket (`control.sock`) and the served web TUI.

Not guarded, by ruling:

- **Local CLI with filesystem access to the estate root is adm by
  definition.** Anyone who can read the WAL and write the spool needs no
  socket. This is documented, not fought. The CLI verb tables below are
  therefore *semantic* tiers — what the verb means — not enforcement.
- **`supervisor.sock` is governed but not tiered** (v1 ruling): it
  stays owner-`0600` — kernel-enforced, owner-only, and the local owner
  is adm by definition (previous bullet), which contains every lower
  tier — including when the run root opens to `0710` traversal (§8).
  `supervise shutdown` can kill every managed command; it remains an
  owner-only act. A later version may put it behind the same gate;
  nothing in this model blocks that.

## 3. Local authentication: kernel peer credentials

At connection accept, the engine reads the peer's credentials from the
kernel: `SO_PEERCRED` on Linux, `LOCAL_PEERCRED` on macOS. The precedent
is `runner_supervisor.peer_uid`. The access gate extends it:

- uid → passwd name → groups via `getgrouplist`, on both platforms —
  macOS's `xucred.cr_groups` truncates at 16 and NSS is already the
  source the map's `group:` names come from. Any resolution error is a
  refusal, never an unexplained EOF.
- The principal is fixed at accept time and is immutable for the life of
  the connection. Changing OS groups requires reconnect; that is an
  administrative fact, not a reload defect (§7).
- **No credential → refuse the connection** when access control is
  configured. The supervisor's `None` fallback is not copied; the gate
  fails closed.

`claimed_actor` survives as a wire field but loses all weight: when
access is configured, the server **overwrites** the actor with the
canonical authenticated spelling (`os/<name>`) before anything is
fingerprinted or logged. The seal fingerprint contains the actor
(`boundary.SealRequest`), so the invariant is stated precisely: the
fingerprint carries *identity*, never *tier*. A role-map edit changes no
fingerprint — an admitted retry still matches its original attempt.
Admission itself is a separate question: every request, a retry
included, decides under the current policy (§5, §7), so a principal
whose tier dropped below ops is denied at the perimeter before the
retry route is reached. A different authenticated principal
retrying someone else's boundary mismatches by design and is answered by
the existing re-read-and-re-decide path. When access is not configured,
the claim passes through untouched (byte-compatible). One transition
corner is named: arming or disarming access **between** a seal attempt
and its retry changes the actor spelling, so the retry mismatches the
committed stand-in and falls to the same re-read-and-re-decide path.
An operational note for the arming runbook, not a wire change.

## 4. The role map

One file, strict TOML, one predefined resolver. This is the single
mapping seam: every identity source ends here.

```toml
format_version = 1
unmapped = "deny"              # "read" is the only other legal value
socket_group = "dsl41-control" # optional: the OS group that may reach
                               # the socket; absent = owner-only armed
                               # mode (ss8)

[[binding]]
subject = "group:os/dsl41-observers"
tier = "read"

[[binding]]
subject = "user:os/alice"
tier = "adm"
```

Resolution, in order:

1. An exact `user:` row wins over every group row.
2. Otherwise the highest tier among matching `group:` rows wins.
3. Otherwise `unmapped` applies. `unmapped = "deny"` is the default;
   `unmapped = "read"` is the one legal relaxation, and it is
   disclosure-grade (§1) — the doc of record for that choice is this
   section.

Validation refuses: duplicate subjects, unknown fields, unknown tiers,
wildcards, a subject without a realm. The loader opens the file without
following symlinks (and non-blocking: a FIFO refuses instead of parking
startup) and checks four predicates — the parent before the open, the
file on the opened descriptor:

- the file is a regular file owned by the engine's own uid — root
  included is refused: the map speaks for this engine, so this engine
  must own it;
- the file is not group- or other-writable (the owner-write bit itself
  is not required);
- the parent is no symlink (path resolution at open enforces that it
  is a directory) and is owned by root or the engine's own uid;
- the parent is not group- or other-writable — otherwise an ops-tier
  user could swap the map by renaming over it.

One residual is named: ancestors ABOVE the parent are not walked, so
place the map under a root-owned path (`/etc`, or the estate owner's
home) rather than under a world-writable tree. The map lives outside
the sealed estate artifacts; it is policy, not evidence.

**Configured vs absent is explicit.** No `access_map` configured: today's
model stands — socket `0600`, owner-only, nothing changes for zero-config
estates. `access_map` configured but the file is missing, unreadable, or
invalid: **startup refuses**; on reload, the old policy stays and a
receipt records the failure (§7). A configured path never silently falls
back to owner-wide authority.

## 5. The enforcement point

The gate sits in `ControlServer._handle`, before the `cmd` split — the
one place both `_respond` and `_subscribe` pass through (`subscribe`
owns its connection and skips `_respond`; DL-90 already taught this
lesson for the version check). Nothing reaches `Engine.submit`
unauthorized. The engine, oracle and journal stay authz-free.

Per request:

1. The connection's immutable principal (from accept).
2. One immutable policy snapshot (§7) with its generation number.
3. Classify `cmd` against the closed table (§10). Unknown or unlisted →
   denied. The verb inside `sendevent`/`host` is deliberately NOT a
   second classification axis (one gate — the dispatcher already owns
   verb validity, DL-145 defect-2's lesson); it rides in the receipt
   label only.
4. Compare granted tier with required tier.
5. Denied → perimeter receipt (§6), answer `ok: false, refused: true`
   with prose naming the tier gap. A denial consumes no engine index and
   advances no engine time.
6. Admitted → stamp the authenticated principal, continue to the
   existing dispatcher unchanged.

No envelope change. No v4, no dialect event, no tombstone: enforcement
is outside the wire contract, and refusals reuse the existing
`ok:false, refused:true` vocabulary.

## 6. Receipts: the perimeter journal

Access decisions never enter the engine WAL — a denial is not an engine
input, and replay must not see policy. A separate append-only journal:

```text
<run_root>/perimeter.jsonl
```

Every record carries `rec` (the kind), its own `access_seq` (never the
engine index) and `at`. The rest of the schema is per kind:

- **Decision records** — `access_denied`, `privileged_admitted` (every
  admitted request that required ops or higher — passed the PERIMETER — the break-glass ledger;
  the engine's own decision on that request is the WAL's to record, so
  a verb the dispatcher then refuses still shows a perimeter admission
  here), `stream_revoked`. Fields: `realm`, `principal`, `action`,
  `required_tier`, `granted_tier`, `policy_generation`,
  `policy_digest`. `action` is one bounded label — `cmd` or
  `cmd:verb`, each part truncated at 64 characters; a non-string `cmd` is
  recorded as `<non-string-cmd>`, never stringified. Request bodies,
  global values and JIL are never recorded.
- **Policy records** — `policy_loaded` (`generation`, `digest`, the
  binding count) and `policy_reload_failed` (`generation`, `error`,
  plus `orphaned_generation` when a landed `policy_loaded` line must
  not stand, §7). They carry no principal and no action: policy has
  neither.
- The §9 web records join this table when that seam lands.

Durability is per kind, and each rule is deliberate:

- `access_denied` — synced before the refusal is answered; a storage
  failure still denies. The decision fails closed; the receipt does
  not gate it.
- `policy_loaded` — sync-gated: a policy that cannot be receipted does
  not arm and does not install (§7).
- `policy_reload_failed` — best effort (written synced, the result
  ignored): it reports a failure already answered by keeping the old
  policy.
- `privileged_admitted` — best effort, unsynced. The admission stands
  when the receipt write fails: the WAL is the authoritative record of
  what the engine then did, and gating every ops verb on receipt
  storage would turn a full disk into a total operations outage while
  reads stayed open. The corroborating ledger is best-effort by
  ruling.
- `stream_revoked` — the revocation is mandatory, its receipt best
  effort: a stream that lost read closes whether or not the record
  lands.

`access_seq` is journal-wide: it continues across engine restarts (the
writer recovers it from the last complete record and heals a torn
tail), so the key stays unique for the journal's life. The policy
`generation` is process-local: arming starts every incarnation at 1.
Across restarts the `policy_digest` — not the generation — identifies
the policy a record was decided under; decision records carry both.

**Evolution and lifetime** (collected in the `docs/protocol-evolution.md`
§1 matrix): the discriminator is the record's `rec` kind. Evolution is
additive — a writer may add fields, a reader must ignore fields it does
not know; an incompatible change takes a NEW kind name, the same move
the WAL makes for record kinds. Unknown kinds are skipped, not refused:
no engine dispatches this journal (seq recovery reads only `access_seq`;
everything else reads it as an audit trail), so there is no per-record
version field to refuse on. There is no physical roll in v1: the
journal is one append-only file for the life of its run root, pruned
only with that root — truncating it in place would restart `access_seq`
and forge duplicate keys. It sits outside the period-model §12 lineage
floor (no replay reads it, nothing in the lineage reaches it). Which
roots may be pruned and when is the same business decision as every
other retention choice (`deployment-runbook.md` §2a); the act is adm.

## 7. Reload and revocation

Policy is an immutable snapshot with a generation number. Reload is
explicit: write a temp file, fsync, rename, `SIGHUP`. Install is
receipt-gated, in this order: validate the complete candidate, sync the
`policy_loaded` receipt, then install the snapshot — a policy change
that cannot be receipted does not happen, and the old snapshot stays
active. Any other failure keeps the old snapshot and writes
`policy_reload_failed` (best effort). Startup with a configured but
invalid map, or one whose arming receipt cannot be synced, refuses
(§4).

`socket_group` is fixed at arming: a reload that names a different
group is refused whole (`policy_reload_failed`) — the kernel side of the
grant cannot follow a map edit, and a half-applied change is worse than
a restart. When the `policy_loaded` line lands but its fsync fails, the
failure receipt names the `orphaned_generation` the landed line must
not stand for.

Connections are **kept** across reload:

- A request decides under the snapshot current at its admission; the
  next request sees the new policy. An admitted request finishes under
  its original decision — closing connections could not revoke admitted
  work either.
- `subscribe` has no next request, so reload re-evaluates every live
  stream under the new policy and closes exactly those that lost read,
  each with a `stream_revoked` receipt.
- OS group changes propagate on reconnect (§3). Forcing reconnects is a
  separate administrative act, not part of reload semantics.

## 8. Filesystem modes

The run root is forced `0700` today (`runner_startup`), so a `0660`
socket alone is unreachable — parent traversal must be granted
deliberately. Arming has two modes, chosen by the map:

- **Armed, owner-only** (`socket_group` absent): the gate, the
  receipts and the actor overwrite are all live, and every mode stays
  exactly as today (`0700` root, `0600` socket, children untouched).
  Nobody but the owner reaches the socket; the perimeter is an audit
  and policy layer for the owner's own connections, and a staging step
  before a group grant.
- **Armed, group-open** (`socket_group` named): run root `0710`, group
  = `socket_group` — execute-only traversal, no listing. `control.sock`
  becomes `0660`, owner unchanged (the run-root owner), group
  `socket_group`. The `0700` root was the
  fence for its children (`logs/` and `runs/` are born `0755`), so
  opening it first **tightens every direct child to owner-only** (dirs
  `0700`, files `0600`); later artifacts land inside those directories.
  A test asserts nothing but the socket is group-accessible after
  arming. `supervisor.sock` stays `0600` (§2).
- Access not configured: everything stays exactly as today (`0700`,
  `0600`), and no gate, receipts or overwrite exist at all (§4).
- Sockets are created with no access, ownership and group set, and the
  final mode applied last. The socket directory is never group-writable.
- Opening to a group changes exactly three things: the direct children
  tighten to owner-only, the root takes the group and `0710`, the
  socket takes the group and `0660`. Every further grant — log
  visibility for a web tier above all (§9) — is the operator's explicit
  act, never the perimeter's.

## 9. The web tier

v1 ships **per-tier serve instances**: one `textual-serve` under a
dedicated OS service account per exposed tier (`svc-dsl41-web-read`,
`svc-dsl41-web-ops`). The corporate proxy authenticates the browser —
PAM, LDAP, Entra, client certs, whatever the estate runs — and routes
the session to the tier's instance. The engine sees an OS peer like any
other; the same map, the same gate. No new channel exists, so nothing
can be spoofed, and corporate integration costs zero core code.

Consequences, stated plainly:

- Each web backend must be unreachable except through the proxy. On a
  shared host, bare loopback TCP is not enough; bind the backend where
  only the proxy can reach it.
- The TUI child tails log files directly (`runner_tui`), and §8 keeps
  those files owner-only. Log visibility for a web tier is therefore an
  explicit deployment grant: give the tier's service account read on
  the log destinations (a group on the run dirs' log files, or external
  `std_out_file` paths it may read) — or grant nothing, and the TUI's
  log panes refuse while status, trace and the rest still serve. The
  perimeter never widens logs itself. The OS account is the
  containment; grant per tier.
- Receipts identify the tier's service account, not the human in the
  browser. That loss is accepted for v1; the proxy's own log carries the
  human. The deferred seam is named **`web-session-principal-v2`**: a
  per-session principal asserted by a broker over an explicitly trusted
  channel (the codex round-1 sketch is the reference design). No v1 code
  anticipates it.

## 10. The verb table

Closed table, default deny. A dispatcher `cmd` without a row here is a
test failure (the completeness gate diffs the dispatcher against this
table), and a `cmd` outside the table is denied at runtime.
Classification is by `cmd` alone (§5): the perimeter admits any verb
string inside a listed `cmd`, and the DISPATCHER then refuses an
unknown one — two refusals from two owners, and a dispatcher refusal
after a perimeter admission still leaves its `privileged_admitted`
receipt (§6). The verb column below is therefore the dispatcher's
accepted set, listed here for the tier it rides under, not a second
classification axis.

| cmd | verb | tier |
| --- | --- | --- |
| `status`, `trace`, `explain`, `spec`, `deps`, `timers`, `plan`, `global`, `globals`, `hosts`, `subscribe` | — | read |
| `sendevent` | the externally injectable verbs (control-protocol §3): `STARTJOB`, `FORCE_STARTJOB`, `KILLJOB`, `ON_ICE`/`OFF_ICE`, `ON_HOLD`/`OFF_HOLD`, `ON_NOEXEC`/`OFF_NOEXEC`, `SET_GLOBAL`, `CHANGE_STATUS`. The internal EventKinds (`STATUS`, `TIMER`, the alarms) have no wire door: the dispatcher refuses them like any unknown verb | ops |
| `host` | `activate`, `drain`, `evict` (forced included) | ops |
| `seal` | normal and `force_seal` | ops |

`quarantine`/`reinstate` stay leader-only inputs, not operator verbs
(control-protocol §3); they have no tier because they have no external
door.

CLI semantic tiers (enforcement is the filesystem, §2): `query *`,
`journal`, `runs`, `verify`, offline `rehearse`, and every pure-compiler
verb are read-shaped; `sendevent`, `host`, `supervise shutdown` are ops;
`run`, `serve`, `audit` (writes attestations and registry state),
`seal` (stages C2 files before the ops verb — a later slice may split
staging from committing), `estate prune`, `estate reclaim` are adm.
`supervise shutdown` is ops-shaped — it operates, it configures
nothing — while its only door is the owner-`0600` supervisor socket
(§2), so the enforcement (owner = adm by definition) exceeds the tier
the verb semantically needs. Adm contains ops; no contradiction.

The TUI: a read session may render the mutating console disabled as a
courtesy; the server refusal is the authority either way.

## 11. Non-goals and deferred seams

- No fourth tier, no per-verb exceptions (§1).
- No LDAP/OIDC/PAM client in core, ever. Integration is the proxy's job.
- No plugin API. The seam is the role map and, later, the asserted
  principal — protocol seams, not loader seams.
- `web-session-principal-v2` (§9): per-session web identity.
- Supervisor socket under the gate: possible later, out of v1 (§2).
- Splitting CLI `seal` staging (adm) from committing (ops) (§10).
- Error codes on denials (control-protocol §7 gap 4 stands).

## 12. Test obligations

Tests are named `test_access_*`. The set that must exist before this
document freezes:

1. Zero-config estates: no behavior change anywhere (the whole existing
   suite is the fixture).
2. Configured-but-invalid map: startup refusal; reload keeps old policy
   and writes the receipt.
3. Resolution order: user row beats group rows; highest group wins;
   `unmapped` applies last; realms never cross-match.
4. Gate coverage: every dispatcher `cmd` has a row (completeness gate);
   `subscribe` is gated; a denial consumes no engine index.
5. Denied mutation → `refused: true`, receipt written and synced, WAL
   untouched.
6. `privileged_admitted` written for every ops admission.
7. Reload: connections survive; next request sees new policy; a live
   subscribe stream that lost read closes with `stream_revoked`.
8. Modes: armed group-open → `0710`/`0660` and owner-only WAL
   asserted; armed owner-only (no `socket_group`) → `0700`/`0600`
   unchanged with the gate live; unconfigured → `0700`/`0600`
   unchanged.
9. Peer credential absent with access configured → connection refused.
10. With access configured, the authenticated spelling replaces the
    claim in every journaled record and in the seal fingerprint; a
    role-map edit changes no fingerprint — an ADMITTED retry survives
    reload, while admission itself decides under the current policy
    (§3: a tier lost is a retry denied); without access, actor bytes
    pass through unchanged.
11. `runner_access.py` sits under CI's 100% branch-coverage gate
    (`[tool.coverage.report]`): every refusal arm is held, not asserted.
