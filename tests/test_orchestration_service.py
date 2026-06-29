from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from na_tools.services.orchestration_service import (
    OrchestrationRequest,
    OrchestrationService,
    OrchestrationServiceError,
)


class FakeDocker:
    def __init__(
        self,
        *,
        docker_installed: bool = True,
        compose_installed: bool = True,
        up_ok: bool = True,
        down_ok: bool = True,
    ) -> None:
        self.docker_installed = docker_installed
        self.compose_installed = compose_installed
        self.up_ok = up_ok
        self.down_ok = down_ok
        self.events: list[str] = []
        self.ups: list[tuple[Path, Path | None]] = []
        self.downs: list[tuple[Path, Path | None]] = []

    def up(self, cwd: Path, env_file: Path | None = None) -> bool:
        self.events.append("compose_up")
        self.ups.append((cwd, env_file))
        return self.up_ok

    def down(self, cwd: Path, env_file: Path | None = None) -> bool:
        self.events.append("compose_down")
        self.downs.append((cwd, env_file))
        return self.down_ok


class FakeDaemonManager:
    def __init__(self, events: list[str] | None = None, *, missing: bool = False) -> None:
        self.events = events if events is not None else []
        self.missing = missing
        self.starts: list[Path] = []
        self.stops: list[Path] = []

    def start_registered(self, data_dir: Path) -> object:
        if self.missing:
            from na_tools.services.daemon_service import DaemonServiceError

            raise DaemonServiceError("daemon_service_missing", "missing")
        self.events.append("daemon_start")
        self.starts.append(data_dir)
        return object()

    def stop_registered(self, data_dir: Path) -> object:
        if self.missing:
            from na_tools.services.daemon_service import DaemonServiceError

            raise DaemonServiceError("daemon_service_missing", "missing")
        self.events.append("daemon_stop")
        self.stops.append(data_dir)
        return object()


class OrchestrationServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name) / "nekro_agent"
        self.data_dir.mkdir()
        self.compose_path = self.data_dir / "docker-compose.yml"
        self.env_path = self.data_dir / ".env"
        self.compose_path.write_text("services: {}\n", encoding="utf-8")
        self.env_path.write_text("NEKRO_EXPOSE_PORT=8021\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_start_runs_compose_up_with_env_file(self) -> None:
        docker = FakeDocker()
        service = OrchestrationService(docker_factory=lambda: docker)

        result = service.run(
            OrchestrationRequest(
                data_dir=self.data_dir,
                action="start",
                with_daemon=False,
            )
        )

        self.assertEqual(result.action, "start")
        self.assertEqual(result.command, "docker compose up -d")
        self.assertEqual(docker.ups, [(self.data_dir.resolve(), self.env_path)])
        self.assertEqual(docker.downs, [])

    def test_stop_runs_compose_down_with_env_file(self) -> None:
        docker = FakeDocker()
        service = OrchestrationService(docker_factory=lambda: docker)

        result = service.run(
            OrchestrationRequest(
                data_dir=self.data_dir,
                action="stop",
                with_daemon=False,
            )
        )

        self.assertEqual(result.action, "stop")
        self.assertEqual(result.command, "docker compose down")
        self.assertEqual(docker.downs, [(self.data_dir.resolve(), self.env_path)])
        self.assertEqual(docker.ups, [])

    def test_missing_compose_fails_before_docker_call(self) -> None:
        self.compose_path.unlink()
        docker = FakeDocker()
        service = OrchestrationService(docker_factory=lambda: docker)

        with self.assertRaises(OrchestrationServiceError) as raised:
            service.run(
                OrchestrationRequest(
                    data_dir=self.data_dir,
                    action="start",
                    with_daemon=False,
                )
            )

        self.assertEqual(raised.exception.code, "compose_missing")
        self.assertEqual(docker.ups, [])

    def test_docker_or_compose_unavailable_fails_structurally(self) -> None:
        service = OrchestrationService(
            docker_factory=lambda: FakeDocker(compose_installed=False)
        )

        with self.assertRaises(OrchestrationServiceError) as raised:
            service.run(
                OrchestrationRequest(
                    data_dir=self.data_dir,
                    action="stop",
                    with_daemon=False,
                )
            )

        self.assertEqual(raised.exception.code, "docker_unavailable")

    def test_compose_failure_uses_action_specific_code(self) -> None:
        service = OrchestrationService(docker_factory=lambda: FakeDocker(up_ok=False))

        with self.assertRaises(OrchestrationServiceError) as raised:
            service.run(
                OrchestrationRequest(
                    data_dir=self.data_dir,
                    action="start",
                    with_daemon=False,
                )
            )

        self.assertEqual(raised.exception.code, "start_failed")

    def test_start_runs_compose_then_registered_daemon(self) -> None:
        docker = FakeDocker()
        daemon = FakeDaemonManager(docker.events)
        service = OrchestrationService(
            docker_factory=lambda: docker,
            daemon_service_manager=daemon,
        )

        result = service.run(
            OrchestrationRequest(data_dir=self.data_dir, action="start")
        )

        self.assertEqual(docker.events, ["compose_up", "daemon_start"])
        self.assertIsNotNone(result.daemon_service)
        self.assertEqual(daemon.starts, [self.data_dir.resolve()])

    def test_stop_runs_registered_daemon_then_compose(self) -> None:
        docker = FakeDocker()
        daemon = FakeDaemonManager(docker.events)
        service = OrchestrationService(
            docker_factory=lambda: docker,
            daemon_service_manager=daemon,
        )

        result = service.run(OrchestrationRequest(data_dir=self.data_dir, action="stop"))

        self.assertEqual(docker.events, ["daemon_stop", "compose_down"])
        self.assertIsNotNone(result.daemon_service)
        self.assertEqual(daemon.stops, [self.data_dir.resolve()])

    def test_start_missing_registered_daemon_returns_structured_error(self) -> None:
        docker = FakeDocker()
        daemon = FakeDaemonManager(docker.events, missing=True)
        service = OrchestrationService(
            docker_factory=lambda: docker,
            daemon_service_manager=daemon,
        )

        with self.assertRaises(OrchestrationServiceError) as raised:
            service.run(OrchestrationRequest(data_dir=self.data_dir, action="start"))

        self.assertEqual(raised.exception.code, "daemon_service_missing")
        self.assertEqual(docker.events, ["compose_up"])

    def test_cli_registers_start_and_stop_commands(self) -> None:
        from na_tools.cli import main

        self.assertIn("start", main.commands)
        self.assertIn("stop", main.commands)


if __name__ == "__main__":
    unittest.main()
