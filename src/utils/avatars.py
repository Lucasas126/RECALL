"""Profile pictures of users and agents: normalised once on upload, stored outside every agent's own data folder.

Files live in `<data_dir>/<user_id>/.avatars/`: `<agent_id>.png` (or `.webp` when animated) for an agent and `_user.png`
(or `_user.webp`) for the user. That folder
is a sibling of the agents' data folders (`<data_dir>/<user_id>/<agent_id>/`), so the `agent:` source, the folder tools
and the memory tools (all rooted in an agent's own folder) can neither list nor write it; an agent id cannot contain a
dot, so it can never be named `.avatars`.

Nothing the client says about an upload is trusted: the bytes are decoded with Pillow (PNG, JPEG, WEBP and GIF only),
and what is stored is a fresh PNG of at most 256 px on the longest side, without metadata; an animation (GIF, animated
WEBP, APNG) is kept as an animated WEBP of at most 100 frames, each of at most 256 px.
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

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_INPUT_SIDE = 4096  # pixels, either side of the uploaded image
OUTPUT_SIDE = 256  # pixels, longest side of the stored image
MAX_FRAMES = 100  # frames kept of an animation; the extra ones are dropped
MAX_TOTAL_PIXELS = 64_000_000  # frames kept x width x height of the upload, checked from the headers
ANIMATION_TARGET_BYTES = 1024 * 1024  # a lossless animation heavier than this is stored lossy instead
ALLOWED_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
_FORMATS = ["PNG", "JPEG", "WEBP", "GIF"]
_EXTENSIONS = {"image/png": ".png", "image/webp": ".webp"}
AVATAR_STYLES = ("circle", "free")
USER_KEY = "_user"  # file stem of the user's own picture; an agent with this id cannot have a picture
AVATARS_DIR = ".avatars"
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class ImageTooLarge(InputError):
    """The upload is over the size limit (413)."""

    code = "image_too_large"


class UnsupportedImageType(InputError):
    """The upload is not declared as PNG, JPEG, WEBP or GIF (415)."""

    code = "unsupported_image_type"


class InvalidImage(InputError):
    """The bytes are not a usable PNG, JPEG, WEBP or GIF image (400)."""

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
    def media_type(self) -> str:
        return "image/webp" if self.path.suffix == ".webp" else "image/png"

    @property
    def etag(self) -> str:
        return f'"{self.version}-{self.size:x}"'


def check_content_type(declared: str | None) -> None:
    """Refuse an upload that does not declare PNG, JPEG, WEBP or GIF (parameters such as a charset are ignored)."""
    media_type = (declared or "").split(";", 1)[0].strip().lower()
    if media_type not in ALLOWED_CONTENT_TYPES:
        raise UnsupportedImageType("Use a PNG, JPEG, WEBP or GIF image.")


@dataclass(frozen=True)
class NormalisedAvatar:
    """The picture ready to store: PNG for a still image, animated WEBP for an animation."""

    data: bytes
    has_alpha: bool
    media_type: str


_ORIENTATIONS = {
    2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def _invalid() -> InvalidImage:
    return InvalidImage("The file is not a valid PNG, JPEG, WEBP or GIF image.")


def _encode_animation(frames: list[Image.Image], durations: list[int], loop: int, has_alpha: bool) -> bytes:
    """Lossless animated WEBP; a heavy one falls back to high-quality lossy (alpha kept) to stay small."""
    if not has_alpha:
        frames = [frame.convert("RGB") for frame in frames]
    common = {"format": "WEBP", "save_all": True, "append_images": frames[1:], "duration": durations, "loop": loop}
    best = b""
    for options in ({"lossless": True}, {"quality": 90, "method": 4}, {"quality": 75, "method": 4}):
        out = io.BytesIO()
        frames[0].save(out, **common, **options)
        best = out.getvalue()
        if len(best) <= ANIMATION_TARGET_BYTES:
            break
    return best


def normalise_avatar(data: bytes) -> NormalisedAvatar:
    """Decode an upload and return the picture ready to store.

    The format is decided by the content, never by a name or a declared type. The EXIF orientation is applied, the
    longest side is cut to `OUTPUT_SIDE` (never enlarged, aspect ratio and transparency kept) and every piece of
    metadata is dropped. A single frame becomes a PNG. An animation (GIF, animated WEBP or APNG) stays animated: at
    most `MAX_FRAMES` frames (the rest are dropped), each resized, with their durations and loop count, stored as an
    animated WEBP. `has_alpha` is true only when some pixel is really transparent.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageTooLarge("The image is larger than {limit} MiB.", limit=MAX_UPLOAD_BYTES // (1024 * 1024))
    if not data:
        raise _invalid()
    try:
        with Image.open(io.BytesIO(data), formats=_FORMATS) as image:
            # the sizes come from the headers: a decompression bomb is refused before any pixel is decoded
            if image.width > MAX_INPUT_SIDE or image.height > MAX_INPUT_SIDE:
                raise InvalidImage(
                    "The image is larger than {side} x {side} pixels.", side=MAX_INPUT_SIDE,
                )
            kept = min(getattr(image, "n_frames", 1), MAX_FRAMES)
            if kept * image.width * image.height > MAX_TOTAL_PIXELS:
                raise InvalidImage("The animation is too large (too many pixels in all its frames).")
            orientation = image.getexif().get(0x0112)
            loop = int(image.info.get("loop", 1 if image.format == "GIF" else 0) or 0)  # a GIF without the loop block plays once
            durations: list[int] = []
            frames: list[Image.Image] = []
            for index in range(kept):
                image.seek(index)
                image.load()
                durations.append(max(0, int(image.info.get("duration", 100) or 0)))
                rgba = image.convert("RGBA")
                if orientation in _ORIENTATIONS:
                    rgba = rgba.transpose(_ORIENTATIONS[orientation])
                rgba.thumbnail((OUTPUT_SIDE, OUTPUT_SIDE), Image.Resampling.LANCZOS)
                frames.append(rgba)
    except InputError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, OSError,
            ValueError, SyntaxError, EOFError, TypeError, MemoryError):
        raise _invalid() from None
    has_alpha = any(frame.getchannel("A").getextrema()[0] < 255 for frame in frames)
    if len(frames) > 1:
        clean = [Image.frombytes(f.mode, f.size, f.tobytes()) for f in frames]  # new images: no info, no EXIF
        return NormalisedAvatar(_encode_animation(clean, durations, loop, has_alpha), has_alpha, "image/webp")
    rgba = frames[0]
    result = rgba if has_alpha else rgba.convert("RGB")
    clean_image = Image.frombytes(result.mode, result.size, result.tobytes())
    out = io.BytesIO()
    clean_image.save(out, format="PNG", optimize=True)
    return NormalisedAvatar(out.getvalue(), has_alpha, "image/png")


def normalise_image(data: bytes) -> tuple[bytes, bool]:
    """`(stored_bytes, has_alpha)` of `normalise_avatar` (PNG for a still image, animated WEBP for an animation)."""
    result = normalise_avatar(data)
    return result.data, result.has_alpha


class AvatarStore:
    """The pictures of one installation, under `data_dir`; every path is checked to stay inside the user's `.avatars`."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)

    def folder(self, user_id: str) -> Path:
        if not _ID.fullmatch(user_id or ""):
            raise ValueError("invalid user id for a profile picture")
        return self.data_dir / user_id / AVATARS_DIR

    def path(self, user_id: str, key: str, media_type: str = "image/png") -> Path:
        """The file of `key` (an agent id, or `USER_KEY` for the user); an id that is not a plain slug is refused."""
        if not _ID.fullmatch(key or ""):
            raise ValueError("invalid id for a profile picture")
        extension = _EXTENSIONS.get(media_type)
        if extension is None:
            raise ValueError("unsupported profile picture type")
        folder = self.folder(user_id)
        path = folder / f"{key}{extension}"
        if path.parent != folder:  # defence in depth: the slug check already excludes separators and dots
            raise ValueError("a profile picture must stay inside the avatars folder")
        return path

    def _candidates(self, user_id: str, key: str) -> list[Path]:
        return [self.path(user_id, key, media_type) for media_type in _EXTENSIONS]

    def get(self, user_id: str, key: str) -> Avatar | None:
        for path in self._candidates(user_id, key):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            return Avatar(path=path, version=f"{stat.st_mtime_ns:x}", size=stat.st_size)
        return None

    def read(self, avatar: Avatar) -> bytes | None:
        try:
            return avatar.path.read_bytes()
        except OSError:
            return None

    def save(self, user_id: str, key: str, data: bytes, media_type: str = "image/png") -> Avatar:
        """Replace the picture atomically (a reader never sees half a file); a file of the other type is removed."""
        path = self.path(user_id, key, media_type)
        temporary: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=path.parent, prefix=f".{key}-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
            os.replace(temporary, path)
            for other in self._candidates(user_id, key):
                if other != path:
                    other.unlink(missing_ok=True)
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
            for path in self._candidates(user_id, key):
                path.unlink(missing_ok=True)
        except OSError as exc:
            raise AvatarStorageError(
                "Could not remove the profile picture; check the data folder permissions.",
            ) from exc

    def move(self, user_id: str, old_key: str, new_key: str) -> bool:
        """Rename an agent's picture with the agent; returns whether there was one. A stale file at the target is replaced."""
        self._candidates(user_id, new_key)  # validates the target id
        avatar = self.get(user_id, old_key)
        if avatar is None:
            return False
        target = self.path(user_id, new_key, avatar.media_type)
        try:
            os.replace(avatar.path, target)
            for other in self._candidates(user_id, new_key):
                if other != target:
                    other.unlink(missing_ok=True)
        except OSError as exc:
            raise AvatarStorageError(
                "Could not move the agent's profile picture; close other programs using its files and try again.",
            ) from exc
        return True
