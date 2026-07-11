"""status 命令：查看服务状态。"""

from pathlib import Path

import click

from ..services.instance_service import InstanceService, InstanceServiceError
from ..utils.console import console, error, info
from ..utils.privilege import with_sudo_fallback


@click.command()
@with_sudo_fallback
@click.option("--data-dir", type=click.Path(), default=None, help="数据目录路径")
def status(data_dir: str | None) -> None:
    """查看 Nekro Agent 服务状态。"""
    try:
        result = InstanceService().status(
            Path(data_dir).expanduser().resolve() if data_dir else None
        )
    except InstanceServiceError as exc:
        error(exc.message)
        raise click.Abort() from exc

    if result.output:
        info(f"数据目录: {result.data_dir}\n")
        console.print(result.output)
    else:
        info("没有运行中的服务。")
