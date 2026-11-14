"""Build a PDF from a structured spec with reportlab (the engine behind the `make_pdf` tool).

The agent never writes layout code: it describes the document as a list of elements and this module lays it out.
Standard PDF fonts are used, so text outside the WinAnsi (Windows-1252) range cannot be drawn; such characters are
reported back instead of being rendered as black boxes.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from PIL import Image
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image as PlatypusImage, KeepTogether, ListFlowable, ListItem, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
    TableStyle,
)

PAGE_SIZES = {"a4": A4, "letter": letter}
MAX_ELEMENTS = 500
MAX_TABLE_CELLS = 2000
MAX_TEXT_CHARS = 200_000
ELEMENT_TYPES = ("heading", "paragraph", "bullets", "numbered", "table", "image", "spacer", "pagebreak")
_MARGIN = 20 * mm

_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", re.DOTALL)


class SpecError(ValueError):
    """The spec is not valid; the message says which element and what to change."""


@dataclass
class BuiltPdf:
    data: bytes
    pages: int
    warnings: list[str] = field(default_factory=list)


def _unsupported(text: str) -> set[str]:
    bad = set()
    for char in text:
        try:
            char.encode("cp1252")
        except UnicodeEncodeError:
            if not char.isspace():
                bad.add(char)
    return bad


def _markup(text: str) -> str:
    """Escape the text for reportlab, then turn `**bold**` and `*italic*` into tags and line breaks into <br/>."""
    safe = escape(text)
    safe = _BOLD.sub(r"<b>\1</b>", safe)
    safe = _ITALIC.sub(r"<i>\1</i>", safe)
    return safe.replace("\r\n", "\n").replace("\n", "<br/>")


def _text_of(element: dict, key: str, where: str) -> str:
    value = element.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{where}: '{key}' must be a non-empty string")
    return value


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=base["BodyText"], fontName="Helvetica", fontSize=10.5, leading=14.5,
                          spaceAfter=6, alignment=TA_LEFT)
    return {
        "body": body,
        "cell": ParagraphStyle("cell", parent=body, fontSize=9.5, leading=12, spaceAfter=0),
        "head": ParagraphStyle("head", parent=body, fontName="Helvetica-Bold", fontSize=9.5, leading=12, spaceAfter=0,
                               textColor=colors.white),
        "h1": ParagraphStyle("h1", parent=body, fontName="Helvetica-Bold", fontSize=18, leading=22, spaceBefore=10,
                             spaceAfter=8, textColor=colors.HexColor("#1f2d3d")),
        "h2": ParagraphStyle("h2", parent=body, fontName="Helvetica-Bold", fontSize=14, leading=18, spaceBefore=8,
                             spaceAfter=6, textColor=colors.HexColor("#1f2d3d")),
        "h3": ParagraphStyle("h3", parent=body, fontName="Helvetica-Bold", fontSize=11.5, leading=15, spaceBefore=6,
                             spaceAfter=4, textColor=colors.HexColor("#1f2d3d")),
        "title": ParagraphStyle("title", parent=body, fontName="Helvetica-Bold", fontSize=24, leading=29,
                                spaceAfter=14, textColor=colors.HexColor("#1f2d3d")),
    }


def _table(element: dict, where: str, styles: dict, width: float, texts: list[str]):
    rows = element.get("rows")
    if not isinstance(rows, list) or not rows or not all(isinstance(row, list) and row for row in rows):
        raise SpecError(f"{where}: 'rows' must be a non-empty list of non-empty lists")
    columns = max(len(row) for row in rows)
    if sum(len(row) for row in rows) > MAX_TABLE_CELLS:
        raise SpecError(f"{where}: more than {MAX_TABLE_CELLS} cells; split the table")
    header = element.get("header", True)
    data = []
    for index, row in enumerate(rows):
        cells = []
        for cell in [*row, *[""] * (columns - len(row))]:
            text = "" if cell is None else str(cell)
            texts.append(text)
            cells.append(Paragraph(_markup(text), styles["head" if header and index == 0 else "cell"]))
        data.append(cells)
    table = Table(data, colWidths=[width / columns] * columns, repeatRows=1 if header else 0)
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b8c0c8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    if header:
        commands.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#3b5870")))
    if len(data) > 2:
        commands.append(("ROWBACKGROUNDS", (0, 1 if header else 0), (-1, -1), [colors.white, colors.HexColor("#f2f5f8")]))
    table.setStyle(TableStyle(commands))
    return table


def _image(element: dict, where: str, width: float, load_image) -> PlatypusImage:
    address = _text_of(element, "path", where)
    try:
        data = load_image(address)
    except Exception as exc:  # the loader says what went wrong (unknown source, not readable, missing file)
        raise SpecError(f"{where}: cannot read image '{address}' ({exc})") from exc
    try:
        with Image.open(io.BytesIO(data)) as picture:
            picture.load()
            pixel_width, pixel_height = picture.size
            if picture.mode not in ("RGB", "L"):
                picture = picture.convert("RGB")
            buffer = io.BytesIO()
            picture.save(buffer, "PNG")
    except Exception as exc:
        raise SpecError(f"{where}: '{address}' is not an image Pillow can read") from exc
    percent = element.get("width_percent", 100)
    if not isinstance(percent, (int, float)) or not 5 <= percent <= 100:
        raise SpecError(f"{where}: 'width_percent' must be a number from 5 to 100")
    shown = width * percent / 100
    buffer.seek(0)
    return PlatypusImage(buffer, width=shown, height=shown * pixel_height / pixel_width, hAlign="CENTER")


def build_pdf(
    title: str, elements: list, *, author: str = "", page_size: str = "a4", page_numbers: bool = True,
    load_image=lambda address: (_ for _ in ()).throw(ValueError("images are not available here")),
) -> BuiltPdf:
    """Lay out `elements` and return the PDF bytes, the page count and warnings (for example unsupported characters)."""
    if not isinstance(title, str) or not title.strip():
        raise SpecError("'title' must be a non-empty string")
    if not isinstance(elements, list) or not elements:
        raise SpecError("'elements' must be a non-empty list")
    if len(elements) > MAX_ELEMENTS:
        raise SpecError(f"more than {MAX_ELEMENTS} elements; split the document")
    size = PAGE_SIZES.get(str(page_size).lower())
    if size is None:
        raise SpecError(f"'page_size' must be one of: {', '.join(PAGE_SIZES)}")
    styles = _styles()
    width = size[0] - 2 * _MARGIN
    texts: list[str] = [title, author]
    story: list = [Paragraph(_markup(title), styles["title"])]

    for number, element in enumerate(elements, start=1):
        if not isinstance(element, dict):
            raise SpecError(f"element {number}: must be an object with a 'type'")
        kind = element.get("type")
        where = f"element {number} ({kind})"
        if kind not in ELEMENT_TYPES:
            raise SpecError(f"element {number}: unknown type {kind!r}; use one of: {', '.join(ELEMENT_TYPES)}")
        if kind == "heading":
            level = element.get("level", 1)
            if level not in (1, 2, 3):
                raise SpecError(f"{where}: 'level' must be 1, 2 or 3")
            text = _text_of(element, "text", where)
            texts.append(text)
            story.append(Paragraph(_markup(text), styles[f"h{level}"]))
        elif kind == "paragraph":
            text = _text_of(element, "text", where)
            texts.append(text)
            story.append(Paragraph(_markup(text), styles["body"]))
        elif kind in ("bullets", "numbered"):
            items = element.get("items")
            if not isinstance(items, list) or not items or not all(isinstance(item, str) and item.strip() for item in items):
                raise SpecError(f"{where}: 'items' must be a non-empty list of non-empty strings")
            texts.extend(items)
            story.append(ListFlowable(
                [ListItem(Paragraph(_markup(item), styles["body"])) for item in items],
                bulletType="1" if kind == "numbered" else "bullet", start=None if kind == "bullets" else 1,
                leftIndent=14, bulletFontName="Helvetica",
            ))
        elif kind == "table":
            story.append(_table(element, where, styles, width, texts))
            story.append(Spacer(1, 8))
        elif kind == "image":
            story.append(KeepTogether([_image(element, where, width, load_image), Spacer(1, 8)]))
        elif kind == "spacer":
            height = element.get("height_mm", 6)
            if not isinstance(height, (int, float)) or not 1 <= height <= 100:
                raise SpecError(f"{where}: 'height_mm' must be a number from 1 to 100")
            story.append(Spacer(1, height * mm))
        else:
            story.append(PageBreak())

    joined = "\n".join(texts)
    if len(joined) > MAX_TEXT_CHARS:
        raise SpecError(f"more than {MAX_TEXT_CHARS} characters of text; split the document")

    def footer(canvas, document) -> None:
        if not page_numbers:
            return
        canvas.saveState()
        canvas.setFont("Helvetica", 8.5)
        canvas.setFillColor(colors.HexColor("#6b7785"))
        canvas.drawCentredString(size[0] / 2, 10 * mm, str(document.page))
        canvas.restoreState()

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=size, leftMargin=_MARGIN, rightMargin=_MARGIN, topMargin=_MARGIN, bottomMargin=_MARGIN + 4 * mm,
        title=title.strip(), author=author.strip(),
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)

    warnings = []
    if bad := _unsupported(joined):
        shown = " ".join(sorted(bad)[:12])
        warnings.append(
            f"these characters cannot be drawn with the standard fonts and appear as boxes or are dropped: {shown}; "
            "write them as words and rebuild"
        )
    return BuiltPdf(data=buffer.getvalue(), pages=_count_pages(buffer.getvalue()), warnings=warnings)


def _count_pages(data: bytes) -> int:
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as document:
        return document.page_count
