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
   `agents/<agent_id>.yaml`, see [configuration](config.md#config-files)). To
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

The home server is Proxmox VE; each app runs in its own unprivileged Debian LXC
with Docker Compose under `/opt/<app>` (same layout as the other apps). For RECALL:

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

- `RECALL_SESSION_SECRET`: web session signing secret ([web authentication](config.md#web-authentication)).
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`: [Telegram access](config.md#telegram-access).
  Only one process can poll a bot token, so do not run a local
  `run_telegram.py` with the same token while the server bot is up.
- `OPENROUTER_API_KEY` (associative memory, chat naming) and one variable per
  user key referenced in `user.yaml`.
- Optional `TAVILY_API_KEY` ([shared web access](config.md#shared-web-access-tavily))
  and `OLLAMA_HOST`. To reach Ollama on the Docker host via
  `host.docker.internal`, add `extra_hosts: ["host.docker.internal:host-gateway"]`
  to the RECALL services.
- Deploy-only values read by Compose: `GHCR_OWNER` (lowercase), `RECALL_TAG`,
  `RECALL_PORT`, `RECALL_*_DIR`, `TZ`.

The image sets `RECALL_WEB_HOST=0.0.0.0` so the web UI accepts connections on
the published port (see [web authentication](config.md#web-authentication)).

## image contents

- Python 3.12 slim with the locked dependencies (`uv sync --frozen --no-dev`).
- `tesseract-ocr` with English and Spanish data, matching the default
  `pdf.ocr_languages: eng+spa` ([Tesseract OCR](config.md#tesseract-ocr)). Other
  languages need a custom image.
- `ffmpeg`/`ffprobe` for [video input](config.md#video-input).
- Runs as the non-root user `app` (uid/gid 1000).
- Healthcheck: `GET /` on port 8000 (the static page; no login needed). The
  Telegram service disables it because it has no HTTP server.

## limitations

- **Google Calendar MCP**: the image has no Node.js/`npx`, so the server reports
  the missing command and chats continue without it ([MCP](mcp.md#runtime-behavior)).
  Its sign-in also redirects to `localhost` on the RECALL host, which a browser
  on another machine cannot reach. Supporting it needs Node.js in the image and
  a way to complete the OAuth flow remotely.
- The web UI is plain HTTP and its session cookie is not marked `Secure`. Keep it
  on the LAN or a VPN, or put an HTTPS reverse proxy in front.
- amd64 only. Add `linux/arm64` to the workflow `platforms` for ARM servers.
