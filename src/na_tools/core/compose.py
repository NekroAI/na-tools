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
    """修补 docker-compose.yml 以移除硬编码的容器名和卷名，避免冲突。

    先检测是否存在冲突容器（硬编码 container_name），如果没有则不做修改。
    如果有，询问用户是否要添加前缀来隔离。

    Args:
        data_dir: 数据目录。
    """
    import yaml

    compose_path = data_dir / COMPOSE_FILE
    if not compose_path.exists():
        return

    with open(compose_path, encoding="utf-8") as f:
        content = yaml.safe_load(f)  # pyright: ignore[reportAny]

    if not isinstance(content, dict):
        return

    data = cast(dict[str, object], content)

    # 1. 检测是否存在硬编码的 container_name
    services = cast(dict[str, dict[str, object]], data.get("services", {}))
    container_names_to_patch: dict[str, str] = {}

    for service_name, service_config in services.items():
        if "container_name" in service_config:
            container_name = service_config["container_name"]
            if isinstance(container_name, str):
                container_names_to_patch[service_name] = container_name

    # 如果没有硬编码的 container_name，则不做任何修改
    if not container_names_to_patch:
        info("检测到 docker-compose.yml 中无硬编码容器名，无需修改")
        return

    # 有冲突容器，询问用户是否要添加前缀
    info(f"检测到 {len(container_names_to_patch)} 个硬编码容器名：")
    for service_name, container_name in container_names_to_patch.items():
        info(f"  - {service_name}: {container_name}")

    if not confirm("是否要添加前缀来隔离这些容器?", default=True):
        return

    # 获取前缀（默认为 "na"）
    prefix = prompt("请输入容器名前缀", default="na")
    if not prefix:
        prefix = "na"

    modified = False

    # 2. 为 services 下的 container_name 添加前缀
    for service_name, old_container_name in container_names_to_patch.items():
        new_container_name = f"{prefix}_{old_container_name}"
        services[service_name]["container_name"] = new_container_name
        modified = True
        info(f"  已修改服务 {service_name} 的容器名: {old_container_name} -> {new_container_name}")

    # 3. 为 volumes 下的 name 添加前缀
    volumes = cast(dict[str, dict[str, object] | None], data.get("volumes", {}))
    if volumes:
        for volume_name, volume_config in volumes.items():
            if volume_config and "name" in volume_config:
                old_name = volume_config["name"]
                if isinstance(old_name, str):
                    new_name = f"{prefix}_{old_name}"
                    volume_config["name"] = new_name
                    modified = True
                    info(f"  已修改卷 {volume_name} 的名称: {old_name} -> {new_name}")

    if modified:
        with open(compose_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        success(f"已更新 docker-compose.yml，使用前缀 '{prefix}' 隔离容器")


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
