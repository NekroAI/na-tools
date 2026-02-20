"""restore 命令：从备份恢复 Nekro Agent 数据。"""

import tarfile
from pathlib import Path

import click

from ..core.docker import DockerEnv
from ..core.platform import default_data_dir
from ..utils.privilege import with_sudo_fallback
from ..utils.console import confirm, error, info, success, warning


@click.command()
@with_sudo_fallback
@click.argument("backup_file", type=click.Path(exists=True), required=False)
@click.option("--data-dir", type=click.Path(), default=None, help="恢复目标数据目录")
def restore(backup_file: str | None, data_dir: str | None) -> None:
    """从备份文件恢复 Nekro Agent 数据。"""
    data_dir_path = Path(data_dir or default_data_dir()).expanduser().resolve()

    if not backup_file:
        from datetime import datetime

        backup_dir = Path("~/.config/na-tools/backup").expanduser() / data_dir_path.name

        backups = []
        if backup_dir.exists():
            backups = sorted(
                list(backup_dir.glob("*.tar.gz")),
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            )

        if not backups:
            ctx = click.get_current_context()
            error(
                "缺少参数 'BACKUP_FILE'。必须提供备份文件路径，且默认备份目录中未找到任何备份。"
            )
            info("示例: na-tools restore ./na_backup_20240101.tar.gz\n")
            click.echo(ctx.get_help())
            ctx.exit(1)

        info("发现以下历史备份：")
        for i, b in enumerate(backups, 1):
            mtime = datetime.fromtimestamp(b.stat().st_mtime).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            click.echo(
                f"  [{i}] {b.name} (备份时间: {mtime}, 大小: {b.stat().st_size / 1024 / 1024:.1f} MB)"
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
                    # 此时 docker-compose.yml 和 .env 应该已经恢复到了 data_dir_path
                    # 我们尝试获取最新的配置来解析卷名
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

                        config = docker.get_compose_config(
                            cwd=data_dir_path,
                            env_file=env_path if env_path.exists() else None,
                        )

                        if (
                            config
                            and "services" in config
                            and isinstance(config["services"], dict)
                        ):
                            from typing import cast

                            services = cast(
                                dict[str, dict[str, object]], config["services"]
                            )

                            # 映射关系: 备份文件名 -> 内部挂载点 -> 服务名
                            restore_map = {
                                "postgres.tar.gz": (
                                    "/var/lib/postgresql/data",
                                    "nekro_postgres",
                                ),
                                "qdrant.tar.gz": ("/qdrant/storage", "nekro_qdrant"),
                            }

                            for vol_file in volumes_backup_dir.iterdir():
                                if vol_file.name in restore_map:
                                    internal_path, svc_name = restore_map[vol_file.name]

                                    if svc_name in services:
                                        # 尝试解析卷名
                                        target_volume = docker.get_service_volume(
                                            cwd=data_dir_path,
                                            service=svc_name,
                                            target=internal_path,
                                            env_file=env_path
                                            if env_path.exists()
                                            else None,
                                        )

                                        # 如果解析失败，尝试从配置读取 (fallback)
                                        if not target_volume:
                                            svc_config = services[svc_name]
                                            volumes_config = svc_config.get(
                                                "volumes", []
                                            )
                                            if isinstance(volumes_config, list):
                                                volumes = cast(
                                                    list[dict[str, str]], volumes_config
                                                )
                                                for vol in volumes:
                                                    if not vol:
                                                        continue
                                                    if (
                                                        vol.get("type") == "volume"
                                                        and vol.get("target")
                                                        == internal_path
                                                    ):
                                                        target_volume = vol.get(
                                                            "source"
                                                        )
                                                        break

                                        if target_volume:
                                            info(
                                                f"正在恢复存储卷 {target_volume} ({vol_file.name})..."
                                            )

                                            # 使用 alpine 恢复
                                            # 注意：volumes_backup_dir 是在 tmp_dir 下的，我们需要把这个文件挂载进去
                                            # 或者我们可以直接把这个文件读入？不，挂载最简单

                                            success_restore = docker.run_ephemeral(
                                                image="alpine:latest",
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
    except PermissionError as e:
        import os
        import sys

        error(f"由于权限不足导致恢复中断: {e}")
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0:
            warning("检测到权限问题，将尝试获取管理员权限(root)以完成恢复。")
            info("请在下方输入您的当前用户密码：")
            # 使用 sudo -E 重新执行，-E 参数用于保留当前的所有环境变量（对虚拟环境/uv很重要）
            # 执行 sys.executable 以使用当前的 Python 解释器
            os.execvp("sudo", ["sudo", "-E", sys.executable] + sys.argv)
        else:
            raise click.Abort()
    except Exception as e:
        import os
        import sys

        if (
            "Permission denied" in str(e)
            and os.name != "nt"
            and hasattr(os, "geteuid")
            and os.geteuid() != 0
        ):
            error(f"由于权限不足导致恢复中断: {e}")
            warning("检测到权限问题，将尝试获取管理员权限(root)以完成恢复。")
            info("请在下方输入您的当前用户密码：")
            os.execvp("sudo", ["sudo", "-E", sys.executable] + sys.argv)

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
