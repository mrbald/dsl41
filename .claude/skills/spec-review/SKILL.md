---
name: spec-review
description: Rewrite one specification under docs/ so it states the present design only, in plain dry English, and reconcile it with the code. Use when the user asks for a spec review or scripts/arch_check.py reports one due. Takes the document path. Does not stamp a document until the boss accepts the pass.
---

# Specification review (DL-225)

One document per pass. The argument is the document path
(`/spec-review docs/ir-design.md`). Without one, print the table and take
the first due document in the order under "Which document":

```sh
uv run python scripts/arch_check.py --spec-status
```

The table lists every specification with its newest `spec-review/<stem>/*`
tag, the lines changed under `src/` since that tag, and whether a review is
due: no tag, or more than 800 lines. The ordinary gate run prints one
advisory line when any document is due. Neither changes the exit code, and
both stay silent in a shallow clone, where tags are absent.

## Which document

In scope: every `docs/*.md` except `decision-log.md`, the append-only
history, and `citation-index.md`, a registry (DL-230).
`simulation-coverage.md` is held to the coverage register by
`tests/test_simulation_register.py`: edit its prose only, never its table.

Calibration order for the first pass over the shelf: `ir-design.md` first
(not frozen, densely cited), then `autosys-semantics.md` and
`stonebranch-semantics.md`, then the remaining unfrozen documents, then the
frozen contracts, with `period-model.md` last. After the first pass, take
the due document with the most lines changed.

## Step 1: the fact sheet

Delegate a read-only fact sheet before writing anything. It lists, per
section of the document, what the code says about every claim the section
makes: verbs and options from `--help`, constants and their values, module
and function names, and test names cited in backticks. It also lists every
place that cites the document's sections. Search `src/`, `tests/`,
`scripts/`, and `docs/` for the document's stem; then, in every file that
names the document, search for bare `ssN` and `§N` references, which the
shorthand rules in `docs/citation-index.md` bind to that document. The
writer works from the sheet, not from memory.

## Step 2: the rewrite

Work paragraph by paragraph, then each chapter as a whole, then the whole
document. At each level:

- Drop how the design got here: phase labels, "since DL-x", superseded
  drafts, closed questions told as a story, and changelog-style sentences
  ("now", "no longer", "was").
- Keep every rule, default, refusal, open question, and re-find trigger
  that still holds. A "not built" paragraph is where a settled choice
  lives; read the whole entry before calling it history.
- Check every claim against the fact sheet.
- Plain dry English: short sentences, common words, no metaphors.

Keep section numbers and citation tokens. Other documents, docstrings, and
the decision log cite both, and no gate checks section numbers. A section
with nothing left keeps its number and one line saying where the rule went.
A new citation namespace needs its row in `docs/citation-index.md` first.

## Step 3: reconciliation

When the specification and the code disagree, the decision log decides.

- A later entry rules for the code: update the specification and cite the
  entry.
- No entry rules: the code is wrong. Change neither the specification nor
  the code. List the finding in the PR body with file and line, as a fact.
- A frozen contract (`period-model.md`, `concurrency-model.md`,
  `control-protocol.md`, `supervisor-protocol.md`, `protocol-evolution.md`,
  `access-model.md`, and the U3a record schema in `uc-edge-schema.md`)
  changes meaning only
  through a new decision-log entry. So does any rule an entry doc-froze,
  such as SEM-36 to SEM-39.

Code fixes are separate slices with their own review.

## Step 4: verification

Run the full local gates listed under "Verify a change" in
`docs/agent-workflow.md`, starting with `scripts/arch_check.py`: it fails
on a `test_...` name in backticks that no test defines, which is the
gate a rewritten document is most likely to trip. Report each command with
its exit status.

Then a fresh adversarial review of the new text against the code, by an
Opus reviewer that writes its findings to a scratch file and reports a
ranked table. It hunts, in this order: false claims against the code,
present facts dropped, history that survived, broken links or section
references. A frozen contract also gets a Codex read-only review. Rule on
every finding before the commit.

## Step 5: delivery and stamp

One document per PR, on a branch named `spec/<stem>`. The PR body carries
the reconciliation findings. Ask before merging. A pass that changes
nothing has no PR: report the fact sheet's result and the reconciliation
findings, and stamp once the boss accepts.

After the boss accepts the pass, and the PR if there is one is merged,
stamp `main`. Never stamp a branch commit: the gate measures drift from
the stamp, and a branch commit is not on main's line. First bring main up
to date and check for an existing stamp:

```sh
git switch main && git fetch origin && git rebase origin/main
git status --short                                   # must be empty
git tag --points-at HEAD --list 'spec-review/<stem>/*'
```

Reuse a stamp that already points at HEAD. Otherwise create an annotated
tag whose message states the pass's outcome, one of `no change`,
`wording`, or `ruling DL-NNN`, and push that one tag:

```sh
ts=$(date -u +%Y-%m-%dT%H%M%SZ)
git tag -a "spec-review/<stem>/$ts" -m "spec review: docs/<stem>.md; outcome: wording"
git push origin "spec-review/<stem>/$ts"
```

Never move an existing tag. If the name exists, choose a later timestamp.
Invoking this skill does not by itself authorize the push: it happens under
the repository's push rules, after the boss's acceptance.

A disagreement ruled during the pass gets its own decision-log entry; a
pass that changed wording only gets none. Re-find trigger for the shared
`src/` line count: a document whose last three stamps all say
`outcome: no change` (`git tag -l 'spec-review/<stem>/*' -n1`) is being
re-reviewed for changes that do not concern it; a per-document module map
is then worth building.
