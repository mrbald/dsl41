---
name: chart-simplify
description: Make a declared statechart smaller and clearer when the user asks for a chart review or simplification, or when a slice finds hidden control flow, copied rows or guard families in a chart. Reviews first, then runs the catalogued steps as slices with STOP conditions. Changes no observable behaviour and merges nothing on its own.
---

# Chart simplification (DL-363 to DL-366, DL-368 to DL-372)

A chart is right when a reader sees the lifecycle in its rows and nothing else
decides a move. This skill is the method that took the `job` chart from 169
rows to 151 with every golden unchanged, two hidden sequences declared and 29
copied rows gone, and the rulings that stopped three steps that would have made
it worse. Run it on any chart in `src/dsl41/machines.py`, whole or as a pass
when a slice touches one.

Words used below: a *T item* is a decision the engineer made that the brief
left open, listed for the boss's ruling; *the gate* is the full suite under
CI's coverage form, the transition gate, the chart's replay, ruff and mypy, run
with no other agent running coverage in the checkout.

## 0. Invariants of every step

- No observable change: the chart's own replay shows no difference (for the
  oracle's charts `tests/oracle_goldens.py replay`; for a runner chart its
  fixtures and journals), no trace line changes, and a cascade's items keep
  their order. When an item count changes, the entry states the measured shapes
  and the cap margin.
- A guard reads the same facts in the same order as before. Lazy facts are
  computed on first read, and an unread fact is observable (an unread window
  arms no timer), so boolean equivalence is not enough (DL-370, R9).
- A retired state, event or row id is not stored, restored or sent on the wire.
  Check the persisted configurations, `docs/protocol-evolution.md`, and any
  code that builds states from stored values (`host` builds from the stored
  `HostState`); the gate stays green while restore breaks. DL-365, DL-369 and
  DL-371 each made this check and said so in the entry.
- Ids are stable: a retired id stays out of use for good (DL-364); a new row
  takes the next id in its range.
- Every cell stays declared (§10, §13). An impossible cell is a marked
  unreachable row. In the oracle's charts the writer hook raises on a marked
  row with no writer and the coverage gate forbids its hit (job.450, job.453);
  the engine itself does not raise, so a chart with another hook needs its own
  rejection of a marked row before the step relies on it.
- The version constant moves by DL-235 alone.
- Review class: semantic when rows, events, a frozen rule or the
  oracle's writes change. `docs/agents/review.md` "Required passes" sets the required
  passes and fallbacks; `docs/agents/coordination.md` supplies the bindings. Each reviewer attacks with
  constructed input sequences, or with random fact vectors over the guards when
  only guard bodies changed, and a differential run of HEAD against the tree;
  the raiser or its named stand-in confirms a fix. Follow `docs/agents/review.md`
  "Review rounds" for the caps and escalation.
- Merging is the owner's call, or a standing owner rule that names the scope.
  This skill never adds a scope.

## 1. Review first (one read-only slice)

Three explorers in parallel, then a ruling file:

1. **Control flow outside the table.** Every place code decides the next move or the next event:
   generators (`Program` payloads), consequence dispatchers keyed by action name, writer tables
   keyed by row id, verdict helpers (an ordered tree whose labels guards compare), handlers that
   test the configuration (`"QUE_WAIT" in ctx.active`).
2. **Shape.** The state tree; regions and what reads them; events by source; guards grouped by what
   they test, with negation pairs and conjunctions; per-leaf copies and split sources; rows that
   move nothing; the other charts' sizes for scale.
3. **Engine features against use.** Hierarchy, regions, entry and exit actions, completion rows,
   choices, deferral; which the chart uses. An unused feature is usually where a hidden form lives.

Render the chart's lifecycle before judging it: `scripts/render_job_lifecycle.py`
prints the external moves of the `job` chart grouped by target in
`Target = Source + Event [guard] // note` form, the modes and holding beside,
and the ignored, unreachable, internal and self rows folded into counts.
Generalise it to the chart at hand; a reader judges the view, not the table.

Then rule, in writing, on each node with the owner's test: **state** if rows
fire on its change or select by it; **data** if guards only consult it. A
grouping state earns its place only as the source of real rows. A single-child
wrapper exists only because a source must be a state. A thing derived from
other state is still state if an interval exists where the derivation does not
hold (the holding region: a terminal job is `reserved` until settled).

Rank the findings by harm to readability and write the steps as a table with
size and what each reopens. Take the structural rulings to the boss before
the first implementation brief; the boss may consult the advisor.

Which steps apply: 1, 5, 6 and 7 fit any chart with rows and guards; 2 and 8
need completion rows and entry actions; 3 needs a payload that can carry a fact
and a hook that writes the flag; 4 needs a hook that writes the status. A chart
whose rows carry no actions (`runtime_assembly`) gets 5 and the view only.

## 2. The step catalogue (in this order; skip what does not fit)

1. **A decision in a handler is a row.** A handler that branches on a payload field or tests the
   configuration becomes one row per case with a guard that reads the fact (DL-363: job.402, job.424
   to job.426; job.234 to job.238). Two rows at one source must be exclusive (§3): a new row beside
   an ignored one at the same source needs the ignored one narrowed by a named complement guard
   (`recomputes_nothing` beside the recompute rows, DL-368).
2. **A transient state left by a self-event it sent is an automatic state.** An entry action reports
   what precedes the next write; a completion row moves on. The engine rule it needed: a completion
   carries the payload of the microstep that completed its state, the entering event for a simple
   state and the event that put its last region at rest for a composite (DL-364). Conditions: the
   completion descends from the item that entered the state, so a bound that counts items in the
   ancestry must see the right item (DL-364 moved the entering row to `start admitted` for this);
   the facts read before the writes travel on the payload; one completion row per kind of payload
   that can enter the state; the entry reports only what must precede the move, the rest follows the
   move.
3. **A flag nothing fires on is a payload fact.** Apply the §1 test first: on the `job` chart ice,
   hold and noexec stayed regions, only the arm went. Keep every old-value pair as two guarded rows
   so the coverage gate keeps both cells; add a postcondition that reads the flag before the writes
   and, after them, requires it set if a setting row fired, clear if a clearing row fired, and
   unchanged otherwise; derive the two row sets from the declaration and pin them with a test
   (DL-365).
4. **The status write is the state's entry action.** The row's writer gives the line (cause, code,
   clear); the hook writes the status the entry reported after the row writers and before the other
   intents, so a move's record reads the status written; no status-entering row may fire beside a
   writing row of another region, or a trace line would move; a status entered with no line, or a
   line with no status, raises; the initial entry reports nothing and a restored instance runs no
   action (DL-366). The cause stays the writer's: it is a transition attribute, and a
   declaration-side table for the few rows with a literal cause and no extras was a second table
   keyed by row id (R10, not landed).
5. **A rule is written once at the source the hierarchy gives it.** Collapse per-leaf copies under
   the parent; replace a split source by one row whose guard names the set it had (`_recomputes`)
   and excludes what the sources excluded by absence (a verdict equal to the current status must
   still fire nothing). A parent may hold children the copies never covered (ADMITTED gave job.453),
   and a raise the copies gave by having no writer must survive as a marked row (job.450). Where the
   collapsed row would be enabled beside an ignored one, narrow that one with a named complement
   guard, or leave the copies (DL-368 left `noexec_settles_box` as three rows; DL-369 collapsed it
   under `noexec_moves_nothing`) (DL-368).
6. **A chained event names its cause.** One event per cause instead of a `cause` field and a guard
   family switching on it; an event that cannot reach a state gets its marked row there (job.453 at
   ADMITTED). A guard that reads a payload value as an enum (the `injected status` choice) stays
   (DL-369).
7. **A condition the rule reads has a name.** Name the tree's tests; one gate order in one function;
   a guard that is another's negation or conjunction is a one-line definition calling the shared
   predicate, with the operands in the code's test order, never a combinator that sets `__name__`
   (DL-370, REJECTED).
8. **A phase whose decision is a chart row is a state.** First decide who owns the sequence: a phase
   of one instance's lifecycle becomes a region under its status with an entry action per phase and
   completion rows that decide; orchestration across instances becomes its own coordinator chart
   with one instance, whose record is the payload every completion carries (R11, DL-372). Only a
   phase that waits for a cascade is a state; a decision that waits for nothing is a choice,
   resolved inside the microstep, or it costs an item per turn and moves the item cap (DL-372,
   REJECTED, R13). For the region form: keep distinct visits distinct
   (override-only, fold-only, full); build each phase's facts at its entry, and reading them before
   the phase begins is an error; no external row from the status into its own region (it would
   repeat the status write); and only when the entries only read and the entering rows send nothing
   may the busy cell be a marked row and the run check be dropped, because then no item pops between
   an entry and its completion; an entry that sends anything voids both (DL-371, the box door). Tier
   steps that only sequence sends stay moments.

## 3. What not to do, with the reason

- **Ordered row selection in place of a verdict tree** (R9): the tree as rows needs compound guards
  that repeat their prefixes, conflicts with the depth rule across sources, and makes lazy reads a
  selection contract. The tree in one named function is the declared precedence; step 7 names its
  tests.
- **A second table keyed by row id** for a minority of rows (R10).
- **Orchestration moved after the dispatch returns** (R11, the REJECTED sentence): changes resource
  winners and resets the item budget and ancestry.
- **A `stay` branch on choices** (R4): a choice resolves after the exits, so its guards see a
  configuration without the source; writer dispatch keys on the rows an outcome fired, which a
  branch is not; ignored rows yield to a sibling region's row.
- **Guard combinators** that set names and docstrings by hand (DL-370, REJECTED): tracebacks and the
  registry lose the real name; a swapped string passes validation.

## 4. The loop per step

Brief with goal, non-goals, the invariants above, the conditions as STOP
clauses, the exact acceptance commands, a 25-line report. Engineer builds; the
boss rules on the T items; the reviewer reads the diff and performs the reviews of §0 with
probes aimed at the step's own risk (the cell table under the id mapping, the
first-read order of lazy facts, the busy cells); fixes confirmed by their
raiser; the gate; the entry appended and the index re-rendered; commit by
explicit paths; PR. Record every ruling, kept or reversed, in the programme's
rulings file, which lives outside the repo.

## 5. Acceptance of the whole pass

- A reader follows each sequence the review found hidden from the rows alone.
- Each row has one explicit route to its effects: its action names what it
  sends, its state's entry writes the status, its writer gives the line and
  the row's own writes.
- In a differential run, each retired row's count equals its replacement's;
  rows merged into one are compared as a sum, a row split in two as a
  partition.
