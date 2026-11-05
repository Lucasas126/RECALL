"""The running app's version: one implementation shared by the web UI, Telegram, the admin CLI and the skill."""
from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from importlib import metadata
from pathlib import Path

UNKNOWN = "unknown"
_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
_BUILD_LENGTH = 7


@lru_cache(maxsize=1)
def app_version() -> str:
    """The version in pyproject.toml (shipped with the code, also in the Docker image), else the installed
    package metadata, else "unknown". Nothing is invented."""
    try:
        return str(tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    try:
        return metadata.version("recall")
    except metadata.PackageNotFoundError:
        return UNKNOWN


def app_build() -> str | None:
    """RECALL_BUILD (a git sha injected at image build) shortened to 7 characters, None when unset."""
    build = os.environ.get("RECALL_BUILD", "").strip()
    return build[:_BUILD_LENGTH] or None


def version_info() -> dict[str, str | None]:
    return {"version": app_version(), "build": app_build()}


def version_label() -> str:
    """"RECALL v0.1.0", plus " (abc1234)" when a build id is set."""
    build = app_build()
    return f"RECALL v{app_version()}" + (f" ({build})" if build else "")
