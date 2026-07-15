# NA-Tools

**Nekro Agent 跨平台自动部署 CLI 工具**

支持 macOS / Linux，提供一键安装、更新、备份、恢复和配置管理。

## 安装

```bash
# 推荐通过 uv tool 安装（需要 Python 3.10+）
uv tool install na-tools

# 或下载 GitHub Release 中的 Linux x86_64 二进制文件

# 或从源码安装
uv sync
```

## 命令一览

### 部署管理

| 命令 | 说明 |
|------|------|
| `na-tools install` | 安装 Nekro Agent（Docker 检测 → 配置 → 部署） |
| `na-tools start` | 启动当前或指定实例的 Docker Compose 编排 |
| `na-tools stop` | 关闭当前或指定实例的 Docker Compose 编排 |
| `na-tools update` | 更新服务到最新版本 |
| `na-tools remove` | 卸载并移除指定的 NA 实例 |
| `na-tools daemon register` | 主动注册并启动当前或指定实例的 root daemon 服务 |

### 实例管理

| 命令 | 说明 |
|------|------|
| `na-tools bind` | 绑定已安装的 NA 实例到管理列表 |
| `na-tools use <id/path>` | 切换当前激活的数据目录 |
| `na-tools list` | 列出所有已安装的 Nekro Agent 及序号 |
| `na-tools status` | 查看服务状态 |

### 数据管理

| 命令 | 说明 |
|------|------|
| `na-tools backup` | 备份数据和配置 |
| `na-tools backup list` | 列出所有历史备份 |
| `na-tools restore [file]` | 从备份恢复（不指定文件则从列表选择） |
| `na-tools config` | 配置镜像源 |

### 日志与工具

| 命令 | 说明 |
|------|------|
| `na-tools logs [service]` | 查看服务日志 |
| `na-tools napcat` | 引导 NapCat 登录并自动配置 OneBot 连接 |
| `na-tools upgrade` | 检测并更新 na-tools 自身 |

## 快速开始

```bash
# 一键安装
na-tools install

# 绑定已安装的 NA 实例（适用于从其他方式安装的或迁移的 NA）
na-tools bind --data-dir /path/to/nekro_data

# 配置镜像源
na-tools config "docker.1ms.run"

# 更新到最新版
na-tools update

# 启动 / 关闭编排
na-tools start
na-tools stop

# 主动注册并启动 root daemon 服务（适用于迁移或服务文件丢失后的修复）
na-tools daemon register

# 备份数据
na-tools backup

# 恢复备份（交互式选择）
na-tools restore

# 查看状态
na-tools status

# 检测并更新 na-tools 自身
na-tools upgrade --check
na-tools upgrade
```

## na-tools 自身更新

`na-tools upgrade` 用于更新部署工具自身，和 `na-tools update`（更新 Nekro Agent 服务）是两个独立命令。

```bash
# 只检测当前版本是否为最新
na-tools upgrade --check

# 检测后交互确认并自动更新
na-tools upgrade

# 跳过确认，适合脚本调用
na-tools upgrade --yes
```

版本检测以 `NekroAI/na-tools` 的 GitHub Releases 最新稳定版为准。自动更新支持：

- `uv tool install na-tools` 安装的版本：强制安装检测到的目标版本
- GitHub Release 中的 Linux x86_64 二进制：下载 `na-tools-linux-x86_64`，校验后替换当前可执行文件

源码开发安装、pip 普通安装或其他方式安装的版本仅支持检测，会提示手动更新方式。

除 `upgrade` 外，成功执行其他 na-tools 命令后也会检测最新版本；检测结果会缓存 6 小时，
缓存过期后才会再次联网。发现新版时会在标准错误流提示运行
`na-tools upgrade`，但不会自动更新或询问确认。`na-tools --version`
同样会检测，帮助输出、执行失败或用户取消的命令不会检测。联网检测的各网络阶段超时为
3 秒，网络或 GitHub API 异常时静默跳过并进入缓存冷却，不影响原命令结果或标准输出。

## Preview 频道

Preview 频道提供预览版镜像 (`kromiose/nekro-agent:preview`)，可提前体验新功能，但可能不稳定。

### 全新安装 preview 版本

```bash
na-tools install --preview
```

### 从稳定版切换到 preview

```bash
# 自动创建名为 "pre-preview" 的备份，然后切换镜像 tag
na-tools update --preview
```

切换前会自动备份，备份名称为 `pre-preview`，用于后续快速回退。

### 从 preview 回退到稳定版

```bash
# 自动切回 latest 镜像，并提示从 pre-preview 备份还原数据
na-tools update --rollback
```

回退流程：
1. 将 docker-compose.yml 中的镜像 tag 切回 `latest`
2. 自动查找最近的 `pre-preview` 备份
3. 询问是否从该备份还原数据
4. 拉取 latest 镜像并重启服务

## 备份与恢复

### 基本备份

```bash
# 默认备份（自动停止服务、打包数据、备份存储卷、重启服务）
na-tools backup

# 指定输出路径
na-tools backup -o /path/to/backup.tar.gz

# 备份后不自动重启服务
na-tools backup --no-restart
```

### 命名备份

通过 `--name` 为备份添加名称标识，方便在恢复时识别用途：

```bash
# 带名称的备份
na-tools backup --name before-migration

# 文件名格式：nekro_agent_backup_before-migration_20260318_120000.tar.gz
```

### 查看备份列表

```bash
na-tools backup list

# 只查看指定名称的备份
na-tools backup list --name pre-preview

# 只查看最近 5 个备份
na-tools backup list --limit 5
```

输出示例：
```
ℹ 发现以下历史备份：
  [1] nekro_agent_backup_pre-preview_20260318_120000.tar.gz (备份时间: 2026-03-18 12:00:00, 名称: pre-preview, 大小: 45.2 MB)
  [2] nekro_agent_backup_20260317_100000.tar.gz (备份时间: 2026-03-17 10:00:00, 大小: 43.8 MB)
```

### 恢复备份

```bash
# 交互式选择备份（显示备份名称）
na-tools restore

# 指定备份文件
na-tools restore /path/to/backup.tar.gz

# 恢复到指定数据目录
na-tools restore --data-dir /path/to/data
```

备份内容包括：
- 数据目录下所有文件（`.env`、`docker-compose.yml`、应用配置等）
- Docker 存储卷（PostgreSQL、Qdrant 数据）
- 自动排除缓存和临时文件以减小体积

## CC 沙盒镜像

CC 沙盒 (`kromiose/nekro-cc-sandbox`) 是可选组件，安装和更新时均可选择是否拉取：

```bash
# 安装时拉取 CC 沙盒（交互模式下也会询问）
na-tools install --with-cc-sandbox

# 安装时明确不拉取
na-tools install --without-cc-sandbox

# 更新时同时更新 CC 沙盒
na-tools update --update-cc-sandbox
```

## 多实例管理

支持在同一台机器上管理多个 Nekro Agent 实例：

```bash
# 安装到不同目录
na-tools install --data-dir ~/nekro_agent_dev
na-tools install --data-dir ~/nekro_agent_prod

# 绑定已有安装
na-tools bind --data-dir /opt/nekro_data

# 查看所有实例
na-tools list

# 切换激活实例
na-tools use 2

# 移除实例（保留数据）
na-tools remove --keep-data

# 直接启动或关闭指定实例编排
na-tools start --data-dir ~/nekro_agent_prod
na-tools stop --data-dir ~/nekro_agent_prod
```

## 跨平台支持

| 功能 | Linux | macOS |
|------|-------|-------|
| Docker 安装 | ✅ 自动 | ⚠️ 引导 |
| 服务部署 | ✅ | ✅ |
| 备份恢复 | ✅ | ✅ |
| 配置管理 | ✅ | ✅ |

## 技术栈

- **Python** ≥ 3.10，src 布局，hatchling 构建
- **Click** — CLI 框架
- **Rich** — 终端输出与交互
- **httpx** — HTTP 客户端
- **PyYAML** — YAML 配置读写
- 运行时依赖系统 `docker` / `docker compose` 和 `sudo`
