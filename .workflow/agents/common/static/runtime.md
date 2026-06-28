# Common Agent Runtime Context

Every pipeline agent runs inside a task workspace and must use a small, explicit tool surface.

## Working Folder

- Each agent receives a working folder for the current project or task.
- The working folder is the root for relative file paths.
- Agents must keep generated artifacts inside the folders allowed for their stage.
- Agents must not write outside the working folder.
- Agents must report the working folder they used in their structured output or generated context snapshot.

## Required Basic Tools

Agents may assume these basic tools exist:

- `read_file(path)`: read a file from the working folder.
- `write_file(path, content)`: write or replace a file in the working folder.
- `list_files(path)`: list directory contents.
- `grep_files(pattern, path)`: search file contents.
- `find_files(glob, path)`: find files by name or glob.
- `bash(command)`: run shell commands inside the working folder.
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

- Prefer `find_files` or `grep_files` over broad shell commands when discovering project context.
- Use `web_search` when source material is named but no URL is provided.
- Use `fetch_url` for referenced source material that is not present in the working folder.
- Use `bash` for deterministic local commands such as tests, formatters, and static checks.
- Do not use tools to perform responsibilities assigned to another SDLC agent.
- Do not hide tool failures; include them in the agent report.

## Web Search Configuration

- `web_search` currently supports `WEB_SEARCH_PROVIDER=tavily`.
- Tavily search requires `TAVILY_API_KEY`.
- If web search is not configured, the tool returns a structured error. Agents must not silently fall back to model memory for missing external source material.
