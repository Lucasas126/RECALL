---
description: "Use when creating or changing Python source or unittest tests in RECALL, including the agent, providers, configuration, sources, tools, services, web, Telegram, and CLI."
applyTo: "src/**/*.py,tests/**/*.py,*.py"
---
# Python guidelines

- Follow the existing style: four-space indentation, `snake_case` functions and variables, `PascalCase` classes, and type annotations on public APIs and non-obvious values.
- Write clear, concise docstrings that describe only the documented module, class, or function. Use one line when that is enough; do not document ticket history, feature origin, or obvious implementation details.
- Add comments only to clarify non-obvious logic; do not narrate what the code already expresses.
- Use `pathlib.Path` for paths, UTF-8 for text files, and context managers for opened files. Keep filesystem paths anchored to the owning source rather than assembling unchecked paths in callers.
- Use the existing Pydantic models for validated user/agent configuration. Reuse shared helpers and logger utilities instead of duplicating parsing, path, or logging behavior.
- Keep responsibilities separated: source access belongs in `src/core/sources/`, provider integrations in `src/providers/`, and agent orchestration in `src/agent/`. Pass dependencies explicitly; do not introduce module-level mutable state.
- Preserve per-user/per-agent boundaries. Resolve memory addresses through `SourceRegistry` and use source APIs so path traversal checks, access policy, and any overlay remain effective.
- Use `try`/`except` only when code can handle, translate, or add useful context to an error. Let exceptions propagate across well-defined internal module contracts instead of catching and re-raising them redundantly.
- Report actionable failures using the existing exception and logging patterns. Avoid broad exception catches, silent fallbacks, and logging secrets or private memory content.
- Add or update focused `unittest` tests in `tests/` for changed behavior, following existing fixtures and mocking patterns. Keep tests independent of real provider credentials and private user data.
- Follow the test and compilation commands in [AGENTS.md](../../AGENTS.md#configuration-and-running). Run the smallest relevant test selection first, and manually validate behavior that depends on providers or local data.
