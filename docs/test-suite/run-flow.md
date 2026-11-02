# Run flow tests

Ticket #184 of Spec #171 inventoried and judged the tests of the Run flow: Trackers and remotes, Tickets, Attempts, Stages, Deciders, CI and landing.
Every test of these files has a row in [`tests/inventory.csv`](../../tests/inventory.csv) with its behaviour, source, level and verdict.
The cases of one parametrized test share its behaviour, and a case parametrized by Tracker names the Tracker.
The rows of tests outside these files changed only in their measured columns, `duration` and `only_branches`, which `make inventory` rewrote.

## Files

| Area | Files |
|---|---|
| Trackers and remotes | `test_tracker_run.py` (new), `test_github_run.py`, `test_gitlab_run.py`, `test_github_remote.py`, `test_gitlab_remote.py`, `test_base_fetch.py` |
| Tickets and Attempts | `test_run_one_ticket.py`, `test_parallel.py`, `test_recovery.py`, `test_finish.py`, `test_fix_loop.py`, `test_attempt_charge.py`, `test_attempt_failures.py`, `test_attempt_stats.py`, `test_attempt_renderer.py`, `test_cost_duration.py` |
| Stages and Deciders | `test_deciders.py`, `test_decision.py`, `test_human_decider.py`, `test_review.py`, `test_tdd_review.py`, `test_self_pipeline.py` |
| CI and landing | `test_ci_wait.py`, `test_merge_window.py`, `test_throttle.py`, `test_rate_limit.py`, `test_infra_pause.py`, `test_sleep_block.py` |

`test_gitlab_land.py` is gone.
Its landing scenarios run in `test_tracker_run.py` for both Trackers, and its helpers moved to `test_gitlab_run.py`.

## Verdicts

| Verdict | Tests |
|---|---|
| keep | 239 |
| merge | 74 |
| speed up | 27 |
| move down | 7 |
| rewrite | 4 |
| left the suite | 184, listed below |

A merged test runs the scenario its twins ran, and it asserts what each of them asserted.
A test that left the suite was merged into another test or deleted, and a merged test that changed its name counts there too.

- Scenarios that differ only by Tracker run as one test parametrized by `github` and `gitlab` in `test_tracker_run.py`.
- CI tests run on a one-Ticket Spec folder whose remote is on GitHub or GitLab, because the CI phase depends only on the PullRequests.
- GitLab tests that check only the snapshot of a Spec run a Pipeline whose one Gate abandons every Ticket, so the Run reads the Spec and starts no agent.
- The risk Decider rules, the reviewer schemas, the pace of a late subscription window and the rule that opens `infra_failure` moved down to unit tests of public functions.
- No test in scope imports a private name of `src/factory`.

## Time

`make measure-durations` ran three times on a 14-core Apple Silicon Mac.

| Number | Baseline | After |
|---|---|---|
| Tests in scope | 457 | 351 |
| Summed time of the tests in scope | 370.3 s | 176.2 to 180.0 s |
| Summed time of the suite | 681.6 s | 516.3 to 523.7 s |
| Wall time of the pytest session | 51.6 s | 39.7 to 40.4 s |

The baseline is the [one of Ticket #182](baseline.md).
The tests in scope took 308.8 s after Ticket #183 made the harness cheaper and before this Ticket.

## Coverage

`make measure-coverage` before and after this Ticket takes the same branches, and one more.
After it, 7470 of 7875 lines and 1988 of 2246 branches are covered, and the branch map takes 1987 branches against 1986 before.
`src/factory/orchestrator.py:502->504` gained a test, and no branch lost the last test that took it.
The branch map stands in for the mutation score, as [the baseline](baseline.md#branch-map) explains.
A deletion also read the assertions of the test, so the test that replaces it checks the same outcome.

## Gaps

- The warning that a Run cannot tell the default branch of a GitLab project, `src/factory/wiring.py:87`, has no test.
  Issue #204 tracks it with `needs-triage`.

## Deleted tests

Each line names a test that left the suite and the test that kills its mutants in its place.

`H` stands for `tests/test_tracker_run.py::test_run_snapshots_the_open_tickets_once_and_lands_a_pr_that_closes_them_through_the_specs_remote[github|gitlab]`.

### test_github_run.py
- test_run_reads_the_spec_its_sub_issues_and_their_blockers_into_the_local_snapshot_shape -> H[github]: same snapshot, spec.md, ticket.md and claimed branches assertions.
- test_run_that_merged_tickets_pushes_the_integration_branch_and_opens_a_pr_closing_them -> H[github]: same remote branches, PR dict, IntegrationBranchPushed, PullRequestOpened and final lines; the remote is now named upstream.
- test_run_without_the_flag_skips_closed_sub_issues -> H[github]: the Spec has a closed sub-issue #29 that the snapshot leaves out.
- test_edits_and_closed_issues_during_the_run_do_not_reach_it -> H[github]: the agent edits the Spec title and closes #30 mid-Run; ticket.md, claims and PR title and body stay as snapshotted.
  The pin on 4 `gh api` calls is dropped as an internal count.
- test_push_goes_to_the_remote_of_the_spec_repository -> H[github]: origin is renamed upstream and origin points at a fork, and the push goes to upstream.
- test_pr_lists_the_abandoned_tickets_and_those_they_left_waiting -> tests/test_tracker_run.py::test_pr_lists_the_abandoned_tickets_and_those_they_left_waiting[github]: identical.
- test_failed_pr_is_journaled_and_leaves_the_run_state_and_the_command_to_open_it -> tests/test_tracker_run.py::test_failed_pr_is_journaled_and_leaves_the_run_done_with_the_command_to_open_it[github]: same LandingFailed reason prefix, by-hand gh command line and pushed branch, plus Run state done.
- test_tickets_flag_takes_only_the_listed_sub_issues -> tests/test_github_run.py::test_tickets_flag_takes_the_listed_sub_issues_once_and_a_closed_blocker_in_another_repository_counts_as_done: same snapshot ids and PR body.
- test_tickets_flag_takes_a_repeated_number_once -> same merged test: it passes `--tickets 28,28`.
- test_closed_blocker_in_another_repository_counts_as_done -> same merged test: #28 waits on the closed owner/other#5 and its blocked_by is empty.
- test_closed_sub_issue_in_the_tickets_flag_stops_the_run -> tests/test_tracker_run.py::test_closed_ticket_in_the_tickets_flag_stops_the_run[github]: same refusal and message.
- test_open_blocker_outside_the_selection_stops_the_run_before_it_starts -> tests/test_tracker_run.py::test_open_blocker_outside_the_selection_stops_the_run_before_it_starts[github]: same.
- test_spec_with_no_open_sub_issues_stops_the_run -> tests/test_tracker_run.py::test_spec_with_no_open_tickets_stops_the_run[github]: same.
- test_missing_spec_issue_stops_the_run_before_it_starts -> tests/test_tracker_run.py::test_missing_spec_issue_stops_the_run_before_it_starts[github]: same.
- test_local_spec_records_its_pr_in_the_folder_and_never_calls_gh -> tests/test_base_fetch.py::test_project_without_a_remote_runs_from_the_local_base_says_so_once_and_records_its_pr_in_the_spec_folder: same scenario (Spec folder detaches the remotes), now asserts no gh call, no remote branch and the recorded PR.
- test_a_waiting_human_decision_labels_and_comments_the_issue_and_the_answer_unlabels_it -> tests/test_tracker_run.py::test_a_waiting_human_decision_is_projected_onto_its_issue_whatever_the_owner_does_to_the_issue[github]: #30 is seen labelled with one comment, then unlabelled with the outcome comment.
- test_closing_the_issue_of_a_waiting_ticket_does_not_affect_the_run -> same merged test: the owner closes #28 while it waits; the Run ends done with both Tickets claimed.
- test_an_answer_to_an_issue_the_owner_already_unlabelled_still_publishes_the_outcome -> same merged test: the owner unlabels #28 before answering; the outcome comment arrives and no TrackerDeliveryFailed is journaled.
- test_an_unreachable_github_never_stops_the_run_and_the_projection_is_delivered_after_it_comes_back -> tests/test_tracker_run.py::test_an_unreachable_tracker_never_stops_the_run_and_the_projection_is_delivered_after_it_comes_back[github]: identical.

### test_gitlab_run.py
- test_run_reads_the_spec_its_child_tasks_and_their_front_matter_blockers_into_the_snapshot_shape[gitlab.example.com] -> H[gitlab]: same snapshot shape and Artifacts.
- test_run_reads_the_spec_its_child_tasks_and_their_front_matter_blockers_into_the_snapshot_shape[gitlab.com] -> tests/test_gitlab_run.py::test_a_work_items_link_on_any_gitlab_host_reads_the_spec_from_that_host: Tickets and blockers read from gitlab.com, every glab call goes to that host.
- test_work_items_link_is_a_gitlab_link_too -> same test: the link is a work_items link.
- test_native_blocking_links_join_the_front_matter -> tests/test_gitlab_run.py::test_blockers_join_front_matter_and_native_links_and_a_closed_one_anywhere_counts_as_done: #30 waits on 29 by front matter and 28 natively, giving ["29", "28"].
- test_a_closed_native_blocker_counts_as_done_and_an_open_one_in_another_project_stops_the_run -> split: the closed half into the blockers test above, the open half into tests/test_gitlab_run.py::test_an_open_native_blocker_in_another_project_stops_the_run_with_the_tickets_name with the same message.
- test_closed_blocker_that_is_not_a_child_task_counts_as_done -> blockers test above: #28 waits on closed #5 and '#27', neither a child.
- test_run_without_the_flag_skips_closed_and_non_task_children -> H[gitlab]: the Spec has a closed child #29 and a child #31 that is no Task.
- test_tickets_flag_takes_only_the_listed_child_tasks -> tests/test_gitlab_run.py::test_tickets_flag_takes_listed_child_tasks_and_part_of_tickets.
- test_tickets_flag_takes_part_of_tickets -> same test.
- test_tickets_flag_with_a_number_that_is_not_a_child_task_stops_the_run -> tests/test_gitlab_run.py::test_tickets_flag_refuses_a_number_that_is_neither_a_child_task_nor_part_of_the_spec: same message for a number outside the Spec.
- test_closed_child_task_in_the_tickets_flag_stops_the_run -> tests/test_tracker_run.py::test_closed_ticket_in_the_tickets_flag_stops_the_run[gitlab].
- test_spec_with_no_open_child_tasks_stops_the_run -> tests/test_tracker_run.py::test_spec_with_no_open_tickets_stops_the_run[gitlab].
- test_open_blocker_outside_the_selection_stops_the_run_before_it_starts -> tests/test_tracker_run.py::test_open_blocker_outside_the_selection_stops_the_run_before_it_starts[gitlab].
- test_gitlab_access_error_stops_the_run_before_it_starts -> tests/test_gitlab_run.py::test_check_and_run_report_each_glab_problem_with_its_fix[sign-in]: `denied` and `signed_out` both fail `glab auth status`, the same branch, and that test asserts the run refusal with the sign-in message.
- test_unknown_host_or_project_or_issue_stops_the_run_before_it_starts -> unknown host: check_and_run[sign-in] (a host the fake does not hold fails auth status); unknown project: check_and_run[access]; missing issue: tests/test_tracker_run.py::test_missing_spec_issue_stops_the_run_before_it_starts[gitlab].
- test_missing_glab_stops_the_run_before_it_starts -> tests/test_gitlab_run.py::test_check_and_run_report_each_glab_problem_with_its_fix[glab]: same refusal and message.
- test_a_waiting_human_decision_labels_and_notes_the_issue_and_the_answer_unlabels_it -> tests/test_tracker_run.py::test_a_waiting_human_decision_is_projected_onto_its_issue_whatever_the_owner_does_to_the_issue[gitlab].
  The pin on the REST methods and encoded paths of the glab calls is dropped; the fake only stores labels and notes for the right item.
- test_closing_the_issue_of_a_waiting_ticket_does_not_affect_the_run -> same merged test [gitlab].
- test_an_answer_to_an_issue_the_owner_already_unlabelled_still_publishes_the_outcome -> same merged test [gitlab].
- test_an_unreachable_gitlab_never_stops_the_run_and_the_projection_is_delivered_after_it_comes_back -> tests/test_tracker_run.py::test_an_unreachable_tracker_never_stops_the_run_and_the_projection_is_delivered_after_it_comes_back[gitlab].
- test_issues_whose_leading_lines_say_part_of_the_spec_are_its_tickets_in_order_of_their_blocked_by_line -> tests/test_gitlab_run.py::test_part_of_tickets_are_issues_whose_leading_lines_name_the_spec_read_over_every_page_in_blocked_by_order: same Tickets 5, 7, 8 and ticket.md check.
- test_part_of_anywhere_but_the_leading_lines_or_for_another_number_is_no_ticket -> same test: issues 6, 9, 13, 14, 15 carry the same non-Ticket texts.
- test_a_spec_with_more_part_of_tickets_than_one_page_holds_is_read_whole -> same test: search pages of 2 over 8 matching issues.
- test_part_of_blocked_by_joins_front_matter_and_native_links -> tests/test_gitlab_run.py::test_child_tasks_and_part_of_tickets_mix_join_every_blocker_source_and_an_issue_in_both_counts_once: #5 joins Blocked by and a native link, #6 joins Part of and front matter.
- test_child_tasks_and_part_of_tickets_mix_and_an_issue_in_both_counts_once -> same test.

### test_gitlab_land.py
- test_run_that_merged_tickets_pushes_the_integration_branch_and_opens_an_mr_closing_them -> H[gitlab].
- test_push_goes_to_the_remote_of_the_spec_project -> H[gitlab]: the push goes to upstream, not the fork origin.
- test_run_on_the_default_branch_does_not_warn -> H[gitlab]: asserts an empty stderr.
- test_mr_lists_the_abandoned_tickets_and_those_they_left_waiting -> tests/test_tracker_run.py::test_pr_lists_the_abandoned_tickets_and_those_they_left_waiting[gitlab].
- test_failed_mr_is_journaled_and_prints_the_command_to_open_it -> tests/test_tracker_run.py::test_failed_pr_is_journaled_and_leaves_the_run_done_with_the_command_to_open_it[gitlab].
- test_failed_push_is_journaled_and_prints_the_commands_to_land_it_by_hand -> tests/test_github_run.py::test_failed_push_is_journaled_and_leaves_the_run_state_and_the_commands_to_land_it covers the shared push failure; the glab command it prints is checked by the failed-PR test [gitlab].
- test_run_that_merged_no_ticket_opens_no_mr -> tests/test_github_run.py::test_run_that_merged_no_ticket_opens_no_pr: landing skips before any PullRequests adapter is called.
- test_run_warns_when_base_is_not_the_default_branch_of_the_project -> tests/test_gitlab_run.py::test_run_warns_when_base_is_not_the_default_branch_of_the_project: same message, on a Run whose Gate abandons every Ticket, since the warning comes at Run start.

### test_base_fetch.py
- test_project_without_a_remote_runs_from_the_local_base_and_says_so_once -> renamed test_project_without_a_remote_runs_from_the_local_base_says_so_once_and_records_its_pr_in_the_spec_folder, same assertions plus those of the GitHub local-Spec test.

### test_run_one_ticket.py

- tests/test_run_one_ticket.py::test_run_lands_the_agent_commit_on_the_integration_branch -> tests/test_run_one_ticket.py::test_run_lands_each_agent_commit_with_its_trailers_and_shows_the_tree_and_status: same one-Ticket Run, asserts hello.txt on the Integration branch and the summary subject.
- tests/test_run_one_ticket.py::test_stage_boundary_commit_carries_trailers_and_falls_back_to_stage_subject -> same merged test: Ticket t2 has no summary, so its commit falls back to `implement: t2`; both commits are checked for the four trailers and the factory author.
- tests/test_run_one_ticket.py::test_run_leaves_the_owner_branches_alone -> same merged test: owner branches unchanged, new branches under `factory/<run>`, clean status.
- tests/test_run_one_ticket.py::test_agent_gets_an_immutable_brief_of_pointers_to_files_rendered_from_the_snapshot -> same merged test: spec.md, ticket.md, read-only brief, inputs, base, output.json.
- tests/test_run_one_ticket.py::test_commit_subject_is_the_first_line_of_summary -> same merged test: t1's summary carries a forged `Factory-Ticket` line, the subject is its first line and the body has no `forged`.
- tests/test_run_one_ticket.py::test_attempt_artifact_records_the_commit_range -> same merged test: attempt.json head_before, head_after and outcome.
- tests/test_run_one_ticket.py::test_status_shows_each_ticket_with_its_node_round_attempt_and_the_run_state -> same merged test: exact `factory status` lines for both Tickets.
- tests/test_run_one_ticket.py::test_run_prints_its_id_first_and_then_the_tree_as_it_moves -> same merged test: first line, live rows, final tree and final line.
- tests/test_run_one_ticket.py::test_output_that_breaks_the_role_schema_is_never_committed -> tests/test_attempt_failures.py::test_output_that_breaks_the_role_schema_fails_as_an_executor_crash_and_is_never_committed: same scenario, keeps the Integration branch at main and the exact `partial` status lines.

### test_attempt_failures.py

- tests/test_attempt_failures.py::test_failed_attempt_is_replaced_by_a_new_one_with_the_same_brief -> tests/test_attempt_failures.py::test_failed_attempts_are_replaced_with_the_same_brief_and_their_changes_go_to_a_patch: three Attempts (crash, hang, done) with identical briefs but `attempt` and `output_dir`, causes and the final tree.
- tests/test_attempt_failures.py::test_uncommitted_changes_of_a_failed_attempt_go_to_a_patch_and_the_workspace_is_reset -> same merged test: read-only discarded.patch with `+# Broken` and `+half`, head_before, README reset.
- tests/test_attempt_failures.py::test_a_hung_attempt_times_out -> same merged test: second Attempt hangs, cause `timeout`, patch, no junk; the fixed 0.5 s sleep became a wait until the agent's process group is gone.
- tests/test_attempt_failures.py::test_stage_exits_with_failed_after_its_last_attempt -> tests/test_attempt_failures.py::test_stage_exits_with_failed_after_its_last_attempt_and_each_failure_carries_its_class: same status row, no refs/factory, Integration branch at main.
- tests/test_attempt_failures.py::test_auth_failure_carries_its_class -> same merged test: its first Attempt fails `auth` and the Journal says so.
- tests/test_attempt_failures.py::test_a_silent_attempt_is_killed_as_stalled_though_its_heartbeat_keeps_coming -> tests/test_attempt_failures.py::test_a_silent_attempt_is_killed_as_stalled_and_failed_takes_its_own_route: stalled cause, silent bounds, patch.
  The retry after a stall is the charge of `stalled` like `timeout`, which tests/test_attempt_charge.py::test_timeout_kills_the_agent_and_spends_an_attempt covers; the stall is now 1m so the Run ticks 5 times less.
- tests/test_attempt_failures.py::test_status_shows_how_long_a_stalled_attempt_was_silent -> same merged test: `status --timeline` shows `silent 1 min N s`.
- tests/test_attempt_failures.py::test_failed_is_an_outcome_of_its_own_with_its_own_route -> same merged test: the stalled Attempt's `failed` routes to `rescue`, which lands hello.txt, nodes implement, rescue, merge, done.
- tests/test_attempt_failures.py::test_sandbox_that_does_not_start_fails_the_attempt -> tests/test_attempt_failures.py::test_attempts_the_sandbox_lost_or_a_rate_limit_ended_spend_no_attempt_and_a_rate_limit_spends_no_loss: `sandbox_start` cause, `did not start` reason, later Attempt done.
- tests/test_attempt_failures.py::test_attempts_the_sandbox_lost_spend_no_attempt_of_the_stage -> same merged test: two `sandbox_start` losses, every brief in Round 1, status ATTEMPT and LOST columns.
- tests/test_attempt_failures.py::test_attempt_numbers_do_not_repeat_after_a_loss -> same merged test: AttemptStarted numbers 1, 2, 3, 4.
- tests/test_attempt_failures.py::test_rate_limit_between_two_sandbox_losses_spends_no_loss -> same merged test: two losses fill the default `sandbox_retries` of 2, a rate limit follows, and no SandboxRetriesExhausted appears.
- tests/test_attempt_failures.py::test_sandbox_retries_of_zero_fail_the_stage_on_the_first_loss -> tests/test_recovery.py::test_stage_fails_once_its_sandbox_vanished_more_often_than_sandbox_retries: `sandbox_retries: 0` fails the Stage on its first loss, with SandboxRetriesExhausted and one Attempt; tests/test_attempt_charge.py::test_the_node_fails_once_the_sandbox_was_lost_more_often_than_sandbox_retries kills the comparison mutants.
- tests/test_attempt_failures.py::test_output_that_breaks_the_role_schema_fails_as_an_executor_crash -> tests/test_attempt_failures.py::test_output_that_breaks_the_role_schema_fails_as_an_executor_crash_and_is_never_committed: same cause, errors, no refs/factory, Workspace removed.
- tests/test_attempt_failures.py::test_stage_fails_with_the_last_loss_once_sandbox_retries_are_used_up -> tests/test_attempt_failures.py::test_stage_fails_with_the_last_loss_once_sandbox_retries_are_used_up_and_names_it: same scenario and assertions.
- tests/test_attempt_failures.py::test_stage_that_ran_out_of_sandbox_retries_names_the_last_loss_in_the_journal_and_in_status -> same merged test: SandboxRetriesExhausted fields and the status line.

### test_cost_duration.py

- tests/test_cost_duration.py::test_attempt_finished_writes_usage_from_model_usage_and_the_duration -> tests/test_cost_duration.py::test_every_attempt_writes_its_usage_or_unknown_and_its_duration_and_status_and_the_projection_sum_them: t1 waits while the clock jumps 90 s; usage, duration bound and duration from the Journal times.
- tests/test_cost_duration.py::test_failed_attempt_with_a_result_writes_its_usage_too -> same merged test: the second Run's first Attempt runs out of turns and writes its usage.
- tests/test_cost_duration.py::test_attempt_without_a_result_writes_its_usage_as_unknown_not_zero -> same merged test: the crashed Attempt has usage None and cost_usd NULL in the attempts projection.
- tests/test_cost_duration.py::test_sums_show_that_they_hold_attempts_of_unknown_cost -> same merged test: final line, status header and row end in `$0.72+?`.
- tests/test_cost_duration.py::test_attempts_projection_gives_the_price_of_a_stage_across_runs -> same merged test: projection across two Runs and the column check.
- tests/test_cost_duration.py::test_status_shows_duration_and_cost_per_ticket_and_per_run -> same merged test: status of the first Run with two Tickets, duration and cost per Ticket and Run.
- tests/test_cost_duration.py::test_every_node_of_a_ticket_writes_its_duration_and_only_stages_have_a_cost -> tests/test_cost_duration.py::test_every_node_writes_its_duration_only_stages_have_a_cost_and_the_tree_shows_both_so_far: same pipeline and event assertions.
- tests/test_cost_duration.py::test_the_tree_shows_the_time_since_the_start_and_the_cost_so_far -> same merged test: the waiting `review` Stage costs 0.1, its live row shows `$0.42`, the final tree and line show `$0.52` and the time after a 125 s jump.
- tests/test_cost_duration.py::test_on_a_tty_the_tree_is_redrawn_each_tick_so_the_time_moves_without_events -> tests/test_cost_duration.py::test_on_a_tty_the_tree_is_redrawn_each_tick_and_no_row_wraps_when_the_terminal_narrows: the first 5 wide frames show at least 3 times.
- tests/test_cost_duration.py::test_on_a_tty_narrower_than_the_table_no_row_wraps_so_each_redraw_replaces_the_tree -> same merged test: every frame after the narrowing has rows under 40 columns.
- tests/test_cost_duration.py::test_narrowing_the_terminal_while_a_run_is_live_moves_up_over_the_rows_the_old_tree_rewrapped_to -> same merged test: the move-up count equals the rows the last wide tree rewraps to at 40 columns.

### test_fix_loop.py

- tests/test_fix_loop.py::test_fix_gets_only_its_declared_inputs_and_skips_an_optional_one_without_a_source -> tests/test_fix_loop.py::test_fix_gets_only_its_declared_inputs_as_read_only_artifacts_and_rounds_all_gives_every_round: exact implement and first fix inputs, optional input skipped, output_dir.
- tests/test_fix_loop.py::test_inputs_are_read_only_artifacts_outside_the_workspace -> same merged test: every input of the second fix lies in the run dir, outside the Workspace, read-only; output.json handoff.
- tests/test_fix_loop.py::test_rounds_all_gives_every_round_of_the_named_stages -> same merged test: `rounds: all` gives (implement, 1) and (fix, 1), and Gate probes 1 and 2.
- tests/test_fix_loop.py::test_route_with_reset_rounds_is_journaled -> same merged test: RoundsReset event.
- tests/test_fix_loop.py::test_next_round_gets_the_handoff_of_the_last_one_and_its_commit_range -> tests/test_fix_loop.py::test_next_round_gets_the_handoff_of_the_last_one_and_switches_to_the_escalation_model_from_its_round: same handoff and commit range of fix Round 1, base, and now the status row with Round 3.
- tests/test_fix_loop.py::test_stage_switches_to_the_escalation_model_from_the_given_round -> same merged test: models sonnet, opus, opus in requests and the Journal.
- tests/test_fix_loop.py::test_fix_loop_runs_out_of_rounds_and_goes_to_then_with_exhausted -> tests/test_fix_loop.py::test_fix_loop_runs_out_of_rounds_and_its_then_stage_gets_the_cause_that_sent_the_ticket_into_it: RoundsExhausted event and nodes; the status ROUND column above 1 moved to the escalation test.
- tests/test_fix_loop.py::test_stage_after_exhausted_gets_the_cause_that_sent_the_ticket_into_the_loop -> same merged test: rescue gets the cause of Gate probe 2.

### test_parallel.py

- tests/test_parallel.py::test_run_records_the_final_pull_request_in_the_tracker_folder -> tests/test_parallel.py::test_ticket_starts_after_its_blockers_merged_from_the_tip_and_the_run_records_its_pr_in_the_folder: the PR file's title, from line, Ticket list and the printed path.
- tests/test_parallel.py::test_ticket_blocked_by_an_abandoned_ticket_never_starts -> tests/test_tracker_run.py::test_pr_lists_the_abandoned_tickets_and_those_they_left_waiting: #30 blocked by the abandoned #28 is `not started`; the branch check shows no branch only this test took.
- tests/test_parallel.py::test_slot_cap_of_the_machine_holds_back_attempts_of_independent_tickets -> tests/test_parallel.py::test_runs_on_one_machine_share_the_slot_cap_and_it_holds_back_independent_tickets_of_one_run: Run `a` has two independent Tickets and `max_parallel: 2`, and with `slots: 1` its Attempts and those of Run `b` go start, end three times.
- tests/test_parallel.py::test_runs_on_one_machine_share_the_slot_cap -> tests/test_parallel.py::test_runs_on_one_machine_share_the_slot_cap_and_it_holds_back_independent_tickets_of_one_run: the same two Runs share the cap, with one more Ticket in Run `a`.

### test_recovery.py

- tests/test_recovery.py::test_running_run_beats_on_the_injected_clock -> tests/test_recovery.py::test_run_beats_on_the_injected_clock_and_once_it_stops_beating_is_orphaned_and_frees_its_slots: the Run runs past a minute with a parked agent before the crash, beats count and gaps, no RunOrphaned while alive.
- tests/test_recovery.py::test_run_that_stopped_beating_is_orphaned_and_frees_its_slots -> same merged test: running until stale, then orphaned, Slots freed, silent_seconds.
- tests/test_recovery.py::test_resume_loses_the_attempt_in_flight_stops_its_agent_and_goes_on_from_the_journal -> tests/test_recovery.py::test_resume_takes_up_only_an_orphaned_run_loses_its_attempt_in_flight_and_goes_on_from_the_journal: AttemptLost, unknown usage and duration, patch, agent stopped and not adopted.
- tests/test_recovery.py::test_resume_takes_up_only_an_orphaned_run -> same merged test: resume of a running, an unknown and a done Run is refused.
- tests/test_recovery.py::test_resume_keeps_rounds_and_merged_tickets_of_the_journal -> same merged test: t1 merged before the crash, TicketClaimed, both files, AttemptStarted rounds, branches.
- tests/test_finish.py::test_finish_of_an_ended_or_unknown_run_is_refused -> same merged test: `finish` of the done Run and of an unknown Run is refused.
- tests/test_recovery.py::test_resume_restores_a_waiting_decision_and_its_answer_in_the_inbox -> tests/test_recovery.py::test_decide_sent_to_a_dead_run_orphans_it_waits_in_the_inbox_and_resume_applies_it_on_its_first_tick: waiting() holds t1 after the crash, one DecisionWaiting, decided accept, hello.txt merged.
  The answer from a second process to a live resumed Run is the same Command path that tests of `decide` cover.
- tests/test_recovery.py::test_attempt_whose_sandbox_vanished_is_lost_and_retried_without_spending_an_attempt -> tests/test_recovery.py::test_attempts_whose_sandbox_or_workspace_vanished_are_lost_and_retried_without_spending_an_attempt: t1 loses its Sandbox, cause, mended, attempt 1, status `lost its Sandbox 1x`.
- tests/test_recovery.py::test_attempt_whose_workspace_vanished_is_lost_and_the_workspace_made_again -> same merged test: t2 loses its Workspace, cause and mended.
- tests/test_recovery.py::test_cancel_sent_to_a_dead_run_waits_in_the_inbox_and_is_applied_after_resume -> tests/test_recovery.py::test_cancel_and_limits_sent_to_a_dead_run_wait_in_the_inbox_and_are_applied_after_resume: pending then accepted with its text, TicketCancelled, t1 not started again, partial.
- tests/test_recovery.py::test_limits_sent_to_a_dead_run_are_applied_after_resume -> same merged test: LimitsChanged with max_parallel 2.

### test_finish.py

- tests/test_finish.py::test_finish_abandons_unmerged_tickets_and_ends_the_run_partial -> tests/test_finish.py::test_finish_abandons_unmerged_tickets_keeps_their_work_ends_the_run_partial_and_records_the_pr: same crashed pair and assertions.
- tests/test_finish.py::test_finish_keeps_unsaved_work_in_the_abandoned_ref_and_cleans_up -> same merged test: abandoned ref content, branch and Workspace gone, SandboxRemoved, agent stopped, status line.
- tests/test_finish.py::test_finish_of_a_local_run_records_the_pr_with_the_abandoned_ticket_listed -> same merged test: PR file path and abandoned line.
- tests/test_finish.py::test_finish_abandons_a_ticket_that_never_started -> tests/test_finish.py::test_finish_abandons_tickets_that_never_started_and_opens_no_pr_without_a_merged_ticket: both Tickets abandoned, one Attempt.
- tests/test_finish.py::test_finish_of_a_run_without_merged_ticket_opens_no_pr_and_says_why -> same merged test: the no-PR message, no PullRequestRecorded, no pull_requests folder.
- tests/test_finish.py::test_finish_with_a_failed_push_journals_it_and_prints_the_commands -> tests/test_github_run.py::test_failed_push_is_journaled_and_leaves_the_run_state_and_the_commands_to_land_it: `finish` lands through the same landing code as a Run's end; the branch check shows no branch only the finish tests took.
- tests/test_finish.py::test_finish_with_a_failed_pr_journals_it_and_prints_the_command -> tests/test_tracker_run.py::test_failed_pr_is_journaled_and_leaves_the_run_done_with_the_command_to_open_it: same reason.
- tests/test_finish.py::test_finish_takes_a_run_stopped_with_ctrl_c -> tests/test_parallel.py::test_run_stopped_mid_attempt_kills_its_agent_leaves_the_run_orphaned_for_finish_and_gives_back_its_slot[press_ctrl_c]: after Ctrl-C the test finishes the Run, which ends `partial` with the Ticket abandoned.

### test_attempt_renderer.py

- tests/test_attempt_renderer.py::test_attempt_terminal_log_follows_the_agent_and_the_raw_stream_stays_raw -> tests/test_attempt_renderer.py::test_attempt_terminal_log_follows_the_agent_attach_prints_it_and_the_raw_stream_stays_raw: same Run and assertions.
- tests/test_attempt_renderer.py::test_attach_prints_the_terminal_of_the_latest_attempt_of_a_ticket -> same merged test: `attach` prints `Done.`.
- tests/test_attempt_renderer.py::test_attach_to_a_ticket_without_attempts_or_a_missing_run_fails -> same merged test: `no Attempt` and `no Run`.

- tests/test_deciders.py::test_risk_function_lets_a_change_below_the_thresholds_on_and_writes_its_outcome_as_an_artifact -> tests/test_deciders.py::test_risk_function_decides_with_the_thresholds_of_the_project_file_and_writes_its_outcome_as_an_artifact: same assertions on decision.json, DecisionDecided, the merge and no judge Attempt; the safe reason text moved to the unit test test_risk_function_defaults_to_one_way_and_cross_module_and_sends_a_change_that_reaches_one_on_risky
- tests/test_deciders.py::test_risk_function_defaults_to_cross_module_and_the_project_file_sets_the_thresholds -> the harness test above checks the override (safe at cross_module with blast_radius external), and test_risk_function_defaults_to_one_way_and_cross_module_and_sends_a_change_that_reaches_one_on_risky checks the default threshold and its reason
- tests/test_deciders.py::test_risk_function_sends_a_one_way_door_on_its_risky_route_with_the_reviewers_notes -> test_risk_function_gives_a_one_way_door_its_risky_outcome_with_the_reviewers_notes: same Ruling; routing an outcome to abandoned is checked by test_model_decider_that_crashes_is_replaced_and_escalates_once_every_attempt_failed
- tests/test_deciders.py::test_function_outcomes_come_from_with_and_must_be_declared -> test_risk_function_takes_its_outcome_names_from_with (outcome names) and test_check_requires_the_outcomes_of_a_risk_function_to_be_declared (declaration)
- tests/test_deciders.py::test_risk_function_escalates_to_escalate_to_when_the_review_lacks_door_and_blast_radius -> test_risk_function_escalates_when_no_output_carries_a_valid_door_and_blast_radius (Ruling), test_an_escalate_route_in_next_wins_over_escalate_to (the escalate outcome through the CLI) and test_model_decider_that_crashes_is_replaced_and_escalates_once_every_attempt_failed (escalate_to route)
- tests/test_deciders.py::test_model_decider_is_a_fresh_agent_that_gets_the_closed_list_and_picks_from_it -> test_model_decider_is_a_fresh_agent_that_is_tried_again_off_the_closed_list_and_loses_its_workspace_changes: same Brief and decision.json assertions
- tests/test_deciders.py::test_model_decider_with_an_outcome_off_the_list_is_tried_again -> same merged test: two judge Attempts, the failed one names `ship it`, the outcome is safe
- tests/test_deciders.py::test_model_decider_changes_to_the_workspace_are_discarded -> same merged test: WorkspaceViolated on judge and the Integration branch without sneaky.txt
- tests/test_deciders.py::test_model_decider_that_keeps_answering_off_the_list_escalates -> test_model_decider_that_crashes_is_replaced_and_escalates_once_every_attempt_failed: two failed judge Attempts, the last off the list, decision escalate and the route to abandoned
- tests/test_deciders.py::test_model_decider_that_crashes_is_replaced_and_then_escalates -> same test: the crash gets a second Attempt and the Decision escalates once both failed
- tests/test_deciders.py::test_model_decider_whose_sandbox_is_lost_spends_no_attempt_and_escalates_past_sandbox_retries (two Runs) -> rewritten as one Run with attempts 1: a second Attempt after the first loss proves no attempt was spent, and the second loss escalates with the same decision.json
- tests/test_human_decider.py::test_decide_answers_a_human_decision_and_prints_the_outcome -> test_decide_answers_a_human_decision_whose_card_waits_in_its_artifact_and_whose_sandbox_stops_meanwhile: same assertions
- tests/test_human_decider.py::test_entering_a_human_decision_renders_the_question_card -> same merged test: same card assertions
- tests/test_human_decider.py::test_a_terminal_that_is_no_tty_is_not_asked -> same merged test asserts no card on the output; the terminal route that ends the Ticket (abandon, no Sandbox restart) is taken by test_answers_that_name_no_waiting_decision_are_rejected_and_only_the_first_valid_one_is_accepted, which also asserts the abandoned state
- tests/test_human_decider.py::test_the_sandbox_of_a_waiting_ticket_is_stopped_and_started_again_by_the_answer -> same merged test, run on the fake sbx: same stop and start assertions
- tests/test_human_decider.py::test_an_outcome_outside_the_list_is_rejected -> test_an_answer_typed_in_the_terminal_of_the_run_is_the_same_command types `ship-it` first and asserts the same message; the CLI exit code of a rejected answer is asserted by test_rework_needs_reasons_and_the_next_stage_gets_them_as_its_cause; check_ruling's message has the unit test test_an_outcome_that_is_none_of_the_decisions_is_refused
- tests/test_human_decider.py::test_the_first_valid_answer_is_accepted_and_the_second_is_rejected_as_already_decided -> test_answers_that_name_no_waiting_decision_are_rejected_and_only_the_first_valid_one_is_accepted: same assertions, with the answers as abandon
- tests/test_human_decider.py::test_an_answer_sent_while_the_ticket_waited_for_none_is_rejected_when_a_decision_comes -> same merged test: the stale Command is rejected with the same text
- tests/test_human_decider.py::test_waiting_for_the_owner_holds_no_slot_and_the_other_tickets_go_on -> test_waiting_tickets_hold_no_slot_and_a_line_typed_for_a_card_decided_elsewhere_is_dropped: same setup; it records the Slots when both Tickets wait and asserts none, and asserts two implement Attempts
- tests/test_human_decider.py::test_a_line_typed_for_a_card_decided_elsewhere_is_dropped_and_not_sent_for_the_next_card -> renamed into the merged test above, assertions kept
- tests/test_human_decider.py::test_the_tracker_gets_the_card_and_the_label_and_then_the_outcome_without_the_label -> test_the_tracker_gets_the_card_and_the_label_after_it_comes_back_and_then_the_outcome_without_the_label: same label and comment assertions
- tests/test_human_decider.py::test_an_unreachable_tracker_never_stops_the_run_and_the_delivery_goes_on_from_the_cursor -> same merged test: TrackerDeliveryFailed and RunFinished done
- tests/test_human_decider.py::test_delivery_repeats_after_the_tracker_comes_back_without_repeating_what_it_took -> same merged test: same TrackerDelivered and label assertions
- tests/test_review.py::test_green_path_goes_through_review_and_its_fixes_land_in_a_stage_boundary_commit -> test_green_path_goes_through_review_whose_fixes_land_in_a_commit_and_whose_findings_reach_the_describer: same assertions
- tests/test_review.py::test_review_output_lists_every_finding_and_reaches_the_describers_brief -> same merged test
- tests/test_review.py::test_review_output_carries_both_axis_reports_to_the_describers_brief -> same merged test
- tests/test_review.py::test_reviewer_output_without_an_outcome_on_a_finding_fails_the_attempt -> tests/test_self_pipeline.py::test_reviewer_schema_requires_findings_with_an_outcome_the_merge_danger_fields_and_both_axis_reports runs factory.attempt.schema_errors on the same Role schema and asserts `'outcome' is a required property`; an output that breaks its schema failing the Attempt is guarded by tests/test_run_one_ticket.py
- tests/test_review.py::test_reviewer_output_without_the_spec_axis_report_fails_the_attempt -> same unit test asserts `'spec' is a required property`
- tests/test_tdd_review.py::test_brief_carries_the_release_for_the_reviewer_to_judge_the_door_by -> test_green_path_goes_through_review_triage_risk_and_acceptance_and_merges_after_the_gates_on_the_synced_tree now runs with release shipped_binary and asserts the same Brief and prompt
- tests/test_tdd_review.py::test_reviewer_output_without_an_axis_report_fails_the_attempt -> split: test_reviewer_output_without_both_axis_reports_breaks_the_tdd_reviewer_schema (the schema rule, unit) and test_review_that_fails_its_attempts_escalates_to_the_owner_whose_card_links_the_gate_verdicts (the same Run, now asserting the escalation card it alone renders, decision.py:181->183 and 185->186)

- tests/test_ci_wait.py::test_run_ends_done_once_the_checks_of_the_pushed_commit_pass -> tests/test_ci_wait.py::test_ci_of_an_older_head_does_not_count_and_the_run_ends_done_once_the_pushed_commit_passes[github]: same CiStarted, CiFinished, no CiAbsent, final line and slots-held assertions, on a Spec folder with a GitHub remote.
- tests/test_ci_wait.py::test_checks_of_an_older_commit_do_not_end_the_wait -> ...passes[github]: the older head lasts past grace in the same scenario, with the same no-CiAbsent and >4 min assertions.
- tests/test_ci_wait.py::test_status_shows_phase_ci_and_the_commit_the_run_waits_for -> ...passes[github]: the same status read in phase ci, with "Run phase: ci" and the waits-for line.
- tests/test_ci_wait.py::test_spec_folder_on_a_github_remote_opens_its_pr_on_github_and_waits_for_its_ci -> ...passes[github]: the scenario is now that Spec folder, with the same PR URL, pushed branch, no pull_requests folder and passed CI assertions.
- tests/test_ci_wait.py::test_gitlab_run_ends_done_once_the_head_pipeline_of_the_mr_passes -> ...passes[gitlab] (success) and test_failed_pipeline_opens_ci_failed_... (running counts as pending): passed outcome and "CI passed" line.
- tests/test_ci_wait.py::test_gitlab_pipeline_of_an_older_commit_does_not_count -> ...passes[gitlab]: same older head past grace, no CiAbsent, passed, >4 min.
- tests/test_ci_wait.py::test_gitlab_pipeline_that_stops_at_a_manual_job_passes -> ...passes[gitlab-manual]: a manual pipeline passes.
- tests/test_ci_wait.py::test_gitlab_failed_pipeline_opens_ci_failed_with_the_trace_of_its_failed_jobs -> tests/test_ci_wait.py::test_failed_pipeline_opens_ci_failed_with_the_trace_of_its_jobs_and_accept_ends_the_run_with_red_ci_recorded: same jobs and trace assertions, plus the accept assertions of the GitHub red test.
- tests/test_ci_wait.py::test_red_ci_opens_ci_failed_with_the_jobs_and_accept_ends_the_run_done_with_the_red_ci_recorded -> test_failed_pipeline_opens_ci_failed_... (accept, card, "CI failed, accepted by the owner", waiting empty) and test_red_checks_open_ci_failed_with_their_jobs_and_recheck_waits_for_the_current_head_until_it_times_out (GitHub jobs, log artifact, card).
- tests/test_ci_wait.py::test_recheck_waits_for_the_ci_of_the_current_head_again -> tests/test_ci_wait.py::test_red_checks_open_ci_failed_with_their_jobs_and_recheck_waits_for_the_current_head_until_it_times_out: same recheck and CiStarted commits [pushed, fixed]; passing after recheck is the passes test's outcome.
- tests/test_ci_wait.py::test_ci_that_does_not_end_in_time_opens_ci_failed -> test_red_checks_open_ci_failed_...: the head after recheck never ends, so timeout, >= timeout and "CI timed out, accepted by the owner".
- tests/test_merge_window.py::test_merge_window_goes_to_one_ticket_at_a_time_in_the_order_they_ask -> tests/test_merge_window.py::test_merge_window_goes_to_one_ticket_at_a_time_in_the_order_they_ask_and_merge_only_fast_forwards: renamed with the fast-forward assertions added.
- tests/test_merge_window.py::test_sync_brings_the_integration_tip_into_the_ticket_and_merge_only_fast_forwards -> ..._and_merge_only_fast_forwards: syncs clean, each sync and merge starts at the previous tip, the tip is the last merge, and the only merge commits are the syncs'.
- tests/test_merge_window.py::test_ticket_that_returns_to_a_stage_gives_up_the_window_and_queues_again -> tests/test_merge_window.py::test_semantic_conflict_fails_the_gate_after_sync_and_the_ticket_gives_up_the_window_for_the_fix_loop: the same scenario, same window assertions.
- tests/test_merge_window.py::test_semantic_conflict_fails_the_gate_after_sync_and_goes_through_the_fix_loop -> test_semantic_conflict_fails_the_gate_after_sync_and_the_ticket_gives_up_the_window_for_the_fix_loop: same scenario, same Gate, fix and file assertions.
- tests/test_merge_window.py::test_sync_conflict_lists_files_and_merged_tickets_and_the_host_commits_the_merge_after_resolve -> tests/test_merge_window.py::test_sync_conflict_lists_files_and_merged_tickets_and_the_host_commits_the_merge_once_no_markers_are_left: the resolve that ends right is the second Attempt, with every artifact and merge commit assertion.
- tests/test_merge_window.py::test_markers_left_after_resolve_block_the_commit_and_the_next_attempt_starts_from_the_conflict -> test_sync_conflict_lists_files_..._once_no_markers_are_left: same [MARKERS, RESOLVED] scenario and conflict_markers assertions.
- tests/test_merge_window.py::test_text_conflict_in_sync_leaves_the_ticket_branch_and_takes_the_conflict_route -> tests/test_merge_window.py::test_text_conflict_in_sync_leaves_the_ticket_branch_and_markers_that_never_go_abandon_it_without_a_merge: same sync outcomes, tip, note and abandoned-ref assertions, with the conflict route going through resolve.
- tests/test_merge_window.py::test_markers_that_never_go_leave_the_ticket_abandoned_without_a_merge_commit -> test_text_conflict_in_sync_leaves_the_ticket_branch_and_markers_that_never_go_abandon_it_without_a_merge: same scenario, exit 1, note of the merged Ticket and no merge commit.
- tests/test_throttle.py::test_a_brake_journaled_as_text_before_the_upgrade_still_shows_in_status -> tests/test_throttle.py::test_usage_above_the_cap_starts_no_new_attempts_until_the_window_resets: the same scenario reads status a second time after rewriting the brake as text.
- tests/test_throttle.py::test_a_run_that_first_sees_a_window_late_gets_no_pace_for_the_time_before (harness) -> the unit test of the same name on Throttle.check: same pace kind, until and limit.
- tests/test_throttle.py::test_a_run_without_throttle_ignores_the_usage -> tests/test_throttle.py::test_limits_reject_a_bad_window_or_share_set_a_cap_and_lift_the_brake_on_the_go: the Run starts without a throttle under usage, and exactly one ThrottleOn comes, from the cap the Owner sets.
- tests/test_throttle.py::test_limits_lift_the_brake_on_the_go -> test_limits_reject_a_bad_window_or_share_set_a_cap_and_lift_the_brake_on_the_go: same "brake lifted" result, second Attempt before the reset, ThrottleOff within bounds, LimitsChanged {seven_day: None}.
- tests/test_throttle.py::test_limits_set_a_cap_on_the_go -> test_limits_reject_...: same "cap 0.35" result and ThrottleOn.
- tests/test_throttle.py::test_limits_reject_an_unknown_window_and_a_share_out_of_range -> test_limits_reject_...: same two refusals and messages.
- tests/test_rate_limit.py::test_rate_limit_without_resets_at_waits_the_fixed_pause -> tests/test_attempt_charge.py::test_rate_limited_without_a_reset_time_pauses_for_the_fixed_time (paused_until = now + RATE_LIMIT_PAUSE), and the next Attempt starting at paused_until is asserted by test_rate_limited_attempt_spends_no_attempt_and_the_next_one_starts_at_resets_at.
- tests/test_rate_limit.py::test_resets_at_that_already_passed_waits_the_fixed_pause -> tests/test_rate_limit.py::test_rate_limits_do_not_hide_the_failures_that_count_and_a_past_resets_at_waits_the_fixed_pause: its second limit's reset time has passed, and the next Attempt starts the fixed pause later.
- tests/test_rate_limit.py::test_status_shows_the_pause_while_the_run_waits -> tests/test_rate_limit.py::test_rate_limited_attempt_spends_no_attempt_and_the_next_one_starts_at_resets_at: same [LIMITED, DONE] scenario with the same status reads.
- tests/test_infra_pause.py::test_failures_of_one_ticket_or_of_different_causes_open_no_decision (harness) -> the unit test of the same name on RunMemory.opens_decision and row_with.
- tests/test_sleep_block.py::test_run_stopped_with_ctrl_c_releases_the_block -> tests/test_sleep_block.py::test_run_stopped_with_ctrl_c_releases_the_block_and_resume_blocks_idle_sleep_again: same Ctrl-C once blocked, then assert_gone, before the resume.
- tests/test_sleep_block.py::test_resume_blocks_idle_sleep_like_run -> test_run_stopped_with_ctrl_c_releases_the_block_and_resume_blocks_idle_sleep_again: renamed.
