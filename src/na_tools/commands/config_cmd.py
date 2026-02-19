"""config 命令：快捷配置 nekro-agent.yaml。"""

from pathlib import Path
from typing import cast

import click

from ..core.docker import DockerEnv
from ..core.na_config import (
    get_nested,
    get_super_users,
    load_na_config,
    save_na_config,
    set_model_group,
    set_nested,
    set_super_users,
)
from ..core.platform import default_data_dir
from ..utils.console import (
    confirm,
    console,
    error,
    info,
    prompt,
    success,
    warning,
    create_table,
)


def _resolve_data_dir(data_dir: str | None) -> Path:
    return Path(data_dir or default_data_dir()).expanduser().resolve()


def _resolve_ctx_data_dir(ctx: click.Context) -> Path:
    obj = cast(dict[str, object], ctx.obj)
    return _resolve_data_dir(cast(str | None, obj.get("data_dir")))


@click.group(invoke_without_command=True)
@click.option("--data-dir", type=click.Path(), default=None, help="数据目录路径")
@click.pass_context
def config(ctx: click.Context, data_dir: str | None) -> None:
    """快捷配置 nekro-agent.yaml。

    不带子命令时进入交互式配置向导。
    """
    obj = cast(dict[str, object], ctx.ensure_object(dict))
    obj["data_dir"] = data_dir

    if ctx.invoked_subcommand is None:
        _interactive_wizard(data_dir)


def _interactive_wizard(data_dir_str: str | None) -> None:
    """交互式配置向导。"""
    data_dir = _resolve_data_dir(data_dir_str)
    data = load_na_config(data_dir)

    if not data:
        warning("配置文件不存在或为空，将创建新配置。")
        warning("请先运行 `na-tools install` 完成安装并启动服务后，再执行配置。")
        if not confirm("是否继续?"):
            return

    info("=== Nekro Agent 快捷配置向导 ===")

    # 1. 模型 API 配置
    # 1. 模型 API 配置
    info("\n📦 步骤 1/3: 模型 API 配置")
    system = cast(dict[str, object], data.get("system", data))
    groups = cast(dict[str, object], system.get("MODEL_GROUPS", {}))
    default_group = cast(dict[str, object], groups.get("default", {}))

    base_url = prompt(
        "API 地址 (BASE_URL)",
        default=cast(str, default_group.get("BASE_URL", "https://api.nekro.ai/v1")),
    )
    api_key = prompt(
        "API 密钥 (API_KEY)", default=cast(str, default_group.get("API_KEY", ""))
    )
    model = prompt(
        "聊天模型 (CHAT_MODEL)",
        default=cast(str, default_group.get("CHAT_MODEL", "gemini-2.5-flash")),
    )

    set_model_group(data, "default", base_url=base_url, api_key=api_key, model=model)

    # 2. 管理员配置
    info("\n👤 步骤 2/3: 管理员配置")
    current_users = get_super_users(data)
    if current_users:
        info(f"当前管理员: {', '.join(current_users)}")

    add_admin = prompt("添加管理员 QQ 号 (多个用逗号分隔，留空跳过)", default="")
    if add_admin.strip():
        new_users = [u.strip() for u in add_admin.split(",") if u.strip()]
        merged = list(dict.fromkeys(current_users + new_users))
        set_super_users(data, merged)
        info(f"管理员列表: {', '.join(merged)}")

    # 3. 聊天人设 (可选)
    info("\n🎭 步骤 3/3: 聊天人设 (可选)")
    current_name = cast(str, system.get("AI_CHAT_PRESET_NAME", "可洛喵"))
    if confirm(f"是否修改聊天人设? (当前: {current_name})", default=False):
        preset_name = prompt("人设名称", default=current_name)
        preset_setting = prompt(
            "人设描述", default=cast(str, system.get("AI_CHAT_PRESET_SETTING", ""))
        )
        if "system" in data and isinstance(data["system"], dict):
            system_conf = cast(dict[str, object], data["system"])
            system_conf["AI_CHAT_PRESET_NAME"] = preset_name
            system_conf["AI_CHAT_PRESET_SETTING"] = preset_setting
        else:
            data["AI_CHAT_PRESET_NAME"] = preset_name
            data["AI_CHAT_PRESET_SETTING"] = preset_setting

    # 保存
    save_na_config(data_dir, data)

    # 提示重启
    info("\n配置已保存。修改将在服务重启后生效。")
    if confirm("是否立即重启 nekro_agent 服务?", default=False):
        docker = DockerEnv()
        env_path = data_dir / ".env"
        if docker.restart_service(
            "nekro_agent",
            cwd=data_dir,
            env_file=env_path if env_path.exists() else None,
        ):
            success("服务已重启!")
        else:
            warning("重启失败，请手动重启。")


@config.command("get")
@click.argument("key")
@click.pass_context
def config_get(ctx: click.Context, key: str) -> None:
    """查看配置项的值。"""
    data_dir = _resolve_ctx_data_dir(ctx)
    data = load_na_config(data_dir)
    value = cast(object, get_nested(data, key))
    if value is None:
        # 尝试从 system 下查找
        value = cast(object, get_nested(data, f"system.{key}"))

    if value is None:
        error(f"配置项不存在: {key}")
    else:
        console.print(f"[bold]{key}[/bold] = {value}")


@config.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_context
def config_set(ctx: click.Context, key: str, value: str) -> None:
    """设置配置项的值。"""
    data_dir = _resolve_ctx_data_dir(ctx)
    data = load_na_config(data_dir)

    # 尝试类型转换
    parsed_value: object = value
    if value.lower() in ("true", "false"):
        parsed_value = value.lower() == "true"
    else:
        try:
            parsed_value = int(value)
        except ValueError:
            try:
                parsed_value = float(value)
            except ValueError:
                pass

    set_nested(data, key, parsed_value)
    save_na_config(data_dir, data)

    info("\n修改将在服务重启后生效。")


@config.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """查看当前配置摘要。"""
    data_dir = _resolve_ctx_data_dir(ctx)
    data = load_na_config(data_dir)

    if not data:
        warning("配置文件不存在或为空。")
        return

    # 模型组
    info("📦 模型组配置:")
    system = cast(dict[str, object], data.get("system", data))
    groups = cast(dict[str, object], system.get("MODEL_GROUPS", {}))
    if groups:
        table = create_table("组名", "模型", "API 地址", "视觉", "思维链")
        for name, group in groups.items():
            if isinstance(group, dict):
                g = cast(dict[str, object], group)
                table.add_row(
                    name,
                    cast(str, g.get("CHAT_MODEL", "")),
                    cast(str, g.get("BASE_URL", "")),
                    "✓" if cast(bool, g.get("ENABLE_VISION")) else "✗",
                    "✓" if cast(bool, g.get("ENABLE_COT")) else "✗",
                )
        console.print(table)
    else:
        warning("  无模型组配置")

    # 管理员
    users = get_super_users(data)
    info(f"\n👤 管理员: {', '.join(users) if users else '未设置'}")

    # 人设
    info(f"🎭 人设: {system.get('AI_CHAT_PRESET_NAME', '未设置')}")

    # 使用的模型组
    info(f"🔧 主模型组: {system.get('USE_MODEL_GROUP', 'default')}")


@config.command("model")
@click.option("--group", default="default", help="模型组名称")
@click.pass_context
def config_model(ctx: click.Context, group: str) -> None:
    """交互式配置模型组。"""
    data_dir = _resolve_ctx_data_dir(ctx)
    data = load_na_config(data_dir)
    data_dir = _resolve_ctx_data_dir(ctx)
    data = load_na_config(data_dir)
    system = cast(dict[str, object], data.get("system", data))
    groups = cast(dict[str, object], system.get("MODEL_GROUPS", {}))
    current = cast(dict[str, object], groups.get(group, {}))

    info(f"配置模型组: {group}")
    base_url = prompt("API 地址", default=cast(str, current.get("BASE_URL", "")))
    api_key = prompt("API 密钥", default=cast(str, current.get("API_KEY", "")))
    model = prompt("模型名称", default=cast(str, current.get("CHAT_MODEL", "")))
    enable_vision = confirm(
        "启用视觉?", default=cast(bool, current.get("ENABLE_VISION", False))
    )
    enable_cot = confirm(
        "启用外置思维链?", default=cast(bool, current.get("ENABLE_COT", False))
    )

    set_model_group(
        data,
        group,
        base_url=base_url,
        api_key=api_key,
        model=model,
        ENABLE_VISION=enable_vision,
        ENABLE_COT=enable_cot,
    )
    save_na_config(data_dir, data)

    info("\n修改将在服务重启后生效。")
    if confirm("是否立即重启 nekro_agent 服务?", default=False):
        docker = DockerEnv()
        env_path = data_dir / ".env"
        if docker.restart_service(
            "nekro_agent",
            cwd=data_dir,
            env_file=env_path if env_path.exists() else None,
        ):
            success("服务已重启!")


@config.command("admin")
@click.option("--add", "add_user", default=None, help="添加管理员 QQ 号")
@click.option("--remove", "remove_user", default=None, help="移除管理员 QQ 号")
@click.pass_context
def config_admin(
    ctx: click.Context, add_user: str | None, remove_user: str | None
) -> None:
    """管理管理员列表。"""
    data_dir = _resolve_ctx_data_dir(ctx)
    data = load_na_config(data_dir)
    users = get_super_users(data)

    if add_user:
        if add_user not in users:
            users.append(add_user)
            set_super_users(data, users)
            save_na_config(data_dir, data)
            success(f"已添加管理员: {add_user}")
        else:
            info(f"{add_user} 已在管理员列表中。")
    elif remove_user:
        if remove_user in users:
            users.remove(remove_user)
            set_super_users(data, users)
            save_na_config(data_dir, data)
            success(f"已移除管理员: {remove_user}")
        else:
            warning(f"{remove_user} 不在管理员列表中。")
    else:
        info(f"当前管理员: {', '.join(users) if users else '无'}")
