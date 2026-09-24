"""Small, provider-independent routing rules from the old Rolebot."""

import hashlib
import re
from datetime import datetime
from zoneinfo import ZoneInfo


def bounded(value, default, low, high):
    try:
        return min(high, max(low, int(value)))
    except (TypeError, ValueError):
        return default


def duration(text):
    match = re.fullmatch(r"(\d+)([smh]?)", text.lower())
    if not match:
        raise ValueError("duration")
    seconds = int(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]
    if not 1 <= seconds <= 604800:
        raise ValueError("duration")
    return seconds


def intent(text):
    text = text.strip().lower()
    if re.search(r"(?:别|不要|不用|无需|不必).{0,6}(?:搜|查|联网)", text):
        return "none"
    if re.search(
        r"(?:现在|目前|今天|今日|北京时间)?.{0,2}(?:几点(?:了|钟)?|几号|星期几|周几|几月几日)|what time is it",
        text,
    ):
        return "time"
    if re.search(r"搜索|搜一下|搜一搜|搜搜|查一下|帮我查|联网|search for|look up", text):
        return "search"
    if re.search(r"天气|气温|天气预报|汇率|股价|热搜|新闻", text) and re.search(
        r"今天|明天|后天|现在|最近|最新|多少|怎么样|如何|怎样", text
    ):
        return "search"
    if re.search(r"最新|最近|目前|当前", text) and re.search(
        r"版本|公告|更新|发布|赛果|比分|价格", text
    ):
        return "search"
    return "none"


def voice_requested(text):
    return bool(
        re.search(
            r"(?:发|用|来|说|读|听).{0,6}语音|语音.{0,4}(?:说|读|回复)|念给我听|读给我听|听听你的声音",
            text,
        )
    ) and not re.search(r"(?:别|不要|不用).{0,5}(?:语音|念|读)", text)


def clock_context():
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    weekday = "一二三四五六日"[now.weekday()]
    return f"现实世界北京时间：{now:%Y-%m-%d %H:%M:%S}，星期{weekday}（Asia/Shanghai）。与泰拉历法分开。"


class GroupPolicy:
    def __init__(self):
        self.followups = {}
        self.chains = {}
        self.cooldowns = {}
        self.quotes = {}
        self.replies = {}
        self.activity = {}
        self.repeat_groups = {}

    def prune(self, now):
        for table in (self.followups, self.cooldowns, self.quotes, self.repeat_groups):
            for key, until in list(table.items()):
                if until <= now:
                    table.pop(key, None)
        self.replies = {
            scope: recent
            for scope, stamps in self.replies.items()
            if (recent := [stamp for stamp in stamps if stamp > now - 60])
        }
        self.activity = {
            scope: stamp for scope, stamp in self.activity.items() if stamp > now - 3600
        }
        for key, value in list(self.chains.items()):
            if now - value[2] > 600:
                self.chains.pop(key, None)

    def followup(self, scope, user, text, now, keywords):
        return self.followups.get((scope, user), 0) > now and (
            "?" in text or "？" in text or any(k and k in text for k in keywords)
        )

    def can_reply(self, scope, now, interval=3, limit=6):
        recent = [stamp for stamp in self.replies.get(scope, []) if stamp > now - 60]
        return (not recent or now - recent[-1] >= interval) and (limit <= 0 or len(recent) < limit)

    def record_reply(self, scope, now):
        recent = [stamp for stamp in self.replies.get(scope, []) if stamp > now - 60]
        self.replies[scope] = [*recent, now]
        self.activity[scope] = now

    def can_random_reply(self, scope, now, cooldown=120):
        last = self.activity.get(scope)
        return last is None or now - last >= cooldown

    def repeat(
        self, scope, user, signature, now, threshold=2, window=600, cooldown=600, group_cooldown=0
    ):
        if not signature:
            self.chains.pop(scope, None)
            return False
        digest = hashlib.sha256(signature.encode()).hexdigest()
        previous = self.chains.get(scope)
        users = (
            previous[1]
            if previous and previous[0] == digest and now - previous[2] <= window
            else []
        )
        users = [*users, user][-threshold:]
        self.chains[scope] = (digest, users, now)
        if (
            len(users) < threshold
            or len(set(users)) < 2
            or self.cooldowns.get((scope, digest), 0) > now
            or self.repeat_groups.get(scope, 0) > now
        ):
            return False
        self.cooldowns[(scope, digest)] = now + cooldown
        self.repeat_groups[scope] = now + group_cooldown
        return True

    def quote(self, scope, user, now, seconds=60):
        if self.quotes.get((scope, user), 0) > now:
            return False
        self.quotes[(scope, user)] = now + seconds
        return True
