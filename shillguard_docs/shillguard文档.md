# ShillGuard · UGC 内容治理 AI 助手 — 完整技术文档

> 本文档记录 ShillGuard 项目截止 2026-07-13 的全部技术实现，供后续项目参考对比。

---

## 一、项目概述

### 1.1 业务背景

ShillGuard 是一个面向 UGC（用户生成内容）平台的内容治理 AI 系统，核心功能：

| 功能模块 | 说明 |
|---------|------|
| **内容审核** | 对帖子/评论进行多级别违规判定（clear_normal / gray / clear_violation） |
| **恶意用户检测** | 对用户近期发言进行异常评分（0~1，≥0.9 为高危） |
| **禁言证据生成** | 为高危用户自动生成结构化禁言证据报告 |
| **智能对话 Agent** | 支持多轮对话、长期记忆、RAG 知识库检索、工具调用 |
| **法规检索** | 通过 MCP 工具检索平台规则与国家法律法规原文 |

### 1.2 项目地址

```
后端：shill-guard-ai/        （Python FastAPI + LangGraph）
前端：shill-guard-frontend/  （Vue 3 + Element Plus）
```

---

## 二、完整技术栈

### 2.1 后端

| 类别 | 技术 | 用途 |
|------|------|------|
| Web 框架 | FastAPI | HTTP 接口、SSE 流式输出 |
| Agent 框架 | LangGraph | 状态机、节点编排、工具调用 |
| LLM | DeepSeek-v4-flash | 主对话模型（thinking 模型） |
| Worker LLM | DeepSeek-v4-flash | 上下文提炼专用（隔离污染） |
| Embedding | text-embedding-v3（阿里百炼） | 向量化，1024 维 |
| 向量库 | ChromaDB（RAG） + pgvector（mem0） | 双向量后端 |
| 全文检索 | Elasticsearch（BM25） | 关键词检索 |
| Reranker | BAAI/bge-reranker-v2-m3（硅基流动 API） | 重排序 |
| 长期记忆 | mem0 | 跨会话用户记忆 |
| 数据库 | PostgreSQL（asyncpg）| LangGraph checkpointer + UI 历史 + mem0 |
| 任务调度 | APScheduler | 定时任务（预留） |
| 搜索工具 | Tavily | Agent 联网搜索工具 |
| 内容安全 | 阿里云内容安全（comment_detection_pro） | 商业 T2 审核 API |
| 可观测 | LangSmith | Agent 链路追踪（可关闭） |
| 安全防护 | agent-memory-guard（AMG，OWASP ASI06） | 记忆写入前校验 |

### 2.2 前端

| 技术 | 用途 |
|------|------|
| Vue 3 + Vite | 前端框架 |
| Element Plus | UI 组件库 |
| Pinia | 状态管理 |
| Vue Router | 路由管理 |
| marked.js | Markdown → HTML 渲染 |
| highlight.js | 代码块语法高亮 |
| SSE（EventSource/fetch） | 接收 Agent 流式输出 |

### 2.3 Docker 服务

所有 Docker 服务运行在 `localhost`（本地 Docker 环境）：

| 服务 | 端口 | 用途 | 连接信息 |
|------|------|------|---------|
| PostgreSQL | 5432 | LangGraph checkpointer / chat历史 / mem0 向量 | user=postgres, pwd=your_pg_password, db=shillguard |
| Elasticsearch | 9200 | RAG BM25 全文检索 | 无认证，索引=shillguard_kg |
| Redis | 6379 | 预留（当前未用） | pwd=your_pg_password |
| Java 微服务 | 9000 | shill-content/shill-ai Java 后端 | http://localhost:9000 |

```yaml
# 启动命令（在 Docker 宿主机上）
docker compose up -d postgres elasticsearch redis
```

---

## 三、数据存储全景

### 3.1 PostgreSQL（shillguard 库）

| 表名 | 存储内容 | 写入时机 |
|------|---------|---------|
| `checkpoints` | LangGraph 状态（消息+摘要+RAG上下文） | 每次 Agent 运行后自动写入 |
| `checkpoint_blobs` | 大对象 blob（msgpack 序列化） | 同上 |
| `checkpoint_migrations` | 版本迁移记录 | LangGraph 初始化时 |
| `chat_messages` | UI 展示用对话历史（append-only） | 每次用户发消息/Agent 回复后 |
| `mem0_vectors` | mem0 长期记忆向量（pgvector，1024维） | 每次对话结束后异步写入 |

### 3.2 ChromaDB（本地文件，仅用于 RAG）

```
./data/chroma/          → RAG 知识库向量（平台规则 + 法律法规文档）
./data/mem0_chroma/     → 已废弃（mem0 已迁移至 pgvector），保留旧数据引用
```

### 3.3 Elasticsearch

```
shillguard_kg           → RAG BM25 索引，存储 chunk text + metadata
```

### 3.4 双轨存储说明

```
用户问 → router.py → 1. 写 chat_messages（UI历史，永久）
                     2. 交给 LangGraph → checkpointer（LLM工作记忆，可被压缩）

用户查历史 → 只读 chat_messages 表（不读 checkpointer）
LangGraph 恢复上下文 → 只读 checkpointer（不读 chat_messages）
```

---

## 四、LangGraph Agent 图结构

### 4.1 节点拓扑（5 节点，memory/rag 并行）

```
START
  ├── memory_node ──────┐
  └── rag_node   ───────┴──→ context_processor_node → chat_node
                                                          │
                                              有 tool_calls？
                                                  ↓YES     ↓NO
                                             tools_node    END
                                                  │
                                              chat_node（循环）
```

### 4.2 各节点职责

| 节点 | 职责 | 输出 State 字段 |
|------|------|---------------|
| `memory_node` | 检索 mem0 记忆 + 执行 LLM 摘要压缩 | `mem0_context`, `summary` |
| `rag_node` | Adaptive RAG：路由判断 + 混合检索 + 重排序 | `rag_context` |
| `context_processor_node` | Worker LLM 提炼上下文为单句 fact | `processed_context` |
| `chat_node` | 主 LLM 对话（只看 processed_context） | `messages`（append） |
| `tools_node` | 执行工具调用（Tavily搜索等） | `messages`（append） |

### 4.3 ChatState 数据结构

```python
class ChatState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]  # LangGraph 管理
    summary: str           # LLM 压缩摘要（旧消息摘要）
    mem0_context: str      # mem0 检索到的跨会话记忆
    rag_context: str       # RAG 检索到的知识库片段
    processed_context: str # Worker LLM 提炼后的背景事实（传给 Main LLM）
```

---

## 五、三层记忆系统

### 5.1 架构图

```
Layer 1: 滑动窗口（最近 6 条原文消息）
    ↓ 超过阈值（8条）触发
Layer 2: LLM 摘要压缩（旧消息 → summary 字段）
    ↓ 每次对话
Layer 3: mem0 跨会话记忆（pgvector）
```

### 5.2 配置参数（app/config.py）

```python
max_messages_before_summary: int = 8    # 超过此数触发摘要压缩
keep_recent_messages: int = 6           # 压缩后保留最近 N 条原文
mem0_search_limit: int = 8             # 每次从 mem0 检索的条数上限
```

### 5.3 摘要压缩关键实现

```python
# 摘要切点对齐到 HumanMessage 边界（避免切断 tool_calls 序列）
cut_idx = max_messages_before_summary
while cut_idx > 0 and not isinstance(messages[-(cut_idx)], HumanMessage):
    cut_idx -= 1

# 使用 RemoveMessage 删除已压缩的旧消息
remove_msgs = [RemoveMessage(id=m.id) for m in to_compress]
```

### 5.4 mem0 配置（pgvector 后端）

```python
{
    "vector_store": {
        "provider": "pgvector",
        "config": {
            "host": "localhost", "port": 5432,
            "dbname": "shillguard",
            "collection_name": "shillguard_mem0",
            "embedding_model_dims": 1024,  # text-embedding-v3 实测值
        }
    }
}
```

### 5.5 mem0 API 注意事项

```python
# 正确写法（新版 API）
mem0.search(query, filters={"user_id": user_id})  # 用 filters 参数
# 错误写法（旧版，已弃用）
mem0.search(query, user_id=user_id)               # 报 TopLevelEntity 错误

# 结果提取
for m in results:
    content = m.get("data") or m.get("memory") or ""  # 字段名因版本不同
```

---

## 六、RAG 系统（混合检索）

### 6.1 Adaptive RAG（查询路由）

```python
# 先用 Worker LLM 判断是否需要检索
async def _should_retrieve(query: str) -> bool:
    """yes/no 输出，超时5s 保守降级为 True"""
    resp = await llm.ainvoke([SystemMessage(_RAG_ROUTER_SYSTEM), HumanMessage(query)])
    return bool(re.search(r'\byes\b', resp.content, re.IGNORECASE))
```

无关查询（闲聊/日期/常识等）直接返回空 `rag_context`，跳过全流程，节省 2~4s。

### 6.2 混合检索流程

```
用户查询
   ↓
路由判断（Worker LLM，5s 超时）
   ↓ YES
   ├── 向量检索（ChromaDB，8s 超时）
   └── BM25 检索（Elasticsearch）
         ↓ 并行执行
       RRF 融合排序（k=60）
         ↓
       Reranker 重排（硅基流动 API，5s 超时）
         ↓
       过滤 score < 0.3 的 chunk
         ↓
     rag_context（传入 context_processor）
```

### 6.3 Reranker 配置

```python
# 硅基流动 API（BAAI/bge-reranker-v2-m3）
SILICONFLOW_RERANK_URL = "https://api.siliconflow.cn/v1/rerank"
# 超时 5s，失败不降级到本地模型（避免 40s+ 本地 torch 冷启动）
```

---

## 七、Context Engineering（上下文工程）

### 7.1 四大策略对照

| 策略 | 实现方式 |
|------|---------|
| **Write** | mem0 异步写入（添加/更新/删除记忆） |
| **Select** | RAG 相关性过滤（reranker score ≥ 0.3）+ Adaptive RAG 路由 |
| **Compress** | LLM 摘要压缩旧消息 + Worker LLM 提炼上下文 |
| **Isolate** | Worker LLM 隔离层，Main LLM 只看 processed_context |

### 7.2 Worker LLM 隔离（核心防污染机制）

```
原始上下文（mem0 + summary + RAG）
         ↓
  context_processor_node（Worker LLM）
         ↓ 输出结构化 JSON
  processed_context = "用户叫张三，30岁，从事AI开发"
         ↓
  chat_node（Main LLM）只看这一句话
```

**为什么这样做**：防止 Main LLM 把背景信息"复读"给用户（"根据您提供的信息，您叫张三..."）。

### 7.3 结构化输出（prompt-based JSON，兼容 thinking 模型）

```python
# thinking 模型不支持 function_calling / json_mode
# 解决方案：在 prompt 里内嵌 JSON Schema，手动解析
WORKER_PROMPT = """
...输出严格 JSON（不要 markdown）：
{"background_fact": "一句话背景"}
"""
# 手动解析
raw = resp.content
raw = re.sub(r'```json\s*|\s*```', '', raw).strip()
data = json.loads(raw)
```

### 7.4 _sanitize_window（消息窗口修复）

解决 LangGraph 滑动窗口切割产生孤儿 ToolMessage 的问题：

```python
def _sanitize_window(messages):
    """
    原子处理 AIMessage(tool_calls) + 对应的所有 ToolMessage：
    1. 删除开头的非 HumanMessage 消息
    2. AIMessage(tool_calls) 必须和全部对应的 ToolMessage 一起保留或一起删除
    3. 尾部不完整的 AIMessage(tool_calls) 整组删除
    """
```

---

## 八、性能优化

### 8.1 并行执行（最重要的优化）

```python
# memory_node 与 rag_node 完全独立，并行执行
g.add_edge(START, "memory")   # 并行开始
g.add_edge(START, "rag")      # 并行开始
g.add_edge("memory", "context_processor")
g.add_edge("rag", "context_processor")
# 节省约 max(mem0延迟, RAG延迟) - min(两者) ≈ 600ms+
```

### 8.2 超时控制

| 组件 | 超时设置 | 超时后行为 |
|------|---------|----------|
| mem0.search | 5s | 返回空记忆，不阻塞 |
| 向量 embedding | 8s | 跳过向量检索 |
| Reranker API | 5s | 直接用 RRF 结果 |
| RAG 路由判断 | 5s | 保守降级：执行检索 |
| LLM 工具调用 | 30s | 正常超时报错 |

### 8.3 关闭外部 Telemetry

```python
# main.py 启动时最先设置
os.environ["MEM0_TELEMETRY"] = "false"       # 关闭 PostHog
os.environ["ANONYMIZED_TELEMETRY"] = "false"  # 关闭 ChromaDB 遥测
# langsmith_setup.py
if not os.environ.get("LANGCHAIN_TRACING_V2") == "true":
    os.environ["LANGCHAIN_TRACING_V2"] = "false"  # 防止后台连接 LangSmith
```

### 8.4 性能指标（测试用例：农历查询）

```
[TIMER] memory_node=1240ms
[TIMER] rag_node=120ms (router: skip)     ← Adaptive RAG 跳过检索
[RAGRouter] 98ms → 跳过RAG
[TIMER] context_processor_node=450ms
[TIMER] chat_node=3200ms（LLM 生成）
Total ≈ 4~5s（相比优化前 15~40s）
```

---

## 九、解决的 Bug 记录

### Bug 1：ToolMessage 孤儿问题（出现 3 次）

**错误**：`openai.BadRequestError: Messages with role 'tool' must be a response to a preceding message with 'tool_calls'`

**根因**：LangGraph 滑动窗口/RemoveMessage 切割了 `AIMessage(tool_calls)` 与对应 `ToolMessage` 之间的关联。

**修复**：重写 `_sanitize_window`，将 `AIMessage(tool_calls)` 和全部对应 `ToolMessage` 作为原子单元处理。

---

### Bug 2：thinking 模型不支持 tool_choice

**错误**：`Thinking mode does not support this tool_choice`

**根因**：Worker LLM 用了 `with_structured_output()`，底层使用 `tool_choice` 参数，DeepSeek thinking 模型不支持。

**修复**：改为 prompt 内嵌 JSON Schema + 手动解析，完全兼容 thinking 模型。

---

### Bug 3：mem0 API 变更

**错误**：`Top-level entity parameters frozenset({'user_id'}) are not supported in search()`

**根因**：mem0 新版 API 废弃了 `user_id=` 关键字参数。

**修复**：`mem0.search(query, filters={"user_id": user_id})`

---

### Bug 4：mem0 数据提取字段名

**问题**：Agent 记住了名字但每次都说"没有告诉过我"。

**根因**：mem0 返回结果字段名由 `"memory"` 变为 `"data"`，提取失败导致 `mem0_context` 始终为空。

**修复**：
```python
content = m.get("data") or m.get("memory") or ""
```

---

### Bug 5：PostgreSQL 多语句报错

**错误**：`psycopg.errors.SyntaxError: cannot insert multiple commands into a prepared statement`

**根因**：psycopg3 不允许在一条 `execute` 中执行多条 SQL（用分号分隔）。

**修复**：将 `CREATE TABLE` 和 `CREATE INDEX` 分开执行。

---

### Bug 6：mem0 ChromaDB 不支持关键字搜索

**错误**：`The 'chroma' vector store does not support keyword search` + `Failed to load spaCy lemma model`

**根因**：mem0 内部尝试对 ChromaDB 做 BM25 混合搜索，ChromaDB 不支持。

**修复**：将 mem0 向量后端从 ChromaDB 迁移到 pgvector，在同一 PostgreSQL 实例中统一管理。

---

### Bug 7：Reranker 本地模型冷启动 40s+

**问题**：Reranker API 失败时静默降级到本地 torch 模型，冷启动需要 40+ 秒。

**修复**：`reranker.py` API 失败时直接抛出异常，`retriever.py` 捕获异常后立即返回 RRF 结果（0ms 降级）。

---

## 十、OWASP Agent Memory Guard（安全防护）

### 10.1 ASI06 记忆污染防护

```python
from agent_memory_guard import MemoryGuard, Policy

guard = MemoryGuard(policy=Policy.strict())

# 写入 mem0 前校验
result = guard.check(content)
if result.is_safe:
    await mem0.add(content, ...)
else:
    # 拒绝写入，记录日志
```

防护对象：
- Prompt Injection（注入伪装成用户记忆的指令）
- Self-reinforcement loop（LLM 把自己的输出当作事实存入记忆）
- 个人信息泄露（PII 检测）

---

## 十一、审核级联系统

### 11.1 三级级联架构

```
用户内容
   ↓
T1: 白名单快速通过（长度≤15字 + 无触发词 → clear_normal，<1ms）
   ↓ 不满足
T2: 阿里云内容安全 API（comment_detection_pro，~200ms）
   ↓ 灰区（0.15~0.85）
T3: 主 LLM 深度分析（DeepSeek，~3s）
```

### 11.2 阿里云内容安全配置

```python
ALIYUN_ACCESS_KEY_ID = "LTAI5t8gjtHkpBb2sPmstF1k"
ALIYUN_GREEN_ENDPOINT = "green-cip.cn-shanghai.aliyuncs.com"
ALIYUN_GREEN_SERVICE = "comment_detection_pro"
CASCADE_API_TIMEOUT_MS = 800  # 超时 fail-open，退回 LLM
```

---

## 十二、MCP 工具服务

### 12.1 shillguard MCP Server（4 个工具）

| 工具 | 用途 | 调用时机 |
|------|------|---------|
| `rag_search_laws` | 检索平台规则和法律法规（本地知识库） | 涉及法规条款时必须调用 |
| `moderate_content` | 审核被举报内容（返回违规程度+证据） | 有具体内容需要审核时 |
| `detect_user_score` | 评估用户恶意程度（0~1 异常分） | 评估某用户是否应预警/禁言 |
| `generate_evidence` | 生成禁言证据报告 | 异常分≥0.9 后链式调用 |

### 12.2 调用优先级规则

```
涉及法律/规则 → rag_search_laws（不用网络搜索）
审核内容 → moderate_content（content_list 参数）
检测用户 → detect_user_score → generate_evidence（链式）
通用问题 → Tavily 网络搜索
```

---

## 十三、前端实现

### 13.1 SSE 流式接收

```typescript
// useSSE.ts
async function readSSE(resp: Response, handlers) {
    const reader = resp.body.getReader()
    let buf = ''
    while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buf += decoder.decode(value, { stream: true })
        const lines = buf.split('\n')
        buf = lines.pop() ?? ''
        for (const line of lines) {
            if (!line.startsWith('data: ')) continue
            const evt = JSON.parse(line.slice(6))
            if (evt.type === 'delta') handlers.onDelta(evt.content)
            if (evt.type === 'done')  handlers.onDone?.()
        }
    }
}
```

### 13.2 SSE 事件格式（后端）

```python
# router.py 只输出 chat_node 的 delta，过滤其他节点事件
if event["event"] == "on_chat_model_stream":
    if event.get("metadata", {}).get("langgraph_node") == "chat":
        chunk = event["data"]["chunk"]
        if chunk.content:
            yield f'data: {{"type":"delta","content":{json.dumps(chunk.content)}}}\n\n'
```

### 13.3 Markdown 渲染

```typescript
import { marked } from 'marked'
import hljs from 'highlight.js'
import 'highlight.js/styles/github.css'

marked.setOptions({ breaks: true })
// v-html="marked(content)" 渲染 AI 输出
```

### 13.4 Vite 代理配置

```typescript
// vite.config.ts
proxy: {
  '/ai': { target: 'http://localhost:8000', changeOrigin: true },
  '/policy-api': {
    target: 'http://localhost:8001',
    changeOrigin: true,
    rewrite: (path) => path.replace(/^\/policy-api/, '')
  }
}
```

---

## 十四、API 接口清单

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/ai/chat` | 对话（SSE 流式） |
| GET  | `/ai/history/{thread_id}` | 获取对话历史 |
| GET  | `/ai/conversations` | 获取会话列表 |
| POST | `/ai/upload` | 上传 RAG 知识库文件 |
| GET  | `/ai/documents` | 获取知识库文档列表 |
| DELETE | `/ai/documents/{filename}` | 删除知识库文档 |
| POST | `/ai/generate-tests` | 生成测试用例（SSE） |
| POST | `/moderate` | 内容审核 |
| POST | `/detect` | 用户异常检测 |
| POST | `/evidence` | 生成禁言证据 |

---

## 十五、启动方式

### 后端启动

```
cd shill-guard-ai
conda activate agent
python -m uvicorn app.main:app --reload --port 8000
```

### 前端启动

```
cd shill-guard-frontend
npm run dev
# 访问 http://localhost:3000
```

### 环境变量关键配置（.env）

```env
LLM_API_KEY=your_llm_api_key
LLM_MODEL=deepseek-v4-flash
LLM_BASE_URL=https://api.deepseek.com

embed_api_key=your_embed_api_key
embed_base_url=https://dashscope.aliyuncs.com/compatible-mode/v1
embedding_model=text-embedding-v3

pg_host=localhost
pg_port=5432
pg_dbname=shillguard
pg_user=postgres
pg_password=your_pg_password

es_url=http://localhost:9200
SILICONFLOW_API_KEY=your_siliconflow_api_key
```

---

## 十六、与 PolicyRadar 的对比

| 维度 | ShillGuard | PolicyRadar |
|------|-----------|------------|
| 服务端口 | 8000 | 8001 |
| PG 数据库 | shillguard | policy_radar |
| ES 索引 | shillguard_kg | policy_radar_* |
| Redis DB | db=0（未用） | db=1（未用） |
| Agent 框架 | LangGraph（5节点） | LangGraph（3节点） |
| 记忆系统 | 三层（窗口+摘要+mem0） | 无（单次报告生成） |
| RAG | 混合检索 + Adaptive RAG | 混合检索（pgvector+ES） |
| 爬虫 | 无 | Playwright + 定时采集 |
| 输出格式 | 对话流式 | 结构化报告流式 |

---

*最后更新：2026-07-13*
