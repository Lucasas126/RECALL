# syntax=docker/dockerfile:1.7

# One image, two processes: the web UI (default CMD) and the Telegram bot
# (`python run_telegram.py`, see docker-compose.yml). See docs/deployment/docker.md.

# ---------- builder ----------
# Use uv to resolve & install deps into a venv we copy into the runtime image.
FROM python:3.12-slim AS builder

# UV_PYTHON_DOWNLOADS=0: use the image's Python, never a managed interpreter.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=0

# Pinned uv binary from the official image (reproducible, no curl | sh).
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app

# Resolve dependencies first (cached unless pyproject.toml / uv.lock change).
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# Now copy the source. pyproject has no [build-system], so uv treats RECALL as
# a virtual project and there is nothing to install for the project itself.
COPY . .

# ---------- claude-cli ----------
# Claude Code CLI for the `claude_subscription:` provider, installed into its own
# prefix so the runtime image only copies that folder plus the `node` binary
# (no npm, no build tools). Bump CLAUDE_CODE_VERSION on release; the models in
# docs/user-guide/claude-subscription.md need >= 2.1.293 (Haiku 5.5).
FROM node:22-slim AS claude-cli
ARG CLAUDE_CODE_VERSION=2.1.316
RUN npm install -g --prefix /opt/claude --no-audit --no-fund \
      @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
 && /opt/claude/bin/claude --version

# ---------- runtime ----------
FROM python:3.12-slim AS runtime

# Optional build identifier (CI passes the git sha); shown next to the version as the first 7 characters.
ARG RECALL_BUILD=""

# settings.yaml binds the web UI to 127.0.0.1; inside a container it must
# listen on all interfaces so the published port reaches it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:/opt/claude/bin:$PATH" \
    RECALL_WEB_HOST=0.0.0.0 \
    RECALL_BUILD=$RECALL_BUILD

# System programs RECALL shells out to:
# - tesseract-ocr (+ eng/spa data, matching pdf.ocr_languages): scanned PDF pages
# - ffmpeg (includes ffprobe): video decomposition
# - tzdata: log timestamps honour the TZ env var
# - file, diffutils: the `type: local` terminal backend (see docs/deployment/docker.md) runs commands inside this
#   image; bash, coreutils (head, wc, tr, mktemp, dirname, mv, chmod), findutils, grep and awk (mawk) that the
#   sandbox_* file tools need already come with python:slim, `file` does not, diffutils is listed to be explicit
# npx for the Google Calendar MCP is intentionally not included (only `node`, below).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa \
      ffmpeg \
      openssh-client \
      file diffutils \
      tzdata \
 && rm -rf /var/lib/apt/lists/*

# Claude Code CLI (+ the Node runtime it runs on) from the claude-cli stage; both
# are root-owned and world-readable. /opt/claude/bin/claude is on PATH, which is
# where RECALL looks for `claude` (override: RECALL_CLAUDE_COMMAND).
COPY --from=claude-cli /usr/local/bin/node /usr/local/bin/node
COPY --from=claude-cli /opt/claude /opt/claude

# Non-root user for the runtime.
RUN groupadd --system --gid 1000 app \
 && useradd --system --uid 1000 --gid app --home /app app

WORKDIR /app

# Pull the resolved venv + source from the builder.
COPY --from=builder --chown=app:app /app /app

# Persist user/agent config (+ MCP tokens), the data plane and logs outside the
# container. Paths match settings.yaml `paths`, relative to WORKDIR.
RUN mkdir -p /app/.config /app/data /app/logs \
 && chown -R app:app /app/.config /app/data /app/logs
VOLUME ["/app/.config", "/app/data", "/app/logs"]

USER app
EXPOSE 8000

# Cheap unauthenticated route (serves the static index.html). The Telegram
# service disables this check in docker-compose.yml since it has no HTTP port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3).status < 500 else 1)"

CMD ["python", "web.py"]
