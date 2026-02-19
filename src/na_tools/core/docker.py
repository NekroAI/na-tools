"""Docker / Docker Compose 环境检测与操作。"""

import shutil
import subprocess
from pathlib import Path
import tempfile


from ..utils.console import confirm, error, info, prompt, success, warning
from .platform import is_macos, run_cmd


def _find_docker() -> str | None:
    """查找 docker 可执行文件路径。"""
    return shutil.which("docker")


def _detect_compose_cmd() -> list[str] | None:
    """检测可用的 Docker Compose 命令。

    优先使用 `docker compose`（V2 plugin），其次 `docker-compose`。
    """
    docker = _find_docker()
    if docker:
        try:
            _ = run_cmd([docker, "compose", "version"], capture=True, check=True)
            return [docker, "compose"]
        except Exception:
            pass

    dc = shutil.which("docker-compose")
    if dc:
        return [dc]

    return None


class DockerEnv:
    """Docker 环境管理。"""

    def __init__(self) -> None:
        self.docker_path: str | None = _find_docker()
        self.compose_cmd: list[str] | None = _detect_compose_cmd()

    @property
    def docker_installed(self) -> bool:
        return self.docker_path is not None

    @property
    def compose_installed(self) -> bool:
        return self.compose_cmd is not None

    def print_status(self) -> None:
        if self.docker_installed:
            assert self.docker_path is not None
            try:
                result = run_cmd([self.docker_path, "--version"], capture=True)
                success(f"Docker 已安装: {result.stdout.strip()}")
            except Exception:
                success("Docker 已安装")
        else:
            error("Docker 未安装")

        if self.compose_installed:
            assert self.compose_cmd is not None
            try:
                result = run_cmd([*self.compose_cmd, "version"], capture=True)
                success(f"Docker Compose 已安装: {result.stdout.strip()}")
            except Exception:
                success("Docker Compose 已安装")
        else:
            error("Docker Compose 未安装")

    def ensure_docker(self) -> bool:
        """确保 Docker 环境可用，不可用时引导安装。

        Returns:
            Docker 是否可用。
        """
        if self.docker_installed and self.compose_installed:
            self.print_status()
            return True

        if is_macos():
            error("Docker 未安装。请安装 Docker Desktop for macOS:")
            info("  方式 1: brew install --cask docker")
            info("  方式 2: https://www.docker.com/products/docker-desktop/")
            if shutil.which("brew"):
                if confirm("是否通过 Homebrew 安装 Docker Desktop?"):
                    try:
                        _ = run_cmd(["brew", "install", "--cask", "docker"])
                        info(
                            "Docker Desktop 已安装，请从 应用程序 中启动后重新运行此工具。"
                        )
                    except Exception as e:
                        error(f"安装失败: {e}")
            return False

        # Linux: 自动安装
        if not self.docker_installed:
            warning("Docker 未安装，将尝试自动安装...")
            if not confirm("是否安装 Docker?", default=True):
                return False

            if not self._install_docker_linux():
                error("Docker 安装失败，请手动安装后重试。")
                return False

            # 刷新检测
            self.docker_path = _find_docker()
            self.compose_cmd = _detect_compose_cmd()

        return self.docker_installed and self.compose_installed

    def _install_docker_linux(self) -> bool:
        """Linux 上通过官方脚本安装 Docker。"""
        info("正在通过 Docker 官方脚本安装...")
        mirrors = {"1": "", "2": "Aliyun", "3": "AzureChinaCloud"}
        info("请选择 Docker 安装源:")
        info("  1) Docker 官方")
        info("  2) 阿里云")
        info("  3) Azure 中国")

        choice = prompt("请输入选项", default="1")
        mirror = mirrors.get(choice, "")

        try:
            import httpx  # lazy import for optional dependency

            resp = httpx.get(
                "https://get.docker.com", timeout=30, follow_redirects=True
            )
            _ = resp.raise_for_status()
            script = resp.text
        except Exception as e:
            error(f"Docker 安装脚本下载失败: {e}")
            return False

        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            _ = f.write(script)
            script_path = f.name

        try:
            cmd = ["sh", script_path]
            if mirror:
                cmd.extend(["--mirror", mirror])
            _ = run_cmd(["sudo"] + cmd, check=True)
            success("Docker 安装成功!")
            return True
        except Exception as e:
            error(f"Docker 安装失败: {e}")
            return False
        finally:
            Path(script_path).unlink(missing_ok=True)

    # --- Docker Compose 操作 ---

    def compose(
        self,
        *args: str,
        cwd: Path | None = None,
        env_file: Path | None = None,
        check: bool = True,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """执行 docker compose 命令。

        注意: Docker Compose 中 shell 环境变量优先级高于 --env-file，
        因此在运行前先从进程环境中移除 .env 中定义的变量，
        确保 --env-file 的值不被 shell 环境变量覆盖。
        """
        if not self.compose_cmd:
            raise RuntimeError("Docker Compose 不可用")

        cmd = [*self.compose_cmd]
        keys_to_unset: set[str] | None = None
        if env_file:
            cmd.extend(["--env-file", str(env_file)])
            # 读取 .env 中的 key，运行前从环境中移除，避免 shell 变量覆盖
            from .config import load_env

            keys_to_unset = set(load_env(env_file).keys())
        cmd.extend(args)

        return run_cmd(
            cmd, cwd=cwd, check=check, capture=capture, unset_keys=keys_to_unset
        )

    def pull(self, cwd: Path, env_file: Path | None = None) -> bool:
        try:
            _ = self.compose("pull", cwd=cwd, env_file=env_file)
            return True
        except Exception as e:
            error(f"镜像拉取失败: {e}")
            return False

    def up(self, cwd: Path, env_file: Path | None = None) -> bool:
        try:
            _ = self.compose("up", "-d", cwd=cwd, env_file=env_file)
            return True
        except Exception as e:
            error(f"服务启动失败: {e}")
            return False

    def down(self, cwd: Path, env_file: Path | None = None) -> bool:
        try:
            _ = self.compose("down", cwd=cwd, env_file=env_file)
            return True
        except Exception as e:
            error(f"服务停止失败: {e}")
            return False

    def ps(self, cwd: Path, env_file: Path | None = None) -> str:
        try:
            result = self.compose("ps", cwd=cwd, env_file=env_file, capture=True)
            return result.stdout
        except Exception:
            return ""

    def restart_service(
        self, service: str, cwd: Path, env_file: Path | None = None
    ) -> bool:
        try:
            _ = self.compose("restart", service, cwd=cwd, env_file=env_file)
            return True
        except Exception as e:
            error(f"服务重启失败: {e}")
            return False

    def docker_pull(self, image: str, mirror: str = "") -> bool:
        """拉取单个 Docker 镜像。"""
        if not self.docker_path:
            return False

        target_image = image
        if mirror:
            mirror = mirror.replace("https://", "").replace("http://", "").rstrip("/")
            target_image = f"{mirror}/{image}"
            info(f"使用镜像站拉取: {target_image}")

        docker_path: str = self.docker_path
        try:
            _ = run_cmd([docker_path, "pull", target_image])
            # 如果是镜像站拉取的，tag回来，保证原名可用（可选，但在 na-tools 场景下通常不需要，因为我们是 compose up）
            # 但对于 sandbox 这种直接 docker run 的，或者后续可能引用的，tag 回来比较保险？
            # 这里的 sandbox 是通过 docker run 运行的吗？
            # 查一下 sandbox 用法。目前只看到 docker_pull。
            # 假设 sandbox 只是 pull 下来备用，或者 user 手动 run。
            # 如果 user 手动 run original name，docker 会找不到。
            # 所以最好 tag 一下。
            if mirror:
                try:
                    _ = run_cmd([docker_path, "tag", target_image, image])
                except Exception:
                    pass
            return True
        except Exception as e:
            error(f"镜像拉取失败 {target_image}: {e}")
            return False

    def logs(
        self,
        service: str,
        cwd: Path,
        *,
        follow: bool = False,
        tail: int = 100,
        env_file: Path | None = None,
    ) -> None:
        """查看服务日志。"""
        args = ["logs", f"--tail={tail}"]
        if follow:
            args.append("-f")
        args.append(service)
        _ = self.compose(*args, cwd=cwd, env_file=env_file, check=False)
