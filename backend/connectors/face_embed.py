"""
face_embed.py — Local Face Detection & Embedding
==================================================
DeepFace-powered face detection, embedding, and Qdrant storage.
Faces NEVER leave the box — all processing is local.
Cross-investigation matching via vector similarity in Qdrant.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import uuid
from typing import Any

log = logging.getLogger("face_embed")

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
FACE_COLLECTION = "samaritan_faces"
EMBEDDING_DIM = 512  # ArcFace produces 512-dim vectors


class FaceEmbedConnector:
    """Local face detection, embedding, and vector-similarity search."""

    def __init__(self) -> None:
        self._qdrant = None

    def _get_qdrant(self) -> Any:
        """Lazy-init Qdrant client."""
        if self._qdrant is None:
            try:
                from qdrant_client import QdrantClient
                self._qdrant = QdrantClient(url=QDRANT_URL)
                # Ensure collection exists
                try:
                    self._qdrant.get_collection(FACE_COLLECTION)
                except Exception:
                    from qdrant_client.models import VectorParams, Distance
                    self._qdrant.create_collection(
                        collection_name=FACE_COLLECTION,
                        vectors_config=VectorParams(
                            size=EMBEDDING_DIM,
                            distance=Distance.COSINE,
                        ),
                    )
            except ImportError:
                log.warning("qdrant-client not installed")
        return self._qdrant

    async def detect_faces(self, image_path: str) -> list[dict[str, Any]]:
        """Detect faces in an image. Returns bounding boxes + confidence."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._detect_sync, image_path)

    def _detect_sync(self, image_path: str) -> list[dict[str, Any]]:
        try:
            from deepface import DeepFace
            faces = DeepFace.extract_faces(
                img_path=image_path,
                detector_backend="retinaface",
                enforce_detection=False,
            )
            results = []
            for face in faces:
                area = face.get("facial_area", {})
                results.append({
                    "bbox": {
                        "x": area.get("x", 0),
                        "y": area.get("y", 0),
                        "w": area.get("w", 0),
                        "h": area.get("h", 0),
                    },
                    "confidence": face.get("confidence", 0.0),
                })
            return results
        except ImportError:
            return [{"error": "deepface not installed — pip install deepface"}]
        except Exception as e:
            return [{"error": str(e)}]

    async def embed_face(self, image_path: str) -> list[float] | None:
        """Generate a 512-dim face embedding using ArcFace. Returns None on failure."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._embed_sync, image_path)

    def _embed_sync(self, image_path: str) -> list[float] | None:
        try:
            from deepface import DeepFace
            embeddings = DeepFace.represent(
                img_path=image_path,
                model_name="ArcFace",
                detector_backend="retinaface",
                enforce_detection=False,
            )
            if embeddings:
                return embeddings[0].get("embedding")
            return None
        except ImportError:
            log.warning("deepface not installed")
            return None
        except Exception as e:
            log.warning("Face embedding failed: %s", e)
            return None

    @staticmethod
    def compare_faces(emb1: list[float], emb2: list[float]) -> float:
        """Cosine similarity between two embedding vectors. Returns 0.0–1.0."""
        dot = sum(a * b for a, b in zip(emb1, emb2))
        norm1 = math.sqrt(sum(a * a for a in emb1))
        norm2 = math.sqrt(sum(b * b for b in emb2))
        if norm1 == 0 or norm2 == 0:
            return 0.0
        # Cosine similarity [-1, 1] → normalized to [0, 1]
        return max(0.0, min(1.0, (dot / (norm1 * norm2) + 1.0) / 2.0))

    def store_embedding(
        self,
        embedding: list[float],
        metadata: dict[str, Any],
        collection: str = FACE_COLLECTION,
    ) -> str | None:
        """Store a face embedding in Qdrant. Returns the point ID."""
        qdrant = self._get_qdrant()
        if qdrant is None:
            return None
        try:
            from qdrant_client.models import PointStruct
            point_id = str(uuid.uuid4())
            qdrant.upsert(
                collection_name=collection,
                points=[
                    PointStruct(
                        id=point_id,
                        vector=embedding,
                        payload=metadata,
                    )
                ],
            )
            return point_id
        except Exception as e:
            log.warning("Qdrant store failed: %s", e)
            return None

    def search_similar(
        self,
        embedding: list[float],
        collection: str = FACE_COLLECTION,
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """Search Qdrant for similar face embeddings."""
        qdrant = self._get_qdrant()
        if qdrant is None:
            return []
        try:
            hits = qdrant.search(
                collection_name=collection,
                query_vector=embedding,
                limit=top_k,
            )
            return [
                {"score": hit.score, "metadata": hit.payload, "id": str(hit.id)}
                for hit in hits
            ]
        except Exception as e:
            log.warning("Qdrant search failed: %s", e)
            return []


async def run_face_embed(image_path: str, **kwargs: Any) -> dict[str, Any]:
    """Top-level entry point: detect faces, embed, store, and search for matches."""
    connector = FaceEmbedConnector()
    inv_id = kwargs.get("investigation_id", "unknown")
    source_url = kwargs.get("source_url", image_path)
    store = kwargs.get("store", True)
    search = kwargs.get("search", True)

    # Detect faces
    faces = await connector.detect_faces(image_path)
    if faces and "error" in faces[0]:
        return {
            "faces_detected": 0,
            "faces": faces,
            "matches": [],
            "error": faces[0]["error"],
        }

    # Embed the primary face
    embedding = await connector.embed_face(image_path)
    matches: list[dict] = []

    if embedding:
        # Store in Qdrant
        point_id = None
        if store:
            point_id = connector.store_embedding(
                embedding,
                metadata={
                    "investigation_id": inv_id,
                    "source_url": source_url,
                    "person_name": kwargs.get("person_name"),
                },
            )

        # Search for similar faces
        if search:
            matches = connector.search_similar(embedding)

    return {
        "faces_detected": len(faces),
        "faces": [
            {**f, "embedding_stored": embedding is not None and store}
            for f in faces
        ],
        "matches": matches,
    }
