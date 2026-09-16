"""PolicyRadar FastAPI 入口（端口 8001，与 ShillGuard 8000 隔离）"""
import os
from pathlib import Path
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

# 关闭 telemetry（最先设置）
os.environ.setdefault("MEM0_TELEMETRY", "false")
os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

from app.config import get_settings
from app.db.init import init_db
from app.db.postgres import close_pool
from app.rag.es_client import ensure_indices
from app.crawler.scheduler import create_scheduler
from app.api import categories, policies, report, crawl
from app.api import chat, feedback, admin_kb
from app.reflexion.scheduler import create_reflexion_job


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── 启动 ──────────────────────────────────────────────────
    # 完整 Agent 流程总览写在 app/db/init.py → init_db() 文档字符串
    # （报告 Agent / 对话 Agent / 数据供给 / 反馈与 Reflexion）
    print("[PolicyRadar] 初始化数据库...", flush=True)
    await init_db()

    print("[PolicyRadar] 初始化 ES 索引...", flush=True)
    try:
        await ensure_indices()
    except Exception as e:
        print(f"[PolicyRadar] ES 初始化失败（可继续运行）: {e}", flush=True)

    # 预热 LangGraph checkpointer（首次调用时初始化）
    print("[PolicyRadar] 预热对话 Agent...", flush=True)
    try:
        from app.agents.chat.graph import get_graph
        await get_graph()
    except Exception as e:
        print(f"[PolicyRadar] Agent 预热失败（可继续运行）: {e}", flush=True)

    # 预热 mem0
    print("[PolicyRadar] 初始化 mem0...", flush=True)
    try:
        from app.core.memory import get_mem0
        get_mem0()
    except Exception as e:
        print(f"[PolicyRadar] mem0 初始化失败（可继续运行）: {e}", flush=True)

    # 启动 Langfuse（触发一次即完成初始化和连通性验证）
    print("[PolicyRadar] 初始化 Langfuse 可观测性...", flush=True)
    try:
        from app.core.observability import get_callbacks
        get_callbacks()   # 内部打印 "[Langfuse] 已启用" 或 "未配置"
    except Exception as e:
        print(f"[PolicyRadar] Langfuse 初始化失败（可继续运行）: {e}", flush=True)

    # 启动调度器
    scheduler = create_scheduler()
    create_reflexion_job(scheduler)  # 注册 Reflexion 周期任务
    scheduler.start()
    print("[PolicyRadar] 调度器已启动（爬虫 + Reflexion）", flush=True)

    yield

    # ── 关闭 ──────────────────────────────────────────────────
    scheduler.shutdown(wait=False)
    await close_pool()
    print("[PolicyRadar] 已关闭", flush=True)


app = FastAPI(
    title="PolicyRadar API",
    description="政策情报 Agent 系统 - 后端服务（含对话 Agent + Reflexion 自进化）",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 注册路由 ───────────────────────────────────────────────────
app.include_router(categories.router, prefix="/api/v1")
app.include_router(policies.router,   prefix="/api/v1")
app.include_router(report.router,     prefix="/api/v1")
app.include_router(crawl.router,      prefix="/api/v1")
app.include_router(admin_kb.router,   prefix="/api/v1")  # 管理端知识库上传/建图
app.include_router(chat.router)        # /ai/chat, /ai/chat/history/*, /ai/chat/conversations
app.include_router(feedback.router)    # /ai/feedback, /ai/feedback/stats


@app.get("/health")
async def health():
    return {"status": "ok", "service": "policy-radar", "version": "2.0.0"}


@app.get("/")
async def root():
    """默认进入独立管理前端（无鉴权）。"""
    return RedirectResponse(url="/admin/")


@app.post("/api/v1/admin/reflexion/trigger")
async def trigger_reflexion(dry_run: bool = True):
    """手动触发一次 Reflexion 优化（管理接口）"""
    from app.reflexion.scheduler import trigger_now
    result = await trigger_now(dry_run=dry_run)
    return result


# 独立管理前端：http://localhost:8001/admin/（须放在 /admin API 路由之后）
_ADMIN_STATIC = Path(__file__).resolve().parent / "static" / "admin"
app.mount(
    "/admin",
    StaticFiles(directory=str(_ADMIN_STATIC), html=True),
    name="admin",
)


if __name__ == "__main__":
    s = get_settings()
    uvicorn.run("app.main:app", host="0.0.0.0", port=s.app_port, reload=True)
