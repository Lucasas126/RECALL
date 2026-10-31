from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.utils.files import atomic_write_text
from src.utils.i18n import UserMessageError

FavoriteModelId = Annotated[str, Field(strict=True, min_length=1, max_length=256)]


class FavoritesError(UserMessageError, RuntimeError):
    """Model favorites could not be read, validated or saved."""


class _Favorites(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_ids: list[FavoriteModelId]


class ModelFavorites:
    """User-scoped model preferences with atomic writes and in-process serialization."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._lock = threading.Lock()

    def _path(self, user_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user_id):
            raise ValueError("invalid user id for model favorites")
        user_root = self.root / user_id
        path = (user_root / "openrouter-favorites.json").resolve()
        if not path.is_relative_to(user_root) or not user_root.is_dir():
            raise ValueError("model favorites require an existing user configuration directory")
        return path

    @staticmethod
    def _load(path: Path) -> _Favorites:
        try:
            return _Favorites.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _Favorites(model_ids=[])
        except (OSError, UnicodeError, ValidationError) as exc:
            raise FavoritesError(
                "Could not read model favorites; check the user's openrouter-favorites.json."
            ) from exc

    def list(self, user_id: str) -> list[str]:
        with self._lock:
            return self._load(self._path(user_id)).model_ids

    def update(self, user_id: str, model_id: str, *, favorite: bool) -> list[str]:
        """Add or remove an ID idempotently; adding requires validation by the caller."""
        _Favorites(model_ids=[model_id])
        with self._lock:
            path = self._path(user_id)
            data = self._load(path)
            if (model_id in data.model_ids) == favorite:
                return data.model_ids
            if favorite:
                data.model_ids.append(model_id)
            else:
                data.model_ids = [saved for saved in data.model_ids if saved != model_id]
            try:
                atomic_write_text(path, data.model_dump_json(indent=2) + "\n")
            except OSError as exc:
                raise FavoritesError("Could not save model favorites; check configuration directory permissions.") from exc
            return data.model_ids
