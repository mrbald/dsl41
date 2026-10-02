# JIL Statement-Level Syntax (hand-scanner spec)

Why not lark: JIL statements are line-oriented, with raw-to-EOL values, escaped colons, and
attribute-specific multi-line continuation. A hand scanner does this context-sensitive lexing in
a few hundred lines. A CFG does it badly. The compiler uses lark only for condition expressions
(`grammars/condition.lark`). This document is the normative spec for the scanner,
`src/dsl41/ast_jil.py`. Each rule here gets a fidelity test (AST contract, `ir-design.md` §2).
A `[?]` marks a corner that needs a live instance to settle; the scanner runs a documented
default there.

## Tokenization rules

0. **Input contract**: the file is UTF-8. Invalid UTF-8 fails at decode. The scanner takes
   Unicode scalar strings only. An unpaired surrogate is a scanner error that names its line:
   a span is a UTF-8 byte offset, and a surrogate has no UTF-8 spelling (period-model PR-10a).
   "Whitespace" below means space or tab. The blank-line and after-comment tests are the two
   exceptions: they accept every character Python `str.strip()` removes.
1. **Attribute line** = `key ':' value`, where `key` matches `/[A-Za-z_][A-Za-z0-9_]*/` at line
   start (after optional whitespace). The colon comes directly after the key. A space between
   them (`command : x`) leaves the line with no key: rule 6 can then take it as a continuation,
   rule 11 as a date row, and otherwise it is a scanner error. JIL "parses on the combination of
   keyword followed by a colon" (Broadcom, condition-attribute page). As a result:
2. **Escaped colon** `\:` inside a value is literal and does NOT start a new key. The scanner
   splits on the FIRST unescaped colon of a line whose prefix is a valid key shape. The escape
   is SURFACE syntax on the job-name lane (DL-39): lowering funnels insert_job subjects,
   box_name values, and condition job references through the one
   `conditions.unescape_job_name`, per the rule 7 principle that semantic unquoting happens at
   lowering. Both estate spellings therefore converge on the semantic catalog key, and each
   JIL-emitting path re-escapes. Other value lanes stay verbatim in IR. [?] Whether the engine
   unescapes `\:` inside general values (command, std_*_file) is not known; value lanes stay
   escaped until a live instance gives the answer.
3. **Statement boundary**: a line whose key is a subcommand starts a new statement. The
   recognized set tracks the TechDocs 12.1 JIL subcommand pages (DL-29), each added as it was
   found: `insert_job`, `update_job`, `delete_job`, `rename_job`, `delete_box`,
   `insert_machine`, `update_machine`, `delete_machine`, `insert_global`, `delete_global`,
   `override_job`, `insert_xinst`, `update_xinst`, `delete_xinst`, `insert_blob`,
   `update_blob`, `delete_blob`, `insert_glob`, `update_glob`, `delete_glob`,
   `insert_resource`, `update_resource`, `delete_resource`, `insert_monbro`, `update_monbro`,
   `delete_monbro`, `insert_job_type`, `update_job_type`, `delete_job_type`,
   `insert_connectionprofile`, `update_connectionprofile`, `delete_connectionprofile`; plus
   the autocal_asc calendar-export statements `calendar`, `cycle`, `extended_calendar` (rule
   11, DL-36) and the accepted `ext_calendar` spelling (DL-57). This set was once claimed
   complete against TechDocs 12.1 (DL-29); `update_blob` and `update_glob` were missing from
   it (DL-245), so the claim is retracted -- treat the list above as the verbs found so
   far, not a proof of completeness. All attribute lines that follow belong to this statement
   until the next subcommand or EOF. An attribute line before the first statement is a
   scanner error. Unknown keys are attributes, never boundaries (forward compatibility).
   There is one exception: a key that matches the subcommand shape
   `/(insert|update|delete|override|rename)_\w+/i` but is not in the recognized set is a
   scanner error (DL-18, DL-27). A missed statement boundary folded into the previous
   statement is silent *structural* loss, strictly worse than a loud stop. No documented JIL
   *attribute* has this shape, so the guard costs nothing on valid input. The verb list of
   the guard is part of the subcommand inventory: when the recognized set changes, make sure
   that the verb list matches the vendor subcommand page.
4. **One-line form**: `insert_job: name   job_type: c` — a subcommand line can carry a second
   `key: value` pair after the subject. The scanner detects a second unescaped ` key:`-shaped
   token on the subcommand line. Rule 4 covers every boundary line of rule 3, the calendar-export
   verbs of rule 11 included. Rule 4b runs the same detector on attribute lines, where every hit
   is an error. (This form is common in estate JIL and autorep -q output.) The scanner
   recognizes only `job_type` as the inline key, stored in the `job_type_inline` field of the
   AST model, because autorep emits only that pair. Any other second `key:`-shaped token on a
   subcommand line is a scanner error, and so is a third pair after `job_type`. The error is
   loud, and the scanner never silently folds the token into the subject.
   The detector reads past closed block comments, on subcommand and attribute lines alike
   (DL-151). A `key:`-shaped token inside a closed `/*...*/` span is comment prose, not a
   pair; rule 5 keeps that span as opaque value text. The span opens exactly where rule 5
   opens a comment: the `/*` is unquoted and sits at the value start or after whitespace. A
   marker glued to the text before it opens nothing, so a pair inside it is a real pair and
   gets the loud error, and a quote inside an opened span shadows nothing (rule 7). Skipping a
   span invents no whitespace boundary either: a token glued to the closing `*/` is still not
   whitespace-preceded, and stays value text, while a whitespace-preceded pair after the `*/`
   is a real pair. [?] Whether the vendor binary strips a comment BEFORE it splits pairs is
   not known, and it decides one input: `command: a /* c */b: x` is one value here and would
   be two attributes to a stripping engine. The scanner follows rule 4b's own wording, which
   reads the source line.
4b. **Attribute lines carry ONE pair** (DL-30): the Broadcom syntax rules permit several
   `attribute: value` statements on one line (whitespace-separated) and require escapes (`\:`)
   or quotes for colons *inside* values. As a result, a second unescaped, unquoted,
   whitespace-preceded `key:`-shaped token in an attribute value is a real second attribute or
   invalid JIL. (If the scanner folds a real second attribute into the value, that is silent
   loss that the DL-07 firewall cannot see.) Both cases get a loud scanner error, from the same
   detector as rule 4. Colons not in that shape (no leading whitespace, escaped, quoted,
   digit-led as in `/tmp/out:file.err` or `02:00-04:00`) remain value text per rule 2/F4. So
   does a pair shape that sits inside a closed block comment (rule 4).
   The detector covers the JOINED value (DL-160): each rule-6 continuation line is scanned
   with the quote parity seeded from the value accumulated so far, so a quote opened on the
   attribute line and closed on a continuation line is one quoted span, not a bare pair.
   Rule 11 date rows stay exempt, because rule 11's own "the scanner does not validate the row
   shape" sentence governs there.
5. **Comments**: JIL has `/* ... */` comments (they can span lines) and full-line `#`
   comments. A comment attaches to the nearest statement/attr that follows (leading) or to
   the same line (trailing; block comments only). Free comments at EOF are `floating`. The
   scanner preserves the text. `Comment.text` holds the marker and its body, with `\n` between
   lines. The indent, the blank lines before the comment, and the run after the closing `*/` ride
   in separate layout fields. That is what keeps preserve-mode rendering byte-exact.
   Disambiguation from values (pinned by F4 fixtures; DL-161): a trailing block comment starts
   at the leftmost unquoted `/*` that is at the value start or preceded by whitespace. If its
   first `*/` ends the line, the comment is closed on the line. If NO `*/` follows on the line,
   the marker OPENS a block comment that spans lines. The scanner consumes the body lines
   atomically with the opening line, so no body line ever reaches the other line rules. A
   comment still open at EOF is a loud `unterminated block comment` error that names the
   opener line. This follows the `/*`-comment formats (C, C++, Java, JavaScript, Go, Rust,
   SQL, CSS, HCL, PHP, proto): all of them open a comment closure-independently outside string
   literals; none decides by whether `*/` follows. CSS is in that list for the opening rule
   only: its tokenizer consumes to EOF and records a parse error, so it recovers rather than
   refuses, and does not support the loud-EOF half. The whitespace-preceded predicate is
   dsl41's own boundary for glued glob values (`/tmp/*` opens nothing). It is NOT part of that
   majority; C-family lexers open without it. Formats that protect unquoted globs do so by not
   having block comments at all. The opener also runs at the value START,
   so a bare root glob such as `command: /*.sh` has no bare spelling; it must be quoted.
   Quoting is the only complete escape: a quoted `/*` opens nothing (rule 7), so a
   whitespace-preceded or value-start glob-shaped value must be quoted.
   Known hazard, stated plainly: a stray later `*/` silently captures the lines up to it.
   An unterminated comment is loud; a wrongly terminated one is not. A closed `/*...*/`
   with value text after it stays in the value as opaque text. A full-line block comment
   must close at the end of its last line; the same close rule governs a multi-line
   trailing comment. Non-whitespace content after `*/` on the closing line is a scanner
   error. [?] The live `jil` binary has not adjudicated the multi-line trailing form; the
   runbook carries the probe (the DL-59 pattern: a documented deterministic default, not a
   guess-resolution).
   `#` starts a comment only as the first non-whitespace character of the line (DL-31). The
   Broadcom syntax rules put `#` comments "in the first column" and list `#` among valid
   name/value characters, so a mid-line whitespace-preceded `#`-tail is VALUE text. The
   scanner accepts leading whitespace before a full-line `#` as harmless leniency. [?] Two
   open questions need a live `jil` binary: does it accept indented `#` comments, and how
   does it treat mid-line `#`?
6. **Continuation**: some list-valued attributes (`start_mins`, `start_times`, `must_*_times`,
   calendars) "can contain up to 255 characters and multiple lines without a continuation
   character" (Broadcom, start_mins page). Scanner rule: a line that does NOT match the `key:`
   shape and directly follows a known list-valued attribute is a continuation of the value of that
   attribute. The trigger set is `start_times`, `start_mins`, `must_start_times`,
   `must_complete_times`, `run_calendar`, `exclude_calendar`. A blank line or a comment line
   closes the open continuation. The continuation line goes into `raw_value` verbatim, unless
   it opens a block comment (below); no closed comment is extracted from it, the same carry
   rule 11 uses for a date row. A non-key-shaped line with no
   open continuation is a scanner error, unless rule 11 makes it a date row. [?] Make sure that
   the exact continuation trigger set matches real `autorep -q` output. Then encode the findings
   as synthetic corpus fixtures.
   A continuation line CAN open a block comment (DL-161). The rule-5 opener runs on it with
   the quote state seeded from the joined value (the DL-160 walk), so a marker inside a quote
   opened on an earlier line stays value text. Only the pre-opener prefix is value, and the
   rule-4b detector reads it alone; the body lines are consumed with the comment. The comment
   closes the open continuation (this rule's own comment-closes sentence: the body lines are
   comment lines), so the line after the closer is a scanner error, not a resumed
   continuation. A trailing comment that CLOSES on its own line is different in both places:
   on the attribute line it leaves the continuation open, and on a continuation line it stays
   in the verbatim carry, with no comment extracted there.
7. **Quoted values**: the scanner preserves `"..."` verbatim, and this includes the internal
   spaces/colons. The quotes are part of raw_value at the AST level (semantic unquoting
   happens at lowering). Quote handling is lexical: every `"` toggles the shadow, a backslash
   does not escape it at this layer, and an unmatched quote shadows the markers of rules 4, 4b
   and 5 to the end of the line.
8. **Case**: the scanner recognizes keys case-insensitively but stores them as they are
   written. Job names are stored as they are written and are compared case-sensitively
   (ir-design §6, with the `--case-fold` escape hatch).
9. **Blank lines** delimit no statement: one inside a statement does not end it, and it stays
   as layout trivia in preserve-mode rendering. A blank line does close an open continuation
   (rule 6) and an open date body (rule 11). A line counts as blank when it holds whitespace
   only.
10. **Line endings**: each file has one style, `\n` or `\r\n`, the `JilFile.newline_style`
    model field. Mixed line endings are a scanner error, and so is a bare-CR ending. A missing
    final newline is layout trivia and survives round-trip.
11. **Calendar exports** (DL-36): the `autocal_asc -E`/`-I` export statements `calendar`,
    `cycle`, and `extended_calendar` (TechDocs 12.1 "autocal_asc Command — Manage Calendars")
    are recognized statement boundaries. `ext_calendar` is the Manage Calendars spelling of the
    same record and is accepted too (DL-57); rendering returns the spelling that appeared
    (SEM-36, DL-60). They are NOT `jil` subcommands. The vendor processes them with a
    different binary. But migration estates ship calendar exports together with JIL, and the
    format is JIL-shaped with one exception. A standard `calendar:` body carries bare date rows
    (`MM/DD/YYYY [HH:MM[:SS]]`; the format varies with `-f date_format`, and an observed export
    writes the `HH:MM:SS` tail, DL-60). The scanner does not validate the row shape. Scanner
    rules: a non-key-shaped line inside a `calendar:` statement is a **date row** (the check
    comes after rule 6, and no export attribute is in the rule-6 trigger set, so rule 6 does not
    fire there). The scanner carries a date row verbatim (`JilStatement.date_lines`, no comment
    extraction). The date rows are contiguous with their statement. A blank line or a comment
    between date rows ends the date body, and a date row after that point is a scanner error. An
    attribute line after a date row is a scanner error. The export format puts all attributes
    before the date list, and a re-render of an interleaved shape silently reorders it. No
    documented JIL attribute is named `calendar`, `cycle`, `extended_calendar`, or
    `ext_calendar`, so the recognition of these boundaries costs nothing on valid JIL (the DL-18
    argument). These verbs deliberately stay OUT of the rule-3 guard-verb inventory. The guard
    covers `(insert|update|delete|override|rename)_*` shapes only, and calendar exports have no
    update/delete verbs (re-import with `-F` overwrites).
    A date row is NOT value position (DL-161): the rule-5 opener does not run on it, and the
    row stays verbatim, the same exemption rule 4b's date-row carve-out uses (DL-160). A
    multi-line trailing comment on the `calendar:` line or on one of its attributes does not
    break date-body contiguity: the atomic scan consumes the body lines with their statement
    line, so they are trailing trivia, not comment lines between rows.
12. **Literal blob region** (DL-245): the vendor's "JIL Syntax Rules" page, its own rule 8
    (a different document from this one's numbering -- this scanner rule is 12, not 8), reads:
    "You can use the blob_input attribute to enter multiline text manually... The blob_input
    attribute has the following form: `blob_input:<auto_blobt> this is a multi-line
    text</auto_blobt>`. Use the auto_blobt meta-tags to indicate the beginning and end of
    multiline text. JIL interprets every character input between the auto_blobt meta-tags
    literally. This behavior implies that JIL does not enforce any of the previously discussed
    rules for text that is entered in an open auto_blobt meta-tag." The scanner gates this on
    the key, and anchors the opener at the value's own start: only a `blob_input:` value that
    STARTS WITH `<auto_blobt>` opens the region -- a `/* <auto_blobt> */` sitting inside an
    ordinary closed comment, or a quoted `"<auto_blobt>"`, is not the region and goes through
    every rule above exactly as an ordinary value does. Once a region opens, every rule above
    is suspended for its text: no `/*` opens a comment, no `key:`-shaped token is a pair (rule
    4b) or a statement boundary (rule 3), and no line is blank-line or comment-line trivia. The
    closer is searched strictly AFTER the opener's own text, so a glued
    `</auto_blobt><auto_blobt>` on the opening line can never self-close against the opener
    that follows it (and that shape opens no region at all, failing the start-anchor on
    whichever line reads it next). The span from the opener through the line holding
    `</auto_blobt>` becomes part of `blob_input`'s `raw_value` verbatim, `\n`-joined the same
    way a rule-6 continuation is. A closer on the opening line needs no further lines. Text
    AFTER the closer, still on the closer's own line, is NOT part of the region: it is an
    ordinary value tail and goes through the ordinary pipeline -- a trailing comment there
    still splits off (rule 5), and a `key:`-shaped pair there still gets the loud rule-4b
    error -- exactly as it would following any other attribute's value. The closer's own `>`
    counts as a real, non-whitespace character immediately before that tail, even though the
    tail is scanned as its own string: a `/*` GLUED right after the closer opens nothing
    (rule 5's own glued-marker rule), so it stays ordinary value text rather than being
    misread as sitting at a value's own start -- it neither swallows following lines into a
    comment body nor hides a `key:`-shaped pair from rule 4b. The vendor states a
    beginning and an end, not an optional end: an opener with no `</auto_blobt>` by EOF is a
    loud scanner error naming the opener's line, never a silent close-at-EOF. Canonical mode
    must not trim the lines strictly inside the region (the vendor's "every character...
    literally" covers trailing whitespace and tabs mid-payload too); only the merged last
    line, where a tail (if any) lives, gets the ordinary per-line trim rule 6's own continuation
    values get. The corpus fixture that pins the structural half of this rule (a `blob_input`
    value whose literal text contains a complete `insert_job:` fragment at column 0, scanning
    into exactly one statement rather than a phantom second one) is synthetic, hand-written for
    this rule -- the vendor's own insert_blob examples use plain prose and a JSON payload, never
    a JIL fragment; the fixture exists to demonstrate that the region cannot start a statement,
    not to reproduce a vendor example.

## Corpus policy

`tests/corpus/` contains **synthetic JIL only**. Each fixture is hand-written from Broadcom
documentation examples, or is generated. Proprietary or production JIL must never
enter this repository (LICENSING.md, operational requirement 2). When one dossier entry is a
fixture's main purpose, its token comes first in the file name, lowercased:
`sem04_lookback.jil`, `l018_calendar_ref.jil`, `m07_mutex.jil`. A fixture that serves several
entries keeps a descriptive name.

## Fidelity tests (normative)

- F1 preserve-mode identity: `render(parse(text)) == text` for every corpus file.
- F2 canonical fixpoint: `c = render_canonical(parse(text))`. Then
  `render_canonical(parse(c)) == c`.
- F3 fuzz: hypothesis-generated JIL-shaped text and raw character soups. Where parse
  succeeds, F1 holds; for the JIL-shaped half, F2 holds as well.
- F4 lexical torture, as an inline case matrix: escaped and quoted colons, a `#` inside quotes,
  a glued glob in a value, a trailing opener whose comment closes on a later line (LF, CRLF,
  and opened on a continuation line, DL-161), a quoted unclosed marker (the glob escape), a
  closed block kept inside a value, a block marker at the value start, a quoted block marker
  whose `*/` falls after the closing quote, a closed block kept in a subcommand subject and
  after the inline `job_type`, the one-line `job_type` form with a trailing comment, and the
  layout corners (no space after the colon, empty value, trailing value spaces, indented
  attribute, empty subject, blank and whitespace-only lines, CRLF, no final newline,
  comment-only file, empty file). Every case is checked for both F1 and F2. The
  unclosed-at-EOF marker is an exact error assertion (message plus opener line, DL-161).
  Key-shaped lookalikes inside a value sit in the rule-4b guard matrix. `/tmp/out:file.err` is
  also in the corpus torture fixture, where F1 and F2 cover it.
