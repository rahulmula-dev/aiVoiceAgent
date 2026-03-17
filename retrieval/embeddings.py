import os
import json
import logging
import asyncio
from typing import List

logger = logging.getLogger("Embeddings")

async def get_bedrock_embeddings(text: str, region: str = "ca-central-1", local_test: bool = True) -> List[float]:
    """
    Shared embedding function to ensure consistency between ingestion and retrieval (H5 point 1).
    Titan v2 embeddings are confirmed to be 1536 dimensions (H5 point 3).
    """
    if local_test:
        # Mock 1536-dim vector for local testing
        return [1.0] * 1536

    try:
        import boto3
        # Use simple caching or pooling if needed in the future
        bedrock = boto3.client(service_name="bedrock-runtime", region_name=region)
        body = json.dumps({
            "inputText": text,
            "dimensions": 1536,
            "normalize": True
        })
        
        # Bedrock invoke_model is synchronous, wrap in executor if needed for high perf
        # But for migration and small queries, direct is fine in this async wrapper
        response = bedrock.invoke_model(
            body=body,
            modelId="amazon.titan-embed-text-v2:0",
            accept="application/json",
            contentType="application/json"
        )
        response_body = json.loads(response.get("body").read())
        embedding = response_body.get("embedding")
        
        # Point 2 check: Strict dimension validation
        if not embedding or len(embedding) != 1536:
            raise ValueError(f"Bedrock returned invalid embedding dimension: {len(embedding) if embedding else 0}. Expected 1536.")
            
        return embedding
    except Exception as e:
        logger.error(f"Bedrock API call failed: {e}")
        # We RAISING here to allow caller (like the Migrator) to decide if to abort (H5 point 2)
        raise
