"""The estate manifest and the verbs' `--manifest` option (DL-407, DL-422).

House style follows test_estate.py: every refusal asserts the message fragment that only its own
rule produces, and every gate has a passing counterpart beside it. Each verb's test reads back
what the verb itself took in (lint's and viz's loaded catalog, rehearse's and run's profile,
seal's bundle) against literal expectations, never against a second builder of the same
arguments. One test seals and opens the same inputs by both spellings and compares the bundle
hash and the runtime profile.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from dsl41 import cli_compile, cli_run, estate_manifest
from dsl41.boundary import read_seal
from dsl41.cli import app
from dsl41.estate_manifest import ManifestError, load_manifest, resolve_manifest
from dsl41.period import (
    Manifest,
    bundle_sources,
    read_period_manifest,
    runtime_profile_from_cli,
    wal_path,
)

from test_estate import _native_root
from test_nightbank_example import SMALL, _launcher
from test_runner_leadership import engine

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


# ------------------------------------------------------------- each verb's inputs

#: The verbs' estate: job b reads one value from each layer, so the command it ends up with
#: names the layer that won each key. The base layer sets all three, the environment two, the
#: site's `-p` one.
JIL_A = "insert_job: a\njob_type: c\ncommand: sleep 600\n"
JIL_B = "insert_job: b\njob_type: c\ncommand: echo ~{$BASE}~ ~{$ENV}~ ~{$SITE}~\n"
VERB_MANIFEST = """\
name = "verbs"
files = ["b.jil", "a.jil"]
properties = ["base.properties"]
timezone = "Europe/Zurich"
timezone_map = "tz.map"

[semantics]
dst-start-times = "fold0"
queued-recheck = "1"

[environments.prod]
properties = ["prod.properties"]

[environments.dev]
properties = []
"""


def _release(base: Path, text: str = VERB_MANIFEST) -> tuple[Path, Path]:
    """A release directory holding the verbs' estate, and a site properties file outside it.
    Returns (manifest, site layer)."""
    base.mkdir(parents=True, exist_ok=True)
    (base / "a.jil").write_text(JIL_A)
    (base / "b.jil").write_text(JIL_B)
    (base / "base.properties").write_text("BASE=base\nENV=base\nSITE=base\n")
    (base / "prod.properties").write_text("ENV=prod\nSITE=prod\n")
    (base / "tz.map").write_text("ZRH Europe/Zurich\n")
    (base / "estate.toml").write_text(text)
    site = base.parent / "site.properties"
    site.write_text("SITE=site\n")
    return base / "estate.toml", site


def _manifest_words(manifest: Path, site: Path, environment: str = "prod") -> list[str]:
    return ["--manifest", str(manifest), "--environment", environment, "-p", str(site)]


class _Loads:
    """Every catalog a verb loaded: the files and the `-p` layers, as path strings, and the
    loaded catalog."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, module: Any, name: str) -> None:
        self.calls: list[tuple[list[str], list[str]]] = []
        self.catalogs: list[Any] = []
        real = getattr(module, name)

        def load(files: Any, permit_unknown: bool, properties: Any = None) -> Any:
            self.calls.append(([str(f) for f in files], [str(p) for p in properties or []]))
            loaded = real(files, permit_unknown, properties)
            self.catalogs.append(loaded[0] if isinstance(loaded, tuple) else loaded)
            return loaded

        monkeypatch.setattr(module, name, load)

    def command(self, job: str) -> str:
        [catalog] = self.catalogs
        return str(catalog.jobs[job].exec_.command)


def test_lint_takes_the_files_layers_timezone_and_map_from_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, site = _release(tmp_path / "with space")
    d = tmp_path / "with space"
    loads = _Loads(monkeypatch, cli_compile, "load_catalog_or_exit_2")
    seen: dict[str, Any] = {}
    real_lint = cli_compile.lint_catalog

    def lint_catalog(catalog: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real_lint(catalog, **kwargs)

    monkeypatch.setattr(cli_compile, "lint_catalog", lint_catalog)
    result = _invoke("lint", *_manifest_words(manifest, site))
    assert result.exit_code == 0, result.output
    assert loads.calls == [
        (
            [f"{d}/b.jil", f"{d}/a.jil"],
            [f"{d}/base.properties", f"{d}/prod.properties", str(site)],
        )
    ]
    assert loads.command("b") == "echo base prod site"
    assert seen == {"base_tz": "Europe/Zurich", "tz_aliases": {"zrh": "Europe/Zurich"}}


def test_an_environment_with_no_layers_keeps_the_base_layers_then_the_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, site = _release(tmp_path / "rel")
    loads = _Loads(monkeypatch, cli_compile, "load_catalog_or_exit_2")
    result = _invoke("lint", *_manifest_words(manifest, site, "dev"))
    assert result.exit_code == 0, result.output
    assert loads.calls[0][1] == [str(tmp_path / "rel" / "base.properties"), str(site)]
    assert loads.command("b") == "echo base base site"


def test_viz_takes_the_files_and_layers_from_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, site = _release(tmp_path / "rel")
    d = tmp_path / "rel"
    loads = _Loads(monkeypatch, cli_compile, "load_catalog_or_exit_2")
    result = _invoke("viz", "--format", "chart", *_manifest_words(manifest, site))
    assert result.exit_code == 0, result.output
    assert loads.calls == [
        (
            [f"{d}/b.jil", f"{d}/a.jil"],
            [f"{d}/base.properties", f"{d}/prod.properties", str(site)],
        )
    ]
    assert loads.command("b") == "echo base prod site"


def _expected_profile(**deployment: Any) -> Any:
    """The profile the verbs' estate pins: VERB_MANIFEST's zone, map and switches, typed out."""
    return runtime_profile_from_cli(
        timezone="Europe/Zurich",
        tz_aliases={"zrh": "Europe/Zurich"},
        semantics={"dst-start-times": "fold0", "queued-recheck": "1"},
        **deployment,
    )


def test_rehearse_pins_the_manifests_profile_and_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, site = _release(tmp_path / "rel")
    d = tmp_path / "rel"
    loads = _Loads(monkeypatch, cli_run, "load_catalog_and_ast_or_exit_2")
    root = tmp_path / "root"
    result = _invoke(
        "rehearse",
        *_manifest_words(manifest, site),
        "--run-root",
        str(root),
        "--start",
        "2026-01-06T00:00:00",
        "--hours",
        "1",
    )
    assert result.exit_code == 0, result.output
    assert loads.calls[0][1] == [f"{d}/base.properties", f"{d}/prod.properties", str(site)]
    pinned = read_period_manifest(root)
    assert pinned is not None
    assert pinned.runtime_profile == _expected_profile()
    sources = bundle_sources(root, pinned.source_bundle_hash)
    assert [source.path for source in sources] == [f"{d}/b.jil", f"{d}/a.jil"]
    assert "command: echo base prod site" in sources[0].text


def test_run_takes_the_manifests_profile_and_keeps_its_deployment_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, site = _release(tmp_path / "rel")
    d = tmp_path / "rel"
    loads = _Loads(monkeypatch, cli_run, "load_catalog_and_ast_or_exit_2")
    served: dict[str, Any] = {}

    async def serve(catalog: Any, run_root: Path, resume: bool, warns: Any, **kwargs: Any) -> int:
        served.update(kwargs, run_root=run_root)
        return 0

    monkeypatch.setattr(cli_run, "_serve_run", serve)
    root = tmp_path / "root"
    result = _invoke(
        "run",
        *_manifest_words(manifest, site),
        "--run-root",
        str(root),
        "--as-machine",
        "localhost",
    )
    assert result.exit_code == 0, result.output
    assert loads.calls[0][0] == [f"{d}/b.jil", f"{d}/a.jil"]
    assert loads.calls[0][1] == [f"{d}/base.properties", f"{d}/prod.properties", str(site)]
    assert loads.command("b") == "echo base prod site"
    assert served["profile"] == _expected_profile(as_machine=["localhost"])
    assert served["run_root"] == root


def test_a_manifest_that_omits_an_option_leaves_the_verbs_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An omitted field is the default, as an omitted option is: no zone, no map, no switch."""
    manifest, _ = _release(tmp_path / "rel", 'name = "bare"\nfiles = ["a.jil"]\n')
    served: dict[str, Any] = {}

    async def serve(*args: Any, **kwargs: Any) -> int:
        served.update(kwargs)
        return 0

    monkeypatch.setattr(cli_run, "_serve_run", serve)
    result = _invoke("run", "--manifest", str(manifest), "--run-root", str(tmp_path / "root"))
    assert result.exit_code == 0, result.output
    assert served["profile"] == runtime_profile_from_cli()
    loads = _Loads(monkeypatch, cli_compile, "load_catalog_or_exit_2")
    assert _invoke("lint", "--manifest", str(manifest)).exit_code == 0
    assert loads.calls == [([str(tmp_path / "rel" / "a.jil")], [])]


def _staged(root: Path) -> Manifest:
    """The manifest of the period ROOT's seal committed."""
    staged = read_period_manifest(root, 2)
    assert staged is not None
    return staged


def test_seal_stages_the_manifests_files_layers_and_profile(tmp_path: Path) -> None:
    manifest, site = _release(tmp_path / "rel")
    d = tmp_path / "rel"
    root = tmp_path / "root"
    _native_root(root, d / "a.jil")
    sealed = _invoke(
        "seal",
        "--run-root",
        str(root),
        *_manifest_words(manifest, site),
        "--next-as-machine",
        "localhost",
    )
    assert sealed.exit_code == 0, sealed.output
    staged = _staged(root)
    assert staged.runtime_profile == _expected_profile(as_machine=["localhost"])
    assert read_seal(root, 1).next_period.source_bundle_hash == staged.source_bundle_hash
    sources = bundle_sources(root, staged.source_bundle_hash)
    assert [source.path for source in sources] == [f"{d}/b.jil", f"{d}/a.jil"]
    assert "command: echo base prod site" in sources[0].text


def test_the_paths_are_the_manifests_directory_joined_as_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """period-model ss1.1 hashes each path string, so a symlinked release directory stays in the
    path the verb loads, and a name with a space reaches it as one path."""
    real = tmp_path / "releases" / "r 1"
    manifest, site = _release(real)
    link = tmp_path / "current"
    link.symlink_to(real)
    loads = _Loads(monkeypatch, cli_compile, "load_catalog_or_exit_2")
    result = _invoke("lint", *_manifest_words(link / manifest.name, site))
    assert result.exit_code == 0, result.output
    files, layers = loads.calls[0]
    assert files == [str(link / "b.jil"), str(link / "a.jil")]
    assert layers[:2] == [str(link / "base.properties"), str(link / "prod.properties")]
    assert str(real) not in " ".join(files + layers)


# ------------------------------------------------------------- the refusals


#: Each verb with what it needs besides the estate, and the options `--manifest` replaces.
VERBS: dict[str, tuple[list[str], list[list[str]]]] = {
    "lint": ([], [["FILE"], ["--timezone", "UTC"], ["--timezone-map", "MAP"]]),
    "viz": (["-o", "OUT"], [["FILE"]]),
    "rehearse": (
        ["--run-root", "ROOT"],
        [["FILE"], ["--timezone", "UTC"], ["--timezone-map", "MAP"], ["--semantics", "S"]],
    ),
    "run": (
        ["--run-root", "ROOT"],
        [["FILE"], ["--timezone", "UTC"], ["--timezone-map", "MAP"], ["--semantics", "S"]],
    ),
    "seal": (
        ["--run-root", "ROOT"],
        [
            ["--next", "FILE"],
            ["--next-timezone", "UTC"],
            ["--next-timezone-map", "MAP"],
            ["--next-semantics", "S"],
        ],
    ),
    "estate quiesce": (
        ["--run-root", "ROOT"],
        [
            ["--next", "FILE"],
            ["--next-timezone", "UTC"],
            ["--next-timezone-map", "MAP"],
            ["--next-semantics", "S"],
        ],
    ),
}


BARE_MANIFEST = """\
name = "bare"
files = ["b.jil", "a.jil"]

[environments.prod]
properties = ["prod.properties"]
"""


def _refused_before_any_write(
    tmp_path: Path, verb: str, words: list[str], sentence: str, text: str = VERB_MANIFEST
) -> None:
    """VERB with WORDS exits 2 with SENTENCE as its one line, and nothing appears on disk."""
    release = tmp_path / "rel"
    manifest, _ = _release(release, text)
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    extra, _ = VERBS[verb]
    names = {
        "FILE": str(release / "a.jil"),
        "MAP": str(release / "tz.map"),
        "S": "dst-start-times=fold0",
        "ROOT": str(tmp_path / "root"),
        "OUT": str(tmp_path / "out.md"),
        "MANIFEST": str(manifest),
    }
    argv = [names.get(word, word) for word in [*verb.split(), *extra, *words]]
    result = _invoke(*argv)
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert result.stderr == sentence + "\n"
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    ("verb", "beside"),
    [(verb, words) for verb, (_, replaced) in VERBS.items() for words in replaced],
    ids=[
        f"{verb.split()[-1]}-{words[0].lstrip('-').lower()}"
        for verb, (_, replaced) in VERBS.items()
        for words in replaced
    ],
)
def test_the_files_and_interpretation_options_are_refused_beside_the_manifest(
    tmp_path: Path, verb: str, beside: list[str]
) -> None:
    """Refused whether or not the manifest sets the value: a command-line value would give the
    opener a profile the seal did not state (DL-422)."""
    _refused_before_any_write(
        tmp_path,
        verb,
        ["--manifest", "MANIFEST", "--environment", "prod", *beside],
        "--manifest supplies the files and the interpretation options; give either.",
    )


@pytest.mark.parametrize(
    ("verb", "beside"),
    [
        (verb, words)
        for verb, (_, replaced) in VERBS.items()
        for words in replaced
        if words[0] not in ("FILE", "--next")
    ],
    ids=[
        f"{verb.split()[-1]}-{words[0].lstrip('-').lower()}"
        for verb, (_, replaced) in VERBS.items()
        for words in replaced
        if words[0] not in ("FILE", "--next")
    ],
)
def test_an_interpretation_option_is_refused_beside_a_manifest_that_omits_the_field(
    tmp_path: Path, verb: str, beside: list[str]
) -> None:
    """The omitted field is the manifest's authoritative default, so the option is refused as
    it is beside a manifest that sets the value (DL-422)."""
    _refused_before_any_write(
        tmp_path,
        verb,
        ["--manifest", "MANIFEST", "--environment", "prod", *beside],
        "--manifest supplies the files and the interpretation options; give either.",
        BARE_MANIFEST,
    )


@pytest.mark.parametrize("verb", list(VERBS))
def test_an_environment_without_a_manifest_is_refused(tmp_path: Path, verb: str) -> None:
    files = ["--next", "FILE"] if verb in ("seal", "estate quiesce") else ["FILE"]
    _refused_before_any_write(
        tmp_path, verb, [*files, "--environment", "prod"], "--environment needs --manifest."
    )


@pytest.mark.parametrize("verb", list(VERBS))
def test_neither_files_nor_a_manifest_is_refused(tmp_path: Path, verb: str) -> None:
    word = "--next" if verb in ("seal", "estate quiesce") else "FILES"
    _refused_before_any_write(tmp_path, verb, [], f"give {word} or --manifest.")


@pytest.mark.parametrize("verb", [verb for verb in VERBS if verb != "estate quiesce"])
@pytest.mark.parametrize(
    ("environment", "fragment"),
    [
        ([], "choose an environment; the manifest declares dev, prod"),
        (["--environment", "qa"], "no environment 'qa'; the manifest declares dev, prod"),
    ],
    ids=["unchosen", "undeclared"],
)
def test_an_environment_the_manifest_does_not_resolve_is_refused(
    tmp_path: Path, verb: str, environment: list[str], fragment: str
) -> None:
    """resolve_manifest's own sentence; quiesce reads the manifest in its seal stage."""
    manifest = tmp_path / "rel" / "estate.toml"
    _refused_before_any_write(
        tmp_path, verb, ["--manifest", "MANIFEST", *environment], f"{manifest}: {fragment}"
    )


@pytest.mark.parametrize("verb", list(VERBS))
def test_a_relative_manifest_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str
) -> None:
    """Two spellings of one manifest would join two path strings and two bundle hashes."""
    monkeypatch.chdir(tmp_path)
    _refused_before_any_write(
        tmp_path,
        verb,
        ["--manifest", "rel/estate.toml", "--environment", "prod"],
        "rel/estate.toml: give the manifest as an absolute path: the joined paths are hashed"
        " as given",
    )


def test_an_invalid_manifest_is_the_verbs_refusal(tmp_path: Path) -> None:
    manifest, site = _release(tmp_path / "rel")
    manifest.write_text('owner = "ops"\n' + manifest.read_text())
    result = _invoke("lint", *_manifest_words(manifest, site))
    assert result.exit_code == 2
    assert result.stderr == f"{manifest}: unknown key owner\n"


# ------------------------------------------------------------- one estate, two spellings


def test_a_manifest_and_its_explicit_arguments_seal_and_open_the_same_period(
    short_root: Path,
) -> None:
    """Root A seals through `--manifest`, root B through the same inputs typed out. Both stage
    one bundle hash and one profile, and each root opens under the other spelling, so the
    opener's path strings agree with the seal's (DL-422, period-model ss1.1)."""
    manifest, site = _release(short_root / "rel")
    d = short_root / "rel"
    files = [d / "b.jil", d / "a.jil"]
    layers = ["-p", f"{d}/base.properties", "-p", f"{d}/prod.properties", "-p", str(site)]
    zone = ["timezone", "Europe/Zurich", "timezone-map", f"{d}/tz.map"]
    switches = ["semantics", "dst-start-times=fold0", "semantics", "queued-recheck=1"]
    options = [*zone, *switches]

    def spelled(prefix: str) -> list[str]:
        return [f"{prefix}{word}" if i % 2 == 0 else word for i, word in enumerate(options)]

    roots = {"a": short_root / "a", "b": short_root / "b"}
    for root in roots.values():
        _native_root(root, d / "a.jil")
    by_manifest = _invoke("seal", "--run-root", str(roots["a"]), *_manifest_words(manifest, site))
    assert by_manifest.exit_code == 0, by_manifest.output
    next_files = [word for file in files for word in ("--next", str(file))]
    by_hand = _invoke(
        "seal", "--run-root", str(roots["b"]), *next_files, *layers, *spelled("--next-")
    )
    assert by_hand.exit_code == 0, by_hand.output
    staged_a, staged_b = _staged(roots["a"]), _staged(roots["b"])
    assert staged_a.source_bundle_hash == staged_b.source_bundle_hash
    assert staged_a.runtime_profile == staged_b.runtime_profile == _expected_profile()
    sealed_a, sealed_b = read_seal(roots["a"], 1).next_period, read_seal(roots["b"], 1).next_period
    assert sealed_a.source_bundle_hash == sealed_b.source_bundle_hash
    assert sealed_a.catalog_hash == sealed_b.catalog_hash
    assert sealed_a.runtime_hash == sealed_b.runtime_hash

    # each root opens under the other spelling
    openers = {
        "a": ([*layers, *spelled("--")], files),
        "b": (_manifest_words(manifest, site), []),
    }
    for name, (extra, opener_files) in openers.items():
        with engine(short_root, run_root=roots[name], resume=True, extra=extra, files=opener_files):
            pass
        assert wal_path(roots[name], 2).exists(), f"period 2 did not open in root {name}"


# ------------------------------------------------------------- the nightbank release


@pytest.fixture(scope="module")
def nightbank_release(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The small estate copied into a release directory beside its manifest, with the night's
    properties prepared by the launcher's own `prepare_night` at a fixed anchor. The prod layer
    restates one of the night's values, so it reaches the verbs without changing the estate."""
    base = tmp_path_factory.mktemp("release")
    _launcher().prepare_night(
        base, "small", anchor_utc=datetime(2026, 1, 6, 0, 5, tzinfo=UTC), incidents=False
    )
    names = [Path(path).name for path in SMALL]
    for path in SMALL:
        shutil.copy(path, base)
    (base / "prod.properties").write_text((base / "night.properties").read_text().splitlines()[0])
    (base / "tz-aliases").write_text("ZRH Europe/Zurich\n")
    manifest = base / "estate.toml"
    manifest.write_text(
        'name = "nightbank-small"\n'
        f"files = {json.dumps(names)}\n"
        'properties = ["night.properties"]\n'
        'timezone = "UTC"\n'
        'timezone_map = "tz-aliases"\n'
        '[semantics]\ndst-start-times = "fold0"\n'
        '[environments.prod]\nproperties = ["prod.properties"]\n'
    )
    return manifest


def test_the_nightbank_release_lints_clean_through_its_manifest(nightbank_release: Path) -> None:
    result = _invoke("lint", "--manifest", str(nightbank_release), "--environment", "prod")
    assert result.exit_code == 0, result.stdout + result.stderr
    assert result.stdout.strip() == ""


def test_the_nightbank_release_rehearses_through_its_manifest(nightbank_release: Path) -> None:
    result = _invoke(
        "rehearse",
        "--manifest",
        str(nightbank_release),
        "--environment",
        "prod",
        "--start",
        "2026-01-06T00:00:00",
        "--hours",
        "1",
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "APAC_EOD_B" in result.stdout


# ------------------------------------------------------------- manifest validation


def _refusal(manifest: Path, env: str | None = None) -> str:
    with pytest.raises(ManifestError) as caught:
        resolve_manifest(manifest, env)
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
    # tomllib recurses per nesting level, and deep nesting raises RecursionError
    manifest.write_text("a = " + "[" * 100_000 + "]" * 100_000 + "\n")
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


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"a.jil"]', '"ABS"]'),
        ('"a.jil"]', '"../rel/a.jil"]'),
        ('"a.jil"]', '"sub/../a.jil"]'),
        ('timezone_map = "tz.map"', 'timezone_map = ""'),
    ],
    ids=["absolute", "parent", "inner-parent", "empty"],
)
def test_a_name_outside_the_release_directory_is_refused(
    tmp_path: Path, old: str, new: str
) -> None:
    manifest = _estate(tmp_path / "rel")
    (tmp_path / "rel" / "sub").mkdir()
    _edit(manifest, old, new.replace("ABS", str(tmp_path / "rel" / "a.jil")))
    assert "must be a path relative to the manifest, with no '..' part" in _refusal(manifest)


def test_a_directory_named_as_a_file_is_refused(tmp_path: Path) -> None:
    manifest = _estate(tmp_path)
    (tmp_path / "sub").mkdir()
    _edit(manifest, '"a.jil"]', '"sub"]')
    assert f"'sub' is not a file under {tmp_path}" in _refusal(manifest)


@pytest.mark.parametrize(
    ("old", "new", "fragment"),
    [
        ('"a.jil"]', '"a.jil", "a.jil"]', "files names {d}/a.jil twice"),
        ('["prod.properties"', '["base.properties"', "properties names {d}/base.properties twice"),
        (
            '"prod.properties", "site.properties"]',
            '"prod.properties", "prod.properties"]',
            "properties names {d}/prod.properties twice",
        ),
    ],
    ids=["files", "base-and-environment", "environment"],
)
def test_a_repeated_path_is_refused(tmp_path: Path, old: str, new: str, fragment: str) -> None:
    manifest = _estate(tmp_path)
    # the same layer in two environments is not a repeat: one build sees one environment
    _edit(
        manifest,
        "[environments.dev]\nproperties = []",
        '[environments.dev]\nproperties = ["site.properties"]',
    )
    assert resolve_manifest(manifest, "dev")
    _edit(manifest, old, new)
    # every environment is checked, so the build for dev refuses a repeat in prod too
    assert fragment.format(d=tmp_path) in _refusal(manifest, env="dev")


def test_an_undeclared_or_missing_environment_is_refused(tmp_path: Path) -> None:
    manifest = _estate(tmp_path)
    assert "no environment 'qa'; the manifest declares dev, prod" in _refusal(manifest, env="qa")
    assert "choose an environment; the manifest declares dev, prod" in _refusal(manifest)
    bare = _estate(tmp_path / "bare", 'name = "x"\nfiles = ["a.jil"]\n')
    assert "no environment 'prod'; the manifest declares none" in _refusal(bare, env="prod")
    assert resolve_manifest(bare).files == (str(tmp_path / "bare" / "a.jil"),)


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
    assert resolve_manifest(manifest, "dev")


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


# ------------------------------------------------------------- DL-413: normal form, no Python


@pytest.mark.parametrize(
    "name",
    ["./a.jil", "sub//a.jil", "sub/", "sub/./a.jil", "."],
    ids=["dot-slash", "double-slash", "trailing-slash", "inner-dot", "dot"],
)
def test_a_name_not_in_normal_form_is_refused(tmp_path: Path, name: str) -> None:
    """DL-413: one file has one spelling, so `estate build --check` and the layer list see
    one name for it."""
    manifest = _estate(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.jil").write_text("")
    _edit(manifest, '"a.jil"]', f'"{name}"]')
    assert f"{name!r} must be a path relative to the manifest, with no '..' part, no '.'" in (
        _refusal(manifest)
    )


def test_a_timezone_map_not_in_normal_form_is_refused(tmp_path: Path) -> None:
    manifest = _estate(tmp_path)
    _edit(manifest, 'timezone_map = "tz.map"', 'timezone_map = "./tz.map"')
    assert "'./tz.map' must be a path relative to the manifest" in _refusal(manifest)


@pytest.mark.parametrize(
    ("old", "new", "key"),
    [
        ('"a.jil"]', '"a.py"]', "files"),
        ('properties = ["base.properties"]', 'properties = ["base.py"]', "properties"),
        ('"site.properties"]', '"site.py"]', "environments.prod.properties"),
        ('timezone_map = "tz.map"', 'timezone_map = "tz.py"', "timezone_map"),
    ],
    ids=["file", "base-layer", "environment-layer", "timezone-map"],
)
def test_a_python_input_is_refused(tmp_path: Path, old: str, new: str, key: str) -> None:
    """DL-410, DL-413: no Python enters a release; `estate build --emit` renders a module."""
    manifest = _estate(tmp_path)
    for name in ("a.py", "base.py", "site.py", "tz.py"):
        (tmp_path / name).write_text("def estate(): ...\n")
    _edit(manifest, old, new)
    message = _refusal(manifest)
    assert f"{key} names '{new.split(chr(34))[-2]}', a Python file; a release takes no Python" in (
        message
    )


def test_a_symlinked_manifest_is_refused_before_it_is_read(tmp_path: Path) -> None:
    real = _estate(tmp_path / "real")
    link = tmp_path / "real" / "linked.toml"
    link.symlink_to(real)
    assert "a symlink, not a file" in _refusal(link)


def test_a_fifo_manifest_is_refused_without_blocking(tmp_path: Path) -> None:
    _estate(tmp_path)
    fifo = tmp_path / "fifo.toml"
    os.mkfifo(fifo)
    assert "not a regular file" in _refusal(fifo)
