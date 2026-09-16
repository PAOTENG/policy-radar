"""种子数据：初始化完整分类树（参考查策网/找政策/工信部体系）+ 示例政策

使用方法：
  cd D:/Projects/policy-radar
  python scripts/seed_data.py
"""
import asyncio, sys, os
from datetime import date
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db.init import init_db
from app.db.postgres import get_pool


def d(s: str | None):
    """将 'YYYY-MM-DD' 字符串转为 date 对象，None 原样返回"""
    if s is None:
        return None
    y, m, day = s.split("-")
    return date(int(y), int(m), int(day))


# ═══════════════════════════════════════════════════════════════
# 分类树定义
# 格式：(code, label, parent_code_or_None, level, icon, sort_order)
# ═══════════════════════════════════════════════════════════════
CATEGORIES = [
    # ─── 一级：政策申报模块 ────────────────────────────────────
    ("fund",     "资金支持", None, 1, "Money",        1),
    ("cert",     "资质认定", None, 1, "Medal",        2),
    ("tech",     "科技创新", None, 1, "Cpu",          3),
    ("talent",   "人才政策", None, 1, "User",         4),
    ("industry", "产业发展", None, 1, "Factory",      5),
    ("green",    "绿色低碳", None, 1, "WindPower",    6),
    ("trade",    "对外贸易", None, 1, "Ship",         7),
    ("startup",  "创业扶持", None, 1, "Rocket",       8),

    # ─── 资金支持 → 二级 ──────────────────────────────────────
    ("fund.subsidy", "政府直接补贴", "fund", 2, None, 1),
    ("fund.tax",     "税收优惠",     "fund", 2, None, 2),
    ("fund.loan",    "贷款贴息",     "fund", 2, None, 3),
    ("fund.special", "专项基金",     "fund", 2, None, 4),
    ("fund.post",    "后补助/奖励",  "fund", 2, None, 5),

    # 资金支持 → 三级（补贴）
    ("fund.subsidy.rd",   "研发补贴",     "fund.subsidy", 3, None, 1),
    ("fund.subsidy.mfg",  "技改补贴",     "fund.subsidy", 3, None, 2),
    ("fund.subsidy.exp",  "出口补贴",     "fund.subsidy", 3, None, 3),
    ("fund.subsidy.emp",  "就业补贴",     "fund.subsidy", 3, None, 4),
    ("fund.subsidy.rent", "场地租金补贴", "fund.subsidy", 3, None, 5),

    # 税收优惠 → 三级
    ("fund.tax.15",     "企业所得税15%优惠", "fund.tax", 3, None, 1),
    ("fund.tax.rdadd",  "研发费加计扣除",    "fund.tax", 3, None, 2),
    ("fund.tax.vat",    "增值税即征即退",    "fund.tax", 3, None, 3),
    ("fund.tax.import", "进口税收减免",      "fund.tax", 3, None, 4),

    # 贷款贴息 → 三级
    ("fund.loan.discount", "贷款贴息",     "fund.loan", 3, None, 1),
    ("fund.loan.guaran",   "政府担保贷款", "fund.loan", 3, None, 2),
    ("fund.loan.startup",  "创业担保贷款", "fund.loan", 3, None, 3),

    # 专项基金 → 三级
    ("fund.special.guide", "产业引导基金", "fund.special", 3, None, 1),
    ("fund.special.innov", "科技创新专项", "fund.special", 3, None, 2),
    ("fund.special.green", "绿色发展专项", "fund.special", 3, None, 3),
    ("fund.special.mfg",   "制造业专项",   "fund.special", 3, None, 4),

    # 后补助 → 三级
    ("fund.post.list",   "上市奖励",     "fund.post", 3, None, 1),
    ("fund.post.patent", "知识产权奖励", "fund.post", 3, None, 2),
    ("fund.post.award",  "科技奖项奖励", "fund.post", 3, None, 3),

    # ─── 资质认定 → 二级 ──────────────────────────────────────
    ("cert.hnte",    "高新技术企业",   "cert", 2, None, 1),
    ("cert.zjtx",    "专精特新",       "cert", 2, None, 2),
    ("cert.sme",     "科技型中小企业", "cert", 2, None, 3),
    ("cert.ip",      "知识产权",       "cert", 2, None, 4),
    ("cert.techctr", "企业技术中心",   "cert", 2, None, 5),

    # 高新 → 三级
    ("cert.hnte.apply", "首次申报",     "cert.hnte", 3, None, 1),
    ("cert.hnte.renew", "复审/重新认定","cert.hnte", 3, None, 2),
    ("cert.hnte.award", "认定奖励补贴", "cert.hnte", 3, None, 3),

    # 专精特新 → 三级
    ("cert.zjtx.innov",  "创新型中小企业",     "cert.zjtx", 3, None, 1),
    ("cert.zjtx.sme",    "专精特新中小企业",   "cert.zjtx", 3, None, 2),
    ("cert.zjtx.little", "专精特新\"小巨人\"", "cert.zjtx", 3, None, 3),
    ("cert.zjtx.single", "制造业单项冠军",     "cert.zjtx", 3, None, 4),

    # 科技型中小企业 → 三级
    ("cert.sme.eval",  "科技型中小企业评价入库", "cert.sme", 3, None, 1),
    ("cert.sme.incub", "孵化器/加速器入驻",     "cert.sme", 3, None, 2),

    # 知识产权 → 三级
    ("cert.ip.std",    "知识产权贯标认证",   "cert.ip", 3, None, 1),
    ("cert.ip.demo",   "知识产权示范企业",   "cert.ip", 3, None, 2),
    ("cert.ip.patent", "发明专利资助",       "cert.ip", 3, None, 3),

    # 企业技术中心 → 三级
    ("cert.techctr.nation", "国家级企业技术中心", "cert.techctr", 3, None, 1),
    ("cert.techctr.prov",   "省级企业技术中心",   "cert.techctr", 3, None, 2),

    # ─── 科技创新 → 二级 ──────────────────────────────────────
    ("tech.ai",         "人工智能/大数据", "tech", 2, None, 1),
    ("tech.semi",       "半导体/集成电路", "tech", 2, None, 2),
    ("tech.new_energy", "新能源/储能",     "tech", 2, None, 3),
    ("tech.bio",        "生物医药/医疗器械","tech",2, None, 4),
    ("tech.material",   "新材料",          "tech", 2, None, 5),
    ("tech.iiot",       "工业互联网/智能制造","tech",2, None, 6),
    ("tech.aero",       "航空航天/卫星",   "tech", 2, None, 7),
    ("tech.quantum",    "量子信息技术",    "tech", 2, None, 8),

    # AI → 三级
    ("tech.ai.apply", "应用场景示范",      "tech.ai", 3, None, 1),
    ("tech.ai.infra", "算力/数据基础设施", "tech.ai", 3, None, 2),
    ("tech.ai.model", "大模型/算法研发",   "tech.ai", 3, None, 3),

    # 半导体 → 三级
    ("tech.semi.design", "芯片设计补贴", "tech.semi", 3, None, 1),
    ("tech.semi.fab",    "制造工艺支持", "tech.semi", 3, None, 2),
    ("tech.semi.pkg",    "封测补贴",     "tech.semi", 3, None, 3),

    # 新能源 → 三级
    ("tech.ne.pv",   "光伏/风电", "tech.new_energy", 3, None, 1),
    ("tech.ne.batt", "储能/电池", "tech.new_energy", 3, None, 2),
    ("tech.ne.h2",   "氢能",     "tech.new_energy", 3, None, 3),

    # 生物医药 → 三级
    ("tech.bio.drug",   "创新药研发", "tech.bio", 3, None, 1),
    ("tech.bio.device", "医疗器械",   "tech.bio", 3, None, 2),
    ("tech.bio.cro",    "CRO/医疗服务","tech.bio",3, None, 3),

    # 工业互联网 → 三级
    ("tech.iiot.demo", "智能工厂示范", "tech.iiot", 3, None, 1),
    ("tech.iiot.5g",   "5G+工业应用", "tech.iiot", 3, None, 2),

    # ─── 人才政策 → 二级 ──────────────────────────────────────
    ("talent.senior", "高层次人才引进",   "talent", 2, None, 1),
    ("talent.post",   "博士后设站",       "talent", 2, None, 2),
    ("talent.train",  "职业技能培训补贴", "talent", 2, None, 3),
    ("talent.create", "创业人才扶持",     "talent", 2, None, 4),

    # 高层次人才 → 三级
    ("talent.senior.house",  "住房补贴/安家费", "talent.senior", 3, None, 1),
    ("talent.senior.salary", "薪资补贴",        "talent.senior", 3, None, 2),
    ("talent.senior.abroad", "海外人才归国",    "talent.senior", 3, None, 3),

    # ─── 产业发展 → 二级 ──────────────────────────────────────
    ("industry.digital",  "数字经济",   "industry", 2, None, 1),
    ("industry.mfg",      "先进制造业", "industry", 2, None, 2),
    ("industry.service",  "现代服务业", "industry", 2, None, 3),
    ("industry.culture",  "文化创意",   "industry", 2, None, 4),
    ("industry.agri",     "农业现代化", "industry", 2, None, 5),
    ("industry.sports",   "体育产业",   "industry", 2, None, 6),

    # ─── 绿色低碳 → 二级 ──────────────────────────────────────
    ("green.energy", "节能减排",   "green", 2, None, 1),
    ("green.ev",     "新能源汽车", "green", 2, None, 2),
    ("green.carbon", "碳达峰/碳中和","green",2, None, 3),
    ("green.cycle",  "循环经济",   "green", 2, None, 4),
    ("green.build",  "绿色建筑",   "green", 2, None, 5),

    # ─── 对外贸易 → 二级 ──────────────────────────────────────
    ("trade.export",   "出口退税/补贴", "trade", 2, None, 1),
    ("trade.cross",    "跨境电商",      "trade", 2, None, 2),
    ("trade.ftz",      "自贸区政策",    "trade", 2, None, 3),
    ("trade.overseas", "对外投资扶持",  "trade", 2, None, 4),

    # ─── 创业扶持 → 二级 ──────────────────────────────────────
    ("startup.sub",   "创业补贴",    "startup", 2, None, 1),
    ("startup.incub", "孵化基地入驻","startup", 2, None, 2),
    ("startup.angel", "天使投资引导","startup", 2, None, 3),
    ("startup.park",  "园区优惠政策","startup", 2, None, 4),

    # ═══ 科技竞赛模块（competition） ═══
    ("comp.national", "国家级赛事", None, 1, "Trophy",      10),
    ("comp.province", "省级赛事",   None, 1, "MapLocation", 11),
    ("comp.industry", "行业专项赛", None, 1, "Histogram",   12),
    ("comp.award",    "奖励级别",   None, 1, "Medal",       13),

    ("comp.nat.inno",   "中国创新创业大赛",   "comp.national", 2, None, 1),
    ("comp.nat.chuang", "\"创客中国\"大赛",   "comp.national", 2, None, 2),
    ("comp.nat.cup",    "\"挑战杯\"系列",     "comp.national", 2, None, 3),
    ("comp.nat.inter",  "\"互联网+\"大赛",    "comp.national", 2, None, 4),
    ("comp.nat.disr",   "颠覆性技术大赛",     "comp.national", 2, None, 5),
    ("comp.nat.data",   "数据要素大赛",       "comp.national", 2, None, 6),

    ("comp.prov.gd",  "广东省创新创业大赛", "comp.province", 2, None, 1),
    ("comp.prov.bj",  "北京创新创业大赛",   "comp.province", 2, None, 2),
    ("comp.prov.sh",  "长三角创业大赛",     "comp.province", 2, None, 3),
    ("comp.prov.sz",  "深圳创新创业大赛",   "comp.province", 2, None, 4),

    ("comp.ind.ai",   "AI/大数据专项赛", "comp.industry", 2, None, 1),
    ("comp.ind.mfg",  "智能制造专项赛",  "comp.industry", 2, None, 2),
    ("comp.ind.bio",  "生物医药专项赛",  "comp.industry", 2, None, 3),
    ("comp.ind.ne",   "新能源专项赛",    "comp.industry", 2, None, 4),
    ("comp.ind.fin",  "金融科技赛",      "comp.industry", 2, None, 5),

    ("comp.award.a",  "国家级奖项", "comp.award", 2, None, 1),
    ("comp.award.b",  "省部级奖项", "comp.award", 2, None, 2),
    ("comp.award.c",  "市厅级奖项", "comp.award", 2, None, 3),

    # ═══ 校企合作模块（school） ═══
    ("school.research", "产学研合作",       None, 1, "Reading",     20),
    ("school.talent",   "人才培养合作",     None, 1, "GraduateCap", 21),
    ("school.incub",    "孵化/转化",        None, 1, "Opportunity", 22),
    ("school.gov",      "政府支持校企合作", None, 1, "Office",      23),

    ("school.res.joint", "联合研究院/实验室", "school.research", 2, None, 1),
    ("school.res.fund",  "横向科研项目",      "school.research", 2, None, 2),
    ("school.res.trans", "科技成果转化",      "school.research", 2, None, 3),

    ("school.tal.intern", "定向实习/实训基地", "school.talent", 2, None, 1),
    ("school.tal.doctor", "博士后工作站",      "school.talent", 2, None, 2),
    ("school.tal.order",  "订单式人才培养",    "school.talent", 2, None, 3),

    ("school.incub.park",  "大学科技园入驻", "school.incub", 2, None, 1),
    ("school.incub.spin",  "技术转让/入股",  "school.incub", 2, None, 2),
    ("school.incub.fund",  "高校孵化基金",   "school.incub", 2, None, 3),

    ("school.gov.pilot", "国家产教融合试点",  "school.gov", 2, None, 1),
    ("school.gov.base",  "实训基地建设补贴",  "school.gov", 2, None, 2),
    ("school.gov.dual",  "双师型教师培养",    "school.gov", 2, None, 3),
]


SAMPLE_POLICIES = [
    {
        "title": "关于支持人工智能产业高质量发展的若干措施",
        "publisher": "科技部 工业和信息化部",
        "region": "全国",
        "pub_date": "2024-03-15",
        "deadline": "2025-12-31",
        "summary": "支持人工智能核心技术研发和产业化，最高补贴500万元，适用于主营业务为AI的企业。支持场景示范、算力基础设施和大模型研发三类。",
        "category_l1": "科技创新", "category_l2": "人工智能/大数据",
        "policy_types": ["补贴", "资质"],
        "key_conditions": "注册满3年、年营收500万以上、主营AI业务、有核心专利",
        "funding_amount": "最高500万元",
        "source_url": "https://www.most.gov.cn/example/ai2024.html",
        "score_relevance": 0.90, "score_urgency": 0.80, "score_value": 0.90,
    },
    {
        "title": "高新技术企业认定管理办法（2024年修订）",
        "publisher": "科技部 财政部 国家税务总局",
        "region": "全国",
        "pub_date": "2024-01-01",
        "deadline": None,
        "summary": "高新技术企业享受15%优惠税率（普通25%），须满足研发费用占比、人员比例、知识产权等条件，每3年复审一次。",
        "category_l1": "资质认定", "category_l2": "高新技术企业",
        "policy_types": ["减税", "资质"],
        "key_conditions": "研发费用占比≥3%（5千万以下≥5%），科技人员≥10%，高新产品收入≥60%",
        "funding_amount": "减按15%税率",
        "source_url": "https://www.most.gov.cn/example/hnte2024.html",
        "score_relevance": 0.95, "score_urgency": 0.70, "score_value": 0.95,
    },
    {
        "title": "2025年广东省新一代AI应用示范项目申报指南",
        "publisher": "广东省科学技术厅",
        "region": "广东",
        "pub_date": "2024-04-01",
        "deadline": "2025-09-30",
        "summary": "征集人工智能应用示范项目，重点支持制造、医疗、交通领域，入选项目最高资助300万元，省财政直接拨付。",
        "category_l1": "科技创新", "category_l2": "人工智能/大数据",
        "policy_types": ["补贴", "资质"],
        "key_conditions": "在粤注册、有AI技术落地场景、配套自筹资金≥30%",
        "funding_amount": "最高300万元",
        "source_url": "https://gdst.gd.gov.cn/example/ai_demo2025.html",
        "score_relevance": 0.88, "score_urgency": 0.90, "score_value": 0.88,
    },
    {
        "title": "2025年专精特新\"小巨人\"企业认定申报通知",
        "publisher": "工业和信息化部",
        "region": "全国",
        "pub_date": "2025-01-10",
        "deadline": "2025-08-31",
        "summary": "第六批专精特新小巨人申报，需营收≥5000万，有Ⅰ类发明专利≥2项，细分市场占有率≥10%，优先支持制造业和供应链核心企业。",
        "category_l1": "资质认定", "category_l2": "专精特新",
        "policy_types": ["资质", "补贴"],
        "key_conditions": "营收≥5000万，发明专利≥2项，细分市场占有率证明，已获专精特新中小企业认定",
        "funding_amount": "奖励资金100-200万元（各省不同）",
        "source_url": "https://www.miit.gov.cn/example/zjtx2025.html",
        "score_relevance": 0.92, "score_urgency": 0.85, "score_value": 0.92,
    },
    {
        "title": "上海市支持创业投资高质量发展若干举措",
        "publisher": "上海市地方金融监督管理局",
        "region": "上海",
        "pub_date": "2024-06-15",
        "deadline": "2025-12-31",
        "summary": "对在沪注册并启动科创板上市辅导的企业给予一次性奖励50万元；对天使轮融资的科创企业按融资额10%给予补贴，上限100万元。",
        "category_l1": "资金支持", "category_l2": "后补助/奖励",
        "policy_types": ["补贴"],
        "key_conditions": "在沪注册、已开展科创板上市辅导或完成天使轮融资、净资产≥2000万",
        "funding_amount": "最高100万元",
        "source_url": "https://jrj.sh.gov.cn/example/vc2024.html",
        "score_relevance": 0.75, "score_urgency": 0.80, "score_value": 0.80,
    },
    {
        "title": "北京市研发费用加计扣除政策操作指引（2025）",
        "publisher": "北京市税务局 北京市科委",
        "region": "北京",
        "pub_date": "2025-02-01",
        "deadline": None,
        "summary": "制造业企业研发费用按100%加计扣除，其他企业按75%加计扣除，高新企业可享最高100%优惠，需在汇算清缴时填报。",
        "category_l1": "资金支持", "category_l2": "税收优惠",
        "policy_types": ["减税"],
        "key_conditions": "在北京注册纳税、有真实研发活动、已归集研发费用台账",
        "funding_amount": "实际减税额视营收规模",
        "source_url": "https://beijing.chinatax.gov.cn/example/rdadd2025.html",
        "score_relevance": 0.88, "score_urgency": 0.65, "score_value": 0.88,
    },
]


async def seed():
    await init_db()
    pool = await get_pool()

    async with pool.acquire() as conn:
        # ── 插入分类树 ────────────────────────────────────────────
        code_to_id: dict[str, int] = {}

        for code, label, parent_code, level, icon, sort in CATEGORIES:
            parent_id = code_to_id.get(parent_code) if parent_code else None
            existing = await conn.fetchval(
                "SELECT id FROM categories WHERE code=$1", code
            )
            if existing:
                code_to_id[code] = existing
                print(f"  [skip] {code}", end=" ", flush=True)
                continue
            row = await conn.fetchrow(
                "INSERT INTO categories(parent_id, level, code, label, icon, sort_order) "
                "VALUES($1,$2,$3,$4,$5,$6) RETURNING id",
                parent_id, level, code, label, icon, sort,
            )
            code_to_id[code] = row["id"]

        print(f"\n[Seed] 分类树完成，共 {len(code_to_id)} 个节点", flush=True)

        # ── 插入示例政策 ──────────────────────────────────────────
        inserted = 0
        for p in SAMPLE_POLICIES:
            existing = await conn.fetchval(
                "SELECT id FROM policies WHERE source_url=$1", p["source_url"]
            )
            if existing:
                continue
            await conn.execute(
                """INSERT INTO policies(
                    title, publisher, region, pub_date, deadline, summary,
                    category_l1, category_l2, policy_types, key_conditions,
                    funding_amount, source_url, score_relevance, score_urgency, score_value
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)""",
                p["title"], p["publisher"], p["region"],
                d(p.get("pub_date")), d(p.get("deadline")), p["summary"],
                p["category_l1"], p["category_l2"], p["policy_types"],
                p["key_conditions"], p["funding_amount"], p["source_url"],
                p["score_relevance"], p["score_urgency"], p["score_value"],
            )
            inserted += 1

        print(f"[Seed] 示例政策写入完成，新增 {inserted} 条", flush=True)

    print("[Seed] 全部完成！", flush=True)


if __name__ == "__main__":
    asyncio.run(seed())
