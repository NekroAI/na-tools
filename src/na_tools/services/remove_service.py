"""Reusable remove service for Nekro Agent instances."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..core.compose import compose_exists
from ..core.config import load_env
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir, load_global_config, save_global_config
from .common import EventSink, ServiceError, ServiceEvent, null_event_sink
from .daemon_service import (
    DaemonRootServiceManager,
    DaemonRootServiceResult,
    DaemonServiceError,
)


class DockerLike(Protocol):
    compose_installed: bool

    def down(self, cwd: Path, env_file: Path | None = None) -> bool:
        """Run docker compose down."""

    def compose(
        self,
        *args: str,
        cwd: Path | None = None,
        env_file: Path | None = None,
        check: bool = True,
        capture: bool = False,
    ) -> object:
        """Run docker compose."""


DockerFactory = Callable[[], DockerLike]


@dataclass(frozen=True)
class RemovePreview:
    """Information shown before removing an instance."""

    data_dir: Path
    is_managed: bool
    keep_data: bool
    instance_name: str | None = None


@dataclass(frozen=True)
class RemoveRequest:
    """Explicit remove request."""

    data_dir: Path | None = None
    keep_data: bool = False
    remove_daemon: bool = True


@dataclass(frozen=True)
class RemoveResult:
    """Summary of instance removal."""

    data_dir: Path
    keep_data: bool
    was_managed: bool
    service_stopped: bool
    containers_removed: bool
    data_removed: bool
    remaining_installations: int
    daemon_service: DaemonRootServiceResult | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


class RemoveServiceError(ServiceError):
    """Structured remove failure."""


@dataclass
class RemoveService:
    """Remove a managed or unmanaged Nekro Agent instance."""

    docker_factory: DockerFactory = DockerEnv
    daemon_service_manager: DaemonRootServiceManager = field(
        default_factory=DaemonRootServiceManager
    )

    def preview(self, data_dir: Path | None, *, keep_data: bool) -> RemovePreview:
        resolved = Path(data_dir or default_data_dir()).expanduser().resolve()
        self._validate_data_dir(resolved)
        config = load_global_config()
        installations = config.get("installations", {})
        if not isinstance(installations, dict):
            installations = {}
        env_path = resolved / ".env"
        instance_name = None
        if env_path.exists():
            instance_name = load_env(env_path).get("INSTANCE_NAME") or None
        return RemovePreview(
            data_dir=resolved,
            is_managed=str(resolved) in installations,
            keep_data=keep_data,
            instance_name=instance_name,
        )

    def run(
        self,
        request: RemoveRequest,
        sink: EventSink = null_event_sink,
    ) -> RemoveResult:
        data_dir = Path(request.data_dir or default_data_dir()).expanduser().resolve()
        self._validate_data_dir(data_dir)
        str_path = str(data_dir)
        env_path = data_dir / ".env"
        env_file = env_path if env_path.exists() else None
        docker = self.docker_factory()
        warnings: list[str] = []
        service_stopped = False
        containers_removed = False
        daemon_service: DaemonRootServiceResult | None = None

        if request.remove_daemon:
            sink(ServiceEvent("info", "\n正在删除 root daemon 服务..."))
            try:
                daemon_service = self.daemon_service_manager.uninstall_registered(data_dir)
                sink(ServiceEvent("success", f"daemon 服务已删除: {daemon_service.service_name}"))
            except DaemonServiceError as exc:
                if exc.code == "daemon_service_missing":
                    message = "未找到已注册的 root daemon 服务，跳过删除"
                    warnings.append(message)
                    sink(ServiceEvent("warning", message))
                else:
                    raise RemoveServiceError(exc.code, exc.message, exc.details) from exc

        if docker.compose_installed:
            sink(ServiceEvent("info", "\n正在停止服务..."))
            if docker.down(cwd=data_dir, env_file=env_file):
                service_stopped = True
                sink(ServiceEvent("success", "服务已停止"))
            else:
                warnings.append("服务停止失败，可能已经停止")
                sink(ServiceEvent("warning", "服务停止失败，可能已经停止"))

            sink(ServiceEvent("info", "正在删除容器..."))
            try:
                _ = docker.compose("down", "-v", cwd=data_dir, env_file=env_file)
                containers_removed = True
                sink(ServiceEvent("success", "容器已删除"))
            except Exception as exc:
                message = f"容器删除时出现问题: {exc}"
                warnings.append(message)
                sink(ServiceEvent("warning", message))
        else:
            warnings.append("Docker Compose 不可用，跳过服务停止")
            sink(ServiceEvent("warning", "Docker Compose 不可用，跳过服务停止"))

        config = load_global_config()
        installations = config.get("installations", {})
        if not isinstance(installations, dict):
            installations = {}
        was_managed = str_path in installations
        if was_managed:
            sink(ServiceEvent("info", "\n正在从管理列表移除..."))
            del installations[str_path]
            config["installations"] = installations
            if config.get("current_data_dir") == str_path:
                config.pop("current_data_dir", None)
            save_global_config(config)
            sink(ServiceEvent("success", "已从管理列表移除"))

        data_removed = False
        if not request.keep_data:
            sink(ServiceEvent("info", "\n正在删除数据目录..."))
            try:
                shutil.rmtree(data_dir)
                data_removed = True
                sink(ServiceEvent("success", f"数据目录已删除: {data_dir}"))
            except Exception as exc:
                message = f"数据目录删除失败: {exc}"
                warnings.append(message)
                sink(ServiceEvent("warning", message))
                sink(ServiceEvent("info", f"您可能需要手动删除: {data_dir}"))
        else:
            sink(ServiceEvent("info", f"数据目录已保留: {data_dir}"))

        return RemoveResult(
            data_dir=data_dir,
            keep_data=request.keep_data,
            was_managed=was_managed,
            service_stopped=service_stopped,
            containers_removed=containers_removed,
            daemon_service=daemon_service,
            data_removed=data_removed,
            remaining_installations=len(installations),
            warnings=tuple(warnings),
        )

    def _validate_data_dir(self, data_dir: Path) -> None:
        if not data_dir.exists():
            raise RemoveServiceError("data_dir_missing", f"数据目录不存在: {data_dir}")
        if not compose_exists(data_dir):
            raise RemoveServiceError("compose_missing", f"该目录不是有效的 NA 安装目录: {data_dir}")
