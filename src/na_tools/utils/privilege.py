from typing import Callable, ParamSpec, TypeVar
import click

P = ParamSpec("P")
R = TypeVar("R")


def with_sudo_fallback(func: Callable[P, R]) -> Callable[P, R]:
    import functools
    import sys
    import os
    from .console import error, warning, info

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except PermissionError as e:
            error(f"由于权限不足导致操作中断: {e}")
            if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() != 0:
                warning("检测到权限问题，将尝试获取管理员权限(root)以完成配置。")
                info("请在下方输入您的当前用户密码：")
                os.execvp("sudo", ["sudo", "-E", sys.executable] + sys.argv)
            else:
                raise click.Abort()
        except Exception as e:
            if (
                "Permission denied" in str(e)
                and os.name != "nt"
                and hasattr(os, "geteuid")
                and os.geteuid() != 0
            ):
                error(f"由于权限不足导致操作中断: {e}")
                warning("检测到权限问题，将尝试获取管理员权限(root)以完成配置。")
                info("请在下方输入您的当前用户密码：")
                os.execvp("sudo", ["sudo", "-E", sys.executable] + sys.argv)
            raise

    return wrapper
