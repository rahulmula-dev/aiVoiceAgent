import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("PG_DATABASE_URL")

async def check():
    conn = await asyncpg.connect(DB_URL)
    tables = await conn.fetch("""
        SELECT table_schema, table_name 
        FROM information_schema.tables 
        WHERE table_name IN ('chunks', 'embeddings', 'documents');
    """)
    for t in tables:
        print(f"Schema: {t['table_schema']}, Table: {t['table_name']}")
        count = await conn.fetchval(f"SELECT COUNT(*) FROM {t['table_schema']}.{t['table_name']}")
        print(f"  Count: {count}")
    await conn.close()

if __name__ == "__main__":
    asyncio.run(check())
