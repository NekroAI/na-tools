from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from na_tools.commands.restore import _remove_existing_path


class FakeDocker:
    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def run_ephemeral(
        self,
        image: str,
        cmd: list[str],
        volumes: dict[str, str],
        workdir: str | None = None,
    ) -> bool:
        self.calls.append(
            {
                "image": image,
                "cmd": cmd,
                "volumes": volumes,
                "workdir": workdir,
            }
        )
        return self.result


def test_remove_existing_path_uses_docker_fallback_for_permission_error(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "na_ap"
    target = data_dir / "system"
    target.mkdir(parents=True)
    docker = FakeDocker(result=True)

    with patch(
        "na_tools.commands.restore.shutil.rmtree",
        side_effect=PermissionError("denied"),
    ):
        _remove_existing_path(
            target, data_dir, docker, "alpine:latest"  # pyright: ignore[reportArgumentType]
        )

    assert docker.calls == [
        {
            "image": "alpine:latest",
            "cmd": [
                "sh",
                "-c",
                'rm -rf -- "$1"',
                "sh",
                "/restore-target/system",
            ],
            "volumes": {str(data_dir): "/restore-target"},
            "workdir": None,
        }
    ]


def test_remove_existing_path_reraises_when_docker_fallback_fails(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "na_ap"
    target = data_dir / "system"
    target.mkdir(parents=True)
    docker = FakeDocker(result=False)

    with (
        patch(
            "na_tools.commands.restore.shutil.rmtree",
            side_effect=PermissionError("denied"),
        ),
        pytest.raises(PermissionError, match="denied"),
    ):
        _remove_existing_path(
            target, data_dir, docker, "alpine:latest"  # pyright: ignore[reportArgumentType]
        )
