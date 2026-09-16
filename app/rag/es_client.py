"""Elasticsearch 客户端（policy_radar 独立索引，不影响 ShillGuard）"""
from elasticsearch import AsyncElasticsearch
from app.config import get_settings

_es: AsyncElasticsearch | None = None


def get_es() -> AsyncElasticsearch:
    global _es
    if _es is None:
        s = get_settings()
        _es = AsyncElasticsearch(
            hosts=[s.es_host],
            verify_certs=False,
        )
    return _es


POLICY_MAPPING = {
    "mappings": {
        "properties": {
            "title":          {"type": "text",    "analyzer": "ik_max_word"},
            "full_text":      {"type": "text",    "analyzer": "ik_max_word"},
            "summary":        {"type": "text",    "analyzer": "ik_max_word"},
            "publisher":      {"type": "keyword"},
            "region":         {"type": "keyword"},
            "category_l1":    {"type": "keyword"},
            "category_l2":    {"type": "keyword"},
            "policy_types":   {"type": "keyword"},
            "deadline":       {"type": "date",    "format": "yyyy-MM-dd||epoch_millis"},
            "funding_amount": {"type": "keyword"},
            "source_url":     {"type": "keyword", "index": False},
            "pg_id":          {"type": "integer"},
        }
    }
}

COMPETITION_MAPPING = {
    "mappings": {
        "properties": {
            "title":     {"type": "text",    "analyzer": "ik_max_word"},
            "full_text": {"type": "text",    "analyzer": "ik_max_word"},
            "organizer": {"type": "keyword"},
            "level":     {"type": "keyword"},
            "fields":    {"type": "keyword"},
            "deadline":  {"type": "date",    "format": "yyyy-MM-dd||epoch_millis"},
            "pg_id":     {"type": "integer"},
        }
    }
}


async def ensure_indices():
    """确保 ES 索引存在（不存在才创建）"""
    es = get_es()
    s = get_settings()
    if not await es.indices.exists(index=s.es_policy_index):
        await es.indices.create(index=s.es_policy_index, body=POLICY_MAPPING)
        print(f"[ES] 创建索引 {s.es_policy_index}", flush=True)
    if not await es.indices.exists(index=s.es_competition_index):
        await es.indices.create(index=s.es_competition_index, body=COMPETITION_MAPPING)
        print(f"[ES] 创建索引 {s.es_competition_index}", flush=True)


async def search_policies(keywords: dict, size: int = 20) -> list[dict]:
    """BM25 全文检索政策，支持多字段过滤"""
    es = get_es()
    s = get_settings()

    must_clauses = []
    filter_clauses = []

    # 关键词拼成查询文本
    query_text = " ".join(filter(None, [
        keywords.get("l1", ""),
        keywords.get("l2", ""),
        keywords.get("l3", ""),
        keywords.get("user_text", ""),
    ]))

    if query_text.strip():
        must_clauses.append({
            "multi_match": {
                "query": query_text,
                "fields": ["title^3", "summary^2", "full_text"],
            }
        })

    if keywords.get("region") and keywords["region"] != "全国":
        filter_clauses.append({"terms": {"region": [keywords["region"], "全国"]}})

    if keywords.get("l1"):
        filter_clauses.append({"term": {"category_l1": keywords["l1"]}})

    if keywords.get("l2"):
        filter_clauses.append({"term": {"category_l2": keywords["l2"]}})

    body = {
        "query": {
            "bool": {
                "must":   must_clauses or [{"match_all": {}}],
                "filter": filter_clauses,
            }
        },
        "size": size,
        "_source": True,
    }

    resp = await es.search(index=s.es_policy_index, body=body)
    return [hit["_source"] for hit in resp["hits"]["hits"]]
