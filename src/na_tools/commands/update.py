"""update 命令：更新 Nekro Agent 服务。"""

from pathlib import Path

import click

from ..core.compose import compose_exists, set_image_tag
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir, get_global_config_dir, resolve_mirror
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
    "--update-cc-sandbox/--no-update-cc-sandbox",
    default=False,
    help="是否同时更新 CC 沙盒镜像",
)
@click.option(
    "--backup/--no-backup",
    "should_backup",
    default=None,
    help="更新前是否备份数据 (如果不指定则交互询问)",
)
@click.option("--preview", is_flag=True, default=False, help="切换到 preview 频道")
@click.option(
    "--rollback", is_flag=True, default=False, help="从 preview 回退到稳定版"
)
def update(
    ctx: click.Context,
    data_dir: str | None,
    update_sandbox: bool,
    update_cc_sandbox: bool,
    should_backup: bool | None,
    preview: bool,
    rollback: bool,
) -> None:
    """更新 Nekro Agent 到最新版本。"""
    if preview and rollback:
        error("不能同时指定 --preview 和 --rollback。")
        raise click.Abort()

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

    # --- preview 模式 ---
    if preview:
        _do_preview(ctx, data_dir, data_dir_path, env_path, docker, update_sandbox, update_cc_sandbox)
        return

    # --- rollback 模式 ---
    if rollback:
        _do_rollback(ctx, data_dir_path, env_path, docker)
        return

    # --- 普通更新流程 ---
    # 备份确认
    if should_backup is None:
        should_backup = confirm("是否在更新前备份数据？", default=True)

    if should_backup:
        from .backup import backup as backup_cmd

        info("正在执行更新前备份...")
        ctx.invoke(backup_cmd, data_dir=data_dir, no_restart=True)

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
    mirror = resolve_mirror(env_path)
    if update_sandbox:
        info("正在更新沙盒镜像...")
        if not docker.docker_pull("kromiose/nekro-agent-sandbox", mirror=mirror):
            warning("沙盒镜像更新失败，可稍后手动更新。")

    if update_cc_sandbox:
        info("正在更新 CC 沙盒镜像...")
        if not docker.docker_pull("kromiose/nekro-cc-sandbox", mirror=mirror):
            warning("CC 沙盒镜像更新失败，可稍后手动更新。")

    success("🎉 更新完成!")


def _do_preview(
    ctx: click.Context,
    data_dir: str | None,
    data_dir_path: Path,
    env_path: Path,
    docker: DockerEnv,
    update_sandbox: bool,
    update_cc_sandbox: bool,
) -> None:
    """执行 preview 频道切换。"""
    from .backup import backup as backup_cmd

    warning("即将切换到 preview 频道，这是预览版本，可能不稳定。")
    if not confirm("是否继续？", default=False):
        raise click.Abort()

    # 强制备份（名称 pre-preview）
    info("正在执行切换前自动备份（名称: pre-preview）...")
    ctx.invoke(backup_cmd, data_dir=data_dir, no_restart=True, name="pre-preview")

    # 修改镜像 tag
    info("正在切换到 preview 镜像...")
    if not set_image_tag(data_dir_path, "kromiose/nekro-agent", "preview"):
        error("无法修改镜像 tag，请检查 docker-compose.yml。")
        raise click.Abort()

    # 拉取并重启
    info("正在拉取 preview 镜像...")
    if not docker.pull(cwd=data_dir_path, env_file=env_path):
        error("镜像拉取失败。")
        raise click.Abort()

    info("正在重启服务...")
    if not docker.up(cwd=data_dir_path, env_file=env_path):
        error("服务重启失败。")
        raise click.Abort()

    # 更新沙盒镜像
    mirror = resolve_mirror(env_path)
    if update_sandbox:
        info("正在更新沙盒镜像...")
        if not docker.docker_pull("kromiose/nekro-agent-sandbox", mirror=mirror):
            warning("沙盒镜像更新失败，可稍后手动更新。")

    if update_cc_sandbox:
        info("正在更新 CC 沙盒镜像...")
        if not docker.docker_pull("kromiose/nekro-cc-sandbox", mirror=mirror):
            warning("CC 沙盒镜像更新失败，可稍后手动更新。")

    success("🎉 已切换到 preview 频道!")
    info("如需回退到稳定版，请运行: na-tools update --rollback")


def _do_rollback(
    ctx: click.Context,
    data_dir_path: Path,
    env_path: Path,
    docker: DockerEnv,
) -> None:
    """从 preview 回退到稳定版。"""
    from .backup import parse_backup_name

    info("正在从 preview 回退到稳定版...")

    # 切换镜像 tag 回 latest
    info("正在切换回 latest 镜像...")
    set_image_tag(data_dir_path, "kromiose/nekro-agent", "latest")

    # 查找最近的 pre-preview 备份
    backup_dir = get_global_config_dir() / "backup" / data_dir_path.name
    pre_preview_backup: Path | None = None
    if backup_dir.exists():
        backups = sorted(
            list(backup_dir.glob("*.tar.gz")),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        for b in backups:
            if parse_backup_name(b.name) == "pre-preview":
                pre_preview_backup = b
                break

    if pre_preview_backup:
        info(f"找到切换前备份: {pre_preview_backup.name}")
        if confirm("是否从 pre-preview 备份还原数据？", default=True):
            ctx.invoke(
                _get_restore_cmd(),
                backup_file=str(pre_preview_backup),
                data_dir=str(data_dir_path),
            )
            success("🎉 已回退到稳定版!")
            return

    # 没有 pre-preview 备份或用户不还原，只 pull + up
    info("正在拉取 latest 镜像...")
    if not docker.pull(cwd=data_dir_path, env_file=env_path):
        error("镜像拉取失败。")
        raise click.Abort()

    info("正在重启服务...")
    if not docker.up(cwd=data_dir_path, env_file=env_path):
        error("服务重启失败。")
        raise click.Abort()

    success("🎉 已回退到稳定版!")


def _get_restore_cmd() -> click.Command:
    """延迟导入 restore 命令，避免循环依赖。"""
    from .restore import restore
    return restore
