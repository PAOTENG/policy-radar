"""
管理员上传政策文件 → 解析 → 向量化 → ES → 知识图谱建边

供 app/api/admin_kb.py 调用；无鉴权（内网管理端直连）。
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, date
from pathlib import Path

from langchain_openai import OpenAIEmbeddings

from app.config import get_settings
from app.db.postgres import get_pool
from app.crawler.base import _chunk_text
from app.rag.relation_builder import (
    upsert_relation,
    llm_extract_edges,
    is_noise_policy,
    _title_prefix,
    _title_doctype,
)
from app.rag.policy_graph import RELATION_TYPES


def _get_embedder() -> OpenAIEmbeddings:
    s = get_settings()
    return OpenAIEmbeddings(
        model=s.embedding_model,
        api_key=s.embed_api_key,
        base_url=s.embed_base_url,
        check_embedding_ctx_length=False,
    )


async def extract_text_from_upload(filename: str, raw: bytes) -> str:
    """按扩展名抽取纯文本。"""
    name = filename.lower()
    if name.endswith((".md", ".txt", ".markdown", ".csv")):
        for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="ignore")

    if name.endswith(".pdf"):
        try:
            from pypdf import PdfReader
            import io
            reader = PdfReader(io.BytesIO(raw))
            parts = [(p.extract_text() or "") for p in reader.pages]
            return "\n".join(parts).strip()
        except ImportError:
            raise ValueError("未安装 pypdf，无法解析 PDF。请 pip install pypdf，或改传 MD/TXT。")
        except Exception as e:
            raise ValueError(f"PDF 解析失败: {e}") from e

    if name.endswith(".docx"):
        try:
            import io
            from docx import Document
            doc = Document(io.BytesIO(raw))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip()).strip()
        except ImportError:
            raise ValueError("未安装 python-docx，无法解析 DOCX。请 pip install python-docx，或改传 MD/TXT。")
        except Exception as e:
            raise ValueError(f"DOCX 解析失败: {e}") from e

    raise ValueError("暂不支持该格式，请上传 MD / TXT / PDF / DOCX")


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """可选 YAML frontmatter。"""
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict = {}
    for line in parts[1].strip().splitlines():
        if ":" not in line or line.strip().startswith("-"):
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, parts[2].lstrip("\n")


def _guess_title(filename: str, body: str, meta: dict) -> str:
    if meta.get("title"):
        return meta["title"][:200]
    for line in body.splitlines():
        t = line.strip().lstrip("#").strip()
        if t and len(t) > 4:
            return t[:200]
    return Path(filename).stem[:200]


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _summary(body: str, title: str) -> str:
    lines = []
    for line in body.splitlines():
        t = re.sub(r"[#*_>`|]", "", line).strip()
        if len(t) < 8:
            continue
        lines.append(t)
        if sum(len(x) for x in lines) > 240:
            break
    return ("".join(lines) or title)[:280]


async def _embed(embedder: OpenAIEmbeddings, text: str) -> list[float] | None:
    try:
        return await asyncio.wait_for(embedder.aembed_query(text[:2000]), timeout=20.0)
    except Exception as e:
        print(f"[UploadIngest] embed fail: {e}", flush=True)
        return None


async def _save_es(meta: dict, body: str, pg_id: int, url: str) -> None:
    from elasticsearch import AsyncElasticsearch
    from app.rag.es_client import ensure_indices

    try:
        await ensure_indices()
    except Exception:
        pass
    s = get_settings()
    deadline = meta.get("deadline")
    doc = {
        "title": meta.get("title", ""),
        "full_text": body[:15000],
        "summary": meta.get("summary", ""),
        "keywords": " ".join(meta.get("keywords") or []),
        "publisher": meta.get("publisher", ""),
        "region": meta.get("region", "全国"),
        "category_l1": meta.get("category_l1", ""),
        "category_l2": meta.get("category_l2", ""),
        "policy_types": meta.get("policy_types") or [],
        "deadline": deadline.isoformat() if isinstance(deadline, date) else None,
        "funding_amount": meta.get("funding_amount", ""),
        "source_url": url,
        "pg_id": pg_id,
    }
    try:
        async with AsyncElasticsearch(hosts=[s.es_host], verify_certs=False) as es:
            await es.index(index=s.es_policy_index, document=doc)
    except Exception as e:
        print(f"[UploadIngest] ES skip: {e}", flush=True)


async def build_relations_for_policy(policy_id: int) -> list[dict]:
    """为新入库政策构建图边：规则启发式 + LLM 抽边，返回关联详情。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        seed = await conn.fetchrow(
            """
            SELECT id, title, region, category_l1, publisher, summary,
                   LEFT(COALESCE(full_text,''), 2000) AS full_text,
                   key_conditions
            FROM policies WHERE id=$1
            """,
            policy_id,
        )
        if not seed:
            return []
        seed_d = dict(seed)
        if is_noise_policy(seed_d.get("title"), seed_d.get("full_text")):
            return []

        cands = await conn.fetch(
            """
            SELECT id, title, region, category_l1, publisher, summary,
                   LEFT(COALESCE(full_text,''), 1200) AS full_text
            FROM policies
            WHERE id <> $1
              AND (
                    category_l1 = $2
                 OR region = $3
                 OR region = '全国'
              )
            ORDER BY
              CASE WHEN category_l1 = $2 AND region = $3 THEN 0
                   WHEN category_l1 = $2 THEN 1
                   ELSE 2 END,
              id DESC
            LIMIT 25
            """,
            policy_id,
            seed_d.get("category_l1") or "其他",
            seed_d.get("region") or "全国",
        )
    candidates = [dict(r) for r in cands]

    # 规则：标题前缀 / 文种
    for c in candidates:
        pref = _title_prefix(seed_d.get("title", ""), 8)
        if pref and len(pref) >= 4 and pref in (c.get("title") or ""):
            await upsert_relation(
                policy_id,
                c["id"],
                "same_program",
                confidence=0.7,
                evidence=f"上传建边：标题前缀「{pref}…」",
                build_source="upload_rule",
            )
        st, ct = _title_doctype(seed_d.get("title", "")), _title_doctype(c.get("title", ""))
        if st == "implement" and ct == "notice":
            await upsert_relation(
                policy_id,
                c["id"],
                "implements",
                confidence=0.55,
                evidence="上传建边：实施细则→通知",
                build_source="upload_rule",
            )
        if st == "list" and ct in ("notice", "other"):
            await upsert_relation(
                policy_id,
                c["id"],
                "lists_under",
                confidence=0.5,
                evidence="上传建边：公示/名单→上级文",
                build_source="upload_rule",
            )

    # LLM 抽边（最多 8 候选）
    await llm_extract_edges(seed_d, candidates[:8], max_candidates=8)

    # 汇总该节点相关边
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.relation_type, r.confidence, r.evidence, r.build_source,
                   r.source_policy_id, r.target_policy_id,
                   s.title AS source_title, t.title AS target_title,
                   s.region AS source_region, t.region AS target_region
            FROM policy_relations r
            JOIN policies s ON s.id = r.source_policy_id
            JOIN policies t ON t.id = r.target_policy_id
            WHERE r.source_policy_id = $1 OR r.target_policy_id = $1
            ORDER BY r.confidence DESC, r.id DESC
            LIMIT 40
            """,
            policy_id,
        )
    relations = []
    for r in rows:
        neighbor_id = (
            r["target_policy_id"]
            if r["source_policy_id"] == policy_id
            else r["source_policy_id"]
        )
        neighbor_title = (
            r["target_title"]
            if r["source_policy_id"] == policy_id
            else r["source_title"]
        )
        neighbor_region = (
            r["target_region"]
            if r["source_policy_id"] == policy_id
            else r["source_region"]
        )
        relations.append({
            "relation_type": r["relation_type"],
            "confidence": float(r["confidence"] or 0),
            "evidence": r["evidence"] or "",
            "build_source": r["build_source"] or "",
            "neighbor_id": neighbor_id,
            "neighbor_title": neighbor_title,
            "neighbor_region": neighbor_region,
            "direction": "out" if r["source_policy_id"] == policy_id else "in",
        })
    return relations


async def ingest_uploaded_file(filename: str, raw: bytes) -> dict:
    """
    完整流水线：解析 → policies + chunks 向量 → ES → 图谱建边。

    返回管理端弹窗所需字段。
    """
    text = await extract_text_from_upload(filename, raw)
    if not text or len(text.strip()) < 40:
        return {"status": "skipped", "reason": "文档无有效文本或过短", "file": filename}

    fm, body = parse_frontmatter(text)
    if not body.strip():
        body = text

    title = _guess_title(filename, body, fm)
    region = fm.get("region") or "全国"
    category_l1 = fm.get("category_l1") or "其他"
    publisher = fm.get("publisher") or ""
    funding = fm.get("funding_amount") or ""
    deadline = _parse_date(fm.get("deadline"))
    pub_date = _parse_date(fm.get("pub_date"))
    summary = fm.get("summary") or _summary(body, title)
    keywords = ["管理员上传", Path(filename).stem[:40]]
    url = f"upload://admin/{datetime.now().strftime('%Y%m%d%H%M%S')}_{filename}"

    embedder = _get_embedder()
    embed_src = " ".join([title, summary, " ".join(keywords)])
    embedding = await _embed(embedder, embed_src)
    vec_clause = (
        "'[" + ",".join(map(str, embedding)) + "]'::vector" if embedding else "NULL"
    )

    pool = await get_pool()
    async with pool.acquire() as conn:
        # 同名文件覆盖：删旧 upload 同名
        old = await conn.fetch(
            "SELECT id FROM policies WHERE source_url LIKE $1 AND title = $2",
            "upload://admin/%",
            title,
        )
        old_ids = [r["id"] for r in old]
        if old_ids:
            await conn.execute(
                "DELETE FROM policy_relations WHERE source_policy_id = ANY($1::int[]) OR target_policy_id = ANY($1::int[])",
                old_ids,
            )
            await conn.execute("DELETE FROM policies WHERE id = ANY($1::int[])", old_ids)

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
            publisher or None,
            region,
            pub_date,
            deadline,
            body[:60000],
            summary[:500],
            keywords,
            category_l1,
            fm.get("category_l2") or fm.get("doc_type") or "",
            [fm.get("doc_type") or "上传文件"],
            (fm.get("key_conditions") or "")[:200],
            funding,
            url,
        )
        policy_id = int(row["id"])

    chunks = _chunk_text(body)
    saved_chunks = 0
    async with pool.acquire() as conn:
        for idx, chunk in enumerate(chunks):
            vec = await _embed(embedder, chunk)
            if not vec:
                continue
            vec_lit = "[" + ",".join(map(str, vec)) + "]"
            await conn.execute(
                f"INSERT INTO policy_chunks(policy_id,chunk_index,chunk_text,embedding)"
                f" VALUES($1,$2,$3,'{vec_lit}'::vector)",
                policy_id,
                idx,
                chunk,
            )
            saved_chunks += 1

    await _save_es(
        {
            "title": title,
            "summary": summary,
            "keywords": keywords,
            "publisher": publisher,
            "region": region,
            "category_l1": category_l1,
            "category_l2": fm.get("category_l2") or "",
            "policy_types": [fm.get("doc_type") or "上传文件"],
            "deadline": deadline,
            "funding_amount": funding,
        },
        body,
        policy_id,
        url,
    )

    relations = await build_relations_for_policy(policy_id)

    return {
        "status": "ok",
        "file": filename,
        "policy_id": policy_id,
        "title": title,
        "region": region,
        "category_l1": category_l1,
        "publisher": publisher,
        "funding_amount": funding,
        "deadline": deadline.isoformat() if deadline else None,
        "chunks": saved_chunks,
        "total_chars": len(body),
        "source_url": url,
        "relation_count": len(relations),
        "relations": relations,
        "pipeline": [
            "文本解析",
            "写入 policies + 文档向量",
            f"分块向量 ×{saved_chunks}",
            "Elasticsearch 索引",
            f"知识图谱建边 ×{len(relations)}",
        ],
    }
