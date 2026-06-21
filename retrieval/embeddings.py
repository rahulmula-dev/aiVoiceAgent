"""
retrieval/embeddings.py — Shared embedding generation for the RAG pipeline.

Two modes:
  Production  (local_test=False): calls AWS Bedrock Titan Text Embeddings v2,
                                   returns a real 1536-dim vector.
  Local/Test  (local_test=True):  returns [1.0]*1536 so the full pgvector
                                   pipeline runs without AWS credentials.

Used by both migrate_to_pgvector.py (ingest time) and vector_store.py
(query time) — a single shared function guarantees both sides embed text
in the same vector space, which is required for cosine similarity to work.
"""

import json
import logging
from typing import List

logger = logging.getLogger("Embeddings")


async def get_bedrock_embeddings(
    text: str,
    region: str = "ca-central-1",
    local_test: bool = True,
) -> List[float]:
    """
    Return a 1536-dimensional embedding for `text`.

    In local_test mode returns a synthetic all-ones vector instantly.
    In production mode calls Bedrock Titan v2 and validates the dimension.
    Any Bedrock error is re-raised so the caller decides how to handle it.
    """
    if local_test:
        return [1.0] * 1536

    try:
        import boto3

        bedrock = boto3.client(service_name="bedrock-runtime", region_name=region)
        body = json.dumps({
            "inputText": text,
            "dimensions": 1536,
            "normalize": True,
        })
        response = bedrock.invoke_model(
            body=body,
            modelId="amazon.titan-embed-text-v2:0",
            accept="application/json",
            contentType="application/json",
        )
        response_body = json.loads(response.get("body").read())
        embedding = response_body.get("embedding")

        if not embedding or len(embedding) != 1536:
            raise ValueError(
                f"Bedrock returned unexpected dimension: "
                f"{len(embedding) if embedding else 0} (expected 1536)"
            )
        return embedding

    except Exception as e:
        logger.error(f"Bedrock embedding failed: {e}")
        raise
