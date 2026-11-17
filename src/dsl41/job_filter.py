"""Read the jobs filter line into a predicate or a one-line refusal (runner-design §11, DL-511).

The text is words separated by spaces, and a job is shown when every word matches it. A word is
read by its first character; letter case is ignored. A refusal names the first refused word.
The reader imports no UI toolkit, so the rule is tested without one.
"""

from __future__ import annotations

import fnmatch
import re
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import get_args

from dsl41.oracle_state import JobStatus

#: one word's test: job name, status, and the name of the job's own box (None for no box)
type WordTest = Callable[[str, str, str | None], bool]

#: the job statuses in the job chart's order; a status value names one of them by its start
_STATUSES: tuple[str, ...] = get_args(JobStatus)
_FIELDS = ("status", "box")
_FIELD_MARK = re.compile(r"[=@]")


@dataclass(frozen=True)
class JobFilter:
    """A good filter line: one test per word. No words match every job."""

    tests: tuple[WordTest, ...] = ()

    def matches(self, name: str, status: str, box: str | None) -> bool:
        return all(test(name, status, box) for test in self.tests)


def read_filter(text: str) -> JobFilter | str:
    """The filter that `text` states, or the refusal of its first refused word (DL-511)."""
    tests: list[WordTest] = []
    for word in text.split():
        test = _read_word(word)
        if isinstance(test, str):
            return test
        tests.append(test)
    return JobFilter(tuple(tests))


def _read_word(word: str) -> WordTest | str:
    """A leading `\\` makes the rest text; a leading `!` negates the word that follows."""
    if not word.startswith("!"):
        return _read_body(word, word, negated=False)
    if len(word) == 1:
        return "!: nothing to negate"
    test = _read_body(word, word[1:], negated=True)
    if isinstance(test, str):
        return test
    return _negate(test)


def _read_body(word: str, body: str, *, negated: bool) -> WordTest | str:
    """One word without its negation; `word` is the whole word, which every refusal names."""
    marker, rest = body[0], body[1:]
    if marker == "\\":
        if not rest:
            return f"{word}: empty text"
        return _contains(rest.lower())
    if marker == "!":
        return f"{word}: a word takes one negation"
    if marker == "=":
        return _name_globs(word, [rest], listed=False)
    if marker == "@":
        return _name_globs(word, rest.split(","), listed=True)
    if marker == "~":
        return _regex(word, rest)
    cut = _FIELD_MARK.search(body)
    if cut is not None:
        return _field(word, body, cut.start(), negated=negated)
    return _name_glob(f"*{body.lower()}*")


def _field(word: str, body: str, at: int, *, negated: bool) -> WordTest | str:
    """`status` or `box`, then `=`, `@`, `!=` or `!@`; `!=` and `!@` negate."""
    field, op, value = body[:at], body[at], body[at + 1 :]
    negate = field.endswith("!")
    if negate:
        field = field[:-1]
    if field.lower() not in _FIELDS:
        escaped = word[: len(word) - len(body)] + "\\" + body
        return f"{word}: the fields are status and box; write {escaped} for the text"
    if negate and negated:
        return f"{word}: a word takes one negation"
    is_status = field.lower() == "status"
    if op == "=" and is_status and "," in value:
        # the list hint is given only for a list that is itself good
        if not all(value.split(",")):
            return f"{word}: empty value"
        listed = word[: len(word) - len(body) + at] + "@" + value
        return f"{word}: one status after =; write {listed} for a list"
    values = [value] if op == "=" else value.split(",")
    empty = _empty(word, values, listed=op == "@", what="value" if is_status else "pattern")
    if empty is not None:
        return empty
    test = _status_test(word, values) if is_status else _box_test([v.lower() for v in values])
    if isinstance(test, str) or not negate:
        return test
    return _negate(test)


def _status_test(word: str, values: Sequence[str]) -> WordTest | str:
    wanted: set[str] = set()
    for value in values:
        found = [status for status in _STATUSES if status.startswith(value.upper())]
        if not found:
            return f"{word}: {value} starts no status ({', '.join(_STATUSES)})"
        if len(found) > 1:
            return f"{word}: {value} could be {' or '.join(found)}"
        wanted.add(found[0])

    def test(_name: str, status: str, _box: str | None) -> bool:
        return status in wanted

    return test


def _box_test(patterns: Sequence[str]) -> WordTest:
    """A job in no box matches no pattern, so a negated box test shows it."""

    def test(_name: str, _status: str, box: str | None) -> bool:
        return box is not None and any(fnmatch.fnmatchcase(box.lower(), p) for p in patterns)

    return test


def _name_globs(word: str, patterns: Sequence[str], *, listed: bool) -> WordTest | str:
    """The whole name matches one of the patterns."""
    empty = _empty(word, patterns, listed=listed, what="pattern")
    if empty is not None:
        return empty
    lowered = [pattern.lower() for pattern in patterns]

    def test(name: str, _status: str, _box: str | None) -> bool:
        return any(fnmatch.fnmatchcase(name.lower(), pattern) for pattern in lowered)

    return test


def _name_glob(pattern: str) -> WordTest:
    def test(name: str, _status: str, _box: str | None) -> bool:
        return fnmatch.fnmatchcase(name.lower(), pattern)

    return test


def _contains(text: str) -> WordTest:
    def test(name: str, _status: str, _box: str | None) -> bool:
        return text in name.lower()

    return test


def _regex(word: str, pattern: str) -> WordTest | str:
    """The pattern keeps its case and the search ignores it. Overflow and recursion are the
    library's other faults on a pattern it cannot compile. The library's FutureWarning on a
    possible set operation (`[[`, `&&`, `--`) is ignored: the line shows refusals, not warnings."""
    if not pattern:
        return f"{word}: empty pattern"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            compiled = re.compile(pattern, re.IGNORECASE)
    except (re.error, OverflowError, RecursionError) as exc:
        return f"{word}: bad regular expression: {exc}"

    def test(name: str, _status: str, _box: str | None) -> bool:
        return compiled.search(name) is not None

    return test


def _empty(word: str, items: Sequence[str], *, listed: bool, what: str) -> str | None:
    if all(items):
        return None
    if listed and any(items):
        return f"{word}: empty item in the list"
    return f"{word}: empty {what}"


def _negate(test: WordTest) -> WordTest:
    def negated(name: str, status: str, box: str | None) -> bool:
        return not test(name, status, box)

    return negated
