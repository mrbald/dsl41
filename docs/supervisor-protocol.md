# Supervisor protocol — the lifecycle tier's public contract

Status: spool format + wrapper input spec frozen 2026-07-11 (phase 11b,
DL-42 item 3); supervisor socket protocol frozen 2026-07-11 (phase 11f,
DL-48). This document is the future extraction boundary: if the lifecycle
tier (runner_wrapper.py + runner_supervisor.py) is ever spun off (DL-42
triggers), what is written here is its public API. Changing anything frozen
here requires a decision-log entry.

The tier is deliberately dumb: it records process lifecycle facts durably
and does nothing else. No timers, no conditions, no retries, no policy.
Scheduling semantics live in the orchestrator (dsl41's oracle); dashboards
of meaning live in the orchestrator's UI (DL-42 item 6).

## 1. Roles

- **wrapper** (`runner_wrapper.py`, phase 11b): per-run shim, the direct
  parent of the command. The one process that cannot miss the exit status;
  writes it durably. Parent-agnostic: engine (11b–11e) and supervisor (11f)
  spawn it identically. Stdlib-only — enforced by an import test.
- **supervisor** (`runner_supervisor.py`, phase 11f): keeps parenthood
  alive across engine restarts. Owns the wrapper lifelines, so an engine
  restart REATTACHES rather than killing the jobs (E4 dissolved). Speaks the
  §5 socket protocol (SPAWN/SIGNAL/LIST/SHUTDOWN/PING + lease verbs).
  Stdlib-only, run by file path — same enforced boundary as the wrapper.

## 2. Wrapper input spec (frozen)

A single JSON object on the wrapper's stdin; the wrapper repoints stdin at
/dev/null after reading it. The wrapper is executed **by file path**
(`sys.executable <path>/runner_wrapper.py`), never `-m`, so its runtime
imports stay stdlib-only.

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

- `lifeline_fd`: read end of a pipe whose **write end lives in exactly one
  process — the spawner** (fd-hygiene invariant, leak-tested). EOF on it
  means the parent died, `kill -9` included.
- `stdin_path: null` means /dev/null. Append on stdout/stderr is vendor
  parity (AutoSys appends to std_out_file/std_err_file).
- `grace_seconds`: SIGTERM→SIGKILL escalation window for the parent-loss
  kill (and the spawner reuses the same figure for its own kills).

## 3. Spool format (frozen)

Everything below lives in `run_dir` and is written with the durability
liturgy: same-directory temp file, fsync(file), rename, fsync(directory).
`run_dir` must be on a **local** filesystem (rename-over-NFS has ambiguous
crash semantics). Files are single JSON objects, sort_keys, one trailing
newline. Consumers must ignore unknown fields (forward compatibility);
`version` bumps only on incompatible change.

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
  on macOS (compare within ±2s; ps rounds to whole seconds). **Never signal
  a pid whose live token fails to match the recorded one** (PID-reuse
  guard, DL-41a item 5).
- `command_pgid == command_pid`: the command is its own process-group
  leader, in a group the wrapper is deliberately NOT a member of — a group
  kill must never kill the recorder before it records (DL-41a item 2).
- `boot_id` (kern.bootsessionuuid / /proc/sys/kernel/random/boot_id): a
  mismatch with the current boot voids all liveness checks and proves
  nothing survived (DL-42 item 5).
- Timestamps are aware-UTC ISO-8601.

### status.json — written by the wrapper before reaping

```json
{"version": 1, "run_id": "…", "job": "…", "run_number": 3,
 "outcome": "exited", "exit_code": 7,
 "ended_at": "2026-07-11T12:23:56.357872+00:00"}
```

Outcomes (exactly one per run; the file appears at most once):

| outcome       | extra fields        | meaning                                    |
|---------------|---------------------|--------------------------------------------|
| `exited`      | `exit_code`         | command exited on its own                  |
| `signaled`    | `signal`            | command killed by a signal not sent by the wrapper |
| `terminated`  | `cause`, `observed` | the wrapper killed the group (`cause: "parent lost"`, or a spawn-record write failure); `observed` carries the forensic exit detail |
| `spawn_failed`| `error`             | /bin/sh could not be spawned at all        |

The **absence** of status.json is the one state the wrapper can never
produce deliberately: it means the recorder itself was killed (-9) or the
machine died — the orchestrator's E7 unobservable case, reported as
FAILURE `exit_status_unobservable`, never guessed.

Orchestrator mapping (dsl41's, recorded here as the reference consumer):
`exited` → raw exit_code through the SEM-09 boundary; `signaled` and
`terminated` → STATUS TERMINATED (a kill that actually happened);
`spawn_failed` → STATUS FAILURE; absence → STATUS FAILURE
`exit_status_unobservable` (PENDING: E7).

### DSL41_RUN env tag — forensics only

base64url JSON `{"boot_id", "job", "run_id", "run_number"}` in the
command's environment. Never used for identity decisions: macOS
KERN_PROCARGS2 omits env for restricted binaries (/bin/sh) and Linux
/proc/pid/environ is ptrace-gated (DL-41a item 5, probed empirically).

## 4. Wrapper behavior (frozen semantics)

1. Own session (`setsid`), command in its own pgid (`setpgid(0,0)`
   equivalent at spawn), default signal dispositions restored in the
   child pre-exec (SIG_IGN inherits across exec; without the reset the
   command would ignore graceful SIGTERM).
2. Wrapper ignores SIGTERM/SIGINT/SIGHUP/SIGQUIT: only SIGKILL or machine
   death silences the recorder — pinning the residual crash matrix to the
   DL-41a accepted cases.
3. Event loop: SIGCHLD self-pipe + select over {self-pipe, lifeline}. On
   every wakeup child-exit is checked BEFORE lifeline EOF: a completion
   racing parent death records as a completion.
4. On exit: observe via waitid(WNOWAIT), write status.json, then reap.
5. On lifeline EOF: re-check exit, SIGTERM the command pgid, grace,
   SIGKILL, write `terminated / parent lost`, exit.
6. Wrapper exit code is a notification only (0 = a status record exists,
   2 = spec error, 3 = a record write failed, e.g. ENOSPC); status.json is
   the sole data channel.

## 5. Supervisor socket protocol (frozen — phase 11f, DL-48)

One supervisor per run_root. Named socket `<run_root>/supervisor.sock`, mode
0600, **same-uid peer-cred check on every accept** (Linux SO_PEERCRED; macOS
LOCAL_PEERCRED / struct xucred). The supervisor also writes
`<run_root>/supervisor.pid` (JSON: pid, boot_id, started_at) and logs to
`<run_root>/supervisor.log`; on start it refuses to run if a live supervisor
already holds the socket (connect probe), and unlinks a stale one — parity
with the engine's control-socket gate (runner-design §10). Linux hardening:
`PR_SET_CHILD_SUBREAPER` (prctl 36) at startup, best-effort. The supervisor
never restarts itself; surviving ITS death is Tier 2's job.

**Framing.** JSON lines over `SOCK_STREAM`; one request line → one response
line, except async pushes (below). Every request carries `"v": 1`. Responses
are `{"ok": true, …}` or `{"ok": false, "error": "<code>", …}`. Unknown
fields are ignored (forward compat); an unknown verb → `unknown_verb`; a
missing/wrong `v` → `unsupported_version`; a malformed line → `malformed_json`
(the stream is not desynced).

**Read-only verbs** (any connection, no lease):

- `LIST` → `{ok, version: 1, supervisor_pid, boot_id, lease: {holder,
  expires_at} | null, runs: [{run_id, job, run_number, run_dir, wrapper_pid,
  wrapper_alive, spawned_at, wrapper_rc}]}` — everything spawned since THIS
  supervisor started (a supervisor restart implies all prior wrappers EOF'd
  and recorded; the spool is the cross-restart truth, LIST is live state
  only). `wrapper_rc` is null while alive.
- `PING` → `{ok, version: 1}`.

**Lease verbs** (single controller; observers are unlimited):

- `ACQUIRE {controller_id, ttl_s}` → `{ok, token, expires_at}`. Refused
  `{ok: false, error: "lease_held", holder, expires_at}` while another
  unexpired lease exists. `token` is a monotonically increasing fencing
  integer (in-memory: supervisor death kills all wrappers by lifeline, so the
  counter cannot regress while any spawned run is alive). Re-acquire by the
  SAME controller_id is allowed and mints a NEW token (the old one dies) — so
  a crashed engine's resume re-acquires without waiting out the TTL.
- `RENEW {token, ttl_s}` → `{ok, expires_at}`; `RELEASE {token}` → `{ok}`.
- Engine defaults: `ttl_s = 60`, renew every 20 s.

**Mutating verbs** (require `token`; a stale/expired token → `{ok: false,
error: "stale_token"}`):

- `SPAWN {token, spec}` — `spec` is the §2 frozen wrapper input spec MINUS
  `lifeline_fd`, which the supervisor owns and fills (the write end lives in
  the supervisor ONLY — this is precisely what detaches job lifetime from the
  engine). `run_id` doubles as the idempotency key: a replayed SPAWN with a
  known run_id returns the original result plus `"duplicate": true`, spawning
  nothing. → `{ok, run_id, wrapper_pid, spawned_at}`.
- `SIGNAL {token, run_id, sig}` with `sig` ∈ {`TERM`, `KILL`} — verifies the
  recorded command (pid, start-time) from `spawn.json` (the PID-reuse guard,
  reimplemented stdlib-side), then signals the command PGID, never the
  wrapper. Exactly one signal per call: TERM→grace→KILL escalation stays
  engine-side (the oracle decides kills; the supervisor stays dumb). →
  `{ok}`, or `{ok, "noop": true}` for an already-dead/unverifiable group.
- `SHUTDOWN {token}` — orderly, the one exception to no-escalation (the engine
  may be gone): TERM each live command PGID, per-run `grace_seconds`, KILL
  survivors; **lifelines stay open until wrappers exit**, so wrappers observe
  the command deaths and record `signaled`/`exited` truthfully (never
  "parent lost"); wait for wrappers; reply `{ok}`; exit; unlink socket +
  pidfile. Also triggered by SIGTERM/SIGINT (Tier 2 / `supervise shutdown`
  fallback); only SIGKILL (unhandleable) leaves wrappers to their own EOF.

**Pushes.** The connection holding the current lease receives async lines
`{"push": "exit", run_id, wrapper_rc, at}` when a wrapper is reaped. Pushes
are NOTIFICATIONS only — droppable, never the data channel; a disconnected
controller loses them and recovers by LIST + status.json on reconnect (the
spool is the truth, same philosophy as the wrapper exit code).

The engine's OWN control socket (runner-design §10) deliberately keeps no
lease: sendevent is multi-writer by AutoSys nature and the single-writer
engine loop serializes it. The lease guards the tier that spawns without
semantics.

## 6. License earmark

These two modules and this document are earmarked Apache-2.0 on
extraction (LICENSING.md item 6). No per-file headers meanwhile; no
external contributions to earmarked files before CLA + relicense
disclosure.
