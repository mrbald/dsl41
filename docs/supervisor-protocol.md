# Supervisor protocol — the lifecycle tier's public contract

Status: the spool format and the wrapper input spec are frozen
(2026-07-11, phase 11b, DL-42 item 3). The supervisor socket protocol is
frozen (2026-07-11, phase 11f, DL-48). This document is the future
extraction boundary. If the lifecycle tier (runner_wrapper.py +
runner_supervisor.py + the runner_procid.py they share, DL-72) is
extracted (DL-42 triggers), this document is its
public API. Each change to a frozen item requires a decision-log entry.

The tier is deliberately dumb. It records process lifecycle facts durably
and does nothing else. It has no timers, no conditions, no retries, and
no policy. Scheduling semantics live in the orchestrator (dsl41's
oracle). Dashboards of meaning live in the orchestrator's UI (DL-42
item 6).

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
  test. Extraction takes all three files or none.

## 2. Wrapper input spec (frozen)

The input is a single JSON object on the wrapper's stdin. After the
wrapper reads the object, it repoints stdin at /dev/null. The spawner
runs the wrapper **by file path** (`sys.executable <path>/runner_wrapper.py`),
never with `-m`. Thus the runtime imports of the wrapper stay
stdlib-only.

```json
{
  "version": 1,
  "run_id": "uuid4 string, minted by the spawner",
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

- `lifeline_fd`: the read end of a pipe. Its **write end lives in exactly
  one process — the spawner** (fd-hygiene invariant, leak-tested). EOF on
  this fd means that the parent died, `kill -9` included.
- `stdin_path: null` means /dev/null. Append on stdout/stderr is vendor
  parity (AutoSys appends to std_out_file/std_err_file).
- `grace_seconds`: the SIGTERM→SIGKILL escalation window for the
  parent-loss kill. The spawner reuses the same value for its own kills.

## 3. Spool format (frozen)

Everything below lives in `run_dir`. Every write uses the durability
liturgy: same-directory temp file, fsync(file), rename, fsync(directory).
`run_dir` must be on a **local** filesystem (rename-over-NFS has
ambiguous crash semantics). Each file is a single JSON object, with
sort_keys and one trailing newline. Consumers must ignore unknown fields
(forward compatibility). `version` increases only on an incompatible
change.

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
| `spawn_failed`| `error`             | the wrapper failed to spawn /bin/sh        |

The **absence** of status.json is the one state that the wrapper can
never produce deliberately. It means that the recorder itself was killed
(-9) or that the machine died. This is the orchestrator's E7 unobservable
case. The orchestrator reports it as FAILURE `exit_status_unobservable`
and never guesses.

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
   SIGTERM to the command pgid, waits the grace period, and sends
   SIGKILL. Then it writes `terminated / parent lost` and exits.
6. The wrapper exit code is a notification only (0 = a status record
   exists, 2 = spec error, 3 = a record write failed, for example
   ENOSPC). status.json is the sole data channel.

## 5. Supervisor socket protocol (frozen — phase 11f, DL-48)

One supervisor exists per run_root. The named socket is
`<run_root>/supervisor.sock`, mode 0600, with a **same-uid peer-cred
check on every accept** (Linux SO_PEERCRED, macOS LOCAL_PEERCRED / struct
xucred). The supervisor also writes `<run_root>/supervisor.pid` (JSON:
pid, boot_id, started_at) and logs to `<run_root>/supervisor.log`. On
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

The supervisor ignores unknown fields (forward compatibility). An
unknown verb → `unknown_verb`. A missing/wrong `v` →
`unsupported_version`. A malformed line → `malformed_json` (the stream
is not desynced).

**Read-only verbs** (any connection, no lease):

- `LIST` → `{ok, version: 1, supervisor_pid, boot_id, lease: {holder,
  expires_at} | null, runs: [{run_id, job, run_number, run_dir, wrapper_pid,
  wrapper_alive, spawned_at, wrapper_rc}]}` — the response lists everything
  spawned since THIS supervisor started. A supervisor restart implies that
  all prior wrappers received EOF and recorded. The spool is the
  cross-restart truth, and LIST shows live state only. `wrapper_rc` is
  null while the wrapper is alive.
- `PING` → `{ok, version: 1}`.

**Lease verbs** (single controller, observers are unlimited):

- `ACQUIRE {controller_id, ttl_s, token?}` → `{ok, token, expires_at}`.
  `token` is a monotonically increasing fencing integer. The counter is
  in-memory only: supervisor death kills all wrappers by lifeline, so the
  counter cannot regress while any spawned run is alive.

  A lease is **live** when it is unexpired *and* its holder's connection
  is still open. A live lease yields only to a claimant that presents the
  **current token** in the request; everyone else gets
  `{ok: false, error: "lease_held", holder, expires_at}`. The incumbent
  re-keys this way (a fresh token, the old one dies), which is how a
  reconnect after a poisoned connection fences anything the old
  connection had in flight.

  A lease whose holder's connection is **gone** is freely grantable even
  while unexpired. That is what lets a crashed engine's resume re-acquire
  without waiting out the TTL. It is sound because the kernel closes this
  AF_UNIX fd only when the holder process is gone, `kill -9` included, so
  EOF is proof of death.

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
- `RENEW {token, ttl_s}` → `{ok, expires_at}`. `RELEASE {token}` → `{ok}`.
- Engine defaults: `ttl_s = 60`, with a renewal every 20 s.

**Mutating verbs** (these require `token`, and a stale/expired token →
`{ok: false, error: "stale_token"}`):

- `SPAWN {token, spec}` — `spec` is the §2 frozen wrapper input spec MINUS
  `lifeline_fd`, which the supervisor owns and fills. The write end lives
  in the supervisor ONLY. This is precisely the mechanism that detaches
  job lifetime from the engine. `run_id` doubles as the idempotency key.
  A replayed SPAWN with a known run_id spawns nothing and returns the
  original result plus `"duplicate": true`.
  → `{ok, run_id, wrapper_pid, spawned_at}`.
- `SIGNAL {token, run_id, sig}` with `sig` ∈ {`TERM`, `KILL`} — the
  supervisor compares the recorded command (pid, start-time) from
  `spawn.json` with the live process (the PID-reuse guard, reimplemented
  stdlib-side). Then it signals the command PGID, never the wrapper. Each
  call sends exactly one signal: the TERM→grace→KILL escalation stays
  engine-side (the oracle decides kills — the supervisor stays dumb). →
  `{ok}`, or `{ok, "noop": true}` for an already-dead or unverifiable
  group.
- `SHUTDOWN {token}` — orderly, the one exception to no-escalation (the
  engine is possibly gone). The supervisor sends TERM to each live
  command PGID, waits the per-run `grace_seconds`, and sends KILL to
  survivors. **Lifelines stay open until wrappers exit**, so wrappers
  observe the command deaths and record `signaled`/`exited` truthfully
  (never "parent lost"). The supervisor waits for the wrappers, replies
  `{ok}`, exits, and unlinks the socket + pidfile. SIGTERM/SIGINT also
  trigger this shutdown (Tier 2 / `supervise shutdown` fallback). Only
  SIGKILL (unhandleable) leaves the wrappers to their own EOF.

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

## 6. License earmark

These two modules and this document are earmarked Apache-2.0 on
extraction (LICENSING.md item 6). Until the extraction, do not add
per-file headers. Before CLA + relicense disclosure, do not accept
external contributions to earmarked files.
