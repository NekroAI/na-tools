"""bind 命令：将已安装的 NA 实例绑定到 na-tools 管理列表。"""

from pathlib import Path

import click

from ..services.instance_service import BindRequest, InstanceService, InstanceServiceError
from ..utils.console import confirm, error, info, prompt, success, warning
from ..utils.privilege import with_sudo_fallback


@click.command()
@with_sudo_fallback
@click.option(
    "--data-dir",
    type=click.Path(),
    default=None,
    help="NA 实例的数据目录路径（未指定时交互式输入）",
)
@click.option("--name", type=str, default=None, help="为该实例指定一个名称（可选）")
@click.option(
    "--as-current/--no-as-current",
    default=None,
    help="绑定后是否设为当前激活实例",
)
def bind(data_dir: str | None, name: str | None, as_current: bool | None) -> None:
    """将环境中已手动安装的 NA 实例绑定到 na-tools 管理列表。"""
    if data_dir is None:
        data_dir = prompt("请输入 NA 实例的数据目录路径")
        if not data_dir.strip():
            error("数据目录路径不能为空")
            raise click.Abort()

    if name is None:
        input_name = prompt("为该实例指定一个名称（直接回车跳过）")
        if input_name.strip():
            name = input_name.strip()

    data_dir_path = Path(data_dir).expanduser().resolve()
    service = InstanceService()

    # Preserve the original prompt timing as closely as possible.
    if as_current is None:
        as_current = confirm("是否将其设为当前激活实例?", default=True)

    try:
        result = service.bind(
            BindRequest(data_dir=data_dir_path, name=name, as_current=as_current)
        )
    except InstanceServiceError as exc:
        error(exc.message)
        if exc.code == "compose_missing":
            info("有效的 NA 目录应包含 docker-compose.yml 文件")
        raise click.Abort() from exc

    info(f"daemon 实例 ID: {result.daemon_channel.instance_id}")
    if result.daemon_channel.compose_warning:
        warning(f"daemon compose 配置未自动合并: {result.daemon_channel.compose_warning}")
    elif result.daemon_channel.compose_updated:
        info("已补齐 daemon compose 环境变量和 host gateway。")

    if result.already_bound:
        info(f"该 NA 实例已在管理列表中: {result.data_dir}")
        if result.as_current:
            success("已设为当前激活实例")
        return

    info_lines = [f"已成功绑定 NA 实例: {result.data_dir}"]
    if result.name:
        info_lines.append(f"实例名称: {result.name}")
    if result.as_current:
        info_lines.append("已设为当前激活实例")
    success("\n".join(info_lines))

    info("\n后续操作：")
    info("  na-tools status    查看该实例状态")
    info("  na-tools list      查看所有实例")
