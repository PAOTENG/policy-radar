# -*- coding: utf-8 -*-
"""
将合成语料写入 PG 向量库 + ES，并按 graph_manifest 构建知识图谱边。

用法：
  D:\\Environment\\Anaconda\\envs\\agent\\python.exe scripts/ingest_synthetic_kg_corpus.py
  D:\\Environment\\Anaconda\\envs\\agent\\python.exe scripts/ingest_synthetic_kg_corpus.py --skip-es

副作用：
  - 删除旧的 synthetic://yuechuang/* 记录后重写（幂等）
  - 写入 policies / policy_chunks / policy_relations / ES
  - 输出 id_map.json 与 test_cases.json（评测用例）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CORPUS = ROOT / "data" / "synthetic_kg_corpus"
POL_DIR = CORPUS / "policies"
MANIFEST_PATH = CORPUS / "graph_manifest.json"
ID_MAP_PATH = CORPUS / "id_map.json"
TEST_CASES_PATH = CORPUS / "test_cases.json"

URL_PREFIX = "synthetic://yuechuang/"


def parse_md(path: Path) -> tuple[dict, str]:
    """解析 YAML frontmatter + Markdown 正文。"""
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("---", 2)
    if len(parts) < 3:
        return {}, raw
    meta: dict = {}
    for line in parts[1].strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        if line.startswith("- "):
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if val in ("true", "false"):
            meta[key] = val == "true"
        else:
            meta[key] = val
    # 列表项（keywords 等）简单忽略；正文为第三段
    body = parts[2].lstrip("\n")
    return meta, body


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _summary_from_body(title: str, body: str) -> str:
    # 取前几段非标题行
    lines = []
    for line in body.splitlines():
        t = line.strip()
        if not t or t.startswith("#") or t.startswith("|") or t.startswith("---"):
            continue
        if t.startswith("**") and "号" in t:
            continue
        lines.append(re.sub(r"[*_>`]", "", t))
        if sum(len(x) for x in lines) > 220:
            break
    text = "".join(lines)[:280]
    return text or title[:80]


def _key_conditions(doc_id: str, body: str) -> str:
    """按文档注入可比较门槛，便于冲突门控抽槽。"""
    presets = {
        "P01": "注册资本不少于300万元；成立满1年",
        "P02": "注册资本不少于500万元；成立满2年",
        "P04": "注册资本不少于200万元；成立满1年",
        "P05": "注册资本不少于500万元；成立满2年",
        "P06": "注册资本不少于500万元；成立满2年",
        "P07": "注册资本不少于200万元；成立满1年",
        "P08": "注册资本不少于500万元；成立满2年",
        "P14": "注册资本不少于500万元；成立满2年",
        "P15": "注册资本不少于500万元；成立满2年",
        "P19": "注册资本不少于300万元；成立满1年",
        "P20": "注册资本不少于300万元；成立满1年",
    }
    if doc_id in presets:
        return presets[doc_id]
    m = re.findall(r"注册资本不少于\d+万元|成立满\d+年", body)
    return "；".join(dict.fromkeys(m))[:200]


async def embed_text(embedder, text: str, timeout: float = 20.0):
    import asyncio
    return await asyncio.wait_for(embedder.aembed_query(text[:2000]), timeout=timeout)


async def clear_old_synthetic(conn) -> int:
    """删除旧合成数据（级联删 chunks；再删相关边）。"""
    ids = await conn.fetch(
        "SELECT id FROM policies WHERE source_url LIKE $1",
        URL_PREFIX + "%",
    )
    id_list = [r["id"] for r in ids]
    if not id_list:
        return 0
    await conn.execute(
        "DELETE FROM policy_relations WHERE source_policy_id = ANY($1::int[]) OR target_policy_id = ANY($1::int[])",
        id_list,
    )
    await conn.execute("DELETE FROM policies WHERE id = ANY($1::int[])", id_list)
    return len(id_list)


async def save_one(conn, embedder, meta: dict, body: str, url: str) -> int:
    from app.crawler.base import _chunk_text

    title = meta.get("title") or ""
    summary = _summary_from_body(title, body)
    keywords = ["粤创智造", "合成评测", meta.get("doc_id", ""), meta.get("region", "")]
    keywords = [k for k in keywords if k]
    doc_id = meta.get("doc_id", "")
    key_cond = _key_conditions(doc_id, body)

    embed_src = " ".join([title, summary, " ".join(keywords)])
    try:
        embedding = await embed_text(embedder, embed_src)
        vec_clause = "'[" + ",".join(map(str, embedding)) + "]'::vector"
    except Exception as e:
        print(f"[ingest] doc embed fail {doc_id}: {e}", flush=True)
        vec_clause = "NULL"

    row = await conn.fetchrow(
        f"""
        INSERT INTO policies(
            title, publisher, region, pub_date, deadline,
            full_text, summary, keywords,
            category_l1, category_l2, policy_types,
            key_conditions, funding_amount,
            source_url, embedding
        ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,{vec_clause})
        RETURNING id
        """,
        title,
        meta.get("publisher") or None,
        meta.get("region") or "全国",
        _parse_date(meta.get("pub_date")),
        _parse_date(meta.get("deadline")),
        body[:60000],
        summary[:500],
        keywords,
        meta.get("category_l1") or "资金支持",
        meta.get("doc_type") or "",
        [meta.get("doc_type") or "通知"],
        key_cond[:200],
        meta.get("funding_amount") or "",
        url,
    )
    pid = int(row["id"])

    chunks = _chunk_text(body)
    saved = 0
    for idx, chunk in enumerate(chunks):
        try:
            vec = await embed_text(embedder, chunk)
            vec_lit = "[" + ",".join(map(str, vec)) + "]"
            await conn.execute(
                f"INSERT INTO policy_chunks(policy_id,chunk_index,chunk_text,embedding)"
                f" VALUES($1,$2,$3,'{vec_lit}'::vector)",
                pid,
                idx,
                chunk,
            )
            saved += 1
        except Exception as e:
            print(f"[ingest] chunk fail {doc_id}#{idx}: {e}", flush=True)
    print(f"[ingest] {doc_id} -> pg_id={pid} chunks={saved}/{len(chunks)}", flush=True)
    return pid


async def save_es(meta: dict, body: str, pg_id: int, url: str) -> None:
    from elasticsearch import AsyncElasticsearch
    from app.config import get_settings
    from app.rag.es_client import ensure_indices

    await ensure_indices()
    s = get_settings()
    deadline = _parse_date(meta.get("deadline"))
    doc = {
        "title": meta.get("title", ""),
        "full_text": body[:15000],
        "summary": _summary_from_body(meta.get("title", ""), body)[:500],
        "keywords": "粤创智造 合成评测",
        "publisher": meta.get("publisher", ""),
        "region": meta.get("region", "全国"),
        "category_l1": meta.get("category_l1", ""),
        "category_l2": meta.get("doc_type", ""),
        "policy_types": [meta.get("doc_type") or "通知"],
        "deadline": deadline.isoformat() if deadline else None,
        "funding_amount": meta.get("funding_amount", ""),
        "source_url": url,
        "pg_id": pg_id,
    }
    try:
        async with AsyncElasticsearch(hosts=[s.es_host], verify_certs=False) as es:
            await es.index(index=s.es_policy_index, document=doc)
    except Exception as e:
        print(f"[ingest] ES skip {meta.get('doc_id')}: {e}", flush=True)


def generate_test_cases(manifest: dict, id_map: dict[str, int]) -> dict:
    """根据图与金标冲突生成可执行测试用例。"""
    # 邻接表（双向）用于 expand 期望
    adj: dict[str, set[str]] = {}
    for e in manifest["edges"]:
        a, b = e["source"], e["target"]
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    strong = {"implements", "same_program", "lists_under", "funding_of", "supersedes"}
    strong_adj: dict[str, set[str]] = {}
    for e in manifest["edges"]:
        if e["relation_type"] not in strong:
            continue
        a, b = e["source"], e["target"]
        strong_adj.setdefault(a, set()).add(b)
        strong_adj.setdefault(b, set()).add(a)

    expand_cases = []
    for seed in ("P02", "P06", "P04", "P07", "P01"):
        neighbors = sorted(strong_adj.get(seed, []))
        if not neighbors:
            continue
        expand_cases.append({
            "case_id": f"expand_{seed}",
            "type": "graph_expand",
            "seed_doc_id": seed,
            "seed_pg_id": id_map.get(seed),
            "expected_neighbor_doc_ids": neighbors,
            "expected_neighbor_pg_ids": [id_map[n] for n in neighbors if n in id_map],
            "query_hint": {
                "region": next(
                    (n["region"] for n in manifest["nodes"] if n["doc_id"] == seed),
                    "广东",
                ),
                "l1": "资金支持",
                "user_text": "粤创智造 研发费用补贴",
            },
            "assert": "expand_related 后候选中应包含至少 1 个 expected_neighbor_pg_ids",
        })

    conflict_cases = []
    for i, ec in enumerate(manifest["expected_conflicts"], 1):
        a, b = ec["pair"]
        conflict_cases.append({
            "case_id": f"conflict_{i:02d}_{a}_{b}",
            "type": "conflict_gate",
            "pair_doc_ids": [a, b],
            "pair_pg_ids": [id_map.get(a), id_map.get(b)],
            "relation_type": next(
                (
                    e["relation_type"]
                    for e in manifest["edges"]
                    if {e["source"], e["target"]} == {a, b}
                ),
                "same_program",
            ),
            "expected_conflict_types": ec["conflict_types"],
            "note": ec["note"],
            "assert": "check_pair_conflicts / run_conflict_gate 检出类型应与 expected 有交集",
        })

    retrieval_cases = [
        {
            "case_id": "retrieve_yuechuang_gd",
            "type": "hybrid_retrieve",
            "keywords": {
                "l1": "资金支持",
                "l2": "",
                "l3": "研发补贴",
                "region": "广东",
                "user_text": "粤创智造高新技术企业研发费用补贴管理办法",
            },
            "user_input": "广东高新技术企业研发费用补贴申报条件与金额",
            "expect_title_substr": ["粤创智造", "研发费用"],
            "assert": "混合检索结果标题应命中粤创智造相关合成文",
        },
        {
            "case_id": "retrieve_shenzhen_detail",
            "type": "hybrid_retrieve",
            "keywords": {
                "l1": "资金支持",
                "region": "深圳",
                "user_text": "深圳 粤创智造 实施细则",
            },
            "user_input": "深圳研发费用补贴实施细则最高多少万",
            "expect_title_substr": ["深圳", "粤创智造"],
            "assert": "检索应能召回深圳细则或申报通知",
        },
        {
            "case_id": "retrieve_deadline_notice",
            "type": "hybrid_retrieve",
            "keywords": {
                "l1": "资金支持",
                "region": "广东",
                "user_text": "2026年粤创智造申报通知截止",
            },
            "user_input": "2026年粤创智造研发费用补贴什么时候截止申报",
            "expect_title_substr": ["申报", "粤创智造"],
            "assert": "应召回省级或市级申报通知",
        },
    ]

    e2e_cases = [
        {
            "case_id": "e2e_province_city_gate",
            "type": "expand_then_conflict",
            "seed_doc_id": "P02",
            "seed_pg_id": id_map.get("P02"),
            "focus_neighbor_doc_id": "P04",
            "focus_neighbor_pg_id": id_map.get("P04"),
            "expected_conflict_types": ["funding_hierarchy_diff", "eligibility_conflict"],
            "note": "命中省办法后图谱补召深圳细则，应检出金额层级差与门槛冲突",
        },
        {
            "case_id": "e2e_deadline_gate",
            "type": "expand_then_conflict",
            "seed_doc_id": "P06",
            "seed_pg_id": id_map.get("P06"),
            "focus_neighbor_doc_id": "P07",
            "focus_neighbor_pg_id": id_map.get("P07"),
            "expected_conflict_types": ["deadline_conflict"],
            "note": "省申报通知补召深圳通知后应检出截止冲突",
        },
    ]

    return {
        "corpus": "粤创智造合成政策图谱评测集",
        "generated_from": str(MANIFEST_PATH.relative_to(ROOT)),
        "id_map": id_map,
        "cases": {
            "graph_expand": expand_cases,
            "conflict_gate": conflict_cases,
            "hybrid_retrieve": retrieval_cases,
            "e2e": e2e_cases,
        },
        "stats": {
            "expand": len(expand_cases),
            "conflict": len(conflict_cases),
            "retrieve": len(retrieval_cases),
            "e2e": len(e2e_cases),
        },
    }


async def main(skip_es: bool = False) -> None:
    from langchain_openai import OpenAIEmbeddings
    from app.config import get_settings
    from app.db.init import init_db
    from app.db.postgres import get_pool, close_pool

    await init_db()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    s = get_settings()
    embedder = OpenAIEmbeddings(
        model=s.embedding_model,
        api_key=s.embed_api_key,
        base_url=s.embed_base_url,
        check_embedding_ctx_length=False,
    )

    pool = await get_pool()
    async with pool.acquire() as conn:
        deleted = await clear_old_synthetic(conn)
        print(f"[ingest] cleared old synthetic rows={deleted}", flush=True)

    id_map: dict[str, int] = {}
    files = sorted(POL_DIR.glob("*.md"))
    print(f"[ingest] files={len(files)}", flush=True)

    async with pool.acquire() as conn:
        for path in files:
            meta, body = parse_md(path)
            doc_id = meta.get("doc_id") or path.stem.split("_")[0]
            # frontmatter 里 doc_id 可能是 P01
            if not str(doc_id).startswith("P"):
                # 从文件名 01_xxx -> 需对照 manifest filename
                for n in manifest["nodes"]:
                    if n["filename"] == path.name:
                        doc_id = n["doc_id"]
                        meta["doc_id"] = doc_id
                        break
            meta["doc_id"] = doc_id
            url = URL_PREFIX + str(doc_id)
            pid = await save_one(conn, embedder, meta, body, url)
            id_map[str(doc_id)] = pid
            if not skip_es:
                await save_es(meta, body, pid, url)

    from app.rag.relation_builder import upsert_relation

    edge_n = 0
    for e in manifest["edges"]:
        sid, tid = id_map.get(e["source"]), id_map.get(e["target"])
        if not sid or not tid:
            print(f"[graph] skip missing {e.get('source')}->{e.get('target')}", flush=True)
            continue
        ok = await upsert_relation(
            sid,
            tid,
            e["relation_type"],
            confidence=float(e.get("confidence") or 0.8),
            evidence=f"[synthetic] {e.get('evidence', '')}"[:500],
            build_source="synthetic_manifest",
        )
        if ok:
            edge_n += 1

    ID_MAP_PATH.write_text(
        json.dumps(id_map, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    cases = generate_test_cases(manifest, id_map)
    TEST_CASES_PATH.write_text(
        json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 校验边数
    pool = await get_pool()
    async with pool.acquire() as conn:
        syn_edges = await conn.fetchval(
            """
            SELECT COUNT(*) FROM policy_relations
            WHERE build_source = 'synthetic_manifest'
            """
        )
        syn_pols = await conn.fetchval(
            "SELECT COUNT(*) FROM policies WHERE source_url LIKE $1",
            URL_PREFIX + "%",
        )
        syn_chunks = await conn.fetchval(
            """
            SELECT COUNT(*) FROM policy_chunks c
            JOIN policies p ON p.id = c.policy_id
            WHERE p.source_url LIKE $1
            """,
            URL_PREFIX + "%",
        )

    print(
        f"[ingest] done policies={syn_pols} chunks={syn_chunks} "
        f"edges_written≈{edge_n} edges_in_db={syn_edges}",
        flush=True,
    )
    print(f"[ingest] id_map -> {ID_MAP_PATH}", flush=True)
    print(f"[ingest] test_cases -> {TEST_CASES_PATH} stats={cases['stats']}", flush=True)
    await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-es", action="store_true", help="跳过 Elasticsearch 写入")
    args = parser.parse_args()
    asyncio.run(main(skip_es=args.skip_es))
