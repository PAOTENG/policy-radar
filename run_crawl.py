"""
独立爬虫运行脚本 - 不需要启动 FastAPI 服务器

用法（在项目根目录、已激活 Python 环境后运行）：
  # 采集全部站点
  python run_crawl.py

  # 只采集指定来源（空格分隔多个）
  python run_crawl.py miit mof gdstc

  # 查看所有来源名称
  python run_crawl.py --list
"""
import asyncio
import sys
import time

# Windows 必须用 ProactorEventLoop 才能运行 Playwright 子进程
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

sys.path.insert(0, ".")


def run_one(crawler_cls, retry_urls=None):
    """在独立事件循环中运行单个爬虫"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        from app.db.postgres import close_pool
        try:
            return await crawler_cls().run(retry_urls=retry_urls)
        finally:
            await close_pool()

    try:
        return loop.run_until_complete(_run())
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            if pending:
                for t in pending:
                    t.cancel()
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        finally:
            loop.close()


def main():
    from app.crawler.sources import ALL_CRAWLERS, CRAWLER_MAP

    args = sys.argv[1:]

    # --list 模式
    if "--list" in args:
        print(f"共 {len(ALL_CRAWLERS)} 个爬虫:")
        for i, C in enumerate(ALL_CRAWLERS, 1):
            print(f"  {i:3d}. {C.name}")
        return

    # 选择要运行的爬虫
    if args:
        selected = []
        for name in args:
            cls = CRAWLER_MAP.get(name)
            if cls:
                selected.append(cls)
            else:
                print(f"[WARN] 未知来源: {name}，跳过")
        if not selected:
            print("没有有效的来源，退出")
            return
    else:
        selected = list(ALL_CRAWLERS)

    print(f"=== 开始采集 {len(selected)} 个站点 ===")
    total_saved = 0
    total_failed = 0
    t0 = time.time()

    for i, cls in enumerate(selected, 1):
        print(f"\n[{i}/{len(selected)}] 运行 {cls.name} ...", flush=True)
        try:
            result = run_one(cls)
            saved = result.get("saved", 0)
            failed = result.get("failed", 0)
            total_saved += saved
            total_failed += failed
            print(f"  -> saved={saved} failed={failed}", flush=True)
        except Exception as e:
            print(f"  -> 异常: {e}", flush=True)
            total_failed += 1

    elapsed = time.time() - t0
    print(f"\n=== 采集完成 ===")
    print(f"耗时: {elapsed/60:.1f} 分钟")
    print(f"总入库: {total_saved}")
    print(f"总失败: {total_failed}")


if __name__ == "__main__":
    main()
