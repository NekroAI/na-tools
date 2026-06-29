"""logs 命令：查看服务日志。"""

from pathlib import Path

import click

from ..core.compose import SERVICE_AGENT
from ..services.instance_service import InstanceService, InstanceServiceError
from ..utils.console import error
from ..utils.privilege import with_sudo_fallback


@click.command()
@with_sudo_fallback
@click.argument("service", default=SERVICE_AGENT)
@click.option("--data-dir", type=click.Path(), default=None, help="数据目录路径")
@click.option("--follow", "-f", is_flag=True, default=False, help="持续跟踪日志")
@click.option(
    "--tail",
    "-n",
    type=click.IntRange(0),
    default=100,
    help="显示最后 N 行",
)
def logs(service: str, data_dir: str | None, follow: bool, tail: int) -> None:
    """查看指定服务的日志。"""
    try:
        InstanceService().logs(
            service,
            data_dir=Path(data_dir).expanduser().resolve() if data_dir else None,
            follow=follow,
            tail=tail,
        )
    except InstanceServiceError as exc:
        error(exc.message)
