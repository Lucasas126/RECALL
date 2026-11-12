# RECALL development guidelines

## Project context

- RECALL is a Python 3.12+ framework for per-user, per-agent assistants with memory and tools.
- Before changing behavior, read the relevant design pages: [architecture](docs/architecture/overview.md) for layer boundaries and channels, configuration ([settings.yaml](docs/reference/settings-yaml.md), [user.yaml](docs/reference/user-yaml.md), [agent.yaml](docs/reference/agent-yaml.md)) for settings and ownership, [sources](docs/architecture/sources.md) for data access, [agents](docs/architecture/agents-and-users.md) for orchestration, and [tools](docs/reference/tools.md) for tool behavior. The full map is [docs/README.md](docs/README.md). Read only the pages needed for the task, not all documentation on every request.
- Check each document's implementation/status notes and the current code. Some documented features are planned, not implemented; do not present or build on a planned feature as if it already exists.
- Keep the data plane scoped to the requesting user and agent. Route memory access through `SourceRegistry` and source APIs; do not bypass their path validation, isolation, or overlay behavior with direct filesystem access.
- Keep capabilities independent of orchestration and pass dependencies explicitly. Avoid adding global mutable state.

## Working in the repository

- Preserve unrelated work in the worktree. Inspect `git status` before editing and do not overwrite existing user changes.
- Keep changes focused. When behavior or configuration changes, update the directly related documentation and examples.
- Agents read their own manual from `src/knowledge/recall/` (the built-in `recall` skill). When you change behavior, config fields, tools or commands, update it too; `tests/test_recall_knowledge.py` enforces part of it.
- Treat each requested feature or change as a ticket. Before implementation, present a practical multi-step action plan, with each step scoped as a coherent commit, and ask for confirmation to begin the whole plan.
- Once the plan is confirmed, implement its steps in order without asking for confirmation between steps. After each step, summarize what changed and suggest a commit message for that step. Do not create commits unless explicitly asked.
- Never add credentials, `.env` contents, private user data, logs, or generated indexes to source control. API keys belong in environment variables; YAML may contain the variable name, never the secret.
- Prefer existing project dependencies and patterns. When dependencies change, update `pyproject.toml` and `uv.lock` using `uv`.
- Raise actionable errors for invalid input or failed operations; do not silently substitute success-shaped defaults.

## Configuration and running

- Framework defaults live in the root `settings.yaml`. User and agent configuration lives under `.config/<user_id>/` and is excluded from Git.
- Put user-level provider settings in `.config/<user_id>/user.yaml` and agent settings in `.config/<user_id>/agents/<agent_id>.yaml`. Agent IDs must belong to the configured user.
- PDF attachments are read locally with PyMuPDF, and scanned pages with the Tesseract executable. Tesseract is a system program that `uv sync` does not install; without it, OCR fails and only PDFs with selectable text work. Install it per the Tesseract OCR section in docs/user-guide/files-and-media.md before testing or reporting OCR behavior.
- Put provider secrets in environment variables (typically loaded from the ignored `.env` file); configure the environment-variable name in user YAML, not the secret itself.
- Install/sync project dependencies with `uv sync`. For the configured CLI, set `USER_ID` and `AGENT_ID` in `.env`, then run `uv run python cli.py` (continues the agent's most recent chat). For the web UI run `uv run python web.py` (host/port in `settings.yaml`).
- The project is in initial development with no production deployment. After implementing a feature, always restart both the web and Telegram services so the changes take effect; no additional restart confirmation is required. Identify and stop only their specific process IDs, preserve their launch configuration, and verify that both services are running and responsive afterward. Report restart or readiness failures explicitly. Documentation-only and instruction-only changes do not require a restart.
- Automated tests in `tests/` use the standard-library `unittest` framework. For Python changes, run the relevant tests with `uv run python -m unittest discover -s tests -p "test_<area>.py"` (replace the pattern with the affected test files), and run `uv run python -m compileall src tests cli.py main.py web.py run_telegram.py`. Exercise relevant behavior manually when it requires configured providers or data; compilation alone does not validate behavior.
- The full suite takes about 160 s. Run it as a background process (`process_start`) instead of relying on the 600 s terminal timeout, and read the result with `process_wait` or the completion note.
- Web UI tests are Node tests: before a push, run every `tests/*.cjs` with `node --test` (ignore `tests/test_model_sorting.cjs`, which already fails), and `node --check src/web/static/app.js` after editing it.
- Each page in `src/knowledge/recall/references/` is limited to 12288 bytes; `tests/test_recall_knowledge.py` enforces it. Trim existing text rather than exceeding it.

## More specific guidance

- Python source conventions: `.github/instructions/python.instructions.md` (Copilot) and `.claude/rules/python.md` (Claude)
- YAML configuration conventions: `.github/instructions/config.instructions.md` (Copilot) and `.claude/rules/config.md` (Claude)
- Documentation conventions: `.github/instructions/documentation.instructions.md` (Copilot) and `.claude/rules/documentation.md` (Claude)
- Keep the Copilot and Claude copies in sync. When you change a file in `.github/instructions/`, make the same change to its `.claude/rules/` counterpart in the same commit, and the reverse. The body after the frontmatter must match exactly. Frontmatter is tool-specific: `applyTo` for Copilot, `paths` for Claude. Keep the two path lists equivalent when you change them.
- `CLAUDE.md` imports this file with `@AGENTS.md`. Put shared project rules here, not in `CLAUDE.md`.
