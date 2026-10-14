"""The oracle's reads of the clock, and its timers (SEM-33 to SEM-35;
DL-246 to DL-260).

`OracleSchedule` adds to `OracleReads` what depends on the instant: the side
of a job's run_window a start falls on (SEM-33, DL-249), a deferred start and
its staleness (DL-246, DL-257), the queued recheck's run days and calendars
(DL-257), the deadlines a tick arms and their alarms (SEM-34, DL-248,
DL-253), term_run_time's timer (dossier ss5), the timer heap's closed set of
payload shapes (PR-09), and the live timers a status query shows.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta, tzinfo
from typing import Final

from dsl41.autocal import CalendarRuleError, CompiledCalendar, compile_calendar, standard_days
from dsl41.canon import CanonError, canonical_bytes
from dsl41.ir import JobIR, MustTime, Time
from dsl41.oracle_reads import OracleReads
from dsl41.oracle_state import LIVE, Event, JobRuntime, OracleError
from dsl41.timezones import (
    must_instant,
    resolve_timezone,
    start_time_instants,
    to_local,
    to_utc,
    wall_window_contains,
    window_span,
    window_spans_near,
)

#: how far ahead `_next_eligible_opening` looks for an eligible day (DL-257)
_OPENING_SCAN_DAYS: Final = 2 * 366

#: `date.weekday()` (Monday 0) -> the JIL days_of_week token
_WEEKDAY_TOKENS: Final = ("mo", "tu", "we", "th", "fr", "sa", "su")

#: SEM-34: how long after a start time's instant an event still names its
#: slot -- the minute the wall-time match used to cover (DL-260)
_SLOT_MINUTE = timedelta(minutes=1)

#: The closed set of deadline checks the oracle arms as TIMER payloads
#: (`payload["check"]`); the fourth shape, the run-window defer, carries
#: `deferred_cause` instead. PR-09 enumerates this set and proves each member
#: canonicalizes; `_schedule_timer` refuses anything outside it.
TIMER_CHECKS: Final[frozenset[str]] = frozenset({"must_start", "must_complete", "term_run_time"})

#: A box member's deferred start also carries its box's name and run number
#: (DL-246): the deferral belongs to that box run, and a box restart, or a
#: rebaseline that moves the member to another box, makes it stale.
DEFERRED_BOX_KEY: Final = "box"
DEFERRED_BOX_RUN_KEY: Final = "box_run"

#: PR-09's fourth armed shape: a run_window-deferred start carries no
#: deadline `check`, it carries the provenance of the start it defers.
#: Named once (DL-209) -- `_schedule_timer` admits it, `_dispatch` replays
#: it, and the coverage register derives the timer domain from the pair.
DEFERRED_TIMER_KEY: Final = "deferred_cause"

#: A deferred start that resumes `queued-recheck`'s scan for an eligible day
#: instead of attempting a start (DL-257) carries this key, valued at the
#: job's run number when the scan stopped at its bound.
DEFERRED_RESCAN_KEY: Final = "rescan_run"


class OracleSchedule(OracleReads):
    """`OracleReads` with the run_window, calendar and deadline reads, and the timers."""

    def pending_timers(self) -> list[tuple[datetime, str, str]]:
        """Read-only snapshot of LIVE pending timers as (due, job, kind),
        due-ordered; kind is the deadline-check name (`must_start`,
        `must_complete`, `term_run_time`) or `run_window` for a SEM-33
        deferred start. Liveness mirrors the fire-time rules, the alarms'
        (`_dispatch_timer_check`) and term_run_time's rows -- a heap entry a
        fire would discard as stale (run_number moved on; a term_run_time run no longer RUNNING; a must_complete deadline
        already met by the run its tick asked for, DL-248) is not pending, it
        is dead weight awaiting its lazy pop. The ss10 status query renders this for
        the ss11 jobs table; display truth must be the dispatch truth --
        which is why the order is `store.timers()`'s and is NOT re-sorted
        here. On a TIE that order carries the ordering token, the firing
        order of equal-time timers (period-model ss3.2); a `sorted()` over
        `(due, job, kind)` reads as a no-op on an already-due-ordered list
        and silently replaced it with job-name order (DL-143)."""
        live: list[tuple[datetime, str, str]] = []
        for due, _, ev in self.store.timers():
            job = ev.payload.get("job")
            if not isinstance(job, str):  # pragma: no cover -- see below
                # Unreachable: every timer the oracle arms names its job (the
                # deadline checks and the run_window deferral both build the
                # payload from a job name), so no heap entry lacks one.
                continue
            check = ev.payload.get("check")
            if check is None:
                # SEM-33 deferred starts stay live UNCONDITIONALLY: unlike the
                # deadline checks, whose run-mismatch staleness is permanent,
                # a deferred STARTJOB is a real start attempt whose outcome
                # depends on fire-time state -- a job RUNNING now may have
                # completed by next_open, and the fire would legally start it
                # again. Filtering on current status would hide a timer that
                # can still act (DL-46 review, finding rejected with reason).
                # Two stalenesses ARE permanent: a box restart (DL-246), and
                # a scan continuation whose job has started since (DL-257).
                if self._deferral_is_stale(ev) is None and not (
                    DEFERRED_RESCAN_KEY in ev.payload
                    and self._rescan_superseded(job, ev.payload[DEFERRED_RESCAN_KEY])
                ):
                    live.append((due, job, "run_window"))
                continue
            rt = self.store.job.get(job)
            if check == "must_complete":
                # DL-248: judged against the tick's run, so a job that never
                # started still has a live completion deadline
                if rt is None or not self._slot_run_completed(rt, ev.payload.get("run")):
                    live.append((due, job, str(check)))
                continue
            if rt is None or ev.payload.get("run") != rt.run_number:
                continue  # stale: a later run superseded this deadline
            if check == "term_run_time" and rt.status != "RUNNING":
                continue  # the run already ended; the check fires as a no-op
            live.append((due, job, str(check)))
        return live

    def _schedule_timer(self, at: datetime, ev: Event) -> None:
        # PR-09: the shapes a timer payload can take are a CLOSED set, because
        # every one must be proven canonicalizable before it may be armed -- a
        # timer that cannot be written would leave the estate unsealable for
        # as long as it stayed armed. A new deadline kind is added to
        # TIMER_CHECKS (and to the PR-09 test that enumerates it) first.
        check = ev.payload.get("check")
        if check is None:
            if DEFERRED_TIMER_KEY not in ev.payload:
                raise OracleError("unregistered timer shape (PR-09)")
        elif check not in TIMER_CHECKS:
            raise OracleError(f"unregistered timer check {check!r} (PR-09)")
        # and the payload itself canonicalizes -- the registry names the
        # shapes, this proves the bytes, and neither vanishes under -O
        try:
            canonical_bytes(ev.payload)
        except CanonError as exc:
            raise OracleError(f"timer payload is not canonicalizable (PR-09): {exc}") from exc
        self.store.enqueue_timer(at, ev)

    def _job_tz(self, job_ir: JobIR) -> tzinfo | None:
        """SEM-35: the zone this job's time attributes are read in, resolved
        through the runner's own ladder (zoneinfo, POSIX fixed offsets, the
        DL-62 unique-city default). A job that declares no `timezone:` reads
        the constructor's `default_tz`, resolved through the same ladder and
        alias table (DL-151/DL-155). None means the engine clock IS the
        comparison basis -- the vendor's scheduler-zone rule (TechDocs
        12.0.01, timezone attribute) with the simulation's frame as the
        scheduler's zone."""
        schedule = job_ir.schedule
        name = schedule.timezone if schedule is not None else None
        if name is None:
            name = self._default_tz
        if name is None:
            return None
        if name not in self._tz_cache:
            resolved = resolve_timezone(name, self._tz_aliases)
            if resolved is None:
                raise OracleError(
                    f"{job_ir.name}: timezone {name!r} is not resolvable (SEM-35: a zoneinfo"
                    " name, a POSIX fixed offset, or a ujo_timezones entry supplied to the"
                    " runner as --timezone-map)"
                )
            self._tz_cache[name] = resolved.tz
        return self._tz_cache[name]

    def _window_side(self, job_ir: JobIR) -> tuple[str, datetime | None]:
        """Which side of the SEM-33 closer-edge rule `now` falls on, with no
        effect: "inside", "defer" (with the next opening on the engine
        clock) or "skip"."""
        schedule = job_ir.schedule
        if schedule is None or schedule.run_window is None:
            return "inside", None
        assert self._now is not None
        tz = self._job_tz(job_ir)
        now_local = to_local(self._now, tz)
        lo, hi = schedule.run_window
        lo_t = _to_time(lo)
        hi_t = _to_time(hi)
        today = now_local.date()
        spans = window_spans_near(today, lo_t, hi_t, tz)
        if spans is not None:
            # DL-249: near a documented DST change the window is a set of
            # concrete intervals whose endpoints follow the vendor's rules.
            if any(opens <= self._now <= closes for opens, closes in spans):
                return "inside", None
            next_open = min(opens for opens, _ in spans if opens > self._now)
            prev_close = max(closes for _, closes in spans if closes < self._now)
        else:
            if wall_window_contains(now_local.time(), lo_t, hi_t):
                return "inside", None
            next_open = to_utc(_next_occurrence(now_local, lo_t), tz)
            prev_close = to_utc(_prev_occurrence(now_local, hi_t), tz)
        # both distances are measured on the ENGINE clock: a DST shift inside
        # the gap makes the two wall-clock distances lie about elapsed time
        to_open = next_open - self._now
        since_close = self._now - prev_close
        if to_open <= since_close:  # [?] midpoint tie -> next opening
            return "defer", next_open
        return "skip", None

    def _waiting_for_window(self, box: str, member_ir: JobIR) -> bool:
        """A run_window member the box start leaves waiting (DL-246): not
        iced or held, not live, not run or resolved this execution."""
        schedule = member_ir.schedule
        if schedule is None or schedule.run_window is None:
            return False
        rt = self._runtime(member_ir.name)
        box_rt = self._runtime(box)
        return not (
            rt.on_ice
            or rt.on_hold
            or rt.status in LIVE | {"QUE_WAIT"}
            or member_ir.name in box_rt.ran_members
            or member_ir.name in box_rt.window_skipped_members
        )

    def _defer_start(
        self,
        job_ir: JobIR,
        next_open: datetime,
        cause: str,
        *,
        rescan_run: int | None = None,
        note: str = "outside run_window; closer to next opening -- STARTJOB queued",
    ) -> None:
        """Queue the SEM-33 deferred STARTJOB at the next opening. A member's
        deferral carries its box's run number, so a box restart makes it
        stale (DL-246)."""
        payload: dict[str, object] = {"job": job_ir.name, DEFERRED_TIMER_KEY: cause}
        if rescan_run is not None:
            payload[DEFERRED_RESCAN_KEY] = rescan_run
        box = job_ir.box.box_name
        if box is not None:
            payload[DEFERRED_BOX_KEY] = box
            payload[DEFERRED_BOX_RUN_KEY] = self._runtime(box).run_number
        # DL-54 review MINOR: an armed job can reach this branch on every
        # condition edge -- one pending defer per (job, opening) instant, not
        # one per attempt (duplicate timers spammed pending_timers()). Only a
        # deferred start of the same box run counts: a deadline timer at the
        # same instant is not one (DL-246).
        pending = any(
            due == next_open
            and e.kind == "TIMER"
            and DEFERRED_TIMER_KEY in e.payload
            and e.payload.get("job") == job_ir.name
            and e.payload.get(DEFERRED_BOX_KEY) == payload.get(DEFERRED_BOX_KEY)
            and e.payload.get(DEFERRED_BOX_RUN_KEY) == payload.get(DEFERRED_BOX_RUN_KEY)
            and e.payload.get(DEFERRED_RESCAN_KEY) == payload.get(DEFERRED_RESCAN_KEY)
            for due, _, e in self.store.timers()
        )
        if pending:
            return
        self._schedule_timer(next_open, Event(at=next_open, kind="TIMER", payload=payload))
        self._record(job_ir.name, "RUN_WINDOW_DEFER", f"{note} ({cause})")

    def _deferral_is_stale(self, ev: Event) -> str | None:
        """The refusal reason for a deferred start that no longer belongs to
        its job's box run (DL-246), None otherwise: the box has started
        again since the deferral, or a rebaseline moved the job into another
        box or out of one. The current box run decides the job afresh."""
        job = ev.payload.get("job")
        if not isinstance(job, str):  # pragma: no cover -- see below
            # Unreachable: both callers hold a payload job that is already a str.
            # `pending_timers` filters on it before it asks, and no armed timer
            # lacks one (see there); the dispatch reads the event through
            # `_required_job` first.
            return None
        job_ir = self.catalog.jobs.get(job)
        current = job_ir.box.box_name if job_ir is not None else None
        origin = ev.payload.get(DEFERRED_BOX_KEY)
        if origin != current:
            return (
                f"deferred start was queued in box {origin!r}, and the job is now in box"
                f" {current!r} -- no effect (SEM-33, DL-246)"
            )
        box_run = ev.payload.get(DEFERRED_BOX_RUN_KEY)
        if current is None or self._runtime(current).run_number == box_run:
            return None
        return (
            f"deferred start belongs to box {current!r} run {box_run}, and the box has started"
            " again since -- no effect (SEM-33, DL-246)"
        )

    def _rescan_superseded(self, job: str, run: object) -> bool:
        """Whether a scan continuation armed at the job's run number `run` is
        permanently dead: the job has started since, or the catalog no longer
        defines it (DL-257, DL-340). One test for `pending_timers` and
        the firing, so the two cannot disagree. A continuation at the same
        run number stays live whatever the status, which can return to
        INACTIVE before it fires."""
        return job not in self.catalog.jobs or self._runtime(job).run_number != run

    def _defer_to_next_opening(self, job_ir: JobIR, cause: str) -> None:
        """Defer a tickless job to the run_window opening of its next eligible
        day: the first day after today that is not excluded and is a run day
        by run_calendar or days_of_week (DL-257). The scan covers
        `_OPENING_SCAN_DAYS` days; when none is eligible, one continuation
        timer is armed at local midnight of the last scanned day, and the
        scan resumes there, so a rare eligible day (a 29 February) is still
        found. A job with no run_window has no opening and is not deferred."""
        schedule = job_ir.schedule
        if schedule is None or schedule.run_window is None:
            return
        assert self._now is not None
        tz = self._job_tz(job_ir)
        today = to_local(self._now, tz).date()
        first, last = today + timedelta(days=1), today + timedelta(days=_OPENING_SCAN_DAYS)
        excluded = self._calendar_days(job_ir, schedule.exclude_calendar, first, last)
        runs = self._calendar_days(job_ir, schedule.run_calendar, first, last)
        lo, hi = (_to_time(t) for t in schedule.run_window)
        day = first
        while day <= last:
            eligible = (excluded is None or day not in excluded) and (
                day in runs if runs is not None else self._is_run_day(job_ir, day)
            )
            if eligible:
                self._defer_start(job_ir, _window_span(day, lo, hi, tz)[0], cause)
                return
            day += timedelta(days=1)
        self._defer_start(
            job_ir,
            to_utc(datetime.combine(last, dtime()), tz),
            cause,
            rescan_run=self._runtime(job_ir.name).run_number,
            note=f"no eligible day through {last}; the scan for one resumes then",
        )

    def _calendar_days(
        self, job_ir: JobIR, name: str | None, first: date, last: date
    ) -> frozenset[date] | None:
        """The days of calendar `name` within [first, last], generated once
        for the range (DL-257); None when there is no calendar."""
        if name is None:
            return None
        try:
            days = self._calendar(job_ir, name)
            if isinstance(days, CompiledCalendar):
                return days.days_between(first, last)
        except CalendarRuleError as exc:
            raise OracleError(f"{job_ir.name}: {exc}") from exc
        return frozenset(d for d in days if first <= d <= last)

    def _queued_recheck(self, job_ir: JobIR) -> tuple[str, str] | None:
        """Why a job leaving QUE_WAIT must not start, as (check, reason), or
        None (DL-257). `check` is "day", "window" or "condition". The
        `queued-recheck` switch is the vendor's EvaluateQueuedJobStarts: 0
        re-checks nothing; 1 re-checks the starting conditions "other than
        the date condition check for the day of evaluation" -- run_calendar,
        days_of_week, start_times and start_mins are not re-read, but a day
        in the exclusion calendar and a time outside the run window still
        stop the start; 2 also checks that today is a run day. The day is
        the job's local day, in the zone its run_window is read in."""
        mode = self.semantics.queued_recheck
        if mode == "0":
            return None
        schedule = job_ir.schedule
        if schedule is not None:
            assert self._now is not None
            today = to_local(self._now, self._job_tz(job_ir)).date()
            excluded = schedule.exclude_calendar
            if excluded is not None and self._calendar_has(job_ir, excluded, today):
                return "day", f"{today} is in exclude_calendar {excluded!r}"
            if mode == "2" and not self._is_run_day(job_ir, today):
                return "day", f"{today} is not a run day"
            if self._window_side(job_ir)[0] != "inside":
                return "window", "outside run_window"
        gate = job_ir.sem.condition
        if gate is not None and not self._cond_true(gate.cond, job_ir.name):
            return "condition", "condition false"
        return None

    def _is_run_day(self, job_ir: JobIR, day: date) -> bool:
        """`queued-recheck=2`'s date check for one day: run_calendar XOR
        days_of_week (SEM-31), with an absent days_of_week read as every
        day, as the scheduler reads it. The exclusion is checked apart."""
        schedule = job_ir.schedule
        assert schedule is not None
        if schedule.run_calendar is not None:
            return self._calendar_has(job_ir, schedule.run_calendar, day)
        days = schedule.days_of_week
        return days is None or "all" in days or _WEEKDAY_TOKENS[day.weekday()] in days

    def _calendar_has(self, job_ir: JobIR, name: str, day: date) -> bool:
        """Whether calendar `name` holds `day`. The scheduler owns ticks; this
        is the one calendar question the oracle asks, so a replay with no
        scheduler reads the same days (DL-257). Preflight refuses a missing
        or uninterpretable calendar before a run; a direct caller that
        skipped it gets an OracleError, never a guess."""
        try:
            days = self._calendar(job_ir, name)
            if isinstance(days, CompiledCalendar):
                return day in days.days_between(day, day)
            return day in days
        except CalendarRuleError as exc:
            raise OracleError(f"{job_ir.name}: {exc}") from exc

    def _calendar(self, job_ir: JobIR, name: str) -> frozenset[date] | CompiledCalendar:
        """Calendar `name`'s days, resolved once: a standard calendar's day
        set, or a compiled extended one. Raises CalendarRuleError on a rule
        it cannot interpret, OracleError on a missing definition."""
        days = self._calendars.get(name)
        if days is None:
            cal = self.catalog.calendars.get(name)
            if cal is None:
                raise OracleError(
                    f"{job_ir.name}: calendar {name!r} has no definition in the loaded set"
                )
            days = (
                compile_calendar(cal, self.catalog, self.semantics)
                if cal.kind == "extended"
                else standard_days(cal)
            )
            self._calendars[name] = days
        return days

    @staticmethod
    def _has_ticks(job_ir: JobIR) -> bool:
        """Whether the scheduler ticks this job: start_times, start_mins, or a
        run_calendar's own row times (E11, DL-58)."""
        schedule = job_ir.schedule
        return schedule is not None and bool(
            schedule.start_times or schedule.start_mins or schedule.run_calendar
        )

    def _start_slot(self, job_ir: JobIR) -> int | None:
        """SEM-34: which `start_times` entry the current instant is. None
        when the job declares none, or when the instant is not one of them
        -- an operator's sendevent, say. A start_mins job carries one
        broadcast offset (DL-248), so it never needs a slot.

        The slot is named by instant, not by wall time (DL-260). The
        scheduler and this method convert the start times of the instant's
        local day, in the job's zone, through one definition,
        `timezones.start_time_instants`, under the same `dst-start-times`
        value. So a tick names its own slot on a DST change day too: under
        `vendor`, a 02:45 start runs at 03:00:45 and a 03:45 start at
        03:45, each naming its own. An instant names the latest start time
        at or before it, less than a minute earlier, so an event inside a
        start time's minute still names it, as the wall-time match did.
        Under `fold0` a missing-hour start can share an instant with a later
        start time; the scheduler then ticks once, and the tick names the
        earlier wall time."""
        schedule = job_ir.schedule
        if schedule is None or not schedule.start_times:  # pragma: no cover -- see below
            # Unreachable from JIL: both callers need a start-times schedule. A
            # multi-offset relative must time and any absolute one are refused at
            # lowering unless start_times exist (SEM-34, DL-248, DL-253); a single
            # relative offset broadcasts and never asks for a slot (`_sla_offset`);
            # `_slot_deadline` returns first for a job with no schedule.
            return None
        assert self._now is not None
        tz = self._job_tz(job_ir)
        day = to_local(self._now, tz).date()
        times = [(start.hour, start.minute) for start in schedule.start_times]
        best: tuple[datetime, int] | None = None
        for index, at in start_time_instants(day, times, tz, dst=self.semantics.dst_start_times):
            if at <= self._now < at + _SLOT_MINUTE and (best is None or at > best[0]):
                best = (at, index)
        return None if best is None else best[1]

    def _sla_offset(self, job_ir: JobIR, offsets: list[int]) -> int:
        """SEM-34: "+n minutes from each start time" under the strict count
        match -- N offsets against N start_times pair BY POSITION, so the
        second tick gets the second offset.

        Two cases keep the first offset. A SINGLE offset broadcasts over
        every start time: the vendor's relative syntax is one `+minutes`
        "after each start time" (DL-248). An instant that matches no start time cannot be paired at
        all, and the first offset is what the oracle used before the pairing
        existed."""
        if len(offsets) == 1:
            return offsets[0]
        slot = self._start_slot(job_ir)
        return offsets[0] if slot is None else offsets[slot]

    def _slot_deadline(self, job: str, check: str) -> Event | None:
        """SEM-34: the deadline of the slot this tick names, or None. The
        payload carries the run_number AT THE TICK: the run the slot asks for
        is the first one to begin after it (DL-248). A relative form is due
        at tick+offset; an absolute one at `_absolute_deadline`."""
        job_ir = self.catalog.jobs.get(job)
        if job_ir is None or job_ir.schedule is None:
            return None
        schedule = job_ir.schedule
        spec = schedule.must_start if check == "must_start" else schedule.must_complete
        if spec is None:
            return None
        assert self._now is not None
        deadline: datetime | None
        if spec.kind == "relative":
            if not spec.offsets_min:  # pragma: no cover -- see below
                # Unreachable from JIL: lowering refuses an empty must_start_times or
                # must_complete_times value ("empty value"), and SlaSpec requires
                # offsets_min for a relative spec. Only an IR edited after lowering holds
                # []; it would arm no deadline, which is what this returns.
                return None
            deadline = self._now + timedelta(minutes=self._sla_offset(job_ir, spec.offsets_min))
        else:
            deadline = self._absolute_deadline(job_ir, spec.times or [])
        if deadline is None:
            return None
        payload = {"check": check, "job": job, "run": self._runtime(job).run_number}
        return Event(at=deadline, kind="TIMER", payload=payload)

    def _absolute_deadline(self, job_ir: JobIR, times: list[MustTime]) -> datetime | None:
        """SEM-34 (DL-253): the absolute must time of the slot this tick
        names, on the tick's local calendar day in the job's zone, plus a
        day for each 24 hours past 23 ("Limits: 00:00-71:59 (2 calendar days
        ahead of the current calendar day)"). None when the tick names no
        slot: the times pair with start_times by position, so an instant
        that is no start time has none.

        A deadline that falls before the tick is due at the tick. Only one
        case reaches that, because lowering refuses a must time below its
        own start time (SEM-34): under `dst-start-times=fold0` a start in a
        spring change's missing hour ticks past the gap, later than the
        vendor's first minute of the next hour, where its must time may lie
        (DL-260)."""
        slot = self._start_slot(job_ir)
        schedule = job_ir.schedule
        if slot is None or schedule is None or not schedule.start_times or slot >= len(times):
            return None
        assert self._now is not None
        tz = self._job_tz(job_ir)
        day = to_local(self._now, tz).date()
        due = _must_instant(day, schedule.start_times[slot], times[slot], tz)
        return max(due, self._now)

    def _pending_check(self, job: str, check: str, rt: JobRuntime) -> bool:
        """Is a deadline of this kind still pending for `job`: armed, not
        fired, and not yet met (DL-248, DL-253)? A must_start deadline is met
        once a run began after its tick; a must_complete one once that run
        completed. The test reads the timer heap, so no new state is kept."""
        for _, _, armed in self.store.timers():
            payload = armed.payload
            if payload.get("check") != check or payload.get("job") != job:
                continue
            tick_run = payload.get("run")
            if check == "must_start" and tick_run == rt.run_number:
                return True
            if check == "must_complete" and not self._slot_run_completed(rt, tick_run):
                return True
        return False

    def _tick_deadline(self, job: str, check: str) -> Event | None:
        """SEM-34: the must_start or must_complete deadline this tick arms,
        or None. MUST_START_ALARM fires if no new run has begun by it;
        MUST_COMPLETE_ALARM if the run this tick asks for has not completed
        by it, whenever that run started (DL-248).

        At most one deadline of each kind is pending per job (DL-248,
        DL-253). The vendor inserts the next CHK_START and CHK_COMPLETE only
        "after the job completes", so a tick that finds the job live, or
        finds an earlier tick's deadline of the same kind still pending,
        arms nothing."""
        timer = self._slot_deadline(job, check)
        if timer is None:
            return None
        rt = self._runtime(job)
        if rt.status in ("STARTING", "RUNNING", "QUE_WAIT") or self._pending_check(job, check, rt):
            return None
        return timer

    @staticmethod
    def _slot_run_completed(rt: JobRuntime, tick_run: object) -> bool:
        """SEM-34 (DL-248): has the run a tick asked for completed? That run
        is the first to begin after the tick, number tick_run+1. It has
        completed once it is no longer STARTING or RUNNING, or once a later
        run began -- runs of one job never overlap."""
        if not isinstance(tick_run, int):
            return False
        if rt.run_number > tick_run + 1:
            return True
        return rt.run_number == tick_run + 1 and rt.status not in LIVE

    def _arm_term_run_time(self, job_ir: JobIR) -> None:
        assert self._now is not None
        run_number = self._runtime(job_ir.name).run_number + 1  # the run being started
        # zero means no limit (vendor default; DL-241); arm no timer for it
        if job_ir.sem.term_run_time_min is not None and job_ir.sem.term_run_time_min != 0:
            deadline = self._now + timedelta(minutes=job_ir.sem.term_run_time_min)
            self._schedule_timer(
                deadline,
                Event(
                    at=deadline,
                    kind="TIMER",
                    payload={"check": "term_run_time", "job": job_ir.name, "run": run_number},
                ),
            )

    def _lazy_clock_checks(self) -> None:
        """Deadline timers fire through the timer heap inside feed(); nothing
        else is time-lazy v1."""

    def _dispatch_timer_check(self, ev: Event) -> None:
        """A deadline alarm (SEM-34): it moves no state. term_run_time is a
        move, the job chart's (`Oracle._handle_timer`), and never reaches
        here; any other check is a no-op."""
        check = ev.payload.get("check")
        job = self._required_job(ev)
        rt = self._runtime(job)
        if check == "must_start":
            # inverted run check: alarm iff NO new run began since the tick
            if ev.payload.get("run") == rt.run_number:
                self._emit("MUST_START_ALARM", job=job)
                self._record(job, "MUST_START_ALARM", "must_start_times deadline (SEM-34)")
            return
        if check == "must_complete":
            # SEM-34 (DL-248): alarm only, no control flow -- iff the run the
            # tick asked for has not completed, including when it never began
            if not self._slot_run_completed(rt, ev.payload.get("run")):
                self._emit("MUST_COMPLETE_ALARM", job=job)
                self._record(job, "MUST_COMPLETE_ALARM", "must_complete_times deadline (SEM-34)")


def _to_time(t: Time) -> dtime:
    return dtime(hour=t.hour, minute=t.minute)


def _must_instant(day: date, start: Time, must: MustTime, tz: tzinfo | None) -> datetime:
    """An absolute must time as an engine instant (SEM-34, DL-253):
    `timezones.must_instant`, which holds the DST rules, so `dsl41 lint`
    reads the same ones."""
    return must_instant(day, (start.hour, start.minute), (must.hour, must.minute), tz)


def _next_occurrence(now: datetime, target: dtime) -> datetime:
    candidate = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def _prev_occurrence(now: datetime, target: dtime) -> datetime:
    candidate = now.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    if candidate > now:
        candidate -= timedelta(days=1)
    return candidate


def _window_span(day: date, lo: dtime, hi: dtime, tz: tzinfo | None) -> tuple[datetime, datetime]:
    """The run_window that opens on local `day`, as two engine instants
    (DL-249): `timezones.window_span`, which holds the DST rules, so
    `dsl41 lint` reads the same ones."""
    return window_span(day, lo, hi, tz)
