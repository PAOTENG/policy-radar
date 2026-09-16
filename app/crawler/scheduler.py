"""定时采集调度器（APScheduler）

任务时间表：
  - 每天 12:00 正午  → 运行所有爬虫（正常采集）
  - 每天 14:00 下午  → 重试昨日失败条目（补漏）
"""
import asyncio
import sys
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from app.crawler.sources import ALL_CRAWLERS
from app.db.postgres import get_pool


def _run_in_own_loop(crawler, retry_urls=None) -> dict:
    """在独立线程中运行爬虫，与 crawl.py 保持相同策略"""
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


async def run_all_crawlers():
    """正常采集：每个爬虫在独立线程中运行（解决 Windows 子进程限制）"""
    print("[Scheduler] ===== 开始每日政策采集 =====", flush=True)
    for CrawlerClass in ALL_CRAWLERS:
        crawler = CrawlerClass()
        try:
            result = await asyncio.to_thread(_run_in_own_loop, crawler)
            await _log_crawl(result)
        except Exception as e:
            print(f"[Scheduler] 爬虫 {CrawlerClass.name} 异常: {e}", flush=True)
    print("[Scheduler] ===== 每日采集完成 =====", flush=True)


async def retry_failed_items():
    """
    重试失败条目：
    查出所有 resolved=FALSE 且 retry_count < 3 的记录，
    按来源分组后交给对应爬虫重试。
    """
    print("[Scheduler] ===== 开始重试失败条目 =====", flush=True)
    pool = await get_pool()

    # 按爬虫名分组
    crawler_map = {C.name: C for C in ALL_CRAWLERS}
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT source_name, page_url FROM failed_crawl_items
               WHERE resolved = FALSE AND retry_count < 3
               ORDER BY source_name, last_tried ASC"""
        )

    if not rows:
        print("[Scheduler] 无待重试条目", flush=True)
        return

    # 按来源聚合 URL
    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(r["source_name"], []).append(r["page_url"])

    for source_name, urls in grouped.items():
        print(f"[Scheduler] 重试 {source_name}: {len(urls)} 条", flush=True)
        CrawlerClass = crawler_map.get(source_name)
        if not CrawlerClass:
            print(f"[Scheduler] 未找到爬虫 {source_name}，跳过", flush=True)
            continue
        crawler = CrawlerClass()
        try:
            result = await asyncio.to_thread(_run_in_own_loop, crawler, urls)
            await _log_crawl({**result, "source": f"{source_name}(retry)"})
        except Exception as e:
            print(f"[Scheduler] 重试 {source_name} 异常: {e}", flush=True)

    print("[Scheduler] ===== 重试完成 =====", flush=True)


async def _log_crawl(result: dict):
    """写入 crawl_logs"""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO crawl_logs(source_name, status, items_found, items_saved) "
                "VALUES($1, $2, $3, $4)",
                result["source"],
                "success" if result.get("failed", 0) == 0 else "partial",
                result.get("saved", 0) + result.get("failed", 0) + result.get("skipped", 0),
                result.get("saved", 0),
            )
    except Exception:
        pass


def create_scheduler() -> AsyncIOScheduler:
    """
    创建定时调度器：
      - 每天 12:00 正式采集
      - 每天 14:00 重试失败条目
    """
    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    scheduler.add_job(
        run_all_crawlers,
        CronTrigger(hour=12, minute=0),
        id="daily_crawl",
        name="每日政策采集（12:00）",
        replace_existing=True,
    )

    scheduler.add_job(
        retry_failed_items,
        CronTrigger(hour=14, minute=0),
        id="retry_failed",
        name="重试失败条目（14:00）",
        replace_existing=True,
    )

    return scheduler
