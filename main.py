"""astrbot_plugin_pixiv_lite —— 极简 Pixiv 插画 / 小说插件。

指令（wake_prefix 之后）：
    /px i <ID 或含 ID 的文本或标题>      查看插画信息（作者/标题/标签/点赞… 转成图片）
    /px d <同上> [张数]                 下载该插画原图，合并转发（默认最多 40 张）
    /px s [R18] [AI] <标签> [天数] [页码] 按标签搜索插画（时间窗内按收藏降序）
    /novel i <同上>                     查看小说信息（转成图片）
    /novel d <同上>                     下载小说正文，合并转发 txt
    /novel s <标签> [天数] [页码]        按标签搜索小说（转成图片）
    /auth <作者> [页码]                 作者作品网格
    /auth s <作者名> [页码]              按名字搜作者
    /px help                            用法说明

所有卡片都由本地 zdy_t2i.py 渲染，底图与字体全部来自本地。
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import time
from datetime import date, datetime, timedelta

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Node, Nodes, Plain
from astrbot.api.star import Context, Star, StarTools

from . import cards, long_card
from .novel_text import parse_novel_text, to_txt_bytes
from .pixiv_api import (
    MARK_SIZE,
    PixivClient,
    PixivError,
    check_image,
    guess_ext_from_url,
    illust_image_url,
    illust_page_count,
    image_format,
    is_ai_work,
    mark_image,
    normalize_illust,
    normalize_novel,
    normalize_user,
)

PLUGIN_NAME = "astrbot_plugin_pixiv_lite"
DOWNLOAD_HARD_MAX = 40       # /px d 一次最多下载几张（NapCat 合并转发最多 50 个节点）
DOWNLOAD_TIME_BUDGET = 180   # /px d 下载总时间预算（秒）
MAX_SCAN = 300               # 搜索时最多扫多少条原始结果（翻页靠它）
MAX_PAGE = 10                # 搜索结果最多翻到第几页
MAX_RECENT_DAYS = 1825       # 搜索时间窗上限（天），约 5 年
API_PAGE_SIZE = 30           # Pixiv 搜索单次最多返回 30 条
RECENT_DAYS = 365            # 「近期热门」的默认时间窗（天），指令里可覆盖

# /auth 相关
AUTHOR_SCAN = 30             # 判定「插画作者 / 小说作者」时看最近多少件
AUTHOR_PAGE_SIZE = 20        # /auth 每页展示几件作品
AUTHOR_PER_ROW = 4           # 网格每行几个
MAX_AUTHOR_PAGE = 20         # /auth 最多翻到第几页
AUTHOR_SEARCH_PER_PAGE = 4   # /auth s 每页几个作者
AUTHOR_SEARCH_WORKS = 4      # /auth s 每个作者展示几件作品


def _parse_date(value: str | None) -> date | None:
    """解析 Pixiv 的 create_date（形如 2024-06-01T10:00:00+09:00）。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None

# 真实字节嗅探出来的格式 -> 文件后缀
_EXT_BY_FORMAT = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp", "BMP": ".bmp"}


def _is_r18(item: dict, tags: list[str]) -> bool:
    if int(item.get("x_restrict") or 0) > 0:
        return True
    for tag in tags:
        low = str(tag).lower()
        if "r-18" in low or "r18" in low:
            return True
    return False


def _extract_id(text: str) -> int | None:
    """从一段文字里抠出作品 ID：优先 6~12 位数字，其次形如 id=123456 的片段。"""
    text = text.strip()
    if text.isdigit() and 4 <= len(text) <= 12:
        return int(text)
    m = re.search(r"\b(\d{5,12})\b", text)
    if m:
        return int(m.group(1))
    m = re.search(r"id[=:：]?\s*(\d{4,12})", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _split_tags(text: str) -> tuple[list[str], list[str]]:
    """把「初音ミク,可爱,-ntr」拆成 (正向标签, 排除标签)。"""
    include: list[str] = []
    exclude: list[str] = []
    for part in re.split(r"[,，\s]+", text.strip()):
        part = part.strip()
        if not part:
            continue
        if part.startswith("-") and len(part) > 1:
            exclude.append(part[1:])
        else:
            include.append(part)
    return include, exclude


def _split_page_n(text: str, max_page: int = MAX_PAGE) -> tuple[str, int]:
    """把「原神 2」拆成 ("原神", 2)；末尾不是小数字就当第 1 页。"""
    text = (text or "").strip()
    m = re.search(r"[\s,，]+(\d{1,2})$", text)
    if m:
        page = int(m.group(1))
        if page >= 1:
            return text[: m.start()].strip(), min(page, max_page)
    return text, 1


def _split_page(text: str) -> tuple[str, int]:
    return _split_page_n(text, MAX_PAGE)


def _split_window(text: str) -> tuple[str, int | None, int]:
    """拆出「搜索时间窗（天）」和「页码」，返回 (剩余文本, 天数|None, 页码)。

    末尾的数字按个数判断：

        <tag>               -> (<tag>, None, 1)     用配置里的默认天数
        <tag> 2             -> (<tag>, None, 2)     一个数且 ≤ MAX_PAGE 当页码
        <tag> 180           -> (<tag>, 180, 1)      一个数且 > MAX_PAGE 当天数
        <tag> 180 2         -> (<tag>, 180, 2)      两个数 = 天数 + 页码

    这样老写法 `<tag> 2` 还是第 2 页，不会突然变成「近 2 天」。
    """
    text = (text or "").strip()
    m = re.search(r"[\s,，]+(\d{1,4})(?:[\s,，]+(\d{1,2}))?$", text)
    if not m:
        return text, None, 1
    body = text[: m.start()].strip()
    first, second = int(m.group(1)), m.group(2)
    if second is not None:
        days = min(max(first, 1), MAX_RECENT_DAYS)
        return body, days, min(max(int(second), 1), MAX_PAGE)
    if first <= MAX_PAGE:
        return body, None, max(1, first)
    return body, min(max(first, 1), MAX_RECENT_DAYS), 1


def _split_count(text: str) -> tuple[str, int | None]:
    """拆出「下载张数」，返回 (剩余文本, 张数|None)。

    只有前面确实含作品 ID 时，末尾的数字才算张数，
    否则 `/px d 191823184` 里的 ID 会被当成张数。
    """
    text = (text or "").strip()
    m = re.search(r"[\s,，]+(\d{1,3})$", text)
    if not m:
        return text, None
    rest = text[: m.start()].strip()
    if not rest or _extract_id(rest) is None:
        return text, None
    return rest, max(1, min(int(m.group(1)), DOWNLOAD_HARD_MAX))


def _split_flags(text: str) -> tuple[str, bool, bool]:
    """摘出 R18 / AI 开关，返回 (清掉开关后的文本, 仅R18, 仅AI)。

    开关的含义是**「只要这一类」**：
    没写 R18 就是不含 R18，写了 R18 就是只留 R18；AI 同理。
    大小写不限、位置不限：
        /px s 原神            -> ("原神", False, False)
        /px s R18 原神        -> ("原神", True,  False)
        /px s AI 原神         -> ("原神", False, True)
        /px s R18 AI 原神     -> ("原神", True,  True)
        /px s R18 原神 2      -> ("原神 2", True, False)   页码由调用方再拆
    """
    only_r18 = only_ai = False
    pieces: list[str] = []
    for part in re.split(r"([,，\s]+)", (text or "").strip()):
        low = part.strip().lower()
        if low == "r18":
            only_r18 = True
            continue
        if low == "ai":
            only_ai = True
            continue
        pieces.append(part)
    # 用 "".join 拼回再归一化空格：删掉 R18/AI 后可能留下重复空格
    cleaned = re.sub(r"[,，\s]+", " ", "".join(pieces))
    return cleaned.strip(), only_r18, only_ai


class PixivLite(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.conf: dict = dict(config or {})
        self.data_dir = self._prepare_data_dir()
        self.cards = cards.CardRenderer(
            self.data_dir,
            top="",                       # 固定用插件自带的 resources/top.png
            font="",                      # 固定用插件自带的 resources/MaokenZhuyuanTi.ttf
            color=self.conf.get("card_color") or cards.CARD_COLOR_DEFAULT,
        )
        for note in self._apply_card_notes():
            logger.warning("[%s] %s", PLUGIN_NAME, note)
        self._clients: dict[tuple, PixivClient] = {}
        self._running: set[str] = set()
        self._last_cleanup = 0.0

    # ------------------------------------------------------------------ 初始化
    def _prepare_data_dir(self) -> str:
        """持久化目录固定为 data/plugin_data/<插件名>，绝不写进插件自身目录。"""
        try:
            base = StarTools.get_data_dir(PLUGIN_NAME)
        except Exception as exc:
            raise RuntimeError(
                f"拿不到插件数据目录（StarTools.get_data_dir('{PLUGIN_NAME}') 调用失败）。"
                f"请检查 AstrBot 的 data 目录配置是否正常：{exc}"
            ) from exc
        if not base:
            raise RuntimeError(
                f"插件数据目录为空（StarTools.get_data_dir('{PLUGIN_NAME}') 返回 None）。"
                "请检查 AstrBot 的 data 目录配置。"
            )
        path = os.path.join(str(base), "cache")
        os.makedirs(path, exist_ok=True)
        return path

    def _apply_card_notes(self) -> list[str]:
        try:
            return self.cards.configure() or []
        except Exception as exc:
            logger.error("[%s] 卡片渲染初始化失败：%s", PLUGIN_NAME, exc)
            return []

    # -------------------------------------------------------------------- 配置
    def _cfg(self, key: str, default=None):
        value = self.conf.get(key, default)
        return default if value is None else value

    def _client(self) -> PixivClient:
        token = str(self._cfg("refresh_token", "")).strip()
        if not token:
            raise PixivError("还没配置 refresh_token，请到 AstrBot WebUI → 插件配置里填写")
        proxy = str(self._cfg("proxy", "")).strip()
        timeout = int(self._cfg("timeout", 20) or 20)
        key = (token, proxy, timeout)
        client = self._clients.get(key)
        if client is None:
            for old_key, old in list(self._clients.items()):
                if old_key[0] != token:
                    asyncio.ensure_future(old.close())
                    self._clients.pop(old_key, None)
            client = PixivClient(token, proxy=proxy, timeout=timeout)
            self._clients[key] = client
        return client

    def _search_sort(self) -> str:
        return str(self._cfg("sort", "热度优先（失败自动降级）"))

    # -------------------------------------------------------------------- 工具
    async def _send_forward(self, event: AstrMessageEvent, nodes: list[Node]):
        """合并转发（OneBot v11 / NapCat）。"""
        if not nodes:
            return
        yield event.chain_result([Nodes(nodes=nodes)])

    def _bot_identity(self, event: AstrMessageEvent) -> tuple[str, str]:
        """合并转发节点的发送者应该是**机器人自己**，不是发指令的人。

        优先用机器人自身的 QQ 号；拿不到就退回 0（NapCat 会用机器人账号兜底）。
        """
        uin = ""
        getter = getattr(event, "get_self_id", None)
        if callable(getter):
            try:
                uin = str(getter() or "")
            except Exception:
                uin = ""
        if not uin:
            uin = str(getattr(getattr(event, "message_obj", None), "self_id", "") or "")
        return uin or "0", str(self._cfg("bot_name", "")).strip() or "Pixiv"

    def _node(self, event: AstrMessageEvent, content: list) -> Node:
        uin, name = self._bot_identity(event)
        return Node(content=content, name=name, uin=uin)

    async def _card(self, content, **kwargs) -> str:
        return await self.cards.render(content, **kwargs)

    async def _thumb(self, url: str) -> bytes | None:
        """下缩略图，重新编码成干净的 PNG 字节再交给 zdy_t2i。

        直接把原始字节交给 PIL 时，一旦文件被判定为「截断」就直接抛错；
        统一 re-encode 既避开这个坑，也顺带验证了图片真能解码。
        """
        if not url:
            return None
        try:
            data = await self._client().image_bytes(url, max_bytes=12 * 1024 * 1024)
            return await asyncio.to_thread(_clean_png, data)
        except Exception as exc:
            logger.debug("[%s] 缩略图不可用：%s", PLUGIN_NAME, exc)
            return None

    async def _cleanup(self) -> None:
        """超过 6 小时的缓存图片清理一次，避免长期跑爆盘。"""
        now = time.time()
        if now - self._last_cleanup < 1800:
            return
        self._last_cleanup = now
        limit = now - 6 * 3600

        def _work() -> int:
            removed = 0
            try:
                for name in os.listdir(self.data_dir):
                    full = os.path.join(self.data_dir, name)
                    if not os.path.isfile(full):
                        continue
                    try:
                        if os.path.getmtime(full) < limit:
                            os.remove(full)
                            removed += 1
                    except OSError:
                        continue
            except OSError:
                return removed
            return removed

        try:
            count = await asyncio.to_thread(_work)
            if count:
                logger.info("[%s] 清理了 %s 个过期缓存文件", PLUGIN_NAME, count)
        except Exception as exc:  # pragma: no cover
            logger.debug("[%s] 缓存清理失败：%s", PLUGIN_NAME, exc)

    # ---------------------------------------------------------------- 业务解析
    def _keep_raw(self, kind: str, raw_list: list[dict],
                  include: list[str], exclude: list[str],
                  only_r18: bool = False,
                  only_ai: bool = False) -> list[dict]:
        """按 R18 / AI 筛选与排除标签过掉一批作品。

        **小说不做 R18 / AI 过滤**（全放行），只认排除标签。
        插画的规则是「要么只要、要么不要」：

            only_r18=False, only_ai=False   不含 R18、不含 AI（默认）
            only_r18=True,  only_ai=False   仅 R18
            only_r18=False, only_ai=True    仅 AI
            only_r18=True,  only_ai=True    仅「既 R18 又 AI」

        没写 AI 开关时，是否排除 AI 由 `ai_mode` 配置决定
        （允许 AI = AI 作品也照发，但不强制只要 AI）。

        过滤只看 x_restrict、illust_ai_type 和 tags，
        避免为了过滤多打一次详情接口。
        """
        filter_kind = kind == "illust"
        # 没写 AI 开关 + ai_mode=允许 AI -> 不排除 AI
        ai_mode_off = (filter_kind and not only_ai
                       and str(self._cfg("ai_mode", "过滤 AI")).strip()
                       .startswith("允许"))

        def _tag_names(item) -> list[str]:
            out = []
            for tag in item.get("tags") or []:
                name = tag.get("name") if isinstance(tag, dict) else tag
                if name:
                    out.append(str(name))
            return out

        kept = []
        for item in raw_list:
            tags = _tag_names(item)
            if filter_kind:
                # 要 R18 就只留 R18；没要就排除 R18
                if only_r18 != _is_r18(item, tags):
                    continue
                if only_ai:
                    if not is_ai_work(item, tags):
                        continue
                elif not ai_mode_off and is_ai_work(item, tags):
                    continue
            if exclude and any(
                any(x.lower() in tag.lower() for tag in tags) for x in exclude
            ):
                continue
            kept.append(item)
        return kept

    async def _resolve_illust(self, text: str) -> dict:
        """数字 ID 直接查详情；否则当成标题关键词搜一搜。返回统一结构。"""
        client = self._client()
        illust_id = _extract_id(text)
        if illust_id:
            return normalize_illust(await client.illust_detail(illust_id))
        keyword = text.strip()
        if not keyword:
            raise PixivError("请给出作品 ID 或标题关键词")
        results = await client.search_illust(
            keyword, sort="date_desc", limit=10, search_target="partial_match_for_tags"
        )
        picked = self._keep_raw("illust", results, [keyword], [])
        if not picked:
            raise PixivError(f"没搜到和「{keyword}」相关的插画（或都被 R18 规则拦掉了）")
        return normalize_illust(await client.illust_detail(int(picked[0]["id"])))

    async def _resolve_novel(self, text: str) -> dict:
        """小说元数据。正文要另外取（见 _novel_content）。"""
        client = self._client()
        novel_id = _extract_id(text)
        if not novel_id:
            keyword = text.strip()
            if not keyword:
                raise PixivError("请给出小说 ID 或标题关键词")
            results = await client.search_novel(keyword, sort="date_desc", limit=10)
            picked = self._keep_raw("novel", results, [keyword], [])
            if not picked:
                raise PixivError(f"没搜到和「{keyword}」相关的小说（或都被 R18 规则拦掉了）")
            novel_id = int(picked[0]["id"])
        app = await client.novel_detail(int(novel_id))
        return normalize_novel(app)

    async def _novel_full(self, novel_id: int) -> tuple[dict, str]:
        """返回 (统一结构（含正文简介等信息）, 正文纯文本)。

        正文只能从网页接口 /ajax/novel/{id} 拿，app-api 的正文接口已经下线。
        """
        client = self._client()
        app = await client.novel_detail(int(novel_id))
        web: dict = {}
        try:
            web = await client.novel_web(int(novel_id))
        except PixivError as exc:
            logger.warning("[%s] 网页接口取小说详情失败：%s", PLUGIN_NAME, exc)
        item = normalize_novel(app, web)
        body = parse_novel_text(web.get("content") or web.get("novel_text") or "")
        return item, body

    def _page_size(self) -> int:
        return max(1, min(20, int(self._cfg("page_size", 10) or 10)))

    def _recent_days(self) -> int:
        """默认时间窗固定 365 天；想换天数在指令里写（如 `/px s 原神 180`）。"""
        return RECENT_DAYS

    async def _search(self, kind: str, word: str, page: int, size: int | None = None,
                      force_sort: str | None = None,
                      only_r18: bool = False,
                      only_ai: bool = False,
                      days: int | None = None) -> dict:
        """按标签搜索并返回某一页（条目已归一化）。

        「近期热门」= Pixiv 官方 popular_desc + `start_date`（最近 N 天），
        这是**时间窗内**按收藏降序的真实排行；窗口内还能用 offset 继续翻页取样本。
        非会员时 popular_desc 会 400，自动降级为「抓最新结果 → 本地按收藏重排」。

        force_sort="date_desc" 时走纯最新（供 /px new 用）。
        days 是指令里指定的时间窗，传 None 就用默认的 RECENT_DAYS（365）。
        only_r18 / only_ai 来自指令开关，含义是「只要这一类」；
        小说不认这两个参数（`_keep_raw` 里会忽略）。
        """
        client = self._client()
        include, exclude = _split_tags(word)
        if not include:
            raise PixivError("标签不能为空")
        query = " ".join(include)
        page = max(1, min(int(page), MAX_PAGE))
        size = size or self._page_size()
        need = page * size
        search = client.search_illust if kind == "illust" else client.search_novel

        def _keep(raw: list[dict]) -> list[dict]:
            return self._keep_raw(kind, raw, include, exclude, only_r18, only_ai)

        if force_sort == "date_desc" or (
            force_sort is None and self._search_sort().startswith("最新")
        ):
            kept = await self._scan(kind, query, include, exclude, need,
                                    keep=_keep)
            return self._slice(self._normalize(kind, kept), page, size, "按最新投稿")

        span = days if days else self._recent_days()
        start_date = (date.today() - timedelta(days=span)).strftime("%Y-%m-%d")
        try:
            kept = await self._scan(kind, query, include, exclude, need,
                                    sort="popular_desc", start_date=start_date,
                                    keep=_keep)
            label = f"近 {span} 天最热门（按收藏降序）"
        except PixivError as exc:
            logger.info("[%s] %s + start_date 不可用，降级本地重排：%s",
                        PLUGIN_NAME, kind, exc)
            kept = []
            label = f"近 {span} 天本地重排（本账号非会员）"

        # 官方的「popular_desc + start_date」会给一个**不完整**的子集：
        # 实测有窗口内作品被漏掉（比如 665 天窗口丢了 2025-06 的作品）。
        # 所以这一页没填满时，再用 date_desc 扫到窗口边界补齐，最后本地按收藏重排。
        if len(kept) < need:
            extra = await self._scan_by_window(kind, query, include, exclude,
                                               span, _keep)
            seen = {it.get("id") for it in kept}
            added = 0
            for item in extra:
                if item.get("id") not in seen:
                    kept.append(item)
                    seen.add(item.get("id"))
                    added += 1
            if added:
                logger.info("[%s] %s「%s」官方热门漏了 %s 条，已补全",
                            PLUGIN_NAME, kind, query, added)
                if "本地重排" not in label:
                    label = f"近 {span} 天最热门（按收藏降序，已补全 {added} 条）"

        kept.sort(key=self._popularity, reverse=True)
        return self._slice(self._normalize(kind, kept), page, size, label)

    async def _scan_by_window(self, kind: str, query: str,
                              include: list[str], exclude: list[str],
                              days: int, keep) -> list[dict]:
        """按发布时间倒序扫，直到越过时间窗起点（或扫到 MAX_SCAN）。

        用来补官方的 popular_desc + start_date 漏掉的作品。
        """
        client = self._client()
        search = client.search_illust if kind == "illust" else client.search_novel
        floor = date.today() - timedelta(days=days)
        items: list[dict] = []
        offset = 0
        while offset < MAX_SCAN:
            batch = await search(query, sort="date_desc", offset=offset,
                                 limit=API_PAGE_SIZE)
            if not batch:
                break
            items.extend(keep(batch))
            dates = [d for d in (_parse_date(b.get("create_date")) for b in batch) if d]
            if dates and min(dates) < floor:
                break                       # 已经翻到窗口之外，后面更老
            offset += API_PAGE_SIZE
        return [it for it in items
                if _parse_date(it.get("create_date")) is None
                or _parse_date(it.get("create_date")) >= floor]

    @staticmethod
    def _popularity(item: dict) -> tuple:
        return (int(item.get("total_bookmarks") or item.get("bookmark_count") or 0),
                int(item.get("total_view") or item.get("view_count") or 0))

    def _normalize(self, kind: str, raw_list: list[dict]) -> list[dict]:
        if kind == "illust":
            return [normalize_illust(i) for i in raw_list]
        return [normalize_novel(i) for i in raw_list]

    async def _scan(self, kind: str, query: str, include: list[str], exclude: list[str],
                    need: int, sort: str = "date_desc",
                    start_date: str | None = None, keep=None) -> list[dict]:
        """抓多页搜索结果，凑够 need 条（过滤用 keep，默认按配置的 R18 规则）。"""
        client = self._client()
        search = client.search_illust if kind == "illust" else client.search_novel
        if keep is None:
            def keep(raw: list[dict]) -> list[dict]:
                return self._keep_raw(kind, raw, include, exclude)
        items: list[dict] = []
        offset = 0
        while offset < MAX_SCAN and len(items) < need:
            batch = await search(query, sort=sort, offset=offset,
                                 limit=API_PAGE_SIZE, start_date=start_date)
            if not batch:
                break
            items.extend(keep(batch))
            offset += API_PAGE_SIZE
        return items

    @staticmethod
    def _slice(items: list[dict], page: int, size: int, label: str) -> dict:
        start = (page - 1) * size
        chunk = items[start: start + size]
        return {
            "items": chunk,
            "page": page,
            "total": len(items),
            "total_pages": max(1, (len(items) + size - 1) // size),
            "has_more": len(items) > start + size,
            "label": label,
        }

    # ------------------------------------------------------------ 图片下载落盘
    async def _download_image(
        self, url: str, sem: asyncio.Semaphore, budget: "_Budget"
    ) -> tuple[str | None, str]:
        """下载一页原图，返回 (本地路径, 备注)。"""
        if not url:
            return None, "无图片地址"
        if budget.expired():
            return None, "已超时跳过"
        async with sem:
            try:
                return await asyncio.wait_for(
                    self._download_and_prepare(url), timeout=budget.remaining()
                )
            except asyncio.TimeoutError:
                return None, "下载超时跳过"
            except OSError as exc:
                return None, f"写文件失败（{exc}）"
            except Exception as exc:
                return None, f"{exc}"

    async def _download_and_prepare(self, url: str) -> tuple[str | None, str]:
        """下载原图并落盘。**不做压缩**，但会在右上角点一小块黑像素防屏蔽。

        后缀按真实字节嗅探决定：Pixiv 经常「地址 .jpg、内容 PNG」，
        而 medium 图的地址干脆没有后缀。后缀不对的文件 QQ 会当普通文件而不是图片。
        """
        data = await self._client().image_bytes(url)  # 内部已校验是图片
        fmt = check_image(data, url)

        # QQ 会按图片特征屏蔽某些图；右上角点几像素黑点就能改掉特征。
        # 这是唯一一处对原图的改动，不需要可在配置里关掉（mark_images）。
        if self._mark_on():
            marked = await asyncio.to_thread(mark_image, data, self._mark_px())
            if marked and image_format(marked):
                data = marked
                fmt = image_format(data) or fmt
            else:
                logger.debug("[%s] 防屏蔽标记失败，按原图发送", PLUGIN_NAME)

        base = os.path.splitext(os.path.basename(url.split("?")[0]))[0] or "image"
        ext = _EXT_BY_FORMAT.get(fmt) or guess_ext_from_url(url, data)
        path = os.path.join(self.data_dir, f"{int(time.time() * 1000)}_{base}{ext}")
        with open(path, "wb") as fh:
            fh.write(data)
        return path, ""

    def _mark_on(self) -> bool:
        value = self._cfg("mark_images", True)
        if isinstance(value, str):
            return value.strip().lower() not in ("0", "false", "no", "off", "关", "关闭")
        return bool(value)

    def _mark_px(self) -> int:
        """标记像素边长。注意 `int(x or 默认)` 会把 0 顶成默认，所以显式判空。"""
        value = self._cfg("mark_size", MARK_SIZE)
        if value is None or value == "":
            value = MARK_SIZE
        try:
            px = int(value)
        except (TypeError, ValueError):
            px = MARK_SIZE
        return max(1, min(8, px))

    # ------------------------------------------------------------------ 指令区
    @filter.command("px")
    async def px(self, event: AstrMessageEvent):
        """Pixiv 插画：i 查看 / d 下载 / s 搜索"""
        async for result in self._dispatch(event, "illust"):
            yield result

    @filter.command("novel")
    async def novel(self, event: AstrMessageEvent):
        """Pixiv 小说：i 查看 / d 下载 / s 搜索"""
        async for result in self._dispatch(event, "novel"):
            yield result

    @filter.command("auth", alias={"作者", "author"})
    async def auth(self, event: AstrMessageEvent):
        """Pixiv 作者：查资料 + 作品网格 / 按名字搜作者"""
        raw = event.message_str or ""
        arg = re.sub(r"^\s*/?(?:auth|author|作者)\b", "", raw, count=1,
                     flags=re.IGNORECASE).strip()
        arg = arg.strip('"').strip("“”").strip()


        if not arg:
            yield event.plain_result(
                "用法：\n"
                "/auth <作者名 或 作者ID> [页码]     看该作者的作品网格（每页 20 件）\n"
                "/auth s <作者名> [页码]           按名字搜作者（每页 4 位，每位 4 件作品）\n"
                "例如：/auth 案山子杏子    /auth 案山子杏子 2    /auth s カンザリン"
            )
            return

        sub, _, rest = arg.partition(" ")
        key = f"{event.get_sender_id()}:auth"
        if sub.strip().lower() in ("s", "search", "搜索", "找"):
            name, page = _split_page_n(rest.strip(), MAX_AUTHOR_PAGE)
            if not name:
                yield event.plain_result("用法：/auth s <作者名> [页码]")
                return
            agen = self._do_author_search(event, name, page)
        else:
            name, page = _split_page_n(arg, MAX_AUTHOR_PAGE)
            agen = self._do_author(event, name, page)
        try:
            async for r in self._guard(event, key, agen):
                yield r
        except PixivError as exc:
            yield event.plain_result(f"⚠ {exc}")
        except Exception as exc:  # pragma: no cover - 兜底
            logger.exception("[%s] /auth 处理失败", PLUGIN_NAME)
            yield event.plain_result(f"⚠ 出错了：{exc}")

    async def _resolve_user(self, text: str) -> tuple[dict, dict]:
        """按作者名或 ID 找到作者，返回 (user, profile)。"""
        client = self._client()
        text = text.strip()
        uid = int(text) if text.isdigit() and 3 <= len(text) <= 12 else None
        if uid is None:
            candidates = await client.search_user(text)
            if not candidates:
                raise PixivError(f"没搜到作者「{text}」")
            exact = [u for u in candidates if str(u.get("name") or "") == text]
            uid = int((exact or candidates)[0]["id"])
        data = await client.user_detail(uid)
        user = data.get("user") or {}
        if not user:
            raise PixivError(f"找不到作者 {uid}")
        return user, data.get("profile") or {}

    # ------------------------------------------------------------------ /auth
    async def _author_recent(self, uid: int, limit: int = AUTHOR_SCAN) -> list[tuple[str, dict]]:
        """把该作者的插画和小说合并，按发布时间倒序取前 limit 件。"""
        client = self._client()
        merged: list[tuple[str, dict]] = []
        for kind, fetch in (("illust", client.user_illusts), ("novel", client.user_novels)):
            try:
                raw_list, _next = await fetch(uid, limit=min(limit, API_PAGE_SIZE))
            except PixivError as exc:
                logger.info("[%s] 取作者 %s 的%s失败：%s", PLUGIN_NAME, uid, kind, exc)
                continue
            merged += [(kind, it) for it in raw_list]
        merged.sort(key=lambda pair: str(pair[1].get("create_date") or ""), reverse=True)
        return merged[:limit]

    async def _grid_items_from(self, entries: list[tuple[str, dict]]) -> list[dict]:
        """把 [(kind, 原始作品)] 变成网格格子：封面 + 标题 + PID。"""
        if not entries:
            return []
        sem = asyncio.Semaphore(int(self._cfg("download_concurrency", 3) or 3))

        async def _one(kind: str, raw: dict) -> dict:
            item = normalize_illust(raw) if kind == "illust" else normalize_novel(raw)
            if kind == "illust":
                url = illust_image_url(item.get("raw") or {}, 0, quality="medium")
            else:
                url = item.get("cover") or ""
            async with sem:
                thumb = await self._thumb(url)
            return {"thumb": thumb, "title": item.get("title"), "pid": item.get("id")}

        results = await asyncio.gather(*(_one(k, r) for k, r in entries),
                                       return_exceptions=True)
        out: list[dict] = []
        for res in results:
            if isinstance(res, BaseException):
                logger.debug("[%s] 网格格子失败：%s", PLUGIN_NAME, res)
                continue
            out.append(res)
        return out

    @staticmethod
    def _author_lines(info: dict) -> list[str]:
        """作者资料那几行（第一行是名字，后面是详情）。"""
        lines = [cards.strip_html(str(info.get("name") or "未知作者"))]
        meta = []
        if info.get("account"):
            meta.append(f"@{info['account']}")
        meta.append(f"UID {info.get('id')}")
        counts = [f"插画 {info.get('illusts') or 0}"]
        if info.get("manga"):
            counts.append(f"漫画 {info['manga']}")
        counts.append(f"小说 {info.get('novels') or 0}")
        meta.append("作品：" + " / ".join(counts))
        lines.append("　".join(meta))
        extra = [f"获赞 {info.get('bookmarks') or 0}", f"关注 {info.get('following') or 0}"]
        if info.get("region"):
            extra.append(str(info["region"]))
        lines.append("　".join(extra))
        return lines

    async def _do_author(self, event: AstrMessageEvent, arg: str, page: int = 1):
        """作者作品网格：自动判定插画作者 / 小说作者，每页 20 件，每行 4 件。"""
        await self._cleanup()
        client = self._client()
        user, profile = await self._resolve_user(arg)
        uid = int(user["id"])
        info = normalize_user(user, profile)

        # 看最近 30 件里插画多还是小说多，决定这位作者按哪类展示
        preview = await self._author_recent(uid, AUTHOR_SCAN)
        if not preview:
            raise PixivError(f"这位作者（UID {uid}）没有公开作品")
        n_illust = sum(1 for kind, _raw in preview if kind == "illust")
        n_novel = len(preview) - n_illust
        kind = "illust" if n_illust >= n_novel else "novel"
        kind_label = "插画" if kind == "illust" else "小说"

        page = max(1, min(int(page), MAX_AUTHOR_PAGE))
        offset = (page - 1) * AUTHOR_PAGE_SIZE
        if kind == "illust":
            works, _next = await client.user_illusts(uid, offset=offset,
                                                     limit=AUTHOR_PAGE_SIZE)
        else:
            works, _next = await client.user_novels(uid, offset=offset,
                                                    limit=AUTHOR_PAGE_SIZE)
        works = works[:AUTHOR_PAGE_SIZE]
        if not works:
            raise PixivError(f"第 {page} 页没有{kind_label}了")
        if offset and not _next and len(works) < AUTHOR_PAGE_SIZE:
            pass    # 最后一页，正常

        items = await self._grid_items_from([(kind, w) for w in works])
        if not items:
            raise PixivError("封面都取不到，稍后再试")
        avatar = await self._thumb(info.get("avatar") or "")

        total = info["illusts"] if kind == "illust" else info["novels"]
        subtitle = (f"第 {page} 页 · 本页 {len(items)} 件{kind_label}"
                    f"（该作者共 {total} 件{kind_label}，按{kind_label}展示）")
        footer = [f"主页：https://www.pixiv.net/users/{info['id']}"]
        if len(works) >= AUTHOR_PAGE_SIZE:
            footer.append(f"下一页：/auth {info.get('name')} {page + 1}")
        footer.append(f"按标签搜这些{kind_label}："
                      + ("/px s" if kind == "illust" else "/novel s")
                      + f" {info.get('name')}")

        data = await asyncio.to_thread(
            long_card.render_grid,
            f"Pixiv 作者：{cards.strip_html(str(info.get('name') or ''))}",
            subtitle,
            [{"avatar": avatar, "lines": self._author_lines(info), "items": items}],
            footer, AUTHOR_PER_ROW,
        )
        path = os.path.join(self.data_dir, f"author_{uid}_{int(time.time() * 1000)}.jpg")
        with open(path, "wb") as fh:
            fh.write(data)
        yield event.image_result(path)

    async def _do_author_search(self, event: AstrMessageEvent, name: str, page: int = 1):
        """按名字搜作者：每页 4 位，每位展示最近 4 件作品（插画小说混排）。"""
        await self._cleanup()
        client = self._client()
        users = await client.search_user(name)
        if not users:
            raise PixivError(f"没搜到作者「{name}」")
        per = AUTHOR_SEARCH_PER_PAGE
        total_pages = max(1, (len(users) + per - 1) // per)
        page = max(1, min(int(page), total_pages))
        chunk = users[(page - 1) * per: page * per]

        sections: list[dict] = []
        for user in chunk:
            uid = int(user["id"])
            try:
                data = await client.user_detail(uid)
            except PixivError:
                data = {}
            info = normalize_user(data.get("user") or user, data.get("profile") or {})
            preview = await self._author_recent(uid, AUTHOR_SEARCH_WORKS)
            items = await self._grid_items_from(preview[:AUTHOR_SEARCH_WORKS])
            avatar = await self._thumb(info.get("avatar") or "")
            sections.append({"avatar": avatar,
                             "lines": self._author_lines(info),
                             "items": items})

        if not sections:
            raise PixivError(f"「{name}」这页没有作者")
        subtitle = (f"第 {page} 页 / 共 {total_pages} 页 · 本页 {len(sections)} 位作者"
                    f"（每页 {per} 位，每位最近 {AUTHOR_SEARCH_WORKS} 件作品）")
        footer = [f"搜到 {len(users)} 位同名作者"]
        if page > 1:
            footer.append(f"上一页：/auth s {name} {page - 1}")
        if page < total_pages:
            footer.append(f"下一页：/auth s {name} {page + 1}")
        footer.append(f"看某位作者全部作品：/auth <作者名>")

        data = await asyncio.to_thread(
            long_card.render_grid,
            f"Pixiv 作者搜索：{cards.strip_html(name)}",
            subtitle, sections, footer, AUTHOR_PER_ROW,
        )
        path = os.path.join(self.data_dir,
                            f"authsearch_{int(time.time() * 1000)}.jpg")
        with open(path, "wb") as fh:
            fh.write(data)
        yield event.image_result(path)

    async def _dispatch(self, event: AstrMessageEvent, kind: str):
        raw = event.message_str or ""
        # 去掉开头的指令名，剩下的原样保留（含空格、引号）
        body = re.sub(r"^\s*/?(?:px|novel)\b", "", raw, count=1, flags=re.IGNORECASE).strip()
        sub, _, arg = body.partition(" ")
        sub = sub.strip().lower()
        arg = arg.strip().strip('"').strip("“”").strip()

        if sub in ("", "help", "帮助"):
            yield event.plain_result(_HELP)
            return

        try:
            if sub == "i":
                async for r in self._cmd_info(event, kind, arg):
                    yield r
            elif sub == "d":
                async for r in self._cmd_download(event, kind, arg):
                    yield r
            elif sub == "s":
                async for r in self._cmd_search(event, kind, arg):
                    yield r
            elif sub in ("new", "n", "最新"):
                async for r in self._cmd_new(event, kind, arg):
                    yield r
            else:
                yield event.plain_result(f"未知子指令「{sub}」\n\n{_HELP}")
        except PixivError as exc:
            yield event.plain_result(f"⚠ {exc}")
        except Exception as exc:  # pragma: no cover - 兜底，避免插件把事件打崩
            logger.exception("[%s] 处理失败", PLUGIN_NAME)
            yield event.plain_result(f"⚠ 出错了：{exc}")

    # ------------------------------------------------------------ i：查看信息
    async def _cmd_info(self, event: AstrMessageEvent, kind: str, arg: str):

        if not arg:
            yield event.plain_result("用法：/px i <作品ID 或标题>")
            return
        key = f"{event.get_sender_id()}:{kind}:i"
        async for r in self._guard(event, key, self._do_info(event, kind, arg)):
            yield r

    async def _do_info(self, event: AstrMessageEvent, kind: str, arg: str):
        """信息卡直接发图片（不合并转发）。"""
        await self._cleanup()
        if kind == "illust":
            item = await self._resolve_illust(arg)
            thumb = await self._thumb(illust_image_url(item["raw"], 0, quality="medium"))
            path = await self._card(cards.illust_info_card(item, thumb))
        else:
            item = await self._resolve_novel(arg)
            thumb = await self._thumb(item.get("cover") or "")
            path = await self._card(cards.novel_info_card(item, thumb))
        yield event.image_result(path)

    # ------------------------------------------------------------ d：下载内容
    async def _cmd_download(self, event: AstrMessageEvent, kind: str, arg: str):

        if not arg:
            if kind == "illust":
                yield event.plain_result(
                    "用法：/px d <作品ID 或标题> [张数]\n"
                    "不写张数就下载全部原图，写数字只下前 N 张\n"
                    "例如：/px d 191823184      /px d 191823184 3"
                )
            else:
                yield event.plain_result("用法：/novel d <作品ID 或标题>")
            return
        if kind == "illust":
            async for r in self._guard(event, f"{event.get_sender_id()}:px:d", self._do_download_illust(event, arg)):
                yield r
        else:
            async for r in self._guard(event, f"{event.get_sender_id()}:novel:d", self._do_download_novel(event, arg)):
                yield r

    async def _guard(self, event: AstrMessageEvent, key: str, agen):
        """同一会话同一功能串行，避免重复刷屏把 API 打爆。"""
        if key in self._running:
            yield event.plain_result("同一个任务还在进行中，稍等一下～")
            return
        self._running.add(key)
        try:
            async for r in agen:
                yield r
        finally:
            self._running.discard(key)

    async def _do_download_illust(self, event: AstrMessageEvent, arg: str):
        await self._cleanup()
        arg, want = _split_count(arg)     # /px d <id> <张数>，不写张数就下全部
        item = await self._resolve_illust(arg)
        raw = item["raw"]
        pages = illust_page_count(raw)
        limit = min(pages, want or DOWNLOAD_HARD_MAX, DOWNLOAD_HARD_MAX)
        extra = ""
        if item.get("ugoira"):
            extra = "这组是动图（ugoira），这里只发静帧"
        if want and want < pages:
            extra = (extra + "；" if extra else "") + \
                f"作品共 {pages} 张，按要求只发前 {limit} 张"
        elif limit < pages:
            extra = (extra + "；" if extra else "") + \
                f"作品共 {pages} 张，超过单次上限，只发前 {DOWNLOAD_HARD_MAX} 张"
        budget = _Budget(DOWNLOAD_TIME_BUDGET)
        sem = asyncio.Semaphore(int(self._cfg("download_concurrency", 3) or 3))
        tasks = [
            self._download_image(illust_image_url(raw, i), sem, budget)
            for i in range(limit)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        nodes: list[Node] = []
        note_lines: list[str] = []
        ok = 0
        multi = limit > 1
        for idx, result in enumerate(results):
            if isinstance(result, BaseException) or not result:
                note_lines.append(f"第 {idx + 1} 张：失败")
                continue
            path, note = result
            if not path:
                note_lines.append(f"第 {idx + 1} 张：{note}")
                continue
            ok += 1
            caption = f"{idx + 1}/{limit}" if multi else "原图"
            if note:
                caption += f"（{note}）"
            nodes.append(self._node(event, [Plain(caption + "\n"), Image.fromFileSystem(path)]))

        if not nodes:
            yield event.plain_result("⚠ 一张都没下载成功：" + "；".join(note_lines[:3]))
            return
        if note_lines:
            extra = (extra + "；" if extra else "") + "；".join(note_lines[:5])
        header = await self._card(cards.download_header_card(item, ok, extra))
        nodes.insert(0, self._node(event, [Image.fromFileSystem(header)]))
        async for r in self._send_forward(event, nodes):
            yield r

    async def _do_download_novel(self, event: AstrMessageEvent, arg: str):
        await self._cleanup()
        novel_id = _extract_id(arg)
        if novel_id:
            item, body = await self._novel_full(novel_id)
        else:
            guess = await self._resolve_novel(arg)      # 关键词先搜到 ID
            item, body = await self._novel_full(int(guess["id"]))
        if not body.strip():
            yield event.plain_result(
                "⚠ 没取到正文（这篇可能需要登录才能看，或网页接口被限制）"
            )
            return
        meta = [
            f"作者：{item.get('author')}（UID {item.get('uid')}）",
            f"小说ID：{item.get('id')}",
            "标签：" + " ".join(f"#{t}" for t in (item.get("tags") or [])),
            f"收藏 {item.get('bookmarks')} ｜ 浏览 {item.get('views')}"
            f" ｜ 评论 {item.get('comments')}",
        ]
        data = to_txt_bytes(str(item.get("title") or ""), meta, body)
        safe_title = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(item.get("title") or "novel"))[:40]
        path = os.path.join(self.data_dir, f"{item.get('id')}_{safe_title}.txt")
        with open(path, "wb") as fh:
            fh.write(data)
        size_kb = max(1, len(data) // 1024)

        thumb = await self._thumb(item.get("cover") or "")
        header = await self._card(cards.novel_info_card(item, thumb))

        # 只发两样东西：信息卡片（图片）+ txt 文件。
        # 不走合并转发——节点里的文件段在 NapCat 上会变成 0 字节空文件，
        # 而顶层消息里的 file:// 它能正常处理。
        from astrbot.api.message_components import File

        yield event.image_result(header)
        yield event.chain_result([File(file=path, name=os.path.basename(path))])

    # -------------------------------------------------------------- s：搜索
    async def _cmd_search(self, event: AstrMessageEvent, kind: str, arg: str):

        if not arg:
            if kind == "illust":
                yield event.plain_result(
                    "用法：/px s [R18] [AI] <标签> [页码]\n"
                    "多个标签用逗号分隔，-标签 表示排除；\n"
                    "R18 / AI 的含义是「只要这一类」，位置和大小写不限；小说不筛选"
                )
            else:
                yield event.plain_result(
                    "用法：/novel s <标签> [页码]（多个标签用逗号分隔，-标签 表示排除）"
                )
            return
        key = f"{event.get_sender_id()}:{kind}:s"
        async for r in self._guard(event, key, self._do_search(event, kind, arg)):
            yield r

    async def _do_search(self, event: AstrMessageEvent, kind: str, arg: str,
                         force_sort: str | None = None):
        """搜索：**一张长图**里放下本页所有结果，直接发图片（不合并转发）。

        force_sort="date_desc" 就是 /px new（看最新），否则按配置走「近期热门」。
        `R18` / `AI` 开关**只有插画支持**，小说不过滤、也不认这两个词。
        """
        await self._cleanup()
        if kind == "illust":
            body, only_r18, only_ai = _split_flags(arg)
        else:
            body, only_r18, only_ai = arg, False, False
        word, days, page = _split_window(body)
        if not word:
            yield event.plain_result("标签不能为空")
            return
        result = await self._search(kind, word, page, force_sort=force_sort,
                                    only_r18=only_r18, only_ai=only_ai,
                                    days=days)
        items = result["items"]
        if not items:
            hint = f"没搜到「{word}」第 {page} 页" if page > 1 else f"没搜到「{word}」"
            if kind != "illust":
                yield event.plain_result(hint)
                return
            tail = f"（当前{'仅 R18' if only_r18 else '不含 R18'}、"
            tail += f"{'仅 AI' if only_ai else '不含 AI'}）"
            yield event.plain_result(f"{hint}{tail}")
            return
        entries, problems = await self._collect_entries(kind, items)
        if not entries:
            yield event.plain_result("⚠ 结果都处理失败了：" + "；".join(problems[:3]))
            return

        # 排序标签后面补上筛选状态，方便一眼看出这页是什么
        label = result["label"]
        if kind == "illust" and (only_r18 or only_ai):
            label += "　仅 " + " + ".join(
                n for n, on in (("R18", only_r18), ("AI", only_ai)) if on)

        # 翻页提示要带上时间窗，不然点下一页就丢掉天数了
        nav = f"{word} {days}" if days else word
        data = await asyncio.to_thread(
            long_card.render_search_long, kind, word, label, page,
            result["total_pages"], result["has_more"], entries, nav,
        )
        path = os.path.join(self.data_dir, f"search_{kind}_{int(time.time() * 1000)}.jpg")
        with open(path, "wb") as fh:
            fh.write(data)
        yield event.image_result(path)
        if problems:
            yield event.plain_result("；".join(problems[:5]))

    # ------------------------------------------------------------ new：看最新
    async def _cmd_new(self, event: AstrMessageEvent, kind: str, arg: str):

        if not arg:
            if kind == "illust":
                yield event.plain_result(
                    "用法：/px new [R18] [AI] <标签> [页码]\n"
                    "看该标签最新投稿；默认不含 R18 和 AI"
                )
            else:
                yield event.plain_result(
                    "用法：/novel new <标签> [页码]（看该标签最新投稿）"
                )
            return
        key = f"{event.get_sender_id()}:{kind}:new"
        async for r in self._guard(
            event, key, self._do_search(event, kind, arg, force_sort="date_desc")
        ):
            yield r

    async def _build_entries(self, kind: str, items: list[dict],
                            error_label: str = "") -> list[dict]:
        """把一批作品变成长图条目（并发下缩略图）。

        items 既可以是原始响应，也可以是已经归一化过的（有 "raw" 键）——
        两类入口都会走到这里。
        """
        if not items:
            return []
        normalized = [it if "raw" in it else
                      (normalize_illust(it) if kind == "illust" else normalize_novel(it))
                      for it in items]
        sem = asyncio.Semaphore(int(self._cfg("download_concurrency", 3) or 3))

        async def _one(item: dict) -> dict:
            async with sem:
                thumb = await self._thumb(
                    illust_image_url(item.get("raw") or {}, 0, quality="medium"))
            return {"item": item, "thumb": thumb,
                    "note": "" if thumb else "缩略图下载失败"}

        results = await asyncio.gather(*(_one(it) for it in normalized),
                                       return_exceptions=True)
        entries: list[dict] = []
        for index, res in enumerate(results, 1):
            if isinstance(res, BaseException):
                logger.debug("[%s] %s 第 %s 条处理失败：%s",
                             PLUGIN_NAME, error_label or kind, index, res)
                continue
            entries.append(res)
        return entries

    async def _collect_entries(self, kind: str, items: list[dict]) -> tuple[list[dict], list[str]]:
        """搜索用：已归一化的条目 -> (长图条目, 问题说明)。"""
        entries = await self._build_entries(kind, items, error_label="搜索")
        problems = [] if len(entries) == len(items) else ["有结果处理失败"]
        return entries, problems

    async def terminate(self):
        for client in self._clients.values():
            try:
                await client.close()
            except Exception:
                pass
        self._clients.clear()


class _Budget:
    """下载总时长预算。"""

    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + max(5.0, seconds)

    def remaining(self) -> float:
        return max(0.5, self.deadline - time.monotonic())

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline


def _clean_png(data: bytes) -> bytes | None:
    """把任意图片字节重新编码成干净的 PNG，供 zdy_t2i 当行内图片用。"""
    try:
        from PIL import Image as PILImage
    except ImportError:
        return None
    try:
        with PILImage.open(io.BytesIO(data)) as img:
            img.load()
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            if max(img.size) > 1600:  # 卡片里只会缩到几百像素高，先压一下省内存
                img = img.copy()
                img.thumbnail((1600, 1600), PILImage.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:
        return None


_HELP = """Pixiv 插件用法
/px i <ID｜含ID的文字｜标题>      查看插画信息卡：作者/标题/标签/收藏…（直接发图片）
/px d <同上> [张数]               下载原图（合并转发）；默认最多 40 张
/px s [R18] [AI] <标签> [天数] [页码]  搜该标签近期最热门插画（时间窗内按收藏降序）
/px new [R18] [AI] <标签> [页码]  看该标签最新投稿插画
/novel i <ID｜标题>               查看小说信息卡（直接发图片）
/novel d <同上>                   下载小说正文（合并转发 信息卡 + 正文，另发 txt）
/novel s <标签> [天数] [页码]      搜该标签近期最热门小说
/novel new <标签> [页码]          看该标签最新投稿小说
/auth <作者名｜作者ID> [页码]     作者作品网格（自动判定插画/小说作者，每页 20 件）
/auth s <作者名> [页码]           按名字搜作者（每页 4 位，每位 4 件作品）
插画可以用 R18 / AI 开关筛（含义是「只要这一类」，位置、大小写不限）；小说不过滤：
      /px s 原神              不含 R18、不含 AI
      /px s R18 原神          仅 R18
      /px s AI 原神           仅 AI
      /px s R18 AI 原神       仅「既 R18 又 AI」
      /px s R18 AI 原神 180 2 近 180 天，第 2 页
时间窗写法：标签后面一个数 ≤10 当页码，>10 当天数；两个数就是「天数 + 页码」。
      /px d 191823184 3       只下前 3 张原图
      /auth 案山子杏子 2      该作者作品网格第 2 页
      /auth s カンザリン      按名字搜作者
多个标签用逗号分隔，「-标签」表示排除。
默认时间窗 365 天，想换天数在标签后面写（如 /px s 原神 180）；排序、过滤在插件配置里改。"""
