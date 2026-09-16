"""对话 Agent 接口（SSE 流式输出）"""
import json
import asyncio
import uuid
from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from app.agents.chat.graph import chat_stream
from app.db.postgres import get_pool

router = APIRouter(prefix="/ai", tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(..., description="用户消息")
    thread_id: str = Field(default="", description="会话 ID，为空时自动生成")
    user_id: str = Field(default="anonymous", description="用户标识，用于跨会话记忆")


async def _event_stream(message: str, thread_id: str, user_id: str):
    """SSE 事件生成器"""
    # 先写入用户消息到 chat_messages 表
    asyncio.create_task(_save_message(thread_id, user_id, "user", message))

    full_response = []
    try:
        async for event_type, content in chat_stream(message, thread_id, user_id):
            if event_type == "delta":
                full_response.append(content)
                data = json.dumps({"type": "delta", "content": content}, ensure_ascii=False)
                yield f"data: {data}\n\n"
            elif event_type == "done":
                yield 'data: {"type":"done"}\n\n'
            elif event_type == "error":
                err = json.dumps({"type": "error", "message": content}, ensure_ascii=False)
                yield f"data: {err}\n\n"
    except Exception as e:
        err = json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False)
        yield f"data: {err}\n\n"
    finally:
        # 保存 Assistant 回复
        if full_response:
            asyncio.create_task(
                _save_message(thread_id, user_id, "assistant", "".join(full_response))
            )


@router.post("/chat")
async def chat(req: ChatRequest):
    """流式对话接口（SSE）"""
    thread_id = req.thread_id or str(uuid.uuid4())
    return StreamingResponse(
        _event_stream(req.message, thread_id, req.user_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-Thread-Id": thread_id,
        },
    )


@router.get("/chat/history/{thread_id}")
async def get_history(thread_id: str, limit: int = Query(50, ge=1, le=200)):
    """获取对话历史（UI 展示用）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content, created_at
            FROM chat_messages
            WHERE thread_id = $1
            ORDER BY created_at ASC
            LIMIT $2
            """,
            thread_id,
            limit,
        )
    return {"thread_id": thread_id, "messages": [dict(r) for r in rows]}


@router.get("/chat/conversations")
async def get_conversations(user_id: str = Query("anonymous"), limit: int = Query(20)):
    """获取用户的会话列表"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (thread_id)
                thread_id,
                content as last_message,
                created_at
            FROM chat_messages
            WHERE user_id = $1 AND role = 'user'
            ORDER BY thread_id, created_at DESC
            LIMIT $2
            """,
            user_id,
            limit,
        )
    return {"conversations": [dict(r) for r in rows]}


async def _save_message(thread_id: str, user_id: str, role: str, content: str):
    """后台任务：持久化消息到 chat_messages 表"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO chat_messages(thread_id, user_id, role, content) VALUES($1,$2,$3,$4)",
                thread_id, user_id, role, content,
            )
    except Exception as e:
        print(f"[ChatAPI] 消息保存失败: {e}", flush=True)
