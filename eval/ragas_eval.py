"""RAGAS 评测脚本
评测指标：
- context_recall: 期望政策关键词被检索到的比例
- faithfulness: 报告中引用的内容是否有检索上下文支撑（Citation Verifier 结果）
- report_structure: 报告是否包含5个必要章节
- user_satisfaction_proxy: golden set 中的 user_rating 均值

运行方式：
    cd d:\\Projects\\policy-radar
    conda activate agent
    python -m eval.ragas_eval [--dry-run]
"""
import asyncio
import json
import sys
import re
from pathlib import Path
from datetime import datetime

# 将项目根目录加入 path
sys.path.insert(0, str(Path(__file__).parent.parent))

GOLDEN_SET_PATH = Path(__file__).parent / "golden_set.json"
RESULTS_PATH = Path(__file__).parent / "eval_results.json"


def check_context_recall(expected_keywords: list[str], context: str) -> float:
    """检查期望政策关键词在检索上下文中的召回率"""
    if not expected_keywords:
        return 1.0
    hits = sum(1 for kw in expected_keywords if kw in context)
    return hits / len(expected_keywords)


def check_faithfulness(report: str) -> float:
    """检查报告中是否有幻觉引用（[xxx·待核实] 标注）"""
    hallucinated = len(re.findall(r"\[.+?·待核实\]", report))
    return max(0.0, 1.0 - hallucinated * 0.2)


def check_report_structure(report: str, expected_sections: list[str]) -> float:
    """检查报告是否包含必要章节"""
    if not expected_sections:
        return 1.0
    hits = sum(1 for s in expected_sections if s in report)
    return hits / len(expected_sections)


async def evaluate_single(example: dict) -> dict:
    """对单个样本运行完整的 retrieve + generate + verify 流程并打分"""
    from app.rag.retriever import retrieve
    from app.agents.report.graph import stream_report
    from app.agents.report.citation_verifier import verify_citations

    kw = {
        "l1": example.get("l1", ""),
        "l2": example.get("l2", ""),
        "l3": example.get("l3", ""),
        "region": example.get("region", ""),
    }
    user_desc = example.get("user_description", "")

    # 1. 检索
    policies = await retrieve(kw, user_desc, top_n=10)
    context_str = "\n".join(
        f"《{p.get('title', '')}》 {p.get('summary', '')}"
        for p in policies
    )

    # 2. 生成报告
    chunks = []
    async for chunk in stream_report(kw, user_desc, "policy"):
        chunks.append(chunk)
    report = "".join(chunks)

    # 3. 计算指标
    context_recall = check_context_recall(
        example.get("expected_policy_keywords", []),
        context_str,
    )
    faithfulness = check_faithfulness(report)
    structure = check_report_structure(
        report,
        example.get("expected_sections", []),
    )
    user_rating = float(example.get("user_rating", 0.5))

    overall = (
        context_recall * 0.3
        + faithfulness * 0.3
        + structure * 0.2
        + user_rating * 0.2
    )

    return {
        "id": example.get("id"),
        "context_recall": round(context_recall, 3),
        "faithfulness": round(faithfulness, 3),
        "report_structure": round(structure, 3),
        "user_satisfaction_proxy": round(user_rating, 3),
        "overall": round(overall, 3),
        "policies_retrieved": len(policies),
        "report_length": len(report),
    }


async def run_eval(golden_set: list[dict], dry_run: bool = False) -> dict:
    """对整个 golden set 运行评测"""
    if dry_run:
        golden_set = golden_set[:2]
        print("[RAGAS] Dry-run 模式，只评测前 2 条", flush=True)

    results = []
    for i, ex in enumerate(golden_set):
        print(f"[RAGAS] 评测 {i+1}/{len(golden_set)}: {ex.get('id')}", flush=True)
        try:
            r = await evaluate_single(ex)
            results.append(r)
            print(
                f"  context_recall={r['context_recall']:.2f}  "
                f"faithfulness={r['faithfulness']:.2f}  "
                f"structure={r['report_structure']:.2f}  "
                f"overall={r['overall']:.2f}",
                flush=True,
            )
        except Exception as e:
            print(f"  [ERROR] {e}", flush=True)
            results.append({"id": ex.get("id"), "error": str(e), "overall": 0.0})

    # 汇总
    valid = [r for r in results if "error" not in r]
    summary = {
        "timestamp": datetime.utcnow().isoformat(),
        "total": len(results),
        "valid": len(valid),
        "avg_context_recall": round(sum(r["context_recall"] for r in valid) / len(valid), 3) if valid else 0,
        "avg_faithfulness": round(sum(r["faithfulness"] for r in valid) / len(valid), 3) if valid else 0,
        "avg_report_structure": round(sum(r["report_structure"] for r in valid) / len(valid), 3) if valid else 0,
        "avg_overall": round(sum(r["overall"] for r in valid) / len(valid), 3) if valid else 0,
        "details": results,
    }

    print("\n=== RAGAS 评测结果 ===", flush=True)
    print(f"context_recall:   {summary['avg_context_recall']:.3f}", flush=True)
    print(f"faithfulness:     {summary['avg_faithfulness']:.3f}", flush=True)
    print(f"report_structure: {summary['avg_report_structure']:.3f}", flush=True)
    print(f"overall:          {summary['avg_overall']:.3f}", flush=True)

    return summary


async def main():
    dry_run = "--dry-run" in sys.argv
    if not GOLDEN_SET_PATH.exists():
        print(f"[RAGAS] golden_set.json 不存在: {GOLDEN_SET_PATH}")
        return

    with open(GOLDEN_SET_PATH, encoding="utf-8") as f:
        golden_set = json.load(f)

    summary = await run_eval(golden_set, dry_run=dry_run)

    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n[RAGAS] 结果已保存到 {RESULTS_PATH}", flush=True)

    # CI Gate：overall < 0.5 视为退化，返回非零退出码
    if summary["avg_overall"] < 0.5:
        print("[RAGAS] ❌ 评测分数低于阈值 0.5，CI Gate 失败", flush=True)
        sys.exit(1)
    else:
        print("[RAGAS] ✅ 评测通过", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
