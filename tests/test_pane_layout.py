"""The arithmetic of the TUI's three splits (DL-520).

The module is pure, so these tests need no UI toolkit. The pilot tests in test_runner_tui.py
drive the same rules through the real app's borders and keys.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dsl41.pane_layout import Placement, Sizes, Split, place, set_cells, step


def test_the_defaults_place_three_fifths_and_eleven_console_rows() -> None:
    assert place(Sizes(), 120, 38) == Placement(table=72, log=16, console=11)


def test_a_placement_names_the_cells_of_each_split() -> None:
    placed = Placement(table=50, log=12, console=9)
    assert [placed.of(split) for split in Split] == [50, 12, 9]


def test_the_limits_hold_when_a_ratio_or_the_rows_ask_for_more() -> None:
    assert place(Sizes(table=0.05, log=0.01, console=1), 120, 38) == Placement(20, 5, 6)
    # the console leaves 8 rows above it, and the log leaves the explain pane 3
    assert place(Sizes(table=0.99, log=0.99, console=60), 120, 38) == Placement(100, 5, 30)


def test_a_change_of_size_keeps_the_ratios_and_the_console_rows() -> None:
    sizes = Sizes(table=0.4, log=0.5, console=15)
    assert place(sizes, 120, 38) == Placement(48, 12, 15)
    assert place(sizes, 180, 48) == Placement(72, 17, 15)
    assert place(sizes, 50, 14) == Placement(20, 5, 6)  # the limits give way, nothing else
    assert place(sizes, 120, 38) == Placement(48, 12, 15)


def test_a_span_too_small_for_the_limits_is_placed_as_it_is() -> None:
    sizes = Sizes(table=0.4, log=0.5, console=15)
    # 11 rows: the console keeps its rows, as many as there are, and the log what is left
    assert place(sizes, 36, 11) == Placement(table=14, log=0, console=11)
    assert place(Sizes(console=6), 36, 11) == Placement(table=22, log=3, console=6)
    for split in Split:
        assert set_cells(sizes, split, 7, 36, 11) == sizes
        assert step(sizes, split, 1, 36, 11) == sizes


def test_set_cells_takes_the_cells_within_the_limits() -> None:
    sizes = Sizes()
    assert set_cells(sizes, Split.TABLE, 79, 120, 38) == Sizes(table=79 / 120)
    assert set_cells(sizes, Split.TABLE, 0, 120, 38) == Sizes(table=20 / 120)
    assert set_cells(sizes, Split.TABLE, 200, 120, 38) == Sizes(table=100 / 120)
    assert set_cells(sizes, Split.LOG, 12, 120, 38) == Sizes(log=12 / 27)
    assert set_cells(sizes, Split.LOG, 30, 120, 38) == Sizes(log=24 / 27)
    assert set_cells(sizes, Split.CONSOLE, 2, 120, 38) == Sizes(console=6)
    assert set_cells(sizes, Split.CONSOLE, 40, 120, 38) == Sizes(console=30)


def test_set_cells_where_the_split_already_is_keeps_the_ratio() -> None:
    sizes = Sizes(table=0.6001)
    assert set_cells(sizes, Split.TABLE, 72, 120, 38) is sizes
    assert set_cells(sizes, Split.CONSOLE, 11, 120, 38) is sizes


def test_step_moves_a_ratio_from_where_it_is_placed() -> None:
    assert step(Sizes(), Split.TABLE, 0.05, 120, 38).table == pytest.approx(0.65)
    assert step(Sizes(), Split.LOG, -0.2, 120, 38).log == pytest.approx(0.4)
    # a ratio beyond what the limits place now steps from the limit, not from itself
    assert step(Sizes(table=0.95), Split.TABLE, -0.2, 120, 38).table == pytest.approx(76 / 120)
    assert step(Sizes(table=0.8), Split.TABLE, 0.2, 120, 38).table == pytest.approx(100 / 120)
    assert step(Sizes(log=0.1), Split.LOG, -0.05, 120, 38).log == pytest.approx(5 / 27)


def test_step_moves_the_console_by_rows_within_the_limits() -> None:
    assert step(Sizes(), Split.CONSOLE, 2, 120, 38).console == 13
    assert step(Sizes(console=29), Split.CONSOLE, 2, 120, 38).console == 30
    assert step(Sizes(console=7), Split.CONSOLE, -2, 120, 38).console == 6
    assert step(Sizes(console=60), Split.CONSOLE, -2, 120, 38).console == 28


def test_small_steps_on_a_small_span_still_move() -> None:
    sizes = Sizes()
    rows = [place(sizes, 120, 26).log]
    for _ in range(4):
        sizes = step(sizes, Split.LOG, 0.05, 120, 26)
        rows.append(place(sizes, 120, 26).log)
    assert rows == sorted(rows) and rows[-1] > rows[0]


@given(
    table=st.floats(0, 1),
    log=st.floats(0, 1),
    console=st.integers(0, 200),
    width=st.integers(40, 400),
    height=st.integers(14, 200),
)
def test_placement_keeps_every_limit_on_a_screen_that_fits(
    table: float, log: float, console: int, width: int, height: int
) -> None:
    placed = place(Sizes(table, log, console), width, height)
    assert 20 <= placed.table <= width - 20
    assert 6 <= placed.console <= height - 8
    above = height - placed.console
    if above >= 8:
        assert 5 <= placed.log <= above - 3
