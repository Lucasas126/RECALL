# installation and first run

For the administrator who sets RECALL up. A person who only uses an existing install should read the
[new user guide](new-user-guide.md). To run on a server with Docker, see [Docker deployment](../deployment/docker.md).

## requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- An [OpenRouter](https://openrouter.ai) API key (or a local [Ollama](https://ollama.com) server) for chat and embeddings.
- Optional: Tesseract (scanned PDFs), `ffmpeg` (video input), Node.js (MCP servers such as Google Calendar and the experimental
  Claude login), a Tavily key (web search). See [files and media](../user-guide/files-and-media.md#tesseract-ocr).

## install

```bash
uv sync
```

Create a `.env` file in the repository root (it is ignored by Git; never commit it). Names only here, never values in docs or YAML:

```dotenv
RECALL_SESSION_SECRET=<long random value>   # signs the web session cookie
OPENROUTER_ALICE_API_KEY=<key of the user>  # any name; user.yaml refers to it
TELEGRAM_BOT_TOKEN=<token>                  # only if you use Telegram
TELEGRAM_ALLOWED_USERS=<telegram_id>=<user_id>,...
TAVILY_API_KEY=<key>                        # optional, web search for every agent
```

All variables are listed in [environment variables](../reference/environment-variables.md).

## create a user

Users are created by hand; there is no sign-up page. Copy `.config/example_user/` to `.config/<user_id>/` and edit
`user.yaml`:

1. set `id` and `name`, and `agents: [<agent_id>]` (or `[]`: the user then starts with an empty state and creates an agent in the web UI);
2. set `provider.api_key` to the **name** of the environment variable that holds that user's OpenRouter key (for example
   `OPENROUTER_ALICE_API_KEY`) and define that variable in `.env`;
3. generate the web password hash and paste it as `password_hash`:

   ```bash
   uv run python -m src.web.auth
   ```

   It asks for the password twice without echoing it (minimum 8 characters) and prints an Argon2 hash.

All keys are described in the [user.yaml reference](../reference/user-yaml.md). Give each person their own user, their own key
and their own agents; never share a user between people.

## run

```bash
uv run python web.py            # web UI, http://127.0.0.1:8000 by default (web.host / web.port in settings.yaml)
uv run python run_telegram.py   # Telegram bot (optional)
uv run python cli.py            # terminal chat; set USER_ID and AGENT_ID in .env first
```

Log in on the web UI with the user id and the password. Restart both processes after changing YAML or `.env`. Then continue
with the [new user guide](new-user-guide.md) (agents, models, Telegram, memory).

## tests

```bash
uv run python -m unittest discover -s tests
```

Some tests read `.env` or `.config`; run them on a configured checkout.
