from __future__ import annotations

import base64
import io
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace


import pymupdf  # noqa: E402
from PIL import Image  # noqa: E402
from tests.support.terminal_helpers import FakeSandbox, make_session  # noqa: E402

from src.agent import agent as agent_module  # noqa: E402
from src.agent.agent import Agent  # noqa: E402
from src.agent.toolsets.images import build_image_tools  # noqa: E402
from src.core.sources.base import Source  # noqa: E402
from src.core.sources.registry import SourceRegistry  # noqa: E402
from src.utils import image_prep  # noqa: E402
from src.utils.pdf_build import build_pdf  # noqa: E402


def png(width: int, height: int, color="red", mode="RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


def decode(url: str) -> Image.Image:
    assert url.startswith("data:image/jpeg;base64,")
    return Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))


def sample_pdf(pages: int = 3) -> bytes:
    elements = []
    for number in range(1, pages + 1):
        elements += [{"type": "heading", "text": f"Página {number}"}, {"type": "paragraph", "text": "texto " * 80}]
        if number < pages:
            elements.append({"type": "pagebreak"})
    return build_pdf("Prueba", elements).data


class ImagePrepTests(unittest.TestCase):
    def test_a_large_image_is_downscaled_and_small_ones_are_left(self) -> None:
        url, what, size = image_prep.prepare_image(png(3000, 1500), 1024)
        self.assertEqual(decode(url).size, (1024, 512))
        self.assertIn("3000x1500", what)
        self.assertLess(size, 60_000)
        self.assertEqual(decode(image_prep.prepare_image(png(300, 200))[0]).size, (300, 200))

    def test_the_edge_is_clamped(self) -> None:
        self.assertEqual(image_prep.clamp_edge(10), image_prep.MIN_EDGE)
        self.assertEqual(image_prep.clamp_edge(99999), image_prep.MAX_EDGE)

    def test_transparency_becomes_white_not_black(self) -> None:
        url, _, _ = image_prep.prepare_image(png(40, 40, (0, 0, 0, 0), "RGBA"))
        self.assertTrue(all(channel > 240 for channel in decode(url).getpixel((20, 20))))

    def test_garbage_and_huge_images_are_refused(self) -> None:
        with self.assertRaises(image_prep.ImageError):
            image_prep.prepare_image(b"not an image")
        huge = io.BytesIO()
        Image.new("1", (8000, 8000)).save(huge, "PNG")
        with self.assertRaises(image_prep.ImageError) as caught:
            image_prep.prepare_image(huge.getvalue())
        self.assertIn("megapixels", str(caught.exception))

    def test_pages_are_parsed_and_validated(self) -> None:
        self.assertEqual(image_prep.parse_pages("", 5), [1])
        self.assertEqual(image_prep.parse_pages("1-3,5,2", 5), [1, 2, 3, 5])
        for bad in ("0", "6", "3-1", "a", "1,,x", "1-9"):
            with self.subTest(bad=bad), self.assertRaises(image_prep.ImageError):
                image_prep.parse_pages(bad, 5)

    def test_one_page_is_rendered_alone_and_several_make_one_sheet(self) -> None:
        data = sample_pdf(3)
        url, what, _ = image_prep.render_pdf(data, "2", 800)
        page = decode(url)
        self.assertEqual(max(page.size), 800)
        self.assertIn("page 2 of 3", what)
        sheet_url, sheet_what, _ = image_prep.render_pdf(data, "1-3")
        sheet = decode(sheet_url)
        self.assertGreater(sheet.width, sheet.height * 1.0)  # three columns
        self.assertIn("contact sheet of pages 1, 2, 3 of 3", sheet_what)
        with self.assertRaises(image_prep.ImageError):
            image_prep.render_pdf(data, "1-9")
        with self.assertRaises(image_prep.ImageError):
            image_prep.render_pdf(sample_pdf(8), "1-7")

    def test_a_pdf_is_recognised_by_its_header_and_a_broken_one_is_refused(self) -> None:
        self.assertTrue(image_prep.is_pdf(sample_pdf(1)))
        self.assertFalse(image_prep.is_pdf(png(2, 2)))
        with self.assertRaises(image_prep.ImageError):
            image_prep.render_pdf(b"%PDF-1.4 broken")

    def test_an_encrypted_pdf_is_refused(self) -> None:
        document = pymupdf.open()
        document.new_page()
        locked = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
        with self.assertRaises(image_prep.ImageError) as caught:
            image_prep.render_pdf(locked)
        self.assertIn("encrypted", str(caught.exception))


class FakeProvider:
    def __init__(self, modalities: set[str]) -> None:
        self.modalities, self.calls = modalities, 0

    def supported_modalities(self, model: str) -> set[str]:
        self.calls += 1
        return self.modalities


class ViewImageToolTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / "pic.png").write_bytes(png(2000, 1000))
        (self.root / "doc.pdf").write_bytes(sample_pdf(3))
        (self.root / "note.txt").write_text("hola", encoding="utf-8")
        self.box = FakeSandbox()
        self.box.raw["/w/out.pdf"] = sample_pdf(2)
        self.queued: list[tuple[str, str]] = []
        self.provider = FakeProvider({"text", "image"})
        self.agent = SimpleNamespace(
            sources=SourceRegistry({"notes": Source("notes", self.root)}),
            config=SimpleNamespace(models=SimpleNamespace(chat="m")), provider=self.provider,
            terminal_sessions={"box": make_session(self.box, "off")},
            queue_image=lambda label, url: self.queued.append((label, url)) or True,
        )
        self.view = build_image_tools(self.agent)[0]

    def test_an_image_of_a_source_is_queued_downscaled(self) -> None:
        result = self.view("notes:pic.png")
        self.assertTrue(result.startswith("showing pic.png: 2000x1000 px, shown at 1024x512"), result)
        self.assertEqual(len(self.queued), 1)
        self.assertEqual(self.queued[0][0], "notes:pic.png (2000x1000 px, shown at 1024x512)")
        self.assertEqual(decode(self.queued[0][1]).size, (1024, 512))

    def test_pdf_pages_and_a_contact_sheet(self) -> None:
        self.assertIn("page 1 of 3", self.view("notes:doc.pdf"))
        self.assertIn("page 3 of 3", self.view("notes:doc.pdf", pages="3"))
        self.assertIn("contact sheet", self.view("notes:doc.pdf", pages="1-3"))
        self.assertEqual(len(self.queued), 3)
        self.assertIn("outside this document", self.view("notes:doc.pdf", pages="7"))
        self.assertIn("only applies to a PDF", self.view("notes:pic.png", pages="2"))

    def test_a_file_on_a_backend_can_be_viewed(self) -> None:
        self.assertIn("showing out.pdf: page 1 of 2", self.view("box:/w/out.pdf"))
        self.assertEqual(len(self.queued), 1)

    def test_a_model_without_vision_gets_a_pointer_to_read_and_nothing_is_fetched(self) -> None:
        self.provider.modalities = {"text"}
        agent = SimpleNamespace(**{**vars(self.agent), "provider": self.provider})
        view = build_image_tools(agent)[0]
        self.assertIn("cannot see images", view("notes:pic.png"))
        self.assertIn("`read`", view("notes:pic.png"))
        self.assertEqual((self.queued, self.box.calls), ([], []))
        self.assertEqual(self.provider.calls, 1)  # asked once per model, not once per call

    def test_a_provider_error_means_no_vision(self) -> None:
        def broken(model):
            raise RuntimeError("catalog down")
        self.provider.supported_modalities = broken
        view = build_image_tools(SimpleNamespace(**vars(self.agent)))[0]
        self.assertIn("cannot see images", view("notes:pic.png"))

    def test_errors_are_plain_and_nothing_is_queued(self) -> None:
        for path, expected in (
            ("notes:missing.png", "is not a file"), ("notes:../x.png", "escapes"), ("nowhere:x", "unknown source or backend"),
            ("notes:note.txt", "not an image"), ("box:/w/missing.png", "no such file"),
        ):
            with self.subTest(path=path):
                result = self.view(path)
                self.assertTrue(result.startswith("error:"), result)
                self.assertIn(expected, result)
        self.assertEqual(self.queued, [])

    def test_the_per_message_limit_is_reported(self) -> None:
        self.agent.queue_image = lambda label, url: False
        view = build_image_tools(SimpleNamespace(**vars(self.agent)))[0]
        self.assertIn("most images", view("notes:pic.png"))

    def test_the_spec_names_only_what_the_agent_chooses(self) -> None:
        self.assertEqual(set(self.view.tool_spec["function"]["parameters"]["properties"]), {"path", "pages", "max_edge"})
        self.assertEqual(self.view.tool_spec["function"]["parameters"]["required"], ["path"])


class AgentQueueTests(unittest.TestCase):
    """The queue, the flush after the tool messages and the drop at the next user message (no model needed)."""

    def agent(self):
        host = Agent.__new__(Agent)
        host.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
        host._pending_images, host._tool_images_shown = [], 0
        return host

    def test_pictures_follow_the_tool_messages_as_one_user_message(self) -> None:
        host = self.agent()
        self.assertTrue(host.queue_image("a.png (1x1)", "data:image/jpeg;base64,AAAA"))
        self.assertTrue(host.queue_image("b.png (2x2)", "data:image/jpeg;base64,BBBB"))
        host.messages.append({"role": "tool", "tool_call_id": "1", "name": "view_image", "content": "showing a.png"})
        host._flush_tool_images()
        last = host.messages[-1]
        self.assertEqual(last["role"], "user")
        self.assertEqual([part["type"] for part in last["content"]], ["text", "image_url", "image_url"])
        self.assertIn("not a message from the user", last["content"][0]["text"])
        self.assertIn("a.png (1x1); b.png (2x2)", last["content"][0]["text"])
        self.assertEqual(host.messages[-2]["role"], "tool")  # the tool message comes first
        count = len(host.messages)
        host._flush_tool_images()  # nothing queued: nothing added
        self.assertEqual(len(host.messages), count)

    def test_the_limit_counts_pending_and_shown_pictures(self) -> None:
        host = self.agent()
        accepted = [host.queue_image(f"p{n}", "data:image/jpeg;base64,AA") for n in range(agent_module._MAX_TOOL_IMAGES + 2)]
        self.assertEqual(accepted.count(True), agent_module._MAX_TOOL_IMAGES)
        host._flush_tool_images()
        self.assertFalse(host.queue_image("more", "data:image/jpeg;base64,AA"))
        host._drop_tool_images()  # a new message of the user resets the count
        self.assertTrue(host.queue_image("again", "data:image/jpeg;base64,AA"))

    def test_the_next_user_message_drops_the_pictures_and_keeps_everything_else(self) -> None:
        host = self.agent()
        host.queue_image("a.png", "data:image/jpeg;base64,AAAA")
        host.messages.append({"role": "assistant", "content": "looking"})
        host._flush_tool_images()
        host.messages.append({"role": "assistant", "content": "the layout is fine"})
        before = [m for m in host.messages if not Agent._is_tool_images(m)]
        host._drop_tool_images()
        self.assertEqual(host.messages, before)
        self.assertFalse(any(Agent._is_tool_images(m) for m in host.messages))
        self.assertEqual(host._pending_images, [])

    def test_a_real_user_message_with_a_picture_is_never_dropped(self) -> None:
        host = self.agent()
        user = {"role": "user", "content": [{"type": "text", "text": "what is this?"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}
        host.messages.append(user)
        host._drop_tool_images()
        self.assertIn(user, host.messages)


class CompactionTests(unittest.TestCase):
    def test_compaction_removes_the_tool_pictures_before_counting_user_messages(self) -> None:
        host = Agent.__new__(Agent)
        host.config = SimpleNamespace(has_memory=True)
        host.compaction_due = lambda: True
        host._ensure_max_context = lambda: (_ for _ in ()).throw(StopIteration)  # stop right after the cleanup
        host._pending_images, host._tool_images_shown = [], 0
        host.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
        host.queue_image("a.png", "data:image/jpeg;base64,AA")
        host._flush_tool_images()
        self.assertTrue(any(Agent._is_tool_images(m) for m in host.messages))
        with self.assertRaises(StopIteration):
            host.compact()
        self.assertFalse(any(Agent._is_tool_images(m) for m in host.messages))


if __name__ == "__main__":
    unittest.main()
