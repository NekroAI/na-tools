import functools
import os
import subprocess
import sys
from collections.abc import Sequence
from typing import Callable, ParamSpec, TypeVar, cast

import click

from .console import error, info, warning

P = ParamSpec("P")
R = TypeVar("R")


def is_permission_error(e: Exception) -> bool:
    # 1. 明确的 Python 权限异常
    if isinstance(e, PermissionError):
        return True

    # 2. 异常消息中包含 "Permission denied" 或 "permission denied"
    msg = str(e).lower()
    if "permission denied" in msg:
        return True

    # 3. 针对 CalledProcessError 的特殊处理（尤其是 Docker）
    if isinstance(e, subprocess.CalledProcessError):
        # 如果当前不是 root 且在 Linux/macOS 上
        if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0:
            # 检查命令是否包含 docker
            cmd_val = cast(object, e.cmd)
            if isinstance(cmd_val, list):
                # 使用 cast 确保类型检查器知道列表元素可以被转换为字符串，使用 object 避免 Any
                cmd_str = " ".join(str(c) for c in cast(Sequence[object], cmd_val))
            else:
                cmd_str = str(cmd_val)

            if "docker" in cmd_str:
                # 检查 docker socket 权限
                docker_socket = "/var/run/docker.sock"
                if os.path.exists(docker_socket) and not os.access(
                    docker_socket, os.W_OK
                ):
                    return True

    return False


def with_sudo_fallback(func: Callable[P, R]) -> Callable[P, R]:
    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if is_permission_error(e):
                error(f"由于权限不足导致操作中断: {e}")
                if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0:
                    warning("检测到权限问题，将尝试获取管理员权限(root)以完成配置。")
                    info("请在下方输入您的当前用户密码：")
                    # 使用 sudo -E 保留环境变量，并执行当前脚本
                    os.execvp("sudo", ["sudo", "-E", sys.executable] + sys.argv)
                else:
                    raise click.Abort()
            raise

    return wrapper
