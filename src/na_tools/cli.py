"""NA-Tools CLI 入口。"""

from io import StringIO

import click
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from . import __version__
from .commands.backup import backup
from .commands.bind import bind
from .commands.config_cmd import config
from .commands.install import install
from .commands.list_cmd import list_cmd
from .commands.logs import logs
from .commands.napcat import napcat
from .commands.remove import remove
from .commands.restore import restore
from .commands.status import status
from .commands.update import update
from .commands.use import use

# 命令分组：(分组名, [命令名...])
COMMAND_GROUPS: list[tuple[str, list[str]]] = [
    ("部署管理", ["install", "update", "remove"]),
    ("实例管理", ["bind", "use", "list", "status"]),
    ("数据管理", ["backup", "restore", "config"]),
    ("日志与工具", ["logs", "napcat"]),
]


class RichGroup(click.Group):
    """使用 Rich 美化帮助输出的 Click 命令组。"""

    def format_help(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        sio = StringIO()
        console = Console(file=sio, force_terminal=True)

        # 标题面板
        title = Text()
        title.append("◆ ", style="bold cyan")
        title.append("NA-Tools", style="bold white")
        title.append(f" v{__version__}", style="dim")
        title.append("\n")
        title.append("Nekro Agent 部署管理工具", style="cyan")
        console.print()
        console.print(Panel(title, border_style="cyan", padding=(1, 2)))

        # 收集所有命令
        commands = self.list_commands(ctx)
        cmd_map: dict[str, click.Command] = {}
        for name in commands:
            cmd = self.get_command(ctx, name)
            if cmd is not None:
                cmd_map[name] = cmd

        # 按分组渲染
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

        # 未分组的命令（兜底）
        uncategorized = [
            (n, cmd_map[n]) for n in commands if n not in categorized and n in cmd_map
        ]
        if uncategorized:
            console.print("  [bold cyan]其他[/]")
            for name, cmd in uncategorized:
                help_text = cmd.get_short_help_str(limit=60)
                console.print(f"    [green]{name:<12}[/] {help_text}")
            console.print()

        # 全局选项
        console.print("  [bold cyan]选项[/]")
        console.print("    [green]--version     [/] 显示版本号")
        console.print("    [green]--help        [/] 显示帮助信息")
        console.print()

        # 尾部提示
        console.print(
            "  [dim]使用[/] [bold]na-tools <命令> --help[/] [dim]查看具体命令的帮助信息[/]"
        )
        console.print()

        formatter.write(sio.getvalue())


@click.group(cls=RichGroup)
@click.version_option(version=__version__, prog_name="na-tools")
def main() -> None:
    """NA-Tools: Nekro Agent 部署管理工具"""


main.add_command(install)
main.add_command(bind)
main.add_command(remove)
main.add_command(update)
main.add_command(backup)
main.add_command(restore)
main.add_command(config)
main.add_command(status)
main.add_command(logs)
main.add_command(use)
main.add_command(list_cmd)
main.add_command(napcat)


if __name__ == "__main__":
    main()
