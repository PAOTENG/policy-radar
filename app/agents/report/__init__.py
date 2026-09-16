"""
报告生成 Agent 子包（app/agents/report）

模块：
  graph.py              LangGraph 编排 + stream_report 入口
                        （retrieve → expand_related → grade → verify+gate → format）
  policy_verifier.py    生成前：时效/批内矛盾声明级验证
  citation_verifier.py  生成后：引用名是否在 context 中
  prompts.py            REPORT_SYSTEM / SCORE_SYSTEM（含双条目冲突溯源格式）

图谱与门控（在 app/rag/）：
  policy_graph.py       1 跳补召回
  relation_builder.py   规则/LLM 建边
  conflict_gate.py      五类关联冲突门控

对外常用：
  from app.agents.report.graph import stream_report
"""
