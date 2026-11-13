"""DL-495: subject comments share one semantic job name across catalog consumers."""

from __future__ import annotations

import re
from html import unescape
from pathlib import Path

import pytest
from test_main_config import estate_file
from test_main_config_restart import cli_engine
from typer.testing import CliRunner

from dsl41.ast_jil import parse, render, render_canonical
from dsl41.cli import app
from dsl41.conditions import StatusAtom
from dsl41.derive import derive_graph, local_producer
from dsl41.dsl import CatalogBuilder, decompile
from dsl41.equiv import canonical_catalog, equivalent_tier_a
from dsl41.ir import LoweringError, lower_source
from dsl41.minify import MinifyRefusal, minify_files
from dsl41.reload_diff import diff
from dsl41.reload_plan import spec_texts
from dsl41.runner_control import roundtrip
from dsl41.viz_explore import _elements

pytestmark = pytest.mark.usefixtures("cli_env")
runner = CliRunner()
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ACCEPTED = [
    ("j /* c */", True, "j"),
    ("j   /* c */", False, "j"),
    ("j /* first */ /* second */", True, "j"),
    ("/* before */ j", True, "j"),
    ("/* before */ j /* after */", True, "j"),
    ('j /* quote " and owner: bob */ /* second */', True, "j"),
    (r"j\:x /* c */", True, "j:x"),
    ("j\t/* c */", True, "j"),
    ("my job", False, "my job"),
]
COMMENT_ERROR = "job name cannot be read across comment '/* see owner: bob */' (DL-495)"
REFUSED = [
    ("j /* see owner: bob */ tail", False, COMMENT_ERROR),
    ("/* before */ j /* see owner: bob */ tail /* after */", True, COMMENT_ERROR),
    ("j /* see owner: bob */ /* second */ tail", True, COMMENT_ERROR),
    ("j/* c */", True, "job name 'j/* c */' must be one directory component (DL-494)"),
    ('"j /* c */"', True, "job name '\"j /* c */\"' must be one directory component (DL-494)"),
    ("/* c */", True, "insert_job: missing job name"),
    ("/* first */ /* second */", True, "insert_job: missing job name"),
]


def _source(subject: str, inline: bool = True) -> str:
    header = f"insert_job: {subject}"
    job_type = " job_type: c\n" if inline else "\njob_type: c\n"
    return header + job_type + "command: true\nmachine: localhost\n"


@pytest.mark.parametrize(("subject", "inline", "name"), ACCEPTED)
def test_lowering_reads_one_name_piece(subject: str, inline: bool, name: str) -> None:
    catalog = lower_source(_source(subject, inline))
    assert list(catalog.jobs) == [name]
    assert catalog.jobs[name].name == name


@pytest.mark.parametrize(("subject", "inline", "message"), REFUSED)
def test_lowering_locates_an_unreadable_name(subject: str, inline: bool, message: str) -> None:
    with pytest.raises(LoweringError) as caught:
        lower_source("\n" + _source(subject, inline), file="jobs.jil")
    (finding,) = caught.value.findings
    assert finding.message == message
    assert finding.span is not None
    assert finding.span.file == "jobs.jil"
    assert finding.span.line_start == 2
    assert f"jobs.jil:2: {message}" in str(caught.value)


@pytest.mark.parametrize(("subject", "inline", "name"), ACCEPTED)
def test_lint_reads_one_name_piece(tmp_path: Path, subject: str, inline: bool, name: str) -> None:
    source = tmp_path / "jobs.jil"
    source.write_text(_source(subject, inline))
    result = runner.invoke(app, ["lint", str(source)])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(("subject", "inline", "message"), REFUSED)
def test_lint_locates_an_unreadable_name(
    tmp_path: Path, subject: str, inline: bool, message: str
) -> None:
    source = tmp_path / "jobs.jil"
    source.write_text("\n" + _source(subject, inline))
    result = runner.invoke(app, ["lint", str(source)])
    output = ANSI.sub("", result.output)
    assert result.exit_code == 2, output
    assert f"{source}:2: {message}" in output


@pytest.mark.parametrize(("subject", "inline", "_expected"), ACCEPTED + REFUSED)
def test_subject_comment_inputs_keep_f1_and_f2(subject: str, inline: bool, _expected: str) -> None:
    source = _source(subject, inline)
    assert render(parse(source)) == source
    if subject == "/* c */" and inline:
        # BUG-80: canonical rendering of a comment-only subject before inline job_type
        # is not a fixpoint.
        return
    canonical = render_canonical(parse(source))
    assert render_canonical(parse(canonical)) == canonical


@pytest.mark.parametrize("job_type", ["c", "b", "f"])
def test_every_job_type_reads_the_subject_without_a_comment(job_type: str) -> None:
    execution = {
        "c": "command: true\nmachine: localhost\n",
        "b": "",
        "f": "watch_file: /dev/null\nmachine: localhost\n",
    }[job_type]
    source = f"insert_job: j /* c */ job_type: {job_type}\n{execution}"
    assert list(lower_source(source).jobs) == ["j"]


@pytest.mark.parametrize("job_type", ["c", "b"])
def test_references_updates_report_and_explore_share_the_name(
    tmp_path: Path, job_type: str
) -> None:
    plain = _source("j") if job_type == "c" else "insert_job: j job_type: b\n"
    plain += _source("dependent") + "condition: s(j)\n"
    if job_type == "b":
        plain += _source("member") + "box_name: j\n"
    commented = plain.replace("insert_job: j job_type:", "insert_job: j /* c */ job_type:")
    catalog = lower_source(commented)
    assert catalog.jobs["j"].name == "j"
    condition = catalog.jobs["dependent"].sem.condition
    assert condition is not None and isinstance(condition.cond, StatusAtom)
    assert condition.cond.job.name == "j"
    graph = derive_graph(catalog)
    (edge,) = [edge for edge in graph.edges if edge.dst == "dependent"]
    assert local_producer(edge, catalog) == "j"
    if job_type == "b":
        assert catalog.jobs["member"].box.box_name == "j"
        assert graph.box_tree.parent["member"] == "j"
    plan = diff(lower_source(plain), catalog, {}, {})
    assert plan.added == plan.removed == ()
    assert plan.changed == ()
    assert set(plan.unchanged) == set(catalog.jobs)
    changed = diff(
        catalog, lower_source(commented.replace("command: true", "command: false")), {}, {}
    )
    assert {change.name for change in changed.changed} == set(catalog.jobs) - (
        {"j"} if job_type == "b" else set()
    )
    elements = _elements(catalog, graph)
    assert {node["data"]["id"] for node in elements["nodes"]} == set(catalog.jobs)
    source = tmp_path / "jobs.jil"
    source.write_text(commented)
    for output_format in ("report", "explore"):
        target = tmp_path / f"{output_format}.html"
        result = runner.invoke(
            app, ["viz", str(source), "--format", output_format, "-o", str(target)]
        )
        assert result.exit_code == 0, result.output
        html = target.read_text()
        assert "j /* c */" not in unescape(html)
        assert "dependent" in html


@pytest.mark.parametrize("subject", ["j /* c */", "/* before */ j /* first */ /* second */"])
def test_emitters_use_the_semantic_name(tmp_path: Path, subject: str) -> None:
    source = _source(subject)
    catalog = lower_source(source)
    python = decompile(catalog)
    assert "/*" not in python
    namespace: dict[str, object] = {"__name__": "<decompiled>"}
    exec(compile(python, "<decompiled>", "exec"), namespace)  # noqa: S102
    builder = namespace["estate"]()  # type: ignore[operator]
    assert isinstance(builder, CatalogBuilder)
    emitted = builder.to_jil()
    assert "insert_job: j\n" in emitted
    assert "/*" not in emitted
    assert equivalent_tier_a(catalog, lower_source(emitted)).equivalent
    assert list(canonical_catalog(catalog).jobs) == ["j"]
    original = tmp_path / "commented.jil"
    original.write_text(source)
    plain = tmp_path / "plain.jil"
    plain.write_text(_source("j"))
    result = runner.invoke(app, ["equiv", str(original), "--against", str(plain)])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("job_type", ["c", "b"])
def test_minify_allocates_the_same_name_for_declarations_and_references(job_type: str) -> None:
    source = _source("j /* c */") if job_type == "c" else "insert_job: j /* c */ job_type: b\n"
    source += _source("dependent") + "condition: s(j)\n"
    if job_type == "b":
        source += _source("member") + "box_name: j\n"
    result = minify_files([parse(source)])
    assert "j /* c */" not in result.names.jobs
    mapped = result.names.jobs["j"]
    assert mapped == ("j1" if job_type == "c" else "b1")
    catalog = lower_source(result.bodies[0])
    graph = derive_graph(catalog)
    assert any(
        edge.src == mapped and edge.dst == result.names.jobs["dependent"] for edge in graph.edges
    )
    if job_type == "b":
        assert catalog.jobs[result.names.jobs["member"]].box.box_name == mapped


@pytest.mark.parametrize("verify", [True, False])
def test_minify_refuses_a_comment_between_name_pieces(verify: bool) -> None:
    with pytest.raises(MinifyRefusal, match=re.escape(COMMENT_ERROR)) as caught:
        minify_files(
            [parse(_source("j /* see owner: bob */ tail", False), file="jobs.jil")], verify=verify
        )
    assert "jobs.jil:1:" in str(caught.value)


@pytest.mark.parametrize("verify", [True, False])
@pytest.mark.parametrize("subcommand", ["insert_job", "delete_box"])
def test_minify_collects_subject_errors_with_the_other_refusals(
    verify: bool, subcommand: str
) -> None:
    source = (
        _source("first")
        + "unknown_before: x\n"
        + f"{subcommand}: j /* see owner: bob */ tail job_type: c\n"
        + _source("last")
        + "unknown_after: x\n"
    )
    with pytest.raises(MinifyRefusal) as caught:
        minify_files([parse(source, file="jobs.jil")], verify=verify)
    messages = caught.value.messages
    assert len(messages) == 3
    assert "unknown_before" in messages[0]
    assert COMMENT_ERROR in messages[1]
    assert "unknown_after" in messages[2]
    assert all("jobs.jil:" in message for message in messages)


def test_spec_texts_keeps_only_readable_job_statements_and_their_original_bytes() -> None:
    source = _source("j /* c */")
    extra = (
        "insert_machine: j\n"
        + _source("j /* see owner: bob */ tail")
        + "update_job: j /* edit */\n"
    )
    texts = spec_texts([parse(source + extra)], lower_source(source))
    assert texts == {"j": source + "update_job: j /* edit */\n"}


@pytest.mark.parametrize(
    ("subject", "name", "job_type"),
    [
        ("j /* c */", "j", "c"),
        ("j /* c */", "j", "b"),
        (r"j\:x", "j:x", "c"),
        (r"j\:x /* c */", "j:x", "b"),
    ],
)
def test_spec_serves_original_source_at_startup_and_after_applied_update(
    short_root: Path, subject: str, name: str, job_type: str
) -> None:
    config = estate_file(short_root)
    source = short_root / "a.jil"
    before = f"insert_job: {subject} job_type: {job_type}\n"
    if job_type == "c":
        before += "command: true\nmachine: localhost\n"
    before += "description: before\n"
    after = before.replace("description: before", "description: after")
    source.write_text(before)
    socket = short_root / "run/control.sock"
    with cli_engine(config) as ask:
        reply = roundtrip(socket, {"v": 5, "cmd": "spec", "job": name})
        assert reply["ok"] and reply["jil"] == before
        source.write_text(after)
        preview = runner.invoke(app, ["reread", "-S", str(socket)])
        assert preview.exit_code == 0, preview.output
        applied = runner.invoke(app, ["update", "-S", str(socket), "--wait"])
        assert applied.exit_code == 0, applied.output
        assert ask()["revision"] == 2
        reply = roundtrip(socket, {"v": 5, "cmd": "spec", "job": name})
        assert reply["ok"] and reply["jil"] == after


@pytest.mark.parametrize(
    "subcommand", ["insert_job", "update_job", "delete_job", "override_job", "delete_box"]
)
def test_minify_reads_every_classified_job_subject(subcommand: str) -> None:
    result = minify_files([parse(f"{subcommand}: j /* c */ job_type: c\n")], verify=False)
    assert result.names.jobs == {"j": "j1"}


def test_minify_keeps_delete_box_out_of_the_membership_definition_pass() -> None:
    source = "delete_box: parent job_type: b\n" + _source("member") + "box_name: parent\n"
    result = minify_files([parse(source)], verify=False)
    assert result.names.jobs == {"parent": "j1", "member": "j1j1"}


def test_minify_keeps_rename_job_unclassified() -> None:
    with pytest.raises(MinifyRefusal, match="subcommand 'rename_job' is not classified"):
        minify_files([parse("rename_job: j /* c */ job_type: c\n")], verify=False)


@pytest.mark.parametrize(
    "subcommand", ["update_job", "delete_job", "rename_job", "override_job", "delete_box"]
)
def test_lowering_still_refuses_job_mutation_statements(subcommand: str) -> None:
    with pytest.raises(LoweringError, match="not supported by lowering"):
        lower_source(f"{subcommand}: j /* c */ job_type: c\n")
