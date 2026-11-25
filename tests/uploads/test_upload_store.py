from __future__ import annotations

import os
import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from src.utils.uploads import UploadStore, sanitise_name


class SanitiseTests(unittest.TestCase):
    def test_strips_paths_dots_and_control_characters(self) -> None:
        self.assertEqual(sanitise_name("../../etc/passwd"), "passwd")
        self.assertEqual(sanitise_name("..\\..\\win.ini"), "win.ini")
        self.assertEqual(sanitise_name(".hidden"), "hidden")
        self.assertEqual(sanitise_name("a\x00b\nc.pdf"), "a_b_c.pdf")
        self.assertEqual(sanitise_name(""), "file")
        self.assertEqual(sanitise_name("..."), "file")

    def test_bounds_the_length_and_keeps_the_extension(self) -> None:
        name = sanitise_name("x" * 500 + ".pdf")
        self.assertLessEqual(len(name), 80)
        self.assertTrue(name.endswith(".pdf"))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = UploadStore(self.root)
        self.now = datetime(2026, 11, 25, 10, 0, 0)

    def test_saves_the_original_bytes_under_the_chat(self) -> None:
        path = self.store.save("user-a", "chat1", "Report.pdf", b"%PDF-original", now=self.now)
        self.assertEqual(path, self.root / "user-a" / ".uploads" / "chat1" / "20261125-100000-Report.pdf")
        self.assertEqual(path.read_bytes(), b"%PDF-original")

    def test_collision_gets_a_new_name_and_keeps_both(self) -> None:
        first = self.store.save("user-a", "chat1", "a.png", b"1", now=self.now)
        second = self.store.save("user-a", "chat1", "a.png", b"2", now=self.now)
        self.assertNotEqual(first, second)
        self.assertEqual((first.read_bytes(), second.read_bytes()), (b"1", b"2"))
        self.assertEqual(second.suffix, ".png")

    def test_traversal_in_name_stays_inside(self) -> None:
        path = self.store.save("user-a", "chat1", "../../../x.txt", b"x", now=self.now)
        self.assertEqual(path.parent, self.root / "user-a" / ".uploads" / "chat1")

    def test_invalid_chat_or_user_is_refused(self) -> None:
        for chat in ("..", "a/b", "", "a" * 65):
            with self.assertRaises(ValueError):
                self.store.save("user-a", chat, "f", b"x")
        for user in ("..", "a/b", ""):
            with self.assertRaises(ValueError):
                self.store.save(user, "chat1", "f", b"x")

    @unittest.skipUnless(hasattr(os, "symlink"), "needs symlinks")
    def test_symlinked_folder_is_refused(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "user-a").mkdir()
        os.symlink(outside, self.root / "user-a" / ".uploads")
        with self.assertRaises(RuntimeError):
            self.store.save("user-a", "chat1", "f.txt", b"x")
        self.assertEqual(list(outside.iterdir()), [])

    def test_expire_removes_only_old_files_of_every_user(self) -> None:
        old = self.store.save("user-a", "chat1", "old.mp4", b"o")
        new = self.store.save("user-b", "chat2", "new.mp3", b"n")
        fresh = self.store.save("user-a", "chat1", "fresh.pdf", b"f")
        base = old.stat().st_mtime
        os.utime(old, (base - 40 * 86400,) * 2)
        os.utime(new, (base - 31 * 86400,) * 2)
        self.assertEqual(self.store.expire(30), 2)
        self.assertFalse(old.exists())
        self.assertFalse(new.exists())
        self.assertFalse(new.parent.exists())  # an emptied chat folder goes too
        self.assertTrue(fresh.exists())

    def test_expire_without_data_dir(self) -> None:
        self.assertEqual(UploadStore(self.root / "missing").expire(30), 0)


class ServiceCleanupTests(unittest.TestCase):
    def test_cleanup_uses_the_configured_days_and_never_raises(self) -> None:
        from unittest.mock import Mock, patch

        from src.service.chat import ChatService

        service = ChatService.__new__(ChatService)
        service._uploads = Mock()
        service._uploads.expire.return_value = 3
        with patch("src.service.chat.UPLOADS", Mock(retention_days=7)):
            self.assertEqual(service.cleanup_uploads(), 3)
            service._uploads.expire.assert_called_once_with(7)
            service._uploads.expire.side_effect = OSError("boom")
            self.assertEqual(service.cleanup_uploads(), 0)

    def test_default_retention_is_thirty_days(self) -> None:
        from src.config.settings import UPLOADS

        self.assertEqual(UPLOADS.retention_days, 30)
