"""
多轮对话 Agent 子包（app/agents/chat）

模块：
  graph.py    LangGraph 5 节点编排 + chat_stream 入口
  memory.py   窗口清洗 / 摘要压缩 / 上下文提炼
  tools.py    generate_report / search_web
  prompts.py  CHAT_SYSTEM 等 Prompt 常量

对外常用：
  from app.agents.chat.graph import chat_stream, get_graph
"""
