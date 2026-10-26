"""文字排版引擎。

核心只有三件事，和参考站点的 canvas 管线一一对应：

1. **逐字 cell** —— 每个字占一个等宽格子，格子内再叠加旋转、缩放、位移，
   这是「贴纸感」的来源，也是它区别于「图片加水印」的地方。
2. **先描边后填充** —— Pillow 的 ``stroke_width`` 直接完成，
   让字能压在角色身上而不糊。
3. **抖动必须可复现** —— 同一个 ``seed`` 出同一张图。
   seed 由调用方按轮次派生，所以既满足「每轮不一样」，
   又不会在同一轮里两次渲染出不同结果。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CANVAS = 512
PAD = 16
MIN_FONT_SIZE = 26
# 文字带最多占画布高度的比例，超过就压角色了
BAND_MAX_RATIO = 0.26


@dataclass(frozen=True)
class Style:
    font_path: str
    size: int = 96
    fill: str = "#FFFFFF"
    stroke: str = "#1F1F1F"
    stroke_ratio: float = 0.115
    rotate: float = 6.0
    y_jitter: float = 0.04
    scale_jitter: float = 0.045
    layout: str = "top"  # top | bottom | left | right
    char_gap: float = 0.02
    max_ratio: float = 0.94
    seed: str = ""


def load_font(path: str | Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def fit_size(
    text: str, style: Style, max_w: int, max_h: int, horiz: bool = True
) -> int:
    """按**实际绘制宽度**收敛字号。

    不能用 ``textbbox`` 反推：那是字形包围盒，而布局用的是等宽 cell
    (``size * (1 + gap)``)，两者不一致时长句会直接溢出画布。
    """
    size = style.size
    while size > MIN_FONT_SIZE:
        cell = size * (1.0 + style.char_gap)
        width = len(text) * cell if horiz else size * 1.06
        height = size * 1.06 if horiz else len(text) * cell
        if width <= max_w and height <= max_h:
            return size
        size = int(size * 0.92)
    return MIN_FONT_SIZE


def _jitter(seed: str, text: str, index: int, salt: int) -> float:
    """[-1, 1) 的确定性抖动。"""
    key = f"{seed}|{text}|{index}|{salt}".encode("utf-8")
    digest = hashlib.sha256(key).digest()
    return (int.from_bytes(digest[:4], "big") / 0xFFFFFFFF) * 2 - 1


def _draw_chars(
    canvas: Image.Image,
    text: str,
    style: Style,
    origin: tuple[int, int],
    horizontal: bool,
    canvas_size: int,
) -> None:
    size = style.size
    cell = size * (1.0 + style.char_gap)
    stroke = max(3, int(size * style.stroke_ratio))
    font = load_font(style.font_path, size)
    x0, y0 = origin

    # 每字先画进独立图层再旋转，避免整块文字被统一旋转
    pad = int(size * 0.95)
    side = pad * 2
    for index, char in enumerate(text):
        if char.isspace():
            continue
        layer = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text(
            (pad, pad),
            char,
            font=font,
            anchor="mm",
            fill=style.fill,
            stroke_width=stroke,
            stroke_fill=style.stroke,
        )

        angle = _jitter(style.seed, text, index, 1) * style.rotate
        scale = 1.0 + _jitter(style.seed, text, index, 2) * style.scale_jitter
        if abs(angle) > 0.3:
            layer = layer.rotate(angle, resample=Image.BICUBIC, expand=False)
        if abs(scale - 1.0) > 0.006:
            side_after = max(1, int(layer.width * scale))
            layer = layer.resize((side_after, side_after), Image.LANCZOS)

        dy = _jitter(style.seed, text, index, 3) * size * style.y_jitter
        if horizontal:
            cx = x0 + index * cell + cell / 2
            cy = y0 + size * 0.5 + dy
        else:
            dx = _jitter(style.seed, text, index, 4) * size * 0.03
            cx = x0 + size * 0.5 + dx
            cy = y0 + index * cell + cell / 2 + dy

        canvas.alpha_composite(
            layer, (int(cx - layer.width / 2), int(cy - layer.height / 2))
        )


def compose(
    base_path: Path | str,
    text: str,
    style: Style,
    canvas_size: int = CANVAS,
) -> Image.Image:
    """把 ``text`` 排到透明底贴图上，返回同尺寸 RGBA 图。"""
    source = Image.open(base_path).convert("RGBA")
    box = source.getbbox()
    if box:
        source = source.crop(box)

    horizontal = style.layout in ("top", "bottom")
    avail_w = int(canvas_size * style.max_ratio)
    if horizontal:
        size = fit_size(
            text,
            style,
            avail_w,
            int(canvas_size * BAND_MAX_RATIO),
            horiz=True,
        )
    else:
        # 竖排只占一列宽度，短句沿用横排的初始字号会显得单薄，
        # 所以给竖排更高的起点，让 2-3 字能撑满整列。
        vertical_style = replace(
            style, size=max(style.size, int(canvas_size * BAND_MAX_RATIO))
        )
        size = fit_size(
            text,
            vertical_style,
            int(canvas_size * BAND_MAX_RATIO),
            int(canvas_size * 0.92),
            horiz=False,
        )

    stroke = max(3, int(size * style.stroke_ratio))
    # 文字带要装得下字形、两侧描边和上下留白，否则描边会被画布裁掉
    glyph = size * 1.10 + stroke * 2
    band = int(glyph) + PAD * 2
    sized = replace(style, size=size)

    canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))

    if horizontal:
        role_w = canvas_size - PAD * 2
        role_h = canvas_size - band - PAD
    else:
        role_w = canvas_size - band - PAD
        role_h = canvas_size - PAD * 2

    scale = min(role_w / source.width, role_h / source.height)
    role = source.resize(
        (max(1, int(source.width * scale)), max(1, int(source.height * scale))),
        Image.LANCZOS,
    )

    # 字在上 → 角色贴底，其余同理：角色永远被推到与文字带相反的一侧
    if style.layout == "top":
        rx, ry = (canvas_size - role.width) // 2, canvas_size - role.height
    elif style.layout == "bottom":
        rx, ry = (canvas_size - role.width) // 2, 0
    elif style.layout == "left":
        rx, ry = canvas_size - role.width, (canvas_size - role.height) // 2
    else:
        rx, ry = 0, (canvas_size - role.height) // 2
    canvas.alpha_composite(role, (rx, ry))

    cell = size * (1.0 + style.char_gap)
    inner = int(glyph)
    if horizontal:
        tx = int((canvas_size - len(text) * cell) / 2)
        ty = (
            int((band - inner) / 2)
            if style.layout == "top"
            else int(canvas_size - band + (band - inner) / 2)
        )
    else:
        tx = (
            int((band - inner) / 2)
            if style.layout == "left"
            else int(canvas_size - band + (band - inner) / 2)
        )
        ty = int((canvas_size - len(text) * cell) / 2)

    _draw_chars(canvas, text, sized, (tx, ty), horizontal, canvas_size)
    return canvas


def flatten(image: Image.Image, background: str = "#FFFFFF") -> Image.Image:
    """合成到不透明底色。

    QQ 客户端对透明 PNG 的处理不一致，部分安卓版本会显示黑底，
    所以发送前统一压到白底。
    """
    output = Image.new("RGB", image.size, background)
    output.paste(image, (0, 0), image)
    return output
