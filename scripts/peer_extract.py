"""Read a `codex exec --json` stream and keep the thread id and the last agent message.

Usage: peer_extract.py JSONL OUT [--thread ID]

Writes OUT.thread and OUT.md, then prints the thread id and the path of OUT.md.
`--thread` names the id to use when the stream has no `thread.started` event, as on
a resumed thread. Exits 70 when the stream holds no agent message.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

NO_MESSAGE = 70


def read_stream(lines: list[str]) -> tuple[str | None, str | None]:
    """The first `thread.started` thread id and the text of the last `agent_message` item."""
    thread: str | None = None
    message: str | None = None
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started" and thread is None:
            thread = event.get("thread_id")
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                message = text
    return thread, message


def main(argv: list[str]) -> int:
    fallback: str | None = None
    args = list(argv)
    if "--thread" in args:
        at = args.index("--thread")
        if at + 1 >= len(args):
            print("usage: peer_extract.py JSONL OUT [--thread ID]", file=sys.stderr)
            return 64
        fallback = args[at + 1]
        del args[at : at + 2]
    if len(args) != 2:
        print("usage: peer_extract.py JSONL OUT [--thread ID]", file=sys.stderr)
        return 64
    source, out = Path(args[0]), args[1]
    thread, message = read_stream(source.read_text(encoding="utf-8").splitlines())
    thread = thread or fallback
    if thread:
        Path(out + ".thread").write_text(thread + "\n", encoding="utf-8")
    if message is None:
        print(f"peer: no agent message in {source}.", file=sys.stderr)
        return NO_MESSAGE
    Path(out + ".md").write_text(message + "\n", encoding="utf-8")
    print(thread or "")
    print(out + ".md")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
