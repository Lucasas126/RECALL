"""Classify a file by its name for viewers: (kind, mime). Shared by the file panel API and agent tools."""
from __future__ import annotations

import mimetypes
import posixpath

_TEXT = {".txt", ".log", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env.example", ".jsonl", ".rst"}
_CODE = {
    ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".css", ".scss", ".sh", ".bash", ".zsh", ".c", ".h", ".cpp", ".hpp",
    ".java", ".go", ".rs", ".rb", ".php", ".sql", ".xml", ".kt", ".swift", ".lua", ".pl", ".r", ".cs", ".vue",
}
_IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_MIMES = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".yaml": "text/plain", ".yml": "text/plain",
    ".toml": "text/plain", ".ini": "text/plain", ".log": "text/plain", ".json": "application/json",
    ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".svg": "image/svg+xml", ".webp": "image/webp",
    ".pdf": "application/pdf", ".html": "text/html", ".htm": "text/html",
}


def file_kind(name: str) -> tuple[str, str]:
    """`(kind, mime)`: kind is markdown, text, code, image, pdf, csv, html or binary (mime application/octet-stream)."""
    ext = posixpath.splitext(name.lower())[1]
    if ext in (".md", ".markdown"):
        kind = "markdown"
    elif ext in _TEXT:
        kind = "text"
    elif ext in _CODE:
        kind = "code"
    elif ext in _IMAGE:
        kind = "image"
    elif ext == ".pdf":
        kind = "pdf"
    elif ext in (".csv", ".tsv"):
        kind = "csv"
    elif ext in (".html", ".htm"):
        kind = "html"
    else:
        return "binary", "application/octet-stream"
    mime = _MIMES.get(ext) or mimetypes.guess_type(name)[0] or ("text/plain" if kind in ("text", "code") else "application/octet-stream")
    return kind, mime
