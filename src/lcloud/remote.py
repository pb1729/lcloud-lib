from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from pathlib import Path


class RemoteError(RuntimeError):
    pass


class Remote:
    def __init__(self, ip: str, key_path: str, *, user: str = "ubuntu") -> None:
        self.ip = ip
        self.key_path = str(Path(key_path).expanduser())
        self.user = user

    @property
    def target(self) -> str:
        return f"{self.user}@{self.ip}"

    def ssh_argv(self) -> list[str]:
        return [
            "ssh",
            "-i",
            self.key_path,
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=4",
            "-o",
            "StrictHostKeyChecking=accept-new",
            self.target,
        ]

    def command(
        self,
        command: str,
        *,
        stdin: bytes | None = None,
        attempts: int = 12,
        retry_interval: float = 5,
    ) -> None:
        for attempt in range(1, attempts + 1):
            result = subprocess.run(self.ssh_argv() + [command], input=stdin)
            if result.returncode == 0:
                return
            if result.returncode != 255 or attempt == attempts:
                raise subprocess.CalledProcessError(
                    result.returncode, self.ssh_argv() + [command]
                )
            print(
                f"SSH connection dropped; retrying "
                f"({attempt}/{attempts})...",
                file=sys.stderr,
            )
            time.sleep(retry_interval)

    def output(self, command: str) -> str:
        result = subprocess.run(
            self.ssh_argv() + [command],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        return result.stdout

    def stream(self, command: str) -> int:
        """Stream a remote command until it exits or the SSH connection closes."""
        return subprocess.run(self.ssh_argv() + [command]).returncode

    def wait(
        self,
        *,
        timeout: float = 300,
        interval: float = 5,
        stable_connections: int = 3,
    ) -> None:
        deadline = time.monotonic() + timeout
        argv = self.ssh_argv() + ["true"]
        consecutive_successes = 0
        while time.monotonic() < deadline:
            result = subprocess.run(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode == 0:
                consecutive_successes += 1
                if consecutive_successes >= stable_connections:
                    return
                time.sleep(2)
            else:
                consecutive_successes = 0
                time.sleep(interval)
        raise RemoteError(f"SSH did not become ready on {self.target}")

    def rsync_to(self, source: str, destination: str) -> None:
        ssh_transport = " ".join(shlex.quote(part) for part in self.ssh_argv()[:-1])
        source_path = os.path.expanduser(source)
        argv = [
            "rsync",
            "-az",
            "--partial",
            "--info=progress2",
            "-e",
            ssh_transport,
            source_path,
            f"{self.target}:{destination}",
        ]
        transient_codes = {10, 12, 30, 35, 255}
        for attempt in range(1, 7):
            result = subprocess.run(argv)
            if result.returncode == 0:
                return
            if result.returncode not in transient_codes or attempt == 6:
                raise subprocess.CalledProcessError(result.returncode, argv)
            print(
                f"rsync connection dropped during setup; retrying ({attempt}/6)...",
                file=sys.stderr,
            )
            time.sleep(5)

    def rsync_from(self, source: str, destination: str, *, delete: bool = False) -> None:
        ssh_transport = " ".join(shlex.quote(part) for part in self.ssh_argv()[:-1])
        destination_path = os.path.expanduser(destination)
        argv = [
            "rsync",
            "-az",
            "--partial",
            "--info=progress2",
            "-e",
            ssh_transport,
            f"{self.target}:{source}",
            destination_path,
        ]
        if delete:
            argv.insert(4, "--delete-delay")
        transient_codes = {10, 12, 30, 35, 255}
        for attempt in range(1, 7):
            result = subprocess.run(argv)
            if result.returncode == 0:
                return
            if result.returncode not in transient_codes or attempt == 6:
                raise subprocess.CalledProcessError(result.returncode, argv)
            print(
                f"rsync connection dropped during download; retrying ({attempt}/6)...",
                file=sys.stderr,
            )
            time.sleep(5)

    def ssh_command(self) -> str:
        return " ".join(shlex.quote(part) for part in self.ssh_argv())
