"""Reusable backup service for Nekro Agent data."""

from __future__ import annotations

import shutil
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Protocol

from ..core.compose import compose_exists, resolve_service_volumes
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir, get_global_config_dir, resolve_mirror
from .common import EventSink, ServiceError, ServiceEvent, null_event_sink

_CACHE_PATTERNS: list[str] = [
    "napcat_data/QQ/nt_qq/*/nt_data/Log",
    "napcat_data/QQ/nt_qq/*/nt_temp",
    "napcat_data/QQ/Crashpad",
    "logs",
]


class DockerLike(Protocol):
    compose_installed: bool

    def down(self, cwd: Path, env_file: Path | None = None) -> bool:
        """Run docker compose down."""

    def up(self, cwd: Path, env_file: Path | None = None) -> bool:
        """Run docker compose up -d."""

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
ConfigDirGetter = Callable[[], Path]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class BackupRequest:
    """Explicit backup request."""

    data_dir: Path | None = None
    output: Path | None = None
    no_restart: bool = False
    name: str | None = None


@dataclass(frozen=True)
class BackupResult:
    """Summary of a completed backup."""

    data_dir: Path
    backup_path: Path
    skipped_cache: int
    size_bytes: int
    service_stopped: bool
    service_restarted: bool
    volume_backups: tuple[Path, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BackupSummary:
    """Safe backup file summary for CLI and daemon surfaces."""

    path: Path
    name: str | None
    created_at: datetime
    size_bytes: int


class BackupServiceError(ServiceError):
    """Structured backup failure."""


@dataclass
class BackupService:
    """Create and list Nekro Agent backups."""

    docker_factory: DockerFactory = DockerEnv
    config_dir_getter: ConfigDirGetter = get_global_config_dir
    clock: Clock = datetime.now

    def run(
        self,
        request: BackupRequest,
        sink: EventSink = null_event_sink,
    ) -> BackupResult:
        data_dir = Path(request.data_dir or default_data_dir()).expanduser().resolve()
        if not data_dir.exists():
            raise BackupServiceError("data_dir_missing", f"数据目录不存在: {data_dir}")

        docker = self.docker_factory()
        env_path = data_dir / ".env"
        backup_path = self._backup_path(data_dir, request)
        backup_path.parent.mkdir(parents=True, exist_ok=True)

        volume_backups_map: list[tuple[str, str, Path]] = []
        volumes_dir = data_dir / "volumes_backup_tmp"
        env_file = env_path if env_path.exists() else None
        if compose_exists(data_dir) and docker.compose_installed:
            for vol_name, filename in resolve_service_volumes(docker, data_dir, env_file):
                volume_backups_map.append((vol_name, filename, volumes_dir / filename))

        should_restart = False
        if compose_exists(data_dir) and docker.compose_installed:
            sink(ServiceEvent("info", "正在停止服务以确保数据一致性..."))
            if not docker.down(cwd=data_dir, env_file=env_file):
                raise BackupServiceError(
                    "stop_failed",
                    "服务停止失败，为避免备份数据不一致，已中止备份。",
                )
            should_restart = True

        volume_backups: list[Path] = []
        try:
            if volume_backups_map:
                mirror = resolve_mirror(env_file)
                alpine_images = (
                    [f"{mirror}/alpine:latest", "alpine:latest"]
                    if mirror
                    else ["alpine:latest"]
                )
                volumes_dir.mkdir(exist_ok=True)
                for vol_name, filename, backup_file in volume_backups_map:
                    sink(ServiceEvent("info", f"正在备份存储卷 {vol_name}..."))
                    success_backup = False
                    for image in alpine_images:
                        success_backup = docker.run_ephemeral(
                            image=image,
                            cmd=["tar", "czf", f"/backup/{filename}", "-C", "/data", "."],
                            volumes={vol_name: "/data", str(volumes_dir): "/backup"},
                        )
                        if success_backup:
                            break
                        if image != alpine_images[-1]:
                            sink(ServiceEvent("warning", f"镜像 {image} 拉取失败，尝试回退..."))
                    if success_backup:
                        volume_backups.append(backup_file)
                        sink(ServiceEvent("success", f"卷备份完成: {filename}"))
                    else:
                        sink(ServiceEvent("error", f"卷备份失败: {vol_name}"))

            sink(ServiceEvent("info", f"正在备份数据到: {backup_path}"))
            skipped_cache = self._write_archive(data_dir, backup_path, volume_backups)
        except Exception as exc:
            if should_restart and not request.no_restart:
                sink(ServiceEvent("info", "正在重新启动服务..."))
                _ = docker.up(cwd=data_dir, env_file=env_file)
            if isinstance(exc, BackupServiceError):
                raise
            raise BackupServiceError("backup_failed", f"备份失败: {exc}") from exc
        finally:
            if volumes_dir.exists():
                shutil.rmtree(volumes_dir)

        service_restarted = False
        if should_restart and not request.no_restart:
            sink(ServiceEvent("info", "正在重新启动服务..."))
            if docker.up(cwd=data_dir, env_file=env_file):
                service_restarted = True
                sink(ServiceEvent("success", "服务已重新启动。"))
            else:
                sink(ServiceEvent("warning", "服务重启失败，请手动启动。"))

        return BackupResult(
            data_dir=data_dir,
            backup_path=backup_path,
            skipped_cache=skipped_cache,
            size_bytes=backup_path.stat().st_size,
            service_stopped=should_restart,
            service_restarted=service_restarted,
            volume_backups=tuple(volume_backups),
        )

    def list_backups(
        self,
        data_dir: Path | None = None,
        *,
        name: str | None = None,
        limit: int | None = None,
    ) -> list[BackupSummary]:
        resolved_data_dir = Path(data_dir or default_data_dir()).expanduser().resolve()
        backup_dir = backup_dir_for(resolved_data_dir, self.config_dir_getter)
        if not backup_dir.exists():
            return []
        backups = sorted(
            backup_dir.glob("*.tar.gz"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if name:
            backups = [path for path in backups if parse_backup_name(path.name) == name]
        if limit is not None:
            backups = backups[:limit]
        return [backup_summary(path) for path in backups]

    def _backup_path(self, data_dir: Path, request: BackupRequest) -> Path:
        if request.output is not None:
            return request.output.expanduser().resolve()
        timestamp = self.clock().strftime("%Y%m%d_%H%M%S")
        backup_dir = backup_dir_for(data_dir, self.config_dir_getter)
        backup_dir.mkdir(parents=True, exist_ok=True)
        name_part = f"{request.name}_" if request.name else ""
        return backup_dir / f"{data_dir.name}_backup_{name_part}{timestamp}.tar.gz"

    def _write_archive(
        self,
        data_dir: Path,
        backup_path: Path,
        volume_backups: list[Path],
    ) -> int:
        skipped_cache = 0

        def _tar_filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
            nonlocal skipped_cache
            if "volumes_backup_tmp" in ti.name:
                return None
            if is_cache_path(ti.name):
                skipped_cache += 1
                return None
            return ti

        with tarfile.open(backup_path, "w:gz") as tar:
            tar.add(data_dir, arcname=data_dir.name, filter=_tar_filter)
            for volume_backup in volume_backups:
                tar.add(volume_backup, arcname=f"volumes/{volume_backup.name}")
        return skipped_cache


def is_cache_path(arcname: str) -> bool:
    """Return whether an archive member should be skipped as cache."""

    parts = arcname.split("/", 1)
    if len(parts) < 2:
        return False
    rel = parts[1]
    return any(fnmatch(rel, pat) or fnmatch(rel, pat + "/*") for pat in _CACHE_PATTERNS)


def parse_backup_name(filename: str) -> str | None:
    """Parse the optional custom backup name from a backup filename."""

    stem = filename
    for suffix in (".tar.gz", ".tar", ".gz"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    parts = stem.split("_")
    if len(parts) < 4:
        return None
    if not (
        parts[-2].isdigit()
        and len(parts[-2]) == 8
        and parts[-1].isdigit()
        and len(parts[-1]) == 6
    ):
        return None
    try:
        backup_idx = parts.index("backup")
    except ValueError:
        return None
    name_parts = parts[backup_idx + 1 : -2]
    if not name_parts:
        return None
    return "_".join(name_parts)


def backup_dir_for(
    data_dir: Path,
    config_dir_getter: ConfigDirGetter = get_global_config_dir,
) -> Path:
    """Return the standard backup directory for one instance."""

    return config_dir_getter() / "backup" / data_dir.name


def backup_summary(path: Path) -> BackupSummary:
    """Return a structured backup summary."""

    stat = path.stat()
    return BackupSummary(
        path=path,
        name=parse_backup_name(path.name),
        created_at=datetime.fromtimestamp(stat.st_mtime),
        size_bytes=stat.st_size,
    )
