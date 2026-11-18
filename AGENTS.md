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
- Agents read their own manual from `src/knowledge/recall/` (the built-in `recall` skill). When you change behavior, config fields, tools or commands, update it too; `tests/knowledge/test_recall_knowledge.py` enforces part of it.
- Never add credentials, `.env` contents, private user data, logs, or generated indexes to source control. API keys belong in environment variables; YAML may contain the variable name, never the secret.
- Prefer existing project dependencies and patterns. When dependencies change, update `pyproject.toml` and `uv.lock` using `uv`.
- Raise actionable errors for invalid input or failed operations; do not silently substitute success-shaped defaults.

## Configuration and running

- Framework defaults live in the root `settings.yaml`. User and agent configuration lives under `.config/<user_id>/` and is excluded from Git.
- Put user-level provider settings in `.config/<user_id>/user.yaml` and agent settings in `.config/<user_id>/agents/<agent_id>.yaml`. Agent IDs must belong to the configured user.
- PDF attachments are read locally with PyMuPDF, and scanned pages with the Tesseract executable. Tesseract is a system program that `uv sync` does not install; without it, OCR fails and only PDFs with selectable text work. Install it per the Tesseract OCR section in docs/user-guide/files-and-media.md before testing or reporting OCR behavior.
- Put provider secrets in environment variables (typically loaded from the ignored `.env` file); configure the environment-variable name in user YAML, not the secret itself.
- Install/sync project dependencies with `uv sync`. For the configured CLI, set `USER_ID` and `AGENT_ID` in `.env`, then run `uv run python cli.py` (continues the agent's most recent chat). For the web UI run `uv run python web.py` (host/port in `settings.yaml`).
- Automated tests in `tests/` use the standard-library `unittest` framework. For Python changes, run the relevant tests with `uv run python -m unittest discover -s tests -p "test_<area>.py"` (replace the pattern with the affected test files), and run `uv run python -m compileall src tests cli.py main.py web.py run_telegram.py`. Exercise relevant behavior manually when it requires configured providers or data; compilation alone does not validate behavior.
- Tests are grouped by domain in `tests/<domain>/` (see `tests/README.md`): put a new test file in the folder of the code it exercises, and shared helpers in `tests/support/`. Import helpers and other test modules with package paths (`from tests.support.auth_helpers import login`). `discover -s tests` still works because every folder is a package; never name a test folder like an installed library (`telegram`, `yaml`, `mcp`...), it would shadow it.
- The full suite takes about 3 minutes; run it in the background when your tool has a command timeout.
- Web UI tests are Node tests (`tests/web/js/*.cjs`). `tests/web/js/test_node_suite.py` runs all of them inside the Python suite when `node` is installed; to run one alone use `node --test --test-timeout=20000 tests/web/js/<file>.cjs`. Run `node --check src/web/static/app.js` after editing it. When you change the UI the tests exercise (model picker, settings), update the matching `.cjs` test in the same commit.
- Each page in `src/knowledge/recall/references/` is limited to 12288 bytes; `tests/knowledge/test_recall_knowledge.py` enforces it. Trim existing text rather than exceeding it.

## More specific guidance

### Python

- Follow the existing style: four-space indentation, `snake_case` functions and variables, `PascalCase` classes, and type annotations on public APIs and non-obvious values.
- Write clear, concise docstrings that describe only the documented module, class, or function. Use one line when that is enough; do not document ticket history, feature origin, or obvious implementation details.
- Add comments only to clarify non-obvious logic; do not narrate what the code already expresses.
- Use `pathlib.Path` for paths, UTF-8 for text files, and context managers for opened files. Keep filesystem paths anchored to the owning source rather than assembling unchecked paths in callers.
- Use the existing Pydantic models for validated user/agent configuration. Reuse shared helpers and logger utilities instead of duplicating parsing, path, or logging behavior.
- Keep responsibilities separated: source access belongs in `src/core/sources/`, provider integrations in `src/providers/`, and agent orchestration in `src/agent/`. Pass dependencies explicitly; do not introduce module-level mutable state.
- Use `try`/`except` only when code can handle, translate, or add useful context to an error. Let exceptions propagate across well-defined internal module contracts instead of catching and re-raising them redundantly.
- Report actionable failures using the existing exception and logging patterns. Avoid broad exception catches, silent fallbacks, and logging secrets or private memory content.
- Add or update focused `unittest` tests in `tests/` for changed behavior, following existing fixtures and mocking patterns. Keep tests independent of real provider credentials and private user data. Run the smallest relevant test selection first.

### YAML configuration

- Keep framework settings (`settings.yaml`) separate from user and agent configuration under `.config/`; these have different owners and lifecycles.
- Follow the existing schema and examples in [the configuration reference](docs/README.md#reference) and the [Pydantic models](src/config/models.py). Do not add a YAML option without wiring and validating it in the implementation.
- Use the existing lowercase `snake_case` keys and stable identifiers. Quote values when YAML could interpret them as another type.
- Keep paths and data scoped to the intended user and agent. Do not add shared defaults or implicit cross-agent access unless the design and validation are updated together.
- Keep examples consistent with the implemented behavior; label planned configuration features as planned rather than implying they work today.
- When changing a setting, update its model, loader/runtime use, and the configuration reference together. Validate loading with the project environment when practical.

### Documentation

- Write concise, practical English, matching the existing project documentation. Use lowercase headings where that is already the convention in the surrounding document.
- Explain the behavior developers can verify in code. Clearly separate implemented behavior from planned design, and keep status notes current.
- Prefer updating the existing canonical page over adding a duplicate guide. Link related topics rather than copying their content.
- Use relative Markdown links to existing project files, and verify that their targets and any section anchors exist.
- Keep examples consistent with the current schema, source names, paths, and CLI. Never include real credentials, private user data, or sensitive log excerpts.
- Update documentation alongside changes to externally visible behavior, configuration, setup, or developer workflows.

`CLAUDE.md` imports this file with `@AGENTS.md`. Put shared project rules here, not in `CLAUDE.md`.
