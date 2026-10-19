"""Offline preparation keeps activation and retained runtimes separate (DL-389)."""

from __future__ import annotations

import base64
import hashlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "scripts/offline"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def python312() -> str:
    python = shutil.which("python3.12")
    if python is None and sys.version_info[:2] == (3, 12):
        python = sys.executable
    if python is None:
        pytest.skip("offline target is system Python 3.12; this host does not provide it")
    return python


def _wheel(directory: Path, name: str, extra_metadata: str = "") -> Path:
    normalized = name.replace("-", "_")
    wheel = directory / f"{normalized}-1.0.0-py3-none-any.whl"
    info = f"{normalized}-1.0.0.dist-info"
    files = {
        f"{normalized}/__init__.py": b"# Synthetic packaging fixture.\n",
        f"{info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.0.0\n{extra_metadata}\n"
        ).encode(),
        f"{info}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    if name == "dsl41":
        files[f"{info}/entry_points.txt"] = b"[console_scripts]\ndsl41 = dsl41.cli:main\n"
        files["dsl41/cli.py"] = b"""import json
import pathlib
import sys

def main():
    args = sys.argv[1:]
    if args == ["--help"]:
        print("synthetic packaging CLI")
        return
    output = pathlib.Path(args[args.index("-o") + 1])
    if args[0] == "uc":
        output.write_text(json.dumps({"tool_version": "1.0.0", "records": ["synthetic"]}))
    elif args[0] == "viz":
        output.write_text("<!doctype html><html>cytoscape synthetic</html>")
    else:
        raise SystemExit(2)
"""
    record = "".join(
        f"{path},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip('=')},{len(data)}\n"
        for path, data in files.items()
    )
    files[f"{info}/RECORD"] = (record + f"{info}/RECORD,,\n").encode()
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return wheel


def _tool(python: str, *args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [python, "-I", str(TOOLS / "bundle.py"), *map(str, args)],
        text=True,
        capture_output=True,
        timeout=90,
    )


def _bundle(tmp_path: Path, python: str, *, missing_dependency: bool = False) -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "wheels").mkdir(parents=True)
    (bundle / "assets").mkdir()
    (bundle / "assets/dsl41-launch").write_text("synthetic deployment template\n")
    shutil.copytree(TOOLS, bundle / "tools", ignore=shutil.ignore_patterns("__pycache__"))
    extra = "Requires-Dist: absent-offline-fixture==99\n" if missing_dependency else ""
    wheels = [
        _wheel(bundle / "wheels", name, extra if name == "dsl41" else "")
        for name in ("dsl41", "textual", "textual-serve")
    ]
    (bundle / "pyproject.toml").write_text('[project]\nname="dsl41"\nversion="1.0.0"\n')
    lock = "version = 1\n"
    for name, wheel in zip(("dsl41", "textual", "textual-serve"), wheels, strict=True):
        lock += f'''\n[[package]]
name = "{name}"
version = "1.0.0"
wheels = [{{ url = "https://example.invalid/{wheel.name}", hash = "sha256:{_sha(wheel)}" }}]
'''
    (bundle / "uv.lock").write_text(lock)
    requirements = [
        f"{name}==1.0.0 --hash=sha256:{_sha(wheel)}\n"
        for name, wheel in zip(("dsl41", "textual", "textual-serve"), wheels, strict=True)
    ]
    (bundle / "requirements-base.txt").write_text(requirements[0])
    (bundle / "requirements-ui.txt").write_text("".join(requirements))
    result = _tool(python, "manifest", bundle, wheels[0])
    assert result.returncode == 0, result.stderr
    return bundle


def _prepare(
    python: str, bundle: Path, prefix: Path, profile: str = "base", **env: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(TOOLS / "prepare.sh"), str(bundle), str(prefix), profile],
        env={**os.environ, "PYTHON": python, **env},
        text=True,
        capture_output=True,
        timeout=120,
    )


def _candidate(python: str, bundle: Path, prefix: Path, profile: str = "base") -> Path:
    result = _tool(python, "candidate", bundle, prefix.resolve(), profile)
    assert result.returncode == 0, result.stderr
    return Path(result.stdout.strip())


@pytest.mark.parametrize("profile", ["base", "ui"])
def test_offline_prepare_repeat_retains_active_runtime(
    tmp_path: Path, python312: str, profile: str
) -> None:
    bundle = _bundle(tmp_path, python312)
    prefix = tmp_path / "custom prefix"
    retained = prefix / ".venvs/retained"
    retained.mkdir(parents=True)
    (retained / "sentinel").write_text("retained runtime")
    (prefix / ".venv").symlink_to(".venvs/retained")
    result = _prepare(python312, bundle, prefix, profile)
    assert result.returncode == 0, result.stderr
    candidate = _candidate(python312, bundle, prefix, profile)
    assert candidate == Path(result.stdout.splitlines()[-1])
    assert (candidate / "bin/dsl41").is_file()
    receipt = candidate / ".dsl41/receipt.json"
    before = receipt.read_bytes()
    assert (candidate / ".dsl41/assets/dsl41-launch").read_text().startswith("synthetic")
    assert json.loads(before)["manifest_sha256"] == _sha(bundle / "manifest.json")
    repeated = _prepare(python312, bundle, prefix, profile)
    assert repeated.returncode == 0, repeated.stderr
    assert receipt.read_bytes() == before
    assert (retained / "sentinel").read_text() == "retained runtime"
    assert os.readlink(prefix / ".venv") == ".venvs/retained"


def test_offline_prepare_serializes_concurrent_candidates(tmp_path: Path, python312: str) -> None:
    bundle = _bundle(tmp_path, python312)
    prefix = tmp_path / "prefix"
    processes = [
        subprocess.Popen(
            ["bash", str(TOOLS / "prepare.sh"), str(bundle), str(prefix)],
            env={**os.environ, "PYTHON": python312},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    try:
        outputs = [process.communicate(timeout=120) for process in processes]
        for process, (_, error) in zip(processes, outputs, strict=True):
            assert process.returncode == 0, error
        assert outputs[0][0].splitlines()[-1] == outputs[1][0].splitlines()[-1]
        assert len(list((prefix / ".venvs").iterdir())) == 1
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)


@pytest.mark.parametrize("fault", ["missing-wheel", "changed-wheel", "changed-template"])
def test_offline_prepare_refuses_changed_delivery(
    tmp_path: Path, python312: str, fault: str
) -> None:
    bundle = _bundle(tmp_path, python312)
    wheel = next((bundle / "wheels").glob("dsl41-*.whl"))
    if fault == "missing-wheel":
        wheel.unlink()
    elif fault == "changed-wheel":
        wheel.write_bytes(wheel.read_bytes() + b"changed")
    else:
        (bundle / "assets/dsl41-launch").write_text("changed template")
    prefix = tmp_path / "prefix"
    result = _prepare(python312, bundle, prefix)
    assert result.returncode != 0
    assert "inventory" in result.stderr
    assert not list((prefix / ".venvs").iterdir())


def test_offline_prepare_refuses_partial_candidate_without_mutation(
    tmp_path: Path, python312: str
) -> None:
    bundle = _bundle(tmp_path, python312)
    prefix = tmp_path / "prefix"
    candidate = _candidate(python312, bundle, prefix)
    candidate.mkdir(parents=True)
    (candidate / "incomplete").write_text("preserve for inspection")
    result = _prepare(python312, bundle, prefix)
    assert result.returncode != 0
    assert "No retained candidate was removed or repaired" in result.stderr
    assert list(candidate.iterdir()) == [candidate / "incomplete"]
    assert (candidate / "incomplete").read_text() == "preserve for inspection"


@pytest.mark.parametrize("fault", ["source", "script", "added-pth", "bytecode", "receipt"])
def test_offline_prepare_repeat_checks_installed_integrity(
    tmp_path: Path, python312: str, fault: str
) -> None:
    bundle = _bundle(tmp_path, python312)
    prefix = tmp_path / "prefix"
    first = _prepare(python312, bundle, prefix)
    assert first.returncode == 0, first.stderr
    candidate = _candidate(python312, bundle, prefix)
    site = next((candidate / "lib").glob("python*/site-packages"))
    source = site / "dsl41/__init__.py"
    if fault == "source":
        source.write_text("raise RuntimeError('changed installed source')\n")
    elif fault == "script":
        (candidate / "bin/dsl41").write_text("#!/bin/sh\nexit 0\n")
    elif fault == "added-pth":
        (site / "injected.pth").write_text("/unexpected/import/path\n")
    elif fault == "bytecode":
        subprocess.run(
            [
                python312,
                "-I",
                "-c",
                "import py_compile,sys; py_compile.compile(sys.argv[1])",
                str(source),
            ],
            check=True,
            timeout=30,
        )
        cache = next((site / "dsl41/__pycache__").glob("__init__.*.pyc"))
        cache.write_bytes(cache.read_bytes()[:16] + b"invalid-bytecode")
    else:
        (candidate / ".dsl41/receipt.json").write_text("{}")
    receipt_before = (candidate / ".dsl41/receipt.json").read_bytes()
    repeated = _prepare(python312, bundle, prefix)
    assert repeated.returncode != 0
    assert (candidate / ".dsl41/receipt.json").read_bytes() == receipt_before


def test_offline_prepare_ignores_pip_environment_and_config(tmp_path: Path, python312: str) -> None:
    bundle = _bundle(tmp_path, python312)
    redirected = tmp_path / "redirected"
    injection = tmp_path / "python-injection"
    injection.mkdir()
    marker = tmp_path / "python-override-ran"
    (injection / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('override ran')\n"
    )
    config = tmp_path / "pip.conf"
    config.write_text(f"[global]\ntarget = {redirected}\nindex-url = http://127.0.0.1:9\n")
    result = _prepare(
        python312,
        bundle,
        tmp_path / "prefix",
        PIP_CONFIG_FILE=str(config),
        PIP_TARGET=str(redirected),
        PIP_INDEX_URL="http://127.0.0.1:9",
        PIP_FIND_LINKS="http://127.0.0.1:9",
        PYTHONPATH=str(injection),
    )
    assert result.returncode == 0, result.stderr
    assert not redirected.exists()
    assert not marker.exists()


def test_offline_pip_check_failure_never_writes_receipt(tmp_path: Path, python312: str) -> None:
    bundle = _bundle(tmp_path, python312, missing_dependency=True)
    prefix = tmp_path / "prefix"
    result = _prepare(python312, bundle, prefix)
    assert result.returncode != 0
    candidate = _candidate(python312, bundle, prefix)
    assert (candidate / "bin/python").exists()
    assert not (candidate / ".dsl41/receipt.json").exists()
    assert "absent-offline-fixture" in result.stdout
    repeated = _prepare(python312, bundle, prefix)
    assert repeated.returncode != 0
    assert not (candidate / ".dsl41/receipt.json").exists()


def test_offline_no_network_or_cache_fallback_for_missing_pin(
    tmp_path: Path, python312: str
) -> None:
    bundle = _bundle(tmp_path, python312)
    (bundle / "requirements-base.txt").write_text(
        "absent-offline-fixture==99 --hash=sha256:" + "0" * 64 + "\n"
    )
    release = next((bundle / "wheels").glob("dsl41-*.whl"))
    assert _tool(python312, "manifest", bundle, release).returncode == 0
    prefix = tmp_path / "prefix"
    result = _prepare(python312, bundle, prefix, PIP_INDEX_URL="http://127.0.0.1:9")
    assert result.returncode != 0
    assert "No matching distribution found" in result.stderr
    assert "Retrying" not in result.stderr
    assert not (_candidate(python312, bundle, prefix) / ".dsl41/receipt.json").exists()


def test_offline_refuses_redirected_venvs(tmp_path: Path, python312: str) -> None:
    bundle = _bundle(tmp_path, python312)
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (prefix / ".venvs").symlink_to(other, target_is_directory=True)
    result = _prepare(python312, bundle, prefix)
    assert result.returncode != 0
    assert ".venvs must be a plain directory" in result.stderr
    assert not list(other.iterdir())


@pytest.mark.parametrize(
    "requirement",
    [
        "dsl41 @ https://example.invalid/dsl41.whl --hash=sha256:" + "0" * 64,
        "--find-links https://example.invalid/wheels",
        "-r /unexpected/requirements.txt",
        "dsl41==1.0.0 ; python_version == '3.12' --index-url https://example.invalid --hash=sha256:"
        + "0" * 64,
    ],
)
def test_offline_refuses_requirement_network_escape(
    tmp_path: Path, python312: str, requirement: str
) -> None:
    bundle = _bundle(tmp_path, python312)
    (bundle / "requirements-base.txt").write_text(requirement + "\n")
    release = next((bundle / "wheels").glob("dsl41-*.whl"))
    assert _tool(python312, "manifest", bundle, release).returncode == 0
    prefix = tmp_path / "prefix"
    result = _prepare(python312, bundle, prefix)
    assert result.returncode != 0
    assert "only hashed package==version pins" in result.stderr
    assert not list((prefix / ".venvs").iterdir())


def test_offline_smoke_failure_never_writes_receipt(tmp_path: Path, python312: str) -> None:
    bundle = _bundle(tmp_path, python312)
    (bundle / "requirements-ui.txt").write_text((bundle / "requirements-base.txt").read_text())
    release = next((bundle / "wheels").glob("dsl41-*.whl"))
    assert _tool(python312, "manifest", bundle, release).returncode == 0
    prefix = tmp_path / "prefix"
    result = _prepare(python312, bundle, prefix, "ui")
    assert result.returncode != 0
    assert not (_candidate(python312, bundle, prefix, "ui") / ".dsl41/receipt.json").exists()


def test_offline_repeat_accepts_ordinary_bytecode_cache(tmp_path: Path, python312: str) -> None:
    bundle = _bundle(tmp_path, python312)
    prefix = tmp_path / "prefix"
    first = _prepare(python312, bundle, prefix)
    assert first.returncode == 0, first.stderr
    candidate = _candidate(python312, bundle, prefix)
    source = next((candidate / "lib").glob("python*/site-packages/dsl41/__init__.py"))
    subprocess.run(
        [
            python312,
            "-I",
            "-c",
            "import py_compile,sys; py_compile.compile(sys.argv[1])",
            str(source),
        ],
        check=True,
        timeout=30,
    )
    repeated = _prepare(python312, bundle, prefix)
    assert repeated.returncode == 0, repeated.stderr


def test_offline_repeat_after_active_link_invocation(tmp_path: Path, python312: str) -> None:
    bundle = _bundle(tmp_path, python312)
    prefix = tmp_path / "prefix"
    result = _prepare(python312, bundle, prefix)
    assert result.returncode == 0, result.stderr
    candidate = _candidate(python312, bundle, prefix)
    (prefix / ".venv").symlink_to(".venvs/" + candidate.name)
    receipt = (candidate / ".dsl41/receipt.json").read_bytes()
    for command in (
        [str(prefix / ".venv/bin/dsl41"), "--help"],
        [str(prefix / ".venv/bin/python"), "-c", "import dsl41.cli"],
    ):
        subprocess.run(command, check=True, capture_output=True, timeout=30)
    other = prefix / ".venvs/other-retained"
    other.mkdir()
    (other / "sentinel").write_text("keep other retained runtime")
    (prefix / ".venv").unlink()
    (prefix / ".venv").symlink_to(".venvs/other-retained")
    repeated = _prepare(python312, bundle, prefix)
    assert repeated.returncode == 0, repeated.stderr
    assert (candidate / ".dsl41/receipt.json").read_bytes() == receipt
    assert os.readlink(prefix / ".venv") == ".venvs/other-retained"
    assert (other / "sentinel").read_text() == "keep other retained runtime"


def test_offline_lock_survives_lock_owner_sigterm(tmp_path: Path, python312: str) -> None:
    gate = tmp_path / "gate"
    os.mkfifo(gate)
    recipe = tmp_path / "blocked-recipe.sh"
    recipe.write_text('printf "%s\n" "$$" > "$1/child-pid"\nread -r ignored < "$1/gate"\n')
    prefix = tmp_path / "prefix"
    owner = subprocess.Popen(
        [
            python312,
            "-I",
            str(TOOLS / "bundle.py"),
            "lock",
            python312,
            str(recipe),
            str(tmp_path),
            str(prefix),
            "base",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    writer = None
    completed = False
    try:
        deadline = time.monotonic() + 15
        while not (tmp_path / "child-pid").exists():
            assert owner.poll() is None, "lock recipe exited before reaching the gate"
            assert time.monotonic() < deadline, "lock recipe did not reach the gate"
            time.sleep(0.01)
        owner.terminate()
        owner.wait(timeout=10)
        with (prefix / ".offline.lock").open("a") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deadline = time.monotonic() + 15
        while writer is None:
            try:
                writer = os.open(gate, os.O_WRONLY | os.O_NONBLOCK)
            except OSError:
                assert time.monotonic() < deadline, "lock child did not open gate"
                time.sleep(0.01)
        os.write(writer, b"release\n")
        os.close(writer)
        writer = None
        owner.communicate(timeout=15)
        deadline = time.monotonic() + 15
        with (prefix / ".offline.lock").open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    completed = True
                    break
                except BlockingIOError:
                    assert time.monotonic() < deadline, (
                        "child retained lock after recipe completion"
                    )
                    time.sleep(0.01)
    finally:
        if writer is not None:
            os.close(writer)
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=10)
        if not completed and (tmp_path / "child-pid").exists():
            try:
                os.kill(int((tmp_path / "child-pid").read_text()), 15)
            except ProcessLookupError:
                pass
        owner.communicate(timeout=15)
