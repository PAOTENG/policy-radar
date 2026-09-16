"""
对话 Agent 工具定义（app/agents/chat/tools.py）

═══════════════════════════════════════════════════════════════════
本文件函数清单（共 2 个工具函数 + 1 个列表常量）
═══════════════════════════════════════════════════════════════════
  1. generate_report   调用报告 Agent，生成完整 Markdown 报告
  2. search_web        Tavily 联网搜索最新政策/资讯
  3. TOOLS             供主 LLM bind_tools / ToolNode 使用的工具列表

调用关系：
  chat_node（主 LLM）→ tool_calls → tools_node → 本文件函数 → 结果回 chat_node
"""
from langchain_core.tools import tool


@tool
async def generate_report(
    keywords_json: str,
    user_description: str = "",
    module: str = "policy",
) -> str:
    """生成结构化政策分析报告（内部复用报告 Agent 全流水线）。

    流程：
      1. 解析 keywords_json 为 dict（失败则 {}）
      2. 异步迭代 stream_report(keywords, user_description, module)
      3. 拼接全部 token 为完整 Markdown 字符串返回

    参数：
      keywords_json: JSON 字符串，如 {"l1":"科技创新","l2":"...","region":"广东"}
      user_description: 企业/团队补充描述，默认 ""
      module: 模块类型，默认 "policy"（预留 competition/school）

    返回：
      str：完整 Markdown 报告；异常时由上游 ToolMessage 承载错误信息

    作用：
      让对话 Agent 在用户明确要求「出报告」时，无缝调用报告 Agent，
      而不复制一套检索/验证逻辑。
    """
    import json
    from app.agents.report.graph import stream_report

    try:
        keywords = json.loads(keywords_json) if isinstance(keywords_json, str) else keywords_json
    except Exception:
        keywords = {}

    chunks = []
    async for chunk in stream_report(keywords, user_description, module):
        chunks.append(chunk)
    return "".join(chunks)


@tool
async def search_web(query: str) -> str:
    """搜索最新政策动态、竞赛信息等公开网络内容。

    流程：
      1. 检查 Settings.tavily_api_key；未配置则返回提示文案
      2. TavilyClient.search(query, max_results=5)
      3. 将 title/content/url 格式化为 Markdown 片段拼接

    参数：
      query: 搜索查询（中文或英文）

    返回：
      str：多条结果摘要，或「未配置 / 失败 / 无结果」说明

    作用：
      补充库内 RAG 未覆盖的时效性资讯（库外信息需谨慎引用）。
    """
    from app.config import get_settings
    s = get_settings()

    if not s.tavily_api_key:
        return "【联网搜索未配置，请联系管理员添加 TAVILY_API_KEY】"

    try:
        from tavily import TavilyClient
        client = TavilyClient(api_key=s.tavily_api_key)
        result = client.search(query, max_results=5, search_depth="basic")
        results_text = []
        for r in result.get("results", []):
            results_text.append(
                f"**{r.get('title', '')}**\n{r.get('content', '')}\n来源：{r.get('url', '')}"
            )
        return "\n\n---\n\n".join(results_text) or "未找到相关结果"
    except Exception as e:
        return f"搜索失败: {str(e)}"


TOOLS = [generate_report, search_web]
