from __future__ import annotations

import re
import threading
from urllib.parse import quote
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable, Literal, Optional

from src.agent.dream import DreamIndex, DreamRunner, dream_folder_options
from src.agent.profile import DREAM_TASKS_FILE, open_items
from src.config.models import DreamConfig, ResolvedAgent
from src.core.review import ReviewQueue
from src.core.sources.registry import SourceRegistry
from src.providers.provider import LLMProvider
from src.providers.tavily import TavilyClient
from src.utils.i18n import message
from src.utils.logger import get_logger

logger = get_logger("dream")

_COMMAND_RE = re.compile(r"^/dream(?:@\w+)?(?:\s+(?P<arg>.*?))?\s*$", re.IGNORECASE | re.DOTALL)
DreamCommand = Literal["status", "start", "cancel", "invalid"]
StartStatus = Literal["started", "started_nothing_pending", "already_running", "nothing_pending"]
CancelStatus = Literal["cancelling", "not_running"]


def _has_dream_tasks(sources) -> bool:
    """Whether the agent's `dream_tasks.md` has an open item (a reason to dream with no chat pending)."""
    palace = sources.mind_palace()
    try:
        return bool(palace.exists(DREAM_TASKS_FILE) and open_items(palace.read(DREAM_TASKS_FILE), limit=1))
    except (OSError, ValueError):
        return False


def parse_dream_command(text: str) -> DreamCommand | None:
    """`/dream` -> status, `/dream start|cancel` -> start|cancel, other `/dream ...` -> invalid; None otherwise."""
    match = _COMMAND_RE.match(text.strip())
    if match is None:
        return None
    arg = (match.group("arg") or "").strip().lower()
    if not arg:
        return "status"
    return arg if arg in ("start", "cancel") else "invalid"


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def format_local_time(value: str) -> str:
    """An ISO timestamp as local `YYYY-MM-DD HH:MM`, for command replies and notifications."""
    return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")


def session_link(public_url: str | None, agent_id: str, session_id: str) -> str | None:
    """The web UI address of a dream session (`<public_url>/#agents/<agent>/dreams/<session>`), if configured."""
    if not public_url:
        return None
    return f"{public_url.rstrip('/')}/#agents/{quote(agent_id, safe='')}/dreams/{quote(session_id, safe='')}"


def next_run(
    dream: DreamConfig, last_started_at: datetime | None, scheduled_since: datetime | None, now: datetime,
) -> datetime | None:
    """When the next scheduled session is due, or None when the schedule is off.

    The anchor is the last session start, else the moment the scheduler first saw the schedule, so
    enabling a schedule never starts a session at once. `daily_at` is local wall-clock time.
    """
    if not dream.enabled or (dream.every_hours is None and dream.daily_at is None):
        return None
    anchor = (last_started_at or scheduled_since or now).astimezone()
    if dream.every_hours is not None:
        return anchor + timedelta(hours=dream.every_hours)
    hour, minute = (int(part) for part in dream.daily_at.split(":"))
    local = anchor.replace(tzinfo=None)
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone()


@dataclass
class _Running:
    session_id: str
    started_at: str
    runner: DreamRunner
    thread: threading.Thread | None = None


@dataclass
class DreamDependencies:
    """How the manager loads agents and builds what a session needs; tests replace these with fakes."""

    resolve_agent: Callable[[str, str], ResolvedAgent]
    provider: Callable[[ResolvedAgent], LLMProvider]
    registry: Callable[[ResolvedAgent], SourceRegistry] = field(
        default=lambda config: SourceRegistry.for_agent(config.user_id, config.id, **dream_folder_options(config)),
    )
    index: Callable[[ResolvedAgent, SourceRegistry], DreamIndex] | None = None
    web_client: Callable[[], TavilyClient | None] = field(default=lambda: None)
    agents: Callable[[], Iterable[tuple[str, str]]] = field(default=lambda: ())
    language: Callable[[str], str] = field(default=lambda user_id: "en")
    notifier: Optional[Callable[[str, str], object]] = None
    public_url: Optional[str] = None  # web UI base address for links in notifications (web.public_url)
    clock: Callable[[], datetime] = datetime.now


class DreamManager:
    """Starts dream sessions in background threads, one per agent, and runs their schedule.

    A session holds the agent's `dream/.lock` for its whole run, so concurrent starts (from this or
    another process) report that a dream is already running. Chats never wait for a dream.
    """

    def __init__(self, deps: DreamDependencies):
        self._deps = deps
        self._running: dict[tuple[str, str], _Running] = {}
        self._guard = threading.Lock()
        self._scheduler: threading.Thread | None = None
        self._stop = threading.Event()

    # ------------------------------------------------------------------ sessions

    def start(self, user_id: str, agent_id: str, trigger: str = "manual") -> dict:
        """Start a session unless one runs; scheduled starts skip agents with no pending chat and no dream task."""
        config = self._deps.resolve_agent(user_id, agent_id)
        sources = self._deps.registry(config)
        state = sources.dream_state()
        key = (user_id, agent_id)
        with self._guard:
            if key in self._running:
                return {"status": "already_running", "session_id": self._running[key].session_id}
            lock = state.run_lock()
            if not lock.acquire():
                return {"status": "already_running", "session_id": None}
            try:
                pending = ReviewQueue(sources.conversations(), state).pending()
                if trigger == "schedule" and not pending and not _has_dream_tasks(sources):
                    lock.release()
                    return {"status": "nothing_pending", "session_id": None, "pending": 0}
                now = self._deps.clock()
                session_id = state.new_session_id(now)
                started_at = now.astimezone().isoformat(timespec="seconds")
                state.update_state(lambda data: data.update(last_started_at=started_at))
                state.clear_cancel()  # a stale request from a session that ended without clearing it
                runner = DreamRunner(
                    config, self._deps.provider(config), sources, session_id=session_id, trigger=trigger,
                    index_factory=(lambda: self._deps.index(config, sources)) if self._deps.index else None,
                    web_client=self._deps.web_client(), clock=self._deps.clock,
                )
                running = _Running(session_id, started_at, runner)
                running.thread = threading.Thread(
                    target=self._run, args=(key, config, runner, lock), daemon=True,
                    name=f"dream-{user_id}-{agent_id}",
                )
                self._running[key] = running
                running.thread.start()
            except BaseException:
                self._running.pop(key, None)
                if lock.held:
                    lock.release()
                raise
        status = "started" if pending else "started_nothing_pending"
        return {"status": status, "session_id": session_id, "pending": len(pending)}

    def _run(self, key: tuple[str, str], config: ResolvedAgent, runner: DreamRunner, lock) -> None:
        summary: dict | None = None
        try:
            summary = runner.run()
        finally:
            try:
                finished = self._deps.clock().astimezone().isoformat(timespec="seconds")
                runner.sources.dream_state().update_state(lambda data: data.update(last_finished_at=finished))
            except (OSError, RuntimeError):
                logger.exception("could not record the end of dream session %s", runner.session_id)
            lock.release()
            with self._guard:
                self._running.pop(key, None)
        if summary is not None and config.dream.notify_telegram:
            self._notify(config, summary)

    def _notify(self, config: ResolvedAgent, summary: dict) -> None:
        if self._deps.notifier is None:
            logger.info("dream notification skipped for %s: no Telegram bot token is configured", config.user_id)
            return
        language = self._deps.language(config.user_id)
        text = dream_notification(language, config.name, summary, session_link(self._deps.public_url, config.id, summary["id"]))
        try:
            self._deps.notifier(config.user_id, text)
        except Exception:  # a notification never breaks the session
            logger.exception("could not send the dream notification to %s", config.user_id)

    def wait(self, user_id: str, agent_id: str, timeout: float | None = None) -> bool:
        """Wait for the agent's running session (if any) to finish; False on timeout."""
        with self._guard:
            running = self._running.get((user_id, agent_id))
        if running is None or running.thread is None:
            return True
        running.thread.join(timeout)
        return not running.thread.is_alive()

    def cancel(self, user_id: str, agent_id: str) -> dict:
        """Stop the agent's running session gracefully: a running tool finishes, model generation stops.

        A session of this process is stopped directly; one run by another process (web or Telegram)
        gets a cancel request file that its runner picks up within a second.
        """
        with self._guard:
            running = self._running.get((user_id, agent_id))
        if running is not None:
            running.runner.request_stop("cancelled")
            return {"status": "cancelling", "session_id": running.session_id}
        config = self._deps.resolve_agent(user_id, agent_id)
        state = self._deps.registry(config).dream_state()
        if not state.is_running():
            return {"status": "not_running", "session_id": None}
        session_id = next((sid for sid in state.session_ids() if state.summary(sid) is None), None)
        if session_id is None:
            return {"status": "not_running", "session_id": None}
        state.request_cancel(session_id)
        return {"status": "cancelling", "session_id": session_id}

    # ------------------------------------------------------------------ reads

    def status(self, user_id: str, agent_id: str) -> dict:
        """Running state, last session summary, schedule with its next run, and pending chat count."""
        config = self._deps.resolve_agent(user_id, agent_id)
        sources = self._deps.registry(config)
        state_source = sources.dream_state()
        state = state_source.state()
        with self._guard:
            local = self._running.get((user_id, agent_id))
        running = local is not None or state_source.is_running()
        if local is not None:
            cancelling = local.runner.stop_reason == "cancelled"
        else:
            cancelling = running and state_source.pending_cancel() is not None
        last = None
        for session_id in state_source.session_ids():
            summary = state_source.summary(session_id)
            if summary is not None:
                last = summary
                break
        nxt = next_run(
            config.dream, _parse_time(state["last_started_at"]), _parse_time(state["scheduled_since"]),
            self._deps.clock().astimezone(),
        )
        dream = config.dream
        return {
            "running": running,
            "cancelling": cancelling,
            "current_session_id": local.session_id if local else None,
            "last_session": last,
            "schedule": {
                "enabled": dream.enabled,
                "every_hours": dream.every_hours,
                "daily_at": dream.daily_at,
                "next_run": nxt.isoformat(timespec="seconds") if nxt else None,
            },
            "last_started_at": state["last_started_at"],
            "last_finished_at": state["last_finished_at"],
            "pending_chats": len(ReviewQueue(sources.conversations(), state_source).pending()),
        }

    def sessions(self, user_id: str, agent_id: str) -> list[dict]:
        """Session summaries, newest first; sessions without a summary are running or were interrupted."""
        config = self._deps.resolve_agent(user_id, agent_id)
        state_source = self._deps.registry(config).dream_state()
        with self._guard:
            local = self._running.get((user_id, agent_id))
        items = []
        for session_id in state_source.session_ids():
            summary = state_source.summary(session_id)
            if summary is None:
                running = (local is not None and local.session_id == session_id) or (
                    local is None and state_source.is_running() and not items
                )
                summary = {"id": session_id, "status": "running" if running else "interrupted"}
            items.append({key: value for key, value in summary.items() if key != "diffs"})
        return items

    def session(self, user_id: str, agent_id: str, session_id: str) -> dict:
        """One session: its summary (None while it runs) and the full trace."""
        config = self._deps.resolve_agent(user_id, agent_id)
        state_source = self._deps.registry(config).dream_state()
        trace = state_source.trace(session_id)
        return {"id": session_id, "summary": state_source.summary(session_id), "events": trace}

    # ------------------------------------------------------------------ schedule

    def tick(self) -> list[tuple[str, str, dict]]:
        """Start every due scheduled session; returns (user, agent, start result) for each attempt."""
        started = []
        now = self._deps.clock().astimezone()
        for user_id, agent_id in self._deps.agents():
            try:
                config = self._deps.resolve_agent(user_id, agent_id)
                dream = config.dream
                if not dream.enabled or (dream.every_hours is None and dream.daily_at is None):
                    continue
                state_source = self._deps.registry(config).dream_state()
                state = state_source.state()
                if state["scheduled_since"] is None:
                    stamp = now.isoformat(timespec="seconds")
                    state = state_source.update_state(lambda data: data.update(scheduled_since=data["scheduled_since"] or stamp))
                due = next_run(dream, _parse_time(state["last_started_at"]), _parse_time(state["scheduled_since"]), now)
                if due is None or due > now:
                    continue
                with self._guard:
                    if (user_id, agent_id) in self._running:
                        continue
                started.append((user_id, agent_id, self.start(user_id, agent_id, trigger="schedule")))
            except Exception:  # one broken agent must not stop the others' schedules
                logger.exception("dream schedule check failed for %s/%s", user_id, agent_id)
        return started

    def start_scheduler(self, interval_seconds: float = 60.0) -> None:
        """Check the schedules every `interval_seconds` in a daemon thread until `stop` is called."""
        if self._scheduler is not None:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(interval_seconds):
                self.tick()

        self._scheduler = threading.Thread(target=loop, daemon=True, name="dream-scheduler")
        self._scheduler.start()

    def stop(self) -> None:
        """Stop the scheduler and ask running sessions to end after their current step."""
        self._stop.set()
        if self._scheduler is not None:
            self._scheduler.join(5)
            self._scheduler = None
        with self._guard:
            for running in self._running.values():
                running.runner.request_stop("cancelled")


_NOTIFICATIONS = {
    "completed": "Dream session completed for {agent}.",
    "budget_exhausted": "Dream session ran out of budget for {agent}.",
    "timed_out": "Dream session for {agent} reached its time limit and was closed.",
    "failed": "Dream session failed for {agent}.",
    "cancelled": "Dream session cancelled for {agent}.",
}


STATUS_EMOJI = {"completed": "✅", "budget_exhausted": "⚠️", "failed": "❌", "cancelled": "⏹", "timed_out": "⏱"}
NOTIFICATION_SUMMARY_CHARS = 600
STATUS_SUMMARY_CHARS = 1500


def md_escape(text: str) -> str:
    """Neutralize Markdown specials so names cannot break the surrounding bold/italic markers."""
    return re.sub(r"([\\`*_{}\[\]()#+\-.!~>|=<])", r"\\\1", text)


def summary_block(language: str, text: str | None, limit: int) -> str:
    """The dream agent's summary as a Markdown block quote, cut at a paragraph or sentence boundary.

    The summary is Markdown written by a model: it keeps its paragraphs and lists, an unclosed code
    fence is closed after truncation, and every line sits inside the quote so it cannot leak out of it.
    Returns "" for an empty summary.
    """
    body = (text or "").replace("\r\n", "\n").strip()
    if not body:
        return ""
    cut = len(body) > limit
    if cut:
        head = body[:limit]
        floor = limit // 3
        for pattern in (r"\n\s*\n", r"[.!?…](?:\s|$)", r"\n", r"\s"):
            ends = [m.start() + (1 if pattern.startswith("[") else 0) for m in re.finditer(pattern, head) if m.start() >= floor]
            if ends:
                head = head[:ends[-1]]
                break
        body = head.rstrip()
    if sum(1 for line in body.split("\n") if line.lstrip().startswith(("```", "~~~"))) % 2:
        body += "\n```"
    if cut:
        body += "\n\n… " + message(language, "(full summary in the web UI)")
    return "\n".join(f"> {line}" if line.strip() else ">" for line in body.split("\n"))


def dream_notification(language: str, agent_name: str, summary: dict, link: str | None) -> str:
    """The Telegram message (Markdown) for a finished session: outcome, summary excerpt, where to find it."""
    agent = md_escape(agent_name)
    title = message(language, _NOTIFICATIONS[summary["status"]], agent=agent)
    parts = [f"**{STATUS_EMOJI.get(summary['status'], '💤')} {title}**"]
    excerpt = summary_block(language, summary.get("summary"), NOTIFICATION_SUMMARY_CHARS)
    if excerpt:
        parts.append(excerpt)
    lines = [message(
        language, "See it in the web UI: Agents → {agent} → Dream sessions → {date}.",
        agent=agent, date=format_local_time(summary["started_at"]),
    )]
    if link:
        lines.append(message(language, "Open it: {url}", url=f"<{link}>"))
    parts.append("\n".join(lines))
    return "\n\n".join(parts)
