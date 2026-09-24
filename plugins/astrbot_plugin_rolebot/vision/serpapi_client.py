from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..diagnostics import DebugTrace
from .vision_types import (
    ExactSearchResult,
    LensAllEvidence,
    LensSearchResult,
    SearchSource,
)

_TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "ref",
    "source",
    "spm",
}
_ERROR_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    filtered = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in _TRACKING_KEYS:
            continue
        filtered.append((key, value))
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            urlencode(filtered),
            "",
        )
    )


def parse_lens_all_response(
    data: dict[str, Any],
    *,
    visual_limit: int,
    related_limit: int,
    overview_limit: int,
) -> LensAllEvidence:
    return LensAllEvidence(
        visual_matches=_parse_sources(data.get("visual_matches"), "visual")[:visual_limit],
        related_content=_parse_sources(data.get("related_content"), "related")[:related_limit],
        ai_overview=_parse_ai_overview(data.get("ai_overview"))[:overview_limit],
    )


def _parse_sources(value: Any, result_kind: str) -> tuple[SearchSource, ...]:
    if not isinstance(value, list):
        return ()
    sources: list[SearchSource] = []
    seen_urls: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        title = _text(item.get("title"))
        raw_url = _text(item.get("link") or item.get("url"))
        if not title or not raw_url:
            continue
        url = canonicalize_url(raw_url)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        sources.append(
            SearchSource(
                title=title,
                url=url,
                domain=urlsplit(url).netloc.lower(),
                snippet=_text(item.get("snippet") or item.get("source")),
                result_kind=result_kind,
            )
        )
    return tuple(sources)


def _parse_ai_overview(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    direct = _text(value.get("text") or value.get("snippet"))
    if direct:
        return direct
    blocks = value.get("text_blocks")
    if not isinstance(blocks, list):
        return ""
    parts = [
        text
        for item in blocks
        if isinstance(item, dict)
        if (text := _text(item.get("snippet") or item.get("text")))
    ]
    return "\n".join(parts)


class SerpApiLensClient:
    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float,
        exact_limit: int = 5,
        visual_limit: int = 20,
        related_limit: int = 10,
        overview_limit: int = 2000,
        poll_interval_seconds: float = 0.75,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.exact_limit = exact_limit
        self.visual_limit = visual_limit
        self.related_limit = related_limit
        self.overview_limit = overview_limit
        self.poll_interval_seconds = poll_interval_seconds
        self.sleep = sleep or asyncio.sleep
        self.clock = clock or time.monotonic
        self._client = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)

    async def search_all(
        self,
        image_url: str,
        *,
        timeout_seconds: float | None = None,
        trace: DebugTrace | None = None,
    ) -> LensSearchResult:
        timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        data, cached, error = await self._run_async_lens(
            image_url,
            result_type="all",
            timeout_seconds=timeout,
            trace=trace,
            trace_prefix="vision.lens_all",
        )
        if data is None:
            return LensSearchResult(
                ok=False,
                error=error,
                unreachable=_looks_unreachable(error),
            )
        return LensSearchResult(
            ok=True,
            evidence=parse_lens_all_response(
                data,
                visual_limit=self.visual_limit,
                related_limit=self.related_limit,
                overview_limit=self.overview_limit,
            ),
            cached=cached,
        )

    async def search_exact(
        self,
        image_url: str,
        *,
        timeout_seconds: float | None = None,
        trace: DebugTrace | None = None,
    ) -> ExactSearchResult:
        timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        data, cached, error = await self._run_async_lens(
            image_url,
            result_type="exact_matches",
            timeout_seconds=timeout,
            trace=trace,
            trace_prefix="vision.lens_exact",
        )
        if data is None:
            return ExactSearchResult(ok=False, error=error)
        return ExactSearchResult(
            ok=True,
            sources=_parse_sources(data.get("exact_matches"), "exact")[: self.exact_limit],
            cached=cached,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _run_async_lens(
        self,
        image_url: str,
        *,
        result_type: str,
        timeout_seconds: float,
        trace: DebugTrace | None,
        trace_prefix: str,
    ) -> tuple[dict[str, Any] | None, bool, str]:
        started = self.clock()
        deadline = started + timeout_seconds
        self._trace_stage(trace, f"{trace_prefix}.submit", {"result_type": result_type})
        try:
            data = await self._request_json(
                "/search.json",
                params={
                    "engine": "google_lens",
                    "url": image_url,
                    "type": result_type,
                    "async": "true",
                    "auto_crop": "false",
                    "api_key": self.api_key,
                },
                timeout_seconds=self._remaining(deadline),
            )
            while True:
                status = self._status(data)
                if status in {"success", "cached"} or (not status and result_type in data):
                    cached = status == "cached"
                    self._trace_stage(
                        trace,
                        f"{trace_prefix}.result",
                        {
                            "ok": True,
                            "cached": cached,
                            "elapsed_ms": round((self.clock() - started) * 1000),
                        },
                    )
                    return data, cached, ""
                if status == "error" or data.get("error"):
                    error = self._sanitize_error(
                        _text(data.get("error")) or "SerpApi search failed"
                    )
                    self._trace_async_failure(trace, trace_prefix, started, error)
                    return None, False, error
                if status != "processing":
                    error = f"unexpected SerpApi search status: {status or 'missing'}"
                    self._trace_async_failure(trace, trace_prefix, started, error)
                    return None, False, error
                search_id = self._search_id(data)
                if not search_id:
                    error = "SerpApi processing response is missing search id"
                    self._trace_async_failure(trace, trace_prefix, started, error)
                    return None, False, error
                remaining = self._remaining(deadline)
                await self.sleep(min(self.poll_interval_seconds, remaining))
                if self.clock() >= deadline:
                    error = "SerpApi polling deadline exhausted"
                    self._trace_async_failure(trace, trace_prefix, started, error)
                    return None, False, error
                data = await self._request_json(
                    f"/searches/{search_id}.json",
                    params={"api_key": self.api_key},
                    timeout_seconds=self._remaining(deadline),
                )
        except Exception as exc:
            error = self._sanitize_error(_exception_text(exc))
            self._trace_async_failure(trace, trace_prefix, started, error)
            return None, False, error

    async def _request_json(
        self,
        path: str,
        *,
        params: dict[str, object],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        response = await self._client.get(
            f"https://serpapi.com{path}",
            params=params,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("invalid SerpApi response")
        return data

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("SerpApi polling deadline exhausted")
        return remaining

    def _sanitize_error(self, error: str) -> str:
        sanitized = error.replace(self.api_key, "[redacted]") if self.api_key else error
        sanitized = _ERROR_URL_RE.sub("[url]", sanitized)
        return " ".join(sanitized.split())[:300]

    @staticmethod
    def _status(data: dict[str, Any]) -> str:
        metadata = data.get("search_metadata")
        if not isinstance(metadata, dict):
            return ""
        return _text(metadata.get("status")).casefold()

    @staticmethod
    def _search_id(data: dict[str, Any]) -> str:
        metadata = data.get("search_metadata")
        if not isinstance(metadata, dict):
            return ""
        return _text(metadata.get("id"))

    def _trace_async_failure(
        self,
        trace: DebugTrace | None,
        trace_prefix: str,
        started: float,
        error: str,
    ) -> None:
        self._trace_stage(
            trace,
            f"{trace_prefix}.result",
            {
                "ok": False,
                "cached": False,
                "unreachable": _looks_unreachable(error),
                "error_type": _error_type(error),
                "elapsed_ms": round((self.clock() - started) * 1000),
            },
        )

    @staticmethod
    def _trace_stage(
        trace: DebugTrace | None,
        name: str,
        data: dict[str, object],
    ) -> None:
        if trace is not None:
            trace.event(name, data)


class SerpApiWebClient:
    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._client = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)

    async def search(
        self,
        query: str,
        *,
        trace: DebugTrace | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[SearchSource, ...]:
        bounded_query = " ".join(query.split())[:250]
        if not bounded_query:
            return ()
        timeout = timeout_seconds if timeout_seconds is not None else self.timeout_seconds
        started = time.monotonic()
        try:
            response = await self._client.get(
                "https://serpapi.com/search.json",
                params={
                    "engine": "google",
                    "q": bounded_query,
                    "hl": "zh-cn",
                    "api_key": self.api_key,
                },
                timeout=timeout,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("invalid SerpApi Web response")
            sources = _parse_sources(data.get("organic_results"), "web")[:10]
        except Exception as exc:
            if trace is not None:
                trace.event(
                    "vision.web_search.result",
                    {
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "elapsed_ms": _elapsed_ms(started),
                    },
                )
            return ()
        if trace is not None:
            trace.event(
                "vision.web_search.result",
                {
                    "ok": True,
                    "result_count": len(sources),
                    "elapsed_ms": _elapsed_ms(started),
                },
            )
        return sources

    async def close(self) -> None:
        await self._client.aclose()


def _looks_unreachable(error: str) -> bool:
    lowered = error.casefold()
    return any(token in lowered for token in ("could not fetch", "unreachable", "image url"))


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _exception_text(exc: Exception) -> str:
    return str(exc).strip() or type(exc).__name__


def _error_type(error: str) -> str:
    lowered = error.casefold()
    if "deadline" in lowered or "timeout" in lowered:
        return "timeout"
    if _looks_unreachable(error):
        return "unreachable"
    if "status" in lowered:
        return "status"
    return "provider"


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
