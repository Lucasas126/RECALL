# RECALL development guidelines

## Project context

- RECALL is a Python 3.12+ framework for per-user, per-agent assistants with memory and tools.
- Before changing behavior, read the relevant design pages: [architecture](docs/architecture.md) for layer boundaries and channels, [configuration](docs/config.md) for settings and ownership, [sources](docs/sources.md) for data access, [agents](docs/agents.md) for orchestration, and [tools](docs/tools.md) for tool behavior. Read only the pages needed for the task, not all documentation on every request.
- Check each document's implementation/status notes and the current code. Some documented features are planned, not implemented; do not present or build on a planned feature as if it already exists.
- Keep the data plane scoped to the requesting user and agent. Route memory access through `SourceRegistry` and source APIs; do not bypass their path validation, isolation, or overlay behavior with direct filesystem access.
- Keep capabilities independent of orchestration and pass dependencies explicitly. Avoid adding global mutable state.

## Working in the repository

- Preserve unrelated work in the worktree. Inspect `git status` before editing and do not overwrite existing user changes.
- Keep changes focused. When behavior or configuration changes, update the directly related documentation and examples.
- Treat each requested feature or change as a ticket. Before implementation, present a practical multi-step action plan, with each step scoped as a coherent commit, and ask for confirmation to begin.
- Implement one plan step at a time. After each step, summarize what changed, suggest a commit message for that step, and ask whether to continue or stop. Do not create commits unless explicitly asked.
- Never add credentials, `.env` contents, private user data, logs, or generated indexes to source control. API keys belong in environment variables; YAML may contain the variable name, never the secret.
- Prefer existing project dependencies and patterns. When dependencies change, update `pyproject.toml` and `uv.lock` using `uv`.
- Raise actionable errors for invalid input or failed operations; do not silently substitute success-shaped defaults.

## Configuration and running

- Framework defaults live in the root `settings.yaml`. User and agent configuration lives under `.config/<user_id>/` and is excluded from Git.
- Put user-level provider settings in `.config/<user_id>/user.yaml` and agent settings in `.config/<user_id>/agents/<agent_id>.yaml`. Agent IDs must belong to the configured user.
- Put provider secrets in environment variables (typically loaded from the ignored `.env` file); configure the environment-variable name in user YAML, not the secret itself.
- Install/sync project dependencies with `uv sync`. For the configured CLI, set `USER_ID` and `AGENT_ID` in `.env`, then run `uv run python cli.py` (continues the agent's most recent chat). For the web UI run `uv run python web.py` (host/port in `settings.yaml`).
- The project is in initial development with no production deployment. After implementing a feature, always restart both the web and Telegram services so the changes take effect; no additional restart confirmation is required. Identify and stop only their specific process IDs, preserve their launch configuration, and verify that both services are running and responsive afterward. Report restart or readiness failures explicitly. Documentation-only and instruction-only changes do not require a restart.
- Automated tests in `tests/` use the standard-library `unittest` framework. For Python changes, run the relevant tests with `uv run python -m unittest discover -s tests -p "test_<area>.py"` (replace the pattern with the affected test files), and run `uv run python -m compileall src tests cli.py main.py web.py run_telegram.py`. Exercise relevant behavior manually when it requires configured providers or data; compilation alone does not validate behavior.

## More specific guidance

- Before creating or changing Python source or tests, read the [Python conventions](.github/instructions/python.instructions.md).
- Before changing YAML settings or configuration examples, read the [configuration conventions](.github/instructions/config.instructions.md).
- Before changing project documentation, read the [documentation conventions](.github/instructions/documentation.instructions.md).
