"""status 命令：查看服务状态。"""

import click

from ..core.compose import compose_exists
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir
from ..utils.console import console, error, info


@click.command()
@click.option("--data-dir", type=click.Path(), default=None, help="数据目录路径")
def status(data_dir: str | None) -> None:
    """查看 Nekro Agent 服务状态。"""
    from pathlib import Path

    data_dir_path = Path(data_dir or default_data_dir()).expanduser().resolve()

    if not compose_exists(data_dir_path):
        error(f"未找到已有安装。数据目录: {data_dir_path}")
        return

    docker = DockerEnv()
    if not docker.compose_installed:
        error("Docker Compose 不可用。")
        return

    env_path = data_dir_path / ".env"
    output = docker.ps(
        cwd=data_dir_path, env_file=env_path if env_path.exists() else None
    )

    if output:
        info(f"数据目录: {data_dir_path}\n")
        console.print(output)
    else:
        info("没有运行中的服务。")
