# Runner design — executing IR-F with AutoSys semantics (phase 11)

Status: the design is frozen (2026-07-11, DL-41). Decisions E1 (prod
grade), E2 (both time domains), and E3 (web behind proxy/tunnel) are
resolved. Implementation phases 11a–11f are all built (§14). For
phase 11, this document is normative in the same way that ir-design.md is
normative for phases 1–10.

## 1. Mission and scope

The runner is a production-capable **single-node** executor of CatalogIR
with AutoSys semantics (E1). One engine does two duties:

- **run** — the engine owns real workloads on the local machine: wall
  clock, real processes, durable state, and a control surface.
- **rehearse** — a virtual clock plus scripted adapters test and show an
  estate's behavior (a 24h estate in seconds) with the *same engine code
  path*. Thus rehearsal results are evidence about production behavior.

These items are explicitly not in scope (§12): an HA/clustered scheduler, a
multi-node agent fabric, and an RBAC system. (A single-node resource/load
manager DID land, DL-50, because the prod estate carries locks that the
estate relies on for correctness. The oracle honors these locks as capacity
buckets, and preflight refuses the unmodelable.) Here, prod grade means
durable, resumable, auditable, and loud about everything that the runner
does not do. Prod grade does not mean highly available.

Lifecycle stance (DL-41a): phases 11a–11e are **tethered** — engine death
terminates all jobs, and the record is durable even under `kill -9` (§6a).
This behavior is a documented semantic choice, not an accident. For
long-running estates, operators correctly expect that an engine restart
(upgrade) does not kill active work. Thus the **detached** supervisor tier
(§6a Tier 1, phase 11f) is part of the prod-grade story, not an optional
extra. Without this tier, the MVP is prod-grade for restartable workloads
only.

## 2. Position in the pipeline

The runner consumes IR-F (`CatalogIR`) through the existing loaders
(JIL → ast_jil → lowering, or the DSL surface). IR-G stays derived, and the
runner uses it only for `plan` output and UI layout. The runner adds
**no semantics**: the oracle is the single semantics authority. The runner
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
  `STATUS(job, STARTING)` *is* the dispatch instruction.
- **Shell → oracle**: the shell injects a process exit as
  `STATUS {job, exit_code}`. The payload carries the raw exit code only.
  The oracle applies the SEM-09 / DL-33 boundary itself
  (`exit_is_success`). **Adapters never classify**.
- **Oracle keeps**: KILLJOB termination, term_run_time auto-TERMINATE
  (dossier §5, timer-heap scheduled), run_window closer-edge (SEM-33), SLA
  alarms (SEM-34), box folds, ON_ICE/ON_HOLD/ON_NOEXEC (SEM-20/21/22),
  SEM-32 arm-and-wait (default since DL-54, cited-resolved DL-58).

Phase 11 makes only two core changes, and both are additions to the oracle:

- `next_timer_due() -> datetime | None` — a read-only look at the timer
  heap, so that a real-time shell knows when to wake. Today, timers fire
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
   first queued event)`.
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
| terminal status, job has a live task      | signal command pgid (§6a); the wrapper records the outcome |
| `MUST_START_ALARM` / `MUST_COMPLETE_ALARM`| journal + UI surface only       |
| anything on a BOX                         | none (folds are oracle logic)   |

The terminal-status row makes KILLJOB and term_run_time work with no
adapter timeout logic: the oracle decides, the shell kills. ON_NOEXEC needs
no row. The SEM-22 bypass emits SUCCESS and never emits STARTING, so
nothing spawns by construction.

**Stale-completion gate.** Completions carry `(job, run_number)`. The
engine drops each completion whose run_number does not match the current
one or whose job is already terminal. The engine also journals the drop.
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
arm-and-wait on a false condition (Q3 resolved by citation, DL-58) and
run_window closer-edge handling (SEM-33) stay oracle-side, exactly as in
simulation.

`run_calendar` / `exclude_calendar` resolve against the loaded calendar
export (DL-56). A **standard** calendar's date rows (`mm/dd/yyyy` with an
optional `HH:MM` tail) become an explicit day set. Day-eligibility is
membership for `run_calendar` (SEM-31: XOR `days_of_week`) minus
`exclude_calendar`'s days (SEM-31: subtracts from whichever is active).
The runner evaluates eligibility on the job's LOCAL day. When
`start_times`/`start_mins` are present, they are the ticks. With NEITHER,
a run_calendar job fires at each row's own time: bare rows at 00:00, an
extended (generated) day always at 00:00. Job-level times override row
times (E11 resolved, DL-58). Exclusion stays day-level.
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
Same-date holiday/non-workday collisions take the holiday action (Q8a
resolved, DL-58). Cycle-bound calendars exhaust to dormancy exactly like
explicit date lists. Unbounded rules scan to a 60-year dormancy ceiling
(a leap-day-weekday conjunction can legally gap ~40 years). The old
materialize-to-standard route stays the workaround for refused calendars.
The scheduler runs identically over the virtual clock, so rehearse mode
uses real calendar arithmetic.

## 6. Adapters

`JobAdapter` protocol: `async run(job_ir, run_number, ctx) -> int` (raw exit
code). Cancellation must kill the whole process group and never report.

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
`<run_dir>/logs/<job>.<run_number>.{out,err}`. Whether the engine
unescapes `\:` inside command/std_* values is the DL-39 [?]. Verbatim
carry applies here too. There is no timeout logic (term_run_time is the
oracle's timer). **No retry logic**: n_retrys is FAILURE-only (Q4
resolved, DL-53), and retry modeling stays deliberately unimplemented in
v1. This is a recorded scope decision, not an open question. A shell-side
retry forks semantics from the oracle and breaks bisimulation. Preflight
warns instead (§8).

**FileWatcherAdapter** (FW): polls every `watch_interval` seconds (default
60 [?]) until `watch_file` exists with size `>= watch_file_min_size` and
the size is stable across two consecutive polls ([?] steady-size reading
pinned — E6). The adapter completes with exit 0.

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

**Tier 1 — supervisor** (phase 11f, the availability tier): engine →
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
extraction boundary (DL-42). Clients split into **unlimited read-only
observers** and **exactly one controller**. Mutating verbs require a
controller lease (controller_id, expiry, fencing token). Mutations carry
the token and an idempotency key. This lease is a v1 correctness feature,
not ceremony. A TUI, a script, and the engine that race SPAWN/SIGNAL on
the same job graph corrupt scheduler semantics long before security is at
risk. dsl41's own *engine* socket (§10) deliberately has no lease:
sendevent is multi-writer by AutoSys nature, and the single-writer engine
loop serializes it. The lease guards the tier that spawns without
semantics. When its trigger fires (DL-42), the supervisor tier is set for
extraction as a standalone permissively-licensed package. Until then, it
lives here under an enforced import boundary: wrapper and supervisor
import nothing from dsl41, stdlib only, tested.

**Tier 0 — per-run wrapper** (phase 11b, the correctness tier, always
present). This tier is a dumb stdlib-only shim (`runner_wrapper.py`, run
as `python -m`, no third-party imports). The shim is parent-agnostic: it
does not know or care whether the engine or the supervisor spawned it.
This property lets Tier 1 attach later without a change to the shim.
Duties:

1. `setsid()` — its own session. The command child then does
   `setpgid(0, 0)` into its **own pgid, separate from the wrapper's**.
   This separation is load-bearing: `kill(-pgid, SIGKILL)` signals every
   member, and a wrapper inside the group dies *before it writes the
   record*. The wrapper stays outside the group that it (or the engine)
   signals.
2. Durably write `runs/<job>.<run_number>/spawn.json`: run_id, job,
   run_number, wrapper pid + start-time, command pid, command pgid,
   started_at, and **boot_id** (`kern.bootsessionuuid` /
   `/proc/sys/kernel/random/boot_id`). A reboot recycles the whole
   (pid, start-time) identity space. Thus a boot_id mismatch both voids
   liveness checks and *proves* that nothing survived. Durability liturgy
   for every record: temp file in the same directory, `fsync(file)`,
   `rename`, `fsync(directory)`. The runs dir itself is fsynced at
   creation. The run directory must be a **local** filesystem.
   Rename-over-NFS has ambiguous crash semantics.
3. Spawn the command with the `DSL41_RUN` env tag (base64url JSON:
   run_id, job, run_number, engine_boot_id). The tag is **forensics, not
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
   write `status.json` {exit_code | signal, ended_at}, *then* reap. This
   order narrows the observe-to-record hole to a few syscalls. The status
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
read end enforce this rule. 11b ships a leak test (spawn two wrappers,
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

The journal is an append-only JSONL WAL, one file per run. Record kinds:

- `header` — catalog content hash, dsl41 version, clock domain, started_at.
- `input` — `{seq, at, kind, payload, source}`,
  source ∈ {scheduler, adapter, control, reconcile}.
- `advance` — `{seq, at}`: a time observation that the engine acted on
  (`Oracle.advance`), written before the advance (DL-44 amendment). The
  input alphabet has two halves: external events and time observations.
  Without the latter, an advance-fired term_run_time kill vanishes from
  replay, and a late natural-exit record can resurrect the job.
- `dispatch` — `{job, run_number, wrapper_pid, run_dir, started_at}`
  (audit/ordering only). The wrapper's `spawn.json` is the authoritative
  spawn record, written by the process that did the spawn. This closes
  the crash window between spawn and journal append. The pgid is the
  wrapper's child's business — the engine never observes it (DL-44).
- `drop` — stale completions that the §4 gate dropped. Also scheduler
  ticks missed across downtime, skipped-and-recorded at resume (E9,
  DL-45).
- `preflight` — the §8 WARN items that the run started under (DL-45:
  "prints, journals, and runs" made literal). This record is not an
  input, and replay ignores it.

**Inputs-only principle**: emitted events and the trace are pure functions
of the input sequence — external events plus time observations (oracle
determinism). Thus they are never journaled. `dsl41 journal` replays
inputs and advances through a fresh Oracle to reconstruct the full trace.
One source of truth, no divergence possible. Write-ahead discipline: fsync
per record before `feed()`/`advance()` in run mode, batched in rehearse.

**Resume** (`dsl41 run --resume <journal>`):

1. On catalog-hash mismatch, refuse — no silent semantic drift. A changed
   estate re-baselines explicitly.
2. Replay inputs in seq order through a fresh Oracle (original timestamps).
3. Reconcile from the §6a records, sweep = union(journal dispatch records,
   `runs/` directory). In tethered mode, the wrappers self-terminated
   their groups and recorded that fact when the engine died (lifeline
   EOF). Thus resume normally only *reads* outcomes. Signals are for the
   residual crash matrix only. First the boot_id shortcut: a spawn record
   whose boot_id differs from the current boot means that the machine
   rebooted. Then nothing survived — skip all liveness checks and resolve
   each run from status.json or E7 directly. Otherwise, per incomplete
   run, in order:
   - Wrapper alive per the (pid, start-time) check → the wrapper is
     mid-grace. Allow a short settle window for its `status.json` to
     land.
   - `status.json` present → inject the real completion (raw exit_code
     through SEM-09 as usual, recorded signal/parent-lost → TERMINATED).
     Inject it at `max(ended_at, last journal at)`, with the true
     `ended_at` carried in the payload (feed times are non-decreasing).
   - Command group alive, wrapper dead → kill the members that pass the
     (pid, start-time) check, SIGTERM → SIGKILL. Inject
     `STATUS TERMINATED` cause `wrapper lost; killed at resume` (a kill
     that actually happened — TERMINATED is truthful).
   - Nothing alive, no `status.json` → the status is unobservable: inject
     `STATUS FAILURE` cause `exit_status_unobservable` (PENDING: E7).
     Not TERMINATED — that status is reserved for kills that we or the
     oracle actually performed or observed. Also never a status that can
     satisfy a success-dependent downstream. FAILURE routes the estate's
     common f()-recovery paths. Either way, the runner reports loudly.
   All reconciliation injections journal with source=reconcile. Adoption
   never appears at this tier. When the supervisor (§6a Tier 1, 11f)
   exists, jobs survive engine restarts by *reattachment* — their parent
   never died. Then this step reduces to the supervisor's LIST.

## 8. Preflight — refuse loudly, run honestly

This is the backend_uc R/A discipline applied to execution: ERROR refuses
the run, and WARN prints, journals, and runs. Every rule ships the house
fixture pair.

ERROR:
- `job_type` outside {CMD, BOX, FW}.
- `machine` set and not local. Locality is decided against this runner's
  DECLARED IDENTITY (`--as-machine NAME`, repeatable, DL-52). The runner is
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
- `run_calendar` / `exclude_calendar` that names a calendar absent from
  the loaded set (L018's lint WARN, fail-closed here — the same strictness
  split as L016-vs-DL-50 resources). Also a calendar reference that
  carries an unparseable date row, or that names an **extended** calendar
  that the interpreter refuses (DL-57/58/59: unknown or doc-defective
  tokens, missing holcal/cyccal dependencies).
  The ERROR carries the interpreter's reason. A valid extended calendar
  passes and is honored (§5). Open composition corners compile on pinned
  defaults and never refuse. With a `start` anchor, the probe also
  surfaces generation-time refusals (for example, an exhausted replacement
  walk) as ERRORs.
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
  Unknown `res_type` (not R/D/T). Malformed (non-integer) `job_load` /
  `priority` / machine `max_load`. Refused in BOTH run and rehearse
  (resource semantics gate the oracle in either clock domain).
- Oracle construction failure (surfaces IR-level refusals unchanged).

WARN:
- `run_calendar` whose eligible day set (run minus exclude) is empty, or
  lies entirely before the run/rehearse `start` anchor when the caller
  passes it (run: wall-now, rehearse: virtual `--start`, the only consumer
  of the parameter). Then the job never fires (DL-56 dormant semantics —
  silent-never-fires is preflight's business). Extended sources probe
  their generator from the anchor instead (DL-57). Without an anchor
  there is no probe, only compile validation.
- `n_retrys > 0` — the job runs WITHOUT retries (Q4 resolved, DL-53:
  retries stay deliberately unmodeled in v1 by scope decision).
- `job_load` on a **pool** machine — the machine-load throttle is
  unmodeled for pools (DL-50, PENDING Qr3). Resource semaphores on such a
  job still apply. (Plain `job_load`/`priority`/`resources:` are now
  HONORED (DL-50), not warned. An unsized/unknown-res_type/malformed
  resource is an ERROR below, not a WARN.)
- Cycle in the AND-success skeleton (graphlib `CycleError`): cycles are
  *legal* AutoSys (edge-triggered re-runs, DL-13, L010's territory). Thus
  this rule warns and disables `plan`, and it does not refuse.

graphlib's role is deliberately bounded to that skeleton check plus `plan`
(wave-by-wave `get_ready()` batches for acyclic estates). General
eligibility is predicate evaluation over the status store. That is the
oracle's edge-triggered referencer machinery, not a topological order.

## 9. Time domains (E2)

`Clock` protocol: `now()`, `wait_until(t, interrupt)`. **RealClock** sleeps
(interruptible by queue activity). **VirtualClock** jumps to the next wake
instantly. The oracle takes explicit timestamps everywhere, which makes
this jump possible.

- `dsl41 run` — RealClock + real adapters + control socket.
- `dsl41 rehearse` — VirtualClock + FakeAdapter + optional scenario, in
  batch mode. It exits when quiescent (no queued events, no due timers,
  no occurrence within the horizon). Same engine loop, byte-identical
  trace format.

## 10. Control plane

The control plane is a Unix domain socket in the run directory, mode 0600,
with a JSON-lines protocol.

- **sendevent parity** (maps 1:1 onto oracle EventKind): STARTJOB,
  FORCE_STARTJOB, KILLJOB, ON_ICE/OFF_ICE, ON_HOLD/OFF_HOLD,
  ON_NOEXEC/OFF_NOEXEC, SET_GLOBAL, CHANGE_STATUS (inject STATUS).
- **Queries**: `status [job]`, `trace [--since seq]`, `explain <job>` — the
  job's condition shown with per-atom truth over the current store (the
  Cond IR makes this nearly free), and `plan` (acyclic estates only).
- **subscribe** — stream journal records from a seq (the UI feed).

Every control input is journaled like any other injected event
(source=control). The WAL is the audit trail — there is no second log.

## 11. UI — one Textual app, terminal and web (E3)

`runner_tui.py`, an optional extra `dsl41[ui]` (textual, plus
textual-serve for web). The core package keeps its three runtime
dependencies.

The app is a **client of the control socket only** — never in-process with
the engine. This split is forced, not stylistic. textual-serve spawns one
app instance per browser session, so an in-process engine hands every
viewer a private universe. Views: jobs table (status, run_number, pending
timers/alarms), explain pane ("waiting on: s(A) ∧ ¬f(B)" with live truth),
log tail (the §6 std files), event console (sendevent verbs).

`dsl41 run --ui` starts the engine and attaches the TUI in the terminal.
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

The non-goals: HA/clustering, remote machines or agent fabric, and RBAC.
Also the refused extended-calendar residue (standard calendars honored
since DL-56, the SEM-36..39 extended-calendar freeze interpreted since
DL-57, open composition corners on pinned defaults since DL-59 — only
doc-defective tokens stay materialize-on-a-live-instance).
Also retry semantics (Q4 resolved DL-53, kept deliberately unmodeled by
scope decision). Also non-child orphan adoption (dissolved by design: the
11f supervisor makes survival a *reattachment*, never an adoption — E4).
Also alarm delivery beyond journal + UI (no mail/pager integrations).
Also cgroup/scope containment (documented Linux hardening path, §6a).
Resource/load management LANDED single-node (DL-50): the oracle honors it
as capacity buckets, and preflight refuses the unmodelable. Still out:
DEPLETABLE replenishment (mid-run `update_resource` = SEM-16), and
cross-node resource coordination (subsumed today by the foreign-machine
refusal — a distributed concern, DL-49 future track).

## 13. Testing — bisimulation is the acceptance gate

1. **Bisimulation**: every SEM trace test is parametrized over
   Oracle-direct and Engine(VirtualClock, FakeAdapter). Traces must be
   identical. This is equivalence tier c between simulator and executor,
   and it reuses the entire existing fixture corpus. It is the phase-11a
   definition of done.
2. **Hypothesis**: random event scripts (existing strategies) run through
   both paths with the same property. The suite also compares feed-only
   and advance+feed timer schedules.
3. **Journal**: replay reproduces the trace (property test). Crash-recovery
   integration test: real sleep jobs, SIGKILL the engine, resume, assert
   reconciliation records and terminal states.
   The lifecycle tier lives or dies on its failure matrix. Thus 11b/11f
   test kills at every phase boundary, not only mid-run. The boundaries:
   before/after spawn.json, after fork before exec, after wait
   observation before status write, and after status write before reap.
   They also test
   ENOSPC on the runs dir, a stale control socket, pid reuse (spoofed
   spawn.json), and a simulated reboot (boot_id flip).
4. **Preflight**: trigger/non-trigger fixture pair per rule.
5. **Adapters**: pgid kill, append semantics, profile sourcing. Also the
   FW watcher over a tmpfile that grows to stability.
6. **TUI**: textual pilot snapshot smoke only.

## 14. Module layout and phasing

The house layout is flat: `runner.py` (clock, engine, scheduler, adapters,
journal, preflight, control server), `runner_wrapper.py` (the §6a Tier-0
shim: stdlib-only, no third-party imports — its dumbness is a correctness
property), and `runner_tui.py` (guarded textual import). CLI verbs in
cli.py: `run`, `rehearse`, `sendevent`, `serve`, `journal`.

- **11a** — oracle additions (`next_timer_due`, `advance`) + engine loop +
  FakeAdapter + VirtualClock + bisimulation suite. Proves the design.
- **11b** — wrapper (`runner_wrapper.py`) + LocalCommandAdapter +
  FileWatcherAdapter + WAL journal + replay resume + reconciliation.
  Tests: crash-recovery (SIGKILL engine mid-run), lifeline fd-leak,
  wrapper pgid-separation, unobservable-status path.
- **11c** — scheduler + preflight + headless `run`/`sendevent` CLI +
  control socket. It landed with two additions within the frame (DL-45):
  the `rehearse` verb (its quiescence needs the scheduler, so it ships
  here, not 11d/e) and a minimal `query` CLI client for the §10 query
  verbs (the headless autorep analog — the 11d TUI consumes the same
  protocol).
- **11d** — Textual TUI (terminal).
- **11e** — `serve` via textual-serve + deployment notes.
- **11f** — supervisor tier (§6a Tier 1): detached mode, engine
  reattachment, versioned named-socket protocol + controller lease
  (frozen in `docs/supervisor-protocol.md` — the future extraction
  boundary, DL-42), import-boundary test, Linux subreaper. This phase
  completes the prod-grade story for long-running estates (§1).

## 15. Open questions (E-series)

The house rule applies: implemented defaults are marked `# PENDING: En` in
code. None is guess-resolved.

- **E4** — jobs that survive engine restarts. RESOLVED in design by DL-41a
  and BUILT in 11f (DL-48): never non-child adoption. The supervisor keeps
  parenthood alive, so survival is reattachment (`run --detached`). The
  default tethered path is unchanged — engine death terminates jobs, and
  resume uses §7's reconciliation ladder.
- **E5** — profile sourcing failure semantics [?]. Default: the job fails
  with sh's exit code (§6).
- **E6** — FW steady-size semantics and default watch_interval [?].
  Default: two stable polls, 60s (§6).
- **E7** — verdict for an unobservable exit status (§7). Default: FAILURE
  with cause `exit_status_unobservable`. TERMINATED is reserved for kills
  that actually happened. If an estate shows t()-conditioned recovery
  paths that are intended to fire instead, revisit this default.
- **E8** — verdict for an EXTERNAL signal death (the wrapper records
  `signaled`, the engine is alive, no oracle kill decision — segfault,
  OOM kill, operator `kill -9` of the command). Default: TERMINATED,
  uniform with the DL-41a recorded-signal reading. Real AutoSys can
  instead mark FAILURE (128+signum through the SEM-09 boundary). If it
  does, t()/f() routing flips. Opened by the 11b adversarial review
  (DL-44 amendment). Swept 2026-07-28 (DL-53): publicly undocumented.
  Re-swept 2026-07-30 (DL-58): KB 230562 shows an agent-side spawn-path
  signal-9 abort reported as `State FAILED … Status(Aborted, Signal 9)`.
  That is directional evidence for FAILURE, but spawn-time (no PID ever
  existed), not the mid-run kill. The lean moves toward FAILURE, and the
  pin stands. The live closer stays one kill test, plus the trap-TERM
  variant (KILLJOB against a command that traps SIGTERM and exits 0) to
  discriminate recorded-intent vs wait-status mechanisms.
- **E9** — scheduler ticks missed across engine DOWNTIME (crash/stop →
  resume). Default: skip-and-report. Resume drops each missed tick AND
  journals it (a WAL `drop` record), and it never fires the tick late. A
  live-but-stalled engine, in contrast, fires its backlog, stamped at the
  tick. The downtime/live boundary is pinned to the resume-sweep instant
  (wall-now when `resume_run` re-anchors). Ticks at or before that
  instant are downtime (dropped). Ticks after it are live backlog, even
  if the loop starts seconds later. Vendor behavior for an
  event-processor outage that spans a start_times tick is unverified [?].
  A live instance decides fire-late vs skip. Opened by 11c (DL-45).
- **E10** — schedule interpretation defaults [?]: absent `days_of_week`
  = every day. Jobs without a per-job `timezone` read their times in the
  run-level `--timezone` base zone, with UTC as the default. The vendor
  uses the AutoSys server's zone, which a migrated estate must set
  explicitly. DST corners are pinned to PEP 495 fold=0 (ambiguous = first
  occurrence, nonexistent maps past the gap). Opened by 11c (DL-45).
- **E11** — RESOLVED 2026-07-30 (DL-58, citation sweep, opened by DL-56):
  `run_calendar` with neither `start_times` nor `start_mins` is a valid
  vendor shape. The job fires at the calendar row's own time-of-day
  (`mm/dd/yyyy HH:MM`), and at 00:00 when neither the row nor the job
  supplies a time. Job-level `start_times` overrides row times.
  Thread 734033: CA support's worked examples ("the job will attempt to
  start based on the dates and times in the calendar" and "if the date in
  the calendar has no time … 00:00"), with two estate JILs accepted
  in-thread. The preflight refusal is deleted. §5 implements row-time
  firing. Free corroboration from the same sweep: an exhausted calendar
  logs `CAUAJM_W_10119/10120`, and the job silently stops being scheduled
  — the DL-56 dormancy pin. The vendor's 365-day materialization horizon
  (KB 14195) additionally drops yearly jobs whose next occurrence lies
  >366 days out (KB 442457). That is a vendor operational artifact
  (resolution: regenerate the calendar) deliberately NOT replicated: our
  generator computes occurrences directly.
- Inherited from the oracle: Q3 (SEM-32 arm-and-wait — resolved by
  citation DL-58, the Q3c box-arm-scope residue is oracle-side) and Q4
  (n_retrys — resolved DL-53, kept deliberately unmodeled by scope
  decision). The runner implements the documented oracle defaults and
  adds no new switch. The ss10 status response carries the `armed` latch
  per job (DL-54).
- Inherited from the dossier (DL-57, pruned by DL-58): Q8b/Q8c/Q8d — the
  remaining extended-calendar generation corners, each a pinned
  deterministic default in `autocal.py` (`# PENDING: Q8x`). Refusals
  apply only for doc-defective tokens since DL-59. Q8a and Q8e are
  resolved by citation (DL-58). Q9 is resolved at the [F] tier (DL-60):
  the export format is pinned in SEM-36, and both record spellings are
  still accepted on input. The rest closes mechanically against a live
  autocal date-set diff (docs/live-instance-runbook.md has the
  protocols).
