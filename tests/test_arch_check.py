"""The deterministic architecture gate, scripts/arch_check.py (DL-75).

Every check ships a case that trips it and one that does not (the CLAUDE.md
convention). The cases are tiny trees under tmp_path, not the real repo: the
gate's job is to fail when the tree gets worse, so tests that assert on the
real tree would go red every time the real code legitimately changes. The two
exceptions are deliberate -- the IR-F schema pin and the citation index are
pinned artifacts, and a test that they still match the tree is the point.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "arch_check.py"


def _load() -> ModuleType:
    """scripts/ is not a package (the gate is a standalone stdlib script that
    CI runs by path), so the test loads it by path too."""
    spec = importlib.util.spec_from_file_location("arch_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["arch_check"] = module
    spec.loader.exec_module(module)
    return module


arch_check = _load()


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------ 1. duplicate bodies

_BODY = """
    total = 0
    seen = set()
    for item in items:
        seen.add(item)
        total += item
    return total, seen
"""


def test_identical_body_in_two_modules_blocks(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.py", f"def sum_a(items):{_BODY}")
    b = _write(tmp_path / "b.py", f"def sum_b(items):{_BODY}")
    findings = arch_check.duplicate_function_bodies([a, b])
    assert len(findings) == 1
    assert "identical body in another module" in findings[0].message
    assert "sum_b()" in findings[0].message


def test_identical_body_twice_in_one_module_does_not_block(tmp_path: Path) -> None:
    # one module's own repetition is that module's business; the drift class
    # DL-72 removed was a copy that two modules maintained separately
    a = _write(tmp_path / "a.py", f"def sum_a(items):{_BODY}\n\ndef sum_b(items):{_BODY}")
    assert arch_check.duplicate_function_bodies([a]) == []


def test_trivial_and_dunder_bodies_are_exempt(tmp_path: Path) -> None:
    trivial = "\n    self.x = 1\n    return self.x\n"
    a = _write(tmp_path / "a.py", f"class A:\n  def get(self):{trivial}")
    b = _write(tmp_path / "b.py", f"class B:\n  def get(self):{trivial}")
    assert arch_check.duplicate_function_bodies([a, b]) == []
    long_dunder = f"class A:\n  def __eq__(self, other):{_BODY}"
    c = _write(tmp_path / "c.py", long_dunder)
    d = _write(tmp_path / "d.py", long_dunder.replace("class A", "class B"))
    assert arch_check.duplicate_function_bodies([c, d]) == []


# --------------------------------------------- 2. private cross-module imports


def test_new_private_cross_module_import_blocks(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.py", "from dsl41.oracle import _TERMINAL\n")
    findings = arch_check.private_cross_module_imports([a], allowed=[])
    assert len(findings) == 1
    assert "imports the private name _TERMINAL from dsl41.oracle" in findings[0].message


def test_baselined_private_import_and_public_import_do_not_block(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.py", "from dsl41.oracle import _TERMINAL\nfrom dsl41.ir import Job\n")
    allowed = arch_check.private_import_keys([a])
    assert allowed == [f"{arch_check._rel(a)}:dsl41.oracle._TERMINAL"]
    assert arch_check.private_cross_module_imports([a], allowed=allowed) == []


def test_dunder_and_foreign_package_private_imports_are_not_the_rule(tmp_path: Path) -> None:
    # the rule is about coupling two dsl41 modules; __all__-shaped names and
    # third-party privates are somebody else's contract
    a = _write(tmp_path / "a.py", "from dsl41.ir import __all__\nfrom lark import _lib\n")
    assert arch_check.private_cross_module_imports([a], allowed=[]) == []


# ---------------------------------------------------------------- 3. citations

_INDEX_PATTERNS = arch_check.index_patterns(arch_check.INDEX_PATH.read_text(encoding="utf-8"))


def test_the_real_index_yields_its_namespace_rows() -> None:
    assert len(_INDEX_PATTERNS) >= 15  # one per namespace row, machine-read
    assert any(pattern.fullmatch("SEM-12") for pattern in _INDEX_PATTERNS)
    assert any(pattern.fullmatch("DL-75") for pattern in _INDEX_PATTERNS)
    assert any(pattern.fullmatch("Qr2") for pattern in _INDEX_PATTERNS)


def test_token_with_no_index_row_blocks(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.py", "# ZZZ-42: a namespace nobody documented\nx = 1\n")
    findings = arch_check.unresolved_citations([a], _INDEX_PATTERNS)
    assert len(findings) == 1
    assert "'ZZZ-42' resolves to no namespace" in findings[0].message


def test_retired_conversation_pointer_shapes_block(tmp_path: Path) -> None:
    # the two shapes DL-75 deleted: a citation whose target is a conversation
    a = _write(tmp_path / "a.py", '"""Owner-only per sol #3."""\n')
    assert [f.message for f in arch_check.unresolved_citations([a], _INDEX_PATTERNS)]


def test_indexed_tokens_and_ordinary_prose_do_not_block(tmp_path: Path) -> None:
    a = _write(
        tmp_path / "a.py",
        '"""SEM-12 and DL-75 and M07 and L015 and ss6a and Q8b and E11 and [F]."""\n'
        "# Tier-0 wrapper, phase 11f, UTF-8, a 3-line note  # noqa: E501\n"
        'MODE = "W1"  # the quoted literal names a value, not a source\n',
    )
    assert arch_check.unresolved_citations([a], _INDEX_PATTERNS) == []


# ------------------------------------------------------------- 4. IR schema pin


def test_pinned_ir_schema_still_matches_the_tree() -> None:
    assert arch_check.ir_schema_findings(arch_check.IR_SCHEMA_PIN) == []


def test_schema_change_without_a_version_bump_blocks() -> None:
    from dsl41.ir import IR_VERSION

    findings = arch_check.ir_schema_findings({"ir_version": IR_VERSION, "sha256": "0" * 64})
    assert len(findings) == 1
    assert "bump IR_VERSION" in findings[0].message


def test_version_bump_licenses_a_new_hash(capsys: pytest.CaptureFixture[str]) -> None:
    findings = arch_check.ir_schema_findings({"ir_version": "0.0", "sha256": "0" * 64})
    assert findings == []
    assert "re-pin" in capsys.readouterr().out


# --------------------------------------------------------------- 5. size advisory


def _oversized(tmp_path: Path) -> Path:
    long_body = "\n".join(f"    x = {i}" for i in range(150))
    branchy_body = "\n".join(f"    if x == {i}:\n        x += 1" for i in range(45))
    filler = "\n".join(f"FILLER_{i} = {i}" for i in range(1250))
    return _write(
        tmp_path / "big.py",
        f"def long_one(x):\n{long_body}\n\n\ndef branchy(x):\n{branchy_body}\n"
        f"    return x\n\n\n{filler}\n",
    )


def test_new_sizes_are_advisory_and_named(tmp_path: Path) -> None:
    measured = arch_check.measure_sizes([_oversized(tmp_path)])
    assert len(measured["long_modules"]) == 1
    assert list(measured["long_functions"]) == [f"{arch_check._rel(_oversized(tmp_path))}:long_one"]
    assert list(measured["branchy_functions"]) == [
        f"{arch_check._rel(_oversized(tmp_path))}:branchy"
    ]
    notes = arch_check.size_advisories(measured, {})
    assert len(notes) == 3
    assert all(note.endswith("new)") for note in notes)


def test_sizes_at_or_under_the_baseline_are_silent(tmp_path: Path) -> None:
    measured = arch_check.measure_sizes([_oversized(tmp_path)])
    assert arch_check.size_advisories(measured, dict(measured)) == []  # a ratchet, not a bill
    worsened = {kind: {k: v - 1 for k, v in sizes.items()} for kind, sizes in measured.items()}
    notes = arch_check.size_advisories(measured, worsened)
    assert len(notes) == 3
    assert all("was " in note for note in notes)


def test_a_module_under_every_threshold_measures_nothing(tmp_path: Path) -> None:
    small = _write(tmp_path / "small.py", "def f(x):\n    if x:\n        return 1\n    return 0\n")
    assert arch_check.measure_sizes([small]) == {
        "long_modules": {},
        "long_functions": {},
        "branchy_functions": {},
    }


# ------------------------------------------------------- main(): exit + escalation


def _tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    drift: int,
    test_source: str = "X = 1\n",
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    _write(src / "mod.py", source)
    tests = tmp_path / "tests"
    tests.mkdir()
    _write(tests / "test_mod.py", test_source)
    monkeypatch.setattr(arch_check, "SRC", src)
    monkeypatch.setattr(arch_check, "BASELINE_PATH", tmp_path / "missing-baseline.json")
    monkeypatch.setattr(arch_check, "changed_lines_since_review", lambda: (drift, "arch-review/x"))


def test_main_exits_0_and_says_nothing_when_the_tree_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tree(tmp_path, monkeypatch, "# SEM-12: a citation that resolves\nX = 1\n", drift=10)
    assert arch_check.main([]) == 0
    out = capsys.readouterr().out
    assert "arch_check: clean" in out
    assert "architecture review due" not in out


def test_main_exits_1_and_escalates_on_a_blocking_finding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tree(tmp_path, monkeypatch, "from dsl41.oracle import _TERMINAL\n", drift=10)
    assert arch_check.main([]) == 1
    out = capsys.readouterr().out
    assert "BLOCK" in out
    assert "architecture review due -- run /arch-review (1 blocking finding(s))" in out


def test_main_does_not_apply_the_private_import_rule_to_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # DL-75: the rule scans src/ only. A white-box test reaching into the
    # module it tests is this project's style (over half the pre-gate sites
    # were tests), so the same import that blocks in src/ is clean here --
    # otherwise an ordinary new test reds CI and --update-baseline, the only
    # remedy, re-blesses every src/ site added in the same commit.
    _tree(
        tmp_path,
        monkeypatch,
        "X = 1\n",
        drift=10,
        test_source="from dsl41.viz import _auto_direction\n",
    )
    assert arch_check.main([]) == 0
    assert "arch_check: clean" in capsys.readouterr().out


def test_main_escalates_on_accumulated_diff_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # signal, not calendar: a clean tree still earns a review once enough of
    # it has moved since the last arch-review/<date> tag
    _tree(tmp_path, monkeypatch, "X = 1\n", drift=arch_check.REVIEW_DIFF_LINES + 1)
    assert arch_check.main([]) == 0
    out = capsys.readouterr().out
    assert "arch_check: clean" in out
    assert f"{arch_check.REVIEW_DIFF_LINES + 1} lines changed since arch-review/x" in out


# ------------------------------------------------------ 5. state-owner bypasses (DL-82)

_OWNER_MODULE = """
class JobRuntime:
    status = "INACTIVE"
    armed = False


class StatusStore:
    def update(self, job, **fields):
        rt = self.job[job]
        rt.status = fields["status"]   # the owner itself is exempt
        rt.armed = fields["armed"]
        self.job[job] = rt


class Oracle:
    def bad_field(self, job):
        rt = self.store.runtime(job)
        rt.armed = True

    def bad_bump(self, job):
        rt = self.store.runtime(job)
        rt.run_number += 1

    def good(self, job):
        self.store.update(job, armed=True)
"""


def test_direct_state_assignment_outside_the_owner_blocks(tmp_path: Path) -> None:
    """DL-82: the concurrency model needs one observable write path per
    entity. A property test over feeds cannot enforce it -- one feed mutates
    several fields, so a missed site hides behind a sibling's write -- which
    is why this guard is structural and blocking."""
    mod = _write(tmp_path / "oracle.py", _OWNER_MODULE)
    findings = arch_check.state_owner_bypasses([mod])
    messages = [f.message for f in findings]
    assert len(findings) == 2, messages
    assert any("JobRuntime.armed" in m for m in messages)
    assert any("JobRuntime.run_number" in m for m in messages)  # AugAssign counts
    assert all("StatusStore (DL-82)" in m for m in messages)


def test_owner_body_and_unrelated_modules_do_not_block(tmp_path: Path) -> None:
    """Two exemptions that must hold or the gate is unusable: the owner class
    writes the fields by definition, and a module that does not define
    JobRuntime may legitimately own an attribute of the same name (the
    supervisor's _Run.run_number is the real instance)."""
    owner_only = _write(
        tmp_path / "oracle.py",
        "class JobRuntime:\n    armed = False\n\n\n"
        "class StatusStore:\n    def update(self, job):\n        self.job[job].armed = True\n",
    )
    assert arch_check.state_owner_bypasses([owner_only]) == []

    elsewhere = _write(
        tmp_path / "runner_supervisor.py",
        "class _Run:\n    def __init__(self, run_number):\n        self.run_number = run_number\n",
    )
    assert arch_check.state_owner_bypasses([elsewhere]) == []


def test_reaching_through_the_store_containers_blocks(tmp_path: Path) -> None:
    """The escape hatch a field-name check alone would miss: another module
    writing whole rows through store.job / store.globals_. The gate that found
    the real one was this clause -- the SEM-24 seeding path in Oracle.__init__
    assigned a whole JobRuntime and no field-name grep saw it."""
    mod = _write(
        tmp_path / "runner.py",
        "def poke(engine, job):\n"
        "    engine.oracle.store.job[job] = 1\n"
        "    engine.oracle.store.globals_['X'] = 'y'\n",
    )
    findings = arch_check.state_owner_bypasses([mod])
    assert len(findings) == 2
    assert {f.message.split(":")[0] for f in findings} == {
        "writes store.job directly",
        "writes store.globals_ directly",
    }


def test_setattr_outside_the_owner_blocks(tmp_path: Path) -> None:
    """The hole an assignment walk cannot see: setattr()'s attribute name is a
    runtime value, so no field-name check applies to it. Legitimate only
    inside the owner -- and once JobRuntime is frozen, not even there."""
    mod = _write(
        tmp_path / "oracle.py",
        "class JobRuntime:\n    armed = False\n\n\n"
        "class StatusStore:\n"
        "    def update(self, job, **fields):\n"
        "        for k, v in fields.items():\n"
        "            setattr(self.job[job], k, v)\n\n\n"
        "class Oracle:\n"
        "    def sneak(self, rt, name, value):\n"
        "        setattr(rt, name, value)\n",
    )
    findings = arch_check.state_owner_bypasses([mod])
    assert len(findings) == 1
    assert "setattr() outside StatusStore" in findings[0].message
