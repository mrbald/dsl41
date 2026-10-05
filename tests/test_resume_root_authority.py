"""`run --resume` refuses a root its anchor does not name (period-model
ss1.3, ss11 step 2; PR-57; DL-224).

The rule has two halves. NAMED: some registry row, the head's root, or a
`claimed` head's target equals the resumed root. OWNED: the registry row for
the period of the root's newest opened segment names this root, or, with no
row yet, the head is this root's claim. Both run before any repair of the
root, so a refused resume leaves every byte and every directory entry where
it was, apart from the two lock files it may create or take.

House style follows test_boundary.py and test_estate.py: every refusal
asserts the fragment only its own rule produces, and every refusal has a
passing counterpart at the recorded path beside it.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import stat
from pathlib import Path

import pytest

from dsl41 import boundary, runner_startup
from dsl41.boundary import (
    ANCHOR_LOCK_NAME,
    Anchor,
    ClaimedHead,
    ClosedHead,
    EstateAnchor,
    OpenHead,
    RootAuthorityError,
    default_anchor_dir,
    read_seal,
    resume_root_refusal,
    same_root,
)
from dsl41.ast_jil import parse
from dsl41.ir import lower_catalog
from dsl41.period import wal_path
from dsl41.runner_adapters import FakeAdapter, FileWatcherAdapter, LocalCommandAdapter
from dsl41.runner_clock import EngineError, RealClock, VirtualClock
from dsl41.runner_ledger import LOCK_NAME, acquire_run_root
from dsl41.runner_startup import resume_run
from test_boundary import (
    C1_JIL,
    C2_JIL,
    T0,
    _catalog,
    _close,
    _crashed_between_the_segment_and_the_cas,
    _genesis,
    _request,
    _seal,
    _stage,
)
from test_estate import (
    _estate,
    _invoke,
    _native_root,
    _open_in_place,
    _roll,
    _seal_offline,
    _Stopped,
)
from test_runner_leadership import engine, cli, wait_for
from test_runner_supervisor import _kill_group

#: the only entries a refused resume may create or touch (DL-224)
_LOCKS = {LOCK_NAME, ANCHOR_LOCK_NAME}


def _tree(*tops: Path) -> dict[tuple[int, str], tuple[str, int, bytes]]:
    """Every entry under `tops`: its kind, its mode and, for a file, its
    bytes. The lock files are left out, and so are the modes of the tops
    themselves: the 0700 tightening and the locks are the permitted writes."""
    seen: dict[tuple[int, str], tuple[str, int, bytes]] = {}
    for index, top in enumerate(tops):
        for dirpath, dirnames, filenames in os.walk(top):
            for name in dirnames + filenames:
                path = Path(dirpath) / name
                if name in _LOCKS:
                    continue
                info = path.lstat()
                if path.is_symlink():
                    seen[(index, str(path.relative_to(top)))] = ("link", 0, b"")
                elif stat.S_ISSOCK(info.st_mode) or stat.S_ISFIFO(info.st_mode):
                    # never copied (`_backup_copy`), so one here APPEARED
                    seen[(index, str(path.relative_to(top)))] = ("special", info.st_mode, b"")
                elif path.is_dir():
                    seen[(index, str(path.relative_to(top)))] = ("dir", info.st_mode, b"")
                else:
                    body = path.read_bytes()
                    seen[(index, str(path.relative_to(top)))] = ("file", info.st_mode, body)
    return seen


def _skip_sockets(directory: str, names: list[str]) -> set[str]:
    """`copytree`'s ignore: the sockets and FIFOs a backup tool skips."""
    skipped = set()
    for name in names:
        mode = os.lstat(os.path.join(directory, name)).st_mode
        if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode):
            skipped.add(name)
    return skipped


def _backup_copy(source: Path, target: Path) -> None:
    """A tree copied the way a backup tool copies it: regular files,
    directories and symlinks. A live root's `control.sock` or
    `supervisor.sock` is not copied, and `copytree` could not copy it."""
    shutil.copytree(source, target, symlinks=True, ignore=_skip_sockets)


def _copy(root: Path, to: Path) -> Path:
    """The root and its sibling anchor, copied to `to` and `to`'s sibling."""
    _backup_copy(root, to)
    _backup_copy(default_anchor_dir(root), default_anchor_dir(to))
    return to


def test_pr57_the_backup_copy_skips_sockets_and_fifos(short_root: Path) -> None:
    """Pinned on every platform: a bound socket and a FIFO in the source
    are not copied; files, directories and symlinks are."""
    source = short_root / "src"
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "file").write_bytes(b"kept")
    (source / "link").symlink_to("sub/file")
    os.mkfifo(source / "pipe")
    bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        bound.bind(str(source / "control.sock"))
        assert stat.S_ISSOCK(os.lstat(source / "control.sock").st_mode)
        _backup_copy(source, short_root / "dst")
    finally:
        bound.close()
    target = short_root / "dst"
    assert (target / "sub" / "file").read_bytes() == b"kept"
    assert (target / "link").is_symlink()
    assert not os.path.lexists(target / "control.sock")
    assert not os.path.lexists(target / "pipe")


def _resume_virtual(run_root: Path, anchor_dir: Path | None, text: str = C1_JIL):
    catalog, _ = _catalog(text)
    return asyncio.run(
        resume_run(
            catalog,
            run_root,
            clock=VirtualClock(start=T0),
            adapters={"CMD": FakeAdapter(default=None)},
            anchor_dir=anchor_dir,
        )
    )


def _resume_real(run_root: Path, anchor_dir: Path, jil: Path) -> None:
    """A real-clock resume that opens its period and stops, the way
    test_estate.py's `_open_in_place` does, with the anchor named."""
    catalog = lower_catalog([parse(jil.read_text(), file=str(jil))])
    opened = asyncio.run(
        resume_run(
            catalog,
            run_root,
            clock=RealClock(),
            adapters={"CMD": LocalCommandAdapter(), "FW": FileWatcherAdapter()},
            anchor_dir=anchor_dir,
        )
    )
    asyncio.run(opened.shutdown())
    assert opened.journal is not None
    opened.journal.close()


def _root_with_head(run_root: Path, head: str) -> str:
    """A period-1 root whose lineage head is `head`, built by the real
    operations. Returns the catalog a resume of it runs under."""
    engine_ = _genesis(run_root)
    if head == "open":
        _close(engine_)
        return C1_JIL
    asyncio.run(_seal(engine_, _request(engine_, _stage(run_root, C2_JIL))))
    _close(engine_)
    if head == "claimed":
        # the opener's crash window after the claim and before the segment
        seal = read_seal(run_root, 1)
        anchor = EstateAnchor(default_anchor_dir(run_root))
        anchor.acquire()
        anchor.claim_successor(
            estate_id=seal.estate_id,
            seal_digest=seal.digest,
            next_period=2,
            target_root=run_root,
        )
        anchor.release()
    return C2_JIL


# ------------------------------------------------ NAMED: relocated copies


@pytest.mark.parametrize("head", ["open", "closed", "claimed"])
@pytest.mark.parametrize("which_anchor", ["copied", "original"])
def test_pr57_a_relocated_copy_is_refused_and_left_untouched(
    tmp_path: Path, head: str, which_anchor: str
) -> None:
    """A copy resumed against its own copied anchor, or against the
    original one (split brain), is refused. Nothing under the copy or
    under the anchor it named moves, and no entry appears but the locks."""
    original = tmp_path / "a"
    text = _root_with_head(original, head)
    copy = _copy(original, tmp_path / "c")
    anchor_dir = default_anchor_dir(copy if which_anchor == "copied" else original)
    before = _tree(copy, anchor_dir)

    with pytest.raises(EngineError) as refused:
        _resume_virtual(copy, anchor_dir, text)
    message = str(refused.value)
    assert "this anchor does not name" in message
    assert str(copy.resolve()) in message and str(original.resolve()) in message
    assert str(anchor_dir / "anchor.json") in message
    assert "a restore must land at the recorded path" in message
    assert "period-model ss1.3" in message
    assert _tree(copy, anchor_dir) == before

    # the passing counterpart: the same state at its recorded path resumes
    _close(_resume_virtual(original, default_anchor_dir(original), text))


def test_pr57_a_refused_resume_holds_neither_lock(tmp_path: Path) -> None:
    """Both locks are released on the refusal path: this same process can
    take each again, which `flock` on a leaked descriptor would refuse."""
    original = tmp_path / "a"
    _root_with_head(original, "open")
    copy = _copy(original, tmp_path / "c")
    with pytest.raises(EngineError, match="this anchor does not name"):
        _resume_virtual(copy, default_anchor_dir(copy))
    lock = acquire_run_root(copy)
    lock.release()
    anchor = EstateAnchor(default_anchor_dir(copy))
    anchor.acquire()
    anchor.release()


# ------------------------------------------- the refusal precedes repair


def test_pr57_a_torn_tail_in_a_relocated_copy_is_not_repaired(tmp_path: Path) -> None:
    """Two segments and a torn final line: at the recorded path resume cuts
    the fragment; in a copy the refusal comes first and the fragment stays."""
    original = tmp_path / "a"
    engine_ = _genesis(original)
    asyncio.run(_seal(engine_, _request(engine_, _stage(original, C2_JIL))))
    _close(engine_)
    _close(_resume_virtual(original, default_anchor_dir(original), C2_JIL))
    torn = b'{"rec": "lead'
    with wal_path(original, 2).open("ab") as handle:
        handle.write(torn)
    copy = _copy(original, tmp_path / "c")
    before = _tree(copy, default_anchor_dir(copy))

    with pytest.raises(EngineError, match="this anchor does not name"):
        _resume_virtual(copy, default_anchor_dir(copy), C2_JIL)
    assert _tree(copy, default_anchor_dir(copy)) == before
    assert wal_path(copy, 2).read_bytes().endswith(torn)

    _close(_resume_virtual(original, default_anchor_dir(original), C2_JIL))
    assert torn not in wal_path(original, 2).read_bytes()  # the repair ran there


def test_pr57_an_empty_successor_segment_in_a_relocated_copy_is_not_dropped(
    tmp_path: Path,
) -> None:
    """A committed seal and an empty `wal/000002.jsonl`: at the recorded
    path resume drops the file and re-opens it; in a copy the file stays."""
    original = tmp_path / "a"
    _root_with_head(original, "closed")
    wal_path(original, 2).touch()
    copy = _copy(original, tmp_path / "c")
    before = _tree(copy, default_anchor_dir(copy))

    with pytest.raises(EngineError, match="this anchor does not name"):
        _resume_virtual(copy, default_anchor_dir(copy), C2_JIL)
    assert _tree(copy, default_anchor_dir(copy)) == before
    assert wal_path(copy, 2).read_bytes() == b""

    _close(_resume_virtual(original, default_anchor_dir(original), C2_JIL))
    assert wal_path(original, 2).stat().st_size > 0  # dropped and re-opened there


# -------------------------------------------- OWNED: the misplaced restore


def _rolled_lineage(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Period 1 in A, attested; a physical roll opens period 2 in B. The
    anchor holds {1: A, 2: B} and the head is `open(2, B)`."""
    c1, c2, _ = _estate(tmp_path / "estate")
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    _native_root(root_a, c1)
    assert _seal_offline(root_a, c2).exit_code == 0
    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0
    anchor_dir = default_anchor_dir(root_a)
    _roll(root_b, anchor_dir, c2)
    return root_a, root_b, anchor_dir, c2


def test_pr57_a_restore_misplaced_at_an_older_roots_path_is_refused(tmp_path: Path) -> None:
    """B's tree put back at A's path passes NAMED through period 1's row
    and fails OWNED: the period its segment holds is registered to B."""
    root_a, root_b, anchor_dir, c2 = _rolled_lineage(tmp_path)
    stored = EstateAnchor(anchor_dir).read()
    assert stored is not None and isinstance(stored.head, OpenHead)
    assert stored.head.period_id == 2
    root_a.rename(tmp_path / "a-moved")
    _backup_copy(root_b, root_a)
    before = _tree(root_a, anchor_dir)

    with pytest.raises(EngineError) as refused:
        _resume_real(root_a, anchor_dir, c2)
    message = str(refused.value)
    assert f"does not hold period 2 for {root_a.resolve()}" in message
    assert f"row for period 2 names {root_b.resolve()}" in message
    assert "a restore must land at the recorded path" in message
    assert _tree(root_a, anchor_dir) == before

    _resume_real(root_b, anchor_dir, c2)  # the passing counterpart: B itself


def test_pr57_a_historical_root_keeps_todays_refusal(tmp_path: Path) -> None:
    """A after the roll: the anchor names it and it owns period 1, so it
    reaches the refusal it reached before this rule, in the same words."""
    root_a, root_b, anchor_dir, c2 = _rolled_lineage(tmp_path)
    with pytest.raises(EngineError) as refused:
        _resume_real(root_a, anchor_dir, c2)
    message = str(refused.value)
    assert "cannot claim the successor of" in message
    assert f"the head is open(period 2, {root_b.resolve()})" in message
    assert "this anchor does not" not in message


def test_pr57_a_reclaimed_roll_target_is_refused_by_the_rule(tmp_path: Path) -> None:
    """B wrote its opening segment and died before the head moved; the
    operator reclaimed the claim. B is no longer a root the anchor names."""
    c1, c2, _ = _estate(tmp_path / "estate")
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    _native_root(root_a, c1)
    assert _seal_offline(root_a, c2).exit_code == 0
    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0
    anchor_dir = default_anchor_dir(root_a)
    with pytest.raises(_Stopped):
        _roll(root_b, anchor_dir, c2, stop_at="after_opening_segment")
    assert wal_path(root_b, 2).exists()
    reclaimed = _invoke(
        "estate", "reclaim", "--estate-anchor", str(anchor_dir), "--force", "--claimed-actor", "ops"
    )
    assert reclaimed.exit_code == 0, reclaimed.output
    stored = EstateAnchor(anchor_dir).read()
    assert stored is not None and isinstance(stored.head, ClosedHead)
    before = _tree(root_b, anchor_dir)

    with pytest.raises(EngineError) as refused:
        _resume_real(root_b, anchor_dir, c2)
    message = str(refused.value)
    assert f"this anchor does not name {root_b.resolve()}" in message
    assert "a target whose claim was reclaimed" in message
    assert _tree(root_b, anchor_dir) == before


# ------------------------------- the no-row claim exception, both ways


def test_pr57_an_opening_that_crashed_before_its_cas_resumes_through_its_claim(
    tmp_path: Path,
) -> None:
    """A real in-place opening stopped between its segment and the head
    CAS: no registry row for period 2, and the head is this root's claim.
    OWNED admits it through the claim, and resume finishes the opening."""
    root = tmp_path / "a"
    with _crashed_between_the_segment_and_the_cas(root) as (anchor, _, _):
        stored = anchor.read()
        assert stored is not None and isinstance(stored.head, ClaimedHead)
        assert stored.row(2) is None and wal_path(root, 2).exists()
    _close(_resume_virtual(root, default_anchor_dir(root), C2_JIL))
    stored = EstateAnchor(default_anchor_dir(root)).read()
    assert stored is not None and isinstance(stored.head, OpenHead)
    assert stored.head.period_id == 2 and stored.row(2) is not None


def test_pr57_a_reclaimed_in_place_opening_is_refused_by_owned(tmp_path: Path) -> None:
    """The same crash, then the claim reclaimed. The root is still NAMED
    through period 1's row, but its abandoned period-2 segment has no row
    and the head is no longer its claim: OWNED refuses, touching nothing."""
    root = tmp_path / "a"
    with _crashed_between_the_segment_and_the_cas(root) as (anchor, committed, _):
        anchor.reclaim(estate_id=committed.seal.estate_id, claimed_actor="ops")
        stored = anchor.read()
        assert stored is not None and isinstance(stored.head, ClosedHead)
    before = _tree(root, default_anchor_dir(root))

    with pytest.raises(RootAuthorityError) as refused:
        _resume_virtual(root, default_anchor_dir(root), C2_JIL)
    message = str(refused.value)
    assert (
        f"does not hold period 2 for {root.resolve()} -- no registry row names period 2" in message
    )
    assert "not a claim by this root" in message
    assert _tree(root, default_anchor_dir(root)) == before


# -------------------------- OWNED through the never-opened fallback


def _misplaced_restore(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """B's tree put back at A's path, the anchor still {1: A, 2: B}."""
    root_a, root_b, anchor_dir, c2 = _rolled_lineage(tmp_path)
    root_a.rename(tmp_path / "a-moved")
    _backup_copy(root_b, root_a)
    return root_a, root_b, anchor_dir, c2


@pytest.mark.parametrize("newest", [b"", b'{"rec": "seg'], ids=["empty", "torn"])
def test_pr57_a_misplaced_restore_with_a_never_opened_segment_is_refused_by_owned(
    tmp_path: Path, newest: bytes
) -> None:
    """A never-opened `wal/000003.jsonl` on the misplaced restore: P comes
    from the segment before it, period 2, whose row names B. Without that
    fallback P is unreadable, OWNED is skipped and the repair removes the
    file."""
    root_a, root_b, anchor_dir, _ = _misplaced_restore(tmp_path)
    wal_path(root_a, 3).write_bytes(newest)
    before = _tree(root_a, anchor_dir)

    with pytest.raises(RootAuthorityError) as refused:
        _resume_virtual(root_a, anchor_dir)
    assert f"row for period 2 names {root_b.resolve()}" in str(refused.value)
    assert _tree(root_a, anchor_dir) == before


def test_pr57_a_deeply_nested_first_line_is_a_refusal_not_a_traceback(tmp_path: Path) -> None:
    """A first line nested too deep to parse counts as not a document, as
    the sentinel reader treats one: through the CLI the misplaced restore
    is refused with exit 2 rather than dying on a RecursionError."""
    root_a, root_b, anchor_dir, c2 = _misplaced_restore(tmp_path)
    wal_path(root_a, 3).write_bytes(b"[" * 100_000)
    result = _invoke(
        "run", str(c2), "--run-root", str(root_a), "--estate-anchor", str(anchor_dir), "--resume"
    )
    assert result.exit_code == 2, result.output
    assert f"row for period 2 names {root_b.resolve()}" in result.output


# ---------------------------------------- the pre-check and the locks


def _opened_in_place(tmp_path: Path) -> tuple[Path, Path, Path, Anchor]:
    """A root that sealed period 1 offline and opened period 2 in place,
    and the anchor as it stood between the two: `closed(1, root)`."""
    c1, c2, c3 = _estate(tmp_path / "estate")
    root = tmp_path / "run"
    _native_root(root, c1)
    assert _seal_offline(root, c2).exit_code == 0
    stale = EstateAnchor(default_anchor_dir(root)).read()
    assert stale is not None and isinstance(stale.head, ClosedHead)
    _open_in_place(root, c2)
    return root, c2, c3, stale


async def _admitted(*_args: object, **_kwargs: object) -> None:
    raise EngineError("admitted: resume_run was reached")


@pytest.mark.parametrize("route", ["run", "seal"])
def test_pr57_a_stale_refusal_is_overturned_under_the_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """The pre-check reads the anchor as it stood before an in-place
    opening completed: period 2 has no row there and the head is not a
    claim, so the snapshot refuses. Under the locks the current anchor
    admits the root, and the command goes on as any resume does."""
    root, c2, c3, stale = _opened_in_place(tmp_path)
    real_read = EstateAnchor.read
    monkeypatch.setattr(EstateAnchor, "read", lambda self: stale)
    assert resume_root_refusal(root, None) is not None  # the snapshot alone refuses
    served: list[Path] = []

    def first_read_stale(self: EstateAnchor) -> Anchor | None:
        if not served:
            served.append(self.path)
            return stale
        return real_read(self)

    monkeypatch.setattr(EstateAnchor, "read", first_read_stale)
    if route == "seal":
        result = _seal_offline(root, c3)
        assert result.exit_code == 0, result.output  # the boundary committed
    else:
        # a resume that is admitted serves until stopped: stand in for it
        monkeypatch.setattr(runner_startup, "resume_run", _admitted)
        result = _invoke("run", str(c2), "--run-root", str(root), "--resume")
        assert result.exit_code == 2, result.output
        assert "admitted: resume_run was reached" in result.output
    assert served == [default_anchor_dir(root) / "anchor.json"]
    assert "this anchor does not" not in result.output


@pytest.mark.parametrize("route", ["run", "seal"])
def test_pr57_a_reclaim_after_the_pre_check_is_refused_under_the_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """B holds a durable segment and the claim, so the pre-check admits it;
    the claim is reclaimed before the command takes its locks. `resume_run`
    refuses under both: no leader record, no head move."""
    c1, c2, c3 = _estate(tmp_path / "estate")
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    _native_root(root_a, c1)
    assert _seal_offline(root_a, c2).exit_code == 0
    assert _invoke("audit", "--run-root", str(root_a)).exit_code == 0
    anchor_dir = default_anchor_dir(root_a)
    with pytest.raises(_Stopped):
        _roll(root_b, anchor_dir, c2, stop_at="after_opening_segment")
    estate_id = read_seal(root_a, 1).estate_id
    segment = wal_path(root_b, 2).read_bytes()
    real = boundary.resume_root_refusal

    def then_reclaim(
        run_root: Path, anchor_dir_: Path | None, *, locked: bool = False
    ) -> RootAuthorityError | None:
        verdict = real(run_root, anchor_dir_, locked=locked)
        assert verdict is None and not locked  # admitted, so never re-asked
        anchor = EstateAnchor(anchor_dir)
        anchor.acquire()
        anchor.reclaim(estate_id=estate_id, claimed_actor="ops")
        anchor.release()
        return verdict

    monkeypatch.setattr(boundary, "resume_root_refusal", then_reclaim)
    named = ("--estate-anchor", str(anchor_dir))
    if route == "seal":
        result = _seal_offline(root_b, c3, *named)
    else:
        result = _invoke("run", str(c2), "--run-root", str(root_b), *named, "--resume")
    assert result.exit_code == 2, result.output
    assert f"this anchor does not name {root_b.resolve()}" in result.output
    stored = EstateAnchor(anchor_dir).read()
    assert stored is not None and isinstance(stored.head, ClosedHead)
    assert wal_path(root_b, 2).read_bytes() == segment


def test_pr57_a_corrupt_anchor_on_a_held_root_still_reports_the_holder(tmp_path: Path) -> None:
    """The pre-check reports only the rule's refusal: a corrupt anchor.json
    passes it. A root another engine holds says so first, as before."""
    root = tmp_path / "a"
    _root_with_head(root, "open")
    (default_anchor_dir(root) / "anchor.json").write_text("{not json\n")
    assert resume_root_refusal(root, None) is None  # the unlocked read passes it
    jil = tmp_path / "estate.jil"
    jil.write_text(C1_JIL)
    held = acquire_run_root(root)
    try:
        result = _invoke("run", str(jil), "--run-root", str(root), "--resume")
    finally:
        held.release()
    assert result.exit_code == 2, result.output
    assert "is held by another" in result.output
    assert "anchor.json" not in result.output


# ------------------------------------------------------------- spelling


def test_pr57_other_spellings_of_the_recorded_root_are_admitted(tmp_path: Path) -> None:
    """The same root through a symlink and through a `..` detour, with the
    anchor named explicitly: both resume. Paths compare normalized."""
    root = tmp_path / "a"
    _root_with_head(root, "open")
    anchor_dir = default_anchor_dir(root)
    link = tmp_path / "link"
    link.symlink_to(root)
    _close(_resume_virtual(link, anchor_dir))
    (tmp_path / "x").mkdir()
    detour = tmp_path / "x" / ".." / "a"
    assert str(detour) != str(root)
    _close(_resume_virtual(detour, anchor_dir))


def test_pr57_a_symlink_with_the_default_anchor_keeps_todays_refusal(tmp_path: Path) -> None:
    """Pinned as it is: the default anchor is derived from the SPELLING
    given (`<link>.anchor`), which holds no anchor, so the resume is refused
    for that before the root rule is reached."""
    root = tmp_path / "a"
    _root_with_head(root, "open")
    link = tmp_path / "link"
    link.symlink_to(root)
    with pytest.raises(EngineError, match="this lineage has no anchor"):
        _resume_virtual(link, None)


def test_pr57_same_root_compares_directories_not_spellings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`same_root`'s second half on any filesystem: with normalization
    stubbed out, a symlink spelling of a directory differs as a string and
    is still the same directory; another directory, and a recorded path
    that no longer exists, are not."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(boundary, "normalized_root", str)
    assert boundary.normalized_root(link) != boundary.normalized_root(real)
    assert same_root(str(link), str(real))
    assert not same_root(str(other), str(real))
    assert not same_root(str(tmp_path / "gone"), str(real))


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.mkdir()
    try:
        return (directory / "caseprobe").exists()
    finally:
        probe.rmdir()


def test_pr57_a_case_variant_spelling_is_the_same_root(tmp_path: Path) -> None:
    """On a case-insensitive filesystem `RUNROOT` is the directory recorded
    as `runroot`: the same directory, so the same root. Probed, not assumed
    from the platform."""
    if not _case_insensitive(tmp_path):
        pytest.skip("this filesystem is case-sensitive")
    root = tmp_path / "runroot"
    _root_with_head(root, "open")
    _close(_resume_virtual(tmp_path / "RUNROOT", default_anchor_dir(root)))


# ------------------------------------------------------ the CLI, detached


def _detached_copy(short_root: Path) -> tuple[Path, Path]:
    """A DETACHED period-1 root run by a real `dsl41 run --detached`, its
    supervisor shut down, then copied with its anchor. Returns (original,
    copy). The caller kills whatever either root still names."""
    original = short_root / "run"
    with engine(short_root, extra=("--detached",)):
        pass
    assert (original / "supervisor.pid").exists()  # the detached period's supervisor
    down = cli("supervise", "shutdown", "--run-root", str(original))
    assert down.returncode == 0, down.stderr
    wait_for(lambda: not (original / "supervisor.pid").exists())
    wait_for(lambda: not (original / "supervisor.sock").exists())
    return original, _copy(original, short_root / "copy")


def _refused_by_the_rule(result, original: Path, copy: Path) -> None:
    assert result.returncode == 2, result.stderr
    assert "this anchor does not name" in result.stderr
    assert str(copy.resolve()) in result.stderr
    assert str(original.resolve()) in result.stderr
    assert not (copy / "supervisor.pid").exists()
    assert not (copy / "supervisor.sock").exists()


def test_pr57_a_detached_resume_of_a_relocated_copy_starts_no_supervisor(
    short_root: Path,
) -> None:
    """`dsl41 run --resume --detached` on a copy: exit 2, the rule's
    message, and no supervisor. The refusal runs before the CLI wires one,
    so no supervisor file appears and nothing under the copy moves."""
    try:
        original, copy = _detached_copy(short_root)
        before = _tree(copy, default_anchor_dir(copy))
        refused = cli(
            "run", "--run-root", str(copy), "--resume", "--detached", str(short_root / "estate.jil")
        )
        _refused_by_the_rule(refused, original, copy)
        assert _tree(copy, default_anchor_dir(copy)) == before
    finally:
        _kill_group(short_root / "copy")
        _kill_group(short_root / "run")


def test_pr57_an_offline_seal_of_a_relocated_copy_starts_no_supervisor(
    short_root: Path,
) -> None:
    """The offline `dsl41 seal` resumes the root it seals, so it meets the
    same rule first: exit 2, nothing staged into the copy, no supervisor
    wired or started, and nothing under the copy or its anchor moves."""
    try:
        original, copy = _detached_copy(short_root)
        before = _tree(copy, default_anchor_dir(copy))
        refused = cli("seal", "--run-root", str(copy), "--next", str(short_root / "estate.jil"))
        _refused_by_the_rule(refused, original, copy)
        assert _tree(copy, default_anchor_dir(copy)) == before
    finally:
        _kill_group(short_root / "copy")
        _kill_group(short_root / "run")


@pytest.mark.parametrize("route", ["run", "seal"])
def test_pr57_a_copy_against_a_busy_original_anchor_starts_no_supervisor(
    short_root: Path, route: str
) -> None:
    """Split brain while the original leads: a copy resumed, or sealed
    offline, against the ORIGINAL's anchor while another holder has its
    lock. The confirmation under the locks cannot take the anchor lock,
    and that is the command's refusal, before anything is staged or
    wired: exit 2, no supervisor, nothing under the copy moved."""
    try:
        original, copy = _detached_copy(short_root)
        anchor_dir = default_anchor_dir(original)
        before = _tree(copy, anchor_dir)
        holder = EstateAnchor(anchor_dir)
        holder.acquire()
        try:
            jil = str(short_root / "estate.jil")
            named = ("--estate-anchor", str(anchor_dir))
            if route == "seal":
                result = cli("seal", "--run-root", str(copy), "--next", jil, *named)
            else:
                result = cli("run", "--run-root", str(copy), "--resume", "--detached", *named, jil)
        finally:
            holder.release()
        assert result.returncode == 2, result.stderr
        assert "is held by another" in result.stderr
        assert not (copy / "supervisor.pid").exists()
        assert not (copy / "supervisor.sock").exists()
        assert _tree(copy, anchor_dir) == before
    finally:
        _kill_group(short_root / "copy")
        _kill_group(short_root / "run")
