"""Estate templating preprocessor: `~{$NAME}~` placeholder resolution (DL-19).

Non-core helper. Estate JIL is templated with `~{$NAME}~` tokens that an external properties
mechanism substitutes before `jil` sees the text. This module reproduces that step so the
compiler core never models templating: the scanner and lowering treat unresolved tokens as
opaque name characters (DL-18 fixtures), and resolved estates flow through the ordinary
pipeline. Nothing in the core imports this module.

Format rules, each pinned by a test:
- Token: `~{$NAME}~`, NAME matching `[A-Za-z_][A-Za-z0-9_]*`. `$$VAR` (SEM-08 globals) and
  `${VAR}` (machine-side runtime vars) never match.
- Properties lines: blank lines and `#` or `!` lines are comments; otherwise `KEY=VALUE` split
  on the first `=`, key and value whitespace-stripped. A line with no `=` is an error.
- References are legal in both key and value: `HOST_~{$ENVID}~=~{$ENVID}~.example.com` defines
  HOST_DEV1 once ENVID resolves. Resolution is an order-independent fixpoint over all entries;
  nested tokens (`~{$HOST_~{$ENVID}~}~`) resolve inner-out across passes.
- Layering: later properties files override earlier ones by raw key (base plus environment
  overlay). A duplicate raw key within one file is an error. Two entries whose keys resolve to
  the same name collide, which is an error. A resolved key must be identifier-shaped.
- No silent loss: entries still carrying references at the fixpoint (an undefined name or a
  cycle) fail with every stuck entry listed. Substitution leaves no `~{...}~`-shaped residue:
  an undefined name or a malformed lookalike (`~{ENVID}~`, `~{$}~`) is an error with file:line
  unless `permit_unresolved` carries it verbatim and reports it (the DL-07 escape-hatch
  convention).
- Passes are bounded; non-convergence (values re-forming tokens) is an error.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path

from pydantic import BaseModel

from dsl41.ast_jil import render_location

PLACEHOLDER_RE = re.compile(r"~\{\$([A-Za-z_][A-Za-z0-9_]*)\}~")
#: Anything still `~{...}~`-shaped after substitution: undefined names and
#: malformed lookalikes both refuse to pass silently.
_RESIDUE_RE = re.compile(r"~\{[^{}\n]*\}~")
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_PASSES = 16  # nesting depth bound; beyond this it is a value re-forming tokens


class PlaceholderError(ValueError):
    """Loud resolution failure; the message carries every finding."""

    def __init__(self, findings: list[str]) -> None:
        super().__init__(
            f"{len(findings)} placeholder finding(s):\n" + "\n".join(f"  - {f}" for f in findings)
        )
        self.findings = findings


class _Entry(BaseModel):
    key: str  # raw, may carry references
    value: str  # raw, may carry references
    file: str
    line: int

    def where(self) -> str:
        return render_location(self.file, self.line)


def _substitute_known(text: str, bindings: Mapping[str, str]) -> str:
    """One pass: replace every token whose name is bound; leave the rest."""
    return PLACEHOLDER_RE.sub(
        lambda m: bindings.get(m.group(1), m.group(0)),
        text,
    )


def _parse_properties(path: Path) -> list[_Entry]:
    entries: list[_Entry] = []
    seen: dict[str, int] = {}
    findings: list[str] = []
    text = path.read_bytes().decode("utf-8")
    for lineno, raw_line in enumerate(text.split("\n"), start=1):
        line = raw_line.rstrip("\r").strip()
        if not line or line.startswith(("#", "!")):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            findings.append(f"{path}:{lineno}: no '=' in property line {line!r}")
            continue
        key = key.strip()
        if not key:
            findings.append(f"{path}:{lineno}: empty property key")
            continue
        if key in seen:
            findings.append(
                f"{path}:{lineno}: duplicate key {key!r} within one file"
                f" (first defined at line {seen[key]}; within-file duplicates are"
                " never intentional -- use a later overlay file to override)"
            )
            continue
        seen[key] = lineno
        entries.append(_Entry(key=key, value=value.strip(), file=str(path), line=lineno))
    if findings:
        raise PlaceholderError(findings)
    return entries


def load_properties(paths: Iterable[str | Path]) -> dict[str, str]:
    """Parse, layer and resolve properties files into a placeholder-free map.

    Later files override earlier ones by raw key. Raises PlaceholderError listing every parse
    error, stuck entry (cycle or undefined reference), key collision and non-identifier key.
    """
    layered: dict[str, _Entry] = {}
    for path in paths:
        for entry in _parse_properties(Path(path)):
            layered[entry.key] = entry  # later file wins (raw-key layering)

    resolved: dict[str, str] = {}
    provenance: dict[str, _Entry] = {}
    pending = list(layered.values())
    findings: list[str] = []
    while pending:
        progressed = False
        still: list[_Entry] = []
        for entry in pending:
            key = _substitute_known(entry.key, resolved)
            value = _substitute_known(entry.value, resolved)
            if PLACEHOLDER_RE.search(key) or PLACEHOLDER_RE.search(value):
                still.append(_Entry(key=key, value=value, file=entry.file, line=entry.line))
                continue
            if not _NAME_RE.fullmatch(key):
                findings.append(
                    f"{entry.where()}: resolved key {key!r} is not identifier-shaped;"
                    " nothing could reference it"
                )
                progressed = True
                continue
            if key in resolved:
                findings.append(
                    f"{entry.where()}: key resolves to {key!r}, already defined at"
                    f" {provenance[key].where()} (collision, not layering)"
                )
                progressed = True
                continue
            resolved[key] = value
            provenance[key] = entry
            progressed = True
        if not progressed:
            for entry in still:
                names = sorted(
                    set(PLACEHOLDER_RE.findall(entry.key) + PLACEHOLDER_RE.findall(entry.value))
                )
                findings.append(
                    f"{entry.where()}: unresolvable references {names}"
                    " (undefined name or reference cycle)"
                )
            break
        pending = still
    if findings:
        raise PlaceholderError(sorted(findings))
    return resolved


def substitute(
    text: str,
    bindings: Mapping[str, str],
    *,
    file: str = "<memory>",
    permit_unresolved: bool = False,
) -> tuple[str, list[str]]:
    """Replace every `~{$NAME}~` in `text`; returns (resolved text, reports).

    Nested tokens resolve inner-out. A `~{...}~`-shaped span left at the fixpoint (undefined
    name or malformed lookalike) raises PlaceholderError with its line, or with
    `permit_unresolved` stays verbatim and is reported. All other bytes are preserved.
    """
    for _ in range(_MAX_PASSES):
        replaced = _substitute_known(text, bindings)
        if replaced == text:
            break
        text = replaced
    else:
        raise PlaceholderError(
            [
                f"{file}: substitution did not converge in {_MAX_PASSES} passes (a bound"
                " value re-forms placeholder tokens)"
            ]
        )
    reports = [
        f"{file}:{lineno}: unresolved placeholder-like token {token!r}"
        for lineno, line in enumerate(text.split("\n"), start=1)
        for token in _RESIDUE_RE.findall(line)
    ]
    if reports and not permit_unresolved:
        raise PlaceholderError(reports)
    return text, reports


def resolve_text(
    text: str,
    properties: Iterable[str | Path],
    *,
    file: str = "<memory>",
    permit_unresolved: bool = False,
) -> tuple[str, list[str]]:
    """load_properties then substitute, in one call."""
    return substitute(
        text, load_properties(properties), file=file, permit_unresolved=permit_unresolved
    )
