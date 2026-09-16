# PolicyRadar · 政策情报 Agent 系统

> 企业政策信息采集 → AI 打分清洗 → 智能报告生成

## 目录结构

```
policy-radar/
├── app/
│   ├── main.py                # FastAPI 入口（端口 8001）
│   ├── config.py              # 配置（与 ShillGuard 完全隔离）
│   ├── db/
│   │   ├── init.py            # 建表（pgvector 扩展、5 张表）
│   │   ├── postgres.py        # asyncpg 连接池
│   │   └── schemas.py         # Pydantic 数据模型
│   ├── agents/
│   │   └── report/
│   │       ├── graph.py       # LangGraph 报告 Agent（retrieve→format→stream）
│   │       └── prompts.py     # 系统提示词
│   ├── crawler/
│   │   ├── base.py            # 爬虫基类（Playwright + LLM 提取）
│   │   ├── sources.py         # 具体爬虫（工信部/科技部/财政部）
│   │   └── scheduler.py       # APScheduler 定时任务（每天 03:00）
│   ├── rag/
│   │   ├── es_client.py       # Elasticsearch BM25 检索
│   │   └── retriever.py       # 混合检索（pgvector + BM25 + RRF）
│   └── api/
│       ├── categories.py      # 分类树接口
│       ├── policies.py        # 政策列表/详情接口
│       ├── report.py          # 报告生成（SSE 流式）
│       └── crawl.py           # 手动触发采集
├── scripts/
│   └── seed_data.py           # 初始化分类树 + 示例政策数据
├── requirements.txt
├── .env.example
└── README.md
```

## 快速启动

### 1. 创建 PostgreSQL 数据库

```sql
CREATE DATABASE policy_radar;
-- 进入 policy_radar 库
\c policy_radar
CREATE EXTENSION IF NOT EXISTS vector;
```

### 2. 安装依赖

```
pip install -r requirements.txt
playwright install chromium
```

### 3. 配置环境变量

```
copy .env.example .env
```

编辑 `.env`，填入 LLM API Key、PostgreSQL 密码等。

### 4. 初始化数据库 + 种子数据

```
python scripts/seed_data.py
```

### 5. 启动服务

```
python -m app.main
```

服务运行在 http://localhost:8001，Swagger 文档：http://localhost:8001/docs

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | /api/v1/categories | 获取完整分类树（4 级） |
| GET  | /api/v1/policies | 政策列表（支持 region/l1/l2 过滤） |
| GET  | /api/v1/policies/{id} | 政策详情 |
| POST | /api/v1/report/generate | 流式生成报告（SSE） |
| POST | /api/v1/crawl/trigger | 手动触发采集任务 |
| GET  | /health | 健康检查 |

## 与 ShillGuard 的隔离关系

| 资源 | ShillGuard | PolicyRadar | 隔离方式 |
|------|-----------|-------------|---------|
| 服务端口 | 8000 | 8001 | 不同端口 |
| PostgreSQL 数据库 | shillguard | policy_radar | 不同 DB |
| Elasticsearch 索引 | shillguard_kg | policy_radar_* | 不同前缀 |
| Redis 数据库 | db=0 | db=1 | 不同 DB 编号 |
| Python 包环境 | agent venv | 可复用 agent venv | 共用 |

## 前端访问

在 `shill-guard-frontend` 项目中，admin 侧边栏"政策情报"分组下点击"政策情报 Agent"。

Vite 代理：`/policy-api/*` → `http://localhost:8001/*`
