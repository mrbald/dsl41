# Wrapper protocol — the wrapper's public contract

Status: frozen. DL-42 item 3 freezes the spool format and the wrapper
input spec. Each change to a frozen item needs a decision-log entry.
For the architect: this page fixes wrapper input, process evidence and
recording behavior. The [architect track](../README.md#documents) links
the engine design. [Runner design §7](runner-design.md#7-journal-and-recovery-e1-prod-grade)
owns interruption plans and restart; the wrapper does not choose them.
The implementation is `runner_wrapper.py` and `runner_procid.py`.

## 2. Wrapper input spec (frozen)

The input is a single JSON object on the wrapper's stdin. After the
wrapper reads the object, it points stdin at /dev/null. The spawner runs
the wrapper **by file path** (`sys.executable <path>/runner_wrapper.py`),
never with `-m`, so the wrapper's runtime imports stay in the standard
library.

```json
{
  "version": 1,
  "run_id": "uuid4 string identifying this attempt, from its dispatch (DL-428, DL-429)",
  "job": "job name",
  "run_number": 3,
  "command": "exact /bin/sh -c command line (profile already composed)",
  "run_dir": "/abs/path/runs/<job>.<run_number>/<ordinal>",
  "lifeline_fd": 3,
  "stdout_path": "/abs/path (opened APPEND)",
  "stderr_path": "/abs/path (opened APPEND)",
  "stdin_path": null,
  "grace_seconds": 10.0
}
```

`run_id` must match a **filename-safe grammar** at the wire:
`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`.
This is the canonical uuid4 string form (DL-118, DL-129).
Any other value is refused before anything is created.

`run_dir` is `<run_root>/runs/<job>.<run_number>/<ordinal>/` (DL-428,
DL-429). The ordinal starts at 1 for each run. One attempt has one
`run_id` and one directory; replacement attempts of the same run have
different ids and directories. The spawner owns the path. Path identity
uses resolved paths, so relative, absolute and symlinked paths to the
same directory agree.

- `lifeline_fd`: the read end of a pipe. Its **write end lives in exactly
  one process, the spawner** (fd-hygiene invariant, leak-tested). EOF on
  this fd means that the parent died, `kill -9` included.
- `stdin_path: null` means /dev/null. The wrapper opens stdout and stderr
  for append, as AutoSys appends to std_out_file and std_err_file (vendor
  parity).
- `grace_seconds`: the SIGTERM→SIGKILL escalation window for the
  parent-loss kill. The spawner uses the same value for its own kills.
- `env`, optional: an object of strings the command gets on top of the
  wrapper's environment, the invocation the attempt's `dispatch` record
  holds. Any other value is the spec refusal.

Every key in the JSON example is required; the spawner fills
`lifeline_fd` (DL-150). `env` is optional (DL-428). `version`, `run_number` and `lifeline_fd` are integers,
never booleans. `run_id`, `job`, `command`, `run_dir`, `stdout_path` and
`stderr_path` are strings. `stdin_path` is a string or null.
`grace_seconds` is a **finite** number, representable as a double, zero
or more (DL-151).
`job` names one directory component: it must not be empty, must not hold
a path separator, and must not be `.` or `..`.

**No string value may hold a NUL**: not `job`, not `command`, not any path
(DL-151). An embedded NUL makes `os.path`, `os.open` and the spawn raise
`ValueError`, not `OSError`. The check refuses it by name, as
`bad_spec`, before anything durable is written. Unchecked, a NUL in `command` or a stdio path would reach the
wrapper, which records the spawn as `spawn_failed`.

An **unknown key is refused** (`bad_spec`). The object is frozen, so a key
that this list does not name has no pinned type. This is the one
place in the protocol where an unknown field refuses instead of being
ignored.

## 3. Spool format (frozen)

`spawn.json` and `status.json` live in `run_dir`. Every write uses the
durability liturgy: a temp file in the same directory, fsync(file),
rename, fsync(directory). `run_dir` must be on a **local** filesystem,
because a rename over NFS has ambiguous crash semantics. Each file holds a
single JSON object. The wrapper writes its two files, `spawn.json` and
`status.json`, with sorted keys and one trailing newline. Consumers must
ignore unknown fields (forward compatibility). `version` increases only
on an incompatible change.

A consumer of `spawn.json` or `status.json` must **refuse** a `version`
that it does not implement. `true` and `1.0` are not the integer 1
(DL-151). The rule is tolerant on fields and strict on versions: the
`Wrapper-owned spool files` row of `docs/protocol-evolution.md`. The engine treats an unsupported version as unread, so it cannot
supply an outcome (DL-151, DL-229). At restart an unread record is
quarantined under DL-433; it does not become a guessed completion.

An **absent** `version` passes the spool reader. These two files form the
wrapper-owned spool row of the evolution matrix, and that row alone passes
an absent version: the Tier-0 wrapper writes them before any reader exists
to require one (`docs/protocol-evolution.md` §1, DL-227, DL-229).

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

- Start-time tokens are opaque strings. On Linux the token is `ticks:<n>`
  (field 22 of /proc/pid/stat), compared for exact equality. On macOS it
  is `lstart:<ps -o lstart= output>`, compared within ±2 s, because ps
  rounds to whole seconds. **If the live token of a pid does not match the
  recorded token, never signal that pid** (PID-reuse guard, DL-41a item 5).
  A zombie (exited, not yet reaped) is not alive for this guard, although
  its token still matches: it is already dead (DL-322).
- `command_pgid == command_pid`: the command leads its own process group.
  The wrapper is NOT a member of this group, so that a group kill can never
  kill the recorder before the recorder writes its record (DL-41a item 2).
- `boot_id` (kern.bootsessionuuid / /proc/sys/kernel/random/boot_id): a
  mismatch with the current boot voids all liveness checks and proves that
  nothing survived (DL-42 item 5).
- Timestamps are aware-UTC ISO-8601.

### status.json — written by the wrapper before reaping

```json
{"version": 1, "run_id": "…", "job": "…", "run_number": 3,
 "outcome": "exited", "exit_code": 7,
 "ended_at": "2026-07-11T12:23:56.357872+00:00"}
```

The wrapper writes this record before it reaps, on every path where
`spawn.json` landed. One path is the exception: the `spawn.json` write
itself failed. That attempt has no `spawn.json`, so no process identity is
recorded. The wrapper kills what it started, reaps it, then
attempts the `status.json` write as a best effort and exits 3. E7 covers
the file's absence on that path (DL-229).

Each attempt has at most one `status.json`, and it holds exactly one outcome:

| outcome       | extra fields        | meaning                                    |
|---------------|---------------------|--------------------------------------------|
| `exited`      | `exit_code`         | the command exited on its own              |
| `signaled`    | `signal`            | the command was killed by a signal that the wrapper did not send |
| `terminated`  | `cause`, `observed` | the wrapper killed the group (`cause: "parent lost"`, or a spawn-record write failure). `observed` carries the forensic exit detail |
| `spawn_failed`| `error`             | the wrapper could not open the command's stdin, stdout or stderr, or could not spawn /bin/sh (DL-150) |

No outcome of a command that ran is written as an absent file. The
**absence** of `status.json` means one of four things: the recorder itself
was killed (-9); the machine died; the wrapper refused the spec before it
spawned anything (§4 step 6, exit 1 or 2); or the record write itself
failed (exit 3) (DL-150). These are evidence gaps; the attempt's first stop
and the interruption rule decide their consumer meaning (DL-424).
The wrapper also reports a spec refusal or write failure on stderr.

The wrapper's exit code only tells the engine to read evidence. It never
decides an oracle status. The engine first translates the spool into an
attempt outcome (DL-426), then chooses its follow-up plan
([runner design §7](runner-design.md#7-journal-and-recovery-e1-prod-grade),
DL-424, DL-429):

| Evidence | Attempt outcome | Consumer action |
| --- | --- | --- |
| `exited`, before a stop | `natural{exit_code}` | Feed the raw code through SEM-09. |
| `signaled`, before a stop | `natural{signal}` | Feed TERMINATED (E8). |
| Exit or signal during the attempt's stop | `stopped{cause}` | Apply the interruption rule. |
| `terminated`, `parent lost` | `stopped{lifeline}` | Apply the interruption rule. |
| `spawn_failed`, or a spawn-record write failure | `spawn_failed` | Feed FAILURE `wrapper_spawn_failed`; retain whether launch occurred. |
| No readable record (wrapper gone, record timeout, unread version, or restart) | `unobservable` | Apply the interruption rule. |

The interruption rule can replace an idempotent attempt without ending its
run, hold a marked job or a run at its attempt limit, or preserve an oracle
end. Missing records are not unconditional FAILURE `exit_status_unobservable`:
DL-424 limits E7 to unobserved exits outside the interruption rule.
At restart, a missing record carries the first stop's cause or
`engine_death`. A restart reads each open attempt once and waits for no
previous wrapper (DL-433). Cleanup of found processes runs in the live engine loop.
Mismatched or unreadable records cause quarantine with evidence; a missing
record or failed process scan does not (DL-433).

### DSL41_RUN env tag — forensics only

The tag is base64url JSON `{"boot_id", "job", "run_id", "run_number"}` in
the command's environment. Never use it for identity decisions. macOS
KERN_PROCARGS2 omits the environment for restricted binaries (/bin/sh),
and Linux /proc/pid/environ is ptrace-gated (DL-41a item 5, probed
empirically).

### DSL41_COOKIE env variable — the process cookie (an addition)

DL-431 and DL-433 fix the process cookie. The spec keys, the spool files
and the wrapper's behaviour are unchanged (DL-436). The cookie uses the
optional `env` field.
The engine puts `DSL41_COOKIE=<root_id>.<run_id>` in the spec's
`env` for every command attempt, and in the wrapper's own environment.
`root_id` names the run root: a uuid written to the store's `meta` at
genesis and read at every restart (state version 27, DL-433), so one
prefix covers every attempt of the root, whichever engine start launched
it. `run_id` is the attempt's. The command gets the variable through
`env`, and its children inherit it.
On Linux, with or without pidfd support, the engine reads
`/proc/<pid>/environ` of the processes its uid may read. A process that
carries an attempt's cookie belongs to that attempt, whatever its
process group (`runner_procid.ours`). The wrapper is left out by the pid
`spawn.json` names. EACCES/EPERM skips an unreadable process and counts it; ENOENT/ESRCH
means gone. Other read failures hold cleanup as unknown (DL-431).
What is left of an attempt is the live membership of its proven process
group and the processes that carry its cookie. A leader's start token or
a member's cookie proves the group. Proof lasts until a reading finds it
empty; a later nonempty group with no proof is left alone (DL-431). Both are taken as reliable: the
engine is an orchestrator, not a jail. A process left when the command
exits is a leak, stopped or left running by the `process-leak` switch
(`docs/runner-design.md` §8a).
The limit: outside the recorded group the engine sees only what the
scan can read. A process that leaves the group and drops the cookie
(`setsid` with an exec that removes the variable, or with a main thread
that exits), one that leaves it and turns non-dumpable (`ssh-agent`
does both), and one that leaves it under another uid (a setuid helper)
are beyond the engine by design.
Calling `setenv`, `unsetenv` or `clearenv` after the exec does not
change what `/proc/<pid>/environ` shows. The systemd unit's cgroup
remains the stronger fence. Signals use a pidfd where supported (DL-436). A pidfd open or send
failure never falls back to `os.kill`. Without it,
the engine re-reads the start token (`proc_start_token`, `/proc/<pid>/stat`
field 22) and calls `os.kill` only if it still matches the scanned token.
The race between that check and the signal is accepted: the engine is an
orchestrator, not a jail. The scan, TERM, grace and KILL are the same with
either primitive. One log line per serving start with a run root names the
primitive; in-memory rehearsals print none.
On other platforms, and for an attempt dispatched before the cookie, the (pid, start-time) tuple and
the process group decide.

## 4. Wrapper behavior (frozen semantics)

1. The wrapper runs in its own session (`setsid`). The command runs in its
   own process group (`setpgid(0,0)` equivalent at spawn). The child
   restores the default signal dispositions before exec. SIG_IGN is
   inherited across exec, so without the reset the command would ignore a
   graceful SIGTERM.
2. The wrapper ignores SIGTERM, SIGINT, SIGHUP, SIGQUIT, SIGUSR1 and
   SIGUSR2. SIGKILL, SIGABRT, any other signal whose default action
   terminates and that the wrapper does not ignore, or machine death
   silences it. The engine applies the attempt rule above (DL-229, DL-424).
3. The event loop is a SIGCHLD self-pipe plus select over {self-pipe,
   lifeline}. The self-pipe is also the interpreter's wakeup fd, so the
   signal writes to it at delivery. A command that exits just before the
   select starts still wakes the wrapper. On every wakeup, the wrapper
   checks for child exit BEFORE it checks for lifeline EOF. So a
   completion that races parent death records as a completion.
4. On exit, the wrapper observes the exit with waitid(WNOWAIT), writes
   status.json, and then reaps. A stopped, continued or trapped report is
   not an exit: the command is still running (DL-300).
5. On lifeline EOF, the wrapper checks for exit again. Then it sends
   SIGTERM to the command's process group and waits the grace period. It
   sends SIGKILL only to a command that is still alive at the end of that
   wait. A command that ends on the SIGTERM is observed, and the wait stops
   there (DL-150). Then the wrapper writes `terminated / parent lost` and
   exits.
6. The wrapper exit code is a notification only: 0 = a status record
   exists, 1 = an unreadable spec, a missing key or any other uncaught
   error, 2 = the spec refusal, 3 = a record write failed (for example
   ENOSPC). The spec refusal comes
   before anything spawns. It refuses a `version` that is absent or is not
   the integer 1 (`true` and `1.0` are refused), a present `lifeline_fd`
   that is not an integer, and a present `grace_seconds` that is not a
   finite number of zero or more. A boolean is not a number (DL-229).
   A spec that is not readable JSON exits 1 before any record. So does a
   spec that lacks a key the wrapper reads with no default: `run_id`,
   `job`, `run_number`, `command`, `run_dir`, `lifeline_fd`,
   `stdout_path`, `stderr_path` (DL-150). The wrapper has two defaults of
   its own: `stdin_path` (null, meaning /dev/null) and `grace_seconds`
   (10.0). status.json is the sole data
   channel, and its absence reads the same way whatever the exit code was.

Citation pointers:

- DL-226 — the engine outcome and interruption mapping is defined in §3 (DL-424, DL-426).
- period-model §11a (`period-model.md`, retired) — DL-424 defines the exclusion; DL-428 fixes the current attempt spool and invocation.
