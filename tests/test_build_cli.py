"""`dsl41 build` and the decompiled module's shape: estate() returns a CatalogBuilder, importing
the module builds nothing, and the verb writes, prints or checks the JIL that estate() declares."""

from __future__ import annotations

import sys
from collections.abc import Callable
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
        "machine: ~{$HOST}~\n\n"
        "insert_job: ~{$PFX}~_report\njob_type: c\ncommand: ~{$BIN}~/report.sh ~{$DAY}~\n"
        'machine: ~{$HOST}~\ncondition: s(~{$PFX}~_load) & v(~{$VAR}~) = "~{$VAL}~"\n',
    )
    result = runner.invoke(app, ["decompile", "-o", str(tmp_path / "estate.py"), str(template)])
    assert result.exit_code == 0, result.stderr
    module = tmp_path / "estate.py"
    source = module.read_text(encoding="utf-8")
    for token in ("~{$PFX}~", "~{$HOST}~", "~{$BIN}~", "~{$DAY}~", "~{$VAR}~", "~{$VAL}~"):
        assert token in source
    assert """condition='s(~{$PFX}~_load) & v(~{$VAR}~) = "~{$VAL}~"'""" in source
    built = runner.invoke(app, ["build", str(module), "--stdout"])
    assert built.exit_code == 0, built.stderr
    assert built.stdout == template.read_text(encoding="utf-8")


def test_decompile_properties_help_warns_that_tokens_are_lost() -> None:
    result = runner.invoke(app, ["decompile", "--help"], env={"COLUMNS": "400"})
    assert "cannot recover the ~{$NAME}~ tokens" in result.stdout
    assert "decompile the template JIL to keep them" in result.stdout


def test_decompile_permit_unknown_reaches_the_check_the_footer_and_the_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    jil = "insert_job: a\njob_type: c\ncommand: x\nmachine: m1\nfrob: 1\n"
    source = _write(tmp_path, "unknown.jil", jil)
    result = runner.invoke(app, ["decompile", "--permit-unknown", str(source)])
    assert result.exit_code == 0, result.stderr
    module = result.stdout
    assert "passthrough={'frob': '1'}" in module
    assert module.startswith(
        "# Decompiled by dsl41 (phase 10). Write its JIL with: dsl41 build --permit-unknown"
    )
    assert "    main(estate, permit_unknown=True)\n" in module
    monkeypatch.setattr(sys, "argv", ["estate.py"])
    namespace: dict[str, object] = {"__name__": "__main__"}
    exec(compile(module, "<main>", "exec"), namespace)  # noqa: S102
    assert capsys.readouterr().out == jil
    plain = decompile(lower_catalog([parse_file(SOURCE_JIL)]))
    assert "--permit-unknown" not in plain and "    main(estate)\n" in plain


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


def test_build_check_marks_a_missing_final_newline(tmp_path: Path) -> None:
    module = _module(tmp_path)
    expected = _expected_jil(module)
    target = _write(tmp_path, "jobs.jil", expected.rstrip("\n").replace("job_b", "job_old"))
    result = runner.invoke(app, ["build", str(module), "--check", str(target)])
    assert result.exit_code == 1
    assert "\n\\ No newline at end of file\n" in result.stdout
    assert all(line.count("insert_job") <= 1 for line in result.stdout.splitlines())
    assert "only in line endings" not in result.stdout


@pytest.mark.parametrize(
    "variant",
    [
        lambda text: text.replace("\n", "\r\n"),
        lambda text: text.rstrip("\n"),
        lambda text: text + "\n",
    ],
    ids=["crlf", "no-final-newline", "extra-blank-line"],
)
def test_build_check_names_a_line_ending_only_difference(
    tmp_path: Path, variant: Callable[[str], str]
) -> None:
    module = _module(tmp_path)
    target = tmp_path / "jobs.jil"
    target.write_bytes(variant(_expected_jil(module)).encode("utf-8"))
    result = runner.invoke(app, ["build", str(module), "--check", str(target)])
    assert result.exit_code == 1
    assert result.stdout.endswith("The two differ only in line endings or trailing newlines.\n")


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


@pytest.mark.parametrize("where", ["at-import", "inside-estate"])
def test_build_imports_sibling_modules_and_restores_the_interpreter(
    tmp_path: Path, where: str
) -> None:
    """The second estate's sibling has the first one's name; it must load its own."""
    path_before = list(sys.path)
    for index, machine in enumerate(("m1", "m2")):
        directory = tmp_path / f"estate{index}"
        directory.mkdir()
        _write(directory, "train_shared.py", f"MACHINE = {machine!r}\n")
        (directory / "train_pkg").mkdir()
        _write(directory / "train_pkg", "__init__.py", "")
        _write(directory / "train_pkg", "values.py", f"COMMAND = 'run_{machine}'\n")
        importing = (
            "    from train_shared import MACHINE\n    from train_pkg.values import COMMAND\n"
        )
        module = _write(
            directory,
            "estate.py",
            "from dsl41.dsl import CatalogBuilder\n"
            + (importing.replace("    ", "") if where == "at-import" else "")
            + "\n\ndef estate():\n"
            + (importing if where == "inside-estate" else "")
            + "    return CatalogBuilder().job('a', command=COMMAND, machine=MACHINE)\n",
        )
        result = runner.invoke(app, ["build", str(module), "--stdout"])
        assert result.exit_code == 0, result.stderr
        assert f"command: run_{machine}\nmachine: {machine}\n" in result.stdout
        assert sorted(p.name for p in directory.iterdir()) == [
            "estate.py",
            "train_pkg",
            "train_shared.py",
        ]
        assert not (directory / "train_pkg" / "__pycache__").exists()
    assert sys.path == path_before
    assert not {"train_shared", "train_pkg", "train_pkg.values"} & set(sys.modules)
    assert not [name for name in sys.modules if name.startswith("dsl41_build_")]


def test_build_keeps_a_module_found_through_another_path_entry_below_the_estate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project's `.venv` or `src` below the estate's folder is not the estate's sibling."""
    library = tmp_path / "src"
    (library / "train_lib").mkdir(parents=True)
    _write(library / "train_lib", "__init__.py", "MACHINE = 'm1'\n")
    monkeypatch.syspath_prepend(str(library))
    module = _write(
        tmp_path,
        "estate.py",
        "from dsl41.dsl import CatalogBuilder\nfrom train_lib import MACHINE\n\n\n"
        "def estate():\n    return CatalogBuilder().job('a', command='x', machine=MACHINE)\n",
    )
    try:
        result = runner.invoke(app, ["build", str(module), "--stdout"])
        assert result.exit_code == 0, result.stderr
        assert "machine: m1\n" in result.stdout
        assert "train_lib" in sys.modules
    finally:
        sys.modules.pop("train_lib", None)


def test_build_runs_a_dataclass_under_postponed_annotations(tmp_path: Path) -> None:
    module = _write(
        tmp_path,
        "estate.py",
        "from __future__ import annotations\n\nfrom dataclasses import dataclass\n\n"
        "from dsl41.dsl import CatalogBuilder\n\n\n@dataclass\nclass Feed:\n"
        "    name: str\n    machine: str\n\n\ndef estate() -> CatalogBuilder:\n"
        "    feed = Feed('a', 'm1')\n"
        "    return CatalogBuilder().job(feed.name, command='x', machine=feed.machine)\n",
    )
    result = runner.invoke(app, ["build", str(module), "--stdout"])
    assert result.exit_code == 0, result.stderr
    assert "insert_job: a\n" in result.stdout
    assert not [name for name in sys.modules if name.startswith("dsl41_build_")]


# ------------------------------------------------- one module per file, and Params (DL-418)

#: Two template files already in the builder's layout, so a round trip is byte-equal; other
#: layouts are equal in canonical form only (see the corpus test below). b.jil's
#: report waits on a.jil's check: a condition across files.
TEMPLATE_A = (
    "insert_machine: ~{$HOST}~\n\n"
    "insert_job: ~{$PFX}~_box\njob_type: b\n\n"
    "insert_job: ~{$PFX}~_load\njob_type: c\nbox_name: ~{$PFX}~_box\n"
    "command: ~{$BIN}~/load.sh --day ~{$DAY}~\nmachine: ~{$HOST}~\n\n"
    "insert_job: ~{$PFX}~_check\njob_type: c\nbox_name: ~{$PFX}~_box\n"
    "command: ~{$BIN}~/check.sh\nmachine: ~{$HOST}~\ncondition: s(~{$PFX}~_load)\n"
)
TEMPLATE_B = (
    "insert_job: report\njob_type: c\ncommand: ~{$BIN}~/report.sh ~{$DAY}~\n"
    "machine: ~{$HOST}~\ncondition: s(~{$PFX}~_check)\n\n"
    "insert_job: mail\njob_type: c\ncommand: mail.sh\nmachine: ~{$HOST}~\n"
    "condition: s(report)\n"
)


def _templates(tmp_path: Path) -> tuple[Path, Path]:
    return _write(tmp_path, "a.jil", TEMPLATE_A), _write(tmp_path, "b.jil", TEMPLATE_B)


def _decompile(*args: str) -> None:
    result = runner.invoke(app, ["decompile", *args])
    assert result.exit_code == 0, result.stderr


def test_decompile_writes_one_module_per_file_and_build_renders_each_file(
    tmp_path: Path,
) -> None:
    a, b = _templates(tmp_path)
    package = tmp_path / "pkg"
    _decompile(str(b), str(a), "-o", f"{package}/")
    assert sorted(p.name for p in package.iterdir()) == ["__init__.py", "a.py", "b.py"]
    init = (package / "__init__.py").read_text(encoding="utf-8")
    assert "        'b.jil': b.estate(),\n        'a.jil': a.estate(),\n" in init
    assert "return CatalogBuilder.compose(files)\n" in init
    assert "condition='s(~{$PFX}~_check)'" in (package / "b.py").read_text(encoding="utf-8")
    checked = runner.invoke(app, ["build", str(package), "--check", str(tmp_path)])
    assert checked.exit_code == 0, checked.stderr
    out = tmp_path / "out"
    assert runner.invoke(app, ["build", str(package), "--out", str(out)]).exit_code == 0
    assert sorted(p.name for p in out.iterdir()) == ["a.jil", "b.jil"]
    assert (out / "a.jil").read_text(encoding="utf-8") == TEMPLATE_A
    assert (out / "b.jil").read_text(encoding="utf-8") == TEMPLATE_B
    whole = runner.invoke(app, ["build", str(package), "--stdout"])
    assert whole.stdout == TEMPLATE_B + "\n" + TEMPLATE_A
    alone = runner.invoke(app, ["build", str(package / "b.py"), "--stdout"])
    assert alone.exit_code == 0, alone.stderr
    assert alone.stdout == TEMPLATE_B
    assert not (package / "__pycache__").exists()
    assert not [name for name in sys.modules if name.startswith("dsl41_build_")]


def test_decompile_into_an_existing_directory_writes_a_package(tmp_path: Path) -> None:
    a, b = _templates(tmp_path)
    (tmp_path / "pkg").mkdir()
    _decompile(str(a), str(b), "-o", str(tmp_path / "pkg"))
    assert (tmp_path / "pkg" / "__init__.py").exists()


def test_build_check_of_a_package_diffs_each_stale_file(tmp_path: Path) -> None:
    a, b = _templates(tmp_path)
    _decompile(str(a), str(b), "-o", f"{tmp_path / 'pkg'}/")
    b.write_text(TEMPLATE_B.replace("mail.sh", "post.sh"), encoding="utf-8")
    result = runner.invoke(app, ["build", str(tmp_path / "pkg"), "--check", str(tmp_path)])
    assert result.exit_code == 1
    assert (
        f"--- {b}\n" in result.stdout and "-command: post.sh\n+command: mail.sh\n" in result.stdout
    )
    assert str(a) not in result.stdout
    as_file = runner.invoke(app, ["build", str(tmp_path / "pkg"), "--check", str(b)])
    assert as_file.exit_code == 2
    assert "declares 2 files; --check takes a directory" in as_file.stderr
    b.unlink()
    missing = runner.invoke(app, ["build", str(tmp_path / "pkg"), "--check", str(tmp_path)])
    assert missing.exit_code == 2


def test_decompile_package_names_each_module_after_its_file(tmp_path: Path) -> None:
    names = ["2nd-feed.jil", "estate.jil", "class.jil", "2nd_feed.jil"]
    paths = [
        _write(tmp_path, name, f"insert_job: j{i}\njob_type: c\ncommand: x\nmachine: m\n")
        for i, name in enumerate(names)
    ]
    package = tmp_path / "pkg"
    _decompile(*map(str, paths), "-o", f"{package}/")
    init = (package / "__init__.py").read_text(encoding="utf-8")
    for name, module in zip(names, ["jil_2nd_feed", "jil_estate", "jil_class", "jil_2nd_feed_2"]):
        assert f"        {name!r}: {module}.estate(),\n" in init
        assert (package / f"{module}.py").exists()
    result = runner.invoke(app, ["build", str(package), "--check", str(tmp_path)])
    assert result.exit_code == 0, result.stderr


@pytest.mark.parametrize(
    ("second", "name", "message"),
    [
        (
            "insert_job: j\njob_type: c\ncommand: x\nmachine: m\nbox_name: box1\n",
            "b.jil",
            "b.jil does not lower alone, and one module per file keeps each box",
        ),
        (
            "insert_job: j2\njob_type: c\ncommand: x\nmachine: m\n",
            "a.jil",
            "two inputs are named a.jil",
        ),
    ],
    ids=["box-split", "same-name"],
)
def test_decompile_package_refusals(tmp_path: Path, second: str, name: str, message: str) -> None:
    a = _write(tmp_path, "a.jil", "insert_job: box1\njob_type: b\n")
    other = tmp_path / "x"
    other.mkdir()
    b = _write(other, name, second)
    result = runner.invoke(app, ["decompile", str(a), str(b), "-o", f"{tmp_path / 'pkg'}/"])
    assert result.exit_code == 2
    assert message in result.stderr
    assert not (tmp_path / "pkg").exists()


def test_decompile_params_adds_a_dataclass_and_keeps_the_round_trip(tmp_path: Path) -> None:
    a, _ = _templates(tmp_path)
    module = tmp_path / "estate.py"
    _decompile("--params", str(a), "-o", str(module))
    source = module.read_text(encoding="utf-8")
    assert (
        "@dataclass(frozen=True)\nclass Params:\n"
        '    """Values for the ~{$NAME}~ placeholders. A field left None keeps its token."""\n\n'
        "    # binds command\n    BIN: str | None = None\n\n"
        "    # binds command\n    DAY: str | None = None\n\n"
        "    # binds insert_machine, machine\n    HOST: str | None = None\n\n"
        "    # binds box_name, condition, insert_job\n    PFX: str | None = None\n"
    ) in source
    assert "def estate(params: Params | None = None) -> CatalogBuilder:\n" in source
    assert "    return c.bind(params)\n" in source
    plain = runner.invoke(app, ["build", str(module), "--stdout"])
    assert plain.exit_code == 0, plain.stderr
    assert plain.stdout == TEMPLATE_A
    bound = runner.invoke(
        app, ["build", str(module), "--stdout", "--param", "HOST=h1", "--param", "PFX=n"]
    )
    assert bound.exit_code == 0, bound.stderr
    expected = TEMPLATE_A.replace("~{$HOST}~", "h1").replace("~{$PFX}~", "n")
    assert bound.stdout == expected
    assert "~{$BIN}~/load.sh --day ~{$DAY}~" in bound.stdout


def test_decompile_params_puts_one_dataclass_in_the_package(tmp_path: Path) -> None:
    a, b = _templates(tmp_path)
    package = tmp_path / "pkg"
    _decompile("--params", str(a), str(b), "-o", f"{package}/")
    init = (package / "__init__.py").read_text(encoding="utf-8")
    assert "class Params:" in init
    assert "    return CatalogBuilder.compose(files).bind(params)\n" in init
    assert "Params" not in (package / "a.py").read_text(encoding="utf-8")
    out = tmp_path / "out"
    result = runner.invoke(
        app, ["build", str(package), "--out", str(out), "--param", "PFX=n", "--param", "DAY=1"]
    )
    assert result.exit_code == 0, result.stderr
    assert (out / "b.jil").read_text(encoding="utf-8") == TEMPLATE_B.replace(
        "~{$PFX}~", "n"
    ).replace("~{$DAY}~", "1")
    checked = runner.invoke(app, ["build", str(package), "--check", str(tmp_path)])
    assert checked.exit_code == 0, checked.stderr


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--param", "NOPE=1"], "--param does not fit Params"),
        (["--param", "HOST"], "give NAME=VALUE"),
        (["--param", "HOST=a", "--param", "HOST=b"], "--param HOST: given twice"),
        (["--param", "HOST=a\nb"], "carries newline/control characters"),
    ],
    ids=["unknown", "form", "twice", "newline"],
)
def test_build_param_refusals(tmp_path: Path, args: list[str], message: str) -> None:
    a, _ = _templates(tmp_path)
    _decompile("--params", str(a), "-o", str(tmp_path / "estate.py"))
    result = runner.invoke(app, ["build", str(tmp_path / "estate.py"), "--stdout", *args])
    assert result.exit_code == 2
    assert message in result.stderr


def test_build_param_needs_a_params_class(tmp_path: Path) -> None:
    module = _module(tmp_path)
    result = runner.invoke(app, ["build", str(module), "--stdout", "--param", "HOST=h1"])
    assert result.exit_code == 2
    assert "--param needs a Params class" in result.stderr


def test_decompile_params_refuses_properties(tmp_path: Path) -> None:
    a, _ = _templates(tmp_path)
    layer = _write(tmp_path, "base.properties", "HOST=h\nPFX=p\nBIN=b\nDAY=d\n")
    result = runner.invoke(app, ["decompile", "--params", "-p", str(layer), str(a)])
    assert result.exit_code == 2
    assert "--params keeps the ~{$NAME}~ tokens, and -p binds them first" in result.stderr


def test_a_corpus_package_renders_each_file_canonically_equal_not_byte_equal(
    tmp_path: Path,
) -> None:
    """DL-418: the claim is canonical equality per file. These inputs carry comments and
    another attribute order, so `--check` against them differs."""
    inputs = [
        CORPUS_DIR / name
        for name in ("sem10_box_basic.jil", "comments_layout.jil", "sem08_globals.jil")
    ]
    package = tmp_path / "pkg"
    _decompile(*map(str, inputs), "-o", f"{package}/")
    out = tmp_path / "out"
    assert runner.invoke(app, ["build", str(package), "--out", str(out)]).exit_code == 0
    for path in inputs:
        rendered = lower_catalog([parse_file(out / path.name)])
        assert catalog_hash(rendered) == catalog_hash(lower_catalog([parse_file(path)]))
    stale = runner.invoke(app, ["build", str(package), "--check", str(CORPUS_DIR)])
    assert stale.exit_code == 1
    assert runner.invoke(app, ["build", str(package), "--check", str(out)]).exit_code == 0


def test_decompile_package_refuses_names_equal_ignoring_case(tmp_path: Path) -> None:
    jobs = "insert_job: j{}\njob_type: c\ncommand: x\nmachine: m\n"
    (tmp_path / "x").mkdir()
    first = _write(tmp_path, "Feed.jil", jobs.format(1))
    second = _write(tmp_path / "x", "feed.jil", jobs.format(2))
    result = runner.invoke(app, ["decompile", str(first), str(second), "-o", f"{tmp_path}/pkg/"])
    assert result.exit_code == 2
    assert "two inputs are named Feed.jil and feed.jil" in result.stderr
    assert not (tmp_path / "pkg").exists()


@pytest.mark.parametrize("name", ["class", "str", "dataclass", "Params", "estate", "__HOST"])
def test_decompile_params_refuses_a_name_that_cannot_be_a_field(tmp_path: Path, name: str) -> None:
    jil = _write(
        tmp_path, "a.jil", f"insert_job: j\njob_type: c\ncommand: x\nmachine: ~{{${name}}}~\n"
    )
    result = runner.invoke(app, ["decompile", "--params", str(jil)])
    assert result.exit_code == 2
    assert f"placeholder name(s) {name} cannot be Params fields" in result.stderr


def test_decompile_package_names_a_reference_in_another_file_or_in_none(tmp_path: Path) -> None:
    a = _write(tmp_path, "a.jil", "insert_job: a1\njob_type: c\ncommand: x\nmachine: m\n")
    b = _write(
        tmp_path,
        "b.jil",
        "insert_job: b1\njob_type: c\ncommand: x\nmachine: m\ncondition: s(a1)\n\n"
        "insert_job: b2\njob_type: c\ncommand: x\nmachine: m\ncondition: s(gone)\n",
    )
    result = runner.invoke(app, ["decompile", str(a), str(b), "-o", f"{tmp_path}/pkg/"])
    assert result.exit_code == 0, result.stderr
    assert "b.jil: explicit: job 'b1' -- references a job in another input file" in result.stderr
    assert "b.jil: explicit: job 'b2' -- references a job in no input file" in result.stderr


def test_build_refuses_a_statement_declared_after_compose(tmp_path: Path) -> None:
    module = _write(
        tmp_path,
        "estate.py",
        "from dsl41.dsl import CatalogBuilder\n\n\ndef estate():\n"
        "    part = CatalogBuilder().job('a', command='x', machine='m')\n"
        "    c = CatalogBuilder.compose({'a.jil': part})\n"
        "    return c.job('late', command='x', machine='m')\n",
    )
    result = runner.invoke(app, ["build", str(module), "--out", str(tmp_path / "out")])
    assert result.exit_code == 2
    assert "a statement declared after compose() belongs to no file" in result.stderr
    assert not (tmp_path / "out").exists()


def test_decompile_out_naming_a_file_writes_one_module(tmp_path: Path) -> None:
    a, b = _templates(tmp_path)
    target = _write(tmp_path, "estate.py", "stale\n")
    _decompile(str(a), str(b), "-o", str(target))
    assert "def estate() -> CatalogBuilder:" in target.read_text(encoding="utf-8")
    result = runner.invoke(app, ["decompile", str(a), str(b), "-o", f"{target}/"])
    assert result.exit_code == 2


def test_decompile_params_accepts_a_builtin_name_the_module_does_not_read(tmp_path: Path) -> None:
    jil = "insert_job: j\njob_type: c\ncommand: ~{$type}~\nmachine: ~{$id}~\n"
    module = tmp_path / "estate.py"
    _decompile("--params", str(_write(tmp_path, "a.jil", jil)), "-o", str(module))
    assert "    id: str | None = None\n" in module.read_text(encoding="utf-8")
    bound = runner.invoke(app, ["build", str(module), "--stdout", "--param", "id=m1"])
    assert bound.exit_code == 0, bound.stderr
    assert bound.stdout == jil.replace("~{$id}~", "m1")
