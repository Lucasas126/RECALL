# environment variables

Secrets are never written in YAML: YAML files hold the **name** of the variable. Variables are read from the process
environment and, for the normal entry points, from the ignored `.env` file. Exceptions are marked.

| variable | used for |
| --- | --- |
| `RECALL_SESSION_SECRET` | signs the web session cookie (the name can be changed with `web.session_secret_env`) |
| `<per-user key variable>` | each user's provider key; its name is `provider.api_key` in that `user.yaml` (for example `OPENROUTER_ALICE_API_KEY`) |
| `OPENROUTER_API_KEY` | default name of the shared OpenRouter key used by associative memory and chat naming (`api_key` in `settings.yaml`) and by `openrouter.api_key` |
| `TAVILY_API_KEY` | shared Tavily key for `web_search` / `web_retrieve` (`tavily.api_key`) |
| `TELEGRAM_BOT_TOKEN` | the bot token |
| `TELEGRAM_ALLOWED_USERS` | comma-separated `<telegram_id>=<user_id>` entries (name set by `telegram.allowed_users_env`) |
| `OLLAMA_HOST` | Ollama address when `ollama.host` is not set (default `http://localhost:11434`) |
| `GOOGLE_OAUTH_CREDENTIALS` | path of the Google OAuth client JSON for the Google Calendar MCP server |
| `RECALL_WEB_HOST` | overrides `web.host`; read from the process environment, not from `.env` (Docker sets `0.0.0.0`) |
| `RECALL_PUBLIC_URL` | overrides `web.public_url` (links in Telegram dream notifications); process environment only |
| `RECALL_TELEGRAM_CHAT_IDLE_MINUTES` | overrides `telegram.chat_idle_minutes`: any positive number of minutes, fractions allowed (e.g. `1` to test the idle flow); process environment only |
| `RECALL_TERMINAL_ALLOW_LOCAL` | `true`/`false` (also `1`/`0`, `yes`/`no`, `on`/`off`): overrides `terminal.allow_local`, the switch that accepts `type: local` terminal backends (commands run on this host); process environment only. An empty value keeps the yaml setting; a value that is not a boolean stops the start-up |
| `RECALL_CLAUDE_COMMAND` | path of the `claude` executable ([Claude subscription](../user-guide/claude-subscription.md)) |
| `CLAUDE_CONFIG_DIR` | used by the `claude` CLI itself to choose a login folder; RECALL passes the user's `claude_subscription.claude_config_dir` |
| `USER_ID`, `AGENT_ID` | which user and agent the terminal client `cli.py` opens (read from `.env`) |

**Fallback file `.config/secrets.env`.** Credential variables (the per-user provider keys, `OPENROUTER_API_KEY`, `TAVILY_API_KEY`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, MCP `env` variables) are looked up in the process environment first and, when
the variable is missing or empty there, in `<config_dir>/secrets.env` (`NAME=value` lines, `#` comments; gitignored; written with
mode 0600 by `python -m src.admin set-secret NAME`; see [Admin CLI](../user-guide/admin-cli.md#credentials)). The file is re-read
when it changes, so changing a key needs no restart (the Telegram bot token is read at start-up). `RECALL_*` settings overrides are
not read from this file.

RECALL refuses to start the Claude CLI while `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
`ANTHROPIC_FOUNDRY_API_KEY` or `CLAUDE_CODE_USE_BEDROCK`/`VERTEX`/`FOUNDRY` are set in its environment.

Docker-only values (`GHCR_OWNER`, `RECALL_TAG`, `RECALL_PORT`, `RECALL_CONFIG_DIR`, `RECALL_DATA_DIR`, `RECALL_LOGS_DIR`, `TZ`,
`COMPOSE_PROFILES`, `WATCHTOWER_POLL_INTERVAL`) are explained in [Docker deployment](../deployment/docker.md#environment).
