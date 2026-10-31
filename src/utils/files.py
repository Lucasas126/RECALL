import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from typing import Generator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


@contextmanager
def exclusive_file_lock(path: Path) -> Generator[None, None, None]:
    """Serialize processes using a persistent lock file beside the protected file."""
    with path.open("a+b") as lock:
        if os.name == "nt":
            lock.seek(0, os.SEEK_END)
            if lock.tell() == 0:
                lock.write(b"\0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


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
        for attempt in range(5):
            try:
                temporary_path.replace(path)
                break
            except PermissionError as exc:
                # Windows can briefly deny replacement while another handle is closing.
                if os.name != "nt" or getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 4:
                    raise
                sleep(0.05 * (attempt + 1))
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
