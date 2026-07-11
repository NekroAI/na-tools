"""upgrade command: update na-tools itself."""

from __future__ import annotations

import click

from ..services.upgrade_service import (
    UpgradeCheckResult,
    UpgradeResult,
    UpgradeService,
    UpgradeServiceError,
)
from ..utils.console import confirm, error, info, success, warning


@click.command()
@click.option("--check", "check_only", is_flag=True, help="只检测是否有新版本")
@click.option("-y", "--yes", is_flag=True, help="跳过确认并执行自动更新")
def upgrade(check_only: bool, yes: bool) -> None:
    """检测并更新 na-tools 自身。"""

    service = UpgradeService()
    try:
        check_result = service.check()
    except UpgradeServiceError as exc:
        error(exc.message)
        raise click.Abort() from exc

    _print_check_result(check_result)

    if check_only:
        return

    if not check_result.update_available:
        success("na-tools 已是最新版本。")
        return

    if check_result.installation.method == "unsupported":
        warning("当前安装方式暂不支持自动更新。")
        info("请使用 `uv tool install --force na-tools`，或手动下载最新二进制。")
        raise click.Abort()

    if not yes and not confirm(
        f"是否将 na-tools 从 {check_result.current_version} 更新到 {check_result.latest_version}？",
        default=True,
    ):
        raise click.Abort()

    try:
        result = service.upgrade(check_result)
    except UpgradeServiceError as exc:
        error(exc.message)
        _print_error_hint(exc)
        raise click.Abort() from exc

    _print_upgrade_result(result)


def _print_check_result(result: UpgradeCheckResult) -> None:
    info(f"当前版本: {result.current_version}")
    info(f"最新版本: {result.latest_version} ({result.latest_tag})")
    info(f"安装方式: {_method_label(result.installation.method)}")
    info(f"可执行文件: {result.installation.executable}")
    if result.update_available:
        warning("发现新版本可更新。")
    else:
        success("当前已是最新版本。")


def _print_upgrade_result(result: UpgradeResult) -> None:
    if not result.restart_required:
        success("na-tools 已是最新版本。")
        return
    success(f"na-tools 已更新到 {result.latest_version}。")
    if result.backup_path is not None:
        info(f"旧二进制已备份到: {result.backup_path}")
    info("当前进程仍使用旧版本；下次运行 na-tools 时新版本生效。")


def _print_error_hint(exc: UpgradeServiceError) -> None:
    if exc.code == "uv_missing":
        info("请先安装 uv，或改用二进制发布包。")
    elif exc.code == "uv_version_mismatch":
        info("请检查 uv 使用的软件源，并重新运行 `na-tools upgrade`。")
    elif exc.code == "asset_missing":
        info("请到 GitHub Release 页面手动确认二进制资源是否已发布。")
    elif exc.code == "binary_platform_unsupported":
        info("当前二进制自动更新仅覆盖 Linux x86_64。")
    elif exc.code == "unsupported_install":
        info("请使用 `uv tool install --force na-tools`，或手动下载最新二进制。")


def _method_label(method: str) -> str:
    if method == "uv_tool":
        return "uv tool"
    if method == "binary":
        return "二进制"
    return "不支持自动更新"
