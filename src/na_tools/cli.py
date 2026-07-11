"""NA-Tools CLI 入口。"""

import json
import os
import time
from io import StringIO
from pathlib import Path

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from . import __version__
from .commands.backup import backup
from .commands.bind import bind
from .commands.config_cmd import config
from .commands.daemon import daemon
from .commands.install import install
from .commands.list_cmd import list_cmd
from .commands.logs import logs
from .commands.napcat import napcat
from .commands.orchestration import start, stop
from .commands.remove import remove
from .commands.restore import restore
from .commands.status import status
from .commands.update import update
from .commands.upgrade import upgrade
from .commands.use import use
from .services.upgrade_service import (
    UpgradeService,
    UpgradeServiceError,
    parse_version,
)

PASSIVE_UPGRADE_CHECK_TIMEOUT = 3.0
PASSIVE_UPGRADE_CHECK_INTERVAL = 6 * 60 * 60

# 命令分组：(分组名, [命令名...])
COMMAND_GROUPS: list[tuple[str, list[str]]] = [
    ("部署管理", ["install", "start", "stop", "update", "remove", "daemon"]),
    ("实例管理", ["bind", "use", "list", "status"]),
    ("数据管理", ["backup", "restore", "config"]),
    ("日志与工具", ["logs", "napcat", "upgrade"]),
]


class RichGroup(click.Group):
    """使用 Rich 美化帮助输出的 Click 命令组。"""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        sio = StringIO()
        console = Console(file=sio, force_terminal=True)

        self._render_title(console)
        cmd_map = self._collect_commands(ctx)
        self._render_groups(console, cmd_map, ctx)
        self._render_options(console)

        formatter.write(sio.getvalue())

    def _render_title(self, console: Console) -> None:
        """渲染标题面板。"""
        title = Text()
        title.append("◆ ", style="bold cyan")
        title.append("NA-Tools", style="bold white")
        title.append(f" v{__version__}", style="dim")
        title.append("\n")
        title.append("Nekro Agent 部署管理工具", style="cyan")
        console.print()
        console.print(Panel(title, border_style="cyan", padding=(1, 2)))

    def _collect_commands(self, ctx: click.Context) -> dict[str, click.Command]:
        """收集所有已注册命令。"""
        cmd_map: dict[str, click.Command] = {}
        for name in self.list_commands(ctx):
            cmd = self.get_command(ctx, name)
            if cmd is not None:
                cmd_map[name] = cmd
        return cmd_map

    def _render_groups(
        self,
        console: Console,
        cmd_map: dict[str, click.Command],
        ctx: click.Context,
    ) -> None:
        """按分组渲染命令列表，包括未分组的兜底。"""
        categorized: set[str] = set()
        for group_name, cmd_names in COMMAND_GROUPS:
            group_cmds = [(n, cmd_map[n]) for n in cmd_names if n in cmd_map]
            if not group_cmds:
                continue
            console.print(f"  [bold cyan]{group_name}[/]")
            for name, cmd in group_cmds:
                help_text = cmd.get_short_help_str(limit=60)
                console.print(f"    [green]{name:<12}[/] {help_text}")
                categorized.add(name)
            console.print()

        # 未分组的命令
        uncategorized = [
            (n, cmd_map[n])
            for n in self.list_commands(ctx)
            if n not in categorized and n in cmd_map
        ]
        if uncategorized:
            console.print("  [bold cyan]其他[/]")
            for name, cmd in uncategorized:
                help_text = cmd.get_short_help_str(limit=60)
                console.print(f"    [green]{name:<12}[/] {help_text}")
            console.print()

    @staticmethod
    def _render_options(console: Console) -> None:
        """渲染全局选项和尾部提示。"""
        console.print("  [bold cyan]选项[/]")
        console.print("    [green]--version     [/] 显示版本号")
        console.print("    [green]--help        [/] 显示帮助信息")
        console.print()
        console.print(
            "  [dim]使用[/] [bold]na-tools <命令> --help[/] [dim]查看具体命令的帮助信息[/]"
        )
        console.print()


def _notify_upgrade_available() -> None:
    """Best-effort update notice shown after successful CLI invocations."""

    latest_version = _load_cached_latest_version()
    update_available: bool
    if latest_version is None:
        try:
            result = UpgradeService(
                release_timeout=PASSIVE_UPGRADE_CHECK_TIMEOUT,
            ).check()
        except UpgradeServiceError:
            _save_cached_latest_version(__version__)
            return
        latest_version = result.latest_version
        update_available = result.update_available
        _save_cached_latest_version(latest_version)
    else:
        try:
            update_available = parse_version(__version__) < parse_version(latest_version)
        except UpgradeServiceError:
            return

    if update_available:
        click.echo(
            f"⚠ 发现 na-tools 新版本 {latest_version}"
            f"（当前 {__version__}），运行 na-tools upgrade 更新。",
            err=True,
        )


def _upgrade_cache_path() -> Path:
    """Return the per-user cache used only by passive update checks."""

    cache_root = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache_root).expanduser() if cache_root else Path.home() / ".cache"
    return base / "na-tools" / "upgrade-check.json"


def _load_cached_latest_version() -> str | None:
    """Return a fresh cached release version, ignoring malformed cache data."""

    try:
        payload = json.loads(_upgrade_cache_path().read_text(encoding="utf-8"))
        checked_at = float(payload["checked_at"])
        latest_version = payload["latest_version"]
        age = time.time() - checked_at
        if (
            not isinstance(latest_version, str)
            or not 0 <= age <= PASSIVE_UPGRADE_CHECK_INTERVAL
        ):
            return None
        _ = parse_version(latest_version)
        return latest_version
    except (KeyError, OSError, TypeError, ValueError, UpgradeServiceError):
        return None


def _save_cached_latest_version(latest_version: str) -> None:
    """Persist a passive-check result without affecting the command on failure."""

    path = _upgrade_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"checked_at": time.time(), "latest_version": latest_version},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError:
        return


def _version_callback(
    ctx: click.Context,
    _param: click.Parameter,
    value: bool,
) -> None:
    """Print the version, then perform the passive update check."""

    if not value or ctx.resilient_parsing:
        return
    click.echo(f"na-tools, version {__version__}", color=ctx.color)
    _notify_upgrade_available()
    ctx.exit()


@click.group(cls=RichGroup)
@click.option(
    "--version",
    is_flag=True,
    expose_value=False,
    is_eager=True,
    callback=_version_callback,
    help="显示版本号并退出",
)
def main() -> None:
    """NA-Tools: Nekro Agent 部署管理工具"""


@main.result_callback()
@click.pass_context
def _after_command(ctx: click.Context, result: object) -> object:
    """Check for updates after a successful non-upgrade command."""

    if ctx.invoked_subcommand != "upgrade":
        _notify_upgrade_available()
    return result


main.add_command(install)
main.add_command(start)
main.add_command(stop)
main.add_command(bind)
main.add_command(remove)
main.add_command(update)
main.add_command(backup)
main.add_command(restore)
main.add_command(config)
main.add_command(daemon)
main.add_command(status)
main.add_command(logs)
main.add_command(use)
main.add_command(list_cmd)
main.add_command(napcat)
main.add_command(upgrade)


if __name__ == "__main__":
    main()
