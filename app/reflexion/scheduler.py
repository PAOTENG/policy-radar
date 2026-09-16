"""Reflexion 定时调度：每周日凌晨 3:00 触发一次 DSPy 优化
通过 APScheduler 集成到主应用生命周期中。
"""
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler


def create_reflexion_job(scheduler: AsyncIOScheduler):
    """注册 Reflexion 定时任务到现有 scheduler"""

    async def _run():
        print("[Reflexion] 定时任务触发", flush=True)
        try:
            from app.reflexion.optimizer import run_optimization
            result = await run_optimization(dry_run=False)
            print(f"[Reflexion] 优化完成: {result}", flush=True)
        except Exception as e:
            print(f"[Reflexion] 定时任务异常: {e}", flush=True)

    scheduler.add_job(
        _run,
        trigger="cron",
        day_of_week="sun",
        hour=3,
        minute=0,
        id="reflexion_optimize",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    print("[Reflexion] 定时优化任务已注册（每周日 03:00）", flush=True)


async def trigger_now(dry_run: bool = True) -> dict:
    """手动触发一次优化（API 接口用）"""
    from app.reflexion.optimizer import run_optimization
    return await run_optimization(dry_run=dry_run)
