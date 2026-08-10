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
/opt/dsl41/venv/bin/pip install 'dsl41[ui]==0.8.0'   # headless host: dsl41==0.8.0
ln -s /opt/dsl41/venv/bin/dsl41 /usr/local/bin/dsl41  # or add the venv bin to PATH
dsl41 --help                                          # smoke test
python3.12 -c 'from importlib.metadata import version; print(version("dsl41"))'
```

(`uv tool install 'dsl41[ui]==0.8.0'` is the equivalent one-liner where
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
(the WAL), `manifest/` (the post-placeholder JIL this run actually loaded +
`manifest.json` with tool version, catalog hash, input sha256s, launch
options), `runs/` + logs, `control.sock`, and `supervisor.sock` when
detached. Run roots are `0700`, journals and job output `0600` — the WAL
carries globals and every control input, so keep the service account's
home to itself. A run root is the audit artifact of its night: retention
is a business decision, not a cleanup script's.

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
  (§6).

Exit codes: 0 = clean stop, 1 = engine/estate failure, 2 = refused
before start (used run root, hash mismatch, preflight ERROR, live
socket). Treat 2 as "a human misconfigured something" — restarting
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
  file identity, so the byte-exact copies under `<root>/manifest/`
  do not pass directly from a different location (runner-design §7,
  a deliberate defer). Keep the estate tag checked out where
  `manifest.json`'s recorded paths say it was; the manifest copies are
  for inspection and for restoring that checkout, not a drop-in
  replay input.

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
dsl41 viz --whole-graph new-estate/*.jil -p site.properties  # review the diff visually
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
   baseline.
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
7. **Verify**: preflight WARNs, `manifest.json` (hash, version, files),
   `query plan` for the expected waves, then the first scheduled fire.

**Rollback** is the same procedure with the previous tag and another
fresh run root. If VCS is ever in doubt, the old run root's `manifest/`
holds the post-placeholder JIL that baseline actually ran — byte-exact,
though with placeholders already resolved, so prefer the tag.

## 7. Upgrading dsl41 itself

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
