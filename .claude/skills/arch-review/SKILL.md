---
name: arch-review
description: Review dsl41 for unnecessary conceptual complexity — duplicated concepts, parallel models, pass-through layers, one-implementation abstractions, flag matrices that should be enums, vocabulary re-encoded per layer. Use when the user asks for an architecture review, or when scripts/arch_check.py printed "architecture review due".
---

# Architecture review (DL-75)

Run the gate first and read its output:

```sh
python scripts/arch_check.py
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

Stamp the review so the gate measures accumulated drift from here, not from
the beginning of time:

```sh
git tag arch-review/$(date +%Y-%m-%d)
```
