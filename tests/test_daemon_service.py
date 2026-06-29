from __future__ import annotations

import hashlib
import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from na_tools.services.daemon_service import DaemonRootServiceManager, DaemonServiceError


class DaemonRootServiceManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "nekro_agent"
        self.meta_dir = self.data_dir / ".na-tools"
        self.meta_dir.mkdir(parents=True)
        self.instance_id = "sha256:" + "a" * 64
        (self.meta_dir / "daemon.json").write_text(
            json.dumps({"instance_id": self.instance_id}),
            encoding="utf-8",
        )
        self.calls: list[list[str]] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_linux_install_writes_systemd_unit_and_starts_service(self) -> None:
        manager = DaemonRootServiceManager(
            systemd_dir=self.root / "systemd",
            runner=self._record_run,
            platform_getter=lambda: "linux",
            executable="/opt/na-tools/python",
            root_checker=lambda: True,
        )

        result = manager.install_and_start(self.data_dir)

        suffix = hashlib.sha256(self.instance_id.encode("utf-8")).hexdigest()[:12]
        self.assertEqual(result.service_name, f"na-tools-daemon-{suffix}.service")
        unit = result.service_path.read_text(encoding="utf-8")
        self.assertIn("ExecStart=/opt/na-tools/python -m na_tools daemon start", unit)
        self.assertIn(f"--data-dir {self.data_dir.resolve()}", unit)
        self.assertIn("Environment=NA_TOOLS_DAEMON_MODE=1", unit)
        self.assertIn(
            f"StandardOutput=append:{self.meta_dir / 'daemon.out.log'}",
            unit,
        )
        self.assertEqual(
            self.calls,
            [
                ["systemctl", "daemon-reload"],
                ["systemctl", "enable", result.service_name],
                ["systemctl", "start", result.service_name],
            ],
        )

    def test_macos_install_writes_launch_daemon_and_starts_service(self) -> None:
        ownership: list[tuple[Path, int, int]] = []
        modes: list[tuple[Path, int]] = []
        manager = DaemonRootServiceManager(
            launchd_dir=self.root / "launchd",
            runner=self._record_run,
            platform_getter=lambda: "darwin",
            executable="/opt/na-tools/python",
            chown=lambda path, uid, gid: ownership.append((path, uid, gid)),
            chmod=lambda path, mode: modes.append((path, mode)),
            root_checker=lambda: True,
        )

        result = manager.install_and_start(self.data_dir)

        plist = plistlib.loads(result.service_path.read_bytes())
        label = result.service_path.stem
        self.assertEqual(plist["Label"], label)
        self.assertEqual(
            plist["ProgramArguments"],
            [
                "/opt/na-tools/python",
                "-m",
                "na_tools",
                "daemon",
                "start",
                "--data-dir",
                str(self.data_dir.resolve()),
            ],
        )
        self.assertEqual(ownership, [(result.service_path, 0, 0)])
        self.assertEqual(modes, [(result.service_path, 0o644)])
        self.assertEqual(
            self.calls,
            [
                ["launchctl", "bootstrap", "system", str(result.service_path)],
                ["launchctl", "kickstart", "-k", f"system/{label}"],
            ],
        )

    def test_start_registered_does_not_write_or_enable_service(self) -> None:
        manager = DaemonRootServiceManager(
            systemd_dir=self.root / "systemd",
            runner=self._record_run,
            platform_getter=lambda: "linux",
            root_checker=lambda: True,
        )
        installed = manager.install_and_start(self.data_dir)
        self.calls.clear()

        result = manager.start_registered(self.data_dir)

        self.assertEqual(result.service_path, installed.service_path)
        self.assertEqual(self.calls, [["systemctl", "start", installed.service_name]])

    def test_start_registered_missing_service_fails(self) -> None:
        manager = DaemonRootServiceManager(
            systemd_dir=self.root / "systemd",
            runner=self._record_run,
            platform_getter=lambda: "linux",
            root_checker=lambda: True,
        )

        with self.assertRaises(DaemonServiceError) as raised:
            manager.start_registered(self.data_dir)

        self.assertEqual(raised.exception.code, "daemon_service_missing")
        self.assertEqual(self.calls, [])

    def test_linux_uninstall_stops_disables_removes_and_reloads(self) -> None:
        manager = DaemonRootServiceManager(
            systemd_dir=self.root / "systemd",
            runner=self._record_run,
            platform_getter=lambda: "linux",
            root_checker=lambda: True,
        )
        installed = manager.install_and_start(self.data_dir)
        self.assertTrue(installed.service_path.exists())
        self.calls.clear()

        result = manager.uninstall_registered(self.data_dir)

        self.assertEqual(result.action, "uninstall")
        self.assertFalse(installed.service_path.exists())
        self.assertEqual(
            self.calls,
            [
                ["systemctl", "stop", installed.service_name],
                ["systemctl", "disable", installed.service_name],
                ["systemctl", "daemon-reload"],
            ],
        )

    def test_uninstall_missing_service_fails_before_root_check(self) -> None:
        manager = DaemonRootServiceManager(
            systemd_dir=self.root / "systemd",
            runner=self._record_run,
            platform_getter=lambda: "linux",
            root_checker=lambda: False,
        )

        with self.assertRaises(DaemonServiceError) as raised:
            manager.uninstall_registered(self.data_dir)

        self.assertEqual(raised.exception.code, "daemon_service_missing")
        self.assertEqual(self.calls, [])

    def _record_run(self, cmd: list[str], **_kwargs: object) -> object:
        self.calls.append(cmd)
        return object()


if __name__ == "__main__":
    unittest.main()
