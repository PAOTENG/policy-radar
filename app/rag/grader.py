"""
Retrieval Grader — 报告 Agent 的评分/过滤模块（app/rag/grader.py）

由报告 Agent 的 grade_policies_node 调用，不属于独立 Agent，但是报告流水线关键一环。

═══════════════════════════════════════════════════════════════════
本文件函数清单（共 5 个）
═══════════════════════════════════════════════════════════════════
  1. _build_compact_desc   单条政策压成短描述（控 token）
  2. _build_user_query     把 keywords + user_input 拼成需求描述
  3. _parse_grades         解析 LLM JSON 打分结果（容错）
  4. grade_and_filter      主入口：打分 + 过滤 + 提炼 key_facts
  5. _fallback_enrich      失败/未评分时注入默认字段

设计：Self-RAG 评分 + D2PLAN Purifier，合并为 1 次 Fast LLM 调用。
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage
from app.config import get_settings


# ══════════════════════════════════════════════════════════════════
# 配置常量
# ══════════════════════════════════════════════════════════════════

GRADE_THRESHOLD = 5   # score >= 5 才保留（0-10 分制）
MIN_KEPT = 3          # 至少保留几条（保底召回）
MAX_INPUT = 12        # 单次最多处理几条（控制 prompt 长度）


# ══════════════════════════════════════════════════════════════════
# LLM Prompt
# ══════════════════════════════════════════════════════════════════

_SYSTEM = """你是政策信息筛选专家，负责评估政策文档与企业需求的匹配程度。

【评分标准（0-10）】
9-10：精准命中，政策主题、地区、支持对象与需求高度一致
6-8 ：高度相关，包含用户所需关键信息（金额、条件、截止日期等）
3-5 ：部分相关，话题接近但细节不匹配（如行业不符、地区不同）
0-2 ：关联度低，基本无法为该用户提供有价值信息

【核心事实要求】
仅对 score ≥ 5 的政策，用50字以内提取与用户需求直接相关的核心事实。
内容优先级：补贴金额 > 申报截止 > 关键条件 > 其他
score < 5 的政策，key_facts 填空字符串 ""。

【输出格式】
严格输出 JSON，不要其他任何文字：
{"grades": [{"idx": 0, "score": 8, "key_facts": "最高补贴300万元，需通过国家高新认定，截止2025-06-30"}, ...]}"""


# ══════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════

def _build_compact_desc(idx: int, policy: dict) -> str:
    """构建紧凑的政策描述，控制评分 prompt 的 token。

    流程：标题截断 → 拼类别/地区/补贴/截止 → 摘要取前 100 字

    参数：
      idx: 列表下标（LLM 回写用）
      policy: 政策 dict

    返回：
      str：多行短文本

    作用：
      让 12 条政策能放进单次 Fast LLM 上下文。
    """
    title = (policy.get("title") or "未知")[:35]
    parts = [f"[{idx}] 《{title}》"]

    meta_items = []
    if policy.get("category_l1"):
        meta_items.append(policy["category_l1"])
    if policy.get("region") and policy["region"] not in ("全国", ""):
        meta_items.append(policy["region"])
    if policy.get("funding_amount"):
        meta_items.append(f"补贴:{policy['funding_amount'][:20]}")
    if policy.get("deadline"):
        meta_items.append(f"截止:{policy['deadline']}")
    if meta_items:
        parts.append(f"   属性: {' | '.join(meta_items)}")

    summary = (
        policy.get("summary")
        or policy.get("key_conditions")
        or policy.get("matched_chunk")
        or ""
    )
    if summary:
        parts.append(f"   摘要: {summary[:100]}")

    return "\n".join(parts)


def _build_user_query(keywords: dict, user_input: str) -> str:
    """把导航关键词与企业描述拼成评分用的「用户需求」段落。

    流程：拼接 l1/l2/l3/地区/规模 → 附加 user_input 前 200 字

    参数：
      keywords: 导航 dict
      user_input: 企业补充信息

    返回：
      str：需求描述；全空时返回「政策查询」

    作用：
      作为 HumanMessage 前半段，指导 LLM 打分与提炼 key_facts。
    """
    kw_parts = list(filter(None, [
        keywords.get("l1", ""),
        keywords.get("l2", ""),
        keywords.get("l3", ""),
        f"地区:{keywords['region']}" if keywords.get("region") else "",
        f"企业规模:{keywords['company_size']}" if keywords.get("company_size") else "",
    ]))
    lines = []
    if kw_parts:
        lines.append(f"政策方向：{'、'.join(kw_parts)}")
    if user_input and user_input.strip():
        lines.append(f"企业情况：{user_input.strip()[:200]}")
    return "\n".join(lines) or "政策查询"


def _parse_grades(raw: str, count: int) -> list[dict]:
    """解析 LLM 返回的打分 JSON，做容错清洗。

    流程：
      1. 去掉 ```json 包裹
      2. 正则取第一个 {...}
      3. 读取 grades 列表，校验 idx/score 类型与范围
      4. key_facts 截断至 100 字

    参数：
      raw:   LLM 原始字符串
      count: 本批政策条数（用于校验 idx 边界）

    返回：
      list[dict]：[{idx, score, key_facts}, ...]；失败则 []

    作用：
      兼容模型偶发 markdown/非法字段，避免整条流水线崩溃。
    """
    # 去除 markdown 代码块
    raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
    # 提取第一个 JSON 对象
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return []
    data = json.loads(m.group())
    grades = data.get("grades", [])
    if not isinstance(grades, list):
        return []
    # 过滤非法条目
    valid = []
    for g in grades:
        if not isinstance(g, dict):
            continue
        idx = g.get("idx")
        score = g.get("score")
        if idx is None or score is None:
            continue
        try:
            idx = int(idx)
            score = max(0, min(10, int(score)))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < count:
            valid.append({
                "idx": idx,
                "score": score,
                "key_facts": str(g.get("key_facts", "")).strip()[:100],
            })
    return valid


# ══════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════

async def grade_and_filter(
    policies: list[dict],
    keywords: dict,
    user_input: str,
) -> list[dict]:
    """对候选政策打分、过滤噪声，并提炼核心事实。

    流程：
      1. 取前 MAX_INPUT 条，拼用户需求 + 政策短描述
      2. Fast LLM（15s 超时）一次返回全部 grades JSON
      3. 合并 score/key_facts 到政策 dict
      4. 保留 score≥GRADE_THRESHOLD；不足 MIN_KEPT 则按分补齐
      5. 超时/异常 → _fallback_enrich(原列表)

    参数：
      policies:   RAG 候选（通常 ≤12）
      keywords:   导航关键词
      user_input: 企业补充描述

    返回：
      list[dict]：含 grader_score、graded_key_facts 的过滤后列表

    作用：
      报告 Agent 节点②；减少噪声政策进入验证与生成。
    """
    if not policies:
        return []

    t0 = time.perf_counter()
    input_batch = policies[:MAX_INPUT]

    # 构建 prompt
    policy_block = "\n\n".join(
        _build_compact_desc(i, p) for i, p in enumerate(input_batch)
    )
    user_query = _build_user_query(keywords, user_input)

    s = get_settings()
    llm = ChatOpenAI(
        model=s.fast_llm_model,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        temperature=0,
        max_tokens=1000,
    )

    try:
        resp = await asyncio.wait_for(
            llm.ainvoke([
                SystemMessage(content=_SYSTEM),
                HumanMessage(content=(
                    f"【用户需求】\n{user_query}\n\n"
                    f"【待评估政策，共 {len(input_batch)} 条】\n{policy_block}"
                )),
            ]),
            timeout=15.0,
        )
        grade_list = _parse_grades(resp.content, len(input_batch))
        """得到的结果：
        [
            {"idx": 0, "score": 8, "key_facts": "最高补贴300万元，需通过国家高新认定，截止2025-06-30"},
            {"idx": 1, "score": 4, "key_facts": ""},
            ...
        ]
        """
    except asyncio.TimeoutError:
        print("[Grader] LLM 超时，降级使用原始排序", flush=True)
        return _fallback_enrich(policies)

    except Exception as e:
        print(f"[Grader] 评分异常（{type(e).__name__}），降级使用原始排序", flush=True)
        return _fallback_enrich(policies)

    # ── 合并评分结果 ─────────────────────────────────────────────
    grade_map = {g["idx"]: g for g in grade_list}

    scored: list[tuple[dict, int]] = []
    for i, p in enumerate(input_batch):
        g = grade_map.get(i, {"score": 5, "key_facts": ""})
        score = g["score"]
        enriched = {
            **p,
            "grader_score": score,
            "graded_key_facts": g.get("key_facts", ""),
        }
        scored.append((enriched, score))

    # ── 过滤：保留 score ≥ GRADE_THRESHOLD ──────────────────────
    passed = [(p, s) for p, s in scored if s >= GRADE_THRESHOLD]

    # ── 保底：至少保留 MIN_KEPT 条 ───────────────────────────────
    if len(passed) < MIN_KEPT:
        sorted_all = sorted(scored, key=lambda x: x[1], reverse=True)
        passed = sorted_all[:MIN_KEPT]

    result = [p for p, _ in passed]

    # ── 如果原始列表超出 MAX_INPUT，追加未评估部分（保持原序）──
    if len(policies) > MAX_INPUT:
        result.extend(_fallback_enrich(policies[MAX_INPUT:]))

    elapsed = int((time.perf_counter() - t0) * 1000)
    filtered_in = len([s for _, s in scored if s >= GRADE_THRESHOLD])
    filtered_out = len(scored) - filtered_in
    print(
        f"[Grader] {len(input_batch)}条 → 通过{filtered_in}条 / 过滤{filtered_out}条"
        f"（≥{GRADE_THRESHOLD}分）→ 最终{len(result)}条 [{elapsed}ms]",
        flush=True,
    )
    return result


def _fallback_enrich(policies: list[dict]) -> list[dict]:
    """降级：为未评分政策注入默认 grader 字段。

    流程：无 grader_score 的条目补 score=5、key_facts=""

    参数：
      policies: 政策列表

    返回：
      list[dict]

    作用：
      LLM 失败或超出批处理上限时，保证下游字段齐全。
    """
    return [
        {**p, "grader_score": 5, "graded_key_facts": ""}
        if "grader_score" not in p else p
        for p in policies
    ]
