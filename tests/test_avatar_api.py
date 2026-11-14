"""Profile pictures over HTTP: owner-only routes, headers, payloads, style, rename."""

from __future__ import annotations

import io
import json

import yaml
from admin_helpers import AdminFixture
from auth_helpers import TEST_PASSWORD_HASH, login
from fastapi.testclient import TestClient
from PIL import Image

from src.utils.i18n import messages
from src.web.app import create_app

USER_B = "id: user-b\nname: User B\nmax_level: operator\nprovider: {name: ollama}\nagents: [other]\npassword_hash: '@HASH@'\n"


def image_bytes(fmt: str = "PNG", size=(64, 48), colour=(10, 120, 200, 255)) -> bytes:
    image = Image.new("RGBA" if fmt != "JPEG" else "RGB", size, colour if fmt != "JPEG" else colour[:3])
    out = io.BytesIO()
    image.save(out, format=fmt)
    return out.getvalue()


def animated_gif() -> bytes:
    frames = [Image.new("RGB", (40, 30), colour) for colour in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
    return out.getvalue()


class AvatarFixture(AdminFixture):
    def setUp(self) -> None:
        super().setUp()
        (self.config_dir / "user-b" / "agents").mkdir(parents=True)
        (self.config_dir / "user-b" / "user.yaml").write_text(USER_B.replace("@HASH@", TEST_PASSWORD_HASH), encoding="utf-8")
        (self.config_dir / "user-b" / "agents" / "other.yaml").write_text("id: other\nname: Other\n", encoding="utf-8")
        self.anonymous = self.client_for(None)
        self.owner = self.client_for("user-a")
        self.stranger = self.client_for("user-b")

    def client_for(self, user: str | None) -> TestClient:
        client = TestClient(create_app(self.service))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        if user:
            login(client, user)
        return client

    def put(self, client: TestClient, path: str, data: bytes | None = None, content_type: str = "image/png"):
        return client.put(path, content=data if data is not None else image_bytes(), headers={"Content-Type": content_type})

    def agent_yaml(self, name: str = "bot") -> dict:
        return yaml.safe_load((self.config_dir / "user-a" / "agents" / f"{name}.yaml").read_text(encoding="utf-8"))


class AgentAvatarTests(AvatarFixture):
    def test_upload_returns_url_alpha_and_version_and_get_serves_the_png(self) -> None:
        response = self.put(self.owner, "/api/agents/bot/avatar", image_bytes("PNG", colour=(0, 0, 0, 0)))
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(set(body), {"url", "has_alpha", "version"})
        self.assertEqual((body["url"], body["has_alpha"]), ("/api/agents/bot/avatar", True))
        got = self.owner.get(body["url"])
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got.headers["content-type"], "image/png")
        self.assertEqual(got.headers["x-content-type-options"], "nosniff")
        self.assertEqual(got.headers["cache-control"], "private, max-age=0, must-revalidate")
        self.assertTrue(got.headers["etag"].startswith('"'))
        self.assertEqual(Image.open(io.BytesIO(got.content)).format, "PNG")
        self.assertTrue((self.data_dir / "user-a" / ".avatars" / "bot.png").is_file())

    def test_etag_gives_a_304_and_a_new_upload_changes_it(self) -> None:
        self.put(self.owner, "/api/agents/bot/avatar")
        first = self.owner.get("/api/agents/bot/avatar")
        again = self.owner.get("/api/agents/bot/avatar", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.headers["etag"], first.headers["etag"])
        self.put(self.owner, "/api/agents/bot/avatar", image_bytes("PNG", size=(30, 30), colour=(255, 0, 0, 255)))
        second = self.owner.get("/api/agents/bot/avatar", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(second.headers["etag"], first.headers["etag"])

    def test_jpeg_and_webp_are_accepted(self) -> None:
        self.assertEqual(self.put(self.owner, "/api/agents/bot/avatar", image_bytes("JPEG"), "image/jpeg").status_code, 200)
        self.assertEqual(self.put(self.owner, "/api/agents/bot/avatar", image_bytes("WEBP"), "image/webp").status_code, 200)

    def test_an_animated_gif_is_served_as_webp_and_replaces_a_png_and_back(self) -> None:
        folder = self.data_dir / "user-a" / ".avatars"
        self.put(self.owner, "/api/agents/bot/avatar")
        self.assertTrue((folder / "bot.png").is_file())
        response = self.put(self.owner, "/api/agents/bot/avatar", animated_gif(), "image/gif")
        self.assertEqual(response.status_code, 200, response.text)
        got = self.owner.get("/api/agents/bot/avatar")
        self.assertEqual(got.headers["content-type"], "image/webp")
        self.assertEqual(got.headers["x-content-type-options"], "nosniff")
        self.assertEqual(Image.open(io.BytesIO(got.content)).n_frames, 3)
        self.assertTrue((folder / "bot.webp").is_file())
        self.assertFalse((folder / "bot.png").exists())
        again = self.owner.get("/api/agents/bot/avatar", headers={"If-None-Match": got.headers["etag"]})
        self.assertEqual(again.status_code, 304)
        self.put(self.owner, "/api/agents/bot/avatar")
        self.assertEqual(self.owner.get("/api/agents/bot/avatar").headers["content-type"], "image/png")
        self.assertTrue((folder / "bot.png").is_file())
        self.assertFalse((folder / "bot.webp").exists())
        self.put(self.owner, "/api/me/avatar", animated_gif(), "image/gif")
        self.assertEqual(self.owner.get("/api/me/avatar").headers["content-type"], "image/webp")
        self.assertEqual(self.owner.delete("/api/agents/bot/avatar").status_code, 204)
        self.assertFalse((folder / "bot.png").exists())

    def test_no_picture_is_a_404_and_delete_removes_it(self) -> None:
        self.assertEqual(self.owner.get("/api/agents/bot/avatar").status_code, 404)
        self.put(self.owner, "/api/agents/bot/avatar")
        self.assertEqual(self.owner.delete("/api/agents/bot/avatar").status_code, 204)
        self.assertEqual(self.owner.get("/api/agents/bot/avatar").status_code, 404)
        self.assertFalse((self.data_dir / "user-a" / ".avatars" / "bot.png").exists())
        self.assertEqual(self.owner.delete("/api/agents/bot/avatar").status_code, 204)  # idempotent

    def test_error_codes_and_spanish_messages(self) -> None:
        wrong = self.put(self.owner, "/api/agents/bot/avatar", b"<svg/>", "image/svg+xml")
        self.assertEqual((wrong.status_code, wrong.json()["code"]), (415, "unsupported_image_type"))
        garbage = self.put(self.owner, "/api/agents/bot/avatar", b"not an image")
        self.assertEqual((garbage.status_code, garbage.json()["code"]), (400, "invalid_image"))
        svg_as_png = self.put(self.owner, "/api/agents/bot/avatar", b"<svg/>", "image/png")
        self.assertEqual((svg_as_png.status_code, svg_as_png.json()["code"]), (400, "invalid_image"))
        big = self.put(self.owner, "/api/agents/bot/avatar", b"\0" * (5 * 1024 * 1024 + 1))
        self.assertEqual((big.status_code, big.json()["code"]), (413, "image_too_large"))
        self.owner.patch("/api/preferences", json={"language": "es"})
        spanish = self.put(self.owner, "/api/agents/bot/avatar", b"x")
        self.assertEqual(spanish.json()["detail"], messages("es")["The file is not a valid PNG, JPEG, WEBP or GIF image."])
        self.assertEqual(spanish.json()["code"], "invalid_image")
        self.assertFalse((self.data_dir / "user-a" / ".avatars" / "bot.png").exists())

    def test_a_form_upload_is_not_accepted(self) -> None:
        response = self.owner.put("/api/agents/bot/avatar", files={"file": ("a.png", image_bytes(), "image/png")})
        self.assertEqual(response.status_code, 415)

    def test_anonymous_is_401(self) -> None:
        for call in (
            lambda: self.put(self.anonymous, "/api/agents/bot/avatar"),
            lambda: self.anonymous.get("/api/agents/bot/avatar"),
            lambda: self.anonymous.delete("/api/agents/bot/avatar"),
        ):
            self.assertEqual(call().status_code, 401)

    def test_another_user_can_neither_read_write_nor_remove_it(self) -> None:
        self.put(self.owner, "/api/agents/bot/avatar")
        before = (self.data_dir / "user-a" / ".avatars" / "bot.png").read_bytes()
        for response in (
            self.put(self.stranger, "/api/agents/bot/avatar"),
            self.stranger.get("/api/agents/bot/avatar"),
            self.stranger.delete("/api/agents/bot/avatar"),
        ):
            self.assertEqual(response.status_code, 404)
        self.assertEqual((self.data_dir / "user-a" / ".avatars" / "bot.png").read_bytes(), before)
        self.assertFalse((self.data_dir / "user-b" / ".avatars" / "bot.png").exists())
        # the stranger's own agent is theirs alone
        self.assertEqual(self.put(self.stranger, "/api/agents/other/avatar").status_code, 200)
        self.assertEqual(self.owner.get("/api/agents/other/avatar").status_code, 404)

    def test_an_agent_id_with_odd_characters_is_a_404_not_a_path(self) -> None:
        for agent_id in ("..%2F..%2Fx", "%2e%2e", "bot%00", "a b", ".avatars"):
            with self.subTest(agent_id=agent_id):
                self.assertEqual(self.put(self.owner, f"/api/agents/{agent_id}/avatar").status_code, 404)
        self.assertFalse(list((self.data_dir / "user-a").glob("**/*.png")))

    def test_an_agent_cannot_have_the_users_file_name(self) -> None:
        self.owner.post("/api/agents", json={"id": "_user", "name": "Odd"})
        self.assertEqual(self.put(self.owner, "/api/agents/_user/avatar").status_code, 400)
        self.assertFalse((self.data_dir / "user-a" / ".avatars" / "_user.png").exists())


class PayloadTests(AvatarFixture):
    def test_lists_and_details_carry_the_avatar_and_its_style(self) -> None:
        for body in (
            self.owner.get("/api/agents/bot").json(), self.owner.get("/api/agents").json()[0],
            self.owner.get("/api/me").json()["agents"][0],
        ):
            self.assertIsNone(body["avatar"])
        self.put(self.owner, "/api/agents/bot/avatar")
        version = self.owner.get("/api/agents/bot").json()["avatar"]["version"]
        detail = self.owner.get("/api/agents/bot").json()
        self.assertEqual(detail["avatar"], {"url": "/api/agents/bot/avatar", "version": version, "style": "circle"})
        self.assertEqual(self.owner.get("/api/agents").json()[0]["avatar"], detail["avatar"])
        self.assertEqual(self.owner.get("/api/me").json()["agents"][0]["avatar"], detail["avatar"])

    def test_avatar_style_is_saved_validated_and_kept_when_omitted(self) -> None:
        config = self.owner.get("/api/agents/bot").json()
        config.pop("avatar"), config.pop("level")
        self.assertEqual(config["avatar_style"], "circle")
        free = self.owner.put("/api/agents/bot", json={**config, "avatar_style": "free"})
        self.assertEqual((free.status_code, free.json()["avatar_style"]), (200, "free"))
        self.assertEqual(self.agent_yaml()["avatar_style"], "free")
        omitted = {key: value for key, value in config.items() if key != "avatar_style"}
        kept = self.owner.put("/api/agents/bot", json={**omitted, "description": "changed"})
        self.assertEqual((kept.status_code, kept.json()["avatar_style"]), (200, "free"))
        self.assertEqual(self.agent_yaml()["avatar_style"], "free")
        self.assertEqual(self.owner.put("/api/agents/bot", json={**config, "avatar_style": "square"}).status_code, 422)
        back = self.owner.put("/api/agents/bot", json={**config, "avatar_style": "circle"})
        self.assertEqual(back.json()["avatar_style"], "circle")
        self.assertNotIn("avatar_style", self.agent_yaml())  # the default is not written

    def test_the_style_shows_in_the_avatar_payload(self) -> None:
        self.put(self.owner, "/api/agents/bot/avatar")
        config = self.owner.get("/api/agents/bot").json()
        config.pop("avatar"), config.pop("level")
        self.owner.put("/api/agents/bot", json={**config, "avatar_style": "free"})
        self.assertEqual(self.owner.get("/api/agents/bot").json()["avatar"]["style"], "free")

    def test_a_view_sent_back_as_read_is_accepted_and_its_avatar_info_ignored(self) -> None:
        self.put(self.owner, "/api/agents/bot/avatar")
        config = self.owner.get("/api/agents/bot").json()
        config.pop("level")
        config["avatar"] = {"url": "/api/agents/other/avatar", "version": "x", "style": "free"}
        response = self.owner.put("/api/agents/bot", json=config)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["avatar"]["url"], "/api/agents/bot/avatar")
        self.assertEqual(response.json()["avatar_style"], "circle")
        self.assertNotIn("avatar", self.agent_yaml())


class RenameTests(AvatarFixture):
    def test_renaming_the_agent_moves_its_picture(self) -> None:
        self.put(self.owner, "/api/agents/bot/avatar")
        config = self.owner.get("/api/agents/bot").json()
        config.pop("avatar"), config.pop("level")
        moved = self.owner.put("/api/agents/bot", json={**config, "id": "robot"})
        self.assertEqual(moved.status_code, 200, moved.text)
        self.assertEqual(moved.json()["avatar"]["url"], "/api/agents/robot/avatar")
        folder = self.data_dir / "user-a" / ".avatars"
        self.assertTrue((folder / "robot.png").is_file())
        self.assertFalse((folder / "bot.png").exists())
        self.assertEqual(self.owner.get("/api/agents/robot/avatar").status_code, 200)

    def test_the_admin_rename_command_moves_it_too(self) -> None:
        from src.admin.agents import agent_rename
        self.put(self.owner, "/api/agents/bot/avatar")
        self.assertEqual(agent_rename(self.service, "user-a", "bot", "robot", None), 0)
        folder = self.data_dir / "user-a" / ".avatars"
        self.assertTrue((folder / "robot.png").is_file())
        self.assertFalse((folder / "bot.png").exists())

    def test_the_admin_cli_sets_the_style_and_shows_it(self) -> None:
        import contextlib
        from src.admin.agents import agent_set, agent_show
        self.assertEqual(agent_set(self.service, "user-a", "bot", ["avatar_style=free"]), 0)
        self.assertEqual(self.agent_yaml()["avatar_style"], "free")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            agent_show(self.service, "user-a", "bot")
        self.assertIn("avatar_style: free", out.getvalue())
        agent_set(self.service, "user-a", "bot", ["avatar_style=circle"])
        self.assertNotIn("avatar_style", self.agent_yaml())

    def test_deleting_the_agent_keeps_the_picture_like_its_data(self) -> None:
        self.put(self.owner, "/api/agents/bot/avatar")
        self.assertEqual(self.owner.delete("/api/agents/bot").status_code, 204)
        self.assertTrue((self.data_dir / "user-a" / ".avatars" / "bot.png").is_file())

    def test_the_picture_never_sits_in_the_agents_data_folder(self) -> None:
        self.put(self.owner, "/api/agents/bot/avatar")
        self.assertFalse(list((self.data_dir / "user-a" / "bot").glob("**/*.png")) if (self.data_dir / "user-a" / "bot").exists() else [])


class UserAvatarTests(AvatarFixture):
    def test_me_has_no_avatar_until_one_is_uploaded(self) -> None:
        self.assertIsNone(self.owner.get("/api/me").json()["avatar"])
        self.assertEqual(self.owner.get("/api/me/avatar").status_code, 404)

    def test_upload_get_headers_and_delete(self) -> None:
        response = self.put(self.owner, "/api/me/avatar")
        body = response.json()
        self.assertEqual((response.status_code, body["url"], body["has_alpha"]), (200, "/api/me/avatar", False))
        got = self.owner.get("/api/me/avatar")
        self.assertEqual(got.headers["content-type"], "image/png")
        self.assertEqual(got.headers["x-content-type-options"], "nosniff")
        self.assertEqual(got.headers["cache-control"], "private, max-age=0, must-revalidate")
        self.assertEqual(self.owner.get("/api/me/avatar", headers={"If-None-Match": got.headers["etag"]}).status_code, 304)
        self.assertTrue((self.data_dir / "user-a" / ".avatars" / "_user.png").is_file())
        self.assertEqual(self.owner.get("/api/me").json()["avatar"], {"url": "/api/me/avatar", "version": body["version"], "style": "circle"})
        self.assertEqual(self.owner.delete("/api/me/avatar").status_code, 204)
        self.assertEqual(self.owner.get("/api/me/avatar").status_code, 404)

    def test_each_user_has_only_their_own(self) -> None:
        self.put(self.owner, "/api/me/avatar")
        self.assertEqual(self.stranger.get("/api/me/avatar").status_code, 404)
        self.assertIsNone(self.stranger.get("/api/me").json()["avatar"])
        self.put(self.stranger, "/api/me/avatar", image_bytes("JPEG"), "image/jpeg")
        self.assertEqual(self.owner.get("/api/me/avatar").headers["content-type"], "image/png")
        self.assertTrue((self.data_dir / "user-b" / ".avatars" / "_user.png").is_file())

    def test_anonymous_is_401_and_bad_uploads_are_refused(self) -> None:
        self.assertEqual(self.put(self.anonymous, "/api/me/avatar").status_code, 401)
        self.assertEqual(self.anonymous.get("/api/me/avatar").status_code, 401)
        self.assertEqual(self.anonymous.delete("/api/me/avatar").status_code, 401)
        self.assertEqual(self.put(self.owner, "/api/me/avatar", b"x", "text/plain").status_code, 415)
        self.assertEqual(self.put(self.owner, "/api/me/avatar", b"x").status_code, 400)
        self.assertEqual(self.put(self.owner, "/api/me/avatar", b"\0" * (5 * 1024 * 1024 + 1)).status_code, 413)

    def test_the_style_is_a_validated_preference_shown_in_me(self) -> None:
        self.assertEqual(self.owner.get("/api/preferences").json()["avatar_style"], "circle")
        self.assertEqual(self.owner.get("/api/preferences").json()["defaults"]["avatar_style"], "circle")
        self.put(self.owner, "/api/me/avatar")
        saved = self.owner.patch("/api/preferences", json={"avatar_style": "free"})
        self.assertEqual((saved.status_code, saved.json()["avatar_style"]), (200, "free"))
        self.assertEqual(self.owner.get("/api/me").json()["avatar"]["style"], "free")
        self.assertEqual(json.loads((self.config_dir / "user-a" / "preferences.json").read_text())["avatar_style"], "free")
        self.assertEqual(self.owner.patch("/api/preferences", json={"avatar_style": "square"}).status_code, 422)
        self.assertEqual(self.owner.patch("/api/preferences", json={"avatar_style": None}).status_code, 422)
        self.assertEqual(self.owner.patch("/api/preferences", json={"show_thinking": False}).json()["avatar_style"], "free")
        self.assertEqual(self.stranger.get("/api/preferences").json()["avatar_style"], "circle")
