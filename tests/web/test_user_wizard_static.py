"""The administrator's onboarding wizard: every element the script uses exists, and its texts are translated."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from src.utils.i18n import _SPANISH

STATIC = Path(__file__).resolve().parents[2] / "src" / "web" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
SCRIPT = (STATIC / "app.js").read_text(encoding="utf-8")
WIZARD = re.search(r"// ---- user onboarding wizard[\s\S]*?// ---- end user onboarding wizard", SCRIPT).group(0)


class UserWizardStaticTests(unittest.TestCase):
    def test_every_element_id_exists(self) -> None:
        ids = set(re.findall(r'\$\("((?:wizard|user)-[a-z0-9-]+)"\)', WIZARD)) | {f"wizard-step-{i}" for i in range(1, 5)}
        for element in ids:
            self.assertIn(f'id="{element}"', HTML, element)

    def test_only_administrators_see_the_button(self) -> None:
        self.assertIn('$("user-add").hidden = !isAdministrator(state.user);', SCRIPT)
        self.assertIn('id="user-add" type="button" hidden', HTML)

    def test_the_secrets_are_not_kept_or_put_in_a_url(self) -> None:
        self.assertNotIn("?key=", WIZARD)
        self.assertIn('$("wizard-key").value = ""', WIZARD)
        self.assertIn('$("wizard-generated").textContent = ""', WIZARD)

    def test_a_step_change_scrolls_the_wizard_to_its_top_without_animation(self) -> None:
        shown = re.search(r"function wizardShow[\s\S]*?\n\}\n", WIZARD).group(0)
        self.assertIn('$("user-wizard").scrollIntoView({ block: "start", behavior: "instant" });', shown)

    def test_the_agent_id_is_suggested_until_the_user_types_one(self) -> None:
        self.assertIn("if (!wizard.agentIdEdited) $(\"wizard-agent-id\").value = suggestAgentId(", WIZARD)
        self.assertIn('$("wizard-agent-id").addEventListener("input", () => { wizard.agentIdEdited = true; });', WIZARD)
        self.assertIn('$("wizard-template").addEventListener("change", wizardSuggestAgentId);', WIZARD)
        self.assertIn("agentIdEdited: false", WIZARD)  # reset when the wizard opens

    def test_texts_have_spanish_translations(self) -> None:
        for text in re.findall(r'tr\("([^"]+)"', WIZARD):
            self.assertIn(text, _SPANISH, text)
        for element in re.findall(r'\["((?:wizard|user)-[a-z0-9-]+)", "([^"]+)"\]', SCRIPT):
            self.assertIn(element[1], _SPANISH, element[1])


if __name__ == "__main__":
    unittest.main()
