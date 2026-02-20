"""list 命令：列出所有已安装的 Nekro Agent。"""

import click
from datetime import datetime

from ..core.platform import load_global_config
from ..utils.console import console


from typing import cast

from ..utils.privilege import with_sudo_fallback


@click.command(name="list")
@with_sudo_fallback
def list_cmd() -> None:
    """列出本机所有已记录的 Nekro Agent 安装目录。"""
    config = load_global_config()
    current_data_dir = config.get("current_data_dir")
    installations = config.get("installations", {})

    if not isinstance(installations, dict) or not installations:
        console.print("暂无安装记录。")
        return

    installations = cast(dict[str, dict[str, int | float]], installations)

    # 按路径排序，保证顺序稳定，方便用索引引用
    sorted_paths = sorted(installations.keys())

    for idx, path in enumerate(sorted_paths, start=1):
        is_current = path == current_data_dir
        marker = "*" if is_current else " "

        # 获取最后使用时间
        install_info = installations[path]
        last_used_ts = install_info.get("last_used", 0)

        last_used_str = "-"
        if last_used_ts > 0:
            dt = datetime.fromtimestamp(last_used_ts)
            last_used_str = dt.strftime("%Y-%m-%d %H:%M:%S")

        color = "green" if is_current else "white"
        row_str = (
            f"[{color}]{marker} [{idx}] {path} (最后使用: {last_used_str})[/{color}]"
        )
        console.print(row_str)

    if not current_data_dir:
        console.print(
            "\n[yellow]提示: 当前未激活任何数据目录，请使用 'na-tools use <序号>' 切换。[/yellow]"
        )
