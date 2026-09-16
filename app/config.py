from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── 应用 ──────────────────────────────────────
    app_name: str = "PolicyRadar API"
    app_port: int = 8001
    debug: bool = False

    # ── LLM（主模型） ────────────────────────────
    llm_model: str = "deepseek-chat"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.deepseek.com/v1"

    # 轻量 Worker LLM（路由/摘要/提炼用，与主模型隔离）
    fast_llm_model: str = "deepseek-chat"

    # ── Embedding ────────────────────────────────
    embedding_model: str = "text-embedding-v3"
    embed_api_key: str = ""
    embed_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    # ── PostgreSQL（独立数据库，与 ShillGuard 隔离）──
    pg_host: str = "localhost"
    pg_port: int = 5432
    pg_dbname: str = "policy_radar"
    pg_user: str = "postgres"
    pg_password: str = ""
    # psycopg3 格式连接串（LangGraph checkpointer 用）
    pg_dsn: str = ""

    # ── Elasticsearch ─────────────────────────────
    es_host: str = "http://localhost:9200"
    es_policy_index: str = "policy_radar_policies"
    es_competition_index: str = "policy_radar_competitions"
    es_school_index: str = "policy_radar_school"

    # ── Redis ─────────────────────────────────────
    redis_url: str = "redis://localhost:6379/1"

    # ── SiliconFlow（Reranker API） ───────────────
    siliconflow_api_key: str = ""
    siliconflow_rerank_url: str = "https://api.siliconflow.cn/v1/rerank"
    siliconflow_rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # ── Langfuse（可观测性） ──────────────────────
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # ── Tavily（联网搜索工具） ────────────────────
    tavily_api_key: str = ""

    # ── 记忆系统参数 ───────────────────────────────
    max_messages_before_summary: int = 8   # 超过此数触发摘要压缩
    keep_recent_messages: int = 6          # 压缩后保留最近 N 条原文
    mem0_search_limit: int = 8             # mem0 每次检索条数上限

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"   # 忽略 .env 中未定义的多余字段

    def get_pg_dsn(self) -> str:
        """自动构造 psycopg3 格式连接串"""
        if self.pg_dsn:
            return self.pg_dsn
        return (
            f"postgresql://{self.pg_user}:{self.pg_password}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_dbname}"
        )


@lru_cache()
def get_settings() -> Settings:
    return Settings()
