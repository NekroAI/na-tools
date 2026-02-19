"""网络请求工具，支持多源下载与重试。"""

from pathlib import Path

import httpx

from .console import error, info

# nekro-agent 资源的多个下载源
BASE_URLS = [
    "https://raw.githubusercontent.com/KroMiose/nekro-agent/main/docker",
    "https://ep.nekro.ai/e/KroMiose/nekro-agent/main/docker",
]

TIMEOUT = 30.0


def download_file(filename: str, output: Path) -> bool:
    """从多个源尝试下载文件。

    Args:
        filename: 远程文件名（相对于 BASE_URLS）。
        output: 本地保存路径。

    Returns:
        下载是否成功。
    """
    output.parent.mkdir(parents=True, exist_ok=True)

    for base_url in BASE_URLS:
        url = f"{base_url}/{filename}"
        try:
            info(f"正在下载 {filename} ...")
            with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
                resp = client.get(url)
                resp.raise_for_status()
            output.write_bytes(resp.content)
            return True
        except httpx.HTTPError:
            info(f"从 {base_url} 下载失败，尝试其他源...")
            continue

    error(f"所有源均下载失败: {filename}")
    return False


def download_url(url: str, output: Path) -> bool:
    """下载指定 URL 到本地文件。"""
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        info(f"正在下载 {url} ...")
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
            resp = client.get(url)
            resp.raise_for_status()
        output.write_bytes(resp.content)
        return True
    except httpx.HTTPError as e:
        error(f"下载失败: {e}")
        return False
