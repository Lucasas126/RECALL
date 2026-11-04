# claude subscription provider (experimental)

Status: implemented, experimental. It lets one user run Claude
models through **their own** Claude Pro/Max login by driving the official Claude
Code CLI (`claude`) instead of an API key. It is adapted from the MIT-licensed
Hermes plugin `claude-subscription-directsdk-experimental` by Nous Research
(attribution is kept in the header of every adapted file under
`src/providers/claude_subscription/`).

## terms and who may use it

Use it only for yourself, with your own Claude login. Anthropic's terms do not
allow a third-party product to offer claude.ai logins to other people, so **do not
let other people use your login** and do not point one user's configuration at
another person's login. There is no per-user switch: any RECALL user can pick a
Claude model whenever the CLI on the server is logged in, and then uses *that*
login. On a shared install give each person their own login folder
(`claude_config_dir` below, run `claude auth login` with that folder), or do not
log the CLI in at all.
Subscription limits, extra-usage settings and data retention are those of your
Claude account; this route is **not zero data retention**.

## how it works

Each model request starts one `claude -p` process in stream-json mode:

- RECALL's conversation is replayed as stream-json frames (earlier user frames are
  sent with `shouldQuery: false`); the system prompt goes in a private temporary file.
- Claude Code's own tools, skills, settings files, CLAUDE.md files, auto memory,
  slash commands and session persistence are disabled. RECALL's tools are listed
  by a small inert MCP server (`inert_mcp.py`) that refuses every call; the model
  emits `tool_use`, the run stops after one step (`--max-turns 1`), and **RECALL**
  executes the tool and sends the result in the next request. A tool turn's signed
  thinking is kept in memory by the provider and replayed with it, as the API
  requires while a tool loop is open.
- A request-scoped loopback relay (`admission.py`) is set as the CLI's
  `ANTHROPIC_BASE_URL`. It forwards only the **first** Messages request of the run
  and refuses later ones locally, because the CLI can otherwise start extra
  generations (recovery, retries) that spend plan allowance. Authorization headers
  pass through memory to `api.anthropic.com`; nothing is logged or stored. The
  standard `HTTPS_PROXY`/`NO_PROXY` variables are honored for an `http://` CONNECT proxy.
- Prompt cache: before forwarding, the relay applies the plugin's prompt-cache
  hygiene (`cache.py`). Claude Code adds per-request context to the turn it answers
  (today's date split into a trailing system message, an account reminder before
  the newest user text, notes after tool results) and puts its message cache
  breakpoint on it; the next request replays the turn without that context, so the
  cached prefix never recurred. The relay moves the date line back into the
  environment message, drops the prepended reminder, restores RECALL's tool-result
  frame exactly as sent, and keeps the message breakpoint on the last block the
  next request replays unchanged (plus one extra breakpoint after long tool loops,
  never more than four in total). The system prompt file is written byte-identical
  (no newline translation), the session id is derived from the system prompt and
  first message (Claude Code names a per-session folder in its environment
  message), and the working folder is stable. A request whose shape the relay does
  not recognise is forwarded unchanged: a CLI change costs cache, never a turn.
- Text and thinking stream to the channels as they arrive. Usage (input, output,
  cache read/write tokens) comes from the upstream answer. The debug/usage report
  adds a line saying the turn counts against the plan, with the CLI's list-price
  estimate (not a charge).
- Cancelling a reply (Stop, steering, an associative-memory pause, dream limits)
  kills the CLI's whole process tree (`taskkill /T` on Windows, the process group
  on Linux/macOS), so the `node` child behind the npm shim never outlives it.
- RECALL never reads, copies, refreshes or prints Claude credential files; all
  authentication goes through `claude`.

The provider refuses to start the CLI when RECALL's environment sets
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
`ANTHROPIC_FOUNDRY_API_KEY` or `CLAUDE_CODE_USE_BEDROCK`/`VERTEX`/`FOUNDRY`; the error
names the variables (never their values). Remove them to use the login.

## setup

1. Install Claude Code (needs Node.js): `npm install -g @anthropic-ai/claude-code`.
   `claude` is looked up on `PATH`, then in the usual install folders
   (`~/.local/bin`, `~/.claude/local`, `~/.npm-global/bin`, ...). Set
   `claude_subscription.command` (or the `RECALL_CLAUDE_COMMAND` env var) to the
   executable if it lives elsewhere.
2. Log in as the account RECALL runs as: `claude auth login`. For a separate login
   folder, run `CLAUDE_CONFIG_DIR=<dir> claude auth login` and set
   `claude_config_dir: <dir>` below.
3. Pick Claude in the model picker: its **Claude subscription** tab becomes
   selectable once `claude` is found and logged in (see
   [providers](config.md#providers)); until then the tab is disabled and says why.
   There is nothing to enable. The options below are optional, advanced and have
   no UI; set them in that user's `user.yaml` only if you need them:

```yaml
# <user_id>/user.yaml
provider:
  name: openrouter                  # stays the main provider (embeddings, speech-to-text)
  api_key: OPENROUTER_API_KEY
claude_subscription:                # every key optional; there is no `enabled` key
  claude_config_dir: null           # optional CLAUDE_CONFIG_DIR of this user's own login; null = the CLI default
  command: null                     # optional path to the claude executable; null = PATH / usual folders
  claude_code_telemetry: false      # false (default) starts claude with telemetry-off flags
  timeout_seconds: 300              # longest silence from claude before the request fails (10-3600)
```

Model versions: the pinned aliases need a recent CLI (Haiku 5.5: 2.1.293+, Sonnet
5.5: 2.1.284+, Opus 5.5: 2.1.280+). RECALL checks `claude --version` once per
provider and asks you to run `claude update` when it is older.

## selecting it: model references

A model reference is a bare model id, served by the user's main provider, or
`<provider>:<model>` (see [model references](config.md#model-references));
`claude_subscription:<model>` is served by the user's Claude login. `<model>` is a
Claude Code model name; the pickers offer `haiku`, `sonnet` and `opus` (pinned to
Haiku/Sonnet/Opus 5.5 with their 1M context window). The same reference works
everywhere a model is chosen:

| use | where | example |
| --- | --- | --- |
| user's default chat model | `models.chat` in `user.yaml`, or Settings → Default LLM (Claude tab) | `claude_subscription:sonnet` |
| one chat | the chat's model picker (Claude tab) | `claude_subscription:opus` |
| an agent's dreams | `dream.model` in the agent file / Dream tab → Browse (Claude tab) | `claude_subscription:sonnet` |
| associative-memory query | `associative_memory.query_model` in `settings.yaml` | `claude_subscription:haiku` |
| chat naming | `chat_naming.model` in `settings.yaml` | `claude_subscription:haiku` |

The prefix was chosen over a separate `provider` field because model references
already travel as plain strings (user defaults, chat overrides, favorites, the
Dream tab, Telegram); a prefixed string needs no parallel field in each place. The
same scheme serves `openrouter:` and `ollama:` references; only known provider
names count as a prefix, so it cannot collide with model ids.

Rules:

- Availability replaces any enabled switch: the Claude tab is selectable when the
  CLI is found and `claude auth status` reports a login. Choosing a Claude model
  while it is not (for example after the login lapsed) is refused with
  `Provider not available: Claude subscription. ...` naming the fix; a reply that
  already uses one fails with Claude Code's own error (`claude auth login`).
- `associative_memory.query_model` and `chat_naming.model` are framework-wide, but
  each request runs with the **requesting user's** Claude settings and login. When
  Claude Code is not installed or not logged in, the associative search is skipped
  for that turn (indicator "skipped") and the chat keeps its title; both are logged.
- These two settings need no ZDR setting to use a `claude_subscription:` model.
  Zero data retention is optional and off by default (`require_zdr` in `user.yaml`,
  see [config](config.md#zero-data-retention-is-a-user-policy)); the privacy of the
  Anthropic account (for example opting out of training) is managed in that account.
  Only when ZDR is required does a subscription model conflict: `settings.yaml`
  fails to load with `chat_naming.zdr: true` or `associative_memory.zdr: true` and a
  subscription model, and a user with `require_zdr: true` fails to load with a clear
  error naming the setting to change (`zdr: false`) while those features use one.
  A chat that selects a subscription model stores no `zdr` flag, or `zdr: false`
  while the user requires ZDR.
- `claude_subscription` cannot be the main `provider` or `embeddings_provider`, and
  `models.embeddings`/`models.stt` reject `claude_subscription:` references: Claude
  Code has no embeddings or speech-to-text, so those stay on OpenRouter (or Ollama
  for embeddings). The STT picker never lists Claude models.
- In the web model pickers, subscription models are listed on their own
  **Claude subscription** tab (also listed, tagged, in the **Any** tab), labelled *experimental*; the ZDR checkbox
  only applies to OpenRouter rows. They have no per-token price; they count
  against the plan limits. Telegram (no provider tabs; its list merges the
  available providers) shows them with 🧪. A user who requires ZDR also sees
  *not ZDR* labels (and Telegram reports "Not ZDR (Claude subscription,
  experimental)" instead of "ZDR: enabled"); otherwise there is no ZDR wording.

## privacy and telemetry

By default every spawned CLI gets `DISABLE_TELEMETRY=1`, `DISABLE_ERROR_REPORTING=1`
and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, plus (always)
`DISABLE_AUTOUPDATER=1`, `DISABLE_FEEDBACK_COMMAND=1` and
`CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1`. The CLI identifies its caller as
`CLAUDE_CODE_ENTRYPOINT=recall`. The user's own `~/.claude/settings.json` is not
read for the run (`--setting-sources ''`) and is never modified.

Turning nonessential traffic off also stops the CLI's feature-flag fetch, which can
hide newer models from Claude Code's own model picker. The pinned aliases above
still work; set `claude_code_telemetry: true` only if you need a model that is
unlocked by a flag.

## limitations

- Experimental and version-sensitive: stream-json history replay and the
  `CLAUDE_CODE_EXTRA_BODY` generation fields are CLI interfaces, not a public SDK
  contract. Qualified with Claude Code 2.1.316.
- One CLI process per model call (about 1-4 s of start-up per call). Measured with
  Claude Code 2.1.316 and `haiku` on a 3-turn chat with a tool round and a ~3.6k-token
  system prompt: request 1 wrote 7,043 cached tokens; the three follow-ups read
  7,043, 7,455 and 7,509 tokens from cache (94-99% of their prompt) and wrote only
  the new turn (412, 54, 50 tokens).
- Not yet measured with real traffic: a turn in which an associative-memory hint was
  injected as a trailing system message. Its breakpoint covers the hint, which the
  next turn no longer replays, so that next request is expected to read only the
  system prompt and tools from cache and write the messages again.
- Images (inline data URLs) are supported; audio and video parts are replaced with a
  text note (video is decomposed into frames before it reaches the model).
  `reasoning_tokens` is ignored; reasoning effort maps to `--effort` (`minimal` →
  `low`). Models that cannot turn thinking off run at low effort when reasoning is off.
- No plan-limit (`/usage`) query and no live model discovery: the pickers show the
  pinned aliases; other Claude Code model names can be typed as
  `claude_subscription:<name>` in YAML or the Dream tab.
- Docker: not supported. The image has no Node.js, no Claude Code and no login;
  run RECALL directly on the host to use this provider (see [deployment](deployment.md)).
