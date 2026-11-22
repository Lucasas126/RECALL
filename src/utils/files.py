import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Generator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_held = threading.local()  # lock path -> how many times this thread holds it (re-entry never blocks on itself)
_WINDOWS_LOCK_PATIENCE = 120.0  # seconds a Windows writer keeps retrying (msvcrt LK_LOCK alone gives up after ~10 s)


@contextmanager
def exclusive_file_lock(path: Path) -> Generator[None, None, None]:
    """Serialize threads and processes using a persistent lock file beside the protected file.

    Waiting writers queue on the OS lock. The lock is re-entrant within one thread: a holder that calls code taking
    the same lock again (a tool that reads, checks and then calls a method that locks too) does not deadlock.
    """
    key = str(Path(path).resolve())
    depth = getattr(_held, "depth", None)
    if depth is None:
        depth = _held.depth = {}
    if depth.get(key):
        depth[key] += 1
        try:
            yield
        finally:
            depth[key] -= 1
        return
    with path.open("a+b") as lock:
        if os.name == "nt":
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            deadline = time.monotonic() + _WINDOWS_LOCK_PATIENCE
            while True:
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        depth[key] = 1
        try:
            yield
        finally:
            depth.pop(key, None)
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class NonBlockingFileLock:
    """An exclusive lock on a file that is taken without waiting and held until released.

    The lock belongs to the open file handle, not to a thread, so it can be taken in one
    thread and released in another; a second handle (in this or another process) fails to take it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        """Take the lock if it is free; return False when another holder has it."""
        if self._handle is not None:
            raise RuntimeError(f"lock already held: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def atomic_write_text(path: Path, content: str) -> None:
    """Replace a UTF-8 file atomically using a temporary file in the same directory."""
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}-", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def atomic_replace_bytes(path: Path, data: bytes) -> None:
    """Replace a file atomically with `data`, keeping the permissions of the file it replaces.

    A reader sees the old bytes or the new ones, never half a file, even if the process dies mid-write. A new file
    gets the usual permissions (0666 minus the umask), not the private 0600 of a temporary file. On Windows, where a
    file another program holds open cannot be replaced, it falls back to writing in place.
    """
    try:
        mode = path.stat().st_mode & 0o7777
    except FileNotFoundError:
        mode = 0o666 & ~_UMASK
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, mode)
        try:
            temporary_path.replace(path)
        except PermissionError:
            if os.name != "nt":
                raise
            path.write_bytes(data)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def atomic_replace_text(path: Path, content: str) -> None:
    """`atomic_replace_bytes` for UTF-8 text, with the newline translation of `Path.write_text`."""
    atomic_replace_bytes(path, content.replace("\n", os.linesep).encode("utf-8"))


def _read_umask() -> int:
    """The process umask, read once at import (setting it is process-wide, so it is never touched later)."""
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


_UMASK = _read_umask()
