"""
政策关系边构建器 — LightRAG 风格抽边 + 规则边（app/rag/relation_builder.py）

═══════════════════════════════════════════════════════════════════
本文件职责
═══════════════════════════════════════════════════════════════════
  离线/异步把文档级关系写入 policy_relations：
    1) 噪声过滤（404/报表/导航页不建业务边）
    2) 规则边：行政区层级、文种配对（通知↔公示、办法↔细则）
    3) 标题簇：同 category_l1 + 标题前缀相似 → same_program 候选
    4) LLM 结构化抽边（LightRAG 思路：给定种子+候选 → JSON 边）

函数清单：
  1. is_noise_policy           判定无效/噪声文档
  2. upsert_relation           幂等写入一条边
  3. build_region_child_edges  规则：省→市 region_child
  4. build_doctype_edges       规则：lists_under / implements 文种启发式
  5. build_title_cluster_edges 规则：标题簇 same_program
  6. llm_extract_edges         Fast LLM 对候选对抽边
  7. build_all_relations       主入口：规则全量 + 可选 LLM

设计：
  - 一期不做实体碎图，只连 policies 文档节点
  - confidence=0 的边可写入但查询侧 min_confidence 默认过滤
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from app.config import get_settings
from app.db.postgres import get_pool
from app.rag.policy_graph import RELATION_TYPES


# ── 噪声标题/正文特征（建边前剔除）──────────────────────────────
_NOISE_TITLE_PATTERNS = (
    r"抱歉",
    r"您访问的地址",
    r"404",
    r"页面不存在",
    r"年度报表",
    r"政府网站年度",
    r"本区域包含首页",
    r"网站地图",
    r"无障碍浏览",
)

# 省级 region → 下属市级 region（库内实际出现的子集，可增量扩充）
REGION_CHILDREN: dict[str, tuple[str, ...]] = {
    "广东": ("深圳", "广州", "东莞", "佛山", "珠海", "中山", "惠州"),
    "浙江": ("杭州", "宁波", "温州", "嘉兴", "绍兴"),
    "湖南": ("长沙", "株洲", "湘潭"),
    "江苏": ("南京", "苏州", "无锡", "常州"),
    "山东": ("济南", "青岛", "烟台"),
    "四川": ("成都",),
    "湖北": ("武汉",),
    "福建": ("厦门", "福州"),
}

# 文种关键词
_NOTICE_KW = ("通知", "办法", "意见", "细则", "规定", "指南", "申报")
_LIST_KW = ("公示", "名单", "公告", "通报")
_IMPLEMENT_KW = ("实施细则", "实施办法", "贯彻落实", "操作规程")


def is_noise_policy(title: str | None, full_text: str | None = None) -> bool:
    """判断政策是否为噪声页（不应产生业务关系边）。

    流程：
      1. 标题命中噪声正则 → True
      2. 正文过短（<80）且标题含「报表/导航」类 → True
      3. 否则 False

    参数：
      title / full_text: 政策标题与正文

    返回：
      bool

    作用：
      防止「其他」类中的 404/报表污染图谱。
    """
    t = title or ""
    for pat in _NOISE_TITLE_PATTERNS:
        if re.search(pat, t):
            return True
    body = full_text or ""
    if len(body.strip()) < 80 and re.search(r"报表|导航|首页|栏目", t):
        return True
    return False


async def upsert_relation(
    source_id: int,
    target_id: int,
    relation_type: str,
    *,
    confidence: float = 0.5,
    evidence: str = "",
    build_source: str = "rule",
) -> bool:
    """幂等写入一条政策关系边（UNIQUE 冲突则更新置信度/证据）。

    流程：
      1. 校验 id 不同、relation_type 合法
      2. INSERT ... ON CONFLICT DO UPDATE（取更高 confidence）

    参数：
      source_id / target_id: 政策主键
      relation_type:         见 RELATION_TYPES
      confidence:            0~1
      evidence:              规则说明或原文摘录
      build_source:          rule | title_cluster | llm

    返回：
      bool：是否成功执行（非法参数返回 False）

    作用：
      规则边与 LLM 边的统一落库入口。
    """
    if source_id == target_id or relation_type not in RELATION_TYPES:
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO policy_relations (
                source_policy_id, target_policy_id, relation_type,
                confidence, evidence, build_source
            ) VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (source_policy_id, target_policy_id, relation_type)
            DO UPDATE SET
                confidence = GREATEST(policy_relations.confidence, EXCLUDED.confidence),
                evidence = CASE
                    WHEN EXCLUDED.confidence >= policy_relations.confidence
                    THEN EXCLUDED.evidence
                    ELSE policy_relations.evidence
                END,
                build_source = CASE
                    WHEN EXCLUDED.confidence >= policy_relations.confidence
                    THEN EXCLUDED.build_source
                    ELSE policy_relations.build_source
                END
            """,
            source_id,
            target_id,
            relation_type,
            float(confidence),
            (evidence or "")[:500],
            build_source,
        )
    return True


async def _load_policy_rows(limit: int | None = None) -> list[dict]:
    """加载建边用的政策轻量字段。"""
    pool = await get_pool()
    sql = """
        SELECT id, title, region, category_l1, publisher, pub_date,
               LEFT(COALESCE(full_text, ''), 2000) AS full_text,
               summary, key_conditions
        FROM policies
        ORDER BY id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql)
    return [dict(r) for r in rows]


async def build_region_child_edges(policies: list[dict] | None = None) -> int:
    """规则建边：省级政策 → 同 category_l1 的市级政策，打 region_child。

    流程：
      1. 按 REGION_CHILDREN 把政策分成省桶 / 市桶
      2. 同 category_l1（且非「其他」或双方都是其他则跳过噪声）配对
      3. 每省文最多连同市 5 条，避免爆炸

    返回：
      新写入（或更新）边的尝试次数
    """
    rows = policies if policies is not None else await _load_policy_rows()
    rows = [p for p in rows if not is_noise_policy(p.get("title"), p.get("full_text"))]

    by_region: dict[str, list[dict]] = defaultdict(list)
    for p in rows:
        by_region[p.get("region") or "全国"].append(p)

    n = 0
    for parent, children in REGION_CHILDREN.items():
        parent_pols = by_region.get(parent, [])
        for child_region in children:
            child_pols = by_region.get(child_region, [])
            if not parent_pols or not child_pols:
                continue
            # 按 category_l1 对齐
            child_by_cat: dict[str, list[dict]] = defaultdict(list)
            for c in child_pols:
                child_by_cat[c.get("category_l1") or "其他"].append(c)
            for pp in parent_pols:
                cat = pp.get("category_l1") or "其他"
                if cat == "其他":
                    continue
                for cp in child_by_cat.get(cat, [])[:5]:
                    ok = await upsert_relation(
                        pp["id"],
                        cp["id"],
                        "region_child",
                        confidence=0.55,
                        evidence=f"行政区划：{parent}→{child_region}，同类「{cat}」",
                        build_source="rule",
                    )
                    if ok:
                        n += 1
    print(f"[RelationBuilder] region_child edges≈{n}", flush=True)
    return n


def _title_doctype(title: str) -> str:
    """粗分文种：notice / list / implement / other。"""
    if any(k in title for k in _IMPLEMENT_KW):
        return "implement"
    if any(k in title for k in _LIST_KW):
        return "list"
    if any(k in title for k in _NOTICE_KW):
        return "notice"
    return "other"


def _title_prefix(title: str, n: int = 10) -> str:
    """标题前缀（去书名号/空格），用于簇键。"""
    t = re.sub(r"[《》\s　]", "", title or "")
    return t[:n]


async def build_doctype_edges(policies: list[dict] | None = None) -> int:
    """规则建边：同 region+category 下文种配对。

    - notice/implement → list  : lists_under（名单从属于通知/办法）
    - implement → notice       : implements（细则实施办法/通知）
    """
    rows = policies if policies is not None else await _load_policy_rows()
    rows = [p for p in rows if not is_noise_policy(p.get("title"), p.get("full_text"))]

    groups: dict[tuple, list[dict]] = defaultdict(list)
    for p in rows:
        key = (p.get("region") or "", p.get("category_l1") or "其他")
        if key[1] == "其他":
            continue
        groups[key].append(p)

    n = 0
    for (_, cat), group in groups.items():
        notices = [p for p in group if _title_doctype(p.get("title", "")) == "notice"]
        lists_ = [p for p in group if _title_doctype(p.get("title", "")) == "list"]
        implements = [p for p in group if _title_doctype(p.get("title", "")) == "implement"]

        for notice in notices[:20]:
            for lst in lists_[:10]:
                if notice["id"] == lst["id"]:
                    continue
                # 标题前缀有交集再连，降低误连
                if _title_prefix(notice["title"], 6) not in (lst.get("title") or "") and \
                   _title_prefix(lst["title"], 6) not in (notice.get("title") or ""):
                    # 同发布单位放宽
                    if (notice.get("publisher") or "") != (lst.get("publisher") or ""):
                        continue
                ok = await upsert_relation(
                    lst["id"],
                    notice["id"],
                    "lists_under",
                    confidence=0.5,
                    evidence=f"文种：公示/名单从属于通知/办法（{cat}）",
                    build_source="rule",
                )
                if ok:
                    n += 1

        for impl in implements[:15]:
            for notice in notices[:15]:
                if impl["id"] == notice["id"]:
                    continue
                same_pub = (impl.get("publisher") or "") == (notice.get("publisher") or "")
                prefix_hit = (
                    _title_prefix(impl["title"], 6) in (notice.get("title") or "")
                    or _title_prefix(notice["title"], 6) in (impl.get("title") or "")
                )
                if not (same_pub or prefix_hit):
                    continue
                ok = await upsert_relation(
                    impl["id"],
                    notice["id"],
                    "implements",
                    confidence=0.6 if prefix_hit else 0.45,
                    evidence=f"文种：实施细则/办法实施上级通知（{cat}）",
                    build_source="rule",
                )
                if ok:
                    n += 1

    print(f"[RelationBuilder] doctype edges≈{n}", flush=True)
    return n


async def build_title_cluster_edges(policies: list[dict] | None = None) -> int:
    """规则建边：同 category_l1 + 标题前 10 字相同 → same_program。

    流程：
      1. 过滤噪声与「其他」类
      2. 簇 size∈[2, 12] 才建边（过大多为噪声批）
      3. 簇内按 id 排序，两两连边（有限条数）
    """
    rows = policies if policies is not None else await _load_policy_rows()
    clusters: dict[tuple, list[dict]] = defaultdict(list)
    for p in rows:
        if is_noise_policy(p.get("title"), p.get("full_text")):
            continue
        cat = p.get("category_l1") or "其他"
        if cat == "其他":
            continue
        key = (cat, _title_prefix(p.get("title", ""), 10))
        if len(key[1]) < 4:
            continue
        clusters[key].append(p)

    n = 0
    for (cat, prefix), group in clusters.items():
        if len(group) < 2 or len(group) > 12:
            continue
        group = sorted(group, key=lambda x: x["id"])
        for i, a in enumerate(group):
            for b in group[i + 1: i + 4]:  # 每点最多连后 3 个，控边密度
                ok = await upsert_relation(
                    a["id"],
                    b["id"],
                    "same_program",
                    confidence=0.65,
                    evidence=f"标题簇「{prefix}…」同类{cat}",
                    build_source="title_cluster",
                )
                if ok:
                    n += 1
    print(f"[RelationBuilder] title_cluster same_program≈{n}", flush=True)
    return n


_LLM_EXTRACT_SYSTEM = """你是政策知识图谱抽边专家。根据「种子政策」与「候选政策列表」，判断文档级关系。
只允许以下 relation_type：
implements, same_program, supersedes, amends, lists_under, funding_of, complements

输出 JSON 数组，每项：
{"target_id": 数字, "relation_type": "...", "confidence": 0.0~1.0, "evidence": "不超过40字"}

规则：
- 无把握不要硬连，可输出 []
- supersedes：明确废止/替代旧文时才用
- same_program：同一认定/专项/补贴的不同文件
- implements：下级实施细则落实上级文件
只输出 JSON，不要 markdown。"""


async def llm_extract_edges(
    seed: dict,
    candidates: list[dict],
    *,
    max_candidates: int = 8,
) -> int:
    """LightRAG 风格：对单条种子 + 候选，用 Fast LLM 抽文档级边。

    流程：
      1. 过滤噪声与自身；截断候选
      2. 拼紧凑 prompt（标题/地区/分类/摘要前 200 字）
      3. temperature=0 调 Fast LLM，解析 JSON 并 upsert

    返回：
      成功写入边数量
    """
    if not seed or not candidates:
        return 0
    if is_noise_policy(seed.get("title"), seed.get("full_text")):
        return 0

    cands = [
        c for c in candidates
        if c.get("id") != seed.get("id")
        and not is_noise_policy(c.get("title"), c.get("full_text"))
    ][:max_candidates]
    if not cands:
        return 0

    seed_block = (
        f"种子 id={seed['id']} 《{seed.get('title','')[:40]}》 "
        f"地区={seed.get('region')} 分类={seed.get('category_l1')}\n"
        f"摘要：{(seed.get('summary') or seed.get('full_text') or '')[:200]}"
    )
    cand_lines = []
    for c in cands:
        cand_lines.append(
            f"- id={c['id']} 《{(c.get('title') or '')[:36]}》 "
            f"地区={c.get('region')} 分类={c.get('category_l1')} "
            f"摘要：{(c.get('summary') or c.get('full_text') or '')[:120]}"
        )

    s = get_settings()
    llm = ChatOpenAI(
        model=s.fast_llm_model,
        api_key=s.llm_api_key,
        base_url=s.llm_base_url,
        temperature=0,
    )
    try:
        resp = await llm.ainvoke([
            SystemMessage(content=_LLM_EXTRACT_SYSTEM),
            HumanMessage(content=seed_block + "\n\n候选：\n" + "\n".join(cand_lines)),
        ])
        raw = re.sub(r"```json\s*|\s*```", "", (resp.content or "").strip())
        items = json.loads(raw)
        if not isinstance(items, list):
            return 0
    except Exception as e:
        print(f"[RelationBuilder] LLM 抽边失败 seed={seed.get('id')}: {e}", flush=True)
        return 0

    valid_ids = {c["id"] for c in cands}
    n = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        tid = item.get("target_id")
        rtype = item.get("relation_type")
        if tid not in valid_ids or rtype not in RELATION_TYPES:
            continue
        conf = float(item.get("confidence") or 0.5)
        conf = max(0.0, min(1.0, conf))
        ok = await upsert_relation(
            int(seed["id"]),
            int(tid),
            rtype,
            confidence=conf,
            evidence=str(item.get("evidence", ""))[:200],
            build_source="llm",
        )
        if ok:
            n += 1
    return n


async def build_all_relations(
    *,
    use_llm: bool = False,
    llm_max_seeds: int = 40,
    limit: int | None = None,
) -> dict:
    """存量建边主入口：规则三件套 + 可选 LLM 抽边。

    流程：
      1. 加载政策（可选 limit）
      2. region_child → doctype → title_cluster
      3. use_llm 时：对标题簇代表种子调 llm_extract_edges（控成本）

    参数：
      use_llm:       是否启用 LLM（默认 False，脚本可打开）
      llm_max_seeds: LLM 最多处理种子数
      limit:         调试时限制加载条数

    返回：
      统计 dict：各阶段边数 / 总边数查询值
    """
    policies = await _load_policy_rows(limit=limit)
    stats = {
        "loaded": len(policies),
        "region_child": await build_region_child_edges(policies),
        "doctype": await build_doctype_edges(policies),
        "title_cluster": await build_title_cluster_edges(policies),
        "llm": 0,
    }

    if use_llm:
        # 优先对非「其他」且有同类邻居的政策抽边
        by_cat: dict[str, list[dict]] = defaultdict(list)
        for p in policies:
            if is_noise_policy(p.get("title"), p.get("full_text")):
                continue
            cat = p.get("category_l1") or "其他"
            if cat != "其他":
                by_cat[cat].append(p)

        seeds_done = 0
        llm_n = 0
        for cat, group in by_cat.items():
            if seeds_done >= llm_max_seeds:
                break
            # 同地区优先作候选
            for seed in group[:8]:
                if seeds_done >= llm_max_seeds:
                    break
                region = seed.get("region")
                cands = [
                    x for x in group
                    if x["id"] != seed["id"]
                    and (x.get("region") == region or x.get("region") in ("全国", region))
                ][:8]
                if len(cands) < 1:
                    continue
                llm_n += await llm_extract_edges(seed, cands)
                seeds_done += 1
        stats["llm"] = llm_n

    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM policy_relations")
    stats["total_edges"] = int(total or 0)
    print(f"[RelationBuilder] build_all done: {stats}", flush=True)
    return stats
