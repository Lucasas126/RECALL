# new user guide

A step-by-step guide for a **new person on an existing RECALL install**. No technical knowledge is needed, but a few steps are
done by the **administrator** (the person who runs the server); those are marked *Administrator*. Examples use placeholders such
as `<user_id>`: replace them with your own values.

RECALL gives you one or more **agents**: assistants with their own personality, memory and tools. You chat with them on the web
and, if you want, on Telegram.

## 1. Your account (Administrator)

The administrator creates your user; there is no sign-up page. The simplest way is the admin command (on Docker, run it
inside the container):

```bash
docker compose exec -it recall-web python -m src.admin create-user <user_id> --name "<Your Name>" --api-key-env OPENROUTER_<USER>_API_KEY
# without Docker: uv run python -m src.admin create-user <user_id> ...
```

It asks for a password of at least 8 characters (typed twice, never shown) or, with `--generate`, creates a strong one and
prints it once. Add `--claude-login` if the user will use their own Claude account (section 6). It never asks for the API key:
put it in `.env` under the variable name you gave (`provider.api_key` holds only that **name**). To change a password later:
`python -m src.admin set-password <user_id>`. Telegram: add `<telegram_id>=<user_id>` to `TELEGRAM_ALLOWED_USERS` in `.env`.
The by-hand alternative is to copy `.config/example_user/` to `.config/<user_id>/` and edit `user.yaml`
([user.yaml reference](../reference/user-yaml.md)); the hash then comes from `uv run python -m src.web.auth`.

Restart the web UI so the new user is picked up. The administrator then tells you your user id and password through a private
   channel.

## 2. Log in on the web

Open the address the administrator gave you (for example `http://<server-ip>:8000`), type your user id and password and press
log in. **Settings** (bottom of the sidebar) lets you change the theme, language, whether thinking is shown, the default models
and how many memory fragments are suggested per message. **Log out** ends the session.

## 3. First screen: no agents yet

If you have no agents the chat shows an empty state with a **Create an agent** button (also available in the **Agents** tab of the
sidebar). You cannot chat until you have one.

## 4. Create your first agent

In **Agents** choose **New agent**:

- **Name** and **description**: what you see in the list; the description is not sent to the model.
- **Personality (SOUL)**: free text that says who the agent is and how it should talk. It is stored in the agent's `SOUL.md`, can
  be edited later in **Soul & memory**, and keeps a history of the last versions.
- **Advanced**: streaming, whether the agent keeps the conversation in context, reasoning, how many messages are kept, how it
  searches its memory and its optional default model.
- **Tools**: tool groups the agent may use: memory (read, or read and edit), web search (needs the administrator's Tavily key),
  self (it may edit its own personality, memories and skills) and folders. Fewer tools means a safer, simpler agent.
- **MCP**: external services such as Google Calendar, if the administrator offers them (connect your own account in
  Settings > Connections first).

Save. Open the chat and say hello. More on agents: [agents](../user-guide/agents.md).

## 5. Choose models

Models belong to **you**, not to the agent. The model picker has one tab per provider: **Any** (all available providers together),
**OpenRouter**, **Ollama** and **Claude** (experimental). A greyed-out tab says why it is unavailable, for example a missing key
or a program that is not installed. Star a model to make it a **favorite**; favorites are also what Telegram offers.

The model used in a chat is, in order: the model you pick in that chat (composer) > the agent's default model (Agents >
Advanced > Default model) > your default model (Settings). Choosing **Use agent default** or **Use user default** clears an override.
Speech-to-text (voice messages) is a single default in Settings. Details: [models and providers](../user-guide/models-and-providers.md).

## 6. Use your own Claude login (optional, experimental)

If you have a Claude Pro/Max account you can use it for Claude models. It must be **your own** account: never share logins.

1. *Administrator*: in your `.config/<user_id>/user.yaml` set a separate login folder:

   ```yaml
   claude_subscription:
     claude_config_dir: /path/to/<user_id>-claude-login
   ```

2. You (or the administrator on your behalf, on the server) log in once with your own account:

   ```bash
   CLAUDE_CONFIG_DIR=/path/to/<user_id>-claude-login claude auth login
   ```

3. Open the model picker: the **Claude** tab becomes selectable once the login works.

On a Docker server the Claude program is already in the image: the administrator creates your login folder under
`/app/.config/<user_id>/claude-login` and you sign in once with the command in
[deployment](../deployment/docker.md#claude-subscription-logins) (a URL to open in your browser, then a code to paste).
Otherwise Claude Code must be installed on the server. Read
[Claude subscription](../user-guide/claude-subscription.md) for the terms and limits; this route is not zero-data-retention.

## 7. Connect Telegram (optional)

1. Find your numeric Telegram id: message a bot such as `@userinfobot`, which replies with it.
2. *Administrator*: add `<telegram_id>=<user_id>` to `TELEGRAM_ALLOWED_USERS` in `.env` (comma-separated for several people,
   for example `111111111=alice,222222222=bob`) and restart the Telegram bot.
3. Open the bot in Telegram and write to it. Useful commands: `/help`, `/newchat`, `/selectagent`, `/selectmodel`,
   `/settings`, `/dream`. Messages from ids that are not listed are ignored.

See [Telegram](../user-guide/telegram.md).

## 8. How the agent remembers

- **Mind palace**: private notes the agent writes for itself.
- **Conversations**: your past chats; the agent can look them up.
- **Associative memory**: before each reply RECALL searches those memories and gives the agent the most relevant fragments. A
  small pill above the reply shows it (searching, number of matches, none or skipped). You can set how many fragments per
  message in Settings.
- **Dream mode**: from time to time (on a schedule or with `/dream start`) the agent reviews recent chats and tidies its own
  personality, memories and notes. See [dream mode](../user-guide/dream.md).
- **User folders**: in **Agents > Folders** the administrator or you can give an agent a folder on the server, such as a
  notes vault. It can read it (and edit it unless marked read-only) when the matching tool group is on. See [memory](../user-guide/memory.md).

## 9. Privacy basics

By default RECALL uses any route your provider offers. If you want only **zero data retention** (ZDR) routes at OpenRouter, ask
the administrator to set `require_zdr: true` in your `user.yaml`; models without ZDR then disappear from the picker. The
Claude route and Ollama cloud models are not ZDR. Search (Tavily) and external services (MCP) receive what the agent sends them.
Do not put secrets in chats. See [security notes](../deployment/security.md).

## 10. When something goes wrong

Errors appear as a card with a title, a hint and sometimes a button.

| card (code) | meaning | what to do |
| --- | --- | --- |
| no agents (`no_agents`) | you have no agent yet | **Create an agent** |
| unknown agent / chat (`unknown_agent`, `unknown_chat`) | it was deleted or is not yours | start a new chat |
| missing API key (`missing_api_key`) | the server has no key for that provider | choose another model; tell the administrator |
| provider unreachable (`provider_unreachable`) | the model server did not answer | choose another model or try later |
| Claude not installed / not logged in (`claude_not_installed`, `claude_not_logged_in`) | the Claude program or login is missing | tell the administrator; log in with your own account |
| provider unavailable (`provider_unavailable`) | Claude did not report its login state | choose another model |
| model unavailable (`model_unavailable`) | the model is retired or not installed | choose another model |
| rate limited (`rate_limited`) | the provider is busy (HTTP 429) | wait or choose another model |
| agent busy (`agent_busy`) | the agent is still answering | wait; you can also send a new message to steer it |
| service not connected (`mcp_not_connected`) | a tool needs an account you have not connected | Settings > Connections |

The full table is in the [API reference](../reference/api.md#error-codes).
