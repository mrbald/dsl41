"""docs/architecture.md: every diagram box names something real.

Each Mermaid node, subgraph title and sequence participant in the
architecture overview is checked. A label is split into lines at `<br/>`.
At least one line must be a whole name: a `src/dsl41/` module file, a
comma-separated list of `dsl41` verbs and module files, or one entry of
the process and actor lists below. Every module and verb a label mentions
must exist. Edge and message text is free prose and is not checked.
"""

from __future__ import annotations

import re

import pytest
import typer

from dsl41.cli import app
from test_docs_hygiene import ROOT

DOC = ROOT / "docs" / "architecture.md"
RUNBOOK = ROOT / "docs" / "deployment-runbook.md"

#: Processes and stores, as deployment-runbook ss0's process diagram names them
#: (RUN_ROOT and ESTATE_ANCHOR read with `_` as a space). `fixed_names` reads them there.
PROCESSES = ("engine", "supervisor", "wrapper", "command", "run root", "estate anchor")
#: People, machines and input around dsl41, as deployment-runbook ss0 and ss4 name them.
#: `fixed_names` reads them there.
ACTORS = ("operator", "browser", "reverse proxy", "host", "estate")
DIAGRAM_MARKER = "<!-- diagram: processes -->"
#: The whole vocabulary, for the synthetic cases below.
FIXED = {*PROCESSES, *ACTORS}

MERMAID = re.compile(r"^```mermaid\n(.*?)^```$", re.M | re.S)
QUOTED = re.compile(r"\"([^\"]*)\"")
#: A flowchart link, with or without spaces around it.
LINK = re.compile(r"\s*(?:<?(?:-{2,}|={2,}|-\.+-)[->ox]?|~~~)\s*")
SHAPED = re.compile(r"^(\w+)\s*([\[\(\{>].*?)(?::::\w+)?$")
BARE = re.compile(r"^(\w+)(?::::\w+)?$")
SKIP = re.compile(
    r"^\s*(?:(?:flowchart|graph|classDef|class|style|linkStyle|direction|click|end)\b|%%)"
)
PARTICIPANT = re.compile(r"^\s*(?:participant|actor)\s+(\w+)(?:\s+as\s+(.+?))?\s*$")
MESSAGE = re.compile(r"^\s*(\w+)\s*(?:<<)?-{1,2}(?:>>|>|x|\))[+-]?\s*(\w+)\s*:")
NOTE = re.compile(r"^\s*[Nn]ote\s+(?:over|left of|right of)\s+([\w\s,]+?)\s*:")
MODULE_ITEM = re.compile(r"^(\w+)\.py$")
VERB_ITEM = re.compile(r"^dsl41 ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?(?: --[a-z-]+)*$")
MODULE = re.compile(r"\b(\w+)\.py\b")
VERB = re.compile(r"\bdsl41 ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?")


def _modules() -> set[str]:
    return {path.stem for path in (ROOT / "src" / "dsl41").glob("*.py")}


def _verbs() -> dict[str, set[str] | None]:
    """Each top-level verb, mapped to its sub-verbs when it is a group."""
    root = typer.main.get_command(app)
    verbs: dict[str, set[str] | None] = {}
    for name, command in root.commands.items():  # type: ignore[attr-defined]
        sub = getattr(command, "commands", None)
        verbs[name] = set(sub) if sub is not None else None
    return verbs


def _flowchart(block: str) -> list[str]:
    found: list[str] = []
    labelled: set[str] = set()
    bare: list[str] = []
    for line in block.splitlines():
        if not line.strip() or SKIP.match(line):
            continue
        texts: list[str] = []

        def hold(match: re.Match[str]) -> str:
            texts.append(match.group(1))
            return f"Q{len(texts) - 1}Q"

        line = QUOTED.sub(hold, line)
        line = re.sub(r"\|[^|]*\|", " ", line)  # edge labels are prose
        sub = re.match(r"^\s*subgraph\s+(.*)$", line)
        parts = [sub.group(1)] if sub else LINK.split(line)
        for part in (p.strip() for frag in parts for p in frag.split("&")):
            if not part:
                continue
            if shaped := SHAPED.match(part):
                ident, body = shaped.groups()
                held = re.fullmatch(r"[\[\(\{>/\\]*Q(\d+)Q[\]\)\}/\\]*", body)
                if held:
                    found.append(texts[int(held.group(1))])
                    labelled.add(ident)
                else:
                    found.append(f"UNQUOTED:{ident}")
            elif plain := BARE.match(part):
                bare.append(plain.group(1))
            else:
                found.append(f"UNQUOTED:{part}")
    found += [f"UNQUOTED:{ident}" for ident in bare if ident not in labelled]
    return found


def _sequence(block: str) -> list[str]:
    found: list[str] = []
    declared: set[str] = set()
    for line in block.splitlines():
        if declared_match := PARTICIPANT.match(line):
            ident, alias = declared_match.groups()
            declared.add(ident)
            found.append(alias or ident)
            continue
        ends: list[str] = []
        if message := MESSAGE.match(line):
            ends = list(message.groups())
        elif note := NOTE.match(line):
            ends = [end.strip() for end in note.group(1).split(",")]
        found += [f"UNDECLARED:{end}" for end in ends if end not in declared]
    return found


def labels(markdown: str) -> list[str]:
    """Every node label, subgraph title and participant in the Mermaid
    blocks of `markdown`. A node with no quoted label comes back as
    `UNQUOTED:<id>`, and a sequence endpoint never declared as
    `UNDECLARED:<id>`, so that the check reports them rather than skip them."""
    found: list[str] = []
    for block in MERMAID.findall(markdown):
        if block.lstrip().startswith("sequenceDiagram"):
            found += _sequence(block)
        else:
            found += _flowchart(block)
    return found


def _has_word(word: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text, re.I) is not None


def _section(text: str, heading: str) -> str:
    text = "\n" + text
    start = text.find(f"\n## {heading}")
    assert start >= 0, f"deployment-runbook.md has no section '## {heading}'"
    stop = text.find("\n## ", start + 1)
    return text[start : stop if stop >= 0 else len(text)]


def fixed_names(runbook: str) -> set[str]:
    """The entries of PROCESSES and ACTORS that the runbook really uses:
    processes in its ss0 process diagram, actors in its ss0 and ss4."""
    assert DIAGRAM_MARKER in runbook, (
        f"deployment-runbook.md lost its {DIAGRAM_MARKER!r} marker; the process words"
        " in tests/test_architecture_doc.py are read from the diagram under it"
    )
    after = runbook.split(DIAGRAM_MARKER, 1)[1]
    fenced = re.match(r"\s*```\w*\n(.*?)^```$", after, re.M | re.S)
    assert fenced is not None, f"no fenced block follows {DIAGRAM_MARKER!r}"
    diagram = fenced.group(1).replace("_", " ")
    sections = _section(runbook, "0. ") + _section(runbook, "4. ")
    return {word for word in PROCESSES if _has_word(word, diagram)} | {
        word for word in ACTORS if _has_word(word, sections)
    }


def _names_line(
    line: str, modules: set[str], verbs: dict[str, set[str] | None], fixed: set[str]
) -> bool:
    if line.lower() in fixed:
        return True
    for item in (item.strip() for item in line.split(",")):
        module = MODULE_ITEM.match(item)
        verb = VERB_ITEM.match(item)
        if not (module and module.group(1) in modules) and not (verb and verb.group(1) in verbs):
            return False
    return True


def problems(
    found: list[str], modules: set[str], verbs: dict[str, set[str] | None], fixed: set[str]
) -> list[str]:
    """The labels that name nothing real, or name a module or verb that does
    not exist. `fixed` is the process and actor vocabulary, `fixed_names`'s."""
    bad: list[str] = []
    for label in found:
        kind, _, ident = label.partition(":")
        if kind == "UNQUOTED" and ident:
            bad.append(f"node {ident!r} has no quoted label")
            continue
        if kind == "UNDECLARED" and ident:
            bad.append(f"participant {ident!r} is never declared")
            continue
        for name in MODULE.findall(label):
            if name not in modules:
                bad.append(f"{label!r}: no module src/dsl41/{name}.py")
        for verb, sub in VERB.findall(label):
            subs = verbs.get(verb, set())
            if verb not in verbs:
                bad.append(f"{label!r}: no CLI verb 'dsl41 {verb}'")
            elif subs is not None and sub not in subs:
                bad.append(f"{label!r}: no CLI verb 'dsl41 {verb} {sub}'")
        lines = (line.strip() for line in label.split("<br/>"))
        if not any(_names_line(line, modules, verbs, fixed) for line in lines):
            bad.append(f"{label!r} names no module, CLI verb, process or actor as a whole")
    return bad


def test_architecture_diagrams_name_real_modules_verbs_and_processes() -> None:
    text = DOC.read_text(encoding="utf-8")
    found = labels(text)
    assert len(MERMAID.findall(text)) >= 4
    assert "wrapper<br/>runner_wrapper.py" in found
    fixed = fixed_names(RUNBOOK.read_text(encoding="utf-8"))
    assert problems(found, _modules(), _verbs(), fixed) == []


def test_architecture_process_and_actor_words_come_from_the_runbook() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")
    assert sorted({*PROCESSES, *ACTORS} - fixed_names(runbook)) == []


def test_architecture_vocabulary_without_its_runbook_source_is_reported() -> None:
    with pytest.raises(AssertionError, match="lost its '<!-- diagram: processes -->' marker"):
        fixed_names("## 0. The operator path\n## 4. UI surfaces\n")
    runbook = (
        "## 0. The operator path\nThe operator runs an estate on a host.\n"
        f"{DIAGRAM_MARKER}\n```text\nthe engine and its wrapper\n```\n"
        "## 4. UI surfaces\nNothing here.\n## 5. Routine\nA browser behind a reverse proxy.\n"
    )
    fixed = fixed_names(runbook)
    assert fixed == {"operator", "estate", "host", "engine", "wrapper"}
    assert problems(["browser", "run root", "engine"], set(), {}, fixed) == [
        "'browser' names no module, CLI verb, process or actor as a whole",
        "'run root' names no module, CLI verb, process or actor as a whole",
    ]


def test_architecture_label_check_reports_unknown_names() -> None:
    doc = (
        "```mermaid\nflowchart LR\n"
        '  a["frobnicator<br/>frob.py"] -->|"engine"| b["vague thing"]\n'
        "  c[bare]\n"
        '  subgraph s["dsl41 frobnicate"]\n  end\n'
        '  d["prune<br/>dsl41 estate vacuum"]\n'
        '  e["engine"]-->f["no space before me"]\n'
        '  e -->|"x"|g["no space after the edge label"]\n'
        "  subgraph loose title\n  end\n"
        "  e --> zzz\n"
        '  h["quantum command bus"]\n'
        '  classy["bogus one"] --> e\n'
        '  graphite["bogus two"]\n'
        '  clicker["bogus three"]\n'
        "```\n"
        "```mermaid\nsequenceDiagram\n"
        "  participant X as mystery box\n"
        "  participant Y\n"
        "  X->>Ghost: hi\n"
        "  Note over X,Phantom: hello\n"
        "```\n"
    )
    verbs = {"run": None, "estate": {"prune"}}
    assert problems(labels(doc), {"runner"}, verbs, FIXED) == [
        "'frobnicator<br/>frob.py': no module src/dsl41/frob.py",
        "'frobnicator<br/>frob.py' names no module, CLI verb, process or actor as a whole",
        "'vague thing' names no module, CLI verb, process or actor as a whole",
        "node 'c' has no quoted label",
        "'dsl41 frobnicate': no CLI verb 'dsl41 frobnicate'",
        "'dsl41 frobnicate' names no module, CLI verb, process or actor as a whole",
        "'prune<br/>dsl41 estate vacuum': no CLI verb 'dsl41 estate vacuum'",
        "'no space before me' names no module, CLI verb, process or actor as a whole",
        "'no space after the edge label' names no module, CLI verb, process or actor as a whole",
        "node 'loose title' has no quoted label",
        "'quantum command bus' names no module, CLI verb, process or actor as a whole",
        "'bogus one' names no module, CLI verb, process or actor as a whole",
        "'bogus two' names no module, CLI verb, process or actor as a whole",
        "'bogus three' names no module, CLI verb, process or actor as a whole",
        "node 'zzz' has no quoted label",
        "'mystery box' names no module, CLI verb, process or actor as a whole",
        "'Y' names no module, CLI verb, process or actor as a whole",
        "participant 'Ghost' is never declared",
        "participant 'Phantom' is never declared",
    ]


def test_architecture_label_check_accepts_real_names() -> None:
    doc = (
        "```mermaid\nflowchart TB\n"
        '  a["engine loop<br/>runner.py"] -->|"anything at all"| b[("run root")]\n'
        '  subgraph h["host"]\n'
        '    c["clients<br/>dsl41 run --ui, dsl41 estate prune"]\n'
        "  end\n"
        "  a-->c & b\n"
        "  classDef specified stroke-dasharray: 5 5\n"
        "  class c specified\n"
        "```\n"
        "```mermaid\nsequenceDiagram\n"
        "  actor Op as operator<br/>dsl41 run\n"
        "  participant Wr as wrapper\n"
        "  Op->>Wr: start\n"
        "  Wr-->>Op: done\n"
        "  Note over Op,Wr: free text\n"
        "```\n"
    )
    verbs = {"run": None, "estate": {"prune"}}
    found = labels(doc)
    assert found == [
        "engine loop<br/>runner.py",
        "run root",
        "host",
        "clients<br/>dsl41 run --ui, dsl41 estate prune",
        "operator<br/>dsl41 run",
        "wrapper",
    ]
    assert problems(found, {"runner"}, verbs, FIXED) == []
