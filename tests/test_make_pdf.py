from __future__ import annotations

import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pymupdf
from PIL import Image

from src.agent.tools import build_tools, chat_folder_options
from src.agent.toolsets.pdf import slugify
from src.config.levels import Level
from src.config.models import FolderConfig
from src.core.sources.registry import SourceRegistry
from src.utils.pdf_build import SpecError, build_pdf

ELEMENTS = [
    {"type": "heading", "text": "Sección 1: ¿Qué tal, niño?", "level": 1},
    {"type": "paragraph", "text": "Texto con **negrita**, *cursiva* y <etiquetas> & símbolos.\nSegunda línea."},
    {"type": "bullets", "items": ["uno", "dos"]},
    {"type": "numbered", "items": ["primero", "segundo"]},
    {"type": "table", "rows": [["Nombre", "Valor"], ["Año", "2026"], ["Ñandú", "x"]]},
    {"type": "pagebreak"},
    {"type": "paragraph", "text": "Página dos"},
]


def page_text(data: bytes) -> list[str]:
    with pymupdf.open(stream=data, filetype="pdf") as document:
        return [page.get_text() for page in document]


class BuildPdfTests(unittest.TestCase):
    def test_builds_pages_with_accents_and_metadata(self) -> None:
        built = build_pdf("Informe de prueba", ELEMENTS, author="Ana")
        self.assertTrue(built.data.startswith(b"%PDF"))
        self.assertEqual(built.pages, 2)
        first, second = page_text(built.data)
        for word in ("Sección 1", "¿Qué tal, niño?", "negrita", "<etiquetas> & símbolos", "Ñandú", "Informe de prueba"):
            self.assertIn(word, first)
        self.assertIn("Página dos", second)
        with pymupdf.open(stream=built.data, filetype="pdf") as document:
            self.assertEqual(document.metadata["title"], "Informe de prueba")
            self.assertEqual(document.metadata["author"], "Ana")
        self.assertEqual(built.warnings, [])

    def test_page_numbers_can_be_turned_off_and_letter_size_is_used(self) -> None:
        with_numbers = page_text(build_pdf("T", [{"type": "paragraph", "text": "x"}]).data)[0]
        without = page_text(build_pdf("T", [{"type": "paragraph", "text": "x"}], page_numbers=False).data)[0]
        self.assertIn("1", with_numbers.split())
        self.assertNotIn("1", without.split())
        built = build_pdf("T", [{"type": "paragraph", "text": "x"}], page_size="letter")
        with pymupdf.open(stream=built.data, filetype="pdf") as document:
            self.assertEqual((round(document[0].rect.width), round(document[0].rect.height)), (612, 792))

    def test_long_tables_repeat_the_header_and_long_text_paginates(self) -> None:
        rows = [["Col A", "Col B"]] + [[f"fila {n}", "dato"] for n in range(120)]
        built = build_pdf("T", [{"type": "table", "rows": rows}])
        self.assertGreater(built.pages, 1)
        self.assertTrue(all("Col A" in text for text in page_text(built.data)))

    def test_characters_outside_the_standard_fonts_are_reported(self) -> None:
        built = build_pdf("T", [{"type": "paragraph", "text": "mayor ≥ menor → ok □"}])
        self.assertEqual(len(built.warnings), 1)
        for char in "≥→□":
            self.assertIn(char, built.warnings[0])
        self.assertEqual(build_pdf("T", [{"type": "paragraph", "text": "café — «ok» €"}]).warnings, [])

    def test_bad_specs_name_the_element_and_the_fix(self) -> None:
        cases = [
            (dict(title="", elements=ELEMENTS), "'title'"),
            (dict(title="T", elements=[]), "'elements'"),
            (dict(title="T", elements=[{"type": "chart"}]), "element 1: unknown type 'chart'"),
            (dict(title="T", elements=[{"type": "paragraph"}]), "element 1 (paragraph): 'text'"),
            (dict(title="T", elements=[{"type": "heading", "text": "x", "level": 9}]), "'level'"),
            (dict(title="T", elements=[{"type": "table", "rows": []}]), "'rows'"),
            (dict(title="T", elements=[{"type": "bullets", "items": []}]), "'items'"),
            (dict(title="T", elements=["text"]), "element 1: must be an object"),
            (dict(title="T", elements=ELEMENTS, page_size="a3"), "'page_size'"),
        ]
        for kwargs, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(SpecError) as caught:
                build_pdf(**kwargs)
            self.assertIn(expected, str(caught.exception))

    def test_images_come_from_the_loader_and_errors_name_the_element(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 20), "red").save(buffer, "PNG")
        element = {"type": "image", "path": "notes:a.png", "width_percent": 50}
        built = build_pdf("T", [element], load_image=lambda address: buffer.getvalue())
        with pymupdf.open(stream=built.data, filetype="pdf") as document:
            self.assertEqual(len(document[0].get_images()), 1)
        with self.assertRaises(SpecError) as caught:
            build_pdf("T", [element], load_image=lambda address: b"not an image")
        self.assertIn("element 1 (image)", str(caught.exception))
        with self.assertRaises(SpecError) as caught:
            build_pdf("T", [element])
        self.assertIn("cannot read image", str(caught.exception))

    def test_slug(self) -> None:
        self.assertEqual(slugify("Guía de Entrenamiento: ¡Fuerza!"), "guia-de-entrenamiento-fuerza")
        self.assertEqual(slugify("???"), "document")


class MakePdfToolTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.notes = self.root / "notes"
        self.notes.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.folders = [FolderConfig(name="notes", path=str(self.notes)), FolderConfig(name="work", path=str(self.work), read_only=True)]
        groups = ["memory_read", "memory_edit", "folders_edit"]
        options = chat_folder_options(SimpleNamespace(folders=self.folders, tool_groups=groups, tools=True, recall_knowledge=False))
        self.sources = SourceRegistry.for_agent("u", "a", data_dir=self.root / "data", **options)
        self.written: list[str] = []
        host = SimpleNamespace(sources=self.sources, config=SimpleNamespace(level=Level.ADMIN), index=None,
                               note_written=self.written.append)
        self.tools = build_tools(host, None, groups)[1]
        self.palace = self.root / "data" / "u" / "a" / "mind_palace"

    def test_default_location_is_the_palace_documents_folder(self) -> None:
        result = self.tools["make_pdf"](title="Plan de Entrenamiento", elements=ELEMENTS[:2])
        self.assertIn("saved mind_palace:documents/plan-de-entrenamiento.pdf (1 pages", result)
        self.assertTrue((self.palace / "documents" / "plan-de-entrenamiento.pdf").read_bytes().startswith(b"%PDF"))
        self.assertIn("mind_palace:documents/plan-de-entrenamiento.pdf", self.written)

    def test_a_taken_name_gets_a_number_unless_overwrite(self) -> None:
        first = self.tools["make_pdf"](title="A", elements=ELEMENTS[:2])
        second = self.tools["make_pdf"](title="A", elements=ELEMENTS[:2])
        self.assertIn("documents/a.pdf", first)
        self.assertIn("documents/a-2.pdf", second)
        self.assertIn("documents/a.pdf", self.tools["make_pdf"](title="A", elements=ELEMENTS[:2], overwrite=True))
        self.assertFalse((self.palace / "documents" / "a-3.pdf").exists())

    def test_the_agent_chooses_the_path_in_any_writable_source(self) -> None:
        result = self.tools["make_pdf"](title="B", elements=ELEMENTS[:2], path="notes:Reports/b.pdf")
        self.assertIn("saved notes:Reports/b.pdf", result)
        self.assertTrue((self.notes / "Reports" / "b.pdf").exists())
        self.assertIn("read-only", self.tools["make_pdf"](title="B", elements=ELEMENTS[:2], path="work:b.pdf"))
        self.assertFalse((self.work / "b.pdf").exists())
        self.assertIn(".pdf", self.tools["make_pdf"](title="B", elements=ELEMENTS[:2], path="notes:b.txt"))
        self.assertIn("path escapes", self.tools["make_pdf"](title="B", elements=ELEMENTS[:2], path="notes:../b.pdf"))
        self.assertIn("unknown source", self.tools["make_pdf"](title="B", elements=ELEMENTS[:2], path="nowhere:b.pdf").lower()
                      .replace("'nowhere'", "unknown source"))

    def test_spec_errors_are_returned_not_raised_and_nothing_is_written(self) -> None:
        result = self.tools["make_pdf"](title="C", elements=[{"type": "chart"}])
        self.assertTrue(result.startswith("error: element 1: unknown type"))
        self.assertFalse((self.palace / "documents").exists())

    def test_warnings_are_reported(self) -> None:
        result = self.tools["make_pdf"](title="D", elements=[{"type": "paragraph", "text": "a → b"}])
        self.assertIn("Warning:", result)
        self.assertIn("→", result)

    def test_a_saved_pdf_can_be_read_back_as_page_text_and_moved_intact(self) -> None:
        self.tools["make_pdf"](title="E", elements=ELEMENTS)
        text = self.tools["read"](memory_path="mind_palace:documents/e.pdf")
        self.assertIn("[Page 1]", text)
        self.assertIn("Ñandú", text)
        self.assertIn("[Page 2]", text)
        original = (self.palace / "documents" / "e.pdf").read_bytes()
        self.assertIn("moved", self.tools["move"](memory_path="mind_palace:documents/e.pdf", new_path="notes:Archive/e.pdf"))
        self.assertEqual((self.notes / "Archive" / "e.pdf").read_bytes(), original)
        self.assertFalse((self.palace / "documents" / "e.pdf").exists())
        self.assertIn("Ñandú", self.tools["folder_read"](path="notes:Archive/e.pdf"))
        self.assertIn("moved", self.tools["folder_move"](path="notes:Archive/e.pdf", new_path="notes:e.pdf"))
        self.assertEqual((self.notes / "e.pdf").read_bytes(), original)

    def test_a_binary_file_moves_byte_for_byte(self) -> None:
        data = bytes(range(256)) * 4
        (self.palace / "blob.bin").parent.mkdir(parents=True, exist_ok=True)
        (self.palace / "blob.bin").write_bytes(data)
        self.tools["move"](memory_path="mind_palace:blob.bin", new_path="mind_palace:sub/blob.bin")
        self.assertEqual((self.palace / "sub" / "blob.bin").read_bytes(), data)

    def test_images_can_be_taken_from_a_source(self) -> None:
        Image.new("RGB", (30, 30), "blue").save(self.notes / "pic.png")
        elements = [{"type": "image", "path": "notes:pic.png"}]
        self.assertIn("saved", self.tools["make_pdf"](title="F", elements=elements))
        self.assertIn("cannot read image", self.tools["make_pdf"](title="G", elements=[{"type": "image", "path": "notes:missing.png"}]))
        self.assertIn("cannot read image", self.tools["make_pdf"](title="G", elements=[{"type": "image", "path": "notes:../x.png"}]))

    def test_the_tool_belongs_to_memory_edit_only(self) -> None:
        host = SimpleNamespace(sources=self.sources, config=SimpleNamespace(level=Level.ADMIN), index=None)
        self.assertNotIn("make_pdf", build_tools(host, None, ["memory_read"])[1])
        self.assertIn("make_pdf", build_tools(host, None, ["memory_edit"])[1])


if __name__ == "__main__":
    unittest.main()
