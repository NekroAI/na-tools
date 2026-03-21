"""Docker Compose 编排文件管理。"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from pathlib import Path

from ..utils.console import info, success, confirm, prompt
from ..utils.network import download_file

if TYPE_CHECKING:
    from .docker import DockerEnv


COMPOSE_FILE = "docker-compose.yml"
COMPOSE_NAPCAT_FILE = "docker-compose-x-napcat.yml"

# 需要备份/恢复的服务卷映射: 服务名 -> (容器内挂载路径, 备份文件名)
VOLUME_BACKUP_TARGETS: dict[str, tuple[str, str]] = {
    "nekro_postgres": ("/var/lib/postgresql/data", "postgres.tar.gz"),
    "nekro_qdrant": ("/qdrant/storage", "qdrant.tar.gz"),
}


def download_compose(data_dir: Path, *, with_napcat: bool = False) -> bool:
    """下载对应的 docker-compose.yml 到数据目录。

    Args:
        data_dir: 数据目录。
        with_napcat: 是否下载含 NapCat 的版本。
    """
    remote_file = COMPOSE_NAPCAT_FILE if with_napcat else COMPOSE_FILE
    local_path = data_dir / COMPOSE_FILE

    if with_napcat:
        info("将同时运行 NapCat 服务")

    if download_file(remote_file, local_path):
        success(f"docker-compose.yml 已下载到: {local_path}")
        return True
    return False


def compose_exists(data_dir: Path) -> bool:
    """检查 docker-compose.yml 是否存在。"""
    return (data_dir / COMPOSE_FILE).exists()


def apply_mirror_to_compose(data_dir: Path, mirror: str) -> None:
    """将镜像站应用到 docker-compose.yml 中的所有服务。

    Args:
        data_dir: 数据目录。
        mirror: 镜像站地址 (e.g. docker.1ms.run)。
    """
    if not mirror:
        return

    # 去除协议头和尾部斜杠
    mirror = mirror.replace("https://", "").replace("http://", "").rstrip("/")

    import yaml

    compose_path = data_dir / COMPOSE_FILE
    if not compose_path.exists():
        return

    with open(compose_path, encoding="utf-8") as f:
        content = yaml.safe_load(f)  # pyright: ignore[reportAny]

    if not isinstance(content, dict) or "services" not in content:
        return

    data = cast(dict[str, object], content)
    services_data = data["services"]

    if not isinstance(services_data, dict):
        return

    services = cast(dict[str, dict[str, object]], services_data)
    modified = False

    for service_name, service_config in services.items():
        if "image" in service_config:
            image = service_config["image"]
            if not isinstance(image, str):
                continue

            # 避免重复添加
            if not image.startswith(mirror):
                # 处理已经有域名的镜像 (e.g. ghcr.io/...)
                # 简单策略：直接在该镜像前拼上 mirror
                # 常见镜像站用法: mirror.com/library/image:tag  or mirror.com/image:tag
                # 对于 ghcr.io/kro... 这种，有些镜像站支持 mirror.com/ghcr.io/kro...
                # 或者有些是 mirror.com/kro...
                # 这里采用最通用的: mirror/image_name

                # 如果镜像本身包含 /，则认为是完整路径或者 namespace/image
                # 如果镜像不包含 /，则是 library/image (Docker Hub)

                # 简单粗暴做法：mirror/image_original

                new_image = f"{mirror}/{image}"
                service_config["image"] = new_image
                modified = True
                info(f"  服务 {service_name}: {image} -> {new_image}")

    if modified:
        with open(compose_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        success(f"已更新 docker-compose.yml 使用镜像站: {mirror}")


def patch_compose_isolation(data_dir: Path) -> None:
    """检测容器名冲突并通过 INSTANCE_NAME 环境变量实现多实例隔离。

    compose 文件已使用 ${INSTANCE_NAME:-} 作为容器名、卷名、网络名前缀，
    仅需在 .env 中设置 INSTANCE_NAME 即可完成隔离，无需修改 YAML。

    Args:
        data_dir: 数据目录。
    """
    import shutil

    from .config import load_env, save_env
    from .platform import run_cmd

    env_path = data_dir / ".env"
    env = load_env(env_path)

    # 已设置 INSTANCE_NAME，无需处理
    if env.get("INSTANCE_NAME"):
        info(f"已配置实例名称前缀: {env['INSTANCE_NAME']}")
        return

    # 检测是否存在同名容器
    docker_path = shutil.which("docker")
    if not docker_path:
        return

    try:
        result = run_cmd(
            [docker_path, "ps", "-a", "--format", "{{.Names}}"],
            capture=True,
            check=False,
        )
        existing_names = set(result.stdout.strip().splitlines())
    except Exception:
        return

    default_names = {"nekro_agent", "nekro_postgres", "nekro_qdrant", "nekro_napcat"}
    conflicts = default_names & existing_names

    if not conflicts:
        return

    info(f"检测到 {len(conflicts)} 个同名容器已存在：")
    for name in sorted(conflicts):
        info(f"  - {name}")

    if not confirm("是否设置实例名称前缀来隔离?", default=True):
        return

    prefix = prompt("请输入实例名称前缀", default="na")
    if not prefix:
        prefix = "na"

    # 确保前缀以 _ 结尾，与 compose 模板 ${INSTANCE_NAME:-}xxx 拼接
    if not prefix.endswith("_"):
        prefix = f"{prefix}_"

    env["INSTANCE_NAME"] = prefix
    save_env(env_path, env)
    success(f"已设置 INSTANCE_NAME={prefix}，容器将使用前缀隔离")


def set_image_tag(data_dir: Path, image_prefix: str, tag: str) -> bool:
    """修改 compose 中匹配 image_prefix 的服务镜像 tag。

    处理镜像站前缀：mirror/kromiose/nekro-agent:latest → mirror/kromiose/nekro-agent:preview

    Args:
        data_dir: 数据目录。
        image_prefix: 镜像前缀，如 "kromiose/nekro-agent"。
        tag: 目标 tag，如 "preview" 或 "latest"。

    Returns:
        是否成功修改。
    """
    import yaml

    compose_path = data_dir / COMPOSE_FILE
    if not compose_path.exists():
        return False

    with open(compose_path, encoding="utf-8") as f:
        content = yaml.safe_load(f)

    if not isinstance(content, dict) or "services" not in content:
        return False

    data = cast(dict[str, object], content)
    services_data = data["services"]
    if not isinstance(services_data, dict):
        return False

    services = cast(dict[str, dict[str, object]], services_data)
    matched = False
    modified = False

    for service_config in services.values():
        image = service_config.get("image")
        if not isinstance(image, str):
            continue
        # 匹配：image_prefix 可能带镜像站前缀
        # 例如 "docker.1ms.run/kromiose/nekro-agent:latest" 包含 "kromiose/nekro-agent"
        if image_prefix not in image:
            continue
        matched = True
        # 替换 tag：取 : 前的部分，拼接新 tag
        base = image.rsplit(":", 1)[0]
        new_image = f"{base}:{tag}"
        if new_image != image:
            service_config["image"] = new_image
            info(f"  镜像 tag 变更: {image} -> {new_image}")
            modified = True

    if modified:
        with open(compose_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    return matched


def resolve_service_volumes(
    docker: DockerEnv,
    data_dir: Path,
    env_file: Path | None,
) -> list[tuple[str, str]]:
    """解析 Compose 服务中需要备份/恢复的卷名。

    依次尝试：1) docker inspect 获取实际卷名；2) compose config 静态解析。

    Returns:
        [(实际卷名, 备份文件名), ...]
    """
    config = docker.get_compose_config(cwd=data_dir, env_file=env_file)
    if not config or not isinstance(config.get("services"), dict):
        return []

    services = cast(dict[str, dict[str, object]], config["services"])
    result: list[tuple[str, str]] = []

    for svc_name, (mount_path, filename) in VOLUME_BACKUP_TARGETS.items():
        if svc_name not in services:
            continue

        # 优先通过 docker inspect 解析
        volume_name = docker.get_service_volume(
            cwd=data_dir, service=svc_name, target=mount_path, env_file=env_file,
        )

        # 回退到 compose config 静态解析
        if not volume_name:
            volumes_config = services[svc_name].get("volumes", [])
            if isinstance(volumes_config, list):
                for vol in cast(list[dict[str, str]], volumes_config):
                    if (
                        vol
                        and vol.get("type") == "volume"
                        and vol.get("target") == mount_path
                    ):
                        volume_name = vol.get("source")
                        break

        if volume_name:
            result.append((volume_name, filename))

    return result
