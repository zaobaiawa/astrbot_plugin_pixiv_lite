"""把一页搜索结果 / 作者资料拼成**一张**长图。

新版 zdy_t2i 的版式是「顶部图（原色）+ 白底正文」，所以这里保持一致：
    * 最上方是顶部图（原样、只按 1080 等比缩小，不调色、不淡化）
    * 底下纯白背景，高度随内容自动延长
    * 宽度由顶部图决定（超 1080 缩到 1080），绝不横向变宽
    * 默认纯黑字

排版自己做（zdy_t2i.render 是按整篇内容排的，不适合「一页 N 条」的列表版式），
但字体、顶部图、行内图片处理都复用 zdy_t2i 的实现。全程本地，不联网。
"""

from __future__ import annotations

import io
import os

from . import cards
from .cards import SEP, _font, wrap  # noqa: F401

# 版式参数（宽度由顶部图决定，最多 1080）
WIDTH_MAX = 1080
MARGIN = 30
TITLE_SIZE = 32
LINE_SIZE = 28
THUMB_H = 230          # 缩略图高度，调大就是「预览图放大」
LINE_GAP = 10
BLOCK_GAP = 16
MAX_CANVAS_H = 3200

INK = (0, 0, 0)          # 纯黑字
BODY = (255, 255, 255)   # 纯白底


# --------------------------------------------------------------------------
# 顶部图与画布
# --------------------------------------------------------------------------

def top_image():
    """当前生效的顶部图（RGBA）与 (宽, 高)；没有就返回 (None, 1080, 0)。"""
    from PIL import Image

    spec = cards._ACTIVE_TOP[0]
    if not spec:
        return None, WIDTH_MAX, 0
    try:
        with Image.open(spec) as raw:
            raw.load()
            img = raw.convert("RGBA")
    except Exception:
        return None, WIDTH_MAX, 0
    if img.width > WIDTH_MAX:
        ratio = WIDTH_MAX / img.width
        img = img.resize((WIDTH_MAX, max(1, round(img.height * ratio))), Image.LANCZOS)
    return img, img.width, img.height


def _canvas(width: int, height: int):
    """建一张「顶部图 + 白底」的画布，返回 (canvas, 正文起始 y)。"""
    from PIL import Image

    top, _w, top_h = top_image()
    canvas = Image.new("RGB", (max(1, width), max(1, height)), BODY)
    if top is not None:
        canvas.paste(top, (0, 0), top)
    return canvas, top_h


# --------------------------------------------------------------------------
# 文本绘制
# --------------------------------------------------------------------------

def _fit(text: str, font, max_w: float) -> str:
    """截断到指定宽度。**不加省略号**（标题/作者用，不允许出现 …）。"""
    if font.getlength(text) <= max_w:
        return text
    while text and font.getlength(text) > max_w:
        text = text[:-1]
    return text


def _fit_lines(text: str, font, max_w: float, max_lines: int) -> list[str]:
    """折成最多 max_lines 行；每行都截断，**不加省略号**。

    返回行列表，长度可能小于 max_lines（文字短的时候）。
    """
    text = str(text or "").replace("\n", " ").strip()
    if not text:
        return []
    lines: list[str] = []
    cur = ""
    for ch in text:
        if font.getlength(cur + ch) > max_w and cur:
            lines.append(cur)
            cur = ch
            if len(lines) >= max_lines:
                cur = ""
                break
        else:
            cur += ch
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines


def _draw(draw, xy, text, font, max_w: float):
    if text:
        draw.text(xy, _fit(str(text), font, max_w), font=font, fill=INK)


def _open_thumb(thumb, height: int = THUMB_H):
    """把缩略图统一成 RGBA 且高度 = height。"""
    from PIL import Image

    if hasattr(thumb, "mode"):
        img = thumb
    elif isinstance(thumb, (bytes, bytearray)):
        img = Image.open(io.BytesIO(thumb))
    else:
        with Image.open(str(thumb)) as raw:
            img = raw.copy()
    if getattr(img, "is_animated", False):
        img.seek(0)
    img = img.convert("RGBA")
    ratio = height / img.height
    size = (max(1, int(img.width * ratio)), height)
    return img.resize(size, Image.LANCZOS)


def _rows_for(item: dict) -> list[tuple[str, int]]:
    """列表里每条作品的行：(文本, 最多几行)。

    标签行给 0 = 不限行数，多行也全部显示（用户要求标签不能被截断）。
    """
    kind = "novel" if "text_len" in item else "illust"
    return [
        (f"作者：{cards.strip_html(str(item.get('author') or '?'))}"
         f"（UID {item.get('uid')}）", 1),
        ((f"页数 {item.get('pages') or 1}"
          + ("   动图" if item.get("ugoira") else "")
          + ("   R18" if item.get("x_restrict") else "")) if kind == "illust"
         else (f"字数 {item.get('text_len') or 0}"
               + ("   R18" if item.get("x_restrict") else "")), 1),
        (cards.stat_text(item, with_label=False), 1),
        ("标签：" + cards.tags_text(item.get("tags") or [], 0), 0),
    ]


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------

def render_sections(title: str, subtitle: str, sections: list[dict],
                    footer: list[str] | None = None,
                    max_bytes: int = 2_600_000) -> bytes:
    """通用长图渲染：顶部图 + 标题 + 若干「分区」，每区若干条目。

    sections: [{"label": "插画（最近 5 件）", "entries": [entry, ...]}, ...]
    entry:    {"item": 统一结构, "thumb": 本地路径|bytes|PIL.Image|None,
               "note": "...", "rows": ["自定义行", ...]（可选）}
    返回 JPEG 字节。
    """
    from PIL import Image, ImageDraw

    # 不要 `import zdy_t2i`：插件按文件路径加载的是 `_pixiv_lite_zdy_t2i`，
    # sys.modules 里没有裸名 `zdy_t2i`，裸 import 会 ModuleNotFoundError。
    # 统一用 cards.zdy_t2i（cards 已经把它加载好了）。
    zdy_t2i = cards.zdy_t2i

    title_font = _font(TITLE_SIZE) or zdy_t2i._get_font(
        zdy_t2i._resolve_font_path(None), TITLE_SIZE)  # noqa: SLF001
    line_font = _font(LINE_SIZE) or title_font

    _top, width, top_h = top_image()
    content_w = width - 2 * MARGIN
    line_h = max(TITLE_SIZE, LINE_SIZE) + LINE_GAP
    thumb_h = THUMB_H

    # 预排版缩略图
    for section in sections:
        for entry in section.get("entries") or []:
            entry["_thumb_img"] = None
            if entry.get("thumb"):
                try:
                    entry["_thumb_img"] = _open_thumb(entry["thumb"], thumb_h)
                except Exception:
                    entry["_thumb_img"] = None

    def _rows_of(entry: dict, text_w: float) -> list[tuple[str, int]]:
        """返回 [(文本, 行数), ...]，行数按实际折行算；0 表示不限行。"""
        raw = list(entry.get("rows") or _rows_for(entry.get("item") or {}))
        if entry.get("note"):
            raw.append(f"（{entry['note']}）")
        out = []
        for row in raw:
            text, max_lines = row if isinstance(row, (list, tuple)) else (row, 1)
            lines = _fit_lines(str(text), line_font, text_w, max_lines or 999)
            out.append(("".join(lines), max(1, len(lines))))
        return out

    def _head_of(entry: dict, index: int) -> str:
        item = entry.get("item") or {}
        return (f"{index}. 【{item.get('id')}】"
                f"{cards.strip_html(str(item.get('title') or '无标题'))}")

    # 先量高度：放不下的条目直接不进图（避免为了硬塞而把底部的翻页/主页链接裁掉）
    fixed = top_h + MARGIN + (line_h + 6)
    fixed += sum(line_h for line in (subtitle or "").split("\n") if line)
    fixed += BLOCK_GAP + 6 + MARGIN
    budget = MAX_CANVAS_H - fixed - 2 * line_h   # 给 footer 多留两行余量

    def _entry_h(index: int, entry: dict) -> int:
        img = entry.get("_thumb_img")
        text_left = MARGIN + (img.width + 16 if img is not None else 0)
        text_w = width - MARGIN - text_left
        head_lines = max(1, len(_fit_lines(_head_of(entry, index), title_font,
                                           text_w, 2)))
        row_lines = sum(lines for _t, lines in _rows_of(entry, text_w))
        text_h = (head_lines + row_lines) * line_h + 4
        return max(thumb_h, text_h) + BLOCK_GAP

    dropped = 0
    trimmed: list[dict] = []
    for section in sections:
        keep: list[dict] = []
        for index, entry in enumerate(section.get("entries") or [], 1):
            cost = _entry_h(index, entry)
            label_h = line_h + 8 if section.get("label") else 0
            if budget - cost - label_h < 0:
                dropped += sum(1 for _ in (section.get("entries") or [])) - index + 1
                break
            budget -= cost
            keep.append(entry)
        trimmed.append({**section, "entries": keep,
                        "label": section.get("label")})
        budget -= line_h + 8 if section.get("label") else 0
        if dropped:
            break
    sections = trimmed

    measured = fixed + len(footer or []) * line_h
    for section in sections:
        if section.get("label"):
            measured += line_h + 8
        entries = section.get("entries") or []
        if not entries:
            measured += line_h + BLOCK_GAP
            continue
        for index, entry in enumerate(entries, 1):
            measured += _entry_h(index, entry)
    height = int(min(max(measured, top_h + 240), MAX_CANVAS_H))

    tail = list(footer or [])
    if dropped:
        tail.insert(0, f"（图太长，还有 {dropped} 件未显示）")

    canvas, top_h = _canvas(width, height)
    draw = ImageDraw.Draw(canvas)

    y = top_h + MARGIN
    _draw(draw, (MARGIN, y), title, title_font, content_w)
    y += line_h + 6
    for line in (subtitle or "").split("\n"):
        if line:
            _draw(draw, (MARGIN, y), line, line_font, content_w)
            y += line_h
    y += BLOCK_GAP

    for section in sections:
        label = section.get("label") or ""
        entries = section.get("entries") or []
        if label:
            draw.line([(MARGIN, y - 6), (width - MARGIN, y - 6)], fill=INK, width=1)
            _draw(draw, (MARGIN, y), label, title_font, content_w)
            y += line_h + 8
        if not entries:
            _draw(draw, (MARGIN, y), section.get("empty") or "（没有作品）",
                  line_font, content_w)
            y += line_h + BLOCK_GAP
            continue
        for index, entry in enumerate(entries, 1):
            item = entry.get("item") or {}
            img = entry.get("_thumb_img")
            text_left = MARGIN + (img.width + 16 if img is not None else 0)
            text_w = width - MARGIN - text_left
            # 标题最多 2 行；正文行数按实际折行算（标签行不限行数，全部显示）
            ty = y
            head = _fit_lines(_head_of(entry, index), title_font, text_w, 2)
            if not head:
                head = ["无标题"]
            for line in head:
                draw.text((text_left, ty), line, font=title_font, fill=INK)
                ty += line_h
            ty += 4
            for row_text, row_lines in _rows_of(entry, text_w):
                _draw(draw, (text_left, ty), row_text, line_font, text_w)
                ty += line_h * row_lines
            if img is not None:
                canvas.paste(img, (MARGIN, y), img)
            sep_y = max(ty, y + (img.height if img is not None else 0)) + BLOCK_GAP // 2
            draw.line([(MARGIN, sep_y), (width - MARGIN, sep_y)], fill=INK, width=1)
            y = sep_y + BLOCK_GAP // 2

    fy = y + 6
    for hint in tail:
        _draw(draw, (MARGIN, fy), hint, line_font, content_w)
        fy += line_h

    return _encode_jpeg(canvas, max_bytes)


def render_search_long(kind: str, word: str, label: str, page: int,
                       total_pages: int, has_more: bool,
                       entries: list[dict], nav: str | None = None,
                       max_bytes: int = 2_600_000) -> bytes:
    """把一页搜索结果画成一张长图（顶部图 + 白底），返回 JPEG 字节。

    nav 是「翻页指令里跟在标签后面的东西」（比如 `原神 180`），
    让下一页的提示带上时间窗；不给就用 word。
    """
    kind_label = "插画" if kind == "illust" else "小说"
    subtitle = (f"第 {page} 页 / 共 {total_pages} 页 · 本页 {len(entries)} 条 · "
                f"排序：{label}")
    cmd = "/px s" if kind == "illust" else "/novel s"
    args = nav or word
    footer = [f"—— 第 {page} 页 / 共 {total_pages} 页 ——"]
    if page > 1:
        footer.append(f"上一页：{cmd} {args} {page - 1}")
    if has_more:
        footer.append(f"下一页：{cmd} {args} {page + 1}")
    elif page == 1:
        footer.append("没有更多了")
    return render_sections(f"Pixiv {kind_label}搜索：{word}", subtitle,
                           [{"label": "", "entries": entries}], footer,
                           max_bytes=max_bytes)


# --------------------------------------------------------------------------
# 网格版式：横排作品格子（模仿 pixiv 用户页）
# --------------------------------------------------------------------------

GRID_GAP = 14
GRID_TITLE_SIZE = 22
GRID_PID_SIZE = 19
GRID_THUMB_H = 300
GRID_AVATAR = 92
GRID_LINE_GAP = 4


def _open_any(thumb):
    """bytes / 路径 / PIL.Image -> RGBA。"""
    from PIL import Image

    if hasattr(thumb, "mode"):
        return thumb.convert("RGBA")
    if isinstance(thumb, (bytes, bytearray)):
        with Image.open(io.BytesIO(bytes(thumb))) as im:
            im.load()
            return im.convert("RGBA")
    with Image.open(str(thumb)) as im:
        im.load()
        return im.convert("RGBA")


def _fit_box(img, box_w: float, box_h: float):
    """等比缩放到能塞进 box_w×box_h（不放大到超出，也不裁切）。"""
    from PIL import Image

    box_w = max(1, int(box_w))
    box_h = max(1, int(box_h))
    ratio = min(box_w / img.width, box_h / img.height)
    size = (max(1, int(round(img.width * ratio))),
            max(1, int(round(img.height * ratio))))
    return img.resize(size, Image.LANCZOS)


def _round_avatar(thumb, size: int = GRID_AVATAR):
    """作者头像裁成圆形。"""
    from PIL import Image, ImageDraw

    img = _open_any(thumb)
    side = max(1, min(int(size), min(img.width, img.height)))
    ratio = side / min(img.width, img.height)
    img = img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                     Image.LANCZOS)
    side = min(img.width, img.height)
    img = img.crop((0, 0, side, side))
    mask = Image.new("L", (side, side), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, side - 1, side - 1], fill=255)
    out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def render_grid(title: str, subtitle: str, sections: list[dict],
                footer: list[str] | None = None, per_row: int = 4,
                thumb_h: int = GRID_THUMB_H,
                max_bytes: int = 2_600_000) -> bytes:
    """横排网格长图（顶部图 + 白底 + 一行行作品格子）。

    sections: [{"avatar": bytes|路径|Image|None,
                "lines":  ["作者名", "UID 123", "插画 100 / 小说 3"],
                "items":  [{"thumb": ..., "title": "标题", "pid": 123}, ...]}]

    每个 section 先画「头像 + 资料行」，再把 items 排成 per_row 列的网格；
    格子里只放封面预览图、作品名和 PID。
    """
    from PIL import ImageDraw

    title_font = _font(TITLE_SIZE)
    line_font = _font(LINE_SIZE) or title_font
    gtitle_font = _font(GRID_TITLE_SIZE) or line_font
    pid_font = _font(GRID_PID_SIZE) or gtitle_font
    if title_font is None:                      # 字体起不来就交给 zdy_t2i 兜底
        zdy = cards.zdy_t2i
        title_font = zdy._get_font(zdy._resolve_font_path(None), TITLE_SIZE)  # noqa: SLF001
        line_font = line_font or title_font
        gtitle_font = gtitle_font or _font(GRID_TITLE_SIZE) or line_font
        pid_font = pid_font or _font(GRID_PID_SIZE) or gtitle_font

    _top, width, top_h = top_image()
    content_w = width - 2 * MARGIN
    per_row = max(1, min(6, int(per_row)))
    cell_w = (content_w - (per_row - 1) * GRID_GAP) / per_row

    head_line = max(TITLE_SIZE, LINE_SIZE) + LINE_GAP
    g_line = GRID_TITLE_SIZE + GRID_LINE_GAP
    pid_line = GRID_PID_SIZE + GRID_LINE_GAP
    text_h = 6 + g_line * 2 + pid_line          # 缩略图下面固定这么多文字高度

    # ---- 先量高度，太长就把缩略图按比例压扁，保证一页装得下 ----
    rows_total = 0
    for sec in sections:
        items = sec.get("items") or []
        rows_total += max(1, (len(items) + per_row - 1) // per_row)

    def _head_h(sec: dict) -> int:
        """资料区高度：头像和文字行取高的那个。"""
        lines = sec.get("lines") or []
        h = len(lines) * head_line
        if sec.get("avatar"):
            h = max(h, GRID_AVATAR)
        return h + 8 if (lines or sec.get("avatar")) else 0

    fixed = top_h + MARGIN + head_line + 6
    fixed += sum(head_line for line in (subtitle or "").split("\n") if line)
    fixed += BLOCK_GAP + 6 + MARGIN
    fixed += sum(_head_h(sec) for sec in sections)
    fixed += BLOCK_GAP * len(sections)
    fixed += len(footer or []) * head_line

    avail = MAX_CANVAS_H - fixed
    if rows_total:
        room = (avail - rows_total * (text_h + GRID_GAP)) // rows_total
        thumb_h = int(max(120, min(thumb_h, room)))
    item_h = thumb_h + text_h + GRID_GAP
    height = int(min(max(fixed + rows_total * item_h, top_h + 240), MAX_CANVAS_H))

    canvas, top_h = _canvas(width, height)
    draw = ImageDraw.Draw(canvas)

    y = top_h + MARGIN
    _draw(draw, (MARGIN, y), title, title_font, content_w)
    y += head_line + 6
    for line in (subtitle or "").split("\n"):
        if line:
            _draw(draw, (MARGIN, y), line, line_font, content_w)
            y += head_line
    y += BLOCK_GAP

    for sec in sections:
        avatar = sec.get("avatar")
        lines = sec.get("lines") or []
        if avatar or lines:
            text_left = MARGIN
            block_h = len(lines) * head_line
            if avatar:
                try:
                    av = _round_avatar(avatar, GRID_AVATAR)
                    canvas.paste(av, (MARGIN, y), av)
                    text_left = MARGIN + av.width + 14
                    block_h = max(block_h, av.height)
                except Exception:
                    pass
            ty = y
            for index, line in enumerate(lines):
                font = title_font if index == 0 else line_font
                _draw(draw, (text_left, ty), line, font, width - MARGIN - text_left)
                ty += head_line
            y += block_h + 8

        items = sec.get("items") or []
        for index, item in enumerate(items):
            col = index % per_row
            row = index // per_row
            x = MARGIN + col * (cell_w + GRID_GAP)
            cy = y + row * item_h
            thumb = item.get("thumb")
            if thumb:
                try:
                    img = _fit_box(_open_any(thumb), cell_w, thumb_h)
                    canvas.paste(img, (int(x + (cell_w - img.width) / 2),
                                       int(cy + (thumb_h - img.height) / 2)), img)
                except Exception:
                    pass
            ty = cy + thumb_h + 6
            for line in _fit_lines(str(item.get("title") or "无标题"),
                                   gtitle_font, cell_w, 2):
                draw.text((x, ty), line, font=gtitle_font, fill=INK)
                ty += g_line
            draw.text((x, ty), f"P {item.get('pid')}", font=pid_font, fill=INK)
        rows = max(1, (len(items) + per_row - 1) // per_row)
        y += rows * item_h + BLOCK_GAP

    for hint in (footer or []):
        _draw(draw, (MARGIN, y), hint, line_font, content_w)
        y += head_line

    return _encode_jpeg(canvas, max_bytes)


def _encode_jpeg(canvas, max_bytes: int) -> bytes:
    """压到体积上限内：先降质量，再等比例缩尺寸。"""
    from PIL import Image

    quality = 88
    while True:
        buf = io.BytesIO()
        canvas.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        if len(data) <= max_bytes or quality <= 56:
            break
        quality -= 6
    scale = 0.88
    while len(data) > max_bytes and canvas.width * scale > 560:
        size = (max(1, int(canvas.width * scale)), max(1, int(canvas.height * scale)))
        buf = io.BytesIO()
        canvas.convert("RGB").resize(size, Image.LANCZOS).save(
            buf, format="JPEG", quality=quality, optimize=True)
        data = buf.getvalue()
        scale *= 0.88
    return data
