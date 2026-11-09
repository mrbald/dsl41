#!/usr/bin/env python3
"""Independent inventory and output checks; never executes or changes upstream tests."""

import argparse
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import time

SECTIONS = [
    (
        "Running the test program with no faked time specified",
        "Running the test program with your system-wide default in /etc/faketimerc",
    ),
    ("Running the test program with absolute date 2003-01-01 10:00:05 specified",),
    ("Running the test program with START date @2005-03-29 14:14:14 specified",),
    ("Running the test program with 10 days negative offset specified",),
    ("Running the test program with 10 days negative offset specified, and FAKE_STAT disabled",),
    ("Running the test program with 10 days positive offset specified, and speed-up factor",),
    ("Running the 'date' command with 15 days negative offset specified",),
    ("Running the test program with malloc interception",),
    ("Running the test program with malloc interception and file faketimerc",),
]
TIMETEST_MARKERS = (
    "pthread_cond_timedwait: CLOCK_REALTIME test",
    "pthread_cond_timedwait: CLOCK_MONOTONIC test",
    "Blocking signal ",
    "time()         : Current date and time:",
    "time(NULL)     : Seconds since Epoch  :",
    "ftime()        : Current date and time:",
    "(Intentionally sleeping 2 seconds...)",
    "gettimeofday() : Current date and time:",
    "clock_gettime(): Current date and time:",
    "stat(): mod. time of file",
)
SUITES = {"test_exclude_mono.sh": 1, "test_null.sh": 1, "test_true.sh": 1, "test_walkone.sh": 31}


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def inventory(args):
    names = sorted(os.listdir("/dev/shm"))
    stat = os.stat("/dev/shm")
    baseline = json.loads(Path(args.baseline_report).read_text())
    entries = [row for row in baseline["records"] if row["event"] == "initial_inventory"]
    require(len(entries) == 1, "Missing unique original inventory")
    initial = entries[0]
    same_namespace = initial["pid_namespace"] == os.readlink("/proc/self/ns/pid") and initial[
        "tmpfs_identity"
    ] == [stat.st_dev, stat.st_ino]
    additions = sorted(set(names) - set(initial["names"])) if same_namespace else []
    relevant = sorted(
        {name for name in names if "faketime" in name or name.startswith("sem.")} | set(additions)
    )
    # Every surviving addition may include sem_open's anonymous temporary name.
    # Any residue stops the upstream gate; a guessed future PID is never used.
    receipt = {
        "at_ns": time.monotonic_ns(),
        "wall_ns": time.time_ns(),
        "names": names,
        "tmpfs_identity": [stat.st_dev, stat.st_ino],
        "pid_namespace": os.readlink("/proc/self/ns/pid"),
        "ns_last_pid": int(Path("/proc/sys/kernel/ns_last_pid").read_text()),
        "pid_max": int(Path("/proc/sys/kernel/pid_max").read_text()),
        "provenance": args.provenance,
        "provenance_note": args.provenance_note,
        "fd0": {
            "target": os.readlink("/proc/self/fd/0"),
            "access_mode": fcntl.fcntl(0, fcntl.F_GETFL) & os.O_ACCMODE,
        },
        "same_namespace_as_regression": same_namespace,
        "additions_since_negative_control_start": additions,
        "relevant_residues": relevant,
        "decision": "STOP" if relevant else "CLEAR_AT_INVENTORY_TIME",
        "pid_reuse": "possible; coverage limited, do not run gate"
        if relevant
        else "no relevant names present; exclusivity must continue",
    }
    Path(args.output).write_text(json.dumps(receipt, indent=2) + "\n")
    require(
        not relevant, "Residue could collide after PID reuse; stop without deletion/PID hunting"
    )


def trace_statuses(directory):
    timetests, dates = [], []
    for path in sorted(Path(directory).glob("child.*")):
        text = path.read_text()
        executed = re.findall(r'execve\("([^"]+)", \[([^\n]*)\].*\) = 0$', text, re.M)
        if not executed:
            continue
        program, arguments = executed[-1]
        name = os.path.basename(program)
        if name not in ("timetest", "date", "perl"):
            continue
        exits = re.findall(r"\+\+\+ exited with ([0-9]+) \+\+\+", text)
        require(len(exits) == 1 and exits[0] == "0", f"Command did not exit normally: {path}")
        require("killed by" not in text, f"Command caught a fatal signal: {path}")
        if name == "timetest":
            timetests.append(str(path))
        if name == "date" and arguments in ('"date"', '"/usr/bin/date"', '"/bin/date"'):
            dates.append(str(path))
    require(len(timetests) == 8, "Need eight observed successful timetest exec/exit pairs")
    require(len(dates) == 1, "Need the ninth section's observed successful date exec/exit pair")
    return {"timetest_traces": timetests, "date_trace": dates[0]}


def check(args):
    require(args.make_status == 0, "make test did not exit 0")
    output = Path(args.combined_log).read_text()
    require(output.endswith("\n"), "Truncated stdout/stderr log")
    inventory_receipt = json.loads(Path(args.inventory_receipt).read_text())
    require(
        inventory_receipt["decision"] == "CLEAR_AT_INVENTORY_TIME"
        and inventory_receipt.get("fd0", {}).get("target"),
        "Missing pre-gate residue/fd0 receipt",
    )
    report = json.loads(Path(args.regression_report).read_text())
    abi_rows = [row for row in report["records"] if row["event"] == "probe_identity"]
    require(report["status"] == "PASS" and len(abi_rows) == 1, "No sealed native probe result")
    sigusr1 = abi_rows[0]["abi"]["sigusr1"]
    summaries = re.findall(
        r"^# functests/(test_[^ ]+\.sh) summary: (\d+) succeeded, (\d+) failed$", output, re.M
    )
    require(
        len(summaries) == 4
        and {name: int(ok) for name, ok, _ in summaries} == SUITES
        and all(failed == "0" for _, _, failed in summaries),
        "Four functional summaries not proved",
    )
    for name in SUITES:
        require(
            output.count(f"# End functests/{name} - OK") == 1, "Functional suite did not end OK"
        )
    require(
        output.count("# Test Suites summary: 4 succeeded, 0 failed") == 1
        and output.count("# End Test Suites - OK") == 1,
        "Functional aggregate missing",
    )
    diagnostics = "\n".join(
        line
        for line in output.splitlines()
        if not re.fullmatch(
            r"# (functests/test_[^ ]+\.sh|Test Suites) "
            r"summary: \d+ succeeded, 0 failed",
            line,
        )
    )
    require(
        not re.search(
            r"\bfailed\b|Segmentation fault|command not found|out of memory|"
            r"cannot open shared object|undefined symbol|\babort(?:ed)?\b|"
            r"^(sigaction|sigprocmask|timer_create|timer_settime):|"
            r"(^|\s)(error:|fatal:)",
            diagnostics,
            re.I | re.M,
        ),
        "Upstream printed an assertion/perror/abnormal/loader/missing-command diagnostic",
    )
    lines = output.splitlines()
    starts = []
    for alternatives in SECTIONS:
        indexes = [index for index, line in enumerate(lines) if line in alternatives]
        require(len(indexes) == 1, "Missing or repeated test.sh section")
        starts.append(indexes[0])
    require(starts == sorted(starts), "test.sh sections out of order")
    require(lines.count("Testing finished.") == 1, "Missing test.sh final marker")
    starts.append(lines.index("Testing finished."))
    signal_total, section_receipts = 0, []
    for index in range(9):
        section_lines = lines[starts[index] + 1 : starts[index + 1]]
        command_lines = [
            line
            for line in section_lines
            if line.startswith(("$ LD_PRELOAD=", "LD_PRELOAD=", "$ FAKETIME_NO_CACHE="))
        ]
        require(len(command_lines) == 1, "Missing/extra echoed source command")
        allocator = re.compile(
            r"Called (malloc\(\) from|free\(\) on from) libmallocintercept\.\.\."
            r"(?:FAIL_PRE_INIT_CALLS not defined, skipping poke_faketime\(\) )?successfully"
        )
        payload = [
            line
            for line in section_lines
            if line and not re.fullmatch("=+", line) and line not in command_lines
        ]
        if index != 6:
            positions = []
            for marker in TIMETEST_MARKERS:
                found = [
                    position for position, line in enumerate(payload) if line.startswith(marker)
                ]
                require(len(found) == 1, f"Section {index + 1}: missing/duplicate marker {marker}")
                positions.append(found[0])
            require(positions == sorted(positions), "Timestest progress markers out of order")
            require(payload[positions[2]] == f"Blocking signal {sigusr1}", "Wrong signal number")
            signal_count = 0
            for position, line in enumerate(payload):
                if line.startswith("Caught"):
                    require(
                        line == f"Caught signal {sigusr1}" and position > positions[7],
                        "Unexpected/malformed/early signal marker",
                    )
                    signal_count += 1
                elif allocator.fullmatch(line):
                    require(index in (7, 8), "Allocator diagnostics outside sections 8-9")
                elif position in positions:
                    marker = TIMETEST_MARKERS[positions.index(position)]
                    if marker in (
                        TIMETEST_MARKERS[3],
                        TIMETEST_MARKERS[5],
                        TIMETEST_MARKERS[7],
                        TIMETEST_MARKERS[8],
                    ):
                        datetime.strptime(line[len(marker) :].strip(), "%a %b %d %H:%M:%S %Y")
                    elif marker == TIMETEST_MARKERS[4]:
                        require(
                            re.fullmatch(r"[0-9]+", line[len(marker) :].strip()),
                            "Invalid epoch reading",
                        )
                    elif marker == TIMETEST_MARKERS[2]:
                        pass
                    elif marker == TIMETEST_MARKERS[9]:
                        match = re.fullmatch(r"stat\(\): mod\. time of file '[^']+': (.+)", line)
                        require(match, "Malformed final stat marker")
                        datetime.strptime(match[1], "%a %b %d %H:%M:%S %Y")
                    else:
                        require(line == marker, "Broken progress heading")
                else:
                    require(
                        line
                        in (
                            "(Intentionally sleeping 1 second...)",
                            "(If this test hangs for more than a few seconds, please report",
                            " your glibc version and system details as FORCE_MONOTONIC_FIX",
                            " issue at https://github.com/wolfcw/libfaketime)",
                        ),
                        f"Unexpected/unparseable section output: {line}",
                    )
            require(signal_count >= 1, "Expected timer SIGUSR1 output missing")
            signal_total += signal_count
            section_receipts.append({"section": index + 1, "sigusr1_count": signal_count})
        else:
            require(
                len(payload) == 1
                and re.fullmatch(
                    r"(Mon|Tue|Wed|Thu|Fri|Sat|Sun) "
                    r"[A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2} UTC \d{4}",
                    payload[0],
                ),
                "Section 7 must contain exactly one date output line and no errors",
            )
            datetime.strptime(payload[0], "%a %b %d %H:%M:%S UTC %Y")
    for position, line in enumerate(lines):
        if re.search("Caught[- ]signal", line, re.I):
            require(
                any(
                    starts[index] < position < starts[index + 1]
                    for index in (0, 1, 2, 3, 4, 5, 7, 8)
                ),
                "Signal output outside timetest sections",
            )
        if line.startswith(("Called ", "FAIL_PRE_INIT_CALLS not defined", "successfully")):
            require(
                any(starts[index] < position < starts[index + 1] for index in (7, 8)),
                "Allocator diagnostics outside sections 8-9",
            )
    receipt = trace_statuses(args.trace_directory)
    print(
        json.dumps(
            {
                "status": "PASS",
                "functional_suites": 4,
                "test_sh_sections": 9,
                "fd0": inventory_receipt["fd0"],
                "sigusr1_total": signal_total,
                "sections": section_receipts,
                **receipt,
            }
        )
    )


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    inv = sub.add_parser("inventory")
    inv.add_argument("--baseline-report", required=True)
    inv.add_argument("--output", required=True)
    inv.add_argument("--provenance", required=True, choices=("fresh", "retained", "unknown"))
    inv.add_argument("--provenance-note", required=True)
    chk = sub.add_parser("check")
    chk.add_argument("--combined-log", required=True)
    chk.add_argument("--inventory-receipt", required=True)
    chk.add_argument("--regression-report", required=True)
    chk.add_argument("--make-status", required=True, type=int)
    chk.add_argument("--trace-directory", required=True)
    args = parser.parse_args()
    try:
        inventory(args) if args.action == "inventory" else check(args)
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "NOT ESTABLISHED", "reason": str(error)}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
