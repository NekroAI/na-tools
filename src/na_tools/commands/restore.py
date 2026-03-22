"""restore 命令：从备份恢复 Nekro Agent 数据。"""

import tarfile
from pathlib import Path

import click

from ..core.compose import resolve_service_volumes
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir, get_global_config_dir, resolve_mirror
from ..utils.privilege import with_sudo_fallback
from ..utils.console import confirm, console, error, info, success, warning
from .backup import parse_backup_name


@click.command()
@with_sudo_fallback
@click.argument("backup_file", type=click.Path(exists=True), required=False)
@click.option("--data-dir", type=click.Path(), default=None, help="恢复目标数据目录")
def restore(backup_file: str | None, data_dir: str | None) -> None:
    """从备份文件恢复 Nekro Agent 数据。"""
    data_dir_path = Path(data_dir or default_data_dir()).expanduser().resolve()

    if not backup_file:
        from datetime import datetime

        backup_dir = get_global_config_dir() / "backup" / data_dir_path.name

        backups = []
        if backup_dir.exists():
            backups = sorted(
                list(backup_dir.glob("*.tar.gz")),
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            )

        if not backups:
            ctx = click.get_current_context(silent=True)
            error(
                "缺少参数 'BACKUP_FILE'。必须提供备份文件路径，且默认备份目录中未找到任何备份。"
            )
            info("示例: na-tools restore ./na_backup_20240101.tar.gz\n")
            if ctx:
                click.echo(ctx.get_help())
                ctx.exit(1)
            raise click.Abort()

        info("发现以下历史备份：")
        for i, b in enumerate(backups, 1):
            mtime = datetime.fromtimestamp(b.stat().st_mtime).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            bk_name = parse_backup_name(b.name)
            name_str = f", 名称: {bk_name}" if bk_name else ""
            console.print(
                f"  [{i}] {b.name} (备份时间: {mtime}{name_str}, 大小: {b.stat().st_size / 1024 / 1024:.1f} MB)"
            )

        import typing

        choice_val = typing.cast(
            int, click.prompt("\n请选择要恢复的备份序号", type=int)
        )
        if choice_val < 1 or choice_val > len(backups):
            error("无效的选择。")
            raise click.Abort()

        backup_path: Path = backups[choice_val - 1]
    else:
        backup_path = Path(backup_file).expanduser().resolve()

    if not tarfile.is_tarfile(backup_path):
        error(f"不是有效的备份文件: {backup_path}")
        raise click.Abort()

    docker = DockerEnv()
    env_path = data_dir_path / ".env"

    # 停止已有服务
    if (data_dir_path / "docker-compose.yml").exists() and docker.compose_installed:
        info("正在停止现有服务...")
        _ = docker.down(
            cwd=data_dir_path, env_file=env_path if env_path.exists() else None
        )

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
                import sys

                if sys.version_info >= (3, 12):
                    tar.extractall(tmp_dir, filter="data")
                else:
                    tar.extractall(tmp_dir)
                extracted_dir = Path(tmp_dir) / top_dir

                # 检查是否存在 volumes 备份
                # volumes 目录在归档根目录下，与 top_dir 平级
                volumes_backup_dir = Path(tmp_dir) / "volumes"
                has_volumes = (
                    volumes_backup_dir.exists() and volumes_backup_dir.is_dir()
                )

                if extracted_dir.exists():
                    # 确保目标目录存在
                    data_dir_path.mkdir(parents=True, exist_ok=True)
                    # 复制内容 (跳过 volumes 目录，因为它不需要复制到 data_dir，而是恢复到 docker volume)
                    for item in extracted_dir.iterdir():
                        if item.name == "volumes":
                            continue

                        dest = data_dir_path / item.name
                        if dest.exists():
                            if dest.is_dir():
                                shutil.rmtree(dest)
                            else:
                                dest.unlink()
                        _ = shutil.move(str(item), str(dest))

                # 恢复存储卷
                if has_volumes:
                    info("发现存储卷备份，正在恢复...")
                    if (
                        data_dir_path / "docker-compose.yml"
                    ).exists() and docker.compose_installed:
                        # 确保容器存在（但不启动），以便解析卷名
                        info("正在初始化服务容器...")
                        _ = docker.compose(
                            "up",
                            "--no-start",
                            cwd=data_dir_path,
                            env_file=env_path if env_path.exists() else None,
                            check=False,
                        )

                        env_file = env_path if env_path.exists() else None
                        # 解析镜像源
                        mirror = resolve_mirror(env_path if env_path.exists() else None)
                        alpine_image = f"{mirror}/alpine:latest" if mirror else "alpine:latest"
                        # 建立 备份文件名 -> 卷名 的映射
                        volume_map = {
                            filename: vol_name
                            for vol_name, filename in resolve_service_volumes(
                                docker, data_dir_path, env_file
                            )
                        }

                        for vol_file in volumes_backup_dir.iterdir():
                            target_volume = volume_map.get(vol_file.name)
                            if target_volume:
                                info(
                                    f"正在恢复存储卷 {target_volume} ({vol_file.name})..."
                                )

                                success_restore = docker.run_ephemeral(
                                    image=alpine_image,
                                    cmd=[
                                        "tar",
                                        "xzf",
                                        f"/backup/{vol_file.name}",
                                        "-C",
                                        "/data",
                                    ],
                                    volumes={
                                        target_volume: "/data",
                                        str(volumes_backup_dir): "/backup",
                                    },
                                )

                                if success_restore:
                                    success(f"卷恢复完成: {target_volume}")
                                else:
                                    error(f"卷恢复失败: {target_volume}")

        success("备份恢复完成!")
    except Exception as e:
        if isinstance(e, (click.Abort, PermissionError)):
            raise
        if "Permission denied" in str(e):
            raise
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
