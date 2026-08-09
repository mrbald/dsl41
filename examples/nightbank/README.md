# Nightbank — Alpenbank Global Overnight

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
uv run examples/nightbank/bin/nightbank up          # TUI night, incidents on
uv run examples/nightbank/bin/nightbank up --headless --no-incidents
uv run examples/nightbank/bin/nightbank up --estate bank   # ~520 jobs
```

Then work through [RUNBOOK.md](RUNBOOK.md) — twelve operator exercises
(stalled feeds, hung jobs, reruns, QUE_WAIT, the approval gate, restart
drills). The default night does not complete without you.

## Layout

- `estate/small/` — ~80 jobs, hand-written and commented; read these first.
- `estate/bank/` — ~520 jobs, emitted by `generate.py` (same topology,
  8 asset classes × 8 valuation shards; regenerate with other knobs).
- `bin/nightbank` — launcher: builds a run directory under `runs/`,
  computes the night's properties (region anchors in local wall time),
  and execs `dsl41 run`. Also `props` and `drop-file` subcommands.
- `bin/fakework` — the only worker any job runs; consumes/produces marker
  files under the run's `data/` and applies `incidents.conf` behaviors.
- `estate/<profile>/incidents.conf` — each estate's scripted failures
  (bank targets are per-asset-class names that only exist there).

Each night is one directory under `runs/` (data, logs, properties, WAL
journal, manifest, control socket). Delete old ones freely — but stop
the night first (Ctrl-C, and for `--detached` runs drain the supervisor:
`dsl41 supervise shutdown --run-root <run>/engine`); deleting a LIVE
run pulls the socket, journal, and supervisor state out from under
running processes.

## Notes

- Everything is synthetic: names, calendars, incidents. No production JIL,
  no real holidays that could collide with a training night (HOL_GLOBAL is
  Jan 1 / Dec 25 only — don't train on Christmas).
- The estate doubles as a full-pipeline fixture: `lint` is clean on it, and
  `viz --whole-graph`, `report`, `uc`, and `rehearse` all accept it
  (see `tests/test_nightbank_example.py`).
- Repo-only; not part of the published package.
