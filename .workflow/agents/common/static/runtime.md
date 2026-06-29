# Common Agent Runtime Context

Every pipeline agent runs inside a task workspace and must use a small, explicit tool surface.

## Working Folder

- Each agent receives a working folder for the current project or task.
- The working folder is the root for relative file paths.
- Agents must not write outside the working folder.
- Hooky, not the agent, manages workflow state, reports, context snapshots, runtime logs, and `.workflow` artifacts.
- Agents must not write `.workflow` files directly.

## Required Basic Tools

Agents may assume these basic tools exist:

- `read_file(path)`: read a file from the working folder.
- `read_file_excerpt(path, start_line, max_lines)`: read selected line ranges from a file.
- `read_many_files(paths, max_bytes_per_file)`: read several text files with per-file truncation.
- `write_file(path, content)`: write or replace a file in the working folder.
- `list_files(path)`: list directory contents.
- `grep_files(pattern, path)`: search file contents.
- `find_files(glob, path)`: find files by name or glob.
- `detect_project_environment()`: inspect package managers, lockfiles, scripts, languages, and likely test commands.
- `run_tests(command, test_file, test_name, list_only, timeout_seconds)`: run a structured test/check command and save full output to an artifact.
- `capture_visual_snapshot(url, wait_selector, viewport_width, viewport_height, full_page, timeout_seconds)`: capture a browser screenshot and return layout metrics for visual verification.
- `git_status()`: read git working-tree status.
- `git_diff(path, staged, max_bytes)`: read git diff output.
- `git_show(ref, path, max_bytes)`: read a file or object from git.
- `bash(command)`: run shell commands inside the working folder.
- `start_process(command, name, wait_for_url, wait_seconds)`: start a long-running local process such as a development server.
- `read_process(process_id, max_bytes)`: read recent output from a managed process.
- `stop_process(process_id)`: stop a managed process.
- `list_processes()`: list managed processes started during the agent run.
- `web_search(query, max_results, include_domains, exclude_domains)`: search the web for external source material. It requires a configured search provider.
- `fetch_url(url)`: fetch UTF-8 text from an `http` or `https` URL when a task explicitly references external source material.
- `todo_read()`: read the current task todo list.
- `todo_write(items)`: update the current task todo list.

## Mandatory Todo Usage

- Agents must call `todo_read()` before starting substantive work.
- Agents must call `todo_write()` to record the planned steps before making changes.
- Agents must keep the todo list current as work progresses.
- Agents must mark all completed steps before returning a successful final report.
- If blocked, agents must record the blocking item in the todo list before returning.

## Tool Discipline

- Prefer targeted tools over shell commands.
- Prefer `detect_project_environment` before probing package managers, scripts, or test commands.
- Prefer `run_tests` for tests, syntax checks, discovery checks, lint/typecheck commands, and other deterministic verification commands. It stores full output and returns structured pass/fail evidence.
- Prefer `capture_visual_snapshot` when verifying browser UI layout, visual presence, screenshots, or console errors.
- Prefer `git_status`, `git_diff`, and `git_show` for read-only git inspection.
- Prefer `read_file_excerpt` or `read_many_files` over `cat`, `head`, `tail`, or `sed` through bash.
- Prefer `find_files` or `grep_files` over broad shell commands when discovering project context.
- Use `web_search` when source material is named but no URL is provided.
- Use `fetch_url` for referenced source material that is not present in the working folder.
- Use `bash` only when no targeted tool fits the command.
- Use `start_process` for dev servers or other long-running commands needed by tests, then inspect with `read_process` and stop with `stop_process`.
- Do not background long-running servers through shell job control when managed process tools can run them.
- Do not compose shell pipelines only to truncate or parse output; use tool options and artifact paths instead.
- Do not use tools to perform responsibilities assigned to another SDLC agent.
- Do not hide tool failures; include them in the agent report.

## Web Search Configuration

- `web_search` currently supports `WEB_SEARCH_PROVIDER=tavily`.
- Tavily search requires `TAVILY_API_KEY`.
- If web search is not configured, the tool returns a structured error. Agents must not silently fall back to model memory for missing external source material.
