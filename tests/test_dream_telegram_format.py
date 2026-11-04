from __future__ import annotations

import unittest
from unittest.mock import Mock

import telegramify_markdown

from src.service.chat import ChatService
from src.service.dream import dream_notification, summary_block

_LAST = {"id": "20261103-040000-ab12", "status": "completed", "started_at": "2026-11-03T04:00:00+01:00"}


def _status(**over) -> dict:
    base = {
        "running": False, "last_started_at": None, "last_finished_at": None, "pending_chats": 3,
        "last_session": {**_LAST, "summary": "First paragraph.\n\n- point a\n- point b"},
        "schedule": {"enabled": True, "every_hours": 6, "daily_at": None, "next_run": "2026-11-04T12:00:00+01:00"},
    }
    base.update(over)
    return base


def _entities(text: str):
    plain, entities = telegramify_markdown.convert(text)
    return plain, [(e.type, plain_slice(plain, e)) for e in entities]


def plain_slice(plain: str, entity) -> str:
    raw = plain.encode("utf-16-le")
    return raw[entity.offset * 2:(entity.offset + entity.length) * 2].decode("utf-16-le")


class DreamStatusTelegramTests(unittest.TestCase):
    def render(self, status=None, language="en", channel="telegram") -> str:
        return ChatService._dream_status_text(language, status or _status(), channel, "Aria")

    def test_title_and_labels_are_bold_entities(self) -> None:
        plain, ents = _entities(self.render())
        bold = [text for kind, text in ents if kind == "bold"]
        self.assertIn("💤 Dream · Aria", bold)
        for label in ("✅ Last session:", "🗓 Schedule:", "📥 Pending chats:", "📝 Summary"):
            self.assertIn(label, bold)
        self.assertIn("completed · 2026-11-03 04:00", plain)
        self.assertIn("every 6 h · next run 2026-11-04 12:00", plain)
        self.assertIn("Pending chats: 3", plain)

    def test_summary_keeps_paragraphs_and_bullets_in_a_quote(self) -> None:
        plain, ents = _entities(self.render())
        self.assertIn("First paragraph.\n\n⦁ point a\n⦁ point b", plain)
        self.assertIn("blockquote", [kind for kind, _ in ents])

    def test_hint_uses_the_one_word_commands_only_for_telegram(self) -> None:
        self.assertIn("/dreamstart", self.render())
        self.assertNotIn("/dream start", self.render())
        running = _status(running=True, last_started_at="2026-11-04T10:00:00+01:00")
        text = self.render(running)
        self.assertIn("/dreamcancel", text)
        self.assertIn("🔄 Running:", text)
        web = self.render(channel="web")
        self.assertIn("Use /dream start to start a new session.", web)
        self.assertNotIn("/dreamstart", web)
        self.assertIn("Use /dream cancel to stop the running session.", self.render(running, channel="web"))

    def test_web_layout_is_unchanged(self) -> None:
        web = ChatService._dream_status_text("en", _status())
        self.assertIn("Last dream session: 2026-11-03 04:00 (completed).", web)
        self.assertIn("First paragraph. - point a - point b", web)
        self.assertNotIn("**", web)

    def test_empty_summary_omits_the_section(self) -> None:
        for last in ({**_LAST, "summary": ""}, {**_LAST}, None):
            text = self.render(_status(last_session=last))
            self.assertNotIn("Summary", text)
            self.assertNotIn(">", text)

    def test_status_labels_and_spanish(self) -> None:
        for status, label in (("budget_exhausted", "⚠️"), ("failed", "❌"), ("cancelled", "⏹"), ("timed_out", "⏱")):
            self.assertIn(label, self.render(_status(last_session={**_LAST, "status": status})))
        es = self.render(language="es")
        self.assertIn("**💤 Sueño · Aria**", es)
        self.assertIn("Usa /dreamstart para iniciar una sesión nueva.", es)
        self.assertIn("Chats pendientes", es)

    def test_agent_name_and_summary_specials_cannot_break_entities(self) -> None:
        text = ChatService._dream_status_text(
            "en", _status(last_session={**_LAST, "summary": "Unbalanced **bold and _under and `tick\n\n# heading\n\n```\nopen fence"}),
            "telegram", "my_*agent*",
        )
        plain, ents = _entities(text)
        self.assertIn("💤 Dream · my_*agent*", [t for k, t in ents if k == "bold"])
        for kind, _ in ents:
            self.assertIsInstance(kind, str)
        self.assertIn("open fence", plain)
        self.assertTrue(text.rstrip().endswith("_"))  # hint line still closes after the quote


class SummaryBlockTests(unittest.TestCase):
    def test_short_summary_is_quoted_whole(self) -> None:
        self.assertEqual(summary_block("en", "One.\n\nTwo.", 100), "> One.\n>\n> Two.")

    def test_truncates_at_paragraph_boundary_with_ellipsis(self) -> None:
        text = "A" * 60 + "\n\n" + "B" * 60 + "\n\n" + "C" * 60
        block = summary_block("en", text, 150)
        self.assertIn("> " + "B" * 60, block)
        self.assertNotIn("C", block)
        self.assertTrue(block.endswith("… (full summary in the web UI)"))

    def test_truncates_at_sentence_boundary(self) -> None:
        text = "First sentence here. Second sentence goes on and on and on and on. Third sentence."
        block = summary_block("en", text, 50)
        self.assertIn("> First sentence here.", block)
        self.assertNotIn("Second", block)
        spanish = summary_block("es", text, 50)
        self.assertTrue(spanish.endswith("… (resumen completo en la web)"))

    def test_empty_and_open_fence(self) -> None:
        self.assertEqual(summary_block("en", "  \n ", 100), "")
        self.assertEqual(summary_block("en", None, 100), "")
        block = summary_block("en", "```\n" + "x\n" * 100, 40)
        self.assertEqual(sum(1 for l in block.splitlines() if l.startswith("> ```")), 2)


class DreamNotificationMarkdownTests(unittest.TestCase):
    def test_title_summary_and_location(self) -> None:
        summary = {**_LAST, "summary": "Reviewed 3 chats.\n\n- updated SOUL"}
        link = "http://recall.example/#agents/aria/dreams/x_y"
        plain, ents = _entities(dream_notification("en", "Aria", summary, link))
        self.assertEqual(plain.splitlines()[0], "✅ Dream session completed for Aria.")
        self.assertIn("✅ Dream session completed for Aria.", [t for k, t in ents if k == "bold"])
        self.assertIn("Reviewed 3 chats.\n\n⦁ updated SOUL", plain)
        self.assertIn("blockquote", [k for k, _ in ents])
        self.assertIn("Agents → Aria → Dream sessions → 2026-11-03 04:00.", plain)
        self.assertIn(("text_link", link), [(k, t) for k, t in ents])

    def test_no_summary_no_quote_and_long_summary_is_shortened(self) -> None:
        plain, ents = _entities(dream_notification("en", "Aria", {**_LAST, "summary": ""}, None))
        self.assertNotIn("blockquote", [k for k, _ in ents])
        long_summary = {**_LAST, "summary": ("word " * 40 + "\n\n") * 10}
        text = dream_notification("en", "Aria", long_summary, None)
        self.assertLess(len(text), 900)
        self.assertIn("… (full summary in the web UI)", text)

    def test_other_outcomes_have_their_emoji(self) -> None:
        for status, emoji in (("failed", "❌"), ("budget_exhausted", "⚠️"), ("cancelled", "⏹"), ("timed_out", "⏱")):
            text = dream_notification("en", "Aria", {**_LAST, "status": status}, None)
            self.assertTrue(text.startswith(f"**{emoji} "), text)


if __name__ == "__main__":
    unittest.main()
