#!/usr/bin/env python3
"""Generate the soak estate: 200 synthetic jobs on one machine whose
`max_load` admits 20 runs at once, about 1,000 job starts a day.

    uv run python examples/soak/generate.py [--seed 41] [--out DIR]

The output is deterministic for a seed: no clock and no environment is read,
so the checked-in `estate/soak.jil` regenerates byte for byte. Every name is
invented. Every command is `sleep N`, `true`, `false` or `sleep N; false`.

The shape, all in UTC. Every scheduled start falls between 01:00 and 21:45,
so a seal at midnight finds the estate quiet:

- 16 boxes of 6 members. Two members start with the box, two chains follow,
  and two fan-in members join them. One box carries a member that fails.
  Every box starts at 02:00, so the night's batch asks for more than 20
  slots at once and the machine queues the rest.
- 43 scheduled jobs with 1 to 5 `start_times` a day.
- 30 condition jobs: chains on the scheduled jobs and the boxes, and fan-in
  over two of them with zero lookbacks, so one cycle fires them once.
- 6 window jobs: `start_mins` inside a `run_window`.
- 4 jobs that fail every run, each with a retry job on `f(...)`, and one
  alert job on the failing box.

dsl41 does not model `n_retrys` (DL-53), so a retry is a job of its own.
Machine load is checked only for a positive priority (DL-247), so every
command job sets `priority` and `job_load: 1`.
"""

from __future__ import annotations

import argparse
import random
from dataclasses import dataclass, field
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE / "estate"
SEED = 41
MACHINE = "soakhost"
MAX_LOAD = 20
#: start-count weights: most jobs start five times a day, a few once
STARTS = {1: 1, 2: 1, 3: 1, 4: 2, 5: 40}
#: hours the scheduled work clusters in, as batch estates do
BUSY_HOURS = (1, 2, 3, 4, 18, 19, 20, 21)
QUIET_HOURS = (5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17)
MINUTES = (0, 0, 0, 0, 0, 15, 30, 30, 45)
KICKOFF = 2
#: groups that set the job count; the test pins the total at 200
BOXES, MEMBERS, SCHEDULED, CONDITIONAL, WINDOWED, FAILING = 16, 6, 43, 30, 6, 4


@dataclass
class Job:
    name: str
    job_type: str  # "b" or "c"
    starts: int  # planned starts a day, retries excluded
    box: str | None = None
    command: str | None = None
    condition: str | None = None
    extra: list[str] = field(default_factory=list)

    def jil(self) -> str:
        lines = [f"insert_job: {self.name}", f"job_type: {self.job_type}"]
        if self.box:
            lines.append(f"box_name: {self.box}")
        if self.condition:
            lines.append(f"condition: {self.condition}")
        if self.job_type == "c":
            lines += [
                f"command: {self.command}",
                f"machine: {MACHINE}",
                "job_load: 1",
            ]
        lines += self.extra
        return "\n".join(lines) + "\n"


def _times(rng: random.Random, count: int, *, kickoff: bool = False) -> list[str]:
    """`count` start times in distinct hours, mostly busy ones. A kickoff
    schedule starts at 02:00 first: every box does, so the night's batch
    asks for more than 20 slots at once and the machine queues it."""
    pool = list(BUSY_HOURS) * 2 + list(QUIET_HOURS)
    hours: set[int] = {KICKOFF} if kickoff else set()
    while len(hours) < count:
        hours.add(rng.choice(pool))
    return [
        "02:00" if kickoff and hour == KICKOFF else f"{hour:02d}:{rng.choice(MINUTES):02d}"
        for hour in sorted(hours)
    ]


def _schedule(times: list[str]) -> list[str]:
    return [
        "date_conditions: 1",
        "days_of_week: all",
        f'start_times: "{", ".join(times)}"',
    ]


def _sleep(rng: random.Random, *, long_share: float = 0.05) -> str:
    """Mostly short work; a few long runs keep the machine's 20 slots full."""
    if rng.random() < long_share:
        return f"sleep {rng.randrange(900, 2701, 60)}"
    return f"sleep {rng.choice((1, 5, 10, 30, 60, 120, 180, 300, 420, 600))}"


def _count(rng: random.Random) -> int:
    return rng.choices(list(STARTS), weights=list(STARTS.values()))[0]


def build(seed: int = SEED) -> list[Job]:
    rng = random.Random(seed)
    jobs: list[Job] = []

    def command_job(
        name: str,
        starts: int,
        command: str,
        *,
        box: str | None = None,
        condition: str | None = None,
        extra: list[str] | None = None,
    ) -> Job:
        priority = f"priority: {rng.choice((1, 2, 2, 3))}"
        job = Job(name, "c", starts, box, command, condition, [priority, *(extra or [])])
        jobs.append(job)
        return job

    boxes: list[Job] = []
    for b in range(1, BOXES + 1):
        name = f"SK_BOX{b:02d}_B"
        starts = _count(rng)
        box = Job(name, "b", starts, extra=_schedule(_times(rng, starts, kickoff=True)))
        jobs.append(box)
        boxes.append(box)
        m = [f"SK_BOX{b:02d}_M{i}_C" for i in range(1, MEMBERS + 1)]
        conds = [None, None, f"s({m[0]})", f"s({m[0]}) & s({m[1]})", f"s({m[2]})"]
        conds.append(f"s({m[3]}) & s({m[4]})")
        for i, (member, cond) in enumerate(zip(m, conds, strict=True)):
            # box 16's last member fails every run: the box fails with it
            failing = b == BOXES and i == MEMBERS - 1
            command = "sleep 5; false" if failing else _sleep(rng, long_share=0.02)
            command_job(member, starts, command, box=name, condition=cond)

    scheduled: list[Job] = []
    for s in range(1, SCHEDULED + 1):
        starts = _count(rng)
        scheduled.append(
            command_job(
                f"SK_SCHED{s:02d}_C",
                starts,
                rng.choice((_sleep(rng), _sleep(rng), "true")),
                extra=_schedule(_times(rng, starts)),
            )
        )

    # the failing box never succeeds, so nothing waits on its success
    upstreams = scheduled + boxes[:-1]
    for c in range(1, CONDITIONAL + 1):
        if c % 3 == 0:
            # fan-in: zero lookbacks, so one completion of each fires it once
            a, b = rng.sample(upstreams, 2)
            cond = f"s({a.name},0) & s({b.name},0)"
            starts = min(a.starts, b.starts)
        else:
            up = rng.choice(upstreams)
            cond = f"s({up.name})"
            starts = up.starts
        job = command_job(f"SK_COND{c:02d}_C", starts, _sleep(rng), condition=cond)
        upstreams.append(job)

    for w in range(1, WINDOWED + 1):
        first = rng.choice((7, 9, 13, 15))
        command_job(
            f"SK_WINDOW{w:02d}_C",
            5,
            rng.choice(("true", "sleep 2", "sleep 10")),
            extra=[
                "date_conditions: 1",
                "days_of_week: all",
                f"start_mins: {rng.choice((0, 10, 20, 30))}",
                # a tick before the window runs at its opening, and four
                # ticks fall inside it: five starts a day
                f'run_window: "{first:02d}:00-{first + 3:02d}:30"',
            ],
        )

    for f in range(1, FAILING + 1):
        starts = _count(rng)
        failing = command_job(
            f"SK_FAIL{f:02d}_C", starts, "false", extra=_schedule(_times(rng, starts))
        )
        command_job(f"SK_FAIL{f:02d}_RETRY_C", starts, _sleep(rng), condition=f"f({failing.name})")
    failed_box = boxes[-1]
    command_job("SK_ALERT_C", failed_box.starts, "true", condition=f"f({failed_box.name})")
    return jobs


def render(jobs: list[Job], seed: int) -> str:
    header = (
        f"/* The soak estate: {len(jobs)} synthetic jobs, seed {seed}.\n"
        "   GENERATED by generate.py -- edit the generator, not this file. */\n"
    )
    machine = f"insert_machine: {MACHINE}\ntype: a\nnode_name: localhost\nmax_load: {MAX_LOAD}\n"
    return "\n".join([header, machine, *(job.jil() for job in jobs)])


def planned_starts(jobs: list[Job]) -> int:
    """Job starts a day as planned: boxes and commands, retries included."""
    return sum(job.starts for job in jobs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--out", type=Path, default=OUT, help="Output directory.")
    args = parser.parse_args()
    jobs = build(args.seed)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "soak.jil").write_text(render(jobs, args.seed))
    print(f"soak.jil: {len(jobs)} jobs, {planned_starts(jobs)} planned starts a day")


if __name__ == "__main__":
    main()
