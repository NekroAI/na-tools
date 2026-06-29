"""backup 命令：备份 Nekro Agent 数据。"""

from datetime import datetime
from pathlib import Path

import click

from ..core.platform import default_data_dir
from ..services.backup_service import (
    BackupRequest,
    BackupService,
    BackupServiceError,
    parse_backup_name,
)
from ..services.common import ServiceEvent
from ..utils.console import console, error, info, success, warning
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


@click.group(invoke_without_command=True)
@click.pass_context
@with_sudo_fallback
@click.option("--data-dir", type=click.Path(), default=None, help="数据目录路径")
@click.option(
    "--output", "-o", type=click.Path(), default=None, help="备份文件输出路径"
)
@click.option("--no-restart", is_flag=True, default=False, help="备份后不重启服务")
@click.option("--name", default=None, help="备份名称标识（例如 pre-preview）")
def backup(
    ctx: click.Context,
    data_dir: str | None,
    output: str | None,
    no_restart: bool,
    name: str | None,
) -> None:
    """备份 Nekro Agent 数据和配置。"""
    _ = ctx.ensure_object(dict)
    ctx.obj["data_dir"] = data_dir

    if ctx.invoked_subcommand is not None:
        return

    try:
        result = BackupService().run(
            BackupRequest(
                data_dir=Path(data_dir).expanduser().resolve() if data_dir else None,
                output=Path(output).expanduser().resolve() if output else None,
                no_restart=no_restart,
                name=name,
            ),
            _render_event,
        )
    except BackupServiceError as exc:
        error(exc.message)
        raise click.Abort() from exc

    success(
        f"备份完成: {result.backup_path} "
        f"({result.size_bytes / 1024 / 1024:.1f} MB)"
    )
    if result.skipped_cache:
        info(f"已跳过 {result.skipped_cache} 个缓存/临时文件。")
    success("🎉 备份完成!")


@backup.command("list")
@click.option("--name", "filter_name", default=None, help="只显示指定名称的备份")
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=None,
    help="最多显示的备份数量",
)
@click.pass_context
def list_backups(
    ctx: click.Context,
    filter_name: str | None,
    limit: int | None,
) -> None:
    """列出可用的备份文件。"""
    obj = ctx.ensure_object(dict)
    data_dir: str | None = obj.get("data_dir")
    data_dir_path = Path(data_dir or default_data_dir()).expanduser().resolve()
    backups = BackupService().list_backups(
        data_dir_path,
        name=filter_name,
        limit=limit,
    )

    if not backups:
        if filter_name:
            info(f"没有找到名称为 {filter_name} 的历史备份。")
            return
        info("备份目录不存在或为空。")
        return

    filters: list[str] = []
    if filter_name:
        filters.append(f"名称: {filter_name}")
    if limit is not None:
        filters.append(f"最多 {limit} 个")
    filter_text = f"（{', '.join(filters)}）" if filters else ""

    info(f"发现以下历史备份{filter_text}：")
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
