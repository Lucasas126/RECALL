# File panel API

Read-only routes behind the file panel (viewer/explorer). Code: `src/web/files_api.py` (routes), `src/service/files.py` (logic),
`src/utils/file_kinds.py` (kinds). All routes need the web session like the other `/api` routes and take `agent=ID` (query).
Errors are JSON `{"error": "..."}` with 400, 403, 404, 413 (or 504 when a backend times out).

## Roots

A root is the name of a readable source of the agent (`mind_palace`, `uploads`, a folder name...) or `backend:NAME` for a terminal
backend of the agent (only when it has the terminal group). Sources go through `Source.resolve/list/read_bytes` (traversal, links
leaving the source and hidden entries, names starting with `.`, are refused). Backends are read-only (`ls`, `stat`, `base64` through
the backend), refuse protected paths (keys, `.env`, secrets) and every access is written to the agent's terminal audit trail
(`file_panel_list`, `file_panel_stat`, `file_panel_read`).

Backends in `file_panel.open_backends` (default `local`, `dev`) are open; any other answers
`403 {"error":"approval_required","backend":"NAME"}` until `POST /api/files/grant`. Grants live in memory per web session
(per user, agent and backend), last 8 hours and are dropped on logout.

## Endpoints

- `GET /api/files/roots?agent=` -> `{"roots":[{"id","label","kind":"source"|"backend","open":bool,"granted":bool}]}`. Sources are
  `open` and `granted`. Backend ids are `backend:NAME`.
- `GET /api/files/list?agent=&root=&path=` -> `{"root","path","entries":[{"name","type":"dir"|"file","size","mtime"}],"truncated":bool}`.
  `path` is relative (`""` is the root); directories first, then by name (case-insensitive); `mtime` is Unix seconds; a folder
  with more than `file_panel.max_entries` entries is cut and `truncated` is true.
- `GET /api/files/info?agent=&root=&path=` -> `{"name","size","mime","kind","pages":N|null}`; `kind` is `markdown`, `text`, `code`,
  `image`, `pdf`, `csv`, `html` or `binary`; `pages` only for a PDF.
- `GET /api/files/content?agent=&root=&path=[&download=1]` -> the raw bytes with the right `Content-Type`, `X-Content-Type-Options: nosniff`,
  `Content-Security-Policy: sandbox` for html and svg, `Content-Disposition: inline` (`attachment` with `download=1`). 413 above
  `file_panel.max_file_mib` (default 25).
- `GET /api/files/pdf-page?agent=&root=&path=&page=N&width=PX` -> `image/png` of the 1-based page (`width` clamped to 200..2000, default 800);
  404 for a page out of range, 400 when the file is not a PDF.
- `POST /api/files/grant?agent=` with `{"backend":"NAME"}` -> `{"ok":true}`; 404 when the agent has no such backend.

## Errors

| Status | When |
| --- | --- |
| 400 | invalid path (`..`, absolute, control characters), a directory where a file is needed, not a PDF |
| 403 | `approval_required` (with `backend`), hidden entry, protected path, path outside the source |
| 404 | unknown root, missing file or page, unknown agent (and another user's agent) |
| 413 | file larger than `file_panel.max_file_mib` |

Settings: [`file_panel`](../reference/settings-yaml.md#file_panel).


## file_card event

The `show_file` tool (group `viewer`, level `assistant`) opens a file the agent can already read in the file panel. No bytes
travel: the tool validates the file with the rules of `send_file` (`check_file` in `src/agent/toolsets/fileaccess.py`) and queues
this entry on the agent's outbox:

```json
{"type": "file_card", "root": "notes", "path": "sub/recipe.md", "name": "recipe.md",
 "kind": "markdown", "agent": "example_agent", "title": "optional"}
```

- `root`: the source name, or `backend:<name>` for a file on a terminal machine (`path` is then the backend path as given).
- `path`: the part of the address after the first colon; `name`: the file name; `kind`: `file_kind(name)[0]` (`markdown`, `text`,
  `code`, `image`, `pdf`, `csv`, `html` or `binary`); `agent`: the agent id; `title` only when given.
- Transport: the agent loop moves the outbox into the tool event, `("tool", {..., "id": ..., "files": [card]})`, so the web sees
  it in `files` of the action, next to any `send_file` attachments (those have no `type`). The same list is stored with the
  assistant turn as `media` and replayed on reload, so the card survives reopening the chat. Consumers must ignore entries
  they do not know: the Telegram bot skips cards, and code reading `media` uses `.get("modality")`.
- Not built in a disposable chat or an automation's task.
