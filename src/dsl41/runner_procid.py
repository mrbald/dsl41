"""Durable records and process identity, shared by the wrapper and the engine (DL-72).

The one copy of the helpers the Tier-0 wrapper (runner_wrapper.py) and the engine both need:
the DL-41a durability liturgy, the boot-session id, the (pid, start-time) PID-reuse guard, and
the group-kill that tolerates an already-dead group. The stdlib-only boundary (DL-42) allows a
shared module, and one copy cannot drift; its temp files are created 0o600 (the run_root is
0o700 anyway).

STDLIB ONLY, like the wrapper: it imports nothing from dsl41 and nothing third-party (the
import-graph test in tests/test_runner_lifecycle.py covers both files). The wrapper runs BY
FILE PATH, never ``-m``, and imports this as a plain top-level module from sys.path[0]. Under
PYTHONSAFEPATH=1 it prepends its directory and removes it after, because the engine imports it
as a package module too and a package directory left on sys.path would shadow top-level names.
The engine imports this module as ``dsl41.runner_procid``.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

#: the `version` the Tier-0 wrapper stamps on `spawn.json` and `status.json`
#: (docs/wrapper-protocol.md). ONE integer, held in the one module both readers
#: may import: the wrapper writes it, and the engine checks it (DL-152, DL-72).
SPOOL_VERSION: Final[int] = 1


def spool_version_supported(doc: Mapping[str, Any]) -> bool:
    """The `Wrapper-owned spool files` row of docs/protocol-evolution.md, asked once
    (DL-152).

    The row is tolerant on fields and STRICT on versions: a version this binary does not
    implement stops the reader, since the field says the meaning changed. `true` and
    `1.0` are not the integer 1. An ABSENT `version` passes, the one row of the matrix
    that lets a missing version through (DL-157, DL-227), because the Tier-0 wrapper
    writes the file before any reader exists to require it.

    The type check is spelled out, not borrowed from `canon.is_wire_int`: this module
    imports nothing from dsl41 so the Tier-0 wrapper can import it (DL-42/DL-72).
    """
    if "version" not in doc:
        return True
    version = doc["version"]
    return isinstance(version, int) and not isinstance(version, bool) and version == SPOOL_VERSION


class LockHeld(OSError):
    """A nonblocking exclusive lock was unavailable or its path moved (DL-210)."""


def flock_exclusive(path: str | os.PathLike[str]) -> int:
    """Return the caller-owned fd only while it locks the file still at path.

    The file is never unlinked. ESTALE distinguishes replacement from contention so the
    ledger keeps its refusal messages.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockHeld(exc.errno, str(exc), os.fspath(path)) from exc
        held = os.fstat(fd)
        try:
            current = os.stat(path)
        except FileNotFoundError:
            current = None
        if current is None or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
            raise LockHeld(errno.ESTALE, "lock path moved", os.fspath(path))
    except BaseException:
        os.close(fd)
        raise
    return fd


# ------------------------------------------------------------------ durability


def mkdir_durable(path: str) -> None:
    """Create a directory (parents included) with every new entry durable: each created
    component is fsynced, and so is the deepest PRE-EXISTING ancestor, whose entry for the
    first new component is a record too. It is unconditional on retry: a call that created
    nothing still fsyncs the parent, repairing a crash between an earlier mkdir and its
    fsync."""
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


def fsync_dir(path: "str | os.PathLike[str]", *, nofollow: bool = False) -> None:
    """Fsync a directory so a create, rename or unlink survives power loss; with `nofollow`,
    a symlink or a non-directory at PATH refuses (O_NOFOLLOW | O_DIRECTORY).

    The one implementation (DL-137, DL-178k); the DL-42 licence for a Tier-0/1 copy
    covers none."""
    fd = os.open(path, os.O_RDONLY | (os.O_DIRECTORY | os.O_NOFOLLOW if nofollow else 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_all(fd: int, data: bytes) -> None:
    """Every byte, or an error. `os.write` may write FEWER bytes than given and return the
    count without raising, and the liturgy would then fsync and rename a TRUNCATED record
    into place (DL-151), the loss it exists to prevent. Public (DL-178i):
    `runner_adapters.append_watch_line` needs the same guard."""
    written = 0
    while written < len(data):
        count = os.write(fd, data[written:])
        if count <= 0:  # pragma: no cover -- POSIX: 0 only for an empty write
            raise OSError(f"short write: {written} of {len(data)} bytes")
        written += count


def durable_write(path: str, data: bytes, *, mode: int = 0o600) -> None:
    """The DL-41a durability liturgy: same-dir temp file, fsync(file), rename,
    fsync(directory). Requires a local filesystem. The file gets MODE whatever the umask,
    owner-only by default."""
    directory = os.path.dirname(path) or "."
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    try:
        os.unlink(tmp)  # a failed EARLIER attempt by this pid; O_EXCL would wedge the retry
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        try:
            os.fchmod(fd, mode)
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
    fsync_dir(directory)


def durable_write_json(path: str, record: dict[str, Any]) -> None:
    durable_write(path, json.dumps(record, sort_keys=True).encode("utf-8") + b"\n")


# ------------------------------------------------------------ machine identity


def current_boot_id() -> str:
    """Boot session identity (DL-42 item 5): a reboot recycles the whole (pid, start-time)
    space, so a boot_id mismatch voids liveness checks AND proves nothing survived."""
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


#: where Linux mounts procfs; a test points it at a stand-in tree
_PROC = "/proc"


def _stat_fields(raw: bytes) -> list[str]:
    """The fields of one /proc/<pid>/stat line after the comm. Split after the LAST ')',
    since comm may contain spaces and parens. fields[0] is field 3 overall (the state), so
    field N is fields[N - 3]."""
    return raw.decode("ascii", "replace").rsplit(")", 1)[1].split()


def proc_entry_error(exc: OSError) -> Literal["gone", "foreign", "failed"]:
    """What an error reading one /proc/<pid> entry means, for every reader of /proc that
    decides what is left of a command: ENOENT or ESRCH, the process is gone; EPERM or EACCES,
    a process that is not ours, since this uid can always open and read its own entries
    (`hidepid=noaccess`, systemd's `ProtectProc=noaccess`, or an LSM hides the others');
    anything else (EIO, EMFILE, ENFILE, ENOMEM) a failed reading, which proves nothing."""
    if exc.errno in (errno.ENOENT, errno.ESRCH):
        return "gone"
    if exc.errno in (errno.EPERM, errno.EACCES):
        return "foreign"
    return "failed"


def _read_stat(pid: int) -> list[str] | None:
    """The fields of /proc/<pid>/stat after the comm (`_stat_fields`), in one read, or None
    when the process is gone or not ours (`proc_entry_error`); a failed reading raises."""
    try:
        with open(f"{_PROC}/{pid}/stat", "rb") as f:
            raw = f.read()
    except OSError as exc:
        if proc_entry_error(exc) == "failed":
            raise
        return None
    return _stat_fields(raw)


def _linux_stat_fields(pid: int) -> list[str] | None:
    """`_read_stat` for one known pid, None for any error: a check of a recorded process's
    start token or state, where a pid that cannot be read is not the recorded process."""
    try:
        return _read_stat(pid)
    except OSError:
        return None


def _linux_zombie(fields: list[str]) -> bool:
    """State `Z` with at most one task left. The state is the thread-group leader's, so a
    live process whose main thread exited (pthread_exit with other threads running) also
    shows `Z`; it still counts its threads in field 20 (`Threads:`). A stat line without
    that field is read by its state alone."""
    if not fields or fields[0] != "Z":
        return False
    try:
        return int(fields[17]) <= 1
    except (IndexError, ValueError):
        return True


def _ps(pid: int, columns: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ps", "-o", columns, "-p", str(pid)],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
        check=False,
    )


def proc_start_token(pid: int) -> str | None:
    """Opaque start-time token for the (pid, start-time) PID-reuse guard, or None when the
    pid is gone. Linux: tick-exact starttime, field 22 of /proc/<pid>/stat. macOS:
    ``ps -o lstart=`` under LC_ALL=C, 1-second resolution."""
    if sys.platform.startswith("linux"):
        fields = _linux_stat_fields(pid)
        return None if fields is None else f"ticks:{fields[19]}"
    out = _ps(pid, "lstart=")
    lstart = out.stdout.strip()
    if out.returncode != 0 or not lstart:
        return None
    return f"lstart:{lstart}"


def proc_is_zombie(pid: int) -> bool:
    """Whether `pid` is a zombie: exited, unreaped, holding no descriptors or locks. A
    zombie keeps a /proc entry and a `ps` row, so the start-token guard would read it as
    the live recorded owner; the supervisor's reclaim treats it as absent (DL-210). Linux:
    state `Z` with one task left (`_linux_zombie`). macOS: `ps -o stat=` starts with Z; a
    live process whose main thread exited does not show Z there."""
    if sys.platform.startswith("linux"):
        fields = _linux_stat_fields(pid)
        return fields is not None and _linux_zombie(fields)
    out = _ps(pid, "stat=")
    return out.returncode == 0 and out.stdout.strip().startswith("Z")


def _proc_look(pid: int) -> tuple[str, bool] | None:
    """(start token, zombie) from ONE look at the process, or None when it is gone. One
    read, so a reap cannot fall between the token and the state: with two, a pid reaped
    after its token was read passed for a live process. Linux: one /proc/<pid>/stat read.
    macOS: one ``ps -o stat=,lstart=`` (stat has no spaces; lstart is the rest of the
    line)."""
    if sys.platform.startswith("linux"):
        fields = _linux_stat_fields(pid)
        if fields is None:
            return None
        return f"ticks:{fields[19]}", _linux_zombie(fields)
    out = _ps(pid, "stat=,lstart=")
    parts = out.stdout.strip().split(None, 1)
    if out.returncode != 0 or len(parts) != 2:
        return None
    return f"lstart:{parts[1]}", parts[0].startswith("Z")


_LSTART_FORMAT = "%a %b %d %H:%M:%S %Y"
_TICKS_TOKEN_RE = re.compile(r"ticks:(0|[1-9][0-9]*)")


def valid_start_token(value: object) -> bool:
    """Is `value` a start token THIS module could have minted?

    `start_tokens_match` cannot draw this line: a token that will not parse is not
    evidence of reuse, so a reader deciding "absent" from a recorded token must know the
    vocabulary first. The grammar lives beside the minting (DL-75)."""
    if not isinstance(value, str):
        return False
    if value.startswith("ticks:"):
        return _TICKS_TOKEN_RE.fullmatch(value) is not None
    if value.startswith("lstart:"):
        try:
            time.strptime(value[len("lstart:") :], _LSTART_FORMAT)
        except ValueError:
            return False
        return True
    return False


def start_tokens_match(a: str, b: str, *, tolerance_s: float = 2.0) -> bool:
    """Compare start-time tokens: tick tokens exactly, lstart tokens within +/-2s (macOS
    ps rounds to whole seconds; DL-41a probed the drift)."""
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
    """The PID-reuse guard: a pid is 'ours' only if it exists AND its start time matches
    the recorded token. Never signal a pid that fails this.

    A zombie is not alive: its token still matches until its parent reaps it, so without
    this check an exited command read as live wherever no init reaped it. The
    supervisor's owner check uses the same zombie test (DL-210). The token and the state
    come from one look (`_proc_look`), so a reap between two reads cannot pass a gone pid
    as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # exists under another uid; the token check decides
    look = _proc_look(pid)
    if look is None:
        return False
    token, zombie = look
    return start_tokens_match(token, recorded_token) and not zombie


# --------------------------------------------------------------- process group


def killpg_quiet(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass  # the whole group is already gone


def linux_group_pids(pgid: int) -> dict[int, str] | None:
    """Every process that is not a zombie in process group `pgid`, by pid with its start token,
    from field 5 (pgrp), field 3 (state) and field 22 (start ticks) of each /proc/<pid>/stat;
    None when the reading failed: /proc cannot be listed, or an entry's error is a failed
    reading (`proc_entry_error`). A process whose main thread exited while other threads run
    is no zombie (`_linux_zombie`). The kernel keeps a group exactly: a process leaves it only
    by `setsid` or `setpgid`."""
    try:
        names = os.listdir(_PROC)
    except OSError:
        return None
    members: dict[int, str] = {}
    for name in names:
        try:
            fields = _read_stat(int(name)) if name.isdigit() else None
        except OSError:
            return None
        if fields is not None and fields[2] == str(pgid) and not _linux_zombie(fields):
            members[int(name)] = f"ticks:{fields[19]}"
    return members


def linux_group_has_live_member(pgid: int) -> bool:
    """Whether a process that is not a zombie has process group `pgid` (`linux_group_pids`). A
    /proc that cannot be listed proves nothing gone, so it reads as a live member (DL-426)."""
    members = linux_group_pids(pgid)
    return members is None or bool(members)


# ------------------------------------------------------------- the process cookie

#: The variable every process of a command attempt carries, `<engine instance>.<run id>`:
#: the engine sets it for the wrapper and the command, and the command's children inherit
#: it, so a scan of the processes this uid can read finds the attempt's own, whatever their
#: process group (docs/wrapper-protocol.md, an addition). Finding one proves presence; finding
#: none proves nothing (`runner_stops.EngineStops.left`).
COOKIE_ENV: Final[str] = "DSL41_COOKIE"


def cookie(instance: str, run_id: str) -> str:
    """The cookie of the attempt `run_id` dispatched by the engine instance `instance`."""
    return f"{instance}.{run_id}"


@dataclass(frozen=True)
class Ours:
    """What one scan found: the run id and start token of each pid whose cookie matched, how
    many processes of this uid it could not read, and the errno that cut the scan short (then
    `pids` is not the whole answer)."""

    pids: dict[int, tuple[str, str]]
    skipped: int = 0
    error: int | None = None


def ours(cookie_prefix: str) -> Ours | None:
    """Every live process of this uid whose `DSL41_COOKIE` starts with `cookie_prefix`, by pid
    with its run id and start token; None off Linux.

    Each process is read through one directory fd, so its environment and its stat are the
    same process's even if the pid is reused meanwhile. An error on an entry is classified
    by `proc_entry_error`: a process gone is skipped; one this uid may not open or read is
    not ours, skipped and counted in `skipped` (another uid under `hidepid=noaccess`, or a
    non-dumpable process, which keeps its uid on its directory, the kernel's
    `task_dump_owner`, while its `environ` becomes unreadable); a failed reading, or a
    listing that fails, cuts the scan short with its errno in `error`, and the caller knows
    nothing from it. A zombie has no environment left, so it is never found."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        names = os.listdir(_PROC)
    except OSError as exc:
        return Ours({}, error=exc.errno)
    uid, marker = os.getuid(), f"{COOKIE_ENV}=".encode("ascii")
    found: dict[int, tuple[str, str]] = {}
    skipped = 0
    for name in (n for n in names if n.isdigit()):
        try:
            fd = os.open(f"{_PROC}/{name}", os.O_RDONLY | os.O_DIRECTORY)
        except OSError as exc:
            kind = proc_entry_error(exc)
            if kind == "failed":
                return Ours(found, skipped, exc.errno)
            skipped += kind == "foreign"
            continue
        try:
            if os.fstat(fd).st_uid != uid:
                continue
            environ = _read_at(fd, "environ")
            value = next((e for e in environ.split(b"\0") if e.startswith(marker)), None)
            if value is None:
                continue
            text = value[len(marker) :].decode("ascii", "replace")
            if text.startswith(cookie_prefix):
                start = _stat_fields(_read_at(fd, "stat"))[19]
                found[int(name)] = (text.partition(".")[2], f"ticks:{start}")
        except OSError as exc:
            kind = proc_entry_error(exc)
            if kind == "failed":
                return Ours(found, skipped, exc.errno)  # the finally closes the fd
            skipped += kind == "foreign"
        finally:
            os.close(fd)
    return Ours(found, skipped)


def _read_at(directory: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY, dir_fd=directory)
    try:
        chunks = []
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def pidfd_supported() -> bool:
    """Whether this Python and kernel can signal through a pidfd: `os.pidfd_open` and
    `signal.pidfd_send_signal` exist, and opening a pidfd on this process works (a kernel
    before 5.3 answers ENOSYS). Without it the cookie's processes cannot be signalled safely,
    so the engine does not scan for them."""
    pidfd_open = getattr(os, "pidfd_open", None)
    if pidfd_open is None or getattr(signal, "pidfd_send_signal", None) is None:
        return False
    try:
        os.close(pidfd_open(os.getpid()))
    except OSError:
        return False
    return True


def signal_ours(pid: int, start_token: str, sig: int) -> bool:
    """Send `sig` to `pid` while it is still the process a scan found (`start_token`); True
    when it was sent. A pidfd pins the process before the token is read again, so a pid
    reused after the scan is never signalled. A process gone, one under another uid
    (PermissionError), or no pidfd support sends nothing; nothing raises."""
    pidfd_open = getattr(os, "pidfd_open", None)
    send = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is None or send is None:
        return False
    try:
        fd = pidfd_open(pid)
    except OSError:
        return False
    try:
        if proc_start_token(pid) != start_token:
            return False
        send(fd, sig)
    except OSError:
        return False
    finally:
        os.close(fd)
    return True


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
