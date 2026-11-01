"""Navigation over a synthetic estate, its three input forms and its executable DSL."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41.cli import app
from dsl41.derive import DerivedGraph, derive_graph
from dsl41.dsl import CatalogBuilder, decompile, job_to_source
from dsl41.ir import CatalogIR
from dsl41.nav import NavNotFound, navigate

pytestmark = pytest.mark.usefixtures("cli_env")
runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


@pytest.fixture
def builder() -> CatalogBuilder:
    c = CatalogBuilder()
    c.global_("root", "yes")
    c.xinst("REMOTE", xtype="a")
    c.job("root", command="root.sh")
    with c.box("outer", box_success="s(end)"):
        c.job("left", command="left.sh", condition="s(root)")
        with c.box("inner"):
            c.job("right", command="right.sh", condition="f(root)")
            c.job("join", command="join.sh", condition="s(left) | d(right)")
        c.job("end", command="end.sh", condition="s(join)")
    c.job(
        "mixed",
        command="mixed.sh",
        condition="s(root) & n(lock) & s(missing) & s(remote^REMOTE) & v(root) = yes",
    )
    c.job("lock", command="lock.sh", condition="n(mixed)")
    c.job("qualified", command="qualified.sh", condition="n(root, 00.05) & e(left) >= 3")
    c.job("multi", command="multi.sh", condition="s(root) | f(root)")
    c.job("self", command="self.sh", condition="s(self)")
    c.job("cycle_a", command="cycle.sh", condition="s(cycle_b)")
    c.job("cycle_b", command="cycle.sh", condition="s(cycle_a)")
    c.job("remote^REMOTE", command="local.sh")
    c.job("isolated", command="isolated.sh")
    c.job("watch", job_type="f", watch_file="/tmp/synthetic-input")
    return c


@pytest.fixture
def catalog(builder: CatalogBuilder) -> CatalogIR:
    return builder.build()


@pytest.fixture
def graph(catalog: CatalogIR) -> DerivedGraph:
    return derive_graph(catalog)


@pytest.fixture
def estate(tmp_path: Path, builder: CatalogBuilder) -> Path:
    path = tmp_path / "estate.jil"
    path.write_text(builder.to_jil(), encoding="utf-8")
    return path


def names(graph: DerivedGraph, verb: str, job: str, **kwargs: object) -> list[str]:
    return [item.name for item in navigate(graph, verb, job, **kwargs)]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("verb", "job", "expected"),
    [
        ("fanins", "join", ["left", "right"]),
        ("upstream", "join", ["left", "right"]),
        ("fanouts", "root", ["left", "mixed", "multi", "qualified", "right"]),
        ("downstream", "join", ["end"]),
        ("box", "join", ["inner"]),
        ("box", "outer", []),
        ("children", "outer", ["end", "inner", "left"]),
        ("children", "root", []),
        ("siblings", "join", ["right"]),
        ("siblings", "root", []),
        ("ancestors", "end", ["join", "left", "right", "root"]),
        ("descendants", "left", ["end", "join", "qualified"]),
        ("ancestors", "cycle_a", ["cycle_b"]),
        ("descendants", "self", []),
        ("fanins", "self", ["self"]),
        ("fanouts", "self", ["self"]),
        ("fanouts", "outer", []),
        ("fanouts", "end", []),
        ("fanins", "mixed", ["root"]),
        ("fanins", "lock", []),
        ("fanins", "qualified", ["left", "root"]),
        ("fanouts", "remote^REMOTE", []),
    ],
)
def test_navigation_verbs(graph: DerivedGraph, verb: str, job: str, expected: list[str]) -> None:
    before = graph.model_dump_json()
    assert names(graph, verb, job) == expected
    assert graph.model_dump_json() == before


def test_all_keeps_mutex_direction_external_identity_and_undefined_jobs(
    graph: DerivedGraph,
) -> None:
    assert names(graph, "fanins", "mixed", include_all=True) == [
        "lock",
        "missing",
        "remote^REMOTE",
        "root",
    ]
    assert names(graph, "fanouts", "lock", include_all=True) == ["mixed"]
    assert names(graph, "fanins", "lock", include_all=True) == ["mixed"]
    assert names(graph, "descendants", "remote^REMOTE", include_all=True) == []
    assert names(graph, "ancestors", "mixed", include_all=True) == [
        "lock",
        "missing",
        "remote^REMOTE",
        "root",
    ]
    with pytest.raises(NavNotFound):
        navigate(graph, "fanouts", "missing")
    assert names(graph, "fanouts", "missing", include_all=True) == ["mixed"]


def test_all_keeps_one_way_mutex_and_omits_global_names() -> None:
    c = CatalogBuilder().global_("flag", "1").job("producer", command="x")
    c.job("guarded", command="x", condition="n(producer) & n(undefined)")
    c.job("global_only", command="x", condition="v(flag) = 1")
    c.job("self", command="x", condition="n(self)")
    graph = derive_graph(c.build())
    assert names(graph, "fanins", "guarded") == []
    assert names(graph, "fanins", "guarded", include_all=True) == ["producer", "undefined"]
    assert names(graph, "fanins", "producer", include_all=True) == []
    assert names(graph, "fanouts", "producer", include_all=True) == ["guarded"]
    assert names(graph, "fanins", "global_only", include_all=True) == []
    assert names(graph, "fanins", "self", include_all=True) == ["self"]
    assert names(graph, "path", "undefined", target="guarded", include_all=True) == [
        "undefined",
        "guarded",
    ]
    with pytest.raises(NavNotFound):
        navigate(graph, "path", "guarded", "producer", include_all=True)


@pytest.mark.parametrize(
    ("depth", "expected"), [(0, []), (1, ["join"]), (2, ["join", "left", "right"])]
)
def test_depth_bounds_by_edge_count(graph: DerivedGraph, depth: int, expected: list[str]) -> None:
    assert names(graph, "ancestors", "end", depth=depth) == expected
    assert names(graph, "descendants", "left", depth=1) == ["join", "qualified"]


def test_deep_children_and_membership_atoms(graph: DerivedGraph) -> None:
    assert names(graph, "children", "outer", deep=True) == ["end", "inner", "join", "left", "right"]
    assert [(m.name, m.atom) for m in navigate(graph, "box", "join")] == [
        ("inner", "box_name: inner")
    ]
    assert [(m.name, m.atom) for m in navigate(graph, "children", "inner")] == [
        ("join", "box_name: inner"),
        ("right", "box_name: inner"),
    ]
    assert [(m.name, m.atom) for m in navigate(graph, "siblings", "join")] == [
        ("right", "box_name: inner")
    ]


def test_path_is_shortest_sorted_and_directed(graph: DerivedGraph) -> None:
    path = navigate(graph, "path", "root", "end")
    assert [(m.name, m.atom) for m in path] == [
        ("root", ""),
        ("left", "s(root)"),
        ("join", "s(left)"),
        ("end", "s(join)"),
    ]
    assert names(graph, "path", "root", target="root") == ["root"]
    with pytest.raises(NavNotFound, match="no dependency path"):
        navigate(graph, "path", "end", "root")
    with pytest.raises(NavNotFound, match="not found"):
        navigate(graph, "path", "root", "absent")


def test_explanations_preserve_atoms_and_collapse_duplicate_names(graph: DerivedGraph) -> None:
    assert [(m.name, m.atom) for m in navigate(graph, "fanins", "multi")] == [
        ("root", "f(root); s(root)")
    ]
    assert [(m.name, m.atom) for m in navigate(graph, "fanins", "qualified")] == [
        ("left", "e(left) >= 3"),
        ("root", "n(root, 00.05)"),
    ]
    assert navigate(graph, "fanins", "mixed", include_all=True)[0].atom == "n(lock)"
    assert navigate(graph, "ancestors", "end")[0].atom == "s(join)"


@pytest.mark.parametrize(
    "verb", ["fanins", "fanouts", "box", "children", "siblings", "ancestors", "descendants"]
)
def test_every_relation_rejects_an_unknown_job(graph: DerivedGraph, verb: str) -> None:
    with pytest.raises(NavNotFound, match="not found"):
        navigate(graph, verb, "absent")


@pytest.mark.parametrize(
    ("job", "expected_conditions"),
    [
        ("root", []),
        ("outer", ["box_success: s(end)"]),
        ("join", ["condition: s(left) | d(right)"]),
        ("watch", []),
        (
            "mixed",
            ["condition: s(root) & n(lock) & s(missing) & s(remote^REMOTE) & v(root) = yes"],
        ),
    ],
)
def test_show_definition_uses_dsl_renderer(
    catalog: CatalogIR, estate: Path, job: str, expected_conditions: list[str]
) -> None:
    result = runner.invoke(app, ["nav", "show", str(estate), job, "--json"])
    assert result.exit_code == 0, ANSI.sub("", result.stderr)
    output = json.loads(ANSI.sub("", result.stdout))
    assert output == {"name": job, "definition": job_to_source(catalog.jobs[job])}
    c = CatalogBuilder()
    if job == "join":
        with c.box("inner"):
            pass
    exec(output["definition"], {"c": c})
    assert [
        line
        for line in c.to_jil().splitlines()
        if line.partition(":")[0] in {"condition", "box_success", "box_failure"}
    ] == expected_conditions
    rebuilt = c.build().jobs[job]
    assert rebuilt.job_type == catalog.jobs[job].job_type
    assert rebuilt.box == catalog.jobs[job].box


@pytest.mark.parametrize(
    ("verb", "job", "extra", "expected"),
    [
        ("fanins", "join", [], "left\nright\n"),
        ("upstream", "join", [], "left\nright\n"),
        ("fanouts", "join", [], "end\n"),
        ("downstream", "join", [], "end\n"),
        ("box", "join", [], "inner\n"),
        ("children", "outer", ["--deep"], "end\ninner\njoin\nleft\nright\n"),
        ("siblings", "join", ["--explain"], "right\tbox_name: inner\n"),
        ("ancestors", "end", ["--depth", "1"], "join\n"),
        ("descendants", "left", ["--depth", "1"], "join\nqualified\n"),
        ("fanins", "multi", ["--explain"], "root\tf(root); s(root)\n"),
        ("fanins", "mixed", ["--all", "--json"], '["lock", "missing", "remote^REMOTE", "root"]\n'),
        ("path", "root", ["end", "--json"], '["root", "left", "join", "end"]\n'),
        (
            "path",
            "root",
            ["end", "--explain"],
            "root\t\nleft\ts(root)\njoin\ts(left)\nend\ts(join)\n",
        ),
        ("box", "root", [], ""),
        ("box", "root", ["--json"], "[]\n"),
        ("fanins", "join", ["--explain", "--json"], '["left", "right"]\n'),
    ],
)
def test_cli_results(estate: Path, verb: str, job: str, extra: list[str], expected: str) -> None:
    result = runner.invoke(app, ["nav", verb, str(estate), job, *extra])
    assert result.exit_code == 0, ANSI.sub("", result.stderr)
    assert ANSI.sub("", result.stdout) == expected
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("words", "code", "message"),
    [
        (["fanins", "absent"], 1, "not found"),
        (["show", "absent"], 1, "not found"),
        (["path", "end", "root"], 1, "no dependency path"),
        (["path", "root", "absent"], 1, "not found"),
        (["nope", "root"], 2, "unknown nav verb"),
        (["path", "root"], 2, "two job names"),
        (["box", "root", "left"], 2, "one"),
        (["box", "root", "--all"], 2, "--all"),
        (["box", "root", "--deep"], 2, "--deep"),
        (["fanins", "root", "--depth", "2"], 2, "--depth"),
        (["ancestors", "root", "--depth", "-1"], 2, "--depth"),
        (["ancestors", "root", "--depth", "bad"], 2, "--depth"),
        (["show", "root", "--explain"], 2, "--explain"),
        (["fanins", "root", "--unknown"], 2, "--unknown"),
        (["fanins", "root", "--param", "X=y"], 2, "Python estate"),
    ],
)
def test_cli_error_codes(estate: Path, words: list[str], code: int, message: str) -> None:
    result = runner.invoke(app, ["nav", words[0], str(estate), *words[1:]])
    assert result.exit_code == code, result.exception
    assert result.stdout == ""
    assert message in ANSI.sub("", result.stderr)


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("missing.jil", None),
        ("bad.jil", "insert_job:"),
        ("unknown.jil", "insert_job: a\njob_type: c\nfrob: 1\n"),
        ("bad.toml", "not toml"),
        ("bad.py", "raise RuntimeError('synthetic failure')\n"),
        ("bad.py", "def estate():\n    return None\n"),
        ("bad.py", "def estate(:\n"),
        ("bad.jil", "insert_job: a\njob_type: c\ncondition: s(\n"),
    ],
)
def test_load_errors_are_exit_2(tmp_path: Path, name: str, content: str | None) -> None:
    path = tmp_path / name
    if content is not None:
        path.write_text(content, encoding="utf-8")
    result = runner.invoke(app, ["nav", "box", str(path), "a"])
    assert result.exit_code == 2, result.exception
    assert result.stdout == ""
    assert ANSI.sub("", result.stderr)


def test_all_input_forms_use_shared_loaders(
    tmp_path: Path, estate: Path, catalog: CatalogIR
) -> None:
    module = tmp_path / "estate.py"
    module.write_text(decompile(catalog), encoding="utf-8")
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text(decompile(catalog), encoding="utf-8")
    manifest = tmp_path / "estate.toml"
    manifest.write_text('name = "synthetic"\nfiles = ["estate.jil"]\n', encoding="utf-8")
    for path in (estate, module, package, manifest):
        result = runner.invoke(app, ["nav", "fanins", str(path), "join", "--json"])
        assert result.exit_code == 0, ANSI.sub("", result.stderr)
        assert json.loads(result.stdout) == ["left", "right"]
    assert not list(tmp_path.rglob("*.pyc"))


def test_manifest_layers_environment_and_cli_properties(tmp_path: Path) -> None:
    (tmp_path / "a.jil").write_text("insert_job: ~{$NAME}~\njob_type: c\ncommand: x\n")
    (tmp_path / "base.properties").write_text("NAME=base\n")
    (tmp_path / "env.properties").write_text("NAME=env\n")
    override = tmp_path / "override.properties"
    override.write_text("NAME=override\n")
    manifest = tmp_path / "estate.toml"
    manifest.write_text(
        'name="synthetic"\nfiles=["a.jil"]\nproperties=["base.properties"]\n'
        '[environments.test]\nproperties=["env.properties"]\n'
    )
    for job, extra in [("env", []), ("override", ["-p", str(override)])]:
        result = runner.invoke(
            app, ["nav", "show", str(manifest), job, "--environment", "test", *extra]
        )
        assert result.exit_code == 0, ANSI.sub("", result.stderr)
        assert f"c.job('{job}'" in result.stdout
    failed = runner.invoke(app, ["nav", "show", str(manifest), "env"])
    assert failed.exit_code == 2
    assert "choose an environment" in ANSI.sub("", failed.stderr)
    direct = runner.invoke(
        app, ["nav", "show", str(tmp_path / "a.jil"), "override", "-p", str(override)]
    )
    assert direct.exit_code == 0, ANSI.sub("", direct.stderr)


def test_python_param_and_permit_unknown(tmp_path: Path) -> None:
    path = tmp_path / "estate.py"
    path.write_text(
        "from dataclasses import dataclass\nfrom dsl41.dsl import CatalogBuilder\n"
        "@dataclass\nclass Params:\n    name: str\n"
        "def estate(params):\n    return CatalogBuilder().job(params.name, command='x', frob='1')\n"
    )
    result = runner.invoke(
        app, ["nav", "show", str(path), "chosen", "--param", "name=chosen", "--permit-unknown"]
    )
    assert result.exit_code == 0, ANSI.sub("", result.stderr)
    assert "passthrough={'frob': '1'}" in result.stdout
    for extra in [
        ["--param", "bad"],
        ["--param", "name=a", "--param", "name=b"],
        ["-p", "ignored"],
    ]:
        failed = runner.invoke(app, ["nav", "show", str(path), "chosen", *extra])
        assert failed.exit_code == 2
        assert failed.stdout == ""
        assert ANSI.sub("", failed.stderr)


@pytest.mark.parametrize(
    ("arguments", "code", "expected"),
    [
        (["fanins", "join"], 0, "left\nright\n"),
        (["downstream", "join", "--json"], 0, '["end"]\n'),
        (["path", "root", "end", "--json"], 0, '["root", "left", "join", "end"]\n'),
        (
            ["fanins", "mixed", "--all", "--json"],
            0,
            '["lock", "missing", "remote^REMOTE", "root"]\n',
        ),
        (["children", "outer", "--deep"], 0, "end\ninner\njoin\nleft\nright\n"),
        (["ancestors", "end", "--depth", "1", "--explain"], 0, "join\ts(join)\n"),
        (["fanins", "absent"], 1, ""),
        (["path", "end", "root"], 1, ""),
        (["nope", "join"], 2, ""),
        (["fanins"], 2, ""),
    ],
)
def test_decompiled_footer_navigation_subprocess(
    tmp_path: Path, catalog: CatalogIR, arguments: list[str], code: int, expected: str
) -> None:
    path = tmp_path / "estate.py"
    path.write_text(decompile(catalog), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(path), "nav", *arguments], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == code, ANSI.sub("", result.stderr)
    assert ANSI.sub("", result.stdout) == expected
    assert bool(ANSI.sub("", result.stderr)) == (code != 0)


def test_decompiled_footer_preserves_no_argument_jil_and_show(
    tmp_path: Path, catalog: CatalogIR
) -> None:
    path = tmp_path / "estate.py"
    path.write_text(decompile(catalog), encoding="utf-8")
    plain = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=30)
    assert plain.returncode == 0, plain.stderr
    assert "insert_job: root" in plain.stdout
    shown = subprocess.run(
        [sys.executable, str(path), "nav", "show", "join", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert shown.returncode == 0, ANSI.sub("", shown.stderr)
    assert json.loads(ANSI.sub("", shown.stdout))["definition"] == job_to_source(
        catalog.jobs["join"]
    )


@pytest.mark.parametrize(
    "body", ["raise RuntimeError('synthetic failure')", "raise SystemExit(5)", "return None"]
)
def test_embedded_estate_load_failure_is_exit_2(tmp_path: Path, body: str) -> None:
    path = tmp_path / "estate.py"
    path.write_text(
        f"from dsl41.dsl import main\ndef estate():\n    {body}\n"
        "if __name__ == '__main__':\n    main(estate)\n"
    )
    result = subprocess.run(
        [sys.executable, str(path), "nav", "box", "job"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "estate()" in ANSI.sub("", result.stderr)
    assert "Traceback" not in result.stderr
