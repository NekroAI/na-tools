from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from na_tools.services.job_events import UpdateEvent
from na_tools.services.update_service import (
    BackupRequest,
    HealthCheckResult,
    RestoreRequest,
    UpdateRequest,
    UpdateService,
    UpdateServiceError,
)


class FakeDocker:
    def __init__(
        self,
        *,
        docker_installed: bool = True,
        compose_installed: bool = True,
        pull_ok: bool = True,
        up_ok: bool = True,
        sandbox_ok: bool = True,
        cc_sandbox_ok: bool = True,
    ) -> None:
        self.docker_installed = docker_installed
        self.compose_installed = compose_installed
        self.pull_ok = pull_ok
        self.up_ok = up_ok
        self.sandbox_ok = sandbox_ok
        self.cc_sandbox_ok = cc_sandbox_ok
        self.pulls: list[tuple[Path, Path | None]] = []
        self.ups: list[tuple[Path, Path | None]] = []
        self.docker_pulls: list[tuple[str, str]] = []

    def pull(self, cwd: Path, env_file: Path | None = None) -> bool:
        self.pulls.append((cwd, env_file))
        return self.pull_ok

    def up(self, cwd: Path, env_file: Path | None = None) -> bool:
        self.ups.append((cwd, env_file))
        return self.up_ok

    def docker_pull(self, image: str, mirror: str = "") -> bool:
        self.docker_pulls.append((image, mirror))
        if image == "kromiose/nekro-cc-sandbox":
            return self.cc_sandbox_ok
        return self.sandbox_ok


class UpdateServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_dir = self.root / "nekro_agent"
        self.data_dir.mkdir()
        self.config_dir = self.root / "config"
        self.events: list[UpdateEvent] = []
        self.backups: list[BackupRequest] = []
        self.restores: list[RestoreRequest] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_stable_update_with_backup_and_sandbox(self) -> None:
        self._write_instance()
        docker = FakeDocker()
        service = self._service(docker)

        result = service.run(
            UpdateRequest(
                data_dir=self.data_dir,
                channel="stable",
                backup=True,
                update_sandbox=True,
                update_cc_sandbox=False,
            ),
            self.events.append,
        )

        self.assertEqual(result.channel, "stable")
        self.assertEqual(result.image_tag, "latest")
        self.assertEqual(len(self.backups), 1)
        self.assertIsNotNone(result.backup_file)
        self.assertEqual(len(docker.pulls), 1)
        self.assertEqual(len(docker.ups), 1)
        self.assertEqual(docker.docker_pulls, [("kromiose/nekro-agent-sandbox", "")])
        self.assert_event_types_cover_contract()

    def test_stable_update_without_backup(self) -> None:
        self._write_instance()
        docker = FakeDocker()
        service = self._service(docker)

        result = service.run(
            UpdateRequest(data_dir=self.data_dir, channel="stable", backup=False),
            self.events.append,
        )

        self.assertEqual(result.channel, "stable")
        self.assertEqual(self.backups, [])
        self.assertEqual(len(docker.pulls), 1)

    def test_preview_switch_creates_pre_preview_backup_and_sets_tag(self) -> None:
        self._write_instance(tag="latest")
        docker = FakeDocker()
        service = self._service(docker)

        result = service.run(
            UpdateRequest(data_dir=self.data_dir, channel="preview"),
            self.events.append,
        )

        self.assertEqual(result.channel, "preview")
        self.assertEqual(result.image_tag, "preview")
        self.assertEqual([backup.name for backup in self.backups], ["pre-preview"])

    def test_preview_pull_when_already_preview_does_not_repeat_backup(self) -> None:
        self._write_instance(tag="preview")
        docker = FakeDocker()
        service = self._service(docker)

        result = service.run(
            UpdateRequest(data_dir=self.data_dir, channel="preview"),
            self.events.append,
        )

        self.assertEqual(result.image_tag, "preview")
        self.assertEqual(self.backups, [])
        self.assertEqual(len(docker.pulls), 1)

    def test_rollback_with_restore_uses_latest_pre_preview_backup(self) -> None:
        self._write_instance(tag="preview")
        backup_file = self._write_backup("pre-preview")
        docker = FakeDocker()
        service = self._service(docker, restore_runner_restarts_service=False)

        result = service.run(
            UpdateRequest(
                data_dir=self.data_dir,
                channel="rollback",
                update_sandbox=False,
                restore_pre_preview=True,
            ),
            self.events.append,
        )

        self.assertEqual(result.channel, "stable")
        self.assertEqual(result.image_tag, "latest")
        self.assertEqual(result.backup_file, backup_file)
        self.assertEqual(
            [restore.backup_file for restore in self.restores],
            [backup_file],
        )
        self.assertEqual(len(docker.ups), 1)
        self.assertEqual(docker.pulls, [])

    def test_rollback_without_restore_pulls_latest(self) -> None:
        self._write_instance(tag="preview")
        docker = FakeDocker()
        service = self._service(docker)

        result = service.run(
            UpdateRequest(
                data_dir=self.data_dir,
                channel="rollback",
                update_sandbox=False,
                restore_pre_preview=False,
            ),
            self.events.append,
        )

        self.assertEqual(result.image_tag, "latest")
        self.assertEqual(self.restores, [])
        self.assertEqual(len(docker.pulls), 1)
        self.assertEqual(len(docker.ups), 1)

    def test_rollback_restore_without_pre_preview_backup_fails_structurally(self) -> None:
        self._write_instance(tag="preview")
        service = self._service(FakeDocker())

        with self.assertRaises(UpdateServiceError) as raised:
            service.run(
                UpdateRequest(
                    data_dir=self.data_dir,
                    channel="rollback",
                    restore_pre_preview=True,
                ),
                self.events.append,
            )

        self.assertEqual(raised.exception.code, "backup_not_found")
        self.assertEqual(self.restores, [])

    def test_docker_or_compose_unavailable_fails_before_real_calls(self) -> None:
        self._write_instance()
        service = self._service(FakeDocker(compose_installed=False))

        with self.assertRaises(UpdateServiceError) as raised:
            service.run(UpdateRequest(data_dir=self.data_dir), self.events.append)

        self.assertEqual(raised.exception.code, "docker_unavailable")

    def test_missing_compose_or_env_file_have_frozen_error_codes(self) -> None:
        service = self._service(FakeDocker())

        with self.assertRaises(UpdateServiceError) as raised_compose:
            service.run(UpdateRequest(data_dir=self.data_dir), self.events.append)
        self.assertEqual(raised_compose.exception.code, "compose_missing")

        self._write_compose("latest")
        with self.assertRaises(UpdateServiceError) as raised_env:
            service.run(UpdateRequest(data_dir=self.data_dir), self.events.append)
        self.assertEqual(raised_env.exception.code, "env_missing")

    def test_sandbox_pull_failure_is_warning_not_task_failure(self) -> None:
        self._write_instance()
        docker = FakeDocker(sandbox_ok=False)
        service = self._service(docker)

        result = service.run(
            UpdateRequest(
                data_dir=self.data_dir,
                channel="stable",
                backup=False,
                update_sandbox=True,
            ),
            self.events.append,
        )

        self.assertEqual(result.channel, "stable")
        self.assertEqual(
            result.warnings,
            ("沙盒镜像更新失败，可稍后手动更新。",),
        )
        self.assertTrue(any(event.type == "warning" for event in self.events))

    def assert_event_types_cover_contract(self) -> None:
        event_types = {event.type for event in self.events}
        self.assertIn("phase", event_types)
        self.assertIn("progress", event_types)
        self.assertIn("log", event_types)
        self.assertIn("result", event_types)

    def _service(
        self,
        docker: FakeDocker,
        *,
        restore_runner_restarts_service: bool = False,
    ) -> UpdateService:
        return UpdateService(
            docker_factory=lambda: docker,
            backup_runner=self._backup_runner,
            restore_runner=self._restore_runner,
            health_checker=self._health_checker,
            mirror_resolver=lambda _: "",
            config_dir_getter=lambda: self.config_dir,
            restore_runner_restarts_service=restore_runner_restarts_service,
        )

    def _backup_runner(self, request: BackupRequest) -> Path:
        self.backups.append(request)
        name = f"{request.name}_" if request.name else ""
        backup_dir = self.config_dir / "backup" / request.data_dir.name
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_file = (
            backup_dir
            / f"{request.data_dir.name}_backup_{name}20260611_010203.tar.gz"
        )
        backup_file.write_text("backup", encoding="utf-8")
        return backup_file

    def _restore_runner(self, request: RestoreRequest) -> None:
        self.restores.append(request)

    def _health_checker(self, data_dir: Path, env_path: Path) -> HealthCheckResult:
        self.assertEqual(data_dir, self.data_dir)
        self.assertTrue(env_path.exists())
        return HealthCheckResult(ok=True, url="http://127.0.0.1:8021/api/health")

    def _write_instance(self, *, tag: str = "latest") -> None:
        self._write_compose(tag)
        (self.data_dir / ".env").write_text(
            "NEKRO_EXPOSE_PORT=8021\n",
            encoding="utf-8",
        )

    def _write_compose(self, tag: str) -> None:
        (self.data_dir / "docker-compose.yml").write_text(
            "\n".join(
                [
                    "services:",
                    "  nekro_agent:",
                    f"    image: kromiose/nekro-agent:{tag}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _write_backup(self, name: str) -> Path:
        backup_dir = self.config_dir / "backup" / self.data_dir.name
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_file = (
            backup_dir
            / f"{self.data_dir.name}_backup_{name}_20260611_010203.tar.gz"
        )
        backup_file.write_text("backup", encoding="utf-8")
        return backup_file


if __name__ == "__main__":
    unittest.main()
