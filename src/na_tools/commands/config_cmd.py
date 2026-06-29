"""config 命令：管理全局配置。"""

import click

from ..services.config_service import ConfigService
from ..utils.console import info, success


@click.group()
def config() -> None:
    """管理全局配置。"""


@config.command("mirror")
@click.argument("value", required=False)
def config_mirror(value: str | None) -> None:
    """查看或设置全局 Docker 镜像源。"""
    service = ConfigService()
    if value is None:
        current = service.get_mirror()
        if current:
            info(f"当前全局镜像源: {current}")
        else:
            info("未配置全局镜像源。")
        return

    service.set_mirror(value)
    if value:
        success(f"全局镜像源已设置: {value}")
    else:
        success("全局镜像源已清除。")
