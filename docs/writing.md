# Writing for readers

Status: binding for every human-facing document (DL-405): the README, the
pages under `docs/` that are not frozen contracts, the block cards, and the
READMEs and runbooks under `examples/`. The frozen contracts and the decision
log keep their own register, the one `spec-review` enforces. Replies and
reports by agents follow the core style contract, not this page.

The rules below follow the four reader outcomes of ISO 24495-1 (plain
language, governing principles and guidelines), stated here in this
project's words. The standard's text is not reproduced.

## 1. One reader, one task

Every document is written for one of four readers and says so in its first
paragraph, with the task the reader comes with:

- **Packager**: builds the site package, installs the services, and ships JIL
  releases on a schedule of their own.
- **Operator**: starts, watches, recovers and retires the engine; drills and
  incidents.
- **Job author**: writes an estate in JIL or in the Python DSL and gets it
  accepted for rollout.
- **Architect**: wants the design and the implementation, as background for
  any of the other three.

A document that serves two readers is two documents, or one document with a
named section per reader. The README's "Documents" section keeps one reading
track per reader; a new page joins its track in the same change.

## 2. The reader gets what they need

- Content follows the reader's task, not the code's layout or the order in
  which the design was made.
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
- Sections come in the order the reader meets them: install before start,
  start before recover, the common case before the exception.
- One concept, one place. A second place links the first.
- A procedure the reader runs is a numbered list with one action per step and
  the command complete and copyable.

## 4. The reader can understand it

- Short sentences, one idea each. Common words. A term the glossary defines
  is used exactly as defined and never renamed.
- Active voice and a named actor: "the archiver unlinks the source", not
  "the source is unlinked".
- An example before a rule when the rule is not obvious.
- Counts and measures go in a table or on a line of their own.
- No metaphor, no marketing, no changelog sentence ("now", "no longer",
  "was"). A fact is stated once, as a fact.

## 5. The reader can use it

- A document is walked by a reviewer who reads as its reader, before it
  merges: can they do the task from the page alone?
- Every claim about the code comes from a fact sheet taken from the code,
  never from memory (the `reader-docs` and `spec-review` procedures).
- A page under `docs/` carries a review stamp; the gate reports when it is
  due again.

## 6. Visuals

Use a visual when words would make the reader hold more than three things at
once: topology (hosts, units, processes, run roots, locks), a flow that
branches, or a file layout. Not for a list, a single sequence, or decoration.

- Text in git, so a diff reviews it: Mermaid where GitHub renders the page,
  ASCII where the page is read in a terminal.
- One sentence under every visual says what it shows and what it leaves out.
- Never an image of text.

## 7. State machines

Human-facing documents show a state machine in one notation, the one
`scripts/render_job_lifecycle.py` writes:

```text
STATE <- initial
TARGET = SOURCE + event [guard] // note
       || OTHER + event
```

A document embeds a generated view or links
[job-chart-lifecycle.md](job-chart-lifecycle.md) and
[chart-lifecycles.md](chart-lifecycles.md). It never hand-copies rows: a
copied row drifts from the chart, and the chart is the rule. A chart with no
generated view gets one before its document is written.

## 8. Procedure

Agents write and review documents through the `reader-docs` skill
(`.claude/skills/reader-docs/SKILL.md`), which carries the checklist for
this page, the audience brief, and the review order. A human author uses the
same checklist.
