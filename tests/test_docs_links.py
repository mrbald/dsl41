"""Repository links in the documentation are relative and resolve (DL-239).

A link to a file in this repository is written as a relative path, so it works
in a clone, on GitHub and in a fork. README.md is also the package readme, and
the PyPI project page cannot resolve a relative path, so the build rewrites
each relative link to the file on main. The substitution lives in
pyproject.toml; the last test applies it to README.md as the build does.
"""

from __future__ import annotations

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


def prose(path: Path) -> str:
    """The document without fenced blocks and code spans, where `](` is text."""
    return CODE_SPAN.sub("", FENCE.sub("", path.read_text(encoding="utf-8")))


def targets(text: str) -> list[str]:
    """Every link and image target, in order."""
    return TARGET.findall(text)


def anchors(path: Path) -> set[str]:
    """GitHub's heading ids: lower case, punctuation dropped, spaces to hyphens."""
    found: set[str] = set()
    for line in prose(path).splitlines():
        if match := HEADING.match(line):
            text = re.sub(r"[`*]", "", match.group(1).lower())
            found.add(re.sub(r"[^\w\- ]", "", text).replace(" ", "-"))
    return found


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
