# Nightbank — Alpenbank Global Overnight

See [offline snapshot examples](../snapshots/README.md) for synthetic observations on this graph.

A fake bank's overnight batch, sized and shaped like the real thing, for
training operators on the **real dsl41 engine**: the TUI, `sendevent` /
`query`, the supervisor, the calendar scheduler, restart/resume, the works.
Nothing here touches the engine — all fakery lives in the estate's data and
scripts (`fakework` sleeps, moves marker files, and exits with whatever the
night's incident script demands).

Three regions close follow-the-sun (Tokyo → Zurich → New York, each a
top-level box in its own timezone), then group risk consolidates, and the
night ends in a transactional start-of-day flip that waits for a human
approval. The interesting structural bit: instrument definitions and marks
are ordered only *after* positions, investment macros, and open orders
determine which universe is needed — so the estate is almost entirely
condition/file-driven and a whole "night" plays out in about 15 real
minutes. The only clocks are the three region anchors, stamped a few
minutes ahead at launch (static JIL + `~{$X}~` placeholders, one generated
properties file per night).

## Quick start

```sh
uv run dsl41 lint examples/nightbank/estate/small/*.jil -p night.properties
uv run dsl41 rehearse examples/nightbank/estate/small/*.jil -p night.properties
```

The estate is templated: `night.properties` supplies the three region
anchors, the heartbeat window and the data paths. `tests/nightbank_props.py`
builds one for a chosen UTC anchor.

## Layout

- `estate/small/` — ~80 jobs, hand-written and commented; read these first.
- `estate/bank/` — ~520 jobs, emitted by `generate.py` (same topology,
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
- [`deploy/DRILL.md`](deploy/DRILL.md) — the service drill, for a
  maintainer: the unit's start, stop, restart, reload and recovery in a
  container with systemd.

## Notes

- Everything is synthetic: names, calendars, incidents. No production JIL,
  no real holidays that could collide with a training night (HOL_GLOBAL is
  Jan 1 / Dec 25 only — don't train on Christmas).
- The estate doubles as a full-pipeline fixture: `lint` is clean on it, and
  `viz --format chart` and `rehearse` both accept it
  (see `tests/test_nightbank_example.py`).
- Repo-only; not part of the published package.
