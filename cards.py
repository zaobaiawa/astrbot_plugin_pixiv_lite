"""用 zdy_t2i 生成 Pixiv 卡片。

新版 zdy_t2i 的版式是「顶部图（原色）+ 白底正文」：
    * 宽度由顶部图决定（不会超过 max_width，也绝不放大）
    * 白底高度随内容自动延长
    * 默认纯黑字

本插件把字体和顶图都放在 resources/ 里，启动时设成 zdy_t2i 的默认值。

卡片函数吃 pixiv_api.normalize_illust / normalize_novel 产出的统一结构：
    {id, title, author, uid, tags, likes, bookmarks, views, comments,
     pages / text_len, x_restrict, create, ...}
"""

from __future__ import annotations

import importlib.util
import os
import sys

# zdy_t2i.py 就跟本插件同目录。
#
# 不能直接 `import zdy_t2i`：AstrBot 环境里可能已经有别的同名模块被加载进
# sys.modules（那时 sys.path 插入是没用的，`import` 会命中缓存那份，
# 于是出现「module 'zdy_t2i' has no attribute ...」）。
# 所以这里按**文件路径**显式加载插件目录里的这一份，确保用的是自带的。
_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_zdy_t2i():
    path = os.path.join(_HERE, "zdy_t2i.py")
    spec = importlib.util.spec_from_file_location("_pixiv_lite_zdy_t2i", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"加载内置 zdy_t2i 失败：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_pixiv_lite_zdy_t2i"] = module
    spec.loader.exec_module(module)
    return module


zdy_t2i = _load_zdy_t2i()

# zdy_t2i 的强制换行符
SEP = zdy_t2i.LINE_SEP

# 插件自带的字体与顶部图
FONT_DIR = os.path.join(_HERE, "resources")
BUNDLED_FONT = os.path.join(FONT_DIR, "MaokenZhuyuanTi.ttf")
BUNDLED_TOP = os.path.join(FONT_DIR, "top.png")

# zdy_t2i 自带的默认值（环境变量没设时用它写死的那个），用于「恢复默认」
_BUILTIN_FONT = getattr(zdy_t2i, "DEFAULT_FONT", None)
_BUILTIN_TOP = getattr(zdy_t2i, "DEFAULT_TOP", None)

# 卡片默认排版参数
CARD_SIZE = 40
CARD_MAX_BYTES = 1_500_000

# 字色：直接交给 zdy_t2i 的 color 参数。配置项 card_color 支持三种写法，
# 中文选项（WebUI 里显示的那些）会在这里映射成 zdy_t2i 认的值：
#   彩色（每行不同色） -> "multi"
#   纯黑               -> "black"
#   单色（全篇一个颜色）-> "single"
# 也可以直接填 "#RRGGBB" 指定颜色。
CARD_COLOR_DEFAULT = "multi"

_COLOR_ALIASES = {
    "multi": "multi", "color": "multi", "colour": "multi", "彩色": "multi",
    "彩色（每行不同色）": "multi", "每行不同色": "multi",
    "black": "black", "纯黑": "black", "黑": "black", "黑色": "black",
    "single": "single", "单色": "single", "单色（全篇一个颜色）": "single",
}


def normalize_color(value):
    """把配置里的 Card 色写法转成 zdy_t2i 认的 color 值。"""
    if value is None or isinstance(value, bool):
        return CARD_COLOR_DEFAULT if value is None else value
    text = str(value).strip()
    if not text:
        return CARD_COLOR_DEFAULT
    return _COLOR_ALIASES.get(text.lower(), text)

# 当前生效的字体/顶图（由 CardRenderer.configure 写入），用于缺字检测等
_ACTIVE_FONT: list = [""]
_ACTIVE_TOP: list = [""]


def _first_existing(*paths):
    for path in paths:
        if path and os.path.exists(str(path)):
            return str(path)
    return None


def _set_default(kind: str, value) -> None:
    """调用 zdy_t2i 的 set_default_font / set_default_top（没有这个函数就跳过）。

    这两个函数只是设默认值；真正渲染时插件会显式传 font= / top=，
    所以旧版 zdy_t2i 缺这两个函数也不影响出图。
    """
    func = getattr(zdy_t2i, f"set_default_{kind}", None)
    if callable(func):
        func(value)


def _default_font():
    """没配置字体时用哪个：优先插件自带字体，其次 zdy_t2i 默认字体。"""
    return _first_existing(BUNDLED_FONT, _BUILTIN_FONT) or _BUILTIN_FONT


def _default_top():
    """没配置顶部图时用哪个：优先插件自带顶图，其次 zdy_t2i 默认顶图。"""
    return _first_existing(BUNDLED_TOP, _BUILTIN_TOP)


class CardRenderer:
    """按插件配置渲染卡片；配置改了调用 configure() 即可。"""

    def __init__(self, cache_dir: str, top: str = "", font: str = "",
                 color=CARD_COLOR_DEFAULT):
        self.cache_dir = cache_dir
        self.top = (top or "").strip()
        self.font = (font or "").strip()
        self.color = normalize_color(color)
        self.top_effective = ""
        self.configure(cache_dir=cache_dir, top=top, font=font, color=color)

    def configure(self, cache_dir: str | None = None, top: str | None = None,
                  font: str | None = None, bg_dir: str | None = None,
                  color=None) -> list[str]:
        """应用配置，返回需要写进日志的提示信息。

        bg_dir 是旧配置名，兼容用：当 top 为空时拿它当顶部图。
        color 见 CARD_COLOR_DEFAULT：True=全文彩色 / "black"=纯黑 / "single"=单色。
        """
        notes: list[str] = []
        if cache_dir:
            self.cache_dir = cache_dir
        if top is not None:
            self.top = top.strip()
        if bg_dir is not None and not self.top:
            self.top = bg_dir.strip()
        if font is not None:
            self.font = font.strip()
        if color is not None:
            self.color = normalize_color(color)
        os.makedirs(self.cache_dir, exist_ok=True)

        # 顶部图不存在就当没配，退回自带顶图，并提示
        if self.top and not os.path.exists(self.top):
            notes.append(f"顶部图不存在，已改用自带顶图：{self.top}")
            self.top = ""

        # set_default_font / set_default_top 只是「设个默认值」，不是必需的：
        # 渲染时每个参数都会显式传（见 render()），所以这两个函数有没有都能跑。
        if self.font:
            try:
                _set_default("font", self.font)
            except Exception as exc:  # 字体路径写错不该让整个插件起不来
                notes.append(f"字体加载失败，回退自带字体：{exc}")
                self.font = ""
        if not self.font:
            self.font = _default_font() or ""
            _set_default("font", self.font)
        _ACTIVE_FONT[0] = self.font

        self.top_effective = self.top or _default_top() or ""
        _set_default("top", self.top_effective)
        _ACTIVE_TOP[0] = self.top_effective

        _FONT_CACHE.clear()  # 换字体后，折行用的字宽测量必须重建
        return notes

    async def render(self, content, **kwargs) -> str:
        """渲染成图片文件并返回绝对路径。

        字色由 zdy_t2i 的 color 参数决定（插件配置 card_color）：
            "multi"（默认）-> 全文每行彩色
            "black"        -> 纯黑
            "single"       -> 全篇一个随机色
            "#RRGGBB"      -> 指定色
        也可以显式覆盖 top / font / size / color 等参数（自测和调试用）。
        """
        params = {
            "size": CARD_SIZE,
            "color": self.color,
            "format": "JPEG",
            "quality": 90,
            "max_bytes": CARD_MAX_BYTES,
            "max_width": 1080,
            "max_height": 3000,
            "align": "left",
        }
        if self.font:
            params["font"] = self.font
        params["top"] = self.top_effective if self.top_effective else None
        params.update(kwargs)
        return await zdy_t2i.arender_to_file(content, out_dir=self.cache_dir, **params)


# --------------------------------------------------------------------------
# 字体 / 顶图
# --------------------------------------------------------------------------

def _measure_font(size: int = CARD_SIZE):
    """拿到 zdy_t2i 真正会用的字体对象，用于精确测量字宽。失败返回 None。"""
    try:
        path = zdy_t2i._resolve_font_path(None)  # noqa: SLF001
        return zdy_t2i._get_font(path, size)  # noqa: SLF001
    except Exception:
        return None


_FONT_CACHE: dict[int, object] = {}


def _font(size: int = CARD_SIZE):
    if size not in _FONT_CACHE:
        _FONT_CACHE[size] = _measure_font(size)
    return _FONT_CACHE[size]


def top_size() -> tuple[int, int]:
    """当前生效顶部图的 (宽, 高)；没有顶图返回 (1080, 0)。

    新版画布宽度 = 顶部图宽度（超 max_width 时按比例缩到 max_width）。
    """
    spec = _ACTIVE_TOP[0]
    if not spec:
        return 1080, 0
    try:
        with zdy_t2i.Image.open(spec) as img:
            width = int(img.width)
        cap = 1080
        if width > cap:
            return cap, 0
        return width, 0
    except Exception:
        return 1080, 0


# --------------------------------------------------------------------------
# 文本小工具
# --------------------------------------------------------------------------

def _char_w(ch: str) -> float:
    return 1.0 if ord(ch) > 0x2E80 else 0.72


def content_width() -> float:
    """正文可用宽度：画布宽 - 左右边距（边距 = 0.6×字号，与 zdy_t2i 一致）。"""
    width, _h = top_size()
    return max(80.0, width - 2 * CARD_SIZE * 0.6)


def fit_one_line(text: str, size: int = CARD_SIZE) -> str:
    """压成一行，超了就截断（不加省略号）。标签行/统计行用，避免被折得七零八落。"""
    text = str(text or "").replace("\n", " ").strip()
    if not text:
        return ""
    font = _font(size)
    budget = content_width()
    if font is None:
        return text
    if font.getlength(text) <= budget:
        return text
    while text and font.getlength(text) > budget:
        text = text[:-1]
    return text


def wrap(text: str, max_lines: int = 0, size: int = CARD_SIZE,
         ellipsis: bool = True) -> str:
    """按「实际字宽」折行，避免把行切在错误位置。

    画布宽度 = 顶部图宽度（超 1080 会缩到 1080），左右边距各 0.6×字号。
    优先用字体测量（精确），拿不到字体时退回估算。
    ellipsis=False 时超出行数直接截断，**不加省略号**（标题/简介用）。
    """
    text = (text or "").replace("\n", " ").strip()
    if not text:
        return ""
    font = _font(size)
    width, _h = top_size()
    budget = max(80, int(width - 2 * size * 0.6))

    def measure(chunk: str) -> float:
        if font is not None:
            try:
                return font.getlength(chunk)
            except Exception:
                pass
        return sum(_char_w(c) for c in chunk) * size

    lines: list[str] = []
    cur = ""
    cur_w = 0.0
    for ch in text:
        w = measure(ch)
        if cur and cur_w + w > budget:
            lines.append(cur)
            cur, cur_w = ch, w
        else:
            cur += ch
            cur_w += w
    if cur:
        lines.append(cur)
    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        if ellipsis:
            lines[-1] = lines[-1][: max(1, len(lines[-1]) - 1)] + "…"
    return SEP.join(lines)


def strip_html(text: str) -> str:
    """清掉网页残留：`<br />`、`<p>` 之类标签，以及 HTML 实体。

    Pixiv 小说的简介字段经常直接带 `<br />`，照原样画到图上很难看。
    """
    import html as _html
    import re as _re

    if not text:
        return ""
    cleaned = _re.sub(r"<\s*br\s*/?\s*>", " ", str(text), flags=_re.IGNORECASE)
    cleaned = _re.sub(r"<[^>]{1,80}>", "", cleaned)
    cleaned = _html.unescape(cleaned)
    cleaned = _re.sub(r"[ \t\u3000]{2,}", " ", cleaned)
    return cleaned.strip()


def tags_text(tags: list[str], limit: int = 0) -> str:
    """标签行。默认全部显示（limit=0 表示不限）。

    传了正数 limit 才截断，且**只有标签允许出现省略号**（`…(+N)`）。
    """
    names = [strip_html(str(t)) for t in tags if str(t).strip()]
    names = [n for n in names if n]
    if not names:
        return "无"
    if limit and len(names) > limit:
        names = names[:limit] + [f"…(+{len(names) - limit})"]
    return " ".join(f"#{n}" for n in names)


def stat_text(item: dict, with_label: bool = True) -> str:
    """统计行。Pixiv 现在只给收藏数/浏览数/评论数，「点赞」字段已废弃。"""
    bookmarks = item.get("bookmarks") or 0
    views = item.get("views") or 0
    comments = item.get("comments") or 0
    if with_label:
        text = f"★ 收藏 {bookmarks}   ◉ 浏览 {views}"
    else:
        text = f"★ {bookmarks}   ◉ {views}"
    if comments:
        text += f"   评论 {comments}"
    return text


# --------------------------------------------------------------------------
# 卡片构造：返回给 zdy_t2i 的段列表
# --------------------------------------------------------------------------
#
# 约定：不要预先折行——zdy_t2i 会按 avail_w 再折一次，两层叠加会出现难看的断行。
# 只有需要「限行数 + 不加省略号」的标题/简介才显式 wrap()（用 ↔ 分行，
# zdy_t2i 认得这个分隔符，不会再折）。
# 标签行**不截断**：标签多就让 zdy_t2i 折行显示完。统计行本来就只有一行。
# 字色统一交给 CardRenderer.color（默认全文彩色）。


def illust_info_card(item: dict, thumb: bytes | None) -> list:
    # 标题最多 5 行，超了直接截断，不加省略号
    title = wrap(strip_html(str(item.get("title"))), 5, ellipsis=False)
    segs: list = [("text", f"【插画】{title}")]
    if thumb:
        segs += [("text", SEP), ("img", thumb, 6.0), ("text", SEP)]
    else:
        segs.append(("text", SEP))
    segs += [
        ("text", f"作者：{strip_html(str(item.get('author')))}（UID {item.get('uid')}）"),
        ("text", SEP),
        ("text", f"作品ID：{item.get('id')}    页数：{item.get('pages')}"),
        ("text", SEP),
        ("text", f"标签：{tags_text(item.get('tags') or [])}"),
        ("text", SEP),
        ("text", fit_one_line(stat_text(item))),
        ("text", SEP),
        ("text", f"发布：{item.get('create')}"
                 + ("    [R18]" if item.get("x_restrict") else "")
                 + ("    [动图]" if item.get("ugoira") else "")),
    ]
    return segs


def novel_info_card(item: dict, thumb: bytes | None) -> list:
    title = wrap(strip_html(str(item.get("title"))), 5, ellipsis=False)
    segs: list = [("text", f"【小说】{title}")]
    if thumb:
        segs += [("text", SEP), ("img", thumb, 5.6), ("text", SEP)]
    else:
        segs.append(("text", SEP))
    segs += [
        ("text", f"作者：{strip_html(str(item.get('author')))}（UID {item.get('uid')}）"),
        ("text", SEP),
        ("text", f"小说ID：{item.get('id')}    字数：{item.get('text_len')}"),
        ("text", SEP),
        ("text", f"标签：{tags_text(item.get('tags') or [])}"),
        ("text", SEP),
        ("text", fit_one_line(stat_text(item))),
        ("text", SEP),
        ("text", f"发布：{item.get('create')}"
                 + ("    [R18]" if item.get("x_restrict") else "")),
    ]
    # 简介：去掉 <br /> 之类残留，最多 5 行，同样不加省略号
    caption = wrap(strip_html(item.get("caption") or ""), 5, ellipsis=False)
    if caption:
        segs += [("text", SEP), ("text", "简介：" + caption)]
    return segs


def search_header_card(kind: str, word: str, sorted_by: str, page: int,
                       total_pages: int, total: int) -> list:
    label = "插画" if kind == "illust" else "小说"
    return [
        ("text", f"Pixiv {label}搜索：{wrap(word, 2)}"),
        ("text", SEP),
        ("text", f"第 {page} 页 / 共 {total_pages} 页（本页 {total} 条）"),
        ("text", SEP),
        ("text", f"排序：{sorted_by}"),
    ]


def page_tail_card(kind: str, word: str, page: int, has_more: bool) -> list:
    """结果末尾的翻页提示卡。"""
    cmd = "/px s" if kind == "illust" else "/novel s"
    segs: list = [("text", f"—— 第 {page} 页 ——")]
    if page > 1:
        segs += [("text", SEP), ("text", f"上一页：{cmd} {word} {page - 1}")]
    if has_more:
        segs += [("text", SEP), ("text", f"下一页：{cmd} {word} {page + 1}")]
    elif page == 1:
        segs += [("text", SEP), ("text", "没有更多了")]
    return segs


def download_header_card(item: dict, count: int, extra: str = "") -> list:
    segs = [
        ("text", f"【下载】{wrap(strip_html(str(item.get('title'))), 4, ellipsis=False)}"),
        ("text", SEP),
        ("text", f"作者：{strip_html(str(item.get('author')))}（UID {item.get('uid')}）"),
        ("text", SEP),
        ("text", f"作品ID：{item.get('id')}    共 {count} 张"),
        ("text", SEP),
        ("text", f"标签：{tags_text(item.get('tags') or [], 0)}"),
    ]
    if extra:
        segs += [("text", SEP), ("text", extra)]
    return segs


def error_card(message: str, detail: str = "") -> list:
    segs = [("text", f"⚠ {wrap(strip_html(message), 4)}")]
    if detail:
        segs += [("text", SEP), ("text", wrap(strip_html(detail), 6))]
    return segs
