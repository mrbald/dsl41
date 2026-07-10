"""Placeholder resolver tests (DL-19, non-core preprocessor).

Normative detail: the placeholders.py module docstring (format decisions:
token grammar, properties grammar, fixpoint resolution, layering vs.
collision, loud-residue rule, convergence bound). Every decision listed
there is pinned by a test below. The end-to-end test feeds the resolved
DL-18 corpus fixture through the ordinary pipeline -- the tool's whole
purpose is that the core never has to model templating.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41.cli import app
from dsl41.ir import lower_source
from dsl41.placeholders import (
    PlaceholderError,
    load_properties,
    resolve_text,
    substitute,
)

CORPUS_DIR = Path(__file__).parent / "corpus"

runner = CliRunner()


def props(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ----------------------------------------------------------------- substitution


def test_basic_substitution_everywhere_in_the_text(tmp_path: Path) -> None:
    p = props(tmp_path, "env.properties", "ENVID=DEV1\n")
    text = (
        "insert_job: APP_~{$ENVID}~_A   job_type: box\n"
        "condition: s(APP_~{$ENVID}~_B)\napplication: ~{$ENVID}~_APP\n"
    )
    resolved, reports = resolve_text(text, [p])
    assert reports == []
    assert resolved == (
        "insert_job: APP_DEV1_A   job_type: box\ncondition: s(APP_DEV1_B)\napplication: DEV1_APP\n"
    )


def test_shell_and_global_var_syntaxes_never_match(tmp_path: Path) -> None:
    """`$$VAR` (SEM-08) and `${VAR}` (machine-side runtime) are not estate
    tokens; they pass through untouched and trigger no residue error."""
    p = props(tmp_path, "env.properties", "ENVID=DEV1\n")
    text = "std_out_file: /log/${AUTO_JOB_NAME}_$$RUNID.out\n"
    resolved, reports = resolve_text(text, [p])
    assert resolved == text
    assert reports == []


def test_crlf_and_layout_are_byte_preserved(tmp_path: Path) -> None:
    p = props(tmp_path, "env.properties", "X=y\n")
    text = "insert_job: ~{$X}~  \r\n\r\nowner: op\r\n"
    resolved, _ = resolve_text(text, [p])
    assert resolved == "insert_job: y  \r\n\r\nowner: op\r\n"


def test_undefined_token_is_loud_with_file_and_line(tmp_path: Path) -> None:
    p = props(tmp_path, "env.properties", "ENVID=DEV1\n")
    with pytest.raises(PlaceholderError) as exc_info:
        resolve_text("a: 1\nb: ~{$MISSING}~\n", [p], file="in.jil")
    assert "in.jil:2" in str(exc_info.value)
    assert "~{$MISSING}~" in str(exc_info.value)


def test_malformed_lookalike_is_loud(tmp_path: Path) -> None:
    """`~{ENVID}~` (no `$`) is residue, not a silent pass-through."""
    p = props(tmp_path, "env.properties", "ENVID=DEV1\n")
    with pytest.raises(PlaceholderError, match="placeholder-like"):
        resolve_text("name: ~{ENVID}~\n", [p])


def test_permit_unresolved_carries_verbatim_and_reports(tmp_path: Path) -> None:
    p = props(tmp_path, "env.properties", "ENVID=DEV1\n")
    text = "a: ~{$ENVID}~ ~{$MISSING}~\n"
    resolved, reports = resolve_text(text, [p], file="in.jil", permit_unresolved=True)
    assert resolved == "a: DEV1 ~{$MISSING}~\n"
    assert reports == ["in.jil:1: unresolved placeholder-like token '~{$MISSING}~'"]


def test_nested_tokens_resolve_inner_out(tmp_path: Path) -> None:
    p = props(tmp_path, "env.properties", "ENVID=DEV1\nHOST_DEV1=dev1.example.com\n")
    resolved, _ = resolve_text("machine: ~{$HOST_~{$ENVID}~}~\n", [p])
    assert resolved == "machine: dev1.example.com\n"


def test_substitution_convergence_is_bounded() -> None:
    """Hand-built oscillating bindings (load_properties would refuse them as
    stuck) must hit the pass bound, not spin."""
    with pytest.raises(PlaceholderError, match="did not converge"):
        substitute("~{$A}~", {"A": "~{$B}~", "B": "~{$A}~"})


# ------------------------------------------------------------------- properties


def test_reference_in_value(tmp_path: Path) -> None:
    p = props(tmp_path, "a.properties", "ENVID=DEV1\nAPP_HOME=/opt/app/~{$ENVID}~\n")
    assert load_properties([p]) == {"ENVID": "DEV1", "APP_HOME": "/opt/app/DEV1"}


def test_reference_in_key(tmp_path: Path) -> None:
    p = props(tmp_path, "a.properties", "ENVID=DEV1\nHOST_~{$ENVID}~=dev1.example.com\n")
    assert load_properties([p]) == {"ENVID": "DEV1", "HOST_DEV1": "dev1.example.com"}


def test_use_before_define_and_across_files(tmp_path: Path) -> None:
    """Resolution is an order-independent fixpoint: a reference may precede
    its definition, including across file boundaries in either direction."""
    a = props(tmp_path, "a.properties", "URL=https://~{$HOST}~/x\n")
    b = props(tmp_path, "b.properties", "HOST=h.example.com\n")
    assert load_properties([a, b])["URL"] == "https://h.example.com/x"
    assert load_properties([b, a])["URL"] == "https://h.example.com/x"


def test_chained_references_resolve(tmp_path: Path) -> None:
    p = props(tmp_path, "a.properties", "A=1\nB=~{$A}~2\nC=~{$B}~3\n")
    assert load_properties([p])["C"] == "123"


def test_later_file_overrides_earlier_by_raw_key(tmp_path: Path) -> None:
    base = props(tmp_path, "base.properties", "ENVID=BASE\nKEEP=1\n")
    over = props(tmp_path, "dev1.properties", "ENVID=DEV1\n")
    assert load_properties([base, over]) == {"ENVID": "DEV1", "KEEP": "1"}


def test_within_file_duplicate_key_is_error(tmp_path: Path) -> None:
    p = props(tmp_path, "a.properties", "X=1\nX=2\n")
    with pytest.raises(PlaceholderError, match="duplicate key 'X'"):
        load_properties([p])


def test_resolved_key_collision_is_error(tmp_path: Path) -> None:
    """HOST_~{$ENVID}~ and HOST_DEV1 are different raw keys resolving to the
    same name: collision, not layering."""
    p = props(tmp_path, "a.properties", "ENVID=DEV1\nHOST_~{$ENVID}~=a\nHOST_DEV1=b\n")
    with pytest.raises(PlaceholderError, match="collision, not layering"):
        load_properties([p])


def test_cycle_is_loud_listing_stuck_entries(tmp_path: Path) -> None:
    p = props(tmp_path, "a.properties", "A=~{$B}~\nB=~{$A}~\n")
    with pytest.raises(PlaceholderError) as exc_info:
        load_properties([p])
    message = str(exc_info.value)
    assert "a.properties:1" in message and "a.properties:2" in message
    assert "undefined name or reference cycle" in message


def test_undefined_reference_in_properties_is_loud(tmp_path: Path) -> None:
    p = props(tmp_path, "a.properties", "A=~{$NOWHERE}~\n")
    with pytest.raises(PlaceholderError, match=r"\['NOWHERE'\]"):
        load_properties([p])


def test_non_identifier_resolved_key_is_error(tmp_path: Path) -> None:
    p = props(tmp_path, "a.properties", "ENVID=DEV-1\nHOST_~{$ENVID}~=x\n")
    with pytest.raises(PlaceholderError, match="not identifier-shaped"):
        load_properties([p])


def test_no_equals_line_is_error(tmp_path: Path) -> None:
    p = props(tmp_path, "a.properties", "just some text\n")
    with pytest.raises(PlaceholderError, match="no '='"):
        load_properties([p])


def test_value_may_contain_equals_and_comments_are_ignored(tmp_path: Path) -> None:
    p = props(
        tmp_path,
        "a.properties",
        "# comment\n! also a comment\n\nQUERY=a=b=c\n  SPACED  =  padded value  \n",
    )
    assert load_properties([p]) == {"QUERY": "a=b=c", "SPACED": "padded value"}


# ------------------------------------------------------------------- end to end


def test_resolved_dl18_fixture_lowers_with_concrete_names(tmp_path: Path) -> None:
    """The tool's purpose: the templated DL-18 corpus fixture, resolved with
    an environment overlay, flows through the ordinary pipeline with fully
    concrete names -- and runtime `${...}` vars survive untouched."""
    p = props(tmp_path, "dev1.properties", "SITE=DEV1\nNODE=core1.example.com\n")
    text = (CORPUS_DIR / "sem24_status_resource.jil").read_bytes().decode("utf-8")
    resolved, reports = resolve_text(text, [p], file="sem24_status_resource.jil")
    assert reports == []
    catalog = lower_source(resolved, file="resolved.jil")
    assert "ETL_DEV1_LOAD_C" in catalog.jobs
    assert catalog.jobs["ETL_DEV1_NIGHT_SB"].sem.initial_status == "ON_HOLD"
    assert set(catalog.machines) == {"DEV1_RM_CORE", "DEV1_VM_ETL"}
    assert catalog.machines["DEV1_RM_CORE"].attrs["node_name"] == "core1.example.com"
    assert set(catalog.resources) == {"ETL_DEV1_IMPORT_LOCK", "ETL_DEV1_SLOT_POOL"}
    refs = catalog.jobs["ETL_DEV1_LOAD_C"].resources
    assert [(r.name, r.quantity) for r in refs] == [
        ("ETL_DEV1_IMPORT_LOCK", 1),
        ("ETL_DEV1_SLOT_POOL", 2),
    ]
    exec_ = catalog.jobs["ETL_DEV1_LOAD_C"].exec_
    assert exec_ is not None and exec_.std_out_file is not None
    assert "${AUTO_JOB_NAME}" in exec_.std_out_file


def test_dl22_typed_lane_start_times_is_the_motivating_case(tmp_path: Path) -> None:
    """A templated start_times value must refuse to lower raw (typed lane,
    DL-19: the core never learns templating) and lower cleanly after
    preprocessing."""
    from dsl41.ir import LoweringError

    text = (
        "insert_job: nightly\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "~{$LOAD_START}~"\n'
    )
    with pytest.raises(LoweringError, match="invalid time"):
        lower_source(text)
    p = props(tmp_path, "env.properties", "LOAD_START=04:20\n")
    resolved, _ = resolve_text(text, [p])
    catalog = lower_source(resolved)
    schedule = catalog.jobs["nightly"].schedule
    assert schedule is not None and schedule.start_times is not None
    assert [(t.hour, t.minute) for t in schedule.start_times] == [(4, 20)]


# -------------------------------------------------------------------------- CLI


def test_cli_resolve_stdout(tmp_path: Path) -> None:
    p = props(tmp_path, "env.properties", "ENVID=DEV1\n")
    jil = tmp_path / "in.jil"
    jil.write_text("insert_job: APP_~{$ENVID}~_A\njob_type: c\ncommand: x\n", encoding="utf-8")
    result = runner.invoke(app, ["resolve", str(jil), "-p", str(p)])
    assert result.exit_code == 0
    assert "insert_job: APP_DEV1_A" in result.stdout


def test_cli_resolve_layered_files_to_output(tmp_path: Path) -> None:
    base = props(tmp_path, "base.properties", "ENVID=BASE\n")
    over = props(tmp_path, "dev1.properties", "ENVID=DEV1\n")
    jil = tmp_path / "in.jil"
    jil.write_text("insert_job: J_~{$ENVID}~\r\njob_type: c\r\n", encoding="utf-8")
    out = tmp_path / "out.jil"
    result = runner.invoke(
        app, ["resolve", str(jil), "-p", str(base), "-p", str(over), "-o", str(out)]
    )
    assert result.exit_code == 0
    assert out.read_bytes() == b"insert_job: J_DEV1\r\njob_type: c\r\n"


def test_cli_resolve_undefined_token_exits_2(tmp_path: Path) -> None:
    p = props(tmp_path, "env.properties", "ENVID=DEV1\n")
    jil = tmp_path / "in.jil"
    jil.write_text("insert_job: ~{$MISSING}~\n", encoding="utf-8")
    result = runner.invoke(app, ["resolve", str(jil), "-p", str(p)])
    assert result.exit_code == 2


def test_cli_resolve_permit_unresolved_exits_0(tmp_path: Path) -> None:
    p = props(tmp_path, "env.properties", "ENVID=DEV1\n")
    jil = tmp_path / "in.jil"
    jil.write_text("insert_job: ~{$MISSING}~\n", encoding="utf-8")
    result = runner.invoke(app, ["resolve", str(jil), "-p", str(p), "--permit-unresolved"])
    assert result.exit_code == 0


def test_cli_resolve_merges_multiple_files_in_order(tmp_path: Path) -> None:
    """DL-22: several inputs concatenate in argument order; a missing final
    newline between inputs is completed so statements never fuse."""
    p = props(tmp_path, "env.properties", "ENVID=DEV1\n")
    a = tmp_path / "a.jil"
    a.write_bytes(b"insert_job: A_~{$ENVID}~\njob_type: c\ncommand: x\nmachine: m1")  # no final NL
    b = tmp_path / "b.jil"
    b.write_bytes(b"insert_job: B_~{$ENVID}~\njob_type: c\ncommand: y\nmachine: m1\n")
    out = tmp_path / "merged.jil"
    result = runner.invoke(app, ["resolve", str(a), str(b), "-p", str(p), "-o", str(out)])
    assert result.exit_code == 0
    merged = out.read_text(encoding="utf-8")
    assert "insert_job: A_DEV1\n" in merged
    assert "machine: m1\ninsert_job: B_DEV1\n" in merged  # completed newline, no fusing
    catalog = lower_source(merged)
    assert set(catalog.jobs) == {"A_DEV1", "B_DEV1"}


def test_cli_resolve_refuses_mixing_lf_and_crlf_inputs(tmp_path: Path) -> None:
    p = props(tmp_path, "env.properties", "ENVID=DEV1\n")
    lf = tmp_path / "lf.jil"
    lf.write_bytes(b"insert_job: a\njob_type: c\ncommand: x\nmachine: m1\n")
    crlf = tmp_path / "crlf.jil"
    crlf.write_bytes(b"insert_job: b\r\njob_type: c\r\ncommand: x\r\nmachine: m1\r\n")
    result = runner.invoke(app, ["resolve", str(lf), str(crlf), "-p", str(p)])
    assert result.exit_code == 2


def test_cli_lint_with_properties_processes_templated_estate_in_one_step(tmp_path: Path) -> None:
    """DL-22 end to end: two templated JILs + properties -> one lint run.
    Without -p the templated start_times is an exit-2 lowering refusal."""
    p = props(tmp_path, "env.properties", "ENVID=DEV1\nLOAD_START=04:20\n")
    a = tmp_path / "seed.jil"
    a.write_text(
        "insert_job: SEED_~{$ENVID}~\njob_type: c\ncommand: x\nmachine: m1\n"
        'date_conditions: 1\ndays_of_week: all\nstart_times: "~{$LOAD_START}~"\n',
        encoding="utf-8",
    )
    b = tmp_path / "load.jil"
    b.write_text(
        "insert_job: LOAD_~{$ENVID}~\njob_type: c\ncommand: y\nmachine: m1\n"
        "condition: s(SEED_~{$ENVID}~)\n",
        encoding="utf-8",
    )
    without = runner.invoke(app, ["lint", str(a), str(b)])
    assert without.exit_code == 2
    result = runner.invoke(app, ["lint", str(a), str(b), "-p", str(p)])
    assert result.exit_code == 0, result.output
