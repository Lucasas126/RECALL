"""The `secrets` tool group: names only, one approval per change, never a value in a result, a card or the audit."""
import json
import os
from unittest.mock import patch

from tests.support.agents_tool_helpers import AgentsFixture
from tests.support.permissions_tool_helpers import Run, resolved

from src.agent.toolsets.secrets import build_secrets_tools
from src.agent.tool_groups import DISPOSABLE_BLOCKED_TOOLS, SECRET_TOOLS
from src.config import secrets, settings
from src.config.levels import Level
from src.terminal.audit import audit_path
from src.terminal.context import run_context

VALUE = "sk-or-v1-supersecretvalue1234567890"
OTHER = "other-secret-value-0987654321"


class SecretsToolTests(AgentsFixture):
    def setUp(self) -> None:
        super().setUp()
        self.write_agent("chief", "id: chief\nname: Chief\nlevel: admin\nchat_config:\n  tool_groups: [memory_read, secrets]\n")
        (self.config_dir / "secrets.env").write_text(f"# keep\nSRC_KEY={VALUE}\nOLD_KEY={OTHER}\n", encoding="utf-8")
        for name in ("SRC_KEY", "OLD_KEY", "DST_KEY", "NEW_KEY"):
            os.environ.pop(name, None)
        self.addCleanup(lambda: setattr(secrets, "_cache", None))

    def secret_tools(self, level: Level = Level.ADMIN) -> dict:
        built = build_secrets_tools(resolved("user-a", "chief", level, ["memory_read", "secrets"]))
        return {func.tool_spec["function"]["name"]: func for func in built}

    def call(self, tool_name: str, run: Run, /, **args) -> str:
        with run_context(run.context()):
            return self.secret_tools()[tool_name](**args)

    def run_of(self, answers=(("approve", "once"),), **kwargs) -> Run:
        return Run(self.service, "chief", answers, **kwargs)

    def file(self) -> dict:
        return secrets.parse((self.config_dir / "secrets.env").read_text(encoding="utf-8"))

    def everything_seen(self, run: Run, *results: str) -> str:
        audit = audit_path(self.data_dir, "user-a", "chief")
        written = audit.read_text(encoding="utf-8") if audit.exists() else ""
        return "\n".join([*results, json.dumps(run.cards), written])

    def test_only_an_admin_gets_the_tools(self):
        self.assertEqual(set(SECRET_TOOLS), set(self.secret_tools()))
        self.assertEqual({}, self.secret_tools(Level.OPERATOR))

    def test_a_disposable_chat_never_gets_them(self):
        self.assertTrue(set(SECRET_TOOLS) <= DISPOSABLE_BLOCKED_TOOLS)

    def test_list_returns_names_and_never_a_value(self):
        run = self.run_of()
        text = self.call("secret_list", run)
        names = {item["name"]: item["has_value"] for item in json.loads(text)["secrets"]}
        self.assertEqual({"SRC_KEY": True, "OLD_KEY": True}, names)
        self.assertNotIn(VALUE, text)
        self.assertNotIn(OTHER, text)

    def test_copy_moves_the_value_without_showing_it(self):
        run = self.run_of()
        result = self.call("secret_copy", run, source="SRC_KEY", target="DST_KEY")
        self.assertEqual("applied", json.loads(result)["status"])
        self.assertEqual(VALUE, self.file()["DST_KEY"])
        self.assertEqual(VALUE, self.file()["SRC_KEY"])  # the source stays
        self.assertEqual(1, len(run.cards))
        for value in (VALUE, OTHER):
            self.assertNotIn(value, self.everything_seen(run, result))

    def test_the_card_warns_when_the_target_gets_overwritten(self):
        run = self.run_of()
        self.call("secret_copy", run, source="SRC_KEY", target="OLD_KEY")
        self.assertIn("OVERWRITES", json.dumps(run.cards))
        self.assertEqual(VALUE, self.file()["OLD_KEY"])

    def test_a_denied_change_changes_nothing(self):
        run = self.run_of(answers=(("deny", "once"),))
        result = self.call("secret_copy", run, source="SRC_KEY", target="DST_KEY")
        self.assertTrue(result.startswith("error:"))
        self.assertNotIn("DST_KEY", self.file())

    def test_no_scope_wider_than_once_applies(self):
        run = self.run_of(answers=(("approve", "always"),))
        result = self.call("secret_delete", run, name="OLD_KEY")
        self.assertTrue(result.startswith("error:"))
        self.assertIn("OLD_KEY", self.file())

    def test_rename_moves_the_value_and_refuses_to_overwrite(self):
        run = self.run_of()
        self.assertEqual("applied", json.loads(self.call("secret_rename", run, old="OLD_KEY", new="NEW_KEY"))["status"])
        self.assertEqual(OTHER, self.file()["NEW_KEY"])
        self.assertNotIn("OLD_KEY", self.file())
        refused = self.call("secret_rename", self.run_of(), old="SRC_KEY", new="NEW_KEY")
        self.assertTrue(refused.startswith("error:"))
        self.assertEqual(VALUE, self.file()["SRC_KEY"])
        self.assertEqual(OTHER, self.file()["NEW_KEY"])

    def test_delete_removes_only_that_line_and_keeps_comments(self):
        run = self.run_of()
        self.call("secret_delete", run, name="OLD_KEY")
        text = (self.config_dir / "secrets.env").read_text(encoding="utf-8")
        self.assertNotIn("OLD_KEY", text)
        self.assertIn("SRC_KEY=" + VALUE, text)
        self.assertIn("# keep", text)

    def test_deleting_a_missing_name_asks_nobody(self):
        run = self.run_of()
        self.assertEqual("unchanged", json.loads(self.call("secret_delete", run, name="NOPE_KEY"))["status"])
        self.assertEqual([], run.cards)

    def test_copying_a_name_without_a_value_fails_after_the_card_and_says_nothing_changed(self):
        run = self.run_of()
        result = self.call("secret_copy", run, source="MISSING_KEY", target="DST_KEY")
        self.assertTrue(result.startswith("error:"))
        self.assertIn("Nothing was changed", result)
        self.assertNotIn("DST_KEY", self.file())

    def test_bad_names_never_reach_the_card(self):
        run = self.run_of()
        for args in ({"source": "bad name", "target": "DST_KEY"}, {"source": "SRC_KEY", "target": "../x"}, {"source": "SRC_KEY", "target": "SRC_KEY"}):
            self.assertTrue(self.call("secret_copy", run, **args).startswith("error:"))
        self.assertEqual([], run.cards)

    def test_every_attempt_is_audited_by_name_only(self):
        run = self.run_of(answers=(("deny", "once"), ("approve", "once")))
        results = [self.call("secret_delete", run, name="OLD_KEY"), self.call("secret_delete", run, name="OLD_KEY")]
        lines = [json.loads(line) for line in audit_path(self.data_dir, "user-a", "chief").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(["denied", "approved"], [line["decision"] for line in lines])
        self.assertTrue(all(line["tool"] == "secret_delete" and line["target"] == "secrets.env" for line in lines))
        for value in (VALUE, OTHER):
            self.assertNotIn(value, self.everything_seen(run, *results))

    def test_an_unwritable_audit_file_stops_the_change(self):
        run = self.run_of()
        with patch("src.agent.toolsets.secrets.audit_writable", return_value=False):
            result = self.call("secret_delete", run, name="OLD_KEY")
        self.assertTrue(result.startswith("error:"))
        self.assertEqual([], run.cards)
        self.assertIn("OLD_KEY", self.file())

    def test_nothing_runs_without_a_person_to_ask(self):
        quiet = self.run_of(interactive=False)
        self.assertIn("only available in a chat", self.call("secret_list", quiet))
        self.assertIn("only available in a chat", self.call("secret_copy", quiet, source="SRC_KEY", target="DST_KEY"))
        self.assertNotIn("DST_KEY", self.file())
        self.assertIn("only available in a chat", self.secret_tools()["secret_list"]())  # no run context at all
