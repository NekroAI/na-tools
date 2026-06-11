"""Current-instance registry and daemon metadata files."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .. import __version__
from ..core.compose import COMPOSE_FILE, SERVICE_AGENT
from ..core.config import get_container_name, load_env
from ..core.docker import DockerEnv
from ..services.update_service import read_agent_image
from . import (
    DEFAULT_DAEMON_API_BASE,
    DEFAULT_DAEMON_SOCKS_URL,
    DEFAULT_SOCKS_BIND_HOST,
    DEFAULT_SOCKS_BIND_PORT,
    JOB_LOG_RETENTION_DAYS,
    PROTOCOL_VERSION,
    PROVIDER,
)


class DockerSummary(Protocol):
    """Subset of DockerEnv used by the daemon instance registry."""

    docker_installed: bool
    compose_installed: bool
    compose_cmd: list[str] | None


@dataclass(frozen=True)
class DaemonPaths:
    """Filesystem layout for one bound instance."""

    data_dir: Path
    meta_dir: Path
    jobs_dir: Path
    daemon_json: Path
    token_file: Path
    pid_file: Path
    salt_file: Path


class InstanceRegistry:
    """Manage the daemon binding for the current Nekro Agent instance."""

    def __init__(
        self,
        data_dir: Path,
        *,
        docker_factory: type[DockerEnv] | None = DockerEnv,
        started_at: str | None = None,
    ) -> None:
        self.data_dir = data_dir.expanduser().resolve()
        self.paths = DaemonPaths(
            data_dir=self.data_dir,
            meta_dir=self.data_dir / ".na-tools",
            jobs_dir=self.data_dir / ".na-tools" / "jobs",
            daemon_json=self.data_dir / ".na-tools" / "daemon.json",
            token_file=self.data_dir / ".na-tools" / "daemon.token",
            pid_file=self.data_dir / ".na-tools" / "daemon.pid",
            salt_file=self.data_dir / ".na-tools" / "instance.salt",
        )
        self._docker_factory = docker_factory or DockerEnv
        self.started_at = started_at or _utc_now()
        self.instance_id: str = ""
        self.http_bind = "127.0.0.1:18081"
        self.socks_bind = f"{DEFAULT_SOCKS_BIND_HOST}:{DEFAULT_SOCKS_BIND_PORT}"
        self.api_base = DEFAULT_DAEMON_API_BASE
        self.socks_url = DEFAULT_DAEMON_SOCKS_URL
        self.daemon_pid: int | None = None

    def prepare(
        self,
        *,
        http_bind: str,
        socks_bind: str | None = None,
        api_base: str = DEFAULT_DAEMON_API_BASE,
        socks_url: str = DEFAULT_DAEMON_SOCKS_URL,
        write_pid: bool = True,
        daemon_pid: int | None = None,
    ) -> None:
        """Create daemon metadata and calculate the stable instance id."""

        self.http_bind = http_bind
        self.socks_bind = socks_bind or self.socks_bind
        self.api_base = api_base
        self.socks_url = socks_url
        self.daemon_pid = daemon_pid if daemon_pid is not None else (
            os.getpid() if write_pid else None
        )
        self.paths.meta_dir.mkdir(parents=True, exist_ok=True)
        self.paths.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_token()
        self._ensure_salt()
        self.instance_id = self._calculate_instance_id()
        if write_pid and self.daemon_pid is not None:
            self._write_pid(self.daemon_pid)
        self._write_daemon_json()

    def token(self) -> bytes:
        """Return the daemon token bytes used as HMAC key."""

        return self.paths.token_file.read_bytes().strip()

    def capabilities(self) -> dict[str, object]:
        """Return current daemon capabilities."""

        unavailable_reason = self.unavailable_reason()
        return {
            "enabled": unavailable_reason is None,
            "provider": PROVIDER,
            "protocol_version": PROTOCOL_VERSION,
            "platform": platform.system().lower(),
            "instance_id": self.instance_id,
            "supports": {
                "update": True,
                "preview": True,
                "rollback": True,
                "backup": True,
                "restore_pre_preview": True,
                "cancel": False,
                "log_stream": True,
                "daemon_update": False,
            },
            "limits": {
                "max_parallel_jobs_per_instance": 1,
                "job_log_retention_days": JOB_LOG_RETENTION_DAYS,
            },
            "unavailable_reason": unavailable_reason,
        }

    def current_instance(self) -> dict[str, object]:
        """Return information for the current bound instance."""

        env_path = self.data_dir / ".env"
        compose_path = self.data_dir / COMPOSE_FILE
        env = load_env(env_path)
        image, image_tag = read_agent_image(self.data_dir)
        expose_port = env.get("NEKRO_EXPOSE_PORT", "8021") or "8021"
        docker = self._docker()
        unavailable_reason = self.unavailable_reason(docker=docker)

        return {
            "instance_id": self.instance_id,
            "data_dir": str(self.data_dir),
            "compose_file": str(compose_path),
            "env_file": str(env_path),
            "channel": "preview" if image_tag == "preview" else "stable",
            "available": unavailable_reason is None,
            "unavailable_reason": unavailable_reason,
            "app": {
                "expose_port": int(expose_port) if expose_port.isdigit() else expose_port,
                "health_url": f"http://127.0.0.1:{expose_port}/api/health",
            },
            "container": {
                "name": get_container_name(SERVICE_AGENT, env),
                "status": "unknown",
                "image": image,
                "image_tag": image_tag,
            },
            "docker": {
                "docker_installed": docker.docker_installed,
                "compose_installed": docker.compose_installed,
                "compose_cmd": docker.compose_cmd,
            },
        }

    def unavailable_reason(self, *, docker: DockerSummary | None = None) -> str | None:
        """Return the first reason the instance cannot run update jobs."""

        if not (self.data_dir / COMPOSE_FILE).exists():
            return "compose_missing"
        if not (self.data_dir / ".env").exists():
            return "env_missing"
        docker = docker or self._docker()
        if not docker.docker_installed or not docker.compose_installed:
            return "docker_unavailable"
        return None

    def _docker(self) -> DockerSummary:
        return self._docker_factory()

    def _ensure_token(self) -> None:
        if not self.paths.token_file.exists():
            token = secrets.token_hex(32).encode("ascii") + b"\n"
            fd = os.open(
                self.paths.token_file,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(fd, "wb") as f:
                f.write(token)
        if os.name != "nt":
            os.chmod(self.paths.token_file, 0o600)

    def _ensure_salt(self) -> None:
        if not self.paths.salt_file.exists():
            self.paths.salt_file.write_text(secrets.token_hex(16), encoding="utf-8")

    def _calculate_instance_id(self) -> str:
        env = load_env(self.data_dir / ".env")
        project_name = (
            env.get("COMPOSE_PROJECT_NAME") or env.get("INSTANCE_NAME") or self.data_dir.name
        )
        salt = self.paths.salt_file.read_text(encoding="utf-8").strip()
        material = "\0".join([str(self.data_dir), project_name, salt])
        return "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _write_pid(self, daemon_pid: int) -> None:
        self.paths.pid_file.write_text(f"{daemon_pid}\n", encoding="utf-8")

    def _write_daemon_json(self) -> None:
        existing = self._read_daemon_json()
        daemon_pid = self.daemon_pid
        if daemon_pid is None and isinstance(existing.get("daemon_pid"), int):
            daemon_pid = existing["daemon_pid"]
        started_at = self.started_at
        if (
            daemon_pid is not None
            and self.daemon_pid is None
            and isinstance(existing.get("started_at"), str)
        ):
            started_at = existing["started_at"]

        data = {
            "protocol_version": PROTOCOL_VERSION,
            "api_base": self.api_base,
            "socks_url": self.socks_url,
            "instance_id": self.instance_id,
            "data_dir": str(self.data_dir),
            "token_file": str(self.paths.token_file),
            "http_bind": self.http_bind,
            "socks_bind": self.socks_bind,
            "daemon_pid": daemon_pid,
            "daemon_version": __version__,
            "started_at": started_at,
        }
        self.paths.daemon_json.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def _read_daemon_json(self) -> dict[str, object]:
        if not self.paths.daemon_json.exists():
            return {}
        try:
            data = json.loads(self.paths.daemon_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
