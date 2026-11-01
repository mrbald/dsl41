# CLI and TUI qualification

For maintainers checking which CLI and TUI claims have executable evidence.
Follow the maintainer track under [Documents](../README.md#documents).
This inventory distinguishes source tests from installed-command and display checks.
Run the general gates in [agent-workflow.md](agent-workflow.md#verify-a-change).
Engine operation belongs to [deploy.md](deploy.md).
A listed test is evidence for its assertions, not for every option or deployment.
The control and recovery rules are in `control-protocol.md` §3,
`runner-design.md` §11, DL-391, and `access-model.md` §§0, 3 and 5
for the actor rules and §9 for the served TUI.

## CLI inventory

The command registry is `src/dsl41/cli.py`.
These source tests invoke Typer or the command implementation.
They do not establish that a separately installed wheel works.

| Public command | Source checks |
| --- | --- |
| `lint` | `tests/test_lint.py`, `tests/test_main_config.py` |
| `viz` | `tests/test_viz.py`, `tests/test_viz_html.py`, `tests/test_viz_explore.py`; actual explore-page controls in `tests/test_viz_explore_browser.py` |
| `equiv` | `tests/test_equiv.py` |
| `decompile`, `folds` | `tests/test_dsl.py` |
| `build` | `tests/test_build_cli.py` |
| `nav` | `tests/test_nav.py`: relations, depth, paths, input forms, refusals and subprocess navigation |
| `minify` | `tests/test_minify.py` |
| `resolve` | `tests/test_placeholders.py` |
| `run` | `tests/test_cli_run.py`, `tests/test_runner_journal.py`, `tests/test_main_config_restart.py` |
| `rehearse` | `tests/test_semantics.py`, `tests/test_rehearse_check.py`, `tests/test_main_config.py` |
| `journal` | `tests/test_runner_journal.py` |
| `runs` | `tests/test_run_history.py` |
| `query` | `tests/test_runner_control.py`, `tests/test_cli_control_branches.py`, `tests/test_subscriber_bound.py` |
| `sendevent` | `tests/test_runner_control.py`, `tests/test_cli_control_branches.py`; TUI recovery reconstruction in `tests/test_tui_recovery_cli.py` |
| `off-hold-all` | `tests/test_runner_control.py`, `tests/test_cli_control_branches.py` |
| `ui`, `serve` | `tests/test_runner_serve.py`; PTY and served sessions in `tests/test_tui_recovery_drivers.py` |
| `reread`, `update` | `tests/test_reload_update.py`, `tests/test_main_config_update.py`: plan output, consent, stale plans, application and interrupted update |
| `engine stop` | `tests/test_cli_control_branches.py`: stop request, refusal, unreachable engine and `--wait` through socket removal and lock release |
| `--manifest`, `--environment` | `tests/test_estate_manifest.py`; navigation forms in `tests/test_nav.py` |

The authority for `report`, `uc`, `host`, `supervise`, `seal`, `audit`, `verify`,
`estate prune/reclaim/quiesce/build/diff` and
`release prepare/requalify/apply/resume/rollback/status` is [DL-424](decision-log.md).
They are outside the current registry.
The matching test-file references resolve through that entry:
tests/test_backend_uc.py, tests/test_runner_supervisor.py,
tests/test_estate.py, tests/test_estate_wide.py, tests/test_retention.py,
tests/test_estate_quiesce.py, tests/test_estate_build.py, tests/test_nightbank_boundary.py,
tests/test_release_prepare.py, tests/test_release_apply.py,
tests/test_release_apply_holds.py, tests/test_release_apply_answers.py,
tests/test_release_apply_seal.py, tests/test_release_apply_horizon.py,
tests/test_release_rollback.py, tests/test_release_rollback_takeover.py,
tests/test_release_rollback_holds.py and tests/test_release_status.py.
The tests/test_journal_replay.py reference resolves to `tests/test_runner_journal.py`;
tests/test_nightbank_deploy.py resolves through DL-424's exclusion of the
deployment example and drills (DL-218, DL-219).
The current operator procedure is [deploy.md](deploy.md); it is not a replacement test.
These pointers are not evidence of a current test or a passing run.

Release qualification must run the installed executable outside the source checkout.
It must exercise both a core-only install and an install with the `ui` extra.
A source import and `python -m dsl41` are different evidence.

Three test files run an installed executable.
`tests/test_installed_cli.py` runs the executable named by `DSL41_INSTALLED_EXECUTABLE`.
It contains help, usage-refusal, mutation/no-resend and socket-permission checks.
Its help parameters include the excluded `host` command and omit `nav`, `reread`,
`update` and `engine stop`. That is an evidence gap; the file does not establish
current public-help qualification until its parameters match the registry.
`tests/test_installed_tui_recovery.py` installs the wheel named by `DSL41_RELEASE_WHEEL` offline, under the interpreter named by `DSL41_RECOVERY_PYTHON`, with the UI dependency wheels from `DSL41_RECOVERY_WHEELHOUSE` (default: the wheel's directory).
It runs a complete recovery command from that install (DL-393).
`tests/test_tui_recovery_drivers.py` runs the executable named by `DSL41_TUI_EXECUTABLE` or `DSL41_INSTALLED_EXECUTABLE`, and `python -m dsl41` when neither is set.
The first two skip when their variables are unset.

## TUI interaction inventory

`tests/test_runner_tui.py` drives the actual app with Textual Pilot.
Most interactions use a real control socket and synthetic estate.
The baseline trace tests deliberately script response headers.
They prove client cursor rules; they do not exercise a configuration update.
The epoch and live-seal evidence references resolve to DL-424.

| Action or boundary | Observable check |
| --- | --- |
| Table focus, row selection, `s` | `test_pilot_s_key_startjob_lands_success_in_the_table` |
| `i/I`, `h/H`, `n/N`, typed job verbs | Parser checks enumerate `JOB_EVENT_VERBS`; keyboard focus is checked by `test_the_operator_verbs_live_on_the_jobs_table_not_on_the_app`; real `h` targets only the filtered selected job in both display lanes |
| `f`, `k`, box members, frozen revision | `test_pilot_force_on_a_box_names_the_members_it_reaches`, `test_pilot_the_confirmed_kill_names_the_revision_the_dialog_showed` |
| Confirm Enter/`y`, Escape cancel, typed destructive command | `test_pilot_k_confirms_first_escape_cancels_and_enter_sends`, `test_pilot_a_typed_killjob_sends_without_a_confirm` |
| `:` console, `SET_GLOBAL`, `CHANGE_STATUS`, exit code | Parser/echo tests and `test_pilot_explain_pane_atoms_and_console_change_status_echo` |
| Recovery before write, UNKNOWN, no automatic resend | `test_pilot_displays_recovery_before_send_keeps_it_with_unknown_and_never_resends` in `test_tui_recovery_cli.py`; reconstruction includes every job verb, global value, status, exit code and actor |
| `/`, Enter keep, Escape clear, `v`, space, `z` | `test_pilot_slash_filter_narrows_flat_enter_keeps_escape_clears`, `test_pilot_v_cycles_all_problems_active_and_back_to_all`, `test_pilot_box_tree_order_indent_fold_and_fold_all` |
| `d`/Enter details, Escape/`q` close | `test_pilot_d_key_opens_the_spec_popup_and_escape_closes_it`, `test_pilot_q_closes_the_spec_popup` |
| `t` triggers, countdown, refresh, selection | `test_pilot_t_opens_the_live_triggers_view_and_operator_verbs_do_not_fire`, `test_pilot_triggers_refresh_preserves_the_cursor_across_ticks` |
| F1/`?` help | `test_pilot_f1_and_question_mark_toggle_the_help_panel`, `test_pilot_f1_reaches_the_maximized_pager_and_the_triggers_screen` |
| `m`, `[]`, `{}`, terminal/viewport resize | `test_pilot_pane_geometry_maximize_toggle_and_share_nudges`; actual size changes in both display lanes |
| Log follow, scroll, `/`/`?`, `n/N`, `&`, `F`, `o` | Pager motion/search/filter/follow tests, `test_pilot_log_tail_shows_appended_bytes_for_the_selected_job`, log fault tests |
| Refused queries, disconnect, reconnect | Query fault tests; `test_pilot_reconnect_keeps_the_last_table_and_never_replays_a_mutation` uses socket close/rebind |
| Changed baseline, mixed read headers, reconnect with the same baseline | `test_dl210_u1_u4_trace_baseline_change_restarts_a_longer_trace_next_poll`, `test_dl210_u3_mixed_read_baselines_return_without_updating_the_table`, `test_dl210_u5_reconnect_same_baseline_keeps_cursor_across_a_resume`; both display lanes also observe an engine stop and restart with the same baseline |
| `q`/Ctrl+Q stop versus detach | `test_pilot_quit_confirms_when_the_app_owns_the_run`, `test_pilot_quit_detaches_at_once_when_it_does_not_own_the_run`; real detached viewers exit without stopping their engine |

The PTY lane uses a child terminal process.
The browser lane starts the real `serve` command and a per-session `ui` process.
Chromium, WebKit and Firefox receive actual keyboard input and a viewport resize.
The lanes assert the selected job's resulting state through the control socket.
They also require the confirmation/cancellation and recovery text on the terminal stream.
The engine receives SIGTERM, exits with code 0, and restarts in the same root.
The lanes assert that the baseline is unchanged and the selected child's hold survives.
The attached viewer must display the FAILURE of another job force-started after restart.
The live-seal, run exit-3 and successor-period references resolve to DL-424.
`run --resume` is accepted and ignored; an existing store resumes automatically.
They retain raw terminal output, browser errors and the serve log in
pytest's temporary directory.
Missing Playwright or browser binaries fail an explicitly enabled browser lane.
The default suite skips that lane with a stated reason.

1. Run the source app and recovery checks.

   ```sh
   uv run pytest -q tests/test_runner_tui.py tests/test_tui_recovery_cli.py
   ```

2. Run the real terminal lane.

   ```sh
   uv run pytest -q tests/test_tui_recovery_drivers.py -k real_terminal
   ```

3. Install the three browser engines for the served lane.

   ```sh
   uv run playwright install chromium webkit firefox
   ```

4. Collect the display checks to inspect the selected cases. Collection lists the
   three browser cases even without the opt-in; it does not prove they will run.

   ```sh
   DSL41_BROWSER_TESTS=1 uv run pytest -q --collect-only tests/test_tui_recovery_drivers.py
   ```

5. Run the enabled display checks. Require `PASSED` for each of the three
   `served_tui_real_browser` cases: Chromium, WebKit and Firefox.
   A skipped browser case means the lane is unvalidated, even if pytest exits 0.
   Inspect the skip reasons when the opt-in is lost.

   ```sh
   DSL41_BROWSER_TESTS=1 uv run pytest -v -rs tests/test_tui_recovery_drivers.py
   ```

6. Repeat against the installed executable and require the same three browser
   passes. Supply its absolute path; child commands run outside the checkout.

   ```sh
   DSL41_TUI_EXECUTABLE=/absolute/install/bin/dsl41 DSL41_BROWSER_TESTS=1 \
     uv run pytest -v -rs tests/test_tui_recovery_drivers.py
   ```

These display checks are bounded smoke checks.
They do not cover every key in each browser, remote authentication, or production logs.
An installed wheel is covered only when the explicit executable override is used.
`DSL41_INSTALLED_EXECUTABLE` is accepted as the shared installed-command override too.
Run those lanes explicitly before claiming them.

## Recovering a TUI mutation

The events console prints a complete shell-quoted command before attempting the mutation.
It includes the socket, verb, payload, request id, revision, baseline and claimed actor.
The executable names the current backing runtime and the socket path is absolute.
Destructive keyboard actions refuse if their confirmation pins cannot be read.
The app waits for the screen refresh before writing to the control socket.
An unknown answer or a failure after the write starts repeats the command beside UNKNOWN.
The app never retries a mutation on refresh or reconnect.

1. Read the current estate state through the queries in
   [deploy.md](deploy.md).
2. If recovery is needed, copy the complete displayed command to a shell.
   Keep its id, payload, revision, baseline and actor unchanged.

The displayed command supplies the actual socket, payload and pins.
Recovery is limited to the same protocol version and retained request records
under [the lost-answer rules](control-protocol.md#3-mutating-verb-sendevent).
Journal retention defaults to 90 days; pruning also depends on snapshots and open work.
A pruned id can be admitted as new work even when the baseline is unchanged.
Do not resend an expired id to recover an unknown outcome.
A stale or colliding recovery must be read as its reported outcome.
It is not permission to compose a new mutation automatically.

`--claimed-actor` preserves logged metadata.
It does not authenticate or delegate authority.
The socket still checks the caller's OS principal under the current access policy.
An access-controlled retry must use the same authenticated principal.
Changing access posture can change the stamped actor and cause a collision.
