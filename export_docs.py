"""
export_docs.py
将数据库 policies 表中的所有政策文章导出为 Markdown 文件，存放在 docs/ 目录下。

用法（在 policy-radar 根目录，激活 conda agent 环境后）：
    python export_docs.py

可选参数：
    --out-dir  输出目录（默认 docs）
    --limit    只导出前 N 条（调试用，0 = 全量）
    --encoding 文件编码（默认 utf-8）
"""
import asyncio
import os
import re
import argparse
import asyncpg
from datetime import date
from dotenv import load_dotenv

load_dotenv()

# ── 数据库连接参数（直接读 .env）──────────────────────────────
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_DBNAME = os.getenv("PG_DBNAME", "policy_radar")
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")


def safe_filename(title: str, policy_id: int, max_len: int = 60) -> str:
    """将标题转为合法的文件名（保留中文/英文/数字/连字符）。"""
    name = re.sub(r'[\\/:*?"<>|]', "", title)   # 去掉 Windows 非法字符
    name = re.sub(r'\s+', "_", name.strip())     # 空格→下划线
    name = name[:max_len]                         # 截断
    return f"{policy_id:05d}_{name}" if name else f"{policy_id:05d}_policy"


def fmt_date(d) -> str:
    if d is None:
        return "—"
    if isinstance(d, date):
        return d.strftime("%Y-%m-%d")
    return str(d)


def build_markdown(row: asyncpg.Record) -> str:
    """将一行 policies 记录组装成 Markdown 字符串。"""
    title       = row["title"] or "（无标题）"
    publisher   = row["publisher"] or "—"
    region      = row["region"] or "—"
    pub_date    = fmt_date(row["pub_date"])
    deadline    = fmt_date(row["deadline"])
    source_url  = row["source_url"] or "—"
    summary     = (row["summary"] or "").strip()
    full_text   = (row["full_text"] or "").strip()
    category_l1 = row["category_l1"] or "—"
    category_l2 = row["category_l2"] or "—"
    funding     = row["funding_amount"] or "—"
    key_cond    = row["key_conditions"] or "—"

    keywords    = row["keywords"] or []
    kw_str      = "、".join(keywords) if keywords else "—"

    policy_types = row["policy_types"] or []
    pt_str       = "、".join(policy_types) if policy_types else "—"

    lines = [
        f"# {title}",
        "",
        "## 基本信息",
        "",
        f"| 字段 | 内容 |",
        f"|------|------|",
        f"| 发布机构 | {publisher} |",
        f"| 适用地区 | {region} |",
        f"| 发布日期 | {pub_date} |",
        f"| 申报截止 | {deadline} |",
        f"| 一级分类 | {category_l1} |",
        f"| 二级分类 | {category_l2} |",
        f"| 政策类型 | {pt_str} |",
        f"| 资助金额 | {funding} |",
        f"| 关键词   | {kw_str} |",
        f"| 来源链接 | {source_url} |",
        "",
    ]

    if key_cond and key_cond != "—":
        lines += ["## 申报条件", "", key_cond, ""]

    if summary:
        lines += ["## 摘要", "", summary, ""]

    if full_text:
        lines += ["## 全文", "", full_text, ""]

    return "\n".join(lines)


async def main(out_dir: str, limit: int, encoding: str):
    os.makedirs(out_dir, exist_ok=True)

    conn = await asyncpg.connect(
        host=PG_HOST, port=PG_PORT,
        database=PG_DBNAME, user=PG_USER, password=PG_PASSWORD,
    )

    try:
        sql = """
            SELECT id, title, source_url, publisher, region,
                   pub_date, deadline, full_text, summary, keywords,
                   category_l1, category_l2, policy_types,
                   key_conditions, funding_amount
            FROM policies
            ORDER BY id
        """
        if limit > 0:
            sql += f" LIMIT {limit}"

        rows = await conn.fetch(sql)
        total = len(rows)
        print(f"[export] 共 {total} 条政策记录，开始写入 {out_dir}/")

        ok = 0
        for row in rows:
            pid = row["id"]
            fname = safe_filename(row["title"] or "", pid) + ".md"
            fpath = os.path.join(out_dir, fname)
            content = build_markdown(row)
            with open(fpath, "w", encoding=encoding, errors="replace") as f:
                f.write(content)
            ok += 1
            if ok % 50 == 0 or ok == total:
                print(f"  [{ok}/{total}] 已完成")

        print(f"\n[export] 全部完成！共写入 {ok} 个 .md 文件 → {os.path.abspath(out_dir)}/")

    finally:
        await conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导出 policies 表为 Markdown 文件")
    parser.add_argument("--out-dir",  default="docs",    help="输出目录（默认: docs）")
    parser.add_argument("--limit",    default=0, type=int, help="只导出前 N 条（0=全量）")
    parser.add_argument("--encoding", default="utf-8",   help="文件编码（默认: utf-8）")
    args = parser.parse_args()

    asyncio.run(main(args.out_dir, args.limit, args.encoding))
