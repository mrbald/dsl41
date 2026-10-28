"""Durable records and process identity, shared by the lifecycle tier (DL-72).

The one copy of the helpers the Tier-0 wrapper (runner_wrapper.py) and the Tier-1
supervisor (runner_supervisor.py) both need: the DL-41a durability liturgy, the
boot-session id, the (pid, start-time) PID-reuse guard, and the group-kill that tolerates
an already-dead group. The stdlib-only boundary (DL-42) allows a shared module, and one
copy cannot drift; its temp files are created 0o600 (the run_root is 0o700 anyway).

STDLIB ONLY, like its two callers: it imports nothing from dsl41 and nothing third-party
(import-graph tests in tests/test_runner_lifecycle.py and tests/test_runner_supervisor.py
cover all three files). The wrapper and the supervisor run BY FILE PATH, never ``-m``, and
import this as a plain top-level module from sys.path[0]. Under PYTHONSAFEPATH=1 both
prepend their directory and remove it after, because the engine imports them as package
modules too and a package directory left on sys.path would shadow top-level names. The
engine imports this module as ``dsl41.runner_procid``.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from datetime import UTC, datetime
from typing import Any, Final

#: the `version` the Tier-0 wrapper stamps on `spawn.json` and `status.json`
#: (docs/supervisor-protocol.md ss3). ONE integer, held in the one module all
#: three readers may import: the wrapper writes it, the supervisor and the
#: engine each check it (DL-152, DL-72).
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


def verify_lock_path(fd: int, path: str | os.PathLike[str]) -> None:
    """Maintenance excludes engines only while the existing regular inode stays put (DL-390)."""
    held = os.fstat(fd)
    current = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(held.st_mode) or not stat.S_ISREG(current.st_mode):
        raise LockHeld(errno.ESTALE, "lock is not a regular file", os.fspath(path))
    if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
        raise LockHeld(errno.ESTALE, "lock path moved", os.fspath(path))
    if not current.st_mode & 0o444 or not current.st_mode & 0o222:
        raise PermissionError(errno.EACCES, "lock is unreadable or unwritable", os.fspath(path))


def flock_existing(path: str | os.PathLike[str]) -> int:
    """Take maintenance exclusion without creating, following or truncating evidence (DL-390)."""
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        verify_lock_path(fd, path)
    except BaseException:
        os.close(fd)
        raise
    return fd


class ObservationUnavailable(OSError):
    """Complete, dedicated service containment cannot be established (DL-390)."""


# The supervisor's log names the busctl read of its exit policy once per process (DL-390).
_busctl_policy_logged = False


def _systemd_exit_status_codes(text: str) -> set[int]:
    """Only the exit-code array of systemd's typed (aiai) status pair admits 8 (DL-390)."""
    fields = text.split()
    if (
        len(fields) < 3
        or fields[0] != "(aiai)"
        or any(not re.fullmatch(r"[0-9]+", field) for field in fields[1:])
    ):
        raise ObservationUnavailable("malformed typed systemd exit policy")
    try:
        values = [int(field) for field in fields[1:]]
    except ValueError as exc:
        raise ObservationUnavailable("malformed typed systemd exit policy") from exc
    count = values[0]
    if any(value > 2**31 - 1 for value in values) or count > len(values) - 2:
        raise ObservationUnavailable("invalid typed systemd exit policy counts")
    if len(values) != count + 2 + values[count + 1]:
        raise ObservationUnavailable("invalid typed systemd exit policy length")
    return set(values[1 : count + 1])


def _own_systemd_cgroup() -> tuple[str, str, str]:
    """This process's systemd hierarchy: (controllers, cgroup path, unit), the unit a
    `.service`. Raises `ObservationUnavailable` when it is not unambiguous."""
    entries = [line.split(":", 2) for line in Path("/proc/self/cgroup").read_text().splitlines()]
    candidates = [
        (controllers, group)
        for _, controllers, group in entries
        if "name=systemd" in controllers.split(",")
    ]
    if not candidates:
        candidates = [
            (controllers, group) for _, controllers, group in entries if controllers == ""
        ]
    if len(candidates) != 1:
        raise ObservationUnavailable("no unambiguous systemd cgroup hierarchy")
    controllers, group = candidates[0]
    unit = group.rsplit("/", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_.@:\\-]+[.]service", unit):
        raise ObservationUnavailable("supervisor is not in a service cgroup")
    return controllers, group, unit


@dataclass(frozen=True)
class ServiceContainment:
    """The qualified Linux systemd hierarchy, including its complete subtree (DL-390)."""

    directory: Path
    control_group: str
    unit: str
    pid: int

    @classmethod
    def observe(cls) -> ServiceContainment:
        if not sys.platform.startswith("linux"):
            raise ObservationUnavailable("maintenance requires Linux service containment")
        controllers, group, unit = _own_systemd_cgroup()
        mounts: list[Path] = []
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            left, right = line.split(" - ", 1)
            fields, fs = left.split(), right.split()
            expected = "cgroup2" if controllers == "" else "cgroup"
            if fs[0] != expected:
                continue
            if controllers and "name=systemd" not in fs[2].split(","):
                continue
            # A restricted mount root cannot prove the manager's whole subtree (DL-390).
            if fields[3] != "/" or "\\" in fields[4]:
                raise ObservationUnavailable("unreconciled cgroup mount namespace")
            mounts.append(Path(fields[4]))
        if len(mounts) != 1 or not group.startswith("/") or ".." in group.split("/"):
            raise ObservationUnavailable("no unambiguous complete cgroup mount")
        proof = cls(mounts[0] / group.lstrip("/"), group, unit, os.getpid())
        proof.verify_manager()
        return proof

    def verify_manager(self) -> None:
        # Identity, containment and exit policy; no stop guard (DL-399, DL-416).
        # All manager reads share one bound; helpers are reaped before sampling (DL-390).
        deadline = time.monotonic() + 5.0

        def query(argv: list[str]) -> str:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ObservationUnavailable("service manager observation timed out")
            return subprocess.run(
                argv, capture_output=True, text=True, check=True, timeout=remaining
            ).stdout

        output = query(
            [
                "systemctl",
                "show",
                self.unit,
                "--no-pager",
                "--property=MainPID",
                "--property=ControlGroup",
                "--property=Delegate",
                "--property=KillMode",
                "--property=SuccessExitStatus",
                "--property=RestartPreventExitStatus",
            ]
        )
        properties = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if (
            properties.get("MainPID") != str(self.pid)
            or properties.get("ControlGroup") != self.control_group
            or properties.get("Delegate") != "no"
            or properties.get("KillMode") != "control-group"
        ):
            raise ObservationUnavailable("service ownership, delegation or containment mismatch")
        statuses = ("SuccessExitStatus", "RestartPreventExitStatus")
        if any(properties.get(name) == "[unprintable]" for name in statuses):
            # systemd 239's text formatter cannot print these typed manager values (DL-390).
            reply = query(
                [
                    "busctl",
                    "--system",
                    "call",
                    "org.freedesktop.systemd1",
                    "/org/freedesktop/systemd1",
                    "org.freedesktop.systemd1.Manager",
                    "GetUnit",
                    "s",
                    self.unit,
                ]
            )
            signature, _, encoded_path = reply.strip().partition(" ")
            try:
                object_path = json.loads(encoded_path)
            except ValueError as exc:
                raise ObservationUnavailable("malformed systemd unit object path") from exc
            if (
                signature != "o"
                or not isinstance(object_path, str)
                or not re.fullmatch(r"/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+", object_path)
            ):
                raise ObservationUnavailable("unverified systemd unit object path")
            for name in statuses:
                if properties.get(name) == "[unprintable]":
                    value = query(
                        [
                            "busctl",
                            "--system",
                            "get-property",
                            "org.freedesktop.systemd1",
                            object_path,
                            "org.freedesktop.systemd1.Service",
                            name,
                        ]
                    )
                    codes = _systemd_exit_status_codes(value)
                    properties[name] = " ".join(map(str, codes))
            global _busctl_policy_logged
            if not _busctl_policy_logged:
                # a diagnostic never decides admission: a failed write leaves the verdict and
                # the flag alone, so the line is tried again on the next read (DL-402)
                try:
                    print(
                        "supervisor: exit policy read through busctl"
                        " (systemd text formatter printed [unprintable])",
                        file=sys.stderr,
                        flush=True,
                    )
                except OSError:
                    pass
                else:
                    _busctl_policy_logged = True
        if any("8" not in properties.get(name, "").split() for name in statuses):
            raise ObservationUnavailable("service exit policy does not retain maintenance stop")

    def members(self) -> set[int]:
        """Read every subgroup. Missing or unreadable membership is never empty (DL-390)."""
        self.verify_manager()
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ObservationUnavailable("service cgroup disappeared or became a symlink")
        members: set[int] = set()

        def fail(error: OSError) -> None:
            raise error

        for root, directories, _files in os.walk(self.directory, onerror=fail, followlinks=False):
            if any((Path(root) / name).is_symlink() for name in directories):
                raise ObservationUnavailable("cgroup subtree contains a symlink")
            lines = (Path(root) / "cgroup.procs").read_text().splitlines()
            if any(not line.isdecimal() or int(line) <= 0 for line in lines):
                raise ObservationUnavailable("malformed cgroup membership")
            members.update(map(int, lines))
        if self.pid not in members:
            raise ObservationUnavailable("supervisor absent from observed service cgroup")
        return members


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


def fsync_dir(path: "str | os.PathLike[str]") -> None:
    """Fsync a directory so a create, rename or unlink survives power loss.

    The one implementation (DL-137, DL-178k); the DL-42 licence for a Tier-0/1 copy
    covers none."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_file(path: "str | os.PathLike[str]") -> None:
    """Make an EXISTING file's bytes durable before a mutation relies on them: recovery
    reads prove readable, not durable, and a CAS over a line a power cut then removes
    leaves a successor whose naming record is gone. Read-only open."""
    fd = os.open(path, os.O_RDONLY)
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


def durable_write(path: str, data: bytes) -> None:
    """The DL-41a durability liturgy: same-dir temp file, fsync(file), rename,
    fsync(directory). Requires a local filesystem."""
    directory = os.path.dirname(path) or "."
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    try:
        os.unlink(tmp)  # a failed EARLIER attempt by this pid; O_EXCL would wedge the retry
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
    """The DL-41a liturgy with CREATE-ONLY publication: the temp is linked into place,
    never renamed over an existing file. Raises FileExistsError when the name is taken;
    the caller decides what a lost race means (for a content-addressed or verifiable
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
            # the WINNER's link may not be durable yet (it links, then fsyncs the
            # directory): a loser who acts on the visible file must make it durable
            # first, or a power cut can drop the link under the row that points at it
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
    """Fsync an EXISTING file and its directory entry: what a reader owes a file it will
    durably rely on when the writer may have died between the link and its fsync."""
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


def _linux_stat_fields(pid: int) -> list[str] | None:
    """The fields of /proc/<pid>/stat after the comm, in one read, or None when the pid is
    gone. Split after the LAST ')', since comm may contain spaces and parens. fields[0] is
    field 3 overall (the state), so field N is fields[N - 3]."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read().decode("ascii", "replace")
    except OSError:
        return None
    return raw.rsplit(")", 1)[1].split()


def _cmdline(pid: int) -> list[str] | None:
    """The argv of a live Linux process, or None when it cannot be read."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return raw.decode("utf-8", "replace").split("\0")[:-1]


def stop_helpers(members: set[int]) -> set[int]:
    """The supervisor unit's ExecStop helper among `members`, with the `sleep` its wait loop
    runs: exactly one process whose parent is PID 1 and whose argv is the launcher's
    `supervisor-stop` mode (`dsl41-launch` ... `supervisor-stop`), and its `sleep` children.
    A stop that overlaps maintenance keeps it in the unit's cgroup, and it is no residual
    work (DL-416). Two such processes, or none, are no helper: an empty set."""
    helpers = []
    for pid in members:
        fields, argv = _linux_stat_fields(pid), _cmdline(pid)
        if fields is None or argv is None or len(fields) < 2 or fields[1] != "1":
            continue
        if argv and argv[-1] == "supervisor-stop":
            if any(Path(word).name == "dsl41-launch" for word in argv):
                helpers.append(pid)
    if len(helpers) != 1:
        return set()
    (helper,) = helpers
    found = {helper}
    for pid in members:
        fields, argv = _linux_stat_fields(pid), _cmdline(pid)
        if fields is None or argv is None or len(fields) < 2 or fields[1] != str(helper):
            continue
        if argv and Path(argv[0]).name == "sleep":
            found.add(pid)
    return found


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


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
