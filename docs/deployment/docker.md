# deployment (Docker)

how to run RECALL on a home server with Docker, and how releases reach it.

Status: implemented for the web UI and the Telegram bot. The Google Calendar MCP
server is not supported in the Docker image yet (see [limitations](#limitations)).

## overview

- `.github/workflows/docker-publish.yml` builds the image and pushes it to
  `ghcr.io/<owner>/recall` **only when a GitHub Release is published** (or on a
  manual run). Pull requests and pushes to `main` that touch the image inputs
  build it as a check, without pushing.
- `Dockerfile` builds one image for both processes: the web UI (default command,
  `python web.py`) and the Telegram bot (`python run_telegram.py`).
- `docker-compose.yml` runs `recall-web`, `recall-telegram` (opt-in, see
  [telegram on the server](#telegram-on-the-server)) and
  [Watchtower](https://github.com/nicholas-fedor/watchtower), which polls GHCR
  and recreates the two RECALL containers when the tag they run changes.
- `.env.deploy.example` lists every environment variable, with placeholders.

## image tags

| event | tags pushed |
| --- | --- |
| release `v1.2.3` published | `1.2.3`, `1.2`, `1`, `latest`, `sha-<short>` |
| pre-release `v1.3.0-rc.1` published | `1.3.0-rc.1`, `sha-<short>` (no `latest`) |
| manual run on tag `v1.2.3` | `1.2.3`, `1.2`, `1`, `sha-<short>`; `latest` only with the **latest** input checked |
| manual run on a branch | `sha-<short>` |
| pull request / push to `main` | nothing (build only) |

The server runs `latest` by default (`RECALL_TAG` in `.env`), so it only changes
when a non-pre-release GitHub Release is published. Publishing a release for an
older version also moves `latest` back to it. Pin `RECALL_TAG` to a version
(for example `0.2.0`) to stop automatic updates, or to a `sha-` tag to roll back.

## release procedure

1. Bump `version` in `pyproject.toml` (for example `0.2.0`), run `uv lock` so
   `uv.lock` records it, and merge to `main`.
2. Create a GitHub Release with a new tag `v0.2.0` targeting `main` and publish
   it. The workflow fails if the tag (without `v`) differs from the
   `pyproject.toml` version.
3. Wait for **Build & push image** to finish in the Actions tab.
4. Watchtower pulls the new `latest` within `WATCHTOWER_POLL_INTERVAL` (default
   300 seconds), recreates `recall-web` and `recall-telegram` one at a time, and
   removes the old image.

Mark the release as a pre-release to publish a test image without updating the
server; run it elsewhere with `RECALL_TAG=1.3.0-rc.1`.

## server setup

Requirements: Docker Engine with the Compose plugin, amd64.

1. Copy `docker-compose.yml` and `.env.deploy.example` to a folder on the
   server, rename the example to `.env`, and fill it in. `.env` must stay
   private (`chmod 600 .env`).
2. If the GHCR package is private, log the host in once with a GitHub personal
   access token that has `read:packages`:

   ```bash
   echo "$GHCR_PAT" | docker login ghcr.io -u <github-user> --password-stdin
   ```

   Watchtower reads the resulting `/root/.docker/config.json` (mounted
   read-only). Remove that mount if the package is public.
3. Create the state folders and give them to the container user (uid 1000):

   ```bash
   mkdir -p config data logs
   sudo chown -R 1000:1000 config data logs
   ```

4. Put each user's configuration in `config/<user_id>/` (`user.yaml` and
   `agents/<agent_id>.yaml`, see [configuration](../reference/user-yaml.md#example)). To
   move an existing install, copy its `.config/` contents to `config/` and its
   `data/` to `data/`.
5. Start everything:

   ```bash
   docker compose up -d
   docker compose ps          # recall-web should become "healthy"
   docker compose logs -f recall-web recall-telegram
   ```

The web UI listens on `http://<server>:${RECALL_PORT}` (default 8000).

## proxmox LXC

Example for a Proxmox VE host where each app runs in its own unprivileged Debian LXC
with Docker Compose under `/opt/<app>`. For RECALL:

1. Create an unprivileged Debian LXC (for example CT `400`, hostname `recall`,
   `onboot: 1`) with the `nesting=1,keyctl=1` features so Docker runs inside it.
   Starting point: 2 cores, 2 GB RAM, 16 GB disk (Chroma, Tesseract and ffmpeg
   are heavier than a plain web app); adjust after watching usage.
2. Install Docker Engine + Compose plugin in the CT, then follow
   [server setup](#server-setup) in `/opt/recall` (`config/`, `data/`, `logs/`
   live there; `chown 1000:1000` inside the CT is correct, the container user is
   uid 1000 of the CT).
3. Access is LAN-only: open `http://<CT IP>:8000` from devices on the home
   network. Do not forward the port on the router; use a VPN for remote access.
4. Back up the CT with `vzdump` like the others, or at least `/opt/recall`
   (`.env`, `config/`, `data/`).

## telegram on the server

`recall-telegram` is behind the Compose profile `telegram`, so by default only
the web UI and Watchtower start. While the bot runs on another machine, leave
`COMPOSE_PROFILES` unset. To move the bot to the server, stop the other
`run_telegram.py`, set `COMPOSE_PROFILES=telegram` and the Telegram variables in
`.env`, and run `docker compose up -d`.

## persistent state

The containers run from `/app`, and the relative paths in `settings.yaml`
(`paths`) resolve there. Everything RECALL writes lives in three bind mounts:

| host (default) | container | contents |
| --- | --- | --- |
| `./config` (`RECALL_CONFIG_DIR`) | `/app/.config` | `user.yaml`, agent YAML (written by the Agents tab), preferences, favorites, lock files, MCP tokens under `<user_id>/mcp/` |
| `./data` (`RECALL_DATA_DIR`) | `/app/data` | per-agent `mind_palace/`, `conversations/` and the Chroma vector index (`.index/`) |
| `./logs` (`RECALL_LOGS_DIR`) | `/app/logs` | one folder per run (`<timestamp>_web`, `<timestamp>_telegram`) |

Back up `config` and `data`. Nothing else in the container needs to persist.
The image never contains `.config/`, `data/`, `logs/`, `.env` or OAuth key
files (see `.dockerignore`). To change framework settings without a new
release, uncomment the `settings.yaml` mount in `docker-compose.yml`.

Both RECALL containers share the same folders, exactly like running `web.py`
and `run_telegram.py` side by side on one machine. Agent YAML and preference
writes are serialized with file locks, which work across containers on the same
host.

## environment

`docker-compose.yml` passes the whole `.env` to both RECALL containers
(`env_file`), because the names of per-user API key variables come from each
`user.yaml` (`provider.api_key`) and cannot be listed in advance. See
`.env.deploy.example` for:

- `RECALL_SESSION_SECRET`: web session signing secret ([web authentication](../reference/settings-yaml.md#web-authentication)).
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`: [Telegram access](../user-guide/telegram.md#telegram-access).
  Only one process can poll a bot token, so do not run a local
  `run_telegram.py` with the same token while the server bot is up.
- `OPENROUTER_API_KEY` (associative memory, chat naming) and one variable per
  user key referenced in `user.yaml`.
- Optional `TAVILY_API_KEY` ([shared web access](../reference/settings-yaml.md#shared-web-access-tavily))
  and `OLLAMA_HOST`. To reach Ollama on the Docker host via
  `host.docker.internal`, add `extra_hosts: ["host.docker.internal:host-gateway"]`
  to the RECALL services.
- Optional `RECALL_PUBLIC_URL`, the address you open the web UI at (for
  example `http://<server-ip>:8000`), so Telegram dream notifications link to
  the session ([web.public_url](../reference/settings-yaml.md#web-authentication)). Set it in `.env`
  (a commented placeholder is in `.env.deploy.example`); without it the
  notification is text-only.
- Optional `RECALL_TERMINAL_ALLOW_LOCAL=true`: accept `type: local` terminal backends (commands run inside the container as
  the `app` user in `/app`). Only for an `admin` agent that runs the [Admin CLI](../user-guide/admin-cli.md) (the `guide` template no longer needs it); read the
  [risks](security.md#terminal-sandbox) first, since the agent can reach every user's `/app/.config` and `/app/data`. Add it
  to the environment of both services and restart them.
- Credentials can also live in `.config/secrets.env` (inside the mounted config folder), written with
  `docker compose exec -it recall-web python -m src.admin set-secret NAME`; no restart is needed for those. The real
  environment wins over the file. Prefer the file for keys that change after the first run (provider, search, Telegram token):
  a variable in `.env` needs a restart, cannot be seen or managed by an agent's `secrets` tools and must be removed from `.env`
  to be overridden. To move one, add the line to `secrets.env` (mode 0600, owned by the container user), delete it from `.env`
  and restart both services once; keep a copy of `.env` first.
- `RECALL_BUILD`: build identifier baked into the image (not a runtime setting you edit). The `RECALL_BUILD` build
  argument of the `Dockerfile` is set to the commit sha by the publish workflow; without it no build is shown.
  The version comes from `pyproject.toml`, copied with the code. Where it shows: `GET /api/version`, the login form
  and Settings dialog of the web UI (`RECALL v0.1.0 (abc1234)`), the first line of Telegram `/help`, and
  `docker compose exec web python -m src.admin version`.
- Deploy-only values read by Compose: `GHCR_OWNER` (lowercase), `RECALL_TAG`,
  `RECALL_PORT`, `RECALL_*_DIR`, `TZ`.

The image sets `RECALL_WEB_HOST=0.0.0.0` so the web UI accepts connections on
the published port (see [web authentication](../reference/settings-yaml.md#web-authentication)).

## users and passwords

Create users inside the running container instead of hand-editing YAML (the password is typed twice, hidden, or
generated with `--generate` and printed once; it is never an argument and never stored in clear):

```bash
docker compose exec -it recall-web python -m src.admin create-user <user_id> --name "User A" --api-key-env OPENROUTER_USER_A_API_KEY
docker compose exec -it recall-web python -m src.admin create-user <user_id> --generate --claude-login
docker compose exec -it recall-web python -m src.admin set-password <user_id>
```

The command writes `/app/.config/<user_id>/user.yaml` (no agents; the user creates the first one in the web UI),
refuses to overwrite an existing user, and prints the next steps: put the provider key in `.env` under the
variable name given (the key itself is never asked for), and add `<telegram_id>=<user_id>` to
`TELEGRAM_ALLOWED_USERS` yourself. `set-password` keeps the other keys but YAML comments are lost. Restart the
services afterwards if they were running.

## claude subscription logins

The image contains the Claude Code CLI, but each person logs in with **their own** Claude account, in their own
folder, inside the persisted config volume:

1. Create the user with `--claude-login` (above), or add to their `user.yaml`
   `claude_subscription: {claude_config_dir: /app/.config/<user_id>/claude-login}`.
2. Log in inside the container (headless flow: it prints a URL, open it in any browser, sign in as that person,
   paste the code back):

   ```bash
   docker compose exec -it recall-web sh -c 'CLAUDE_CONFIG_DIR=/app/.config/<user_id>/claude-login claude auth login'
   ```

   The container user is `app`, so the folder is created with the right owner. Check with
   `... claude auth status` using the same `CLAUDE_CONFIG_DIR`.
3. No restart is needed; the Claude tab of the model picker becomes selectable.

Why it persists: with `CLAUDE_CONFIG_DIR` set, the CLI keeps its credentials (`.credentials.json`) and its
settings (`.claude.json`) inside that folder, which lives under the `/app/.config` volume, so it survives image
updates (Watchtower) and container recreation. Without `CLAUDE_CONFIG_DIR` it would write to `/app/.claude`, which
is not a volume and is lost on update; RECALL's guard (`claude_subscription.default_login_users`) already refuses
the CLI default login for users without their own folder, so `HOME` is deliberately not redirected.

Rules: never copy a login between users or machines. Copying `.credentials.json` from another machine is not
recommended: the refresh tokens rotate, so the two copies invalidate each other. Treat the folder like a password
(back it up like `config/`, `chmod 700`). To log a user out, run `claude auth logout` with the same variable or
delete the folder. Terms and limits: [Claude subscription](../user-guide/claude-subscription.md).

## image contents

- Python 3.12 slim with the locked dependencies (`uv sync --frozen --no-dev`).
- `tesseract-ocr` with English and Spanish data, matching the default
  `pdf.ocr_languages: eng+spa` ([Tesseract OCR](../user-guide/files-and-media.md#tesseract-ocr)). Other
  languages need a custom image.
- `ffmpeg`/`ffprobe` for [video input](../user-guide/files-and-media.md#video-input).
- Node.js 22 (the `node` binary only, from the official `node:22-slim` image) and the Claude Code CLI, pinned by
  the `CLAUDE_CODE_VERSION` build argument of the `Dockerfile` (currently 2.1.316) and installed in `/opt/claude`,
  which is on `PATH`. Used only by the [Claude subscription provider](#claude-subscription-logins);
  RECALL never updates it, a new release of the image bumps it.
- `bash`, coreutils, `findutils`, `grep`, `awk` (mawk) from the base image, plus `file` and `diffutils`: what a `type: local`
  terminal backend and its `sandbox_*` file tools need (commands run through `bash -c`, or `/bin/sh -c` when bash is missing,
  with a scrubbed environment: no `RECALL_*`, keys or tokens, but `PATH` includes `/app/.venv/bin`, so `python -m src.admin`
  works from `/app`).
- Runs as the non-root user `app` (uid/gid 1000).
- Healthcheck: `GET /` on port 8000 (the static page; no login needed). The
  Telegram service disables it because it has no HTTP server.

## limitations

- **Google Calendar MCP**: the image has no Node.js/`npx`, so the server reports
  the missing command and chats continue without it ([MCP](../user-guide/mcp.md#runtime-behavior)).
  Its sign-in also redirects to `localhost` on the RECALL host, which a browser
  on another machine cannot reach. Supporting it needs Node.js in the image and
  a way to complete the OAuth flow remotely.
- **Claude subscription provider** ([experimental](../user-guide/claude-subscription.md)): the image has
  the CLI but no login; each user needs their own, see [Claude subscription logins](#claude-subscription-logins).
  Without it, `claude_subscription:` models are refused for that user.
- The web UI is plain HTTP and its session cookie is not marked `Secure`. Keep it
  on the LAN or a VPN, or put an HTTPS reverse proxy in front.
- amd64 only. Add `linux/arm64` to the workflow `platforms` for ARM servers.
