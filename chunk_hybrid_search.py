"""Adapted public excerpt: tenant-scoped Qdrant hybrid search.

Embeddings and identity come from the caller. See NOTICE.md for terms.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from qdrant_chunks import CollectionSettings
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    Fusion,
    FusionQuery,
    MatchValue,
    PointStruct,
    Prefetch,
    ScoredPoint,
    SparseVector,
)


@dataclass(frozen=True)
class SearchScope:
    """Identity from trusted application context, never unchecked request fields."""

    tenant_id: str
    owner_id: str

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.owner_id.strip():
            raise ValueError("Tenant and owner IDs must not be empty")

    def get_filter(self) -> Filter:
        """Require both tenant and owner on every read."""
        return Filter(
            must=[
                FieldCondition(key="tenant_id", match=MatchValue(value=self.tenant_id)),
                FieldCondition(key="owner_id", match=MatchValue(value=self.owner_id)),
            ]
        )


class ChunkHybridSearch:
    """Store supplied vectors and combine dense and sparse search results."""

    def __init__(
        self,
        client: AsyncQdrantClient,
        settings: CollectionSettings,
        *,
        candidate_limit: int = 64,
    ) -> None:
        if candidate_limit < 1:
            raise ValueError("Candidate limit must be positive")
        self._client = client
        self._settings = settings
        self._candidate_limit = candidate_limit

    def _check_vectors(self, dense: list[float], sparse: SparseVector) -> None:
        """Reject invalid vectors before any database request."""
        if len(dense) != self._settings.dense_size:
            raise ValueError("Dense vector size does not match collection settings")
        if not all(math.isfinite(value) for value in dense) or not any(dense):
            raise ValueError("Dense vector must be finite and nonzero")
        if len(sparse.indices) != len(sparse.values):
            raise ValueError("Sparse indices and values must have equal lengths")
        if len(set(sparse.indices)) != len(sparse.indices):
            raise ValueError("Sparse indices must be unique")
        if any(index < 0 for index in sparse.indices):
            raise ValueError("Sparse indices must be nonnegative")
        if not all(math.isfinite(value) for value in sparse.values):
            raise ValueError("Sparse values must be finite")

    async def save_memory(
        self,
        *,
        scope: SearchScope,
        text: str,
        dense: list[float],
        sparse: SparseVector,
    ) -> str:
        """Store one memory; repeat saves of the same scoped text replace it."""
        self._check_vectors(dense, sparse)
        text = text.strip()
        if not text:
            raise ValueError("Memory text must not be empty")
        # === Encode boundaries so distinct identities cannot share an ID. ===
        key = json.dumps([scope.tenant_id, scope.owner_id, text], ensure_ascii=False)
        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
        point = PointStruct(
            id=point_id,
            vector={
                self._settings.dense_vector_name: dense,
                self._settings.sparse_vector_name: sparse,
            },
            payload={
                "tenant_id": scope.tenant_id,
                "owner_id": scope.owner_id,
                "text": text,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        await self._client.upsert(
            collection_name=self._settings.collection_name,
            points=[point],
            wait=True,
        )
        return point_id

    def _get_prefetch(
        self, dense: list[float], sparse: SparseVector, query_filter: Filter
    ) -> list[Prefetch]:
        """Keep candidate depth fixed across result pages."""
        prefetch = [
            Prefetch(
                query=dense,
                using=self._settings.dense_vector_name,
                filter=query_filter,
                limit=self._candidate_limit,
            )
        ]
        if sparse.indices:
            prefetch.append(
                Prefetch(
                    query=sparse,
                    using=self._settings.sparse_vector_name,
                    filter=query_filter,
                    limit=self._candidate_limit,
                )
            )
        return prefetch

    async def search_memories(
        self,
        *,
        scope: SearchScope,
        dense: list[float],
        sparse: SparseVector,
        limit: int = 10,
        offset: int = 0,
    ) -> list[ScoredPoint]:
        """Return one page of reciprocal-rank-fusion results within the scope."""
        self._check_vectors(dense, sparse)
        if limit < 1 or offset < 0 or offset + limit > self._candidate_limit:
            raise ValueError("Page must fit within the fixed candidate limit")
        query_filter = scope.get_filter()
        response = await self._client.query_points(
            collection_name=self._settings.collection_name,
            prefetch=self._get_prefetch(dense, sparse, query_filter),
            query=FusionQuery(fusion=Fusion.RRF),
            query_filter=query_filter,
            limit=limit,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        return list(response.points)
