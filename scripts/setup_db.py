"""
数据库初始化脚本：
1. 连接 postgres 主库，创建 policy_radar 数据库（如不存在）
2. 连接 policy_radar，启用 pgvector 扩展
3. 建立所有业务表

使用：
  python scripts/setup_db.py
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 从 .env 读取配置 ──────────────────────────────────────────
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = int(os.getenv("PG_PORT", "5432"))
PG_USER = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "")
PG_DBNAME = os.getenv("PG_DBNAME", "policy_radar")

print(f"[Setup] 连接信息: {PG_USER}@{PG_HOST}:{PG_PORT}/{PG_DBNAME}")


async def step1_create_database():
    """在 postgres 主库中创建 policy_radar 数据库"""
    import asyncpg
    # 连接到 postgres 默认库
    conn = await asyncpg.connect(
        host=PG_HOST, port=PG_PORT,
        database="postgres",
        user=PG_USER, password=PG_PASSWORD,
    )
    try:
        # 检查是否已存在
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", PG_DBNAME
        )
        if exists:
            print(f"[Setup] 数据库 '{PG_DBNAME}' 已存在，跳过创建")
        else:
            # CREATE DATABASE 不能在事务中执行
            await conn.execute(f'CREATE DATABASE "{PG_DBNAME}"')
            print(f"[Setup] 数据库 '{PG_DBNAME}' 创建成功")

        # 刷新 collation
        try:
            await conn.execute(f'ALTER DATABASE "{PG_DBNAME}" REFRESH COLLATION VERSION')
            print(f"[Setup] Collation 版本已刷新")
        except Exception as e:
            print(f"[Setup] Collation 刷新（可忽略）: {e}")
    finally:
        await conn.close()


async def step2_enable_extension():
    """连接 policy_radar，启用 pgvector"""
    import asyncpg
    conn = await asyncpg.connect(
        host=PG_HOST, port=PG_PORT,
        database=PG_DBNAME,
        user=PG_USER, password=PG_PASSWORD,
    )
    try:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        version = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname='vector'")
        print(f"[Setup] pgvector 扩展已启用，版本: {version}")
    finally:
        await conn.close()


async def step3_init_tables():
    """建立所有业务表"""
    from app.db.init import init_db
    await init_db()


async def main():
    print("\n===== PolicyRadar 数据库初始化 =====\n")
    try:
        print("[步骤 1] 创建数据库...")
        await step1_create_database()

        print("\n[步骤 2] 启用 pgvector 扩展...")
        await step2_enable_extension()

        print("\n[步骤 3] 建立业务表...")
        await step3_init_tables()

        print("\n===== 初始化完成！=====")
        print(f"可在数据库工具中新建连接：")
        print(f"  主机: {PG_HOST}:{PG_PORT}")
        print(f"  数据库: {PG_DBNAME}")
        print(f"  用户: {PG_USER}")

    except Exception as e:
        print(f"\n[ERROR] {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
