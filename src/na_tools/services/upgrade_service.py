"""Self-upgrade service for na-tools."""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import httpx

from .. import __version__
from ..core.platform import run_cmd
from .common import ServiceError

GITHUB_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/NekroAI/na-tools/releases/latest"
)
BINARY_ASSET_NAME = "na-tools-linux-x86_64"
BINARY_VERSION_PREFIX = "na-tools, version "
PACKAGE_NAME = "na-tools"
HTTP_TIMEOUT = 30.0

InstallMethod = Literal["uv_tool", "binary", "unsupported"]


@dataclass(frozen=True)
class InstallationInfo:
    """Current na-tools installation shape."""

    method: InstallMethod
    executable: Path
    detail: str


@dataclass(frozen=True)
class ReleaseInfo:
    """Latest release metadata needed for self-upgrade."""

    tag: str
    version: str
    assets: dict[str, str]


@dataclass(frozen=True)
class UpgradeCheckResult:
    """Result of checking whether na-tools itself is current."""

    current_version: str
    latest_version: str
    latest_tag: str
    update_available: bool
    installation: InstallationInfo
    binary_asset_url: str | None = None


@dataclass(frozen=True)
class UpgradeResult:
    """Result of executing a self-upgrade."""

    method: InstallMethod
    previous_version: str
    latest_version: str
    backup_path: Path | None = None
    restart_required: bool = True


class UpgradeServiceError(ServiceError):
    """Structured self-upgrade failure."""


ReleaseFetcher = Callable[[], dict[str, Any]]
UvToolDirGetter = Callable[[], Path | None]
UvFinder = Callable[[str], str | None]
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]
Downloader = Callable[[str, Path], None]
PlatformGetter = Callable[[], str]
MachineGetter = Callable[[], str]
Clock = Callable[[], float]


class UpgradeService:
    """Check and upgrade the current na-tools installation."""

    def __init__(
        self,
        *,
        current_version: str = __version__,
        executable: Path | None = None,
        frozen: bool | None = None,
        release_fetcher: ReleaseFetcher | None = None,
        uv_tool_dir_getter: UvToolDirGetter | None = None,
        uv_finder: UvFinder = shutil.which,
        runner: Runner | None = None,
        downloader: Downloader | None = None,
        platform_getter: PlatformGetter = platform.system,
        machine_getter: MachineGetter = platform.machine,
        clock: Clock = time.time,
        release_timeout: float = HTTP_TIMEOUT,
    ) -> None:
        self._current_version = current_version
        self._executable = executable or Path(sys.executable)
        self._frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self._release_fetcher = release_fetcher or self._fetch_latest_release
        self._uv_tool_dir_getter = uv_tool_dir_getter or self._default_uv_tool_dir
        self._uv_finder = uv_finder
        self._runner = runner or self._default_runner
        self._downloader = downloader or self._download_file
        self._platform_getter = platform_getter
        self._machine_getter = machine_getter
        self._clock = clock
        self._release_timeout = release_timeout

    def check(self) -> UpgradeCheckResult:
        """Check the latest release and current installation shape."""

        release = self._latest_release()
        current = parse_version(self._current_version)
        latest = parse_version(release.version)
        installation = self.detect_installation()
        return UpgradeCheckResult(
            current_version=normalize_version(self._current_version),
            latest_version=release.version,
            latest_tag=release.tag,
            update_available=current < latest,
            installation=installation,
            binary_asset_url=release.assets.get(BINARY_ASSET_NAME),
        )

    def upgrade(self, check_result: UpgradeCheckResult | None = None) -> UpgradeResult:
        """Upgrade na-tools according to its installation shape."""

        result = check_result or self.check()
        if not result.update_available:
            return UpgradeResult(
                method=result.installation.method,
                previous_version=result.current_version,
                latest_version=result.latest_version,
                restart_required=False,
            )

        if result.installation.method == "uv_tool":
            self._upgrade_uv_tool(result.latest_version)
            return UpgradeResult(
                method="uv_tool",
                previous_version=result.current_version,
                latest_version=result.latest_version,
            )

        if result.installation.method == "binary":
            backup = self._upgrade_binary(result)
            return UpgradeResult(
                method="binary",
                previous_version=result.current_version,
                latest_version=result.latest_version,
                backup_path=backup,
            )

        raise UpgradeServiceError(
            "unsupported_install",
            "当前安装方式暂不支持自动更新。",
            {
                "method": result.installation.method,
                "detail": result.installation.detail,
            },
        )

    def detect_installation(self) -> InstallationInfo:
        """Detect whether the current process is a uv tool or bundled binary."""

        executable = self._executable.expanduser().absolute()
        if self._frozen:
            return InstallationInfo(
                method="binary",
                executable=executable.resolve(),
                detail="当前进程是打包后的二进制文件。",
            )

        uv_tool_dir = self._uv_tool_dir_getter()
        if uv_tool_dir is not None and _is_relative_to(
            executable,
            uv_tool_dir.expanduser().absolute(),
        ):
            return InstallationInfo(
                method="uv_tool",
                executable=executable,
                detail=f"Python 解释器位于 uv tool 目录: {uv_tool_dir}",
            )

        return InstallationInfo(
            method="unsupported",
            executable=executable,
            detail="当前进程既不是 uv tool 环境，也不是打包二进制。",
        )

    def _latest_release(self) -> ReleaseInfo:
        payload = self._release_fetcher()
        tag_value = payload.get("tag_name")
        if not isinstance(tag_value, str) or not tag_value.strip():
            raise UpgradeServiceError(
                "release_tag_missing",
                "GitHub Release 响应缺少 tag_name。",
            )

        version = normalize_version(tag_value)
        _ = parse_version(version)

        assets_payload = payload.get("assets", [])
        assets: dict[str, str] = {}
        if isinstance(assets_payload, list):
            for item in assets_payload:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                url = item.get("browser_download_url")
                if isinstance(name, str) and isinstance(url, str):
                    assets[name] = url

        return ReleaseInfo(tag=tag_value, version=version, assets=assets)

    def _upgrade_uv_tool(self, expected_version: str) -> None:
        if self._uv_finder("uv") is None:
            raise UpgradeServiceError("uv_missing", "未找到 uv，无法执行 uv tool 升级。")
        normalized_expected = normalize_version(expected_version)
        try:
            self._runner(
                [
                    "uv",
                    "tool",
                    "install",
                    "--force",
                    f"{PACKAGE_NAME}=={normalized_expected}",
                ]
            )
            version_result = self._runner(
                [
                    str(self._executable),
                    "-c",
                    "from na_tools import __version__; print(__version__)",
                ]
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise UpgradeServiceError(
                "uv_upgrade_failed",
                f"uv tool 升级失败: {_error_message(exc)}",
            ) from exc

        actual_version = normalize_version(version_result.stdout.strip())
        try:
            _ = parse_version(actual_version)
        except UpgradeServiceError:
            actual_version = ""
        if actual_version != normalized_expected:
            raise UpgradeServiceError(
                "uv_version_mismatch",
                "uv tool 命令执行完成，但安装版本未达到目标版本: "
                f"期望 {normalized_expected}，实际 {actual_version or '无法识别'}。",
                {
                    "expected_version": normalized_expected,
                    "actual_version": actual_version or None,
                },
            )

    def _upgrade_binary(self, result: UpgradeCheckResult) -> Path:
        self._ensure_binary_platform()
        if result.binary_asset_url is None:
            raise UpgradeServiceError(
                "asset_missing",
                f"最新 Release 中未找到二进制资源: {BINARY_ASSET_NAME}",
            )

        executable = result.installation.executable
        if not executable.exists():
            raise UpgradeServiceError(
                "binary_executable_missing",
                f"当前二进制不存在: {executable}",
            )

        try:
            with tempfile.TemporaryDirectory(
                prefix=".na-tools-upgrade-",
                dir=executable.parent,
            ) as temp:
                candidate = Path(temp) / BINARY_ASSET_NAME
                self._downloader(result.binary_asset_url, candidate)
                candidate.chmod(0o755)
                version_result = self._runner([str(candidate), "--version"])
                self._verify_binary_version(
                    version_result.stdout,
                    result.latest_version,
                )
                backup = executable.with_name(
                    f"{executable.name}.bak-{int(self._clock())}"
                )
                shutil.copy2(executable, backup)
                os.replace(candidate, executable)
                executable.chmod(0o755)
                return backup
        except UpgradeServiceError:
            raise
        except (OSError, subprocess.CalledProcessError) as exc:
            raise UpgradeServiceError(
                "binary_upgrade_failed",
                f"二进制自动更新失败: {_error_message(exc)}",
            ) from exc

    @staticmethod
    def _verify_binary_version(output: str, expected_version: str) -> None:
        first_line = next(
            (line.strip() for line in output.splitlines() if line.strip()),
            "",
        )
        actual_version = (
            first_line.removeprefix(BINARY_VERSION_PREFIX)
            if first_line.startswith(BINARY_VERSION_PREFIX)
            else ""
        )
        normalized_expected = normalize_version(expected_version)
        normalized_actual = normalize_version(actual_version)

        try:
            _ = parse_version(normalized_actual)
        except UpgradeServiceError:
            normalized_actual = ""

        if normalized_actual != normalized_expected:
            actual_label = normalized_actual or "无法识别"
            raise UpgradeServiceError(
                "binary_version_mismatch",
                "候选二进制版本不匹配: "
                f"期望 {normalized_expected}，实际 {actual_label}。",
                {
                    "expected_version": normalized_expected,
                    "actual_version": normalized_actual or None,
                },
            )

    def _ensure_binary_platform(self) -> None:
        system = self._platform_getter().lower()
        machine = self._machine_getter().lower()
        if system != "linux" or machine not in {"x86_64", "amd64"}:
            raise UpgradeServiceError(
                "binary_platform_unsupported",
                "二进制自动更新当前仅支持 Linux x86_64。",
                {"platform": system, "machine": machine},
            )

    def _fetch_latest_release(self) -> dict[str, Any]:
        try:
            with httpx.Client(
                timeout=self._release_timeout,
                follow_redirects=True,
            ) as client:
                response = client.get(
                    GITHUB_LATEST_RELEASE_URL,
                    headers={"Accept": "application/vnd.github+json"},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise UpgradeServiceError(
                "release_fetch_failed",
                f"获取 GitHub Release 失败: {exc}",
            ) from exc
        if not isinstance(payload, dict):
            raise UpgradeServiceError(
                "release_fetch_failed",
                "GitHub Release 响应不是 JSON 对象。",
            )
        return cast(dict[str, Any], payload)

    def _default_uv_tool_dir(self) -> Path | None:
        if self._uv_finder("uv") is None:
            return None
        try:
            result = self._runner(["uv", "tool", "dir"])
        except (OSError, subprocess.CalledProcessError):
            return None
        path = result.stdout.strip()
        return Path(path) if path else None

    @staticmethod
    def _default_runner(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        return run_cmd(cmd, capture=True)

    @staticmethod
    def _download_file(url: str, output: Path) -> None:
        try:
            with httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with output.open("wb") as file:
                        for chunk in response.iter_bytes():
                            file.write(chunk)
        except httpx.HTTPError as exc:
            raise UpgradeServiceError(
                "download_failed",
                f"下载二进制失败: {exc}",
            ) from exc


def normalize_version(value: str) -> str:
    """Strip the supported v-prefix from a release version."""

    return value.strip().removeprefix("v")


def parse_version(value: str) -> tuple[int, int, int]:
    """Parse the repository's strict MAJOR.MINOR.PATCH version format."""

    normalized = normalize_version(value)
    parts = normalized.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        raise UpgradeServiceError(
            "invalid_version",
            f"不支持的版本格式: {value}",
            {"version": value},
        )
    return int(parts[0]), int(parts[1]), int(parts[2])


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _error_message(exc: BaseException) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        output = (exc.stderr or exc.stdout or "").strip()
        return output or str(exc)
    return str(exc)
