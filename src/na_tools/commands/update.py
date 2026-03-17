"""update 命令：更新 Nekro Agent 服务。"""

from pathlib import Path

import click

from ..core.compose import compose_exists
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir, resolve_mirror
from ..utils.privilege import with_sudo_fallback
from ..utils.console import confirm, error, info, success, warning


@click.command()
@click.pass_context
@with_sudo_fallback
@click.option("--data-dir", type=click.Path(), default=None, help="数据目录路径")
@click.option(
    "--update-sandbox/--no-update-sandbox", default=True, help="是否同时更新沙盒镜像"
)
@click.option(
    "--backup/--no-backup",
    "should_backup",
    default=None,
    help="更新前是否备份数据 (如果不指定则交互询问)",
)
def update(
    ctx: click.Context,
    data_dir: str | None,
    update_sandbox: bool,
    should_backup: bool | None,
) -> None:
    """更新 Nekro Agent 到最新版本。"""
    data_dir_path = Path(data_dir or default_data_dir()).expanduser().resolve()

    # 验证现有安装
    if not compose_exists(data_dir_path):
        error(f"未找到已有安装。数据目录: {data_dir_path}")
        info("请先运行 `na-tools install` 安装。")
        raise click.Abort()

    # 备份确认
    if should_backup is None:
        should_backup = confirm("是否在更新前备份数据？", default=True)

    if should_backup:
        from .backup import backup as backup_cmd

        info("正在执行更新前备份...")
        ctx.invoke(backup_cmd, data_dir=data_dir, no_restart=True)

    env_path = data_dir_path / ".env"
    if not env_path.exists():
        error(f"未找到 .env 文件: {env_path}")
        raise click.Abort()

    docker = DockerEnv()
    if not docker.docker_installed or not docker.compose_installed:
        error("Docker 环境不可用。")
        raise click.Abort()

    # 拉取最新镜像
    info("正在拉取最新镜像...")
    if not docker.pull(cwd=data_dir_path, env_file=env_path):
        error("镜像拉取失败。")
        raise click.Abort()

    # 重启服务
    info("正在重启服务...")
    if not docker.up(cwd=data_dir_path, env_file=env_path):
        error("服务重启失败。")
        raise click.Abort()

    # 更新沙盒镜像
    if update_sandbox:
        info("正在更新沙盒镜像...")
        mirror = resolve_mirror(env_path)
        if not docker.docker_pull("kromiose/nekro-agent-sandbox", mirror=mirror):
            warning("沙盒镜像更新失败，可稍后手动更新。")

    success("🎉 更新完成!")
