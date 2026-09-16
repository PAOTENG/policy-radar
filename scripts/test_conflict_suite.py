"""
冲突门控扩展测试套件

覆盖：
  A. 五类冲突合成用例（应检出 / 不应误报）
  B. 知识库强关联对：125-141（期望 pass）
  C. 知识库扫描：在强边上找「槽位有差异」的真实对，跑门控并汇报

用法：
  D:\\Environment\\Anaconda\\envs\\agent\\python.exe scripts/test_conflict_suite.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _base(**kwargs):
    d = {
        "id": 0,
        "title": "测试政策",
        "region": "广东",
        "publisher": "测试厅",
        "funding_amount": "",
        "deadline": None,
        "key_conditions": "",
        "full_text": "",
        "matched_chunk": "",
        "summary": "",
        "category_l1": "资金支持",
    }
    d.update(kwargs)
    return d


def test_synthetic() -> list[str]:
    """合成用例：期望命中/期望不命中。"""
    from app.rag.conflict_gate import check_pair_conflicts
    from app.agents.report.policy_verifier import _extract_amount_wan

    failures: list[str] = []

    # --- 金额抽取防误报 ---
    for text in [
        "电话：020-83163946、83163873",
        "粤科公示〔2026〕12号 2026年6月18日",
        "邮编：510033",
    ]:
        if _extract_amount_wan(text) != 0:
            failures.append(f"金额误抽: {text!r} -> {_extract_amount_wan(text)}")

    if abs(_extract_amount_wan("最高补贴500万元") - 500) > 1e-6:
        failures.append("金额漏抽: 500万元")

    cases = [
        (
            "deadline_conflict",
            _base(
                id=1, title="A截止早", deadline="2026-12-01",
                full_text="申报截止：2026-12-01",
            ),
            _base(
                id=2, title="B截止晚", deadline="2026-12-20",
                full_text="申报截止：2026-12-20",
            ),
            "same_program",
            {"deadline_conflict"},
        ),
        (
            "funding_amount_conflict",
            _base(
                id=3, title="同级金额大", funding_amount="最高补贴500万元",
                full_text="最高补贴500万元",
            ),
            _base(
                id=4, title="同级金额小", funding_amount="最高补贴100万元",
                full_text="最高补贴100万元",
            ),
            "same_program",
            {"funding_amount_conflict"},
        ),
        (
            "funding_hierarchy_diff",
            _base(
                id=5, title="省级金额", funding_amount="最高补贴500万元",
                full_text="最高补贴500万元", region="广东",
            ),
            _base(
                id=6, title="市级金额", funding_amount="最高补贴100万元",
                full_text="最高补贴100万元", region="深圳",
            ),
            "implements",
            {"funding_hierarchy_diff"},
        ),
        (
            "eligibility_conflict",
            _base(
                id=7, title="高门槛",
                key_conditions="注册资本不少于500万元；成立满2年",
                full_text="注册资本不少于500万元。成立满2年。",
            ),
            _base(
                id=8, title="低门槛",
                key_conditions="注册资本不少于200万元；成立满1年",
                full_text="注册资本不少于200万元。成立满1年。",
            ),
            "same_program",
            {"eligibility_conflict"},
        ),
        (
            "legal_validity_conflict",
            _base(
                id=9, title="旧文废止",
                full_text="本办法自公布之日起废止原有规定。",
            ),
            _base(
                id=10, title="现行文",
                full_text="本办法自2026年1月1日起施行。",
            ),
            "supersedes",
            {"legal_validity_conflict"},
        ),
        (
            "exclusivity_conflict",
            _base(
                id=11, title="含互斥",
                full_text="同一项目不得重复申报，就高不就低。最高补贴50万元。",
                funding_amount="最高补贴50万元",
            ),
            _base(
                id=12, title="无互斥表述",
                full_text="最高补贴50万元。",
                funding_amount="最高补贴50万元",
            ),
            "complements",
            {"exclusivity_conflict"},
        ),
        (
            "no_false_phone_amount",
            _base(
                id=13, title="搬迁公告",
                full_text="完成异地搬迁后高新技术企业资格继续有效。2026年7月6日。",
            ),
            _base(
                id=14, title="更名公示",
                full_text="拟更名高新技术企业名单公示。电话：020-83163946。邮编：510033。",
            ),
            "same_program",
            set(),  # 期望零冲突
        ),
    ]

    print("=" * 64)
    print("A. 合成用例（五类 + 防误报）")
    print("=" * 64)
    for name, a, b, rel, expect in cases:
        got = {c.conflict_type for c in check_pair_conflicts(a, b, rel)}
        # 允许额外检出，但必须包含 expect；若 expect 为空则必须完全为空
        if expect:
            ok = expect.issubset(got)
        else:
            ok = len(got) == 0
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}")
        print(f"       expect⊆ {sorted(expect) or '∅'}  got={sorted(got) or '∅'}")
        if not ok:
            failures.append(f"{name}: expect {expect}, got {got}")
            for c in check_pair_conflicts(a, b, rel):
                print(f"         - {c.conflict_type}: {c.description[:80]}")
    return failures


async def test_db_pair_125_141() -> list[str]:
    from app.db.postgres import get_pool
    from app.rag.conflict_gate import run_conflict_gate

    failures: list[str] = []
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, title, publisher, region, pub_date, deadline, summary,
                   category_l1, funding_amount, key_conditions, source_url,
                   LEFT(COALESCE(full_text, ''), 4000) AS full_text,
                   LEFT(COALESCE(full_text, summary, ''), 800) AS matched_chunk
            FROM policies WHERE id = ANY($1::int[])
            """,
            [125, 141],
        )
    policies = [dict(r) for r in rows]
    if len(policies) < 2:
        return ["DB缺少 125/141"]

    gate = await run_conflict_gate(
        policies,
        edges_used=[{
            "source_policy_id": 125,
            "target_policy_id": 141,
            "relation_type": "same_program",
            "confidence": 0.65,
            "evidence": "test",
        }],
        locate_fulltext=False,
    )
    print("=" * 64)
    print("B. 知识库对 125↔141（期望 gate=pass, conflicts=0）")
    print("=" * 64)
    ok = gate.gate_status == "pass" and len(gate.conflict_pairs) == 0
    print(f"[{'PASS' if ok else 'FAIL'}] gate={gate.gate_status} n={len(gate.conflict_pairs)}")
    if not ok:
        failures.append(f"125/141 期望pass，实际 {gate.gate_status} {len(gate.conflict_pairs)}")
        for c in gate.conflict_pairs:
            print(f"  - {c.conflict_type}: {c.description[:100]}")
    return failures


async def test_db_scan_real_diffs(limit_pairs: int = 40) -> list[str]:
    """扫描强边，找槽位上有真实差异的对，展示门控结果。"""
    from app.db.postgres import get_pool
    from app.rag.conflict_gate import (
        extract_slots,
        check_pair_conflicts,
        run_conflict_gate,
    )

    failures: list[str] = []
    strong = ["implements", "same_program", "lists_under", "funding_of", "supersedes"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        edges = await conn.fetch(
            """
            SELECT r.source_policy_id, r.target_policy_id, r.relation_type,
                   r.confidence, r.evidence,
                   s.title AS stitle, t.title AS ttitle
            FROM policy_relations r
            JOIN policies s ON s.id = r.source_policy_id
            JOIN policies t ON t.id = r.target_policy_id
            WHERE r.relation_type = ANY($1::text[])
              AND r.confidence >= 0.5
              AND s.category_l1 IS DISTINCT FROM '其他'
              AND t.category_l1 IS DISTINCT FROM '其他'
              AND s.title NOT LIKE '%抱歉%'
              AND t.title NOT LIKE '%抱歉%'
              AND s.title NOT LIKE '%新闻发布会%'
              AND t.title NOT LIKE '%新闻发布会%'
            ORDER BY r.confidence DESC, r.id
            LIMIT $2
            """,
            strong,
            limit_pairs * 3,
        )

        # 批量取正文
        ids = set()
        for e in edges:
            ids.add(e["source_policy_id"])
            ids.add(e["target_policy_id"])
        rows = await conn.fetch(
            """
            SELECT id, title, publisher, region, pub_date, deadline, summary,
                   category_l1, funding_amount, key_conditions, source_url,
                   LEFT(COALESCE(full_text, ''), 3500) AS full_text,
                   LEFT(COALESCE(full_text, summary, ''), 800) AS matched_chunk
            FROM policies WHERE id = ANY($1::int[])
            """,
            list(ids),
        )
    by_id = {r["id"]: dict(r) for r in rows}

    interesting = []  # (conflicts, edge, a, b)
    scanned = 0
    for e in edges:
        a = by_id.get(e["source_policy_id"])
        b = by_id.get(e["target_policy_id"])
        if not a or not b:
            continue
        # 跳过标题几乎相同的重复入库
        if (a.get("title") or "").strip() == (b.get("title") or "").strip():
            continue
        scanned += 1
        cs = check_pair_conflicts(a, b, e["relation_type"])
        if cs:
            interesting.append((cs, e, a, b))
        if scanned >= limit_pairs and len(interesting) >= 8:
            break

    print("=" * 64)
    print(f"C. 知识库强边扫描（扫描≈{scanned} 对，有冲突命中 {len(interesting)} 对）")
    print("=" * 64)

    if not interesting:
        print("未在样本强边中扫到五类冲突命中（库内多数是公示/公告，金额截止字段稀疏）。")
        print("下面展示 3 对「槽位摘要」供人工看是否该冲突：")
        shown = 0
        for e in edges:
            a = by_id.get(e["source_policy_id"])
            b = by_id.get(e["target_policy_id"])
            if not a or not b:
                continue
            if (a.get("title") or "").strip() == (b.get("title") or "").strip():
                continue
            sa, sb = extract_slots(a), extract_slots(b)
            print(f"\n  edge={e['relation_type']} {e['source_policy_id']}↔{e['target_policy_id']}")
            print(f"    A《{(a.get('title') or '')[:50]}》 amt={sa['amount_wan']} dl={sa['deadline']} elig={sa['eligibility_hits']}")
            print(f"    B《{(b.get('title') or '')[:50]}》 amt={sb['amount_wan']} dl={sb['deadline']} elig={sb['eligibility_hits']}")
            shown += 1
            if shown >= 3:
                break
        return failures

    # 打印前若干有冲突的真实对，并跑门控
    for i, (cs, e, a, b) in enumerate(interesting[:8], 1):
        print(f"\n【库内冲突样本 {i}】{e['relation_type']} conf={e['confidence']}")
        print(f"  A.id={a['id']} 《{(a.get('title') or '')[:60]}》")
        print(f"  B.id={b['id']} 《{(b.get('title') or '')[:60]}》")
        for c in cs:
            print(f"  -> {c.conflict_type} ({c.severity}): {c.description[:100]}")

        gate = await run_conflict_gate(
            [a, b],
            edges_used=[{
                "source_policy_id": e["source_policy_id"],
                "target_policy_id": e["target_policy_id"],
                "relation_type": e["relation_type"],
                "confidence": float(e["confidence"] or 0),
                "evidence": e["evidence"] or "",
            }],
            locate_fulltext=False,
        )
        print(f"  gate={gate.gate_status} n={len(gate.conflict_pairs)}")
        if gate.gate_status != "conflict":
            failures.append(
                f"库内对 {a['id']}-{b['id']} 规则有冲突但 gate!={gate.gate_status}"
            )

    return failures


async def main() -> None:
    from app.db.postgres import close_pool

    fails: list[str] = []
    fails.extend(test_synthetic())
    fails.extend(await test_db_pair_125_141())
    fails.extend(await test_db_scan_real_diffs(limit_pairs=50))
    await close_pool()

    print("\n" + "=" * 64)
    if fails:
        print(f"汇总: FAIL ({len(fails)})")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print("汇总: ALL PASS")
    print("说明: 125↔141 为 0 冲突是预期（同项目不同环节，无金额/截止/门槛数值冲突）。")
    print("      合成用例覆盖五类真冲突；C 段展示库内真实命中对（若有）。")


if __name__ == "__main__":
    asyncio.run(main())
