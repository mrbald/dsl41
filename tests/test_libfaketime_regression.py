"""Host checks for refusals in the optional native dependency regression tools."""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "examples" / "dst-drill" / "libfaketime"


def load_tool(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"libfaketime_{name}", PACKAGE / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def observer() -> ModuleType:
    return load_tool("observe")


@pytest.fixture
def upstream() -> ModuleType:
    return load_tool("upstream_gate")


@pytest.mark.parametrize("key", ["LD_PRELOAD", "LD_LIBRARY_PATH", "FAKETIME", "NO_FAKE_STAT"])
def test_observer_refuses_inherited_overrides(observer, monkeypatch, key):
    monkeypatch.setenv(key, "inherited")
    with pytest.raises(RuntimeError, match="Observer inherited overrides"):
        observer.clean_process()


@pytest.mark.parametrize(
    ("uid", "machine", "reason"),
    [(0, "aarch64", "non-root"), (1000, "x86_64", "must be aarch64")],
)
def test_observer_refuses_wrong_target_before_commands(observer, monkeypatch, uid, machine, reason):
    monkeypatch.setattr(observer.os, "getuid", lambda: uid)
    monkeypatch.setattr(observer.platform, "machine", lambda: machine)
    instance = observer.Observer.__new__(observer.Observer)
    with pytest.raises(RuntimeError, match=reason):
        instance.run()


def prepared_observer(observer, monkeypatch, tmp_path, rpm="glibc-2.28-251.el8_10.48.aarch64"):
    monkeypatch.setattr(observer.os, "getuid", lambda: 1000)
    monkeypatch.setattr(observer.platform, "machine", lambda: "aarch64")
    original, candidate = tmp_path / "original.so", tmp_path / "candidate.so"
    original.write_bytes(b"original test library identity")
    candidate.write_bytes(b"candidate test library identity")
    instance = observer.Observer.__new__(observer.Observer)
    instance.args = argparse.Namespace(
        original=str(original),
        candidate=str(candidate),
        candidate_sha256="0" * 64,
        exclusive_note="isolated host refusal test",
    )
    instance.probe = str(tmp_path / "probe")
    Path(instance.probe).write_bytes(b"synthetic compiled probe")
    instance.log = lambda *args, **kwargs: None
    responses = iter([rpm, "", json.dumps({"glibc": "2.28"}), "VERSION perl=test\n10.0\n10.2\n"])
    instance.command = lambda *args, **kwargs: next(responses)
    return instance


def test_observer_refuses_wrong_glibc_before_build(observer, monkeypatch, tmp_path):
    instance = prepared_observer(observer, monkeypatch, tmp_path, rpm="glibc-other")
    with pytest.raises(RuntimeError, match="Wrong native glibc build"):
        instance.run()


def test_observer_refuses_original_library_identity_before_preload(observer, monkeypatch, tmp_path):
    instance = prepared_observer(observer, monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="Original exact bytes do not match"):
        instance.run()


def test_observer_refuses_candidate_library_identity_before_preload(
    observer, monkeypatch, tmp_path
):
    instance = prepared_observer(observer, monkeypatch, tmp_path)
    monkeypatch.setattr(observer, "ORIGINAL", observer.digest(instance.args.original))
    with pytest.raises(RuntimeError, match="Candidate bytes do not match"):
        instance.run()


@pytest.mark.parametrize("output", ["VERSION perl=test\n10\n10\n", "VERSION perl=test\nnan\n11\n"])
def test_observer_refuses_unusable_monotonic_prerequisite(observer, monkeypatch, tmp_path, output):
    instance = prepared_observer(observer, monkeypatch, tmp_path)
    responses = iter(["glibc-2.28-251.el8_10.48.aarch64", "", '{"glibc":"2.28"}', output])
    instance.command = lambda *args, **kwargs: next(responses)
    with pytest.raises(RuntimeError, match="two increasing numeric readings"):
        instance.run()


def gate_arguments(tmp_path):
    combined, inventory, report = (tmp_path / name for name in ("combined", "inventory", "report"))
    combined.write_text("incomplete upstream output\n")
    inventory.write_text(
        json.dumps({"decision": "CLEAR_AT_INVENTORY_TIME", "fd0": {"target": "/dev/null"}})
    )
    report.write_text(json.dumps({"status": "PASS", "records": []}))
    return argparse.Namespace(
        make_status=0,
        combined_log=str(combined),
        inventory_receipt=str(inventory),
        regression_report=str(report),
        trace_directory=str(tmp_path / "trace"),
    )


def test_upstream_refuses_failed_make_before_reading_logs(upstream):
    with pytest.raises(RuntimeError, match="make test did not exit 0"):
        upstream.check(argparse.Namespace(make_status=2))


def test_upstream_refuses_truncated_log(upstream, tmp_path):
    args = gate_arguments(tmp_path)
    Path(args.combined_log).write_text("unterminated output")
    with pytest.raises(RuntimeError, match="Truncated stdout/stderr"):
        upstream.check(args)


@pytest.mark.parametrize(
    "receipt",
    [{"decision": "STOP", "fd0": {"target": "/dev/null"}}, {"decision": "CLEAR_AT_INVENTORY_TIME"}],
)
def test_upstream_refuses_missing_or_blocked_inventory(upstream, tmp_path, receipt):
    args = gate_arguments(tmp_path)
    Path(args.inventory_receipt).write_text(json.dumps(receipt))
    with pytest.raises(RuntimeError, match="Missing pre-gate residue/fd0 receipt"):
        upstream.check(args)


def test_upstream_refuses_missing_native_probe_identity(upstream, tmp_path):
    with pytest.raises(RuntimeError, match="No sealed native probe result"):
        upstream.check(gate_arguments(tmp_path))


def test_upstream_requires_complete_child_exit_evidence(upstream, tmp_path):
    (tmp_path / "child.1").write_text(
        'execve("./timetest", ["./timetest"], 0x0 /* environment */) = 0\n'
    )
    with pytest.raises(RuntimeError, match="Command did not exit normally"):
        upstream.trace_statuses(tmp_path)


def test_upstream_refuses_a_fatal_child_even_with_zero_exit_receipt(upstream, tmp_path):
    (tmp_path / "child.1").write_text(
        'execve("./timetest", ["./timetest"], 0x0 /* environment */) = 0\n'
        "+++ killed by SIGSEGV +++\n+++ exited with 0 +++\n"
    )
    with pytest.raises(RuntimeError, match="Command caught a fatal signal"):
        upstream.trace_statuses(tmp_path)


def test_upstream_accepts_only_complete_expected_child_inventory(upstream, tmp_path):
    for index in range(8):
        (tmp_path / f"child.{index}").write_text(
            'execve("./timetest", ["./timetest"], 0x0 /* environment */) = 0\n+++ exited with 0 +++\n'
        )
    date = tmp_path / "child.8"
    date.write_text(
        'execve("/usr/bin/date", ["date"], 0x0 /* environment */) = 0\n+++ exited with 0 +++\n'
    )
    assert len(upstream.trace_statuses(tmp_path)["timetest_traces"]) == 8
    date.unlink()
    with pytest.raises(RuntimeError, match="ninth section"):
        upstream.trace_statuses(tmp_path)
