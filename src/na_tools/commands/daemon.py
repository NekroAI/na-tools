"""daemon command group for the host-side update API."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import click

from ..core.platform import default_data_dir
from ..daemon import (
    DEFAULT_BIND_HOST,
    DEFAULT_BIND_PORT,
    DEFAULT_DAEMON_API_BASE,
    DEFAULT_DAEMON_SOCKS_URL,
    DEFAULT_SOCKS_BIND_HOST,
    DEFAULT_SOCKS_BIND_PORT,
)
from ..daemon.app import create_app
from ..daemon.socks import resolve_default_socks_bind_host
from ..utils.console import error, info, success, warning
from ..utils.privilege import with_sudo_fallback


@click.group()
def daemon() -> None:
    """Run and inspect the na-tools daemon."""


@daemon.command()
@click.option("--data-dir", type=click.Path(), default=None, help="Nekro Agent data dir")
@click.option("--host", default=DEFAULT_BIND_HOST, show_default=True, help="HTTP bind host")
@click.option("--port", default=DEFAULT_BIND_PORT, show_default=True, help="HTTP bind port")
@click.option(
    "--socks-host",
    default=None,
    help="SOCKS5 bind host; defaults to 0.0.0.0 for Docker host-gateway access",
)
@click.option(
    "--socks-port",
    default=DEFAULT_SOCKS_BIND_PORT,
    show_default=True,
    help="SOCKS5 bind port",
)
def start(
    data_dir: str | None,
    host: str,
    port: int,
    socks_host: str | None,
    socks_port: int,
) -> None:
    """Start the daemon HTTP API and SOCKS5 control channel."""

    import uvicorn

    # 设置环境变量，告知 with_sudo_fallback 当前处于 daemon 模式
    import os
    os.environ["NA_TOOLS_DAEMON_MODE"] = "1"

    resolved_data_dir = Path(data_dir or default_data_dir()).expanduser().resolve()
    resolved_socks_host = socks_host or resolve_default_socks_bind_host()
    _ensure_bind_available(host, port, "HTTP API")
    _ensure_bind_available(resolved_socks_host, socks_port, "SOCKS5")
    app = create_app(
        resolved_data_dir,
        host=host,
        port=port,
        socks_host=resolved_socks_host,
        socks_port=socks_port,
        enable_socks=True,
    )
    success(f"na-tools daemon listening on http://{host}:{port}/v1")
    success(f"na-tools daemon SOCKS5 listening on {resolved_socks_host}:{socks_port}")
    if resolved_socks_host == DEFAULT_SOCKS_BIND_HOST:
        warning(
            "SOCKS5 is bound to 0.0.0.0; target whitelist is enforced and HTTP "
            "API still requires HMAC."
        )
    info(f"bound instance: {app.state.registry.instance_id}")
    uvicorn.run(app, host=host, port=port)


def _ensure_bind_available(host: str, port: int, label: str) -> None:
    """Fail early when a daemon listener port is already in use."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, port))
    except OSError as exc:
        error(f"{label} 监听地址不可用: {host}:{port} ({exc.strerror or exc})")
        info("如果 na-tools daemon 已经启动，无需重复启动；可运行 `na-tools daemon status` 查看状态。")
        raise click.Abort() from exc


@daemon.command()
@click.option("--data-dir", type=click.Path(), default=None, help="Nekro Agent data dir")
@click.option("--json", "as_json", is_flag=True, help="Print raw daemon.json")
def status(data_dir: str | None, as_json: bool) -> None:
    """Print daemon metadata for the bound instance."""

    from ..services.daemon_service import DaemonService, DaemonServiceError

    try:
        status_data = DaemonService().status(
            Path(data_dir).expanduser().resolve() if data_dir else None
        )
    except DaemonServiceError as exc:
        error(exc.message)
        raise click.Abort()
    daemon_json = status_data.daemon_json
    payload = status_data.payload
    if as_json:
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    token_file = status_data.token_file
    click.echo("Daemon status")
    click.echo(f"  daemon.json: {daemon_json} (exists: {daemon_json.exists()})")
    click.echo(
        f"  token file: {token_file or '-'} "
        f"(exists: {token_file.exists() if token_file else False})"
    )
    click.echo(f"  instance_id: {payload.get('instance_id') or '-'}")
    click.echo(f"  HTTP bind: {payload.get('http_bind') or '-'}")
    click.echo(f"  SOCKS bind: {payload.get('socks_bind') or '-'}")
    click.echo(f"  API base: {payload.get('api_base') or DEFAULT_DAEMON_API_BASE}")
    click.echo(f"  SOCKS URL: {payload.get('socks_url') or DEFAULT_DAEMON_SOCKS_URL}")
    click.echo(f"  daemon pid: {payload.get('daemon_pid') or '-'}")
    click.echo("")
    click.echo("Container access")
    click.echo(f"  API base: {DEFAULT_DAEMON_API_BASE}")
    click.echo(f"  SOCKS: {DEFAULT_DAEMON_SOCKS_URL}")
    click.echo("")
    click.echo("Troubleshooting order")
    click.echo("  1. NA_TOOLS_DAEMON_ENABLED is not true")
    click.echo("  2. daemon token file is missing")
    click.echo("  3. SOCKS cannot connect")
    click.echo("  4. /v1/health times out")
    click.echo("  5. HMAC signature fails")
    click.echo("  6. instance_id does not match")


@daemon.command()
@with_sudo_fallback
@click.option("--data-dir", type=click.Path(), default=None, help="Nekro Agent data dir")
def stop(data_dir: str | None) -> None:
    """Stop the registered root daemon service."""

    from ..services.daemon_service import DaemonRootServiceManager, DaemonServiceError

    resolved_data_dir = Path(data_dir or default_data_dir()).expanduser().resolve()

    try:
        result = DaemonRootServiceManager().stop_registered(resolved_data_dir)
    except DaemonServiceError as exc:
        error(exc.message)
        if exc.code == "daemon_service_missing":
            info("请先运行 `na-tools install` 注册 daemon 服务。")
        raise click.Abort()
    success(f"daemon root 服务已停止: {result.service_name}")
