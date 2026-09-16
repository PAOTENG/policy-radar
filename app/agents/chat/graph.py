"""
对话 Agent — LangGraph 编排入口（app/agents/chat/graph.py）

═══════════════════════════════════════════════════════════════════
本文件函数/对象清单（共 12 项）
═══════════════════════════════════════════════════════════════════
  1. _get_main_llm          主对话 LLM（bind 工具、可流式）
  2. memory_node            节点：摘要压缩 + mem0 检索
  3. rag_node               节点：Adaptive RAG
  4. context_processor_node 节点：Worker LLM 提炼背景事实
  5. chat_node              节点：主 LLM 回复（可触发工具）
  6. tools_node             预构建 ToolNode（执行工具调用）
  7. route_after_chat       条件边：有 tool_calls → tools，否则 END
  8. build_graph            注册节点/边并 compile
  9. _is_proactor_loop      检测 Windows ProactorEventLoop
 10. _get_pg_pool           Checkpointer 专用 psycopg3 连接池
 11. get_graph              单例图（含 Checkpointer 或降级）
 12. chat_stream            对外流式入口
 13. _write_mem0            对话结束后异步写长期记忆

节点拓扑：
  START ─┬─ memory ─┐
         └─ rag ────┴─→ context_processor → chat ⇄ tools → END

完整系统流程见 app/db/init.py 中 init_db() 文档字符串。
"""
from __future__ import annotations

import asyncio
import time
from typing import Annotated, Literal

from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage,
)
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row
from typing_extensions import TypedDict

from app.agents.chat.memory import distill_context, maybe_compress, sanitize_window
from app.agents.chat.prompts import CHAT_SYSTEM
from app.agents.chat.tools import TOOLS
from app.config import get_settings
from app.core.memory import mem0_add, mem0_search
from app.core.observability import get_callbacks
from app.rag.retriever import retrieve_with_routing


# ── State ─────────────────────────────────────────────────────────────────────
class ChatState(TypedDict):
    """对话 Agent 图状态。

    messages:          消息列表（add_messages reducer）
    summary:           Layer2 历史摘要
    mem0_context:      Layer3 跨会话记忆检索结果
    rag_context:       Adaptive RAG 文本
    processed_context: Worker 提炼后的背景事实（主模型只看这个）
    user_id:           mem0 用户维度
    """
    messages: Annotated[list[BaseMessage], add_messages]
    summary: str
    mem0_context: str
    rag_context: str
    processed_context: str
    user_id: str


# ── LLM ───────────────────────────────────────────────────────────────────────
def _get_main_llm() -> ChatOpenAI:
    """构造主对话模型，并绑定 TOOLS。

    流程：读 Settings → ChatOpenAI(temperature=0.7, streaming=True) → bind_tools(TOOLS)

    参数：无

    返回：
      已绑定工具的 ChatOpenAI

    作用：
      chat_node 统一用此模型，支持 function/tool calling。
    """
    s = get_settings()
    return ChatOpenAI(
        model=s.llm_model,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        temperature=0.7,
        streaming=True,
    ).bind_tools(TOOLS)


# ── 节点 1 & 2（并行）: memory_node ──────────────────────────────────────────
async def memory_node(state: ChatState) -> dict:
    """记忆节点：清洗窗口 → 必要时摘要压缩 → 检索 mem0。

    流程：
      1. sanitize_window(messages) 去掉孤儿 ToolMessage
      2. maybe_compress → 可能产生 RemoveMessage + 新 summary
      3. 取最后一条 HumanMessage 文本做 mem0_search(user_id)
      4. 返回 mem0_context、summary，以及可选的 messages 删除指令

    参数：
      state: ChatState（messages / summary / user_id）

    返回：
      dict：至少含 mem0_context、summary；可能含 messages=[RemoveMessage...]

    作用：
      提供 Layer1/2/3 记忆侧输入，与 rag_node 并行执行。
    """
    t0 = time.perf_counter()
    messages = state["messages"]
    summary = state.get("summary", "")
    user_id = state.get("user_id", "anonymous")

    messages = sanitize_window(messages)
    kept_msgs, new_summary, remove_msgs = await maybe_compress(messages, summary)

    last_human = next(
        (m.content for m in reversed(messages) if isinstance(m, HumanMessage)),
        "",
    )
    mem0_ctx = await mem0_search(last_human, user_id) if last_human else ""

    elapsed = int((time.perf_counter() - t0) * 1000)
    print(f"[TIMER] memory_node={elapsed}ms (mem0={'有' if mem0_ctx else '无'})", flush=True)

    result: dict = {
        "mem0_context": mem0_ctx,
        "summary": new_summary,
    }
    if remove_msgs:
        result["messages"] = remove_msgs
    return result


# ── 节点 1 & 2（并行）: rag_node ─────────────────────────────────────────────
async def rag_node(state: ChatState) -> dict:
    """检索节点：Adaptive RAG 路由 + 混合检索。

    流程：
      1. 取最后一条用户消息；无则返回空 rag_context
      2. retrieve_with_routing(query=用户消息, top_n=10)
         （内部：should_retrieve 判断闲聊是否跳过；否则混合 RAG）
      3. 返回 rag_context 字符串

    参数：
      state: ChatState

    返回：
      {"rag_context": str}

    作用：
      为对话提供政策依据；闲聊时跳过以降低延迟。
    """
    t0 = time.perf_counter()
    messages = state["messages"]

    last_human = next(
        (m.content for m in reversed(messages) if isinstance(m, HumanMessage)),
        "",
    )
    if not last_human:
        return {"rag_context": ""}

    rag_ctx, _ = await retrieve_with_routing(
        query=last_human,
        keywords={},
        user_input=last_human,
        top_n=10,
    )
    elapsed = int((time.perf_counter() - t0) * 1000)
    print(f"[TIMER] rag_node={elapsed}ms ({'有结果' if rag_ctx else '跳过'})", flush=True)
    return {"rag_context": rag_ctx}


# ── 节点 3: context_processor_node ───────────────────────────────────────────
async def context_processor_node(state: ChatState) -> dict:
    """上下文提炼节点：Worker LLM 合并三路上下文为背景事实。

    流程：
      1. 读取 summary / mem0_context / rag_context
      2. distill_context(...) → processed_context
      3. 打 TIMER 日志

    参数：
      state: ChatState（三路上下文字段）

    返回：
      {"processed_context": str}

    作用：
      隔离原始长上下文，避免主模型被噪声/回声污染。
    """
    t0 = time.perf_counter()
    processed = await distill_context(
        summary=state.get("summary", ""),
        mem0_context=state.get("mem0_context", ""),
        rag_context=state.get("rag_context", ""),
    )
    elapsed = int((time.perf_counter() - t0) * 1000)
    print(f"[TIMER] context_processor_node={elapsed}ms", flush=True)
    return {"processed_context": processed}


# ── 节点 4: chat_node ─────────────────────────────────────────────────────────
async def chat_node(state: ChatState) -> dict:
    """主对话节点：只消费 processed_context + 对话 messages。

    流程：
      1. 将 processed_context 注入 CHAT_SYSTEM 的 {background}
      2. SystemMessage + 历史 messages → 主 LLM ainvoke
      3. 返回新的 AIMessage（可能含 tool_calls）

    参数：
      state: ChatState

    返回：
      {"messages": [AIMessage]}

    作用：
      产出对用户的回复或工具调用请求。
    """
    t0 = time.perf_counter()
    processed = state.get("processed_context", "")
    messages = state["messages"]

    background = f"\n\n## 用户背景（已提炼）\n{processed}" if processed else ""
    system_content = CHAT_SYSTEM.format(background=background)

    full_messages = [SystemMessage(content=system_content)] + messages

    llm = _get_main_llm()
    callbacks = get_callbacks()
    config = {"callbacks": callbacks} if callbacks else {}

    response = await llm.ainvoke(full_messages, config=config)
    elapsed = int((time.perf_counter() - t0) * 1000)
    print(f"[TIMER] chat_node={elapsed}ms", flush=True)
    return {"messages": [response]}


# ── 节点 5: tools_node ────────────────────────────────────────────────────────
# LangGraph 预构建节点：执行 AIMessage.tool_calls，返回 ToolMessage 列表
tools_node = ToolNode(TOOLS)


# ── 路由函数 ──────────────────────────────────────────────────────────────────
def route_after_chat(state: ChatState) -> Literal["tools", "__end__"]:
    """chat 节点之后的条件路由。

    流程：
      看最后一条消息：若是带 tool_calls 的 AIMessage → "tools"，否则 END

    参数：
      state: ChatState

    返回：
      "tools" 或 END（"__end__"）

    作用：
      实现 chat ⇄ tools 循环，直到模型不再调用工具。
    """
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return END


# ── 图构建 ─────────────────────────────────────────────────────────────────────
def build_graph(checkpointer=None):
    """构建对话 Agent 图并 compile。

    流程：
      1. 注册 memory/rag/context_processor/chat/tools
      2. START 并行连 memory 与 rag
      3. 二者汇入 context_processor → chat
      4. chat 条件边 → tools 或 END；tools 回到 chat
      5. compile(checkpointer=...)

    参数：
      checkpointer: AsyncPostgresSaver 或 None（无状态）

    返回：
      CompiledGraph

    作用：
      定义多轮对话拓扑；checkpointer 负责 thread_id 级状态持久化。
    """
    g = StateGraph(ChatState)

    g.add_node("memory", memory_node)
    g.add_node("rag", rag_node)
    g.add_node("context_processor", context_processor_node)
    g.add_node("chat", chat_node)
    g.add_node("tools", tools_node)

    g.add_edge(START, "memory")
    g.add_edge(START, "rag")
    g.add_edge("memory", "context_processor")
    g.add_edge("rag", "context_processor")
    g.add_edge("context_processor", "chat")
    g.add_conditional_edges("chat", route_after_chat)
    g.add_edge("tools", "chat")

    return g.compile(checkpointer=checkpointer)


# ── Checkpointer 单例 ─────────────────────────────────────────────────────────
_pg_pool: AsyncConnectionPool | None = None
_checkpointer = None
_graph = None


def _is_proactor_loop() -> bool:
    """判断当前是否运行在 Windows ProactorEventLoop 上。

    流程：
      非 win32 → False；取 running/current loop，isinstance ProactorEventLoop

    参数：无

    返回：
      bool

    作用：
      psycopg3 异步池不兼容 Proactor；为 True 时跳过 Checkpointer，避免刷屏报错。
    """
    import sys, asyncio
    if sys.platform != "win32":
        return False
    try:
        loop = asyncio.get_event_loop()
        return isinstance(loop, asyncio.ProactorEventLoop)
    except Exception:
        return False


async def _get_pg_pool() -> AsyncConnectionPool:
    """懒创建 Checkpointer 专用的 psycopg3 AsyncConnectionPool。

    流程：
      1. 已有池则复用
      2. 用 Settings 拼 conninfo，max_size=10，autocommit=True
      3. await pool.open()

    参数：无

    返回：
      AsyncConnectionPool

    作用：
      与业务侧 asyncpg 池隔离，专供 LangGraph AsyncPostgresSaver。
    """
    global _pg_pool
    if _pg_pool is None:
        s = get_settings()
        conninfo = (
            f"host={s.pg_host} port={s.pg_port} "
            f"dbname={s.pg_dbname} user={s.pg_user} "
            f"password={s.pg_password}"
        )
        _pg_pool = AsyncConnectionPool(
            conninfo=conninfo,
            max_size=10,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
            open=False,
        )
        await _pg_pool.open()
    return _pg_pool


async def get_graph():
    """获取对话图单例（首次初始化 Checkpointer 或降级无状态）。

    流程：
      1. 已有 _graph → 直接返回
      2. Proactor → build_graph(None)
      3. 否则尝试 AsyncPostgresSaver.setup()；失败则 checkpointer=None
      4. build_graph 并缓存

    参数：无

    返回：
      CompiledGraph

    作用：
      应用启动 lifespan 与每次 chat_stream 共用同一图实例。
    """
    global _checkpointer, _graph
    if _graph is not None:
        return _graph

    if _is_proactor_loop():
        print("[LangGraph] Windows ProactorEventLoop 检测到，跳过 checkpointer（无状态模式）", flush=True)
        _graph = build_graph(checkpointer=None)
        return _graph

    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    try:
        pool = await _get_pg_pool()
        _checkpointer = AsyncPostgresSaver(pool)
        await _checkpointer.setup()
        print("[LangGraph] PostgreSQL checkpointer 初始化成功", flush=True)
    except Exception as e:
        print(f"[LangGraph] Checkpointer 初始化失败，使用无状态模式: {e}", flush=True)
        _checkpointer = None

    _graph = build_graph(checkpointer=_checkpointer)
    return _graph


# ── 对话入口 ──────────────────────────────────────────────────────────────────
async def chat_stream(
    message: str,
    thread_id: str,
    user_id: str = "anonymous",
):
    """对话 Agent 对外流式入口，产出 (event_type, content)。

    流程：
      1. await get_graph()
      2. 以 HumanMessage 为输入，astream_events(v2)
      3. 仅转发 langgraph_node=="chat" 的 on_chat_model_stream token
      4. 正常结束：异步 _write_mem0，yield ("done","")
      5. 异常：yield ("error", 错误信息)

    参数：
      message:   用户本轮输入
      thread_id: 会话线程 ID（Checkpointer 键）
      user_id:   mem0 用户 ID，默认 anonymous

    产出：
      ("delta", token) | ("done", "") | ("error", msg)

    作用：
      供 /ai/chat SSE API 使用；过滤 Worker/tools 的流，只给用户看主回复。
    """
    graph = await get_graph()
    config = {
        "configurable": {"thread_id": thread_id},
        "callbacks": get_callbacks(),
    }
    input_state = {
        "messages": [HumanMessage(content=message)],
        "summary": "",
        "mem0_context": "",
        "rag_context": "",
        "processed_context": "",
        "user_id": user_id,
    }

    try:
        async for event in graph.astream_events(input_state, config=config, version="v2"):
            kind = event.get("event")
            if kind == "on_chat_model_stream":
                meta = event.get("metadata", {})
                if meta.get("langgraph_node") == "chat":
                    chunk = event["data"]["chunk"]
                    if hasattr(chunk, "content") and chunk.content:
                        yield "delta", chunk.content

        asyncio.create_task(_write_mem0(thread_id, user_id, message))
        yield "done", ""

    except Exception as e:
        yield "error", str(e)


async def _write_mem0(thread_id: str, user_id: str, user_message: str):
    """对话轮次结束后，将用户消息异步写入 mem0。

    流程：
      组装 [{"role":"user","content":...}] → mem0_add(messages, user_id)

    参数：
      thread_id: 会话 ID（预留扩展，当前未写入 mem0 metadata）
      user_id:   用户维度
      user_message: 本轮用户原文

    返回：
      None（副作用：更新长期记忆库）

    作用：
      跨会话偏好/事实积累，供后续 memory_node 检索。
    """
    messages = [{"role": "user", "content": user_message}]
    await mem0_add(messages, user_id)
