"""Reusable NapCat configuration service."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..core.compose import SERVICE_AGENT
from ..core.config import get_container_name, get_service_name, load_env
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir
from .common import ServiceError


class DockerLike(Protocol):
    def restart_service(
        self, service: str, cwd: Path, env_file: Path | None = None
    ) -> bool:
        """Restart a compose service."""


DockerFactory = Callable[[], DockerLike]


@dataclass(frozen=True)
class NapcatPrepareResult:
    data_dir: Path
    env_path: Path
    napcat_port: str
    token: str
    missing_napcat_port: bool


@dataclass(frozen=True)
class NapcatConfigureRequest:
    data_dir: Path | None
    qq: str
    overwrite: bool
    restart: bool


@dataclass(frozen=True)
class NapcatConfigureResult:
    data_dir: Path
    config_path: Path
    qq: str
    ws_url: str
    token_set: bool
    restarted: bool
    restart_service_name: str


class NapcatServiceError(ServiceError):
    """Structured NapCat-service failure."""


@dataclass
class NapcatService:
    """Prepare NapCat OneBot client configuration."""

    docker_factory: DockerFactory = DockerEnv

    def prepare(self, data_dir: Path | None = None) -> NapcatPrepareResult:
        resolved = Path(data_dir or default_data_dir()).expanduser().resolve()
        env_path = resolved / ".env"
        if not env_path.exists():
            raise NapcatServiceError("env_missing", f"未找到 .env 文件: {env_path}")
        env = load_env(env_path)
        return NapcatPrepareResult(
            data_dir=resolved,
            env_path=env_path,
            napcat_port=env.get("NAPCAT_EXPOSE_PORT", "6099"),
            token=env.get("ONEBOT_ACCESS_TOKEN", ""),
            missing_napcat_port=not bool(env.get("NAPCAT_EXPOSE_PORT")),
        )

    def configure(self, request: NapcatConfigureRequest) -> NapcatConfigureResult:
        prepared = self.prepare(request.data_dir)
        qq = request.qq.strip()
        if not qq:
            raise NapcatServiceError("invalid_qq", "QQ 号不能为空。")
        if not qq.isdigit():
            raise NapcatServiceError("invalid_qq", "QQ 号只能包含数字。")

        env = load_env(prepared.env_path)
        na_hostname = get_container_name(SERVICE_AGENT, env)
        ws_url = f"ws://{na_hostname}:8021/onebot/v11/ws"
        config_path = napcat_config_path(prepared.data_dir, qq)
        if config_path.exists() and not request.overwrite:
            raise NapcatServiceError("config_exists", f"配置文件已存在: {config_path}")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(build_onebot_config(ws_url, prepared.token), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        napcat_service = get_service_name("nekro_napcat")
        restarted = False
        if request.restart:
            docker = self.docker_factory()
            restarted = docker.restart_service(
                napcat_service,
                cwd=prepared.data_dir,
                env_file=prepared.env_path,
            )
        return NapcatConfigureResult(
            data_dir=prepared.data_dir,
            config_path=config_path,
            qq=qq,
            ws_url=ws_url,
            token_set=bool(prepared.token),
            restarted=restarted,
            restart_service_name=napcat_service,
        )


def napcat_config_path(data_dir: Path, qq: str) -> Path:
    """Return the NapCat onebot config file path."""

    return data_dir / "napcat_data" / "napcat" / f"onebot11_{qq}.json"


def build_onebot_config(ws_url: str, token: str) -> dict[str, object]:
    """Build NapCat OneBot WebSocket client config."""

    return {
        "network": {
            "httpServers": [],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [],
            "websocketClients": [
                {
                    "enable": True,
                    "name": "na",
                    "url": ws_url,
                    "reportSelfMessage": False,
                    "messagePostFormat": "array",
                    "token": token,
                    "debug": False,
                    "heartInterval": 30000,
                    "reconnectInterval": 7000,
                }
            ],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
        "imageDownloadProxy": "",
    }
