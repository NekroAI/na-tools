"""nekro-agent.yaml 应用配置管理。"""

from pathlib import Path
from typing import cast

import yaml


from ..utils.console import success, warning


def config_path(data_dir: Path) -> Path:
    """返回 nekro-agent.yaml 的路径。"""
    return data_dir / "configs" / "nekro-agent.yaml"


def load_na_config(data_dir: Path) -> dict[str, object]:
    """加载 nekro-agent.yaml 配置。

    Returns:
        配置字典。文件不存在时返回空字典。
    """
    path = config_path(data_dir)
    if not path.exists():
        warning(f"配置文件不存在: {path}")
        return {}

    with open(path, encoding="utf-8") as f:
        data: object = cast(object, yaml.safe_load(f))

    return cast(dict[str, object], data) if isinstance(data, dict) else {}


def save_na_config(data_dir: Path, data: dict[str, object]) -> None:
    """保存配置到 nekro-agent.yaml。"""
    path = config_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(  # type: ignore
            data, f, default_flow_style=False, allow_unicode=True, sort_keys=False
        )

    success(f"配置已保存: {path}")


def get_nested(data: dict[str, object], key_path: str) -> object | None:
    """通过点分路径获取嵌套值。

    示例: get_nested(data, "MODEL_GROUPS.default.API_KEY")
    """
    keys = key_path.split(".")
    current = data
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current


def set_nested(data: dict[str, object], key_path: str, value: object) -> None:
    """通过点分路径设置嵌套值。"""
    keys = key_path.split(".")
    current: dict[str, object] = data
    for key in keys[:-1]:
        if key not in current or not isinstance(current[key], dict):
            current[key] = {}

        # We ensured it's a dict above, so we can cast it
        next_val = current[key]
        if isinstance(next_val, dict):
            current = cast(dict[str, object], next_val)
        else:
            # Fallback (should normally not happen due to line 70)
            new_dict: dict[str, object] = {}
            current[key] = new_dict
            current = new_dict

    current[keys[-1]] = value


def get_model_groups(data: dict[str, object]) -> dict[str, object]:
    """获取模型组配置。"""
    system: object = data.get("system", data)
    if not isinstance(system, dict):
        system = {}

    # We know system is a dict now
    system_dict = cast(dict[str, object], system)
    groups = system_dict.get("MODEL_GROUPS", {})
    if isinstance(groups, dict):
        return cast(dict[str, object], groups)
    return {}


def set_model_group(
    data: dict[str, object],
    group_name: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    **kwargs: object,
) -> None:
    """设置模型组。"""
    groups: dict[str, object]

    if "system" in data and isinstance(data["system"], dict):
        system = cast(dict[str, object], data["system"])
        # setdefault returns the value. We need to cast it.
        groups_val = system.setdefault("MODEL_GROUPS", {})
        if isinstance(groups_val, dict):
            groups = cast(dict[str, object], groups_val)
        else:
            groups = {}  # Should probably fix it in data if it's wrong type?
            system["MODEL_GROUPS"] = groups
    else:
        groups_val = data.setdefault("MODEL_GROUPS", {})
        if isinstance(groups_val, dict):
            groups = cast(dict[str, object], groups_val)
        else:
            groups = {}
            data["MODEL_GROUPS"] = groups

    group_val = groups.get(group_name, {})
    group: dict[str, object] = (
        cast(dict[str, object], group_val) if isinstance(group_val, dict) else {}
    )

    group["BASE_URL"] = base_url
    group["API_KEY"] = api_key
    group["CHAT_MODEL"] = model
    group.update(kwargs)  # kwargs is dict[str, object] which fits
    groups[group_name] = group


def get_super_users(data: dict[str, object]) -> list[str]:
    """获取管理员列表。"""
    system: object = data.get("system", data)
    if not isinstance(system, dict):
        system = {}

    system_dict = cast(dict[str, object], system)
    users = system_dict.get("SUPER_USERS", [])
    if isinstance(users, list):
        # Assuming list of strings. If not, we might need check.
        return cast(list[str], users)
    return []


def set_super_users(data: dict[str, object], users: list[str]) -> None:
    """设置管理员列表。"""
    if "system" in data and isinstance(data["system"], dict):
        system = cast(dict[str, object], data["system"])
        system["SUPER_USERS"] = users
    else:
        data["SUPER_USERS"] = users
