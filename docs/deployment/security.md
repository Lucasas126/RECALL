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
- **Terminal sandbox**: see [below](#terminal-sandbox).
- **Backups**: back up `.env`, `config/` (or `.config/`) and `data/`.

## terminal sandbox

The [terminal tool group](../user-guide/terminal.md) lets a model run commands. Treat it as the most powerful tool RECALL has.

**Threat model.** The model reads untrusted text: web pages and search results, MCP responses, files in user folders,
documents and images the user attaches, and the output of its own commands. Any of it can contain *prompt injection*
("run this command to continue..."). The defenses, in order of strength:

1. **The sandbox is the boundary.** Commands run on a different machine/container that holds no RECALL data, no `.env`, no
   provider keys and no credentials for anything else. Assume anything inside can be destroyed or read by the model.
2. **Approvals.** In `manual` mode the user sees every command before it runs; this is the mode that stops a prompt injection
   from acting. `smart` only stops the dangerous list, which is a heuristic, not a guarantee; `off` trusts the model entirely.
   Read the command in the approval before tapping *Approve*; only the chat's owner can answer, and an unanswered approval is
   denied after the timeout.
3. **Hardline blocklist**, enforced in every mode (disk wipes, `rm -rf /`, shutdowns, fork bombs...).
4. **Containment of the transport**: key-only SSH with strict host-key checking, no agent/X11 forwarding, no local shell, a
   minimal local environment for the `ssh` process, no secrets in the command line (file contents go on stdin).
5. **Output hygiene and audit**: output is redacted and truncated before the model sees it; every action is logged to
   `data/<user_id>/<agent_id>/terminal_audit.jsonl`, which the agent cannot read or change.

**Local backend.** `type: local` runs commands on the RECALL host as the RECALL user, with only secret-looking variables
removed from the environment. The process can still read every file that user can read, including `.env`, `config/` and
every user's `data/`. It is disabled by default (`terminal.allow_local`) and only suitable for a single-user machine you
would also trust a shell on. Never enable it on a shared installation.

**Advice for the sandbox.**

- A dedicated unprivileged user; no `sudo`, no Docker socket, no mounts from the host.
- A container/VM with resource limits (memory, PIDs, disk) and `--cap-drop ALL`, `no-new-privileges`.
- Restrict outbound network to what is needed (an egress firewall or a proxy). An unrestricted sandbox can exfiltrate whatever
  it can read, including everything the model put there.
- Keep nothing precious inside; snapshot or rebuild it regularly.
- Use a key pair made for this purpose, passphrase-less, readable only by the RECALL user, mounted read-only into the container.
  Pin the host key in `known_hosts_file`; never disable strict checking.
- One sandbox per user or per trust level; do not share a sandbox between users who should not see each other's files.
- Keep dream use (`allow_in_dream`) for disposable sandboxes: nobody is there to approve anything.

**Privacy.** Commands and their outputs are part of the conversation and go to the model provider. Zero-data-retention routing
applies as for any other message.

