# Ariadne: LLM DBA管理与目的驱动的联想记忆检索系统 — 工程化落地计划

> 2026-08-12 | 全部 P0/P1 任务已完成
>
> 本文是 [LLM DBA自动化图谱构建](LLM%20DBA自动化图谱构建.md) 和 [目的驱动的联想记忆检索模型](%E7%9B%AE%E7%9A%84%E9%A9%B1%E5%8A%A8%E7%9A%84%E8%81%94%E6%83%B3%E8%AE%B0%E5%BF%86%E6%A3%80%E7%B4%A2%E6%A8%A1%E5%9E%8B.md) 两篇论文的工程化实现文档，将原型验证系统封装为可部署的完整产品。

---

> *命名取自希腊神话：阿里阿德涅（Ariadne）将线团交给忒修斯，助其深入迷宫杀死牛头人后循线返回。*
> 在 Ariadne 系统中：
> - **记忆图谱 = 迷宫** — 复杂、深不可测的联想网络
> - **图边 / 跳转轴 = 阿里阿德涅之线** — 联想检索中唯一正确的引导路径
> - **P 检索 = 循线而行** — 不会被迷宫中的噪音节点吞噬
> - **DBA 维护 = 织线人** — 每次维护都是在加固/修正线索

## 实施结果总览

| 模块 | 文件 | 状态 |
|---|------|:---:|
| 图谱 YAML 导出/导入 | `src/graph/memory_graph.py` | 完成 |
| VectorStore 缓存序列化 | `src/embedding/store.py` | 完成 |
| 统一 checkpoint/restore | `src/extraction/dba.py` | 完成 |
| 调度器状态持久化 | `src/extraction/maintenance_scheduler.py` | 完成 |
| 构建器状态持久化 | `src/extraction/graph_builder.py` | 完成 |
| 定时自动检查点 | `src/extraction/maintenance_scheduler.py` | 完成 |
| 3D 可视化面板 | `src/viz/templates/dashboard_3d.html` | 完成 |
| CRUD 控制面板 | 同上（集成在 3D 面板中） | 完成 |
| API 自动持久化 | `src/viz/api_server.py` | 完成 |
| MCP Server (6 tools) | `src/mcp_server.py` | 完成 |
| 集成测试 | `tests/test_persistence.py` | 完成 |

### 关键变更

**MemoryGraph** (`src/graph/memory_graph.py`):
- `add_memory()` 新增 `deprecated`/`forgotten` 参数
- `to_dict()` — 序列化为 YAML 兼容的 dict（双向边自动去重）
- `from_dict(data)` — 类方法，从 dict 还原 MemoryGraph
- `to_mermaid(max_nodes)` — 导出 Mermaid flowchart（备用）

**VectorStore** (`src/embedding/store.py`):
- `save()` 额外写入 `content_vectors.npz`
- `load()` 自动恢复 `_content_vectors` 缓存

**MemoryDBA** (`src/extraction/dba.py`):
- `save_checkpoint(save_dir)` — 保存 图谱 YAML + FAISS 索引 + content_vectors.npz + builder_state.json + checkpoint.json
- `restore_checkpoint(save_dir)` — 从检查点恢复全部状态

**MaintenanceScheduler** (`src/extraction/maintenance_scheduler.py`):
- `save_state()` / `load_state()` — 序列化 buffer、计数器、统计
- `_start_auto_checkpoint()` — threading.Timer 定时自动保存

**GraphBuilder** (`src/extraction/graph_builder.py`):
- `save_state()` / `load_state()` — 序列化 orphan_tracker、stats

**API Server** (`src/viz/api_server.py`):
- `MemoryGraphAPI.__init__` 新增 `yaml_path` 参数
- `_save()` — 每次 CRUD 操作后自动写回 YAML 文件

**MCP Server** (`src/mcp_server.py`):
- 6 个 MCP Tool: `dba_add_conversation`, `dba_query_memory`, `dba_inspect_graph`, `dba_intervene`, `dba_checkpoint`, `dba_get_stats`
- 双传输: stdio（本地 Agent）+ SSE（远程 Agent）
- `dba_add_conversation` 支持接入真实 `MemoryDBA.maintain()`（通过 `--llm-model` 参数启用）
- `dba_checkpoint` 完整保存图谱 + 向量 + 构建器 + 调度器状态
- 依赖 `pip install mcp`

### 使用方式

```bash
# API 服务器（CRUD + 自动持久化 + 3D 可视化）
python -m src.viz.api_server --yaml data/natural_person/memory_graph.yaml --port 8765

# MCP Server (stdio 存根模式)
python -m src.mcp_server --yaml data/natural_person/memory_graph.yaml

# MCP Server (stdio 完整 DBA 模式)
python -m src.mcp_server --yaml data/natural_person/memory_graph.yaml \
    --llm-model gpt-4o-mini --llm-api-key sk-xxx --llm-base-url https://api.openai.com/v1 \
    --embedding-model BAAI/bge-m3 --vector-dir data/vectors

# MCP Server (SSE)
python -m src.mcp_server --yaml data/natural_person/memory_graph.yaml --sse --port 8765

# 集成测试
python -c "from tests.test_persistence import *; test_roundtrip(); test_yaml_export(); test_exporter(); test_crud_persistence(); test_edge_serialization()"
```

---

## 一、持久化

### 1.1 检查点涵盖的完整状态

```
checkpoint = {
    graph:     {nodes: [{id, content, node_type, deprecated, forgotten, created_at, previous_content}],
                edges:  [{from, to, rel_type, weight}]},
    vectors:   FAISS index（二进制） + _content_vectors 缓存,
    scheduler: {buffer, meaningful_count, consecutive_skips, proactive_mode},
    builder:   {orphan_tracker: {node_id: count}, stats},
    version:   "1"
}
```

### 1.2 存储格式与目录结构

```
snapshots/
  dba_20260812_143000/
    memory_graph.yaml          # 节点 + 边（完整 YAML，可被 data/loader.py 直接加载）
    faiss_index.faiss          # FAISS 原生索引
    faiss_index.pkl            # FAISS 元数据
    content_vectors.npz        # _content_vectors 缓存（numpy 压缩）
    scheduler_state.json       # 调度器运行时计数器
    builder_state.json         # orphan_tracker + stats
    checkpoint.json            # 版本号 + 时间戳 + 统计信息
```

### 1.3 触发策略

| 触发方式 | 机制 | 频率 |
|---------|------|------|
| 手动 | `dba.checkpoint()` / MCP `dba_checkpoint` | 按需 |
| 定时 | 调度器 `threading.Timer` 自动触发 | 可配置 `auto_checkpoint_interval` |
| 关闭 | `dba.save_checkpoint()` 最后一次保存 | 每次关闭 |

---

## 二、DBA 可视化

已通过 `3d-force-graph` + `Three.js` 实现 3D 可视化面板，替代原计划的 Mermaid 导出。

### 2.1 功能清单

| 功能 | 状态 |
|------|:---:|
| 3D 力导向图渲染 | 完成 |
| 节点类型颜色区分（6 种） | 完成 |
| 图层过滤（按节点类型/边类型独立开关） | 完成 |
| 聚焦模式（点击节点隐藏无关边和节点） | 完成 |
| 搜索框（模糊搜索 + 下拉列表 + 点击聚焦） | 完成 |
| 拖拽固定（含球状轨道 + 半透明环特效） | 完成 |
| CRUD 控制面板（创建/编辑/删除节点和边） | 完成 |
| 悬浮窗布局（左右面板 + 中间 3D 图） | 完成 |
| 双向边渲染 + 方向箭头 | 完成 |

详见 [DBA可视化面板设计.md](DBA可视化面板设计.md)。

---

## 三、人工干预

通过 3D 可视化面板的 CRUD 控制面板和 MCP `dba_intervene` 工具实现。

### 3.1 干预操作集

| 操作 | 前端面板 | MCP Tool | 校验 |
|------|:---:|:---:|------|
| 创建节点 | 支持 | `dba_intervene create_node` | 类型合法性 |
| 更新节点 | 支持 | `dba_intervene update_node` | 节点存在性 |
| 删除节点 | 支持 | `dba_intervene delete_node` | 节点存在性 |
| 创建边 | 支持（含连接模式） | `dba_intervene create_edge` | 节点存在 + 不重复 |
| 删除边 | 支持 | `dba_intervene delete_edge` | 边存在性 |

所有操作自动触发 `_save()` 写回 YAML。

---

## 四、MCP 封装

### 4.1 架构

```
┌─────────────────┐    MCP (stdio / SSE)     ┌──────────────────┐
│  LLM Agent       │◄──────────────────────►│  DBA MCP Server   │
│  (Claude/GPT等)  │                         │  ┌──────────────┐ │
└─────────────────┘                         │  │ DBA Core      │ │
                                            │  │ +Scheduler    │ │
                                            │  │ +VectorStore  │ │
                                            │  │ +GraphBuilder │ │
                                            │  │ +Checkpoint   │ │
                                            │  │ +Intervene    │ │
                                            │  └──────────────┘ │
                                            └──────────────────┘
```

### 4.2 暴露的 MCP Tools

| Tool 名 | 输入 | 输出 | 说明 |
|---------|------|------|------|
| `dba_add_conversation` | `{conversation: str}` | `{maintained: bool, stats}` | 追加对话，触发维护（接入真实 DBA 时调用 `MemoryDBA.maintain()`） |
| `dba_query_memory` | `{query: str, rerank_k?: int}` | `{memories: [...]}` | 目的驱动联想检索（P 链路：跳转轴 + 目的回归 + 寻峰） |
| `dba_inspect_graph` | `{node_id: str}` | `{nodes: [...], edges: [...]}` | 图谱查看（单节点 1-hop 展开） |
| `dba_intervene` | `{action: str, params: {...}}` | `{result, errors}` | 人工干预（create/update/delete 节点/边） |
| `dba_checkpoint` | `{save_dir?: str}` | `{save_dir, stats}` | 保存完整检查点（图谱+向量+构建器+调度器状态） |
| `dba_get_stats` | `{}` | `{stats}` | 系统统计（节点/边数、废弃数） |

### 4.3 运行模式

| 模式 | 说明 |
|------|------|
| 存根模式 | 仅加载 YAML 图谱，`dba_add_conversation` 返回空结果，`dba_checkpoint` 仅保存 YAML |
| 完整 DBA 模式 | 通过 `--llm-model` 等参数接入 LLM + Embedding，`dba_add_conversation` 实时调用 `MemoryDBA.maintain()` |

### 4.4 Agent 接入配置

MCP 客户端通过 JSON 配置注册 Server，以下为常见平台的配置模板。

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "ariadne": {
      "command": "python",
      "args": [
        "-m", "src.mcp_server",
        "--yaml", "C:\\Users\\makot\\Desktop\\memory\\data\\natural_person\\memory_graph.yaml"
      ],
      "cwd": "C:\\Users\\makot\\Desktop\\memory"
    }
  }
}
```

**Cursor / 其他支持 SSE 的客户端** (需先启动 MCP Server):

```json
{
  "mcpServers": {
    "ariadne": {
      "url": "http://127.0.0.1:8765/sse"
    }
  }
}
```

```bash
# SSE 模式需先启动服务端（LLM 和 Embedding 的配置都在此命令行里，Agent 端只写 url）
# 存根模式
python -m src.mcp_server --yaml data/natural_person/memory_graph.yaml --sse --port 8765

# 完整 DBA 模式（API embedding）
python -m src.mcp_server --yaml data/natural_person/memory_graph.yaml --sse --port 8765 \
    --llm-model gpt-4o-mini --llm-api-key sk-xxx --llm-base-url https://api.openai.com/v1 \
    --embedding-model text-embedding-3-small

# 本地 embedding
python -m src.mcp_server --yaml data/natural_person/memory_graph.yaml --sse --port 8765 \
    --llm-model gpt-4o-mini --llm-api-key sk-xxx \
    --embedding-model BAAI/bge-large-zh-v1.5 --embedding-local
```

**完整 DBA 模式配置**（带 LLM + Embedding API，以 OpenAI 为例）:

```json
{
  "mcpServers": {
    "ariadne": {
      "command": "python",
      "args": [
        "-m", "src.mcp_server",
        "--yaml", "C:\\Users\\makot\\Desktop\\memory\\data\\natural_person\\memory_graph.yaml",
        "--llm-model", "gpt-4o-mini",
        "--llm-api-key", "sk-xxx",
        "--llm-base-url", "https://api.openai.com/v1",
        "--embedding-model", "text-embedding-3-small"
      ],
      "cwd": "C:\\Users\\makot\\Desktop\\memory"
    }
  }
}
```

**SiliconFlow 示例**（LLM + Embedding 同 API）:

```json
{
  "mcpServers": {
    "ariadne": {
      "command": "python",
      "args": [
        "-m", "src.mcp_server",
        "--yaml", "C:\\Users\\makot\\Desktop\\memory\\data\\natural_person\\memory_graph.yaml",
        "--llm-model", "deepseek-ai/DeepSeek-V3",
        "--llm-api-key", "sf-xxx",
        "--llm-base-url", "https://api.siliconflow.cn/v1",
        "--embedding-model", "BAAI/bge-m3"
      ],
      "cwd": "C:\\Users\\makot\\Desktop\\memory"
    }
  }
}
```

**本地 Embedding 示例**（无需 embedding API）:

```json
{
  "mcpServers": {
    "ariadne": {
      "command": "python",
      "args": [
        "-m", "src.mcp_server",
        "--yaml", "C:\\Users\\makot\\Desktop\\memory\\data\\natural_person\\memory_graph.yaml",
        "--llm-model", "gpt-4o-mini",
        "--llm-api-key", "sk-xxx",
        "--embedding-model", "BAAI/bge-large-zh-v1.5",
        "--embedding-local"
      ],
      "cwd": "C:\\Users\\makot\\Desktop\\memory"
    }
  }
}
```

| 参数 | 必填 | 说明 |
|------|:---:|------|
| `--yaml` | 是 | YAML 图谱文件路径 |
| `--llm-model` | 否 | LLM 模型名，提供后启用真实 DBA 维护 |
| `--llm-api-key` | 否 | LLM API Key |
| `--llm-base-url` | 否 | LLM API Base URL |
| `--embedding-model` | 否 | Embedding 模型名 |
| `--embedding-api-key` | 否 | Embedding API Key（未指定时复用 `--llm-api-key`） |
| `--embedding-base-url` | 否 | Embedding API Base URL（未指定时复用 `--llm-base-url`） |
| `--embedding-local` | 否 | 使用本地 sentence-transformers 模型（默认使用 API） |
| `--vector-dir` | 否 | 向量索引目录（持久化用） |

Embedding 支持三种模式：
- **API（默认）**：调用任意 OpenAI 兼容 `/v1/embeddings` 端点（OpenAI、SiliconFlow、DeepSeek、本地 vLLM 等）
- **本地**：`--embedding-local` 启用，使用 sentence-transformers，无需网络
- **不使用**：不指定 `--embedding-model`，仅图谱操作，不启用向量检索

---

## 五、不变更的模块

以下模块在工程化中保持不变，仅被上层封装调用：

- `graph_builder.py` — 操作执行与校验逻辑不变
- `dba.py` — System Prompt 和上下文构建不变（新增 checkpoint/restore 方法）
- `retriever.py` — P 检索逻辑不变
- `inference.py` — 状态推断不变（Rerank 已重构为 StoryRank）
- `src/core/jump_axis.py` — 跳转轴规则不变> *命名取自希腊神话：阿里阿德涅（Ariadne）将线团交给忒修斯，助其深入迷宫杀死牛头人后循线返回。*
> 在 Ariadne 系统中：
> - **记忆图谱 = 迷宫** — 复杂、深不可测的联想网络
> - **图边 / 跳转轴 = 阿里阿德涅之线** — 联想检索中唯一正确的引导路径
> - **P 检索 = 循线而行** — 不会被迷宫中的噪音节点吞噬
> - **DBA 维护 = 织线人** — 每次维护都是在加固/修正线索


