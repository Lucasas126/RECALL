"""Approval rules, pure part: shapes, layers, verdict resolution, shorthand equivalence and the blanket-allow guardrail."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

from pydantic import ValidationError


os.environ.setdefault("RECALL_SESSION_SECRET", "x")

from src.config.levels import ApprovalRuleNotAllowed, Level, check_agent_level, check_approval_rules
from src.config.models import AgentConfig, UserConfig
from src.terminal.policy import command_units
from src.terminal.rules import (
    Action, ApprovalRule, ApprovalRuleList, RuleSet, blanket_allows, evaluate, glob_match,
)

SSH = {"type": "ssh", "host": "box.example", "user": "deploy", "key_file": "/k", "known_hosts_file": "/h"}


def rule(verdict: str, **fields) -> dict:
    return {"verdict": verdict, **fields}


def rules(installation=None, user=None, agent=None) -> RuleSet:
    def lst(value):
        if value is None:
            return None
        return ApprovalRuleList.model_validate(value if isinstance(value, dict) else {"rules": value})

    return RuleSet.build(lst(installation), lst(user), lst(agent))


def cmd(command: str, *, kind: str = "sandbox", backend: str = "box", dangerous: bool = False, why: str = "") -> Action:
    return Action("terminal", command, "", backend, kind, dangerous, why)


def files(tool: str, path: str, *, kind: str = "sandbox", backend: str = "box", dangerous: bool = False) -> Action:
    return Action(tool, "", path, backend, kind, dangerous)  # type: ignore[arg-type]


class CommandUnitTests(unittest.TestCase):
    def test_every_segment_is_a_unit(self) -> None:
        self.assertEqual(command_units("ls -l && cat a.txt | wc -l; echo hi"), ["ls -l", "cat a.txt", "wc -l", "echo hi"])

    def test_redirections_are_their_own_units(self) -> None:
        self.assertEqual(command_units("cat a > b"), ["cat a", "> b"])
        self.assertEqual(command_units("make 2> err.log"), ["make", "> err.log"])
        self.assertEqual(command_units("ls >> /tmp/x"), ["ls", "> /tmp/x"])

    def test_substitutions_and_shell_scripts_are_judged(self) -> None:
        self.assertIn("rm -rf x", command_units("echo $(rm -rf x)"))
        self.assertIn("rm -rf x", command_units("echo `rm -rf x`"))
        self.assertIn("rm -rf x", command_units("bash -c 'ls; rm -rf x'"))
        self.assertIn("rm -rf x", command_units("eval rm -rf x"))

    def test_quoted_operators_do_not_split(self) -> None:
        self.assertEqual(command_units('echo "a && b"'), ['echo "a && b"'])

    def test_empty_command_has_no_units(self) -> None:
        self.assertEqual(command_units("   "), [])


class GlobTests(unittest.TestCase):
    def test_plain_pattern_is_a_word_prefix(self) -> None:
        self.assertTrue(glob_match("git status", "git status"))
        self.assertTrue(glob_match("git status", "git status -s"))
        self.assertFalse(glob_match("git status", "git statusx"))
        self.assertFalse(glob_match("ls", "lsblk"))
        self.assertFalse(glob_match("git status", "echo git status"))

    def test_glob_pattern_is_a_full_match(self) -> None:
        self.assertTrue(glob_match("git *", "git push origin"))
        self.assertTrue(glob_match("npm run test*", "npm run test:unit"))
        self.assertFalse(glob_match("git *", "xgit push"))
        self.assertFalse(glob_match("LS*", "ls"))  # case sensitive


class ShapeValidationTests(unittest.TestCase):
    def bad(self, data) -> str:
        with self.assertRaises(ValidationError) as caught:
            ApprovalRuleList.model_validate(data)
        return str(caught.exception)

    def test_a_valid_list_loads(self) -> None:
        parsed = ApprovalRuleList.model_validate({"default": "ask", "rules": [
            rule("allow", tool="terminal", command="ls", backend_kind="sandbox", id="ls"),
            rule("deny", tool="file_write", path="/etc/*", reason="no"),
            rule("ask", dangerous=True),
        ]})
        self.assertEqual([r.verdict for r in parsed.rules], ["allow", "deny", "ask"])
        self.assertEqual(parsed.rules[2].tool, "any")

    def test_unknown_keys_are_refused(self) -> None:
        self.assertIn("extra_forbidden", self.bad({"rules": [rule("allow", comand="ls")]}))
        self.assertIn("extra_forbidden", self.bad({"defaults": "ask"}))

    def test_bad_verdicts_tools_and_kinds_are_refused(self) -> None:
        for bad in (rule("maybe"), rule("allow", tool="folder_write"), rule("allow", backend_kind="cloud"),
                    rule("allow", dangerous="yes"), {"tool": "terminal"}):
            with self.subTest(bad=bad):
                self.bad({"rules": [bad]})
        self.bad({"default": "sometimes"})

    def test_fields_must_fit_the_tool(self) -> None:
        self.assertIn("use 'path'", self.bad({"rules": [rule("allow", tool="file_read", command="ls")]}))
        self.assertIn("use 'command'", self.bad({"rules": [rule("allow", tool="terminal", path="/tmp/*")]}))

    def test_blank_and_nul_patterns_are_refused(self) -> None:
        self.bad({"rules": [rule("allow", command="  ")]})
        self.bad({"rules": [rule("allow", command="a\x00b")]})

    def test_duplicate_ids_are_refused(self) -> None:
        self.assertIn("used twice", self.bad({"rules": [rule("allow", id="x"), rule("ask", id="x")]}))

    def test_models_carry_the_lists(self) -> None:
        user = UserConfig.model_validate({
            "id": "user-a", "name": "A", "max_level": "admin", "provider": {"name": "ollama"}, "agents": [],
            "approval_rules": {"rules": [rule("deny", command="rm*")]},
        })
        self.assertEqual(user.approval_rules.rules[0].command, "rm*")
        agent = AgentConfig.model_validate({"id": "a", "name": "A", "approval_rules": {"default": "deny"}})
        self.assertEqual(agent.approval_rules.default, "deny")
        with self.assertRaises(ValidationError):
            AgentConfig.model_validate({"id": "a", "name": "A", "approval_rules": {"rules": [{"verdict": "x"}]}})

    def test_ssh_backend_kind(self) -> None:
        user = UserConfig.model_validate({
            "id": "user-a", "name": "A", "max_level": "admin", "provider": {"name": "ollama"}, "agents": [],
            "terminal_backends": {"s": {**SSH, "kind": "sandbox"}, "m": SSH, "l": {"type": "local", "workdir": "/x"}},
        })
        self.assertEqual([b.backend_kind for b in user.terminal_backends.values()], ["sandbox", "machine", "local"])
        with self.assertRaises(ValidationError):
            UserConfig.model_validate({
                "id": "user-a", "name": "A", "max_level": "admin", "provider": {"name": "ollama"}, "agents": [],
                "terminal_backends": {"s": {**SSH, "kind": "local"}},
            })
        with self.assertRaises(ValidationError):  # local is implicit and has no `kind` key
            UserConfig.model_validate({
                "id": "user-a", "name": "A", "max_level": "admin", "provider": {"name": "ollama"}, "agents": [],
                "terminal_backends": {"l": {"type": "local", "workdir": "/x", "kind": "machine"}},
            })

    def test_shipped_installation_defaults_keep_todays_behaviour(self) -> None:
        from src.config.settings import APPROVAL_RULES

        self.assertEqual(APPROVAL_RULES.default, "ask")
        self.assertEqual(APPROVAL_RULES.rules, [])


class ResolutionTests(unittest.TestCase):
    def test_nothing_defined_asks(self) -> None:
        decision = evaluate(RuleSet(), cmd("ls"))
        self.assertEqual((decision.verdict, decision.layer, decision.rule, decision.source), ("ask", "builtin", None, "default"))

    def test_the_most_specific_default_wins(self) -> None:
        for layers, expected in (
            (dict(installation={"default": "ask"}), ("ask", "installation")),
            (dict(installation={"default": "ask"}, user={"default": "allow"}), ("allow", "user")),
            (dict(installation={"default": "ask"}, user={"default": "allow"}, agent={"default": "deny"}), ("deny", "agent")),
            (dict(installation={"default": "deny"}, user={"rules": []}, agent={"rules": []}), ("deny", "installation")),
        ):
            with self.subTest(layers=layers):
                decision = evaluate(rules(**layers), cmd("ls"))
                self.assertEqual((decision.verdict, decision.layer), expected)
                self.assertIsNone(decision.rule)

    def test_deny_beats_ask_beats_allow_across_layers(self) -> None:
        match = dict(tool="terminal", command="git push")
        for installation, user, agent, expected in (
            ([rule("allow", **match)], [rule("ask", **match)], None, ("ask", "user")),
            ([rule("allow", **match)], None, [rule("deny", **match)], ("deny", "agent")),
            ([rule("deny", **match)], None, [rule("allow", **match)], ("deny", "installation")),  # a more specific allow cannot lift a deny
            ([rule("ask", **match)], [rule("allow", **match)], [rule("allow", **match)], ("ask", "installation")),
        ):
            with self.subTest(expected=expected):
                decision = evaluate(rules(installation, user, agent), cmd("git push origin main"))
                self.assertEqual((decision.verdict, decision.layer), expected)
                self.assertEqual(decision.source, "rule")

    def test_a_matching_rule_beats_any_default(self) -> None:
        decision = evaluate(rules(agent={"default": "deny", "rules": [rule("allow", command="ls")]}), cmd("ls -l"))
        self.assertEqual((decision.verdict, decision.rule), ("allow", "agent:0"))

    def test_rule_labels_use_the_index_or_the_id(self) -> None:
        ruleset = rules(user=[rule("allow", command="a"), rule("allow", command="b", id="bee")])
        self.assertEqual(evaluate(ruleset, cmd("a")).rule, "user:0")
        self.assertEqual(evaluate(ruleset, cmd("b")).rule, "user:bee")

    def test_ties_name_the_most_specific_layer_then_the_first_rule(self) -> None:
        both = rule("ask", command="x")
        decision = evaluate(rules([both], [both, both], [both]), cmd("x"))
        self.assertEqual(decision.rule, "agent:0")
        decision = evaluate(rules([both], [both, both]), cmd("x"))
        self.assertEqual(decision.rule, "user:0")

    def test_reason_carries_the_rule_text(self) -> None:
        decision = evaluate(rules(agent=[rule("ask", command="x", reason="touches production")]), cmd("x"))
        self.assertIn("agent:0", decision.reason)
        self.assertIn("touches production", decision.reason)

    def test_append_agent_rule_at_runtime(self) -> None:
        base = rules(installation={"default": "ask"})
        self.assertEqual(evaluate(base, cmd("make test")).verdict, "ask")
        grown = base.with_rule("agent", ApprovalRule(verdict="allow", command="make test"))
        self.assertEqual(evaluate(grown, cmd("make test")).verdict, "allow")
        self.assertEqual(evaluate(base, cmd("make test")).verdict, "ask", "the original set is untouched")
        with self.assertRaises(KeyError):
            base.with_rule("nobody", ApprovalRule(verdict="allow"))


class MatchingTests(unittest.TestCase):
    def test_command_matches_every_segment_and_the_worst_verdict_wins(self) -> None:
        ruleset = rules(
            installation={"default": "ask"},
            user=[rule("allow", tool="terminal", command="ls"), rule("allow", tool="terminal", command="cat")],
            agent=[rule("deny", tool="terminal", command="rm*")],
        )
        self.assertEqual(evaluate(ruleset, cmd("ls && cat a")).verdict, "allow")
        self.assertEqual(evaluate(ruleset, cmd("ls && uname")).verdict, "ask")  # `uname` matches nothing: the default
        decision = evaluate(ruleset, cmd("ls; rm -r x; uname"))
        self.assertEqual((decision.verdict, decision.rule), ("deny", "agent:0"))
        self.assertIn("rm -r x", decision.reason)

    def test_substituted_commands_are_judged(self) -> None:
        ruleset = rules(installation={"default": "ask"}, user=[rule("allow", command="echo"), rule("deny", command="rm*")])
        self.assertEqual(evaluate(ruleset, cmd("echo $(rm x)")).verdict, "deny")
        self.assertEqual(evaluate(ruleset, cmd("echo hi")).verdict, "allow")

    def test_an_allow_rule_does_not_cover_a_redirection(self) -> None:
        ruleset = rules(installation={"default": "ask"}, user=[rule("allow", command="cat")])
        self.assertEqual(evaluate(ruleset, cmd("cat a")).verdict, "allow")
        self.assertEqual(evaluate(ruleset, cmd("cat a > b")).verdict, "ask")
        self.assertEqual(evaluate(rules(user=[rule("allow", command="cat"), rule("allow", command="> b")]), cmd("cat a > b")).verdict, "allow")

    def test_an_allow_rule_does_not_cover_a_wrapped_command(self) -> None:
        ruleset = rules(installation={"default": "ask"}, user=[rule("allow", command="ls")])
        for wrapped in ("sudo ls", "env X=1 ls", "timeout 5 ls", "bash -c 'ls; id'"):
            with self.subTest(wrapped=wrapped):
                self.assertEqual(evaluate(ruleset, cmd(wrapped)).verdict, "ask")

    def test_empty_command_matches_no_command_rule(self) -> None:
        ruleset = rules(installation={"default": "ask"}, user=[rule("allow", command="*")])
        self.assertEqual(evaluate(ruleset, cmd("")).verdict, "ask")

    def test_path_glob_on_file_tools(self) -> None:
        ruleset = rules(installation={"default": "ask"}, user=[
            rule("allow", tool="file_write", path="/home/agent/work/*"),
            rule("deny", tool="file_write", path="*/.ssh/*"),
        ])
        self.assertEqual(evaluate(ruleset, files("file_write", "/home/agent/work/a/b.txt")).verdict, "allow")
        self.assertEqual(evaluate(ruleset, files("file_write", "/home/agent/work/../other/b.txt")).verdict, "ask")
        self.assertEqual(evaluate(ruleset, files("file_write", "/home/agent/work/.ssh/id")).verdict, "deny")
        self.assertEqual(evaluate(ruleset, files("file_read", "/home/agent/work/a")).verdict, "ask")  # tool differs
        self.assertEqual(evaluate(ruleset, cmd("ls")).verdict, "ask")

    def test_tool_kinds(self) -> None:
        ruleset = rules(user=[rule("allow", tool="file_read"), rule("deny", tool="file_write")])
        self.assertEqual(evaluate(ruleset, files("file_read", "/x")).verdict, "allow")
        self.assertEqual(evaluate(ruleset, files("file_write", "/x")).verdict, "deny")
        self.assertEqual(evaluate(ruleset, cmd("ls")).verdict, "ask")
        every = rules(user=[rule("deny")])
        for action in (cmd("ls"), files("file_read", "/x"), files("file_write", "/x")):
            self.assertEqual(evaluate(every, action).verdict, "deny")

    def test_backend_name_glob_and_kind(self) -> None:
        ruleset = rules(installation={"default": "ask"}, user=[
            rule("allow", tool="terminal", backend="lab*", command="ls"),
            rule("allow", tool="terminal", backend_kind="sandbox", command="pwd"),
        ])
        self.assertEqual(evaluate(ruleset, cmd("ls", backend="lab1", kind="machine")).verdict, "allow")
        self.assertEqual(evaluate(ruleset, cmd("ls", backend="prod", kind="machine")).verdict, "ask")
        self.assertEqual(evaluate(ruleset, cmd("pwd", backend="prod", kind="sandbox")).verdict, "allow")
        self.assertEqual(evaluate(ruleset, cmd("pwd", backend="prod", kind="machine")).verdict, "ask")
        self.assertEqual(evaluate(ruleset, cmd("pwd", backend="here", kind="local")).verdict, "ask")

    def test_dangerous_matching(self) -> None:
        ruleset = rules(user=[
            rule("allow", tool="terminal", dangerous=False),
            rule("ask", tool="terminal", dangerous=True, reason="destructive"),
        ])
        self.assertEqual(evaluate(ruleset, cmd("ls")).verdict, "allow")
        decision = evaluate(ruleset, cmd("rm -rf x", dangerous=True, why="recursive delete"))
        self.assertEqual((decision.verdict, decision.rule), ("ask", "user:1"))
        self.assertIn("destructive", decision.reason)

    def test_dangerous_ask_beats_a_matching_allow(self) -> None:
        ruleset = rules(user=[rule("allow", command="find"), rule("ask", dangerous=True)])
        self.assertEqual(evaluate(ruleset, cmd("find . -delete", dangerous=True)).verdict, "ask")
        self.assertEqual(evaluate(ruleset, cmd("find . -name x")).verdict, "allow")


class ShorthandTests(unittest.TestCase):
    """manual = default ask; smart = default allow + ask for dangerous; off = default allow (floor wins elsewhere)."""

    def test_manual_asks_everything(self) -> None:
        for action in (cmd("ls"), cmd("rm -rf x", dangerous=True), files("file_read", "/x"), files("file_write", "/x")):
            decision = evaluate(RuleSet(), action, shorthand="manual")
            self.assertEqual((decision.verdict, decision.rule), ("ask", "approvals:manual"))

    def test_smart_asks_only_dangerous(self) -> None:
        self.assertEqual(evaluate(RuleSet(), cmd("ls"), shorthand="smart").verdict, "allow")
        self.assertEqual(evaluate(RuleSet(), files("file_write", "/x"), shorthand="smart").verdict, "allow")
        decision = evaluate(RuleSet(), cmd("rm -rf x", dangerous=True, why="recursive delete"), shorthand="smart")
        self.assertEqual(decision.verdict, "ask")
        self.assertEqual(decision.reason, "recursive delete")

    def test_off_allows_everything_attended(self) -> None:
        for action in (cmd("ls"), cmd("rm -rf x", dangerous=True), files("file_write", "/x")):
            self.assertEqual(evaluate(RuleSet(), action, shorthand="off").verdict, "allow")

    def test_unattended(self) -> None:
        def verdict(action, mode):
            return evaluate(RuleSet(), action, shorthand=mode, attended=False)

        self.assertEqual(verdict(cmd("ls"), "manual").verdict, "deny")
        self.assertEqual(verdict(cmd("ls"), "smart").verdict, "allow")
        self.assertEqual(verdict(cmd("rm -rf x", dangerous=True), "smart").verdict, "deny")
        self.assertEqual(verdict(cmd("ls"), "off").verdict, "allow")
        self.assertEqual(verdict(files("file_read", "/x"), "off").verdict, "allow")
        self.assertEqual(verdict(cmd("rm -rf x", dangerous=True), "off").verdict, "deny")  # off never widens unattended
        self.assertEqual(verdict(files("file_write", "/x"), "off").verdict, "deny")
        self.assertEqual(verdict(cmd("ls"), "manual").source, "unattended")
        self.assertEqual(verdict(cmd("ls"), "manual").asked, "ask")

    def test_matching_rules_apply_on_top_of_the_shorthand(self) -> None:
        deny = rules(agent=[rule("deny", command="git push*")])
        ask = rules(user=[rule("ask", command="deploy*")])
        allow = rules(user=[rule("allow", command="ls")])
        self.assertEqual(evaluate(deny, cmd("git push -f"), shorthand="off").verdict, "deny")
        self.assertEqual(evaluate(ask, cmd("deploy now"), shorthand="off").verdict, "ask")
        self.assertEqual(evaluate(ask, cmd("deploy now"), shorthand="smart").verdict, "ask")
        self.assertEqual(evaluate(allow, cmd("ls"), shorthand="manual").verdict, "allow")
        self.assertEqual(evaluate(allow, cmd("uname"), shorthand="manual").verdict, "ask")

    def test_the_shorthand_beats_layer_defaults(self) -> None:
        layers = rules(installation={"default": "allow"}, user={"default": "deny"})
        self.assertEqual(evaluate(layers, cmd("ls"), shorthand="manual").verdict, "ask")
        self.assertEqual(evaluate(layers, cmd("ls")).verdict, "deny")

    def test_unattended_generalises_to_rules(self) -> None:
        ruleset = rules(agent=[rule("ask", command="deploy*"), rule("allow", command="ls"), rule("deny", command="rm*")])
        self.assertEqual(evaluate(ruleset, cmd("deploy"), attended=False).verdict, "deny")
        self.assertEqual(evaluate(ruleset, cmd("deploy"), attended=False).source, "unattended")
        self.assertEqual(evaluate(ruleset, cmd("ls"), attended=False).verdict, "allow")
        self.assertEqual(evaluate(ruleset, cmd("rm x"), attended=False).source, "rule")
        self.assertEqual(evaluate(rules(), cmd("ls"), attended=False).verdict, "deny")  # the built-in fallback asks
        self.assertEqual(evaluate(rules(user={"default": "allow"}), cmd("ls"), attended=False).verdict, "allow")


def agent_config(*, backends=("sandbox",), level=None, approvals=None, rules_=None, groups=None) -> AgentConfig:
    entries = {name: ({"approvals": approvals[name]} if approvals and name in approvals else {}) for name in backends}
    data: dict = {
        "id": "bot", "name": "Bot", "chat_config": {"tool_groups": groups or ["terminal"]},
        "terminal": {"backends": entries, "default_backend": backends[0]},
    }
    if level:
        data["level"] = level
    if rules_ is not None:
        data["approval_rules"] = rules_
    return AgentConfig.model_validate(data)


def user_config(**extra) -> UserConfig:
    return UserConfig.model_validate({
        "id": "user-a", "name": "A", "max_level": "admin", "provider": {"name": "ollama"}, "agents": [],
        "terminal_backends": {
            "sandbox": {**SSH, "kind": "sandbox"}, "pc": {**SSH, "user": "user-a"},
            "here": {"type": "local", "workdir": "/app"},
        },
        **extra,
    })


class BlanketAllowTests(unittest.TestCase):
    kinds = {"sandbox": "sandbox", "pc": "machine", "here": "local"}

    def test_blanket_detection(self) -> None:
        def problems(layers, shorthands=None, only=None):
            kinds = {k: v for k, v in self.kinds.items() if only is None or k in only}
            return blanket_allows(layers, kinds, shorthands)

        self.assertEqual(problems(rules()), [])
        self.assertEqual(problems(rules(user=[rule("allow")])), [
            "rule user:0 allows everything on backend 'pc' (machine)", "rule user:0 allows everything on backend 'here' (local)",
        ])
        self.assertEqual(problems(rules(user=[rule("allow")]), only=["sandbox"]), [])  # a sandbox may be blanket-allowed
        self.assertEqual(problems(rules(user=[rule("allow", backend_kind="sandbox")])), [])
        self.assertEqual(len(problems(rules(user=[rule("allow", backend="pc")]))), 1)
        self.assertEqual(len(problems(rules(user=[rule("allow", tool="terminal", backend_kind="machine")]))), 1)
        for narrowed in (dict(command="ls"), dict(path="/tmp/*", tool="file_write"), dict(dangerous=False)):
            self.assertEqual(problems(rules(user=[rule("allow", **narrowed)])), [], narrowed)
        self.assertEqual(problems(rules(user=[rule("ask"), rule("deny")])), [])
        self.assertEqual(len(problems(rules(installation={"default": "allow"}), only=["pc"])), 1)
        self.assertEqual(problems(rules(installation={"default": "allow"}, user={"default": "ask"})), [])  # overridden
        self.assertEqual(len(problems(rules(user={"default": "allow"}), {"pc": None})), 2)  # pc and here
        self.assertEqual(problems(rules(user={"default": "allow"}), {"pc": "manual", "here": "smart"}, only=["pc", "here"]), [])
        self.assertEqual(len(problems(rules(), {"pc": "off"}, only=["pc"])), 1)
        self.assertEqual(problems(rules(), {"sandbox": "off"}, only=["sandbox"]), [])

    def check(self, agent: AgentConfig, user: UserConfig, installation=None) -> None:
        check_approval_rules(agent, user, installation)

    def test_below_admin_a_blanket_on_a_machine_is_refused(self) -> None:
        user = user_config()
        for level in ("operator",):
            with self.subTest(level=level):
                with self.assertRaisesRegex(ApprovalRuleNotAllowed, r"level 'operator'.*backend 'pc' \(machine\).*admin"):
                    self.check(agent_config(backends=("pc",), level=level, approvals={"pc": "off"}), user)
                with self.assertRaises(ApprovalRuleNotAllowed):
                    self.check(agent_config(backends=("pc",), level=level, rules_={"rules": [rule("allow")]}), user)
                with self.assertRaises(ApprovalRuleNotAllowed):
                    self.check(agent_config(backends=("pc",), level=level), user_config(approval_rules={"default": "allow"}))
                with self.assertRaises(ApprovalRuleNotAllowed):
                    self.check(agent_config(backends=("pc",), level=level), user, ApprovalRuleList(default="allow"))

    def test_below_admin_a_sandbox_may_be_blanket_allowed(self) -> None:
        user = user_config()
        self.check(agent_config(level="operator", approvals={"sandbox": "off"}), user)
        self.check(agent_config(level="operator", rules_={"default": "allow", "rules": [rule("allow")]}), user)

    def test_narrow_rules_smart_and_manual_are_fine_on_a_machine(self) -> None:
        user = user_config(approval_rules={"rules": [rule("allow", command="ls", backend_kind="machine")]})
        self.check(agent_config(backends=("pc",), level="operator", approvals={"pc": "smart"},
                                rules_={"rules": [rule("allow", tool="file_read", path="/srv/*")]}), user)
        self.check(agent_config(backends=("pc",), level="operator", approvals={"pc": "manual"}), user)

    def test_admin_may_do_it(self) -> None:
        user = user_config()
        self.check(agent_config(backends=("pc", "here"), level="admin", approvals={"pc": "off", "here": "off"},
                                rules_={"rules": [rule("allow")]}), user)

    def test_only_the_agents_own_backends_count(self) -> None:
        user = user_config(approval_rules={"rules": [rule("allow", backend="pc")]})
        self.check(agent_config(backends=("sandbox",), level="operator"), user)

    def test_check_agent_level_runs_the_guardrail(self) -> None:
        user = user_config()
        config = agent_config(backends=("pc",), level="operator", approvals={"pc": "off"})
        with self.assertRaises(ApprovalRuleNotAllowed):
            check_agent_level(config, user, Level.ADMIN)
        check_agent_level(agent_config(backends=("pc",), level="admin", approvals={"pc": "off"}), user, Level.ADMIN)

    def test_agents_without_a_terminal_are_not_checked(self) -> None:
        user = user_config(approval_rules={"default": "allow"})
        config = AgentConfig.model_validate({"id": "bot", "name": "Bot", "chat_config": {"tool_groups": ["web"]}})
        self.check(config, user)


class ProductionShapeTests(unittest.TestCase):
    """sandbox smart, machine manual, local smart: the setup the project runs with."""

    def test_each_backend_keeps_its_own_mode(self) -> None:
        layers = RuleSet.build(ApprovalRuleList(default="ask"))
        shape = {"sandbox": ("smart", "sandbox"), "pc": ("manual", "machine"), "here": ("smart", "local")}
        for name, (mode, kind) in shape.items():
            with self.subTest(backend=name):
                normal = evaluate(layers, cmd("ls", kind=kind, backend=name), shorthand=mode)  # type: ignore[arg-type]
                risky = evaluate(layers, cmd("rm -rf x", kind=kind, backend=name, dangerous=True), shorthand=mode)  # type: ignore[arg-type]
                self.assertEqual(normal.verdict, "ask" if mode == "manual" else "allow")
                self.assertEqual(risky.verdict, "ask")

    def test_the_shape_is_a_valid_operator_configuration_except_local(self) -> None:
        user = user_config()
        check_approval_rules(
            agent_config(backends=("sandbox", "pc"), level="operator", approvals={"sandbox": "smart", "pc": "manual"}), user,
        )
        check_approval_rules(
            agent_config(backends=("sandbox", "pc", "here"), level="admin", approvals={"sandbox": "smart", "pc": "manual", "here": "smart"}),
            user,
        )


if __name__ == "__main__":
    unittest.main()
