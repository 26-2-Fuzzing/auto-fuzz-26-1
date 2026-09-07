"""Small Paramiko SSH/SFTP wrapper with mockable process boundaries."""

from __future__ import annotations

import os
import shlex
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Optional, Sequence

from can_common import require_module


@dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    exit_status: int


@dataclass(frozen=True)
class RemoteProcess:
    pid: int
    command: tuple[str, ...]
    stdout_path: str


class SSHManager:
    def __init__(
        self,
        config: Mapping[str, Any],
        client_factory: Optional[Callable[[], Any]] = None,
    ):
        self.config = dict(config)
        self._client_factory = client_factory
        self.client: Any = None

    def connect(self) -> None:
        if self.client is not None:
            return
        paramiko = require_module("paramiko")
        client = self._client_factory() if self._client_factory else paramiko.SSHClient()
        client.load_system_host_keys()
        if self.config.get("allow_unknown_host_key", False):
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        else:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        password = None
        password_env = self.config.get("password_env")
        if password_env:
            password = os.environ.get(str(password_env))
            if password is None:
                raise RuntimeError(f"SSH password environment variable is missing: {password_env}")
        client.connect(
            hostname=str(self.config["host"]),
            port=int(self.config.get("port", 22)),
            username=self.config.get("user"),
            key_filename=self.config.get("key_filename"),
            password=password,
            timeout=float(self.config.get("connect_timeout_seconds", 10.0)),
            look_for_keys=bool(self.config.get("look_for_keys", True)),
            allow_agent=bool(self.config.get("allow_agent", True)),
        )
        self.client = client

    def run(
        self,
        command: Sequence[str],
        *,
        timeout: Optional[float] = None,
        check: bool = True,
    ) -> CommandResult:
        self.connect()
        rendered = shlex.join(str(item) for item in command)
        stdin, stdout, stderr = self.client.exec_command(rendered, timeout=timeout)
        del stdin
        result = CommandResult(
            stdout.read().decode("utf-8", errors="replace"),
            stderr.read().decode("utf-8", errors="replace"),
            int(stdout.channel.recv_exit_status()),
        )
        if check and result.exit_status != 0:
            raise RuntimeError(
                f"Remote command failed ({result.exit_status}): {rendered}\n{result.stderr.strip()}"
            )
        return result

    def ensure_directory(self, path: str) -> None:
        self.run(["mkdir", "-p", "--", path])

    def start_process(
        self,
        command: Sequence[str],
        stdout_path: str,
    ) -> RemoteProcess:
        self.connect()
        rendered = shlex.join(str(item) for item in command)
        wrapper = (
            f"nohup {rendered} > {shlex.quote(stdout_path)} 2>&1 < /dev/null "
            "& echo $!"
        )
        stdin, stdout, stderr = self.client.exec_command(
            f"sh -lc {shlex.quote(wrapper)}"
        )
        del stdin
        output = stdout.read().decode("utf-8", errors="replace").strip()
        error = stderr.read().decode("utf-8", errors="replace").strip()
        status = int(stdout.channel.recv_exit_status())
        try:
            pid = int(output.splitlines()[-1])
        except (IndexError, ValueError) as exc:
            raise RuntimeError(f"Unable to start remote process: {error or output}") from exc
        if status != 0 or pid <= 1:
            raise RuntimeError(f"Unable to start remote process: {error or output}")
        return RemoteProcess(pid, tuple(str(item) for item in command), stdout_path)

    def stop_process(self, process: RemoteProcess, timeout_seconds: float = 10.0) -> None:
        if process.pid <= 1:
            raise ValueError("Refusing to signal an invalid remote PID")
        self.run(["kill", "-TERM", "--", str(process.pid)], check=False)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            status = self.run(["kill", "-0", "--", str(process.pid)], check=False)
            if status.exit_status != 0:
                return
            time.sleep(0.1)
        raise TimeoutError(f"Remote process {process.pid} did not stop after SIGTERM")

    def process_alive(self, process: RemoteProcess) -> bool:
        if process.pid <= 1:
            return False
        return self.run(
            ["kill", "-0", "--", str(process.pid)], check=False
        ).exit_status == 0

    def download(self, remote_path: str, local_path: Path) -> None:
        self.connect()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        sftp = self.client.open_sftp()
        try:
            sftp.get(remote_path, str(local_path))
        finally:
            sftp.close()

    def clock_sample(self) -> dict[str, Any]:
        started = time.time_ns()
        result = self.run(["date", "+%s%N"])
        ended = time.time_ns()
        remote_ns = int(result.stdout.strip())
        midpoint = (started + ended) // 2
        chrony = self.run(["chronyc", "tracking"], check=False)
        return {
            "offset_ms": (remote_ns - midpoint) / 1_000_000.0,
            "round_trip_ms": (ended - started) / 1_000_000.0,
            "chrony_available": chrony.exit_status == 0,
            "chrony_tracking": chrony.stdout.strip() if chrony.exit_status == 0 else None,
        }

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None


def remote_join(*parts: str) -> str:
    return str(PurePosixPath(parts[0]).joinpath(*parts[1:]))
