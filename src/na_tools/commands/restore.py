"""restore 命令：从备份恢复 Nekro Agent 数据。"""

import tarfile
from pathlib import Path

import click

from ..core.docker import DockerEnv
from ..core.platform import default_data_dir
from ..utils.console import confirm, error, info, success, warning


@click.command()
@click.argument("backup_file", type=click.Path(exists=True))
@click.option("--data-dir", type=click.Path(), default=None, help="恢复目标数据目录")
def restore(backup_file: str, data_dir: str | None) -> None:
    """从备份文件恢复 Nekro Agent 数据。"""
    backup_path = Path(backup_file).expanduser().resolve()

    if not tarfile.is_tarfile(backup_path):
        error(f"不是有效的备份文件: {backup_path}")
        raise click.Abort()

    data_dir_path = Path(data_dir or default_data_dir()).expanduser().resolve()

    docker = DockerEnv()
    env_path = data_dir_path / ".env"

    # 停止已有服务
    if (data_dir_path / "docker-compose.yml").exists() and docker.compose_installed:
        info("正在停止现有服务...")
        docker.down(cwd=data_dir_path, env_file=env_path if env_path.exists() else None)

    # 确认覆盖
    if data_dir_path.exists() and any(data_dir_path.iterdir()):
        warning(f"目标目录非空: {data_dir_path}")
        if not confirm("是否覆盖现有数据?"):
            raise click.Abort()

    # 解压备份
    info(f"正在恢复备份到: {data_dir_path}")
    try:
        with tarfile.open(backup_path, "r:gz") as tar:
            # 获取归档中的顶层目录名
            members = tar.getmembers()
            if not members:
                error("备份文件为空。")
                raise click.Abort()

            top_dir = members[0].name.split("/")[0]

            # 解压到临时位置然后移动
            import tempfile
            import shutil

            with tempfile.TemporaryDirectory() as tmp_dir:
                tar.extractall(tmp_dir)
                extracted_dir = Path(tmp_dir) / top_dir

                if extracted_dir.exists():
                    # 确保目标目录存在
                    data_dir_path.mkdir(parents=True, exist_ok=True)
                    # 复制内容
                    for item in extracted_dir.iterdir():
                        dest = data_dir_path / item.name
                        if dest.exists():
                            if dest.is_dir():
                                shutil.rmtree(dest)
                            else:
                                dest.unlink()
                        shutil.move(str(item), str(dest))

        success("备份恢复完成!")
    except Exception as e:
        error(f"恢复失败: {e}")
        raise click.Abort()

    # 重新启动服务
    env_path = data_dir_path / ".env"
    if (data_dir_path / "docker-compose.yml").exists() and docker.compose_installed:
        if confirm("是否启动服务?", default=True):
            info("正在启动服务...")
            if docker.up(
                cwd=data_dir_path, env_file=env_path if env_path.exists() else None
            ):
                success("服务已启动。")
            else:
                warning("服务启动失败，请手动启动。")

    success("🎉 恢复完成!")
