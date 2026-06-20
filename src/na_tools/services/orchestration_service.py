"""Reusable Docker Compose orchestration lifecycle service."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from ..core.compose import compose_exists
from ..core.docker import DockerEnv

OrchestrationAction = Literal["start", "stop"]


@dataclass(frozen=True)
class OrchestrationRequest:
    """Explicit request to start or stop a compose orchestration."""

    data_dir: Path
    action: OrchestrationAction


@dataclass(frozen=True)
class OrchestrationResult:
    """Structured orchestration lifecycle result for commands and integrations."""

    data_dir: Path
    action: OrchestrationAction
    env_file: Path | None
    command: str


class OrchestrationServiceError(RuntimeError):
    """Structured orchestration failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class DockerLike(Protocol):
    """Subset of DockerEnv used by the orchestration service."""

    docker_installed: bool
    compose_installed: bool

    def up(self, cwd: Path, env_file: Path | None = None) -> bool:
        """Run docker compose up -d."""

    def down(self, cwd: Path, env_file: Path | None = None) -> bool:
        """Run docker compose down."""


DockerFactory = Callable[[], DockerLike]


@dataclass
class OrchestrationService:
    """Start or stop the compose stack for a bound Nekro Agent instance."""

    docker_factory: DockerFactory = field(default=DockerEnv)

    def run(self, request: OrchestrationRequest) -> OrchestrationResult:
        data_dir = request.data_dir.expanduser().resolve()
        if not compose_exists(data_dir):
            raise OrchestrationServiceError(
                "compose_missing",
                f"未找到 docker-compose.yml。数据目录: {data_dir}",
                details={"data_dir": str(data_dir)},
            )

        docker = self.docker_factory()
        if not docker.docker_installed or not docker.compose_installed:
            raise OrchestrationServiceError(
                "docker_unavailable",
                "Docker 或 Docker Compose 不可用。",
                details={
                    "docker_installed": docker.docker_installed,
                    "compose_installed": docker.compose_installed,
                },
            )

        env_path = data_dir / ".env"
        env_file = env_path if env_path.exists() else None

        if request.action == "start":
            command = "docker compose up -d"
            ok = docker.up(cwd=data_dir, env_file=env_file)
        elif request.action == "stop":
            command = "docker compose down"
            ok = docker.down(cwd=data_dir, env_file=env_file)
        else:
            raise OrchestrationServiceError(
                "unsupported_action",
                f"不支持的编排操作: {request.action}",
                details={"action": request.action},
            )

        if not ok:
            raise OrchestrationServiceError(
                f"{request.action}_failed",
                "编排启动失败。" if request.action == "start" else "编排关闭失败。",
                details={"command": command, "data_dir": str(data_dir)},
            )

        return OrchestrationResult(
            data_dir=data_dir,
            action=request.action,
            env_file=env_file,
            command=command,
        )
