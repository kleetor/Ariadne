"""
LangChain LLM 封装：状态推断 + 目的推断 + 回复生成
"""

from typing import List, Optional

from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate


# ---- 状态推断 Prompt ----

STATUS_INFERENCE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是一个心理状态分析助手。分析用户当前消息，推断其状态。

输出格式（严格 JSON）：
{{
    "status": "当前状态",
    "emotion": "情绪标签"
}}

状态示例：压力大、疲惫、开心、困惑、焦虑、放松
情绪示例：负面、正面、中性
只输出 JSON。"""),
    ("human", "{query}"),
])

# ---- 目的推断 Prompt ----

PURPOSE_INFERENCE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是一个心理状态分析助手。根据用户的消息，推断用户的隐含状态和目的。

输出格式（严格 JSON）：
{{
    "status": "用户当前状态",
    "purposes": ["目的1", "目的2", "目的3"]
}}

目的应该是简洁的动词或短语，如"缓解"、"倾诉"、"寻求建议"、"理解原因"等。
只输出 JSON。"""),
    ("human", "{query}"),
])

# ---- 记忆联想 Rerank Prompt ----

RERANK_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是一个记忆联想助手。用户说了一句话，你需要从候选记忆中选出用户**最可能自然联想到**的那些记忆。

判断标准（按优先级排序）：
1. 因果链：这个记忆与用户当前状态有因果关系（导致了当前状态 / 是当前状态的结果）
2. 功能等价：这个记忆在功能上与用户当前需求匹配（如用户想放松 → 散步/喝咖啡/听音乐）
3. 情境关联：这个记忆与用户描述的场景属于同一情境（如加班场景 → 办公室/同事/咖啡）
4. 时序关联：这个记忆在时间顺序上与用户描述的事件相邻

注意：
- 不要选只有关键词重叠但实际无关的记忆（"咖啡"→"意大利咖啡文化"不应该被联想）
- 不要选过于宽泛的上位概念
- 选你能确信"用户说这句话时自己也会想到"的记忆

输出格式（严格 JSON）：
{{
    "ranked_ids": ["id1", "id2", ...],
    "reasons": {{
        "id1": "一句话简述联想理由",
        "id2": "一句话简述联想理由"
    }}
}}

按联想自然度降序排列。未被选中的记忆不要出现在 ranked_ids 中。
只输出 JSON。"""),
    ("human", """用户当前消息：
{query}

候选记忆：
{memories}"""),
])

# ---- 回复生成 Prompt ----

RESPONSE_PROMPT = ChatPromptTemplate.from_messages([
    ("system", """你是一个有共情能力的对话助手。根据以下信息生成自然回复。

{context}

要求：
1. 回复自然流畅，如朋友间聊天
2. 如果有相关记忆，自然地融入回复
3. 不要机械地列举信息
4. 保持简洁温暖"""),
    ("human", "{query}"),
])


class InferenceEngine:
    """基于 LangChain ChatOpenAI 的推理引擎"""

    def __init__(self, llm: ChatOpenAI):
        self.llm = llm

    def infer_status(self, query: str) -> dict:
        """推断用户状态和情绪"""
        import json
        import logging
        chain = STATUS_INFERENCE_PROMPT | self.llm
        response = chain.invoke({"query": query})
        try:
            return json.loads(response.content)
        except json.JSONDecodeError:
            logging.warning(f"状态推断 JSON 解析失败，原始响应: {response.content[:200]}")
            return {"status": "unknown", "emotion": "中性"}

    def infer_purpose(self, query: str) -> dict:
        """推断用户隐含目的"""
        import json
        import logging
        chain = PURPOSE_INFERENCE_PROMPT | self.llm
        response = chain.invoke({"query": query})
        try:
            return json.loads(response.content)
        except json.JSONDecodeError:
            logging.warning(f"目的推断 JSON 解析失败，原始响应: {response.content[:200]}")
            return {"status": "unknown", "purposes": ["理解"]}

    def rerank_memories(
        self,
        query: str,
        memories: List[tuple],  # [(memory_id, content), ...]
        top_k: int = 8,
    ) -> List[tuple]:
        """LLM 联想视角重排序：选出用户最可能自然联想到的记忆

        Args:
            query: 用户当前消息
            memories: 候选记忆列表 [(memory_id, content), ...]
            top_k: 保留 top-K 条（0 = 不截断，仅排序）

        Returns:
            [(memory_id, content), ...] 按联想自然度降序
        """
        import json
        import logging

        if not memories:
            return []

        # 格式化候选记忆
        memory_lines = []
        for mid, content in memories:
            memory_lines.append(f"[{mid}] {content}")
        memories_text = "\n".join(memory_lines)

        # 调用 LLM
        chain = RERANK_PROMPT | self.llm
        response = chain.invoke({
            "query": query,
            "memories": memories_text,
        })

        # 解析
        try:
            cleaned = response.content.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                lines = [l for l in lines if not l.startswith("```")]
                cleaned = "\n".join(lines)
            result = json.loads(cleaned)
        except json.JSONDecodeError:
            # fallback: 尝试正则提取
            import re
            ids_match = re.findall(r'"([^"]+)"', response.content)
            logging.warning(f"Rerank JSON 解析失败，降级使用正则提取的 ID: {ids_match[:top_k] if ids_match else '无'}")
            if ids_match:
                result = {"ranked_ids": ids_match, "reasons": {}}
            else:
                return memories[:top_k] if top_k > 0 else memories

        ranked_ids = result.get("ranked_ids", [])

        # 按 LLM 输出顺序重排
        id_to_memory = {mid: (mid, content) for mid, content in memories}
        reranked = []
        for mid in ranked_ids:
            if mid in id_to_memory:
                reranked.append(id_to_memory[mid])

        # 补充 LLM 未提及的记忆（排在后面）
        mentioned = set(ranked_ids)
        for mid, content in memories:
            if mid not in mentioned:
                reranked.append((mid, content))

        # 截断
        if top_k > 0:
            reranked = reranked[:top_k]

        logger = logging.getLogger(__name__)
        logger.info(f"Rerank: {len(memories)} → {len(reranked)} 条, "
                    f"LLM选出 {len(ranked_ids)} 条, top_k={top_k}")

        return reranked

    def generate_response(
        self,
        query: str,
        context_items: List[str],
    ) -> str:
        """注入检索到的记忆上下文，生成回复"""
        if context_items:
            context_text = "【关于用户的已知信息】\n" + "\n".join(
                f"- {item}" for item in context_items
            )
        else:
            context_text = "【关于用户的已知信息】\n暂无相关记忆。"

        chain = RESPONSE_PROMPT | self.llm
        response = chain.invoke({
            "query": query,
            "context": context_text,
        })
        return response.content
