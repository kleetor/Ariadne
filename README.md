# Ariadne — LLM DBA管理与目的驱动的联想记忆检索系统

LLM 驱动的记忆图谱构建、检索、可视化全链路管线。

> *"A thread through the labyrinth of memory."*\
> 记忆迷宫中的阿里阿德涅之线。

## 架构

```
对话日志 ──→ DBA维护 ──→ MemoryGraph + VectorStore ──→ P检索 ──→ Rerank ──→ 回复
   │            │                    │
   │     MaintenanceScheduler       ├──→ API Server (HTTP REST + 3D 面板)
   │     (批量异步调度)              ├──→ MCP Server (6 tools, stdio/SSE)
   │                                └──→ 离线 HTML
   └──→ 人工干预 (CRUD面板 + MCP dba_intervene)
```

## 安装

```bash
pip install -e .
# 需要本地 embedding 模型时（可选）
pip install -e ".[local]"
```

## 快速开始

仓库自带样例 `data/sample_graph.yaml`（4 节点 3 边）。

### 一键启动（推荐）

在 `dba_pipeline` 目录内运行 `start_all.py`，一条命令同时启动 3D 面板与 MCP SSE：

```bash
# 1) 复制配置模板并填入模型信息（MCP 会自动读取 .env，无需在命令行重复传参）
cp .env.example .env

# 2) 一键启动（面板 8765 + MCP SSE 8766，端口自动错开）
python start_all.py --yaml data/sample_graph.yaml

# 可视化面板  http://127.0.0.1:8765
# MCP SSE     http://127.0.0.1:8766/sse
```

> 可用 `--api-port` / `--mcp-port` / `--host` 覆盖默认端口与地址；Ctrl+C 同时停止两个服务。

### 1. 3D 可视化面板（浏览器查看 + 手动 CRUD）

```bash
ariadne-api --yaml data/sample_graph.yaml --port 8765
# 浏览器打开 http://127.0.0.1:8765
```

功能：3D 力导向图、图层过滤、聚焦模式、模糊搜索、CRUD 面板、操作自动写回 YAML。

### 2. MCP Server（供 LLM Agent 调用）

```bash
# 存根模式：Agent 可查图/手动 CRUD，无需 LLM 配置
ariadne-mcp --yaml data/sample_graph.yaml

# 完整 DBA 模式：Agent 说话后自动抽取维护图谱（需 LLM + Embedding）
ariadne-mcp --yaml data/sample_graph.yaml \
    --llm-model gpt-4o-mini --llm-api-key sk-xxx --llm-base-url https://api.openai.com/v1 \
    --embedding-model text-embedding-3-small
```

### 3. 离线 HTML（无需服务器）

```bash
ariadne-render --yaml data/sample_graph.yaml -o output.html
```

## MCP 服务详解

### 两种传输模式

| 模式            | 用法                                              | 适用场景                       |
| ------------- | ----------------------------------------------- | -------------------------- |
| **stdio**（默认） | `ariadne-mcp --yaml xxx.yaml`                   | Claude Desktop 等本地拉起进程的客户端 |
| **SSE**       | `ariadne-mcp --yaml xxx.yaml --sse --port 8765` | Cursor 等通过网络 url 连接的客户端    |

> ⚠️ SSE 默认端口 `8765` 与 `ariadne-api` 相同，同时运行需改端口，如 `--port 8766`。一键启动（`start_all.py`）会自动错开为 8766。

### 环境变量配置（可选）

LLM / Embedding 配置除命令行传参外，也可放到项目根 `.env`（或系统环境变量）中，启动时自动读取；命令行参数优先级更高，可覆盖环境变量。

| 命令行参数                  | 环境变量                 |
| ---------------------- | -------------------- |
| `--llm-model`          | `OPENAI_MODEL`       |
| `--llm-api-key`        | `OPENAI_API_KEY`     |
| `--llm-base-url`       | `OPENAI_API_BASE`    |
| `--embedding-model`    | `EMBEDDING_MODEL`    |
| `--embedding-api-key`  | `EMBEDDING_API_KEY`  |
| `--embedding-base-url` | `EMBEDDING_API_BASE` |
| `--embedding-local`    | `EMBEDDING_LOCAL`    |

```bash
# 项目根 .env 示例
OPENAI_API_KEY=sk-xxx
OPENAI_API_BASE=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-v4-flash
EMBEDDING_MODEL=BAAI/bge-large-zh-v1.5
EMBEDDING_LOCAL=true
```

> 设了 `EMBEDDING_LOCAL=true` 需安装本地依赖 `pip install -e ".[local]"`；否则启动时会提示并回退到存根模式。

### 两种运行模式

| 模式         | LLM 配置                                   | `dba_add_conversation` 行为 |
| ---------- | ---------------------------------------- | ------------------------- |
| **存根**     | 不需要                                      | 仅存储文本，不自动维护图谱             |
| **完整 DBA** | 需 LLM 配置（`--llm-model` 或 `OPENAI_MODEL`） | 调用内部 LLM 抽取节点/边、纠错、废弃旧记忆  |

> Agent 客户端的 LLM 与 Ariadne 内部的维护 LLM 是两个独立模型。Agent 的 LLM 负责理解意图、调用工具；Ariadne 的 LLM 负责把对话变成图谱事实。两者通过图谱（而非上下文窗口）共享记忆。

### Embedding 三种配置

| 模式          | 参数                                        | 说明                                 |
| ----------- | ----------------------------------------- | ---------------------------------- |
| **API**（默认） | `--embedding-model`                       | 调用任意 OpenAI 兼容 `/v1/embeddings` 端点 |
| **本地**      | `--embedding-model ... --embedding-local` | 用 sentence-transformers，无需网络       |
| **不使用**     | 不传 `--embedding-model`                    | 仅图谱操作，不启用向量检索                      |

`--embedding-base-url` / `--embedding-api-key` 未指定时自动复用 `--llm-base-url` / `--llm-api-key`。

```bash
# API embedding（OpenAI）
ariadne-mcp --yaml data.yaml --llm-model gpt-4o-mini --llm-api-key sk-xxx \
    --llm-base-url https://api.openai.com/v1 --embedding-model text-embedding-3-small

# 本地 embedding
ariadne-mcp --yaml data.yaml --llm-model gpt-4o-mini --llm-api-key sk-xxx \
    --embedding-model BAAI/bge-large-zh-v1.5 --embedding-local
```

### 6 个 Tool

| Tool                   | 说明                      |
| ---------------------- | ----------------------- |
| `dba_add_conversation` | 追加对话（存根仅存储，完整 DBA 触发维护） |
| `dba_query_memory`     | 按关键词搜索记忆节点（支持多关键词、模糊匹配）   |
| `dba_inspect_graph`    | 展开节点邻居 / 话题搜索           |
| `dba_intervene`        | 人工 CRUD 节点和边            |
| `dba_checkpoint`       | 保存完整检查点                 |
| `dba_get_stats`        | 图谱统计信息                  |

### 检索说明（`dba_query_memory`）

- 支持逗号分隔多关键词（如 `工作，公司，职业，上班`），任一关键词命中即返回。
- 匹配分两级：精确子串得满分，否则按字符 bigram 相似度做模糊匹配。
- `rerank_k` 控制返回上限（默认 20）；返回结果里 `total_matched` 是真实命中总数，`returned` 是本次实际返回数，若 `total_matched > returned` 可调大 `rerank_k` 取回剩余记忆。

### Agent 接入 JSON

**Claude Desktop（stdio）**：

```json
{
  "mcpServers": {
    "ariadne": {
      "command": "python",
      "args": ["-m", "dba_pipeline.mcp_server", "--yaml", "/path/to/memory_graph.yaml"],
      "cwd": "/path/to/dba_pipeline"
    }
  }
}
```

**Cursor / SSE 客户端**（先启动 Server，再填 url）：

```bash
# 推荐：一键启动（面板 8765 + MCP 8766），模型配置从 .env 读取
python start_all.py --yaml /path/to/memory_graph.yaml

# 或单独启动 SSE（模型配置同样从 .env 读取，也可用命令行覆盖）
ariadne-mcp --yaml /path/to/memory_graph.yaml --sse --port 8766
```

```json
{
  "mcpServers": {
    "ariadne": { "url": "http://127.0.0.1:8766/sse" }
  }
}
```

## YAML 数据格式

```yaml
nodes:
  - id: n1
    content: "用户在互联网公司做后端开发"
    type: status
  - id: n2
    content: "用户经常加班到晚上十点"
    type: reason

edges:
  - from: n2
    to: n1
    type: causal
    bidirectional: false
```

## 类型体系

**节点（6 种）**：`STATUS` `REASON` `ACTION` `THING` `PERSON` `EMOTION`

**边（8 种）**：`CAUSAL` `SCENARIO` `SEQUENCE` `PREFERENCE` `SOCIAL` `ATTRIBUTE` `TEMPORAL` `TAXONOMIC`

## 论文

- [LLM DBA 自动化记忆图谱构建](../docs/LLM%20DBA自动化图谱构建.md)
- [目的驱动的联想记忆检索模型](../docs/目的驱动的联想记忆检索模型.md)

## 许可证

MIT
