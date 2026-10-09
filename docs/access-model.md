# Access model — three tiers at the perimeter

Status: **frozen (DL-231; design DL-146, amended by DL-147, DL-148, DL-149,
DL-150, DL-151, DL-152, DL-158, DL-256, DL-272, DL-275, DL-311
and DL-314).** This
document is the design of record for `runner_access.py`, the control-plane
gate and the served web TUI. A change to a frozen item requires a
decision-log entry, as in `docs/control-protocol.md`. The authentication
half of the web session (§9) is a named seam, not a frozen item.

`docs/runner-design.md` §1 and §12 leave access control to this document.
This document closes the authorization half of control-protocol §7 gap 2.
It closes the authentication half for local peers only. The web session
keeps a named seam (§9).

## 0. The problem

Without an access map, the control socket's `0600` mode is the whole
access-control model (control-protocol §7 gap 2). Any process that runs as
the invoking user has full `sendevent` authority. The envelope's actor
field is named `claimed_actor` because it is a claim. It is a note in the
log, never an authorization. Operators need three grades of access: look,
operate and administer.

## 1. The model

There are three tiers, strictly nested: **read < ops < adm**. They are one
enum, not a flag matrix.

- **read**: every query verb, the journal stream and the observer TUI.
  Read discloses data; it is not harmless. `spec` shows job commands,
  `globals` shows runtime values, `subscribe` streams raw WAL records, and
  job logs can hold credentials. Grant it deliberately.
- **ops**: read, plus every mutating socket verb. That is every
  `sendevent` verb, `host` activate, drain and evict, and `seal`. Ops is
  destructive by design: `FORCE_STARTJOB`, `CHANGE_STATUS`, forced
  eviction and `force_seal` are in it (DL-146). There is no fourth
  "break-glass" tier and no per-verb deny overlay. Either would be a
  second policy axis and a sparse flag matrix. Break-glass is a receipt
  category (§6), not an authorization dimension.
- **adm**: ops, plus configuration. Every configuration surface is on the
  filesystem: profiles, timezone maps, anchors, the role map, retention,
  and `estate prune` and `reclaim`. So adm has **no socket verbs of its
  own**. Over the wire, adm and ops admit the same set. The tier exists in
  the map and the model for two reasons: a mapping can say what a
  principal *is*, and a future adm-grade verb has a home.

A **principal** is `(realm, name, groups)`. The realm names the identity
source: `os` for a peer the kernel authenticates, anything else for a
future asserted source (§9). Realms keep a web user named `root` from ever
matching the OS user `root`.

## 2. The boundary

Guarded: the control socket (`control.sock`) and the served web TUI.

Not guarded, by ruling:

- **A local CLI with filesystem access to the estate root is adm by
  definition.** Anyone who can read the WAL and write the spool needs no
  socket. The CLI verb tables below are therefore *semantic* tiers: they
  say what a verb means, and they enforce nothing. The exemption covers
  the filesystem path only. A CLI request that arrives through
  `control.sock` passes the same gate as every other client (§5). The
  gate knows no client identity, only the peer credential.
- **`supervisor.sock` is governed but not tiered** (DL-146). It keeps
  both controls of supervisor-protocol §5: owner-only `0600` mode, and the
  same-uid peer-credential check on every accept. That check refuses a
  peer uid that differs from the owner's. It admits a peer for which the
  platform supplies no uid; there the `0600` mode is the whole boundary.
  §3 does not copy that fallback. The kernel supplies the credential and
  enforces the mode; the supervisor makes the comparison. Either way the
  socket is owner-only. The local owner is adm by definition (previous
  bullet), and adm contains every lower tier. This holds when the run root
  opens to `0710` traversal too (§8). `supervise shutdown` can kill every
  managed command, and it stays an owner-only act. It may later move
  behind the same gate; nothing in this model blocks that.

## 3. Local authentication: kernel peer credentials

At connection accept, the engine reads the peer's credentials from the
kernel: `SO_PEERCRED` on Linux, `LOCAL_PEERCRED` on macOS. The gate uses
`runner_supervisor.peer_uid` for the uid and adds the following:

- It maps the uid to a passwd name, then to groups through
  `getgrouplist`, on both platforms. macOS's `xucred.cr_groups` truncates
  at 16 groups, and NSS is the source of the map's `group:` names. A
  failed credential read, a uid with no passwd entry, or a `getgrouplist`
  error is a refusal, never an unexplained EOF. The refusal carries code
  `peer_unauthenticated` (DL-272). One narrowing is deliberate: a gid with
  no group name is dropped from the group set, not refused. A nameless
  gid can match no named `group:` row.
- The principal is fixed at accept time. It does not change for the life
  of the connection. A change to OS groups takes effect on reconnect;
  that is an administrative fact, not a reload defect (§7).
- **No credential: the connection is refused** when access control is
  configured, with the same code `peer_unauthenticated`. The supervisor's
  `None` fallback is not copied; the gate fails closed.

`claimed_actor` stays a wire field but carries no authority. When access
is configured, the server **overwrites** the actor with the canonical
authenticated spelling (`os/<name>`) before anything is fingerprinted or
logged. The seal fingerprint contains the actor (`boundary.SealRequest`),
so the invariant is precise: the fingerprint carries *identity*, never
*tier*. A role-map edit changes no fingerprint, so an admitted retry still
matches its original attempt. Admission is a separate question. Every
request, a retry included, is decided under the current policy (§5, §7).
A principal whose tier dropped below ops is denied at the perimeter before
the retry route is reached. A different authenticated principal that
retries someone else's boundary mismatches by design. The existing
re-read-and-re-decide path answers it. When access is not configured, the
claim passes through untouched, byte for byte. One transition corner is
named. Arming or disarming access **between** a seal attempt and its retry
changes the actor spelling. The retry then mismatches the committed
stand-in and takes the same re-read-and-re-decide path. This is an
operational note for the arming runbook, not a wire change.

## 4. The role map

The role map is one file, in strict TOML, with one predefined resolver. It
is the single mapping seam: every identity source ends here.

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
2. Otherwise the highest tier among the matching `group:` rows wins.
3. Otherwise `unmapped` applies. `unmapped = "deny"` is the default.
   `unmapped = "read"` is the one legal relaxation, and it discloses data
   (§1). This section is the document of record for that choice.

Validation refuses each of these: a duplicate subject, an unknown field,
an unknown tier, a wildcard, a subject without a realm, a `format_version`
that is not the integer 1, an `unmapped` other than deny or read, a
`socket_group` that is not a non-empty string, a `binding` that is not an
array, a row that does not hold exactly `subject` and `tier`, a map over
the loader's 1 MiB ceiling (DL-149), and a `socket_group` this host does
not know. The loader resolves the named group to a gid and carries the
gid on the loaded policy. So the group is looked up exactly once, and an
unusable group refuses like any other unusable field (DL-152).

The loader opens the file without following symlinks. It opens it
non-blocking, so a FIFO refuses instead of stalling startup. It checks
four predicates. It checks the parent before the open, and the file on
the opened descriptor:

- The file is a regular file owned by the engine's own effective uid. Any
  other owner is refused, root included. The map speaks for this engine,
  so this engine must own it. A root engine owns its map as uid 0.
- The file is not group- or other-writable. The owner-write bit itself is
  not required.
- The parent is not a symlink, and it is owned by root or by the engine's
  own uid. Path resolution at open enforces that the parent is a
  directory.
- The parent is not group- or other-writable. Otherwise an ops-tier user
  could swap the map by renaming another file over it.

One residual is named. The loader does not walk the ancestors ABOVE the
parent. Place the map under a root-owned path, such as `/etc` or the
estate owner's home, and not under a world-writable tree. The map lives
outside the sealed estate artifacts: it is policy, not evidence.

**Configured and absent are explicit states.** With no `access_map`
configured, the zero-config model stands: socket `0600`, owner-only, and
no change for such estates. With `access_map` configured and the file
missing, unreadable or invalid, **startup refuses**. On reload, the old
policy stays and the refused candidate gets a best-effort failure
receipt. Descriptor I/O errors and parser errors of every kind are
wrapped into the same refusal (DL-149), so every loader failure on
reload attempts a receipt. A configured path never falls back to owner-wide authority.

The preflight refusal writes nothing. Before the run root is claimed or
the WAL opened, the CLI runs the same loader that arming runs, read-only.
`socket_group` resolution is part of that load (DL-152). Arming validates
the map again after the root is claimed. A failure at that point still
refuses, but the claimed root and its WAL already exist. Such a failure
can come from the second validation, from journal recovery, from the
sync of the arming receipt, or from the group grant. The receipt trail
differs by failure:

- A failure of the second validation writes no receipt.
- An existing `perimeter.jsonl` that this engine cannot read refuses at
  recovery, before any new record. It does not restart the key series
  (§6).
- A failed receipt sync can leave no, partial or complete
  `policy_loaded` bytes, and no policy stands.
- A group-grant failure comes after the synced `policy_loaded` line, and
  no failure record follows that line. A `policy_loaded` line alone does
  not prove that the engine went on to serve under that policy.

The group grant is not transactional. A failure can leave some of the
children already re-moded, or the root re-grouped while still `0700`.
Startup then refuses without rolling anything back. The child pass
applies exact modes: `0700` for directories and `0600` for files. Group
and other bits never widen, but owner bits can change: a `0400` file
gains owner write. The pass follows a symlink child to its target,
wherever that points. Both are the owner's own artifacts, inside a root
that was `0700` until that moment.

## 5. The enforcement point

The gate sits in `ControlServer._handle`, before the `cmd` split. That is
the one place both `_respond` and `_subscribe` pass through. `subscribe`
owns its connection and skips `_respond`, for the same reason as the
version check (DL-90). Nothing reaches `Engine.submit` unauthorized. The
engine, the oracle and the journal hold no authorization logic.

Two doors precede the gate, in this order. The line must decode to a JSON
object, and the request must name `v: 3` (control-protocol §2). A
malformed or wrong-version request is answered before classification. It
reaches neither the policy nor the perimeter journal.

Per request:

1. Take the connection's fixed principal (from accept).
2. Take one immutable policy snapshot (§7) with its generation number.
3. Classify `cmd` against the closed table (§10). An unknown or unlisted
   `cmd` is denied. The verb inside `sendevent` or `host` is deliberately
   NOT a second classification axis. There is one gate, and the
   dispatcher owns verb validity (DL-145 defect 2). The verb appears in
   the receipt label only.
4. Compare the granted tier with the required tier.
5. Denied: write a perimeter receipt (§6) and answer `ok: false,
   refused: true, code: "access_denied"`, with prose naming the tier gap.
   A denial consumes no engine index and advances no engine time. The
   denial carries no read header, like every answer sent before routing:
   the malformed line, the version refusal and the credential refusal.
   The header is stamped only on the answer to a request that passed
   routing and the lineage proof (control-protocol §2, DL-148). A
   perimeter denial is sent before either.
6. Admitted: stamp the authenticated principal and continue to the
   existing dispatcher unchanged.

Enforcement is outside the wire contract. It adds no envelope field, no
v4, no dialect event and no tombstone. Refusals reuse the existing
`ok: false, refused: true` vocabulary.

## 6. Receipts: the perimeter journal

Access decisions never enter the engine WAL. A denial is not an engine
input, and replay must not see policy. The decisions go to a separate
append-only journal:

```text
<run_root>/perimeter.jsonl
```

Every record carries `rec` (the kind), its own `access_seq` (never the
engine index) and `at`. `at` is UTC wall time, ISO-8601 at seconds
precision. `at` orders nothing; `access_seq` is the order. The rest of
the schema depends on the kind:

- **Decision records**: `access_denied`, `privileged_admitted` and
  `stream_revoked`.
  - `privileged_admitted` is written for every admitted request that
    required ops or higher. It is the break-glass ledger. It records a
    pass through the perimeter only. The engine's own decision on that
    request is the WAL's to record. So a verb that the dispatcher then
    refuses still shows a perimeter admission here.
  - Fields: `realm`, `principal`, `action`, `required_tier`,
    `granted_tier`, `policy_generation` and `policy_digest`.
  - `principal` is the unqualified name; the realm is in `realm`.
  - `required_tier` is null for a `cmd` outside the table. `granted_tier`
    is null when resolution denies.
  - `action` is one bounded label, `cmd` or `cmd:verb`, with each part
    truncated at 64 characters. A non-string `cmd` is recorded as
    `<non-string-cmd>`, never stringified.
  - Request bodies, global values and JIL are never recorded.
- **Policy records**: `policy_loaded` and `policy_reload_failed`. They
  carry no principal and no action, because policy has neither.
  - `policy_loaded` carries `generation`, `digest` and `bindings` (the
    binding count).
  - `policy_reload_failed` carries `generation` (the installed generation,
    which stays active) and `error`. It also carries
    `orphaned_generation` whenever the `policy_loaded` write reported
    failure.
  - That `policy_loaded` line may or may not have landed: an fsync can
    fail after the bytes are out. Seq adjacency decides whether a line is
    void, because the generation is process-local and an older
    incarnation may have used the same number. A failed write attempt
    still consumes its `access_seq`. If a complete `policy_loaded` record
    with the named generation exists at the failure's `access_seq - 1`,
    that record is void. If none exists there, no complete line is void.
  - A later successful reload may reuse the same generation number,
    because a failed reload does not consume it. That later line stands
    (§7).
- The §9 web records join this list when that seam lands.

Durability depends on the kind, and each rule is deliberate:

- `access_denied` is synced before the refusal is answered. A storage
  failure still denies. The decision fails closed; the receipt does not
  gate it.
- `policy_loaded` is sync-gated. A policy that cannot be receipted does
  not arm and does not install (§7).
- `policy_reload_failed` is best effort: it is written synced and the
  result is ignored. It reports a failure that keeping the old policy has
  already answered.
- `privileged_admitted` is best effort and unsynced. The admission stands
  when the receipt write fails. The WAL is the authoritative record of
  what the engine then did. Gating every ops verb on receipt storage would
  turn a full disk into a total operations outage while reads stayed
  open. The corroborating ledger is best effort by ruling.
- `stream_revoked`: the revocation is mandatory and its receipt is best
  effort. The receipt is written synced before the close, and the result
  is ignored. A stream that lost read closes whether or not the record
  lands.

`access_seq` is journal-wide. It continues across engine restarts: the
writer recovers it from the last complete record and makes a best-effort
attempt to heal a torn tail. So complete records carry strictly
increasing seqs across restarts in normal operation. Every write attempt,
whatever its kind, allocates its seq before the I/O. A reported failure
has two cases:

- No complete line landed. The number is a gap once a later write
  succeeds. A restart can reissue it only if no later complete record
  carries a higher seq.
- The line landed complete, and only the I/O after it failed: the fsync,
  the parent-directory fsync that a pending name owes (DL-151,
  DL-314), or the close. The record exists and carries its seq. The void
  rule above exists for exactly this case, and it depends on this
  allocation order.

Recovery reads the last complete record. An unreadable file refuses
arming (§4). A readable file with no complete record starts the series at
zero, so the next append issues 1, as in a fresh journal. One corner is
accepted. If the heal fails and the next append succeeds, that record
joins the torn fragment. Recovery cannot read it, so a later incarnation
can reissue its seq. Where a reader meets a reissued seq, physical file
order breaks the tie.

The policy `generation` is process-local: arming starts every incarnation
at 1. Across restarts, the `policy_digest`, not the generation, identifies
the policy a record was decided under. Decision records carry both. The
digest is `sha256:` plus the SHA-256 hex of the exact map bytes the
loader read.

**Evolution and lifetime** (collected in the `docs/protocol-evolution.md`
§1 matrix):

- The discriminator is the record's `rec` kind.
- Evolution is additive. A writer may add fields, and a reader must
  ignore fields it does not know. An incompatible change takes a NEW kind
  name, as the WAL does for record kinds.
- Unknown kinds are skipped, not refused. No engine dispatches this
  journal: seq recovery reads only `access_seq`, and every other reader
  reads it as an audit trail. So there is no per-record version field to
  refuse on.
- There is no physical roll. The journal is one append-only file for the
  life of its run root, and it is pruned only with that root. Truncating
  it in place would restart `access_seq` and forge duplicate keys.
- The writer trusts the path it owns. It opens `perimeter.jsonl` without
  the map loader's symlink and FIFO checks. A synced append that CREATES
  the file fsyncs the run root, so the name is durable before the receipt
  it gates is answered (DL-151). An unsynced create does not fsync it,
  because its own bytes are not durable either. It leaves the name
  pending, and the first synced append after it fsyncs the run root
  (DL-314). A directory fsync that fails leaves the name pending
  too, so the next synced append tries again. A writer that opens an
  existing file also starts with the name pending, because an earlier
  process may have created it unsynced. Its first synced append is the
  arming receipt, so the name is durable before the engine serves, and
  arming is refused if that fsync fails. Each recovery, heal and append
  resolves the name again. The run root has been owner-only since before
  arming (§8), so anything planted at that name is the owner's own act. The one-file
  lifetime holds only while the owner keeps the name bound to the same
  regular file.
- The journal is outside the period-model §12 lineage floor. No replay
  reads it, and nothing in the lineage reaches it.
- Which roots may be pruned, and when, is the same business decision as
  every other retention choice (`deployment-runbook.md` §2a). The act is
  adm.

## 7. Reload and revocation

Policy is an immutable snapshot with a generation number. Reload is
explicit: write a temp file, fsync it, rename it over the map, and send
`SIGHUP`. The engine arms its `SIGHUP` handler before it binds
`control.sock` (DL-275). So a `SIGHUP` sent once the socket answers is a
reload. A socket file alone proves nothing. A crashed run can leave one
behind, and before the handler is armed the signal keeps its default
action, which stops the process.

Install is receipt-gated, in this order: validate the complete candidate,
sync the `policy_loaded` receipt, then install the snapshot. A policy
change that cannot be receipted does not happen, and the old snapshot
stays active. "Complete" means everything the descriptor holds. The
loader reads to EOF, up to a 1 MiB ceiling past which it refuses
(DL-149). So a short read cannot install a valid-TOML prefix. The
temp-fsync-rename procedure keeps the file stable during the read.

Each of these keeps the old snapshot and attempts `policy_reload_failed`
(best effort): a refused candidate, a changed `socket_group` (below), a
failed `policy_loaded` write, a descriptor I/O error during the read, and
a parser error of any kind. The last two are wrapped into the refusal
(DL-149). Reload does not raise. Startup refuses with a configured but
invalid map, or with one whose arming receipt cannot be synced (§4).

`socket_group` is fixed at arming: the name AND the gid it resolved to
(DL-152). A reload is refused whole (`policy_reload_failed`) when it
names a different group, or when its group was renumbered under this
engine. The kernel side of the grant cannot follow a map edit, and a
half-applied change is worse than a restart. When the `policy_loaded`
write reports failure, the failure receipt names the
`orphaned_generation`. That receipt is itself best effort. The line may
have landed complete before its fsync or close failed. Seq adjacency
decides whether a line is void (§6), never a scan for the generation. A
later successful reload may reuse the number, and its line stands (§6).

Connections are **kept** across reload:

- A request is decided under the snapshot current at its admission. The
  next request sees the new policy. An admitted request finishes under its
  original decision; closing connections could not revoke admitted work
  either.
- `subscribe` has no next request. So reload re-evaluates every live
  stream under the new policy and closes exactly those that lost read. It
  attempts a best-effort `stream_revoked` receipt for each (§6).
- OS group changes take effect on reconnect (§3). Forcing reconnects is a
  separate administrative act, not part of reload.

## 8. Filesystem modes

The run root is forced to `0700` (`runner_startup`), so a `0660` socket
alone is unreachable. Traversal of the parent must be granted
deliberately. Arming has two modes, and the map chooses:

- **Armed, owner-only** (`socket_group` absent). The gate, the receipts
  and the actor overwrite are all live. Every mode stays as in the
  zero-config model: `0700` root, `0600` socket, children untouched.
  Nobody but the owner reaches the socket. The perimeter is an audit and
  policy layer for the owner's own connections, and a staging step
  before a group grant.
- **Armed, group-open** (`socket_group` named). The run root becomes
  `0710` with group `socket_group`. The loader resolves the group to its
  gid (§4), and arming applies it here (DL-152). The group gets
  execute-only traversal and no listing. `control.sock` becomes `0660`.
  Its owner stays the run-root owner, and its group becomes
  `socket_group`. The `0700` root was the fence for its children:
  `logs/` and `runs/` are created under the process umask, `0755` by
  default. So arming first **tightens every direct child to owner-only**:
  `0700` for directories and `0600` for files. These are exact modes:
  group and other bits never widen, owner bits can change, and for a
  symlink child the target changes (§4). Later artifacts land inside
  those directories. A test asserts that nothing but the socket is
  group-accessible after arming. `supervisor.sock` stays `0600` (§2).
- Access not configured. Everything stays as in the zero-config model:
  `0700` root, `0600` socket. No gate and no actor overwrite is active,
  and no new perimeter receipt is attempted (§4). A `perimeter.jsonl`
  left by an earlier armed incarnation stays where it is, for the life of
  the root (§6).

Further rules:

- Sockets are created owner-only (`0600`, from the umask at bind). The
  group is set next, and the final mode is applied last. No socket is
  ever group-readable before it carries its group. The socket directory
  is never group-writable.
- The receipt journal (`perimeter.jsonl`, §6) is created owner-only, with
  mode `0600` before the umask, which can only narrow it. The
  child-tightening pass of group-open arming also forces an existing
  journal to `0600`. Owner-only arming leaves the mode of an existing
  file alone. The perimeter never adds group or other permissions to the
  journal. The child pass can restore owner bits (`0400` to `0600`, §4).
- Opening to a group changes exactly three things: the direct children
  tighten to owner-only, the root takes the group and `0710`, and the
  socket takes the group and `0660`. Every further grant, above all log
  visibility for a web tier (§9), is the operator's explicit act, never
  the perimeter's.

## 9. The web tier

The web tier is **one serve instance per tier**. Each exposed tier gets
one `textual-serve` under its own OS service account
(`svc-dsl41-web-read`, `svc-dsl41-web-ops`). The corporate proxy
authenticates the browser, with PAM, LDAP, Entra, client certificates or
whatever the estate runs. It routes the session to the tier's instance.
The engine sees an OS peer like any other, under the same map and the
same gate. There is no new channel to spoof, and the proxy integration
needs no core code.

Consequences:

- Each web backend must be unreachable except through the proxy. On a
  shared host, bare loopback TCP is not enough. Bind the backend where
  only the proxy can reach it.
- The TUI child reads log files directly (`runner_tui`), and §8 keeps
  those files owner-only. So log visibility for a web tier is an explicit
  deployment grant. Either give the tier's service account read access to
  the log destinations (a group on the log files in the run directories,
  or external `std_out_file` paths it may read), or grant nothing. With
  no grant, the TUI's log panes refuse, and status, trace and the rest
  still serve. The perimeter never widens logs itself. The OS account is
  the containment; grant per tier.
- Receipts identify the tier's service account, not the human in the
  browser. That loss is accepted; the proxy's own log records the human.
  The deferred seam is named **`web-session-principal-v2`**: a
  per-session principal asserted by a broker over an explicitly trusted
  channel (reference design in DL-146). No code anticipates it.

## 10. The verb table

The table is closed, and the default is deny. A dispatcher `cmd` without
a row here is a test failure: the completeness gate diffs the dispatcher
against this table. A `cmd` outside the table is denied at runtime.
Classification is by `cmd` alone (§5). The perimeter admits any verb
string inside a listed `cmd`, and the DISPATCHER then refuses an unknown
one. These are two refusals from two owners. A dispatcher refusal after a
perimeter admission still leaves its `privileged_admitted` receipt (§6).
So the verb column below is the dispatcher's accepted set, listed here
for the tier it falls under. It is not a second classification axis.

| cmd | verb | tier |
| --- | --- | --- |
| `status`, `trace`, `explain`, `spec`, `deps`, `timers`, `plan`, `global`, `globals`, `hosts`, `subscribe` | — | read |
| `sendevent` | the externally injectable verbs (control-protocol §3): `STARTJOB`, `FORCE_STARTJOB`, `KILLJOB`, `ON_ICE`/`OFF_ICE`, `ON_HOLD`/`OFF_HOLD`, `ON_NOEXEC`/`OFF_NOEXEC`, `DISARM` (DL-158), `RELEASE_RESOURCE` (DL-256), `SET_GLOBAL`, `CHANGE_STATUS`. The internal EventKinds (`STATUS`, `TIMER`, the alarms) have no wire door: the dispatcher refuses them like any unknown verb | ops |
| `host` | `activate`, `drain`, `evict` (forced included) | ops |
| `seal` | normal and `force_seal` | ops |

`quarantine` and `reinstate` are leader-only inputs, not operator verbs
(control-protocol §3). They have no tier because they have no external
door.

CLI semantic tiers (the only enforcement is filesystem access, and these
tiers enforce nothing, §2):

- Read-shaped: `query *`, `host list`, `supervise list`, `journal`,
  `runs`, `verify`, offline `rehearse`, and every pure-compiler verb.
- Ops: `sendevent`, `release-held` (a sweep of `sendevent OFF_HOLD`
  requests, so ops by composition; DL-311), `host
  activate`/`drain`/`evict` and `supervise shutdown`.
- Adm: `run`, `serve`, `audit` (it writes attestations and registry
  state), `seal` (it stages C2 files before the ops verb; staging may
  later be split from committing), `estate prune`, `estate reclaim`, and
  `supervise start` (it creates the run root and writes the supervisor
  log there; DL-311).

`supervise shutdown` is ops-shaped: it operates and configures nothing.
Its only door is the owner-`0600` supervisor socket (§2). So the
enforcement (owner = adm by definition) exceeds the tier the verb needs.
Adm contains ops, so there is no contradiction.

`ui` has no single tier. Its panes are reads and its console sends ops
verbs, so each request meets the table above on its own.

In the TUI, a read session may show the mutating console disabled as a
courtesy. The server's refusal is the authority either way.

## 11. Non-goals and deferred seams

- No fourth tier and no per-verb exceptions (§1).
- No LDAP, OIDC or PAM client in core, ever. Integration is the proxy's
  job.
- No plugin API. The seams are the role map and, later, the asserted
  principal. They are protocol seams, not loader seams.
- `web-session-principal-v2` (§9): per-session web identity.
- The supervisor socket under the gate: possible later (§2).
- Splitting CLI `seal` staging (adm) from committing (ops) (§10).

## 12. Test obligations

Tests are named `test_access_*`. These obligations hold (DL-146):

1. Zero-config estates: no behavior change anywhere. The whole existing
   suite is the fixture.
2. Configured but invalid map: startup refuses. Reload keeps the old
   policy and attempts the failure receipt (best effort, §6).
3. Resolution order: a user row beats group rows, the highest group wins,
   `unmapped` applies last, and realms never cross-match.
4. Gate coverage: every dispatcher `cmd` has a row (the completeness
   gate), `subscribe` is gated, and a denial consumes no engine index.
5. Denied mutation: `refused: true`, the receipt synced before the
   answer, and the WAL untouched. A receipt write that fails still denies
   (§6).
6. `privileged_admitted` is attempted for every ops admission. A failed
   write does not block the admission (§6).
7. Reload: connections survive, and the next request sees the new policy.
   A live subscribe stream that lost read closes, and its
   `stream_revoked` receipt is attempted (revocation mandatory, receipt
   best effort, §6).
8. Modes: armed group-open gives `0710`/`0660`, with an owner-only WAL
   asserted. Armed owner-only (no `socket_group`) leaves `0700`/`0600`
   unchanged with the gate live. Unconfigured leaves `0700`/`0600`
   unchanged.
9. Peer credential absent with access configured: the connection is
   refused.
10. With access configured, the authenticated spelling replaces the claim
    in every journaled record and in the seal fingerprint. A role-map edit
    changes no fingerprint: an ADMITTED retry survives reload, while
    admission itself is decided under the current policy (§3: a lost tier
    means a denied retry). Without access, actor bytes pass through
    unchanged.
11. `runner_access.py` is under CI's 100% branch-coverage gate
    (`[tool.coverage.report]`). A test exercises every refusal arm; no
    arm is only claimed.
