"""Isolation, dependency layers and local drill failure handling."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_workflow_unique_image_propagation() -> None:
    spec = importlib.util.spec_from_file_location(
        "cache_lane", REPO / "examples/workflows/tests/lane.py"
    )
    assert spec and spec.loader
    import sys
    from unittest.mock import patch

    lane = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"cache_lane": lane}):
        spec.loader.exec_module(lane)
    a = lane.compose("checkout-a", "localhost/runner:session-a")
    b = lane.compose("checkout-b", "localhost/runner:session-b")
    assert a[1] == "WORKFLOW_RUNNER_IMAGE=localhost/runner:session-a"
    assert b[1] == "WORKFLOW_RUNNER_IMAGE=localhost/runner:session-b"


def test_service_export_excludes_session_paths(tmp_path: Path) -> None:
    script = (REPO / "examples/nightbank/deploy/drill-local.sh").read_text()
    export = script.split("uv export", 1)[1].split("\n", 1)[0]
    assert "--no-header" in export
    outputs = [tmp_path / "session-a.txt", tmp_path / "session-b.txt"]
    for output in outputs:
        subprocess.run(
            [
                "uv",
                "export",
                "--quiet",
                "--frozen",
                "--no-dev",
                "--no-header",
                "--no-emit-project",
                "--no-hashes",
                "--python",
                "3.12",
                "-o",
                str(output),
            ],
            cwd=REPO,
            check=True,
            timeout=120,
            capture_output=True,
        )
    assert outputs[0].read_bytes() == outputs[1].read_bytes()
    assert str(tmp_path).encode() not in outputs[0].read_bytes()


@pytest.mark.parametrize(
    "build,scenario,export,cleanup,image_cleanup,expected",
    [
        (0, 0, 0, 0, 0, 0),
        (0, 7, 9, 4, 5, 7),
        (0, 0, 9, 0, 0, 1),
        (42, 0, 0, 0, 0, 42),
        (0, 0, 0, 0, 5, 1),
    ],
)
def test_dst_preserves_status_and_exports_before_cleanup(
    tmp_path: Path,
    build: int,
    scenario: int,
    export: int,
    cleanup: int,
    image_cleanup: int,
    expected: int,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "dst-drill-local.sh").write_bytes((REPO / "scripts/dst-drill-local.sh").read_bytes())
    fake = tmp_path / "podman"
    log = tmp_path / "commands.jsonl"
    fake.write_text(
        "#!/usr/bin/env python3\nimport json,os,sys\n"
        "assert sys.argv[1:3] == ['--connection', 'selected']\n"
        "sys.argv = [sys.argv[0], *sys.argv[3:]]\n"
        "with open(os.environ['TEST_LOG'],'a') as out: out.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "status={'build': 'TEST_BUILD', 'start': 'TEST_SCENARIO', 'cp': 'TEST_EXPORT', 'rm': 'TEST_CLEANUP', 'image': 'TEST_IMAGE_CLEANUP'}.get(sys.argv[1])\n"
        "sys.exit(int(os.environ[status]) if status else 0)\n"
    )
    fake.chmod(0o755)
    result = subprocess.run(
        ["bash", str(scripts / "dst-drill-local.sh")],
        env={
            **os.environ,
            "PODMAN": f"{fake} --connection selected",
            "DST_EVIDENCE": str(tmp_path / "evidence"),
            "TEST_LOG": str(log),
            "TEST_BUILD": str(build),
            "TEST_SCENARIO": str(scenario),
            "TEST_EXPORT": str(export),
            "TEST_CLEANUP": str(cleanup),
            "TEST_IMAGE_CLEANUP": str(image_cleanup),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == expected, result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    if build:
        assert [command[0] for command in commands] == ["build"]
        return
    assert [command[0] for command in commands] == ["build", "create", "start", "cp", "rm", "image"]
    assert commands[-1][1:3] == ["rm", "--no-prune"]
    assert commands[-1][-1].startswith("localhost/dsl41-dst:")
    assert "--init" in commands[1] and "--pull" in commands[1] and "never" in commands[1]
    assert not any(arg in commands[1] for arg in ["--user", "--volume", "-v"])


@pytest.mark.parametrize("service", [False, True])
def test_wargame_build_context(tmp_path: Path, service: bool) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ["wargame.sh", "wargame.Containerfile"]:
        (scripts / name).write_bytes((REPO / "scripts" / name).read_bytes())
    requirements = tmp_path / "runtime-requirements.txt"
    requirements.write_text("lark==1.3.1\n")
    fake = tmp_path / "podman"
    record = tmp_path / "build.json"
    fake.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
if sys.argv[1] in ['container', 'image']:
    sys.exit(1)
if sys.argv[1] == 'build':
    context = Path(sys.argv[-1])
    req = context / 'requirements.txt'
    Path(os.environ['TEST_RECORD']).write_text(json.dumps({
        'args': sys.argv[1:],
        'files': sorted(p.name for p in context.iterdir()),
        'requirements': req.read_text() if req.exists() else None,
    }))
""")
    fake.chmod(0o755)
    env = {**os.environ, "PODMAN": str(fake), "TEST_RECORD": str(record)}
    env.pop("WARGAME_REQUIREMENTS", None)
    if service:
        env["WARGAME_REQUIREMENTS"] = str(requirements)
    result = subprocess.run(
        ["bash", str(scripts / "wargame.sh"), "build", "owned"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    build = json.loads(record.read_text())
    assert "--layers=true" in build["args"]
    assert build["args"][build["args"].index("--target") + 1] == ("service" if service else "host")
    assert build["files"] == (
        ["Containerfile", "requirements.txt"] if service else ["Containerfile"]
    )
    assert build["requirements"] == (requirements.read_text() if service else None)
    assert not Path(build["args"][-1]).exists()


def test_dst_refuses_existing_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "previous-run"
    evidence.mkdir()
    sentinel = evidence / "scenarios.json"
    sentinel.write_text("previous evidence")
    result = subprocess.run(
        ["bash", str(REPO / "scripts/dst-drill-local.sh")],
        env={**os.environ, "DST_EVIDENCE": str(evidence), "PODMAN": "must-not-run"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "DST_EVIDENCE must be a new directory" in result.stderr
    assert sentinel.read_text() == "previous evidence"
    assert sorted(p.name for p in evidence.iterdir()) == ["scenarios.json"]


def test_sessions_sharing_evidence_keep_their_own_runner(tmp_path: Path, monkeypatch) -> None:
    import itertools
    import sys
    from types import SimpleNamespace
    from unittest.mock import patch

    def module(name: str, path: str):
        spec = importlib.util.spec_from_file_location(name, REPO / path)
        assert spec and spec.loader
        return spec, importlib.util.module_from_spec(spec)

    lane_spec, plumbing = module("lane", "examples/workflows/tests/lane.py")
    fixture_spec, fixtures = module(
        "shared_evidence_fixtures", "examples/workflows/tests/conftest.py"
    )
    with patch.dict(sys.modules, {"lane": plumbing, "shared_evidence_fixtures": fixtures}):
        lane_spec.loader.exec_module(plumbing)
        fixture_spec.loader.exec_module(fixtures)

    monkeypatch.setenv("WORKFLOW_EVIDENCE", str(tmp_path))
    monkeypatch.setenv("WORKFLOW_CONTEXT", "selected-context")
    monkeypatch.setenv("PODMAN", "wrong-default-engine")
    tokens = itertools.count(1)
    monkeypatch.setattr(fixtures.secrets, "token_hex", lambda _: f"{next(tokens):012x}")
    calls = []
    removed = []

    def required(record, name, args, timeout):
        calls.append((name, args))
        values = {
            "docker version": "{}",
            "context inspect": '[{"Name":"selected-context","Endpoints":{"docker":{"Host":"selected-endpoint"}}}]',
        }
        if name == "compose config":
            tag = args[1].split("=", 1)[1]
            output = json.dumps(
                {
                    "services": {
                        "postgres": {"image": "postgres@sha256:pinned"},
                        "runner": {"image": tag},
                    }
                }
            )
        else:
            output = values.get(name, "versions")
        return plumbing.Result(args, 0, output, "", 0, "now")

    def execute(args, timeout):
        removed.append(args)
        return plumbing.Result(args, 0, "", "", 0, "now")

    monkeypatch.setattr(fixtures, "_required", required)
    monkeypatch.setattr(fixtures, "execute", execute)
    observed = []

    class OwnedLane:
        def __init__(self, project, evidence, runner):
            self.runner = runner

        def up(self):
            observed.append(self.runner)

        def close(self):
            pass

    monkeypatch.setattr(fixtures, "Lane", OwnedLane)
    configs = [SimpleNamespace(stash=pytest.Stash()) for _ in range(2)]
    sessions = [fixtures.workflow_session.__wrapped__(None, config) for config in configs]
    values = [next(session) for session in sessions]
    assert values[0][0] == values[1][0] == tmp_path
    assert values[0][1] != values[1][1]
    assert json.loads((tmp_path / "session.json").read_text())["runner_image"] == values[1][1]
    for value in values:
        request = SimpleNamespace(node=SimpleNamespace(name="test_owned"))
        run = fixtures.lane.__wrapped__(value, request)
        assert next(run).runner == value[1]
        assert next(run, None) is None
    assert observed == [value[1] for value in values]
    for session in sessions:
        assert next(session, None) is None
    assert removed == [
        ["docker", "--context", "selected-context", "image", "rm", "--no-prune", value[1]]
        for value in values
    ]
    builds = [args for name, args in calls if name == "runner build"]
    assert len(builds) == 2
    for args, value in zip(builds, values, strict=True):
        assert args[:5] == [
            "env",
            f"WORKFLOW_RUNNER_IMAGE={value[1]}",
            "docker",
            "--context",
            "selected-context",
        ]
        assert args[-2:] == ["build", "runner"]
