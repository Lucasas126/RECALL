# Recipes

With the `agents` tools (the guide) do agent recipes with `agent_create`, `agent_update`, `agent_rename`, `agent_delete` (one approval card per change; no `U`, no CLI); levels and groups go through the permissions tools. The commands below are for an admin agent with the `terminal` tool and `python -m src.admin` (one card per command): explain a step, run it, read the result. Replace `U` with the user's id. Finish every recipe that touched a yaml by hand with `python -m src.admin agent check`. Terminal backends, folders, users and secrets are the administrator's: the guide tells the user what to ask for.

## Create an agent for a topic

1. Ask the user for the topic, a short id (letters, digits, `_`, `-`) and a name.
2. `python -m src.admin agent create --user U --id travel --name Travel --set description="Trip planning"`
3. Optional: `python -m src.admin agent set --user U --agent travel --set chat_config.tool_groups=[memory_read,memory_edit,web,self]`
4. `python -m src.admin agent check`; tell the user to open the agent in the Agents tab. Its SOUL is the default; the agent edits it itself later.

## Change an agent's model

1. `python -m src.admin agent show --user U --agent A` to see the current `chat_config.model`.
2. `python -m src.admin agent set --user U --agent A --set chat_config.model=openrouter:vendor/model` (the value is `<provider>:<model>`; an unavailable model or provider is refused).
3. Applies on the next message. To go back to the user's default: `--set chat_config.model=null`.

## Turn the guide into the user's main agent

1. Set the display name (what the web UI and Telegram show): the user does it in the Agents tab (you cannot rename yourself); an admin: `python -m src.admin agent set --user U --agent guide --set name="Main"`. The `name` is separate from the SOUL, so changing only the SOUL leaves the old name in the UI.
2. Rewrite your own SOUL with `soul_edit`: new name and role, keep the rules about secrets, approvals and other users. Tell the user what changed (SOUL and display name).
3. Keep the `id` (folder and URL) unless the user asks for a new one. Renaming the id of the agent running the current chat moves its data and can break the open chat: only do it on request, with `python -m src.admin agent rename --user U --agent guide --new-id main`, and tell the user to open a new chat afterwards. The data folder moves with it; chats, memories and skills stay.
4. Run `python -m src.admin agent check`.
5. Decide with the user whether the main agent keeps the `terminal` group. If not: `python -m src.admin agent set --user U --agent main --set chat_config.tool_groups=[memory_read,memory_edit,web,self]` (the agent loses the terminal on its next message; do this last).

## Rename an agent (display name)

An agent's DISPLAY NAME is `name` in its yaml; the SOUL holds the personality and is separate. When asked to change an agent's name (including your own), update BOTH and tell the user: the SOUL with `soul_edit` and the display name with `python -m src.admin agent set --user U --agent A --set name="NewName"`, then run `python -m src.admin agent check`. The `id` normally stays.

## Add a folder

1. Ask for the absolute path as the server sees it (in Docker it must be mounted in the container first; that is a compose change by the administrator).
2. `python -m src.admin agent set --user U --agent A --set folders=[{name: docs, path: /data/docs}]` and add `read_only: true` inside the braces when it should not be edited. This REPLACES the list: show the current one first and include its entries.
3. Folder tools also need the tool group: `folders_read` or `folders_edit` in `chat_config.tool_groups` (keep the existing groups in the list).

## Enable the terminal group with a backend

1. The user must have a backend: `python -m src.admin user show --user U` (look under `terminal_backends`). For a remote machine ask the user for host, port, user and key paths; for the container itself use `python -m src.admin user set --user U --set terminal_backends.local={type: local, workdir: /app}`. `local` also needs the server switch `RECALL_TERMINAL_ALLOW_LOCAL=true` (administrator, restart); read `references/security-for-admins.md` first.
2. `python -m src.admin agent set --user U --agent A --set chat_config.tool_groups=[memory_read,web,self,terminal] --set terminal.backends.local.approvals=manual` (the backend key is the backend's name).
3. `python -m src.admin agent check`. For an `ssh` backend the user can also run `python -m src.admin terminal-test --user U --backend NAME`; for `local` it cannot, because the CLI does not see the server's switch. Keep approvals `manual`.

## Add a user's secret

1. Find the variable name: `python -m src.admin user show --user U` (`provider.api_key`) or `python -m src.admin secret list`. To use another name: `python -m src.admin user set --user U --set provider.api_key=MY_KEY_NAME`.
2. Tell the user to run in his own terminal (not in this chat): `python -m src.admin set-secret MY_KEY_NAME` (in Docker: `docker compose exec -it recall-web python -m src.admin set-secret MY_KEY_NAME`), type the value and press Enter.
3. Verify without seeing it: `python -m src.admin secret list`, then ask the user to send a message.

## Rotate a key

Same as adding: the user runs `python -m src.admin set-secret NAME` again with the new value; it replaces the old line. Agents rebuild on their next message. If the variable is also set in `.env`, the environment wins: the administrator must change or remove it there and restart.

## Delete safely

1. `python -m src.admin agent show --user U --agent A` and confirm with the user which agent, by name.
2. Mention that the configuration goes and the data folder `data/U/A` (memories, chats) is kept on disk.
3. Only after an explicit yes: `python -m src.admin agent delete --user U --agent A --yes`. Never delete data folders yourself.
