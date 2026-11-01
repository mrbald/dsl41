# Nightbank — Alpenbank Global Overnight

For the operator preparing a synthetic overnight estate to practise queries,
interventions and engine recovery. Follow the [operator track](../../README.md#documents)
for installation and incidents. This example leaves engine service management
to [deploy.md](../../docs/deploy.md); it supplies the estate and workers.

The estate runs real commands and file watches in one engine. All names,
calendars, inputs and incident settings are synthetic. `fakework` sleeps,
writes marker files and returns scripted results.
Three regional boxes run in Tokyo, Zurich and New York timezones. Group risk
follows them, then the start-of-day flip waits for operator approval.
The three regional anchors come from one generated properties file.
See [offline snapshot examples](../snapshots/README.md) for synthetic observations on this graph.

## Quick start

Run these steps from a development checkout with the locked environment
installed. Python needs the project's dependencies; the published package
does not contain this estate or its preparation helper.

1. Prepare one small training night, with the first region start fifteen minutes ahead:

   ```sh
   uv run python - <<'PY'
   import runpy
   from datetime import UTC, datetime, timedelta
   from pathlib import Path

   prepare = runpy.run_path("tests/nightbank_props.py")["prepare_night"]
   night = Path("/tmp/nightbank-training").resolve()
   if night.exists():
       raise SystemExit("Choose a fresh training directory before preparing another night")
   prepare(night, "small", anchor_utc=datetime.now(UTC) + timedelta(minutes=15))
   estate = Path("examples/nightbank/estate/small").resolve()
   (night / "dsl41.toml").write_text(
       'format_version = 1\n'
       '[engine]\nrun_root = "' + str(night / "engine") + '"\n'
       '[estate]\ninclude = ["' + str(estate / "*.jil") + '"]\n'
       'properties = ["' + str(night / "night.properties") + '"]\n'
       'timezone = "UTC"\n'
       '[lint]\nrefuse = "none"\nignore = []\n'
   )
   print(night / "dsl41.toml")
   PY
   ```

   The helper creates data and log directories, `night.properties`,
   `profile.env` and a copy of `incidents.conf`. The profile supplies
   `NIGHTBANK_DATA`, `NIGHTBANK_STATE`, `NIGHTBANK_INCIDENTS`,
   `NIGHTBANK_SOCKET` and PATH entries for `fakework` and the checkout's
   Python environment. Keep that checkout available while the night runs.
   Inspect `incidents.conf` before starting; it intentionally injects faults.

2. Check the interpreted estate:

   ```sh
   uv run dsl41 lint --config /tmp/nightbank-training/dsl41.toml
   ```

   With `lint.refuse = "none"`, exit 0 from lint can include findings;
   inspect its output before proceeding.

3. Rehearse it without real job processes:

   ```sh
   uv run dsl41 rehearse --config /tmp/nightbank-training/dsl41.toml
   ```

   Rehearsal uses scripted adapters. It does not execute `fakework` or prove
   the incident behaviors.

4. Start real training work in the foreground within ten minutes of preparation:

   ```sh
   uv run dsl41 run --config /tmp/nightbank-training/dsl41.toml
   ```

5. From another terminal, inspect it:

   ```sh
   uv run dsl41 query status --socket /tmp/nightbank-training/engine/control.sock
   ```

   Use the [incident guide](../../docs/incident-triage.md) for interventions
   and the [drain procedure](../../docs/deploy.md#drain-the-engine) to stop.
   A restart on this root resumes its stored revision; it does not prepare a
   fresh night or apply changed source files. Review changes through
   [reread and update](../../docs/deploy.md#reload-a-changed-estate).

If the first region's time passes before `run`, it waits until the next day.
Stop any engine on this training root and save any evidence you need.
Remove `/tmp/nightbank-training`, including its engine root and synthetic
data, then repeat preparation and the checks and start within ten minutes.
Do not prepare over an existing night or expect a restart to adopt new times.
For a service with an existing root, use [reread and update](../../docs/deploy.md#reload-a-changed-estate)
or select a fresh root as described in the installation guide.

For a portable manifest check instead of the main config, select the declared
environment explicitly:

```sh
uv run dsl41 lint --manifest "$(pwd)/examples/nightbank/estate/small/estate.toml" \
  --environment sandbox -p /tmp/nightbank-training/night.properties
```

The main config above selects JIL directly and does not load that manifest's
environments. The [main-config rule (DL-437)](../../docs/decision-log.md)
defines this input selection. For an installed service, use the complete
[installed input recipe](../../docs/deploy.md#install-the-package-and-unit).
`bank` is a separate generated profile; select its JIL and prepare its
incident settings together instead of mixing them with `small`.

## Layout

- `estate/small/` — the small estate, hand-written and commented; read these first.
- `estate/bank/` — the generated estate, emitted by `generate.py` (same topology,
  8 asset classes × 8 valuation shards; regenerate with other knobs).
- `bin/fakework` — the only worker any job runs; consumes/produces marker
  files under the run's `data/` and applies `incidents.conf` behaviors.
- `estate/<profile>/incidents.conf` — each estate's scripted failures
  (bank targets are per-asset-class names that only exist there).
- `estate/<profile>/estate.toml` — the estate's manifest (DL-407): its
  files in catalog order, its timezone and its environments.
- `deploy/dsl41-engine.service` — one systemd unit for the engine: `run`
  resumes the root it names, and the unit's stop is the engine's drain
  (`dsl41 engine stop --wait`).
- [`deploy/dsl41.toml`](deploy/dsl41.toml) — the installed service's main
  config: run root, input glob, properties, timezone and lint policy.
- [`deploy/DRILL.md`](deploy/DRILL.md) — the maintainer's service-drill entry
  point; [drill coverage and limits](../../docs/deploy.md#exercise-the-unit-before-site-use)
  identifies what its current scripts check.

## Notes

- Everything is synthetic: names, calendars, incidents. No production JIL,
  no real holidays that could collide with a training night (HOL_GLOBAL is
  Jan 1 / Dec 25 only — don't train on Christmas).
- `tests/test_nightbank_example.py` checks compiler and rehearsal behavior.
  Those checks do not qualify a site's service or workload.
- Repo-only; not part of the published package.

The supervisor and detached-job training references resolve to the
[v2 charter (DL-424)](../../docs/decision-log.md); practise the engine's drain
and same-run replacement attempts through the procedures above.
