"""The jobs filter reader (DL-511): every word form, its negation, and every refusal.

The reader is pure, so these tests need no UI toolkit. The pilot tests in test_runner_tui.py
drive the same rule through the real filter line.
"""

from __future__ import annotations

import re
import warnings

import pytest
from hypothesis import given
from hypothesis import strategies as st

from dsl41.job_filter import JobFilter, read_filter

_STATUSES = (
    "INACTIVE",
    "QUE_WAIT",
    "STARTING",
    "RUNNING",
    "SUCCESS",
    "FAILURE",
    "TERMINATED",
)


def _reads(text: str) -> JobFilter:
    reading = read_filter(text)
    assert isinstance(reading, JobFilter), reading
    return reading


#: (text, name, status, box, shown)
_MATCHES = [
    # any other word: the name contains it, with wildcards
    ("core", "app_core_load", "INACTIVE", None, True),
    ("CORE", "app_core_load", "INACTIVE", None, True),
    ("core", "app_load", "INACTIVE", None, False),
    ("!core", "app_load", "INACTIVE", None, True),
    ("!core", "app_core_load", "INACTIVE", None, False),
    ("c?re", "x_cure_y", "INACTIVE", None, True),
    ("co*ad", "core_load", "INACTIVE", None, True),
    ("[ab]x", "zbx", "INACTIVE", None, True),
    ("[ab]x", "zcx", "INACTIVE", None, False),
    # =glob: the whole name
    ("=core_*", "core_load", "INACTIVE", None, True),
    ("=CORE_*", "core_load", "INACTIVE", None, True),
    ("=core_*", "app_core_load", "INACTIVE", None, False),
    ("!=core_*", "app_core_load", "INACTIVE", None, True),
    ("!=core_*", "core_load", "INACTIVE", None, False),
    ("=a,b", "a,b", "INACTIVE", None, True),  # a comma is literal after =
    ("=a,b", "a", "INACTIVE", None, False),
    # @glob,glob: one pattern of the list
    ("@core_*,app_*", "app_x", "INACTIVE", None, True),
    ("@core_*,app_*", "core_x", "INACTIVE", None, True),
    ("@core_*,app_*", "web_x", "INACTIVE", None, False),
    ("!@core_*,app_*", "web_x", "INACTIVE", None, True),
    ("!@core_*,app_*", "core_x", "INACTIVE", None, False),
    # ~regex: found in the name, case ignored, pattern case kept (\D is not \d)
    ("~^core_\\d+$", "core_12", "INACTIVE", None, True),
    ("~^core_\\d+$", "core_x", "INACTIVE", None, False),
    ("~CORE", "app_core", "INACTIVE", None, True),
    ("~^\\D+$", "abc", "INACTIVE", None, True),
    ("~^\\D+$", "a1", "INACTIVE", None, False),
    ("!~^core", "app_core", "INACTIVE", None, True),
    ("!~^core", "core_app", "INACTIVE", None, False),
    # \text: the name contains the text as written, each marker included
    ("\\=a", "x=a", "INACTIVE", None, True),
    ("\\@a", "x@a", "INACTIVE", None, True),
    ("\\~a", "x~a", "INACTIVE", None, True),
    ("\\!a", "x!a", "INACTIVE", None, True),
    ("\\\\a", "x\\a", "INACTIVE", None, True),
    ("\\status=su", "status=su_job", "INACTIVE", None, True),
    ("\\a*b", "xa*b", "INACTIVE", None, True),
    ("\\a*b", "axxb", "INACTIVE", None, False),  # no wildcards after \
    ("\\=A", "x=a", "INACTIVE", None, True),
    ("!\\=a", "x=a", "INACTIVE", None, False),
    ("!\\=a", "xa", "INACTIVE", None, True),
    # status: a name or the start of exactly one
    ("status=SUCCESS", "j", "SUCCESS", None, True),
    ("status=success", "j", "SUCCESS", None, True),
    ("status=su", "j", "SUCCESS", None, True),
    ("STATUS=Su", "j", "SUCCESS", None, True),
    ("status=su", "j", "FAILURE", None, False),
    ("status@su,fa", "j", "SUCCESS", None, True),
    ("status@su,fa", "j", "FAILURE", None, True),
    ("status@su,fa", "j", "RUNNING", None, False),
    ("status!=su", "j", "FAILURE", None, True),
    ("status!=su", "j", "SUCCESS", None, False),
    ("!status=su", "j", "FAILURE", None, True),
    ("!status=su", "j", "SUCCESS", None, False),
    ("status!@su,fa", "j", "RUNNING", None, True),
    ("status!@su,fa", "j", "FAILURE", None, False),
    ("!status@su,fa", "j", "RUNNING", None, True),
    ("!status@su,fa", "j", "SUCCESS", None, False),
    # box: the job's own box; a job in no box matches no pattern
    ("box=b_*", "j", "INACTIVE", "b_main", True),
    ("BOX=B_*", "j", "INACTIVE", "b_main", True),
    ("box=b_*", "j", "INACTIVE", "a_main", False),
    ("box=b_*", "j", "INACTIVE", None, False),
    ("box=*", "j", "INACTIVE", None, False),
    ("!box=*", "j", "INACTIVE", None, True),
    ("!box=b_*", "j", "INACTIVE", "b_main", False),
    ("!box=b_*", "j", "INACTIVE", "a_main", True),
    ("box!=b_*", "j", "INACTIVE", None, True),
    ("box!=b_*", "j", "INACTIVE", "b_main", False),
    ("box@a*,b*", "j", "INACTIVE", "a1", True),
    ("box@a*,b*", "j", "INACTIVE", "b1", True),
    ("box@a*,b*", "j", "INACTIVE", "c1", False),
    ("box@a*,b*", "j", "INACTIVE", None, False),
    ("box!@a*,b*", "j", "INACTIVE", None, True),
    ("box!@a*,b*", "j", "INACTIVE", "a1", False),
    ("!box@a*,b*", "j", "INACTIVE", "c1", True),
    ("box=b,c", "j", "INACTIVE", "b,c", True),  # one value after =, comma literal
    # several words: every word matches
    ("!status=su =core_*", "core_a", "FAILURE", None, True),
    ("!status=su =core_*", "core_a", "SUCCESS", None, False),
    ("!status=su =core_*", "app_a", "FAILURE", None, False),
    ("core load", "core_load", "INACTIVE", None, True),
    ("core load", "core_run", "INACTIVE", None, False),
    # blank text matches every job
    ("", "anything", "FAILURE", None, True),
    ("   ", "anything", "SUCCESS", "b", True),
]


@pytest.mark.parametrize(("text", "name", "status", "box", "shown"), _MATCHES)
def test_a_good_word_matches_by_its_form(
    text: str, name: str, status: str, box: str | None, shown: bool
) -> None:
    assert _reads(text).matches(name, status, box) is shown


@pytest.mark.parametrize(
    ("start", "status"),
    [
        ("in", "INACTIVE"),
        ("qu", "QUE_WAIT"),
        ("st", "STARTING"),
        ("ru", "RUNNING"),
        ("su", "SUCCESS"),
        ("fa", "FAILURE"),
        ("te", "TERMINATED"),
    ],
)
def test_a_two_letter_start_names_each_status(start: str, status: str) -> None:
    reading = _reads(f"status={start}")
    assert [s for s in _STATUSES if reading.matches("j", s, None)] == [status]


_ALL = "INACTIVE, QUE_WAIT, STARTING, RUNNING, SUCCESS, FAILURE, TERMINATED"


#: (text, refusal): one line that names the word
_REFUSALS = [
    # a field other than status and box
    ("core=x", "core=x: the fields are status and box; write \\core=x for the text"),
    ("a@b", "a@b: the fields are status and box; write \\a@b for the text"),
    ("!core=x", "!core=x: the fields are status and box; write !\\core=x for the text"),
    ("core!=x", "core!=x: the fields are status and box; write \\core!=x for the text"),
    # a status value that starts no status, or more than one
    ("status=xy", f"status=xy: xy starts no status ({_ALL})"),
    ("status@su,xy", f"status@su,xy: xy starts no status ({_ALL})"),
    ("status=s", "status=s: s could be STARTING or SUCCESS"),
    ("status!@s", "status!@s: s could be STARTING or SUCCESS"),
    # a comma in a status= value
    ("status=su,fa", "status=su,fa: one status after =; write status@su,fa for a list"),
    ("status=su,", "status=su,: empty value"),
    ("status=,", "status=,: empty value"),
    ("status!=su,fa", "status!=su,fa: one status after =; write status!@su,fa for a list"),
    ("!status=su,fa", "!status=su,fa: one status after =; write !status@su,fa for a list"),
    # an empty pattern, value or list item
    ("\\", "\\: empty text"),
    ("!\\", "!\\: empty text"),
    ("=", "=: empty pattern"),
    ("@", "@: empty pattern"),
    ("@a,", "@a,: empty item in the list"),
    ("!@,a", "!@,a: empty item in the list"),
    ("~", "~: empty pattern"),
    ("status=", "status=: empty value"),
    ("status@su,", "status@su,: empty item in the list"),
    ("box=", "box=: empty pattern"),
    ("box@,x", "box@,x: empty item in the list"),
    # two negations
    ("!", "!: nothing to negate"),
    ("!!x", "!!x: a word takes one negation"),
    ("!status!=su", "!status!=su: a word takes one negation"),
    ("!box!@a", "!box!@a: a word takes one negation"),
    # the first refused word is the one named
    ("core status=xy =", f"status=xy: xy starts no status ({_ALL})"),
]


@pytest.mark.parametrize(("text", "refusal"), _REFUSALS)
def test_a_refused_word_is_named_in_one_line(text: str, refusal: str) -> None:
    assert read_filter(text) == refusal


@pytest.mark.parametrize(
    ("text", "pattern"),
    [("~(", "("), ("!~[z-a]", "[z-a]"), ("~a{4294967296}", "a{4294967296}")],
)
def test_a_regular_expression_that_does_not_compile_carries_the_library_message(
    text: str, pattern: str
) -> None:
    try:
        re.compile(pattern, re.IGNORECASE)
    except (re.error, OverflowError) as exc:
        message = str(exc)
    assert read_filter(text) == f"{text}: bad regular expression: {message}"


@pytest.mark.parametrize("text", ["~[[", "~[a&&b]", "~[a--b]"])
def test_a_regular_expression_the_library_warns_about_raises_no_warning(text: str) -> None:
    """A possible set operation makes the library warn; the reader stays silent (DL-511)."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        reading = read_filter(text)
        assert isinstance(reading, (JobFilter, str))
        if isinstance(reading, JobFilter):
            reading.matches("a&&b", "INACTIVE", None)


def test_a_regular_expression_nested_past_the_recursion_limit_is_refused() -> None:
    reading = read_filter("~" + "(" * 5000 + ")" * 5000)
    assert isinstance(reading, str)
    assert ": bad regular expression: " in reading


_MARKED = st.lists(
    st.sampled_from(
        ["a", "B", "_", "*", "?", "[", "]", "!", "\\", "=", "@", "~", ",", " ", "(", ")", "{"]
        + ["status", "box", "su", "s", "x", "1"]
    ),
    max_size=12,
).map("".join)


@given(
    text=st.one_of(_MARKED, st.text(max_size=20)),
    name=st.text(max_size=12),
    status=st.one_of(st.sampled_from(_STATUSES), st.text(max_size=8)),
    box=st.one_of(st.none(), st.text(max_size=8)),
)
def test_the_reader_and_its_predicate_never_raise(
    text: str, name: str, status: str, box: str | None
) -> None:
    reading = read_filter(text)
    if isinstance(reading, str):
        assert "\n" not in reading
        return
    assert isinstance(reading.matches(name, status, box), bool)
