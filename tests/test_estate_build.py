"""`dsl41 estate build` and `dsl41 estate diff` (DL-413).

The source directory is corpus-shaped and synthetic: a manifest with two environments, a
calendar file and a layer copied as they are, and jobs.jil rendered from a Python module that
sits beside them and never enters the release. Every refusal asserts the fragment that only
its own rule prints, and leaves no release and no OUT.tmp behind.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41.cli import app

runner = CliRunner()

MANIFEST = """\
name = "demo"
files = ["calendars.jil", "jobs.jil"]
properties = ["layers/base.properties"]
timezone = "UTC"

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
    """The authoring directory: the manifest, the calendars, two layers and the module."""
    (base / "layers").mkdir(parents=True)
    (base / "estate.toml").write_text(manifest)
    (base / "calendars.jil").write_text(CALENDARS)
    (base / "layers" / "base.properties").write_text("ROOT=/opt/demo\n")
    (base / "layers" / "prod.properties").write_text("ROOT=/opt/prod\n")
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


def test_build_writes_the_release_prepare_takes(tmp_path: Path) -> None:
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
    ]
    assert files["jobs.jil"].decode() == built.stdout
    assert files["estate.toml"] == (source / "estate.toml").read_bytes()
    assert files["layers/prod.properties"] == b"ROOT=/opt/prod\n"
    assert not (tmp_path / "dist" / "v1.tmp").exists()
    for environment in ("prod", "uat"):
        args = _invoke(
            "estate", "args", str(out / "estate.toml"), "--for", "lint",
            "--environment", environment,
        )  # fmt: skip
        assert args.exit_code == 0, args.stderr


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
            lambda s: (s / "estate.toml").write_text(MANIFEST.replace("layers/", "../")),
            (),
            "must be a path relative to the manifest, with no '..' part",
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
        "dotdot",
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


@pytest.mark.parametrize("linked", ["layers/base.properties", "layers", "estate.toml"])
def test_build_refuses_a_symlinked_input(tmp_path: Path, linked: str) -> None:
    source = _source(tmp_path / "src")
    real = tmp_path / "real"
    (source / linked).rename(real)
    (source / linked).symlink_to(real)
    out = tmp_path / "v1"
    result = _build(source, out)
    assert result.exit_code == 2
    assert f"{source / linked} is a symlink; copy the file in" in result.stderr
    assert not out.exists()


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


def test_build_refuses_a_left_over_tmp(tmp_path: Path) -> None:
    source = _source(tmp_path / "src")
    (tmp_path / "v1.tmp").mkdir()
    result = _build(source, tmp_path / "v1")
    assert result.exit_code == 2 and "v1.tmp exists; remove it, then build again" in result.stderr
    assert not (tmp_path / "v1").exists()


def test_build_refuses_an_unreadable_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source(tmp_path / "src")
    read_bytes = Path.read_bytes

    def refuse_calendars(path: Path) -> bytes:
        if path.name == "calendars.jil":
            raise PermissionError(13, "Permission denied", str(path))
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", refuse_calendars)
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
    (out / "layers" / "prod.properties").unlink()
    (out / "layers" / "prod.properties").symlink_to(source / "layers" / "prod.properties")
    before = _files(out)
    result = _build(source, out, "--check")
    assert result.exit_code == 1
    assert result.stdout.splitlines() == [
        "missing  calendars.jil",
        "differs  jobs.jil",
        "differs  layers/prod.properties",
        "extra    notes.txt",
    ]
    assert _files(out) == before


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


def _release(base: Path, jobs: str, calendars: str = CALENDARS, extra: str = "") -> Path:
    """A release directory with its manifest; returns the manifest's path."""
    (base / "layers").mkdir(parents=True)
    (base / "estate.toml").write_text(MANIFEST.replace("[semantics]\n", f"[semantics]\n{extra}"))
    (base / "calendars.jil").write_text(calendars)
    (base / "jobs.jil").write_text(jobs)
    (base / "layers" / "base.properties").write_text("ROOT=/opt/demo\n")
    (base / "layers" / "prod.properties").write_text("ROOT=/opt/prod\n")
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
        "profile\n"
        "  semantics ice-lookback: true -> ordinary\n"
    )


def test_diff_json_is_one_object(tmp_path: Path) -> None:
    old, new = _pair(tmp_path)
    result = _invoke(
        "estate", "diff", str(old), str(new), "--environment", "uat", "--format", "json"
    )
    assert result.exit_code == 1, result.stderr
    none: dict[str, list[str]] = {"added": [], "removed": [], "changed": []}
    assert json.loads(result.stdout) == {
        "jobs": {"added": ["fresh"], "removed": ["gone"], "changed": ["edited"]},
        "calendars": {"added": [], "removed": [], "changed": ["holidays"]},
        "cycles": none,
        "resources": none,
        "machines": none,
        "globals": none,
        "external_instances": none,
        "profile": {"semantics": {"ice-lookback": {"old": "true", "new": "ordinary"}}},
    }


def test_diff_of_a_release_with_itself_is_empty(tmp_path: Path) -> None:
    old = _release(tmp_path / "r1", OLD_JOBS)
    text = _invoke("estate", "diff", str(old), str(old), "--environment", "prod")
    assert text.exit_code == 0 and text.stdout == "no difference\n"
    doc = _invoke("estate", "diff", str(old), str(old), "--environment", "prod", "--format", "json")
    assert doc.exit_code == 0
    assert json.loads(doc.stdout)["profile"] == {}


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
    old = _release(tmp_path / "r1", OLD_JOBS.replace("~{$ROOT}~/gone", "~{$SITE}~/gone"))
    new = _release(tmp_path / "r2", OLD_JOBS)
    unbound = _invoke("estate", "diff", str(old), str(new), "--environment", "uat")
    assert unbound.exit_code == 2 and "SITE" in unbound.stderr
    site = tmp_path / "site.properties"
    site.write_text("SITE=/opt/demo\n")
    bound = _invoke("estate", "diff", str(old), str(new), "--environment", "uat", "-p", str(site))
    assert bound.exit_code == 0, bound.stdout + bound.stderr


def test_diff_reports_the_timezone_and_the_map_contents(tmp_path: Path) -> None:
    old = _release(tmp_path / "r1", OLD_JOBS)
    new = _release(tmp_path / "r2", OLD_JOBS)
    (old.parent / "tz.map").write_text("ZRH Europe/Zurich\n")
    (new.parent / "tz.map").write_text("ZRH Europe/Berlin\n")
    old.write_text(old.read_text().replace('timezone = "UTC"', 'timezone_map = "tz.map"'))
    new.write_text(
        new.read_text().replace(
            'timezone = "UTC"', 'timezone = "Europe/Zurich"\ntimezone_map = "tz.map"'
        )
    )
    result = _invoke("estate", "diff", str(old), str(new), "--environment", "prod")
    assert result.exit_code == 1
    lines = result.stdout.splitlines()
    assert lines[:2] == ["profile", "  timezone: UTC -> Europe/Zurich"]
    assert lines[2].startswith("  timezone_map: sha256:") and len(lines) == 3


def test_diff_reports_a_map_added(tmp_path: Path) -> None:
    old = _release(tmp_path / "r1", OLD_JOBS)
    new = _release(tmp_path / "r2", OLD_JOBS)
    (new.parent / "tz.map").write_text("ZRH Europe/Zurich\n")
    new.write_text(new.read_text().replace('timezone = "UTC"', 'timezone_map = "tz.map"'))
    result = _invoke(
        "estate", "diff", str(old), str(new), "--environment", "prod", "--format", "json"
    )
    profile = json.loads(result.stdout)["profile"]
    assert result.exit_code == 1 and profile["timezone_map"]["old"] == "none"


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
