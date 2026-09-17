"""文本清洗与噪音识别（纯函数，方便单独测试）。"""

from __future__ import annotations

import re

# 纯语气词/笑声（哈哈哈、嘿嘿、呵呵、hahaha…）
LAUGHTER_RE = re.compile(r"^(?:(?:哈|嘻|嘿|呵)+|(?:ha+|he+|hi+|h+)+)$", re.IGNORECASE)

# 常用 emoji 范围（"纯表情"噪音识别）
EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U0001F1E6-\U0001F1FF"
    "\u2600-\u27BF"
    "\u2B00-\u2BFF"
    "\u2190-\u21FF"
    "\uFE0F\u200D\u2764\u2B50"
    "]"
)

EFFECTIVE_CHAR_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]")
URL_RE = re.compile(r"https?://\S+")

# 默认噪音词（整条消息就是这类词时直接丢掉）
DEFAULT_NOISE_WORDS = ("打卡", "签到", "冒泡", "收到", "顶", "+1")


def clean_text(text: str) -> str:
    """压掉多余空白，并把换行折成空格。"""

    return " ".join(str(text or "").replace("\u200b", "").split())


def clip(text: str, limit: int = 200) -> str:
    body = clean_text(text)
    return body if len(body) <= limit else body[:limit] + "…"


def is_noise(text: str, *, min_length: int = 2, noise_words: list[str] | None = None) -> bool:
    """明显不值得判断的消息：纯表情、纯符号、太短、纯语气词、打卡类复读。"""

    body = clean_text(text)
    if not body:
        return True
    if EMOJI_RE.sub("", body).strip() == "":
        return True
    if not EFFECTIVE_CHAR_RE.search(body):
        return True
    effective = "".join(EFFECTIVE_CHAR_RE.findall(body))
    if len(effective) < max(0, min_length):
        return True
    if LAUGHTER_RE.match(effective):
        return True
    stripped_url = URL_RE.sub("", body).strip()
    if not stripped_url:
        # 整条就是一个（或几个）链接
        return True
    lowered = body.lower()
    words = DEFAULT_NOISE_WORDS if noise_words is None else noise_words
    return any(
        word and word.lower() in lowered and len(body) <= len(word) + 2 for word in words
    )


def looks_like_question(text: str) -> bool:
    body = clean_text(text)
    return body.endswith(("?", "？")) or "怎么" in body or "为什么" in body or "吗" in body
