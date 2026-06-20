"""daemon command group for the host-side update API."""

from __future__ import annotations

import json
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


@daemon.command()
@click.option("--data-dir", type=click.Path(), default=None, help="Nekro Agent data dir")
@click.option("--json", "as_json", is_flag=True, help="Print raw daemon.json")
def status(data_dir: str | None, as_json: bool) -> None:
    """Print daemon metadata for the bound instance."""

    resolved_data_dir = Path(data_dir or default_data_dir()).expanduser().resolve()
    daemon_json = resolved_data_dir / ".na-tools" / "daemon.json"
    if not daemon_json.exists():
        error(f"daemon metadata not found: {daemon_json}")
        raise click.Abort()
    payload = json.loads(daemon_json.read_text(encoding="utf-8"))
    if as_json:
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    token_file_value = payload.get("token_file")
    token_file = Path(str(token_file_value)) if token_file_value else None
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
@click.option("--data-dir", type=click.Path(), default=None, help="Nekro Agent data dir")
def stop(data_dir: str | None) -> None:
    """Show the daemon pid; service stop is implemented by the supervisor."""

    resolved_data_dir = Path(data_dir or default_data_dir()).expanduser().resolve()
    pid_file = resolved_data_dir / ".na-tools" / "daemon.pid"
    if not pid_file.exists():
        error(f"daemon pid not found: {pid_file}")
        raise click.Abort()
    info(f"daemon pid: {pid_file.read_text(encoding='utf-8').strip()}")
    info("stop the process through your shell or service supervisor")
