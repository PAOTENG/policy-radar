"""DSPy 模块定义：将报告生成 pipeline 包装为可优化的 DSPy Module
DSPy MIPROv2 会自动优化这些模块的 prompt instructions，实现"harness 学，模型不动"。
"""
import dspy
from app.config import get_settings


def configure_dspy():
    """配置 DSPy 使用 DeepSeek 作为后端 LM"""
    s = get_settings()
    lm = dspy.LM(
        model=f"openai/{s.llm_model}",
        api_key=s.llm_api_key,
        api_base=s.llm_base_url,
        temperature=0.3,
        max_tokens=3000,
    )
    dspy.configure(lm=lm)
    return lm


# ── Signature 定义（DSPy 的"接口规范"）────────────────────────────────────────

class PolicyReportSignature(dspy.Signature):
    """根据检索到的政策信息，为企业生成结构化政策分析报告。
    报告需包含：政策总览表格、重点推荐政策详解、申报时间表、行动建议、信息来源。
    只引用检索到的政策，不编造任何信息。
    """
    keywords_text: str = dspy.InputField(desc="用户选择的关键词（行业/类型/地区）")
    user_description: str = dspy.InputField(desc="用户企业/团队的描述和需求")
    retrieved_policies: str = dspy.InputField(desc="从数据库检索到的政策原文片段")
    report: str = dspy.OutputField(desc="结构化 Markdown 政策分析报告，5个章节")


class PolicyRelevanceSignature(dspy.Signature):
    """评估政策与用户需求的匹配程度"""
    policy_info: str = dspy.InputField(desc="政策详情")
    user_description: str = dspy.InputField(desc="用户企业描述")
    relevance_score: str = dspy.OutputField(
        desc="匹配度分数 0.0-1.0 和一句话理由，格式: {score}|{reason}"
    )


# ── Module 定义（DSPy 的"实现模块"）──────────────────────────────────────────

class PolicyReportModule(dspy.Module):
    """可被 DSPy 优化器优化的报告生成模块"""

    def __init__(self):
        self.generate = dspy.ChainOfThought(PolicyReportSignature)
        self.score_relevance = dspy.Predict(PolicyRelevanceSignature)

    def forward(
        self,
        keywords_text: str,
        user_description: str,
        retrieved_policies: str,
    ) -> dspy.Prediction:
        return self.generate(
            keywords_text=keywords_text,
            user_description=user_description,
            retrieved_policies=retrieved_policies,
        )


# ── 全局模块单例（优化后 load 进来） ────────────────────────────────────────────
_module: PolicyReportModule | None = None
_dspy_configured = False


def get_module() -> PolicyReportModule:
    """获取当前激活的 DSPy 模块（优化后或默认）"""
    global _module, _dspy_configured
    if not _dspy_configured:
        configure_dspy()
        _dspy_configured = True
    if _module is None:
        _module = PolicyReportModule()
    return _module


def load_optimized_module(path: str) -> bool:
    """从文件加载优化后的模块（Reflexion 晋升后调用）"""
    global _module, _dspy_configured
    try:
        if not _dspy_configured:
            configure_dspy()
            _dspy_configured = True
        m = PolicyReportModule()
        m.load(path)
        _module = m
        print(f"[DSPy] 已加载优化模块: {path}", flush=True)
        return True
    except Exception as e:
        print(f"[DSPy] 加载优化模块失败: {e}", flush=True)
        return False


async def generate_with_dspy(
    keywords_text: str,
    user_description: str,
    retrieved_policies: str,
) -> str:
    """使用当前 DSPy 模块生成报告（同步调用包装为异步）"""
    import asyncio
    module = get_module()

    def _run():
        pred = module(
            keywords_text=keywords_text,
            user_description=user_description,
            retrieved_policies=retrieved_policies,
        )
        return pred.report

    return await asyncio.get_running_loop().run_in_executor(None, _run)
