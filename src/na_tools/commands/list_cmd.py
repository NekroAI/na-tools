"""list 命令：列出所有已安装的 Nekro Agent。"""

import click

from ..services.instance_service import InstanceService
from ..utils.console import console
from ..utils.privilege import with_sudo_fallback


@click.command(name="list")
@with_sudo_fallback
def list_cmd() -> None:
    """列出本机所有已记录的 Nekro Agent 安装目录。"""
    entries, has_current = InstanceService().list_installations()
    if not entries:
        console.print("暂无安装记录。")
        return

    for entry in entries:
        marker = "*" if entry.is_current else " "
        last_used_str = entry.last_used.strftime("%Y-%m-%d %H:%M:%S") if entry.last_used else "-"
        color = "green" if entry.is_current else "white"
        console.print(
            f"[{color}]{marker} [{entry.index}] {entry.path} "
            f"(最后使用: {last_used_str})[/{color}]"
        )

    if not has_current:
        console.print(
            "\n[yellow]提示: 当前未激活任何数据目录，请使用 'na-tools use <序号>' 切换。[/yellow]"
        )
