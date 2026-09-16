"""分类树接口"""
from fastapi import APIRouter
from app.db.postgres import get_pool
from app.db.schemas import CategoryNode

router = APIRouter(prefix="/categories", tags=["categories"])


def _build_tree(rows: list[dict]) -> list[CategoryNode]:
    nodes: dict[int, CategoryNode] = {}
    for r in rows:
        nodes[r["id"]] = CategoryNode(
            id=r["id"],
            code=r["code"],
            label=r["label"],
            icon=r["icon"],
            level=r["level"],
            policy_count=r["policy_count"],
        )
    roots = []
    for r in rows:
        node = nodes[r["id"]]
        if r["parent_id"] is None:
            roots.append(node)
        else:
            parent = nodes.get(r["parent_id"])
            if parent:
                parent.children.append(node)
    return roots


@router.get("", response_model=list[CategoryNode])
async def get_category_tree():
    """返回完整分类树（4 级）"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, parent_id, level, code, label, icon, policy_count "
            "FROM categories ORDER BY level, sort_order, id"
        )
    return _build_tree([dict(r) for r in rows])
