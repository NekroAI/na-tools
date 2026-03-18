"""install 命令：全新安装 Nekro Agent。"""

from pathlib import Path

import click

from ..core.compose import (
    apply_mirror_to_compose,
    download_compose,
    patch_compose_isolation,
    set_image_tag,
)

from ..core.config import load_env, setup_env
from ..core.docker import DockerEnv
from ..core.platform import default_data_dir, resolve_mirror, set_default_data_dir
from ..utils.privilege import with_sudo_fallback
from ..utils.console import confirm, error, info, print_panel, prompt, warning


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
    with_cc_sandbox: bool | None,
) -> None:
    """安装 Nekro Agent 服务。"""
    interactive = not non_interactive

    info("=== Nekro Agent 安装向导 ===")

    # 1. 检测 Docker 环境
    docker = DockerEnv()
    if not docker.ensure_docker():
        raise click.Abort()

    # 2. 选择数据目录
    default_dir = str(default_data_dir())
    if data_dir is None and interactive:
        data_dir = prompt("请设置数据目录", default=default_dir)
    data_dir_path = Path(data_dir or default_dir).expanduser().resolve()

    data_dir_path.mkdir(parents=True, exist_ok=True)
    info(f"数据目录: {data_dir_path}")

    # 3. 是否含 NapCat
    if with_napcat is None and interactive:
        with_napcat = confirm("是否同时使用 NapCat 服务?", default=True)
    elif with_napcat is None:
        with_napcat = False

    # 4. 生成 .env 配置
    info("正在配置 .env 文件...")
    try:
        env_path = setup_env(
            data_dir_path, interactive=interactive, with_napcat=with_napcat, port=port
        )
    except RuntimeError as e:
        error(str(e))
        raise click.Abort()

    if interactive:
        if not confirm("配置已生成，是否继续安装?", default=True):
            info("安装已取消。您可以编辑 .env 文件后重新运行安装。")
            raise click.Abort()

    # 5. 下载 docker-compose.yml
    info("正在下载 docker-compose.yml...")
    if not download_compose(data_dir_path, with_napcat=with_napcat):
        error("无法下载 docker-compose.yml，请检查网络连接。")
        raise click.Abort()

    patch_compose_isolation(data_dir_path)

    # 6. 配置镜像站
    mirror = resolve_mirror(env_path)
    if mirror:
        info(f"应用镜像站配置: {mirror}")
        apply_mirror_to_compose(data_dir_path, mirror)

    # 6.5 如果使用 preview 频道，修改镜像 tag
    if preview:
        info("使用 preview 频道镜像...")
        if not set_image_tag(data_dir_path, "kromiose/nekro-agent", "preview"):
            warning("无法修改镜像 tag，将使用默认 latest 版本。")

    # 7. 拉取服务镜像
    info("正在拉取服务镜像...")
    if not docker.pull(cwd=data_dir_path, env_file=env_path):
        error("镜像拉取失败，请检查网络连接。")
        raise click.Abort()

    # 8. 启动服务
    info("正在启动服务...")
    if not docker.up(cwd=data_dir_path, env_file=env_path):
        error("服务启动失败。")
        raise click.Abort()

    # 9. 拉取沙盒镜像
    info("正在拉取沙盒镜像...")
    if not docker.docker_pull("kromiose/nekro-agent-sandbox", mirror=mirror):
        warning(
            "沙盒镜像拉取失败，可稍后手动拉取: docker pull kromiose/nekro-agent-sandbox"
        )

    # 9.5 CC 沙盒镜像
    if with_cc_sandbox is None and interactive:
        with_cc_sandbox = confirm("是否拉取 CC 沙盒镜像 (nekro-cc-sandbox)?", default=False)
    if with_cc_sandbox:
        info("正在拉取 CC 沙盒镜像...")
        if not docker.docker_pull("kromiose/nekro-cc-sandbox", mirror=mirror):
            warning(
                "CC 沙盒镜像拉取失败，可稍后手动拉取: docker pull kromiose/nekro-cc-sandbox"
            )

    # 10. 保存到全局配置
    set_default_data_dir(data_dir_path)

    # 11. 显示部署结果
    env = load_env(env_path)
    expose_port = env.get("NEKRO_EXPOSE_PORT", "8021")
    admin_password = env.get("NEKRO_ADMIN_PASSWORD", "")
    onebot_token = env.get("ONEBOT_ACCESS_TOKEN", "")

    channel = "preview" if preview else "stable"
    result_lines = [
        f"数据目录: {data_dir_path}",
        f"频道: {channel}",
        f"服务端口: {expose_port}",
        f"Web 访问: http://127.0.0.1:{expose_port}",
        "",
        "管理员账号: admin",
        f"管理员密码: {admin_password}",
        f"OneBot Token: {onebot_token}",
    ]

    if with_napcat:
        napcat_port = env.get("NAPCAT_EXPOSE_PORT", "6099")
        result_lines.append(f"NapCat 端口: {napcat_port}")

    result_lines.extend(
        [
            "",
            "查看日志: na-tools logs nekro_agent",
            "查看状态: na-tools status",
        ]
    )

    print_panel("🎉 部署完成!", "\n".join(result_lines), style="green")
