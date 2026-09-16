"""
政策文档级知识图谱查询与补召回（app/rag/policy_graph.py）

═══════════════════════════════════════════════════════════════════
本文件职责
═══════════════════════════════════════════════════════════════════
  - 定义 8 种文档级关系类型（与 policy_relations.relation_type 一致）
  - 按种子政策 ID 做 1 跳邻居查询
  - expand_related：RAG 命中 A 后强制补全关联政策 B

设计说明：
  - 存储在 PostgreSQL 边表 policy_relations（非图向量库）
  - 向量检索仍走现有 pgvector+ES；本模块只做「结构扩展」
  - 强扩展边（implements/same_program 等）强制并入候选；弱扩展边可选

函数清单：
  1. get_neighbors          查询一批政策的 1 跳邻居边
  2. fetch_policies_by_ids  按 ID 拉取政策全文/元数据
  3. expand_related         主入口：种子列表 → 合并图谱补召回结果
"""
from __future__ import annotations

from app.db.postgres import get_pool


# ── 关系类型本体（文档级，共 8 类）────────────────────────────────
RELATION_TYPES = (
    "region_child",   # 行政区下级（省→市）
    "implements",     # 下级实施上级（市细则实施省办法）
    "same_program",   # 同一认定/专项/补贴项目的不同文件
    "supersedes",     # 新文替代/废止旧文
    "amends",         # 修订/补充旧办法
    "lists_under",    # 公示/名单从属于某申报通知或办法
    "funding_of",     # 资金指南/征集从属于某专项
    "complements",    # 配套/叠加类政策
)

# 命中种子后强制 1 跳补召的关系（高业务价值）
STRONG_EXPAND_TYPES = (
    "implements",
    "same_program",
    "lists_under",
    "funding_of",
    "supersedes",
)

# 弱扩展：进入候选但可被 Grader 淘汰
WEAK_EXPAND_TYPES = (
    "region_child",
    "complements",
    "amends",
)

# 拉取政策行时的公共列（与 retriever 字段对齐，便于下游复用）
_POLICY_COLS = """
    id, title, publisher, region, pub_date, deadline, summary, keywords,
    category_l1, category_l2, policy_types, funding_amount, key_conditions,
    source_url, full_text,
    LEFT(COALESCE(full_text, summary, ''), 800) AS matched_chunk
"""


async def get_neighbors(
    policy_ids: list[int],
    relation_types: tuple[str, ...] | None = None,
    min_confidence: float = 0.3,
) -> list[dict]:
    """查询种子政策的 1 跳邻居边（双向：作为 source 或 target）。

    流程：
      1. 空 ID → 空列表
      2. SQL：匹配 source 或 target 在种子集合内，且对端不在种子内也返回
      3. 按 confidence 降序

    参数：
      policy_ids:      种子政策主键列表
      relation_types:  限定关系类型；None 表示全部
      min_confidence:  最低置信度（噪声边可标 0 隔离）

    返回：
      list[dict]：每条含 source_policy_id / target_policy_id / relation_type /
                  confidence / evidence / neighbor_id / direction
                  （direction: 'out' 种子→邻居，'in' 邻居→种子）

    作用：
      为 expand_related 提供边集合，不直接查政策正文。
    """
    if not policy_ids:
        return []

    types = relation_types or RELATION_TYPES
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                r.source_policy_id,
                r.target_policy_id,
                r.relation_type,
                r.confidence,
                r.evidence,
                r.build_source
            FROM policy_relations r
            WHERE r.confidence >= $1
              AND r.relation_type = ANY($2::text[])
              AND (
                    r.source_policy_id = ANY($3::int[])
                 OR r.target_policy_id = ANY($3::int[])
              )
            ORDER BY r.confidence DESC
            """,
            min_confidence,
            list(types),
            policy_ids,
        )

    seed = set(policy_ids)
    edges: list[dict] = []
    for r in rows:
        src, tgt = r["source_policy_id"], r["target_policy_id"]
        if src in seed and tgt not in seed:
            neighbor_id, direction = tgt, "out"
        elif tgt in seed and src not in seed:
            neighbor_id, direction = src, "in"
        elif src in seed and tgt in seed:
            # 两端都已在种子中：仍记录边，供冲突门控使用，neighbor 取对端
            neighbor_id, direction = tgt, "both"
        else:
            continue
        edges.append({
            "source_policy_id": src,
            "target_policy_id": tgt,
            "relation_type": r["relation_type"],
            "confidence": float(r["confidence"] or 0),
            "evidence": r["evidence"] or "",
            "build_source": r["build_source"] or "",
            "neighbor_id": neighbor_id,
            "direction": direction,
        })
    return edges


async def fetch_policies_by_ids(policy_ids: list[int]) -> list[dict]:
    """按主键批量拉取政策元数据与正文摘要块。

    流程：
      1. 去重、过滤非法 ID
      2. SELECT 公共列，保持传入顺序尽量稳定（ORDER BY array_position）

    参数：
      policy_ids: 政策 ID 列表

    返回：
      list[dict]：与混合检索结果字段兼容的政策字典

    作用：
      把图谱邻居 ID 物化为可进入 Grader/验证器的政策对象。
    """
    ids = [int(x) for x in dict.fromkeys(policy_ids) if x is not None]
    if not ids:
        return []

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT {_POLICY_COLS}
            FROM policies
            WHERE id = ANY($1::int[])
            ORDER BY array_position($1::int[], id)
            """,
            ids,
        )
    return [dict(r) for r in rows]


async def expand_related(
        #返回的是
    seed_policies: list[dict],
    *,
    include_weak: bool = True,
    max_expand: int = 8,
) -> tuple[list[dict], dict]:
    """对 RAG 种子政策做图谱 1 跳补召回，合并去重后返回。

    流程：
      1. 收集种子 id；无 id 则原样返回
      2. 查强扩展边；若 include_weak 再查弱扩展边
      3. 收集尚未在种子中的 neighbor_id，截断至 max_expand
      4. fetch_policies_by_ids 拉正文，打标 from_graph_expand / expand_relation
      5. 种子在前、扩展在后合并；meta 记录 expanded_ids / edges

    参数：
      seed_policies: 检索得到的政策列表（需含 id）
      include_weak:  是否纳入弱扩展关系
      max_expand:    最多新补多少条，防止 context 爆炸

    返回：
      (merged_policies, meta)
      meta 键：
        expanded_ids / expand_count / edges_used / gate_hint

    作用：
      解决「只召回省级 A、漏掉市级实施细则 B」的结构漏召问题。
    """
    if not seed_policies:
        return [], {
            "expanded_ids": [],
            "expand_count": 0,
            "edges_used": [],
            "gate_hint": "no_seeds",
        }

    seed_ids = [p["id"] for p in seed_policies if p.get("id") is not None]
    seed_id_set = set(seed_ids)
    if not seed_ids:
        return list(seed_policies), {
            "expanded_ids": [],
            "expand_count": 0,
            "edges_used": [],
            "gate_hint": "seeds_missing_id",
        }

    strong_edges = await get_neighbors(seed_ids, STRONG_EXPAND_TYPES)
    weak_edges = await get_neighbors(seed_ids, WEAK_EXPAND_TYPES) if include_weak else []
    """
    边的信息：
    {
            "source_policy_id": src,
            "target_policy_id": tgt,
            "relation_type": r["relation_type"],
            "confidence": float(r["confidence"] or 0),
            "evidence": r["evidence"] or "",
            "build_source": r["build_source"] or "",
            "neighbor_id": neighbor_id,
            "direction": direction,
        }
    """
    # 优先强边邻居
    neighbor_meta: dict[int, dict] = {}
    for e in strong_edges + weak_edges:
        nid = e["neighbor_id"]
        if nid in seed_id_set:
            continue
        if nid not in neighbor_meta:
            neighbor_meta[nid] = e
        # 强类型覆盖弱类型
        elif e["relation_type"] in STRONG_EXPAND_TYPES:
            neighbor_meta[nid] = e

    expand_ids = list(neighbor_meta.keys())[:max_expand]
    #前面获取的是种子政策以及知识图谱拓展的政策id，并且输出了边以及其他信息
    #接下来根据这些id去数据库中获取政策的详细信息，包括“政策元数据与正文摘要块”
    fetched = await fetch_policies_by_ids(expand_ids)

    expanded: list[dict] = []
    for p in fetched:
        edge = neighbor_meta.get(p["id"], {})
        item = dict(p)
        item["from_graph_expand"] = True
        item["expand_relation"] = edge.get("relation_type", "")
        item["expand_evidence"] = edge.get("evidence", "")
        item["expand_confidence"] = edge.get("confidence", 0)
        # 无 matched_chunk 时用摘要兜底（fetch 已带 LEFT full_text）
        if not item.get("matched_chunk"):
            item["matched_chunk"] = (item.get("summary") or "")[:800]
        expanded.append(item)

    # 合并：种子优先，扩展追加；边上两端都在种子内的边也记入 edges_used
    merged = list(seed_policies) + expanded
    edges_used = []
    for e in strong_edges + weak_edges:
        edges_used.append({
            "source_policy_id": e["source_policy_id"],
            "target_policy_id": e["target_policy_id"],
            "relation_type": e["relation_type"],
            "confidence": e["confidence"],
            "evidence": e["evidence"],
        })

    meta = {
        "expanded_ids": [p["id"] for p in expanded],
        "expand_count": len(expanded),
        "edges_used": edges_used,
        "gate_hint": "expanded" if expanded else "no_neighbors",
    }
    print(
        f"[PolicyGraph] expand: seeds={len(seed_ids)} "
        f"+expanded={len(expanded)} edges={len(edges_used)}",
        flush=True,
    )
    return merged, meta
