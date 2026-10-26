"""Exit codes and the exception mapping shared by the sample CLIs.

CONTRACT.md "Activation is external": 1 is a stopped run whose reason is in the
ledger. A busy role lock and a refusal are never success and never a stopped run;
their values come from sysexits.h. ProtocolError is a ValueError; the library's other
ValueErrors reject arguments such as an invalid processor id. A ledger busy past its
timeout stays an OperationalError, so it exits 1 with its reason on stderr.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import sys

from dsl41.file_processing import LockBusy

EXIT_STOPPED = 1
EXIT_LOCK_BUSY = 75  # EX_TEMPFAIL: the role's lock was busy; nothing ran.
EXIT_REFUSED = 65  # EX_DATAERR: corrupt, inconsistent or unsupported worker input.


@contextmanager
def exit_codes(command: str) -> Iterator[None]:
    """Map a busy lock to 75 and a refusal to 65, each with one line on stderr."""
    try:
        yield
    except LockBusy as exc:
        print(f"{exc}.lock busy: another {exc} run owns it", file=sys.stderr)
        raise SystemExit(EXIT_LOCK_BUSY) from None
    except ValueError as exc:
        print(f"{command} refused: {' '.join(str(exc).split())}", file=sys.stderr)
        raise SystemExit(EXIT_REFUSED) from None
