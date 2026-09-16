"""DSPy MIPROv2 离线优化器（Reflexion 自进化核心）

工作流程（"harness 学，模型不动"）：
1. 从数据库读取低分反馈样本（score < 0.6 的 explicit_rating）
2. 从 eval/golden_set.json 加载标注样本
3. 用 DSPy MIPROv2 在反馈样本 + golden set 上优化 PolicyReportModule 的 prompt
4. 在 golden set 上评估优化效果
5. 若指标提升 > 阈值，晋升（promote）新 prompt，写入 DB 记录
6. 生成 audit log

运行方式（手动触发 or APScheduler 周期任务）：
    python -m app.reflexion.optimizer
"""
import json
import os
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

GOLDEN_SET_PATH = Path(__file__).parent.parent.parent / "eval" / "golden_set.json"
OPTIMIZED_MODULE_PATH = Path(__file__).parent.parent.parent / "eval" / "optimized_module.json"
PROMOTE_THRESHOLD = 0.05  # eval 分数需提升超过 5% 才晋升


# ── 评估指标 ──────────────────────────────────────────────────────────────────

def report_quality_metric(example: dict, prediction, trace=None) -> float:
    """综合评分：引用忠实度 + 结构完整性 + 用户评分
    
    返回 0~1 的分数，越高越好。
    """
    report = getattr(prediction, "report", "") or ""
    if not report:
        return 0.0

    score = 0.0

    # 1. 结构完整性（5个章节各 0.1 分）
    required_sections = ["一、", "二、", "三、", "四、", "五、"]
    structure_score = sum(0.1 for s in required_sections if s in report)
    score += structure_score

    # 2. 引用忠实度（避免 [xxx·待核实] 标注，有则扣分）
    import re
    hallucinated = len(re.findall(r"\[.+?·待核实\]", report))
    faithfulness_score = max(0.0, 0.3 - hallucinated * 0.1)
    score += faithfulness_score

    # 3. 长度合理性（300~3000 字为合理区间）
    length = len(report)
    if 300 <= length <= 3000:
        score += 0.2

    # 4. 用户反馈分数（若有）
    user_rating = example.get("user_rating", 0.5)
    score += user_rating * 0.3

    return min(score, 1.0)


# ── 样本加载 ──────────────────────────────────────────────────────────────────

def load_golden_set() -> list[dict]:
    """加载标注好的 golden set"""
    if not GOLDEN_SET_PATH.exists():
        print(f"[Reflexion] Golden set 不存在: {GOLDEN_SET_PATH}", flush=True)
        return []
    with open(GOLDEN_SET_PATH, encoding="utf-8") as f:
        return json.load(f)


async def load_feedback_examples(min_count: int = 10) -> list[dict]:
    """从数据库加载低分反馈样本（score < 0.6 的 explicit_rating）"""
    try:
        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT f.session_id, f.score, f.metadata,
                       q.keywords, q.user_input
                FROM feedback f
                JOIN query_logs q ON q.session_id = f.session_id
                WHERE f.signal_type = 'explicit_rating'
                  AND f.score < 0.6
                  AND f.created_at > NOW() - INTERVAL '30 days'
                ORDER BY f.created_at DESC
                LIMIT 100
                """
            )
        examples = []
        for r in rows:
            kw = r["keywords"] or {}
            if isinstance(kw, str):
                import json as _json
                try:
                    kw = _json.loads(kw)
                except Exception:
                    kw = {}
            examples.append({
                "keywords_text": "、".join(filter(None, [
                    kw.get("l1"), kw.get("l2"), kw.get("l3"),
                ])),
                "user_description": r["user_input"] or "",
                "retrieved_policies": "",  # 需要重新检索
                "user_rating": float(r["score"] or 0.3),
            })
        print(f"[Reflexion] 加载反馈样本 {len(examples)} 条", flush=True)
        return examples
    except Exception as e:
        print(f"[Reflexion] 加载反馈样本失败: {e}", flush=True)
        return []


# ── 主优化流程 ────────────────────────────────────────────────────────────────

async def run_optimization(dry_run: bool = False) -> dict:
    """执行一次完整的 DSPy MIPROv2 优化"""
    import dspy
    from app.reflexion.dspy_modules import PolicyReportModule, configure_dspy
    from app.rag.retriever import retrieve

    print("[Reflexion] === 开始 DSPy MIPROv2 优化 ===", flush=True)
    configure_dspy()

    golden = load_golden_set()
    feedback_examples = await load_feedback_examples()

    if len(golden) < 5:
        return {"status": "skipped", "reason": f"golden set 样本不足（{len(golden)} < 5）"}

    # 反馈数量门槛：不足时跳过，避免在样本太少时优化效果不稳定
    MIN_FEEDBACK = 10
    if len(feedback_examples) < MIN_FEEDBACK:
        return {
            "status": "skipped",
            "reason": f"用户反馈不足（当前 {len(feedback_examples)} 条，需 {MIN_FEEDBACK} 条）",
        }

    # 填充 retrieved_policies（对 golden set 样本重新检索）
    trainset = []
    for ex in golden[:30] + feedback_examples[:20]:  # 最多 50 条训练样本
        kw = {"l1": ex.get("l1", ""), "l2": ex.get("l2", ""), "region": ex.get("region", "")}
        try:
            policies = await retrieve(kw, ex.get("user_description", ""), top_n=8)
            context = "\n".join(
                f"[政策] 《{p.get('title', '')}》 {p.get('summary', '')}"
                for p in policies[:5]
            )
        except Exception:
            context = ""
        trainset.append(
            dspy.Example(
                keywords_text=ex.get("keywords_text", ""),
                user_description=ex.get("user_description", ""),
                retrieved_policies=context,
                user_rating=ex.get("user_rating", 0.5),
            ).with_inputs("keywords_text", "user_description", "retrieved_policies")
        )

    if not trainset:
        return {"status": "skipped", "reason": "无有效训练样本"}

    # 基线评分
    baseline_module = PolicyReportModule()
    baseline_scores = []
    for ex in trainset[:10]:
        try:
            pred = baseline_module(**{k: ex[k] for k in ex.inputs()})
            baseline_scores.append(report_quality_metric(ex.toDict(), pred))
        except Exception:
            baseline_scores.append(0.0)
    baseline_avg = sum(baseline_scores) / len(baseline_scores) if baseline_scores else 0.0
    print(f"[Reflexion] 基线分数: {baseline_avg:.3f}", flush=True)

    if dry_run:
        return {"status": "dry_run", "baseline_score": baseline_avg, "trainset_size": len(trainset)}

    # 运行 MIPROv2 优化（在 executor 中运行，避免阻塞事件循环）
    def _optimize():
        optimizer = dspy.MIPROv2(
            metric=report_quality_metric,
            auto="light",      # light/medium/heavy，light 约 10-20 分钟
            num_threads=1,     # 单线程避免 API 并发限制
        )
        optimized = optimizer.compile(
            PolicyReportModule(),
            trainset=trainset,
        )
        return optimized

    print("[Reflexion] 运行 MIPROv2 优化（auto=light）...", flush=True)
    try:
        optimized = await asyncio.get_event_loop().run_in_executor(None, _optimize)
    except Exception as e:
        return {"status": "failed", "error": str(e)}

    # 评估优化后的模块
    new_scores = []
    for ex in trainset[:10]:
        try:
            pred = optimized(**{k: ex[k] for k in ex.inputs()})
            new_scores.append(report_quality_metric(ex.toDict(), pred))
        except Exception:
            new_scores.append(0.0)
    new_avg = sum(new_scores) / len(new_scores) if new_scores else 0.0
    improvement = new_avg - baseline_avg
    print(f"[Reflexion] 优化后分数: {new_avg:.3f} (提升: {improvement:+.3f})", flush=True)

    result = {
        "status": "evaluated",
        "baseline_score": round(baseline_avg, 4),
        "new_score": round(new_avg, 4),
        "improvement": round(improvement, 4),
        "trainset_size": len(trainset),
        "promoted": False,
    }

    # 晋升判断
    if improvement >= PROMOTE_THRESHOLD:
        optimized.save(str(OPTIMIZED_MODULE_PATH))
        await _record_patch(baseline_avg, new_avg, improvement, "promoted")
        result["promoted"] = True
        print(f"[Reflexion] ✅ 已晋升新 prompt（改善 {improvement:.1%}）", flush=True)

        # 热加载新模块
        from app.reflexion.dspy_modules import load_optimized_module
        load_optimized_module(str(OPTIMIZED_MODULE_PATH))
    else:
        await _record_patch(baseline_avg, new_avg, improvement, "rejected")
        result["rejected_reason"] = f"提升 {improvement:.1%} 未达阈值 {PROMOTE_THRESHOLD:.1%}"
        print(f"[Reflexion] ❌ 优化效果不足，维持现有 prompt", flush=True)

    return result


async def _record_patch(baseline: float, new: float, improvement: float, status: str):
    """记录优化结果到 reflexion_patches 表"""
    try:
        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO reflexion_patches
                    (status, patch_type, target, diff, baseline_scores, new_scores, promoted_at)
                VALUES ($1, 'prompt', 'PolicyReportSignature', $2, $3::jsonb, $4::jsonb, $5)
                """,
                status,
                f"MIPROv2 自动优化，改善 {improvement:+.1%}",
                json.dumps({"avg": baseline}),
                json.dumps({"avg": new}),
                datetime.utcnow() if status == "promoted" else None,
            )
    except Exception as e:
        print(f"[Reflexion] 记录补丁失败: {e}", flush=True)


if __name__ == "__main__":
    result = asyncio.run(run_optimization(dry_run=False))
    print(json.dumps(result, indent=2, ensure_ascii=False))
