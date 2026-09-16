# 级联测试以及 RAG 检索质量优化

> 本文档记录 ShillGuard 内容审核系统在两个核心方向上的工程优化：**审核级联（Cascade）** 与 **RAG 检索质量**。包括原始问题描述、解决思路、实现细节与量化效果。

---

## 一、审核级联（Cascade）

### 1.1 问题背景

原始架构中，每一条被举报的内容都会走完整的 LLM 链路：

```
classify_node → retrieve_node → judge_node → evidence_node → action_node
```

这条链路平均调用 LLM **3~4 次**，端到端耗时 **10~30 秒**。但实际上：

- **明确违规内容**（如"约炮加微信"、"刷单兼职日赚过千"）无需 LLM 判断，关键词一眼就能认定
- **明确正常内容**（如用户随手发的短句子、无任何风险信号）同样不需要 LLM 处理
- 只有**模糊地带**（阴阳怪气、语境复杂、多义词）才真正需要 LLM 出手

大量 LLM 调用被浪费在"本来就不需要 AI 来判"的内容上，造成：
- 响应延迟高、并发能力差
- API 成本随请求量线性增长
- 资源不能集中在真正困难的审核任务上

### 1.2 解决思路：三层级联架构

参考小红书 **Hi-Guard（KDD 2026）** 和 **TianPan 四级架构**，在 LLM 链路前插入确定性前置过滤层：

```
T1-黑名单  (<1ms)  → 铁证违规，直接输出模板证据报告
T1-白名单  (<1ms)  → 铁证正常，直接放行
T2-加权    (<5ms)  → 软信号累加，超阈值判违规
T3-LLM    (1-30s) → 以上均无命中，退回完整 LLM 链路处理灰区
```

整个前置过滤层是**纯确定性代码，不调用任何 LLM**，单条耗时 < 5ms。

### 1.3 实现细节

**T1 黑名单**（`cascade.py`）

维护按违规类型分类的关键词表，每条命中直接返回预设的分数、违规规则、法律条文，并生成模板化证据报告（无需调 LLM）：

```
pornography  → ["约炮", "裸聊", "色情服务", ...]    score=0.92
fraud        → ["刷单兼职", "日赚过千", "稳赚不赔",...] score=0.88
illegal_sales → ["处方药代购", "电子烟批发", ...]    score=0.85
political    → [涉政词表，运营热更新]               score=0.95
privacy_doxxing → ["人肉他", "挂人", "扒他底细",...]  score=0.90
```

**T2 加权软信号**

对于没有命中硬关键词但含有风险信号的内容，累加权重分数：

```python
软信号词表示例：
  "废物" → 0.15    "去死"  → 0.20
  "进群" → 0.10    "外链"  → +0.15
  "联系方式（微信/QQ/TG）" → +0.25
```

当累加分数 ≥ 高阈值（默认 0.55）时，判定 `clear_violation`。

**T1 白名单**

同时满足以下全部条件才判正常（保守策略，任意一条不满足则进 T3）：
- 文本长度 < 15 字
- 未命中任何黑名单关键词
- 软信号加权分 = 0
- 不含联系方式/URL/手机号/身份证
- 举报分类为"其他"（误报率最高的类别）

**LangGraph 路由**

级联节点 `pre_filter_node` 作为图的入口，输出 `tier_verdict`：

```
pre_filter → clear_violation → evidence → citation_verify → action
           → clear_normal   → action
           → ambiguous      → classify → retrieve → judge → evidence → ...
```

`cascade_enabled=False` 时整个前置层透明穿透，零回归影响。

### 1.4 评测体系（cascade_eval.py）

专门编写 `loadtest/cascade_eval.py`，**直接调用 `pre_filter_node` 函数**（不走 HTTP，纯本地），计算以下指标：

| 指标 | 含义 |
|---|---|
| 各 Tier 命中率 | T1黑名单 / T1白名单 / T2加权 / T3退回LLM 各自处理比例 |
| **LLM 调用减少率** | `1 - (退回LLM数 / 总数)` — 简历核心硬指标 |
| 用例正确率 | 期望 verdict vs 实际 verdict |
| 误判率 | 黑判白 / 白判黑 各自比例 |
| 单条平均延迟 + P95 延迟 | 确定性层延迟（应 < 5ms） |
| 开启 vs 关闭对照 | 量化级联带来的 LLM 调用节省量 |

测试用例覆盖 4 类场景：
- `t1_blacklist_violation`：应被 T1 黑名单命中
- `t1_whitelist_normal`：应被 T1 白名单放行
- `t2_weighted_violation`：应被 T2 加权判违规
- `t3_ambiguous`：应退回 LLM 处理

### 1.5 核心价值

**工程价值**：可量化"LLM 调用减少 X%"，直接对应成本和并发性能提升。

**架构价值**：确定性层 + AI层 分工明确，体现系统设计能力，不是"把所有东西都塞给 LLM"。

**参考来源**：Hi-Guard（小红书，KDD 2026）提出将人工审核成本降低 56% 的工程化对应方案；TianPan 四级架构是工业界内容审核标准范式。

---

## 二、RAG 检索质量优化

### 2.1 问题背景

原始 RAG 检索方案存在以下缺陷：

**问题一：单路检索，top_k=5，召回率低**

`retrieve_node` 只构造一条查询，例如：

```python
query = "网络暴力 侮辱诽谤 人身攻击 治安管理处罚法"
snippets = await retrieve(query, top_k=5)
```

当 `classify_node` 分类错误时（把"阴阳怪气"分类成 `normal` 而非 `cyberbullying`），检索直接使用错误的类型模板，导致完全检索不到相关法条。

测量结果：`context_recall = 65.2%`（1/3 的关键法条没被检索到）

**问题二：LLM 凭记忆引用法条（幻觉）**

即便 RAG 没有检索到《某法》，`evidence_node` 生成报告时 LLM 仍会从训练记忆里"凭空"引用该法律，导致引用法条在检索上下文中无原文支撑。

测量结果：`faithfulness = 68%`，幻觉率高达 **32%**

**问题三：评测工具测的和后端跑的不一致**

`ragas_eval.py` 内部用单路 `retrieve(content, top_k=5)` 来评测 `context_recall`，而后端实际使用多路检索。这导致评测数据不能真实反映系统的实际召回能力（测量偏差）。

### 2.2 解决方案一：三路并行检索（Step-Back + 本体约束）

**设计思路**

参考 2026 年法律检索领域论文（arXiv 2505.03970）和 OG-RAG（Ontology-Guided RAG）：

> 单一查询路径的最大风险是"分类一旦错，检索就废"。增加多路独立查询，让不同路径互相兜底。

引入 **Step-Back 抽象 + 法律本体约束** 防止 HyDE（Hypothetical Document Embeddings）在中文法律垂直域的漂移问题。

**三路查询设计**（`query_expander.py`）

```
Q1 原文语义查询：直接用内容文本做检索，完全绕开分类
                 → 保底路径，分类错也能找到相关内容
Q2 类型模板查询：保留原有 _TYPE_QUERY_MAP 精准检索
                 → 分类正确时效果最好
Q3 Step-Back 本体约束查询：
   1. 轻量 LLM 从 law_taxonomy.py 固定词表中选 3 条最相关检索词
   2. 这些词作为查询，搜索同一个 RAG 知识库
   3. LLM 输出受本体约束，无法漂移到训练集中不存在的法律名称
```

**法律本体（`law_taxonomy.py`）**

固定了 12 种违规类型的法律检索词表，覆盖从 `cyberbullying` 到新增的 `violence`、`gambling`、`drugs`，每类 5~7 条精准检索词。重要补充：

- `cyberbullying` 补充了**刑法第293条寻衅滋事罪**（网络暴力最高频入罪条款）
- `rumor` 补充了**互联网信息服务深度合成管理规定**（AI 换脸谣言）
- `fraud` 补充了**证券法虚假陈述**（投资诈骗广告）
- 三处定义（`CLASSIFY_PROMPT` / `law_taxonomy.py` / `_TYPE_QUERY_MAP`）严格保持一致

**检索合并策略**

- top_k 从原来的 5 提升到 **10/路**
- 三路并行（`asyncio.gather`），互不阻塞
- 去重后按"被多路命中次数"重排（被 2 路命中优于只被 1 路命中）
- 取前 **15 条**进入 judge_node，避免上下文过长

### 2.3 解决方案二：引用幻觉双重防御

**防御层 A — 事前 Prompt 约束（`EVIDENCE_PROMPT`）**

在 `EVIDENCE_PROMPT` 加入最高优先级规则段：

```
## 引用规则（最高优先级，必须严格遵守）
1. 只引用上方"知识库检索到的相关条款"中出现过的法律名称和条文
2. 若某法律未出现在检索结果中，不得引用，改写为"依据相关法律法规"
3. 禁止凭记忆补充任何《xxx》格式的法律引用
4. 宁可引用少、引用准，不要引用多、引用错
```

**防御层 B — 事后 Citation Verifier（`citation_verifier.py`）**

新增独立 LangGraph 节点 `citation_verify_node`，在 `evidence_node` 之后、`action_node` 之前执行：

```
evidence → citation_verify → action
```

处理逻辑（纯 Python，无 LLM）：
1. 正则提取 evidence_detail 中所有 `《法律名》` 引用
2. 检查每个法律名是否在 `rag_context` 字符串中有原文出现
3. 找不到的：
   - 将正文中的 `《xxx》` 替换为 `[xxx·待核实]`
   - 在报告末尾追加 `> **引用溯源说明**` 警告块
   - 从 `violated_laws` 列表中剔除该条
4. 记录 `hallucinated_laws` 列表到 state，供遥测使用

### 2.4 修复评测工具的测量偏差

将 `ragas_eval.py` 的 `get_rag_contexts()` 改为与 `retrieve_node` 完全相同的逻辑：

```python
# 改前（测的是旧逻辑）
return await retrieve(content_text, top_k=5)

# 改后（测的是后端真实逻辑）
queries = await expand_queries(content_list, content_type, type_query)
result_lists = await asyncio.gather(*[retrieve(q, top_k=10) for q in queries])
merged = dedup_and_merge(list(result_lists))
return merged[:15]
```

保证"评测用的检索上下文"与"后端实际用的检索上下文"完全一致。

### 2.5 量化效果

| 指标 | 优化前 | 优化后 | 提升 |
|---|---|---|---|
| `context_recall` | 65.2% | **88.9%** | +23.7pp |
| `faithfulness` | 68.0% | **94.4%** | +26.4pp |
| 幻觉率 | 32.0% | **6.0%** | -26pp |

---

## 三、两模块的关系

```
                  ┌──────────────────────────────────────────┐
  举报内容输入 ──→│        pre_filter_node（级联层）           │
                  │  T1黑名单 / T1白名单 / T2加权 / T3退回LLM │
                  └────────────────────┬─────────────────────┘
                                       │ ambiguous（灰区）
                                       ↓
                  ┌──────────────────────────────────────────┐
                  │           classify_node（LLM分类）        │
                  └────────────────────┬─────────────────────┘
                                       ↓
                  ┌──────────────────────────────────────────┐
                  │  retrieve_node（三路并行 RAG 检索）        │
                  │  Q1原文 + Q2类型模板 + Q3 Step-Back本体   │
                  │  top_k=10/路，去重后取前15条              │
                  └────────────────────┬─────────────────────┘
                                       ↓
                        judge / evidence / citation_verify / action
```

- **级联层** 解决的是"不该调 LLM 的不调"，降低成本和延迟
- **RAG 优化** 解决的是"要调 LLM 时，给 LLM 的上下文足够准确"，提升召回和降低幻觉
- 两者互补，共同构成完整的审核质量保障体系

---

*最后更新：2026-07-10*
