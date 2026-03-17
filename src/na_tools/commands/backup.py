"""backup 命令：备份 Nekro Agent 数据。"""

import tarfile
from datetime import datetime
from pathlib import Path

import click

from ..core.compose import compose_exists, resolve_service_volumes
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir, get_global_config_dir, resolve_mirror
from ..utils.privilege import with_sudo_fallback
from ..utils.console import console, error, info, success, warning


@click.group(invoke_without_command=True)
@click.pass_context
@with_sudo_fallback
@click.option("--data-dir", type=click.Path(), default=None, help="数据目录路径")
@click.option(
    "--output", "-o", type=click.Path(), default=None, help="备份文件输出路径"
)
@click.option("--no-restart", is_flag=True, default=False, help="备份后不重启服务")
def backup(
    ctx: click.Context, data_dir: str | None, output: str | None, no_restart: bool
) -> None:
    """备份 Nekro Agent 数据和配置。"""
    _ = ctx.ensure_object(dict)
    ctx.obj["data_dir"] = data_dir

    if ctx.invoked_subcommand is not None:
        return

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
        backup_dir = get_global_config_dir() / "backup" / data_dir_path.name
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"{data_dir_path.name}_backup_{timestamp}.tar.gz"

    backup_path.parent.mkdir(parents=True, exist_ok=True)

    # 准备备份存储卷（在停止服务前解析卷名）
    volume_backups_map: list[
        tuple[str, str, Path]
    ] = []  # (volume_name, backup_filename, backup_path)
    volumes_dir = data_dir_path / "volumes_backup_tmp"

    if compose_exists(data_dir_path) and docker.compose_installed:
        env_file = env_path if env_path.exists() else None
        for vol_name, filename in resolve_service_volumes(
            docker, data_dir_path, env_file
        ):
            volume_backups_map.append((vol_name, filename, volumes_dir / filename))

    # 停止服务
    should_restart = False
    if compose_exists(data_dir_path) and docker.compose_installed:
        info("正在停止服务以确保数据一致性...")
        if not docker.down(
            cwd=data_dir_path, env_file=env_path if env_path.exists() else None
        ):
            error("服务停止失败，为避免备份数据不一致，已中止备份。")
            raise click.Abort()
        should_restart = True

    # 执行卷备份
    volume_backups: list[Path] = []
    if volume_backups_map:
        mirror = resolve_mirror(env_path if env_path.exists() else None)
        alpine_image = f"{mirror}/alpine:latest" if mirror else "alpine:latest"
        volumes_dir.mkdir(exist_ok=True)
        for vol_name, filename, backup_file in volume_backups_map:
            info(f"正在备份存储卷 {vol_name}...")

            # 使用 alpine 打包
            success_backup = docker.run_ephemeral(
                image=alpine_image,
                cmd=["tar", "czf", f"/backup/{filename}", "-C", "/data", "."],
                volumes={vol_name: "/data", str(volumes_dir): "/backup"},
            )

            if success_backup:
                volume_backups.append(backup_file)
                success(f"卷备份完成: {filename}")
            else:
                error(f"卷备份失败: {vol_name}")

    # 打包数据
    info(f"正在备份数据到: {backup_path}")
    try:
        with tarfile.open(backup_path, "w:gz") as tar:
            # 添加主数据目录
            tar.add(
                data_dir_path,
                arcname=data_dir_path.name,
                filter=lambda x: None if "volumes_backup_tmp" in x.name else x,
            )

            # 添加卷备份
            if volume_backups:
                # 在 tar 中创建一个 volumes 目录
                for vb in volume_backups:
                    tar.add(vb, arcname=f"volumes/{vb.name}")

        success(
            f"备份完成: {backup_path} ({backup_path.stat().st_size / 1024 / 1024:.1f} MB)"
        )
    except Exception as e:
        error(f"备份失败: {e}")
        # 即使备份失败也要尝试重启
        if should_restart and not no_restart:
            info("正在重新启动服务...")
            _ = docker.up(
                cwd=data_dir_path, env_file=env_path if env_path.exists() else None
            )
        raise click.Abort()
    finally:
        # 清理临时卷备份目录
        import shutil

        if volumes_dir.exists():
            shutil.rmtree(volumes_dir)

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


@backup.command("list")
@click.pass_context
def list_backups(ctx: click.Context) -> None:
    """列出可用的备份文件。"""
    obj = ctx.ensure_object(dict)
    data_dir: str | None = obj.get("data_dir")
    data_dir_path = Path(data_dir or default_data_dir()).expanduser().resolve()
    backup_dir = get_global_config_dir() / "backup" / data_dir_path.name

    if not backup_dir.exists():
        info("备份目录不存在或为空。")
        return

    backups = sorted(
        list(backup_dir.glob("*.tar.gz")),
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )

    if not backups:
        info("没有任何历史备份。")
        return

    info("发现以下历史备份：")
    for i, b in enumerate(backups, 1):
        mtime = datetime.fromtimestamp(b.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        console.print(
            f"  [{i}] {b.name} (备份时间: {mtime}, 大小: {b.stat().st_size / 1024 / 1024:.1f} MB)"
        )
