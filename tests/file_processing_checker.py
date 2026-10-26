"""Independent side-effect checker for the sample CountsHandler (HANDOVER.md plan row 3).

It reads only the ledger, objects/ and the output directory. It recomputes each
expected output from the archived object with its own counting code, so a defect
shared with the sample handler cannot hide here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3


def expected_output(root: Path, work_key: str, content_hash: str) -> bytes:
    data = (root / "objects" / content_hash).read_bytes()
    assert hashlib.sha256(data).hexdigest() == content_hash, f"object {content_hash} damaged"
    if data.lstrip()[:1] in (b"[", b"{"):
        fmt, count = "json", len(json.loads(data))
    else:
        fmt, count = "text", len(data.decode("utf-8").splitlines())
    value: dict[str, object] = {
        "processing_key": work_key,
        "content_hash": content_hash,
        "format": fmt,
        "count": count,
    }
    return (json.dumps(value, sort_keys=True) + "\n").encode()


def record(root: Path) -> dict[str, list[sqlite3.Row]]:
    """Every work key's decisions, oldest first."""
    db = sqlite3.connect(f"file:{root / 'ledger.sqlite3'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        result: dict[str, list[sqlite3.Row]] = {}
        for row in db.execute("SELECT * FROM decisions ORDER BY seq"):
            result.setdefault(row["work_key"], []).append(row)
        return result
    finally:
        db.close()


def check_counts_outputs(root: Path, output: Path | None = None) -> None:
    """Assert the sample handler's effects agree with the ledger.

    The only permitted entries in the output directory are `<work_key>.json` regular
    files of known work; anything else is stray, including a private `.count-*` stage
    file, because no probe interrupts the handler between staging and publication.
    Every present output, whatever its row's state, holds exactly the recomputed bytes.
    A DONE row has its output. Its single DONE decision's evidence names that file and
    its SHA-256, unless an operator ruled it, whose evidence is free text. A READY or
    REJECTED row has no output unless the record shows an earlier claim. An UNKNOWN or
    RUNNING row may lack its output: its effect is uncertain.
    """
    output = root / "output" if output is None else output
    db = sqlite3.connect(f"file:{root / 'ledger.sqlite3'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        work = db.execute("SELECT work_key, content_hash, state FROM work").fetchall()
    finally:
        db.close()
    decisions = record(root)
    permitted = {f"{row['work_key']}.json" for row in work}
    entries = sorted(output.iterdir()) if output.exists() else []
    stray = [
        path.name
        for path in entries
        if path.name not in permitted or path.is_symlink() or not path.is_file()
    ]
    assert not stray, f"stray output entries: {stray}"
    for row in work:
        key, state = row["work_key"], row["state"]
        target = output / f"{key}.json"
        rows = decisions.get(key, [])
        present = target.exists()
        if present:
            data = target.read_bytes()
            assert data == expected_output(root, key, row["content_hash"]), f"{state} {key}"
        if state == "DONE":
            assert present, f"DONE {key} has no output"
            done = [decision for decision in rows if decision["state"] == "DONE"]
            assert len(done) == 1, f"DONE {key} has {len(done)} DONE decisions"
            if done[0]["origin"] != "operator":
                evidence = json.loads(done[0]["evidence"])
                assert Path(evidence["output"]) == target
                assert evidence["sha256"] == hashlib.sha256(data).hexdigest()
        elif state in ("READY", "REJECTED") and present:
            claims = [decision for decision in rows if decision["chart_row"] == "file_process.01"]
            assert claims, f"{state} {key} has output but no earlier attempt"
