"""OneBot media metadata and safe, consecutive repeat payloads."""

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote, urlsplit

from astrbot.api.message_components import Face, Plain, Reply, Video


def local_reference(source):
    if source.startswith("file://"):
        return Path(unquote(urlsplit(source).path))
    return Path(source) if source.startswith("/") else None


def napcat_file(source):
    path = local_reference(source)
    if path is not None:
        if path.stat().st_size > 12 * 1024 * 1024:
            raise ValueError("media too large")
        return "base64://" + base64.b64encode(path.read_bytes()).decode()
    return source


class OneBotMedia:
    # Deliberately not an Image subclass: AstrBot's Image serializer drops subtype.
    def __init__(self, kind, data):
        self.type = kind
        self.data = data

    def toDict(self):
        return {"type": self.type, "data": self.data}

    def __repr__(self):
        return f"OneBotMedia(type={self.type})"


def sticker_component(item):
    if item.is_sendable_mface:
        return OneBotMedia(
            "mface",
            {
                "emoji_id": item.emoji_id,
                "emoji_package_id": item.emoji_package_id,
                "key": item.key,
                "summary": item.summary,
            },
        )
    return OneBotMedia(
        "image",
        {
            "file": napcat_file(str(item.path)),
            "sub_type": 1,
            "summary": item.summary or "[动画表情]",
        },
    )


def raw_segments(event):
    raw = event.message_obj.raw_message
    value = raw.get("message", []) if hasattr(raw, "get") else []
    return value if isinstance(value, list) else []


def repeat_payload(event):
    raw = raw_segments(event)
    if not raw:
        comps = event.get_messages()
        if len(comps) == 1 and isinstance(comps[0], Face):
            return f"face:{comps[0].id}", comps[0]
        if comps and all(isinstance(c, Plain) for c in comps):
            text = event.get_message_str().strip()
            return ("text:" + text, Plain(text=text)) if text and len(text) <= 2000 else ("", None)
        return "", None
    meaningful = []
    for seg in raw:
        if not isinstance(seg, dict) or not isinstance(seg.get("data"), dict):
            return "", None
        if seg.get("type") == "text" and not str(seg["data"].get("text", "")).strip():
            continue
        meaningful.append(seg)
    if len(meaningful) != 1:
        return "", None
    segment = meaningful[0]
    kind, data = segment.get("type"), segment.get("data", {})
    if not isinstance(data, dict):
        return "", None
    if kind == "text":
        text = str(data.get("text", "")).strip()
        return ("text:" + text, Plain(text=text)) if text and len(text) <= 2000 else ("", None)
    if kind == "face":
        try:
            face_id = int(data.get("id"))
        except (ValueError, TypeError):
            return "", None
        return f"face:{face_id}", Face(id=face_id)
    if kind == "mface":
        result = {
            "emoji_id": data.get("emoji_id", data.get("emojiId")),
            "emoji_package_id": data.get("emoji_package_id", data.get("emojiPackageId")),
            "key": data.get("key"),
            "summary": data.get("summary") or "[商城表情]",
        }
        if not all(str(v or "").strip() for v in result.values()):
            return "", None
    elif kind == "image":
        try:
            subtype = int(data["sub_type"])
        except (ValueError, TypeError, KeyError):
            return "", None
        source = str(data.get("url") or data.get("file") or "")
        if subtype not in {0, 1} or not source.startswith(("https://", "http://")):
            return "", None
        result = {"file": source, "sub_type": subtype, "summary": str(data.get("summary") or "")}
    else:
        return "", None
    signature = kind + ":" + hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    return signature, OneBotMedia(kind, result)


def video_references(event):
    values = []
    for comp in event.get_messages():
        items = (comp.chain or []) if isinstance(comp, Reply) else [comp]
        for item in items:
            if isinstance(item, Video):
                value = item.url or item.file
                if value and value.startswith(("http://", "https://")):
                    values.append(value)
    for seg in raw_segments(event):
        if seg.get("type") == "video":
            data = seg.get("data", {})
            value = data.get("url") or data.get("file") or ""
            if value.startswith(("http://", "https://")):
                values.append(value)
    return list(dict.fromkeys(values))[:4]


class NapCatFaces:
    def __init__(self, bot):
        self.bot = bot

    async def add_custom_face(self, *, file, is_origin=True):
        return await self.bot.call_action(
            "add_custom_face", file=napcat_file(file), is_origin=is_origin
        )

    async def fetch_custom_face_detail(self, *, count=48):
        return await self.bot.call_action("fetch_custom_face_detail", count=count)
