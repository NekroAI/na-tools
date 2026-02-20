"""Docker Compose 编排文件管理。"""

from typing import cast
from pathlib import Path

from ..utils.console import info, success
from ..utils.network import download_file


COMPOSE_FILE = "docker-compose.yml"
COMPOSE_NAPCAT_FILE = "docker-compose-x-napcat.yml"


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
    modified = False

    # 1. 移除 services 下的 container_name
    services = cast(dict[str, dict[str, object]], data.get("services", {}))
    for service_name, service_config in services.items():
        if "container_name" in service_config:
            del service_config["container_name"]
            modified = True
            info(f"  已移除服务 {service_name} 的 container_name")

    # 2. 移除 volumes 下的 name (让 docker compose 自动生成)
    volumes = cast(dict[str, dict[str, object] | None], data.get("volumes", {}))
    if volumes:
        for volume_name, volume_config in volumes.items():
            if volume_config:
                if "name" in volume_config:
                    del volume_config["name"]
                    modified = True
                    info(f"  已移除卷 {volume_name} 的显式 name")

    if modified:
        with open(compose_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        success("已更新 docker-compose.yml 以支持多实例部署")
