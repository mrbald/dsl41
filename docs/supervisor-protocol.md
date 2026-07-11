# Supervisor protocol — the lifecycle tier's public contract

Status: spool format + wrapper input spec frozen 2026-07-11 (phase 11b,
DL-42 item 3); the supervisor socket protocol section is a placeholder
frozen when phase 11f lands. This document is the future extraction
boundary: if the lifecycle tier (runner_wrapper.py + runner_supervisor.py)
is ever spun off (DL-42 triggers), what is written here is its public API.
Changing anything frozen here requires a decision-log entry.

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
  alive across engine restarts (SPAWN/SIGNAL/LIST/SHUTDOWN). Not built yet.

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

## 5. Supervisor socket protocol (11f — NOT YET FROZEN)

Pinned by DL-42 for when 11f lands, recorded so the shape is not
relitigated: a **versioned line protocol over a named unix socket**
(0600 + same-uid peer-cred check), verbs SPAWN/SIGNAL/LIST/SHUTDOWN;
unlimited read-only observers, exactly one controller holding a lease
(controller_id, expiry, fencing token; mutations carry the token and an
idempotency key). Linux hardening: PR_SET_CHILD_SUBREAPER. The engine's
own control socket keeps no lease (sendevent is multi-writer by AutoSys
nature; the single-writer engine loop serializes it).

## 6. License earmark

These two modules and this document are earmarked Apache-2.0 on
extraction (LICENSING.md item 6). No per-file headers meanwhile; no
external contributions to earmarked files before CLA + relicense
disclosure.
