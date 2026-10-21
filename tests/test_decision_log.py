"""The decision log's ceilings (DL-379)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG = REPO_ROOT / "docs" / "decision-log.md"

#: DL-379: the entry that set the ceilings; every entry after it in the file is under them.
POLICY = (379, "")
ENTRY_LINES = 40
ENTRY_LINES_WIDE = 60
COLUMNS = 100
#: DL-379: the entries granted the wide ceiling, by full identifier (a lettered entry needs its
#: own), each for changing the engine or freezing a rule of docs/; an entry joins this set in the
#: PR that lands it.
WIDE: frozenset[str] = frozenset({"DL-380"})
#: DL-379: the lines of the log up to the end of DL-379. A compression tranche lowers this in
#: the same PR; only a superseded-by mark raises it, by the lines it adds.
PREFIX_LINES = 23700

_ENTRY_START = re.compile(r"^- DL-(\d+)([a-z]?) ")


def _entries() -> list[tuple[int, str, list[str]]]:
    """Each entry in file order: its number, its letter and its lines, the first line to the
    line before the next entry (DL-14a is an entry of its own)."""
    entries: list[tuple[int, str, list[str]]] = []
    for line in LOG.read_text(encoding="utf-8").splitlines():
        match = _ENTRY_START.match(line)
        if match:
            entries.append((int(match.group(1)), match.group(2), [line]))
        elif entries:
            entries[-1][2].append(line)
    return entries


def _split() -> tuple[int, list[tuple[int, str, list[str]]]]:
    """The lines up to the end of the POLICY entry, and every entry after it in file order (an
    amendment appended later is among them)."""
    text = LOG.read_text(encoding="utf-8").splitlines()
    entries = _entries()
    policy = next(i for i, entry in enumerate(entries) if entry[:2] == POLICY)
    through = sum(len(lines) for _, _, lines in entries[: policy + 1])
    preamble = next(i for i, line in enumerate(text) if _ENTRY_START.match(line))
    return preamble + through, entries[policy + 1 :]


def _ceiled() -> list[tuple[int, str, list[str]]]:
    return _split()[1]


def test_a_new_entry_stays_under_its_line_ceiling() -> None:
    """DL-379: an entry after DL-379 has at most ENTRY_LINES lines, or ENTRY_LINES_WIDE when
    the WIDE set names it."""
    over = []
    for number, letter, lines in _ceiled():
        ceiling = ENTRY_LINES_WIDE if f"DL-{number}{letter}" in WIDE else ENTRY_LINES
        if len(lines) > ceiling:
            over.append(f"DL-{number}{letter}: {len(lines)} lines, ceiling {ceiling}")
    assert not over, over


def test_a_new_entry_keeps_to_the_column_limit() -> None:
    """DL-379: every line of an entry after DL-379 is at most COLUMNS wide."""
    wide = [
        f"DL-{number}{letter}: line {index} is {len(line)} columns"
        for number, letter, lines in _ceiled()
        for index, line in enumerate(lines, start=1)
        if len(line) > COLUMNS
    ]
    assert not wide, wide


def test_the_text_before_the_ceiled_entries_never_grows() -> None:
    """DL-379: the text up to the end of DL-379 has at most PREFIX_LINES lines, so the room a
    compression tranche frees is not refilled; a tranche lowers the constant, and only a
    superseded-by mark raises it."""
    before, _ = _split()
    assert before <= PREFIX_LINES, f"{before} lines, ceiling {PREFIX_LINES}"
