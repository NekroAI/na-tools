from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from na_tools.services.upgrade_service import (
    BINARY_ASSET_NAME,
    InstallationInfo,
    UpgradeService,
    UpgradeServiceError,
)


def _release(tag: str = "v1.4.0", *, include_binary: bool = True) -> dict[str, object]:
    assets: list[dict[str, str]] = []
    if include_binary:
        assets.append(
            {
                "name": BINARY_ASSET_NAME,
                "browser_download_url": "https://example.test/na-tools-linux-x86_64",
            }
        )
    return {"tag_name": tag, "assets": assets}


def _completed(
    cmd: list[str],
    *,
    stdout: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")


class UpgradeServiceTest(unittest.TestCase):
    def test_release_check_uses_configured_timeout(self) -> None:
        timeouts: list[float] = []

        class FakeResponse:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict[str, object]:
                return _release("v1.4.0")

        class FakeClient:
            def __init__(self, *, timeout: float, follow_redirects: bool) -> None:
                timeouts.append(timeout)
                self.follow_redirects = follow_redirects

            def __enter__(self) -> FakeClient:
                return self

            def __exit__(
                self,
                _exc_type: object,
                _exc: object,
                _traceback: object,
            ) -> None:
                return None

            def get(self, _url: str, *, headers: dict[str, str]) -> FakeResponse:
                self.assert_headers(headers)
                return FakeResponse()

            @staticmethod
            def assert_headers(headers: dict[str, str]) -> None:
                if headers != {"Accept": "application/vnd.github+json"}:
                    raise AssertionError(headers)

        service = UpgradeService(
            current_version="1.3.7",
            uv_tool_dir_getter=lambda: None,
            release_timeout=3.0,
        )

        with patch("na_tools.services.upgrade_service.httpx.Client", FakeClient):
            result = service.check()

        self.assertEqual(timeouts, [3.0])
        self.assertTrue(result.update_available)

    def test_check_reports_current_when_versions_match(self) -> None:
        service = UpgradeService(
            current_version="1.4.0",
            release_fetcher=lambda: _release("v1.4.0"),
            uv_tool_dir_getter=lambda: None,
        )

        result = service.check()

        self.assertEqual(result.current_version, "1.4.0")
        self.assertEqual(result.latest_version, "1.4.0")
        self.assertFalse(result.update_available)
        self.assertEqual(result.installation.method, "unsupported")

    def test_check_reports_update_available_for_older_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            uv_dir = Path(tmp) / "tools"
            executable = uv_dir / "na-tools" / "bin" / "python"
            service = UpgradeService(
                current_version="1.3.7",
                executable=executable,
                release_fetcher=lambda: _release("v1.4.0"),
                uv_tool_dir_getter=lambda: uv_dir,
            )

            result = service.check()

        self.assertTrue(result.update_available)
        self.assertEqual(result.installation.method, "uv_tool")
        self.assertEqual(
            result.binary_asset_url,
            "https://example.test/na-tools-linux-x86_64",
        )

    def test_detects_uv_tool_when_python_is_symlinked_outside_tool_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uv_dir = root / "tools"
            executable = uv_dir / "na-tools" / "bin" / "python"
            system_python = root / "usr" / "bin" / "python3.10"
            executable.parent.mkdir(parents=True)
            system_python.parent.mkdir(parents=True)
            system_python.touch()
            executable.symlink_to(system_python)
            service = UpgradeService(
                executable=executable,
                uv_tool_dir_getter=lambda: uv_dir,
            )

            installation = service.detect_installation()

        self.assertEqual(installation.method, "uv_tool")
        self.assertEqual(installation.executable, executable.absolute())

    def test_missing_release_tag_fails_structurally(self) -> None:
        service = UpgradeService(release_fetcher=lambda: {"assets": []})

        with self.assertRaises(UpgradeServiceError) as raised:
            service.check()

        self.assertEqual(raised.exception.code, "release_tag_missing")

    def test_invalid_release_version_fails_structurally(self) -> None:
        service = UpgradeService(release_fetcher=lambda: _release("latest"))

        with self.assertRaises(UpgradeServiceError) as raised:
            service.check()

        self.assertEqual(raised.exception.code, "invalid_version")

    def test_release_fetch_failure_is_reported(self) -> None:
        def fetch() -> dict[str, object]:
            raise UpgradeServiceError("release_fetch_failed", "boom")

        service = UpgradeService(release_fetcher=fetch)

        with self.assertRaises(UpgradeServiceError) as raised:
            service.check()

        self.assertEqual(raised.exception.code, "release_fetch_failed")

    def test_uv_tool_upgrade_runs_uv_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            uv_dir = Path(tmp) / "tools"
            executable = uv_dir / "na-tools" / "bin" / "python"
            calls: list[list[str]] = []

            def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
                calls.append(cmd)
                if cmd[0] == str(executable):
                    return _completed(cmd, stdout="1.4.0\n")
                return _completed(cmd)

            service = UpgradeService(
                current_version="1.3.7",
                executable=executable,
                release_fetcher=lambda: _release("v1.4.0"),
                uv_tool_dir_getter=lambda: uv_dir,
                uv_finder=lambda _name: "/usr/bin/uv",
                runner=runner,
            )

            result = service.upgrade(service.check())

        self.assertEqual(
            calls,
            [
                ["uv", "tool", "install", "--force", "na-tools==1.4.0"],
                [
                    str(executable),
                    "-c",
                    "from na_tools import __version__; print(__version__)",
                ],
            ],
        )
        self.assertEqual(result.method, "uv_tool")
        self.assertEqual(result.latest_version, "1.4.0")

    def test_uv_tool_upgrade_rejects_unchanged_installed_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            uv_dir = Path(tmp) / "tools"
            executable = uv_dir / "na-tools" / "bin" / "python"

            def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
                if cmd[0] == str(executable):
                    return _completed(cmd, stdout="1.3.7\n")
                return _completed(cmd)

            service = UpgradeService(
                current_version="1.3.7",
                executable=executable,
                release_fetcher=lambda: _release("v1.4.0"),
                uv_tool_dir_getter=lambda: uv_dir,
                uv_finder=lambda _name: "/usr/bin/uv",
                runner=runner,
            )

            with self.assertRaises(UpgradeServiceError) as raised:
                service.upgrade(service.check())

        self.assertEqual(raised.exception.code, "uv_version_mismatch")

    def test_binary_upgrade_downloads_smoke_checks_backs_up_and_replaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "na-tools"
            executable.write_text("old-binary", encoding="utf-8")
            calls: list[list[str]] = []
            downloads: list[tuple[str, Path]] = []

            def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
                calls.append(cmd)
                return _completed(cmd, stdout="na-tools, version 1.4.0\n")

            def downloader(url: str, output: Path) -> None:
                downloads.append((url, output))
                output.write_text("new-binary", encoding="utf-8")

            service = UpgradeService(
                current_version="1.3.7",
                executable=executable,
                frozen=True,
                release_fetcher=lambda: _release("v1.4.0"),
                runner=runner,
                downloader=downloader,
                platform_getter=lambda: "Linux",
                machine_getter=lambda: "x86_64",
                clock=lambda: 123.0,
            )

            result = service.upgrade(service.check())

            backup = Path(tmp) / "na-tools.bak-123"
            self.assertEqual(result.method, "binary")
            self.assertEqual(result.backup_path, backup)
            self.assertEqual(executable.read_text(encoding="utf-8"), "new-binary")
            self.assertEqual(backup.read_text(encoding="utf-8"), "old-binary")
            self.assertEqual(
                downloads,
                [("https://example.test/na-tools-linux-x86_64", downloads[0][1])],
            )
            self.assertEqual(downloads[0][1].parent.parent, executable.parent)
            self.assertEqual(calls[0][1], "--version")

    def test_binary_upgrade_rejects_invalid_or_mismatched_version(self) -> None:
        outputs = {
            "missing": "",
            "invalid": "unexpected output\n",
            "mismatch": "na-tools, version 1.3.9\n",
        }

        for label, output in outputs.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                executable = Path(tmp) / "na-tools"
                executable.write_text("old-binary", encoding="utf-8")

                def runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
                    return _completed(cmd, stdout=output)

                def downloader(_url: str, target: Path) -> None:
                    target.write_text("new-binary", encoding="utf-8")

                service = UpgradeService(
                    current_version="1.3.7",
                    executable=executable,
                    frozen=True,
                    release_fetcher=lambda: _release("v1.4.0"),
                    runner=runner,
                    downloader=downloader,
                    platform_getter=lambda: "Linux",
                    machine_getter=lambda: "x86_64",
                    clock=lambda: 123.0,
                )

                with self.assertRaises(UpgradeServiceError) as raised:
                    service.upgrade(service.check())

                self.assertEqual(raised.exception.code, "binary_version_mismatch")
                self.assertEqual(executable.read_text(encoding="utf-8"), "old-binary")
                self.assertEqual(list(Path(tmp).glob("na-tools.bak-*")), [])

    def test_binary_upgrade_requires_release_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "na-tools"
            executable.write_text("old-binary", encoding="utf-8")
            service = UpgradeService(
                current_version="1.3.7",
                executable=executable,
                frozen=True,
                release_fetcher=lambda: _release("v1.4.0", include_binary=False),
                platform_getter=lambda: "Linux",
                machine_getter=lambda: "x86_64",
            )

            with self.assertRaises(UpgradeServiceError) as raised:
                service.upgrade(service.check())

        self.assertEqual(raised.exception.code, "asset_missing")

    def test_unsupported_install_does_not_upgrade(self) -> None:
        service = UpgradeService(
            current_version="1.3.7",
            release_fetcher=lambda: _release("v1.4.0"),
            uv_tool_dir_getter=lambda: None,
        )

        with self.assertRaises(UpgradeServiceError) as raised:
            service.upgrade(service.check())

        self.assertEqual(raised.exception.code, "unsupported_install")

    def test_upgrade_returns_without_action_when_already_current(self) -> None:
        installation = InstallationInfo(
            method="unsupported",
            executable=Path("/tmp/na-tools"),
            detail="test",
        )
        service = UpgradeService(
            current_version="1.4.0",
            release_fetcher=lambda: _release("v1.4.0"),
            uv_tool_dir_getter=lambda: None,
        )
        check = service.check()
        self.assertEqual(check.installation.method, installation.method)

        result = service.upgrade(check)

        self.assertFalse(result.restart_required)


if __name__ == "__main__":
    unittest.main()
