from __future__ import annotations

import re
import unicodedata
from contextlib import nullcontext
from typing import TYPE_CHECKING, Annotated, Callable

from src.agent.tooling import tool
from src.agent.toolsets.common import mark_written, readonly_error
from src.utils.pdf_build import ELEMENT_TYPES, SpecError, build_pdf

if TYPE_CHECKING:
    from src.agent.agent import Agent

DEFAULT_FOLDER = "mind_palace:documents"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_PDF_BYTES = 10 * 1024 * 1024  # the same cap `send_file` applies, so what is built can be sent


def slugify(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", plain).strip("-").lower()
    return slug[:60].strip("-") or "document"


def build_pdf_tools(agent: Agent) -> list[Callable[..., str]]:
    """The `make_pdf` tool: a structured spec in, a PDF file in a writable source out (nothing is sent to the user)."""
    sources = agent.sources

    def locked():
        try:
            return sources.agent_files().memory_lock()
        except (KeyError, AttributeError):
            return nullcontext()

    def load_image(address: str) -> bytes:
        source, rel = sources.resolve(address)
        if not source.readable:
            raise PermissionError(f"source '{source.name}' is not readable here")
        target = source.resolve(rel)
        if not target.is_file():
            raise FileNotFoundError("not a file")
        if target.stat().st_size > MAX_IMAGE_BYTES:
            raise ValueError(f"larger than {MAX_IMAGE_BYTES // 1024 // 1024} MiB")
        return source.read_bytes(rel)

    def free_address(address: str) -> str:
        source, rel = sources.resolve(address)
        stem, dot, suffix = rel.rpartition(".")
        for number in range(2, 1000):
            if not source.exists(rel):
                break
            rel = f"{stem}-{number}.{suffix}"
        return f"{source.name}:{rel}"

    @tool
    def make_pdf(
        title: Annotated[str, "document title (printed at the top and stored in the PDF)"],
        elements: Annotated[list, (
            "the content, in order: a list of objects with a 'type': "
            "heading {text, level 1-3}, paragraph {text}, bullets {items: [text]}, numbered {items: [text]}, "
            "table {rows: [[cell]], header: true|false}, image {path: 'source:path', width_percent: 5-100}, "
            "spacer {height_mm}, pagebreak. Text accepts **bold**, *italic* and line breaks. "
            "Write accented letters normally; avoid symbols such as arrows or check marks (use words)."
        )],
        path: Annotated[str, (
            "where to save it, as 'source:path' ending in .pdf, in a source you may write (mind_palace or a user folder "
            f"that is not read-only). Default: {DEFAULT_FOLDER}/<title>.pdf"
        )] = "",
        author: Annotated[str, "optional author stored in the PDF"] = "",
        page_size: Annotated[str, "a4 (default) or letter"] = "a4",
        page_numbers: Annotated[bool, "print page numbers (default true)"] = True,
        overwrite: Annotated[bool, "replace the file when it exists; by default a number is added to the name"] = False,
    ) -> str:
        """Create a PDF from a structured description and save it as a file; it does not send it to the user (use send_file
        for that). Returns the file's address, the page count and any warning. To check the layout of a long or visual
        document, read the PDF back with `read` or `folder_read` (page text) before sending it."""
        target = path.strip() or f"{DEFAULT_FOLDER}/{slugify(title)}.pdf"
        if not target.lower().endswith(".pdf"):
            return "error: the path must end in .pdf"
        try:
            if error := readonly_error(sources, target):
                return error
            built = build_pdf(
                title, elements, author=author, page_size=page_size, page_numbers=page_numbers, load_image=load_image,
            )
            if len(built.data) > MAX_PDF_BYTES:
                return f"error: the PDF is {len(built.data) // 1024} KiB; the limit is {MAX_PDF_BYTES // 1024 // 1024} MiB. Use smaller images or split it"
            with locked():
                final = target if overwrite else free_address(target)
                written = sources.write_bytes(final, built.data)
        except (SpecError, ValueError, KeyError, PermissionError, OSError) as exc:  # a bad spec or address: tell the agent
            return f"error: {exc}"
        mark_written(agent, final)
        note = f" Warning: {'; '.join(built.warnings)}." if built.warnings else ""
        return f"saved {final} ({built.pages} pages, {len(built.data)} bytes, {written}).{note}"

    return [make_pdf]


__all__ = ["build_pdf_tools", "slugify", "ELEMENT_TYPES"]
