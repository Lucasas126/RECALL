# plan: background processes, worker inheritance and small fixes

Status: step 5 (background processes) is **done** (2026-11-12); see the terminal guide. Each step is one branch and one commit (or a short series),
merged with `--no-ff` into `main`, with tests and docs in the same commit. Order is by dependency, not size.

## steps

### 1. docs and limits: reference pages up to 12 KB
- Raise the per-page limit of `src/knowledge/recall/references/` from 8192 to 12288 bytes, done in step 1 (`tests/test_recall_knowledge.py` lines 91 and 259, `AGENTS.md`).
- Docs: nothing else. Do not pad pages; the limit is a ceiling, not a target. Splitting by topic still comes first.

### 2. `agent_update` accepts any setting, always with an approval card (done)
- Today an agent may change only its description and SOUL (of itself); level, tool groups, rules, backends and `delegation` are refused and sent to other tools.
- Change (decided 2026-11-12): `agent_update` accepts any configuration key, also for the calling agent itself, and every change shows the user an approval card with before and after. The card is the control, not the key list.
- Hard caps stay in the service, not in the tool: a level never above the caller's or the user's maximum, a level is lowered but not raised by the agent itself, the fixed safety floor, and no secrets or credentials. Backends and users remain admin-only.
- Touches `src/service/agent_admin.py` (`PLAIN_SETTINGS`, `_PERMISSION_KEYS`, `_ACCESS_KEYS`, the self-change check), `src/agent/toolsets/agents.py` (docstring), `docs/architecture/permissions.md` and tests.

### 3. optional token cap per task (done)
- Default: no cap per task (`subagents.max_tokens` stops being a default of 200k; `0`/absent = unlimited). The per-user daily budget was removed (design decision, 2026-11-12): only time and parallelism limit tasks.
- `task_start(max_tokens=...)` lets the agent set a cap for one task; `agent.delegation.max_tokens` and the application setting stay as upper bounds when set.
- Changes: `TaskLimits.max_tokens` becomes optional, the checks in `src/service/tasks.py` (~1189 and ~1211), the notice text `DELEGATE_NOTICE`, the task record `limits`, docs (delegation guide, settings reference).

### 4. worker inheritance (`inherit`) (done)
Proposal, to confirm before coding:
- `task_start(inherit=true)` for an ephemeral worker: it gets the parent's tool groups except `delegate`, `agents`, `permissions` and `self` (the worker never changes configuration or launches tasks), the parent's model unless `model` is set, and the parent's terminal backends **with the parent's own approval rules** instead of the forced `manual` mode.
- Unchanged on purpose: depth 1, the level of the worker is never above the parent's, dangerous and protected actions stay denied for unattended runs, memory of the parent is not copied (the parent passes what matters in `context`).
- Existing agents (`agent=`) keep their own SOUL, groups and backends, as today.
- Default of `inherit`: **true** (decided 2026-11-12). It must be explained in the `task_start` docstring, in `DELEGATE_NOTICE` and in the shipped skill/knowledge page, so an agent knows when to pass `inherit=false` (a worker that reads untrusted pages, or a cheap read-only job).
- Risk to document: a worker that reads untrusted web pages now holds the parent's terminal rights. Dangerous-command denial and the audit log are the mitigation.

### 5. background processes (the large step)
Design from an existing implementation (process registry, rolling output buffer, poll/log/wait/kill, completion notification), adapted to RECALL's backends.

Tools (terminal group, same approval engine as `terminal`; a start is one action, polls are read-only):
- `process_start(command, backend, workdir, notify_on_complete=true)` returns an id at once.
- `process_list()`, `process_read(id, tail=N)` (status, exit code, last lines), `process_wait(id, timeout_s<=120)`, `process_stop(id)`.
- `terminal` keeps its foreground behaviour; its docstring points to `process_start` for long commands.

Mechanics:
- Local backend: `subprocess.Popen` in its own process group, stdout and stderr to a log file under the user's data folder (rolling cap ~200 KB), a watcher thread records the exit code.
- SSH backends: start the command on the remote machine with `nohup` writing to a log and a pid/exit file in a per-process directory (`~/.recall-proc/<id>/`); `read`, `wait` and `stop` run short commands over the same SSH path. No persistent connection needed.
- Registry: one JSON record per process (`id, chat_id, agent, backend, command, started, state, exit_code`) next to the task records, so a restart can mark orphans as `lost`.
- Notification: on completion with `notify_on_complete` the chat gets a message that wakes the agent, reusing the task wake mechanism (the origin process claims it). Default is on, because a silent background job is forgotten by the model (other agent frameworks documents this flaw).
- Limits: processes per chat and per user, maximum lifetime (default 2 h), output cap, stop on chat deletion. Same protected-path scan and audit as foreground commands.
- Out of scope for the first version: `watch_patterns`, stdin to a running process, checkpoint recovery of processes across a RECALL restart (local ones die with the container; remote ones survive and can be re-read by id).
- Sub-steps, one commit each: (a) registry and local backend, (b) tools and approvals, (c) SSH backends, (d) completion wake and UI hint (header indicator already exists for tasks), (e) docs, knowledge page and notice text.

### 6. `dev` suite run
Once step 5 lands, the full test suite (~150 s) runs with `process_start` instead of relying on the 600 s timeout; update `AGENTS.md`.

### 7. natural-language summaries on approval cards
- A small model writes one or two plain sentences saying what a command or action does, shown on the approval card next to the raw command (the raw command always stays visible and is what is approved).
- Model: a new application-wide setting in `settings.yaml` (not per user or agent), like the model that titles chats; default set, empty = no summary. Reuse the title-model plumbing and provider rules (no forced ZDR labels).
- Safety: the summary is advisory and untrusted (it can be wrong or be steered by the command text); label it as such, give the model only the command, backend and short context, cap its time (card must not wait on it) and tokens, and never use it to auto-approve.
- Changes: settings model and defaults, a summarizer in the approval path (`src/terminal/approvals.py`), card UI in `app.js`, docs, tests with a fake provider.
- Not started; order after step 5 unless User A moves it.

## parked (backlog, not in this plan)
- Dedupe of associative hints across turns (judged unnecessary after the 0.3.3 changes).
- Conversation search, scheduled jobs and proactive messages, ask-with-options, agent-to-agent messages, browser, vision, voice, connectors.
- Auto-approval of read-only commands for workers on `pc`/`laptop`/`orion` beyond what `inherit` gives.
- Passing files or skills to a fresh worker (today: paste into `context`).

## done in this run
- Default tool rounds per turn (`max_tool_hops`): 25 -> 50 (`4ee0502`).
