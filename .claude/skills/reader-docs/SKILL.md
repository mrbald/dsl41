---
name: reader-docs
description: Write, split or restructure one human-facing document (README sections, unfrozen docs/ pages, block cards, example READMEs and runbooks) for one named reader, under docs/writing.md. Use when the user asks for a reader-facing document, a documentation track, or a rewrite for a packager, operator, job author or architect. Takes the document path and the reader. Does not merge or stamp until the boss accepts the pass.
---

# Reader-facing documents (DL-405)

One document, one reader, one PR. The argument is the document path and the
reader (`/reader-docs docs/site-package.md packager`). The rules are in
[docs/writing.md](../../../docs/writing.md); this skill is the procedure
that applies them. For a frozen contract, use `spec-review` instead; this
skill never changes a rule's meaning.

## Step 0: the audience brief

Before any reading, write the brief to the review directory, under ten
lines:

- Reader: one of packager, operator, job author, architect.
- Task: what the reader comes to do, as a verb phrase.
- Done: how the reader knows they have finished (a running service, an
  accepted estate, a recovered engine).
- Prior knowledge assumed, and the glossary terms the page may use without
  defining.
- Sources: the contracts, code and examples the page draws on.
- Out of scope: what the page links instead of covering.

The brief is the spec for the writer and the reviewer. A page with no brief
is not written.

## Step 1: the fact sheet

Delegate a read-only fact sheet, as `spec-review` step 1 describes: per
planned section, what the code says about every claim the section will
make (verbs and options from `--help`, constants, module and function names,
test names), plus every existing document that already covers the ground,
with the section to link. The writer works from the sheet, never from
memory. For a packager or operator page, the sheet also lists the files
under `examples/*/deploy/` and `scripts/` the page will name.

## Step 2: the draft

Write from the brief and the sheet. Check the draft against this list,
section by section, then as a whole:

Relevant (writing.md §2)
- Every section serves the brief's task. A section that serves another
  reader moves to that reader's page, with a link.
- History and rejected options are not told; the decision entry is cited.
- No rule is restated from a frozen table; the section is linked.

Findable (§3)
- The first paragraph names the reader, the task, and the track.
- Headings are tasks or facts. Order is the reader's order.
- Procedures are numbered, one action per step, commands complete.
- One concept has one home; the second mention links it.

Understandable (§4)
- One idea per sentence; common words; glossary terms used as defined.
- Active voice with a named actor.
- An example precedes a non-obvious rule.
- Numbers sit in a table or on their own line.
- No metaphor, marketing or changelog sentence.

Usable (§5)
- A reader of the brief's kind could do the task from the page alone.
- Every code claim traces to a fact-sheet line.
- The page says what it leaves out, with the link.

Visuals and state machines (§6, §7)
- A visual appears only where the reader would hold more than three things
  at once; it is text in git with one sentence under it.
- A state machine is a generated view or a link to one, in the renderer's
  notation. No hand-copied rows. A chart without a view gets a renderer
  change first, as its own slice.

Cross-vendor drafting: Sol (`codex exec -m gpt-6.1-sol`, xhigh,
`--sandbox workspace-write`) may write the draft from the brief and the
sheet, one page per run, with the checklist in its prompt. Its diff gets the
same review as any other.

## Step 3: reconciliation

When the draft and the code disagree, the decision log decides, as
`spec-review` step 3 says: a later entry rules for the code; no entry means
the code is wrong, and the finding goes to the PR body as a fact, with file
and line. The page changes neither a rule nor the code.

## Step 4: verification and review

Run `scripts/arch_check.py` first (test names in backticks, the source map,
links), then the docs tests:

```sh
uv run python scripts/arch_check.py
uv run pytest -q tests/test_docs_links.py tests/test_docs_hygiene.py tests/test_arch_check.py
```

Then one Opus reviewer, read-only, that writes its findings to a scratch
file and reports a ranked table. Its hunt order: false claims against the
code and the fact sheet; a fact the reader needs that is missing; a rule
restated instead of linked; history that survived; then the checklist above,
and last, broken links and anchors. The reviewer reads the page once more as
the brief's reader and says whether they could do the task.

A page that touches instruction policy, or restates how a frozen contract
is used, also gets the Astra pass the repository's "Self-review" requires.
Rule on every finding before the commit; three rounds at most.

## Step 5: delivery and stamp

One document per PR, on a branch named `docs/<stem>`. The PR body carries
the brief, the reconciliation findings, and the review table. Ask before
merging. A page under `docs/` is stamped on `main` after the merge the way
`spec-review` step 5 stamps, with the tag prefix `spec-review/<stem>/` and
the outcome `reader`. The README and the pages under `examples/` carry no
stamp; the README "Documents" tracks are updated in the same PR as any page
they list.
