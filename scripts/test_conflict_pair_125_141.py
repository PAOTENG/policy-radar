"""
对强关联对 (125, 141) 走一遍冲突门控，并打印槽位/冲突详情，便于人工对照。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main() -> None:
    from app.db.postgres import get_pool, close_pool
    from app.rag.conflict_gate import (
        extract_slots,
        check_pair_conflicts,
        run_conflict_gate,
        locate_conflict_fulltext,
    )

    ids = [125, 141]
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, publisher, region, pub_date, deadline, summary,
                   category_l1, funding_amount, key_conditions, source_url,
                   LEFT(COALESCE(full_text, ''), 4000) AS full_text,
                   LEFT(COALESCE(full_text, summary, ''), 800) AS matched_chunk
            FROM policies
            WHERE id = ANY($1::int[])
            ORDER BY id
            """,
            ids,
        )
        edge = await conn.fetchrow(
            """
            SELECT relation_type, confidence, evidence
            FROM policy_relations
            WHERE (source_policy_id=125 AND target_policy_id=141)
               OR (source_policy_id=141 AND target_policy_id=125)
            LIMIT 1
            """
        )

    policies = [dict(r) for r in rows]
    by_id = {p["id"]: p for p in policies}
    a, b = by_id[125], by_id[141]
    rel = (edge["relation_type"] if edge else "same_program")

    print("=" * 70)
    print("1) 边信息")
    print("=" * 70)
    print(f"relation={rel} conf={edge['confidence'] if edge else 'N/A'} evidence={edge['evidence'] if edge else ''}")

    print("\n" + "=" * 70)
    print("2) 槽位抽取 extract_slots")
    print("=" * 70)
    for p in (a, b):
        slots = extract_slots(p)
        # 可序列化
        dump = {
            k: (str(v) if hasattr(v, "isoformat") else v)
            for k, v in slots.items()
        }
        print(f"\n--- id={p['id']} 《{p['title'][:50]}》---")
        print(json.dumps(dump, ensure_ascii=False, indent=2))

    print("\n" + "=" * 70)
    print("3) 规则五类冲突 check_pair_conflicts（0 LLM）")
    print("=" * 70)
    conflicts = check_pair_conflicts(a, b, rel)
    print(f"检出 {len(conflicts)} 条")
    for i, c in enumerate(conflicts, 1):
        print(f"\n[{i}] type={c.conflict_type} severity={c.severity}")
        print(f"    A={c.value_a}")
        print(f"    B={c.value_b}")
        print(f"    desc={c.description}")
        print(f"    advice={c.advice}")

    print("\n" + "=" * 70)
    print("4) 全文定位 locate_conflict_fulltext（若有冲突则对每条调 LLM）")
    print("=" * 70)
    located = []
    for c in conflicts[:5]:
        c2 = await locate_conflict_fulltext(a, b, c)
        located.append(c2)
        print(f"\n[{c2.conflict_type}]")
        print(f"  evidence_a: {c2.evidence_a}")
        print(f"  evidence_b: {c2.evidence_b}")
        print(f"  advice: {c2.advice}")

    print("\n" + "=" * 70)
    print("5) 门控主入口 run_conflict_gate（含 annotate）")
    print("=" * 70)
    edges = [{
        "source_policy_id": 125,
        "target_policy_id": 141,
        "relation_type": rel,
        "confidence": float(edge["confidence"]) if edge else 0.65,
        "evidence": edge["evidence"] if edge else "",
    }]
    gate = await run_conflict_gate(
        policies, edges_used=edges, locate_fulltext=True, max_locate=3
    )
    print(f"gate_status={gate.gate_status} conflict_count={len(gate.conflict_pairs)}")
    for c in gate.conflict_pairs:
        print(f"  - {c.conflict_type} ({c.severity}): {c.description[:120]}")

    print("\n" + "=" * 70)
    print("6) 正文摘要（人工对照用，各截断 1200 字）")
    print("=" * 70)
    for p in (a, b):
        print(f"\n##### id={p['id']} deadline={p.get('deadline')} funding={p.get('funding_amount')}")
        print(f"key_conditions={(p.get('key_conditions') or '')[:300]}")
        print(f"full_text[:1200]=\n{(p.get('full_text') or '')[:1200]}")
        print("---")

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
