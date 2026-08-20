# Deployment runbook — installing and operating dsl41 on a server

Scope: a single host running one engine per estate (the runner's machine
model, DL-49/DL-52 — jobs pinned elsewhere are refused as foreign). Four
components ship in one package:

| Component | Process | How it runs |
|---|---|---|
| engine (runner + calendar scheduler + control socket) | `dsl41 run` | one long-lived foreground process per estate |
| supervisor | spawned by `dsl41 run --detached` | one per run root, outlives the engine |
| TUI | `dsl41 ui` | thin client of the control socket, attach/detach at will |
| web UI | `dsl41 serve` | thin client; one `dsl41 ui` subprocess per browser session |

Everything below assumes a POSIX server with Python ≥ 3.12 on it.

## 1. Install

Dedicated venv, pinned version, `[ui]` extra only where humans look:

```sh
python3.12 -m venv /opt/dsl41/venv
/opt/dsl41/venv/bin/pip install 'dsl41[ui]==0.9.0'   # headless host: dsl41==0.9.0
ln -s /opt/dsl41/venv/bin/dsl41 /usr/local/bin/dsl41  # or add the venv bin to PATH
dsl41 --help                                          # smoke test
python3.12 -c 'from importlib.metadata import version; print(version("dsl41"))'
```

(`uv tool install 'dsl41[ui]==0.9.0'` is the equivalent one-liner where
uv is the site convention — keep the pin there too.) The package installs no services, writes nothing
outside the run roots you name, and has no runtime network dependencies —
the engine is a foreground process you place under your init system.

The `[ui]` extra floors (`textual>=8`, `textual-serve>=1.1`) are tested
pairs (DL-46/47); do not force older ones.

## 2. Filesystem layout

```
/opt/dsl41/venv/          the pinned install
/srv/dsl41/estate/        JIL + properties files — a git checkout of a tag,
                          never hand-edited in place
/srv/dsl41/runs/<id>/     one run root per baseline (see §6 for <id> choice)
```

The run root is created by `dsl41 run` and is self-contained: `journal.jsonl`
(the WAL), `catalogs/<source_bundle_hash>/` (the post-placeholder JIL this
run actually loaded + `sources.json` with the original paths and the
sha256 of the stored — post-placeholder — text, which is not the checksum
of the file you passed when `-p` resolved anything in it), `periods/000001/manifest.json` (catalog hash and its
version, bundle address, the runtime profile and its hash, state-machine
version, and the period's own `baseline_id`/`first_index`), `runs/` + logs,
`control.sock`, and `supervisor.sock` when detached. *(Amended by DL-133
and DL-134.)* Since the periodized layout the records live in
`wal/<segment_no>.jsonl` and `journal.jsonl` is a one-line sentinel; the
root also holds `seals/<period>.json`, `seals/<period>.audit.json` and
`periods/<period>/manifest.json`, and the LINEAGE anchor lives OUTSIDE it,
at `<run-root>.anchor` by default — deliberately, so `tar`ing a root never
carries the fence away with it. Back both up. A run root written
before DL-130 has `manifest/` instead of `catalogs/` + `periods/`; every
reader still accepts it, and `dsl41 estate adopt` is how it joins a
lineage (§6a).
Run roots are `0700`, journals and job output `0600` — the WAL
carries globals and every control input, so keep the service account's
home to itself. A run root is the audit artifact of its night: retention
is a business decision, not a cleanup script's — see §2a.

## 2a. Retention — the floors, and the prune verb

*(Added by DL-135, at build of period-model §11a and §12.)* An estate root
grows: one WAL segment per period, one spool directory per run, one
`.by_run_id` entry per run, logs beside them. Nothing here removes any of
it on a timer.

**What you keep is your decision. What you may never delete is not.** The
model states a floor, and the floor is everything reachable from the
lineage head:

| kept, always | why |
|---|---|
| `journal.jsonl` (the sentinel) | the one file that says this directory belongs to a lineage. Without it an older binary reads the root as unused and starts a second estate beside the live one |
| the anchor directory — `anchor.json`, `anchor.lock`, and the claim a `claimed` head names | the lineage head and the lock that fences it. A crashed opener resumes through its claim |
| the sidecar the current period opened from, and the one it will close with | recovery selects its seal by lineage and refuses without the sidecar |
| the current period's manifest, and the next one its seal committed | the pins the next opening reads first |
| an uncommitted candidate's `staged_manifest.json` and `candidate.json` | recovery after an install-before-seal crash is decided by exactly those two files |
| the catalog bundles and `sources.json` those manifests name | recovery refuses without a catalog directory |
| the newest attestation, and any after it | the chain checkpoint every later proof stands on |
| the WAL and spool of any unattested period | its `audit` has not run yet, and this is what `audit` reads |
| the spool of any live or carried execution | the run is not over |
| a SPAWN tombstone whose effect can still be replayed | "no index entry" means "first application", so deleting one **authorizes a second spawn** of a job that already ran |

Backing up a root means backing up the anchor too. It is a sibling of the
root and not inside it, so `tar czf root.tgz /srv/dsl41/runs/<id>` takes
the estate and leaves the fence behind.

**The verb.**

```sh
dsl41 estate prune --run-root /srv/dsl41/runs/<id> --dry-run
dsl41 estate prune --run-root /srv/dsl41/runs/<id> --tombstones --keep-runs 200
dsl41 estate prune --run-root /srv/dsl41/runs/<id> --quarantine
```

`--dry-run` names every artifact and the verdict retention gives it, and
deletes nothing.
A run with no class named deletes nothing either and says so: a default
set would be a retention policy, and the policy is yours. Add
`--estate-anchor` wherever the rest of your commands need it.

The three verdicts:

- **floored** — the model refuses. The verb cannot be made to delete these,
  by any flag.
- **held** — the head has moved past it and a later checkpoint covers it,
  and it is kept anyway. One question is open — may a seal-only archive
  stand in for pruned inputs? — and until it is answered the inputs of a
  period that must stay auditable stay. Closed periods' WAL segments,
  older sidecars, older manifests and unreferenced bundles are all here.
- **prunable** — deletion is licensed by name. Two classes today: a SPAWN
  tombstone whose period is attested and whose run has ended
  (`--tombstones`: the run directory, its `.by_run_id` entry and its
  default logs, always together), and a quarantined candidate
  (`--quarantine`).

**"Attested" is what unlocks a tombstone.** Run `dsl41 audit` (§6a) first.
Until a period is attested, its whole spool is floored, because that spool
is what `audit` re-derives the period from. After it is attested, that
period's finished runs may go — and once they are gone, the period can no
longer be re-derived from its own evidence, and its attestation is the
proof that stands for it. That is the trade, and it only goes one way.

What you also lose is the run's TIMINGS in `dsl41 runs`: start and end
come from `spawn.json` and `status.json`, and a pruned spool reads as
absent. The row itself stays — it is the WAL's, not the spool's — and so
does `dsl41 journal`, which replays records and reads no spool at all.

`--keep-runs N` keeps the N newest run spools **of each job**, and
`--older-than-days D` keeps anything touched more recently than D days.
Both are your policy, not the model's. Both filter whole runs: a directory
is never removed while its index entry stays.

`--keep-runs` is per job because `run_number` is per job. One list ranked
by run number would compare numbers from different series — a busy job's
fifth run outranking a quiet job's first — and `--keep-runs 3` would then
delete the quiet job's whole history.

A removal the filesystem refuses — a permission, a directory that vanished
under the sweep — is reported and the sweep goes on, and the rest of that
run stays with it. The exit code is 2 and the report names each artifact
that did not go.

The verb reads the anchor and never locks it, so it runs against a live
engine. It cannot reach that engine's work: everything a running period
creates belongs to a period that is not attested, and is floored for that
reason alone.

## 3. Starting the engine

```sh
dsl41 run /srv/dsl41/estate/*.jil \
    --run-root /srv/dsl41/runs/<id> \
    --detached \
    --as-machine <name-your-jils-use> \
    [--timezone-map tz-aliases.json] [-p site.properties]
```

Decisions to make once, per site:

- **Tethered vs detached.** Tethered (default): engine death kills all
  jobs, durably recorded — simplest, right for dev and for estates where
  a dead engine should mean a dead night. `--detached`: jobs run under a
  per-run-root supervisor; engine restarts reattach (`--resume
  --detached`) instead of killing — the production default. Inspect with
  `dsl41 supervise list --run-root <root>`; `supervise shutdown` is the
  break-glass kill-everything — for a *stopped* engine: it must acquire
  the supervisor's fencing lease, and a live engine holds it (the
  refusal names the holder). Stop or kill the engine first and let the
  lease lapse.
- **Machine identity.** Pass `--as-machine` explicitly; the zero-config
  fallback (forward hostname) is for laptops. Jobs whose `machine:`
  resolves elsewhere are refused at preflight (`--machine-policy strict`,
  keep it).
- **Init system.** The engine runs until SIGINT/SIGTERM and shuts down
  cleanly on both. Under systemd: `Type=simple`, `Restart=on-failure`,
  `RestartPreventExitStatus=2` (exit 2 is a configuration refusal —
  see below — that a retry loop cannot fix), a sane `RestartSec`,
  and an `ExecStart` wrapper that passes `--resume` iff
  `<root>/journal.jsonl` exists — a crash-restart must resume the same
  run root, while the first start of a new baseline must not. Never
  automate the *choice* of run root: new baselines are operator actions
  (§6). *(Amended by DL-134.)* Add **3** to
  `RestartPreventExitStatus=2 3`: a sealed engine exits 3 and the next
  period is opened by an operator, not by a restart loop (§6a).

Exit codes: 0 = clean stop, 1 = engine/estate failure, 2 = refused
before start (used run root, hash mismatch, preflight ERROR, live
socket), 3 = sealed; period N+1 is ready to open (§6a). Treat 2 as "a human misconfigured something" — restarting
harder will not help, hence `RestartPreventExitStatus=2` above. (A
second engine on a live run root is refused by a socket probe, so even
a misconfigured restart loop cannot double-start — it just loops.)

Preflight ERRORs refuse the run; WARNs print, journal, and run — read
them on first deploy of a new estate, they are the lint findings that
survive into operation.

## 4. UI surfaces

- `dsl41 ui --socket <root>/control.sock` — TUI in a terminal on the
  server (or over ssh). Quitting detaches; the run is untouched.
- `dsl41 serve --socket <root>/control.sock [--host 127.0.0.1 --port 8000]`
  — the same TUI in a browser. **textual-serve ships no auth and the
  socket gives full sendevent control**: keep the loopback default and
  front it with your reverse proxy (TLS + auth) or an ssh tunnel. It is
  a separate process: start it after the engine, restart it freely,
  systemd `After=`/`BindsTo=` the engine unit if you run it as a service.
- Headless glue (every control-plane command takes
  `--socket <root>/control.sock`, `-S` for short — set
  `S=<root>/control.sock` once in ops scripts):
  `dsl41 query status --brief -S $S`, `query is-success -J <job> -S $S`
  (shell exit codes), `query subscribe -S $S` (live journal stream) for
  feeding the site monitoring.
- Offline audit: `dsl41 journal <root>/journal.jsonl <estate files>`
  replays the WAL with no engine — but it is hash-gated against the
  exact estate *at its recorded paths*: the catalog hash covers source
  file identity, so the byte-exact copies under
  `<root>/catalogs/<source_bundle_hash>/` do not pass directly from a
  different location (runner-design §7, a deliberate defer). Keep the
  estate tag checked out where `sources.json`'s recorded paths say it was;
  the stored copies are for inspection and for restoring that checkout,
  not a drop-in replay input.
- Offline history: `dsl41 runs <root>... [--job NAME] [--since ISO8601]
  [--format table|json|csv]` folds one or more run roots' journal +
  manifest + spool into one row per job run — "how long did it take, run
  after run, and did it change" (DL-113). Unlike `dsl41 journal`
  it needs no estate-file argument: it rebuilds the catalog from the run
  root's own stored inputs (DL-130's bundle, or DL-66's `manifest/` on a
  root that predates it). Name
  several run roots on one command line to carry a series across a
  baseline change — the default table marks the break rather than
  blending two catalogs into one misleading line. *(Amended by DL-136.)*
  A root that has crossed a boundary holds one WAL segment per period, and
  every retained one is read: each period is folded under its own
  catalog, so a series crosses a seal exactly as it crosses a run root.

## 5. Routine operations

Same-estate restart (patching the OS, moving the process, crash
recovery): stop the engine (SIGTERM; detached jobs keep running), start
again with the exact same file list + `--resume` (+ `--detached` if it
was). The resume gate refuses on catalog-hash or clock-domain mismatch —
no silent semantic drift. Scheduler ticks that came due while the engine
was down are dropped and journaled (`dropped STARTJOB ...`), never fired
late: schedule maintenance windows accordingly, and catch up specific
jobs afterwards with explicit, journaled `FORCE_STARTJOB`s.

## 6. JIL rollout — updating the estate

*(Amended by DL-133, at build of period-model §7; the operator surface
landed with DL-134.)* **There is a second cycle, and it is the one this
model is built around: seal → swap → open IN PLACE.** The window below
still applies to the fresh-run-root cycle, and a fresh run root is still a
correct thing to do; what it is no longer is the only way to change an
estate. A boundary closes the running period at a chosen instant T and
commits the next one, and `dsl41 run --resume` on the SAME root opens it.
The verbs are in §6a below. State does not reset: runtime globals, operator holds,
`last_end_at`, armed latches, every box's `ran_members` and `run_number`
all cross the boundary, because the boundary is a record rather than a
directory. Two sentences in this section were true only of the old cycle
and are corrected where they stand: **"latches die with the old
baseline" is false across a seal** (step 1), and step 6's "new run root"
is one of two openers (period-model §7). Both corrections stand whether or
not the CLI verb exists, because the state they are about is the record's,
not the directory's.

There is no mid-run reload, by design: the running catalog is the truth
until the engine stops, resume gates on the exact catalog hash, and a
used run root refuses re-baselining. An estate change is therefore a
**stop → swap → new run root** cycle. Editing files under a running
engine only flips the TUI's SPEC DRIFT flag (an advisory fingerprint
re-check); it changes nothing live.

**Before the window** (off the production run, any checkout):

```sh
dsl41 lint new-estate/*.jil -p site.properties        # gate on exit code
dsl41 rehearse new-estate/*.jil -p rehearse.properties # whole night, virtual clock
dsl41 viz --format chart new-estate/*.jil -p site.properties  # review the diff visually
```

Rehearse is the cheap insurance: a full night in seconds, same oracle,
scripted adapters. Fix everything here; the production window is for
swapping files, not discovering problems.

**The window, in order:**

1. **Quiesce triggers**: `ON_HOLD` every scheduled top-level job/box
   that still has a future tick — `query timers -S $S` is the list, and
   "already fired today" is not an exemption (multiple `start_times`
   and `start_mins` jobs fire again). Holds satisfy nothing downstream;
   `ON_ICE` would — it marks the job satisfied immediately. Ticks
   landing on held jobs latch (flag `A` in `query status --brief`),
   which is fine: latches are run-root state and die with the old
   baseline. *(Corrected by DL-133.* They die with the old **run root**,
   and a seal does not create one. Across a seal an armed latch
   **survives**, deliberately: dropping it at the boundary would be an
   implicit transition with no admitted input. So the operator's
   `OFF_HOLD` in the new period produces exactly one start — that is the
   whole point of the hold — and an operator who does NOT want the latch
   disarms it explicitly, with a journaled command, **before** the seal.
   The old sentence stays true of the fresh-run-root cycle below, where
   the state is genuinely thrown away.)*
2. **Drain**: let RUNNING work finish (`query status --brief -S $S`), or
   `KILLJOB` what the window cannot wait for — kill command jobs, not
   boxes (only `job_terminator` members die with a box).
3. **Stop the web UI**, if any (it is stateless; order only matters for
   tidy monitoring).
4. **Stop the engine** (SIGTERM). Detached: confirm nothing you are about
   to redefine is still alive under the supervisor (`supervise list`);
   wait it out or `supervise shutdown`. A job left running across a
   re-baseline is a process the new catalog knows nothing about.
5. **Swap the estate**: `git -C /srv/dsl41/estate checkout <new-tag>`.
6. **New run root**: `dsl41 run ... --run-root /srv/dsl41/runs/<new-id>`
   (fresh, no `--resume`). Name run roots after the baseline —
   date + estate tag serves well. The old run root stays untouched as the
   record of the old world.
7. **Verify**: preflight WARNs, `periods/000001/manifest.json` (hashes,
   versions, runtime profile) and `sources.json` (files),
   `query plan` for the expected waves, then the first scheduled fire.

**Rollback** is the same procedure with the previous tag and another
fresh run root. If VCS is ever in doubt, the old run root's `catalogs/`
holds the post-placeholder JIL that baseline actually ran — byte-exact,
though with placeholders already resolved, so prefer the tag.

## 6a. The boundary — sealing a period and opening the next

*(Added by DL-134, at build of period-model §7 and §11.)* The cycle §6
describes, as commands. It keeps the state: runtime globals, operator
holds, `last_end_at`, armed latches, every box's `ran_members` and
`run_number` all cross, because the boundary is a record rather than a
directory.

**Seal.** Steps 1–3 of §6's window are unchanged — quiesce triggers,
drain, stop the web UI. Then:

```sh
dsl41 seal --run-root /srv/dsl41/runs/<id> \
    --next /srv/dsl41/estate/*.jil \
    [-p site.properties] [--next-timezone …] [--claimed-actor you@host]
```

`seal` has two entry modes and **the lock decides which**, not a flag: an
engine holding `leader.lock` is a live engine, so the CLI stages C2 and
asks it over the control socket, and that engine then exits **code 3**
("sealed; period N+1 is ready to open") — set `RestartPreventExitStatus=2 3`
under systemd, or an init system restart-loops a sealed engine. With no
engine running, the same command takes the lock itself, replays and
reconciles, and performs the boundary as an offline leader. C1 comes from
the run root's own bundle in both modes, so the estate files the period was
launched from need not still exist.

Exit codes: 0 committed; 2 not committed and the period is still open (C1
may legitimately have advanced first — an offline sealer's `leader` record
and the cutoff's admitted ticks are C1 activity, not damage); 4 the outcome
is UNKNOWN, and the printed `request_id` is the only safe way to retry.
`--force-seal` commits inside the closing period's retry horizon and is
recorded as such in the seal.

The `--next-*` options describe the period about to OPEN
(`--next-timezone`, `--next-as-machine`, `--next-machine-policy`,
`--next-detached`, `--next-deadman`, `--next-timezone-map`). A change to
any of them is a new period exactly as a catalog change is.

**Open, in place** — the ordinary case:

```sh
dsl41 run --resume --run-root /srv/dsl41/runs/<id> /srv/dsl41/estate/*.jil
```

**Attest.** Before a period's root can be archived or rolled away from,
audit it:

```sh
dsl41 audit  --run-root /srv/dsl41/runs/<id>     # re-derive + checkpoint
dsl41 verify --run-root /srv/dsl41/runs/<id>     # validate a checkpoint
```

`audit` rebuilds the seal from the period's own evidence and refuses if the
two disagree; it needs the period's WAL, spool and manifests, and the
predecessor checkpoint present and verified. `verify` validates a
checkpoint alone — its digest, its binding to the seal it names, and the
chain it claims — which is what a rolled root can do and a full audit is
not. Auditing an old period needs the dsl41 version that produced it (§7's
venv-per-version pattern); the refusal names the version.

**Open, in a fresh root** — the physical roll, optional archival hygiene:

```sh
dsl41 audit --run-root /srv/dsl41/runs/<old>
dsl41 run --open-from /srv/dsl41/runs/<old>.anchor \
    --run-root /srv/dsl41/runs/<new> /srv/dsl41/estate/*.jil
```

It refuses unless the head is `closed`, the closing period is quiescent
(no live executions at all) and **attested**. The anchor is the
LINEAGE's, not the root's, so every later `--resume` of the new root needs
`--estate-anchor /srv/dsl41/runs/<old>.anchor`. Put it in the unit file
with the run root.

**Adopt** a run root written before the periodized layout — a `header`
journal and `manifest/`. Such a root no longer resumes; it is adopted, once:

```sh
dsl41 estate adopt /srv/dsl41/runs/<legacy> \
    --next /srv/dsl41/estate/*.jil \
    [--deadman …] [--timezone-map …]     # what `manifest/` did not record
```

It requires a **drained and settled** estate: no live wrapper, no live file
watch, nothing pending in the legacy outbox, and every admitted input
holding a durable decision. Each is a refusal, not a repair — resume the
legacy engine, let it settle, and retry. A drain is what §6's window
already does.

The legacy period's `timezone`, `as_machine`, `machine_policy` and
`detached` come from `manifest/manifest.json`'s own `options` block — the
estate's record of how it ran beats anyone's memory of it. The deadman,
the timezone table and the reconciliation windows were never recorded, so
those are the flags — and that statement is **unchecked**: the adoption
barrier is wired from the profile it is attesting, so nothing can
contradict it. It is pinned in period 1's manifest and every later
`--resume` is held to it, which is where a wrong one surfaces. Get it
right, and keep it in the unit file.

Every step is idempotent: a re-run continues from where it stopped, never
mints a second estate id, and refuses rather than re-describing a period
it already pinned. A re-run after a crash between the seal record and the
head CAS performs the CAS and reports the adoption complete.

A roll that is refused **after** it wrote the target root's sentinel
leaves that directory owned by the claim it was attempting: §1.1's
ownership rule then refuses every later roll into it. That is the rule
working, not a bug — pick a fresh directory, or remove the abandoned one
after checking it holds nothing but the sentinel and the import.

**Break-glass.** A `claimed` lineage head whose target root is gone blocks
every opener. Overriding it can FORK the lineage — two roots opening one
period, running the same `(job, run_number)` twice — so prove the claimant
is gone first:

```sh
dsl41 estate reclaim --estate-anchor /srv/dsl41/runs/<id>.anchor --force
```

It is recorded in the anchor and again in the next `segment` record with
the actor who claimed to authorize it.

**Day 2.** *(Added by DL-135.)* The things that go wrong after the first
boundary, and the move for each.

*A seal exited 4.* The outcome is UNKNOWN — the seal may or may not have
committed. Do not re-seal from scratch. Re-send the same request with the
`--request-id` the command printed: a retry that finds the boundary
committed is answered from the new period, and one that finds it did not
performs it. Do not compose a new request: a fresh `request_id` is a
different command, and once period N+1 is open the envelope it carries
names the closed period's `baseline_id` and is refused as stale.

*The engine exited 3 and the init system restarted it.* It will loop.
Exit 3 is "sealed; period N+1 is ready to open", and the opening is an
operator action. Put `RestartPreventExitStatus=2 3` in the unit file.

*`audit` printed "the registry row could not be set".* The checkpoint IS
written and durable, and the checkpoint is what `verify` and `run
--open-from` read. Only the anchor's `attested` row is outstanding, and a
live engine holds the lineage lock for its whole process lifetime. Re-run
`dsl41 audit` when the lock is free; it is idempotent and finishes the row.

*`dsl41 audit` does not name a period you expected.* With no `--period` it
names only the periods this root holds a WAL for. A rolled root holds the
seal it opened from and none of that period's evidence, by design — that
seal is this root's to `verify` and the closing root's to audit. The
anchor's registry says which root holds which period.

*`audit` refuses naming a version.* Auditing an old period runs the
interpreter that produced it. Keep the venv (§7's pattern) and run the
audit from it; the refusal names the version to use.

*A roll was refused after it wrote the target root's sentinel.* That
directory is now owned by the claim that was attempting it, and §1.1's
ownership rule refuses every later roll into it. Pick a fresh directory,
or remove the abandoned one after checking it holds nothing but the
sentinel and the import.

*Every later command on a rolled root needs the anchor.* The anchor is the
LINEAGE's, not the root's: `--estate-anchor /srv/dsl41/runs/<first>.anchor`
on `run --resume`, `audit` and `estate prune` alike. Put it in the unit
file beside the run root.

*A client subscription resumed across a boundary.* Nothing to do: `since`
is an estate-wide index and the backfill spans segments. A subscriber whose
cursor is below what the root still retains — a rolled root, for instance
— receives an explicit `{"gap": true, "earliest_retained": N}` line before
the backfill (`control-protocol.md` §5).

*The root is growing.* Attest, then prune (§2a). Nothing removes anything
on a timer.

## 7. Upgrading dsl41 itself

*(Amended by DL-133, at build of period-model §1.1.)* **The upgrade keeps
the state.** `catalog_hash` v2 excludes `meta.tool_version`, and
`dsl41_version` is not on the `segment` record at all, so a patch release
does not move a period's pins and the conservative cycle below no longer
needs a fresh run root to be safe about the format. Step 3's rollback is
still the symlink; step 2's "fresh run root" becomes "stop, flip the
symlink, `run --resume`" wherever the release notes do not say the WAL
format moved. What still requires a full drain and a new estate is a
**state-machine version** bump: one executable implements exactly one, and
a seal whose `next_period` names a different one is refused at readiness
(period-model §2.1).

The journal header and manifest record the tool version, but resume
gates on catalog hash and clock domain — not version. Do not lean on
that: treat an engine upgrade like an estate change unless the release
notes say the journal format is resume-compatible across the pair.
The conservative cycle, which needs no such promise:

1. Build the new venv beside the old (`/opt/dsl41/venv-<ver>`), smoke
   test `dsl41 --help` and a `rehearse` of the current estate with it.
2. At the next natural baseline boundary (§6 window, or the nightly
   fresh run root if the site works that way): stop old engine, flip the
   symlink, start the new version on a **fresh run root**.
3. Old venv stays until the new one has run a full cycle; rollback is
   the symlink plus another fresh root.

Same-venv `pip install -U` mid-life is for patch releases explicitly
marked resume-safe, nothing else.
