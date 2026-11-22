# Operate an estate

This page helps the person who owns an estate's go-live and daily running write the
site's operating procedure, so that nothing the engine does on a change or a release
comes as a surprise; the mechanisms are in [deploy.md](deploy.md), which also owns
install, start and recovery. The rest of the track is under **Operator** in the
[README's Documents section](../README.md#documents).

## Decide how changes reach the engine

The engine applies what the estate files say. It does not know what a change was
meant to do, who approved it or whether now is a good time. That is the site's change
procedure. Write it down before the first production day, with answers to these
questions.

- **Files.** Where do the estate files live, who may change them, and who reviews a
  change? `reread` and `update` read the launch config's current files and need the
  adm tier ([access model](access-model.md#1-the-model), DL-463).
- **Checks.** What must pass before a change is applied? `lint` and `rehearse` print
  every finding ([start the estate](deploy.md#start-the-estate), DL-483). `reread`
  adds the engine's view: changed jobs, findings and the running jobs an update would
  stop. Decide which findings block (`[lint] refuse`).
- **Timing.** An update stops the running jobs it names and applies all its changes
  together, once every stop is finished (DL-522). While it waits, it launches no job it
  names: added, changed and removed jobs, the boxes it restarts and their members
  (DL-527). Name the windows for an update, and what you do when `reread` exits 1
  (running jobs would stop).
- **New jobs.** Live at once, or held in the JIL with `status: ON_HOLD` and released by
  a named person? A `status:` line seeds the hold, ice or noexec flag when the job's
  row is made: on a root's first start and on a job an update adds. It is never
  applied to a job an update keeps, so a hold an operator released stays released
  (SEM-24, DL-531). To hold or release a job that exists, use `sendevent`
  ([send the intervention](incident-triage.md#send-the-intervention)).
- **State outside the files.** Holds, ice, noexec flags, globals and statuses set by
  hand live in the run root ([the fresh-root list](deploy.md#upgrade-the-engine)).
  An update keeps completed statuses and holds of retained jobs whose runs it does not
  restart (step 5 of the fixed-path variant in
  [Upgrade the engine](deploy.md#upgrade-the-engine)). Decide how the site records
  this state and when it takes [a backup](deploy.md#take-a-backup-for-restore).
- **Going back.** No command applies a previous configuration. Going back means
  restoring the previous files and running `reread` and `update` again. That is an
  update like any other: it stops the running jobs it names (DL-522). It cannot
  un-stop a run (DL-424), undo a restarted box's new run (DL-463) or undo what a job
  did outside the engine.
- **Notice.** Who is told before an update that stops jobs, and after it?

An edit of only the `status:` line of a job that stays in the estate is not a reason to
stop it (DL-533). The update records the new line, stops nothing for it and sets no flag
for it, so a running job runs on. `reread` and `update` show the line
with that note and count such jobs on their own, as `status line only`. A box whose
own edit is its `status:` line and whose members change is the exception: `reread`
counts it as changed (membership) and `update` counts it under `status line only`, and
each command's counts add up to its jobs. The line takes
effect when the job's row is made again: on a fresh root, or when a later update adds
the job back. To hold or release the job now, use `sendevent`. A job inside a box that
the update restarts is still stopped with the box (DL-463)
([reload a changed estate](deploy.md#reload-a-changed-estate)).

## Release a go-live that loads every job on hold

A go-live may load every job with `status: ON_HOLD` and release the holds by hand.
That is a legitimate choice. `OFF_HOLD` re-attempts the job's start through all its
gates (SEM-21), so each kind of job reacts as follows.

- A job with no date conditions and no condition starts at once (SEM-21, DL-531;
  `test_sem21_off_hold_starts_a_job_with_no_condition_and_no_date_at_once`).
- A job with a condition starts if the condition already holds. A scheduled job starts
  through the schedule gate that a tick during the hold armed (SEM-21, DL-54).
- A box with no date conditions and no condition starts at once. Its members with no
  date conditions and no condition start (SEM-10, DL-531). Released alone, a held
  member stays held and keeps the box RUNNING (SEM-21;
  `test_sem21_off_hold_on_a_box_runs_its_members_and_a_held_member_keeps_it_running`).
- A box whose resource is taken waits in QUE_WAIT and starts when the resource frees
  (SEM-21, DL-50; `test_sem21_off_hold_on_a_box_whose_resource_is_taken_queues_it`).
- A job that is FAILURE and on hold carries an interruption: a marked job that an
  update or an engine death stopped, or one at its attempt limit
  ([runner-design](runner-design.md#7-journal-and-recovery-e1-prod-grade) interruption
  rule). `OFF_HOLD` attempts its start, so the job runs again before its recovery is
  done ([authoring](authoring.md#62-write-every-job-to-survive-interruption)).

`dsl41 off-hold-all` sends one `OFF_HOLD` per job on hold at its status read
(DL-423, [README](../README.md#run-an-estate),
[send the intervention](incident-triage.md#send-the-intervention)). Each job then starts
only if its gates admit it. The sweep also releases a held member of a box and every
interrupted job, and it has no exclusion option. First run `dsl41 off-hold-all --dry-run`,
which lists the held jobs and sends nothing. Read `dsl41 query status --brief` for each
name. If any job is FAILURE and on hold, or any box is kept for hand launches, do not
sweep. Release each job with `sendevent OFF_HOLD --job NAME`, the interrupted ones after
their recovery, in the order you name. The sweep goes in name order by character code
(`src/dsl41/cli_control.py`): digits, capitals, `_`, lowercase.
