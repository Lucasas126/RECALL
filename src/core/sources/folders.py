from __future__ import annotations

import hashlib
import time
from pathlib import Path

from src.core.sources.base import Source, scan_files
from src.terminal.protected import ProtectedPaths, build_protected
from src.utils.files import exclusive_file_lock
from src.utils.i18n import InputError

_RESERVED_SYSTEM = ("/proc", "/sys", "/dev")
_PROTECTED_TTL = 5.0  # seconds a computed protected-path set is reused


def reserved_roots() -> list[tuple[Path, str]]:
    """(resolved path, label) of the places a user folder may never equal, contain or sit inside."""
    from src.config import settings  # late: tests point these directories at temporary folders

    roots = [
        (settings.CONFIG_DIR, "the configuration folder"),
        (settings.DATA_DIR, "the data folder"),
        (settings.LOGS_DIR, "the logs folder"),
        (Path(__file__).resolve().parents[3], "the RECALL program folder"),
        *((Path(path), "a system folder") for path in _RESERVED_SYSTEM),
        (Path.home() / ".ssh", "the SSH folder"),
        (Path.home() / ".gnupg", "the GnuPG folder"),
    ]
    out = []
    for path, label in roots:
        try:
            out.append((Path(path).expanduser().resolve(), label))
        except (OSError, RuntimeError):
            continue
    return out


def folder_path_problem(path: str | Path, allowed_roots: list[str] | None = None) -> str | None:
    """Why a folder path may not be exposed to an agent (symlinks resolved), else None.

    Refused: a path that equals, contains or is inside a reserved root (see `reserved_roots`), and, when the
    administrator set `folders.allowed_roots`, a path outside all of those roots.
    """
    try:
        folder = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        return f"it cannot be resolved ({exc})"
    for root, label in reserved_roots():
        if folder == root or root in folder.parents or folder in root.parents:
            return f"it overlaps {label} ({root}), which agents may never reach"
    if allowed_roots is None:
        from src.config import settings

        allowed_roots = settings.FOLDERS_ALLOWED_ROOTS
    if allowed_roots:
        resolved = []
        for item in allowed_roots:
            try:
                resolved.append(Path(item).expanduser().resolve())
            except (OSError, RuntimeError, ValueError):
                continue
        if not any(folder == root or root in folder.parents for root in resolved):
            return "it is outside the roots the administrator allows (`folders.allowed_roots` in settings.yaml)"
    return None


class FolderSource(Source):
    """A folder chosen by the user (for example an Obsidian vault), exposed to one agent as `<name>:`.

    The folder must already exist; it is never created. Hidden entries such as `.obsidian/`, `.git/` or
    `.trash/` are left out of listings, searches and the index. `readable` and `writable` follow the
    agent's folder tool groups (and `read_only`); `indexed` follows the folder's own setting.
    """

    def __init__(self, name: str, root: Path, *, readable: bool = True, writable: bool = False, indexed: bool = True):
        folder = Path(root).expanduser()
        self._protected: tuple[float, ProtectedPaths] | None = None
        if not folder.is_dir():
            raise InputError(
                "folder '{name}' not found: {path}; fix `folders` in the agent configuration",
                name=name, path=str(folder),
            )
        self.name = name
        self.root = folder
        self.readable = readable
        self.writable = writable
        self.indexed = indexed

    def _protected_paths(self) -> ProtectedPaths:
        """The protected files of this installation (keys, secrets, .env...), recomputed every few seconds."""
        now = time.monotonic()
        if self._protected is None or now - self._protected[0] > _PROTECTED_TTL:
            from src.config import settings

            self._protected = (now, build_protected(settings.TERMINAL.protected_paths))
        return self._protected[1]

    def _is_protected(self, protected: ProtectedPaths, target: Path) -> bool:
        return protected.reason(str(target)) is not None

    def list(self, subpath: str = "") -> list[str]:
        protected = self._protected_paths()
        base = super().resolve(subpath)
        return [
            entry for entry in super().list(subpath)
            if not hidden(entry) and not self._is_protected(protected, base / entry.rstrip("/"))
        ]

    def walk(self, exts: set[str] | None = None) -> list[str]:
        protected = self._protected_paths()
        root = self.root.resolve()
        return [
            entry for entry in super().walk(exts)
            if not hidden(entry) and not self._is_protected(protected, root / entry)
        ]

    def index_scan(self, exts: set[str]) -> dict[str, tuple[int, int]]:
        protected = self._protected_paths()
        root = self.root.resolve()
        return {
            rel: stat for rel, stat in scan_files(root, exts).items()
            if not self._is_protected(protected, root / rel)
        }

    def write_lock(self):
        """The lock the folder tools hold around each read-modify-write, shared by every agent that has this folder.

        It lives in the data folder (one file per real folder path), never inside the user's folder."""
        from src.config import settings  # late: tests point these directories at temporary folders

        key = hashlib.sha256(str(self.root.resolve()).encode("utf-8")).hexdigest()[:32]
        locks = settings.DATA_DIR / ".folder_locks"
        locks.mkdir(parents=True, exist_ok=True)
        return exclusive_file_lock(locks / f"{key}.lock")

    def resolve(self, rel_path: str) -> Path:
        target = super().resolve(rel_path)
        root = self.root.resolve()
        if target != root and hidden(target.relative_to(root).as_posix()):
            raise ValueError(f"hidden entries of folder '{self.name}' are not accessible: {rel_path!r}")
        why = self._protected_paths().reason(str(target))
        if why:
            raise ValueError(f"{rel_path!r} in folder '{self.name}' is protected ({why}) and cannot be accessed")
        return target


def hidden(rel_path: str) -> bool:
    """Whether a relative path goes through a hidden entry (a name starting with '.')."""
    return any(part.startswith(".") for part in rel_path.rstrip("/").split("/") if part)
