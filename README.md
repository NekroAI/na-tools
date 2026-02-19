# NA-Tools

**Nekro Agent 跨平台自动部署 CLI 工具**

支持 Windows / macOS / Linux，提供一键安装、更新、备份、恢复和配置管理。

## 安装

```bash
# 需要 Python 3.10+
pip install -e .

# 或使用 uv
uv sync
```

## 命令

| 命令 | 说明 |
|------|------|
| `na-tools install` | 安装 Nekro Agent（Docker 检测 → 配置 → 部署） |
| `na-tools update` | 更新服务到最新版本 |
| `na-tools backup` | 备份数据和配置 |
| `na-tools restore <file>` | 从备份恢复 |
| `na-tools config` | 快捷配置 nekro-agent.yaml |
| `na-tools status` | 查看服务状态 |
| `na-tools logs [service]` | 查看服务日志 |

## 快速开始

```bash
# 一键安装
na-tools install

# 配置模型 API
na-tools config model

# 添加管理员
na-tools config admin --add 12345678

# 更新到最新版
na-tools update

# 备份数据
na-tools backup

# 查看状态
na-tools status
```

## 跨平台支持

| 功能 | Linux | macOS | Windows |
|------|-------|-------|---------|
| Docker 安装 | ✅ 自动 | ⚠️ 引导 | ⚠️ 引导 |
| 服务部署 | ✅ | ✅ | ✅ |
| 备份恢复 | ✅ | ✅ | ✅ |
| 配置管理 | ✅ | ✅ | ✅ |
