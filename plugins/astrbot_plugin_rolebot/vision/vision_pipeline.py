from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..diagnostics import DebugTrace
from .image_preprocessor import NormalizedImage
from .vision_types import (
    ConfidenceBand,
    ExactSearchResult,
    ImageDecision,
    ImageFallbackEvidence,
    ImageLensResult,
    LensSearchResult,
    VisionObservation,
    VisionSynthesis,
)


@dataclass(frozen=True)
class VisionPipelineResult:
    ok: bool
    context_text: str
    synthesis: VisionSynthesis = VisionSynthesis()
    timed_out: bool = False
    error: str = ""


@dataclass(frozen=True)
class _PreparedImage:
    image_number: int
    source_url: str
    image: NormalizedImage


class VisionPipeline:
    def __init__(
        self,
        *,
        preprocessor: Any,
        analyzer: Any,
        lens_client: Any | None,
        web_client: Any | None,
        cache: Any,
        total_timeout_seconds: float,
        multi_timeout_seconds: float,
        lens_timeout_seconds: float,
        model_timeout_seconds: float,
        lens_concurrency: int,
        max_images: int,
        exact_fallback_enabled: bool,
        web_fallback_enabled: bool,
        max_exact_fallbacks: int,
        max_web_fallbacks: int,
        model_name: str,
        prompt_version: str,
        lens_parser_version: str,
        schema_version: str,
        now: Callable[[], int] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.preprocessor = preprocessor
        self.analyzer = analyzer
        self.lens_client = lens_client
        self.web_client = web_client
        self.cache = cache
        self.total_timeout_seconds = total_timeout_seconds
        self.multi_timeout_seconds = multi_timeout_seconds
        self.lens_timeout_seconds = lens_timeout_seconds
        self.model_timeout_seconds = model_timeout_seconds
        self.max_images = max_images
        self.exact_fallback_enabled = exact_fallback_enabled
        self.web_fallback_enabled = web_fallback_enabled
        self.max_exact_fallbacks = max_exact_fallbacks
        self.max_web_fallbacks = max_web_fallbacks
        self.model_name = model_name
        self.prompt_version = prompt_version
        self.lens_parser_version = lens_parser_version
        self.schema_version = schema_version
        self.now = now or (lambda: int(time.time()))
        self.monotonic = monotonic or time.monotonic
        self._lens_semaphore = asyncio.Semaphore(lens_concurrency)
        self._inflight_lock = asyncio.Lock()
        self._inflight_lens: dict[str, asyncio.Task[LensSearchResult]] = {}

    async def describe(
        self,
        image_urls: list[str],
        video_urls: list[str] | None = None,
        *,
        user_question: str,
        chat_context: str,
        trace: DebugTrace | None = None,
    ) -> VisionPipelineResult:
        valid_images = [
            url
            for url in image_urls
            if url.startswith(("http://", "https://", "file://", "base64://", "data:image/"))
            or Path(url).is_absolute()
        ]
        selected_images = valid_images[: self.max_images]
        overflow_count = max(0, len(valid_images) - len(selected_images))
        selected_videos = [
            url for url in (video_urls or []) if url.startswith(("http://", "https://"))
        ][: self.max_images]
        if not selected_images and not selected_videos:
            return VisionPipelineResult(ok=False, context_text="", error="no media urls")

        started = self.monotonic()
        total_timeout = (
            self.multi_timeout_seconds if len(selected_images) >= 3 else self.total_timeout_seconds
        )
        deadline = started + total_timeout
        dynamic_task = (
            asyncio.create_task(
                self._describe_dynamic(
                    selected_videos,
                    user_question=user_question,
                    deadline=deadline,
                    trace=trace,
                )
            )
            if selected_videos
            else None
        )
        timed_out = False
        try:
            synthesis = await self._describe_static(
                selected_images,
                user_question=user_question,
                chat_context=chat_context,
                started=started,
                deadline=deadline,
                trace=trace,
            )
        except TimeoutError:
            timed_out = True
            synthesis = self._unavailable_synthesis(len(selected_images))
        except Exception:
            synthesis = self._unavailable_synthesis(len(selected_images))

        dynamic = VisionObservation()
        if dynamic_task is not None:
            try:
                dynamic = await asyncio.wait_for(
                    dynamic_task,
                    timeout=self._remaining(deadline),
                )
            except (TimeoutError, Exception):
                dynamic_task.cancel()
                await asyncio.gather(dynamic_task, return_exceptions=True)

        context_parts = [synthesis.to_context_text()] if synthesis.images else []
        if selected_videos:
            description = dynamic.scene_description or "无法可靠描述动态媒体。"
            context_parts.append(
                "动态媒体：\n"
                f"视觉观察：{description}\n"
                "身份判断：动态媒体只提供客观描述，不确认具体身份。"
            )
        if overflow_count:
            context_parts.append(f"另有 {overflow_count} 张图片未进入识图。")
        context_text = "\n\n".join(part for part in context_parts if part)
        usable = bool(dynamic.scene_description) or any(
            item.confidence is not ConfidenceBand.UNAVAILABLE for item in synthesis.images
        )
        if trace is not None:
            confidence_counts = {
                confidence.value: sum(item.confidence is confidence for item in synthesis.images)
                for confidence in ConfidenceBand
            }
            trace.event(
                "vision.pipeline.result",
                {
                    "ok": usable,
                    "timed_out": timed_out,
                    "image_count": len(selected_images),
                    "video_count": len(selected_videos),
                    "elapsed_ms": round((self.monotonic() - started) * 1000),
                    "confidence_counts": confidence_counts,
                },
            )
        return VisionPipelineResult(
            ok=usable,
            context_text=context_text,
            synthesis=synthesis,
            timed_out=timed_out,
        )

    async def close(self) -> None:
        tasks = tuple(self._inflight_lens.values())
        if tasks:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        clients = [self.analyzer, self.lens_client, self.web_client]
        for client in clients:
            close = getattr(client, "close", None)
            if close is not None:
                await close()

    async def _describe_static(
        self,
        image_urls: list[str],
        *,
        user_question: str,
        chat_context: str,
        started: float,
        deadline: float,
        trace: DebugTrace | None,
    ) -> VisionSynthesis:
        if not image_urls:
            return VisionSynthesis()
        prepared_results = await asyncio.gather(
            *(
                self._prepare_image(index, url, deadline=deadline, trace=trace)
                for index, url in enumerate(image_urls, start=1)
            )
        )
        prepared = tuple(item for item in prepared_results if item is not None)
        image_hashes = tuple(
            next(
                (item.image.sha256 for item in prepared if item.image_number == image_number),
                f"unavailable-{image_number}",
            )
            for image_number in range(1, len(image_urls) + 1)
        )
        synthesis_key = self.cache.build_synthesis_key(
            image_hashes=image_hashes,
            user_question=user_question,
            chat_context=chat_context,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            lens_parser_version=self.lens_parser_version,
            schema_version=self.schema_version,
        )
        cached_synthesis = await self.cache.get_synthesis(synthesis_key, now=self.now())
        if cached_synthesis is not None:
            if trace is not None:
                trace.event("vision.cache.hit", {"stage": "synthesis"})
            return cached_synthesis
        if not prepared:
            return self._unavailable_synthesis(len(image_urls))

        is_multi = len(image_urls) >= 3
        reserve = self.model_timeout_seconds if is_multi else min(self.model_timeout_seconds, 15.0)
        lens_stage_deadline = min(started + self.lens_timeout_seconds, deadline - reserve)
        unique: dict[str, _PreparedImage] = {}
        for item in prepared:
            if not item.image.animated:
                unique.setdefault(item.image.sha256, item)
        lens_by_hash = await self._lens_results(
            unique,
            stage_deadline=lens_stage_deadline,
            trace=trace,
        )
        lens_results = tuple(
            ImageLensResult(
                image_number=item.image_number,
                search=lens_by_hash.get(
                    item.image.sha256,
                    LensSearchResult(ok=False, error="Lens unavailable"),
                ),
            )
            for item in prepared
        )
        synthesis = await asyncio.wait_for(
            self.analyzer.synthesize(
                tuple(item.image for item in prepared),
                lens_results,
                user_question=user_question,
                chat_context=chat_context,
                timeout_seconds=min(self.model_timeout_seconds, self._remaining(deadline)),
                trace=trace,
            ),
            timeout=self._remaining(deadline),
        )
        synthesis = self._restore_positions(synthesis, len(image_urls))
        animated = {item.image_number for item in prepared if item.image.animated}
        if animated:
            synthesis = VisionSynthesis(
                tuple(
                    replace(
                        item,
                        confidence=ConfidenceBand.NO_IDENTITY,
                        subject_identity="",
                        work_or_affiliation="",
                        source_series_or_author="",
                        needs_exact=False,
                        needs_web=False,
                        scene_description="动图抽样帧（非完整时序）：" + item.scene_description,
                    )
                    if item.image_number in animated
                    else item
                    for item in synthesis.images
                ),
                "",
            )
        synthesis = await self._run_fallbacks(
            synthesis,
            prepared=prepared,
            deadline=deadline,
            trace=trace,
        )
        if any(item.confidence is not ConfidenceBand.UNAVAILABLE for item in synthesis.images):
            await self.cache.put_synthesis(synthesis_key, synthesis=synthesis, now=self.now())
        return synthesis

    async def _prepare_image(
        self,
        image_number: int,
        url: str,
        *,
        deadline: float,
        trace: DebugTrace | None,
    ) -> _PreparedImage | None:
        try:
            image = await asyncio.wait_for(
                self.preprocessor.fetch(
                    url,
                    trace=trace,
                    timeout_seconds=self._remaining(deadline),
                ),
                timeout=self._remaining(deadline),
            )
        except Exception as exc:
            if trace is not None:
                trace.event(
                    "vision.image.failure",
                    {
                        "image_number": image_number,
                        "stage": "preprocess",
                        "error_type": self._failure_type(exc),
                    },
                )
            return None
        return _PreparedImage(image_number=image_number, source_url=url, image=image)

    async def _lens_results(
        self,
        unique: dict[str, _PreparedImage],
        *,
        stage_deadline: float,
        trace: DebugTrace | None,
    ) -> dict[str, LensSearchResult]:
        results: dict[str, LensSearchResult] = {}

        async def resolve(image_hash: str, item: _PreparedImage) -> None:
            cached = await self.cache.get_lens_all(
                image_hash,
                version=self.lens_parser_version,
                now=self.now(),
            )
            if cached is not None:
                if trace is not None:
                    trace.event("vision.cache.hit", {"stage": "lens_all"})
                results[image_hash] = cached
                return
            if (
                self.lens_client is None
                or not item.source_url.startswith(("https://", "http://"))
                or self.monotonic() >= stage_deadline
            ):
                results[image_hash] = LensSearchResult(ok=False, error="Lens unavailable")
                return
            try:
                results[image_hash] = await asyncio.wait_for(
                    asyncio.shield(
                        await self._inflight_lens_task(
                            image_hash,
                            item.source_url,
                            timeout_seconds=min(
                                self.lens_timeout_seconds,
                                self._remaining(stage_deadline),
                            ),
                            trace=trace,
                        )
                    ),
                    timeout=self._remaining(stage_deadline),
                )
            except TimeoutError:
                results[image_hash] = LensSearchResult(ok=False, error="Lens stage deadline")

        await asyncio.gather(*(resolve(image_hash, item) for image_hash, item in unique.items()))
        return results

    async def _inflight_lens_task(
        self,
        image_hash: str,
        source_url: str,
        *,
        timeout_seconds: float,
        trace: DebugTrace | None,
    ) -> asyncio.Task[LensSearchResult]:
        async with self._inflight_lock:
            task = self._inflight_lens.get(image_hash)
            if task is None:
                task = asyncio.create_task(
                    self._run_lens(
                        image_hash,
                        source_url,
                        timeout_seconds=timeout_seconds,
                        trace=trace,
                    )
                )
                self._inflight_lens[image_hash] = task
            elif trace is not None:
                trace.event("vision.inflight.coalesced", {"stage": "lens_all"})
            return task

    async def _run_lens(
        self,
        image_hash: str,
        source_url: str,
        *,
        timeout_seconds: float,
        trace: DebugTrace | None,
    ) -> LensSearchResult:
        try:
            async with self._lens_semaphore:
                result = await self.lens_client.search_all(
                    source_url,
                    timeout_seconds=timeout_seconds,
                    trace=trace,
                )
            if result.ok:
                await self.cache.put_lens_all(
                    image_hash,
                    version=self.lens_parser_version,
                    result=result,
                    now=self.now(),
                )
            return result
        finally:
            async with self._inflight_lock:
                if self._inflight_lens.get(image_hash) is asyncio.current_task():
                    self._inflight_lens.pop(image_hash, None)

    async def _run_fallbacks(
        self,
        synthesis: VisionSynthesis,
        *,
        prepared: tuple[_PreparedImage, ...],
        deadline: float,
        trace: DebugTrace | None,
    ) -> VisionSynthesis:
        by_number = {item.image_number: item for item in prepared}
        exact_requests = [
            item for item in synthesis.images if item.needs_exact and item.image_number in by_number
        ][: self.max_exact_fallbacks]
        web_requests = [
            item for item in synthesis.images if item.needs_web and item.verification_query
        ][: self.max_web_fallbacks]
        jobs = []
        if self.exact_fallback_enabled and self.lens_client is not None:
            jobs.extend(
                self._exact_fallback(item, by_number[item.image_number], deadline, trace)
                for item in exact_requests
            )
        if self.web_fallback_enabled and self.web_client is not None:
            jobs.extend(self._web_fallback(item, deadline, trace) for item in web_requests)
        if not jobs or self.monotonic() >= deadline:
            return synthesis
        try:
            results = await asyncio.gather(*jobs)
        except Exception:
            return synthesis
        grouped: dict[int, ImageFallbackEvidence] = {}
        for image_number, kind, sources, error in results:
            current = grouped.get(image_number, ImageFallbackEvidence(image_number))
            if kind == "exact":
                grouped[image_number] = ImageFallbackEvidence(
                    image_number,
                    exact_sources=sources,
                    web_sources=current.web_sources,
                    exact_error=error,
                    web_error=current.web_error,
                )
            else:
                grouped[image_number] = ImageFallbackEvidence(
                    image_number,
                    exact_sources=current.exact_sources,
                    web_sources=sources,
                    exact_error=current.exact_error,
                    web_error=error,
                )
        if not any(item.exact_sources or item.web_sources for item in grouped.values()):
            return synthesis
        try:
            return await asyncio.wait_for(
                self.analyzer.reevaluate(
                    synthesis,
                    tuple(grouped.values()),
                    images=tuple((item.image_number, item.image) for item in prepared),
                    timeout_seconds=min(self.model_timeout_seconds, self._remaining(deadline)),
                    trace=trace,
                ),
                timeout=self._remaining(deadline),
            )
        except Exception:
            return synthesis

    async def _exact_fallback(self, decision, prepared, deadline, trace):
        if not prepared.source_url.startswith(("https://", "http://")):
            return decision.image_number, "exact", (), "public image URL unavailable"
        cached = await self.cache.get_exact(
            prepared.image.sha256,
            version=self.lens_parser_version,
            now=self.now(),
        )
        if cached is not None:
            if trace is not None:
                trace.event("vision.cache.hit", {"stage": "lens_exact"})
        else:
            try:
                cached = await asyncio.wait_for(
                    self.lens_client.search_exact(
                        prepared.source_url,
                        timeout_seconds=self._remaining(deadline),
                        trace=trace,
                    ),
                    timeout=self._remaining(deadline),
                )
            except Exception:
                cached = ExactSearchResult(ok=False, error="exact unavailable")
            if cached.ok:
                await self.cache.put_exact(
                    prepared.image.sha256,
                    version=self.lens_parser_version,
                    result=cached,
                    now=self.now(),
                )
        return decision.image_number, "exact", cached.sources, cached.error

    async def _web_fallback(self, decision, deadline, trace):
        try:
            sources = await asyncio.wait_for(
                self.web_client.search(
                    decision.verification_query,
                    timeout_seconds=self._remaining(deadline),
                    trace=trace,
                ),
                timeout=self._remaining(deadline),
            )
            return decision.image_number, "web", sources, ""
        except Exception:
            return decision.image_number, "web", (), "web unavailable"

    async def _describe_dynamic(self, video_urls, *, user_question, deadline, trace):
        return await self.analyzer.describe_dynamic_media(
            video_urls,
            user_question=user_question,
            timeout_seconds=min(self.model_timeout_seconds, self._remaining(deadline)),
            trace=trace,
        )

    @staticmethod
    def _restore_positions(synthesis: VisionSynthesis, image_count: int) -> VisionSynthesis:
        by_number = {item.image_number: item for item in synthesis.images}
        return VisionSynthesis(
            images=tuple(
                by_number.get(
                    image_number,
                    ImageDecision(image_number, ConfidenceBand.UNAVAILABLE),
                )
                for image_number in range(1, image_count + 1)
            ),
            combined_answer=synthesis.combined_answer,
        )

    @staticmethod
    def _unavailable_synthesis(image_count: int) -> VisionSynthesis:
        return VisionSynthesis(
            images=tuple(
                ImageDecision(image_number, ConfidenceBand.UNAVAILABLE)
                for image_number in range(1, image_count + 1)
            )
        )

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise TimeoutError("vision pipeline deadline exhausted")
        return remaining

    @staticmethod
    def _failure_type(exc: Exception) -> str:
        text = f"{type(exc).__name__} {exc}".casefold()
        if "timeout" in text or "deadline" in text:
            return "timeout"
        if "ssl" in text or "tls" in text or "certificate" in text:
            return "tls"
        if "connect" in text or "name or service" in text or "dns" in text:
            return "dns"
        if "401" in text or "403" in text or "auth" in text:
            return "authentication"
        return "provider"
