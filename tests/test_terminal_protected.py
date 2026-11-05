"""Protected paths of the local terminal backend: keys and secrets that no mode, tool or approval can reach."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from pydantic import ValidationError

from src.config import settings
from src.config.models import AgentTerminalBackend, AgentTerminalConfig, TerminalSettings
from src.terminal import files
from src.terminal.backends import Result
from src.terminal.policy import assess
from src.terminal.protected import build_protected
from src.terminal.session import TerminalSession
from terminal_helpers import FakeBackend, Responder, StubBroker, ssh_config  # noqa: F401

from src.terminal.context import RunContext, run_context  # noqa: E402


def write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class ProtectedCase(unittest.TestCase):
    """A temp 'app': .config with two users (one SSH backend each), a keys folder, a workdir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.cfg = self.root / ".config"
        self.work = self.root / "work"
        self.work.mkdir()
        self.key = write(self.cfg / "keys" / "pc_ed25519", "PRIVATE")
        self.pub = write(self.cfg / "keys" / "pc_ed25519.pub", "PUBLIC")
        self.hosts = write(self.cfg / "keys" / "known_hosts", "HOSTS")
        self.key2 = write(self.root / "elsewhere" / "second_key", "PRIVATE2")
        write(self.cfg / "owner" / "user.yaml", (
            "id: owner\nterminal_backends:\n"
            f"  pc: {{type: ssh, host: 192.0.2.1, user: me, key_file: {self.key.as_posix()}, "
            f"known_hosts_file: {self.hosts.as_posix()}}}\n  box: {{type: local, workdir: /app}}\n"
        ))
        # a user whose yaml is invalid for the models but still names a key; two that are plainly broken
        write(self.cfg / "bob" / "user.yaml", (
            "id: bob\nunknown_field: 1\nterminal_backends:\n"
            f"  other: {{type: ssh, host: 192.0.2.2, user: bob, key_file: {self.key2.as_posix()}, known_hosts_file: /nope}}\n"
        ))
        write(self.cfg / "broken" / "user.yaml", "terminal_backends: [unclosed\n  - : :")
        write(self.cfg / "odd" / "user.yaml", "terminal_backends: {a: {type: ssh, key_file: 5}, b: 7}\n")
        self.secrets = write(self.cfg / "secrets.env", "TOKEN=abc")
        self.creds = write(self.cfg / "owner" / "claude-login" / ".credentials.json", "{}")
        self.user_yaml = self.cfg / "owner" / "user.yaml"
        for patcher in (
            mock.patch.object(settings, "CONFIG_DIR", self.cfg),
            mock.patch.dict(os.environ, {"HOME": str(self.root), "USERPROFILE": str(self.root)}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.audit = self.root / "audit.jsonl"

    # ------------------------------------------------------------------ helpers
    def session(self, approvals: str = "off", *, local: bool = True, unattended: bool = False,
                protected_paths: list[str] | None = None, handler=None) -> TerminalSession:
        backend = FakeBackend(handler)
        return TerminalSession(
            backend,
            AgentTerminalConfig(backends={"box": AgentTerminalBackend(approvals=approvals, workdir=str(self.work))}),
            TerminalSettings(protected_paths=protected_paths or []), user_id="u", agent_id="a", name="box",
            audit_file=self.audit, unattended=unattended, local=local,
        )

    def run_cmd(self, session: TerminalSession, command: str, cwd: str | None = None):
        return session.run("terminal", command, command, cwd=cwd)

    def decisions(self) -> list[str]:
        if not self.audit.exists():
            return []
        return [json.loads(line)["decision"] for line in self.audit.read_text(encoding="utf-8").splitlines()]

    def assertDenied(self, session: TerminalSession, command: str, cwd: str | None = None) -> None:
        before = len(session.backend.calls)
        result, denial = self.run_cmd(session, command, cwd)
        self.assertIsNone(result, command)
        self.assertIn("protected path", denial or "", command)
        self.assertEqual(len(session.backend.calls), before, command)

    def assertRuns(self, session: TerminalSession, command: str, cwd: str | None = None) -> None:
        result, denial = self.run_cmd(session, command, cwd)
        self.assertIsNone(denial, command)
        self.assertIsNotNone(result, command)


class ProtectedSetTests(ProtectedCase):
    def test_set_is_built_from_every_users_ssh_backends(self) -> None:
        prot = build_protected()
        self.assertIsNotNone(prot.reason(str(self.key)))
        self.assertIsNotNone(prot.reason(str(self.key2)))  # bob's yaml has an unknown field but still counts
        self.assertIn("backend 'pc'", prot.reason(str(self.key)))
        self.assertIn("backend 'other'", prot.reason(str(self.key2)))

    def test_fixed_members(self) -> None:
        prot = build_protected()
        for path in (self.secrets, self.cfg / "keys" / "anything", self.creds, self.root / ".env", self.work / ".env.local",
                     self.work / "id_rsa", Path("/proc/self/environ"), Path("/proc/123/environ")):
            self.assertIsNotNone(prot.reason(str(path)), path)

    def test_public_and_admin_data_stay_readable(self) -> None:
        prot = build_protected()
        for path in (self.pub, self.hosts, self.user_yaml, self.cfg / "keys", self.work / ".env.example",
                     self.work / "id_rsa.pub", Path("/proc/self/cmdline"), self.work / "notes.md"):
            self.assertIsNone(prot.reason(str(path)), path)

    def test_set_follows_the_yaml_at_call_time(self) -> None:
        new = self.root / "later_key"
        self.assertIsNone(build_protected().reason(str(new)))
        write(self.cfg / "late" / "user.yaml", f"terminal_backends:\n  x: {{type: ssh, key_file: {new.as_posix()}}}\n")
        self.assertIsNotNone(build_protected().reason(str(new)))

    def test_ssh_backend_names_do_not_protect_local_ones(self) -> None:
        self.assertIsNone(build_protected().reason(str(self.work)))

    def test_extra_patterns(self) -> None:
        extra = build_protected([f"{self.root.as_posix()}/private/*", "docs/secret-*.md"])
        self.assertIsNotNone(extra.reason(str(self.root / "private" / "a.txt")))
        self.assertIsNone(extra.reason(str(self.root / "public" / "a.txt")))
        repo = Path(__file__).resolve().parents[1]
        self.assertIsNotNone(extra.reason(str(repo / "docs" / "secret-plan.md")))
        folder = build_protected([f"{self.root.as_posix()}/vault"])
        self.assertIsNotNone(folder.reason(str(self.root / "vault" / "deep" / "x")))

    def test_settings_validation(self) -> None:
        self.assertEqual(TerminalSettings().protected_paths, [])
        self.assertEqual(TerminalSettings(protected_paths=[" a/* "]).protected_paths, ["a/*"])
        with self.assertRaises(ValidationError):
            TerminalSettings(protected_paths=[""])
        with self.assertRaises(ValidationError):
            TerminalSettings(protected_paths="notalist")


class CommandTests(ProtectedCase):
    KEYCMDS = (
        "cat {key}", "cp {key} /tmp/copy", "head -c 20 {key}", "less {key}", "tar cf - {key}", "base64 {key}",
        "scp -i {key} host:file .", "ssh -i {key} me@host", "ssh -o IdentityFile={key} me@host", "ssh-add {key}",
        "ssh-keygen -y -f {key}", "rsync -e 'ssh -i {key}' a b:c", "cat < {key}", "echo hi > {key}",
        "bash -c 'cat {key}'", "echo $(cat {key})", "FOO=1 cat {key}", "sudo cat {key}", "env X=1 cat {key}",
        "cat {key} | head", "ls && cat {key}", "dd if={key} of=/tmp/o",
    )

    def test_denied_in_every_mode_and_context(self) -> None:
        for approvals in ("manual", "smart", "off"):
            for unattended in (False, True):
                session = self.session(approvals, unattended=unattended)
                for template in self.KEYCMDS:
                    self.assertDenied(session, template.format(key=self.key.as_posix()))
        self.assertTrue(self.decisions())
        self.assertEqual(set(self.decisions()), {"denied:protected"})

    def test_denied_with_an_attached_broker_without_asking(self) -> None:
        session = self.session("manual")
        broker = StubBroker(approved=True)  # even a user who would approve is never asked
        responder = Responder(broker)
        with run_context(responder.context()):
            self.assertDenied(session, f"cat {self.key.as_posix()}")
        self.assertEqual(broker.requests, [])
        self.assertEqual(responder.kinds(), [])

    def test_path_variants(self) -> None:
        session = self.session("off")
        home = f"~/.config/keys/pc_ed25519"
        for command, cwd in (
            (f"cat {home}", None),
            ("cat $HOME/.config/keys/pc_ed25519", None),
            ("cat ${HOME}/.config/keys/pc_ed25519", None),
            ("cat .config/keys/pc_ed25519", str(self.root)),
            ("cat keys/pc_ed25519", str(self.cfg)),
            ("cat ./pc_ed25519", str(self.cfg / "keys")),
            ("cat ../keys/pc_ed25519", str(self.cfg / "owner")),
            (f"cat {self.root.as_posix()}/work/../.config/keys/pc_ed25519", None),
            ("cat .config/keys/pc_*", str(self.root)),
            ("cat .config/k*/*", str(self.root)),
            ("cat .config/keys/pc_ed2551[0-9]", str(self.root)),
            ("cat pc_*", str(self.cfg / "keys")),
            (f"cat {self.secrets.as_posix()}", None),
            ("cat .config/secrets.env", str(self.root)),
            ("cat .env", None),
            ("cat /proc/self/environ", None),
            ("cat /proc/*/environ", None),
            (f"cat {self.creds.as_posix()}", None),
            ("cat ~/.ssh/id_rsa", None),
            ("tar czf /tmp/x.tgz .config", str(self.root)),
            ("grep -r token .", str(self.root)),
            ("cp -r .config /tmp/x", str(self.root)),
            ("rsync -a .config/ /tmp/x/", str(self.root)),
            ("find . -type f -exec cat {} +", str(self.root)),
        ):
            self.assertDenied(session, command, cwd)

    def test_obvious_references(self) -> None:
        session = self.session("off")
        for command in (
            "python3 -c \"print(open('/app/.config/keys/some').read())\"", "cat keys/foo", "echo $(echo a)_ed25519",
            "cat id_rsa", "cat a/id_ed25519", "grep x .env", "cat x/secrets.env", "cat /proc/self/environ",
            "cat cfg/.credentials.json", "cat foo/.env.production",
        ):
            self.assertDenied(session, command)

    def test_symlink_to_a_key_is_followed(self) -> None:
        link = self.work / "innocent"
        try:
            os.symlink(self.key, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")
        session = self.session("off")
        self.assertDenied(session, "cat innocent")
        self.assertIn("protected path", files.sandbox_read(session, "innocent"))

    def test_script_and_ssh_config_that_name_keys_are_refused(self) -> None:
        write(self.work / "go.sh", f"cat {self.key.as_posix()}\n")
        write(self.work / "cfg", "Host x\n  IdentityFile somewhere\n")
        write(self.work / "fine.sh", "echo ok\n")
        session = self.session("off")
        self.assertDenied(session, "bash go.sh")
        self.assertDenied(session, "ssh -F cfg x")
        self.assertRuns(session, "bash fine.sh")

    def test_innocuous_commands_still_run(self) -> None:
        session = self.session("off")
        for command, cwd in (
            ("echo keys", None), ("echo 'monkeys/x' && ls keys", None), ("ls .config/keys", str(self.root)),
            ("ls .config/keys/", str(self.root)), ("ls -la .config", str(self.root)), ("cat .config/keys/pc_ed25519.pub", str(self.root)),
            ("cat .config/keys/known_hosts", str(self.root)), ("cat .config/owner/user.yaml", str(self.root)),
            ("env", None), ("printenv PATH", None), ("set", None), ("cat .env.example", None),
            ("python -c \"import os; print(os.environ.get('A'))\"", None), ("node -e \"process.env.A\"", None),
            ("ssh-keygen -y -f x.pub", None), ("cat notes.md", None), ("echo hi > out.txt", None),
            ("git status", None), ("grep -rn foo src docs", str(self.work)), ("ls /proc/self/cmdline", None),
        ):
            self.assertRuns(session, command, cwd)

    def test_ssh_backends_are_not_affected(self) -> None:
        session = self.session("off", local=False)
        self.assertRuns(session, f"cat {self.key.as_posix()}")
        self.assertRuns(session, "cat /proc/self/environ")
        self.assertIsNone(session.protected())
        self.assertNotIn("protected path", files.sandbox_read(session, str(self.key)))

    def test_assess_reports_the_protected_level(self) -> None:
        prot = build_protected()
        verdict = assess(f"cat {self.key.as_posix()}", None, prot, str(self.work))
        self.assertEqual(verdict.level, "protected")
        self.assertEqual(assess(f"cat {self.key.as_posix()}").level, "normal")  # no set, no verdict (ssh backends)
        self.assertEqual(assess("rm -rf /", None, prot, str(self.work)).level, "hardline")

    def test_extra_patterns_apply_to_commands(self) -> None:
        session = self.session("off", protected_paths=[f"{self.root.as_posix()}/vault/*"])
        self.assertDenied(session, f"cat {self.root.as_posix()}/vault/plan.txt")
        self.assertDenied(session, "cat vault/plan.txt", str(self.root))

    def test_audit_line_records_the_denial(self) -> None:
        session = self.session("off")
        self.run_cmd(session, f"cat {self.key.as_posix()}")
        record = json.loads(self.audit.read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(record["decision"], "denied:protected")
        self.assertEqual(record["tool"], "terminal")
        self.assertIsNone(record["exit_code"])

    def test_denial_text_tells_the_model_not_to_retry(self) -> None:
        _, denial = self.run_cmd(self.session("off"), f"cat {self.key.as_posix()}")
        self.assertIn("Do not retry", denial)
        self.assertIn("off limits", denial)
        self.assertNotIn("PRIVATE", denial)


class FileToolTests(ProtectedCase):
    def test_read_write_patch_search_deny_exact_and_below(self) -> None:
        session = self.session("off")
        key = self.key.as_posix()
        for path in (
            key, f"{self.work.as_posix()}/../.config/keys/pc_ed25519", "~/.config/keys/pc_ed25519",
            "../.config/keys/other", self.secrets.as_posix(), ".env", "/proc/self/environ", self.creds.as_posix(),
        ):
            self.assertIn("protected path", files.sandbox_read(session, path), path)
            self.assertIn("protected path", files.sandbox_write(session, path, "x"), path)
            self.assertIn("protected path", files.sandbox_patch(session, path, "a", "b"), path)
        self.assertEqual(session.backend.calls, [])
        self.assertEqual(set(self.decisions()), {"denied:protected"})
        self.assertEqual(self.key.read_text(encoding="utf-8"), "PRIVATE")

    def test_denied_in_manual_and_unattended_too(self) -> None:
        for approvals in ("manual", "smart", "off"):
            session = self.session(approvals, unattended=True)
            self.assertIn("protected path", files.sandbox_read(session, self.key.as_posix()))
            self.assertEqual(session.backend.calls, [])

    def test_search_root_that_is_protected_is_denied(self) -> None:
        session = self.session("off")
        self.assertIn("protected path", files.sandbox_search(session, "x", str(self.key)))
        self.assertIn("protected path", files.sandbox_search(session, "x", str(self.cfg / "keys" / "sub" / "z"), files=True))
        self.assertEqual(session.backend.calls, [])

    def test_search_excludes_and_filters_protected_files(self) -> None:
        def handler(command, cwd, timeout, stdin):
            return Result(0, f"{self.key.as_posix()}:1:SECRET\n{self.work.as_posix()}/ok.txt:2:fine\n"
                             f"{self.secrets.as_posix()}:1:TOKEN\n")

        session = self.session("off", handler=handler)
        out = files.sandbox_search(session, "S", str(self.root))
        self.assertIn("ok.txt", out)
        self.assertNotIn("SECRET", out)
        self.assertNotIn("TOKEN", out)
        script = session.backend.calls[-1].command
        self.assertIn("--exclude=pc_ed25519", script)
        self.assertIn("--exclude=secrets.env", script)
        self.assertIn("--exclude-dir=keys", script)

        names = self.session("off", handler=lambda *a: Result(0, f"{self.key.as_posix()}\n{self.work.as_posix()}/a.txt\n"))
        out = files.sandbox_search(names, "*", str(self.root), files=True)
        self.assertNotIn("pc_ed25519", out)
        self.assertIn("a.txt", out)
        self.assertIn("-not -name pc_ed25519", names.backend.calls[-1].command)

    def test_listing_the_keys_directory_and_public_files_is_allowed(self) -> None:
        session = self.session("off")
        files.sandbox_read(session, str(self.cfg / "keys"))
        files.sandbox_read(session, str(self.pub))
        files.sandbox_read(session, str(self.hosts))
        files.sandbox_read(session, str(self.user_yaml))
        self.assertEqual(len(session.backend.calls), 4)

    def test_ssh_sessions_use_the_file_tools_unchanged(self) -> None:
        session = self.session("off", local=False)
        files.sandbox_read(session, str(self.key))
        self.assertEqual(len(session.backend.calls), 1)

    def test_broken_users_do_not_break_the_tools(self) -> None:
        # the fixture already holds a broken and an odd user.yaml
        session = self.session("off")
        self.assertIn("protected path", files.sandbox_read(session, str(self.key)))


class GlobResolutionTests(ProtectedCase):
    """Globs are resolved against the real filesystem: refused only when an actual match is protected."""

    def make_skills(self) -> Path:
        skills = self.root / "skills"
        for name in ("alpha", "beta"):
            write(skills / name / "SKILL.md", "# " + name + "\n")
        return skills

    def test_reported_loop_is_allowed_without_secrets(self) -> None:
        skills = self.make_skills()
        session = self.session("off")
        command = (f"cd {skills.as_posix()}; for s in */; do echo \"$s: $(grep -m1 -n '^# ' $s/SKILL.md)\"; done")
        self.assertRuns(session, command)

    def test_glob_that_expands_to_an_env_file_is_denied_and_named(self) -> None:
        skills = self.make_skills()
        write(skills / ".env", "TOKEN=1")
        session = self.session("off")
        self.assertRuns(session, f"cd {skills.as_posix()}; cat *")  # the shell skips dotfiles in `*`
        result, denial = self.run_cmd(session, f"cd {skills.as_posix()}; cat .*")
        self.assertIsNone(result)
        self.assertIn(".env", denial)
        self.assertIn("protected path", denial)

    def test_star_in_the_keys_folder_is_denied(self) -> None:
        session = self.session("off")
        result, denial = self.run_cmd(session, "cat *", str(self.cfg / "keys"))
        self.assertIsNone(result)
        self.assertIn("pc_ed25519", denial)

    def test_ls_of_dirs_in_a_normal_folder_is_allowed(self) -> None:
        self.make_skills()
        self.assertRuns(self.session("off"), "ls */", str(self.root / "skills"))

    def test_unresolvable_glob_falls_back_to_protected_names_only(self) -> None:
        session = self.session("off")
        for command in ("cat $D/*", "cat $D/*/", "cat $D/*.md", "cd $D; cat *", "ls $(pwd)/*"):
            self.assertRuns(session, command)
        for command in ("cat $D/*.env", "cat $D/.env*", "cat $D/*_ed25519", "cat $D/keys/*", "cd $D; cat *.env"):
            self.assertDenied(session, command)


if __name__ == "__main__":
    unittest.main()
