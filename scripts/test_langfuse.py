"""
Langfuse 可观测性连通性测试
运行方式：conda run -n agent python scripts/test_langfuse.py
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_config():
    print("\n━━━ [1/4] 检查 .env 配置 ━━━")
    from app.config import get_settings
    s = get_settings()
    ok = True
    for field in ["langfuse_public_key", "langfuse_secret_key", "langfuse_host"]:
        val = getattr(s, field)
        if val:
            print(f"  ✅ {field} = {val[:12]}...")
        else:
            print(f"  ❌ {field} 未配置！")
            ok = False
    return ok


def test_init():
    print("\n━━━ [2/4] 初始化 Langfuse CallbackHandler ━━━")
    from app.core.observability import get_callbacks, get_handler
    callbacks = get_callbacks()
    handler = get_handler()
    if callbacks and handler:
        print(f"  ✅ CallbackHandler 初始化成功")
        print(f"  ✅ host = {handler.langfuse.base_url if hasattr(handler, 'langfuse') else 'ok'}")
        return True
    else:
        print("  ❌ CallbackHandler 初始化失败（callbacks 为空）")
        return False


def test_trace():
    print("\n━━━ [3/4] 发送一条测试 Trace ━━━")
    from app.core.observability import get_handler
    handler = get_handler()
    if not handler:
        print("  ⏭  跳过（handler 未初始化）")
        return False
    try:
        lf = handler.langfuse
        trace = lf.trace(
            name="test-policy-radar",
            input={"test": True},
            output={"status": "ok"},
            metadata={"source": "test_langfuse.py"},
        )
        lf.flush()
        print(f"  ✅ Trace 已发送: trace_id={trace.id}")
        print(f"  🌐 在 Langfuse 控制台查看: https://cloud.langfuse.com")
        return True
    except Exception as e:
        print(f"  ❌ 发送 Trace 失败: {e}")
        return False


def test_llm_with_callback():
    print("\n━━━ [4/4] 真实 LLM 调用（带 Langfuse Callback）━━━")
    from app.core.observability import get_callbacks
    callbacks = get_callbacks()
    if not callbacks:
        print("  ⏭  跳过（callbacks 为空）")
        return False
    try:
        from app.config import get_settings
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage
        s = get_settings()
        llm = ChatOpenAI(
            model=s.fast_llm_model,
            api_key=s.llm_api_key,
            base_url=s.llm_base_url,
            temperature=0,
            max_tokens=30,
        )
        t0 = time.perf_counter()
        resp = llm.invoke(
            [HumanMessage("用一句话回答：1+1等于多少？")],
            config={"callbacks": callbacks, "run_name": "test-langfuse-llm"},
        )
        elapsed = int((time.perf_counter() - t0) * 1000)
        print(f"  ✅ LLM 回复: {resp.content.strip()} ({elapsed}ms)")
        print(f"  ✅ Trace 已上报 Langfuse，请在控制台查看 'test-langfuse-llm'")
        return True
    except Exception as e:
        print(f"  ❌ LLM 调用失败: {e}")
        return False


if __name__ == "__main__":
    print("=" * 52)
    print("  PolicyRadar · Langfuse 可观测性 测试")
    print("=" * 52)

    results = [
        test_config(),
        test_init(),
        test_trace(),
        test_llm_with_callback(),
    ]

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 52}")
    print(f"  结果: {passed}/{total} 通过")
    if passed == total:
        print("  🎉 Langfuse 完全正常！去控制台查看 Traces。")
    elif passed >= 2:
        print("  ⚠️  部分功能正常，请检查上方 ❌ 的项目。")
    else:
        print("  ❌ Langfuse 未能正常工作，请检查 .env 配置。")
    print("=" * 52)
