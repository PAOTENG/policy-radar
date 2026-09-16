"""采集管理接口（管理员用）

端点：
  POST /api/v1/crawl/trigger      手动触发采集
  POST /api/v1/crawl/retry-failed 手动重试所有失败条目
  GET  /api/v1/crawl/failed       查看失败条目列表
  GET  /api/v1/crawl/logs         查看采集日志
"""
import asyncio
import sys
from fastapi import APIRouter, BackgroundTasks, Query
from pydantic import BaseModel
from app.crawler.sources import ALL_CRAWLERS, CRAWLER_MAP
from app.db.postgres import get_pool

router = APIRouter(prefix="/crawl", tags=["crawl"])


class CrawlRequest(BaseModel):
    source: str = "all"   # "all" 或任意 CRAWLER_MAP 中的 name


def _run_crawler_in_thread(crawler, retry_urls=None) -> dict:
    """
    在独立线程中运行单个爬虫，完全隔离于 FastAPI 主事件循环。

    关键点：
    - Windows 必须用 ProactorEventLoop 才能让 Playwright 创建子进程
    - 手动管理 loop 生命周期（而非 asyncio.run），
      退出前先 cancel 所有后台任务（openai/httpx 连接池清理），
      再 run_until_complete(gather) 让它们有机会响应 cancel，
      最后再 close()，避免 "Event loop is closed" RuntimeError
    - threading.local() pool 在 finally 中显式关闭，防止连接泄漏
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        from app.db.postgres import close_pool
        try:
            return await crawler.run(retry_urls=retry_urls)
        finally:
            await close_pool()

    try:
        return loop.run_until_complete(_run())
    finally:
        # 取消所有仍在运行的后台任务（httpx/openai 连接池清理等）
        # 再给它们一次机会响应 cancel，然后才 close loop
        try:
            pending = asyncio.all_tasks(loop)
            if pending:
                for task in pending:
                    task.cancel()
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
        finally:
            loop.close()


# ── 手动触发采集 ──────────────────────────────────────────────
@router.post("/trigger")
async def trigger_crawl(req: CrawlRequest, background_tasks: BackgroundTasks):
    """手动触发采集任务（后台运行，立即返回）"""
    if req.source == "all":
        crawlers = [C() for C in ALL_CRAWLERS]
    else:
        cls = CRAWLER_MAP.get(req.source)
        if not cls:
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail=f"未知来源: {req.source}")
        crawlers = [cls()]

    async def run():
        for c in crawlers:
            try:
                # 每个爬虫在独立线程+独立 ProactorEventLoop 中运行
                result = await asyncio.to_thread(_run_crawler_in_thread, c)
                print(f"[crawl/trigger] {c.name} 完成: {result}", flush=True)
            except Exception as e:
                print(f"[crawl/trigger] {c.name} 失败: {e}", flush=True)

    background_tasks.add_task(run)
    return {"message": f"采集任务已触发（后台运行）: {req.source}", "status": "running"}


# ── 手动重试失败条目 ──────────────────────────────────────────
@router.post("/retry-failed")
async def retry_failed(background_tasks: BackgroundTasks):
    """手动重试所有 resolved=FALSE 且 retry_count<3 的失败条目"""
    from app.crawler.scheduler import retry_failed_items
    background_tasks.add_task(retry_failed_items)
    return {"message": "重试任务已触发（后台运行）", "status": "running"}


# ── 查看失败条目 ──────────────────────────────────────────────
@router.get("/failed")
async def list_failed(
    resolved: bool = Query(False, description="True=显示已解决，False=显示待处理"),
    limit: int = Query(50, le=200),
):
    """查看失败条目列表"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, source_name, page_url, error_msg,
                      retry_count, last_tried, resolved, created_at
               FROM failed_crawl_items
               WHERE resolved = $1
               ORDER BY last_tried DESC
               LIMIT $2""",
            resolved, limit,
        )
    return [dict(r) for r in rows]


# ── 手动标记某条目已解决 ──────────────────────────────────────
@router.post("/failed/{item_id}/resolve")
async def resolve_failed(item_id: int):
    """手动标记某失败条目为已解决"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE failed_crawl_items SET resolved=TRUE WHERE id=$1", item_id
        )
    return {"message": f"条目 {item_id} 已标记为已解决"}


# ── 采集日志 ──────────────────────────────────────────────────
@router.get("/logs")
async def crawl_logs(limit: int = Query(20, le=100)):
    """查看最近采集日志"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, source_name, status, items_found, items_saved, ran_at
               FROM crawl_logs
               ORDER BY ran_at DESC
               LIMIT $1""",
            limit,
        )
    return [dict(r) for r in rows]
