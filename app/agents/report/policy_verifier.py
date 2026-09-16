"""
声明级政策验证器 — Claim-Level Policy Verifier（app/agents/report/policy_verifier.py）

═══════════════════════════════════════════════════════════════════
本文件内容清单
═══════════════════════════════════════════════════════════════════
数据结构（3 个 dataclass）：
  PolicyValidity / PolicyContradiction / VerificationResult

函数（共 7 个）：
  1. _extract_amount_wan              从文本提取补贴金额（万元）
  2. _parse_date                      多格式日期解析为 date
  3. check_validity                   单条政策时效状态判定（规则）
  4. _detect_amount_contradictions    同类政策金额差异矛盾（规则）
  5. _detect_condition_contradictions 同类政策申报条件矛盾（1 次 LLM）
  6. verify_policies                  主入口：三层验证汇总
  7. format_verified_context          把验证结果格式化为 LLM context

设计原则：
  - 时效/金额：纯规则，0 LLM
  - 条件矛盾：最多 1 次 Fast LLM 批量调用
  - 验证发生在报告生成前，结果预注入 context
"""
from __future__ import annotations

import re
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from app.config import get_settings


# ══════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════

@dataclass
class PolicyValidity:
    """单条政策的时效验证结果。"""
    policy_id: int | None
    title: str
    status: Literal["active", "expiring_soon", "expired", "potentially_outdated", "unknown"]
    badge: str      # 显示标签，如 "【已截止】"
    note: str       # 详细说明，注入 context


@dataclass
class PolicyContradiction:
    """两条政策之间在某一维度上的矛盾描述。"""
    dimension: Literal["funding_amount", "deadline", "conditions", "region_scope"]
    policy_a_title: str
    policy_b_title: str
    value_a: str
    value_b: str
    description: str
    severity: Literal["high", "medium", "low"]


@dataclass
class VerificationResult:
    """一批政策的完整验证结果（供 graph 节点序列化）。"""
    validity_map: dict[str, PolicyValidity] = field(default_factory=dict)
    contradictions: list[PolicyContradiction] = field(default_factory=list)
    active_policies: list[dict] = field(default_factory=list)
    expired_policies: list[dict] = field(default_factory=list)
    warning_summary: str = ""
    # 图谱冲突门控字段（由 graph 节点合并 GateResult 后填充）
    gate_status: str = "pass"
    conflict_pairs: list[dict] = field(default_factory=list)
    expanded_ids: list[int] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════════

def _mask_non_money_noise(text: str) -> str:
    """屏蔽易被误认为金额的电话、邮编、文号、日期等噪声。

    业界常见做法：金额抽取必须带货币单位，并先排除电话号等长数字串
    （参见 jionlp extract_money / 货币正则需锚定「元/万」单位）。
    """
    t = text
    # 座机 020-83163946 / 02083163946；手机 1xxxxxxxxxx
    t = re.sub(r"(?<!\d)0\d{2,3}-?\d{7,8}(?!\d)", " ", t)
    t = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", " ", t)
    t = re.sub(r"邮\s*编[：:\s]*\d{6}", " ", t)
    # 公文文号 〔2026〕25号 / [2026]12号
    t = re.sub(r"[〔\[]\d{4}[〕\]]\s*\d+\s*号?", " ", t)
    # 完整日期，避免年份数字干扰
    t = re.sub(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", " ", t)
    t = re.sub(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", " ", t)
    return t


def _extract_amount_wan(text: str | None) -> float:
    """从自由文本中提取金额，统一换算为「万元」。

    流程：
      1. 空文本 → 0
      2. 屏蔽电话/文号/日期噪声
      3. 优先匹配「X亿」→ ×10000
      4. 再匹配「X万/万元」（须带万单位，避免裸数字）
      5. 再匹配「补贴/资助/奖励…X元」→ ÷10000
      6. 多处匹配时取最大值；不再使用「任意 ≥4 位数字」兜底（易把电话当金额）

    参数：
      text: funding_amount 或含金额描述的字符串，可为 None

    返回：
      float：万元；无法解析则为 0.0

    作用：
      为金额矛盾比较提供可运算的数值。
    """
    if not text:
        return 0.0
    cleaned = _mask_non_money_noise(text)

    values: list[float] = []
    for x in re.findall(r"(\d+(?:\.\d+)?)\s*亿", cleaned):
        values.append(float(x) * 10000)
    # 「万」「万元」；排除「万号」「万岁」等偶发误触风险较低，政策文本可接受
    for x in re.findall(r"(\d+(?:\.\d+)?)\s*万(?:元)?", cleaned):
        values.append(float(x))
    # 仅在补贴语义邻近时接受「元」，避免邮编/编号
    for x in re.findall(
        r"(?:补贴|资助|奖励|资助资金|奖补|最高|不超过|额度|奖金)"
        r"[^。；;\d]{0,12}(\d+(?:\.\d+)?)\s*元",
        cleaned,
    ):
        values.append(float(x) / 10000)

    return max(values) if values else 0.0


def _parse_date(val) -> date | None:
    """将多种日期表示统一解析为 datetime.date。

    流程：
      1. None → None
      2. 已是 date → 原样返回
      3. 字符串尝试 %Y-%m-%d / %Y年%m月%d日 / %Y/%m/%d

    参数：
      val: date | str | None（也可能是其它类型则返回 None）

    返回：
      date | None

    作用：
      支撑 deadline / pub_date 的时效计算。
    """
    if val is None:
        return None
    if isinstance(val, date):
        return val
    if isinstance(val, str):
        for fmt in ("%Y-%m-%d", "%Y年%m月%d日", "%Y/%m/%d"):
            try:
                from datetime import datetime
                return datetime.strptime(val.strip(), fmt).date()
            except ValueError:
                continue
    return None


# ══════════════════════════════════════════════════════════════════
# ① 时效验证（纯规则，0 LLM 调用）
# ══════════════════════════════════════════════════════════════════

def check_validity(policy: dict) -> PolicyValidity:
    """判定单条政策的时效状态并生成展示标签。

    流程：
      1. 解析 deadline、pub_date（或 created_at）
      2. 有截止日：已过期 / ≤7天紧急 / ≤30天即将截止
      3. 无截止日但发布时间 >730 天：potentially_outdated
      4. 否则 active

    参数：
      policy: 政策 dict，常用键 id/title/deadline/pub_date/created_at

    返回：
      PolicyValidity（status/badge/note）

    作用：
      在报告中标注【已截止】等，并驱动 expired_policies 分流。
    """
    today = date.today()
    title = policy.get("title", "未知政策")
    pid = policy.get("id")

    deadline = _parse_date(policy.get("deadline"))
    pub_date = _parse_date(policy.get("pub_date") or policy.get("created_at"))

    if deadline:
        days_left = (deadline - today).days
        if days_left < 0:
            return PolicyValidity(
                policy_id=pid, title=title,
                status="expired",
                badge=f"【已截止·{deadline}】",
                note=f"申报截止日期 {deadline} 已过（{-days_left} 天前）。不建议列为重点推荐。",
            )
        elif days_left <= 7:
            return PolicyValidity(
                policy_id=pid, title=title,
                status="expiring_soon",
                badge=f"【紧急·仅剩{days_left}天】",
                note=f"申报截止 {deadline}，仅剩 {days_left} 天，建议立即行动。",
            )
        elif days_left <= 30:
            return PolicyValidity(
                policy_id=pid, title=title,
                status="expiring_soon",
                badge=f"【即将截止·{deadline}】",
                note=f"申报截止 {deadline}，剩余 {days_left} 天，请抓紧准备材料。",
            )

    if pub_date:
        days_since = (today - pub_date).days
        if days_since > 730:
            return PolicyValidity(
                policy_id=pid, title=title,
                status="potentially_outdated",
                badge="【发布较久·建议核实】",
                note=f"此政策于 {pub_date} 发布（已 {days_since // 365} 年），建议前往官网确认是否仍有效。",
            )

    return PolicyValidity(
        policy_id=pid, title=title,
        status="active",
        badge="",
        note="",
    )


# ══════════════════════════════════════════════════════════════════
# ② 矛盾检测
# ══════════════════════════════════════════════════════════════════

def _detect_amount_contradictions(policies: list[dict]) -> list[PolicyContradiction]:
    """按 category_l1 分组比较补贴金额，差异 >50% 记为矛盾。

    流程：
      1. 按 category_l1 分组
      2. 组内提取金额（万元），至少 2 条有效金额才比较
      3. max > min×1.5 → 生成 PolicyContradiction
      4. 差异 >3 倍标 high，否则 medium

    参数：
      policies: 通常为 active 政策列表

    返回：
      list[PolicyContradiction]

    作用：
      无 LLM 成本下发现「同主题补贴数字悬殊」的风险点。
    """
    contradictions = []
    groups: dict[str, list[dict]] = {}
    for p in policies:
        cat = p.get("category_l1") or "其他"
        groups.setdefault(cat, []).append(p)

    for cat, group in groups.items():
        if len(group) < 2:
            continue

        with_amount = [
            (p, _extract_amount_wan(p.get("funding_amount", "")))
            for p in group
        ]
        with_amount = [(p, a) for p, a in with_amount if a > 0]

        if len(with_amount) < 2:
            continue

        max_p, max_a = max(with_amount, key=lambda x: x[1])
        min_p, min_a = min(with_amount, key=lambda x: x[1])

        if max_a > min_a * 1.5:
            contradictions.append(PolicyContradiction(
                dimension="funding_amount",
                policy_a_title=max_p.get("title", "")[:30],
                policy_b_title=min_p.get("title", "")[:30],
                value_a=f"{max_a:.0f}万元",
                value_b=f"{min_a:.0f}万元",
                description=(
                    f"【补贴金额差异】同为「{cat}」类，"
                    f"《{max_p.get('title','')[:20]}》补贴最高 {max_a:.0f} 万元，"
                    f"而《{min_p.get('title','')[:20]}》为 {min_a:.0f} 万元，"
                    f"相差 {(max_a/min_a - 1)*100:.0f}%。"
                    "可能适用于不同规模或阶段的企业，请核实匹配条件。"
                ),
                severity="high" if max_a > min_a * 3 else "medium",
            ))

    return contradictions


async def _detect_condition_contradictions(
    policies: list[dict],
) -> list[PolicyContradiction]:
    """用 1 次 Fast LLM 批量检测申报条件上的实质性矛盾。

    流程：
      1. 筛有 key_conditions 的政策，按 category_l1 分组（≥2 条）
      2. 最多取 3 组×3 条拼成紧凑 prompt
      3. temperature=0 调用 Fast LLM，要求输出 JSON 列表
      4. 解析失败或异常 → 返回空列表（不阻断主流程）

    参数：
      policies: 通常为 active 政策列表

    返回：
      list[PolicyContradiction]，dimension="conditions"

    作用：
      发现「同一类政策对注册资本/成立年限等要求不一致」等问题。
    """
    groups: dict[str, list[dict]] = {}
    for p in policies:
        if not p.get("key_conditions"):
            continue
        cat = p.get("category_l1") or "其他"
        groups.setdefault(cat, []).append(p)

    candidates = {k: v for k, v in groups.items() if len(v) >= 2}
    if not candidates:
        return []

    policy_descriptions = []
    for cat, group in list(candidates.items())[:3]:
        for p in group[:3]:
            policy_descriptions.append(
                f"[{cat}] 《{p.get('title','')[:25]}》：{p.get('key_conditions','')[:150]}"
            )

    if not policy_descriptions:
        return []

    prompt_content = "\n".join(policy_descriptions)

    s = get_settings()
    llm = ChatOpenAI(
        model=s.fast_llm_model,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        temperature=0,
    )

    system = """你是政策对比专家。分析以下同类政策的申报条件，找出实质性矛盾（不同政策对同一要求有不同规定）。

输出 JSON 列表，每个矛盾：
{"policy_a": "简短标题", "policy_b": "简短标题", "conflict": "矛盾说明（30字内）", "severity": "high/medium/low"}

没有矛盾时输出空列表 []。只输出 JSON，不要其他内容。"""

    try:
        resp = await llm.ainvoke([
            SystemMessage(content=system),
            HumanMessage(content=f"政策条件列表：\n{prompt_content}"),
        ])
        raw = resp.content.strip()
        raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
        items = json.loads(raw)
        contradictions = []
        for item in items:
            if not isinstance(item, dict):
                continue
            contradictions.append(PolicyContradiction(
                dimension="conditions",
                policy_a_title=str(item.get("policy_a", ""))[:30],
                policy_b_title=str(item.get("policy_b", ""))[:30],
                value_a="见原文",
                value_b="见原文",
                description=f"【申报条件矛盾】{item.get('conflict', '')}",
                severity=item.get("severity", "medium"),
            ))
        return contradictions
    except Exception as e:
        print(f"[PolicyVerifier] 条件矛盾检测 LLM 调用失败: {e}", flush=True)
        return []


# ══════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════

async def verify_policies(policies: list[dict]) -> VerificationResult:
    """对一批候选政策执行完整三层验证。

    流程：
      1. 空列表 → 空 VerificationResult
      2. 对每条 check_validity → 写入 validity_map，分流 active/expired
      3. 对 active 做金额矛盾检测
      4. 对 active 做条件矛盾检测（async LLM）
      5. 拼 warning_summary（过期条数、高优矛盾提示）

    参数：
      policies: 政策 dict 列表（通常为 graded 后的 3–12 条）

    返回：
      VerificationResult

    作用：
      报告图 verify_policies_node 的唯一业务入口。
    """
    if not policies:
        return VerificationResult()

    result = VerificationResult()

    for p in policies:
        v = check_validity(p)
        result.validity_map[p.get("title", "")] = v
        if v.status == "expired":
            result.expired_policies.append(p)
        else:
            result.active_policies.append(p)

    amount_conflicts = _detect_amount_contradictions(result.active_policies)
    result.contradictions.extend(amount_conflicts)

    condition_conflicts = await _detect_condition_contradictions(result.active_policies)
    result.contradictions.extend(condition_conflicts)

    warnings = []
    expired_count = len(result.expired_policies)
    if expired_count:
        titles = "、".join(
            p.get("title", "未知")[:15] for p in result.expired_policies[:3]
        )
        warnings.append(f"⚠️ **{expired_count} 条政策已截止申报**（{titles}等），已在下方标注，不列为重点推荐。")

    high_conflicts = [c for c in result.contradictions if c.severity == "high"]
    if high_conflicts:
        warnings.append(
            f"⚠️ **检测到 {len(high_conflicts)} 处高优先级政策矛盾**，"
            "不同政策对同一问题规定不一致，已在对应部分详细标注，请企业依据自身情况核实适用的具体标准。"
        )
    elif result.contradictions:
        warnings.append(
            f"ℹ️ 检测到 {len(result.contradictions)} 处政策差异，已在报告中标注。"
        )

    result.warning_summary = "\n".join(warnings)

    print(
        f"[PolicyVerifier] 验证完成: active={len(result.active_policies)} "
        f"expired={expired_count} contradictions={len(result.contradictions)}",
        flush=True,
    )
    return result


def format_verified_context(
    policies: list[dict],
    result: VerificationResult,
) -> str:
    """将验证结果与政策列表格式化为可注入 LLM 的 context 文本。

    流程：
      1. 若有矛盾 → 先写「政策矛盾预警」段落
      2. 逐条写政策：badge + 标题 + 核心摘要(graded_key_facts) + 元数据 + 时效提示
      3. 用分隔线连接各段

    参数：
      policies: 与验证时同一批政策（含可选 graded_key_facts）
      result:   verify_policies 的返回值

    返回：
      str：带标注的长文本 context

    作用：
      供 format_context_node / verification["_formatted_context"] 直接使用。
    """
    sections = []

    if result.contradictions:
        conflict_lines = [f"  - {c.description}" for c in result.contradictions]
        sections.append(
            "【⚠️ 政策矛盾预警 - 请在报告中明确标注以下差异】\n"
            + "\n".join(conflict_lines)
        )

    # 门控未通过时：前置双条目冲突对照说明
    if result.gate_status == "conflict" and result.conflict_pairs:
        dual_lines = ["【🔗 关联冲突双条目对照 — 生成报告时必须保留双方并标注】"]
        for cp in result.conflict_pairs[:8]:
            if isinstance(cp, dict):
                dual_lines.append(
                    f"  - 类型={cp.get('conflict_type')} | "
                    f"《{str(cp.get('policy_a_title', ''))[:20]}》 vs "
                    f"《{str(cp.get('policy_b_title', ''))[:20]}》 | "
                    f"{cp.get('description', '')[:100]}"
                    + (f" | 建议：{cp.get('advice', '')}" if cp.get("advice") else "")
                )
            else:
                dual_lines.append(f"  - {cp}")
        sections.append("\n".join(dual_lines))

    active_lines = []
    for i, p in enumerate(policies, 1):
        title = p.get("title", "未知")
        v = result.validity_map.get(title)
        badge = v.badge if v else ""
        validity_note = v.note if v and v.note else ""

        deadline = str(p.get("deadline") or "未注明")
        expand_tag = ""
        if p.get("from_graph_expand"):
            expand_tag = f"【图谱补召·{p.get('expand_relation', '')}】"

        lines = [
            f"[政策{i}] {badge}{expand_tag}《{title}》",
            f"  发布单位：{p.get('publisher', '未知')}  地区：{p.get('region', '全国')}",
            f"  申报截止：{deadline}  补贴金额：{p.get('funding_amount', '未注明')}",
            f"  核心条件：{p.get('key_conditions', '') or '见正文'}",
            f"  摘要：{p.get('summary', '') or '（暂无摘要）'}",
            f"  来源：{p.get('source_url', '') or '内部数据库'}",
        ]
        graded_facts = p.get("graded_key_facts", "")
        if graded_facts:
            lines.insert(1, f"  🎯 核心摘要：{graded_facts}")
        if validity_note:
            lines.append(f"  ⏰ 时效提示：{validity_note}")
        if p.get("status") == "expired" or (v and v.status == "expired"):
            lines.append("  ❌ 此政策已截止，不建议列为重点推荐")
        # 冲突门控附注（双条目溯源）
        for note in (p.get("conflict_notes") or [])[:3]:
            lines.append(f"  {note}")
        active_lines.append("\n".join(lines))

    if active_lines:
        sections.append("\n\n".join(active_lines))

    return "\n\n" + "─" * 50 + "\n\n".join(sections)
