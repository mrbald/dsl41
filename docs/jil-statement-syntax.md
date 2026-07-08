# JIL Statement-Level Syntax (hand-scanner spec)

Why not lark: JIL statements are line-oriented with raw-to-EOL values, escaped colons, and
attribute-specific multi-line continuation — context-sensitive lexing that a hand scanner does
in ~100 lines and a CFG does badly. Lark is used only for condition expressions
(`grammars/condition.lark`). This document is the scanner's normative spec; every rule here
gets a fidelity test (AST contract, `ir-design.md` §2).

## Tokenization rules

1. **Attribute line** = `key ':' value`, where `key` matches `/[A-Za-z_][A-Za-z0-9_]*/` at
   line start (after optional whitespace). JIL "parses on the combination of keyword followed
   by a colon" (Broadcom, condition-attribute page) — therefore:
2. **Escaped colon** `\:` inside a value is literal and does NOT start a new key. The scanner
   splits on the FIRST unescaped colon of a line whose prefix is a valid key shape.
3. **Statement boundary**: a line whose key is a subcommand (`insert_job`, `update_job`,
   `delete_job`, `delete_box`, `insert_machine`, `update_machine`, `delete_machine`,
   `insert_global`, `delete_global`, `override_job`, `insert_xinst`, `update_xinst`,
   `delete_xinst`, `insert_blob`, …) begins a new statement; all following attribute lines
   belong to it until the next subcommand or EOF. Unknown keys are attributes, never
   boundaries (forward compatibility).
4. **One-line form**: `insert_job: name   job_type: c` — a subcommand line may carry a second
   `key: value` pair after the subject; the scanner detects a second unescaped
   ` key:`-shaped token on the subcommand line only. (Common in real estates and autorep -q
   output.) Only `job_type` is recognized as the inline key — the only pair autorep emits;
   any other second `key:`-shaped token on a subcommand line is a scanner error — loud,
   never silently folded into the subject. *(Amended 2026-07-03: generic wording narrowed
   to match the AST model's `job_type_inline` field.)*
5. **Comments**: `/* ... */` (may span lines) and `#`-to-EOL outside quoted strings. Comments
   attach to the nearest following statement/attr (leading) or same line (trailing); free
   comments at EOF are `floating`. Verbatim text preserved. Disambiguation vs. values
   (pinned by F4 fixtures, amended 2026-07-03): a trailing block comment starts at the
   leftmost whitespace-preceded, unquoted `/*` whose first following `*/` ends the line;
   a `/*` that never closes on the line (e.g. a shell glob after a space) is value text,
   and a closed `/*...*/` with value text after it stays in the value as opaque text.
   A full-line block comment must close at the end of its last line; non-whitespace
   content after `*/` is a scanner error.
6. **Continuation**: some list-valued attributes (`start_mins`, `start_times`,
   `must_*_times`, calendars) "can contain up to 255 characters and multiple lines without a
   continuation character" (Broadcom, start_mins page). Scanner rule: a line that does NOT
   match the `key:` shape and follows a known list-valued attribute is a continuation of that
   attribute's value. [?] Verify the exact continuation trigger set against `autorep -q`
   output from the real estate — encode findings as corpus fixtures.
7. **Quoted values**: `"..."` preserved verbatim including internal spaces/colons; quotes are
   part of raw_value at AST level (semantic unquoting happens at lowering).
8. **Case**: keys are case-insensitively recognized but stored as written; job names stored
   as written, compared case-sensitively (ir-design §6, with `--case-fold` escape hatch).
9. **Blank lines** delimit nothing (preserved as layout trivia in preserve-mode rendering).
10. **Line endings** (amended 2026-07-03): one style per file — `\n` or `\r\n`, the
    `JilFile.newline_style` model field — mixed line endings are a scanner error. A missing
    final newline is layout trivia and survives round-trip.

## Corpus policy

`tests/corpus/` contains **synthetic JIL only** — hand-written from Broadcom documentation
examples or generated. Production JIL from any employer estate must never enter this
repository (see LICENSING.md / CONTRIBUTING note). Fixtures are named
`sem_<entry>_<slug>.jil` when they exercise a specific dossier entry.

## Fidelity tests (normative)

- F1 preserve-mode identity: `render(parse(text)) == text` for every corpus file.
- F2 canonical fixpoint: `c = render_canonical(parse(text))`; `render_canonical(parse(c)) == c`.
- F3 fuzz: hypothesis-generated attr soups; wherever parse succeeds, F1 holds.
- F4 escaped-colon torture: values containing `\:`, `:` inside quotes, `key:`-lookalikes.
