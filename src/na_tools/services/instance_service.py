"""Reusable instance-management services."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast

from ..core.compose import (
    SERVICE_AGENT,
    SERVICE_NAPCAT,
    SERVICE_POSTGRES,
    SERVICE_QDRANT,
    compose_exists,
    list_compose_services,
)
from ..core.config import get_service_name
from ..core.docker import DockerEnv
from ..core.platform import (
    default_data_dir,
    load_global_config,
    save_global_config,
    set_default_data_dir,
)
from ..daemon.channel import DaemonChannelResult, ensure_daemon_channel
from .common import ServiceError


class DockerLike(Protocol):
    compose_installed: bool

    def ps(self, cwd: Path, env_file: Path | None = None) -> str:
        """Run docker compose ps."""

    def logs(
        self,
        service: str,
        cwd: Path,
        *,
        follow: bool = False,
        tail: int = 100,
        env_file: Path | None = None,
    ) -> None:
        """Stream docker compose logs."""


DockerFactory = Callable[[], DockerLike]

LOG_SERVICE_ALIASES: dict[str, str] = {
    "agent": SERVICE_AGENT,
    "nekro-agent": SERVICE_AGENT,
    "nekro_agent": SERVICE_AGENT,
    "postgres": SERVICE_POSTGRES,
    "db": SERVICE_POSTGRES,
    "database": SERVICE_POSTGRES,
    "qdrant": SERVICE_QDRANT,
    "vector": SERVICE_QDRANT,
    "napcat": SERVICE_NAPCAT,
    "qq": SERVICE_NAPCAT,
}


@dataclass(frozen=True)
class BindRequest:
    data_dir: Path
    name: str | None
    as_current: bool


@dataclass(frozen=True)
class BindResult:
    data_dir: Path
    name: str | None
    as_current: bool
    already_bound: bool
    daemon_channel: DaemonChannelResult


@dataclass(frozen=True)
class InstallationEntry:
    index: int
    path: str
    is_current: bool
    last_used: datetime | None


@dataclass(frozen=True)
class StatusResult:
    data_dir: Path
    output: str


class InstanceServiceError(ServiceError):
    """Structured instance-service failure."""


@dataclass
class InstanceService:
    """Manage stored Nekro Agent instances and compose views."""

    docker_factory: DockerFactory = DockerEnv

    def bind(self, request: BindRequest) -> BindResult:
        data_dir = request.data_dir.expanduser().resolve()
        if not data_dir.exists():
            raise InstanceServiceError("data_dir_missing", f"数据目录不存在: {data_dir}")
        if not compose_exists(data_dir):
            raise InstanceServiceError("compose_missing", f"该目录不是有效的 NA 安装目录: {data_dir}")

        config = load_global_config()
        installations = config.get("installations", {})
        if not isinstance(installations, dict):
            installations = {}
        str_path = str(data_dir)
        daemon_channel = ensure_daemon_channel(data_dir, overwrite_env=False)
        already_bound = str_path in installations
        if not already_bound:
            import time

            install_info: dict[str, int | str] = {
                "installed_at": int(time.time()),
                "last_used": int(time.time()),
            }
            if request.name:
                install_info["name"] = request.name
            installations[str_path] = install_info
            config["installations"] = installations
        if request.as_current:
            config["current_data_dir"] = str_path
        save_global_config(config)
        return BindResult(
            data_dir=data_dir,
            name=request.name,
            as_current=request.as_current,
            already_bound=already_bound,
            daemon_channel=daemon_channel,
        )

    def list_installations(self) -> tuple[list[InstallationEntry], bool]:
        config = load_global_config()
        current_data_dir = config.get("current_data_dir")
        installations = config.get("installations", {})
        if not isinstance(installations, dict) or not installations:
            return [], bool(current_data_dir)
        typed = cast(dict[str, dict[str, int | float]], installations)
        entries: list[InstallationEntry] = []
        for idx, path in enumerate(sorted(typed.keys()), start=1):
            last_used_ts = typed[path].get("last_used", 0)
            last_used = (
                datetime.fromtimestamp(last_used_ts)
                if isinstance(last_used_ts, (int, float)) and last_used_ts > 0
                else None
            )
            entries.append(
                InstallationEntry(
                    index=idx,
                    path=path,
                    is_current=path == current_data_dir,
                    last_used=last_used,
                )
            )
        return entries, bool(current_data_dir)

    def resolve_use_target(self, data_dir: str) -> Path:
        if data_dir.isdigit():
            idx = int(data_dir)
            config = load_global_config()
            installations = config.get("installations", {})
            if not isinstance(installations, dict) or not installations:
                raise InstanceServiceError("no_installations", "没有找到任何安装记录，无法使用序号切换。")
            sorted_paths = sorted(installations.keys())
            if not (1 <= idx <= len(sorted_paths)):
                raise InstanceServiceError("invalid_index", f"序号 {idx} 无效。请使用 'na-tools list' 查看可用序号。")
            return Path(sorted_paths[idx - 1])
        return Path(data_dir).expanduser().resolve()

    def use(self, data_dir: str) -> Path:
        path = self.resolve_use_target(data_dir)
        if not path.is_dir():
            raise InstanceServiceError("data_dir_missing", f"目录不存在: {path}")
        if not compose_exists(path):
            raise InstanceServiceError(
                "compose_missing",
                f"该目录不是有效的 Nekro Agent 数据目录（缺少 docker-compose.yml）: {path}",
            )
        set_default_data_dir(path)
        return path

    def status(self, data_dir: Path | None = None) -> StatusResult:
        resolved = Path(data_dir or default_data_dir()).expanduser().resolve()
        if not compose_exists(resolved):
            raise InstanceServiceError("compose_missing", f"未找到已有安装。数据目录: {resolved}")
        docker = self.docker_factory()
        if not docker.compose_installed:
            raise InstanceServiceError("docker_unavailable", "Docker Compose 不可用。")
        env_path = resolved / ".env"
        output = docker.ps(cwd=resolved, env_file=env_path if env_path.exists() else None)
        return StatusResult(data_dir=resolved, output=output)

    def logs(
        self,
        service: str,
        *,
        data_dir: Path | None = None,
        follow: bool = False,
        tail: int = 100,
    ) -> None:
        resolved = Path(data_dir or default_data_dir()).expanduser().resolve()
        if not compose_exists(resolved):
            raise InstanceServiceError("compose_missing", f"未找到已有安装。数据目录: {resolved}")
        docker = self.docker_factory()
        if not docker.compose_installed:
            raise InstanceServiceError("docker_unavailable", "Docker Compose 不可用。")
        env_path = resolved / ".env"
        service_name = _resolve_log_service(service)
        available_services = list_compose_services(resolved)
        if available_services and service_name not in available_services:
            available = ", ".join(sorted(available_services))
            raise InstanceServiceError(
                "service_missing",
                f"服务不存在: {service}。可用服务: {available}",
                details={
                    "service": service,
                    "resolved_service": service_name,
                    "available_services": sorted(available_services),
                },
            )
        docker.logs(
            get_service_name(service_name),
            cwd=resolved,
            follow=follow,
            tail=tail,
            env_file=env_path if env_path.exists() else None,
        )


def _resolve_log_service(service: str) -> str:
    normalized = service.strip().lower()
    return LOG_SERVICE_ALIASES.get(normalized, service.strip())
