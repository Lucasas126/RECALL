"""File operations on the sandbox filesystem, executed THROUGH the terminal backend.

Every operation is a small POSIX shell snippet; file content travels on STDIN (never inside the command
line). Paths are validated and shell-quoted. The same approval policy as `terminal` applies, reads included.
"""

from __future__ import annotations

import difflib
import posixpath
import re
import shlex

from src.terminal.output import clean
from src.terminal.policy import Assessment, assess_write
from src.terminal.session import TerminalSession

MAX_PATH = 4096
MAX_FILE_BYTES = 2_000_000
MAX_READ_LINES = 2000
MAX_LINE_CHARS = 2000
MAX_SEARCH_RESULTS = 200
_NORMAL = Assessment("normal")
_TOTAL = "@@TOTAL "
# exit codes of the snippets
_MISSING, _IS_DIR, _BINARY, _FAILED = 3, 4, 5, 2
_BINARY_CHECK = (
    """[ "$(head -c 8192 "$f" | LC_ALL=C tr -d '\\000' | wc -c)" = "$(head -c 8192 "$f" | wc -c)" ] || exit 5"""
)
_AWK = (
    'NR>=s && NR<=e { l=$0; if (length(l)>m) l=substr(l,1,m) " [line cut]"; printf "%6d|%s\\n", NR, l } '
    'END { print "' + _TOTAL + '" NR }'
)


def check_path(path: str) -> str:
    """The path as typed, or ValueError: no NUL, not empty, bounded."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must not be empty")
    if "\x00" in path:
        raise ValueError("path must not contain a NUL byte")
    if len(path) > MAX_PATH:
        raise ValueError(f"path is longer than {MAX_PATH} characters")
    if "\n" in path or "\r" in path:
        raise ValueError("path must not contain line breaks")
    return path.strip()


def shell_path(path: str) -> str:
    """A shell word for `path`; a leading `~` expands to $HOME (relative paths hang from the working directory)."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def _guard(path: str) -> str:
    """Make a path that starts with `-` safe as an operand."""
    return "./" + path if path.startswith("-") else path


def _where(session: TerminalSession, path: str) -> str:
    return path if path.startswith(("/", "~")) else posixpath.join(session.workdir, path)


def target_path(session: TerminalSession, path: str) -> str:
    """The path as the approval rules see it: absolute and normalized, relative paths hung from the workdir (`~` stays)."""
    return path if path.startswith("~") else posixpath.normpath(_where(session, path))


def _problem(code: int | None, path: str, stderr: str) -> str:
    return {
        _MISSING: f"error: no such file: {path}",
        _IS_DIR: f"error: {path} is a directory",
        _BINARY: f"error: {path} looks like a binary file; it cannot be shown as text",
    }.get(code, f"error: could not access {path}" + (f": {stderr.strip()}" if stderr.strip() else ""))


def protected_verdict(session: TerminalSession, path: str, *, write: bool = False) -> Assessment | None:
    """The `protected` verdict for a path on a local backend (key, secret, .env...), else None. Exact, not a scan.

    `write=True` (sandbox_write, sandbox_patch) also refuses the write-only paths: audit trail, user/agent/settings yaml, known_hosts.
    """
    protected = session.protected()
    if protected is None:
        return None
    why = protected.reason(path, session.local_cwd(), write=write)
    return Assessment("protected", f"{path} is {why}") if why else None


def _checked(session: TerminalSession, tool: str, path: str) -> str:
    """`check_path`, recording a refused path in the audit trail (early refusals are audited too)."""
    try:
        return check_path(path)
    except ValueError as exc:
        session.audit_refusal(tool, f"{tool} {str(path)[:200]!r}".replace("\x00", "\\0"), str(exc))
        raise


PREVIEW_CHARS = 400


def preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    """A truncated one-block preview of text for an approval card (control characters made visible)."""
    shown = text[:limit].replace("\x00", "\\0")
    return shown + (f"\n... [{len(text) - limit} more characters]" if len(text) > limit else "")


def _fetch(session: TerminalSession, tool: str, display: str, path: str, body: str):
    result, denial = session.run(
        tool, display, body, assessment=protected_verdict(session, path) or _NORMAL, path=target_path(session, path),
    )
    return result, denial


# ---------------------------------------------------------------------- read
def sandbox_read(session: TerminalSession, path: str, offset: int = 1, limit: int = 200) -> str:
    path = _checked(session, "sandbox_read", path)
    offset, limit = max(1, int(offset)), max(1, min(int(limit), MAX_READ_LINES))
    script = "\n".join([
        "f=" + shell_path(path),
        f'[ -e "$f" ] || exit {_MISSING}',
        f'[ -d "$f" ] && exit {_IS_DIR}',
        _BINARY_CHECK,
        f"awk -v s={offset} -v e={offset + limit - 1} -v m={MAX_LINE_CHARS} '{_AWK}' \"$f\"",
    ])
    result, denial = _fetch(session, "sandbox_read", f"read {path} (lines {offset}-{offset + limit - 1})", path, script)
    if denial:
        return denial
    if result.timed_out or result.exit_code != 0:
        return "error: reading timed out" if result.timed_out else _problem(result.exit_code, path, result.stderr)
    lines = result.stdout.splitlines()
    total = 0
    if lines and lines[-1].startswith(_TOTAL):
        total = int(lines.pop().removeprefix(_TOTAL) or 0)
    text, _ = clean("\n".join(lines), session.output_limit)
    shown_end = offset + len(lines) - 1
    if not lines:
        return f"{path}: empty or past the end ({total} lines in total)"
    footer = f"\n[{path}: lines {offset}-{shown_end} of {total}]"
    if shown_end < total:
        footer += f" Use offset={shown_end + 1} to continue."
    return text + footer


# ---------------------------------------------------------------------- fetch (a whole file, as bytes)
_TOO_BIG = 6


def _readable_size(count: int) -> str:
    return f"{count // 1024 // 1024} MiB" if count >= 1024 * 1024 else f"{max(1, count // 1024)} KiB"


def fetch_bytes(session: TerminalSession, path: str, max_bytes: int, *, tool: str = "send_file") -> bytes:
    """The raw bytes of a file on the backend (base64 over stdout, decoded here); ValueError with the reason when not.

    Same approval and protected-path treatment as `sandbox_read` (the tool counts as a read for the rules). The size is
    checked on the backend before anything is sent, so an oversized file is never transferred.
    """
    import base64
    import binascii

    path = _checked(session, tool, path)
    script = "\n".join([
        "f=" + shell_path(path),
        f'[ -e "$f" ] || exit {_MISSING}',
        f'[ -d "$f" ] && exit {_IS_DIR}',
        'n=$(wc -c < "$f" | tr -d " ")',
        f'if [ "$n" -gt {int(max_bytes)} ]; then echo "$n"; exit {_TOO_BIG}; fi',
        "base64 < \"$f\" | tr -d '\\n\\r'",
    ])
    result, denial = _fetch(session, tool, f"fetch {path}", path, script)
    if denial:
        raise ValueError(denial)
    if result.timed_out:
        raise ValueError("error: reading the file timed out")
    if result.exit_code == _TOO_BIG:
        size = int((result.stdout.strip() or "0")) if result.stdout.strip().isdigit() else 0
        raise ValueError(f"error: the file is {size // 1024} KiB; the limit is {_readable_size(max_bytes)}")
    if result.exit_code != 0:
        raise ValueError(_problem(result.exit_code, path, result.stderr))
    try:
        return base64.b64decode(result.stdout.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"error: could not decode {path} as received") from exc


# ---------------------------------------------------------------------- write
def _write_script(path: str) -> str:
    p = shell_path(path)
    return (
        f"f={p}\n"
        f'[ -d "$f" ] && exit {_IS_DIR}\n'
        'd=$(dirname -- "$f") && mkdir -p -- "$d" || exit 2\n'
        't=$(mktemp "$d/.recall-tmp-XXXXXX") || exit 2\n'
        'if ! cat > "$t"; then rm -f -- "$t"; exit 2; fi\n'
        'if [ -e "$f" ]; then chmod --reference="$f" "$t" 2>/dev/null || true; else chmod 644 "$t"; fi\n'
        'mv -f -- "$t" "$f" || { rm -f -- "$t"; exit 2; }'
    )


def _write(session: TerminalSession, tool: str, display: str, path: str, content: str, assessment: Assessment,
           preapproved: bool = False, detail: str = ""):
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        return None, f"error: content is {len(data)} bytes; the limit is {MAX_FILE_BYTES}"
    result, denial = session.run(tool, display, _write_script(path), stdin=data, assessment=assessment, preapproved=preapproved,
                                  detail=detail, path=target_path(session, path))
    if denial:
        return None, denial
    if result.timed_out:
        return None, "error: writing timed out"
    if result.exit_code != 0:
        return None, _problem(result.exit_code, path, result.stderr)
    return len(data), None


def sandbox_write(session: TerminalSession, path: str, content: str) -> str:
    path = _checked(session, "sandbox_write", path)
    size, problem = _write(
        session, "sandbox_write", f"write {path} ({len(content.encode('utf-8'))} bytes)", path, content,
        protected_verdict(session, path, write=True) or assess_write(posixpath.normpath(_where(session, path))),
        detail="content:\n" + preview(content),
    )
    return problem or f"wrote {size} bytes to {path}"


# ---------------------------------------------------------------------- patch
def _lines_match(text: str, old: str) -> list[tuple[int, int]]:
    """Whitespace-tolerant matches of `old` in `text` as (start, end) character spans of whole lines."""
    target = [line.strip() for line in old.strip("\n").split("\n")]
    if not target or not any(target):
        return []
    lines = text.split("\n")
    offsets, pos = [], 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1
    spans = []
    for i in range(len(lines) - len(target) + 1):
        if all(lines[i + k].strip() == target[k] for k in range(len(target))):
            end = offsets[i + len(target) - 1] + len(lines[i + len(target) - 1])
            spans.append((offsets[i], end))
    return spans


def patch_text(text: str, old: str, new: str, replace_all: bool = False) -> tuple[str | None, str]:
    """(patched text, how it matched) or (None, error message)."""
    if not old:
        return None, "error: old must not be empty"
    if old == new:
        return None, "error: old and new are identical"
    crlf = "\r\n" in text and "\r\n" not in old
    if crlf:
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    count = text.count(old)
    if count == 1 or (count > 1 and replace_all):
        return text.replace(old, new), "exact" if count == 1 else f"exact, {count} replacements"
    if count > 1:
        return None, f"error: old matches {count} places; add surrounding lines to make it unique or set replace_all"
    work = text.replace("\r\n", "\n") if crlf else text
    spans = _lines_match(work, old.replace("\r\n", "\n") if crlf else old)
    if not spans:
        return None, "error: old was not found (also tried ignoring leading and trailing whitespace on each line)"
    if len(spans) > 1 and not replace_all:
        return None, f"error: old matches {len(spans)} places ignoring whitespace; add context or set replace_all"
    replacement = new.replace("\r\n", "\n") if crlf else new
    out, last = [], 0
    for start, end in spans:
        out.append(work[last:start])
        out.append(replacement.strip("\n"))
        last = end
    out.append(work[last:])
    result = "".join(out)
    return (result.replace("\n", "\r\n") if crlf else result), "whitespace-tolerant"


def sandbox_patch(session: TerminalSession, path: str, old: str, new: str, replace_all: bool = False) -> str:
    path = _checked(session, "sandbox_patch", path)
    fetch = "\n".join([
        "f=" + shell_path(path),
        f'[ -e "$f" ] || exit {_MISSING}',
        f'[ -d "$f" ] && exit {_IS_DIR}',
        f'[ "$(wc -c < "$f")" -le {MAX_FILE_BYTES} ] || exit 2',
        _BINARY_CHECK,
        'cat -- "$f"',
    ])
    assessment = protected_verdict(session, path, write=True) or assess_write(posixpath.normpath(_where(session, path)))
    detail = f"replace{' all' if replace_all else ''}:\n{preview(old)}\nwith:\n{preview(new)}"
    result, denial = session.run(
        "sandbox_patch", f"patch {path}", fetch, assessment=assessment, detail=detail, path=target_path(session, path),
    )
    if denial:
        return denial
    if result.timed_out or result.exit_code != 0:
        return "error: reading timed out" if result.timed_out else _problem(result.exit_code, path, result.stderr)
    original = result.stdout
    patched, how = patch_text(original, old, new, replace_all)
    if patched is None:
        return how
    # the approval for this path was just given; the write below is part of the same operation
    size, problem = _write_after_patch(session, path, patched, assessment)
    if problem:
        return problem
    diff = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), patched.splitlines(keepends=True), f"a/{path}", f"b/{path}",
    ))
    text, _ = clean(diff or "(no textual change)", session.output_limit)
    return f"patched {path} ({how})\n{text}"


def _write_after_patch(session: TerminalSession, path: str, content: str, assessment: Assessment):
    """Write the patched file under the approval that already covered the fetch (one prompt per patch)."""
    return _write(session, "sandbox_patch", f"write patched {path}", path, content, assessment, preapproved=True)


# ---------------------------------------------------------------------- search
def sandbox_search(session: TerminalSession, pattern: str, path: str = ".", glob: str | None = None,
                   files: bool = False) -> str:
    path = _checked(session, "sandbox_search", path or ".")
    if not isinstance(pattern, str) or not pattern or "\x00" in pattern:
        session.audit_refusal("sandbox_search", "sandbox_search (invalid pattern)", "invalid pattern")
        raise ValueError("pattern must be a non-empty string without NUL bytes")
    if glob is not None and "\x00" in glob:
        session.audit_refusal("sandbox_search", "sandbox_search (invalid glob)", "invalid glob")
        raise ValueError("glob must not contain a NUL byte")
    p = shell_path(path)
    cap = MAX_SEARCH_RESULTS + 1
    protected = session.protected()
    skip_names, skip_dirs = protected.search_excludes() if protected else ([], [])
    if files:
        name = f" -name {shlex.quote(pattern)}"
        hide = "".join(f" -not -name {shlex.quote(n)}" for n in skip_names)
        hide += "".join(f" -not -path {shlex.quote('*/' + d + '/*')}" for d in skip_dirs)
        script = (
            f"f={p}\n[ -e \"$f\" ] || exit {_MISSING}\n"
            f"find \"$f\"{name}{hide} -not -path '*/.git/*' 2>/dev/null | head -n {cap}"
        )
        display = f"find files named {pattern} in {path}"
    else:
        include = f" --include={shlex.quote(glob)}" if glob else ""
        include += "".join(f" --exclude={shlex.quote(n)}" for n in skip_names)
        include += "".join(f" --exclude-dir={shlex.quote(d)}" for d in skip_dirs)
        script = (
            f"f={p}\n[ -e \"$f\" ] || exit {_MISSING}\n"
            f"grep -rIn -E --exclude-dir=.git{include} -e {shlex.quote(pattern)} -- \"$f\" | head -n {cap}"
        )
        display = f"search {pattern} in {path}" + (f" ({glob})" if glob else "")
    result, denial = session.run(
        "sandbox_search", display, script, assessment=protected_verdict(session, path) or _NORMAL,
        path=target_path(session, path),
    )
    if denial:
        return denial
    if result.timed_out:
        return "error: the search timed out; narrow the path or the pattern"
    if result.exit_code == _MISSING:
        return _problem(_MISSING, path, "")
    lines = [line for line in result.stdout.splitlines() if line]
    if protected is not None:  # belt and braces: whatever the excludes missed (symlinks, configured patterns)
        lines = protected.filter_results(lines, session.local_cwd(), grep_format=not files)
    if not lines:
        if result.stderr.strip():
            return "error: " + clean(result.stderr, 500)[0]
        return "no matches"
    more = len(lines) > MAX_SEARCH_RESULTS
    text, _ = clean("\n".join(lines[:MAX_SEARCH_RESULTS]), session.output_limit)
    return text + (f"\n[showing the first {MAX_SEARCH_RESULTS} results; narrow the search]" if more else "")
