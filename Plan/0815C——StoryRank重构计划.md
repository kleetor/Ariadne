# StoryRank 重构计划

> 目标：将现有 rerank（对扁平记忆列表做 LLM 重排序）重构为 **StoryRank**，承担三个职责：
>
> 1. **保留检索链路的因果关系**——理解 DAG 中的节点类型与关系类型（CAUSAL / PREFERENCE / SEQUENCE / SCENARIO 等），输出时因果与关联语义不丢失。
> 2. **前置整理记忆片段，避免污染聊天 LLM 上下文**——把检索产出的混乱 `[id] content` 节点列表，提前加工成连贯、易读的故事片段，聊天模型拿到的是干净上下文而非原始节点列举。
> 3. **对检索质量做一次语义过滤**——LLM 按边关系组合故事时，明显突兀、与链路无关的节点自然被舍弃，不纳入故事。

---

## 一、已确认设计决策

| 决策点 | 结论 |
|--------|------|
| 边形态 | **树（单一最优父边）**——每个节点保留权重最高的来源边，回溯唯一，与现有 `expand` 的 max 语义一致 |
| 链路组织 | **按连通分量拆多个片段**——多条独立链路分别生成故事片段 |
| 截断策略 | **两层过滤**：工程连通性粗筛 + StoryRank 语义细筛 |
| 输出格式 | **连贯故事段落**（关系语义自然融入句子） |
| 接入入口 | **仅库内方法** `retrieve_with_story`，暂不改 MCP |
| 过滤可观测性 | **纯故事 + result 附带 `story_nodes` / `discarded_nodes`** |

---

## 二、现状与缺口

当前 `retrieve` 返回结构（[retriever.py#L278-L284](file:///c:/Users/makot/Desktop/Ariadne/dba_pipeline/retrieval/retriever.py#L278-L284)）：

- 有：`peak_memories`（id+content）、`peak_scores`（combined_score）、`purpose`、`hop_history`（含 jump_weight/purpose_score）
- **缺：边的关系类型 `rel_type` 与来源父节点 `from`** —— 这正是「因果链路」的核心，而 `MemoryGraph.expand`（[memory_graph.py#L113-L153](file:///c:/Users/makot/Desktop/Ariadne/dba_pipeline/graph/memory_graph.py#L113-L153)）只返回 `{neighbor: weight}`，把 `rel_type` 丢掉了。

因此重构的第一步是**在检索过程中记录扩展轨迹（DAG）**。

---

## 三、数据流设计

```
retrieve(query)
  └─ expand_with_trace → 记录 {neighbor: {weight, from, rel_type, is_reverse}}
  └─ 输出 result.paths = DAG 轨迹（节点 + 边 + 得分）

retrieve_with_story(query)
  ├─ result = retrieve(query)
  ├─ core = select_core_nodes(result.paths)   # 粗筛：工程连通性筛选
  ├─ story = inference.story_rank(query, core) # 细筛+理解：LLM 按边关系组故事，突兀/无关节点自然舍弃
  └─ response = inference.generate_response(query, [story])
       # ↑ 聊天 LLM 的上下文只有一段干净故事，替代原 [id] content 列举
```

---

## 四、改动清单

### 1. [memory_graph.py](file:///c:/Users/makot/Desktop/Ariadne/dba_pipeline/graph/memory_graph.py) — 新增 `expand_with_trace`

新增方法，在 `expand` 逻辑基础上记录每条候选边的来源：

```python
def expand_with_trace(self, seed_ids, path_tracker=None):
    """扩展一轮并记录候选边的来源
    Returns:
        {neighbor_id: {"weight": float, "from": seed_id,
                       "rel_type": RelationType, "is_reverse": bool}}
    """
```

关键点：同一 `neighbor_id` 可能被多个 seed 指向，保留 `weight` 最高者作为主来源边。

### 2. [retriever.py](file:///c:/Users/makot/Desktop/Ariadne/dba_pipeline/retrieval/retriever.py) — 记录轨迹 + 连通性筛选 + 新入口

- **retrieve 循环改造**（#L143、#L179、#L192-200）：
  - `expanded` 改用 `expand_with_trace`，value 从 `float` 变 `dict`
  - `combined` 每条额外携带 `from` / `rel_type` / `is_reverse`
  - `hop_history` 的 candidates 同步带上这三个字段
- **新增 `_build_trace(hop_history)`**：从 hop_history 组装成 DAG 结构（节点列表 + 边列表）
- **新增 `select_core_nodes(trace, result_ids, seed_ids)`**：连通性筛选，算法见下
- **新增 `retrieve_with_story(query, seed_k=5)`**：编排「检索 → 筛选 → StoryRank → 回复」，替代原 `retrieve_with_response`

**连通性筛选算法**：
1. 以 `trace` 的边（from→to）构建有向 DAG
2. 骨架链：对每个 `result_id`，沿 `from` 回溯到种子，收集路径上的节点
3. 关联分支：与骨架链节点直接相连、且 `combined_score` ≥ 骨架链均分 × 0.6 的节点
4. 丢弃：既不在骨架链、也不满足分支阈值的孤立节点

### 3. [inference.py](file:///c:/Users/makot/Desktop/Ariadne/dba_pipeline/llm/inference.py) — 新增 `story_rank` + Prompt

新增 `STORY_RANK_PROMPT`，输入：
- `{query}`：用户当前消息
- `{path}`：格式化后的链路（节点含类型与内容，边含关系类型）

Prompt 要求 LLM：
1. 理解节点类型与关系类型（给出关系→自然语言映射，如 `CAUSAL→因为/导致`、`PREFERENCE→偏爱`、`SEQUENCE→然后`、`SCENARIO→在…场景下`）
2. 沿链路把记忆组织成连贯故事段落
3. 忠实于节点内容，不编造未出现的事实
4. 关系语义自然融入句子（参照 `text` 示例）
5. 只采纳与链路连贯的记忆；明显突兀、与主线无关的节点应舍弃，不出现在故事中（语义过滤）

新增方法：

```python
def story_rank(self, query: str, path: dict) -> str:
    """把检索因果链路理解成故事化记忆片段文档"""
```

### 4. 测试

- 新增 `tests/test_story_rank.py`：
  - `expand_with_trace` 正确记录 from/rel_type/is_reverse
  - `select_core_nodes` 能剔除孤立节点、保留主链
  - `story_rank` 在 mock LLM 下按预期格式输出（链路→故事）

---

## 五、输出结构

`retrieve_with_story` 返回：

```python
{
    "story": str,               # 故事化记忆片段文档
    "story_nodes": [id, ...],   # 纳入故事的核心节点
    "peak_memories": [...],     # 原始检索结果（向后兼容，供调试）
    "purpose": {...},
    "response": str,            # 注入故事后生成的回复
}
```

---

## 六、验收标准

1. 检索轨迹完整保留 `rel_type` 与 `from`，能还原出 `n1 CAUSAL-> n2 CAUSAL-> n3 ...` 这类链路。
2. `select_core_nodes` 在含分支的图上，只保留主链 + 强关联分支，剔除孤立节点。
3. `story_rank` 输出连贯自然语言段落，关系语义正确融入（不出现「n1 是…」式列举）。
4. `retrieve_with_story` 端到端产出「故事 → 回复」，聊天模型直接可用。
5. 现有测试不受破坏；新增 story rank 测试通过。

---

## 七、风险与权衡

- **DAG 分支规模**：宽度扩展可能产生较多分支，需靠 `select_core_nodes` 收敛；阈值（0.6）需在实现后按样例图调优。
- **StoryRank 一次 LLM 调用**：相比原 rerank（也是 LLM 调用）无额外成本；但把「筛选 + 理解」都交给 prompt 后，需防 LLM 编造。
- **向后兼容**：`retrieve_with_response` 是否删除？建议保留标记 deprecated 或直接替换为 `retrieve_with_story`（P1-5 已定「重设计」，建议直接替换）。

---

## 八、实施结果（已完成）

### 落地决策

| 决策点 | 最终结论 |
|--------|---------|
| 边形态 | 树（单一最优父边），`expand_with_trace` 保留权重最高的来源边 |
| 链路组织 | 按根（种子）分组，`select_core_nodes` 拆成多个核心节点组 |
| 接入入口 | 仅库内方法 `retrieve_with_story`；`retrieve_with_response` 已删除 |
| 过滤可观测性 | `story_rank` 返回 `story` + `adopted_ids`，`retrieve_with_story` 汇总 `story_nodes` / `discarded_nodes` |

### 改动文件

- `graph/memory_graph.py`：新增 `expand_with_trace`
- `retrieval/retriever.py`：检索循环记录轨迹；新增 `_collect_trace_nodes` / `select_core_nodes` / `_build_path` / `retrieve_with_story`；删除 `retrieve_with_response`
- `llm/inference.py`：删除 `RERANK_PROMPT` / `rerank_memories`；新增 `STORY_RANK_PROMPT` / `story_rank`
- `tests/test_story_rank.py`：5 项新增测试

### 验证

- 测试：**38 passed**（原 33 + 新增 5）。
- 真实 LLM（DeepSeek）产物验证：输入 `text` 示例链路，输出连贯故事，CAUSAL/PREFERENCE/SCENARIO 语义自然融入，5 节点全部采纳。

### 关联修复

- MCP `dba_query_memory` 的 `rerank_k` 已解除「`seed_k` = `rerank_k`」的错误等号：`seed_k` 回归默认 5，`rerank_k` 恢复 rank-k 语义（返回条数上限）。
