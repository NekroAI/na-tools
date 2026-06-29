"""Reusable daemon metadata and root service management."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from subprocess import CalledProcessError
from typing import Callable, Literal

from ..core.platform import default_data_dir, get_os, run_cmd
from ..daemon import DEFAULT_DAEMON_API_BASE, DEFAULT_DAEMON_SOCKS_URL
from .common import ServiceError

DaemonRootAction = Literal["install_start", "start", "stop", "uninstall"]


@dataclass(frozen=True)
class DaemonStatus:
    data_dir: Path
    daemon_json: Path
    payload: dict[str, object]
    token_file: Path | None


class DaemonServiceError(ServiceError):
    """Structured daemon metadata failure."""


@dataclass(frozen=True)
class DaemonRootServiceResult:
    """Summary of a root service operation."""

    data_dir: Path
    service_name: str
    service_path: Path
    action: DaemonRootAction
    command: str


@dataclass
class DaemonService:
    """Read daemon metadata files for commands."""

    def status(self, data_dir: Path | None = None) -> DaemonStatus:
        resolved = Path(data_dir or default_data_dir()).expanduser().resolve()
        daemon_json = resolved / ".na-tools" / "daemon.json"
        if not daemon_json.exists():
            raise DaemonServiceError("daemon_metadata_missing", f"daemon metadata not found: {daemon_json}")
        payload = json.loads(daemon_json.read_text(encoding="utf-8"))
        token_file_value = payload.get("token_file")
        token_file = Path(str(token_file_value)) if token_file_value else None
        return DaemonStatus(
            data_dir=resolved,
            daemon_json=daemon_json,
            payload=payload,
            token_file=token_file,
        )

    def pid(self, data_dir: Path | None = None) -> str:
        resolved = Path(data_dir or default_data_dir()).expanduser().resolve()
        pid_file = resolved / ".na-tools" / "daemon.pid"
        if not pid_file.exists():
            raise DaemonServiceError("daemon_pid_missing", f"daemon pid not found: {pid_file}")
        return pid_file.read_text(encoding="utf-8").strip()

    @staticmethod
    def default_api_base() -> str:
        return DEFAULT_DAEMON_API_BASE

    @staticmethod
    def default_socks_url() -> str:
        return DEFAULT_DAEMON_SOCKS_URL


Runner = Callable[..., object]
PlatformGetter = Callable[[], str]
Chown = Callable[[Path, int, int], None]
Chmod = Callable[[Path, int], None]
RootChecker = Callable[[], bool]


@dataclass
class DaemonRootServiceManager:
    """Install and control the root/system daemon service."""

    systemd_dir: Path = Path("/etc/systemd/system")
    launchd_dir: Path = Path("/Library/LaunchDaemons")
    runner: Runner = run_cmd
    platform_getter: PlatformGetter = get_os
    executable: str = sys.executable
    chown: Chown = os.chown
    chmod: Chmod = os.chmod
    root_checker: RootChecker = lambda: not hasattr(os, "geteuid") or os.geteuid() == 0

    def install_and_start(self, data_dir: Path) -> DaemonRootServiceResult:
        """Write the root service definition, enable it, and start it."""

        self._ensure_root()
        resolved = data_dir.expanduser().resolve()
        platform_name = self.platform_getter()
        service_name, service_path = self._service_identity(resolved, platform_name)
        self._write_service_file(platform_name, service_path, service_name, resolved)
        try:
            if platform_name == "linux":
                self._run(["systemctl", "daemon-reload"])
                self._run(["systemctl", "enable", service_name])
                self._run(["systemctl", "start", service_name])
                command = "systemctl start"
            elif platform_name == "darwin":
                label = service_path.stem
                self._run(["launchctl", "bootstrap", "system", str(service_path)])
                self._run(["launchctl", "kickstart", "-k", f"system/{label}"])
                command = "launchctl kickstart"
            else:
                raise DaemonServiceError(
                    "unsupported_platform",
                    f"当前系统不支持 root daemon 服务: {platform_name}",
                )
        except (CalledProcessError, OSError) as exc:
            raise self._operation_error("daemon_service_start_failed", exc) from exc

        return DaemonRootServiceResult(
            data_dir=resolved,
            service_name=service_name,
            service_path=service_path,
            action="install_start",
            command=command,
        )

    def start_registered(self, data_dir: Path) -> DaemonRootServiceResult:
        """Start an already registered root service without rewriting it."""

        self._ensure_root()
        return self._control_registered(data_dir, "start")

    def stop_registered(self, data_dir: Path) -> DaemonRootServiceResult:
        """Stop an already registered root service without rewriting it."""

        self._ensure_root()
        return self._control_registered(data_dir, "stop")

    def uninstall_registered(self, data_dir: Path) -> DaemonRootServiceResult:
        """Stop, disable, and remove an already registered root service."""

        resolved = data_dir.expanduser().resolve()
        platform_name = self.platform_getter()
        service_name, service_path = self._service_identity(resolved, platform_name)
        if not service_path.exists():
            raise DaemonServiceError(
                "daemon_service_missing",
                "未找到已注册的 root daemon 服务。",
                details={
                    "service_name": service_name,
                    "service_path": str(service_path),
                    "data_dir": str(resolved),
                },
            )

        self._ensure_root()
        try:
            if platform_name == "linux":
                self._run(["systemctl", "stop", service_name])
                self._run(["systemctl", "disable", service_name])
                service_path.unlink()
                self._run(["systemctl", "daemon-reload"])
                command = "systemctl disable"
            elif platform_name == "darwin":
                label = service_path.stem
                self._run(["launchctl", "bootout", "system", str(service_path)])
                service_path.unlink()
                command = f"launchctl bootout system/{label}"
            else:
                raise DaemonServiceError(
                    "unsupported_platform",
                    f"当前系统不支持 root daemon 服务: {platform_name}",
                )
        except (CalledProcessError, OSError) as exc:
            raise self._operation_error("daemon_service_uninstall_failed", exc) from exc

        return DaemonRootServiceResult(
            data_dir=resolved,
            service_name=service_name,
            service_path=service_path,
            action="uninstall",
            command=command,
        )

    def _control_registered(
        self,
        data_dir: Path,
        action: Literal["start", "stop"],
    ) -> DaemonRootServiceResult:
        resolved = data_dir.expanduser().resolve()
        platform_name = self.platform_getter()
        service_name, service_path = self._service_identity(resolved, platform_name)
        if not service_path.exists():
            raise DaemonServiceError(
                "daemon_service_missing",
                "未找到已注册的 root daemon 服务，请先运行 `na-tools install`，"
                "或使用 `--without-daemon` 跳过 daemon 操作。",
                details={
                    "service_name": service_name,
                    "service_path": str(service_path),
                    "data_dir": str(resolved),
                },
            )

        try:
            if platform_name == "linux":
                self._run(["systemctl", action, service_name])
                command = f"systemctl {action}"
            elif platform_name == "darwin":
                label = service_path.stem
                if action == "start":
                    self._run(["launchctl", "kickstart", "-k", f"system/{label}"])
                    command = "launchctl kickstart"
                else:
                    self._run(["launchctl", "bootout", "system", str(service_path)])
                    command = "launchctl bootout"
            else:
                raise DaemonServiceError(
                    "unsupported_platform",
                    f"当前系统不支持 root daemon 服务: {platform_name}",
                )
        except (CalledProcessError, OSError) as exc:
            code = f"daemon_service_{action}_failed"
            raise self._operation_error(code, exc) from exc

        return DaemonRootServiceResult(
            data_dir=resolved,
            service_name=service_name,
            service_path=service_path,
            action=action,
            command=command,
        )

    def _write_service_file(
        self,
        platform_name: str,
        service_path: Path,
        service_name: str,
        data_dir: Path,
    ) -> None:
        meta_dir = data_dir / ".na-tools"
        meta_dir.mkdir(parents=True, exist_ok=True)
        stdout_log = meta_dir / "daemon.out.log"
        stderr_log = meta_dir / "daemon.err.log"
        service_path.parent.mkdir(parents=True, exist_ok=True)

        if platform_name == "linux":
            content = self._systemd_unit(data_dir, stdout_log, stderr_log)
            service_path.write_text(content, encoding="utf-8")
        elif platform_name == "darwin":
            plist = self._launchd_plist(service_name, data_dir, stdout_log, stderr_log)
            service_path.write_bytes(plistlib.dumps(plist, sort_keys=False))
            self.chown(service_path, 0, 0)
            self.chmod(service_path, 0o644)
        else:
            raise DaemonServiceError(
                "unsupported_platform",
                f"当前系统不支持 root daemon 服务: {platform_name}",
            )

    def _systemd_unit(
        self,
        data_dir: Path,
        stdout_log: Path,
        stderr_log: Path,
    ) -> str:
        command = shlex.join(
            [
                self.executable,
                "-m",
                "na_tools",
                "daemon",
                "start",
                "--data-dir",
                str(data_dir),
            ]
        )
        return "\n".join(
            [
                "[Unit]",
                "Description=NA-Tools daemon",
                "After=network-online.target docker.service",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                f"ExecStart={command}",
                "Restart=always",
                "RestartSec=3",
                f"StandardOutput=append:{stdout_log}",
                f"StandardError=append:{stderr_log}",
                "Environment=NA_TOOLS_DAEMON_MODE=1",
                "",
                "[Install]",
                "WantedBy=multi-user.target",
                "",
            ]
        )

    def _launchd_plist(
        self,
        service_name: str,
        data_dir: Path,
        stdout_log: Path,
        stderr_log: Path,
    ) -> dict[str, object]:
        label = service_name.removesuffix(".plist")
        return {
            "Label": label,
            "ProgramArguments": [
                self.executable,
                "-m",
                "na_tools",
                "daemon",
                "start",
                "--data-dir",
                str(data_dir),
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(stdout_log),
            "StandardErrorPath": str(stderr_log),
            "EnvironmentVariables": {"NA_TOOLS_DAEMON_MODE": "1"},
        }

    def _service_identity(self, data_dir: Path, platform_name: str) -> tuple[str, Path]:
        suffix = self._service_suffix(data_dir)
        if platform_name == "linux":
            service_name = f"na-tools-daemon-{suffix}.service"
            return service_name, self.systemd_dir / service_name
        if platform_name == "darwin":
            service_name = f"io.nekro.na-tools.daemon.{suffix}.plist"
            return service_name, self.launchd_dir / service_name
        raise DaemonServiceError(
            "unsupported_platform",
            f"当前系统不支持 root daemon 服务: {platform_name}",
        )

    @staticmethod
    def _service_suffix(data_dir: Path) -> str:
        daemon_json = data_dir / ".na-tools" / "daemon.json"
        if daemon_json.exists():
            try:
                payload = json.loads(daemon_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                payload = {}
            if isinstance(payload, dict):
                instance_id = payload.get("instance_id")
                if isinstance(instance_id, str) and instance_id:
                    material = instance_id
                else:
                    material = str(data_dir)
            else:
                material = str(data_dir)
        else:
            material = str(data_dir)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]

    def _run(self, cmd: list[str]) -> None:
        self.runner(cmd, capture=True)

    def _ensure_root(self) -> None:
        if not self.root_checker():
            raise PermissionError("root 权限不足，无法操作系统级 daemon 服务。")

    @staticmethod
    def _operation_error(code: str, exc: BaseException) -> DaemonServiceError:
        message = str(exc)
        if isinstance(exc, CalledProcessError):
            output = (exc.stderr or exc.stdout or "").strip()
            if output:
                message = output
        return DaemonServiceError(code, f"daemon root 服务操作失败: {message}")
