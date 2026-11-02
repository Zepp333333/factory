# Check, Gate, Sandbox and CLI tests

Ticket #185 of Spec #171 inventoried and judged the tests of the rest of the factory: `factory check`, Gates, Sandbox, Executors, Commands, cleanup, status and telemetry, the Library and the module graph.
Every test of these files has a row in [`tests/inventory.csv`](../../tests/inventory.csv) with its behaviour, source, level and verdict.
The cases of one parametrized test share its behaviour.
The rows of tests outside these files changed only in their measured columns, `duration` and `only_branches`, which `make inventory` rewrote.

## Files

| Area | Files | Tests before | Tests after |
|---|---|---|---|
| `factory check` and Gates | `test_check.py`, `test_gates.py`, `test_gate_command.py`, `test_gate_coverage.py`, `test_gate_coverage_run.py`, `test_gate_tests.py` | 173 | 152 |
| Sandbox and Executors | `test_sbx_sandbox.py`, `test_sbx_oom.py`, `test_sandbox_reap.py`, `test_host_git_guard.py`, `test_host_tool_failure.py`, `test_claude_code_executor.py`, `test_executor_stream.py`, `test_orca_workbench.py` | 61 | 26 |
| Describer | `test_describer.py` | 35 | 17 |
| Commands, cleanup, status and telemetry | `test_commands.py`, `test_clean.py`, `test_cleanup.py`, `test_status_timeline.py`, `test_status_waits.py`, `test_run_view_osc.py`, `test_telemetry.py`, `test_run_memory.py` | 76 | 32 |
| Library and module graph | `test_library.py`, `test_run_library.py`, `test_context_words.py`, `test_layers.py`, `test_factory_layer.py`, `test_wiring.py`, `test_version.py` | 51 | 26 |
| All | | 396 | 253 |

## Verdicts

| Verdict | Tests |
|---|---|
| keep | 115 |
| speed up | 79 |
| merge | 59 |
| left the suite | 199, listed below |

A merged test runs the scenario its twins ran, and it asserts what each of them asserted.
A test that left the suite was merged into another test or deleted, and a merged test that changed its name counts there too.

- Harness tests that started a Run to check one fact share a Run with the tests of the same scenario.
  The Describer tests start 6 Runs instead of 23, and the Attempts of one Run play the failure modes that each test played alone.
- `test_clean.py` and `test_cleanup.py` both tested a Sandbox that does not stop and a Workspace with uncommitted edits.
  Each pair is one Run in `test_clean.py`, which checks the cleanup at the end of the Run and then `factory clean` without and with `--yes`.
- Tests that checked a sandbox loss or its charge, which `test_attempt_failures.py`, `test_attempt_charge.py` and `test_recovery.py` already check at the same level, are deleted in their favour.
- The checks of `factory check` stay one test per broken config, so the 74 `speed up` tests of `test_check.py` gained only the cheaper harness below.
- The Gate kind unit tests of `test_gate_command.py`, `test_gate_coverage.py` and `test_gate_tests.py` judge fixtures through public functions, so they stay as they are.
- `test_wiring.py` swaps the registries of `factory.wiring` for copies built through the public `Registry`, instead of saving the private `Registry._factories`.
- The source scans of `test_layers.py` and `test_context_words.py` guard `docs/architecture/dependencies.md` and the `CONTEXT.md` naming rule of `CODING_STANDARDS.md`.
  They run no line of `src/factory`, and `test_layers.py` builds the module graph once per worker.
- No test in scope imports a private name of `src/factory`.

## Harness

`Harness.end` returns at once for a test whose every Machine ran `factory check`, and that spawned nothing and started no Sandbox process.
`factory check` runs in the test's process and starts no other, and the scan of every process on the machine cost such a test more than the check itself.
The change is a commit of its own, and the whole suite passed with it before any test of this Ticket changed.

## Time

`make measure-durations` ran three times on a 14-core Apple Silicon Mac.

| Number | Baseline | After |
|---|---|---|
| Tests in scope | 396 | 253 |
| Summed time of the tests in scope | 277.4 s | 99.8 to 113.8 s |
| Summed time of the suite | 681.6 s | 318.1 to 345.1 s |
| Wall time of the pytest session | 51.6 s | 25.1 to 27.5 s |

The baseline is the [one of Ticket #182](baseline.md).

## Coverage

`make measure-coverage` before and after this Ticket took every branch it took before, and six more.
After it, 7471 of 7875 lines and 1994 of 2246 branches are covered, against 7470 lines and 1988 branches before.
These branches gained a test:

- `src/factory/clean.py:71->73` and `src/factory/clean.py:113->124`
- `src/factory/cli/clean.py:52->55`
- `src/factory/cli/status.py:99->101`
- `src/factory/git.py:150->exit`
- `src/factory/metrics.py:322->323`

The branch map stands in for the mutation score, as [the baseline](baseline.md#branch-map) explains.
A deletion also read the assertions of the test, so the test that replaces it checks the same outcome.

Most branches of `src/factory/commands.py` show as untaken, though `test_commands.py` and `test_throttle.py` send every Command.
The tests send them through `Harness.spawn`, a process of its own that the coverage run does not measure.
Issue #210 tracks it with `needs-triage`.

## Dead code

The inventory found no dead product code.
`src/factory/library_lookup.py:347` raises for an item in no Layer, and no caller in `src/factory` reaches it, because each one checks that the item exists first.
It stays, because it guards the public `Library.prompt` and `Library.schema` against an unknown name.

## Gaps

No test before or after this Ticket takes these branches, and each points to behaviour without a test.

Issues #206, #207 and #208 track them with `needs-triage`, one per area below.

- `factory check` of a Pipeline, #206:
  - `src/factory/pipeline_checks.py:62->64`, a Decision whose Role has a Decider that is no model.
  - `src/factory/pipeline_checks.py:117->118` and `:121->122`, a function Decision that reads no fields or whose input Stages have no schema.
  - `src/factory/pipeline_checks.py:134->135`, a feeding Role whose schema allows additional properties.
  - `src/factory/pipeline_checks.py:149->151` and `:196->197`, a required read of another type than the input Stage outputs.
  - `src/factory/pipeline_checks.py:233->220`, a Describer read that some Stage outputs.
  - `src/factory/pipeline_checks.py:318->319`, a `rounds.then` chain that a route on `exhausted` with reset rounds breaks.
  - `src/factory/gates.py:303->299`, a coverage report that names a changed file but none of its changed lines.
- `factory check` of a Library, #207:
  - `src/factory/library_lookup.py:125`, `:166`, `:171`, `:180`, `:285`, `:300`, `:304` and `:310`, a Role, prompt or schema of the wrong shape.
  - `src/factory/wiring.py:295`, a Run without a frozen Library.
  - `src/factory/fingerprint.py:83->84`, the factory version outside a checkout of this repo.
- `factory clean`, `status` and `stats`, #208:
  - `src/factory/clean.py:118->122`, `factory clean` that leaves a Sandbox behind.
  - `src/factory/metrics.py:226->227` and `:228->229`, the utilization of the subscription windows.
  - `src/factory/metrics.py:255->256`, a Ticket that the owner finished by hand.
  - `src/factory/timeline.py:83->84` and `:86->87`, the timeline lines of a decided Decision and an opened PR.
  - `src/factory/stats.py:22->23`, a percentile over no values.

## Deleted tests

Each line names a test that left the suite and the test that kills its mutants in its place.

### test_check.py

- test_check_accepts_a_valid_config -> tests/test_check.py::test_check_accepts_a_valid_config_whose_pipeline_is_given_by_a_relative_or_an_absolute_path: the first check of the default project file exits 0 and prints the same `OK` line.
- test_check_accepts_a_pipeline_given_by_its_absolute_path -> same merged test: the second check names the Pipeline by its absolute path and asserts the same exit code and `OK` line.
- test_vars_are_substituted_from_the_project_file -> tests/test_check.py::test_run_takes_vars_from_the_project_file_and_overrides_that_merge_dicts_and_replace_lists: defaults set the model through `${vars.model}`, the Stage through `claude-${vars.size}`, and the agent's request has model `claude-opus`; the override's inputs come through `${vars.input}`.
- test_overrides_merge_dicts_recursively_and_replace_lists -> same merged test: without `escalate_to` the check passes only because the override merges `failed` into `next`, and the brief's inputs are the override's `["ticket"]`.
- test_sandbox_retries_of_zero_passes -> tests/test_check.py::test_sandbox_retries_is_a_non_negative_integer: after the error for -1, the same test sets 0 and the check exits 0.
- test_sandbox_env_that_clashes_with_the_executor_is_refused -> tests/test_check.py::test_sandbox_env_that_clashes_with_the_executor_is_refused_by_check_and_when_showing_a_role: same exit code 2 and message for `factory check`.
- test_sandbox_env_that_clashes_with_the_executor_is_refused_when_showing_a_role -> same merged test: same exit code 2 and message for `factory check --show reviewer`.
- test_a_pipeline_not_in_the_library_is_an_error -> tests/test_check.py::test_pipeline_named_in_the_project_file_is_found_in_the_library_or_is_an_error: same exit code 2 and message while `mine` is in no Layer.
- test_pipeline_is_found_in_the_library_by_name -> same merged test: once `.factory/pipelines/mine.yaml` exists, the check exits 0.
- test_overrides_assign_a_stage_another_role_of_the_library -> tests/test_check.py::test_overrides_assign_a_stage_another_role_only_when_the_library_has_it: the override to Role `other` passes the check.
- test_overrides_naming_a_role_that_is_in_no_layer_fail_the_check -> same merged test: the override to `reviewer-single` gives the same error.
- test_a_risk_decision_rejects_input_stages_whose_door_and_blast_radius_cannot_match -> tests/test_check.py::test_a_risk_decision_needs_an_input_stage_that_outputs_the_door_and_blast_radius_it_reads: same schema and the same error.
- test_a_risk_decision_accepts_input_stages_that_output_the_values_it_reads -> same merged test: the second schema has the right enums and the check exits 0.
- test_check_reports_git_remotes_that_no_kind_of_pull_requests_serves -> tests/test_check.py::test_check_reports_git_remotes_that_no_kind_of_pull_requests_serves_until_code_host_names_one: same remote, exit code 2 and message parts.
- test_code_host_in_the_project_file_serves_a_remote_that_does_not_tell -> same merged test: with `code_host: gitlab` on the same remote the check exits 0.

### test_gates.py

`A` stands for `tests/test_gates.py::test_passing_gate_sends_the_ticket_on_after_lost_probes_and_reads_only_what_its_probe_wrote`.

- test_passing_gate_sends_the_ticket_on_by_next -> A: same nodes `implement, build, merge, done`, the same verdict keys and the same gate, kind, outcome and exit code.
- test_lost_probe_retries_the_gate_without_spending_a_round -> A: the Sandbox loses two Probes; each ProbeLost names `build`, its attempt and `did not start`, the nodes show no second round and the verdict passes.
- test_gate_reaches_a_verdict_after_as_many_lost_probes_as_sandbox_retries -> A: same two lost Probes, ProbeStarted attempts `[1, 2, 3]` and the losses column `2` of `factory status`.
- test_report_committed_by_the_agent_never_reaches_gate_out -> A: the agent commits the same three fake reports, the Probe command checks `$GATE_OUT` is empty, and the last Probe's `$GATE_OUT` holds only `report.txt` outside the Workspace.
- test_failing_gate_sends_the_ticket_to_fix_and_judges_again -> tests/test_gates.py::test_failing_gate_sends_the_ticket_to_fix_and_judges_again_and_its_probe_leaves_nothing_behind: same nodes, failed and passed verdicts, matched line and log.
- test_probe_leaves_nothing_behind_for_the_next_stage_commit -> same merged test: the Probe writes `probe.txt` and appends to README.md, and the Integration branch has only README.md, factory.yaml and hello.txt with README.md unchanged.
- test_gate_escalates_once_the_ticket_lost_its_sandbox_more_often_than_sandbox_retries -> renamed tests/test_gates.py::test_gate_escalates_once_the_ticket_lost_its_sandbox_more_often_than_sandbox_retries_naming_the_last_loss, same assertions plus those of the next line.
- test_gate_that_escalates_names_the_last_lost_probe_in_the_journal_and_in_status -> same renamed test: the same Run asserts SandboxRetriesExhausted and the `lost its Sandbox 2x` line of `factory status`.
- test_clean_removes_ignored_files_before_the_probe[True-pass] -> tests/test_gates.py::test_caches_the_project_lists_are_no_stray_files_and_clean_removes_them_before_the_probe[True-pass]: same planted file and verdict `pass`.
- test_clean_removes_ignored_files_before_the_probe[False-fail] -> tests/test_gates.py::test_caches_the_project_lists_are_no_stray_files_and_clean_removes_them_before_the_probe[False-fail]: same planted file and verdict `fail`.
- test_caches_the_project_lists_are_no_stray_ignored_files -> both cases above: the agent writes the same cache files under the listed caches, and the Attempt finishes `done` instead of failing as `ignored_files`.
- test_stage_that_writes_a_file_under_gitignore_fails_naming_the_file -> tests/test_gates.py::test_stage_that_writes_files_or_a_nested_repository_under_gitignore_fails_naming_them: same refused Sandbox removal, no GateFinished, cause, error for the fixture file and the removed `tests` dir.
- test_ignored_nested_repository_is_a_stray_the_host_removes -> same merged test: the agent also inits `vendor/lib` as a repository, the cause is `ignored_files`, `removed_ignored` names `vendor/lib/` and it is gone from the Workspace.
- test_gate_that_runs_past_its_timeout_fails -> tests/test_gates.py::test_lost_attempt_and_lost_probe_of_one_ticket_count_together_after_a_probe_that_timed_out: the Probe that is not lost runs `sleep 30` past a 2s timeout, and its verdict is `fail` with reason `timed out after 2s` and no exit code.
- test_lost_attempt_and_lost_probe_of_one_ticket_count_together -> renamed to the test above, with the same ProbeLost, AttemptFinished, SandboxRetriesExhausted and status assertions.
- test_read_only_stage_that_leaves_an_ignored_file_is_a_violation -> F below: its read-only review Stage writes `report.xml`, which the implement Stage's `.gitignore` covers, and the Run still lands while WorkspaceViolated names `report.xml` in `removed_ignored`.

### test_gate_coverage_run.py

`F` stands for `tests/test_gate_coverage_run.py::test_gate_with_from_judges_the_source_probe_and_a_decision_after_it_reads_the_verdict_and_routes_the_ticket`.

- test_gate_with_from_judges_the_changed_lines_by_the_source_probe -> F: same two passing verdicts, the same `changed` summary and the shared log.
- test_gate_with_from_runs_no_probe_and_names_the_probe_it_read -> F: same single ProbeStarted of `tests`, `from` and `probe` of GateFinished, and the `from-tests-probe-1` verdict path.
- test_decision_after_a_gate_with_from_reads_the_verdict_and_routes_the_ticket -> renamed F with the same assertions.
- test_gate_with_its_own_probe_runs_it -> F: the Gate `own` after `cov` runs its own Probe, ProbeStarted names `tests` and `own`, and all three verdicts pass.
- test_changed_lines_below_the_threshold_fail_the_gate -> tests/test_gate_coverage.py::test_changed_lines_below_the_threshold_fail_with_the_uncovered_ones[lcov]: the same `66.7% of 3 changed lines covered, below N%` reason and the uncovered lines, judged by the public `evaluate`; F checks the changed lines that a real Run hands the evaluator, and test_gates.py checks that a failing Gate routes by `fail`.

`S` stands for `tests/test_sbx_sandbox.py::test_ticket_runs_in_a_sbx_sandbox_built_from_the_project_settings_and_left_behind_when_sbx_cannot_delete_it`.
`L` stands for `tests/test_sbx_sandbox.py::test_attempt_and_probe_whose_sandbox_dies_are_lost_with_the_sandbox_status_and_retried_in_the_same_round`.
`O` stands for `tests/test_sbx_oom.py::test_sandbox_tells_an_oom_kill_by_its_count_and_a_second_one_in_a_row_opens_a_run_decision`.

### test_sbx_sandbox.py
- test_ticket_gets_a_sandbox_from_the_pinned_template_with_explicit_resources -> S: same `--name`, `-t`, `--pull`, `--cpus 2` and `--memory 4g` of the one `create`, and hello.txt on the Integration branch.
- test_sandbox_mounts_every_path_at_its_host_path_with_only_the_ticket_outputs_writable -> S: same list of mounts after `claude`.
- test_sandbox_egress_allows_only_the_claude_api_and_the_project_hosts -> S: same two `policy` calls with `pypi.org,files.pythonhosted.org`.
- test_notice_that_sbx_prints_after_its_json_does_not_stop_the_run -> S: sbx prints the same notice after its JSON and the Run ends with code 0.
- test_agent_runs_with_a_placeholder_token_and_no_github_token_or_venv_in_the_workspace -> S: same launch flags, placeholder token, scrubbed `GH_TOKEN` and `ANTHROPIC_API_KEY` and `UV_PROJECT_ENVIRONMENT`.
  The check that `/home/agent/venv` lies outside the Workspace is dropped, since it compares two constants.
- test_project_env_reaches_agents_and_probes_and_the_run_journals_only_its_names -> S: `TOOLCHAIN` reaches the agent and the Probe log, and `RunStarted` journals only the egress and the sorted env names.
- test_command_runs_under_the_stdin_watchdog_and_leaves_its_exit_code_in_the_attempt_dir -> S: same `exit-code` file and `-i -w ... setsid -w sh` exec of the agent.
- test_stopped_sandbox_is_started_before_exec_so_sbx_writes_nothing_to_stderr_log -> S: `idle_stop` is on, stderr.log is empty, the Probe reads hello.txt and two warm-up execs run.
- test_sandbox_that_sbx_cannot_delete_after_its_vm_died_stays_behind_without_blocking_the_run -> S: `rm_fails` is on, with the same `SandboxLeftBehind`, no `SandboxRemoved`, and no Workspace or Ticket branch left.
- test_killed_attempt_stops_the_agent_and_its_children_in_the_sandbox -> O: the third Attempt hangs past `timeout`, ends `timeout` with no `exit-code`, and its tool never writes late.txt.
- test_attempts_the_sandbox_lost_spend_no_attempt_and_the_next_one_gets_the_sandbox_again -> L: first Attempt `sandbox_lost`, second `done`, both in Round 1, hello.txt merged.
- test_attempt_whose_sandbox_dies_is_lost_with_the_sandbox_status -> L: same `and no exit code, Sandbox <run>-t1 is stopped` reason.
  The Stage that fails after `sandbox_retries` losses, with its `status` row, is checked by tests/test_attempt_failures.py::test_stage_fails_with_the_last_loss_once_sandbox_retries_are_used_up_and_names_it and tests/test_recovery.py::test_stage_fails_once_its_sandbox_vanished_more_often_than_sandbox_retries.
- test_probe_whose_sandbox_dies_is_lost_and_retried_without_a_verdict -> L: same nodes, `ProbeLost` of probe 1 with the stopped reason, no verdict of probe-1 and a `pass` of probe 2.
- test_gate_escalates_after_its_last_probe_whose_sandbox_died -> tests/test_gates.py::test_gate_escalates_once_the_ticket_lost_its_sandbox_more_often_than_sandbox_retries_naming_the_last_loss: same nodes `implement, build, abandoned`, `ProbeLost` probes [1, 2] and no `GateFinished`; L checks that a dying sbx Sandbox makes a Probe lost.

### test_sbx_oom.py
- test_agent_the_oom_killer_killed_ends_oom_and_spends_no_attempt -> O: the `oom` Attempts carry no charge, the reason holds `exited with code 137`, and five Attempts run on 3 attempts.
- test_peak_usage_is_left_out_of_the_journal_when_the_sandbox_shows_none -> O: the first `oom` has the default limit `8g` and no `memory_peak`.
- test_sigkill_without_an_oom_kill_is_an_executor_crash -> O: the second Attempt dies by SIGKILL with no new OOM kill, ends `executor_crash` and has no `memory_limit`.
- test_kill_of_the_engine_on_timeout_is_no_oom_even_after_an_oom_kill -> O: the third Attempt counts an OOM kill and hangs, and it ends `timeout`.
- test_second_oom_in_a_row_of_one_ticket_opens_a_run_decision_and_status_advises_more_memory -> O: renamed, same `DecisionWaiting`, card advice and `status` lines, with limit `8g` in place of `1g`.
- test_ooms_of_different_tickets_open_a_run_decision -> tests/test_infra_pause.py::test_two_tickets_failing_alike_open_a_run_decision_and_retry_spends_no_attempt and tests/test_attempt_charge.py::test_an_attempt_held_by_the_runs_decision_spends_nothing_and_opens_it: the rule for two Tickets is the same for every infrastructure cause, and O shows that `oom` is one.

### test_sandbox_reap.py
- test_reap_kills_a_detached_process_in_the_sbx_sandbox_and_spares_the_current_attempt -> S: the agent leaves a detached `sleep 300`, the Probe's start kills it with one `SandboxProcessKilled` of the same pid and command, and the Probe still passes.

### test_host_git_guard.py
- test_agent_pointing_the_worktree_git_file_elsewhere_does_not_redirect_host_git -> tests/test_host_git_guard.py::test_agent_pointing_the_git_file_elsewhere_and_moving_head_redirects_no_host_git_and_writes_no_foreign_ref: same hostile repository, no marks, its HEAD and status unchanged, and the `.git` file restored with a clean status.
- test_engine_writes_no_ref_outside_its_namespaces_even_when_the_agent_moves_head -> same merged test: the same agent also moves the worktree HEAD, and the refs before stay, new refs sit under `factory/<run>` and HEAD points at the Ticket branch.
- test_worktree_git_file_replaced_by_a_directory_is_restored -> tests/test_host_git_guard.py::test_hooks_fsmonitor_and_nested_config_the_agent_plants_never_run_when_its_work_is_discarded_or_committed: the done Attempt replaces `.git` by a directory with a planted fsmonitor and pre-commit hook; no mark, no `.git` in the tree, and `.git` is a file again.
  The clean `git status` of the Workspace is replaced by its HEAD on the Ticket branch, because the nested repository leaves the Workspace dirty.
- test_hooks_planted_by_the_agent_never_run[commit] -> same merged test: the done Attempt plants every hook under `core.hooksPath`.
- test_hooks_planted_by_the_agent_never_run[discard] -> same merged test: the crashed first Attempt plants every hook too.
- test_config_of_a_repository_nested_in_the_workspace_never_runs[commit] -> same merged test: the done Attempt nests the repository with the filter, which stays in the Workspace.
- test_config_of_a_repository_nested_in_the_workspace_never_runs[discard] -> same merged test: the crashed first Attempt nests it too.
- test_fsmonitor_from_the_workspace_config_never_runs -> same merged test: the Target project sets `core.fsmonitor` and its first Attempt crashes.

### test_claude_code_executor.py
- test_agent_runs_claude_with_the_factory_launch_flags -> tests/test_claude_code_executor.py::test_agent_runs_claude_with_the_factory_launch_flags_no_api_credentials_and_the_projects_skills: same flags, prompt, stdin and env.
- test_agent_environment_is_scrubbed_of_api_credentials -> same merged test: the three API variables are set and reach the agent as unset.
- test_project_stage_may_load_the_target_projects_skills -> same merged test: the Target project has the skill `build` and the Attempt ends `done`.
- test_hermetic_stage_loads_no_setting_sources -> tests/test_claude_code_executor.py::test_each_failure_the_agent_streams_ends_its_attempt_with_its_cause_and_reason: the Stage is hermetic and each of its Attempts gets an empty `--setting-sources`.
- test_rejected_rate_limit_ends_the_attempt_as_rate_limited -> same merged test: the first Attempt ends `rate_limited` with `resets_at`.
- test_failed_login_is_an_auth_failure -> same merged test: same cause and reason.
- test_agent_that_crashes_without_a_result_fails_with_its_stderr -> same merged test: same cause and reason.
- test_error_result_is_an_executor_crash_with_its_subtype -> same merged test: same cause and reason.
- test_attempt_fails_when_the_agent_loads_more_than_the_factory_set -> same merged test: same `agent_config` reason and hello.txt in the files.

### test_orca_workbench.py
- test_workspaces_are_shown_in_orca_and_the_run_still_merges -> tests/test_orca_workbench.py::test_workspace_and_attempt_terminal_are_shown_in_orca_with_the_stage_card_and_the_run_still_merges: same `repo add`, visibility, `worktree rm` and merged hello.txt.
- test_attempt_terminal_opens_in_orca_on_the_workspace_and_shows_the_renderer -> same merged test: same terminal worktree, title, output and terminal.log.
- test_workspace_card_shows_the_current_stage_and_round -> same merged test: same first comment and worktree of every card.
- test_terminal_is_not_alive_when_only_a_longer_handle_is -> tests/test_orca_workbench.py::test_workbench_answers_reconciliation_for_workspaces_and_terminals: a terminal under the handle `term` is not alive while `term-1` is.
- test_terminal_ends_when_the_renderer_shows_the_result -> same test: a second terminal on a stream that holds the result stops being alive.

### test_describer.py

`B` stands for `tests/test_describer.py::test_describer_reads_a_brief_of_the_merged_tickets_in_a_throwaway_workspace_and_its_text_opens_the_pr`.
`F` stands for `tests/test_describer.py::test_describer_that_fails_every_attempt_is_journaled_with_each_cause_and_the_engines_text_stands_in`.
`L` stands for `tests/test_describer.py::test_local_run_writes_the_text_of_a_describer_whose_role_requires_no_headings_and_its_names_meet_no_ticket`.

- test_pr_gets_the_title_and_sections_the_describer_wrote_and_the_engines_closes_lines -> B: same PR title, body of the Describer's sections followed by `Closes #28` and `Closes #30`, pull_request.md equal to the PR body, one PullRequestDescribed and no DescriberFailed.
- test_only_the_engines_footer_closes_issues_whatever_the_describer_wrote -> B: the Describer's body carries the same closing keywords, which the PR shows as `Refs`, and the footer closes only the merged #28 and #30 and lists #31, which its Gate abandoned, under `Not landed`.
  The blocking and not-started lines of that list are the Tracker's footer, which tests/test_tracker_run.py::test_pr_lists_the_abandoned_tickets_and_those_they_left_waiting checks.
- test_describer_reads_a_brief_of_the_spec_merged_tickets_stage_outputs_base_and_integration_branch -> B: same base, base ref, Integration branch, spec path, Stage handoff and request model; the inputs list now holds the Gate verdicts of each merged Ticket and leaves out the abandoned #31.
- test_describer_works_on_a_workspace_that_is_gone_afterwards_and_its_edits_are_discarded -> B: same Workspace gone, branches, `junk.txt` in discarded.patch and Integration branch tree.
- test_removal_of_the_describers_sandbox_is_journaled -> B: same DescriberSandboxRemoved event and no DescriberCleanupFailed.
- test_describer_brief_holds_the_gate_verdicts_of_the_merged_tickets -> B: the Gate `build` gives a `gate` input per merged Ticket whose verdict file says `pass`.
- test_describer_that_fails_is_journaled_and_the_pr_opens_with_the_engines_text -> F: Attempt 1 crashes with `executor_crash` and `exited with code 1`, and once every Attempt failed the Tracker's own text and title are the Run's PR Artifacts.
  The PR dict of the fallback on GitHub is the one tests/test_tracker_run.py::test_run_snapshots_the_open_tickets_once_and_lands_a_pr_that_closes_them_through_the_specs_remote[github] checks.
- test_output_off_the_schema_or_without_the_sections_is_a_failure_and_the_next_attempt_may_pass -> F: Attempts 2 and 3 fail `invalid_output` with the same two reasons; a passing Attempt after failed ones is in tests/test_describer.py::test_describer_role_checks_only_the_headings_and_words_it_requires_and_the_run_freezes_them.
- test_describer_that_hangs_is_killed_at_its_timeout_and_the_pr_opens_with_the_engines_text -> F: Attempt 6 hangs and fails `timeout` with `no result after 60s`.
- test_title_over_several_lines_or_with_space_around_it_fails_the_attempt -> F: Attempts 4 and 5 fail `invalid_output` with `one line` in the reason.
- test_describer_sandbox_left_behind_is_journaled_with_its_reason -> F: each of the 6 Attempts journals DescriberSandboxLeftBehind with the same reason, and no DescriberSandboxRemoved or DescriberCleanupFailed.
- test_pipeline_without_a_describer_keeps_the_engines_text -> tests/test_tracker_run.py::test_run_snapshots_the_open_tickets_once_and_lands_a_pr_that_closes_them_through_the_specs_remote[github]: a Pipeline without a Describer opens the PR with the same engine's title and body.
- test_local_run_without_a_describer_writes_the_engines_text_as_the_artifact -> F: the Tracker's own text is pull_request.md of a local Run, `Lands Spec Greeting from Run <id>.` with the Merged list.
- test_fallback_title_names_the_tickets_the_run_merged -> F: pull_request_title.txt is `Greeting: Greet`, from t1's front matter title.
- test_local_run_writes_the_pr_text_artifact_without_opening_a_pr -> L: pull_request.md is the Describer's body with the Merged list, there is no gh call and no remote branch.
- test_local_run_keeps_the_describers_title_next_to_the_pr_text -> L: pull_request_title.txt holds the Describer's title next to pull_request.md.
- test_ticket_named_describe_does_not_meet_the_describers_workspace_sandbox_or_slot -> L: same Tickets `describe` and `describer`, same request stage, Sandbox and Workspace of the Describer and the Ticket Sandboxes.
- test_describer_role_that_requires_no_headings_opens_the_pr_with_its_own_body_and_title -> L: the Role without `body` lets the template body with none of the default headings through on its first Attempt, with its own title.
  The engine's footer under a Describer's body on GitHub is checked by B.
- test_describer_sandbox_that_does_not_stop_is_journaled_and_the_pr_still_opens -> L: one DescriberCleanupFailed with `did not stop`, no DescriberSandboxRemoved, and the Run ends with the Describer's text.
- test_describer_holds_a_slot_of_the_machine_while_it_works -> tests/test_describer.py::test_describer_waits_for_a_slot_that_other_runs_hold_and_holds_one_while_it_works: the Describer's agent sees only its own `/describer` slot and no slot is held after the Run; the machine now has 2 slots, which the other Runs hold until the Describer waits.
- test_describer_waits_for_a_slot_that_other_runs_hold -> renamed test_describer_waits_for_a_slot_that_other_runs_hold_and_holds_one_while_it_works, same assertions plus those of the slot-holding test.
- test_describer_role_checks_only_the_headings_and_words_it_requires -> tests/test_describer.py::test_describer_role_checks_only_the_headings_and_words_it_requires_and_the_run_freezes_them: same failure reasons and the third Attempt's title, now read from pull_request_title.txt of a local Run.
- test_run_freezes_the_body_rules_of_its_describer_role_for_resume -> same merged test: the Run's frozen Library holds the Role's BodyRules.

### test_commands.py
- test_cancel_abandons_a_ticket_at_work_and_its_dependents_never_start -> tests/test_commands.py::test_cancel_abandons_a_ticket_at_work_its_dependents_never_start_and_the_pr_names_them: same cancel of t1 at work, the Command's reply, the `partial` status rows, claims, the `cancelled` AttemptLost with its patch, free Slots, RunFinished and the Integration branch without half.txt; the Spec now has four Tickets and the owner waits for t1's own Attempt.
- test_pr_names_the_cancelled_ticket_and_what_it_blocked -> same merged test: same four-Ticket Spec and the same `Not landed` list at the end of the recorded PR.
- test_cancel_takes_a_queued_ticket_out_before_it_starts -> tests/test_commands.py::test_cancel_takes_a_queued_ticket_out_and_is_rejected_for_a_merged_ticket_and_an_unknown_one: with `max_parallel` 1 the owner cancels the queued t3, which is never claimed and gets no Workspace, then cancels t2 at work; both end `abandoned`.
- test_cancel_is_rejected_for_a_merged_ticket_and_an_unknown_one -> same merged test: same rejections and messages for the merged t1 and the unknown t9.
- test_cancel_of_a_ticket_that_waits_for_the_owner_takes_its_question_back -> tests/test_commands.py::test_cancel_and_override_of_tickets_that_wait_for_the_owner_take_the_question_back_or_decide_it: t2 waits for the owner and is cancelled; no question is left waiting and its work is under its abandoned ref.
- test_override_of_a_waiting_decision_decides_it -> same merged test: the last `override t1 accept` decides the waiting Decision, hello.txt lands and DecisionOverridden ends with `accept`.
- test_override_sends_the_ticket_back_to_work_only_with_reasons_and_only_from_the_outcomes_of_the_decision -> same merged test: same five `override` Commands on t1 and t9 with the same codes and messages.
- test_override_is_rejected_for_a_ticket_without_a_decision -> tests/test_commands.py::test_override_and_limits_are_rejected_when_the_ticket_made_no_decision_or_the_values_do_not_fit: same rejection while implement works.
- test_limits_are_rejected_for_a_stage_without_a_round_limit_a_missing_stage_and_no_values -> same merged test: same four `limits` refusals with their codes and messages.
- test_limits_cannot_raise_parallelism_past_a_merge_without_a_sync -> same merged test: the Pipeline has no `sync`, and `limits --max-parallel 2` is rejected with the same message.
- test_limits_raise_the_parallelism_of_a_run_on_the_go -> tests/test_commands.py::test_limits_raise_the_parallelism_and_change_the_round_limit_of_a_run_on_the_go: one `limits --max-parallel 2 --rounds fix=2` Command; t2 starts while t1 holds its Attempt, which only a raised parallelism allows, and LimitsChanged records 2.
- test_limits_change_the_round_limit_of_a_stage -> same merged test: each Ticket runs `fix` twice and RoundsExhausted records `fix` after 2 Rounds.

### test_clean.py
- test_without_a_flag_it_prints_the_leftovers_and_changes_nothing -> tests/test_clean.py::test_sandbox_that_does_not_stop_keeps_what_it_works_on_until_clean_removes_it: same plan lines for t1 and no change to branches, worktrees or Journal; the Run now ends `partial`.
- test_sandbox_that_does_not_stop_keeps_the_workspace_and_branch -> same merged test: `clean --yes` while the Sandbox still does not stop keeps it with the same line, the Workspace and the branch.
- test_yes_removes_them_and_journals_each_step_to_its_run -> same merged test: once the Sandbox stops, `clean --yes` removes the Workspaces and branches, journals the three steps and leaves `Nothing to clean`.
- test_workspace_with_uncommitted_changes_is_planned_to_stay_and_changes_nothing -> tests/test_clean.py::test_workspace_with_uncommitted_edits_stays_with_its_branch_through_the_cleanup_and_clean: same `keep Workspace ... uncommitted changes` plan for the uncommitted late.txt, nothing changes.
- test_workspace_with_uncommitted_changes_stays_and_the_output_names_the_reason -> same merged test: `clean --yes` keeps the Workspace, names the reason, keeps late.txt and the branch and journals CleanupFailed `workspace`.
- test_a_run_that_still_runs_is_not_touched -> tests/test_clean.py::test_clean_waits_until_a_run_stops_then_saves_unsaved_work_and_removes_only_the_factorys_leftovers: same `Nothing to clean` while the Run runs and the same `(orphaned)` line once it is stale.
- test_branch_with_unsaved_work_is_planned_with_its_ref_and_changes_nothing -> same merged test: same plan line naming the ref, branch kept, no ref written.
- test_branch_with_unsaved_work_goes_to_its_ref_before_it_is_deleted -> same merged test: same ref, hello.txt under it, AbandonedBranchSaved, step order and output line.
- test_main_the_integration_branch_owner_branches_and_foreign_worktrees_do_not_change -> same merged test: same foreign worktree and owner branch, unchanged after `clean --yes`.
- test_it_removes_the_sandbox_through_the_sbx_cli -> same merged test: the project uses `sbx`, and the same `removed Sandbox` line and `sbx rm` call are checked.
- test_a_sandbox_that_is_already_gone_is_not_a_leftover -> tests/test_cleanup.py::test_merged_tickets_are_cleaned_one_after_another_and_leave_only_the_integration_branch: after the cleanup removed both Sandboxes, `factory clean` prints `Nothing to clean`, SandboxRemoved is journaled and the fake Sandbox refuses `exec`.

### test_cleanup.py
- test_merged_ticket_leaves_no_branch_workspace_or_sandbox -> tests/test_cleanup.py::test_merged_tickets_are_cleaned_one_after_another_and_leave_only_the_integration_branch: same checks for t1 and t2: no branch, Workspace or its parent, no Sandbox, the three cleanup events.
- test_cleanup_keeps_the_integration_branch_owner_branches_and_run_dir -> same merged test: same owner branches, one new Integration branch with hello.txt, each Attempt's output.json and attempt.json and spec.md.
- test_each_cleanup_step_is_journaled_with_what_it_removed -> same merged test: same SandboxRemoved, WorkspaceRemoved and TicketBranchDeleted payloads; the deleted commit is checked to be in the Integration branch, since t1's is no longer its head.
- test_tickets_are_cleaned_one_after_another -> same merged test: TicketBranchDeleted for t1 then t2 and only the main worktree left.
- test_removing_the_sandbox_stops_what_the_agent_left_running -> same merged test: t1's agent leaves `sleep 60` running and the process is gone after the Run.
- test_workspace_with_uncommitted_edits_stays_with_its_branch -> tests/test_clean.py::test_workspace_with_uncommitted_edits_stays_with_its_branch_through_the_cleanup_and_clean: same checks for the merged t1: late.txt, Workspace, branch, SandboxRemoved then CleanupFailed `workspace` and the `merged` row; the Run ends `partial` because t2 is abandoned.
- test_abandoned_workspace_with_uncommitted_edits_stays_and_the_outcome_holds -> same merged test: same checks for the abandoned t2, its ref at its branch head, the `partial` Run line and the `abandoned` row.
- test_sandbox_that_does_not_stop_keeps_the_workspace_it_works_on -> tests/test_clean.py::test_sandbox_that_does_not_stop_keeps_what_it_works_on_until_clean_removes_it: same CleanupFailed `sandbox` payload, Workspace and branch for t1.
- test_failed_cleanup_of_one_ticket_does_not_stop_the_next -> same merged test: t2 is blocked by t1, both get CleanupFailed in order and their rows are `merged` and `abandoned`.
- test_sandbox_that_does_not_stop_does_not_change_the_abandoned_outcome -> same merged test: t2 keeps its Workspace, branch and abandoned ref, and its only CleanupFailed step is `sandbox`.
- test_abandoned_ticket_keeps_its_head_under_a_ref_and_leaves_nothing_else -> tests/test_cleanup.py::test_abandoned_tickets_keep_their_work_under_a_ref_named_by_status_and_the_pr_and_leave_nothing_else: same checks for t1; the Integration branch, which t3 now merges into, is checked to lack hello.txt in place of being equal to main.
- test_abandoned_ticket_cleanup_is_journaled_step_by_step -> same merged test: same cleanup events, AbandonedBranchSaved and TicketBranchDeleted payloads for t1.
- test_status_names_the_ref_that_holds_an_abandoned_ticket -> same merged test: the status line naming t1's ref.
- test_ticket_abandoned_without_a_commit_gets_no_ref_and_is_still_cleaned_up -> same merged test: t2 commits nothing, gets AbandonedWithoutWork at main, no ref and no status line, and is cleaned up.
- test_pr_says_that_the_ref_of_an_abandoned_ticket_is_local -> same merged test: t3 merges, so the PR lists t1 with the same local-ref line.
- test_abandoned_ticket_is_cleaned_up_through_the_sbx_cli -> tests/test_clean.py::test_clean_waits_until_a_run_stops_then_saves_unsaved_work_and_removes_only_the_factorys_leftovers: removal through `sbx rm` is checked there, and the Sandbox adapter it calls is the same for both cleanups; tests/test_sbx_sandbox.py covers the sbx Sandbox of a Run.
- test_discarded_patch_of_a_failed_attempt_survives_the_cleanup -> tests/test_attempt_failures.py::test_failed_attempts_are_replaced_with_the_same_brief_and_their_changes_go_to_a_patch: it reads the discarded.patch of failed Attempts after the Run's cleanup removed the Workspace; the test took no branch of its own.

### test_status_timeline.py
- test_timeline_lists_every_node_entry_with_its_outcome_and_the_cost_of_the_run -> tests/test_status_timeline.py::test_timeline_lists_every_node_entry_with_its_outcome_and_the_cost_and_plain_status_keeps_the_ticket_table: same timeline rows.
- test_status_without_timeline_keeps_the_ticket_table -> same merged test: same table header, from the same Run.

### test_status_waits.py
- test_status_says_a_queued_ticket_waits_for_its_unmerged_blockers -> tests/test_status_waits.py::test_status_says_a_queued_ticket_waits_for_its_unmerged_blockers_or_for_a_slot: same chain 38, 37, 42 and the same lines.
- test_status_says_a_ticket_with_merged_blockers_waits_for_a_slot -> same merged test: with `max_parallel` 1 the unblocked b waits for a Slot.

### test_run_view_osc.py
- test_a_terminal_that_answers_the_probe_gets_the_tree_as_osc_7501_records -> tests/test_run_view_osc.py::test_a_terminal_that_answers_the_probe_gets_the_tree_as_osc_7501_records_and_a_human_decision_blocks_it: same probe order, root and Ticket records and text tree, on a Run that also asks a human.
- test_a_human_decision_blocks_the_ticket_and_the_root_with_a_question -> same merged test: same blocked records with `question` and no BEL.
- test_a_run_without_a_tty_sends_no_probe -> tests/test_cleanup.py::test_merged_tickets_are_cleaned_one_after_another_and_leave_only_the_integration_branch: that Run has no terminal on stdin and its output holds no `\x1b]7501`.

### test_telemetry.py
- test_attempt_started_writes_the_fingerprint_and_run_started_the_runs -> tests/test_telemetry.py::test_attempts_write_their_fingerprint_stats_change_size_failures_and_workspace_violations: same fingerprint checks on the first implement Attempt.
- test_attempt_finished_writes_the_stats_and_leaves_unknown_what_the_stream_lacks -> same merged test: same stats of the crashed and the done Attempt.
- test_change_size_is_written_at_the_stage_boundary_and_at_the_merge -> same merged test: same change size at the boundary and at the merge.
- test_a_rate_limit_pause_is_counted_as_waiting_for_the_pause_and_not_for_a_slot -> same merged test: the first Attempt is rate limited; same wait_pause_s and wait_slot_s, and failures now hold the crash too.
- test_a_ticket_finished_by_hand_and_a_workspace_violation_show_in_its_metrics -> same merged test: same read-only `review` Stage writing sneaky.txt, one workspace violation and by_hand False.
- test_stats_of_an_unknown_run_is_an_error -> same merged test: `factory stats nope` exits 2 with the same message.
- test_model_in_the_fingerprint_is_the_model_after_escalate -> tests/test_telemetry.py::test_model_in_the_fingerprint_follows_escalate_and_gate_failures_and_convergence_are_counted: same models per Round.
- test_gate_failures_and_convergence_are_counted -> same merged test: the same Run converges after two fix Rounds; gates, attempts, rounds, fix_rounds and first-pass are checked for it.
- test_the_fingerprint_follows_the_prompt_and_the_pipeline_files_and_not_the_run -> tests/test_telemetry.py::test_fingerprint_follows_the_prompt_and_stats_compare_its_variants_by_median_and_p90: the first two Runs share a prompt and the fourth changes it; same sha checks.
- test_stats_by_prompt_groups_attempts_with_n_median_and_p90 -> same merged test: two prompt rows with n 3 and 2, their median and p90, requests, rounds, first-pass and cache hit.
- test_stats_pools_attempts_of_the_same_variant_into_percentiles -> same merged test: the same five costs 0.1 to 0.5 pool by model into p50 0.3 and p90 0.46.
- test_stats_text_prints_the_runs_and_their_stages_and_by_adds_the_column -> same merged test: same text checks for the first and the last Run.
- test_stats_json_without_by_has_the_run_its_stages_tickets_and_attempts -> same merged test: same JSON of the first Run, whose cost is now 0.1.

### test_library.py

- test_a_missing_layer_is_empty -> tests/test_library.py::test_each_item_comes_from_the_highest_layer_that_has_it_and_an_unknown_role_is_in_none: the user Layer does not exist, `factory check` passes and the prompt falls through it to factory.
- test_project_replaces_user_and_factory -> same test: Role r in project replaces Role r in factory, which names another prompt, and the project schema replaces the factory one whole.
- test_lookup_falls_through_to_lower_layers -> same test: prompt p is only in factory and is found through the missing user Layer.
- test_prompt_extends_the_prompt_below_it -> tests/test_library.py::test_prompt_and_schema_extend_the_ones_below_them_and_another_item_resolves_from_its_own_layer_down: same three-Layer prompt chain, text and parts line.
- test_schema_extends_by_merging_properties_and_required -> same test: same schema extension, property and required sources, kept `additionalProperties` and no `x-extends`.
- test_an_extension_naming_another_item_resolves_from_its_own_layer -> same test: the project prompt q extends shared, which comes from user and not factory.
- test_an_extension_naming_another_item_skips_the_layers_above_its_own -> same test: the user prompt shared extends base, which comes from factory and not project.
- test_an_extension_cycle_names_the_layers -> tests/test_library.py::test_check_names_the_layers_of_every_broken_item: same two cycle messages.
- test_a_missing_base_names_the_layers -> same test: same message for a user prompt that extends its own name with nothing below.
- test_a_conflicting_property_names_the_layers -> same test: same message for schema s.
- test_a_conflicting_schema_key_names_the_layers -> same test: same message for schema t.
- test_a_broken_role_fails_check_even_when_an_upper_layer_replaces_it -> same test: same message for the factory Role r that the project Role r replaces.
- test_show_of_an_unknown_role_is_an_error -> tests/test_library.py::test_each_item_comes_from_the_highest_layer_that_has_it_and_an_unknown_role_is_in_none: `--show nope` gives the same single error.

### test_run_library.py

- test_a_prompt_edited_between_run_and_resume_does_not_reach_the_resumed_run -> tests/test_run_library.py::test_a_prompt_and_a_skill_edited_between_run_and_resume_do_not_reach_the_resumed_run: same crashed Run and resume, same prompt assertions on both Attempts.
- test_a_skill_edited_between_run_and_resume_does_not_reach_the_resumed_run -> same test: the project skill extra is edited in the same gap, and the resumed Attempt's kit holds the first version.
- test_a_skill_of_the_project_layer_replaces_the_kit_skill_under_hermetic -> tests/test_run_library.py::test_the_fingerprint_and_the_kit_follow_the_final_prompt_schema_and_skills_of_the_layers: the last Run is hermetic and asserts the same kit files and the frozen review skill.
- test_a_skill_of_the_project_layer_replaces_the_kit_skill_no_role_names -> same test: no Role names review, and the project review skill still replaces the kit one.
- test_the_fingerprint_follows_the_final_prompt_schema_and_skills_of_the_layers -> same test: a schema change moves only schema_sha, a prompt extension moves only prompt_sha, a new prompt text moves prompt_sha again, and kit_sha stays put through them.
- test_the_fingerprint_of_the_kit_follows_the_skills_of_the_layers -> same test: adding the project review skill moves kit_sha.

### test_layers.py

- test_cycle_check_fails_on_a_ring -> tests/test_layers.py::test_cycle_check_finds_a_ring_but_not_a_diamond: same ring fixture and result.
- test_cycle_check_passes_on_a_diamond -> same test: same diamond fixture and result.
- test_layer_check_fails_on_an_import_against_the_rules -> tests/test_layers.py::test_layer_check_names_an_import_against_the_rules_and_a_module_in_no_group: same journal import of pipeline and message.
- test_layer_check_fails_on_a_module_in_no_group -> same test: same newcomer module and message.

### test_factory_layer.py

- test_one_axis_review_output_needs_no_axes -> tests/test_factory_layer.py::test_pipeline_with_a_one_axis_review_is_assembled_from_the_factory_layer_alone: same Pipeline passes `factory check`, and `--show reviewer-single` shows the same chain and no axes.
- test_schemas_chain_through_one_base_from_merge_danger_to_reviewer_single_to_reviewer -> same test: `--show reviewer` on the same factory Layer shows the same three-schema chain and the door and axes properties.
- test_self_has_no_pipelines_directory_and_keeps_its_pipeline_in_the_project_layer -> tests/test_factory_layer.py::test_self_keeps_only_its_pipeline_and_its_prompt_extensions_in_the_project_layer: same layout and start assertions.
- test_self_extends_the_factory_prompts_and_declares_optional_reads_for_its_describer -> same test: same Describer prompt, prompt extensions and schema assertions.

### test_wiring.py

- test_check_rejects_an_unknown_executor_and_lists_the_known_ones -> tests/test_wiring.py::test_check_rejects_an_unknown_kind_of_every_port_and_lists_the_known_ones: same executor message in factory.yaml.
- test_check_rejects_an_unknown_executor_of_a_stage_and_of_defaults -> same test: same defaults and Stage messages in pipeline.yaml.
- test_check_rejects_an_unknown_sandbox_kind_and_workbench_and_lists_the_known_ones -> same test: same sandbox.kind and workbench messages.
- test_check_rejects_a_stage_executor_other_than_the_projects -> tests/test_wiring.py::test_check_rejects_a_stage_executor_other_than_the_projects_and_sandbox_fields_its_kind_cannot_take: same Stage executor message, with the fake Executor registered on a copy of the registry instead of through its private dict.
- test_claude_code_named_as_the_executor_runs_like_no_executor -> tests/test_wiring.py::test_claude_code_checks_like_no_executor_and_a_fake_kind_of_every_port_runs_through_check_and_run: same two checks with the same output, and the registry still makes a ClaudeCodeExecutor.
- test_a_fake_kind_of_every_port_runs_through_check_and_run -> same test: same fake kinds, check and Run, with the kinds registered on copies of the registries instead of through their private dict.
- test_check_names_the_kind_that_a_field_applies_to -> same test: template and cpus on a kind that does not take them give the same two messages.
- test_check_rejects_egress_on_a_kind_that_cannot_enforce_it -> same test: same egress message for kind open.

### test_version.py

- test_version_prints_the_installed_package_version -> tests/test_version.py::test_version_prints_the_installed_package_version_and_help_lists_it: same exact version line.
- test_help_lists_version -> same test: `--help` lists `--version`.
