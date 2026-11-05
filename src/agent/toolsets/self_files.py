from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Callable, Optional

from src.agent.tooling import tool
from src.agent.toolsets.common import mark_written
from src.config.settings import memory_limits
from src.core.sources.agent_files import BUILTIN_SKILL, SKILL_FILE, join_memory
from src.utils.i18n import InputError

if TYPE_CHECKING:
    from src.agent.agent import Agent

_APPLIES_LATER = "The change applies to new chats; this conversation keeps the version it started with."


class MemoryOperationError(ValueError):
    """A memory operation could not be applied; nothing was saved."""


def apply_memory_operations(entries: list[str], operations: list[dict]) -> list[str]:
    """Apply add/patch/remove operations in order to a copy of `entries` and return the result.

    `patch` replaces the `old_text` span inside the only entry containing it; `remove` deletes
    that entry. Raises MemoryOperationError naming the failing operation, leaving `entries` untouched.
    """
    result = list(entries)
    for number, operation in enumerate(operations, 1):
        label = f"operation {number}" if len(operations) > 1 else "operation"
        if not isinstance(operation, dict):
            raise MemoryOperationError(f"{label}: expected an object with action, content and old_text")
        action = str(operation.get("action") or "").strip()
        content = operation.get("content")
        if content is None:
            content = operation.get("new_text")
        content = "" if content is None else str(content)
        old_text = str(operation.get("old_text") or "")
        if action == "add":
            entry = content.strip()
            if not entry:
                raise MemoryOperationError(f"{label}: add needs a non-empty content")
            if "\n§\n" in f"\n{entry}\n":
                raise MemoryOperationError(f"{label}: content cannot contain a line with only '§' (the entry separator)")
            if entry in result:
                raise MemoryOperationError(f"{label}: this entry already exists")
            result.append(entry)
            continue
        if action not in ("patch", "remove"):
            raise MemoryOperationError(f"{label}: unknown action {action!r}; use add, patch or remove")
        if not old_text:
            raise MemoryOperationError(f"{label}: {action} needs old_text from the entry to change")
        matches = [index for index, entry in enumerate(result) if old_text in entry]
        if not matches:
            raise MemoryOperationError(f"{label}: old_text not found in any entry")
        if len(matches) > 1:
            raise MemoryOperationError(f"{label}: old_text matches {len(matches)} entries; quote more of the entry")
        index = matches[0]
        if action == "remove":
            result.pop(index)
            continue
        if result[index].count(old_text) > 1:
            raise MemoryOperationError(f"{label}: old_text appears several times in the entry; quote more context")
        patched = result[index].replace(old_text, content, 1).strip()
        if not patched:
            raise MemoryOperationError(f"{label}: the patch would leave the entry empty; use remove instead")
        result[index] = patched
    return result


def build_self_tools(agent: Agent) -> list[Callable[..., str]]:
    """Build the tools with which the agent edits its own SOUL.md, memories and skills."""
    sources = agent.sources

    def limit(target: str) -> int:
        limits = memory_limits()
        return limits.memory_chars if target == "memory" else limits.user_chars

    @tool
    def memory(
        target: Annotated[str, "'memory' for your own notes, 'user' for what you know about the user"],
        action: Annotated[str, "single change: add, patch or remove; omit when using operations"] = "",
        content: Annotated[str, "add: the new entry; patch: the text that replaces old_text"] = "",
        old_text: Annotated[str, "patch and remove: text from exactly one entry (patch replaces only this span)"] = "",
        operations: Annotated[
            Optional[list[dict]],
            "batch of {action, content?, old_text?} objects applied atomically; the char limit is checked on the final result",
        ] = None,
    ) -> str:
        """Save durable facts in your memory files, which are injected in the system prompt of every new chat.
        Keep entries short and high-signal: who the user is, stable preferences and corrections (target 'user');
        conventions, environment facts and lessons for yourself (target 'memory'). Make all changes to one target
        in ONE call with 'operations': the batch is atomic and the character limit is checked only on the final
        result, so you can remove or shorten stale entries and add new ones together when memory is full.
        Do not store task progress, raw data or reusable procedures (write a skill for those)."""
        if target not in ("memory", "user"):
            return "error: target must be 'memory' or 'user'"
        if operations is None:
            if not action:
                return "error: give an action (add, patch, remove) or a list of operations"
            operations = [{"action": action, "content": content, "old_text": old_text}]
        elif action:
            return "error: use either action or operations, not both"
        files = sources.agent_files()
        with files.memory_lock():
            current = files.memory_entries(target)
            try:
                updated = apply_memory_operations(current, operations)
            except MemoryOperationError as exc:
                return f"error: {exc}. Nothing was saved."
            text = join_memory(updated)
            if len(text) > limit(target):
                listing = "\n".join(f"- {entry}" for entry in current) or "(empty)"
                return (
                    f"error: the result would use {len(text)} of {limit(target)} chars. Nothing was saved. "
                    "Reissue ONE batch that removes or shortens stale entries and adds the new one. "
                    f"Current entries:\n{listing}"
                )
            files.write_memory_text(target, text)
        return f"saved {target}: {len(updated)} entries, {len(text)}/{limit(target)} chars. {_APPLIES_LATER}"

    @tool
    def skill_view(
        name: Annotated[str, "the skill name from the skills index"],
        file_path: Annotated[str, "optional supporting file, e.g. references/notes.md; a folder (references/) lists its files"] = "",
    ) -> str:
        """Load a skill's full SKILL.md (or one of its supporting files, or the file list of a folder) before following it."""
        files = sources.agent_files()
        if name == BUILTIN_SKILL and not agent.config.recall_knowledge:
            raise FileNotFoundError(f"skill not found: {name}")
        if file_path:
            folder = file_path.replace("\\", "/").strip().strip("/")
            listing = files.skill_files(name)
            if file_path.strip().endswith(("/", "\\")) or (folder and folder not in listing and any(f.startswith(folder + "/") for f in listing)):
                inside = [f for f in listing if not folder or f.startswith(folder + "/")]
                if not inside:
                    raise FileNotFoundError(f"no files in {folder or 'the skill'}/ of skill {name}")
                return f"[files in {folder or name}/]\n" + "\n".join(inside)
            return files.read_skill_file(name, file_path)
        text = files.skill_text(name)
        extra = files.skill_files(name)
        if extra:
            text += "\n\n[supporting files: " + ", ".join(extra) + "; load them with skill_view(name, file_path)]"
        return text

    @tool
    def skill_manage(
        action: Annotated[str, "create, edit (replace SKILL.md), patch, delete, write_file or remove_file"],
        name: Annotated[str, "skill name: lowercase letters, digits, '-' or '_'"],
        content: Annotated[str, "create/edit: the full SKILL.md, starting with frontmatter (name, description)"] = "",
        old_string: Annotated[str, "patch: exact text to replace; must be unique"] = "",
        new_string: Annotated[str, "patch: the replacement text"] = "",
        file_path: Annotated[str, "write_file/remove_file, or patch of a supporting file: path inside the skill"] = "",
        file_content: Annotated[str, "write_file: the file's full contents"] = "",
    ) -> str:
        """Create and maintain skills: reusable procedures you write for yourself (SKILL.md with YAML
        frontmatter `name` and `description`, then the steps, pitfalls and checks). The index (name and
        description) is shown in every new chat; the body is loaded on demand with skill_view. Prefer patching
        an existing skill over creating a near-duplicate."""
        files = sources.agent_files()
        try:
            files.reject_builtin(name)
            if action == "create":
                try:
                    files.skill_text(name)
                except FileNotFoundError:
                    pass
                else:
                    return f"error: skill '{name}' already exists; use edit or patch"
                files.write_skill(name, content)
            elif action == "edit":
                files.skill_text(name)
                files.write_skill(name, content)
            elif action == "patch":
                text = files.read_skill_file(name, file_path) if file_path else files.skill_text(name)
                count = text.count(old_string) if old_string else 0
                if count != 1:
                    return f"error: old_string must appear exactly once (found {count}); add surrounding context"
                patched = text.replace(old_string, new_string, 1)
                if file_path:
                    files.write_skill_file(name, file_path, patched)
                else:
                    files.write_skill(name, patched)
            elif action == "delete":
                files.delete_skill(name)
            elif action == "write_file":
                files.write_skill_file(name, file_path, file_content)
            elif action == "remove_file":
                files.remove_skill_file(name, file_path)
            else:
                return "error: unknown action; use create, edit, patch, delete, write_file or remove_file"
        except InputError as exc:
            return f"error: {exc}"
        target = file_path if action in ("write_file", "remove_file") or (action == "patch" and file_path) else SKILL_FILE
        mark_written(agent, f"agent:skills/{name}/{target}")
        return f"{action} done: skill '{name}' ({target}). {_APPLIES_LATER}"

    @tool
    def soul_edit(
        action: Annotated[str, "replace (write the whole SOUL.md) or patch (replace one exact fragment)"],
        content: Annotated[str, "replace: the full new SOUL.md"] = "",
        old_string: Annotated[str, "patch: exact fragment to replace; must be unique"] = "",
        new_string: Annotated[str, "patch: the replacement text"] = "",
    ) -> str:
        """Change your SOUL.md: your identity, personality and lasting rules of behavior. Make small,
        explained changes for durable lessons only; facts go to memory and procedures to skills. The previous
        version is saved to soul_history/ (the last 5 are kept) so the user can restore it."""
        files = sources.agent_files()
        current = files.soul() or ""
        if action == "replace":
            if not content.strip():
                return "error: replace needs the full new SOUL.md in content"
            updated = content
        elif action == "patch":
            count = current.count(old_string) if old_string else 0
            if count != 1:
                return f"error: old_string must appear exactly once in SOUL.md (found {count}); add surrounding context"
            updated = current.replace(old_string, new_string, 1)
        else:
            return "error: action must be replace or patch"
        try:
            changed = files.write_soul(updated)
        except InputError as exc:
            return f"error: {exc}"
        if not changed:
            return "SOUL.md is unchanged."
        return f"SOUL.md updated ({len(updated)} chars); the previous version was saved. {_APPLIES_LATER}"

    return [memory, skill_view, skill_manage, soul_edit]

