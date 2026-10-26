"""Sample operator CLI: list work, show one row, rule one row. JSON on stdout.

Exit codes are the worker's. list and show take no role lock. rule takes
process.lock and checks the expected state and attempt in the transaction that
moves the row (CONTRACT.md "Failure disposition is `stop`").
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import getpass
import json
from pathlib import Path

from dsl41.file_processing import list_work, rule_work, show_work
from examples.file_processing.exits import exit_codes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--processor-id", default="counts-v1")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="every work row with its diagnostics")
    show = commands.add_parser("show", help="one work row and its decision record")
    show.add_argument("--key", required=True)
    rule = commands.add_parser("rule", help="rule one work row under process.lock")
    rule.add_argument("--key", required=True)
    rule.add_argument(
        "--event", required=True, choices=("completed", "retry_safe", "rejected", "uncertain")
    )
    rule.add_argument("--expect-state", required=True)
    rule.add_argument("--expect-attempt", required=True, type=int)
    rule.add_argument("--actor", default=None, help="default: the login name")
    given = rule.add_mutually_exclusive_group(required=True)
    given.add_argument("--reason")
    given.add_argument("--evidence")
    args = parser.parse_args()
    with exit_codes(args.command):
        value: object
        if args.command == "list":
            value = [asdict(status) for status in list_work(args.root, args.processor_id)]
        elif args.command == "show":
            status, record = show_work(args.root, args.processor_id, args.key)
            value = {"work": asdict(status), "decisions": [asdict(item) for item in record]}
        else:
            value = asdict(
                rule_work(
                    args.root,
                    args.processor_id,
                    args.key,
                    args.event,
                    expect_state=args.expect_state,
                    expect_attempt=args.expect_attempt,
                    actor=args.actor or getpass.getuser(),
                    reason=args.reason,
                    evidence=args.evidence,
                )
            )
    print(json.dumps(value, sort_keys=True))


if __name__ == "__main__":
    main()
