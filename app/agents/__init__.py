"""
PolicyRadar Agent 包

本包包含两个独立 Agent，均由 LangGraph 编排，共享 RAG 知识库与配置。

目录：
  report/  报告生成 Agent（选词 → 检索 → 评分 → 验证 → 流式报告）
  chat/    多轮对话 Agent（记忆∥RAG → 提炼 → 主对话 → 可选工具）

完整流程总览写在 app/db/init.py 的 init_db() 函数文档字符串中。
API 入口：
  - 报告：app/api/report.py  → stream_report
  - 对话：app/api/chat.py    → chat_stream
"""
