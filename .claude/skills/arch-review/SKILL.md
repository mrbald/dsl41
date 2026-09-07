---
name: arch-review
description: Review dsl41 for unnecessary conceptual complexity when the user requests an architecture review or scripts/arch_check.py reports one due. Covers concepts and ownership beyond the mechanical gate. Does not stamp or publish a review until the boss accepts it.
---

# Architecture review (DL-75)

Run the gate first and read its output:

```sh
uv run python scripts/arch_check.py
```

It answers the mechanical questions (duplicate bodies, new private
cross-module imports, unresolvable citations, an IR-F shape change without an
IR_VERSION bump, sizes that grew past the baseline). Do not repeat that work.
Your subject is the half a script cannot see: concepts that exist and do not
need to.

## What to look for

Complexity that is *inherent* to AutoSys→UC migration is not a finding —
this domain is genuinely irregular, and the citation density is the project's
core discipline. Look only for complexity the code *added*:

- **Duplicated concepts.** The same idea implemented twice under two names,
  or two modules that each know the same rule.
- **Parallel models.** Two types holding the same facts, kept in sync by
  hand — the class DL-73 removed when `DerivedEdge.atom` replaced re-scanning.
- **Pass-through layers.** A module, function, or facade whose whole body is
  forwarding. A re-export layer is a pass-through layer.
- **Abstractions with one implementation.** A protocol, base class, or
  strategy table with exactly one member and no second member in sight.
- **Flag matrices encoding an enum.** N booleans for N+1 exclusive modes,
  plus a precedence rule to settle the impossible combinations (the surface
  DL-75 collapsed into `--format`).
- **Vocabulary re-encoded per layer.** The same status/class/kind spelled one
  way in IR-F, another in IR-G, another in the emitter, with translation
  tables between.
- **Data copied across a layer boundary** where a reference or a lookup would
  do, so the copy can go stale.

## How to report

Rank by **(cognitive load removed) / (cost to change)** — the cheap deletions
that shrink what a reader must hold in their head come first; a deep rework
that saves one concept comes last or not at all. Anchor every finding to
`file:line`. State what the reader must currently know, and what they would
have to know instead.

**Always include a "load-bearing — leave alone" section.** Name the things
that look complex and are not: the irregular parts that are irregular because
AutoSys is, the citation comments, the tested-and-frozen contracts. A review
that only lists problems reads as "everything here is too complex" and gets
ignored, which is the same as not reviewing.

Findings that get acted on become a `docs/decision-log.md` entry. Findings
that get declined become one too — a declined finding will otherwise be
re-found every review.

## Close it out

The review itself is read-only. Give its findings to the boss before recording
completion. A partial review or a review of only a proposal does not reset the
architecture baseline.

After the boss accepts a complete review, stamp the reviewed commit so the gate
measures drift from it. First check for an existing stamp:

```sh
git tag --points-at HEAD --list 'arch-review/*'
```

Reuse a stamp that covers this review. Otherwise, with a clean worktree at the
reviewed commit, create an annotated tag. The timestamp allows multiple reviews
on the same day and gives the gate a creation date for sorting:

```sh
git tag -a "arch-review/$(date -u +%Y-%m-%dT%H%M%SZ)" -m 'Architecture review accepted'
```

Never move an existing tag. If the name already exists, choose a later timestamp.
Publish only the accepted stamp with its reviewed commit, under the repository's
push rules. Invoking this skill alone does not authorize publishing either.
