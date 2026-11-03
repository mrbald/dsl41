---
name: reader-docs
description: Write, split or restructure human-facing documentation for its readers under docs/writing.md. Use for README sections, unfrozen docs pages, block cards, example READMEs and runbooks. Takes the document paths and intended readers. Review and merge authority follow the project workflow.
---

# Reader-facing documents (DL-405, DL-448)

Help the reader complete their task. Use [docs/writing.md](../../../docs/writing.md)
for content and presentation, and scale this procedure to the change.
One document, one primary reader and one PR SHOULD be the default. Related
pages and behavior-coupled corrections can belong in the same change.

SHOULD means a recommendation: the responsible role may depart after considering
its purpose and the consequences. Record a material departure and its reason
briefly in the existing task or PR record. No separate waiver or owner escalation
is needed solely for procedural form. The sequence, separate artifacts, delegated
fact gathering, one-page drafting runs and branch naming below are defaults.
A small correction may use a short combined note and direct source inspection.

This discretion does not relax binding contracts, truthful source evidence,
public hygiene, citation and anchor integrity, generated state-machine views,
required independent review, gates or commit/merge authority. For a frozen
contract, use `spec-review`; this skill cannot change its meaning. Where both
procedures apply, `spec-review` governs section numbers, citation tokens and
history. Role bindings and dispatch remain with the operator.

## Audience and facts

The writer SHOULD start with a short audience brief: reader, task, what done
looks like, assumed knowledge, reading context, sources and scope. Name an
existing page's pattern when it helps. The brief can be in the task or PR body;
use the reader and task already supplied instead of restating them for form.

Before asserting a code fact, inspect its source. A separate read-only fact
sheet SHOULD precede a substantial draft; delegate it when an independent
inventory helps. Record relevant commands, options, behavior, limits and
existing pages to link. For a small edit, source references beside the proposed
change can be enough. Never substitute memory or invented evidence for inspection.

When moving sections, splitting a page or changing anchors, find and update its
citers. Search `src/`, `tests/`, `scripts/`, `docs/` and `examples/` for the page
stem and section references. Preserve existing anchors when possible. A reorder
SHOULD be a separate slice unless coupling it keeps the reader's task coherent.

## Draft and reconcile

Write for the named reader from the verified facts. Use the checklist in
writing.md: relevant content, findable steps, clear language and usable commands.
Put warnings before the affected action, expose limits, and link the governing
contract rather than copying its rule tables. Keep state-machine rows generated.

The engineer may draft reader documentation; one page per run SHOULD be the
usual scope. The boss rules on voice and the reviewer checks the diff. Separate
reader sections can serve a mixed audience when they make the task easier.

If code and documented rules disagree, trace the applicable decision and report
the discrepancy with evidence. Follow `spec-review` reconciliation where it
applies. Do not silently resolve a semantic disagreement by rewriting the page.

## Verify and review

Follow [agent-workflow.md](../../../docs/agent-workflow.md#verify-a-change)
for applicable checks and repeat checks invalidated by the edit. The operator
makes new documents visible to Git before rendering the decision index; its
renderer selects tracked paths. Run document dependency checks after rewrites
and regenerate affected outputs. Report commands and exit status.

The reviewer checks false or unsupported claims, missing facts needed for the
task, conflicts with contracts, then usability and links. Read the changed page
as its intended reader and say whether the task can be completed. Review depth
should match the change; a small correction need not reconstruct an unchanged
page's entire fact sheet.

Instruction-policy changes and restatements of frozen-contract use are semantic
class under [review.md](../../../docs/agents/review.md#required-passes).
Its required passes, finding dispositions and round limits remain binding.
This skill grants no exemption from independent review or boss voice acceptance.

## Deliver

A standalone documentation change SHOULD use `docs/<stem>` and one document per
PR. The PR SHOULD carry or link the audience, source/reconciliation notes and
review result in proportion to the change. Keep related README navigation and
behavior-coupled documentation consistent in the same PR when useful.

Merge through [coordination](../../../docs/agents/coordination.md#git-workflow).
This skill grants no publication authority and adds no stamp: specification
stamps belong to `spec-review`; other reader pages use their PR review.
After delivery, treat reader confusion within the page's scope as evidence to
improve it. Make the correction in an appropriate later change.
