"""The file-processing estate: one literal-path FW and the two worker jobs."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from dsl41.dsl import CatalogBuilder
from dsl41.file_processing_dsl import add_file_processing_jobs


def estate(root: Path, ready_signal: Path, python: Path, machine: str) -> CatalogBuilder:
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
        process_argv=[
            *worker,
            "process",
            *common,
            "--handler",
            "examples.file_processing.worker:counts_handler",
        ],
    )
    return builder


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--ready-signal", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--machine", default="localhost")
    args = parser.parse_args()
    print(estate(args.root, args.ready_signal, args.python, args.machine).to_jil())


if __name__ == "__main__":
    main()
