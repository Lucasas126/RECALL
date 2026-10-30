# RECALL development guidelines

## Project context

- RECALL is a Python 3.12+ framework for per-user, per-agent assistants with memory and tools.
- Read the relevant pages in `docs/` before changing behavior. In particular, `docs/architecture.md`, `docs/config.md`, `docs/sources.md`, `docs/agents.md`, and `docs/tools.md` describe the intended design.
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
- Install/sync project dependencies with `uv sync`. For the configured CLI, set `USER_ID` and `AGENT_ID` in `.env`, then run `uv run python cli.py`.
- The repository currently has no automated test suite configured. For Python changes, run `uv run python -m compileall src cli.py main.py`; exercise the relevant behavior manually when it requires configured providers or data.

## More specific guidance

- Python source conventions: `.github/instructions/python.instructions.md`
- YAML configuration conventions: `.github/instructions/config.instructions.md`
- Documentation conventions: `.github/instructions/documentation.instructions.md`
