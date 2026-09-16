"""筛更高质量的强关联边：标题有公共子串 / 同发布单位批次。"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def common_ratio(a: str, b: str) -> float:
    a = a.replace("《", "").replace("》", "")[:40]
    b = b.replace("《", "").replace("》", "")[:40]
    if not a or not b:
        return 0.0
    # longest common prefix-ish: shared chars in first 20
    sa, sb = set(a[:20]), set(b[:20])
    return len(sa & sb) / max(len(sa | sb), 1)


async def main() -> None:
    from app.db.postgres import get_pool, close_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.relation_type, r.confidence, r.evidence,
                   s.id AS sid, s.title AS stitle, s.region AS sregion,
                   s.category_l1 AS scat, s.publisher AS spub, s.source_url AS surl,
                   t.id AS tid, t.title AS ttitle, t.region AS tregion,
                   t.category_l1 AS tcat, t.publisher AS tpub, t.source_url AS turl
            FROM policy_relations r
            JOIN policies s ON s.id = r.source_policy_id
            JOIN policies t ON t.id = r.target_policy_id
            WHERE r.relation_type = ANY($1::text[])
              AND r.confidence >= 0.5
              AND s.category_l1 IS DISTINCT FROM '其他'
              AND t.category_l1 IS DISTINCT FROM '其他'
            """,
            ["implements", "same_program", "lists_under"],
        )

        scored = []
        for r in rows:
            st, tt = r["stitle"] or "", r["ttitle"] or ""
            if any(x in st + tt for x in ("抱歉", "年度报表", "新闻发布会", "404")):
                continue
            # 标题前缀相同更可靠
            prefix_a = "".join(ch for ch in st if ch not in "《》 \t")[:12]
            prefix_b = "".join(ch for ch in tt if ch not in "《》 \t")[:12]
            prefix_hit = prefix_a[:8] and (prefix_a[:8] in tt or prefix_b[:8] in st)
            same_pub = (r["spub"] or "") and (r["spub"] == r["tpub"])
            score = common_ratio(st, tt)
            if prefix_hit:
                score += 0.4
            if same_pub:
                score += 0.1
            if r["relation_type"] == "same_program":
                score += 0.05
            # 关键词业务簇加分
            for kw in ("高新", "专精特新", "单项冠军", "科技专项", "认定", "申报", "公示", "名单"):
                if kw in st and kw in tt:
                    score += 0.15
            if score < 0.55:
                continue
            scored.append((score, r))

        scored.sort(key=lambda x: x[0], reverse=True)

        print("=" * 60)
        print("高质量强关联边（标题相似 / 同项目关键词）Top 8")
        print("=" * 60)
        used_pairs = set()
        shown = 0
        for score, r in scored:
            pair = tuple(sorted([r["sid"], r["tid"]]))
            if pair in used_pairs:
                continue
            used_pairs.add(pair)
            shown += 1
            print(f"\n【推荐验证 {shown}】score={score:.2f}  relation={r['relation_type']}  conf={r['confidence']}")
            print(f"  evidence: {r['evidence']}")
            print(f"  A.id={r['sid']} [{r['sregion']}/{r['scat']}]")
            print(f"     《{r['stitle'][:90]}》")
            if r["surl"]:
                print(f"     {r['surl']}")
            print(f"  B.id={r['tid']} [{r['tregion']}/{r['tcat']}]")
            print(f"     《{r['ttitle'][:90]}》")
            if r["turl"]:
                print(f"     {r['turl']}")
            if shown >= 8:
                break

        # 专门找高企/专精特新簇
        print("\n" + "=" * 60)
        print("专题簇：标题同时含 高新/专精特新/单项冠军 的 same_program")
        print("=" * 60)
        special = await conn.fetch(
            """
            SELECT r.relation_type, r.confidence,
                   s.id AS sid, s.title AS stitle, s.region AS sregion, s.source_url AS surl,
                   t.id AS tid, t.title AS ttitle, t.region AS tregion, t.source_url AS turl
            FROM policy_relations r
            JOIN policies s ON s.id = r.source_policy_id
            JOIN policies t ON t.id = r.target_policy_id
            WHERE r.relation_type = 'same_program'
              AND (
                (s.title LIKE '%高新%' AND t.title LIKE '%高新%')
                OR (s.title LIKE '%专精特新%' AND t.title LIKE '%专精特新%')
                OR (s.title LIKE '%单项冠军%' AND t.title LIKE '%单项冠军%')
              )
            ORDER BY r.confidence DESC
            LIMIT 6
            """
        )
        if not special:
            print("（暂无同时命中关键词的边，下面给同 region 资质认定同簇）")
            special = await conn.fetch(
                """
                SELECT r.relation_type, r.confidence,
                       s.id AS sid, s.title AS stitle, s.region AS sregion, s.source_url AS surl,
                       t.id AS tid, t.title AS ttitle, t.region AS tregion, t.source_url AS turl
                FROM policy_relations r
                JOIN policies s ON s.id = r.source_policy_id
                JOIN policies t ON t.id = r.target_policy_id
                WHERE r.relation_type = 'same_program'
                  AND s.category_l1 = '资质认定'
                  AND s.region = t.region
                  AND s.title NOT LIKE '%新闻发布会%'
                ORDER BY r.id
                LIMIT 6
                """
            )
        for i, r in enumerate(special, 1):
            print(f"\n【专题 {i}】{r['relation_type']} conf={r['confidence']}")
            print(f"  A.id={r['sid']} [{r['sregion']}] 《{r['stitle'][:80]}》")
            if r["surl"]:
                print(f"     {r['surl']}")
            print(f"  B.id={r['tid']} [{r['tregion']}] 《{r['ttitle'][:80]}》")
            if r["turl"]:
                print(f"     {r['turl']}")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
