"""极简 Pixiv API 客户端（只用 aiohttp，不依赖 pixivpy 等第三方库）。

一些实测得到的事实（2026-06 用真实账号验证过）：

* 认证：refresh_token -> access_token，放进 Authorization 头即可。
* `image_urls` / `meta_pages[].image_urls` 里的地址才是能下载的地址；
  **不要**自己把 `/img-original/` 改写成 `/img-medium/` 之类，Pixiv 返回 404。
* 统计数据字段是 `total_bookmarks` / `total_view` / `total_comments`；
  老的 `like_count` / `bookmark_count` / `view_count` 现在恒为 null。
* `/v1/novel/detail` 已下线（404），必须用 `/v2/novel/detail`；它只有元数据，
  **没有正文**。正文要从网页接口 `https://www.pixiv.net/ajax/novel/{id}` 取
  （`body.content` 就是已经排好版的纯文本），该接口不需要 token。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time

import aiohttp

logger = logging.getLogger("astrbot_plugin_pixiv_lite")

APP_API = "https://app-api.pixiv.net"
WEB_API = "https://www.pixiv.net"
OAUTH_URL = "https://oauth.secure.pixiv.net/auth/token"

# 与 pixivpy3 使用的公开客户端凭据保持一致
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
HASH_SECRET = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"

APP_UA = "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)"
WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# 图片 CDN 会校验 Referer，App 域名即可
IMG_HEADERS = {
    "Referer": "https://app-api.pixiv.net/",
    "User-Agent": APP_UA,
    "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*",
}

_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")


class PixivError(RuntimeError):
    """Pixiv API 相关错误的统一包装。"""


# --------------------------------------------------------------------------
# 从原始响应里取图片地址（唯一正确的做法：读响应，别改写 URL）
# --------------------------------------------------------------------------

def _urls(item: dict, page: int = 0) -> dict:
    """取某一份作品/某一页的 image_urls 字典。"""
    if not isinstance(item, dict):
        return {}
    pages = item.get("meta_pages") or []
    if page < len(pages):
        urls = pages[page].get("image_urls")
        if isinstance(urls, dict) and urls:
            return urls
    if page == 0:
        single = item.get("meta_single_page") or {}
        if single.get("original_image_url"):
            return {"original": single["original_image_url"],
                    **{k: v for k, v in (item.get("image_urls") or {}).items()}}
    urls = item.get("image_urls")
    if isinstance(urls, dict) and urls:
        return urls
    if pages and isinstance(pages[0].get("image_urls"), dict):
        return pages[0]["image_urls"]
    return {}


def illust_image_url(illust: dict, page: int = 0, quality: str = "original") -> str:
    """取某一页的图片地址。

    quality: original（原图）/ large（600x1200）/ medium（540）/ square_medium（360）
    """
    urls = _urls(illust, page)
    if quality == "original":
        return urls.get("original") or urls.get("large") or urls.get("medium") or ""
    if quality == "large":
        return urls.get("large") or urls.get("original") or urls.get("medium") or ""
    if quality == "medium":
        return urls.get("medium") or urls.get("large") or ""
    return urls.get(quality) or urls.get("medium") or urls.get("large") or ""


def illust_page_count(illust: dict) -> int:
    """页数。动图（ugoira）的 meta_pages 是空的，只能靠 page_count。"""
    pages = illust.get("meta_pages") or []
    if pages:
        return len(pages)
    return max(1, int(illust.get("page_count") or 1))


def is_ugoira(illust: dict) -> bool:
    return str(illust.get("type") or "") == "ugoira"


def _tag_names(tags) -> list[str]:
    """兼容两种 tags 结构：[{name}] / [{tag}] / ["字符串"]。"""
    out: list[str] = []
    for tag in tags or []:
        if isinstance(tag, str):
            name = tag
        elif isinstance(tag, dict):
            name = tag.get("name") or tag.get("tag") or ""
        else:
            continue
        name = str(name).strip()
        if name:
            out.append(name)
    return out


def _user_of(item: dict, web: dict | None = None) -> dict:
    user = item.get("user") or {}
    if not user and web:
        return {"id": web.get("userId"), "name": web.get("userName")}
    return {"id": user.get("id"), "name": user.get("name")}


def stats_of(item: dict) -> dict:
    """把 app-api（snake_case）与网页接口（camelCase）的统计字段统一。

    现在的字段是 total_bookmarks / total_view / total_comments（app）
    与 bookmarkCount / viewCount / commentCount（web）；
    老的 like_count / bookmark_count / view_count 已恒为 null，所以 likes 基本是 0。
    """
    def pick(*keys):
        for key in keys:
            value = item.get(key)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    continue
        return 0

    return {
        "likes": pick("like_count", "likeCount"),
        "bookmarks": pick("total_bookmarks", "bookmark_count", "bookmarkCount"),
        "views": pick("total_view", "view_count", "viewCount"),
        "comments": pick("total_comments", "comment_count", "commentCount"),
    }


def _looks_ai(tags: list[str]) -> bool:
    """标签里有没有 AI 字样（AI / AIイラスト / AI-generated / AIart / PixAI…）。"""
    for tag in tags:
        if "ai" in str(tag).lower():
            return True
    return False


def is_ai_work(item: dict, tags: list[str] | None = None) -> bool:
    """判断作品是不是 AI 生成的。

    两个信号，**任一命中就算 AI**（实测两者会不一致，所以合并判断）：
      * `illust_ai_type` / `novel_ai_type` == 2（1 = 人类，2 = AI）
      * 标签里含 AI 字样
    """
    kind = 1 if "text_length" in item or "text_len" in item else 2
    for key in ("illust_ai_type", "novel_ai_type"):
        value = item.get(key)
        try:
            if value is not None and int(value) >= kind:
                return True
        except (TypeError, ValueError):
            pass
    return _looks_ai(tags if tags is not None else _tag_names(item.get("tags")))


def normalize_illust(illust: dict) -> dict:
    """插画 -> 卡片统一结构。"""
    stats = stats_of(illust)
    tags = _tag_names(illust.get("tags"))
    return {
        "id": illust.get("id"),
        "title": illust.get("title") or "无标题",
        "author": _user_of(illust).get("name") or "未知作者",
        "uid": _user_of(illust).get("id"),
        "tags": tags,
        "likes": stats["likes"],
        "bookmarks": stats["bookmarks"],
        "views": stats["views"],
        "comments": stats["comments"],
        "pages": illust_page_count(illust),
        "x_restrict": int(illust.get("x_restrict") or 0),
        "create": str(illust.get("create_date") or "")[:19].replace("T", " "),
        "ugoira": is_ugoira(illust),
        "ai": is_ai_work(illust, tags),
        # 原样保留，供 illust_image_url 使用
        "raw": illust,
    }


def normalize_novel(app: dict | None, web: dict | None = None) -> dict:
    """小说 -> 卡片统一结构。app = /v2/novel/detail 的 novel，web = 网页接口的 body。"""
    app = app or {}
    web = web or {}
    stats = stats_of(web) if web else stats_of(app)
    if not any(stats.values()) and app:
        stats = stats_of(app)
    user = _user_of(app, web)
    cap = web.get("caption") or app.get("caption") or ""
    web_tags = web.get("tags")
    if isinstance(web_tags, dict):
        web_tags = web_tags.get("tags")
    tags = _tag_names(web_tags) or _tag_names(app.get("tags"))
    merged = dict(app)
    if web.get("aiType") is not None:
        merged["novel_ai_type"] = web.get("aiType")
    return {
        "id": app.get("id") or web.get("id"),
        "title": web.get("title") or app.get("title") or "无标题",
        "author": user.get("name") or "未知作者",
        "uid": user.get("id"),
        "tags": tags,
        "likes": stats["likes"],
        "bookmarks": stats["bookmarks"],
        "views": stats["views"],
        "comments": stats["comments"],
        "text_len": web.get("characterCount") or app.get("text_length") or 0,
        "x_restrict": int(web.get("xRestrict") or app.get("x_restrict") or 0),
        "create": str(web.get("createDate") or app.get("create_date") or "")[:19].replace("T", " "),
        "caption": cap,
        "cover": (app.get("image_urls") or {}).get("large")
        or (app.get("image_urls") or {}).get("medium")
        or web.get("coverUrl") or "",
        "ai": is_ai_work(merged, tags),
        "raw": app,
    }


_EXT_BY_FORMAT = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "BMP": ".bmp", "WEBP": ".webp"}


def normalize_user(user: dict, profile: dict | None = None) -> dict:
    """作者 -> 卡片统一结构。profile 来自 /v1/user/detail 的同级字段。"""
    user = user or {}
    profile = profile or {}
    images = user.get("profile_image_urls") or {}
    return {
        "id": user.get("id"),
        "name": user.get("name") or "未知作者",
        "account": user.get("account") or "",
        "avatar": images.get("medium") or images.get("px_170x170") or "",
        "comment": str(user.get("comment") or "").strip(),
        "webpage": str(profile.get("webpage") or "").strip(),
        "region": str(profile.get("region") or "").strip(),
        "gender": profile.get("gender"),
        "birth": str(profile.get("birth") or "").strip(),
        "job": str(profile.get("job") or "").strip(),
        "illusts": int(profile.get("total_illusts") or 0),
        "manga": int(profile.get("total_manga") or 0),
        "novels": int(profile.get("total_novels") or 0),
        "bookmarks": int(profile.get("total_illust_bookmarks_public") or 0),
        "following": int(profile.get("total_follow_users") or 0),
        "mypixiv": int(profile.get("total_mypixiv_users") or 0),
        "series": int(profile.get("total_illust_series") or 0),
    }


def image_format(data: bytes) -> str | None:
    """按字节魔数判断图片格式，判断不出来返回 None（不抛异常）。"""
    if not data:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if data[:2] == b"BM":
        return "BMP"
    return None


def guess_ext_from_url(url: str, data: bytes = b"") -> str:
    """定文件后缀：真实字节 > URL 后缀 > 兜底 .jpg。

    Pixiv 的 medium 图地址形如 `..._p0_master1200`（没有后缀），
    而且经常「地址 .jpg、内容 PNG」，所以字节优先。
    """
    fmt = image_format(data)
    if fmt:
        return _EXT_BY_FORMAT.get(fmt, ".jpg")
    _stem, ext = os.path.splitext(os.path.basename(url.split("?")[0]))
    if ext.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        return ext.lower()
    return ".jpg"


def check_image(data: bytes, url: str = "") -> str:
    """确认拿到的是图片而不是错误页，返回格式名；不是图片就抛错。"""
    fmt = image_format(data)
    if fmt:
        return fmt
    if not data:
        raise PixivError("下载到的内容是空的")
    head = data[:60].decode("utf-8", "replace").replace("\n", " ")
    raise PixivError(
        f"拿到的不是图片（{len(data)} 字节，开头是 {head!r}）"
        + (f"｜地址 {url}" if url else "")
    )


MARK_SIZE = 3                # 防屏蔽标记的边长（像素）
_MARK_INK = (0, 0, 0)        # 标记颜色：纯黑


def mark_image(data: bytes, size: int = MARK_SIZE) -> bytes | None:
    """在图片**右上角**点一小块纯黑像素，改掉文件特征，绕过 QQ 的图片屏蔽。

    * PNG / BMP / WEBP / GIF —— 按原格式重存，**PNG 完全无损**
    * JPEG —— 只能有损重编码，用 `quality=95` + 不做色度抽样，
      肉眼基本无差别（代价是体积会比原图大些）
    * 动图 —— 不处理，免得把动画弄坏

    标记之外的地方一律不动，尺寸和格式都不变。
    处理失败返回 None，调用方原样发原图，绝不让下载流程挂掉。
    """
    import io as _io

    from PIL import Image, ImageDraw

    try:
        with Image.open(_io.BytesIO(data)) as src:
            src.load()
            fmt = str(src.format or "").upper()
            # is_animated 只有打开着的对象上才有，copy() 之后就丢了
            animated = bool(getattr(src, "is_animated", False))
            img = src.copy()
    except Exception:
        return None
    if not fmt or animated:
        return None

    width, height = img.size
    if width < 8 or height < 8:
        return None
    px = max(1, min(int(size), width // 8, height // 8))
    try:
        if img.mode not in ("RGB", "RGBA", "L", "LA"):
            img = img.convert("RGBA")
        ImageDraw.Draw(img).rectangle(
            [width - px, 0, width - 1, px - 1], fill=_MARK_INK)
    except Exception:
        return None

    buf = _io.BytesIO()
    try:
        if fmt in ("JPEG", "JPG", "MPO"):
            img.convert("RGB").save(
                buf, "JPEG", quality=95, subsampling=0, optimize=True)
        elif fmt == "PNG":
            img.save(buf, "PNG", optimize=True)
        elif fmt == "WEBP":
            img.save(buf, "WEBP", quality=95, method=4)
        elif fmt == "BMP":
            img.save(buf, "BMP")
        elif fmt == "GIF":
            img.save(buf, "GIF")
        else:
            return None
    except Exception:
        return None
    return buf.getvalue() or None


class PixivClient:
    """一个账号一个客户端；内部缓存 access_token 与少量查询结果。"""

    def __init__(self, refresh_token: str, proxy: str = "", timeout: int = 20):
        self.refresh_token = (refresh_token or "").strip()
        self.proxy = (proxy or "").strip() or None
        self.timeout = max(5, int(timeout))
        self._session: aiohttp.ClientSession | None = None
        self._token = ""
        self._token_expire = 0.0
        self._lock = asyncio.Lock()
        self._cache: dict[tuple, tuple[float, object]] = {}

    # ---------------------------------------------------------------- session
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # 关键：会话**不设** total 超时。
            # 一旦会话有总时长上限，大图（几 MB）下载会在超时点被 aiohttp 悄悄截断，
            # content.read() 不报错但只返回已读到的部分（实测 3.3MB 只拿到 15KB）。
            # 超时改成按请求传（API 用 timeout，图片用 image_timeout）。
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _req_kwargs(self) -> dict:
        return {"proxy": self.proxy} if self.proxy else {}

    def _timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(total=self.timeout, connect=min(15, self.timeout))

    def _img_timeout(self) -> aiohttp.ClientTimeout:
        # 图片给足时间：只限制「连接 + 单次读阻塞」，不限制总时长
        return aiohttp.ClientTimeout(total=None, connect=min(20, self.timeout),
                                     sock_read=60)

    # ------------------------------------------------------------------ token
    async def _login(self, force: bool = False) -> str:
        if not self.refresh_token:
            raise PixivError("未配置 refresh_token，请到插件配置里填写")
        async with self._lock:
            if self._token and not force and time.time() < self._token_expire:
                return self._token
            session = await self._get_session()
            stamp = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
            headers = {
                "User-Agent": APP_UA,
                "App-OS": "android",
                "App-OS-Version": "11",
                "App-Version": "5.0.234",
                "X-Client-Time": stamp,
                "X-Client-Hash": hashlib.md5((stamp + HASH_SECRET).encode()).hexdigest(),
                "Content-Type": "application/x-www-form-urlencoded",
            }
            data = {
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "refresh_token",
                "include_policy": "true",
                "refresh_token": self.refresh_token,
            }
            try:
                async with session.post(
                    OAUTH_URL, data=data, headers=headers,
                    timeout=self._timeout(), **self._req_kwargs()
                ) as resp:
                    payload = await resp.json(content_type=None)
                    if resp.status != 200:
                        raise PixivError(
                            f"Pixiv 登录失败 HTTP {resp.status}: "
                            f"{_brief(payload) or (await resp.text())[:200]}"
                        )
            except aiohttp.ClientError as exc:
                raise PixivError(f"连接 Pixiv 失败（检查代理配置）：{exc}") from exc
            token = payload.get("access_token")
            if not token:
                raise PixivError(f"Pixiv 登录响应异常：{_brief(payload)}")
            self._token = token
            self._token_expire = time.time() + int(payload.get("expires_in") or 3600) - 120
            new_refresh = payload.get("refresh_token")
            if new_refresh and new_refresh != self.refresh_token:
                # Pixiv 偶尔会轮换 refresh_token，这里只留在内存里（不写回配置）
                self.refresh_token = new_refresh
            logger.info("Pixiv access_token 获取成功")
            return self._token

    # ------------------------------------------------------------------ 请求
    async def api_get(self, path: str, params: dict | None = None) -> dict:
        """带 token 的 GET（app-api），401 时自动重登重试一次。"""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        url = path if path.startswith("http") else APP_API + path
        for attempt in (0, 1):
            token = await self._login(force=attempt == 1)
            status, text = await self._raw_get(url, params, {
                "Authorization": f"Bearer {token}", "User-Agent": APP_UA,
            })
            if status == 401 and attempt == 0:
                continue
            if status != 200:
                raise PixivError(_explain(status, text))
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise PixivError(f"Pixiv 返回了非 JSON 内容：{text[:120]}") from exc
        raise PixivError("Pixiv 认证失败，refresh_token 可能已失效")

    async def web_get(self, path: str, params: dict | None = None) -> dict:
        """网页接口（www.pixiv.net/ajax/...），不需要 token。"""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        status, text = await self._raw_get(WEB_API + path, params, {
            "User-Agent": WEB_UA,
            "Referer": "https://www.pixiv.net/",
            "Accept": "application/json",
        })
        if status != 200:
            raise PixivError(_explain(status, text))
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PixivError(f"Pixiv 网页接口返回非 JSON：{text[:120]}") from exc
        if payload.get("error"):
            raise PixivError(f"Pixiv 网页接口报错：{payload.get('message') or '未知'}")
        return payload.get("body") or {}

    async def _raw_get(self, url: str, params: dict, headers: dict) -> tuple[int, str]:
        session = await self._get_session()
        try:
            async with session.get(
                url, params=params, headers=headers,
                timeout=self._timeout(), **self._req_kwargs()
            ) as resp:
                return resp.status, await resp.text()
        except aiohttp.ClientError as exc:
            raise PixivError(f"网络错误（{url.split('/')[2]}）：{exc}") from exc
        except asyncio.TimeoutError as exc:
            raise PixivError(f"请求超时（{url.split('/')[2]}），检查代理是否可用") from exc

    async def image_bytes(self, url: str, max_bytes: int = 64 * 1024 * 1024) -> bytes:
        """下载图片原始字节（图片 CDN 不需要 token，但需要 Referer）。

        用流式读取并对照 Content-Length 校验：aiohttp 在超时/连接中断时可能
        只返回已读到的部分而不报错，那样会把半张图写进磁盘。
        """
        if not url:
            raise PixivError("图片地址为空")
        session = await self._get_session()
        try:
            async with session.get(
                url, headers=IMG_HEADERS,
                timeout=self._img_timeout(), **self._req_kwargs()
            ) as resp:
                if resp.status != 200:
                    raise PixivError(f"图片下载失败 HTTP {resp.status}：{url}")
                declared = resp.headers.get("Content-Length")
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise PixivError("图片超过下载上限")
        except aiohttp.ClientError as exc:
            raise PixivError(f"图片下载网络错误：{exc}") from exc
        except asyncio.TimeoutError as exc:
            raise PixivError(f"图片下载超时（可能是代理不稳定）：{url}") from exc
        data = bytes(buf)
        if declared and declared.isdigit() and len(data) != int(declared):
            raise PixivError(
                f"图片下载不完整（只拿到 {len(data)} / {declared} 字节），"
                "多半是代理中途断了，重试或换个节点"
            )
        check_image(data, url)  # 不是图片就直接报错，绝不把错误页落到磁盘
        return data

    # ------------------------------------------------------------- 业务封装
    async def illust_detail(self, illust_id: int) -> dict:
        data = await self._cached(("illust", int(illust_id)), 60, "/v1/illust/detail",
                                  {"illust_id": int(illust_id)})
        illust = data.get("illust")
        if not illust:
            raise PixivError(f"找不到插画 {illust_id}")
        return illust

    async def novel_detail(self, novel_id: int) -> dict:
        """小说元数据。注意是 v2，v1 已下线。"""
        data = await self._cached(("novel", int(novel_id)), 60, "/v2/novel/detail",
                                  {"novel_id": int(novel_id)})
        novel = data.get("novel")
        if not novel:
            raise PixivError(f"找不到小说 {novel_id}")
        return novel

    async def novel_web(self, novel_id: int) -> dict:
        """网页接口的小说详情，含完整正文（body.content）。"""
        return await self._cached(("novelweb", int(novel_id)), 300,
                                  f"web:/ajax/novel/{int(novel_id)}", {})

    # ------------------------------------------------------------- 作者相关
    async def search_user(self, word: str) -> list[dict]:
        """按作者名/账号搜用户。注意是 /v1/search/user，
        `/v1/user/search` 与 `/v2/user/search` 都已下线（404）。"""
        data = await self.api_get("/v1/search/user", {"word": word})
        out = []
        for item in data.get("user_previews") or []:
            user = item.get("user") or {}
            if user.get("id"):
                out.append(user)
        return out

    async def user_detail(self, user_id: int) -> dict:
        """作者资料：user 本体 + 同级 profile（数量、地区、主页等）。"""
        return await self._cached(("user", int(user_id)), 300, "/v1/user/detail",
                                  {"user_id": int(user_id)})

    async def user_illusts(self, user_id: int, offset: int = 0, limit: int = 30,
                           type_: str | None = "illust") -> tuple[list[dict], str]:
        """作者的作品列表（默认按最新）。返回 (作品, next_url)。"""
        params = {"user_id": int(user_id), "offset": offset,
                  "filter": "for_ios"}
        if type_:
            params["type"] = type_
        data = await self.api_get("/v1/user/illusts", params)
        return list(data.get("illusts") or [])[:limit], str(data.get("next_url") or "")

    async def user_novels(self, user_id: int, offset: int = 0,
                          limit: int = 30) -> tuple[list[dict], str]:
        data = await self.api_get("/v1/user/novels",
                                  {"user_id": int(user_id), "offset": offset})
        return list(data.get("novels") or [])[:limit], str(data.get("next_url") or "")

    async def search_illust(
        self, word: str, sort: str = "date_desc", offset: int = 0, limit: int = 30,
        search_target: str = "partial_match_for_tags",
        start_date: str | None = None, end_date: str | None = None,
    ) -> list[dict]:
        """搜索插画。

        `start_date` / `end_date` 用 `YYYY-MM-DD`（带时间戳会 400）。
        只有 `sort="popular_desc"` 会真正尊重这两个参数——也就是「时间窗内按收藏降序」；
        `date_desc` 会忽略它们。
        """
        params = {
            "word": word,
            "sort": sort,
            "search_target": search_target,
            "offset": offset,
            "filter": "for_ios",
        }
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        data = await self.api_get("/v1/search/illust", params)
        return list(data.get("illusts") or [])[:limit]

    async def search_novel(
        self, word: str, sort: str = "date_desc", offset: int = 0, limit: int = 30,
        search_target: str = "partial_match_for_tags",
        start_date: str | None = None, end_date: str | None = None,
    ) -> list[dict]:
        params = {
            "word": word,
            "sort": sort,
            "search_target": search_target,
            "offset": offset,
            "filter": "for_ios",
        }
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        data = await self.api_get("/v1/search/novel", params)
        return list(data.get("novels") or [])[:limit]

    async def _cached(self, key: tuple, ttl: float, path: str, params: dict) -> dict:
        hit = self._cache.get(key)
        now = time.time()
        if hit and now - hit[0] < ttl:
            return hit[1]  # type: ignore[return-value]
        if path.startswith("web:"):
            data = await self.web_get(path[4:], params)
        else:
            data = await self.api_get(path, params)
        if len(self._cache) > 96:
            self._cache.clear()
        self._cache[key] = (now, data)
        return data


def _brief(payload) -> str:
    if isinstance(payload, dict):
        err = payload.get("error") or payload.get("errors") or payload
        return str(err)[:200]
    return str(payload)[:200]


def _explain(status: int, text: str) -> str:
    if status == 400:
        return f"Pixiv 拒绝了请求（HTTP 400，排序方式可能需要会员）：{text[:180]}"
    if status == 403:
        return "Pixiv 拒绝访问（HTTP 403，作品可能已删除或不可见）"
    if status == 404:
        return "Pixiv 返回 404：作品不存在、已删除，或该接口已下线"
    if status == 429:
        return "Pixiv 限流（HTTP 429），请稍后再试"
    return f"Pixiv 接口错误 HTTP {status}: {text[:180]}"
