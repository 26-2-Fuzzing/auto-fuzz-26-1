"""Concurrent lifecycle and SFTP collection for the three Raspberry Pis."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ssh_manager import RemoteProcess, SSHManager, remote_join


@dataclass
class CaptureHandle:
    bus: str
    remote_jsonl: str
    remote_stdout: str
    process: RemoteProcess


class RemoteCapture:
    def __init__(
        self,
        managers: Mapping[str, SSHManager],
        project_dirs: Mapping[str, str],
        python_commands: Mapping[str, str],
        receiver_configs: Mapping[str, str],
        remote_root: str,
    ):
        self.managers = dict(managers)
        self.project_dirs = dict(project_dirs)
        self.python_commands = dict(python_commands)
        self.receiver_configs = dict(receiver_configs)
        self.remote_root = remote_root
        self.handles: dict[str, CaptureHandle] = {}

    def _start_one(self, bus: str, experiment_id: int, trial_id: int) -> CaptureHandle:
        manager = self.managers[bus]
        remote_dir = remote_join(
            self.remote_root, f"experiment_{experiment_id:04d}", f"trial_{trial_id:04d}"
        )
        manager.ensure_directory(remote_dir)
        remote_jsonl = remote_join(remote_dir, f"{bus}.jsonl")
        remote_stdout = remote_join(remote_dir, f"{bus}_capture.stdout.log")
        command = [
            self.python_commands[bus],
            remote_join(self.project_dirs[bus], "can_receiver.py"),
            "--config", remote_join(self.project_dirs[bus], self.receiver_configs[bus]),
            "--bus-name", bus,
            "--output", remote_jsonl,
            "--output-policy", "fail",
            "--experiment-id", str(experiment_id),
            "--print-mode", "none",
            "--no-report",
        ]
        process = manager.start_process(command, remote_stdout)
        return CaptureHandle(bus, remote_jsonl, remote_stdout, process)

    def start_all(self, experiment_id: int, trial_id: int) -> dict[str, CaptureHandle]:
        if self.handles:
            raise RuntimeError("Captures are already running")
        with ThreadPoolExecutor(max_workers=len(self.managers)) as executor:
            futures = {
                bus: executor.submit(self._start_one, bus, experiment_id, trial_id)
                for bus in self.managers
            }
            try:
                self.handles = {bus: future.result() for bus, future in futures.items()}
            except Exception:
                for bus, future in futures.items():
                    if future.done() and not future.exception():
                        handle = future.result()
                        self.managers[bus].stop_process(handle.process)
                raise
        return dict(self.handles)

    def stop_all(self) -> None:
        handles = dict(self.handles)
        if not handles:
            return
        errors = []
        with ThreadPoolExecutor(max_workers=len(handles)) as executor:
            futures = {
                bus: executor.submit(self.managers[bus].stop_process, handle.process)
                for bus, handle in handles.items()
            }
            for bus, future in futures.items():
                try:
                    future.result()
                except Exception as exc:
                    errors.append(f"{bus}: {exc}")
        self.handles = {}
        if errors:
            raise RuntimeError("Capture stop failure: " + "; ".join(errors))

    def assert_all_running(self) -> None:
        if set(self.handles) != set(self.managers):
            raise RuntimeError("Not all configured captures were started")
        stopped = [
            bus for bus, handle in self.handles.items()
            if not self.managers[bus].process_alive(handle.process)
        ]
        if stopped:
            raise RuntimeError(
                "Capture exited before injection; refusing to transmit: "
                + ", ".join(sorted(stopped))
            )

    def download_all(self, trial_dir: Path) -> dict[str, Path]:
        paths: dict[str, Path] = {}
        handles = dict(self.handles)
        if not handles:
            raise RuntimeError("Capture handles are not available for download")
        with ThreadPoolExecutor(max_workers=len(handles)) as executor:
            futures = {}
            for bus, handle in handles.items():
                local_path = trial_dir / f"{bus}.jsonl"
                paths[bus] = local_path
                futures[bus] = executor.submit(
                    self.managers[bus].download, handle.remote_jsonl, local_path
                )
            for future in futures.values():
                future.result()
        return paths

    def stop_and_download(self, trial_dir: Path) -> dict[str, Path]:
        # Retain handles across stop so SFTP knows the exact immutable paths.
        handles = dict(self.handles)
        stop_error = None
        try:
            self.stop_all()
        except Exception as exc:
            stop_error = exc
        self.handles = handles
        try:
            paths = self.download_all(trial_dir)
        finally:
            self.handles = {}
        if stop_error is not None:
            raise stop_error
        return paths
