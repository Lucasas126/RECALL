"""Profile pictures of users and agents: normalised once on upload, stored outside every agent's own data folder.

Files live in `<data_dir>/<user_id>/.avatars/`: `<agent_id>.png` for an agent and `_user.png` for the user. That folder
is a sibling of the agents' data folders (`<data_dir>/<user_id>/<agent_id>/`), so the `agent:` source, the folder tools
and the memory tools (all rooted in an agent's own folder) can neither list nor write it; an agent id cannot contain a
dot, so it can never be named `.avatars`.

Nothing the client says about an upload is trusted: the bytes are decoded with Pillow (PNG, JPEG and WEBP only), and
what is stored is a fresh PNG of at most 256 px on the longest side, without metadata.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from PIL import Image, ImageOps, UnidentifiedImageError

from src.utils.i18n import InputError, UserMessageError

MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_INPUT_SIDE = 4096  # pixels, either side of the uploaded image
OUTPUT_SIDE = 256  # pixels, longest side of the stored image
ALLOWED_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_FORMATS = ["PNG", "JPEG", "WEBP"]
AVATAR_STYLES = ("circle", "free")
USER_KEY = "_user"  # file stem of the user's own picture; an agent with this id cannot have a picture
AVATARS_DIR = ".avatars"
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class ImageTooLarge(InputError):
    """The upload is over the size limit (413)."""

    code = "image_too_large"


class UnsupportedImageType(InputError):
    """The upload is not declared as PNG, JPEG or WEBP (415)."""

    code = "unsupported_image_type"


class InvalidImage(InputError):
    """The bytes are not a usable PNG, JPEG or WEBP image (400)."""

    code = "invalid_image"


class AvatarStorageError(UserMessageError, RuntimeError):
    """The picture could not be written, moved or removed on disk (an actionable message for the owner)."""

    code = "avatar_storage"


@dataclass(frozen=True)
class Avatar:
    """A stored picture: `version` changes whenever the file does (it busts the browser's cache)."""

    path: Path
    version: str
    size: int

    @property
    def etag(self) -> str:
        return f'"{self.version}-{self.size:x}"'


def check_content_type(declared: str | None) -> None:
    """Refuse an upload that does not declare PNG, JPEG or WEBP (parameters such as a charset are ignored)."""
    media_type = (declared or "").split(";", 1)[0].strip().lower()
    if media_type not in ALLOWED_CONTENT_TYPES:
        raise UnsupportedImageType("Use a PNG, JPEG or WEBP image.")


def normalise_image(data: bytes) -> tuple[bytes, bool]:
    """Decode an upload and return `(png_bytes, has_alpha)`, the picture ready to store.

    The format is decided by the content, never by a name or a declared type. The EXIF orientation is applied, only
    the first frame of an animation is kept, the longest side is cut to `OUTPUT_SIDE` (never enlarged, aspect ratio
    and transparency kept) and every piece of metadata is dropped. `has_alpha` is true only when some pixel is
    really transparent.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageTooLarge("The image is larger than {limit} MiB.", limit=MAX_UPLOAD_BYTES // (1024 * 1024))
    if not data:
        raise InvalidImage("The file is not a valid PNG, JPEG or WEBP image.")
    try:
        with Image.open(io.BytesIO(data), formats=_FORMATS) as image:
            # the size comes from the header: a decompression bomb is refused before any pixel is decoded
            if image.width > MAX_INPUT_SIDE or image.height > MAX_INPUT_SIDE:
                raise InvalidImage(
                    "The image is larger than {side} x {side} pixels.", side=MAX_INPUT_SIDE,
                )
            image.seek(0)
            image.load()
            oriented = ImageOps.exif_transpose(image)
            rgba = oriented.convert("RGBA")
    except InputError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, OSError,
            ValueError, SyntaxError, EOFError, TypeError, MemoryError):
        raise InvalidImage("The file is not a valid PNG, JPEG or WEBP image.") from None
    rgba.thumbnail((OUTPUT_SIDE, OUTPUT_SIDE), Image.Resampling.LANCZOS)
    has_alpha = rgba.getchannel("A").getextrema()[0] < 255
    result = rgba if has_alpha else rgba.convert("RGB")
    clean = Image.frombytes(result.mode, result.size, result.tobytes())  # a new image: no info, no EXIF
    out = io.BytesIO()
    clean.save(out, format="PNG", optimize=True)
    return out.getvalue(), has_alpha


class AvatarStore:
    """The pictures of one installation, under `data_dir`; every path is checked to stay inside the user's `.avatars`."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    def folder(self, user_id: str) -> Path:
        if not _ID.fullmatch(user_id or ""):
            raise ValueError("invalid user id for a profile picture")
        return self.data_dir / user_id / AVATARS_DIR

    def path(self, user_id: str, key: str) -> Path:
        """The file of `key` (an agent id, or `USER_KEY` for the user); an id that is not a plain slug is refused."""
        if not _ID.fullmatch(key or ""):
            raise ValueError("invalid id for a profile picture")
        folder = self.folder(user_id)
        path = folder / f"{key}.png"
        if path.parent != folder:  # defence in depth: the slug check already excludes separators and dots
            raise ValueError("a profile picture must stay inside the avatars folder")
        return path

    def get(self, user_id: str, key: str) -> Avatar | None:
        path = self.path(user_id, key)
        try:
            if path.is_symlink() or not path.is_file():
                return None
            stat = path.stat()
        except OSError:
            return None
        return Avatar(path=path, version=f"{stat.st_mtime_ns:x}", size=stat.st_size)

    def read(self, avatar: Avatar) -> bytes | None:
        try:
            return avatar.path.read_bytes()
        except OSError:
            return None

    def save(self, user_id: str, key: str, png: bytes) -> Avatar:
        """Replace the picture atomically (a reader never sees half a file)."""
        path = self.path(user_id, key)
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=path.parent, prefix=f".{key}-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(png)
            os.replace(temporary, path)
        except OSError as exc:
            raise AvatarStorageError(
                "Could not save the profile picture; check the data folder permissions.",
            ) from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        avatar = self.get(user_id, key)
        if avatar is None:
            raise AvatarStorageError("Could not save the profile picture; check the data folder permissions.")
        return avatar

    def delete(self, user_id: str, key: str) -> None:
        try:
            self.path(user_id, key).unlink(missing_ok=True)
        except OSError as exc:
            raise AvatarStorageError(
                "Could not remove the profile picture; check the data folder permissions.",
            ) from exc

    def move(self, user_id: str, old_key: str, new_key: str) -> bool:
        """Rename an agent's picture with the agent; returns whether there was one. A stale file at the target is replaced."""
        source = self.path(user_id, old_key)
        target = self.path(user_id, new_key)
        if not source.is_file():
            return False
        try:
            os.replace(source, target)
        except OSError as exc:
            raise AvatarStorageError(
                "Could not move the agent's profile picture; close other programs using its files and try again.",
            ) from exc
        return True
