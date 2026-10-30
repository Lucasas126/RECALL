ui interface with chat:
- option to create new chats: context is empty, except for the current agent system prompt
- option to choose from the different models the user has access to
- option to view and manage existing chats: users can see a list of their previous conversations, with the ability to delete or continue them

## status

implemented (`uv run python web.py`, then open the configured host/port, default http://127.0.0.1:8000):
- login by user id (identification only, see [users.md](users.md)): shows that user's agents.
- choose among the user's agents, create new chats, continue and delete existing ones (list per agent; title = first message). chats that were created but never used are removed automatically when the list is refreshed or another chat is created (the open chat is kept).
- streamed replies rendered as markdown (headings, lists, code, tables, links; sanitized, user messages stay plain text), with a "thinking" block (open while streaming; closing one keeps them closed), the agent's actions (tool calls, each with its arguments and a truncated result; also stored with the turn so they show when reopening a chat) and the context usage report.
- api: `/api/login`, `/api/logout`, `/api/me`, `/api/agents/<agent>/chats[/<chat>[/messages]]` (replies are server-sent events: `reasoning`, `text`, `tool`, `usage`, `error`, `done`).

planned: choosing the model per chat (today the model is fixed per agent in its yaml), renaming chats, real authentication, telegram channel (see [architecture.md](architecture.md)). the frontend bundles `marked` and `DOMPurify` under `src/web/static/vendor/` (no CDN, no build step).
