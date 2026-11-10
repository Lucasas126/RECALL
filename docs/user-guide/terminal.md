# Terminal sandbox

The `terminal` tool group lets an agent run shell commands and edit files on one or more **backends**: a sandbox (a separate
machine, normally a small container or VM) and/or your own PC, reached over SSH. An agent may be allowed several backends at
once; each has its own working directory and approval mode, and the model picks one per call. It is off by default and enabled per agent. Every action goes through an approval
policy, every action is written to an audit file, and a few actions are refused whatever anyone approves.

Five tools make up the group: `terminal` (run a command), `sandbox_read` (numbered, paginated reads), `sandbox_write`
(atomic whole-file write), `sandbox_patch` (unique find-and-replace returning a diff) and `sandbox_search` (regex over
contents or glob over names). They act **only** on the chosen backend's filesystem, never on the user's folders or the agent's
memory. Each takes an optional `backend` argument (see [configuration](#configuration)). File contents travel on the standard input of the remote command, never inside the command line.

Status: phase 1. Commands run in the foreground and are killed at the timeout; there is no background `process` tool yet
(see the [backlog](../project/backlog.md)).

## backends

| type | where commands run | availability |
| --- | --- | --- |
| `ssh` | on another machine, through the local `ssh` binary (key only, strict host-key checking, no agent/X11 forwarding, no password prompts, a minimal local environment) | always |
| `local` | on the RECALL host itself, in a working directory, with an environment ALLOWLIST (`PATH`, `HOME`, `LANG`, `LC_*`, `TERM`, `TMPDIR`, `USER`, `LOGNAME`, `SHELL`, `TZ`); nothing else is inherited (no `SSH_AUTH_SOCK`, `DOCKER_HOST`, `KUBECONFIG`, `DBUS_SESSION_BUS_ADDRESS`, `DATABASE_URL`, proxies, keys, tokens or `RECALL_*`) | only when `settings.yaml` has `terminal.allow_local: true` or the server environment sets `RECALL_TERMINAL_ALLOW_LOCAL=true`; trusted installs only |

Pointing an agent at the RECALL container itself (a `local` backend) gives it every user's `.config` and `data`. The only intended
use is the setup guide of the [Admin CLI](admin-cli.md#templates), with `approvals: manual`. See
[security notes](../deployment/security.md#terminal-sandbox).

## setting up an SSH sandbox

1. **Create the sandbox.** Any Linux container or VM with `bash`, `openssh-server`, `coreutils`, `grep`, `findutils` and
   `awk`, and an unprivileged user (`agent` below) with a home directory. Give it no mounts of RECALL data, no secrets and
   as little network access as the work needs. Example with Docker on the sandbox host:

   ```bash
   docker run -d --name recall-sandbox --restart unless-stopped \
     --cap-drop ALL --security-opt no-new-privileges --memory 1g --pids-limit 256 \
     -p 2222:22 my-sandbox-image
   ```

2. **Make a dedicated key pair** for this purpose on the machine that runs RECALL and install the public half in the
   sandbox user's `~/.ssh/authorized_keys`:

   ```bash
   ssh-keygen -t ed25519 -N "" -C recall-sandbox -f ./config/keys/sandbox_ed25519
   chmod 600 ./config/keys/sandbox_ed25519
   ```

   The key must have no passphrase (commands run non-interactively). Keep it out of Git and out of the agent's reach.

3. **Pin the host key** (strict checking is always on, so an unknown host is refused). Check the fingerprint against the
   sandbox through a channel you trust, then record it:

   ```bash
   ssh-keyscan -p 2222 192.0.2.10 > ./config/keys/sandbox_known_hosts
   ssh-keygen -lf ./config/keys/sandbox_known_hosts      # compare with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the sandbox
   ```

4. **Declare the backend** in the user's `user.yaml` (see below). In Docker, mount the keys read-only into the RECALL
   container (for example `./config/keys:/app/.config/keys:ro`) and use the in-container paths. The image includes
   `openssh-client`.

5. **Test it** without involving a model:

   ```bash
   docker compose exec recall-web python -m src.admin terminal-test --user <user_id> --backend sandbox
   ```

   It runs `echo ok; uname -sr` with a 15 s timeout and prints the exit code and the (redacted, truncated) output. It never
   prints the key. Exit code 255 means ssh itself failed (network, key or host key).

## configuration

User level, in `.config/<user_id>/user.yaml` (the sandboxes that user's agents may use; key files are **paths**, never
inline secrets):

```yaml
terminal_backends:
  sandbox:                                 # ^[a-z][a-z0-9_-]{0,31}$
    type: ssh
    host: 192.0.2.10
    port: 2222                             # default 22
    user: agent
    key_file: /app/.config/keys/sandbox_ed25519
    known_hosts_file: /app/.config/keys/sandbox_known_hosts
    description: Disposable Linux sandbox    # optional, <= 200 chars; shown to the model and in the agent form
  pc:
    type: ssh
    host: 192.0.2.20
    user: user-a
    key_file: /app/.config/keys/pc_ed25519
    known_hosts_file: /app/.config/keys/pc_known_hosts
    description: User A's Windows PC, real files, Git Bash
  # here:                                  # only with settings.yaml terminal.allow_local: true
  #   type: local
  #   workdir: /srv/recall-workspace
```

Agent level, in the agent file (the group must also be listed in `chat_config.tool_groups`):

```yaml
chat_config:
  tool_groups: [memory_read, terminal]
terminal:
  backends:                                # 1+ keys of the user's terminal_backends, each with its own policy
    sandbox:
      workdir: ~/workspace                 # default directory on that backend (default ~, the login user's home)
      approvals: smart                     # shorthand: manual | smart | off (quote it: bare off is a YAML boolean); see approval rules
    pc:
      workdir: ~
      approvals: manual                    # a real machine: keep it manual (off here needs level admin)
      # timeout_seconds: 120               # optional per-backend override
  default_backend: sandbox                 # required when more than one; used when a call names no backend
  timeout_seconds: 60                      # default command timeout, 1-600
  max_output_chars: 20000                  # output returned to the model, 500-200000
```

Framework level, in `settings.yaml` (`terminal:` block, see [settings.yaml](../reference/settings-yaml.md#terminal)):
`approval_timeout_seconds`, `allow_local`, `default_timeout_seconds`, `max_timeout_seconds`, `max_output_chars`. The
effective limits are the smaller of the agent's and the framework's.

Validation: every key under `backends` must exist in the user's `terminal_backends`, `default_backend` must be one of them
(and is required with more than one), and `terminal.backend`, `workdir` and `approvals` at block level no longer exist. A
backend that fails to resolve at load time (not defined, or `local` while `allow_local` is off) is disabled with a logged
warning and the agent keeps the others; if none resolves, or the block is missing, the agent still loads and the tools are
simply not built.

**Choosing a backend.** The five tools take an optional `backend` string, enumerated in the tool schema with the agent's
allowed names (omitted from the schema when only one backend is configured). Without it the call goes to `default_backend`; an
unknown name returns a plain-text error listing the allowed names. The system prompt lists every allowed backend with its
working directory, approval mode and `description`, marks the default, and says that a backend is a disposable sandbox only if
its description says so: otherwise it is one of the user's real machines. Approvals, the working directory and the timeout
override of the **chosen** backend apply to the call, so the same command can run freely on a `smart` sandbox and ask on a
`manual` PC.
The tools are never built in [disposable chats](memory.md#disposable-chats).

## approvals

Every action is decided in this order: (1) the fixed floor, [hardline](#hardline-and-dangerous) commands and
[protected paths](#protected-paths-local-backend), refused in every case; (2) the [approval rules](#approval-rules); (3) the person,
when the verdict is `ask`.

The per-backend `approvals` of the agent file is a shorthand for that backend:

| mode | what asks for approval |
| --- | --- |
| `manual` | every command and every file operation, reads included (the default when nothing else is configured) |
| `smart` | only the *dangerous* actions listed below |
| `off` | nothing asks while a person is attached to the chat; the floor still applies. When nobody is attached it is **not** a free pass: see [unattended runs](#unattended-runs). On a real machine (`kind: machine`, or `local`) it needs an `admin` agent |

## approval rules

Rules replace the fixed modes as the decision source. There are three lists, from least to most specific: installation
(`approval_rules` in [settings.yaml](../reference/settings-yaml.md#approval_rules)), user (`approval_rules` in
[user.yaml](../reference/user-yaml.md#approval-rules)) and agent (`approval_rules` in the [agent file](../reference/agent-yaml.md)).
Design: [permissions](../architecture/permissions.md#approval-rules).

```yaml
approval_rules:
  default: ask                          # optional: allow | ask | deny for what no rule covers
  rules:
    - {verdict: allow, tool: terminal, backend_kind: sandbox, command: "git status"}
    - {verdict: allow, tool: file_read, backend_kind: sandbox, path: "~/workspace/*"}
    - {verdict: ask,   tool: terminal, dangerous: true, reason: destructive command}
    - {verdict: deny,  tool: terminal, command: "git push*", id: no-push}
```

| field | meaning |
| --- | --- |
| `verdict` | `allow`, `ask` or `deny` (required) |
| `tool` | `terminal` (the shell command), `file_read` (`sandbox_read`, `sandbox_search`), `file_write` (`sandbox_write`, `sandbox_patch`) or `any` (default) |
| `command` | for `terminal` or `any`. A plain text is a word prefix (`git status` matches `git status -s`, not `git statusx`); with `*`, `?` or `[` it is a glob over the whole segment |
| `path` | for file tools or `any`. A glob over the resolved target (relative paths hang from the backend's workdir; `~` stays as typed, so write `~/workspace/*`) |
| `backend` | backend name, glob allowed (`lab*`) |
| `backend_kind` | `sandbox`, `machine` or `local` (see [kind](../reference/user-yaml.md#terminal-backends)) |
| `dangerous` | `true` matches what the [dangerous list](#hardline-and-dangerous) flags, `false` what it does not |
| `exact` | `true` compares `command` (the whole segment) and `path` literally: no word prefix, no glob. The rules written by "Always for this agent" use it |
| `id`, `reason` | optional: a stable name for the audit trail, and a short text for the approval card |

All the fields you set must match. Unknown keys and combinations that cannot match (`command` with `file_read`, `path` with `terminal`) are
refused when the file loads.

**Resolution.** Every matching rule of every layer is collected and the worst verdict wins: deny beats ask beats allow, so a
more specific layer cannot lift a deny. When none matches, the backend's `approvals` applies if the agent file sets it, else the `default`
of the most specific layer that has one (agent, user, installation), else `ask`. A rule that matches still applies on top of a shorthand:
`deny` and `ask` rules beat `smart`/`off`'s allow, and an `allow` rule lifts `manual` for the actions it names.

**Commands are judged segment by segment.** `ls && rm x` is two segments (`&&`, `||`, `;`, `|`, `&`, newlines, subshells, `$(...)`,
backticks and the script of `bash -c` count); each is judged and the worst verdict wins. A redirection is judged as its own segment
(`> out`), so allowing `cat` does not allow `cat a > b`. `sudo ls`, `env X=1 ls` or `timeout 5 ls` do not match a rule for `ls`. The
parser is best effort; keep `ask` or `deny` rules for what matters and add an `ask` rule with `dangerous: true` next to broad allows.

**Shipped behaviour.** `settings.yaml` ships `default: ask` and no rules, which reproduces `manual` exactly; it carries a commented
example that lets agents run `ls`, `cat` and `git status` on sandbox backends without asking.

**Guardrail.** Allowing everything on a real machine needs an `admin` agent. A rule that allows without a `command`, `path` or `dangerous`
condition, a `default: allow`, or `approvals: off`, reaching a `machine` or `local` backend the agent uses, makes the agent invalid
(the error names the rule and the backend) unless its level is `admin`. On a `kind: sandbox` backend it is allowed from `operator`.

**Unattended runs.** `allow` still runs and every `ask` becomes `deny` ([below](#unattended-runs)).

An approval is shown to the chat's owner while the reply waits:

- **Web**: the `approval` event arrives on the message stream and `POST .../approvals/<id>` answers it
  ([API](../reference/api.md#terminal-approvals)). The bundled web UI draws a card for it
  ([below](#in-the-web-ui)); answers can also be given from Telegram.
- **Telegram**: a message with the command, backend, working directory, reason and **Approve once** / **Deny** buttons, plus
  **Always in this session** and **Always for this agent** when the card offers them (see [remembering an approval](#remembering-an-approval)); the outcome is
  written onto the message when it closes.

An approval is denied automatically after `terminal.approval_timeout_seconds` (default 300 s), when the reply is cancelled
or interrupted, and when the service shuts down. Only the owner of the chat can answer. A denied action returns text telling
the model not to retry or work around it. A `sandbox_patch` asks once for both its read and its write.

### remembering an approval

A card offers up to four answers:

| answer | effect |
| --- | --- |
| Approve once | this action only (the default of the API) |
| Always in this session | the same action is not asked again in this chat, until the chat is deleted or the service restarts. Kept in memory, per chat |
| Always for this agent | adds an **exact** `allow` rule to the agent's `approval_rules` (the exact command, or each segment of a multi-segment command; or the exact path and kind of a file operation), on this backend |
| Deny | refuses it |

"Always" is not offered for a *dangerous* action, and "for this agent" is not offered when an `ask` rule would still win over the new
rule, when the command looks like it contains a secret, or in a subagent task. A rule is never a wildcard or a prefix, and it cannot lift the
[fixed floor](#approvals). If the rule cannot be saved (for instance the agent would break the blanket-allow guardrail), the action is
approved once and you are told why. The rule is active in the same reply and in every chat of the agent from its next reply.

Every learned rule is listed under **Learned approvals** in the Terminal block of the agent form (each with **Remove**, and **Reset to
defaults** for all of them), and with `python -m src.admin agent rules --user U --agent A [--clear]`. The rule file edits and every approval
(with its `scope`) are in the [audit file](#audit).

### in the web UI

- **Approval card**: when the agent asks, the chat shows a card as a step of the turn: it appears exactly where the tool call happened, between the text written before it and the text written after it, and the tool's result summary follows it. Several approvals each sit at their own step. While any approval waits, a small bar at the bottom of the chat says "1 approval pending, jump to it" and scrolls to it. The card has the command, a small badge with the backend name, the working directory, the reason and a
  countdown to the automatic denial. A flagged action is titled "Dangerous command", any other "Approval required".
  **Approve once** and **Deny** answer it (with **Always in this session** and **Always for this agent** when offered, see [above](#remembering-an-approval); a card that offers nothing more shows a plain **Approve**); once closed the card shows an outcome badge (approved, denied, timed out,
  cancelled or expired). A resolved card folds into one line ("Approved: terminal [backend] first 60 characters of the command") that expands to the full card, so long histories stay compact.
- **After a reload** the cards still waiting are restored inside the last assistant message, before the matching tool step when it is identifiable, else at its end. An approval resolved from Telegram shows up in the web card
  within a few seconds.
- **Agent form**: the **Terminal** block appears in the Tools tab once the terminal group is enabled. It lists your
  backends (name, type and description only, never host or key) each with an **Allow** checkbox that reveals its working
  directory and approvals mode (`off` is shown as a warning), a **Default backend** select limited to the allowed ones, the
  command timeout, the **Learned approvals** (the rules written by *Always for this agent*, with **Remove** and **Reset to defaults**, which
  removes only those), and the **Approval rules** editor. With no backends defined it explains how to add one. Each backend also shows,
  read only, its `kind` (`sandbox`, `machine` or `local`) and the level it needs; backends are defined in `user.yaml` only, never in the web UI.
- **Approval rules editor** (Terminal block, Tools tab): the agent's own hand-written `approval_rules`. **When no rule matches** is the
  layer `default` (empty follows the user and installation defaults). **Add rule** appends a row with the fields of a rule: verdict
  (allow, ask, deny), what it applies to (any action, terminal commands, read files, write files), command, path, backend (name or
  pattern), kind of backend, danger (any, only dangerous, only not dangerous) and **Exact match**; each row has **Remove**, and keys the
  editor does not show (`id`, `reason`) survive an edit. Nothing is saved until **Save agent**, which sends the whole list through
  `PUT /api/agents/<id>`: the rules the model refuses (a `command` with a file tool, `exact` without a command or path, unknown keys) and the
  [blanket-allow guardrail](#approval-rules) (`approval_rule_not_allowed`: an allow that is not narrowed by a command, path or danger
  condition on a machine or `local` backend, or a `default: allow` that really is the default, below `admin`) show their message under the
  editor, and nothing is written. The learned rules are kept as they are when the list is saved. A saved agent also shows **Effective rules (read
  only)**: the installation, user and agent layers with their defaults, the per-backend `approvals` shorthand and the default that applies
  (`GET /api/agents/<id>/approval-rules/effective`, owner only).

### hardline: always refused, in every mode

Commands are split into segments (`&&`, `||`, `;`, `|`, `&`, newlines, subshells, `$(...)`, backticks, `bash -c '...'`,
`eval`, `sudo`/`env`/`nohup`/`timeout`... wrappers) and every segment is judged on its parsed words, so quoted text such as
`echo "rm -rf /"` is just text. The worst verdict wins. Hardline:

- recursive `rm` of `/`, `~`, `$HOME` or a system directory (`/etc /usr /bin /sbin /lib /lib32 /lib64 /boot /var /home /root
  /dev /proc /sys /opt /srv /mnt /media`), and `rm --no-preserve-root`;
- writing to a block device (`/dev/sd*`, `hd*`, `vd*`, `xvd*`, `nvme*`, `mmcblk*`, `loop*`, `md*`, `dm-*`, `disk*`) by
  redirect, `dd of=`, `cp`/`tee`/`install`/`ln`/`truncate` targets, `shred`, `wipefs`, `mkfs*`, `mke2fs`, `mkswap`, `fdisk`,
  `sfdisk`, `parted`, `sgdisk`; also `sandbox_write` to such a path;
- `shutdown`, `reboot`, `halt`, `poweroff`, `init 0|6`, `telinit 0|6`, `systemctl poweroff|reboot|halt|kexec|suspend|hibernate`;
- fork bombs;
- recursive `chmod`/`chown`/`chgrp` on `/` or a system directory; `mv` of those directories;
- `find /` with `-delete` or `-exec rm`;
- a NUL byte in the command.

### protected paths (local backend)

A `type: local` backend runs as the RECALL user, so an agent there could read the SSH key of someone else's PC backend or the
secrets of the installation. On a local backend (**only**; SSH backends are other machines) these paths are refused in **every**
approval mode, `off` included, without asking anyone, also in unattended runs and dreams. The set is recomputed on every call:

| protected | note |
| --- | --- |
| the private `key_file` of every SSH backend of every user (`.config/*/user.yaml`, broken users skipped) | the `.pub` sibling and the `known_hosts_file` stay readable |
| `.config/secrets.env`, any file named `.env` or `.env.*`, `/app/.env`, `<repo>/.env` | `.env.example`, `.env.sample`, `.env.template`, `.env.dist` are templates and stay readable |
| any file inside a directory named `keys` under the config folder | listing the folder (`ls .config/keys`) is allowed |
| `id_rsa`, `id_ed25519`, `id_ecdsa`, `id_dsa` (no `.pub`) anywhere | |
| `*/claude-login/.credentials.json`, any `.credentials.json` | |
| `.config/<user>/mcp/**` (MCP credential folders) | read **and** write protected |
| `/proc/<pid>/environ`, `/proc/self/environ` | `/proc/*/cmdline` is fine |
| patterns in `settings.yaml` `terminal.protected_paths` | globs, repo-relative or absolute; a folder protects its contents |

A `user.yaml` that cannot be parsed does not drop its user from the set: the `key_file:` values its text names are still
protected and the failure is logged (fail closed).

**Write-only protection** (reading stays allowed because agents need `user.yaml` for admin work and the admin CLI never prints
password hashes): `terminal_audit.jsonl`, every `user.yaml`, agent yaml (`.config/<user>/agents/*.yaml`), `settings.yaml` and
`known_hosts` files cannot be written by `sandbox_write`, `sandbox_patch` or a shell command that redirects into them, or uses
`tee`, `cp`, `mv`, `install`, `ln`, `rsync`, `scp`, `dd of=`, `sed -i`, `perl -i`, `rm`, `truncate`, `touch`, `chmod`, `chown`.
Cross-user access to config and data is otherwise still allowed by the rules of the setup guide.

What is checked:

- **File tools** (`sandbox_read`, `sandbox_write`, `sandbox_patch`, `sandbox_search`): exact. The path is made absolute (`~`,
  relative to the working directory, `..`) and checked as written and with symlinks resolved. A search whose root is a protected
  path is refused; a search over a parent folder skips protected files (`--exclude`, `-not -name`) and drops them from the results.
- **`terminal`**: best effort. Every word, redirect target and `option=value` of every parsed segment (also inside `$(...)`,
  backticks and `bash -c`) is resolved like the shell would (`~`, `$HOME`, relative paths, `..`, symlinks) and compared; glob
  words (`keys/pc_*`) are matched against the protected files; obvious references (`keys/<file>`, `_ed25519`, `id_rsa`, `.env`,
  `secrets.env`, `/environ`, `.credentials.json`) are refused anywhere in the command; `tar`, `zip`, `rsync`, `scp`, `cp -r`,
  `grep -r`, `rg` and `find -exec` may not start at a folder that contains protected files (name narrower paths); a script run
  by an interpreter and an `ssh -F` config that mention a protected path or an `IdentityFile` are refused. `ssh`/`scp`/`sftp`/
  `ssh-add`/`ssh-keygen` with `-i`, `-F` or `-o IdentityFile` pointing at a protected path are refused like any other mention.
  `env`, `printenv` and `set` are fine (the local backend passes only an allowlist of variables).

The refusal reads `Denied: protected path: <what>...`, tells the model not to retry or work around it, and the audit decision
is `denied:protected`. No approval card is shown: nobody can approve it from the chat.

**Limits, honestly.** The command parser is a best-effort heuristic, not a security boundary, and that risk is accepted. The `local` backend is a real host shell: anything the RECALL user can do, an approved (or `off`-mode) command can do. The command scan is a heuristic. A determined obfuscated command (a path assembled from pieces,
`printf` + `eval` tricks, a hard link, a program written and run in two steps by an interpreter the scan cannot read) can bypass
it; the file tools cannot be bypassed this way. The approval card (`manual`) stays the second barrier, and the real boundary is
separation: see the ideas in the [backlog](../project/backlog.md) (a separate OS user or container for local backends).

### dangerous: asks in `manual` and `smart`

- privilege: `sudo`, `doas`, `pkexec`, `su`; account tools `passwd`, `usermod`, `useradd`, `userdel`, `groupdel`, `visudo`,
  `chpasswd`;
- deletion and disk: any recursive `rm`, `rm` of a system path, `shred`, `wipefs`, `mkfs*`/partition tools on non-devices,
  `dd`, `find ... -delete` / `-exec rm`;
- system paths: `tee`, `cp`, `mv`, `install`, `ln` into a system directory; `sandbox_write`/`sandbox_patch` of a system path;
  redirects (`>`, `>>`) into a system directory or to an absolute path outside the working directory (`/tmp` and the
  working directory are fine);
- permissions: `chown`, `chgrp`, world-writable `chmod` (`777`, `o+w`, `a+w`, `+w`), recursive `chmod`;
- services and processes: `systemctl stop|disable|mask|kill|restart|isolate`, `service ... stop|restart`, `kill -9` /
  `-KILL`, `killall`, `pkill`, `skill`, `mount`, `umount`, `losetup`, firewall tools (`iptables`, `ip6tables`, `nft`, `ufw`,
  `firewall-cmd`, `ebtables`), `crontab -r`;
- network hops: `ssh`, `scp`, `sftp`, `sshpass`, `ssh-copy-id` from inside the sandbox;
- history rewriting: `git push --force` / `-f` / `--force-with-lease` / `+ref`, `git reset --hard`, `git clean -f`,
  `git branch -D`;
- installs and removals: global `npm`/`pnpm`/`yarn`/`bun` installs, `pip`/`uv` with `--break-system-packages`, `--global`
  or `--system`, `docker`/`podman` `rm|rmi|prune|kill`, package removal with `apt`, `apt-get`, `dpkg`, `yum`, `dnf`, `apk`,
  `pacman`, `zypper`, `brew`, `snap`;
- downloads piped into an interpreter (`curl ... | sh`, `wget ... | python`, `bash <(curl ...)`), destructive SQL
  (`DROP TABLE|DATABASE|...`, `DELETE FROM`, `TRUNCATE`).

The lists are deliberately conservative: a false "dangerous" costs one tap, a false "normal" costs a sandbox. They are a
safety net, not a security boundary: the boundary is the sandbox itself.

## unattended runs

Nobody can answer when there is no attached client, so the policy never waits: every `ask` becomes a denial and `allow` runs. So with `approvals: manual` (or no rule that allows) everything is denied;
with `smart` normal actions run and dangerous ones are denied; with `off` the behaviour is that of `smart` (dangerous actions are denied) and `sandbox_write`/`sandbox_patch` are denied too, so `off` never widens what runs unattended. The hardline and protected paths apply as always. A run counts as attended only when the chat path says so explicitly (`RunContext.interactive` defaults to `False`). The mode is
that of the backend the call targets. The denial text tells the model so. This applies to runs without an interactive client and to dream sessions.

## subagent workers

An agent with the `delegate` group can grant a [worker task](delegation.md#terminal-and-approvals) some of its backends (`task_start(backends=[...])`).
For the worker those backends are always on `manual`, whatever mode the agent has for them (`off` and `smart` become `manual`, `local` included),
and it only gets backends the agent itself has. The approval rules still apply to it, so an `allow` rule lifts `manual` for the worker too. Hardline rules, dangerous-command detection and protected paths apply unchanged. The approval card
is shown in the chat that launched the task (web card or Telegram message), labelled *Task <title> (ephemeral) wants to run:*, and answering it
resumes the worker; the task's timeout is paused while it waits. A worker run nobody watches follows the [unattended](#unattended-runs) rules:
`manual` and dangerous actions are denied. The audit records those commands under the agent like any other.

## dream mode

[Dream](dream.md) sessions never get the terminal. `terminal.allow_in_dream` and `terminal` in `dream.tools` are
configuration errors. Read-only sandbox variants for dreams are planned.

## Using your own PC as a backend

A Windows PC can be a second backend through the built-in OpenSSH Server, with Git Bash as the login shell so commands stay
the POSIX ones the tools expect (RECALL still runs `bash -lc 'cd "$HOME"... && <cmd>'`). This lets the agent act on your **real
files**: start with `approvals: manual`, so every command waits for your tap. Example addresses below are placeholders.

1. **Enable OpenSSH Server** (elevated PowerShell): Settings > Apps > Optional features > *OpenSSH Server*, or
   `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0`, then
   `Set-Service sshd -StartupType Automatic; Start-Service sshd`.
2. **Set Git Bash as the default shell** (elevated PowerShell; adjust the path to your Git install):

   ```powershell
   New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell -Value "C:\Program Files\Git\bin\bash.exe" -PropertyType String -Force
   ```

3. **Key-only login with a dedicated key.** Generate a key pair used for nothing else on the RECALL host
   (`ssh-keygen -t ed25519 -N "" -C recall-pc -f ./config/keys/pc_ed25519`), and install only the public half:
   - for a **normal user**: append it to `C:\Users\<you>\.ssh\authorized_keys`;
   - for a member of **Administrators**: append it to `C:\ProgramData\ssh\administrators_authorized_keys` and fix its ACL
     (`icacls "C:\ProgramData\ssh\administrators_authorized_keys" /inheritance:r /grant "Administrators:F" "SYSTEM:F"`),
     because `sshd` ignores the per-user file for administrators.

   In `C:\ProgramData\ssh\sshd_config` set `PasswordAuthentication no` and `PubkeyAuthentication yes`, then restart `sshd`.
4. **Firewall: only the RECALL host.** Allow inbound TCP 22 from the RECALL host's address only and nothing else:

   ```powershell
   New-NetFirewallRule -Name recall-sshd -DisplayName "OpenSSH for RECALL" -Direction Inbound -Protocol TCP -LocalPort 22 `
     -RemoteAddress 192.0.2.10 -Action Allow
   ```

   Disable the default broad `OpenSSH-Server-In-TCP` rule (or scope it the same way).
5. **Git for Windows tools** (`head`, `wc`, `find`, `diff`, `awk`, `mktemp`, `file`): the file tools only use standard POSIX utilities, which Git Bash ships; no Python is needed on the PC.
6. **Pin the host key and test**, as for the sandbox: `ssh-keyscan 192.0.2.20 > ./config/keys/pc_known_hosts`, declare the backend
   in `terminal_backends` (example above) and run `terminal-test --backend pc`.
7. **The PC must be on and awake** when the agent uses it; otherwise calls fail with exit code 255 (ssh failed). Dreams never use it.

See the [security notes](../deployment/security.md#terminal-sandbox) before enabling it.

## audit

Every executed or refused action appends one JSON line (`backend` is the configured backend name) to `data/<user_id>/<agent_id>/terminal_audit.jsonl`:

```json
{"ts": 1767225600.123, "user": "user-a", "agent": "aria", "backend": "sandbox", "tool": "terminal", "command": "ls -la", "cwd": "~/workspace", "decision": "approved", "exit_code": 0, "duration_ms": 412, "timed_out": false, "verdict": "normal", "layer": "user", "rule": "user:0", "rule_verdict": "ask", "approval_id": "3f9c0a1b7d2e"}
```

The `terminal` result starts with one line telling the agent how the command was authorized (`[approved by the user]`, `[ran without approval: smart mode, normal command]` or `[approvals off]`).

`decision` is one of `approved`, `approved:same-operation`, `auto:smart`, `auto:off`, `auto:rule` (allowed by a rule), `auto:default` (allowed by a layer default), `denied:rule` (denied by a rule), `hardline`, `denied:protected`, `denied:unattended`, `denied:invalid` (NUL byte, too long, bad path),
`denied:<reason>` (`denied by user`, `timed out`, `cancelled`, `service shutdown`) or `cancelled`. The command is redacted
for known secret formats and cut at 2000 characters; the output is not recorded. The agent's own tools cannot read or edit
this file: the memory/palace/folder tools only reach their sources, and the sandbox tools only reach the sandbox. Records carry the `verdict` (`normal`, `dangerous`, `hardline`, `protected`) of refused and executed actions, and which rule decided: `layer` (`installation`, `user`, `agent`, `builtin` when no layer defines a default, `floor` for hardline and protected), `rule` (`agent:2`, or `user:<id>` for a rule with an `id`; `approvals:smart` for the shorthand; absent when a layer default decided), `rule_verdict` (`allow`, `ask` or `deny`: what the engine said before the person answered) and, when a card was shown, its `approval_id` and the `scope` of the answer (`once`, `session`, `agent`, or `deny`; an action allowed by a "for this session" grant is `decision: approved:session` with no `approval_id`). Changes to the agent's learned rules are lines of their own: `event: approval_rule`, `action` (`add` | `remove`), `actor` (`user`, or `admin` from the CLI), `user`, `agent`, the `rule`, and `index`, `chat` or `via: reset` where they apply. The approval card shows the rule that asked in its reason. **The audit trail fails closed:** when it cannot be written (full disk, bad permissions) the action is refused with an error instead of running unrecorded. The approval card of `sandbox_write`/`sandbox_patch` shows a truncated preview (400 characters) of the content, or of the old and new text; the preview is not written to the audit trail. Each
decision is also kept in an in-memory log of the last 50 per user.

## limits and privacy

- Foreground only: a command that must keep running is killed at the timeout (default 60 s, ceiling 600 s). Each call starts a
  fresh shell; only the sandbox filesystem persists.
- Output is stripped of terminal escape codes, redacted for known secret formats (API keys, bearer tokens, private key
  blocks, Telegram tokens) and truncated in the middle to the output limit.
- **Commands and their outputs are sent to the model provider** like the rest of the conversation. Do not let an agent print
  secrets into its context, and choose the provider accordingly (see [models and providers](models-and-providers.md)).
