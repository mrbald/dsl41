"""DL-387: standalone conversion validates the whole input before publishing JSON."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "autorep_snapshot.py"
EXAMPLE = ROOT / "examples" / "snapshots" / "autorep-summary.txt"
EXPECTED_EXAMPLE = {
    "EMEA_EOD_B": {"state": "RU/NE"},
    "EMEA_EXE_B": {"state": "RU"},
    "EMEA_EXE_ORDER_FREEZE_C": {"state": "SU"},
    "GROUP_TREASURY_EOD^PRD": {"state": "Z?"},
}


def _report(rows: list[tuple[str, str, str, str, str, str]], name_width: int = 28) -> bytes:
    widths = (name_width, 20, 20, 5, 8, 7)
    header = ("Job Name", "Last Start", "Last End", "ST/Ex", "Run/Ntry", "Pri/Xit")
    lines = [
        " ".join(f"{cell:<{width}}" for cell, width in zip(header, widths)),
        " ".join("_" * width for width in widths),
    ]
    lines.extend(
        " ".join(f"{cell:<{width}}" for cell, width in zip(row, widths)).rstrip(" ") for row in rows
    )
    return ("\n".join(lines) + "\n").encode("utf-8")


def _row(name: str = "synthetic_job", state: str = "RU") -> tuple[str, str, str, str, str, str]:
    return name, "01/02/2026 03:04:05", "-----", state, "17/1", ""


def _run(
    tmp_path: Path, data: bytes | None, *args: str, script: Path = SCRIPT
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-I", "-S", str(script), *args],
        input=data,
        capture_output=True,
        cwd=tmp_path,
        timeout=30,
        check=False,
    )


def test_standalone_without_site_or_project_imports(tmp_path: Path) -> None:
    copied_script = tmp_path / "converter.py"
    copied_script.write_bytes(SCRIPT.read_bytes())
    (tmp_path / "sitecustomize.py").write_text("raise RuntimeError('site loaded')\n")
    (tmp_path / "json.py").write_text("raise RuntimeError('cwd imported')\n")
    result = _run(tmp_path, _report([_row()]), script=copied_script)
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    assert json.loads(result.stdout) == {"nodes": {"synthetic_job": {"state": "RU"}}}


@pytest.mark.parametrize("name_width", [8, 21, 28, 64, 4096])
def test_variable_full_name_width_and_exact_identity(tmp_path: Path, name_width: int) -> None:
    name = "x" * (name_width - 5) + "^PRD!"
    result = _run(tmp_path, _report([_row(name)], name_width))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"nodes": {name: {"state": "RU"}}}


@pytest.mark.parametrize("state", ["RU/NE", "AC/NE", "Z?", "A B", '<&"!'])
def test_indentation_opaque_dates_and_raw_state(tmp_path: Path, state: str) -> None:
    data = _report(
        [
            ("  Case.job-1^PRD", "not a parsed date", "opaque", state, "0/0", "-0x1"),
            ("case.job-1^PRD", "-----", "-----", "SU", "999/1", "0"),
        ]
    )
    result = _run(tmp_path, b" \r\n" + data.replace(b"\n", b"\r\n") + b"\r\n ")
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    assert json.loads(result.stdout) == {
        "nodes": {"Case.job-1^PRD": {"state": state}, "case.job-1^PRD": {"state": "SU"}}
    }


def test_file_input_and_explicit_stdout(tmp_path: Path) -> None:
    source = tmp_path / "summary.txt"
    source.write_bytes(EXAMPLE.read_bytes())
    result = _run(tmp_path, None, str(source), "-o", "-")
    assert result.returncode == 0, result.stderr
    assert result.stderr == b""
    assert json.loads(result.stdout) == {"nodes": EXPECTED_EXAMPLE}


def _invalid_reports() -> list[bytes]:
    good = _report([_row()])
    lines = good.splitlines()
    return [
        b"",
        b" \n\n",
        b"\n".join(lines[:2]),
        b"\n".join([lines[0], lines[1], lines[2], b"unfinished final job"]),
        good + b"ERROR: report failed\n",
        good + good,
        b"\n".join([*lines, b"", lines[2]]),
        _report([_row(), _row(" synthetic_job")]),
        _report([_row("two names")]),
        _report([_row("")]),
        _report([_row(state="")]),
        _report([("job", "", "-----", "RU", "1/1", "")]),
        _report([("job", "-----", "", "RU", "1/1", "")]),
        _report([("job", "-----", "-----", "RU", "", "")]),
        _report([("job", "-----", "-----", "RU", "run/try", "")]),
        _report([("job", "-----", "-----", "RU", "1/1", "two val")]),
        _report([_row("x" * 29)]),
        _report([_row(state="TOOLONG")]),
        good.replace(b"synthetic_job", b"synthetic\tjob"),
        good + b"\x1b[0m\n",
        good + b"\x7f\n",
        good.replace(b"synthetic_job", b"job\rname"),
        good.replace(b"synthetic_job", "synthetic_j\u00f6b".encode()),
        good.replace(b"RU", "R\u0423".encode()),
        good.replace(b"01/02/2026", "01/02/202\u0666".encode()),
        b"\xef\xbb\xbf" + good,
        good + b"\xff",
        good.replace(b"Last Start", b"Last Begin"),
        good.replace(b"_____ ________ _______", b"______ ________ _______"),
        b"\n".join([lines[0], lines[1], lines[2][:28] + b"X" + lines[2][29:]]),
        good + b"continuation_of_wrapped_name\n",
        good.rstrip(b"\n") + b"      overflow\n",
    ]


@pytest.mark.parametrize("data", _invalid_reports())
def test_invalid_whole_input_preserves_existing_output(tmp_path: Path, data: bytes) -> None:
    output = tmp_path / "snapshot.json"
    prior = b'{"prior":"unchanged"}\n'
    output.write_bytes(prior)
    result = _run(tmp_path, data, "-", "-o", str(output))
    assert result.returncode != 0
    assert result.stdout == b""
    assert result.stderr.startswith(b"autorep_snapshot:")
    assert output.read_bytes() == prior
    assert sorted(path.name for path in tmp_path.iterdir()) == ["snapshot.json"]
    stdout_result = _run(tmp_path, data)
    assert stdout_result.returncode != 0
    assert stdout_result.stdout == b""


def test_valid_output_replaces_only_after_full_input_read(tmp_path: Path) -> None:
    path = tmp_path / "same-input-output.txt"
    path.write_bytes(EXAMPLE.read_bytes())
    result = _run(tmp_path, None, str(path), "--output", str(path))
    assert result.returncode == 0, result.stderr
    assert result.stdout == result.stderr == b""
    assert json.loads(path.read_bytes()) == {"nodes": EXPECTED_EXAMPLE}
    assert list(tmp_path.iterdir()) == [path]


def test_input_and_output_io_failures_publish_no_json(tmp_path: Path) -> None:
    missing = _run(tmp_path, None, str(tmp_path / "missing.txt"))
    assert missing.returncode != 0
    assert missing.stdout == b""
    directory = tmp_path / "output-directory"
    directory.mkdir()
    failed_replace = _run(tmp_path, EXAMPLE.read_bytes(), "-o", str(directory))
    assert failed_replace.returncode != 0
    assert failed_replace.stdout == b""
    assert directory.is_dir()
    assert list(tmp_path.iterdir()) == [directory]


@pytest.mark.skipif(os.environ.get("DSL41_BROWSER_TESTS") != "1", reason="opt-in browser proof")
@pytest.mark.parametrize("engine", ["chromium", "webkit", "firefox"])
def test_converted_fixture_native_viewer_import(tmp_path: Path, engine: str) -> None:
    from playwright.sync_api import sync_playwright

    output = tmp_path / "snapshot.json"
    converted = _run(tmp_path, None, str(EXAMPLE), "-o", str(output))
    assert converted.returncode == 0, converted.stderr
    assert converted.stdout == converted.stderr == b""
    html = tmp_path / "nightbank.html"
    emitted = subprocess.run(
        [
            sys.executable,
            "-m",
            "dsl41",
            "viz",
            "--format",
            "explore",
            "-p",
            str(EXAMPLE.parent / "viewer.properties"),
            "-o",
            str(html),
            *map(str, sorted((ROOT / "examples/nightbank/estate/small").glob("*.jil"))),
        ],
        cwd=ROOT,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert emitted.returncode == 0, emitted.stderr
    with sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch()
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            errors: list[str] = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(html.as_uri())
            page.wait_for_function(
                "() => !document.getElementById('stats').textContent.includes('laying out')",
                timeout=60_000,
            )
            positions = "() => cy.nodes().map(n => [n.id(), n.position()])"
            before = page.evaluate(positions)
            page.locator("#snapshot-toggle").click()
            with page.expect_file_chooser() as chooser:
                page.locator("#snapshot-import").click()
            chooser.value.set_files(str(output))
            page.wait_for_function("() => snapshotLoaded && !snapshotPending")
            assert page.locator("#snapshot-error").is_hidden()
            assert page.evaluate("() => Object.fromEntries(snapshotNodes)") == EXPECTED_EXAMPLE
            assert "External matches: 1" in page.inner_text("#snapshot-buckets")
            assert "Unknown IDs: 0" in page.inner_text("#snapshot-buckets")
            page.locator("#snapshot-preset").select_option("autosys")
            tones = page.evaluate(
                "ids => ids.map(id => decodeURIComponent(nodeMarks(cy.$id(id)))"
                '.match(/data-observation="([^"]+)"/)?.[1])',
                list(EXPECTED_EXAMPLE),
            )
            assert tones == ["neutral", "running", "success", "neutral"]
            assert page.evaluate(positions) == before
            assert errors == []
        finally:
            browser.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_replacement_keeps_an_existing_outputs_mode(tmp_path: Path) -> None:
    output = tmp_path / "snapshot.json"
    output.write_text("old\n", encoding="utf-8")
    output.chmod(0o640)
    result = _run(tmp_path, EXAMPLE.read_bytes(), "-o", str(output))
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["nodes"] == EXPECTED_EXAMPLE
    assert stat.S_IMODE(output.stat().st_mode) == 0o640


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_new_output_takes_the_umask_derived_mode(tmp_path: Path) -> None:
    output = tmp_path / "snapshot.json"
    previous = os.umask(0o027)
    try:
        result = _run(tmp_path, EXAMPLE.read_bytes(), "-o", str(output))
    finally:
        os.umask(previous)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(output.stat().st_mode) == 0o640
