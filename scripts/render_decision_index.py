"""Write `docs/decision-index.md`, a table of every decision-log entry.

Run it after the log or a citing doc changes:

    uv run python scripts/render_decision_index.py

`tests/test_decision_index.py` fails when the committed file and the
rendering differ. A range such as `DL-263..DL-271` counts for its two named
ends only, as in `scripts/arch_check.py`, which does not expand ranges.

Limit: ids match literally. The log pads ids below 10 (`DL-07`), so a
citation written `DL-7` is not credited to `DL-07`. No doc does this today.
Only docs git tracks are scanned, so an untracked scratch file never puts
its name into this public index. Stage a new doc, then regenerate.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOG = ROOT / "docs" / "decision-log.md"
INDEX = ROOT / "docs" / "decision-index.md"

#: `- DL-41a Title` at column 0. The id is digits and an optional lowercase letter.
HEADER = re.compile(r"^- (DL-\d+[a-z]?)(?:\s+(.*))?$")
#: A whole id: not glued to a word character before or after.
CITATION = re.compile(r"(?<!\w)(DL-\d+[a-z]?)(?!\w)")

REGENERATE = "uv run python scripts/render_decision_index.py"


TITLE_LIMIT = 160
#: A date parenthetical starts the amendment note, not the title.
_DATE_PAREN = re.compile(r" \(20\d")
#: A dot ends a sentence unless an abbreviation owns it; `3.12` has no space after the dot.
_SENTENCE_END = re.compile(r"(?<!\be\.g)(?<!\bi\.e)(?<!\bcf)(?<!\bvs)\.(?:\s|$)")


def make_title(text: str) -> str:
    """The title from a header's text and its continuation lines, joined."""
    text = " ".join(text.split())
    cut = len(text)
    for pattern in (_DATE_PAREN, _SENTENCE_END):
        match = pattern.search(text)
        if match:
            cut = min(cut, match.start())
    title = text[:cut].strip()
    if len(title) > TITLE_LIMIT:
        title = title[: TITLE_LIMIT + 1].rsplit(" ", 1)[0].rstrip() + "..."
    return title


def parse_entries(log_text: str) -> list[tuple[str, str]]:
    """`(id, title)` for each entry header, in log order."""
    entries: list[tuple[str, str]] = []
    current: tuple[str, list[str]] | None = None
    for line in log_text.splitlines() + [""]:
        match = HEADER.match(line)
        if match or not line.strip():
            if current:
                entries.append((current[0], make_title(" ".join(current[1]))))
            current = (match.group(1), [match.group(2) or ""]) if match else None
        elif current and line[0] == " ":
            current[1].append(line)
        elif current:
            entries.append((current[0], make_title(" ".join(current[1]))))
            current = None
    return entries


def cited_ids(text: str) -> set[str]:
    """Every whole decision id that `text` names."""
    return set(CITATION.findall(text))


def _git_names(root: pathlib.Path) -> set[str] | None:
    """Docs git tracks in `root` (the index included); None when `root`
    is not itself a repository top level or git is absent."""

    def git(*args: str) -> str | None:
        try:
            out = subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout if out.returncode == 0 else None

    top = git("rev-parse", "--show-toplevel")
    if top is None or pathlib.Path(top.strip()).resolve() != root.resolve():
        return None
    listing = git(
        "ls-files",
        "-z",
        "--cached",
        "--",
        "README.md",
        "docs/*.md",
    )
    if listing is None:
        return None
    return {n for n in listing.split("\0") if n}


def citing_docs(root: pathlib.Path) -> list[pathlib.Path]:
    """README and docs that git tracks, sorted,
    without the log and the index.

    Walks the tree, with a note on stderr, when `root` is not a repository top
    level or git is absent."""
    names = _git_names(root)
    if names is None:
        print("decision index: not a git top level; scanning every doc", file=sys.stderr)
        names = {p.relative_to(root).as_posix() for p in (root / "docs").rglob("*.md")}
        names.add("README.md")
    skip = {"docs/decision-log.md", "docs/decision-index.md"}
    return sorted(root / n for n in names - skip if (root / n).is_file())


def citations(root: pathlib.Path) -> dict[str, list[str]]:
    """Decision id to the sorted repo-relative paths of the docs that cite it."""
    cited: dict[str, list[str]] = {}
    for path in citing_docs(root):
        rel = path.relative_to(root).as_posix()
        for dl_id in cited_ids(path.read_text(encoding="utf-8")):
            cited.setdefault(dl_id, []).append(rel)
    return cited


def _link(rel: str) -> str:
    """A markdown link, relative to `docs/` where the index lives."""
    target = rel[len("docs/") :] if rel.startswith("docs/") else f"../{rel}"
    return f"[{rel}]({target})"


def render(entries: list[tuple[str, str]], cited: dict[str, list[str]]) -> str:
    """The whole index file."""
    lines = [
        "# Decision index",
        "",
        f"This file is generated from the decision log; regenerate it with `{REGENERATE}`.",
        "",
        "| Entry | Title | Cited by |",
        "| --- | --- | --- |",
    ]
    for dl_id, title in entries:
        links = ", ".join(_link(rel) for rel in cited.get(dl_id, []))
        lines.append(f"| {dl_id} | {title.replace('|', chr(92) + '|')} | {links} |")
    return "\n".join(lines) + "\n"


def render_index(root: pathlib.Path = ROOT) -> str:
    """The index for the repository at `root`."""
    log = (root / "docs" / "decision-log.md").read_text(encoding="utf-8")
    return render(parse_entries(log), citations(root))


def is_current(root: pathlib.Path = ROOT) -> bool:
    """True when the committed index at `root` equals the rendering."""
    index = root / "docs" / "decision-index.md"
    return index.exists() and index.read_text(encoding="utf-8") == render_index(root)


def main() -> int:
    text = render_index()
    if is_current():
        print(f"{INDEX.name}: already current")
        return 0
    INDEX.write_text(text, encoding="utf-8")
    print(f"{INDEX.name}: written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
