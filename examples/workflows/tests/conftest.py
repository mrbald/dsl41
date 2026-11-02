"""Fixtures for the workflow examples' integration lane (DL-236, DL-441).

A missing engine, compose or image build fails the session fixture, so every
test errors by name. The lane has no skip of any kind.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from lane import Lane, Result, compose, docker, execute, now, write_json

EVIDENCE = pytest.StashKey[Path]()
# The README's version command (Container setup), run once in the built image.
VERSIONS = (
    "python --version; ffmpeg -version; /usr/local/bin/python -m pip --version;"
    " /usr/local/bin/uv pip freeze --python /app/.venv/bin/python"
)


def _required(record: dict[str, Any], name: str, args: list[str], timeout: float) -> Result:
    result = execute(args, timeout)
    record.setdefault("commands", []).append({"name": name, **asdict(result)})
    if result.exit != 0:
        record["failure"] = result.describe()
        pytest.fail(
            f"workflow lane session: {name} failed; no example can run.\n{result.describe()}",
            pytrace=False,
        )
    return result


def _evidence_root(factory: pytest.TempPathFactory) -> Path:
    configured = os.environ.get("WORKFLOW_EVIDENCE")
    root = Path(configured) if configured else factory.getbasetemp() / "workflow-evidence"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


@pytest.fixture(scope="session")
def workflow_session(
    tmp_path_factory: pytest.TempPathFactory, pytestconfig: pytest.Config
) -> Iterator[tuple[Path, str]]:
    """Check the engine and compose, build the runner image once, record the provider."""
    started = time.monotonic()
    root = _evidence_root(tmp_path_factory)
    pytestconfig.stash[EVIDENCE] = root
    path = root / "session.json"
    record: dict[str, Any] = {
        "started_at": now(),
        "workflow_context": os.environ.get("WORKFLOW_CONTEXT"),
    }
    image_built = False
    runner_tag = f"localhost/dsl41-workflow-examples:dsl41-wf-session-{secrets.token_hex(6)}"
    try:
        project = f"dsl41-wf-session-{secrets.token_hex(6)}"
        record["runner_image"] = runner_tag
        record["compose_version"] = _required(
            record, "compose version", [*docker(), "compose", "version"], 60
        ).stdout.strip()
        _required(record, "engine info", [*docker(), "info"], 60)
        record["docker_version"] = json.loads(
            _required(
                record, "docker version", [*docker(), "version", "--format", "{{json .}}"], 60
            ).stdout
        )
        context = os.environ.get("WORKFLOW_CONTEXT")
        inspected = json.loads(
            _required(
                record,
                "context inspect",
                [*docker(), "context", "inspect", *([context] if context else [])],
                60,
            ).stdout
        )[0]
        record["context"] = inspected["Name"]
        record["endpoint"] = inspected["Endpoints"]["docker"]["Host"]
        config = json.loads(
            _required(
                record,
                "compose config",
                [*compose(project, runner_tag), "config", "--format", "json"],
                60,
            ).stdout
        )
        postgres = config["services"]["postgres"]["image"]
        runner = config["services"]["runner"]["image"]
        record["postgres_image"] = postgres
        record["postgres_digest"] = postgres.partition("@")[2]
        record["build_started_at"] = now()
        _required(record, "runner build", [*compose(project, runner_tag), "build", "runner"], 1200)
        image_built = True
        record["build_finished_at"] = now()
        record["runner_image"] = runner
        record["runner_image_id"] = _required(
            record,
            "runner image inspect",
            [*docker(), "image", "inspect", runner, "-f", "{{.Id}}"],
            60,
        ).stdout.strip()
        # The session records versions and removes its own tag after exporting evidence.
        record["image_versions"] = _required(
            record,
            "image versions",
            [*docker(), "run", "--rm", "--pull", "never", "--network", "none", runner]
            + ["sh", "-c", VERSIONS],
            120,
        ).stdout
        write_json(path, record)
        yield root, runner_tag
    except BaseException as exc:
        record.setdefault("failure", repr(exc))
        raise
    finally:
        cleanup = None
        if image_built:
            cleanup = execute([*docker(), "image", "rm", "--no-prune", runner_tag], 60)
            record.setdefault("commands", []).append(
                {"name": "runner image removal", **asdict(cleanup)}
            )
        record["finished_at"] = now()
        record["duration_s"] = time.monotonic() - started
        write_json(path, record)
        if cleanup is not None:
            cleanup.require()


@pytest.fixture
def lane(workflow_session: tuple[Path, str], request: pytest.FixtureRequest) -> Iterator[Lane]:
    """A fresh compose project per test, always torn down with its volumes."""
    name = request.node.name.removeprefix("test_").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", name).strip("-")[:32].strip("-")
    project = f"dsl41-wf-{slug}-{secrets.token_hex(3)}"
    root, runner_tag = workflow_session
    owned = Lane(project, root / project, runner_tag)
    try:
        owned.up()
        yield owned
    finally:
        owned.close()


def pytest_terminal_summary(terminalreporter: Any, config: pytest.Config) -> None:
    root = config.stash.get(EVIDENCE, None)
    if root is not None:
        terminalreporter.write_line(f"workflow lane evidence: {root}")
