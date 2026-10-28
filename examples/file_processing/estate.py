"""The file-processing estate: one literal-path FW and the two worker jobs.

With a control socket, it also adds REARM, the example's activation (CONTRACT.md
"Activation is external", DL-417): after the archiver's success and the processor's end
it runs `examples.file_processing.rearm`, which starts the watcher again and publishes
the ready signal again while work is pending.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import shlex
import sys

from dsl41.dsl import CatalogBuilder
from dsl41.file_processing_dsl import add_file_processing_jobs

COUNTS = "examples.file_processing.worker:counts_handler"


def estate(
    root: Path,
    ready_signal: Path,
    python: Path,
    machine: str,
    *,
    handler: str = COUNTS,
    rearm: Path | None = None,
) -> CatalogBuilder:
    builder = CatalogBuilder()
    builder.job(
        "INBOX_FW",
        job_type="f",
        machine=machine,
        watch_file=str(ready_signal),
        watch_interval=1,
        watch_file_min_size=1,
    )
    worker = [str(python), "-m", "dsl41.file_processing_worker"]
    common = ["--root", str(root), "--processor-id", "counts-v1"]
    add_file_processing_jobs(
        builder,
        inbox_watcher="INBOX_FW",
        machine=machine,
        archive_argv=[*worker, "archive", *common],
        process_argv=[*worker, "process", *common, "--handler", handler],
    )
    if rearm is not None:
        # After the archiver's success and the processor's end, whatever the outcome (DL-417).
        command = [str(python), "-m", "examples.file_processing.rearm", *common]
        command += ["--signal", str(ready_signal), "--socket", str(rearm)]
        builder.job(
            "REARM",
            machine=machine,
            condition="s(ARCHIVER, 0) & d(PROCESSOR, 0)",
            command=shlex.join(command),
        )
    return builder


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--ready-signal", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--machine", default="localhost")
    parser.add_argument("--handler", default=COUNTS, metavar="MODULE:FACTORY")
    parser.add_argument("--rearm", type=Path, metavar="CONTROL_SOCKET")
    args = parser.parse_args()
    builder = estate(
        args.root,
        args.ready_signal,
        args.python,
        args.machine,
        handler=args.handler,
        rearm=args.rearm,
    )
    print(builder.to_jil())


if __name__ == "__main__":
    main()
