# Memory, dream and disposable chats

## What you remember

| Where | What | Limit | In the prompt |
| --- | --- | --- | --- |
| `SOUL.md` | identity, personality, lasting rules | `memory_limits.soul_chars` (15000) | always |
| `memories/MEMORY.md` | your own notes and lessons | `memory_limits.memory_chars` (2500) | always, with a used/limit counter |
| `memories/USER.md` | what you know about the user | `memory_limits.user_chars` (2500) | always, with a counter |
| `skills/<name>/SKILL.md` | procedures you wrote; frontmatter `name` and `description`; supporting files under the folder | 50000 chars per file | index only; load with `skill_view` |
| `mind_palace/` | free-form notes; `for_user.md` holds questions, ideas and findings for the user; `dream_tasks.md` holds work queued for your next dream | none | up to 3 open `for_user.md` items |
| `conversations/` | every chat as JSONL | none | the current chat only |

The limits are set in `settings.yaml` for every user and agent. Memory entries are separated by a line holding only `§`; use the `memory` tool instead of editing the file by hand. Put facts in memory, procedures in skills, personality in SOUL.

Keep MEMORY and USER for what you need in every chat, cannot get with a tool and is not about one chat; move everything else to the palace and leave a pointer. When a file is full, move, never delete. The palace layout is yours to design (a good one: `projects/<name>/status.md`, `decisions/`, `journal/YYYY-MM.md`, `reference/`); every chat prompt carries a short "Memory habits" block with this.

## Parallel writers

Your other chats, your remember tasks and your dream may write the same files while you work. Every write tool waits its
turn on your agent's lock, so changes never interleave, and files are replaced atomically.

- `append`, `edit`, `skill_manage patch`, `soul_edit patch` and `memory` work on the current file: safe at any time. An
  `old_string not found` may mean someone changed it: read again.
- `write` (and `folder_write`, `soul_edit replace`, `skill_manage edit`/`write_file`) replaces the whole file, so it is refused
  if the file exists and you have not read it in this chat, or it changed since you read it. Read it again, redo the change on
  the new text, write. Prefer `append`/`edit` for small changes.
- `delete` is refused when you read the file and it changed since.

## Frozen prompt per chat

The system prompt is built when a chat starts: SOUL, then memories, skills index and open `for_user.md` items, then notices (associative memory, disposable chat, terminal, MCP). Your edits reach the files at once but only new chats see them; the tool results say so. Do not expect to see your own change in this conversation. The last line of the profile blocks, `Profile loaded <time>; SOUL <n> chars, <k> skills (own) + built-in recall`, tells you when this chat's profile was read: changes apply to new chats, so compare it with the time of an edit.

## Associative memory

Before each reply RECALL searches your vector index and may add the best fragments to your context.

- `chat_config.associative_memory`: `off`, `llm` (a small model proposes the query) or `question_only` (the user's message is searched as is).
- `chat_config.associative_execution`: `parallel` (default; if hits arrive while you are writing, your reply may pause at a sentence end and continue with them) or `sequential` (search first).
- Framework settings (`associative_memory` block in `settings.yaml`): `context_messages`, `top_n`, `score_threshold`, `query_model`, `api_key`, `zdr`. Each user can set a maximum number of matches in Settings.
- The index covers the mind palace, `agent:skills/`, conversations, folders with `indexed: true` and, while `chat_config.recall_knowledge` is on, this manual as the read-only source `recall_docs` (e.g. `recall_docs:references/permissions.md`; it is purged from the index when the flag goes off). It does not cover SOUL or the memory files (already in the prompt). The current chat is never suggested back.
- Files the user sends in chats are also saved as originals (read-only source `uploads`, address `uploads:<chat_id>/<file>`, not indexed) for agents that can read files; the message shows the path. They are removed after `uploads.retention_days` (settings.yaml, default 30).
- The index is updated in the background: after each reply, at chat and dream start (a stat-only scan), and for user folders on an interval. If the embeddings model or chunking changes it is rebuilt automatically.
- If the search fails, the turn continues without it and the web shows "skipped".
- Clicking the web indicator opens a dialog with the small model's query and the injected fragments (address, heading, score, the excerpt the model got). Turns keep them as `associative_fragments` (address/label/score/excerpt only, never full chunks) and `associative_query`.

## Dream sessions

A dream is a background session where you review recent chats and curate your own files, with no user in the loop. It is started by the schedule (`dream.enabled` with `every_hours` or `daily_at`), by `/dream start` (Telegram also has `/dreamstart`, `/dreamcancel`) or from the web Dream tab.

- One dream per agent at a time (`dream/.lock`); scheduled runs are skipped while nothing is pending. A running session can be cancelled.
- The review queue lists chats with unreviewed turns, the most recently active first, so a later fix is read before an earlier worry; disposable chats are never in it. Mark each chat reviewed when done.
- Dream tasks: when the user asks for something to be done in a dream (research, a self-improvement), add `- [ ] task (date, why)` to `mind_palace:dream_tasks.md`. A dream lists the open ones at the start, does them after the review, and checks each off with a link to its result note (`- [x] task -> mind_palace:<note>`); `dream_finish` refuses once while a listed task is still open. A scheduled dream also runs when only tasks are pending.
- The session ends when you call `dream_finish(summary)`. Replying without tool calls does not end it: RECALL tells you what is still pending and asks you to go on (after 3 such replies in a row it closes anyway). There is no need to rush: work until the queue is done, then finish.
- Limits per session: `dream.max_tokens` (0 = no cap), `dream.max_minutes` (a warning one minute before, then it is force-closed).
- Context compaction: near 55% of the model's window RECALL warns once (save what you learned and mark finished chats now); after your next step the older part becomes notes plus the exact review progress. Save after every chat so nothing depends on the notes.
- Tools: the review tools, memory read tools, `memory`, `skill_view`, `skill_manage`, `soul_edit`, and edits to `mind_palace` only. Write tools need the agent's `memory_edit` / `self` groups. Folders (read-only; also need a folder read group) and web only if enabled in `dream.tools`. Never a terminal. Chat text is shown to the dream as untrusted data; disposable and task chats are not searchable or readable.
- Workers in dreams: with `dream.delegate`, the `delegate` group and level collaborator+, `dream_delegate(chat_ids, focus)` hands up to 6 pending chats to read-only workers (fresh context each, notes with turn numbers back). Use it for long chats; you save and mark. Their tokens count toward the budget.
- Rereading chats already reviewed (a dream task may ask for it): `dream_read_chat(chat_id, from_turn=1)` and continue from `read_until` + 1, or `dream_delegate(chat_ids, from_turn=1)` for long ones. Clean whole turns, nothing marked. Never `read` a `conversations:` file for this: raw records cut at 2000 characters, with attachment data.
- In a dream you keep your SOUL, memories and skills index: the dream prompt is your normal profile plus the dream instructions.
- Typical uses: file what you learned in the mind palace (one note per topic, listed in `mind_palace/INDEX.md`), keep MEMORY/USER as short facts plus pointers to palace notes (move detail to the palace when a file is near its limit), journal in `mind_palace/journal/YYYY-MM.md`, add or clear entries in `for_user.md`, do and check off `dream_tasks.md` items, turn a repeated task into a skill, small SOUL changes.
- Traces are in `dream/sessions/<ts>.jsonl` and a summary in `<ts>.json`; the user sees them in Dream sessions. The built-in `recall` skill is readable in dreams.

## Disposable chats

A disposable chat is one the agent does not learn from. It is skipped by dreams and by the index, deleted after 30 days of inactivity by default (user setting `disposable_retention_days`, 1-365), and your persistent state is read-only there (see `references/tools-and-groups.md`). If the user wants something remembered, they can convert the chat to a normal one.

## Hidden chats

A hidden chat is only left out of the web chat list (unless the eye button at the right of the tabs is on, or the API gets `include_hidden=true`). The chat an @mention opens with the mentioned agent is born hidden. Unlike a disposable chat it is indexed, searched and reviewed by dreams as usual.
