"""update 命令：更新 Nekro Agent 服务。"""

from pathlib import Path

import click

from ..core.platform import default_data_dir
from ..services.job_events import UpdateEvent
from ..services.update_service import (
    BackupRequest,
    RestoreRequest,
    UpdateRequest,
    UpdateService,
    UpdateServiceError,
    find_latest_named_backup,
)
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

    if preview:
        warning("即将切换到 preview 频道，这是预览版本，可能不稳定。")
        if not confirm("是否继续？", default=False):
            raise click.Abort()
        channel = "preview"
        should_backup = True
        restore_pre_preview = False

    elif rollback:
        info("正在从 preview 回退到稳定版...")
        pre_preview_backup = find_latest_named_backup(data_dir_path, "pre-preview")
        restore_pre_preview = False
        if pre_preview_backup:
            info(f"找到切换前备份: {pre_preview_backup.name}")
            restore_pre_preview = confirm(
                "是否从 pre-preview 备份还原数据？", default=True
            )
        channel = "rollback"
        should_backup = False

    else:
        channel = "stable"
        restore_pre_preview = False
        if should_backup is None:
            should_backup = confirm("是否在更新前备份数据？", default=True)

    request = UpdateRequest(
        data_dir=data_dir_path,
        channel=channel,
        backup=bool(should_backup),
        update_sandbox=update_sandbox,
        update_cc_sandbox=update_cc_sandbox,
        restore_pre_preview=restore_pre_preview,
    )
    service = UpdateService(
        backup_runner=_make_backup_runner(ctx),
        restore_runner=_make_restore_runner(ctx),
        restore_runner_restarts_service=True,
    )

    try:
        _ = service.run(request, _console_event_sink)
    except UpdateServiceError as exc:
        error(exc.message)
        if exc.code == "compose_missing":
            info("请先运行 `na-tools install` 安装。")
        raise click.Abort() from exc

    if preview:
        success("🎉 已切换到 preview 频道!")
        info("如需回退到稳定版，请运行: na-tools update --rollback")
    elif rollback:
        success("🎉 已回退到稳定版!")
    else:
        success("🎉 更新完成!")


def _console_event_sink(event: UpdateEvent) -> None:
    """Render service events into the existing CLI console style."""
    if event.type == "warning" and event.message:
        warning(event.message)
    elif event.type == "log" and event.message:
        if event.level == "warning":
            warning(event.message)
        else:
            info(event.message)


def _make_backup_runner(ctx: click.Context):
    def _backup_runner(request: BackupRequest) -> Path | None:
        from .backup import backup as backup_cmd

        ctx.invoke(
            backup_cmd,
            data_dir=str(request.data_dir),
            no_restart=request.no_restart,
            name=request.name,
        )
        if request.name:
            return find_latest_named_backup(request.data_dir, request.name)
        return _find_latest_backup(request.data_dir)

    return _backup_runner


def _make_restore_runner(ctx: click.Context):
    def _restore_runner(request: RestoreRequest) -> None:
        ctx.invoke(
            _get_restore_cmd(),
            backup_file=str(request.backup_file),
            data_dir=str(request.data_dir),
        )

    return _restore_runner


def _find_latest_backup(data_dir: Path) -> Path | None:
    from ..core.platform import get_global_config_dir

    backup_dir = get_global_config_dir() / "backup" / data_dir.name
    if not backup_dir.exists():
        return None
    backups = sorted(
        backup_dir.glob("*.tar.gz"),
        key=lambda backup_file: backup_file.stat().st_mtime,
        reverse=True,
    )
    return backups[0] if backups else None


def _get_restore_cmd() -> click.Command:
    """延迟导入 restore 命令，避免循环依赖。"""
    from .restore import restore
    return restore
