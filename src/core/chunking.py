from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

# bump when the chunk boundaries or texts change, so existing indexes are rebuilt
CHUNKING_VERSION = 2

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?…])[\"')\]]*\s+")


def estimate_tokens(text: str) -> int:
    """A cheap token estimate (characters / 4), close enough for sizing embedding chunks."""
    return (len(text) + 3) // 4


@dataclass(frozen=True)
class ChunkSizes:
    """Chunk sizes in estimated tokens."""

    target: int = 400
    maximum: int = 600
    overlap: int = 60


@dataclass(frozen=True)
class Chunk:
    """One embeddable piece: `text` is the prefix line plus the body; `context` is the heading path."""

    text: str
    body: str
    context: str


@dataclass(frozen=True)
class _Block:
    text: str
    code: bool = False


def _blocks(lines: list[str]) -> list[_Block]:
    """Paragraphs (separated by blank lines) and fenced code blocks, which are kept whole."""
    blocks: list[_Block] = []
    paragraph: list[str] = []
    fence: str | None = None
    code: list[str] = []

    def flush() -> None:
        if paragraph and "".join(paragraph).strip():
            blocks.append(_Block("\n".join(paragraph).strip("\n")))
        paragraph.clear()

    for line in lines:
        if fence is not None:
            code.append(line)
            if line.strip().startswith(fence):
                blocks.append(_Block("\n".join(code), code=True))
                fence, code = None, []
            continue
        match = _FENCE_RE.match(line)
        if match:
            flush()
            fence = match.group(1)[0] * len(match.group(1))
            code = [line]
        elif not line.strip():
            flush()
        else:
            paragraph.append(line)
    flush()
    if code:  # an unclosed fence runs to the end of the section
        blocks.append(_Block("\n".join(code), code=True))
    return blocks


def _sections(text: str) -> list[tuple[list[str], list[str]]]:
    """Split Markdown into (heading path, body lines) sections; headings inside code fences do not count."""
    sections: list[tuple[list[str], list[str]]] = []
    path: list[tuple[int, str]] = []
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        if fence is not None:
            lines.append(line)
            if line.strip().startswith(fence):
                fence = None
            continue
        match = _FENCE_RE.match(line)
        if match:
            fence = match.group(1)[0] * len(match.group(1))
            lines.append(line)
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            sections.append(([title for _, title in path], lines))
            level = len(heading.group(1))
            path = [item for item in path if item[0] < level] + [(level, heading.group(2).strip())]
            lines = []
            continue
        lines.append(line)
    sections.append(([title for _, title in path], lines))
    return sections


def _split_text(text: str, limit: int) -> list[str]:
    """Split prose into pieces of at most `limit` tokens, at sentence boundaries, else at spaces."""
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END_RE.split(text):
        if not sentence:
            continue
        if estimate_tokens(sentence) > limit:
            if current:
                pieces.append(current)
                current = ""
            words = sentence.split(" ")
            for word in words:
                while estimate_tokens(word) > limit:  # one huge token (a URL, base64): cut it hard
                    head, word = word[: limit * 4], word[limit * 4:]
                    if current:
                        pieces.append(current)
                        current = ""
                    pieces.append(head)
                candidate = f"{current} {word}" if current else word
                if current and estimate_tokens(candidate) > limit:
                    pieces.append(current)
                    current = word
                else:
                    current = candidate
            continue
        candidate = f"{current} {sentence}" if current else sentence
        if current and estimate_tokens(candidate) > limit:
            pieces.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        pieces.append(current)
    return [piece.strip() for piece in pieces if piece.strip()]


def _split_code(text: str, limit: int) -> list[str]:
    """Split an oversized code block by lines; each piece stays a fenced block."""
    lines = text.split("\n")
    opening = lines[0]
    closing = lines[-1] if len(lines) > 1 and _FENCE_RE.match(lines[-1]) else ""
    inner = lines[1:-1] if closing else lines[1:]
    pieces: list[str] = []
    current: list[str] = []
    for line in inner:
        for part in _split_text(line, limit) if estimate_tokens(line) > limit else [line]:
            if current and estimate_tokens("\n".join([opening, *current, part, closing])) > limit:
                pieces.append("\n".join([opening, *current, closing or opening[:3]]))
                current = []
            current.append(part)
    if current or not pieces:
        pieces.append("\n".join([opening, *current, closing or opening[:3]]))
    return pieces


def _overlap_tail(body: str, tokens: int) -> str:
    """The last ~`tokens` of a chunk body, starting at a sentence (else a word) boundary."""
    if tokens <= 0 or not body:
        return ""
    budget = tokens * 4
    if len(body) <= budget:
        return body
    tail = body[-budget:]
    sentence = _SENTENCE_END_RE.search(tail)
    if sentence and sentence.end() < len(tail) - 20:
        return tail[sentence.end():].strip()
    space = tail.find(" ")
    return tail[space + 1:].strip() if space != -1 else tail.strip()


def _pack(blocks: list[_Block], sizes: ChunkSizes) -> list[str]:
    """Pack blocks into bodies of about `target` tokens (never over `maximum`), with overlap between them."""
    units: list[_Block] = []
    for block in blocks:
        if estimate_tokens(block.text) <= sizes.maximum:
            units.append(block)
        elif block.code:
            units.extend(_Block(piece, code=True) for piece in _split_code(block.text, sizes.target))
        else:
            units.extend(_Block(piece) for piece in _split_text(block.text, sizes.target))
    bodies: list[str] = []
    current: list[_Block] = []
    has_new = False  # the current chunk holds more than the overlap carried from the previous one

    def size(parts: list[_Block]) -> int:
        return estimate_tokens("\n\n".join(part.text for part in parts))

    for unit in units:
        if current and has_new and size(current + [unit]) > sizes.target:
            bodies.append("\n\n".join(part.text for part in current))
            last = current[-1]
            tail = "" if last.code else _overlap_tail(last.text, sizes.overlap)
            current = [_Block(tail)] if tail and size([_Block(tail), unit]) <= sizes.maximum else []
            has_new = False
        current.append(unit)
        has_new = True
    if current and has_new:
        bodies.append("\n\n".join(part.text for part in current))
    return bodies


def _prefix(address: str, path: list[str]) -> str:
    return " > ".join([address, *path])


def _title(address: str) -> str:
    """The note title from its file name: `source:folder/My note.md` -> `My note`."""
    name = address.split(":", 1)[-1].rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def chunk_markdown(text: str, address: str, sizes: ChunkSizes = ChunkSizes(), *, title: str | None = None) -> list[Chunk]:
    """Split Markdown or plain text by headings, then paragraphs, prefixing each chunk with its context.

    The prefix is `<address> > <title> > <heading path>`; the title (the file name unless given) is left
    out when the first heading already repeats it.
    """
    title = _title(address) if title is None else title
    chunks: list[Chunk] = []
    for path, lines in _sections(text):
        blocks = _blocks(lines)
        if not blocks:
            continue
        full = path if path and title and path[0].strip().lower() == title.strip().lower() else ([title] if title else []) + path
        context = " > ".join(full)
        prefix = _prefix(address, full)
        for body in _pack(blocks, sizes):
            chunks.append(Chunk(f"{prefix}\n\n{body}", body, context))
    return chunks


def chunk_skill(text: str, address: str, sizes: ChunkSizes = ChunkSizes()) -> list[Chunk]:
    """One chunk per section of a SKILL.md (split further only when too long), prefixed with name and description."""
    meta: dict = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            try:
                loaded = yaml.safe_load(text[3:end]) or {}
            except yaml.YAMLError:
                loaded = {}
            if isinstance(loaded, dict):
                meta = loaded
                body = text[end + 4:].lstrip("\n")
    folder = address.rsplit("/", 2)[-2] if address.count("/") >= 1 else address
    name = str(meta.get("name") or folder)
    description = " ".join(str(meta.get("description") or "").split())
    label = f"skill {name}: {description}" if description else f"skill {name}"
    sections = [Chunk(c.text.replace(address, f"{address} > {label}", 1), c.body, c.context)
                for c in chunk_markdown(body, address, ChunkSizes(sizes.maximum, sizes.maximum, 0), title="")]
    if sections:
        return sections
    return [Chunk(f"{address} > {label}\n\n{description}", description, "")] if description else []


def chunk_exchange(prefix: str, user: str, assistant: str, sizes: ChunkSizes = ChunkSizes()) -> list[Chunk]:
    """One chunk per exchange (user message plus reply); very long exchanges are split by the Markdown rules."""
    text = f"user: {user.strip()}\n\nassistant: {assistant.strip()}".strip()
    if estimate_tokens(text) <= sizes.maximum:
        return [Chunk(f"{prefix}\n\n{text}", text, "")]
    pieces = [_Block(piece.text, piece.code) for piece in _blocks(text.splitlines())]
    return [Chunk(f"{prefix}\n\n{body}", body, "") for body in _pack(pieces, sizes)]


def excerpt(text: str, limit: int = 400) -> str:
    """A single-line excerpt of at most `limit` characters, cut at a sentence end when one is close enough."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    head = text[:limit]
    ends = [match.end() for match in _SENTENCE_END_RE.finditer(head)]
    if head[-1:] in ".!?…" and text[limit:limit + 1] == " ":
        ends.append(limit)
    best = max((end for end in ends if end >= limit // 2), default=None)
    if best is not None:
        return head[:best].strip()
    cut = head.rsplit(" ", 1)[0] if " " in head[limit // 2:] else head
    return cut.rstrip(" ,;:") + "…"
