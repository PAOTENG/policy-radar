"""列出知识库中可用于人工验证的强关联边样本。"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

STRONG = ["implements", "same_program", "lists_under", "funding_of", "supersedes"]


async def main() -> None:
    from app.db.postgres import get_pool, close_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.relation_type, r.confidence, r.evidence, r.build_source,
                   s.id AS sid, s.title AS stitle, s.region AS sregion,
                   s.category_l1 AS scat, s.source_url AS surl,
                   t.id AS tid, t.title AS ttitle, t.region AS tregion,
                   t.category_l1 AS tcat, t.source_url AS turl
            FROM policy_relations r
            JOIN policies s ON s.id = r.source_policy_id
            JOIN policies t ON t.id = r.target_policy_id
            WHERE r.relation_type = ANY($1::text[])
              AND r.confidence >= 0.45
            ORDER BY
              CASE r.relation_type
                WHEN 'implements' THEN 1
                WHEN 'same_program' THEN 2
                WHEN 'lists_under' THEN 3
                WHEN 'supersedes' THEN 4
                ELSE 5
              END,
              r.confidence DESC,
              r.id
            LIMIT 80
            """,
            STRONG,
        )

        picked = []
        seen_types: dict[str, int] = {}
        for r in rows:
            rt = r["relation_type"]
            if seen_types.get(rt, 0) >= 3:
                continue
            blob = (r["stitle"] or "") + (r["ttitle"] or "")
            if any(x in blob for x in ("抱歉", "年度报表", "404")):
                continue
            picked.append(r)
            seen_types[rt] = seen_types.get(rt, 0) + 1
            if len(picked) >= 9:
                break

        print("=" * 60)
        print("强关联边验证样本（implements / same_program / lists_under ...）")
        print(f"扫描={len(rows)} 精选={len(picked)}")
        print("=" * 60)
        for i, r in enumerate(picked, 1):
            print(f"\n【样本 {i}】relation={r['relation_type']}  conf={r['confidence']}  build={r['build_source']}")
            print(f"  evidence: {r['evidence']}")
            print(f"  A.id={r['sid']}  [{r['sregion']}/{r['scat']}]")
            print(f"     《{r['stitle']}》")
            if r["surl"]:
                print(f"     url: {r['surl']}")
            print(f"  B.id={r['tid']}  [{r['tregion']}/{r['tcat']}]")
            print(f"     《{r['ttitle']}》")
            if r["turl"]:
                print(f"     url: {r['turl']}")

        # 补召回演示：优先 implements，否则 same_program
        demo = next((r for r in picked if r["relation_type"] == "implements"), None)
        if demo is None:
            demo = next((r for r in picked if r["relation_type"] == "same_program"), None)
        if demo:
            sid = demo["sid"]
            neigh = await conn.fetch(
                """
                SELECT r.relation_type, r.confidence,
                       CASE WHEN r.source_policy_id=$1 THEN r.target_policy_id
                            ELSE r.source_policy_id END AS nid,
                       p.title, p.region, p.category_l1
                FROM policy_relations r
                JOIN policies p ON p.id = CASE
                    WHEN r.source_policy_id=$1 THEN r.target_policy_id
                    ELSE r.source_policy_id END
                WHERE (r.source_policy_id=$1 OR r.target_policy_id=$1)
                  AND r.relation_type = ANY($2::text[])
                ORDER BY r.confidence DESC
                LIMIT 8
                """,
                sid,
                STRONG,
            )
            print("\n" + "=" * 60)
            print("补召回验证建议：报告检索若只命中种子 A，expand 后应出现下列邻居")
            print("=" * 60)
            print(f"种子 A: id={sid}")
            print(f"  《{demo['stitle']}》")
            print(f"  建议 keywords: region={demo['sregion']} l1={demo['scat']}")
            for n in neigh:
                print(
                    f"  -> [{n['relation_type']}] id={n['nid']} "
                    f"[{n['region']}/{n['category_l1']}] 《{n['title'][:60]}》"
                )

        # 统计
        stats = await conn.fetch(
            """
            SELECT relation_type, COUNT(*) AS n
            FROM policy_relations
            WHERE relation_type = ANY($1::text[])
            GROUP BY relation_type
            ORDER BY n DESC
            """,
            STRONG,
        )
        print("\n强边类型计数：")
        for s in stats:
            print(f"  {s['relation_type']}: {s['n']}")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
