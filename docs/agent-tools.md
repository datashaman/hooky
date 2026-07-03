# Agent Tools

Hooky model roles use a shared tool runtime with role-specific permissions. This
page documents the public tool surface exposed to planner, generator, and
evaluator roles.

## Role Availability

All roles receive the same base runtime, but each role is configured differently.

| Role | Tool intent | Write access |
| --- | --- | --- |
| `planner` | Convert proposal input into initial run artifacts. | Selected run state under `.hooky/runs/<key>/`. |
| `generator` during contract negotiation | Propose done criteria and feature checklist. | `.hooky/runs/<key>/contract.md` and `.hooky/runs/<key>/feature_list.json`. |
| `generator` during implementation | Change implementation and project-native tests. | Workspace files except `.hooky/`. |
| `evaluator` during contract review | Accept or reject contract quality. | No file writes; `final_report` only during contract review. |
| `evaluator` during attempt review | Gather evidence and grade an attempt. | No file writes except tool-owned result artifacts. |

The selected run directory is controlled by `--run-key` or `HOOKY_RUN_KEY`.
Runtime files live under `.hooky/runs/<key>/`.

## Filesystem Tools

- `read_files`
  Reads one or more UTF-8 files with per-file truncation.

- `read_file_excerpt`
  Reads selected line ranges from one UTF-8 file.

- `write_files`
  Writes one or more UTF-8 files. Existing files must be read in the current uncompacted
  context before they can be overwritten. If the file changed after the read, the
  write is rejected until the agent reads it again.

- `edit_files`
  Applies line-oriented edits to one or more existing UTF-8 files. Each edit
  replaces an inclusive line range with exact replacement text; an edit where
  `end_line` is `start_line - 1` inserts text before `start_line`. The full batch
  is validated before any file is written.

- `list_files`
  Lists direct children of a directory.

- `find_files`
  Finds files by glob pattern.

- `search_files`
  Searches UTF-8 files for a literal string.

## Project And Test Tools

- `detect_project_environment`
  Detects language/package-manager hints, scripts, lockfiles, and likely test
  commands.

- `run_tests`
  Runs a project test command and returns structured pass/fail evidence. Full
  output is saved to a tool-result artifact under the selected run directory.

- `run_lint`
  Runs a project lint/type-check command (ruff, eslint, mypy, cargo clippy, go
  vet, or a package.json `lint` script, auto-detected the same way `run_tests`
  detects test commands) and returns pass/fail evidence, with full output
  saved to a tool-result artifact. The generator must call this before
  finishing an implementation attempt; the evaluator must report the result as
  `lint_status` and cannot pass an attempt with `lint_status="issues"`.

- `latest_test_failure_context`
  Builds a concise, file-backed diagnostic bundle from the latest failed
  `run_tests` call and nearby Playwright error-context files.

## Visual Verification Tools

- `capture_visual_snapshot`
  Captures a browser screenshot and returns layout metrics. The evaluator uses
  this for UI evidence. Screenshots and metrics are saved under
  `.hooky/runs/<key>/tool-results/visual-snapshots/`.

## Evidence Tools

Evidence tools write a human-reviewable report owned by Hooky, not by the model.
For attempts, the report lives at `.hooky/runs/<key>/attempts/<id>/evidence.md`.
Before attempts exist, run-level evidence lives at `.hooky/runs/<key>/evidence.md`.

- `append_evidence_note`
  Appends a Markdown note section to `evidence.md`.

- `append_evidence_command`
  Runs a bounded shell command, saves the real output under
  `evidence/command-output/`, and appends command metadata plus output tail to
  `evidence.md`.

- `append_evidence_screenshot`
  Captures a browser screenshot using the visual snapshot tool and appends the
  image link plus metrics to `evidence.md`.

## Git Tools

These tools are read-only wrappers around git:

- `git_status`
- `git_diff`
- `git_show`

They are preferred over ad hoc shell commands when the agent needs repository
state or diffs.

## Shell And Process Tools

- `bash`
  Runs a bounded shell command in the working folder. The runtime blocks common
  long-running server commands and background commands; agents should use
  `start_process` for servers.

- `start_process`
  Starts a managed long-running process, such as a dev server. It can take an
  explicit `port`, auto-allocate a port, and optionally wait for a URL.

- `read_process`
  Reads recent stdout/stderr from a managed process.

- `stop_process`
  Stops a managed process.

- `list_processes`
  Lists managed processes and their detected ports/URLs.

## Web Tools

- `web_search`
  Searches the web and returns normalized result metadata.

- `fetch_url`
  Fetches UTF-8 text content from an HTTP or HTTPS URL.

## Skill Tools

When skills are available, agents can progressively disclose them:

- `activate_skill`
  Loads one skill's `SKILL.md` instructions and lists its optional resources.

- `read_skill_resource`
  Reads a file resource from an already activated skill directory.

## Runtime Control Tools

- `request_time_extension`
  Requests a bounded runtime extension when recent tool evidence shows useful
  progress and a concrete next step remains. Extensions are available only when
  enabled by runtime configuration.

- `todo_read`
  Reads the current todo list.

- `todo_write`
  Replaces the current todo list. Agents are expected to use todos for
  substantive work.

- `final_report`
  Finishes the role invocation with the typed output contract required for that
  role.

## Guardrails

- `.hooky` is system-owned. Agents only get explicit access to the selected run
  directory and tool-result artifacts.
- Implementation agents must not edit `.hooky`.
- Evaluator roles do not edit implementation or test files.
- Existing files require a current read before overwrite.
- Shell commands that look like persistent servers or background jobs are
  rejected by `bash`; use `start_process`.
- Tool outputs that can become large are written to files and returned as paths
  or truncated summaries.
