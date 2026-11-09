"""Protected paths of the `local` terminal backend: keys and secrets an agent must never read or use.

On a server the `local` backend runs commands as the same OS user as RECALL, so without this module an agent of one user
could read the SSH private key of another user's PC backend, `.config/secrets.env`, a `.env` file or the process
environment. `build_protected()` computes the set at call time (it scans `<config_dir>/*/user.yaml`, tolerant of broken
users) and the checks below apply to `type: local` backends ONLY (SSH backends are other machines):

- file tools (`sandbox_read|write|patch|search`): EXACT. The requested path is made absolute (`~`, relative to the
  workdir, `..`), checked as written and after resolving symlinks (`realpath`).
- `terminal` commands: BEST EFFORT. Every word, redirect target and `opt=value` of every parsed segment (also inside
  `$(...)`, backticks and `bash -c`) is resolved like the shell would and compared; glob words are matched against the
  protected files; obvious references (`keys/<file>`, `_ed25519`, `id_rsa`, `.env`, `secrets.env`, `/environ`,
  `.credentials.json`) are refused; recursive tools (tar, zip, rsync, cp -r, grep -r, find -exec...) may not start at a
  directory that contains protected files; interpreters and `ssh -F` are refused when the script or config they name
  mentions a protected path. A determined obfuscated command (a path built from pieces, a program written first) can
  bypass this scan; approval cards remain the second barrier (see docs/deployment/security.md).

Read-protected too: the per-user `mcp/` credential folders (`<config_dir>/<user>/mcp/**`).

WRITE-protected only (reading stays allowed: agents need `user.yaml` for admin work and the admin CLI never prints
password hashes): the audit trail (`terminal_audit.jsonl`), every `user.yaml`, agent yaml (`<config_dir>/<user>/agents/*.yaml`)
and `settings.yaml`, and `known_hosts` files. For shell commands a write is a redirect target, `tee`, `cp`/`mv`/`install`/`ln`/
`rsync`/`scp` destinations, `dd of=`, `sed -i`/`perl -i`, `rm`, `truncate`, `touch`, `chmod`, `chown`, `shred`.

Not protected on purpose: public keys (`*.pub`), `.env.example`-style templates and the *names* in a keys directory.

A `user.yaml` that cannot be parsed fails CLOSED: the key files it names are still found by a text scan, and the
failure is logged (never silently skipped).
"""

from __future__ import annotations

import fnmatch
import glob as globlib
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.terminal.policy import (
    _KEYWORDS, _SHELLS, _WRAPPERS_NOARG, _flags, _operands, _redirect_targets, _tokens, split_command,
)
from src.utils.logger import get_logger

logger = get_logger("terminal")

KEYS_DIR_NAME = "keys"
PRIVATE_KEY_NAMES = frozenset({"id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"})
ENV_TEMPLATE_SUFFIXES = frozenset({"example", "sample", "template", "dist", "defaults"})
PUBLIC_NAMES = frozenset({"known_hosts", "known_hosts.old"})
AUDIT_NAME = "terminal_audit.jsonl"
MCP_DIR_NAME = "mcp"
_WRITE_ALL = frozenset({"tee", "mv", "rm", "truncate", "touch", "chmod", "chown", "chgrp", "shred", "unlink", "sponge"})
_WRITE_LAST = frozenset({"cp", "install", "ln", "rsync", "scp"})
_KEY_FILE_TEXT = re.compile(r"key_file\s*['\"]?\s*:\s*['\"]?([^\s'\",}\]]+)")
_PROC_ENVIRON = re.compile(r"^/proc/(?:self|thread-self|\d+)(?:/task/\d+)?/environ$")
_SSH_TOOLS = frozenset({"ssh", "scp", "sftp", "rsync", "ssh-add", "ssh-agent", "ssh-keygen", "ssh-copy-id", "sshpass"})
_ARCHIVERS = frozenset({"tar", "bsdtar", "zip", "7z", "7za", "7zr", "cpio", "pax", "rsync", "scp", "rg", "ag", "ack"})
_GREPS = frozenset({"grep", "egrep", "fgrep", "zgrep"})
_COPIERS = frozenset({"cp", "mv", "install"})
_MAX_SCAN_BYTES = 262_144
_MAX_WALK = 500
_MAX_DEPTH = 4
_GLOB_CHARS = re.compile(r"[*?\[]")
_MAX_GLOB_MATCHES = 5000

# obvious references inside a command line (checked on the raw text, so quoting tricks do not matter)
_REF_KEYS = re.compile(r"(?:^|[\s'\"=:/])keys/([^\s'\";|&)<>]+)")
_REF_ENV = re.compile(r"(?:^|[\s'\"=:/])(\.env(?:\.[\w-]+)?)(?=$|[\s/'\";|&)<>])")
_REF_PLAIN = (
    (re.compile(r"(?:id_rsa|id_ed25519|id_ecdsa|id_dsa|_ed25519)(?!\.pub\b)"), "an SSH private key name"),
    (re.compile(r"secrets\.env"), "the secrets file"),
    (re.compile(r"/environ\b"), "a process environment file"),
    (re.compile(r"\.credentials\.json"), "a login credentials file"),
)


def _fold(path: str) -> str:
    """Canonical comparison form: forward slashes, case-folded on Windows."""
    path = path.replace("\\", "/")
    return path.lower() if os.name == "nt" else path


def _is_abs(path: str) -> bool:
    return path.startswith("/") or os.path.isabs(path)


def _absolute(path: str, cwd: str) -> str:
    path = os.path.expanduser(path)
    if _is_abs(path):
        return path
    return os.path.join(os.path.expanduser(cwd or "."), path)


def forms(path: str, cwd: str) -> list[str]:
    """The comparison forms of `path`: lexically normalised, and with symlinks resolved."""
    absolute = _absolute(path, cwd)
    out = [_fold(os.path.normpath(absolute))]
    try:
        real = _fold(os.path.realpath(absolute))
    except (OSError, ValueError):
        real = out[0]
    if real not in out:
        out.append(real)
    return out


def _strip_trailing(path: str) -> str:
    return path.rstrip("/") or "/"


@dataclass
class ProtectedPaths:
    """The protected set at one moment. `keys` maps a folded private key path to its description."""

    config_dir: str = ""
    keys: dict[str, str] = field(default_factory=dict)
    public_files: set[str] = field(default_factory=set)
    secret_files: set[str] = field(default_factory=set)
    extra: list[str] = field(default_factory=list)   # folded absolute glob patterns from settings.yaml
    concrete: list[str] = field(default_factory=list)  # folded paths of the known protected files
    write_only: list[str] = field(default_factory=list)  # folded paths that may be read but never written
    repo_roots: list[str] = field(default_factory=list)  # folded roots whose settings.yaml is write-protected

    # ------------------------------------------------------------------ single paths
    def _rule(self, p: str) -> str | None:
        """Why the folded absolute path `p` is protected, else None."""
        for pattern in self.extra:  # the administrator's patterns win over the exemptions
            if _glob_hits_path(pattern, p):
                return f"matches the configured protected pattern {pattern}"
        base = p.rsplit("/", 1)[-1]
        if base.endswith(".pub") or base in PUBLIC_NAMES or p in self.public_files:
            return None
        if p in self.keys:
            return self.keys[p]
        if p in self.secret_files or base == "secrets.env":
            return "the secrets file"
        if base == ".env" or (base.startswith(".env.") and base.rsplit(".", 1)[-1] not in ENV_TEMPLATE_SUFFIXES):
            return "an environment file with secrets"
        if base in PRIVATE_KEY_NAMES:
            return "an SSH private key"
        if base == ".credentials.json":
            return "a login credentials file"
        if _PROC_ENVIRON.match(p):
            return "a process environment"
        if self.config_dir and p.startswith(self.config_dir + "/"):
            parents = p[len(self.config_dir) + 1:].split("/")[:-1]
            if KEYS_DIR_NAME in parents:
                return "a file in a keys directory"
            if len(parents) >= 2 and parents[1] == MCP_DIR_NAME:
                return "an MCP credentials folder"
        return None

    def _write_rule(self, p: str) -> str | None:
        """Why the folded absolute path `p` may be read but not written, else None."""
        base = p.rsplit("/", 1)[-1]
        if base == AUDIT_NAME:
            return "the terminal audit trail"
        if base == "user.yaml":
            return "a user configuration file"
        if base in PUBLIC_NAMES or p in self.public_files:
            return "a known_hosts file"
        if p in self.write_only:
            return "a configuration file"
        if base == "settings.yaml" and (any(p == root + "/settings.yaml" for root in self.repo_roots)
                                        or (self.config_dir and p.startswith(self.config_dir + "/"))):
            return "the settings file"
        if self.config_dir and p.startswith(self.config_dir + "/"):
            parts = p[len(self.config_dir) + 1:].split("/")
            if len(parts) == 3 and parts[1] == "agents" and base.endswith((".yaml", ".yml")):
                return "an agent configuration file"
        return None

    def reason(self, path: str, cwd: str = ".", *, write: bool = False) -> str | None:
        """Why `path` (as an agent wrote it) is protected, else None. Checked lexically and through symlinks.

        `write=True` adds the write-only rules (audit trail, user/agent/settings yaml, known_hosts).
        """
        try:
            for form in forms(path, cwd):
                why = self._rule(form) or (self._write_rule(form) if write else None)
                if why:
                    return why
        except (OSError, ValueError):
            return None
        return None

    # ------------------------------------------------------------------ directories and globs
    def contains_protected(self, directory: str, cwd: str = ".") -> bool:
        """True when `directory` is, or is an ancestor of, a known protected file."""
        for form in forms(directory, cwd):
            prefix = _strip_trailing(form)
            prefix = "" if prefix == "/" else prefix
            if any(c == prefix or c.startswith(prefix + "/") for c in self.concrete):
                return True
        return False

    def glob_reason(self, pattern: str, cwd: str = ".", *, write: bool = False) -> str | None:
        """Why a glob word expands to a protected file, else None.

        On the local backend the glob is resolved against the real filesystem (like the shell: `*` skips dotfiles) and
        refused only when an actual match is protected; the message names that match.
        """
        absolute = _absolute(pattern, cwd)
        try:
            matches = globlib.glob(absolute)[:_MAX_GLOB_MATCHES]
        except (OSError, ValueError):
            matches = []
        for match in matches:
            why = self.reason(match, cwd, write=write)
            if why:
                return f"{match} ({why})"
        folded = _fold(os.path.normpath(absolute))
        for probe in ("/proc/self/environ", "/proc/1/environ"):  # not on every host's disk: match by name
            if fnmatch.fnmatchcase(probe, folded):
                return f"{probe} (a process environment)"
        return None

    def name_pattern_reason(self, pattern: str) -> str | None:
        """For a glob that cannot be resolved (variables, substitutions): refuse only when it could match a protected
        NAME (`*.env`, `.env*`, `*_ed25519`, `keys/*`); never bare `*`, `*/` or `*.md`."""
        parts = [p for p in pattern.replace("\\", "/").split("/") if p]
        if not parts:
            return None
        last = parts[-1]
        if KEYS_DIR_NAME in parts[:-1] and _GLOB_CHARS.search(last):
            return f"the pattern {pattern} would expand to files in a keys directory"
        if len(re.sub(r"[*?\[\]!^-]", "", last)) < 3:  # too little literal text to point at a secret
            return None
        names = {".env", "secrets.env", ".credentials.json", *PRIVATE_KEY_NAMES}
        names.update(c.rsplit("/", 1)[-1] for c in self.concrete)
        for name in sorted(names):
            if not name or name.endswith(".pub") or name in PUBLIC_NAMES:
                continue
            if fnmatch.fnmatchcase(_fold(name), _fold(last)) and not (last.startswith("*") and name.startswith(".")):
                return f"the pattern {pattern} could match {name}, a protected file name"
        return None

    def search_excludes(self) -> tuple[list[str], list[str]]:
        """(file base names, directory names) a recursive search should skip."""
        names = {".env", "secrets.env", ".credentials.json", *PRIVATE_KEY_NAMES}
        names.update(c.rsplit("/", 1)[-1] for c in self.concrete)
        names = {n for n in names if n and not _GLOB_CHARS.search(n)}
        return sorted(names), [KEYS_DIR_NAME]

    def filter_results(self, lines: list[str], cwd: str, *, grep_format: bool) -> list[str]:
        """Drop result lines that name a protected file (`path:line:text` for grep, a bare path for find)."""
        kept = []
        for line in lines:
            candidate = line
            if grep_format:  # `path:lineno:text` (the path may hold a drive letter or colons)
                match = re.match(r"(.+?):\d+:", line)
                candidate = match.group(1) if match else line.split(":", 1)[0]
            if candidate and self.reason(candidate, cwd):
                continue
            kept.append(line)
        return kept

    # ------------------------------------------------------------------ commands
    def command_reason(self, command: str, cwd: str) -> str | None:
        """Why a shell command line touches a protected path (best effort), else None."""
        return self._command(command, cwd, 0)

    def _command(self, command: str, cwd: str, depth: int) -> str | None:
        why = self._text_reference(command)
        if why:
            return why
        segments, inner = split_command(command)
        here = cwd
        for segment in segments:
            words = _tokens(segment.text)
            why = self._words(words, _redirect_targets(segment), here, depth)
            if why:
                return why
            here = _after_cd(words, here)
        if depth < _MAX_DEPTH:
            for text in inner:
                why = self._command(text, here, depth + 1)
                if why:
                    return why
        return None

    def _text_reference(self, text: str) -> str | None:
        """Obvious references in raw text (a command line or a script the agent names)."""
        for match in _REF_KEYS.finditer(text):
            if not match.group(1).endswith(".pub") and match.group(1).rsplit("/", 1)[-1] not in PUBLIC_NAMES:
                return f"it names a file inside a keys directory (keys/{match.group(1)})"
        for match in _REF_ENV.finditer(text):
            if match.group(1).rsplit(".", 1)[-1] not in ENV_TEMPLATE_SUFFIXES or match.group(1) == ".env":
                return f"it names an environment file ({match.group(1)})"
        for pattern, label in _REF_PLAIN:
            hit = pattern.search(text)
            if hit:
                return f"it names {label} ({hit.group(0)})"
        folded = _fold(text)
        for path in self.concrete:
            if path in folded and self._rule(path):
                return f"it names {self._rule(path)} ({path})"
        return None

    def _words(self, words: list[str], redirects: list[str], cwd: str, depth: int) -> str | None:
        for target in redirects:
            why = self._candidate(target, cwd) or self._candidate(target, cwd, write=True)
            if why:
                return why
        while words and re.match(r"^[A-Za-z_]\w*=", words[0]):
            why = self._candidate(words[0].split("=", 1)[1], cwd)
            if why:
                return why
            words = words[1:]
        while words and words[0] in _KEYWORDS:
            words = words[1:]
        for _ in range(4):  # strip wrappers: env, sudo, timeout, nohup...
            if words and os.path.basename(words[0]) in _WRAPPERS_NOARG:
                words = words[1:]
                while words and (words[0].startswith("-") or "=" in words[0] and not _is_abs(words[0])
                                 or re.fullmatch(r"\d+(?:\.\d+)?[smhd]?", words[0])):
                    words = words[1:]
            else:
                break
        if not words:
            return None
        name = os.path.basename(words[0])
        args = words[1:]
        for index, word in enumerate(words):
            if index == 0 and "/" not in word and not _GLOB_CHARS.search(word):
                continue  # a bare command name is found on PATH, not in the working directory
            why = self._word(word, cwd)
            if why:
                return why
        why = self._ancestor_use(name, args, cwd)
        if why:
            return why
        for target in _write_targets(name, args):
            why = self._candidate(target, cwd, write=True)
            if why:
                return why
        why = self._named_files(name, args, cwd)
        if why:
            return why
        if name in _SHELLS and depth < _MAX_DEPTH and "-c" in args and args.index("-c") + 1 < len(args):
            return self._command(args[args.index("-c") + 1], cwd, depth + 1)
        if name == "eval" and depth < _MAX_DEPTH:
            return self._command(" ".join(args), cwd, depth + 1)
        return None

    def _word(self, word: str, cwd: str) -> str | None:
        candidates = [word]
        if word.startswith("-") and not word.startswith("--"):
            if len(word) > 2:
                candidates.append(word[2:])  # -i/path, -F/path
        if "=" in word:
            candidates.append(word.split("=", 1)[1])
        if "," in word or ":" in word:
            candidates.extend(re.split(r"[,:]", word))
        for candidate in candidates:
            why = self._candidate(candidate, cwd)
            if why:
                return why
        return None

    def _candidate(self, text: str, cwd: str, *, write: bool = False) -> str | None:
        if not text or text.startswith("-") and "/" not in text or "\x00" in text:
            return None
        home = os.path.expanduser("~")
        text = re.sub(r"^\$\{?HOME\}?(?=/|$)", lambda _m: home, text)
        unresolved = "$" in text or "SUBST" == text or (bool(_GLOB_CHARS.search(text)) and "SUBST" in text)
        if _GLOB_CHARS.search(text):
            if unresolved or (isinstance(cwd, _UnknownCwd) and not _is_abs(text)):
                return self.name_pattern_reason(text)
            why = self.glob_reason(text, cwd, write=write)
            return f"the pattern {text} expands to {why}" if why else None
        if unresolved:
            return None
        why = self.reason(text, cwd, write=write)
        return f"{text} is {why}" if why else None

    def _ancestor_use(self, name: str, args: list[str], cwd: str) -> str | None:
        """A recursive reader started at a directory that contains protected files."""
        short, long = _flags(args)
        recursive = (
            name in _ARCHIVERS
            or (name in _GREPS and (short & {"r", "R"} or "--recursive" in long or "--dereference-recursive" in long
                                    or any(a in ("-d", "--directories=recurse") for a in args)))
            or (name in _COPIERS and (short & {"r", "R", "a"} or "--recursive" in long or "--archive" in long))
            or (name == "find" and any(a in ("-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf") for a in args))
        )
        if not recursive:
            return None
        operands = [a for a in args if not a.startswith("-")] or (["."] if name in _GREPS | {"rg", "ag", "ack", "find"} else [])
        for operand in operands:
            if _GLOB_CHARS.search(operand) or "$" in operand:
                continue
            if os.path.isdir(_absolute(operand, cwd)) and self.contains_protected(operand, cwd):
                return f"{name} would recurse over {operand}, which contains protected files; name narrower paths"
        return None

    def _named_files(self, name: str, args: list[str], cwd: str) -> str | None:
        """`ssh -F config` and `bash script`: a file the agent wrote must not carry a protected path either."""
        targets: list[str] = []
        if name in _SSH_TOOLS:
            targets = [args[i + 1] for i, a in enumerate(args[:-1]) if a == "-F"]
            targets += [a[2:] for a in args if a.startswith("-F") and len(a) > 2]
        elif name in _SHELLS or name in ("source", ".") or re.fullmatch(r"python[\d.]*|perl|ruby|node|php", name):
            operands = [a for a in args if not a.startswith("-")]
            targets = operands[:1]
        for target in targets:
            path = _absolute(target, cwd)
            try:
                if os.path.isfile(path) and os.path.getsize(path) <= _MAX_SCAN_BYTES:
                    text = Path(path).read_text(encoding="utf-8", errors="replace")
                    why = self._text_reference(text)
                    if why:
                        return f"{target} is read by {name} and {why}"
                    if name in _SSH_TOOLS and re.search(r"(?im)^\s*identityfile\b", text):
                        return f"{target} configures an IdentityFile for {name}"
            except OSError:
                continue
        return None


def _write_targets(name: str, args: list[str]) -> list[str]:
    """The words of a command that name files it writes, replaces or deletes (best effort)."""
    short, long = _flags(args)
    operands = _operands(args)
    if name in _WRITE_ALL:
        return operands
    if name in _WRITE_LAST:
        targets = operands[-1:]
        for index, arg in enumerate(args):
            if arg in ("-t", "--target-directory") and index + 1 < len(args):
                targets.append(args[index + 1])
            elif arg.startswith("--target-directory="):
                targets.append(arg.split("=", 1)[1])
        return targets
    if name == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    if name in ("sed", "perl", "ruby") and ("i" in short or any(f.startswith("--in-place") for f in long)):
        return operands
    return []


class _UnknownCwd(str):
    """A working directory that a `cd` with a variable or substitution made unknowable (relative globs cannot be resolved)."""


def _after_cd(words: list[str], cwd: str) -> str:
    """The directory after a simple `cd <dir>` segment, as far as it can be determined."""
    if not words or words[0] != "cd":
        return cwd
    operands = [w for w in words[1:] if w not in ("-L", "-P", "--")]
    if len(operands) != 1:
        return cwd if not operands else _UnknownCwd(cwd)
    target = operands[0]
    if "$" in target or "SUBST" in target or _GLOB_CHARS.search(target) or target == "-":
        return _UnknownCwd(cwd)
    return os.path.normpath(_absolute(target, cwd))


def _glob_hits_path(pattern: str, path: str) -> bool:
    """A configured pattern matches `path` or one of its ancestor directories."""
    parts = path.split("/")
    for i in range(2, len(parts) + 1):
        if fnmatch.fnmatchcase("/".join(parts[:i]), pattern):
            return True
    return False


# ---------------------------------------------------------------------- building the set
def _ssh_entries(config_dir: Path) -> list[tuple[str, str, str, str]]:
    """(user dir, backend name, key_file, known_hosts_file) of every SSH backend.

    Fails CLOSED: a `user.yaml` that cannot be parsed still protects every `key_file:` its text names (and is logged);
    one that cannot be read at all is logged as an error. A user is never silently dropped from the set.
    """
    entries = []
    try:
        users = sorted(p for p in config_dir.iterdir() if p.is_dir())
    except OSError:
        return entries
    for user_dir in users:
        yaml_file = user_dir / "user.yaml"
        if not yaml_file.exists():
            continue
        broken = ""
        try:
            text = yaml_file.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
            broken = type(exc).__name__
            try:
                text = yaml_file.read_bytes().decode("utf-8", errors="replace")
            except OSError:
                logger.error("protected paths: %s cannot be read; the SSH keys it names cannot be protected", yaml_file)
                continue
        if broken:
            logger.error("protected paths: %s cannot be parsed (%s); protecting every key_file it names", yaml_file, broken)
            for index, match in enumerate(_KEY_FILE_TEXT.finditer(text)):
                entries.append((user_dir.name, f"unparsed-{index + 1}", match.group(1).strip(), ""))
            continue
        backends = data.get("terminal_backends") if isinstance(data, dict) else None
        if not isinstance(backends, dict):
            continue
        for name, cfg in backends.items():
            if isinstance(cfg, dict) and cfg.get("type") == "ssh" and isinstance(cfg.get("key_file"), str) and cfg["key_file"].strip():
                hosts = cfg.get("known_hosts_file")
                entries.append((user_dir.name, str(name), cfg["key_file"].strip(), hosts if isinstance(hosts, str) else ""))
    return entries


def _walk_files(directory: Path) -> list[Path]:
    out: list[Path] = []
    try:
        for root, _dirs, names in os.walk(directory):
            for n in names:
                out.append(Path(root) / n)
                if len(out) >= _MAX_WALK:
                    return out
    except OSError:
        pass
    return out


def build_protected(extra: list[str] | None = None, *, config_dir: str | Path | None = None,
                    repo_root: str | Path | None = None, app_root: str | Path | None = "/app") -> ProtectedPaths:
    """The protected set now: computed on every call from the users' yaml files and settings."""
    if config_dir is None:
        from src.config import settings  # late: tests point CONFIG_DIR at a temp folder
        config_dir = settings.CONFIG_DIR
    repo = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    config = Path(config_dir).expanduser()
    config = config if config.is_absolute() else Path.cwd() / config
    config_folded = _fold(os.path.normpath(str(config)))
    result = ProtectedPaths(config_dir=config_folded)
    concrete: set[str] = set()

    def add_key(path: str, label: str) -> None:
        for form in forms(path, str(Path.cwd())):
            result.keys[form] = label
            concrete.add(form)

    for user, name, key_file, hosts in _ssh_entries(config):
        add_key(key_file, f"the SSH private key of backend '{name}' (user {user})")
        if hosts:
            result.public_files.update(forms(hosts, str(Path.cwd())))
    secrets = _fold(os.path.normpath(str(config / "secrets.env")))
    result.secret_files.add(secrets)
    concrete.add(secrets)
    for root in {repo, Path(app_root)} if app_root else {repo}:
        concrete.add(_fold(os.path.normpath(str(root / ".env"))))
    concrete.add(_fold(os.path.normpath(str(config / ".env"))))
    for keys_dir in (config / KEYS_DIR_NAME, *config.glob(f"*/{KEYS_DIR_NAME}")):
        for file in _walk_files(keys_dir):
            if not file.name.endswith(".pub") and file.name not in PUBLIC_NAMES:
                concrete.add(_fold(os.path.normpath(str(file))))
    for creds in config.glob("*/claude-login/.credentials.json"):
        concrete.add(_fold(os.path.normpath(str(creds))))
    for mcp_dir in config.glob(f"*/{MCP_DIR_NAME}"):
        for file in _walk_files(mcp_dir):
            concrete.add(_fold(os.path.normpath(str(file))))
    result.repo_roots = sorted({_fold(os.path.normpath(str(root))) for root in (repo, Path(app_root)) if root} if app_root else
                               {_fold(os.path.normpath(str(repo)))})
    writes = {_fold(os.path.normpath(str(config / "settings.yaml")))}
    writes.update(_fold(os.path.normpath(str(p))) for p in config.glob("*/user.yaml"))
    writes.update(_fold(os.path.normpath(str(p))) for pat in ("*.yaml", "*.yml") for p in config.glob(f"*/agents/{pat}"))
    result.write_only = sorted(writes)
    for pattern in extra or []:
        text = os.path.expanduser(pattern.strip())
        if not text:
            continue
        absolute = text if _is_abs(text) else str(repo / text)
        folded = _fold(os.path.normpath(absolute))
        result.extra.append(folded)
        if not _GLOB_CHARS.search(absolute):
            concrete.add(folded)
            real = _fold(os.path.realpath(absolute))
            result.extra.append(real) if real != folded else None
        else:
            for hit in globlib.glob(absolute)[:_MAX_WALK]:
                concrete.add(_fold(os.path.normpath(hit)))
    result.concrete = sorted(concrete)
    return result


def protected_summary(extra: list[str] | None = None) -> str:
    """A one-line description of the current set for logs and diagnostics (paths only, never contents)."""
    current = build_protected(extra)
    return ", ".join(shlex.quote(p) for p in current.concrete)
