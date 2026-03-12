import asyncio
import asyncpg
import os
import json
from dotenv import load_dotenv

load_dotenv()
DB_URL = os.getenv("PG_DATABASE_URL")

async def list_data():
    conn = await asyncpg.connect(DB_URL)
    
    # Query to join documents, chunks and governance metadata
    query = """
        SELECT 
            d.title as doc_title,
            c.content,
            c.metadata->>'category' as category,
            gm.topic_tags
        FROM rag.chunks c
        JOIN rag.documents d ON c.document_id = d.id
        LEFT JOIN rag.governance_metadata gm ON c.id = gm.chunk_id
        ORDER BY d.title, c.content
        LIMIT 50;
    """
    
    rows = await conn.fetch(query)
    
    print("\n" + "="*80)
    print(f"{'DOCUMENT':<20} | {'CATEGORY':<12} | {'CONTENT SUMMARY'}")
    print("-" * 80)
    
    for row in rows:
        content_preview = (row['content'][:100].replace('\n', ' ') + '...') if len(row['content']) > 100 else row['content']
        title = row['doc_title'] or "Untitled"
        category = row['category'] or "N/A"
        
        print(f"{title[:20]:<20} | {category:<12} | {content_preview}")
    
    print("="*80)
    print(f"Total chunks retrieved: {len(rows)}")
    
    await conn.close()

if __name__ == "__main__":
    asyncio.run(list_data())
