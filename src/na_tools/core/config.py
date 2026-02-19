""".env 配置文件管理。"""

from pathlib import Path
from typing import Optional

from ..utils.console import info, prompt, success
from ..utils.crypto import random_string
from ..utils.network import download_file


ENV_EXAMPLE_FILENAME = ".env.example"


def load_env(env_path: Path) -> dict[str, str]:
    """解析 .env 文件为字典。忽略注释和空行。"""
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def save_env(env_path: Path, data: dict[str, str]) -> None:
    """将字典写入 .env 文件。保留原有注释行。"""
    lines: list[str] = []
    written_keys: set[str] = set()

    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                lines.append(line)
                continue
            if "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in data:
                    lines.append(f"{key}={data[key]}")
                    written_keys.add(key)
                else:
                    lines.append(line)
            else:
                lines.append(line)

    # 追加新的 key
    for key, value in data.items():
        if key not in written_keys:
            lines.append(f"{key}={value}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def download_env_example(data_dir: Path) -> bool:
    """下载 .env.example 模板到数据目录。"""
    return download_file(ENV_EXAMPLE_FILENAME, data_dir / ENV_EXAMPLE_FILENAME)


def setup_env(
    data_dir: Path,
    *,
    interactive: bool = True,
    with_napcat: bool = False,
    port: Optional[int] = None,
) -> Path:
    """设置 .env 文件（交互式或使用默认值）。

    Returns:
        .env 文件路径。
    """
    env_path = data_dir / ".env"

    if not env_path.exists():
        example_path = data_dir / ENV_EXAMPLE_FILENAME
        if not example_path.exists():
            info("正在下载 .env.example 模板...")
            if not download_env_example(data_dir):
                raise RuntimeError("无法下载 .env.example")

        import shutil

        shutil.copy(example_path, env_path)
        info(f"已创建 .env 文件: {env_path}")

    # 加载现有配置
    env = load_env(env_path)

    # 设置 NEKRO_DATA_DIR
    env["NEKRO_DATA_DIR"] = str(data_dir)

    # 端口
    if port:
        env["NEKRO_EXPOSE_PORT"] = str(port)
    elif interactive and not env.get("NEKRO_EXPOSE_PORT"):
        env["NEKRO_EXPOSE_PORT"] = prompt("请设置服务端口", default="8021")

    if not env.get("NEKRO_EXPOSE_PORT"):
        env["NEKRO_EXPOSE_PORT"] = "8021"

    # NapCat 端口
    if with_napcat:
        if interactive and not env.get("NAPCAT_EXPOSE_PORT"):
            env["NAPCAT_EXPOSE_PORT"] = prompt("请设置 NapCat 端口", default="6099")
        if not env.get("NAPCAT_EXPOSE_PORT"):
            env["NAPCAT_EXPOSE_PORT"] = "6099"

    # 镜像站配置
    if interactive and not env.get("MIRROR_REGISTRY"):
        env["MIRROR_REGISTRY"] = prompt(
            "Docker 镜像站 (可选, e.g. docker.1ms.run)", default=""
        )

    # 自动生成安全随机值
    if not env.get("ONEBOT_ACCESS_TOKEN"):
        env["ONEBOT_ACCESS_TOKEN"] = random_string(32)
        info("已自动生成 ONEBOT_ACCESS_TOKEN")

    if not env.get("NEKRO_ADMIN_PASSWORD"):
        env["NEKRO_ADMIN_PASSWORD"] = random_string(16)
        info("已自动生成 NEKRO_ADMIN_PASSWORD")

    if not env.get("QDRANT_API_KEY"):
        env["QDRANT_API_KEY"] = random_string(32)
        info("已自动生成 QDRANT_API_KEY")

    # 数据库默认值
    env.setdefault("POSTGRES_USER", "nekro_agent")
    env.setdefault("POSTGRES_PASSWORD", "nekro_agent")
    env.setdefault("POSTGRES_DATABASE", "nekro_agent")

    save_env(env_path, env)
    success(f".env 配置已保存: {env_path}")
    return env_path
