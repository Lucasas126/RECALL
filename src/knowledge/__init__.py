from __future__ import annotations

from pathlib import Path, PurePosixPath

import yaml

from src.utils.i18n import InputError
from src.utils.version import UNKNOWN, app_version

# The built-in knowledge skill: shipped inside the code, read-only for agents, always the version that runs.
BUILTIN_SKILL = "recall"
KNOWLEDGE_DIR = Path(__file__).resolve().parent / BUILTIN_SKILL
SKILL_FILE = "SKILL.md"


def skill_text() -> str:
    """SKILL.md with the app version stamped into its frontmatter (when known)."""
    text = (KNOWLEDGE_DIR / SKILL_FILE).read_text(encoding="utf-8").replace("\r\n", "\n")
    version = app_version()
    end = text.find("\n---", 3)
    if version != UNKNOWN and end != -1 and "version:" not in text[:end]:
        text = f"{text[:end]}\nversion: {version}{text[end:]}"
    return text


def description() -> str:
    """The one-line description from SKILL.md's frontmatter."""
    end = skill_text().find("\n---", 3)
    meta = yaml.safe_load(skill_text()[3:end])
    return " ".join(str(meta["description"]).split())


def files() -> list[str]:
    """Reference files of the skill (everything but SKILL.md), relative to its folder."""
    return sorted(
        item.relative_to(KNOWLEDGE_DIR).as_posix() for item in KNOWLEDGE_DIR.rglob("*")
        if item.is_file() and item.name != SKILL_FILE and "__pycache__" not in item.parts
    )


def read_file(file_path: str) -> str:
    """One file of the skill; only relative paths that stay inside the knowledge folder are accepted."""
    relative = PurePosixPath(str(file_path).replace("\\", "/"))
    if not str(file_path).strip() or relative.is_absolute() or ".." in relative.parts or ":" in relative.parts[0]:
        raise InputError("invalid skill file path {path!r}; use a relative path such as references/notes.md", path=file_path)
    root = KNOWLEDGE_DIR.resolve()
    target = (root / relative).resolve()
    if root not in target.parents:  # also catches symlinks that leave the folder
        raise InputError("invalid skill file path {path!r}; use a relative path such as references/notes.md", path=file_path)
    if not target.is_file():
        raise FileNotFoundError(f"file not found in skill {BUILTIN_SKILL}: {file_path}")
    return target.read_text(encoding="utf-8").replace("\r\n", "\n")
