# Runner design — executing IR-F with AutoSys semantics

Status: the design is frozen (DL-41); a frozen item changes only through a
decision-log entry. Decisions E1 (prod grade), E2 (both time domains), and
E3 (web behind proxy/tunnel) are closed. Every tier is built (§14). This
document is normative for the runner in the same way that ir-design.md is
normative for the compiler.

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

Lifecycle stance (DL-41a): the default is **tethered**: engine death
terminates all jobs, and the record is durable even under `kill -9` (§6a).
This behavior is a documented semantic choice, not an accident. For
long-running estates, operators correctly expect that an engine restart
(upgrade) does not kill active work. Thus the **detached** supervisor tier
(§6a Tier 1) is part of the prod-grade story, not an optional extra.
Without this tier, the engine is prod-grade for restartable workloads only.

## 2. Position in the pipeline

The runner consumes IR-F (`CatalogIR`) through the existing loaders
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
 control ────▶│  WAL journal (JSONL, fsync-before-feed)         │
              └───────────────┬──────────────────────────────────┘
                              │ control socket (unix domain, JSON lines)
                ┌─────────────┴─────────────┐
                │ Textual app (thin client) │
                │  terminal: dsl41 run --ui │
                │  browser:  dsl41 serve    │
                └───────────────────────────┘
```

The cut line comes directly from the oracle's existing event contract:

- **Oracle → shell**: the oracle emits every internal transition as a
  STATUS event (`_set_status` → `_emit`). An emitted
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

The runner makes only two core changes, and both are additions to the oracle:

- `next_timer_due() -> datetime | None` — a read-only look at the timer
  heap, so that a real-time shell knows when to wake. In the oracle, timers fire
  lazily inside the next `feed()` whose `at` reaches them. A wall-clock
  shell cannot be sure that an external event will arrive.
- `advance(now: datetime) -> list[Event]` — fires the timers due `<= now`
  without an external event (factored from `feed()`'s drain, same
  non-decreasing time discipline).

Both are deterministic and pure-core-compatible. Bisimulation (§13) pins
that `feed`-only and `advance`+`feed` schedules produce identical traces.

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
   run `feed()` / `advance()`.
4. Act on emitted events per the dispatch table. Journal the dispatch
   records.

Dispatch table (emitted event → side effect):

| emitted                                   | side effect                     |
|-------------------------------------------|---------------------------------|
| `STATUS STARTING`, job_type CMD           | spawn LocalCommandAdapter task  |
| `STATUS STARTING`, job_type FW            | spawn FileWatcherAdapter task   |
| terminal status, job has a live task      | cancel the adapter task (see below) |
| `MUST_START_ALARM` / `MUST_COMPLETE_ALARM`| none; they reach the operator through the trace and the UI |
| anything on a BOX                         | none (folds are oracle logic)   |

The terminal-status row makes KILLJOB and term_run_time work with no
adapter timeout logic: the oracle decides, the shell kills. The shell's own
act is the cancellation; what a cancellation costs is the adapter's
business. A CMD cancellation signals the command pgid (§6a) and the wrapper
records the outcome. An FW cancellation only stops polling — a watch owns
no process. ON_NOEXEC needs
no row. The SEM-22 bypass emits SUCCESS and never emits STARTING, so
nothing spawns by construction. The alarm row writes no WAL record of its
own: emitted events are never journaled (§7), so an alarm exists in the
replayed trace and nowhere else.

The two STARTING rows carry one more condition, the **ghost-run gate**: a
STARTING spawns only when the job's run number is past the last one this
engine dispatched. A CHANGE_STATUS overwrite re-emits STARTING without
advancing the run number, and vendor parity launches nothing.

**Stale-completion gate.** Completions carry `(job, run_number)`. Every
completion enters the common admission order first, so it is durable before
it is judged (§7). The gate then rejects one whose run_number does not match
the current one, or whose row is no longer STARTING or RUNNING: a terminal
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
POSIX strings with dst rules refuse. The runner injects `STARTJOB` at the
tick and then computes the next occurrence. The scheduler fires **unconditionally** at the tick. SEM-32
arm-and-wait on a false condition (Q3, DL-58) and
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
completion. When `std_out_file` / `std_err_file` are set, stdout/stderr
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
the size is stable across two consecutive polls ([?] steady-size reading
pinned — E6). The adapter completes with exit 0.

**Its progress is evidence, not memory** (DL-129; period-model §2.2). Last
observed size and stable-poll count decide when
a watch completes, and a restart resets both. So the watch has a spool, and it
is append-only: `runs/<job>.<run_number>/watch.jsonl`, a `start` line on
dispatch — the first durable act, so a dispatched watch always has one — then
one line per poll, fsynced, *including* polls that changed nothing. The line
is appended **before** progress moves or a completion is emitted, so an
observation that changed the watch is never one an audit cannot see.
`next_poll_at` is exactly: after `start` and no poll line, `start.at`; after
a poll line, `poll.at + interval`. A torn final line truncates on open, as
the WAL's does. A resumed watch reconstructs from the log and appends no
second `start`.

**FakeAdapter** (tests, rehearse): scripted `(job, run_number) →
(duration, exit_code)`, with instant success as the default. Scenario
files reuse the event-script shapes that the oracle trace tests already
use.

Execution identity: jobs run as the invoking user. `owner` set to a
different user is a preflight ERROR (no setuid in MVP). `machine` must be
local (§8).

## 6a. Process lifecycle tiers (DL-41a)

Unix gives exactly one `wait()` observation of a child's exit status. If
the observer is down when the child dies, init reaps the child, and the
status is gone forever. No tagging scheme recovers it. The fix is
structural: a process that cannot miss the observation writes it to a
durable location. Prior art: containerd-shim, Slurm's slurmstepd, and
HTCondor's starter. Every serious scheduler keeps a small local process
that reaps and reports after the control daemon disconnects. There are
three tiers, outermost first:

**Tier 2 — init-system backstop** (deployment note, not code): run the
runner under systemd (Linux) / launchd (macOS). On Linux, per-run
transient scopes (cgroup kill) are the only true containment (see below).

**Tier 1 — supervisor** (the availability tier): engine →
supervisor → wrappers. The supervisor exists for exactly one reason: jobs
that must SURVIVE engine restarts (upgrades, crash isolation). The
supervisor is deliberately dumb (postmaster / s6-supervise philosophy):
SPAWN, SIGNAL, LIST, SHUTDOWN, fork wrappers, reap, forward completions.
It has no timers, no conditions, and no configuration reload. Its own-bug
crash surface is near zero. Wrappers hold ITS lifeline (below). Thus even
`kill -9` of the supervisor keeps
"supervisor death ⇒ all jobs terminate and are recorded". On restart, the
engine reattaches and LISTs. The E4 "orphan adoption" question dissolves:
jobs never orphan because their parent never died. Linux hardening:
`PR_SET_CHILD_SUBREAPER`, so that a killed wrapper's command reparents to
the supervisor for reaping/killing.

From day one, the supervisor speaks a **versioned line protocol over a
named unix socket** (0600 + same-uid peer-cred check), not an inherited
socketpair. The reason: the protocol plus the spool format
(spawn.json/status.json) is the tier's public contract and future
extraction boundary (DL-42). The supervisor also writes `receipt.json`,
`reply.json` and the `runs/.by_run_id/<run_id>` index, and those three
plus the run directory itself are what make SPAWN idempotent across its
own restart (DL-129). Clients split into **unlimited read-only
observers** and **exactly one controller**. Mutating verbs require a
controller lease (controller_id, expiry, fencing token). Every mutation
carries the incarnation and the fencing token. Only SPAWN carries an
idempotency key, and it is the `run_id` itself; SIGNAL and SHUTDOWN carry
none, because neither is a second act when it repeats. This lease is a
correctness feature, not ceremony. A TUI, a script, and the engine that race SPAWN/SIGNAL on
the same job graph corrupt scheduler semantics long before security is at
risk. dsl41's own *engine* socket (§10) deliberately has no lease:
sendevent is multi-writer by AutoSys nature, and the single-writer engine
loop serializes it. The lease guards the tier that spawns without
semantics. When its trigger fires (DL-42), the supervisor tier is set for
extraction as a standalone permissively-licensed package. Until then, it
lives here under an enforced import boundary: wrapper and supervisor
import nothing from dsl41, stdlib only, tested.

**Tier 0 — per-run wrapper** (the correctness tier, always present). This
tier is a dumb stdlib-only shim (`runner_wrapper.py`, no
third-party imports). Both spawners run it BY FILE PATH, never as
`python -m`: `-m` would import the `dsl41` package first and drag
third-party imports into the recorder. The shim is parent-agnostic: it
does not know or care whether the engine or the supervisor spawned it.
This property lets Tier 1 attach later without a change to the shim.
Duties:

1. `setsid()` — its own session. The command child then does
   `setpgid(0, 0)` into its **own pgid, separate from the wrapper's**.
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
   (platform/code-signed) targets like `/bin/sh` (shown empirically and
   in XNU source), and Linux `/proc/pid/environ` is ptrace-gated. The
   identity check uses the **(pid, start-time)** tuple from spawn.json
   instead (`ps -o lstart=` / `/proc/<pid>/stat` starttime, ±2s tolerance
   on macOS's 1-second resolution). Never signal a pid that fails this
   check — the PID-reuse guard.
4. Portable event loop: SIGCHLD self-pipe + `waitpid(WNOHANG)`, with
   `select` over {self-pipe, lifeline}. On every wakeup, **do the
   child-exit check before the lifeline-EOF check**. A job that completes
   at the instant the parent dies must be recorded as completed, not as
   "parent lost".
5. On child exit: observe with `waitid(WNOWAIT)` where available, durably
   write `status.json` — the same identity fields, an `outcome`, `ended_at`,
   and whichever of `exit_code` or `signal` applies — *then* reap. This
   order narrows the observe-to-record hole to a few syscalls. Both spool
   records are frozen field by field in `docs/supervisor-protocol.md` §3,
   which is the schema; this list is what each duty owes. The status
   file is the **authority**. The live reaping chain is only a
   notification that the file exists.
6. On lifeline EOF: the parent died, which includes `kill -9`, because
   the kernel closes fds regardless. SIGTERM the command pgid, grace,
   SIGKILL, write `status.json` {terminated, cause: parent lost}, exit.
   This behavior makes "engine death ⇒ jobs terminate AND are recorded"
   hold with no polling and no Linux-only mechanism. Linux secondary
   safeguard: `PR_SET_PDEATHSIG` + `getppid()` re-check (thread-tied and
   exec-cleared, so never the primary tether).

**Lifeline fd hygiene (invariant, tested):** the pipe's write end exists
in exactly one process — the parent. One write end that leaks into any
other process silently disables parent-loss detection for that wrapper.
Python's default non-inheritable fds plus explicit `pass_fds` of only the
read end enforce this rule. A leak test holds it (spawn two wrappers,
kill parent, assert both EOF).

**Containment honesty:** pgid kill does not catch `setsid`/double-fork
escapees (`sh -c 'daemon & '`). Vendor agents share this limitation. The
documented Linux hardening is a per-run transient systemd scope (cgroup
kill), which also survives runner restarts better than any fd- or
pid-based mechanism. A `--scope` option is future work, not MVP.

The crash matrix that remains after Tier 0: `kill -9` of a *wrapper*
alone (the command survives, no status.json will ever appear) and `-9` of
the whole tree at once (same). Both are sabotage-grade. Both are detected
at reconciliation (§7) and reported truthfully, not guessed.

## 7. Journal and recovery (E1: prod grade)

The journal is an append-only JSONL WAL, one file per SEGMENT, and a period
holds one or more segments (DL-133; `docs/period-model.md` §1.1 and §7). A
root's records live in `wal/<segment_no>.jsonl`, and `journal.jsonl` holds
one line, the permanent
`period_root` sentinel `{rec, artifact_format_version, estate_id, see,
claim_id}`. The NAME is kept deliberately: the file is never absent, so a
root that sealed and exited never reads as *unused* to a build that would
genesis into it. Every reader follows the sentinel's `see` through one
function (`period.resolve_wal`), so a caller holding a run root, the
sentinel or a segment reads the same records. A root where `journal.jsonl`
IS the WAL is a retired layout (DL-138): the first record's kind tells the
two apart, and a `header` there is refused by name rather than read.

Record kinds:

- `segment` — the first record of every segment, and the whole identity of
  the period it opens: `{rec, segment_no, estate_id, period_id,
  baseline_id, catalog_hash, catalog_hash_version, source_bundle_hash,
  runtime_hash, state_machine_version, clock_domain,
  first_index, opens_from_seal, reclaimed, trust_unaudited, at}`
  (`docs/period-model.md` §2.1, DL-130). Self-describing on purpose: a
  reader that opens it knows the period, the catalog and the semantics
  without reading an earlier file. `catalog_hash` + `state_machine_version`
  are what leader eligibility is an exact match on
  (`docs/concurrency-model.md` §7, S6a); `catalog_hash_version` says which
  recipe the hash was taken under, so the gate compares like for like.
  `dsl41_version` is deliberately NOT here: it is per-process, it rides on
  `leader`, and a patch release must not move these bytes (PR-07).
- `leader` — `{epoch, at, pid, host, dsl41_version}`: one term of leadership
  over this run root (S6a, DL-100). Appended under the run root's lock
  immediately after acquiring it, which is what makes the epoch monotone:
  it is allocated by being written, so no two terms can read the same log
  and choose the same number. Not an input: it is applied to nothing and
  replay skips it. Every input between two of these records was admitted by
  the incarnation the earlier one names.
- `input` — `{seq, at, kind, payload, source, request_id, fingerprint,
  epoch}` plus `expect` and `claimed_actor` where the input was externally
  requested, source ∈ {scheduler, adapter, control, reconcile}.
- `advance` — `{seq, at, request_id, fingerprint, epoch}`: a time observation that
  the engine acted on (`Oracle.advance`), written before the advance (DL-44).
  The input alphabet has two halves: external events and time observations.
  Without the latter, an advance-fired term_run_time kill vanishes from
  replay, and a late natural-exit record can resurrect the job.
- `host` — `{seq, at, host: {verb, id, force}, source, request_id,
  fingerprint, epoch}` plus `expect` and `claimed_actor`: a change to the
  §8 routing table of `docs/concurrency-model.md` (S5a, DL-94). An admitted
  input like the two above and gated in the same place, carrying no oracle
  event: the interpreter never reads a host row, so this one is applied to
  the state owner rather than fed. Under its own key rather than
  `payload`, so no record shape spells one field name two ways.
- `decision` — `{index, request_id, decision, reason, revisions,
  legacy_batch, effects}`: the whole §4 step-7 batch in one line (DL-118,
  `docs/period-model.md` §2.3): the decision the attempt at `index` got,
  the revisions it moved, and every effect it planned. `index`, not `seq`,
  because it shares its attempt's number and `seq` is the §10 subscribe
  cursor. Each nested effect is `{effect_id, kind, job, run_number, run_id,
  executor_id, generation, index, at}`: an act on an execution host that the
  engine INTENDS, recorded before the attempt, which is the whole content of
  an outbox. An engine that dies between deciding and acting leaves the
  record that it meant to act. A SPAWN's `run_id` is minted in the same
  transaction (PR-36a) and the wrapper spec carries it, so the WAL and the
  spool name one process identity; a KILL carries the id its run's SPAWN
  bound, or null for a run this root holds no binding for. `generation` is the
  executor host row's value at birth (PR-16). The list is in ADMISSION
  order, so a SPAWN precedes its run's later KILL. `legacy_batch` is
  **required false**: this writer pins it, `true` is a retired dialect
  refused naming DL-138, and missing or non-boolean is malformed and refused
  as its own error.
- `effect_result` — `{effect_id, state, run_id, detail}`: what became of one
  attempt: `applied`, `indeterminate`, or `retired` (superseded before it
  ran). Its ABSENCE means `pending`: no outcome was recorded, which is the
  crash window (an attempt may have been made and died before its record).
  `indeterminate` is the distinct case the kind keeps separate: something
  was tried and cannot be reported on.
- `dispatch` — `{job, run_number, wrapper_pid, run_dir, started_at}`
  (audit/ordering only). The wrapper's `spawn.json` is the authoritative
  spawn record, written by the process that did the spawn. This closes
  the crash window between spawn and journal append. The pgid is the
  wrapper's child's business; the engine never observes it (DL-44).
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
- Retired kinds are refused by name, citing DL-138: `header` (DL-130
  stopped writing it: a once-per-log header cannot describe a log made of
  segments), and `result`
  and standalone `effect` (the decision and its intents as separate records,
  each its own fsync, with the window between them the atomicity violation
  §4 step 7 forbids; DL-118 merged them into `decision`). A
  `catalog_hash_version` of 1 is refused the same way. An **unknown** `rec`
  is refused too, naming the kind, as its own error: the version gate sits
  on the opening `segment`, so an unrecognised kind inside a version-matched
  segment is corruption (`docs/protocol-evolution.md`).

The admission fields (DL-89, DL-90; `docs/concurrency-model.md` §4 is
normative for the order, this section for what the records hold): `epoch`
rides on every attempt because it is the LEADER's, not the caller's, and S6
fences on it. `expect` and `claimed_actor` ride only where they exist: an
input the engine raised has neither, and writing nulls for them would blur
the one distinction the log has to keep, which inputs were externally
requested and therefore had to name a revision. Replay reads `expect` back,
because an attempt admitted without a result is re-decided through the same
gate, and the revision it named is half of what that gate reads.

Resume's side effects are bounded: a kill the engine decided and recorded,
then died before delivering, is re-driven (DL-96); a supervised SPAWN with a
bound `run_id` and no spool is replayed (DL-129); and a live wrapper under a
terminal row is killed (DL-133). Nothing else. Without the first, the sweep
would walk past a detached run whose parent is the supervisor, because its
row is already TERMINAL and reads as "completion already replayed".

`dsl41 runs` is not a new record kind: its rows are a projection folded from
the records above (`dispatch`, `input(kind=STATUS)`, and each `decision`,
both the effects nested in it and its verdict) plus the replayed trace and
the spool, offline, with nothing appended to the journal (DL-113). The
**verdict** is read for the same reason §4's gate writes it (DL-151): a
completion that gate REJECTED never reached the oracle, so the fold skips it
too; read without the verdict, a late `exit 0` would decide the row over the
real FAILURE. **The fold decides the crash window itself** (DL-156): an attempt whose `decision` record was never written is re-decided
through the §4 gate on replay, and the full-fidelity fold takes those
recovered verdicts (`Replay.recovered`, returned by `replay_trace`) instead
of throwing them away. A recovered rejection is skipped exactly as a durable
one, and a recovered application still decides the row. This exercises no
new authority: the same gate, the same records, deterministic, version-gated
(`check_replay_version`), and a resume derives the identical verdict from
the same log. A fold reading records alone cannot run the gate and REFUSES
to decide instead: the row's status stands on what the records do decide,
an earlier durable verdict or the pre-completion RUNNING, and it carries
`undecided`, so the operator is told the newest completion did not decide
it. Only an explicit or recovered `rejected` is skipped.

The row is `{job, run_number, catalog_hash, job_hash, started_at,
ended_at, duration_s, status, exit_code, started_by, executor_id, run_dir,
box_name, clock_source, fidelity, undecided}` (DL-113, DL-136, DL-141). `clock_source` says whether the timing came from
the wrapper's spool or fell back to the journal; `fidelity` says how much of
the row could be established when the period's manifest is gone. The
caller names the run roots, or names the lineage anchor alone and the
registry supplies every root in period order. The stable key across a
lineage is `(estate, job, run_number)`, with the estate supplied by the
lineage named, not by a field. An open run gets a row with a null
`ended_at` and a null duration, never a fabricated one. A run that spans a
boundary keeps its row in the period that dispatched it and stays RUNNING
there, because its terminal input is in the next segment. A box gets no
`dispatch` record, so its row comes from the replay, as does a leaf run that
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
determinism). Thus they are never journaled. `dsl41 journal` replays
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
still needs the recorded original paths; relocation-independent hashing is
a deliberate defer (it orphans every existing journal's resume gate).

**Permissions** (DL-66): run roots are `0700` (created and re-tightened
at resume); the journal, wrapper spool files, and job stdout/stderr are
`0600` at creation. The WAL carries globals and every control input, and
job output carries whatever commands print: owner-only, not
umask-hopeful. One deliberate exception: arming the DL-146 perimeter with a
named `socket_group` opens the run root to `0710` for traversal and the
socket to `0660`, and tightens every direct child first
(`docs/access-model.md` §8). Nothing else moves. The re-tightening at
resume may happen on a resume that is then refused by period-model §1.3's
resume rule (DL-224). A CLI refusal never tightens the root; its
confirmation under the locks takes the anchor lock, which tightens the
anchor directory. It is one of the few writes such a refusal may make:
`leader.lock` and `anchor.lock`, created or taken; the `0700` tightening of
the root and of the anchor directory; and the directory fsyncs those imply.
None of them carries estate state.

**Resume** (`dsl41 run <files> --run-root <root> --resume`):

**A root the anchor does not name is refused before the steps below**
(DL-224). After the `estate_id` check, resume applies period-model §1.3's
resume rule: the anchor must name this root, and the registry row for the
period of the root's newest opened segment must name it too (or, with no
row, the head must be this root's claim). The check reads and never
repairs, so it runs before a torn tail is cut or a never-opened segment is
removed. A refused resume releases both locks and creates, changes or
removes no WAL, journal, segment, sidecar, `anchor.json`, claim or
supervisor file. `dsl41 run --resume` and the offline `dsl41 seal` run the
same check first, before they stage anything or wire a supervisor, and
confirm a refusal under both locks before they act on it, since a read with
no anchor lock can be stale. Any refusal met under the locks (a busy anchor
lock; a missing, corrupt or foreign anchor; or the rule) is the command's
refusal, in its own words. A root that fails the rule when the command
starts is therefore refused with no supervisor started. The scope is this
rule: a missing anchor or another estate's anchor passes that first check
and keeps its order and side effects, and if the anchor changes between the
first check and the locks, admission is still refused but what the command
staged or wired may remain.

**Four steps run before step 1 below, and they decide which segment step 1
is about** (DL-133; period-model §11). First, the sentinel is read and
§1.1's ownership rule applied to it, exactly as it is applied at creation.
Second, `anchor.lock` is taken and the anchor's `estate_id` must be this
root's. Third, the SEAL is selected by lineage from what this root holds:
the active segment's `opens_from_seal` when it has one, else the newest
**committed** `seal` record in this root's last segment, else this is
period 1 before any seal and replay starts at genesis. For a selected seal,
the sidecar's recomputed digest is verified against the digest the naming
record carries, and so is every field the two duplicate. Fourth, the head
is acted on, which repairs whichever window the last process died in:
`open` with a `seal` record present performs the CAS the crashed sealer
did not; `claimed` with our claim and a durable segment moves the head to
`open`; `claimed` with another refuses, naming the holder. When the
selected seal is committed and has no
successor segment, resume **opens the next period** first: it claims the
successor, writes `wal/<N+1>.jsonl` with its opening `segment` at T, moves
the head, and seeds the interpreter from the sidecar; carried rows install
verbatim, revisions included, and only genuinely new rows are seeded from
the catalog. Step 1 then gates on THAT segment's pins, which are C2's.

A `journal.jsonl` whose first record is a `header` is refused as a
**retired dialect**, citing DL-138 (the refusal sits on `--resume`,
DL-134), and there is no verb that lifts it. The
question asked is narrow: the first record is a `header`. A `journal.jsonl`
that opens with a `segment` is not legacy, and its own refusals are
unchanged.

1. On catalog-hash mismatch, refuse: no silent semantic drift. A changed
   estate re-baselines explicitly. A clock-domain mismatch refuses on the
   same pin: a real run and a rehearsal are not one record.
2. Replay inputs in seq order through a fresh Oracle (original timestamps).
3. Reconcile from the §6a records, sweep = union(journal dispatch records,
   `runs/` directory, and what the supervisor LISTs when there is one). In
   tethered mode, the wrappers self-terminated their groups and recorded
   that fact when the engine died (lifeline EOF). Thus resume normally only
   *reads* outcomes. Signals are for the residual crash matrix only. First
   the boot_id shortcut: a spawn record whose boot_id differs from the
   current boot means that the machine rebooted. Then nothing survived:
   skip all liveness checks and resolve each run from status.json or E7
   directly. Otherwise, per incomplete run, in order:
   - Wrapper alive per the (pid, start-time) check → the wrapper is
     mid-grace. Allow a short settle window for its `status.json` to
     land.
   - `status.json` present → inject the real completion (raw exit_code
     through SEM-09 as usual, recorded signal/parent-lost → TERMINATED).
     Inject it at `max(ended_at, last journal at)`, with the true
     `ended_at` carried in the payload (feed times are non-decreasing).
   - Command group alive after the settle window — its wrapper dead, or
     alive and silent past the settle window plus the grace → verify the
     group leader's (pid, start-time), SIGTERM the pgid, then SIGKILL the
     pgid once the leader has died or the grace has passed (DL-226; the
     vendor's default cancel signals the process group too). A group
     whose leader is already dead is not signalled: E7. Inject `STATUS TERMINATED` cause `wrapper lost; killed at resume` (a
     kill that actually happened; TERMINATED is truthful).
   - A SUPERVISED run whose durable effect bound a `run_id` and whose spool
     holds neither `spawn.json` nor `status.json` → the SPAWN is
     **replayed** (DL-129): period-model §11a made it idempotent, so the
     supervisor's directory answers first-application, duplicate,
     in-progress, or indeterminate/collision, and the run happens once,
     resumes, or fails naming the supervisor's own reason (PR-36a).
   - Nothing alive, no `status.json`, and nothing to replay → the status is
     unobservable: inject `STATUS FAILURE` cause `exit_status_unobservable`
     (PENDING: E7). Not TERMINATED: that status is reserved for kills that
     the engine or the oracle actually performed or observed. Also never a
     status that can satisfy a success-dependent downstream. FAILURE routes
     the estate's common f()-recovery paths. Either way, the runner reports
     loudly. This verdict is the tethered path's and the identity-less
     chain's, where nothing can be replayed safely.
   All reconciliation injections journal with source=reconcile. When the
   supervisor (§6a Tier 1) exists, jobs survive engine restarts by
   *reattachment*: their parent never died. Then this step reduces to the
   supervisor's LIST.

After the ladder, and after the untraced-start sweep and the pending-KILL
re-drive below, one more rule runs over the wrappers the supervisor lists
as alive: **a live wrapper under a TERMINAL row is re-driven regardless of
the KILL effect's recorded state**, and regardless of whether a KILL effect
exists at all (DL-133; period-model §3.5, PR-33). `_apply_kill` records
`applied` when the cancellation is delivered and the TERM/grace/KILL ladder
runs on the way out of the task, so an engine that dies mid-ladder leaves a
live wrapper under a terminal row with the effect already resolved. The row
being terminal is what makes the process an orphan: the ladder skips
TERMINAL rows, a re-drive of PENDING kills alone would read that state and
walk past it, and nothing else ever looks again.

This ladder is the middle of `docs/concurrency-model.md` §7's takeover
barrier (DL-102, S6c): ACQUIRE, reconcile every execution host, retire
superseded and re-drive pending, dispatch. Two of its steps follow from
that name.

**The sweep is over every host, not every local directory.** What the
supervisor LISTs joins the union above. A step that concluded "never
spawned" from absence, where absence only meant "the run directory is
gone", would let the barrier start a second process for a run the host is
still holding.

**A start with no trace anywhere splits in two.** The spool is one kind of
evidence; the outbox (S5c) is a second, and it lives in the log. A start
whose SPAWN is still PENDING is an intent the previous leader recorded and
did not deliver; it is re-driven, at the run_number the oracle already
decided, which is §7's "re-drive pending" and needs no new mechanism:
leaving the effect pending is enough, because dispatch drains the outbox
through the same gates a fresh effect passes (so a drained host still holds
it). A start with no pending intent, an effect already resolved whose spool
has since gone, is FAILED, unless a supervised adapter holds a bound
`run_id`, in which case the SPAWN is replayed (PR-36a, the rung above).

**The barrier ends in a dispatch,** because §7 says so and because without
it the outbox is drained only on the way out of the next admitted input: a
re-driven start would wait on unrelated traffic, which on a quiet estate is
hours and on one whose only remaining work was the lost run is forever.

An FW watch leaves a run directory, its spool (§6), so the sweep finds
watches too, and the ladder has two rules for them (DL-129; period-model
§2.2 and §11a). A pending FW SPAWN whose run directory holds a `start` line
carrying the effect's `run_id` is **resolved applied by that line**: a
watch spawns no process, so `spawn.json` is not the evidence that it was
dispatched, and without the rule the barrier would re-launch a live watch
as an untraced start, two `start` lines and a fold nothing can reproduce
(PR-34). A `watch.jsonl` whose last line is a **completing** observation
while the row is still RUNNING is the sibling window, the poll appended and
the engine dead before the STATUS input; the completion is **injected from
the log**, exactly as a CMD's is injected from `status.json` (PR-34a).
Re-polling would decide the watch again against a world that has moved on.

The detached CMD path does not create its own run directory: the
supervisor creates it on receipt, because that directory is the SPAWN
tombstone, and an engine that made it first could die before sending,
leaving the retry's supervisor to read "directory exists, no receipt",
indeterminate, for a run that provably never reached the host (§11a,
PR-36). The tethered path keeps engine ownership: there is no supervisor in
it.

## 8. Preflight — refuse loudly, run honestly

This is the backend_uc R/A discipline applied to execution: ERROR refuses
the run, and WARN prints, journals, and runs. Every rule ships the house
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
  `--resource-capacity` override is a documented future escape hatch.
  Unknown `res_type` (not R/D/T). The same resource named twice in one
  `resources:` list — the demand is ambiguous. A `QUANTITY` above the
  resource's `amount` — the job would wait in QUE_WAIT forever. A
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
  job still apply. (Plain `job_load`/`priority`/`resources:` are now
  HONORED (DL-50), not warned. An unsized/unknown-res_type/malformed
  resource is an ERROR below, not a WARN.) The oracle applies the vendor's
  load rules (DL-247). Only a job with a positive `priority` checks its
  `job_load`: "The scheduler ignores any load unit values defined for the
  job or machine when the job has a priority value of zero", and 0 is the
  default. That job's load still counts against the machine: "even when
  jobs have a priority of 0, AutoSys Workload Automation tracks job loads
  on each machine". A FORCE_STARTJOB "runs even if its load exceeds the
  machine's max_load value"; its units are held the same way. A job
  waiting for load "automatically blocks all the lower priority jobs that
  specify the same machine attribute value", on a fresh start and on
  readmission; a positive priority is blocked even without a `job_load`.
  Named resources gate every start, forced or not. Pools stay outside
  both rules. This WARN still fires for a pool job at priority 0.
- Cycle in the AND-success skeleton (graphlib `CycleError`): cycles are
  *legal* AutoSys (edge-triggered re-runs, DL-13, L010's territory). Thus
  this rule warns and disables `plan`, and it does not refuse.

graphlib's role is deliberately bounded to that skeleton check plus `plan`
(wave-by-wave `get_ready()` batches for acyclic estates). General
eligibility is predicate evaluation over the status store. That is the
oracle's edge-triggered referencer machinery, not a topological order.

## 9. Time domains (E2)

`Clock` protocol: `virtual`, `now()`, `wait_until(t, interrupt)`,
`sleep_until(t)`, `next_sleeper_due()`, `pending_sleepers()`. The first
three are the engine's; `sleep_until` is the adapter's only legal block; the
last two are the virtual domain's bookkeeping, and the real clock answers
None and 0 to them. **RealClock** sleeps
(interruptible by queue activity). **VirtualClock** jumps to the next wake
instantly. The oracle takes explicit timestamps everywhere, which makes
this jump possible.

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

There are three mutating commands — `sendevent`, `host` and `seal` — a set
of queries, and the streaming `subscribe`. `docs/control-protocol.md` is the
frozen inventory; what follows is what each is for.

- **sendevent parity** (maps 1:1 onto oracle EventKind): STARTJOB,
  FORCE_STARTJOB, KILLJOB, ON_ICE/OFF_ICE, ON_HOLD/OFF_HOLD,
  ON_NOEXEC/OFF_NOEXEC, DISARM (DL-158), SET_GLOBAL, CHANGE_STATUS
  (inject STATUS).
- **host** (S5a, DL-94) and **seal** (DL-133) are the other two mutations.
  `host` changes the execution-host routing table (§7's `host` record);
  `seal` ends a period. Both take the same admission order as sendevent.
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
  global's value and its revision, inserting nothing (DL-87), `hosts [ids]`
  — the routing table's rows, and `plan` (acyclic estates only). The
  status response also carries per-job `job_type`/`box_name` (the ss11
  tree), `started_by` — the trace cause of the most recent actual start
  — `state_rev` (the revision an `expect` may name, DL-87), `held` (the
  oracle started it and its executor routes no new effect, DL-94), plus,
  for a live FW run, a `watching {file, interval, min_size}`
  object (DL-68), and a `spec_drift` flag — a lazy fingerprint re-check of the
  loaded input files; there is no reload, the flag tells the operator
  the running catalog no longer matches the disk (DL-65). The CLI adds
  scriptable predicates `is-success`/`is-failed` (print status, exit
  0/1) over the status verb.
- **subscribe** — stream journal records from a seq (the UI feed).

Every control input is journaled like any other injected event
(source=control). The WAL is the audit trail of engine decisions — no
second log carries them. Access decisions at the DL-146 perimeter go to
the perimeter journal (`docs/access-model.md` §6) and never enter the WAL
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
proxy or SSH tunnel. This posture is documented in README deployment
notes, not built.

Scope fence (DL-42): the dashboard of *meaning* — conditions, boxes,
explain, sendevent — is dsl41's and stays here. The §6a supervisor tier,
extracted or not, ships at most a boring JSON CLI and an optional
read-only top view. "free dashboard via textual-serve" is free as a demo
only. Production dashboard semantics (auth, audit of signals, history,
log redaction, retention) are orchestrator concerns, never the
supervisor's.

## 12. Non-goals

The non-goals: HA/clustering, and remote machines or agent fabric. Access
control is not a non-goal: `docs/access-model.md` (DL-146) puts three tiers
at the perimeter, and the core stays authz-free.
Also the refused extended-calendar residue (standard calendars are honored,
DL-56; the SEM-36..39 extended-calendar freeze is interpreted, DL-57; open
composition corners run on pinned defaults, DL-59; only doc-defective
tokens stay materialize-on-a-live-instance).
Also retry semantics (Q4, DL-53: kept deliberately unmodeled by scope
decision). Also non-child orphan adoption (dissolved by design: the
supervisor makes survival a *reattachment*, never an adoption, E4).
Also alarm delivery beyond the replayed trace and the UI (no mail/pager
integrations; an alarm is never a WAL record of its own, §4).
Also cgroup/scope containment (documented Linux hardening path, §6a).
Resource/load management is single-node (DL-50): the oracle honors it as
capacity buckets, and preflight refuses the unmodelable. Out of scope:
DEPLETABLE replenishment (mid-run `update_resource` = SEM-16), and
cross-node resource coordination (subsumed today by the foreign-machine
refusal — a distributed concern, DL-49 future track).

## 13. Testing — bisimulation is the acceptance gate

1. **Bisimulation**: every SEM trace test is parametrized over
   Oracle-direct and Engine(VirtualClock, FakeAdapter). Traces must be
   identical. This is equivalence tier c between simulator and executor,
   and it reuses the entire existing fixture corpus. It is the engine's
   definition of done.
2. **Hypothesis**: random event scripts (existing strategies) run through
   both paths with the same property. The suite also compares feed-only
   and advance+feed timer schedules.
3. **Journal**: replay reproduces the trace (property test). Crash-recovery
   integration test: real sleep jobs, SIGKILL the engine, resume, assert
   reconciliation records and terminal states.
   The lifecycle tier lives or dies on its failure matrix. Thus the
   wrapper and supervisor suites test kills at every phase boundary, not
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

The house layout is flat, with no `runner` subpackage: eighteen
`runner*.py` sibling modules. The first seven were split along the seams
their test files use (DL-74, DL-78); the later ones were added under the
same rule, each documented by the entry that built it. Nothing is
re-exported: every import site names the module that owns the symbol, so
the split cannot decay into a second name for one file.

- `runner.py`: the §4 engine loop.
- `runner_control.py`: the §10 control plane, the socket server, its wire
  vocabulary, and both clients; frozen in `docs/control-protocol.md`, the
  outer counterpart to the lifecycle tier's `docs/supervisor-protocol.md`.
  The server owns its own file (DL-78): every query handler is a pure
  projection, and what it shares with the loop is the loop's *task*, not
  its single-writer invariant.
- `runner_clock.py`: the §9 clock domains, plus `EngineError` at the bottom
  of the import graph.
- `runner_scheduler.py`: the §5 scheduler, turning its ticks into UTC
  instants through `timezones.py` (SEM-35 name resolution and the one
  naive-UTC ↔ local conversion, phase-free and shared with the oracle,
  DL-163).
- `runner_adapters.py`: the §6/§6a adapter contract and every adapter.
- `runner_journal.py`: the §7 WAL and its replay.
- `runner_preflight.py`: the §8 rules.
- `runner_wrapper.py`: the §6a Tier-0 shim, stdlib-only with no third-party
  imports; its dumbness is a correctness property.
- `runner_supervisor.py`: the §6a Tier-1 daemon, held to the same boundary.
- `runner_procid.py`: the process-identity helpers those two share,
  stdlib-only for the same reason (DL-72).
- `runner_tui.py`: the §11 TUI (guarded textual import).
- `runner_startup.py`: taking possession of a run root: genesis, resume and
  the takeover barrier (DL-106). §7's resume ladder lives there, not in
  `runner.py`.
- `runner_admission.py`: the one admission order (`docs/concurrency-model.md`
  §4).
- `runner_effects.py`: the effect outbox (concurrency-model §5).
- `runner_hosts.py`: the execution-host routing table (concurrency-model
  §8).
- `runner_ledger.py`: leadership over one run root (concurrency-model §7).
- `runner_history.py`: the offline `dsl41 runs` projection (DL-113).
- `runner_access.py`: the control-socket access perimeter
  (`docs/access-model.md`).

Runner CLI verbs live in `cli_run.py` (`run`, `rehearse`, `journal`,
`runs`) and `cli_control.py` (`sendevent`, `host`, `ui`, `serve`, `query`,
`supervise`), the five-module CLI split of DL-137, assembled by `cli.py`.
The period verbs are `cli_estate.py`'s.

The runner's six phases, 11a to 11f (DL-41; 11f by DL-41a), are all built,
and module docstrings cite them by label: 11a, the engine loop and the
bisimulation suite; 11b, the wrapper, adapters, WAL and resume; 11c, the
scheduler, preflight and control socket with the headless verbs
(`rehearse` and a minimal `query` client ship with them, DL-45); 11d, the
Textual TUI; 11e, `serve`; 11f, the supervisor tier (§6a Tier 1, frozen in
`docs/supervisor-protocol.md`, the extraction boundary of DL-42).

## 15. Open questions (E-series)

The house rule applies: implemented defaults are marked `# PENDING: En` in
code. None is guess-resolved.

- **E4** — jobs that survive engine restarts: closed (DL-41a, DL-48). Never
  non-child adoption. The supervisor keeps parenthood alive, so survival is
  reattachment (`run --detached`). The default tethered path is unchanged:
  engine death terminates jobs, and resume uses §7's reconciliation ladder.
- **E5** — profile sourcing failure semantics [?]. Default: the job fails
  with sh's exit code (§6).
- **E6** — FW steady-size semantics and default watch_interval [?].
  Default: two stable polls, 60s (§6).
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
  versus wait status, which the trap-TERM live test discriminates
  (KILLJOB against a command that traps SIGTERM and exits 0,
  `docs/live-instance-runbook.md`), and the signal deaths no operator
  sent (segfault, OOM kill). If a live instance marks those FAILURE
  (128+signum through the SEM-09 boundary), t()/f() routing flips
  (DL-44). KB 230562 (DL-58) shows a spawn-path signal-9 abort as
  agent-level FAILED: spawn-time, no PID ever existed, not the mid-run
  kill.
- **E9** — scheduler ticks missed across engine DOWNTIME (crash/stop →
  resume) (DL-45). Default: skip-and-report. Resume drops each missed tick
  AND journals it (a WAL `drop` record), and it never fires the tick late.
  A live-but-stalled engine, in contrast, fires its backlog, stamped at the
  tick. The downtime/live boundary is pinned to the resume-sweep instant
  (wall-now when `resume_run` re-anchors). Ticks at or before that
  instant are downtime (dropped). Ticks after it are live backlog, even
  if the loop starts seconds later. Vendor behavior for an
  event-processor outage that spans a start_times tick is unverified [?].
  A live instance decides fire-late vs skip.
- **E10** — schedule interpretation defaults (DL-45), split by DL-155: the
  no-timezone clock half is [V], the rest stays [?]. The cited half:
  jobs without a per-job `timezone` read their times in the run-level
  `--timezone` base zone, with UTC as the default. The vendor uses the
  AutoSys server's zone, which a migrated estate must set explicitly:
  "The start event for jobs with time-based starting conditions that do
  not specify a time zone is scheduled based on the time zone under
  which the scheduler is running" (TechDocs 12.0.01, timezone attribute
  page). The oracle exposes the same rule as a `default_tz` constructor
  knob; with none set, the engine clock plays the scheduler's zone. Two
  halves stay open [?], each behind its `# PENDING: E10` marker in
  `runner_scheduler.py`: absent `days_of_week` = every day, and DST
  corners pinned to PEP 495 fold=0 (ambiguous = first occurrence,
  nonexistent maps past the gap).
- **E11** — opened by DL-56, closed by DL-58: `run_calendar` with neither
  `start_times` nor `start_mins` is a valid vendor shape. The job fires at
  the calendar row's own time-of-day (`mm/dd/yyyy HH:MM`), and at 00:00
  when neither the row nor the job supplies a time. Job-level
  `start_times` overrides row times. Source: thread 734033, CA support's
  worked examples ("the job will attempt to start based on the dates and
  times in the calendar" and "if the date in the calendar has no time …
  00:00"), with two estate JILs accepted in-thread. §5 implements row-time
  firing. Corroboration (DL-58): an exhausted calendar logs
  `CAUAJM_W_10119/10120`, and the job silently stops being scheduled, the
  DL-56 dormancy pin. The vendor's 365-day materialization horizon
  (KB 14195) additionally drops yearly jobs whose next occurrence lies
  >366 days out (KB 442457). That is a vendor operational artifact
  (resolution: regenerate the calendar) deliberately NOT replicated: the
  generator computes occurrences directly.
- **E12** to **E15** — opened by the HA plan and withdrawn with it
  (DL-189). Not open.
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
- **E19** — closed by period-model §10.3 (PR-42): a member changed while
  its box executes is R, even when the member is INACTIVE.
- **E20** — closed by period-model §12a (DL-144), by policy: a seal-only
  archive may stand in for pruned inputs.
- **E21** — whether a follower can run on the file substrate by tailing
  the WAL another process appends. The WAL is append-only and
  line-framed, so the proposal is yes. Open; waits for follower mode
  (DL-230).
- **E22** — whether a migrated estate expects parity with the vendor's own
  run-history surface (`autorep -r`, and the average-run-time notion
  behind its runtime alarms). A dossier question with a citation sweep in
  front of it [?] (DL-230).
- **E23** — closed by period-model §11: audit runs the interpreter that
  produced the period and old versions stay installable; an SM bump is
  not a transition (period-model §2.1).
- Inherited from the oracle: Q3 (SEM-32 arm-and-wait, DL-58; the Q3c
  box-arm-scope residue is oracle-side) and Q4 (n_retrys, DL-53, kept
  deliberately unmodeled by scope decision). The runner implements the
  documented oracle defaults and adds no new switch. The ss10 status
  response carries the `armed` latch per job (DL-54).
- Inherited from the dossier (DL-57, DL-58): Q8b/Q8c/Q8d, the remaining
  extended-calendar generation corners, each a pinned deterministic
  default in `autocal.py` (`# PENDING: Q8x`). Refusals apply only for
  doc-defective tokens (DL-59). Q8a and Q8e are closed by citation
  (DL-58). Q9 is closed at the [F] tier (DL-60): the export format is
  pinned in SEM-36, and both record spellings are accepted on input. The
  rest closes mechanically against a live autocal date-set diff
  (docs/live-instance-runbook.md has the protocols).
