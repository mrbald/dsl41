# JIL Statement-Level Syntax (hand-scanner spec)

Why not lark: JIL statements are line-oriented, with raw-to-EOL values, escaped colons, and
attribute-specific multi-line continuation. A hand scanner does this context-sensitive lexing
in ~100 lines. A CFG does it badly. The compiler uses lark only for condition expressions
(`grammars/condition.lark`). This document is the normative spec for the scanner. Each rule
here gets a fidelity test (AST contract, `ir-design.md` §2).

## Tokenization rules

1. **Attribute line** = `key ':' value`, where `key` matches `/[A-Za-z_][A-Za-z0-9_]*/` at
   line start (after optional whitespace). JIL "parses on the combination of keyword followed
   by a colon" (Broadcom, condition-attribute page). As a result:
2. **Escaped colon** `\:` inside a value is literal and does NOT start a new key. The scanner
   splits on the FIRST unescaped colon of a line whose prefix is a valid key shape.
   *(Amended 2026-07-10, DL-39: the escape is SURFACE syntax on the job-name lane. Lowering
   funnels insert_job subjects, box_name values, and condition job references through the one
   `conditions.unescape_job_name`. This matches the rule 7 principle: "semantic unquoting
   happens at lowering". As a result, both estate spellings converge on the semantic catalog
   key, and each JIL-emitting path re-escapes. Other value lanes stay verbatim in IR. [?] It
   is not known whether the engine unescapes `\:` inside general values (command,
   std_*_file). Value lanes stay escaped until a live instance gives the answer.)*
3. **Statement boundary**: a line whose key is a subcommand (`insert_job`, `update_job`,
   `delete_job`, `rename_job`, `delete_box`, `insert_machine`, `update_machine`,
   `delete_machine`, `insert_global`, `delete_global`, `override_job`, `insert_xinst`,
   `update_xinst`, `delete_xinst`, `insert_blob`, `delete_blob`, `insert_glob`,
   `delete_glob`, `insert_resource`, `update_resource`, `delete_resource`,
   `insert_monbro`, `update_monbro`, `delete_monbro`, `insert_job_type`,
   `update_job_type`, `delete_job_type`, `insert_connectionprofile`,
   `update_connectionprofile`, `delete_connectionprofile` — the complete TechDocs 12.1
   inventory, DL-29 — plus the autocal_asc calendar-export statements `calendar`, `cycle`,
   `extended_calendar`, rule 11 / DL-36) starts a new statement. All attribute lines that
   follow belong to this statement until the next subcommand or EOF. Unknown keys are
   attributes, never boundaries (forward compatibility). There is one exception: a key that
   matches the subcommand shape `/(insert|update|delete|override|rename)_\w+/i` but is not
   in the recognized set is a scanner error. If the scanner folds a missed statement
   boundary into the previous statement, the result is silent *structural* loss. This loss
   is strictly worse than a loud stop. No documented JIL *attribute* has this shape, so the
   guard costs nothing on valid input.
   *(Amended 2026-07-09 / DL-18: estate-shaped input used `insert_resource`, and
   the scanner silently folded the new statement into the `insert_machine` before it. This
   is the exact failure class that this rule now makes impossible. The same amendment added
   the resource subcommands to the recognized set.)*
   *(Amended 2026-07-10 / DL-27: the 12.x doc sweep found `rename_job`, a documented
   subcommand. Its `rename_` verb was outside the guard shape, so it folded silently. This
   is the same DL-18 failure class that the guard exists to stop. This amendment added
   `rename_job` to the recognized set and `rename` to the guard verbs. The verb list of
   the guard is part of the subcommand inventory. When the recognized set changes, make
   sure that the verb list matches the vendor subcommand page.)*
4. **One-line form**: `insert_job: name   job_type: c` — a subcommand line can carry a second
   `key: value` pair after the subject. The scanner detects a second unescaped
   ` key:`-shaped token on the subcommand line only. (This form is common in estate JIL
   and autorep -q output.) The scanner recognizes only `job_type` as the inline key, because
   autorep emits only that pair. Any other second `key:`-shaped token on a subcommand line
   is a scanner error. The error is loud, and the scanner never silently folds the token
   into the subject. *(Amended 2026-07-03: this amendment narrowed the generic wording to
   match the `job_type_inline` field of the AST model.)*
4b. **Attribute lines carry ONE pair** *(added 2026-07-10, DL-30)*: the Broadcom syntax
   rules permit several `attribute: value` statements on one line (whitespace-separated)
   and require escapes (`\:`) or quotes for colons *inside* values. As a result, a second
   unescaped, unquoted, whitespace-preceded `key:`-shaped token in an attribute value is a
   real second attribute or invalid JIL. (If the scanner folds a real second attribute into
   the value, that is silent loss that the DL-07 firewall cannot see.) Both cases get a
   loud scanner error, from the same detector as rule 4. Colons not in that shape (no
   leading whitespace, escaped, quoted, digit-led as in `/tmp/out:file.err` or
   `02:00-04:00`) remain value text per rule 2/F4.
5. **Comments**: JIL has `/* ... */` comments (they can span lines) and full-line `#`
   comments. A comment attaches to the nearest statement/attr that follows (leading) or to
   the same line (trailing — block comments only). Free comments at EOF are `floating`. The
   scanner preserves the verbatim text. Disambiguation from values (pinned by F4 fixtures,
   amended 2026-07-03): a trailing block comment starts at the leftmost whitespace-preceded,
   unquoted `/*` whose next `*/` ends the line. A `/*` that never closes on the line (for
   example, a shell glob after a space) is value text. A closed `/*...*/` with value text
   after it stays in the value as opaque text. A full-line block comment must close at the
   end of its last line. Non-whitespace content after `*/` is a scanner error.
   *(Amended 2026-07-10, DL-31: `#` starts a comment only as the first non-whitespace
   character of the line. The Broadcom syntax rules put `#` comments "in the first
   column" and list `#` among valid name/value characters. As a result, a mid-line
   whitespace-preceded `#`-tail is VALUE text. The previous trailing-strip silently
   changed the value relative to the parse of the engine. The scanner accepts leading
   whitespace before a full-line `#` as harmless leniency. [?] Two open questions need a
   live `jil` binary: does it accept indented `#` comments, and how does it treat
   mid-line `#`?)*
6. **Continuation**: some list-valued attributes (`start_mins`, `start_times`,
   `must_*_times`, calendars) "can contain up to 255 characters and multiple lines without a
   continuation character" (Broadcom, start_mins page). Scanner rule: a line that does NOT
   match the `key:` shape and follows a known list-valued attribute is a continuation of the
   value of that attribute. [?] Make sure that the exact continuation trigger set matches
   real `autorep -q` output. Then encode the findings as synthetic corpus fixtures.
7. **Quoted values**: the scanner preserves `"..."` verbatim, and this includes the internal
   spaces/colons. The quotes are part of raw_value at the AST level (semantic unquoting
   happens at lowering).
8. **Case**: the scanner recognizes keys case-insensitively but stores them as they are
   written. Job names are stored as they are written and are compared case-sensitively
   (ir-design §6, with the `--case-fold` escape hatch).
9. **Blank lines** delimit nothing (they stay as layout trivia in preserve-mode rendering).
10. **Line endings** (amended 2026-07-03): each file has one style — `\n` or `\r\n`, the
    `JilFile.newline_style` model field. Mixed line endings are a scanner error. A missing
    final newline is layout trivia and survives round-trip.
11. **Calendar exports** *(added 2026-07-10, DL-36)*: the `autocal_asc -E`/`-I` export
    statements `calendar`, `cycle`, and `extended_calendar` (TechDocs 12.1 "autocal_asc
    Command — Manage Calendars") are recognized statement boundaries. They are NOT `jil`
    subcommands. The vendor processes them with a different binary. But migration estates
    ship calendar exports together with JIL, and the format is JIL-shaped with one
    exception. A standard `calendar:` body carries bare date rows (`MM/DD/YYYY [HH:MM]`,
    format varies with `-f date_format`). Scanner rules: a non-key-shaped line inside a
    `calendar:` statement is a **date row** (this check comes after rule 6, and rule 6
    never applies there). The scanner carries a date row verbatim
    (`JilStatement.date_lines`, no comment extraction). The date rows are contiguous with
    their statement. A blank line or a comment between date rows ends the date body. An
    attribute line after a date row is a scanner error. The export format puts all
    attributes before the date list, and a re-render of an interleaved shape silently
    reorders it. No documented JIL attribute is named `calendar`, `cycle`, or
    `extended_calendar`, so the recognition of these boundaries costs nothing on valid JIL
    (the DL-18 argument). These verbs deliberately stay OUT of the rule-3 guard-verb
    inventory note. The guard covers `(insert|update|delete|override|rename)_*` shapes
    only, and calendar exports have no update/delete verbs (re-import with `-F`
    overwrites).

## Corpus policy

`tests/corpus/` contains **synthetic JIL only**. Each fixture is hand-written from Broadcom
documentation examples, or is generated. Production JIL from any employer estate must never
enter this repository (see LICENSING.md / CONTRIBUTING note). When a fixture exercises a
specific dossier entry, its name is `sem_<entry>_<slug>.jil`.

## Fidelity tests (normative)

- F1 preserve-mode identity: `render(parse(text)) == text` for every corpus file.
- F2 canonical fixpoint: `c = render_canonical(parse(text))`. Then
  `render_canonical(parse(c)) == c`.
- F3 fuzz: hypothesis-generated attr soups. Where parse succeeds, F1 holds.
- F4 escaped-colon torture: values that contain `\:`, `:` inside quotes, and
  `key:`-lookalikes.
