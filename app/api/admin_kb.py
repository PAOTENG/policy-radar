"""
政策知识库管理 API（无鉴权，供独立管理前端直连）

  POST /api/v1/admin/kb/upload     上传文件 → 解析/向量/建图
  GET  /api/v1/admin/kb/documents  已入库文档列表
  GET  /api/v1/admin/kb/documents/{id}/relations  某文档关联边
  DELETE /api/v1/admin/kb/documents/{id}  删除文档及边
"""
from __future__ import annotations

from fastapi import APIRouter, File, UploadFile, HTTPException

from app.db.postgres import get_pool
from app.rag.upload_ingest import ingest_uploaded_file

router = APIRouter(prefix="/admin/kb", tags=["admin-kb"])

MAX_BYTES = 15 * 1024 * 1024  # 15MB


@router.post("/upload")
async def upload_policy_file(file: UploadFile = File(...)):
    """上传政策文件，后台完成解析、向量化、知识图谱建边。"""
    if not file.filename:
        raise HTTPException(400, "缺少文件名")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "空文件")
    if len(raw) > MAX_BYTES:
        raise HTTPException(400, f"文件过大（>{MAX_BYTES // (1024*1024)}MB）")

    try:
        result = await ingest_uploaded_file(file.filename, raw)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        print(f"[admin_kb] upload error: {e}", flush=True)
        raise HTTPException(500, f"处理失败: {e}") from e
    return result


@router.get("/documents")
async def list_documents(limit: int = 50, uploaded_only: bool = False):
    """文档列表（默认含全部政策；uploaded_only=true 仅管理员上传）。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if uploaded_only:
            rows = await conn.fetch(
                """
                SELECT p.id, p.title, p.region, p.category_l1, p.publisher,
                       p.funding_amount, p.deadline, p.source_url, p.created_at,
                       (SELECT COUNT(*) FROM policy_chunks c WHERE c.policy_id = p.id) AS chunks,
                       (SELECT COUNT(*) FROM policy_relations r
                        WHERE r.source_policy_id = p.id OR r.target_policy_id = p.id) AS relations
                FROM policies p
                WHERE p.source_url LIKE 'upload://admin/%'
                ORDER BY p.id DESC
                LIMIT $1
                """,
                limit,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT p.id, p.title, p.region, p.category_l1, p.publisher,
                       p.funding_amount, p.deadline, p.source_url, p.created_at,
                       (SELECT COUNT(*) FROM policy_chunks c WHERE c.policy_id = p.id) AS chunks,
                       (SELECT COUNT(*) FROM policy_relations r
                        WHERE r.source_policy_id = p.id OR r.target_policy_id = p.id) AS relations
                FROM policies p
                ORDER BY p.id DESC
                LIMIT $1
                """,
                limit,
            )
    docs = []
    for r in rows:
        docs.append({
            "id": r["id"],
            "title": r["title"],
            "region": r["region"],
            "category_l1": r["category_l1"],
            "publisher": r["publisher"],
            "funding_amount": r["funding_amount"],
            "deadline": r["deadline"].isoformat() if r["deadline"] else None,
            "source_url": r["source_url"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "chunks": int(r["chunks"] or 0),
            "relations": int(r["relations"] or 0),
            "is_upload": str(r["source_url"] or "").startswith("upload://admin/"),
        })
    return {"documents": docs, "count": len(docs)}


@router.get("/documents/{policy_id}/relations")
async def get_document_relations(policy_id: int):
    """查询单篇政策的知识图谱关联边。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        pol = await conn.fetchrow(
            "SELECT id, title, region, category_l1 FROM policies WHERE id=$1",
            policy_id,
        )
        if not pol:
            raise HTTPException(404, "政策不存在")
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
            ORDER BY r.confidence DESC
            """,
            policy_id,
        )
    relations = []
    for r in rows:
        out = r["source_policy_id"] == policy_id
        relations.append({
            "relation_type": r["relation_type"],
            "confidence": float(r["confidence"] or 0),
            "evidence": r["evidence"] or "",
            "build_source": r["build_source"] or "",
            "neighbor_id": r["target_policy_id"] if out else r["source_policy_id"],
            "neighbor_title": r["target_title"] if out else r["source_title"],
            "neighbor_region": r["target_region"] if out else r["source_region"],
            "direction": "out" if out else "in",
        })
    return {
        "policy_id": pol["id"],
        "title": pol["title"],
        "region": pol["region"],
        "category_l1": pol["category_l1"],
        "relation_count": len(relations),
        "relations": relations,
    }


@router.delete("/documents/{policy_id}")
async def delete_document(policy_id: int):
    """删除政策及其 chunks / 关系边。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT id FROM policies WHERE id=$1", policy_id)
        if not exists:
            raise HTTPException(404, "政策不存在")
        await conn.execute(
            "DELETE FROM policy_relations WHERE source_policy_id=$1 OR target_policy_id=$1",
            policy_id,
        )
        await conn.execute("DELETE FROM policies WHERE id=$1", policy_id)
    return {"status": "ok", "deleted_id": policy_id}
