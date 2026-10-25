"""Two small business functions behind one reconciling sample callback."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from dsl41.file_processing import (
    Completed,
    LockBusy,
    Policy,
    ProtocolError,
    Result,
    RetrySafe,
    Unknown,
    WorkItem,
    archive,
    process,
)


def count_json(data: bytes) -> int:
    records = json.loads(data)
    if not isinstance(records, list):
        raise ValueError("sample JSON input must be a records array")
    return len(records)


def count_text(data: bytes) -> int:
    return len(data.decode("utf-8").splitlines())


class CountsHandler:
    """The sole public effect is an immutable, atomically published count file.

    Missing output therefore proves RetrySafe. Other handlers need their own
    evidence and may be unable to prove that an external effect did not happen.
    """

    def __init__(self, output: Path) -> None:
        self.output = output

    def _expected(self, item: WorkItem) -> bytes:
        data = item.input_path.read_bytes()
        is_json = data.lstrip().startswith((b"[", b"{"))
        return (
            json.dumps(
                {
                    "processing_key": item.processing_key,
                    "content_hash": item.content_hash,
                    "format": "json" if is_json else "text",
                    "count": count_json(data) if is_json else count_text(data),
                },
                sort_keys=True,
            )
            + "\n"
        ).encode()

    def reconcile(self, item: WorkItem) -> Result:
        target = self.output / f"{item.processing_key}.json"
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError:
            return RetrySafe("atomic count output absent; no other public effect")
        if not stat.S_ISREG(mode) or target.read_bytes() != self._expected(item):
            return Unknown("count output conflicts with input identity/content")
        directory = os.open(self.output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        evidence = {
            "output": str(target),
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
        return Completed(json.dumps(evidence, sort_keys=True))

    def process(self, item: WorkItem) -> Result:
        self.output.mkdir(parents=True, exist_ok=True)
        if self.output.is_symlink():
            raise ProtocolError("sample output directory must not be a symlink")
        expected = self._expected(item)
        fd, name = tempfile.mkstemp(prefix=".count-", dir=self.output)
        temp = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(expected)
                stream.flush()
                os.fchmod(stream.fileno(), 0o444)
                os.fsync(stream.fileno())
            try:
                os.link(temp, self.output / f"{item.processing_key}.json")
            except FileExistsError:
                pass
            return self.reconcile(item)
        finally:
            temp.unlink(missing_ok=True)


# Exit codes (CONTRACT.md "Activation is external"): 1 is a stopped run whose reason
# is in the ledger. A busy role lock and a refusal are never success and never a
# stopped run; their values come from sysexits.h. ProtocolError is a ValueError; the
# library's other ValueErrors reject arguments such as an invalid processor id.
EXIT_STOPPED = 1
EXIT_LOCK_BUSY = 75  # EX_TEMPFAIL: the role's lock was busy; nothing ran.
EXIT_REFUSED = 65  # EX_DATAERR: corrupt, inconsistent or unsupported worker input.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("archive", "process"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--processor-id", default="counts-v1")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--order", choices=("oldest", "newest"), default="oldest")
    parser.add_argument("--batch", choices=("one", "all"), default="all")
    args = parser.parse_args()
    policy = Policy(args.order, args.batch)
    try:
        if args.role == "archive":
            summary = archive(args.root, args.processor_id, policy)
        else:
            output = args.output or args.root / "output"
            summary = process(args.root, args.processor_id, CountsHandler(output), policy)
    except LockBusy:
        print(f"{args.role}.lock busy: another {args.role} run owns it", file=sys.stderr)
        raise SystemExit(EXIT_LOCK_BUSY) from None
    except ValueError as exc:
        print(f"{args.role} refused: {' '.join(str(exc).split())}", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED) from None
    print(json.dumps(asdict(summary), sort_keys=True))
    if summary.stopped:
        raise SystemExit(EXIT_STOPPED)


if __name__ == "__main__":
    main()
