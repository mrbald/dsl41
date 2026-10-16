"""scripts/comment_equivalence.py: the gate for the comment diet."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "comment_equivalence.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("comment_equivalence", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["comment_equivalence"] = module
    spec.loader.exec_module(module)
    return module


comment_equivalence = _load()

BASE_TEXT = '''"""Module doc."""


def f(a):
    """Doc."""
    # DL-123 explains this.
    total = a + 1
    return total
'''


def _repo(tmp_path: Path) -> Path:
    def run(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text(BASE_TEXT, encoding="utf-8")
    run("add", ".")
    run("commit", "-q", "-m", "base")
    return tmp_path


def _check(root: Path) -> list[str]:
    findings, _ = comment_equivalence.run(root, "HEAD", [])
    return findings


def _edit(root: Path, text: str, rel: str = "src/m.py") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_comment_only_change_passes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _edit(root, BASE_TEXT.replace("explains this", "is the rule here"))
    assert _check(root) == []


def test_docstring_only_change_passes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _edit(root, BASE_TEXT.replace('"""Doc."""', '"""A different doc."""'))
    assert _check(root) == []


def test_renamed_local_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _edit(root, BASE_TEXT.replace("total", "result"))
    assert any("code differs" in line for line in _check(root))


def test_changed_literal_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _edit(root, BASE_TEXT.replace("a + 1", "a + 2"))
    assert any("code differs" in line for line in _check(root))


def test_lost_citation_token_fails_and_is_named(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _edit(root, BASE_TEXT.replace("# DL-123 explains this.", "# explains this."))
    findings = _check(root)
    assert len(findings) == 1
    assert "DL-123" in findings[0]


def test_kept_token_with_new_wording_passes(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _edit(root, BASE_TEXT.replace("DL-123 explains this.", "Rule of DL-123."))
    assert _check(root) == []


def test_added_file_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/new.py"], cwd=root, check=True)
    assert any("added" in line for line in _check(root))


def test_untracked_file_is_reported_as_added(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "src" / "conftest.py").write_text("x = 1\n", encoding="utf-8")
    findings = _check(root)
    assert findings == ["src/conftest.py: file added since HEAD"]


def test_deleted_file_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "src" / "m.py").unlink()
    assert any("deleted" in line for line in _check(root))


def test_changed_non_python_file_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    page = root / "src" / "page.html"
    page.write_text("<script>var x=1</script>\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/page.html"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "page"], cwd=root, check=True)
    page.write_text("<script>var x=2</script>\n", encoding="utf-8")
    assert any("not a .py file" in line for line in _check(root))


def test_explicit_paths_limit_the_check(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _edit(root, BASE_TEXT.replace("total", "result"))
    findings, count = comment_equivalence.run(root, "HEAD", ["src/other.py"])
    assert count == 1
    assert findings == ["src/other.py: file added since HEAD"]


def test_bad_base_rev_raises(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    with pytest.raises(comment_equivalence.GitError):
        comment_equivalence.run(root, "NOSUCHREV", [])


def test_main_exits_1_and_names_git_on_a_bad_rev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(comment_equivalence, "ROOT", _repo(tmp_path))
    assert comment_equivalence.main(["NOSUCHREV"]) == 1
    assert "git failed" in capsys.readouterr().err


def test_main_exit_codes_and_count_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(comment_equivalence, "ROOT", root)
    _edit(root, BASE_TEXT.replace("explains this", "is the rule here"))
    assert comment_equivalence.main(["HEAD"]) == 0
    assert capsys.readouterr().out == "comment_equivalence: 1 files equal\n"
    _edit(root, BASE_TEXT.replace("a + 1", "a + 2"))
    assert comment_equivalence.main(["HEAD"]) == 1
    assert "code differs" in capsys.readouterr().out


@pytest.mark.parametrize(
    "token",
    [
        "DL-178j",
        "CM-07",
        "ss1.3",
        "Q8d(iii)",
        "PR-12",
        "SEM-36",
        "Qr2",
        "U3b",
        "E12",
        "T-001",
        "T04a",
        "job.403",
        "PENDING: E10",
    ],
)
def test_citation_shapes_are_matched(token: str) -> None:
    assert comment_equivalence.citation_tokens(f"see {token} here.") == {token}


def test_citation_regex_ignores_lookalikes() -> None:
    assert comment_equivalence.citation_tokens("SEQ17 and 17 and x = 12") == set()


def test_lost_lettered_token_fails(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _edit(root, BASE_TEXT.replace("# DL-123 explains", "# DL-123j and CM-07 explain"))
    subprocess.run(["git", "commit", "-qam", "tokens"], cwd=root, check=True)
    _edit(root, BASE_TEXT)
    findings = _check(root)
    assert len(findings) == 1
    assert "CM-07" in findings[0] and "DL-123j" in findings[0]


MODEL_TEXT = '''from pydantic import BaseModel


class Base(BaseModel):
    """Schema text."""

    a: int


class Child(Base):
    """Child text."""


class Plain:
    """Plain text."""
'''


SCHEMA_REL = "src/dsl41/ir.py"


def _model_repo(tmp_path: Path, rel: str = SCHEMA_REL) -> Path:
    root = _repo(tmp_path)
    _edit(root, MODEL_TEXT, rel)
    subprocess.run(["git", "add", rel], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "models"], cwd=root, check=True)
    return root


def test_basemodel_docstring_change_trips(tmp_path: Path) -> None:
    root = _model_repo(tmp_path)
    _edit(root, MODEL_TEXT.replace("Schema text", "Other text"), SCHEMA_REL)
    findings = _check(root)
    assert len(findings) == 1
    assert "schema-bearing docstring changed on class Base" in findings[0]


def test_subclass_of_local_model_docstring_change_trips(tmp_path: Path) -> None:
    root = _model_repo(tmp_path)
    _edit(root, MODEL_TEXT.replace("Child text", "Other text"), SCHEMA_REL)
    findings = _check(root)
    assert len(findings) == 1
    assert "class Child" in findings[0]


def test_plain_class_docstring_change_is_silent(tmp_path: Path) -> None:
    root = _model_repo(tmp_path)
    _edit(root, MODEL_TEXT.replace("Plain text", "Other text"), SCHEMA_REL)
    assert _check(root) == []


def test_model_docstring_change_outside_schema_modules_passes(tmp_path: Path) -> None:
    root = _model_repo(tmp_path, "src/m.py")
    _edit(root, MODEL_TEXT.replace("Schema text", "Other text"))
    assert _check(root) == []
