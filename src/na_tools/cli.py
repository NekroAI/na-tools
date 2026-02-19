"""NA-Tools CLI 入口。"""

import click

from . import __version__
from .commands.backup import backup
from .commands.config_cmd import config
from .commands.install import install
from .commands.logs import logs
from .commands.restore import restore
from .commands.status import status
from .commands.update import update
from .commands.use import use


@click.group()
@click.version_option(version=__version__, prog_name="na-tools")
def main() -> None:
    """NA-Tools: Nekro Agent 部署管理工具

    支持一键安装、更新、备份、恢复和配置管理。
    """
    pass


main.add_command(install)
main.add_command(update)
main.add_command(backup)
main.add_command(restore)
main.add_command(config)
main.add_command(status)
main.add_command(logs)
main.add_command(use)


if __name__ == "__main__":
    main()
