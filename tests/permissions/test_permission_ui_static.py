"""Permission levels UI: the browser logic agrees with the server rule, the markup has the ids the script uses, and every
text the new screens show has a Spanish translation."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.config.levels import required_level
from src.config.levels_help import levels_catalog
from src.config.models import AgentConfig
from src.terminal.rules import LEARNED_REASON
from src.utils.i18n import messages

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "web" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")

NEW_IDS = (
    "agent-level-section", "agent-level", "agent-level-description", "agent-level-info", "agent-level-message",
    "agent-form-level", "agent-rules", "agent-rules-default", "agent-rules-list", "agent-rules-empty", "agent-rules-add",
    "agent-rules-error", "agent-rules-backends", "agent-rules-effective", "agent-rules-effective-body",
    "settings-tab-users", "users-admin", "users-list", "users-status", "users-error",
)
BACKENDS = {
    "pc": SimpleNamespace(type="ssh", user="deploy"), "box": SimpleNamespace(type="ssh", user="deploy"),
    "rootbox": SimpleNamespace(type="ssh", user="root"), "local": SimpleNamespace(type="local"),
}
NODE = r"""
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const source = readFileSync(process.argv[2], "utf8");
const take = (name) => source.match(new RegExp(`function ${name}\\([\\s\\S]*?\\r?\\n\\}\\r?\\n`))[0];
const names = ["levelNumber", "requiredLevelFor"];
const context = vm.createContext({ tr: (text) => text });
vm.runInContext(`${names.map(take).join("\n")}\n${names.map((n) => `this.${n} = ${n};`).join(" ")}`, context);
const { catalog, scenarios } = JSON.parse(readFileSync(process.argv[3], "utf8"));
console.log(JSON.stringify(scenarios.map((choices) => context.requiredLevelFor(catalog, choices).level)));
"""


def scenarios() -> list[tuple[dict, AgentConfig]]:
    """(the form's choices, the same choices as the stored agent) for a spread of configurations."""
    from src.agent.tool_groups import TOOL_GROUP_CATALOG

    groups = [item["id"] for item in TOOL_GROUP_CATALOG]
    cases: list[tuple[dict, dict]] = [({"toolGroups": [], "mcp": False, "backends": [], "delegationTargets": False}, {"tool_groups": []})]
    for group in groups:
        base = {"toolGroups": [group], "mcp": False, "backends": [], "delegationTargets": False}
        cases.append((base, {"tool_groups": [group]}))
    cases.append(({"toolGroups": ["memory_read", "web"], "mcp": True, "backends": [], "delegationTargets": False},
                  {"tool_groups": ["memory_read", "web"], "mcp": {"srv": {}}}))
    for names in (["pc"], ["box"], ["pc", "rootbox"], ["local"], ["pc", "gone"]):
        cases.append((
            {"toolGroups": ["terminal"], "mcp": False, "delegationTargets": False,
             "backends": [{"name": n, "min_level": None} for n in names]},
            {"tool_groups": ["terminal"], "terminal": {"backends": {n: {} for n in names}, "default_backend": names[0]}},
        ))
    cases.append(({"toolGroups": ["delegate"], "mcp": False, "backends": [], "delegationTargets": True},
                  {"tool_groups": ["delegate"], "delegation": {"enabled": True, "targets": [{"agent": "other"}]}}))
    cases.append(({"toolGroups": ["delegate"], "mcp": False, "backends": [], "delegationTargets": False},
                  {"tool_groups": ["delegate"], "delegation": {"enabled": True}}))
    result = []
    for number, (choices, config) in enumerate(cases):
        chat = {"tool_groups": config["tool_groups"], **({"mcp": config["mcp"]} if "mcp" in config else {})}
        extra = {key: config[key] for key in ("terminal", "delegation") if key in config}
        result.append((choices, AgentConfig.model_validate({"id": f"a{number}", "name": "A", "chat_config": chat, **extra})))
    return result


class BrowserRuleMatchesServerTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_required_level_in_the_browser_equals_required_level_on_the_server(self) -> None:
        catalog = levels_catalog()
        cases = scenarios()
        by_name = {name: backend.__dict__.get("user") for name, backend in BACKENDS.items()}
        from src.config.levels import backend_min_level

        payload_cases = []
        for choices, _ in cases:
            choices = json.loads(json.dumps(choices))
            for backend in choices["backends"]:  # what GET /api/me gives: the level of each defined backend
                if backend["name"] in BACKENDS:
                    backend["min_level"] = backend_min_level(BACKENDS[backend["name"]]).label
            payload_cases.append(choices)
        self.assertIn("root", by_name.values())
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "cases.json"
            data.write_text(json.dumps({"catalog": catalog, "scenarios": payload_cases}), encoding="utf-8")
            script = Path(directory) / "run.cjs"
            script.write_text(NODE, encoding="utf-8")
            out = subprocess.run(["node", str(script), str(STATIC / "app.js"), str(data)], capture_output=True, text=True, check=True)
        browser = json.loads(out.stdout)
        server = [required_level(config, BACKENDS).label for _, config in cases]
        self.assertEqual(browser, server)
        self.assertGreater(len(set(server)), 3)  # the scenarios really span several levels


class MarkupTests(unittest.TestCase):
    def test_the_script_and_the_markup_share_the_new_ids(self) -> None:
        for element_id in NEW_IDS:
            with self.subTest(id=element_id):
                self.assertEqual(INDEX_HTML.count(f'id="{element_id}"'), 1)
                self.assertIn(element_id, APP_JS)

    def test_every_dollar_lookup_of_the_new_code_exists_in_the_markup(self) -> None:
        ids = set(re.findall(r'id="([^"]+)"', INDEX_HTML))
        for element_id in re.findall(r'\$\("((?:agent-(?:level|rules)|users|settings-tab-users)[a-z-]*)"\)', APP_JS):
            with self.subTest(id=element_id):
                self.assertIn(element_id, ids)

    def test_the_users_tab_starts_hidden_and_is_outside_the_settings_form(self) -> None:
        self.assertRegex(INDEX_HTML, r'<button id="settings-tab-users"[^>]* hidden>')
        self.assertRegex(INDEX_HTML, r'<section id="users-admin"[^>]* hidden>')
        form = INDEX_HTML[INDEX_HTML.index('<form id="settings-form"'):INDEX_HTML.index("</form>", INDEX_HTML.index('<form id="settings-form"'))]
        self.assertNotIn("users-admin", form)

    def test_the_learned_reason_is_the_one_the_server_writes(self) -> None:
        self.assertIn(f'const LEARNED_RULE_REASON = "{LEARNED_REASON}";', APP_JS)


class LocalizationTests(unittest.TestCase):
    def test_option_labels_built_from_tables_have_a_translation(self) -> None:
        spanish = messages("es")
        for table in ("RULE_TOOLS", "RULE_DANGEROUS"):
            block = re.search(rf"const {table} = \[(.*?)\];", APP_JS, re.S).group(1)
            for label in re.findall(r'\["[a-z_]*", "([^"]+)"\]', block):
                with self.subTest(table=table, label=label):
                    self.assertIn(label, spanish)
        for label in ("Allow", "Ask", "Deny", "Any kind"):
            self.assertIn(label, spanish)

    def test_new_server_codes_have_spanish_strings(self) -> None:
        spanish = messages("es")
        for template in (
            "administrator access is required",
            "You cannot change your own maximum level; ask another administrator.",
            "The maximum level '{max_level}' is below the level of these agents of '{user_id}': {names}. Lower those agents "
            "(or remove the services that need the level) first, then lower the maximum.",
        ):
            self.assertIn(template, spanish)

    def test_the_guardrail_message_is_translated_with_the_same_fields(self) -> None:
        from src.config.levels import APPROVAL_RULE_NOT_ALLOWED

        spanish = messages("es")
        self.assertIn(APPROVAL_RULE_NOT_ALLOWED, spanish)
        fields = lambda text: set(re.findall(r"\{(\w+)\}", text))
        self.assertEqual(fields(APPROVAL_RULE_NOT_ALLOWED), fields(spanish[APPROVAL_RULE_NOT_ALLOWED]))


if __name__ == "__main__":
    unittest.main()
