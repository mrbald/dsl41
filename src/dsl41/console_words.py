"""The event console's words and the completion of a half-typed line (runner-design §11,
DL-516).

One table names every verb the console takes, the jobs-table key that sends it, whether that
key asks first, and one line of meaning. A second names every status `CHANGE_STATUS` takes.
The help renders both, and the completion tries them in their order. The module imports no UI
toolkit, so the rule is tested without one.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import NamedTuple, get_args

from dsl41.oracle_state import JobStatus


class VerbRow(NamedTuple):
    """One verb of the console: the jobs-table key that sends it (None for none), whether that
    key asks first, what it does, and the words after it when they are not one job."""

    verb: str
    key: str | None
    asks: bool
    meaning: str
    args: str = ""


class StatusRow(NamedTuple):
    """One status `CHANGE_STATUS` takes and what setting it does."""

    status: str
    meaning: str


#: every verb `:` takes, in the order the help lists them and the completion tries them (DL-516)
VERB_ROWS: tuple[VerbRow, ...] = (
    VerbRow(
        "STARTJOB",
        "s",
        False,
        "start the job if its conditions hold; if held or its condition is false, a dated job arms",
    ),
    VerbRow("FORCE_STARTJOB", "f", True, "start the job despite its conditions, hold or ice"),
    VerbRow("KILLJOB", "k", True, "kill the job's live or queued run; it ends TERMINATED"),
    VerbRow(
        "CHANGE_STATUS",
        None,
        False,
        "set the job's status; starts nothing itself",
        "[job] STATUS [exit]",
    ),
    VerbRow("ON_HOLD", "h", False, "hold the job; it does not start and its dependents wait"),
    VerbRow(
        "OFF_HOLD",
        "H",
        False,
        "release the hold; it starts if its conditions hold or it has none; dated: only if armed",
    ),
    VerbRow("ON_ICE", "i", False, "skip the job; s(), d(), n() on it count as met"),
    VerbRow("OFF_ICE", "I", False, "take the job off ice; it waits for its conditions to recur"),
    VerbRow(
        "ON_NOEXEC",
        "n",
        False,
        "the next start ends SUCCESS, no command; a FAILURE or TERMINATED job goes INACTIVE now",
    ),
    VerbRow("OFF_NOEXEC", "N", False, "undo ON_NOEXEC; the next start runs the command"),
    VerbRow(
        "SET_GLOBAL",
        None,
        False,
        "set a global variable; conditions that read it re-check",
        "NAME=value",
    ),
    VerbRow("DISARM", None, False, "clear the job's armed latch (flag A); nothing else"),
    VerbRow("RELEASE_RESOURCE", None, False, "free the resource units a job holds after its run"),
)

#: every status `CHANGE_STATUS` takes, in the order the help lists them and the completion tries
#: them (DL-516)
STATUS_ROWS: tuple[StatusRow, ...] = (
    StatusRow("SUCCESS", "counts as succeeded; a live run stops; dependents may start"),
    StatusRow("FAILURE", "counts as failed; a live run stops"),
    StatusRow("TERMINATED", "counts as killed; a live run stops"),
    StatusRow(
        "INACTIVE",
        "counts as not run, a box's members too; a live run goes on and its exit is not recorded",
    ),
    StatusRow("RUNNING", "counts as running; no command is started"),
    StatusRow("STARTING", "counts as starting; no command is started"),
)

_VERBS = tuple(row.verb for row in VERB_ROWS)
#: the verbs the first word completes to: never one whose key asks first, since a typed verb is
#: sent without a question only because typing it in full is the intent (DL-187, DL-516)
_SUGGESTED_VERBS = tuple(row.verb for row in VERB_ROWS if not row.asks)
#: the verbs whose one argument is a job
_JOB_VERBS = frozenset(_VERBS) - {"CHANGE_STATUS", "SET_GLOBAL"}
_STATUSES = tuple(row.status for row in STATUS_ROWS)
#: every status the console's parser reads as a status, QUE_WAIT included
_ANY_STATUS: frozenset[str] = frozenset(get_args(JobStatus))


def _first(candidates: Iterable[str], word: str, *, fold: bool) -> str | None:
    """The first candidate that starts with `word`, without regard to case when `fold`."""
    probe = word.upper() if fold else word
    for candidate in candidates:
        if (candidate.upper() if fold else candidate).startswith(probe):
            return candidate
    return None


def complete_line(text: str, jobs: Iterable[str]) -> str | None:
    """The console line with its last word completed, or None (DL-516).

    The first word completes to a verb, upper-cased, but never to one whose key asks first. After
    `CHANGE_STATUS` a word completes to a status, else to a job name; after a job name, to a
    status. After another job verb, `KILLJOB` and `FORCE_STARTJOB` included, a word completes to
    a job name. Verbs and statuses match without regard to case, job names
    exactly. An empty word, `SET_GLOBAL`, a word past the verb's last argument and a word that
    starts nothing give None, and so does a word that is already whole."""
    word = "" if not text or text[-1].isspace() else text.split()[-1]
    if not word:
        return None
    start = len(text) - len(word)
    before = text[:start].split()
    names = sorted(jobs)
    found: str | None = None
    if not before:
        found = _first(_SUGGESTED_VERBS, word, fold=True)
    elif before[0].upper() == "CHANGE_STATUS":
        if len(before) == 1:
            found = _first(_STATUSES, word, fold=True) or _first(names, word, fold=False)
        elif len(before) == 2 and (before[1] in names or before[1].upper() not in _ANY_STATUS):
            # the second word names a job: one the status answer names, or a word that is no
            # status (DL-516)
            found = _first(_STATUSES, word, fold=True)
    elif before[0].upper() in _JOB_VERBS and len(before) == 1:
        found = _first(names, word, fold=False)
    if found is None or len(found) == len(word):
        return None
    return text[:start] + found
