"""Run the synthetic business demo without a scheduler or supervisor."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3

from dsl41.file_processing import Policy, archive, process
from examples.file_processing.worker import CountsHandler


def run(root: Path) -> dict[str, object]:
    if root.exists() and any(root.iterdir()):
        raise ValueError("demo requires an empty root")
    handler = CountsHandler(root / "output")
    before = process(root, "counts-v1", handler)
    for name, data in (
        ("001-report.txt", b"one\ntwo\n"),
        ("002-report.txt", b"one\ntwo\n"),
        ("003-report.txt", b"three\n"),
        ("004-records.json", b'[{"n":1},{"n":2},{"n":3}]\n'),
    ):
        (root / "inbox" / name).write_bytes(data)
    archived = archive(root, "counts-v1")
    first = process(root, "counts-v1", handler, Policy(batch="one"))
    later = process(root, "counts-v1", handler)
    archive(root, "counts-v1")
    rerun = process(root, "counts-v1", handler)
    db = sqlite3.connect(root / "ledger.sqlite3")
    try:
        receipts = db.execute("SELECT count(*) FROM receipts").fetchone()[0]
        done = db.execute("SELECT count(*) FROM work WHERE state='DONE'").fetchone()[0]
    finally:
        db.close()
    outputs = [json.loads(path.read_text()) for path in sorted((root / "output").glob("*.json"))]
    assert before.selected == rerun.selected == 0
    assert archived.duplicates == 1 and first.remaining == 2 and later.remaining == 0
    assert receipts == 4 and done == len(outputs) == 3
    assert sorted((output["format"], output["count"]) for output in outputs) == [
        ("json", 3),
        ("text", 1),
        ("text", 2),
    ]
    for path in (root / "output").glob("*.json"):
        assert json.loads(path.read_text())["processing_key"] == path.stem
    return {
        "receipts": receipts,
        "objects": len(list((root / "objects").iterdir())),
        "done": done,
        "todo": len(list((root / "todo").iterdir())),
        "before": asdict(before),
        "archive": asdict(archived),
        "first": asdict(first),
        "later": asdict(later),
        "rerun": asdict(rerun),
        "outputs": outputs,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().root), sort_keys=True, indent=2))
