from __future__ import annotations

from contextlib import ExitStack, contextmanager, nullcontext

from src.agent.read_ledger import ReadLedger, bytes_digest
from src.core.sources.agent_files import SOUL_FILE
from src.core.sources.base import Source, pdf_text
from src.core.sources.registry import SourceRegistry


READ_MAX_CHARS = 40_000  # most text one read returns; a longer file comes with a note on how to continue
READ_LINE_CHARS = 2_000  # longest line shown; a longer one (a chat turn in a .jsonl, minified data) is cut


def cap_text(content: str, lines: list[int] | None = None) -> str:
    """The requested lines of a file, capped so one read never floods the model's context.

    Lines longer than `READ_LINE_CHARS` are cut with a marker; once the output reaches `READ_MAX_CHARS` it stops at
    the last whole line and a note says which lines were shown and how to ask for the next ones."""
    rows = content.splitlines()
    total = len(rows)
    start = max(1, lines[0]) if lines else 1
    end = min(total, lines[1] if lines and len(lines) > 1 else (start if lines else total))
    out: list[str] = []
    size = 0
    cut_lines = 0
    last = start - 1
    for number in range(start, end + 1):
        row = rows[number - 1]
        if len(row) > READ_LINE_CHARS:
            row = f"{row[:READ_LINE_CHARS]} …[line {number} cut: {len(row)} chars]"
            cut_lines += 1
        if out and size + len(row) + 1 > READ_MAX_CHARS:
            break
        out.append(row)
        size += len(row) + 1
        last = number
    text = "\n".join(out)
    notes = []
    if last < end:
        notes.append(
            f"output capped at {READ_MAX_CHARS} chars: showed lines {start}-{last} of {total}; "
            f"continue with lines=[{last + 1}, {min(end, last + 200)}]"
        )
    if cut_lines:
        notes.append(f"{cut_lines} long line(s) cut at {READ_LINE_CHARS} chars")
    return f"{text}\n…[{'; '.join(notes)}]" if notes else text


def readable_sources(sources: SourceRegistry, source: str) -> list[Source]:
    """Return readable sources, optionally restricted to one source name."""
    names = [source] if source else sources.names()
    return [sources.get(name) for name in names if sources.get(name).readable]


def mark_written(agent, address: str) -> None:
    """Tell the agent a tool changed `address`, so the index picks it up after the turn."""
    note = getattr(agent, "note_written", None)
    if note is not None:
        note(address)


def readonly_error(sources: SourceRegistry, memory_path: str) -> str | None:
    """Return an error when an address resolves to a read-only source."""
    source, _ = sources.resolve(memory_path)
    if not source.writable:
        return f"error: source '{source.name}' is read-only here; write to mind_palace or to a folder you may edit"
    return None


def read_text(sources: SourceRegistry, address: str) -> str:
    """The text of a file; a PDF is read as its page text (the file itself is never rewritten from it)."""
    if address.lower().endswith(".pdf"):
        source, rel = sources.resolve(address)
        return pdf_text(source.read_bytes(rel), rel)
    return sources.read(address)


def ledger_of(agent) -> ReadLedger:
    """The chat's read ledger (created on first use). The SOUL shown in the chat's prompt counts as read."""
    ledger = getattr(agent, "read_ledger", None)
    if ledger is None:
        ledger = ReadLedger()
        try:
            agent.read_ledger = ledger
        except AttributeError:  # a host that cannot hold one: a fresh ledger per tool set
            return ledger
        soul = getattr(getattr(agent, "profile", None), "soul", "")
        if soul:
            try:
                ledger.record(agent.sources.agent_files().resolve(SOUL_FILE), bytes_digest(soul.encode("utf-8")))
            except (KeyError, AttributeError, ValueError):
                pass
    return ledger


def note_read(agent, source: Source, rel: str) -> None:
    """Record what the chat just read at `source:rel`, so a later whole-file write can tell if it changed since."""
    try:
        path = source.resolve(rel)
    except ValueError:
        return
    ledger_of(agent).saw(path)


def write_lock_for(sources: SourceRegistry, source: Source):
    """The lock a read-modify-write of `source` must hold: a user folder's own lock, else the agent's folder lock
    (the one its memory, SOUL, skills and dreams use)."""
    folder_lock = getattr(source, "write_lock", None)
    if folder_lock is not None:
        return folder_lock()
    try:
        return sources.agent_files().memory_lock()
    except (KeyError, AttributeError):  # a registry without the agent source (nothing shared to protect)
        return nullcontext()


@contextmanager
def write_locks(sources: SourceRegistry, *involved: Source):
    """Hold the write locks of every source involved, always in the same order (agent lock first, then folders by
    name), so two writers that need the same pair cannot deadlock."""
    agent_side = [source for source in involved if getattr(source, "write_lock", None) is None]
    folders = sorted({source.name: source for source in involved if source not in agent_side}.values(), key=lambda s: s.name)
    with ExitStack() as stack:
        if agent_side:
            stack.enter_context(write_lock_for(sources, agent_side[0]))
        for folder in folders:
            stack.enter_context(folder.write_lock())
        yield
