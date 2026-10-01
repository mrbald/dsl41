"""Small media commands. Publication is an application rule, not scheduler state."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflows"))
from support import read_json, write_json  # noqa: E402

OUTPUTS = {"low.mp4": (160, 90), "high.mp4": (320, 180), "poster.png": (320, 180)}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError(f"command exited {result.returncode}: {result.stderr}")
    return result.stdout


def inspect_media(path: Path, size: tuple[int, int], poster: bool = False) -> dict[str, Any]:
    probe = json.loads(
        command(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
        )
    )
    streams = probe["streams"]
    videos = [stream for stream in streams if stream["codec_type"] == "video"]
    audios = [stream for stream in streams if stream["codec_type"] == "audio"]
    if len(videos) != 1 or (videos[0]["width"], videos[0]["height"]) != size:
        raise ValueError(f"wrong video dimensions: {path}")
    duration = None if poster else float(probe["format"]["duration"])
    if poster:
        if audios or videos[0]["codec_name"] != "png":
            raise ValueError(f"invalid poster: {path}")
    elif (
        len(audios) != 1
        or videos[0]["codec_name"] != "h264"
        or audios[0]["codec_name"] != "aac"
        or duration is None
        or abs(duration - 2.0) > 0.15
    ):
        raise ValueError(f"invalid rendition streams or duration: {path}")
    command(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"])
    return {
        "sha256": digest(path),
        "bytes": path.stat().st_size,
        "duration": duration,
        "width": size[0],
        "height": size[1],
    }


def request(root: Path, revision: int) -> tuple[dict[str, Any], Path]:
    value = read_json(root / "inputs" / f"request-r{revision}.json")
    source = root / "inputs" / f"clip-r{revision}.mp4"
    if value["revision"] != revision or value["expected_previous"] != revision - 1:
        raise ValueError("revision does not match the fixed business request")
    if digest(source) != value["input_sha256"]:
        raise ValueError("input differs from the admitted business request")
    return value, source


def encode(root: Path, revision: int, action: str, source: Path) -> None:
    work = root / "work" / f"r{revision}"
    work.mkdir(parents=True, exist_ok=True)
    name = "poster.png" if action == "poster" else f"{action}.mp4"
    output = work / name
    if output.exists():
        inspect_media(output, OUTPUTS[name], action == "poster")
        return
    fault = root / "faults" / f"r{revision}-{action}.armed"
    fired = fault.with_suffix(".fired")
    partial = work / f"{action}.partial{output.suffix}"
    args = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source)]
    if action == "poster":
        args.extend(["-frames:v", "1", "-threads", "1"])
    else:
        args.extend(
            [
                "-vf",
                f"scale={OUTPUTS[name][0]}:{OUTPUTS[name][1]}",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-threads",
                "1",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
            ]
        )
    if fault.exists():
        fault.rename(fired)
        partial = work / f"{action}.failed.partial{output.suffix}"
        command([*args, "-t", "0.5", str(partial)])
        write_json(
            fired,
            {
                "revision": revision,
                "action": action,
                "partial": str(partial),
                "sha256": digest(partial),
            },
        )
        raise SystemExit(23)
    command([*args, str(partial)])
    inspect_media(partial, OUTPUTS[name], action == "poster")
    os.replace(partial, output)


def stage(root: Path, revision: int, admitted: dict[str, Any]) -> None:
    work = root / "work" / f"r{revision}"
    entries = {
        name: inspect_media(work / name, size, name.endswith(".png"))
        for name, size in OUTPUTS.items()
    }
    manifest = {"revision": revision, "input_sha256": admitted["input_sha256"], "files": entries}
    release = root / "public" / "releases" / f"r{revision}"
    if release.exists():
        if read_json(release / "manifest.json") != manifest:
            raise ValueError("immutable release already exists with different content")
        return
    staging = root / "staging"
    staging.mkdir(exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f"r{revision}-", dir=staging))
    for name in OUTPUTS:
        shutil.copyfile(work / name, temporary / name)
        with (temporary / name).open("rb") as handle:
            os.fsync(handle.fileno())
        (temporary / name).chmod(0o444)
    write_json(temporary / "manifest.json", manifest)
    (temporary / "manifest.json").chmod(0o444)
    release.parent.mkdir(parents=True, exist_ok=True)
    temporary.rename(release)
    release.chmod(0o555)
    directory = os.open(release.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def publish(root: Path, revision: int, admitted: dict[str, Any]) -> None:
    release = root / "public" / "releases" / f"r{revision}"
    manifest = read_json(release / "manifest.json")
    if manifest["revision"] != revision or manifest["input_sha256"] != admitted["input_sha256"]:
        raise ValueError("release does not match the business request")
    if set(manifest["files"]) != set(OUTPUTS):
        raise ValueError("release membership differs from the required outputs")
    for name, size in OUTPUTS.items():
        if inspect_media(release / name, size, name.endswith(".png")) != manifest["files"][name]:
            raise ValueError(f"release metadata differs from its media: {name}")
    pointer = {
        "revision": revision,
        "manifest": f"releases/r{revision}/manifest.json",
        "manifest_sha256": digest(release / "manifest.json"),
    }
    current = root / "public" / "current.json"
    previous = read_json(current) if current.exists() else {"revision": 0}
    if previous == pointer:
        return
    if previous["revision"] != admitted["expected_previous"]:
        raise ValueError(
            f"stale publication: expected {admitted['expected_previous']}, "
            f"current is {previous['revision']}"
        )
    write_json(current, pointer)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument(
        "action", choices=["input", "low", "high", "poster", "stage", "publish", "verify"]
    )
    args = parser.parse_args()
    config = read_json(args.run / "config.json")
    root, revision = Path(config["root"]), int(config["revision"])
    write_json(
        args.run / "attempts" / f"{args.action}-{uuid.uuid4()}.json",
        {
            "action": args.action,
            "revision": revision,
            "pid": os.getpid(),
            "scheduler_run": os.environ.get("DSL41_RUN"),
        },
    )
    admitted, source = request(root, revision)
    if args.action == "input":
        inspect_media(source, (320, 180))
    elif args.action in {"low", "high", "poster"}:
        encode(root, revision, args.action, source)
    elif args.action == "verify":
        command(
            [
                sys.executable,
                str(Path(__file__).with_name("check.py")),
                "--url",
                config["url"],
                "--revision",
                str(revision),
                "--source",
                str(source),
                "--evidence",
                str(args.run / "http-check"),
            ]
        )
    else:
        with (root / "publication.lock").open("a") as lock:
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("publication lock stayed busy") from None
                    time.sleep(0.05)
            if args.action == "stage":
                stage(root, revision, admitted)
            else:
                publish(root, revision, admitted)


if __name__ == "__main__":
    main()
