"""JIL statement-level AST: hand scanner + preserve/canonical renderers.

Normative spec: docs/jil-statement-syntax.md (tokenization rules 1-9, fidelity
tests F1-F4) and docs/ir-design.md ss2 (model sketches, loss policy). No
interpretation happens at this layer: `condition` is a RawAttr like any other;
expression parsing is lowering's job.

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
- Trailing block comments: the leftmost whitespace-preceded, unquoted `/*`
  whose first following `*/` ends the line starts the comment. A `/*` that
  never closes on the line (e.g. a shell glob after a space) stays in the
  value; a closed `/*...*/` with value text after it is kept in the value as
  opaque text.
- A full-line block comment must close at end of line; non-whitespace content
  after `*/` on the closing line is an error.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

#: Statement boundaries per jil-statement-syntax.md rule 3, recognized
#: case-insensitively. Unknown keys are attributes, never boundaries
#: (forward compatibility).
SUBCOMMANDS = frozenset(
    {
        "insert_job",
        "update_job",
        "delete_job",
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
    }
)

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
    """Scan JIL text into a byte-faithful AST (rules 1-9 of the scanner spec)."""
    return _Scanner(text, file).scan()


def parse_file(path: str | Path) -> JilFile:
    """Parse a file, bypassing universal-newline translation (fidelity!)."""
    p = Path(path)
    return parse(p.read_bytes().decode("utf-8"), file=str(p))


def _split_trailing_comment(body: str) -> tuple[str, str, str, str]:
    """Split a value body into (value, gap, comment_text, post).

    comment_text == "" means no trailing comment. Markers are recognized only
    outside double quotes and only at the value start or after whitespace
    (rule 5 + the block-comment decisions in the module docstring).
    """
    in_q = False
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == '"':
            in_q = not in_q
        elif not in_q and ch == "#" and (i == 0 or body[i - 1] in " \t"):
            value_ws = body[:i]
            value = value_ws.rstrip(" \t")
            return value, value_ws[len(value) :], body[i:], ""
        elif not in_q and body.startswith("/*", i) and (i == 0 or body[i - 1] in " \t"):
            close = body.find("*/", i + 2)
            if close == -1:
                i += 1  # never closes on this line (e.g. a glob): stays in the value
                continue
            after = body[close + 2 :]
            if after.strip() == "":
                value_ws = body[:i]
                value = value_ws.rstrip(" \t")
                return value, value_ws[len(value) :], body[i : close + 2], after
            i = close + 2  # closed block with value text after it: opaque, keep scanning
            continue
        i += 1
    return body, "", "", ""


def _find_inline_pair(value: str) -> re.Match[str] | None:
    """First quote-unshadowed `key:` pair after whitespace (one-line form)."""
    for m in _INLINE_RE.finditer(value):
        if value.count('"', 0, m.start()) % 2 == 0:
            return m
    return None


class _Scanner:
    def __init__(self, text: str, file: str) -> None:
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
                value, gap, ctext, cpost = _split_trailing_comment(rest[len(sep) :])
                span = self._span(i, i)
                trailing = (
                    Comment(text=ctext, span=span, attachment="trailing", indent=gap, post=cpost)
                    if ctext
                    else None
                )
                comments, blanks, pend_c, pend_b = pend_c, pend_b, [], []
                if key.lower() in SUBCOMMANDS:
                    cur = self._make_statement(
                        key, value, indent, sep, span, comments, blanks, trailing, i
                    )
                    stmts.append(cur)
                    cont = None
                else:
                    if cur is None:
                        raise JilParseError(
                            f"attribute line {key!r} before any statement", self.file, i + 1
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
                    self._extend_span(cur.span, i)
                    cont = attr if key.lower() in CONTINUATION_ATTRS else None
                i += 1
                continue
            if cont is not None and not pend_c and not pend_b:
                # Rule 6: non-key-shaped line continues the open list-valued attr.
                cont.raw_value += "\n" + line
                self._extend_span(cont.span, i)
                assert cur is not None
                self._extend_span(cur.span, i)
                i += 1
                continue
            raise JilParseError(
                "unrecognized line (not an attribute, comment, blank, or continuation)",
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
        m = _find_inline_pair(value)
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
            if _find_inline_pair(jt) is not None:
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


def render_preserve(jf: JilFile) -> str:
    """Byte-exact reconstruction of the source (F1: render(parse(x)) == x)."""
    out: list[str] = []

    def emit_full_line(c: Comment) -> None:
        out.extend(c.pre_blank_lines)
        parts = c.text.split("\n")
        parts[0] = c.indent + parts[0]
        parts[-1] = parts[-1] + c.post
        out.extend(parts)

    def trailing_suffix(comments: list[Comment]) -> str:
        for c in comments:
            if c.attachment == "trailing":
                return c.indent + c.text + c.post
        return ""

    for stmt in jf.statements:
        for c in stmt.comments:
            if c.attachment != "trailing":
                emit_full_line(c)
        out.extend(stmt.pre_blank_lines)
        header = stmt.indent + stmt.subcommand + ":" + stmt.sep + stmt.subject
        if stmt.job_type_inline is not None:
            header += stmt.inline_gap + stmt.inline_key + ":" + stmt.inline_sep
            header += stmt.job_type_inline
        out.append(header + trailing_suffix(stmt.comments))
        for a in stmt.attrs:
            for c in a.comments:
                if c.attachment != "trailing":
                    emit_full_line(c)
            out.extend(a.pre_blank_lines)
            vlines = a.raw_value.split("\n")
            out.append(a.indent + a.key + ":" + a.sep + vlines[0] + trailing_suffix(a.comments))
            out.extend(vlines[1:])
    for c in jf.trailing_comments:
        emit_full_line(c)
    out.extend(jf.eof_blank_lines)
    if not out:
        return ""
    nl = jf.newline_style
    return nl.join(out) + (nl if jf.final_newline else "")


#: Fixed canonical attribute order (ir-design ss2: "subcommand first, then a
#: fixed key order, unknown keys alphabetically last"). The exact order within
#: the known set is implementation-defined; keep it stable -- canonical output
#: is a diff/storage format. Duplicate keys keep their source order.
_CANONICAL_KEY_ORDER: tuple[str, ...] = (
    "job_type",
    "box_name",
    "command",
    "machine",
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
    "term_run_time",
    "n_retrys",
    "auto_hold",
    "auto_delete",
    "watch_file",
    "watch_interval",
    "watch_file_min_size",
    "job_load",
    "priority",
    "profile",
    "std_out_file",
    "std_err_file",
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
        header = f"{stmt.subcommand}: {subject}" if subject else f"{stmt.subcommand}:"
        lines.append(header + _canonical_trailing(stmt.comments))
        attrs = list(stmt.attrs)
        if stmt.job_type_inline is not None:
            attrs.insert(
                0, RawAttr(key=stmt.inline_key, raw_value=stmt.job_type_inline, span=stmt.span)
            )
        for a in _canonical_sort(attrs):
            _emit_canonical_comments(lines, a.comments)
            vlines = [ln.rstrip() for ln in a.raw_value.split("\n")]
            first = f"{a.key}: {vlines[0]}" if vlines[0] else f"{a.key}:"
            lines.append(first + _canonical_trailing(a.comments))
            lines.extend(vlines[1:])
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


def _canonical_trailing(comments: list[Comment]) -> str:
    for c in comments:
        if c.attachment == "trailing":
            return " " + c.text.rstrip()
    return ""


def _canonical_sort(attrs: list[RawAttr]) -> list[RawAttr]:
    def sort_key(pair: tuple[int, RawAttr]) -> tuple[int, str, int]:
        idx, attr = pair
        kl = attr.key.lower()
        rank = _ORDER_INDEX.get(kl, _UNKNOWN_RANK)
        return (rank, kl if rank == _UNKNOWN_RANK else "", idx)

    return [a for _, a in sorted(enumerate(attrs), key=sort_key)]
