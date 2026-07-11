from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from na_tools import __version__
from na_tools.cli import PASSIVE_UPGRADE_CHECK_TIMEOUT, main
from na_tools.services.daemon_service import DaemonStatus
from na_tools.services.instance_service import InstanceServiceError
from na_tools.services.upgrade_service import (
    InstallationInfo,
    UpgradeCheckResult,
    UpgradeServiceError,
)


@pytest.fixture(autouse=True)
def _disable_passive_cache(monkeypatch: Any) -> None:
    monkeypatch.setattr("na_tools.cli._load_cached_latest_version", lambda: None)
    monkeypatch.setattr("na_tools.cli._save_cached_latest_version", lambda _version: None)


def _check_result(*, update_available: bool = True) -> UpgradeCheckResult:
    return UpgradeCheckResult(
        current_version="1.3.7",
        latest_version="1.4.0",
        latest_tag="v1.4.0",
        update_available=update_available,
        installation=InstallationInfo(
            method="uv_tool",
            executable=Path("/tmp/na-tools"),
            detail="test",
        ),
    )


def _register_probe(monkeypatch: Any, callback: Callable[[], None]) -> None:
    command = click.command("probe")(callback)
    monkeypatch.setitem(main.commands, "probe", command)


def test_successful_command_checks_once_and_prints_notice(monkeypatch: Any) -> None:
    service_timeouts: list[float] = []

    class FakeService:
        def __init__(self, *, release_timeout: float) -> None:
            service_timeouts.append(release_timeout)

        def check(self) -> UpgradeCheckResult:
            return _check_result()

    def probe() -> None:
        click.echo("command output")

    _register_probe(monkeypatch, probe)
    runner = CliRunner()
    with patch("na_tools.cli.UpgradeService", FakeService):
        result = runner.invoke(main, ["probe"])

    assert result.exit_code == 0, result.output
    assert service_timeouts == [PASSIVE_UPGRADE_CHECK_TIMEOUT]
    assert result.output.index("command output") < result.output.index(
        "发现 na-tools 新版本"
    )
    assert "运行 na-tools upgrade 更新" in result.output
    assert result.stdout.strip() == "command output"
    assert "发现 na-tools 新版本" in result.stderr


def test_fresh_cache_skips_network_check(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "na_tools.cli._load_cached_latest_version",
        lambda: "1.4.0",
    )

    def probe() -> None:
        click.echo("command output")

    _register_probe(monkeypatch, probe)
    with patch("na_tools.cli.UpgradeService") as service:
        result = CliRunner().invoke(main, ["probe"])

    assert result.exit_code == 0, result.output
    service.assert_not_called()
    assert result.stdout.strip() == "command output"
    assert "发现 na-tools 新版本" in result.stderr


def test_daemon_status_json_keeps_notice_out_of_stdout(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "na_tools.cli._load_cached_latest_version",
        lambda: "1.4.0",
    )
    status = DaemonStatus(
        data_dir=Path("/tmp/instance"),
        daemon_json=Path("/tmp/instance/.na-tools/daemon.json"),
        payload={"instance_id": "test-instance"},
        token_file=None,
    )

    with patch(
        "na_tools.services.daemon_service.DaemonService.status",
        return_value=status,
    ):
        result = CliRunner().invoke(main, ["daemon", "status", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"instance_id": "test-instance"}
    assert "发现 na-tools 新版本" in result.stderr


def test_current_version_does_not_print_notice(monkeypatch: Any) -> None:
    class FakeService:
        def __init__(self, *, release_timeout: float) -> None:
            assert release_timeout == PASSIVE_UPGRADE_CHECK_TIMEOUT

        def check(self) -> UpgradeCheckResult:
            return _check_result(update_available=False)

    def probe() -> None:
        click.echo("command output")

    _register_probe(monkeypatch, probe)
    with patch("na_tools.cli.UpgradeService", FakeService):
        result = CliRunner().invoke(main, ["probe"])

    assert result.exit_code == 0, result.output
    assert "command output" in result.output
    assert "na-tools 新版本" not in result.output


def test_check_failure_is_silent(monkeypatch: Any) -> None:
    cached_versions: list[str] = []

    class FakeService:
        def __init__(self, *, release_timeout: float) -> None:
            assert release_timeout == PASSIVE_UPGRADE_CHECK_TIMEOUT

        def check(self) -> UpgradeCheckResult:
            raise UpgradeServiceError("release_fetch_failed", "network failed")

    def probe() -> None:
        click.echo("command output")

    _register_probe(monkeypatch, probe)
    monkeypatch.setattr(
        "na_tools.cli._save_cached_latest_version",
        cached_versions.append,
    )
    with patch("na_tools.cli.UpgradeService", FakeService):
        result = CliRunner().invoke(main, ["probe"])

    assert result.exit_code == 0, result.output
    assert result.output.strip() == "command output"
    assert cached_versions == [__version__]


def test_failed_command_does_not_check(monkeypatch: Any) -> None:
    def probe() -> None:
        raise click.Abort()

    _register_probe(monkeypatch, probe)
    with patch("na_tools.cli._notify_upgrade_available") as notify:
        result = CliRunner().invoke(main, ["probe"])

    assert result.exit_code != 0
    notify.assert_not_called()


@pytest.mark.parametrize(
    ("arguments", "service_method"),
    [
        (["status"], "status"),
        (["logs"], "logs"),
        (["use", "/tmp/missing"], "use"),
    ],
)
def test_instance_command_service_errors_do_not_check(
    arguments: list[str],
    service_method: str,
) -> None:
    with (
        patch(
            f"na_tools.services.instance_service.InstanceService.{service_method}",
            side_effect=InstanceServiceError("missing", "实例不存在"),
        ),
        patch("na_tools.cli._notify_upgrade_available") as notify,
    ):
        result = CliRunner().invoke(main, arguments)

    assert result.exit_code != 0
    assert "实例不存在" in result.output
    notify.assert_not_called()


def test_upgrade_does_not_run_passive_check() -> None:
    class FakeUpgradeService:
        def check(self) -> UpgradeCheckResult:
            return _check_result(update_available=False)

    with (
        patch("na_tools.commands.upgrade.UpgradeService", FakeUpgradeService),
        patch("na_tools.cli._notify_upgrade_available") as notify,
    ):
        result = CliRunner().invoke(main, ["upgrade", "--check"])

    assert result.exit_code == 0, result.output
    notify.assert_not_called()


def test_version_prints_before_update_notice() -> None:
    class FakeService:
        def __init__(self, *, release_timeout: float) -> None:
            assert release_timeout == PASSIVE_UPGRADE_CHECK_TIMEOUT

        def check(self) -> UpgradeCheckResult:
            return _check_result()

    with patch("na_tools.cli.UpgradeService", FakeService):
        result = CliRunner().invoke(main, ["--version"])

    assert result.exit_code == 0, result.output
    version_output = f"na-tools, version {__version__}"
    assert result.output.index(version_output) < result.output.index("发现 na-tools 新版本")


def test_help_and_missing_command_do_not_check() -> None:
    runner = CliRunner()
    with patch("na_tools.cli._notify_upgrade_available") as notify:
        help_result = runner.invoke(main, ["--help"])
        missing_result = runner.invoke(main, [])

    assert help_result.exit_code == 0, help_result.output
    assert missing_result.exit_code != 0
    notify.assert_not_called()
