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
/opt/dsl41/venv/bin/python -c 'from importlib.metadata import version; print(version("dsl41"))'
```

(`uv tool install 'dsl41[ui]==0.9.0'` is the equivalent one-liner where
uv is the site convention — keep the pin there too.) The package installs no
services and has no runtime network dependencies —
the engine is a foreground process you place under your init system. It
writes into the run roots you name and their sibling anchor directories
(§2). The one thing it writes elsewhere is job output: a job's
`std_out_file`/`std_err_file` is used verbatim and lands wherever the JIL
says.

The `[ui]` extra floors (`textual>=8`, `textual-serve>=1.1`) are tested
pairs (DL-46/47); do not force older ones.

## 2. Filesystem layout

```
/opt/dsl41/venv/          the pinned install
/srv/dsl41/estate/        JIL + properties files — a git checkout of a tag,
                          never hand-edited in place
/srv/dsl41/runs/<id>/     a run root holds one period per baseline and
                          gains another at every in-place boundary; a
                          lineage spans several roots once it has rolled.
                          A fresh root is genesis or a physical roll
                          (§6, §6a)
```

The run root is created by `dsl41 run` and is self-contained: `journal.jsonl`
(then the WAL, now the one-line sentinel — see the amendment below),
`catalogs/<source_bundle_hash>/` (the post-placeholder JIL this
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
carries the fence away with it. Back both up. *(Amended by DL-138.)* A run root
written before DL-130 has `manifest/` instead of `catalogs/` + `periods/`. That
layout is retired: it is refused by name, not read, and there is no path from
such a root into a lineage (`docs/protocol-evolution.md`).
Run roots are `0700`; journals and job output are created `0600` — the WAL
carries globals and every control input, so keep the service account's
home to itself. `0600` is a CREATE mode: a `std_out_file` that already
exists keeps the mode it has, because appending is the vendor's
semantics. One thing widens the root: an armed access map that names a
socket group tightens every direct child to owner-only and opens the root
itself to `0710` traversal (§4). A run root is the audit artifact of its
night: retention is a business decision, not a cleanup script's — see
§2a.

## 2a. Retention — the floors, and the prune verb

*(Added by DL-135, at build of period-model §11a and §12.)* An estate root
grows: one WAL segment per period, one spool directory per dispatched CMD
or FW run (a box gets none), a `runs/.by_run_id/<run_id>` entry for each
DETACHED CMD run, logs beside them. Nothing here removes any of
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
| an ARCHIVED period's receipt, attestation and sidecar | *(DL-144.)* Its inputs are gone by policy. Delete the receipt and the absence reads as accidental LOSS and every reader refuses; delete either of the other two and the period has neither inputs nor proof |
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
dsl41 estate prune --run-root /srv/dsl41/runs/<id> --archive-inputs   # irreversible
```

`--dry-run` names every artifact and the verdict retention gives it, and
deletes nothing. With `--dry-run` and no class named it surveys ALL of
them, so you can pick from what is there.
Without `--dry-run` and with no class named it deletes nothing and exits 2
saying so: a default set would be a retention policy, and the policy is
yours. Add `--estate-anchor` wherever the rest of your commands need it.

*(DL-141.)* Name
`--estate-anchor` **alone**, with no `--run-root`, and the sweep covers
every root the registry names, in period order, as one result (§6a). Each
root is still planned on its own: the floors, the refusals and the
descriptor the removal walks are per root, and so is `--keep-runs`, which
then keeps N per job **per root** — more than asked, never less.

The three verdicts:

- **floored** — the model refuses. The verb cannot be made to delete these,
  by any flag.
- **held** — the head has moved past it and a later checkpoint covers it,
  and no class licenses deleting it. Every held row says WHICH dependency
  is in the way, so a WAL that reads "no chain checkpoint above period 3
  covers it" is telling you to attest a later period. Older sidecars,
  older manifests and unreferenced bundles live here permanently in this
  version — the archive class does not cover them.
- **prunable** — deletion is licensed by name. Three classes: a SPAWN
  tombstone whose period is attested and whose run has ended
  (`--tombstones`: the run directory, its `.by_run_id` entry and its
  default logs, always together), a quarantined candidate
  (`--quarantine`), and an archivable period's INPUTS
  (`--archive-inputs`, below).

**`--archive-inputs`: the one deletion you cannot undo.** *(DL-144, closing
period-model PR-Q3.)* It deletes an attested period's WAL segment and its
committed `staged_manifest.json` + `candidate.json`, after writing
`seals/<period>.archive.json` — the **receipt** — durably first. Afterwards
the period reads at the **attestation-verified** tier: `dsl41 audit` says
so by name, `dsl41 journal` narrates an unreplayable gap and crosses to
the next period on the checkpoint, `dsl41 runs` names the coverage it no
longer has, and nothing can ever re-derive that period again. Restoring
the files does not undo it — the receipt governs.

The order is fixed and the verb tells you where you are in it:

1. `dsl41 audit --run-root <root>` for the period **and a later one** — a
   chain checkpoint above it is what stands in for the inputs. With no
   `--period` it audits every closed period the root holds;
2. `dsl41 estate prune --run-root <root> --tombstones` for that period's
   runs. There is no period selector: the sweep takes every eligible run
   in the root it is addressed at. Until they
   are gone the archive refuses and names what remains: the tombstone
   floor resolves a run directory to a period through the SPAWN effect in
   that period's WAL, so archiving the WAL first would strand every
   tombstone it explains, floored forever;
3. archive the OLDEST unarchived period first. The verb enforces this — the
   archived periods are a prefix of what a root retains, so the segments
   that remain are always contiguous and every segment-spanning reader
   keeps working.

If a segment goes missing with **no** receipt, that is loss and not an
archive, and every reader refuses by name rather than replaying a lineage
that quietly starts later than it did. That is the whole reason the
receipt is written before the first deletion.

**"Attested" is what unlocks a tombstone.** Run `dsl41 audit` (§6a) first.
Until a period is attested, its whole spool is floored, because that spool
is what `audit` re-derives the period from. After it is attested, that
period's finished runs may go — and once they are gone, the period can no
longer be re-derived from its own evidence, and its attestation is the
proof that stands for it. That is the trade, and it only goes one way.

What you lose is the PROCESS clock in `dsl41 runs`. With a spool the row
times a run by `spawn.json` and `status.json`; with the spool gone it
falls back to the journal's own `dispatch` record and terminal transition,
and the row says which, in `clock_source`. Start and end usually survive
the prune; their source changes. The row itself always stays — it is the
WAL's, not the spool's. `dsl41 journal` reads no spool while it replays
one period, but it does read the CLOSING period's spool at every boundary
it crosses: re-deriving that seal reads the executions the seal carried.

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
  `dsl41 supervise list --run-root <root>`; `dsl41 supervise shutdown
  --run-root <root>` is the break-glass kill-everything — for a *stopped*
  engine: it must acquire the supervisor's fencing lease, and a live engine holds it (the
  refusal names the holder). Stop or kill the engine first. There is no
  TTL to wait out: the supervisor reads the closed connection as proof
  the holder is gone, so the lease is grantable at once.
- **Machine identity.** Pass `--as-machine` explicitly; the zero-config
  fallback (forward hostname) is for laptops. Jobs whose `machine:`
  resolves elsewhere are refused at preflight (`--machine-policy strict`,
  keep it).
- **Init system.** The engine runs until SIGINT/SIGTERM and shuts down
  cleanly on both. Under systemd: `Type=simple`, `Restart=on-failure`,
  `RestartPreventExitStatus=2 3` (exit 2 is a configuration refusal —
  see below — that a retry loop cannot fix, and exit 3 is a sealed
  engine), a sane `RestartSec`,
  and an `ExecStart` wrapper that passes `--resume` iff
  `<root>/journal.jsonl` exists — a crash-restart must resume the same
  run root, while the first start of a new baseline must not. Never
  automate the *choice* of run root: new baselines are operator actions
  (§6). *(Amended by DL-134.)* **3** joined the list above: a sealed
  engine exits 3 and the next period is opened by an operator, not by a
  restart loop (§6a). A unit that still says `=2` alone restart-loops
  every boundary.

Exit codes: 0 = clean stop, 1 = engine/estate failure, 2 = refused
before start (used run root, a resume gate — catalog hash, clock domain
or runtime profile — preflight ERROR, a root another engine already
leads), 3 = sealed; period N+1 is ready to open (§6a). Treat 2 as "a human misconfigured something" — restarting
harder will not help, hence `RestartPreventExitStatus=2 3` above. (A
second engine on a live run root is refused by `leader.lock`, an
`flock` the leader holds for its whole process life; the kernel
releases it when that process dies, `kill -9` included. So even a
misconfigured restart loop cannot double-start — it just loops.)

Preflight ERRORs refuse the run; WARNs print, journal, and run — read
them on first deploy of a new estate, they are the lint findings that
survive into operation.

## 4. UI surfaces

- `dsl41 ui --socket <root>/control.sock` — TUI in a terminal on the
  server (or over ssh). Quitting detaches; the run is untouched.
- `dsl41 serve --socket <root>/control.sock [--host 127.0.0.1 --port 8000]`
  — the same TUI in a browser. **textual-serve ships no auth, and an
  unarmed socket gives full sendevent control to anyone who reaches
  it**: keep the loopback default and
  front it with your reverse proxy (TLS + auth) or an ssh tunnel. It is
  a separate process: start it after the engine, restart it freely,
  systemd `After=`/`BindsTo=` the engine unit if you run it as a service.
- The socket's own perimeter is off unless you arm it. `dsl41 run
  --access-map <file>` loads a role map that gives each OS peer one of
  three tiers; a configured path that is missing or invalid refuses
  startup, and SIGHUP reloads it. A map that also names a socket group
  is what opens the run root to `0710` and the socket to `0660`; without
  one, the gate is live and the `0600` owner-only modes stand. Omit the
  option and nothing changes at all. `docs/access-model.md` is the
  contract — read it before exposing a socket to a second account.
- Headless glue (every control-plane command takes
  `--socket <root>/control.sock`, `-S` for short — set
  `S=<root>/control.sock` once in ops scripts):
  `dsl41 query status --brief -S $S`, `query is-success -J <job> -S $S`
  (shell exit codes), `query subscribe -S $S` (live journal stream) for
  feeding the site monitoring.
- Offline audit: `dsl41 journal <root> [estate files]` replays the WAL
  with no engine. *(Amended by DL-142.)* **The estate files are
  optional.** Omit them and every period's catalog is loaded from that
  period's own bundle under `<root>/catalogs/<source_bundle_hash>/`,
  by the hash its opening `segment` pins — the bundle re-parses under the
  ORIGINAL paths `sources.json` records, so it reproduces that hash
  exactly. Give them and they are the FIRST replayed period's catalog,
  hash-gated against its pin as before; later periods still come from
  their own bundles, and a supplied catalog that disagrees with a pin
  refuses rather than winning. `--permit-unknown` and `-p` therefore apply
  to the files you SUPPLY and to nothing else: a bundle holds the exact
  post-placeholder bytes the period ran, already past the launch gate. What is still true is the *path*
  sensitivity: passing the stored copies yourself, from
  `<root>/catalogs/<hash>/`, parses them under the STORED names and will
  not match (runner-design §7, a deliberate defer) — let the verb load
  them instead.
  **The replay CROSSES boundaries.** A root argument replays every
  segment the root retains, in period order; at each boundary the state
  folds through the seal exactly as an engine opening the period does,
  the next period's catalog is loaded from its bundle, and the boundary is
  printed (`period N sealed at index I; period N+1 opens in <root>`). Name
  one `wal/NNNNNN.jsonl` to replay exactly that period — it opens from
  its own seal too, and because nothing re-derives that seal there, it
  needs the predecessor **attested** (`dsl41 audit`) and says so if it is
  not. `wal/000001.jsonl` is the exception: period 1 opens from no seal,
  so it needs nothing. Name the lineage ANCHOR directory instead of a root
  and the read is estate-wide: every period, its root and its segment, in
  registry order, replayed as one lineage across the roll. A boundary is crossed only over
  a seal that proves out — the digest the record names, the record's own
  fields against the sidecar, the chain, `next_period` agreement, and the
  seal **re-derived from the period's own evidence** (period-model §11),
  which is what catches a sidecar, record and opening forged consistently together.
  Anything less refuses by name. The re-derivation costs one extra replay
  per crossed boundary.
- Offline history: `dsl41 runs <root>... [--job NAME] [--since ISO8601]
  [--format table|json|csv]` folds one or more run roots' journal +
  manifest + spool into one row per job run — "how long did it take, run
  after run, and did it change" (DL-113). Like `dsl41 journal` since
  DL-142, it needs no estate-file argument: it rebuilds the catalog from
  the run root's own stored inputs — DL-130's bundle, and since DL-138 only that:
  DL-66's `manifest/` layout is retired and refused rather than read. Name
  several run roots on one command line to carry a series across a
  baseline change — the default table marks the break rather than
  blending two catalogs into one misleading line. *(Amended by DL-136.)*
  A root that has crossed a boundary holds one WAL segment per period, and
  every retained one is read: each period is folded under its own
  catalog, so a series crosses a seal exactly as it crosses a run root.
  *(Amended at the build of PR-02f.)* Name the lineage ANCHOR directory
  in place of the roots and the list comes from the registry: one table
  across every root
  of the estate, in period order, and a root that holds two periods is
  folded once. Name it alone — mixing it with roots is refused.

## 5. Routine operations

Same-estate restart (patching the OS, moving the process, crash
recovery): stop the engine (SIGTERM; detached jobs keep running), start
again with the exact same command line + `--resume`. **The whole command
line, not only the files.** Every opener re-parses the JIL, so the file
list and its ORDER, every `-p`, and `--permit-unknown` all have to be
what they were, or the catalog hashes differently. The launch options have
to match too: the resume gate refuses on catalog-hash, clock-domain or
runtime-profile mismatch — no silent semantic drift — and the runtime
profile is `--timezone`, `--timezone-map`, `--as-machine`,
`--machine-policy`, `--detached` and `--deadman`. `--access-map` is not in
the profile and is not gated: omit it and the run comes back with no
perimeter (§4). Keep the whole line in the unit file.

Scheduler ticks that came due while the engine
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
restart, in one of two shapes: the **seal → swap → open in place** cycle
of §6a, or the **stop → swap → new run root** cycle below. Editing files
under a running engine only flips the TUI's SPEC DRIFT flag (an advisory fingerprint
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
   that still has a future tick. `dsl41 query timers -S $S` is where the
   list comes from, and it is a SUPERSET: it holds every pending oracle
   timer, every scheduled job's next tick — box members included — and
   every live filewatch as a due-less row. Take the `kind=schedule` rows
   and drop the ones that name a box member. "Already fired today" is not
   an exemption (multiple `start_times` and `start_mins` jobs fire
   again). Holds satisfy nothing downstream;
   `ON_ICE` would — it marks the job satisfied immediately. Ticks
   landing on held jobs latch (flag `A` in `query status --brief`),
   which is fine: latches are run-root state and die with the old
   baseline. *(Corrected by DL-133.* They die with the old **run root**,
   and a seal does not create one. Across a seal an armed latch
   **survives**, deliberately: dropping it at the boundary would be an
   implicit transition with no admitted input. So the operator's
   `OFF_HOLD` in the new period produces exactly one start — that is the
   whole point of the hold. An operator who does NOT want that start has
   no verb for it today: `dsl41 sendevent` has no disarm, so plan the
   window around the one start the `OFF_HOLD` will produce.
   The old sentence stays true of the fresh-run-root cycle below, where
   the state is genuinely thrown away.)*
2. **Drain**: let RUNNING work finish (`query status --brief -S $S`), or
   `KILLJOB` what the window cannot wait for — kill command jobs, not
   boxes (only `job_terminator` members die with a box).
3. **Stop the web UI**, if any (it is stateless; order only matters for
   tidy monitoring).
4. **Stop the engine** (SIGTERM). Detached: confirm nothing you are about
   to redefine is still alive under the supervisor (`dsl41 supervise list
   --run-root <root>`); wait it out or `dsl41 supervise shutdown
   --run-root <root>`. A job left running across a re-baseline is a
   process the new catalog knows nothing about.
5. **Swap the estate**: `git -C /srv/dsl41/estate checkout <new-tag>`.
6. **New run root**: `dsl41 run ... --run-root /srv/dsl41/runs/<new-id>`
   (fresh, no `--resume`). Name run roots after the baseline —
   date + estate tag serves well. The old run root stays untouched as the
   record of the old world.
7. **Verify**: preflight WARNs, `periods/000001/manifest.json` (hashes,
   versions, runtime profile) and `sources.json` (files),
   `dsl41 query plan -S $S` for the expected waves, then the first
   scheduled fire. Repoint `S` first: `S=/srv/dsl41/runs/<new-id>/control.sock`
   — the old root's socket went with its engine.

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
    --next /srv/dsl41/estate/first.jil --next /srv/dsl41/estate/second.jil \
    [-p site.properties] [--next-timezone …] [--claimed-actor you@host]
```

`--next` is an OPTION, not the positional file list `dsl41 run` takes:
name it once per file. A shell glob after one `--next` is refused as an
extra argument, so expand the estate yourself. The order is part of
`source_bundle_hash`, so use the order `dsl41 run` will open the period
with.

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
is UNKNOWN — read the estate before you retry, and then retry only with
the printed `request_id` (Day 2, below).
`--force-seal` commits inside the closing period's retry horizon and is
recorded as such in the seal.

The `--next-*` options describe the period about to OPEN
(`--next-timezone`, `--next-as-machine`, `--next-machine-policy`,
`--next-detached`, `--next-deadman`, `--next-timezone-map`). A change to
any of them is a new period exactly as a catalog change is — the model's
rule, and the opener holds you to it: see "Open, in place" below.

**They do not inherit C1. State the whole profile every time.** An omitted
`--next-*` takes its own default, not the running period's: no
`--next-timezone` means UTC, no `--next-as-machine` means no declared
machine, no `--next-detached` means TETHERED. Sealing a detached period
with a bare `--next` therefore commits a tethered successor. And
`--next-deadman` needs `--next-detached`; alone it exits 2 before C2 is
staged, exactly as `--deadman` needs `--detached` on `dsl41 run`.

**Open, in place.** *(Amended by DL-151.)* One command, whatever the
boundary moved. The FILES are C2's and so are the OPTIONS: state every
`--next-*` the seal staged again on the opener, without the `--next-`
prefix (`--next-timezone Europe/Zurich` → `--timezone Europe/Zurich`).
A catalog-only boundary therefore repeats the closing period's options,
because that is what it staged.

Wrong options refuse and write NOTHING: the successor's segment is not
created, the lineage head does not move, and the refusal names the fields
that disagree (`runtime-profile mismatch on <field>`), so the corrected
command opens the same committed boundary. That holds for the fields the
engine wires and for the two it cannot see — `--next-as-machine`,
`--next-machine-policy` — alike; before DL-151 the first pair took two
commands to open and the second pair opened silently under the OLD
machine identity.

```sh
dsl41 run --resume --run-root /srv/dsl41/runs/<id> \
    --detached --as-machine <name> /srv/dsl41/estate/*.jil
```

**Attest.** Before a period's root can be archived or rolled away from,
audit it:

```sh
dsl41 audit  --run-root /srv/dsl41/runs/<id>     # re-derive + checkpoint
dsl41 verify --run-root /srv/dsl41/runs/<id>     # validate a checkpoint
```

`audit` rebuilds the seal from the period's own evidence and refuses if the
two disagree; it needs the period's WAL, spool and manifests, and the
predecessor checkpoint present and verified. Period 1 is the base case and
needs no predecessor. `verify` validates a checkpoint alone — its digest,
its binding to the seal it names, and the chain it claims — which is what
a rolled root can do and a full audit is not. Auditing a period whose STATE-MACHINE VERSION differs from this
binary's needs the dsl41 version that produced it (§7's venv-per-version
pattern); the refusal names the version. A period run by an older
release of the same state-machine version audits under the current
binary.

**Open, in a fresh root** — the physical roll, optional archival hygiene:

```sh
A=/srv/dsl41/runs/<first>.anchor                 # whatever genesis used
dsl41 audit --run-root /srv/dsl41/runs/<old> --estate-anchor $A
dsl41 run --open-from $A --run-root /srv/dsl41/runs/<new> \
    --detached --as-machine <name> -p site.properties \
    /srv/dsl41/estate/*.jil
```

The opener is a full launch line, exactly as §5 says: the C2 files in
their order, every `-p`, `--permit-unknown` if the estate needs it, and
the run options.

It refuses unless the head is `closed`, the closing period is quiescent
(no live executions at all) and **attested**. The anchor is the
LINEAGE's, not the root's: a roll creates no new one, so `$A` is the
anchor genesis used for every later roll — `<first>.anchor` when genesis
named none, and whatever `--estate-anchor` it did name otherwise. Never
`<old>.anchor` after the first roll. Every later `--resume` of the new
root needs `--estate-anchor $A` too. Put it in the unit file with the
run root.

**Reading the whole estate.** *(DL-141.)* After a roll the estate is more than one directory, and which root
holds which period is the anchor's registry to answer, not yours. Four
verbs read it, and all four are addressed the same way — **name the
lineage ANCHOR where you would name a run root**:

```sh
A=/srv/dsl41/runs/<first>.anchor
dsl41 audit --estate-anchor $A                  # every closed period, in its own root
dsl41 journal $A                                # every segment, replayed in period order
dsl41 runs $A                                   # one table across every root
dsl41 estate prune --estate-anchor $A --dry-run # one retention result
```

`audit` and `estate prune` already take `--estate-anchor`, so naming it
with **no `--run-root`** is their estate-wide form; `runs` and `journal`
take their root as an argument, so the anchor goes there instead. A verb
given neither address refuses rather than guessing.

Each of the four covers every period it can, and refuses rather than
guessing: a root the registry names
that is missing, holds no sentinel, holds one that cannot be read, belongs
to another estate, or has lost the segment it is registered for stops the
command by name. Two things are left out and SAID out loud instead — a
registry row whose first segment is not durable yet, which every
cross-period reader ignores, and, for `audit`, a period that is still
open. Nothing is skipped quietly — a total that silently left a
root out is worse than no total. If you have archived a root away on
purpose, use the single-root form for the roots you still have.

One limit, stated where you meet it: `estate prune` plans each root
separately — the floors, the refusals and `--keep-runs` are per root —
because a plan is bound to the root it was computed over. `dsl41 journal`
names every segment and **replays all of them** (DL-142): each boundary is
folded through its seal, the next period's catalog comes from its own
bundle, and the crossing is printed. It needs no estate-file argument, for the
same reason `runs` does not — the estate holds its own catalogs.

**Adoption is retired (DL-138).** `dsl41 estate adopt` took a run root
written before the periodized layout — a `header` journal and `manifest/` —
and translated it into period 1 of a new lineage. No dsl41 estate ran in
production, so the verb had nothing to adopt. It is gone, and so are the read
paths it fed: a `header` journal, a `catalog_hash_version` of 1, a `result` or
standalone `effect` record and a `manifest/manifest.json` layout are each
refused by name, citing DL-138.

**A run root written before the boundary era is not adoptable.** There is no
supported path from one into a lineage. Start a new estate with `dsl41 run`
and let the old root stand as the archive of the nights it holds.
`docs/protocol-evolution.md` is the contract that governs a retirement like
this one — what each protocol tolerates, how long its instances live, and what
has to be true before a reader may drop a dialect.

The `estate` group keeps its other verbs: `reclaim` (below) and `prune` (§2a).

A roll that is refused **after** it wrote the target root's sentinel
leaves that directory owned by the claim it was attempting. Period-model
§1.1's ownership rule then admits exactly one thing: the SAME roll,
retried. Fix what it refused on and re-run the identical `dsl41 run
--open-from` — same anchor, same target root — and the claim resumes,
because a claim is idempotent on its id. Any OTHER roll into that
directory is refused. That is the rule working, not a bug.

Whether you may roll somewhere ELSE instead depends on the lineage HEAD,
not on the directory. Still `closed` — the roll died before it took its
claim — and a fresh target root is a normal roll. Already `claimed`, and
only that claim's own target is accepted: retry it, or prove the claimant
gone and use the break-glass below. Deleting the abandoned directory
clears no claim.

**Break-glass.** A `claimed` lineage head whose target root is gone blocks
every opener. Overriding it can FORK the lineage — two roots opening one
period, running the same `(job, run_number)` twice — so prove the claimant
is gone first:

```sh
dsl41 estate reclaim --estate-anchor $A --force
```

It is recorded in the anchor and again in the next `segment` record with
the actor who claimed to authorize it.

**Day 2.** *(Added by DL-135.)* The things that go wrong after the first
boundary, and the move for each.

*A seal exited 4.* The outcome is UNKNOWN — the seal may or may not have
committed. **Read the estate before you send anything.** A committed
boundary left `seals/<N>.json`, a `seal` record at the end of
`wal/<N>.jsonl` and a `closed` head in the anchor; if they are there, the
boundary is done and the next move is to OPEN it, never to seal again.
If they are not, re-send the SAME request with the `--request-id` the
command printed — a retry the still-open period recognises is answered
from its own decision and applies nothing twice. Never compose a fresh
`request_id` for a retry: a new id is a new command. *(Amended by
DL-151.)* A retry that finds the boundary ALREADY committed is answered
from the seal it committed — the same digest, the same next period, and no
second boundary — whether the root is live or offline; before DL-151 that
route existed in the engine and the CLI could not reach it.

*The engine exited 3 and the init system restarted it.* It will loop.
Exit 3 is "sealed; period N+1 is ready to open", and the opening is an
operator action. Put `RestartPreventExitStatus=2 3` in the unit file.

*`audit` printed "the registry row could not be set".* The checkpoint IS
written and durable, and the checkpoint is what `verify` and `run
--open-from` read. Only the anchor's `attested` row is outstanding, and a
live engine holds the lineage lock for its whole process lifetime. Re-run
`dsl41 audit` when the lock is free; it is idempotent and finishes the row.
An estate-wide audit does not stop there: every other period is still
audited, and the last line says how many rows are outstanding.

*`dsl41 audit` does not name a period you expected.* With no `--period` it
names the periods this root holds evidence for — a WAL, or an archive
receipt where the inputs went under `--archive-inputs`. A rolled root holds the
seal it opened from and none of that period's evidence, by design — that
seal is this root's to `verify` and the closing root's to audit. The
anchor's registry says which root holds which period.

*`audit` refuses naming a version.* The period ran a different
STATE-MACHINE version, and auditing it runs the interpreter that produced
it. Keep the venv (§7's pattern) and run the audit from it; the refusal
names the version to use. A patch-release gap alone does not trigger
this.

*A roll was refused after it wrote the target root's sentinel.* That
directory is now owned by the claim that was attempting it. Re-run the
IDENTICAL `dsl41 run --open-from` and it resumes; period-model §1.1's
ownership rule refuses any other roll into it. To roll somewhere else,
read the head first: `closed` accepts a fresh target root, `claimed`
accepts only its own — retry it, or reclaim it after proving the claimant
is gone.

*A command on a rolled root refuses, naming an anchor.* The anchor is the
LINEAGE's, not the root's, and four verbs take it:
`--estate-anchor /srv/dsl41/runs/<first>.anchor`
on `run --resume`, `seal`, `audit` and `estate prune` alike. The other
readers are addressed by root or by socket and take no anchor. Put it in the unit
file beside the run root. To read the estate rather than one of its roots,
name that anchor and no root at all (§6a, "Reading the whole estate").

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

The `leader` record names the tool version, but resume
gates on catalog hash, clock domain and runtime profile — not version. Do not lean on
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
