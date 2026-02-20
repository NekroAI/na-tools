"""update 命令：更新 Nekro Agent 服务。"""

import click

from ..core.compose import compose_exists
from ..core.config import load_env
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir
from ..utils.privilege import with_sudo_fallback
from ..utils.console import error, info, success, warning


@click.command()
@with_sudo_fallback
@click.option("--data-dir", type=click.Path(), default=None, help="数据目录路径")
@click.option(
    "--update-sandbox/--no-update-sandbox", default=True, help="是否同时更新沙盒镜像"
)
def update(data_dir: str | None, update_sandbox: bool) -> None:
    """更新 Nekro Agent 到最新版本。"""
    from pathlib import Path

    data_dir_path = Path(data_dir or default_data_dir()).expanduser().resolve()

    # 验证现有安装
    if not compose_exists(data_dir_path):
        error(f"未找到已有安装。数据目录: {data_dir_path}")
        info("请先运行 `na-tools install` 安装。")
        raise click.Abort()

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
        env_dict = load_env(env_path)
        mirror = env_dict.get("MIRROR_REGISTRY", "")
        if not docker.docker_pull("kromiose/nekro-agent-sandbox", mirror=mirror):
            warning("沙盒镜像更新失败，可稍后手动更新。")

    success("🎉 更新完成!")
