"""报告生成接口（SSE 流式输出）"""
import json
import uuid
import asyncio
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from app.db.schemas import ReportRequest
from app.agents.report.graph import stream_report
from app.db.postgres import get_pool

router = APIRouter(prefix="/report", tags=["report"])


async def _event_stream(keywords: dict, user_input: str, module: str, session_id: str):
    """SSE 事件生成器。
    
    数据链路：
      流式输出 tokens → 前端渲染
      流结束后 → UPDATE query_logs 写入完整报告文本 + 验证元数据
    """
    out_meta: dict = {}
    full_report: list[str] = []
    try:
        yield f"data: {json.dumps({'type': 'session', 'session_id': session_id}, ensure_ascii=False)}\n\n"

        async for token in stream_report(keywords, user_input, module, out_meta=out_meta):
            full_report.append(token)
            data = json.dumps({"type": "delta", "content": token}, ensure_ascii=False)
            yield f"data: {data}\n\n"

        yield 'data: {"type":"done"}\n\n'

        # 流结束后异步写入报告全文 + 验证元数据（不阻塞前端）
        report_text = "".join(full_report)
        asyncio.create_task(_save_report(session_id, report_text, out_meta))

    except Exception as e:
        err = json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False)
        yield f"data: {err}\n\n"


@router.post("/generate")
async def generate_report(req: ReportRequest):
    """流式生成政策分析报告（SSE）"""
    session_id = req.session_id or str(uuid.uuid4())
    asyncio.create_task(_log_query(req, session_id))
    return StreamingResponse(
        _event_stream(req.keywords, req.user_input, req.module, session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Session-Id": session_id,
        },
    )


async def _log_query(req: ReportRequest, session_id: str):
    """流开始时记录查询意图（keywords + user_input），session_id 供后续 JOIN"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO query_logs(session_id, keywords, user_input) VALUES($1, $2::jsonb, $3)",
                session_id,
                json.dumps(req.keywords, ensure_ascii=False),
                req.user_input,
            )
    except Exception:
        pass


async def _save_report(session_id: str, report_text: str, verification_meta: dict):
    """流结束后写入报告全文 + 验证元数据到 query_logs。
    
    verification_meta 字段（来自 policy_verifier）：
      - policy_count:         本次检索到的候选政策数量
      - expired_count:        已截止的政策数量
      - contradiction_count:  检测到的矛盾数量
      - contradictions:       矛盾列表详情 [{dimension, policy_a, policy_b, ...}]
    """
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE query_logs
                SET report_text       = $1,
                    verification_meta = $2::jsonb
                WHERE session_id = $3
                """,
                report_text,
                json.dumps(verification_meta, ensure_ascii=False),
                session_id,
            )
        print(
            f"[report] 报告已入库 session={session_id[:8]}... "
            f"字符={len(report_text)} "
            f"矛盾={verification_meta.get('contradiction_count', 0)} "
            f"过期={verification_meta.get('expired_count', 0)}",
            flush=True,
        )
    except Exception as e:
        print(f"[report] 报告入库失败: {e}", flush=True)
