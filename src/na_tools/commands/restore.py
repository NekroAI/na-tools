"""restore 命令：从备份恢复 Nekro Agent 数据。"""

from datetime import datetime
from pathlib import Path

import click

from ..core.platform import default_data_dir
from ..services.backup_service import BackupService
from ..services.common import ServiceEvent
from ..services.restore_service import RestoreRequest, RestoreService, RestoreServiceError
from ..utils.console import confirm, console, error, info, success, warning
from ..utils.privilege import with_sudo_fallback


def _render_event(event: ServiceEvent) -> None:
    if event.level == "success":
        success(event.message)
    elif event.level == "warning":
        warning(event.message)
    elif event.level == "error":
        error(event.message)
    else:
        info(event.message)


@click.command()
@with_sudo_fallback
@click.argument("backup_file", type=click.Path(exists=True), required=False)
@click.option("--data-dir", type=click.Path(), default=None, help="恢复目标数据目录")
def restore(backup_file: str | None, data_dir: str | None) -> None:
    """从备份文件恢复 Nekro Agent 数据。"""
    data_dir_path = Path(data_dir or default_data_dir()).expanduser().resolve()
    backup_path = _select_backup_file(backup_file, data_dir_path)

    if data_dir_path.exists() and any(data_dir_path.iterdir()):
        warning(f"目标目录非空: {data_dir_path}")
        if not confirm("是否覆盖现有数据?"):
            raise click.Abort()

    try:
        _ = RestoreService().run(
            RestoreRequest(
                backup_file=backup_path,
                data_dir=data_dir_path,
                start_service=None,
                choose_start_service=lambda: confirm("是否启动服务?", default=True),
            ),
            _render_event,
        )
    except RestoreServiceError as exc:
        error(exc.message)
        raise click.Abort() from exc

    success("🎉 恢复完成!")


def _select_backup_file(backup_file: str | None, data_dir_path: Path) -> Path:
    if backup_file:
        return Path(backup_file).expanduser().resolve()

    backups = BackupService().list_backups(data_dir_path)
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
    for i, backup_summary in enumerate(backups, 1):
        mtime = datetime.fromtimestamp(
            backup_summary.path.stat().st_mtime
        ).strftime("%Y-%m-%d %H:%M:%S")
        name_str = f", 名称: {backup_summary.name}" if backup_summary.name else ""
        console.print(
            f"  [{i}] {backup_summary.path.name} "
            f"(备份时间: {mtime}{name_str}, "
            f"大小: {backup_summary.size_bytes / 1024 / 1024:.1f} MB)"
        )

    choice_val = int(click.prompt("\n请选择要恢复的备份序号", type=int))
    if choice_val < 1 or choice_val > len(backups):
        error("无效的选择。")
        raise click.Abort()
    return backups[choice_val - 1].path
