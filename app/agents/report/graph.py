"""
报告生成 Agent — LangGraph 编排入口（app/agents/report/graph.py）

═══════════════════════════════════════════════════════════════════
本文件函数清单
═══════════════════════════════════════════════════════════════════
  1. get_llm                 构造报告主模型（流式 ChatOpenAI）
  2. retrieve_node           节点①：混合 RAG 检索候选政策
  3. expand_related_node     节点②：政策关系图谱 1 跳补召回
  4. grade_policies_node     节点③：相关性评分 + 噪声过滤 + 核心事实
  5. verify_policies_node    节点④：时效验证 + 五类关联冲突门控
  6. format_context_node     节点⑤：拼装 LLM messages 与 context_text
  7. _build_plain_context    降级：无验证结果时的纯文本 context
  8. build_graph             注册节点与边，编译 StateGraph
  9. get_graph               单例获取已编译图
 10. stream_report           对外入口：跑图 → 流式生成 → 引用校验

节点流程：
  START → retrieve → expand_related → grade_policies
        → verify_policies → format_context → END
  图外：LLM.astream(messages) → citation_verifier（事后保底）

依赖：
  app.rag.retriever.retrieve
  app.rag.policy_graph.expand_related
  app.rag.grader.grade_and_filter
  app.rag.conflict_gate.run_conflict_gate
  app.agents.report.policy_verifier
  app.agents.report.citation_verifier
  app.agents.report.prompts.REPORT_SYSTEM
"""
import time
from typing import Annotated, AsyncIterator
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langchain_core.messages import SystemMessage, HumanMessage, BaseMessage
from langchain_openai import ChatOpenAI
from app.config import get_settings
from app.rag.retriever import retrieve
from app.rag.policy_graph import expand_related
from app.rag.conflict_gate import run_conflict_gate
from app.agents.report.prompts import REPORT_SYSTEM
from app.agents.report.citation_verifier import verify_citations
from app.agents.report.policy_verifier import (
    verify_policies,
    format_verified_context,
    VerificationResult,
)
from app.core.observability import get_callbacks


# ── State ──────────────────────────────────────────────────────────────────────
class ReportState(TypedDict):
    """报告 Agent 图状态。

    keywords:          前端导航词 {l1,l2,l3,region,company_size,...}
    user_input:        企业补充描述
    module:            业务模块标识（如 policy）
    policies:          RAG + 图谱扩展后的候选
    graded_policies:   Grader 过滤后的高质量候选
    graph_meta:        图谱扩展元数据（expanded_ids / edges_used）
    verification:      验证+门控结果序列化 dict
    context_text:      最终喂给 LLM 的政策上下文
    messages:          System + Human，供流式生成
    report_text:       预留完整报告文本字段
    hallucinated_laws: 预留幻觉引用列表
    """
    keywords: dict
    user_input: str
    module: str
    policies: list[dict]
    graded_policies: list[dict]
    graph_meta: dict
    verification: dict
    context_text: str
    messages: Annotated[list[BaseMessage], add_messages]
    report_text: str
    hallucinated_laws: list[str]


# ── LLM ────────────────────────────────────────────────────────────────────────
def get_llm() -> ChatOpenAI:
    """构造报告生成用的主 LLM（流式）。

    流程：
      1. 读取 Settings（模型名、api_key、base_url）
      2. 返回 temperature=0.3、streaming=True 的 ChatOpenAI

    参数：无

    返回：
      ChatOpenAI：供 stream_report 中 astream 使用

    作用：
      统一报告生成模型配置，避免各处散落硬编码。
    """
    s = get_settings()
    return ChatOpenAI(
        model=s.llm_model,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        temperature=0.3,
        streaming=True,
    )


# ── 节点1：检索 ─────────────────────────────────────────────────────────────────
async def retrieve_node(state: ReportState) -> dict:
    """节点①：按用户关键词与补充描述做混合 RAG，召回候选政策。

    流程：
      1. 调用 retrieve(keywords, user_input, top_n=12)
      2. 打 TIMER 日志
      3. 将结果写入 state["policies"]

    作用：
      为后续图谱扩展、评分、验证、生成提供种子证据池。
    """
    t0 = time.perf_counter()
    policies = await retrieve(state["keywords"], state["user_input"], top_n=12)
    elapsed = int((time.perf_counter() - t0) * 1000)
    print(f"[TIMER] report.retrieve={elapsed}ms ({len(policies)} items)", flush=True)
    return {"policies": policies}


# ── 节点2：图谱 1 跳补召回 ──────────────────────────────────────────────────────
async def expand_related_node(state: ReportState) -> dict:
    """节点②：沿 policy_relations 强/弱边补全关联政策（如省→市细则）。

    流程：
      1. 取 retrieve 的 policies 作为种子
      2. expand_related → 合并邻居政策，标记 from_graph_expand
        先获得种子政策id，然后根据该id进行获取知识图谱的边信息，是一个list的dict类型数据
      3. 写入 policies + graph_meta（expanded_ids / edges_used）

      #最终返回的是种子、以及图谱捕获的信息，以及捕获知识图谱的元数据：

    作用：
      解决「只召回 A、漏掉同项目 B」的结构漏召；为冲突门控提供关联对。
    """
    
    """
    merged = [
    # ── 种子：检索直接命中，没有图谱标记 ──
    {
        "id": 101,
        "title": "广东省粤创智造高新技术企业研发费用补贴管理办法",
        "publisher": "广东省科学技术厅",
        "region": "广东省",
        "deadline": "2026-09-30",
        "summary": "对高新技术企业给予研发费用补贴……",
        "funding_amount": "最高500万元",
        "key_conditions": "须为高新技术企业",
        "category_l1": "科技创新",
        "category_l2": "",
        "source_url": "https://example.com/p101",
        "matched_chunk": "……最高补贴500万元，申报截止2026-09-30……",
        "sim": 0.82,              # 检索可能带的相似度（有则保留）
        # 注意：没有 from_graph_expand
    },

    # ── 补召：图谱一跳拉来，多 4 个标记字段 ──
    {
        "id": 205,
        "title": "东莞市粤创智造高新技术企业研发费用补贴实施细则",
        "publisher": "东莞市科技局",
        "region": "东莞市",
        "deadline": "2026-10-31",
        "summary": "落实省级政策，本市单户最高450万元……",
        "funding_amount": "最高450万元",
        "key_conditions": "须在东莞注册",
        "category_l1": "科技创新",
        "category_l2": "",
        "source_url": "https://example.com/p205",
        "matched_chunk": "……本市补贴上限450万元……",
        "full_text": "……（fetch 可能带正文摘要）……",

        "from_graph_expand": True,              # 标记：来自图谱
        "expand_relation": "implements",        # 与种子的关系
        "expand_evidence": "标题：实施细则→管理办法",
        "expand_confidence": 0.9,               # 边置信度
    },
]
    meta = {
    "expanded_ids": [205],       # 本次新补进列表的政策 id
    "expand_count": 1,           # 新补了几条
    "gate_hint": "expanded",     # 提示：补到了邻居（没有邻居则是 "no_neighbors"）
    "edges_used": [              # 查到的边，给后面冲突门控用
        {
            "source_policy_id": 101,
            "target_policy_id": 205,
            "relation_type": "implements",
            "confidence": 0.9,
            "evidence": "标题：实施细则→管理办法",
        }
    ],
}
    """
    t0 = time.perf_counter()
    seeds = state.get("policies") or []
    if not seeds:
        return {"policies": [], "graph_meta": {}}

    merged, meta = await expand_related(seeds, include_weak=True, max_expand=8)
    elapsed = int((time.perf_counter() - t0) * 1000)
    print(
        f"[TIMER] report.expand={elapsed}ms "
        f"(seeds={len(seeds)} → {len(merged)}, +{meta.get('expand_count', 0)})",
        flush=True,
    )
    return {"policies": merged, "graph_meta": meta}


# ── 节点3：Retrieval Grader（相关性评分 + 噪声过滤）──────────────────────────
async def grade_policies_node(state: ReportState) -> dict:
    """节点③：对候选政策做相关性打分、过滤噪声并提炼核心事实。

    流程：
      1. 若 policies 为空 → 返回空 graded_policies
      2. grade_and_filter（1 次 Fast LLM）
      3. 图谱补召条目若被滤掉但存在 high-confidence 强边，保底加回最多 2 条

    作用：
      减少无关政策进入验证与生成；同时避免强关联细则被误杀。
    """
    t0 = time.perf_counter()
    policies = state["policies"]
    if not policies:
        return {"graded_policies": []}

    from app.rag.grader import grade_and_filter
    #构建紧凑的政策描述
    graded = await grade_and_filter(
        policies,
        state["keywords"],
        state["user_input"],
    )

    # 强扩展保底：被 Grader 滤掉的图谱补召 + 置信度≥0.55 的条目，最多救回 2 条
    graded_ids = {p.get("id") for p in graded}
    rescued = []
    for p in policies:
        if not p.get("from_graph_expand"):
            continue
        if p.get("id") in graded_ids:
            continue
        if float(p.get("expand_confidence") or 0) < 0.55:
            continue
        if p.get("expand_relation") not in (
            "implements", "same_program", "lists_under", "funding_of", "supersedes",
        ):
            continue
        item = dict(p)
        item.setdefault("grader_score", 5)
        item.setdefault("graded_key_facts", "（图谱强关联保底保留）")
        rescued.append(item)
        if len(rescued) >= 2:
            break
    if rescued:
        graded = graded + rescued
        print(f"[ReportGraph] 图谱强关联保底救回 {len(rescued)} 条", flush=True)

    elapsed = int((time.perf_counter() - t0) * 1000)
    print(
        f"[TIMER] report.grade={elapsed}ms "
        f"({len(policies)}条 → {len(graded)}条)",
        flush=True,
    )
    return {"graded_policies": graded}


# ── 节点4：声明级验证 + 五类冲突门控 ───────────────────────────────────────────
async def verify_policies_node(state: ReportState) -> dict:
    """节点④：时效/批内矛盾 + 关联对五类冲突门控，并预生成带标注 context。

    流程：
      1. 取 graded_policies
      2. verify_policies（单条时效 + 批内金额/条件）
      3. run_conflict_gate（按 graph_meta.edges_used 对关联对做 5 类检测）
      4. 用门控附注后的政策重跑 format_verified_context
      5. 合并 warning / gate_status / conflict_pairs / expanded_ids 写入 verification

    作用：
      生成前强制暴露截止/金额/门槛/效力/互斥冲突，驱动双条目报告输出。
    """
    t0 = time.perf_counter()
    policies = state.get("graded_policies") or state["policies"]
    graph_meta = state.get("graph_meta") or {}
    if not policies:
        return {"verification": {}}

    result: VerificationResult = await verify_policies(policies)

    # 五类关联冲突门控：仅对强关联边检测，避免 region_child 弱边误报
    _STRONG = {
        "implements", "same_program", "lists_under", "funding_of", "supersedes",
    }
    edges_for_gate = [
        e for e in (graph_meta.get("edges_used") or [])
        if e.get("relation_type") in _STRONG
    ]
    gate = await run_conflict_gate(
        policies,
        edges_used=edges_for_gate,
        locate_fulltext=True,
        max_locate=3,
    )
    annotated = gate.annotated_policies or policies
    result.gate_status = gate.gate_status
    result.conflict_pairs = gate.to_meta().get("conflict_pairs", [])
    result.expanded_ids = list(graph_meta.get("expanded_ids") or [])

    # 合并预警文案
    warnings = [w for w in (result.warning_summary, gate.warning_summary) if w]
    result.warning_summary = "\n".join(warnings)

    formatted = format_verified_context(annotated, result)

    verification_dict = {
        "expired_count": len(result.expired_policies),
        "contradiction_count": len(result.contradictions) + len(gate.conflict_pairs),
        "warning_summary": result.warning_summary,
        "gate_status": gate.gate_status,
        "expanded_ids": result.expanded_ids,
        "conflict_pairs": result.conflict_pairs,
        "contradictions": [
            {
                "dimension": c.dimension,
                "policy_a": c.policy_a_title,
                "policy_b": c.policy_b_title,
                "value_a": c.value_a,
                "value_b": c.value_b,
                "description": c.description,
                "severity": c.severity,
            }
            for c in result.contradictions
        ],
        "validity_map": {
            k: {"status": v.status, "badge": v.badge, "note": v.note}
            for k, v in result.validity_map.items()
        },
        "_formatted_context": formatted,
        "_annotated_policies": annotated,
    }

    elapsed = int((time.perf_counter() - t0) * 1000)
    print(
        f"[TIMER] report.verify={elapsed}ms "
        f"(expired={verification_dict['expired_count']} "
        f"batch_conflicts={len(result.contradictions)} "
        f"gate={gate.gate_status} gate_conflicts={len(gate.conflict_pairs)})",
        flush=True,
    )
    return {
        "verification": verification_dict,
        "graded_policies": annotated,
    }


# ── 节点5：构造 LLM 上下文 ──────────────────────────────────────────────────────
async def format_context_node(state: ReportState) -> dict:
    """节点⑤：把验证/门控后的政策信息格式化为 LLM 的 System/Human messages。

    流程：
      1. 取 graded_policies 与 verification
      2. 用 _formatted_context 或降级拼装；前置 warning_summary
      3. format REPORT_SYSTEM（含 gate_status / conflict 计数）
      4. 返回 messages + context_text
    """
    policies = state.get("graded_policies") or state["policies"]
    verification = state.get("verification", {})

    if not policies:
        context = "【当前暂无匹配政策数据，请提示用户系统正在持续采集中。】"
    else:
        context = verification.get("_formatted_context") or _build_plain_context(policies)
        warning = verification.get("warning_summary", "")
        if warning:
            context = warning + "\n\n" + "─" * 50 + "\n\n" + context

    kw = state["keywords"]
    kw_text = "、".join(filter(None, [
        kw.get("l1"), kw.get("l2"), kw.get("l3"),
        f"地区：{kw['region']}" if kw.get("region") else None,
    ]))

    system_content = REPORT_SYSTEM.format(
        keywords_text=kw_text or "（未指定）",
        user_input=state["user_input"] or "（企业未提供额外信息）",
        contradiction_count=verification.get("contradiction_count", 0),
        expired_count=verification.get("expired_count", 0),
        gate_status=verification.get("gate_status", "pass"),
        expand_count=len(verification.get("expanded_ids") or []),
        gate_conflict_count=len(verification.get("conflict_pairs") or []),
    )

    msgs = [
        SystemMessage(content=system_content),
        HumanMessage(content=f"以下是经过验证的政策信息：\n\n{context}\n\n请生成报告。"),
    ]
    return {"messages": msgs, "context_text": context}


def _build_plain_context(policies: list[dict]) -> str:
    """无验证结果时的降级 context 拼装。"""
    lines = []
    for i, p in enumerate(policies, 1):
        deadline = str(p.get("deadline") or "未知")
        expand = ""
        if p.get("from_graph_expand"):
            expand = f"【图谱补召·{p.get('expand_relation', '')}】"
        lines.append(
            f"[政策{i}] {expand}《{p.get('title', '未知')}》\n"
            f"  发布单位：{p.get('publisher', '未知')}  地区：{p.get('region', '全国')}\n"
            f"  申报截止：{deadline}  补贴金额：{p.get('funding_amount', '未注明')}\n"
            f"  核心条件：{p.get('key_conditions', '') or '见正文'}\n"
            f"  摘要：{p.get('summary', '') or '（暂无摘要）'}\n"
            f"  来源：{p.get('source_url', '') or '内部数据库'}"
        )
    return "\n\n".join(lines)


# ── 图构建 ──────────────────────────────────────────────────────────────────────
def build_graph():
    """构建并编译报告 Agent 的 StateGraph。

    拓扑：
      START → retrieve → expand_related → grade_policies
            → verify_policies → format_context → END
    """
    g = StateGraph(ReportState)
    g.add_node("retrieve", retrieve_node)
    g.add_node("expand_related", expand_related_node)
    g.add_node("grade_policies", grade_policies_node)
    g.add_node("verify_policies", verify_policies_node)
    g.add_node("format_context", format_context_node)
    g.add_edge(START, "retrieve")
    g.add_edge("retrieve", "expand_related")
    g.add_edge("expand_related", "grade_policies")
    g.add_edge("grade_policies", "verify_policies")
    g.add_edge("verify_policies", "format_context")
    g.add_edge("format_context", END)
    return g.compile()


_graph = None


def get_graph():
    """懒加载单例：获取已编译的报告图。"""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


# ── 流式生成器（供 API router 和 chat tools 使用）───────────────────────────────
async def stream_report(
    keywords: dict,
    user_input: str,
    module: str,
    out_meta: dict | None = None,
) -> AsyncIterator[str]:
    """报告 Agent 对外异步生成器：跑图 → 流式 LLM → 引用校验。

    out_meta 额外写入：
      gate_status / expanded_ids / conflict_pairs
    """
    graph = get_graph()
    init_state: ReportState = {
        "keywords": keywords,
        "user_input": user_input,
        "module": module,
        "policies": [],
        "graded_policies": [],
        "graph_meta": {},
        "verification": {},
        "context_text": "",
        "messages": [],
        "report_text": "",
        "hallucinated_laws": [],
    }

    result = await graph.ainvoke(init_state)
    messages = result["messages"]
    context_text = result.get("context_text", "")

    if out_meta is not None:
        v = result.get("verification", {})
        out_meta["expired_count"] = v.get("expired_count", 0)
        out_meta["contradiction_count"] = v.get("contradiction_count", 0)
        out_meta["contradictions"] = v.get("contradictions", [])
        out_meta["gate_status"] = v.get("gate_status", "pass")
        out_meta["expanded_ids"] = v.get("expanded_ids", [])
        out_meta["conflict_pairs"] = v.get("conflict_pairs", [])
        out_meta["policy_count"] = len(result.get("policies", []))
        out_meta["graded_count"] = len(result.get("graded_policies", []))

    llm = get_llm()
    callbacks = get_callbacks()
    config = {"callbacks": callbacks} if callbacks else {}

    full_report = []
    async for chunk in llm.astream(messages, config=config):
        if chunk.content:
            full_report.append(chunk.content)
            yield chunk.content

    report_str = "".join(full_report)
    verified, hallucinated = verify_citations(report_str, context_text)

    if hallucinated:
        suffix = verified[len(report_str):]
        if suffix:
            yield suffix
