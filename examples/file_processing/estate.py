"""Python authoring for one literal-path FW and two ordinary command jobs."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from dsl41.dsl import CatalogBuilder
from dsl41.file_processing_dsl import add_file_processing_jobs


def build_estate(root: Path, ready_signal: Path, python: Path, machine: str) -> CatalogBuilder:
    builder = CatalogBuilder()
    builder.job(
        "INBOX_FW",
        job_type="f",
        machine=machine,
        watch_file=str(ready_signal),
        watch_interval=1,
        watch_file_min_size=1,
    )
    worker = [str(python), "-m", "examples.file_processing.worker"]
    add_file_processing_jobs(
        builder,
        inbox_watcher="INBOX_FW",
        machine=machine,
        archive_argv=[*worker, "archive", "--root", str(root)],
        process_argv=[*worker, "process", "--root", str(root)],
    )
    return builder


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--ready-signal", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--machine", default="localhost")
    args = parser.parse_args()
    print(build_estate(args.root, args.ready_signal, args.python, args.machine).to_jil())


if __name__ == "__main__":
    main()
