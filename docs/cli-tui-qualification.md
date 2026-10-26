# CLI and TUI qualification

This inventory maps the public entry points to executable checks.
It distinguishes source tests from installed-command and display checks.
A listed test is evidence for its assertions, not for every option or deployment.
The control and recovery rules are in `control-protocol.md` §3,
`runner-design.md` §11, `access-model.md` §9, and DL-391.

## CLI inventory

The command registry is `src/dsl41/cli.py`.
These source tests invoke Typer or the command implementation.
They do not establish that a separately installed wheel works.

| Public command | Existing source checks |
| --- | --- |
| `lint` | `tests/test_lint.py` |
| `report` | `tests/test_backend_uc.py` |
| `viz` | `tests/test_viz.py`, `tests/test_viz_html.py`, `tests/test_viz_explore.py`; actual explore-page controls in `tests/test_viz_explore_browser.py` |
| `equiv` | `tests/test_equiv.py` |
| `uc` | `tests/test_backend_uc.py` |
| `decompile`, `folds` | `tests/test_dsl.py` |
| `minify` | `tests/test_minify.py` |
| `resolve` | `tests/test_placeholders.py` |
| `run` | `tests/test_runner_journal.py`, `tests/test_journal_replay.py`, `tests/test_nightbank_deploy.py` |
| `rehearse` | `tests/test_semantics.py`, `tests/test_rehearse_check.py` |
| `journal` | `tests/test_journal_replay.py`, `tests/test_estate_wide.py` |
| `runs` | `tests/test_run_history.py` |
| `query` | `tests/test_runner_control.py`, `tests/test_cli_control_branches.py`, `tests/test_subscriber_bound.py` |
| `sendevent` | `tests/test_runner_control.py`, `tests/test_cli_control_branches.py`; complete TUI recovery reconstruction in `tests/test_tui_recovery_cli.py` |
| `release-held` | `tests/test_runner_control.py`, `tests/test_cli_control_branches.py` |
| `host` | `tests/test_runner_control.py`, `tests/test_cli_control_branches.py`; actor recovery pin in `tests/test_tui_recovery_cli.py` |
| `ui`, `serve` | `tests/test_runner_serve.py`; real PTY and served sessions in `tests/test_tui_recovery_drivers.py` |
| `supervise` | `tests/test_runner_supervisor.py`, `tests/test_cli_control_branches.py` |
| `seal`, `audit`, `verify` | `tests/test_estate.py`, `tests/test_estate_wide.py`, `tests/test_nightbank_boundary.py` |
| `estate prune`, `estate reclaim` | `tests/test_retention.py`, `tests/test_estate_wide.py` |
| `estate args` | `tests/test_estate_manifest.py` |

Release qualification must run the installed executable outside the source checkout.
It must exercise both a core-only install and an install with the `ui` extra.
A source import and `python -m dsl41` are different evidence.

## TUI interaction inventory

`tests/test_runner_tui.py` drives the actual app with Textual Pilot.
Most interactions use a real control socket and synthetic estate.
The baseline/epoch trace tests deliberately script response headers.
They prove the client cursor rules; they do not run a live seal.

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
| Period change, mixed read headers, same-period epoch change | `test_dl210_u1_u4_trace_baseline_change_restarts_a_longer_trace_next_poll`, `test_dl210_u3_mixed_read_baselines_return_without_updating_the_table`, `test_dl210_u5_reconnect_same_baseline_keeps_cursor_across_a_new_epoch`; both display lanes also observe a real live seal and resume |
| `q`/Ctrl+Q stop versus detach | `test_pilot_quit_confirms_when_the_app_owns_the_run`, `test_pilot_quit_detaches_at_once_when_it_does_not_own_the_run`; real detached viewers exit without stopping their engine |

The PTY lane uses a child terminal process.
The browser lane starts the real `serve` command and a per-session `ui` process.
Chromium, WebKit and Firefox receive actual keyboard input and a viewport resize.
The lanes assert the selected job's resulting state through the control socket.
They also require the confirmation/cancellation and recovery text on the terminal stream.
The engine seals live, exits with code 3, and resumes in the successor period.
The same viewer must report the changed baseline and display a newly added job.
The selected child's hold must survive the boundary.
They retain raw terminal output, browser errors and the serve log in
pytest's temporary directory.
Missing Playwright or browser binaries fail an explicitly enabled browser lane.
The default suite skips that lane with a stated reason.

```sh
uv run pytest -q tests/test_runner_tui.py tests/test_tui_recovery_cli.py
uv run pytest -q tests/test_tui_recovery_drivers.py -k real_terminal
DSL41_BROWSER_TESTS=1 uv run pytest -q tests/test_tui_recovery_drivers.py
# Repeat against the packaged executable, with child commands outside the checkout:
DSL41_TUI_EXECUTABLE=/absolute/install/bin/dsl41 DSL41_BROWSER_TESTS=1 \
  uv run pytest -q tests/test_tui_recovery_drivers.py
```

These display checks are bounded smoke checks.
They do not cover every key in each browser, remote authentication, or production logs.
An installed wheel is covered only when the explicit executable override is used.
`DSL41_INSTALLED_EXECUTABLE` is accepted as the shared installed-command override too.
Run those lanes explicitly before claiming them.

## Recovering a TUI mutation

The events console prints a complete shell-quoted command before attempting the mutation.
It includes the socket, verb, payload, request id, revision, baseline, epoch and claimed actor.
The executable names the current backing runtime and the socket path is absolute.
Destructive keyboard actions refuse if their confirmation pins cannot be read.
The app waits for the screen refresh before writing to the control socket.
An unknown answer or a failure after the write starts repeats the command beside UNKNOWN.
The app never retries a mutation on refresh or reconnect.

Read the current estate state first.
If recovery is needed, copy the complete displayed command to a shell.
Do not replace its id, payload, revision, baseline, epoch or actor with current values.
Recovery is limited to the same protocol version and the period retaining the decision.
A stale or colliding recovery must be read as its reported outcome.
It is not permission to compose a new mutation automatically.

`--claimed-actor` preserves logged metadata.
It does not authenticate or delegate authority.
The socket still checks the caller's OS principal under the current access policy.
An access-controlled retry must use the same authenticated principal.
Changing access posture can change the stamped actor and cause a collision.
