from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from na_tools.commands.upgrade import upgrade
from na_tools.services.upgrade_service import (
    InstallationInfo,
    UpgradeCheckResult,
    UpgradeResult,
)


def _check_result(
    *,
    update_available: bool = True,
    method: str = "uv_tool",
) -> UpgradeCheckResult:
    return UpgradeCheckResult(
        current_version="1.3.7",
        latest_version="1.4.0",
        latest_tag="v1.4.0",
        update_available=update_available,
        installation=InstallationInfo(
            method=method,  # type: ignore[arg-type]
            executable=Path("/tmp/na-tools"),
            detail="test",
        ),
        binary_asset_url="https://example.test/na-tools-linux-x86_64",
    )


def test_upgrade_check_only_does_not_run_upgrade() -> None:
    calls: list[UpgradeCheckResult] = []

    class FakeService:
        def check(self) -> UpgradeCheckResult:
            return _check_result()

        def upgrade(self, result: UpgradeCheckResult) -> UpgradeResult:
            calls.append(result)
            return UpgradeResult("uv_tool", "1.3.7", "1.4.0")

    runner = CliRunner()
    with patch("na_tools.commands.upgrade.UpgradeService", FakeService):
        result = runner.invoke(upgrade, ["--check"])

    assert result.exit_code == 0, result.output
    assert calls == []
    assert "当前版本" in result.output
    assert "发现新版本可更新" in result.output


def test_upgrade_prompts_and_runs_when_confirmed() -> None:
    calls: list[UpgradeCheckResult] = []

    class FakeService:
        def check(self) -> UpgradeCheckResult:
            return _check_result()

        def upgrade(self, result: UpgradeCheckResult) -> UpgradeResult:
            calls.append(result)
            return UpgradeResult("uv_tool", "1.3.7", "1.4.0")

    runner = CliRunner()
    with patch("na_tools.commands.upgrade.UpgradeService", FakeService):
        result = runner.invoke(upgrade, input="y\n")

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert "是否将 na-tools" in result.output
    assert "na-tools 已更新到 1.4.0" in result.output


def test_upgrade_yes_skips_confirmation() -> None:
    calls: list[UpgradeCheckResult] = []

    class FakeService:
        def check(self) -> UpgradeCheckResult:
            return _check_result()

        def upgrade(self, result: UpgradeCheckResult) -> UpgradeResult:
            calls.append(result)
            return UpgradeResult("uv_tool", "1.3.7", "1.4.0")

    runner = CliRunner()
    with patch("na_tools.commands.upgrade.UpgradeService", FakeService):
        result = runner.invoke(upgrade, ["--yes"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert "是否将 na-tools" not in result.output


def test_upgrade_does_not_run_when_already_current() -> None:
    calls: list[UpgradeCheckResult] = []

    class FakeService:
        def check(self) -> UpgradeCheckResult:
            return _check_result(update_available=False)

        def upgrade(self, result: UpgradeCheckResult) -> UpgradeResult:
            calls.append(result)
            return UpgradeResult("uv_tool", "1.4.0", "1.4.0")

    runner = CliRunner()
    with patch("na_tools.commands.upgrade.UpgradeService", FakeService):
        result = runner.invoke(upgrade)

    assert result.exit_code == 0, result.output
    assert calls == []
    assert "na-tools 已是最新版本" in result.output


def test_upgrade_unsupported_install_aborts_without_running_upgrade() -> None:
    calls: list[UpgradeCheckResult] = []

    class FakeService:
        def check(self) -> UpgradeCheckResult:
            return _check_result(method="unsupported")

        def upgrade(self, result: UpgradeCheckResult) -> UpgradeResult:
            calls.append(result)
            return UpgradeResult("unsupported", "1.3.7", "1.4.0")

    runner = CliRunner()
    with patch("na_tools.commands.upgrade.UpgradeService", FakeService):
        result = runner.invoke(upgrade, ["--yes"])

    assert result.exit_code != 0
    assert calls == []
    assert "暂不支持自动更新" in result.output
