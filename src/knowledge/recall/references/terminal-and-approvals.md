# Terminal and approvals

The `terminal` group runs commands and edits files on a **backend** chosen by the user: normally a sandbox, possibly the user's own PC, reached over SSH. It is off by default. The file tools do not use your memory sources, and the terminal is never built in disposable chats. With a `local` backend the commands DO reach RECALL's own `.config` and `data` folders: read `references/security-for-admins.md` before using one.

## Backends

| Type | Where commands run | Availability |
| --- | --- | --- |
| `ssh` | another machine, through the local `ssh` binary; key only, strict host-key checking | always |
| `local` | the RECALL host itself (in Docker: the container, as the app user, in `/app`), with an allowlisted environment | only if `terminal.allow_local: true` in `settings.yaml` or `RECALL_TERMINAL_ALLOW_LOCAL=true` in the server environment |

Backends are declared by the user in `terminal_backends` of the user yaml; each agent lists the ones it may use in its own `terminal.backends`, with a `workdir` and an `approvals` mode each. Your system prompt lists your allowed backends; trust that list. A backend is a disposable sandbox only if its description says so; otherwise treat it as one of the user's real machines. Pass `backend` to choose one; without it the call goes to `terminal.default_backend`.

Limits: foreground only; killed at the timeout (agent-wide `terminal.timeout_seconds`, default 60, capped by `terminal.max_timeout_seconds` in `settings.yaml`, default 600); each call is a fresh shell, only the backend's filesystem persists; output is stripped, redacted and truncated to `max_output_chars`.

## Approval rules

Every action is judged in this order: (1) the fixed floor, hardline commands and protected paths, always refused; (2) the **approval rules**; (3) the person, if the verdict is `ask`. Rules come in three layers, each `approval_rules: {default: allow|ask|deny, rules: [...]}`: installation (`settings.yaml`), user (`user.yaml`) and agent (the agent yaml). A rule is `{verdict, tool, command, path, backend, backend_kind, dangerous, id, reason}`; all set fields must match. `tool` is `terminal`, `file_read` (`sandbox_read`, `sandbox_search`), `file_write` (`sandbox_write`, `sandbox_patch`) or `any`. `command` is a word prefix (`git status`) or a glob, matched against EVERY segment of the command (`&&`, `;`, `|`, `$(...)`, `bash -c`); a redirection (`> f`) is a segment of its own. `path` is a glob on the resolved target. `backend_kind` is `sandbox`, `machine` (default for ssh) or `local`. `dangerous: true` matches what the policy flags.

All matching rules from all layers are collected and the worst verdict wins: deny beats ask beats allow. With no match, the most specific `default` applies (agent over user over installation), else `ask`. The shipped default asks for everything. You cannot change rules; the user does.

The per-backend `approvals` still works as a shorthand for that backend: `manual` (default ask), `smart` (allow, ask for dangerous), `off` (allow; nothing asks while a person is attached). It is the default for that backend when the agent file sets it; matching rules still apply on top. A rule or default that allows everything on a `machine` or `local` backend is only valid for an `admin` agent.

The user sees an approval card in the web chat or buttons in Telegram, and only the owner of the chat can answer. The card names the rule that asked and offers: approve once, always in this session (that exact action, this chat only), always for this agent (saves an exact `allow` rule with `exact: true`, one per command segment, or the exact path of a file operation, on that backend; the user can list and reset these in the agent form or with `agent rules`) and deny. "Always" is never offered for dangerous actions, and a rule never lifts the floor. If saving the rule fails, the action is approved once. The first line of a `terminal` result says how it ran: `[approved by the user]` (or `earlier in this session`), `[ran without approval: allowed by user:0]` (the rule), `[ran without approval: smart mode, normal command]` or `[approvals off]`. A pending approval is denied after `terminal.approval_timeout_seconds` (default 300), on cancel or shutdown.

## Hardline and dangerous

Refused whatever anyone approves (the hardline): recursive `rm` of `/`, `~` or system directories, writing to block devices, formatting tools, shutdown or reboot, fork bombs, recursive permission changes on system directories. Dangerous (`smart` asks; rules can match it with `dangerous: true`): `sudo` and account tools, recursive or system-path deletions, `dd`, permission changes, stopping services or killing processes, firewall and mount tools, `ssh`/`scp` from the backend, `git push --force`/`reset --hard`, global package changes, downloads piped into an interpreter, destructive SQL. The lists (`src/terminal/policy.py`) are a safety net, not a security boundary.

## Protected paths (local backend)

On a `local` backend, `terminal` and the `sandbox_*` tools refuse, in every approval mode and in unattended runs, the SSH private keys of all users' backends (`key_file`, usually `.config/keys/`), `.config/secrets.env`, `.env` files, Claude login `.credentials.json`, `/proc/*/environ` and the extra patterns in `settings.yaml` `terminal.protected_paths`. The text `Denied: protected path: ...` means this: nobody can approve it, so do not retry, rephrase, glob, copy, archive or script around it; tell the user instead. Public keys (`*.pub`), `known_hosts`, `user.yaml` and the directory listing of `keys/` stay allowed; recursive tools (`tar`, `grep -r`, `cp -r`, `find -exec`) may not start at a folder that contains protected files, so name narrower paths. A glob is refused only when an actual match is protected (the denial names it), or when it cannot be resolved (variables, substitutions) and could match a protected name such as `*.env`. SSH backends are not affected. Audit decision: `denied:protected`.

## When a command is denied

1. Do not retry it, rephrase it, or split it to get around the refusal; the denial text says so.
2. Tell the user what you wanted to do, why, and the exact command or file change.
3. Offer a safer alternative (a read-only inspection, a path inside the working directory) and let the user decide.
4. If the user wants it automated, tell them the setting: a narrow allow rule in `approval_rules` (an exact command and a backend), or the backend's `approvals`. Never suggest `off` or an allow-everything rule for a real machine.

## Unattended runs

When nobody can answer (no attached client, and every dream session) the policy never waits: every `ask` becomes a denial, `allow` still runs. So `manual` denies everything, `smart` runs normal actions and denies dangerous ones, and `off` acts like `smart` and also denies file writes. Dreams never get the terminal.

## Workers launched with `task_start`

An agent can hand a worker some of its backends with `task_start(backends=[...])` (see the delegation page). For the worker those backends are always `manual`, even if the agent's mode is `off` or `smart` (`local` too); allow, ask and deny rules still apply to it, and a worker never gets a backend its parent lacks. Its approval cards appear in the chat that launched the task (not relayed across channels), labelled "Task <title> (ephemeral) wants to run:", offer approve once or deny only, and the task's own timeout is paused while it waits. Hardline, dangerous detection and protected paths apply unchanged. A worker nobody watches follows the unattended rules above.

## Audit and privacy

Every executed or refused action appends a JSON line to `data/<user_id>/<agent_id>/terminal_audit.jsonl` (decision, command redacted, exit code, and the `layer`, `rule`, `rule_verdict`, `approval_id` and answer `scope` that decided; rule changes are `event: approval_rule` lines); you cannot read it. Commands and outputs go to the model provider like the rest of the conversation: never print secrets into your context. The user can test a backend with `python -m src.admin terminal-test --user <user_id> --backend <name>`.
