"""Durable records and process identity, shared by the lifecycle tier (DL-72).

The one copy of the helpers the Tier-0 wrapper (runner_wrapper.py) and the
Tier-1 supervisor (runner_supervisor.py) both need: the DL-41a durability
liturgy, the boot-session id, the (pid, start-time) PID-reuse guard, and the
group-kill that tolerates an already-dead group. They were copied between the
two modules while the stdlib-only boundary (DL-42) was read as "no imports at
all"; the copies then drifted (durable_write created its temp file 0o600 in
one and 0o644 in the other), which is what a copy costs. One module, imported
by both, keeps the boundary and drops the drift -- the tighter 0o600 wins
(the run_root is 0o700 anyway).

STDLIB ONLY, like its two callers: this module imports nothing from dsl41 and
nothing third-party (import-graph tests in tests/test_runner_lifecycle.py and
tests/test_runner_supervisor.py cover all three files). The wrapper and the
supervisor are run BY FILE PATH (``sys.executable <file>``), never ``-m``, so
they import this as a plain top-level module -- their own directory is
sys.path[0]. Under PYTHONSAFEPATH=1 that entry is absent, so both prepend the
script's directory themselves before importing, and remove it again after: the
engine imports those two files as ordinary package modules as well, and a
package directory left behind on sys.path would shadow top-level names for the
importing process. The engine imports this module as ``dsl41.runner_procid``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Final

#: the `version` the Tier-0 wrapper stamps on `spawn.json` and `status.json`
#: (docs/supervisor-protocol.md ss3). ONE integer, held here because this is
#: the one module all three readers may import: the wrapper writes it, the
#: supervisor and the engine each check it (DL-152). It used to be a literal
#: in the wrapper and a hand-kept copy in the supervisor, on the reading that
#: the stdlib-only tier imports nothing at all -- the same reading DL-72
#: already corrected for the durability helpers.
SPOOL_VERSION: Final[int] = 1


def spool_version_supported(doc: Mapping[str, Any]) -> bool:
    """The `Wrapper-owned spool files` row of docs/protocol-evolution.md,
    asked once (DL-152).

    The row is tolerant on fields and STRICT on versions: a version this
    binary does not implement must stop the reader, because the field exists
    to say the meaning changed. `true` and `1.0` are not the integer 1.

    An ABSENT `version` passes. The matrix has no column for a missing
    version and no document rules one, so refusing here would pick a side by
    guess; the split is recorded in the round's ledger instead.

    The type check is spelled out rather than borrowed from `canon.is_wire_int`:
    this module imports nothing from dsl41, which is what lets the Tier-0
    wrapper import it (DL-42/DL-72).
    """
    if "version" not in doc:
        return True
    version = doc["version"]
    return isinstance(version, int) and not isinstance(version, bool) and version == SPOOL_VERSION


# ------------------------------------------------------------------ durability


def mkdir_durable(path: str) -> None:
    """Create a directory (parents included) with every new entry made
    durable: each created component is fsynced, and so is the deepest
    PRE-EXISTING ancestor -- its entry for the first new component is a
    record too. Unconditional on retry: a call that created nothing still
    fsyncs the parent, so a crash between an earlier mkdir and its fsync
    is repaired rather than skipped."""
    probe = os.path.abspath(path)
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    os.makedirs(path, exist_ok=True)

    def _sync(directory: str) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    walk = os.path.abspath(path)
    while True:
        _sync(walk)
        if walk == probe:
            break
        walk = os.path.dirname(walk)
    if probe == os.path.abspath(path):
        _sync(os.path.dirname(probe) or ".")


def fsync_dir(path: "str | os.PathLike[str]") -> None:
    """Fsync a directory: a create, rename or unlink is a directory-entry
    write, and without this it is not durable across a power loss. The
    ONE spelling (DL-137) -- five modules each had their own; runner_wrapper
    never had a copy, and runner_supervisor's is gone too now (DL-178k), so
    the DL-42 licence for a Tier-0/1 copy protects nothing that still
    exists."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_file(path: "str | os.PathLike[str]") -> None:
    """Make an EXISTING file's bytes durable before a mutation relies on
    them: recovery reads prove readable, not durable, and a CAS over a
    line a power cut then removes leaves a successor whose naming record
    is gone. Read-only open -- the caller appends nothing here."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_all(fd: int, data: bytes) -> None:
    """Every byte, or an error. `os.write` may write FEWER bytes than it was
    given and return the count without raising, and the liturgy below then
    fsyncs and renames a TRUNCATED record into place (DL-151) -- a
    half-written `spawn.json` published as the durable one, which is the
    exact loss the liturgy exists to prevent. Public (DL-178i): the third
    append site outside this module, `runner_adapters.append_watch_line`,
    needed the same guard."""
    written = 0
    while written < len(data):
        count = os.write(fd, data[written:])
        if count <= 0:  # pragma: no cover -- POSIX: 0 only for an empty write
            raise OSError(f"short write: {written} of {len(data)} bytes")
        written += count


def durable_write(path: str, data: bytes) -> None:
    """The DL-41a durability liturgy: same-dir temp file, fsync(file),
    rename, fsync(directory). Requires a local filesystem."""
    directory = os.path.dirname(path) or "."
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    try:
        os.unlink(tmp)  # a failed EARLIER attempt by this same pid; O_EXCL would wedge retry
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # owner-only, not umask's call
    try:
        try:
            write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)  # never leave a half-written temp to wedge or mislead
        except FileNotFoundError:
            pass
        raise
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def durable_create(path: str, data: bytes) -> None:
    """The DL-41a liturgy with CREATE-ONLY publication: the temp is linked
    into place, never renamed over an existing file. Raises
    FileExistsError when the name is already taken -- the caller decides
    what a lost race means (for a content-addressed or verify-able
    artifact, the winner IS the answer)."""
    directory = os.path.dirname(path) or "."
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            write_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)  # fails EEXIST rather than replacing
        except FileExistsError:
            # the WINNER's link may not be durable yet (it links, then
            # fsyncs the directory): a loser who acts on the visible file
            # -- verifying it, flipping a row over it -- must first make
            # it durable itself, or a power cut can drop the link and
            # leave the row pointing at nothing
            make_durable(path)
            raise
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    dfd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def make_durable(path: str) -> None:
    """Fsync an EXISTING file and its directory entry: what a reader owes
    a file it is about to durably rely on when the writer may have died
    between the link and its fsync."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    dfd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def durable_write_json(path: str, record: dict[str, Any]) -> None:
    durable_write(path, json.dumps(record, sort_keys=True).encode("utf-8") + b"\n")


# ------------------------------------------------------------ machine identity


def current_boot_id() -> str:
    """Boot session identity (DL-42 item 5): a reboot recycles the whole
    (pid, start-time) space, so a boot_id mismatch voids liveness checks AND
    proves nothing survived."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
            return f.read().strip()
    except OSError:
        pass
    out = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip()
    return "unknown"


def proc_start_token(pid: int) -> str | None:
    """Opaque start-time token for the (pid, start-time) PID-reuse guard, or
    None when the pid is gone. Linux: tick-exact starttime, field 22 of
    /proc/<pid>/stat (split after the LAST ')' -- comm may contain spaces and
    parens). macOS: ``ps -o lstart=`` under LC_ALL=C, 1-second resolution."""
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                raw = f.read().decode("ascii", "replace")
        except OSError:
            return None
        fields = raw.rsplit(")", 1)[1].split()
        return f"ticks:{fields[19]}"  # fields[0] is field 3 overall
    out = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
        check=False,
    )
    lstart = out.stdout.strip()
    if out.returncode != 0 or not lstart:
        return None
    return f"lstart:{lstart}"


_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"


def start_tokens_match(a: str, b: str, *, tolerance_s: float = 2.0) -> bool:
    """Compare start-time tokens: tick tokens exactly, lstart tokens within
    +/-2s (macOS ps rounds to whole seconds; DL-41a probed the drift)."""
    if a.startswith("ticks:") or b.startswith("ticks:"):
        return a == b
    if not (a.startswith("lstart:") and b.startswith("lstart:")):
        return False
    try:
        ta = time.mktime(time.strptime(a[len("lstart:") :], _LSTART_FORMAT))
        tb = time.mktime(time.strptime(b[len("lstart:") :], _LSTART_FORMAT))
    except ValueError:
        return False
    return abs(ta - tb) <= tolerance_s


def verify_alive(pid: int, recorded_token: str) -> bool:
    """The PID-reuse guard: a pid is only 'ours' if it exists AND its start
    time matches the recorded token. Never signal a pid that fails this."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists under another uid; the token check decides
    token = proc_start_token(pid)
    return token is not None and start_tokens_match(token, recorded_token)


# --------------------------------------------------------------- process group


def killpg_quiet(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass  # the whole group is already gone


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
