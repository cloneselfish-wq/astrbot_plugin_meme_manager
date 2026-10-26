"""版式与配色分配。

两类问题在这里分开：

1. **能不能画** —— 字体覆盖预检，画不出来就整句拒绝，绝不产出豆腐块。
2. **怎么变** —— 版式/配色/逐字抖动都由 ``variation`` 派生。
   用户明确要求「每轮都不一样」，所以 ``variation`` 每轮注入一个新值；
   而同一轮内重复调用会拿到同一个 ``variation``，保证同轮结果稳定。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from .coverage import coverage_ratio, has_coverage_table
from .library import BaseStamp

# 中文首选：实测 GB2312 一级字库 3755 字全支持
DEFAULT_FONT_CHAIN = ("SSFangTangTi", "YurukaStd", "Pretendard-SemiBold")

# (填充, 描边)
PALETTES: dict[str, tuple[str, str]] = {
    "white_black": ("#FFFFFF", "#1F1F1F"),
    "yellow_black": ("#FFE04D", "#1F1F1F"),
    "white_navy": ("#FFFFFF", "#22305C"),
    "black_white": ("#2C2C2A", "#FFFFFF"),
}

# 横排永远可用；竖排只给短句，否则字号会掉到看不清
HORIZONTAL_LAYOUTS = ("top", "bottom")
VERTICAL_LAYOUTS = ("left", "right")
# 每多一个 top/bottom 权重，就多一份「角色居中、字在上下」的稳定观感
VERTICAL_MAX_CHARS = 5

STROKE_RATIO = 0.115


@dataclass(frozen=True)
class StylePlan:
    font_name: str
    font_path: str
    layout: str
    horizontal: bool
    palette: str
    fill: str
    stroke: str
    stroke_ratio: float
    rotate: float
    y_jitter: float
    scale_jitter: float

    def describe(self) -> str:
        orient = "横" if self.horizontal else "竖"
        return f"{self.font_name}/{self.layout}{orient}/{self.palette}"


def _hash_int(variation: str, salt: str, modulo: int) -> int:
    if modulo <= 1:
        return 0
    digest = hashlib.sha256(f"{salt}|{variation}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % modulo


def _hash_unit(variation: str, salt: str) -> float:
    """[0,1) 稳定随机数，用于抖动幅度。"""
    digest = hashlib.sha256(f"{salt}|{variation}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0x100000000


def layout_pool(char_count: int) -> tuple[str, ...]:
    """按字数给出候选版式。

    主体实测 83/84 居中，所以横排上下是主干；竖排只在 5 字以内出现，
    且比重压到很低，避免角色被压成一条。
    """
    if char_count <= VERTICAL_MAX_CHARS:
        return ("top", "bottom", "top", "bottom", "left", "right")
    return HORIZONTAL_LAYOUTS


def band_dark_ratio(stamp: BaseStamp, layout: str, band: float = 0.30) -> float:
    """估算文字带在合成到白底后的暗像素占比。

    白描边压在浅色角色上会糊成空心字，所以深色填充的配色
    只有在文字带足够暗时才进候选池。
    """
    from PIL import Image

    try:
        im = Image.open(stamp.path).convert("RGBA")
    except OSError:
        return 0.5
    box = im.getbbox()
    if box:
        im = im.crop(box)
    w, h = im.size
    if w <= 0 or h <= 0:
        return 0.5
    if layout in ("top", "bottom"):
        strip = (
            im.crop((0, 0, w, max(1, int(h * band))))
            if layout == "top"
            else im.crop((0, max(0, int(h * (1 - band))), w, h))
        )
    elif layout == "left":
        strip = im.crop((0, 0, max(1, int(w * band)), h))
    else:
        strip = im.crop((max(0, int(w * (1 - band))), 0, w, h))

    # 文字带里 alpha 越低越安全（合成后就是白底），用缩略图统计即可
    thumb = strip.convert("RGBA").resize((32, 32), Image.BILINEAR)
    bg = Image.new("RGBA", thumb.size, (255, 255, 255, 255))
    flat = Image.alpha_composite(bg, thumb).convert("L")
    pixels = list(flat.getdata())
    if not pixels:
        return 0.5
    return sum(1 for v in pixels if v < 110) / len(pixels)


def pick_palette(variation: str, dark_ratio: float) -> str:
    pool = ["white_black", "yellow_black", "white_navy"]
    if dark_ratio > 0.55:
        pool.append("black_white")
    return pool[_hash_int(variation, "palette", len(pool))]


def pick_font(
    text: str,
    font_dir: Path,
    prefer: str | None = None,
    variation: str = "",
) -> tuple[str, str] | None:
    """在**整句都能完整渲染**的字体里挑一套。

    只保留覆盖率 100% 的候选：混排出来的句子风格会断裂，
    而缺字直接画成豆腐块比不出图更糟，所以宁可返回 None。

    中文实测只有方堂 100% 覆盖 GB2312 一级字库，YurukaStd 覆盖日文汉字集
    （简体缺约三分之一），因此「字体随机」实际只在两句都能覆盖时才生效——
    这恰好是安全的随机。
    """
    if not has_coverage_table(str(font_dir)):
        return None

    chain = list(DEFAULT_FONT_CHAIN)
    if prefer and prefer in chain:
        chain.remove(prefer)
        chain.insert(0, prefer)

    usable: list[tuple[str, str]] = []
    for name in chain:
        path = font_dir / f"{name}.otf"
        if not path.is_file():
            continue
        if coverage_ratio(font_dir, name, text) >= 1.0:
            usable.append((name, str(path)))

    if not usable:
        return None
    if len(usable) == 1:
        return usable[0]
    # 指定了首选字体且它可用时优先，否则按轮次种子在多字体间轮换
    if prefer and usable[0][0] == prefer:
        return usable[0]
    index = _hash_int(variation, "font", len(usable)) if variation else 0
    return usable[index]


def plan_style(
    text: str,
    variation: str,
    stamp: BaseStamp,
    font_dir: Path,
    *,
    prefer_font: str | None = None,
    force_layout: str | None = None,
) -> StylePlan | None:
    """为一句文案决定字体、版式、配色与抖动幅度。"""
    picked = pick_font(
        text, font_dir, prefer=prefer_font, variation=variation
    )
    if not picked:
        return None
    font_name, font_path = picked

    if force_layout in ("top", "bottom", "left", "right"):
        layout = force_layout
    else:
        pool = layout_pool(len(text))
        layout = pool[_hash_int(variation, "layout", len(pool))]
    horizontal = layout in ("top", "bottom")

    palette = pick_palette(variation, band_dark_ratio(stamp, layout))
    fill, stroke = PALETTES[palette]

    # 抖动幅度每轮浮动一点，避免所有图都像同一个模板套出来的
    rotate = 4.0 + _hash_unit(variation, "rotate") * 5.0
    y_jitter = 0.025 + _hash_unit(variation, "yj") * 0.03
    scale_jitter = 0.02 + _hash_unit(variation, "sj") * 0.04

    return StylePlan(
        font_name=font_name,
        font_path=font_path,
        layout=layout,
        horizontal=horizontal,
        palette=palette,
        fill=fill,
        stroke=stroke,
        stroke_ratio=STROKE_RATIO,
        rotate=rotate,
        y_jitter=y_jitter,
        scale_jitter=scale_jitter,
    )
