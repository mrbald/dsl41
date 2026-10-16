"""JIL statement-level AST: hand scanner and preserve/canonical renderers.

Normative spec: docs/jil-statement-syntax.md (rules 0-12, 4b included, fidelity tests F1-F4)
and docs/ir-design.md ss2 (model sketches, loss policy). Nothing is interpreted here:
`condition` is a RawAttr like any other, and expression parsing is lowering's job.

Fidelity: `render(parse(text)) == text` byte for byte in preserve mode; canonical mode is a
fixpoint. The ir-design ss2 fields are the semantic API. The extra fields (pre_blank_lines,
indent, sep, post, inline_*, eof_blank_lines, final_newline) are layout trivia that make
preserve-mode rendering exact.

Scanner rules, each pinned by a fixture or unit test:
- Mixed line endings are a parse error (rule 10; JilFile.newline_style is file-wide).
- One-line form (rule 4): only `job_type` is accepted as the inline key; any other second
  `key:` pair on a subcommand line is an error.
- Trailing block comments (rule 5, DL-161): the leftmost unquoted `/*` at the value start or
  after whitespace starts the comment. If a `*/` follows on the line the comment is closed
  there. If none does, the marker opens a block comment spanning lines; its body lines are
  consumed atomically and never reach the scan loop, and a comment still open at EOF is an
  `unterminated block comment` error at the opener line. A stray later `*/` silently closes
  the comment, so quoting is the only complete escape; quoted markers stay value text (rule 7).
  A closed `/*...*/` with value text after it stays in the value as opaque text, and a marker
  glued to the text before it opens nothing. A rule-6 continuation line can open a comment
  (seeded quote state); the comment closes the continuation.
- A full-line block comment must close at end of line, and so must a multi-line trailing
  comment: text after `*/` on the closing line is an error.
- Subcommand-shaped unknown keys (rule 3, DL-18): an attribute-position key matching
  (insert|update|delete|override)_* that is not a recognized subcommand is an error, because a
  missed statement boundary folding into the previous statement is structural loss.
- Calendar exports (rule 11, DL-36): the autocal_asc statements (`calendar`, `cycle`,
  `extended_calendar`) are recognized boundaries. A standard `calendar:` body may carry bare
  date rows, kept verbatim after the attrs; an attribute after a date row is an error.
- Literal blob region (our rule 12, DL-245; the vendor's "JIL Syntax Rules" page, its rule 8,
  numbers differently): a `blob_input:` value that starts with `<auto_blobt>` opens a region
  through the line holding `</auto_blobt>`. No comment or rule-4b detector runs on the span, and
  none of its lines reach the scan loop. A `/* <auto_blobt> */` inside a closed comment or a
  quoted `"<auto_blobt>"` opens nothing. Text after the closer on its line goes through the
  ordinary value-tail pipeline. Canonical mode does not `rstrip()` lines strictly inside the
  region; the merged last line, which holds any tail, gets the ordinary trim. A region open at
  EOF is an error naming the opener's line.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import BaseModel

from dsl41.canon import is_scalar_string

#: Statement boundaries (rule 3), recognized case-insensitively. Unknown keys are attributes,
#: never boundaries.
SUBCOMMANDS = frozenset(
    {
        "insert_job",
        "update_job",
        "delete_job",
        "rename_job",
        "delete_box",
        "insert_machine",
        "update_machine",
        "delete_machine",
        "insert_global",
        "delete_global",
        "override_job",
        "insert_xinst",
        "update_xinst",
        "delete_xinst",
        "insert_blob",
        # DL-245: update_blob and update_glob are documented subcommands (TechDocs)
        "update_blob",
        "delete_blob",
        "insert_glob",
        "update_glob",
        "delete_glob",
        "insert_resource",
        "update_resource",
        "delete_resource",
        # DL-29: the full TechDocs 12.1 subcommand inventory scans (F1 holds over any valid
        # file); lowering refuses the out-of-scope object classes
        "insert_monbro",
        "update_monbro",
        "delete_monbro",
        "insert_job_type",
        "update_job_type",
        "delete_job_type",
        "insert_connectionprofile",
        "update_connectionprofile",
        "delete_connectionprofile",
        # DL-36: the autocal_asc -E/-I calendar-export statements, not jil subcommands. Estates
        # ship exports alongside JIL, and no documented JIL attribute shares these names
        # (the DL-18 argument)
        "calendar",
        "cycle",
        "extended_calendar",
        # Q9 (DL-60): exports write `extended_calendar:`; `ext_calendar:` is the Manage Calendars
        # spelling, accepted as input, and rendering preserves what appeared (SEM-36, DL-57)
        "ext_calendar",
    }
)

#: Rule 11 (DL-36): statements whose body may carry bare date rows (the standard-calendar
#: export). Extended calendars and cycles are pure `key: value`. No calendar-export attribute is in
#: CONTINUATION_ATTRS, so a continuation never swallows date rows; a hand-made `run_calendar:`
#: inside a calendar body does swallow the rows after it (rule 3). The rule-4b guard (DL-160)
#: does not reach date rows: rule 11 carries a row verbatim, and autocal refuses a key-shaped tail.
_DATE_BODY_SUBCOMMANDS = frozenset({"calendar"})

#: Rule 3 guard (DL-18, DL-27): an attribute-position key shaped like a subcommand but not in
#: SUBCOMMANDS is a scanner error, because folding a missed boundary into the previous statement
#: is structural loss. No documented JIL attribute has this shape. The verb list is part of the
#: inventory it protects (`rename_job`).
_SUBCOMMAND_SHAPE_RE = re.compile(r"(?:insert|update|delete|override|rename)_\w+", re.IGNORECASE)

#: Continuation trigger set (rule 6): a non-key-shaped line after one of these attributes continues
#: its value. [?] The exact set is rule 6's open question, to verify against `autorep -q`.
CONTINUATION_ATTRS = frozenset(
    {
        "start_times",
        "start_mins",
        "must_start_times",
        "must_complete_times",
        "run_calendar",
        "exclude_calendar",
    }
)


class JilParseError(ValueError):
    """Loud scanner failure; never silently drop or reinterpret input."""

    def __init__(self, message: str, file: str, line: int) -> None:
        super().__init__(f"{file}:{line}: {message}")
        self.file = file
        self.line = line


def render_location(file: str, line: int) -> str:
    """`file:line`, the citation shorthand diagnostics use (DL-178).

    `SourceSpan.at` is the usual caller; this form serves callers that hold file and line apart
    from a span (placeholders._Entry).
    """
    return f"{file}:{line}"


class SourceSpan(BaseModel):
    file: str
    line_start: int  # 1-based, inclusive
    line_end: int  # 1-based, inclusive
    byte_start: int  # UTF-8 offset of the first line's start
    byte_end: int  # UTF-8 offset past the last line's content (EOL excluded)

    @property
    def at(self) -> str:
        """`file:line_start`, shared by the diagnostic renderers."""
        return render_location(self.file, self.line_start)


class Comment(BaseModel):
    text: str  # raw, including '/*...*/' or '#...' marker; '\n'-joined if multiline
    span: SourceSpan
    attachment: Literal["leading", "trailing", "floating"]
    # True for the rule-5 opener of a value's multi-line trailing comment (DL-161); meaningful
    # only when attachment == "trailing"
    trailing_block: bool = False
    # layout trivia (preserve-mode fidelity only)
    pre_blank_lines: list[str] = []  # verbatim blank/ws-only lines before the comment
    indent: str = ""  # ws before the marker: line indent, or the gap after a value
    post: str = ""  # ws after a closing '*/' to end of line


class RawAttr(BaseModel):
    key: str  # exactly as written (case preserved)
    raw_value: str  # verbatim; continuation lines joined with '\n'
    span: SourceSpan
    comments: list[Comment] = []
    # layout trivia
    pre_blank_lines: list[str] = []
    indent: str = ""
    sep: str = " "  # verbatim ws between ':' and the value
    # Rule 12 (DL-245): the number of leading `raw_value.split("\n")` lines that a literal blob
    # region owns verbatim, 0 for any other attribute. Canonical rendering does not `rstrip()`
    # them; the merged last line, which carries any tail, gets the ordinary trim.
    literal_prefix_lines: int = 0


class JilStatement(BaseModel):
    subcommand: str  # e.g. "insert_job", as written
    subject: str  # the value after the subcommand key (job name, etc.)
    job_type_inline: str | None = None  # 'insert_job: X job_type: c' one-line form
    attrs: list[RawAttr]  # in source order: the fidelity guarantee
    date_lines: list[str] = []  # verbatim autocal date rows; `calendar:` only (rule 11)
    comments: list[Comment] = []
    span: SourceSpan
    # layout trivia
    pre_blank_lines: list[str] = []
    indent: str = ""
    sep: str = " "
    inline_gap: str = ""  # ws between subject and the inline 'job_type:' key
    inline_key: str = "job_type"  # inline key as written (case preserved)
    inline_sep: str = " "  # ws between the inline ':' and its value


class JilFile(BaseModel):
    statements: list[JilStatement]
    trailing_comments: list[Comment] = []
    newline_style: Literal["\n", "\r\n"] = "\n"
    # layout trivia
    eof_blank_lines: list[str] = []  # verbatim blank lines after the last element
    final_newline: bool = True
    file: str = "<memory>"


# --------------------------------------------------------------------------- scanner

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
#: One-line form (rule 4): a second `key:` pair on the subcommand line, split on the whitespace
#: before it. The char before ':' is an identifier char, so the colon is never the escaped `\:`.
_INLINE_RE = re.compile(r"([ \t]+)([A-Za-z_][A-Za-z0-9_]*):")

#: Rule 12 literal blob region (DL-245): the vendor's meta-tag spelling ("JIL Syntax Rules",
#: its rule 8), case-sensitive as in every vendor example (`blob_input: <auto_blobt>...`).
_BLOB_OPEN = "<auto_blobt>"
_BLOB_CLOSE = "</auto_blobt>"


def parse(text: str, file: str = "<memory>") -> JilFile:
    """Scan JIL text into a byte-faithful AST (rules 0-12 of the scanner spec)."""
    return _Scanner(text, file).scan()


def parse_file(path: str | Path) -> JilFile:
    """Parse a file, bypassing universal-newline translation (fidelity!)."""
    p = Path(path)
    return parse(p.read_bytes().decode("utf-8"), file=str(p))


class _TrailingSplit(NamedTuple):
    """One value body split at its rule-5 trailing comment; `comment_text == ""` means none."""

    #: the whole body when there is no comment
    value: str
    gap: str
    #: opener through close, or the opener tail if the comment is open
    comment_text: str
    #: text after a closed comment's `*/`; "" when the comment is open
    post: str
    #: no `*/` on this line: the marker opens a multi-line block comment (rule 5, DL-161)
    opens_comment: bool


def _split_trailing_comment(
    body: str, *, in_quote: bool = False, at_start: bool = True
) -> _TrailingSplit:
    """Split a value body into (value, gap, comment_text, post, open).

    comment_text == "" means no trailing comment. Only `/* ... */` block comments trail a
    value; a mid-line `#` is value text, because `#` comments are first-column only (DL-31).
    Markers count only outside double quotes, at the value start or after whitespace (rule 5).

    A marker whose first `*/` ends the line is a closed comment (open=False; post holds the
    run after `*/`). A marker with no `*/` on the line opens a multi-line comment (rule 5,
    DL-161): comment_text holds the opener tail and open=True, and the caller consumes the body
    lines. A closed `/*...*/` with value text after it stays in the value, and a quoted or glued
    marker opens nothing. `in_quote` seeds the quote walk for a rule-6 continuation line
    (DL-160, DL-161). `at_start` is False when `body[0]` is glued to the closer's `>` of a
    rule-12 blob region (DL-245), where a `/*` opens nothing.
    """
    in_q = in_quote
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == '"':
            in_q = not in_q
        elif (
            not in_q
            and body.startswith("/*", i)
            and ((i == 0 and at_start) or (i > 0 and body[i - 1] in " \t"))
        ):
            close = body.find("*/", i + 2)
            value_ws = body[:i]
            value = value_ws.rstrip(" \t")
            gap = value_ws[len(value) :]
            if close == -1:
                # Rule 5 (DL-161): no `*/` on the line, so the marker opens a block comment that
                # spans lines
                return _TrailingSplit(value, gap, body[i:], "", True)
            after = body[close + 2 :]
            if after.strip() == "":
                return _TrailingSplit(value, gap, body[i : close + 2], after, False)
            i = close + 2  # closed block with value text after it: opaque, keep scanning
            continue
        i += 1
    return _TrailingSplit(body, "", "", "", False)


def value_opens_comment(value: str) -> bool:
    """True when the bare spelling of `value` would open a rule-5 block comment (DL-161).

    JIL-emitting paths call this to re-escape: such a value must be quoted or it swallows the
    lines after it.
    """
    return _split_trailing_comment(value).opens_comment


def _find_inline_pair(value: str, *, in_quote: bool = False) -> re.Match[str] | None:
    """First quote-unshadowed `key:` pair after whitespace (one-line form).

    `in_quote` seeds the parity walk for a rule-6 continuation line (rule 4b, DL-160).
    """
    parity = 1 if in_quote else 0
    for m in _INLINE_RE.finditer(value):
        if (parity + value.count('"', 0, m.start())) % 2 == 0:
            return m
    return None


#: Mask filler for `_mask_closed_blocks`. It is not a key character, a quote or whitespace, because
#: rule 4b reads a pair only where the `key:` token follows whitespace (`command: a /* c */b: x`
#: has no second pair).
_MASK_FILL = "*"


def _mask_closed_blocks(value: str, *, in_quote: bool = False, at_start: bool = True) -> str:
    """Mask closed `/*...*/` spans (char for char, offsets kept) before the rule-4/4b pair scan.

    Rule 5 keeps a closed inline comment with trailing text as value text, so a `key:` shape
    inside one is not a second pair. A span opens where `_split_trailing_comment` opens a
    comment: an unquoted marker at the value start or after whitespace. A quote inside a masked
    span toggles nothing, so `"/* " */ key: value` still trips the rule-4b guard against the
    DL-30 silent-loss class (DL-151). `in_quote` seeds the walk for a rule-6 continuation line
    (rule 4b, DL-160). `at_start` mirrors `_split_trailing_comment`: False when `value[0]` is
    glued to a rule-12 blob closer's `>` (DL-245).
    """
    out = list(value)
    in_q = in_quote
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == '"':
            in_q = not in_q
        elif (
            not in_q
            and value.startswith("/*", i)
            and ((i == 0 and at_start) or (i > 0 and value[i - 1] in " \t"))
        ):
            close = value.find("*/", i + 2)
            if close == -1:
                # reachable only on a rule-6 continuation line, which is carried verbatim; an
                # attribute value cannot keep an unclosed marker (rule 5, DL-161)
                break
            out[i : close + 2] = [_MASK_FILL] * (close + 2 - i)
            i = close + 2
            continue
        i += 1
    return "".join(out)


class _Scanner:
    def __init__(self, text: str, file: str) -> None:
        # PR-10a: an unpaired surrogate has no UTF-8 spelling, so span arithmetic would raise
        # and the estate could never be sealed (period-model ss3.2); refused here with its line
        if not is_scalar_string(text):
            bad = next(i for i, ch in enumerate(text) if not is_scalar_string(ch))
            raise JilParseError(
                "unpaired surrogate: JIL text must be Unicode scalar values",
                file,
                text.count("\n", 0, bad) + 1,
            )
        self.text = text
        self.file = file
        self.style: Literal["\n", "\r\n"] = self._detect_newline()
        self.final_newline = bool(text) and text.endswith(self.style)
        if text == "":
            self.lines: list[str] = []
        else:
            self.lines = text.split(self.style)
            if self.final_newline:
                self.lines.pop()
        starts: list[int] = []
        pos = 0
        for ln in self.lines:
            starts.append(pos)
            pos += len(ln.encode("utf-8")) + len(self.style)
        self.starts = starts

    def _detect_newline(self) -> Literal["\n", "\r\n"]:
        text = self.text
        if "\r\n" in text:
            rest = text.replace("\r\n", "\x00")
            stray = [j for j in (rest.find("\r"), rest.find("\n")) if j != -1]
            if stray:
                j = min(stray)
                line = sum(rest.count(c, 0, j) for c in ("\x00", "\r", "\n")) + 1
                raise JilParseError("mixed line endings", self.file, line)
            return "\r\n"
        if "\r" in text:
            line = text.count("\n", 0, text.find("\r")) + 1
            raise JilParseError("bare-CR line endings are unsupported", self.file, line)
        return "\n"

    def _span(self, i0: int, i1: int) -> SourceSpan:
        return SourceSpan(
            file=self.file,
            line_start=i0 + 1,
            line_end=i1 + 1,
            byte_start=self.starts[i0],
            byte_end=self.starts[i1] + len(self.lines[i1].encode("utf-8")),
        )

    def scan(self) -> JilFile:
        stmts: list[JilStatement] = []
        pend_c: list[Comment] = []  # full-line comments awaiting their element
        pend_b: list[str] = []  # blank lines since the last comment/element
        cur: JilStatement | None = None
        cont: RawAttr | None = None  # open continuation target (rule 6)
        cont_quote = False  # quote parity at the end of cont's joined value
        i = 0
        while i < len(self.lines):
            line = self.lines[i]
            if line.strip() == "":
                pend_b.append(line)
                cont = None
                i += 1
                continue
            indent = line[: len(line) - len(line.lstrip(" \t"))]
            body = line[len(indent) :]
            if body.startswith("#"):
                pend_c.append(
                    Comment(
                        text=body,
                        span=self._span(i, i),
                        attachment="leading",
                        pre_blank_lines=pend_b,
                        indent=indent,
                    )
                )
                pend_b = []
                cont = None
                i += 1
                continue
            if body.startswith("/*"):
                comment, i = self._scan_block_comment(i, indent, body, pend_b)
                pend_c.append(comment)
                pend_b = []
                cont = None
                i += 1
                continue
            m = _KEY_RE.match(body)
            if m is not None and m.end() < len(body) and body[m.end()] == ":":
                # Rule 1/2: the first unescaped colon with a valid key-shaped prefix.
                key = m.group(0)
                rest = body[m.end() + 1 :]
                sep = rest[: len(rest) - len(rest.lstrip(" \t"))]
                candidate = rest[len(sep) :]
                span = self._span(i, i)
                # Rule 12 (DL-245): the literal <auto_blobt> meta-tag belongs to blob_input and
                # anchors at the value start; elsewhere (`/* <auto_blobt> */`, a quoted tag) the
                # value takes the normal rule-4b and rule-5 handling
                blob_open = key.lower() == "blob_input" and candidate.startswith(_BLOB_OPEN)
                literal, tail, k = "", candidate, i
                if blob_open:
                    # Rule 12: every character up to the closer is literal, so the span never
                    # reaches the comment splitter or the rule-4b detector, and its lines never
                    # reach the scan loop (an `insert_job:` fragment in blob text is not a
                    # statement). Open at EOF is an error at the opener line. Text after the
                    # closer on its line runs through the ordinary value-tail pipeline below.
                    literal, tail, k = self._scan_blob_literal(i, candidate)
                literal_prefix_lines = literal.count("\n")
                # `at_start=not blob_open`: after a blob closer, offset 0 of `tail` is glued to the
                # closer's `>` (DL-245), so a `/*` there opens nothing (rule 5)
                tail_value, gap, ctext, cpost, copen = _split_trailing_comment(
                    tail, at_start=not blob_open
                )
                value = literal + tail_value
                if copen:
                    # Rule 5 (DL-161): the trailing marker opens a multi-line comment. Its body
                    # lines are consumed here, by the walk a full-line comment uses, so the rule-6
                    # branch and its 4b detector (DL-160) see only value lines. Open at EOF is an
                    # `unterminated block comment` error at the opener line
                    tc, k = self._scan_block_comment(k, gap, ctext, [])
                    tc.attachment = "trailing"
                    tc.trailing_block = True
                    trailing: Comment | None = tc
                else:
                    trailing = (
                        Comment(
                            text=ctext,
                            span=self._span(k, k),
                            attachment="trailing",
                            indent=gap,
                            post=cpost,
                            # a closed tail comment rides the last value line of a multi-line blob,
                            # as `trailing_block` places a rule-5 opener (DL-161)
                            trailing_block=blob_open,
                        )
                        if ctext
                        else None
                    )
                comments, blanks, pend_c, pend_b = pend_c, pend_b, [], []
                if key.lower() in SUBCOMMANDS:
                    cur = self._make_statement(
                        key, value, indent, sep, span, comments, blanks, trailing, i
                    )
                    stmts.append(cur)
                    if k > i:
                        self._extend_span(cur.span, k)
                    cont = None
                else:
                    if _SUBCOMMAND_SHAPE_RE.fullmatch(key):
                        raise JilParseError(
                            f"{key!r} is shaped like a subcommand but is not a recognized"
                            " statement boundary; folding it into the previous statement"
                            " would be silent structural loss (rule 3, DL-18)",
                            self.file,
                            i + 1,
                        )
                    if cur is None:
                        raise JilParseError(
                            f"attribute line {key!r} before any statement", self.file, i + 1
                        )
                    if cur.date_lines:
                        # Rule 11 (DL-36): attributes precede date rows; re-rendering an interleaved
                        # shape would reorder it
                        raise JilParseError(
                            f"attribute line {key!r} after the date rows of"
                            f" {cur.subcommand}: {cur.subject.strip()!r} (rule 11, DL-36)",
                            self.file,
                            i + 1,
                        )
                    # `tail_value` is the whole value, or for a rule-12 region (DL-245) the text
                    # after the closer: the region is exempt from rule 4b, its tail gets the
                    # ordinary check with `at_start=False`
                    masked = _mask_closed_blocks(tail_value, at_start=not blob_open)
                    if (pair := _find_inline_pair(masked)) is not None:
                        # Rule 4b (DL-30): JIL permits several `attr: value` statements on one line;
                        # folding the second into the first value is silent loss the DL-07 firewall
                        # cannot see. Valid JIL escapes or quotes value colons (the DL-18 argument)
                        raise JilParseError(
                            f"value of {key!r} carries a second {pair.group(2)!r}:-shaped"
                            " pair; JIL allows multiple attribute statements per line, so"
                            " folding it into the value would be silent loss -- split the"
                            " line, or escape the colon as \\: (or quote the value) if it"
                            " is value text (rule 4b, DL-30)",
                            self.file,
                            i + 1,
                        )
                    if trailing is not None:
                        comments = [*comments, trailing]
                    attr = RawAttr(
                        key=key,
                        raw_value=value,
                        span=span,
                        comments=comments,
                        pre_blank_lines=blanks,
                        indent=indent,
                        sep=sep,
                        literal_prefix_lines=literal_prefix_lines,
                    )
                    cur.attrs.append(attr)
                    if k > i:
                        # the attr owns its multi-line trailing comment's lines (DL-161)
                        self._extend_span(attr.span, k)
                    self._extend_span(cur.span, k)
                    # a multi-line trailing comment closes the continuation (rule 6, DL-161); a
                    # closed single-line one leaves it armed. A blob region is never a continuation
                    # trigger (CONTINUATION_ATTRS holds no blob key)
                    cont = attr if key.lower() in CONTINUATION_ATTRS and not copen else None
                    if cont is not None:
                        cont_quote = masked.count('"') % 2 == 1
                i = k + 1
                continue
            if cont is not None and not pend_c and not pend_b:
                # Rule 6: a non-key-shaped line continues the open list-valued attr. Rule 4b covers
                # the joined value (DL-160), with quote parity seeded from the lines above; a hit
                # is the DL-30 loss class. Rule 11 date rows stay out (DL-36). The line can open
                # a block comment under the same seeded quote state (DL-161); only the
                # pre-opener prefix is value, and the comment closes the continuation
                cvalue, cgap, ctext, _cp, copen = _split_trailing_comment(line, in_quote=cont_quote)
                scan_text = cvalue + cgap if copen else line
                masked = _mask_closed_blocks(scan_text, in_quote=cont_quote)
                if (pair := _find_inline_pair(masked, in_quote=cont_quote)) is not None:
                    raise JilParseError(
                        f"continuation of {cont.key!r} carries a {pair.group(2)!r}:-shaped"
                        " pair; JIL allows multiple attribute statements per line, so"
                        " folding it into the joined value would be silent loss -- split"
                        " the line, or escape the colon as \\: (or quote the value) if"
                        " it is value text (rule 4b, DL-160)",
                        self.file,
                        i + 1,
                    )
                assert cur is not None
                if copen:
                    tc, k = self._scan_block_comment(i, cgap, ctext, [])
                    tc.attachment = "trailing"
                    tc.trailing_block = True
                    cont.comments.append(tc)
                    cont.raw_value += "\n" + cvalue
                    self._extend_span(cont.span, k)
                    self._extend_span(cur.span, k)
                    cont = None
                    i = k + 1
                    continue
                cont_quote ^= masked.count('"') % 2 == 1
                cont.raw_value += "\n" + line
                self._extend_span(cont.span, i)
                self._extend_span(cur.span, i)
                i += 1
                continue
            if (
                cur is not None
                and cur.subcommand.lower() in _DATE_BODY_SUBCOMMANDS
                and not pend_c
                and not pend_b
            ):
                # Rule 11 (DL-36): a bare date row of a standard-calendar export, carried
                # verbatim, contiguous with its statement
                cur.date_lines.append(line)
                self._extend_span(cur.span, i)
                i += 1
                continue
            raise JilParseError(
                "unrecognized line (not an attribute, comment, blank, continuation,"
                " or calendar date row)",
                self.file,
                i + 1,
            )
        for c in pend_c:
            c.attachment = "floating"
        return JilFile(
            statements=stmts,
            trailing_comments=pend_c,
            newline_style=self.style,
            eof_blank_lines=pend_b,
            final_newline=self.final_newline,
            file=self.file,
        )

    def _extend_span(self, span: SourceSpan, i: int) -> None:
        span.line_end = i + 1
        span.byte_end = self.starts[i] + len(self.lines[i].encode("utf-8"))

    def _scan_block_comment(
        self, i: int, indent: str, body: str, pend_b: list[str]
    ) -> tuple[Comment, int]:
        parts = [body]
        k = i
        close = body.find("*/", 2)  # from 2: '/*/' alone does not self-close
        while close == -1:
            k += 1
            if k >= len(self.lines):
                raise JilParseError("unterminated block comment", self.file, i + 1)
            parts.append(self.lines[k])
            close = parts[-1].find("*/")
        after = parts[-1][close + 2 :]
        if after.strip():
            raise JilParseError("content after '*/' on a block-comment line", self.file, k + 1)
        parts[-1] = parts[-1][: close + 2]
        comment = Comment(
            text="\n".join(parts),
            span=self._span(i, k),
            attachment="leading",
            pre_blank_lines=pend_b,
            indent=indent,
            post=after,
        )
        return comment, k

    def _scan_blob_literal(self, i: int, candidate: str) -> tuple[str, str, int]:
        """Rule 12 literal blob region (DL-245).

        `candidate` already starts with `<auto_blobt>`; the caller anchors that. The closer is
        searched after the opener's own text, so a glued `</auto_blobt><auto_blobt>` never
        self-closes.

        Returns `(literal, tail, k)`: `literal` runs from the opener through the closer,
        verbatim and `\\n`-joined across lines; `tail` is the ordinary text after the closer on
        its line, which the caller runs through the usual trailing-comment and rule-4b pipeline;
        `k` is the index of the last line consumed. An opener with no closer by EOF is an error
        naming the opener's line.
        """
        close = candidate.find(_BLOB_CLOSE, len(_BLOB_OPEN))
        if close != -1:
            end = close + len(_BLOB_CLOSE)
            return candidate[:end], candidate[end:], i
        parts = [candidate]
        k = i
        while True:
            k += 1
            if k >= len(self.lines):
                raise JilParseError(
                    'unterminated <auto_blobt> blob literal (vendor "JIL Syntax Rules" rule 8)',
                    self.file,
                    i + 1,
                )
            line = self.lines[k]
            close = line.find(_BLOB_CLOSE)
            if close != -1:
                end = close + len(_BLOB_CLOSE)
                parts.append(line[:end])
                return "\n".join(parts), line[end:], k
            parts.append(line)

    def _make_statement(
        self,
        key: str,
        value: str,
        indent: str,
        sep: str,
        span: SourceSpan,
        comments: list[Comment],
        blanks: list[str],
        trailing: Comment | None,
        i: int,
    ) -> JilStatement:
        subject = value
        jt: str | None = None
        inline_key = "job_type"
        inline_gap = ""
        inline_sep = " "
        # Rule 4 runs the rule-4b detector, so it masks closed blocks the same way (DL-151):
        # `insert_job: j /* see owner: bob */ tail` has no inline `owner` pair (rule 5). The fill
        # keeps offsets, and `m.group(1)` is real whitespace
        masked = _mask_closed_blocks(value)
        m = _find_inline_pair(masked)
        if m is not None:
            k2 = m.group(2)
            if k2.lower() != "job_type":
                raise JilParseError(
                    f"unsupported inline attribute {k2!r} on subcommand line "
                    "(only job_type; jil-statement-syntax.md rule 4)",
                    self.file,
                    i + 1,
                )
            subject = value[: m.start(1)]
            tail = value[m.end() :]
            inline_sep = tail[: len(tail) - len(tail.lstrip(" \t"))]
            jt = tail[len(inline_sep) :]
            # reuse the line mask: re-masking `jt` would re-anchor rule 5's value start and
            # swallow the pair in `job_type:/* key: y */ z`
            if _find_inline_pair(masked[m.end() + len(inline_sep) :]) is not None:
                raise JilParseError(
                    "multiple inline attributes on subcommand line", self.file, i + 1
                )
            inline_key = k2
            inline_gap = m.group(1)
        if trailing is not None:
            comments = [*comments, trailing]
        return JilStatement(
            subcommand=key,
            subject=subject,
            job_type_inline=jt,
            attrs=[],
            comments=comments,
            span=span,
            pre_blank_lines=blanks,
            indent=indent,
            sep=sep,
            inline_gap=inline_gap,
            inline_key=inline_key,
            inline_sep=inline_sep,
        )


# ------------------------------------------------------------------------- renderers


def render(jf: JilFile, mode: Literal["preserve", "canonical"] = "preserve") -> str:
    return render_preserve(jf) if mode == "preserve" else render_canonical(jf)


def _trailing_pair(comments: list[Comment]) -> tuple[Comment | None, Comment | None]:
    """The rule-5 trailing pair on one value: (inline rider, block rider).

    A closed trailing comment rides the first value line; a multi-line one (DL-161) rides the
    last. Read off `Comment.trailing_block`, not re-derived from `"\\n" in c.text` (F4).
    """
    inline_tc: Comment | None = None
    block_tc: Comment | None = None
    for c in comments:
        if c.attachment == "trailing":
            if c.trailing_block:
                block_tc = c
            elif inline_tc is None:
                inline_tc = c
    return inline_tc, block_tc


def render_preserve(jf: JilFile) -> str:
    """Byte-exact reconstruction of the source (F1: render(parse(x)) == x)."""
    out: list[str] = []

    def emit_full_line(c: Comment) -> None:
        out.extend(c.pre_blank_lines)
        parts = c.text.split("\n")
        parts[0] = c.indent + parts[0]
        parts[-1] = parts[-1] + c.post
        out.extend(parts)

    def emit_with_trailing(first: str, rest: list[str], comments: list[Comment]) -> None:
        # a closed trailing comment rides the first value line; a multi-line one (DL-161) rides
        # the last, with body lines emitted as their own entries so `nl.join` sees no embedded
        # '\n' (CRLF fidelity)
        inline_tc, block_tc = _trailing_pair(comments)
        if inline_tc is not None:
            first = first + inline_tc.indent + inline_tc.text + inline_tc.post
        lines = [first, *rest]
        if block_tc is not None:
            parts = block_tc.text.split("\n")
            parts[0] = lines[-1] + block_tc.indent + parts[0]
            parts[-1] = parts[-1] + block_tc.post
            lines = lines[:-1] + parts
        out.extend(lines)

    for stmt in jf.statements:
        for c in stmt.comments:
            if c.attachment != "trailing":
                emit_full_line(c)
        out.extend(stmt.pre_blank_lines)
        header = stmt.indent + stmt.subcommand + ":" + stmt.sep + stmt.subject
        if stmt.job_type_inline is not None:
            header += stmt.inline_gap + stmt.inline_key + ":" + stmt.inline_sep
            header += stmt.job_type_inline
        emit_with_trailing(header, [], stmt.comments)
        for a in stmt.attrs:
            for c in a.comments:
                if c.attachment != "trailing":
                    emit_full_line(c)
            out.extend(a.pre_blank_lines)
            vlines = a.raw_value.split("\n")
            emit_with_trailing(a.indent + a.key + ":" + a.sep + vlines[0], vlines[1:], a.comments)
        out.extend(stmt.date_lines)
    for c in jf.trailing_comments:
        emit_full_line(c)
    out.extend(jf.eof_blank_lines)
    if not out:
        return ""
    nl = jf.newline_style
    return nl.join(out) + (nl if jf.final_newline else "")


def render_statement(stmt: JilStatement) -> str:
    """Preserve-render one statement as a block: its own bytes minus the separating blank lines.

    The ss10 `spec` query serves this, so operators read the block that was loaded.
    """
    solo = stmt.model_copy(update={"pre_blank_lines": []})
    # lstrip: a leading comment carries its own pre_blank_lines trivia
    return render_preserve(JilFile(statements=[solo], file=stmt.span.file)).lstrip("\n")


#: Canonical attribute order (ir-design ss2): subcommand first, then a fixed key order, unknown
#: keys alphabetically last. The order within the known set is stable because canonical output is
#: a diff and storage format. Duplicate keys keep their source order.
_CANONICAL_KEY_ORDER: tuple[str, ...] = (
    "job_type",
    "box_name",
    "command",
    "machine",
    "machine_method",
    "owner",
    "permission",
    "date_conditions",
    "days_of_week",
    "run_calendar",
    "exclude_calendar",
    "start_times",
    "start_mins",
    "run_window",
    "timezone",
    "must_start_times",
    "must_complete_times",
    "condition",
    "box_success",
    "box_failure",
    "box_terminator",
    "job_terminator",
    "max_exit_success",
    "success_codes",
    "fail_codes",
    "term_run_time",
    "n_retrys",
    "auto_hold",
    "auto_delete",
    "watch_file",
    "watch_interval",
    "watch_file_min_size",
    "job_load",
    "priority",
    "job_class",
    "avg_runtime",
    "profile",
    "envvars",
    "ulimit",
    "elevated",
    "interactive",
    "std_in_file",
    "std_out_file",
    "std_err_file",
    "chk_files",
    "heartbeat_interval",
    "alarm_if_fail",
    "description",
    "value",
)
_ORDER_INDEX = {k: n for n, k in enumerate(_CANONICAL_KEY_ORDER)}
_UNKNOWN_RANK = len(_CANONICAL_KEY_ORDER)


def render_canonical(jf: JilFile) -> str:
    """Lexical canonical form (F2 fixpoint): stable attribute order, one space after ':', the
    one-line form split into a job_type attr, trivia dropped, one blank line between
    statements, '\\n' endings. Abbreviation expansion is IR-level (ir-design ss2)."""
    blocks: list[str] = []
    for stmt in jf.statements:
        lines: list[str] = []
        _emit_canonical_comments(lines, stmt.comments)
        subject = stmt.subject.rstrip()
        has_trailing = any(c.attachment == "trailing" for c in stmt.comments)
        if stmt.job_type_inline is not None and not has_trailing:
            # a closed block comment between the subject and the inline `job_type:` rides inside
            # `stmt.subject`. Once `job_type` moves to its own line it is line-final, and a second
            # parse would normalize its gap (`_canonical_trailing`), breaking the fixpoint (F2,
            # DL-159); re-split here as that parse would
            value, _gap, ctext, _post, copen = _split_trailing_comment(stmt.subject)
            if ctext and not copen:
                # a parsed subject holds no open marker (DL-161); the guard keeps a hand-built AST
                # verbatim
                subject = f"{value} {ctext}"
        header = f"{stmt.subcommand}: {subject}" if subject else f"{stmt.subcommand}:"
        lines.extend(_canonical_with_trailing(header, [], stmt.comments))
        attrs = list(stmt.attrs)
        if stmt.job_type_inline is not None:
            attrs.insert(
                0, RawAttr(key=stmt.inline_key, raw_value=stmt.job_type_inline, span=stmt.span)
            )
        for a in _canonical_sort(attrs):
            _emit_canonical_comments(lines, a.comments)
            # Rule 12 (DL-245): lines strictly inside a literal blob region
            # (`literal_prefix_lines`) keep their trailing whitespace; the merged last line is
            # trimmed
            raw_lines = a.raw_value.split("\n")
            vlines = [
                ln if idx < a.literal_prefix_lines else ln.rstrip()
                for idx, ln in enumerate(raw_lines)
            ]
            first = f"{a.key}: {vlines[0]}" if vlines[0] else f"{a.key}:"
            lines.extend(_canonical_with_trailing(first, vlines[1:], a.comments))
        lines.extend(ln.strip() for ln in stmt.date_lines)
        blocks.append("\n".join(lines))
    if jf.trailing_comments:
        lines = []
        _emit_canonical_comments(lines, jf.trailing_comments)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n" if blocks else ""


def _emit_canonical_comments(lines: list[str], comments: list[Comment]) -> None:
    for c in comments:
        if c.attachment != "trailing":
            lines.extend(ln.rstrip() for ln in c.text.split("\n"))


def _canonical_with_trailing(first: str, rest: list[str], comments: list[Comment]) -> list[str]:
    """The value lines plus trailing comments: gap one space, comment lines right-stripped.

    Placement matches the preserve renderer: a closed comment on the first value line, a
    multi-line one (DL-161) on the last with its body lines as their own lines.
    """
    inline_tc, block_tc = _trailing_pair(comments)
    if inline_tc is not None:
        first = f"{first} {inline_tc.text.rstrip()}"
    lines = [first, *rest]
    if block_tc is not None:
        parts = [ln.rstrip() for ln in block_tc.text.split("\n")]
        parts[0] = f"{lines[-1]} {parts[0]}"
        lines = lines[:-1] + parts
    return lines


def _canonical_sort(attrs: list[RawAttr]) -> list[RawAttr]:
    def sort_key(pair: tuple[int, RawAttr]) -> tuple[int, str, int]:
        idx, attr = pair
        kl = attr.key.lower()
        rank = _ORDER_INDEX.get(kl, _UNKNOWN_RANK)
        return (rank, kl if rank == _UNKNOWN_RANK else "", idx)

    return [a for _, a in sorted(enumerate(attrs), key=sort_key)]
