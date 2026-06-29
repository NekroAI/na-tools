"""remove 命令：移除（卸载）指定的 NA 实例。"""

from pathlib import Path

import click

from ..services.common import ServiceEvent
from ..services.remove_service import RemoveRequest, RemoveService, RemoveServiceError
from ..utils.console import confirm, error, info, print_panel, success, warning
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
@click.option(
    "--data-dir",
    type=click.Path(),
    default=None,
    help="NA 实例的数据目录路径（默认当前激活的实例）",
)
@click.option(
    "--keep-data/--no-keep-data",
    default=False,
    help="是否保留数据目录（默认删除数据）",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="跳过确认直接执行",
)
def remove(data_dir: str | None, keep_data: bool, force: bool) -> None:
    """卸载并移除指定的 NA 实例。"""
    service = RemoveService()
    target = Path(data_dir).expanduser().resolve() if data_dir else None
    try:
        preview = service.preview(target, keep_data=keep_data)
    except RemoveServiceError as exc:
        error(exc.message)
        raise click.Abort() from exc

    info("=== NA 实例移除预览 ===")
    info(f"数据目录: {preview.data_dir}")
    if preview.is_managed:
        info("管理状态: 已由 na-tools 管理")
    else:
        warning("管理状态: 未在 na-tools 管理列表中")
    info(f"保留数据: {'是' if keep_data else '否'}")
    if preview.instance_name:
        info(f"实例名称前缀: {preview.instance_name}")

    if not force:
        warning("\n⚠️  此操作不可恢复！")
        if not confirm("确认移除该 NA 实例？", default=False):
            info("操作已取消")
            raise click.Abort()

    try:
        result = service.run(
            RemoveRequest(data_dir=preview.data_dir, keep_data=keep_data),
            _render_event,
        )
    except RemoveServiceError as exc:
        error(exc.message)
        raise click.Abort() from exc

    result_lines = [
        "🎉 NA 实例移除完成!",
        "",
        f"数据目录: {result.data_dir}",
        f"保留数据: {'是' if result.keep_data else '否'}",
    ]
    if result.daemon_service is not None:
        result_lines.append(f"Daemon 服务已删除: {result.daemon_service.service_name}")
    print_panel(result_lines[0], "\n".join(result_lines[1:]), style="green")

    if result.remaining_installations > 0:
        info(f"\n您还有 {result.remaining_installations} 个 NA 实例在管理列表中")
        info("使用 'na-tools list' 查看所有实例")
