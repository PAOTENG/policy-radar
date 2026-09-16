"""Adaptive RAG 路由：判断当前用户消息是否需要检索政策知识库
- 闲聊/常识/通用问题 → 跳过 RAG
- 政策/竞赛/申报/补贴相关 → 执行 RAG
- 超时 5s 保守降级为需要检索
"""
import re
import asyncio
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

_ROUTER_SYSTEM = """你是政策情报系统的查询路由器。

判断用户消息是否需要检索政策知识库才能回答。

需要检索（输出 yes）的情况：
- 询问具体政策、补贴、申报条件、截止日期
- 询问科研竞赛、校企合作项目
- 需要了解某行业/地区的政策支持情况
- 要求生成政策分析报告

不需要检索（输出 no）的情况：
- 打招呼、闲聊、感谢
- 询问你是谁、你能做什么
- 通用常识问题（天气、时间等）
- 重复确认已有信息

只输出 yes 或 no，不要解释。"""


async def should_retrieve(query: str) -> bool:
    """判断是否需要 RAG 检索，超时 5s 降级为 True（保守策略）"""
    from app.config import get_settings
    s = get_settings()
    llm = ChatOpenAI(
        model=s.fast_llm_model,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        temperature=0,
        max_tokens=10,
    )
    try:
        resp = await asyncio.wait_for(
            llm.ainvoke([SystemMessage(_ROUTER_SYSTEM), HumanMessage(query)]),
            timeout=5.0,
        )
        result = bool(re.search(r"\byes\b", resp.content, re.IGNORECASE))
        print(f"[RAGRouter] {'需要' if result else '跳过'} RAG: {query[:40]}", flush=True)
        return result
    except asyncio.TimeoutError:
        print("[RAGRouter] 超时，保守降级为需要 RAG", flush=True)
        return True
    except Exception as e:
        print(f"[RAGRouter] 异常，降级为需要 RAG: {e}", flush=True)
        return True
