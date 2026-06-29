"""use 命令：切换当前数据目录。"""

import click

from ..services.instance_service import InstanceService, InstanceServiceError
from ..utils.console import error, info, success
from ..utils.privilege import with_sudo_fallback


@click.command()
@with_sudo_fallback
@click.argument("data_dir", required=True)
def use(data_dir: str) -> None:
    """切换当前激活的 Nekro Agent 数据目录。"""
    try:
        path = InstanceService().use(data_dir)
    except InstanceServiceError as exc:
        error(exc.message)
        return
    success(f"已切换当前数据目录至: {path}")
    info("后续命令将默认操作该目录。")
