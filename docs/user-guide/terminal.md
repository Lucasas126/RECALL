# Terminal sandbox

The `terminal` tool group lets an agent run shell commands and edit files on a **sandbox**: a separate machine (normally a
small container or VM) reached over SSH. It is off by default and enabled per agent. Every action goes through an approval
policy, every action is written to an audit file, and a few actions are refused whatever anyone approves.

Five tools make up the group: `terminal` (run a command), `sandbox_read` (numbered, paginated reads), `sandbox_write`
(atomic whole-file write), `sandbox_patch` (unique find-and-replace returning a diff) and `sandbox_search` (regex over
contents or glob over names). They act **only** on the sandbox filesystem, never on the user's folders or the agent's
memory. File contents travel on the standard input of the remote command, never inside the command line.

Status: phase 1. Commands run in the foreground and are killed at the timeout; there is no background `process` tool yet
(see the [backlog](../project/backlog.md)).

## backends

| type | where commands run | availability |
| --- | --- | --- |
| `ssh` | on another machine, through the local `ssh` binary (key only, strict host-key checking, no agent/X11 forwarding, no password prompts, a minimal local environment) | always |
| `local` | on the RECALL host itself, in a working directory, with a scrubbed environment (no `*_KEY`, `*_TOKEN`, `RECALL_*`, provider variables...) | only when `settings.yaml` has `terminal.allow_local: true`; trusted single-user installs only |

Never point an agent at the RECALL container itself: it holds `.env` and every user's data. See
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
  # here:                                  # only with settings.yaml terminal.allow_local: true
  #   type: local
  #   workdir: /srv/recall-workspace
```

Agent level, in the agent file (the group must also be listed in `chat_config.tool_groups`):

```yaml
chat_config:
  tool_groups: [memory_read, terminal]
terminal:
  backend: sandbox                         # a key of the user's terminal_backends
  workdir: ~/workspace                     # default directory inside the sandbox (~ is the sandbox user's home)
  approvals: manual                        # manual | smart | off (default manual)
  timeout_seconds: 60                      # default command timeout, 1-600
  max_output_chars: 20000                  # output returned to the model, 500-200000
  allow_in_dream: false                    # see "dream mode" below
```

Framework level, in `settings.yaml` (`terminal:` block, see [settings.yaml](../reference/settings-yaml.md#terminal)):
`approval_timeout_seconds`, `allow_local`, `default_timeout_seconds`, `max_timeout_seconds`, `max_output_chars`. The
effective limits are the smaller of the agent's and the framework's.

If the agent lists the group but its `terminal` block is missing, names an unknown backend, or names a `local` backend while
`allow_local` is off, the agent still loads: the tools are simply not built and a warning is logged.
The tools are never built in [disposable chats](memory.md#disposable-chats).

## approvals

| mode | what asks for approval |
| --- | --- |
| `manual` (default) | every command and every file operation, reads included |
| `smart` | only the *dangerous* actions listed below |
| `off` | nothing asks; the hardline still applies |

An approval is shown to the chat's owner while the reply waits:

- **Web**: the `approval` event arrives on the message stream and `POST .../approvals/<id>` answers it
  ([API](../reference/api.md#terminal-approvals)). The bundled web UI draws a card for it
  ([below](#in-the-web-ui)); answers can also be given from Telegram.
- **Telegram**: a message with the command, working directory, reason and **Approve** / **Deny** buttons; the outcome is
  written onto the message when it closes.

An approval is denied automatically after `terminal.approval_timeout_seconds` (default 300 s), when the reply is cancelled
or interrupted, and when the service shuts down. Only the owner of the chat can answer. A denied action returns text telling
the model not to retry or work around it. A `sandbox_patch` asks once for both its read and its write.

### in the web UI

- **Approval card**: when the agent asks, the chat shows a card with the command, the working directory, the reason and a
  countdown to the automatic denial. A flagged action is titled "Dangerous command", any other "Approval required".
  **Approve** and **Deny** answer it; once closed the card shows an outcome badge (approved, denied, timed out,
  cancelled or expired).
- **After a reload** the cards still waiting are restored. An approval resolved from Telegram shows up in the web card
  within a few seconds.
- **Agent form**: the **Terminal** block appears in the Tools tab once the terminal group is enabled. It picks one of
  your backends (by name and type only), the working directory, the approvals mode, the command timeout and whether
  dreams may use it. With no backends defined it explains how to add one.

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

Nobody can answer when there is no attached client, so the policy never waits: with `approvals: manual` everything is denied;
with `smart` normal actions run and dangerous ones are denied; with `off` everything except the hardline runs. The denial text
tells the model so. This applies to runs without an interactive client and to dream sessions.

## dream mode

[Dream](dream.md) sessions get the terminal only when **both** the agent's `terminal.allow_in_dream` is `true` **and**
`dream.tools` lists `terminal`; listing `terminal` without `allow_in_dream` is a configuration error. Dream runs are always
unattended (previous section), so they are best used with `approvals: smart` or `off` on a disposable sandbox.

## audit

Every executed or refused action appends one JSON line to `data/<user_id>/<agent_id>/terminal_audit.jsonl`:

```json
{"ts": 1767225600.123, "user": "ana", "agent": "aria", "backend": "sandbox", "tool": "terminal", "command": "ls -la", "cwd": "~/workspace", "decision": "approved", "exit_code": 0, "duration_ms": 412, "timed_out": false}
```

`decision` is one of `approved`, `approved:same-operation`, `auto:smart`, `auto:off`, `hardline`, `denied:unattended`,
`denied:<reason>` (`denied by user`, `timed out`, `cancelled`, `service shutdown`) or `cancelled`. The command is redacted
for known secret formats and cut at 2000 characters; the output is not recorded. The agent's own tools cannot read or edit
this file: the memory/palace/folder tools only reach their sources, and the sandbox tools only reach the sandbox. Each
decision is also kept in an in-memory log of the last 50 per user.

## limits and privacy

- Foreground only: a command that must keep running is killed at the timeout (default 60 s, ceiling 600 s). Each call starts a
  fresh shell; only the sandbox filesystem persists.
- Output is stripped of terminal escape codes, redacted for known secret formats (API keys, bearer tokens, private key
  blocks, Telegram tokens) and truncated in the middle to the output limit.
- **Commands and their outputs are sent to the model provider** like the rest of the conversation. Do not let an agent print
  secrets into its context, and choose the provider accordingly (see [models and providers](models-and-providers.md)).
