"""
引用幻觉防御 — Citation Verifier（app/agents/report/citation_verifier.py）

═══════════════════════════════════════════════════════════════════
本文件函数清单（共 1 个）
═══════════════════════════════════════════════════════════════════
  1. verify_citations
     事后校验报告中《政策名》/【政策名】是否出现在 RAG context；
     无据则替换为「待核实」并追加说明。纯规则，无 LLM，耗时通常 <1ms。

在流水线中的位置：
  stream_report 流式生成完成后调用 → 必要时再 yield 溯源后缀。
"""
import re


# 匹配 《政策名》 或 【政策名】 格式的引用（2–40 字）
_CITATION_RE = re.compile(r"[《【]([^》】]{2,40})[》】]")


def verify_citations(report: str, rag_context: str) -> tuple[str, list[str]]:
    """校验报告内政策引用是否在检索上下文中有据可查。

    流程：
      1. 正则提取全部 《…》/【…】 引用名
      2. 无引用或无 context → 原样返回
      3. 对每个唯一名称：若全名与前 4 字均不在 context → 记为幻觉
      4. 将幻觉引用替换为 [名称·待核实]
      5. 若有幻觉，追加 Markdown 溯源说明段落

    参数：
      report:      LLM 生成的完整报告文本
      rag_context: format_context_node 产出的 context_text

    返回：
      (verified_report, hallucinated_list)
      - verified_report: 可能被改写并追加说明的报告
      - hallucinated_list: 未在 context 中找到的引用名称列表

    作用：
      生成后保底防线；与生成前的 policy_verifier 互补，不阻断流式体验。
    """
    cited = _CITATION_RE.findall(report)
    if not cited or not rag_context:
        return report, []

    hallucinated: list[str] = []
    verified_report = report

    for name in set(cited):
        short = name[:4] if len(name) >= 4 else name
        if short not in rag_context and name not in rag_context:
            hallucinated.append(name)
            verified_report = verified_report.replace(
                f"《{name}》", f"[{name}·待核实]"
            ).replace(
                f"【{name}】", f"[{name}·待核实]"
            )

    if hallucinated:
        notice = (
            "\n\n---\n> **引用溯源说明**：以下引用未能在当前检索到的政策原文中找到对应原文，"
            f"已标注为「待核实」，建议人工复核：{', '.join(hallucinated)}"
        )
        verified_report += notice
        print(
            f"[CitationVerifier] 发现 {len(hallucinated)} 处待核实引用: {hallucinated}",
            flush=True,
        )

    return verified_report, hallucinated
