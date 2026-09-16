"""SiliconFlow Reranker API（BAAI/bge-reranker-v2-m3）
超时 5s 时直接返回原始列表（RRF 结果），不调用本地模型。
"""
import asyncio
import httpx
from typing import Optional


async def rerank(
    query: str,
    documents: list[dict],
    top_n: int = 12,
    text_field: str = "summary",
) -> list[dict]:
    """对文档列表重排序，返回得分 >= 0.1 的前 top_n 条
    
    documents: 每条为 dict，至少有 text_field 字段
    返回：重排后的 list[dict]，原 dict 附加 rerank_score 字段
    """
    from app.config import get_settings
    s = get_settings()

    if not s.siliconflow_api_key or not documents:
        return documents[:top_n]

    texts = [str(d.get(text_field) or d.get("title") or "") for d in documents]
    payload = {
        "model": s.siliconflow_rerank_model,
        "query": query,
        "documents": texts,
        "top_n": min(top_n, len(documents)),
        "return_documents": False,
    }
    headers = {
        "Authorization": f"Bearer {s.siliconflow_api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(s.siliconflow_rerank_url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        reranked: list[dict] = []
        for r in results:
            idx = r.get("index", 0)
            score = r.get("relevance_score", 0.0)
            if score >= 0.1 and idx < len(documents):
                doc = dict(documents[idx])
                doc["rerank_score"] = score
                reranked.append(doc)

        print(f"[Reranker] {len(documents)} → {len(reranked)} docs (threshold=0.1)", flush=True)
        return reranked if reranked else documents[:top_n]

    except asyncio.TimeoutError:
        print("[Reranker] API 超时（5s），使用 RRF 结果", flush=True)
        return documents[:top_n]
    except Exception as e:
        print(f"[Reranker] API 异常，使用 RRF 结果: {e}", flush=True)
        return documents[:top_n]
