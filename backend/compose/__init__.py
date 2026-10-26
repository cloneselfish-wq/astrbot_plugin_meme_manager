"""底图配字合成：把一句话排到透明底贴图上，输出可直接发送的 PNG。

与图库检索并列但互斥：命中合成的轮次只暴露 ``compose_sticker`` 工具，
不再暴露 ``search_memes``，因此一轮回复物理上不可能同时发出两张图。
"""
from __future__ import annotations

from .coverage import font_covers, missing_chars
from .engine import Style, compose, flatten
from .library import BaseLibrary, BaseStamp
from .service import ComposeService, ComposeSettings, sanitize_caption

__all__ = [
    "BaseLibrary",
    "BaseStamp",
    "ComposeService",
    "ComposeSettings",
    "Style",
    "compose",
    "flatten",
    "font_covers",
    "missing_chars",
    "sanitize_caption",
]
