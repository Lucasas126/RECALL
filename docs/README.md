# RECALL documentation

Documentation map. The root [README](../README.md) gives the short pitch; [AGENTS.md](../AGENTS.md) holds the rules for coding agents.

## getting started

- [Installation and first run](getting-started/installation.md): install, `.env`, creating users, running the web UI, Telegram and CLI.
- [New user guide](getting-started/new-user-guide.md): step-by-step onboarding for a new person (account, first agent, models, own Claude login, Telegram, memory, privacy, error cards).

## user guide

- [Agents](user-guide/agents.md): creating and editing agents in the web UI.
- [User settings](user-guide/settings.md): the Settings dialog, its sections, defaults and the reset button.
- [Models and providers](user-guide/models-and-providers.md): provider tabs, model references, defaults, per-chat override, favorites, ZDR.
- [Claude subscription](user-guide/claude-subscription.md): the experimental Claude login provider.
- [Memory](user-guide/memory.md): mind palace, associative memory, the index, user folders.
- [Dream mode](user-guide/dream.md): background self-review sessions.
- [Telegram](user-guide/telegram.md): access mapping, commands, panels.
- [Files and media](user-guide/files-and-media.md): images, audio, video, PDFs, OCR.
- [MCP servers](user-guide/mcp.md): external services such as Google Calendar.
- [RECALL knowledge](user-guide/recall-knowledge.md): the built-in, read-only skill that tells each agent how RECALL works, and its per-agent switch.
- [Terminal sandbox](user-guide/terminal.md): commands and file edits in an SSH sandbox, with approvals and an audit file.
- [Delegating tasks](user-guide/delegation.md): the `delegate` tool group, a worker in a hidden chat with limits (phase 1: synchronous).
- [Automations](user-guide/automations.md): things an agent does later (`at`, `every`, `cron`, when a process ends), started as unattended tasks; the service, the agent tools and the web tab are built.
- [Admin CLI](user-guide/admin-cli.md): `python -m src.admin` for users, agents, templates (the setup guide, which manages agents with the `agents` tools) and credentials (`set-secret`).

## reference

- [settings.yaml](reference/settings-yaml.md), [user.yaml and per-user files](reference/user-yaml.md), [agent.yaml](reference/agent-yaml.md)
- [Environment variables](reference/environment-variables.md)
- [HTTP API and error codes](reference/api.md)
- [Tools](reference/tools.md)

## architecture

- [Overview](architecture/overview.md): layers, channels, chat service, steering, cancellation.
- [Agents and users](architecture/agents-and-users.md): ownership, identity, isolation.
- [Sources](architecture/sources.md): source types, addressing, `SourceRegistry`.
- [Associative memory internals](architecture/associative-memory.md): indexing, chunking, concurrency.
- [Obsidian overlay](architecture/obsidian-overlay.md): safe/auto overlay design (planned).
- [Permission levels](architecture/permissions.md): levels L0-L5, approval rules, dream limits and known gaps (implemented).

## deployment

- [Docker](deployment/docker.md): image, Compose, releases, persistent state.
- [Security notes](deployment/security.md)

## project

- [Changelog](project/changelog.md): what changed, by topic, with upgrade notes.
- [Backlog](project/backlog.md): what is done and what is planned.

Conventions: see `.claude/rules/documentation.md`. Prefer updating the canonical page over adding a new one, and link instead of copying.
