# Context compaction: plan

Status: in progress (branch `feat/compaction`). Replaces the message-count trim (`conversation_messages_retained`).

## Goal

When a chat's context grows past a threshold, a small model summarises the older part of the conversation from the
agent's own perspective, and that summary replaces it in the context. The system prompt is never touched. The full
history stays in the chat's jsonl, and the agent is told it can read it with `session_search`.

## Decisions

- Trigger by tokens: the real input tokens of the last model call (`usage.in`, already persisted per turn).
- Per agent, `chat_config.compaction`:
  - `mode`: `percent` (of the model's context window) or `tokens` (absolute).
  - `trigger`: percent (default 50) or tokens; `keep_recent`: the protected tail, same unit (default 20 % / tokens).
  - `model`: summary model, default `claude_subscription:haiku` (the user's own Claude subscription).
  - `enabled`: default true.
- Protected: the whole system prompt, and a recent tail sized in tokens that never starts on an orphan tool
  result and always keeps the latest user message.
- Summarised: everything between the prompt and the tail, plus the previous summary (incremental).
- The summary is written in first person by the agent ("My notes on the earlier part of this chat ...") and injected
  as a `system` message right after the system prompt (a leading assistant message is invalid for Anthropic).
  It ends with a note that the conversation was compacted and the full history is readable with
  `session_search` (quoted text for exact matches).
- Content of the summary: what the user asked, what I did (files, commits, commands, results), decisions and why,
  open items, preferences the user stated, facts needed to continue.
- Runs after the reply is sent; the next turn waits for it if it is still running.
- Persisted as a `compaction` turn in the chat jsonl: summary, index of the first kept turn, tokens before and after,
  model, ts. Loading a chat seeds from the last compaction plus the turns after it.
- Web UI: a permanent divider in the chat ("Conversation compacted, 182K -> 41K tokens (-77 %)"), also after reload.
  Telegram: silent.
- `/compact [instructions]` command in web and Telegram, answered by RECALL (like `/dream`); optional instructions
  tell the summariser what to keep.
- Failure: retry once; if it still fails and the context passes 90 % of the window, fall back to cutting the oldest
  messages with the `[CUTOFF CONVERSATION]` marker so the chat keeps working.
- Removed: `conversation_messages_retained` (agent config, loader, form defaults, UI, docs) and
  `prompt_cache.trim_slack_percent`.
- Not done: dream hand-off before compacting (dreams already review chats).

## Related change

- Default small models move to the user's Claude subscription Haiku: `chat_naming.model` and
  `associative_memory.query_model` become `claude_subscription:haiku`.

## Steps

1. Config: `CompactionConfig` in `ChatConfig`, drop `conversation_messages_retained` / `trim_slack_percent`,
   loader, field defaults, settings.yaml, Haiku defaults.
2. Core: `src/agent/compaction.py` (split, prompt, summary call, record), agent wiring (load, trigger, context).
3. Persistence and API: `compaction` turn in the jsonl, exposed in the chat history endpoint.
4. `/compact` command: service, web, Telegram.
5. Web UI: divider with the token reduction; remove the retained-messages field from the agent form.
6. Tests, docs (agent-yaml, settings-yaml, tools, overview, built-in manual, backlog), full suite, commit.

Later (separate branch): `/usage` command and the context/subscription meter next to the input box.
