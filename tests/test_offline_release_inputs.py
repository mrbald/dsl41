"""Frozen release exports govern uv-free target downloads (DL-397)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_offline_packaging import TOOLS, _candidate, _prepare, _sha, _tool, _wheel
from test_offline_packaging import python312 as python312

REPO = TOOLS.parents[1]


@pytest.fixture(scope="module")
def target_python(python312: str, tmp_path_factory: pytest.TempPathFactory) -> str:
    """The target interpreter supplies pip independently of the test runner (DL-389)."""
    target = tmp_path_factory.mktemp("offline-target-python") / "venv"
    subprocess.run(
        [python312, "-I", "-m", "venv", str(target)],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    return str(target / "bin/python")


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    repo = tmp_path / "checkout"
    shutil.copytree(TOOLS, repo / "scripts/offline", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(REPO / "scripts/export-release-inputs.sh", repo / "scripts/")
    deploy = repo / "examples/nightbank/deploy"
    deploy.mkdir(parents=True)
    for name in (
        "dsl41-launch",
        "dsl41-engine.service",
        "dsl41-supervisor.service",
        "nightbank-access.toml",
        "offline-local.sh",
    ):
        shutil.copy2(REPO / "examples/nightbank/deploy" / name, deploy / name)
    (repo / "pyproject.toml").write_text("""[project]
name = "dsl41"
version = "1.0.0"
requires-python = ">=3.12"
dependencies = ["offline-base-fixture==1.0.0; sys_platform != 'win32'"]
[project.optional-dependencies]
ui = ["textual==1.0.0", "textual-serve==1.0.0"]
[build-system]
requires = []
build-backend = "synthetic"
""")
    wheel_dir = tmp_path / "wheels"
    wheel_dir.mkdir()
    release = _wheel(wheel_dir, "dsl41")
    lock = """version = 1
revision = 3
requires-python = ">=3.12"

[[package]]
name = "dsl41"
version = "1.0.0"
source = { editable = "." }
dependencies = [{ name = "offline-base-fixture", marker = "sys_platform != 'win32'" }]
[package.optional-dependencies]
ui = [{ name = "textual" }, { name = "textual-serve" }]
[package.metadata]
requires-dist = [
  { name = "offline-base-fixture", marker = "sys_platform != 'win32'", specifier = "==1.0.0" },
  { name = "textual", marker = "extra == 'ui'", specifier = "==1.0.0" },
  { name = "textual-serve", marker = "extra == 'ui'", specifier = "==1.0.0" },
]
provides-extras = ["ui"]
"""
    index = tmp_path / "simple"
    pins = []
    for name in ("offline-base-fixture", "textual", "textual-serve"):
        wheel = _wheel(wheel_dir, name)
        project = index / name
        project.mkdir(parents=True)
        (project / "index.html").write_text(f'<a href="{wheel.as_uri()}">{wheel.name}</a>\n')
        lock += f'''
[[package]]
name = "{name}"
version = "1.0.0"
source = {{ registry = "https://pypi.org/simple" }}
wheels = [{{ url = "https://example.invalid/{wheel.name}", hash = "sha256:{_sha(wheel)}" }}]
'''
        marker = " ; sys_platform != 'win32'" if name == "offline-base-fixture" else ""
        pins.append(f"{name}==1.0.0{marker} \\\n    --hash=sha256:{_sha(wheel)}\n")
    (repo / "uv.lock").write_text(lock)
    exports = tmp_path / "exports"
    exports.mkdir()
    ignored = "windows-only-fixture==1.0.0 ; sys_platform == 'win32' --hash=sha256:" + "0" * 64
    (exports / "requirements-base.txt").write_text("# Frozen export\n" + pins[0] + ignored + "\n")
    (exports / "requirements-ui.txt").write_text(
        "# Frozen export\n" + "".join(pins) + ignored + "\n"
    )
    result = _tool(sys.executable, "release-inputs", repo, release, exports)
    assert result.returncode == 0, result.stderr
    return repo, release, exports, index


def _build(
    inputs: tuple[Path, Path, Path, Path],
    python: str,
    destination: Path,
    path: Path,
) -> subprocess.CompletedProcess[str]:
    repo, release, exports, index = inputs
    return subprocess.run(
        [
            "bash",
            str(repo / "scripts/offline/build-wheelhouse.sh"),
            str(release),
            str(exports),
            str(destination),
        ],
        env={
            **os.environ,
            "PYTHON": python,
            "PATH": f"{path}:/usr/bin:/bin",
            "DSL41_PYTHON_INDEX": index.as_uri(),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )


def _trap(directory: Path, name: str) -> Path:
    directory.mkdir(exist_ok=True)
    marker = directory / f"{name}-called"
    executable = directory / name
    executable.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 99\n")
    executable.chmod(0o755)
    return marker


def test_release_inputs_roundtrip_on_producer_python(
    inputs: tuple[Path, Path, Path, Path],
) -> None:
    repo, release, exports, _ = inputs
    result = _tool(sys.executable, "verify-inputs", repo, release, exports)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((exports / "release-inputs.json").read_text())
    assert manifest == {
        "schema": 1,
        "version": "1.0.0",
        "release_wheel": {"filename": release.name, "sha256": _sha(release)},
        "pyproject_sha256": _sha(repo / "pyproject.toml"),
        "uv_lock_sha256": _sha(repo / "uv.lock"),
        "requirements": {
            f"requirements-{profile}.txt": _sha(exports / f"requirements-{profile}.txt")
            for profile in ("base", "ui")
        },
    }
    before = (exports / "release-inputs.json").read_bytes()
    assert _tool(sys.executable, "release-inputs", repo, release, exports).returncode != 0
    assert (exports / "release-inputs.json").read_bytes() == before


@pytest.mark.parametrize(
    "fault",
    [
        "swapped",
        "missing-base",
        "missing-ui",
        "tampered-base",
        "tampered-ui",
        "missing-manifest",
        "wheel",
        "wheel-name",
        "project",
        "lock",
    ],
)
def test_builder_refuses_inputs_before_output_or_download(
    tmp_path: Path,
    target_python: str,
    inputs: tuple[Path, Path, Path, Path],
    fault: str,
) -> None:
    repo, release, exports, index = inputs
    base, ui = exports / "requirements-base.txt", exports / "requirements-ui.txt"
    if fault == "swapped":
        base_text, ui_text = base.read_bytes(), ui.read_bytes()
        base.write_bytes(ui_text)
        ui.write_bytes(base_text)
    elif fault.startswith("missing-"):
        (
            exports
            / (
                "release-inputs.json"
                if fault == "missing-manifest"
                else f"requirements-{fault.removeprefix('missing-')}.txt"
            )
        ).unlink()
    elif fault.startswith("tampered-"):
        with (exports / f"requirements-{fault.removeprefix('tampered-')}.txt").open("a") as stream:
            stream.write("# different bytes, same pins\n")
    elif fault == "wheel":
        release.write_bytes(release.read_bytes() + b"changed release bytes")
    elif fault == "wheel-name":
        renamed = release.with_name("dsl41-1.0.0-1-py3-none-any.whl")
        release.rename(renamed)
        inputs = repo, renamed, exports, index
    else:
        path = repo / ("pyproject.toml" if fault == "project" else "uv.lock")
        with path.open("a") as stream:
            stream.write("\n# same version, stale source bytes\n")
    trap = tmp_path / "bin"
    marker = _trap(trap, "uv")
    # pip's index is absent too: a reached download would fail independently (DL-397).
    shutil.rmtree(index)
    bundle = tmp_path / "refused-bundle"
    result = _build(inputs, target_python, bundle, trap)
    assert result.returncode != 0
    assert "refused" in result.stderr
    assert not bundle.exists()
    assert not marker.exists()


@pytest.mark.parametrize(
    "requirement",
    [
        "dependency @ https://example.invalid/x.whl",
        "--extra-index-url https://example.invalid",
        "-r other.txt",
        "dependency==1.0.0",
        "dependency==1.0.0 ; python_version >= '3.12' --find-links https://example.invalid",
        "Dsl41[ui]==1.0.0",
    ],
)
def test_unsafe_exports_refuse_creation_and_import_even_with_matching_manifest(
    tmp_path: Path,
    target_python: str,
    inputs: tuple[Path, Path, Path, Path],
    requirement: str,
) -> None:
    repo, release, exports, _ = inputs
    path = exports / "requirements-ui.txt"
    path.write_text(
        requirement + " --hash=sha256:" + "0" * 64 + "\n"
        if requirement != "dependency==1.0.0"
        else requirement + "\n"
    )
    manifest_path = exports / "release-inputs.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["requirements"][path.name] = _sha(path)
    manifest_path.unlink()
    refused = _tool(sys.executable, "release-inputs", repo, release, exports)
    assert refused.returncode != 0
    assert not manifest_path.exists()
    manifest_path.write_text(json.dumps(manifest))
    trap = tmp_path / "bin"
    marker = _trap(trap, "uv")
    bundle = tmp_path / "refused-bundle"
    result = _build(inputs, target_python, bundle, trap)
    assert result.returncode != 0
    assert not bundle.exists()
    assert not marker.exists()


@pytest.mark.parametrize("uv_present", [True, False])
def test_builder_downloads_and_installs_both_profiles_without_uv(
    tmp_path: Path,
    target_python: str,
    inputs: tuple[Path, Path, Path, Path],
    uv_present: bool,
) -> None:
    repo, release, exports, _ = inputs
    trap = tmp_path / "bin"
    trap.mkdir()
    marker = _trap(trap, "uv") if uv_present else trap / "uv-called"
    bundle = tmp_path / "bundle"
    originals = {path.name: path.read_bytes() for path in exports.iterdir()}
    built = _build(inputs, target_python, bundle, trap)
    assert built.returncode == 0, built.stdout + built.stderr
    assert not marker.exists()
    assert {path.name: path.read_bytes() for path in exports.iterdir()} == originals
    for name, contents in originals.items():
        assert (bundle / "release-inputs" / name).read_bytes() == contents
    assert (
        _tool(target_python, "verify-inputs", repo, release, bundle / "release-inputs").returncode
        == 0
    )
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert set(manifest["wheels"]) == {path.name for path in release.parent.glob("*.whl")}
    assert all(f"release-inputs/{name}" in manifest["files"] for name in originals)
    for profile in ("base", "ui"):
        requirement = bundle / f"requirements-{profile}.txt"
        assert requirement.read_bytes().startswith(originals[requirement.name])
        assert requirement.read_text().endswith(f"dsl41==1.0.0 --hash=sha256:{_sha(release)}\n")
        prefix = tmp_path / profile
        prepared = _prepare(target_python, bundle, prefix, profile)
        assert prepared.returncode == 0, prepared.stdout + prepared.stderr
        candidate = _candidate(target_python, bundle, prefix, profile)
        installed = subprocess.run(
            [
                str(candidate / "bin/python"),
                "-I",
                "-c",
                "import importlib.metadata as m,json; print(json.dumps(sorted(d.metadata['Name'] "
                "for d in m.distributions() if d.metadata['Name'] != 'pip')))",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        expected = ["dsl41", "offline-base-fixture"]
        if profile == "ui":
            expected += ["textual", "textual-serve"]
        assert json.loads(installed.stdout) == expected
    retained_export = bundle / "release-inputs/requirements-ui.txt"
    retained_export.write_bytes(retained_export.read_bytes() + b"# tampered export\n")
    assert _tool(target_python, "candidate", bundle, tmp_path / "refused", "base").returncode != 0


@pytest.mark.parametrize("missing", ["wheel", "exports", "invalid-exports"])
def test_native_harness_checks_inputs_before_any_container_work(
    tmp_path: Path,
    inputs: tuple[Path, Path, Path, Path],
    missing: str,
) -> None:
    repo, release, exports, _ = inputs
    trap = tmp_path / "bin"
    marker = _trap(trap, "podman")
    env = {key: value for key, value in os.environ.items() if not key.startswith("DSL41_")}
    env.update(PYTHON=sys.executable, PATH=f"{trap}:/usr/bin:/bin")
    if missing != "wheel":
        env["DSL41_RELEASE_WHEEL"] = str(release)
    if missing != "exports":
        env["DSL41_RELEASE_INPUTS"] = str(exports)
    if missing == "invalid-exports":
        (exports / "requirements-ui.txt").unlink()
    evidence = tmp_path / "evidence"
    result = subprocess.run(
        ["bash", str(repo / "examples/nightbank/deploy/offline-local.sh"), "ubuntu", str(evidence)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert not marker.exists()
    assert not evidence.exists()


def test_export_producer_uses_locked_uv_upstream_and_preserves_selected_wheels(
    tmp_path: Path,
    target_python: str,
    inputs: tuple[Path, Path, Path, Path],
) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("upstream producer needs uv; target importer tests do not")
    repo, release, frozen, index = inputs
    generated = tmp_path / "generated-exports"
    env = {key: value for key, value in os.environ.items() if not key.startswith("UV_")}
    env.update(
        PYTHON=sys.executable,
        UV_OFFLINE="1",
        UV_PYTHON_DOWNLOADS="never",
        UV_CACHE_DIR=str(tmp_path / "uv-cache"),
        UV_NO_CONFIG="1",
    )
    lock_before = (repo / "uv.lock").read_bytes()
    result = subprocess.run(
        ["bash", str(repo / "scripts/export-release-inputs.sh"), str(release), str(generated)],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "uv.lock").read_bytes() == lock_before
    assert _tool(sys.executable, "verify-inputs", repo, release, generated).returncode == 0
    assert "sys_platform != 'win32'" in (generated / "requirements-base.txt").read_text()
    for profile in ("base", "ui"):
        exported = (generated / f"requirements-{profile}.txt").read_text()
        assert str(repo) not in exported
        assert str(generated) not in exported
    trap = tmp_path / "bin"
    marker = _trap(trap, "uv")
    selected = []
    for exports in (frozen, generated):
        bundle = tmp_path / ("bundle-" + exports.name)
        result = _build((repo, release, exports, index), target_python, bundle, trap)
        assert result.returncode == 0, result.stdout + result.stderr
        selected.append({path.name: _sha(path) for path in (bundle / "wheels").iterdir()})
    assert selected[0] == selected[1]
    assert not marker.exists()
    # A same-version dependency edit requires a fresh lock, not a new input manifest (DL-397).
    project = repo / "pyproject.toml"
    project.write_text(project.read_text().replace("offline-base-fixture==", "different-fixture=="))
    stale_exports = tmp_path / "stale-exports"
    stale = subprocess.run(
        ["bash", str(repo / "scripts/export-release-inputs.sh"), str(release), str(stale_exports)],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert stale.returncode != 0
    assert not (stale_exports / "release-inputs.json").exists()
    assert (repo / "uv.lock").read_bytes() == lock_before


@pytest.mark.parametrize("fault", ["existing-output", "wrong-release"])
def test_export_producer_refuses_before_uv(
    tmp_path: Path,
    inputs: tuple[Path, Path, Path, Path],
    fault: str,
) -> None:
    repo, release, exports, _ = inputs
    before = {path.name: path.read_bytes() for path in exports.iterdir()}
    if fault == "wrong-release":
        (repo / "pyproject.toml").write_text('[project]\nname="dsl41"\nversion="2.0.0"\n')
        exports = tmp_path / "refused-exports"
    trap = tmp_path / "bin"
    marker = _trap(trap, "uv")
    result = subprocess.run(
        ["bash", str(repo / "scripts/export-release-inputs.sh"), str(release), str(exports)],
        env={**os.environ, "PYTHON": sys.executable, "PATH": f"{trap}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert not marker.exists()
    if fault == "wrong-release":
        assert not exports.exists()
    else:
        assert {path.name: path.read_bytes() for path in exports.iterdir()} == before


def test_target_builder_keeps_python312_qualification(
    tmp_path: Path,
    inputs: tuple[Path, Path, Path, Path],
) -> None:
    if sys.version_info[:2] == (3, 12):
        pytest.skip("producer Python equals target Python")
    trap = tmp_path / "bin"
    marker = _trap(trap, "uv")
    bundle = tmp_path / "refused-bundle"
    result = _build(inputs, sys.executable, bundle, trap)
    assert result.returncode != 0
    assert "Python 3.12" in result.stderr
    assert not bundle.exists()
    assert not marker.exists()


@pytest.mark.parametrize("mode", ["build", "qualify"])
def test_target_container_refuses_uv_on_path(
    tmp_path: Path,
    inputs: tuple[Path, Path, Path, Path],
    mode: str,
) -> None:
    repo, _, _, _ = inputs
    for args in (["init", "-q"], ["add", "pyproject.toml", "uv.lock", "scripts", "examples"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, timeout=30)
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    work = tmp_path / "work"
    script = tmp_path / "target.sh"
    source = (REPO / "examples/nightbank/deploy/offline-target.sh").read_text()
    # Fixed container roots map to disposable fixture roots (DL-397).
    for old, new in (
        ("/src", str(repo)),
        ("/delivery", str(delivery)),
        ("/work", str(work)),
        ("/opt/dsl41", str(tmp_path / "opt/dsl41")),
        ("/srv/dsl41", str(tmp_path / "srv/dsl41")),
        ("/tmp/dsl41", str(tmp_path / "tmp/dsl41")),
        ("/tmp/rpm-lifecycle", str(tmp_path / "tmp/rpm-lifecycle")),
    ):
        source = source.replace(old, new)
    script.write_text(source)
    trap = tmp_path / "bin"
    uv_called = _trap(trap, "uv")
    result = subprocess.run(
        ["bash", str(script), mode, "ubuntu"],
        env={**os.environ, "PATH": f"{trap}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "refused: uv found" in result.stderr
    recorded = (
        delivery / "uv-absence-build.txt"
        if mode == "build"
        else work / "evidence/uv-absence-qualification.txt"
    )
    assert str(trap / "uv") in recorded.read_text()
    assert not uv_called.exists()
    assert not (delivery / "bundle").exists()
