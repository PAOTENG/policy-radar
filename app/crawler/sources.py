"""
政策数据来源矩阵（共 211 个站点）
══════════════════════════════════════════════════════
参考依据：政策补贴宝 / 政企通 / 海策政策大脑的数据渠道，
结合企业关注的补贴、资质、税收、人才、研发等政策维度选定。

数据范围：2025-01-01 至今，仅采集与企业相关的政策文件。

站点分类：
  A. 国家部委（原）   8 个（手写类）
  B. 国家部委（配置） 7 个
  F. 新增国家机构      9 个（农业/交通/住建/金融/证监/卫健/税务/司法/海关）
  G. 省级政府（原）   16 个
  H. 省级政府（补全） 14 个（剩余省份/自治区/直辖市）
  I. 重点城市政府      30 个
  J. 城市科技局        15 个
  C. 省科技厅（原）   12 个
  K. 省科技厅（补全） 13 个
  L. 省工信厅/经信委  16 个
  D. 重点高校（原）    8 个
  M. 高校研究院（扩）  50 个（985/211/双一流）
  E. 专项平台（原）    4 个
  N. 高新区/园区      10 个
  O. 行业协会          9 个
══════════════════════════════════════════════════════
"""
from __future__ import annotations
from dataclasses import dataclass, field
from playwright.async_api import Page
from app.crawler.base import BaseCrawler


# ────────────────────────────────────────────────────────
#  通用工厂：SiteConfig → BaseCrawler 子类
# ────────────────────────────────────────────────────────

@dataclass
class SiteConfig:
    name: str                            # 唯一 ID
    label: str                           # 人类可读名称
    source_urls: list[str]               # 列表页入口 URL（可多个）
    base: str                            # 域名前缀，用于补全相对 URL
    link_contains: list[str]             # 详情页 URL 必须包含其中之一
    link_ends: list[str]                 # 允许的 URL 后缀（空=不限制）
    content_selectors: list[str]         # 依次尝试的正文 CSS 选择器
    max_per_list: int = 20               # 每个列表页最多取多少条链接


def make_crawler(cfg: SiteConfig) -> type[BaseCrawler]:
    """工厂函数：根据 SiteConfig 生成 BaseCrawler 子类"""

    # 提前捕获，避免 Python 闭包晚绑定问题
    _name       = cfg.name
    _sources    = cfg.source_urls
    _base       = cfg.base.rstrip("/")
    _contains   = cfg.link_contains
    _ends       = cfg.link_ends
    _selectors  = cfg.content_selectors
    _max        = cfg.max_per_list

    # 从 base 中提取域名，用于过滤外站链接
    _domain = _base.replace("https://", "").replace("http://", "").split("/")[0]

    class _Crawler(BaseCrawler):
        name = _name
        source_urls = _sources

        async def get_list_urls(self, page: Page) -> list[str]:
            links = await page.query_selector_all("a")
            urls: list[str] = []

            for a in links:
                href = await a.get_attribute("href")
                if not href:
                    continue
                # 过滤无效协议
                if href.startswith(("javascript:", "mailto:", "tel:", "#", "void")):
                    continue
                # 补全协议
                if href.startswith("//"):
                    href = "https:" + href
                elif not href.startswith("http"):
                    href = _base + "/" + href.lstrip("/")
                # 必须在同域
                if _domain not in href:
                    continue
                # 后缀过滤
                if _ends and not any(href.endswith(e) for e in _ends):
                    continue
                # 路径关键词过滤
                if _contains and not any(p in href for p in _contains):
                    continue
                # 跳过明显的列表/首页
                if any(x in href for x in [
                    "/index.", "home_", "/list_", "index.shtml",
                    "index.html", "index.htm", "/list/",
                ]):
                    continue
                urls.append(href)

            return list(set(urls))[:_max]

        async def get_text(self, page: Page, url: str) -> str:
            for sel in _selectors:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if len(text) > 30:
                        return text
            body = (await page.inner_text("body")).strip()
            return body[:8000] if body else ""

    _Crawler.__name__ = f"{_name.title().replace('_', '')}Crawler"
    _Crawler.__qualname__ = _Crawler.__name__
    return _Crawler


# ════════════════════════════════════════════════════════
#  A. 国家部委（原有 8 个 - 保留手写类以保持兼容性）
# ════════════════════════════════════════════════════════

def _scan_links(links_raw: list, base: str,
                contains: list[str], ends: list[str],
                skip_index: bool = True) -> list[str]:
    """通用链接过滤：扫描所有 <a> 后按规则筛选，比固定 CSS 选择器更健壮"""
    from urllib.parse import urljoin, urlparse
    urls = []
    for href in links_raw:
        if not href or href.startswith(("javascript:", "mailto:", "#", "tel:", "void")):
            continue
        # urljoin 正确处理 ./  ../  //  绝对路径 等所有情况
        href = urljoin(base, href)
        # 只保留同域链接
        base_domain = urlparse(base).netloc
        if base_domain not in urlparse(href).netloc:
            continue
        if ends and not any(href.endswith(e) for e in ends):
            continue
        if contains and not any(p in href for p in contains):
            continue
        if skip_index and any(x in href for x in [
            "/index.", "home_", "/list_", "index.shtml", "index.html"
        ]):
            continue
        urls.append(href)
    return list(set(urls))


async def _all_hrefs(page: Page) -> list[str]:
    """提取页面所有 <a> 的 href"""
    return await page.evaluate("""
        () => Array.from(document.querySelectorAll('a[href]'))
                   .map(a => a.getAttribute('href'))
                   .filter(Boolean)
    """)


class MiitCrawler(BaseCrawler):
    """工业和信息化部（制造/数字经济/中小企业/专精特新）"""
    name = "miit"
    source_urls = [
        "https://www.miit.gov.cn/zwgk/zcwj/index.html",
        "https://www.miit.gov.cn/zwgk/zcwj/wjfb/index.html",
    ]

    async def get_list_urls(self, page: Page) -> list[str]:
        hrefs = await _all_hrefs(page)
        return _scan_links(
            hrefs, "https://www.miit.gov.cn",
            contains=["/zwgk/zcwj/", "/zwgk/wjfb/"],
            ends=[".html", ".htm"],
        )[:20]

    async def get_text(self, page: Page, url: str) -> str:
        for sel in ["#UCAP-CONTENT", ".article-content", ".TRS_Editor", "#content"]:
            el = await page.query_selector(sel)
            if el:
                return (await el.inner_text()).strip()
        return (await page.inner_text("body"))[:5000]


class ScienceCrawler(BaseCrawler):
    """科学技术部（科研/高新/R&D/科技奖励）"""
    name = "most"
    source_urls = [
        "https://www.most.gov.cn/tztg/",
        "https://www.most.gov.cn/ywcx/kjzc/",
    ]

    async def get_list_urls(self, page: Page) -> list[str]:
        hrefs = await _all_hrefs(page)
        return _scan_links(
            hrefs, "https://www.most.gov.cn",
            contains=["/tztg/", "/ywcx/", "/content/", "/kjzc/"],
            ends=[".html", ".htm"],
        )[:20]

    async def get_text(self, page: Page, url: str) -> str:
        for sel in ["#UCAP-CONTENT", ".TRS_Editor", ".article-body", "#content"]:
            el = await page.query_selector(sel)
            if el:
                return (await el.inner_text()).strip()
        return (await page.inner_text("body"))[:5000]


class FinanceCrawler(BaseCrawler):
    """财政部（补贴/税收/政府采购/专项资金）"""
    name = "mof"
    source_urls = [
        "https://www.mof.gov.cn/zhengwuxinxi/zhengcefabu/",
        "https://www.mof.gov.cn/zhengwuxinxi/caizhengxinwen/",
    ]

    async def get_list_urls(self, page: Page) -> list[str]:
        hrefs = await _all_hrefs(page)
        # 财政部详情链接分布在 *.mof.gov.cn 各子域，用根域名过滤
        return _scan_links(
            hrefs, "https://mof.gov.cn",
            contains=["/zhengcefabu/", "/zhengwuxinxi/", "/gks/", "/caizhengxinwen/"],
            ends=[".htm", ".html"],
        )[:20]

    async def get_text(self, page: Page, url: str) -> str:
        for sel in ["#UCAP-CONTENT", ".detail_text", ".TRS_Editor", "#article"]:
            el = await page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if len(text) > 30:
                    return text
        body = (await page.inner_text("body")).strip()
        return body[:8000] if body else ""


class GovCrawler(BaseCrawler):
    """中国政府网（国务院顶层政策、部委联合发文）"""
    name = "gov"
    source_urls = [
        "https://www.gov.cn/zhengce/zuixin/home_1.htm",
        "https://www.gov.cn/zhengce/zhengceku/gwywj/home_14.htm",
    ]

    async def get_list_urls(self, page: Page) -> list[str]:
        hrefs = await _all_hrefs(page)
        return _scan_links(
            hrefs, "https://www.gov.cn",
            contains=["/zhengce/content/", "/zhengce/zhengceku/"],
            ends=[".htm", ".html"],
        )[:20]

    async def get_text(self, page: Page, url: str) -> str:
        for sel in ["#UCAP-CONTENT", ".pages-content", ".article-content", "#content"]:
            el = await page.query_selector(sel)
            if el:
                return (await el.inner_text()).strip()
        return (await page.inner_text("body"))[:5000]


class NdrcCrawler(BaseCrawler):
    """国家发改委（投资/产业规划/十四五/设备更新/以旧换新）"""
    name = "ndrc"
    source_urls = [
        "https://www.ndrc.gov.cn/xxgk/zcfb/tz/",
        "https://www.ndrc.gov.cn/xxgk/zcfb/ghxwj/",
    ]

    async def get_list_urls(self, page: Page) -> list[str]:
        hrefs = await _all_hrefs(page)
        return _scan_links(
            hrefs, "https://www.ndrc.gov.cn",
            contains=["/xxgk/zcfb/"],
            ends=[".html", ".htm"],
        )[:20]

    async def get_text(self, page: Page, url: str) -> str:
        for sel in ["#UCAP-CONTENT", ".TRS_Editor", ".article-body", "#content"]:
            el = await page.query_selector(sel)
            if el:
                return (await el.inner_text()).strip()
        return (await page.inner_text("body"))[:5000]


class MohrssCrawler(BaseCrawler):
    """人力资源和社会保障部（就业补贴/稳岗/技能培训/人才激励）"""
    name = "mohrss"
    source_urls = [
        "http://www.mohrss.gov.cn/xxgk2020/fdzdgknr/zcfg/gfxwj/",
        "http://www.mohrss.gov.cn/SYrlzyhshbzb/zcfg/",
    ]

    async def get_list_urls(self, page: Page) -> list[str]:
        hrefs = await _all_hrefs(page)
        return _scan_links(
            hrefs, "http://www.mohrss.gov.cn",
            contains=["mohrss.gov.cn"],
            ends=[".html", ".htm"],
        )[:20]

    async def get_text(self, page: Page, url: str) -> str:
        for sel in ["#UCAP-CONTENT", ".article-body", ".TRS_Editor", ".detail-content"]:
            el = await page.query_selector(sel)
            if el:
                return (await el.inner_text()).strip()
        return (await page.inner_text("body"))[:5000]


class MofcomCrawler(BaseCrawler):
    """商务部（外资/外贸/消费促进/跨境电商/以旧换新）"""
    name = "mofcom"
    source_urls = [
        "https://www.mofcom.gov.cn/xwfb/zhengcejiedu/",
        "https://www.mofcom.gov.cn/article/zcfb/",
    ]

    async def get_list_urls(self, page: Page) -> list[str]:
        hrefs = await _all_hrefs(page)
        return _scan_links(
            hrefs, "https://www.mofcom.gov.cn",
            contains=["/article/", "mofcom.gov.cn"],
            ends=[".shtml", ".html", ".htm"],
        )[:20]

    async def get_text(self, page: Page, url: str) -> str:
        for sel in ["#UCAP-CONTENT", ".article-content", ".TRS_Editor", "#article"]:
            el = await page.query_selector(sel)
            if el:
                return (await el.inner_text()).strip()
        return (await page.inner_text("body"))[:5000]


class SamrCrawler(BaseCrawler):
    """市场监管总局（高新认定/专精特新/质量认证/知识产权）"""
    name = "samr"
    source_urls = [
        "https://www.samr.gov.cn/zw/gkwj/",
        "https://www.samr.gov.cn/zw/tz/",
    ]

    async def get_list_urls(self, page: Page) -> list[str]:
        hrefs = await _all_hrefs(page)
        return _scan_links(
            hrefs, "https://www.samr.gov.cn",
            contains=["/zw/gkwj/", "/zw/tz/", "samr.gov.cn"],
            ends=[".html", ".htm"],
        )[:20]

    async def get_text(self, page: Page, url: str) -> str:
        for sel in ["#UCAP-CONTENT", ".TRS_Editor", ".article-content", "#content"]:
            el = await page.query_selector(sel)
            if el:
                return (await el.inner_text()).strip()
        return (await page.inner_text("body"))[:5000]


# ════════════════════════════════════════════════════════
#  A. 国家部委 - 新增 7 个（工厂模式）
# ════════════════════════════════════════════════════════

_NATIONAL_EXTRA: list[SiteConfig] = [

    SiteConfig(
        name="moe", label="教育部（校企合作/产学研/技术转移政策）",
        source_urls=[
            "https://www.moe.gov.cn/jyb_xxgk/moe_1777/moe_1778/",   # 政策文件
            "https://www.moe.gov.cn/jyb_xxgk/moe_1777/moe_1779/",   # 部门规章
        ],
        base="https://www.moe.gov.cn",
        link_contains=["/jyb_xxgk/", "/srcsite/A16/", "/srcsite/A10/"],
        link_ends=[".html", ".htm"],
        content_selectors=["#UCAP-CONTENT", ".article-body", ".TRS_Editor", ".content"],
    ),

    SiteConfig(
        name="mee", label="生态环境部（碳中和/绿色发展/排放权交易）",
        source_urls=[
            "https://www.mee.gov.cn/zcwj/zcjd/",    # 政策解读
            "https://www.mee.gov.cn/zcwj/gwywj/",   # 国务院文件
            "https://www.mee.gov.cn/zcwj/hjbwj/",   # 部门文件
        ],
        base="https://www.mee.gov.cn",
        link_contains=["/zcwj/"],
        link_ends=[".shtml", ".html", ".htm"],
        content_selectors=["#UCAP-CONTENT", ".article-content", ".TRS_Editor"],
    ),

    SiteConfig(
        name="nea", label="国家能源局（新能源/光伏/风电/储能/氢能补贴）",
        source_urls=[
            "https://www.nea.gov.cn/xxgk/zcwj/",    # 政策文件
            "https://www.nea.gov.cn/xxgk/tztg/",    # 通知公告
        ],
        base="https://www.nea.gov.cn",
        link_contains=["/xxgk/zcwj/", "/xxgk/tztg/", "/xxgk/gjzcjd/"],
        link_ends=[".html", ".htm"],
        content_selectors=["#UCAP-CONTENT", ".TRS_Editor", ".article-body", ".content-text"],
    ),

    SiteConfig(
        name="cnipa", label="国家知识产权局（专利补贴/技术转让/知产营商环境）",
        source_urls=[
            "https://www.cnipa.gov.cn/col/col1/index.html",   # 政策法规
            "https://www.cnipa.gov.cn/col/col1549/index.html",  # 通知公告
        ],
        base="https://www.cnipa.gov.cn",
        link_contains=["/art/", "/col/"],
        link_ends=[".html", ".htm"],
        content_selectors=["#UCAP-CONTENT", ".article-content", ".TRS_Editor", ".con-body"],
    ),

    SiteConfig(
        name="chinatax", label="国家税务总局（减税降费/研发加计扣除/高新税收优惠）",
        source_urls=[
            "https://www.chinatax.gov.cn/chinatax/n810219/n810744/index.html",  # 税收政策
            "https://www.chinatax.gov.cn/chinatax/n810219/n810720/index.html",  # 公告通知
        ],
        base="https://www.chinatax.gov.cn",
        link_contains=["/n810219/", "/chinatax/"],
        link_ends=[".html", ".htm"],
        content_selectors=["#UCAP-CONTENT", ".article-body", ".TRS_Editor", ".con-content"],
    ),

    SiteConfig(
        name="pbc", label="中国人民银行（货币政策/绿色金融/科创金融/贷款贴息）",
        source_urls=[
            "https://www.pbc.gov.cn/goutongjiaoliu/113456/113469/index.html",  # 货币政策
            "https://www.pbc.gov.cn/tiaofasi/144941/144959/index.html",         # 法规规章
        ],
        base="https://www.pbc.gov.cn",
        link_contains=["/goutongjiaoliu/", "/tiaofasi/", "/zhengwugongkai/"],
        link_ends=[".html", ".htm"],
        content_selectors=["#UCAP-CONTENT", ".article-content", ".TRS_Editor"],
    ),

    SiteConfig(
        name="sasac", label="国资委（国企改革/混改/国有资本投资/产业引导基金）",
        source_urls=[
            "https://www.sasac.gov.cn/n2588035/n2588320/index.html",   # 政策法规
            "https://www.sasac.gov.cn/n2588035/n2641579/index.html",   # 通知公告
        ],
        base="https://www.sasac.gov.cn",
        link_contains=["/n2588035/", "/n4470755/"],
        link_ends=[".html", ".htm"],
        content_selectors=["#UCAP-CONTENT", ".article-body", ".TRS_Editor"],
    ),
]


# ════════════════════════════════════════════════════════
#  B. 省级政府 16 个（直辖市+经济强省）
#  覆盖理由：政策补贴宝等商业平台均将省级政府列为核心数据源，
#  各省政府每年发布地方性产业补贴/人才引进/园区优惠政策数百条
# ════════════════════════════════════════════════════════

# 省级政府政策页面通用 CSS 选择器（TRS CMS 框架通用）
_PROV_SELECTORS = ["#UCAP-CONTENT", ".TRS_Editor", ".article-content",
                   ".content-text", ".zwml", ".pages-content", ".detail-content"]

# 省级政府详情页通用 URL 关键词
_PROV_LINK_CONTAINS = [
    "/zfwj/", "/zcwj/", "/xxgk/", "/zwgk/",
    "/tztg/", "/zcjd/", "/gkml/", "/gongkai/",
    "/content/", "/article/",
]

_PROVINCIAL_SITES: list[SiteConfig] = [

    SiteConfig(
        name="beijing_gov", label="北京市政府（科创中心/数字经济/高精尖）",
        source_urls=[
            "https://www.beijing.gov.cn/zhengce/zfwj/",        # 政府文件
            "https://www.beijing.gov.cn/zhengce/zcjd/",        # 政策解读
        ],
        base="https://www.beijing.gov.cn",
        link_contains=_PROV_LINK_CONTAINS + ["/zhengce/"],
        link_ends=[".html", ".htm", ".shtml"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="shanghai_gov", label="上海市政府（自贸区/集成电路/生物医药）",
        source_urls=[
            "https://www.shanghai.gov.cn/nw12344/index.html",   # 政策文件
        ],
        base="https://www.shanghai.gov.cn",
        link_contains=["/nw12344/", "/nw11/", "/nw48455/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="guangdong_gov", label="广东省政府（制造强省/数字广东/港澳合作）",
        source_urls=[
            "https://www.gd.gov.cn/zwgk/wjk/qbwj/index.html",  # 全部文件
        ],
        base="https://www.gd.gov.cn",
        link_contains=["/zwgk/wjk/", "/gkmlpt/content/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="jiangsu_gov", label="江苏省政府（制造大省/独角兽/特色产业集群）",
        source_urls=[
            "https://www.jiangsu.gov.cn/col/col86736/",   # 省政府文件（不带index.html）
            "https://www.jiangsu.gov.cn/col/col84/",       # 政策文件
        ],
        base="https://www.jiangsu.gov.cn",
        link_contains=["/art/", "/col/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="zhejiang_gov", label="浙江省政府（数字经济一号工程/民营经济）",
        source_urls=[
            "https://www.zhejiang.gov.cn/col/col1554671/index.html",  # 省政府文件
        ],
        base="https://www.zhejiang.gov.cn",
        link_contains=["/art/", "/col/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="sichuan_gov", label="四川省政府（成渝双城/新能源/航空航天）",
        source_urls=[
            "https://www.sc.gov.cn/10462/10778/index.shtml",   # 政策文件
        ],
        base="https://www.sc.gov.cn",
        link_contains=["/10462/", "/zcjd/", "/zfwj/"],
        link_ends=[".shtml", ".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="hubei_gov", label="湖北省政府（光芯屏端网/东湖高新/流域治理）",
        source_urls=[
            "https://www.hubei.gov.cn/zwgk/zfwj/szfgfxwj/",   # 省政府规范性文件
            "https://www.hubei.gov.cn/zwgk/zfwj/szfbgt/",      # 省政府办公厅文件
        ],
        base="https://www.hubei.gov.cn",
        link_contains=["/zwgk/zfwj/", "/t202"],  # t202X 匹配年份
        link_ends=[".shtml", ".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="shandong_gov", label="山东省政府（新旧动能转换/海洋经济/农业大省）",
        source_urls=[
            "https://www.shandong.gov.cn/col/col93931/index.html",  # 政策文件
        ],
        base="https://www.shandong.gov.cn",
        link_contains=["/art/", "/col/col93931/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="henan_gov", label="河南省政府（制造业转型/中原城市群/农产品加工）",
        source_urls=[
            "https://www.henan.gov.cn/hnsrmzf/zwgk/zfwj/",  # 去掉 index.html
            "https://www.henan.gov.cn/hnsrmzf/zwgk/zcjd/",  # 政策解读
        ],
        base="https://www.henan.gov.cn",
        link_contains=["/hnsrmzf/", "/t202"],  # 更宽松的匹配
        link_ends=[".html", ".htm", ".shtml"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="hunan_gov", label="湖南省政府（工程机械/新材料/湘商回归）",
        source_urls=[
            "https://www.hunan.gov.cn/hnszf/xxgk/wjk/szfwj/index.html",
        ],
        base="https://www.hunan.gov.cn",
        link_contains=["/xxgk/wjk/", "/hnszf/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="anhui_gov", label="安徽省政府（新能源汽车/量子计算/芯片产业）",
        source_urls=[
            "https://www.ah.gov.cn/public/1681/",   # 政策文件列表
        ],
        base="https://www.ah.gov.cn",
        link_contains=["/public/", "/szfwj/", "/hnsrmzf/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="fujian_gov", label="福建省政府（海洋经济/对台合作/数字福建）",
        source_urls=[
            "https://www.fujian.gov.cn/zwgk/ztzl/",
            "https://www.fujian.gov.cn/zwgk/zfxxgk/szfwj/",
        ],
        base="https://www.fujian.gov.cn",
        link_contains=["/zwgk/zfxxgk/", "/zwgk/ztzl/"],
        link_ends=[".htm", ".html"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="chongqing_gov", label="重庆市政府（成渝双城/汽车制造/智慧城市）",
        source_urls=[
            "https://www.cq.gov.cn/zwgk/zfxxgkml/szfwj/index.html",
        ],
        base="https://www.cq.gov.cn",
        link_contains=["/zwgk/zfxxgkml/szfwj/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="shaanxi_gov", label="陕西省政府（军民融合/航空航天/硬科技）",
        source_urls=[
            "https://www.shaanxi.gov.cn/szf/zfxxgkzl/szfgfxwj/",
        ],
        base="https://www.shaanxi.gov.cn",
        link_contains=["/szf/zfxxgkzl/", "/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="hebei_gov", label="河北省政府（京津冀协同/钢铁转型/雄安新区）",
        source_urls=[
            "https://www.hebei.gov.cn/hebei/11937442/index.html",
        ],
        base="https://www.hebei.gov.cn",
        link_contains=["/hebei/11937442/", "/hebei/11937441/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="tianjin_gov", label="天津市政府（信创产业/港口贸易/生物医药）",
        source_urls=[
            "https://www.tj.gov.cn/zwgk/szfwj/",
        ],
        base="https://www.tj.gov.cn",
        link_contains=["/zwgk/szfwj/", "/zwgk/zcjd/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),
]


# ════════════════════════════════════════════════════════
#  C. 省科技厅 / 科委  12 个
#  覆盖理由：企业申报"高新技术企业""技术创新中心""科技计划项目"
#  等资质及专项资金，主要由省科技厅发布申报通知
# ════════════════════════════════════════════════════════

_SCI_SELECTORS = ["#UCAP-CONTENT", ".TRS_Editor", ".article-content",
                  ".content", ".art-content", ".detail-content"]

_SCIENCE_DEPT_SITES: list[SiteConfig] = [

    SiteConfig(
        name="gdstc", label="广东省科学技术厅（高新认定/科技奖励/产学研项目）",
        source_urls=[
            "https://gdstc.gd.gov.cn/zwgk_n/zcfg/",       # 政策法规
            "https://gdstc.gd.gov.cn/zwgk_n/tzgg/",       # 通知公告
        ],
        base="https://gdstc.gd.gov.cn",
        link_contains=["/zwgk_n/zcfg/", "/zwgk_n/tzgg/", "/art/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="stcsm", label="上海市科学技术委员会（科技创新/技术转移/国际合作）",
        source_urls=[
            "https://stcsm.sh.gov.cn/zwgk/zcfg/",         # 政策法规
            "https://stcsm.sh.gov.cn/zwgk/tzgg/",         # 通知公告
        ],
        base="https://stcsm.sh.gov.cn",
        link_contains=["/zwgk/zcfg/", "/zwgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="jst_js", label="江苏省科学技术厅（科创型企业培育/产业技术研究院）",
        source_urls=[
            "http://kxjst.jiangsu.gov.cn/col/col62076/index.html",  # 政策法规
            "http://kxjst.jiangsu.gov.cn/col/col62078/index.html",  # 通知公告
        ],
        base="http://kxjst.jiangsu.gov.cn",
        link_contains=["/art/", "/col/col62076/", "/col/col62078/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="zjsti", label="浙江省科学技术厅（新型研发机构/科技计划/企业研发补助）",
        source_urls=[
            "https://kjt.zj.gov.cn/col/col1229561085/index.html",  # 规范性文件
            "https://kjt.zj.gov.cn/col/col1229561084/index.html",  # 通知公告
        ],
        base="https://kjt.zj.gov.cn",
        link_contains=["/art/", "/col/col1229561"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_sc", label="四川省科学技术厅（科技型企业/科技成果转化/孵化器）",
        source_urls=[
            "https://kjt.sc.gov.cn/kjt/c103949/zfxxgk_list.shtml",  # 行政规范性文件
            "https://kjt.sc.gov.cn/kjt/c103950/zfxxgk_list.shtml",  # 通知公告
        ],
        base="https://kjt.sc.gov.cn",
        link_contains=["/kjt/", "/art/"],
        link_ends=[".shtml", ".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_hb", label="湖北省科学技术厅（东湖科学城/院士资源/企业技术创新）",
        source_urls=[
            "https://kjt.hubei.gov.cn/zwgk/zcfg/gfxwj/",   # 规范性文件
            "https://kjt.hubei.gov.cn/zwgk/tzgg/",          # 通知公告
        ],
        base="https://kjt.hubei.gov.cn",
        link_contains=["/zwgk/zcfg/", "/zwgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_sd", label="山东省科学技术厅（新旧动能/技术创新/重大科研项目）",
        source_urls=[
            "https://kjt.shandong.gov.cn/zwgk/zcfg/",
            "https://kjt.shandong.gov.cn/zwgk/tzgg/",
        ],
        base="https://kjt.shandong.gov.cn",
        link_contains=["/zwgk/zcfg/", "/zwgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_hn", label="河南省科学技术厅（中原科技城/农业科技/创新主体培育）",
        source_urls=[
            "https://kjt.henan.gov.cn/zwgk/zcwj/",
            "https://kjt.henan.gov.cn/zwgk/tzgg/",
        ],
        base="https://kjt.henan.gov.cn",
        link_contains=["/zwgk/zcwj/", "/zwgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_hu", label="湖南省科学技术厅（岳麓山实验室/科技计划/高企培育）",
        source_urls=[
            "https://kjt.hunan.gov.cn/xxgk/zcfg/",
            "https://kjt.hunan.gov.cn/xxgk/tzgg/",
        ],
        base="https://kjt.hunan.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_hlj", label="黑龙江省科学技术厅（寒地农业科技/振兴东北/科技奖励）",
        source_urls=[
            "https://kjt.hlj.gov.cn/kjt/c107855/zfxxgk_list.shtml",  # 规范性文件
            "https://kjt.hlj.gov.cn/kjt/c107856/zfxxgk_list.shtml",  # 通知
        ],
        base="https://kjt.hlj.gov.cn",
        link_contains=["/kjt/c", "/art/"],
        link_ends=[".shtml", ".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kw_bj", label="北京市科学技术委员会（国际科技合作/首都科创/独角兽）",
        source_urls=[
            "https://kw.beijing.gov.cn/col/col18/index.html",   # 政策法规
            "https://kw.beijing.gov.cn/col/col12/index.html",   # 通知公告
        ],
        base="https://kw.beijing.gov.cn",
        link_contains=["/art/", "/col/col18/", "/col/col12/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_sx", label="陕西省科学技术厅（硬科技之都/军民融合/高校成果转化）",
        source_urls=[
            "https://kjt.shaanxi.gov.cn/xxgk/zcfg/gfxwj/",
            "https://kjt.shaanxi.gov.cn/xxgk/tzgg/",
        ],
        base="https://kjt.shaanxi.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),
]


# ════════════════════════════════════════════════════════
#  D. 重点高校产学研 / 技术转移  8 个
#  覆盖理由：企业通过高校技术转移获得科技成果，高校发布的
#  "横向合作""成果转让""联合研究"公告直接影响企业研发布局；
#  校企合作政策（政府层面）也在这些渠道发布通知
# ════════════════════════════════════════════════════════

_UNI_SELECTORS = [".TRS_Editor", ".article-content", ".content",
                  "#main-content", ".news-content", ".page-content",
                  "#UCAP-CONTENT", ".detail", "article"]

_UNIVERSITY_SITES: list[SiteConfig] = [

    SiteConfig(
        name="tsinghua_rdi", label="清华大学科研院（横向合作/成果转让/校企联合实验室）",
        source_urls=[
            "https://www.rdi.tsinghua.edu.cn/lmjj/cgzh.htm",      # 成果转化
            "https://www.rdi.tsinghua.edu.cn/lmjj/tzgg.htm",      # 通知公告
        ],
        base="https://www.rdi.tsinghua.edu.cn",
        link_contains=["/lmjj/", "/info/"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_SELECTORS,
    ),

    SiteConfig(
        name="zju_research", label="浙江大学科研院（成果转化/产学研基地/概念验证）",
        source_urls=[
            "https://www.srac.zju.edu.cn/list.htm?cattreeId=75",    # 成果转化动态
            "https://www.srac.zju.edu.cn/list.htm?cattreeId=79",    # 通知公告
        ],
        base="https://www.srac.zju.edu.cn",
        link_contains=["/view.htm", "/info/"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_SELECTORS,
    ),

    SiteConfig(
        name="sjtu_aitri", label="上海交通大学技术转移院（专利许可/产业化/校企合作）",
        source_urls=[
            "https://aitri.sjtu.edu.cn/index/news/list",   # 新闻公告
        ],
        base="https://aitri.sjtu.edu.cn",
        link_contains=["/cmsdoc/", "/index/news/"],
        link_ends=[],   # 不限后缀（该站用 UUID 路径）
        content_selectors=_UNI_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="fudan_ist", label="复旦大学科学技术研究院（政产学研金/横向合作）",
        source_urls=[
            "https://ist.fudan.edu.cn/News/List/5",   # 通知
            "https://ist.fudan.edu.cn/News/List/6",   # 新闻
        ],
        base="https://ist.fudan.edu.cn",
        link_contains=["/Data/View/", "/News/"],
        link_ends=[],
        content_selectors=_UNI_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="hit_sr", label="哈尔滨工业大学科学技术院（国防科工/重大装备/成果转化）",
        source_urls=[
            "https://sr.hit.edu.cn/xwzx/tzgg.htm",   # 通知公告
        ],
        base="https://sr.hit.edu.cn",
        link_contains=["/xwzx/", "/info/"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="hust_kjc", label="华中科技大学科学技术发展院（光电/机械/医工/横向合作）",
        source_urls=[
            "https://kjc.hust.edu.cn/info/list.htm?treeid=1003048",  # 政策通知
        ],
        base="https://kjc.hust.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="uestc_kj", label="电子科技大学科技处（信息安全/人工智能/集成电路）",
        source_urls=[
            "https://kj.uestc.edu.cn/info/1052/list.htm",   # 政策通知
        ],
        base="https://kj.uestc.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="tongji_tkm", label="同济大学科技管理部（交通/城建/可持续/产学研合作）",
        source_urls=[
            "https://tkm.tongji.edu.cn/info/1002/list.htm",  # 通知公告
        ],
        base="https://tkm.tongji.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_SELECTORS,
        max_per_list=15,
    ),
]


# ════════════════════════════════════════════════════════
#  E. 专项平台  4 个
# ════════════════════════════════════════════════════════

_SPECIAL_SITES: list[SiteConfig] = [

    SiteConfig(
        name="nsfc", label="国家自然科学基金委（企业参与基础研究/联合基金/重大项目）",
        source_urls=[
            "https://www.nsfc.gov.cn/publish/portal0/tab442/",    # 政策法规
            "https://www.nsfc.gov.cn/publish/portal0/tab434/",    # 项目公告
        ],
        base="https://www.nsfc.gov.cn",
        link_contains=["/publish/portal0/tab"],
        link_ends=[".htm", ".html"],
        content_selectors=["#UCAP-CONTENT", ".content", ".article-content", ".TRS_Editor"],
    ),

    SiteConfig(
        name="miit_sme", label="工信部中小企业局（专精特新/隐形冠军/中小企业发展基金）",
        source_urls=[
            "https://www.miit.gov.cn/zwgk/zcwj/wjfb/index.html",   # 文件发布
        ],
        base="https://www.miit.gov.cn",
        link_contains=["/zwgk/zcwj/wjfb/", "/zwgk/zcwj/"],
        link_ends=[".html", ".htm"],
        content_selectors=["#UCAP-CONTENT", ".TRS_Editor", ".article-content"],
    ),

    SiteConfig(
        name="most_plan", label="科技部规划司（五年规划/国家中长期科技计划）",
        source_urls=[
            "https://www.most.gov.cn/ywcx/gjkjgh/",    # 国家科技规划
            "https://www.most.gov.cn/ywcx/fzgh/",      # 发展规划
        ],
        base="https://www.most.gov.cn",
        link_contains=["/ywcx/gjkjgh/", "/ywcx/fzgh/", "/content/"],
        link_ends=[".html", ".htm"],
        content_selectors=["#UCAP-CONTENT", ".TRS_Editor", ".article-body"],
    ),

    SiteConfig(
        name="ndrc_plan", label="发改委规划司（十四五/十五五国家规划全文/产业规划）",
        source_urls=[
            "https://www.ndrc.gov.cn/xxgk/zcfb/ghwb/",    # 规划文本（十四五等）
        ],
        base="https://www.ndrc.gov.cn",
        link_contains=["/xxgk/zcfb/ghwb/"],
        link_ends=[".html", ".htm"],
        content_selectors=["#UCAP-CONTENT", ".TRS_Editor", ".article-body"],
    ),
]


# ════════════════════════════════════════════════════════
#  F. 新增国家机构（9 个）
# ════════════════════════════════════════════════════════

_NATIONAL_MORE: list[SiteConfig] = [

    SiteConfig(
        name="mara", label="农业农村部（农业补贴/种业振兴/乡村振兴/农机购置补贴）",
        source_urls=[
            "http://www.moa.gov.cn/govpublic/index.htm",
            "http://www.moa.gov.cn/nybgb/",
        ],
        base="http://www.moa.gov.cn",
        link_contains=["/govpublic/", "/nybgb/", "/zcfg/"],
        link_ends=[".htm", ".html"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="mot", label="交通运输部（物流补贴/新能源船舶/智慧交通/基础设施建设）",
        source_urls=[
            "https://www.mot.gov.cn/zhengcejiedu/",
            "https://www.mot.gov.cn/xinwenfabu/zhengcefabu/",
        ],
        base="https://www.mot.gov.cn",
        link_contains=["/zhengcejiedu/", "/xinwenfabu/", "/zcfg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="mohurd", label="住建部（绿色建筑/装配式建筑/房地产/智慧城市）",
        source_urls=[
            "https://www.mohurd.gov.cn/gongkai/zhengcewenjian/index.html",
            "https://www.mohurd.gov.cn/gongkai/fdzdgknr/zcjd/index.html",
        ],
        base="https://www.mohurd.gov.cn",
        link_contains=["/gongkai/", "/fdzdgknr/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="nhc", label="国家卫生健康委（生物医药/医疗器械/互联网医疗/健康产业）",
        source_urls=[
            "http://www.nhc.gov.cn/wjw/gfxwjyjbj/list.shtml",
            "http://www.nhc.gov.cn/wjw/zcjd/list.shtml",
        ],
        base="http://www.nhc.gov.cn",
        link_contains=["/wjw/gfxwjyjbj/", "/wjw/zcjd/", "/xxgk2014/"],
        link_ends=[".shtml", ".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="nfra", label="国家金融监管总局（科创金融/绿色金融/中小微信贷/融资担保）",
        source_urls=[
            "https://www.nfra.gov.cn/cn/view/pages/ItemList.html?itemPId=923&itemId=924",
            "https://www.nfra.gov.cn/cn/view/pages/ItemList.html?itemPId=923&itemId=4113",
        ],
        base="https://www.nfra.gov.cn",
        link_contains=["/nfra/", "/ItemDetail", "/governmentInfo"],
        link_ends=[],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="csrc", label="中国证监会（科创板/北交所/股权激励/直接融资/上市政策）",
        source_urls=[
            "http://www.csrc.gov.cn/csrc/c100028/c101304/list.shtml",  # 规则
            "http://www.csrc.gov.cn/csrc/c100028/c101306/list.shtml",  # 通知
        ],
        base="http://www.csrc.gov.cn",
        link_contains=["/csrc/c100028/", "/csrc/c10"],
        link_ends=[".shtml", ".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="gac", label="海关总署（跨境电商/进出口便利化/综合保税区/RCEP优惠）",
        source_urls=[
            "http://www.customs.gov.cn/customs/zcfg1/zyfl1/index.html",
            "http://www.customs.gov.cn/customs/ywsd/index.html",
        ],
        base="http://www.customs.gov.cn",
        link_contains=["/customs/zcfg1/", "/customs/ywsd/", "/customs/302427/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="moj", label="司法部（营商环境/公平竞争/合规经营/行政许可）",
        source_urls=[
            "http://www.moj.gov.cn/pub/sfbgw/gwzfxxgk/gwzfxxgktnlm/sfgfxwj/",
        ],
        base="http://www.moj.gov.cn",
        link_contains=["/pub/sfbgw/gwzfxxgk/", "/sfgfxwj/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="caa", label="中国民航局（无人机/通用航空/机场建设/航空制造补贴）",
        source_urls=[
            "https://www.caac.gov.cn/XXGK/XXGK/GFXWJ/index_1.html",
            "https://www.caac.gov.cn/XXGK/XXGK/TZTG/index_1.html",
        ],
        base="https://www.caac.gov.cn",
        link_contains=["/XXGK/XXGK/GFXWJ/", "/XXGK/XXGK/TZTG/", "/DOCS/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),
]


# ════════════════════════════════════════════════════════
#  H. 省级政府补全（14 个 — 填满全国 30 省市区）
# ════════════════════════════════════════════════════════

_PROVINCIAL_MORE: list[SiteConfig] = [

    SiteConfig(
        name="guangxi_gov", label="广西壮族自治区政府（东盟合作/边境贸易/糖业/铝业）",
        source_urls=["https://www.gxzf.gov.cn/zfwj/zzqrmzfwj/"],
        base="https://www.gxzf.gov.cn",
        link_contains=["/zfwj/", "/xxgk/", "/gkml/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="yunnan_gov", label="云南省政府（跨境互联网/有色金属/旅游/绿色能源）",
        source_urls=["https://www.yn.gov.cn/zwgk/zcwj/szfwj/"],
        base="https://www.yn.gov.cn",
        link_contains=["/zwgk/zcwj/", "/zwgk/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="guizhou_gov", label="贵州省政府（大数据中心/磷化工/白酒/乡村振兴）",
        source_urls=["https://www.guizhou.gov.cn/zwgk/zfwj/szfwj/"],
        base="https://www.guizhou.gov.cn",
        link_contains=["/zwgk/zfwj/", "/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="jiangxi_gov", label="江西省政府（电子信息/航空制造/稀土/新能源）",
        source_urls=[
            "https://www.jiangxi.gov.cn/art/2025/1/1/art_5066_4450097.html",
            "https://www.jiangxi.gov.cn/col/col72585/",
        ],
        base="https://www.jiangxi.gov.cn",
        link_contains=["/art/", "/col/col72585/", "/zfwj/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="liaoning_gov", label="辽宁省政府（装备制造/石化/振兴老工业基地/港口）",
        source_urls=["https://www.ln.gov.cn/web/zfxxgk/zfwj/szfgfxwj/"],
        base="https://www.ln.gov.cn",
        link_contains=["/web/zfxxgk/zfwj/", "/web/news/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="jilin_gov", label="吉林省政府（汽车产业/农业/碳汇/冰雪经济）",
        source_urls=["https://www.jl.gov.cn/szf/zcwj/szfgfxwj/"],
        base="https://www.jl.gov.cn",
        link_contains=["/szf/zcwj/", "/szf/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="shanxi_gov", label="山西省政府（煤炭转型/碳中和/先进制造/文旅）",
        source_urls=["https://www.shanxi.gov.cn/zcwj/szfwj/"],
        base="https://www.shanxi.gov.cn",
        link_contains=["/zcwj/szfwj/", "/zcwj/gfxwj/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="neimenggu_gov", label="内蒙古自治区政府（稀土/绿氢/草原畜牧/数据中心）",
        source_urls=["https://www.nmg.gov.cn/zwgk/zfxxgk/zfwj/zzqrmzfwj/"],
        base="https://www.nmg.gov.cn",
        link_contains=["/zwgk/zfxxgk/zfwj/", "/zwgk/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="xinjiang_gov", label="新疆维吾尔自治区政府（特色农产品/棉花/旅游/援疆）",
        source_urls=["https://www.xinjiang.gov.cn/xinjiang/xxgk/"],
        base="https://www.xinjiang.gov.cn",
        link_contains=["/xinjiang/xxgk/", "/xinjiang/c"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="gansu_gov", label="甘肃省政府（新材料/中医药/风光储能/古丝绸之路）",
        source_urls=["https://www.gansu.gov.cn/gsszf/c100178/"],
        base="https://www.gansu.gov.cn",
        link_contains=["/gsszf/c100178/", "/gsszf/c100179/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="ningxia_gov", label="宁夏回族自治区政府（清洁能源/葡萄酒/枸杞/移民）",
        source_urls=["https://www.nx.gov.cn/zwgk/qbwj/szfwj/"],
        base="https://www.nx.gov.cn",
        link_contains=["/zwgk/qbwj/", "/zwgk/zcjd/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="hainan_gov", label="海南省政府（自贸港/离岛免税/数字经济/海洋渔业）",
        source_urls=[
            "https://www.hainan.gov.cn/hainan/szfwj/szfgfxwj/",
            "https://www.hainan.gov.cn/hainan/szfwj/bwgfxwj/",
        ],
        base="https://www.hainan.gov.cn",
        link_contains=["/hainan/szfwj/", "/hainan/zcjd/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="qinghai_gov", label="青海省政府（盐湖锂资源/清洁能源/高原农牧/生态）",
        source_urls=["https://www.qinghai.gov.cn/zwgk/zfxx/zfwj/szfwj/"],
        base="https://www.qinghai.gov.cn",
        link_contains=["/zwgk/zfxx/zfwj/", "/zwgk/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="xizang_gov", label="西藏自治区政府（援藏/生态保护/旅游/特色农牧产品）",
        source_urls=["https://www.xizang.gov.cn/zwgk/zfwj/"],
        base="https://www.xizang.gov.cn",
        link_contains=["/zwgk/zfwj/", "/zwgk/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),
]


# ════════════════════════════════════════════════════════
#  I. 重点城市政府（30 个）
#  覆盖计划单列市、省会城市、新一线城市
# ════════════════════════════════════════════════════════

_CITY_GOV_SELECTORS = _PROV_SELECTORS + [".content-box", ".zwgk-content", ".article-box"]

_CITY_LINK_CONTAINS = [
    "/zwgk/", "/xxgk/", "/zfwj/", "/zcfg/", "/tztg/",
    "/content/", "/article/", "/art/",
]

_CITY_SITES: list[SiteConfig] = [

    SiteConfig(
        name="shenzhen_gov", label="深圳市政府（硬科技/先行示范区/数字经济/前海合作）",
        source_urls=[
            "https://www.sz.gov.cn/szzt2010/zwgk/fgwj/szgfxwj/",
            "https://www.sz.gov.cn/szzt2010/wjk/qbwj/",
        ],
        base="https://www.sz.gov.cn",
        link_contains=["/szzt2010/zwgk/", "/szzt2010/wjk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="hangzhou_gov", label="杭州市政府（数字经济/亚运/直播电商/新零售）",
        source_urls=["https://www.hangzhou.gov.cn/art/"],
        base="https://www.hangzhou.gov.cn",
        link_contains=["/art/2025", "/art/2026", "/zwgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="wuhan_gov", label="武汉市政府（光芯屏端网/东湖高新/生物医药/汽车）",
        source_urls=["https://www.wuhan.gov.cn/zwgk/zfxxgk/zfgwgk/szfgfxwj/"],
        base="https://www.wuhan.gov.cn",
        link_contains=["/zwgk/zfxxgk/", "/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="chengdu_gov", label="成都市政府（新经济/电子信息/生物医药/新能源）",
        source_urls=[
            "https://www.chengdu.gov.cn/chengdu/c137009/zcwj_list.shtml",
            "https://www.chengdu.gov.cn/chengdu/c137010/zcjd_list.shtml",
        ],
        base="https://www.chengdu.gov.cn",
        link_contains=["/chengdu/c137", "/content/"],
        link_ends=[".shtml", ".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="xian_gov", label="西安市政府（硬科技/军民融合/航空制造/人才引进）",
        source_urls=["https://www.xa.gov.cn/szf/szfwj/"],
        base="https://www.xa.gov.cn",
        link_contains=["/szf/szfwj/", "/szf/zcjd/", "/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="suzhou_gov", label="苏州市政府（先进制造/开放型经济/纳米/生物医药）",
        source_urls=["https://www.suzhou.gov.cn/szsrmzf/xxgk/wjhb/szfgfxwj/"],
        base="https://www.suzhou.gov.cn",
        link_contains=["/szsrmzf/xxgk/", "/content/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="nanjing_gov", label="南京市政府（软件名城/集成电路/生物医药/智能电网）",
        source_urls=["https://www.nanjing.gov.cn/njszfxxgkml/szfwj/"],
        base="https://www.nanjing.gov.cn",
        link_contains=["/njszfxxgkml/szfwj/", "/content/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="qingdao_gov", label="青岛市政府（海洋经济/家电/半导体/对外开放）",
        source_urls=["https://www.qingdao.gov.cn/zwgk/xxgk/zfwj/szfgfxwj/"],
        base="https://www.qingdao.gov.cn",
        link_contains=["/zwgk/xxgk/zfwj/", "/content/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="zhengzhou_gov", label="郑州市政府（超算中心/汽车/装备/现代物流枢纽）",
        source_urls=["https://www.zhengzhou.gov.cn/zfwj/szfgfxwj/"],
        base="https://www.zhengzhou.gov.cn",
        link_contains=["/zfwj/", "/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="changsha_gov", label="长沙市政府（工程机械/先进储能/医疗器械/互联网）",
        source_urls=["https://www.changsha.gov.cn/xxgk/szfwj/szfgfxwj/"],
        base="https://www.changsha.gov.cn",
        link_contains=["/xxgk/szfwj/", "/content/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="hefei_gov", label="合肥市政府（量子计算/新能源汽车/芯片/科学城）",
        source_urls=["https://www.hefei.gov.cn/zwgk/szfwj/"],
        base="https://www.hefei.gov.cn",
        link_contains=["/zwgk/szfwj/", "/content/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="fuzhou_gov", label="福州市政府（数字峰会/海上福州/软件信息/跨境电商）",
        source_urls=["https://www.fuzhou.gov.cn/zwgk/zfgkzl/gkzl/wjzl/szfwj/"],
        base="https://www.fuzhou.gov.cn",
        link_contains=["/zwgk/zfgkzl/", "/content/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="ningbo_gov", label="宁波市政府（智能制造/绿色石化/港口物流/民营经济）",
        source_urls=["https://www.ningbo.gov.cn/art/"],
        base="https://www.ningbo.gov.cn",
        link_contains=["/art/2025", "/art/2026", "/zwgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="xiamen_gov", label="厦门市政府（自贸区/对台/海洋经济/跨境电商）",
        source_urls=["https://www.xm.gov.cn/zwgk/flfg/sfgfxwj/"],
        base="https://www.xm.gov.cn",
        link_contains=["/zwgk/flfg/", "/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="nanchang_gov", label="南昌市政府（航空制造/VR产业/光电/数字经济）",
        source_urls=["https://www.nc.gov.cn/ncszf/ncxxgk/szfwj/"],
        base="https://www.nc.gov.cn",
        link_contains=["/ncszf/ncxxgk/szfwj/", "/content/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="taiyuan_gov", label="太原市政府（煤化工转型/碳基材料/光伏/钢铁）",
        source_urls=["https://www.taiyuan.gov.cn/szf/szfwj/"],
        base="https://www.taiyuan.gov.cn",
        link_contains=["/szf/", "/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="kunming_gov", label="昆明市政府（花卉/有色金属/绿色能源/南亚合作）",
        source_urls=["https://www.km.gov.cn/c/2025-01-01/"],
        base="https://www.km.gov.cn",
        link_contains=["/c/202", "/article/", "/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="guiyang_gov", label="贵阳市政府（大数据/磷化工/生态旅游/新能源）",
        source_urls=["https://www.guiyang.gov.cn/zwgk/zfwj/"],
        base="https://www.guiyang.gov.cn",
        link_contains=["/zwgk/zfwj/", "/content/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="nanning_gov", label="南宁市政府（东盟合作/糖业/汽车/大宗农产品）",
        source_urls=["https://www.nanning.gov.cn/zwgk/zfxxgkml/xzgfxwj/szfgfxwj/"],
        base="https://www.nanning.gov.cn",
        link_contains=["/zwgk/zfxxgkml/", "/content/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="shenyang_gov", label="沈阳市政府（机床/重型装备/航空发动机/数字化）",
        source_urls=["https://www.shenyang.gov.cn/zwgk/szfwj/"],
        base="https://www.shenyang.gov.cn",
        link_contains=["/zwgk/szfwj/", "/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="dalian_gov", label="大连市政府（保税港/石化/船舶/软件/日韩合作）",
        source_urls=["https://www.dl.gov.cn/zwgk/wjzl/szfwj/"],
        base="https://www.dl.gov.cn",
        link_contains=["/zwgk/wjzl/szfwj/", "/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="changchun_gov", label="长春市政府（汽车/轨道交通/航空航天/光学）",
        source_urls=["https://www.changchun.gov.cn/ywwz/"],
        base="https://www.changchun.gov.cn",
        link_contains=["/ywwz/", "/xxgk/", "/zfwj/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="harbin_gov", label="哈尔滨市政府（装备制造/食品加工/冰雪旅游/重化工）",
        source_urls=["https://www.harbin.gov.cn/zwgk/szfwj/"],
        base="https://www.harbin.gov.cn",
        link_contains=["/zwgk/szfwj/", "/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="wuxi_gov", label="无锡市政府（物联网/集成电路/高端装备/文旅）",
        source_urls=["https://www.wuxi.gov.cn/wxszfxxgkml/szfwj/szfgfxwj/"],
        base="https://www.wuxi.gov.cn",
        link_contains=["/wxszfxxgkml/szfwj/", "/content/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="foshan_gov", label="佛山市政府（家电/建材/智能装备/工业互联网）",
        source_urls=["https://www.foshan.gov.cn/foshan/zwgk/wjzl/sfgfxwj/"],
        base="https://www.foshan.gov.cn",
        link_contains=["/foshan/zwgk/wjzl/", "/content/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="dongguan_gov", label="东莞市政府（消费电子/先进制造/机器人/松山湖）",
        source_urls=["https://www.dg.gov.cn/zwgk/wjzl/szfgfxwj/"],
        base="https://www.dg.gov.cn",
        link_contains=["/zwgk/wjzl/szfgfxwj/", "/content/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="wenzhou_gov", label="温州市政府（民营经济/时尚产业/数字金融/眼镜鞋革）",
        source_urls=["https://www.wenzhou.gov.cn/art/"],
        base="https://www.wenzhou.gov.cn",
        link_contains=["/art/2025", "/art/2026", "/zwgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="jinan_gov", label="济南市政府（量子通信/先进制造/新材料/数字经济）",
        source_urls=["https://www.jinan.gov.cn/art/"],
        base="https://www.jinan.gov.cn",
        link_contains=["/art/2025", "/art/2026", "/zwgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="sjz_gov", label="石家庄市政府（生物医药/新型显示/先进装备/功能食品）",
        source_urls=["https://www.sjz.gov.cn/col/1626745613099/"],
        base="https://www.sjz.gov.cn",
        link_contains=["/col/1626745613099/", "/xxgk/", "/zfwj/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),

    SiteConfig(
        name="quanzhou_gov", label="泉州市政府（纺织服装/鞋业/数控机床/海洋经济）",
        source_urls=["https://www.quanzhou.gov.cn/zwgk/zfgkzl/zfwj/szfwj/"],
        base="https://www.quanzhou.gov.cn",
        link_contains=["/zwgk/zfgkzl/zfwj/", "/content/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_GOV_SELECTORS,
    ),
]


# ════════════════════════════════════════════════════════
#  J. 城市科技局（15 个）
# ════════════════════════════════════════════════════════

_CITY_KSTL_SELECTORS = _SCI_SELECTORS + [".article-box", ".content-box"]

_CITY_STL_SITES: list[SiteConfig] = [

    SiteConfig(
        name="sz_stic", label="深圳市科技创新委（硬科技/专项资金/高企认定/科研机构）",
        source_urls=[
            "https://stic.sz.gov.cn/xxgk/zcfg/zcwj/",
            "https://stic.sz.gov.cn/xxgk/tzgg/",
        ],
        base="https://stic.sz.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_KSTL_SELECTORS,
    ),

    SiteConfig(
        name="hz_kjj", label="杭州市科学技术局（新兴产业/科技小巨人/企业研发补助）",
        source_urls=[
            "https://kjj.hangzhou.gov.cn/art/",
        ],
        base="https://kjj.hangzhou.gov.cn",
        link_contains=["/art/2025", "/art/2026"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_KSTL_SELECTORS,
    ),

    SiteConfig(
        name="wh_kjj", label="武汉市科学技术局（创新驱动/科技企业倍增/产学研）",
        source_urls=[
            "https://kjj.wuhan.gov.cn/zwgk/zcfg/",
            "https://kjj.wuhan.gov.cn/zwgk/tztg/",
        ],
        base="https://kjj.wuhan.gov.cn",
        link_contains=["/zwgk/zcfg/", "/zwgk/tztg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_KSTL_SELECTORS,
    ),

    SiteConfig(
        name="cd_kjj", label="成都市科技局（科技创业/天使基金/概念验证/国际合作）",
        source_urls=[
            "https://kjj.chengdu.gov.cn/kjj/zcfg/zcwj/",
            "https://kjj.chengdu.gov.cn/kjj/zwgk/tztg/",
        ],
        base="https://kjj.chengdu.gov.cn",
        link_contains=["/kjj/zcfg/", "/kjj/zwgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_KSTL_SELECTORS,
    ),

    SiteConfig(
        name="xa_kjj", label="西安市科学技术局（秦创原/硬科技/军民融合/成果转化）",
        source_urls=[
            "https://kj.xa.gov.cn/xxgk/zcfg/",
            "https://kj.xa.gov.cn/xxgk/tzgg/",
        ],
        base="https://kj.xa.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_KSTL_SELECTORS,
    ),

    SiteConfig(
        name="sz_kjj", label="苏州市科技局（独角兽/纳米科技/产业技术研究院/海创）",
        source_urls=[
            "https://kj.suzhou.gov.cn/szskjj/zwgk/zcfg/",
            "https://kj.suzhou.gov.cn/szskjj/zwgk/tzgg/",
        ],
        base="https://kj.suzhou.gov.cn",
        link_contains=["/szskjj/zwgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_KSTL_SELECTORS,
    ),

    SiteConfig(
        name="nj_kjj", label="南京市科学技术局（新型研发机构/科技小巨人/开放创新）",
        source_urls=[
            "https://kjj.nanjing.gov.cn/njskjj/xxgk/zcfg/",
            "https://kjj.nanjing.gov.cn/njskjj/xxgk/tztg/",
        ],
        base="https://kjj.nanjing.gov.cn",
        link_contains=["/njskjj/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_KSTL_SELECTORS,
    ),

    SiteConfig(
        name="qd_kjj", label="青岛市科学技术局（攻坚技术/高企/院士工作站/科创）",
        source_urls=[
            "https://kjj.qingdao.gov.cn/zwgk/zcfg/",
            "https://kjj.qingdao.gov.cn/zwgk/tzgg/",
        ],
        base="https://kjj.qingdao.gov.cn",
        link_contains=["/zwgk/zcfg/", "/zwgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_KSTL_SELECTORS,
    ),

    SiteConfig(
        name="zz_kjj", label="郑州市科学技术局（新型研发机构/科创企业孵化/补贴）",
        source_urls=[
            "https://kj.zhengzhou.gov.cn/xxgk/zcfg/",
        ],
        base="https://kj.zhengzhou.gov.cn",
        link_contains=["/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_KSTL_SELECTORS,
    ),

    SiteConfig(
        name="cs_kjj", label="长沙市科学技术局（三智一芯/高企培育/科技计划项目）",
        source_urls=[
            "https://kjj.changsha.gov.cn/xxgk/zcfg/",
            "https://kjj.changsha.gov.cn/xxgk/tzgg/",
        ],
        base="https://kjj.changsha.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_KSTL_SELECTORS,
    ),

    SiteConfig(
        name="hf_kjj", label="合肥市科学技术局（创新高地/量子科学/基础研究/孵化）",
        source_urls=[
            "https://kjj.hefei.gov.cn/zwgk/zcfg/",
            "https://kjj.hefei.gov.cn/zwgk/tztg/",
        ],
        base="https://kjj.hefei.gov.cn",
        link_contains=["/zwgk/zcfg/", "/zwgk/tztg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_KSTL_SELECTORS,
    ),

    SiteConfig(
        name="nb_kjj", label="宁波市科学技术局（甬江实验室/专精特新/科技金融）",
        source_urls=[
            "https://kjj.ningbo.gov.cn/art/",
        ],
        base="https://kjj.ningbo.gov.cn",
        link_contains=["/art/2025", "/art/2026"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_KSTL_SELECTORS,
    ),

    SiteConfig(
        name="xm_kjj", label="厦门市科学技术局（高新技术/产学研/科技兴贸/海西）",
        source_urls=[
            "https://kjj.xm.gov.cn/zwgk/zcwj/",
            "https://kjj.xm.gov.cn/zwgk/tzgg/",
        ],
        base="https://kjj.xm.gov.cn",
        link_contains=["/zwgk/zcwj/", "/zwgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_KSTL_SELECTORS,
    ),

    SiteConfig(
        name="tj_kjj", label="天津市科学技术局（滨海新区/生物制药/新一代信息技术）",
        source_urls=[
            "https://kj.tj.gov.cn/tjskjj/xxgk/zcfg/",
            "https://kj.tj.gov.cn/tjskjj/xxgk/tzgg/",
        ],
        base="https://kj.tj.gov.cn",
        link_contains=["/tjskjj/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_KSTL_SELECTORS,
    ),

    SiteConfig(
        name="gzh_kjj", label="广州市科学技术局（IAB战略/科技奖励/高企培育/孵化器）",
        source_urls=[
            "https://kjj.gz.gov.cn/gzskjj/xxgk/zcfg/",
            "https://kjj.gz.gov.cn/gzskjj/xxgk/tzgg/",
        ],
        base="https://kjj.gz.gov.cn",
        link_contains=["/gzskjj/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_CITY_KSTL_SELECTORS,
    ),
]


# ════════════════════════════════════════════════════════
#  K. 省科技厅补全（13 个）
# ════════════════════════════════════════════════════════

_SCIENCE_DEPT_MORE: list[SiteConfig] = [

    SiteConfig(
        name="kjt_gx", label="广西科学技术厅（广西特色农业科技/新材料/北部湾）",
        source_urls=[
            "http://kjt.gxzf.gov.cn/xxgk/zcfg/gfxwj/",
            "http://kjt.gxzf.gov.cn/xxgk/tzgg/",
        ],
        base="http://kjt.gxzf.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_yn", label="云南科学技术厅（绿色能源/大健康/高原农业/数字经济）",
        source_urls=[
            "https://kjt.yn.gov.cn/xxgk/zcfg/",
            "https://kjt.yn.gov.cn/xxgk/tzgg/",
        ],
        base="https://kjt.yn.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="stjt_gz", label="贵州省科学技术厅（大数据/新能源汽车/磷化工/中医药）",
        source_urls=[
            "https://stjt.guizhou.gov.cn/xxgk/zcfg/",
            "https://stjt.guizhou.gov.cn/xxgk/tzgg/",
        ],
        base="https://stjt.guizhou.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_jx", label="江西科学技术厅（电子信息/航空产业/稀有金属/中医药）",
        source_urls=[
            "https://kjt.jiangxi.gov.cn/xxgk/zcfg/",
            "https://kjt.jiangxi.gov.cn/xxgk/tzgg/",
        ],
        base="https://kjt.jiangxi.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_ln", label="辽宁科学技术厅（装备制造/精细化工/新材料/振兴）",
        source_urls=[
            "https://kjt.ln.gov.cn/xxgk/zcfg/",
            "https://kjt.ln.gov.cn/xxgk/tzgg/",
        ],
        base="https://kjt.ln.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_jl", label="吉林科学技术厅（汽车零部件/光电子/农业育种/碳汇）",
        source_urls=[
            "https://kjt.jl.gov.cn/xxgk/zcfg/",
            "https://kjt.jl.gov.cn/xxgk/tzgg/",
        ],
        base="https://kjt.jl.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_shanxi", label="山西科学技术厅（煤机装备/新能源/碳基材料/光伏）",
        source_urls=[
            "https://kjt.shanxi.gov.cn/xxgk/zcfg/",
            "https://kjt.shanxi.gov.cn/xxgk/tzgg/",
        ],
        base="https://kjt.shanxi.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_nmg", label="内蒙古科学技术厅（稀土新材料/新能源/草原生态）",
        source_urls=[
            "https://kjt.nmg.gov.cn/xxgk/zcfg/",
            "https://kjt.nmg.gov.cn/xxgk/tzgg/",
        ],
        base="https://kjt.nmg.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_xj", label="新疆科学技术厅（特色农产品/新能源/煤化工/丝路科技）",
        source_urls=[
            "http://kjt.xinjiang.gov.cn/kjt/xxgk/zcfg/",
            "http://kjt.xinjiang.gov.cn/kjt/xxgk/tzgg/",
        ],
        base="http://kjt.xinjiang.gov.cn",
        link_contains=["/kjt/xxgk/zcfg/", "/kjt/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_gs", label="甘肃科学技术厅（新能源/生物医药/中药材/航天科研）",
        source_urls=[
            "http://kjt.gansu.gov.cn/kjt/c115020/zfxxgk_list.shtml",
            "http://kjt.gansu.gov.cn/kjt/c115021/zfxxgk_list.shtml",
        ],
        base="http://kjt.gansu.gov.cn",
        link_contains=["/kjt/c115", "/art/"],
        link_ends=[".shtml", ".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjt_nx", label="宁夏科学技术厅（清洁能源/葡萄酒/枸杞健康/农业创新）",
        source_urls=[
            "https://kjt.nx.gov.cn/xxgk/zcfg/",
            "https://kjt.nx.gov.cn/xxgk/tzgg/",
        ],
        base="https://kjt.nx.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="std_hainan", label="海南省科技厅（自贸港科技/热带农业/海洋科技/种业）",
        source_urls=[
            "https://std.hainan.gov.cn/stdhainan/zcfg/",
            "https://std.hainan.gov.cn/stdhainan/tzgg/",
        ],
        base="https://std.hainan.gov.cn",
        link_contains=["/stdhainan/zcfg/", "/stdhainan/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),

    SiteConfig(
        name="kjj_cq", label="重庆市科学技术局（智能网联/集成电路/生物医药/西部科学城）",
        source_urls=[
            "https://kjj.cq.gov.cn/xxgk_175/szfbgwj/",
            "https://kjj.cq.gov.cn/xxgk_175/tztg/",
        ],
        base="https://kjj.cq.gov.cn",
        link_contains=["/xxgk_175/"],
        link_ends=[".html", ".htm"],
        content_selectors=_SCI_SELECTORS,
    ),
]


# ════════════════════════════════════════════════════════
#  L. 省工信厅 / 经济信息化委（16 个）
#  企业申报"专精特新""高技术制造业""制造业单项冠军"
#  等资质主要通过省工信厅发布，是资金支持类政策核心渠道
# ════════════════════════════════════════════════════════

_GONGXIN_SELECTORS = _SCI_SELECTORS

_GONGXIN_SITES: list[SiteConfig] = [

    SiteConfig(
        name="gxt_gd", label="广东省工业和信息化厅（专精特新/数字化/智能制造）",
        source_urls=[
            "https://gdida.gd.gov.cn/zwgk/zcfg/",
            "https://gdida.gd.gov.cn/zwgk/tzgg/",
        ],
        base="https://gdida.gd.gov.cn",
        link_contains=["/zwgk/zcfg/", "/zwgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),

    SiteConfig(
        name="gxt_js", label="江苏省工业和信息化厅（专精特新/工业互联/先进制造）",
        source_urls=[
            "http://gxt.jiangsu.gov.cn/col/col68094/index.html",  # 通知公告
            "http://gxt.jiangsu.gov.cn/col/col68093/index.html",  # 政策文件
        ],
        base="http://gxt.jiangsu.gov.cn",
        link_contains=["/art/", "/col/col68"],
        link_ends=[".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),

    SiteConfig(
        name="jxt_zj", label="浙江省经济和信息化厅（数字经济/专精特新/平台经济）",
        source_urls=[
            "https://jxt.zj.gov.cn/col/col1229561120/index.html",
        ],
        base="https://jxt.zj.gov.cn",
        link_contains=["/art/", "/col/col1229561"],
        link_ends=[".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),

    SiteConfig(
        name="sheitc_sh", label="上海市经济和信息化委（集成电路/智能制造/信创）",
        source_urls=[
            "https://sheitc.sh.gov.cn/zcfg/index.html",
            "https://sheitc.sh.gov.cn/tzgg/index.html",
        ],
        base="https://sheitc.sh.gov.cn",
        link_contains=["/zcfg/", "/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),

    SiteConfig(
        name="jxt_sc", label="四川省经济和信息化厅（成渝制造/电子信息/新能源/航空）",
        source_urls=[
            "https://jxt.sc.gov.cn/scjxt/c104068/zfxxgk_list.shtml",
            "https://jxt.sc.gov.cn/scjxt/c104067/zfxxgk_list.shtml",
        ],
        base="https://jxt.sc.gov.cn",
        link_contains=["/scjxt/c104", "/art/"],
        link_ends=[".shtml", ".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),

    SiteConfig(
        name="gxt_hb", label="湖北省经济和信息化厅（光芯屏端网/汽车/石化纺织）",
        source_urls=[
            "http://gxt.hubei.gov.cn/bsqydjpt/xxgk/zcfg/",
            "http://gxt.hubei.gov.cn/bsqydjpt/xxgk/tzgg/",
        ],
        base="http://gxt.hubei.gov.cn",
        link_contains=["/bsqydjpt/xxgk/zcfg/", "/bsqydjpt/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),

    SiteConfig(
        name="gxt_sd", label="山东省工业和信息化厅（高端制造/海洋装备/工业互联）",
        source_urls=[
            "http://gxt.shandong.gov.cn/art/",
        ],
        base="http://gxt.shandong.gov.cn",
        link_contains=["/art/2025", "/art/2026", "/zwgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),

    SiteConfig(
        name="gxt_hn_hena", label="河南省工业和信息化厅（制造强省/电子信息/新能源）",
        source_urls=[
            "https://gxt.henan.gov.cn/xxgk/tzgg/",
            "https://gxt.henan.gov.cn/xxgk/zcfg/",
        ],
        base="https://gxt.henan.gov.cn",
        link_contains=["/xxgk/tzgg/", "/xxgk/zcfg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),

    SiteConfig(
        name="jxt_hu", label="湖南省工业和信息化厅（工程机械/航空/新材料/锰产业）",
        source_urls=[
            "https://jxt.hunan.gov.cn/jxt/xxgk/tzgg/",
            "https://jxt.hunan.gov.cn/jxt/xxgk/zcfg/",
        ],
        base="https://jxt.hunan.gov.cn",
        link_contains=["/jxt/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),

    SiteConfig(
        name="jxt_ah", label="安徽省经济和信息化厅（新能源汽车/量子/芯片/装备）",
        source_urls=[
            "https://jxt.ah.gov.cn/zwgk/tzgg/",
            "https://jxt.ah.gov.cn/zwgk/zcfg/",
        ],
        base="https://jxt.ah.gov.cn",
        link_contains=["/zwgk/tzgg/", "/zwgk/zcfg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),

    SiteConfig(
        name="gxt_bj", label="北京市经济和信息化局（信创/高精尖/元宇宙/数字经济）",
        source_urls=[
            "https://jxj.beijing.gov.cn/jxfz/",
        ],
        base="https://jxj.beijing.gov.cn",
        link_contains=["/jxfz/", "/art/", "/xxgk/"],
        link_ends=[".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),

    SiteConfig(
        name="gxt_tj", label="天津市工业和信息化局（信创/生物医药/新能源/航空）",
        source_urls=[
            "https://xyj.tj.gov.cn/xxgk/tzgg/",
            "https://xyj.tj.gov.cn/xxgk/zcfg/",
        ],
        base="https://xyj.tj.gov.cn",
        link_contains=["/xxgk/tzgg/", "/xxgk/zcfg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),

    SiteConfig(
        name="gxt_sn", label="陕西省工业和信息化厅（军民融合/硬科技/装备/能化）",
        source_urls=[
            "https://gxt.shaanxi.gov.cn/xxgk/tzgg/",
            "https://gxt.shaanxi.gov.cn/xxgk/zcfg/",
        ],
        base="https://gxt.shaanxi.gov.cn",
        link_contains=["/xxgk/tzgg/", "/xxgk/zcfg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),

    SiteConfig(
        name="gxt_he", label="河北省工业和信息化厅（钢铁转型/雄安/医疗器械/电子）",
        source_urls=[
            "https://gxt.hebei.gov.cn/hbgxt/tzgg/",
        ],
        base="https://gxt.hebei.gov.cn",
        link_contains=["/hbgxt/tzgg/", "/hbgxt/zcfg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),

    SiteConfig(
        name="gxt_ln", label="辽宁省工业和信息化厅（装备制造/机床/化工/船舶）",
        source_urls=[
            "https://gxt.ln.gov.cn/xxgk/tzgg/",
            "https://gxt.ln.gov.cn/xxgk/zcfg/",
        ],
        base="https://gxt.ln.gov.cn",
        link_contains=["/xxgk/tzgg/", "/xxgk/zcfg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),

    SiteConfig(
        name="gxt_cq", label="重庆市经济和信息化委（智能网联/生物医药/集成电路）",
        source_urls=[
            "https://jxw.cq.gov.cn/xxgk/szfbgwj/",
            "https://jxw.cq.gov.cn/xxgk/tztg/",
        ],
        base="https://jxw.cq.gov.cn",
        link_contains=["/xxgk/szfbgwj/", "/xxgk/tztg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_GONGXIN_SELECTORS,
    ),
]


# ════════════════════════════════════════════════════════
#  M. 高校研究院 / 技术转移（50 所 985/211/双一流）
#  聚焦科研院所：科研管理处、技术转移中心、产业研究院
# ════════════════════════════════════════════════════════

_UNI_MORE_SELECTORS = _UNI_SELECTORS + [".news-list", ".main-content", ".detail-box"]

_UNIVERSITY_MORE: list[SiteConfig] = [

    # ── 北京 ──────────────────────────────────────────────
    SiteConfig(
        name="pku_kyb", label="北京大学科学研究部（国家级项目/重大专项/成果转化）",
        source_urls=["https://kyb.pku.edu.cn/tzgg/"],
        base="https://kyb.pku.edu.cn",
        link_contains=["/info/", "/tzgg/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="buaa_kyb", label="北京航空航天大学（航空航天/人工智能/新材料科研政策）",
        source_urls=["https://kyb.buaa.edu.cn/info/1053/list.htm"],
        base="https://kyb.buaa.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="bit_kyjsc", label="北京理工大学（爆炸科学/精确制导/电动智能车/科研政策）",
        source_urls=["https://kyjsc.bit.edu.cn/tzgg/"],
        base="https://kyjsc.bit.edu.cn",
        link_contains=["/tzgg/", "/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="bupt_kyc", label="北京邮电大学（5G/6G/人工智能/信息安全/科研通知）",
        source_urls=["https://kyc.bupt.edu.cn/info/1023/list.htm"],
        base="https://kyc.bupt.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="cau_ky", label="中国农业大学（农业科技/种业创新/智慧农业/乡村振兴）",
        source_urls=["https://ky.cau.edu.cn/info/1051/list.htm"],
        base="https://ky.cau.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="bjtu_ky", label="北京交通大学（轨道交通/智慧物流/通信信号/科研通知）",
        source_urls=["https://ky.bjtu.edu.cn/info/1057/list.htm"],
        base="https://ky.bjtu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    # ── 天津 ──────────────────────────────────────────────
    SiteConfig(
        name="nku_kyy", label="南开大学（化学/材料科学/量子信息/经济研究/科研通知）",
        source_urls=["https://kyb.nankai.edu.cn/tzgg/tzgg.htm"],
        base="https://kyb.nankai.edu.cn",
        link_contains=["/tzgg/", "/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="tju_kyc", label="天津大学（化工/建筑/新能源/产学研合作政策）",
        source_urls=["https://kyc.tju.edu.cn/info/1006/list.htm"],
        base="https://kyc.tju.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    # ── 东北 ──────────────────────────────────────────────
    SiteConfig(
        name="jlu_kyc", label="吉林大学（汽车/化学/物理/基因工程/科研项目通知）",
        source_urls=["https://kyc.jlu.edu.cn/info/1084/list.htm"],
        base="https://kyc.jlu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="neu_kyb", label="东北大学（冶金/自动化/计算机/振兴东北/产学研）",
        source_urls=["https://kyb.neu.edu.cn/info/1030/list.htm"],
        base="https://kyb.neu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="dlut_kyb", label="大连理工大学（精细化工/船舶海洋/新材料/光子）",
        source_urls=["https://kyb.dlut.edu.cn/info/1076/list.htm"],
        base="https://kyb.dlut.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    # ── 华东 ──────────────────────────────────────────────
    SiteConfig(
        name="nju_ky", label="南京大学（凝聚态物理/化学/人工智能/基础研究申报）",
        source_urls=["https://ky.nju.edu.cn/tzgg/list.htm"],
        base="https://ky.nju.edu.cn",
        link_contains=["/tzgg/", "/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="seu_ky", label="东南大学（通信/集成电路/建筑/新能源/产学研通知）",
        source_urls=["https://ky.seu.edu.cn/list.htm?cattreeId=8"],
        base="https://ky.seu.edu.cn",
        link_contains=["/view.htm", "/info/"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="hhu_ky", label="河海大学（水利/海洋/新能源/环境/科研申报）",
        source_urls=["https://ky.hhu.edu.cn/info/1053/list.htm"],
        base="https://ky.hhu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="njust_kc", label="南京理工大学（兵器/爆炸/材料/智能制造/国防科工）",
        source_urls=["https://kechuang.njust.edu.cn/info/1022/list.htm"],
        base="https://kechuang.njust.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="nuaa_kjc", label="南京航空航天大学（航空/航天/精密制造/适航/成果转化）",
        source_urls=["https://kjc.nuaa.edu.cn/info/1062/list.htm"],
        base="https://kjc.nuaa.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="ouc_ky", label="中国海洋大学（海洋药物/深海技术/海洋材料/蓝色经济）",
        source_urls=["https://ky.ouc.edu.cn/info/1041/list.htm"],
        base="https://ky.ouc.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="ecnu_sci", label="华东师范大学（先进材料/精密光学/软件工程/产学研）",
        source_urls=["https://research.ecnu.edu.cn/info/1007/list.htm"],
        base="https://research.ecnu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="ustc_ky", label="中国科学技术大学（量子/人工智能/先进材料/科研申报）",
        source_urls=["https://ky.ustc.edu.cn/info/1054/list.htm"],
        base="https://ky.ustc.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    # ── 中部 ──────────────────────────────────────────────
    SiteConfig(
        name="whu_kyy", label="武汉大学（测绘/遥感/生命科学/区块链/科研通知）",
        source_urls=["https://kyy.whu.edu.cn/info/1053/list.htm"],
        base="https://kyy.whu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="csu_ky", label="中南大学（有色冶金/精准医疗/交通/新材料/科研申报）",
        source_urls=["https://ky.csu.edu.cn/info/1054/list.htm"],
        base="https://ky.csu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="hnu_ky", label="湖南大学（机械/材料/金融工程/清洁能源/科研通知）",
        source_urls=["https://ky.hnu.edu.cn/info/1057/list.htm"],
        base="https://ky.hnu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="ccnu_ky", label="华中师范大学（大数据/教育信息化/化学/物理科研申报）",
        source_urls=["https://ky.ccnu.edu.cn/info/1052/list.htm"],
        base="https://ky.ccnu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="hzau_ky", label="华中农业大学（种业/水产/动物疫苗/农业生物技术）",
        source_urls=["https://ky.hzau.edu.cn/info/1055/list.htm"],
        base="https://ky.hzau.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="zzu_ky", label="郑州大学（新型材料/生物/橡胶化工/科研申报）",
        source_urls=["https://yky.zzu.edu.cn/info/1017/list.htm"],
        base="https://yky.zzu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    # ── 华南 ──────────────────────────────────────────────
    SiteConfig(
        name="sysu_ris", label="中山大学（生命科学/药学/海洋/人工智能/成果转化）",
        source_urls=["https://ris.sysu.edu.cn/news_tzgg/list.htm"],
        base="https://ris.sysu.edu.cn",
        link_contains=["/news_tzgg/", "/view.htm", "/info/"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="scut_sci", label="华南理工大学（轻工/材料/计算机/生物技术/成果转化）",
        source_urls=["https://sci2.scut.edu.cn/info/1004/list.htm"],
        base="https://sci2.scut.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="jnu_research", label="暨南大学（华侨经济/生命科学/中药/科研政策）",
        source_urls=["https://research.jnu.edu.cn/info/1017/list.htm"],
        base="https://research.jnu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="xmu_kyy", label="厦门大学（化学/海洋/材料/两岸合作/科研申报）",
        source_urls=["https://kyy.xmu.edu.cn/info/1045/list.htm"],
        base="https://kyy.xmu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="scnu_ky", label="华南师范大学（光学/量子点/激光/教育技术/科研通知）",
        source_urls=["https://ky.scnu.edu.cn/info/1053/list.htm"],
        base="https://ky.scnu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    # ── 西部 ──────────────────────────────────────────────
    SiteConfig(
        name="xjtu_kyy", label="西安交通大学（电力/机械/材料/能化/医学院/科研申报）",
        source_urls=["https://kyy.xjtu.edu.cn/info/1053/list.htm"],
        base="https://kyy.xjtu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="nwpu_kyy", label="西北工业大学（无人机/深海/航天/新材料/军民融合）",
        source_urls=["https://kyy.nwpu.edu.cn/info/1053/list.htm"],
        base="https://kyy.nwpu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="scu_kyc", label="四川大学（生物医学/轻工/材料/华西医学/科研项目）",
        source_urls=["https://kyc.scu.edu.cn/info/1049/list.htm"],
        base="https://kyc.scu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="cqu_ky", label="重庆大学（机械/汽车/建筑/新能源/科研申报）",
        source_urls=["https://ky.cqu.edu.cn/info/1053/list.htm"],
        base="https://ky.cqu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="swjtu_kybz", label="西南交通大学（轨道交通/磁浮/新材料/科研通知）",
        source_urls=["https://kybz.swjtu.edu.cn/info/1054/list.htm"],
        base="https://kybz.swjtu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="lzu_kyjsc", label="兰州大学（化学/生态/资源勘探/核技术/科研申报）",
        source_urls=["https://kyjsc.lzu.edu.cn/info/1052/list.htm"],
        base="https://kyjsc.lzu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    # ── 高校专项研究院（技术转移为主）──────────────────────

    SiteConfig(
        name="tju_tti", label="天津大学技术转移中心（科技成果转化/专利运营）",
        source_urls=["https://tti.tju.edu.cn/info/1008/list.htm"],
        base="https://tti.tju.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="ustb_ky", label="北京科技大学（冶金/钢铁/材料/新能源/科研申报）",
        source_urls=["https://ky.ustb.edu.cn/info/1014/list.htm"],
        base="https://ky.ustb.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="bjmu_ky", label="北京大学医学部（生物医药/医疗器械/创新药/转化医学）",
        source_urls=["https://www.bjmu.edu.cn/xwdt/list.htm"],
        base="https://www.bjmu.edu.cn",
        link_contains=["/xwdt/", "/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="gdut_ky", label="广东工业大学（智能制造/工业互联/设计/新能源）",
        source_urls=["https://ky.gdut.edu.cn/info/1052/list.htm"],
        base="https://ky.gdut.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="ncu_kyjsc", label="南昌大学（航空制造/材料/食品科学/VR/科研申报）",
        source_urls=["https://kyjsc.ncu.edu.cn/info/1048/list.htm"],
        base="https://kyjsc.ncu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="hfut_ky", label="合肥工业大学（汽车/仪器/材料/智能制造/科研通知）",
        source_urls=["https://ky.hfut.edu.cn/info/1046/list.htm"],
        base="https://ky.hfut.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="whut_ky", label="武汉理工大学（船舶/汽车/新材料/交通/科研通知）",
        source_urls=["https://kyjsc.whut.edu.cn/info/1052/list.htm"],
        base="https://kyjsc.whut.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="bjfu_ky", label="北京林业大学（碳汇/绿色农林/生物质能/科研申报）",
        source_urls=["https://ky.bjfu.edu.cn/info/1052/list.htm"],
        base="https://ky.bjfu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="nfu_ky", label="南京林业大学（木材加工/碳汇/生物质/林产化学）",
        source_urls=["https://ky.njfu.edu.cn/info/1054/list.htm"],
        base="https://ky.njfu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="csu_med_ky", label="中南大学湘雅医学院（临床研究/医疗器械/精准医疗）",
        source_urls=["https://yxy.csu.edu.cn/info/1027/list.htm"],
        base="https://yxy.csu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="hit_tti", label="哈工大技术转移公司（成果转化/专利许可/孵化）",
        source_urls=["http://www.hitta.com.cn/news/"],
        base="http://www.hitta.com.cn",
        link_contains=["/news/", "/article/"],
        link_ends=[".html", ".htm"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="upc_ky", label="中国石油大学（石油化工/新能源/智能油田/海洋油气）",
        source_urls=["https://ky.upc.edu.cn/info/1052/list.htm"],
        base="https://ky.upc.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="cmu_ky", label="中国矿业大学（矿业/新能源/安全/智能开采/科研）",
        source_urls=["https://ky.cumt.edu.cn/info/1052/list.htm"],
        base="https://ky.cumt.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="gxu_ky", label="广西大学（蔗糖/有色金属/北部湾合作/科研申报）",
        source_urls=["https://kyy.gxu.edu.cn/info/1052/list.htm"],
        base="https://kyy.gxu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),

    SiteConfig(
        name="ynu_ky", label="云南大学（生物多样性/跨境经济/有机农业/民族医学）",
        source_urls=["https://ky.ynu.edu.cn/info/1052/list.htm"],
        base="https://ky.ynu.edu.cn",
        link_contains=["/info/", "/view.htm"],
        link_ends=[".htm", ".html"],
        content_selectors=_UNI_MORE_SELECTORS,
        max_per_list=15,
    ),
]


# ════════════════════════════════════════════════════════
#  N. 国家级高新区 / 科技园（10 个）
# ════════════════════════════════════════════════════════

_PARK_SITES: list[SiteConfig] = [

    SiteConfig(
        name="zgc", label="中关村科技园（北京/企业联盟/硬科技孵化/政策服务）",
        source_urls=[
            "https://www.zgc.gov.cn/zggs/zcfg/",
            "https://www.zgc.gov.cn/zggs/tzgg/",
        ],
        base="https://www.zgc.gov.cn",
        link_contains=["/zggs/zcfg/", "/zggs/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="zhangjiang", label="上海张江高科（集成电路/生物医药/人工智能/科创板）",
        source_urls=[
            "https://www.zjpark.com/news/",
        ],
        base="https://www.zjpark.com",
        link_contains=["/news/", "/zcfg/", "/article/"],
        link_ends=[".html", ".htm"],
        content_selectors=_UNI_SELECTORS,
    ),

    SiteConfig(
        name="donghu_hitech", label="武汉东湖高新区（光谷/光电/生物医药/芯片设计）",
        source_urls=[
            "https://www.wehdz.gov.cn/zwgk/zcfg/",
            "https://www.wehdz.gov.cn/zwgk/tzgg/",
        ],
        base="https://www.wehdz.gov.cn",
        link_contains=["/zwgk/zcfg/", "/zwgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="tianjin_hitech", label="天津滨海高新区（信创/生物医药/新能源/中关村协同）",
        source_urls=[
            "https://www.thstp.gov.cn/xxgk/zcfg/",
            "https://www.thstp.gov.cn/xxgk/tztg/",
        ],
        base="https://www.thstp.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tztg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="xian_hitech", label="西安高新区（硬科技/集成电路/智能制造/一带一路）",
        source_urls=[
            "https://www.xdz.gov.cn/xxgk/zcfg/",
            "https://www.xdz.gov.cn/xxgk/tzgg/",
        ],
        base="https://www.xdz.gov.cn",
        link_contains=["/xxgk/zcfg/", "/xxgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="chengdu_hitech", label="成都高新区（电子信息/新经济/产业孵化/独角兽）",
        source_urls=[
            "https://www.cdht.gov.cn/zwgk/zcfg/",
        ],
        base="https://www.cdht.gov.cn",
        link_contains=["/zwgk/zcfg/", "/zwgk/tztg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="suzhou_indie", label="苏州工业园区（跨境合作/纳米/生物医药/开放创新）",
        source_urls=[
            "https://www.sipac.gov.cn/zwgk/zcfg/",
            "https://www.sipac.gov.cn/zwgk/tzgg/",
        ],
        base="https://www.sipac.gov.cn",
        link_contains=["/zwgk/zcfg/", "/zwgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="qdstp", label="青岛高新区（海洋装备/蓝色经济/新材料/智能制造）",
        source_urls=[
            "https://www.qdstp.gov.cn/zwgk/zcfg/",
        ],
        base="https://www.qdstp.gov.cn",
        link_contains=["/zwgk/zcfg/", "/zwgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="guangzhou_hitech", label="广州高新区/黄埔（IAB/NEM/大湾区/独角兽）",
        source_urls=[
            "https://www.hp.gov.cn/zwgk/zcfg/szfgfxwj/",
        ],
        base="https://www.hp.gov.cn",
        link_contains=["/zwgk/zcfg/", "/zwgk/tzgg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="shenzhen_hitech", label="深圳南山/坂田高新区（硬科技/医疗器械/AI/半导体）",
        source_urls=[
            "https://www.nsqzf.gov.cn/zwgk/zcfg/",
        ],
        base="https://www.nsqzf.gov.cn",
        link_contains=["/zwgk/zcfg/", "/zwgk/tztg/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),
]


# ════════════════════════════════════════════════════════
#  O. 行业协会 / 专项平台（9 个）
#  政策公告、行业标准、补贴申报指引的重要补充渠道
# ════════════════════════════════════════════════════════

_INDUSTRY_ASSOC_SITES: list[SiteConfig] = [

    SiteConfig(
        name="csia_semi", label="中国半导体行业协会（集成电路/设计制造/人才政策）",
        source_urls=[
            "http://www.csia.net.cn/Article/ChannelList.aspx?ChannelID=4",
        ],
        base="http://www.csia.net.cn",
        link_contains=["/Article/", "/ArticleShow"],
        link_ends=[".aspx", ".html"],
        content_selectors=_UNI_SELECTORS,
    ),

    SiteConfig(
        name="caefi", label="中国电子信息行业联合会（电子/信息化/行业补贴通知）",
        source_urls=[
            "http://www.caefi.net/news/",
        ],
        base="http://www.caefi.net",
        link_contains=["/news/"],
        link_ends=[".html", ".htm"],
        content_selectors=_UNI_SELECTORS,
    ),

    SiteConfig(
        name="cpcif", label="中国石油化工工业协会（石化/碳中和/绿色化工/新材料）",
        source_urls=[
            "http://www.cpcif.org.cn/detail/zcfg/",
        ],
        base="http://www.cpcif.org.cn",
        link_contains=["/detail/", "/news/"],
        link_ends=[".html", ".htm"],
        content_selectors=_UNI_SELECTORS,
    ),

    SiteConfig(
        name="chinabio", label="中国生物工程学会（生物医药/基因编辑/生物技术补贴）",
        source_urls=[
            "https://www.chinabio.org.cn/gonggao/",
        ],
        base="https://www.chinabio.org.cn",
        link_contains=["/gonggao/", "/news/"],
        link_ends=[".html", ".htm"],
        content_selectors=_UNI_SELECTORS,
    ),

    SiteConfig(
        name="cast_sci", label="中国科协（科技奖励/科学家精神/科技成果/竞赛）",
        source_urls=[
            "https://www.cast.org.cn/col/col90/index.html",  # 通知公告
        ],
        base="https://www.cast.org.cn",
        link_contains=["/col/col90/", "/art/"],
        link_ends=[".html", ".htm"],
        content_selectors=_PROV_SELECTORS,
    ),

    SiteConfig(
        name="siia_ai", label="中国人工智能学会（AI政策/大模型/数据标注/行业申报）",
        source_urls=[
            "https://www.caai.cn/index.php?s=/Home/Article/index/id/3.html",
        ],
        base="https://www.caai.cn",
        link_contains=["/index.php", "/Article/"],
        link_ends=[".html"],
        content_selectors=_UNI_SELECTORS,
    ),

    SiteConfig(
        name="camce", label="中国机械工程学会（机器人/数控/绿色制造/智能制造申报）",
        source_urls=[
            "https://www.cmes.org/news/type/4/",
        ],
        base="https://www.cmes.org",
        link_contains=["/news/"],
        link_ends=[".html", ".htm"],
        content_selectors=_UNI_SELECTORS,
    ),

    SiteConfig(
        name="caee_ev", label="中国电动汽车百人会（新能源汽车/动力电池/充电桩政策）",
        source_urls=[
            "http://www.chinaev100.org/PolicyReport/Index",
        ],
        base="http://www.chinaev100.org",
        link_contains=["/PolicyReport/", "/News/"],
        link_ends=[],
        content_selectors=_UNI_SELECTORS,
    ),

    SiteConfig(
        name="cspf_solar", label="中国光伏行业协会（光伏补贴/弃光限电/组件标准）",
        source_urls=[
            "https://www.chinapv.org.cn/news/",
        ],
        base="https://www.chinapv.org.cn",
        link_contains=["/news/", "/policy/"],
        link_ends=[".html", ".htm"],
        content_selectors=_UNI_SELECTORS,
    ),
]


# ════════════════════════════════════════════════════════
#  生成所有配置驱动的爬虫类
# ════════════════════════════════════════════════════════

_ALL_CONFIGS: list[SiteConfig] = (
    _NATIONAL_EXTRA          # B-extra (7)
    + _NATIONAL_MORE         # F (9)
    + _PROVINCIAL_SITES      # G-original (16)
    + _PROVINCIAL_MORE       # H (14)
    + _CITY_SITES            # I (30)
    + _CITY_STL_SITES        # J (15)
    + _SCIENCE_DEPT_SITES    # C-original (12)
    + _SCIENCE_DEPT_MORE     # K (13)
    + _GONGXIN_SITES         # L (16)
    + _UNIVERSITY_SITES      # D-original (8)
    + _UNIVERSITY_MORE       # M (45)
    + _SPECIAL_SITES         # E-original (4)
    + _PARK_SITES            # N (10)
    + _INDUSTRY_ASSOC_SITES  # O (9)
)

_GENERATED_CRAWLERS: list[type[BaseCrawler]] = [
    make_crawler(cfg) for cfg in _ALL_CONFIGS
]


# ════════════════════════════════════════════════════════
#  汇总：所有爬虫（手写 8 个 + 自动生成 203 个 = 211 个）
# ════════════════════════════════════════════════════════

_HANDCRAFTED: list[type[BaseCrawler]] = [
    MiitCrawler,     # 工信部
    ScienceCrawler,  # 科技部
    FinanceCrawler,  # 财政部
    GovCrawler,      # 中国政府网
    NdrcCrawler,     # 发改委
    MohrssCrawler,   # 人社部
    MofcomCrawler,   # 商务部
    SamrCrawler,     # 市监总局
]

ALL_CRAWLERS: list[type[BaseCrawler]] = _HANDCRAFTED + _GENERATED_CRAWLERS

CRAWLER_MAP: dict[str, type[BaseCrawler]] = {
    C.name: C for C in ALL_CRAWLERS
}
