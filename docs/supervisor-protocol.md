# Supervisor protocol — the lifecycle tier's public contract

Status: the spool format and the wrapper input spec are frozen
(2026-07-11, phase 11b, DL-42 item 3). The supervisor socket protocol is
frozen (2026-07-11, phase 11f, DL-48). This document is the future
extraction boundary. If the lifecycle tier (the four modules of §1) is
extracted (DL-42 triggers), this document is its
public API. Each change to a frozen item requires a decision-log entry.

The tier is deliberately dumb. It records process lifecycle facts durably
and does nothing else. It has no conditions, no retries, and no policy.
*(Amended by DL-150.)* The time bounds it does keep are lifecycle bounds
only: the lease TTL, the SHUTDOWN waits and the optional deadman (all
§5). None of them decides what runs. Scheduling semantics
live in the orchestrator (dsl41's oracle). Dashboards of meaning live in
the orchestrator's UI (DL-42 item 6).

## 1. Roles

- **wrapper** (`runner_wrapper.py`, phase 11b): the per-run shim and the
  direct parent of the command. It is the one process that cannot miss
  the exit status, and it writes the status durably. It is
  parent-agnostic: the engine (11b–11e) and the supervisor (11f) spawn it
  identically. It is stdlib-only, and an import test enforces this
  boundary.
- **supervisor** (`runner_supervisor.py`, phase 11f): keeps parenthood
  alive across engine restarts. It owns the wrapper lifelines. Thus an
  engine restart REATTACHES and does not kill the jobs (E4 dissolved). It
  speaks the §5 socket protocol (SPAWN/SIGNAL/LIST/SHUTDOWN/PING + lease
  verbs). It is stdlib-only and runs by file path — the same enforced
  boundary as the wrapper.
- **process identity** (`runner_procid.py`, DL-72): the one copy of the
  durability liturgy, the boot-session id, the (pid, start-time)
  PID-reuse guard and the quiet group kill that the two above share. It
  is a sibling *inside* the boundary: stdlib-only itself, imported by
  both under its plain top-level name, and covered by the same import
  test.
- **canonical form** (`canon.py`, DL-129): the one implementation of the
  §3.2 canonical form (`docs/period-model.md`) that the supervisor's three
  §3 records are written in and read back through. *(Added by DL-150.)*
  It is the second sibling inside the boundary, on the same terms:
  stdlib-only itself, imported by the supervisor under its plain
  top-level name, and covered by the same import test. The wrapper does
  not import it.

Extraction takes all four files or none.

## 2. Wrapper input spec (frozen)

The input is a single JSON object on the wrapper's stdin. After the
wrapper reads the object, it repoints stdin at /dev/null. The spawner
runs the wrapper **by file path** (`sys.executable <path>/runner_wrapper.py`),
never with `-m`. Thus the runtime imports of the wrapper stay
stdlib-only.

```json
{
  "version": 1,
  "run_id": "uuid4 string, from the decision that planned the SPAWN (DL-118); the spawner mints only on effect-less paths",
  "job": "job name",
  "run_number": 3,
  "command": "exact /bin/sh -c command line (profile already composed)",
  "run_dir": "/abs/path/runs/<job>.<run_number>",
  "lifeline_fd": 3,
  "stdout_path": "/abs/path (opened APPEND)",
  "stderr_path": "/abs/path (opened APPEND)",
  "stdin_path": null,
  "grace_seconds": 10.0
}
```

*(Amended by DL-129, at build of period-model §11a.)* `run_id` is checked
against a **filename-safe grammar** at the wire —
`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`, the
canonical uuid4 string form the adapter has always minted. It names a
directory entry now (§3), so anything else is refused before anything is
created. `run_dir` must be `<run_root>/runs/<job>.<run_number>`: the
supervisor owns that path, and one `run_id` maps to one `(job, run_number)`
maps to one directory, in both directions. The two are compared as **resolved
paths**, not as strings — the engine and the supervisor are told the run root
separately, and `./r`, `/abs/r` and a symlinked `/tmp/r` are one directory.

- `lifeline_fd`: the read end of a pipe. Its **write end lives in exactly
  one process — the spawner** (fd-hygiene invariant, leak-tested). EOF on
  this fd means that the parent died, `kill -9` included.
- `stdin_path: null` means /dev/null. Append on stdout/stderr is vendor
  parity (AutoSys appends to std_out_file/std_err_file).
- `grace_seconds`: the SIGTERM→SIGKILL escalation window for the
  parent-loss kill. The spawner reuses the same value for its own kills.

*(Amended by DL-150.)* The supervisor checks the whole object before it
writes anything durable (§5). Every key above is required except
`lifeline_fd`, which the supervisor fills. `version`, `run_number` and
`lifeline_fd` are integers and never booleans. `run_id`, `job`,
`command`, `run_dir`, `stdout_path` and `stderr_path` are strings.
`stdin_path` is a string or null. `grace_seconds` is a number, zero or
more. `job` names one directory component: it must not be empty, must not
hold a path separator, and must not be `.` or `..`. An **unknown key is
refused** (`bad_spec`). The object is frozen, so a key this list does not
name is a key whose type is not pinned either, and the receipt
fingerprint (§3) stays injective only over pinned types. This is the one
place in the protocol where an unknown field refuses rather than being
ignored: §5's forward-compatibility rule covers the fields of a REQUEST,
not the keys inside `spec`.

## 3. Spool format (frozen)

`spawn.json`, `status.json`, `receipt.json` and `reply.json` live in
`run_dir`. *(Amended by DL-150.)* The one file below that does not is the
`run_id` index entry, at `<run_root>/runs/.by_run_id/<run_id>`. Every
write uses the durability
liturgy: same-directory temp file, fsync(file), rename, fsync(directory).
`run_dir` must be on a **local** filesystem (rename-over-NFS has
ambiguous crash semantics). Each file is a single JSON object, with
sort_keys and one trailing newline. Consumers must ignore unknown fields
(forward compatibility). `version` increases only on an incompatible
change.

*(Amended by DL-129, at build of period-model §11a.)* Three files join the
spool, and the **supervisor** writes them, not the wrapper: `receipt.json`
and `reply.json` in `run_dir`, and the `run_id` index entry
`runs/.by_run_id/<run_id>`. Each is one object in the **§3.2 canonical form**
(`docs/period-model.md`) — UTF-8, keys sorted at every depth, no whitespace,
**no trailing newline** — written by the liturgy above, and each carries
`artifact_format_version`. The wrapper's own two files keep the format of
this section (`sort_keys`, one trailing newline) and are unchanged. A
detached run's directory is created by the supervisor on receipt; the engine
keeps that ownership only for tethered runs. *(Amended by DL-150.)*
`received_at` and `spawned_at` are aware-UTC ISO-8601 strings, like the
wrapper's timestamps. §3.2 governs the bytes of the object; it does not
reshape these two strings. A reader of these records checks that the field
is a string and does not parse its timestamp form.

### receipt.json — written by the supervisor BEFORE it forks the wrapper

```json
{"artifact_format_version": 1, "run_id": "…",
 "spec_fingerprint": "sha256:…",
 "received_at": "2026-08-20T12:23:55.123456+00:00"}
```

`spec_fingerprint` is sha256 over the §3.2 canonical form of the §2 input
spec with `lifeline_fd` removed (the supervisor fills that field, so a retry
carrying one must not read as a different spec). §3.2's value grammar has no
floats and `grace_seconds` is one, so a float is fingerprinted as the tagged
string `"float:" + float.hex()` — exact, and nothing but this fingerprint
reads it.

### reply.json — written by the supervisor after the fork

```json
{"artifact_format_version": 1, "run_id": "…",
 "wrapper_pid": 4242, "spawned_at": "2026-08-20T12:23:55.987654+00:00"}
```

The answer as first given. A replayed SPAWN is answered from this file.

### runs/.by_run_id/&lt;run_id&gt; — the run_id index

```json
{"artifact_format_version": 1, "run_id": "…", "job": "…", "run_number": 3}
```

The first durable *record* a SPAWN writes — the run directory itself is
made one step earlier (§5) — and the only route from a `run_id` back to
its directory. **"No index entry" means "first application"**, so
deleting one authorizes a spawn: it may not be pruned while the SPAWN effect
that names it can still be replayed (period-model §11a, §12). The directory
therefore grows one entry per run, and the retention floor is what bounds it.
Reading it is a single lookup by name; the whole directory is scanned in one
case only — an orphan run directory, to find whether another `run_id` already
claims it — and that case exists only after a crash.

### spawn.json — written by the wrapper immediately after spawning

```json
{
  "version": 1,
  "run_id": "…", "job": "…", "run_number": 3,
  "wrapper_pid": 4242,
  "wrapper_start_time": "lstart:Sat Jul 11 14:19:32 2026",
  "command_pid": 4243,
  "command_pgid": 4243,
  "command_start_time": "lstart:Sat Jul 11 14:19:32 2026",
  "boot_id": "D985983E-…",
  "started_at": "2026-07-11T12:23:55.123456+00:00"
}
```

- Start-time tokens are opaque strings: `ticks:<n>` on Linux (field 22 of
  /proc/pid/stat, tick-exact equality) or `lstart:<ps -o lstart= output>`
  on macOS (compare within ±2s, because ps rounds to whole seconds). **If
  the live token of a pid does not match the recorded token, never signal
  that pid** (PID-reuse guard, DL-41a item 5).
- `command_pgid == command_pid`: the command is its own process-group
  leader. The wrapper is deliberately NOT a member of this group. A group
  kill must never kill the recorder before the recorder writes its record
  (DL-41a item 2).
- `boot_id` (kern.bootsessionuuid / /proc/sys/kernel/random/boot_id): a
  mismatch with the current boot voids all liveness checks and proves
  that nothing survived (DL-42 item 5).
- Timestamps are aware-UTC ISO-8601.

### status.json — written by the wrapper before reaping

```json
{"version": 1, "run_id": "…", "job": "…", "run_number": 3,
 "outcome": "exited", "exit_code": 7,
 "ended_at": "2026-07-11T12:23:56.357872+00:00"}
```

Outcomes (exactly one per run — the file appears at most once):

| outcome       | extra fields        | meaning                                    |
|---------------|---------------------|--------------------------------------------|
| `exited`      | `exit_code`         | the command exited on its own              |
| `signaled`    | `signal`            | the command was killed by a signal that the wrapper did not send |
| `terminated`  | `cause`, `observed` | the wrapper killed the group (`cause: "parent lost"`, or a spawn-record write failure). `observed` carries the forensic exit detail |
| `spawn_failed`| `error`             | the wrapper could not open the command's stdin/stdout/stderr, or could not spawn /bin/sh (DL-150) |

The **absence** of status.json is the one state that the wrapper can
never produce for a command that ran. *(Amended by DL-150.)* It means one
of four things: the recorder itself was killed (-9); the machine died;
the wrapper refused the spec before it spawned anything (§4 step 6, exit
1 or 2); or the record write itself failed (exit 3). The first two are
the orchestrator's E7 unobservable case. The last two are the wrapper
saying, on stderr and in its exit code, that no run started or no record
survived. The orchestrator decides no outcome from the exit code — it may
quote it in the cause and nothing more. It reports every absence as
FAILURE `exit_status_unobservable` and never guesses.

Orchestrator mapping (dsl41's, recorded here as the reference consumer):

- `exited` → the raw exit_code through the SEM-09 boundary
- `signaled` and `terminated` → STATUS TERMINATED (a kill that actually
  happened)
- `spawn_failed` → STATUS FAILURE
- absence → STATUS FAILURE `exit_status_unobservable` (PENDING: E7)

### DSL41_RUN env tag — forensics only

The tag is base64url JSON `{"boot_id", "job", "run_id", "run_number"}` in
the command's environment. Never use it for identity decisions. macOS
KERN_PROCARGS2 omits env for restricted binaries (/bin/sh), and Linux
/proc/pid/environ is ptrace-gated (DL-41a item 5, probed empirically).

## 4. Wrapper behavior (frozen semantics)

1. The wrapper has its own session (`setsid`). The command is in its own
   pgid (`setpgid(0,0)` equivalent at spawn). The child restores the
   default signal dispositions pre-exec. SIG_IGN inherits across exec.
   Without the reset, the command ignores a graceful SIGTERM.
2. The wrapper ignores SIGTERM/SIGINT/SIGHUP/SIGQUIT. Only SIGKILL or
   machine death silences the recorder. This pins the residual crash
   matrix to the DL-41a accepted cases.
3. The event loop is a SIGCHLD self-pipe + select over {self-pipe,
   lifeline}. On every wakeup, the wrapper does the child-exit check
   BEFORE the lifeline-EOF check. Thus a completion that races parent
   death records as a completion.
4. On exit, the wrapper observes via waitid(WNOWAIT), writes status.json,
   and then reaps.
5. On lifeline EOF, the wrapper does the exit check again. Then it sends
   SIGTERM to the command pgid and waits the grace period. *(Amended by
   DL-150.)* It sends SIGKILL only to a command that is still alive at
   the end of that wait; a command that ends on the SIGTERM is observed
   and the wait stops there. Then it writes `terminated / parent lost`
   and exits.
6. The wrapper exit code is a notification only (0 = a status record
   exists, 2 = the spec's `version` is absent or is one the wrapper does
   not implement, 3 = a record write failed, for example ENOSPC).
   *(Amended by DL-150.)* A spec that is not readable JSON exits 1 before
   any record, and so does one that misses a key the wrapper reads with
   no default: `run_id`, `job`, `run_number`, `command`, `run_dir`,
   `lifeline_fd`, `stdout_path`, `stderr_path`. The wrapper's own two
   defaults are `stdin_path` (null, meaning /dev/null) and
   `grace_seconds` (10.0); the supervisor refuses either omission first
   (§2), so the defaults are reachable only by a direct spawner.
   status.json is the sole data channel, and its absence reads
   the same way whatever the exit code was.

## 5. Supervisor socket protocol (frozen — phase 11f, DL-48)

One supervisor exists per run_root. The named socket is
`<run_root>/supervisor.sock`, mode 0600, with a **same-uid peer-cred
check on every accept** (Linux SO_PEERCRED, macOS LOCAL_PEERCRED / struct
xucred). *(Amended by DL-150.)* The check refuses a peer uid that differs
from the supervisor's own. Where the platform supplies no uid at all, the
peer is admitted and the 0600 mode is the whole boundary. The access
perimeter deliberately does not copy that fallback: it fails closed
(`docs/access-model.md` §3). The supervisor also writes `<run_root>/supervisor.pid` (JSON:
`pid`, `boot_id`, `incarnation`, `started_at`). *(Amended by DL-150.)* It
logs to its own stderr and opens no log file. The spawner is what points
that stderr at `<run_root>/supervisor.log`. On
start, if a live supervisor already holds the socket (connect probe), the
supervisor refuses to run. It unlinks a stale socket — parity with the
engine's control-socket gate (runner-design §10).

Linux hardening: the supervisor sets `PR_SET_CHILD_SUBREAPER` (prctl 36)
at startup, best-effort. The supervisor never restarts itself. Survival
across ITS death is the job of Tier 2.

**Framing.** The protocol is JSON lines over `SOCK_STREAM`. One request
line → one response line, except async pushes (below). Every request
carries `"v": 1`. Responses are `{"ok": true, …}` or
`{"ok": false, "error": "<code>", …}`.

**Incarnation** (DL-80). The supervisor mints an `incarnation` id at every
start and returns it from `PING`, `LIST` and `ACQUIRE`. Every verb that
changes lease or run state must carry it — `SPAWN`, `SIGNAL`, `SHUTDOWN`,
`RENEW` and `RELEASE` (DL-150 names the last two); a mismatch is
`{"ok": false, "error":
"wrong_incarnation", "incarnation": <current>}`. The reason it is not
folded into the token: the fencing counter is in-memory, so a restarted
supervisor mints token 1 again, and a controller still holding a token
from the previous incarnation would match the new holder's token by
coincidence. The two refusals must also stay distinct, because they demand
opposite client behaviour — `wrong_incarnation` means the supervisor you
knew is gone and every wrapper it held died by lifeline, so re-acquire
**and** reconcile from the spool; `stale_token` means the supervisor is
the one you knew, so no lifeline was cut and there is nothing to
reconcile. *(Amended by DL-150.)* Beyond that, `stale_token` says only
that the token presented cannot mutate this incarnation. The lease may
have expired, may have been released, or may be held by someone else
under another token — the refusal does not say which, and it asserts
nothing about any run. A client may answer it with `ACQUIRE`, and the
shipped engine does: the supervisor grants a free or expired lease, and
refuses a live incumbent with `lease_held`. That refusal, not
`stale_token`, is what says the lease is somebody else's. The incarnation is
public (any reader gets it from `PING`); the token is the secret half.

The supervisor ignores unknown fields (forward compatibility). An
unknown verb → `unknown_verb`. A missing/wrong `v` →
`unsupported_version`. A malformed line → `malformed_json` (the stream
is not desynced).

*(Amended by DL-150.)* A blank or whitespace-only line is ignored and gets
no answer at all; every other line gets exactly one. A handler that raises
is answered `{"ok": false, "error": "internal: <type>: <message>"}` and the
supervisor keeps running. Its own death would EOF the lifeline of every
wrapper on the host, so one request may never end it.

**Read-only verbs** (any connection, no lease):

- `LIST` → `{ok, version: 1, supervisor_pid, boot_id, incarnation, deadman_s,
  lease: {holder,
  expires_at} | null, runs: [{run_id, job, run_number, run_dir, wrapper_pid,
  wrapper_alive, spawned_at, wrapper_rc}]}` — the response lists what THIS
  supervisor still holds. A supervisor restart implies that
  all prior wrappers received EOF and recorded. The spool is the
  cross-restart truth, and LIST shows this supervisor's in-memory state
  only. `wrapper_rc` is
  null while the wrapper is alive. *(Amended by DL-129, at build of
  period-model §11a.)* Every live run, plus a bounded window of the most
  recent completions: a completed entry may be evicted once its exit is
  recorded and pushed, because LIST was never the idempotency store. Older
  completions are read from the spool by the client; LIST itself never
  reads it. A SIGNAL for an evicted run answers
  `unknown_run`, exactly as it does for any run of an incarnation that has
  ended. *(Amended by DL-150.)* The `lease` field reports an UNEXPIRED
  lease, which is not the same as a live one (see the lease verbs below):
  it can still name a holder whose connection is gone. Nothing may read it
  as proof that a controller is watching.
- `PING` → `{ok, version: 1, incarnation, deadman_s}`.

`deadman_s` (S5b, DL-95) rides both read verbs: the interval this supervisor
was started with, or `null`. It is read back rather than assumed because a
reattaching engine meets a supervisor it did not start, and
`docs/concurrency-model.md` §8's eviction bound has to describe the host
rather than some engine's launch options. Additive; older clients ignore it
like any unknown field.

**Lease verbs** (single controller, observers are unlimited):

- `ACQUIRE {controller_id, ttl_s, token?, incarnation?}` → `{ok, token,
  expires_at, incarnation}`. *(Amended by DL-150.)* `controller_id` must
  be a non-empty string; anything else is
  `{ok: false, error: "bad_controller_id"}`, checked before `ttl_s` and
  before any lease state. `ttl_s` is optional on `ACQUIRE` and on `RENEW`,
  and defaults to 60 s. The supervisor puts no bound on it: a zero or
  negative value makes a lease that is already expired. `ACQUIRE` is the
  one lease verb that does not
  require the incarnation: a free lease is granted without one, and the
  incarnation is read only to test incumbency against a live lease.
  `token` is a monotonically increasing fencing integer. The counter is
  in-memory only: supervisor death kills all wrappers by lifeline, so the
  counter cannot regress while any spawned run is alive.

  A lease is **live** when it is unexpired *and* its holder's connection
  is still open. A live lease yields only to a claimant that presents both
  the **current token** and **this incarnation**; everyone else gets
  `{ok: false, error: "lease_held", holder, expires_at}`. The incumbent
  re-keys this way (a fresh token, the old one dies), which is how a
  reconnect after a poisoned connection fences anything the old
  connection had in flight.

  A lease whose holder's connection is **gone** is freely grantable even
  while unexpired. That is what lets a crashed engine's resume re-acquire
  without waiting out the TTL. *(Amended by DL-150.)* It is sound on a
  local AF_UNIX socket because EOF there has exactly two causes, and both
  end in the same place. The kernel closes the fd when the holder process
  is gone, `kill -9` included. The holder can also close it itself: the
  shipped controller poisons a connection whose reply may be in flight and
  reconnects on it. Either way the next `ACQUIRE` mints a fresh token, and
  the old token — the one a poisoned connection may still be carrying — is
  dead from that moment. So the branch keys on EOF and needs neither cause.

  `controller_id` authorizes nothing (DL-79). It is a label for `LIST`
  and for the `lease_held` refusal, and clients should make it unique per
  incarnation so those two reads name a specific controller. Until DL-79
  a *matching label* took a live lease, which was safe only because one
  run_root had one engine and the orchestrator's own control-socket bind
  enforced that on one machine.

  The token proves **incumbency, not authenticity** — it is a small
  monotone integer. Authentication is the same-uid peer-cred gate on
  accept; a same-uid process is already inside the trust boundary.

  **Constraint on any future non-local transport:** EOF stops being proof
  of death. A relay must not close the supervisor-side connection while
  its controller lives, or the orphan branch must become TTL-gated.
- `RENEW {incarnation, token, ttl_s}` → `{ok, expires_at}`.
  `RELEASE {incarnation, token}` → `{ok}`. *(Amended by DL-150.)* Both
  change lease state, so both take the same two-step check as the
  mutating verbs below: the incarnation first, then the token.
- Engine defaults: `ttl_s = 60`, with a renewal every 20 s. *(DL-150:)*
  this is the client's policy. The supervisor's own default for an absent
  `ttl_s` is the same 60 s.

**Mutating verbs** (these require `incarnation` and `token`; a foreign
incarnation → `{ok: false, error: "wrong_incarnation", incarnation}`,
checked first, and then a stale/expired token →
`{ok: false, error: "stale_token"}`):

- `SPAWN {token, spec}` — `spec` is the §2 frozen wrapper input spec, and
  `lifeline_fd` is the supervisor's to own and fill. *(Amended by
  DL-150.)* It is the one optional key: a `spec` that carries one is
  accepted, its value is replaced before the wrapper starts, and the
  receipt fingerprint (§3) ignores the field either way, so a retry
  carrying a stale fd does not read as a different spec. The write end lives
  in the supervisor ONLY. This is precisely the mechanism that detaches
  job lifetime from the engine. `run_id` doubles as the idempotency key.
  A replayed SPAWN with a known run_id spawns nothing and returns the
  original result plus `"duplicate": true`.
  → `{ok, run_id, wrapper_pid, spawned_at}`.

  *(Amended by DL-129, at build of period-model §11a.)* **The idempotency
  store is the run directory, not `self.runs`.** LIST must stay bounded on a
  root that never rolls, so completed entries leave memory — and an in-memory
  dedup turns a delayed duplicate SPAWN into a second execution the moment
  they do. On receipt the supervisor writes, in this order: `mkdir
  runs/<job>.<run_number>` — *(amended by DL-150)* the directory can exist
  already in one case only, the orphan the table's last row cleared for
  reuse, because the replay resolution runs first; the `run_id` index entry (§3) —
  **index before receipt**, because the first durable thing that names a run
  must be the thing every later lookup goes through; `receipt.json`,
  **before** the fork; the wrapper; `reply.json`; then the answer. A replay
  resolves the directory **through the index**, never through the incoming
  path, and answers from the directory, not from memory. *(Amended by
  DL-150.)* The incoming path is read in one case only — no index entry for
  this `run_id`. A receipt there for a DIFFERENT `run_id` is a `collision`.
  A receipt there for THIS `run_id` means the index was lost under a live
  tombstone: the directory answers, and it is never a first application,
  because losing an index must not authorize a second process. The table's
  last row is reached only when the path holds no receipt either:

  | directory state | answer |
  | --- | --- |
  | index, receipt with an equal `spec_fingerprint`, `reply.json` | duplicate: the original result fields from `reply.json` |
  | equal fingerprint, `spawn.json`, no `reply.json` | duplicate: `wrapper_pid` and `spawned_at := started_at` from `spawn.json` — equivalent, and said rather than promised |
  | the incoming path holds a receipt (or an index) for a different `run_id` | `collision` |
  | the same `run_id` against a different `(job, run_number)` | `collision` |
  | `receipt.json` with a different fingerprint | `collision` |
  | equal fingerprint, no `spawn.json`, wrapper alive | `in_progress` — no second spawn |
  | equal fingerprint, no `spawn.json`, nothing alive | `indeterminate` — the crash landed between receipt and fork; nothing may re-spawn, and the engine's E7 policy decides the run |
  | index entry → a directory with no `receipt.json` | `indeterminate` |
  | index entry → a directory that does not exist | impossible by write order; `indeterminate` if ever seen |
  | no index entry, no receipt, but a `spawn.json` or `status.json` | `indeterminate` — a directory from before this protocol, engine-made and receiptless; forking into it would overwrite the first run's records |
  | no index entry, no receipt, nothing else | first application — an orphan directory with neither is reused, because nothing durable names its run |

  The duplicate envelope is frozen: `{ok, run_id, wrapper_pid, spawned_at,
  "duplicate": true}`. Each new refusal is `{ok: false, error, detail}` with
  `error` ∈ {`bad_run_id`, `bad_spec`, `collision`, `in_progress`,
  `indeterminate`}; *(DL-150)* the `in_progress` refusal also carries
  `run_id`. `in_progress` is **retryable and not a completion**: the
  wrapper is alive, so a client must wait for its outcome rather than record
  a failure for a running process. `collision` and `indeterminate` are final,
  and the engine's E7 policy owns what the run then becomes. A failed
  `mkdir`, index write, receipt write or fork keeps the existing
  `{ok: false, error: "spawn_failed: <reason>"}`, and this tier ANSWERS such a
  failure rather than dying of it — its own death would EOF the lifeline of
  every wrapper it holds. *(Amended by DL-150.)* `reply.json` is the one
  exception, because it is written AFTER the fork: the wrapper is already
  running, so a failure there is logged and the SPAWN still answers
  `{ok, …}`. A replay then rebuilds that answer from `spawn.json`, which is
  the table's second row. Losing the run over its copy of the receipt is
  the one mistake this write order exists to avoid.
  Idempotency therefore outlives LIST presence and a
  supervisor **restart**: the entry is gone, and the directory answers.

  *(Amended by DL-150.)* **Absent means ENOENT and nothing else.** A record
  that exists and cannot be read — wrong permissions, a truncated write,
  bytes the §3.2 ingress refuses, a missing required field, an
  `artifact_format_version` this binary does not implement — is never
  absence, because absence authorizes a spawn. An unreadable index entry or
  `receipt.json` is `indeterminate`. An unreadable `reply.json` falls to the
  next row of the table, and every next row is safer than the one above it,
  so it can cost the answer detail but can never invent one. An index entry
  that names a `run_id` other than its own filename is `indeterminate`. In
  the orphan-directory scan, an index entry that cannot be read blocks
  reuse — it might claim this directory — and the answer is `collision`; a
  directory that cannot be listed is `indeterminate`. A
  `.<name>.<pid>.tmp` file left behind by an interrupted write is not a
  record and is skipped.
- `SIGNAL {token, run_id, sig}` with `sig` ∈ {`TERM`, `KILL`} — the
  supervisor compares the recorded command (pid, start-time) from
  `spawn.json` with the live process (the PID-reuse guard, reimplemented
  stdlib-side). Then it signals the command PGID, never the wrapper. Each
  call sends exactly one signal: the TERM→grace→KILL escalation stays
  engine-side (the oracle decides kills — the supervisor stays dumb). →
  `{ok}`, or `{ok, "noop": true}` for an already-dead or unverifiable
  group.

  *(Amended by DL-150, recording DL-83.)* There is a third answer:
  `{ok: false, error: "not_ready"}`. SPAWN answers as soon as the wrapper is
  forked, and the wrapper writes `spawn.json` a few syscalls later. A SIGNAL
  that lands in that window finds a live wrapper and no record. That is not
  an already-dead group, and it must not read as `noop`, or a kill decided
  milliseconds after a start is dropped. The wrapper is the discriminator:
  alive with no record is `not_ready` and means retry; exited with no record
  is `noop`, because nothing here can still be addressed. A `sig` outside
  {`TERM`, `KILL`} is `{ok: false, error: "bad_signal"}`, checked before
  `run_id`.
- `SHUTDOWN {token}` — orderly, the one exception to no-escalation (the
  engine is possibly gone). The supervisor sends TERM to each live
  command PGID, waits the per-run `grace_seconds`, and sends KILL to
  survivors. **Lifelines stay open until wrappers exit**, so wrappers
  observe the command deaths and record `signaled`/`exited` truthfully
  (never "parent lost"). The supervisor waits for the wrappers, replies
  `{ok}`, exits, and unlinks the socket + pidfile. SIGTERM/SIGINT also
  trigger this shutdown (Tier 2 / `supervise shutdown` fallback). Only
  SIGKILL (unhandleable) leaves the wrappers to their own EOF.

  *(Amended by DL-150.)* Two bounds hold that wait finite. First a wait of
  up to 5 s for a just-spawned wrapper's missing `spawn.json`: a command
  with no record cannot be signalled, and it would otherwise die by
  lifeline EOF alone and record "parent lost" (DL-48). Then, after the
  TERM, a wait of the longest per-run `grace_seconds` plus 2 s. Past that
  the supervisor sends one last KILL to every survivor's group, stops
  waiting, and answers. A wrapper still alive at that point loses its
  lifeline when the supervisor exits, and then runs §4 step 5 on its own:
  it records the command's own ending if the command has ended, and
  `terminated / parent lost` only if it has to kill it. The promise above
  is bounded, not absolute.

**Pushes.** When the supervisor reaps a wrapper, the connection that
holds the current lease receives async lines
`{"push": "exit", run_id, wrapper_rc, at}`. Pushes are NOTIFICATIONS
only — droppable, never the data channel. A disconnected controller
loses them. On reconnect, it recovers with LIST + status.json (the spool
is the truth, the same philosophy as the wrapper exit code).

The engine's OWN control socket (runner-design §10) deliberately keeps
no lease: sendevent is multi-writer by AutoSys nature, and the
single-writer engine loop serializes it. The lease guards the tier that
spawns without semantics.

**The deadman** (S5b, DL-95). Started with `--deadman-seconds N`, a
supervisor that has had **no live leaseholder** for N seconds stops its loop
and returns. Both halves of "live" from the lease definition above apply:
unexpired *and* the holder's connection still open — an expired lease whose
connection is open is a controller that stopped renewing, and an unexpired
one whose connection died is a controller that is gone. The clock restarts
whenever a live leaseholder appears, so a reconnecting engine reprieves it.

Its exit is the whole mechanism: the process dying EOFs every lifeline it
owns, and each wrapper then runs §4 step 5 — TERM, grace, KILL, record
`terminated / parent lost`. *(DL-150:)* step 5 checks the command first,
so a command that already ended records its own outcome instead.
That is the existing kill path, not a new one,
and the supervisor still decides nothing about what should run. Omitted, a
supervisor tolerates an absent controller forever, which is what lets an
engine crash and resume with its runs intact (DL-79).

It is here for one reason outside this tier: `docs/concurrency-model.md`
§8's `evict` — the only state that lets another host run work bound to this
one — must be provable, and nothing else bounds when a controller-less
supervisor's wrappers die. A run root without a deadman is never reroutable
except by force.

## 6. License earmark

*(Amended by DL-150.)* The four modules of §1 and this document are
earmarked Apache-2.0 on
extraction (LICENSING.md item 6). Until the extraction, do not add
per-file headers. Before CLA + relicense disclosure, do not accept
external contributions to earmarked files.
