from pathlib import Path
from tempfile import NamedTemporaryFile


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
