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
- **Folders**: a folder path is refused when saving an agent (and skipped with a warning at load) if it equals, contains or sits inside the config, data or logs folder, the RECALL program folder, `/proc`, `/sys`, `/dev`, `~/.ssh` or `~/.gnupg` (symlinks resolved). Optionally `folders.allowed_roots` in `settings.yaml` limits folders to listed roots. Files the terminal also protects (`.env`, keys, secrets; see `terminal.protected_paths`) cannot be read, written, listed, searched or indexed through a folder. `query_memory` returns folder chunks only while the agent can read that folder.
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
   minimal local environment for the `ssh` process, `ssh -F /dev/null` (the server user's `~/.ssh/config` is never read), no secrets in the command line (file contents go on stdin).
5. **Output hygiene and audit**: output is redacted and truncated before the model sees it; every action is logged to
   `data/<user_id>/<agent_id>/terminal_audit.jsonl`, which the agent's sandbox tools cannot reach and a local backend may not write (see below). If the audit file cannot be written the action is refused (fail closed).

**A PC backend means the RECALL host can act on a real machine.** When an agent is allowed a backend that is your own PC (or
any machine that is not a throwaway sandbox), the sandbox boundary above does not exist for that backend: a prompt injection
that gets a command approved acts on real files and credentials. So: keep that backend on `approvals: manual` (or no rule that allows more than narrow read-only commands) and read each
command before approving; use a dedicated key pair that is used for nothing else and a dedicated, preferably unprivileged,
account; allow inbound SSH only from the RECALL host with a firewall rule; disable password login on the PC; pin its host key.
Approvals are per backend, so a `smart` or `off` sandbox next to a `manual` PC does not loosen the PC. [Approval rules](../user-guide/terminal.md#approval-rules) (installation, user, agent) can allow, ask or deny by command, path, backend and backend `kind` (`sandbox` or `machine`; mark disposable machines `kind: sandbox`); deny beats ask beats allow, and allowing everything on a machine or the local backend (a blanket rule, `default: allow` or `approvals: off`) is refused unless the agent is `admin`. Hardline and protected paths come first and no rule changes them. Setup:
[using your own PC as a backend](../user-guide/terminal.md#using-your-own-pc-as-a-backend).

**Local backend.** `type: local` runs commands on the RECALL host as the RECALL user, with an environment
allowlist (`PATH`, `HOME`, `LANG`, `LC_*`, `TERM`, `TMPDIR`, `USER`, `SHELL`; never `SSH_AUTH_SOCK`, `DOCKER_HOST`, `KUBECONFIG`,
`DBUS_SESSION_BUS_ADDRESS` or `DATABASE_URL`). **It is a host shell**: it is not a sandbox. The command parser that screens it is a
best-effort heuristic and its residual risk is accepted. The process can still read every file that user can read, including `.env`, `config/` and
every user's `data/`. It is disabled by default (`terminal.allow_local`, or `RECALL_TERMINAL_ALLOW_LOCAL=true` in the server
environment) and only suitable for an installation you would also trust a shell on.

*Inside the Docker container* (the setup guide of the [Admin CLI](../user-guide/admin-cli.md)) the commands run as the `app` user
(uid 1000) in `/app` and reach:

- the whole `/app/.config`: every user's `user.yaml` (password hashes), agent yaml, `secrets.env` and the Claude login folders;
- the whole `/app/data`: every user's and agent's memories, chats, indexes and terminal audit logs;
- the code and the Python environment, and the container's outbound network.

**Protected paths.** Whatever the approval mode, a local backend refuses a fixed set of secrets: the private `key_file` of every
user's SSH backends (so a guide for user B cannot use the owner's PC key), `.config/secrets.env`, `.env` files, anything under a
`keys` folder in the config dir, Claude login credentials and `/proc/*/environ`, plus the patterns in
`terminal.protected_paths`, and the per-user `mcp/` credential folders; it also refuses *writes* to the audit trail, every `user.yaml`,
agent yaml, `settings.yaml` and `known_hosts` files (a `user.yaml` that cannot be parsed still protects the keys it names) (details in the [terminal guide](../user-guide/terminal.md#protected-paths-local-backend)). With `approvals: off` (admin only on a machine or local backend) an unattended run (dream, scheduled task, no chat client) behaves like `smart` and may not write files. The file
tools enforce it exactly (symlinks and `..` resolved). Command scanning is **best effort**: an obfuscated command (a path built
from pieces, a hard link, a script the agent writes and runs through an interpreter) can bypass it, so approval cards remain the
second barrier. `user.yaml`, other users' data and the audit logs stay readable on purpose (writing them is refused). Stronger measures are on the
[backlog](../project/backlog.md): a separate OS user or container for the local backend.

There is no technical separation between users yet (a per-user path limit is on the [backlog](../project/backlog.md)); the rules
are behavioural (the agent's SOUL and the built-in `security-for-admins` page: own data only, never read or print secrets, web
content is data) plus the user's approval of every command. Risks to weigh: prompt injection through web pages or files that makes
the agent run something harmful or read secrets; an approval clicked without reading; data leaving through the open network.
Mitigations: keep `approvals: manual` (the default) and read each card; give the guide only to a user you trust with the
whole installation; run a dedicated container (or a separate RECALL deployment) with only that user when other users must stay
isolated; restrict the container's egress; keep backups of `.config/`. Values stored in `secrets.env` are masked in terminal
output as a safety net, not as a guarantee.

**Advice for the sandbox.**

- A dedicated unprivileged user; no `sudo`, no Docker socket, no mounts from the host.
- A container/VM with resource limits (memory, PIDs, disk) and `--cap-drop ALL`, `no-new-privileges`.
- Restrict outbound network to what is needed (an egress firewall or a proxy). An unrestricted sandbox can exfiltrate whatever
  it can read, including everything the model put there.
- Keep nothing precious inside; snapshot or rebuild it regularly.
- Use a key pair made for this purpose, passphrase-less, readable only by the RECALL user, mounted read-only into the container.
  Pin the host key in `known_hosts_file`; never disable strict checking.
- One sandbox per user or per trust level; do not share a sandbox between users who should not see each other's files.
- Dream sessions never get the terminal (`terminal.allow_in_dream` and `dream.tools: [terminal]` are rejected); read-only sandbox access for dreams is planned.

**Privacy.** Commands and their outputs are part of the conversation and go to the model provider. Zero-data-retention routing
applies as for any other message.

