# security notes

RECALL is built for a household or a small trusted group, not for the open internet. What it does and what you must do:

- **Authentication**: web users are preconfigured with Argon2 password hashes and signed HTTP-only session cookies; there is no
  registration. The cookie is not marked `Secure` and the UI is plain HTTP: keep it on a LAN or VPN, or put an HTTPS reverse
  proxy in front. Do not publish the port.
- **Isolation**: every agent only reaches its own sources through the `SourceRegistry` and its own index namespace; users cannot
  see each other's agents, chats or memories ([agents and users](../architecture/agents-and-users.md)).
- **Secrets**: provider keys live in environment variables / `.env` (kept out of Git, `chmod 600` on a server). YAML only holds
  variable names. MCP credentials are stored per user in `.config/<user_id>/mcp/`.
- **Telegram**: ids that are not in `TELEGRAM_ALLOWED_USERS` are ignored completely; one bot token, one polling process.
- **Claude logins**: the experimental Claude provider uses a login of the account RECALL runs as, or of a per-user
  `claude_config_dir`. Never share one person's login with another; see [Claude subscription](../user-guide/claude-subscription.md).
- **Privacy of model providers**: zero data retention is an optional per-user policy (`require_zdr`), see
  [models and providers](../user-guide/models-and-providers.md#openrouter-model-catalog-and-zero-data-retention). Tavily and
  any MCP server receive what the agent sends them.
- **Files**: user folders are exposed to an agent only when it lists them; `read_only: true` forbids every write; the agent
  can only touch the agent's own files through its `self` tools.
- **Backups**: back up `.env`, `config/` (or `.config/`) and `data/`.
