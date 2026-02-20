"""logs 命令：查看服务日志。"""

import click

from ..core.compose import compose_exists
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir
from ..utils.privilege import with_sudo_fallback
from ..utils.console import error


@click.command()
@with_sudo_fallback
@click.argument("service", default="nekro_agent")
@click.option("--data-dir", type=click.Path(), default=None, help="数据目录路径")
@click.option("--follow", "-f", is_flag=True, default=False, help="持续跟踪日志")
@click.option("--tail", "-n", type=int, default=100, help="显示最后 N 行")
def logs(service: str, data_dir: str | None, follow: bool, tail: int) -> None:
    """查看指定服务的日志。

    SERVICE: 服务名称，默认 nekro_agent。可选: nekro_postgres, nekro_qdrant, nekro_napcat
    """
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
    docker.logs(
        service,
        cwd=data_dir_path,
        follow=follow,
        tail=tail,
        env_file=env_path if env_path.exists() else None,
    )
