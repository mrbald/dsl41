"""The sizes of the TUI's three splits and their limits (runner-design §11, DL-520).

The table column against the side column and the explain pane against the log pane are each a
ratio; the console is a number of rows. A ratio is placed on the cells there are at the moment,
so a change of the terminal's size keeps it. Every way of resizing goes through the same limits:
no pane narrower than 20 columns, the explain pane at least 3 rows, the log pane at least 5, the
console box at least 6 and the area above it at least 8. A span too small for both limits of a split
keeps the present layout: the ratio or the rows are placed as they are, within the span, and
nothing moves them.
The module imports no UI toolkit, so the arithmetic is tested without one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

#: the narrowest a pane may be, in columns (DL-520)
MIN_COLUMNS = 20
#: the lowest the explain pane, the log pane, the console box and the area above it may be, in
#: rows; the console box's 6 show one line of events above the 3-row command line
MIN_EXPLAIN_ROWS = 3
MIN_LOG_ROWS = 5
MIN_CONSOLE_ROWS = 6
MIN_ABOVE_ROWS = 8


class Split(StrEnum):
    """One split: what its size measures."""

    TABLE = "table"  # the table column's share of the width
    LOG = "log"  # the log pane's share of the side column's height
    CONSOLE = "console"  # the console's rows


@dataclass(frozen=True)
class Sizes:
    """The sizes a person set: two ratios and the console's rows. The defaults are the layout
    before any resize: three fifths for the table and for the log, and 11 rows of console."""

    table: float = 0.6
    log: float = 0.6
    console: int = 11


@dataclass(frozen=True)
class Placement:
    """Sizes placed on a screen: the table column's columns, the log pane's rows and the
    console's rows."""

    table: int
    log: int
    console: int

    def of(self, split: Split) -> int:
        """The cells of one split's measured part."""
        if split is Split.TABLE:
            return self.table
        return self.log if split is Split.LOG else self.console


def _fits(total: int, low: int, high: int) -> bool:
    return total >= low + high


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def _cells(ratio: float, total: int) -> int:
    return int(ratio * total + 0.5)


def _place_ratio(ratio: float, total: int, low: int, high: int) -> int:
    if not _fits(total, low, high):
        return int(_clamp(_cells(ratio, total), 0, max(total, 0)))
    return int(_clamp(_cells(ratio, total), low, total - high))


def _place_rows(rows: int, total: int, low: int, high: int) -> int:
    if not _fits(total, low, high):
        return int(_clamp(rows, 0, max(total, 0)))
    return int(_clamp(rows, low, total - high))


def place(sizes: Sizes, width: int, height: int) -> Placement:
    """Place `sizes` on `width` columns and `height` rows, the rows of the area above the
    console and the console together. The log pane's span is what the console leaves."""
    console = _place_rows(sizes.console, height, MIN_CONSOLE_ROWS, MIN_ABOVE_ROWS)
    return Placement(
        table=_place_ratio(sizes.table, width, MIN_COLUMNS, MIN_COLUMNS),
        log=_place_ratio(sizes.log, height - console, MIN_LOG_ROWS, MIN_EXPLAIN_ROWS),
        console=console,
    )


def _span(split: Split, sizes: Sizes, width: int, height: int) -> tuple[int, int, int]:
    """The cells a split divides, and the least the measured part and the other part take."""
    if split is Split.TABLE:
        return width, MIN_COLUMNS, MIN_COLUMNS
    if split is Split.LOG:
        console = place(sizes, width, height).console
        return height - console, MIN_LOG_ROWS, MIN_EXPLAIN_ROWS
    return height, MIN_CONSOLE_ROWS, MIN_ABOVE_ROWS


def _with_ratio(sizes: Sizes, split: Split, ratio: float) -> Sizes:
    return replace(sizes, table=ratio) if split is Split.TABLE else replace(sizes, log=ratio)


def set_cells(sizes: Sizes, split: Split, cells: int, width: int, height: int) -> Sizes:
    """Set a split to `cells`, within the limits, as a drag does. A span too small for the
    limits, or cells that place where the split already is, change nothing."""
    total, low, high = _span(split, sizes, width, height)
    if not _fits(total, low, high):
        return sizes
    cells = int(_clamp(cells, low, total - high))
    if cells == place(sizes, width, height).of(split):
        return sizes
    if split is Split.CONSOLE:
        return replace(sizes, console=cells)
    return _with_ratio(sizes, split, cells / total)


def step(sizes: Sizes, split: Split, delta: float, width: int, height: int) -> Sizes:
    """Move a split by `delta`, from where it is placed, within the limits: points of the ratio
    for the table and the log, rows for the console. A span too small for the limits changes
    nothing."""
    total, low, high = _span(split, sizes, width, height)
    if not _fits(total, low, high):
        return sizes
    if split is Split.CONSOLE:
        rows = _place_rows(sizes.console, total, low, high) + int(delta)
        return replace(sizes, console=int(_clamp(rows, low, total - high)))
    least, most = low / total, (total - high) / total
    ratio = _clamp(sizes.table if split is Split.TABLE else sizes.log, least, most) + delta
    return _with_ratio(sizes, split, _clamp(ratio, least, most))
