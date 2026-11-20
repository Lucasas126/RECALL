from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.utils.files import atomic_write_text, exclusive_file_lock
from src.utils.i18n import UserMessageError


# bounds of the numeric preferences; the interface reads them from the preferences `defaults` object
ASSOCIATIVE_MAX_MATCHES_BOUNDS = (1, 20)
ASSOCIATIVE_PALACE_SLOTS_BOUNDS = (0, 20)
DISPOSABLE_RETENTION_DAYS_BOUNDS = (1, 365)


class PreferencesError(UserMessageError, RuntimeError):
    """User preferences could not be read, validated or saved."""


class UserPreferences(BaseModel):
    """User-wide display, notification language and agent preferences (`agent_id` is the last used agent)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    show_thinking: bool = Field(default=True, strict=True)
    # whether the agent's picture is shown large at the start of its replies in the web chat
    show_agent_avatar: bool = Field(default=True, strict=True)
    language: Literal["en", "es"] = "en"
    agent_id: Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_-]{1,64}$")] | None = None
    default_llm_model: Annotated[str, Field(strict=True, min_length=1, max_length=256)] | None = None
    default_stt_model: Annotated[str, Field(strict=True, min_length=1, max_length=256)] | None = None
    # spoken language for voice transcription ("es", "es-ES"); None follows user.yaml `models.stt_language`, then settings.yaml
    stt_language: Annotated[str, Field(strict=True, pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")] | None = None
    # most associative-memory fragments injected per turn; None follows settings.yaml `associative_memory.top_n`
    associative_max_matches: Annotated[int, Field(strict=True, ge=ASSOCIATIVE_MAX_MATCHES_BOUNDS[0], le=ASSOCIATIVE_MAX_MATCHES_BOUNDS[1])] | None = None
    # of those, how many are reserved for the agent's mind palace; None follows settings.yaml `associative_memory.palace_slots`
    associative_palace_slots: Annotated[int, Field(strict=True, ge=ASSOCIATIVE_PALACE_SLOTS_BOUNDS[0], le=ASSOCIATIVE_PALACE_SLOTS_BOUNDS[1])] | None = None
    # days of inactivity after which a disposable chat is deleted; None means the default (30)
    disposable_retention_days: Annotated[int, Field(strict=True, ge=DISPOSABLE_RETENTION_DAYS_BOUNDS[0], le=DISPOSABLE_RETENTION_DAYS_BOUNDS[1])] | None = None
    # Telegram orchestrator for this user; None follows settings.yaml `orchestrator.enabled`
    orchestrator: bool | None = Field(default=None, strict=True)
    # agent for new chats nobody picked an agent for (and the Telegram router's fallback); None means the first agent
    default_agent_id: Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_-]{1,64}$")] | None = None
    # agent used when the orchestrator is unsure (greetings, general talk); None means the default agent
    core_agent_id: Annotated[str, Field(strict=True, pattern=r"^[A-Za-z0-9_-]{1,64}$")] | None = None
    # how the user's own profile picture is shown: cropped into a circle, or as it is (for transparent PNGs)
    avatar_style: Literal["circle", "free"] = "circle"

    def language_for(self, configured: str) -> Literal["en", "es"]:
        """Resolve an explicit language override or the language configured in YAML."""
        if "language" in self.model_fields_set:
            return self.language
        return "es" if configured.split("-", 1)[0] == "es" else "en"


class UserPreferencesStore:
    """Persist explicit overrides without caching or rewriting user YAML."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, user_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", user_id):
            raise ValueError("invalid user id for preferences")
        user_root = self.root / user_id
        path = (user_root / "preferences.json").resolve()
        if not path.is_relative_to(user_root) or not user_root.is_dir():
            raise ValueError("preferences require an existing user configuration directory")
        return path

    @staticmethod
    def _load(path: Path) -> UserPreferences:
        try:
            return UserPreferences.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return UserPreferences()
        except (OSError, UnicodeError, ValidationError) as exc:
            raise PreferencesError(
                "Could not read preferences; check the user's preferences.json."
            ) from exc

    def get(self, user_id: str) -> UserPreferences:
        return self._load(self._path(user_id))

    def update(
        self, user_id: str, changes: UserPreferences,
        *, validate: Callable[[UserPreferences], UserPreferences] | None = None,
    ) -> UserPreferences:
        """Merge only explicitly supplied fields under an interprocess lock."""
        path = self._path(user_id)
        lock_path = path.with_suffix(".lock").resolve()
        if not lock_path.is_relative_to(path.parent):
            raise ValueError("preferences lock must stay inside the user's configuration directory")
        try:
            with exclusive_file_lock(lock_path):
                current = self._load(path)
                updated = UserPreferences.model_validate(
                    current.model_dump(exclude_unset=True) | changes.model_dump(exclude_unset=True)
                )
                if validate is not None:
                    validate(updated)
                atomic_write_text(path, updated.model_dump_json(exclude_unset=True, indent=2) + "\n")
                return updated
        except OSError as exc:
            raise PreferencesError(
                "Could not save preferences; check configuration directory permissions "
                "and whether another process is holding the preferences lock."
            ) from exc
