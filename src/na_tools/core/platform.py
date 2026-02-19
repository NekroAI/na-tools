"""跨平台适配层（仅支持 Linux 和 macOS）。"""

import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Optional

from ..utils.console import error


def get_os() -> str:
    """返回当前操作系统标识: 'linux', 'darwin'。Windows 下直接退出。"""
    os_name = platform.system().lower()
    if os_name == "windows":
        error("不支持 Windows 系统。请在 Linux 或 macOS 上运行。")
        sys.exit(1)
    return os_name


def is_linux() -> bool:
    return get_os() == "linux"


def is_macos() -> bool:
    return get_os() == "darwin"


def get_global_config_dir() -> Path:
    """返回全局配置目录。"""
    path = Path.home() / ".config" / "na-tools"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_global_config() -> dict[str, object]:
    """加载全局配置。"""
    config_path = get_global_config_dir() / "config.json"
    if not config_path.exists():
        return {}
    try:
        import json

        return json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_global_config(config: dict[str, object]) -> None:
    """保存全局配置。"""
    import json

    config_path = get_global_config_dir() / "config.json"
    config_path.write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def set_default_data_dir(data_dir: Path) -> None:
    """设置默认数据目录。"""
    config = load_global_config()
    str_path = str(data_dir.expanduser().resolve())
    config["current_data_dir"] = str_path

    # Update installations list
    import time

    installations = config.get("installations", {})
    if not isinstance(installations, dict):
        installations = {}

    installations[str_path] = {
        "installed_at": installations.get(str_path, {}).get(
            "installed_at", int(time.time())
        ),
        "last_used": int(time.time()),
    }
    config["installations"] = installations

    save_global_config(config)


def default_data_dir() -> Path:
    """返回默认数据目录。优先读取全局配置。"""
    # 1. 尝试读取全局配置
    config = load_global_config()
    if current := config.get("current_data_dir"):
        if isinstance(current, str):
            return Path(current)

    # 2. 回退到硬编码默认值
    return Path.home() / "nekro_agent"


def run_cmd(
    cmd: list[str],
    *,
    cwd: Optional[Path] = None,
    capture: bool = False,
    check: bool = True,
    env: Optional[dict[str, str]] = None,
    unset_keys: Optional[set[str]] = None,
) -> subprocess.CompletedProcess[str]:
    """跨平台命令执行封装。

    Args:
        cmd: 命令参数列表。
        cwd: 工作目录。
        capture: 是否捕获输出。
        check: 是否在非零退出码时抛异常。
        env: 额外环境变量（合并到当前环境）。
        unset_keys: 需要从环境中移除的变量名集合。
    """
    merged_env: dict[str, str] | None = None
    if env or unset_keys:
        merged_env = {**os.environ}
        if unset_keys:
            for key in unset_keys:
                merged_env.pop(key, None)
        if env:
            merged_env.update(env)

    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=capture,
        text=True,
        check=check,
        env=merged_env,
    )


def docker_socket_volume() -> str:
    """返回 Docker socket 挂载路径（用于 compose 文件中）。"""
    return "/var/run/docker.sock:/var/run/docker.sock"
