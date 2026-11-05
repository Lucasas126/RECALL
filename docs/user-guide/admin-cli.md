# Admin CLI

`python -m src.admin` is the administrator's command line. It manages users, agents and credentials through the same service,
validation and locks as the web UI, so it is safe to run while the web and Telegram services are up. It is also what the **setup
guide** agent uses (through its terminal), so the commands never print secrets and every change is validated before it is written.

In Docker run it inside a container: `docker compose exec -it recall-web python -m src.admin <command>`. On a plain install run it
from the repository root. Agents with a `local` terminal backend run it the same way, in `/app`; see
[security notes](../deployment/security.md#terminal-sandbox).

| command | purpose |
| --- | --- |
| `create-user`, `set-password` | create a user / replace the web password (hidden prompt or `--generate`); see [user.yaml](../reference/user-yaml.md#creating-users-administrator) |
| `agent list --user U` | the user's agents |
| `agent show --user U --agent A` | the agent yaml as the loader sees it |
| `agent create --user U --id A [--name N] [--from-template default\|guide] [--set path=value ...]` | create an agent (same checks as the web UI) |
| `agent set --user U --agent A --set path=value [--set ...]` | change an agent's settings |
| `agent rename --user U --agent A --new-id B [--name N]` | change the id and move the data folder, like the web UI |
| `agent delete --user U --agent A --yes` | delete the configuration (the data folder stays) |
| `agent check [--user U]` | load everything with the real loader; `OK` or the precise errors, exit code 1 on error |
| `user show --user U` | `user.yaml` with key variable names; never the password hash |
| `user set --user U --set path=value` | non-secret fields (`name`, `language`, `models.*`, `provider.name`, `provider.api_key` = a variable NAME, `terminal_backends.*`, ...) |
| `set-secret NAME [--stdin]` | store a credential in `.config/secrets.env` |
| `secret list` | credential names and whether they are set, never values |
| `terminal-test`, `route-test` | connection check of a backend / Telegram routing preview |

## Setting values

`--set a.b.c=value` addresses a dotted path in the file. The value is parsed as YAML: `true`, `42`, `[memory_read, web]`,
`{type: local, workdir: /app}`, `null` (clears an optional field) or a quoted string. Note that `description=Hello: world` is a map
in YAML; quote it (`description="Hello: world"`). The whole result is validated with the real models before anything is written;
errors are printed as `path: reason` and the files are left untouched. A model reference is checked against the provider catalog
like in the web UI. Rewriting a file drops its YAML comments.

```bash
python -m src.admin agent create --user ana --id guide --from-template guide
python -m src.admin agent set --user ana --agent notes --set chat_config.model=openrouter:vendor/model
python -m src.admin agent set --user ana --agent notes --set chat_config.tool_groups=[memory_read,web]
python -m src.admin agent rename --user ana --agent guide --new-id main --name "Ana's assistant"
python -m src.admin agent check
```

## Templates

`--from-template default` is the plain agent with the default SOUL. `guide` is the setup guide: tool groups `memory_read`,
`memory_edit`, `web`, `self` and `terminal`; a `terminal` block with a backend named `local` (working directory `/app`,
`approvals: manual`); the user's `terminal_backends.local` is added when missing; and a short SOUL that tells it to read the
built-in [RECALL knowledge](recall-knowledge.md), use this CLI instead of editing YAML, run `agent check`, ask before anything
destructive, never read or print secrets and never touch another user's folders. The guide owns its SOUL and can later be renamed
and reshaped into the user's main agent. It only works when the server allows local backends (`RECALL_TERMINAL_ALLOW_LOCAL=true`).

## Changes made from another process

Edits made by the CLI or by hand in another process reach the running web and Telegram services on the **next message**, without a
restart: each service compares the user and agent yaml (and `secrets.env`) with what its cached agent was built from and rebuilds
the agent between replies, never during one. If the file is invalid (a typo made by hand), the service keeps the last good
configuration, logs the error, shows a notice with the reason in the next chat message and keeps listing agents; fix the file and run
`agent check`. `settings.yaml`, `.env` and `RECALL_*` variables still need a restart. The CLI cannot see a reply that is running in
the web process, so rename and delete agents while they are idle.

## Credentials

Provider keys are variables named in `user.yaml` (`provider.api_key`). Besides the environment and `.env`, RECALL reads
`.config/secrets.env` (lines `NAME=value`, mode 0600, never committed) for variables that are not set in the process environment;
the real environment wins. Store a key **yourself** in a terminal:

```bash
docker compose exec -it recall-web python -m src.admin set-secret OPENROUTER_API_KEY    # hidden prompt, asked twice
printf %s "$KEY" | python -m src.admin set-secret OPENROUTER_API_KEY --stdin           # from a pipe
python -m src.admin secret list
```

The value is never an argument, never printed (the command prints `saved NAME`) and the file is re-read when it changes, so a new
or rotated key works on the next message. Terminal output shown to an agent masks every value stored in `secrets.env` that is at
least 8 characters long. **Never paste a secret into a chat.** (A masked input card in the web UI is on the [backlog](../project/backlog.md).)
`secret list` checks its own environment and the file: a key set only in the server's `.env` is hidden from commands run by an agent
and may show as NOT SET.
