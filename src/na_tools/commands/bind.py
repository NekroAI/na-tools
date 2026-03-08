"""bind 命令：将已安装的 NA 实例绑定到 na-tools 管理列表。"""

from pathlib import Path

import click

from ..core.compose import compose_exists
from ..core.platform import (
    load_global_config,
    save_global_config,
    set_default_data_dir,
)
from ..utils.console import confirm, error, info, success, warning
from ..utils.privilege import with_sudo_fallback


@click.command()
@with_sudo_fallback
@click.option(
    "--data-dir",
    type=click.Path(),
    required=True,
    help="NA 实例的数据目录路径",
)
@click.option(
    "--name",
    type=str,
    default=None,
    help="为该实例指定一个名称（可选）",
)
@click.option(
    "--as-current/--no-as-current",
    default=True,
    help="绑定后是否设为当前激活实例（默认设为当前）",
)
def bind(data_dir: str, name: str | None, as_current: bool) -> None:
    """将环境中已手动安装的 NA 实例绑定到 na-tools 管理列表。

    适用于：
    - 之前通过其他方式安装的 NA
    - 从备份恢复的 NA
    - 从其他机器迁移的 NA

    示例：
        na-tools bind --data-dir /path/to/nekro_data
        na-tools bind --data-dir /path/to/nekro_data --name my-na
    """
    data_dir_path = Path(data_dir).expanduser().resolve()

    # 1. 验证目录存在
    if not data_dir_path.exists():
        error(f"数据目录不存在: {data_dir_path}")
        raise click.Abort()

    # 2. 验证是有效的 NA 安装目录
    if not compose_exists(data_dir_path):
        error(f"该目录不是有效的 NA 安装目录: {data_dir_path}")
        info("有效的 NA 目录应包含 docker-compose.yml 文件")
        raise click.Abort()

    # 3. 检查是否已绑定
    config = load_global_config()
    installations = config.get("installations", {})

    if not isinstance(installations, dict):
        installations = {}

    str_path = str(data_dir_path)

    if str_path in installations:
        info(f"该 NA 实例已在管理列表中: {str_path}")
        if as_current:
            config["current_data_dir"] = str_path
            save_global_config(config)
            success("已设为当前激活实例")
        return

    # 4. 绑定新实例
    import time

    install_info: dict[str, int | str] = {
        "installed_at": int(time.time()),
        "last_used": int(time.time()),
    }

    if name:
        install_info["name"] = name

    installations[str_path] = install_info
    config["installations"] = installations

    if as_current:
        config["current_data_dir"] = str_path

    save_global_config(config)

    # 5. 显示结果
    info_lines = [f"已成功绑定 NA 实例: {data_dir_path}"]

    if name:
        info_lines.append(f"实例名称: {name}")

    if as_current:
        info_lines.append("已设为当前激活实例")

    success("\n".join(info_lines))

    info("\n后续操作：")
    info(f"  na-tools status    查看该实例状态")
    info(f"  na-tools list      查看所有实例")
