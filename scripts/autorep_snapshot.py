#!/usr/bin/env python3
"""Convert one supported autorep summary table to offline snapshot JSON (DL-387)."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

_HEADINGS = ("Job Name", "Last Start", "Last End", "ST/Ex", "Run/Ntry", "Pri/Xit")
_RULER = re.compile(r"_{8,} _{20} _{20} _{5} _{8} _{7}")
_RUN_TRY = re.compile(r"[0-9]+/[0-9]+")


def _cells(line: str, number: int, spans: list[tuple[int, int]]) -> list[str]:
    trimmed = line.rstrip(" ")
    if len(trimmed) > spans[-1][1]:
        raise ValueError(f"line {number}: content exceeds the ruler width")
    padded = trimmed.ljust(spans[-1][1])
    if any(padded[end] != " " for _, end in spans[:-1]):
        raise ValueError(f"line {number}: shifted column separator")
    return [padded[start:end].strip(" ") for start, end in spans]


def parse_summary(data: bytes) -> dict[str, dict[str, str]]:
    lines = data.decode("utf-8").replace("\r\n", "\n").split("\n")
    for number, line in enumerate(lines, 1):
        if any(not " " <= character <= "~" for character in line):
            raise ValueError(f"line {number}: only printable ASCII table data is supported")
    start, end = 0, len(lines)
    while start < end and not lines[start].strip(" "):
        start += 1
    while end > start and not lines[end - 1].strip(" "):
        end -= 1
    table = list(enumerate(lines[start:end], start + 1))
    if len(table) < 3:
        raise ValueError("expected one header, ruler and at least one data row")
    ruler_number, ruler = table[1]
    if not _RULER.fullmatch(ruler.rstrip(" ")):
        raise ValueError(f"line {ruler_number}: unsupported six-column underscore ruler")
    spans = [match.span() for match in re.finditer(r"_+", ruler)]
    header_number, header = table[0]
    if tuple(_cells(header, header_number, spans)) != _HEADINGS:
        raise ValueError(f"line {header_number}: column headings do not match the ruler")
    nodes: dict[str, dict[str, str]] = {}
    for number, line in table[2:]:
        if not line.strip(" "):
            raise ValueError(f"line {number}: blank lines inside the table are unsupported")
        cells = _cells(line, number, spans)
        for index in range(5):
            if not cells[index]:
                raise ValueError(f"line {number}: missing {_HEADINGS[index]}")
        name, _, _, state, run_try, priority_exit = cells
        if " " in name:
            raise ValueError(f"line {number}: embedded whitespace in Job Name")
        if not _RUN_TRY.fullmatch(run_try):
            raise ValueError(f"line {number}: Run/Ntry must contain decimal digits/digits")
        if " " in priority_exit:
            raise ValueError(f"line {number}: Pri/Xit must be one token when supplied")
        if name in nodes:
            raise ValueError(f"line {number}: duplicate Job Name")
        nodes[name] = {"state": state}
    return nodes


def _replace_output(path: Path, text: str) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            delete=False,
            prefix=f".{path.name}.",
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", default="-", help="UTF-8 summary file; - is stdin")
    parser.add_argument("-o", "--output", default="-", help="snapshot file; - is stdout")
    args = parser.parse_args(argv)
    try:
        data = sys.stdin.buffer.read() if args.input == "-" else Path(args.input).read_bytes()
        text = json.dumps({"nodes": parse_summary(data)}, indent=2) + "\n"
        if args.output == "-":
            sys.stdout.write(text)
        else:
            _replace_output(Path(args.output), text)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"autorep_snapshot: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
