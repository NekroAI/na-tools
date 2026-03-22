"""napcat 命令：引导 NapCat 登录并自动配置 OneBot 连接。"""

import json
from pathlib import Path

import click

from ..core.compose import SERVICE_AGENT
from ..core.config import get_container_name, get_service_name, load_env
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir
from ..utils.console import confirm, error, info, print_panel, prompt, success, warning
from ..utils.privilege import with_sudo_fallback


def _napcat_config_path(data_dir: Path, qq: str) -> Path:
    """返回 NapCat onebot 配置文件路径。"""
    return data_dir / "napcat_data" / "napcat" / f"onebot11_{qq}.json"

def _build_onebot_config(ws_url: str, token: str) -> dict[str, object]:
    """构建 NapCat OneBot WebSocket 客户端配置。"""
    return {
        "network": {
            "httpServers": [],
            "httpSseServers": [],
            "httpClients": [],
            "websocketServers": [],
            "websocketClients": [
                {
                    "enable": True,
                    "name": "na",
                    "url": ws_url,
                    "reportSelfMessage": False,
                    "messagePostFormat": "array",
                    "token": token,
                    "debug": False,
                    "heartInterval": 30000,
                    "reconnectInterval": 7000,
                }
            ],
            "plugins": [],
        },
        "musicSignUrl": "",
        "enableLocalFile2Url": False,
        "parseMultMsg": False,
        "imageDownloadProxy": "",
    }


@click.command()
@with_sudo_fallback
@click.option("--data-dir", type=click.Path(), default=None, help="数据目录路径")
@click.option("--qq", default=None, help="已登录的 QQ 号")
def napcat(data_dir: str | None, qq: str | None) -> None:
    """引导 NapCat 登录并自动配置 OneBot 连接。"""
    data_dir_path = Path(data_dir or default_data_dir()).expanduser().resolve()

    env_path = data_dir_path / ".env"
    if not env_path.exists():
        error(f"未找到 .env 文件: {env_path}")
        error("请先运行 `na-tools install` 完成安装。")
        raise click.Abort()

    env = load_env(env_path)

    napcat_port = env.get("NAPCAT_EXPOSE_PORT", "6099")
    token = env.get("ONEBOT_ACCESS_TOKEN", "")

    if not env.get("NAPCAT_EXPOSE_PORT"):
        warning("未检测到 NAPCAT_EXPOSE_PORT 配置，请确认安装时已启用 NapCat 服务。")

    # 1. 引导用户登录 NapCat
    login_guide = (
        "请在浏览器中打开 NapCat 管理界面:\n\n"
        f"  http://127.0.0.1:{napcat_port}/webui\n\n"
        "首次访问需要 WebUI Token，可通过以下命令查看启动日志:\n\n"
        "  na-tools logs napcat\n\n"
        "扫描二维码完成 QQ 登录后，返回此处继续。"
    )
    print_panel("NapCat 登录引导", login_guide, style="cyan")

    if not confirm("已完成 NapCat 登录?", default=False):
        info("操作已取消。")
        raise click.Abort()

    # 2. 获取 QQ 号
    if qq is None:
        qq = prompt("请输入已登录的 QQ 号")

    qq = qq.strip()
    if not qq:
        error("QQ 号不能为空。")
        raise click.Abort()
    if not qq.isdigit():
        error("QQ 号只能包含数字。")
        raise click.Abort()

    # 3. 构建并写入配置（容器名与 compose 模板 ${INSTANCE_NAME:-}nekro_agent 一致）
    na_hostname = get_container_name(SERVICE_AGENT, env)
    ws_url = f"ws://{na_hostname}:8021/onebot/v11/ws"
    config = _build_onebot_config(ws_url, token)

    config_path = _napcat_config_path(data_dir_path, qq)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        warning(f"配置文件已存在: {config_path}")
        if not confirm("是否覆盖?", default=True):
            info("操作已取消。")
            raise click.Abort()

    _ = config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    success(f"OneBot 配置已写入: {config_path}")

    # 4. 提示重启 NapCat 使配置生效
    info("配置已更新，需要重启 NapCat 使新配置生效。")
    if confirm("是否立即重启 NapCat 服务?", default=True):
        docker = DockerEnv()
        napcat_service = get_service_name("nekro_napcat")
        if docker.restart_service(napcat_service, cwd=data_dir_path, env_file=env_path):
            success("NapCat 服务已重启!")
        else:
            warning(f"重启失败，请手动执行: docker compose restart {napcat_service}")

    print_panel(
        "配置完成",
        f"QQ 号: {qq}\n"
        + f"WebSocket 地址: {ws_url}\n"
        + f"OneBot Token: {'(已设置)' if token else '(未设置)'}\n\n"
        + "Nekro Agent 现在将通过 NapCat 收发消息。\n"
        + "如连接异常，请检查两个服务是否在同一 Docker 网络中。",
        style="green",
    )
