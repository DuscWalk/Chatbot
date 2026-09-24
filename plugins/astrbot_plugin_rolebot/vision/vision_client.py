from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx

from ..diagnostics import DebugTrace
from .image_preprocessor import NormalizedImage
from .vision_types import (
    ConfidenceBand,
    ImageDecision,
    ImageFallbackEvidence,
    ImageLensResult,
    SearchSource,
    VisionObservation,
    VisionSynthesis,
)

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


class VisualAnalyzer:
    def __init__(
        self,
        *,
        api_base: str,
        api_key: str,
        model_name: str,
        timeout_seconds: float,
        enable_thinking: bool,
        video_fps: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds
        self.enable_thinking = enable_thinking
        self.video_fps = video_fps
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )

    async def synthesize(
        self,
        images: tuple[NormalizedImage, ...],
        lens_results: tuple[ImageLensResult, ...],
        *,
        user_question: str,
        chat_context: str,
        timeout_seconds: float,
        trace: DebugTrace | None = None,
    ) -> VisionSynthesis:
        image_numbers = tuple(item.image_number for item in lens_results)
        if len(image_numbers) != len(images) or len(set(image_numbers)) != len(image_numbers):
            fallback_numbers = image_numbers or tuple(range(1, len(images) + 1))
            return self._unavailable_synthesis(fallback_numbers)
        prompt = self._synthesis_prompt(
            image_numbers=image_numbers,
            lens_results=lens_results,
            user_question=user_question,
            chat_context=chat_context,
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for image_number, image in zip(image_numbers, images, strict=True):
            content.extend(
                (
                    {
                        "type": "text",
                        "text": f"图片{image_number}"
                        + (
                            "：动图的首/中/尾抽样帧拼图，仅描述可见内容，不确认身份、不推测完整时序。"
                            if image.animated
                            else "原图："
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": image.data_url()}},
                )
            )
        payload = self._payload(
            content=content,
            schema=self._synthesis_schema(),
            enable_thinking=False,
        )
        data, error = await self._request(
            payload,
            event_prefix="vision.synthesis",
            trace=trace,
            timeout_seconds=timeout_seconds,
        )
        if error:
            return self._unavailable_synthesis(image_numbers)
        return self._parse_synthesis(data, image_numbers=image_numbers)

    async def reevaluate(
        self,
        previous: VisionSynthesis,
        fallback_results: tuple[ImageFallbackEvidence, ...],
        *,
        timeout_seconds: float,
        trace: DebugTrace | None = None,
    ) -> VisionSynthesis:
        image_numbers = tuple(item.image_number for item in previous.images)
        payload = self._payload(
            content=[
                {
                    "type": "text",
                    "text": self._reevaluation_prompt(previous, fallback_results),
                }
            ],
            schema=self._synthesis_schema(),
            enable_thinking=False,
        )
        data, error = await self._request(
            payload,
            event_prefix="vision.reevaluation",
            trace=trace,
            timeout_seconds=timeout_seconds,
        )
        if error:
            return previous
        return self._parse_synthesis(data, image_numbers=image_numbers)

    async def close(self) -> None:
        await self._client.aclose()

    async def describe_dynamic_media(
        self,
        video_urls: list[str],
        *,
        user_question: str,
        trace: DebugTrace | None = None,
        timeout_seconds: float | None = None,
    ) -> VisionObservation:
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "只客观描述动态媒体中可见的场景、文字和动作，不确认具体人物身份。\n"
                    f"用户问题：{self._bounded(user_question, 500)}"
                ),
            }
        ]
        content.extend(
            {
                "type": "video_url",
                "video_url": {"url": url},
                "fps": self.video_fps,
            }
            for url in video_urls
            if url.startswith(("http://", "https://"))
        )
        payload = self._payload(
            content=content,
            schema=self._dynamic_schema(),
            enable_thinking=False,
        )
        data, error = await self._request(
            payload,
            event_prefix="vision.dynamic",
            trace=trace,
            timeout_seconds=timeout_seconds,
        )
        if error:
            return VisionObservation()
        return VisionObservation(
            scene_description=self._safe_output(self._text(data.get("scene_description")), 500),
            visible_text=self._strings(data.get("visible_text"))[:12],
            people_or_characters=self._strings(data.get("people_or_characters"))[:12],
            distinctive_features=self._strings(data.get("distinctive_features"))[:12],
        )

    def _payload(
        self,
        *,
        content: list[dict[str, Any]],
        schema: dict[str, Any],
        enable_thinking: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "model": self.model_name,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "enable_thinking": (
                self.enable_thinking if enable_thinking is None else enable_thinking
            ),
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "vision_evidence", "strict": True, "schema": schema},
            },
        }

    async def _request(
        self,
        payload: dict[str, Any],
        *,
        event_prefix: str,
        trace: DebugTrace | None,
        timeout_seconds: float | None,
    ) -> tuple[dict[str, Any], str]:
        self._trace(trace, f"{event_prefix}.request", {"payload": payload})
        started = time.monotonic()
        timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        try:
            response = await self._client.post(
                f"{self.api_base}/chat/completions",
                json=payload,
                timeout=timeout,
            )
            response.raise_for_status()
            response_data = response.json()
            content = response_data["choices"][0]["message"]["content"]
            data = json.loads(str(content))
            if not isinstance(data, dict):
                raise ValueError("response is not an object")
        except Exception as exc:
            error = f"invalid visual response: {self._exception_text(exc)}"
            self._trace(
                trace,
                f"{event_prefix}.result",
                {"ok": False, "error": error, "elapsed_ms": self._elapsed_ms(started)},
            )
            return {}, error
        self._trace(
            trace,
            f"{event_prefix}.result",
            {"ok": True, "data": data, "elapsed_ms": self._elapsed_ms(started)},
        )
        return data, ""

    def _synthesis_prompt(
        self,
        *,
        image_numbers: tuple[int, ...],
        lens_results: tuple[ImageLensResult, ...],
        user_question: str,
        chat_context: str,
    ) -> str:
        by_number = {item.image_number: item for item in lens_results}
        evidence_blocks = []
        for image_number in image_numbers:
            result = by_number.get(image_number)
            if result is None or not result.search.ok:
                evidence_blocks.append(f"图片{image_number} Lens 不可用。请直接观察原图。")
                continue
            evidence = result.search.evidence
            lines = [f"图片{image_number} Lens 结果："]
            if evidence.ai_overview:
                lines.append(f"AI 概览：{self._bounded(evidence.ai_overview, 1000)}")
            for source in (*evidence.visual_matches, *evidence.related_content)[:12]:
                lines.append(self._source_line(source))
            evidence_blocks.append(self._bounded("\n".join(lines), 3500))
        return (
            "你负责综合原图、Google Lens 结果和用户问题，只输出符合 JSON schema 的结果。"
            "不要建立固定证据门槛，但要谨慎处理聚合标题、多角色列表、商品页和外观相似项。"
            "原图与搜索结果冲突时返回 uncertain，或按图片编号请求 exact/Web 回退。"
            "只有需要寻找相同图片或原始出处时设置 needs_exact=true；不要默认请求 exact。"
            "需要网页背景时设置 needs_web=true，并给出简短 verification_query。"
            "表情包可以识别系列、作者或来源，不要为它虚构角色名。"
            "未知真人不识别具体身份，只描述外观、动作和场景。\n"
            f"用户问题：{self._bounded(user_question, 500)}\n"
            f"最近聊天：{self._bounded(chat_context, 1500)}\n"
            f"Lens 证据：\n{self._bounded(chr(10).join(evidence_blocks), 14000)}"
        )

    def _reevaluation_prompt(
        self,
        previous: VisionSynthesis,
        fallback_results: tuple[ImageFallbackEvidence, ...],
    ) -> str:
        previous_data = {
            "images": [self._decision_data(item) for item in previous.images],
            "combined_answer": self._bounded(previous.combined_answer, 800),
        }
        fallback_blocks = []
        for result in fallback_results:
            lines = [f"图片{result.image_number}新增证据："]
            for source in (*result.exact_sources, *result.web_sources)[:12]:
                lines.append(self._source_line(source))
            if len(lines) == 1:
                lines.append("回退没有返回可用结果。")
            fallback_blocks.append(self._bounded("\n".join(lines), 3500))
        return (
            "根据第一次结构化判断和新增的 exact/Web 文本证据复判一次。"
            "不得请求更多工具，不得假装重新查看原图。"
            "冲突仍未解决时保持 uncertain。只输出符合 JSON schema 的结果。\n"
            f"第一次判断：{json.dumps(previous_data, ensure_ascii=False)}\n"
            f"新增证据：\n{self._bounded(chr(10).join(fallback_blocks), 7000)}"
        )

    @classmethod
    def _parse_synthesis(
        cls,
        data: dict[str, Any],
        *,
        image_numbers: tuple[int, ...],
    ) -> VisionSynthesis:
        raw_images = data.get("images")
        if not isinstance(raw_images, list):
            return cls._unavailable_synthesis(image_numbers)
        parsed: dict[int, ImageDecision] = {}
        for item in raw_images:
            if not isinstance(item, dict):
                continue
            try:
                image_number = int(item.get("image_number", 0))
                if image_number not in image_numbers or image_number in parsed:
                    continue
                parsed[image_number] = ImageDecision(
                    image_number=image_number,
                    confidence=ConfidenceBand(cls._text(item.get("confidence"))),
                    scene_description=cls._safe_output(
                        cls._text(item.get("scene_description")), 500
                    ),
                    visible_text=tuple(
                        text
                        for raw_text in cls._strings(item.get("visible_text"))[:12]
                        if (text := cls._safe_output(raw_text, 160))
                    ),
                    subject_identity=cls._safe_output(cls._text(item.get("subject_identity")), 160),
                    work_or_affiliation=cls._safe_output(
                        cls._text(item.get("work_or_affiliation")), 200
                    ),
                    source_series_or_author=cls._safe_output(
                        cls._text(item.get("source_series_or_author")), 200
                    ),
                    reason=cls._safe_output(cls._text(item.get("reason")), 500),
                    needs_exact=item.get("needs_exact") is True,
                    needs_web=item.get("needs_web") is True,
                    verification_query=cls._safe_output(
                        cls._text(item.get("verification_query")), 250
                    ),
                )
            except (TypeError, ValueError):
                continue
        decisions = tuple(
            parsed.get(
                image_number,
                ImageDecision(image_number, ConfidenceBand.UNAVAILABLE),
            )
            for image_number in image_numbers
        )
        return VisionSynthesis(
            images=decisions,
            combined_answer=cls._safe_output(cls._text(data.get("combined_answer")), 800),
        )

    @staticmethod
    def _unavailable_synthesis(image_numbers: tuple[int, ...]) -> VisionSynthesis:
        return VisionSynthesis(
            images=tuple(
                ImageDecision(image_number, ConfidenceBand.UNAVAILABLE)
                for image_number in image_numbers
            )
        )

    @classmethod
    def _source_line(cls, source: SearchSource) -> str:
        title = cls._bounded(source.title, 200)
        domain = cls._bounded(source.domain, 120)
        snippet = cls._bounded(source.snippet, 300)
        return f"[{source.result_kind}] {title} | {domain} | {snippet}"

    @classmethod
    def _decision_data(cls, decision: ImageDecision) -> dict[str, object]:
        return {
            "image_number": decision.image_number,
            "scene_description": cls._bounded(decision.scene_description, 500),
            "visible_text": list(decision.visible_text[:12]),
            "subject_identity": cls._bounded(decision.subject_identity, 160),
            "work_or_affiliation": cls._bounded(decision.work_or_affiliation, 200),
            "source_series_or_author": cls._bounded(decision.source_series_or_author, 200),
            "confidence": decision.confidence.value,
            "reason": cls._bounded(decision.reason, 500),
            "needs_exact": decision.needs_exact,
            "needs_web": decision.needs_web,
            "verification_query": cls._bounded(decision.verification_query, 250),
        }

    @staticmethod
    def _dynamic_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "scene_description": {"type": "string"},
                "visible_text": {"type": "array", "items": {"type": "string"}},
                "people_or_characters": {"type": "array", "items": {"type": "string"}},
                "distinctive_features": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "scene_description",
                "visible_text",
                "people_or_characters",
                "distinctive_features",
            ],
            "additionalProperties": False,
        }

    @staticmethod
    def _synthesis_schema() -> dict[str, Any]:
        image_properties = {
            "image_number": {"type": "integer", "minimum": 1, "maximum": 4},
            "scene_description": {"type": "string"},
            "visible_text": {"type": "array", "items": {"type": "string"}},
            "subject_identity": {"type": "string"},
            "work_or_affiliation": {"type": "string"},
            "source_series_or_author": {"type": "string"},
            "confidence": {
                "type": "string",
                "enum": ["confirmed", "uncertain", "no_identity"],
            },
            "reason": {"type": "string"},
            "needs_exact": {"type": "boolean"},
            "needs_web": {"type": "boolean"},
            "verification_query": {"type": "string"},
        }
        return {
            "type": "object",
            "properties": {
                "images": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": image_properties,
                        "required": list(image_properties),
                        "additionalProperties": False,
                    },
                },
                "combined_answer": {"type": "string"},
            },
            "required": ["images", "combined_answer"],
            "additionalProperties": False,
        }

    @staticmethod
    def _text(value: Any) -> str:
        return str(value).strip() if isinstance(value, str) else ""

    @classmethod
    def _strings(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        return tuple(text for item in value if (text := cls._text(item)))

    @staticmethod
    def _bounded(value: str, limit: int) -> str:
        return value.strip()[:limit]

    @staticmethod
    def _safe_output(value: str, limit: int) -> str:
        without_urls = _URL_RE.sub("", value)
        return " ".join(without_urls.split())[:limit].strip()

    @staticmethod
    def _exception_text(exc: Exception) -> str:
        return str(exc).strip() or type(exc).__name__

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return round((time.monotonic() - started) * 1000)

    @classmethod
    def _trace(cls, trace: DebugTrace | None, name: str, data: dict[str, Any]) -> None:
        if trace is not None:
            trace.event(name, cls._redact(data))

    @classmethod
    def _redact(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: cls._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._redact(item) for item in value]
        if isinstance(value, str) and value.startswith("data:"):
            header, _, encoded = value.partition(",")
            return f"{header},<redacted {len(encoded)} chars>"
        return value
