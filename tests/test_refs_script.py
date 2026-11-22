"""scripts/refs.py prints where a name is defined, referenced or called, from one `ty server`
started for the call (DL-538)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REFS = Path(__file__).resolve().parent.parent / "scripts" / "refs.py"

CORE = """\
def compute(x: int) -> int:
    return x


class Box:
    def method(self) -> int:
        return compute(1)
"""

USE = """\
from pkg.core import Box, compute

# compute is called twice below
LABEL = "compute"


def first() -> int:
    return compute(1)


def second() -> int:
    return compute(2) + Box().method()
"""

DECORATED = """\
def register(label: str, *rest: str):
    def wrap(function):
        return function

    return wrap


@register(
    "tick",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
)
def tick() -> int:
    return 1
"""

DECORATED_USE = """\
from pathlib import Path

from pkg.decorated import tick as alias_tick


def go() -> int:
    return alias_tick() + len(str(Path("x")))
"""

SHARED = """\
def shared() -> int:
    return {value}
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "core.py").write_text(CORE, encoding="utf-8")
    (package / "use.py").write_text(USE, encoding="utf-8")
    (package / "decorated.py").write_text(DECORATED, encoding="utf-8")
    (package / "decorated_use.py").write_text(DECORATED_USE, encoding="utf-8")
    (package / "a.py").write_text(SHARED.format(value=1), encoding="utf-8")
    (package / "b.py").write_text(SHARED.format(value=2), encoding="utf-8")
    return tmp_path


def refs(
    project: Path,
    *args: str,
    executable: str = sys.executable,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [executable, str(REFS), "--root", str(project), *args],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


COMPUTE_REFERENCES = [
    "# compute pkg/core.py:1",
    "pkg/core.py:1:5",
    "pkg/core.py:7:16",
    "pkg/use.py:1:27",
    "pkg/use.py:8:12",
    "pkg/use.py:12:12",
]


def test_a_name_gives_its_definition_and_the_code_references_only(project: Path) -> None:
    done = refs(project, "compute")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == COMPUTE_REFERENCES
    assert done.stderr == ""


def test_a_position_gives_the_same_answer(project: Path) -> None:
    full = refs(project, "pkg/use.py:8:12")
    assert full.returncode == 0, full.stderr
    assert full.stdout.splitlines() == COMPUTE_REFERENCES
    first_blank = refs(project, "pkg/use.py:4")  # no COL: the first identifier, keywords apart
    assert first_blank.returncode == 0, first_blank.stderr
    assert first_blank.stdout.splitlines() == ["# LABEL pkg/use.py:4", "pkg/use.py:4:1"]


def test_a_line_without_a_column_skips_keywords(project: Path) -> None:
    on_return = refs(project, "pkg/use.py:8")  # `    return compute(1)`
    assert on_return.returncode == 0, on_return.stderr
    assert on_return.stdout.splitlines() == COMPUTE_REFERENCES
    on_def = refs(project, "pkg/core.py:1")  # `def compute(...)`
    assert on_def.returncode == 0, on_def.stderr
    assert on_def.stdout.splitlines() == COMPUTE_REFERENCES


def test_a_decorated_definition_is_found_at_its_name(project: Path) -> None:
    done = refs(project, "tick")
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0] == "# tick pkg/decorated.py:20"
    assert "pkg/decorated.py:20:5" in lines
    assert "pkg/decorated.py:9:5" not in lines  # the decorator's string is no definition
    assert "pkg/decorated_use.py:3:27" in lines


def test_a_position_on_an_import_alias_lists_the_alias_uses(project: Path) -> None:
    done = refs(project, "pkg/decorated_use.py:7:12")  # `alias_tick()`
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0] == "# alias_tick pkg/decorated.py:20"
    assert "pkg/decorated_use.py:7:12" in lines
    assert "pkg/decorated_use.py:3:35" in lines


def test_a_position_on_a_name_defined_outside_the_workspace_lists_the_local_uses(
    project: Path,
) -> None:
    done = refs(project, "pkg/decorated_use.py:7:35")  # `Path("x")`
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0].startswith("# Path ")
    assert "pkg/decorated_use.py:1:21" in lines
    assert "pkg/decorated_use.py:7:35" in lines


def test_defs_prints_the_header_only(project: Path) -> None:
    done = refs(project, "--defs", "compute")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["# compute pkg/core.py:1"]


def test_callers_name_the_calling_functions(project: Path) -> None:
    done = refs(project, "--callers", "compute")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "# compute pkg/core.py:1",
        "pkg/core.py:7:16 method",
        "pkg/use.py:8:12 first",
        "pkg/use.py:12:12 second",
    ]


def test_two_definitions_of_one_name_give_two_headed_groups(project: Path) -> None:
    done = refs(project, "shared")
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [
        "# shared pkg/a.py:1",
        "pkg/a.py:1:5",
        "# shared pkg/b.py:1",
        "pkg/b.py:1:5",
    ]


def test_an_unknown_name_exits_1(project: Path) -> None:
    done = refs(project, "nothing_called_this")
    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr.splitlines() == ["refs.py: no definition named nothing_called_this"]


@pytest.mark.parametrize(
    "position",
    ["pkg/use.py:99", "pkg/use.py:3:3", "pkg/use.py:4:12", "pkg/missing.py:1"],
)
def test_a_bad_position_exits_1(project: Path, position: str) -> None:
    done = refs(project, position)
    assert done.returncode == 1
    assert done.stdout == ""
    assert len(done.stderr.splitlines()) == 1


@pytest.mark.parametrize(
    "args",
    [[], ["not a name"], ["--callers", "--defs", "compute"], ["--timeout", "0", "compute"]],
)
def test_a_bad_argument_exits_2(project: Path, args: list[str]) -> None:
    done = refs(project, *args)
    assert done.returncode == 2
    assert done.stdout == ""
    assert "Traceback" not in done.stderr


def test_an_unexpected_failure_exits_2_with_one_line(project: Path, tmp_path: Path) -> None:
    script = stand_in(
        tmp_path,
        "sys.stdout.buffer.write(b'Content-Length: 1\\r\\n\\r\\n5')\n"
        "sys.stdout.buffer.flush()\nsys.stdin.buffer.read()",
    )
    done = refs(project, "--ty", str(script), "compute")
    assert done.returncode == 2
    assert len(done.stderr.splitlines()) == 1
    assert "Traceback" not in done.stderr


def test_a_closed_stdout_ends_quietly(project: Path) -> None:
    proc = subprocess.Popen(
        [sys.executable, str(REFS), "--root", str(project), "compute"],
        cwd=project,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.close()
    assert proc.wait(timeout=60) == 0
    assert proc.stderr.read() == ""
    proc.stderr.close()


def stand_in(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-server"
    script.write_text(f"#!{sys.executable}\nimport os, sys\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return script


def test_a_server_that_never_answers_times_out_and_leaves_no_child(
    project: Path, tmp_path: Path
) -> None:
    pidfile = tmp_path / "server.pid"
    script = stand_in(
        tmp_path,
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "sys.stdin.buffer.read()\nimport time\ntime.sleep(600)",
    )
    done = refs(project, "--timeout", "3", "--ty", str(script), "compute")
    assert done.returncode == 2
    assert done.stderr.splitlines()[0].startswith("refs.py: timeout waiting for initialize")
    assert "Traceback" not in done.stderr
    pid = int(pidfile.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_a_server_that_exits_at_once_is_a_protocol_error(project: Path, tmp_path: Path) -> None:
    script = stand_in(tmp_path, "sys.stderr.write('boom')")
    done = refs(project, "--ty", str(script), "compute")
    assert done.returncode == 2
    assert "server closed" in done.stderr
    assert "server stderr: boom" in done.stderr


def test_no_ty_beside_the_interpreter_or_on_the_path_exits_2(project: Path, tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    lonely_python = bin_dir / "python"
    lonely_python.symlink_to(Path(sys.executable).resolve())
    empty = tmp_path / "empty"
    empty.mkdir()
    done = refs(project, "compute", executable=str(lonely_python), env={"PATH": str(empty)})
    assert done.returncode == 2
    assert done.stderr.splitlines() == ["refs.py: ty not found: uv sync --frozen --extra dev"]
