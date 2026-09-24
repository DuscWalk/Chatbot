from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


class ConfidenceBand(StrEnum):
    CONFIRMED = "confirmed"
    UNCERTAIN = "uncertain"
    NO_IDENTITY = "no_identity"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class VisionObservation:
    scene_description: str = ""
    visible_text: tuple[str, ...] = ()
    people_or_characters: tuple[str, ...] = ()
    distinctive_features: tuple[str, ...] = ()


@dataclass(frozen=True)
class SearchSource:
    title: str
    url: str
    domain: str
    snippet: str = ""
    result_kind: str = ""


@dataclass(frozen=True)
class LensAllEvidence:
    visual_matches: tuple[SearchSource, ...] = ()
    related_content: tuple[SearchSource, ...] = ()
    ai_overview: str = ""


@dataclass(frozen=True)
class LensSearchResult:
    ok: bool
    evidence: LensAllEvidence = LensAllEvidence()
    error: str = ""
    unreachable: bool = False
    cached: bool = False


@dataclass(frozen=True)
class ImageLensResult:
    image_number: int
    search: LensSearchResult


@dataclass(frozen=True)
class ExactSearchResult:
    ok: bool
    sources: tuple[SearchSource, ...] = ()
    error: str = ""
    cached: bool = False


@dataclass(frozen=True)
class ImageFallbackEvidence:
    image_number: int
    exact_sources: tuple[SearchSource, ...] = ()
    web_sources: tuple[SearchSource, ...] = ()
    exact_error: str = ""
    web_error: str = ""


@dataclass(frozen=True)
class ImageDecision:
    image_number: int
    confidence: ConfidenceBand
    scene_description: str = ""
    visible_text: tuple[str, ...] = ()
    subject_identity: str = ""
    work_or_affiliation: str = ""
    source_series_or_author: str = ""
    reason: str = ""
    needs_exact: bool = False
    needs_web: bool = False
    verification_query: str = ""

    def to_context_text(self) -> str:
        lines = [f"图片{self.image_number}："]
        scene = _context_text(self.scene_description, limit=500)
        visible_text = tuple(
            text for item in self.visible_text if (text := _context_text(item, limit=160))
        )
        if scene:
            lines.append(f"视觉观察：{scene}")
        if visible_text:
            lines.append(f"可见文字：{'；'.join(visible_text[:12])}")
        if self.confidence is ConfidenceBand.CONFIRMED:
            identity = _context_text(self.subject_identity, limit=160)
            if identity:
                lines.append(f"身份判断：{identity}")
            else:
                lines.append("身份判断：不确定")
            work = _context_text(self.work_or_affiliation, limit=200)
            source = _context_text(self.source_series_or_author, limit=200)
            if work:
                lines.append(f"所属作品或阵营：{work}")
            if source:
                lines.append(f"来源系列或作者：{source}")
        elif self.confidence is ConfidenceBand.UNCERTAIN:
            lines.append("身份判断：不确定，请勿当作已确认事实。")
        elif self.confidence is ConfidenceBand.NO_IDENTITY:
            lines.append("身份判断：图中没有需要确认的具体人物或角色。")
        else:
            lines.append("身份判断：视觉信息暂不可用。")
        return "\n".join(lines)


@dataclass(frozen=True)
class VisionSynthesis:
    images: tuple[ImageDecision, ...] = ()
    combined_answer: str = ""

    def to_context_text(self) -> str:
        blocks = [item.to_context_text() for item in self.images]
        combined = _context_text(self.combined_answer, limit=800)
        if combined:
            blocks.append(f"多图结论：{combined}")
        return "\n\n".join(blocks)


def _context_text(value: str, *, limit: int) -> str:
    without_urls = _URL_RE.sub("", str(value))
    return " ".join(without_urls.split())[:limit].strip()
