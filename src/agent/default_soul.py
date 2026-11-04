from __future__ import annotations

from pathlib import Path

_TEMPLATE = Path(__file__).with_name("default_soul.md")


def default_soul() -> str:
    """The SOUL.md text a newly created agent starts with (a packaged, editable template)."""
    return _TEMPLATE.read_text(encoding="utf-8").replace("\r\n", "\n")
