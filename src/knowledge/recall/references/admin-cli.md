# Admin CLI

`python -m src.admin <command>` changes RECALL through the same code and validation as the web UI. Run it from the repository root (`/app` in Docker, which is the `local` backend's working directory). Use it instead of editing YAML by hand: it validates before writing, takes the same locks, writes atomically and refuses an agent that is busy. It never prints a secret: YAML holds the NAMES of environment variables.

Values after `=` are YAML: `true`, `42`, `[a, b]`, `{k: v}`, `null`, or a quoted string. Paths are dotted keys into the file. Output names the paths changed, never the values.

## Agents

| Command | What it does |
| --- | --- |
| `python -m src.admin agent list --user U` | id, name, model and tool groups of each agent |
| `python -m src.admin agent show --user U --agent A` | the agent yaml as the loader sees it (defaults filled in) |
| `python -m src.admin agent create --user U --id A --name N` | new agent with the default SOUL |
| `python -m src.admin agent create --user U --id A --from-template guide` | a setup guide: tool groups `memory_read`, `memory_edit`, `web`, `self`, `terminal`, local backend, its own SOUL |
| `python -m src.admin agent set --user U --agent A --set path=value` | change settings (repeat `--set`) |
| `python -m src.admin agent rename --user U --agent A --new-id B --name N` | new id (and optional name); moves `data/U/A` to `data/U/B` exactly like the web UI |
| `python -m src.admin agent delete --user U --agent A --yes` | remove the configuration; the data folder stays on disk |
| `python -m src.admin agent check` | load every user and agent with the real loader; prints `OK` or each precise error, exit code 1 on error; `--user U` limits it |

Examples:

```
python -m src.admin agent set --user ana --agent notes --set chat_config.model=openrouter:vendor/model
python -m src.admin agent set --user ana --agent notes --set chat_config.tool_groups=[memory_read,web]
python -m src.admin agent set --user ana --agent notes --set terminal.backends.local.approvals=manual
python -m src.admin agent create --user ana --id travel --name Travel --set description="Trip planning"
```

- A model is checked against the provider catalog like in the web UI, so a typo or an unavailable provider is refused with a message.
- Values are parsed as YAML: wrap text containing `": "` (or starting with `[`, `{`, `#`, `*`, `&`) in quotes. Exceptions: when the target field is a plain string (`description`, `name`, ...) the value is taken raw, so `--set description="Veterinaria: salud y cuidado"` works as typed (one pair of surrounding quotes is stripped); if YAML parsing fails validation, the command retries once with the raw text. Lists, maps, booleans, numbers and optional fields (`null` clears them) keep YAML. `user set` works the same way.
- The agent's DISPLAY NAME is `name` (web UI and Telegram): `agent set --user U --agent A --set name="NewName"`. It is separate from the SOUL, so a rename of an agent changes both (SOUL via `soul_edit`). The `id` is the folder/URL and normally stays.
- `id` cannot be changed with `set`; use `rename`. A backend named in `terminal.backends` must exist in the user's `terminal_backends` (see `user set`).
- The file is rewritten from the validated config: YAML comments are not kept.
- `check` also warns (exit 0) about files in `agents/` that the user does not list, key variables that are not set in the CLI's own environment, and a terminal group without a usable backend.

## Users

| Command | What it does |
| --- | --- |
| `python -m src.admin user show --user U` | `user.yaml` with key variable NAMES; never the password hash |
| `python -m src.admin user set --user U --set path=value` | non-secret fields: `name`, `language`, `models.*`, `provider.name`, `provider.api_key` (a variable NAME), `terminal_backends.*`, `claude_subscription.claude_config_dir`, ... |

`password_hash`, `id` and `agents` are refused (use `set-password` and the `agent` commands). `*.api_key` values must look like a variable name, never a key.

Creating users and passwords stays with `python -m src.admin create-user` and `python -m src.admin set-password`, which the user runs himself (hidden prompt). `python -m src.admin version` prints the running version (and the image build id when set). Other checks: `python -m src.admin terminal-test --user U --backend B` (ssh backends) and `python -m src.admin route-test "text" --user U`.

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

Display name: `agent set --set name="NewName"` (no data moves). `agent rename` changes the `id`: renaming the id of the agent that is running the current chat moves its data and can break the open chat, so do it only when the user asks for the id, and tell him to open a new chat afterwards. `agent rename` moves the data folder, updates the user's agent list and the preferences that point at the old id, and keeps conversations, memories and skills. Do it when the agent has no reply in progress; the CLI cannot see a reply running in another process. Existing chats continue under the new id.

## Errors

Run `python -m src.admin agent check` after every change.

`error: ...` goes to stderr with exit code 1 and nothing is written. Typical causes: unknown key (typo: the models forbid extra keys), wrong type, an id that already exists, `agent busy` (wait for the reply), a missing folder in `folders`.
