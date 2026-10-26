"""字体字形覆盖查表。

插件运行时不再依赖 fontTools：覆盖信息在素材准备阶段一次性导出成码点区间，
这里只做二分查找。目的是**渲染前就能判定会不会画出豆腐块**，
而不是等图出来才发现满屏 □。
"""
from __future__ import annotations

import json
from bisect import bisect_right
from functools import lru_cache
from pathlib import Path

COVERAGE_FILE = "coverage.json"


@lru_cache(maxsize=4)
def _load_ranges(path: str, font_name: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """把区间列表拆成 starts/ends 两个有序元组，供二分使用。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (), ()
    fonts = raw.get("fonts") if isinstance(raw, dict) else None
    entry = fonts.get(font_name) if isinstance(fonts, dict) else None
    ranges = entry.get("ranges") if isinstance(entry, dict) else None
    if not isinstance(ranges, list):
        return (), ()
    starts: list[int] = []
    ends: list[int] = []
    for item in ranges:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        starts.append(int(item[0]))
        ends.append(int(item[1]))
    return tuple(starts), tuple(ends)


@lru_cache(maxsize=4)
def has_coverage_table(font_dir: str) -> bool:
    return (Path(font_dir) / COVERAGE_FILE).is_file()


def _covered(codepoint: int, starts: tuple[int, ...], ends: tuple[int, ...]) -> bool:
    if not starts:
        return False
    index = bisect_right(starts, codepoint) - 1
    return index >= 0 and codepoint <= ends[index]


def missing_chars(font_dir: Path | str, font_name: str, text: str) -> tuple[str, ...]:
    """返回该字体渲染不出来的字符。

    覆盖表缺失时返回空元组（乐观放行），调用方仍有描边兜底，
    但不该把「表没生成」误判成「字全都支持」——所以由 ``has_coverage_table``
    单独给出可信度。
    """
    starts, ends = _load_ranges(str(Path(font_dir) / COVERAGE_FILE), str(font_name))
    if not starts:
        return ()
    return tuple(
        ch for ch in text if not ch.isspace() and not _covered(ord(ch), starts, ends)
    )


def font_covers(font_dir: Path | str, font_name: str, text: str) -> bool:
    """整句预检：有一个字画不出来就返回 False。"""
    return not missing_chars(font_dir, font_name, text)


def coverage_ratio(font_dir: Path | str, font_name: str, text: str) -> float:
    """该字体能画出的字符占比，用于在多字体间挑最优的替补。"""
    visible = [ch for ch in text if not ch.isspace()]
    if not visible:
        return 1.0
    return 1.0 - len(missing_chars(font_dir, font_name, text)) / len(visible)
