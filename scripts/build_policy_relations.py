"""
存量政策关系边批建脚本（scripts/build_policy_relations.py）

用法（请使用 agent 虚拟环境）：
  D:\\Environment\\Anaconda\\envs\\agent\\python.exe scripts/build_policy_relations.py
  D:\\Environment\\Anaconda\\envs\\agent\\python.exe scripts/build_policy_relations.py --llm
  D:\\Environment\\Anaconda\\envs\\agent\\python.exe scripts/build_policy_relations.py --limit 100

流程：
  1. init_db() 确保 policy_relations 表存在
  2. relation_builder.build_all_relations（规则边；可选 LLM）
  3. 打印边类型分布统计

说明：
  - 默认仅规则边（快、零 LLM 成本），适合首次灌库
  - --llm 会对部分种子做 LightRAG 风格抽边（较慢、耗 token）
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 保证以脚本方式运行时可 import app.*
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


async def main(use_llm: bool, limit: int | None, llm_max_seeds: int) -> None:
    """异步主流程：建表 → 建边 → 统计。"""
    from app.db.init import init_db
    from app.db.postgres import get_pool, close_pool
    from app.rag.relation_builder import build_all_relations

    print("[build_policy_relations] init_db ...", flush=True)
    await init_db()

    print(
        f"[build_policy_relations] building edges "
        f"(use_llm={use_llm}, limit={limit}) ...",
        flush=True,
    )
    stats = await build_all_relations(
        use_llm=use_llm,
        llm_max_seeds=llm_max_seeds,
        limit=limit,
    )
    print(f"[build_policy_relations] stats={stats}", flush=True)

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT relation_type, COUNT(*) AS n, ROUND(AVG(confidence)::numeric, 3) AS avg_conf
            FROM policy_relations
            GROUP BY relation_type
            ORDER BY n DESC
            """
        )
    print("[build_policy_relations] by relation_type:", flush=True)
    for r in rows:
        print(
            f"  - {r['relation_type']}: {r['n']} (avg_conf={r['avg_conf']})",
            flush=True,
        )

    await close_pool()
    print("[build_policy_relations] done.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="批建 policy_relations 文档级关系边")
    parser.add_argument(
        "--llm",
        action="store_true",
        help="启用 Fast LLM 抽边（默认关闭，仅规则边）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="仅加载前 N 条政策（调试用）",
    )
    parser.add_argument(
        "--llm-max-seeds",
        type=int,
        default=40,
        help="LLM 最多处理的种子政策数",
    )
    args = parser.parse_args()
    asyncio.run(main(args.llm, args.limit, args.llm_max_seeds))
