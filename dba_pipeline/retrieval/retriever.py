"""
完整检索入口：串联跳转轴 + 目的回归 + 寻峰终止

论文第六章 Algorithm: Purpose-Driven Associative Retrieval
"""

import numpy as np
from typing import List, Tuple, Dict, Optional

from langchain_openai import ChatOpenAI
from langchain_core.embeddings import Embeddings

from dba_pipeline.core.jump_axis import get_jump_weight
from dba_pipeline.core.purpose import PurposeModel
from dba_pipeline.core.peak_find import PeakFinder
from dba_pipeline.graph.memory_graph import MemoryGraph
from dba_pipeline.llm.inference import InferenceEngine
from dba_pipeline.embedding.store import VectorStore


class PurposeDrivenRetriever:
    """目的驱动的联想记忆检索器（完整链路）"""

    def __init__(
        self,
        llm: ChatOpenAI,
        embeddings: Embeddings,
        graph: MemoryGraph,
        vector_store: VectorStore,
        inference: InferenceEngine,
        distance_decay: float = 0.85,
        jump_weight_coef: float = 0.5,
        purpose_weight_coef: float = 0.5,
        purpose_filter_threshold: float = 0.2,
        purpose_filter_decay: float = 0.0,
        path_tracker=None,
    ):
        self.llm = llm
        self.embeddings = embeddings
        self.graph = graph
        self.vector_store = vector_store
        self.inference = inference

        self.purpose_model = PurposeModel(llm, self._embed)
        self.peak_finder = PeakFinder(patience=2, min_delta=0.015)

        self.distance_decay = distance_decay
        self.jump_weight_coef = jump_weight_coef
        self.purpose_weight_coef = purpose_weight_coef
        self.purpose_filter_threshold = purpose_filter_threshold
        self.purpose_filter_decay = purpose_filter_decay
        self.path_tracker = path_tracker  # 可选：PathTracker 实例

    def _get_hop_threshold(self, hop: int) -> float:
        """计算当前跳数的有效过滤阈值

        若 purpose_filter_decay > 0，使用递减公式：
            threshold_hop = purpose_filter_threshold × purpose_filter_decay^hop
        否则使用固定阈值。
        """
        if self.purpose_filter_decay > 0.0:
            return self.purpose_filter_threshold * (self.purpose_filter_decay ** hop)
        return self.purpose_filter_threshold

    def _embed(self, text: str) -> np.ndarray:
        return self.vector_store._embed(text)

    # ---- 算法主流程 ----

    def retrieve(
        self,
        query: str,
        seed_k: int = 5,
        max_hops: int = 5,
        expand_k: int = None,
    ) -> Dict:
        """执行完整的目的驱动联想检索

        Returns:
            {
                "peak_memories": [(memory_id, content), ...],
                "peak_scores": {memory_id: combined_score, ...},
                "purpose": {"status": str, "purposes": [str]},
                "hop_history": [{"hop": int, "candidates": [...], "mean_score": float}, ...],
                "total_candidates": int,
            }
        """
        # Step 1: 推断状态和目的
        purpose_info = self.inference.infer_purpose(query)
        purposes = purpose_info.get("purposes", [])
        purpose_vec = self.purpose_model.get_purpose_vector(purposes)

        # Step 2: 向量搜索种子记忆（混合 query text + 目的向量）
        half_k = max(1, seed_k // 2)
        text_seeds = self.vector_store.search(query, k=half_k)
        purpose_seeds = self.vector_store.search_by_vector(purpose_vec, k=seed_k - half_k)

        # 合并去重，保持 text 种子优先顺序，purpose 种子补充
        seen = set()
        seed_ids = []
        for r in text_seeds:
            if r[0] not in seen:
                seen.add(r[0])
                seed_ids.append(r[0])
        for r in purpose_seeds:
            if r[0] not in seen and len(seed_ids) < seed_k:
                seen.add(r[0])
                seed_ids.append(r[0])
        # 不足时用更多 text 种子补齐
        if len(seed_ids) < seed_k:
            extra = self.vector_store.search(query, k=seed_k * 2)
            for r in extra:
                if r[0] not in seen and len(seed_ids) < seed_k:
                    seen.add(r[0])
                    seed_ids.append(r[0])
        # 计算种子轮目的关联度（使用预缓存向量，无 API 调用）
        seed_vectors = self.vector_store.get_content_vectors(seed_ids)
        seed_scores = self.purpose_model.compute_purpose_score_from_vectors(
            seed_vectors, purpose_vec
        )
        # 种子轮没有跳转轴扩展（jw=0），使用与后续轮次一致的得分公式
        mu_0 = float(np.mean(seed_scores)) * self.purpose_weight_coef

        self.peak_finder.reset()
        decision = self.peak_finder.add_round(mu_0)
        if decision == "peak_found":
            return self._build_result(seed_ids, purpose_info, [])

        # Step 3-6: 循环扩展
        current_ids = list(seed_ids)
        seed_contents = self.graph.get_contents(seed_ids)
        hop_history: List[Dict] = [{
            "hop": 0,
            "candidates": [
                {"id": mid, "content": content, "purpose_score": float(s)}
                for mid, content, s in zip(seed_ids, seed_contents, seed_scores)
            ],
            "mean_score": mu_0,
        }]

        for hop in range(1, max_hops + 1):
            # 跳转轴扩展（含可选 PathTracker 动态权重）
            expanded = self.graph.expand(current_ids, path_tracker=self.path_tracker)

            # 记录路径激活（供 PathTracker 分析）
            if self.path_tracker is not None:
                for seed_id in current_ids:
                    source_type = self.graph.get_node_type(seed_id)
                    if source_type is None:
                        continue
                    for neighbor_id, rel_type, is_reverse in self.graph.get_neighbors(seed_id):
                        if neighbor_id in seed_ids:
                            continue
                        w = get_jump_weight(source_type, rel_type, is_reverse)
                        if w > 0 and neighbor_id in expanded:
                            pk = self.path_tracker.make_path_key(
                                seed_id, neighbor_id, rel_type.value
                            )
                            self.path_tracker.record_activation(pk)
            if not expanded:
                result_ids = current_ids
                break

            # 目的过滤（使用预缓存向量，无 API 调用）
            candidate_ids = list(expanded.keys())
            candidate_contents = self.graph.get_contents(candidate_ids)
            candidate_vectors = self.vector_store.get_content_vectors(candidate_ids)
            candidate_scores = self.purpose_model.compute_purpose_score_from_vectors(
                candidate_vectors, purpose_vec
            )

            filtered: Dict[str, Tuple[str, float, float]] = {}
            # {mem_id: (content, jump_weight, purpose_score)}
            hop_threshold = self._get_hop_threshold(hop)
            for i, mid in enumerate(candidate_ids):
                ps = float(candidate_scores[i])
                if ps < hop_threshold:
                    continue
                filtered[mid] = (
                    candidate_contents[i],
                    expanded[mid],
                    ps,
                )

            if not filtered:
                result_ids = current_ids
                break

            # 距离衰减 + 组合得分
            decay = self.distance_decay ** hop
            combined = {}
            for mid, (content, jw, ps) in filtered.items():
                combined[mid] = {
                    "id": mid,
                    "content": content,
                    "jump_weight": jw,
                    "purpose_score": ps,
                    "combined_score": jw * decay * self.jump_weight_coef
                                      + ps * self.purpose_weight_coef,
                }

            # 当前轮综合得分均值（跳转轴 + 目的，用于寻峰）
            mu_hop = float(np.mean([v["combined_score"] for v in combined.values()]))

            # 寻峰判断
            decision = self.peak_finder.add_round(mu_hop)

            hop_history.append({
                "hop": hop,
                "candidates": sorted(
                    combined.values(),
                    key=lambda x: x["combined_score"],
                    reverse=True,
                ),
                "mean_score": mu_hop,
            })

            if decision == "peak_found":
                result_ids = self._collect_peak_tolerance(hop_history)
                break

            # 当前轮候选用作下一轮扩展
            top_k = expand_k if expand_k is not None else seed_k
            current_ids = [
                c["id"]
                for c in sorted(
                    combined.values(),
                    key=lambda x: x["combined_score"],
                    reverse=True,
                )[:top_k]
            ]

        else:
            # 达到 max_hops 仍未找到峰值，用峰值容忍带
            result_ids = self._collect_peak_tolerance(hop_history)

        return self._build_result(result_ids, purpose_info, hop_history)

    def _collect_peak_tolerance(self, hop_history: List[Dict]) -> List[str]:
        """峰值容忍带：取 μ ≥ peak_mean - tolerance 的所有轮候选"""
        peak_mean = self.peak_finder.history[self.peak_finder.peak_index]
        threshold = peak_mean - self.peak_finder.peak_tolerance
        result_ids = []
        seen = set()
        for h in hop_history:
            mu = h.get("mean_score", 0)
            if mu >= threshold:
                for c in h.get("candidates", []):
                    cid = c.get("id", "")
                    if cid not in seen:
                        seen.add(cid)
                        result_ids.append(cid)
        return result_ids

    def _build_result(
        self,
        result_ids: List[str],
        purpose_info: dict,
        hop_history: List[Dict],
    ) -> Dict:
        # 从 hop_history 收集每条的 combined_score
        id_score = {}
        for h in hop_history:
            for c in h.get("candidates", []):
                cid = c.get("id", "")
                cs = c.get("combined_score", 0)
                if cid not in id_score or cs > id_score[cid]:
                    id_score[cid] = cs

        memories = []
        for mid in result_ids:
            content = self.graph.get_content(mid)
            if content:
                memories.append((mid, content))

        # 按 combined_score 降序排列（作为 LLM rerank 前的默认排序）
        memories.sort(key=lambda x: id_score.get(x[0], 0), reverse=True)
        return {
            "peak_memories": memories,  # [(id, content), ...] 保持向后兼容
            "peak_scores": id_score,    # {id: combined_score} 供 rerank 使用
            "purpose": purpose_info,
            "hop_history": hop_history,
            "total_candidates": len(memories),
        }

    def retrieve_with_response(
        self,
        query: str,
        seed_k: int = 5,
        rerank_k: int = 8,
    ) -> Dict:
        """检索 → LLM 联想重排序 → 截断 → 生成回复

        Args:
            query: 用户当前消息
            seed_k: 向量检索种子数
            rerank_k: 重排序后保留的 top-K 条记忆（0 = 仅排序不截断）
        """
        result = self.retrieve(query, seed_k=seed_k)

        # 用 peak_scores 重排（确保 retrieve 阶段的排序顺序）
        peak_memories = result["peak_memories"]  # [(id, content), ...]
        peak_scores = result.get("peak_scores", {})
        if peak_scores:
            peak_memories.sort(key=lambda x: peak_scores.get(x[0], 0), reverse=True)

        # LLM 联想视角重排序 + 截断
        if peak_memories and rerank_k > 0 and len(peak_memories) > rerank_k:
            reranked = self.inference.rerank_memories(query, peak_memories, top_k=rerank_k)
            result["reranked_memories"] = reranked
            result["reranked"] = True
            context_items = [f"[{mid}] {content}" for mid, content in reranked]
        else:
            result["reranked"] = False
            context_items = [f"[{mid}] {content}" for mid, content in peak_memories]

        response = self.inference.generate_response(query, context_items)
        result["response"] = response
        return result
