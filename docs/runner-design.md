# Runner design — executing IR-F with AutoSys semantics

Status: frozen (DL-41); a frozen item changes only through a decision-log
entry. This document is normative for the runner in the same way that
ir-design.md is normative for the compiler. The runner is prod grade (E1,
§7), runs in two time domains (E2, §9), and serves the web behind a proxy
or a tunnel (E3, §11). The supervisor tier is retired (DL-424).

## 1. Mission and scope

The runner is a production-capable **single-node** executor of CatalogIR
with AutoSys semantics (E1). One engine does two duties:

- **run** — the engine owns real workloads on the local machine: wall
  clock, real processes, durable state, and a control surface.
- **rehearse** — a virtual clock plus scripted adapters test and show an
  estate's behavior (a 24h estate in seconds) with the *same engine code
  path*. Thus rehearsal results are evidence about production behavior.

These items are explicitly not in scope (§12): an HA/clustered scheduler
and a multi-node agent fabric. Access control is `docs/access-model.md`
(DL-146). A single-node resource/load manager is in scope (DL-50), because
estates carry locks that they rely on for correctness; the oracle honors
these locks as capacity buckets, and preflight refuses the unmodelable.
Here, prod grade means
durable, resumable, auditable, and loud about everything that the runner
does not do. Prod grade does not mean highly available.

Lifecycle stance (DL-41a): the default is **tethered**. Engine death
terminates all jobs, and the record is durable even under `kill -9` (§6a).
The detached supervisor tier (§6a Tier 1) is retired (DL-424).

## 2. Position in the pipeline

The runner consumes IR-F (`CatalogIR`) through the compiler's loaders
(JIL → ast_jil → lowering, or the DSL surface). IR-G stays derived, and the
runner does not consume it: `plan` walks the AND-success skeleton over IR-F
(§8) and the UI lays out boxes from the catalog's own `box_name`. The
runner adds **no semantics**: the oracle is the single semantics
authority. The runner
contributes effects (processes, files), wall time, durability, and a
control surface.

## 3. Architecture — functional core, imperative shell

```
              ┌─ engine daemon (asyncio, one process per run) ──┐
 wall clock ─▶│  Oracle.feed / .advance   ◀── pure core         │
 completions ▶│  dispatch table ──▶ JobAdapter (subprocess/FW)  │
 control ────▶│  SQLite store (engine.db, DL-428)              │
              └───────────────┬──────────────────────────────────┘
                              │ control socket (unix domain, JSON lines)
                ┌─────────────┴─────────────┐
                │ Textual app (thin client) │
                │  terminal: dsl41 run --ui │
                │  browser:  dsl41 serve    │
                └───────────────────────────┘
```

The cut line follows the oracle's event contract:

- **Oracle → shell**: the oracle emits every internal transition as a
  STATUS event (`_write` → `_emit`). An emitted
  `STATUS(job, STARTING)` *is* the dispatch instruction, and only when its
  run number is past the last dispatched one (§4).
- **Shell → oracle**: the shell injects a completion as
  `STATUS {job, run_number, …}`. A normal exit carries the raw exit code
  only, and the oracle applies the SEM-09 / DL-33 boundary itself
  (`exit_is_success`). **Adapters never classify an exit code.** The two
  outcomes that have no exit code carry the status the shell observed
  instead: an observed kill is `{status: TERMINATED, cause}`, and a spawn
  failure or an unobservable exit is `{status: FAILURE, cause}` (§6, §7).
- **Oracle keeps**: KILLJOB termination, term_run_time auto-TERMINATE
  (dossier §5, timer-heap scheduled), run_window closer-edge (SEM-33), SLA
  alarms (SEM-34), box folds, ON_ICE/ON_HOLD/ON_NOEXEC (SEM-20/21/22),
  SEM-32 arm-and-wait (Q3; DL-54, DL-58).

For the real-time shell, the runner adds two methods to the oracle:

- `next_timer_due() -> datetime | None` — a read-only look at the timer
  heap, so that a real-time shell knows when to wake. In the oracle, timers fire
  lazily inside the next `feed()` whose `at` reaches them. A wall-clock
  shell cannot be sure that an external event will arrive.
- `advance(now: datetime) -> list[Event]` — fires the timers due `<= now`
  without an external event. It runs the same timer drain as `feed()`,
  under the same non-decreasing time discipline.

Both are deterministic and pure-core-compatible. Bisimulation (§13) pins
that `feed`-only and `advance`+`feed` schedules produce identical traces.

The engine uses more of the oracle than these two methods. `fork` and
`batch` (`InputBatch`) serve the admission order and the host table serves
routing (`docs/concurrency-model.md` §4 and §8). `trace_since` serves the
control `trace` verb (DL-310). The cascade's bound is §4's item cap
(DL-351). Constructing an oracle raises the process's recursion limit to
the fixed `RECURSION_LIMIT` and never lowers it (DL-351, DL-352).

## 4. Engine loop — single writer

One asyncio task owns the Oracle (the Oracle is not reentrant). The event
sources are: the adapter completion queue, control connections, the
scheduler (§5), and the oracle's own timers. Each iteration does these
steps:

1. `next_wake = min(oracle.next_timer_due(), scheduler.next_occurrence(),
   clock.next_sleeper_due(), first queued event)`.
2. The clock waits (§9). A real clock sleeps until `next_wake` and wakes
   early when the queue gains an event. A virtual clock jumps.
3. **Journal first** (§7): WAL-append and fsync the injected event, *then*
   run `feed()` / `advance()`. The oracle runs a cascade of instant
   starts on its executor's worklist, iteratively, and stops a cascade that
   does not end at an item cap set by the estate's job count (DL-351).
4. Append the attempt's `decision` record (`docs/concurrency-model.md` §4
   step 7).
5. Dispatch what the decision implies, in emitted order
   (`runner_admission.dispatch_plan`, `Engine._dispatch`): each start
   appends its `dispatch` record and then launches; each kill cancels the
   live task.

Real-domain admission floors each input's stamp at the admission frontier
after deduplication. A control input captured at wall time `wall` is admitted
at `max(wall, frontier)`, even while the wall clock is below the frontier.
The oracle sees no backward time. Admission does not wait for the wall clock
to catch up; timers and future ticks wait for their wall-clock deadlines.
Scheduler ticks and recovered outcomes keep their chosen stamps, subject to
the same floor. `Frontiers.admit` guards the non-decreasing invariant: a
stamp below the frontier reaching it is an engine bug.
The real-domain loop takes inputs, timers and ticks due at or before
`max(wall, frontier)`, including frontier-stamped completion plans. A backward
step rebases already-due queued inputs to that instant in arrival order.
Inputs queued during the gap keep that order, including `stop_requested`.
Future queued inputs retain their deadlines.

Dispatch table (emitted event → effect → what applying it does):

| emitted                                   | effect | applying it                     |
|-------------------------------------------|--------|---------------------------------|
| `STATUS STARTING`, job_type CMD           | SPAWN  | start a LocalCommandAdapter task |
| `STATUS STARTING`, job_type FW            | SPAWN  | start a FileWatcherAdapter task |
| terminal status, job has a live task      | KILL   | cancel the adapter task (see below) |
| `MUST_START_ALARM` / `MUST_COMPLETE_ALARM`| none   | none; they reach the operator through the trace and the UI |
| anything on a BOX                         | none   | none (folds are oracle logic)   |

An effect is applied only after its decision is durable, and a start only
after its `dispatch` record is. A start the job's row has moved past (a
terminal status, a status that is not live, or another run number) is not
launched (`spawn_superseded`, DL-232). The local adapter's wrapper is the
engine's child, and the FW adapter starts no process.

The terminal-status row makes KILLJOB and term_run_time work with no
adapter timeout logic: the oracle decides, the shell kills. The shell's own
act is the cancellation; what a cancellation costs is the adapter's
business. A CMD cancellation signals the command pgid (§6a) and the wrapper
records the outcome. An FW cancellation only stops polling — a watch owns
no process. ON_NOEXEC needs
no row. The SEM-22 bypass emits SUCCESS and never emits STARTING, so
nothing spawns by construction. The alarm row writes no store record of its
own: emitted events are never recorded in the store (§7), so an alarm exists in the
replayed trace and nowhere else.

The two STARTING rows carry one more condition, the **ghost-run gate**: a
STARTING spawns only when the job's run number is past the last one this
engine dispatched. A CHANGE_STATUS overwrite re-emits STARTING without
advancing the run number, and vendor parity launches nothing.

**Stale-completion gate.** Completions carry `(job, run_number)`. Every
completion enters the common admission order first, so it is durable before
it is judged (§7). The gate then rejects one whose run_number does not match
the current one, or whose row is not STARTING or RUNNING: a terminal
status, a status an operator injected, such as INACTIVE, or QUE_WAIT after
a restart queued behind a reservation (DL-235). An INACTIVE cascaded from the
job's box (SEM-18) counts as injected (DL-242). The rejection is that
attempt's `decision` record with its reason; no `drop` record is written.
This gate closes the race between a natural exit and a concurrent
KILLJOB/term_run_time kill. The gate must live in the shell. The oracle
contract deliberately lets injected STATUS overwrite terminal statuses
(sendevent CHANGE_STATUS parity). Thus, if a late completion is fed
through, it silently rewrites history.

## 5. Scheduler — the calendar the oracle deliberately lacks

The oracle "owns no calendar". In trace tests, the script injects STARTJOB
at the points where AutoSys's scheduler fires. In run mode, the runner has
that role. For each `date_conditions` job, the runner computes the next
occurrence from `days_of_week` + `start_times` (or `start_mins` hourly
ticks). When `timezone` is set, the runner resolves the name per SEM-35
(DL-62): `zoneinfo` first (case-insensitively -- the vendor value is not
case-sensitive), then the instance's ujo_timezones table supplied as
`--timezone-map` (the `autotimezone -l` listing; Alias/City entries chain
at most five reads, the vendor's own bound), then -- only without a map --
the unique-zoneinfo-city default (Zurich -> Europe/Zurich, a preflight
WARN). POSIX fixed offsets (`GMT+5`, `IST-5:30`) resolve west-positive;
POSIX strings with dst rules refuse. On a one-hour DST change at 02:00
local, `start_times` and `start_mins` follow the `dst-start-times` switch
(§8a, DL-260). The default is the vendor's rules. A start time in the
missing hour then ticks in the first minute of 03:00, with its minute as
seconds (2:05 ticks at 3:00:05); that is the one tick that is not on a
whole minute. The runner injects `STARTJOB` at the
tick and then computes the next occurrence. The scheduler fires
**unconditionally** at the tick. SEM-32 arm-and-wait on a false condition (Q3, DL-58) and
run_window closer-edge handling (SEM-33) stay oracle-side, exactly as in
simulation.

`run_calendar` / `exclude_calendar` resolve against the loaded calendar
export (DL-56). A **standard** calendar's date rows (`mm/dd/yyyy` with an
optional `HH:MM` or `HH:MM:SS` tail — the export sample stamps `00:00:00`,
Q9/DL-60) become an explicit day set and a per-day row-tick map. Seconds
truncate, because ticks are minute-grained. Day-eligibility is
membership for `run_calendar` (SEM-31: XOR `days_of_week`) minus
`exclude_calendar`'s days (SEM-31: subtracts from whichever is active).
The runner evaluates eligibility on the job's LOCAL day. When
`start_times`/`start_mins` are present, they are the ticks. With NEITHER,
a run_calendar job fires at each row's own time: bare rows at 00:00, an
extended (generated) day always at 00:00. Job-level times override row
times (E11, DL-58). Exclusion stays day-level.
A calendar with no eligible day at or after the anchor makes the job
**dormant**: no next occurrence, dropped from the tick map, never an
error. A finite date list that runs out is the calendar's stated meaning,
not an error. When this is already true at start, preflight WARNs (§8).
The runner interprets **extended** calendars (DL-57). `autocal.py`
implements the SEM-36..39 doc-freeze: candidate generation from the
date-condition keyword grammar, the O/S/N/W/P disposition pipeline, and
uniform blind `adjust`. The scheduler consumes it through a windowed
membership cache. The interpreter refuses loudly only what it genuinely
cannot interpret: unknown and doc-defective tokens, missing holcal/cyccal
dependencies, and degenerate walks. Open composition corners run on
pinned deterministic defaults (DL-59). Nonzero adjust with an N/W/P
action uses the pipeline order (replace, then blind shift — Q8b).
All-exclusive compound rules evaluate literally as includes (Q8d).
Same-date holiday/non-workday collisions take the holiday action (Q8a,
DL-58). Cycle-bound calendars exhaust to dormancy exactly like
explicit date lists. Unbounded rules scan to a 60-year dormancy ceiling
(a leap-day-weekday conjunction can legally gap ~40 years). Materializing
a refused calendar to a standard one is the workaround.
The scheduler runs identically over the virtual clock, so rehearse mode
uses real calendar arithmetic.

## 6. Adapters

`JobAdapter` protocol: `async run(job_ir, run_number, ctx) -> AdapterResult`,
where `AdapterResult` is `int | Terminated | Failed`. An `int` is the raw
exit code, and only the oracle classifies it. `Terminated(cause)` reports a
kill the wrapper observed. `Failed(cause)` reports a completion with no exit
code — a spawn failure, or the E7 unobservable case. A cancelled adapter must
never report. An adapter that owns a process group must kill the whole group
before the cancellation finishes.

**LocalCommandAdapter** (CMD): an engine-side task that spawns the per-run
**wrapper** (§6a) and awaits it. The wrapper spawns `/bin/sh -c`. When
`profile` is set, the wrapper sources it first — `. <profile> && <command>`.
Thus a failing profile fails the job with sh's exit code ([?] unverified
vendor corner, pinned here — E5). The command runs in its own pgid
*separate from the wrapper's* (§6a). The wrapper's `status.json` is the
**sole data channel** for the outcome. The wrapper's exit only tells the
engine to read it. Cancel (the oracle said terminal, §4): the engine
signals the command pgid SIGTERM, grace, then SIGKILL. The wrapper
observes the deaths and durably records the outcome like any other
completion. A cancel before the spec is handed over SIGKILLs the
wrapper, which has launched nothing.
The detached-SPAWN cancellation rule is retired (DL-424; DL-359).
When `std_out_file` / `std_err_file` are set, stdout/stderr
**append** to them (the vendor appends). Otherwise they append to
`<run_root>/logs/<job>.<run_number>.{out,err}`. Whether the engine
unescapes `\:` inside command/std_* values is the DL-39 [?]. Verbatim
carry applies here too. There is no timeout logic (term_run_time is the
oracle's timer). **No retry logic**: n_retrys is FAILURE-only (Q4,
DL-53), and retry modeling is deliberately unimplemented. This is a
recorded scope decision, not an open question. A shell-side
retry forks semantics from the oracle and breaks bisimulation. Preflight
warns instead (§8).

**FileWatcherAdapter** (FW): polls every `watch_interval` seconds (default
60 [?]) until `watch_file` exists with size `>= watch_file_min_size` and
the size is stable across two consecutive polls — `fw-existence=stable`,
the default (DL-258). With no `watch_file_min_size`, `fw-existence=immediate`
completes only when the run's first poll finds the file — the vendor reading.
A file that appears at a later poll waits for a steady size, as under
`stable`. A job with a minimum size always needs the steady-size rule. The
adapter completes with exit 0.

**Its progress is evidence, not memory** (DL-129; period-model §3.5). Last
observed size and stable-poll count decide when
a watch completes, and a restart resets both. So the watch has a spool, and it
is append-only: `runs/<job>.<run_number>/watch.jsonl`, a `start` line on
dispatch — the first durable record, so a dispatched watch always has one — then
one line per poll, fsynced, *including* polls that changed nothing. The line
is appended **before** progress moves or a completion is emitted, so an
observation that changed the watch is never one an audit cannot see.
`next_poll_at` is exactly: after `start` and no poll line, `start.at`; after
a poll line, `poll.at + interval`. A torn final line truncates on open.
A resumed watch reconstructs from the log and appends no
second `start`.

**FakeAdapter** (tests, rehearse): scripted `(job, run_number) →
(duration, exit_code)`, with instant success as the default. Scenario
files reuse the event-script shapes that the oracle trace tests already
use.

Execution identity: jobs run as the invoking user. `owner` set to a
different user is a preflight ERROR (the runner does not setuid). `machine` must be
local (§8).

## 6a. Process lifecycle tiers (DL-41a)

Unix gives exactly one `wait()` observation of a child's exit status. If
the observer is down when the child dies, init reaps the child, and the
status is gone forever. No tagging scheme recovers it. The fix is
structural: a process that cannot miss the observation writes it to a
durable location. Prior art: containerd-shim, Slurm's slurmstepd, and
HTCondor's starter. Every serious scheduler keeps a small local process
that reaps and reports after the control daemon disconnects. There are
three tiers, outermost first; Tier 1 is retired (DL-424):

**Tier 2 — init-system backstop** (deployment note, not code): run the
runner under systemd (Linux) / launchd (macOS). On Linux the unit's
cgroup contains the whole estate; containment of one run is its process
group, and the escapee that leaves it is a stated limit (DL-403, below).

**Tier 1 — supervisor** is retired, including its socket, lease and verbs (DL-424; DL-129, DL-150, DL-289, DL-396).

The engine socket (§10) has no lease: the single-writer loop serializes
sendevent (DL-42).

**Tier 0 — per-run wrapper** (the correctness tier, always present). This
tier is a dumb stdlib-only shim (`runner_wrapper.py`, no
third-party imports). The engine runs it BY FILE PATH, never as
`python -m`: `-m` would import the `dsl41` package first and drag
third-party imports into the recorder. The shim is parent-agnostic (DL-41a).
Its import boundary is stdlib-only sibling modules by top-level name:
`runner_procid` (DL-42, DL-72). It imports nothing through the dsl41
package, and a test pins the import set.
Duties:

1. `setsid()` — its own session. The command is spawned into its **own
   pgid, separate from the wrapper's** (`process_group=0` at spawn).
   This separation is load-bearing: `kill(-pgid, SIGKILL)` signals every
   member, and a wrapper inside the group dies *before it writes the
   record*. The wrapper stays outside the group that it (or the engine)
   signals.
2. Durably write `runs/<job>.<run_number>/spawn.json`: a schema `version`,
   run_id, job, run_number, wrapper pid + start-time, command pid,
   command pgid, command start-time, started_at, and
   **boot_id** (`kern.bootsessionuuid` /
   `/proc/sys/kernel/random/boot_id`). A reboot recycles the whole
   (pid, start-time) identity space. Thus a boot_id mismatch both voids
   liveness checks and *proves* that nothing survived. Durable-write sequence
   for every record: temp file in the same directory, `fsync(file)`,
   `rename`, `fsync(directory)`. The runs dir itself is fsynced at
   creation. The run directory must be a **local** filesystem.
   Rename-over-NFS has ambiguous crash semantics.
3. Spawn the command with the `DSL41_RUN` env tag (base64url JSON:
   run_id, job, run_number, boot_id). The tag is **forensics, not
   correctness**: macOS `KERN_PROCARGS2` omits env for restricted
   (platform/code-signed) targets like `/bin/sh` (XNU source), and Linux `/proc/pid/environ` is ptrace-gated. The
   identity check uses the **(pid, start-time)** tuple from spawn.json
   instead (`ps -o lstart=` / `/proc/<pid>/stat` starttime, ±2s tolerance
   on macOS's 1-second resolution). Never signal a pid that fails this
   check — the PID-reuse guard. A zombie (exited, not yet reaped) fails it
   too: its token still matches, but it is dead (DL-322).
4. Portable event loop: SIGCHLD self-pipe + `waitpid(WNOHANG)`, with
   `select` over {self-pipe, lifeline}. The self-pipe is also the
   interpreter's wakeup fd (`signal.set_wakeup_fd`), written at signal
   delivery. A Python handler runs only between bytecodes, so without it
   an exit just before the `select` call could leave the wrapper asleep
   while the lifeline is open. On every wakeup, **do the
   child-exit check before the lifeline-EOF check**. A job that completes
   at the instant the parent dies must be recorded as completed, not as
   "parent lost".
5. On child exit: observe with `waitid(WNOWAIT)` where available, durably
   write `status.json` — the same identity fields, an `outcome`, `ended_at`,
   and whichever of `exit_code` or `signal` applies — *then* reap. This
   order narrows the observe-to-record hole to a few syscalls. Both spool
   records are frozen field by field in `docs/wrapper-protocol.md` §3,
   which is the schema; this list is what each duty owes. The status
   file is the **authority**. The live reaping chain is only a
   notification that the file exists.
6. On lifeline EOF: the parent died, which includes `kill -9`, because
   the kernel closes fds regardless. SIGTERM the command pgid, grace,
   SIGKILL, write `status.json` {terminated, cause: parent lost}, exit.
   This behavior makes "engine death ⇒ jobs terminate AND are recorded"
   hold with no polling and no Linux-only mechanism (DL-357).

**Lifeline fd hygiene (invariant, tested):** the pipe's write end exists
in exactly one process — the parent. One write end that leaks into any
other process silently disables parent-loss detection for that wrapper.
Python's default non-inheritable fds enforce this rule: the engine's
adapter passes only the read end by `pass_fds`. Tests hold it for this
path (test_lifeline_write_end_leaks_nowhere,
test_sigkill_engine_midrun_then_resume). The supervisor spawn path is retired (DL-424).

**Containment honesty:** pgid kill does not catch `setsid`/double-fork
escapees (`sh -c 'daemon & '`). On Linux the process cookie (§7) does,
for an escapee that keeps its environment: the ladder signals each process
that carries the attempt's cookie. Vendor agents share this limitation, and
supervisord's group-stop flags are the same group kill. A per-run cgroup
would attribute such a helper to its run; it is a stated limit, not a
hardening to build (DL-403): a forced stop that meets
one ends `uncertain` and names it, the whole-estate stop under systemd
kills the unit's cgroup regardless, and a job that must end its own
helpers ends them itself. A `--scope` option is not built.

The crash matrix that remains after Tier 0: `kill -9` of a *wrapper*
alone (the command survives, no status.json will ever appear) and `-9` of
the whole tree at once (same). Both are sabotage-grade. Both are detected
at reconciliation (§7) and reported truthfully, not guessed.

## 7. Journal and recovery (E1: prod grade)

The store is one SQLite file per run root, `engine.db` (DL-428).
`SqliteStore` is the only durable record; it holds the inputs, configuration
history and snapshots. Each append commits synchronously. A 1.x root with
`journal.jsonl` or `wal/` and no store is refused by name.

Record kinds:

- `segment` — the first record of every segment, and the whole identity of
  the period it opens: `{rec, segment_no, estate_id, period_id,
  baseline_id, catalog_hash, catalog_hash_version, source_bundle_hash,
  runtime_hash, state_machine_version, clock_domain,
  first_index, opens_from_seal, reclaimed, trust_unaudited, at}`
  (`docs/period-model.md` §2.1, DL-130). Self-describing on purpose: a
  reader that opens it knows the period, the catalog and the semantics
  without reading an earlier file. `catalog_hash` + `state_machine_version`
  are what resume eligibility is an exact match on (S6a);
  `catalog_hash_version` says which recipe the hash was taken under, so the
  gate compares like for like. The build's version is deliberately NOT
  here: a patch release must not move these bytes (PR-07).
- `input` — `{seq, at, kind, payload, source, request_id, fingerprint}`
  plus `expect` and `claimed_actor` where the input was externally
  requested, source ∈ {scheduler, adapter, control, reconcile}.
- `advance` — `{seq, at, request_id, fingerprint}`: a time observation that
  the engine acted on (`Oracle.advance`), written before the advance (DL-44).
  The input alphabet has two halves: external events and time observations.
  Without the latter, an advance-fired term_run_time kill vanishes from
  replay, and a late natural-exit record can resurrect the job.
- `decision` — `{index, request_id, decision, reason, code, revisions,
  legacy_batch}`: the concurrency-model §4 step-7 record in one line
  (DL-118, `docs/period-model.md` §2.3): the decision the attempt at
  `index` got and the revisions it moved. `index`, not `seq`, because it
  shares its attempt's number and `seq` is the §10 subscribe cursor.
  `legacy_batch` is **required false**: this writer pins it, `true` is a
  retired dialect refused naming DL-138, and missing or non-boolean is
  malformed and refused as its own error.
- `dispatch` — `{job, run_number, run_id, run_dir, started_at}`: one
  start, appended after its decision and before its adapter task exists.
  The `run_id` is minted here and the wrapper spec carries it (PR-36a), so
  the store and the spool name one process identity. An engine that dies
  between the decision and this record leaves a decided start with no
  trace, which resume dispatches; one that dies after it leaves a record
  whose run directory says how far the start got (§7 resume). The
  wrapper's `spawn.json` is the authoritative spawn record, written by the
  process that did the spawn. The pgid is the wrapper's child's business;
  the engine never observes it (DL-44).
- `drop` — an input refused BEFORE admission, which means only scheduler
  ticks missed across downtime, skipped-and-recorded at resume (E9, DL-45).
  A completion the §4 gate rejects is not a `drop`: it is admitted like
  every other input, and its rejection is that attempt's `decision`, which
  replay can honour rather than guess at (DL-89).
- `seal` — `{rec, estate_id, period_id, closes_at_index, at, digest,
  next_period_id, next_baseline_id, catalog_hash_version, source,
  request_id, request_fingerprint, claimed_actor, force_seal}`: the last
  record of a period's last segment and the boundary's commit point
  (`docs/period-model.md` §2.2, which owns the schema). It names the period
  it closes, the index it closes at, the sidecar digest that must verify,
  and the identity the next period opens with. Not an input: replay applies
  nothing from it. **Any record after one in the same segment is refused at
  the read**, not tolerated as a torn tail.
- `preflight` — the §8 WARN items that the run started under (DL-45:
  "prints, journals, and runs" made literal). This record is not an
  input, and replay ignores it.
- Retired kinds are refused by name, citing DL-138: `header` (a
  once-per-log header cannot describe a log made of segments, DL-130), and
  `result` and standalone `effect` (the decision and its intents as
  separate records, DL-118). The 1.x `leader`, `host` and `effect_result`
  records are refused by name, citing 2.0.0: one engine holds a root with
  no election, no routing table and no outbox. A
  `catalog_hash_version` of 1 is refused the same way. An **unknown** `rec`
  is refused too, naming the kind, as its own error: the version gate sits
  on the opening `segment`, so an unrecognised kind inside a version-matched
  segment is corruption (`docs/protocol-evolution.md`).

The admission fields (DL-89, DL-90; `docs/concurrency-model.md` §4 is
normative for the order, this section for what the records hold):
`expect` and `claimed_actor` ride only where they exist: an
input the engine raised has neither, and writing nulls for them would blur
the one distinction the log has to keep, which inputs were externally
requested and therefore had to name a revision. Replay reads `expect` back,
because an attempt admitted without a result is re-decided through the same
gate, and the revision it named is half of what that gate reads.

Resume's side effects are bounded to this list:

- an attempt admitted with no `decision` record gets that record, with the
  effects its recovered verdict implies and a SPAWN's `run_id` minted in
  it, before the ladder runs (DL-156, DL-315). A start recovered
  this way is then a pending SPAWN like any other;
- a kill the engine decided and recorded, then died before delivering, is
  re-driven (DL-96);
- the live cleanup kills what is left of a command whose attempt ended
  with the engine, as at a live record; nothing waits for a wrapper
  (DL-226, DL-433);
- a pending SPAWN is re-driven (DL-102);
- the supervised-SPAWN replay rule is retired (DL-424; DL-129, DL-315);
- an FW watch is resumed from its log, or, with no trace and an applied
  SPAWN, dispatched again under that SPAWN's `run_id`, because a watch is
  an idempotent read (DL-44 item 7, DL-129). A watch whose SPAWN was
  retired, or that no decision gave a SPAWN, is not launched
  (DL-315);
- the live-wrapper kill re-drive is retired (DL-424; DL-133).

Nothing else. The detached-run kill re-drive rule is retired (DL-424).

`dsl41 runs` is not a new record kind: its rows are a projection folded from
the records above (`dispatch`, `input(kind=STATUS)`, and each `decision`,
both the effects nested in it and its verdict) plus the replayed trace and
the spool, offline, with nothing appended to the store (DL-113). The
**verdict** is read for the same reason §4's gate writes it (DL-151): a
completion that gate REJECTED never reached the oracle, so the fold skips it
too; read without the verdict, a late `exit 0` would decide the row over the
real FAILURE.

**The fold decides the crash window itself** (DL-156). An attempt whose
`decision` record was never written is re-decided through the §4 gate on
replay. The full-fidelity fold uses those recovered verdicts
(`Replay.recovered`, returned by `replay_trace`). A recovered rejection is
skipped exactly as a durable one, and a recovered application decides the
row. This adds no new authority: it is the same gate over the same records,
deterministic and version-gated (`check_replay_version`), and a resume
derives the same verdict from the same log. A fold reading records alone cannot run the gate and REFUSES
to decide instead: the row's status stands on what the records do decide,
an earlier durable verdict or the pre-completion RUNNING, and it carries
`undecided`, so the operator is told the newest completion did not decide
it. Only an explicit or recovered `rejected` is skipped.

The row is `{job, run_number, catalog_hash, job_hash, started_at,
ended_at, duration_s, status, exit_code, started_by, run_dir,
box_name, clock_source, fidelity, undecided}` (DL-113, DL-136, DL-141). `clock_source` says whether the timing came from
the wrapper's spool or fell back to the store; `fidelity` says how much of
the row could be established when the period's manifest is gone. The
caller names the run roots, or names the lineage anchor alone and the
registry supplies every root in period order. The stable key across a
lineage is `(estate, job, run_number)`, with the estate supplied by the
lineage named, not by a field. An open run gets a row with a null
`ended_at` and a null duration, never a fabricated one. A run that spans a
boundary keeps its row in the period that dispatched it and stays RUNNING
there, because its terminal input is in the next segment. Every CMD and FW
start gets a `dispatch` record, so a file watch has a row like a command. A box
gets no `dispatch` record, so its row comes from the replay, as does a leaf run that
KILLJOB or a `term_run_time` kill ended. A duration series breaks where
the row's `job_hash` moved, falling back to `catalog_hash` only when either
row lacks a job hash; both ride on every row in the JSON and CSV formats,
so an external consumer can segment either way; the table shows
`catalog_hash` and marks a break where `job_hash` moved, or where
`catalog_hash` moved for a row without a job hash. A start that produced
no run is not a row: a dropped tick, a held job and a refused command are
`drop` records and trace entries, and "why did it not run" is a different
question from "how long did it take". dsl41 emits these facts and
decides nothing about them: trends, thresholds and paging are the site's
monitoring (§12). Whether a migrated estate expects parity with the
vendor's own run-history surface is E22 (§15).

**Inputs-only principle**: emitted events and the trace are pure functions
of the input sequence, external events plus time observations (oracle
determinism). Thus they are never recorded in the store. `dsl41 journal` replays
inputs and advances through a fresh Oracle to reconstruct the full trace.
One source of truth, no divergence possible. Write-ahead discipline: fsync
per record before `feed()`/`advance()` in run mode, batched in rehearse.

**Self-contained artifact** (DL-66, DL-130): `dsl41 run` materializes the
inputs into `catalogs/<source_bundle_hash>/` before baselining, the
post-placeholder JIL the run loaded (byte-exact, F1) plus `sources.json`
(the ordered vector of original paths and their sha256), and the engine
installs `periods/000001/manifest.json` (`artifact_format_version`,
`catalog_hash` + its version, `source_bundle_hash`, the `RuntimeProfile`
and its `runtime_hash`, `state_machine_version`, and the five fields only
the opening knows: `period_id`, `baseline_id`, `clock_domain`,
`segment_no`, `first_index`). The bundle is addressed by its own bytes, so
a relaunch on unchanged inputs reuses the directory rather than rewriting
it. The run root outlives the estate files it was launched from. A
`manifest/` directory is a retired layout (DL-138): it is neither written
nor read, and a root that has one where the period manifest is absent is
refused naming the layout (`docs/protocol-evolution.md`). The catalog hash
covers `SourceSpan.file`, so byte-exact replay against relocated copies
still needs the recorded original paths. Relocation-independent hashing
is deliberately not built: it would fail every existing store's resume
gate.

**Permissions** (DL-66): run roots are `0700` (created and re-tightened
at resume); the store, wrapper spool files, and job stdout/stderr are
`0600` at creation. The store carries globals and every control input, and
job output carries whatever commands print: owner-only, not
umask-hopeful. One deliberate exception: arming the DL-146 perimeter with a
named `socket_group` opens the run root to `0710` for traversal and the
socket to `0660`, and tightens every direct child first
(`docs/access-model.md` §8). Nothing else moves. The anchor-refusal write list is retired (DL-424; DL-224).

**Resume** (`dsl41 run <files> --run-root <root> --resume`):

The anchor, seal and segment resume rules are retired (DL-424; DL-133, DL-134, DL-138, DL-224).

1. On catalog-hash mismatch, refuse: no silent semantic drift. A changed
   estate re-baselines explicitly. A clock-domain mismatch refuses on the
   same pin: a real run and a rehearsal are not one record.
2. Replay inputs in seq order through a fresh Oracle (original timestamps).
3. Reconcile from the §6a records, sweep = union(store dispatch records,
   `runs/` directory). The supervisor LIST rule is retired (DL-424). In
   tethered mode, the wrappers self-terminated their groups and recorded
   that fact when the engine died (lifeline EOF). Thus resume normally only
   *reads* outcomes. Signals are for the residual crash matrix only. First
   the boot_id shortcut: a spawn record whose boot_id differs from the
   current boot means that the machine rebooted. Then nothing survived:
   skip all liveness checks and resolve each run from status.json, or end
   it `unobservable{engine_death}` under the interruption rule (DL-433).
   Otherwise, per incomplete run, in order:
   - The engine waits for no wrapper (DL-433): each open attempt's spool is
     read once, as a step of the engine's loop, and an attempt with no
     record ended with the engine, `unobservable{engine_death}`. A record
     its wrapper writes later is read by nobody.
   - `status.json` present → the attempt chart reads it: a natural end
     feeds the real completion (raw exit_code through SEM-09 as usual) at
     `max(ended_at, last store at)`, with the true `ended_at` carried in
     the payload (feed times are non-decreasing); `parent lost` or a
     recorded stop is `stopped`, and the interruption rule plans it.
   - No record → the attempt ended with the engine,
     `unobservable{engine_death}` (or the cause of the stop it took
     first), and the interruption rule plans it: rerun the same run as
     the next attempt, hold a job named in `non-idempotent.toml`, or
     nothing where the oracle ended the run (DL-424).
   - Something of the command left → the live cleanup: the ladder TERMs,
     waits the grace and KILLs what a reading finds left, each process by
     itself where the cookie decides, else the group while its leader is
     still the recorded process (DL-226, DL-431); a survivor or a failed
     reading holds the attempt, and its job starts nothing meanwhile.
   - The supervised-SPAWN replay rule is retired (DL-424; DL-129, PR-36a).
   - A command attempt with no record is never `exit_status_unobservable`
     (PENDING: E7, which DL-424 narrows): the interruption rule above plans
     it. Not TERMINATED either: that status is reserved for kills that the
     engine or the oracle actually performed or observed.
   All reconciliation injections are recorded in the store with source=reconcile.
   The supervisor reattachment rule is retired (DL-424).

**How the restart knows a process is ours.** On Linux, every process of a command attempt carries the process cookie,
`DSL41_COOKIE=<root_id>.<run_id>` ([wrapper protocol](wrapper-protocol.md),
DL-431, DL-433); `root_id` names the run root.
The engine sets it for the wrapper and the command, and the command's
children inherit it. The `dispatch` record holds it, so the restart knows
each open attempt's cookie. What is left of an attempt is its recorded
group's live members and every process that carries its cookie, whatever
its group, the wrapper left out; a failed scan is unknown and releases
nothing. The restart stops what it finds left, one process at a time, as
the live engine does, unless `process-leak=leak` (§8a) and the outcome is
a natural exit. A group whose leader fails its start token and none of
whose members carries the cookie is left alone: it may be the attempt's
own cookie-less child or a stranger's that took the id after a pid wrap,
and nothing tells them apart. A process outside the group that the scan
cannot read is beyond the engine by design; the unit's cgroup is the
stronger fence. Each signal uses a pidfd where supported. Without pidfd support,
the engine re-reads `/proc/<pid>/stat` field 22 through `proc_start_token`
and calls `os.kill` only if it still matches the scanned start token.
The race between this check and the signal is accepted: the engine is an
orchestrator, not a jail. TERM, grace, KILL and the held poll use the same
scan with either primitive. One log line per serving start with a run root
names the primitive; in-memory rehearsals print none.
On other platforms, and for an attempt dispatched before the cookie, the (pid, start-time)
checks above decide.

The supervisor live-wrapper kill re-drive rule is retired (DL-424; DL-133, PR-33).

The multi-host takeover barrier and supervisor LIST sweep are retired (DL-424; DL-102, S6c).

**A live start with no run directory is decided by the log** (DL-102, DL-315). A start's
`dispatch` record is durable before its adapter task exists, and its run directory is made
after, so the log tells three cases apart:

- a `dispatch` record and no directory: the start was launched and the crash took it before
  it made its directory. A tethered command start is FAILED with cause `dispatch lost to
  engine crash (never spawned)`, because a run provably never started is never silently
  re-executed. An FW watch is dispatched again under its recorded `run_id`, because a watch
  is an idempotent read (DL-44 item 7);
- no record, and replay derives the start (`Replay.spawns`): the previous engine decided it
  and died before dispatching it, or the attempt's decision never landed and resume has just
  written it. Nothing ran, so resume dispatches it now, at the run number the oracle already
  decided, through the supersession check a fresh start passes;
- neither: the live engine launched nothing for the run and never would, such as a STARTING
  overwrite at a run that never started. It is left as it is, RUNNING or STARTING with
  nothing running, for the operator to end or restart.

An FW watch leaves a run directory, its spool (§6), so the sweep finds
watches too, and the ladder has two rules for them (DL-129; period-model
§3.5). A dispatched watch whose run directory holds a `start` line carrying
its `run_id` is relaunched as an idempotent read: the adapter reconstructs
its progress from the log and appends no second `start` line, so the fold
stays one watch (PR-34). A `watch.jsonl` whose last line is a **completing** observation
while the row is still RUNNING is the sibling window, the poll appended and
the engine dead before the STATUS input; the completion is **injected from
the log**, exactly as a CMD's is injected from `status.json` (PR-34a).
Re-polling would decide the watch again against a world that has moved on.

## 8. Preflight — refuse loudly, run honestly

This is the backend_uc R/A discipline applied to execution: ERROR refuses
the run, and WARN prints, records in the store, and runs. Every rule ships the house
fixture pair.

ERROR:
- `job_type` outside {CMD, BOX, FW}.
- `machine` set and not local. This rule and the `owner` rule below apply
  to `run` only: rehearse spawns no process, so preflight skips both there.
  Locality is decided against this runner's DECLARED IDENTITY
  (`--as-machine NAME`, repeatable, DL-52). The runner is
  TOLD what machine it is — no FQDN/reverse-DNS guessing. A job whose
  `machine:` IS an identity name runs here directly. Otherwise the name
  resolves through `insert_machine` (DL-49): an agent's `node_name`, a real
  machine's `node_name`/name, or a virtual pool's members (local if and
  only if ALL members resolve to the identity). A split pool is ERROR under
  `--machine-policy strict` and a WARN under `local-eligible`. Identity
  omitted => the forward hostname + localhost (zero configuration, no
  reverse-DNS). An undefined-and-not-ours name is foreign. A bad definition
  (missing `node_name`, empty pool, undefined/nested member, unknown/missing
  type) is ERROR — never guessed. Resolution is preflight-only and
  shell-side. The bisimulation gate is untouched. Remote dispatch (routing
  a foreign machine to another box) stays a §12 non-goal.
- `owner` set and not the invoking user.
- `envvars` set on a CMD job's `ExecSpec`: the adapter never applies it to
  the child environment (DL-240). `run` only; rehearse spawns no process.
- a non-BOX job whose `var_sites` name a field of its exec spec (command,
  std_in_file, envvars, profile, std_out_file, std_err_file, machine,
  owner; watch_file on FW): global substitution is not implemented, so the
  literal `$$NAME` text would be used as-is (DL-240) -- on `command`, for
  example, the shell would read `$$` as its own PID. `run` only; rehearse
  spawns no process.
- a non-BOX job whose `passthrough` carries `chk_files`: the pre-start
  disk-space gate is never evaluated (DL-240). `run` only; rehearse spawns
  no process.
- `run_calendar` / `exclude_calendar` that names a calendar absent from
  the loaded set (L018's lint WARN, fail-closed here — the same strictness
  split as L016-vs-DL-50 resources). Also a calendar reference that
  carries an unparseable date row, or that names an **extended** calendar
  that the interpreter refuses (DL-57/58/59: unknown or doc-defective
  tokens, missing holcal/cyccal dependencies).
  The ERROR carries the interpreter's reason. A valid extended calendar
  passes and is honored (§5). Open composition corners compile on pinned
  defaults and never refuse. The probe also surfaces generation-time
  refusals (for example, an exhausted replacement walk) as ERRORs.
- `timezone` not resolvable through the SEM-35 ladder (DL-62): not a
  `zoneinfo` name (case-insensitive), not in the supplied `--timezone-map`,
  not a POSIX fixed offset, and -- without a map -- not a unique zoneinfo
  city match (a unique match resolves with a WARN, not an ERROR). The
  message names the applicable remedy (`autotimezone -l` via
  `--timezone-map`; the full zone name for an ambiguous city).
- `resources:` that requires a resource with no `insert_resource` in the
  set, or with no parseable `amount` — an unsized semaphore cannot be
  honored (DL-50: fail-closed, stricter than L016's warn). A
  `--resource-capacity` override is not built.
  Unknown `res_type` (not R/D/T). FREE=Y or FREE=A on a `res_type: D`
  resource: release semantics undocumented (DL-287, Q12); FREE=N accepted.
  The same resource named twice in one `resources:` list — the demand is
  ambiguous. A `QUANTITY` above the
  resource's `amount`, at any priority — the job would wait in QUE_WAIT
  forever, and at a positive priority it would block every lower priority
  that names the resource (DL-255). A
  `job_load` above its machine's `max_load` on a job with a positive
  `priority` — it would wait forever and block every lower priority on
  that machine (DL-247); priority 0 or unset skips the load check and is
  exempt, and so is a pool machine. Malformed (non-integer) `job_load` / `priority` / machine `max_load`.
  Refused in BOTH run and rehearse
  (resource semantics gate the oracle in either clock domain).
- Oracle construction failure (surfaces IR-level refusals unchanged).

WARN:
- `run_calendar` whose eligible day set (run minus exclude) is empty, or
  lies entirely before the run/rehearse `start` anchor (run: wall-now,
  rehearse: virtual `--start`). Then the job never fires (DL-56 dormant
  semantics — silent-never-fires is preflight's business). Extended
  sources probe their generator from the anchor instead (DL-57). An
  omitted anchor means now, so the probes run on every call (DL-213).
  The anchor is read as the job's local day, else the run's base zone,
  else UTC — the scheduler's own ladder, so the WARN names the day the
  engine names (DL-212).
- an **extended** `exclude_calendar` that covers every eligible day within
  two years of the anchor, on a job whose source is `days_of_week` — a
  standard exclusion can never cover a weekly source, an extended one can
  (DL-57). Two years is the probe bound, so the WARN says the job never
  fires within it.
- `timezone` read as a POSIX fixed offset with a NON-ZERO offset — the sign
  is west-positive, so `GMT+5` means five hours WEST of GMT. The WARN
  prints the offset it resolved to. A zero offset has no sign to misread,
  so it passes silently. A unique zoneinfo city match WARNs on a different
  principle: the name was guessed, and the remedy is `--timezone-map`.
- `n_retrys > 0` — the job runs WITHOUT retries (Q4, DL-53: retries are
  unmodeled by scope decision).
- `job_load` on a **pool** machine — the machine-load throttle is
  unmodeled for pools (DL-50, PENDING Qr3). Resource semaphores on such a
  job still apply. Pools stay outside both load rules below, so this WARN
  fires for a pool job at priority 0 too.
- Cycle in the AND-success skeleton (graphlib `CycleError`): cycles are
  *legal* AutoSys (edge-triggered re-runs, DL-13, L010's territory). Thus
  this rule warns and disables `plan`, and it does not refuse.

graphlib's role is deliberately bounded to that skeleton check plus `plan`
(wave-by-wave `get_ready()` batches for acyclic estates). General
eligibility is predicate evaluation over the status store. That is the
oracle's edge-triggered referencer machinery, not a topological order.

**Load and resource rules the oracle applies.** These are not preflight
items. Plain `job_load`, `priority` and `resources:` are honored (DL-50)
and draw no WARN. An unsized, unknown-res_type or malformed resource is an
ERROR (above).

The oracle applies the vendor's load rules (DL-247). Only a job with a
positive `priority` checks its `job_load`: "The scheduler ignores any load
unit values defined for the job or machine when the job has a priority
value of zero", and 0 is the default. That job's load still counts against
the machine: "even when jobs have a priority of 0, AutoSys Workload
Automation tracks job loads on each machine". A FORCE_STARTJOB "runs even
if its load exceeds the machine's max_load value"; its units are held the
same way. A job waiting for load "automatically blocks all the lower
priority jobs that specify the same machine attribute value", on a fresh
start and on readmission; a positive priority is blocked even without a
`job_load`. Named resources gate every start, forced or not, with one
vendor exception: a FORCE_STARTJOB of a FAILURE or TERMINATED job that
still holds resource units starts on them and does not re-evaluate its
other resources (DL-256).

The oracle applies the vendor's named-resource rule too (DL-255): "A job
in the RESWAIT state for one resource name automatically blocks all the
lower priority jobs that specify the same resource name. It does not
automatically block higher or equal priority jobs that specify the same
resource name or a job that specifies a different resource name." The
blocked job has a positive priority and may be forced; a forced start on
held units is not checked at all (DL-256). The blocker has a positive
priority, names a resource the blocked job names, is short on any resource
it names (Broadcom KB 240816, AutoSys 12.0: a job waiting for its second
resource blocks jobs that need only its first), and has passed its load
check. That is, its load fits and no higher-priority load waiter blocks
it, since jobs still in QUE_WAIT for load "do not automatically block
lower priority jobs that specify the same resource attribute". A blocker
that holds units from an earlier run counts them as its own, as its
admission does (DL-256). A queued job holds no load: jobs "that enter the
RESWAIT state after the load balancing attributes are successfully
evaluated do not consume any load units". A start or enqueue that takes
machine load can send a resource waiter back to its load check, so it
owes an admit-only queue scan. The input pays that scan after every
referencer it woke.

Held resource units (DL-256). A renewable resource's units that a run's
FREE policy does not release stay held by the job after the run: FREE=N
always, and FREE=Y, or an omitted FREE under `renewable-free=Y`, after
FAILURE or TERMINATED. They count against the resource until the operator
sends `RELEASE_RESOURCE` for the job, or the job's next run takes them
over. That run re-uses them for the same resources and is never charged
twice. A depletable resource's units are spent.

## 8a. Semantic switches

A semantic switch names one place where dsl41 can read an estate two
ways. The rule for defaults (DL-252): where AutoSys behavior is
documented, dsl41 follows it. Where dsl41's own choice is safer or more
useful, dsl41 keeps it. Either way, the other reading can be selected
without a code change, so an incompatibility found after rollout can be
flipped by the operator.

The switches form a closed registry in `src/dsl41/semantics.py`. Each
entry has a name, its allowed values, its default, the value that matches
documented AutoSys behavior, and a one-line description. An unknown name
or value is refused with the allowed names or values. Nothing is dropped.

Set a switch with `--semantics NAME=VALUE` on `dsl41 run` and
`dsl41 rehearse`. The option is repeatable. `dsl41 seal` takes
`--next-semantics NAME=VALUE` for the period it opens. `dsl41 run --help`
lists every switch and its values.

Overrides are recorded in the period's runtime profile (`semantics`,
period-model §2.1), so they are part of `runtime_hash`. An explicit
default, such as `ice-lookback=true`, is the same as no override. Replay, `journal`,
`runs`, audit and the boundary classifier read the switches from that
profile, and so use the values the engine ran. A resume or a period
opening with different switches is refused, the same as a changed
`--timezone`. A new value takes effect at a period boundary, and the
boundary classifier treats the jobs each switch names as changed: a
running box whose `box_success` reads a lookback atom is refused, an
armed job gated on one is carried with a recorded assumption. For
`queued-recheck`, a queued job is carried with a recorded assumption, and
a running box with a member that can queue is refused. For
`off-ice-in-running-box` and `box-terminator-on-terminated`, the members
the switch names are changed, so a running box that holds one is refused.
For `box-start-all-members-out` and `idle-box-iced-member`, every box is
changed, so a running box is refused. `process-leak` changes no job's
oracle reading: it decides what the engine does with the processes a
command leaves running when it exits, so a flip changes no job.
A switch can also name calendars; then every job that names such a calendar is changed.
Replay
refuses a period whose manifest is missing or is not bound to its
segment, because it cannot know which switches the engine ran. Only
explicit overrides are recorded, and a profile with none writes
`"semantics": {}`.

Defaults live in code. Changing a default changes how existing estates are
read, so it is a state-machine change and bumps `STATE_MACHINE_VERSION`.

Static tools that have no runtime profile use the defaults: `equiv`,
`lint`, `derive`, and the genesis credit of `rehearse --check-cadence`.
Preflight's oracle and calendar checks use the run's switches: `run` and
`rehearse` pass their `--semantics`, and a boundary's preflight the staged
profile's. The cadence sweeps and the expected
tick counts use the rehearsal's own switches. The scheduler compiles
extended calendars under the calendar switches and places start instants
under `dst-start-times` (DL-260). Those switches are read back from the
scheduler into the runtime profile, like its timezone, so a profile that
disagrees with them is refused as drift before anything durable is
written. An engine also refuses a scheduler built under other values of
them, as a backstop. The switch a wired adapter reads (`fw-existence`) is
read back from the adapter the same way, and an engine given a pin or
`semantics` refuses an adapter built under another value (DL-280). A test
scans src for calls that leave `semantics` to
its default; only a short allow-list of static callers may.

| Switch | Values | Default | Documented AutoSys | Why this default |
| --- | --- | --- | --- | --- |
| `ice-lookback` | `true`, `ordinary` | `true` | `true` | A condition atom with a lookback qualifier whose predecessor is on ice. `true`: the atom is true, lookback ignored. `ordinary`: the qualifier is dropped and the ordinary on-ice table applies (s, d, n true; f, t, exitcode false). The "condition Attribute" page (AutoSys 24.2) says "If the predecessor job being evaluated for the look-back condition is currently in an ON_ICE status, it always evaluates to true. That is, any look-back evaluation is ignored." `ordinary` extends the Start Conditions on-ice table (SEM-20), which does not separate lookback atoms, to the lookback atom. Q10 stays open. |
| `renewable-free` | `Y`, `A` | `Y` | `Y` | A renewable resource request (`res_type: R` or none) that states no FREE. `Y`: the units are freed only when the run ends SUCCESS; after FAILURE or TERMINATED the job holds them until `RELEASE_RESOURCE` or its next run. `A`: the units are freed on every completion. The "resources Attribute" page (AutoSys 24.2) gives FREE's default: "Default: Y", where "Y -- Frees the units only if the job completes successfully". An explicit FREE is not affected. Qr1 is decided. |
| `queued-recheck` | `0`, `1`, `2` | `0` | `1` | A job that can queue (it names a resource, or a positive priority makes it check machine load) and leaves QUE_WAIT. The values are the vendor's EvaluateQueuedJobStarts (Administrating > Configure a Scheduler, AutoSys 24.2). `0`: it starts without a recheck. `1`: its `condition`, `run_window` and `exclude_calendar` are checked again, but not `run_calendar`, `days_of_week`, `start_times` or `start_mins`. `2`: `run_calendar` or `days_of_week` is checked for the day too. A job that fails goes INACTIVE without starting, its arm is cleared, and its next start time starts it; a member of a running box that fails its condition waits and keeps the box running, the vendor's ACTIVATED. A `run_window` failure takes DL-246's disposition at that instant (the skip, or one deferral to the opening), and a day failure of a job with no start times of its own is deferred to its next eligible window opening, so no job waits for a tick that never comes. The vendor's default is `1`. dsl41 keeps `0`: a queued job met its conditions when it started. Qr6 is decided (DL-50, DL-257). The day and window checks use the oracle's zone, which DL-253 aligns with the scheduler's base zone. |
| `fw-existence` | `stable`, `immediate` | `stable` | `immediate` | What an FW job with no `watch_file_min_size` does once the watched file exists. `stable`: wait for the size to stay steady across two polls, like a job with a minimum size — dsl41's own choice, because a file still being written is not complete. `immediate`: complete at once, `watch_interval` ignored — the vendor reading. The "watch_interval Attribute" page (AutoSys 24.2) says "If you are monitoring for the existence of a file (not the size) and the file already exists when the job runs, the job completes immediately. The watch_interval attribute is ignored." The "watch_file_min_size Attribute" page says "If you do not specify the watch_file_min_size attribute in your job definition, the job completes if the file exists (the default)." A job with a minimum size is unaffected by this switch either way (§6, E6). |
| `wekr-first-week` | `first-full`, `partial` | `first-full` | unknown | Where week 1 of a WEKR token's year starts (SEM-37). A WEKR token selects a week of the year whose weeks start on the anchor day. `first-full`: week 1 starts on the first anchor day on or after January 1, and the days before it are in no week, as in the C library's `%U`/`%W` numbering. `partial`: week 1 runs from January 1 to the day before that anchor day. The vendor text does not say; its example, "consider full weeks as those that start on a Monday", supports `first-full`. Both readings agree when January 1 falls on the anchor day, and both count `Mnn` back from the week that holds December 31. The switch reaches every job whose `run_calendar` or `exclude_calendar` has a WEKR token. Q11 is closed (DL-259). |
| `dst-start-times` | `vendor`, `fold0` | `vendor` | `vendor` | `start_times` and `start_mins` on a one-hour DST change at 02:00 local, the shape DL-249 detects. `vendor`: a start time in the repeated 01:00-01:59 runs once, in the second (standard time) pass. `start_mins` run in both passes. A start time in the missing 02:00-02:59 runs in the first minute of 03:00, its minute read as seconds (2:05 at 3:00:05), and only the first such start time runs. `start_mins` ticks in the missing hour do not exist. `fold0`: the PEP 495 fold=0 reading. A repeated time runs once, in the first pass, and each missing time runs past the gap (2:05 at 3:05). "Standard Time Changes" and "Daylight Time Changes" (AutoSys 12.1 and 24.2) document the vendor rules. Other change shapes use fold=0 under both values; that is unverified. The scheduler and the oracle read the same value: the runtime profile reads it back from the scheduler, so a disagreeing scheduler is refused before anything durable is written, and the engine refuses one as a backstop. |
| `off-ice-in-running-box` | `next-run`, `same-run` | `next-run` | `next-run` | A job inside a box, taken off ice while its box runs and before it ran in that run. `next-run`: it sits the run out. A plain start of it is refused, the box completes without it while it reads INACTIVE (a terminal status given to it votes, DL-360), FORCE_STARTJOB still starts it, and the box waits while that forced start is queued; the box's next run clears the mark. "Start Conditions" (AutoSys 24.2 and 12.0) says "If a job is contained in a running box when it is taken off ice, the scheduler does not restart the job until the following run of the box, even if its starting conditions recur during the existing run of the box." `same-run`: it re-enters the run and may start on its condition's next edge, and the box waits for it; the reading of the AutoSys 24.0 Web UI help. SEM-20. |
| `box-terminator-on-terminated` | `true`, `false` | `true` | `true` | A box member with `box_terminator`, a subbox included, that ends TERMINATED: killed, given the status, killed while queued, or ended by `term_run_time`. `true`: it terminates its running box, as a FAILURE does. "Force the Job or the Box to Stop Running" (AutoSys 12.1 and 24.2) says "if the job completes with a FAILURE or TERMINATED status, the box terminates." `false`: only a FAILURE does, the reading of the value line of the 24.2 `box_terminator` reference page. SEM-14. |
| `box-start-all-members-out` | `complete`, `wait` | `complete` | unknown | A box whose start leaves no member in the run: every direct member on ice, or no members. `complete`: the start is a completion moment, so the box completes at once through the completion door, overrides first, then the default fold, which is SUCCESS over no member that ran. `wait`: the box stays RUNNING until an operator acts; the rest of DL-304 (an iced member that is live, or holds a live or queued job, is not out of the run) applies under both values. No vendor sentence names the case. "Basic Box Job Concepts" (AutoSys 12.0 and 24.2) keeps a box RUNNING "as long as there are jobs in it with ACTIVATED or RUNNING status", and "Events" (12.0 and 24.2) says of JOB_ON_ICE: "If the job is in a box, the scheduler does not execute the job for the entire run of the box." The runbook's Q15 protocol settles which value AutoSys follows. SEM-11, DL-304. |
| `idle-box-iced-member` | `ignore`, `vote` | `ignore` | unknown | A box that is not running and re-derives its status when a member changes (SEM-15). `ignore`: an iced member that is out of the run, on ice with nothing inside it live or queued, is dropped from the re-derivation, as an INACTIVE member is, so a completed box does not flip when a job inside an iced subbox, or an iced member given a status, ends later. Nor is such a member's own change a verdict: if no other member votes, the box keeps its status, so a box that never ran stays INACTIVE; when the member that changed is in the run, DL-242's vacuous SUCCESS stands. An iced member that is live, or holds a live or queued job, is not dropped: its own status votes, and a live one blocks the re-derivation. `vote`: the iced member's status counts. A member that is not iced votes under both values. "Basic Box Job Concepts" (AutoSys 12.0 and 24.2) ignores only INACTIVE members and does not name ON_ICE; "Job States" (12.0 and 24.2) says an ON_ICE job "is removed from the job stream but is still defined", and SEM-11's fold already skips such a member (DL-304). The runbook's Q16 protocol settles which value AutoSys follows. SEM-15, SEM-20. |
| `process-leak` | `terminate`, `leak` | `terminate` | `leak` | The processes a job's command leaves running when it exits: what is left of its recorded process group and every process carrying its `DSL41_COOKIE` (the wrapper protocol). `terminate`: a leak, stopped like any stop, TERM, `grace_term_s`, then KILL, each process by itself; the outcome stays what the command returned, the attempt closes with `ladder_done`, and the job's next run waits for it. `leak`: left running with a warning naming the job, the run and the pids, and the attempt settles at once; the drain and the restart leave them too. KILLJOB "kills the currently running process and the associated process group but does not kill orphan processes" (AutoSys events reference); a command that exits on its own leaves its processes running under the vendor. Linux with either pidfd or start-token-checked `os.kill`; elsewhere the leader and the group decide as before the cookie. |

## 9. Time domains (E2)

`Clock` protocol: `virtual`, `now()`, `wait_until(t, interrupt)`,
`sleep_until(t)`, `next_sleeper_due()`, `pending_sleepers()`. The first
three are the engine's; `sleep_until` is the adapter's only legal block; the
last two are the virtual domain's bookkeeping, and the real clock answers
None and 0 to them. **RealClock** sleeps
(interruptible by queue activity). **VirtualClock** jumps to the next wake
instantly. The oracle takes explicit timestamps everywhere, which makes
this jump possible.

In the real domain, a backward wall-clock step does not refuse an input
(§4). The loop and enqueue path report a warning on stderr with the fixed
prefix `dsl41: WARNING:`, the wall reading, admission frontier
and gap in seconds on the first observation below the frontier and on each
further observed backward step there. Inputs while the wall clock catches
up do not repeat the warning. A broken stderr does not stop the engine.
The observation state is local to the engine
process; it adds no store record or status field. Oracle time stays at the
frontier until the wall clock passes it. The virtual domain keeps its
existing stamp and jump rules. A real-domain restart below the store's last
stamp restores that admission floor and reports the step. It starts without
waiting for the wall to catch up. Store identity, configuration, version
and clock-domain checks still apply.

- `dsl41 run` — RealClock + real adapters + control socket.
- `dsl41 rehearse` — VirtualClock + FakeAdapter + optional scenario, in
  batch mode. It exits when quiescent (no queued events, no due timers,
  no occurrence within the horizon). Same engine loop, byte-identical
  trace format. `--check-cadence` (DL-182/DL-184) compares each job's
  observed run count — a run_number delta — against a typed cadence
  bound and exits 3 on deviations; `--cadence-policy` declares intended
  exceptions; `--sweep fail` replays the estate once per start-gate
  producer with that producer's first run scripted to a SEM-09 FAILURE
  and reports the suppressed-run inventory (the dynamic L022);
  `--sweep flags` replays once per (global, region value, reset
  variant) on the equivalence literal regions, checking global-gated
  consumers under the pinned flag (the L021 globals exclusion,
  recovered dynamically). The expectation side lives in
  `rehearse_check.py`.

## 10. Control plane

The control plane is a Unix domain socket in the run directory, mode 0600
(0660 to a named `socket_group` when the DL-146 access perimeter is armed —
`docs/access-model.md` §8), with a JSON-lines protocol.

There is one mutating command — `sendevent` — a set of queries, and the
streaming `subscribe`. `docs/control-protocol.md` is the
frozen inventory; what follows is what each is for.

- **sendevent parity** (maps 1:1 onto oracle EventKind): STARTJOB,
  FORCE_STARTJOB, KILLJOB, ON_ICE/OFF_ICE, ON_HOLD/OFF_HOLD,
  ON_NOEXEC/OFF_NOEXEC, DISARM (DL-158), RELEASE_RESOURCE (DL-256),
  SET_GLOBAL, CHANGE_STATUS (inject STATUS).
- **Queries**: `status [job]`, `trace [--since seq]`, `explain <job>` — the
  job's condition shown with per-atom truth over the current store (the
  Cond IR makes this nearly free), `spec <job>` — the preserve-rendered
  post-placeholder JIL block the engine loaded (DL-64; served from an
  optional spec_texts map, jil:null when the embedder has no source),
  `deps <job>` — upstream entities the condition references plus the
  downstream jobs whose conditions reference this one, straight from the
  oracle's edge-trigger index (DL-65: the blast-radius view), `timers` —
  every pending oracle timer plus each scheduled job's next calendar
  tick, due-ordered (DL-65); `due` is nullable (DL-68): live
  filewatches join as trailing rows with `due: null` (they fire on a
  file, not a clock), `global <name>` / `globals <names>` — a named
  global's value and its revision, inserting nothing (DL-87), and `plan`
  (acyclic estates only). The
  status response also carries per-job `job_type`/`box_name` (the §11
  tree), `started_by` — the trace cause of the most recent actual start
  — `state_rev` (the revision an `expect` may name, DL-87), `held` (always
  false: one engine dispatches every start it decides, DL-94), plus,
  for a live FW run, a `watching {file, interval, min_size}`
  object (DL-68), and a `spec_drift` flag — a lazy fingerprint re-check of the
  loaded input files; there is no reload, the flag tells the operator
  the running catalog does not match the disk (DL-65). The CLI adds
  scriptable predicates `is-success`/`is-failed` (print status, exit
  0/1) over the status verb.
- **subscribe** — stream the store's wire records from a seq (the UI feed;
  control-protocol §5, DL-428).

Every control input is recorded in the store like any other injected event
(source=control). The store is the audit trail of engine decisions — no
second log carries them. Access decisions at the DL-146 perimeter go to
the perimeter journal (`docs/access-model.md` §6) and never enter the store
(DL-148).

## 11. UI — one Textual app, terminal and web (E3)

`runner_tui.py`, an optional extra `dsl41[ui]` (textual, plus
textual-serve for web). The core package keeps its three runtime
dependencies.

The app is a **client of the control socket only** — never in-process with
the engine. This split is forced, not stylistic. textual-serve spawns one
app instance per browser session, so an in-process engine hands every
viewer a private universe. Views: jobs table (status, run_number, pending
timers/alarms), explain pane ("waiting on: s(A) ∧ ¬f(B)" with live truth),
log tail (the §6 std files), event console (sendevent verbs), job-details
popup (the `spec` block + `deps` needs/blocks lines + a short log tail),
and the read-only triggers view on `t` (the `timers` verb live with
countdowns, plus one row per armed job — DL-68).
The header clock is UTC — the engine's time basis, one time base on
screen (DL-64). Pane geometry is keyboard-only: maximize toggle on the
log tail plus fr-share nudges for both splits. Navigation at estate
scale (DL-65): the table renders the box tree from the status response's
box_name (fold a box with space, all with z; a folded box shows its
hidden count and a red problem tally), `/` filters by name, `v` cycles
all → problems → active — filtered and non-all views flatten so a match
never hides inside a fold. The console focuses with `:`.
Verb keys act only while the jobs table has focus; KILLJOB and
FORCE_STARTJOB from a key confirm first, showing the target, its box
members, and the revision the request will name, frozen when the modal
opens (DL-187). Escape cancels a console line. F1 toggles the help panel.
A refused query is reported in the console and the subtitle, never shown
as an empty estate.
The trace cursor and alarm tally reset (DL-210) on a changed `baseline_id`
or disagreement between the status and trace baselines, with one
console notice and a read from zero on the next poll; a same-period restart
preserves the cursor because replay restores the whole trace prefix.

`dsl41 run --ui` starts the engine and attaches the TUI in the terminal, and
that terminal owns the run. `dsl41 ui --socket <path>` attaches the same app
to an engine already running, from another terminal; quitting detaches the
viewer and leaves the run alone. The footer says which: `q` reads "stop
run" and confirms under `run --ui`, "detach" under `dsl41 ui`. A crashed
TUI exits non-zero in both postures (DL-187).
`dsl41 serve --socket <path>` wraps textual-serve around the same app. Web
posture (E3): textual-serve ships no auth. Deploy it behind a reverse
proxy or an SSH tunnel; the README's deployment notes show both, and
`docs/access-model.md` §9 is the web tier. dsl41 builds no web
authentication of its own.

Scope fence (DL-42): the dashboard of *meaning* — conditions, boxes,
explain, sendevent — is dsl41's and stays here. The supervisor dashboard
scope rule is retired (DL-424).

## 12. Non-goals

The non-goals: HA/clustering, and remote machines or agent fabric. Access
control is not a non-goal: `docs/access-model.md` (DL-146) puts three tiers
at the perimeter, and the core stays authz-free.
Also the refused extended-calendar residue (standard calendars are honored,
DL-56; the SEM-36..39 extended-calendar freeze is interpreted, DL-57; open
composition corners run on pinned defaults, DL-59; only doc-defective
tokens stay materialize-on-a-live-instance).
Also retry semantics (Q4, DL-53: kept deliberately unmodeled by scope
decision). Also non-child orphan adoption (E4).
The supervisor reattachment rule is retired (DL-424).
Also alarm delivery beyond the replayed trace and the UI (no mail/pager
integrations; an alarm is never a store record of its own, §4).
Also cgroup/scope containment (documented Linux hardening path, §6a).
Resource/load management is single-node (DL-50): the oracle honors it as
capacity buckets, and preflight refuses the unmodelable. Out of scope:
DEPLETABLE replenishment (mid-run `update_resource` = SEM-16), and
cross-node resource coordination (covered by the foreign-machine refusal;
a distributed concern, DL-49).

## 13. Testing — bisimulation is the acceptance gate

1. **Bisimulation**: every SEM trace test is parametrized over
   Oracle-direct and Engine(VirtualClock, FakeAdapter). Traces must be
   identical. This is equivalence tier c between simulator and executor,
   and it reuses the oracle's whole fixture corpus. It is the engine's
   definition of done.
2. **Hypothesis**: random event scripts (the oracle's strategies) run through
   both paths with the same property. The suite also compares feed-only
   and advance+feed timer schedules.
3. **Store**: replay reproduces the trace (property test). Crash-recovery
   integration test: real sleep jobs, SIGKILL the engine, resume, assert
   reconciliation records and terminal states.
   The lifecycle tier lives or dies on its failure matrix. Thus the
   wrapper suite tests kills at every phase boundary, not
   only mid-run. The boundaries:
   before/after spawn.json, after fork before exec, after wait
   observation before status write, and after status write before reap.
   They also test
   ENOSPC on the runs dir, a stale control socket, pid reuse (spoofed
   spawn.json), and a simulated reboot (boot_id flip).
4. **Preflight**: trigger/non-trigger fixture pair per rule.
5. **Adapters**: pgid kill, append semantics, profile sourcing. Also the
   FW watcher over a tmpfile that grows to stability.
6. **TUI**: pure view tests over the parser and the control client, plus
   textual pilot tests that press the keys — operator verbs, navigation,
   details, triggers, pane geometry, the log pager. There is no visual
   snapshot suite.

## 14. Module layout and phasing

The house layout is flat, with no `runner` subpackage: twenty
`runner*.py` sibling modules. Each module owns one seam (DL-74, DL-78);
the entry that built a module documents it. Nothing is re-exported: every import site names the module that owns the symbol, so
the split cannot decay into a second name for one file.

- `runner.py`: the §4 engine loop.
- `runner_control.py`: the §10 control plane, the socket server, its wire
  vocabulary, and both clients; frozen in `docs/control-protocol.md`, the
  control contract. The supervisor protocol is retired (DL-424).
  The server owns its own file (DL-78): every query handler is a pure
  projection, and what it shares with the loop is the loop's *task*, not
  its single-writer invariant.
- `runner_clock.py`: the §9 clock domains, plus `EngineError`, which every
  runner module above it raises.
- `runner_codes.py`: the stable codes of the control protocol's `ok: false`
  answers and the append-only set a rejected decision stores (DL-272). It
  imports nothing from dsl41 and sits below `runner_clock.py`, at the
  bottom of the import graph.
- `runner_scheduler.py`: the §5 scheduler, turning its ticks into UTC
  instants through `timezones.py` (SEM-35 name resolution and the one
  naive-UTC ↔ local conversion, phase-free and shared with the oracle,
  DL-163).
- `runner_adapters.py`: the §6/§6a adapter contract and every adapter.
- `runner_journal.py`: the §7 store records, wire projection and replay (DL-428).
- `runner_preflight.py`: the §8 rules.
- `runner_wrapper.py`: the §6a Tier-0 shim, stdlib-only with no third-party
  imports; its dumbness is a correctness property.
- `runner_procid.py`: the process-identity helpers the wrapper uses,
  stdlib-only for the same reason (DL-72).
- `runner_tui.py`: the §11 TUI (guarded textual import).
- `runner_startup.py`: taking possession of a run root: genesis, resume and
  the takeover barrier (DL-106). §7's resume ladder lives there, not in
  `runner.py`.
- `runner_admission.py`: the one admission order (`docs/concurrency-model.md`
  §4).
- `runner_ledger.py`: one engine per run root: the lock and the version
  pins (DL-99).
- `runner_history.py`: the offline `dsl41 runs` projection (DL-113).
- `runner_access.py`: the control-socket access perimeter
  (`docs/access-model.md`).

The CLI keeps one module per domain (DL-137). `cli.py` builds the app and
registers every verb; `cli_common.py` holds the shared options and exit-code
readings, and `cli_compile.py` the compiler verbs. The runner and its
estates use these:

- `cli_run.py`: `run`, `rehearse`, `journal`, `runs`.
- `cli_control.py`: `sendevent`, `off-hold-all`, `ui`, `serve`,
  `query`. The `supervise` verbs are retired (DL-424).

Module docstrings name the runner's build phases 11a to 11f; DL-41 and
DL-41a map them to parts.

## 15. Open questions (E-series)

The house rule applies: implemented defaults are marked `# PENDING: En` in
code. None is guess-resolved. A settled entry keeps its token and states
its rule, with the entry or section that holds it.

- **E4** — on the tethered path, engine death terminates jobs and resume
  uses §7's reconciliation ladder (DL-41a, DL-424). The supervisor
  reattachment rule is retired (DL-424; DL-48, DL-396).
- **E5** — profile sourcing failure semantics [?]. Default: the job fails
  with sh's exit code (§6).
- **E6** — FW steady-size semantics are the `fw-existence` switch (§8a,
  DL-258): default `stable`, two stable polls; `immediate`, the vendor
  reading, applies only to a job with no `watch_file_min_size`. The default
  `watch_interval` stays open [?]: the "watch_interval Attribute" page
  (AutoSys 24.2) gives "Default: 60", but on an agent, "Define a File
  Watcher Job" says a job with no watch_interval checks the file every 30
  seconds. The adapter's 60 s default stays pinned until a decision
  resolves that disagreement (DL-250).
- **E7** — verdict for an unobservable exit status (§7). Default: FAILURE
  with cause `exit_status_unobservable`. TERMINATED is reserved for kills
  that actually happened. The vendor's "Lost Control" is the same
  verdict: a job whose PID the agent can no longer find, and whose exit
  it never recorded, fails with a JOBFAILURE alarm (KB 11501, DL-226). If
  an estate shows t()-conditioned recovery paths that are intended to
  fire instead, revisit this default.
- **E8** — verdict for an EXTERNAL signal death (the wrapper records
  `signaled`, the engine is alive, no oracle kill decision: segfault,
  OOM kill, operator `kill -9` of the command). Default: TERMINATED,
  uniform with the DL-41a recorded-signal reading. The vendor's status
  definition lists "you issue the kill command (UNIX)" among the
  situations that make a job TERMINATED, beside the KILLJOB event, the
  job_terminator and the maximum run time (TechDocs, AutoSys 12.0 and
  24.1.01, System States > Status), and its scheduler KillSignals page
  says the default `2,9` "usually" returns TERMINATED (DL-226). The
  operator-kill case is therefore documented. `# PENDING: E8` stays for
  what is not: the mechanism "usually" leaves open, recorded intent
  versus wait status, which the trap live test discriminates
  (KILLJOB against a command that traps INT and TERM and exits 0,
  `docs/live-instance-runbook.md`). The test reads the mechanism only
  when the agent's first kill signal is INT or TERM; the System Agent's
  `oscomponent.killsignals` default is 9 (DL-226). Also open are the
  signal deaths no operator sent (segfault, OOM kill). If a live instance marks those FAILURE
  (128+signum through the SEM-09 boundary), t()/f() routing flips
  (DL-44). KB 230562 (DL-58) shows a spawn-path signal-9 abort as
  agent-level FAILED: spawn-time, no PID ever existed, not the mid-run
  kill.
- **E9** — scheduler ticks missed across engine DOWNTIME (crash/stop →
  resume) (DL-45). Default: skip-and-report. Resume drops each missed tick
  AND records it in the store (a `drop` record), and it never fires the tick late.
  A live-but-stalled engine, in contrast, fires its backlog, stamped at the
  tick. The downtime/live boundary is pinned to the resume-sweep instant
  (wall-now when `resume_run` re-anchors). Ticks at or before that
  instant are downtime (dropped). Ticks after it are live backlog, even
  if the loop starts seconds later. Vendor behavior for an
  event-processor outage that spans a start_times tick is unverified [?].
  A live instance decides fire-late vs skip.
- **E10** — schedule interpretation defaults (DL-45, DL-155, DL-260). The
  no-timezone clock half is [V]. Jobs without a per-job `timezone` read
  their times in the run-level `--timezone` base zone, with UTC as the
  default. The vendor uses the AutoSys server's zone, which a migrated
  estate must set explicitly: "The start event for jobs with time-based
  starting conditions that do not specify a time zone is scheduled based on
  the time zone under which the scheduler is running" (TechDocs 12.0.01,
  timezone attribute page). The oracle exposes the same rule as a
  `default_tz` constructor knob; with none set, the engine clock plays the
  scheduler's zone. The DST half is the `dst-start-times` switch (§8a,
  DL-260): the vendor's rules are the default, and the PEP 495 fold=0
  reading is selectable. The rules apply to the change shape DL-249
  detects; other shapes keep fold=0 under both values, which is unverified,
  and the register carries it. Calendar row times (E11) keep fold=0 too.
  One half stays open [?], behind its `# PENDING: E10` marker in
  `runner_scheduler.py`: absent `days_of_week` = every day.
- **E11** — a `run_calendar` job with neither `start_times` nor
  `start_mins` is a valid vendor shape (DL-56, DL-58). It fires at the
  calendar row's own time of day, and at 00:00 when neither the row nor the
  job supplies a time; job-level `start_times` override row times (§5).
  Vendor staff gave the worked examples in thread 734033. An exhausted
  calendar makes the job dormant (DL-56); the vendor logs
  `CAUAJM_W_10119/10120` for it (DL-58). The vendor's 365-day
  materialization horizon (KB 14195) drops a yearly job whose next
  occurrence lies more than 366 days out (KB 442457); the generator
  computes occurrences directly and does not.
- **E12** to **E15** — withdrawn with the HA plan (DL-189). Not open.
- **E16** — seal cadence. Every boundary is an operator act (automatic
  sealing on a timer is a period-model §12 non-goal) and costs a restart,
  so whether the boundary is per estate, per booking centre or per
  regulatory period is the estate's choice; the answer sets the replay
  bound and with it the takeover time. Open (DL-230).
- **E17** — what a standby readiness check must assert before continuity
  governance accepts a green. Open; waits for follower mode (DL-230).
- **E18** — whether an A-classified boundary truth flip needs an operator
  acknowledgement before the period opens, or reporting it is enough. The
  classification and every A assumption are carried in the seal
  (period-model §3.1), so the record exists either way. Open (DL-230).
- **E19** — a member changed while its box executes is R, even when the
  member is INACTIVE (period-model §10.3, PR-42).
- **E20** — a seal-only archive may stand in for pruned inputs
  (period-model §12a, DL-144).
- **E21** — whether a follower can run on the file substrate by tailing
  the WAL another process appends. The WAL is append-only and
  line-framed, so the proposal is yes. Open; waits for follower mode
  (DL-230).
- **E22** — whether a migrated estate expects parity with the vendor's own
  run-history surface (`autorep -r`, and the average-run-time notion
  behind its runtime alarms). A dossier question with a citation sweep in
  front of it [?] (DL-230).
- **E23** — audit runs the interpreter that produced the period, and old
  versions stay installable; a state-machine bump is not a transition
  (period-model §11 and §2.1).
- Inherited from the oracle: Q3 (SEM-32 arm-and-wait, DL-58; the Q3c
  box-arm-scope residue is oracle-side) and Q4 (n_retrys, DL-53, kept
  deliberately unmodeled by scope decision). The runner implements the
  documented oracle defaults and adds no new switch. The §10 status
  response carries the `armed` latch per job (DL-54).
- Inherited from the dossier (DL-57, DL-58): Q8b/Q8c/Q8d, the remaining
  extended-calendar generation corners, each a pinned deterministic
  default in `autocal.py` (`# PENDING: Q8x`). Refusals apply only for
  doc-defective tokens (DL-59). Q8a and Q8e are closed by citation
  (DL-58). Q9 is closed at the [F] tier (DL-60): the export format is
  pinned in SEM-36, and both record spellings are accepted on input. The
  rest closes mechanically against a live autocal date-set diff
  (docs/live-instance-runbook.md has the protocols).
