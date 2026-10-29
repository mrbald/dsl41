"""`dsl41 estate build` and `dsl41 estate diff` (DL-413).

The source directory is corpus-shaped and synthetic: a manifest with two environments, a
calendar file, two layers and a timezone map copied as they are, and jobs.jil rendered from a
Python module that sits beside them and never enters the release. Every refusal asserts the
fragment that only its own rule prints, and leaves no release and no OUT.tmp behind. The diff
tests compare what the seal's classifier compares, one kind at a time.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41.cli import app

from release_site import Site, site_at

runner = CliRunner()

MANIFEST = """\
name = "demo"
files = ["calendars.jil", "jobs.jil"]
properties = ["layers/base.properties"]
timezone = "UTC"
timezone_map = "tz.map"

[semantics]

[environments.prod]
properties = ["layers/prod.properties"]

[environments.uat]
"""

CALENDARS = """\
calendar: holidays
01/01/2026 00:00
12/25/2026 00:00
"""

MODULE = """\
from dsl41.dsl import CatalogBuilder


def estate() -> CatalogBuilder:
    b = CatalogBuilder()
    b.machine("m1")
    b.job("extract", command="~{$ROOT}~/extract.sh", machine="m1", run_calendar="holidays")
    b.job("load", command="~{$ROOT}~/load.sh", machine="m1", condition="s(extract)")
    return b
"""


def _invoke(*args: str):
    return runner.invoke(app, list(args), catch_exceptions=False)


def _source(base: Path, manifest: str = MANIFEST) -> Path:
    """The authoring directory: the manifest, the calendars, two layers, the map and the
    module."""
    (base / "layers").mkdir(parents=True)
    (base / "estate.toml").write_text(manifest)
    (base / "calendars.jil").write_text(CALENDARS)
    (base / "layers" / "base.properties").write_text("ROOT=/opt/demo\n")
    (base / "layers" / "prod.properties").write_text("ROOT=/opt/prod\n")
    (base / "tz.map").write_text("ZRH Europe/Zurich\n")
    (base / "catalog.py").write_text(MODULE)
    return base


def _build(source: Path, out: Path, *extra: str):
    return _invoke(
        "estate", "build", str(source), "-o", str(out),
        "--emit", f"jobs.jil={source / 'catalog.py'}", *extra,
    )  # fmt: skip


def _files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ------------------------------------------------------------- estate build


def test_build_writes_the_manifest_files_and_the_rendered_jil(tmp_path: Path) -> None:
    source = _source(tmp_path / "src")
    out = tmp_path / "dist" / "v1"
    out.parent.mkdir()
    result = _build(source, out)
    assert result.exit_code == 0, result.stderr
    assert result.stdout == f"wrote {out}\n"
    built = _invoke("build", str(source / "catalog.py"), "--stdout")
    files = _files(out)
    assert sorted(files) == [
        "calendars.jil",
        "estate.toml",
        "jobs.jil",
        "layers/base.properties",
        "layers/prod.properties",
        "tz.map",
    ]
    assert files["jobs.jil"].decode() == built.stdout
    assert files["estate.toml"] == (source / "estate.toml").read_bytes()
    assert files["layers/prod.properties"] == b"ROOT=/opt/prod\n"
    assert files["tz.map"] == b"ZRH Europe/Zurich\n"
    assert not (tmp_path / "dist" / "v1.tmp").exists()
    for environment in ("prod", "uat"):
        linted = _invoke(
            "lint", "--manifest", str(out / "estate.toml"), "--environment", environment
        )
        assert linted.exit_code == 0, linted.stdout + linted.stderr


ADDED_MODULE = """\
from dsl41.dsl import CatalogBuilder


def estate() -> CatalogBuilder:
    b = CatalogBuilder()
    b.job("NB_ADDED_C", command="echo added", machine="localhost", owner="dsl41")
    return b
"""


@pytest.fixture
def site(tmp_path: Path) -> Iterator[Site]:
    yield from site_at(tmp_path)


def test_release_prepare_takes_the_built_directory(site: Site) -> None:
    """DL-412, DL-413: the handoff the authoring guide gives, on the release tests' stub site:
    `estate build` renders one file of the nightbank small estate from Python, and `dsl41
    release prepare` copies, lints and rehearses the result, with no Python in it."""
    source = site.source("v2")
    (source / "added.py").write_text(ADDED_MODULE)
    manifest = source / "estate.toml"
    manifest.write_text(manifest.read_text().replace('"infra.jil"]', '"infra.jil", "added.jil"]'))
    out = site.base / "dist" / "v2"
    out.parent.mkdir()
    built = _invoke(
        "estate", "build", str(source), "-o", str(out), "--emit", f"added.jil={source / 'added.py'}"
    )
    assert built.exit_code == 0, built.stderr
    done = site.run("prepare", "v2", str(out))
    assert done.returncode == 0, done.stderr
    prepared = site.receipt("v2", "prepared")
    assert prepared["lint"]["exit"] == 0 and prepared["rehearse"]["exit"] == 0
    assert "added.jil" in prepared["files"]
    assert not [name for name in prepared["files"] if name.endswith(".py")]
    assert b"NB_ADDED_C" in (site.releases / "v2" / "added.jil").read_bytes()


def test_build_fills_an_empty_out_directory(tmp_path: Path) -> None:
    source = _source(tmp_path / "src")
    out = tmp_path / "v1"
    out.mkdir()
    assert _build(source, out).exit_code == 0
    assert "jobs.jil" in _files(out)


def test_an_emitted_name_that_is_also_a_source_file_is_refused(tmp_path: Path) -> None:
    source = _source(tmp_path / "src")
    (source / "jobs.jil").write_text("stale\n")
    out = tmp_path / "v1"
    result = _build(source, out)
    assert result.exit_code == 2
    assert (
        f"--emit jobs.jil={source / 'catalog.py'}: {source / 'jobs.jil'} exists too;"
        " remove one of them"
    ) in result.stderr
    assert not out.exists() and not (tmp_path / "v1.tmp").exists()


def test_build_without_emit_copies_a_checked_in_jil(tmp_path: Path) -> None:
    source = _source(tmp_path / "src")
    (source / "jobs.jil").write_text("insert_job: one\njob_type: c\ncommand: x\nmachine: m\n")
    out = tmp_path / "v1"
    result = _invoke("estate", "build", str(source), "-o", str(out))
    assert result.exit_code == 0, result.stderr
    assert (out / "jobs.jil").read_bytes() == (source / "jobs.jil").read_bytes()


def test_the_module_is_imported_once_by_each_verb_through_one_loader(tmp_path: Path) -> None:
    """DL-410, DL-413: `dsl41 build` and `estate build` render through cli_common.module_jil,
    and each call imports the module once."""
    source = _source(tmp_path / "src")
    count = tmp_path / "imports.txt"
    (source / "catalog.py").write_text(
        f"from pathlib import Path\nwith Path({str(count)!r}).open('a') as f:\n"
        "    f.write('x')\n" + MODULE
    )
    assert _invoke("build", str(source / "catalog.py"), "--stdout").exit_code == 0
    assert count.read_text() == "x"
    assert _build(source, tmp_path / "v1").exit_code == 0
    assert count.read_text() == "xx"
    assert _build(source, tmp_path / "v1", "--check").exit_code == 0
    assert count.read_text() == "xxx"


PACKAGE_INIT = """\
from pathlib import Path

from dsl41.dsl import CatalogBuilder

from . import extract, load

with Path(__file__).with_name("imports.txt").open("a") as handle:
    handle.write("x")


def estate() -> CatalogBuilder:
    return CatalogBuilder.compose({"extract.jil": extract.estate(), "load.jil": load.estate()})
"""


def _package_source(base: Path) -> Path:
    """The authoring directory with jobs.jil split in two files, each from one module of a
    package whose __init__.py counts its imports (DL-418)."""
    source = _source(base, MANIFEST.replace('"jobs.jil"]', '"extract.jil", "load.jil"]'))
    package = source / "jobs"
    package.mkdir()
    (package / "__init__.py").write_text(PACKAGE_INIT)
    for name, line in (
        ("extract", 'b.job("extract", command="~{$ROOT}~/extract.sh", machine="m1")'),
        (
            "load",
            'b.job("load", command="~{$ROOT}~/load.sh", machine="m1", condition="s(extract)")',
        ),
    ):
        (package / f"{name}.py").write_text(
            MODULE.split("    b.machine")[0]
            + ("    b.machine('m1')\n" if name == "extract" else "")
            + f"    {line}\n    return b\n"
        )
    return source


def test_build_emits_each_file_of_a_package_and_imports_it_once(tmp_path: Path) -> None:
    source = _package_source(tmp_path / "src")
    package = source / "jobs"
    out = tmp_path / "v1"
    result = _invoke(
        "estate", "build", str(source), "-o", str(out),
        "--emit", f"extract.jil={package}", "--emit", f"load.jil={package}",
    )  # fmt: skip
    assert result.exit_code == 0, result.stderr
    assert (package / "imports.txt").read_text() == "x"
    files = _files(out)
    assert sorted(files) == [
        "calendars.jil",
        "estate.toml",
        "extract.jil",
        "layers/base.properties",
        "layers/prod.properties",
        "load.jil",
        "tz.map",
    ]
    assert files["load.jil"] == (
        b"insert_job: load\njob_type: c\ncommand: ~{$ROOT}~/load.sh\nmachine: m1\n"
        b"condition: s(extract)\n"
    )
    assert files["extract.jil"].startswith(b"insert_machine: m1\n")


def test_build_imports_a_package_once_under_two_spellings_of_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _package_source(tmp_path / "src")
    monkeypatch.chdir(source)
    result = _invoke(
        "estate", "build", str(source), "-o", str(tmp_path / "v1"),
        "--emit", "extract.jil=jobs", "--emit", f"load.jil={source / 'jobs'}",
    )  # fmt: skip
    assert result.exit_code == 0, result.stderr
    assert (source / "jobs" / "imports.txt").read_text() == "x"


def test_build_refuses_a_name_the_package_does_not_compose(tmp_path: Path) -> None:
    source = _package_source(tmp_path / "src")
    for name in ("extract", "load"):
        (source / f"{name}.jil").write_text(f"insert_job: {name}\njob_type: c\ncommand: x\n")
    (source / "estate.toml").write_text(
        MANIFEST.replace('"jobs.jil"]', '"extract.jil", "load.jil", "other.jil"]')
    )
    out = tmp_path / "v1"
    result = _invoke(
        "estate", "build", str(source), "-o", str(out), "--emit", f"other.jil={source / 'jobs'}"
    )
    assert result.exit_code == 2
    assert (
        f"--emit other.jil={source / 'jobs'}: {source / 'jobs'} composes extract.jil, load.jil"
        in (result.stderr)
    )
    assert not out.exists()


def test_build_refuses_param_because_the_layers_bind(tmp_path: Path) -> None:
    source = _source(tmp_path / "src")
    out = tmp_path / "v1"
    result = _build(source, out, "--param", "ROOT=/opt/x")
    assert result.exit_code == 2
    assert "a release binds them through the manifest's layers" in result.stderr
    assert not out.exists()


def _layer_twice(source: Path) -> None:
    (source / "estate.toml").write_text(
        MANIFEST.replace(
            'properties = ["layers/prod.properties"]',
            'properties = ["layers/base.properties"]',
        )
    )


@pytest.mark.parametrize(
    ("setup", "extra", "fragment"),
    [
        (
            lambda s: (s / "calendars.jil").unlink(),
            (),
            "the manifest names calendars.jil, which is not a file there",
        ),
        (
            lambda s: (s / "estate.toml").write_text('python = "catalog.py"\n' + MANIFEST),
            (),
            "unknown key python",
        ),
        (
            lambda s: (s / "estate.toml").write_text(
                MANIFEST.replace('"jobs.jil"]', '"jobs.jil", "catalog.py"]')
            ),
            (),
            "files names 'catalog.py', a Python file; a release takes no Python",
        ),
        (
            lambda s: (s / "estate.toml").write_text(MANIFEST.replace("layers/", "../")),
            (),
            "must be a path relative to the manifest, with no '..' part",
        ),
        (
            lambda s: (s / "estate.toml").write_text(
                MANIFEST.replace('"calendars.jil"', '"./calendars.jil"')
            ),
            (),
            "'./calendars.jil' must be a path relative to the manifest",
        ),
        (None, ("--emit", "other.jil=x.py"), "--emit other.jil: the manifest's files do not"),
        (None, ("--emit", "jobs.jil"), "give NAME=MODULE"),
        (None, ("--emit", "jobs.jil=x.py"), "--emit jobs.jil: given twice"),
        (_layer_twice, (), "properties names"),
        (
            lambda s: (s / "catalog.py").write_text("def estate():\n    raise ValueError('no')\n"),
            (),
            "estate() raised ValueError: no",
        ),
    ],
    ids=[
        "missing",
        "python-key",
        "python-file",
        "dotdot",
        "not-normal",
        "emit-unnamed",
        "emit-form",
        "emit-twice",
        "fails-validation",
        "module-raises",
    ],
)
def test_build_refusals_write_nothing(
    tmp_path: Path, setup: object, extra: tuple[str, ...], fragment: str
) -> None:
    source = _source(tmp_path / "src")
    if callable(setup):
        setup(source)
    out = tmp_path / "v1"
    result = _build(source, out, *extra)
    assert result.exit_code == 2
    assert fragment in result.stderr
    assert not out.exists() and not (tmp_path / "v1.tmp").exists()


def test_a_layer_named_by_base_and_environment_refuses_at_validation(tmp_path: Path) -> None:
    source = _source(tmp_path / "src")
    _layer_twice(source)
    result = _build(source, tmp_path / "v1")
    assert result.exit_code == 2
    assert f"{tmp_path}/v1.tmp/estate.toml: properties names" in result.stderr
    assert "twice" in result.stderr


@pytest.mark.parametrize(
    ("linked", "fragment"),
    [
        ("layers/base.properties", "layers/base.properties: a symlink, not a file"),
        ("tz.map", "tz.map: a symlink, not a file"),
        ("layers", "layers: a symlink; copy the files in"),
        ("estate.toml", "estate.toml: a symlink, not a file"),
    ],
    ids=["layer", "timezone-map", "directory", "manifest"],
)
def test_build_refuses_a_symlinked_input(tmp_path: Path, linked: str, fragment: str) -> None:
    source = _source(tmp_path / "src")
    real = tmp_path / "real"
    (source / linked).rename(real)
    (source / linked).symlink_to(real)
    out = tmp_path / "v1"
    result = _build(source, out)
    assert result.exit_code == 2
    assert f"{source}/{fragment}" in result.stderr
    assert not out.exists()


def test_build_refuses_a_fifo_without_blocking(tmp_path: Path) -> None:
    source = _source(tmp_path / "src")
    (source / "calendars.jil").unlink()
    os.mkfifo(source / "calendars.jil")
    result = _build(source, tmp_path / "v1")
    assert result.exit_code == 2
    assert f"{source}/calendars.jil: not a regular file" in result.stderr


def test_build_refuses_a_non_empty_out(tmp_path: Path) -> None:
    source = _source(tmp_path / "src")
    out = tmp_path / "v1"
    out.mkdir()
    (out / "keep").write_text("keep\n")
    result = _build(source, out)
    assert result.exit_code == 2 and f"{out} exists and is not an empty directory" in result.stderr
    assert _files(out) == {"keep": b"keep\n"}


def test_build_refuses_a_file_at_out(tmp_path: Path) -> None:
    source = _source(tmp_path / "src")
    out = tmp_path / "v1"
    out.write_text("x")
    result = _build(source, out)
    assert result.exit_code == 2 and "is not an empty directory" in result.stderr


def test_build_refuses_an_out_it_cannot_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path / "src")
    out = tmp_path / "v1"
    out.mkdir()
    listdir = os.listdir

    def refuse_out(path: object = ".") -> list[str]:
        if Path(str(path)) == out:
            raise PermissionError(13, "Permission denied", str(path))
        return listdir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "listdir", refuse_out)
    result = _build(source, out)
    assert result.exit_code == 2 and "Permission denied" in result.stderr


def test_build_refuses_a_left_over_tmp(tmp_path: Path) -> None:
    source = _source(tmp_path / "src")
    (tmp_path / "v1.tmp").mkdir()
    result = _build(source, tmp_path / "v1")
    assert result.exit_code == 2 and "v1.tmp exists; remove it, then build again" in result.stderr
    assert not (tmp_path / "v1").exists()


def test_build_refuses_an_unreadable_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source(tmp_path / "src")
    real_open = os.open

    def refuse_calendars(path: object, flags: int, *args: int, **kwargs: object) -> int:
        if Path(str(path)).name == "calendars.jil":
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", refuse_calendars)
    result = _build(source, tmp_path / "v1")
    assert result.exit_code == 2 and "Permission denied" in result.stderr


def test_build_refuses_an_out_in_a_missing_directory(tmp_path: Path) -> None:
    source = _source(tmp_path / "src")
    result = _build(source, tmp_path / "no" / "v1")
    assert result.exit_code == 2 and "No such file or directory" in result.stderr


# ------------------------------------------------------------- estate build --check


def test_check_passes_on_an_equal_release_and_writes_nothing(tmp_path: Path) -> None:
    source = _source(tmp_path / "src")
    out = tmp_path / "v1"
    assert _build(source, out).exit_code == 0
    before = _files(out)
    result = _build(source, out, "--check")
    assert result.exit_code == 0, result.stderr
    assert result.stdout == f"{out} matches {source}\n"
    assert _files(out) == before and not (tmp_path / "v1.tmp").exists()


def test_check_lists_each_difference_and_exits_1(tmp_path: Path) -> None:
    source = _source(tmp_path / "src")
    out = tmp_path / "v1"
    assert _build(source, out).exit_code == 0
    (out / "jobs.jil").write_text("stale\n")
    (out / "calendars.jil").unlink()
    (out / "notes.txt").write_text("extra\n")
    (out / "hidden").mkdir()
    (out / "hidden" / "extra.py").write_text("x = 1\n")
    (out / "layers" / "prod.properties").unlink()
    (out / "layers" / "prod.properties").symlink_to(source / "layers" / "prod.properties")
    before = _files(out)
    result = _build(source, out, "--check")
    assert result.exit_code == 1
    assert result.stdout.splitlines() == [
        "missing  calendars.jil",
        "extra    hidden/extra.py",
        "differs  jobs.jil",
        "differs  layers/prod.properties",
        "extra    notes.txt",
    ]
    assert _files(out) == before


def test_check_refuses_a_directory_it_cannot_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable directory under OUT may hide an extra file, so the check refuses rather
    than report a match (DL-413)."""
    source = _source(tmp_path / "src")
    out = tmp_path / "v1"
    assert _build(source, out).exit_code == 0
    (out / "hidden").mkdir()
    scandir = os.scandir

    def refuse_hidden(path: object = ".") -> object:
        if Path(str(path)) == out / "hidden":
            raise PermissionError(13, "Permission denied", str(path))
        return scandir(path)  # type: ignore[call-overload]

    monkeypatch.setattr(os, "scandir", refuse_hidden)
    result = _build(source, out, "--check")
    assert result.exit_code == 2 and f"Permission denied: '{out}/hidden'" in result.stderr
    assert result.stdout == ""


def test_check_refuses_a_missing_out(tmp_path: Path) -> None:
    source = _source(tmp_path / "src")
    result = _build(source, tmp_path / "v1", "--check")
    assert result.exit_code == 2 and "--check compares with an existing directory" in result.stderr


# ------------------------------------------------------------- estate diff

OLD_JOBS = """\
insert_global: LIMIT
value: 5

insert_job: keep
job_type: c
command: ~{$ROOT}~/keep.sh
machine: m1
date_conditions: 1
start_times: "02:00"
run_calendar: holidays

insert_job: edited
job_type: c
command: ~{$ROOT}~/edited.sh
machine: m1

insert_job: gone
job_type: c
command: ~{$ROOT}~/gone.sh
machine: m1
"""

NEW_JOBS = """\
insert_global: LIMIT
value: 5

insert_job: keep
job_type: c
command: ~{$ROOT}~/keep.sh
machine: m1
date_conditions: 1
start_times: "02:00"
run_calendar: holidays

insert_job: edited
job_type: c
command: ~{$ROOT}~/edited.sh --fast
machine: m1

insert_job: fresh
job_type: c
command: ~{$ROOT}~/fresh.sh
machine: m1
"""

NONE: dict[str, list[str]] = {"added": [], "removed": [], "changed": []}


def _release(base: Path, jobs: str, calendars: str = CALENDARS, extra: str = "") -> Path:
    """A release directory with its manifest; returns the manifest's path."""
    (base / "layers").mkdir(parents=True)
    (base / "estate.toml").write_text(MANIFEST.replace("[semantics]\n", f"[semantics]\n{extra}"))
    (base / "calendars.jil").write_text(calendars)
    (base / "jobs.jil").write_text(jobs)
    (base / "layers" / "base.properties").write_text("ROOT=/opt/demo\n")
    (base / "layers" / "prod.properties").write_text("ROOT=/opt/prod\n")
    (base / "tz.map").write_text("ZRH Europe/Zurich\n")
    return base / "estate.toml"


def _pair(tmp_path: Path) -> tuple[Path, Path]:
    old = _release(tmp_path / "r1", OLD_JOBS)
    new = _release(
        tmp_path / "r2",
        NEW_JOBS,
        CALENDARS.replace("12/25/2026", "12/24/2026"),
        'ice-lookback = "ordinary"\n',
    )
    return old, new


def _json(old: Path, new: Path, *extra: str) -> tuple[int, dict[str, object]]:
    result = _invoke("estate", "diff", str(old), str(new), "--format", "json", *extra)
    assert result.exit_code in (0, 1), result.stderr
    return result.exit_code, json.loads(result.stdout)


def test_diff_text_has_one_section_per_kind(tmp_path: Path) -> None:
    old, new = _pair(tmp_path)
    result = _invoke("estate", "diff", str(old), str(new), "--environment", "prod")
    assert result.exit_code == 1, result.stderr
    assert result.stdout == (
        "jobs\n"
        "  added   fresh\n"
        "  removed gone\n"
        "  changed edited\n"
        "calendars\n"
        "  changed holidays\n"
        "    referenced by keep\n"
        "profile\n"
        "  semantics ice-lookback: true -> ordinary\n"
    )


def test_diff_json_is_one_object(tmp_path: Path) -> None:
    old, new = _pair(tmp_path)
    code, doc = _json(old, new, "--environment", "uat")
    assert code == 1
    assert doc == {
        "jobs": {"added": ["fresh"], "removed": ["gone"], "changed": ["edited"]},
        "boxes": NONE,
        "calendars": {
            "added": [],
            "removed": [],
            "changed": ["holidays"],
            "referenced_by": {"holidays": ["keep"]},
        },
        "cycles": NONE,
        "resources": {**NONE, "referenced_by": {}},
        "machines": NONE,
        "globals": NONE,
        "external_instances": NONE,
        "profile": {"semantics": {"ice-lookback": {"old": "true", "new": "ordinary"}}},
    }


def test_diff_of_a_release_with_itself_is_empty(tmp_path: Path) -> None:
    old = _release(tmp_path / "r1", OLD_JOBS)
    text = _invoke("estate", "diff", str(old), str(old), "--environment", "prod")
    assert text.exit_code == 0 and text.stdout == "no difference\n"
    code, doc = _json(old, old, "--environment", "prod")
    assert code == 0 and doc["profile"] == {}


MACHINE = "insert_machine: m1\ntype: a\nmax_load: 10\n\n"
RESOURCE = "insert_resource: POOL\nres_type: R\namount: 1\n\n"
CYCLE = "cycle: cyc_q\nstart_date: 03/28/2026\nend_date: 04/02/2026\n\n"
XINST = "insert_xinst: PRD\nxtype: a\nxmachine: prd.example.com\n\n"


@pytest.mark.parametrize(
    ("old_extra", "new_extra", "kind", "expected"),
    [
        (MACHINE, MACHINE.replace("10", "20"), "machines", {**NONE, "changed": ["m1"]}),
        (RESOURCE, RESOURCE.replace("amount: 1", "amount: 2"), "resources", None),
        ("", "", "globals", {**NONE, "changed": ["LIMIT"]}),
        ("", CYCLE, "cycles", {**NONE, "added": ["cyc_q"]}),
        (XINST, "", "external_instances", {**NONE, "removed": ["PRD"]}),
    ],
    ids=["machine", "resource", "global", "cycle", "external-instance"],
)
def test_diff_reports_each_kind_by_the_seals_node_test(
    tmp_path: Path, old_extra: str, new_extra: str, kind: str, expected: object
) -> None:
    new_jobs = OLD_JOBS.replace("value: 5", "value: 6") if kind == "globals" else OLD_JOBS
    old = _release(tmp_path / "r1", old_extra + OLD_JOBS)
    new = _release(tmp_path / "r2", new_extra + new_jobs)
    code, doc = _json(old, new, "--environment", "uat")
    assert code == 1
    if expected is None:
        expected = {**NONE, "changed": ["POOL"], "referenced_by": {"POOL": []}}
    assert doc[kind] == expected
    others = [name for name, groups in doc.items() if name not in (kind, "profile")]
    for other in others:
        assert not any(doc[other][group] for group in ("added", "removed", "changed"))  # type: ignore[index]


def test_a_respelling_the_seal_ignores_is_no_difference(tmp_path: Path) -> None:
    """A machine's description, or its type in another case, moves no node the seal reads."""
    old = _release(tmp_path / "r1", MACHINE + OLD_JOBS)
    new = _release(
        tmp_path / "r2", MACHINE.replace("type: a", 'type: A\ndescription: "rack 4"') + OLD_JOBS
    )
    result = _invoke("estate", "diff", str(old), str(new), "--environment", "uat")
    assert result.exit_code == 0 and result.stdout == "no difference\n"


def test_a_changed_resource_lists_the_jobs_that_name_it(tmp_path: Path) -> None:
    holder = OLD_JOBS.replace(
        "~{$ROOT}~/edited.sh\n", "~{$ROOT}~/edited.sh\nresources: (POOL, QUANTITY=1, FREE=A)\n"
    )
    old = _release(tmp_path / "r1", RESOURCE + holder)
    new = _release(tmp_path / "r2", RESOURCE.replace("amount: 1", "amount: 2") + holder)
    result = _invoke("estate", "diff", str(old), str(new), "--environment", "uat")
    assert result.exit_code == 1
    assert result.stdout == "resources\n  changed POOL\n    referenced by edited\n"


def test_diff_reads_a_resolved_definition_through_the_environment_layers(tmp_path: Path) -> None:
    """A layer change moves every job whose command reads it: the fingerprint is taken after
    the placeholders are filled, as the seal's classifier takes it."""
    old = _release(tmp_path / "r1", OLD_JOBS)
    new = _release(tmp_path / "r2", OLD_JOBS)
    (new.parent / "layers" / "prod.properties").write_text("ROOT=/srv/prod\n")
    prod = _invoke("estate", "diff", str(old), str(new), "--environment", "prod")
    assert prod.exit_code == 1
    assert "  changed edited\n  changed gone\n  changed keep\n" in prod.stdout
    uat = _invoke("estate", "diff", str(old), str(new), "--environment", "uat")
    assert uat.exit_code == 0 and uat.stdout == "no difference\n"


def test_diff_applies_the_site_layers_last(tmp_path: Path) -> None:
    """The two releases' prod layers set ROOT apart, and a site layer that sets it too wins
    over both, so the difference goes away."""
    old = _release(tmp_path / "r1", OLD_JOBS)
    new = _release(tmp_path / "r2", OLD_JOBS)
    (new.parent / "layers" / "prod.properties").write_text("ROOT=/srv/prod\n")
    site = tmp_path / "site.properties"
    site.write_text("ROOT=/site\n")
    apart = _invoke("estate", "diff", str(old), str(new), "--environment", "prod")
    assert apart.exit_code == 1
    same = _invoke("estate", "diff", str(old), str(new), "--environment", "prod", "-p", str(site))
    assert same.exit_code == 0, same.stdout + same.stderr


def test_diff_reports_the_timezone_and_each_alias(tmp_path: Path) -> None:
    old = _release(tmp_path / "r1", OLD_JOBS)
    new = _release(tmp_path / "r2", OLD_JOBS)
    (new.parent / "tz.map").write_text(
        "Entry Type Zone\n----- ---- ----\nZRH zone Europe/Berlin\nNYC zone America/New_York\n"
    )
    new.write_text(new.read_text().replace('timezone = "UTC"', 'timezone = "Europe/Zurich"'))
    result = _invoke("estate", "diff", str(old), str(new), "--environment", "prod")
    assert result.exit_code == 1
    assert result.stdout == (
        "profile\n"
        "  timezone: UTC -> Europe/Zurich\n"
        "  timezone_map nyc: none -> America/New_York\n"
        "  timezone_map zrh: Europe/Zurich -> Europe/Berlin\n"
    )


def test_a_reformatted_map_and_an_explicit_utc_are_no_difference(tmp_path: Path) -> None:
    old = _release(tmp_path / "r1", OLD_JOBS)
    new = _release(tmp_path / "r2", OLD_JOBS)
    old.write_text(old.read_text().replace('timezone = "UTC"\n', ""))
    (new.parent / "tz.map").write_text(
        "Entry Type Zone\n----- ---- ----\nZRH   zone   Europe/Zurich\n"
    )
    result = _invoke("estate", "diff", str(old), str(new), "--environment", "prod")
    assert result.exit_code == 0 and result.stdout == "no difference\n"


def test_diff_refuses_a_map_that_does_not_parse(tmp_path: Path) -> None:
    old, new = _pair(tmp_path)
    (new.parent / "tz.map").write_text("ZRH\n")
    result = _invoke("estate", "diff", str(old), str(new), "--environment", "prod")
    assert result.exit_code == 2 and f"--timezone-map {new.parent}/tz.map" in result.stderr


def test_diff_takes_permit_unknown(tmp_path: Path) -> None:
    unknown = OLD_JOBS.replace("run_calendar: holidays\n", "run_calendar: holidays\nfancy: 1\n")
    old = _release(tmp_path / "r1", unknown)
    new = _release(tmp_path / "r2", unknown)
    refused = _invoke("estate", "diff", str(old), str(new), "--environment", "uat")
    assert refused.exit_code == 2 and "fancy" in refused.stderr
    taken = _invoke(
        "estate", "diff", str(old), str(new), "--environment", "uat", "--permit-unknown"
    )
    assert taken.exit_code == 0, taken.stderr


@pytest.mark.parametrize(
    ("environment", "fragment"),
    [
        (("--environment", "dev"), "no environment 'dev'; the manifest declares prod, uat"),
        ((), "choose an environment; the manifest declares prod, uat"),
    ],
)
def test_diff_refuses_an_environment_not_declared(
    tmp_path: Path, environment: tuple[str, ...], fragment: str
) -> None:
    old, new = _pair(tmp_path)
    result = _invoke("estate", "diff", str(old), str(new), *environment)
    assert result.exit_code == 2 and fragment in result.stderr


def test_diff_refuses_an_invalid_manifest_and_a_catalog_that_does_not_load(
    tmp_path: Path,
) -> None:
    old, new = _pair(tmp_path)
    new.write_text('python = "catalog.py"\n' + new.read_text())
    invalid = _invoke("estate", "diff", str(old), str(new), "--environment", "prod")
    assert invalid.exit_code == 2 and "unknown key python" in invalid.stderr
    broken = _release(tmp_path / "r3", "insert_job: x\nno_such_attribute: 1\n")
    refused = _invoke("estate", "diff", str(old), str(broken), "--environment", "prod")
    assert refused.exit_code == 2 and refused.stdout == ""
    assert f"{broken.parent}/jobs.jil:1: job_type is required" in refused.stderr


def test_diff_takes_relative_manifest_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = _invoke("estate", "diff", "r1/estate.toml", "r2/estate.toml", "--environment", "uat")
    assert result.exit_code == 1 and result.stdout.startswith("jobs\n")
