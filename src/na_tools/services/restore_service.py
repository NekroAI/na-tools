"""Reusable restore service for Nekro Agent backups."""

from __future__ import annotations

import shutil
import sys
import tarfile
import tempfile
from collections.abc import Callable
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..core.compose import COMPOSE_FILE, resolve_service_volumes
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir, resolve_mirror
from .common import EventSink, ServiceError, ServiceEvent, null_event_sink


class DockerLike(Protocol):
    compose_installed: bool

    def down(self, cwd: Path, env_file: Path | None = None) -> bool:
        """Run docker compose down."""

    def up(self, cwd: Path, env_file: Path | None = None) -> bool:
        """Run docker compose up -d."""

    def compose(
        self,
        *args: str,
        cwd: Path | None = None,
        env_file: Path | None = None,
        check: bool = True,
        capture: bool = False,
    ) -> object:
        """Run docker compose."""

    def run_ephemeral(
        self,
        image: str,
        cmd: list[str],
        volumes: dict[str, str],
        workdir: str | None = None,
    ) -> bool:
        """Run a short-lived container."""

    def get_compose_config(
        self, cwd: Path, env_file: Path | None = None
    ) -> dict[str, object] | None:
        """Return compose config."""

    def get_service_volume(
        self,
        cwd: Path,
        service: str,
        target: str,
        env_file: Path | None = None,
    ) -> str | None:
        """Return a named volume mounted by a service."""


DockerFactory = Callable[[], DockerLike]


@dataclass(frozen=True)
class RestoreRequest:
    """Explicit restore request."""

    backup_file: Path
    data_dir: Path | None = None
    start_service: bool | None = True
    choose_start_service: Callable[[], bool] | None = None


@dataclass(frozen=True)
class RestoreResult:
    """Summary of a completed restore."""

    data_dir: Path
    backup_file: Path
    service_stopped: bool
    service_started: bool
    restored_volumes: tuple[str, ...] = field(default_factory=tuple)


class RestoreServiceError(ServiceError):
    """Structured restore failure."""


@dataclass
class RestoreService:
    """Restore a Nekro Agent backup archive."""

    docker_factory: DockerFactory = DockerEnv

    def run(
        self,
        request: RestoreRequest,
        sink: EventSink = null_event_sink,
    ) -> RestoreResult:
        data_dir = Path(request.data_dir or default_data_dir()).expanduser().resolve()
        backup_path = request.backup_file.expanduser().resolve()

        if not tarfile.is_tarfile(backup_path):
            raise RestoreServiceError("invalid_backup", f"不是有效的备份文件: {backup_path}")

        docker = self.docker_factory()
        env_path = data_dir / ".env"
        env_file = env_path if env_path.exists() else None
        service_stopped = False
        if (data_dir / COMPOSE_FILE).exists() and docker.compose_installed:
            sink(ServiceEvent("info", "正在停止现有服务..."))
            _ = docker.down(cwd=data_dir, env_file=env_file)
            service_stopped = True

        restored_volumes: list[str] = []
        sink(ServiceEvent("info", f"正在恢复备份到: {data_dir}"))
        try:
            with tarfile.open(backup_path, "r:gz") as tar:
                members = tar.getmembers()
                if not members:
                    raise RestoreServiceError("empty_backup", "备份文件为空。")
                top_dir = members[0].name.split("/")[0]
                with tempfile.TemporaryDirectory() as tmp_dir:
                    if sys.version_info >= (3, 12):
                        tar.extractall(tmp_dir, filter="data")
                    else:
                        tar.extractall(tmp_dir)
                    tmp_path = Path(tmp_dir)
                    extracted_dir = tmp_path / top_dir
                    volumes_backup_dir = tmp_path / "volumes"
                    has_volumes = volumes_backup_dir.exists() and volumes_backup_dir.is_dir()

                    if extracted_dir.exists():
                        data_dir.mkdir(parents=True, exist_ok=True)
                        mirror = resolve_mirror(env_file)
                        alpine_image = f"{mirror}/alpine:latest" if mirror else "alpine:latest"
                        for item in extracted_dir.iterdir():
                            if item.name == "volumes":
                                continue
                            dest = data_dir / item.name
                            if dest.exists():
                                remove_existing_path(dest, data_dir, docker, alpine_image, sink)
                            _ = shutil.move(str(item), str(dest))

                    if has_volumes:
                        restored_volumes.extend(
                            self._restore_volumes(
                                data_dir,
                                env_file,
                                volumes_backup_dir,
                                docker,
                                sink,
                            )
                        )
            sink(ServiceEvent("success", "备份恢复完成!"))
        except Exception as exc:
            if isinstance(exc, (RestoreServiceError, PermissionError)):
                raise
            if "Permission denied" in str(exc):
                raise
            raise RestoreServiceError("restore_failed", f"恢复失败: {exc}") from exc

        service_started = False
        start_service = request.start_service
        if start_service is None and request.choose_start_service is not None:
            start_service = request.choose_start_service()
        if bool(start_service) and (data_dir / COMPOSE_FILE).exists() and docker.compose_installed:
            sink(ServiceEvent("info", "正在启动服务..."))
            if docker.up(cwd=data_dir, env_file=env_path if env_path.exists() else None):
                service_started = True
                sink(ServiceEvent("success", "服务已启动。"))
            else:
                sink(ServiceEvent("warning", "服务启动失败，请手动启动。"))

        return RestoreResult(
            data_dir=data_dir,
            backup_file=backup_path,
            service_stopped=service_stopped,
            service_started=service_started,
            restored_volumes=tuple(restored_volumes),
        )

    def _restore_volumes(
        self,
        data_dir: Path,
        env_file: Path | None,
        volumes_backup_dir: Path,
        docker: DockerLike,
        sink: EventSink,
    ) -> list[str]:
        sink(ServiceEvent("info", "发现存储卷备份，正在恢复..."))
        if not ((data_dir / COMPOSE_FILE).exists() and docker.compose_installed):
            return []

        sink(ServiceEvent("info", "正在初始化服务容器..."))
        _ = docker.compose(
            "up",
            "--no-start",
            cwd=data_dir,
            env_file=env_file,
            check=False,
        )
        volume_map = {
            filename: vol_name
            for vol_name, filename in resolve_service_volumes(docker, data_dir, env_file)
        }
        restored: list[str] = []
        mirror = resolve_mirror(env_file)
        alpine_image = f"{mirror}/alpine:latest" if mirror else "alpine:latest"
        for volume_file in volumes_backup_dir.iterdir():
            target_volume = volume_map.get(volume_file.name)
            if not target_volume:
                continue
            sink(ServiceEvent("info", f"正在恢复存储卷 {target_volume} ({volume_file.name})..."))
            success_restore = docker.run_ephemeral(
                image=alpine_image,
                cmd=["tar", "xzf", f"/backup/{volume_file.name}", "-C", "/data"],
                volumes={target_volume: "/data", str(volumes_backup_dir): "/backup"},
            )
            if success_restore:
                restored.append(target_volume)
                sink(ServiceEvent("success", f"卷恢复完成: {target_volume}"))
            else:
                sink(ServiceEvent("error", f"卷恢复失败: {target_volume}"))
        return restored


def remove_existing_path(
    path: Path,
    data_dir: Path,
    docker: DockerLike,
    alpine_image: str,
    sink: EventSink = null_event_sink,
) -> None:
    """Remove an existing restore target, using Docker for container-owned files."""

    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return
    except PermissionError as exc:
        if remove_existing_path_with_docker(path, data_dir, docker, alpine_image, sink):
            return
        raise exc


def remove_existing_path_with_docker(
    path: Path,
    data_dir: Path,
    docker: DockerLike,
    alpine_image: str,
    sink: EventSink = null_event_sink,
) -> bool:
    """Try to remove one top-level child through a helper container."""

    try:
        relative = path.relative_to(data_dir)
    except ValueError:
        return False
    if len(relative.parts) != 1:
        return False

    sink(ServiceEvent("warning", f"检测到容器写入的受限文件，尝试通过 Docker 清理: {path}"))
    return docker.run_ephemeral(
        image=alpine_image,
        cmd=[
            "sh",
            "-c",
            'rm -rf -- "$1"',
            "sh",
            f"/restore-target/{relative.name}",
        ],
        volumes={str(data_dir): "/restore-target"},
    )
