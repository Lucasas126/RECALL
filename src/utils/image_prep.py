"""Prepare an image or PDF pages for a vision model: decoded, downscaled and re-encoded as a small JPEG data URL."""
from __future__ import annotations

import base64
import io
import re

from PIL import Image

MIN_EDGE, DEFAULT_EDGE, MAX_EDGE = 256, 1024, 2048
MAX_SHEET_PAGES = 6
_SHEET_COLUMNS = 3
_SHEET_CELL_EDGE = 560
MAX_PIXELS = 50_000_000  # a decompression-bomb guard: Pillow refuses far more, this refuses anything above 50 MP early


class ImageError(ValueError):
    """The input cannot be shown; the message is for the agent."""


def is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def _flatten(picture: Image.Image) -> Image.Image:
    """RGB on a white background (JPEG has no alpha; a transparent diagram must not turn black)."""
    if picture.mode in ("RGBA", "LA") or (picture.mode == "P" and "transparency" in picture.info):
        rgba = picture.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return picture.convert("RGB")


def _fit(picture: Image.Image, edge: int) -> Image.Image:
    if max(picture.size) <= edge:
        return picture
    scale = edge / max(picture.size)
    return picture.resize((max(1, round(picture.width * scale)), max(1, round(picture.height * scale))), Image.LANCZOS)


def _data_url(picture: Image.Image) -> tuple[str, int]:
    buffer = io.BytesIO()
    picture.save(buffer, "JPEG", quality=80, optimize=True)
    raw = buffer.getvalue()
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"), len(raw)


def clamp_edge(edge: int) -> int:
    return max(MIN_EDGE, min(int(edge), MAX_EDGE))


def parse_pages(spec: str, count: int) -> list[int]:
    """'1-3,5' -> [1, 2, 3, 5] (1-based, in order, no repeats); ImageError for anything out of range or malformed."""
    spec = (spec or "").strip()
    if not spec:
        return [1]
    pages: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        match = re.fullmatch(r"(\d+)(?:\s*-\s*(\d+))?", part)
        if not match:
            raise ImageError(f"pages must look like '2' or '1-3,5', not {spec!r}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start or end > count:
            raise ImageError(f"pages {part!r} are outside this document, which has {count} pages")
        pages.extend(number for number in range(start, end + 1) if number not in pages)
    return pages


def prepare_image(data: bytes, edge: int = DEFAULT_EDGE) -> tuple[str, str, int]:
    """(data URL, description, bytes) of one picture, downscaled so its longest side is at most `edge`."""
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > MAX_PIXELS:
                raise ImageError(f"the image is {source.width}x{source.height}; the limit is {MAX_PIXELS // 1_000_000} megapixels")
            source.load()
            original = source.size
            picture = _fit(_flatten(source), clamp_edge(edge))
    except ImageError:
        raise
    except Exception as exc:  # Pillow raises many types for a corrupt or unsupported file
        raise ImageError("it is not an image Pillow can read") from exc
    url, size = _data_url(picture)
    return url, f"{original[0]}x{original[1]} px, shown at {picture.width}x{picture.height}", size


def render_pdf(data: bytes, pages: str = "", edge: int = DEFAULT_EDGE) -> tuple[str, str, int]:
    """(data URL, description, bytes) of one PDF page, or of a contact sheet when several pages are asked for."""
    import pymupdf

    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ImageError("it is not a readable PDF") from exc
    with document:
        if document.needs_pass:
            raise ImageError("the PDF is encrypted")
        wanted = parse_pages(pages, document.page_count)
        if len(wanted) > MAX_SHEET_PAGES:
            raise ImageError(f"at most {MAX_SHEET_PAGES} pages at once; ask for fewer or call again for the rest")
        single = len(wanted) == 1
        target = clamp_edge(edge) if single else _SHEET_CELL_EDGE
        rendered = []
        for number in wanted:
            page = document[number - 1]
            scale = target / max(page.rect.width, page.rect.height)
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
            rendered.append(Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples))
        count = document.page_count
    if single:
        url, size = _data_url(rendered[0])
        return url, f"page {wanted[0]} of {count}, {rendered[0].width}x{rendered[0].height} px", size
    columns = min(_SHEET_COLUMNS, len(rendered))
    rows = -(-len(rendered) // columns)
    cell_w = max(image.width for image in rendered)
    cell_h = max(image.height for image in rendered)
    gap = 8
    sheet = Image.new("RGB", (columns * cell_w + (columns + 1) * gap, rows * cell_h + (rows + 1) * gap), (150, 150, 150))
    for index, image in enumerate(rendered):
        row, column = divmod(index, columns)
        sheet.paste(image, (gap + column * (cell_w + gap), gap + row * (cell_h + gap)))
    url, size = _data_url(sheet)
    return url, f"contact sheet of pages {', '.join(map(str, wanted))} of {count} (left to right, top to bottom)", size
