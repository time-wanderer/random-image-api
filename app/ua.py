from __future__ import annotations

from typing import Literal

ClientType = Literal["desktop", "mobile"]

MOBILE_TOKENS = (
    "iphone",
    "ipod",
    "ipad",
    "android",
    "mobile",
    "mobi",
    "webos",
    "blackberry",
    "iemobile",
    "opera mini",
    "opera mobi",
    "windows phone",
    "phone",
)


def detect_client_type(user_agent: str | None) -> ClientType:
    if not user_agent:
        return "desktop"
    ua = user_agent.lower()
    if any(token in ua for token in MOBILE_TOKENS):
        return "mobile"
    return "desktop"


def describe_user_agent(user_agent: str | None) -> str:
    if not user_agent:
        return "unknown"
    ua = user_agent.lower()
    if "iphone" in ua or "ipod" in ua:
        return "iphone"
    if "ipad" in ua:
        return "ipad"
    if "android" in ua:
        return "android"
    if "windows nt" in ua or "windows" in ua:
        return "windows"
    if "macintosh" in ua or "mac os x" in ua:
        return "macos"
    if "linux" in ua or "x11" in ua:
        return "linux"
    return "other"
