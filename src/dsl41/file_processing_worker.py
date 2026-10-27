"""The deployed file-processing worker: one archive or process run, then exit.

    python -m dsl41.file_processing_worker archive --root ROOT --processor-id ID
    python -m dsl41.file_processing_worker process --root ROOT --processor-id ID \\
        --handler MODULE:FACTORY

FACTORY is the job author's callable. It takes the root and returns the handler.
The command needs the installed package and the handler's module, not a checkout.
Exit codes follow CONTRACT.md "Activation is external": 0 the snapshot completed,
1 the run stopped, 75 the role's lock was busy, 65 a refusal. A command line or a
handler that cannot be loaded exits 2 before the run touches the root.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
import importlib
import json
from pathlib import Path
import sys
from typing import Literal

from dsl41.file_processing import Handler, LockBusy, Policy, archive, process

EXIT_STOPPED = 1
EXIT_LOCK_BUSY = 75  # EX_TEMPFAIL: the role's lock was busy; nothing ran.
EXIT_REFUSED = 65  # EX_DATAERR: corrupt, inconsistent or unsupported worker input.


@contextmanager
def exit_codes(command: str) -> Iterator[None]:
    """Map a busy lock to 75 and a refusal to 65, each with one line on stderr.

    ProtocolError is a ValueError; the library's other ValueErrors reject arguments
    such as an invalid processor id. A ledger busy past its timeout stays an
    OperationalError, so it exits 1 with its reason on stderr (CONTRACT.md).
    """
    try:
        yield
    except LockBusy as exc:
        print(f"{exc}.lock busy: another {exc} run owns it", file=sys.stderr)
        raise SystemExit(EXIT_LOCK_BUSY) from None
    except ValueError as exc:
        print(f"{command} refused: {' '.join(str(exc).split())}", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED) from None


def load_handler(spec: str, root: Path) -> Handler:
    """Import MODULE:FACTORY and call it with the root.

    A handler without a process method is refused here, before any claim, so a
    misconfiguration never turns work UNKNOWN.
    """
    module, _, attr = spec.partition(":")
    if not module or not attr:
        raise ValueError("expected MODULE:FACTORY")
    factory = getattr(importlib.import_module(module), attr)
    handler: Handler = factory(root)
    if not callable(getattr(handler, "process", None)):
        raise ValueError(f"{attr}(root) returned an object without a process method")
    return handler


def run(
    role: Literal["archive", "process"],
    root: Path,
    processor_id: str,
    policy: Policy,
    handler: Handler | None = None,
) -> None:
    """Run one role, print its JSON summary, and exit 1 when the run stopped."""
    with exit_codes(role):
        if role == "archive":
            summary = archive(root, processor_id, policy)
        else:
            assert handler is not None, "a process run needs a handler"
            summary = process(root, processor_id, handler, policy)
    print(json.dumps(asdict(summary), sort_keys=True))
    if summary.stopped:
        raise SystemExit(EXIT_STOPPED)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m dsl41.file_processing_worker",
        description="Run one file-processing role over a root, then exit.",
    )
    roles = parser.add_subparsers(dest="role", required=True)
    for role in ("archive", "process"):
        sub = roles.add_parser(role)
        sub.add_argument("--root", type=Path, required=True)
        sub.add_argument("--processor-id", required=True)
        sub.add_argument("--order", choices=("oldest", "newest"), default="oldest")
        sub.add_argument("--batch", choices=("one", "all"), default="all")
        if role == "process":
            sub.add_argument("--handler", required=True, metavar="MODULE:FACTORY")
    args = parser.parse_args(argv)
    handler = None
    if args.role == "process":
        try:
            handler = load_handler(args.handler, args.root)
        except Exception as exc:  # the author's import or factory failed; nothing ran
            parser.error(f"--handler {args.handler}: {exc}")
    run(args.role, args.root, args.processor_id, Policy(args.order, args.batch), handler)


if __name__ == "__main__":
    main()
