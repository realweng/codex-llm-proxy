# Codex LLM 代理

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)

[English](README.md) | **中文**

让 **OpenAI Codex CLI** 能够使用**多种国产大模型**，通过本地代理将 OpenAI Responses API 格式转换为 Chat Completions 格式。

已支持供应商：**GLM（智谱 AI）** 和 **Kimi（月之暗面）**。

> **说明：** 本项目修改自 [https://github.com/JichinX/codex-glm-proxy](https://github.com/JichinX/codex-glm-proxy)。

## ✨ 特性

- ✅ **有限 Codex 兼容** - 保证基本功能可用
- ✅ **多后端支持** - 一个参数即可切换 GLM 和 Kimi
- ✅ **流式响应支持** - 实时流式响应
- ✅ **工具调用** - 支持 `apply_patch`、`exec` 等 Codex 工具
- ✅ **多轮对话** - 保持对话上下文
- ✅ **自动模型映射** - 自动将 OpenAI 模型名映射到供应商对应版本
- ✅ **简单配置** - 单个 Python 文件，无需复杂依赖

## 🔄 架构

```
┌─────────────────┐
│   Codex CLI     │  发送: Responses API 请求
│   (用户端)      │  接收: Responses API 响应
└────────┬────────┘
         │ Responses API 格式
         ▼
┌─────────────────────────────────────────┐
│   Codex LLM 代理 (localhost:18765)    │
│                                          │
│  ┌────────────────────────────────────┐ │
│  │  请求转换器                        │ │
│  │  - Responses API → Chat Completions│ │
│  │  - 工具调用历史处理                │ │
│  │  - 模型名映射                      │ │
│  └────────────────────────────────────┘ │
│                                          │
│  ┌────────────────────────────────────┐ │
│  │  响应转换器                        │ │
│  │  - Chat Completions → Responses API│ │
│  │  - 工具调用流式传输                │ │
│  │  - 事件排序                        │ │
│  └────────────────────────────────────┘ │
└──────────────────┬──────────────────────┘
                   │
         ┌─────────┴─────────┐
         │  后端选择器       │
         │  (glm / kimi)     │
         └─────────┬─────────┘
                   │
     ┌─────────────┴─────────────┐
     │                           │
     ▼                           ▼
┌──────────┐            ┌──────────────┐
│ GLM API  │            │  Kimi API    │
│ (智谱AI) │            │ (月之暗面)   │
└──────────┘            └──────────────┘
```

## 🚀 快速开始

### 前置要求

- Python 3.8+
- 所选供应商的 API 密钥
- 已安装 [OpenAI Codex CLI](https://github.com/openai/codex)

### 安装步骤

1. **克隆仓库**
   ```bash
   git clone https://github.com/realweng/codex-llm-proxy.git
   cd codex-llm-proxy
   ```

2. **设置 API 密钥**
   ```bash
   # GLM
   export GLM_API_KEY="你的_GLM_API_密钥"

   # Kimi
   export KIMI_API_KEY="你的_Kimi_API_密钥"
   ```

3. **启动代理**
   ```bash
   # 使用 GLM 后端（默认）
   ./scripts/start.sh

   # 使用 Kimi 后端
   ./scripts/start.sh -p kimi
   ```

   代理将运行在 `http://localhost:18765`

4. **配置 Codex CLI**

   创建或更新 `~/.codex/config.toml`：

   **GLM 配置：**
   ```toml
   model_provider = "glm-proxy"
   model = "gpt-4o"

   [model_providers.glm-proxy]
   name = "GLM via Proxy"
   base_url = "http://localhost:18765/v4"
   wire_api = "responses"
   ```

   **Kimi 配置：**
   ```toml
   model_provider = "kimi-proxy"
   model = "gpt-4o"

   [model_providers.kimi-proxy]
   name = "Kimi via Proxy"
   base_url = "http://localhost:18765/v4"
   wire_api = "responses"
   ```

5. **测试**
   ```bash
   mkdir test-codex && cd test-codex && git init
   codex exec "创建一个 Python hello world 程序" --full-auto
   ```

## 📋 配置说明

### 环境变量

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `BACKEND` | `glm` | 后端供应商：`glm` 或 `kimi` |
| `GLM_API_KEY` | *(glm 必需)* | 你的 GLM API 密钥 |
| `GLM_API_BASE` | `https://open.bigmodel.cn/api/coding/paas/v4` | GLM API 端点 |
| `KIMI_API_KEY` | *(kimi 必需)* | 你的 Kimi API 密钥 |
| `KIMI_API_BASE` | `https://api.kimi.com/coding` | Kimi API 端点 |
| `PROXY_PORT` | `18765` | 本地代理端口 |

### 启动脚本用法

```bash
./scripts/start.sh [-p <glm|kimi>]

# 示例：
./scripts/start.sh              # 默认使用 GLM 后端
./scripts/start.sh -p glm       # 使用 GLM 后端
./scripts/start.sh -p kimi      # 使用 Kimi 后端
```

## 🗺️ 模型映射

### GLM 后端

| OpenAI 模型 | GLM 模型 | 说明 |
|------------|----------|------|
| `gpt-4` | `glm-4` | 标准 GPT-4 |
| `gpt-4-turbo` | `glm-4` | GPT-4 Turbo |
| `gpt-4o` | `glm-5` | **推荐**，最佳编码体验 |
| `gpt-4o-mini` | `glm-4-flash` | 更快、更便宜 |
| `gpt-3.5-turbo` | `glm-4-flash` | 旧版支持 |
| `gpt-5.x-codex` | `glm-5` | 未来 Codex 模型 |

### Kimi 后端

| OpenAI 模型 | Kimi 模型 | 说明 |
|------------|-----------|------|
| `gpt-4` | `kimi-for-coding` | |
| `gpt-4-turbo` | `kimi-for-coding` | |
| `gpt-4o` | `kimi-for-coding` | **推荐** |
| `gpt-4o-mini` | `kimi-for-coding` | |
| `gpt-3.5-turbo` | `kimi-for-coding` | 旧版支持 |
| `gpt-5.x-codex` | `kimi-for-coding` | 未来 Codex 模型 |

**建议：** 在 Codex 配置中使用 `model = "gpt-4o"` 以获得最佳效果。

## 🔧 管理命令

```bash
# 使用 GLM 后端启动
./scripts/start.sh -p glm

# 使用 Kimi 后端启动
./scripts/start.sh -p kimi

# 检查是否运行
curl http://localhost:18765/health

# 查看日志
tail -f /tmp/codex-llm-proxy.log

# 停止代理
./scripts/stop.sh
```

## 📝 使用示例

```bash
# 简单任务
codex exec "创建一个计算斐波那契数列的 Python 函数" --full-auto

# 更复杂的项目
codex exec "用 FastAPI 构建一个待办事项管理的 REST API" --full-auto

# 包含测试
codex exec "创建一个计算器模块并编写单元测试" --full-auto
```

## 🐛 故障排除

### "Streaming complete, sent 0 chunks"
**原因：** 模型名未正确映射
**解决：** 确保配置中使用已知模型如 `gpt-4o`

### Codex 循环/重复操作
**原因：** 工具调用历史未正确处理
**解决：** 更新到最新版本的代理

### 502 Bad Gateway
**原因：** 代理崩溃
**解决：** 检查日志 `/tmp/codex-llm-proxy.log` 并重启

### Connection refused
**原因：** 代理未运行
**解决：** 使用 `./scripts/start.sh` 启动代理

## 🤝 贡献

欢迎贡献！请随时提交 Pull Request。

## 📄 许可证

本项目采用 MIT 许可证 - 详情见 [LICENSE](LICENSE) 文件。

## 🙏 致谢

- [OpenAI Codex](https://github.com/openai/codex) - 强大的编程助手
- [智谱 AI GLM](https://open.bigmodel.cn/) - 强大的国产大模型
- [月之暗面 Kimi](https://kimi.moonshot.cn/) - 强大的编程模型
- [codex-glm-proxy](https://github.com/JichinX/codex-glm-proxy) - 启发了本项目的原始 GLM 代理项目

## 📊 项目状态

⚠️ **Beta** - 核心功能已测试；边界情况可能不工作

| 功能 | 状态 |
|------|------|
| 文本对话 | ✅ 正常 |
| 模型映射 | ✅ 正常 |
| 流式响应 | ✅ 正常 |
| 工具调用 | ✅ 正常 |
| 多轮对话 | ✅ 正常 |
| 工具调用历史 | ✅ 正常 |
| 工具调用结果 | ✅ 正常 |
| 多后端 (GLM/Kimi) | ✅ 正常 |

---

**用 ❤️ 打造，服务社区**

**觉得有用请点个 Star ⭐**
