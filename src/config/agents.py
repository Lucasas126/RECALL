from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Callable

import yaml
from pydantic import ValidationError

from src.config.levels import Level, check_agent_level, effective_agent_level, parse_level, required_level
from src.config.models import AgentConfig, UserConfig
from src.terminal.rules import ApprovalRule, ApprovalRuleList, rule_key
from src.utils.avatars import AvatarStore
from src.utils.files import atomic_write_text, exclusive_file_lock
from src.utils.i18n import UserMessageError

_AGENT_LIST_RE = re.compile(r"^agents\s*:")
_LIST_ITEM_RE = re.compile(r"^\s*-(\s|$)")  # indented or not: `yaml.dump` writes `- item` at the key's column
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class AgentConfigError(UserMessageError, ValueError):
    """An agent configuration could not be read, validated or saved."""


class AgentModelError(AgentConfigError):
    """An agent's default chat model cannot be used under the user's policy."""


class UnknownApprovalRule(AgentConfigError):
    """No agent approval rule at that position."""


class MaxLevelConflict(AgentConfigError):
    """A new `max_level` below the level of some of the user's agents (`agents`: id, name, level of each)."""

    code = "max_level_below_agents"

    def __init__(self, template: str, *, agents: list[dict], **values: object) -> None:
        super().__init__(template, **values)
        self.agents = agents


MAX_LEVEL_BELOW_AGENTS = (
    "The maximum level '{max_level}' is below the level of these agents of '{user_id}': {names}. "
    "Lower those agents (or remove the services that need the level) first, then lower the maximum."
)
_MAX_LEVEL_LINE = re.compile(r"^max_level\s*:.*$", re.MULTILINE)


class AgentConflict(AgentConfigError):
    """An agent id, file or data folder is already in use."""


class _Dumper(yaml.SafeDumper):
    pass


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    # multi-line prompts stay readable when edited by hand
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|" if "\n" in data else None)


_Dumper.add_representer(str, _represent_str)


class AgentStore:
    """Reads and writes a user's agent YAML files and the agent list in `user.yaml`.

    An agent is identified by the `id` field inside its file; the file name is only a convention.
    Renaming an agent moves its data folder under `data_dir`, so callers must release any runtime
    holding that folder first. Every mutation runs under a per-user lock and restores the previous
    files if a later step fails.
    """

    def __init__(self, config_dir: Path, data_dir: Path) -> None:
        self.config_dir = config_dir
        self.data_dir = data_dir

    # ------------------------------------------------------------------ reads

    def user(self, user_id: str) -> UserConfig:
        """Return a user's config; raises FileNotFoundError if the user does not exist."""
        return self._read_user(user_id)

    def list_agents(self, user_id: str) -> list[AgentConfig]:
        """Return the configs of the agents listed in the user's `user.yaml`, in that order."""
        return [self.load(user_id, agent_id) for agent_id in self._read_user(user_id).agents]

    def load(self, user_id: str, agent_id: str) -> AgentConfig:
        """Return one agent's config; raises FileNotFoundError if no file defines it."""
        return self._read(self.path_for(user_id, agent_id))

    def path_for(self, user_id: str, agent_id: str) -> Path:
        """Return the file defining `agent_id`; raises FileNotFoundError if none does."""
        matches: list[Path] = []
        first_error: Exception | None = None
        for path in self._agent_files(user_id):
            try:
                if self._read(path).id == agent_id:
                    matches.append(path)
            except (AgentConfigError, yaml.YAMLError) as exc:  # one broken file must not hide the valid agents
                first_error = first_error or exc
        if not matches:
            if first_error is not None:
                raise first_error
            raise FileNotFoundError(f"agent config not found: {agent_id}")
        if len(matches) > 1:
            raise AgentConflict("Several agent files define the id '{agent_id}'.", agent_id=agent_id)
        return matches[0]

    def fingerprint(self, user_id: str, agent_id: str) -> str:
        """Changes when `user.yaml` or the agent's file changes on disk (by any process), valid or not."""
        digest = hashlib.sha256()
        candidate = self._agents_dir(user_id) / f"{agent_id}.yaml"
        paths = [self._user_file(user_id), candidate]
        if not candidate.is_file():  # the file name is only a convention: find the file whose `id` matches
            for path in self._agent_files(user_id):
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, yaml.YAMLError):
                    continue
                if isinstance(data, dict) and data.get("id") == agent_id:
                    paths = [self._user_file(user_id), path]
                    break
        for path in paths:
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"\0missing")
            digest.update(b"\0")
        return digest.hexdigest()

    # ------------------------------------------------------------------ writes

    def create(self, user_id: str, config: AgentConfig) -> AgentConfig:
        with self._locked(user_id):
            user = self._read_user(user_id)
            self._check_level(user, config)
            self._ensure_free(user_id, config.id, user)
            path = self._agents_dir(user_id) / f"{config.id}.yaml"
            if path.exists():
                raise AgentConflict("The agent file '{file}' already exists.", file=path.name)
            self._write_agent(path, config)
            try:
                self._write_user_agents(user_id, [*user.agents, config.id])
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return config

    def update(self, user_id: str, agent_id: str, config: AgentConfig) -> AgentConfig:
        with self._locked(user_id):
            user = self._read_user(user_id)
            old_path = self.path_for(user_id, agent_id)
            keep_style = "avatar_style" not in config.model_fields_set
            if config.level is None or config.approval_rules is None or keep_style:
                # callers that do not know these fields (the web form) must not drop the stored level, the approval
                # rules (hand-written ones and the ones learned from approval cards) or the picture style;
                # `approval_rules: {rules: []}` clears
                stored = self._read(old_path)
                config = config.model_copy(update={
                    "level": stored.level if config.level is None else config.level,
                    "approval_rules": stored.approval_rules if config.approval_rules is None else config.approval_rules,
                    "avatar_style": stored.avatar_style if keep_style else config.avatar_style,
                })
            self._check_level(user, config)
            renamed = config.id != agent_id
            new_path = old_path
            if renamed:
                self._ensure_free(user_id, config.id, user)
                if old_path.stem == agent_id:
                    new_path = self._agents_dir(user_id) / f"{config.id}.yaml"
                    if new_path.exists():
                        raise AgentConflict("The agent file '{file}' already exists.", file=new_path.name)

            old_text = self._read_text(old_path)
            user_text = self._read_text(self._user_file(user_id)) if renamed else None
            moved = self._move_data(user_id, agent_id, config.id) if renamed else False
            try:
                self._write_agent(new_path, config)
                if new_path != old_path:
                    old_path.unlink()
                if renamed:
                    self._write_user_agents(user_id, [config.id if item == agent_id else item for item in user.agents])
                    # the profile picture lives beside the data folders, so it moves with the agent
                    AvatarStore(self.data_dir).move(user_id, agent_id, config.id)
            except BaseException:
                if new_path != old_path:
                    new_path.unlink(missing_ok=True)
                atomic_write_text(old_path, old_text)
                if user_text is not None:
                    atomic_write_text(self._user_file(user_id), user_text)
                if moved:
                    self._data_dir(user_id, config.id).rename(self._data_dir(user_id, agent_id))
                raise
            return config

    def delete(self, user_id: str, agent_id: str) -> None:
        """Remove the agent from the user and delete its config file; its data folder is kept."""
        with self._locked(user_id):
            user = self._read_user(user_id)
            path = self.path_for(user_id, agent_id)
            user_text = self._read_text(self._user_file(user_id))
            self._write_user_agents(user_id, [item for item in user.agents if item != agent_id])
            try:
                path.unlink()
            except OSError as exc:
                atomic_write_text(self._user_file(user_id), user_text)
                raise AgentConfigError(
                    "Could not delete the agent file '{file}'; check file permissions.", file=path.name,
                ) from exc

    def modify(
        self, user_id: str, agent_id: str, mutate: Callable[[AgentConfig, UserConfig], AgentConfig],
    ) -> tuple[AgentConfig, AgentConfig]:
        """Read-modify-write one agent under the user's lock; returns `(before, after)`.

        `mutate(config, user)` returns the new config (it may raise to abort, so a caller can re-check its own
        constraints against what is stored right now). The result is validated again as a whole, level and blanket-allow
        guardrail included (`check_agent_level`), before anything is written. The id cannot change here.
        """
        with self._locked(user_id):
            user = self._read_user(user_id)
            if agent_id not in user.agents:
                raise FileNotFoundError(f"agent config not found: {agent_id}")
            path = self.path_for(user_id, agent_id)
            before = self._read(path)
            changed = mutate(before, user)
            try:  # a whole new validation: model_copy skips the validators
                after = AgentConfig.model_validate(changed.model_dump(mode="json", exclude_none=True))
            except ValidationError as exc:
                raise AgentConfigError(
                    "Invalid agent configuration '{file}': {details}",
                    file=path.name, details="; ".join(error["msg"] for error in exc.errors()),
                ) from exc
            if after.id != before.id:
                raise AgentConfigError("The id of an agent cannot change here.")
            if after == before:
                return before, before
            self._check_level(user, after)
            self._write_agent(path, after)
            return before, after

    # ------------------------------------------------------------------ agent approval rules

    def approval_rules(self, user_id: str, agent_id: str) -> list[ApprovalRule]:
        """The rules of the agent's own `approval_rules` list, in order (empty when it has none)."""
        rules = self.load(user_id, agent_id).approval_rules
        return list(rules.rules) if rules is not None else []

    def add_approval_rules(self, user_id: str, agent_id: str, rules: list[ApprovalRule]) -> list[ApprovalRule]:
        """Append rules that the agent's list does not hold yet; returns the ones really added.

        Same path as an edit from the web form: under the user's lock, the whole agent is validated again, level and
        blanket-allow guardrail included (`check_agent_level`). Nothing is written when anything fails.
        """
        with self._locked(user_id):
            user = self._read_user(user_id)
            path = self.path_for(user_id, agent_id)
            config = self._read(path)
            current = config.approval_rules or ApprovalRuleList()
            known = {rule_key(rule) for rule in current.rules}
            added: list[ApprovalRule] = []
            for rule in rules:
                if rule_key(rule) not in known:
                    known.add(rule_key(rule))
                    added.append(rule)
            if not added:
                return []
            updated = self._with_rules(config, current, [*current.rules, *added])
            self._check_level(user, updated)
            self._write_agent(path, updated)
            return added

    def remove_approval_rule(self, user_id: str, agent_id: str, index: int) -> ApprovalRule:
        """Remove the rule at `index` of the agent's list (a removal can only narrow what is allowed, but may lift a deny)."""
        with self._locked(user_id):
            self._read_user(user_id)
            path = self.path_for(user_id, agent_id)
            config = self._read(path)
            current = config.approval_rules or ApprovalRuleList()
            if not 0 <= index < len(current.rules):
                raise UnknownApprovalRule("There is no approval rule number {index} on this agent.", index=index)
            rules = list(current.rules)
            removed = rules.pop(index)
            self._write_agent(path, self._with_rules(config, current, rules))
            return removed

    def clear_approval_rules(self, user_id: str, agent_id: str) -> list[ApprovalRule]:
        """Remove every rule of the agent's list (its `default`, if any, stays); returns what was removed."""
        with self._locked(user_id):
            self._read_user(user_id)
            path = self.path_for(user_id, agent_id)
            config = self._read(path)
            current = config.approval_rules or ApprovalRuleList()
            if not current.rules:
                return []
            self._write_agent(path, self._with_rules(config, current, []))
            return list(current.rules)

    @staticmethod
    def _with_rules(config: AgentConfig, current: ApprovalRuleList, rules: list[ApprovalRule]) -> AgentConfig:
        try:
            replaced = ApprovalRuleList(default=current.default, rules=rules)
        except ValidationError as exc:
            raise AgentConfigError(
                "Invalid approval rules: {details}", details="; ".join(error["msg"] for error in exc.errors()),
            ) from exc
        if not replaced.rules and replaced.default is None:
            replaced = None
        return config.model_copy(update={"approval_rules": replaced})

    # ------------------------------------------------------------------ helpers

    def _user_dir(self, user_id: str) -> Path:
        if not _ID_RE.match(user_id):
            raise ValueError(f"invalid user id: {user_id!r}")
        return self.config_dir / user_id

    def _user_file(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "user.yaml"

    def _agents_dir(self, user_id: str) -> Path:
        return self._user_dir(user_id) / "agents"

    def _data_dir(self, user_id: str, agent_id: str) -> Path:
        return self.data_dir / user_id / agent_id

    def _locked(self, user_id: str):
        return exclusive_file_lock(self._user_dir(user_id) / "agents.lock")

    def _agent_files(self, user_id: str) -> list[Path]:
        agents_dir = self._agents_dir(user_id)
        return sorted(agents_dir.glob("*.yaml")) if agents_dir.is_dir() else []

    def agent_levels(self, user_id: str, user: UserConfig | None = None) -> list[dict]:
        """`{id, name, level, declared}` of each agent that loads: `level` is the higher of the declared and the required one."""
        user = user or self._read_user(user_id)
        rows = []
        for agent_id in user.agents:
            try:
                config = self.load(user_id, agent_id)
            except (FileNotFoundError, AgentConfigError, yaml.YAMLError):
                continue  # an unreadable agent has no level to compare
            level = max(effective_agent_level(config, user), required_level(config, user.terminal_backends))
            rows.append({"id": config.id, "name": config.name, "level": level.label, "declared": config.level is not None})
        return rows

    def set_max_level(self, user_id: str, new_max: Level) -> Level:
        """Set the user's `max_level` (the administrator's job, see `ChatService.set_user_max_level`); returns the old one.

        Under the user's lock. Refuses with MaxLevelConflict when an agent's level (declared or required) is above
        `new_max`, and rewrites only the `max_level` line of `user.yaml`, so comments and other keys survive.
        """
        with self._locked(user_id):
            user = self._read_user(user_id)
            above = [row for row in self.agent_levels(user_id, user) if parse_level(row["level"]) > new_max]
            if above:
                raise MaxLevelConflict(
                    MAX_LEVEL_BELOW_AGENTS, agents=above, max_level=new_max.label, user_id=user_id,
                    names=", ".join(f"{row['id']} ({row['level']})" for row in above),
                )
            path = self._user_file(user_id)
            text = self._read_text(path)
            line = f"max_level: {new_max.label}"
            updated = _MAX_LEVEL_LINE.sub(line, text, count=1) if _MAX_LEVEL_LINE.search(text) else f"{text.rstrip()}\n{line}\n"
            try:
                checked = UserConfig.model_validate(yaml.safe_load(updated))
            except ValidationError as exc:
                raise AgentConfigError("Invalid user configuration '{file}'.", file=path.name) from exc
            if checked.max_level != new_max:
                raise AgentConfigError("Invalid user configuration '{file}'.", file=path.name)
            try:
                atomic_write_text(path, updated)
            except OSError as exc:
                raise AgentConfigError(
                    "Could not save the user configuration '{file}'; check file permissions.", file=path.name,
                ) from exc
            return user.max_level

    def _check_level(self, user: UserConfig, config: AgentConfig) -> None:
        """The config rule: required level <= the agent's effective level <= the owner's max_level, else it is refused."""
        check_agent_level(config, user, user.max_level)

    def _ensure_free(self, user_id: str, agent_id: str, user: UserConfig) -> None:
        if agent_id in user.agents or self._is_defined(user_id, agent_id):
            raise AgentConflict("An agent with the id '{agent_id}' already exists.", agent_id=agent_id)

    def _is_defined(self, user_id: str, agent_id: str) -> bool:
        try:
            self.path_for(user_id, agent_id)
        except FileNotFoundError:
            return False
        return True

    def _move_data(self, user_id: str, old_id: str, new_id: str) -> bool:
        source = self._data_dir(user_id, old_id)
        target = self._data_dir(user_id, new_id)
        if not source.exists():
            return False
        if target.exists():
            raise AgentConflict("Data already exists for the agent id '{agent_id}'.", agent_id=new_id)
        try:
            source.rename(target)
        except OSError as exc:
            raise AgentConfigError(
                "Could not move the agent data; close other programs using this agent's files and try again.",
            ) from exc
        return True

    @staticmethod
    def _read_text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AgentConfigError("Could not read the configuration file '{file}'.", file=path.name) from exc

    def _load_yaml(self, path: Path) -> object:
        return yaml.safe_load(self._read_text(path))

    def _read_user(self, user_id: str) -> UserConfig:
        path = self._user_file(user_id)
        try:
            return UserConfig.model_validate(self._load_yaml(path))
        except ValidationError as exc:
            raise AgentConfigError(
                "Invalid user configuration '{file}': {details}",
                file=path.name, details="; ".join(error["msg"] for error in exc.errors()),
            ) from exc

    def _read(self, path: Path) -> AgentConfig:
        try:
            return AgentConfig.model_validate(self._load_yaml(path))
        except ValidationError as exc:
            raise AgentConfigError(
                "Invalid agent configuration '{file}': {details}",
                file=path.name, details="; ".join(error["msg"] for error in exc.errors()),
            ) from exc

    def _write_agent(self, path: Path, config: AgentConfig) -> None:
        data = config.model_dump(mode="json", exclude_none=True)
        if data.get("avatar_style") == "circle":
            del data["avatar_style"]  # the default is not written
        text = yaml.dump(
            data,
            Dumper=_Dumper, sort_keys=False, allow_unicode=True,
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, text)
        except OSError as exc:
            raise AgentConfigError(
                "Could not save the agent file '{file}'; check file permissions and whether another process is using it.",
                file=path.name,
            ) from exc

    def _write_user_agents(self, user_id: str, agent_ids: list[str]) -> None:
        """Replace only the `agents` entry of `user.yaml` so comments and other keys survive."""
        path = self._user_file(user_id)
        lines = self._read_text(path).splitlines(keepends=True)
        start = next((index for index, line in enumerate(lines) if _AGENT_LIST_RE.match(line)), None)
        if start is None:
            raise AgentConfigError("The user configuration '{file}' has no agents list.", file=path.name)
        end = start + 1
        while end < len(lines) and _LIST_ITEM_RE.match(lines[end]):
            end += 1
        lines[start:end] = [f"agents: [{', '.join(_yaml_scalar(item) for item in agent_ids)}]\n"]
        text = "".join(lines)
        try:
            UserConfig.model_validate(yaml.safe_load(text))
        except ValidationError as exc:
            raise AgentConfigError("Invalid user configuration '{file}'.", file=path.name) from exc
        try:
            atomic_write_text(path, text)
        except OSError as exc:
            raise AgentConfigError(
                "Could not save the user configuration '{file}'; check file permissions.", file=path.name,
            ) from exc


def _yaml_scalar(value: str) -> str:
    """Quote an id only when YAML would read it as another type (e.g. `yes` or `2025`)."""
    return value if yaml.safe_load(value) == value else json.dumps(value)
