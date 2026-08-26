"""JIL statement-level AST: hand scanner + preserve/canonical renderers.

Normative spec: docs/jil-statement-syntax.md (tokenization rules 0-11, 4b
included, and fidelity tests F1-F4) and docs/ir-design.md ss2 (model sketches,
loss policy). No interpretation happens at this layer: `condition` is a RawAttr
like any other; expression parsing is lowering's job.

Fidelity contract: `render(parse(text)) == text` byte-exact in preserve mode;
canonical mode is a fixpoint. The ir-design ss2 sketch fields are the semantic
API; the extra fields here (pre_blank_lines, indent, sep, post, inline_*,
eof_blank_lines, final_newline) are layout trivia that exist solely to make
preserve-mode rendering byte-exact -- zero loss, ever.

Scanner decisions, spec'd via the 2026-07-03 amendments to
jil-statement-syntax.md (each pinned by a fixture or unit test):
- Mixed line endings are a parse error (rule 10; JilFile.newline_style is
  file-wide by the ir-design ss2 model).
- One-line form (rule 4): only `job_type` is accepted as the inline key; any
  other second `key:` pair on a subcommand line is a loud error, never silently
  folded into the subject.
- Trailing block comments: the leftmost whitespace-preceded (or value-start),
  unquoted `/*` starts the comment. If its first following `*/` ends the line,
  the comment is closed on the line. If NO `*/` follows on the line, the
  marker OPENS a block comment that spans lines (rule 5, DL-161: /*-comment
  formats open closure-independently; the whitespace-preceded predicate is
  dsl41's retained glob boundary, not part of that analogy). The body lines
  are consumed atomically -- they never reach the scan loop -- and a comment
  still open at EOF is a loud `unterminated block comment` error at the
  opener line. A wrongly terminated comment is NOT loud: a stray later `*/`
  silently captures the lines up to it; quoting is the only complete escape,
  and quoted markers stay value text (rule 7). A closed `/*...*/` with value
  text after it is kept in the value as opaque text, and a marker glued to
  the text before it opens nothing. A rule-6 continuation line can open a
  comment too (seeded quote state); the comment closes the continuation.
- A full-line block comment must close at end of line; non-whitespace content
  after `*/` on the closing line is an error. The same close rule governs a
  multi-line trailing comment.
- Subcommand-shaped unknown keys (rule 3, amended 2026-07-09 / DL-18): an
  attribute-position key matching (insert|update|delete|override)_* that is
  not a recognized subcommand is a loud error, never an attribute -- a missed
  statement boundary folding into the previous statement is structural loss.
- Calendar exports (rule 11, 2026-07-10 / DL-36): the autocal_asc export
  statements (`calendar`, `cycle`, `extended_calendar`) are recognized
  boundaries; a standard `calendar:` body may carry bare date rows, kept
  verbatim after the attrs. An attribute after a date row is a loud error
  (re-rendering would reorder it).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, NamedTuple

from pydantic import BaseModel

from dsl41.canon import is_scalar_string

#: Statement boundaries per jil-statement-syntax.md rule 3, recognized
#: case-insensitively. Unknown keys are attributes, never boundaries
#: (forward compatibility).
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
        "delete_blob",
        "insert_glob",
        "delete_glob",
        "insert_resource",
        "update_resource",
        "delete_resource",
        # DL-29: the full TechDocs 12.1 subcommand inventory scans (F1 holds
        # over any valid file); lowering refuses the out-of-scope object
        # classes loudly.
        "insert_monbro",
        "update_monbro",
        "delete_monbro",
        "insert_job_type",
        "update_job_type",
        "delete_job_type",
        "insert_connectionprofile",
        "update_connectionprofile",
        "delete_connectionprofile",
        # DL-36: the autocal_asc -E/-I calendar-export statements (TechDocs
        # 12.1 "autocal_asc Command") -- NOT jil subcommands; the vendor
        # processes them with a different binary. Accepted here because
        # migration estates ship calendar exports alongside JIL and the
        # format is JIL-shaped except standard-calendar date rows (rule 11).
        # No documented JIL attribute shares these names, so boundary
        # recognition costs nothing on valid input (the DL-18 argument).
        "calendar",
        "cycle",
        "extended_calendar",
        # Q9 RESOLVED (DL-60): an observed export sample writes
        # `extended_calendar:`; `ext_calendar:` is the Manage Calendars
        # syntax-block spelling, kept accepted as input leniency --
        # rendering preserves what appeared (SEM-36, DL-57)
        "ext_calendar",
    }
)

#: Rule 11 (DL-36): statements whose body may carry bare date rows (the
#: autocal_asc standard-calendar export). Extended calendars and cycles are
#: pure `key: value` and stay out. The scan loop tests rule 6 before this
#: branch and needs no exclusion for it: no calendar-export attribute is in
#: CONTINUATION_ATTRS, so a well-formed export never holds a continuation open
#: over its date rows. Unknown keys stay attributes (rule 3), so a hand-made
#: `run_calendar:` inside a calendar body does swallow the rows that follow.
#: The rule-4b continuation guard (DL-160) does not reach date rows either:
#: rule 11 carries a row verbatim and does not validate its shape; a
#: key-shaped tail on a row is autocal's to refuse at consumption.
_DATE_BODY_SUBCOMMANDS = frozenset({"calendar"})

#: Rule 3 guard (amended 2026-07-09, DL-18; rename_ added 2026-07-10, DL-27):
#: an attribute-position key shaped like a subcommand but not in SUBCOMMANDS
#: is a scanner error -- folding a missed statement boundary into the previous
#: statement is silent structural loss. No documented JIL attribute has this
#: shape. `rename_job` (TechDocs 12.x) proved the guard's verb list is part of
#: the inventory it protects: before DL-27 it matched nothing and folded.
_SUBCOMMAND_SHAPE_RE = re.compile(r"(?:insert|update|delete|override|rename)_\w+", re.IGNORECASE)

#: Continuation trigger set per jil-statement-syntax.md rule 6: a non-key-shaped
#: line following one of these attributes continues that attribute's value.
#: [?] Verify the exact trigger set against `autorep -q` output from a real
#: estate (rule 6's own open question); encode findings as corpus fixtures.
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


class SourceSpan(BaseModel):
    file: str
    line_start: int  # 1-based, inclusive
    line_end: int  # 1-based, inclusive
    byte_start: int  # UTF-8 offset of the first line's start
    byte_end: int  # UTF-8 offset past the last line's content (EOL excluded)


class Comment(BaseModel):
    text: str  # raw, including '/*...*/' or '#...' marker; '\n'-joined if multiline
    span: SourceSpan
    attachment: Literal["leading", "trailing", "floating"]
    # True when this is the rule-5 opener that carries a value's multi-line
    # trailing comment (DL-161): the scanner knows this at scan time
    # (`_scan_block_comment` + the `attachment = "trailing"` stamp), so the
    # fact is carried here instead of re-derived from `"\n" in text` -- only
    # meaningful when attachment == "trailing".
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


class JilStatement(BaseModel):
    subcommand: str  # e.g. "insert_job", as written
    subject: str  # the value after the subcommand key (job name, etc.)
    job_type_inline: str | None = None  # 'insert_job: X job_type: c' one-line form
    attrs: list[RawAttr]  # ORDER PRESERVED -- this is the fidelity guarantee
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
#: One-line form (rule 4): a second `key:` pair on the subcommand line, split on
#: the whitespace run before it. The char before ':' is an identifier char, so
#: the colon can never be the escaped form `\:`.
_INLINE_RE = re.compile(r"([ \t]+)([A-Za-z_][A-Za-z0-9_]*):")


def parse(text: str, file: str = "<memory>") -> JilFile:
    """Scan JIL text into a byte-faithful AST (rules 0-11 of the scanner spec)."""
    return _Scanner(text, file).scan()


def parse_file(path: str | Path) -> JilFile:
    """Parse a file, bypassing universal-newline translation (fidelity!)."""
    p = Path(path)
    return parse(p.read_bytes().decode("utf-8"), file=str(p))


class _TrailingSplit(NamedTuple):
    """One value body split at its rule-5 trailing comment (or not split at
    all -- `comment_text == ""` means no trailing comment)."""

    #: value text before the comment (or the whole body, if none)
    value: str
    #: whitespace between `value` and the comment opener
    gap: str
    #: the comment's own text, opener through close (or opener tail if open)
    comment_text: str
    #: text after a CLOSED comment's `*/`; "" when the comment is open
    post: str
    #: True when the comment has no `*/` on this line and opens a
    #: multi-line block comment (rule 5, DL-161)
    opens_comment: bool


def _split_trailing_comment(body: str, *, in_quote: bool = False) -> _TrailingSplit:
    """Split a value body into (value, gap, comment_text, post, open).

    comment_text == "" means no trailing comment. Only `/* ... */` block
    comments can trail a value; a mid-line `#` is VALUE text (amended
    2026-07-10, DL-31: Broadcom's syntax rules define `#` comments in the
    first column only, and `#` is a legal name/value character -- stripping
    a whitespace-preceded `#`-tail silently changed the value vs. the
    engine's parse). Hash comments are full-line only (scan loop). Markers
    are recognized only outside double quotes and only at the value start
    or after whitespace (rule 5 + the block-comment decisions in the module
    docstring).

    A marker whose first `*/` ends the line is a closed trailing comment
    (open=False; post holds the run after `*/`). A marker with NO `*/` after
    it on the line OPENS a multi-line comment (rule 5, DL-161): comment_text
    holds the opener tail and open=True -- the caller consumes the body lines
    and the close atomically, so no body line ever reaches the scan loop. A
    closed `/*...*/` with value text after it stays in the value as opaque
    text, and a quoted or glued marker opens nothing. `in_quote` seeds the
    quote walk for a rule-6 continuation line (DL-160 seeding, DL-161): a
    quote opened on an earlier line of the joined value still shadows here.
    This one quote-aware walk hands the caller both the open state and the
    pre-opener value prefix, so the rule-4/4b mask only ever sees value
    bytes, never an open comment tail.
    """
    in_q = in_quote
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == '"':
            in_q = not in_q
        elif not in_q and body.startswith("/*", i) and (i == 0 or body[i - 1] in " \t"):
            close = body.find("*/", i + 2)
            value_ws = body[:i]
            value = value_ws.rstrip(" \t")
            gap = value_ws[len(value) :]
            if close == -1:
                # Rule 5 (DL-161, majority rule): no `*/` on the line -- the
                # marker opens a block comment that spans lines. Before this
                # amendment the marker stayed value text and the body lines
                # scanned as attributes: silent corruption.
                return _TrailingSplit(value, gap, body[i:], "", True)
            after = body[close + 2 :]
            if after.strip() == "":
                return _TrailingSplit(value, gap, body[i : close + 2], after, False)
            i = close + 2  # closed block with value text after it: opaque, keep scanning
            continue
        i += 1
    return _TrailingSplit(body, "", "", "", False)


def value_opens_comment(value: str) -> bool:
    """True when the bare spelling of `value` would OPEN a rule-5 block
    comment (a whitespace-preceded or value-start unquoted `/*` with no `*/`
    after it on the line, DL-161). JIL-emitting paths call this to re-escape:
    such a value must be quoted or it swallows the lines that follow it."""
    return _split_trailing_comment(value).opens_comment


def _find_inline_pair(value: str, *, in_quote: bool = False) -> re.Match[str] | None:
    """First quote-unshadowed `key:` pair after whitespace (one-line form).

    `in_quote` seeds the parity walk for a rule-6 continuation line: a quote
    opened earlier in the joined value still shadows here (rule 4b, DL-160).
    """
    parity = 1 if in_quote else 0
    for m in _INLINE_RE.finditer(value):
        if (parity + value.count('"', 0, m.start())) % 2 == 0:
            return m
    return None


#: Mask filler for `_mask_closed_blocks`. NOT a space: the fill must not be a
#: key character, a quote, or whitespace, because rule 4b reads a pair only
#: where the `key:` token is whitespace-preceded. A space fill invented that
#: whitespace for a token glued to the closing `*/` (`command: a /* c */b: x`
#: was refused as a second pair, against rule 4b's own wording).
_MASK_FILL = "*"


def _mask_closed_blocks(value: str, *, in_quote: bool = False) -> str:
    """Mask closed `/*...*/` spans (char-for-char, offsets preserved)
    before the rule-4/4b pair scan: rule 5 keeps a closed inline block comment
    with trailing text as opaque VALUE text, so a `key:` shape inside one is
    comment prose, not a second attribute pair.

    A span opens exactly where rule 5 opens a comment, and the walk is the one
    `_split_trailing_comment` makes: the marker is unquoted and sits at the
    value start or after whitespace. A quote-blind mask blanked the span of
    `"/* " */ key: value` over the CLOSING quote, `_find_inline_pair` then read
    the parity as odd, the rule-4b guard never fired, and the second pair folded
    into `raw_value` -- the DL-30 silent-loss class the guard exists to stop
    (DL-151). A quote inside a masked span toggles nothing, again as in
    `_split_trailing_comment`: it is comment prose, not value text.
    `in_quote` seeds the walk for a rule-6 continuation line whose joined
    value holds an open quote (rule 4b, DL-160).
    """
    out = list(value)
    in_q = in_quote
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == '"':
            in_q = not in_q
        elif not in_q and value.startswith("/*", i) and (i == 0 or value[i - 1] in " \t"):
            close = value.find("*/", i + 2)
            if close == -1:
                # Reachable only on a rule-6 continuation line: an attribute
                # or subcommand value cannot keep an unclosed whitespace-
                # preceded marker (rule 5 opens a comment there, DL-161), but
                # rule 6 carries a continuation line verbatim, where the
                # marker stays value text.
                break
            out[i : close + 2] = [_MASK_FILL] * (close + 2 - i)
            i = close + 2
            continue
        i += 1
    return "".join(out)


class _Scanner:
    def __init__(self, text: str, file: str) -> None:
        # PR-10a: an unpaired surrogate is a legal Python str and never a
        # Unicode scalar string, so it has no UTF-8 spelling -- the span
        # arithmetic below would raise UnicodeEncodeError on it, and an estate
        # holding one could never be sealed (period-model ss3.2). Refused here,
        # where the text enters, with the line it is on.
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
                value, gap, ctext, cpost, copen = _split_trailing_comment(rest[len(sep) :])
                span = self._span(i, i)
                k = i
                if copen:
                    # Rule 5 (DL-161): the trailing marker opens a multi-line
                    # comment. Its body lines are consumed HERE, by the same
                    # walk a full-line comment uses, so the scan loop never
                    # sees them: the rule-6 continuation branch and its
                    # seeded 4b detector (DL-160) run only on true value
                    # lines. Open at EOF is the loud `unterminated block
                    # comment` error at the opener line.
                    tc, k = self._scan_block_comment(i, gap, ctext, [])
                    tc.attachment = "trailing"
                    tc.trailing_block = True
                    trailing: Comment | None = tc
                else:
                    trailing = (
                        Comment(
                            text=ctext, span=span, attachment="trailing", indent=gap, post=cpost
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
                        # Rule 11 (DL-36): the export format puts every
                        # attribute before the date rows; re-rendering an
                        # interleaved shape would silently reorder it.
                        raise JilParseError(
                            f"attribute line {key!r} after the date rows of"
                            f" {cur.subcommand}: {cur.subject.strip()!r} (rule 11, DL-36)",
                            self.file,
                            i + 1,
                        )
                    masked = _mask_closed_blocks(value)
                    if (pair := _find_inline_pair(masked)) is not None:
                        # Rule 4b (DL-30): JIL permits several `attr: value`
                        # statements on one line; swallowing the second pair
                        # into the first value would be silent loss the
                        # DL-07 firewall can never see. Valid JIL escapes or
                        # quotes value colons, so this costs nothing on
                        # valid input (the DL-18 argument).
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
                    )
                    cur.attrs.append(attr)
                    if k > i:
                        # The attr owns its multi-line trailing comment's
                        # source lines (DL-161).
                        self._extend_span(attr.span, k)
                    self._extend_span(cur.span, k)
                    # A multi-line trailing comment CLOSES the continuation
                    # (rule 6: a comment line closes it; the body lines are
                    # comment lines). A closed single-line trailing comment
                    # leaves it armed, as before (DL-161).
                    cont = attr if key.lower() in CONTINUATION_ATTRS and not copen else None
                    cont_quote = masked.count('"') % 2 == 1
                i = k + 1
                continue
            if cont is not None and not pend_c and not pend_b:
                # Rule 6: non-key-shaped line continues the open list-valued
                # attr. Rule 4b covers the joined value (DL-160), with the
                # quote parity seeded from the lines above: a quote opened on
                # the attribute line and closed here is one quoted span, not a
                # bare pair. A hit is the DL-30 loss class -- a run_calendar
                # continuation in a calendar-free set reached the backend with
                # no downstream check at all. Rule 11 date rows stay out: rule
                # 11 does not validate the row shape (the DL-36 comment above
                # _DATE_BODY_SUBCOMMANDS).
                # Rule 6 amended (DL-161): the line CAN open a block comment,
                # with the same seeded quote state -- otherwise the rule-5
                # opener would leave the continuation door open to the same
                # silent corruption. Only the pre-opener prefix is value: the
                # 4b detector reads it alone, the comment body is consumed
                # atomically, and the comment closes the continuation (rule
                # 6: a comment line closes it).
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
                # Rule 11 (DL-36): a bare date row of a standard-calendar
                # export. Verbatim carry, no comment extraction, must be
                # contiguous with its statement.
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
        # Rule 4 runs the rule-4b detector, so it masks closed blocks the same
        # way (DL-151): `insert_job: j /* see owner: bob */ tail` is opaque
        # value text per rule 5, not an inline `owner` pair. The fill is
        # char-for-char and never whitespace, so the offsets still index
        # `value` and `m.group(1)` is real whitespace -- a space fill made
        # `insert_job: j /* c */ job_type: c` render the comment as blanks.
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
            # Reuse the line mask instead of re-masking `jt`: a fresh mask
            # would re-anchor rule 5's "value start" to the inline value and
            # open a span the line-level walk refuses, quietly swallowing the
            # pair in `job_type:/* key: y */ z`.
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
    """The rule-5 trailing pair riding one value: (inline rider, block
    rider). A closed (single-line) trailing comment rides the first value
    line; a multi-line trailing comment (rule 5 opener, DL-161) rides the
    last. Read straight off `Comment.trailing_block`, which the scanner
    stamped at scan time -- not re-derived from `"\\n" in c.text` (F4)."""
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
        # A closed (single-line) trailing comment rides its own line: the
        # first value line. A multi-line trailing comment (rule 5 opener,
        # DL-161) rides the LAST value line -- it either opened on the
        # attribute line (then the continuation closed, so first == last) or
        # on the continuation line that opened it. Its body lines are emitted
        # as their own `out` entries so the `nl.join` below never sees an
        # embedded '\n' (CRLF fidelity).
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
    """Preserve-render one statement as a standalone block: the statement's
    own bytes (comments, layout trivia, date rows) minus the blank lines that
    separated it from its neighbours. The ss10 `spec` query serves this --
    operators read the block that was actually loaded, post-placeholder."""
    solo = stmt.model_copy(update={"pre_blank_lines": []})
    # lstrip: a leading comment carries its own pre_blank_lines trivia
    return render_preserve(JilFile(statements=[solo], file=stmt.span.file)).lstrip("\n")


#: Fixed canonical attribute order (ir-design ss2: "subcommand first, then a
#: fixed key order, unknown keys alphabetically last"). The exact order within
#: the known set is implementation-defined; keep it stable -- canonical output
#: is a diff/storage format. Duplicate keys keep their source order.
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
    """Purely lexical canonical form (F2 fixpoint): stable attribute order,
    single space after ':', one-line form split into a regular job_type attr,
    trivia (indents, blank lines, trailing whitespace) dropped, statements
    separated by one blank line, '\\n' endings. Abbreviation expansion is NOT
    done here -- that is IR-level (ir-design ss2)."""
    blocks: list[str] = []
    for stmt in jf.statements:
        lines: list[str] = []
        _emit_canonical_comments(lines, stmt.comments)
        subject = stmt.subject.rstrip()
        has_trailing = any(c.attachment == "trailing" for c in stmt.comments)
        if stmt.job_type_inline is not None and not has_trailing:
            # The inline `job_type` pair was split off the subject at parse
            # time (rule 4), so a closed block comment that sat between the
            # subject and `job_type:` rides inside `stmt.subject` verbatim,
            # not as a separate trailing Comment. Once `job_type` moves to
            # its own attr line below, that comment is line-final here -- a
            # second parse re-extracts it as a real trailing comment and
            # normalizes its gap to one space (`_canonical_trailing`). A
            # verbatim dump of `subject` keeps the original gap, so the
            # fixpoint breaks (F2, DL-159). Re-split here the same way the
            # second parse would, so the first canonical pass already
            # matches it.
            value, _gap, ctext, _post, copen = _split_trailing_comment(stmt.subject)
            if ctext and not copen:
                # A parsed subject can no longer hold an OPEN marker (rule 5
                # takes it as a comment opener at scan time, DL-161); the
                # guard keeps a hand-built AST verbatim.
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
            vlines = [ln.rstrip() for ln in a.raw_value.split("\n")]
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
    """The value lines plus their trailing comments: the gap normalizes to
    one space and every comment line is right-stripped. A closed trailing
    comment rides the first value line; a multi-line trailing comment (rule 5
    opener, DL-161) rides the last, its body lines emitted as their own lines
    -- the same placement the preserve renderer uses."""
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
