# Wrapper protocol — the wrapper's public contract

Status: frozen. DL-42 item 3 freezes the spool format and the wrapper
input spec. Each change to a frozen item needs a decision-log entry.
These sections are §§2-4 of the retired supervisor protocol, trimmed to
the wrapper: its input, the two spool files it writes, and its behaviour.
The code is `src/dsl41/runner_wrapper.py` and `src/dsl41/runner_procid.py`.


## 2. Wrapper input spec (frozen)

The input is a single JSON object on the wrapper's stdin. After the
wrapper reads the object, it points stdin at /dev/null. The spawner runs
the wrapper **by file path** (`sys.executable <path>/runner_wrapper.py`),
never with `-m`, so the wrapper's runtime imports stay in the standard
library.

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

`run_id` must match a **filename-safe grammar** at the wire:
`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`.
This is the canonical uuid4 string form that the adapter mints (DL-129;
period-model §11a). The `run_id` names a directory entry (§3), so any
other value is refused before anything is created.

`run_dir` must be `<run_root>/runs/<job>.<run_number>`. The spawner
owns that path. The mapping is one to one in both directions: one
`run_id`, one `(job, run_number)`, one directory. The two paths are
compared as **resolved paths**, not as strings. The engine and the
spec name the run root separately, and `./r`, `/abs/r` and a
symlinked `/tmp/r` are one directory.

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

Every key above is required except `lifeline_fd`, which the
spawner fills (DL-150). `version`, `run_number` and `lifeline_fd` are integers,
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
`Wrapper-owned spool files` row of `docs/protocol-evolution.md`. The two
consumers state the refusal in their own terms. In the engine, the record
reads as unread. An unread `status.json` loses its outcome and the run
becomes `exit_status_unobservable`, so a record whose meaning changed
cannot decide a verdict (DL-151, DL-229).

An **absent** `version` passes both readers. These two files form the
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
itself failed. That run has no `spawn.json`, so no process identity is
recorded. The wrapper kills what it started, reaps it, then
attempts the `status.json` write as a best effort and exits 3. E7 covers
the file's absence on that path (DL-229).

Each run has at most one `status.json`, and it holds exactly one outcome:

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
failed (exit 3) (DL-150). The first two are the orchestrator's E7
unobservable case. In the last two, the wrapper says on stderr and in its
exit code that no run started or that no record survived.

The orchestrator decides no outcome from the exit code; it may quote the
code in the cause and nothing more. It reports an absence as FAILURE
`exit_status_unobservable` once no verified survivor remains, and it never
guesses. A command group verified alive at resume is killed and recorded
TERMINATED instead; the survivor rule is DL-226's (DL-229).

Orchestrator mapping (dsl41's, recorded here as the reference consumer):

- `exited` → the raw exit_code through the SEM-09 boundary
- `signaled` and `terminated` → STATUS TERMINATED (a kill that actually
  happened)
- `spawn_failed` → STATUS FAILURE
- absence → STATUS FAILURE `exit_status_unobservable` (PENDING: E7) once
  no verified survivor remains; a command group verified alive at resume is
  killed and recorded TERMINATED (DL-226, DL-229)

### DSL41_RUN env tag — forensics only

The tag is base64url JSON `{"boot_id", "job", "run_id", "run_number"}` in
the command's environment. Never use it for identity decisions. macOS
KERN_PROCARGS2 omits the environment for restricted binaries (/bin/sh),
and Linux /proc/pid/environ is ptrace-gated (DL-41a item 5, probed
empirically).

### DSL41_COOKIE env variable — the process cookie (an addition)

This section is an addition to the frozen contract (DL-431). The spec keys, the
spool files and the wrapper's behaviour are unchanged.
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
`spawn.json` names. Same-uid processes are readable, so the ptrace gate
above does not stop this read.
What is left of an attempt is its recorded process group's live members
and the processes that carry its cookie. Both are taken as reliable: the
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
remains the stronger fence. Signals use a pidfd where supported. Without it,
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
   silences it, and E7 then reports the run (DL-229).
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
