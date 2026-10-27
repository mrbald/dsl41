# Writing for readers

Status: binding for every human-facing document (DL-405, DL-406): the README, the
pages under `docs/` that are not frozen contracts, the block cards, and the
READMEs and runbooks under `examples/`. The `spec-review` procedure (DL-225)
covers every page under `docs/` except the decision log, the citation
index and this page, for one purpose: the page states the present design and agrees with
the code. This page adds the reader rules. Where both apply, `spec-review`'s
rules on section numbers, citation tokens and history govern, and this page
adds to them. The block cards keep the shape `docs/blocks/README.md` fixes
(its headings, their order, and the link to the chart's generated reference);
§1 and §3 below do not bind a card, the other sections do. Replies and
reports by agents follow the core style contract, not this page.

The rules below follow four principles: readers get what they need, can
find it, can understand it, and can use it.

An existing page meets a rule added by DL-406 at its next change.

## 1. One reader, one task

Every document is written for one of five readers and says so in its first
paragraph, with the task the reader comes with:

- **Packager**: builds the site package (the RPM or equivalent that installs
  dsl41 and the site's services), and keeps fixed what the application's JIL
  releases run within (DL-408).
- **Operator**: starts, watches, recovers and retires the engine; drills and
  incidents.
- **Job author**: writes an estate in JIL or in the Python DSL, or brings an
  existing estate in, and gets it accepted for rollout.
- **Architect**: wants the design and the implementation, as background for
  any of the other readers.
- **Maintainer**: contributes code or documents, or runs an agent that does.

A document that serves two readers is two documents, or one document with a
named section per reader. The README's "Documents" section keeps one reading
track per reader. Every page under `docs/` that is not a frozen contract, and
every example's README, is in a track, itself or through an index page the track names; a page
inside an example's directory is reached from that README.

## 2. The reader gets what they need

- Content follows the reader's task, not the code's layout or the order in
  which the design was made.
- Content fits where the reader reads it. A procedure read at a terminal
  during an incident gives its commands first and its explanation after; a
  guide read at a desk may explain first.
- The page states, or links, every limit or gap the reader would run into,
  where they would look for it.
- How the design got here, what was rejected, and why, live in the decision
  log. A document cites the entry; it does not retell it.
- Rules live in the contract that states them. A document links the section;
  it never restates a frozen table (the block-card rule).
- A document says what it does not cover, in one sentence, with the link.

## 3. The reader can find it

- The first screen says who the page is for, what it lets them do, and where
  the rest of their track is.
- Headings name the task or the fact ("Roll out a new estate", "The RPM owns
  the delivery subtree"), not the topic ("Rollout", "Ownership").
- A new page puts its sections in the order the reader meets them: install
  before start, start before recover, the common case before the exception.
  An existing page keeps its section numbers and anchors, because code,
  docstrings and the decision log cite them by number and no gate checks
  those citations; a page whose order must change, or that is split, does so
  in its own slice that updates every citer the fact sheet lists.
- One concept, one place. A second place links the first.
- A procedure the reader runs is a numbered list with one action per step and
  the command complete and copyable.
- A warning stands before the step it guards.
- What belongs together is placed together and looks alike: every recipe
  block on a page has one layout, and every table of one kind has one
  column order.

## 4. The reader can understand it

- Short sentences, one idea each. Common words. A term the glossary defines
  is used exactly as defined and never renamed.
- Active voice and a named actor: "the archiver unlinks the source", not
  "the source is unlinked".
- An example before a rule when the rule is not obvious.
- Counts and measures go in a table or on a line of their own.
- A paragraph has one topic, and its first sentence states it.
- No metaphor, no marketing, no changelog sentence ("now", "no longer",
  "was"). A fact is stated once, as a fact.
- The tone respects the reader: no blame, and no "simply", "obviously" or
  "just" that makes a step sound easy.
- Pages of one kind for one reader (the guides of a track, the
  runbooks of an example) use one section pattern; the audience brief names
  the page to match.

## 5. The reader can use it

- A document is walked by a reviewer who reads as its reader, before it
  merges: can they do the task from the page alone?
- A question in the page's scope that a reader had to ask after reading it
  is a defect of the page. The maintainer fixes the page.
- Every claim about the code comes from a fact sheet taken from the code,
  never from memory (the `reader-docs` and `spec-review` procedures).
- A specification under `docs/` carries a `spec-review` stamp, and the gate
  reports when it is due again. A reader page, a card and a page under
  `examples/` carry no stamp; their review is the pull request.

## 6. Visuals

A visual is used for exactly these structures, and for nothing else:

- a topology of three or more parts that talk to each other (hosts, units,
  processes, run roots, locks, sockets);
- a flow with at least one branch (a decision, a retry, a failure path);
- a file or directory layout with nesting.

A list, a single sequence of steps, and decoration get no visual.

- Text in git, so a diff reviews it: Mermaid where GitHub renders the page,
  ASCII where the page is read in a terminal.
- One sentence under every visual says what it shows and what it leaves out.
- Never an image of text.

## 7. State machines

A human-facing document shows a state machine only as a generated view, and
never as hand-copied rows: a copied row drifts from the chart, and the chart
is the rule. Two generated views exist, and a page uses them this way:

- In prose, the notation `scripts/render_job_lifecycle.py` writes:

  ```text
  STATE <- initial
  TARGET = SOURCE + event [guard] // note
         || OTHER + event
  ```

  A page embeds the generated lines or links
  [job-chart-lifecycle.md](job-chart-lifecycle.md) and
  [chart-lifecycles.md](chart-lifecycles.md).
- As a chart's reference, the generated diagram and transition table in
  [state-machines.md](state-machines.md), which the block cards link by
  anchor.

A chart with no generated view gets one before its document is written.

## 8. Procedure

Agents write and review documents through the `reader-docs` skill
(`.claude/skills/reader-docs/SKILL.md`), which carries the checklist for
this page, the audience brief, and the review order. A human author uses the
same checklist.
