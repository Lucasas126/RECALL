"""Profile pictures cannot be reached from an agent's own files, and an id cannot escape `.avatars`."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.core.sources.registry import SourceRegistry
from src.utils.avatars import AVATARS_DIR, USER_KEY, AvatarStore

PNG = b"\x89PNG\r\n\x1a\n-stand-in-"
WEBP = b"RIFF\0\0\0\0WEBP-stand-in-"


class AvatarStorePathTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name) / "data"
        self.data.mkdir()
        self.store = AvatarStore(self.data)

    def test_files_live_in_a_dot_folder_beside_the_agent_data_folders(self) -> None:
        self.assertEqual(self.store.path("user-a", "bot"), self.data / "user-a" / ".avatars" / "bot.png")
        self.assertEqual(self.store.path("user-a", USER_KEY), self.data / "user-a" / ".avatars" / "_user.png")

    def test_ids_that_are_not_plain_slugs_are_refused(self) -> None:
        for key in ("..", "../x", "a/b", "a\\b", ".hidden", "x.png", "bot\0", "", " bot", "bot ", "a" * 65, "é", "a:b"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.path("user-a", key)
            with self.subTest(op="save", key=key), self.assertRaises(ValueError):
                self.store.save("user-a", key, PNG)
            with self.subTest(op="delete", key=key), self.assertRaises(ValueError):
                self.store.delete("user-a", key)
        for user in ("..", "../user-b", "a/b", "", "user a"):
            with self.subTest(user=user), self.assertRaises(ValueError):
                self.store.path(user, "bot")

    def test_nothing_is_written_outside_the_avatars_folder(self) -> None:
        for key in ("../escape", "..", "a/../../escape"):
            with self.assertRaises(ValueError):
                self.store.save("user-a", key, PNG)
        self.store.save("user-a", "bot", PNG)
        written = {path.relative_to(self.data).as_posix() for path in self.data.rglob("*") if path.is_file()}
        self.assertEqual(written, {"user-a/.avatars/bot.png"})

    def test_a_symlinked_picture_is_not_served(self) -> None:
        secret = self.data / "secret.txt"
        secret.write_text("secret", encoding="utf-8")
        folder = self.data / "user-a" / AVATARS_DIR
        folder.mkdir(parents=True)
        (folder / "bot.png").symlink_to(secret)
        self.assertIsNone(self.store.get("user-a", "bot"))

    def test_a_webp_picture_replaces_a_png_and_back_and_is_read_and_moved(self) -> None:
        self.store.save("user-a", "bot", PNG)
        avatar = self.store.save("user-a", "bot", WEBP, "image/webp")
        self.assertEqual((avatar.path.name, avatar.media_type), ("bot.webp", "image/webp"))
        self.assertFalse((self.data / "user-a" / ".avatars" / "bot.png").exists())
        self.assertEqual(self.store.read(avatar), WEBP)
        self.assertTrue(self.store.move("user-a", "bot", "robot"))
        moved = self.store.get("user-a", "robot")
        self.assertEqual((moved.path.name, moved.media_type), ("robot.webp", "image/webp"))
        self.assertIsNone(self.store.get("user-a", "bot"))
        again = self.store.save("user-a", "robot", PNG)
        self.assertEqual((again.path.name, again.media_type), ("robot.png", "image/png"))
        self.assertEqual(sorted(p.name for p in (self.data / "user-a" / ".avatars").iterdir()), ["robot.png"])
        self.store.save("user-a", "robot", WEBP, "image/webp")
        self.store.delete("user-a", "robot")
        self.assertEqual(list((self.data / "user-a" / ".avatars").iterdir()), [])

    def test_a_symlinked_webp_is_not_served(self) -> None:
        secret = self.data / "secret.txt"
        secret.write_text("secret", encoding="utf-8")
        folder = self.data / "user-a" / AVATARS_DIR
        folder.mkdir(parents=True)
        (folder / "bot.webp").symlink_to(secret)
        self.assertIsNone(self.store.get("user-a", "bot"))

    def test_move_and_delete_stay_inside(self) -> None:
        self.store.save("user-a", "bot", PNG)
        self.assertTrue(self.store.move("user-a", "bot", "robot"))
        self.assertIsNone(self.store.get("user-a", "bot"))
        self.assertIsNotNone(self.store.get("user-a", "robot"))
        self.assertFalse(self.store.move("user-a", "none", "other"))
        with self.assertRaises(ValueError):
            self.store.move("user-a", "robot", "../robot")
        self.store.delete("user-a", "robot")
        self.assertIsNone(self.store.get("user-a", "robot"))


class AgentToolsCannotReachThePicturesTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name) / "data"
        self.data.mkdir()
        AvatarStore(self.data).save("user-a", "bot", PNG)
        AvatarStore(self.data).save("user-a", USER_KEY, PNG)
        self.registry = SourceRegistry.for_agent("user-a", "bot", data_dir=self.data)

    def test_the_agent_source_does_not_list_them(self) -> None:
        self.assertNotIn(".avatars", " ".join(self.registry.list("agent")))
        self.assertFalse([name for name in self.registry.get("agent").walk() if "avatars" in name or name.endswith(".png")])

    def test_the_agent_source_cannot_read_or_write_them_by_a_relative_path(self) -> None:
        for address in ("agent:../.avatars/bot.png", "agent:../.avatars/_user.png", "agent:../../user-a/.avatars/bot.png"):
            with self.subTest(address=address):
                with self.assertRaises(Exception):
                    self.registry.read(address)
                with self.assertRaises(Exception):
                    self.registry.write(address, "overwritten")
                with self.assertRaises(Exception):
                    self.registry.delete(address)
        with self.assertRaises(Exception):
            self.registry.list("agent", "..")
        self.assertEqual((self.data / "user-a" / ".avatars" / "bot.png").read_bytes(), PNG)

    def test_the_other_sources_cannot_either(self) -> None:
        for source in ("mind_palace", "conversations", "dream"):
            with self.subTest(source=source):
                with self.assertRaises(Exception):
                    self.registry.read(f"{source}:../../.avatars/bot.png")


if __name__ == "__main__":
    unittest.main()
