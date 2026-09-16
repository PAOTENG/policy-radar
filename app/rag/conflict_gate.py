"""
关联对五类冲突门控（app/rag/conflict_gate.py）

═══════════════════════════════════════════════════════════════════
本文件职责
═══════════════════════════════════════════════════════════════════
  在图谱补召回之后，对「有关系边的政策对」做 5 类冲突验证：
    1. deadline_conflict       截止时间冲突
    2. funding_amount_conflict 政策金额冲突（同级）/ funding_hierarchy_diff（层级差异）
    3. eligibility_conflict    主体门槛冲突（注册资本/年限/营收/纳税/高新等）
    4. legal_validity_conflict 效力冲突（废止/有效期/supersedes）
    5. exclusivity_conflict    互斥/不可重复申报

门控语义：
  - 五类均未触发 → gate_status=pass
  - 任一类触发   → gate_status=conflict：双方保留 + chunk 附注 + 可选全文定位

函数清单：
  1. extract_slots                 从字段/正文抽比较槽位
  2. check_pair_conflicts          单对五类规则检测
  3. locate_conflict_fulltext      LLM 对照全文定位冲突句
  4. annotate_policies_with_conflicts  给政策/chunk 打冲突附注
  5. run_conflict_gate             主入口（供报告图 verify 节点调用）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from typing import Literal

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from app.config import get_settings
from app.agents.report.policy_verifier import (
    _extract_amount_wan,
    _parse_date,
    check_validity,
)


ConflictType = Literal[
    "deadline_conflict",
    "funding_amount_conflict",
    "funding_hierarchy_diff",
    "eligibility_conflict",
    "legal_validity_conflict",
    "exclusivity_conflict",
]

HIERARCHY_RELATIONS = frozenset({"implements", "region_child", "complements", "funding_of"})


@dataclass
class ConflictPair:
    """一条关联对上的冲突记录。"""
    conflict_type: str
    policy_a_id: int | None
    policy_b_id: int | None
    policy_a_title: str
    policy_b_title: str
    relation_type: str
    value_a: str
    value_b: str
    description: str
    severity: Literal["high", "medium", "low"]
    evidence_a: str = ""
    evidence_b: str = ""
    advice: str = ""


@dataclass
class GateResult:
    """冲突门控汇总结果。"""
    gate_status: Literal["pass", "conflict"] = "pass"
    conflict_pairs: list[ConflictPair] = field(default_factory=list)
    annotated_policies: list[dict] = field(default_factory=list)
    warning_summary: str = ""

    def to_meta(self) -> dict:
        """序列化为 verification_meta / out_meta 可用的 dict。"""
        return {
            "gate_status": self.gate_status,
            "conflict_pairs": [asdict(c) for c in self.conflict_pairs],
            "conflict_count": len(self.conflict_pairs),
            "warning_summary": self.warning_summary,
        }


# ── 槽位抽取 ─────────────────────────────────────────────────────

def extract_slots(policy: dict) -> dict:
    """从结构化字段 + 正文片段抽取可比较槽位。

    流程：
      1. 金额：funding_amount → 正文「万元/亿」
      2. 截止：deadline 列 → 正文「截止.*日期」
      3. 效力：废止/有效期关键词
      4. 门槛：注册资本/成立/营收/纳税/高新
      5. 互斥：不得重复/同一项目/就高

    参数：
      policy: 含 funding_amount/deadline/key_conditions/full_text/matched_chunk

    返回：
      dict 槽位（供 check_pair_conflicts 使用）
    """
    text = " ".join(filter(None, [
        str(policy.get("funding_amount") or ""),
        str(policy.get("key_conditions") or ""),
        str(policy.get("matched_chunk") or ""),
        str(policy.get("full_text") or "")[:3000],
        str(policy.get("summary") or ""),
    ]))

    amount = _extract_amount_wan(policy.get("funding_amount") or "")
    if amount <= 0:
        amount = _extract_amount_wan(text)

    deadline = _parse_date(policy.get("deadline"))
    if deadline is None:
        m = re.search(
            r"(?:申报)?截止(?:日期|时间)?[：:\s]*(\d{4}[-年/]\d{1,2}[-月/]\d{1,2})",
            text,
        )
        if m:
            deadline = _parse_date(m.group(1).replace("年", "-").replace("月", "-").replace("日", ""))

    abolished = bool(re.search(r"废止|失效|自行失效|不再执行", text))
    validity_m = re.search(r"有效期[至到]?\s*(\d{4}[-年/]\d{1,2}[-月/]\d{1,2})?", text)
    has_validity_clause = bool(validity_m or re.search(r"有效期", text))

    # 门槛：只抽「带可比较数值」的条件；不把单纯出现「高新技术企业」当门槛
    # （公示/公告标题里高频出现，会导致同项目不同环节假阳性）
    eligibility_hits = _extract_eligibility_hits(text)

    exclusivity = bool(re.search(
        r"不得重复|不可重复|同一项目|就高不就低|不得同时享受|禁止叠加",
        text,
    ))
    exclusivity_snip = ""
    m = re.search(r".{0,20}(?:不得重复|同一项目|就高不就低|不得同时享受).{0,30}", text)
    if m:
        exclusivity_snip = m.group(0)

    return {
        "amount_wan": amount,
        "deadline": deadline,
        "abolished": abolished,
        "has_validity_clause": has_validity_clause,
        "eligibility_hits": eligibility_hits,
        "exclusivity": exclusivity,
        "exclusivity_snip": exclusivity_snip,
        "title": policy.get("title") or "",
        "id": policy.get("id"),
        "region": policy.get("region") or "",
    }


def _extract_eligibility_hits(text: str) -> list[str]:
    """抽取带数值的申报门槛片段（label:snippet:num）。

    仅保留可比较维度：注册资本 / 成立年限 / 营业收入 / 纳税额。
    「高新技术企业」「专精特新」等资格称谓单独出现不算门槛冲突。
    """
    patterns = [
        ("注册资本", r"注册资本[^。；;\n]{0,40}?(\d+(?:\.\d+)?)\s*(?:万|亿)?元?"),
        ("成立年限", r"(?:成立|注册).{0,10}(?:满|不少于|以上)\s*(\d+)\s*年"),
        ("营业收入", r"(?:营业|销售)收入[^。；;\n]{0,40}?(\d+(?:\.\d+)?)\s*(?:万|亿)?元?"),
        ("纳税", r"(?:纳税|上缴税金|实缴税款)[^。；;\n]{0,40}?(\d+(?:\.\d+)?)\s*(?:万|亿)?元?"),
    ]
    hits: list[str] = []
    for label, pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        num = m.group(1)
        snip = m.group(0)[:50].replace("\n", " ")
        hits.append(f"{label}:{snip}:{num}")
    return hits


def _eligibility_conflict_dims(hits_a: list[str], hits_b: list[str]) -> list[str]:
    """同维度数值不同才算门槛冲突；仅片段文字不同不算。"""
    def _parse(hits: list[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for h in hits:
            parts = h.split(":")
            if len(parts) < 3:
                continue
            label, num = parts[0], parts[-1]
            out[label] = num
        return out

    map_a, map_b = _parse(hits_a), _parse(hits_b)
    diffs = []
    for dim in set(map_a) & set(map_b):
        try:
            if abs(float(map_a[dim]) - float(map_b[dim])) > 1e-6:
                diffs.append(dim)
        except ValueError:
            if map_a[dim] != map_b[dim]:
                diffs.append(dim)
    return diffs


def check_pair_conflicts(
    policy_a: dict,
    policy_b: dict,
    relation_type: str,
) -> list[ConflictPair]:
    """对单对关联政策跑 5 类规则冲突检测。

    参数：
      policy_a / policy_b: 政策 dict
      relation_type:       边上的关系类型

    返回：
      list[ConflictPair]（可能为空）
    """
    sa, sb = extract_slots(policy_a), extract_slots(policy_b)
    out: list[ConflictPair] = []
    title_a = (policy_a.get("title") or "")[:40]
    title_b = (policy_b.get("title") or "")[:40]
    id_a, id_b = policy_a.get("id"), policy_b.get("id")

    # 1) 截止时间冲突
    if sa["deadline"] and sb["deadline"] and sa["deadline"] != sb["deadline"]:
        days = abs((sa["deadline"] - sb["deadline"]).days)
        if days >= 1:
            out.append(ConflictPair(
                conflict_type="deadline_conflict",
                policy_a_id=id_a, policy_b_id=id_b,
                policy_a_title=title_a, policy_b_title=title_b,
                relation_type=relation_type,
                value_a=str(sa["deadline"]), value_b=str(sb["deadline"]),
                description=(
                    f"【截止时间冲突】关联政策申报截止不一致："
                    f"《{title_a}》为 {sa['deadline']}，"
                    f"《{title_b}》为 {sb['deadline']}（相差 {days} 天）。"
                ),
                severity="high" if days >= 7 else "medium",
                advice="建议以本级主管机关最新通知截止日为准，并交叉核对官网。",
            ))

    # 一方已过期、另一方仍有效（结合 validity）
    va, vb = check_validity(policy_a), check_validity(policy_b)
    if {va.status, vb.status} >= {"expired", "active"} or (
        va.status == "expired" and vb.status not in ("expired",)
    ) or (vb.status == "expired" and va.status not in ("expired",)):
        if va.status != vb.status:
            out.append(ConflictPair(
                conflict_type="deadline_conflict",
                policy_a_id=id_a, policy_b_id=id_b,
                policy_a_title=title_a, policy_b_title=title_b,
                relation_type=relation_type,
                value_a=va.status, value_b=vb.status,
                description=(
                    f"【时效状态冲突】《{title_a}》状态={va.badge or va.status}，"
                    f"《{title_b}》状态={vb.badge or vb.status}。"
                ),
                severity="high",
                advice="勿将已截止文与有效文并列作为可申报选项；保留对照说明。",
            ))

    # 2) 金额：同级冲突 vs 层级差异
    if sa["amount_wan"] > 0 and sb["amount_wan"] > 0:
        hi, lo = max(sa["amount_wan"], sb["amount_wan"]), min(sa["amount_wan"], sb["amount_wan"])
        if hi > lo * 1.5:
            if relation_type in HIERARCHY_RELATIONS:
                out.append(ConflictPair(
                    conflict_type="funding_hierarchy_diff",
                    policy_a_id=id_a, policy_b_id=id_b,
                    policy_a_title=title_a, policy_b_title=title_b,
                    relation_type=relation_type,
                    value_a=f"{sa['amount_wan']:.0f}万元",
                    value_b=f"{sb['amount_wan']:.0f}万元",
                    description=(
                        f"【层级金额差异】关系={relation_type}，"
                        f"《{title_a}》约 {sa['amount_wan']:.0f} 万元，"
                        f"《{title_b}》约 {sb['amount_wan']:.0f} 万元。"
                        "常见于省市分级补贴，不一定是错误，但需向企业说明适用层级。"
                    ),
                    severity="medium",
                    advice="通常市级实施细则金额可低于省级上限；按企业注册地适用文件执行。",
                ))
            else:
                out.append(ConflictPair(
                    conflict_type="funding_amount_conflict",
                    policy_a_id=id_a, policy_b_id=id_b,
                    policy_a_title=title_a, policy_b_title=title_b,
                    relation_type=relation_type,
                    value_a=f"{sa['amount_wan']:.0f}万元",
                    value_b=f"{sb['amount_wan']:.0f}万元",
                    description=(
                        f"【政策金额冲突】同项目/关联文件金额不一致："
                        f"{sa['amount_wan']:.0f} 万 vs {sb['amount_wan']:.0f} 万。"
                    ),
                    severity="high" if hi > lo * 3 else "medium",
                    advice="核对是否适用不同档次/年度；勿直接取较大值承诺企业。",
                ))

    # 3) 主体门槛冲突：同维度且数值不同（忽略纯资格称谓差异）
    diffs = _eligibility_conflict_dims(sa["eligibility_hits"], sb["eligibility_hits"])
    if diffs:
        out.append(ConflictPair(
            conflict_type="eligibility_conflict",
            policy_a_id=id_a, policy_b_id=id_b,
            policy_a_title=title_a, policy_b_title=title_b,
            relation_type=relation_type,
            value_a="; ".join(sa["eligibility_hits"][:3]),
            value_b="; ".join(sb["eligibility_hits"][:3]),
            description=(
                f"【主体门槛冲突】在 {('、'.join(diffs))} 等维度上，"
                f"关联政策数值要求不一致，错报风险高。"
            ),
            severity="high",
            evidence_a="; ".join(sa["eligibility_hits"][:3]),
            evidence_b="; ".join(sb["eligibility_hits"][:3]),
            advice="以企业所在地现行申报通知为准，逐条核对门槛，勿混用省市条件。",
        ))

    # 4) 效力冲突
    if sa["abolished"] != sb["abolished"] or relation_type == "supersedes":
        if sa["abolished"] or sb["abolished"] or relation_type == "supersedes":
            out.append(ConflictPair(
                conflict_type="legal_validity_conflict",
                policy_a_id=id_a, policy_b_id=id_b,
                policy_a_title=title_a, policy_b_title=title_b,
                relation_type=relation_type,
                value_a="废止/被替代" if sa["abolished"] or relation_type == "supersedes" else "现行",
                value_b="废止/被替代" if sb["abolished"] else "现行",
                description=(
                    f"【效力冲突】关联文件存在废止/替代关系或一方正文含废止表述"
                    f"（relation={relation_type}）。"
                ),
                severity="high",
                advice="优先采信 supersedes 指向的新文；旧文仅作历史对照，不作为可申报依据。",
            ))

    # 5) 互斥 / 不可重复
    if sa["exclusivity"] or sb["exclusivity"]:
        out.append(ConflictPair(
            conflict_type="exclusivity_conflict",
            policy_a_id=id_a, policy_b_id=id_b,
            policy_a_title=title_a, policy_b_title=title_b,
            relation_type=relation_type,
            value_a=sa["exclusivity_snip"] or ("有互斥条款" if sa["exclusivity"] else "未见"),
            value_b=sb["exclusivity_snip"] or ("有互斥条款" if sb["exclusivity"] else "未见"),
            description=(
                "【互斥/不可重复申报】关联政策含「不得重复/同一项目/就高」等条款，"
                "不可默认叠加享受。"
            ),
            severity="high",
            advice="向企业说明通常就高不就低或择一申报；叠加前需书面确认主管机关口径。",
        ))

    return out


async def locate_conflict_fulltext(
    policy_a: dict,
    policy_b: dict,
    conflict: ConflictPair,
) -> ConflictPair:
    """用 1 次 Fast LLM 从两篇正文中定位冲突原句，回填 evidence_a/b。

    失败时原样返回 conflict（不阻断门控）。
    """
    text_a = (policy_a.get("full_text") or policy_a.get("matched_chunk") or "")[:2500]
    text_b = (policy_b.get("full_text") or policy_b.get("matched_chunk") or "")[:2500]
    if not text_a or not text_b:
        return conflict

    s = get_settings()
    llm = ChatOpenAI(
        model=s.fast_llm_model,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        temperature=0,
    )
    system = """你是政策冲突溯源助手。根据冲突类型，从两篇政策原文中各摘 1 句最能体现冲突的原文。
输出 JSON：{"evidence_a":"...","evidence_b":"...","advice":"给企业的选择建议≤40字"}
只输出 JSON。"""
    human = (
        f"冲突类型：{conflict.conflict_type}\n"
        f"说明：{conflict.description}\n\n"
        f"政策A《{conflict.policy_a_title}》：\n{text_a}\n\n"
        f"政策B《{conflict.policy_b_title}》：\n{text_b}"
    )
    try:
        resp = await llm.ainvoke([
            SystemMessage(content=system),
            HumanMessage(content=human),
        ])
        raw = re.sub(r"```json\s*|\s*```", "", (resp.content or "").strip())
        data = json.loads(raw)
        conflict.evidence_a = str(data.get("evidence_a", ""))[:200]
        conflict.evidence_b = str(data.get("evidence_b", ""))[:200]
        if data.get("advice"):
            conflict.advice = str(data["advice"])[:80]
    except Exception as e:
        print(f"[ConflictGate] 全文定位失败: {e}", flush=True)
    return conflict


def annotate_policies_with_conflicts(
    policies: list[dict],
    conflicts: list[ConflictPair],
) -> list[dict]:
    """给政策条目附加冲突附注（写入 conflict_notes，并拼到 matched_chunk 末尾）。

    流程：
      1. 按政策 id/title 聚合涉及该条的冲突
      2. 生成「已验证溯源但与条目N冲突」风格短注
      3. 回写 policy['conflict_notes'] / 追加 matched_chunk
    """
    # 序号映射
    id_to_idx = {}
    for i, p in enumerate(policies, 1):
        if p.get("id") is not None:
            id_to_idx[p["id"]] = i

    notes_map: dict[int, list[str]] = {}
    for c in conflicts:
        for pid, other_pid, other_title in (
            (c.policy_a_id, c.policy_b_id, c.policy_b_title),
            (c.policy_b_id, c.policy_a_id, c.policy_a_title),
        ):
            if pid is None:
                continue
            other_idx = id_to_idx.get(other_pid, "?")
            line = (
                f"⚠️ 已验证溯源，但与条目{other_idx}《{other_title[:20]}》存在"
                f"「{c.conflict_type}」：{c.description[:80]}"
                + (f" 建议：{c.advice}" if c.advice else "")
            )
            notes_map.setdefault(pid, []).append(line)

    annotated = []
    for p in policies:
        item = dict(p)
        pid = item.get("id")
        notes = notes_map.get(pid, []) if pid is not None else []
        item["conflict_notes"] = notes
        if notes:
            chunk = item.get("matched_chunk") or item.get("summary") or ""
            item["matched_chunk"] = (
                chunk + "\n\n【冲突门控附注】\n" + "\n".join(notes[:3])
            )
        annotated.append(item)
    return annotated


async def run_conflict_gate(
    policies: list[dict],
    edges_used: list[dict] | None = None,
    *,
    locate_fulltext: bool = True,
    max_locate: int = 3,
) -> GateResult:
    """冲突门控主入口。

    流程：
      1. 若有 edges_used：只对边上的政策对检测；否则对列表两两（上限组合）检测
      2. 汇总 ConflictPair，去重
      3. 对前 max_locate 条 high/medium 冲突做全文定位
      4. annotate_policies；拼 warning_summary
      5. gate_status = conflict if 有 pair else pass

    参数：
      policies:         扩展+评分后的政策列表
      edges_used:       expand_related 返回的边（含 relation_type）
      locate_fulltext:  是否调用 LLM 定位原文
      max_locate:       最多全文定位条数（控延迟）

    返回：
      GateResult
    """
    if not policies:
        return GateResult(gate_status="pass", annotated_policies=[])

    by_id = {p["id"]: p for p in policies if p.get("id") is not None}
    conflicts: list[ConflictPair] = []
    seen: set[tuple] = set()

    def _add_pairs(a: dict, b: dict, rel: str):
        for c in check_pair_conflicts(a, b, rel):
            key = (c.conflict_type, c.policy_a_id, c.policy_b_id, c.value_a, c.value_b)
            key2 = (c.conflict_type, c.policy_b_id, c.policy_a_id, c.value_b, c.value_a)
            if key in seen or key2 in seen:
                continue
            seen.add(key)
            conflicts.append(c)

    if edges_used:
        for e in edges_used:
            a = by_id.get(e.get("source_policy_id"))
            b = by_id.get(e.get("target_policy_id"))
            if a and b:
                _add_pairs(a, b, e.get("relation_type") or "same_program")
    else:
        # 无边时：同 category_l1 两两比较（最多前 6 条）
        limited = policies[:6]
        for i, a in enumerate(limited):
            for b in limited[i + 1:]:
                if (a.get("category_l1") or "") != (b.get("category_l1") or ""):
                    continue
                _add_pairs(a, b, "same_program")

    # 全文定位（优先 high）
    if locate_fulltext and conflicts:
        ranked = sorted(
            conflicts,
            key=lambda c: {"high": 0, "medium": 1, "low": 2}.get(c.severity, 3),
        )
        located: list[ConflictPair] = []
        for i, c in enumerate(ranked):
            if i < max_locate and c.severity in ("high", "medium"):
                a = by_id.get(c.policy_a_id) or {}
                b = by_id.get(c.policy_b_id) or {}
                c = await locate_conflict_fulltext(a, b, c)
            located.append(c)
        # 保持原序：按 conflict_type 稳定输出
        conflicts = located

    annotated = annotate_policies_with_conflicts(policies, conflicts)

    warnings = []
    if conflicts:
        high_n = sum(1 for c in conflicts if c.severity == "high")
        warnings.append(
            f"⚠️ **冲突门控未通过（gate=conflict）**：检测到 {len(conflicts)} 处关联冲突"
            f"（高优先级 {high_n}）。双方政策均已保留，请在报告中双条目对照，"
            "并写明「已验证溯源但存在冲突」与选择建议。"
        )
        for c in conflicts[:5]:
            warnings.append(f"  - {c.description}")

    result = GateResult(
        gate_status="conflict" if conflicts else "pass",
        conflict_pairs=conflicts,
        annotated_policies=annotated,
        warning_summary="\n".join(warnings),
    )
    print(
        f"[ConflictGate] status={result.gate_status} "
        f"conflicts={len(conflicts)} policies={len(annotated)}",
        flush=True,
    )
    return result
