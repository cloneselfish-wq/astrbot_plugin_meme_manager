"""合成服务：把「一句话」变成一张可直接发送的图片。

这里负责引擎之外的工程部分：

* **会话节奏** —— 按会话计数而非每轮独立掷骰，命中位置在一个窗口内是均匀的，
  避免出现「连续十轮都不出」或「连着三轮都出」的观感。
* **熔断** —— 连续渲染失败三次停十分钟，防止字体/磁盘异常时刷日志。
* **落盘与回收** —— 生成的图放在插件数据目录，按数量回收，不会无限增长。
"""
from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from .engine import Style, compose, flatten
from .library import BaseLibrary, BaseStamp
from .styles import plan_style

# 一次合成最多同时跑两张：服务器可用内存只剩 1G 出头，512×512 RGBA 峰值约 4MB
_RENDER_SEMAPHORE = threading.Semaphore(2)

MAX_STORED_FILES = 300
FAIL_STREAK_LIMIT = 3
COOLDOWN_SECONDS = 600

# 文案里只保留「字」，标点仅留少量有表现力的
_DROP_CHARS = set("&[]{}<>|\\/`*#@$%^_=+~")
_KEEP_PUNCT = set("？！?!…。.·")


def sanitize_caption(text: str) -> str:
    """把模型给的文案压成能画进图里的一行字。

    emoji、箭头、括号等字形大多不在中文字体里，留着只会变成豆腐块，
    所以这里用白名单过滤而不是黑名单。
    """
    if not text:
        return ""
    buffer: list[str] = []
    for char in str(text):
        if char.isspace():
            continue
        if char in _DROP_CHARS:
            continue
        code = ord(char)
        if char in _KEEP_PUNCT:
            buffer.append(char)
            continue
        if 0x4E00 <= code <= 0x9FFF:  # 基本汉字
            buffer.append(char)
            continue
        if 0x3400 <= code <= 0x4DBF:  # 扩展 A
            buffer.append(char)
            continue
        if 0x30A0 <= code <= 0x30FF:  # 片假名，允许日文原文引用
            buffer.append(char)
            continue
        if 0x3040 <= code <= 0x309F:  # 平假名
            buffer.append(char)
            continue
        if 0x20 <= code <= 0x7E:  # ASCII
            buffer.append(char)
            continue
        if 0xFF01 <= code <= 0xFF5E:  # 全角 ASCII
            buffer.append(char)
            continue
        # 其余（emoji、装饰符号、控制字符）一律丢弃
    caption = "".join(buffer).strip()
    # 去掉被丢弃字符留下的成对标点空转，例如「，，」
    while caption and caption[0] in "，,、；;：:":
        caption = caption[1:]
    while caption and caption[-1] in "，,、；;：:":
        caption = caption[:-1]
    return caption


@dataclass
class ComposeSettings:
    """合成相关的运行时配置。"""

    enabled: bool = False
    session_ratio: int = 30
    max_chars: int = 8
    min_chars: int = 1
    # 留空表示自动：只在整句都能完整渲染的字体里轮换
    font: str = ""
    canvas: int = 512

    @property
    def ratio(self) -> int:
        return max(0, min(100, int(self.session_ratio)))


class SessionRhythm:
    """按会话计数决定哪几轮走合成。

    每个 ``WINDOW`` 轮里恰好命中 ``round(ratio * WINDOW)`` 次，
    具体位置由会话键与该窗口序号共同决定——对模型来说不可预测，
    但整体密度是准的。
    """

    WINDOW = 10

    def __init__(self) -> None:
        self._counters: OrderedDict[str, int] = OrderedDict()

    def hit(self, session_key: str, ratio_percent: int) -> bool:
        ratio = max(0, min(100, int(ratio_percent)))
        if ratio <= 0:
            return False
        if ratio >= 100:
            return True

        count = self._counters.get(session_key, 0) + 1
        self._counters[session_key] = count
        self._counters.move_to_end(session_key)
        if len(self._counters) > 512:
            self._counters.popitem(last=False)

        window_index, position = divmod(count - 1, self.WINDOW)
        hits = max(1, min(self.WINDOW - 1, round(ratio * self.WINDOW / 100)))
        digest = hashlib.sha256(
            f"{session_key}|{window_index}".encode("utf-8")
        ).hexdigest()
        # 用窗口种子做 Fisher-Yates，选出 hits 个命中位
        order = list(range(self.WINDOW))
        for i in range(self.WINDOW - 1, 0, -1):
            j = int(digest[(i * 3) % 60 : (i * 3) % 60 + 3], 16) % (i + 1)
            order[i], order[j] = order[j], order[i]
        return position in set(order[:hits])


class ComposeService:
    """无状态的合成入口，状态集中在实例上以便跨轮复用。"""

    def __init__(
        self,
        base_root: Path | str,
        font_dir: Path | str,
        output_dir: Path | str,
        settings: ComposeSettings | None = None,
    ):
        self.library = BaseLibrary(base_root)
        self.font_dir = Path(font_dir)
        self.output_dir = Path(output_dir)
        self.settings = settings or ComposeSettings()
        self.rhythm = SessionRhythm()
        self._recent: OrderedDict[str, list[str]] = OrderedDict()
        # 同一轮可能被调用多次（模型重试、钩子重入），底图必须锁死，
        # 否则「同一句话」在轮内会换角色，观感上像是两套逻辑在抢。
        self._round_bases: OrderedDict[str, str] = OrderedDict()
        self._fail_streak = 0
        self._cooldown_until = 0.0

    # ------------------------------------------------------------------ 状态

    @property
    def available(self) -> bool:
        return self.settings.enabled and self.library.ready

    def _cooling_down(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def _record_failure(self) -> None:
        self._fail_streak += 1
        if self._fail_streak >= FAIL_STREAK_LIMIT:
            self._cooldown_until = time.monotonic() + COOLDOWN_SECONDS
            self._fail_streak = 0

    def _record_success(self) -> None:
        self._fail_streak = 0

    def should_compose(self, session_key: str) -> bool:
        """本轮是否走合成路径。返回 True 时调用方必须只暴露合成工具。"""
        if not self.available or self._cooling_down():
            return False
        return self.rhythm.hit(session_key, self.settings.ratio)

    def _recent_bases(self, session_key: str) -> set[str]:
        return set(self._recent.get(session_key) or [])

    def _remember_base(self, session_key: str, stamp_id: str) -> None:
        used = self._recent.setdefault(session_key, [])
        if not used or used[-1] != stamp_id:
            used.append(stamp_id)
        del used[:-6]
        self._recent.move_to_end(session_key)
        if len(self._recent) > 512:
            self._recent.popitem(last=False)

    def _pick_base(self, variation: str, session_key: str) -> BaseStamp | None:
        cached_id = self._round_bases.get(variation)
        if cached_id:
            cached = self.library.get(cached_id)
            if cached is not None:
                return cached
        stamp = self.library.pick(variation, self._recent_bases(session_key))
        if stamp is not None:
            self._round_bases[variation] = stamp.id
            self._round_bases.move_to_end(variation)
            if len(self._round_bases) > 256:
                self._round_bases.popitem(last=False)
        return stamp

    # ------------------------------------------------------------------ 渲染

    def render_sync(
        self,
        text: str,
        variation: str,
        session_key: str = "",
        *,
        force_layout: str | None = None,
        base_id: str | None = None,
    ) -> dict:
        """同步渲染一张图，返回包含路径与诊断信息的字典。"""
        caption = sanitize_caption(text)
        if not caption:
            return {"ok": False, "reason": "文案为空或没有可渲染字符"}
        if len(caption) < self.settings.min_chars:
            return {"ok": False, "reason": "文案太短"}
        if len(caption) > self.settings.max_chars:
            return {
                "ok": False,
                "reason": (
                    f"文案 {len(caption)} 字，超过上限 {self.settings.max_chars} 字，"
                    "请压缩到更短的口语短句"
                ),
            }

        stamp: BaseStamp | None = None
        if base_id:
            stamp = self.library.get(base_id)
        if stamp is None:
            stamp = self._pick_base(variation, session_key)
        if stamp is None:
            return {"ok": False, "reason": "没有可用底图"}

        plan = plan_style(
            caption,
            variation,
            stamp,
            self.font_dir,
            prefer_font=self.settings.font,
            force_layout=force_layout,
        )
        if plan is None:
            return {
                "ok": False,
                "reason": "当前字体无法完整渲染这句文案（含生僻字或特殊符号），请换一种说法",
            }

        style = Style(
            font_path=plan.font_path,
            fill=plan.fill,
            stroke=plan.stroke,
            stroke_ratio=plan.stroke_ratio,
            rotate=plan.rotate,
            y_jitter=plan.y_jitter,
            scale_jitter=plan.scale_jitter,
            layout=plan.layout,
            seed=variation,
        )

        with _RENDER_SEMAPHORE:
            try:
                image = compose(stamp.path, caption, style, self.settings.canvas)
                image = flatten(image)
            except Exception as exc:  # noqa: BLE001 - 渲染失败要降级而不是中断回复
                self._record_failure()
                return {"ok": False, "reason": f"渲染失败: {exc}"}

        self.output_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(
            f"{caption}|{variation}|{stamp.id}".encode("utf-8")
        ).hexdigest()
        file_name = f"{int(time.time() * 1000)}_{digest[:16]}.png"
        path = self.output_dir / file_name
        try:
            image.save(path, "PNG", optimize=True)
        except OSError as exc:
            self._record_failure()
            return {"ok": False, "reason": f"保存失败: {exc}"}

        self._record_success()
        self._remember_base(session_key, stamp.id)
        self.cleanup()

        return {
            "ok": True,
            "id": f"meme:{digest[:32]}",
            "path": str(path),
            "caption": caption,
            "base_id": stamp.id,
            "base_name": stamp.name,
            "layout": plan.layout,
            "font": plan.font_name,
            "palette": plan.palette,
            "size": path.stat().st_size,
        }

    async def render(self, *args, **kwargs) -> dict:
        """异步包装：Pillow 是阻塞的，必须挪出事件循环。"""
        return await asyncio.to_thread(self.render_sync, *args, **kwargs)

    # ------------------------------------------------------------------ 回收

    def cleanup(self) -> None:
        """按数量回收历史图，避免数据目录无限增长。"""
        try:
            files = sorted(
                (p for p in self.output_dir.glob("*.png") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError:
            return
        for stale in files[: max(0, len(files) - MAX_STORED_FILES)]:
            try:
                stale.unlink()
            except OSError:
                pass
