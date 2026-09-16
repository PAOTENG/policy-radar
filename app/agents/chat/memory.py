"""
对话 Agent 三层记忆实现（app/agents/chat/memory.py）

═══════════════════════════════════════════════════════════════════
本文件函数清单（共 4 个）
═══════════════════════════════════════════════════════════════════
  1. _get_worker_llm   Fast LLM（摘要/提炼用，temperature=0）
  2. sanitize_window   修复滑动窗口中的 ToolMessage 孤儿问题
  3. maybe_compress    Layer2：超阈值时 LLM 摘要压缩旧消息
  4. distill_context   Worker：三路上下文 → 一句 background_fact

分层说明：
  Layer1 滑动窗口：由 graph + checkpointer 保留近期 messages；本文件负责清洗
  Layer2 摘要压缩：maybe_compress
  Layer3 mem0：读写在 graph.py / core.memory，本文件只消费检索结果做提炼
"""
import json
import re
import asyncio
from langchain_core.messages import (
    BaseMessage, HumanMessage, AIMessage, SystemMessage,
    RemoveMessage, ToolMessage,
)
from langchain_openai import ChatOpenAI
from app.config import get_settings
from app.agents.chat.prompts import SUMMARY_SYSTEM


def _get_worker_llm() -> ChatOpenAI:
    """构造 Worker 用 Fast LLM（摘要与上下文提炼）。

    流程：读 Settings.fast_llm_model → ChatOpenAI(temperature=0, max_tokens=400)

    参数：无

    返回：
      ChatOpenAI

    作用：
      与主对话模型隔离，控制成本与输出长度。
    """
    s = get_settings()
    return ChatOpenAI(
        model=s.fast_llm_model,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        temperature=0,
        max_tokens=400,
    )


def sanitize_window(messages: list[BaseMessage]) -> list[BaseMessage]:
    """清洗消息窗口，保证 tool 调用序列完整。

    流程：
      1. 去掉开头非 HumanMessage（窗口截断残留）
      2. 对带 tool_calls 的 AIMessage：对应 ToolMessage 必须齐全，否则整组丢弃
      3. 返回可安全送入 LLM 的消息列表

    参数：
      messages: 原始消息列表（可能含孤儿 ToolMessage）

    返回：
      list[BaseMessage]：清洗后的列表

    作用：
      避免「AIMessage.tool_calls 无 ToolMessage」导致上游 API BadRequest。
    """
    if not messages:
        return messages

    while messages and not isinstance(messages[0], HumanMessage):
        messages = messages[1:]

    tool_msg_ids = {
        m.tool_call_id for m in messages if isinstance(m, ToolMessage)
    }

    result: list[BaseMessage] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if isinstance(msg, AIMessage) and msg.tool_calls:
            expected_ids = {tc["id"] for tc in msg.tool_calls}
            if expected_ids.issubset(tool_msg_ids):
                result.append(msg)
            else:
                i += 1
                while i < len(messages) and isinstance(messages[i], ToolMessage):
                    i += 1
                continue
        else:
            result.append(msg)
        i += 1

    return result


async def maybe_compress(
    messages: list[BaseMessage],
    summary: str,
) -> tuple[list[BaseMessage], str, list[RemoveMessage]]:
    """消息数超阈值时，用 LLM 压缩旧消息为摘要。

    流程：
      1. len(messages) ≤ max_messages_before_summary → 原样返回
      2. 从尾部保留 keep_recent_messages，切点对齐到 HumanMessage
      3. 将前半段文本 + 已有 summary 交给 Worker LLM 生成新摘要
      4. 为被压缩消息生成 RemoveMessage（按 id）
      5. 失败则降级：不压缩

    参数：
      messages: 当前会话消息
      summary:  已有摘要（可为空串）

    返回：
      (保留消息列表, 新摘要, RemoveMessage 列表)
      注意：调用方 memory_node 主要用 new_summary 与 remove_msgs；
      保留列表在 checkpointer 场景下由 RemoveMessage 生效。

    作用：
      Layer2 记忆：控制上下文长度，保留关键事实到 summary。
    """
    s = get_settings()
    if len(messages) <= s.max_messages_before_summary:
        return messages, summary, []

    keep = s.keep_recent_messages
    cut_idx = len(messages) - keep
    while cut_idx > 0 and not isinstance(messages[-cut_idx], HumanMessage):
        cut_idx -= 1

    if cut_idx <= 0:
        return messages, summary, []

    to_compress = messages[:-cut_idx] if cut_idx else messages
    to_keep = messages[-cut_idx:] if cut_idx else []

    history_text = "\n".join(
        f"{m.__class__.__name__}: {m.content[:200]}"
        for m in to_compress
        if isinstance(m.content, str)
    )
    existing = f"（已有摘要：{summary}）\n\n" if summary else ""

    llm = _get_worker_llm()
    try:
        resp = await asyncio.wait_for(
            llm.ainvoke([
                SystemMessage(SUMMARY_SYSTEM),
                HumanMessage(f"{existing}新增对话：\n{history_text}"),
            ]),
            timeout=15.0,
        )
        new_summary = resp.content.strip()
    except Exception as e:
        print(f"[Memory] 摘要压缩失败: {e}", flush=True)
        return messages, summary, []

    remove_msgs = [RemoveMessage(id=m.id) for m in to_compress if m.id]
    print(
        f"[Memory] 摘要压缩：{len(to_compress)} 条 → summary，保留 {len(to_keep)} 条",
        flush=True,
    )
    return to_keep, new_summary, remove_msgs


async def distill_context(
    summary: str,
    mem0_context: str,
    rag_context: str,
) -> str:
    """将摘要 + mem0 + RAG 三路信息提炼为一句背景事实。

    流程：
      1. 三者皆空 → ""
      2. 拼 raw_context（RAG 截断至 800 字）
      3. Worker LLM + CONTEXT_PROCESSOR_SYSTEM → 期望 JSON {background_fact}
      4. 去 markdown 代码块后 json.loads
      5. 失败降级：截断拼接 summary+mem0

    参数：
      summary:      Layer2 摘要
      mem0_context: Layer3 检索文本
      rag_context:  政策检索文本

    返回：
      str：background_fact 或降级短文本

    作用：
      供 chat_node 注入 System Prompt，实现 Worker/主模型上下文隔离。
    """
    if not any([summary, mem0_context, rag_context]):
        return ""

    raw_context = "\n\n".join(filter(None, [
        f"[历史摘要] {summary}" if summary else "",
        f"[用户记忆] {mem0_context}" if mem0_context else "",
        f"[检索到的政策] {rag_context[:800]}" if rag_context else "",
    ]))

    from app.agents.chat.prompts import CONTEXT_PROCESSOR_SYSTEM
    llm = _get_worker_llm()

    try:
        resp = await asyncio.wait_for(
            llm.ainvoke([
                SystemMessage(CONTEXT_PROCESSOR_SYSTEM),
                HumanMessage(raw_context),
            ]),
            timeout=10.0,
        )
        raw = resp.content.strip()
        raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
        data = json.loads(raw)
        return data.get("background_fact", "")
    except Exception as e:
        print(f"[Memory] 上下文提炼失败: {e}", flush=True)
        return f"{summary} {mem0_context}"[:500]
