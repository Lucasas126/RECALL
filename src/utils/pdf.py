from __future__ import annotations

import io
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pymupdf
import pytesseract
from PIL import Image

from src.config.models import PdfConfig
from src.utils.i18n import InputError
from src.utils.logger import get_logger

logger = get_logger("pdf")

_OCR_TIMEOUT_SECONDS = 60
_MIN_IMAGE_AREA_RATIO = 0.05  # images smaller than this share of the page are logos or icons
_MAX_IMAGES_PER_PAGE = 10     # bounds OCR cost on image-heavy pages
_DEFAULT_WINDOWS_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
_TESSERACT_MISSING = (
    "Tesseract is needed to read scanned PDF pages but was not found. "
    "Install it (see docs/config.md, Tesseract OCR section) or set pdf.tesseract_cmd in settings.yaml."
)

OcrFunction = Callable[[bytes], str]


@dataclass(frozen=True)
class PdfPage:
    """The text of one page and whether it came from Tesseract."""

    number: int
    text: str
    ocr: bool


@dataclass(frozen=True)
class PdfText:
    """Extracted text for every page of a PDF, in page order."""

    pages: list[PdfPage]

    def render(self) -> str:
        """Plain text with a `[Page N]` marker before each page, so answers can cite pages."""
        blocks = []
        for page in self.pages:
            marker = f"[Page {page.number}, OCR]" if page.ocr else f"[Page {page.number}]"
            blocks.append(f"{marker}\n{page.text or '(no text)'}")
        return "\n\n".join(blocks)


def extract_pdf_text(data: bytes, name: str, config: PdfConfig, ocr: OcrFunction) -> PdfText:
    """Read digital text from every page and call `ocr` only for pages with too little of it."""
    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except pymupdf.FileDataError as exc:
        raise InputError("{name} is not a valid PDF file.", name=name) from exc
    with document:
        if document.needs_pass:
            raise InputError("{name} is password-protected. Remove the password and attach it again.", name=name)
        if document.page_count == 0:
            raise InputError("{name} has no pages.", name=name)
        if document.page_count > config.max_pages:
            raise InputError(
                "{name} has {pages} pages; the limit is {limit} pages per PDF.",
                name=name, pages=document.page_count, limit=config.max_pages,
            )
        pages: list[PdfPage] = []
        for number, page in enumerate(document, start=1):
            digital = page.get_text("text", sort=True).strip()
            if _visible_length(digital) >= config.ocr_min_chars:
                # Text is sufficient, but large images (such as a card's printed details) can hold more content.
                image_texts = _ocr_large_images(page, config, ocr)
                if image_texts:
                    pages.append(PdfPage(number, "\n\n".join([digital, *image_texts]), ocr=True))
                else:
                    pages.append(PdfPage(number, digital, ocr=False))
                continue
            recognized = ocr(_render_page(page, config.ocr_dpi)).strip()
            pages.append(PdfPage(number, max(digital, recognized, key=len), ocr=True))
    if not any(page.text for page in pages):
        raise InputError("No text could be read from {name}.", name=name)
    logger.info("pdf pages=%d ocr_pages=%d", len(pages), sum(page.ocr for page in pages))
    return PdfText(pages)


def run_tesseract(png: bytes, config: PdfConfig) -> str:
    """Recognize one rendered page with the local Tesseract executable."""
    # pytesseract reads its executable path from a module attribute; set it per call from the config.
    pytesseract.pytesseract.tesseract_cmd = tesseract_command(config)
    try:
        with Image.open(io.BytesIO(png)) as image:
            return pytesseract.image_to_string(image, lang=config.ocr_languages, timeout=_OCR_TIMEOUT_SECONDS)
    except pytesseract.TesseractError as exc:
        raise InputError(
            "Tesseract could not read a page. Check that the language packs {languages} are installed.",
            languages=config.ocr_languages,
        ) from exc
    except RuntimeError as exc:
        # pytesseract reports a timed-out process as a plain RuntimeError
        raise InputError("Tesseract timed out reading a page.") from exc


def tesseract_command(config: PdfConfig) -> str:
    """Resolve the Tesseract executable from settings, PATH or the default Windows install folder."""
    if config.tesseract_cmd is not None:
        if not Path(config.tesseract_cmd).is_file():
            raise InputError("Tesseract executable not found at {path}. Fix pdf.tesseract_cmd in settings.yaml.",
                             path=config.tesseract_cmd)
        return config.tesseract_cmd
    found = shutil.which("tesseract")
    if found is None and _DEFAULT_WINDOWS_TESSERACT.is_file():
        found = str(_DEFAULT_WINDOWS_TESSERACT)
    if found is None:
        raise InputError(_TESSERACT_MISSING)
    return found


def _ocr_large_images(page: pymupdf.Page, config: PdfConfig, ocr: OcrFunction) -> list[str]:
    """OCR the embedded images that cover a meaningful part of the page, skipping logos and icons."""
    page_area = page.rect.get_area()
    regions = [pymupdf.Rect(info["bbox"]) & page.rect for info in page.get_image_info()]
    large = [region for region in regions if region.get_area() >= page_area * _MIN_IMAGE_AREA_RATIO]
    texts = []
    for region in large[:_MAX_IMAGES_PER_PAGE]:
        text = ocr(page.get_pixmap(dpi=config.ocr_dpi, clip=region).tobytes("png")).strip()
        if _visible_length(text) >= config.ocr_min_chars:
            texts.append(text)
    return texts


def _render_page(page: pymupdf.Page, dpi: int) -> bytes:
    """Render a page to PNG bytes at the configured resolution."""
    return page.get_pixmap(dpi=dpi).tobytes("png")


def _visible_length(text: str) -> int:
    """Count characters that are not whitespace."""
    return len("".join(text.split()))
