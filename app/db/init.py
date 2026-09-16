"""初始化数据库表结构（幂等，可重复执行）"""
import asyncpg
from app.db.postgres import get_pool


CREATE_PGVECTOR = "CREATE EXTENSION IF NOT EXISTS vector;"

CREATE_CATEGORIES = """
CREATE TABLE IF NOT EXISTS categories (
    id          SERIAL PRIMARY KEY,
    parent_id   INT REFERENCES categories(id) ON DELETE CASCADE,
    level       INT NOT NULL CHECK (level BETWEEN 1 AND 4),
    code        TEXT UNIQUE NOT NULL,
    label       TEXT NOT NULL,
    icon        TEXT,
    sort_order  INT DEFAULT 0,
    policy_count INT DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
"""

CREATE_POLICIES = """
CREATE TABLE IF NOT EXISTS policies (
    id              SERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    source_url      TEXT,
    publisher       TEXT,
    region          TEXT DEFAULT '全国',
    pub_date        DATE,
    deadline        DATE,
    full_text       TEXT,
    summary         TEXT,
    keywords        TEXT[] DEFAULT '{}',
    category_l1     TEXT,
    category_l2     TEXT,
    policy_types    TEXT[],
    key_conditions  TEXT,
    funding_amount  TEXT,
    score_relevance FLOAT DEFAULT 0.5,
    score_urgency   FLOAT DEFAULT 0.5,
    score_value     FLOAT DEFAULT 0.5,
    embedding       VECTOR(1024),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_policies_region     ON policies(region);
CREATE INDEX IF NOT EXISTS idx_policies_category   ON policies(category_l1, category_l2);
CREATE INDEX IF NOT EXISTS idx_policies_deadline   ON policies(deadline);
CREATE INDEX IF NOT EXISTS idx_policies_embedding  ON policies USING hnsw (embedding vector_cosine_ops);
"""

# policy_chunks：全文分块向量表（chunk 级别语义检索，精度远优于整文向量）
CREATE_POLICY_CHUNKS = """
CREATE TABLE IF NOT EXISTS policy_chunks (
    id           BIGSERIAL PRIMARY KEY,
    policy_id    INT NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
    chunk_index  INT NOT NULL,
    chunk_text   TEXT NOT NULL,
    embedding    VECTOR(1024),
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chunks_policy_id  ON policy_chunks(policy_id);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding  ON policy_chunks USING hnsw (embedding vector_cosine_ops);
"""

# 为已有 policies 表补加 keywords 列（幂等，首次运行时添加）
ALTER_POLICIES_KEYWORDS = """
ALTER TABLE policies ADD COLUMN IF NOT EXISTS keywords TEXT[] DEFAULT '{}';
"""

CREATE_COMPETITIONS = """
CREATE TABLE IF NOT EXISTS competitions (
    id          SERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    organizer   TEXT,
    level       TEXT,
    fields      TEXT[],
    deadline    DATE,
    award_desc  TEXT,
    source_url  TEXT,
    full_text   TEXT,
    summary     TEXT,
    score_value FLOAT DEFAULT 0.5,
    embedding   VECTOR(1024),
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_competitions_embedding ON competitions
    USING hnsw (embedding vector_cosine_ops);
"""

CREATE_SCHOOL_ENTERPRISE = """
CREATE TABLE IF NOT EXISTS school_enterprise (
    id          SERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    school      TEXT,
    enterprise_type TEXT,
    collab_type TEXT,
    region      TEXT,
    description TEXT,
    source_url  TEXT,
    pub_date    DATE,
    embedding   VECTOR(1024),
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_se_embedding ON school_enterprise
    USING hnsw (embedding vector_cosine_ops);
"""

CREATE_QUERY_LOGS = """
CREATE TABLE IF NOT EXISTS query_logs (
    id          SERIAL PRIMARY KEY,
    session_id  TEXT,
    keywords    JSONB,
    user_input  TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
"""

CREATE_CRAWL_LOGS = """
CREATE TABLE IF NOT EXISTS crawl_logs (
    id          SERIAL PRIMARY KEY,
    source_name TEXT,
    source_url  TEXT,
    status      TEXT,
    items_found INT DEFAULT 0,
    items_saved INT DEFAULT 0,
    error_msg   TEXT,
    ran_at      TIMESTAMPTZ DEFAULT NOW()
);
"""

CREATE_FAILED_ITEMS = """
CREATE TABLE IF NOT EXISTS failed_crawl_items (
    id           SERIAL PRIMARY KEY,
    source_name  TEXT NOT NULL,
    page_url     TEXT NOT NULL UNIQUE,
    error_msg    TEXT,
    retry_count  INT DEFAULT 0,
    last_tried   TIMESTAMPTZ DEFAULT NOW(),
    resolved     BOOLEAN DEFAULT FALSE,
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_failed_items_resolved ON failed_crawl_items(resolved);
"""

# ── 对话历史（UI 展示用，append-only） ─────────────────────────
CREATE_CHAT_MESSAGES = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id          BIGSERIAL PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL DEFAULT 'anonymous',
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content     TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chat_messages_thread ON chat_messages(thread_id, created_at);
"""

# ── 用户反馈（Reflexion 数据源） ──────────────────────────────
CREATE_FEEDBACK = """
CREATE TABLE IF NOT EXISTS feedback (
    id           BIGSERIAL PRIMARY KEY,
    session_id   TEXT NOT NULL,
    thread_id    TEXT,
    signal_type  TEXT NOT NULL,   -- 'explicit_rating' | 'policy_click' | 'pdf_export' | 'session_length'
    score        FLOAT,           -- 归一化到 0~1
    metadata     JSONB DEFAULT '{}',
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_feedback_session ON feedback(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_type ON feedback(signal_type, created_at);
"""

# ── DSPy Reflexion 补丁记录 ────────────────────────────────────
CREATE_REFLEXION_PATCHES = """
CREATE TABLE IF NOT EXISTS reflexion_patches (
    id             BIGSERIAL PRIMARY KEY,
    status         TEXT NOT NULL DEFAULT 'proposed',  -- proposed | evaluating | promoted | rejected
    patch_type     TEXT NOT NULL,                     -- 'prompt' | 'retrieval_strategy'
    target         TEXT NOT NULL,                     -- 哪个 prompt/参数
    diff           TEXT NOT NULL,                     -- 变更内容
    baseline_scores JSONB DEFAULT '{}',               -- 优化前 eval 分数
    new_scores     JSONB DEFAULT '{}',                -- 优化后 eval 分数
    promoted_at    TIMESTAMPTZ,
    rejected_reason TEXT,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_reflexion_status ON reflexion_patches(status, created_at);
"""

# ── 政策文档级关系图（属性图边表，供图谱 1 跳补召回）────────────
# 节点 = policies 行；边 = 文档间关系（非实体级碎图）
# relation_type 取值见 app/rag/policy_graph.py::RELATION_TYPES
CREATE_POLICY_RELATIONS = """
CREATE TABLE IF NOT EXISTS policy_relations (
    id                BIGSERIAL PRIMARY KEY,
    source_policy_id  INT NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
    target_policy_id  INT NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
    relation_type     TEXT NOT NULL,
    confidence        FLOAT DEFAULT 0.5,
    evidence          TEXT,
    build_source      TEXT DEFAULT 'rule',
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (source_policy_id, target_policy_id, relation_type),
    CHECK (source_policy_id <> target_policy_id)
);
CREATE INDEX IF NOT EXISTS idx_policy_rel_source ON policy_relations(source_policy_id);
CREATE INDEX IF NOT EXISTS idx_policy_rel_target ON policy_relations(target_policy_id);
CREATE INDEX IF NOT EXISTS idx_policy_rel_type   ON policy_relations(relation_type);
"""


async def init_db():
    """建库建表（幂等，可重复执行），并作为 PolicyRadar 全系统 Agent 流程的总览入口。

    ═══════════════════════════════════════════════════════════════════════════
    PolicyRadar 完整 Agent 流程（本项目独立体系，与业务前端通过 HTTP/SSE 对接）
    ═══════════════════════════════════════════════════════════════════════════

    【一、系统启动链】（见 app/main.py lifespan）
      1. init_db()          ← 本函数：建表 + 扩展 pgvector
      2. ensure_indices()   ← Elasticsearch 政策索引
      3. get_graph()        ← 预热对话 Agent（LangGraph + 可选 Checkpointer）
      4. get_mem0()         ← 跨会话长期记忆
      5. get_callbacks()    ← Langfuse 可观测性（可缺省）
      6. 调度器启动         ← 爬虫定时任务 + Reflexion 离线优化

    【二、数据供给链】（爬虫 → 知识库，供两个 Agent 检索）
      Playwright 采集政务/高校页面
        → trafilatura 抽正文 + 规则抽元数据 + Fast LLM 摘要/分类
        → policies（全文+文档向量）+ policy_chunks（512字块向量）
        → Elasticsearch BM25 全文索引
      表：policies / policy_chunks / policy_relations / crawl_logs / failed_crawl_items

    【三、报告 Agent】（app/agents/report/，入口 stream_report）
      API: POST /api/v1/report/generate（SSE）
      LangGraph 节点链：
        retrieve          → 混合 RAG（pgvector + ES + RRF + Rerank，约12条）
        expand_related    → 政策关系图谱 1 跳补召回（省–市/同项目等）
        grade_policies    → Self-RAG 评分过滤噪声 + 提炼核心事实
        verify_policies   → 时效 + 五类关联对冲突门控（生成前注入）
        format_context    → 拼装带标注的 System/Human messages
        [图外] LLM.astream → 流式生成 Markdown 报告
        [图外] citation_verify → 事后校验《政策名》是否在 context 中有据
      落库：query_logs（keywords → 流结束后 UPDATE report_text + verification_meta）
      反馈：用户星级 → feedback 表 → Reflexion（DSPy）离线优化 Prompt

    【四、对话 Agent】（app/agents/chat/，入口 chat_stream）
      API: /ai/chat（SSE）
      LangGraph 节点链：
        START ─┬─ memory_node  ─→ 摘要压缩 + mem0 检索
               └─ rag_node     ─→ Adaptive RAG（闲聊可跳过）
                    ↓ 汇合
               context_processor_node → Worker LLM 提炼背景事实（防污染）
                    ↓
               chat_node → 主 LLM（可 bind 工具）
                    ↓ tool_calls?
               tools_node → generate_report / search_web → 回到 chat_node
                    ↓ 无工具
               END；异步写入 mem0
      会话：LangGraph Checkpointer（Postgres；Windows Proactor 下可降级无状态）
      历史：chat_messages 表（UI 展示）

    【五、两 Agent 关系】
      - 报告 Agent：单次「选词 → 检索验证 → 出报告」流水线，偏决策交付
      - 对话 Agent：多轮问答；需要完整报告时通过工具调用报告 Agent
      - 共用：同一套 RAG 知识库、同一套 LLM/Embedding 配置、同一反馈与观测基础设施

    【六、本函数具体做什么】
      - 创建/补齐业务与 Agent 依赖表（幂等 IF NOT EXISTS）
      - 启用 vector 扩展；为 query_logs 补 report_text / verification_meta 列
      - 不启动 Agent，只保证 Agent 读写所需 schema 就绪

    Returns:
        None（副作用：DDL 执行；日志打印初始化完成）
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(CREATE_PGVECTOR)
        # 单语句表，直接执行
        for stmt in [
            CREATE_CATEGORIES,
            CREATE_COMPETITIONS,
            CREATE_SCHOOL_ENTERPRISE,
            CREATE_QUERY_LOGS,
            CREATE_CRAWL_LOGS,
        ]:
            await conn.execute(stmt)
        # 含多条语句的 DDL，必须拆开执行（asyncpg 不允许多语句 prepare）
        for multi_stmt in [
            CREATE_POLICIES,
            CREATE_POLICY_CHUNKS,
            CREATE_FAILED_ITEMS,
            CREATE_CHAT_MESSAGES,
            CREATE_FEEDBACK,
            CREATE_REFLEXION_PATCHES,
            CREATE_POLICY_RELATIONS,
        ]:
            for stmt in multi_stmt.strip().split(";"):
                stmt = stmt.strip()
                if stmt:
                    await conn.execute(stmt)
        # 为旧表补加新列（幂等）
        await conn.execute(ALTER_POLICIES_KEYWORDS)
        await conn.execute(
            "ALTER TABLE query_logs ADD COLUMN IF NOT EXISTS report_text TEXT;"
        )
        await conn.execute(
            "ALTER TABLE query_logs ADD COLUMN IF NOT EXISTS verification_meta JSONB DEFAULT '{}';"
        )
    print("[DB] 数据库初始化完成", flush=True)
