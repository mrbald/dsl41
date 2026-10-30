"""The idempotence declaration, the invocation environment and the lint rules over them (DL-424).

The loader reads `ESTATE_DIR/non-idempotent.toml`. L024 and L025 take the loaded exceptions
through `lint_catalog(exceptions=...)`; `dsl41 lint --manifest` loads the sidecar from the
manifest's directory and bare JIL files pass None.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41.canon import canonical_bytes
from dsl41.cli import app
from dsl41.idempotence import (
    ENV_ATTEMPT,
    ENV_JOB,
    ENV_RUN_NUMBER,
    ENV_SCHEDULED_AT,
    EXCEPTIONS_FILE,
    IdempotenceError,
    invocation_env,
    load_exceptions,
)

runner = CliRunner()

JIL = (
    "insert_job: ledger_post\njob_type: c\ncommand: x\nmachine: m1\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "10:00"\n\n'
    "insert_job: other\njob_type: c\ncommand: y\nmachine: m1\n"
    'date_conditions: 1\ndays_of_week: all\nstart_times: "11:00"\n'
)


def _write(directory: Path, text: str) -> None:
    (directory / EXCEPTIONS_FILE).write_text(text, encoding="utf-8")


# ------------------------------------------------------------------------------ the loader


def test_an_absent_file_makes_every_job_idempotent(tmp_path: Path) -> None:
    found = load_exceptions(tmp_path)
    assert found.names == ()
    assert found.is_idempotent("anything")
    assert found.reason("anything") is None


def test_a_well_formed_file_marks_its_jobs_with_their_reasons(tmp_path: Path) -> None:
    _write(
        tmp_path,
        '# the ledger\n[jobs]\n"emea.ledger_post" = "posts to the GL twice if rerun"\n'
        'plain = "writes a file once"\n',
    )
    found = load_exceptions(tmp_path)
    assert found.names == ("emea.ledger_post", "plain")
    assert not found.is_idempotent("emea.ledger_post")
    assert found.is_idempotent("other")
    assert found.reason("emea.ledger_post") == "posts to the GL twice if rerun"


def test_a_file_with_no_jobs_table_is_empty(tmp_path: Path) -> None:
    _write(tmp_path, "# nothing marked yet\n")
    assert load_exceptions(tmp_path).names == ()


def test_malformed_toml_names_the_file_and_the_line(tmp_path: Path) -> None:
    _write(tmp_path, '[jobs]\n"a" = "fine"\n"b" = \n')
    with pytest.raises(IdempotenceError, match=r"non-idempotent\.toml.*line 3"):
        load_exceptions(tmp_path)


def test_a_non_string_reason_names_the_line(tmp_path: Path) -> None:
    _write(tmp_path, '[jobs]\n"a" = "fine"\n"b" = 7\n')
    with pytest.raises(IdempotenceError, match=r"non-idempotent\.toml:3: .*'b'.*string.*int"):
        load_exceptions(tmp_path)


@pytest.mark.parametrize("reason", ['""', '"   "'])
def test_an_empty_reason_names_the_line(tmp_path: Path, reason: str) -> None:
    _write(tmp_path, f'[jobs]\n"a" = "fine"\n"b" = {reason}\n')
    with pytest.raises(IdempotenceError, match=r"non-idempotent\.toml:3: .*'b'.*empty"):
        load_exceptions(tmp_path)


def test_a_bare_dotted_name_is_a_table_and_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, '[jobs]\nemea.ledger_post = "posts twice"\n')
    with pytest.raises(IdempotenceError, match=r"non-idempotent\.toml:2: .*'emea'.*quote"):
        load_exceptions(tmp_path)


def test_an_unknown_top_level_key_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, 'extra = "x"\n[jobs]\n')
    with pytest.raises(IdempotenceError, match=r"non-idempotent\.toml:1: .*'extra'"):
        load_exceptions(tmp_path)


def test_a_multi_line_reason_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, '[jobs]\n"a" = "one\\ntwo"\n')
    with pytest.raises(IdempotenceError, match=r"non-idempotent\.toml:2: .*one line"):
        load_exceptions(tmp_path)


def test_a_dangling_symlink_is_not_an_absent_file(tmp_path: Path) -> None:
    (tmp_path / EXCEPTIONS_FILE).symlink_to(tmp_path / "gone.toml")
    with pytest.raises(IdempotenceError, match="cannot read"):
        load_exceptions(tmp_path)


def test_a_symlink_loop_is_not_an_absent_file(tmp_path: Path) -> None:
    (tmp_path / EXCEPTIONS_FILE).symlink_to(EXCEPTIONS_FILE)
    with pytest.raises(IdempotenceError, match="cannot read"):
        load_exceptions(tmp_path)


@pytest.mark.parametrize(
    "separator", ["\\r", "\\u000b", "\\u000c", "\\u001c", "\\u0085", "\\u2028", "\\u2029"]
)
def test_every_line_terminator_in_a_reason_is_refused(tmp_path: Path, separator: str) -> None:
    _write(tmp_path, f'[jobs]\n"a" = "one{separator}two"\n')
    with pytest.raises(IdempotenceError, match=r"non-idempotent\.toml:2: .*one line"):
        load_exceptions(tmp_path)


def test_a_trailing_line_terminator_in_a_reason_is_refused(tmp_path: Path) -> None:
    _write(tmp_path, '[jobs]\n"a" = "one\\n"\n')
    with pytest.raises(IdempotenceError, match="one line"):
        load_exceptions(tmp_path)


def test_a_file_that_cannot_be_read_is_an_error(tmp_path: Path) -> None:
    (tmp_path / EXCEPTIONS_FILE).mkdir()
    with pytest.raises(IdempotenceError, match="cannot read"):
        load_exceptions(tmp_path)


def test_canonical_bytes_do_not_depend_on_key_order(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _write(first, '[jobs]\n"a" = "x"\n"b" = "y"\n')
    _write(second, '[jobs]\n"b" = "y"\n"a" = "x"\n')
    assert load_exceptions(first).canonical_bytes() == load_exceptions(second).canonical_bytes()
    assert load_exceptions(first).canonical_bytes() == canonical_bytes(
        {"jobs": {"a": "x", "b": "y"}}
    )


def test_canonical_bytes_bind_the_reasons(tmp_path: Path) -> None:
    _write(tmp_path, '[jobs]\n"a" = "x"\n')
    before = load_exceptions(tmp_path).canonical_bytes()
    _write(tmp_path, '[jobs]\n"a" = "z"\n')
    assert load_exceptions(tmp_path).canonical_bytes() != before


def test_an_absent_file_canonicalises_to_an_empty_table(tmp_path: Path) -> None:
    assert load_exceptions(tmp_path).canonical_bytes() == canonical_bytes({"jobs": {}})
    _write(tmp_path, "[jobs]\n")
    assert load_exceptions(tmp_path).canonical_bytes() == canonical_bytes({"jobs": {}})


# ------------------------------------------------------------------------- the environment


def test_invocation_env_has_the_four_variables() -> None:
    env = invocation_env("emea.ledger_post", 7, datetime(2026, 10, 30, 1, 2, 3, tzinfo=UTC), 2)
    assert env == {
        ENV_JOB: "emea.ledger_post",
        ENV_RUN_NUMBER: "7",
        ENV_SCHEDULED_AT: "2026-10-30T01:02:03Z",
        ENV_ATTEMPT: "2",
    }
    assert (ENV_JOB, ENV_RUN_NUMBER, ENV_SCHEDULED_AT, ENV_ATTEMPT) == (
        "DSL41_JOB",
        "DSL41_RUN_NUMBER",
        "DSL41_SCHEDULED_AT",
        "DSL41_ATTEMPT",
    )


def test_scheduled_at_in_another_zone_is_converted_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    env = invocation_env("j", 1, datetime(2026, 10, 30, 3, 0, 0, tzinfo=plus_two), "a1")
    assert env[ENV_SCHEDULED_AT] == "2026-10-30T01:00:00Z"
    assert env[ENV_ATTEMPT] == "a1"


def test_scheduled_at_drops_the_fraction_and_reads_naive_as_utc() -> None:
    env = invocation_env("j", 1, datetime(2026, 10, 30, 1, 2, 3, 987654), 1)
    assert env[ENV_SCHEDULED_AT] == "2026-10-30T01:02:03Z"


def test_a_rerun_keeps_three_fields_and_changes_the_attempt() -> None:
    at = datetime(2026, 10, 30, 1, 0, tzinfo=UTC)
    first = invocation_env("j", 4, at, 1)
    again = invocation_env("j", 4, at, 2)
    assert {k: v for k, v in first.items() if k != ENV_ATTEMPT} == {
        k: v for k, v in again.items() if k != ENV_ATTEMPT
    }
    assert first[ENV_ATTEMPT] != again[ENV_ATTEMPT]


# --------------------------------------------------------------------------------- the CLI


def _estate(directory: Path) -> Path:
    (directory / "estate.jil").write_text(JIL, encoding="utf-8")
    manifest = directory / "estate.toml"
    manifest.write_text('name = "t"\nfiles = ["estate.jil"]\n', encoding="utf-8")
    return manifest


def test_lint_manifest_reads_the_sidecar(tmp_path: Path) -> None:
    manifest = _estate(tmp_path)
    _write(tmp_path, '[jobs]\nledger_post = "posts twice"\nghost = "r"\n')
    result = runner.invoke(app, ["lint", "--manifest", str(manifest)])
    assert "L024 info" in result.stdout
    assert "L025 error" in result.stdout
    assert result.exit_code == 1


def test_lint_manifest_strict_passes_with_only_the_info(tmp_path: Path) -> None:
    manifest = _estate(tmp_path)
    _write(tmp_path, '[jobs]\nledger_post = "posts twice"\n')
    result = runner.invoke(app, ["lint", "--manifest", str(manifest), "--strict"])
    assert "L024 info" in result.stdout
    assert result.exit_code == 0


def test_lint_manifest_strict_fails_on_an_unknown_name(tmp_path: Path) -> None:
    manifest = _estate(tmp_path)
    _write(tmp_path, '[jobs]\nghost = "r"\n')
    result = runner.invoke(app, ["lint", "--manifest", str(manifest), "--strict"])
    assert "L025 error" in result.stdout
    assert result.exit_code == 1


def test_lint_manifest_without_the_sidecar_is_quiet(tmp_path: Path) -> None:
    manifest = _estate(tmp_path)
    result = runner.invoke(app, ["lint", "--manifest", str(manifest)])
    assert "L024" not in result.stdout
    assert result.exit_code == 0


def test_lint_of_bare_files_ignores_a_sidecar_beside_them(tmp_path: Path) -> None:
    _estate(tmp_path)
    _write(tmp_path, '[jobs]\nghost = "r"\n')
    result = runner.invoke(app, ["lint", str(tmp_path / "estate.jil")])
    assert "L024" not in result.stdout
    assert "L025" not in result.stdout
    assert result.exit_code == 0


def test_lint_manifest_with_a_malformed_sidecar_exits_2(tmp_path: Path) -> None:
    manifest = _estate(tmp_path)
    _write(tmp_path, '[jobs]\nledger_post = ""\n')
    result = runner.invoke(app, ["lint", "--manifest", str(manifest)])
    assert result.exit_code == 2


def test_suppress_accepts_the_new_codes(tmp_path: Path) -> None:
    manifest = _estate(tmp_path)
    _write(tmp_path, '[jobs]\nghost = "r"\n')
    result = runner.invoke(app, ["lint", "--manifest", str(manifest), "--suppress", "L025"])
    assert result.exit_code == 0
