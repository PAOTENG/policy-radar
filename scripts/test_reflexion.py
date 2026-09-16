"""
Reflexion 自进化闭环 端到端测试
运行方式：conda run -n agent python scripts/test_reflexion.py

测试内容：
  1. DSPy 配置（LM 后端连通性）
  2. PolicyReportModule 能正常生成报告
  3. 评估指标函数能正常打分
  4. Golden set 加载
  5. 优化器 dry_run（不真正跑 MIPROv2，验证数据管道）
  6. APScheduler 任务注册状态
  7. 数据库 reflexion_patches 表可写入
"""
import sys
import os
import asyncio
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─── 1. DSPy 配置 ──────────────────────────────────────────────
def test_dspy_config():
    print("\n━━━ [1/7] DSPy LM 后端配置 ━━━")
    try:
        from app.reflexion.dspy_modules import configure_dspy
        lm = configure_dspy()
        print(f"  ✅ DSPy LM 已配置: {lm.model}")
        return True
    except Exception as e:
        print(f"  ❌ DSPy 配置失败: {e}")
        return False


# ─── 2. PolicyReportModule 推理 ─────────────────────────────────
def test_module_inference():
    print("\n━━━ [2/7] PolicyReportModule 推理（小规模）━━━")
    try:
        import dspy
        from app.reflexion.dspy_modules import get_module
        module = get_module()
        t0 = time.perf_counter()
        pred = module(
            keywords_text="科技 人工智能 申报 广东",
            user_description="深圳一家 50 人的 AI 软件公司，成立 3 年，年收入 500 万",
            retrieved_policies="[政策1] 《广东省人工智能发展专项资金》 申报条件：AI 企业，研发投入≥10%",
        )
        elapsed = int((time.perf_counter() - t0) * 1000)
        report = getattr(pred, "report", "") or ""
        print(f"  ✅ 推理成功 ({elapsed}ms)")
        print(f"  📄 报告前 100 字: {report[:100].strip()}...")
        return True
    except Exception as e:
        print(f"  ❌ 推理失败: {e}")
        return False


# ─── 3. 评估指标函数 ────────────────────────────────────────────
def test_metric():
    print("\n━━━ [3/7] 评估指标函数 report_quality_metric ━━━")
    try:
        from app.reflexion.optimizer import report_quality_metric

        class FakePred:
            report = "一、政策总览\n\n二、重点政策\n\n三、申报时间表\n\n四、行动建议\n\n五、信息来源\n" * 10

        score = report_quality_metric({"user_rating": 0.8}, FakePred())
        print(f"  ✅ 评分函数正常，示例得分: {score:.3f}")
        assert 0 <= score <= 1, f"分数超范围: {score}"

        class BadPred:
            report = ""

        zero_score = report_quality_metric({}, BadPred())
        assert zero_score == 0.0
        print(f"  ✅ 空报告得 0 分: {zero_score}")
        return True
    except Exception as e:
        print(f"  ❌ 评估指标异常: {e}")
        return False


# ─── 4. Golden Set 加载 ─────────────────────────────────────────
def test_golden_set():
    print("\n━━━ [4/7] Golden Set 加载 ━━━")
    try:
        from app.reflexion.optimizer import load_golden_set
        golden = load_golden_set()
        if not golden:
            print("  ⚠️  Golden set 为空，请补充 eval/golden_set.json")
            return False
        print(f"  ✅ 加载 {len(golden)} 条标注样本")
        first = golden[0]
        required_keys = ["keywords_text", "user_description"]
        missing = [k for k in required_keys if k not in first]
        if missing:
            print(f"  ⚠️  样本缺少字段: {missing}")
        else:
            print(f"  ✅ 样本结构完整，示例: {first['keywords_text'][:40]}")
        return len(golden) >= 5
    except Exception as e:
        print(f"  ❌ Golden set 加载失败: {e}")
        return False


# ─── 5. 优化器 dry_run ──────────────────────────────────────────
async def test_optimizer_dry_run():
    print("\n━━━ [5/7] 优化器 dry_run（验证数据管道，不跑 MIPROv2）━━━")
    try:
        from app.reflexion.optimizer import run_optimization
        t0 = time.perf_counter()
        result = await run_optimization(dry_run=True)
        elapsed = int((time.perf_counter() - t0) * 1000)
        print(f"  ✅ dry_run 完成 ({elapsed}ms)")
        print(f"  📊 状态: {result.get('status')}")
        if result.get("status") == "dry_run":
            print(f"  📊 基线分数: {result.get('baseline_score', 'N/A')}")
            print(f"  📊 训练集大小: {result.get('trainset_size', 'N/A')}")
            return True
        elif result.get("status") == "skipped":
            print(f"  ⚠️  跳过原因: {result.get('reason')}")
            return False
        else:
            print(f"  ⚠️  未预期的状态: {result}")
            return False
    except Exception as e:
        print(f"  ❌ dry_run 失败: {e}")
        import traceback; traceback.print_exc()
        return False


# ─── 6. APScheduler 任务注册 ────────────────────────────────────
def test_scheduler_job():
    print("\n━━━ [6/7] APScheduler Reflexion 任务注册 ━━━")
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from app.reflexion.scheduler import create_reflexion_job
        scheduler = BackgroundScheduler()
        create_reflexion_job(scheduler)
        jobs = scheduler.get_jobs()
        reflexion_jobs = [j for j in jobs if "reflexion" in j.id.lower() or "reflexion" in str(j.func)]
        if reflexion_jobs:
            j = reflexion_jobs[0]
            print(f"  ✅ 任务已注册: id={j.id}")
            print(f"  ⏰ 触发器: {j.trigger}")
            return True
        elif jobs:
            print(f"  ⚠️  有 {len(jobs)} 个任务，但未找到 Reflexion 任务")
            for j in jobs:
                print(f"     - {j.id}: {j.trigger}")
            return False
        else:
            print("  ❌ 未注册任何任务")
            return False
    except Exception as e:
        print(f"  ❌ 调度器测试失败: {e}")
        return False


# ─── 7. reflexion_patches 表写入 ────────────────────────────────
async def test_db_write():
    print("\n━━━ [7/7] reflexion_patches 数据库写入 ━━━")
    try:
        from app.db.postgres import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            # 写入一条测试记录
            await conn.execute(
                """
                INSERT INTO reflexion_patches
                    (status, patch_type, target, diff, baseline_scores, new_scores)
                VALUES ('proposed', 'prompt', 'test-script',
                        'test dry-run patch', '{"avg":0.5}'::jsonb, '{"avg":0.5}'::jsonb)
                """,
            )
            count = await conn.fetchval("SELECT COUNT(*) FROM reflexion_patches")
        print(f"  ✅ 写入成功，reflexion_patches 表共 {count} 条记录")
        return True
    except Exception as e:
        print(f"  ❌ 数据库写入失败: {e}")
        return False


# ─── 主入口 ─────────────────────────────────────────────────────
async def main():
    print("=" * 56)
    print("  PolicyRadar · Reflexion 自进化闭环 端到端测试")
    print("=" * 56)

    sync_results = [
        test_dspy_config(),
        test_module_inference(),
        test_metric(),
        test_golden_set(),
        test_scheduler_job(),
    ]

    async_results = [
        await test_optimizer_dry_run(),
        await test_db_write(),
    ]

    results = sync_results + async_results
    passed = sum(results)
    total = len(results)

    print(f"\n{'=' * 56}")
    print(f"  结果: {passed}/{total} 通过")
    if passed == total:
        print("  🎉 Reflexion 闭环完全正常！")
        print("\n  下一步：")
        print("  • 积累真实用户反馈后，触发完整优化：")
        print("    curl -X POST http://localhost:8001/admin/reflexion/trigger?dry_run=false")
        print("  • 或在 Langfuse 查看历次推理 Trace 分析质量趋势")
    elif passed >= 5:
        print("  ⚠️  核心流程正常，请检查上方 ❌ 的项目。")
    else:
        print("  ❌ 存在阻断性问题，请先排查 ❌ 项目。")
    print("=" * 56)


if __name__ == "__main__":
    asyncio.run(main())
