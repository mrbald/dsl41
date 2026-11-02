"""Fail-closed rehearsal routing and the three-lane scenario contract."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _run_block(text: str, marker: str) -> str:
    body = text.split(marker, 1)[1].split("        run: |\n", 1)[1]
    lines = []
    for line in body.splitlines():
        if line and not line.startswith("          "):
            break
        lines.append(line[10:])
    return "\n".join(lines)


@pytest.mark.parametrize("code", ["true", "false", "", "garbage", "False"])
@pytest.mark.parametrize("valid", ["true", "false", ""])
@pytest.mark.parametrize("classification", ["success", "failure", "skipped", "cancelled"])
@pytest.mark.parametrize("lane", ["success", "failure", "skipped", "cancelled"])
def test_required_rehearsal_truth_table(tmp_path, code, valid, classification, lane):
    text = (REPO / ".github/workflows/ci.yml").read_text()
    script = _run_block(text, "      - name: decide required rehearsal result")
    summary = tmp_path / "summary"
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "CODE": code,
            "CLASSIFY_VALID": valid,
            "CLASSIFY_RESULT": classification,
            "LANE_RESULT": lane,
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        timeout=5,
    )
    passed = (
        classification == "success"
        and valid == "true"
        and ((code == "false" and lane == "skipped") or (code == "true" and lane == "success"))
    )
    assert (result.returncode == 0) == passed
    if passed and code == "false":
        assert "docs-only: rehearsal not needed" in summary.read_text()


@pytest.mark.parametrize(
    "diff_exit,script_exit,output,changed,expected,valid",
    [
        (0, 0, "code=false", "docs/a.md", "false", "true"),
        (0, 0, "code=true", "src/a.py", "true", "true"),
        (7, 0, "code=false", "", "true", "false"),
        (0, 9, "code=false", "docs/a.md", "true", "false"),
        (0, 0, "", "docs/a.md", "true", "false"),
        (0, 0, "garbage", "docs/a.md", "true", "false"),
        (0, 0, "code=false\ncode=true", "docs/a.md", "true", "false"),
        (0, 9, "", ".github/workflows/ci.yml", "true", "true"),
        (0, 9, "", "scripts/prepush_checks.py", "true", "true"),
    ],
)
def test_classifier_failures(tmp_path, diff_exit, script_exit, output, changed, expected, valid):
    text = (REPO / ".github/workflows/ci.yml").read_text()
    script = _run_block(text, "      - id: classify")
    fake_git = tmp_path / "git"
    fake_git.write_text('#!/bin/bash\nprintf "%s\\n" "$CHANGED"\nexit "$DIFF_EXIT"\n')
    fake_git.chmod(0o755)
    fake_python = tmp_path / "python3"
    fake_python.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$CLASSIFIER_OUTPUT"\nexit "$SCRIPT_EXIT"\n'
    )
    fake_python.chmod(0o755)
    target = tmp_path / "outputs"
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "EVENT": "pull_request",
            "REF_TYPE": "branch",
            "BASE_REF": "main",
            "BEFORE": "",
            "DIFF_EXIT": str(diff_exit),
            "SCRIPT_EXIT": str(script_exit),
            "CLASSIFIER_OUTPUT": output,
            "CHANGED": changed,
            "GITHUB_OUTPUT": str(target),
        },
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert dict(line.split("=", 1) for line in target.read_text().splitlines()) == {
        "code": expected,
        "valid": valid,
        "base": "origin/main",
    }


def test_rehearsal_ci_wiring():
    ci = (REPO / ".github/workflows/ci.yml").read_text()
    call = ci.split("  workflow-examples:\n", 1)[1].split("  workflow-examples-required:", 1)[0]
    assert "needs: classify" in call
    assert (
        "!cancelled() && github.event_name == 'pull_request' && needs.classify.outputs.code != 'false'"
        in call
    )
    assert "uses: ./.github/workflows/workflow-examples.yml" in call
    required = ci.split("  workflow-examples-required:", 1)[1].split("  # The docs gate", 1)[0]
    assert "needs: [classify, workflow-examples]" in required
    assert "always() && github.event_name == 'pull_request'" in required
    for name, cron in [
        ("workflow-examples", "17 1 * * *"),
        ("service-drill", "37 1 * * *"),
        ("dst-drill", "17 2 * * *"),
    ]:
        text = (REPO / f".github/workflows/{name}.yml").read_text()
        assert f"cron: '{cron}'" in text
        assert "workflow_dispatch:" in text and "contents: read" in text
        assert "rehearsal_image.py" not in text and "continue-on-error" not in text
        assert "if: always()" in text
    workflow = (REPO / ".github/workflows/workflow-examples.yml").read_text()
    assert "workflow_call:" in workflow and "pull_request:" not in workflow
    dst = (REPO / ".github/workflows/dst-drill.yml").read_text()
    lanes = re.findall(r"matrix.lane == '([^']+)'[^\n]*\n.*?drill.py ([\w-]+)", dst, re.S)
    assert lanes == [
        ("falls", "london-fall"),
        ("falls", "newyork-fall"),
        ("restart", "london-fall-restart"),
        ("restart", "london-spring"),
        ("restart", "london-midnight"),
        ("clock", "clock-step"),
        ("clock", "newyork-spring"),
    ]
    local = (REPO / "scripts/dst-drill-local.sh").read_text()
    for lane, scenarios in [
        ("falls", "london-fall newyork-fall"),
        ("restart", "london-fall-restart london-spring london-midnight"),
        ("clock", "clock-step newyork-spring"),
    ]:
        assert f"run_lane {lane} {scenarios} &" in local


@pytest.mark.parametrize("failure", [0, 43])
@pytest.mark.parametrize("permission_failure", [False, True])
def test_service_exports_before_cleanup_on_build_failure(tmp_path, failure, permission_failure):
    """O, integration-testing §4: export first; remove only a recorded owned-container ID."""
    tree = tmp_path / "tree"
    deploy = tree / "examples/nightbank/deploy"
    deploy.mkdir(parents=True)
    (deploy / "drill-local.sh").write_bytes(
        (REPO / "examples/nightbank/deploy/drill-local.sh").read_bytes()
    )
    (deploy / "drill-steps.sh").write_text("#!/bin/bash\necho install\n")
    (tree / "pyproject.toml").write_text('version = "2.0.0"\n')
    scripts = tree / "scripts"
    scripts.mkdir()
    log = tmp_path / "commands"
    wargame = scripts / "wargame.sh"
    wargame.write_text(
        '#!/bin/bash\necho "wargame $*" >> "$TEST_LOG"\n'
        'if [ "$3" = build ]; then exit "$TEST_FAILURE"; fi\n'
    )
    wargame.chmod(0o755)
    tools = tmp_path / "bin"
    tools.mkdir()
    for name, body in {
        "uv": "exit 0",
        "git": "printf 'pyproject.toml\\0'",
        "podman": 'echo "podman $*" >> "$TEST_LOG"; '
        'if [ "$1:$2:$3" = "inspect:--format:{{.Id}}" ]; then echo recorded-container-id; '
        'elif [ "$1:$2" = "inspect:--format" ]; then echo "$PWD"; '
        'elif [ "$1" = cp ]; then mkdir -p "$3"; printf captured >"$3/captured"; '
        "else echo evidence; fi",
        "chown": f'echo "chown $*" >> "$TEST_LOG"; exec {shutil.which("chown")} "$@"',
        "chmod": 'echo "chmod $*" >> "$TEST_LOG"; '
        'if [ "$TEST_MODE_FAILURE" = 1 ]; then exit 37; fi; '
        f'exec {shutil.which("chmod")} "$@"',
    }.items():
        path = tools / name
        path.write_text("#!/bin/bash\n" + body + "\n")
        path.chmod(0o755)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    unrelated = evidence / "unrelated"
    unrelated.write_text("other evidence")
    unrelated.chmod(0o200)
    original = unrelated.stat()
    result = subprocess.run(
        ["bash", str(deploy / "drill-local.sh"), "all"],
        cwd=tree,
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "PODMAN": str(tools / "podman"),
            "DRILL_EVIDENCE": str(evidence),
            "TEST_LOG": str(log),
            "TEST_FAILURE": str(failure),
            "TEST_MODE_FAILURE": "1" if permission_failure else "0",
        },
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == (failure or int(permission_failure)), result.stderr
    commands = log.read_text()
    assert "wargame --base ubuntu dispose" not in commands
    removed = commands.index("podman rm --force recorded-container-id")
    assert commands.index("podman inspect") < removed
    assert commands.index("podman logs") < removed
    assert commands.index("drill-steps.sh diagnostics") < commands.index(
        "podman rm --force recorded-container-id"
    )
    assert removed < commands.index("chown -Rh") < commands.index("chmod -R u+rX")
    targets = [evidence / "storage", evidence / "storage-filesystem"]
    if permission_failure:
        targets = targets[:1]
    assert [line for line in commands.splitlines() if line.startswith("chown ")] == [
        f"chown -Rh {os.getuid()}:{os.getgid()} {path}" for path in targets
    ]
    assert [line for line in commands.splitlines() if line.startswith("chmod ")] == [
        f"chmod -R u+rX {path}" for path in targets
    ]
    assert (unrelated.stat().st_mode, unrelated.stat().st_uid, unrelated.stat().st_gid) == (
        original.st_mode,
        original.st_uid,
        original.st_gid,
    )
    assert (evidence / "container.id").read_text().strip() == "recorded-container-id"
    for filename in [
        "container.json",
        "container.log",
        "diagnostics.log",
        "build.log",
        "build.json",
    ]:
        assert (evidence / filename).exists()


@pytest.mark.parametrize("present", [False, True])
def test_service_upload_can_read_only_its_exported_evidence(tmp_path, present):
    """O, integration-testing §4: export is readable after teardown; other files keep mode."""
    workflow = (REPO / ".github/workflows/service-drill.yml").read_text()
    marker = "      - name: make restart evidence readable"
    assert workflow.index("      - name: the service drill") < workflow.index(marker)
    assert workflow.index(marker) < workflow.index("      - uses: actions/upload-artifact@")
    assert workflow.split(marker, 1)[1].startswith("\n        if: always()\n")
    evidence = tmp_path / "service-evidence"
    evidence.mkdir()
    exported = evidence / "restart-faults"
    if present:
        exported.mkdir(mode=0o700)
        record = exported / "receipt.json"
        record.write_text('{"result":"passed"}\n')
        record.chmod(0o000)
    other = evidence / "unrelated"
    other.write_text("held")
    other.chmod(0o000)
    tools = tmp_path / "bin"
    tools.mkdir()
    sudo = tools / "sudo"
    sudo.write_text('#!/bin/bash\nexec "$@"\n')
    sudo.chmod(0o755)
    try:
        result = subprocess.run(
            ["bash", "-e", "-c", _run_block(workflow, marker)],
            env={
                **os.environ,
                "PATH": f"{tools}:{os.environ['PATH']}",
                "DRILL_EVIDENCE": str(evidence),
            },
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        if present:
            assert record.read_text() == '{"result":"passed"}\n'
            assert record.stat().st_mode & 0o400
            assert exported.stat().st_mode & 0o500 == 0o500
        else:
            assert not exported.exists()
        assert other.stat().st_mode & 0o777 == 0
    finally:
        other.chmod(0o600)
        if present:
            record.chmod(0o600)


def test_upload_paths_have_no_relative_segments(tmp_path):
    service = (REPO / ".github/workflows/service-drill.yml").read_text()
    script = _run_block(service, "      - name: prepare service evidence")
    output = tmp_path / "github-env"
    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_ENV": str(output)},
        capture_output=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    exported = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert exported == {"DRILL_EVIDENCE": str(tmp_path / "service-evidence")}
    contexts = {
        "runner.temp": str(tmp_path),
        "github.workspace": "/checkout",
        **{f"env.{name}": value for name, value in exported.items()},
    }
    checked = 0
    for workflow in sorted((REPO / ".github/workflows").glob("*.y*ml")):
        for upload in re.split(
            r"(?m)^ +(?:- )?uses: actions/upload-artifact@", workflow.read_text()
        )[1:]:
            match = re.search(r"(?m)^( +)path: (.*)$", upload)
            assert match is not None, workflow
            indent, value = match.groups()
            if value in ("|", ">", "|-", ">-"):
                paths = []
                for line in upload[match.end() :].splitlines():
                    if line.strip() and len(line) - len(line.lstrip()) <= len(indent):
                        break
                    if line.strip():
                        paths.append(line.strip())
            else:
                paths = [value.strip("\"'")]
            for path in paths:
                expanded = re.sub(r"\$\{\{\s*(.*?)\s*\}\}", lambda m: contexts[m[1]], path)
                assert not {".", ".."}.intersection(expanded.replace("\\", "/").split("/")), (
                    workflow,
                    path,
                    expanded,
                )
                checked += 1
    assert checked >= 6


@pytest.mark.parametrize("workflow", ["service-drill", "dst-drill"])
def test_drill_filters_exclude_docs_only_changes(workflow):
    text = (REPO / f".github/workflows/{workflow}.yml").read_text()
    patterns = re.findall(r"^      - '([^']+)'$", text.split("\npermissions:", 1)[0], re.M)
    assert patterns[-1] == "!examples/**/*.md"

    def starts(paths):
        for path in paths:
            matched = False
            for pattern in patterns:
                negative = pattern.startswith("!")
                if fnmatch.fnmatchcase(path, pattern.removeprefix("!")):
                    matched = not negative
            if matched:
                return True
        return False

    for path in [
        "README.md",
        "CLAUDE.md",
        "docs/runner-design.md",
        "examples/nightbank/README.md",
        "examples/nightbank/deploy/DRILL.md",
        "examples/nightbank/deploy/nested/notes.md",
        "examples/dst-drill/README.md",
        "examples/dst-drill/nested/notes.md",
        ".claude/skills/example/SKILL.md",
    ]:
        assert not starts([path]), path
    own_code = (
        "examples/nightbank/deploy/drill-local.sh"
        if workflow == "service-drill"
        else "examples/dst-drill/drill.py"
    )
    for path in [
        "src/dsl41/runner.py",
        "src/notes.md",
        "grammars/jil.lark",
        "grammars/notes.md",
        "pyproject.toml",
        "uv.lock",
        own_code,
        f".github/workflows/{workflow}.yml",
    ]:
        assert starts([path]), path
        assert starts(["examples/dst-drill/README.md", path]), path
