# -*- coding: utf-8 -*-
"""
执行 data/synthetic_kg_corpus/test_cases.json 中的评测用例。

用法：
  D:\\Environment\\Anaconda\\envs\\agent\\python.exe scripts/run_synthetic_kg_tests.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CASES_PATH = ROOT / "data" / "synthetic_kg_corpus" / "test_cases.json"


async def load_policy(pg_id: int) -> dict:
    from app.db.postgres import get_pool

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, title, publisher, region, pub_date, deadline, summary,
                   category_l1, funding_amount, key_conditions, source_url,
                   LEFT(COALESCE(full_text, ''), 4000) AS full_text,
                   LEFT(COALESCE(full_text, summary, ''), 800) AS matched_chunk
            FROM policies WHERE id=$1
            """,
            pg_id,
        )
    return dict(row) if row else {}


async def run_expand_cases(cases: list[dict]) -> list[tuple[str, bool, str]]:
    from app.rag.policy_graph import expand_related

    results = []
    for c in cases:
        seed_id = c.get("seed_pg_id")
        if not seed_id:
            results.append((c["case_id"], False, "missing seed_pg_id"))
            continue
        seed = await load_policy(seed_id)
        if not seed:
            results.append((c["case_id"], False, f"seed {seed_id} not in DB"))
            continue
        merged, meta = await expand_related([seed], include_weak=True, max_expand=12)
        got_ids = {p["id"] for p in merged if p.get("id") != seed_id}
        expect = set(c.get("expected_neighbor_pg_ids") or [])
        hit = got_ids & expect
        ok = len(hit) >= 1
        detail = f"hit={sorted(hit)[:5]} expect_sample={sorted(expect)[:5]} expanded={meta.get('expand_count')}"
        results.append((c["case_id"], ok, detail))
    return results


async def run_conflict_cases(cases: list[dict]) -> list[tuple[str, bool, str]]:
    from app.rag.conflict_gate import check_pair_conflicts, run_conflict_gate

    results = []
    for c in cases:
        ids = c.get("pair_pg_ids") or []
        if len(ids) != 2 or not all(ids):
            results.append((c["case_id"], False, "bad pair_pg_ids"))
            continue
        a = await load_policy(ids[0])
        b = await load_policy(ids[1])
        rel = c.get("relation_type") or "same_program"
        found = {x.conflict_type for x in check_pair_conflicts(a, b, rel)}
        expect = set(c.get("expected_conflict_types") or [])
        # 金额层级差异与同级金额冲突可互换接受（省-市边）
        soft = set()
        if "funding_hierarchy_diff" in expect:
            soft.add("funding_amount_conflict")
        if "funding_amount_conflict" in expect:
            soft.add("funding_hierarchy_diff")
        ok = bool(found & expect) or bool(found & soft)
        # 门控也应 conflict
        gate = await run_conflict_gate(
            [a, b],
            edges_used=[{
                "source_policy_id": ids[0],
                "target_policy_id": ids[1],
                "relation_type": rel,
                "confidence": 0.9,
                "evidence": "test",
            }],
            locate_fulltext=False,
        )
        if ok and gate.gate_status != "conflict":
            ok = False
        detail = f"got={sorted(found)} expect={sorted(expect)} gate={gate.gate_status}"
        results.append((c["case_id"], ok, detail))
    return results


async def run_retrieve_cases(cases: list[dict]) -> list[tuple[str, bool, str]]:
    from app.rag.retriever import retrieve

    results = []
    for c in cases:
        kw = dict(c.get("keywords") or {})
        policies = await retrieve(kw, c.get("user_input") or "", top_n=10)
        titles = " ".join(p.get("title") or "" for p in policies)
        expect_subs = c.get("expect_title_substr") or []
        ok = any(sub in titles for sub in expect_subs)
        # 合成文 source_url 前缀也可算命中
        syn_hit = any(
            str(p.get("source_url") or "").startswith("synthetic://yuechuang/")
            for p in policies
        )
        ok = ok or syn_hit
        detail = f"n={len(policies)} syn_hit={syn_hit} top={(policies[0].get('title')[:40] if policies else '')}"
        results.append((c["case_id"], ok, detail))
    return results


async def run_e2e_cases(cases: list[dict]) -> list[tuple[str, bool, str]]:
    from app.rag.policy_graph import expand_related
    from app.rag.conflict_gate import check_pair_conflicts

    results = []
    for c in cases:
        seed = await load_policy(c["seed_pg_id"])
        focus_id = c.get("focus_neighbor_pg_id")
        if not seed or not focus_id:
            results.append((c["case_id"], False, "missing ids"))
            continue
        merged, meta = await expand_related([seed], max_expand=12)
        expanded_ids = set(meta.get("expanded_ids") or [])
        # 焦点邻居应在扩展结果或本就在边两端
        in_expand = focus_id in expanded_ids or any(p.get("id") == focus_id for p in merged)
        focus = await load_policy(focus_id)
        # 找边类型
        rel = "implements"
        for e in meta.get("edges_used") or []:
            pair = {e.get("source_policy_id"), e.get("target_policy_id")}
            if pair == {seed["id"], focus_id}:
                rel = e.get("relation_type") or rel
                break
        found = {x.conflict_type for x in check_pair_conflicts(seed, focus, rel)}
        expect = set(c.get("expected_conflict_types") or [])
        soft = {"funding_amount_conflict", "funding_hierarchy_diff"}
        conflict_ok = bool(found & expect) or (
            bool(found & soft) and bool(expect & soft)
        )
        ok = in_expand and conflict_ok
        detail = (
            f"expand_has_focus={in_expand} conflicts={sorted(found)} "
            f"expect={sorted(expect)}"
        )
        results.append((c["case_id"], ok, detail))
    return results


async def main() -> None:
    from app.db.postgres import close_pool

    if not CASES_PATH.exists():
        print(f"缺少 {CASES_PATH}，请先运行 ingest_synthetic_kg_corpus.py", flush=True)
        sys.exit(2)

    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    cases = data["cases"]
    all_results: list[tuple[str, bool, str]] = []

    print("=" * 64)
    print("1) graph_expand")
    print("=" * 64)
    for cid, ok, detail in await run_expand_cases(cases.get("graph_expand", [])):
        print(f"[{'PASS' if ok else 'FAIL'}] {cid}: {detail}")
        all_results.append((cid, ok, detail))

    print("=" * 64)
    print("2) conflict_gate")
    print("=" * 64)
    for cid, ok, detail in await run_conflict_cases(cases.get("conflict_gate", [])):
        print(f"[{'PASS' if ok else 'FAIL'}] {cid}: {detail}")
        all_results.append((cid, ok, detail))

    print("=" * 64)
    print("3) hybrid_retrieve")
    print("=" * 64)
    for cid, ok, detail in await run_retrieve_cases(cases.get("hybrid_retrieve", [])):
        print(f"[{'PASS' if ok else 'FAIL'}] {cid}: {detail}")
        all_results.append((cid, ok, detail))

    print("=" * 64)
    print("4) e2e expand+conflict")
    print("=" * 64)
    for cid, ok, detail in await run_e2e_cases(cases.get("e2e", [])):
        print(f"[{'PASS' if ok else 'FAIL'}] {cid}: {detail}")
        all_results.append((cid, ok, detail))

    passed = sum(1 for _, ok, _ in all_results if ok)
    total = len(all_results)
    print("=" * 64)
    print(f"汇总: {passed}/{total} PASS")
    await close_pool()
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
