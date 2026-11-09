#!/usr/bin/env python3
"""Unpreloaded, bounded observer. Native use requires a separate allocation."""

import argparse
import array
import ctypes
import errno
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
ORIGINAL = "67a42d3cdcf743e78d1af2abd64b694cb6ae5407be2efb3f3a69ab33ecce4e58"
FIXED_NS = 1405933200 * 10**9
CAP = 120.0
INIT_OFFSET = 0x20540
META = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_nlink",
    "st_uid",
    "st_gid",
    "st_rdev",
    "st_size",
    "st_blksize",
    "st_blocks",
)


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def token(pid):
    return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])


def forbidden(env):
    return sorted(
        key
        for key in env
        if key.startswith(("FAKETIME", "LD_")) or key in ("NO_FAKE_STAT", "DONT_FAKE_MONOTONIC")
    )


def clean_process():
    require(not forbidden(os.environ), f"Observer inherited overrides: {forbidden(os.environ)}")
    maps = Path("/proc/self/maps").read_text()
    require("libfaketime" not in maps, "Observer was created with libfaketime loaded")


def inventory(provenance, note):
    info = os.stat("/dev/shm")
    return {
        "monotonic_ns": time.monotonic_ns(),
        "wall_ns": time.time_ns(),
        "names": sorted(os.listdir("/dev/shm")),
        "tmpfs_identity": [info.st_dev, info.st_ino],
        "pid_namespace": os.readlink("/proc/self/ns/pid"),
        "ns_last_pid": int(Path("/proc/sys/kernel/ns_last_pid").read_text()),
        "pid_max": int(Path("/proc/sys/kernel/pid_max").read_text()),
        "provenance": provenance,
        "provenance_note": note,
    }


def gate(config_path):
    clean_process()
    config = json.loads(Path(config_path).read_text())
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    connection.connect(config["socket"])
    connection.send(
        json.dumps(
            {
                "kind": "gate",
                "origin": "unfaked_top",
                "pid": os.getpid(),
                "ppid": os.getppid(),
                "token": token(os.getpid()),
                "generation": 0,
                "shared": "",
                "overrides": forbidden(os.environ),
            }
        ).encode()
    )
    read_fd, write_fd = os.pipe()
    connection.sendmsg(
        [b"P"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [write_fd]))]
    )
    os.close(write_fd)
    require(os.read(read_fd, 1) == b"G", "Pre-exec gate denied")
    os.close(read_fd)
    connection.set_inheritable(True)
    environment = dict(os.environ, **config["environment"])
    os.execve(
        config["probe"],
        [
            config["probe"],
            config["mode"],
            str(connection.fileno()),
            config["socket"],
            config["fixture"],
            "0",
        ],
        environment,
    )


class Observer:
    def __init__(self, arguments):
        self.args = arguments
        self.started = time.monotonic()
        self.deadline = self.started + CAP
        self.work = Path(arguments.work).resolve()
        self.out = Path(arguments.output).resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        self.out.mkdir(parents=True, exist_ok=False)
        self.records = []
        self.active = None
        self.command_groups = {}
        self.handles = []
        self.owned = {}
        self.connections = {}
        self.release_pipes = set()
        self.shared = None
        self.shared_identity = None
        self.local_deadline = self.deadline
        self.first_inventory = inventory(arguments.provenance, arguments.provenance_note)
        self.log("initial_inventory", **self.first_inventory)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.socket_path = str(self.work / "control.sock")
        require(len(self.socket_path) < 108, "Unix socket path exceeds target limit")
        require(not Path(self.socket_path).exists(), "Control socket already exists")
        self.listener.bind(self.socket_path)
        self.listener.listen(4)
        self.probe = str(self.work / "probe")
        self.fixture = str(self.work / "fixture")
        require(not Path(self.fixture).exists(), "Fixture must be newly owned by this run")
        fd = os.open(self.fixture, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        os.write(fd, b"independent clock regression\n")
        os.close(fd)
        os.utime(self.fixture, ns=(1600000000123456789, 1600000000987654321))
        self.real_stat = os.stat(self.fixture)
        self.log(
            "fixture",
            metadata=[getattr(self.real_stat, name) for name in META],
            times=[
                self.real_stat.st_atime_ns,
                self.real_stat.st_mtime_ns,
                self.real_stat.st_ctime_ns,
            ],
        )

    def log(self, kind, **fields):
        self.records.append(
            {"event": kind, "at_ns": time.monotonic_ns(), "wall_ns": time.time_ns(), **fields}
        )

    def remaining(self, maximum=120):
        value = min(self.deadline, self.local_deadline) - time.monotonic()
        require(value > 0, "Real-time allocation expired")
        return min(value, maximum)

    def command(self, arguments, maximum=10):
        process = subprocess.Popen(
            arguments,
            cwd=self.work,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.command_groups[process.pid] = process
        try:
            stdout, stderr = process.communicate(timeout=self.remaining(maximum))
        except BaseException:
            self.cleanup_commands()
            raise
        self.log("command", argv=arguments, status=process.returncode, stdout=stdout, stderr=stderr)
        self.cleanup_commands()
        require(process.returncode == 0 and not stderr, "Prerequisite/build command failed")
        return stdout

    def cleanup_commands(self):
        for root, process in list(self.command_groups.items()):
            # A waited root can still leave an adopted compiler child in its owned session.
            end = min(self.deadline, time.monotonic() + 2)
            reaped = []
            while True:
                try:
                    pid, status = os.waitpid(-root, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid:
                    reaped.append([pid, status])
                    if pid == root:
                        process.returncode = os.waitstatus_to_exitcode(status)
                    continue
                try:
                    os.killpg(root, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                require(time.monotonic() < end, "Owned prerequisite/build group was not reaped")
                time.sleep(0.01)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
            self.log("command_cleanup", root=root, reaped=reaped, remaining_owned=[])
            del self.command_groups[root]

    def receive(self, connection, maximum=5):
        ready, _, _ = select.select([connection], [], [], self.remaining(maximum))
        require(ready, "Event deadline expired")
        raw = connection.recv(8192)
        require(raw, "Process closed its control channel early")
        event = json.loads(raw)
        require(isinstance(event, dict), "Invalid event")
        self.log("observed", message=event)
        return event

    def expect(self, connection, kind):
        value = self.receive(connection)
        require(value.get("kind") == kind, f"Expected {kind}, got {value}")
        return value

    def accept_gate(self, generation):
        ready, _, _ = select.select([self.listener], [], [], self.remaining(5))
        require(ready, "No pre-exec PID rendezvous")
        connection, _ = self.listener.accept()
        event = self.expect(connection, "gate")
        ready, _, _ = select.select([connection], [], [], self.remaining(5))
        require(ready, "No actual pre-exec pipe supplied")
        raw, ancillary, flags, _ = connection.recvmsg(
            1, socket.CMSG_SPACE(array.array("i").itemsize)
        )
        require(
            raw == b"P"
            and not flags & (socket.MSG_CTRUNC | socket.MSG_TRUNC)
            and len(ancillary) == 1,
            "Malformed pipe transfer",
        )
        level, kind, data = ancillary[0]
        descriptors = array.array("i")
        descriptors.frombytes(data)
        require(
            level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS and len(descriptors) == 1,
            "Missing unique pre-exec pipe descriptor",
        )
        release_fd = descriptors[0]
        self.release_pipes.add(release_fd)
        require(
            stat.S_ISFIFO(os.fstat(release_fd).st_mode)
            and fcntl.fcntl(release_fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY,
            "Pre-exec release descriptor is not a pipe write end",
        )
        pid = event["pid"]
        credentials = struct.unpack(
            "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
        require(
            credentials[0] == pid and credentials[1] == os.getuid(), "Actual peer PID/UID mismatch"
        )
        require(
            event["generation"] == generation and token(pid) == event["token"],
            "Pre-exec process identity mismatch",
        )
        if generation == 0:
            require(
                event["origin"] == "unfaked_top"
                and event["shared"] == ""
                and event["overrides"] == [],
                "Top-level gate is not unfaked/clean",
            )
            require(pid == self.active.pid, "Top-level process changed PID")
            require(
                "libfaketime" not in Path(f"/proc/{pid}/maps").read_text(),
                "Top-level gate already loaded libfaketime",
            )
        else:
            require(event["origin"] == "loaded_descendant", "Descendant origin misreported")
            require(event["shared"] == self.shared, "Descendant lost parent's shared identity")
            require(
                not event["fake_pid"], "Descendant's current environment contains fake-PID setting"
            )
            require(event["ppid"] == self.owned[generation - 1][0], "Not a real descendant")
            require(token(self.owned[0][0]) == self.owned[0][1], "Root parent no longer alive")
            require(
                self.pair_identity() == self.shared_identity,
                "Inherited shared pair missing/replaced",
            )
            self.check_shared_mapping(pid)
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        require(
            not any(item.startswith(b"FAKETIME_FAKEPID=") for item in environ),
            "Fake-PID setting inherited",
        )
        receipts = []
        for name in (f"sem.faketime_sem_{pid}", f"faketime_shm_{pid}"):
            path = "/dev/shm/" + name
            try:
                os.lstat(path)
            except OSError as error:
                require(error.errno == errno.ENOENT, "Absence not proved with ENOENT")
                receipts.append({"path": path, "errno": error.errno, "at_ns": time.monotonic_ns()})
            else:
                raise RuntimeError(f"PID-owned shared object already exists: {path}; no retry")
        require(token(pid) == event["token"], "PID identity changed during guard")
        self.owned[generation] = (pid, event["token"])
        self.connections[generation] = connection
        self.log(
            "preexec_ENOENT",
            pid=pid,
            generation=generation,
            receipts=receipts,
            inherited_shared=event["shared"],
            peer_credentials=list(credentials),
            release_barrier="verified anonymous pipe",
            exclusive_note=self.args.exclusive_note,
        )
        if generation == 0:
            self.root_release_wall_ns = time.time_ns()
        require(os.write(release_fd, b"G") == 1, "Pipe release failed")
        os.close(release_fd)
        self.release_pipes.remove(release_fd)
        self.log("exec_released", pid=pid, generation=generation)
        return connection

    def pair_identity(self):
        pid = self.owned[0][0]
        result = {}
        for name in (f"sem.faketime_sem_{pid}", f"faketime_shm_{pid}"):
            value = os.lstat("/dev/shm/" + name)
            require(
                stat.S_ISREG(value.st_mode) and value.st_uid == os.getuid(),
                "Shared pair is not a regular object owned by the runner",
            )
            result[name] = [value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid]
        self.log("shared_pair_identity", identity=result)
        return result

    def check_shared_mapping(self, pid):
        maps = Path(f"/proc/{pid}/maps").read_text()
        shm_name = f"faketime_shm_{self.owned[0][0]}"
        identity = self.shared_identity[shm_name]
        matches = []
        for row in maps.splitlines():
            columns = row.split(maxsplit=5)
            if len(columns) == 6 and columns[5] == "/dev/shm/" + shm_name:
                device = tuple(int(part, 16) for part in columns[3].split(":"))
                matches.append(
                    int(columns[4]) == identity[1]
                    and device == (os.major(identity[0]), os.minor(identity[0]))
                    and columns[1].startswith("rw-s")
                )
        require(matches and all(matches), "Living parent's shared-memory mapping absent/replaced")
        self.log("shared_mapping_identity", pid=pid, name=shm_name, identity=identity)

    def start(self, label, library, mode, cap):
        self.local_deadline = min(self.deadline - 3, time.monotonic() + cap)
        self.owned, self.connections, self.shared, self.shared_identity = {}, {}, None, None
        self.current_library = library
        config = {
            "socket": self.socket_path,
            "probe": self.probe,
            "fixture": self.fixture,
            "mode": mode,
            "environment": {
                "LD_PRELOAD": library,
                "FAKETIME": "2014-07-21 09:00:00" if mode == "fixed" else "+60 x2",
                "FAKETIME_DONT_FAKE_MONOTONIC": "1",
                "FAKETIME_DONT_RESET": "1",
            },
        }
        config_path = self.work / "config.json"
        config_path.write_text(json.dumps(config, sort_keys=True))
        self.log(
            "launch",
            label=label,
            config=config,
            uid=os.getuid(),
            cwd=str(self.work),
            library_sha256=digest(library),
            probe_sha256=digest(self.probe),
        )
        stdout = open(self.out / (label + ".stdout"), "wb")
        stderr = open(self.out / (label + ".stderr"), "wb")
        self.handles = [stdout, stderr]
        self.active = subprocess.Popen(
            [sys.executable, str(ROOT / "observe.py"), "_gate", str(config_path)],
            cwd=self.work,
            env=dict(os.environ),
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        return self.accept_gate(0)

    def ready(self, connection, generation):
        event = self.expect(connection, "ready")
        if generation == 0:
            self.root_ready_wall_ns = time.time_ns()
        pid, start = self.owned[generation]
        require(event["pid"] == pid and event["token"] == start, "Exec changed PID/start token")
        require(not event["fake_pid"], "Current environment contains fake-PID setting")
        expected_shared = f"/faketime_sem_{self.owned[0][0]} /faketime_shm_{self.owned[0][0]}"
        require(event["shared"] == expected_shared, "Shared path did not execute")
        if generation == 0:
            self.shared = event["shared"]
            self.shared_identity = self.pair_identity()
        else:
            require(
                self.pair_identity() == self.shared_identity, "Shared identity changed after exec"
            )
        maps = Path(f"/proc/{pid}/maps").read_text()
        (self.out / f"{self.label}-{generation}.maps").write_text(maps)
        self.check_shared_mapping(pid)
        require(
            any(line.split(maxsplit=5)[-1] == self.current_library for line in maps.splitlines()),
            "Expected library not mapped",
        )
        identity = self.expect(connection, "library")
        require(
            all(
                os.path.realpath(identity[key]) == self.current_library
                for key in ("clock", "legacy", "fstat")
            ),
            "Wrong symbol provider",
        )
        require(identity["stat_ver"] == self.abi["stat_ver"], "Legacy ABI changed")

    def sample(self, connection):
        before = time.monotonic_ns()
        connection.send(b"S")
        event = self.expect(connection, "sample")
        after = time.monotonic_ns()
        require(after - before <= 250_000_000, "Sample bracket wider than 250 ms")
        require(
            all(type(event[key]) is int and event[key] >= 0 for key in ("mono", "wall")),
            "Invalid clock readings",
        )
        require(
            before - 50_000_000 <= event["mono"] <= after + 50_000_000,
            "Monotonic reading not consistent with unfaked bracket",
        )
        result = {**event, "before": before, "after": after}
        self.log("sample_bracket", sample=result)
        return result

    def check_stats(self, connection, fixed):
        connection.send(b"M")
        for api in ("fstat", "__fxstat64"):
            value = self.expect(connection, "stat")
            require(value["api"] == api, "Missing ordinary stat API")
            require(
                value["meta"] == [getattr(self.real_stat, name) for name in META],
                "Non-time stat metadata changed",
            )
            require(all(type(item) is int for item in value["times"]), "Invalid stat timestamps")
            if fixed:
                require(
                    value["times"] == [FIXED_NS] * 3,
                    "Stat timestamps were not faked; blanket real-stat passthrough fails",
                )
        current = os.stat(self.fixture)
        require(
            [getattr(current, key) for key in META]
            == [getattr(self.real_stat, key) for key in META],
            "Observer fixture metadata changed",
        )
        require(
            [current.st_atime_ns, current.st_mtime_ns, current.st_ctime_ns]
            == [self.real_stat.st_atime_ns, self.real_stat.st_mtime_ns, self.real_stat.st_ctime_ns],
            "Fixture's actual timestamps changed",
        )

    def progressing(self, anchor, later):
        minimum = 2 * (later["before"] - anchor["after"]) - 100_000_000
        maximum = 2 * (later["after"] - anchor["before"]) + 100_000_000
        require(
            minimum <= later["wall"] - anchor["wall"] <= maximum,
            "x2 shared timeline reset or did not track the unfaked interval",
        )

    def finish(self, connection):
        connection.send(b"Q")
        self.expect(connection, "complete")

    def cleanup(self):
        self.cleanup_commands()
        for descriptor in self.release_pipes:
            os.close(descriptor)
        self.release_pipes.clear()
        if self.active is None:
            return
        root = self.active.pid
        # The observer created this session. Every descendant stays in its process group.
        signals = []

        def pending():
            # waitpid proves a member is still our child before any numeric-group signal.
            # A fully reaped session must never be signalled after possible PID reuse.
            while True:
                try:
                    pid, status = os.waitpid(-root, os.WNOHANG)
                except ChildProcessError:
                    return False
                if pid == 0:
                    return True
                self.log("reaped", pid=pid, status=status)
                if pid == root:
                    self.active.returncode = os.waitstatus_to_exitcode(status)

        if pending():
            try:
                os.killpg(root, signal.SIGTERM)
                signals.append("SIGTERM")
            except ProcessLookupError:
                pass
            end = min(self.deadline, time.monotonic() + 2)
            while pending() and time.monotonic() < end:
                time.sleep(0.02)
        if pending():
            try:
                os.killpg(root, signal.SIGKILL)
                signals.append("SIGKILL")
            except ProcessLookupError:
                pass
            # Reap each adopted member, including a fork that died before its rendezvous.
            while pending():
                require(time.monotonic() < self.deadline, "Owned cleanup exceeded aggregate cap")
                time.sleep(0.01)
        alive = []
        for pid, start in self.owned.values():
            try:
                if token(pid) == start:
                    alive.append(pid)
            except FileNotFoundError:
                pass
        self.log(
            "cleanup",
            root=root,
            signals=signals,
            remaining_owned=alive,
            inventory=inventory(self.args.provenance, self.args.provenance_note),
        )
        require(not alive, "An owned process remains alive or unreaped")
        for connection in self.connections.values():
            connection.close()
        for handle in self.handles:
            handle.close()
        self.active, self.connections, self.handles = None, {}, []

    def normal_exit(self, label, markers):
        status = self.active.wait(timeout=self.remaining(3))
        self.log("normal_exit", label=label, status=status)
        require(status == 0, "Probe did not exit normally")
        for handle in self.handles:
            handle.flush()
        require((self.out / (label + ".stderr")).read_bytes() == b"", "Unexpected probe stderr")
        lines = (self.out / (label + ".stdout")).read_text().splitlines()
        require(sorted(lines) == sorted(markers), "Missing/extra explicit completion marker")

    def negative(self, library):
        self.label = "original-fixed"
        connection = self.start(self.label, library, "fixed", 15)
        end = min(self.deadline - 3, time.monotonic() + 10)
        matches = []
        while time.monotonic() < end:
            require(self.active.poll() is None, "Original exited; repair proof NOT ESTABLISHED")
            ready, _, _ = select.select(
                [connection], [], [], max(0, min(0.2, end - time.monotonic()))
            )
            require(not ready, "Original reached main/closed channel; proof NOT ESTABLISHED")
            pid = self.active.pid
            syscall = Path(f"/proc/{pid}/syscall").read_text().strip()
            maps = Path(f"/proc/{pid}/maps").read_text()
            wchan = Path(f"/proc/{pid}/wchan").read_text().strip()
            snapshot = {"pid": pid, "syscall": syscall, "wchan": wchan, "maps": maps}
            fields = syscall.split()
            if fields and fields[0] != "running" and int(fields[0], 0) == self.abi["futex_syscall"]:
                address = int(fields[1], 0)
                for row in maps.splitlines():
                    columns = row.split(maxsplit=5)
                    lo, hi = (int(part, 16) for part in columns[0].split("-"))
                    if (
                        len(columns) == 6
                        and columns[5] == library
                        and "w" in columns[1]
                        and lo <= address < hi
                    ):
                        offset = address - lo + int(columns[2], 16)
                        snapshot.update(futex_address=address, library_file_offset=offset)
                        if offset == INIT_OFFSET and int(fields[2], 0) & 127 == 0:
                            matches.append(time.monotonic_ns())
            self.log("negative_snapshot", **snapshot)
        require(
            len(matches) >= 2 and matches[-1] - matches[0] >= 250_000_000,
            "Exact writable-mapping initialization futex cut not established",
        )
        self.log("negative_cut_established", file_offset=INIT_OFFSET, match_count=len(matches))
        self.cleanup()
        require(
            not (self.out / (self.label + ".stdout")).read_bytes(),
            "Original produced a marker/output; proof NOT ESTABLISHED",
        )
        require(
            not (self.out / (self.label + ".stderr")).read_bytes(),
            "Original produced a loader/prerequisite error; proof NOT ESTABLISHED",
        )

    def positive(self, library):
        self.label = "candidate-fixed"
        connection = self.start(self.label, library, "fixed", 12)
        self.ready(connection, 0)
        first = self.sample(connection)
        self.check_stats(connection, True)
        time.sleep(0.2)
        second = self.sample(connection)
        require(first["wall"] == second["wall"] == FIXED_NS, "Fixed date differs")
        require(second["mono"] > first["mono"], "Monotonic clock did not progress")
        self.finish(connection)
        self.normal_exit(self.label, ["COMPLETE fixed 0"])
        self.cleanup()

    def timeline(self, library):
        self.label = "candidate-timeline"
        root = self.start(self.label, library, "timeline", 25)
        self.ready(root, 0)
        before_wall = time.time_ns()
        anchor = self.sample(root)
        after_wall = time.time_ns()
        require(
            2 * before_wall - self.root_ready_wall_ns + 60_000_000_000 - 100_000_000
            <= anchor["wall"]
            <= 2 * after_wall - self.root_release_wall_ns + 60_000_000_000 + 100_000_000,
            "Initial +60 x2 value disagrees with unfaked exec/ready/sample bounds",
        )
        self.check_stats(root, False)
        thread_before = time.monotonic_ns()
        root.send(b"T")
        for index in range(2):
            thread = self.expect(root, "thread")
            require(
                thread["index"] == index and thread["count"] == 64 and not thread["error"],
                "Concurrent read/stat probe failed",
            )
            require(
                thread["last"] >= thread["first"] and thread["wall_last"] >= thread["wall_first"],
                "Thread clock went backward",
            )
            require(
                thread_before - 50_000_000
                <= thread["first"]
                <= thread["last"]
                <= time.monotonic_ns() + 50_000_000,
                "Thread monotonic readings outside real bracket",
            )
        time.sleep(2)
        self.progressing(anchor, self.sample(root))
        root.send(b"F")
        self.expect(root, "spawn")
        child = self.accept_gate(1)
        self.ready(child, 1)
        self.progressing(anchor, self.sample(child))
        self.check_stats(child, False)
        time.sleep(2)
        self.progressing(anchor, self.sample(root))
        child.send(b"F")
        self.expect(child, "spawn")
        grandchild = self.accept_gate(2)
        self.ready(grandchild, 2)
        self.progressing(anchor, self.sample(grandchild))
        self.check_stats(grandchild, False)
        self.progressing(anchor, self.sample(child))
        self.progressing(anchor, self.sample(root))
        self.finish(grandchild)
        self.finish(child)
        self.finish(root)
        self.normal_exit(self.label, [f"COMPLETE timeline {index}" for index in range(3)])
        self.cleanup()

    def run(self):
        require(sys.version_info >= (3, 12), "Python 3.12+ required")
        require(os.getuid() != 0, "Native runner must be non-root")
        require(platform.machine() == "aarch64", "Native target must be aarch64")
        self.log(
            "host",
            python=sys.version,
            uname=list(platform.uname()),
            uid=os.getuid(),
            environment={key: os.environ.get(key) for key in ("PATH", "TZ", "LC_ALL")},
            exclusive_note=self.args.exclusive_note,
        )
        rpm = self.command(
            ["rpm", "-q", "--qf", "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n", "glibc"]
        )
        require(rpm.strip() == "glibc-2.28-251.el8_10.48.aarch64", "Wrong native glibc build")
        source = str(ROOT / "probe.c")
        self.command(
            [
                "gcc",
                "-std=gnu11",
                "-O0",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-U_FILE_OFFSET_BITS",
                "-U_TIME_BITS",
                source,
                "-o",
                self.probe,
                "-ldl",
                "-pthread",
                "-lrt",
            ],
            15,
        )
        self.abi = json.loads(self.command([self.probe, "abi"]))
        require(self.abi["glibc"] == "2.28", "Unexpected target headers/runtime")
        self.log("probe_identity", sha256=digest(self.probe), abi=self.abi)
        output = self.command(
            [
                "perl",
                "-w",
                "-MTime::HiRes=clock_gettime,CLOCK_MONOTONIC",
                "-E",
                'say "VERSION perl=$^V Time::HiRes=$Time::HiRes::VERSION"; '
                "say clock_gettime(CLOCK_MONOTONIC); select undef,undef,undef,0.2; "
                "say clock_gettime(CLOCK_MONOTONIC)",
            ],
            5,
        )
        lines = output.splitlines()
        require(
            len(lines) == 3 and lines[0].startswith("VERSION perl="),
            "Missing import/version output",
        )
        values = [float(value) for value in lines[1:]]
        require(
            all(math.isfinite(value) for value in values) and values[1] > values[0],
            "Unfaked prerequisite did not produce two increasing numeric readings",
        )
        require(digest(self.args.original) == ORIGINAL, "Original exact bytes do not match")
        require(
            digest(self.args.candidate) == self.args.candidate_sha256,
            "Candidate bytes do not match",
        )
        self.negative(self.args.original)
        self.positive(self.args.candidate)
        self.timeline(self.args.candidate)
        require(
            digest(self.args.original) == ORIGINAL
            and digest(self.args.candidate) == self.args.candidate_sha256,
            "Library bytes changed during run",
        )


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "_gate":
        gate(sys.argv[2])
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("--original", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--provenance", required=True, choices=("fresh", "retained", "unknown"))
    parser.add_argument("--provenance-note", required=True)
    parser.add_argument("--exclusive-note", required=True)
    args = parser.parse_args()
    require(
        os.environ.get("TZ") == "UTC" and os.environ.get("LC_ALL") == "C",
        "Run the clean observer with TZ=UTC and LC_ALL=C",
    )
    require(
        all(
            re.fullmatch(r"/[A-Za-z0-9_./-]+", path)
            for path in (args.original, args.candidate, args.work, args.output, sys.executable)
        ),
        "Use absolute paths without whitespace, escapes or shell syntax",
    )
    args.original, args.candidate = (
        os.path.realpath(args.original),
        os.path.realpath(args.candidate),
    )
    require(
        args.provenance_note.strip() and args.exclusive_note.strip(),
        "Missing provenance/exclusivity record",
    )
    clean_process()
    # Subreaping owns orphaned child/grandchild cleanup without touching another session.
    libc = ctypes.CDLL(None, use_errno=True)
    prior = ctypes.c_int()
    require(libc.prctl(37, ctypes.byref(prior), 0, 0, 0) == 0, "Cannot query subreaper state")
    require(libc.prctl(36, 1, 0, 0, 0) == 0, "Cannot become an owned-child subreaper")
    observer = None
    status, reason = "NOT ESTABLISHED", "Observer did not finish"

    def interrupted(signum, frame):
        raise RuntimeError(f"Observer interrupted by signal {signum}; owned cleanup required")

    saved_handlers = {
        number: signal.signal(number, interrupted)
        for number in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM)
    }
    try:
        observer = Observer(args)
        signal.setitimer(signal.ITIMER_REAL, max(0.001, observer.deadline - time.monotonic() - 3))
        observer.run()
        status, reason = "PASS", "Sealed original cut and candidate assertions met"
    except Exception as error:
        reason = f"{type(error).__name__}: {error}"
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if observer is not None:
            try:
                observer.cleanup()
            except Exception as error:
                status, reason = "NOT ESTABLISHED", f"Cleanup failed: {error}; {reason}"
            observer.log("final_inventory", **inventory(args.provenance, args.provenance_note))
            observer.listener.close()
            Path(observer.socket_path).unlink(missing_ok=True)
            report = {
                "status": status,
                "reason": reason,
                "aggregate_cap_seconds": CAP,
                "elapsed_seconds": time.monotonic() - observer.started,
                "records": observer.records,
            }
            (observer.out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        libc.prctl(36, prior.value, 0, 0, 0)
        for number, handler in saved_handlers.items():
            signal.signal(number, handler)
    print(json.dumps({"status": status, "reason": reason}))
    if status != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
