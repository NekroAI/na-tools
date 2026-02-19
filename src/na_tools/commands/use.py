"""use 命令：切换当前数据目录。"""

import click
from pathlib import Path

from ..core.platform import set_default_data_dir
from ..core.compose import compose_exists
from ..utils.console import success, info


@click.command()
@click.argument(
    "data_dir", type=click.Path(exists=True, file_okay=False, dir_okay=True)
)
def use(data_dir: str) -> None:
    """切换当前激活的 Nekro Agent 数据目录。

    DATA_DIR: 目标数据目录路径。
    """
    path = Path(data_dir).expanduser().resolve()

    if not compose_exists(path):
        # 即使没有 compose 文件，可能用户只是想切到一个还没安装好的目录？
        # 不过为了安全，最好还是给个警告或者确认。
        # 这里我们假设切换的目标必须是一个有效的na-tools目录（至少看起来像）
        pass
        # error(f"该目录似乎不是一个有效的 Nekro Agent 数据目录: {path}")
        # return

    set_default_data_dir(path)
    success(f"已切换当前数据目录至: {path}")
    info("后续命令将默认操作该目录。")
