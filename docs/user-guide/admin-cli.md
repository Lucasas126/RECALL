# Admin CLI

`python -m src.admin` is the administrator's command line. It manages users, agents and credentials through the same service,
validation and locks as the web UI, so it is safe to run while the web and Telegram services are up. It is also what the **setup
guide** agent uses (through its terminal), so the commands never print secrets and every change is validated before it is written.

In Docker run it inside a container: `docker compose exec -it recall-web python -m src.admin <command>`. On a plain install run it
from the repository root. Agents with a `local` terminal backend run it the same way, in `/app`; see
[security notes](../deployment/security.md#terminal-sandbox).

| command | purpose |
| --- | --- |
| `create-user [--max-level L]`, `set-password` | create a user / replace the web password (hidden prompt or `--generate`); `--max-level` sets the [permission ceiling](../reference/user-yaml.md#max_level-permission-levels) (default `collaborator`); see [user.yaml](../reference/user-yaml.md#creating-users-administrator) |
| `agent list --user U` | the user's agents |
| `agent show --user U --agent A` | the agent yaml as the loader sees it |
| `agent create --user U --id A [--name N] [--from-template default\|guide\|permission-manager] [--level L] [--set path=value ...]` | create an agent (same checks as the web UI); `--level` is the same as `--set level=L` |
| `agent set --user U --agent A [--level L] --set path=value [--set ...]` | change an agent's settings; other settings keep a stored `level` |
| `agent rename --user U --agent A --new-id B [--name N]` | change the id and move the data folder, like the web UI |
| `agent delete --user U --agent A --yes` | delete the configuration (the data folder stays) |
| `agent rules --user U --agent A [--clear\|--effective]` | list the agent's own [approval rules](terminal.md#approval-rules) (marking the ones learned from approval cards), with `--clear` remove all of them (the audit file records it with `actor: admin`), or with `--effective` print the installation, user and agent layers, the per-backend `approvals` shorthand and the default that applies (what the web form shows read only) |
| `agent check [--user U]` | load everything with the real loader; prints each agent's effective level, then `OK` or the precise errors (exit code 1 on error), including agents whose level breaks the [level rule](../reference/agent-yaml.md#level) |
| `user list` | every user with `max_level`, the number of agents and the level of each (the data of the web **Users** section) |
| `user show --user U` | `user.yaml` with key variable names; never the password hash |
| `user set --user U --set path=value` | non-secret fields (`name`, `language`, `models.*` (e.g. `models.stt_language=es`, `models.stt_language=null` to clear), `provider.name`, `provider.api_key` = a variable NAME, `terminal_backends.*`, ...) |
| `set-secret NAME [--stdin]` | store a credential in `.config/secrets.env` |
| `secret list` | credential names and whether they are set, never values |
| `task run --user U --agent A --goal "..." [--context C] [--instructions I] [--notify AGENT:CHAT] [--wait]` | start an **unattended** [task](delegation.md#limits) with no parent chat (the entry point future scheduled jobs will use; for testing). It prints `task: <id>`; with `--wait` it blocks and prints the status, tokens and result. Without `--instructions` the task runs as agent `A` itself (its own SOUL, web tools and its own terminal; memory read-only); with them an ephemeral worker runs on `A`'s model. Manual and dangerous terminal actions are denied, `smart` normal ones run. The same limits and the daily token budget apply. `--notify` delivers the result to that chat as a wake; otherwise it only stays in the web Tasks panel. The task runs inside the command, so without `--wait` it stops when the command exits |
| `terminal-test`, `route-test` | connection check of a backend / Telegram routing preview |

## Permission levels

`max_level` (user) and `level` (agent) take `minimal`, `reader`, `assistant`, `collaborator`, `operator` or `admin`
(see [permissions](../architecture/permissions.md)). Only this CLI changes a user's `max_level`; an agent's `level` is changed by the user (web form, `agent set`) up to `max_level`, or by an operator agent with the `permissions` group (never above its own level, and only lowering its own):
`user set --user U --set max_level=operator` (or, for a user whose own maximum is `admin`, the **Users** section of the web settings: [users](settings.md#users-administrators-only)). `agent show` ends with the agent's effective level and how it was obtained. `max_level` is required in `user.yaml` (a user without it does not load). An agent whose level is above it, or below what its
configuration needs, or whose configuration needs more than `max_level`, is refused by `agent create|set` (and by the web) with a
message naming the service and the level needed. `user set` refuses a `max_level` below the level of one of the user's agents (lower
those first).

The `guide` template (`agent create --from-template guide`) is an `operator` agent with the `agents` and `permissions` tool groups and no terminal, so it
needs a user whose `max_level` is at least `operator`: below that the command fails with a message that names the level and the `user set`
command, and writes nothing. It no longer uses the admin CLI or the `local` backend.

The `permission-manager` template (`agent create --from-template permission-manager`) needs a user whose `max_level` is at least
`operator`: below that the command fails with a message that names the level and the `user set` command, and writes nothing.

## Setting values

`--set a.b.c=value` addresses a dotted path in the file. The value is parsed as YAML: `true`, `42`, `[memory_read, web]`,
`{type: local, workdir: /app}`, `null` (clears an optional field) or a quoted string, so wrap text containing `": "` in quotes. The exception
is a plain string field (`description`, `name`, ...): its value is taken raw, so `description=Veterinaria: salud y cuidado` works as typed
(one pair of surrounding quotes is stripped). If a YAML-parsed value fails validation the command retries once with the raw text. `user set` behaves the same. The whole result is validated with the real models before anything is written;
errors are printed as `path: reason` and the files are left untouched. A model reference is checked against the provider catalog
like in the web UI. Rewriting a file drops its YAML comments.

```bash
python -m src.admin agent create --user user-a --id guide --from-template guide
python -m src.admin agent set --user user-a --agent notes --set chat_config.model=openrouter:vendor/model
python -m src.admin agent set --user user-a --agent notes --set chat_config.tool_groups=[memory_read,web]
python -m src.admin agent rename --user user-a --agent guide --new-id main --name "User A's assistant"
python -m src.admin agent check
```

The agent's **display name** is its `name` (shown in the web UI and Telegram); change it with `agent set --set name="New name"`. It is separate from the SOUL, which holds the personality, and from the `id` (folder and URL), which normally stays: renaming the id of the agent that runs the current chat moves its data and can break the open chat, so open a new chat afterwards. If `python -m src.admin` reports a missing module, the shell's `python` is not the app's: use `.venv/Scripts/python` (Windows) or `.venv/bin/python` (Linux) from the repo root.

## Templates

`--from-template default` is the plain agent with the default SOUL. `guide` is the setup guide: level `operator`, tool groups `memory_read`,
`memory_edit`, `web`, `self`, `agents` and `permissions` (no terminal, no backend), and a short SOUL that tells it to read the
built-in [RECALL knowledge](recall-knowledge.md), manage the user's agents with the [`agents` tools](../reference/tools.md#agents) and their
permissions with the `permissions` tools (every change on an approval card), ask before anything destructive and never read or print
secrets. It says plainly that terminal backends, users and provider keys are managed by the administrator and cannot be changed from a
chat. The guide owns its SOUL; it cannot rename or delete itself (the user does it in the Agents tab), and it can be reshaped into the
user's main agent. An administrator who wants an agent that runs this CLI gives an `admin` agent the `terminal` group by hand.

`permission-manager` creates an agent named `Permission manager` at level `operator` with the tool groups `memory_read`, `memory_edit`,
`self` and `permissions` (no terminal, no web) and a short SOUL: it explains the levels, reviews what each of the user's agents can
do (levels, tool groups and rules it may change; its own only within its level, and its level only lower), proposes a change and its trade-off before making it, recommends the lowest level that does the job and never tries to go around a
limit or an approval. Each change it makes shows the user an approval card (approve once or deny, never "always") and is audited. See
[tools](../reference/tools.md#permissions).

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
