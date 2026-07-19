from __future__ import annotations

import hashlib
import json
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import yaml

from na_tools.core.config import load_env
from na_tools.daemon import (
    CONTAINER_DAEMON_TOKEN_FILE,
    DEFAULT_DAEMON_API_BASE,
    DEFAULT_DAEMON_SOCKS_URL,
)
from na_tools.services.daemon_service import (
    DaemonRegistrationService,
    DaemonRootServiceManager,
    DaemonRootServiceResult,
    DaemonServiceError,
)


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

    def test_install_reuses_legacy_path_hashed_service(self) -> None:
        manager = DaemonRootServiceManager(
            systemd_dir=self.root / "systemd",
            runner=self._record_run,
            platform_getter=lambda: "linux",
            executable="/opt/na-tools/python",
            root_checker=lambda: True,
        )
        legacy_name, legacy_path = self._legacy_linux_identity(manager)
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_text("old unit\n", encoding="utf-8")

        result = manager.install_and_start(self.data_dir)

        self.assertEqual(result.service_name, legacy_name)
        self.assertEqual(result.service_path, legacy_path)
        self.assertIn("ExecStart=/opt/na-tools/python", legacy_path.read_text(encoding="utf-8"))
        current_name, current_path = manager._service_identity(
            self.data_dir.resolve(),
            "linux",
        )
        self.assertNotEqual(current_name, legacy_name)
        self.assertFalse(current_path.exists())
        self.assertEqual(
            self.calls,
            [
                ["systemctl", "daemon-reload"],
                ["systemctl", "enable", legacy_name],
                ["systemctl", "start", legacy_name],
            ],
        )

    def test_start_stop_and_uninstall_resolve_legacy_service(self) -> None:
        manager = DaemonRootServiceManager(
            systemd_dir=self.root / "systemd",
            runner=self._record_run,
            platform_getter=lambda: "linux",
            root_checker=lambda: True,
        )
        legacy_name, legacy_path = self._legacy_linux_identity(manager)
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_text("old unit\n", encoding="utf-8")

        started = manager.start_registered(self.data_dir)
        stopped = manager.stop_registered(self.data_dir)
        removed = manager.uninstall_registered(self.data_dir)

        self.assertEqual(started.service_name, legacy_name)
        self.assertEqual(stopped.service_name, legacy_name)
        self.assertEqual(removed.service_name, legacy_name)
        self.assertFalse(legacy_path.exists())
        self.assertEqual(
            self.calls,
            [
                ["systemctl", "start", legacy_name],
                ["systemctl", "stop", legacy_name],
                ["systemctl", "stop", legacy_name],
                ["systemctl", "disable", legacy_name],
                ["systemctl", "daemon-reload"],
            ],
        )

    def test_current_and_legacy_service_conflict_fails_safely(self) -> None:
        manager = DaemonRootServiceManager(
            systemd_dir=self.root / "systemd",
            runner=self._record_run,
            platform_getter=lambda: "linux",
            root_checker=lambda: True,
        )
        current_name, current_path = manager._service_identity(
            self.data_dir.resolve(),
            "linux",
        )
        legacy_name, legacy_path = self._legacy_linux_identity(manager)
        current_path.parent.mkdir(parents=True)
        current_path.write_text("current unit\n", encoding="utf-8")
        legacy_path.write_text("legacy unit\n", encoding="utf-8")

        with self.assertRaises(DaemonServiceError) as raised:
            manager.install_and_start(self.data_dir)

        self.assertEqual(raised.exception.code, "daemon_service_conflict")
        self.assertEqual(
            raised.exception.details["service_names"],
            [current_name, legacy_name],
        )
        self.assertEqual(self.calls, [])

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

    def _legacy_linux_identity(
        self,
        manager: DaemonRootServiceManager,
    ) -> tuple[str, Path]:
        suffix = hashlib.sha256(
            str(self.data_dir.resolve()).encode("utf-8")
        ).hexdigest()[:12]
        name = f"na-tools-daemon-{suffix}.service"
        return name, manager.systemd_dir / name


class FakeDocker:
    def __init__(self, *, up_ok: bool = True) -> None:
        self.docker_installed = True
        self.compose_installed = True
        self.up_ok = up_ok
        self.ups: list[tuple[Path, Path | None]] = []

    def up(self, cwd: Path, env_file: Path | None = None) -> bool:
        self.ups.append((cwd, env_file))
        return self.up_ok


class DaemonRegistrationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "nekro_agent"
        self.data_dir.mkdir()
        self.env_path = self.data_dir / ".env"
        self.compose_path = self.data_dir / "docker-compose.yml"
        self.env_path.write_text(
            "\n".join(
                [
                    "UNRELATED=value",
                    "NA_TOOLS_DAEMON_ENABLED=false",
                    "NA_TOOLS_DAEMON_API_BASE=http://wrong.invalid/v1",
                    "NA_TOOLS_DAEMON_SOCKS=socks5h://wrong.invalid:1",
                    "NA_TOOLS_DAEMON_INSTANCE_ID=stale",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.compose_path.write_text(
            "services:\n  nekro_agent:\n    image: kromiose/nekro-agent:latest\n",
            encoding="utf-8",
        )
        self.meta_dir = self.data_dir / ".na-tools"
        self.meta_dir.mkdir()
        self.token_file = self.meta_dir / "daemon.token"
        self.token_file.write_text("existing-token\n", encoding="utf-8")
        self.token_file.chmod(0o644)
        self.docker = FakeDocker()
        self.manager = Mock()
        self.manager.install_and_start.return_value = DaemonRootServiceResult(
            data_dir=self.data_dir.resolve(),
            service_name="na-tools-daemon-test.service",
            service_path=Path("/etc/systemd/system/na-tools-daemon-test.service"),
            action="install_start",
            command="systemctl start",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_register_prepares_channel_and_recreates_changed_container(self) -> None:
        result = self._service().run(self.data_dir)

        env = load_env(self.env_path)
        self.assertEqual(env["UNRELATED"], "value")
        self.assertEqual(env["NA_TOOLS_DAEMON_ENABLED"], "true")
        self.assertEqual(env["NA_TOOLS_DAEMON_API_BASE"], DEFAULT_DAEMON_API_BASE)
        self.assertEqual(env["NA_TOOLS_DAEMON_SOCKS"], DEFAULT_DAEMON_SOCKS_URL)
        self.assertEqual(
            env["NA_TOOLS_DAEMON_INSTANCE_ID"],
            result.daemon_channel.instance_id,
        )
        self.assertNotIn("NA_TOOLS_DAEMON_TOKEN", env)
        self.assertNotIn("NA_TOOLS_DAEMON_TOKEN_FILE", env)
        self.assertEqual(self.token_file.read_text(encoding="utf-8"), "existing-token\n")
        self.assertEqual(self.token_file.stat().st_mode & 0o777, 0o600)

        compose = yaml.safe_load(self.compose_path.read_text(encoding="utf-8"))
        agent = compose["services"]["nekro_agent"]
        environment = agent["environment"]
        self.assertIn(
            f"NA_TOOLS_DAEMON_TOKEN_FILE={CONTAINER_DAEMON_TOKEN_FILE}",
            environment,
        )
        self.assertIn("host.docker.internal:host-gateway", agent["extra_hosts"])
        self.manager.install_and_start.assert_called_once_with(self.data_dir.resolve())
        self.assertEqual(
            self.docker.ups,
            [(self.data_dir.resolve(), self.env_path)],
        )
        self.assertTrue(result.container_recreated)

    def test_idempotent_register_does_not_recreate_container(self) -> None:
        service = self._service()
        service.run(self.data_dir)
        self.docker.ups.clear()
        self.manager.reset_mock()

        result = service.run(self.data_dir)

        self.manager.install_and_start.assert_called_once_with(self.data_dir.resolve())
        self.assertEqual(self.docker.ups, [])
        self.assertFalse(result.container_recreated)

    def test_unsafe_compose_aborts_before_service_registration(self) -> None:
        self.compose_path.write_text(
            "services:\n  nekro_agent:\n    environment: invalid\n",
            encoding="utf-8",
        )

        with self.assertRaises(DaemonServiceError) as raised:
            self._service().run(self.data_dir)

        self.assertEqual(raised.exception.code, "daemon_channel_compose_failed")
        self.manager.install_and_start.assert_not_called()
        self.assertEqual(self.docker.ups, [])

    def test_malformed_compose_aborts_before_service_registration(self) -> None:
        self.compose_path.write_text("services: [unterminated\n", encoding="utf-8")

        with self.assertRaises(DaemonServiceError) as raised:
            self._service().run(self.data_dir)

        self.assertEqual(raised.exception.code, "daemon_channel_compose_failed")
        self.manager.install_and_start.assert_not_called()
        self.assertEqual(self.docker.ups, [])

    def test_docker_failure_reports_registered_but_not_applied(self) -> None:
        self.docker.up_ok = False

        with self.assertRaises(DaemonServiceError) as raised:
            self._service().run(self.data_dir)

        self.assertEqual(raised.exception.code, "daemon_container_restart_failed")
        self.assertIn("daemon 已注册", raised.exception.message)
        self.manager.install_and_start.assert_called_once_with(self.data_dir.resolve())

    def _service(self) -> DaemonRegistrationService:
        return DaemonRegistrationService(
            docker_factory=lambda: self.docker,
            daemon_service_manager=self.manager,
        )


if __name__ == "__main__":
    unittest.main()
