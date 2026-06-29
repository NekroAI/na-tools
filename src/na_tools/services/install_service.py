"""Reusable install service for Nekro Agent."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..core.compose import (
    apply_mirror_to_compose,
    download_compose,
    patch_compose_isolation,
    set_image_tag,
)
from ..core.config import load_env, setup_env
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir, resolve_mirror, set_default_data_dir
from ..daemon.channel import DaemonChannelResult, ensure_daemon_channel
from .common import EventSink, ServiceError, ServiceEvent, null_event_sink
from .daemon_service import (
    DaemonRootServiceManager,
    DaemonRootServiceResult,
    DaemonServiceError,
)


class DockerLike(Protocol):
    docker_installed: bool
    compose_installed: bool

    def ensure_docker(self) -> bool:
        """Ensure Docker is installed and available."""

    def pull(self, cwd: Path, env_file: Path | None = None) -> bool:
        """Run docker compose pull."""

    def up(self, cwd: Path, env_file: Path | None = None) -> bool:
        """Run docker compose up -d."""

    def docker_pull(self, image: str, mirror: str = "") -> bool:
        """Pull a single image."""


DockerFactory = Callable[[], DockerLike]


@dataclass(frozen=True)
class InstallRequest:
    """Resolved install request."""

    data_dir: Path | None = None
    with_napcat: bool = False
    port: int | None = None
    interactive_env: bool = False
    preview: bool = False
    start_daemon: bool = True
    with_cc_sandbox: bool | None = False
    continue_after_env: Callable[[], bool] | None = None
    choose_cc_sandbox: Callable[[], bool] | None = None


@dataclass(frozen=True)
class InstallResult:
    """Summary of a completed install."""

    data_dir: Path
    env_path: Path
    daemon_channel: DaemonChannelResult
    expose_port: str
    admin_password: str
    onebot_token: str
    channel: str
    with_napcat: bool
    daemon_service: DaemonRootServiceResult | None = None
    napcat_port: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


class InstallServiceError(ServiceError):
    """Structured install failure."""


@dataclass
class InstallService:
    """Install Nekro Agent into a data directory."""

    docker_factory: DockerFactory = DockerEnv
    daemon_service_manager: DaemonRootServiceManager = field(
        default_factory=DaemonRootServiceManager
    )

    def run(
        self,
        request: InstallRequest,
        sink: EventSink = null_event_sink,
    ) -> InstallResult:
        docker = self.docker_factory()
        if not docker.ensure_docker():
            raise InstallServiceError("docker_unavailable", "Docker 环境不可用。")

        data_dir = Path(request.data_dir or default_data_dir()).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        sink(ServiceEvent("info", f"数据目录: {data_dir}"))

        sink(ServiceEvent("info", "正在配置 .env 文件..."))
        try:
            env_path = setup_env(
                data_dir,
                interactive=request.interactive_env,
                with_napcat=request.with_napcat,
                port=request.port,
            )
        except RuntimeError as exc:
            raise InstallServiceError("env_setup_failed", str(exc)) from exc
        if request.continue_after_env is not None and not request.continue_after_env():
            raise InstallServiceError("install_cancelled", "安装已取消。您可以编辑 .env 文件后重新运行安装。")

        sink(ServiceEvent("info", "正在下载 docker-compose.yml..."))
        if not download_compose(data_dir, with_napcat=request.with_napcat):
            raise InstallServiceError("compose_download_failed", "无法下载 docker-compose.yml，请检查网络连接。")

        patch_compose_isolation(data_dir)
        mirror = resolve_mirror(env_path)
        if mirror:
            sink(ServiceEvent("info", f"应用镜像站配置: {mirror}"))
            apply_mirror_to_compose(data_dir, mirror)

        if request.preview:
            sink(ServiceEvent("info", "使用 preview 频道镜像..."))
            if not set_image_tag(data_dir, "kromiose/nekro-agent", "preview"):
                sink(ServiceEvent("warning", "无法修改镜像 tag，将使用默认 latest 版本。"))

        daemon_channel = ensure_daemon_channel(data_dir, overwrite_env=True)
        sink(ServiceEvent("info", f"daemon 实例 ID: {daemon_channel.instance_id}"))
        if daemon_channel.compose_warning:
            sink(ServiceEvent("warning", f"daemon compose 配置未自动合并: {daemon_channel.compose_warning}"))
        elif daemon_channel.compose_updated:
            sink(ServiceEvent("info", "已写入 daemon compose 环境变量和 host gateway。"))

        sink(ServiceEvent("info", "正在拉取服务镜像..."))
        if not docker.pull(cwd=data_dir, env_file=env_path):
            raise InstallServiceError("pull_failed", "镜像拉取失败，请检查网络连接。")

        sink(ServiceEvent("info", "正在启动服务..."))
        if not docker.up(cwd=data_dir, env_file=env_path):
            raise InstallServiceError("start_failed", "服务启动失败。")

        set_default_data_dir(data_dir)

        daemon_service: DaemonRootServiceResult | None = None
        if request.start_daemon:
            sink(ServiceEvent("info", "正在注册并启动 root daemon 服务..."))
            try:
                daemon_service = self.daemon_service_manager.install_and_start(data_dir)
            except DaemonServiceError as exc:
                raise InstallServiceError(exc.code, exc.message, exc.details) from exc
            sink(ServiceEvent("info", f"daemon root 服务: {daemon_service.service_name}"))

        warnings: list[str] = []
        sink(ServiceEvent("info", "正在拉取沙盒镜像..."))
        if not docker.docker_pull("kromiose/nekro-agent-sandbox", mirror=mirror):
            message = "沙盒镜像拉取失败，可稍后手动拉取: docker pull kromiose/nekro-agent-sandbox"
            warnings.append(message)
            sink(ServiceEvent("warning", message))

        with_cc_sandbox = request.with_cc_sandbox
        if with_cc_sandbox is None and request.choose_cc_sandbox is not None:
            with_cc_sandbox = request.choose_cc_sandbox()
        if bool(with_cc_sandbox):
            sink(ServiceEvent("info", "正在拉取 CC 沙盒镜像..."))
            if not docker.docker_pull("kromiose/nekro-cc-sandbox", mirror=mirror):
                message = "CC 沙盒镜像拉取失败，可稍后手动拉取: docker pull kromiose/nekro-cc-sandbox"
                warnings.append(message)
                sink(ServiceEvent("warning", message))

        env = load_env(env_path)
        return InstallResult(
            data_dir=data_dir,
            env_path=env_path,
            daemon_channel=daemon_channel,
            expose_port=env.get("NEKRO_EXPOSE_PORT", "8021"),
            admin_password=env.get("NEKRO_ADMIN_PASSWORD", ""),
            onebot_token=env.get("ONEBOT_ACCESS_TOKEN", ""),
            channel="preview" if request.preview else "stable",
            with_napcat=request.with_napcat,
            daemon_service=daemon_service,
            napcat_port=env.get("NAPCAT_EXPOSE_PORT", "6099") if request.with_napcat else None,
            warnings=tuple(warnings),
        )
