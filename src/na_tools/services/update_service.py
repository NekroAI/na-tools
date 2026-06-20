"""Non-interactive update workflow service."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, cast

import httpx
import yaml

from ..core.compose import COMPOSE_FILE, compose_exists, set_image_tag
from ..core.config import load_env
from ..core.docker import DockerAccessErrorCode, DockerEnv, docker_access_error_message
from ..core.platform import get_global_config_dir, resolve_mirror
from .job_events import EventSink, UpdateEvent, UpdatePhase, null_event_sink

UpdateChannel = Literal["stable", "preview", "rollback"]
UpdateErrorCode = Literal[
    "compose_missing",
    "env_missing",
    "docker_unavailable",
    "invalid_channel",
    "backup_failed",
    "backup_not_found",
    "pull_failed",
    "restart_failed",
    "sandbox_pull_failed",
    "verify_timeout",
    "docker_not_running",
    "docker_permission_denied",
    "docker_socket_missing",
]

AGENT_IMAGE = "kromiose/nekro-agent"
SANDBOX_IMAGE = "kromiose/nekro-agent-sandbox"
CC_SANDBOX_IMAGE = "kromiose/nekro-cc-sandbox"
PHASE_PROGRESS: dict[UpdatePhase, int] = {
    "validate_instance": 1,
    "backup": 2,
    "switch_channel": 3,
    "pull_images": 4,
    "restart_services": 5,
    "pull_sandbox": 6,
    "verify": 7,
    "finished": 8,
}


@dataclass(frozen=True)
class UpdateRequest:
    """Explicit, non-interactive request for an update workflow."""

    data_dir: Path
    channel: UpdateChannel = "stable"
    backup: bool = True
    update_sandbox: bool = True
    update_cc_sandbox: bool = False
    restore_pre_preview: bool = False


@dataclass(frozen=True)
class BackupRequest:
    """Backup operation requested by the update workflow."""

    data_dir: Path
    name: str | None = None
    no_restart: bool = True


@dataclass(frozen=True)
class RestoreRequest:
    """Restore operation requested by the update workflow."""

    data_dir: Path
    backup_file: Path


@dataclass(frozen=True)
class HealthCheckResult:
    """Health verification result for the updated app."""

    ok: bool
    url: str
    message: str = "ok"
    status_code: int | None = None


@dataclass(frozen=True)
class UpdateResult:
    """Structured update result for CLI output and daemon job state."""

    channel: UpdateChannel
    image: str | None
    image_tag: str | None
    backup_file: Path | None
    app_health: HealthCheckResult
    warnings: tuple[str, ...] = field(default_factory=tuple)


class UpdateServiceError(Exception):
    """Structured update failure that maps to daemon protocol errors."""

    def __init__(
        self,
        code: UpdateErrorCode,
        message: str,
        *,
        phase: UpdatePhase | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.phase = phase
        self.details = details or {}


class DockerLike(Protocol):
    """Subset of DockerEnv used by UpdateService."""

    docker_installed: bool
    compose_installed: bool

    def pull(self, cwd: Path, env_file: Path | None = None) -> bool:
        """Run docker compose pull."""

    def up(self, cwd: Path, env_file: Path | None = None) -> bool:
        """Run docker compose up -d."""

    def docker_pull(self, image: str, mirror: str = "") -> bool:
        """Pull a single image."""

    def check_access(self) -> DockerAccessErrorCode | None:
        """Return why this process cannot access Docker, if any."""


BackupRunner = Callable[[BackupRequest], Path | None]
RestoreRunner = Callable[[RestoreRequest], None]
DockerFactory = Callable[[], DockerLike]
HealthChecker = Callable[[Path, Path], HealthCheckResult]
ImageTagger = Callable[[Path, str, str], bool]
MirrorResolver = Callable[[Path], str]
ConfigDirGetter = Callable[[], Path]


def default_health_checker(data_dir: Path, env_path: Path) -> HealthCheckResult:
    """Verify app health through the exposed host port."""

    env = load_env(env_path)
    port = env.get("NEKRO_EXPOSE_PORT", "8021") or "8021"
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.monotonic() + 120
    last_message = "health endpoint did not respond"
    last_status: int | None = None

    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=2)
            last_status = response.status_code
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict) and payload.get("ok") is True:
                    return HealthCheckResult(ok=True, url=url, status_code=200)
                last_message = "health response was not ok"
            else:
                last_message = f"health returned HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_message = str(exc)
        time.sleep(2)

    return HealthCheckResult(
        ok=False,
        url=url,
        message=last_message,
        status_code=last_status,
    )


class UpdateService:
    """Reusable update workflow for CLI and future daemon execution."""

    def __init__(
        self,
        *,
        docker_factory: DockerFactory = DockerEnv,
        backup_runner: BackupRunner | None = None,
        restore_runner: RestoreRunner | None = None,
        health_checker: HealthChecker = default_health_checker,
        image_tagger: ImageTagger = set_image_tag,
        mirror_resolver: MirrorResolver = resolve_mirror,
        config_dir_getter: ConfigDirGetter = get_global_config_dir,
        restore_runner_restarts_service: bool = False,
    ) -> None:
        self._docker_factory = docker_factory
        self._backup_runner = backup_runner
        self._restore_runner = restore_runner
        self._health_checker = health_checker
        self._image_tagger = image_tagger
        self._mirror_resolver = mirror_resolver
        self._config_dir_getter = config_dir_getter
        self._restore_runner_restarts_service = restore_runner_restarts_service

    def run(
        self,
        request: UpdateRequest,
        sink: EventSink = null_event_sink,
    ) -> UpdateResult:
        """Run an update workflow without reading stdin or using Click context."""

        data_dir = request.data_dir.expanduser().resolve()
        normalized_request = UpdateRequest(
            data_dir=data_dir,
            channel=request.channel,
            backup=request.backup,
            update_sandbox=request.update_sandbox,
            update_cc_sandbox=request.update_cc_sandbox,
            restore_pre_preview=request.restore_pre_preview,
        )

        if normalized_request.channel not in ("stable", "preview", "rollback"):
            raise UpdateServiceError(
                "invalid_channel",
                f"不支持的更新频道: {normalized_request.channel}",
                phase="validate_instance",
                details={"channel": normalized_request.channel},
            )

        docker = self._validate_instance(normalized_request, sink)
        if normalized_request.channel == "preview":
            return self._run_preview(normalized_request, docker, sink)
        if normalized_request.channel == "rollback":
            return self._run_rollback(normalized_request, docker, sink)
        return self._run_stable(normalized_request, docker, sink)

    def _run_stable(
        self,
        request: UpdateRequest,
        docker: DockerLike,
        sink: EventSink,
    ) -> UpdateResult:
        backup_file: Path | None = None
        warnings: list[str] = []
        env_path = request.data_dir / ".env"

        if request.backup:
            backup_file = self._run_backup(request.data_dir, None, sink)

        self._pull_images(
            docker,
            request.data_dir,
            env_path,
            sink,
            label="正在拉取最新镜像...",
        )
        self._restart_services(docker, request.data_dir, env_path, sink)
        self._pull_sandbox_images(docker, env_path, request, sink, warnings)
        health = self._verify(request.data_dir, env_path, sink)
        image, image_tag = read_agent_image(request.data_dir)
        channel = "preview" if image_tag == "preview" else "stable"
        return self._finish(
            channel=cast(UpdateChannel, channel),
            image=image,
            image_tag=image_tag,
            backup_file=backup_file,
            health=health,
            warnings=warnings,
            sink=sink,
        )

    def _run_preview(
        self,
        request: UpdateRequest,
        docker: DockerLike,
        sink: EventSink,
    ) -> UpdateResult:
        backup_file: Path | None = None
        warnings: list[str] = []
        env_path = request.data_dir / ".env"
        _, current_tag = read_agent_image(request.data_dir)

        if current_tag != "preview":
            backup_file = self._run_backup(request.data_dir, "pre-preview", sink)

        self._phase(sink, "switch_channel", "正在切换到 preview 镜像...")
        if not self._image_tagger(request.data_dir, AGENT_IMAGE, "preview"):
            raise UpdateServiceError(
                "compose_missing",
                "无法修改镜像 tag，请检查 docker-compose.yml。",
                phase="switch_channel",
                details={"image": AGENT_IMAGE, "tag": "preview"},
            )

        self._pull_images(
            docker,
            request.data_dir,
            env_path,
            sink,
            label="正在拉取 preview 镜像...",
        )
        self._restart_services(docker, request.data_dir, env_path, sink)
        self._pull_sandbox_images(docker, env_path, request, sink, warnings)
        health = self._verify(request.data_dir, env_path, sink)
        image, image_tag = read_agent_image(request.data_dir)
        return self._finish(
            channel="preview",
            image=image,
            image_tag=image_tag,
            backup_file=backup_file,
            health=health,
            warnings=warnings,
            sink=sink,
        )

    def _run_rollback(
        self,
        request: UpdateRequest,
        docker: DockerLike,
        sink: EventSink,
    ) -> UpdateResult:
        warnings: list[str] = []
        env_path = request.data_dir / ".env"
        backup_file: Path | None = None

        self._phase(sink, "switch_channel", "正在切换回 latest 镜像...")
        if not self._image_tagger(request.data_dir, AGENT_IMAGE, "latest"):
            raise UpdateServiceError(
                "compose_missing",
                "无法修改镜像 tag，请检查 docker-compose.yml。",
                phase="switch_channel",
                details={"image": AGENT_IMAGE, "tag": "latest"},
            )

        if request.restore_pre_preview:
            backup_file = find_latest_named_backup(
                request.data_dir,
                "pre-preview",
                config_dir_getter=self._config_dir_getter,
            )
            if backup_file is None:
                raise UpdateServiceError(
                    "backup_not_found",
                    "未找到名称为 pre-preview 的历史备份。",
                    phase="backup",
                    details={"name": "pre-preview"},
                )
            self._run_restore(request.data_dir, backup_file, sink)
            if self._restore_runner_restarts_service:
                self._phase(sink, "restart_services", "恢复流程已处理服务启动。")
            else:
                self._restart_services(docker, request.data_dir, env_path, sink)
        else:
            self._pull_images(
                docker,
                request.data_dir,
                env_path,
                sink,
                label="正在拉取 latest 镜像...",
            )
            self._restart_services(docker, request.data_dir, env_path, sink)

        self._pull_sandbox_images(docker, env_path, request, sink, warnings)
        health = self._verify(request.data_dir, env_path, sink)
        image, image_tag = read_agent_image(request.data_dir)
        return self._finish(
            channel="stable",
            image=image,
            image_tag=image_tag,
            backup_file=backup_file,
            health=health,
            warnings=warnings,
            sink=sink,
        )

    def _validate_instance(
        self,
        request: UpdateRequest,
        sink: EventSink,
    ) -> DockerLike:
        self._phase(sink, "validate_instance", "正在检查现有安装...")
        if not compose_exists(request.data_dir):
            raise UpdateServiceError(
                "compose_missing",
                f"未找到已有安装。数据目录: {request.data_dir}",
                phase="validate_instance",
                details={"data_dir": str(request.data_dir)},
            )

        env_path = request.data_dir / ".env"
        if not env_path.exists():
            raise UpdateServiceError(
                "env_missing",
                f"未找到 .env 文件: {env_path}",
                phase="validate_instance",
                details={"env_file": str(env_path)},
            )

        docker = self._docker_factory()
        if not docker.docker_installed or not docker.compose_installed:
            raise UpdateServiceError(
                "docker_unavailable",
                "Docker 环境不可用。",
                phase="validate_instance",
                details={
                    "docker_installed": docker.docker_installed,
                    "compose_installed": docker.compose_installed,
                },
            )
        access_error = docker.check_access()
        if access_error is not None:
            raise UpdateServiceError(
                access_error,
                docker_access_error_message(access_error),
                phase="validate_instance",
                details={
                    "docker_installed": docker.docker_installed,
                    "compose_installed": docker.compose_installed,
                },
            )
        return docker

    def _run_backup(
        self,
        data_dir: Path,
        name: str | None,
        sink: EventSink,
    ) -> Path | None:
        self._phase(
            sink,
            "backup",
            "正在执行切换前自动备份（名称: pre-preview）..."
            if name == "pre-preview"
            else "正在执行更新前备份...",
        )
        if self._backup_runner is None:
            raise UpdateServiceError(
                "backup_failed",
                "未配置备份执行器。",
                phase="backup",
                details={"name": name or ""},
            )
        try:
            return self._backup_runner(
                BackupRequest(data_dir=data_dir, name=name, no_restart=True)
            )
        except UpdateServiceError:
            raise
        except Exception as exc:
            raise UpdateServiceError(
                "backup_failed",
                f"备份失败: {exc}",
                phase="backup",
                details={"name": name or ""},
            ) from exc

    def _run_restore(
        self,
        data_dir: Path,
        backup_file: Path,
        sink: EventSink,
    ) -> None:
        self._phase(sink, "backup", f"正在恢复 pre-preview 备份: {backup_file.name}")
        if self._restore_runner is None:
            raise UpdateServiceError(
                "backup_failed",
                "未配置恢复执行器。",
                phase="backup",
                details={"backup_file": str(backup_file)},
            )
        try:
            self._restore_runner(
                RestoreRequest(data_dir=data_dir, backup_file=backup_file)
            )
        except UpdateServiceError:
            raise
        except Exception as exc:
            raise UpdateServiceError(
                "backup_failed",
                f"恢复备份失败: {exc}",
                phase="backup",
                details={"backup_file": str(backup_file)},
            ) from exc

    def _pull_images(
        self,
        docker: DockerLike,
        data_dir: Path,
        env_path: Path,
        sink: EventSink,
        *,
        label: str,
    ) -> None:
        self._phase(sink, "pull_images", label)
        if not docker.pull(cwd=data_dir, env_file=env_path):
            raise UpdateServiceError(
                "pull_failed",
                "镜像拉取失败。",
                phase="pull_images",
                details={"command": "docker compose pull"},
            )

    def _restart_services(
        self,
        docker: DockerLike,
        data_dir: Path,
        env_path: Path,
        sink: EventSink,
    ) -> None:
        self._phase(sink, "restart_services", "正在重启服务...")
        if not docker.up(cwd=data_dir, env_file=env_path):
            raise UpdateServiceError(
                "restart_failed",
                "服务重启失败。",
                phase="restart_services",
                details={"command": "docker compose up -d"},
            )

    def _pull_sandbox_images(
        self,
        docker: DockerLike,
        env_path: Path,
        request: UpdateRequest,
        sink: EventSink,
        warnings: list[str],
    ) -> None:
        if not request.update_sandbox and not request.update_cc_sandbox:
            return

        self._phase(sink, "pull_sandbox", "正在更新沙盒镜像...")
        mirror = self._mirror_resolver(env_path)
        if request.update_sandbox and not docker.docker_pull(
            SANDBOX_IMAGE, mirror=mirror
        ):
            self._warning(sink, warnings, "沙盒镜像更新失败，可稍后手动更新。")
        if request.update_cc_sandbox and not docker.docker_pull(
            CC_SANDBOX_IMAGE, mirror=mirror
        ):
            self._warning(sink, warnings, "CC 沙盒镜像更新失败，可稍后手动更新。")

    def _verify(
        self,
        data_dir: Path,
        env_path: Path,
        sink: EventSink,
    ) -> HealthCheckResult:
        self._phase(sink, "verify", "正在检查服务健康状态...")
        health = self._health_checker(data_dir, env_path)
        if not health.ok:
            raise UpdateServiceError(
                "verify_timeout",
                "服务健康检查超时。",
                phase="verify",
                details={
                    "url": health.url,
                    "message": health.message,
                    "status_code": health.status_code,
                },
            )
        return health

    def _finish(
        self,
        *,
        channel: UpdateChannel,
        image: str | None,
        image_tag: str | None,
        backup_file: Path | None,
        health: HealthCheckResult,
        warnings: list[str],
        sink: EventSink,
    ) -> UpdateResult:
        self._phase(sink, "finished", "更新流程已完成。")
        result = UpdateResult(
            channel=channel,
            image=image,
            image_tag=image_tag,
            backup_file=backup_file,
            app_health=health,
            warnings=tuple(warnings),
        )
        sink(
            UpdateEvent(
                type="result",
                phase="finished",
                message="更新流程已完成。",
                data={
                    "channel": result.channel,
                    "image": result.image,
                    "image_tag": result.image_tag,
                    "backup_file": str(result.backup_file)
                    if result.backup_file
                    else None,
                    "app_health": "ok" if result.app_health.ok else "failed",
                    "warnings": list(result.warnings),
                },
            )
        )
        return result

    def _phase(self, sink: EventSink, phase: UpdatePhase, message: str) -> None:
        sink(UpdateEvent(type="phase", phase=phase, message=message))
        sink(
            UpdateEvent(
                type="progress",
                phase=phase,
                message=message,
                current=PHASE_PROGRESS[phase],
                total=len(PHASE_PROGRESS),
            )
        )
        sink(UpdateEvent(type="log", phase=phase, message=message, level="info"))

    def _warning(self, sink: EventSink, warnings: list[str], message: str) -> None:
        warnings.append(message)
        sink(
            UpdateEvent(
                type="warning",
                phase="pull_sandbox",
                message=message,
                level="warning",
            )
        )


def read_agent_image(data_dir: Path) -> tuple[str | None, str | None]:
    """Return the configured Nekro Agent image and tag from compose."""

    compose_path = data_dir / COMPOSE_FILE
    if not compose_path.exists():
        return None, None

    with open(compose_path, encoding="utf-8") as f:
        content = yaml.safe_load(f)

    if not isinstance(content, dict):
        return None, None
    services_data = content.get("services")
    if not isinstance(services_data, dict):
        return None, None

    services = cast(dict[str, dict[str, object]], services_data)
    for service_config in services.values():
        image = service_config.get("image")
        if isinstance(image, str) and AGENT_IMAGE in image:
            return image, _extract_tag(image)
    return None, None


def _extract_tag(image: str) -> str | None:
    image_name = image.rsplit("/", 1)[-1]
    if ":" not in image_name:
        return None
    return image_name.rsplit(":", 1)[-1]


def find_latest_named_backup(
    data_dir: Path,
    name: str,
    *,
    config_dir_getter: ConfigDirGetter = get_global_config_dir,
) -> Path | None:
    """Find the latest backup archive matching a parsed backup name."""

    backup_dir = config_dir_getter() / "backup" / data_dir.name
    if not backup_dir.exists():
        return None
    backups = sorted(
        backup_dir.glob("*.tar.gz"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for backup_file in backups:
        if _parse_backup_name(backup_file.name) == name:
            return backup_file
    return None


def _parse_backup_name(filename: str) -> str | None:
    stem = filename
    for suffix in (".tar.gz", ".tar", ".gz"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break

    parts = stem.split("_")
    if len(parts) < 4:
        return None

    date_part = parts[-2]
    time_part = parts[-1]
    if not (
        date_part.isdigit()
        and len(date_part) == 8
        and time_part.isdigit()
        and len(time_part) == 6
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
