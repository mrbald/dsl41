"""The event console's word tables and the completion of a half-typed line (DL-516).

The completion is pure, so these tests need no UI toolkit. The pilot tests in
test_runner_tui.py drive the same rule through the real console line, and hold the verb table
to the console's verbs and the jobs table's keys.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dsl41.console_words import STATUS_ROWS, VERB_ROWS, complete_line
from dsl41.oracle_state import INJECTABLE_STATUSES

_JOBS = ("ex_load", "ex_feed", "SUCCESS_feed", "alpha")


def test_the_status_table_is_every_status_change_status_takes_in_the_help_order() -> None:
    assert [row.status for row in STATUS_ROWS] == [
        "SUCCESS",
        "FAILURE",
        "TERMINATED",
        "INACTIVE",
        "RUNNING",
        "STARTING",
    ]
    assert {row.status for row in STATUS_ROWS} == INJECTABLE_STATUSES


def test_each_meaning_is_one_line_of_at_most_ninety_characters() -> None:
    for meaning in [row.meaning for row in VERB_ROWS] + [row.meaning for row in STATUS_ROWS]:
        assert len(meaning) <= 90 and "\n" not in meaning, meaning


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # the first word: the first verb of the table's order, upper-cased
        ("s", "STARTJOB"),
        ("S", "STARTJOB"),
        ("O", "ON_HOLD"),
        ("ON_", "ON_HOLD"),
        ("on_i", "ON_ICE"),
        ("of", "OFF_HOLD"),
        ("ch", "CHANGE_STATUS"),
        ("se", "SET_GLOBAL"),
        ("re", "RELEASE_RESOURCE"),
        ("  st", "  STARTJOB"),
        # never a verb whose key asks first: KILLJOB and FORCE_STARTJOB are typed in full
        ("k", None),
        ("K", None),
        ("kil", None),
        ("kill", None),
        ("KILLJO", None),
        ("f", None),
        ("fo", None),
        ("FORCE_", None),
        ("force_startj", None),
        # a word that starts nothing, a whole word, an empty line, an empty word
        ("x", None),
        ("STARTJOB", None),
        ("change_status", None),
        ("", None),
        ("   ", None),
        ("STARTJOB ", None),
        # after a job verb: the first job name in sorted order, case kept
        ("STARTJOB e", "STARTJOB ex_feed"),
        ("startjob ex_l", "startjob ex_load"),
        ("KILLJOB S", "KILLJOB SUCCESS_feed"),
        ("killjob ex_l", "killjob ex_load"),
        ("FORCE_STARTJOB e", "FORCE_STARTJOB ex_feed"),
        ("force_startjob a", "force_startjob alpha"),
        ("STARTJOB E", None),
        ("STARTJOB ex_load", None),
        ("STARTJOB ex_load ", None),
        ("STARTJOB ex_load x", None),
        # after CHANGE_STATUS: a status first, in the table's order, then a job name
        ("CHANGE_STATUS s", "CHANGE_STATUS SUCCESS"),
        ("change_status st", "change_status STARTING"),
        ("CHANGE_STATUS f", "CHANGE_STATUS FAILURE"),
        ("CHANGE_STATUS SUCC", "CHANGE_STATUS SUCCESS"),
        ("CHANGE_STATUS e", "CHANGE_STATUS ex_feed"),
        ("CHANGE_STATUS SUCCESS_", "CHANGE_STATUS SUCCESS_feed"),
        ("CHANGE_STATUS q", None),
        ("CHANGE_STATUS ", None),
        # after a job name: a status
        ("CHANGE_STATUS ex_load t", "CHANGE_STATUS ex_load TERMINATED"),
        ("CHANGE_STATUS ex_load i", "CHANGE_STATUS ex_load INACTIVE"),
        ("CHANGE_STATUS later_job r", "CHANGE_STATUS later_job RUNNING"),
        ("CHANGE_STATUS ex_load ex", None),
        ("CHANGE_STATUS ex_load ", None),
        # a status in the second place is the status-first form: the next word is an exit code
        ("CHANGE_STATUS SUCCESS f", None),
        ("CHANGE_STATUS que_wait f", None),
        ("CHANGE_STATUS SUCCESS 3", None),
        # too many words
        ("CHANGE_STATUS ex_load FAILURE s", None),
        # SET_GLOBAL and an unknown verb complete nothing
        ("SET_GLOBAL N", None),
        ("SET_GLOBAL ", None),
        ("FROB e", None),
    ],
)
def test_complete_line(text: str, expected: str | None) -> None:
    assert complete_line(text, _JOBS) == expected


def test_a_job_named_like_a_status_in_the_second_place_takes_a_status_next() -> None:
    assert complete_line("CHANGE_STATUS SUCCESS f", ("SUCCESS",)) == (
        "CHANGE_STATUS SUCCESS FAILURE"
    )


def test_with_no_jobs_known_a_job_word_completes_nothing_and_a_status_still_does() -> None:
    assert complete_line("STARTJOB e", ()) is None
    assert complete_line("CHANGE_STATUS e", ()) is None
    assert complete_line("CHANGE_STATUS s", ()) == "CHANGE_STATUS SUCCESS"
    assert complete_line("st", ()) == "STARTJOB"


@given(
    st.text(alphabet="acdefgiklnorstuxACDEFGHIKLNORSTU_ ", max_size=30),
    st.lists(st.text(alphabet="aeSU_x", min_size=1, max_size=8), max_size=4),
)
def test_a_completion_extends_the_line_as_typed_without_regard_to_case(
    text: str, jobs: list[str]
) -> None:
    """The grey text is the completion past the typed length, so a completion must start with
    the typed line, letter for letter up to case, and add to it. The words before the completed
    one stay as typed."""
    completed = complete_line(text, jobs)
    if completed is not None:
        assert len(completed) > len(text)
        assert completed[: len(text)].upper() == text.upper()
        kept = len(text) - len(text.split()[-1])  # the words before the completed one
        assert completed[:kept] == text[:kept]
