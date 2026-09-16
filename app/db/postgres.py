"""PostgreSQL 连接池管理

使用 threading.local() 让每个线程（及其事件循环）持有独立的连接池。

背景：
  asyncpg.Pool 内部绑定了创建它时所在的 asyncio 事件循环。
  FastAPI 主线程 ↔ 爬虫线程 分别运行在不同的事件循环，若共享同一个 pool
  就会出现 ConnectionDoesNotExistError / "another operation is in progress"。
  threading.local() 确保每个线程各自创建、使用、销毁自己的 pool，互不干扰。
"""
import threading
import asyncpg
from app.config import get_settings

_thread_local = threading.local()


async def get_pool() -> asyncpg.Pool:
    """返回当前线程的连接池，不存在则自动创建"""
    pool = getattr(_thread_local, "pool", None)
    if pool is None:
        s = get_settings()
        _thread_local.pool = await asyncpg.create_pool(
            host=s.pg_host,
            port=s.pg_port,
            database=s.pg_dbname,
            user=s.pg_user,
            password=s.pg_password,
            min_size=1,
            max_size=8,
        )
    return _thread_local.pool


async def close_pool():
    """关闭并清除当前线程的连接池（爬虫线程结束时调用）"""
    pool = getattr(_thread_local, "pool", None)
    if pool:
        await pool.close()
        _thread_local.pool = None
