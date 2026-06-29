"""install 命令：全新安装 Nekro Agent。"""

from pathlib import Path

import click

from ..core.platform import default_data_dir
from ..services.common import ServiceEvent
from ..services.install_service import InstallRequest, InstallService, InstallServiceError
from ..utils.console import confirm, error, info, print_panel, prompt, warning
from ..utils.privilege import with_sudo_fallback


def _render_event(event: ServiceEvent) -> None:
    if event.level == "warning":
        warning(event.message)
    elif event.level == "error":
        error(event.message)
    else:
        info(event.message)


@click.command()
@with_sudo_fallback
@click.option("--data-dir", type=click.Path(), default=None, help="数据目录路径")
@click.option("--with-napcat/--without-napcat", default=None, help="是否含 NapCat 服务")
@click.option("--port", type=int, default=None, help="服务暴露端口")
@click.option(
    "--non-interactive", is_flag=True, default=False, help="非交互模式，使用默认值"
)
@click.option("--preview", is_flag=True, default=False, help="使用 preview 频道镜像")
@click.option(
    "--start-daemon/--no-start-daemon",
    default=True,
    help="是否注册并启动 root daemon 服务",
)
@click.option(
    "--with-cc-sandbox/--without-cc-sandbox",
    default=None,
    help="是否拉取 CC 沙盒镜像",
)
def install(
    data_dir: str | None,
    with_napcat: bool | None,
    port: int | None,
    non_interactive: bool,
    preview: bool,
    start_daemon: bool,
    with_cc_sandbox: bool | None,
) -> None:
    """安装 Nekro Agent 服务。"""
    interactive = not non_interactive

    info("=== Nekro Agent 安装向导 ===")

    default_dir = str(default_data_dir())
    if data_dir is None and interactive:
        data_dir = prompt("请设置数据目录", default=default_dir)
    data_dir_path = Path(data_dir or default_dir).expanduser().resolve()

    if with_napcat is None and interactive:
        with_napcat = confirm("是否同时使用 NapCat 服务?", default=True)
    elif with_napcat is None:
        with_napcat = False

    if with_cc_sandbox is None and not interactive:
        with_cc_sandbox = False

    try:
        result = InstallService().run(
            InstallRequest(
                data_dir=data_dir_path,
                with_napcat=with_napcat,
                port=port,
                interactive_env=interactive,
                preview=preview,
                start_daemon=start_daemon,
                with_cc_sandbox=with_cc_sandbox,
                continue_after_env=(
                    (lambda: confirm("配置已生成，是否继续安装?", default=True))
                    if interactive
                    else None
                ),
                choose_cc_sandbox=(
                    (lambda: confirm("是否拉取 CC 沙盒镜像 (nekro-cc-sandbox)?", default=False))
                    if interactive and with_cc_sandbox is None
                    else None
                ),
            ),
            _render_event,
        )
    except InstallServiceError as exc:
        if exc.code == "install_cancelled":
            info(exc.message)
        else:
            error(exc.message)
        raise click.Abort() from exc

    result_lines = [
        f"数据目录: {result.data_dir}",
        f"频道: {result.channel}",
        f"服务端口: {result.expose_port}",
        f"Web 访问: http://127.0.0.1:{result.expose_port}",
        "",
        "管理员账号: admin",
        f"管理员密码: {result.admin_password}",
        f"OneBot Token: {result.onebot_token}",
    ]

    if result.with_napcat and result.napcat_port:
        result_lines.append(f"NapCat 端口: {result.napcat_port}")

    if result.daemon_service is not None:
        result_lines.extend(
            [
                "",
                f"Daemon 服务: {result.daemon_service.service_name}",
                f"Daemon 服务文件: {result.daemon_service.service_path}",
            ]
        )

    result_lines.extend(
        [
            "",
            "查看日志: na-tools logs nekro_agent",
            "查看状态: na-tools status",
        ]
    )

    print_panel("🎉 部署完成!", "\n".join(result_lines), style="green")
