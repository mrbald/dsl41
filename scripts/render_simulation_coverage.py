"""Rewrite the generated block of `docs/simulation-coverage.md` in place.

Run it after changing `src/dsl41/simulation_register_rows.py`:

    uv run python scripts/render_simulation_coverage.py

Everything outside the `register:begin` / `register:end` markers is
hand-written and left alone. `tests/test_simulation_register.py` fails when
the block and `render_markdown()` differ, so this script is the only way to
update the table.
"""

from __future__ import annotations

import pathlib
import sys

from dsl41.simulation_register import render_markdown

BEGIN = "<!-- register:begin -->"
END = "<!-- register:end -->"
DOC = pathlib.Path(__file__).resolve().parents[1] / "docs" / "simulation-coverage.md"


def rewrite(text: str, block: str) -> str:
    """`text` with the marked block replaced by `block`."""
    start = text.find(BEGIN)
    end = text.find(END)
    if start < 0 or end < start:
        raise SystemExit(f"{DOC}: missing {BEGIN} / {END} markers")
    return text[: start + len(BEGIN)] + "\n\n" + block + "\n" + text[end:]


def main() -> int:
    current = DOC.read_text(encoding="utf-8")
    updated = rewrite(current, render_markdown())
    if updated == current:
        print(f"{DOC.name}: already current")
        return 0
    DOC.write_text(updated, encoding="utf-8")
    print(f"{DOC.name}: register block rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
