# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

na-tools 是 Nekro Agent 的跨平台自动部署 CLI 工具（Python），通过 Docker Compose 一键安装、管理和维护 Nekro Agent 服务。仅支持 Linux 和 macOS。

## 开发命令

```bash
# 安装依赖（使用 uv）
uv sync

# 开发安装（使用 pip）
pip install -e .

# 构建发布包
uv build

# 运行 CLI
na-tools --help
python -m na_tools --help
```

项目无测试框架、无 lint 配置、无 Makefile。

## 发布流程

推送 `v*.*.*` 格式的 Git 标签触发 GitHub Actions（`.github/workflows/release.yml`）：自动 `uv build` → 发布到 PyPI → 创建 GitHub Release。版本号在 `pyproject.toml` 和 `src/na_tools/__init__.py` 两处维护，需保持一致。

## 架构

三层结构，严格单向依赖：

```
commands/（命令层：参数解析、用户交互流程编排）
    └──> core/（业务逻辑层：Docker 操作、配置管理、平台适配）
              └──> utils/（工具层：终端输出、网络下载、加密、权限）
```

### 关键模块

- **`core/docker.py`** — `DockerEnv` 类统一封装所有 docker/docker compose 调用，自动检测 Compose V1/V2
- **`core/platform.py`** — 跨平台适配、`run_command()` 命令执行、全局配置管理（`~/.config/na-tools/config.json`），记录所有已安装实例路径
- **`core/compose.py`** — docker-compose.yml 下载、镜像站注入、多实例隔离补丁（移除硬编码 container_name）
- **`core/config.py`** — `.env` 文件读写
- **`core/na_config.py`** — `nekro-agent.yaml` 应用配置读写
- **`utils/privilege.py`** — `with_sudo_fallback` 装饰器，权限不足时通过 `os.execvp("sudo", ...)` 透明重启当前命令
- **`utils/network.py`** — 多源文件下载，主源 GitHub raw + 国内镜像源回退

### CLI 框架

使用 Click，`cli.py` 定义命令组，`commands/` 下每个文件注册一个子命令。新增命令需要：
1. 在 `commands/` 下创建模块，用 `@click.command()` 定义
2. 在 `cli.py` 中 import 并 `main.add_command()`

## 技术栈

- Python >=3.10，构建后端 hatchling，src 布局
- click（CLI）、rich（终端输出/交互）、httpx（HTTP）、pyyaml（YAML 读写）
- 运行时依赖系统 docker/docker compose 和 sudo
