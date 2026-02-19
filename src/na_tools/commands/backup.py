"""backup 命令：备份 Nekro Agent 数据。"""

import tarfile
from datetime import datetime
from pathlib import Path

import click

from ..core.compose import compose_exists
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir
from ..utils.console import confirm, error, info, success, warning


@click.command()
@click.option("--data-dir", type=click.Path(), default=None, help="数据目录路径")
@click.option(
    "--output", "-o", type=click.Path(), default=None, help="备份文件输出路径"
)
@click.option("--no-restart", is_flag=True, default=False, help="备份后不重启服务")
def backup(data_dir: str | None, output: str | None, no_restart: bool) -> None:
    """备份 Nekro Agent 数据和配置。"""
    data_dir_path = Path(data_dir or default_data_dir()).expanduser().resolve()

    if not data_dir_path.exists():
        error(f"数据目录不存在: {data_dir_path}")
        raise click.Abort()

    docker = DockerEnv()
    env_path = data_dir_path / ".env"

    # 生成备份文件名
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if output:
        backup_path = Path(output)
    else:
        backup_path = data_dir_path.parent / f"nekro_agent_backup_{timestamp}.tar.gz"

    backup_path.parent.mkdir(parents=True, exist_ok=True)

    # 停止服务
    should_restart = False
    if compose_exists(data_dir_path) and docker.compose_installed:
        info("正在停止服务以确保数据一致性...")
        docker.down(cwd=data_dir_path, env_file=env_path if env_path.exists() else None)
        should_restart = True

    # 打包数据
    info(f"正在备份数据到: {backup_path}")
    try:
        with tarfile.open(backup_path, "w:gz") as tar:
            tar.add(data_dir_path, arcname=data_dir_path.name)
        success(
            f"备份完成: {backup_path} ({backup_path.stat().st_size / 1024 / 1024:.1f} MB)"
        )
    except Exception as e:
        error(f"备份失败: {e}")
        # 即使备份失败也要尝试重启
        if should_restart and not no_restart:
            info("正在重新启动服务...")
            docker.up(
                cwd=data_dir_path, env_file=env_path if env_path.exists() else None
            )
        raise click.Abort()

    # 重启服务
    if should_restart and not no_restart:
        info("正在重新启动服务...")
        if docker.up(
            cwd=data_dir_path, env_file=env_path if env_path.exists() else None
        ):
            success("服务已重新启动。")
        else:
            warning("服务重启失败，请手动启动。")

    success("🎉 备份完成!")
