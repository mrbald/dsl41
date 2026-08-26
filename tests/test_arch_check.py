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

_OTHER_BODY = """
    mapping = {}
    scale = 1
    for item in items:
        mapping[item] = item
    return mapping, scale
"""


def test_identical_body_in_two_modules_blocks(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.py", f"def sum_a(items):{_BODY}")
    b = _write(tmp_path / "b.py", f"def sum_b(items):{_BODY}")
    findings = arch_check.duplicate_function_bodies([a, b])
    assert len(findings) == 1
    assert "identical body in another module" in findings[0].message
    assert "sum_b()" in findings[0].message


def test_identical_body_twice_in_one_module_blocks(tmp_path: Path) -> None:
    # DL-177: one module's own repetition is the same drift class DL-72
    # named -- a copy strays from its own module's sibling just as easily as
    # from another module's
    a = _write(tmp_path / "a.py", f"def sum_a(items):{_BODY}\n\ndef sum_b(items):{_BODY}")
    findings = arch_check.duplicate_function_bodies([a])
    assert len(findings) == 1
    assert "identical body again in this module" in findings[0].message
    assert "sum_b()" in findings[0].message


def test_distinct_bodies_in_one_module_do_not_block(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.py", f"def sum_a(items):{_BODY}\n\ndef other(items):{_OTHER_BODY}")
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


# --------------------------------------------- 1b. near-miss duplicates (DL-177)

_NEAR_A = """
    a = x + 1
    b = y + 2
    c = a * b
    d = c - 3
    return d
"""

# same shape as _NEAR_A -- every local name renamed, every literal changed
_NEAR_B = """
    m = p + 10
    n = q + 20
    o = m * n
    r = o - 30
    return r
"""

# same names and literals as _NEAR_A, but the first assignment sits inside a
# try/except instead of standing bare -- a different control-flow shape,
# which alpha-normalisation does not, and should not, paper over
_NEAR_STRUCTURAL = """
    try:
        a = x + 1
    except ValueError:
        a = 0
    b = y + 2
    c = a * b
    d = c - 3
    return d
"""


def test_renamed_and_reliteralled_copy_is_a_near_miss(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.py", f"def calc_a(x, y):{_NEAR_A}")
    b = _write(tmp_path / "b.py", f"def calc_b(p, q):{_NEAR_B}")
    pairs = arch_check.near_miss_duplicate_bodies([a, b])
    assert pairs == [f"{arch_check._rel(a)}:calc_a ~ {arch_check._rel(b)}:calc_b"]
    # renamed names and changed literals mean the raw bodies do not match --
    # this pair is this tier's own subject, not the blocking tier's
    assert arch_check.duplicate_function_bodies([a, b]) == []


def test_an_exact_duplicate_pair_is_not_also_reported_as_near_miss(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.py", f"def calc_a(x, y):{_NEAR_A}")
    b = _write(tmp_path / "b.py", f"def calc_b(x, y):{_NEAR_A}")
    assert len(arch_check.duplicate_function_bodies([a, b])) == 1
    assert arch_check.near_miss_duplicate_bodies([a, b]) == []


def test_structurally_different_bodies_are_not_a_near_miss(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.py", f"def calc_a(x, y):{_NEAR_A}")
    d = _write(tmp_path / "d.py", f"def calc_d(x, y):{_NEAR_STRUCTURAL}")
    assert arch_check.near_miss_duplicate_bodies([a, d]) == []


def test_short_bodies_are_exempt_from_the_near_miss_tier(tmp_path: Path) -> None:
    short = "\n    a = 1\n    b = 2\n    return a + b\n"  # 3 statements, under the floor
    a = _write(tmp_path / "a.py", f"def f(x):{short}")
    b = _write(tmp_path / "b.py", f"def g(y):{short}")
    assert arch_check.near_miss_duplicate_bodies([a, b]) == []


def test_a_baselined_near_miss_pair_is_not_reported(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.py", f"def calc_a(x, y):{_NEAR_A}")
    b = _write(tmp_path / "b.py", f"def calc_b(p, q):{_NEAR_B}")
    pairs = arch_check.near_miss_duplicate_bodies([a, b])
    assert arch_check.near_miss_advisories(pairs, {}) == [f"near-miss duplicate: {pairs[0]}"]
    baseline = {"near_miss_duplicate_bodies": pairs}
    assert arch_check.near_miss_advisories(pairs, baseline) == []


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
class JobRuntime(BaseModel):
    status: str = "INACTIVE"
    armed: bool = False
    run_number: int = 0


class RuntimeState:
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
    assert all("RuntimeState (DL-82)" in m for m in messages)


def test_owner_body_and_unrelated_modules_do_not_block(tmp_path: Path) -> None:
    """Two exemptions that must hold or the gate is unusable: the owner class
    writes the fields by definition, and a module that does not define
    JobRuntime may legitimately own an attribute of the same name (the
    supervisor's _Run.run_number is the real instance)."""
    owner_only = _write(
        tmp_path / "oracle.py",
        "class JobRuntime(BaseModel):\n    armed: bool = False\n\n\n"
        "class RuntimeState:\n    def update(self, job):\n        self.job[job].armed = True\n",
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


def test_reaching_past_the_read_only_views_blocks(tmp_path: Path) -> None:
    """DL-86 made `store.job` / `store.globals_` read-only PROXIES, so the
    clause above now describes a write that raises at runtime. The way past a
    proxy is the private map behind it, so the gate watches those names too --
    otherwise hardening the public path would have quietly moved the hole
    rather than closed it."""
    mod = _write(
        tmp_path / "runner.py",
        "def poke(engine, job):\n"
        "    engine.oracle.store._jobs[job] = 1\n"
        "    engine.oracle.store._globals['X'] = 'y'\n"
        "    engine.oracle.store._timers.clear()\n",
    )
    kinds = sorted({f.message.split(":")[0] for f in arch_check.state_owner_bypasses([mod])})
    assert kinds == [
        "mutates store._timers through .clear()",
        "writes store._globals directly",
        "writes store._jobs directly",
    ]


def test_setattr_outside_the_owner_blocks(tmp_path: Path) -> None:
    """The hole an assignment walk cannot see: setattr()'s attribute name is a
    runtime value, so no field-name check applies to it. Since DL-86 froze
    JobRuntime it is not legitimate even inside the owner -- but the exemption
    stays scoped to the owner class rather than removed, because the gate's job
    is to say WHERE state may change, and pydantic already says how."""
    mod = _write(
        tmp_path / "oracle.py",
        "class JobRuntime(BaseModel):\n    armed: bool = False\n\n\n"
        "class RuntimeState:\n"
        "    def update(self, job, **fields):\n"
        "        for k, v in fields.items():\n"
        "            setattr(self.job[job], k, v)\n\n\n"
        "class Oracle:\n"
        "    def sneak(self, rt, name, value):\n"
        "        setattr(rt, name, value)\n",
    )
    findings = arch_check.state_owner_bypasses([mod])
    assert len(findings) == 1
    assert "setattr() outside RuntimeState" in findings[0].message


def test_gate_watches_fields_it_was_never_told_about(tmp_path: Path) -> None:
    """DL-83: the watched set is DERIVED from the model's own AST. A
    hard-coded list stops protecting the moment a field is added -- state_rev
    is about to be one -- and a gate that quietly narrows is worse than none.
    `future_field` appears in no list anywhere and must still be caught."""
    mod = _write(
        tmp_path / "oracle.py",
        "class JobRuntime(BaseModel):\n    future_field: int = 0\n\n\n"
        "class RuntimeState:\n    pass\n\n\n"
        "class Oracle:\n    def poke(self, rt):\n        rt.future_field = 1\n",
    )
    findings = arch_check.state_owner_bypasses([mod])
    assert len(findings) == 1
    assert "JobRuntime.future_field" in findings[0].message


def test_the_wider_write_shapes_all_block(tmp_path: Path) -> None:
    """Ordinary Python that an Assign/AugAssign walk alone misses: annotated
    assignment, tuple destructuring, `del`, and mapping mutators on the owner's
    containers. Each is a real way to change state, so each must block."""
    mod = _write(
        tmp_path / "oracle.py",
        "class JobRuntime(BaseModel):\n    armed: bool = False\n    status: str = ''\n\n\n"
        "class RuntimeState:\n    pass\n\n\n"
        "class Oracle:\n"
        "    def annotated(self, rt):\n        rt.armed: bool = True\n"
        "    def destructured(self, rt, other):\n        rt.armed, other.status = True, 'x'\n"
        "    def deleted(self, rt):\n        del rt.armed\n"
        "    def mapping(self, other):\n        self.store.job.update(other)\n"
        "    def popped(self):\n        self.store.globals_.pop('X', None)\n",
    )
    kinds = sorted({f.message.split(":")[0] for f in arch_check.state_owner_bypasses([mod])})
    assert kinds == [
        "assigns JobRuntime.armed directly",
        "assigns JobRuntime.status directly",
        "mutates store.globals_ through .pop()",
        "mutates store.job through .update()",
    ]


# ------------------------------------------------- 3b. cited tests exist (DL-110)


def test_a_doc_citing_a_test_that_does_not_exist_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worked example's citation is what makes it a claim rather than a
    story, and it breaks by ordinary means: renaming a test is a refactor
    nobody thinks of as a documentation change."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    _write(tests_dir / "test_thing.py", "def test_one_that_is_real() -> None:\n    pass\n")
    monkeypatch.setattr(arch_check, "TESTS", tests_dir)
    doc = _write(
        tmp_path / "spec.md",
        "Held by `test_one_that_is_real`.\nAnd by `test_one_that_was_renamed_away`.\n",
    )
    findings = arch_check.unresolved_test_citations([doc])
    assert len(findings) == 1
    assert findings[0].line == 2
    assert "`test_one_that_was_renamed_away`" in findings[0].message


def test_family_patterns_and_prose_are_not_citations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The docs name conventions as often as they name functions --
    `test_cmNN_*` is a naming rule, not a missing test. Blocking on those
    would make the gate unusable in exactly the documents it exists for."""
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    _write(tests_dir / "test_thing.py", "def test_one_that_is_real() -> None:\n    pass\n")
    monkeypatch.setattr(arch_check, "TESTS", tests_dir)
    doc = _write(
        tmp_path / "spec.md",
        "Tests are named `test_cmNN_*`, on the house convention of `test_semXX_*`.\n"
        "See also `test_sem09*` and `test_harness_*`, and `test_one_that_is_real`.\n"
        "Prose mentioning test_one_that_was_renamed_away without backticks is not a citation.\n",
    )
    assert arch_check.unresolved_test_citations([doc]) == []


def test_the_real_docs_cite_only_tests_that_exist() -> None:
    """The pinned-artifact exception (module docstring): this one asserts on
    the real tree on purpose, because the tree is what the gate protects."""
    assert arch_check.unresolved_test_citations(arch_check.citing_doc_files()) == []
    assert len(arch_check.defined_test_names()) > 500
