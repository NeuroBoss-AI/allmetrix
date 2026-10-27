"""Adapted public excerpt of the memory collection setup.

See NOTICE.md for terms. The caller owns the client lifetime.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import (
    DatetimeIndexParams,
    DatetimeIndexType,
    Distance,
    KeywordIndexParams,
    KeywordIndexType,
    Modifier,
    SparseVectorParams,
    VectorParams,
)


@dataclass(frozen=True)
class CollectionSettings:
    """Names and dense size for one hybrid vector collection."""

    dense_size: int
    collection_name: str = "memory_chunks"
    dense_vector_name: str = "dense"
    sparse_vector_name: str = "sparse"

    def __post_init__(self) -> None:
        """Reject an invalid size or vector name before database access."""
        if type(self.dense_size) is not int or self.dense_size <= 0:
            raise ValueError("dense_size must be a positive integer")
        names = (
            self.collection_name,
            self.dense_vector_name,
            self.sparse_vector_name,
        )
        if any(not isinstance(name, str) or not name.strip() for name in names):
            raise ValueError("Collection and vector names must not be empty")
        if self.dense_vector_name == self.sparse_vector_name:
            raise ValueError("Dense and sparse vector names must differ")


def create_qdrant_client(
    *,
    url: str = "http://localhost:6333",
    api_key: str | None = None,
    timeout_seconds: float = 10.0,
) -> AsyncQdrantClient:
    """Create a client with a positive timeout; the caller must close it."""
    if not url.strip():
        raise ValueError("url must not be empty")
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    # === The client accepts whole seconds; preserve a positive timeout. ===
    return AsyncQdrantClient(
        url=url, api_key=api_key, timeout=math.ceil(timeout_seconds)
    )


async def _check_named_vectors(
    client: AsyncQdrantClient, settings: CollectionSettings
) -> None:
    """Reject an existing collection with an incompatible vector schema."""
    info = await client.get_collection(settings.collection_name)
    vectors = info.config.params.vectors
    sparse_vectors = info.config.params.sparse_vectors or {}
    if not isinstance(vectors, dict):
        raise RuntimeError("Collection must have named dense vectors")
    dense = vectors.get(settings.dense_vector_name)
    sparse = sparse_vectors.get(settings.sparse_vector_name)
    errors: list[str] = []
    if dense is None:
        errors.append("missing dense vector")
    else:
        if dense.size != settings.dense_size:
            errors.append("dense size does not match settings")
        if dense.distance != Distance.COSINE:
            errors.append("dense distance must be cosine")
    if sparse is None:
        errors.append("missing sparse vector")
    elif sparse.modifier != Modifier.IDF:
        errors.append("sparse modifier must be IDF")
    if errors:
        raise RuntimeError("Incompatible vector schema: " + "; ".join(errors))


async def _create_collection(
    client: AsyncQdrantClient, settings: CollectionSettings
) -> None:
    """Create named dense and sparse vectors; allow a concurrent create."""
    try:
        await client.create_collection(
            collection_name=settings.collection_name,
            vectors_config={
                settings.dense_vector_name: VectorParams(
                    size=settings.dense_size, distance=Distance.COSINE
                ),
            },
            sparse_vectors_config={
                settings.sparse_vector_name: SparseVectorParams(modifier=Modifier.IDF),
            },
        )
    except UnexpectedResponse as exc:
        # === Only a create conflict can be a race; verify its schema next. ===
        if exc.status_code != 409:
            raise


async def _create_indexes(
    client: AsyncQdrantClient, settings: CollectionSettings
) -> None:
    """Create tenant, owner, and date indexes; propagate database errors."""
    indexes: tuple[tuple[str, KeywordIndexParams | DatetimeIndexParams], ...] = (
        (
            "tenant_id",
            KeywordIndexParams(type=KeywordIndexType.KEYWORD, is_tenant=True),
        ),
        ("owner_id", KeywordIndexParams(type=KeywordIndexType.KEYWORD)),
        ("created_at", DatetimeIndexParams(type=DatetimeIndexType.DATETIME)),
    )
    for field_name, schema in indexes:
        await client.create_payload_index(
            collection_name=settings.collection_name,
            field_name=field_name,
            field_schema=schema,
            wait=True,
        )


async def ensure_collection(
    client: AsyncQdrantClient, settings: CollectionSettings
) -> None:
    """Create or check the hybrid collection, then create its payload indexes."""
    if not await client.collection_exists(settings.collection_name):
        await _create_collection(client, settings)
    await _check_named_vectors(client, settings)
    await _create_indexes(client, settings)
