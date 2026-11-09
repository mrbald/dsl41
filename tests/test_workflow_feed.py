"""Finite synthetic releases: whole targets, explicit replay and partial evidence."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from examples import workflow_driver as driver
from examples import workflow_feed as feed
from examples import workflow_files as files
from test_workflow_files import MOVE, put, records, run
from test_workflow_files import sandbox as sandbox


FEEDER = Path(__file__).resolve().parents[1] / "examples" / "workflow_feed.py"
pytestmark = pytest.mark.usefixtures("cli_env")


def invoke(root, items=None, *, release="one", validate=False, manifest=None, group="first"):
    path = root.root.parent / "manifest.json"
    path.write_text(json.dumps(manifest if manifest is not None else {"groups": {group: items}}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("DSL41_")}
    return subprocess.run(
        [
            sys.executable,
            str(FEEDER),
            "--manifest",
            str(path),
            "--group",
            group,
            "--file-root",
            str(root.root),
            "--db",
            str(root.ledger),
            "--namespace",
            "deliveries",
            "--release-id",
            release,
            "--validate" if validate else "--release",
        ],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
    )


def publish(root, items, **kwargs):
    return files.execute(
        root,
        {"operation": "publish", "items": items, "key": "release"},
        schema=driver.SCHEMA,
        job="feeder",
        scenario="feeder",
        batch="first",
        namespace="deliveries",
        logical_run=kwargs.pop("release", "one"),
        **kwargs,
    )


ITEMS = [{"target": "inbox/item.ready", "content": "first synthetic\n"}]


def test_dry_validation_has_no_data_or_ledger_mutation(sandbox):
    result = invoke(sandbox, ITEMS, validate=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["outcome"] == "validated"
    assert list((sandbox.root / "inbox").iterdir()) == []
    assert not sandbox.ledger.exists()
    assert not (sandbox.root / files.LOCK).exists()
    assert not (sandbox.root / files.STAGE).exists()


def test_completed_release_replay_does_not_recreate_consumed_input(sandbox):
    first = invoke(sandbox, ITEMS)
    assert first.returncode == 0, first.stderr
    assert (sandbox.root / "inbox/item.ready").read_bytes() == b"first synthetic\n"
    assert run(sandbox, MOVE)["outcome"] == "success"
    replay = invoke(sandbox, ITEMS)
    assert replay.returncode == 0, replay.stderr
    assert (
        json.loads(replay.stdout)["reused_from"]["invocation_id"]
        == json.loads(first.stdout)["invocation_id"]
    )
    assert not (sandbox.root / "inbox/item.ready").exists()
    assert invoke(sandbox, ITEMS, release="two").returncode == 0
    assert (sandbox.root / "inbox/item.ready").read_bytes() == b"first synthetic\n"
    collision = invoke(sandbox, ITEMS, release="three")
    assert collision.returncode == 2
    assert json.loads(collision.stdout)["cause"] == "collision"
    assert len(records(sandbox, "attempts")) == 5


@pytest.mark.parametrize(
    "items",
    [
        [],
        [{"target": "../escape", "content": "x"}],
        [{"target": "inbox/one", "content": "x", "command": "forbidden"}],
        [{"target": "inbox/one", "content": "x"}, {"target": "inbox/one", "content": "y"}],
        [{"target": "inbox/Item", "content": "x"}, {"target": "inbox/item", "content": "y"}],
        [
            {"target": "inbox/caf\u00e9", "content": "x"},
            {"target": "inbox/cafe\u0301", "content": "y"},
        ],
        [{"target": "inbox/one", "content": "x" * (feed.MAX_FILE + 1)}],
        [{"target": f"inbox/item-{i}", "content": "x"} for i in range(257)],
        [{"target": f"inbox/item-{i}", "content": "x" * feed.MAX_FILE} for i in range(17)],
        [{"target": "inbox/one", "content": 3}],
    ],
)
def test_malformed_oversized_and_duplicate_groups_publish_no_prefix(sandbox, items):
    result = invoke(sandbox, items)
    assert result.returncode == 2
    assert not result.stdout
    assert list((sandbox.root / "inbox").iterdir()) == []
    assert not sandbox.ledger.exists()


def test_all_groups_validate_but_only_requested_group_is_published(sandbox):
    manifest = {
        "groups": {"first": ITEMS, "second": [{"target": "inbox/second", "content": "two"}]}
    }
    result = invoke(sandbox, manifest=manifest)
    assert result.returncode == 0, result.stderr
    assert (sandbox.root / "inbox/item.ready").exists()
    assert not (sandbox.root / "inbox/second").exists()


def test_existing_alias_is_preflighted_before_any_publication(sandbox):
    put(sandbox, "inbox/occupied", b"existing")
    items = [
        {"target": "inbox/first", "content": "new"},
        {"target": "inbox/occupied", "content": "existing"},
    ]
    result = invoke(sandbox, items)
    assert result.returncode == 2
    assert json.loads(result.stdout)["cause"] == "collision"
    assert not (sandbox.root / "inbox/first").exists()
    assert (sandbox.root / "inbox/occupied").read_bytes() == b"existing"
    upper = sandbox.root / "inbox/OCCUPIED"
    if upper.exists():
        alias = invoke(
            sandbox,
            [
                {"target": "inbox/first", "content": "new"},
                {"target": "inbox/OCCUPIED", "content": "same"},
            ],
            release="alias",
        )
        assert alias.returncode == 2
        assert json.loads(alias.stdout)["cause"] == "collision"
        assert not (sandbox.root / "inbox/first").exists()


def test_staged_bytes_are_complete_and_hidden_until_publication(sandbox):
    content = "\u03bb" * 1000

    def observe(phase, index):
        if phase == "staged":
            assert not (sandbox.root / "inbox/item.ready").exists()
            assert list((sandbox.root / "inbox").glob("*.ready")) == []
            staged = list((sandbox.root / files.STAGE).iterdir())
            assert len(staged) == 1
            assert staged[0].name.startswith(".")
            assert staged[0].read_bytes() == content.encode("utf-8")

    result = publish(sandbox, [{"target": "inbox/item.ready", "content": content}], fault=observe)
    assert result["outcome"] == "success"
    assert (sandbox.root / "inbox/item.ready").read_bytes() == content.encode("utf-8")
    assert list((sandbox.root / files.STAGE).iterdir()) == []


def test_injected_mid_group_failure_preserves_completed_and_uncertain_prefix(sandbox):
    items = [{"target": f"inbox/{name}", "content": name} for name in ("first", "second", "third")]

    def fault(phase, index):
        if phase == "after_mutation" and index == 1:
            raise OSError("injected post-publication I/O failure")

    result = publish(sandbox, items, fault=fault)
    assert (result["outcome"], result["cause"]) == ("failure", "partial_io")
    assert [m["path"] for m in result["result"]["members"]] == ["inbox/first"]
    assert result["result"]["uncertain"] == {"path": "inbox/second", "index": 1}
    assert (sandbox.root / "inbox/first").read_bytes() == b"first"
    assert (sandbox.root / "inbox/second").read_bytes() == b"second"
    assert not (sandbox.root / "inbox/third").exists()
    replay = publish(sandbox, items)
    assert replay["cause"] == "uncertain"
    assert not (sandbox.root / "inbox/third").exists()
    assert [r["state"] for r in records(sandbox)] == ["pending", "refused"]
    assert len(records(sandbox, "attempts")) == 2


def test_feeder_and_worker_deliberate_lock_contention(sandbox):
    put(sandbox, MOVE["source"])
    with sandbox.locked():
        busy = invoke(sandbox, [{"target": "inbox/second", "content": "second"}])
        assert run(sandbox, MOVE)["cause"] == "busy"
    assert busy.returncode == 2
    assert json.loads(busy.stdout)["cause"] == "busy"
    assert (sandbox.root / MOVE["source"]).exists()
    assert not (sandbox.root / "inbox/second").exists()
    assert len(records(sandbox, "attempts")) == 2
    assert all(r["claim_key"] is None and r["cause"] == "busy" for r in records(sandbox))


def test_publication_cannot_alias_ledger_or_reserved_metadata(sandbox):
    root = files.FileRoot(sandbox.root, sandbox.root / "ledger.sqlite")
    assert run(root, {"operation": "delay", "seconds": 0})["outcome"] == "success"
    for target in ("LEDGER.SQLITE", "ledger.sqlite-wal", files.STAGE + "/item", files.LOCK):
        result = invoke(root, [{"target": target, "content": "forbidden"}], release=target)
        assert result.returncode == 2
        assert json.loads(result.stdout)["cause"] == "reserved_path"
    with closing(sqlite3.connect(root.ledger)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT COUNT(*) FROM attempts").fetchone() == (5,)


def test_symlink_staging_refuses_before_claim_or_publication(sandbox, tmp_path):
    outside = tmp_path / "outside-stage"
    outside.mkdir()
    (outside / "sentinel").write_bytes(b"untouched")
    (sandbox.root / files.STAGE).symlink_to(outside, target_is_directory=True)
    result = invoke(sandbox, ITEMS)
    assert result.returncode == 2
    assert json.loads(result.stdout)["cause"] == "unsupported_path"
    assert records(sandbox)[0]["claim_key"] is None
    assert records(sandbox)[0]["state"] == "refused"
    assert list((sandbox.root / "inbox").iterdir()) == []
    assert list(outside.iterdir()) == [outside / "sentinel"]
    assert (outside / "sentinel").read_bytes() == b"untouched"
