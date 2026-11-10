"""Approval rules: layered allow / ask / deny lists and the pure evaluation that turns an action into a verdict.

Design: docs/architecture/permissions.md ("Approval rules"). Nothing here touches I/O, the broker or the session:
`evaluate` takes the rule layers and a description of one action and returns a `Decision`; `TerminalSession.decide`
and the file tools do the asking, the audit and the execution.

Layers, least to most specific: `installation` (settings.yaml), `user` (user.yaml), `agent` (agent.yaml). Every layer is
an `ApprovalRuleList`: an optional `default` verdict and an ordered list of rules. Resolution:

1. every rule of every layer that matches is collected; the worst verdict wins (deny > ask > allow);
2. no rule matches: the legacy per-backend shorthand (`approvals: manual|smart|off`) when the agent sets one, else the
   `default` of the most specific layer that defines one, else `ask`;
3. a terminal command is judged segment by segment (`command_units`) and takes the worst verdict of its segments;
4. nobody attached (dreams, tasks): every `ask` becomes `deny`.

The fixed floor (hardline and protected paths) is checked by the caller BEFORE this module and cannot be changed by rules.
"""

from __future__ import annotations

import fnmatch
import posixpath
from dataclasses import dataclass
from typing import Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.terminal.policy import command_units

Verdict = Literal["allow", "ask", "deny"]
ToolKind = Literal["terminal", "file_read", "file_write", "any"]
BackendKind = Literal["sandbox", "machine", "local"]
Shorthand = Literal["manual", "smart", "off"]

LAYERS: tuple[str, ...] = ("installation", "user", "agent")  # least to most specific
_RANK: dict[str, int] = {"allow": 0, "ask": 1, "deny": 2}
_REAL_KINDS = ("machine", "local")  # kinds where a blanket allow is admin-only
_BLANKET_TOOLS = ("terminal", "file_read", "file_write", "any")  # tools that run on a backend
_ID = r"^[a-z][a-z0-9_-]{0,31}$"
_BACKEND_GLOB = r"^[a-z0-9_*?\[\]-]{1,64}$"


# ---------------------------------------------------------------------- config models
class ApprovalRule(BaseModel):
    """One rule. All the match fields that are set must match; `tool: any` matches every kind of action."""

    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    tool: ToolKind = "any"
    id: Optional[str] = Field(default=None, pattern=_ID)  # a stable name for the audit trail instead of the index
    command: Optional[str] = Field(default=None, min_length=1, max_length=500)  # glob or word prefix, per command segment
    path: Optional[str] = Field(default=None, min_length=1, max_length=1024)  # glob against the resolved target path
    backend: Optional[str] = Field(default=None, pattern=_BACKEND_GLOB)  # backend name (glob)
    backend_kind: Optional[BackendKind] = None
    dangerous: Optional[bool] = Field(default=None, strict=True)  # matches when the policy assesses the action as dangerous
    exact: Optional[bool] = Field(default=None, strict=True)  # `command` / `path` are compared literally (no prefix, no glob)
    reason: Optional[str] = Field(default=None, min_length=1, max_length=200)  # shown on the approval card

    @field_validator("command", "path")
    @classmethod
    def _clean(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not value.strip() or "\x00" in value:
            raise ValueError("must be a non-empty string without NUL bytes")
        return value.strip()

    @model_validator(mode="after")
    def _fields_fit_the_tool(self) -> "ApprovalRule":
        if self.command is not None and self.tool in ("file_read", "file_write"):
            raise ValueError(f"'command' does not apply to tool '{self.tool}': use 'path'")
        if self.path is not None and self.tool == "terminal":
            raise ValueError("'path' does not apply to tool 'terminal': use 'command'")
        if self.exact and self.command is None and self.path is None:
            raise ValueError("'exact' needs a 'command' or a 'path'")
        return self

    @property
    def blanket(self) -> bool:
        """True when nothing narrows the rule below 'every action of this tool' (backend and kind only choose where)."""
        return self.command is None and self.path is None and self.dangerous is None


class ApprovalRuleList(BaseModel):
    """The rules of one layer (`approval_rules:` in settings.yaml, user.yaml or an agent file)."""

    model_config = ConfigDict(extra="forbid")

    default: Optional[Verdict] = None
    rules: list[ApprovalRule] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _unique_ids(self) -> "ApprovalRuleList":
        seen: set[str] = set()
        for rule in self.rules:
            if rule.id is not None:
                if rule.id in seen:
                    raise ValueError(f"rule id '{rule.id}' is used twice in the same list")
                seen.add(rule.id)
        return self


# ---------------------------------------------------------------------- rule sets
@dataclass(frozen=True)
class RuleLayer:
    name: str
    rules: tuple[ApprovalRule, ...] = ()
    default: Optional[Verdict] = None

    def label(self, index: int) -> str:
        rule = self.rules[index]
        return f"{self.name}:{rule.id if rule.id else index}"


@dataclass(frozen=True)
class RuleSet:
    """The three layers, least specific first. Immutable: `with_rule` returns a new set (step 4 appends agent rules)."""

    layers: tuple[RuleLayer, ...] = tuple(RuleLayer(name) for name in LAYERS)

    @classmethod
    def build(cls, installation: ApprovalRuleList | None = None, user: ApprovalRuleList | None = None,
              agent: ApprovalRuleList | None = None) -> "RuleSet":
        given = {"installation": installation, "user": user, "agent": agent}
        layers = []
        for name in LAYERS:
            lst = given[name]
            layers.append(RuleLayer(name, tuple(lst.rules) if lst is not None else (), lst.default if lst is not None else None))
        return cls(tuple(layers))

    def layer(self, name: str) -> RuleLayer:
        for layer in self.layers:
            if layer.name == name:
                return layer
        raise KeyError(name)

    def with_rule(self, layer: str, rule: ApprovalRule) -> "RuleSet":
        """A copy with `rule` appended to `layer` (the append-only way an 'always allow' will add narrow rules)."""
        self.layer(layer)
        return RuleSet(tuple(
            RuleLayer(item.name, (*item.rules, rule), item.default) if item.name == layer else item for item in self.layers
        ))

    def default_layer(self) -> RuleLayer | None:
        """The most specific layer that defines a `default`."""
        return next((layer for layer in reversed(self.layers) if layer.default is not None), None)


# ---------------------------------------------------------------------- actions and decisions
@dataclass(frozen=True)
class Action:
    """What is about to happen, as the rules see it.

    `command` is the shell command line (terminal actions only); `path` is the resolved target of a file tool;
    `dangerous` is the policy assessment. Folder writes and MCP calls are not routed through the rules yet (backlog).
    """

    tool: Literal["terminal", "file_read", "file_write"]
    command: str = ""
    path: str = ""
    backend: str = ""
    backend_kind: str = ""
    dangerous: bool = False
    danger_reason: str = ""

    def units(self) -> list[str]:
        if self.tool == "terminal":
            return command_units(self.command)
        return []


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    layer: str | None = None  # installation | user | agent | builtin (the fallback when no layer defines a default)
    rule: str | None = None  # 'agent:2' / 'user:my-id', 'approvals:smart' for the shorthand, None for a layer default
    source: Literal["rule", "shorthand", "default", "unattended"] = "default"
    reason: str = ""  # one short line for the approval card and the model
    asked: Verdict | None = None  # the verdict before the unattended override (`ask` when it became `deny`)
    segment: str = ""  # the command segment that set the verdict (multi-segment commands)

    @property
    def label(self) -> str:
        """Short audit label: the rule, else the layer default, else the fallback."""
        if self.rule:
            return self.rule
        return f"{self.layer}:default" if self.layer else "default"


def glob_match(pattern: str, text: str) -> bool:
    """A word prefix when `pattern` has no glob characters (`git status` matches `git status -s`), else a full fnmatch."""
    if not any(char in pattern for char in "*?["):
        return text == pattern or text.startswith(pattern + " ")
    return fnmatch.fnmatchcase(text, pattern)


def _norm_path(path: str) -> str:
    return posixpath.normpath(path) if path.startswith("/") else path


def rule_matches(rule: ApprovalRule, action: Action, unit: str | None) -> bool:
    if rule.tool not in ("any", action.tool):
        return False
    if rule.backend_kind is not None and rule.backend_kind != action.backend_kind:
        return False
    if rule.backend is not None and not (action.backend and fnmatch.fnmatchcase(action.backend, rule.backend)):
        return False
    if rule.dangerous is not None and rule.dangerous != action.dangerous:
        return False
    if rule.path is not None:
        if not action.path:
            return False
        if rule.exact:
            if _norm_path(action.path) != _norm_path(rule.path):
                return False
        elif not fnmatch.fnmatchcase(_norm_path(action.path), rule.path):
            return False
    if rule.command is not None:
        if unit is None:
            return False
        if rule.exact:
            if unit != rule.command:
                return False
        elif not glob_match(rule.command, unit):
            return False
    return True


def _worse(a: Decision, b: Decision) -> Decision:
    return b if _RANK[b.verdict] > _RANK[a.verdict] else a


def _rule_reason(rule: ApprovalRule, label: str) -> str:
    return f"rule {label}" + (f": {rule.reason}" if rule.reason else "")


def _judge(rules: RuleSet, action: Action, unit: str | None, shorthand: Shorthand | None, attended: bool) -> Decision:
    """The decision for one unit of the action (a command segment, or the whole non-terminal action)."""
    best: Decision | None = None
    for layer in reversed(rules.layers):  # on a tie the most specific layer, then the first rule, is the one named
        for index, rule in enumerate(layer.rules):
            if not rule_matches(rule, action, unit):
                continue
            label = layer.label(index)
            found = Decision(rule.verdict, layer.name, label, "rule", _rule_reason(rule, label), segment=unit or "")
            if best is None or _RANK[found.verdict] > _RANK[best.verdict]:
                best = found
    if best is not None:
        return best
    if shorthand is not None:  # the agent's per-backend mode: the most specific default for that backend
        label = f"approvals:{shorthand}"
        if shorthand == "manual":
            return Decision("ask", "agent", label, "shorthand", "approvals are set to manual", segment=unit or "")
        if shorthand == "smart":
            if action.dangerous:
                return Decision("ask", "agent", label, "shorthand", action.danger_reason or "dangerous", segment=unit or "")
            return Decision("allow", "agent", label, "shorthand", "", segment=unit or "")
        # `off` must not widen what runs when nobody can answer: dangerous actions and file writes still ask (-> deny)
        if not attended and (action.dangerous or action.tool == "file_write"):
            what = f"dangerous ({action.danger_reason})" if action.dangerous else "a file write"
            return Decision("ask", "agent", label, "shorthand", f"this action is {what}", segment=unit or "")
        return Decision("allow", "agent", label, "shorthand", "", segment=unit or "")
    layer = rules.default_layer()
    if layer is None:
        return Decision("ask", "builtin", None, "default", "no approval rule allows this action", segment=unit or "")
    verdict: Verdict = layer.default  # type: ignore[assignment]  # default_layer() only returns layers that define one
    return Decision(verdict, layer.name, None, "default", f"the {layer.name} default is {verdict}", segment=unit or "")


def evaluate(
    rules: RuleSet, action: Action, *, shorthand: Shorthand | None = None, attended: bool = True,
) -> Decision:
    """The verdict for `action`: (verdict, layer, rule, reason) as a `Decision`. Pure; see the module docstring."""
    units: list[str | None] = list(action.units()) or [None]
    result: Decision | None = None
    for unit in units:
        decision = _judge(rules, action, unit, shorthand, attended)
        result = decision if result is None else _worse(result, decision)
    assert result is not None
    if len(units) > 1 and result.segment:
        result = Decision(**{**result.__dict__, "reason": f"{result.reason} (segment: {result.segment[:80]})".strip()})
    if result.verdict == "ask" and not attended:
        return Decision(
            "deny", result.layer, result.rule, "unattended", result.reason, asked="ask", segment=result.segment,
        )
    return result


# ---------------------------------------------------------------------- learned rules (approve "always for this agent")
LEARNED_REASON = "approved always for this agent"  # the `reason` of every rule written from an approval card
SCOPES: tuple[str, ...] = ("once", "session", "agent")  # what an approval may be remembered for (deny has no scope)


class NoNarrowRule(ValueError):
    """The action cannot be turned into a narrow, exact allow rule (so 'always for this agent' is not offered)."""


def rules_for_action(action: Action) -> list[ApprovalRule]:
    """The narrow allow rules that cover exactly `action`, never a wildcard or a prefix.

    A terminal command gets one rule per segment (`command_units`), each matching that segment's exact text on the
    action's backend; a file operation gets one rule for its exact path and tool kind on the action's backend.
    Raises `NoNarrowRule` when nothing narrow can be written (no backend name, empty command or path, a segment longer
    than a rule may be).
    """
    if not action.backend:
        raise NoNarrowRule("the action has no backend name")
    try:
        if action.tool == "terminal":
            units = action.units()
            if not units:
                raise NoNarrowRule("the command has no segment to match")
            rules = [
                ApprovalRule(verdict="allow", tool="terminal", command=unit, exact=True, backend=action.backend, reason=LEARNED_REASON)
                for unit in dict.fromkeys(units)
            ]
        else:
            if not action.path:
                raise NoNarrowRule("the action has no path")
            rules = [ApprovalRule(
                verdict="allow", tool=action.tool, path=action.path, exact=True, backend=action.backend, reason=LEARNED_REASON,
            )]
    except ValueError as exc:  # pydantic's ValidationError is a ValueError: too long, odd backend name, NUL byte...
        if isinstance(exc, NoNarrowRule):
            raise
        raise NoNarrowRule(str(exc)) from exc
    from src.terminal.output import redact  # lazily: the config models import this module

    if any(rule.command is not None and redact(rule.command) != rule.command for rule in rules):
        raise NoNarrowRule("the command looks like it contains a secret, which must not be written to the configuration")
    if any(rule.blanket for rule in rules):  # cannot happen (command or path is set); kept as the invariant
        raise NoNarrowRule("the rule would allow everything")
    return rules


def rule_for_action(action: Action) -> ApprovalRule:
    """The single narrow allow rule of an action with one segment (any file operation, a one-segment command)."""
    rules = rules_for_action(action)
    if len(rules) != 1:
        raise NoNarrowRule("the command has several segments: use rules_for_action")
    return rules[0]


def rule_key(rule: ApprovalRule) -> str:
    """A canonical text of a rule's content (identical rules, however written, have the same key)."""
    return rule.model_dump_json(exclude_none=True)


def action_signature(action: Action) -> str:
    """The identity of an action for a session grant: tool kind, backend and the exact command or path."""
    target = action.command if action.tool == "terminal" else action.path
    return f"{action.tool}\x1f{action.backend}\x1f{target}"


def offered_scopes(rules: RuleSet, action: Action, *, shorthand: Shorthand | None, decision: Decision) -> tuple[str, ...]:
    """The approve scopes the card may offer for an `ask` decision (always starts with `once`).

    `session` and `agent` are not offered for a dangerous action, nor when the decision is not a plain `ask`, nor when
    the action has no narrow rule. `agent` is offered only when appending that rule would really allow the action
    (an `ask` rule of any layer still beats an allow rule, so the rule would never take effect).
    """
    scopes = ["once"]
    if action.dangerous or decision.verdict != "ask" or not action.backend:
        return tuple(scopes)
    if not (action.command if action.tool == "terminal" else action.path):
        return tuple(scopes)
    scopes.append("session")
    try:
        learned = rules_for_action(action)
    except NoNarrowRule:
        return tuple(scopes)
    extended = rules
    for rule in learned:
        extended = extended.with_rule("agent", rule)
    if evaluate(extended, action, shorthand=shorthand, attended=True).verdict == "allow":
        scopes.append("agent")
    return tuple(scopes)


# ---------------------------------------------------------------------- guardrail
def blanket_allows(
    rules: RuleSet, backends: Mapping[str, str], shorthands: Mapping[str, Shorthand | None] | None = None,
) -> list[str]:
    """What allows EVERYTHING on a machine or local backend of the agent: one readable line each (empty = fine).

    `backends` maps the agent's backend names to their kind (`sandbox` | `machine` | `local`); `shorthands` maps them
    to the explicit `approvals` mode, if any. A blanket allow is a rule that allows without a `command`, `path` or
    `dangerous` condition, a `default: allow` that applies to the backend, or the shorthand `off`. On a sandbox these
    are fine; on a real machine they are admin-only (the caller compares the level).
    """
    shorthands = shorthands or {}
    problems: list[str] = []
    for name, kind in backends.items():
        if kind not in _REAL_KINDS:
            continue
        mode = shorthands.get(name)
        if mode == "off":
            problems.append(f"backend '{name}' ({kind}) has approvals: off")
        elif mode is None:
            layer = rules.default_layer()
            if layer is not None and layer.default == "allow":
                problems.append(f"the {layer.name} approval default 'allow' applies to backend '{name}' ({kind})")
        for layer in rules.layers:
            for index, rule in enumerate(layer.rules):
                if rule.verdict != "allow" or not rule.blanket or rule.tool not in _BLANKET_TOOLS:
                    continue
                if rule.backend_kind not in (None, kind) or (rule.backend is not None and not fnmatch.fnmatchcase(name, rule.backend)):
                    continue
                problems.append(f"rule {layer.label(index)} allows everything on backend '{name}' ({kind})")
    return list(dict.fromkeys(problems))
