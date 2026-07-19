from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from click.testing import CliRunner

from na_tools.commands.daemon import daemon


class DaemonStartCommandTest(unittest.TestCase):
    def test_start_aborts_before_uvicorn_when_http_port_is_in_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = CliRunner()

            with (
                patch("na_tools.commands.daemon.socket.socket") as socket_factory,
                patch("uvicorn.run") as run,
            ):
                socket_factory.return_value = _FakeBindSocket(fail=True)
                result = runner.invoke(
                    daemon,
                    [
                        "start",
                        "--data-dir",
                        str(Path(tmp) / "nekro_agent"),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "18081",
                        "--socks-host",
                        "127.0.0.1",
                        "--socks-port",
                        "0",
                    ],
                )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("HTTP API", result.output)
        self.assertIn("已经启动", result.output)
        run.assert_not_called()

    def test_start_aborts_before_uvicorn_when_socks_port_is_in_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runner = CliRunner()

            with (
                patch("na_tools.commands.daemon.socket.socket") as socket_factory,
                patch("uvicorn.run") as run,
            ):
                socket_factory.side_effect = [
                    _FakeBindSocket(fail=False),
                    _FakeBindSocket(fail=True),
                ]
                result = runner.invoke(
                    daemon,
                    [
                        "start",
                        "--data-dir",
                        str(Path(tmp) / "nekro_agent"),
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "0",
                        "--socks-host",
                        "127.0.0.1",
                        "--socks-port",
                        "18082",
                    ],
                )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("SOCKS5", result.output)
        self.assertIn("已经启动", result.output)
        run.assert_not_called()


class DaemonRegisterCommandTest(unittest.TestCase):
    def test_register_installs_and_starts_root_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "nekro_agent"
            service_path = Path(tmp) / "na-tools-daemon-test.service"
            manager = Mock()
            manager.run.return_value = SimpleNamespace(
                daemon_channel=SimpleNamespace(
                    instance_id="sha256:test",
                    env_updated_keys=("NA_TOOLS_DAEMON_ENABLED",),
                    compose_updated=True,
                ),
                daemon_service=SimpleNamespace(
                    service_name=service_path.name,
                    service_path=service_path,
                ),
                container_recreated=True,
            )

            with patch(
                "na_tools.services.daemon_service.DaemonRegistrationService",
                return_value=manager,
            ):
                result = CliRunner().invoke(
                    daemon,
                    ["register", "--data-dir", str(data_dir)],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        manager.run.assert_called_once_with(data_dir.resolve())
        self.assertIn("已注册并启动", result.output)
        self.assertIn(service_path.name, result.output)
        self.assertIn(str(service_path), result.output)
        self.assertIn("环境变量", result.output)
        self.assertIn("host gateway", result.output)
        self.assertIn("容器已重建", result.output)


class _FakeBindSocket:
    def __init__(self, *, fail: bool) -> None:
        self.fail = fail

    def __enter__(self) -> _FakeBindSocket:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def bind(self, _address: tuple[str, int]) -> None:
        if self.fail:
            raise OSError(98, "Address already in use")


if __name__ == "__main__":
    unittest.main()
