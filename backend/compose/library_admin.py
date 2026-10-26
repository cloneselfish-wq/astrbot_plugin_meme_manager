"""底图库的文件级管理：列表、上传、停用、删除、改名。

这里的职责是「把用户上传的图安全地放进底图池」以及「让用户在 WebUI 里
看清每张底图到底能不能用」。判定标准与 :mod:`backend.compose.library`
保持一致，否则会出现「页面上显示 64 张，实际只有 60 张能出图」这种鬼故事：

* 必须是带 alpha 通道的 PNG；JPEG 一律拒绝，因为透明底是这个功能的立身之本。
* 主体（alpha > 8 的像素）占画布比例低于 ``MIN_COVERAGE`` 的图会被运行时过滤，
  上传时就该拒绝并说明原因，而不是让用户纳闷「为什么加了图却不出」。
* 内置底图不物理删除，只标记 ``disabled``；自传底图才真删。
  这样误停用的内置图可以一键恢复，而自传的图删了不占空间。

**存储位置**：自传底图写在数据目录（``<plugin_data>/compose/bases``），
不写进插件目录。插件升级会整体覆盖插件目录，放里面等于每次更新都丢用户素材。
清单条目用 ``scope: "custom"`` 标记，运行时按 scope 决定去哪个目录取文件。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import time
from pathlib import Path

from PIL import Image, UnidentifiedImageError

MANIFEST_FILE = "manifest.json"
BASE_DIR_NAME = "base"

# 与 backend.compose.library.MIN_COVERAGE 保持一致
MIN_COVERAGE = 0.02
# 认为「有内容」的 alpha 阈值：留一点余量，避免抗锯齿边缘的极淡像素算成主体
ALPHA_SOLID = 8

MAX_BYTES = 8 * 1024 * 1024
MIN_SIDE = 48
MAX_SIDE = 1200  # 超过就等比缩小，底图最终只画到 512 画布里
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# 缩略图/预览用的降采样边长，算重心不需要全分辨率
_CENTROID_SAMPLE = 64


class BaseLibraryError(Exception):
    """带错误码的底图库异常，方便前端区分「重复」和「非法」。"""

    def __init__(self, message: str, code: str = "invalid"):
        super().__init__(message)
        self.code = code

    @property
    def message(self) -> str:
        return str(self)


# ---------------------------------------------------------------- 路径与清单


def base_root(plugin_dir: Path | str) -> Path:
    """返回随插件分发的 ``stamp_base`` 目录。"""
    return Path(plugin_dir) / "stamp_base"


def _base_dir(root: Path) -> Path:
    return root / BASE_DIR_NAME


def _manifest_path(root: Path) -> Path:
    return root / MANIFEST_FILE


def _entry_dir(item: dict, base_dir: Path, custom_dir: Path | None) -> Path:
    """按条目的 ``scope`` 决定文件所在目录。"""
    if str(item.get("scope") or "builtin") == "custom" and custom_dir is not None:
        return custom_dir
    return base_dir


def load_manifest(root: Path | str) -> dict:
    """读取清单；结构异常统一转成可读错误，避免前端 500。"""
    root = Path(root)
    path = _manifest_path(root)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BaseLibraryError(
            "未找到 stamp_base/manifest.json，插件素材不完整", "missing_manifest"
        ) from exc
    except (OSError, ValueError) as exc:
        raise BaseLibraryError(f"底图清单无法解析：{exc}", "bad_manifest") from exc
    if not isinstance(raw, dict):
        raise BaseLibraryError("底图清单结构异常", "bad_manifest")
    if not isinstance(raw.get("items"), list):
        raw["items"] = []
    return raw


def save_manifest(root: Path | str, manifest: dict) -> None:
    """原子写清单。

    ``BaseLibrary`` 用清单的 mtime 判断缓存是否失效，所以这里必须
    让「写完」和「mtime 变化」是同一件事：先写临时文件再 replace，
    不会出现读到半截 JSON 的窗口。
    """
    root = Path(root)
    path = _manifest_path(root)
    # 不加末尾换行：原始 manifest 生成于 indent=1 且无结尾换行，
    # 加一字节会让 git diff 报「文件改了」但看不到任何行变化。
    payload = json.dumps(manifest, ensure_ascii=False, indent=1)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".manifest-", suffix=".tmp", dir=str(root.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 图像分析


def analyze_image(data: bytes) -> dict:
    """校验并算出底图池需要的几何信息。

    Returns:
        含 ``width`` / ``height`` / ``coverage`` / ``centroid_x`` /
        ``opaque`` / ``image`` 的字典。``image`` 是已 RGBA 化、必要时缩小过的
        图片对象，调用方直接拿去编码保存。
    """
    if not data:
        raise BaseLibraryError("上传内容为空", "empty")
    if len(data) > MAX_BYTES:
        raise BaseLibraryError(
            f"图片超过 {MAX_BYTES // 1024 // 1024}MB 上限", "too_large"
        )
    if not data.startswith(PNG_MAGIC):
        raise BaseLibraryError(
            "只支持带透明背景的 PNG。JPEG/WebP 没有 alpha 通道，"
            "配上文字会压在角色身上糊成一片",
            "not_png",
        )

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise BaseLibraryError(f"图片无法读取：{exc}", "broken_image") from exc

    image = image.convert("RGBA")

    width, height = image.size
    if min(width, height) < MIN_SIDE:
        raise BaseLibraryError(
            f"图片太小（{width}×{height}），短边至少 {MIN_SIDE} 像素", "too_small"
        )
    if max(width, height) > MAX_SIDE:
        ratio = MAX_SIDE / max(width, height)
        image = image.resize(
            (max(1, int(width * ratio)), max(1, int(height * ratio))),
            Image.LANCZOS,
        )
        width, height = image.size

    alpha = image.getchannel("A")
    histogram = alpha.histogram()
    total = max(1, width * height)
    solid = sum(histogram[ALPHA_SOLID + 1 :])
    coverage = solid / total
    if coverage < MIN_COVERAGE:
        raise BaseLibraryError(
            f"这张图几乎全透明（主体只占 {coverage * 100:.1f}%），"
            "配上文字会像一张白纸。请换一张主体完整的图",
            "too_transparent",
        )

    # 重心用降采样后的列投影算，全分辨率逐像素在 Web 请求里太慢
    sample = alpha.resize((_CENTROID_SAMPLE, _CENTROID_SAMPLE), Image.BILINEAR)
    pixels = list(sample.getdata())
    columns = [0] * _CENTROID_SAMPLE
    for index, value in enumerate(pixels):
        if value > ALPHA_SOLID:
            columns[index % _CENTROID_SAMPLE] += value
    weight = sum(columns)
    centroid_x = 0.5
    if weight:
        centroid_x = sum(
            index * value for index, value in enumerate(columns)
        ) / (weight * _CENTROID_SAMPLE)

    return {
        "width": width,
        "height": height,
        "coverage": round(coverage, 4),
        "centroid_x": round(centroid_x, 4),
        "opaque": alpha.getextrema() == (255, 255),
        "image": image,
    }


def _encode_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG", optimize=True)
    return buffer.getvalue()


# ---------------------------------------------------------------- 查询


def _entry_view(item: dict, base_dir: Path, custom_dir: Path | None) -> dict:
    """把清单条目整理成前端直接能渲染的形状。"""
    file_name = str(item.get("file") or "")
    size = item.get("size") or [0, 0]
    centroid = item.get("centroid") or [0.5, 0.5]
    try:
        width, height = int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        width, height = 0, 0
    try:
        centroid_x = float(centroid[0])
    except (TypeError, ValueError, IndexError):
        centroid_x = 0.5
    coverage = float(item.get("coverage") or 0.0)
    disabled = bool(item.get("disabled"))
    exists = bool(file_name) and (
        _entry_dir(item, base_dir, custom_dir) / file_name
    ).is_file()
    return {
        "id": str(item.get("id") or Path(file_name).stem),
        "file": file_name,
        "name": str(item.get("nameZh") or item.get("name") or Path(file_name).stem),
        "characters": [str(c) for c in item.get("characters") or ()],
        "width": width,
        "height": height,
        "coverage": round(coverage, 4),
        "centroid_x": round(centroid_x, 4),
        "custom": bool(item.get("custom")),
        "scope": str(item.get("scope") or "builtin"),
        "disabled": disabled,
        "exists": exists,
        # 运行时口径：存在 + 未停用 + 主体够多
        "usable": exists and not disabled and coverage > MIN_COVERAGE,
        "size_bytes": int(item.get("bytes") or 0),
        "added_at": item.get("addedAt") or "",
    }


def list_bases(root: Path | str, custom_dir: Path | str | None = None) -> dict:
    """列出全部底图与统计信息。"""
    root = Path(root)
    base_dir = _base_dir(root)
    custom_path = Path(custom_dir) if custom_dir else None
    manifest = load_manifest(root)
    entries = [
        _entry_view(item, base_dir, custom_path)
        for item in manifest.get("items") or []
        if isinstance(item, dict)
    ]
    # 自传的排在前面，方便刚上传就能看到
    entries.sort(key=lambda item: (not item["custom"], item["name"]))
    stats = {
        "total": len(entries),
        "usable": sum(1 for item in entries if item["usable"]),
        "custom": sum(1 for item in entries if item["custom"]),
        "disabled": sum(1 for item in entries if item["disabled"]),
        "missing": sum(1 for item in entries if not item["exists"]),
    }
    return {
        "bases": entries,
        "stats": stats,
        "base_dir": str(base_dir),
        "custom_dir": str(custom_path) if custom_path else "",
        "min_coverage": MIN_COVERAGE,
        "max_side": MAX_SIDE,
        "max_bytes": MAX_BYTES,
    }


# ---------------------------------------------------------------- 变更


def _clean_name(value: str | None, fallback: str) -> str:
    text = str(value or "").strip()
    # 名字只用于展示，去掉控制字符和超长部分即可
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:40] or fallback


def add_base(
    root: Path | str,
    data: bytes,
    file_name: str = "",
    name: str = "",
    custom_dir: Path | str | None = None,
) -> dict:
    """保存一张底图并写入清单。"""
    root = Path(root)
    base_dir = _base_dir(root)
    target_dir = Path(custom_dir) if custom_dir else base_dir
    manifest = load_manifest(root)
    analysis = analyze_image(data)
    image = analysis.pop("image")

    encoded = _encode_png(image)
    # 原始字节的哈希用于去重：清单里内置图的 sha256 就是「源文件」哈希，
    # 而我们重新编码后的字节一定与源文件不同，只比编码结果会永远匹配不上。
    raw_digest = hashlib.sha256(data).hexdigest()
    encoded_digest = hashlib.sha256(encoded).hexdigest()

    for item in manifest.get("items") or []:
        if not isinstance(item, dict):
            continue
        known = {
            str(item.get("sha256") or ""),
            str(item.get("encodedSha256") or ""),
        }
        if raw_digest in known or encoded_digest in known:
            raise BaseLibraryError(
                f"这张图和底图「{item.get('nameZh') or item.get('file')}」内容完全相同",
                "duplicate",
            )

    target_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(file_name or "base").stem
    base_id = f"custom_{encoded_digest[:12]}"
    target_name = f"{base_id}.png"
    target = target_dir / target_name

    # 先写临时文件再 replace，避免出现「清单里有、文件是半截」的状态
    fd, tmp_name = tempfile.mkstemp(
        prefix=".base-", suffix=".tmp", dir=str(target_dir)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    item = {
        "id": base_id,
        "file": target_name,
        "scope": "custom" if custom_dir else "builtin",
        "sha256": raw_digest,
        "encodedSha256": encoded_digest,
        "size": [analysis["width"], analysis["height"]],
        "centroid": [analysis["centroid_x"], 0.5],
        "coverage": analysis["coverage"],
        "nameZh": _clean_name(name, stem or "自定义底图"),
        "characters": [],
        "quality": "user-uploaded",
        "custom": True,
        "bytes": len(encoded),
        "addedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    manifest.setdefault("items", []).append(item)
    try:
        save_manifest(root, manifest)
    except Exception:
        # 清单没写成功就把文件删掉，不留孤儿
        try:
            target.unlink()
        except OSError:
            pass
        raise

    view = _entry_view(item, base_dir, Path(custom_dir) if custom_dir else None)
    return {
        **view,
        "opaque": analysis["opaque"],
        "message": (
            "已加入底图池。注意这张图没有透明背景，配字会直接压在画面上，"
            "建议先试一下排版效果。"
            if analysis["opaque"]
            else "已加入底图池"
        ),
    }


def _find_index(manifest: dict, base_id: str) -> int:
    for index, item in enumerate(manifest.get("items") or []):
        if isinstance(item, dict) and str(item.get("id")) == str(base_id):
            return index
    raise BaseLibraryError("底图不存在", "not_found")


def delete_base(
    root: Path | str,
    base_id: str,
    custom_dir: Path | str | None = None,
) -> dict:
    """删除一张**自传**底图；内置底图只允许停用。"""
    root = Path(root)
    base_dir = _base_dir(root)
    custom_path = Path(custom_dir) if custom_dir else None
    manifest = load_manifest(root)
    index = _find_index(manifest, base_id)
    item = manifest["items"][index]

    file_name = str(item.get("file") or "")
    target = _entry_dir(item, base_dir, custom_path) / file_name if file_name else None
    file_exists = bool(target and target.is_file())

    # 内置图只允许停用；但如果它指向的文件本来就不在（比如清单残留），
    # 允许把这条无效记录清掉，否则管理页会永远挂着一张打不开的卡片。
    if not item.get("custom") and file_exists:
        raise BaseLibraryError(
            "内置底图不提供删除，可以改用「停用」；停用后随时能恢复",
            "builtin",
        )

    if file_exists and target is not None:
        try:
            target.unlink()
        except OSError as exc:
            raise BaseLibraryError(f"删除文件失败：{exc}", "io_error") from exc

    manifest["items"].pop(index)
    save_manifest(root, manifest)
    return {"deleted": str(base_id), "file": file_name}


def set_disabled(
    root: Path | str,
    base_id: str,
    disabled: bool,
    custom_dir: Path | str | None = None,
) -> dict:
    """停用/启用一张底图。

    停用只是从运行时池里移出，文件保留，所以内置图可以随时恢复。
    """
    root = Path(root)
    manifest = load_manifest(root)
    index = _find_index(manifest, base_id)
    item = manifest["items"][index]
    if disabled:
        item["disabled"] = True
    else:
        item.pop("disabled", None)
    save_manifest(root, manifest)
    return _entry_view(
        item,
        _base_dir(root),
        Path(custom_dir) if custom_dir else None,
    )


def rename_base(
    root: Path | str,
    base_id: str,
    name: str,
    custom_dir: Path | str | None = None,
) -> dict:
    """改底图的显示名。"""
    root = Path(root)
    manifest = load_manifest(root)
    index = _find_index(manifest, base_id)
    item = manifest["items"][index]
    item["nameZh"] = _clean_name(name, Path(str(item.get("file") or "底图")).stem)
    save_manifest(root, manifest)
    return _entry_view(
        item,
        _base_dir(root),
        Path(custom_dir) if custom_dir else None,
    )


def resolve_base_path(
    root: Path | str,
    base_id: str,
    custom_dir: Path | str | None = None,
) -> Path:
    """按 id 找到底图文件，供预览/缩略图使用。"""
    root = Path(root)
    base_dir = _base_dir(root)
    custom_path = Path(custom_dir) if custom_dir else None
    manifest = load_manifest(root)
    index = _find_index(manifest, base_id)
    item = manifest["items"][index]
    file_name = str(item.get("file") or "")
    if not file_name:
        raise BaseLibraryError("该条目没有关联文件", "not_found")

    directory = _entry_dir(item, base_dir, custom_path).resolve()
    path = (directory / Path(file_name).name).resolve()
    try:
        path.relative_to(directory)
    except ValueError as exc:
        raise BaseLibraryError("非法路径", "bad_path") from exc
    if not path.is_file():
        raise BaseLibraryError("文件已丢失", "missing_file")
    return path
