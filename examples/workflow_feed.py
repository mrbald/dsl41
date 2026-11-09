"""Finite synthetic UTF-8 delivery groups, sharing the file driver's root and ledger.

Only the requested group is published. Staging lives in reserved .workflow-stage;
watch final data names, never that directory. Publication is whole-file and
no-overwrite under the controlled-writer precondition, but a group is not atomic.
A completed release can be replayed after consumption without recreating inputs.
A pending release refuses replay. Choose a new release identity for new input.
"""

import argparse
import importlib
import json
from pathlib import Path
import sys
from typing import Any

driver = importlib.import_module(
    f"{__package__}.workflow_driver" if __package__ else "workflow_driver"
)
files = importlib.import_module(
    f"{__package__}.workflow_files" if __package__ else "workflow_files"
)

MAX_FILE = 1024 * 1024
MAX_GROUP = 16 * MAX_FILE


def load_manifest(path: Path) -> dict[str, list[dict[str, str]]]:
    manifest = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=driver.reject_constant,
        object_pairs_hook=driver.unique_object,
    )
    manifest = driver.object_fields(manifest, {"groups"}, "manifest")
    groups = manifest.get("groups")
    if not isinstance(groups, dict) or not groups:
        raise ValueError("groups must be a nonempty object")
    validated = {}
    for name, items in groups.items():
        driver.string(name, "group")
        if not isinstance(items, list) or not 1 <= len(items) <= 256:
            raise ValueError("group must contain 1 to 256 files")
        total = 0
        targets = []
        for item in items:
            driver.object_fields(item, {"target", "content"}, "item")
            targets.append(files.relative(item.get("target")))
            content = item.get("content")
            if not isinstance(content, str):
                raise ValueError("content must be a UTF-8 string")
            size = len(content.encode("utf-8"))
            if size > MAX_FILE:
                raise ValueError("file exceeds 1 MiB")
            total += size
        if total > MAX_GROUP:
            raise ValueError("group exceeds 16 MiB")
        files.distinct(targets)
        validated[name] = items
    return validated


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate or publish one finite synthetic delivery group.",
        epilog='Manifest: {"groups":{"first":[{"target":"inbox/item.ready",'
        '"content":"synthetic bytes"}]}}. Limits: 1 MiB/file, 256 files/group, '
        "16 MiB/group. All data directories must be precreated, owned and private. "
        "--validate makes no ledger or data changes. --release requires --namespace "
        "and --release-id. Namespace belongs to one root and ledger; root recreation "
        "or ledger deletion invalidates evidence. A completed release reuses its "
        "receipt without recreating consumed inputs. A pending release refuses replay. "
        "Exit 0: validated/completed/reused; 1: partial I/O; 2: configuration, "
        "precondition, busy, uncertain receipt or ledger/evidence error. No scheduler "
        "status is read or written. No retries, rollback or atomic-group guarantee.",
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--file-root", type=Path, required=True)
    parser.add_argument("--db", "--ledger", dest="ledger", type=Path, required=True)
    parser.add_argument("--namespace")
    parser.add_argument("--release-id")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--validate", action="store_true")
    mode.add_argument("--release", action="store_true")
    args = parser.parse_args()
    try:
        groups = load_manifest(args.manifest)
        if args.group not in groups:
            raise ValueError("unknown group")
        root = files.FileRoot(args.file_root, args.ledger)
        action: dict[str, Any] = {"operation": "publish", "items": groups[args.group]}
        if args.validate:
            files.select(root, action)
            print(json.dumps({"version": 1, "outcome": "validated", "group": args.group}))
            return 0
        namespace = driver.string(args.namespace, "namespace")
        release = driver.string(args.release_id, "release-id")
        action["key"] = "release"
        receipt = files.execute(
            root,
            action,
            schema=driver.SCHEMA,
            job="feeder",
            scenario="feeder",
            batch=args.group,
            namespace=namespace,
            logical_run=release,
        )
        print(json.dumps(receipt, sort_keys=True))
        return int(files.exit_code(receipt))
    except Exception as error:
        failure: dict[str, Any] = {"version": 1, "error": str(error)}
        if isinstance(error, files.EvidenceError):
            failure.update(
                invocation_id=error.invocation,
                cause="evidence_error",
                uncommitted_cause=error.cause,
            )
        print(json.dumps(failure, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
