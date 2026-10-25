#!/usr/bin/env python3
"""Check offline delivery identity and retained runtime integrity (DL-389)."""

from __future__ import annotations

import email.parser
import fcntl
import hashlib
import json
import marshal
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
import tomllib
import zipfile
from pathlib import Path
from types import CodeType
from typing import Any
from urllib.parse import unquote, urlsplit


class Refusal(Exception):
    """An offline input or retained candidate needs an operator decision."""


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def target() -> dict[str, Any]:
    if sys.version_info[:2] != (3, 12):
        raise Refusal("use the target system's Python 3.12 with venv and pip")
    release = platform.freedesktop_os_release() if sys.platform == "linux" else {}
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": sysconfig.get_platform(),
        "machine": platform.machine(),
        "libc": list(platform.libc_ver()),
        "os_release": {key: release.get(key, "") for key in ("ID", "VERSION_ID")},
    }


def wheel_identity(wheel: Path) -> tuple[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(names) != 1:
            raise Refusal(f"expected one METADATA in {wheel.name}")
        metadata = email.parser.BytesParser().parsebytes(archive.read(names[0]))
    return str(metadata["Name"]), str(metadata["Version"])


def check_release(repo: Path, wheel: Path) -> str:
    """Check release metadata without qualifying a target interpreter (DL-396)."""
    project = tomllib.loads((repo / "pyproject.toml").read_text())["project"]
    lock = tomllib.loads((repo / "uv.lock").read_text())
    locked = [package for package in lock["package"] if package["name"] == "dsl41"]
    identity = (project["name"], project["version"])
    if identity[0] != "dsl41" or wheel.suffix != ".whl" or wheel_identity(wheel) != identity:
        raise Refusal("release wheel metadata must match pyproject.toml name and version")
    if len(locked) != 1 or locked[0]["version"] != project["version"]:
        raise Refusal("uv.lock must name the same dsl41 release")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", project["version"]):
        raise Refusal("release version is not a safe runtime directory component")
    return str(project["version"])


def check_input(repo: Path, wheel: Path) -> None:
    target()
    check_release(repo, wheel)


def inventory(root: Path) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise Refusal(f"delivery contains a symlink or special file: {path}")
        if path.is_file() and path != root / "manifest.json":
            files[path.relative_to(root).as_posix()] = {
                "sha256": digest(path),
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
    return files


def check_requirements(path: Path, *, reject_project: bool = False) -> None:
    # Only ordinary hashed pins are admitted; pip --no-index permits direct URLs (DL-389).
    pin = r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9][A-Za-z0-9.!+_-]*"
    marker = r"(?:\s*;\s*[^@\\]+?)?"
    hashes = r"(?:\s+--hash=sha256:[0-9a-f]{64})+"
    text = path.read_text().replace("\\\n", " ")
    for line in text.splitlines():
        line = line.strip()
        valid = re.fullmatch(pin + marker + hashes, line) is not None
        options = "--" in re.sub(hashes, "", line)
        if line and not line.startswith("#") and (not valid or options):
            raise Refusal(f"only hashed package==version pins and markers are allowed: {path.name}")
        if reject_project and re.match(r"dsl41(?:\[|==)", line, re.IGNORECASE):
            raise Refusal(f"incoming exports must not pin dsl41: {path.name}")


def release_inputs(repo: Path, wheel: Path, exports: Path) -> dict[str, Any]:
    """Bind upstream exports to exact release and project metadata bytes (DL-396)."""
    version = check_release(repo, wheel)
    requirements = {}
    for profile in ("base", "ui"):
        path = exports / f"requirements-{profile}.txt"
        check_requirements(path, reject_project=True)
        requirements[path.name] = digest(path)
    return {
        "schema": 1,
        "version": version,
        "release_wheel": {"filename": wheel.name, "sha256": digest(wheel)},
        "pyproject_sha256": digest(repo / "pyproject.toml"),
        "uv_lock_sha256": digest(repo / "uv.lock"),
        "requirements": requirements,
    }


def write_release_inputs(repo: Path, wheel: Path, exports: Path) -> None:
    manifest = release_inputs(repo, wheel, exports)
    with (exports / "release-inputs.json").open("x") as stream:
        json.dump(manifest, stream, sort_keys=True, indent=2)
        stream.write("\n")


def verify_release_inputs(repo: Path, wheel: Path, exports: Path) -> None:
    expected = release_inputs(repo, wheel, exports)
    manifest = json.loads((exports / "release-inputs.json").read_text())
    if manifest != expected:
        raise Refusal(
            "release-inputs.json differs from the release wheel, project metadata, or exports"
        )


def write_manifest(bundle: Path, release_wheel: Path) -> None:
    lock = tomllib.loads((bundle / "uv.lock").read_text())
    locked = {
        unquote(Path(urlsplit(wheel["url"]).path).name): wheel
        for package in lock["package"]
        for wheel in package.get("wheels", [])
    }
    provenance = {}
    for wheel in sorted((bundle / "wheels").iterdir()):
        if wheel.suffix != ".whl":
            raise Refusal(f"non-wheel in wheelhouse: {wheel.name}")
        name, version = wheel_identity(wheel)
        sha256 = digest(wheel)
        if wheel.name == release_wheel.name:
            source = "supplied release wheel"
        else:
            entry = locked.get(wheel.name)
            if not entry or entry["hash"] != f"sha256:{sha256}":
                raise Refusal(f"wheel is absent from uv.lock or has a different hash: {wheel.name}")
            source = entry["url"]
        provenance[wheel.name] = {
            "name": name,
            "version": version,
            "sha256": sha256,
            "source": source,
        }
    if digest(bundle / "wheels" / release_wheel.name) != digest(release_wheel):
        raise Refusal("copied release wheel differs from the supplied release wheel")
    _, version = wheel_identity(bundle / "wheels" / release_wheel.name)
    manifest = {
        "schema": 1,
        "version": version,
        "release_wheel": release_wheel.name,
        "target": target(),
        "files": inventory(bundle),
        "wheels": provenance,
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")


def load_bundle(bundle: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((bundle / "manifest.json").read_text())
    if manifest["schema"] != 1 or manifest["target"] != target():
        raise Refusal("delivery target differs; rebuild and qualify on this Python/OS/architecture")
    if inventory(bundle) != manifest["files"]:
        raise Refusal("delivery file inventory, hashes, or modes differ from manifest.json")
    for profile in ("base", "ui"):
        check_requirements(bundle / f"requirements-{profile}.txt")
    return manifest


def runtime_path(bundle: Path, prefix: Path, profile: str) -> Path:
    manifest = load_bundle(bundle)
    if profile not in ("base", "ui"):
        raise Refusal("profile must be base or ui")
    version = manifest["version"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", version):
        raise Refusal("invalid release version")
    return prefix / ".venvs" / f"{version}-{profile}-{digest(bundle / 'manifest.json')[:20]}"


def check_bytecode(path: Path, candidate: Path) -> None:
    if path.parent.name == "__pycache__":
        match = re.fullmatch(r"(.+)\.cpython-312(?:\.opt-([12]))?\.pyc", path.name)
        if match is None:
            raise Refusal(f"unrecognized bytecode cache: {path}")
        source = path.parent.parent / f"{match[1]}.py"
        optimization = int(match[2] or 0)
    else:
        source, optimization = path.with_suffix(".py"), 0
    if not source.is_file():
        raise Refusal(f"sourceless bytecode in retained runtime: {path}")
    cached = marshal.loads(path.read_bytes()[16:])
    alias = candidate.parent.parent / ".venv" / source.relative_to(candidate)
    if not isinstance(cached, CodeType) or (
        cached.co_filename != str(alias) and Path(cached.co_filename).resolve() != source.resolve()
    ):
        raise Refusal(f"bytecode names a different retained source: {path}")
    expected = compile(
        source.read_bytes(), cached.co_filename, "exec", dont_inherit=True, optimize=optimization
    )
    if cached != expected:
        raise Refusal(f"bytecode differs from retained source: {path}")


def runtime_inventory(candidate: Path) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(candidate.rglob("*")):
        relative = path.relative_to(candidate).as_posix()
        if relative == ".dsl41/receipt.json":
            continue
        if path.is_symlink():
            files[relative] = {"link": os.readlink(path)}
        elif path.is_file():
            if path.suffix == ".pyc":
                check_bytecode(path, candidate)
            else:
                files[relative] = {
                    "sha256": digest(path),
                    "mode": stat.S_IMODE(path.stat().st_mode),
                }
        elif not path.is_dir():
            raise Refusal(f"special file in retained runtime: {path}")
    return files


def receipt_identity(bundle: Path, candidate: Path, profile: str) -> dict[str, Any]:
    return {
        "schema": 1,
        "manifest_sha256": digest(bundle / "manifest.json"),
        "profile": profile,
        "candidate": str(candidate),
        "target": target(),
        "system_python_sha256": digest(Path(sys.executable).resolve()),
    }


def complete(bundle: Path, candidate: Path, profile: str) -> None:
    load_bundle(bundle)
    retained = candidate / ".dsl41"
    retained.mkdir()
    for name in ("manifest.json", "uv.lock", "pyproject.toml", f"requirements-{profile}.txt"):
        shutil.copy2(bundle / name, retained / name)
    shutil.copytree(bundle / "assets", retained / "assets")
    receipt = receipt_identity(bundle, candidate, profile)
    receipt["files"] = runtime_inventory(candidate)
    # A completion receipt is the final write, after pip check and smoke (DL-389).
    with (retained / "receipt.json").open("x") as stream:
        json.dump(receipt, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(retained, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def verify(bundle: Path, candidate: Path, profile: str) -> None:
    load_bundle(bundle)
    if candidate.is_symlink() or not candidate.is_dir():
        raise Refusal(f"candidate is not a plain directory: {candidate}")
    receipt = json.loads((candidate / ".dsl41/receipt.json").read_text())
    expected = receipt_identity(bundle, candidate, profile)
    if {key: value for key, value in receipt.items() if key != "files"} != expected:
        raise Refusal("retained candidate receipt conflicts with this delivery or interpreter")
    if receipt["files"] != runtime_inventory(candidate):
        raise Refusal("retained candidate installed files differ from its completion receipt")
    work = os.environ.copy()
    for key in list(work):
        if key.startswith(("PYTHON", "PIP_")) or key == "VIRTUAL_ENV":
            del work[key]
    work.update(PIP_CONFIG_FILE="/dev/null", PYTHONDONTWRITEBYTECODE="1")
    subprocess.run(
        [str(candidate / "bin/python"), "-I", "-B", "-m", "pip", "--no-cache-dir", "check"],
        check=True,
        env=work,
        cwd="/",
    )
    subprocess.run(
        [
            str(candidate / "bin/python"),
            "-I",
            "-B",
            str(Path(__file__).with_name("smoke.py")),
            str(candidate),
            profile,
            str(bundle),
        ],
        check=True,
        env=work,
        cwd="/",
    )


def locked(python: str, recipe: str, bundle_arg: str, prefix_arg: str, profile: str) -> None:
    bundle, prefix = Path(bundle_arg).resolve(strict=True), Path(prefix_arg)
    if not prefix.is_absolute():
        raise Refusal("prefix must be absolute")
    prefix.mkdir(parents=True, exist_ok=True)
    prefix = prefix.resolve(strict=True)
    descriptor = os.open(prefix / ".offline.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        versions = prefix / ".venvs"
        if versions.is_symlink():
            raise Refusal(".venvs must be a plain directory")
        versions.mkdir(exist_ok=True)
        subprocess.run(
            ["bash", recipe, str(bundle), str(prefix), profile, python],
            check=True,
            pass_fds=(descriptor,),
        )


def main() -> None:
    command, *args = sys.argv[1:]
    if command == "lock":
        locked(*args)
    elif command == "check-input":
        check_input(*(Path(arg) for arg in args))
    elif command == "check-release":
        check_release(*(Path(arg) for arg in args))
    elif command == "release-inputs":
        write_release_inputs(*(Path(arg) for arg in args))
    elif command == "verify-inputs":
        verify_release_inputs(*(Path(arg) for arg in args))
    elif command == "manifest":
        write_manifest(*(Path(arg) for arg in args))
    elif command == "add-release":
        bundle, wheel, profile = Path(args[0]), Path(args[1]), args[2]
        name, version = wheel_identity(wheel)
        with (bundle / f"requirements-{profile}.txt").open("a") as stream:
            stream.write(f"\n{name}=={version} --hash=sha256:{digest(wheel)}\n")
        check_requirements(bundle / f"requirements-{profile}.txt")
    elif command == "candidate":
        print(runtime_path(Path(args[0]), Path(args[1]), args[2]))
    elif command in ("receipt", "verify"):
        action = complete if command == "receipt" else verify
        action(Path(args[0]), Path(args[1]), args[2])
    else:
        raise Refusal(f"unknown command: {command}")


if __name__ == "__main__":
    try:
        main()
    except (
        Refusal,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        EOFError,
        subprocess.CalledProcessError,
    ) as error:
        sys.exit(
            f"offline preparation refused: {error}\n"
            "No retained candidate was removed or repaired. Keep the failed directory for inspection. "
            "Use a new qualified delivery, or retire this exact unused candidate through the runbook. "
            "Never remove a candidate used by a process, rollback, or retained run."
        )
