from __future__ import annotations

import base64
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from src.config.models import PdfConfig
from src.service.chat import ChatAttachment, ChatService
from src.utils import pdf
from src.utils.i18n import InputError, message
from src.utils.pdf import PdfPage, PdfText, extract_pdf_text, run_tesseract, tesseract_command

_CONFIG = PdfConfig()
_DIGITAL = "This page has selectable text that is long enough to skip OCR."


def _pdf_bytes(*pages: str | None) -> bytes:
    """Build a PDF with one page per entry; None leaves the page without a text layer."""
    document = pymupdf.open()
    for text in pages:
        page = document.new_page()
        if text is not None:
            page.insert_text((72, 72), text)
    data = document.tobytes()
    document.close()
    return data


def _png_image() -> bytes:
    """A small solid PNG used as an embedded image."""
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
    pixmap.set_rect(pixmap.irect, (200, 200, 200))
    return pixmap.tobytes("png")


def _fake_ocr(calls: list[bytes]):
    def recognize(png: bytes) -> str:
        calls.append(png)
        return "recognized scanned text from the page"
    return recognize


class PdfExtractionTests(unittest.TestCase):
    def test_digital_pages_skip_ocr(self) -> None:
        calls: list[bytes] = []
        result = extract_pdf_text(_pdf_bytes(_DIGITAL, _DIGITAL), "doc.pdf", _CONFIG, _fake_ocr(calls))
        self.assertEqual(calls, [])
        self.assertFalse(any(page.ocr for page in result.pages))
        self.assertIn(_DIGITAL, result.pages[0].text)

    def test_scanned_page_is_recognized_with_ocr(self) -> None:
        calls: list[bytes] = []
        result = extract_pdf_text(_pdf_bytes(None), "scan.pdf", _CONFIG, _fake_ocr(calls))
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith(b"\x89PNG"))
        self.assertEqual(result.pages, [PdfPage(1, "recognized scanned text from the page", ocr=True)])

    def test_mixed_document_runs_ocr_only_on_sparse_pages_and_keeps_order(self) -> None:
        calls: list[bytes] = []
        result = extract_pdf_text(
            _pdf_bytes(_DIGITAL, None, _DIGITAL), "mixed.pdf", _CONFIG, _fake_ocr(calls),
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual([page.number for page in result.pages], [1, 2, 3])
        self.assertEqual([page.ocr for page in result.pages], [False, True, False])

    def test_large_image_on_a_text_page_is_ocr_read_and_small_logo_is_skipped(self) -> None:
        document = pymupdf.open()
        page = document.new_page(width=600, height=800)
        page.insert_text((72, 72), _DIGITAL)
        image = _png_image()
        page.insert_image(pymupdf.Rect(0, 200, 600, 700), stream=image)  # covers most of the page
        page.insert_image(pymupdf.Rect(10, 10, 30, 30), stream=image)  # logo-sized
        data = document.tobytes()
        document.close()
        calls: list[bytes] = []
        result = extract_pdf_text(data, "card.pdf", _CONFIG, _fake_ocr(calls))
        self.assertEqual(len(calls), 1)
        self.assertTrue(result.pages[0].ocr)
        self.assertIn(_DIGITAL, result.pages[0].text)
        self.assertIn("recognized scanned text from the page", result.pages[0].text)

    def test_text_page_without_large_images_skips_ocr(self) -> None:
        document = pymupdf.open()
        page = document.new_page(width=600, height=800)
        page.insert_text((72, 72), _DIGITAL)
        page.insert_image(pymupdf.Rect(10, 10, 30, 30), stream=_png_image())
        data = document.tobytes()
        document.close()
        calls: list[bytes] = []
        result = extract_pdf_text(data, "logo.pdf", _CONFIG, _fake_ocr(calls))
        self.assertEqual(calls, [])
        self.assertFalse(result.pages[0].ocr)

    def test_render_marks_each_page_for_citation(self) -> None:
        text = PdfText([PdfPage(1, "first", ocr=False), PdfPage(2, "second", ocr=True)])
        self.assertEqual(text.render(), "[Page 1]\nfirst\n\n[Page 2, OCR]\nsecond")

    def test_invalid_pdf_is_rejected(self) -> None:
        with self.assertRaises(InputError) as caught:
            extract_pdf_text(b"not a pdf at all", "bad.pdf", _CONFIG, _fake_ocr([]))
        self.assertEqual(caught.exception.template, "{name} is not a valid PDF file.")

    def test_empty_bytes_are_rejected(self) -> None:
        with self.assertRaises(InputError):
            extract_pdf_text(b"", "empty.pdf", _CONFIG, _fake_ocr([]))

    def test_password_protected_pdf_is_rejected(self) -> None:
        document = pymupdf.open()
        document.new_page().insert_text((72, 72), _DIGITAL)
        data = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret")
        document.close()
        with self.assertRaises(InputError) as caught:
            extract_pdf_text(data, "locked.pdf", _CONFIG, _fake_ocr([]))
        self.assertIn("password-protected", caught.exception.template)

    def test_page_limit_is_enforced_before_ocr(self) -> None:
        calls: list[bytes] = []
        config = PdfConfig(max_pages=2)
        with self.assertRaises(InputError) as caught:
            extract_pdf_text(_pdf_bytes(None, None, None), "long.pdf", config, _fake_ocr(calls))
        self.assertEqual(calls, [])
        self.assertEqual(caught.exception.values, {"name": "long.pdf", "pages": 3, "limit": 2})

    def test_default_limit_is_thirty_pages(self) -> None:
        self.assertEqual(PdfConfig().max_pages, 30)

    def test_document_without_any_text_is_rejected(self) -> None:
        with self.assertRaises(InputError) as caught:
            extract_pdf_text(_pdf_bytes(None), "blank.pdf", _CONFIG, lambda png: "")
        self.assertEqual(caught.exception.template, "No text could be read from {name}.")

    def test_spanish_error_messages_are_translated(self) -> None:
        with self.assertRaises(InputError) as caught:
            extract_pdf_text(b"", "vacio.pdf", _CONFIG, _fake_ocr([]))
        self.assertEqual(
            message("es", caught.exception.template, **caught.exception.values),
            "vacio.pdf no es un archivo PDF válido.",
        )


class ChatPdfTests(unittest.TestCase):
    def _attachment(self, data: bytes, name: str = "doc.pdf") -> dict:
        encoded = base64.b64encode(data).decode("ascii")
        return ChatAttachment(
            modality="document", name=name, mime_type="application/pdf", format="pdf",
            data_url=f"data:application/pdf;base64,{encoded}",
        ).model_dump()

    def test_pdf_becomes_text_and_no_attachment_reaches_the_provider(self) -> None:
        attachment = self._attachment(_pdf_bytes(_DIGITAL))
        text, remaining = ChatService._extract_documents("Summarize it", [attachment])
        self.assertEqual(remaining, [])
        self.assertIn("Summarize it", text)
        self.assertIn("Attached document: doc.pdf", text)
        self.assertIn("[Page 1]", text)
        self.assertIn(_DIGITAL, text)

    def test_other_attachments_are_kept_for_the_provider(self) -> None:
        image = {"modality": "image", "name": "p.png", "mime_type": "image/png", "format": "png",
                 "data_url": "data:image/png;base64," + "a" * 32}
        text, remaining = ChatService._extract_documents("", [self._attachment(_pdf_bytes(_DIGITAL)), image])
        self.assertEqual(remaining, [image])
        self.assertTrue(text.startswith("Attached document: doc.pdf"))

    def test_invalid_base64_is_rejected(self) -> None:
        attachment = self._attachment(_pdf_bytes(_DIGITAL))
        attachment["data_url"] = "data:application/pdf;base64,@@@@"
        with self.assertRaises(InputError):
            ChatService._extract_documents("", [attachment])


class TesseractTests(unittest.TestCase):
    def test_missing_executable_gives_actionable_error(self) -> None:
        missing = Path("definitely-missing-tesseract.exe")
        with patch.object(pdf.shutil, "which", return_value=None), \
                patch.object(pdf, "_DEFAULT_WINDOWS_TESSERACT", missing):
            with self.assertRaises(InputError) as caught:
                tesseract_command(_CONFIG)
        self.assertIn("Tesseract is needed", caught.exception.template)
        self.assertIn("pdf.tesseract_cmd", caught.exception.template)

    def test_missing_configured_executable_is_reported_with_its_path(self) -> None:
        config = PdfConfig(tesseract_cmd="no-such-dir/tesseract")
        with self.assertRaises(InputError) as caught:
            tesseract_command(config)
        self.assertEqual(caught.exception.values, {"path": "no-such-dir/tesseract"})

    def test_missing_executable_fails_only_when_ocr_is_needed(self) -> None:
        digital_only = _pdf_bytes(_DIGITAL)
        with patch.object(pdf.shutil, "which", return_value=None), \
                patch.object(pdf, "_DEFAULT_WINDOWS_TESSERACT", Path("missing.exe")):
            result = extract_pdf_text(digital_only, "ok.pdf", _CONFIG,
                                      lambda png: run_tesseract(png, _CONFIG))
        self.assertFalse(result.pages[0].ocr)

    @unittest.skipUnless(
        shutil.which("tesseract") or Path(pdf._DEFAULT_WINDOWS_TESSERACT).is_file(),
        "Tesseract executable is not installed",
    )
    def test_tesseract_recognizes_a_rendered_page(self) -> None:
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 120), "RECALL OCR TEST", fontsize=36)
        png = page.get_pixmap(dpi=300).tobytes("png")
        document.close()
        self.assertIn("RECALL", run_tesseract(png, _CONFIG).upper())


if __name__ == "__main__":
    unittest.main()
