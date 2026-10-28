"""REARM, the example estate's activation: one run after each cycle, then exit (DL-417).

    python -m examples.file_processing.rearm --root ROOT --processor-id ID \\
        --signal READY_SIGNAL --socket CONTROL_SOCKET

It removes the ready signal, publishes it again when work is pending, and sends STARTJOB
to INBOX_FW. Work is pending when a delivery waits in the inbox, when the ledger holds
more RUNNING claims than at REARM's previous run, or when it holds READY or RUNNING work
and no UNKNOWN work and the cycle made progress. Progress is a terminal row or a resolved
claim since REARM's previous run, read from the counts that run left in ROOT/rearm.json;
with no file, both counts are zero. A run without progress, such as a handler that
answers RetrySafe, an unloadable handler, a refusal or a busy lock, starts no cycle, and
an UNKNOWN row stops every processor run until an operator rules (CONTRACT.md "Failure
disposition is `stop`"). The removal comes before
the checks: a delivery published before them is seen, and one published after them comes
with its own signal. The exit status is the sendevent's.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

from dsl41.file_processing import TERMINAL, list_work

MARK = "rearm.json"


def pending(root: Path, processor_id: str) -> bool:
    """Whether work is pending; records this run's counts for the next REARM."""
    states = [row.state for row in list_work(root, processor_id)]
    counts = {
        "terminal": sum(state in TERMINAL for state in states),
        "running": states.count("RUNNING"),
    }
    mark = root / MARK
    before = json.loads(mark.read_text()) if mark.exists() else {"terminal": 0, "running": 0}
    staged = root / f".{MARK}"
    staged.write_text(json.dumps(counts) + "\n")
    os.replace(staged, mark)
    if any((root / "inbox").iterdir()) or counts["running"] > before["running"]:
        return True
    progress = counts["terminal"] > before["terminal"] or counts["running"] < before["running"]
    return bool({"READY", "RUNNING"} & set(states)) and "UNKNOWN" not in states and progress


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--processor-id", required=True)
    parser.add_argument("--signal", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--watcher", default="INBOX_FW")
    args = parser.parse_args()
    args.signal.unlink(missing_ok=True)
    if pending(args.root, args.processor_id):
        args.signal.write_text("ready\n")
    start = ["sendevent", "STARTJOB", "-J", args.watcher, "-S", str(args.socket)]
    raise SystemExit(subprocess.call([sys.executable, "-m", "dsl41", *start]))


if __name__ == "__main__":
    main()
