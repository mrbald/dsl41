"""Run a real media publication, then optionally recover one failed rendition."""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workflows"))
from support import Engine, cli, make_properties, new_run, read_json, write_json  # noqa: E402

HERE = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(root: Path, revision: int) -> None:
    source = root / "inputs" / f"clip-r{revision}.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={440 * revision}:sample_rate=44100",
            "-t",
            "2",
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
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=45,
    )
    write_json(
        root / "inputs" / f"request-r{revision}.json",
        {"revision": revision, "expected_previous": revision - 1, "input_sha256": digest(source)},
    )


def observe(root: Path, url: str, revision: int, evidence: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(HERE / "check.py"),
            "--url",
            url,
            "--revision",
            str(revision),
            "--source",
            str(root / "inputs" / f"clip-r{revision}.mp4"),
            "--evidence",
            str(evidence),
        ],
        check=True,
        timeout=90,
    )


def admission(trace: dict, evidence: Path) -> None:
    jobs = {"MEDIA_LOW", "MEDIA_HIGH", "MEDIA_POSTER"}
    active: set[str] = set()
    queued = 0
    for entry in trace["entries"]:
        if entry["job"] not in jobs or "->" not in entry["transition"]:
            continue
        status = entry["transition"].split("->")[1]
        if status == "QUE_WAIT":
            queued += 1
        elif status == "STARTING":
            if active:
                raise RuntimeError("more than one worker admitted to the encode slot")
            active.add(entry["job"])
        elif status in {"SUCCESS", "FAILURE", "TERMINATED"}:
            active.discard(entry["job"])
    if not queued or active:
        raise RuntimeError("missing resource queue/release evidence")
    write_json(evidence, {"queued_transitions": queued, "maximum_admitted": 1})


def check_history(launch: Path, expected_actions: dict[str, int], failed: bool) -> None:
    rows = json.loads((launch / "runs.json").read_text())
    expected = {
        (f"MEDIA_{action.upper()}", number): ("SUCCESS", 0)
        for action, count in expected_actions.items()
        for number in range(1, count + 1)
    }
    if failed:
        expected[("MEDIA_HIGH", 1)] = ("FAILURE", 23)
    actual = {(row["job"], row["run_number"]): (row["status"], row["exit_code"]) for row in rows}
    if len(rows) != len(expected) or actual != expected:
        raise RuntimeError("offline history differs from expected worker runs and outcomes")
    if any(
        row["fidelity"] != "full" or row["undecided"] or row["ended_at"] is None for row in rows
    ):
        raise RuntimeError("offline history contains incomplete or degraded rows")
    actions = [read_json(path)["action"] for path in (launch / "attempts").glob("*.json")]
    if {action: actions.count(action) for action in set(actions)} != expected_actions:
        raise RuntimeError("stopped-engine worker attempts differ from offline history")
    write_json(launch / "history-check.json", {"rows": len(rows), "counts": expected_actions})


def wave(run: Path, root: Path, url: str, revision: int, fail: bool) -> None:
    launch = run / f"r{revision}"
    launch.mkdir()
    write_json(launch / "config.json", {"root": str(root), "revision": revision, "url": url})
    properties = make_properties(launch, HERE / "worker.py")
    if fail:
        (root / "faults" / f"r{revision}-high.armed").touch()
    engine = Engine(launch, HERE / "estate.jil", properties)
    try:
        with engine:
            engine.event("STARTJOB", "MEDIA_INPUT")
            if fail:
                engine.wait_job("MEDIA_HIGH", "FAILURE")
                engine.wait_job("MEDIA_LOW")
                engine.wait_job("MEDIA_POSTER")
                snapshot = engine.status()
                write_json(launch / "failure-status.json", snapshot)
                if any(
                    snapshot["jobs"][job]["run_number"] != 0
                    for job in ["MEDIA_STAGE", "MEDIA_PUBLISH", "MEDIA_VERIFY"]
                ):
                    raise RuntimeError("an incomplete release passed the dependency gate")
                fired = read_json(root / "faults" / f"r{revision}-high.fired")
                if fired["sha256"] != digest(Path(fired["partial"])):
                    raise RuntimeError("partial-output fault did not fire")
                if (root / "public" / "releases" / f"r{revision}").exists():
                    raise RuntimeError("failed rendition exposed a release")
                observe(root, url, 1, launch / "prior-release-check")
                completed = {
                    name: digest(root / "work" / f"r{revision}" / name)
                    for name in ["low.mp4", "poster.png"]
                }
                engine.event("FORCE_STARTJOB", "MEDIA_HIGH")
            engine.wait_job("MEDIA_VERIFY")
            snapshot = engine.status()
            write_json(launch / "final-status.json", snapshot)
            actions = [read_json(path)["action"] for path in (launch / "attempts").glob("*.json")]
            expected_actions = {
                action: 1
                for action in ["input", "low", "high", "poster", "stage", "publish", "verify"]
            }
            expected_actions["high"] = 2 if fail else 1
            if {action: actions.count(action) for action in set(actions)} != expected_actions:
                raise RuntimeError("unexpected actual worker invocation count")
            write_json(launch / "attempt-counts.json", expected_actions)
            if fail:
                for name, original in completed.items():
                    if digest(root / "work" / f"r{revision}" / name) != original:
                        raise RuntimeError("targeted rerun changed completed work")
                expected = {"MEDIA_HIGH": 2, "MEDIA_LOW": 1, "MEDIA_POSTER": 1}
                if any(
                    snapshot["jobs"][job]["run_number"] != count for job, count in expected.items()
                ):
                    raise RuntimeError("unexpected worker execution count")
            trace = json.loads(cli("query", "trace", "--socket", str(engine.socket)).stdout)
            write_json(launch / "trace.json", trace)
            admission(trace, launch / "resource-admission.json")
    finally:
        engine.stop()
        if engine.root.exists():
            (launch / "runs.json").write_text(
                cli("runs", str(engine.root), "--format", "json").stdout
            )
    check_history(launch, expected_actions, fail)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument(
        "--scenario", choices=["happy", "failed-rendition"], default="failed-rendition"
    )
    args = parser.parse_args()
    run = new_run("media", args.run_dir)
    print(f"retained run: {run}", flush=True)
    root = run / "business"
    for name in ["inputs", "faults", "public"]:
        (root / name).mkdir(parents=True)
    versions = {
        name: subprocess.run(
            [name, "-version"], capture_output=True, text=True, check=True, timeout=10
        ).stdout.splitlines()[0]
        for name in ["ffmpeg", "ffprobe"]
    }
    write_json(run / "versions.json", {"python": sys.version, **versions})
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(root / "public"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"HTTP during this invocation: {url}", flush=True)
    try:
        generate(root, 1)
        wave(run, root, url, 1, False)
        if args.scenario == "failed-rendition":
            original = {path.name: digest(path) for path in (root / "public/releases/r1").iterdir()}
            generate(root, 2)
            wave(run, root, url, 2, True)
            result = subprocess.run(
                [sys.executable, str(HERE / "worker.py"), "--run", str(run / "r1"), "publish"],
                capture_output=True,
                text=True,
                timeout=45,
            )
            write_json(
                run / "stale-publish.json", {"exit": result.returncode, "stderr": result.stderr}
            )
            if result.returncode == 0 or "stale publication" not in result.stderr:
                raise RuntimeError("old revision did not refuse publication")
            if original != {
                path.name: digest(path) for path in (root / "public/releases/r1").iterdir()
            }:
                raise RuntimeError("prior immutable release changed")
        observe(
            root, url, 2 if args.scenario == "failed-rendition" else 1, run / "final-http-check"
        )
        write_json(
            run / "result.json",
            {
                "scenario": args.scenario,
                "status": "passed",
                "current": read_json(root / "public/current.json"),
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print(f"media {args.scenario}: passed; evidence retained at {run}")


if __name__ == "__main__":
    main()
