# RECALL documentation

Documentation map. The root [README](../README.md) gives the short pitch; [AGENTS.md](../AGENTS.md) holds the rules for coding agents.

## getting started

- [Installation and first run](getting-started/installation.md): install, `.env`, creating users, running the web UI, Telegram and CLI.
- [New user guide](getting-started/new-user-guide.md): step-by-step onboarding for a new person (account, first agent, models, own Claude login, Telegram, memory, privacy, error cards).

## user guide

- [Agents](user-guide/agents.md): creating and editing agents in the web UI.
- [Models and providers](user-guide/models-and-providers.md): provider tabs, model references, defaults, per-chat override, favorites, ZDR.
- [Claude subscription](user-guide/claude-subscription.md): the experimental Claude login provider.
- [Memory](user-guide/memory.md): mind palace, associative memory, the index, user folders.
- [Dream mode](user-guide/dream.md): background self-review sessions.
- [Telegram](user-guide/telegram.md): access mapping, commands, panels.
- [Files and media](user-guide/files-and-media.md): images, audio, video, PDFs, OCR.
- [MCP servers](user-guide/mcp.md): external services such as Google Calendar.

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

## deployment

- [Docker](deployment/docker.md): image, Compose, releases, persistent state.
- [Security notes](deployment/security.md)

## project

- [Backlog](project/backlog.md): what is done and what is planned.

Conventions: see `.claude/rules/documentation.md`. Prefer updating the canonical page over adding a new one, and link instead of copying.
