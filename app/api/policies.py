"""政策列表与详情接口"""
from fastapi import APIRouter, Query
from typing import Optional
from app.db.postgres import get_pool
from app.db.schemas import PolicyItem

router = APIRouter(prefix="/policies", tags=["policies"])


@router.get("", response_model=list[PolicyItem])
async def list_policies(
    region:    Optional[str] = Query(None),
    l1:        Optional[str] = Query(None),
    l2:        Optional[str] = Query(None),
    limit:     int = Query(20, le=100),
    offset:    int = Query(0),
):
    """政策列表（按评分降序）"""
    pool = await get_pool()
    filters = []
    params: list = []

    if region:
        params.append(region)
        filters.append(f"region = ANY(ARRAY[${len(params)}, '全国']::text[])")
    if l1:
        params.append(l1)
        filters.append(f"category_l1 = ${len(params)}")
    if l2:
        params.append(l2)
        filters.append(f"category_l2 = ${len(params)}")

    where = ("WHERE " + " AND ".join(filters)) if filters else ""
    params += [limit, offset]

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""SELECT id, title, publisher, region, pub_date, deadline,
                       summary, category_l1, category_l2, policy_types,
                       funding_amount, key_conditions,
                       score_relevance, score_urgency, score_value, source_url
                FROM policies {where}
                ORDER BY (score_relevance + score_urgency + score_value) / 3 DESC
                LIMIT ${len(params)-1} OFFSET ${len(params)}
            """,
            *params,
        )
    return [PolicyItem(**dict(r)) for r in rows]


@router.get("/{policy_id}", response_model=PolicyItem)
async def get_policy(policy_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, title, publisher, region, pub_date, deadline, "
            "summary, category_l1, category_l2, policy_types, "
            "funding_amount, key_conditions, "
            "score_relevance, score_urgency, score_value, source_url "
            "FROM policies WHERE id = $1",
            policy_id,
        )
    if not row:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="政策不存在")
    return PolicyItem(**dict(row))
