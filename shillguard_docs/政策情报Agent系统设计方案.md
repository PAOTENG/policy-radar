# 政策情报 Agent 系统设计方案

> 项目定位：面向中小企业和高校科研团队的政策情报智能平台
> 差异化：聚焦科研竞赛 + 校企联动 + 政策补贴三合一，现有产品的空白地带
> 技术栈：Python（FastAPI + LangGraph + Playwright）+ Vue 3 前端 + PostgreSQL + Elasticsearch

---

## 一、系统整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         前端（Vue 3）                            │
│  ┌────────────┐  ┌──────────────────┐  ┌────────────────────┐  │
│  │ 分类导航页  │  │ 关键词漏斗选择器  │  │  对话 Agent + 报告  │  │
│  │（多级菜单）  │  │（面包屑 + 标签）  │  │  （SSE 流式输出）  │  │
│  └─────┬──────┘  └────────┬─────────┘  └────────┬───────────┘  │
└────────┼──────────────────┼─────────────────────┼──────────────┘
         │                  │                      │
         ▼ HTTP              ▼ HTTP                 ▼ SSE
┌─────────────────────────────────────────────────────────────────┐
│                    后端（Python FastAPI）                        │
│  ┌────────────┐  ┌──────────────────┐  ┌────────────────────┐  │
│  │ 分类接口   │  │  政策检索接口     │  │  Agent 对话接口    │  │
│  │ /api/cats  │  │ /api/policies     │  │  /ai/report (SSE)  │  │
│  └─────┬──────┘  └────────┬─────────┘  └────────┬───────────┘  │
│        │                  │                      │               │
│        ▼                  ▼                      ▼               │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                   LangGraph Agent 层                      │   │
│  │  [分类树构建] → [RAG 检索] → [打分过滤] → [报告生成]     │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              │                                   │
│           ┌──────────────────┼──────────────────┐               │
│           ▼                  ▼                  ▼               │
│     PostgreSQL         Elasticsearch        ChromaDB            │
│    （结构化存储）       （全文+BM25）        （向量检索）         │
└─────────────────────────────────────────────────────────────────┘
         ▲
         │ 定时任务（APScheduler）
┌────────┴────────────────────────────────────────────────────────┐
│                    数据采集 Pipeline                              │
│  Playwright 爬虫 → LLM 结构化提取 → 打分清洗 → 入库索引          │
│  数据源：政府官网 / 部委网站 / 高校官网 / 科协 / 工信部           │
└─────────────────────────────────────────────────────────────────┘
```

---

## 二、前端交互流程（用户视角）

### 第一层：首页导航入口

```
┌──────────────────────────────────────────────────────────┐
│                   政策情报平台 首页                        │
│                                                          │
│   ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│   │ 政策查询  │  │ 科研竞赛  │  │ 校企联动  │  ← 三大主模块  │
│   └────┬─────┘  └──────────┘  └──────────┘             │
│        │ 点击进入                                         │
└────────┼─────────────────────────────────────────────────┘
         ▼
```

### 第二层：分类筛选页（以"政策查询"为例）

```
┌──────────────────────────────────────────────────────────────────┐
│  政策查询                                          [AI 报告生成]  │
│  ─────────────────────────────────────────────────────────────  │
│                                                                  │
│  当前已选：[无]                                                  │
│                                                                  │
│  ① 选择行业方向（L1）                                            │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐       │
│  │ 科技  │ │ 金融  │ │ 医疗  │ │ 建筑  │ │ 教育  │ │ 人才  │       │
│  └──────┘ └──────┘ └──────┘ └──────┘ └──────┘ └──────┘       │
│                                                                  │
│                    用户点击 [科技]                                │
│                         ▼                                        │
│  ② 细化方向（L2）                                                │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐          │
│  │ 人工智能  │ │  芯片半导  │ │  新能源   │ │  生物科技  │          │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘          │
│                                                                  │
│                    用户点击 [人工智能]                            │
│                         ▼                                        │
│  ③ 政策类型（L3）                                                │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐ ┌──────────┐            │
│  │ 补贴  │ │ 申报  │ │ 税收  │ │ 园区  │ │ 高新认定  │            │
│  └──────┘ └──────┘ └──────┘ └──────┘ └──────────┘            │
│                                                                  │
│  ④ 地区范围（L4，可选）                                           │
│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐                         │
│  │ 全国  │ │ 广东  │ │ 北京  │ │ 上海  │  ...                    │
│  └──────┘ └──────┘ └──────┘ └──────┘                         │
│                                                                  │
│  ─────────────────────────────────────────────────────────────  │
│  已选关键词：[科技] [人工智能] [申报] [广东]                      │
│                                                                  │
│  ⑤ 补充说明（对话输入框）                                         │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ 我司是一家深圳的AI算法公司，注册资本500万，成立3年，      │   │
│  │ 目前有软件著作权5项，想了解近期可申报的项目...            │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                  │
│                    ┌──────────────────────┐                     │
│                    │   生成政策分析报告    │                     │
│                    └──────────────────────┘                     │
└──────────────────────────────────────────────────────────────────┘
```

### 第三层：AI 报告输出页（SSE 流式）

```
┌──────────────────────────────────────────────────────────────────┐
│  📊 政策分析报告                                    ⬇ 导出 PDF   │
│  ─────────────────────────────────────────────────────────────  │
│  查询条件：科技 · 人工智能 · 申报 · 广东                          │
│  生成时间：2026-07-13                                            │
│                                                                  │
│  ## 一、当前匹配政策总览（共找到 12 条）                          │
│  | 政策名称          | 发布单位   | 截止日期   | 匹配度  |        │
│  |-------------------|-----------|-----------|--------|        │
│  | 广东省AI先锋城市  | 广东工信厅  | 2026-09-30 | ★★★★★ |        │
│  | 深圳人工智能扶持  | 深圳科创委  | 2026-08-15 | ★★★★☆ |        │
│  ...（流式输出中）                                               │
│                                                                  │
│  ## 二、重点推荐政策详解                                          │
│  ### 1. 广东省打造人工智能先锋城市项目...                         │
│  **申报条件**：注册于广东省内，上年度营收...                      │
│  **补贴金额**：最高 500 万元                                     │
│  **您的符合情况**：✅ 注册地 ✅ 成立年限 ⚠️ 营收待确认           │
│                                                                  │
│  ## 三、近期申报时间表                                            │
│  ## 四、建议行动清单                                             │
│  ...（继续流式输出）                                             │
└──────────────────────────────────────────────────────────────────┘
```

---

## 三、后端数据库设计

### 3.1 主要数据表

```sql
-- 政策原始表
CREATE TABLE policies (
    id          SERIAL PRIMARY KEY,
    title       TEXT NOT NULL,           -- 政策标题
    source_url  TEXT,                    -- 原始链接
    publisher   TEXT,                    -- 发布单位（工信厅/科协等）
    region      TEXT,                    -- 地区（全国/广东/北京...）
    pub_date    DATE,                    -- 发布日期
    deadline    DATE,                    -- 申报截止日期
    full_text   TEXT,                    -- 全文内容
    category_l1 TEXT,                    -- 行业L1（科技/金融/医疗...）
    category_l2 TEXT,                    -- 细化L2（人工智能/芯片...）
    policy_type TEXT[],                  -- 政策类型数组（补贴/申报/税收...）
    score_relevance FLOAT,               -- 相关性评分 0-1
    score_urgency   FLOAT,               -- 紧迫性评分（距截止日期）
    score_value     FLOAT,               -- 价值评分（补贴金额/影响力）
    created_at  TIMESTAMP DEFAULT NOW(),
    embedding   VECTOR(1024)             -- pgvector 向量（用于语义检索）
);

-- 分类树表（驱动前端多级菜单）
CREATE TABLE categories (
    id          SERIAL PRIMARY KEY,
    parent_id   INT REFERENCES categories(id),
    level       INT NOT NULL,            -- 1/2/3/4
    code        TEXT UNIQUE NOT NULL,    -- 唯一标识符
    label       TEXT NOT NULL,           -- 显示名称（科技/人工智能...）
    icon        TEXT,                    -- 前端图标
    policy_count INT DEFAULT 0          -- 该分类下政策数量（定时更新）
);

-- 科研竞赛表
CREATE TABLE competitions (
    id          SERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    organizer   TEXT,                    -- 主办方
    level       TEXT,                    -- 国家级/省级/校级
    field       TEXT[],                  -- 领域标签
    deadline    DATE,
    award_desc  TEXT,                    -- 奖励说明
    source_url  TEXT,
    full_text   TEXT,
    score_value FLOAT,
    embedding   VECTOR(1024)
);

-- 校企联动表
CREATE TABLE school_enterprise (
    id          SERIAL PRIMARY KEY,
    title       TEXT NOT NULL,
    school      TEXT,                    -- 高校名称
    enterprise  TEXT,                    -- 企业方向
    type        TEXT,                    -- 产教融合/联合培养/技术攻关
    region      TEXT,
    description TEXT,
    source_url  TEXT,
    pub_date    DATE,
    embedding   VECTOR(1024)
);

-- 用户查询记录（用于分析热点）
CREATE TABLE query_logs (
    id          SERIAL PRIMARY KEY,
    session_id  TEXT,
    keywords    JSONB,                   -- 用户选择的关键词 {l1,l2,l3,region}
    user_input  TEXT,                    -- 用户自定义输入
    created_at  TIMESTAMP DEFAULT NOW()
);
```

### 3.2 分类树数据（L1-L4 示例）

```
L1（行业）
├── 科技
│   ├── L2: 人工智能
│   │   └── L3: 补贴申报 / 研发税收 / 高新认定 / 重点实验室
│   ├── L2: 芯片半导体
│   ├── L2: 新能源
│   └── L2: 生物科技
├── 金融
├── 医疗
├── 建筑
├── 教育
└── 人才

L4（地区）
全国 / 北京 / 上海 / 广东 / 深圳 / 浙江 / 江苏 / 成都 ...
```

---

## 四、LangGraph Agent 设计

```python
# 报告生成 Agent 的状态图

class ReportState(TypedDict):
    keywords: dict          # {l1, l2, l3, region, policy_type}
    user_input: str         # 用户补充描述
    retrieved_policies: list  # RAG检索到的政策
    scored_policies: list     # 打分排序后的政策
    report: str             # 最终生成的报告

# 节点设计
START
  │
  ▼
【1】retrieve_node           # 根据关键词从 ES + pgvector 混合检索
  │   - Elasticsearch BM25：用 l1+l2+l3 关键词过滤
  │   - pgvector 语义搜索：用 user_input embedding 相似检索
  │   - 合并去重（RRF融合）
  ▼
【2】score_filter_node       # LLM 打分 + 规则过滤
  │   - 相关性重排（是否匹配用户描述的企业类型）
  │   - 时效过滤（已过截止日期的排除/降权）
  │   - 保留 Top 10~15 条
  ▼
【3】report_gen_node         # LLM 生成格式化报告（流式）
      - 输入：用户关键词 + 企业描述 + Top政策列表
      - 输出：结构化 Markdown 报告（5个固定章节）
      - 流式 SSE 推送到前端
  END
```

### 报告生成 Prompt 模板

```python
REPORT_SYSTEM_PROMPT = """
你是一位专业的政策咨询顾问，擅长为企业解读政府政策并生成决策报告。
用户已通过导航选择了以下维度：{keywords}
企业补充信息：{user_input}

请根据以下检索到的政策原文，生成一份专业的政策分析报告。
报告格式严格按照以下结构：

## 一、当前匹配政策总览
[Markdown 表格：政策名称 | 发布单位 | 截止日期 | 补贴金额 | 匹配度]

## 二、重点推荐政策（TOP 3 详解）
[对每条政策：申报条件 / 补贴金额 / 与企业的符合情况分析 / 注意事项]

## 三、近期申报时间表
[按截止日期排序的时间轴]

## 四、行动建议
[具体可执行的 3~5 条建议]

## 五、信息来源
[政策原文链接和发布单位]

注意：
- 只引用检索到的政策原文，不要编造政策
- 符合情况分析要结合企业补充信息判断
- 时间紧迫的政策要重点标注
"""
```

---

## 五、数据采集 Pipeline

```python
# 采集任务调度（APScheduler）

# 每天凌晨 2:00 全量更新
@scheduler.scheduled_job('cron', hour=2)
async def daily_crawl():
    sources = [
        {"name": "国家政策库", "url": "https://www.gov.cn/zhengce/", "type": "policy"},
        {"name": "工信部",     "url": "https://www.miit.gov.cn/",    "type": "policy"},
        {"name": "科技部",     "url": "https://www.most.gov.cn/",    "type": "policy"},
        {"name": "广东省工信厅", "url": "...",                        "type": "policy"},
        # 科研竞赛源
        {"name": "中国科协",   "url": "https://www.cast.org.cn/",   "type": "competition"},
        {"name": "CCF竞赛",    "url": "https://www.ccf.org.cn/",    "type": "competition"},
        # 校企联动源
        {"name": "教育部产教融合", "url": "...",                     "type": "school_enterprise"},
    ]
    for source in sources:
        await crawl_and_process(source)

# 每条原文的处理流程
async def crawl_and_process(source):
    # 1. 爬取原文（Playwright 处理 JS 渲染页面）
    raw_text = await playwright_fetch(source["url"])

    # 2. LLM 结构化提取
    structured = await llm_extract(raw_text)
    # 提取字段: title, publisher, region, deadline, category_l1, category_l2,
    #          policy_type, key_conditions, funding_amount

    # 3. 评分
    scores = await llm_score(structured)
    # score_relevance: 政策质量和可申报性
    # score_urgency: 1 - days_to_deadline/365（越近越高）
    # score_value: 补贴金额 / 影响力评估

    # 4. 向量化 + 入库
    embedding = await get_embedding(structured["full_text"])
    await save_to_postgres(structured, scores, embedding)
    await save_to_elasticsearch(structured)  # 全文索引
```

---

## 六、前端页面路由设计（Vue 3）

```
/policy-agent                         # 平台首页（三大模块入口）
/policy-agent/query                   # 政策查询（分类导航 + 漏斗选择）
/policy-agent/competition             # 科研竞赛（分类导航 + 漏斗选择）
/policy-agent/school-enterprise       # 校企联动（分类导航）
/policy-agent/report                  # AI 报告页（SSE 流式输出）
/policy-agent/history                 # 历史查询记录
```

**组件结构**：
```
src/
├── views/admin/
│   ├── PolicyAgentView.vue           # 首页（三大入口）
│   ├── PolicyQueryView.vue           # 政策查询分类筛选页
│   ├── CompetitionView.vue           # 科研竞赛页
│   ├── SchoolEnterpriseView.vue      # 校企联动页
│   └── PolicyReportView.vue          # 报告输出页（SSE）
├── components/
│   ├── CategoryFunnel.vue            # 多级关键词漏斗选择器（核心复用组件）
│   ├── KeywordBreadcrumb.vue         # 已选关键词面包屑
│   ├── PolicyCard.vue                # 政策卡片展示
│   └── ReportStream.vue             # SSE 流式报告渲染
└── api/
    └── policyAgent.ts                # API 封装（/ai/policy/*）
```

---

## 七、API 接口设计

```
GET  /ai/policy/categories           # 获取完整分类树（驱动前端多级菜单）
GET  /ai/policy/list?l1=科技&l2=AI    # 按关键词分页查询政策列表
GET  /ai/competition/list             # 科研竞赛列表
POST /ai/policy/report  (SSE)         # 生成 AI 分析报告（流式）
     Body: {keywords: {l1,l2,l3,region}, user_input: "..."}

GET  /ai/policy/stats                 # 统计面板（政策数量、热点分类等）
GET  /ai/policy/trending              # 近7天新增热点政策
```

---

## 八、技术栈全景分析

### 8.1 Java 后端是否需要？

**结论：不需要，本项目全程用 Python 完成后端，原因如下：**

| 维度 | Java 后端 | Python 后端（本项目选择） |
|---|---|---|
| 和 AI/LLM 的生态契合度 | 差（Spring AI 很新、生态不成熟） | **极好**（LangGraph/LangChain 原生 Python） |
| 爬虫 / 数据处理 | 繁琐，需第三方 | **Playwright、BeautifulSoup、Pandas 全部原生** |
| 异步支持 | Spring WebFlux（复杂） | **asyncio 原生，FastAPI 天然异步** |
| SSE 流式输出 | 需要额外配置 | **FastAPI StreamingResponse 原生支持** |
| 定时任务调度 | Spring Scheduler | **APScheduler 轻量内嵌，无需额外服务** |
| 维护两套后端 | 前端 + Java + Python = 3套，复杂度 ×3 | **前端 + Python = 2套，简洁** |
| 简历加分 | 不加分（重复技能）| **Python 全栈 AI 工程，契合 Agent 岗 JD** |

**什么时候才需要 Java**：如果你的企业内已有 Java 微服务（ERP/CRM/OA），需要调用其内部接口，才有必要加 Java 服务作为胶水层。本项目是**全新独立系统**，不需要。

---

### 8.2 完整技术栈清单

#### 前端（Vue 3 生态）

| 技术 | 版本建议 | 用途 |
|---|---|---|
| **Vue 3** | ^3.4 | 核心框架 |
| **Vite** | ^5 | 构建工具（已有） |
| **Vue Router 4** | ^4.3 | 页面路由（已有）|
| **Pinia** | ^2 | 状态管理（已有）|
| **Element Plus** | ^2.7 | UI 组件库（已有）|
| **@element-plus/icons-vue** | ^2 | 图标（已有）|
| **marked.js** | ^14 | Markdown 渲染（已安装）|
| **highlight.js** | ^11 | 代码高亮（已安装）|
| **axios** | ^1.6 | HTTP 请求（已有）|

新增依赖（本项目特有）：
```
无需新增，直接复用 shill-guard-frontend 已有的全部依赖
```

#### 后端（Python 生态）

| 技术 | 版本建议 | 用途 |
|---|---|---|
| **Python** | 3.11+ | 主语言 |
| **FastAPI** | ^0.115 | Web 框架 + API + SSE |
| **uvicorn** | ^0.30 | ASGI 服务器 |
| **LangGraph** | ^0.2 | Agent 状态机编排 |
| **LangChain** | ^0.3 | LLM 工具链 |
| **langchain-openai** | ^0.2 | 对接 DeepSeek/Qwen/OpenAI |
| **APScheduler** | ^3.10 | 定时爬取任务调度 |
| **Playwright** | ^1.45 | 动态页面爬取（JS渲染站点）|
| **httpx** | ^0.27 | 异步 HTTP 客户端（静态页面爬取）|
| **BeautifulSoup4** | ^4.12 | HTML 解析 |
| **asyncpg** | ^0.29 | PostgreSQL 异步驱动 |
| **psycopg\[binary\]** | ^3.1 | LangGraph checkpoint 用 |
| **pgvector** | ^0.3 | PostgreSQL 向量扩展 Python 客户端 |
| **elasticsearch** | ^8.13 | Elasticsearch 官方 Python SDK |
| **pydantic** | ^2.7 | 数据模型验证（结构化提取）|
| **pydantic-settings** | ^2 | 配置管理 |
| **redis** | ^5 | 缓存（热点分类、API限流）|
| **python-dotenv** | ^1 | 环境变量 |
| **loguru** | ^0.7 | 结构化日志 |

#### 数据存储层

| 存储 | 用途 | 说明 |
|---|---|---|
| **PostgreSQL 16+** | 主数据库（政策/竞赛/校企结构化数据）| 复用已有实例 |
| **pgvector 扩展** | 向量语义检索（embedding 存储） | 复用已有配置 |
| **Elasticsearch 8** | 全文检索 + BM25 关键词过滤 | 复用已有实例 |
| **Redis** | 热点分类缓存、爬取任务状态、API 限流 | 轻量使用 |

> ChromaDB 不再需要：pgvector 已能完全替代，避免引入额外依赖。

#### AI / LLM 服务

| 服务 | 用途 |
|---|---|
| **DeepSeek V3 / Qwen-Plus** | 结构化提取、报告生成（成本低、中文强）|
| **DeepSeek-R1 / Qwen-Turbo** | 打分评估节点（推理能力要求高）|
| **text-embedding-v3**（阿里云）| 文本向量化（1024维，复用已有配置）|

#### 基础设施 / DevOps

| 技术 | 用途 |
|---|---|
| **Docker + docker-compose** | 本地开发环境一键启动（PG + ES + Redis）|
| **Git** | 版本管理 |

---

### 8.3 技术选型对比总结（Why Not XXX）

| 被排除的技术 | 排除原因 |
|---|---|
| Java / Spring Boot | 无微服务集成需求，多一套服务只增加维护成本 |
| ChromaDB | pgvector 已满足向量需求，避免多余依赖 |
| Celery + RabbitMQ | 爬取任务体量不大，APScheduler 内嵌即可；Celery 需额外 Broker |
| GraphQL | REST + SSE 完全满足需求，引入 GraphQL 无收益 |
| Kubernetes | 学习项目，docker-compose 足够；K8s 过重 |
| LlamaIndex | 本项目 RAG 逻辑简单，用 LangChain 原语自实现更灵活 |

---

### 8.4 开发优先级（建议顺序）

```
Phase 1（核心骨架，1~2 周）
├── FastAPI 项目骨架 + 数据库建表
├── 手动导入 10~20 条政策数据（先跑通流程）
├── LangGraph 报告生成 Agent（3节点）
└── 前端：分类导航页 + 关键词漏斗 + SSE 报告页

Phase 2（数据层，1 周）
├── Playwright 爬虫（先跑通 2~3 个重点站点）
├── LLM 结构化提取 Pipeline
├── APScheduler 定时任务
└── 打分清洗逻辑

Phase 3（完善，1 周）
├── Elasticsearch 全文检索集成
├── 混合检索（pgvector + ES RRF 融合）
├── 科研竞赛 + 校企联动模块
└── 统计面板 / 历史记录

Phase 4（上线 & 简历素材）
├── Docker 部署
├── GitHub 完善 README + 架构图
└── 录制 Demo 视频
```

---

## 九、简历项目描述素材

**项目名**：政策情报 Agent 平台（PolicyRadar）

**项目描述**：
> 面向中小企业和高校科研团队的政策情报智能平台，聚焦政策补贴、科研竞赛、校企联动三大场景。通过多源政务数据自动采集 + LLM 结构化提取打分，结合多级分类漏斗选择器 + 对话 Agent，为企业生成格式化政策分析报告，实现从"企业找政策"到"政策匹配企业"的模式转变。

**技术亮点（简历 bullet 方向）**：
- 设计 Playwright + LLM 双层采集 Pipeline，实现 xxx 个政府站点的每日自动采集 + 结构化提取（标题、截止日期、申报条件、补贴金额）
- 构建三维评分体系（相关性 / 紧迫性 / 价值评分），通过 LLM 自动打分 + 规则过滤，将原始数据的有效信息密度从 ~30% 提升至 ~85%
- 设计 LangGraph 三节点报告生成 Agent（检索 → 重排 → 报告），基于用户选择的 L1~L4 关键词 + 自然语言描述，生成 5 章节结构化 Markdown 报告，支持 SSE 流式输出
- 设计多级分类漏斗 UI（L1 行业 → L2 细化方向 → L3 政策类型 → L4 地区），关键词自动累积传入 Agent 上下文，提升 RAG 检索精准度约 xx%
- 混合检索：向量语义检索（pgvector）+ Elasticsearch BM25 关键词过滤 + RRF 融合排序，保证"描述不精准"时仍有高质量召回
