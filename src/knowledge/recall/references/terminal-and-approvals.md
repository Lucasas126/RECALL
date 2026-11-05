# Terminal and approvals

The `terminal` group runs commands and edits files on a **backend** chosen by the user: normally a sandbox, possibly the user's own PC, reached over SSH. It is off by default. The file tools do not use your memory sources, and the terminal is never built in disposable chats. With a `local` backend the commands DO reach RECALL's own `.config` and `data` folders: read `references/security-for-admins.md` before using one.

## Backends

| Type | Where commands run | Availability |
| --- | --- | --- |
| `ssh` | another machine, through the local `ssh` binary; key only, strict host-key checking | always |
| `local` | the RECALL host itself (in Docker: the container, as the app user, in `/app`), with a scrubbed environment | only if `terminal.allow_local: true` in `settings.yaml` or `RECALL_TERMINAL_ALLOW_LOCAL=true` in the server environment |

Backends are declared by the user in `terminal_backends` of the user yaml; each agent lists the ones it may use in its own `terminal.backends`, with a `workdir` and an `approvals` mode each. Your system prompt lists your allowed backends with working directory, approval mode and description; trust that list. A backend is a disposable sandbox only if its description says so; otherwise treat it as one of the user's real machines. Pass `backend` to choose one; without it the call goes to `terminal.default_backend`. A backend that fails to resolve is dropped with a log warning; with none left, the tools are not built.

Limits: foreground only; killed at the timeout (agent-wide `terminal.timeout_seconds`, default 60, capped by `terminal.max_timeout_seconds` in `settings.yaml`, default 600); each call is a fresh shell, only the backend's filesystem persists; output is stripped, redacted and truncated to `max_output_chars`.

## Approval modes

| `approvals` | Asks the user for |
| --- | --- |
| `manual` (default) | every command and every file operation, reads included |
| `smart` | only dangerous actions |
| `off` | nothing; the hardline still applies |

The user sees an approval card in the web chat or buttons in Telegram, and only the owner of the chat can answer. The first line of a `terminal` result says how it was authorized: `[approved by the user]`, `[ran without approval: smart mode, normal command]` (or `... nobody attached to approve]` when unattended) or `[approvals off]`, so you know whether the user saw it. A pending approval is denied automatically after `terminal.approval_timeout_seconds` (default 300), when the reply is cancelled, or when the service shuts down.

## Hardline and dangerous

Refused whatever anyone approves (the hardline): recursive `rm` of `/`, `~` or system directories, writing to block devices and formatting tools, shutdown or reboot commands, fork bombs, recursive permission changes on system directories. Asking in `manual` and `smart` (dangerous): `sudo` and account tools, recursive or system-path deletions, `dd`, permission changes, stopping services or killing processes, firewall and mount tools, `ssh`/`scp` from inside the backend, `git push --force`, `git reset --hard`, global package installs and removals, downloads piped into an interpreter, destructive SQL. The exact lists live in `src/terminal/policy.py`; they are a safety net, not a security boundary.

## Protected paths (local backend)

On a `local` backend, `terminal` and the `sandbox_*` tools refuse, in every approval mode and in unattended runs, the SSH private keys of all users' backends (`key_file`, usually `.config/keys/`), `.config/secrets.env`, `.env` files, Claude login `.credentials.json`, `/proc/*/environ` and the extra patterns in `settings.yaml` `terminal.protected_paths`. The text `Denied: protected path: ...` means this: nobody can approve it, so do not retry, rephrase, glob, copy, archive or script around it; tell the user instead. Public keys (`*.pub`), `known_hosts`, `user.yaml` and the directory listing of `keys/` stay allowed; recursive tools (`tar`, `grep -r`, `cp -r`, `find -exec`) may not start at a folder that contains protected files, so name narrower paths. A glob is resolved against the real files (relative to the working directory and earlier `cd`s) and refused only when an actual match is protected; the denial names it. A glob that cannot be resolved (variables, substitutions) is refused only if it could match a protected name such as `*.env` or `keys/*`. SSH backends are not affected. Audit decision: `denied:protected`.

## When a command is denied

1. Do not retry it, rephrase it, or split it to get around the refusal; the denial text says so.
2. Tell the user what you wanted to do, why, and the exact command or file change.
3. Offer a safer alternative (a read-only inspection, a path inside the working directory) and let the user decide.
4. If the user wants it automated, tell them the setting: the backend's `approvals` in the agent's `terminal` block (Tools tab). Never suggest `off` for a real machine.

## Unattended runs

When nobody can answer (no attached client, and every dream session) the policy never waits: `manual` denies everything, `smart` runs normal actions and denies dangerous ones, `off` runs everything except the hardline. Dreams use the terminal only with `terminal.allow_in_dream: true` and `terminal` in `dream.tools`.

## Audit and privacy

Every executed or refused action appends a JSON line to `data/<user_id>/<agent_id>/terminal_audit.jsonl` (decision, command redacted, exit code); you cannot read it. Commands and outputs go to the model provider like the rest of the conversation: never print secrets into your context. The user can test a backend with `python -m src.admin terminal-test --user <user_id> --backend <name>`.
