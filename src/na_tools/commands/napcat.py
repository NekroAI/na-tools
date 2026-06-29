"""napcat 命令：引导 NapCat 登录并自动配置 OneBot 连接。"""

from pathlib import Path

import click

from ..services.napcat_service import (
    NapcatConfigureRequest,
    NapcatService,
    NapcatServiceError,
    napcat_config_path,
)
from ..utils.console import confirm, error, info, print_panel, prompt, success, warning
from ..utils.privilege import with_sudo_fallback


@click.command()
@with_sudo_fallback
@click.option("--data-dir", type=click.Path(), default=None, help="数据目录路径")
@click.option("--qq", default=None, help="已登录的 QQ 号")
def napcat(data_dir: str | None, qq: str | None) -> None:
    """引导 NapCat 登录并自动配置 OneBot 连接。"""
    data_dir_path = Path(data_dir).expanduser().resolve() if data_dir else None
    service = NapcatService()
    try:
        prepared = service.prepare(data_dir_path)
    except NapcatServiceError as exc:
        error(exc.message)
        if exc.code == "env_missing":
            error("请先运行 `na-tools install` 完成安装。")
        raise click.Abort() from exc

    if prepared.missing_napcat_port:
        warning("未检测到 NAPCAT_EXPOSE_PORT 配置，请确认安装时已启用 NapCat 服务。")

    login_guide = (
        "请在浏览器中打开 NapCat 管理界面:\n\n"
        f"  http://127.0.0.1:{prepared.napcat_port}/webui\n\n"
        "首次访问需要 WebUI Token，可通过以下命令查看启动日志:\n\n"
        "  na-tools logs napcat\n\n"
        "扫描二维码完成 QQ 登录后，返回此处继续。"
    )
    print_panel("NapCat 登录引导", login_guide, style="cyan")

    if not confirm("已完成 NapCat 登录?", default=False):
        info("操作已取消。")
        raise click.Abort()

    if qq is None:
        qq = prompt("请输入已登录的 QQ 号")

    qq = qq.strip()
    candidate_config_path = napcat_config_path(prepared.data_dir, qq)
    overwrite = True
    if candidate_config_path.exists():
        warning(f"配置文件已存在: {candidate_config_path}")
        overwrite = confirm("是否覆盖?", default=True)
        if not overwrite:
            info("操作已取消。")
            raise click.Abort()

    info("配置已更新，需要重启 NapCat 使新配置生效。")
    restart = confirm("是否立即重启 NapCat 服务?", default=True)
    try:
        result = service.configure(
            NapcatConfigureRequest(
                data_dir=prepared.data_dir,
                qq=qq,
                overwrite=overwrite,
                restart=restart,
            )
        )
    except NapcatServiceError as exc:
        error(exc.message)
        raise click.Abort() from exc

    success(f"OneBot 配置已写入: {result.config_path}")
    if restart:
        if result.restarted:
            success("NapCat 服务已重启!")
        else:
            warning(f"重启失败，请手动执行: docker compose restart {result.restart_service_name}")

    print_panel(
        "配置完成",
        f"QQ 号: {result.qq}\n"
        + f"WebSocket 地址: {result.ws_url}\n"
        + f"OneBot Token: {'(已设置)' if result.token_set else '(未设置)'}\n\n"
        + "Nekro Agent 现在将通过 NapCat 收发消息。\n"
        + "如连接异常，请检查两个服务是否在同一 Docker 网络中。",
        style="green",
    )
