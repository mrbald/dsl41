"""Close the pipes of a child process a test started."""

from __future__ import annotations

import subprocess


def close_pipes(proc: subprocess.Popen) -> None:
    """Close the stdin, stdout and stderr pipes `Popen` made, for a test that
    waits for the child itself. An open pipe is a ResourceWarning when the
    object is collected. Call it after the child has ended."""
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe is not None:
            pipe.close()
