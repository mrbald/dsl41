"""Condition-expression parsing: lark loader + Tree -> Cond transformer.

Phase 2 of the implementation order (CLAUDE.md / DL-03). Normative spec:
docs/ir-design.md ss3 (condition algebra models) and docs/autosys-semantics.md
SEM-02..08 (atom semantics), SEM-04 (lookback). Grammar:
grammars/condition.lark; statement-level JIL is ast_jil's hand scanner (DL-05).

The ss3 sketch fields are the semantic API. Extra `span` fields (char offsets
into the condition source string) implement ir-design ss4's "every Cond retains
a pointer to its AST SourceSpan": lowering (phase 3) composes these offsets
with the owning RawAttr's file-level SourceSpan.

Decisions pinned here (each with a test):
- GlobalAtom.value is semantic: quotes are stripped from QUOTED comparands; the
  verbatim expression text survives in the owning RawAttr.
- JobRef.name is semantic: the `\\:` escape is unescaped to `:` (job names are
  written unescaped in insert_job subjects; escaping is condition-syntax only).
- Lookback.raw keeps the token verbatim (incl. the `\\:` form) for Q2 auditing.
- Bare all-zero token ("0") -> kind="zero"; bare "9999" -> kind="indefinite"
  (legacy explicit form, SEM-04); every dotted/colon form is a window,
  including "0.00" (a zero-minute window, distinct from zero-lookback) and
  "9999.00".
- Lookback minutes part must be 00-59 (SEM-04: max is 9999.59): hard error.
- Node spans cover the node's full lexical extent including punctuation
  (lark propagates positions before token filtering).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Annotated, Literal, cast

from lark import Lark, Token, Tree
from lark.exceptions import UnexpectedInput
from pydantic import BaseModel, Field

# ---------------------------------------------------- IR-F condition algebra (ss3)

Status = Literal["SUCCESS", "FAILURE", "DONE", "TERMINATED", "NOTRUNNING"]
CmpOp = Literal["=", "!=", "<", ">", "<=", ">="]


def compare_int(left: int, op: CmpOp, right: int) -> bool:
    """Compare two integers under one of the six CmpOp operators."""
    return {
        "=": left == right,
        "!=": left != right,
        "<": left < right,
        ">": left > right,
        "<=": left <= right,
        ">=": left >= right,
    }[op]


def compare_value(left: str, op: CmpOp, right: str) -> bool:
    """Compare two comparand strings: numeric comparison if both sides parse
    as int, else string comparison (= and != always available; ordering
    falls back to lexicographic). Shared by oracle, equiv, and the UC oracle
    twin -- see each call site's SEM/UCS-adjacent comment for why identical
    behavior is required there."""
    try:
        return compare_int(int(left), op, int(right))
    except ValueError:
        if op == "=":
            return left == right
        if op == "!=":
            return left != right
        return {"<": left < right, ">": left > right, "<=": left <= right, ">=": left >= right}[op]


class CondSpan(BaseModel):
    """Char-offset span into the condition source string (see module docstring)."""

    start: int  # inclusive
    end: int  # exclusive


class Lookback(BaseModel):
    kind: Literal["window", "zero", "indefinite"]  # SEM-04
    minutes: int | None  # for kind=window; parsed from hhhh.mm / hhhh\:mm
    raw: str  # original token, for round-trip + Q2 auditing


class JobRef(BaseModel):
    name: str
    instance: str | None = None  # cross-instance '^INST' (SEM-07)


class StatusAtom(BaseModel):
    kind: Literal["status"] = "status"
    job: JobRef
    status: Status
    lookback: Lookback | None = None  # None == indefinite w/o explicit token
    span: CondSpan | None = None


class ExitCodeAtom(BaseModel):
    kind: Literal["exitcode"] = "exitcode"
    job: JobRef
    op: CmpOp
    value: int
    lookback: Lookback | None = None
    span: CondSpan | None = None


class GlobalAtom(BaseModel):
    kind: Literal["global"] = "global"
    name: str
    op: CmpOp
    value: str  # lookback FORBIDDEN here (SEM-04) -- lexically excluded by the grammar
    span: CondSpan | None = None


class And(BaseModel):
    kind: Literal["and"] = "and"
    operands: list["Cond"]  # n-ary, flattened
    span: CondSpan | None = None


class Or(BaseModel):
    kind: Literal["or"] = "or"
    operands: list["Cond"]
    span: CondSpan | None = None


class Paren(BaseModel):
    kind: Literal["paren"] = "paren"
    inner: "Cond"  # fidelity only; erased in canonical form
    span: CondSpan | None = None


Cond = Annotated[
    StatusAtom | ExitCodeAtom | GlobalAtom | And | Or | Paren,
    Field(discriminator="kind"),
]

And.model_rebuild()
Or.model_rebuild()
Paren.model_rebuild()


Atom = StatusAtom | ExitCodeAtom | GlobalAtom


def iter_atoms(cond: Cond) -> Iterator[Atom]:
    """Yield every atom of a Cond tree in source (left-to-right) order.

    Pure structural walk shared by analysis passes (linter rules L001/L015,
    derive's atom extraction); Paren and And/Or nesting is transparent.
    """
    stack: list[Cond] = [cond]
    while stack:
        node = stack.pop()
        if isinstance(node, (And, Or)):
            stack.extend(reversed(node.operands))
        elif isinstance(node, Paren):
            stack.append(node.inner)
        else:
            yield node


class ConditionParseError(ValueError):
    """Loud condition failure: lexical/syntactic, or SEM-04 token validation."""

    def __init__(self, message: str, *, text: str | None = None, pos: int | None = None) -> None:
        loc = f" at position {pos}" if pos is not None else ""
        super().__init__(f"{message}{loc}")
        self.text = text
        self.pos = pos


# ------------------------------------------------------- lookback tokens (SEM-04)

#: Lexical shape mirrors LOOKBACK_TOKEN in condition.lark; semantic range
#: checks (mm 00-59) happen here, not in the grammar.
_LOOKBACK_RE = re.compile(r"(\d{1,4})(?:\.(\d{1,2})|\\:(\d{1,2}))?\Z")


def parse_lookback(raw: str, *, pos: int | None = None) -> Lookback:
    """Validate + classify a lookback token per SEM-04.

    hhhh.mm / hhhh\\:mm -> window; bare N -> N-hour window; bare 0 -> zero
    lookback; bare 9999 -> explicit indefinite (legacy 4.5.1 default form).
    """
    m = _LOOKBACK_RE.match(raw)
    if m is None:
        raise ConditionParseError(
            f"malformed lookback token {raw!r} (SEM-04: hhhh.mm, hhhh\\:mm, or bare hours;"
            " '.30' style is invalid)",
            pos=pos,
        )
    hours_s, dot_mm, colon_mm = m.groups()
    mm_s = dot_mm if dot_mm is not None else colon_mm
    if mm_s is None:
        hours = int(hours_s)
        if hours == 0:
            # PENDING: Q2 -- zero-lookback anchoring; the kind carries it to the oracle.
            return Lookback(kind="zero", minutes=None, raw=raw)
        if raw == "9999":
            return Lookback(kind="indefinite", minutes=None, raw=raw)
        return Lookback(kind="window", minutes=hours * 60, raw=raw)
    minutes = int(mm_s)
    if minutes > 59:
        raise ConditionParseError(
            f"lookback minutes out of range in {raw!r} (mm must be 00-59; SEM-04 max 9999.59)",
            pos=pos,
        )
    return Lookback(kind="window", minutes=int(hours_s) * 60 + minutes, raw=raw)


def lookback_pitfalls(lb: Lookback) -> list[str]:
    """L015 raw-shape pitfalls, computed at parse time (ir-design ss9).

    Returns human-readable facts about suspicious-but-valid shapes; the linter
    (phase 4) maps them to Violations with codes/severity. Empty list == clean.
    """
    m = _LOOKBACK_RE.match(lb.raw)
    if m is None:  # hand-built Lookback with a raw the parser would reject
        return [f"unparseable lookback token {lb.raw!r}"]
    hours_s, dot_mm, colon_mm = m.groups()
    mm_s = dot_mm if dot_mm is not None else colon_mm
    pitfalls: list[str] = []
    if mm_s is None and lb.kind == "window":
        pitfalls.append(
            f"bare lookback {lb.raw!r} means {int(hours_s)} hours, not minutes"
            f" (sub-hour windows need a leading 00: '00.30' is 30 minutes; SEM-04)"
        )
    if mm_s is not None and len(mm_s) == 1:
        pitfalls.append(
            f"single-digit minutes in lookback {lb.raw!r}: canonical form is two-digit mm"
            f" ('{hours_s}.{mm_s:0>2}'); verify the intended window"
        )
    return pitfalls


# ------------------------------------------------- grammar loader (Q1 switch, DL-06)

Precedence = Literal["flat", "prec"]

_env_precedence = os.environ.get("CONDITION_PRECEDENCE", "flat")
if _env_precedence not in ("flat", "prec"):
    raise ValueError(
        f"CONDITION_PRECEDENCE env var must be 'flat' or 'prec', got {_env_precedence!r}"
    )

#: PENDING: Q1 (SEM-03) -- operator precedence of & vs | without parentheses.
#: Broadcom's wording is left-to-right with parentheses forcing precedence, so
#: "flat" is the documented default (condition.lark Q1 banner, DL-06). Both
#: grammars ship; after live verification delete the losing start rule, the
#: sentinel tests, and this switch.
CONDITION_PRECEDENCE: Precedence = cast(Precedence, _env_precedence)


def _grammar_text() -> str:
    packaged = resources.files("dsl41") / "grammars" / "condition.lark"
    if packaged.is_file():  # wheel layout (pyproject force-include)
        return packaged.read_text(encoding="utf-8")
    repo = Path(__file__).resolve().parents[2] / "grammars" / "condition.lark"
    return repo.read_text(encoding="utf-8")  # dev/editable checkout layout


@cache
def _parser(precedence: Precedence) -> Lark:
    return Lark(
        _grammar_text(),
        start=f"start_{precedence}",
        parser="lalr",
        propagate_positions=True,
    )


def parse_condition(text: str, precedence: Precedence | None = None) -> Cond:
    """Parse a condition/box_success/box_failure expression into a Cond tree.

    `precedence` defaults to the module-level CONDITION_PRECEDENCE setting.
    """
    mode = CONDITION_PRECEDENCE if precedence is None else precedence
    try:
        tree = _parser(mode).parse(text)
        return _build(cast("Tree[Token]", tree.children[0]))
    except UnexpectedInput as exc:
        first_line = str(exc).strip().splitlines()[0]
        raise ConditionParseError(
            f"invalid condition expression: {first_line}",
            text=text,
            pos=getattr(exc, "pos_in_stream", None),
        ) from exc
    except RecursionError as exc:
        # DL-20: operator chains build iteratively (any realistic length is
        # fine); only pathological grouping depth lands here. Refuse loudly
        # (exit-2 class) instead of leaking a traceback masquerading as
        # exit-1 lint findings -- downstream Cond walkers recurse per
        # nesting level, so admitting the parse would only move the crash.
        raise ConditionParseError(
            "condition grouping is nested deeper than the v1 walker budget"
            " (~1000 levels); flatten the grouping or split the condition",
            text=text,
            pos=None,
        ) from exc


# --------------------------------------------------------- Tree -> Cond transformer

_STATUS_BY_KW: dict[str, Status] = {
    "success": "SUCCESS",
    "s": "SUCCESS",
    "failure": "FAILURE",
    "f": "FAILURE",
    "done": "DONE",
    "d": "DONE",
    "terminated": "TERMINATED",
    "t": "TERMINATED",
    "notrunning": "NOTRUNNING",
    "n": "NOTRUNNING",
}


def _span(node: Tree[Token]) -> CondSpan | None:
    meta = node.meta
    if meta.empty:
        return None
    return CondSpan(start=meta.start_pos, end=meta.end_pos)


def _combine(is_and: bool, left: Cond, right: Cond, span: CondSpan | None) -> Cond:
    """Left-associative combine; flattens same-op runs into one n-ary node.

    A Paren operand is never flattened -- grouping survives for fidelity.
    """
    if is_and:
        operands = [*left.operands, right] if isinstance(left, And) else [left, right]
        return And(operands=operands, span=span)
    operands = [*left.operands, right] if isinstance(left, Or) else [left, right]
    return Or(operands=operands, span=span)


def _job_ref(tree: Tree[Token]) -> JobRef:
    name_tok = cast(Token, tree.children[0])
    instance = str(tree.children[1]) if len(tree.children) == 2 else None
    return JobRef(name=str(name_tok).replace("\\:", ":"), instance=instance)


def _lookback(tree: Tree[Token]) -> Lookback:
    tok = cast(Token, tree.children[0])
    return parse_lookback(str(tok), pos=tok.start_pos)


def _build(node: Tree[Token]) -> Cond:
    data = node.data
    children = node.children
    if data in ("binop", "or_", "and_"):
        # The LALR tree is left-leaning: one nesting level per operator. Walk
        # the left spine iteratively (DL-20: a 1000+-atom flat chain must not
        # blow the Python stack), then fold back left-to-right so the result
        # is identical to the old recursive descent -- _combine flattens
        # same-op runs into one n-ary node either way. Right-hand operands
        # and paren interiors still recurse: their depth is grouping depth,
        # bounded by the parse_condition RecursionError guard.
        spine: list[tuple[bool, Tree[Token], CondSpan | None]] = []
        current = node
        while True:
            d = current.data
            if d == "binop":  # flat mode: expr op operand
                op_tok = cast("Tree[Token]", current.children[1]).children[0]
                is_and = str(op_tok).lower() in ("&", "and")
            elif d in ("or_", "and_"):  # prec mode: expr TOKEN expr
                is_and = d == "and_"
            else:
                break
            spine.append((is_and, cast("Tree[Token]", current.children[-1]), _span(current)))
            current = cast("Tree[Token]", current.children[0])
        result = _build(current)  # leftmost operand: an atom or a paren group
        for is_and, right_node, span in reversed(spine):
            result = _combine(is_and, result, _build(right_node), span)
        return result
    if data == "paren":
        return Paren(inner=_build(cast("Tree[Token]", children[0])), span=_span(node))
    if data == "status_atom":
        lookback = _lookback(cast("Tree[Token]", children[2])) if len(children) == 3 else None
        return StatusAtom(
            job=_job_ref(cast("Tree[Token]", children[1])),
            status=_STATUS_BY_KW[str(children[0]).lower()],
            lookback=lookback,
            span=_span(node),
        )
    if data == "exitcode_atom":
        lookback = _lookback(cast("Tree[Token]", children[2])) if len(children) == 5 else None
        return ExitCodeAtom(
            job=_job_ref(cast("Tree[Token]", children[1])),
            op=cast(CmpOp, str(children[-2])),
            value=int(str(children[-1])),
            lookback=lookback,
            span=_span(node),
        )
    if data == "global_atom":
        value_tok = cast(Token, cast("Tree[Token]", children[3]).children[0])
        value = str(value_tok)
        if value_tok.type == "QUOTED":
            value = value[1:-1]  # semantic unquoting; verbatim text lives in the RawAttr
        return GlobalAtom(
            name=str(cast("Tree[Token]", children[1]).children[0]),
            op=cast(CmpOp, str(children[2])),
            value=value,
            span=_span(node),
        )
    raise AssertionError(f"unhandled condition parse node: {data!r}")
