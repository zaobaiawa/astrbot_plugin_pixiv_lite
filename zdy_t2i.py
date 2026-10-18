"""zdy_t2i —— 本地图文排版（纯本地，不联网）

版式：顶部一张图（原色使用，不做任何调色） + 下面纯白区域放文字与行内图片。
宽度由顶部图决定，永不横向变宽；白区高度随内容自动延长。
依赖：Pillow（唯一第三方依赖）

快速用法：
    import zdy_t2i
    data = zdy_t2i.render("你好世界")                       # -> bytes
    path = zdy_t2i.render_to_file("你好世界", out_dir="D:/cache")

行内图片与配色：
    zdy_t2i.render([("text", "看这个"), ("img", "D:/face.png")], color="multi")

接入 AstrBot（不阻塞事件循环）：
    path = await zdy_t2i.arender_to_file(text)
    yield event.image_result(path)

命令行：
    python zdy_t2i.py --text "你好世界" --out out.jpg
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import hashlib
import io
import os
import random
import re
import shutil
import tempfile
import threading
import zipfile
from collections import OrderedDict
from urllib.request import url2pathname

from PIL import Image, ImageDraw, ImageFont, ImageOps

__all__ = [
    "render",
    "render_to_file",
    "arender",
    "arender_to_file",
    "set_default_font",
    "set_default_top",
    "set_allowed_image_roots",
    "clear_cache",
    "LINE_SEP",
]

# --------------------------------------------------------------------------
# 默认值（都可被参数覆盖）
# --------------------------------------------------------------------------

#: 默认字体：字体文件路径 / 字体压缩包路径 / (压缩包路径, 包内字体名关键字)
DEFAULT_FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "resources", "MaokenZhuyuanTi.ttf")

#: 默认顶部图（原色贴在画布最上方）
DEFAULT_TOP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "resources", "top.png")

#: 与外部接口一致的强制换行符
LINE_SEP = "↔"

#: 白区填充色
BODY_COLOR = (255, 255, 255)

#: 默认字色：纯黑
INK = (0, 0, 0)

#: 中文禁则：这些字符不能出现在行首 / 行尾
_NO_LINE_START = "，。、；：！？）》」』】〕｝”’…—·,.;:!?)]}"
_NO_LINE_END = "（《「『【〔｛“‘([{"

_FONT_EXT = (".ttf", ".otf", ".ttc")

#: 白底上可读的彩色，供「单色 / 彩色」模式取用
_INK_COLORS = (
    "#000000", "#1F2933", "#B02A37", "#0B5ED7", "#0F5132",
    "#6F42C1", "#8A4B08", "#0B7285", "#C2255C", "#343A40",
)

_SEG_KINDS = {"text": "text", "t": "text", "img": "img", "image": "img", "i": "img"}
_FMT_EXT = {"PNG": ".png", "JPEG": ".jpg", "JPG": ".jpg", "WEBP": ".webp"}

_FONT_CACHE_DIR = os.path.join(tempfile.gettempdir(), "zdy_t2i_fonts")
_OUT_DIR = os.path.join(tempfile.gettempdir(), "zdy_t2i_out")

_DEFAULT_FONT = os.environ.get("ZDY_FONT") or DEFAULT_FONT
_DEFAULT_TOP = os.environ.get("ZDY_TOP") or DEFAULT_TOP
_ALLOWED_ROOTS: tuple[str, ...] | None = None

_USE_DEFAULT_TOP = object()          # top= 的默认哨兵：运行时读取 _DEFAULT_TOP

_FONT_CACHE: "OrderedDict[tuple, ImageFont.FreeTypeFont]" = OrderedDict()
_IMG_CACHE: "OrderedDict[tuple, Image.Image]" = OrderedDict()
_CACHE_LOCK = threading.Lock()
_FONT_CACHE_MAX = 24
_IMG_CACHE_MAX = 12


# --------------------------------------------------------------------------
# 全局配置
# --------------------------------------------------------------------------

def set_default_font(font) -> None:
    """设置全局默认字体。font 可为文件路径、zip 路径或 (zip 路径, 包内关键字)。"""
    global _DEFAULT_FONT
    _DEFAULT_FONT = font
    clear_cache(fonts=True)


def set_default_top(top) -> None:
    """设置全局默认顶部图（路径 / bytes / PIL.Image）。"""
    global _DEFAULT_TOP
    _DEFAULT_TOP = top
    clear_cache(images=True)


def set_allowed_image_roots(roots) -> None:
    """限制图片（顶部图、行内图）只能从这些目录下读取。None = 不限制。"""
    global _ALLOWED_ROOTS
    if roots is None:
        _ALLOWED_ROOTS = None
    else:
        _ALLOWED_ROOTS = tuple(os.path.realpath(r) for r in roots)


def clear_cache(fonts: bool = True, images: bool = True) -> None:
    """清空字体 / 图片缓存。"""
    with _CACHE_LOCK:
        if fonts:
            _FONT_CACHE.clear()
        if images:
            _IMG_CACHE.clear()


# --------------------------------------------------------------------------
# 字体
# --------------------------------------------------------------------------

def _extract_font_from_zip(zip_path: str, name: str | None = None) -> str:
    os.makedirs(_FONT_CACHE_DIR, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        entries = [
            e for e in zf.infolist()
            if not e.is_dir() and e.filename.lower().endswith(_FONT_EXT)
        ]
        if not entries:
            raise ValueError(f"压缩包里没有字体文件：{zip_path}")
        if name:
            hit = [e for e in entries if name.lower() in os.path.basename(e.filename).lower()]
            if not hit:
                raise ValueError(f"压缩包里找不到匹配 “{name}” 的字体：{zip_path}")
            entry = hit[0]
        else:
            entry = max(entries, key=lambda e: e.file_size)

        out = os.path.join(
            _FONT_CACHE_DIR,
            f"{os.path.splitext(os.path.basename(zip_path))[0]}__{os.path.basename(entry.filename)}",
        )
        if os.path.exists(out) and os.path.getsize(out) == entry.file_size:
            return out
        with zf.open(entry) as src, open(out, "wb") as dst:
            shutil.copyfileobj(src, dst)
    return out


def _resolve_font_path(font=None) -> str:
    spec = _DEFAULT_FONT if font is None else font
    inner = None
    if isinstance(spec, (tuple, list)):
        if not spec:
            raise ValueError("字体参数为空")
        spec, inner = spec[0], (spec[1] if len(spec) > 1 else None)
    if not spec:
        raise ValueError("未指定字体：请传 font= 或调用 set_default_font()")
    path = os.path.expanduser(str(spec))
    if path.lower().startswith("file://"):
        path = url2pathname(path[7:])
    if not os.path.exists(path):
        raise FileNotFoundError(f"字体文件不存在：{path}")
    if path.lower().endswith(".zip"):
        return _extract_font_from_zip(path, inner)
    return os.path.realpath(path)


def _get_font(path: str, size: int, index: int = 0) -> ImageFont.FreeTypeFont:
    key = (path, int(size), int(index))
    with _CACHE_LOCK:
        font = _FONT_CACHE.get(key)
        if font is not None:
            _FONT_CACHE.move_to_end(key)
            return font
    try:
        font = ImageFont.truetype(path, int(size), index=int(index))
    except OSError as exc:
        raise ValueError(f"字体加载失败（{path}, index={index}）：{exc}") from exc
    with _CACHE_LOCK:
        _FONT_CACHE[key] = font
        while len(_FONT_CACHE) > _FONT_CACHE_MAX:
            _FONT_CACHE.popitem(last=False)
    return font


# --------------------------------------------------------------------------
# 图片（顶部图 / 行内图共用）
# --------------------------------------------------------------------------

def _check_path(path: str) -> str:
    real = os.path.realpath(path)
    if _ALLOWED_ROOTS is not None:
        if not any(real == root or real.startswith(root + os.sep) for root in _ALLOWED_ROOTS):
            raise PermissionError(f"图片不在允许目录内：{real}")
    return real


def _load_image(path: str) -> Image.Image:
    stat = os.stat(path)
    key = (path, stat.st_mtime_ns, stat.st_size)
    with _CACHE_LOCK:
        img = _IMG_CACHE.get(key)
        if img is not None:
            _IMG_CACHE.move_to_end(key)
            return img
    with Image.open(path) as raw:
        raw.load()
        img = ImageOps.exif_transpose(raw).convert("RGBA")
    with _CACHE_LOCK:
        _IMG_CACHE[key] = img
        while len(_IMG_CACHE) > _IMG_CACHE_MAX:
            _IMG_CACHE.popitem(last=False)
    return img


def _load_source(src) -> Image.Image:
    """本地路径 / bytes / PIL.Image -> RGBA。动图取第一帧，不接受 URL。"""
    if isinstance(src, Image.Image):
        return src.convert("RGBA")
    if isinstance(src, (bytes, bytearray)):
        with Image.open(io.BytesIO(bytes(src))) as im:
            im.load()
            return im.convert("RGBA")
    path = os.path.expanduser(str(src))
    if path.lower().startswith("file://"):
        path = url2pathname(path[7:])
    if path.lower().startswith(("http://", "https://")):
        raise ValueError("本工具不联网，图片请用本地路径或 bytes")
    if not os.path.exists(path):
        raise FileNotFoundError(f"图片不存在：{path}")
    return _load_image(_check_path(path))


def _fit_image(img: Image.Image, target_h: int, max_w: float) -> Image.Image:
    """缩到目标高度；若比整行还宽，再按行宽压一次。"""
    target_h = max(1, int(target_h))
    w = max(1, int(round(img.width * target_h / max(1, img.height))))
    h = target_h
    if max_w and w > max_w:
        w = max(1, int(max_w))
        h = max(1, int(round(img.height * w / max(1, img.width))))
    if (w, h) == img.size:
        return img
    return img.resize((w, h), Image.Resampling.LANCZOS)


# --------------------------------------------------------------------------
# 内容段
# --------------------------------------------------------------------------

def _iter_segments(content, default_scale: float):
    """把 str / 段列表统一成 (类型, 载荷, 缩放) 序列。

    段列表里每一项可以是：
        "纯文本"
        ("text", "文本")
        ("img", 本地路径 | bytes | PIL.Image)
        ("img", 本地路径, 1.5)      # 该图高度 = 字号 × 1.5
    """
    if isinstance(content, str):
        yield "text", content, default_scale
        return
    if (isinstance(content, tuple) and len(content) > 1 and isinstance(content[0], str)
            and content[0].lower() in _SEG_KINDS):
        content = [content]          # 直接传单个段，例如 ("text", "你好")
    if not isinstance(content, (list, tuple)):
        raise TypeError("content 只能是字符串或段列表，例如 [('text','你好'),('img','a.png')]")
    for seg in content:
        if isinstance(seg, str):
            yield "text", seg, default_scale
            continue
        if not isinstance(seg, (list, tuple)) or not seg:
            raise ValueError(f"内容段格式不对：{seg!r}")
        kind = _SEG_KINDS.get(str(seg[0]).lower())
        if kind is None:
            raise ValueError(f"不认识的内容段类型：{seg[0]!r}（可用 text / img）")
        if kind == "text":
            if len(seg) < 2:
                raise ValueError(f"文字段缺少内容：{seg!r}")
            yield "text", str(seg[1]), default_scale
        else:
            if len(seg) < 2:
                raise ValueError(f"图片段缺少来源：{seg!r}")
            scale = float(seg[2]) if len(seg) > 2 and seg[2] is not None else default_scale
            yield "img", seg[1], scale


def _has_content(content, default_scale: float) -> bool:
    """是否含实际内容（非空白文字，或任意一张图）。"""
    for kind, payload, _scale in _iter_segments(content, default_scale):
        if kind == "img" or (isinstance(payload, str) and payload.strip()):
            return True
    return False


# --------------------------------------------------------------------------
# 排版（白区内的折行 / 内联图片 / 自适应字号）
# --------------------------------------------------------------------------

def _tokens_width(tokens, font: ImageFont.FreeTypeFont) -> float:
    """一行原子的宽度：连续文字合并后测量，图片按自身宽度累加。"""
    total = 0.0
    buf: list[str] = []
    for atom in tokens:
        if atom[0] == "t":
            buf.append(atom[1])
            continue
        if buf:
            total += font.getlength("".join(buf))
            buf.clear()
        if atom[0] == "i":
            total += atom[2]
    if buf:
        total += font.getlength("".join(buf))
    return total


def _line_box(tokens, font: ImageFont.FreeTypeFont, line_h: int):
    """返回 (行宽, 行高)。行高由字号行高与该行最高的图片共同决定。"""
    img_h = max((atom[3] for atom in tokens if atom[0] == "i"), default=0)
    return _tokens_width(tokens, font), max(line_h, img_h)


def _to_atoms(content, font: ImageFont.FreeTypeFont, image_scale: float,
              avail_w: float, line_sep: str):
    """把内容段摊平成原子序列：("t", 字) / ("i", 图, 宽, 高) / ("n",) 换行。"""
    atoms: list[tuple] = []
    for kind, payload, scale in _iter_segments(content, image_scale):
        if kind == "text":
            for ch in payload.replace(line_sep, "\n"):
                atoms.append(("n",) if ch == "\n" else ("t", ch))
        else:
            img = _fit_image(
                _load_source(payload),
                int(round(font.size * float(scale))),
                avail_w,
            )
            atoms.append(("i", img, img.width, img.height))
    return atoms


def _wrap_atoms(tokens, font: ImageFont.FreeTypeFont, avail_w: float):
    """按可用宽度折行，做中文禁则处理。图片是不可拆的原子。"""
    lines: list[list[tuple]] = []
    cur: list[tuple] = []
    for atom in tokens:
        if atom[0] == "n":
            lines.append(cur)
            cur = []
            continue
        if cur and _tokens_width(cur + [atom], font) > avail_w:
            if atom[0] == "t" and atom[1] in _NO_LINE_START and len(cur) > 1:
                # 收尾标点不能落行首：把前一个原子一起带到下一行
                lines.append(cur[:-1])
                cur = [cur[-1], atom]
                continue
            if cur[-1][0] == "t" and cur[-1][1] in _NO_LINE_END and len(cur) > 1:
                # 开引号/括号不能落行尾：留到下一行
                lines.append(cur[:-1])
                cur = [cur[-1], atom]
                continue
            cut = -1
            for i in range(len(cur) - 1, -1, -1):
                if cur[i][0] == "t" and cur[i][1] == " ":
                    cut = i
                    break
            if cut > 0:
                head = cur[:cut]
                while head and head[-1][0] == "t" and head[-1][1] == " ":
                    head.pop()
                lines.append(head)
                cur = cur[cut + 1:] + [atom]
            else:
                lines.append(cur)
                cur = [atom]
        else:
            cur.append(atom)
    lines.append(cur)
    return lines


def _layout(content, font_path, ttc_index, size, min_size, avail_w, avail_h,
            line_sep, line_spacing, strict, image_scale):
    """自适应字号：先按 size 排，放不下就逐步缩小；缩到 min_size 还放不下则截断。

    返回 (字体, 字号行高, 行列表, 是否截断)，行列表每项为 (原子列表, 行宽, 行高)。
    """
    cur = int(size)
    truncated = False
    while True:
        font = _get_font(font_path, cur, ttc_index)
        ascent, descent = font.getmetrics()
        line_h = max(1, int((ascent + descent) * line_spacing))
        lines = _wrap_atoms(
            _to_atoms(content, font, image_scale, avail_w, line_sep), font, avail_w
        )
        boxes = []
        for line in lines:
            line_w, box_h = _line_box(line, font, line_h)
            boxes.append((line, line_w, box_h))
        if sum(box[2] for box in boxes) <= avail_h or cur <= int(min_size):
            break
        cur = max(int(min_size), int(cur * 0.92))

    used = 0
    max_lines = 0
    for _tokens, _w, box_h in boxes:
        if used + box_h > avail_h:
            break
        used += box_h
        max_lines += 1
    max_lines = max(1, max_lines)

    if len(boxes) > max_lines:
        if strict:
            raise ValueError(
                f"内容放不下：字号缩到 {cur} 仍需 {len(boxes)} 行，上限 {max_lines} 行"
            )
        boxes = boxes[:max_lines]
        last = boxes[-1][0]
        if last and last[-1][0] == "t":
            last[-1] = ("t", "…")
        else:
            last.append(("t", "…"))
        truncated = True

    return font, line_h, boxes, truncated


# --------------------------------------------------------------------------
# 颜色
# --------------------------------------------------------------------------

def _parse_hex(value: str) -> tuple[int, int, int]:
    s = str(value).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise ValueError(f"颜色格式不认识：{value!r}（用 #RRGGBB 或 #RGB）")
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError as exc:
        raise ValueError(f"颜色格式不认识：{value!r}") from exc


def _norm_color(color):
    """统一字色写法：

    None / "black"  -> 纯黑（默认）
    True  / "multi" -> 每行随机彩色
    False / "single"-> 全篇一个随机彩色
    "#RRGGBB"       -> 指定颜色
    [色1, 色2]      -> 循环使用
    """
    if color is None or isinstance(color, bool):
        return color
    if isinstance(color, (list, tuple)):
        return color
    text = str(color).strip()
    low = text.lower()
    if not text or low in ("black", "#000", "#000000"):
        return None
    if low in ("multi", "color", "colour", "random", "rainbow", "true", "1", "yes"):
        return True
    if low in ("single", "mono", "false", "0", "no"):
        return False
    return text


def _line_colors(color, count: int, rng: random.Random):
    """返回 count 个 RGB 颜色。"""
    if color is None:
        return [INK] * count
    if isinstance(color, (list, tuple)) and color and not isinstance(color[0], int):
        return [_parse_hex(color[i % len(color)]) for i in range(count)]
    palette = [_parse_hex(c) for c in _INK_COLORS]
    if color is True:
        return [rng.choice(palette) for _ in range(count)]
    if color is False:
        chosen = rng.choice(palette)
        return [chosen] * count
    return [_parse_hex(color)] * count


# --------------------------------------------------------------------------
# 编码（体积控制）
# --------------------------------------------------------------------------

def _flatten(img: Image.Image) -> Image.Image:
    """有透明通道时铺白底，避免 JPEG/WEBP 把透明区压成黑色。"""
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        out = Image.new("RGB", rgba.size, BODY_COLOR)
        out.paste(rgba, mask=rgba.split()[-1])
        return out
    return img.convert("RGB")


def _dump(img: Image.Image, fmt: str, quality: int) -> bytes:
    buf = io.BytesIO()
    if fmt == "PNG":
        img.save(buf, "PNG", optimize=True)
    elif fmt == "WEBP":
        _flatten(img).save(buf, "WEBP", quality=int(quality), method=4)
    else:
        _flatten(img).save(
            buf, "JPEG", quality=int(quality), optimize=True, progressive=True
        )
    return buf.getvalue()


def _encode(img: Image.Image, fmt: str, quality: int, max_bytes: int) -> bytes:
    """编码并按需降质量、降分辨率以满足体积上限。

    若缩到极小仍超过 max_bytes（例如上限设得离谱），返回能缩到的最小结果。
    """
    fmt = fmt.upper()
    if fmt not in _FMT_EXT:
        raise ValueError(f"不支持的输出格式：{fmt}（可选 PNG / JPEG / WEBP）")
    data = _dump(img, fmt, quality)
    if not max_bytes or len(data) <= max_bytes:
        return data

    if fmt != "PNG":
        for q in (85, 78, 70, 62, 54, 46, 38):
            if q >= quality:
                continue
            data = _dump(img, fmt, q)
            if len(data) <= max_bytes:
                return data

    cur = img
    for _ in range(14):
        if cur.width <= 32 or cur.height <= 32:
            break
        cur = cur.resize(
            (max(32, int(cur.width * 0.8)), max(32, int(cur.height * 0.8))),
            Image.Resampling.LANCZOS,
        )
        data = _dump(cur, fmt, min(quality, 80))
        if len(data) <= max_bytes:
            break
    return data


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def render(
    content,
    *,
    top=_USE_DEFAULT_TOP,
    size: int = 85,
    color=None,
    font=None,
    ttc_index: int = 0,
    line_sep: str = LINE_SEP,
    seed=None,
    margin=None,
    align: str = "left",
    line_spacing: float = 1.15,
    image_scale: float = 1.2,
    format: str = "JPEG",
    quality: int = 90,
    max_bytes: int = 2_000_000,
    max_width: int = 1080,
    max_height: int = 3000,
    min_size: int = 16,
    strict: bool = False,
) -> bytes:
    """排版成图片，返回图片字节。

    content     字符串，或段列表：[("text","你好"), ("img","D:/face.png"), ("text","再见")]
    top         顶部图：默认用 DEFAULT_TOP；传路径/bytes/PIL.Image 可换；
                传 None 表示不要顶部图（此时画布宽度取 max_width）
    size        字号（像素）
    color       字色：默认纯黑；True/"multi"=每行彩色；False/"single"=全篇单色；
                "#RRGGBB"=指定色；[色1,色2]=循环
    font        字体文件或 zip，None 用默认字体
    seed        指定后随机取色可复现
    margin      文字边距，None = 按字号自动
    align       left / center / right
    image_scale 行内图片高度相对字号的比例（默认 1.2）
    format      PNG / JPEG / WEBP
    max_bytes   输出体积上限，超了自动降质量再降分辨率
    max_width   画布宽上限（顶部图超宽时等比缩到它，绝不放大）
    max_height  画布高上限（顶部图 + 白区），内容过多则自动缩字号
    """
    if not _has_content(content, image_scale):
        raise ValueError("content 不能为空")

    rng = random.Random(seed)
    font_path = _resolve_font_path(font)
    size = max(int(min_size), int(size))
    margin = int(margin) if margin else max(12, int(size * 0.6))
    color = _norm_color(color)

    # 顶部图：原色使用，只按 max_width 等比缩小，不变形、不调色
    top_img = None
    if top is _USE_DEFAULT_TOP:
        top = _DEFAULT_TOP
    if top is not None and top is not False and top != "":
        top_img = _load_source(top)
        cap_w = int(max_width)
        if top_img.width > cap_w:
            cap_h = max(1, int(round(top_img.height * cap_w / top_img.width)))
            top_img = top_img.resize((cap_w, cap_h), Image.Resampling.LANCZOS)

    width = top_img.width if top_img is not None else int(max_width)
    top_h = top_img.height if top_img is not None else 0

    avail_w = max(1.0, width - 2 * margin)
    body_max_h = max(int(min_size * 2), int(max_height) - top_h)
    avail_h = max(1.0, body_max_h - 2 * margin)

    font_obj, line_h, boxes, _ = _layout(
        content, font_path, ttc_index, size, min_size,
        avail_w, avail_h, line_sep, line_spacing, strict, image_scale,
    )

    block_h = sum(box_h for _t, _w, box_h in boxes)
    body_h = min(body_max_h, block_h + 2 * margin)          # 白区高度随内容延长
    canvas = Image.new("RGBA", (width, top_h + body_h), BODY_COLOR + (255,))
    if top_img is not None:
        canvas.alpha_composite(top_img, (0, 0))

    colors = _line_colors(color, max(1, len(boxes)), rng)

    draw = ImageDraw.Draw(canvas)
    y = top_h + margin
    for idx, (tokens, line_w, box_h) in enumerate(boxes):
        col = colors[min(idx, len(colors) - 1)]
        if align == "center":
            x = (width - line_w) / 2
        elif align == "right":
            x = width - margin - line_w
        else:
            x = margin
        text_y = y + (box_h - line_h) / 2
        buf: list[str] = []
        for atom in tokens:
            if atom[0] == "t":
                buf.append(atom[1])
                continue
            if buf:                       # 先吐掉图片前面累积的文字
                chunk = "".join(buf)
                draw.text((x, text_y), chunk, font=font_obj, fill=col)
                x += font_obj.getlength(chunk)
                buf.clear()
            image = atom[1]
            canvas.alpha_composite(image, (int(x), int(y + (box_h - image.height) / 2)))
            x += atom[2]
        if buf:
            draw.text((x, text_y), "".join(buf), font=font_obj, fill=col)
        y += box_h

    return _encode(canvas, format, quality, int(max_bytes))


def render_to_file(content, out_dir: str | None = None, **kwargs) -> str:
    """排版并把图片写到磁盘，返回文件绝对路径（适合直接交给机器人发送）。"""
    data = render(content, **kwargs)
    fmt = str(kwargs.get("format", "JPEG")).upper()
    ext = _FMT_EXT.get(fmt, ".jpg")
    folder = os.path.abspath(out_dir or _OUT_DIR)
    os.makedirs(folder, exist_ok=True)
    digest = hashlib.sha1(data).hexdigest()[:16]
    path = os.path.join(folder, f"t2i_{digest}{ext}")
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(data)
    return path


async def arender(content, **kwargs) -> bytes:
    """异步版 render（内部丢线程池，不阻塞事件循环）。"""
    return await asyncio.to_thread(functools.partial(render, content, **kwargs))


async def arender_to_file(content, out_dir: str | None = None, **kwargs) -> str:
    """异步版 render_to_file。"""
    return await asyncio.to_thread(
        functools.partial(render_to_file, content, out_dir, **kwargs)
    )


# --------------------------------------------------------------------------
# 命令行
# --------------------------------------------------------------------------

_IMG_MARK = re.compile(r"\{img:([^}]+)\}")


def _main() -> None:
    ap = argparse.ArgumentParser(description="本地图文排版（顶部图 + 白底）")
    ap.add_argument("--text", required=True,
                    help="要排版的文字，用 ↔ 强制换行；用 {img:图片路径} 插入行内图片")
    ap.add_argument("--out", default=None, help="输出文件路径（默认写到临时目录并打印路径）")
    ap.add_argument("--top", default=None, help="顶部图路径；传 none 表示不要顶部图")
    ap.add_argument("--font", default=None, help="字体文件或 zip")
    ap.add_argument("--size", type=int, default=85)
    ap.add_argument("--color", default="black", help="black / #RRGGBB / single / multi")
    ap.add_argument("--format", default="JPEG")
    ap.add_argument("--max-bytes", type=int, default=2_000_000)
    ap.add_argument("--max-width", type=int, default=1080)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--image-scale", type=float, default=1.2, help="行内图片高度相对字号的比例")
    args = ap.parse_args()

    # {img:路径} 拆成段列表
    pieces = _IMG_MARK.split(args.text)
    content: list = []
    for index, piece in enumerate(pieces):
        if index % 2:
            content.append(("img", piece.strip()))
        elif piece:
            content.append(piece)

    if args.top is None:
        top = _USE_DEFAULT_TOP
    elif args.top.strip().lower() in ("none", "off", "no", ""):
        top = None
    else:
        top = args.top

    kw = dict(
        top=top, size=args.size, color=args.color, font=args.font,
        format=args.format, max_bytes=args.max_bytes, max_width=args.max_width,
        seed=args.seed, image_scale=args.image_scale,
    )
    if args.out:
        data = render(content, **kw)
        with open(args.out, "wb") as fh:
            fh.write(data)
        print(f"{os.path.abspath(args.out)}  ({len(data)} bytes)")
    else:
        print(render_to_file(content, **kw))


if __name__ == "__main__":
    _main()
