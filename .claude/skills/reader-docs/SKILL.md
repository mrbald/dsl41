---
name: reader-docs
description: Write, split or restructure one human-facing document (README sections, unfrozen docs/ pages, block cards, example READMEs and runbooks) for one named reader, under docs/writing.md. Use when the user asks for a reader-facing document, a documentation track, or a rewrite for a packager, operator, job author, architect or maintainer. Takes the document path and the reader. Does not merge until the boss accepts the pass.
---

# Reader-facing documents (DL-405)

One document, one reader, one PR. The argument is the document path and the
reader (`/reader-docs docs/site-package.md packager`). The rules are in
[docs/writing.md](../../../docs/writing.md); this skill is the procedure
that applies them. For a frozen contract, use `spec-review` instead; this
skill never changes a rule's meaning. Where both procedures apply to a page
under `docs/`, `spec-review`'s rules on section numbers, citation tokens
and history govern (writing.md's status paragraph).

## Step 0: the audience brief

Before any reading, write the brief, under ten lines. It goes into the PR
body, so it can be checked later:

- Reader: one of packager, operator, job author, architect, maintainer.
- Task: what the reader comes to do, as a verb phrase.
- Done: how the reader knows they have finished (a running service, an
  accepted estate, a recovered engine).
- Prior knowledge assumed, and the glossary terms the page may use without
  defining.
- Where the reader reads it: a terminal during an incident, or a desk; and
  the existing page of the same kind whose section pattern it follows, or
  "none" for the first page of a kind.
- Sources: the contracts, code and examples the page draws on.
- Out of scope: what the page links instead of covering.

The brief is the spec for the writer and the reviewer. A page with no brief
is not written.

## Step 1: the fact sheet

Delegate a read-only fact sheet, as `spec-review` step 1 describes: per
planned section, what the code says about every claim the section will
make (verbs and options from `--help`, constants, module and function names,
test names), plus every existing document that already covers the ground,
with the section to link. For an existing page, the sheet also lists every
place that cites it: search `src/`, `tests/`, `scripts/`, `docs/` and
`examples/` for the page's stem, then for bare `ssN` and `§N` references in
every file that names it. The writer works from the sheet, never from
memory. For a packager or operator page, the sheet also lists the files
under `examples/*/deploy/` and `scripts/` the page will name.

## Step 2: the draft

Write from the brief and the sheet. An existing page keeps its section
numbers and anchors (writing.md §3); a reorder or a split is its own slice
that updates every citer the sheet lists. Check the draft against this list, section by
section, then as a whole:

Relevant (writing.md §2)
- Every section serves the brief's task. A section that serves another
  reader moves to that reader's page, with a link.
- Commands before explanation where the brief says the page is read at a
  terminal during an incident.
- The page states, or links, every limit or gap the reader would hit,
  where they would look.
- History and rejected options are not told; the decision entry is cited.
- No rule is restated from a frozen table; the section is linked.

Findable (§3)
- The first paragraph names the reader, the task, and the track.
- Headings are tasks or facts. A new page is in the reader's order.
- Procedures are numbered, one action per step, commands complete.
- A warning precedes the step it guards.
- What belongs together is placed together; recipe blocks and tables of
  one kind share one layout.
- One concept has one home; the second mention links it.

Understandable (§4)
- One idea per sentence; common words; glossary terms used as defined.
- Active voice with a named actor.
- An example precedes a non-obvious rule.
- One topic per paragraph; the first sentence states it.
- Numbers sit in a table or on their own line.
- No metaphor, marketing or changelog sentence; no blame, no "simply",
  "obviously" or "just" that makes a step sound easy.
- The page follows the section pattern of the page the brief names.

Usable (§5)
- A reader of the brief's kind could do the task from the page alone.
- Every code claim traces to a fact-sheet line.
- The page says what it leaves out, with the link.

Visuals and state machines (§6, §7)
- A visual appears only for one of the three structures §6 names; it is
  text in git with one sentence under it.
- A state machine is a generated view or a link to one: the renderer's
  equation notation in prose, the generated diagram and table of
  `state-machines.md` as a chart's reference. No hand-copied rows. A chart
  without a view gets a renderer change first, as its own slice.

Cross-vendor drafting: DL-405 lets Sol (`codex exec -m gpt-6.1-sol`, xhigh,
`--sandbox workspace-write`) write the draft of a reader page from the brief
and the sheet, one page per run, with this checklist in its prompt. The
top level rules on the voice, and the diff gets the same review as any
other.

## Step 3: reconciliation

When the draft and the code disagree, the decision log decides, as
`spec-review` step 3 says: a later entry rules for the code; no entry means
the code is wrong, and the finding goes to the PR body as a fact, with file
and line. The page changes neither a rule nor the code.

## Step 4: verification and review

Stage the new or changed page first (`git add` the path): the decision
index renderer and its test select tracked paths only, so a new untracked page
leaves `docs/decision-index.md` stale and CI red. Then run the full local
gates listed under "Verify a change" in `docs/agent-workflow.md`, starting
with `scripts/arch_check.py` (test names in backticks, the source map,
links) and `scripts/render_decision_index.py`. The tests most likely to trip on a
rewritten page parse it: the decision index rendering, the operator recipes,
the nightbank example and deploy tests, the architecture and block-card
tests, the lifecycle view and the simulation register. Report each command
with its exit status.

Then one Opus reviewer, read-only, that writes its findings to a scratch
file and reports a ranked table. Its hunt order: false claims against the
code and the fact sheet; a fact the reader needs that is missing; a rule
restated instead of linked; history that survived; then the checklist above,
and last, broken links and anchors. The reviewer reads the page once more as
the brief's reader and says whether they could do the task.

A page that touches instruction policy, or restates how a frozen contract
is used, also gets the Astra pass the repository's "Self-review" requires.
Rule on every finding before the commit; three rounds at most.

## Step 5: delivery

One document per PR, on a branch named `docs/<stem>`. The PR body carries
the brief, the reconciliation findings, and the review table. Ask before
merging. This skill stamps nothing: a specification's `spec-review` stamp
and its 800-line clock belong to that procedure, and a reader page, a card
or a page under `examples/` has no stamp. The README "Documents" tracks are
updated in the same PR as any page they list.

After the merge, a question in the page's scope that a reader had to ask
is a defect of the page. The maintainer fixes the page, as its own
slice under this procedure.
