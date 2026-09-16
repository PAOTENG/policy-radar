"""用户反馈收集接口
支持显式评分（按钮）和隐式行为（点击、导出）两种信号。
数据存储在 feedback 表，供 Reflexion 离线优化器使用。
"""
import json
from fastapi import APIRouter
from pydantic import BaseModel, Field
from typing import Optional
from app.db.postgres import get_pool

router = APIRouter(prefix="/ai", tags=["feedback"])


class FeedbackRequest(BaseModel):
    session_id: str = Field(..., description="会话/请求唯一标识")
    thread_id: Optional[str] = Field(None, description="关联的对话 thread_id")
    signal_type: str = Field(
        ...,
        description="反馈类型: explicit_rating | policy_click | pdf_export | session_length",
    )
    score: Optional[float] = Field(None, ge=0.0, le=1.0, description="归一化分数 0~1")
    metadata: dict = Field(default_factory=dict, description="附加信息（政策ID、导出数量等）")


@router.post("/feedback")
async def submit_feedback(req: FeedbackRequest):
    """提交用户反馈"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO feedback(session_id, thread_id, signal_type, score, metadata)
            VALUES ($1, $2, $3, $4, $5::jsonb)
            """,
            req.session_id,
            req.thread_id,
            req.signal_type,
            req.score,
            json.dumps(req.metadata, ensure_ascii=False),
        )
    return {"status": "ok", "message": "反馈已记录"}


@router.get("/feedback/stats")
async def get_feedback_stats(days: int = 7):
    """获取反馈统计（用于监控 Reflexion 效果）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                signal_type,
                COUNT(*) as count,
                AVG(score) as avg_score,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY score) as median_score
            FROM feedback
            WHERE created_at > NOW() - INTERVAL '1 day' * $1
            GROUP BY signal_type
            ORDER BY count DESC
            """,
            days,
        )
    return {"days": days, "stats": [dict(r) for r in rows]}


@router.post("/feedback/report-rating")
async def rate_report(
    session_id: str,
    thread_id: Optional[str] = None,
    helpful: bool = True,
):
    """快捷接口：报告有用/没用按钮"""
    score = 1.0 if helpful else 0.0
    req = FeedbackRequest(
        session_id=session_id,
        thread_id=thread_id,
        signal_type="explicit_rating",
        score=score,
        metadata={"helpful": helpful},
    )
    return await submit_feedback(req)
