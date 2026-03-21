"""config 命令：管理全局配置。"""

import click

from ..core.platform import get_global_mirror, set_global_mirror
from ..utils.console import info, success


@click.group()
def config() -> None:
    """管理全局配置。"""


@config.command("mirror")
@click.argument("value", required=False)
def config_mirror(value: str | None) -> None:
    """查看或设置全局 Docker 镜像源。

    \b
    不带参数时显示当前镜像源。
    传入镜像源地址则设置，传入空字符串 "" 则清除。
    """
    if value is None:
        current = get_global_mirror()
        if current:
            info(f"当前全局镜像源: {current}")
        else:
            info("未配置全局镜像源。")
        return

    set_global_mirror(value)
    if value:
        success(f"全局镜像源已设置: {value}")
    else:
        success("全局镜像源已清除。")
