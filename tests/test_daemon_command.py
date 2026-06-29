from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
