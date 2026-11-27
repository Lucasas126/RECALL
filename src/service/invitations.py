"""Invitations to share an agent: one JSON file per invitation under `<data_dir>/.invitations/`."""

from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path

from src.utils.files import atomic_write_text

PENDING, ACCEPTED, DECLINED, REVOKED = "pending", "accepted", "declined", "revoked"


class InvitationStore:
    """Atomic per-file records; `lock` serialises check-then-write sequences inside this process."""

    def __init__(self, data_dir: Path) -> None:
        self.folder = Path(data_dir) / ".invitations"
        self.lock = threading.RLock()

    def _path(self, invitation_id: str) -> Path:
        return self.folder / f"{invitation_id}.json"

    def create(self, **fields) -> dict:
        record = {"id": secrets.token_hex(6), **fields, "created": int(time.time()), "status": PENDING}
        self.save(record)
        return record

    def save(self, record: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._path(record["id"]), json.dumps(record, ensure_ascii=False, indent=1) + "\n")

    def get(self, invitation_id: str) -> dict | None:
        if not invitation_id.isalnum():
            return None
        try:
            return json.loads(self._path(invitation_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def all(self) -> list[dict]:
        found = []
        for path in sorted(self.folder.glob("*.json")) if self.folder.is_dir() else []:
            try:
                found.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        return sorted(found, key=lambda r: (r.get("created", 0), r.get("id", "")))
