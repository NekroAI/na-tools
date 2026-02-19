"""统一终端输出工具，基于 Rich。"""

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.theme import Theme

_theme = Theme(
    {
        "info": "cyan",
        "success": "bold green",
        "warning": "bold yellow",
        "error": "bold red",
    }
)

console = Console(theme=_theme)


def info(msg: str) -> None:
    console.print(f"[info]ℹ[/info] {msg}")


def success(msg: str) -> None:
    console.print(f"[success]✔[/success] {msg}")


def warning(msg: str) -> None:
    console.print(f"[warning]⚠[/warning] {msg}")


def error(msg: str) -> None:
    console.print(f"[error]✖[/error] {msg}")


def confirm(msg: str, default: bool = False) -> bool:
    return Confirm.ask(msg, default=default, console=console)


def prompt(msg: str, default: str = "") -> str:
    return Prompt.ask(msg, default=default or None, console=console) or default


def print_panel(title: str, content: str, style: str = "cyan") -> None:
    console.print(Panel(content, title=title, border_style=style))


def create_table(*columns: str) -> Table:
    table = Table(show_header=True, header_style="bold magenta")
    for col in columns:
        table.add_column(col)
    return table
