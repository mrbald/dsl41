"""`dsl41 build` and the decompiled module's shape: estate() returns a CatalogBuilder, importing
the module builds nothing, and the verb writes, prints or checks the JIL that estate() declares."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41.ast_jil import parse_file
from dsl41.cli import app
from dsl41.dsl import CatalogBuilder, decompile
from dsl41.equiv import catalog_hash
from dsl41.ir import lower_catalog, lower_source

CORPUS_DIR = Path(__file__).parent / "corpus"
SOURCE_JIL = CORPUS_DIR / "sem10_box_basic.jil"

runner = CliRunner()


def _module(tmp_path: Path, name: str = "estate.py") -> Path:
    path = tmp_path / name
    path.write_text(decompile(lower_catalog([parse_file(SOURCE_JIL)])), encoding="utf-8")
    return path


def _expected_jil(module: Path) -> str:
    namespace: dict[str, object] = {"__name__": "<expected>"}
    exec(compile(module.read_text(encoding="utf-8"), str(module), "exec"), namespace)  # noqa: S102
    builder = namespace["estate"]()  # type: ignore[operator]
    assert isinstance(builder, CatalogBuilder)
    return builder.to_jil()


def _write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


# ------------------------------------------------------------- the module's shape


def test_decompiled_module_builds_nothing_at_import(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse_build(self: CatalogBuilder, **kwargs: object) -> None:
        raise AssertionError("the module built a catalog at import")

    source = decompile(lower_catalog([parse_file(SOURCE_JIL)]))
    monkeypatch.setattr(CatalogBuilder, "build", refuse_build)
    namespace: dict[str, object] = {"__name__": "<import>"}
    exec(compile(source, "<import>", "exec"), namespace)  # noqa: S102
    assert "catalog" not in namespace and "c" not in namespace
    assert callable(namespace["estate"])
    assert "\ndef estate() -> CatalogBuilder:\n" in source


def test_decompile_without_properties_keeps_placeholder_tokens(tmp_path: Path) -> None:
    template = _write(
        tmp_path,
        "template.jil",
        "insert_job: ~{$PFX}~_load\njob_type: c\ncommand: ~{$BIN}~/load.sh --day ~{$DAY}~\n"
        "machine: ~{$HOST}~\n",
    )
    result = runner.invoke(app, ["decompile", "-o", str(tmp_path / "estate.py"), str(template)])
    assert result.exit_code == 0, result.stderr
    module = tmp_path / "estate.py"
    source = module.read_text(encoding="utf-8")
    for token in ("~{$PFX}~", "~{$HOST}~", "~{$BIN}~", "~{$DAY}~"):
        assert token in source
    built = runner.invoke(app, ["build", str(module), "--stdout"])
    assert built.exit_code == 0, built.stderr
    assert built.stdout == template.read_text(encoding="utf-8")


def test_decompile_properties_help_warns_that_tokens_are_lost() -> None:
    result = runner.invoke(app, ["decompile", "--help"], env={"COLUMNS": "400"})
    assert "cannot recover the ~{$NAME}~ tokens" in result.stdout
    assert "decompile the template JIL to keep them" in result.stdout


def test_decompile_check_honours_permit_unknown(tmp_path: Path) -> None:
    source = _write(
        tmp_path, "unknown.jil", "insert_job: a\njob_type: c\nmachine: m1\ncommand: x\nfrob: 1\n"
    )
    result = runner.invoke(app, ["decompile", "--permit-unknown", str(source)])
    assert result.exit_code == 0, result.stderr
    assert "passthrough={'frob': '1'}" in result.stdout


# ------------------------------------------------------------------ dsl41 build


def test_build_writes_the_jil_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    module = _module(tmp_path)
    target = tmp_path / "jobs.jil"
    target.write_text("stale\n", encoding="utf-8")
    result = runner.invoke(app, ["build", str(module), "-o", str(target)])
    assert result.exit_code == 0, result.stderr
    assert target.read_text(encoding="utf-8") == _expected_jil(module)
    original = lower_catalog([parse_file(SOURCE_JIL)])
    assert catalog_hash(lower_source(target.read_text(encoding="utf-8"))) == catalog_hash(original)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["estate.py", "jobs.jil"]


def test_build_stdout_prints_the_jil(tmp_path: Path) -> None:
    module = _module(tmp_path)
    result = runner.invoke(app, ["build", str(module), "--stdout"])
    assert result.exit_code == 0, result.stderr
    assert result.stdout == _expected_jil(module)


def test_build_check_passes_on_equal_bytes_and_writes_nothing(tmp_path: Path) -> None:
    module = _module(tmp_path)
    target = _write(tmp_path, "jobs.jil", _expected_jil(module))
    before = target.stat().st_mtime_ns
    result = runner.invoke(app, ["build", str(module), "--check", str(target)])
    assert result.exit_code == 0, result.stderr
    assert target.stat().st_mtime_ns == before


def test_build_check_prints_a_diff_and_exits_1_on_mismatch(tmp_path: Path) -> None:
    module = _module(tmp_path)
    stale = _expected_jil(module).replace("insert_job: job_a", "insert_job: job_old")
    target = _write(tmp_path, "jobs.jil", stale)
    result = runner.invoke(app, ["build", str(module), "--check", str(target)])
    assert result.exit_code == 1
    assert f"--- {target}\n" in result.stdout
    assert f"+++ {module} estate()\n" in result.stdout
    assert "-insert_job: job_old\n" in result.stdout
    assert "+insert_job: job_a\n" in result.stdout
    assert target.read_text(encoding="utf-8") == stale


def test_build_check_refuses_an_unreadable_file(tmp_path: Path) -> None:
    module = _module(tmp_path)
    result = runner.invoke(app, ["build", str(module), "--check", str(tmp_path / "absent.jil")])
    assert result.exit_code == 2


@pytest.mark.parametrize(
    "options", [[], ["--stdout", "--check", "x.jil"], ["-o", "a.jil", "--stdout"]]
)
def test_build_wants_exactly_one_destination(tmp_path: Path, options: list[str]) -> None:
    result = runner.invoke(app, ["build", str(_module(tmp_path)), *options])
    assert result.exit_code == 2
    assert "exactly one of --out, --check or --stdout" in result.stderr


def test_build_refuses_a_target_it_cannot_replace_and_cleans_up(tmp_path: Path) -> None:
    module = _module(tmp_path)
    (tmp_path / "jobs.jil").mkdir()
    (tmp_path / "jobs.jil" / "keep").write_text("x", encoding="utf-8")
    result = runner.invoke(app, ["build", str(module), "-o", str(tmp_path / "jobs.jil")])
    assert result.exit_code == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["estate.py", "jobs.jil"]


@pytest.mark.parametrize(
    ("name", "source", "message"),
    [
        ("estate.jil", "insert_job: a\n", "not a Python source file"),
        ("estate.py", "raise RuntimeError('boom')\n", "importing the module raised RuntimeError"),
        ("estate.py", "import sys\nsys.exit(3)\n", "importing the module raised SystemExit"),
        ("estate.py", "x = 1\n", "the module defines no estate() function"),
        ("estate.py", "estate = 1\n", "the module defines no estate() function"),
        ("estate.py", "def estate():\n    raise ValueError('bad')\n", "estate() raised ValueError"),
        ("estate.py", "def estate():\n    return 'jil'\n", "estate() returned str, not"),
        (
            "estate.py",
            "from dsl41.dsl import CatalogBuilder\n\n\ndef estate():\n"
            "    c = CatalogBuilder()\n"
            "    c.job('a', command='x', machine='m1', passthrough={'frob': '1'})\n"
            "    return c\n",
            "the built JIL does not lower",
        ),
    ],
)
def test_build_refuses_a_module_it_cannot_use(
    tmp_path: Path, name: str, source: str, message: str
) -> None:
    module = _write(tmp_path, name, source)
    result = runner.invoke(app, ["build", str(module), "-o", str(tmp_path / "jobs.jil")])
    assert result.exit_code == 2
    assert message in result.stderr
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "jobs.jil").exists()


def test_build_permit_unknown_carries_unknown_attributes(tmp_path: Path) -> None:
    module = _write(
        tmp_path,
        "estate.py",
        "from dsl41.dsl import CatalogBuilder\n\n\ndef estate():\n"
        "    c = CatalogBuilder()\n"
        "    c.job('a', command='x', machine='m1', passthrough={'frob': '1'})\n"
        "    return c\n",
    )
    result = runner.invoke(app, ["build", str(module), "--stdout", "--permit-unknown"])
    assert result.exit_code == 0, result.stderr
    assert "frob: 1\n" in result.stdout


def test_build_imports_sibling_modules_and_restores_sys_path(tmp_path: Path) -> None:
    _write(tmp_path, "train_shared.py", "MACHINE = 'm1'\n")
    module = _write(
        tmp_path,
        "estate.py",
        "from dsl41.dsl import CatalogBuilder\nfrom train_shared import MACHINE\n\n\n"
        "def estate():\n    return CatalogBuilder().job('a', command='x', machine=MACHINE)\n",
    )
    path_before = list(sys.path)
    try:
        result = runner.invoke(app, ["build", str(module), "--stdout"])
    finally:
        sys.modules.pop("train_shared", None)
    assert result.exit_code == 0, result.stderr
    assert "machine: m1\n" in result.stdout
    assert sys.path == path_before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["estate.py", "train_shared.py"]
