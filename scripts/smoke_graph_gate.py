"""快速冒烟：expand_related + conflict_gate（不跑全文 LLM）。"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main() -> None:
    from app.db.postgres import get_pool, close_pool
    from app.rag.policy_graph import expand_related
    from app.rag.conflict_gate import run_conflict_gate

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT source_policy_id AS id FROM policy_relations
            WHERE relation_type = ANY($1::text[])
            LIMIT 1
            """,
            ["implements", "same_program", "region_child"],
        )
        pid = row["id"]
        p = dict(
            await conn.fetchrow(
                """
                SELECT id, title, publisher, region, deadline, summary, category_l1,
                       funding_amount, key_conditions, source_url,
                       LEFT(COALESCE(full_text, ''), 800) AS matched_chunk,
                       LEFT(COALESCE(full_text, ''), 2000) AS full_text
                FROM policies WHERE id=$1
                """,
                pid,
            )
        )

    merged, meta = await expand_related([p], max_expand=5)
    print(
        f"seed={pid} expanded={meta['expand_count']} edges={len(meta['edges_used'])}",
        flush=True,
    )
    gate = await run_conflict_gate(
        merged, meta["edges_used"], locate_fulltext=False
    )
    print(
        f"gate={gate.gate_status} conflicts={len(gate.conflict_pairs)}",
        flush=True,
    )
    for c in gate.conflict_pairs[:5]:
        print(f"  - {c.conflict_type} ({c.severity})", flush=True)
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
