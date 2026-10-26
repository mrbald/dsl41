"""The estate manifest and `dsl41 estate args` (DL-407).

House style follows test_estate.py: every refusal asserts the message fragment that only its own
rule produces, and every gate has a passing counterpart beside it. The round trip feeds the
printed arguments to the real `lint` and `rehearse` on the nightbank small estate, and to
`seal`'s and `run`'s own parsers, because the claim is that the verbs take the printed list as
it stands.
"""

from __future__ import annotations

import json
import shlex
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer.main
from typer.testing import CliRunner

from dsl41 import estate_manifest
from dsl41.cli import app
from dsl41.estate_manifest import ArgsFor, ManifestError, build_args, load_manifest

from test_nightbank_example import SMALL, _launcher

runner = CliRunner()

MANIFEST = """\
name = "demo"
requires_dsl41 = ">=0.1"
files = ["b.jil", "a.jil"]
properties = ["base.properties"]
timezone = "Europe/Zurich"
timezone_map = "tz.map"

[semantics]
queued-recheck = "1"
dst-start-times = "fold0"

[environments.prod]
properties = ["prod.properties", "site.properties"]

[environments.dev]
properties = []
"""


def _estate(base: Path, text: str = MANIFEST) -> Path:
    """A release directory with every file MANIFEST names, and the manifest itself."""
    base.mkdir(parents=True, exist_ok=True)
    for name in ("a.jil", "b.jil", "base.properties", "prod.properties", "site.properties"):
        (base / name).write_text("")
    (base / "tz.map").write_text("ZRH Europe/Zurich\n")
    manifest = base / "estate.toml"
    manifest.write_text(text)
    return manifest


def _invoke(*args: str):
    return runner.invoke(app, list(args), catch_exceptions=False)


# ------------------------------------------------------------- the five forms


def test_each_for_form_builds_its_verbs_arguments(tmp_path: Path) -> None:
    manifest = _estate(tmp_path / "rel")
    d = str(tmp_path / "rel")
    files = [f"{d}/b.jil", f"{d}/a.jil"]
    layers = ["-p", f"{d}/base.properties"]
    sems = ["queued-recheck=1", "dst-start-times=fold0"]
    run = [
        *files,
        *layers,
        "--timezone",
        "Europe/Zurich",
        "--timezone-map",
        f"{d}/tz.map",
        "--semantics",
        sems[0],
        "--semantics",
        sems[1],
    ]
    assert build_args(manifest, ArgsFor.run) == run
    assert build_args(manifest, ArgsFor.rehearse) == run
    assert build_args(manifest, ArgsFor.lint) == run[:8]
    assert build_args(manifest, ArgsFor.viz) == run[:4]
    assert build_args(manifest, ArgsFor.seal) == [
        "--next",
        files[0],
        "--next",
        files[1],
        *layers,
        "--next-timezone",
        "Europe/Zurich",
        "--next-timezone-map",
        f"{d}/tz.map",
        "--next-semantics",
        sems[0],
        "--next-semantics",
        sems[1],
    ]


def test_optional_keys_left_out_add_no_option(tmp_path: Path) -> None:
    manifest = _estate(tmp_path, 'name = "bare"\nfiles = ["a.jil"]\n')
    file = str(tmp_path / "a.jil")
    for verb in ArgsFor:
        assert build_args(manifest, verb) == (["--next", file] if verb is ArgsFor.seal else [file])


def test_environment_layers_follow_the_manifests_own_in_order(tmp_path: Path) -> None:
    manifest = _estate(tmp_path)
    d = str(tmp_path)
    argv = build_args(manifest, ArgsFor.viz, "prod")
    assert argv[2:] == [
        "-p",
        f"{d}/base.properties",
        "-p",
        f"{d}/prod.properties",
        "-p",
        f"{d}/site.properties",
    ]
    # an environment with no layers of its own keeps the base layers only
    assert build_args(manifest, ArgsFor.viz, "dev") == build_args(manifest, ArgsFor.viz)


def test_paths_are_joined_as_given_without_resolving_symlinks(tmp_path: Path) -> None:
    """period-model ss1.1 hashes the path string, so a symlinked release directory must stay
    in the printed path."""
    real = _estate(tmp_path / "releases" / "r1").parent
    link = tmp_path / "current"
    link.symlink_to(real)
    argv = build_args(link / "estate.toml", ArgsFor.viz)
    assert argv[0] == str(link / "b.jil")
    assert str(real) not in " ".join(argv)


def test_a_relative_manifest_path_gives_relative_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _estate(tmp_path / "rel")
    monkeypatch.chdir(tmp_path)
    assert build_args(Path("rel/estate.toml"), ArgsFor.viz)[:2] == ["rel/b.jil", "rel/a.jil"]


# ------------------------------------------------------------- refusals


def _refusal(manifest: Path, verb: ArgsFor = ArgsFor.run, env: str | None = None) -> str:
    with pytest.raises(ManifestError) as caught:
        build_args(manifest, verb, env)
    message = str(caught.value)
    assert message.startswith(f"{manifest}: ") and "\n" not in message
    return message


def _edit(manifest: Path, old: str, new: str) -> None:
    text = manifest.read_text()
    assert old in text
    manifest.write_text(text.replace(old, new))


def test_a_missing_manifest_is_refused(tmp_path: Path) -> None:
    assert "cannot read the manifest" in _refusal(tmp_path / "estate.toml")


def test_bad_toml_is_refused(tmp_path: Path) -> None:
    manifest = _estate(tmp_path, 'name = "x\n')
    assert "not valid TOML" in _refusal(manifest)
    manifest.write_bytes(b'name = "\xff"\n')
    assert "not valid TOML" in _refusal(manifest)


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        ('name = "demo"', 'name = "demo"\nowner = "ops"', "unknown key owner"),
        (
            "[environments.dev]",
            "[environments.dev]\nfiles = []",
            "unknown key environments.dev.files",
        ),
        ('dst-start-times = "fold0"', "dst-start-times = 0", "semantics.dst-start-times:"),
        ('name = "demo"\n', "", "missing key name"),
        ('files = ["b.jil", "a.jil"]', "files = []", "files: List should have at least 1 item"),
        ('timezone = "Europe/Zurich"', 'timezone = ""', "timezone: String should have"),
    ],
    ids=["top", "nested", "type", "missing", "no-files", "empty-timezone"],
)
def test_the_schema_refuses_with_the_key_it_names(
    tmp_path: Path, old: str, new: str, fragment: str
) -> None:
    manifest = _estate(tmp_path)
    load_manifest(manifest)
    _edit(manifest, old, new)
    assert fragment in _refusal(manifest)


@pytest.mark.parametrize(
    "name", ["a.jil", "prod.properties", "tz.map"], ids=["file", "env-layer", "timezone-map"]
)
def test_a_named_file_that_is_absent_is_refused(tmp_path: Path, name: str) -> None:
    manifest = _estate(tmp_path)
    (tmp_path / name).unlink()
    # every environment's layers are checked, so the base build refuses too
    assert f"{name!r} is not a file under {tmp_path}" in _refusal(manifest)


def test_an_absolute_name_is_refused(tmp_path: Path) -> None:
    manifest = _estate(tmp_path)
    _edit(manifest, '"a.jil"]', f'"{tmp_path / "a.jil"}"]')
    assert "must be a path relative to the manifest" in _refusal(manifest)


def test_a_name_that_would_read_as_an_option_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _estate(tmp_path, 'name = "x"\nfiles = ["-x.jil"]\n')
    (tmp_path / "-x.jil").write_text("")
    assert build_args(tmp_path / "estate.toml", ArgsFor.viz) == [str(tmp_path / "-x.jil")]
    monkeypatch.chdir(tmp_path)
    assert "would reach the verb as an option" in _refusal(Path("estate.toml"))


def test_an_undeclared_environment_is_refused(tmp_path: Path) -> None:
    manifest = _estate(tmp_path)
    assert "no environment 'qa'; the manifest declares dev, prod" in _refusal(manifest, env="qa")
    bare = _estate(tmp_path / "bare", 'name = "x"\nfiles = ["a.jil"]\n')
    assert "no environment 'prod'; the manifest declares none" in _refusal(bare, env="prod")


@pytest.mark.parametrize(
    ("requirement", "installed", "fragment"),
    [
        (">=1.13", "1.12.0", "requires dsl41 >=1.13; this is dsl41 1.12.0"),
        (">=1.12.1", "1.12", "this is dsl41 1.12"),
        (">=0.1", "0+unknown", "this is dsl41 0+unknown"),
        (">=0.0.1", "unknown", "this is dsl41 unknown"),
        ("==1.13", "1.13.0", "supports only '>=', not '=='"),
        ("~=1.13", "1.13.0", "supports only '>=', not '~='"),
        ("> 1.13", "1.13.0", "supports only '>=', not '>'"),
        ("1.13", "1.13.0", "must read '>=X.Y' or '>=X.Y.Z'"),
        (">=1", "1.13.0", "must read '>=X.Y' or '>=X.Y.Z'"),
        (">=1.13rc1", "1.13.0", "must read '>=X.Y' or '>=X.Y.Z'"),
    ],
)
def test_requires_dsl41_refusals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requirement: str,
    installed: str,
    fragment: str,
) -> None:
    monkeypatch.setattr(estate_manifest, "tool_version", lambda: installed)
    manifest = _estate(tmp_path)
    _edit(manifest, 'requires_dsl41 = ">=0.1"', f'requires_dsl41 = "{requirement}"')
    assert fragment in _refusal(manifest)


@pytest.mark.parametrize(
    ("requirement", "installed"),
    [
        (">=1.13", "1.13.0"),
        (" >= 1.12.1 ", "1.13"),
        (">=1.13.0", "1.13.0.dev3+g1"),
        (">=1.9", "1.10"),
    ],
)
def test_requires_dsl41_met(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, requirement: str, installed: str
) -> None:
    monkeypatch.setattr(estate_manifest, "tool_version", lambda: installed)
    manifest = _estate(tmp_path)
    _edit(manifest, 'requires_dsl41 = ">=0.1"', f'requires_dsl41 = "{requirement}"')
    assert build_args(manifest, ArgsFor.viz)


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        ('queued-recheck = "1"', 'queued-recheck = "9"', "'9' is not one of 0, 1, 2"),
        ('queued-recheck = "1"', 'no-such = "1"', "unknown semantic switch 'no-such'"),
    ],
)
def test_bad_semantics_are_refused(tmp_path: Path, old: str, new: str, fragment: str) -> None:
    manifest = _estate(tmp_path)
    _edit(manifest, old, new)
    message = _refusal(manifest)
    assert "semantics: " in message and fragment in message


# ------------------------------------------------------------- the CLI


def test_cli_line_and_json_carry_the_same_argv(tmp_path: Path) -> None:
    (tmp_path / "with space").mkdir()
    manifest = _estate(tmp_path / "with space")
    for verb in ArgsFor:
        line = _invoke(
            "estate", "args", str(manifest), "--for", verb.value, "--environment", "prod"
        )
        doc = _invoke(
            "estate",
            "args",
            str(manifest),
            "--for",
            verb.value,
            "--environment",
            "prod",
            "--format",
            "json",
        )
        assert line.exit_code == 0 and doc.exit_code == 0, line.stderr + doc.stderr
        argv = json.loads(doc.stdout)["argv"]
        assert argv == build_args(manifest, verb, "prod")
        assert shlex.split(line.stdout) == argv
        assert line.stdout.count("\n") == 1


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (["--environment", "qa"], "no environment 'qa'"),
        (["--for", "run"], "unknown key owner"),
    ],
    ids=["environment", "schema"],
)
def test_cli_refuses_with_exit_2_and_one_line(
    tmp_path: Path, args: list[str], fragment: str
) -> None:
    manifest = _estate(tmp_path)
    if fragment.startswith("unknown key"):
        _edit(manifest, 'name = "demo"', 'name = "demo"\nowner = "ops"')
    result = _invoke("estate", "args", str(manifest), "--for", "run", *args)
    assert result.exit_code == 2
    assert result.stdout == ""
    assert fragment in result.stderr and result.stderr.count("\n") == 1


# ------------------------------------------------------------- the round trip


@pytest.fixture(scope="module")
def nightbank_release(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The small estate copied into a release directory beside its manifest, with the night's
    properties prepared by the launcher's own `prepare_night` at a fixed anchor."""
    base = tmp_path_factory.mktemp("release")
    _launcher().prepare_night(
        base, "small", anchor_utc=datetime(2026, 1, 6, 0, 5, tzinfo=UTC), incidents=False
    )
    names = [Path(path).name for path in SMALL]
    for path in SMALL:
        shutil.copy(path, base)
    manifest = base / "estate.toml"
    manifest.write_text(
        'name = "nightbank-small"\n'
        f"files = {json.dumps(names)}\n"
        'properties = ["night.properties"]\n'
        'timezone = "UTC"\n'
        '[semantics]\ndst-start-times = "fold0"\n'
    )
    return manifest


def _printed(manifest: Path, verb: str) -> list[str]:
    result = _invoke("estate", "args", str(manifest), "--for", verb)
    assert result.exit_code == 0, result.stderr
    return shlex.split(result.stdout)


def test_the_printed_lint_arguments_lint_clean(nightbank_release: Path) -> None:
    result = _invoke("lint", *_printed(nightbank_release, "lint"))
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout.strip() == ""


def test_the_printed_rehearse_arguments_rehearse(nightbank_release: Path) -> None:
    argv = _printed(nightbank_release, "rehearse")
    result = _invoke("rehearse", *argv, "--start", "2026-01-06T00:00:00", "--hours", "1")
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "APAC_EOD_B" in result.stdout


@pytest.mark.parametrize(
    ("verb", "extra", "keys"),
    [
        ("seal", ["--run-root", "r"], ("next_files", "next_timezone", "next_semantics")),
        ("run", ["--run-root", "r"], ("files", "timezone", "semantics")),
    ],
)
def test_the_printed_seal_and_run_arguments_parse(
    nightbank_release: Path, verb: str, extra: list[str], keys: tuple[str, str, str]
) -> None:
    """A dry parse through the verb's own parser: a full seal needs a live estate root, and the
    claim here is only that the parser takes the printed list as it stands."""
    command = typer.main.get_command(app).commands[verb]  # type: ignore[attr-defined]
    params = command.make_context(verb, [*_printed(nightbank_release, verb), *extra]).params
    files, timezone, semantics = (params[key] for key in keys)
    assert list(files) == [str(nightbank_release.parent / Path(p).name) for p in SMALL]
    assert list(params["properties"]) == [str(nightbank_release.parent / "night.properties")]
    assert timezone == "UTC"
    assert list(semantics) == ["dst-start-times=fold0"]
