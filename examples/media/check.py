"""Read what an HTTP client receives. This checker imports no worker code."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=10) as response:
        content = response.read(5_000_001)
    if len(content) > 5_000_000:
        raise ValueError("unexpectedly large example artifact")
    return content


def fixture_tone(path: Path, expected_hz: float) -> dict[str, float]:
    args = ["ffmpeg", "-v", "error", "-xerror", "-i", str(path)]
    args += ["-ss", "0.5", "-t", "0.5", "-map", "0:a:0", "-ac", "1", "-ar", "8000"]
    args += ["-f", "f32le", "-"]
    data = subprocess.run(args, capture_output=True, check=True, timeout=30).stdout
    if len(data) != 4000 * 4:
        raise ValueError(f"incomplete middle audio segment: {path.name}")
    samples = struct.unpack("<4000f", data)
    if not all(math.isfinite(sample) for sample in samples):
        raise ValueError(f"nonfinite audio samples: {path.name}")
    rms = math.sqrt(sum(sample * sample for sample in samples) / len(samples))
    crossings = [
        index - left / (right - left)
        for index, (left, right) in enumerate(zip(samples, samples[1:]))
        if left <= 0 < right
    ]
    if rms < 0.01 or len(crossings) < 2:
        raise ValueError(f"missing fixture audio tone: {path.name}")
    frequency = (len(crossings) - 1) * 8000 / (crossings[-1] - crossings[0])
    if abs(frequency - expected_hz) > 5:
        raise ValueError(
            f"fixture audio tone mismatch: {path.name}: "
            f"{frequency:.2f} Hz, expected {expected_hz:.2f} Hz"
        )
    return {"frequency_hz": frequency, "rms": rms}


def check(url: str, revision: int, source: Path, evidence: Path) -> dict[str, Any]:
    evidence.mkdir(parents=True, exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix="attempt-", dir=evidence))
    pointer_bytes = fetch(f"{url}/current.json")
    pointer = json.loads(pointer_bytes)
    expected_manifest = f"releases/r{revision}/manifest.json"
    if pointer["revision"] != revision or pointer["manifest"] != expected_manifest:
        raise ValueError("client sees the wrong business revision")
    manifest_bytes = fetch(f"{url}/{expected_manifest}")
    if hashlib.sha256(manifest_bytes).hexdigest() != pointer["manifest_sha256"]:
        raise ValueError("manifest differs from the atomic publication pointer")
    manifest = json.loads(manifest_bytes)
    if manifest["revision"] != revision:
        raise ValueError("manifest revision mismatch")
    if manifest["input_sha256"] != hashlib.sha256(source.read_bytes()).hexdigest():
        raise ValueError("manifest names a different source clip")
    expected = {"low.mp4": (160, 90), "high.mp4": (320, 180), "poster.png": (320, 180)}
    if set(manifest["files"]) != set(expected):
        raise ValueError("manifest does not contain exactly the three required artifacts")
    tones = {"source": fixture_tone(source, 440 * revision)}
    actual = {}
    for name, dimensions in expected.items():
        content = fetch(f"{url}/releases/r{revision}/{name}")
        metadata = manifest["files"][name]
        if (
            len(content) != metadata["bytes"]
            or hashlib.sha256(content).hexdigest() != metadata["sha256"]
        ):
            raise ValueError(f"served file differs from manifest: {name}")
        output = evidence / name
        output.write_bytes(content)
        probe = json.loads(
            subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-count_frames",
                    "-show_streams",
                    "-show_format",
                    "-of",
                    "json",
                    str(output),
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            ).stdout
        )
        streams = probe["streams"]
        video = [stream for stream in streams if stream["codec_type"] == "video"]
        audio = [stream for stream in streams if stream["codec_type"] == "audio"]
        if len(video) != 1 or (video[0]["width"], video[0]["height"]) != dimensions:
            raise ValueError(f"wrong decoded video shape: {name}")
        if (metadata["width"], metadata["height"]) != dimensions:
            raise ValueError(f"wrong declared dimensions: {name}")
        if name.endswith(".png"):
            if (
                len(streams) != 1
                or audio
                or video[0]["codec_name"] != "png"
                or metadata["duration"] is not None
            ):
                raise ValueError("invalid poster")
            frames = 1
        else:
            duration = float(probe["format"]["duration"])
            if (
                len(streams) != 2
                or len(audio) != 1
                or video[0]["codec_name"] != "h264"
                or audio[0]["codec_name"] != "aac"
                or abs(duration - 2.0) > 0.15
                or abs(duration - metadata["duration"]) > 0.001
            ):
                raise ValueError(f"wrong streams or duration: {name}")
            tones[name] = fixture_tone(output, 440 * revision)
            if abs(tones[name]["frequency_hz"] - tones["source"]["frequency_hz"]) > 5:
                raise ValueError(f"served audio tone differs from source: {name}")
            frames = 48
        if int(video[0]["nb_read_frames"]) != frames:
            raise ValueError(f"incomplete video frame sequence: {name}")
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-i",
                str(output),
                "-map",
                "0",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        actual[name] = probe
    (evidence / "current.json").write_bytes(pointer_bytes)
    (evidence / "manifest.json").write_bytes(manifest_bytes)
    result = {
        "revision": revision,
        "retrieved": sorted(actual),
        "probes": actual,
        "audio_tones": tones,
    }
    (evidence / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--revision", type=int, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    check(args.url.rstrip("/"), args.revision, args.source, args.evidence)
    print(f"revision {args.revision}: three served artifacts validated")
