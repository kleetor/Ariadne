"""
DBA MCP Server

将 DBA 记忆图谱系统封装为标准 MCP (Model Context Protocol) Server，
任何遵循 MCP 的 LLM Agent 均可通过 stdio 或 SSE 调用。

Usage:
    # 本地 stdio 模式（Agent 直接调用）
    python -m src.mcp_server --yaml your_memory_graph.yaml

    # SSE 网络模式（远程 Agent 调用）
    python -m src.mcp_server --yaml your_memory_graph.yaml --sse --port 8765

依赖:
    pip install mcp
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

# Allow running as module
# Package installed via pip

from dba_pipeline.graph.memory_graph import MemoryGraph
from dba_pipeline.loader import load_graph
from dba_pipeline.core.jump_axis import NodeType, RelationType
from dba_pipeline.core.path_tracker import PathTracker

# ---- 可选 DBA 管线导入 ----
try:
    from dba_pipeline.extraction.dba import MemoryDBA
    from dba_pipeline.extraction.graph_builder import GraphBuilder
    from dba_pipeline.embedding.store import VectorStore, OpenAIEmbeddings, LocalEmbeddings
    from langchain_openai import ChatOpenAI
    HAS_DBA = True
except ImportError:
    HAS_DBA = False

# ---- MCP SDK imports (需要 pip install mcp) ----
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import (
        Tool, ListToolsResult, CallToolResult, TextContent,
        PaginatedRequestParams, CallToolRequestParams,
    )
    HAS_MCP = True
except ImportError:
    HAS_MCP = False


NODE_TYPES = {t.value.upper(): t for t in NodeType}
REL_TYPES = {t.value: t for t in RelationType}


def _split_keywords(query: str) -> list:
    """将查询拆分为多个关键词，支持中英文逗号、顿号、分号、空格等分隔。"""
    return [kw for kw in re.split(r"[,，、;；。.！!？?\s]+", query or "") if kw]


def _bigrams(text: str) -> set:
    """生成字符 bigram 集合，用于中文模糊匹配。"""
    text = re.sub(r"\s+", "", text)
    if not text:
        return set()
    if len(text) == 1:
        return {text}
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _fuzzy_score(keyword: str, content: str) -> float:
    """关键词与内容的匹配分数：精确子串得满分，否则按字符 bigram 相似度。"""
    kw = (keyword or "").lower()
    c = (content or "").lower()
    if not kw:
        return 0.0
    if kw in c:
        return 1.0
    k_bigrams = _bigrams(kw)
    c_bigrams = _bigrams(c)
    if not k_bigrams or not c_bigrams:
        return 0.0
    return len(k_bigrams & c_bigrams) / len(k_bigrams | c_bigrams)


class DBAServer:
    """DBA 核心逻辑，与 MCP 协议层解耦"""

    def __init__(self, graph: MemoryGraph, yaml_path: str = None,
                 dba=None, scheduler=None, retriever=None):
        self.graph = graph
        self.yaml_path = yaml_path
        self.dba = dba          # 可选: MemoryDBA 实例
        self.scheduler = scheduler  # 可选: MaintenanceScheduler 实例
        self.retriever = retriever  # 可选: PurposeDrivenRetriever 实例（完整 P 链路）
        self._next_node_id = self._compute_next_id()

    # ---- 序列化 ----

    def _save(self):
        if not self.yaml_path:
            return
        import yaml
        data = self.graph.to_dict()
        with open(self.yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    def _compute_next_id(self) -> int:
        max_id = 0
        for nid in self.graph.graph.nodes():
            try:
                num = int(nid[1:])
                if num > max_id:
                    max_id = num
            except (ValueError, IndexError):
                pass
        return max_id + 1

    # ---- Tool Handlers ----

    def add_conversation(self, conversation: str) -> dict:
        """追加对话文本，若 DBA 管线已接入则维护图谱"""
        # P0: 优先走调度器批量累积（多轮合并为一次 LLM 维护），降低 token
        if self.scheduler:
            self.scheduler.on_conversation(conversation)
            return {
                "queued": True,
                "buffered": self.scheduler.buffer_size,
                "message": "conversation queued for batched maintenance",
            }
        if self.dba:
            try:
                result = self.dba.maintain(conversation)
                self._save()
                return {
                    "maintained": True,
                    "nodes_created": len(result.get("result", {}).get("created_ids", [])),
                    "stats": {
                        k: v for k, v in self.dba.builder.stats.items()
                        if isinstance(v, (int, float))
                    },
                }
            except Exception as e:
                return {
                    "maintained": False,
                    "error": str(e),
                    "conversation_length": len(conversation),
                }
        return {
            "maintained": False,
            "message": "conversation stored, DBA pipeline not wired (stub)",
            "conversation_length": len(conversation),
        }

    def _search(self, query: str) -> list:
        """关键词模糊检索，返回按匹配分数降序的 [(node_id, score), ...]"""
        keywords = _split_keywords(query) or [query]
        scored = []
        for nid in self.graph.graph.nodes():
            content = self.graph.graph.nodes[nid].get("content", "")
            score = max(_fuzzy_score(kw, content) for kw in keywords)
            if score > 0:
                scored.append((nid, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def query_memory(self, query: str, rerank_k: int = 20) -> dict:
        """检索记忆：优先走完整 P 链路（跳转轴+目的回归+寻峰），否则关键词匹配"""
        # 完整 P 链路（需要 LLM + embedding，由 main 构建 retriever）
        if self.retriever:
            try:
                # 每次检索视为一次联想会话，重置同会话饱和计数（跨会话终身增强保留）
                if self.retriever.path_tracker is not None:
                    self.retriever.path_tracker.start_session()
                result = self.retriever.retrieve(query, seed_k=rerank_k)
                memories = []
                for mid, content in result.get("peak_memories", []):
                    node = self.graph.graph.nodes.get(mid, {})
                    nt = node.get("node_type")
                    memories.append({
                        "id": mid,
                        "content": (content or "")[:200],
                        "node_type": nt.value if hasattr(nt, "value") else str(nt),
                        "deprecated": node.get("deprecated", False),
                        "score": round(result.get("peak_scores", {}).get(mid, 0.0), 4),
                    })
                return {
                    "memories": memories,
                    "total_matched": result.get("total_candidates", 0),
                    "returned": len(memories),
                    "query": query,
                    "purpose": result.get("purpose"),
                    "method": "purpose_driven",
                }
            except Exception:
                # 完整链路异常时回退到关键词匹配
                pass

        # 关键词匹配回退
        scored = self._search(query)
        results = []
        for nid, score in scored[:rerank_k]:
            node = self.graph.graph.nodes[nid]
            nt = node.get("node_type")
            results.append({
                "id": nid,
                "content": node.get("content", "")[:200],
                "node_type": nt.value if hasattr(nt, "value") else str(nt),
                "deprecated": node.get("deprecated", False),
                "score": round(score, 4),
            })

        return {
            "memories": results,
            "total_matched": len(scored),
            "returned": len(results),
            "query": query,
            "method": "keyword_fuzzy",
        }

    def inspect_graph(self, node_id: str = None, topic: str = None, limit: int = 20) -> dict:
        """查看图谱：单节点邻居展开 或 话题搜索"""
        nodes = []
        edges = []

        if node_id and node_id in self.graph.graph.nodes:
            nid = node_id
            node = self.graph.graph.nodes[nid]
            nt = node.get("node_type")
            nodes.append({
                "id": nid,
                "content": node.get("content", ""),
                "node_type": nt.value if hasattr(nt, "value") else str(nt),
                "deprecated": node.get("deprecated", False),
                "forgotten": node.get("forgotten", False),
            })

            # 出边
            for _, tgt, edata in self.graph.graph.out_edges(nid, data=True):
                rt = edata["rel_type"]
                tgt_node = self.graph.graph.nodes[tgt]
                tgt_nt = tgt_node.get("node_type")
                nodes.append({
                    "id": tgt,
                    "content": tgt_node.get("content", ""),
                    "node_type": tgt_nt.value if hasattr(tgt_nt, "value") else str(tgt_nt),
                })
                edges.append({
                    "from": nid,
                    "to": tgt,
                    "type": rt.value if hasattr(rt, "value") else str(rt),
                })

            # 入边
            for src, _, edata in self.graph.graph.in_edges(nid, data=True):
                rt = edata["rel_type"]
                src_node = self.graph.graph.nodes[src]
                src_nt = src_node.get("node_type")
                nodes.append({
                    "id": src,
                    "content": src_node.get("content", ""),
                    "node_type": src_nt.value if hasattr(src_nt, "value") else str(src_nt),
                })
                edges.append({
                    "from": src,
                    "to": nid,
                    "type": rt.value if hasattr(rt, "value") else str(rt),
                })

        elif topic:
            # 话题搜索：在所有节点内容中模糊匹配
            matched = []
            for nid, _score in self._search(topic)[:limit]:
                node = self.graph.graph.nodes[nid]
                nt = node.get("node_type")
                matched.append({
                    "id": nid,
                    "content": node.get("content", "")[:200],
                    "node_type": nt.value if hasattr(nt, "value") else str(nt),
                })
            nodes = matched

        return {"nodes": nodes, "edges": edges}

    def intervene(self, action: str, params: dict) -> dict:
        """人工干预 CRUD 操作"""
        result = {"action": action, "success": True}
        try:
            if action == "create_node":
                nt = NODE_TYPES.get(params.get("node_type", "").upper())
                if not nt:
                    return {"error": f"无效的节点类型: {params.get('node_type')}"}
                nid = f"n{self._next_node_id}"
                self._next_node_id += 1
                self.graph.graph.add_node(
                    nid,
                    content=params.get("content", ""),
                    node_type=nt,
                    metadata={},
                    deprecated=False,
                    forgotten=False,
                )
                result["node"] = {"id": nid, "content": params.get("content", ""),
                                  "node_type": params.get("node_type", "").upper()}
                self._save()

            elif action == "update_node":
                nid = params["node_id"]
                if nid not in self.graph.graph.nodes:
                    return {"error": f"节点不存在: {nid}"}
                node = self.graph.graph.nodes[nid]
                if "content" in params:
                    node["content"] = params["content"]
                if "node_type" in params:
                    nt = NODE_TYPES.get(params["node_type"].upper())
                    if nt:
                        node["node_type"] = nt
                if "deprecated" in params:
                    node["deprecated"] = bool(params["deprecated"])
                result["node"] = {"id": nid, "content": node.get("content", "")}
                self._save()

            elif action == "delete_node":
                nid = params["node_id"]
                if nid not in self.graph.graph.nodes:
                    return {"error": f"节点不存在: {nid}"}
                self.graph.graph.remove_node(nid)
                result["deleted"] = nid
                self._save()

            elif action == "create_edge":
                src, tgt = params["source"], params["target"]
                rt = REL_TYPES.get(params.get("rel_type", "").lower())
                if not rt:
                    return {"error": f"无效的边类型: {params.get('rel_type')}"}
                if src == tgt:
                    return {"error": "不能创建自环边"}
                if self.graph.graph.has_edge(src, tgt):
                    return {"error": f"边已存在: {src} -> {tgt}"}
                self.graph.graph.add_edge(src, tgt, rel_type=rt)
                result["edge"] = {"source": src, "target": tgt, "rel_type": params["rel_type"]}
                self._save()

            elif action == "delete_edge":
                src, tgt = params["source"], params["target"]
                if not self.graph.graph.has_edge(src, tgt):
                    return {"error": f"边不存在: {src} -> {tgt}"}
                self.graph.graph.remove_edge(src, tgt)
                result["deleted"] = f"{src} -> {tgt}"
                self._save()

            else:
                return {"error": f"未知的干预类型: {action}"}

        except Exception as e:
            return {"error": str(e), "action": action}

        return result

    def checkpoint(self, save_dir: str = None) -> dict:
        """保存检查点（若 DBA 管线已接入则保存完整状态）"""
        if self.dba:
            target = save_dir or "snapshots/latest"
            try:
                self.dba.save_checkpoint(target)
                # 额外保存调度器状态
                scheduler_state = None
                if self.scheduler:
                    sched_path = Path(target) / "scheduler_state.json"
                    import json as _json
                    with open(sched_path, "w", encoding="utf-8") as f:
                        _json.dump(self.scheduler.save_state(), f, ensure_ascii=False, indent=2)
                    scheduler_state = "saved"
                return {
                    "save_dir": target,
                    "nodes": self.graph.node_count,
                    "edges": self.graph.edge_count,
                    "vectors": "saved",
                    "builder_state": "saved",
                    "scheduler_state": scheduler_state,
                }
            except Exception as e:
                return {"error": str(e), "save_dir": target}

        # 回退：仅保存 YAML
        import yaml
        target = save_dir or (str(Path(self.yaml_path).parent) if self.yaml_path else "snapshots/latest")
        Path(target).mkdir(parents=True, exist_ok=True)
        out_path = Path(target) / "memory_graph.yaml"
        data = self.graph.to_dict()
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return {
            "save_dir": str(out_path),
            "nodes": self.graph.node_count,
            "edges": self.graph.edge_count,
        }

    def get_stats(self) -> dict:
        """获取系统统计信息"""
        nodes = list(self.graph.graph.nodes(data=True))
        deprecated = sum(1 for _, d in nodes if d.get("deprecated"))
        forgotten = sum(1 for _, d in nodes if d.get("forgotten"))
        return {
            "total_nodes": len(nodes),
            "total_edges": self.graph.edge_count,
            "deprecated": deprecated,
            "forgotten": forgotten,
            "active": len(nodes) - deprecated - forgotten,
            "node_types": {},
        }


# ---- MCP Tool Schema ----

TOOL_SCHEMAS = [
    {
        "name": "dba_add_conversation",
        "description": (
            "当用户在对话中透露新的个人信息、状态、偏好、经历、人际关系、"
            "计划等值得长期记忆的事实时，自动调用此工具记录，无需用户明确要求。"
            "纯寒暄、客套、追问细节等无新信息的内容不需要调用。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation": {
                    "type": "string",
                    "description": "要记录的对话文本",
                }
            },
            "required": ["conversation"],
        },
    },
    {
        "name": "dba_query_memory",
        "description": (
            "在回答用户问题、给出建议或延续话题之前，先调用此工具检索与查询相关的"
            "历史记忆节点，以便结合用户过去的上下文做出更贴合的回答。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，支持逗号分隔多个关键词（如：工作，公司，职业，上班），任一关键词命中即返回",
                },
                "rerank_k": {
                    "type": "integer",
                    "description": "最多返回的节点数（默认 20）。返回结果里的 total_matched 是真实命中总数，若大于 returned 可调大此值获取剩余记忆",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "dba_inspect_graph",
        "description": "查看图谱：指定节点展开邻居，或按话题搜索节点",
        "inputSchema": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "节点 ID（如 n12），展开其 1-hop 邻居",
                },
                "topic": {
                    "type": "string",
                    "description": "搜索话题关键词，匹配节点内容",
                },
                "limit": {
                    "type": "integer",
                    "description": "最多返回的节点数（默认 20）",
                },
            },
        },
    },
    {
        "name": "dba_intervene",
        "description": "人工干预图谱：创建/更新/删除节点或边",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create_node", "update_node", "delete_node", "create_edge", "delete_edge"],
                    "description": "干预操作类型",
                },
                "params": {
                    "type": "object",
                    "description": "操作参数。create_node: {node_type, content}; update_node: {node_id, content?, node_type?, deprecated?}; delete_node: {node_id}; create_edge: {source, target, rel_type}; delete_edge: {source, target}",
                },
            },
            "required": ["action", "params"],
        },
    },
    {
        "name": "dba_checkpoint",
        "description": "保存当前图谱到检查点文件",
        "inputSchema": {
            "type": "object",
            "properties": {
                "save_dir": {
                    "type": "string",
                    "description": "保存目录路径（默认为 yaml 文件所在目录）",
                },
            },
        },
    },
    {
        "name": "dba_get_stats",
        "description": "获取当前图谱的统计信息（节点数、边数、废弃节点数等）",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


def _build_tool_list() -> list:
    """将 TOOL_SCHEMAS 转为 MCP Tool 对象列表"""
    tools = []
    for ts in TOOL_SCHEMAS:
        tools.append(Tool(
            name=ts["name"],
            description=ts["description"],
            input_schema=ts["inputSchema"],
        ))
    return tools


def create_mcp_server(dba: DBAServer) -> "Server":
    """构建 MCP Server 实例 (MCP v2.0 API)"""
    server = Server("ariadne")

    async def handle_list_tools(ctx, params: PaginatedRequestParams):
        return ListToolsResult(tools=_build_tool_list())

    async def handle_call_tool(ctx, params: CallToolRequestParams):
        name = params.name
        arguments = params.arguments or {}

        if name == "dba_add_conversation":
            result = dba.add_conversation(**arguments)
        elif name == "dba_query_memory":
            result = dba.query_memory(**arguments)
        elif name == "dba_inspect_graph":
            result = dba.inspect_graph(**arguments)
        elif name == "dba_intervene":
            result = dba.intervene(**arguments)
        elif name == "dba_checkpoint":
            result = dba.checkpoint(**arguments)
        elif name == "dba_get_stats":
            result = dba.get_stats()
        else:
            result = {"error": f"Unknown tool: {name}"}

        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
        )

    server.add_request_handler("tools/list", PaginatedRequestParams, handle_list_tools)
    server.add_request_handler("tools/call", CallToolRequestParams, handle_call_tool)

    return server


async def run_stdio(server: "Server"):
    """启动 stdio 模式的 MCP Server"""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


async def run_sse(server: "Server", host: str, port: int):
    """启动 SSE 模式的 MCP Server"""
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Mount, Route

    sse = SseServerTransport("/messages/")

    async def handle_sse(request):
        async with sse.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await server.run(
                streams[0], streams[1],
                server.create_initialization_options(),
            )
        # 返回空响应，避免 Starlette 因 endpoint 返回 None 报 "NoneType is not callable"
        return Response()

    starlette_app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )

    config = uvicorn.Config(starlette_app, host=host, port=port, log_level="info")
    http_server = uvicorn.Server(config)
    await http_server.serve()


def _find_dotenv() -> Optional[Path]:
    """从当前文件向上查找项目根目录的 .env 文件"""
    current = Path(__file__).resolve().parent
    for parent in (current, *current.parents):
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate
    return None


def _load_dotenv() -> None:
    """加载 .env 到环境变量（已存在的环境变量优先，不覆盖）"""
    env_path = _find_dotenv()
    if env_path is None:
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _env_flag(name: str) -> bool:
    """将环境变量解析为布尔值（用于 store_true 的默认值）"""
    value = os.environ.get(name)
    return value is not None and value.strip().lower() in ("1", "true", "yes", "on")


def _local_embeddings_available() -> bool:
    """检测 sentence-transformers 是否已安装（EMBEDDING_LOCAL=true 时需要）"""
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


def _print_status(graph, dba, retriever, vector_ready, args) -> None:
    """输出统一的状态日志，提醒用户当前运行模式与检索模式"""
    mode = "完整 DBA" if dba else "存根"
    retrieval = "P 链路（跳转轴+目的回归+寻峰）" if retriever else "关键词模糊匹配"
    llm = args.llm_model or "未配置"
    if args.embedding_model:
        emb = f"{args.embedding_model}（{'本地' if args.embedding_local else 'API'}）"
    else:
        emb = "未配置"
    transport = "SSE" if args.sse else "stdio"

    print("=" * 60, file=sys.stderr)
    print("[DBA MCP] 运行状态", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"  运行模式   : {mode}", file=sys.stderr)
    print(f"  检索模式   : {retrieval}", file=sys.stderr)
    print(f"  LLM        : {llm}", file=sys.stderr)
    print(f"  Embedding  : {emb}", file=sys.stderr)
    print(f"  向量索引   : {'已就绪' if vector_ready else '未启用'}", file=sys.stderr)
    print(f"  传输模式   : {transport}", file=sys.stderr)
    print(f"  图谱规模   : {graph.node_count} 节点 / {graph.edge_count} 边", file=sys.stderr)
    print(f"  工具        : dba_add_conversation / dba_query_memory / dba_inspect_graph / "
          f"dba_intervene / dba_checkpoint / dba_get_stats", file=sys.stderr)
    print("=" * 60, file=sys.stderr)


def main():
    if not HAS_MCP:
        print("错误: MCP SDK 未安装。请先运行: pip install mcp", file=sys.stderr)
        sys.exit(1)

    _load_dotenv()

    parser = argparse.ArgumentParser(description="DBA MCP Server")
    parser.add_argument("--yaml", required=True, help="YAML checkpoint 文件路径")
    parser.add_argument("--sse", action="store_true", help="使用 SSE 网络模式（默认 stdio）")
    parser.add_argument("--host", default="127.0.0.1", help="SSE 绑定地址")
    parser.add_argument("--port", type=int, default=8765, help="SSE 端口")

    # ---- 可选 DBA 管线参数（优先级：命令行 > 环境变量）----
    parser.add_argument("--llm-model", default=os.environ.get("OPENAI_MODEL"),
                        help="LLM 模型名（默认读环境变量 OPENAI_MODEL）")
    parser.add_argument("--llm-api-key", default=os.environ.get("OPENAI_API_KEY"),
                        help="LLM API Key（默认读环境变量 OPENAI_API_KEY）")
    parser.add_argument("--llm-base-url", default=os.environ.get("OPENAI_API_BASE"),
                        help="LLM API Base URL（默认读环境变量 OPENAI_API_BASE）")
    parser.add_argument("--embedding-model", default=os.environ.get("EMBEDDING_MODEL"),
                        help="Embedding 模型名（默认读环境变量 EMBEDDING_MODEL）")
    parser.add_argument("--embedding-api-key", default=os.environ.get("EMBEDDING_API_KEY"),
                        help="Embedding API Key（默认读环境变量 EMBEDDING_API_KEY）")
    parser.add_argument("--embedding-base-url", default=os.environ.get("EMBEDDING_API_BASE"),
                        help="Embedding API Base URL（默认读环境变量 EMBEDDING_API_BASE）")
    parser.add_argument("--embedding-local", action="store_true",
                        default=_env_flag("EMBEDDING_LOCAL"),
                        help="使用本地 sentence-transformers 模型（默认读环境变量 EMBEDDING_LOCAL）")
    parser.add_argument("--vector-dir", default=None, help="向量索引持久化目录")
    args = parser.parse_args()

    print(f"[DBA MCP] 加载图数据: {args.yaml}", file=sys.stderr)
    graph = load_graph(args.yaml)

    # 尝试构建 DBA 管线
    dba_instance = None
    scheduler_instance = None
    retriever_instance = None
    vector_ready = False

    # 前置检测：本地 embedding 依赖是否可用
    local_ok = True
    if args.embedding_model and args.embedding_local and not _local_embeddings_available():
        local_ok = False
        print("[DBA MCP] 警告: EMBEDDING_LOCAL=true 但未安装 sentence-transformers。", file=sys.stderr)
        print("[DBA MCP] 请安装本地依赖后再启动: pip install -e \".[local]\"", file=sys.stderr)
        print("[DBA MCP] 当前将回退到存根模式（不启用真实 DBA 维护）。", file=sys.stderr)

    if args.llm_model and HAS_DBA and local_ok:
        print("[DBA MCP] 构建 DBA 管线...", file=sys.stderr)
        try:
            from dba_pipeline.extraction.maintenance_scheduler import MaintenanceScheduler, ScheduleConfig

            # Embeddings: API 或本地
            if args.embedding_model:
                if args.embedding_local:
                    embeddings = LocalEmbeddings(model_name=args.embedding_model)
                else:
                    embeddings = OpenAIEmbeddings(
                        api_key=args.embedding_api_key or args.llm_api_key or "",
                        api_base=args.embedding_base_url or args.llm_base_url or "",
                        model=args.embedding_model,
                    )
            else:
                embeddings = None

            # VectorStore
            vector_store = VectorStore(
                embeddings=embeddings,
                backend="faiss",
                persist_dir=args.vector_dir,
            )
            if args.vector_dir:
                if os.path.exists(args.vector_dir):
                    vector_store.load(args.vector_dir, embeddings=embeddings)

            # GraphBuilder
            builder = GraphBuilder(graph=graph, vector_store=vector_store)

            # LLM
            llm = ChatOpenAI(
                model=args.llm_model,
                api_key=args.llm_api_key or "not-needed",
                base_url=args.llm_base_url,
                temperature=0,
            )

            # DBA
            dba_instance = MemoryDBA(
                llm=llm,
                graph=graph,
                vector_store=vector_store,
                graph_builder=builder,
            )

            # Scheduler（异步批量维护）
            config = ScheduleConfig()
            scheduler_instance = MaintenanceScheduler(
                dba=dba_instance,
                config=config,
            )

            # 完整检索链路（跳转轴 + 目的回归 + 寻峰终止），失败不影响 DBA 维护
            if embeddings is not None:
                try:
                    from dba_pipeline.llm.inference import InferenceEngine
                    from dba_pipeline.retrieval.retriever import PurposeDrivenRetriever

                    # 启动时把当前图节点灌入向量存储（仅在索引为空时）
                    if vector_store.store is None:
                        mem_ids = [
                            nid for nid in graph.graph.nodes()
                            if graph.graph.nodes[nid].get("content")
                        ]
                        contents = [graph.graph.nodes[nid].get("content") for nid in mem_ids]
                        if mem_ids:
                            vector_store.add_memories(mem_ids, contents)

                    inference = InferenceEngine(llm)
                    path_tracker = PathTracker()
                    retriever_instance = PurposeDrivenRetriever(
                        llm=llm,
                        embeddings=embeddings,
                        graph=graph,
                        vector_store=vector_store,
                        inference=inference,
                        path_tracker=path_tracker,
                    )
                    vector_ready = vector_store.store is not None
                except Exception as e:
                    print(f"[DBA MCP] 检索链路初始化失败（回退关键词匹配）: {e}", file=sys.stderr)

            print(f"[DBA MCP] DBA 管线就绪: LLM={args.llm_model}, "
                  f"检索={'P链路' if retriever_instance else '关键词'}", file=sys.stderr)
        except Exception as e:
            print(f"[DBA MCP] DBA 管线初始化失败: {e}", file=sys.stderr)

    dba_server = DBAServer(graph, yaml_path=args.yaml,
                           dba=dba_instance, scheduler=scheduler_instance,
                           retriever=retriever_instance)

    # P0: 启动调度器，维护完成后自动保存 YAML
    if scheduler_instance:
        scheduler_instance.on_maintenance_done = lambda result: dba_server._save()
        scheduler_instance.start()

    server = create_mcp_server(dba_server)

    _print_status(graph=graph, dba=dba_instance, retriever=retriever_instance,
                  vector_ready=vector_ready, args=args)

    if args.sse:
        import asyncio
        print(f"[DBA MCP] SSE 模式: http://{args.host}:{args.port}/sse", file=sys.stderr)
        asyncio.run(run_sse(server, args.host, args.port))
    else:
        import asyncio
        print("[DBA MCP] stdio 模式", file=sys.stderr)
        asyncio.run(run_stdio(server))


if __name__ == "__main__":
    main()
