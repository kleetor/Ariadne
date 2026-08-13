"""
LLM DBA：记忆图谱的自主维护者

每次对话后异步触发，LLM 收到三层上下文（对话、记忆、结构），
自主决定对图谱执行 CRUD 操作（新增/更新/修正/废弃节点，新增/删除边）。
"""

import json
import logging
from typing import Dict, List, Optional, Tuple

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate

from dba_pipeline.graph.memory_graph import MemoryGraph
from dba_pipeline.embedding.store import VectorStore
from dba_pipeline.extraction.graph_builder import GraphBuilder

logger = logging.getLogger(__name__)


# ---- System Prompt ----

DBA_SYSTEM_PROMPT = """你是记忆图谱维护者，维护一个关于用户的事实数据库。节点=用户事实，边=语义关系。每次对话后输出 CRUD 维护操作。

## 节点类型（6）
STATUS=客观处境 | REASON=导致状态/行为的原因 | ACTION=主动行为 | THING=物品/地点 | PERSON=社交关系人 | EMOTION=主观情绪
边界：客观处境→STATUS/主观感受→EMOTION；用户做的事→ACTION/导致原因→REASON；物品本身→THING/互动→ACTION

## 边类型（8）
CAUSAL=因→果 | SCENARIO=同场景 | SEQUENCE=先→后 | PREFERENCE=偏好 | SOCIAL=社交 | ATTRIBUTE=属性 | TEMPORAL=事件→时间 | TAXONOMIC=子→父
方向：CAUSAL/SEQUENCE/PREFERENCE/TEMPORAL/TAXONOMIC 单向；SCENARIO/SOCIAL/ATTRIBUTE 双向
边界：A不发生B还会发生?不会=CAUSAL,可能=SEQUENCE；B是环境=SCENARIO/自身属性=ATTRIBUTE；喜欢=PREFERENCE/需要=CAUSAL

## 操作
节点：create(新事实,content为消解指代、补全省略后的完整陈述) | update(事实变化) | fix_type(修正类型) | deprecate(标记过时,不物理删除)
边：create(只连有联想价值的) | delete(删错误/过时的边)

## 原则
1. 只抽用户事实，忽略助手寒暄
2. 第一人称转"用户"，消解指代、补全省略
3. 每条独立可读
4. 边不全连，只连有联想价值的
5. 错了可下次 fix_type/delete 修正
6. deprecate 节点后检查邻居是否需重连
7. 与已有节点矛盾时 deprecate 旧节点+create 新节点

## 边审计
检查上下文中已有边，删除已明显过时或被新事实覆盖的边（偏好改变/因果被否定/不再处于该场景/指向已废弃节点）。本轮未提到但可能仍成立的保留。

## 维护判断
跳过(返回空 ops)：纯寒暄、追问无新事实、延续话题无新信息、已被已有记忆覆盖
需要维护：新事实、事实变化、矛盾、话题切换

## 输出
严格输出 JSON：
{{"node_ops":[{{"action":"create","content":"...","node_type":"ACTION"}},{{"action":"update","target_id":"n3","content":"...","reason":"..."}},{{"action":"fix_type","target_id":"n7","node_type":"STATUS","reason":"..."}},{{"action":"deprecate","target_id":"n3","reason":"..."}}],"edge_ops":[{{"action":"create","from":"n1","to":"n2","rel_type":"CAUSAL"}},{{"action":"delete","from":"n3","to":"n5","reason":"..."}}]}}
无需维护时返回空 node_ops 和 edge_ops。只输出 JSON。"""


# ---- Few-shot 示例 ----

DBA_FEWSHOT_EXAMPLE = """
示例 1 —— 简单新增：

对话上下文：
user(10:00): 最近加班太多了，咖啡都当水喝了
assistant: 那要注意身体啊
user(10:05): 是啊，想戒了，改喝茶吧

记忆上下文：
[n1] REASON: 用户项目上线前频繁加班
[n2] ACTION: 用户靠喝咖啡提神
[n3] THING: 公司楼下有家星巴克

已有边：
n1 --[CAUSAL]--> n2
n2 --[SCENARIO]--> n3

输出：
```json
{
  "node_ops": [
    {"action": "deprecate", "target_id": "n2", "reason": "用户明确表示想戒咖啡，n2描述的靠咖啡提神已过时"},
    {"action": "create", "content": "用户决定改喝茶来替代咖啡提神", "node_type": "ACTION"}
  ],
  "edge_ops": [
    {"action": "create", "from": "n1", "to": "{新节点id}", "rel_type": "CAUSAL"},
    {"action": "delete", "from": "n2", "to": "n3", "reason": "n2已deprecate，n2→n3的场景边不再有效"}
  ]
}
```
"""


# ---- User Prompt 模板 ----

DBA_USER_PROMPT = """── 对话上下文 ──

{conversation}

── 记忆上下文 ──

与本轮对话相关的已有节点：
{current_nodes}

已有边：
{current_edges}

── 结构上下文 ──

相关节点的一跳邻居：
{neighbor_info}

图谱概况：总节点 {total_nodes} 个，总边 {total_edges} 条

── 请输出维护操作 ──"""


# ---- 维护判断（triage）Prompt ----

DBA_TRIAGE_PROMPT = """判断对话是否包含值得长期记忆的用户新事实，只回答 NEEDED 或 SKIP。

SKIP：纯寒暄/打招呼、追问细节无新事实、延续话题无新信息。
NEEDED：新事实、事实变化、与已有记忆矛盾、话题切换。

对话：
{conversation}

回答（NEEDED 或 SKIP）："""


# ---- DBA 类 ----

class MemoryDBA:
    """记忆图谱的 LLM 数据库管理员"""

    def __init__(
        self,
        llm: ChatOpenAI,
        graph: MemoryGraph,
        vector_store: VectorStore,
        graph_builder: GraphBuilder,
        current_state_k: int = 15,
    ):
        """
        Args:
            llm: LangChain ChatOpenAI 实例
            graph: 目标 MemoryGraph
            vector_store: 向量存储
            graph_builder: GraphBuilder 实例
            current_state_k: 记忆上下文检索多少条相关节点
        """
        self.llm = llm
        self.graph = graph
        self.vector_store = vector_store
        self.builder = graph_builder
        self.current_state_k = current_state_k

        # 组装 Prompt 链
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", DBA_SYSTEM_PROMPT),
            ("human", DBA_USER_PROMPT),
        ])
        self.chain = self.prompt | self.llm

        # 维护判断前置（triage）：先用极小 prompt 判断是否值得维护
        self.triage_chain = ChatPromptTemplate.from_messages([
            ("system", DBA_TRIAGE_PROMPT),
        ]) | self.llm

    # ---- 主入口 ----

    def maintain(
        self,
        conversation: str,
    ) -> Dict:
        """对话完成后执行一次数据库维护

        Args:
            conversation: 本轮对话的完整文本（含角色和时间戳）

        Returns:
            {
                "ops": {"node_ops": [...], "edge_ops": [...]},  # LLM 原始输出
                "result": {"created_ids": [...], "skipped": [...], "errors": [...]},  # 执行结果
                "context": {...},  # 组装的三层上下文（调试用）
            }
        """
        # 0. 维护判断前置：明显无需维护的对话直接跳过，省掉完整 prompt
        if not self._should_maintain(conversation):
            logger.info("DBA 维护: triage 判定跳过")
            return {
                "ops": {"node_ops": [], "edge_ops": []},
                "result": {"created_ids": [], "skipped": [], "errors": []},
                "context": None,
                "skipped": True,
            }

        # 1. 组装三层上下文
        context = self._build_context(conversation)

        # 2. 调用 LLM
        response = self._call_llm(context)

        # 3. 解析操作指令
        ops = self._parse_response(response)

        # 4. 执行操作
        result = self.builder.apply_ops(
            node_ops=ops.get("node_ops", []),
            edge_ops=ops.get("edge_ops", []),
        )

        logger.info(
            f"DBA 维护完成: 创建 {self.builder.stats['nodes_created']} 节点, "
            f"更新 {self.builder.stats['nodes_updated']}, "
            f"修正 {self.builder.stats['nodes_fixed']}, "
            f"废弃 {self.builder.stats['nodes_deprecated']}, "
            f"创建边 {self.builder.stats['edges_created']}, "
            f"删除边 {self.builder.stats['edges_deleted']}"
        )

        return {
            "ops": ops,
            "result": result,
            "context": context,
        }

    # ---- 上下文组装 ----

    def _build_context(self, conversation: str) -> Dict:
        """组装三层上下文"""
        # 第一层：对话上下文（直接使用传入的完整对话）
        conversation_text = conversation

        # 第二层：记忆上下文（语义检索 + 已有边）
        current_nodes_text, current_edges_text = self._build_memory_context(conversation)

        # 第三层：结构上下文（一跳邻居 + 图谱统计）
        neighbor_text = self._build_structure_context(conversation)

        return {
            "conversation": conversation_text,
            "current_nodes": current_nodes_text,
            "current_edges": current_edges_text,
            "neighbor_info": neighbor_text,
            "total_nodes": self.graph.node_count,
            "total_edges": self.graph.edge_count,
        }

    def _build_memory_context(self, conversation: str) -> Tuple[str, str]:
        """构建记忆上下文：语义相关节点 + 已有边"""
        if self.graph.node_count == 0:
            return "（暂无已有记忆）", "（暂无已有边）"

        results = self.vector_store.search(conversation, k=self.current_state_k)

        node_lines = []
        shown_ids = set()
        for mid, score, _ in results:
            node = self.graph.get_node(mid)
            if node is None or node.get("deprecated"):
                continue
            type_val = node["node_type"].value if hasattr(node["node_type"], "value") else str(node["node_type"])
            content = node["content"][:100]
            node_lines.append(f"  [{mid}] {type_val}: {content}")
            shown_ids.add(mid)

        # 收集这些节点之间的边
        edge_lines = []
        shown_edges = set()
        for u in shown_ids:
            for v in shown_ids:
                if u == v:
                    continue
                if self.graph.graph.has_edge(u, v):
                    edge_key = (u, v)
                    if edge_key in shown_edges:
                        continue
                    shown_edges.add(edge_key)
                    edge_data = self.graph.graph.edges[u, v]
                    rel_type = edge_data.get("rel_type")
                    rel_str = rel_type.value if hasattr(rel_type, "value") else str(rel_type)
                    edge_lines.append(f"  {u} --[{rel_str}]--> {v}")

        nodes_text = "\n".join(node_lines) if node_lines else "（未找到相关已有记忆）"
        edges_text = "\n".join(edge_lines) if edge_lines else "（相关节点间暂无直接边）"

        return nodes_text, edges_text

    def _build_structure_context(self, conversation: str) -> str:
        """构建结构上下文：相关节点的一跳邻居"""
        if self.graph.node_count == 0:
            return "（暂无图谱结构）"

        results = self.vector_store.search(conversation, k=5)
        neighbor_set = set()
        for mid, _, _ in results:
            node = self.graph.get_node(mid)
            if node is None or node.get("deprecated"):
                continue
            for neighbor_id, rel_type, is_reverse in self.graph.get_neighbors(mid):
                neighbor_node = self.graph.get_node(neighbor_id)
                if neighbor_node and not neighbor_node.get("deprecated"):
                    direction = "←" if is_reverse else "→"
                    rel_str = rel_type.value if hasattr(rel_type, "value") else str(rel_type)
                    neighbor_set.add(f"  {mid} --{direction}[{rel_str}]-- {neighbor_id}")

        if neighbor_set:
            return "\n".join(sorted(neighbor_set)[:20])
        return "（相关节点暂无邻居）"

    # ---- LLM 调用 ----

    def _should_maintain(self, conversation: str) -> bool:
        """维护判断前置：极小 prompt 判断是否值得维护"""
        snippet = conversation[-500:]
        resp = self.triage_chain.invoke({"conversation": snippet})
        return "NEEDED" in resp.content.upper()

    def _call_llm(self, context: Dict) -> str:
        """调用 LLM 获取维护操作"""
        response = self.chain.invoke(context)
        return response.content

    # ---- 响应解析 ----

    def _parse_response(self, response: str) -> Dict:
        """解析 LLM 输出，提取 node_ops 和 edge_ops"""
        # 尝试直接解析 JSON
        cleaned = response.strip()

        # 去掉可能的 markdown 代码块包裹
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            # 去掉第一行 ```json 和最后一行 ```
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines)

        try:
            ops = json.loads(cleaned)
            return {
                "node_ops": ops.get("node_ops", []),
                "edge_ops": ops.get("edge_ops", []),
            }
        except json.JSONDecodeError:
            logger.warning(f"LLM 输出 JSON 解析失败，原始响应前 200 字符: {response[:200]}")

            # 尝试提取 JSON 片段
            return self._extract_json_fallback(response)

    def _extract_json_fallback(self, response: str) -> Dict:
        """从非标准格式的 LLM 输出中尝试提取 JSON"""
        # 尝试找到第一个 { 和最后一个 }
        start = response.find("{")
        end = response.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(response[start:end + 1])
            except json.JSONDecodeError:
                pass

        logger.error(f"无法从 LLM 输出中提取有效的 JSON 操作指令")
        return {"node_ops": [], "edge_ops": []}

    # ---- 检查点 ----

    def save_checkpoint(self, save_dir: str):
        """保存完整检查点（图谱 + 向量 + 构建器状态）"""
        import yaml
        import json
        import os
        from datetime import datetime

        os.makedirs(save_dir, exist_ok=True)

        # 1. 图谱 → YAML
        graph_path = os.path.join(save_dir, "memory_graph.yaml")
        with open(graph_path, "w", encoding="utf-8") as f:
            yaml.dump(self.graph.to_dict(), f, allow_unicode=True,
                      default_flow_style=False, sort_keys=False)

        # 2. 向量存储
        vs_path = os.path.join(save_dir, "faiss_index")
        if self.vector_store and self.vector_store.has_vectors():
            self.vector_store.save(vs_path)

        # 3. 构建器状态 → JSON
        builder_path = os.path.join(save_dir, "builder_state.json")
        with open(builder_path, "w", encoding="utf-8") as f:
            json.dump(self.builder.save_state(), f, ensure_ascii=False)

        # 4. 检查点元数据
        checkpoint_path = os.path.join(save_dir, "checkpoint.json")
        with open(checkpoint_path, "w", encoding="utf-8") as f:
            json.dump({
                "version": "1",
                "timestamp": datetime.now().isoformat(),
                "nodes": self.graph.node_count,
                "edges": self.graph.edge_count,
            }, f, ensure_ascii=False, indent=2)

        logger.info(f"检查点已保存: {save_dir} ({self.graph.node_count} 节点, {self.graph.edge_count} 边)")
        return save_dir

    def restore_checkpoint(self, save_dir: str) -> bool:
        """从检查点恢复全部状态"""
        import yaml
        import json
        import os

        # 1. 图谱
        graph_path = os.path.join(save_dir, "memory_graph.yaml")
        if os.path.exists(graph_path):
            restored = MemoryGraph.from_dict(
                yaml.safe_load(open(graph_path, encoding="utf-8"))
            )
            self.graph.graph = restored.graph  # 替换内部 nx 图
            logger.info(f"图谱已恢复: {self.graph.node_count} 节点, {self.graph.edge_count} 边")
        else:
            logger.warning("未找到图谱检查点")

        # 2. 向量存储
        vs_path = os.path.join(save_dir, "faiss_index")
        if os.path.exists(vs_path):
            self.vector_store.load(vs_path, embeddings=self.vector_store.embeddings)

        # 3. 构建器状态
        builder_path = os.path.join(save_dir, "builder_state.json")
        if os.path.exists(builder_path):
            with open(builder_path, encoding="utf-8") as f:
                self.builder.load_state(json.load(f))
            logger.info("构建器状态已恢复")

        return True
