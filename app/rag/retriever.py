"""混合检索：pgvector 向量 + Elasticsearch BM25 + RRF 融合 + Reranker 重排
支持 Adaptive RAG（路由判断是否需要检索）。
"""
import asyncio
import time
from app.db.postgres import get_pool
from app.rag.es_client import search_policies, get_es
from app.config import get_settings
from langchain_openai import OpenAIEmbeddings


def get_embedder() -> OpenAIEmbeddings:
    s = get_settings()
    return OpenAIEmbeddings(
        model=s.embedding_model,
        api_key=s.embed_api_key,
        base_url=s.embed_base_url,
        # DashScope 不支持 token ID 整数列表输入，必须始终传字符串。
        # check_embedding_ctx_length=True（默认）会用 tiktoken 把长文本编码为 token ID
        # 列表再发送，导致 DashScope 报 "contents is neither str nor list of str"。
        check_embedding_ctx_length=False,
    )


async def _vector_search(
    query: str,
    top_k: int = 15,
    metadata_filter: dict | None = None,
) -> list[dict]:
    """
    Chunk 级向量检索（v3 — 支持 Metadata Pre-Filtering）

    metadata_filter 可包含：
      region    : 地区（如 '广东'），自动同时包含 '全国'
      category_l1: 政策一级分类
      category_l2: 政策二级分类
    
    过滤顺序：先按元数据缩小候选范围，再做向量相似度排序。
    如果过滤后结果 < top_k/2，自动回退到全库搜索（保证召回）。
    """
    embedder = get_embedder()
    try:
        vec = await asyncio.wait_for(
            embedder.aembed_query(query),
            timeout=8.0,
        )
    except asyncio.TimeoutError:
        print("[Retriever] 向量化超时，跳过向量检索", flush=True)
        return []

    vec_lit = "[" + ",".join(map(str, vec)) + "]"

    # ── 构建元数据过滤的 WHERE 子句 ─────────────────────────────
    meta = metadata_filter or {}
    filter_clauses = []
    if meta.get("region") and meta["region"] not in ("全国", "", "all"):
        # 同时包含目标地区 + '全国' 政策
        filter_clauses.append(
            f"p.region IN ('{meta['region']}', '全国')"
        )
    if meta.get("category_l1"):
        filter_clauses.append(
            f"p.category_l1 = '{meta['category_l1']}'"
        )
    if meta.get("category_l2"):
        filter_clauses.append(
            f"p.category_l2 = '{meta['category_l2']}'"
        )
    meta_where = ("AND " + " AND ".join(filter_clauses)) if filter_clauses else ""

    pool = await get_pool()
    async with pool.acquire() as conn:
        chunk_count = await conn.fetchval("SELECT COUNT(*) FROM policy_chunks")

        if chunk_count and chunk_count > 0:
            # ── chunk 级检索 + 元数据预过滤 ───────────────────────
            sql = f"""
                WITH top_chunks AS (
                    SELECT
                        pc.policy_id,
                        pc.chunk_text,
                        1 - (pc.embedding <=> '{vec_lit}'::vector) AS sim,
                        ROW_NUMBER() OVER (
                            PARTITION BY pc.policy_id
                            ORDER BY pc.embedding <=> '{vec_lit}'::vector
                        ) AS rn
                    FROM policy_chunks pc
                    JOIN policies p ON p.id = pc.policy_id
                    WHERE pc.embedding IS NOT NULL {meta_where}
                    ORDER BY pc.embedding <=> '{vec_lit}'::vector
                    LIMIT {top_k * 4}
                )
                SELECT
                    p.id, p.title, p.publisher, p.region,
                    p.deadline, p.summary, p.keywords,
                    p.category_l1, p.category_l2, p.policy_types,
                    p.funding_amount, p.key_conditions,
                    p.score_relevance, p.score_urgency, p.score_value,
                    p.source_url,
                    tc.sim,
                    tc.chunk_text AS matched_chunk
                FROM top_chunks tc
                JOIN policies p ON p.id = tc.policy_id
                WHERE tc.rn = 1
                ORDER BY tc.sim DESC
                LIMIT $1
            """
            rows = await conn.fetch(sql, top_k)

            # 如果过滤太严导致结果不足，回退到全库搜索
            if len(rows) < max(top_k // 2, 3) and meta_where:
                print(
                    f"[Retriever] 元数据过滤结果不足({len(rows)}条)，回退到全库搜索",
                    flush=True
                )
                rows = await conn.fetch(sql.replace(meta_where, ""), top_k)
        else:
            # ── 回退：整文向量检索（旧版兼容）──────────────────────
            print("[Retriever] policy_chunks 为空，回退到整文向量检索", flush=True)
            rows = await conn.fetch(f"""
                SELECT id, title, publisher, region, deadline, summary,
                       keywords, category_l1, category_l2, policy_types,
                       funding_amount, key_conditions,
                       score_relevance, score_urgency, score_value,
                       source_url,
                       1 - (embedding <=> '{vec_lit}'::vector) AS sim,
                       summary AS matched_chunk
                FROM policies
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> '{vec_lit}'::vector
                LIMIT $1
            """, top_k)

    if meta_where:
        print(f"[Retriever] 元数据预过滤: {filter_clauses} → {len(rows)}条", flush=True)
    return [dict(r) for r in rows]


def _rrf_merge(
    vec_results: list[dict],
    bm25_results: list[dict],
    k: int = 60,
    top_n: int = 20,
) -> list[dict]:
    """Reciprocal Rank Fusion 融合两路结果，返回更多候选供 reranker 使用"""
    scores: dict[int, float] = {}
    items: dict[int, dict] = {}

    for rank, item in enumerate(vec_results):
        pid = item["id"]
        scores[pid] = scores.get(pid, 0) + 1 / (k + rank + 1)
        items[pid] = item

    for rank, item in enumerate(bm25_results):
        pid = item.get("pg_id") or item.get("id")
        if pid is None:
            continue
        scores[pid] = scores.get(pid, 0) + 1 / (k + rank + 1)
        if pid not in items:
            items[pid] = item

    ranked = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)
    return [items[pid] for pid in ranked[:top_n]]


async def retrieve(
    keywords: dict,
    user_input: str = "",
    top_n: int = 12,
    use_reranker: bool = True,
) -> list[dict]:
    """混合检索入口，返回最终排序后的政策列表
    
    keywords 示例：
      {l1:'科技', l2:'人工智能', l3:'补贴申报', region:'广东', company_size:'中小企业'}
    
    region / category_l1(l1) / category_l2(l2) 会作为 SQL 元数据预过滤条件，
    大幅缩小向量搜索范围，提升精度和速度。
    """
    t0 = time.perf_counter()

    query = " ".join(filter(None, [
        keywords.get("l1", ""),
        keywords.get("l2", ""),
        keywords.get("l3", ""),
        keywords.get("company_size", ""),  # 公司规模也加入语义查询
        user_input,
    ]))
    kw_for_es = {**keywords, "user_text": user_input}

    # ── 从用户选择中提取元数据预过滤条件 ─────────────────────────
    metadata_filter = {
        "region": keywords.get("region", ""),
        "category_l1": keywords.get("l1", ""),  # 一级分类 → 直接过滤
        # l2/l3 更细，不做强过滤（避免过度收窄），只用于语义搜索
    }

    vec_task = asyncio.create_task(_vector_search(query, top_k=15, metadata_filter=metadata_filter))
    bm25_task = asyncio.create_task(search_policies(kw_for_es, size=15))
    vec_results, bm25_results = await asyncio.gather(vec_task, bm25_task)

    # RRF 合并，保留更多候选给 reranker
    merged = _rrf_merge(vec_results, bm25_results, top_n=20)

    # Reranker 重排（有 API key 时启用）
    if use_reranker and query.strip():
        from app.rag.reranker import rerank
        # matched_chunk 是最相关的 512 字块，比 summary 更精准
        merged = await rerank(query, merged, top_n=top_n, text_field="matched_chunk")
    else:
        merged = merged[:top_n]

    elapsed = int((time.perf_counter() - t0) * 1000)
    print(
        f"[Retriever] vec={len(vec_results)} bm25={len(bm25_results)} "
        f"merged→reranked={len(merged)} ({elapsed}ms)",
        flush=True,
    )
    return merged


async def retrieve_with_routing(
    query: str,
    keywords: dict | None = None,
    user_input: str = "",
    top_n: int = 12,
) -> tuple[str, list[dict]]:
    """带 Adaptive RAG 路由的检索入口
    
    Returns:
        (rag_context_str, policy_list)
        若路由判断不需要检索，返回 ("", [])
    """
    from app.rag.router import should_retrieve

    if not await should_retrieve(query):
        return "", []

    policies = await retrieve(keywords or {}, user_input or query, top_n=top_n)
    if not policies:
        return "", []

    lines = []
    for i, p in enumerate(policies, 1):
        lines.append(
            f"[政策{i}] 《{p.get('title', '未知')}》"
            f"（{p.get('publisher', '')} · {p.get('region', '全国')} · "
            f"截止：{p.get('deadline') or '未知'}）\n"
            f"  {p.get('summary', '') or p.get('key_conditions', '') or '暂无摘要'}"
        )
    return "\n\n".join(lines), policies
