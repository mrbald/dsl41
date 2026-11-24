"""The run root's file retention without an engine (DL-557): the names it reads, the horizon,
the runs it keeps, the listing, the bounded step and the round's line. Ages are set with
`os.utime`; no test waits on a clock."""

from __future__ import annotations

import asyncio
import os
from collections import deque
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dsl41 import runner_retention as retention
from dsl41.runner_journal import OpenAttempt, OpenPlan, OpenTable, PendingDispatch
from dsl41.runner_retention import Entry, Listing, Round

T0 = datetime(2026, 7, 1, 8, 0)
#: the horizon of a store whose first row is at T0
BEFORE = (T0 - timedelta(days=1)).replace(tzinfo=UTC).timestamp()
TOKEN = "a" * 64


def _age(path: Path, at: datetime) -> None:
    stamp = at.replace(tzinfo=UTC).timestamp()
    os.utime(path, (stamp, stamp), follow_symlinks=False)


def _run(root: Path, name: str, at: datetime, *, logs: bool = True) -> None:
    """A command run's files, all aged to `at`: its directory, an attempt directory with two
    spool files, and both default logs."""
    attempt = root / "runs" / name / "1"
    attempt.mkdir(parents=True)
    for spool in ("spawn.json", "status.json"):
        (attempt / spool).write_text("{}")
        _age(attempt / spool, at)
    _age(attempt, at)
    _age(attempt.parent, at)
    if logs:
        (root / "logs").mkdir(exist_ok=True)
        for suffix in (".out", ".err"):
            (root / "logs" / f"{name}{suffix}").write_text("output\n")
            _age(root / "logs" / f"{name}{suffix}", at)


def _plan(root: Path, token: str, at: datetime, *, plan_json: bool = True) -> Path:
    plan = root / "plans" / token
    plan.mkdir(parents=True)
    (plan / "jobs.jil").write_text("insert_job: j\n")
    if plan_json:
        (plan / "plan.json").write_text("{}")
        _age(plan / "plan.json", at)
    _age(plan, at)
    return plan


def _always() -> bool:
    return True


def _old(root: Path, going_on: Callable[[], bool] = _always) -> Listing:
    return retention.list_old(root, BEFORE, going_on)


def test_names_parse_from_the_right_in_plain_decimal() -> None:
    assert retention.run_name("j.1") == ("j", 1)
    assert retention.run_name("a.b.12") == ("a.b", 12)
    for foreign in ("j", ".1", "j.", "j.01", "j.+1", "j.x", "j.１", "notes.txt"):
        assert retention.run_name(foreign) is None, foreign
    assert retention.log_run("a.b.3.out") == "a.b.3"
    assert retention.log_run("j.1.err") == "j.1"
    for foreign in ("j.1.log", "j.out", "j.01.err", "out"):
        assert retention.log_run(foreign) is None, foreign
    assert retention.is_token(TOKEN)
    for foreign in ("A" * 64, "a" * 63, "g" * 64, "plan"):
        assert not retention.is_token(foreign)


def test_the_horizon_is_the_earlier_first_row_less_a_day() -> None:
    later = T0 + timedelta(days=3)
    assert retention.horizon(T0, later) == BEFORE
    assert retention.horizon(later, T0) == BEFORE  # a later round can read an earlier row
    assert retention.horizon(None, T0) is None
    assert retention.horizon(T0, None) is None


def test_kept_runs_are_every_open_record_and_each_jobs_last_dispatch() -> None:
    table = OpenTable()
    assert retention.kept_runs(table) == frozenset()
    table.pending[4] = PendingDispatch(4, "p", 2, T0.isoformat())
    table.attempts["a1"] = OpenAttempt(
        attempt="a1",
        job="a",
        run_number=3,
        ordinal=1,
        spool=None,
        seq=5,
        started_at=T0.isoformat(),
        env={},
        after=None,
        dispatch={},
    )
    table.plans["f1"] = OpenPlan(6, "f1", "f", 7, "rerun", [{"step": 0}])
    table.dispatches["d"] = {"run_number": 9}
    assert retention.kept_runs(table) == {"p.2", "a.3", "f.7", "d.9"}


def test_an_old_run_is_one_entry_with_its_spool_and_both_logs(tmp_path: Path) -> None:
    _run(tmp_path, "old.1", T0 - timedelta(days=3))
    _run(tmp_path, "new.1", T0 - timedelta(hours=12))
    assert list(_old(tmp_path).old) == [Entry("run", "old.1")]
    assert Entry("run", "old.1").paths(tmp_path) == (
        tmp_path / "logs" / "old.1.out",
        tmp_path / "logs" / "old.1.err",
        tmp_path / "runs" / "old.1",
    )


def test_one_fresh_file_keeps_the_whole_run(tmp_path: Path) -> None:
    """A fresh log keeps an old directory, a fresh spool file deep inside keeps the run, and a
    log alone, with no directory, is a run of its own."""
    old = T0 - timedelta(days=3)
    _run(tmp_path, "logged.1", old)
    _age(tmp_path / "logs" / "logged.1.err", T0)
    _run(tmp_path, "spooled.1", old)
    _age(tmp_path / "runs" / "spooled.1" / "1" / "status.json", T0)
    (tmp_path / "logs" / "orphan.4.out").write_text("")
    _age(tmp_path / "logs" / "orphan.4.out", old)
    assert list(_old(tmp_path).old) == [Entry("run", "orphan.4")]


def test_foreign_names_stay(tmp_path: Path) -> None:
    old = T0 - timedelta(days=3)
    for name in ("notes", "j.01", "j.x"):
        (tmp_path / "runs" / name).mkdir(parents=True)
        _age(tmp_path / "runs" / name, old)
    (tmp_path / "logs").mkdir()
    for name in ("j.1.log", "service.out"):
        (tmp_path / "logs" / name).write_text("")
        _age(tmp_path / "logs" / name, old)
    _plan(tmp_path, "not-a-token", old)
    listing = _old(tmp_path)
    assert list(listing.old) == [] and listing.errors == 0


def test_a_plan_is_judged_by_its_plan_json_or_by_its_directory(tmp_path: Path) -> None:
    old, fresh = T0 - timedelta(days=3), T0
    renewed = _plan(tmp_path, "b" * 64, old)
    _age(renewed / "plan.json", fresh)
    stale = _plan(tmp_path, "c" * 64, old)
    _age(stale, fresh)  # a captured part written later does not count; plan.json does
    _plan(tmp_path, "d" * 64, old, plan_json=False)
    _plan(tmp_path, "e" * 64, fresh, plan_json=False)
    (tmp_path / "plans" / ("f" * 64)).write_text("")  # a file in a plan's place
    _age(tmp_path / "plans" / ("f" * 64), old)
    assert sorted(entry.name[0] for entry in _old(tmp_path).old) == ["c", "d", "f"]


def test_an_empty_or_missing_root_lists_nothing(tmp_path: Path) -> None:
    listing = _old(tmp_path / "absent")
    assert (list(listing.old), listing.errors, listing.error) == ([], 0, None)


def test_the_listing_reaches_the_end_past_an_entry_it_cannot_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run with a part that cannot be read is not reported and counts one error; the runs
    and plans after it are still read. A directory that cannot be listed is an error too."""
    old = T0 - timedelta(days=3)
    for name in ("a.1", "b.1", "c.1"):
        _run(tmp_path, name, old)
    _plan(tmp_path, TOKEN, old)
    _plan(tmp_path, "b" * 64, old)
    real_tree, real_plan = retention.tree_mtime, retention.plan_mtime

    def tree(path: Path) -> float | None:
        if path.name in ("b.1", "c.1.err"):
            raise PermissionError(13, "Permission denied")
        return real_tree(path)

    def plan(path: Path) -> float | None:
        if path.name == TOKEN:
            raise PermissionError(13, "Permission denied")
        return real_plan(path)

    monkeypatch.setattr(retention, "tree_mtime", tree)
    monkeypatch.setattr(retention, "plan_mtime", plan)
    listing = _old(tmp_path)
    assert list(listing.old) == [Entry("run", "a.1"), Entry("plan", "b" * 64)]
    assert listing.errors == 3
    assert listing.error == f"{tmp_path / 'runs' / 'b.1'}: [Errno 13] Permission denied"

    other = tmp_path / "other"
    other.mkdir()
    (other / "runs").write_text("")  # not a directory: it cannot be listed
    unlisted = _old(other)
    assert unlisted.errors == 1 and unlisted.error is not None
    assert unlisted.error.startswith(str(other / "runs"))


def test_a_run_whose_part_fails_after_its_other_parts_were_read_stays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory is read first; a log that fails then marks the run, and a later log of
    the same run does not undo the mark."""
    _run(tmp_path, "a.1", T0 - timedelta(days=3))
    real = retention.tree_mtime

    def tree(path: Path) -> float | None:
        if path.name == "a.1.err":
            raise PermissionError(13, "Permission denied")
        return real(path)

    monkeypatch.setattr(retention, "tree_mtime", tree)
    real_names = retention._names

    def ordered(directory: Path, going_on: Callable[[], bool], listing: Listing) -> Iterator[str]:
        yield from sorted(real_names(directory, going_on, listing))  # the failing log first

    monkeypatch.setattr(retention, "_names", ordered)
    listing = _old(tmp_path)
    assert list(listing.old) == [] and listing.errors == 1


def test_a_run_gone_while_it_was_read_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, "a.1", T0 - timedelta(days=3), logs=False)
    monkeypatch.setattr(retention, "tree_mtime", lambda path: None)
    assert list(_old(tmp_path).old) == []


def test_a_listing_stops_between_two_pages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retention, "PAGE", 2)
    for name in ("a.1", "b.1", "c.1", "d.1", "e.1"):
        _run(tmp_path, name, T0 - timedelta(days=3), logs=False)
    looks: list[int] = []

    def going_on() -> bool:
        looks.append(1)
        return len(looks) < 2

    with pytest.raises(retention.Stopped):
        _old(tmp_path, going_on)
    assert len(looks) == 2  # once after each page of two; the second says stop


def test_the_tree_mtime_is_the_newest_and_follows_no_link(tmp_path: Path) -> None:
    run = tmp_path / "j.1"
    (run / "1").mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("")
    (run / "1" / "link").symlink_to(target)
    _age(run / "1" / "link", T0 - timedelta(days=5))
    _age(run / "1", T0 - timedelta(days=4))
    _age(run, T0 - timedelta(days=3))
    _age(target, T0 + timedelta(days=1))
    assert retention.tree_mtime(run) == (T0 - timedelta(days=3)).replace(tzinfo=UTC).timestamp()
    assert retention.tree_mtime(tmp_path / "absent") is None
    assert retention.entry_mtime(tmp_path, Entry("run", "absent.1")) is None
    assert retention.plan_mtime(tmp_path / "absent") is None


def _round(*entries: Entry) -> Round:
    found = Round(1, T0)
    found.listed(Listing(deque(entries)))
    return found


def _step(root: Path, round: Round, **options: object) -> None:
    arguments: dict[str, object] = {
        "before": BEFORE,
        "runs": frozenset(),
        "plans": False,
        "expired": None,
    } | options
    retention.step(root, round, **arguments)  # type: ignore[arg-type]


def test_a_step_removes_at_most_its_bound_and_owes_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retention, "STEP", 2)
    names = ["a.1", "b.1", "c.1"]
    for name in names:
        _run(tmp_path, name, T0 - timedelta(days=3))
    owed = _round(*(Entry("run", name) for name in names))
    _step(tmp_path, owed)
    assert list(owed.owed) == [Entry("run", "c.1")]
    assert sorted(os.listdir(tmp_path / "runs")) == ["c.1"]
    assert sorted(os.listdir(tmp_path / "logs")) == ["c.1.err", "c.1.out"]
    assert owed.done == {"runs": 2, "logs": 4, "plans": 0}
    _step(tmp_path, owed)
    assert not owed.owed and os.listdir(tmp_path / "runs") == []


def test_a_step_drops_what_is_kept_or_renewed_now(tmp_path: Path) -> None:
    """A kept run, a plan while plans are kept, an entry renewed since the listing, and one
    already gone are dropped and stay; with no horizon every entry is dropped."""
    old = T0 - timedelta(days=3)
    for name in ("kept.1", "renewed.1", "free.1"):
        _run(tmp_path, name, old)
    _age(tmp_path / "logs" / "renewed.1.out", T0)
    _plan(tmp_path, TOKEN, old)
    entries = [Entry("run", n) for n in ("kept.1", "renewed.1", "gone.1", "free.1")]
    owed = _round(*entries, Entry("plan", TOKEN))
    _step(tmp_path, owed, runs=frozenset({"kept.1"}), plans=True)
    assert not owed.owed
    assert sorted(os.listdir(tmp_path / "runs")) == ["kept.1", "renewed.1"]
    assert os.listdir(tmp_path / "plans") == [TOKEN]
    _step(tmp_path, owed := _round(Entry("plan", TOKEN)), before=None)
    assert not owed.owed and os.listdir(tmp_path / "plans") == [TOKEN]
    _step(tmp_path, owed := _round(Entry("plan", TOKEN)))
    assert os.listdir(tmp_path / "plans") == [] and owed.done["plans"] == 1


def test_a_step_counts_an_entry_it_cannot_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable(root: Path, entry: Entry) -> float | None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(retention, "entry_mtime", unreadable)
    owed = _round(Entry("run", "a.1"), Entry("plan", TOKEN))
    _step(tmp_path, owed)
    assert owed.errors == 2
    assert owed.error == f"{tmp_path / 'runs' / 'a.1'}: [Errno 13] Permission denied"


def test_removal_takes_files_and_whole_directories(tmp_path: Path) -> None:
    _run(tmp_path, "a.1", T0)
    owed = Round(1, T0)
    retention.discard(tmp_path, Entry("run", "a.1"), None, owed)
    assert os.listdir(tmp_path / "runs") == os.listdir(tmp_path / "logs") == []
    assert owed.done == {"runs": 1, "logs": 2, "plans": 0}


@pytest.mark.parametrize("expired", [False, True])
def test_an_entry_that_is_a_link_is_not_the_engines(tmp_path: Path, expired: bool) -> None:
    """A linked run directory, a linked log and a linked plan token whose targets are old are
    neither listed nor removed nor moved: target and link survive. A run with a linked log
    stays whole; an entry that became a link after the listing is dropped by the step."""
    root, outside = tmp_path / "root", tmp_path / "outside"
    old = T0 - timedelta(days=3)
    _run(outside, "x.1", old)
    _plan(outside, TOKEN, old)
    (root / "runs").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "plans").mkdir()
    (root / "runs" / "l.1").symlink_to(outside / "runs" / "x.1")
    _run(root, "m.1", old, logs=False)
    (root / "logs" / "m.1.out").symlink_to(outside / "logs" / "x.1.out")
    (root / "plans" / TOKEN).symlink_to(outside / "plans" / TOKEN)
    assert list(_old(root).old) == [Entry("run", "m.1")]  # its directory is real
    owed = _round(Entry("run", "l.1"), Entry("run", "m.1"), Entry("plan", TOKEN))
    target = tmp_path / "expired"
    target.mkdir()
    _step(root, owed, expired=target if expired else None)
    assert sorted(os.listdir(root / "runs")) == ["l.1", "m.1"]
    assert os.listdir(root / "logs") == ["m.1.out"] and os.listdir(root / "plans") == [TOKEN]
    assert (outside / "runs" / "x.1" / "1" / "spawn.json").exists()
    assert (outside / "logs" / "x.1.out").exists() and (outside / "plans" / TOKEN).exists()
    assert os.listdir(target) == [] and owed.done == {"runs": 0, "logs": 0, "plans": 0}


def test_a_failed_removal_leaves_the_rest_and_is_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, "a.1", T0)

    def refused(path: Path) -> None:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(retention.os, "unlink", refused)
    owed = Round(1, T0)
    retention.discard(tmp_path, Entry("run", "a.1"), None, owed)
    assert owed.errors == 1 and sorted(os.listdir(tmp_path / "logs")) == ["a.1.err", "a.1.out"]
    assert (tmp_path / "runs" / "a.1").is_dir()


def test_a_move_keeps_place_and_name_logs_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, expired = tmp_path / "root", tmp_path / "expired"
    expired.mkdir()
    _run(root, "j.1", T0)
    _plan(root, TOKEN, T0)
    moves: list[tuple[str, str]] = []
    real_link, real_rename = os.link, os.rename

    def link(source: Path, target: Path) -> None:
        moves.append(("link", Path(source).name))
        real_link(source, target)

    def rename(source: Path, target: Path) -> None:
        moves.append(("rename", Path(source).name))
        real_rename(source, target)

    monkeypatch.setattr(retention.os, "link", link)
    monkeypatch.setattr(retention.os, "rename", rename)
    owed = Round(1, T0)
    retention.discard(root, Entry("run", "j.1"), expired, owed)
    retention.discard(root, Entry("plan", TOKEN), expired, owed)
    # a file is linked there and unlinked here; a directory is renamed
    assert moves == [
        ("link", "j.1.out"),
        ("link", "j.1.err"),
        ("rename", "j.1"),
        ("rename", TOKEN),
    ]
    assert sorted(os.listdir(expired / "logs")) == ["j.1.err", "j.1.out"]
    assert sorted(os.listdir(expired / "runs" / "j.1" / "1")) == ["spawn.json", "status.json"]
    assert os.listdir(expired / "plans") == [TOKEN]
    assert os.listdir(root / "runs") == os.listdir(root / "logs") == os.listdir(root / "plans")
    assert owed.done == {"runs": 1, "logs": 2, "plans": 1}
    assert owed.summary(str(expired)) == (
        f"dsl41: retention moved runs=1 logs=2 plans=1 to {expired} (DL-557)"
    )


def test_a_move_to_a_missing_directory_leaves_the_entry_and_deletes_nothing(
    tmp_path: Path,
) -> None:
    root, expired = tmp_path / "root", tmp_path / "expired"
    _run(root, "j.1", T0)
    owed = Round(1, T0)
    retention.discard(root, Entry("run", "j.1"), expired, owed)
    assert owed.errors == 1 and (root / "runs" / "j.1" / "1" / "spawn.json").exists()
    assert sorted(os.listdir(root / "logs")) == ["j.1.err", "j.1.out"]
    line = owed.summary(str(expired))
    # the error names the file that stayed and, through the system's text, the cause
    assert line is not None and "errors=1, the first: " in line and str(expired / "logs") in line


@pytest.mark.parametrize("taken", ["logs/j.1.out", "runs/j.1", "plans/" + TOKEN])
def test_a_move_never_replaces_a_name_already_there(tmp_path: Path, taken: str) -> None:
    """A name already under `expired_dir` keeps its bytes; nothing of the entry moves, so a
    run moves whole or not at all, and the round counts one error naming the place."""
    root, expired = tmp_path / "root", tmp_path / "expired"
    _run(root, "j.1", T0)
    _plan(root, TOKEN, T0)
    there = expired / taken
    there.parent.mkdir(parents=True)
    if taken.startswith("logs"):
        there.write_text("an earlier generation\n")
    else:
        there.mkdir()
        (there / "kept").write_text("an earlier generation\n")
    entry = Entry("plan", TOKEN) if taken.startswith("plans") else Entry("run", "j.1")
    owed = Round(1, T0)
    retention.discard(root, entry, expired, owed)
    assert owed.errors == 1 and str(there) in (owed.error or "")
    assert (there if taken.startswith("logs") else there / "kept").read_text() == (
        "an earlier generation\n"
    )
    assert sorted(os.listdir(root / "logs")) == ["j.1.err", "j.1.out"]
    assert os.listdir(root / "runs") == ["j.1"] and os.listdir(root / "plans") == [TOKEN]
    assert owed.done == {"runs": 0, "logs": 0, "plans": 0}


def test_every_destination_name_is_checked_even_for_a_part_the_run_lacks(tmp_path: Path) -> None:
    """An earlier `j.1.out` alone under `expired_dir`, and a run `j.1` with a spool and no
    log of that name: nothing moves, one error."""
    root, expired = tmp_path / "root", tmp_path / "expired"
    _run(root, "j.1", T0, logs=False)
    (expired / "logs").mkdir(parents=True)
    (expired / "logs" / "j.1.out").write_text("an earlier generation\n")
    owed = Round(1, T0)
    retention.discard(root, Entry("run", "j.1"), expired, owed)
    assert owed.errors == 1 and os.listdir(root / "runs") == ["j.1"]
    assert not (expired / "runs").exists()
    assert (expired / "logs" / "j.1.out").read_text() == "an earlier generation\n"


def test_a_name_that_appears_after_the_check_is_not_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The target appears between the check and the move: `link` refuses it, its bytes stay,
    the source stays, and the error is counted."""
    root, expired = tmp_path / "root", tmp_path / "expired"
    expired.mkdir()
    _run(root, "j.1", T0)
    real = os.link

    def link(source: Path, target: Path) -> None:
        Path(target).write_text("arrived meanwhile\n")  # the hook: after the check
        real(source, target)

    monkeypatch.setattr(retention.os, "link", link)
    owed = Round(1, T0)
    retention.discard(root, Entry("run", "j.1"), expired, owed)
    assert owed.errors == 1 and "File exists" in (owed.error or "")
    assert (expired / "logs" / "j.1.out").read_text() == "arrived meanwhile\n"
    assert sorted(os.listdir(root / "logs")) == ["j.1.err", "j.1.out"]
    assert os.listdir(root / "runs") == ["j.1"] and owed.done["logs"] == 0


def test_a_rename_that_fails_partway_leaves_what_moved_and_a_later_step_moves_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory's rename fails across file systems after the logs moved: the logs stay
    under `expired_dir`, the directory stays here, one error. A later step finds the logs'
    names taken and moves nothing; once the archiver has taken them, it moves the
    directory."""
    root, expired = tmp_path / "root", tmp_path / "expired"
    expired.mkdir()
    old = T0 - timedelta(days=3)
    _run(root, "j.1", old)
    real = os.rename

    def rename(source: Path, target: Path) -> None:
        if Path(source).name == "j.1":
            raise OSError(18, "Cross-device link", str(source))
        real(source, target)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(retention.os, "rename", rename)
        first = _round(Entry("run", "j.1"))
        _step(root, first, expired=expired)
    assert first.errors == 1 and "Cross-device link" in (first.error or "")
    assert sorted(os.listdir(expired / "logs")) == ["j.1.err", "j.1.out"]
    assert os.listdir(root / "runs") == ["j.1"] and os.listdir(root / "logs") == []
    assert first.done == {"runs": 0, "logs": 2, "plans": 0}
    later = Round(2, T0)
    later.listed(_old(root))
    assert list(later.owed) == [Entry("run", "j.1")]
    _step(root, later, expired=expired)
    assert later.errors == 1 and os.listdir(root / "runs") == ["j.1"]
    for name in os.listdir(expired / "logs"):
        os.unlink(expired / "logs" / name)  # the archiver takes them away
    again = _round(Entry("run", "j.1"))
    _step(root, again, expired=expired)
    assert os.listdir(root / "runs") == [] and os.listdir(expired / "runs") == ["j.1"]
    assert again.done == {"runs": 1, "logs": 0, "plans": 0} and again.errors == 0


def test_a_step_removes_at_most_one_plan(tmp_path: Path) -> None:
    """A plan holds every captured part: one step removes one, after any runs, and owes the
    next."""
    old = T0 - timedelta(days=3)
    _run(tmp_path, "a.1", old)
    for token in ("b" * 64, "c" * 64):
        _plan(tmp_path, token, old)
    owed = _round(Entry("run", "a.1"), Entry("plan", "b" * 64), Entry("plan", "c" * 64))
    _step(tmp_path, owed)
    assert os.listdir(tmp_path / "runs") == [] and os.listdir(tmp_path / "plans") == ["c" * 64]
    assert list(owed.owed) == [Entry("plan", "c" * 64)]
    _step(tmp_path, owed)
    assert os.listdir(tmp_path / "plans") == [] and not owed.owed


def test_the_round_line_is_said_only_for_work_or_errors() -> None:
    quiet = Round(1, T0)
    assert quiet.summary(None) is None
    quiet.listed(Listing(deque(), 2, "/root/runs/x.1: Permission denied"))
    assert quiet.summary(None) == (
        "dsl41: retention removed runs=0 logs=0 plans=0;"
        " errors=2, the first: /root/runs/x.1: Permission denied (DL-557)"
    )


class _Loop:
    """A stand-in for the loop's `call_soon_threadsafe`: each handed-over call is held, and
    the test runs it."""

    def __init__(self) -> None:
        self.calls: list[tuple[Callable[..., None], tuple[object, ...]]] = []

    def call_soon_threadsafe(self, callback: Callable[..., None], *args: object) -> None:
        self.calls.append((callback, args))


def test_a_worker_hands_its_listing_over_and_a_stopped_one_says_nothing(tmp_path: Path) -> None:
    """The listing is handed to the loop as one call; a worker asked to stop before its
    report, even after its last page, hands over nothing."""
    _run(tmp_path, "a.1", T0 - timedelta(days=3))
    reports: list[tuple[int, Listing]] = []

    def report(number: int, listing: Listing) -> None:
        reports.append((number, listing))

    loop = _Loop()
    worker = retention.RetentionWorker(tmp_path, BEFORE, 7, report)
    worker._loop = loop  # type: ignore[assignment]
    worker._main()
    stopped = retention.RetentionWorker(tmp_path, BEFORE, 8, report)
    stopped._loop = loop  # type: ignore[assignment]
    stopped.stop()
    stopped._main()  # a listing shorter than a page: the look before the report stops it
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(retention, "PAGE", 1)
        stopped._main()  # the look after the first page stops it
    assert [args[0] for _, args in loop.calls] == [7]
    for callback, args in loop.calls:
        callback(*args)
    assert [(number, list(listing.old)) for number, listing in reports] == [
        (7, [Entry("run", "a.1")])
    ]


def test_a_started_worker_runs_on_its_own_thread(tmp_path: Path) -> None:
    reports: list[int] = []

    async def scenario() -> None:
        idle = retention.RetentionWorker(tmp_path, BEFORE, 1, lambda n, _: reports.append(n))
        assert idle.join(0)  # never started
        worker = retention.RetentionWorker(tmp_path, BEFORE, 2, lambda n, _: reports.append(n))
        worker.start()
        assert worker.join(5)
        await asyncio.sleep(0)  # the handed-over call was queued before the join returned

    asyncio.run(scenario())
    assert reports == [2]


class _Failing:
    """A directory iterator that yields one entry and then fails."""

    def __init__(self, first: os.DirEntry[str]) -> None:
        self.first: os.DirEntry[str] | None = first

    def __enter__(self) -> _Failing:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self) -> _Failing:
        return self

    def __next__(self) -> os.DirEntry[str]:
        if self.first is not None:
            first, self.first = self.first, None
            return first
        raise OSError(5, "Input/output error")


def test_a_directory_that_fails_while_read_is_counted_and_the_listing_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = T0 - timedelta(days=3)
    _run(tmp_path, "a.1", old, logs=False)
    _run(tmp_path, "b.1", old, logs=False)
    _plan(tmp_path, TOKEN, old)
    real = os.scandir

    def scandir(path: Path) -> object:
        if Path(path).name == "runs":
            with real(path) as entries:
                first = sorted(entries, key=lambda entry: entry.name)[0]
            return _Failing(first)
        return real(path)

    monkeypatch.setattr(retention.os, "scandir", scandir)
    listing = _old(tmp_path)
    assert list(listing.old) == [Entry("run", "a.1"), Entry("plan", TOKEN)]
    assert listing.errors == 1
    assert listing.error == f"{tmp_path / 'runs'}: [Errno 5] Input/output error"


def test_a_worker_whose_loop_closed_reports_to_nobody(tmp_path: Path) -> None:
    reports: list[int] = []
    worker = retention.RetentionWorker(tmp_path, BEFORE, 1, lambda n, _: reports.append(n))

    async def start() -> None:
        worker._loop = asyncio.get_running_loop()

    asyncio.run(start())  # the loop is closed when the thread hands over
    worker._main()
    assert reports == []
