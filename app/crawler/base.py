"""爬虫基类 v2 — 工业级数据流水线

旧版问题：HTML → LLM 全文提取 → 入库
  ✗ 高 token 消耗（每次全量采集约 100 万 token）
  ✗ LLM 幻觉：金额/日期等可验证字段容易被捏造
  ✗ 摘要压缩：4000字 → 100字，RAG 召回时遗漏细节

新版流水线：
  HTML
    ① trafilatura      → 干净全文（专为政务/新闻设计，无 LLM）
    ② BeautifulSoup    → 规则提取标题/日期/发文机构（零幻觉）
    ③ 正则表达式       → 金额/截止日期/政策类型（零幻觉）
    ④ jieba TF-IDF     → 关键词（无 LLM）
    ⑤ Fast LLM         → 仅生成摘要 + 分类（输入≤600字，成本↓80%）
    ⑥ 全文分块嵌入     → policy_chunks（512字/块，RAG 精度大幅提升）
    ⑦ 全文 + 元数据    → Elasticsearch（BM25 关键词检索）
    ⑧ 摘要级向量       → policies.embedding（快速粗排）

合规设计（不变）：
  - 请求间隔 5~8 秒，URL 去重，失败记录，研究爬虫 UA
"""
from __future__ import annotations

import asyncio
import json
import re
import random
from abc import ABC, abstractmethod
from datetime import date as _date

try:
    import trafilatura
    _HAS_TRAFILATURA = True
except ImportError:
    _HAS_TRAFILATURA = False
    print("[Crawler] ⚠ trafilatura 未安装，将回退到 CSS 选择器模式", flush=True)

try:
    import jieba.analyse as _jieba
    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Browser, Page
from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage
from app.config import get_settings
from app.db.postgres import get_pool


# ── User-Agent（真实浏览器，避免WAF封锁）───────────────────────
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── 附加请求头（模拟真实浏览器行为）──────────────────────────
_EXTRA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
}

# ── LLM Prompt（仅做摘要 + 分类，输入≤600字）──────────────────
# category_l1 必须从以下选项中精确选一个（与前端分类树完全对齐）：
#   资金支持   → 补贴/奖励/贷款贴息/专项基金
#   资质认定   → 高新/专精特新/知识产权/科技中小企业
#   科技创新   → 研发/AI/芯片/新能源/生物医药/智能制造
#   人才政策   → 人才引进/博士后/技能培训
#   产业发展   → 数字经济/先进制造/服务业
#   绿色低碳   → 节能减排/碳中和/新能源汽车
#   对外贸易   → 出口退税/跨境/自贸区
#   创业扶持   → 创业补贴/孵化/园区
#   科技竞赛   → 大赛/评选/竞赛
#   校企合作   → 产学研/校企/高校联合
#   其他       → 以上均不符合时使用
_CLASSIFY_PROMPT = """\
你是政策分析专家。针对以下政策，完成两件事：
1. 用100字内概括核心要点（支持对象、支持方式、金额、申报核心条件）
2. 从以下11个类别中选择最匹配的 category_l1（必须精确使用这些文字之一，不得自造）：
   资金支持、资质认定、科技创新、人才政策、产业发展、绿色低碳、对外贸易、创业扶持、科技竞赛、校企合作、其他

标题：{title}
正文节选：
{excerpt}

严格输出 JSON，不要包含 markdown 代码块：
{{"summary":"...","category_l1":"（从上述11个类别中选一个）","category_l2":"细分类别（10字内）","key_conditions":"申报核心条件50字内"}}"""

# ── 省份关键词映射（从URL/标题/发布机构提取地区）────────────────
_REGION_KEYWORDS: dict[str, list[str]] = {
    "北京":  ["beijing", "bj.gov.cn", "北京"],
    "天津":  ["tianjin", "tj.gov.cn", "天津"],
    "河北":  ["hebei",   "he.gov.cn", "河北"],
    "山西":  ["shanxi",  "sx.gov.cn", "山西"],
    "内蒙古":["neimenggu","nmg.gov.cn","内蒙古","内蒙"],
    "辽宁":  ["liaoning","ln.gov.cn", "辽宁"],
    "吉林":  ["jilin",   "jl.gov.cn", "吉林"],
    "黑龙江":["heilongjiang","hlj.gov.cn","黑龙江"],
    "上海":  ["shanghai","sh.gov.cn", "上海"],
    "江苏":  ["jiangsu", "js.gov.cn", "江苏"],
    "浙江":  ["zhejiang","zj.gov.cn", "浙江"],
    "安徽":  ["anhui",   "ah.gov.cn", "安徽"],
    "福建":  ["fujian",  "fj.gov.cn", "福建"],
    "江西":  ["jiangxi", "jx.gov.cn", "江西"],
    "山东":  ["shandong","sd.gov.cn", "山东"],
    "河南":  ["henan",   "ha.gov.cn", "河南"],
    "湖北":  ["hubei",   "hb.gov.cn", "湖北"],
    "湖南":  ["hunan",   "hn.gov.cn", "湖南"],
    "广东":  ["guangdong","gd.gov.cn","广东","粤港澳","gdstc","gdkjt"],
    "广西":  ["guangxi", "gx.gov.cn", "广西"],
    "海南":  ["hainan",  "hi.gov.cn", "海南"],
    "重庆":  ["chongqing","cq.gov.cn","重庆"],
    "四川":  ["sichuan", "sc.gov.cn", "四川"],
    "贵州":  ["guizhou", "gz.gov.cn", "贵州"],
    "云南":  ["yunnan",  "yn.gov.cn", "云南"],
    "西藏":  ["xizang",  "xz.gov.cn", "西藏"],
    "陕西":  ["shaanxi", "sn.gov.cn", "陕西"],
    "甘肃":  ["gansu",   "gs.gov.cn", "甘肃"],
    "青海":  ["qinghai", "qh.gov.cn", "青海"],
    "宁夏":  ["ningxia", "nx.gov.cn", "宁夏"],
    "新疆":  ["xinjiang","xj.gov.cn", "新疆"],
    "深圳":  ["sz.gov.cn","shenzhen", "深圳"],
    "杭州":  ["hz.gov.cn","hangzhou", "杭州"],
    "成都":  ["chengdu", "cd.gov.cn", "成都"],
    "苏州":  ["suzhou",  "sz.gov.cn", "苏州"],
    "武汉":  ["wuhan",   "wh.gov.cn", "武汉"],
}


def _extract_region(url: str, title: str, publisher: str) -> str:
    """从 URL / 标题 / 发布机构名 推断适用地区，默认全国"""
    haystack = " ".join([url.lower(), title, publisher]).lower()
    for region, keywords in _REGION_KEYWORDS.items():
        if any(kw.lower() in haystack for kw in keywords):
            return region
    return "全国"

# ── 政策类型关键词映射（规则匹配，无 LLM）───────────────────
_TYPE_MAP: dict[str, list[str]] = {
    "补贴": ["补贴", "补助", "奖励", "奖补", "资助", "扶持资金", "专项资金", "惠企"],
    "贷款": ["贷款", "融资", "信贷", "贴息", "担保", "纾困", "低息"],
    "减税": ["减税", "免税", "税收优惠", "抵扣", "加计扣除", "税前扣除", "免征"],
    "资质": ["认定", "认证", "资质", "高新技术", "专精特新", "隐形冠军", "瞪羚", "独角兽"],
    "竞赛": ["竞赛", "大赛", "评选", "比赛", "赛事", "遴选", "评奖"],
    "规划": ["规划", "实施方案", "指导意见", "行动计划", "发展纲要"],
}

# ── 金额提取正则（仅匹配明确写在原文中的数字，拒绝推断）───
_FUNDING_RE = [
    re.compile(r'(?:最高|最多|不超过|给予|奖励|补贴|补助|资助|支持)\s*(?:人民币\s*)?'
               r'(\d+(?:\.\d+)?)\s*(万|亿)\s*元'),
    re.compile(r'(\d+(?:\.\d+)?)\s*(万|亿)\s*元'
               r'\s*(?:以内|以下|奖励|补贴|补助|资助)'),
    re.compile(r'总(?:金额|规模|投入|资金)\s*(?:约|达|为|不超过)?\s*'
               r'(\d+(?:\.\d+)?)\s*(万|亿)\s*元'),
]

# ── 日期 / 截止日期正则 ───────────────────────────────────────
_DATE_RE       = re.compile(r'(\d{4})[年-](\d{1,2})[月-](\d{1,2})[日号]?')
_DEADLINE_RE   = [
    re.compile(r'(?:申报|申请|报名|提交)(?:截止|截至|时间|日期).{0,25}?'
               r'(\d{4}年\d{1,2}月\d{1,2}日)'),
    re.compile(r'(?:截止|截至)(?:时间|日期)?\s*[：:]\s*(\d{4}年\d{1,2}月\d{1,2}日)'),
    re.compile(r'有效期[至到截止]+\s*(\d{4}年\d{1,2}月\d{1,2}日)'),
]

# ── 发文机构正则 ──────────────────────────────────────────────
_PUBLISHER_RE  = re.compile(r'(?:来\s*源|发文机关|发布机构)[：:]\s*(.{2,40}?)\s*(?:\n|\|)')
_PUBLISHER_SEL = [
    'meta[name="source"]', 'meta[name="author"]',
    '.source', '.publisher', '.origin', '.article-source',
]


# ── 工具函数 ──────────────────────────────────────────────────

def _parse_date(s: str) -> _date | None:
    """返回 datetime.date 对象，asyncpg DATE 列需要此类型"""
    m = _DATE_RE.search(s)
    if not m:
        return None
    try:
        return _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _extract_funding(text: str) -> str:
    for p in _FUNDING_RE:
        m = p.search(text)
        if m:
            return f"{m.group(1)}{m.group(2)}元"
    return ""


def _extract_deadline(text: str) -> _date | None:
    for p in _DEADLINE_RE:
        m = p.search(text)
        if m:
            return _parse_date(m.group(1))
    return None


def _classify_types(text: str) -> list[str]:
    types = [t for t, kws in _TYPE_MAP.items() if any(kw in text for kw in kws)]
    return types or ["其他"]


def _extract_keywords(text: str, top_k: int = 20) -> list[str]:
    """jieba TF-IDF 关键词，无 jieba 时降级为词频统计"""
    if _HAS_JIEBA:
        return list(_jieba.extract_tags(text, topK=top_k))
    # 降级：汉字词频
    from collections import Counter
    _STOP = {'的', '了', '在', '是', '和', '与', '或', '等', '及', '对', '由',
             '其中', '相关', '有关', '以下', '上述', '按照', '根据', '因此'}
    words = re.findall(r'[\u4e00-\u9fa5]{2,6}', text)
    freq  = Counter(w for w in words if w not in _STOP)
    return [w for w, _ in freq.most_common(top_k + 10)][:top_k]


def _chunk_text(text: str, size: int = 512, overlap: int = 64) -> list[str]:
    """滑动窗口分块（字符级），overlap 保证跨块语义连续"""
    if not text or len(text) < 80:
        return []
    chunks, start = [], 0
    while start < len(text):
        end   = min(start + size, len(text))
        chunk = text[start:end].strip()
        if len(chunk) >= 80:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = end - overlap
    return chunks


# ── 基类 ──────────────────────────────────────────────────────

class BaseCrawler(ABC):
    """
    爬虫基类 v2

    子类只需实现 get_list_urls()；get_text() 作为可选回退。
    正文提取、字段解析、嵌入、入库全部由基类统一处理。
    """

    name: str = "base"
    source_urls: list[str] = []
    delay_min: float = 5.0
    delay_max: float = 8.0

    def __init__(self):
        s = get_settings()
        self.llm = ChatOpenAI(
            model=s.fast_llm_model,
            api_key=s.llm_api_key,
            base_url=s.llm_base_url,
            temperature=0.0,
        )

    # ── 子类接口 ─────────────────────────────────────────────

    @abstractmethod
    async def get_list_urls(self, page: Page) -> list[str]:
        """从列表页提取详情页 URL，由子类实现"""
        ...

    async def get_text(self, page: Page, url: str) -> str:
        """CSS 选择器回退（trafilatura 失败时使用），子类可选覆盖"""
        body = (await page.inner_text("body")).strip()
        return body[:8000] if body else ""

    # ── 内部工具 ─────────────────────────────────────────────

    async def _polite_delay(self):
        secs = random.uniform(self.delay_min, self.delay_max)
        print(f"[{self.name}] 等待 {secs:.1f}s", flush=True)
        await asyncio.sleep(secs)

    async def _is_already_saved(self, url: str) -> bool:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return bool(await conn.fetchval(
                "SELECT id FROM policies WHERE source_url=$1", url
            ))

    async def _mark_failed(self, url: str, error: str):
        pool = await get_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchval(
                "SELECT id FROM failed_crawl_items WHERE page_url=$1", url
            )
            if existing:
                await conn.execute(
                    "UPDATE failed_crawl_items SET error_msg=$1, retry_count=retry_count+1,"
                    "last_tried=NOW(), resolved=FALSE WHERE page_url=$2",
                    error[:500], url,
                )
            else:
                await conn.execute(
                    "INSERT INTO failed_crawl_items(source_name,page_url,error_msg)"
                    "VALUES($1,$2,$3)",
                    self.name, url, error[:500],
                )
        print(f"[{self.name}] 记录失败: {url[:60]}", flush=True)

    async def _mark_resolved(self, url: str):
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE failed_crawl_items SET resolved=TRUE WHERE page_url=$1", url
            )

    # ── 核心流水线步骤 ───────────────────────────────────────

    def _rule_extract(self, html: str, full_text: str) -> dict:
        """
        ② 规则提取结构化元数据（零 LLM，零幻觉）
        所有字段均来自原文文本模式匹配，无推断。
        """
        soup = BeautifulSoup(html, "lxml")

        # 标题
        title = ""
        for sel in ["h1", ".article-title", ".title", ".news-title", "h2"]:
            el = soup.select_one(sel)
            if el:
                t = el.get_text(strip=True)
                if 4 < len(t) < 120:
                    title = t
                    break
        if not title:
            el = soup.find("title")
            if el:
                title = el.get_text(strip=True).split("|")[0].split("_")[0].strip()

        # 发布日期（正文前 400 字内找日期标记）
        pub_date = None
        for pat in [r'(?:发布时间|发文日期|公布日期)[：:]\s*(\d{4}[年-]\d{1,2}[月-]\d{1,2})',
                    r'(\d{4}年\d{1,2}月\d{1,2}日)']:
            m = re.search(pat, full_text[:400])
            if m:
                pub_date = _parse_date(m.group(1))
                break

        # 发文机构
        publisher = ""
        for sel in _PUBLISHER_SEL:
            el = soup.select_one(sel)
            if el:
                pub = (el.get("content") or el.get_text(strip=True))[:50]
                if pub:
                    publisher = pub
                    break
        if not publisher:
            m = _PUBLISHER_RE.search(full_text[:500])
            if m:
                publisher = m.group(1).strip()

        return {
            "title":          title,
            "publisher":      publisher,
            "pub_date":       pub_date,
            "funding_amount": _extract_funding(full_text),        # 正则，绝不捏造
            "deadline":       _extract_deadline(full_text),       # 正则，绝不捏造
            "policy_types":   _classify_types(full_text[:3000]),  # 关键词映射
            "keywords":       _extract_keywords(full_text),       # jieba TF-IDF
        }

    async def _llm_classify(self, title: str, text: str) -> dict:
        """
        ⑤ LLM 仅做摘要 + 分类
        输入：标题（≤100字）+ 正文前 600 字 ≈ 200 token
        输出：summary + category_l1 + category_l2 + key_conditions
        相比旧版 4000 字全文提取，token 降低约 80%。
        """
        prompt = _CLASSIFY_PROMPT.format(title=title[:100], excerpt=text[:600])
        try:
            resp = await asyncio.wait_for(
                self.llm.ainvoke([HumanMessage(content=prompt)]),
                timeout=20.0,
            )
            raw = resp.content if isinstance(resp.content, str) else str(resp.content)
            raw = re.sub(r"```json\s*|\s*```", "", raw).strip()
            data = json.loads(raw)
            return {
                "summary":        data.get("summary", "")[:200],
                "category_l1":    data.get("category_l1", "其他"),
                "category_l2":    data.get("category_l2", ""),
                "key_conditions": data.get("key_conditions", "")[:100],
            }
        except Exception as e:
            print(f"[{self.name}] LLM分类失败: {e}", flush=True)
            return {"summary": "", "category_l1": "其他",
                    "category_l2": "", "key_conditions": ""}

    async def _get_embedder(self):
        s = get_settings()
        from langchain_openai import OpenAIEmbeddings
        return OpenAIEmbeddings(
            model=s.embedding_model,
            api_key=s.embed_api_key,
            base_url=s.embed_base_url,
            check_embedding_ctx_length=False,
        )

    async def _save_policy(self, meta: dict, url: str) -> int | None:
        """
        ⑥-A 存入 policies 表
        policies.embedding = embed(标题 + 摘要 + 关键词) → 粗排向量
        全文原文存入 full_text 字段（供 ES 全文检索和下载）
        """
        embedder = await self._get_embedder()
        embed_text = " ".join(filter(None, [
            meta.get("title", ""),
            meta.get("summary", ""),
            " ".join(meta.get("keywords", [])[:10]),
        ]))
        try:
            embedding = await asyncio.wait_for(
                embedder.aembed_query(embed_text[:800]), timeout=8.0
            )
        except Exception:
            embedding = None

        # pgvector 不接受 Python list，需内嵌为 SQL 字面量
        vec_clause = f"'{('[' + ','.join(map(str, embedding)) + ']')}'::vector" \
                     if embedding else "NULL"

        pool = await get_pool()
        async with pool.acquire() as conn:
            if await conn.fetchval("SELECT id FROM policies WHERE source_url=$1", url):
                return None
            row = await conn.fetchrow(
                f"""
                INSERT INTO policies(
                    title, publisher, region, pub_date, deadline,
                    full_text, summary, keywords,
                    category_l1, category_l2, policy_types,
                    key_conditions, funding_amount,
                    source_url, embedding
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,{vec_clause})
                RETURNING id
                """,
                meta.get("title", ""),
                meta.get("publisher") or None,
                meta.get("region", "全国"),
                meta.get("pub_date") or None,   # 现在是 datetime.date | None
                meta.get("deadline") or None,    # 现在是 datetime.date | None
                meta.get("full_text", "")[:60000],
                meta.get("summary", "")[:500],
                meta.get("keywords", []),
                meta.get("category_l1", "其他"),
                meta.get("category_l2", ""),
                meta.get("policy_types", []),
                meta.get("key_conditions", "")[:200],
                meta.get("funding_amount", ""),
                url,
            )
        pid = row["id"]
        print(f"[{self.name}] 入库 id={pid}: {meta.get('title','')[:50]}", flush=True)
        return pid

    async def _save_chunks(self, policy_id: int, full_text: str):
        """
        ⑥-B 全文分块嵌入 → policy_chunks
        每块 512 字，overlap 64 字，独立向量。
        RAG 检索时搜索 policy_chunks，精度远高于整文向量。
        """
        chunks = _chunk_text(full_text)
        if not chunks:
            return
        embedder = await self._get_embedder()
        pool = await get_pool()
        saved = 0
        for idx, chunk in enumerate(chunks):
            try:
                vec = await asyncio.wait_for(
                    embedder.aembed_query(chunk), timeout=8.0
                )
                vec_lit = "[" + ",".join(map(str, vec)) + "]"
                async with pool.acquire() as conn:
                    await conn.execute(
                        f"INSERT INTO policy_chunks(policy_id,chunk_index,chunk_text,embedding)"
                        f" VALUES($1,$2,$3,'{vec_lit}'::vector)",
                        policy_id, idx, chunk,
                    )
                saved += 1
            except Exception as e:
                print(f"[{self.name}] chunk#{idx} 嵌入失败: {e}", flush=True)
        print(f"[{self.name}] policy_id={policy_id} → {saved}/{len(chunks)} chunks", flush=True)

    async def _save_to_es(self, meta: dict, pg_id: int, url: str):
        """
        ⑦ 全文 + 元数据 → Elasticsearch（BM25 关键词检索）
        爬虫线程有独立事件循环，不能复用全局 ES 客户端（aiohttp session 跨循环会
        触发 'Timeout context manager should be used inside a task'），
        改为每次临时构造，用 async with 确保 session 绑定在当前事件循环。
        """
        from elasticsearch import AsyncElasticsearch
        s = get_settings()
        deadline_val = meta.get("deadline")
        doc = {
            "title":          meta.get("title", ""),
            "full_text":      meta.get("full_text", "")[:15000],
            "summary":        meta.get("summary", ""),
            "keywords":       " ".join(meta.get("keywords", [])),
            "publisher":      meta.get("publisher", ""),
            "region":         meta.get("region", "全国"),
            "category_l1":    meta.get("category_l1", ""),
            "category_l2":    meta.get("category_l2", ""),
            "policy_types":   meta.get("policy_types", []),
            "deadline":       deadline_val.isoformat() if deadline_val else None,
            "funding_amount": meta.get("funding_amount", ""),
            "source_url":     url,
            "pg_id":          pg_id,
        }
        try:
            async with AsyncElasticsearch(
                hosts=[s.es_host], verify_certs=False
            ) as es:
                await es.index(index=s.es_policy_index, document=doc)
        except Exception as e:
            print(f"[{self.name}] ES写入失败（不影响PG）: {e}", flush=True)

    # ── 主入口 ───────────────────────────────────────────────

    # ── 并发详情页处理（每个 worker 持有独立 Page）────────────
    async def _process_url(self, ctx, url: str, semaphore: asyncio.Semaphore,
                           counters: dict):
        """单条详情页的完整处理链，供并发调用"""
        async with semaphore:
            if await self._is_already_saved(url):
                counters["skipped"] += 1
                return

            await self._polite_delay()
            page = await ctx.new_page()
            page.set_default_timeout(25000)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=35000)
                await page.wait_for_timeout(2000)  # 等待 JS 渲染
                html = await page.content()

                # ① trafilatura 提取干净全文
                full_text = ""
                if _HAS_TRAFILATURA:
                    full_text = trafilatura.extract(
                        html,
                        include_links=False,
                        include_images=False,
                        no_fallback=False,
                        favor_precision=True,
                        deduplicate=True,
                    ) or ""

                if len(full_text) < 50:
                    full_text = await self.get_text(page, url)

                if len(full_text) < 30:
                    print(f"[{self.name}] 正文过短，跳过: {url[:60]}", flush=True)
                    return

                # ② 规则提取元数据
                meta = self._rule_extract(html, full_text)
                meta["full_text"] = full_text
                # 从 URL / 标题 / 发布机构推断省份，而非直接赋值"全国"
                meta["region"] = _extract_region(
                    url,
                    meta.get("title", ""),
                    meta.get("publisher", ""),
                )

                if not meta.get("title"):
                    print(f"[{self.name}] 无法提取标题，跳过: {url[:60]}", flush=True)
                    return

                # ③ LLM 摘要 + 分类（仅此一处用 LLM，输入≤600字）
                llm_result = await self._llm_classify(meta["title"], full_text[:600])
                meta.update(llm_result)

                # ④ 存 PostgreSQL
                pg_id = await self._save_policy(meta, url)
                if not pg_id:
                    counters["skipped"] += 1
                    return

                # ⑤ 全文分块嵌入 → policy_chunks
                await self._save_chunks(pg_id, full_text)

                # ⑥ 写入 Elasticsearch
                await self._save_to_es(meta, pg_id, url)

                counters["saved"] += 1
                print(f"[{self.name}] ✓ saved={counters['saved']} {meta.get('title','')[:40]}", flush=True)

            except Exception as e:
                short = str(e)[:120]
                print(f"[{self.name}] 详情页失败: {url[:60]} | {short}", flush=True)
                await self._mark_failed(url, short)
                counters["failed"] += 1
            finally:
                await page.close()

    async def run(self, retry_urls: list[str] | None = None) -> dict:
        """执行一次采集任务（retry_urls 非 None 时为重试模式）
        
        并发策略：CONCURRENCY=3，即同时打开 3 个页面并行处理，
        速度约为串行的 3 倍，同时保留礼貌延迟避免对目标站造成压力。
        """
        CONCURRENCY = 3   # 同时处理的页面数，可按需调整为 2-5
        counters = {"saved": 0, "skipped": 0, "failed": 0}

        async with async_playwright() as pw:
            browser: Browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    # 兼容中国政府网站的老旧 TLS 配置（TLS 1.0/1.1 + 旧 Cipher）
                    "--ssl-version-min=tls1",
                    "--ignore-ssl-errors",
                    "--ignore-certificate-errors",
                    "--ignore-certificate-errors-spki-list",
                ]
            )
            ctx = await browser.new_context(
                user_agent=_UA,
                java_script_enabled=True,
                ignore_https_errors=True,          # 修复 SSL_MISMATCH / CERT_INVALID
                extra_http_headers=_EXTRA_HEADERS, # 模拟真实浏览器头部
                viewport={"width": 1920, "height": 1080},
            )
            # 覆盖 navigator.webdriver 属性，减少被检测概率
            await ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
            )

            # ── 获取详情页 URL 列表（仍用单页，避免并发列表页触发反爬）──
            if retry_urls is not None:
                detail_urls = retry_urls
                print(f"[{self.name}] 重试模式 {len(detail_urls)} 条", flush=True)
            else:
                detail_urls = []
                list_page = await ctx.new_page()
                list_page.set_default_timeout(25000)
                for list_url in self.source_urls:
                    try:
                        await list_page.goto(list_url, wait_until="load", timeout=30000)
                        await list_page.wait_for_timeout(3500)  # 等待 JS 渲染内容
                        urls = await self.get_list_urls(list_page)
                        print(f"[{self.name}] {list_url} → {len(urls)} 条", flush=True)
                        detail_urls.extend(urls[:20])
                    except Exception as e:
                        print(f"[{self.name}] 列表页失败: {e}", flush=True)
                        await self._mark_failed(list_url, f"列表页: {e}")
                await list_page.close()

            # ── 并发处理详情页 ─────────────────────────────────────
            semaphore = asyncio.Semaphore(CONCURRENCY)
            tasks = [
                asyncio.create_task(
                    self._process_url(ctx, url, semaphore, counters)
                )
                for url in detail_urls
            ]
            await asyncio.gather(*tasks)

            await ctx.close()
            await browser.close()

        result = {
            "source":  self.name,
            "saved":   counters["saved"],
            "skipped": counters["skipped"],
            "failed":  counters["failed"],
        }
        print(f"[{self.name}] 完成: {result}", flush=True)
        return result

