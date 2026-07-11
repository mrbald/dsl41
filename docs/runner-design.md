# Runner design — executing IR-F with AutoSys semantics (phase 11)

Status: design frozen 2026-07-11 (DL-41). Decisions E1 (prod grade),
E2 (both time domains), E3 (web behind proxy/tunnel) resolved by the user.
Implementation phases 11a–11e (§14). This document is normative for phase 11
the way ir-design.md is for phases 1–10.

## 1. Mission and scope

A production-capable **single-node** executor of CatalogIR with AutoSys
semantics (E1). Two duties, one engine:

- **run** — own real workloads on the local machine: wall clock, real
  processes, durable state, control surface.
- **rehearse** — virtual clock + scripted adapters: verify and demonstrate an
  estate's behavior (a 24h estate in seconds) with the *same engine code
  path*, so rehearsal results are evidence about production behavior.

Explicitly not (§12): an HA/clustered scheduler, a multi-node agent fabric, a
resource manager, an RBAC system. Prod grade here means durable, resumable,
auditable, and loud about everything it does not do — not highly available.

Lifecycle stance (DL-41a): phases 11a–11e are **tethered** — engine death
terminates all jobs, recorded durably even under `kill -9` (§6a). That is a
documented semantic choice, not an accident. For long-running estates,
operators rightly expect an engine restart (upgrade) not to kill active
work: the **detached** supervisor tier (§6a Tier 1, phase 11f) is therefore
part of the prod-grade story, not an optional extra — MVP without it is
prod-grade for restartable workloads only.

## 2. Position in the pipeline

The runner consumes IR-F (`CatalogIR`) through the existing loaders
(JIL → ast_jil → lowering, or the DSL surface). IR-G stays derived and is
used only for `plan` output and UI layout. The runner adds **no semantics**:
the oracle is the single semantics authority; the runner contributes effects
(processes, files), wall time, durability, and a control surface.

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

The cut line falls out of the oracle's existing event contract:

- **Oracle → shell**: every internal transition is emitted as a STATUS event
  (`_set_status` → `_emit`). An emitted `STATUS(job, STARTING)` *is* the
  dispatch instruction.
- **Shell → oracle**: a process exit is injected as `STATUS {job, exit_code}`
  — payload carries the raw exit code only. The oracle applies the SEM-09 /
  DL-33 boundary itself (`exit_is_success`); **adapters never classify**.
- **Oracle keeps**: KILLJOB termination, term_run_time auto-TERMINATE
  (dossier §5; timer-heap scheduled), run_window closer-edge (SEM-33), SLA
  alarms (SEM-34), box folds, ON_ICE/ON_HOLD/ON_NOEXEC (SEM-20/21/22),
  SEM-32 abandonment.

Two additions to the oracle — the only core changes phase 11 makes:

- `next_timer_due() -> datetime | None` — read-only peek at the timer heap,
  so a real-time shell knows when to wake. Today timers fire lazily inside
  the next `feed()` whose `at` reaches them; a wall-clock shell cannot rely
  on an external event arriving.
- `advance(now: datetime) -> list[Event]` — fire timers due `<= now` without
  an external event (factored from `feed()`'s drain; same non-decreasing
  time discipline).

Both are deterministic and pure-core-compatible; bisimulation (§13) pins
that `feed`-only and `advance`+`feed` schedules produce identical traces.

## 4. Engine loop — single writer

One asyncio task owns the Oracle (it is not reentrant). Event sources: the
adapter completion queue, control connections, the scheduler (§5), and the
oracle's own timers. Each iteration:

1. `next_wake = min(oracle.next_timer_due(), scheduler.next_occurrence(),
   first queued event)`.
2. The clock waits (§9): real — sleep until `next_wake`, waking early when
   the queue gains an event; virtual — jump.
3. **Journal first** (§7): WAL-append + fsync the injected event, *then*
   `feed()` / `advance()`.
4. Act on emitted events per the dispatch table; journal dispatch records.

Dispatch table (emitted event → side effect):

| emitted                                   | side effect                     |
|-------------------------------------------|---------------------------------|
| `STATUS STARTING`, job_type CMD           | spawn LocalCommandAdapter task  |
| `STATUS STARTING`, job_type FW            | spawn FileWatcherAdapter task   |
| terminal status, job has a live task      | signal command pgid (§6a); the wrapper records the outcome |
| `MUST_START_ALARM` / `MUST_COMPLETE_ALARM`| journal + UI surface only       |
| anything on a BOX                         | none (folds are oracle logic)   |

The terminal-status row is what makes KILLJOB and term_run_time work with no
adapter timeout logic: the oracle decides, the shell kills. ON_NOEXEC needs
no row — the SEM-22 bypass emits SUCCESS without ever emitting STARTING, so
nothing spawns by construction.

**Stale-completion gate.** Completions carry `(job, run_number)`. The engine
drops — and journals the drop — any completion whose run_number mismatches
the current one or whose job is already terminal. This closes the race
between a natural exit and a concurrent KILLJOB/term_run_time kill. The gate
must live in the shell: the oracle contract deliberately lets injected
STATUS overwrite terminal statuses (sendevent CHANGE_STATUS parity), so a
late completion fed through would silently rewrite history.

## 5. Scheduler — the calendar the oracle deliberately lacks

The oracle "owns no calendar"; in trace tests the script injects STARTJOB
where AutoSys's scheduler would fire. In run mode the runner plays that
role: for each `date_conditions` job it computes the next occurrence from
`days_of_week` + `start_times` (or `start_mins` hourly ticks), applying
`timezone` via `zoneinfo` when set, injects `STARTJOB` at the tick, and
computes the next. The scheduler fires **unconditionally** at the tick:
SEM-32 abandonment on a false condition (PENDING: Q3) and run_window
closer-edge handling (SEM-33) remain oracle-side, exactly as in simulation.

`run_calendar` / `exclude_calendar` reference calendar definitions the IR
does not model — preflight ERROR (§8). The scheduler runs identically over
the virtual clock, so rehearse mode exercises real calendar arithmetic.

## 6. Adapters

`JobAdapter` protocol: `async run(job_ir, run_number, ctx) -> int` (raw exit
code). Cancellation must kill the whole process group and never report.

**LocalCommandAdapter** (CMD): an engine-side task that spawns the per-run
**wrapper** (§6a) and awaits it; the wrapper spawns `/bin/sh -c`, with
`profile` sourced first when set — `. <profile> && <command>`, so a failing
profile fails the job with sh's exit code ([?] unverified vendor corner,
pinned here — E5). The command runs in its own pgid *separate from the
wrapper's* (§6a). The wrapper's `status.json` is the **sole data channel**
for the outcome; the wrapper's exit merely notifies the engine to read it.
Cancel (oracle said terminal, §4) = engine signals the command pgid
SIGTERM, grace, SIGKILL; the wrapper observes the deaths and records the
outcome durably like any other completion. stdout/stderr **append** to
`std_out_file` / `std_err_file` when set (vendor appends), else to
`<run_dir>/logs/<job>.<run_number>.{out,err}`. Whether the engine unescapes
`\:` inside command/std_* values is the DL-39 [?] — verbatim carry stands
here too. No timeout logic (term_run_time is the oracle's timer). **No
retry logic**: n_retrys is NOT modeled v1 (PENDING: Q4); a shell-side retry
would fork semantics from the oracle and break bisimulation. Preflight
warns instead (§8).

**FileWatcherAdapter** (FW): poll every `watch_interval` seconds (default
60 [?]) until `watch_file` exists with size `>= watch_file_min_size` and
the size is stable across two consecutive polls ([?] steady-size reading
pinned — E6). Completes with exit 0.

**FakeAdapter** (tests, rehearse): scripted `(job, run_number) →
(duration, exit_code)`; default instant success. Scenario files reuse the
event-script shapes the oracle trace tests already use.

Execution identity: jobs run as the invoking user. `owner` set to anyone
else is a preflight ERROR (no setuid in MVP), `machine` must be local (§8).

## 6a. Process lifecycle tiers (DL-41a)

Unix gives exactly one `wait()` observation of a child's exit status; if
the observer is down when the child dies, init reaps it and the status is
gone forever. No tagging scheme recovers it. The fix is structural: a
process that cannot miss the observation writes it somewhere durable.
Prior art: containerd-shim, Slurm's slurmstepd, HTCondor's starter — every
serious scheduler keeps a small local process that reaps and reports after
the control daemon disconnects. Three tiers, outermost first:

**Tier 2 — init-system backstop** (deployment note, not code): run the
runner under systemd (Linux) / launchd (macOS). On Linux, per-run
transient scopes (cgroup kill) are the only true containment — see below.

**Tier 1 — supervisor** (phase 11f, the availability tier): engine →
supervisor → wrappers. The supervisor exists for exactly one reason: jobs
that must SURVIVE engine restarts (upgrades, crash isolation). It is
deliberately dumb (postmaster / s6-supervise philosophy): SPAWN, SIGNAL,
LIST, SHUTDOWN, fork wrappers, reap, forward completions. No timers, no
conditions, no config reload; near-zero own-bug crash surface. Wrappers
hold ITS lifeline (below), so even `kill -9` of the supervisor preserves
"supervisor death ⇒ all jobs terminate and are recorded". On restart the
engine reattaches and LISTs — the E4 "orphan adoption" problem dissolves:
jobs never orphan because their parent never died. Linux hardening:
`PR_SET_CHILD_SUBREAPER` so a killed wrapper's command reparents to the
supervisor for reaping/killing.

The supervisor speaks a **versioned line protocol over a named unix
socket** (0600 + same-uid peer-cred check) from day one — not an
inherited socketpair — because the protocol plus the spool format
(spawn.json/status.json) is the tier's public contract and future
extraction boundary (DL-42). Clients split into **unlimited read-only
observers** and **exactly one controller**: mutating verbs require a
controller lease (controller_id, expiry, fencing token; mutations carry
the token and an idempotency key). This is a v1 correctness feature, not
ceremony — a TUI, a script, and the engine racing SPAWN/SIGNAL on the
same job graph corrupts scheduler semantics long before it is a security
problem. dsl41's own *engine* socket (§10) deliberately has no lease:
sendevent is multi-writer by AutoSys nature and the single-writer engine
loop serializes it; the lease guards the tier that spawns without
semantics. The supervisor tier is earmarked for extraction as a
standalone permissively-licensed package once its trigger fires (DL-42);
until then it lives here under an enforced import boundary — wrapper and
supervisor import nothing from dsl41, stdlib only, tested.

**Tier 0 — per-run wrapper** (phase 11b, the correctness tier; always
present). A dumb stdlib-only shim (`runner_wrapper.py`, run as
`python -m`, no third-party imports), parent-agnostic — it neither knows
nor cares whether engine or supervisor spawned it, which is what lets
Tier 1 slot in later without touching it. Duties:

1. `setsid()` — its own session. The command child then `setpgid(0, 0)`
   into its **own pgid, separate from the wrapper's**. This is
   load-bearing: `kill(-pgid, SIGKILL)` signals every member, and a
   wrapper inside the group would die *before writing the record*. The
   wrapper stays outside the group it (or the engine) signals.
2. Durably write `runs/<job>.<run_number>/spawn.json` — run_id, job,
   run_number, wrapper pid + start-time, command pid, command pgid,
   started_at, and **boot_id** (`kern.bootsessionuuid` /
   `/proc/sys/kernel/random/boot_id`): a reboot recycles the whole
   (pid, start-time) identity space, so a boot_id mismatch both voids
   liveness checks and *proves* nothing survived. Durability liturgy for
   every record: temp file in the same
   directory, `fsync(file)`, `rename`, `fsync(directory)`; the runs dir
   itself fsync'd at creation. The run directory must be a **local**
   filesystem — rename-over-NFS has ambiguous crash semantics.
3. Spawn the command with the `DSL41_RUN` env tag (base64url JSON:
   run_id, job, run_number, engine_boot_id). The tag is **forensics, not
   correctness**: macOS `KERN_PROCARGS2` omits env for restricted
   (platform/code-signed) targets like `/bin/sh` — confirmed empirically
   and in XNU source — and Linux `/proc/pid/environ` is ptrace-gated.
   Identity verification uses the **(pid, start-time)** tuple from
   spawn.json instead (`ps -o lstart=` / `/proc/<pid>/stat` starttime;
   ±2s tolerance on macOS's 1-second resolution). Never signal a pid that
   fails this check — the PID-reuse guard.
4. Portable event loop: SIGCHLD self-pipe + `waitpid(WNOHANG)`, `select`
   over {self-pipe, lifeline}. On every wakeup **check child exit before
   lifeline EOF** — a job completing at the instant the parent dies must
   be recorded as completed, not as "parent lost".
5. On child exit: observe with `waitid(WNOWAIT)` where available, durably
   write `status.json` {exit_code | signal, ended_at}, *then* reap —
   narrowing the observe-to-record hole to a few syscalls. The status
   file is the **authority**; the live reaping chain is only a
   notification that it exists.
6. On lifeline EOF: the parent died — including `kill -9`, because the
   kernel closes fds regardless. SIGTERM the command pgid, grace,
   SIGKILL, write `status.json` {terminated, cause: parent lost}, exit.
   This makes "engine death ⇒ jobs terminate AND are recorded" hold with
   no polling and no Linux-only mechanism. Linux belt-and-braces:
   `PR_SET_PDEATHSIG` + `getppid()` re-check (thread-tied and
   exec-cleared, so never the primary tether).

**Lifeline fd hygiene (invariant, tested):** the pipe's write end exists
in exactly one process — the parent. One write end leaked into any other
process silently disables parent-loss detection for that wrapper. Python's
default non-inheritable fds plus explicit `pass_fds` of only the read end
enforce this; 11b ships a leak test (spawn two wrappers, kill parent,
assert both EOF).

**Containment honesty:** pgid kill does not catch `setsid`/double-fork
escapees (`sh -c 'daemon & '`). Vendor agents share this limitation. The
documented Linux hardening is a per-run transient systemd scope (cgroup
kill), which also survives runner restarts better than any fd- or
pid-based mechanism; a `--scope` option is future work, not MVP.

The crash matrix that remains after Tier 0: `kill -9` of a *wrapper*
alone (command survives; no status.json will ever appear) and `-9` of the
whole tree at once (same). Both are sabotage-grade, both are detected at
reconciliation (§7) and reported truthfully rather than guessed.

## 7. Journal and recovery (E1: prod grade)

Append-only JSONL WAL, one file per run. Record kinds:

- `header` — catalog content hash, dsl41 version, clock domain, started_at.
- `input` — `{seq, at, kind, payload, source}`,
  source ∈ {scheduler, adapter, control, reconcile}.
- `dispatch` — `{job, run_number, pgid, started_at}` (audit/ordering only:
  the wrapper's `spawn.json` is the authoritative spawn record, written by
  the process that did the spawning — which closes the crash window
  between spawn and journal append).
- `drop` — stale completions discarded by the §4 gate.

**Inputs-only principle**: emitted events and the trace are pure functions
of the input sequence (oracle determinism), so they are never journaled;
`dsl41 journal render` replays inputs through a fresh Oracle to reconstruct
the full trace. One source of truth, no divergence possible. Write-ahead
discipline: fdatasync per record before `feed()` in run mode; batched in
rehearse.

**Resume** (`dsl41 run --resume <journal>`):

1. Refuse on catalog-hash mismatch — no silent semantic drift; a changed
   estate re-baselines explicitly.
2. Replay inputs in seq order through a fresh Oracle (original timestamps).
3. Reconcile from the §6a records, sweep = union(journal dispatch records,
   `runs/` directory). In tethered mode the wrappers self-terminated their
   groups and recorded the fact when the engine died (lifeline EOF), so
   resume normally just *reads* outcomes; signals are for the residual
   crash matrix only. First the boot_id shortcut: a spawn record whose
   boot_id differs from the current boot means the machine rebooted —
   nothing survived, skip all liveness checks, resolve each run from
   status.json or E7 directly. Otherwise, per incomplete run, in order:
   - Wrapper verified alive by (pid, start-time) → it is mid-grace; allow
     a short settle window for its `status.json` to land.
   - `status.json` present → inject the real completion (raw exit_code
     through SEM-09 as usual; recorded signal/parent-lost → TERMINATED),
     injected at `max(ended_at, last journal at)` with the true
     `ended_at` carried in the payload (feed times are non-decreasing).
   - Command group alive, wrapper dead → kill the (pid, start-time)-
     verified members SIGTERM → SIGKILL, inject `STATUS TERMINATED`
     cause `wrapper lost; killed at resume` (a kill that actually
     happened — TERMINATED is truthful).
   - Nothing alive, no `status.json` → the status is unobservable: inject
     `STATUS FAILURE` cause `exit_status_unobservable` (PENDING: E7).
     Not TERMINATED — that is reserved for kills we or the oracle
     actually performed/observed — and never anything that could satisfy
     a success-dependent downstream; FAILURE routes the estate's common
     f()-recovery paths. Reported loudly either way.
   All reconciliation injections journal with source=reconcile. Adoption
   never appears at this tier: once the supervisor (§6a Tier 1, 11f)
   exists, jobs survive engine restarts by *reattachment* — their parent
   never died — and this step reduces to the supervisor's LIST.

## 8. Preflight — refuse loudly, run honestly

The backend_uc R/A discipline applied to execution: ERROR refuses the run;
WARN prints, journals, and runs. Every rule ships the house fixture pair.

ERROR:
- `job_type` outside {CMD, BOX, FW}.
- `machine` set and not local (None, `localhost`, or the local hostname).
- `owner` set and not the invoking user.
- `run_calendar` / `exclude_calendar` present (definitions unmodeled).
- `timezone` not resolvable in `zoneinfo`.
- Oracle construction failure (surfaces IR-level refusals unchanged).

WARN:
- `n_retrys > 0` — runs WITHOUT retries (PENDING: Q4).
- `job_load` / `priority` / resource references — no resource manager.
- Cycle in the AND-success skeleton (graphlib `CycleError`): cycles are
  *legal* AutoSys (edge-triggered re-runs, DL-13; L010's territory), so this
  warns and disables `plan` rather than refusing.

graphlib's role is deliberately bounded to that skeleton check plus `plan`
(wave-by-wave `get_ready()` batches for acyclic estates); general
eligibility is predicate evaluation over the status store, which is the
oracle's edge-triggered referencer machinery, not a topological order.

## 9. Time domains (E2)

`Clock` protocol: `now()`, `wait_until(t, interrupt)`. **RealClock** sleeps
(interruptible by queue activity). **VirtualClock** jumps to the next wake
instantly — enabled by the oracle taking explicit timestamps everywhere.

- `dsl41 run` — RealClock + real adapters + control socket.
- `dsl41 rehearse` — VirtualClock + FakeAdapter + optional scenario; batch;
  exits when quiescent (no queued events, no due timers, no occurrence
  within the horizon). Same engine loop, byte-identical trace format.

## 10. Control plane

Unix domain socket in the run directory, mode 0600, JSON-lines protocol.

- **sendevent parity** (maps 1:1 onto oracle EventKind): STARTJOB,
  FORCE_STARTJOB, KILLJOB, ON_ICE/OFF_ICE, ON_HOLD/OFF_HOLD,
  ON_NOEXEC/OFF_NOEXEC, SET_GLOBAL, CHANGE_STATUS (inject STATUS).
- **Queries**: `status [job]`, `trace [--since seq]`, `explain <job>` — the
  job's condition rendered with per-atom truth over the current store (the
  Cond IR makes this nearly free), `plan` (acyclic estates only).
- **subscribe** — stream journal records from a seq (the UI feed).

Every control input is journaled like any other injected event
(source=control): the WAL is the audit trail; there is no second log.

## 11. UI — one Textual app, terminal and web (E3)

`runner_tui.py`, optional extra `dsl41[ui]` (textual; textual-serve for
web). The core package keeps its three runtime dependencies.

The app is a **client of the control socket only** — never in-process with
the engine. This split is forced, not stylistic: textual-serve spawns one
app instance per browser session, so an in-process engine would hand every
viewer a private universe. Views: jobs table (status, run_number, pending
timers/alarms), explain pane ("waiting on: s(A) ∧ ¬f(B)" with live truth),
log tail (the §6 std files), event console (sendevent verbs).

`dsl41 run --ui` starts the engine and attaches the TUI in the terminal;
`dsl41 serve --socket <path>` wraps textual-serve around the same app. Web
posture (E3): textual-serve ships no auth — deploy behind a reverse proxy
or SSH tunnel; documented in README deployment notes, not built.

Scope fence (DL-42): the dashboard of *meaning* — conditions, boxes,
explain, sendevent — is dsl41's and stays here. The §6a supervisor tier,
extracted or not, ships at most a boring JSON CLI and an optional
read-only top view; "free dashboard via textual-serve" is free as a demo
only — production dashboard semantics (auth, audit of signals, history,
log redaction, retention) are orchestrator concerns, never the
supervisor's.

## 12. Non-goals

HA/clustering, remote machines or agent fabric, RBAC, resource/load
management, custom calendars, retry semantics (Q4), non-child orphan
adoption (dissolved by design: the 11f supervisor makes survival a
*reattachment*, never an adoption — E4), alarm delivery beyond journal +
UI (no mail/pager integrations), cgroup/scope containment (documented
Linux hardening path, §6a).

## 13. Testing — bisimulation is the acceptance gate

1. **Bisimulation**: every SEM trace test parametrized over Oracle-direct
   and Engine(VirtualClock, FakeAdapter); traces must be identical. This is
   equivalence tier c between simulator and executor, reusing the entire
   existing fixture corpus. It is the phase-11a definition of done.
2. **Hypothesis**: random event scripts (existing strategies) through both
   paths — same property; plus feed-only vs advance+feed timer schedules.
3. **Journal**: replay reproduces the trace (property test). Crash-recovery
   integration test: real sleep jobs, SIGKILL the engine, resume, assert
   reconciliation records and terminal states.
   The lifecycle tier lives or dies on its failure matrix, so 11b/11f
   test kills at every phase boundary, not just mid-run: before/after
   spawn.json, after fork before exec, after wait observation before
   status write, after status write before reap — plus ENOSPC on the
   runs dir, a stale control socket, pid reuse (spoofed spawn.json), and
   a simulated reboot (boot_id flip).
4. **Preflight**: trigger/non-trigger fixture pair per rule.
5. **Adapters**: pgid kill, append semantics, profile sourcing; FW watcher
   over a tmpfile growing to stability.
6. **TUI**: textual pilot snapshot smoke only.

## 14. Module layout and phasing

Flat house layout: `runner.py` (clock, engine, scheduler, adapters,
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
  control socket.
- **11d** — Textual TUI (terminal).
- **11e** — `serve` via textual-serve + deployment notes.
- **11f** — supervisor tier (§6a Tier 1): detached mode, engine
  reattachment, versioned named-socket protocol + controller lease
  (frozen in `docs/supervisor-protocol.md` — the future extraction
  boundary, DL-42), import-boundary test, Linux subreaper. Completes the
  prod-grade story for long-running estates (§1).

## 15. Open questions (E-series)

House rule applies: implemented defaults are marked `# PENDING: En` in
code; none is guess-resolved.

- **E4** — jobs surviving engine restarts. RESOLVED in design by DL-41a:
  never non-child adoption; the 11f supervisor keeps parenthood alive so
  survival is reattachment. Until 11f lands, tethered semantics apply
  (§1) and resume follows §7's reconciliation ladder.
- **E5** — profile sourcing failure semantics [?]. Default: job fails with
  sh's exit code (§6).
- **E6** — FW steady-size semantics and default watch_interval [?].
  Default: two stable polls, 60s (§6).
- **E7** — verdict for an unobservable exit status (§7). Default: FAILURE
  with cause `exit_status_unobservable`; TERMINATED reserved for kills
  that actually happened. Revisit if an estate shows t()-conditioned
  recovery paths that should fire instead.
- Inherited pendings: Q3 (SEM-32 abandonment branch), Q4 (n_retrys) — the
  runner implements the documented oracle defaults and adds no new switch.
