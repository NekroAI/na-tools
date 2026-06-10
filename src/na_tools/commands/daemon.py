"""daemon command group for the host-side update API."""

from __future__ import annotations

import json
from pathlib import Path

import click

from ..core.platform import default_data_dir
from ..daemon import DEFAULT_BIND_HOST, DEFAULT_BIND_PORT
from ..daemon.app import create_app
from ..utils.console import error, info, success


@click.group()
def daemon() -> None:
    """Run and inspect the na-tools daemon."""


@daemon.command()
@click.option("--data-dir", type=click.Path(), default=None, help="Nekro Agent data dir")
@click.option("--host", default=DEFAULT_BIND_HOST, show_default=True, help="HTTP bind host")
@click.option("--port", default=DEFAULT_BIND_PORT, show_default=True, help="HTTP bind port")
def start(data_dir: str | None, host: str, port: int) -> None:
    """Start the daemon HTTP API."""

    import uvicorn

    resolved_data_dir = Path(data_dir or default_data_dir()).expanduser().resolve()
    app = create_app(resolved_data_dir, host=host, port=port)
    success(f"na-tools daemon listening on http://{host}:{port}/v1")
    info(f"bound instance: {app.state.registry.instance_id}")
    uvicorn.run(app, host=host, port=port)


@daemon.command()
@click.option("--data-dir", type=click.Path(), default=None, help="Nekro Agent data dir")
def status(data_dir: str | None) -> None:
    """Print daemon metadata for the bound instance."""

    resolved_data_dir = Path(data_dir or default_data_dir()).expanduser().resolve()
    daemon_json = resolved_data_dir / ".na-tools" / "daemon.json"
    if not daemon_json.exists():
        error(f"daemon metadata not found: {daemon_json}")
        raise click.Abort()
    payload = json.loads(daemon_json.read_text(encoding="utf-8"))
    click.echo(json.dumps(payload, indent=2, ensure_ascii=False))


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

