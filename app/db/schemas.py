"""Pydantic 数据模型"""
from pydantic import BaseModel, Field
from typing import Optional
from datetime import date, datetime


# ── 分类 ──────────────────────────────────────────────────────
class CategoryNode(BaseModel):
    id: int
    code: str
    label: str
    icon: Optional[str] = None
    level: int
    policy_count: int = 0
    children: list["CategoryNode"] = []


# ── 政策 ──────────────────────────────────────────────────────
class PolicyItem(BaseModel):
    id: int
    title: str
    publisher: Optional[str] = None
    region: Optional[str] = "全国"
    pub_date: Optional[date] = None
    deadline: Optional[date] = None
    summary: Optional[str] = None
    category_l1: Optional[str] = None
    category_l2: Optional[str] = None
    policy_types: list[str] = []
    funding_amount: Optional[str] = None
    key_conditions: Optional[str] = None
    score_relevance: float = 0.5
    score_urgency: float = 0.5
    score_value: float = 0.5
    source_url: Optional[str] = None


# ── 竞赛 ──────────────────────────────────────────────────────
class CompetitionItem(BaseModel):
    id: int
    title: str
    organizer: Optional[str] = None
    level: Optional[str] = None
    fields: list[str] = []
    deadline: Optional[date] = None
    award_desc: Optional[str] = None
    summary: Optional[str] = None
    score_value: float = 0.5
    source_url: Optional[str] = None


# ── 报告生成请求 ──────────────────────────────────────────────
class ReportRequest(BaseModel):
    keywords: dict = Field(
        default_factory=dict,
        description="已选关键词，如 {l1:'科技', l2:'人工智能', l3:'申报', region:'广东'}"
    )
    user_input: str = Field(default="", description="用户补充的企业信息")
    module: str = Field(default="policy", description="policy / competition / school")
    session_id: str = Field(default="", description="前端生成的会话 ID，用于关联反馈")


# ── 政策提取结果（v2: 规则提取 + LLM 仅做摘要/分类）─────────
class ExtractedPolicy(BaseModel):
    title: str = ""
    publisher: str = ""
    region: str = "全国"
    pub_date: Optional[str] = None
    deadline: Optional[str] = None
    full_text: str = ""           # trafilatura 提取的完整正文
    summary: str = ""             # LLM 生成摘要（≤150字）
    keywords: list[str] = []      # jieba TF-IDF 关键词
    category_l1: str = ""
    category_l2: str = ""
    policy_types: list[str] = []
    key_conditions: str = ""
    funding_amount: str = ""      # 正则提取，无 LLM 幻觉
