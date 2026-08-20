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

Then work through [RUNBOOK.md](RUNBOOK.md) — twenty-one operator exercises
(stalled feeds, hung jobs, reruns, QUE_WAIT, the approval gate, restart
drills, and the boundary era below). The default night does not complete
without you.

## The boundary era

A night is one period of an estate that outlives it. RUNBOOK exercises 15
to 21 are the verbs for the rest of that life: seal the night at an instant
you choose and open the next period in place, keeping every global, hold
and latch; attest the closed period so it can be archived; adopt a run root
written before the period model existed; roll to a fresh directory; free a
lineage a crashed roll left claimed; and prune what retention licenses —
and only that.

Each exercise shows a refusal before the command that works, because the
refusals are what an operator has to recognize. Sealing too soon after a
command is refused by the retry horizon. Rolling before the closing period
is attested is refused by name. A prune with no class named deletes nothing
and says why.

`tests/test_nightbank_boundary.py` drives the same flows on this estate in
CI, so the exercises are checked rather than remembered.

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

Each night is one directory under `runs/` (data, logs, properties, control
socket). The engine's own run root is `<run>/engine` inside it: the
`journal.jsonl` sentinel, one `wal/<period>.jsonl` per period, `seals/`,
`periods/` and `catalogs/`. The LINEAGE anchor is `<run>/engine.anchor`, a
sibling of the run root and never inside it, so archiving the run root
never carries the fence away with it. A night whose lineage anchor still names its root is a RETAINED
estate: retire it through the boundary-era verbs (`dsl41 audit`, then
`verify`, roll forward or `estate prune` the licensed classes —
RUNBOOK exercises 16, 19 and 21), never by raw deletion, which would
remove the sentinel, WAL, seals and spool the lineage still reaches
(period-model §12). A training night whose anchor is gone with it — the
whole `runs/<night>` directory, anchor included — may be deleted as a
unit once stopped (Ctrl-C, and for `--detached` runs drain the
supervisor first: `dsl41 supervise shutdown --run-root <run>/engine`);
deleting a LIVE run pulls the socket, journal, and supervisor state out
from under running processes.

## Notes

- Everything is synthetic: names, calendars, incidents. No production JIL,
  no real holidays that could collide with a training night (HOL_GLOBAL is
  Jan 1 / Dec 25 only — don't train on Christmas).
- The estate doubles as a full-pipeline fixture: `lint` is clean on it, and
  `viz --format chart`, `report`, `uc`, and `rehearse` all accept it
  (see `tests/test_nightbank_example.py`).
- Repo-only; not part of the published package.
