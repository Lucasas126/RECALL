from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml

from src.core.sources.base import Source, scan_files
from src.utils.files import atomic_write_text, exclusive_file_lock
from src.utils.i18n import InputError

SOUL_FILE = "SOUL.md"
SOUL_MAX_CHARS = 8000
SOUL_HISTORY_KEEP = 5
MEMORY_DELIMITER = "\n§\n"
MemoryTarget = Literal["memory", "user"]
MEMORY_FILES: dict[str, str] = {"memory": "memories/MEMORY.md", "user": "memories/USER.md"}
SKILL_FILE = "SKILL.md"
SKILL_MAX_CHARS = 50_000
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_VERSION_RE = re.compile(r"^\d{8}-\d{6}-\d{6}$")
# top-level entries of the agent folder that belong to this source; the rest (chats, dream state, index) do not
_ENTRIES = (SOUL_FILE, "soul_history", "memories", "skills")


@dataclass(frozen=True)
class SkillInfo:
    """A skill's index entry: its folder name and the description from its frontmatter."""

    name: str
    description: str


def parse_skill(text: str) -> tuple[dict, str]:
    """Split a SKILL.md into its YAML frontmatter and body; raises InputError when malformed."""
    if not text.startswith("---"):
        raise InputError("SKILL.md must start with YAML frontmatter (---) containing name and description")
    end = text.find("\n---", 3)
    if end == -1:
        raise InputError("SKILL.md frontmatter is not closed with ---")
    try:
        meta = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError as exc:
        raise InputError("SKILL.md frontmatter is not valid YAML: {error}", error=str(exc)) from exc
    if not isinstance(meta, dict):
        raise InputError("SKILL.md frontmatter must be a mapping with name and description")
    for key in ("name", "description"):
        if not isinstance(meta.get(key), str) or not meta[key].strip():
            raise InputError("SKILL.md frontmatter needs a non-empty '{key}'", key=key)
    return meta, text[end + 4:].lstrip("\n")


class AgentFilesSource(Source):
    """The agent's own identity files: `SOUL.md`, `soul_history/`, `memories/` and `skills/`.

    Rooted at the agent's data folder but restricted to those entries, so chats, dream state and
    the index stay out of reach. Readable by the generic tools; changes go through the methods
    below, which keep the SOUL history, memory limits and skill format consistent. Only `skills/` is
    indexed: SOUL.md and the memory files are always in the system prompt already.
    """

    def __init__(self, root: Path):
        super().__init__("agent", root, readable=True, writable=False, indexed=True)

    # ------------------------------------------------------------------ guard

    def resolve(self, rel_path: str) -> Path:
        target = super().resolve(rel_path)
        root = self.root.resolve()
        if target != root and target.relative_to(root).parts[0] not in _ENTRIES:
            raise ValueError(f"path is outside the agent files ({', '.join(_ENTRIES)}): {rel_path!r}")
        return target

    def list(self, subpath: str = "") -> list[str]:
        if subpath.strip("/"):
            return super().list(subpath)
        return sorted(
            name + "/" if (self.root / name).is_dir() else name
            for name in _ENTRIES if (self.root / name).exists()
        )

    def walk(self, exts: set[str] | None = None) -> list[str]:
        root = self.root.resolve()
        out = []
        for name in _ENTRIES:
            path = root / name
            candidates = [path] if path.is_file() else (path.rglob("*") if path.is_dir() else [])
            for item in candidates:
                if not item.is_file() or (exts and item.suffix.lstrip(".").lower() not in exts):
                    continue
                out.append(item.relative_to(root).as_posix())
        return sorted(out)

    def index_scan(self, exts: set[str]) -> dict[str, tuple[int, int]]:
        root = self.root.resolve()
        return scan_files(root / "skills", exts, base=root)

    def _lock(self):
        return exclusive_file_lock(self.root / ".agent_files.lock")

    # ------------------------------------------------------------------ soul

    def soul(self) -> str | None:
        """SOUL.md's text, or None when the file does not exist yet."""
        path = self.resolve(SOUL_FILE)
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def soul_text(self) -> str:
        """SOUL.md's text; a missing file is an empty personality."""
        return self.soul() or ""

    def write_soul(self, content: str) -> bool:
        """Replace SOUL.md, saving the previous version to `soul_history/` first; False when unchanged."""
        if len(content) > SOUL_MAX_CHARS:
            raise InputError("SOUL.md cannot exceed {limit} characters", limit=SOUL_MAX_CHARS)
        with self._lock():
            current = self.soul()
            if current == content:
                return False
            if current is not None:
                self._snapshot_soul(current)
            atomic_write_text(self.resolve(SOUL_FILE), content)
        return True

    def soul_history(self) -> list[dict]:
        """Saved SOUL versions, newest first: version id, save time and size."""
        folder = self.resolve("soul_history")
        if not folder.is_dir():
            return []
        versions = []
        for path in folder.glob("*.md"):
            if not _VERSION_RE.match(path.stem):
                continue
            saved = datetime.strptime(path.stem, "%Y%m%d-%H%M%S-%f").astimezone()
            versions.append({
                "version": path.stem,
                "saved_at": saved.isoformat(timespec="seconds"),
                "chars": len(path.read_text(encoding="utf-8")),
            })
        return sorted(versions, key=lambda item: item["version"], reverse=True)

    def soul_version(self, version: str) -> str:
        """Text of one saved SOUL version; raises FileNotFoundError when it does not exist."""
        if not _VERSION_RE.match(version):
            raise InputError("invalid SOUL version: {version}", version=version)
        path = self.resolve(f"soul_history/{version}.md")
        if not path.is_file():
            raise FileNotFoundError(f"SOUL version not found: {version}")
        return path.read_text(encoding="utf-8")

    def restore_soul(self, version: str) -> str:
        """Make a saved version the current SOUL.md (the current one is saved first) and return it."""
        content = self.soul_version(version)
        self.write_soul(content)
        return content

    def _snapshot_soul(self, content: str) -> None:
        folder = self.resolve("soul_history")
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now()
        path = folder / f"{stamp:%Y%m%d-%H%M%S-%f}.md"
        while path.exists():  # two saves within the same microsecond
            stamp = stamp.replace(microsecond=(stamp.microsecond + 1) % 1_000_000)
            path = folder / f"{stamp:%Y%m%d-%H%M%S-%f}.md"
        atomic_write_text(path, content)
        versions = sorted(item for item in folder.glob("*.md") if _VERSION_RE.match(item.stem))
        for old in versions[:-SOUL_HISTORY_KEEP]:
            old.unlink()

    # ------------------------------------------------------------------ memories

    def memory_text(self, target: MemoryTarget) -> str:
        path = self.resolve(_memory_file(target))
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def memory_entries(self, target: MemoryTarget) -> list[str]:
        return split_memory(self.memory_text(target))

    def write_memory_text(self, target: MemoryTarget, text: str) -> None:
        path = self.resolve(_memory_file(target))
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text)

    def memory_lock(self):
        """Serialize read-modify-write cycles on the memory files across chats, dreams and processes."""
        return self._lock()

    # ------------------------------------------------------------------ skills

    def skills(self) -> list[SkillInfo]:
        """Index of valid skills, sorted by name; malformed SKILL.md files are skipped."""
        folder = self.resolve("skills")
        if not folder.is_dir():
            return []
        found = []
        for path in sorted(folder.glob(f"*/{SKILL_FILE}")):
            try:
                meta, _ = parse_skill(path.read_text(encoding="utf-8"))
            except (InputError, OSError, UnicodeError):
                continue
            found.append(SkillInfo(path.parent.name, " ".join(str(meta["description"]).split())))
        return found

    def skill_text(self, name: str) -> str:
        path = self._skill_dir(name) / SKILL_FILE
        if not path.is_file():
            raise FileNotFoundError(f"skill not found: {name}")
        return path.read_text(encoding="utf-8")

    def skill_files(self, name: str) -> list[str]:
        """Supporting files of a skill (everything but SKILL.md), relative to its folder."""
        folder = self._skill_dir(name)
        if not folder.is_dir():
            raise FileNotFoundError(f"skill not found: {name}")
        return sorted(
            item.relative_to(folder).as_posix() for item in folder.rglob("*")
            if item.is_file() and item.name != SKILL_FILE
        )

    def read_skill_file(self, name: str, file_path: str) -> str:
        path = self._skill_path(name, file_path)
        if not path.is_file():
            raise FileNotFoundError(f"file not found in skill {name}: {file_path}")
        return path.read_text(encoding="utf-8")

    def write_skill(self, name: str, content: str) -> None:
        """Create or replace a skill's SKILL.md after validating its frontmatter."""
        meta, _ = parse_skill(content)
        if meta["name"].strip() != name:
            raise InputError("the frontmatter name '{found}' must match the skill name '{name}'", found=meta["name"], name=name)
        if len(content) > SKILL_MAX_CHARS:
            raise InputError("SKILL.md cannot exceed {limit} characters", limit=SKILL_MAX_CHARS)
        folder = self._skill_dir(name)
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write_text(folder / SKILL_FILE, content)

    def write_skill_file(self, name: str, file_path: str, content: str) -> None:
        if not (self._skill_dir(name) / SKILL_FILE).is_file():
            raise FileNotFoundError(f"skill not found: {name}")
        if len(content) > SKILL_MAX_CHARS:
            raise InputError("skill files cannot exceed {limit} characters", limit=SKILL_MAX_CHARS)
        path = self._skill_path(name, file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, content)

    def remove_skill_file(self, name: str, file_path: str) -> None:
        path = self._skill_path(name, file_path)
        if not path.is_file():
            raise FileNotFoundError(f"file not found in skill {name}: {file_path}")
        path.unlink()

    def delete_skill(self, name: str) -> None:
        folder = self._skill_dir(name)
        if not folder.is_dir():
            raise FileNotFoundError(f"skill not found: {name}")
        shutil.rmtree(folder)

    def _skill_dir(self, name: str) -> Path:
        if not isinstance(name, str) or not _SKILL_NAME_RE.match(name):
            raise InputError(
                "invalid skill name {name!r}: use lowercase letters, digits, '-' or '_' (max 64)", name=name,
            )
        return self.resolve(f"skills/{name}")

    def _skill_path(self, name: str, file_path: str) -> Path:
        relative = PurePosixPath(file_path.replace("\\", "/"))
        if not file_path.strip() or relative.is_absolute() or ".." in relative.parts or relative.name == SKILL_FILE:
            raise InputError("invalid skill file path {path!r}; use a relative path such as references/notes.md", path=file_path)
        folder = self._skill_dir(name)
        target = (folder / relative).resolve()
        if folder.resolve() not in target.parents:
            raise InputError("invalid skill file path {path!r}; use a relative path such as references/notes.md", path=file_path)
        return target


def _memory_file(target: str) -> str:
    if target not in MEMORY_FILES:
        raise InputError("unknown memory target {target!r}; use 'memory' or 'user'", target=target)
    return MEMORY_FILES[target]


def split_memory(text: str) -> list[str]:
    """Entries of a memory file, separated by a line holding only `§`."""
    return [entry.strip() for entry in text.replace("\r\n", "\n").split(MEMORY_DELIMITER) if entry.strip()]


def join_memory(entries: list[str]) -> str:
    return MEMORY_DELIMITER.join(entries)
