"""Move an agent between owners: share (user to space) and unshare (space to its members).

Design: docs/project/design-shared-agents.md ("Moving an agent between owners"). What moves: the agent yaml, the data
folder `data/<owner>/<id>/` (without `.index`, which stores absolute paths: it is rebuilt on the next start or by
`reindex`), the avatar, the automations of the agent and the entry in the owner's agent list.

Safety: the data is copied into a temporary folder beside the target and renamed at the end; the source is removed only
after every target is complete. A failure removes what was made for the targets and leaves the source untouched.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
from pathlib import Path
from typing import Callable

from src.agent.user_sections import parse_sections, render_sections
from src.config.agents import AgentConfigError, AgentStore
from src.config.levels import Level, required_level, required_level_reason
from src.config.models import AgentConfig, UserConfig
from src.config.spaces import SpaceError, agent_id_conflict, all_spaces, resolve_space
from src.config.yaml_files import load_yaml_file
from src.core.sources.agent_files import MEMORY_FILES, join_memory, split_memory
from src.terminal.audit import write_audit
from src.utils.avatars import AvatarStore
from src.utils.files import atomic_write_text
from src.utils.i18n import InputError

logger = logging.getLogger(__name__)
AUDIT_FILE = "admin_audit.jsonl"
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_USER_MD = MEMORY_FILES["user"]
_SKIP = {".index"}


class MoveError(InputError):
    """The move was refused or failed; the source is untouched."""


class AgentMover:
    """Share and unshare agents for one configuration and data directory."""

    def __init__(self, config_dir: Path, data_dir: Path, *, create_space: Callable[[str, list[str], str | None], str],
                 is_busy: Callable[[str, str], bool] = lambda owner, agent: False,
                 on_removed: Callable[[str, str], None] = lambda owner, agent: None,
                 on_created: Callable[[str, str], None] = lambda owner, agent: None) -> None:
        """`is_busy(owner, agent)` is the live busy check; `on_removed` and `on_created` let the service drop its slot and
        preferences for the old owner and rebuild the index of each new one (best effort, after the move succeeded)."""
        self.config_dir, self.data_dir = Path(config_dir), Path(data_dir)
        self._create_space, self._is_busy = create_space, is_busy
        self._on_removed, self._on_created = on_removed, on_created
        self.store = AgentStore(self.config_dir, self.data_dir)
        self.avatars = AvatarStore(self.data_dir)

    # ------------------------------------------------------------------ public

    def share_agent(self, owner_user: str, agent_id: str, members: list[str]) -> str:
        """Move `owner_user`'s agent into the space of exactly {owner_user} + members (created when missing); returns the space id."""
        everyone = [owner_user, *[m for m in dict.fromkeys(members) if m != owner_user]]
        if len(everyone) < 2:
            raise MoveError("share needs at least one other member")
        user = self._owner(owner_user, "user")
        if agent_id not in user.agents:
            raise MoveError("agent '{agent_id}' is not owned by '{user_id}'", agent_id=agent_id, user_id=owner_user)
        for member in everyone[1:]:
            self._owner(member, "user")
        config = self.store.load(owner_user, agent_id)
        self._check_idle(owner_user, agent_id)
        space = next((s for s in all_spaces(self.config_dir) if set(s.members) == set(everyone)), None)
        for member in everyone:  # the id must be free for every member (their agents and their other spaces)
            clash = agent_id_conflict(member, agent_id, self.config_dir, ignore=owner_user if member == owner_user else None)
            if clash is not None:
                raise MoveError("agent '{agent_id}' clashes with an agent of '{owner}'", agent_id=agent_id, owner=clash)
        backends = (self.store.user(space.id) if space else user).terminal_backends
        self._check_fits(config, {m: self._owner(m, "user").max_level for m in everyone}, backends, "the space")
        created = space is None
        space_id = space.id if space else self._create_space(owner_user, everyone[1:], None)
        try:
            self._transfer(owner_user, space_id, config, copy=False, paused=False, user_md=self._share_user_md(owner_user, config))
        except BaseException:
            if created:
                shutil.rmtree(self.config_dir / space_id, ignore_errors=True)
            raise
        dropped = self._drop_targets(owner_user, drop=agent_id)
        dropped += self._drop_targets(space_id, only=agent_id)
        self._audit(owner_user, "share_agent", agent=agent_id, space=space_id, members=everyone, dropped_targets=dropped)
        self._safely(self._on_removed, owner_user, agent_id)
        self._safely(self._on_created, space_id, agent_id)
        self._audit(space_id, "share_agent", agent=agent_id, from_user=owner_user, members=everyone)
        return space_id

    def unshare_agent(self, space_id: str, agent_id: str, actor: str) -> dict[str, str]:
        """Give the agent back: moved to the only member, else copied to each member (paused automations); returns member -> new id."""
        space = self._owner(space_id, "space")
        if actor not in space.members:
            raise MoveError("'{actor}' is not a member of {space}", actor=actor, space=space_id)
        if agent_id not in space.agents:
            raise MoveError("agent '{agent_id}' is not owned by '{user_id}'", agent_id=agent_id, user_id=space_id)
        config = self.store.load(space_id, agent_id)
        self._check_idle(space_id, agent_id)
        single = len(space.members) == 1
        sections = parse_sections(self._read_user_md(space_id, agent_id))
        plan = {}
        for member in space.members:
            self._owner(member, "user")
            new_id = agent_id if single else self._free_id(member, agent_id, space_id)
            plan[member] = new_id
        for member in space.members:
            self._check_fits(config, {member: self.store.user(member).max_level}, space.terminal_backends, f"the account of '{member}'")
        done: list[tuple[str, str]] = []
        try:
            for member, new_id in plan.items():
                text = join_memory(self._entries(sections, member)) if not single else self._flat(sections)
                self._transfer(space_id, member, config, copy=True, paused=not single, user_md=text, new_id=new_id)
                done.append((member, new_id))
        except BaseException:
            for member, new_id in done:
                self._undo_target(member, new_id)
            raise
        self._remove_source(space_id, agent_id)
        remaining = self.store.user(space_id).agents
        dropped = self._drop_targets(space_id, drop=agent_id) if remaining else []
        if not remaining:
            shutil.rmtree(self.config_dir / space_id, ignore_errors=True)  # its data folder stays
        for member, new_id in plan.items():
            self._drop_targets(member, only=new_id)
            self._audit(member, "unshare_agent", agent=agent_id, new_id=new_id, space=space_id, actor=actor)
            self._safely(self._on_created, member, new_id)
        self._safely(self._on_removed, space_id, agent_id)
        self._audit(space_id, "unshare_agent", agent=agent_id, actor=actor, members=list(space.members), dropped_targets=dropped)
        return plan

    # ------------------------------------------------------------------ steps

    def _transfer(self, source: str, target: str, config: AgentConfig, *, copy: bool, paused: bool, user_md: str,
                  new_id: str | None = None) -> None:
        """Make the agent exist for `target` (config, data, avatar, automations). `copy=False` also removes the source."""
        new_id = new_id or config.id
        target_user = self._target_user(target)
        moved = self._fit_level(config.model_copy(update={"id": new_id}), target_user)
        data_target = self.data_dir / target / new_id
        if data_target.exists():
            raise MoveError("data already exists for agent '{agent_id}' of '{owner}'", agent_id=new_id, owner=target)
        tmp = self.data_dir / target / f".move-{secrets.token_hex(4)}"
        made_config = made_avatar = False
        automation_paths: list[Path] = []
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            origin = self.data_dir / source / config.id
            if origin.is_dir():
                shutil.copytree(origin, tmp, ignore=lambda folder, names: [n for n in names if n in _SKIP and Path(folder) == origin])
            else:
                tmp.mkdir()
            (tmp / _USER_MD).parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(tmp / _USER_MD, user_md)
            self.store.adopt(target, moved)
            made_config = True
            tmp.rename(data_target)
            avatar = self.avatars.get(source, config.id)
            if avatar is not None:
                self.avatars.save(target, new_id, self.avatars.read(avatar) or b"", avatar.media_type)
                made_avatar = True
            automation_paths = self._copy_automations(source, target, config.id, new_id, paused=paused)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            if made_config:
                shutil.rmtree(data_target, ignore_errors=True)
                self._undo_target(target, new_id, data=False)
            for path in automation_paths:
                path.unlink(missing_ok=True)
            if made_avatar:
                self.avatars.delete(target, new_id)
            raise
        if not copy:
            self._remove_source(source, config.id)

    def _remove_source(self, owner: str, agent_id: str) -> None:
        """Remove the agent of `owner`: config and list entry, data folder, picture, automations."""
        self.store.delete(owner, agent_id)
        shutil.rmtree(self.data_dir / owner / agent_id, ignore_errors=True)
        self.avatars.delete(owner, agent_id)
        for path in self._automation_files(owner, agent_id):
            path.unlink(missing_ok=True)

    def _undo_target(self, owner: str, agent_id: str, *, data: bool = True) -> None:
        try:
            self.store.delete(owner, agent_id)
        except (FileNotFoundError, AgentConfigError):
            pass
        if data:
            shutil.rmtree(self.data_dir / owner / agent_id, ignore_errors=True)
            self.avatars.delete(owner, agent_id)
            for path in self._automation_files(owner, agent_id):
                path.unlink(missing_ok=True)

    def _automation_files(self, owner: str, agent_id: str) -> list[Path]:
        folder = self.data_dir / owner / ".automations"
        found = []
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("agent_id") == agent_id:
                    found.append(path)
            except (OSError, ValueError):
                continue
        return found

    def _copy_automations(self, source: str, target: str, old_id: str, new_id: str, *, paused: bool) -> list[Path]:
        made: list[Path] = []
        folder = self.data_dir / target / ".automations"
        for path in self._automation_files(source, old_id):
            record = json.loads(path.read_text(encoding="utf-8"))
            record.update({"user_id": target, "agent_id": new_id})
            if paused:
                record["enabled"] = False
                if "next_run" in record:
                    record["next_run"] = None
            folder.mkdir(parents=True, exist_ok=True)
            out = folder / path.name
            if out.exists():
                raise MoveError("automation '{id}' already exists for '{owner}'", id=path.stem, owner=target)
            atomic_write_text(out, json.dumps(record, ensure_ascii=False, indent=1) + "\n")
            made.append(out)
        return made

    # ------------------------------------------------------------------ checks and helpers

    def _owner(self, owner_id: str, kind: str) -> UserConfig:
        try:
            user = self.store.user(owner_id)
        except (FileNotFoundError, AgentConfigError, SpaceError, ValueError) as exc:
            raise MoveError("'{owner}' cannot be read: {details}", owner=owner_id, details=str(exc)) from exc
        if user.kind != kind:
            raise MoveError("'{owner}' is not a {kind}", owner=owner_id, kind=kind)
        return user

    def _target_user(self, target: str) -> UserConfig:
        user = self.store.user(target)  # a space comes back with its derived max_level
        return resolve_space(user, self.config_dir)

    def _check_idle(self, owner: str, agent_id: str) -> None:
        if self._is_busy(owner, agent_id):
            raise MoveError("wait for this agent's replies, tasks and processes to finish before moving it")

    def _free_id(self, member: str, agent_id: str, space_id: str) -> str:
        for number in range(1, 100):
            candidate = agent_id if number == 1 else f"{agent_id[:60]}-{number}"
            if agent_id_conflict(member, candidate, self.config_dir, ignore=space_id) is None \
                    and not (self.data_dir / member / candidate).exists():
                return candidate
        raise MoveError("no free id for agent '{agent_id}' in the account of '{owner}'", agent_id=agent_id, owner=member)

    @staticmethod
    def _fit_level(config: AgentConfig, user: UserConfig) -> AgentConfig:
        """A declared level above the target's `max_level` is lowered to it (nothing is deleted); a configuration that
        needs more than the maximum is refused by the level rule when the agent is added."""
        if config.level is not None and config.level > user.max_level:
            config = config.model_copy(update={"level": max(user.max_level, required_level(config, user.terminal_backends))})
        return config

    def _read_user_md(self, owner: str, agent_id: str) -> str:
        path = self.data_dir / owner / agent_id / _USER_MD
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _share_user_md(self, owner: str, config: AgentConfig) -> str:
        return render_sections({owner: split_memory(self._read_user_md(owner, config.id))})

    @staticmethod
    def _entries(sections: dict[str, list[str]], member: str) -> list[str]:
        return sections.get(member, [])

    @staticmethod
    def _flat(sections: dict[str, list[str]]) -> str:
        """USER.md of an agent moved back to its only member: that member's section as flat entries."""
        entries = [entry for items in sections.values() for entry in items]
        return join_memory(entries)

    @staticmethod
    def _safely(hook, owner: str, agent_id: str) -> None:
        """Tell the service about the move; a failure here never undoes it (the next start reconciles the index)."""
        try:
            hook(owner, agent_id)
        except Exception:
            logger.warning("agent move follow-up failed for %s/%s", owner, agent_id, exc_info=True)

    def _drop_targets(self, owner: str, *, only: str | None = None, drop: str | None = None) -> list[str]:
        """Remove delegation targets that are not agents of `owner` (or are `drop`); returns "<agent>:<target>" per removal.

        `only` limits the cleaning to one agent (the moved one: its targets stayed behind); without it every agent of
        `owner` is cleaned (the others, whose target just left). A dangling target does not fail the load, but saving
        the agent in the web refuses it, so it is removed and audited.
        """
        try:
            agents = self.store.user(owner).agents
            configs = [self.store.load(owner, agent_id) for agent_id in agents if only is None or agent_id == only]
        except (FileNotFoundError, AgentConfigError, SpaceError):
            return []
        removed = []
        for config in configs:
            if config.delegation is None:
                continue
            kept = [t for t in config.delegation.targets if t.agent in agents and t.agent != drop]
            if len(kept) == len(config.delegation.targets):
                continue
            removed += [f"{config.id}:{t.agent}" for t in config.delegation.targets if t not in kept]
            self.store.update(owner, config.id, config.model_copy(update={
                "delegation": config.delegation.model_copy(update={"targets": kept})}))
        if removed:
            self._audit(owner, "drop_delegation_targets", removed=removed)
        return removed

    def _check_fits(self, config: AgentConfig, levels: dict[str, Level], backends, where: str) -> None:
        """Refuse a configuration that needs more than the lowest `max_level` among `levels`, naming who limits it and why."""
        needed, service = required_level_reason(config, backends)
        member, lowest = min(levels.items(), key=lambda item: item[1])
        if needed > lowest:
            raise MoveError(
                "agent '{agent_id}' uses {service}, which needs the level '{needed}'; the maximum level of '{member}' "
                "is '{lowest}', which limits {where}. Remove {service} from the agent or ask an administrator to raise "
                "that maximum.", agent_id=config.id, service=service, needed=needed.label, member=member,
                lowest=lowest.label, where=where)

    def _audit(self, owner: str, action: str, **fields) -> None:
        write_audit(self.data_dir / owner / AUDIT_FILE, event="admin_action", action=action, **fields)
