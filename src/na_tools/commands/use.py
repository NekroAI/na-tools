"""use 命令：切换当前数据目录。"""

import click
from pathlib import Path

from ..core.platform import set_default_data_dir, load_global_config
from ..core.compose import compose_exists
from ..utils.console import success, info, error


from typing import cast

from ..utils.privilege import with_sudo_fallback


@click.command()
@with_sudo_fallback
@click.argument("data_dir", required=True)
def use(data_dir: str) -> None:
    """切换当前激活的 Nekro Agent 数据目录。

    DATA_DIR: 目标数据目录路径，或者 'na-tools list' 中的序号。
    """
    path = None

    # 尝试作为序号解析
    if data_dir.isdigit():
        idx = int(data_dir)
        config = load_global_config()
        installations = config.get("installations", {})

        if not isinstance(installations, dict) or not installations:
            error("没有找到任何安装记录，无法使用序号切换。")
            return

        installations = cast(dict[str, dict[str, int]], installations)
        sorted_paths = sorted(installations.keys())
        if 1 <= idx <= len(sorted_paths):
            path = Path(sorted_paths[idx - 1])
        else:
            error(f"序号 {idx} 无效。请使用 'na-tools list' 查看可用序号。")
            return
    else:
        # 作为路径解析
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
