from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import aiosqlite

from .vision_types import (
    ConfidenceBand,
    ExactSearchResult,
    ImageDecision,
    LensAllEvidence,
    LensSearchResult,
    SearchSource,
    VisionSynthesis,
)

_SYNTHESIS_CACHE_VERSION = "lens-first-synthesis-v1"
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


class VisionCache:
    def __init__(self, path: Path, *, ttl_seconds: int) -> None:
        self.path = path
        self.ttl_seconds = ttl_seconds

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS vision_cache (
                    image_hash TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (image_hash, stage, version)
                )
                """
            )
            import time

            await db.execute(
                "DELETE FROM vision_cache WHERE created_at < ?",
                (int(time.time()) - self.ttl_seconds,),
            )
            await db.commit()
        self.path.chmod(0o600)

    async def get_lens_all(
        self,
        image_hash: str,
        *,
        version: str,
        now: int,
    ) -> LensSearchResult | None:
        data = await self._get(image_hash, stage="lens_all", version=version, now=now)
        return self._decode_lens_all(data) if data is not None else None

    async def put_lens_all(
        self,
        image_hash: str,
        *,
        version: str,
        result: LensSearchResult,
        now: int,
    ) -> None:
        if not result.ok:
            return
        await self._put(
            image_hash,
            stage="lens_all",
            version=version,
            payload={
                "cached": result.cached,
                "evidence": {
                    "visual_matches": self._encode_sources(result.evidence.visual_matches),
                    "related_content": self._encode_sources(result.evidence.related_content),
                    "ai_overview": self._safe_text(result.evidence.ai_overview),
                },
            },
            now=now,
        )

    async def get_exact(
        self,
        image_hash: str,
        *,
        version: str,
        now: int,
    ) -> ExactSearchResult | None:
        data = await self._get(image_hash, stage="lens_exact", version=version, now=now)
        return self._decode_exact(data) if data is not None else None

    async def put_exact(
        self,
        image_hash: str,
        *,
        version: str,
        result: ExactSearchResult,
        now: int,
    ) -> None:
        if not result.ok:
            return
        await self._put(
            image_hash,
            stage="lens_exact",
            version=version,
            payload={
                "cached": result.cached,
                "sources": self._encode_sources(result.sources),
            },
            now=now,
        )

    @staticmethod
    def build_synthesis_key(
        *,
        image_hashes: tuple[str, ...],
        user_question: str,
        chat_context: str,
        model_name: str,
        prompt_version: str,
        lens_parser_version: str,
        schema_version: str,
    ) -> str:
        normalized_question = " ".join(user_question.split()).casefold()
        bounded_context = " ".join(chat_context.split())[:4000]
        context_digest = hashlib.sha256(bounded_context.encode("utf-8")).hexdigest()
        payload = json.dumps(
            {
                "image_hashes": image_hashes,
                "question": normalized_question,
                "context_digest": context_digest,
                "model_name": model_name,
                "prompt_version": prompt_version,
                "lens_parser_version": lens_parser_version,
                "schema_version": schema_version,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def get_synthesis(self, request_hash: str, *, now: int) -> VisionSynthesis | None:
        data = await self._get(
            request_hash,
            stage="synthesis",
            version=_SYNTHESIS_CACHE_VERSION,
            now=now,
        )
        return self._decode_synthesis(data) if data is not None else None

    async def put_synthesis(
        self,
        request_hash: str,
        *,
        synthesis: VisionSynthesis,
        now: int,
    ) -> None:
        await self._put(
            request_hash,
            stage="synthesis",
            version=_SYNTHESIS_CACHE_VERSION,
            payload=self._encode_synthesis(synthesis),
            now=now,
        )

    async def _get(
        self,
        image_hash: str,
        *,
        stage: str,
        version: str,
        now: int,
    ) -> dict[str, Any] | None:
        cutoff = now - self.ttl_seconds
        async with aiosqlite.connect(self.path) as db:
            rows = await db.execute_fetchall(
                """
                SELECT payload_json, created_at
                FROM vision_cache
                WHERE image_hash = ? AND stage = ? AND version = ?
                """,
                (image_hash, stage, version),
            )
        if not rows or int(rows[0][1]) < cutoff:
            return None
        try:
            data = json.loads(str(rows[0][0]))
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    async def _put(
        self,
        image_hash: str,
        *,
        stage: str,
        version: str,
        payload: dict[str, Any],
        now: int,
    ) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        cutoff = now - self.ttl_seconds
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM vision_cache WHERE created_at < ?", (cutoff,))
            await db.execute(
                """
                INSERT INTO vision_cache(image_hash, stage, version, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(image_hash, stage, version) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    created_at = excluded.created_at
                """,
                (image_hash, stage, version, encoded, now),
            )
            await db.commit()

    @classmethod
    def _decode_lens_all(cls, data: dict[str, Any]) -> LensSearchResult | None:
        evidence = data.get("evidence")
        if not isinstance(evidence, dict):
            return None
        try:
            return LensSearchResult(
                ok=True,
                evidence=LensAllEvidence(
                    visual_matches=cls._decode_sources(evidence.get("visual_matches")),
                    related_content=cls._decode_sources(evidence.get("related_content")),
                    ai_overview=str(evidence.get("ai_overview", "")),
                ),
                cached=bool(data.get("cached", False)),
            )
        except (TypeError, ValueError):
            return None

    @classmethod
    def _decode_exact(cls, data: dict[str, Any]) -> ExactSearchResult | None:
        try:
            return ExactSearchResult(
                ok=True,
                sources=cls._decode_sources(data.get("sources")),
                cached=bool(data.get("cached", False)),
            )
        except (TypeError, ValueError):
            return None

    @classmethod
    def _decode_synthesis(cls, data: dict[str, Any]) -> VisionSynthesis | None:
        raw_images = data.get("images")
        if not isinstance(raw_images, list):
            return None
        images = []
        try:
            for item in raw_images:
                if not isinstance(item, dict):
                    continue
                images.append(
                    ImageDecision(
                        image_number=int(item["image_number"]),
                        confidence=ConfidenceBand(str(item["confidence"])),
                        scene_description=str(item.get("scene_description", "")),
                        visible_text=tuple(str(text) for text in item.get("visible_text", [])),
                        subject_identity=str(item.get("subject_identity", "")),
                        work_or_affiliation=str(item.get("work_or_affiliation", "")),
                        source_series_or_author=str(item.get("source_series_or_author", "")),
                        reason=str(item.get("reason", "")),
                        needs_exact=bool(item.get("needs_exact", False)),
                        needs_web=bool(item.get("needs_web", False)),
                        verification_query=str(item.get("verification_query", "")),
                        question_answer=str(item.get("question_answer", "")),
                    )
                )
            return VisionSynthesis(
                images=tuple(images),
                combined_answer=str(data.get("combined_answer", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @classmethod
    def _decode_sources(cls, value: Any) -> tuple[SearchSource, ...]:
        if not isinstance(value, list):
            return ()
        sources = []
        for item in value:
            if not isinstance(item, dict):
                continue
            sources.append(
                SearchSource(
                    title=str(item.get("title", "")),
                    url=str(item.get("url", "")),
                    domain=str(item.get("domain", "")),
                    snippet=str(item.get("snippet", "")),
                    result_kind=str(item.get("result_kind", "")),
                )
            )
        return tuple(sources)

    @classmethod
    def _encode_sources(cls, sources: tuple[SearchSource, ...]) -> list[dict[str, str]]:
        return [
            {
                "title": cls._safe_text(source.title),
                "domain": cls._safe_text(source.domain),
                "snippet": cls._safe_text(source.snippet),
                "result_kind": cls._safe_text(source.result_kind),
            }
            for source in sources
        ]

    @classmethod
    def _encode_synthesis(cls, synthesis: VisionSynthesis) -> dict[str, Any]:
        return {
            "images": [
                {
                    "image_number": item.image_number,
                    "confidence": item.confidence.value,
                    "scene_description": cls._safe_text(item.scene_description),
                    "visible_text": [cls._safe_text(text) for text in item.visible_text],
                    "subject_identity": cls._safe_text(item.subject_identity),
                    "work_or_affiliation": cls._safe_text(item.work_or_affiliation),
                    "source_series_or_author": cls._safe_text(item.source_series_or_author),
                    "reason": cls._safe_text(item.reason),
                    "needs_exact": item.needs_exact,
                    "needs_web": item.needs_web,
                    "verification_query": cls._safe_text(item.verification_query),
                    "question_answer": _URL_RE.sub("", item.question_answer)[:2400],
                }
                for item in synthesis.images
            ],
            "combined_answer": cls._safe_text(synthesis.combined_answer),
        }

    @staticmethod
    def _safe_text(value: str) -> str:
        return " ".join(_URL_RE.sub("", str(value)).split())
