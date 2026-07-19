"""Helpers for injecting daemon control-channel configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from ..core.compose import COMPOSE_FILE, SERVICE_AGENT
from ..core.config import load_env, save_env
from ..core.docker import DockerEnv
from . import (
    CONTAINER_DAEMON_TOKEN_FILE,
    DEFAULT_BIND_HOST,
    DEFAULT_BIND_PORT,
    DEFAULT_DAEMON_API_BASE,
    DEFAULT_DAEMON_SOCKS_URL,
    DEFAULT_SOCKS_BIND_HOST,
    DEFAULT_SOCKS_BIND_PORT,
)
from .instances import InstanceRegistry


@dataclass(frozen=True)
class DaemonChannelResult:
    """Summary of daemon channel files and config updates."""

    instance_id: str
    token_file: Path
    daemon_json: Path
    env_updated_keys: tuple[str, ...]
    compose_updated: bool
    compose_warning: str | None = None


def daemon_env_values(instance_id: str) -> dict[str, str]:
    """Return .env values visible to the Nekro Agent container."""

    return {
        "NA_TOOLS_DAEMON_ENABLED": "true",
        "NA_TOOLS_DAEMON_API_BASE": DEFAULT_DAEMON_API_BASE,
        "NA_TOOLS_DAEMON_SOCKS": DEFAULT_DAEMON_SOCKS_URL,
        "NA_TOOLS_DAEMON_INSTANCE_ID": instance_id,
    }


def ensure_daemon_channel(
    data_dir: Path,
    *,
    overwrite_env: bool = False,
    http_bind: str | None = None,
    socks_bind: str | None = None,
    update_compose: bool = True,
    docker_factory: type[DockerEnv] | None = DockerEnv,
) -> DaemonChannelResult:
    """Prepare token, daemon metadata, .env values, and compose injection."""

    resolved_data_dir = data_dir.expanduser().resolve()
    http_bind = http_bind or f"{DEFAULT_BIND_HOST}:{DEFAULT_BIND_PORT}"
    socks_bind = socks_bind or f"{DEFAULT_SOCKS_BIND_HOST}:{DEFAULT_SOCKS_BIND_PORT}"

    registry = InstanceRegistry(resolved_data_dir, docker_factory=docker_factory)
    registry.prepare(
        http_bind=http_bind,
        socks_bind=socks_bind,
        api_base=DEFAULT_DAEMON_API_BASE,
        socks_url=DEFAULT_DAEMON_SOCKS_URL,
        write_pid=False,
    )

    env_path = resolved_data_dir / ".env"
    env = load_env(env_path)
    updated_keys: list[str] = []
    for key, value in daemon_env_values(registry.instance_id).items():
        if overwrite_env or key not in env:
            if env.get(key) != value:
                updated_keys.append(key)
            env[key] = value
    if updated_keys or not env_path.exists():
        save_env(env_path, env)

    compose_updated = False
    compose_warning = None
    if update_compose:
        compose_updated, compose_warning = patch_compose_daemon_channel(resolved_data_dir)

    return DaemonChannelResult(
        instance_id=registry.instance_id,
        token_file=registry.paths.token_file,
        daemon_json=registry.paths.daemon_json,
        env_updated_keys=tuple(updated_keys),
        compose_updated=compose_updated,
        compose_warning=compose_warning,
    )


def patch_compose_daemon_channel(data_dir: Path) -> tuple[bool, str | None]:
    """Inject daemon env and host gateway into the nekro_agent service."""

    compose_path = data_dir / COMPOSE_FILE
    if not compose_path.exists():
        return False, f"compose file not found: {compose_path}"

    try:
        with open(compose_path, encoding="utf-8") as f:
            content = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        return False, f"compose file cannot be read safely: {exc}"

    if not isinstance(content, dict):
        return False, "compose file is not a YAML mapping"
    data = cast(dict[str, Any], content)
    services = data.get("services")
    if not isinstance(services, dict):
        return False, "compose services section is missing or invalid"
    service = services.get(SERVICE_AGENT)
    if not isinstance(service, dict):
        return False, f"compose service {SERVICE_AGENT!r} is missing or invalid"

    modified = False

    env_modified, env_warning = _patch_service_environment(service)
    if env_warning:
        return False, env_warning
    modified = modified or env_modified

    hosts_modified, hosts_warning = _patch_extra_hosts(service)
    if hosts_warning:
        return False, hosts_warning
    modified = modified or hosts_modified

    if modified:
        with open(compose_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    return modified, None


def _compose_env_values() -> dict[str, str]:
    return {
        "NA_TOOLS_DAEMON_ENABLED": "${NA_TOOLS_DAEMON_ENABLED:-false}",
        "NA_TOOLS_DAEMON_API_BASE": (
            "${NA_TOOLS_DAEMON_API_BASE:-http://na-tools.local/v1}"
        ),
        "NA_TOOLS_DAEMON_SOCKS": (
            "${NA_TOOLS_DAEMON_SOCKS:-socks5h://host.docker.internal:18082}"
        ),
        "NA_TOOLS_DAEMON_INSTANCE_ID": "${NA_TOOLS_DAEMON_INSTANCE_ID:-}",
        "NA_TOOLS_DAEMON_TOKEN_FILE": CONTAINER_DAEMON_TOKEN_FILE,
    }


def _patch_service_environment(service: dict[str, Any]) -> tuple[bool, str | None]:
    desired = _compose_env_values()
    current = service.get("environment")
    if current is None:
        service["environment"] = [f"{key}={value}" for key, value in desired.items()]
        return True, None

    if isinstance(current, dict):
        modified = False
        env_map = cast(dict[str, Any], current)
        for key, value in desired.items():
            if key not in env_map:
                env_map[key] = value
                modified = True
        return modified, None

    if isinstance(current, list):
        env_list = cast(list[Any], current)
        modified = False
        for key, value in desired.items():
            if not _environment_list_has_key(env_list, key):
                env_list.append(f"{key}={value}")
                modified = True
        return modified, None

    return False, "compose service environment is not a mapping or list"


def _environment_list_has_key(items: list[Any], key: str) -> bool:
    for item in items:
        if isinstance(item, str):
            name = item.split("=", 1)[0].strip()
            if name == key:
                return True
        elif isinstance(item, dict) and key in item:
            return True
    return False


def _patch_extra_hosts(service: dict[str, Any]) -> tuple[bool, str | None]:
    desired = "host.docker.internal:host-gateway"
    current = service.get("extra_hosts")
    if current is None:
        service["extra_hosts"] = [desired]
        return True, None

    if isinstance(current, list):
        hosts = cast(list[Any], current)
        if desired not in hosts:
            hosts.append(desired)
            return True, None
        return False, None

    if isinstance(current, dict):
        hosts_map = cast(dict[str, Any], current)
        if hosts_map.get("host.docker.internal") != "host-gateway":
            hosts_map["host.docker.internal"] = "host-gateway"
            return True, None
        return False, None

    return False, "compose service extra_hosts is not a mapping or list"
