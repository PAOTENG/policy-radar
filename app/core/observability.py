"""Langfuse 可观测性集成
若未配置 LANGFUSE_PUBLIC_KEY，所有操作静默忽略（不影响正常运行）。
"""
from __future__ import annotations
import os
from typing import Any

_handler = None
_enabled = False


def _init():
    global _handler, _enabled
    from app.config import get_settings
    s = get_settings()
    if not s.langfuse_public_key or not s.langfuse_secret_key:
        print("[Langfuse] 未配置 API Key，可观测性已禁用", flush=True)
        return
    try:
        # langfuse 4.x: CallbackHandler 从环境变量读取凭据
        # 必须先把 key 写入 os.environ（pydantic-settings 已加载，但 langfuse 读 os.environ）
        import os
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", s.langfuse_public_key or "")
        os.environ.setdefault("LANGFUSE_SECRET_KEY", s.langfuse_secret_key or "")
        os.environ.setdefault("LANGFUSE_HOST",       s.langfuse_host or "")

        try:
            from langfuse.langchain import CallbackHandler   # langfuse 4.x
        except ImportError:
            from langfuse.callback import CallbackHandler    # langfuse 3.x fallback

        _handler = CallbackHandler()   # 4.x 无需手动传参，自动读 env
        _enabled = True
        print(f"[Langfuse] 已启用，host={s.langfuse_host}", flush=True)
    except Exception as e:
        print(f"[Langfuse] 初始化失败（可继续运行）: {e}", flush=True)


def get_callbacks() -> list[Any]:
    """返回 LangChain/LangGraph callback 列表，供 config={'callbacks': ...} 使用"""
    if _handler is None:
        _init()
    return [_handler] if _enabled and _handler else []


def get_handler():
    """返回 Langfuse handler（可能为 None）"""
    if _handler is None:
        _init()
    return _handler if _enabled else None
