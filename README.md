# RECALL

RECALL is a Python framework for deploying per-user, per-agent AI assistants with memory and tools. Users talk to their agents
through a web UI, a Telegram bot or a terminal client; models come from OpenRouter, Ollama or (experimental) a user's own Claude login.

Main ideas:

- **mind palace**: a private folder where an agent keeps its memories, notes and recollections; the user does not normally read it.
- **associative memory**: before each reply a small subagent turns the conversation into a short query, a vector index is searched, and the best fragments are suggested to the agent. The index covers the palace, skills, conversations and user folders, and is kept current in the background. See [memory](docs/user-guide/memory.md).
- **dream mode**: background sessions (scheduled or `/dream start`) in which the agent reviews its recent conversations and curates its own files: `SOUL.md` personality, memories, skills, palace and journal. See [dream mode](docs/user-guide/dream.md).
- **user folders**: folders on the server (for example an Obsidian vault) that an agent may read and, optionally, edit (`folders:` in the agent config).
- **tools**: memory tools, web search and extraction (Tavily), self-editing tools and folder tools, chosen per agent in tool groups; external services such as Google Calendar through MCP servers (shared catalog, per-user accounts, enabled per agent). See [tools](docs/reference/tools.md) and [MCP servers](docs/user-guide/mcp.md).
- **multi-user**: every user owns their agents, data and provider key; nothing is shared implicitly. See [agents and users](docs/architecture/agents-and-users.md).

Planned, not implemented yet: recall mode, the Obsidian safe/auto overlay, reminders and automations. See the [backlog](docs/project/backlog.md).

## Documentation

Start at the [documentation map](docs/README.md).

- New person on an existing install: the [new user guide](docs/getting-started/new-user-guide.md).
- Administrator setting RECALL up: [installation and first run](docs/getting-started/installation.md).
- Configuration: [settings.yaml](docs/reference/settings-yaml.md), [user.yaml](docs/reference/user-yaml.md), [agent.yaml](docs/reference/agent-yaml.md), [environment variables](docs/reference/environment-variables.md).

## Fonts

The web UI bundles Fraunces, Inter and Geist Mono (`src/web/static/fonts/`), all under the SIL Open Font License 1.1; see
[LICENSES.md](src/web/static/fonts/LICENSES.md) and [OFL.txt](src/web/static/fonts/OFL.txt). Nothing is loaded from a CDN.

## Tesseract OCR

Scanned PDF pages need the Tesseract OCR program installed on the machine that runs RECALL.
Windows: `winget install --id UB-Mannheim.TesseractOCR -e`. For macOS, Linux and the rest of the setup,
see [files and media](docs/user-guide/files-and-media.md#tesseract-ocr).

## Docker deployment

A Docker image (web UI + Telegram bot, with Tesseract and ffmpeg included) is published to GHCR when a
GitHub Release is published, and a Compose file with Watchtower keeps a home server on the latest release.
See [Docker deployment](docs/deployment/docker.md).
