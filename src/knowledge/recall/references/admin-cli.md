# Admin CLI

`python -m src.admin <command>` changes RECALL through the same code and validation as the web UI. Run it from the repository root (`/app` in Docker, which is the `local` backend's working directory). Use it instead of editing YAML by hand: it validates before writing, takes the same locks, writes atomically and refuses an agent that is busy. It never prints a secret: YAML holds the NAMES of environment variables.

Values after `=` are YAML: `true`, `42`, `[a, b]`, `{k: v}`, `null`, or a quoted string. Paths are dotted keys into the file.

## Agents

| Command | What it does |
| --- | --- |
| `python -m src.admin agent list --user U` | id, name, level, model and tool groups |
| `python -m src.admin agent show --user U --agent A` | the agent yaml as loaded, plus its level |
| `python -m src.admin agent create --user U --id A --name N [--level L]` | new agent with the default SOUL |
| `python -m src.admin agent create --user U --id A --from-template guide` | the setup guide: level `operator`, groups `memory_read`, `memory_edit`, `web`, `self`, `agents`, `permissions`, no terminal, its own SOUL; needs `max_level` operator+ |
| `python -m src.admin agent create --user U --id A --from-template permission-manager` | level `operator`, groups `memory_read`, `memory_edit`, `self`, `permissions`; needs `max_level` operator+ |
| `python -m src.admin agent set --user U --agent A --set path=value` | change settings (repeat `--set`); `--level L` sets the permission level |
| `python -m src.admin agent rename --user U --agent A --new-id B --name N` | new id (and optional name); moves `data/U/A` to `data/U/B` exactly like the web UI |
| `python -m src.admin agent delete --user U --agent A --yes` | remove the configuration; the data folder stays on disk |
| `python -m src.admin agent rules --user U --agent A [--clear\|--effective]` | the agent's own approval rules; `--clear` removes them (audited); `--effective`: all three layers |
| `python -m src.admin agent check` | load every user and agent with the real loader; prints each agent's effective level, then `OK` or each error (exit code 1); `--user U` limits it |

Examples:

```
python -m src.admin agent set --user user-a --agent notes --set chat_config.model=openrouter:vendor/model
python -m src.admin agent set --user user-a --agent notes --set chat_config.tool_groups=[memory_read,web]
python -m src.admin agent set --user user-a --agent notes --set terminal.backends.local.approvals=manual
python -m src.admin agent create --user user-a --id travel --name Travel --set description="Trip planning"
```

- A model is checked against the provider catalog like in the web UI, so a typo or an unavailable provider is refused with a message.
- Values are parsed as YAML: wrap text containing `": "` (or starting with `[`, `{`, `#`, `*`, `&`) in quotes. A plain string field (`description`, `name`, ...) takes the value raw (`--set description="Veterinaria: salud"` works as typed); if YAML parsing fails validation, the command retries once with the raw text. `user set` works the same way.
- The DISPLAY NAME is `name`, separate from the SOUL (a rename changes both; SOUL via `soul_edit`). The `id` is the folder/URL and normally stays.
- `id` cannot be changed with `set`; use `rename`. A backend named in `terminal.backends` must exist in the user's `terminal_backends` (see `user set`).
- The file is rewritten from the validated config: YAML comments are not kept.
- `check` also warns (exit 0) about files in `agents/` that the user does not list, key variables that are not set in the CLI's own environment, and a terminal group without a usable backend.

## Users

| Command | What it does |
| --- | --- |
| `python -m src.admin user list` | users with `max_level` and agent levels |
| `python -m src.admin user show --user U` | `user.yaml` with key variable NAMES; never the password hash |
| `python -m src.admin user set --user U --set path=value` | non-secret fields: `name`, `language`, `models.*`, `provider.name`, `provider.api_key` (a variable NAME), `terminal_backends.*`, `claude_subscription.claude_config_dir`, ... |

`max_level` (`minimal`, `reader`, `assistant`, `collaborator`, `operator`, `admin`) is required, is the highest `level` the user's agents may have and is set here or by an admin in Settings > Users (never one's own); refused while an agent needs or declares more. `password_hash`, `id` and `agents` are refused (use `set-password` and the `agent` commands). `*.api_key` values must look like a variable name, never a key.

`python -m src.admin task run --user U --agent A --goal "..." [--context C] [--instructions I] [--notify AGENT:CHAT] [--wait]` starts an **unattended** subagent task with no parent chat: prints `task: <id>` (`--wait`: also status, result). Manual and dangerous terminal actions are denied, `smart` normal ones run; limits and the daily token budget apply; without `--wait` it stops on exit. See `delegation-processes.md`.

Creating users and passwords stays with `python -m src.admin create-user [--max-level L]` (default `collaborator`) and `python -m src.admin set-password`, which the user runs himself (hidden prompt). Administrators (`max_level: admin`) can also create users in the web UI: Settings > Users > Add user (wizard: user, provider key or Claude sign-in, first agent; `POST /api/admin/users` and siblings; the key is write-only and audited without its value in `<data>/<user>/admin_audit.jsonl`). `python -m src.admin version` prints the running version (and the image build id when set). Other checks: `python -m src.admin terminal-test --user U --backend B` (ssh backends) and `python -m src.admin route-test "text" --user U`.

## Spaces

Shared agents (see `shared-agents.md`; the web UI invites people, the CLI is for admins). A space (`space-<8 hex>`) is a pseudo-user with `members`; it never logs in and its `max_level` is the lowest of its members.

| Command | Does |
| --- | --- |
| `space list` / `space show --space S` | spaces with members, payer, derived level, agents |
| `space create --billing-user U [--member V ...] [--name N]` | copies U's provider, models, Claude login and backends settings (NAMES only) |
| `space add-member` / `remove-member --space S --user U` | refused on agent id clash or a level below an agent; removing the payer needs `--new-billing-user` |
| `space delete --space S --yes` | only a space without agents |
| `agent share --user U --agent A --with V` / `agent unshare --space S --agent A --yes` | move an agent into a space, or give it back (copies to each member) |

No user-delete command exists: a user removed by hand makes their spaces unusable until `space remove-member` is run, so run it first.

## Secrets

| Command | What it does |
| --- | --- |
| `python -m src.admin set-secret NAME` | saves a value into `.config/secrets.env` (mode 0600) from a hidden prompt; `--stdin` reads a pipe; prints only `saved NAME` |
| `python -m src.admin secret list` | names the configuration refers to and whether each is set (`environment` or `secrets.env`), never values |

NAME matches `^[A-Z][A-Z0-9_]{2,63}$`. A variable in the process environment wins over the file. The file is re-read when it changes, and agents are rebuilt on their next message, so a new or rotated key needs no restart. The terminal hides the server's own environment, so `secret list` may print NOT SET for a key set in `.env`. NEVER ask the user to paste a secret into the chat and never read `secrets.env` or `.env`: the user runs `set-secret` in his own terminal.

## What applies when

| After | Takes effect |
| --- | --- |
| `agent`, `user` commands, hand edits of the yaml | the next message of each chat in the web UI and Telegram, no restart |
| `set-secret` | the next message (the cached agent is rebuilt) |
| SOUL, memories, skills | new chats |
| `settings.yaml`, `.env`, `RECALL_*` variables | restart of web and Telegram |
| a new Telegram id in `TELEGRAM_ALLOWED_USERS` | restart of Telegram |

Pickup works by comparing the yaml (and the secrets file) with what the running agent was built from; the change is applied between replies, never in the middle of one. If a file is left INVALID the services keep the last good configuration, log an error and show a notice in the chat; fix the file and run `python -m src.admin agent check`.

## Interpreter

If `python -m src.admin` reports `ModuleNotFoundError` (for example `yaml`), the shell's `python` is not the app's. Use the project's virtualenv interpreter, relative to the repo root: `.venv/bin/python` on Linux, `.venv/Scripts/python` on Windows. In the Docker container `python` is the app's.

## Renaming

Display name: `agent set --set name="NewName"` (no data moves). `agent rename` changes the `id`: for the agent running the current chat it can break the open chat, so do it only when the user asks for the id and tell him to open a new chat afterwards. It moves the data folder, updates the user's agent list and the preferences that point at the old id, and keeps conversations, memories and skills. Do it when the agent has no reply in progress (the CLI cannot see other processes).

## Errors

Run `python -m src.admin agent check` after every change.

`error: ...` goes to stderr with exit code 1 and nothing is written. Typical causes: unknown key (typo: the models forbid extra keys), wrong type, an id that already exists, `agent busy` (wait for the reply), a missing folder in `folders`.
