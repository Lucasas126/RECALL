"""Automations: things an agent does later, on a schedule or when a background process ends.

An automation is a durable record: who owns it, which agent runs it, what the agent is asked to do (`instruction`)
and when (`trigger`). When it is due the service starts an unattended task for that agent
(`TaskManager.launch_unattended`): nobody answers approvals, the usual limits and the provider cooldown apply, and the
result stays in the task record. The agent can message its owner with `notify_telegram` / `send_file` while it works.

Triggers (all in the owner's time zone, Europe/Madrid unless the trigger names another)
- `at`: once, at a date and time; the automation then finishes.
- `every`: every N minutes (at least `MIN_INTERVAL_MINUTES`).
- `cron`: a five-field cron expression.
- `process_end`: when a given background process of the owner ends (its id, as the process tools show it); once.

Where things live: `data/<user>/.automations/<id>.json`, one record per automation, read-modify-write under
`.automations/.state.lock`. Finished and failed records are pruned after `KEEP_DAYS`.

Guards (see `Limits`): a minimum gap between runs, runs per hour and per day, and a missed run is made up once after
a restart, never replayed one by one. A run that cannot start (rate-limit cooldown, task limits) is retried on the next
tick, up to `MAX_START_RETRIES` times, then skipped for that run.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from croniter import croniter

from src.utils.files import atomic_write_text, exclusive_file_lock
from src.utils.logger import get_logger

logger = get_logger("automations")

AUTOMATION_ID_RE = re.compile(r"^[0-9a-f]{10}$")
PROCESS_ID_RE = re.compile(r"^p[0-9a-f]{8}$")
TRIGGER_KINDS = ("at", "every", "cron", "process_end")
DEFAULT_TIMEZONE = "Europe/Madrid"
MIN_INTERVAL_MINUTES = 5
MAX_INSTRUCTION_CHARS = 4000
MAX_TITLE_CHARS = 80
MAX_PER_USER = 50
KEEP_DAYS = 30
MAX_START_RETRIES = 5
HISTORY = 20  # runs kept in a record


class AutomationError(ValueError):
    """A request that cannot be honoured; the message is meant for the person or the agent that asked."""


@dataclass(frozen=True)
class Limits:
    runs_per_hour: int = 6
    runs_per_day: int = 48


def _now() -> datetime:
    return datetime.now().astimezone()


def _stamp(value: datetime) -> str:
    """ISO text keeping the zone the datetime carries (the owner's, for trigger times), so the files read naturally."""
    return (value if value.tzinfo else value.astimezone()).isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value).astimezone() if value else None
    except ValueError:
        return None


def zone(name: str | None) -> tzinfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception as exc:
        raise AutomationError(f"unknown time zone: {name!r}") from exc


# ---------------------------------------------------------------------- triggers

def normalize_trigger(trigger: dict, *, now: datetime | None = None) -> dict:
    """Validate a trigger and return it in its stored shape; raises AutomationError with a readable reason."""
    if not isinstance(trigger, dict) or trigger.get("kind") not in TRIGGER_KINDS:
        raise AutomationError(f"trigger.kind must be one of: {', '.join(TRIGGER_KINDS)}")
    kind, tz = trigger["kind"], zone(trigger.get("timezone"))
    now = (now or _now()).astimezone(tz)
    if kind == "at":
        raw = str(trigger.get("at") or "").strip()
        try:
            moment = datetime.fromisoformat(raw)
        except ValueError:
            raise AutomationError("trigger.at must be a date and time like 2026-11-20T09:00 (the owner's local time)") from None
        moment = moment.replace(tzinfo=tz) if moment.tzinfo is None else moment.astimezone(tz)
        if moment <= now:
            raise AutomationError("trigger.at is in the past")
        return {"kind": "at", "at": moment.isoformat(timespec="minutes"), "timezone": tz.key}
    if kind == "every":
        try:
            minutes = int(trigger.get("minutes"))
        except (TypeError, ValueError):
            raise AutomationError("trigger.minutes must be a whole number of minutes") from None
        if minutes < MIN_INTERVAL_MINUTES:
            raise AutomationError(f"trigger.minutes must be at least {MIN_INTERVAL_MINUTES}")
        if minutes > 60 * 24 * 31:
            raise AutomationError("trigger.minutes must be at most 31 days")
        return {"kind": "every", "minutes": minutes, "timezone": tz.key}
    if kind == "cron":
        expression = " ".join(str(trigger.get("cron") or "").split())
        if len(expression.split()) != 5 or not croniter.is_valid(expression):
            raise AutomationError("trigger.cron must be a valid five-field cron expression, e.g. '30 8 * * mon-fri'")
        first = croniter(expression, now).get_next(datetime)
        second = croniter(expression, first).get_next(datetime)
        if second - first < timedelta(minutes=MIN_INTERVAL_MINUTES):
            raise AutomationError(f"trigger.cron fires more often than every {MIN_INTERVAL_MINUTES} minutes")
        return {"kind": "cron", "cron": expression, "timezone": tz.key}
    process_id = str(trigger.get("process") or "").strip()
    if not PROCESS_ID_RE.match(process_id):
        raise AutomationError("trigger.process must be a background process id like p1a2b3c4d")
    return {"kind": "process_end", "process": process_id, "timezone": tz.key}


def next_due(trigger: dict, after: datetime) -> datetime | None:
    """The first moment strictly after `after` at which a time trigger fires; None for `process_end`."""
    kind, tz = trigger["kind"], zone(trigger.get("timezone"))
    if kind == "at":
        return datetime.fromisoformat(trigger["at"]).astimezone(tz)
    if kind == "every":
        return (after + timedelta(minutes=trigger["minutes"])).astimezone(tz)
    if kind == "cron":
        return croniter(trigger["cron"], after.astimezone(tz)).get_next(datetime)  # already in the trigger's zone
    return None


def describe(trigger: dict) -> str:
    kind = trigger["kind"]
    if kind == "at":
        return f"once, {trigger['at'].replace('T', ' ')}"
    if kind == "every":
        minutes = trigger["minutes"]
        return f"every {minutes // 60} h" if minutes % 60 == 0 else f"every {minutes} min"
    if kind == "cron":
        return f"cron '{trigger['cron']}'"
    return f"when process {trigger['process']} ends"


# ---------------------------------------------------------------------- store

class AutomationStore:
    """Durable records of every user under `<data_dir>/<user>/.automations/`."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)

    def folder(self, user_id: str, *, create: bool = True) -> Path:
        path = self._data_dir / user_id / ".automations"
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def lock(self, user_id: str):
        return exclusive_file_lock(self.folder(user_id) / ".state.lock")

    def _path(self, user_id: str, automation_id: str) -> Path:
        if not AUTOMATION_ID_RE.match(automation_id or ""):
            raise AutomationError(f"unknown automation: {automation_id!r}")
        return self.folder(user_id) / f"{automation_id}.json"

    def get(self, user_id: str, automation_id: str) -> dict | None:
        try:
            return json.loads(self._path(user_id, automation_id).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            logger.warning("unreadable automation record %s/%s", user_id, automation_id)
            return None

    def list(self, user_id: str) -> list[dict]:
        folder = self.folder(user_id, create=False)
        records = []
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            if AUTOMATION_ID_RE.match(path.stem):
                record = self.get(user_id, path.stem)
                if record is not None:
                    records.append(record)
        return sorted(records, key=lambda r: r.get("created_at", ""))

    def users(self) -> list[str]:
        if not self._data_dir.is_dir():
            return []
        return sorted(p.parent.name for p in self._data_dir.glob("*/.automations") if p.is_dir())

    def save(self, record: dict) -> None:
        atomic_write_text(self._path(record["user_id"], record["id"]), json.dumps(record, ensure_ascii=False, indent=1) + "\n")

    def update(self, user_id: str, automation_id: str, change: Callable[[dict], bool | None]) -> dict | None:
        """Read-modify-write under the lock; `change` returns False to leave the record as it is."""
        with self.lock(user_id):
            record = self.get(user_id, automation_id)
            if record is None:
                return None
            if change(record) is not False:
                self.save(record)
            return record

    def delete(self, user_id: str, automation_id: str) -> bool:
        with self.lock(user_id):
            try:
                self._path(user_id, automation_id).unlink()
                return True
            except FileNotFoundError:
                return False


# ---------------------------------------------------------------------- service

@dataclass
class AutomationDependencies:
    """What the service needs from the rest of RECALL; tests replace these with fakes."""

    launch: Callable[..., Any]  # TaskManager.launch_unattended(user, agent, goal, context, trigger=...)
    agent_exists: Callable[[str, str], bool]
    clock: Callable[[], datetime] = _now
    process_state: Callable[[str, str], str | None] = lambda user_id, process_id: None  # a state name, None if unknown


class AutomationService:
    def __init__(self, data_dir: Path, deps: AutomationDependencies, limits: Limits = Limits()) -> None:
        self.store = AutomationStore(data_dir)
        self._deps = deps
        self.limits = limits
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._guard = threading.Lock()

    # ---- create / edit / remove

    def create(
        self, user_id: str, agent_id: str, instruction: str, trigger: dict, *, title: str = "", created_by: str = "",
    ) -> dict:
        instruction = (instruction or "").strip()
        if not instruction:
            raise AutomationError("instruction is empty; say what the agent must do when this fires")
        if len(instruction) > MAX_INSTRUCTION_CHARS:
            raise AutomationError(f"instruction is too long ({len(instruction)} characters, at most {MAX_INSTRUCTION_CHARS})")
        if not self._deps.agent_exists(user_id, agent_id):
            raise AutomationError(f"agent '{agent_id}' is not one of this user's agents")
        now = self._deps.clock()
        stored = normalize_trigger(trigger, now=now)
        if stored["kind"] == "process_end" and self._deps.process_state(user_id, stored["process"]) is None:
            raise AutomationError(f"no background process {stored['process']} to wait for")
        with self.store.lock(user_id):
            if len(self.store.list(user_id)) >= MAX_PER_USER:
                raise AutomationError(f"limit: at most {MAX_PER_USER} automations per user; delete one first")
            due = next_due(stored, now)
            record = {
                "id": secrets.token_hex(5), "user_id": user_id, "agent_id": agent_id,
                "title": (title or instruction).strip().replace("\n", " ")[:MAX_TITLE_CHARS],
                "instruction": instruction, "trigger": stored, "enabled": True, "status": "active",
                "created_at": _stamp(now), "created_by": created_by, "next_run": _stamp(due) if due else None,
                "last_run": None, "runs": 0, "retries": 0, "history": [], "recent_runs": [],
            }
            self.store.save(record)
        return record

    def set_enabled(self, user_id: str, automation_id: str, enabled: bool) -> dict:
        def change(record: dict) -> bool:
            if record["status"] == "done":
                raise AutomationError("this one-off automation already ran; create a new one")
            record["enabled"] = bool(enabled)
            if enabled:  # resuming never replays what was missed while it was paused
                record["status"], record["retries"] = "active", 0
                due = next_due(record["trigger"], self._deps.clock())
                record["next_run"] = _stamp(due) if due else None
            return True

        record = self.store.update(user_id, automation_id, change)
        if record is None:
            raise AutomationError(f"unknown automation: {automation_id!r}")
        return record

    def edit(
        self, user_id: str, automation_id: str, *, instruction: str | None = None, trigger: dict | None = None,
        title: str | None = None,
    ) -> dict:
        stored = normalize_trigger(trigger, now=self._deps.clock()) if trigger is not None else None
        if instruction is not None and not instruction.strip():
            raise AutomationError("instruction is empty")
        if instruction is not None and len(instruction) > MAX_INSTRUCTION_CHARS:
            raise AutomationError(f"instruction is too long (at most {MAX_INSTRUCTION_CHARS} characters)")

        def change(record: dict) -> bool:
            if instruction is not None:
                record["instruction"] = instruction.strip()
            if title is not None:
                record["title"] = title.strip().replace("\n", " ")[:MAX_TITLE_CHARS]
            if stored is not None:
                record["trigger"], record["status"], record["retries"] = stored, "active", 0
                due = next_due(stored, self._deps.clock())
                record["next_run"] = _stamp(due) if due and record["enabled"] else None
            return True

        record = self.store.update(user_id, automation_id, change)
        if record is None:
            raise AutomationError(f"unknown automation: {automation_id!r}")
        return record

    def delete(self, user_id: str, automation_id: str) -> None:
        if not self.store.delete(user_id, automation_id):
            raise AutomationError(f"unknown automation: {automation_id!r}")

    def list(self, user_id: str, agent_id: str | None = None) -> list[dict]:
        return [r for r in self.store.list(user_id) if agent_id is None or r["agent_id"] == agent_id]

    # ---- the clock

    def tick(self) -> list[tuple[str, str]]:
        """Run every time-triggered automation that is due; returns (user, automation id) for each one started."""
        started = []
        for user_id in self.store.users():
            for record in self.store.list(user_id):
                try:
                    if self._due(record) and self._run(record):
                        started.append((user_id, record["id"]))
                except Exception:  # one broken automation must not stop the others
                    logger.exception("automation check failed for %s/%s", user_id, record.get("id"))
            self._prune(user_id)
        return started

    def process_ended(self, user_id: str, process_id: str) -> list[str]:
        """A background process finished: fire the automations that wait for it."""
        fired = []
        for record in self.store.list(user_id):
            trigger = record["trigger"]
            waits = trigger["kind"] == "process_end" and trigger["process"] == process_id
            if waits and record["enabled"] and record["status"] == "active":
                try:
                    if self._run(record, reason=f"process {process_id} ended"):
                        fired.append(record["id"])
                except Exception:
                    logger.exception("automation %s/%s failed to start", user_id, record["id"])
        return fired

    def _due(self, record: dict) -> bool:
        if not record["enabled"] or record["status"] != "active" or record["trigger"]["kind"] == "process_end":
            return False
        due = _parse(record.get("next_run"))
        return due is not None and due <= self._deps.clock()

    def _run(self, record: dict, *, reason: str = "scheduled") -> bool:
        """Start the automation now, honouring the limits; True when a task was launched."""
        user_id, now = record["user_id"], self._deps.clock()
        with self._guard:
            blocked = self._over_limit(record, now)
            if blocked:
                self._note(record, now, blocked)
                return False
            if not self._deps.agent_exists(user_id, record["agent_id"]):
                self._finish(record, now, "the agent no longer exists", launched=False)
                return False
            goal = f"Automation '{record['title']}' ({describe(record['trigger'])}; {reason}).\n\n{record['instruction']}"
            try:
                live = self._deps.launch(user_id, record["agent_id"], goal, "", trigger="automation")
            except Exception as exc:  # the provider cooldown, task limits: try again next tick, a few times
                return self._retry_or_skip(record, now, str(exc))
            self._finish(record, now, "started", launched=True, task_id=getattr(live, "task_id", None))
            return True

    def _over_limit(self, record: dict, now: datetime) -> str | None:
        recent = [t for t in (_parse(x) for x in record.get("recent_runs", [])) if t]
        if len([t for t in recent if now - t < timedelta(hours=1)]) >= self.limits.runs_per_hour:
            return f"skipped: more than {self.limits.runs_per_hour} runs in an hour"
        if len([t for t in recent if now - t < timedelta(days=1)]) >= self.limits.runs_per_day:
            return f"skipped: more than {self.limits.runs_per_day} runs in a day"
        last = _parse(record.get("last_run"))
        if last is not None and now - last < timedelta(minutes=MIN_INTERVAL_MINUTES) and record["trigger"]["kind"] != "process_end":
            return f"skipped: ran less than {MIN_INTERVAL_MINUTES} minutes ago"
        return None

    def _advance(self, record: dict, now: datetime) -> None:
        """Move `next_run` past `now`: a missed run is made up once, never replayed one by one."""
        due = next_due(record["trigger"], now)
        record["next_run"] = _stamp(due) if due else None

    def _note(self, record: dict, now: datetime, text: str) -> None:
        def change(stored: dict) -> None:
            self._advance(stored, now)
            stored["history"] = [*stored["history"], {"at": _stamp(now), "outcome": text}][-HISTORY:]

        self.store.update(record["user_id"], record["id"], change)

    def _retry_or_skip(self, record: dict, now: datetime, error: str) -> bool:
        def change(stored: dict) -> None:
            stored["retries"] = stored.get("retries", 0) + 1
            if stored["retries"] >= MAX_START_RETRIES:
                stored["retries"] = 0
                self._advance(stored, now)
                stored["history"] = [*stored["history"], {"at": _stamp(now), "outcome": f"could not start: {error[:200]}"}][-HISTORY:]

        self.store.update(record["user_id"], record["id"], change)
        return False

    def _finish(self, record: dict, now: datetime, outcome: str, *, launched: bool, task_id: str | None = None) -> None:
        def change(stored: dict) -> None:
            stored["retries"] = 0
            entry = {"at": _stamp(now), "outcome": outcome, **({"task": task_id} if task_id else {})}
            stored["history"] = [*stored["history"], entry][-HISTORY:]
            if launched:
                stored["runs"] += 1
                stored["last_run"] = _stamp(now)
                stored["recent_runs"] = [*stored.get("recent_runs", []), _stamp(now)][-self.limits.runs_per_day:]
            if not launched:
                stored["status"], stored["enabled"], stored["next_run"] = "failed", False, None
            elif stored["trigger"]["kind"] in ("at", "process_end"):
                stored["status"], stored["next_run"] = "done", None
            else:
                self._advance(stored, now)

        self.store.update(record["user_id"], record["id"], change)

    def _prune(self, user_id: str) -> None:
        cutoff = self._deps.clock() - timedelta(days=KEEP_DAYS)
        for record in self.store.list(user_id):
            if record["status"] not in ("done", "failed"):
                continue
            last = record["history"][-1]["at"] if record["history"] else record["created_at"]
            if (_parse(last) or cutoff) < cutoff:
                self.store.delete(user_id, record["id"])

    # ---- the thread

    def start(self, interval_seconds: float = 30.0) -> None:
        if self._thread is not None:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(interval_seconds):
                try:
                    self.tick()
                except Exception:
                    logger.warning("the automation scheduler failed", exc_info=True)

        self._thread = threading.Thread(target=loop, daemon=True, name="automations")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(5)
