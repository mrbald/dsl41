"""Operator layouts share prepared data and preserve command outcomes (DL-482)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dsl41 import cli_control, cli_reload
from dsl41.cli import app
from dsl41.cli_output import OutputStyle, prepare_status, status_lines
from dsl41.ir import CatalogIR, lower_source
from dsl41.reload_plan import Live, estate_settings
from dataclasses import replace
from dsl41 import main_config
from test_main_config import estate_file
from dsl41.reload_update import Admitted, Application, Refused
from dsl41.reread_worker import preview

pytestmark = pytest.mark.usefixtures("cli_env", "local_timezone")
runner = CliRunner()


@pytest.fixture(params=list(OutputStyle))
def style(request):
    return request.param


def invoke(command, style, *args):
    return runner.invoke(
        app, [*command, *args, "--style", style.value, "-S", "/synthetic/control.sock"]
    )


def test_status_counts_partition_jobs_and_flags_overlap(style):
    states = ["RUNNING", "STARTING", "QUE_WAIT", "FAILURE", "TERMINATED", "SUCCESS", "INACTIVE"]
    answer = {
        "ok": True,
        "engine": "RUNNING",
        "config_rev": 1,
        "spec_drift": True,
        "jobs": {
            f"j{i}": {
                "status": state,
                "status_at": None,
                "on_hold": True,
                "on_ice": True,
                "on_noexec": True,
                "armed": True,
            }
            for i, state in enumerate(states)
        },
    }
    answer["jobs"]["j5"]["attempt"] = {"state": "STOPPING", "n": 1, "cause": "update_stop"}
    original = copy.deepcopy(answer)
    view = prepare_status(answer)
    assert sum(count for _, count in view.counts) == len(states)
    assert dict(view.counts) == {
        "running": 2,
        "queued": 1,
        "failed": 2,
        "success": 1,
        "inactive": 1,
    }
    assert all(len(names) == 7 for _, names in view.flags)
    text = "\n".join(status_lines(answer, style))
    assert "flags: on ice 7   on hold 7   noexec 7   armed 7" in text
    assert "STOPPING" in text
    assert "run dsl41 reread" not in text
    assert answer == original


@pytest.mark.parametrize(
    "engine,drift,pending",
    [
        ("RUNNING", False, False),
        ("RUNNING", True, False),
        ("DRAINING", None, True),
        ("STARTING", False, False),
        ("STOPPED", False, False),
    ],
)
def test_status_lifecycle_empty_legacy_and_pending(monkeypatch, style, engine, drift, pending):
    answer = {"ok": True, "engine": engine, "jobs": {}, "spec_drift": drift}
    if pending:
        answer["update"] = {"stopping": ["j"]}
    monkeypatch.setattr(cli_control, "_control_roundtrip", lambda *a: answer)
    result = invoke(["query", "status"], style)
    assert result.exit_code == 0, result.output
    assert f"engine {engine}" in result.stdout and "no jobs" in result.stdout
    assert ("update pending" in result.stdout) == pending
    assert "run dsl41 reread" not in result.stdout


@pytest.mark.parametrize("held", [False, True])
def test_status_clock_hold_notice_and_json(monkeypatch, style, held):
    answer = {
        "ok": True,
        "engine": "RUNNING",
        "jobs": {},
        "spec_drift": False,
        "update": {"stopping": ["j"]},
    }
    if held:
        answer["clock_hold"] = {"wall": "2026-07-01T08:02:00", "until": "2026-07-01T08:05:00"}
    monkeypatch.setattr(cli_control, "_control_roundtrip", lambda *a: answer)
    result = invoke(["query", "status"], style)
    assert result.exit_code == 0, result.output
    notice = (
        "wall clock 2026-07-01T10:02:00 is behind engine time; "
        "ticks and timers resume at 2026-07-01T10:05:00"
    )
    assert (notice in result.stdout) == held
    if held:
        assert "update pending; waiting for: j\n" + notice in result.stdout
    raw = runner.invoke(app, ["query", "status", "--json", "-S", "/synthetic/control.sock"])
    assert raw.exit_code == 0
    assert json.loads(raw.stdout) == answer


@pytest.mark.parametrize("command", [["query", "status"], ["reread"], ["update"]])
def test_unreachable_keeps_transport_error(monkeypatch, style, command):
    error = "dsl41: no engine is running at /synthetic/control.sock"

    def fail(*a):
        if command[0] == "query":
            raise __import__("typer").Exit(cli_control.refuse(error))
        raise cli_reload.SendFailed(error)

    monkeypatch.setattr(cli_control, "_control_roundtrip", fail)
    monkeypatch.setattr(cli_reload, "socket_send", lambda *a, **kw: fail)
    result = invoke(command, style)
    assert result.exit_code == 2 and result.stdout == ""
    assert result.stderr == error + "\n"


@pytest.mark.parametrize(
    "error",
    [
        "broken input",
        "This engine was started without a main config. Drain it and start on a fresh run root to change definitions.",
        "old engine",
    ],
)
def test_refused_answers_keep_one_failure_owner(monkeypatch, style, error):
    monkeypatch.setattr(cli_control, "_control_roundtrip", lambda *a: {"ok": False, "error": error})
    result = invoke(["query", "status"], style)
    assert result.exit_code == 2 and result.stderr == error + "\n" and not result.stdout
    monkeypatch.setattr(
        cli_reload, "socket_send", lambda *a, **kw: lambda *a: {"ok": False, "error": error}
    )
    result = invoke(["reread"], style)
    assert result.exit_code == 2 and result.stderr == f"reread: no diff available: {error}\n"

    def fail(*a):
        return cli_reload.update_reply_of(
            {"ok": False, "refused": True, "code": "invalid_argument", "error": error}
        )

    monkeypatch.setattr(cli_reload, "socket_send", lambda *a, **kw: fail)
    result = invoke(["update"], style)
    assert result.exit_code == 2 and error in result.stderr


def test_full_job_alias_conflict_json_and_line_escaping(monkeypatch, style):
    answer = {
        "ok": True,
        "jobs": {
            "j": {
                "status": "FAILURE",
                "status_at": "2026-07-01T23:30:00",
                "started_by": "first\nsecond\rthird",
                "log_err": "/a\nb",
                "pending_timers": [],
                "state_rev": 3,
            }
        },
        "config_rev": 1,
        "engine": "RUNNING",
    }
    requests = []

    def send(path, request):
        requests.append(request)
        return answer

    monkeypatch.setattr(cli_control, "_control_roundtrip", send)
    result = invoke(["query", "status", "j"], style, "--job", "j")
    assert result.exit_code == 0, result.output
    assert "jobs.j.status_at: 2026-07-02T01:30:00" in result.stdout
    assert "first\\nsecond\\rthird" in result.stdout and "\nsecond" not in result.stdout
    assert "jobs.j.pending_timers: []" in result.stdout and "jobs.j.state_rev: 3" in result.stdout
    result = invoke(["query", "status", "j"], style, "--job", "other")
    assert result.exit_code == 2 and len(requests) == 1
    raw = runner.invoke(app, ["query", "status", "--json", "-S", "/synthetic/control.sock"])
    assert raw.stdout == json.dumps(answer, sort_keys=True) + "\n"


def test_brief_is_exactly_the_neutral_renderer(monkeypatch):
    answer = {"ok": True, "jobs": {"j": {"status": "INACTIVE"}}}
    monkeypatch.setattr(cli_control, "_control_roundtrip", lambda *a: answer)
    brief = runner.invoke(app, ["query", "status", "--brief", "-S", "/synthetic/control.sock"])
    neutral = invoke(["query", "status"], OutputStyle.neutral)
    assert brief.exit_code == neutral.exit_code == 0
    assert brief.stdout == neutral.stdout


def candidate_answer(tmp_path, *, changed=True, running=False, settings_only=False, size=1):
    text = "".join(
        f"insert_job: j{i}\njob_type: c\ncommand: true\nmachine: localhost\ndate_conditions: 1\nstart_mins: 0\n"
        for i in range(size)
    )
    old = lower_source(text)
    new = lower_source(text.replace("true", "false") if changed and not settings_only else text)
    prepared = main_config.prepare(estate_file(tmp_path))
    candidate = replace(
        prepared,
        catalog=new,
        settings={
            **estate_settings("UTC", None, {}, new),
            **({"job_env": {"K": "V"}} if settings_only else {}),
        },
    )
    live = Live(
        old,
        estate_settings("UTC", None, {}, old),
        1,
        {name: "RUNNING" for name in old.jobs} if running else {},
        launch=candidate.metadata["launch"],
    )
    return {
        "ok": True,
        "live_rev": 1,
        "source": {"path": "/synthetic/estate.jil"},
        "preview": preview(candidate, live),
    }


@pytest.mark.parametrize(
    "changed,running,settings_only",
    [(False, False, False), (True, False, False), (True, True, False), (True, False, True)],
)
def test_reread_cases(monkeypatch, style, changed, running, settings_only, tmp_path):
    answer = candidate_answer(
        tmp_path, changed=changed, running=running, settings_only=settings_only
    )
    monkeypatch.setattr(cli_reload, "socket_send", lambda *a, **kw: lambda *a: answer)
    result = invoke(["reread"], style)
    assert result.exit_code == int(running), result.output
    assert ("stopping 1 running job" in result.stdout) == running
    assert ("no job or setting changes" in result.stdout) == (not changed and not settings_only)
    assert "nothing changed" not in result.stdout
    assert "apply with:" in result.stdout
    if settings_only:
        assert "estate setting job_env" in result.stdout


@pytest.mark.parametrize(
    "summary",
    [None, [], ["L002 info: 2 findings", "full list: dsl41 lint --config /synthetic/main.toml"]],
)
def test_reread_prints_summary_or_older_engine_messages_verbatim(
    monkeypatch, style, summary, tmp_path
):
    answer = candidate_answer(tmp_path)
    view = answer["preview"]
    view.pop("lint_summary", None)
    view["lint_messages"] = [
        "lint policy: refuse=warn; ignore=none",
        "/tmp/a:1: L002 info: x.jil:5: L001 error: full detail",
        "preflight ERROR [machine] A: foreign:1: L002 info: node",
    ]
    view["diagnostics"] = ["unmatched missing*:1: L002 info: x.jil"]
    if summary is not None:
        view["lint_summary"] = summary
    original = copy.deepcopy(answer)
    monkeypatch.setattr(cli_reload, "socket_send", lambda *a, **kw: lambda *a: answer)
    result = invoke(["reread"], style)
    assert result.exit_code == 0, result.output
    assert result.stderr.splitlines() == [
        *(view["lint_messages"] if summary is None else summary),
        *view["diagnostics"],
    ]
    assert answer == original


@pytest.mark.parametrize("running", [False, True])
def test_reread_grouping_240_jobs(monkeypatch, style, running, tmp_path):
    answer = candidate_answer(tmp_path, size=240, running=running)
    monkeypatch.setattr(cli_reload, "socket_send", lambda *a, **kw: lambda *a: answer)
    result = invoke(["reread"], style)
    assert result.exit_code == int(running)
    assert "changed 240 jobs" in result.stdout
    assert result.stdout.count("command") == (1 if style is OutputStyle.focused else 240)
    if style is OutputStyle.focused:
        assert "240 jobs (all)" in result.stdout


@pytest.mark.parametrize(
    "blocked", [[], ["host launch settings changed"], ["blocking lint"], ["preflight ERROR"]]
)
def test_reread_advice_only_with_shared_admission_success(monkeypatch, style, blocked, tmp_path):
    answer = candidate_answer(tmp_path)
    answer["preview"]["definition_changes"]["blocked"] = blocked
    monkeypatch.setattr(cli_reload, "socket_send", lambda *a, **kw: lambda *a: answer)
    result = invoke(["reread"], style)
    assert ("apply with:" in result.stdout) == (not blocked)
    assert ("update would refuse:" in result.stdout) == bool(blocked)


@pytest.mark.parametrize(
    "case", ["unchanged", "settings", "applied", "no_wait", "pending", "refused"]
)
def test_update_cases(monkeypatch, style, case):
    decision = Admitted(
        "", ("j",) if case in {"applied", "no_wait", "pending"} else (), frozenset()
    )
    application = Application(
        "", 2, "hash", "", CatalogIR(), (), () if case == "settings" else ("j",), (), (), ()
    )
    reply = cli_reload.UpdateReply(
        decision,
        application if case in {"settings", "applied"} else None,
        pending=case == "pending",
        request_id="test-id",
        unchanged={"rev": 1, "catalog_hash": "hash"} if case == "unchanged" else None,
    )
    if case == "refused":
        reply = cli_reload.UpdateReply(Refused("", "update_in_progress"))
    monkeypatch.setattr(cli_reload, "socket_send", lambda *a, **kw: lambda *a: reply)
    result = invoke(["update"], style)
    assert result.exit_code == (2 if case == "refused" else 1 if case == "pending" else 0), (
        result.output
    )
    assert ("nothing changed" in result.stdout) == (case == "unchanged")
    if case in {"pending", "no_wait"}:
        assert "--request-id test-id --wait" in result.stdout
    if case == "settings":
        assert "applied: catalog revision 2" in result.stdout
        assert "changed 0 jobs" in result.stdout


def test_every_update_outcome_has_true_text(monkeypatch, style):
    choices = ("complete", "rerun", "hold", "none")
    application = Application(
        "", 2, "h", "", CatalogIR(), (), ("j",), (), (), tuple((kind, kind) for kind in choices)
    )
    reply = cli_reload.UpdateReply(Admitted("", choices, frozenset()), application)
    monkeypatch.setattr(cli_reload, "socket_send", lambda *a, **kw: lambda *a: reply)
    result = invoke(["update"], style)
    assert result.exit_code == 0
    for text in (
        "its own result stands",
        "rerun under the new definition",
        "now ON_HOLD",
        "stopped, no follow-up of its own",
    ):
        assert text in result.stdout


def test_update_groups_240_identical_outcomes(monkeypatch, style):
    names = tuple(f"job_{index:03}" for index in range(240))
    application = Application(
        "", 2, "h", "", CatalogIR(), (), names, (), (), tuple((name, "rerun") for name in names)
    )
    reply = cli_reload.UpdateReply(Admitted("", names, frozenset()), application)
    monkeypatch.setattr(cli_reload, "socket_send", lambda *a, **kw: lambda *a: reply)
    result = invoke(["update"], style)
    assert result.exit_code == 0
    assert result.stdout.count("stopped, rerun under the new definition") == (
        1 if style is OutputStyle.focused else 240
    )
    assert "changed 240 jobs" in result.stdout


@pytest.mark.parametrize(
    "before,after,shown",
    [
        ([1], [2], True),
        (list(range(100)), list(range(101)), False),
        ({"kind": "status", "name": "a"}, {"kind": "status", "name": "b"}, True),
    ],
)
def test_structured_changes_use_record_labels_and_only_fitting_values(before, after, shown):
    from dsl41.cli_output import change_text
    from dsl41.reload_diff import difference_view, record_difference

    # A condition tree is one field, with no reverse map to source attributes.
    changes = record_difference({"cond": before}, {"cond": after}, ("job", "sem", "condition"))
    assert len(changes) == 1
    text = change_text(difference_view(changes[0]))
    assert text.startswith("condition: changed")
    assert (" -> " in text) == shown


def test_indirect_reference_change_in_each_layout(monkeypatch, style, tmp_path):
    text = "insert_machine: localhost\ntype: a\nnode_name: old\n\ninsert_job: j\njob_type: c\ncommand: true\nmachine: localhost\n"
    old, new = lower_source(text), lower_source(text.replace("node_name: old", "node_name: new"))
    settings = estate_settings("UTC", None, {}, old)
    candidate = replace(main_config.prepare(estate_file(tmp_path)), catalog=new, settings=settings)
    answer = {
        "ok": True,
        "live_rev": 1,
        "source": {"path": "/synthetic/estate.jil"},
        "preview": preview(
            candidate, Live(old, settings, 1, {}, launch=candidate.metadata["launch"])
        ),
    }
    monkeypatch.setattr(cli_reload, "socket_send", lambda *a, **kw: lambda *a: answer)
    result = invoke(["reread"], style)
    assert result.exit_code == 0
    assert "machine localhost: node_name: old -> new" in result.stdout


@pytest.mark.parametrize(
    "flags",
    [["--json", "--style", "focused"], ["--json", "--style", "neutral"], ["--style", "unknown"]],
)
def test_bad_style_combinations_do_not_contact_engine(monkeypatch, flags):
    monkeypatch.setattr(
        cli_control, "_control_roundtrip", lambda *a: pytest.fail("contacted engine")
    )
    result = runner.invoke(app, ["query", "status", *flags, "-S", "/synthetic/control.sock"])
    assert result.exit_code == 2


def test_real_worker_and_application_without_binding_socket(tmp_path, style):
    """The production preparation/application path also drives both renderers."""
    import asyncio
    from datetime import datetime
    from dsl41 import main_config
    from dsl41.cli_output import application_lines, reread_lines
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import VirtualClock
    from dsl41.runner_control import ControlServer
    from dsl41.runner_startup import start_run

    config, jil = tmp_path / "dsl41.toml", tmp_path / "jobs.jil"
    config.write_text(
        f'format_version = 1\n[engine]\nrun_root = "{tmp_path}/run"\n[estate]\ninclude = ["jobs.jil"]\n'
    )
    jil.write_text("insert_job: sample\njob_type: c\ncommand: true\nmachine: localhost\n")
    initial = main_config.prepare(config)
    at = datetime(2026, 7, 1)

    async def scenario():
        engine = start_run(
            initial.catalog,
            tmp_path / "run",
            clock=VirtualClock(start=at),
            adapters={"CMD": FakeAdapter()},
            launched=main_config.profile_of(initial),
            source_capture=main_config.encoded_parts(initial),
        )
        engine.launch_settings = initial.metadata["launch"]
        engine.reread_source = str(config)
        server = ControlServer(
            engine,
            tmp_path / "run" / "control.sock",
            estate_fingerprint=main_config.fingerprint(initial),
        )

        async def drive():
            while True:
                await engine._activity.wait()
                engine._activity.clear()
                await engine.run_until_quiescent(at)

        pump = asyncio.create_task(drive())
        request = {"v": 5, "cmd": "update", "baseline_id": engine.baseline_id, "wait": True}
        try:
            await engine.run_until_quiescent(at)
            jil.write_text(jil.read_text().replace("command: true", "command: false"))
            forecast = await server._reread({"v": 5, "cmd": "reread"})
            assert forecast["ok"], forecast
            groups = forecast["preview"]["definition_changes"]["groups"]
            assert any(
                item["field"] == "command" and item["before"] == "true" and item["after"] == "false"
                for item in groups
            )
            assert "apply with:" in "\n".join(reread_lines(forecast, style, "dsl41 update"))
            answer = await asyncio.wait_for(
                server._update({**request, "request_id": "changed"}), 30
            )
            assert answer.get("application"), answer
            reply = cli_reload.update_reply_of(answer)
            assert "catalog revision 2" in "\n".join(application_lines(reply.application, style))
            identical = await asyncio.wait_for(
                server._update({**request, "request_id": "identical"}), 30
            )
            assert identical["unchanged"]["rev"] == 2
            # A source-only edit still applies; zero changed jobs cannot mean unchanged.
            jil.write_text(jil.read_text() + "\n/* synthetic comment */\n")
            source = await server._reread({"v": 5, "cmd": "reread"})
            assert source["ok"] and not source["preview"]["changed"]
            assert source["preview"]["definition_changes"]["sources_changed"]
            text = "\n".join(reread_lines(source, style, "dsl41 update"))
            assert (
                "estate files changed, but no job or setting did; update still applies them" in text
                and "apply with:" in text
            )
            changed_capture = await asyncio.wait_for(
                server._update({**request, "request_id": "source"}), 30
            )
            assert changed_capture["application"]["rev"] == 3
            assert "unchanged" not in changed_capture
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            await server.close()
            await engine.shutdown()
            engine.journal.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "separator", ["\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
)
def test_unicode_and_ascii_line_breaks_do_not_split_status_values(style, separator):
    value = "first" + separator + "second"
    answer = {
        "ok": True,
        "jobs": {"j": {"status": "FAILURE", "started_by": value, "log_err": value}},
    }
    for full in (False, True):
        lines = status_lines(answer, style, full=full)
        assert all(len(line.splitlines()) <= 1 for line in lines)
        assert all(value not in line for line in lines)


def test_focused_forecasts_are_grouped_and_stopping_is_listed_once():
    jobs = {
        f"job_{i:03}": {
            "status": "INACTIVE" if i < 237 else "FAILURE",
            "reread": {"change": "changed", "action": None},
        }
        for i in range(240)
    }
    lines = status_lines({"jobs": jobs}, OutputStyle.focused)
    assert "reread forecast: changed 237: job_000, job_001, job_002 and 234 more" in lines
    assert len(lines) < 25
    row = {"status": "RUNNING", "attempt": {"state": "STOPPING", "cause": "update_stop", "n": 1}}
    text = "\n".join(status_lines({"jobs": {"stop_job": row}}, OutputStyle.focused))
    assert text.count("stop_job") == 1 and "box -" not in text


def test_filtered_brief_reuses_flags_and_refuses_explicit_focused(monkeypatch):
    row = {"status": "INACTIVE", "job_type": "CMD", "on_hold": True, "armed": True, "state_rev": 3}
    monkeypatch.setattr(
        cli_control, "_control_roundtrip", lambda *a: {"ok": True, "jobs": {"j": row}}
    )
    result = runner.invoke(
        app, ["query", "status", "--brief", "--job", "j", "-S", "/synthetic/control.sock"]
    )
    assert result.exit_code == 0 and len(result.stdout.splitlines()) == 1
    assert result.stdout.split()[-3:] == [cli_control._brief_flags(row), "-", "3"]
    full = invoke(["query", "status", "j"], OutputStyle.focused)
    assert "1 job\n" in full.stdout and "1 jobs" not in full.stdout
    bad = invoke(["query", "status"], OutputStyle.focused, "--brief")
    assert bad.exit_code == 2 and "cannot be combined" in bad.output


@pytest.mark.parametrize("defined", [False, True])
def test_hostname_240_has_exactly_one_difference_line(tmp_path, style, defined):
    from dsl41 import main_config
    from test_main_config import config_file, live_of

    text = "".join(
        f"insert_job: job_{i:03}\njob_type: c\ncommand: true\nmachine: batchhost01\n"
        for i in range(240)
    )
    if defined:
        text += "\ninsert_machine: batchhost01\ntype: a\nnode_name: before\n\ninsert_machine: localhost\ntype: a\nnode_name: after\n"
    path = config_file(tmp_path)
    jil = tmp_path / "a.jil"
    jil.write_text(text)
    before = main_config.prepare(path)
    jil.write_text(
        text.replace("machine: batchhost01\n", "machine: localhost\n").replace(
            "insert_machine: localhost\ntype: a\nnode_name: before",
            "insert_machine: batchhost01\ntype: a\nnode_name: before",
        )
    )
    candidate = main_config.prepare(path)
    answer = {
        "ok": True,
        "live_rev": 1,
        "source": {"path": str(path)},
        "preview": preview(candidate, live_of(before)),
    }
    from dsl41.cli_output import reread_lines

    lines = reread_lines(answer, style, "dsl41 update")
    if style is OutputStyle.focused:
        difference_lines = [line for line in lines if line.startswith("  ")]
        assert difference_lines == ["  machine: batchhost01 -> localhost   240 jobs (all)"]
    else:
        assert len([line for line in lines if "machine: batchhost01 -> localhost" in line]) == 240
    assert "refs." not in "\n".join(lines)


@pytest.mark.parametrize(
    "draining,open_update,preparing",
    [(True, False, False), (False, True, False), (False, False, True)],
)
def test_engine_state_prevents_affirmative_reread_advice(
    style, draining, open_update, preparing, tmp_path
):
    import asyncio
    from types import SimpleNamespace
    from dsl41.runner_control import ControlServer
    from dsl41.cli_output import reread_lines

    server = ControlServer.__new__(ControlServer)
    server.engine = SimpleNamespace(
        draining=draining,
        config_rev=1,
        update=SimpleNamespace(token="open" if open_update else None),
    )
    server._preparing_update = object() if preparing else None

    async def prepare(**kwargs):
        return candidate_answer(tmp_path)

    server._prepare_reload = prepare
    answer = asyncio.run(server._reread({"v": 5, "cmd": "reread"}))
    text = "\n".join(reread_lines(answer, style, "dsl41 update"))
    assert "apply with:" not in text
    assert ("draining" if draining else "another update") in text
    assert "changed 1 job" in text


def test_empty_stop_set_only_claims_what_update_proves(monkeypatch, style):
    # DL-482: a structural box restart can have no process/watch stops.
    application = Application("", 2, "hash", "", CatalogIR(), ("new_child",), (), (), (), ())
    reply = cli_reload.UpdateReply(Admitted("", (), frozenset()), application)
    monkeypatch.setattr(cli_reload, "socket_send", lambda *a, **kw: lambda *a: reply)
    result = invoke(["update"], style)
    assert result.exit_code == 0
    assert "no running job is stopped" not in result.stdout
    assert "no process or watch stops reported" not in result.stdout

    assert "applied: catalog revision 2 (hash)" in result.stdout
    settings = Application("", 3, "new-hash", "", CatalogIR(), (), (), (), (), ())
    monkeypatch.setattr(
        cli_reload,
        "socket_send",
        lambda *a, **kw: lambda *a: cli_reload.UpdateReply(reply.decision, settings),
    )
    result = invoke(["update"], style)
    assert "running box" not in result.stdout and "nothing changed" not in result.stdout


@pytest.mark.parametrize("source_changed", [True, False])
def test_reread_source_only_and_no_change_headers(style, source_changed, tmp_path):
    from dsl41.cli_output import reread_lines

    answer = candidate_answer(tmp_path, changed=False)
    answer["preview"]["definition_changes"]["sources_changed"] = source_changed
    text = "\n".join(reread_lines(answer, style, "dsl41 update"))
    if source_changed:
        assert "estate files changed, but no job or setting did; update still applies them" in text
    else:
        assert text.startswith("no job, setting or estate file changed\n")
    changed = candidate_answer(tmp_path)
    changed["preview"]["definition_changes"]["sources_changed"] = True
    assert "estate files changed, but" not in "\n".join(
        reread_lines(changed, style, "dsl41 update")
    )


def test_read_from_line_is_bounded(style, tmp_path):
    from dsl41.cli_output import reread_lines

    answer = candidate_answer(tmp_path)
    answer["preview"]["source_changes"] = [
        {"job": f"j{i}", "before": None, "after": "/synthetic/" + "x" * 100 + str(i)}
        for i in range(30)
    ]
    line = next(
        line
        for line in reread_lines(answer, style, "dsl41 update")
        if line.startswith("read from:")
    )
    assert len(line) <= 240 and line.endswith("[cut]")


def test_source_only_refusal_does_not_claim_update_applies_it(style, tmp_path):
    from dsl41.cli_output import reread_lines

    answer = candidate_answer(tmp_path, changed=False)
    details = answer["preview"]["definition_changes"]
    details.update(sources_changed=True, blocked=["the engine is draining"])
    text = "\n".join(reread_lines(answer, style, "dsl41 update"))
    assert "update still applies" not in text and "apply with:" not in text
    assert "engine is draining" in text


def test_hash_affecting_setting_is_described_once_without_a_job_list(style, tmp_path):
    from dsl41.cli_output import reread_lines

    answer = candidate_answer(tmp_path, changed=False)
    answer["preview"]["changed"] = [{"name": "j0", "reason": "estate setting timezone changed"}]
    answer["preview"]["unchanged"] = 0
    answer["preview"]["definition_changes"].update(
        settings_changed=True,
        groups=[
            {
                "field": "estate setting timezone",
                "scope": "settings",
                "before": "UTC",
                "after": "Europe/Zurich",
                "complex": False,
                "redacted": False,
                "jobs": [],
            }
        ],
    )
    text = "\n".join(reread_lines(answer, style, "dsl41 update"))
    assert text.count("estate setting timezone") == 1
    assert "affected by shared estate settings" in text


@pytest.mark.parametrize("stops", [(), ("a",), ("a", "b")])
def test_r2_stop_line_is_absent_or_names_only_reported_stops(monkeypatch, style, stops):
    reply = cli_reload.UpdateReply(Admitted("", stops, frozenset()), request_id="sample")
    monkeypatch.setattr(cli_reload, "socket_send", lambda *a, **kw: lambda *a: reply)
    result = invoke(["update"], style)
    assert result.exit_code == 0
    if stops:
        assert (
            result.stdout.splitlines()[0]
            == f"stop requested for {len(stops)} running job{'s' if len(stops) != 1 else ''}: {', '.join(stops)}"
        )
    else:
        assert result.stdout.splitlines()[0] == "update admitted; application not yet confirmed"
        assert "stops reported" not in result.stdout


def test_r2_omitted_job_details_say_size_limit(style, tmp_path):
    from dsl41.cli_output import reread_lines

    answer = candidate_answer(tmp_path)
    answer["preview"]["definition_changes"].update(groups=[], omitted=1)
    text = "\n".join(reread_lines(answer, style, "dsl41 update"))
    assert "details not shown (size limit)" in text
    assert "attribute details unavailable" not in text


def test_r2_blocked_file_only_edit_still_explains_the_change(style, tmp_path):
    from dsl41.cli_output import reread_lines

    answer = candidate_answer(tmp_path, changed=False)
    answer["preview"]["definition_changes"].update(
        sources_changed=True, blocked=["the engine is draining"]
    )
    lines = reread_lines(answer, style, "dsl41 update")
    assert "estate files changed, but no job or setting did" in lines
    assert not any("update still applies" in line for line in lines)


def test_r2_neutral_shared_settings_precede_alphabetically_earlier_jobs(tmp_path):
    from dsl41.cli_output import reread_lines

    answer = candidate_answer(tmp_path)
    answer["preview"]["definition_changes"]["groups"].append(
        {
            "field": "estate setting timezone",
            "scope": "settings",
            "before": "UTC",
            "after": "Europe/Zurich",
            "complex": False,
            "redacted": False,
            "jobs": [],
        }
    )
    lines = reread_lines(answer, OutputStyle.neutral, "dsl41 update")
    rows = [line for line in lines if line.startswith("  ")]
    assert rows[0].startswith("  shared settings   estate setting timezone")
    assert rows[1].startswith("  j0   ")


def test_r2_empty_table_returns_only_its_header():
    from dsl41.cli_output import table_lines

    assert table_lines(("JOB", "REV"), ()) == ["JOB  REV"]


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("stops", [(), ("a",), ("a", "b")])
def test_r3_admission_words_match_pending_and_applied_answers(monkeypatch, style, pending, stops):
    application = Application(
        "", 2, "hash", "", CatalogIR(), (), stops, (), (), tuple((job, "rerun") for job in stops)
    )
    reply = cli_reload.UpdateReply(
        Admitted("", stops, frozenset()),
        application=None if pending else application,
        pending=pending,
    )
    monkeypatch.setattr(cli_reload, "socket_send", lambda *a, **kw: lambda *a: reply)
    result = invoke(["update"], style)
    assert result.exit_code == int(pending)
    request_lines = [
        line for line in result.stdout.splitlines() if line.startswith("stop requested for")
    ]
    assert request_lines == (
        [
            f"stop requested for {len(stops)} running job{'s' if len(stops) != 1 else ''}: {', '.join(stops)}"
        ]
        if stops
        else []
    )
    assert not any(
        line.startswith(("stopped ", "stopping ", "if a running box"))
        for line in result.stdout.splitlines()
    )
    if not pending:
        for job in stops:
            assert f"{job}   " in result.stdout or style is OutputStyle.focused and len(stops) > 1
        assert "stopped, rerun under the new definition" in result.stdout or not stops


def test_r3_application_text_has_no_general_box_rule_and_preserves_outcomes(style):
    from dsl41.cli_output import application_lines

    choices = ("complete", "rerun", "hold", "none")
    application = Application(
        "",
        2,
        "hash",
        "",
        CatalogIR(),
        (),
        ("BOX",),
        (),
        (),
        tuple((choice, choice) for choice in choices),
    )
    lines = application_lines(application, style)
    assert not any(line.startswith("if ") for line in lines)
    for text in (
        "its own result stands",
        "stopped, rerun under the new definition",
        "stopped, now ON_HOLD (automatic rerun withheld)",
        "stopped, no follow-up of its own",
    ):
        assert sum(text in line for line in lines) == 1


def test_r3_existing_preview_branches_remain_exercised(style, tmp_path):
    from dsl41.cli_output import reread_lines

    boxed = {"status": "RUNNING", "box_name": "BX"}
    assert (
        "box BX" in "\n".join(status_lines({"jobs": {"j": boxed}}, style))
        or style is OutputStyle.neutral
    )
    answer = candidate_answer(tmp_path)
    answer["preview"].pop("definition_changes")
    text = "\n".join(reread_lines(answer, style, "dsl41 update"))
    assert "attribute details unavailable" in text and "update readiness unavailable" in text
    answer = candidate_answer(tmp_path)
    view = answer["preview"]
    view["restored"] = [{"name": "j0", "flags": ["ON_HOLD", "ON_ICE"]}]
    view["non_idempotent"] = [{"job": "j0", "reason": "synthetic reason"}]
    view["lint_messages"] = ["synthetic blocking finding"]
    view["definition_changes"]["blocked"] = ["synthetic blocking finding"]
    text = "\n".join(reread_lines(answer, style, "dsl41 update"))
    assert "re-added with its earlier flags" in text
    assert "marked non-idempotent: j0 (synthetic reason)" in text
    assert "update would refuse: blocking findings are reported on stderr" in text


@pytest.mark.parametrize("header", [{"ok": False, "error": "header refused"}, {"ok": True}])
def test_r3_update_does_not_submit_without_a_read_header(monkeypatch, header):
    from dsl41 import runner_control

    requests = []

    def answer(path, request, **kwargs):
        requests.append(request)
        return header

    monkeypatch.setattr(runner_control, "roundtrip", answer)
    with pytest.raises(cli_reload.SendFailed):
        cli_reload.socket_send(Path("/synthetic/control.sock"))("update", {})
    assert requests == [{"cmd": "status"}]


def test_r3_reload_transport_errors_keep_their_detail(monkeypatch):
    from pathlib import Path
    from dsl41 import runner_control

    def fail(*a, **kw):
        raise runner_control.ControlClientError("synthetic transport failure") from OSError(
            "connect failed"
        )

    monkeypatch.setattr(runner_control, "roundtrip", fail)
    with pytest.raises(cli_reload.SendFailed, match="synthetic transport failure"):
        cli_reload.socket_send(Path("/synthetic/control.sock"))("reread", {})


@pytest.mark.parametrize(
    "kind", ["pending", "admitted", "unchanged", "stale_plan", "update_in_progress"]
)
def test_r3_update_reply_keeps_admission_application_and_refusal_distinct(kind):
    answer = {"ok": True, "request_id": "synthetic", "stop": ["a"], "application": None}
    if kind in {"stale_plan", "update_in_progress"}:
        answer.update(ok=False, code=kind, names=["a"])
    elif kind == "pending":
        answer["pending"] = True
    elif kind == "unchanged":
        answer["unchanged"] = {"rev": 2, "catalog_hash": "hash"}
    reply = cli_reload.update_reply_of(answer)
    if kind in {"stale_plan", "update_in_progress"}:
        assert isinstance(reply.decision, Refused) and reply.decision.names == ("a",)
    else:
        assert isinstance(reply.decision, Admitted)
        assert reply.pending == (kind == "pending")
        assert (reply.unchanged is not None) == (kind == "unchanged")


# ---- a `status:` line alone (DL-533)

SEED_ROW = (
    "initial_status: (unset) -> ON_HOLD; the status: edit itself sets no flag on a job that"
    " stays (DL-533); hold or release it with sendevent"
)
#: DL-561: a line that changes to or from a run state is a status, not a flag
SEED_STATUS_ROW = (
    "initial_status: (unset) -> {status}; the status: line is a seed, and the update leaves the"
    " live job as it is (DL-533, DL-561); a flag is changed with its own sendevent event and a"
    " status with sendevent CHANGE_STATUS; either acts on the job as it is now and can start it"
    " or the jobs that wait on it, so look at the job's current state first"
)
_SEED_JOBS = "".join(
    f"insert_job: {name}\njob_type: c\ncommand: true\nmachine: localhost\n\n"
    for name in ("edited", "other", "seeded", "twin")
)


def _seed_line(text: str, job: str, status: str = "ON_HOLD") -> str:
    old = f"insert_job: {job}\njob_type: c\n"
    assert old in text
    return text.replace(old, f"{old}status: {status}\n")


def _seed_forms(tmp_path, *, mixed: bool, status: str = "ON_HOLD", lint: str = "", extra=""):
    """The passive `reread` answer and the stored plan's application for an edit of the
    `status:` lines of `seeded` (running) and `twin`; `mixed` also edits the commands of
    `edited` and `twin`."""
    from dsl41.oracle import Oracle
    from dsl41.reload_plan import read_plan, reread
    from dsl41.reload_update import plan_application
    from test_main_config import config_file, live_of

    path = config_file(tmp_path, lint=lint)
    jil = tmp_path / "a.jil"
    jil.write_text(_SEED_JOBS + extra)
    before = main_config.prepare(path)
    text = _seed_line(_seed_line(_SEED_JOBS + extra, "seeded", status), "twin", status)
    if mixed:
        for job in ("edited", "twin"):
            start = text.index(f"insert_job: {job}\n")
            end = text.index("\n\n", start)
            text = text[:start] + text[start:end].replace("true", "false") + text[end:]
    jil.write_text(text)
    candidate = main_config.prepare(path)
    live = live_of(before, {"seeded": "RUNNING"})
    answer = {
        "ok": True,
        "live_rev": 1,
        "source": {"path": str(path)},
        "preview": preview(candidate, live),
    }
    if lint:
        return answer, None
    result = reread(live, tmp_path / "plans", candidate=candidate)
    document = read_plan(tmp_path / "plans", result.token)
    assert document is not None
    return answer, plan_application(Oracle(before.catalog), candidate.catalog, document)


def test_a_seed_only_reread_and_update_name_the_seed_once_in_each_layout(tmp_path, style):
    from dsl41.cli_output import application_lines, reread_lines

    answer, application = _seed_forms(tmp_path, mixed=False)
    assert answer["preview"]["seed_only"] == [
        {"name": "seeded", "before": "(unset)", "after": "ON_HOLD"},
        {"name": "twin", "before": "(unset)", "after": "ON_HOLD"},
    ]
    assert answer["preview"]["changed"] == [] and answer["preview"]["stop"] == []
    assert answer["preview"]["marks"] == {}
    rows = (
        [f"  {SEED_ROW}   2 jobs: seeded, twin"]
        if style is OutputStyle.focused
        else [f"  seeded   {SEED_ROW}", f"  twin   {SEED_ROW}"]
    )
    assert reread_lines(answer, style, "dsl41 update") == [
        "estate on disk differs from the running estate (revision 1)",
        "",
        "changed 0 jobs, added 0, removed 0, status line only 2, unchanged 2",
        *rows,
        "",
        "if applied now: no running job is stopped",
        f"read from: {tmp_path / 'a.jil'}",
        "apply with: dsl41 update",
        "forecast at reread time; update rereads before applying",
    ]
    assert application is not None and application.changed == ()
    assert application.seed_only == (("seeded", None, "ON_HOLD"), ("twin", None, "ON_HOLD"))
    assert application_lines(application, style) == [
        f"applied: catalog revision 2 ({application.digest})",
        "  changed 0 jobs, added 0, removed 0, status line only 2",
        *rows,
    ]


@pytest.mark.parametrize("status", ["SUCCESS", "FAILURE", "TERMINATED"])
def test_sem24_a_run_state_seed_only_edit_is_shown_and_stops_nothing(tmp_path, style, status):
    """SEM-24, DL-533 (DL-561): a kept job's run-state `status:` line alone is seed-only, as a
    flag value is: shown once, counted on its own, and nothing is stopped."""
    from dsl41.cli_output import application_lines, reread_lines

    answer, application = _seed_forms(tmp_path, mixed=False, status=status)
    assert answer["preview"]["seed_only"] == [
        {"name": "seeded", "before": "(unset)", "after": status},
        {"name": "twin", "before": "(unset)", "after": status},
    ]
    assert answer["preview"]["changed"] == [] and answer["preview"]["stop"] == []
    row = SEED_STATUS_ROW.format(status=status)
    lines = reread_lines(answer, style, "dsl41 update")
    assert "changed 0 jobs, added 0, removed 0, status line only 2, unchanged 2" in lines
    assert "if applied now: no running job is stopped" in lines
    assert any(row in line for line in lines)
    assert application is not None and application.changed == ()
    assert application.seed_only == (("seeded", None, status), ("twin", None, status))
    assert any(row in line for line in application_lines(application, style))


_FLAG_NOTE = (
    "the status: edit itself sets no flag on a job that stays (DL-533); hold or release it with"
    " sendevent"
)
_RUN_NOTE = (
    "the status: line is a seed, and the update leaves the live job as it is (DL-533, DL-561);"
    " a flag is changed with its own sendevent event and a status with sendevent CHANGE_STATUS;"
    " either acts on the job as it is now and can start it or the jobs that wait on it, so look"
    " at the job's current state first"
)


@pytest.mark.parametrize(
    ("before", "after", "note"),
    [
        # among the flags, INACTIVE and none: DL-533's wording
        ("ON_HOLD", "ON_ICE", _FLAG_NOTE),
        (None, "ON_NOEXEC", _FLAG_NOTE),
        ("ON_ICE", "INACTIVE", _FLAG_NOTE),
        ("INACTIVE", None, _FLAG_NOTE),
        # to or from a run state: the seed note, with no sequence of events
        (None, "SUCCESS", _RUN_NOTE),
        ("FAILURE", None, _RUN_NOTE),
        ("TERMINATED", "INACTIVE", _RUN_NOTE),
        ("SUCCESS", "ON_HOLD", _RUN_NOTE),
        ("ON_ICE", "FAILURE", _RUN_NOTE),
        ("ON_NOEXEC", "TERMINATED", _RUN_NOTE),
        ("FAILURE", "SUCCESS", _RUN_NOTE),
    ],
)
def test_dl561_the_seed_note_for_each_kind_of_edit(before, after, note):
    """DL-533, DL-561: an edit among the flags, INACTIVE and none keeps DL-533's wording; an edit
    to or from a run state says the line is a seed, names the events and prescribes no order."""
    from dsl41.cli_output import seed_text

    shown = ["(unset)" if value is None else value for value in (before, after)]
    assert seed_text(before, after) == f"initial_status: {shown[0]} -> {shown[1]}; {note}"
    for word in ("first,", "then ", "nothing starts"):
        assert word not in note


def test_dl561_a_status_change_can_start_the_job_the_note_warns_of():
    """DL-561: the note's warning holds. An idle job seeded SUCCESS with `condition: n(P)` starts
    when CHANGE_STATUS sets it INACTIVE, through its own condition."""
    from datetime import datetime

    from dsl41.ir import lower_source
    from dsl41.oracle import Oracle
    from dsl41.oracle_state import Event

    oracle = Oracle(
        lower_source(
            "insert_job: P\njob_type: c\ncommand: x\nmachine: m1\nstatus: SUCCESS\n"
            "condition: n(P)\n"
        )
    )
    at = datetime(2026, 7, 1, 8, 0)
    oracle.feed(Event(at=at, kind="STATUS", payload={"job": "P", "status": "INACTIVE"}))
    row = oracle.store.job["P"]
    assert (row.status, row.run_number) == ("RUNNING", 1)


def test_a_mixed_reread_and_update_count_the_seed_on_its_own(tmp_path, style):
    from dsl41.cli_output import application_lines, reread_lines

    answer, application = _seed_forms(tmp_path, mixed=True)
    assert [item["name"] for item in answer["preview"]["changed"]] == ["edited", "twin"]
    assert [item["name"] for item in answer["preview"]["seed_only"]] == ["seeded"]
    if style is OutputStyle.focused:
        rows = [
            "  command: true -> false   2 jobs: edited, twin",
            "  initial_status: (unset) -> ON_HOLD   1 job: twin",
            f"  {SEED_ROW}   1 job: seeded",
        ]
        applied = [f"  seeded   {SEED_ROW}"]
    else:
        rows = [
            "  edited   command: true -> false",
            f"  seeded   {SEED_ROW}",
            "  twin   command: true -> false; initial_status: (unset) -> ON_HOLD",
        ]
        applied = [
            "  edited   definition updated",
            f"  seeded   {SEED_ROW}",
            "  twin   definition updated",
        ]
    assert reread_lines(answer, style, "dsl41 update") == [
        "estate on disk differs from the running estate (revision 1)",
        "",
        "changed 2 jobs, added 0, removed 0, status line only 1, unchanged 1",
        *rows,
        "",
        "if applied now: no running job is stopped",
        f"read from: {tmp_path / 'a.jil'}",
        "apply with: dsl41 update",
        "forecast at reread time; update rereads before applying",
    ]
    assert application is not None
    assert application_lines(application, style) == [
        f"applied: catalog revision 2 ({application.digest})",
        "  changed 2 jobs, added 0, removed 0, status line only 1",
        *applied,
    ]


def test_a_seed_that_raises_a_refused_finding_offers_no_update(tmp_path, style):
    """DL-533: the seed-only rows say nothing of admission. An ON_ICE seed on a producer raises
    L020 where ON_HOLD does not; under `refuse = "warn"` the footer says update would refuse."""
    from dsl41.cli_output import reread_lines

    lint = 'refuse = "warn"\nignore = ["L011", "L021"]\n'
    consumer = (
        "insert_job: consumer\njob_type: c\ncommand: true\nmachine: localhost\n"
        "condition: s(seeded) & s(twin)\n"
    )
    hold, _ = _seed_forms(tmp_path / "hold", mixed=False, lint=lint, extra=consumer)
    assert "apply with: dsl41 update" in reread_lines(hold, style, "dsl41 update")
    ice, _ = _seed_forms(tmp_path / "ice", mixed=False, status="ON_ICE", lint=lint, extra=consumer)
    assert any("L020" in message for message in ice["preview"]["lint_messages"])
    assert not any("L020" in message for message in hold["preview"]["lint_messages"])
    text = reread_lines(ice, style, "dsl41 update")
    assert any(line.startswith("update would refuse") for line in text)
    assert "apply with: dsl41 update" not in text
    assert any(
        "initial_status: (unset) -> ON_ICE; the status: edit itself" in line for line in text
    )


def test_an_older_engines_answer_without_seed_only_still_renders(tmp_path, style):
    from dsl41.cli_output import application_lines, reread_lines

    answer, application = _seed_forms(tmp_path, mixed=True)
    older = copy.deepcopy(answer)
    del older["preview"]["seed_only"]
    assert "status line only" not in "\n".join(reread_lines(older, style, "dsl41 update"))
    body = {
        "rev": 2,
        "catalog_hash": "h",
        "added": [],
        "changed": ["edited"],
        "removed": [],
        "restored": [],
        "stopped": [],
    }
    reply = cli_reload.update_reply_of({"ok": True, "token": "t", "application": body})
    assert reply.application is not None and reply.application.seed_only == ()
    assert application_lines(reply.application, style)[1] == "  changed 1 job, added 0, removed 0"


def test_a_status_only_update_applies_live_with_no_stop_and_restart_keeps_the_rows(
    tmp_path, monkeypatch
):
    """DL-533 end to end: a running job and an idle one whose `status:` lines alone change. The
    update applies at once with no stop, `update --wait` exits 0 and does not say nothing
    changed, the attempt runs on, the rows keep their flags, and a restart does not re-seed
    them (SEM-24)."""
    import asyncio
    from datetime import datetime
    from dsl41.cli_output import reread_lines
    from dsl41.oracle_state import Event
    from dsl41.runner_adapters import FakeAdapter
    from dsl41.runner_clock import VirtualClock
    from dsl41.runner_control import ControlServer
    from dsl41.runner_startup import active_configuration, resume_run, start_run

    config, jil = tmp_path / "dsl41.toml", tmp_path / "jobs.jil"
    config.write_text(
        f'format_version = 1\n[engine]\nrun_root = "{tmp_path}/run"\n[estate]\ninclude = ["jobs.jil"]\n'
    )
    jobs = "".join(
        f"insert_job: {name}\njob_type: c\ncommand: true\nmachine: localhost\n\n"
        for name in ("idle", "sample")
    )
    jil.write_text(jobs)
    initial = main_config.prepare(config)
    at = datetime(2026, 7, 1)
    seen: dict[str, object] = {}

    async def scenario():
        engine = start_run(
            initial.catalog,
            tmp_path / "run",
            clock=VirtualClock(start=at),
            adapters={"CMD": FakeAdapter(park={"sample"})},
            launched=main_config.profile_of(initial),
            source_capture=main_config.encoded_parts(initial),
        )
        engine.launch_settings = initial.metadata["launch"]
        engine.reread_source = str(config)
        server = ControlServer(
            engine,
            tmp_path / "run" / "control.sock",
            estate_fingerprint=main_config.fingerprint(initial),
        )

        async def drive():
            while True:
                await engine._activity.wait()
                engine._activity.clear()
                await engine.run_until_quiescent(at)

        pump = asyncio.create_task(drive())
        request = {"v": 5, "cmd": "update", "baseline_id": engine.baseline_id, "wait": True}
        try:
            engine.inject(Event(at=at, kind="FORCE_STARTJOB", payload={"job": "sample"}))
            await engine.run_until_quiescent(at)
            async with asyncio.timeout(10):
                while "sample" not in engine._live:
                    await asyncio.sleep(0.01)
            attempt = engine._live["sample"].attempt
            rows = {name: engine.oracle.store.job[name] for name in ("idle", "sample")}
            assert rows["sample"].status == "RUNNING"
            jil.write_text(
                _seed_line(_seed_line(jobs, "idle", "ON_ICE"), "sample", "ON_HOLD"),
            )
            forecast = await server._reread({"v": 5, "cmd": "reread"})
            assert forecast["ok"], forecast
            seen["reread"] = reread_lines(forecast, OutputStyle.neutral, "dsl41 update")
            answer = await asyncio.wait_for(server._update({**request, "request_id": "seed"}), 30)
            assert answer["stop"] == [] and "unchanged" not in answer, answer
            assert answer["application"]["seed_only"] == [
                ["idle", None, "ON_ICE"],
                ["sample", None, "ON_HOLD"],
            ]
            assert answer["application"]["changed"] == [] and answer["application"]["rev"] == 2
            seen["answer"] = answer
            assert engine.config_rev == 2
            assert engine._live["sample"].attempt == attempt
            assert {name: engine.oracle.store.job[name] for name in rows} == rows
            catalog = engine.oracle.catalog.jobs
            assert (catalog["idle"].sem.initial_status, catalog["sample"].sem.initial_status) == (
                "ON_ICE",
                "ON_HOLD",
            )
            retry = await asyncio.wait_for(server._update({**request, "request_id": "seed"}), 30)
            assert retry["application"]["seed_only"] == answer["application"]["seed_only"]
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
            await server.close()
            await engine.shutdown()
            engine.journal.close()
        stored = active_configuration(tmp_path / "run")
        assert stored.catalog.jobs["idle"].sem.initial_status == "ON_ICE"
        again = await resume_run(
            stored.catalog,
            tmp_path / "run",
            clock=VirtualClock(start=at),
            adapters={"CMD": FakeAdapter(park={"sample"})},
            declared=stored.profile,
            stored_sources=True,
        )
        try:
            assert again.config_rev == 2
            idle = again.oracle.store.job["idle"]
            assert (idle.status, idle.on_hold, idle.on_ice, idle.on_noexec) == (
                "INACTIVE",
                False,
                False,
                False,
            )
            sample = again.oracle.store.job["sample"]
            assert (sample.on_hold, sample.on_ice, sample.on_noexec) == (False, False, False)
        finally:
            await again.shutdown()
            again.journal.close()

    asyncio.run(scenario())
    assert seen["reread"][:4] == [
        "estate on disk differs from the running estate (revision 1)",
        "",
        "changed 0 jobs, added 0, removed 0, status line only 2, unchanged 0",
        "  idle   " + SEED_ROW.replace("ON_HOLD", "ON_ICE"),
    ]
    reply = cli_reload.update_reply_of(seen["answer"])
    monkeypatch.setattr(cli_reload, "socket_send", lambda *a, **kw: lambda *a: reply)
    result = invoke(["update"], OutputStyle.neutral, "--wait")
    assert result.exit_code == 0, result.output
    assert "nothing changed" not in result.stdout
    assert "stop requested" not in result.stdout
    assert result.stdout.splitlines()[1:] == [
        "  changed 0 jobs, added 0, removed 0, status line only 2",
        "  idle   " + SEED_ROW.replace("ON_HOLD", "ON_ICE"),
        f"  sample   {SEED_ROW}",
    ]


def _box_member(name: str) -> str:
    return f"insert_job: {name}\njob_type: c\nbox_name: B\ncommand: true\nmachine: localhost\n\n"


_BOXED = (
    "insert_job: B\njob_type: b\n\n"
    + _box_member("m1")
    + "insert_job: o\njob_type: c\ncommand: true\nmachine: localhost\n\n"
)


def _boxed_answer(tmp_path, text: str, status: dict[str, str], marked: str = ""):
    """The passive `reread` answer for box B (member m1) and job o, edited to `text`; m2 is
    added to B either way."""
    from test_main_config import config_file, live_of

    path = config_file(tmp_path)
    (tmp_path / "a.jil").write_text(_BOXED)
    if marked:
        (tmp_path / "non-idempotent.toml").write_text(f'[jobs]\n{marked} = "synthetic mark"\n')
    before = main_config.prepare(path)
    (tmp_path / "a.jil").write_text(text + _box_member("m2"))
    candidate = main_config.prepare(path)
    return {
        "ok": True,
        "live_rev": 1,
        "source": {"path": str(path)},
        "preview": preview(candidate, live_of(before, status)),
    }


def test_a_box_with_a_seed_and_a_membership_change_is_counted_once_as_changed(tmp_path, style):
    """DL-533: B's own record differs only in `status:`, and it gains member m2. The JSON keeps
    both lists; the summary counts B once, as changed, and its row keeps both explanations."""
    from dsl41.cli_output import reread_lines

    text = _BOXED.replace(
        "insert_job: B\njob_type: b\n", "insert_job: B\njob_type: b\nstatus: ON_HOLD\n"
    )
    answer = _boxed_answer(tmp_path, text, {})
    view = answer["preview"]
    assert view["changed"] == [{"name": "B", "reason": "membership"}]
    assert view["seed_only"] == [{"name": "B", "before": "(unset)", "after": "ON_HOLD"}]
    assert (view["added"], view["removed"], view["unchanged"]) == (["m2"], [], 2)
    source = f"source: - -> {tmp_path / 'a.jil'}"
    if style is OutputStyle.focused:
        rows = [
            "  membership   1 job: B",
            f"  {SEED_ROW}   1 job: B",
            "  added   1 job: m2",
            f"  {source}   1 job: m2",
        ]
    else:
        rows = [f"  B   membership; {SEED_ROW}", f"  m2   added; {source}"]
    assert reread_lines(answer, style, "dsl41 update") == [
        "estate on disk differs from the running estate (revision 1)",
        "",
        "changed 1 job, added 1, removed 0, unchanged 2",
        *rows,
        "",
        "if applied now: no running job is stopped",
        f"read from: {tmp_path / 'a.jil'}",
        "apply with: dsl41 update",
        "forecast at reread time; update rereads before applying",
    ]


def test_a_marked_seed_only_member_of_a_refusing_box_shows_its_seed_and_the_refusal(
    tmp_path, style
):
    """DL-533 with DL-559: running m1 is marked non-idempotent, its `status:` line alone
    changes, and B gains m2. The seed row says what the edit does; the forecast stops nothing
    and says the update would be refused by B and m1. The application row of a resumed update
    that restarted B still carries both."""
    from dsl41.cli_output import application_lines, reread_lines

    text = _BOXED.replace(
        "box_name: B\ncommand: true", "box_name: B\nstatus: ON_HOLD\ncommand: true"
    )
    answer = _boxed_answer(tmp_path, text, {"B": "RUNNING", "m1": "RUNNING"}, marked="m1")
    view = answer["preview"]
    assert view["effects"] == [] and view["stop"] == []
    assert "m1" not in view["marks"]
    assert view["update_refusal"] == {"code": "tree_active", "names": ["B", "m1"]}
    source = f"source: - -> {tmp_path / 'a.jil'}"
    if style is OutputStyle.focused:
        rows = [
            "  membership   1 job: B",
            f"  {SEED_ROW}   1 job: m1",
            "  added   1 job: m2",
            f"  {source}   1 job: m2",
        ]
    else:
        rows = ["  B   membership", f"  m1   {SEED_ROW}", f"  m2   added; {source}"]
    assert reread_lines(answer, style, "dsl41 update") == [
        "estate on disk differs from the running estate (revision 1)",
        "",
        "changed 1 job, added 1, removed 0, status line only 1, unchanged 1",
        *rows,
        "",
        "if applied now: no running job is stopped",
        "update would be refused: it changes box membership and these jobs are active or"
        " would be stopped: B, m1",
        f"read from: {tmp_path / 'a.jil'}",
        "forecast at reread time; update rereads before applying",
    ]
    application = Application(
        "",
        2,
        "h",
        "",
        CatalogIR(),
        ("m2",),
        (),
        (),
        (),
        (("m1", "none"),),
        seed_only=(("m1", None, "ON_HOLD"),),
    )
    outcome = "stopped, no follow-up of its own"
    rows = (
        [f"  m1   {outcome}", f"  m1   {SEED_ROW}"]
        if style is OutputStyle.focused
        else [f"  m1   {SEED_ROW}; {outcome}", "  m2   added"]
    )
    assert application_lines(application, style) == [
        "applied: catalog revision 2 (h)",
        "  changed 0 jobs, added 1, removed 0, status line only 1",
        *rows,
    ]


def test_a_forecast_refusal_names_every_job_in_the_neutral_layout_and_offers_no_apply(
    tmp_path, style
):
    """DL-559: under a forecast `tree_active` refusal reread prints no "apply with" line. The
    neutral layout names every job in the way; the focused one keeps its short list."""
    from dsl41.cli_output import reread_lines

    answer = _boxed_answer(tmp_path, _BOXED, {"B": "RUNNING", "m1": "RUNNING"})
    names = ["B", "c1", "c2", "c3", "m1"]
    answer["preview"]["update_refusal"] = {"code": "tree_active", "names": names}
    lines = reread_lines(answer, style, "dsl41 update")
    shown = "B, c1, c2, c3, m1" if style is OutputStyle.neutral else "B, c1, c2 and 2 more"
    assert (
        "update would be refused: it changes box membership and these jobs are active or"
        f" would be stopped: {shown}"
    ) in lines
    assert not [line for line in lines if line.startswith("apply with:")]


def test_an_older_engines_box_restart_labels_still_print(tmp_path, style):
    """DL-559: an engine before DL-559 forecasts its box restarts with `restart_box` and
    `restart` and sends no `update_refusal`. A newer client prints both labels in both
    layouts, and the apply line as before."""
    from dsl41.cli_output import reread_lines

    answer = _boxed_answer(tmp_path, _BOXED, {"B": "RUNNING", "m1": "RUNNING"})
    view = answer["preview"]
    del view["update_refusal"]
    view["stop"] = ["B", "m1"]
    view["effects"] = [
        {"job": "B", "action": "restart_box"},
        {"job": "m1", "action": "restart"},
    ]
    lines = reread_lines(answer, style, "dsl41 update")
    assert "  B   would restart as a new box run" in lines
    assert "  m1   would restart with its box" in lines
    assert "apply with: dsl41 update" in lines


def test_bug72_a_retyped_stop_reads_as_replaced_and_a_removed_one_as_removed(tmp_path, style):
    """BUG-72, DL-559: the running `R` is retyped into a file watch and the running `G` is
    removed, outside every box. Both are stops with the `remove` label; the text says a new job
    of its name replaces `R` and that `G` gets no replacement. No refusal is forecast."""
    from dsl41.cli_output import reread_lines
    from test_main_config import config_file, live_of

    path = config_file(tmp_path)
    command = "job_type: c\ncommand: true\nmachine: localhost\n\n"
    (tmp_path / "a.jil").write_text(f"insert_job: R\n{command}insert_job: G\n{command}")
    before = main_config.prepare(path)
    (tmp_path / "a.jil").write_text("insert_job: R\njob_type: f\nwatch_file: /synthetic/never\n\n")
    view = preview(main_config.prepare(path), live_of(before, {"R": "RUNNING", "G": "RUNNING"}))
    assert view["effects"] == [
        {"job": "G", "action": "remove"},
        {"job": "R", "action": "remove"},
    ]
    assert view["update_refusal"] is None
    answer = {"ok": True, "live_rev": 1, "source": {"path": str(path)}, "preview": view}
    lines = reread_lines(answer, style, "dsl41 update")
    assert "  R   would be replaced by a new job of the same name" in lines
    assert "  G   would be removed; no replacement" in lines


def test_identical_seeds_on_a_box_that_lost_its_member_and_on_a_job_group_as_all(tmp_path):
    """DL-533: box B loses its only member, and B and o get the same `status:` line. B counts
    once, as changed (membership), so the seed group names every job and reads `(all)`; a
    second count of B would make the total three and drop the label."""
    from dsl41.cli_output import reread_lines
    from test_main_config import config_file, live_of

    path = config_file(tmp_path)
    (tmp_path / "a.jil").write_text(_BOXED)
    before = main_config.prepare(path)
    (tmp_path / "a.jil").write_text(
        "insert_job: B\njob_type: b\nstatus: ON_HOLD\n\n"
        "insert_job: o\njob_type: c\nstatus: ON_HOLD\ncommand: true\nmachine: localhost\n"
    )
    answer = {
        "ok": True,
        "live_rev": 1,
        "source": {"path": str(path)},
        "preview": preview(main_config.prepare(path), live_of(before)),
    }
    view = answer["preview"]
    assert view["changed"] == [{"name": "B", "reason": "membership"}]
    assert [item["name"] for item in view["seed_only"]] == ["B", "o"]
    assert (view["removed"], view["unchanged"]) == (["m1"], 0)
    lines = reread_lines(answer, OutputStyle.focused, "dsl41 update")
    assert "changed 1 job, added 0, removed 1, status line only 1, unchanged 0" in lines
    assert f"  {SEED_ROW}   2 jobs (all)" in lines
