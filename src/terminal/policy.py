"""Command safety policy: a hardline that no approval mode can bypass, and dangerous patterns.

`classify(command)` returns `'hardline'` (always denied), `'dangerous'` (asks for approval in `smart`
mode) or `'normal'`. A command line is split into its segments (`&&`, `||`, `;`, `|`, `&`, newlines,
subshells, `$(...)`, backticks, `bash -c '...'`), every segment is judged on its own parsed words (so
quoted text such as `echo "rm -rf /"` is just text) and the worst verdict wins. The lists are
deliberately conservative: a false "dangerous" costs one tap, a false "normal" costs a sandbox.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import Literal

Level = Literal["normal", "dangerous", "hardline"]
_RANK = {"normal": 0, "dangerous": 1, "hardline": 2}

# directories whose contents an agent has no business rewriting
SYSTEM_DIRS = ("/etc", "/boot", "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/sys", "/proc", "/dev", "/root")
# `rm -rf` of any of these (or of / and ~) is refused outright
_HARD_RM_TARGETS = frozenset({
    "/", "~", "$HOME", "${HOME}", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/boot",
    "/var", "/home", "/root", "/dev", "/proc", "/sys", "/opt", "/srv", "/mnt", "/media",
})
_BLOCK_DEVICE = r"/dev/(?:sd[a-z]|hd[a-z]|vd[a-z]|xvd[a-z]|nvme\d|mmcblk\d|loop\d|md\d|dm-\d|disk\d)"
_BLOCK_DEVICE_RE = re.compile(r"^" + _BLOCK_DEVICE)
_DEVICE_REDIRECT = re.compile(r">\s*['\"]?" + _BLOCK_DEVICE)
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{|(\w+)\s*\(\s*\)\s*\{\s*\1\s*\|\s*\1\s*&")
_CURL_PIPE = re.compile(
    r"\b(?:curl|wget|fetch)\b[^|;&\n]*\|\s*(?:sudo\s+(?:-\S+\s+)*)?(?:\S*/)?(?:ba|z|da|k|c|tc|fi)?sh\b"
    r"|\b(?:curl|wget|fetch)\b[^|;&\n]*\|\s*(?:sudo\s+)?(?:\S*/)?(?:python[\d.]*|perl|ruby|node|php)\b"
    r"|\b(?:ba|z|da|k)?sh\b[^;&|\n]*<\s*\(\s*(?:curl|wget)\b"
    r"|\b(?:ba|z|da|k)?sh\b\s+-c\s+[\"']?\$\(\s*(?:curl|wget)\b"
)
_SQL = re.compile(r"\b(?:drop\s+(?:table|database|schema|index|view|user|role)|delete\s+from|truncate(?:\s+table)?)\b", re.I)
_SYSTEM_REDIRECT = re.compile(
    r">>?\s*['\"]?(?:" + "|".join(re.escape(d) for d in SYSTEM_DIRS if d != "/dev") + r")(?:/|['\"\s]|$)"
)
_WRAPPERS_NOARG = {"env", "nohup", "command", "exec", "builtin", "setsid", "time", "xargs", "unbuffer", "stdbuf", "nice",
                   "ionice", "timeout", "doas", "sudo", "pkexec", "chrt", "watch"}
_KEYWORDS = {"{", "!", "then", "do", "else", "elif", "if", "while", "until", "}", "fi", "done"}
_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "ash"}
_MAX_DEPTH = 4


@dataclass(frozen=True)
class Assessment:
    level: Level
    reason: str = ""


_NORMAL = Assessment("normal")


def _worse(a: Assessment, b: Assessment) -> Assessment:
    return b if _RANK[b.level] > _RANK[a.level] else a


# ---------------------------------------------------------------------- splitting


@dataclass
class _Segment:
    text: str
    redirects: list[int]  # offsets in `text` just after each unquoted `>` / `>>`


def _matching_paren(text: str, start: int) -> int:
    """Index of the `)` closing the `(` at `start` (end of text when unbalanced)."""
    depth = 0
    quote = ""
    i = start
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 1
            elif c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text)


def split_command(command: str) -> tuple[list[_Segment], list[str]]:
    """Split a command line into simple-command segments and the inner commands of substitutions."""
    segments: list[_Segment] = []
    inner: list[str] = []
    buf: list[str] = []
    redirects: list[int] = []
    quote = ""
    i, n = 0, len(command)

    def flush() -> None:
        nonlocal buf, redirects
        text = "".join(buf)
        if text.strip():
            lead = len(text) - len(text.lstrip())
            segments.append(_Segment(text.strip(), [r - lead for r in redirects if r - lead >= 0]))
        buf, redirects = [], []

    def substitution(start: int) -> int:
        """Handle `$(`/`<(`/`>(` at `start`; return the index after the closing paren."""
        close = _matching_paren(command, start + 1)
        inner.append(command[start + 2:close])
        buf.append("SUBST")
        return close + 1

    while i < n:
        c = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if quote == "'":
            buf.append(c)
            if c == "'":
                quote = ""
            i += 1
        elif c == "\\" and nxt:
            buf.extend((c, nxt))
            i += 2
        elif quote == '"':
            if c == '"':
                quote = ""
                buf.append(c)
                i += 1
            elif c == "$" and nxt == "(":
                i = substitution(i)
            elif c == "`":
                end = command.find("`", i + 1)
                end = n if end < 0 else end
                inner.append(command[i + 1:end])
                buf.append("SUBST")
                i = end + 1
            else:
                buf.append(c)
                i += 1
        elif c in "'\"":
            quote = c
            buf.append(c)
            i += 1
        elif (c == "$" and nxt == "(") or (c in "<>" and nxt == "("):
            i = substitution(i)
        elif c == "`":
            end = command.find("`", i + 1)
            end = n if end < 0 else end
            inner.append(command[i + 1:end])
            buf.append("SUBST")
            i = end + 1
        elif c in "\n;":
            flush()
            i += 1
        elif c == "&":
            if nxt == "&":
                flush()
                i += 2
            elif buf and buf[-1] in "<>":  # `>&2`, `<&3`: a file-descriptor duplication
                buf.append(c)
                i += 1
            elif nxt == ">":  # `&> file`
                buf.append(c)
                i += 1
            else:
                flush()
                i += 1
        elif c == "|":
            flush()
            i += 2 if nxt == "|" else 1
        elif c in "()":
            flush()
            i += 1
        elif c == ">":
            j = i + 2 if nxt == ">" else i + 1
            buf.extend(command[i:j])
            redirects.append(len(buf))
            i = j
        else:
            buf.append(c)
            i += 1
    flush()
    return segments, inner


def _tokens(text: str) -> list[str]:
    try:
        return shlex.split(text, posix=True)
    except ValueError:
        return text.split()


def _redirect_targets(segment: _Segment) -> list[str]:
    targets = []
    for offset in segment.redirects:
        rest = segment.text[offset:].lstrip()
        if rest.startswith("&") or not rest:
            continue
        words = _tokens(rest)
        if words:
            targets.append(words[0])
    return targets


# ---------------------------------------------------------------------- word helpers


def _flags(args: list[str]) -> tuple[set[str], set[str]]:
    """(single-letter flags, long flags) of the options in `args` (stops at `--`)."""
    short: set[str] = set()
    long: set[str] = set()
    for arg in args:
        if arg == "--":
            break
        if arg.startswith("--"):
            long.add(arg.split("=", 1)[0])
        elif arg.startswith("-") and len(arg) > 1 and not arg[1:].isdigit():
            short.update(arg[1:])
    return short, long


def _operands(args: list[str]) -> list[str]:
    out: list[str] = []
    done = False
    for arg in args:
        if not done and arg == "--":
            done = True
        elif done or not arg.startswith("-") or arg == "-":
            out.append(arg)
    return out


def _norm(path: str) -> str:
    """A target in canonical form: `/./etc//` -> `/etc`, `/*` -> `/`, `~/` -> `~`."""
    if path.endswith("/*"):
        path = path[:-2] or "/"
    if path.startswith(("/", )):
        path = posixpath.normpath(path)
        return "/" if path.startswith("//") and path.strip("/") == "" else path
    if path in ("~/", "~/.", "$HOME/", "${HOME}/") or path.rstrip("/") in ("~", "$HOME", "${HOME}"):
        return path.rstrip("/").rstrip("/.") or path
    return path


def _in_system_dir(path: str) -> bool:
    path = _norm(path) if path.startswith("/") else path
    return any(path == d or path.startswith(d + "/") for d in SYSTEM_DIRS)


def _is_root_like(path: str) -> bool:
    return _norm(path) in _HARD_RM_TARGETS


def _is_device(path: str) -> bool:
    return bool(_BLOCK_DEVICE_RE.match(path))


# ---------------------------------------------------------------------- segment rules


def _assess_words(words: list[str], redirects: list[str], workdir: str | None, depth: int) -> Assessment:
    result = _NORMAL
    # redirections first: `> /dev/sda` is the worst thing a trivial command can do
    for target in redirects:
        if _is_device(target):
            return Assessment("hardline", f"writes to the block device {target}")
        result = _worse(result, _redirect_verdict(target, workdir))
    while words and re.match(r"^[A-Za-z_]\w*=", words[0]):  # leading VAR=value assignments
        words = words[1:]
    while words and words[0] in _KEYWORDS:
        words = words[1:]
    if not words:
        return result
    name = posixpath.basename(words[0])
    args = words[1:]
    if name in ("sudo", "doas", "pkexec", "su"):
        result = _worse(result, Assessment("dangerous", f"{name}: runs with other privileges"))
        if name == "su":
            return result
    if name in _WRAPPERS_NOARG and depth < _MAX_DEPTH:
        rest = args
        while rest and (rest[0].startswith("-") or (name == "env" and "=" in rest[0])):
            rest = rest[1:]
        while name in ("timeout", "nice", "ionice", "stdbuf", "chrt") and rest and (
                _DURATION.match(rest[0]) or rest[0].startswith("-")):
            rest = rest[1:]
        return _worse(result, _assess_words(rest, [], workdir, depth + 1))
    if name in _SHELLS | {"eval"} and depth < _MAX_DEPTH:
        script = None
        if name == "eval":
            script = " ".join(args)
        elif "-c" in args and args.index("-c") + 1 < len(args):
            script = args[args.index("-c") + 1]
        if script is not None:
            result = _worse(result, _assess(script, workdir, depth + 1))
    return _worse(result, _command_rules(name, args, workdir))


def _redirect_verdict(target: str, workdir: str | None) -> Assessment:
    if target in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty") or target.startswith("/dev/fd/"):
        return _NORMAL
    absolute = target.startswith(("/", "~", "$HOME", "${HOME}"))
    if not absolute:
        return _NORMAL
    if _in_system_dir(target):
        return Assessment("dangerous", f"redirect writes into the system path {target}")
    if target.startswith("/tmp/") or target.startswith("/var/tmp/"):
        return _NORMAL
    base = (workdir or "").rstrip("/")
    if base and (target == base or target.startswith(base + "/")):
        return _NORMAL
    return Assessment("dangerous", f"redirect writes to the absolute path {target} outside the working directory")


def _command_rules(name: str, args: list[str], workdir: str | None) -> Assessment:  # noqa: C901 - a flat rule table
    short, long = _flags(args)
    operands = _operands(args)
    sub = [a for a in args if not a.startswith("-")]
    if name == "rm":
        recursive = bool(short & {"r", "R"} or "--recursive" in long)
        if "--no-preserve-root" in long:
            return Assessment("hardline", "rm --no-preserve-root")
        if recursive and any(_is_root_like(t) for t in operands):
            return Assessment("hardline", "recursive delete of /, ~ or a system directory")
        if recursive:
            return Assessment("dangerous", "recursive delete")
        if any(_in_system_dir(t) for t in operands):
            return Assessment("dangerous", "deletes a system path")
    elif name == "shred" or name == "wipefs":
        if any(_is_device(t) for t in operands):
            return Assessment("hardline", f"{name} on a block device")
        return Assessment("dangerous", f"{name} destroys data")
    elif name == "mkfs" or name.startswith("mkfs.") or name in ("mke2fs", "mkswap", "fdisk", "sfdisk", "parted", "sgdisk"):
        if any(_is_device(t) for t in operands):
            return Assessment("hardline", f"{name} on a block device")
        return Assessment("dangerous", f"{name} formats or partitions")
    elif name == "dd":
        targets = [a[3:] for a in args if a.startswith("of=")]
        if any(_is_device(t) for t in targets):
            return Assessment("hardline", "dd writing to a block device")
        return Assessment("dangerous", "dd copies raw data")
    elif name in ("shutdown", "reboot", "halt", "poweroff"):
        return Assessment("hardline", f"{name} would stop the machine")
    elif name in ("init", "telinit") and sub[:1] and sub[0] in ("0", "6"):
        return Assessment("hardline", f"{name} {sub[0]} would stop the machine")
    elif name == "systemctl":
        if sub[:1] and sub[0] in ("poweroff", "reboot", "halt", "kexec", "suspend", "hibernate"):
            return Assessment("hardline", f"systemctl {sub[0]} would stop the machine")
        if sub[:1] and sub[0] in ("stop", "disable", "mask", "kill", "restart", "isolate"):
            return Assessment("dangerous", f"systemctl {sub[0]}")
    elif name == "service" and sub[1:2] and sub[1] in ("stop", "restart"):
        return Assessment("dangerous", f"service {sub[1]}")
    elif name in ("chmod", "chown", "chgrp"):
        recursive = bool(short & {"R"} or "--recursive" in long)
        if recursive and any(_is_root_like(t) for t in operands[1:]):
            return Assessment("hardline", f"recursive {name} on / or a system directory")
        if name in ("chown", "chgrp"):
            return Assessment("dangerous", f"{name} changes ownership")
        mode = operands[0] if operands else ""
        if re.fullmatch(r"[0-7]?777|[ugoa]*\+[rwxXst]*w[rwxXst]*", mode) and (mode.endswith("777") or "o" in mode or "a" in mode or mode.startswith("+")):
            return Assessment("dangerous", "makes files world-writable")
        if recursive:
            return Assessment("dangerous", "recursive chmod")
    elif name == "find":
        paths = [a for a in args if not a.startswith(("-", "(", "!"))][:1]
        deleting = "-delete" in args or ("-exec" in args and any(posixpath.basename(a) == "rm" for a in args))
        if deleting and paths and _is_root_like(paths[0]):
            return Assessment("hardline", "find / -delete")
        if deleting:
            return Assessment("dangerous", "find deletes files")
    elif name in ("tee", "cp", "mv", "install", "ln", "truncate"):
        if any(_is_device(t) for t in operands):
            return Assessment("hardline", f"{name} writing to a block device")
        if name in ("mv", "cp", "install", "ln", "tee"):
            targets = operands if name in ("tee", "mv") else operands[-1:]
            if any(_in_system_dir(t) for t in targets):
                return Assessment("dangerous", f"{name} touches a system path")
            if name == "mv" and any(_is_root_like(t) for t in operands):
                return Assessment("hardline", "mv of /, ~ or a system directory")
    elif name == "git":
        verb = sub[0] if sub else ""
        if verb == "push" and ("--force" in long or "--force-with-lease" in long or "f" in short or any(
                a.startswith("+") for a in sub[1:])):
            return Assessment("dangerous", "git push --force")
        if verb == "reset" and "--hard" in long:
            return Assessment("dangerous", "git reset --hard")
        if verb == "clean" and ("f" in short or "--force" in long):
            return Assessment("dangerous", "git clean -f")
        if verb == "branch" and "D" in short:
            return Assessment("dangerous", "git branch -D")
    elif name in ("kill", "killall"):
        if name == "killall" or "9" in short or "KILL" in short or any(a in ("-KILL", "-SIGKILL", "-9", "-s9") for a in args):
            return Assessment("dangerous", f"{name} forcibly stops processes")
        if any(a in ("KILL", "9") for a in sub[:1]) and "s" in short:
            return Assessment("dangerous", "kill -s KILL")
    elif name in ("pkill", "skill"):
        return Assessment("dangerous", f"{name} stops processes by name")
    elif name in ("mount", "umount", "losetup"):
        return Assessment("dangerous", f"{name} changes mounts")
    elif name in ("iptables", "ip6tables", "nft", "ufw", "firewall-cmd", "ebtables"):
        return Assessment("dangerous", f"{name} changes the firewall")
    elif name == "crontab" and "r" in short:
        return Assessment("dangerous", "crontab -r removes every job")
    elif name in ("ssh", "scp", "sftp", "sshpass", "ssh-copy-id"):
        return Assessment("dangerous", f"{name} from inside the sandbox")
    elif name in ("npm", "pnpm", "yarn", "bun"):
        if sub[:1] and sub[0] in ("install", "i", "add") and ("g" in short or "--global" in long or "--location=global" in args):
            return Assessment("dangerous", f"{name} global install")
        if name == "yarn" and sub[:2] == ["global", "add"]:
            return Assessment("dangerous", "yarn global add")
    elif name in ("pip", "pip3", "pipx", "uv") or (re.fullmatch(r"python[\d.]*", name) and args[:2] == ["-m", "pip"]):
        if "--break-system-packages" in long or "--global" in long or "--system" in long:
            return Assessment("dangerous", "pip install into the system Python")
    elif name == "docker" or name == "podman":
        if any(w in ("rm", "rmi", "prune", "kill") for w in sub[:3]):
            return Assessment("dangerous", f"{name} removes containers or images")
    elif name in ("apt", "apt-get", "dpkg", "yum", "dnf", "apk", "pacman", "zypper", "brew", "snap"):
        if any(w in ("remove", "purge", "autoremove", "erase", "del", "-R", "-Rs", "uninstall") for w in sub[:2] + args[:1]):
            return Assessment("dangerous", f"{name} removes packages")
    elif name in ("passwd", "usermod", "userdel", "groupdel", "visudo", "chpasswd", "useradd"):
        return Assessment("dangerous", f"{name} changes accounts")
    return _NORMAL


# ---------------------------------------------------------------------- entry points


def _assess(command: str, workdir: str | None, depth: int) -> Assessment:
    result = _NORMAL
    if _FORK_BOMB.search(command):
        return Assessment("hardline", "fork bomb")
    if _DEVICE_REDIRECT.search(command):
        return Assessment("hardline", "writes to a block device")
    if _CURL_PIPE.search(command):
        result = _worse(result, Assessment("dangerous", "pipes a download into an interpreter"))
    if _SQL.search(command):
        result = _worse(result, Assessment("dangerous", "destructive SQL (DROP / DELETE FROM / TRUNCATE)"))
    if _SYSTEM_REDIRECT.search(command):
        result = _worse(result, Assessment("dangerous", "redirect writes into a system directory"))
    segments, inner = split_command(command)
    for segment in segments:
        result = _worse(result, _assess_words(_tokens(segment.text), _redirect_targets(segment), workdir, depth))
        if result.level == "hardline":
            return result
    if depth < _MAX_DEPTH:
        for text in inner:
            result = _worse(result, _assess(text, workdir, depth + 1))
            if result.level == "hardline":
                return result
    return result


def assess(command: str, workdir: str | None = None) -> Assessment:
    """The verdict and a short human reason for a shell command line."""
    if "\x00" in command:
        return Assessment("hardline", "NUL byte in the command")
    return _assess(command, workdir, 0)


def classify(command: str, workdir: str | None = None) -> Level:
    """`'hardline'`, `'dangerous'` or `'normal'`; `workdir` lets absolute `>` targets inside it count as normal."""
    return assess(command, workdir).level


def assess_write(path: str, workdir: str | None = None) -> Assessment:
    """Verdict for a file tool writing `path` (block devices are hardline, system directories dangerous)."""
    if _is_device(path):
        return Assessment("hardline", f"writes to the block device {path}")
    if _in_system_dir(path):
        return Assessment("dangerous", f"writes into the system path {path}")
    return _NORMAL
