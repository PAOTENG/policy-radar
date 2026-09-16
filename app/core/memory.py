"""mem0 跨会话记忆工厂
使用 pgvector 后端，存储在 policy_radar 数据库的 policy_radar_mem0 collection。
"""
from __future__ import annotations
import asyncio
from functools import lru_cache
from typing import Optional

_mem0_client = None


def _build_mem0_config() -> dict:
    from app.config import get_settings
    s = get_settings()
    return {
        "vector_store": {
            "provider": "pgvector",
            "config": {
                "host": s.pg_host,
                "port": s.pg_port,
                "dbname": s.pg_dbname,
                "user": s.pg_user,
                "password": s.pg_password,
                "collection_name": "policy_radar_mem0",
                "embedding_model_dims": 1024,
            },
        },
        "llm": {
            "provider": "openai",
            "config": {
                "model": s.fast_llm_model,
                "api_key": s.llm_api_key,
                # mem0 识别的字段名是 openai_base_url，不是 base_url
                "openai_base_url": s.llm_base_url or "https://api.openai.com/v1",
                "temperature": 0.1,
            },
        },
        "embedder": {
            "provider": "openai",
            "config": {
                "model": s.embedding_model,
                "api_key": s.embed_api_key,
                "openai_base_url": s.embed_base_url,
                # BaseEmbedderConfig 的字段名是 embedding_dims，不是 embedding_model_dims
                "embedding_dims": 1024,
            },
        },
        "version": "v1.1",
    }


def get_mem0():
    """获取 mem0 同步客户端（单例）"""
    global _mem0_client
    if _mem0_client is None:
        try:
            from mem0 import Memory
            config = _build_mem0_config()
            _mem0_client = Memory.from_config(config)
            print("[mem0] 客户端初始化成功（pgvector backend）", flush=True)
        except Exception as e:
            print(f"[mem0] 初始化失败: {e}", flush=True)
            _mem0_client = None
    return _mem0_client


async def mem0_search(query: str, user_id: str, limit: int = 8) -> str:
    """异步包装 mem0 检索，返回格式化字符串"""
    mem0 = get_mem0()
    if not mem0:
        return ""
    try:
        raw = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: mem0.search(query, filters={"user_id": user_id}, limit=limit),
            ),
            timeout=5.0,
        )
        # mem0 v2.x 返回 {"results": [...]}，v1.x 直接返回列表
        if isinstance(raw, dict):
            items = raw.get("results", [])
        else:
            items = raw or []

        facts = []
        for item in items:
            # 兼容 memory / data / text 等不同版本字段名
            content = item.get("memory") or item.get("data") or item.get("text") or ""
            if content:
                facts.append(str(content))
        return "\n".join(facts)
    except asyncio.TimeoutError:
        print("[mem0] 检索超时（5s），返回空记忆", flush=True)
        return ""
    except Exception as e:
        print(f"[mem0] 检索异常: {e}", flush=True)
        return ""


async def mem0_add(messages: list[dict], user_id: str) -> None:
    """异步写入 mem0（后台任务，失败不阻塞）"""
    mem0 = get_mem0()
    if not mem0:
        return
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: mem0.add(messages, user_id=user_id),
            ),
            timeout=10.0,
        )
    except asyncio.TimeoutError:
        print("[mem0] 写入超时", flush=True)
    except Exception as e:
        print(f"[mem0] 写入异常: {e}", flush=True)
