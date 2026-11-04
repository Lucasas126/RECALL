# settings.yaml (framework settings)

The root `settings.yaml` holds framework-wide defaults shared by every user and agent. It never contains secrets: keys
that need one hold the **name** of an environment variable (see [environment variables](environment-variables.md)).
Per-user values are in [user.yaml](user-yaml.md), per-agent values in [agent.yaml](agent-yaml.md). The file is read at start-up; restart the
web UI and the Telegram bot after editing it. In Docker you can mount your own copy (see [Docker](../deployment/docker.md#persistent-state)).

| block | keys | documented in |
| --- | --- | --- |
| `paths` | `config_dir` (`.config`), `data_dir` (`data`), `logs_dir` (`logs`) | [Docker](../deployment/docker.md#persistent-state) |
| `index` | `dir_name`, `collection`, `file_types`, `chunk_tokens`, `max_chunk_tokens`, `overlap_tokens`, `embed_batch`, `folders_reconcile_minutes` | [memory](../user-guide/memory.md#memory-index) |
| `agent` | `max_tool_hops` (default 5): tool round-trips before a final answer is forced | here |
| `memory_limits` | `soul_chars` (15000), `memory_chars` (2500), `user_chars` (2500): maximum sizes of SOUL.md, MEMORY.md and USER.md | [memory limits](#memory-limits) |
| `web` | `host`, `port`, `session_secret_env`, `public_url` | [web authentication](#web-authentication) |
| `telegram` | `allowed_users_env` (default `TELEGRAM_ALLOWED_USERS`), `chat_idle_minutes` (default `60`, 1-10080; `RECALL_TELEGRAM_CHAT_IDLE_MINUTES` overrides it) | [Telegram](../user-guide/telegram.md#telegram-access) |
| `video`, `pdf`, `speech_to_text` | frame sampling, OCR and transcription defaults | [files and media](../user-guide/files-and-media.md) |
| `prompt_cache` | `enabled`, `anthropic_providers`, `trim_slack_percent` | [prompt caching](#prompt-caching) |
| `tavily` | `api_key` (variable name), `timeout_seconds`, `max_content_chars` | [shared web access](#shared-web-access-tavily) |
| `associative_memory` | `context_messages`, `top_n`, `score_threshold`, `query_model`, `api_key`, `zdr` | [memory](../user-guide/memory.md#associative-memory) |
| `chat_naming` | `model`, `api_key`, `zdr`, `min_messages` | [chat naming](#chat-naming) |
| `mcp` | `connect_timeout_seconds`, `call_timeout_seconds`, `servers` | [MCP servers](../user-guide/mcp.md) |

## memory limits

```yaml
memory_limits:
  soul_chars: 15000    # 1000-100000
  memory_chars: 2500   # 100-50000
  user_chars: 2500     # 100-50000
```

Maximum characters of an agent's `SOUL.md`, `MEMORY.md` and `USER.md`. They apply to every user and agent (there is no
per-user or per-agent override) and to writes by the agent, by dreams and by the web editors. `MEMORY.md` and `USER.md`
are injected in every system prompt with a `used/limit` counter. Lowering a limit does not truncate existing text: the
file just cannot be saved or extended until it fits. Values outside the bounds, or unknown keys, stop the start-up.
Agent files from older versions may still contain `memory_limits`; it is ignored.

## web authentication

Web users are preconfigured; there is no registration flow. Add an Argon2 password
hash to each user's `user.yaml`:

```yaml
id: johndoe
name: John Doe
password_hash: "$argon2id$..."
```

Generate a hash without putting the password in a file or command history:

```powershell
uv run python -m src.web.auth
```

The web UI uses a signed, HTTP-only `recall_session` cookie. Set the session
signing secret through the environment variable named by `web.session_secret_env`
(default: `RECALL_SESSION_SECRET`) before starting `web.py`. Use a long random
value and keep it outside YAML and source control. Logging out clears the session.

The web UI binds to `web.host`/`web.port` in `settings.yaml` (default
`127.0.0.1:8000`). The `RECALL_WEB_HOST` environment variable, when set and not
empty, overrides `web.host`; it is read from the process environment when the
settings load, not from `.env`. The Docker image sets it to `0.0.0.0` (see
[deployment](../deployment/docker.md)).

`web.public_url` (default empty) is the address users open the web UI at, for
example `http://<server-ip>:8000`. When set, Telegram [dream
notifications](../user-guide/dream.md#telegram-notification) include a link to the session
(`<public_url>/#agents/<agent_id>/dreams/<session_id>`); when empty they stay
text-only. The `RECALL_PUBLIC_URL` environment variable, when set and not empty,
overrides it; like `RECALL_WEB_HOST` it is read from the process environment, not
from `.env` (Docker Compose passes `.env` to the containers' environment, so
there it works). A trailing `/` is dropped; a value that does not start with
`http://` or `https://` stops startup with an error. The link is sent as a
Telegram `text_link` so it is clickable; Telegram accepts IP addresses and
domains but rejects `localhost`, in which case the message falls back to plain
text (copy the address by hand). Use the server's LAN IP, not `localhost`.

## shared web access (tavily)

Tavily uses a **framework-wide key**, shared by all users and agents. This is
separate from each user's chat/embedding provider credentials and shares one
Tavily account's quota and billing. Put the key in the ignored root `.env` file
(the normal entry points load it), or set it in the process environment:

```dotenv
TAVILY_API_KEY=your_tavily_api_key
```

The root `settings.yaml` references the variable name, never its value:

```yaml
tavily:
  api_key: TAVILY_API_KEY
  timeout_seconds: 30
  max_content_chars: 16000
```

`timeout_seconds` must be between 1 and 60; `max_content_chars` must be an
integer between 1,000 and 100,000. The client resolves the key when constructed,
not when settings are imported. An unset/blank key leaves web access unavailable;
it does not prevent normal chat. Restart after changing configuration or the key.

## prompt caching

Prompt caching lets the provider reuse the part of a request it has already
processed (the system prompt and earlier messages), billed at a fraction of the
input price. It is framework-wide, in the root `settings.yaml`:

```yaml
prompt_cache:
  enabled: true
  anthropic_providers: [Amazon Bedrock]
  trim_slack_percent: 25
```

How each model family is handled:

- **Claude on OpenRouter** (`anthropic/...` and `~anthropic/...` aliases) does not
  cache on its own. When `enabled` is true, RECALL puts a `cache_control` breakpoint
  on the system prompt and another on the last message that has text, on every chat
  request (streaming or not). The second breakpoint moves forward each turn, and the
  provider reuses the longest prefix it already has, so a conversation keeps reading
  its history from the cache. The cache lives about five minutes since its last use
  (OpenRouter's default). A system prompt shorter than the model's minimum
  (roughly 1,000-4,000 tokens, depending on the model) is not cached at all, which
  costs nothing. Cache writes are billed above the normal input price and reads
  well below it; a chat that sends fewer than two messages within the cache
  lifetime pays the write without ever reading it.
- **Other models** (DeepSeek, GPT, Gemini, Grok, ...) cache the repeated prefix on
  their own, so RECALL sends their requests unchanged. Which ones do is up to
  OpenRouter and the provider.
- The associative-memory hint, the `[CUTOFF CONVERSATION]` marker and other
  `system` messages that follow the conversation do not break the cache: the
  prefix before them is unchanged.

`anthropic_providers` is the list of OpenRouter provider names tried first for
Claude requests (`provider.order`). It exists because routing matters for caching:
in tests, requests routed through Google Vertex lost the cache whenever any text
after the breakpoint changed, and sometimes without a visible reason, while Amazon
Bedrock behaved as documented. Other providers stay available as a fallback, and
the ZDR policy still applies on top, so the list is a preference, not a restriction.
An unknown provider name is ignored and the request still works, so a renamed or
dropped provider degrades to OpenRouter's default routing. Use an empty list (`[]`)
to let OpenRouter choose freely. Requiring the `Anthropic` provider alone (no
fallback) returned "no endpoints found" when tested with ZDR on, so it is not a
useful value here.

`trim_slack_percent` (0-90) controls how the context window is cut. A cut moves
the start of the history, which invalidates the cached history. Without slack the
agent drops one message per turn once `conversation_messages_retained` is reached,
so the cache would be lost on every turn. With slack, the agent cuts down to
`retained - slack%` and then lets the window grow again until it passes
`retained`, so the cache survives between cuts. The window never exceeds
`conversation_messages_retained`; it only holds fewer messages right after a cut
(20 retained with 25% slack goes from 21 to 15 messages). Use `0` for the exact
sliding window. It applies to every provider, not only OpenRouter. Small windows
(for example 3 or less) are not reduced.

What still invalidates the cache, by design:

- Changing the agent's tools, personality (`SOUL.md`, read when a chat starts) or model. Tools come before the
  system prompt in the cached prefix, so a change in the tool list (an MCP server
  connecting, going down or being disabled) also invalidates the system prompt. That
  is why MCP notes live in the system prompt and are not split into a separate block.
- A different chat or model: each has its own prefix.
- A pause longer than the cache lifetime.
- Editing earlier messages or the first message in the window being cut off.

The web debug panel and the saved `usage_report` show the result on a second line
of the usage report:

```
cache: read=4958 (91% of input) write=13
```

`read` are input tokens served from the cache, `write` tokens stored in it for
later, and the percentage is the share of input that was read. The line is absent
when the provider returns no breakdown (it is unknown, not zero). The report covers
the last model call of the turn, so a turn that used tools shows the final call
only. The chat history also stores `cache_read` and `cache_write` next to the saved
input and output tokens. Prefer this line to the cost estimate when checking that
caching works: after the first message of a chat, `read` should be close to the
whole previous context.

## chat naming

The root `settings.yaml` holds a framework-wide small model used to generate chat
titles. Like Tavily, it uses one shared key referenced by env var name:

```yaml
chat_naming:
  model: openai/gpt-4o-mini
  api_key: OPENROUTER_API_KEY
  # zdr: true   # optional override of the user's require_zdr
  min_messages: 5
```

`model` is an OpenRouter model ID (or a `claude_subscription:<model>` reference,
which runs with the requesting user's own Claude login; see
[claude subscription](../user-guide/claude-subscription.md)), the optional `zdr` overrides the user's
`require_zdr` (true restricts routing to zero data retention providers; unset follows
the user), and `min_messages` (1-1000) is the number of user and assistant messages
required before a title is generated. Behavior:

- the title is generated once, after the reply that reaches `min_messages`, and stored
  in `<chat_id>.title.json` next to the chat's other metadata; it is not regenerated
- shorter chats keep the first user message as their title
- if the env var is unset or the naming request fails, a warning is logged and the
  chat keeps its previous title; the reply has already been delivered by then. An
  unreadable title file is an explicit error, not a fallback
- the title is shown in the chat list and removed with the chat
- users can rename a chat with messages from the web chat list (✎ or double-click the title),
  or with `PUT /api/agents/<agent_id>/chats/<chat_id>/title` and `{"title": "..."}`. the
  title is trimmed to one line of 1-60 characters (otherwise `400`) and replaces the
  stored one; automatic naming never overwrites an existing title
