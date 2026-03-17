"""remove 命令：移除（卸载）指定的 NA 实例。"""

from pathlib import Path
from typing import cast

import click

from ..core.compose import compose_exists
from ..core.config import load_env
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir, load_global_config, save_global_config
from ..utils.console import (
    confirm,
    error,
    info,
    print_panel,
    success,
    warning,
)
from ..utils.privilege import with_sudo_fallback


@click.command()
@with_sudo_fallback
@click.option(
    "--data-dir",
    type=click.Path(),
    default=None,
    help="NA 实例的数据目录路径（默认当前激活的实例）",
)
@click.option(
    "--keep-data/--no-keep-data",
    default=False,
    help="是否保留数据目录（默认删除数据）",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="跳过确认直接执行",
)
def remove(data_dir: str | None, keep_data: bool, force: bool) -> None:
    """卸载并移除指定的 NA 实例。

    该命令会：
    1. 停止并删除 NA 服务容器
    2. 从 na-tools 管理列表中移除
    3. （可选）删除数据目录

    示例：
        na-tools remove                    # 移除当前激活的实例
        na-tools remove --data-dir /path/to/data  # 移除指定实例
        na-tools remove --keep-data       # 移除但保留数据目录
    """
    # 1. 确定目标目录
    data_dir_path = Path(data_dir or default_data_dir()).expanduser().resolve()
    str_path = str(data_dir_path)

    # 2. 验证目录存在
    if not data_dir_path.exists():
        error(f"数据目录不存在: {data_dir_path}")
        raise click.Abort()

    # 3. 验证是有效的 NA 安装目录
    if not compose_exists(data_dir_path):
        error(f"该目录不是有效的 NA 安装目录: {data_dir_path}")
        raise click.Abort()

    # 4. 检查是否在管理列表中
    config = load_global_config()
    installations = config.get("installations", {})

    if not isinstance(installations, dict):
        installations = {}

    is_managed = str_path in installations

    # 5. 显示即将执行的操作
    info("=== NA 实例移除预览 ===")
    info(f"数据目录: {data_dir_path}")

    if is_managed:
        info("管理状态: 已由 na-tools 管理")
    else:
        warning("管理状态: 未在 na-tools 管理列表中")

    info(f"保留数据: {'是' if keep_data else '否'}")

    # 6. 获取服务信息
    env_path = data_dir_path / ".env"
    if env_path.exists():
        env = load_env(env_path)
        instance_name = env.get("INSTANCE_NAME", "")
        if instance_name:
            info(f"实例名称前缀: {instance_name}")

    # 7. 确认操作
    if not force:
        warning("\n⚠️  此操作不可恢复！")
        if not confirm("确认移除该 NA 实例？", default=False):
            info("操作已取消")
            raise click.Abort()

    # 8. 停止并删除服务
    docker = DockerEnv()
    if docker.compose_installed:
        info("\n正在停止服务...")
        if docker.down(cwd=data_dir_path, env_file=env_path if env_path.exists() else None):
            success("服务已停止")
        else:
            warning("服务停止失败，可能已经停止")

        # 删除容器（使用 down -v 删除卷）
        info("正在删除容器...")
        try:
            _ = docker.compose(
                "down",
                "-v",  # 同时删除匿名卷
                cwd=data_dir_path,
                env_file=env_path if env_path.exists() else None,
            )
            success("容器已删除")
        except Exception as e:
            warning(f"容器删除时出现问题: {e}")
    else:
        warning("Docker Compose 不可用，跳过服务停止")

    # 9. 从管理列表移除
    if is_managed:
        info("\n正在从管理列表移除...")
        del installations[str_path]
        config["installations"] = installations

        # 如果移除的是当前激活的实例，清除 current_data_dir
        if config.get("current_data_dir") == str_path:
            config.pop("current_data_dir", None)

        save_global_config(config)
        success("已从管理列表移除")

    # 10. 删除数据目录
    if not keep_data:
        info("\n正在删除数据目录...")
        try:
            import shutil

            shutil.rmtree(data_dir_path)
            success(f"数据目录已删除: {data_dir_path}")
        except Exception as e:
            warning(f"数据目录删除失败: {e}")
            info("您可能需要手动删除: {data_dir_path}")
    else:
        info("\n数据目录已保留: {data_dir_path}")

    # 11. 显示结果
    result_lines = [
        "🎉 NA 实例移除完成!",
        "",
        f"数据目录: {data_dir_path}",
        f"保留数据: {'是' if keep_data else '否'}",
    ]

    print_panel(result_lines[0], "\n".join(result_lines[1:]), style="green")

    # 12. 提示其他实例
    remaining = len(installations)
    if remaining > 0:
        info(f"\n您还有 {remaining} 个 NA 实例在管理列表中")
        info("使用 'na-tools list' 查看所有实例")
