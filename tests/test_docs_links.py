"""Repository links in the documentation are relative and resolve (DL-239).

A link to a file in this repository is written as a relative path, so it works
in a clone, on GitHub and in a fork. README.md is also the package readme, and
the PyPI project page cannot resolve a relative path, so the build rewrites
each relative link to the file on main. The substitution lives in
pyproject.toml; the last test applies it to README.md as the build does.
"""

from __future__ import annotations

import html
import re
import tomllib
from pathlib import Path

from test_docs_hygiene import ROOT, documents

REPO_URL = "https://github.com/mrbald/dsl41/"
TARGET = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*$")
FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
CODE_SPAN = re.compile(r"`[^`\n]*`")
EXTERNAL = ("http://", "https://", "mailto:")
GLOSSARY = ROOT / "docs" / "glossary.md"
SECTION = re.compile(r"^(?=#{1,6}\s)", re.MULTILINE)


def prose(path: Path) -> str:
    """The document without fenced blocks and code spans, where `](` is text."""
    return CODE_SPAN.sub("", FENCE.sub("", path.read_text(encoding="utf-8")))


def targets(text: str) -> list[str]:
    """Every link and image target, in order."""
    return TARGET.findall(text)


def slug(heading: str) -> str:
    """A heading's id, as GitHub derives it for the headings these documents
    use: entities unescaped outside code spans, lower case, punctuation
    dropped, spaces to hyphens. A code span keeps its text literally,
    entities included; only the backticks go. Emphasis and links inside a
    heading are not handled."""
    parts = re.split(r"(`[^`\n]*`)", heading)
    text = "".join(part if part.startswith("`") else html.unescape(part) for part in parts)
    text = re.sub(r"[`*]", "", text.lower())
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    """The heading ids of a document. A `#` line in a fenced block is not a
    heading, so fences go first; code spans stay, because a heading's code
    span is part of its id. A repeated id gets GitHub's `-1`, `-2` suffix.
    Stated limit: a heading whose own id already ends in `-N` and collides
    with a suffixed one is not disambiguated as GitHub does; such a link is
    reported broken, so the check fails safe."""
    found: set[str] = set()
    seen: dict[str, int] = {}
    for line in FENCE.sub("", path.read_text(encoding="utf-8")).splitlines():
        if match := HEADING.match(line):
            base = slug(match.group(1))
            count = seen.get(base, 0)
            seen[base] = count + 1
            found.add(base if count == 0 else f"{base}-{count}")
    return found


def test_a_heading_code_span_keeps_its_text_in_the_id(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text(
        "## 4. `baseline_id` rotates per period\n\n"
        "### 2.3 `decision` — one atomic batch\n\n"
        "```\n# not a heading\n```\n",
        encoding="utf-8",
    )
    assert anchors(page) == {
        "4-baseline_id-rotates-per-period",
        "23-decision--one-atomic-batch",
    }
    assert slug("1. Identities") == "1-identities"


def test_a_heading_entity_is_unescaped_before_the_id(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text("### runs/.by_run_id/&lt;run_id&gt; — the index\n", encoding="utf-8")
    assert anchors(page) == {"runsby_run_idrun_id--the-index"}
    assert slug("the `&lt;run_id&gt;` index") == "the-ltrun_idgt-index"


def test_a_repeated_heading_gets_a_numbered_id(tmp_path: Path) -> None:
    page = tmp_path / "page.md"
    page.write_text("## Notes\n\n### Notes\n\n## Notes\n\n## Other\n", encoding="utf-8")
    assert anchors(page) == {"notes", "notes-1", "notes-2", "other"}


def test_repository_links_are_relative() -> None:
    """A link to a file in this repository never spells the GitHub URL."""
    offenders = [
        f"{path.relative_to(ROOT)}: {target}"
        for path in documents(ROOT)
        for target in targets(prose(path))
        if target.startswith((REPO_URL + "blob/", REPO_URL + "tree/"))
    ]
    assert offenders == []


def test_relative_links_resolve() -> None:
    """Every relative target is a file in the tree; a fragment names a heading."""
    broken: list[str] = []
    for path in documents(ROOT):
        for target in targets(prose(path)):
            if target.startswith(EXTERNAL):
                continue
            file, _, fragment = target.partition("#")
            destination = path if not file else path.parent / file
            if not destination.exists():
                broken.append(f"{path.relative_to(ROOT)}: {target}")
            elif fragment and destination.suffix == ".md" and fragment not in anchors(destination):
                broken.append(f"{path.relative_to(ROOT)}: {target}")
    assert broken == []


def test_package_readme_rewrites_every_relative_link() -> None:
    """The build's substitution leaves the package readme with no relative link."""
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "readme" in config["project"]["dynamic"]
    hook = config["tool"]["hatch"]["metadata"]["hooks"]["fancy-pypi-readme"]
    assert [fragment["path"] for fragment in hook["fragments"]] == ["README.md"]
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for substitution in hook["substitutions"]:
        text = re.sub(substitution["pattern"], substitution["replacement"], text)
    relative = [t for t in targets(text) if not t.startswith((*EXTERNAL, "#"))]
    assert relative == []
    assert REPO_URL + "blob/main/docs/decision-log.md)" in text
    assert REPO_URL + "blob/main/https://" not in text


def unlinked_entries(text: str) -> list[str]:
    """Each `###` entry whose body, up to the next heading, holds no link.
    A link inside a fenced block or a code span is text, not a link."""
    unlinked: list[str] = []
    for section in SECTION.split(FENCE.sub("", text)):
        heading, _, body = section.partition("\n")
        if heading.startswith("### ") and not targets(CODE_SPAN.sub("", body)):
            unlinked.append(heading.removeprefix("### ").strip())
    return unlinked


def test_glossary_every_term_has_a_link() -> None:
    """Every glossary entry carries at least one link in its body. Whether
    the link reaches the section that defines the term is checked in review,
    not here."""
    text = GLOSSARY.read_text(encoding="utf-8")
    assert len(re.findall(r"^### ", text, re.MULTILINE)) >= 25
    assert unlinked_entries(text) == []


def test_glossary_entry_without_a_link_is_reported() -> None:
    fixture = (
        "# Glossary\n\nIntro with no link.\n\n"
        "### linked\n\nA thing.\nSee [its section](page.md#a).\n\n"
        "### code only\n\nA thing named `[x](page.md)` in a code span.\n\n"
        "### fenced\n\n```\n[x](page.md)\n```\n\n"
        "## Part\n\n[a link under a level-2 heading](page.md)\n\n"
        "### last\n\nNo link at the end of the file.\n"
    )
    assert unlinked_entries(fixture) == ["code only", "fenced", "last"]
    assert unlinked_entries("### alone\n\nSee [it](page.md).\n") == []
