# Memory, dream and disposable chats

## What you remember

| Where | What | Limit | In the prompt |
| --- | --- | --- | --- |
| `SOUL.md` | identity, personality, lasting rules | `memory_limits.soul_chars` (15000) | always |
| `memories/MEMORY.md` | your own notes and lessons | `memory_limits.memory_chars` (2500) | always, with a used/limit counter |
| `memories/USER.md` | what you know about the user | `memory_limits.user_chars` (2500) | always, with a counter |
| `skills/<name>/SKILL.md` | procedures you wrote; frontmatter `name` and `description`; supporting files under the folder | 50000 chars per file | index only; load with `skill_view` |
| `mind_palace/` | free-form notes; `questions.md` holds things you want to ask | none | up to 3 open questions |
| `conversations/` | every chat as JSONL | none | the current chat only |

The limits are set in `settings.yaml` for every user and agent. Memory entries are separated by a line holding only `§`; use the `memory` tool instead of editing the file by hand. Put facts in memory, procedures in skills, personality in SOUL.

## Frozen prompt per chat

The system prompt is built when a chat starts: SOUL, then memories, skills index and open questions, then notices (associative memory, disposable chat, terminal, MCP). Your edits reach the files at once but only new chats see them; the tool results say so. Do not expect to see your own change in this conversation. The last line of the profile blocks, `Profile loaded <time>; SOUL <n> chars, <k> skills (own) + built-in recall`, tells you when this chat's profile was read: changes apply to new chats, so compare it with the time of an edit.

## Associative memory

Before each reply RECALL searches your vector index and may add the best fragments to your context.

- `chat_config.associative_memory`: `off`, `llm` (a small model proposes the query) or `question_only` (the user's message is searched as is).
- `chat_config.associative_execution`: `parallel` (default; if hits arrive while you are writing, your reply may pause at a sentence end and continue with them) or `sequential` (search first).
- Framework settings (`associative_memory` block in `settings.yaml`): `context_messages`, `top_n`, `score_threshold`, `query_model`, `api_key`, `zdr`. Each user can set a maximum number of matches in Settings.
- The index covers the mind palace, `agent:skills/`, conversations and folders with `indexed: true`. It does not cover SOUL or the memory files (already in the prompt). The current chat is never suggested back.
- The index is updated in the background: after each reply, at chat and dream start (a stat-only scan), and for user folders on an interval. If the embeddings model or chunking changes it is rebuilt automatically.
- If the search fails, the turn continues without it and the web shows "skipped".

## Dream sessions

A dream is a background session where you review recent chats and curate your own files, with no user in the loop. It is started by the schedule (`dream.enabled` with `every_hours` or `daily_at`), by `/dream start` (Telegram also has `/dreamstart`, `/dreamcancel`) or from the web Dream tab.

- One dream per agent at a time (`dream/.lock`); scheduled runs are skipped while nothing is pending. A running session can be cancelled.
- The review queue lists chats with unreviewed turns; disposable chats are never in it. Mark each chat reviewed when done.
- Limits per session: `dream.max_steps`, `dream.max_tokens`, `dream.max_minutes` (a warning one minute before, then it is force-closed).
- Tools: the review tools, memory read tools, `memory`, `skill_view`, `skill_manage`, `soul_edit`, and edits to `mind_palace` only. Write tools need the agent's `memory_edit` / `self` groups. Folders (read-only; also need a folder read group) and web only if enabled in `dream.tools`. Never a terminal. Chat text is shown to the dream as untrusted data; disposable and task chats are not searchable or readable.
- Typical uses: journal in `mind_palace/journal/YYYY-MM.md`, add or answer entries in `questions.md`, tidy MEMORY/USER, turn a repeated task into a skill, small SOUL changes.
- Traces are in `dream/sessions/<ts>.jsonl` and a summary in `<ts>.json`; the user sees them in Dream sessions. The built-in `recall` skill is readable in dreams.

## Disposable chats

A disposable chat is one the agent does not learn from. It is skipped by dreams and by the index, deleted after 30 days of inactivity by default (user setting `disposable_retention_days`, 1-365), and your persistent state is read-only there (see `references/tools-and-groups.md`). If the user wants something remembered, they can convert the chat to a normal one.
