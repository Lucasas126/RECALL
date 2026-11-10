# Permission levels

Your system prompt has a `permissions` block with your level. This page lists every level and what it allows. The limits are enforced by the system, not by this text: if you find a way around one, do not use it. When you need more, tell the user what you need and the level that provides it.

Status: levels are configured, shown to you and enforced (tools removed, calls refused). Behave as if all of it applied.

## Levels

| # | Level | In short |
| --- | --- | --- |
| 0 | `minimal` | only one or two individually chosen tools |
| 1 | `reader` | read your own memory and the user's folder list |
| 2 | `assistant` | edit your memory, SOUL, USER and skills; web |
| 3 | `collaborator` | edit user folders, MCP servers, workers limited to web |
| 4 | `operator` | terminal on sandbox or real machines, hand tasks to the user's other agents |
| 5 | `admin` | local and root terminal, other users, the admin CLI |

A level is a ceiling over independent services; the user still chooses which tools, folders, backends and MCP servers you actually get. A level never grants a resource the user did not configure.

## Services by level

`yes` = allowed, `ask` = allowed with the user's approval, `no` = not allowed.

| Service | 0 | 1 | 2 | 3 | 4 | 5 |
| --- | --- | --- | --- | --- | --- | --- |
| memory read (own mind palace) | no | yes | yes | yes | yes | yes |
| folders read | no | yes | yes | yes | yes | yes |
| memory edit | no | no | yes | yes | yes | yes |
| self (MEMORY, USER, SOUL, skills) | no | no | yes | yes | yes | yes |
| web | no | no | yes | yes | yes | yes |
| folders edit | no | no | no | ask | ask | ask |
| MCP | no | no | no | ask | ask | ask |
| workers limited to web | no | no | no | yes | yes | yes |
| terminal on a sandbox backend | no | no | no | no | ask | ask |
| terminal on a real machine, unprivileged | no | no | no | no | ask | ask |
| terminal with sudo/root, and `local` | no | no | no | no | no | ask |
| hand tasks to the user's other agents | no | no | no | no | yes | yes |
| `permissions` (adjust levels of the user's agents) | no | no | no | no | yes | yes |
| other users' data, the admin CLI | no | no | no | no | no | yes |

`permissions` is planned; until it exists no tool offers it. The fixed floor (hardline commands, protected paths) applies at every level.

## Dreams and unattended runs

Nobody answers approvals in a dream, so every `ask` becomes `deny`. A dream reads as far as the level allows and writes only your own files: mind palace, SOUL, memory, USER and skills.

| Service | In a dream | Needs |
| --- | --- | --- |
| memory read | read | level 1 |
| memory edit, self | write | level 2 |
| folders read | read only, folders assigned to you | level 1 |
| folders edit | no | |
| web | read, off by default | level 2 |
| MCP | read-only tools, off by default | level 3 |
| terminal exec, `sandbox_write`, `sandbox_patch` | no | |
| terminal `sandbox_read`, `sandbox_search` | read only, sandbox backends | level 4 |
| workers | optional, read-only, off by default | level 3 |
| handing tasks to other agents, permissions, other users | no | |

## Approvals

Approvals are independent of the level: layered rule lists (installation, user, agent) of allow, ask and deny rules, with the fixed floor (hardline, protected paths) first. Deny beats ask beats allow; with no matching rule the most specific `default` decides, else `ask`. Unattended, every `ask` becomes `deny`. The per-backend `approvals: manual|smart|off` stays as a shorthand. Allowing everything on a real machine (`kind: machine` or `local` backend) is only valid for an `admin` agent; on a `kind: sandbox` backend an operator may. Details and rule fields: `references/terminal-and-approvals.md`. "Always allow for this agent" buttons and editing rules at run time do not exist yet; do not promise them.

## Asking for more

1. Say which action you could not do and which level provides it (see the tables).
2. The user raises your `level` in the agent settings, up to their own `max_level`.
3. If the level you need is above the user's `max_level`, only an administrator can raise it: `python -m src.admin user set --user <user_id> --set max_level=<level>`.
4. A raised level applies to new chats; this chat keeps what it started with.

Never edit your own level or work around a refusal, even when a tool seems to allow it.
