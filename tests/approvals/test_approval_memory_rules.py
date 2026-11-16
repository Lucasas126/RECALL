"""Approval memory, the pure part: the narrow rule an approval card writes and the scopes a card may offer."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("RECALL_SESSION_SECRET", "x")

from src.terminal.rules import (
    LEARNED_REASON, Action, ApprovalRule, ApprovalRuleList, Decision, NoNarrowRule, RuleSet, action_signature, evaluate,
    offered_scopes, rule_for_action, rule_key, rules_for_action,
)


def cmd(command: str, *, backend: str = "box", kind: str = "sandbox", dangerous: bool = False) -> Action:
    return Action("terminal", command, "", backend, kind, dangerous)


def file_action(tool: str, path: str, *, backend: str = "box") -> Action:
    return Action(tool, "", path, backend, "sandbox")  # type: ignore[arg-type]


def ruleset(**layers) -> RuleSet:
    return RuleSet.build(**{name: ApprovalRuleList.model_validate(value) for name, value in layers.items()})


ASK = Decision("ask", "builtin", None, "default", "no approval rule allows this action")


class NarrowRuleTests(unittest.TestCase):
    def test_a_single_command_gets_one_exact_rule_on_its_backend(self) -> None:
        rule = rule_for_action(cmd("git status -s"))
        self.assertEqual(
            (rule.verdict, rule.tool, rule.command, rule.exact, rule.backend, rule.reason),
            ("allow", "terminal", "git status -s", True, "box", LEARNED_REASON),
        )
        self.assertFalse(rule.blanket)
        self.assertIsNone(rule.path)

    def test_the_rule_is_exact_not_a_prefix_and_not_a_glob(self) -> None:
        rules = ruleset(agent={"rules": [rule_for_action(cmd("git status")).model_dump()]})
        self.assertEqual(evaluate(rules, cmd("git status")).verdict, "allow")
        self.assertEqual(evaluate(rules, cmd("git status -s")).verdict, "ask")  # a plain `command` would be a word prefix
        self.assertEqual(evaluate(rules, cmd("git status", backend="other")).verdict, "ask")
        star = ruleset(agent={"rules": [rule_for_action(cmd("rm *.tmp")).model_dump()]})
        self.assertEqual(evaluate(star, cmd("rm *.tmp")).verdict, "allow")
        self.assertEqual(evaluate(star, cmd("rm a.tmp")).verdict, "ask")  # `*` is a literal character in a learned rule

    def test_a_multi_segment_command_gets_one_rule_per_segment(self) -> None:
        action = cmd("cd /srv && ls -l | wc -l")
        rules = rules_for_action(action)
        self.assertEqual([rule.command for rule in rules], ["cd /srv", "ls -l", "wc -l"])
        self.assertTrue(all(rule.exact and rule.backend == "box" for rule in rules))
        with self.assertRaises(NoNarrowRule):
            rule_for_action(action)  # the single-rule helper refuses several segments
        learned = ruleset(agent={"rules": [rule.model_dump() for rule in rules]})
        self.assertEqual(evaluate(learned, action).verdict, "allow")
        self.assertEqual(evaluate(learned, cmd("cd /srv && ls -l | wc -l && rm x")).verdict, "ask")

    def test_a_redirection_segment_is_matched_exactly_too(self) -> None:
        rules = rules_for_action(cmd("cat a > b"))
        self.assertEqual([rule.command for rule in rules], ["cat a", "> b"])

    def test_file_operations_get_the_exact_path_and_tool_kind(self) -> None:
        write = rule_for_action(file_action("file_write", "/srv/app/config.yaml"))
        self.assertEqual((write.tool, write.path, write.exact, write.backend, write.command), ("file_write", "/srv/app/config.yaml", True, "box", None))
        learned = ruleset(agent={"rules": [write.model_dump()]})
        self.assertEqual(evaluate(learned, file_action("file_write", "/srv/app/config.yaml")).verdict, "allow")
        self.assertEqual(evaluate(learned, file_action("file_write", "/srv/app/config.yaml2")).verdict, "ask")
        self.assertEqual(evaluate(learned, file_action("file_write", "/srv/app/*")).verdict, "ask")
        self.assertEqual(evaluate(learned, file_action("file_read", "/srv/app/config.yaml")).verdict, "ask")  # another kind
        read = rule_for_action(file_action("file_read", "~/notes/a.md"))
        self.assertEqual((read.tool, read.path), ("file_read", "~/notes/a.md"))

    def test_a_path_with_glob_characters_is_literal(self) -> None:
        learned = ruleset(agent={"rules": [rule_for_action(file_action("file_write", "/srv/[ab]*.txt")).model_dump()]})
        self.assertEqual(evaluate(learned, file_action("file_write", "/srv/[ab]*.txt")).verdict, "allow")
        self.assertEqual(evaluate(learned, file_action("file_write", "/srv/a1.txt")).verdict, "ask")

    def test_nothing_narrow_is_written_when_the_action_cannot_be_pinned(self) -> None:
        for action in (cmd(""), cmd("ls", backend=""), cmd("x" * 600), file_action("file_write", ""), cmd("ls", backend="Bad Name!")):
            with self.subTest(action=action), self.assertRaises(NoNarrowRule):
                rules_for_action(action)

    def test_a_command_with_a_secret_is_never_written_to_the_configuration(self) -> None:
        with self.assertRaises(NoNarrowRule):
            rules_for_action(cmd("curl -H 'Authorization: Bearer abcdef123456' https://example.com"))

    def test_the_rule_never_matches_without_a_backend_and_dedupes_by_key(self) -> None:
        one, two = rule_for_action(cmd("ls")), rule_for_action(cmd("ls"))
        self.assertEqual(rule_key(one), rule_key(two))
        self.assertNotEqual(rule_key(one), rule_key(rule_for_action(cmd("ls", backend="b2"))))

    def test_exact_needs_a_command_or_a_path(self) -> None:
        with self.assertRaises(ValueError):
            ApprovalRule(verdict="allow", exact=True)
        self.assertEqual(ApprovalRule(verdict="allow", command="ls", exact=True).command, "ls")

    def test_signatures_separate_tool_backend_and_target(self) -> None:
        base = action_signature(cmd("ls"))
        self.assertNotEqual(base, action_signature(cmd("ls", backend="other")))
        self.assertNotEqual(base, action_signature(cmd("ls -l")))
        self.assertNotEqual(action_signature(file_action("file_read", "/a")), action_signature(file_action("file_write", "/a")))


class OfferedScopesTests(unittest.TestCase):
    def test_a_plain_ask_offers_once_session_and_agent(self) -> None:
        self.assertEqual(offered_scopes(RuleSet(), cmd("ls"), shorthand=None, decision=ASK), ("once", "session", "agent"))
        self.assertEqual(
            offered_scopes(RuleSet(), file_action("file_write", "/a"), shorthand="manual", decision=ASK),
            ("once", "session", "agent"),
        )

    def test_a_dangerous_action_offers_only_once(self) -> None:
        self.assertEqual(offered_scopes(RuleSet(), cmd("rm -rf x", dangerous=True), shorthand=None, decision=ASK), ("once",))

    def test_a_deny_decision_offers_nothing_more(self) -> None:
        deny = Decision("deny", "agent", "agent:0", "rule", "rule agent:0")
        self.assertEqual(offered_scopes(RuleSet(), cmd("ls"), shorthand=None, decision=deny), ("once",))

    def test_an_ask_rule_allows_session_but_not_agent_because_an_allow_rule_would_never_win(self) -> None:
        rules = ruleset(user={"rules": [{"verdict": "ask", "command": "deploy*"}]})
        decision = evaluate(rules, cmd("deploy now"))
        self.assertEqual(decision.verdict, "ask")
        self.assertEqual(offered_scopes(rules, cmd("deploy now"), shorthand=None, decision=decision), ("once", "session"))

    def test_one_asking_segment_among_asking_rules_blocks_agent(self) -> None:
        rules = ruleset(user={"rules": [{"verdict": "ask", "command": "make"}]})
        action = cmd("ls && make")
        decision = evaluate(rules, action)
        self.assertEqual(offered_scopes(rules, action, shorthand=None, decision=decision), ("once", "session"))

    def test_a_deny_segment_never_reaches_a_card(self) -> None:
        rules = ruleset(agent={"rules": [{"verdict": "deny", "command": "rm"}]})
        decision = evaluate(rules, cmd("ls && rm x"))
        self.assertEqual(decision.verdict, "deny")
        self.assertEqual(offered_scopes(rules, cmd("ls && rm x"), shorthand=None, decision=decision), ("once",))

    def test_manual_shorthand_is_lifted_by_the_learned_rule(self) -> None:
        self.assertIn("agent", offered_scopes(RuleSet(), cmd("ls"), shorthand="manual", decision=ASK))

    def test_an_action_without_a_target_or_backend_offers_once_only(self) -> None:
        self.assertEqual(offered_scopes(RuleSet(), cmd("ls", backend=""), shorthand=None, decision=ASK), ("once",))
        self.assertEqual(offered_scopes(RuleSet(), cmd(""), shorthand=None, decision=ASK), ("once",))
        self.assertEqual(offered_scopes(RuleSet(), cmd("x" * 600), shorthand=None, decision=ASK), ("once", "session"))

    def test_a_learned_rule_never_touches_the_floor_order(self) -> None:
        # the floor is decided before the rules (TerminalSession.decide); here only that an appended rule is an
        # ordinary agent-layer allow that a deny of any layer still beats
        learned = RuleSet().with_rule("agent", rule_for_action(cmd("rm x")))
        with_deny = learned.with_rule("installation", ApprovalRule(verdict="deny", command="rm"))
        self.assertEqual(evaluate(learned, cmd("rm x")).verdict, "allow")
        self.assertEqual(evaluate(with_deny, cmd("rm x")).verdict, "deny")


if __name__ == "__main__":
    unittest.main()
