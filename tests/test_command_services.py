from __future__ import annotations

import json
import tarfile
from datetime import datetime
from pathlib import Path

import pytest

from na_tools.services.backup_service import BackupRequest, BackupService
from na_tools.services.config_service import ConfigService
from na_tools.services.daemon_service import DaemonRootServiceResult, DaemonServiceError
from na_tools.services.install_service import (
    InstallRequest,
    InstallService,
    InstallServiceError,
)
from na_tools.services.instance_service import InstanceService, InstanceServiceError
from na_tools.services.napcat_service import (
    NapcatConfigureRequest,
    NapcatService,
    build_onebot_config,
)
from na_tools.services.remove_service import RemoveRequest, RemoveService
from na_tools.services.restore_service import RestoreRequest, RestoreService


class FakeBackupDocker:
    compose_installed = False

    def down(self, cwd: Path, env_file: Path | None = None) -> bool:
        return True

    def up(self, cwd: Path, env_file: Path | None = None) -> bool:
        return True

    def run_ephemeral(
        self,
        image: str,
        cmd: list[str],
        volumes: dict[str, str],
        workdir: str | None = None,
    ) -> bool:
        return True


class FakeRemoveDocker:
    compose_installed = False

    def down(self, cwd: Path, env_file: Path | None = None) -> bool:
        return True

    def compose(
        self,
        *args: str,
        cwd: Path | None = None,
        env_file: Path | None = None,
        check: bool = True,
        capture: bool = False,
    ) -> object:
        return object()


class FakeNapcatDocker:
    def __init__(self, restart_ok: bool = True) -> None:
        self.restart_ok = restart_ok
        self.restarts: list[tuple[str, Path, Path | None]] = []

    def restart_service(
        self, service: str, cwd: Path, env_file: Path | None = None
    ) -> bool:
        self.restarts.append((service, cwd, env_file))
        return self.restart_ok


class FakeLogsDocker:
    compose_installed = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, Path, bool, int, Path | None]] = []

    def ps(self, cwd: Path, env_file: Path | None = None) -> str:
        return ""

    def logs(
        self,
        service: str,
        cwd: Path,
        *,
        follow: bool = False,
        tail: int = 100,
        env_file: Path | None = None,
    ) -> None:
        self.calls.append((service, cwd, follow, tail, env_file))


class FakeInstallDocker:
    docker_installed = True
    compose_installed = True

    def __init__(self) -> None:
        self.pulls: list[tuple[Path, Path | None]] = []
        self.ups: list[tuple[Path, Path | None]] = []
        self.image_pulls: list[tuple[str, str]] = []

    def ensure_docker(self) -> bool:
        return True

    def pull(self, cwd: Path, env_file: Path | None = None) -> bool:
        self.pulls.append((cwd, env_file))
        return True

    def up(self, cwd: Path, env_file: Path | None = None) -> bool:
        self.ups.append((cwd, env_file))
        return True

    def docker_pull(self, image: str, mirror: str = "") -> bool:
        self.image_pulls.append((image, mirror))
        return True


class FakeDaemonInstallManager:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.installs: list[Path] = []

    def install_and_start(self, data_dir: Path) -> DaemonRootServiceResult:
        self.installs.append(data_dir)
        if self.fail:
            raise DaemonServiceError("daemon_service_start_failed", "boom")
        return DaemonRootServiceResult(
            data_dir=data_dir,
            service_name="na-tools-daemon-test.service",
            service_path=data_dir / ".na-tools" / "daemon.service",
            action="install_start",
            command="systemctl start",
        )


class FakeDaemonUninstallManager:
    def __init__(self, *, missing: bool = False) -> None:
        self.missing = missing
        self.uninstalls: list[Path] = []

    def uninstall_registered(self, data_dir: Path) -> DaemonRootServiceResult:
        self.uninstalls.append(data_dir)
        if self.missing:
            raise DaemonServiceError("daemon_service_missing", "missing")
        return DaemonRootServiceResult(
            data_dir=data_dir,
            service_name="na-tools-daemon-test.service",
            service_path=data_dir / ".na-tools" / "daemon.service",
            action="uninstall",
            command="systemctl disable",
        )


def _patch_install_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_setup_env(
        data_dir: Path,
        *,
        interactive: bool,
        with_napcat: bool,
        port: int | None,
    ) -> Path:
        env_path = data_dir / ".env"
        env_path.write_text(
            "\n".join(
                [
                    f"NEKRO_EXPOSE_PORT={port or 8021}",
                    "NEKRO_ADMIN_PASSWORD=password",
                    "ONEBOT_ACCESS_TOKEN=token",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return env_path

    def fake_download_compose(data_dir: Path, *, with_napcat: bool) -> bool:
        data_dir.joinpath("docker-compose.yml").write_text(
            "services:\n  nekro_agent:\n    image: kromiose/nekro-agent:latest\n",
            encoding="utf-8",
        )
        return True

    monkeypatch.setattr("na_tools.services.install_service.setup_env", fake_setup_env)
    monkeypatch.setattr(
        "na_tools.services.install_service.download_compose",
        fake_download_compose,
    )
    monkeypatch.setattr(
        "na_tools.services.install_service.patch_compose_isolation",
        lambda _data_dir: None,
    )
    monkeypatch.setattr("na_tools.services.install_service.resolve_mirror", lambda _env: "")
    monkeypatch.setattr(
        "na_tools.services.install_service.set_default_data_dir",
        lambda _data_dir: None,
    )


def test_backup_service_creates_named_backup_and_skips_cache(tmp_path: Path) -> None:
    data_dir = tmp_path / "nekro_data"
    cache_dir = data_dir / "logs"
    cache_dir.mkdir(parents=True)
    (data_dir / ".env").write_text("NEKRO_EXPOSE_PORT=8021\n", encoding="utf-8")
    (data_dir / "keep.txt").write_text("ok", encoding="utf-8")
    (cache_dir / "skip.log").write_text("skip", encoding="utf-8")
    config_dir = tmp_path / "config"

    service = BackupService(
        docker_factory=FakeBackupDocker,
        config_dir_getter=lambda: config_dir,
        clock=lambda: datetime(2026, 1, 2, 3, 4, 5),
    )

    result = service.run(BackupRequest(data_dir=data_dir, name="manual"))

    assert result.backup_path.name == "nekro_data_backup_manual_20260102_030405.tar.gz"
    assert result.skipped_cache == 1
    with tarfile.open(result.backup_path, "r:gz") as tar:
        names = tar.getnames()
    assert "nekro_data/keep.txt" in names
    assert "nekro_data/logs/skip.log" not in names


def test_install_service_registers_and_starts_daemon_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_install_io(monkeypatch)
    data_dir = tmp_path / "nekro_data"
    docker = FakeInstallDocker()
    daemon = FakeDaemonInstallManager()

    result = InstallService(
        docker_factory=lambda: docker,
        daemon_service_manager=daemon,
    ).run(InstallRequest(data_dir=data_dir))

    assert daemon.installs == [data_dir.resolve()]
    assert result.daemon_service is not None
    assert result.daemon_service.service_name == "na-tools-daemon-test.service"
    assert docker.ups == [(data_dir.resolve(), data_dir / ".env")]


def test_install_service_can_skip_daemon_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_install_io(monkeypatch)
    data_dir = tmp_path / "nekro_data"
    daemon = FakeDaemonInstallManager()

    result = InstallService(
        docker_factory=FakeInstallDocker,
        daemon_service_manager=daemon,
    ).run(InstallRequest(data_dir=data_dir, start_daemon=False))

    assert daemon.installs == []
    assert result.daemon_service is None


def test_install_service_daemon_failure_aborts_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_install_io(monkeypatch)
    data_dir = tmp_path / "nekro_data"

    with pytest.raises(InstallServiceError) as raised:
        InstallService(
            docker_factory=FakeInstallDocker,
            daemon_service_manager=FakeDaemonInstallManager(fail=True),
        ).run(InstallRequest(data_dir=data_dir))

    assert raised.value.code == "daemon_service_start_failed"


def test_restore_service_restores_archive_without_starting_service(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.txt").write_text("restored", encoding="utf-8")
    backup = tmp_path / "backup.tar.gz"
    with tarfile.open(backup, "w:gz") as tar:
        tar.add(source, arcname="source")
    target = tmp_path / "target"

    result = RestoreService(docker_factory=FakeBackupDocker).run(
        RestoreRequest(backup_file=backup, data_dir=target, start_service=False)
    )

    assert result.data_dir == target.resolve()
    assert (target / "config.txt").read_text(encoding="utf-8") == "restored"
    assert result.service_started is False


def test_remove_service_unmanaged_keep_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "nekro_data"
    data_dir.mkdir()
    (data_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        "na_tools.services.remove_service.load_global_config",
        lambda: {"installations": {}},
    )
    saved: list[dict[str, object]] = []
    monkeypatch.setattr(
        "na_tools.services.remove_service.save_global_config",
        lambda config: saved.append(config),
    )

    result = RemoveService(docker_factory=FakeRemoveDocker).run(
        RemoveRequest(data_dir=data_dir, keep_data=True, remove_daemon=False)
    )

    assert result.was_managed is False
    assert data_dir.exists()
    assert saved == []


def test_remove_service_deletes_registered_daemon_before_keep_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "nekro_data"
    data_dir.mkdir()
    (data_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        "na_tools.services.remove_service.load_global_config",
        lambda: {"installations": {}},
    )
    daemon = FakeDaemonUninstallManager()

    result = RemoveService(
        docker_factory=FakeRemoveDocker,
        daemon_service_manager=daemon,
    ).run(RemoveRequest(data_dir=data_dir, keep_data=True))

    assert daemon.uninstalls == [data_dir.resolve()]
    assert result.daemon_service is not None
    assert result.daemon_service.service_name == "na-tools-daemon-test.service"
    assert data_dir.exists()


def test_remove_service_skips_missing_daemon_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "nekro_data"
    data_dir.mkdir()
    (data_dir / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    monkeypatch.setattr(
        "na_tools.services.remove_service.load_global_config",
        lambda: {"installations": {}},
    )
    daemon = FakeDaemonUninstallManager(missing=True)

    result = RemoveService(
        docker_factory=FakeRemoveDocker,
        daemon_service_manager=daemon,
    ).run(RemoveRequest(data_dir=data_dir, keep_data=True))

    assert daemon.uninstalls == [data_dir.resolve()]
    assert result.daemon_service is None
    assert any("未找到已注册的 root daemon 服务" in item for item in result.warnings)


def test_instance_logs_resolves_service_aliases(tmp_path: Path) -> None:
    data_dir = tmp_path / "nekro_data"
    data_dir.mkdir()
    (data_dir / "docker-compose.yml").write_text(
        "services:\n  nekro_agent: {}\n  nekro_napcat: {}\n",
        encoding="utf-8",
    )
    (data_dir / ".env").write_text("INSTANCE_NAME=na_\n", encoding="utf-8")
    docker = FakeLogsDocker()

    InstanceService(docker_factory=lambda: docker).logs(
        "napcat",
        data_dir=data_dir,
        follow=True,
        tail=25,
    )

    assert docker.calls == [
        ("nekro_napcat", data_dir.resolve(), True, 25, data_dir / ".env")
    ]


def test_instance_logs_rejects_unknown_service_before_docker_call(tmp_path: Path) -> None:
    data_dir = tmp_path / "nekro_data"
    data_dir.mkdir()
    (data_dir / "docker-compose.yml").write_text(
        "services:\n  nekro_agent: {}\n",
        encoding="utf-8",
    )
    docker = FakeLogsDocker()

    with pytest.raises(InstanceServiceError) as raised:
        InstanceService(docker_factory=lambda: docker).logs(
            "napcat",
            data_dir=data_dir,
        )

    assert raised.value.code == "service_missing"
    assert "nekro_agent" in raised.value.message
    assert docker.calls == []


def test_config_service_delegates_mirror(monkeypatch: pytest.MonkeyPatch) -> None:
    values: list[str] = []
    monkeypatch.setattr("na_tools.services.config_service.get_global_mirror", lambda: "mirror")
    monkeypatch.setattr("na_tools.services.config_service.set_global_mirror", values.append)

    service = ConfigService()

    assert service.get_mirror() == "mirror"
    service.set_mirror("new")
    assert values == ["new"]


def test_napcat_service_writes_config_and_restarts(tmp_path: Path) -> None:
    data_dir = tmp_path / "nekro_data"
    data_dir.mkdir()
    (data_dir / ".env").write_text(
        "NAPCAT_EXPOSE_PORT=6099\nONEBOT_ACCESS_TOKEN=secret\n",
        encoding="utf-8",
    )
    docker = FakeNapcatDocker()

    result = NapcatService(docker_factory=lambda: docker).configure(
        NapcatConfigureRequest(
            data_dir=data_dir,
            qq="123456",
            overwrite=True,
            restart=True,
        )
    )

    payload = json.loads(result.config_path.read_text(encoding="utf-8"))
    assert payload == build_onebot_config(result.ws_url, "secret")
    assert result.restarted is True
    assert docker.restarts
