"""start/stop commands for Docker Compose orchestration."""

from pathlib import Path

import click

from ..core.platform import default_data_dir
from ..services.orchestration_service import (
    OrchestrationAction,
    OrchestrationRequest,
    OrchestrationService,
    OrchestrationServiceError,
)
from ..utils.console import error, info, success
from ..utils.privilege import with_sudo_fallback


@click.command()
@with_sudo_fallback
@click.option("--data-dir", type=click.Path(), default=None, help="数据目录路径")
@click.option(
    "--with-daemon/--without-daemon",
    default=True,
    help="是否同步启动已注册的 root daemon 服务",
)
def start(data_dir: str | None, with_daemon: bool) -> None:
    """启动 Nekro Agent 编排服务。"""

    _run_orchestration("start", data_dir, with_daemon)


@click.command()
@with_sudo_fallback
@click.option("--data-dir", type=click.Path(), default=None, help="数据目录路径")
@click.option(
    "--with-daemon/--without-daemon",
    default=True,
    help="是否同步停止已注册的 root daemon 服务",
)
def stop(data_dir: str | None, with_daemon: bool) -> None:
    """关闭 Nekro Agent 编排服务。"""

    _run_orchestration("stop", data_dir, with_daemon)


def _run_orchestration(
    action: OrchestrationAction,
    data_dir: str | None,
    with_daemon: bool,
) -> None:
    data_dir_path = Path(data_dir or default_data_dir()).expanduser().resolve()
    info(f"数据目录: {data_dir_path}")

    service = OrchestrationService()
    try:
        result = service.run(
            OrchestrationRequest(
                data_dir=data_dir_path,
                action=action,
                with_daemon=with_daemon,
            )
        )
    except OrchestrationServiceError as exc:
        error(exc.message)
        if exc.code == "compose_missing":
            info("请先运行 `na-tools install` 安装，或使用 `na-tools bind` 绑定已有实例。")
        elif exc.code == "docker_unavailable":
            info("请确认 Docker 与 Docker Compose 已安装并可用。")
        elif exc.code == "daemon_service_missing":
            info("请先运行 `na-tools install` 注册 daemon，或使用 `--without-daemon` 跳过。")
        raise click.Abort() from exc

    if result.action == "start":
        success("编排服务已启动。")
        if result.daemon_service is not None:
            success(f"daemon 服务已启动: {result.daemon_service.service_name}")
        info("查看状态: na-tools status")
    else:
        if result.daemon_service is not None:
            success(f"daemon 服务已停止: {result.daemon_service.service_name}")
        success("编排服务已关闭。")
        info("重新启动: na-tools start")
