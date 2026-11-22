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
| `query` | `tests/test_runner_control.py`, `tests/test_cli_control_branches.py`, `tests/test_subscriber_bound.py`; focused and neutral status text in `tests/test_cli_output.py` (DL-482) |
| `sendevent` | `tests/test_runner_control.py`, `tests/test_cli_control_branches.py`; TUI recovery reconstruction in `tests/test_tui_recovery_cli.py` |
| `off-hold-all` | `tests/test_runner_control.py`, `tests/test_cli_control_branches.py` |
| `ui`, `serve` | `tests/test_runner_serve.py`; PTY and served sessions in `tests/test_tui_recovery_drivers.py`. The person of `ui` (hidden `--web-principal`) and its exit 2 on a bad person or a denied status: `tests/test_control_as.py` and `tests/test_runner_web.py` (DL-499). The person-aware server is `serve_people` in `src/dsl41/runner_web.py` |
| `reread`, `update` | `tests/test_reload_update.py`, `tests/test_main_config_update.py`: socket/env selection, passive forecast, marker, application and retry (DL-463); `tests/test_cli_output.py`, `tests/test_definition_difference.py`: both layouts, filtered brief rows, shared candidate and engine-state advice, grouped differences and 5,000-job byte bounds (DL-482) |
| `engine stop` | `tests/test_cli_control_branches.py`: stop request, refusal, unreachable engine; `tests/test_cli_stop_process.py`: `--wait` on the exit of the engine's process, named by the peer pid on the connection that carries the stop; with no peer pid the wait uses `leader.lock` (DL-496) |
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
Its help parameters match the command registry (DL-529). No workflow sets the
variable, so the file runs by hand before a tag.
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
| `i/I`, `h/H`, `n/N`, typed job verbs | Parser checks enumerate `JOB_EVENT_VERBS`; keyboard focus is checked by `test_the_operator_verbs_live_on_the_jobs_table_not_on_the_app`; in both display lanes Enter on a filter that shows one job selects it, and real `h` holds only that job |
| The selection is a job, not a row (DL-532): the first answer selects its first row; the operator's own fold selects the box shown in its place, and a box that stands in by any other change is not selected; a filter or view that hides the job leaves nothing selected, the verb keys, `d` and a console verb without a job say "no job selected", and up, down, Enter or a click on any cell of the resting row selects it, scrolled into view; Enter on the filter line, or `v`, that leaves one job shown selects it, and it stays selected when the filter clears, while typing in the filter line selects nothing; the job is selected again when shown; a refresh, an added job or a status answer that leaves one row moves nothing, a removed job leaves nothing selected, and an event a rebuild overtook selects nothing | `test_pilot_dl532_the_first_answer_selects_the_first_row`, `test_pilot_dl532_fold_all_selects_the_enclosing_box_and_h_holds_it`, `test_pilot_dl532_a_nested_fold_selects_the_outermost_folded_box_shown`, `test_pilot_dl532_an_update_that_puts_the_job_under_a_folded_box_selects_nothing`, `test_pilot_dl532_a_filter_that_hides_the_selection_leaves_nothing_selected`, `test_pilot_dl532_a_cleared_filter_selects_the_hidden_job_again`, `test_pilot_dl532_a_click_anywhere_on_the_resting_row_selects_it`, `test_pilot_dl532_a_pick_after_a_scroll_shows_the_row_it_selects`, `test_pilot_dl532_a_filter_typed_down_to_one_job_selects_it_for_h`, `test_pilot_dl532_typing_to_one_job_without_enter_selects_nothing`, `test_pilot_dl532_typing_from_one_match_to_none_and_back_then_enter_selects_it`, `test_pilot_dl532_a_prefix_with_one_match_then_escape_keeps_the_earlier_pick`, `test_pilot_dl532_enter_selects_the_one_row_a_refresh_left_under_the_filter`, `test_pilot_dl532_the_sole_job_a_filter_shows_stays_selected_when_cleared`, `test_pilot_dl532_a_view_change_to_one_job_selects_it`, `test_pilot_dl532_a_refresh_while_the_selection_is_hidden_changes_nothing`, `test_pilot_dl532_a_refresh_that_leaves_one_other_job_selects_nothing`, `test_pilot_dl532_one_answer_that_swaps_the_single_row_selects_nothing`, `test_pilot_dl532_an_update_moves_no_selection`, `test_pilot_dl532_a_rebuild_never_passes_the_selection_through_another_row`, `test_pilot_dl532_a_row_selected_event_a_rebuild_overtook_selects_nothing`, `test_pilot_dl532_a_pick_between_a_rebuild_and_its_events_stands` |
| `f`, `k`, box members, frozen revision | `test_pilot_force_on_a_box_names_the_members_it_reaches`, `test_pilot_the_confirmed_kill_names_the_revision_the_dialog_showed` |
| Confirm Enter/`y`, Escape cancel, typed destructive command | `test_pilot_k_confirms_first_escape_cancels_and_enter_sends`, `test_pilot_a_typed_killjob_sends_without_a_confirm` |
| `:` console, `SET_GLOBAL`, `CHANGE_STATUS`, exit code | Parser/echo tests and `test_pilot_explain_pane_atoms_and_console_change_status_echo` |
| The console's verbs screen on F1, grey suggestion, Right and Tab, no completion of `KILLJOB` or `FORCE_STARTJOB` (DL-516) | `test_pilot_dl516_f1_on_the_console_opens_the_verbs_screen` at 80x24 and 120x40, `test_pilot_dl516_pagedown_scrolls_the_verbs_screen_and_home_returns`, `test_pilot_dl516_closing_the_verbs_screen_keeps_the_console_line`, `test_pilot_dl516_f1_off_the_console_line_toggles_the_help_panel`, `test_pilot_dl516_operator_keys_on_the_verbs_screen_send_nothing`, `test_pilot_dl516_ch_shows_the_suggestion_and_tab_takes_it`, `test_pilot_dl516_right_takes_a_verb_and_a_job_from_the_status_answer`, `test_pilot_dl516_change_status_su_tab_enter_sends_success_for_the_selected_row`, `test_pilot_dl519_tab_completes_after_the_app_loses_and_regains_focus`, `test_pilot_dl519_tab_completes_before_the_suggestion_is_answered`, `test_pilot_dl519_tab_takes_the_grey_job_after_the_job_names_change`, `test_pilot_dl516_tab_never_leaves_the_console_line`, `test_pilot_dl516_a_job_name_typed_after_tab_stays_in_the_console_line`, `test_pilot_dl516_k_tab_enter_sends_nothing`; the tables against the console's verbs and the table's keys in `test_dl516_the_verb_table_names_every_console_verb` and `test_dl516_the_verb_table_keys_and_asks_agree_with_the_jobs_table`; each completion rule in `tests/test_console_words.py`; Tab completing `ch` in the browser lane, `test_full_local_timestamps_in_real_display_lanes` |
| Keys queued behind an opening key fire no verb (DL-518): `:`, `/`, the pager's `/` and `m` move focus first; `k`, `f`, `d` and a quit that asks hold the verb keys until the dialog or popup opens or is refused, and quit is never held back; Tab stays in the filter line | `test_pilot_bug95_a_burst_after_colon_sends_no_verb`, `test_pilot_bug95_a_burst_after_slash_sends_no_verb`, `test_pilot_bug95_a_burst_after_m_sends_no_verb`, `test_pilot_bug95_a_burst_after_the_pager_slash_keeps_focus_on_the_prompt`, `test_pilot_dl518_a_key_queued_behind_k_or_f_sends_nothing_and_the_confirm_opens`, `test_pilot_dl518_a_detached_viewer_quits_on_a_key_queued_behind_k`, `test_pilot_dl518_a_detached_viewer_quits_while_the_engine_does_not_answer`, `test_pilot_dl518_a_key_queued_behind_d_sends_nothing_and_the_details_open`, `test_pilot_dl518_a_key_queued_behind_the_quit_that_asks_sends_nothing`, `test_pilot_dl518_the_verb_keys_work_again_once_the_confirm_is_closed`, `test_pilot_dl518_the_verb_keys_and_d_work_again_after_the_details_popup_closes`, `test_pilot_dl518_the_verb_keys_work_again_after_a_confirmed_send`, `test_pilot_dl518_the_verb_keys_work_again_after_a_cancelled_quit_that_asks`, `test_pilot_dl518_the_verb_keys_work_again_after_a_refused_precondition`, `test_pilot_dl518_tab_in_the_filter_line_stays_in_the_line`; `:ch` typed without a wait in the browser lane, `test_full_local_timestamps_in_real_display_lanes` |
| Recovery before write, UNKNOWN, no automatic resend | `test_pilot_displays_recovery_before_send_keeps_it_with_unknown_and_never_resends` in `test_tui_recovery_cli.py`; reconstruction includes every job verb, global value, status, exit code and actor |
| `/`, Enter keep, Escape clear, `v`, space, `z` | `test_pilot_slash_filter_narrows_flat_enter_keeps_escape_clears`, `test_pilot_filter_not_success_under_a_name_pattern`, `test_pilot_a_refused_draft_keeps_the_filter_in_force`, `test_pilot_enter_stays_on_a_refused_line_and_escape_clears_all`, `test_pilot_a_refused_draft_survives_a_focus_move_and_f1_shows_the_syntax`, `test_pilot_a_filter_with_no_match_leaves_the_verb_keys_nothing_to_send`, `test_pilot_v_cycles_all_problems_active_and_back_to_all`, `test_pilot_box_tree_order_indent_fold_and_fold_all`; the word forms and refusals in `tests/test_job_filter.py` (DL-511) |
| A pattern that takes too long in the jobs filter or the pager (DL-514) | `test_pilot_dl514_a_draft_that_takes_too_long_is_refused`, `test_pilot_dl514_a_filter_stopped_on_a_status_answer_is_cleared_once`, `test_pilot_dl514_a_pager_pattern_that_takes_too_long_is_refused_at_the_prompt`, `test_pilot_dl514_new_lines_that_stop_the_filter_in_force_clear_it`, `test_pilot_dl514_a_stop_on_new_lines_keeps_a_paused_view_and_its_note_leaves_with_the_log`, `test_pilot_dl514_a_search_does_not_run_the_filter_in_force_again`, `test_pilot_a_filter_with_no_match_leaves_the_verb_keys_nothing_to_send[regex]`; the limit itself in `tests/test_match_bound.py` |
| First-button click on a box's indent or marker; the fold keys on the table's lower border (DL-512) | `test_pilot_dl512_a_click_on_a_box_marker_folds_and_unfolds_it`, `test_pilot_dl512_a_click_left_of_a_nested_box_name_folds_that_box`, `test_pilot_dl512_a_click_on_a_box_name_selects_and_does_not_fold`, `test_pilot_dl512_a_click_left_of_a_job_name_folds_nothing`, `test_pilot_dl512_under_a_filter_a_first_column_click_folds_nothing`, `test_pilot_dl512_the_marker_answers_with_the_table_scrolled`, `test_pilot_dl512_a_right_click_on_a_marker_folds_nothing`, `test_pilot_dl512_the_lower_border_names_the_fold_keys`; a real mouse click on a marker in the browser lane |
| `d`/Enter details, Escape/`q` close | `test_pilot_d_key_opens_the_spec_popup_and_escape_closes_it`, `test_pilot_q_closes_the_spec_popup` |
| `t` triggers, countdown, refresh, selection | `test_pilot_t_opens_the_live_triggers_view_and_operator_verbs_do_not_fire`, `test_pilot_triggers_refresh_preserves_the_cursor_across_ticks` |
| F1/`?` help | `test_pilot_f1_and_question_mark_toggle_the_help_panel`, `test_pilot_f1_reaches_the_maximized_pager_and_the_triggers_screen` |
| `m`, `[]`, `{}`, terminal/viewport resize | `test_pilot_pane_geometry_maximize_toggle_and_share_nudges`; actual size changes in both display lanes |
| A drag on a split's border, `+`/`=`/`-` on the focused pane, the limits, the handle's colour (DL-520) | `test_pilot_dl520_dragging_each_border_moves_it_with_the_pointer_and_back`, `test_pilot_dl520_the_limits_hold_at_both_ends_of_each_split`, `test_pilot_dl520_a_press_and_release_on_a_handle_changes_no_size`, `test_pilot_dl520_a_drag_from_a_handle_selects_folds_and_opens_nothing`, `test_pilot_dl520_a_release_off_the_handle_or_outside_the_app_ends_the_drag`, `test_pilot_dl520_nothing_resizes_while_the_log_is_maximized`, `test_pilot_dl520_plus_and_minus_resize_the_focused_pane`, `test_pilot_dl520_the_bracket_keys_step_a_fifth_from_where_a_drag_left_the_split`, `test_pilot_dl520_a_change_of_the_terminal_size_keeps_the_ratios_and_the_console_rows`, `test_pilot_dl520_the_handle_takes_the_accent_colour_under_the_pointer_and_in_a_drag`, `test_pilot_dl520_the_key_list_names_the_resize_keys`, `test_pilot_dl520_the_zoomed_log_fills_the_screen_and_leaving_restores_the_split` at 120x40 and 80x24, `test_pilot_dl520_the_console_box_at_its_least_shows_an_event_line_above_the_command_line`, `test_pilot_dl520_the_handle_is_only_the_lines_that_meet`; on the drivers' event path, `test_pilot_dl520_a_drag_on_the_drivers_path_resizes_and_its_click_reaches_nothing` and `test_pilot_dl520_a_release_after_the_drag_ended_early_reaches_nothing`; the arithmetic in `tests/test_pane_layout.py`; a real mouse drag on the table's border in the browser lane |
| Log follow, scroll, `/`/`?`, `n/N`, `&`, `F`, `o` | Pager motion/search/filter/follow tests, `test_pilot_log_tail_shows_appended_bytes_for_the_selected_job`, log fault tests |
| Refused queries, disconnect, reconnect | Query fault tests; `test_pilot_reconnect_keeps_the_last_table_and_never_replays_a_mutation` uses socket close/rebind |
| Changed baseline, mixed read headers, reconnect with the same baseline | `test_dl210_u1_u4_trace_baseline_change_restarts_a_longer_trace_next_poll`, `test_dl210_u3_mixed_read_baselines_return_without_updating_the_table`, `test_dl210_u5_reconnect_same_baseline_keeps_cursor_across_a_resume`; both display lanes also observe an engine stop and restart with the same baseline |
| `q`/Ctrl+Q stop versus detach | `test_pilot_quit_confirms_when_the_app_owns_the_run`, `test_pilot_quit_detaches_at_once_when_it_does_not_own_the_run`; real detached viewers exit without stopping their engine |

The PTY lane uses a child terminal process.
The browser lane starts the real `serve` command and a per-session `ui` process.
Chromium, WebKit and Firefox receive actual keyboard input and a viewport resize.
The browser lane also clicks a box's marker with the mouse and sees the box fold (DL-512).
It then drags the table's right border with the mouse and sees it move (DL-520).
Its display check types `ch` and Tab in the console line and sees `CHANGE_STATUS` completed (DL-516).
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
