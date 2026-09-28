"""DL-218: the reattach line a detached `dsl41 run` prints on its way out,
when its period can be resumed.

It is built from the process's own argv, so the operator gets back the
whole command line a resume needs (deployment-runbook ss5), not a schematic
with `<files>` in it. These tests hold the transformation to the real `run`
command's option table: a value option added later is read from the
command, not from a list here.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest
import typer.main

from dsl41.cli import app
from dsl41.cli_run import _reattach_line, _reattach_note, _value_options

RUN_OPTIONS = _value_options(typer.main.get_command(app).commands["run"].params)
PROG = "/opt/dsl41/venv/bin/dsl41"


def _reattach(*words: str) -> list[str]:
    line = _reattach_line([PROG, "run", *words], RUN_OPTIONS)
    assert line is not None
    return shlex.split(line)


def test_the_option_table_is_read_from_the_run_command() -> None:
    assert {
        "--run-root",
        "--open-from",
        "--estate-anchor",
        "--access-map",
        "--as-machine",
        "--properties",
        "-p",
        "--timezone",
        "--timezone-map",
        "--machine-policy",
        "--deadman",
    } <= RUN_OPTIONS
    # flags take no value, so none of them may be in the table
    assert not {"--resume", "--detached", "--ui", "--permit-unknown"} & RUN_OPTIONS


def test_a_physical_roll_reattaches_through_the_lineage_anchor() -> None:
    """`--open-from X` becomes `--estate-anchor X`: the openers are
    exclusive, and every later resume of the new root needs the lineage's
    anchor (period-model ss7). Every other word stays where it was."""
    assert _reattach(
        "first.jil",
        "second.jil",
        "--open-from",
        "/srv/dsl41/runs/nightbank-01.anchor",
        "--run-root",
        "/srv/dsl41/runs/nightbank-02",
        "--detached",
        "--as-machine",
        "localhost",
        "-p",
        "night.properties",
        "--access-map",
        "/etc/dsl41/nightbank-access.toml",
    ) == [
        PROG,
        "run",
        "--resume",
        "first.jil",
        "second.jil",
        "--estate-anchor",
        "/srv/dsl41/runs/nightbank-01.anchor",
        "--run-root",
        "/srv/dsl41/runs/nightbank-02",
        "--detached",
        "--as-machine",
        "localhost",
        "-p",
        "night.properties",
        "--access-map",
        "/etc/dsl41/nightbank-access.toml",
    ]


def test_the_equals_spelling_of_open_from_keeps_its_spelling() -> None:
    assert _reattach("a.jil", "--open-from=/srv/A", "--run-root=/srv/R", "--detached") == [
        PROG,
        "run",
        "--resume",
        "a.jil",
        "--estate-anchor=/srv/A",
        "--run-root=/srv/R",
        "--detached",
    ]


def test_a_plain_invocation_gains_resume_and_keeps_everything_else() -> None:
    """Files in their order, every -p in its order, and the access map:
    omitting the map would bring the resumed engine up with no perimeter."""
    assert _reattach(
        "b.jil",
        "a.jil",
        "--run-root",
        "/srv/R",
        "-p",
        "one.properties",
        "--properties",
        "two.properties",
        "--detached",
        "--deadman",
        "90",
        "--access-map",
        "/etc/m.toml",
        "--permit-unknown",
    ) == [
        PROG,
        "run",
        "--resume",
        "b.jil",
        "a.jil",
        "--run-root",
        "/srv/R",
        "-p",
        "one.properties",
        "--properties",
        "two.properties",
        "--detached",
        "--deadman",
        "90",
        "--access-map",
        "/etc/m.toml",
        "--permit-unknown",
    ]


def test_an_estate_anchor_already_present_absorbs_the_open_from() -> None:
    """`run` accepts both only when they name one directory, so the
    `--open-from` words go and the anchor already given stays."""
    assert _reattach(
        "a.jil",
        "--open-from",
        "/srv/A",
        "--estate-anchor",
        "/srv/A",
        "--run-root",
        "/srv/R2",
        "--detached",
    ) == [
        PROG,
        "run",
        "--resume",
        "a.jil",
        "--estate-anchor",
        "/srv/A",
        "--run-root",
        "/srv/R2",
        "--detached",
    ]


def test_an_estate_anchor_resume_is_its_own_reattach_line() -> None:
    words = ["--resume", "a.jil", "--estate-anchor", "/srv/A", "--run-root", "/srv/R", "--detached"]
    assert _reattach(*words) == [PROG, "run", *words]


def test_resume_and_detached_appear_exactly_once() -> None:
    assert _reattach(
        "a.jil", "--detached", "--run-root", "/srv/R", "--resume", "--detached", "--resume"
    ) == [PROG, "run", "a.jil", "--detached", "--run-root", "/srv/R", "--resume"]


def test_a_missing_detached_is_added() -> None:
    """The caller prints this line only for a detached run, which only
    `--detached` makes; the helper still states the line it promises."""
    assert _reattach("a.jil", "--run-root", "/srv/R") == [
        PROG,
        "run",
        "--resume",
        "--detached",
        "a.jil",
        "--run-root",
        "/srv/R",
    ]


def test_an_option_value_is_never_read_as_a_flag() -> None:
    """A value that looks like an option is the option's value; after
    `--` every word is an input; `-pFILE` is one word."""
    assert _reattach(
        "--run-root",
        "--resume",
        "-pnight.properties",
        "--detached",
        "--",
        "--odd.jil",
    ) == [
        PROG,
        "run",
        "--resume",
        "--run-root",
        "--resume",
        "-pnight.properties",
        "--detached",
        "--",
        "--odd.jil",
    ]


def test_words_that_need_quoting_are_quoted() -> None:
    line = _reattach_line(
        [PROG, "run", "my estate.jil", "--run-root", "/srv/R", "--detached"], RUN_OPTIONS
    )
    assert line == f"{PROG} run --resume 'my estate.jil' --run-root /srv/R --detached"


def test_python_dash_m_is_spelled_back_as_python_dash_m() -> None:
    main = "/opt/dsl41/venv/lib/python3.12/site-packages/dsl41/__main__.py"
    line = _reattach_line([main, "run", "a.jil", "--run-root", "/srv/R", "--detached"], RUN_OPTIONS)
    assert line is not None
    assert shlex.split(line)[:4] == [sys.executable, "-m", "dsl41", "run"]


@pytest.mark.parametrize(
    "argv", [[], [PROG], ["/usr/bin/pytest", "-q", "tests"], [PROG, "journal"]]
)
def test_an_argv_that_is_not_a_run_invocation_yields_none(argv: list[str]) -> None:
    """Only an embedding caller (a test harness) has such an argv; the
    engine then prints the schematic line instead."""
    assert _reattach_line(argv, RUN_OPTIONS) is None


LINE = f"{PROG} run --resume a.jil --run-root /srv/R --detached"


@pytest.mark.parametrize("code", [0, 1])
def test_a_resumable_detached_exit_prints_its_own_line(code: int) -> None:
    """A clean stop (0) and a crash (1) leave a period to resume."""
    assert _reattach_note(LINE, Path("/srv/R"), detached=True, code=code) == (
        f"detached: reattach with `{LINE}`"
    )


def test_an_embedded_run_prints_the_schematic_line() -> None:
    assert _reattach_note(None, Path("/srv/R"), detached=True, code=0) == (
        "detached: reattach with `dsl41 run --resume --detached --run-root /srv/R <files>`"
    )


def test_a_sealed_exit_prints_no_reattach_line() -> None:
    """Exit 3 closed the period; `say_next` already named the opener of the
    next one, and a reattach line here would compete with it."""
    assert _reattach_note(LINE, Path("/srv/R"), detached=True, code=3) is None


def test_a_tethered_run_prints_no_reattach_line() -> None:
    assert _reattach_note(None, Path("/srv/R"), detached=False, code=0) is None
