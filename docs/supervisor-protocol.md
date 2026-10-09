# Supervisor protocol — the lifecycle tier's public contract

Status: frozen. DL-42 item 3 freezes the spool format and the wrapper
input spec, and DL-48 freezes the supervisor socket protocol. Entries
that amend it include DL-80, DL-129, DL-150, DL-151, DL-210, DL-229,
DL-291 and DL-300; each is cited where it applies. Each change to a frozen item needs a decision-log entry. This
document is the extraction boundary of the lifecycle tier (the five
modules of §1). If a DL-42 trigger fires and the tier is extracted, this
document is its public API.

The tier is deliberately dumb. It records process lifecycle facts
durably and does nothing else.
It has no conditions, no retries, no policy and no scheduling timers. Its
time bounds are lifecycle bounds only (DL-150): the lease TTL, the
SHUTDOWN waits, the two-second output drain at teardown, the startup PING
probe and the optional deadman (all §5). The supervisor loop also reaps on
a one-second tick, so that a coalesced SIGCHLD is not missed. None of
these bounds decides what runs. Scheduling semantics live in the
orchestrator (dsl41's oracle). Dashboards of meaning live in the
orchestrator's UI (DL-42 item 6).

## 1. Roles

- **wrapper** (`runner_wrapper.py`): the per-run shim and the direct
  parent of the command. It is the one process that cannot miss the exit
  status, and it writes the status durably. The engine and the supervisor
  spawn it the same way. It imports only the standard library and the
  tier's own stdlib-only modules below, and an import test enforces this
  boundary.
- **supervisor** (`runner_supervisor.py`): keeps parenthood alive across
  engine restarts. It owns the wrapper lifelines, so an engine restart
  REATTACHES to the jobs and does not kill them (E4 dissolved). It speaks
  the §5 socket protocol: SPAWN, SIGNAL, LIST, SHUTDOWN, PING and the lease
  verbs. It imports only the standard library and the tier's own
  stdlib-only modules below, and runs by file path, under the same
  enforced boundary as the wrapper.
- **process identity** (`runner_procid.py`, DL-72): the one copy of the
  durability liturgy, the boot-session id, the (pid, start-time) PID-reuse
  guard and the quiet group kill. The wrapper and the supervisor share it.
  It is a sibling *inside* the boundary: it imports the standard library
  only, both import it under its plain top-level name, and the same import
  test covers it.
- **canonical form** (`canon.py`, DL-129): the one implementation of the
  §3.2 canonical form (`docs/period-model.md`). The supervisor writes its
  three §3 records in this form and reads them back through it. It is the
  second sibling inside the boundary (DL-150), on the same terms: standard
  library only, imported by the supervisor under its plain top-level name,
  and covered by the same import test. The wrapper does not import it.
- **state-machine core** (`state_machine.py`, DL-291): the declared
  transitions of the supervisor's process and lease machines, and the one
  check that takes them. It is the third sibling inside the boundary, on
  the same terms as `canon.py`. The wrapper does not import it.

Extraction takes all five files or none.

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

`run_dir` must be `<run_root>/runs/<job>.<run_number>`. The supervisor
owns that path. The mapping is one to one in both directions: one
`run_id`, one `(job, run_number)`, one directory. The two paths are
compared as **resolved paths**, not as strings. The engine and the
supervisor are told the run root separately, and `./r`, `/abs/r` and a
symlinked `/tmp/r` are one directory.

- `lifeline_fd`: the read end of a pipe. Its **write end lives in exactly
  one process, the spawner** (fd-hygiene invariant, leak-tested). EOF on
  this fd means that the parent died, `kill -9` included.
- `stdin_path: null` means /dev/null. The wrapper opens stdout and stderr
  for append, as AutoSys appends to std_out_file and std_err_file (vendor
  parity).
- `grace_seconds`: the SIGTERM→SIGKILL escalation window for the
  parent-loss kill. The spawner uses the same value for its own kills.

The supervisor checks the whole object before it writes anything durable
(§5, DL-150). Every key above is required except `lifeline_fd`, which the
supervisor fills. `version`, `run_number` and `lifeline_fd` are integers,
never booleans. `run_id`, `job`, `command`, `run_dir`, `stdout_path` and
`stderr_path` are strings. `stdin_path` is a string or null.
`grace_seconds` is a **finite** number, representable as a double, zero
or more. §5's SHUTDOWN escalates TERM→KILL after that many seconds, so an
`Infinity` would make the whole orderly shutdown unbounded (DL-151).
`job` names one directory component: it must not be empty, must not hold
a path separator, and must not be `.` or `..`.

**No string value may hold a NUL**: not `job`, not `command`, not any path
(DL-151). An embedded NUL makes `os.path`, `os.open` and the spawn raise
`ValueError`, not `OSError`. The check refuses it by name, as
`bad_spec`, before anything durable is written. Unchecked, a NUL in
`job` or `run_dir` would reach the supervisor's own path calls and
answer `internal:`; a NUL in `command` or a stdio path would reach the
wrapper, which records the spawn as `spawn_failed`.

An **unknown key is refused** (`bad_spec`). The object is frozen, so a key
that this list does not name has no pinned type, and the receipt
fingerprint (§3) is injective only over pinned types. This is the one
place in the protocol where an unknown field refuses instead of being
ignored. §5's forward-compatibility rule covers the fields of a request,
not the keys inside `spec`.

## 3. Spool format (frozen)

`spawn.json`, `status.json`, `receipt.json` and `reply.json` live in
`run_dir`. One spool file lives elsewhere: the `run_id` index entry, at
`<run_root>/runs/.by_run_id/<run_id>` (DL-150). Every write uses the
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
cannot decide a verdict. In the supervisor, the record reads as PRESENT
AND UNREADABLE, never as absent, because absence authorizes a spawn (§5).
That absence means no index entry for the `run_id` AND no receipt at the
computed path; §5's table lists the other cases (DL-151, DL-229).

An **absent** `version` passes both readers. These two files form the
wrapper-owned spool row of the evolution matrix, and that row alone passes
an absent version: the Tier-0 wrapper writes them before any reader exists
to require one (`docs/protocol-evolution.md` §1, DL-227, DL-229).

The **supervisor**, not the wrapper, writes three spool files:
`receipt.json` and `reply.json` in `run_dir`, and the `run_id` index entry
`runs/.by_run_id/<run_id>` (DL-129; period-model §11a). Each is one object
in the **§3.2 canonical form** (`docs/period-model.md`): UTF-8, keys
sorted at every depth, no whitespace, **no trailing newline**. Each is
written by the liturgy above, and each carries `artifact_format_version`.
The supervisor creates a detached run's directory when it receives the
SPAWN. The engine creates the directory only for tethered runs.

`received_at` and `spawned_at` are aware-UTC ISO-8601 strings, like the
wrapper's timestamps (DL-150). §3.2 governs the bytes of the object and
does not reshape these two strings. A reader of these records checks that
the field is a string and does not parse its timestamp form.

### receipt.json — written by the supervisor BEFORE it forks the wrapper

```json
{"artifact_format_version": 1, "run_id": "…",
 "spec_fingerprint": "sha256:…",
 "received_at": "2026-08-20T12:23:55.123456+00:00"}
```

`spec_fingerprint` is sha256 over the §3.2 canonical form of the §2 input
spec with `lifeline_fd` removed. The supervisor fills that field, so a
retry that carries one must not read as a different spec. §3.2's value
grammar has no floats, and `grace_seconds` can be one. A float is
therefore fingerprinted as the tagged string `"float:" + float.hex()`. The
form is exact, and only this fingerprint reads it.

### reply.json — written by the supervisor after the fork

```json
{"artifact_format_version": 1, "run_id": "…",
 "wrapper_pid": 4242, "spawned_at": "2026-08-20T12:23:55.987654+00:00"}
```

It holds the SPAWN answer as first given. A replayed SPAWN is answered
from this file.

### runs/.by_run_id/&lt;run_id&gt; — the run_id index

```json
{"artifact_format_version": 1, "run_id": "…", "job": "…", "run_number": 3}
```

The index entry is the first durable *record* that a SPAWN writes; the run
directory itself is made one step earlier (§5). It is the only route from
a `run_id` back to its directory.

**"No index entry" means "first application"** only when the computed
path holds no receipt either. A receipt there that names the same `run_id`
answers from the directory. A receipt that names a different `run_id` is a
`collision` (§5; DL-151, DL-229). Once the receipt is gone too, deleting
an entry can authorize a spawn. The supervisor can still answer
`collision` when another `run_id`'s index entry names the directory, or
`indeterminate` when a `spawn.json` or `status.json` remains (§5's table).
So an entry may not be pruned while the SPAWN effect that names it can
still be replayed (period-model §11a, §12).

The index directory therefore grows by one entry per run, and the
retention floor bounds it. A read is a single lookup by name. The whole
directory is scanned in one case only: an orphan run directory, to find
whether another `run_id` already claims it. That case exists only after a
crash.

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
recorded; a supervisor-spawned run does have its index entry and
`receipt.json` (§5). The wrapper kills what it started, reaps it, then
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
   lifeline}. On every wakeup, the wrapper checks for child exit BEFORE it
   checks for lifeline EOF. So a completion that races parent death
   records as a completion.
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
   (10.0). The supervisor refuses either omission first (§2), so only a
   direct spawner reaches these defaults. status.json is the sole data
   channel, and its absence reads the same way whatever the exit code was.

## 5. Supervisor socket protocol (frozen — phase 11f, DL-48)

One supervisor exists per run_root. Its socket is
`<run_root>/supervisor.sock`, mode 0600, with a **same-uid peer-cred check
on every accept** (Linux SO_PEERCRED, macOS LOCAL_PEERCRED / struct
xucred). The check refuses a peer uid that differs from the supervisor's
own (DL-150). Where the platform supplies no uid at all, the peer is
admitted, and the 0600 mode is the whole boundary. The access perimeter
deliberately does not copy that fallback: it fails closed (`docs/access-model.md` §3).

The supervisor also writes `<run_root>/supervisor.pid` (JSON: `pid`,
`start_time`, `boot_id`, `incarnation`, `started_at`). It logs to its own
stderr and opens no log file (DL-150). The spawner points that stderr at
`<run_root>/supervisor.log`.

Startup first takes `<run_root>/supervisor.lock`, an exclusive
non-blocking flock held for the process lifetime (DL-210). The file is
never unlinked. Under that lock, the supervisor sweeps leftover `.s.*`
socket files, probes the published endpoint with three PING attempts
across one second, and checks the pid record before it reclaims anything.
A held lock, a PING answer or a live recorded process refuses startup with
exit 1 and the message `another supervisor owns this root`. A zombie
(exited and unreaped) counts as absent: it holds no descriptor, no lock
and no socket. Runtime `OSError` failures also exit 1, so that the service
can retry. Configuration refusals exit 2.

`start_time` in the pid record is the opaque
`runner_procid.proc_start_token` value, and PID reuse is checked against
it. A failed token lookup alone does not prove absence. A live legacy pid
without that token also refuses startup, so an older supervisor that holds
no lock still owns its root. An unreadable pid record is ambiguous and
refuses reclamation; resolve the owner before you remove the record by
hand. A missing pid record names no owner: once all three PING attempts
fail, the published socket can be reclaimed.

Do not launch old and new binaries at the same time on one root during an
upgrade. The new lock cannot exclude a lockless old starter that appears
after the guards. The guards protect only a legacy owner that has already
published.

After both guards, the supervisor reclaims the stale published socket. It
binds and listens on `<run_root>/.s.<pid>`, chmods that socket to 0600,
records its inode, durably writes the pid record including `start_time`,
and then renames the socket to `supervisor.sock`. Teardown removes the
published socket only while its path still names that inode. If
publication failed, teardown removes its own private socket instead. It
removes the pid record only if the record's incarnation matches, also
when publication failed after that write. The client never unlinks either
path. One startup line goes to stderr:
`supervisor: started pid=<pid> incarnation=<hex> boot_id=<id>`.

Linux hardening: the supervisor sets `PR_SET_CHILD_SUBREAPER` (prctl 36)
at startup, best-effort. The supervisor never restarts itself. Tier 2,
the service manager, restarts it after it dies.

**Framing.** The protocol is JSON lines over `SOCK_STREAM`. One request
line gets one response line; async pushes (below) are the exception. Every
request carries `"v": 1`. Responses are `{"ok": true, …}` or
`{"ok": false, "error": "<code>", …}`.

Accepted sockets are non-blocking (DL-210). Replies and pushes are queued
in order and flushed on write readiness. `REQUEST_LINE_LIMIT` is 1 MiB,
including the newline. An oversized request gets one `request_too_large`
refusal. Its remaining bytes are discarded through the newline, and the
next line is read normally. Invalid UTF-8 and excessively nested JSON
answer `malformed_json`. So do a UTF-16 or UTF-32 line and a line that
begins with a byte-order mark, because a byte-order mark is not UTF-8 JSON
(DL-229). Reads and writes on accepted sockets follow one error rule:

- LATER: `BlockingIOError`, `InterruptedError`, and `OSError` with errno
  `ENOBUFS` or `ENOMEM` keep the connection, change nothing, and retry on
  readiness.
- GONE: a receive that returns `b""` (EOF), or `OSError` with errno
  `EPIPE`, `ECONNRESET`, `ECONNABORTED`, `ENOTCONN` or `EBADF`, drops the
  connection once.
- UNKNOWN: any other `OSError` keeps the connection and logs one stderr
  line with the errno.

A live client is never dropped for an error outside the GONE list. A send
that returns zero leaves the connection and pending output unchanged. EOF
drops even a half-closed connection with queued replies.

`BACKLOG_BYTES` is 16 MiB per connection. At that bound on queued output,
reads pause. When output drains below the bound, lines already buffered
are dispatched first. Retained wire buffers are bounded per connection by
`BACKLOG_BYTES + REQUEST_LINE_LIMIT + one frame`; one large reply may cross
the output bound. Observers are unlimited, so the connection count and the
total memory are unbounded by design. The same-uid check is a trust boundary, not a
memory bound. Accept failures `EMFILE` and `ENFILE` are logged at most
once per minute. A reply at or above the shipped client's own line limit
still poisons that client's connection.

A client that stops reading stops being read, and its later requests
wait. A paused holder's unrenewed lease expires normally. A reading client
can pause across one large reply and resumes as that reply drains.
Shutdown drains pending output under one shared two-second deadline, then
drops all connections. The test-only environment variables
`DSL41_SUPERVISOR_TEST_BACKLOG_BYTES` and
`DSL41_SUPERVISOR_TEST_REQUEST_LINE_LIMIT` override these bounds once at
startup. Leave them unset in production, like `DSL41_WRAPPER_TEST_PAUSE`.

**Incarnation** (DL-80). The supervisor mints an `incarnation` id at every
start and returns it from `PING`, `LIST` and `ACQUIRE`. Every verb that
changes lease or run state must carry it: `SPAWN`, `SIGNAL`, `SHUTDOWN`,
`RENEW` and `RELEASE` (DL-150 names the last two). A mismatch is
`{"ok": false, "error": "wrong_incarnation", "incarnation": <current>}`.

The incarnation is not folded into the token, because the fencing counter
is in memory. A restarted supervisor mints token 1 again, and a controller
that still holds a token from the previous incarnation would match the new
holder's token by coincidence. The two refusals also stay distinct,
because they ask for opposite client behaviour:

- `wrong_incarnation` means that the supervisor you knew is gone and every
  wrapper it held has lost its lifeline. Each such wrapper kills its group
  and records in its own time (DL-205, DL-229). So re-acquire **and**
  reconcile from the spool.
- `stale_token` means that the supervisor is the one you knew, so no
  lifeline was cut and there is nothing to reconcile. Beyond that, it says
  only that the token presented cannot mutate this incarnation (DL-150).
  The lease may have expired, may have been released, or may be held by
  someone else under another token. The refusal does not say which, and it
  asserts nothing about any run.

A client may answer `stale_token` with `ACQUIRE`, and the shipped engine
does. The supervisor grants any lease that is not live (see the lease
verbs below) and refuses a live incumbent with `lease_held`. That refusal,
not `stale_token`, says that the lease is somebody else's. The incarnation
is public (any reader gets it from `PING`); the token is the secret half.

The supervisor ignores unknown fields (forward compatibility). An unknown
verb answers `unknown_verb`. A missing or wrong `v` answers
`unsupported_version`. A malformed line answers `malformed_json`, and the
stream stays in sync. An oversized line answers `request_too_large`; the
reader discards through its newline and resumes (DL-210).

`v` and `token` are **integers**, and neither JSON `true` nor `1.0` is one
(DL-151). In Python, `==` equates all three. A bare comparison would serve
a request that carries `"v": true` as version 1, and would pass a
`"token": true` through the fence at token 1. The `incarnation` needs no
such check: it is a hex string, which no boolean and no number can equal.
This is a type check on the wire, not a tolerance. The token is the whole
fence that keeps a superseded controller from mutating a live one's runs.

A blank or whitespace-only line is ignored and gets no answer at all.
Every other line gets exactly one answer (DL-150). A handler that raises is
answered `{"ok": false, "error": "internal: <type>: <message>"}`, and the
supervisor keeps running. Its own death would EOF the lifeline of every
wrapper on the host, so one request may never end it.

**Read-only verbs** (any connection, no lease):

- `LIST` → `{ok, version: 1, supervisor_pid, boot_id, incarnation,
  deadman_s, lease: {holder, expires_at} | null, runs: [{run_id, job,
  run_number, run_dir, wrapper_pid, wrapper_alive, spawned_at,
  wrapper_rc}]}`. The response lists what THIS supervisor still holds in
  memory. The spool is the cross-restart truth. A supervisor restart
  proves nothing about the prior wrappers. Each survives, takes lifeline
  EOF, kills its group and records in its own time, and a restarted
  supervisor's LIST is empty while that happens (DL-205, DL-229).
  `wrapper_rc` is null while the wrapper is alive.

  LIST holds every live run and a bounded window of the most recent
  completions. A completed entry may be evicted once its exit is recorded
  and pushed, because LIST is not the idempotency store (DL-129;
  period-model §11a). The client reads older completions from the spool;
  LIST itself never reads the spool. A SIGNAL for an evicted run answers
  `unknown_run`, exactly as it does for any run of an incarnation that has
  ended.

  The `lease` field reports an UNEXPIRED lease, which is not the same as a
  live one (see the lease verbs below). It can still name a holder whose
  connection is gone (DL-150). Nothing may read it as proof that a
  controller is watching.
- `PING` → `{ok, version: 1, incarnation, deadman_s}`.

`deadman_s` (S5b, DL-95) is in both read verbs: the interval this
supervisor was started with, or `null`. A client reads it back rather than
assume it, because a reattaching engine meets a supervisor it did not
start. `docs/concurrency-model.md` §8's eviction bound has to describe the
host, not some engine's launch options. The field is additive: older
clients ignore it like any unknown field.

**Lease verbs** (single controller, observers are unlimited):

- `ACQUIRE {controller_id, ttl_s, token?, incarnation?}` → `{ok, token,
  expires_at, incarnation}`. `controller_id` must be a non-empty string.
  Anything else answers `{ok: false, error: "bad_controller_id"}`, checked
  before `ttl_s` and before any lease state (DL-150). `ttl_s` is optional
  on `ACQUIRE` and on `RENEW`, and defaults to 60 s. The supervisor puts no
  bound on it: a zero or negative value makes a lease that is already
  expired. A value whose expiry is not a representable time (`inf`, `nan`,
  `1e20`) is answered `internal:` and changes no lease state (DL-300).
  `ACQUIRE` is the one lease verb that does not require the incarnation. A
  free lease is granted without one, and the incarnation is read only to
  test incumbency against a live lease.

  `token` is a monotonically increasing fencing integer. The counter is in
  memory only, so a restarted supervisor mints token 1 again. The
  incarnation, not the counter, fences the previous incarnation's tokens
  (DL-80). The predecessor's wrappers may still be killing their groups
  and recording when the new supervisor starts (DL-205, DL-229).

  A lease is **live** when it is unexpired *and* its holder's connection
  is still open. A live lease yields only to a claimant that presents both
  the **current token** and **this incarnation**. Everyone else gets
  `{ok: false, error: "lease_held", holder, expires_at}`. The incumbent
  re-keys this way: it gets a fresh token, and the old one dies. This is
  how a reconnect after a poisoned connection fences anything that the old
  connection had in flight.

  A lease whose holder's connection is **gone** is freely grantable, even
  while unexpired. This lets a crashed engine's resume re-acquire without
  waiting out the TTL. It is sound on a local AF_UNIX socket, because EOF
  there has exactly two causes, and both end in the same place (DL-150).
  The kernel closes the fd when the holder process is gone, `kill -9`
  included. Or the holder closes it itself: the shipped controller poisons
  a connection whose reply may be in flight, and reconnects. Either way,
  the next `ACQUIRE` mints a fresh token. The old token, which a poisoned
  connection may still carry, is dead from that moment. So the branch keys
  on EOF and needs neither cause.

  `controller_id` authorizes nothing (DL-79). It is a label for `LIST` and
  for the `lease_held` refusal. Clients should make it unique per
  incarnation, so that those two reads name a specific controller. A
  matching label does not take a live lease (DL-79). That would be safe
  only while one run_root has one engine, which the orchestrator's own
  control-socket bind enforces on one machine.

  The token proves **incumbency, not authenticity**: it is a small
  monotone integer. Authentication is the same-uid peer-cred gate on
  accept, and a same-uid process is already inside the trust boundary.

  **Constraint on any future non-local transport:** EOF stops being proof
  of death. A relay must not close the supervisor-side connection while
  its controller lives, or the orphan branch must become TTL-gated.
- `RENEW {incarnation, token, ttl_s}` → `{ok, expires_at}`.
  `RELEASE {incarnation, token}` → `{ok}`. Both change lease state, so both
  take the same two-step check as the mutating verbs below: the
  incarnation first, then the token (DL-150).
- Engine defaults: `ttl_s = 60`, with a renewal every 20 s. This is the
  client's policy (DL-150). The supervisor's own default for an absent
  `ttl_s` is the same 60 s.

**Mutating verbs** (DL-80, DL-229). These require `incarnation` and
`token`, and each signature line below names both. A foreign incarnation
answers `{ok: false, error: "wrong_incarnation", incarnation}`, checked
first. Then a stale or expired token answers
`{ok: false, error: "stale_token"}`.

- `SPAWN {incarnation, token, spec}` → `{ok, run_id, wrapper_pid,
  spawned_at}`. `spec` is the §2 frozen wrapper input spec. `lifeline_fd`
  is the supervisor's to own and fill, and it is the one optional key. A
  `spec` that carries one is accepted, and its value is replaced before
  the wrapper starts. The receipt fingerprint (§3) ignores the field
  either way, so a retry that carries a stale fd does not read as a
  different spec (DL-150). The write end lives in the supervisor ONLY.
  This is the mechanism that detaches job lifetime from the engine.
  `run_id` doubles as the idempotency key. A replayed SPAWN with a known
  `run_id` spawns nothing and returns the original result plus
  `"duplicate": true`.

  **The idempotency store is the run directory, not `self.runs`** (DL-129;
  period-model §11a). LIST must stay bounded on a root that never rolls,
  so completed entries leave memory. An in-memory dedup would turn a
  delayed duplicate SPAWN into a second execution as soon as they leave.
  On receipt, after the §2 checks and the replay resolution, the
  supervisor writes in this order:

  1. `mkdir runs/<job>.<run_number>`. The directory can already exist in
     one case only: the orphan that the table's last row cleared for
     reuse, because the replay resolution runs first (DL-150).
  2. The `run_id` index entry (§3). **Index before receipt**, because the
     first durable thing that names a run must be the thing that every
     later lookup goes through.
  3. `receipt.json`, **before** the fork.
  4. The wrapper.
  5. `reply.json`.
  6. The answer.

  A replay resolves the directory **through the index**, never through the
  incoming path, and answers from the directory, not from memory. The
  incoming path is read in one case only: there is no index entry for
  this `run_id` (DL-150). A receipt there for a DIFFERENT `run_id` is a
  `collision`. A receipt there for THIS `run_id` means that the index was
  lost under a live tombstone. The directory answers, and the SPAWN is
  never a first application, because losing an index must not authorize a
  second process. The table's last row is reached only when the path
  holds no receipt either:

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
  | no index entry, no receipt, but a `spawn.json` or `status.json` | `indeterminate` — an engine-made, receiptless directory (a tethered run, or one from before this protocol); forking into it would overwrite the first run's records |
  | no index entry, no receipt, nothing else | first application — an orphan directory with neither is reused, because nothing durable names its run |

  The duplicate envelope is frozen: `{ok, run_id, wrapper_pid, spawned_at,
  "duplicate": true}`. The spec and idempotency refusals are
  `{ok: false, error, detail}`, with `error` ∈ {`bad_run_id`, `bad_spec`,
  `collision`, `in_progress`, `indeterminate`}. The `in_progress` refusal
  also carries `run_id` (DL-150). `in_progress` is **retryable and not a
  completion**: the wrapper is alive, so a client must wait for its
  outcome rather than record a failure for a running process. `collision`
  and `indeterminate` are final, and the engine's E7 policy owns what the
  run then becomes.

  A failed `mkdir`, index write, receipt write or fork answers
  `{ok: false, error: "spawn_failed: <reason>"}`. This tier ANSWERS such a
  failure rather than dying of it, because its own death would EOF the
  lifeline of every wrapper it holds. `reply.json` is the one exception,
  because it is written AFTER the fork. The wrapper is already running, so
  a failure there is logged, and the SPAWN still answers `{ok, …}`
  (DL-150). A replay then rebuilds that answer from `spawn.json`, which is
  the table's second row. Losing the run over its copy of the receipt is
  the one mistake this write order exists to avoid. Idempotency therefore
  outlives LIST presence and a supervisor **restart**: the memory entry is
  gone, and the directory answers.

  **Absent means ENOENT and nothing else** (DL-150). A record that exists
  and cannot be read is never absent, because absence authorizes a spawn.
  These records are unreadable:

  - a record with wrong permissions, or a truncated write;
  - bytes that the §3.2 ingress refuses;
  - a record that misses a required field;
  - an `artifact_format_version` that this binary does not implement;
  - bytes that are not UTF-8 at all: UTF-16, UTF-32, or a leading
    byte-order mark, which is not UTF-8 JSON (DL-151, DL-229);
  - a `spawn.json` or `status.json` whose own §3 `version` this binary
    does not implement (DL-151).

  That absence means no index entry AND no receipt at the computed path
  (DL-151, DL-229). An unreadable index entry or `receipt.json` is
  `indeterminate`. An unreadable `reply.json` falls to the next row of the
  table. Every next row is safer than the one above it, so an unreadable
  reply can cost the answer detail but can never invent one. An index
  entry that names a `run_id` other than its own filename is
  `indeterminate`. In the orphan-directory scan, an index entry that
  cannot be read blocks reuse, because it might claim this directory, and
  the answer is `collision`. A directory that cannot be listed is
  `indeterminate`. A `.<name>.<pid>.tmp` file left behind by an
  interrupted write is not a record and is skipped.
- `SIGNAL {incarnation, token, run_id, sig}` with `sig` ∈ {`TERM`,
  `KILL`}. The supervisor compares the command (pid, start-time) recorded
  in `spawn.json` with the live process (the §3 PID-reuse guard). Then it
  signals the command's process group, never the wrapper. Each call sends
  exactly one signal. The TERM→grace→KILL escalation stays on the engine
  side: the oracle decides kills, and the supervisor stays dumb. The
  answer is `{ok}`, or `{ok, "noop": true}` for a group that is already
  dead or cannot be verified.

  There is a third answer (DL-150, recording DL-83):
  `{ok: false, error: "not_ready"}`. SPAWN answers as soon as the wrapper
  is forked, and the wrapper writes `spawn.json` a few syscalls later. A
  SIGNAL that lands in that window finds a live wrapper and no record.
  That is not an already-dead group, and it must not read as `noop`, or a
  kill decided milliseconds after a start is dropped. The wrapper is the
  discriminator. Alive with no record is `not_ready`, and means retry.
  Exited with no record is `noop`, because nothing here can still be
  addressed. A `sig` outside {`TERM`, `KILL`} answers
  `{ok: false, error: "bad_signal"}`, checked before `run_id`.
- `SHUTDOWN {incarnation, token}`: an orderly shutdown. It is the one
  exception to no-escalation, because the engine may be gone. The
  supervisor sends TERM to each live command's process group, waits the
  per-run `grace_seconds`, and sends KILL to survivors. **Lifelines stay
  open until the wrappers exit**, so the wrappers observe the command
  deaths and record `signaled` or `exited` truthfully, never "parent
  lost". The supervisor waits for the wrappers, replies `{ok}`, exits, and
  unlinks the socket and the pid file. Both removals require the ownership
  checks above (DL-210). SIGTERM and SIGINT start the same shutdown: Tier 2
  sends SIGTERM, and an operator can send it as a fallback to `dsl41
  supervise shutdown`. Only SIGKILL, which cannot be handled, leaves the
  wrappers to their own EOF.

  Two bounds keep that wait finite (DL-150). First, the supervisor waits up
  to 5 s for a just-spawned wrapper's missing `spawn.json`. A command with
  no record cannot be signalled, and it would otherwise die by lifeline
  EOF alone and record "parent lost" (DL-48). Second, after the TERM, it
  waits the longest per-run `grace_seconds` plus 2 s. Past that, the
  supervisor sends one last KILL to every survivor's group, stops waiting,
  and answers. A wrapper still alive at that point loses its lifeline when
  the supervisor exits, and then runs §4 step 5 on its own. It records the
  command's own ending if the command has ended, and
  `terminated / parent lost` only if it has to kill the command. The
  promise above is bounded, not absolute.

  If an error ends that wait, the supervisor logs one line,
  `supervisor: the shutdown wait failed (<type>: <message>); running it
  once more`, and the SHUTDOWN is answered `internal:`. No request is
  dispatched after it. The supervisor writes its queued answers once,
  without blocking, then runs the wait once more and exits. A client that
  is not reading, or has a large backlog of pushes ahead of the answer,
  may get the answer only in teardown's two-second flush, or not at all.
  If that second run fails, or the cleanup before it fails, the supervisor
  exits 1, and each wrapper still alive runs §4 step 5 on its own
  (DL-300).

**Pushes.** When the supervisor reaps a wrapper, the connection that holds
the current lease receives an async line
`{"push": "exit", run_id, wrapper_rc, at}`. Pushes are NOTIFICATIONS only:
droppable, never the data channel. A disconnected controller loses them.
On reconnect, it recovers with LIST and status.json. The spool is the
truth, as with the wrapper exit code.

An exit push is suppressed when the lease is inactive, its holder
connection is absent, or that connection is paused. A suppressed push sets
`pushes_dropped` on the lease (DL-210). The next reply to its holder
carries `"pushes_dropped": true`; pushes never carry it. Re-granting the
lease to the same `controller_id` keeps the notice, including on the
ACQUIRE reply after a reconnect. Queuing a reply does not clear the
notice. Flushing that reply fully to the kernel clears the drops it
reports; a later drop still needs a later reply.

The client arms one shared LIST task on this reply field and on
reconnect. The task checks armed waits once per `_LIST_RECHECK_EVERY`
interval, and only after their SPAWN or duplicate reply. A successful
listing that does not show the run as live marks that wait for the
spool-resolution ladder, including its settle window and its checks for
surviving commands. LIST never resolves an exit future. An `ok: false`
listing is unknown and marks nothing. The task stays available while
idle. A malformed LIST fails the observing wait, and the failed task is
replaced so that later waits can recover. Close waits up to five seconds
for an in-flight request to unwind, then cancels the task, even if it is
still waiting on that request. A dropped exit push can make `kill()` pay
both grace waits, after TERM and after KILL, even if the command has
already exited. The LIST net does not resolve that push future.

The engine's OWN control socket (runner-design §10) deliberately keeps no lease.
sendevent is multi-writer by AutoSys nature, and the single-writer engine
loop serializes it. The lease guards the tier that spawns without
semantics.

**The deadman** (S5b, DL-95). A supervisor started with
`--deadman-seconds N` stops its loop and returns once it has had **no live
leaseholder** for N seconds. N must be **finite and positive**; otherwise
the supervisor exits 2 (DL-151). A positivity test alone would admit
`nan`, which fails every comparison, and the interval would then fire on
the first tick: the supervisor would exit at once and take every wrapper
with it. `inf` would mean no deadman at all, which
omitting the flag already says. Both halves of "live" from the lease definition above apply:
unexpired *and* the holder's connection still open. An expired lease
whose connection is open is a controller that stopped renewing. An
unexpired lease whose connection died is a controller that is gone. The
clock restarts whenever a live leaseholder appears, so a reconnecting
engine reprieves the supervisor.

Its exit is the whole mechanism. The process dying EOFs every lifeline it
owns, and each wrapper then runs §4 step 5: TERM, grace, KILL, record
`terminated / parent lost`. Step 5 checks the command first (DL-150), so
a command that already ended records its own outcome instead. Teardown
closes the lifelines of unreaped wrappers explicitly (DL-210), which gives
an in-process supervisor the same EOF behavior as a process exit. The
deadman adds no kill path of its own, and the supervisor still decides
nothing about what should run. Without the flag, a supervisor tolerates an
absent controller forever, which lets an engine crash and resume with its
runs intact (DL-79).

The deadman exists for one reason outside this tier.
`docs/concurrency-model.md` §8's `evict` is the only state that lets
another host run work bound to this one, and it must be provable. Nothing
else bounds when the wrappers of a supervisor without a controller die. A
run root without a deadman is never reroutable except by force.

## 6. License earmark

The five modules of §1 and this document are earmarked Apache-2.0 on
extraction (LICENSING.md item 6, DL-150). Until the extraction, do not add
per-file headers. Until a CLA exists and contributors are told about the
relicense, do not accept external contributions to earmarked files.
