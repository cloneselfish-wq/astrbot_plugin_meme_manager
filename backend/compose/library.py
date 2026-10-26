"""底图库：读取 manifest，按轮次挑一张可用的透明底贴图。

底图全部来自 haneoka 的 textless 贴图：RGBA 透明背景、无原生文字。
透明通道既是「能放字」的保证，也是下面这套几何元数据的来源。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

MANIFEST_FILE = "manifest.json"
# 主体占画布面积低于这个值就认为图是空的（实测空图 coverage 精确为 0）
MIN_COVERAGE = 0.02


@dataclass(frozen=True)
class BaseStamp:
    """一张可用底图及其几何信息。"""

    id: str
    path: Path
    name: str
    characters: tuple[str, ...]
    width: int
    height: int
    centroid_x: float
    coverage: float

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 1.0

    @property
    def leans_left(self) -> bool:
        return self.centroid_x < 0.44

    @property
    def leans_right(self) -> bool:
        return self.centroid_x > 0.56


class BaseLibrary:
    """底图池。

    目录布局约定::

        stamp_base/            # 只读：随插件分发的内置素材
        ├── manifest.json
        └── base/*.png

        <plugin_data>/compose/bases/   # 可写：用户自己上传的底图
        └── custom_*.png

    自传底图刻意放在数据目录而不是插件目录：插件升级/重装时会整体覆盖
    插件目录，放里面等于每次更新都清空用户上传的东西。清单里用
    ``scope`` 字段区分两类素材。
    """

    def __init__(self, root: Path | str, custom_dir: Path | str | None = None):
        self.root = Path(root)
        self.base_dir = self.root / "base"
        self.custom_dir = Path(custom_dir) if custom_dir else None
        self._stamps: list[BaseStamp] | None = None
        self._mtime: float = 0.0

    @property
    def ready(self) -> bool:
        return self.base_dir.is_dir() and (self.root / MANIFEST_FILE).is_file()

    def _resolve_file(self, item: dict, file_name: str) -> Path:
        """按条目的 ``scope`` 决定去哪个目录找文件。"""
        if str(item.get("scope") or "builtin") == "custom" and self.custom_dir:
            return self.custom_dir / file_name
        return self.base_dir / file_name

    def _load(self) -> list[BaseStamp]:
        manifest_path = self.root / MANIFEST_FILE
        try:
            mtime = manifest_path.stat().st_mtime
        except OSError:
            return []
        if self._stamps is not None and mtime == self._mtime:
            return self._stamps

        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

        stamps: list[BaseStamp] = []
        for item in raw.get("items") or []:
            if not isinstance(item, dict):
                continue
            # 用户在 WebUI 里停用的底图不参与挑选，但条目保留以便随时恢复
            if item.get("disabled"):
                continue
            file_name = str(item.get("file") or "")
            if not file_name:
                continue
            path = self._resolve_file(item, file_name)
            if not path.is_file():
                continue
            size = item.get("size") or [0, 0]
            centroid = item.get("centroid") or [0.5, 0.5]
            try:
                width, height = int(size[0]), int(size[1])
                centroid_x = float(centroid[0])
            except (TypeError, ValueError, IndexError):
                continue
            if width <= 0 or height <= 0:
                continue
            coverage = float(item.get("coverage") or 0.0)
            # haneoka 的 textless 清单里混进了 20 张「抹字后剩下的空白」，
            # 它们有尺寸但 alpha 全为 0，画上去就是一张白纸。直接剔除。
            if coverage <= MIN_COVERAGE:
                continue
            stamps.append(
                BaseStamp(
                    id=str(item.get("id") or path.stem),
                    path=path,
                    name=str(item.get("nameZh") or item.get("id") or path.stem),
                    characters=tuple(str(c) for c in item.get("characters") or ()),
                    width=width,
                    height=height,
                    centroid_x=centroid_x,
                    coverage=coverage,
                )
            )

        self._stamps = stamps
        self._mtime = mtime
        return stamps

    def all(self) -> list[BaseStamp]:
        return list(self._load())

    def pick(self, variation: str, exclude: set[str] | None = None) -> BaseStamp | None:
        """按轮次种子挑一张，避开最近用过的。

        底图是角色立绘而不是纯色块，连续两轮出现同一个角色会很跳，
        所以调用方可以传 ``exclude`` 传最近用过的 id。
        """
        stamps = self._load()
        if not stamps:
            return None
        excluded = exclude or set()
        pool = [s for s in stamps if s.id not in excluded]
        if not pool:
            pool = stamps
        digest = hashlib.sha256(f"base|{variation}".encode("utf-8")).hexdigest()
        return pool[int(digest[:8], 16) % len(pool)]

    def get(self, stamp_id: str) -> BaseStamp | None:
        for stamp in self._load():
            if stamp.id == stamp_id:
                return stamp
        return None
