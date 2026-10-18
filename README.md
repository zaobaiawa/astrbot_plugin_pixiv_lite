<div align="center">
  <img src="resources/top.png" alt="Pixiv 插画小说插件" width="720" />
</div>

# <div align="center">astrbot_plugin_pixiv_lite</div>

<div align="center">
  <strong>Pixiv 插画 / 小说 查询与下载</strong>
</div>

<br>
<div align="center">
  <img src="https://img.shields.io/badge/VERSION-v1.0.0-E91E63?style=for-the-badge" alt="Version">
  <img src="https://img.shields.io/badge/license-MIT-009688?style=for-the-badge" alt="License">
  <img src="https://img.shields.io/badge/PYTHON-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/AstrBot-Compatible-00BFA5?style=for-the-badge" alt="AstrBot Compatible">
</div>

<div align="center">
  <img src="https://img.shields.io/badge/OneBotv11-AIOCQHTTP-FF5722?style=for-the-badge&logo=qq&logoColor=white" alt="OneBot v11">
  <img src="https://img.shields.io/badge/UPDATED-2026.10.18-2196F3?style=for-the-badge" alt="Updated">
</div>

## 介绍

查插画、下原图、下小说。

所有文字信息（信息卡、搜索结果、作者作品）都在本地渲染成图片，
通过 OneBot v11（NapCat 接入 QQ）发送，不依赖任何在线文转图服务。

> [!NOTE]
> 大部分代码由 AI 完成。

## 功能特性

- **插画详情** — 作者、标题、标签、收藏/浏览/评论、页数、发布时间，带缩略预览图
- **插画搜索** — 近期热门（时间窗内按收藏降序）与最新投稿两种排序
- **原图下载** — 完整原图，**不做任何压缩/转码**，合并转发一次发出
- **小说支持** — 详情卡片 + 正文 txt 下载
- **作者查询** — 作品网格（自动判断插画作者 / 小说作者）、按名字搜作者
- **自选时间窗** — 直接在指令里写天数，例如 `/px s 原神 180`
- **R18 / AI 筛选** — 指令开关，含义是「只要这一类」
- **长图排版** — 一页搜索结果 / 一屏作品网格，一张图看完
- **防屏蔽标记** — 给下载的原图点一小块黑像素，绕过 QQ 图片特征屏蔽
- **自带资源** — 字体与顶图打包在插件内，开箱即用

## 安装方法

### 1. 下载插件

把插件放到 AstrBot 的插件目录：

```
data/plugins/astrbot_plugin_pixiv_lite/
```

### 2. 安装依赖

```bash
cd data/plugins/astrbot_plugin_pixiv_lite
pip install -r requirements.txt
```

| 依赖              | 用途                          |
| ----------------- | ----------------------------- |
| `aiohttp>=3.8`    | Pixiv API 请求、图片下载      |
| `Pillow>=9.0`     | 卡片与长图渲染                |

> [!NOTE]
> 字体和顶图已经打包在 `resources/` 里（`MaokenZhuyuanTi.ttf` + `top.png`），
> 不需要额外安装中文字体，也不依赖 `zdy_t2i` 的外部安装。

### 3. 重启 AstrBot

确保插件被正确加载。

### 4. 配置 refresh_token（必填）

在 AstrBot 管理面板的「插件配置」里填 `refresh_token`，否则所有指令都会提示未配置。

插件用的是 Pixiv 账号的 **OAuth refresh_token**（长期凭证），需要用你自己的账号
走一次网页登录流程换取。常用工具是 `gppt`（get-pixivpy-token）或 `pixiv_auth.py`，
具体用法以这些工具的文档为准。

> [!WARNING]
> `refresh_token` 等同于账号登录态，**不要泄露给任何人**，也不要提交到仓库。
> 插件只会把它写进日志以外的地方——运行时数据落在
> `data/plugin_data/astrbot_plugin_pixiv_lite/`，不会写回插件目录。

### 5.（可选）配置代理

所在网络不能直连 Pixiv 时，在配置里填 `proxy`，例如：

```
http://127.0.0.1:7890
```

能直连就留空。

## 命令列表

### 指令总览

| 指令 | 发送方式 | 作用 |
| --- | --- | --- |
| `/px i <作品ID｜含ID的文字｜标题>` | 一张图 | 插画信息卡：作者、标题、标签、收藏/浏览/评论、页数、发布时间，带缩略预览图 |
| `/px s [R18] [AI] <标签> [天数] [页码]` | **一张长图** | 搜该标签**近期最热门**插画（时间窗内按收藏降序），每页 10 份 |
| `/px new [R18] [AI] <标签> [天数] [页码]` | **一张长图** | 搜该标签**最新投稿**插画（按发布时间降序） |
| `/px d <作品ID｜标题> [张数]` | **合并转发** | 下载原图；默认最多 40 张（NapCat 合并转发上限约 50 节点），写数字只下前 N 张 |
| `/novel i <小说ID｜标题>` | 一张图 | 小说信息卡：作者、标题、标签、收藏/浏览/评论、字数、简介 |
| `/novel s <标签> [天数] [页码]` | **一张长图** | 搜该标签近期最热门小说 |
| `/novel new <标签> [页码]` | **一张长图** | 搜该标签最新投稿小说 |
| `/novel d <小说ID｜标题>` | **一张图 + 一个 txt** | 下载小说正文：先发信息卡片，再发一个可下载的 txt |
| `/auth <作者名｜作者ID> [页码]` | **一张长图** | 作者作品**网格**（每页 20 件，4 列 × 5 行，只显示封面/标题/PID） |
| `/auth s <作者名> [页码]` | **一张长图** | 按名字搜作者（每页 4 位，每位显示最近 4 件作品，横排） |
| `/px help` | 文本 | 用法 |

`/px new` 也可以写 `/px n`、`/px 最新`；`/auth` 也可以写 `/作者`、`/author`。

### 搜索时间窗（自选天数）

`/px s` / `/novel s` 默认看**近 365 天**，也可以在标签后面直接写天数，**只对这一次生效**：

```
/px s 原神 180                近 180 天最热门
/px s 原神 180 2              近 180 天，第 2 页
/px s R18 AI 原神 180 2       近 180 天 + 仅 R18 且 AI，第 2 页
/px s 原神 17                 近 17 天
```

### 末尾数字的判定规则

| 写法 | 含义 |
| --- | --- |
| `<标签>` | 用默认的 365 天 |
| `<标签> 2` | 一个数且 ≤10 → **页码** |
| `<标签> 180` | 一个数且 >10 → **天数** |
| `<标签> 180 2` | 两个数 → **天数 + 页码** |

天数范围 1~1825（约 5 年），超出会自动夹紧。长图的翻页提示会带上天数。

### 作者查询

```
/auth 案山子杏子          作者作品网格（自动判断展示插画还是小说）
/auth 案山子杏子 2        该作者作品第 2 页
/auth 123128360          也可以直接给 UID
/auth s 初音ミク          按名字搜作者（4 位/页，每位最近 4 件作品）
/auth s 初音ミク 2        第 2 页
```

`/auth <作者>` 会先看该作者**最近 30 件**作品里插画多还是小说多，
按多的那一类展示，每页 20 件、4 列 × 5 行，格子里只有封面预览、作品名和 PID。

`/auth s` 是「搜作者」，和 pixiv 官网的用户搜索页一样：每位作者一行，
头像 + 资料 + 最近 4 件作品（插画小说混排）横排。

### R18 / AI 筛选（只有插画）

`/px s`、`/px new` 的 `R18` / `AI` 开关含义是「只要这一类」，
不写就是**不含**这一类。位置和大小写都不限（写前面后面都行）：

```
/px s 原神            不含 R18、不含 AI
/px s R18 原神        仅 R18（AI 也排除）
/px s AI 原神         仅 AI（R18 也排除）
/px s R18 AI 原神     仅「既 R18 又 AI」
/px s R18 原神 2      第 2 页 + 仅 R18
```

**小说不做筛选**：`/novel s`、`/novel new` 里的 `R18` / `AI` 会被当成
普通标签去搜，R18 和 AI 作品照发。

配置项 `ai_mode`（过滤 AI / 允许 AI）只影响插画**不写 AI 开关**时的默认行为：

| `ai_mode` | `/px s 原神`（没写 AI） |
| --- | --- |
| 过滤 AI（默认） | 不含 AI |
| 允许 AI | AI 作品也照发（但不强制只要 AI） |

### 搜索排序

- `/px s`（近期热门）→ `popular_desc` + `start_date`，**时间窗内按收藏降序**
- `/px new`（最新）→ `date_desc`，按发布时间降序，**不带时间窗**

非会员账号用「近期热门」会收到 HTTP 400，插件自动降级为
「抓最新结果 → 本地按最近 N 天过滤 → 按收藏重排」，并在长图头部标注排序来源。

官方的「热门 + 时间窗」有时会漏掉窗口内的作品，插件会在**这一页没填满**时
再用发布时间倒序补扫到窗口边界，合并去重后本地重排，长图头部的排序行会标
「已补全 N 条」。

### 参数写法

```
/px i 191823184
/px i "191823184"
/px i 我玩原神11小时肝了45141原石抽了91抽没出金（114514191）
/px i 宵宫と夏祭りの夜
```

规则：先找 5~12 位数字当作品 ID；找不到数字就把整段当标题关键词搜作品。

搜索支持多标签与排除：

```
/px s 初音ミク,可愛い -ntr -futa
/px s 初音ミク,可愛い -ntr 2      ← 带页码
```

## 配置项

所有配置可在 AstrBot 管理面板中修改：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `refresh_token` | 空 | **必填**，Pixiv 的 refresh_token |
| `proxy` | 空 | 如 `http://127.0.0.1:7890`；能直连就留空 |
| `timeout` | 20 | **API** 请求超时（秒）。图片下载不受它限制 |
| `card_color` | 彩色 | 彩色 / 纯黑 / 单色。也可以直接填 `#RRGGBB` |
| `ai_mode` | 过滤 AI | 过滤 AI / 允许 AI。只影响 `/px` 没写 `AI` 开关时的默认（写了开关就一定是仅 AI）；小说不筛 |
| `sort` | 近期热门（会员） | 近期热门（会员）走 `popular_desc`；或选「最新」（不推荐） |
| `search_count` | 10 | 搜索每页份数 1~20 |
| `download_concurrency` | 3 | 图片下载并发 1~8（作者网格也用它） |
| `mark_images` | **开** | 给 `/px d` 的原图右上角加防屏蔽标记（绕过 QQ 图片屏蔽） |
| `mark_size` | 3 | 标记边长（像素）1~8 |

### 关于 `/novel d` 的输出

只发两条消息：

1. **小说信息卡片**（图片）
2. **txt 文件**（顶层消息，AstrBot 会转成 `file://` 交给 NapCat）

不走合并转发，因为 NapCat 发普通文件只认 URL 或本地路径、**不认 base64**，
而 AstrBot 只在**顶层消息**里把本地路径转成 `file://`——塞进合并转发节点的文件段
会变成 0 字节空文件。

### 关于「原图」与 QQ 屏蔽

`/px d` **不做压缩**，但会在每张图的**右上角点一小块纯黑像素**（默认 3×3），
用来改掉图片文件特征——QQ 会按特征屏蔽某些图，原图直接发经常发不出去。

各种格式的处理方式：

| 格式 | 处理 | 画质影响 |
| --- | --- | --- |
| PNG | 原格式重存 | **完全无损**（实测标记之外 0 像素差异） |
| JPEG | `quality=95` + 不做色度抽样 | 重编码，平均每通道差 ~1.5/255，肉眼无感 |
| WEBP / BMP / GIF | 原格式重存 | 无损或近无损 |
| 动图 | **不处理** | 免得把动画弄坏 |

标记之外的地方一律不动，尺寸、格式都不变（后缀仍按真实字节嗅探）。
实测 1894KB 的 PNG，标记后 hash 从 `5984db8359c5` 变成 `0a1f0dff2fc9`，
而标记之外的像素差异是 **0**。

关掉或调大：配置项 `mark_images`（默认开）、`mark_size`（默认 3px，可调到 8）。
被屏蔽得厉害就把 `mark_size` 调大。

## 文件结构

```
astrbot_plugin_pixiv_lite/
├── main.py              # 插件入口：指令解析、搜索、下载、合并转发
├── pixiv_api.py         # Pixiv 客户端（OAuth + app-api + 网页接口 + 图片下载 + 字段归一化）
├── long_card.py         # 长图排版：竖排搜索结果 + 横排作者作品网格
├── cards.py             # 信息卡 / 头卡 / 翻页卡 / 错误卡 + 字体与折行
├── novel_text.py        # 小说正文解析（JSON 段落格式与网页纯文本，含 ruby 注音处理）
├── zdy_t2i.py           # 本地文转图库
├── metadata.yaml        # 插件元数据
├── _conf_schema.json    # 配置项定义
├── requirements.txt     # 依赖列表
├── LICENSE              # MIT
└── resources/
    ├── MaokenZhuyuanTi.ttf   # 自带中文字体
    └── top.png               # 卡片顶部图
```

运行时数据（下载的原图、txt、渲染出的卡片）不会写进这个目录，而是放在：

```
data/plugin_data/astrbot_plugin_pixiv_lite/cache/
```

## 常见问题

### Q: 提示「还没配置 refresh_token」？

**A:** 到管理面板「插件配置」里填 `refresh_token`。这是必填项，插件不会从别处读取。

### Q: 下载的图发不出去 / 群里看不到？

**A:** QQ 会按图片特征屏蔽部分内容。插件默认已经在原图右上角点了黑像素
（`mark_images` 开），如果还是发不出去，把 `mark_size` 从 3 调到 5~8。

### Q: `/px d` 只发了一部分图？

**A:** 单次上限 40 张。NapCat 的合并转发最多约 50 个节点，超过会整体发送失败，
所以默认封顶 40。想要更少就写张数：`/px d 191823184 5`。

### Q: `/novel d` 收到的 txt 是 0 字节？

**A:** 早期版本把文件塞进合并转发节点里，NapCat 不支持 `base64://` 的文件段，
会变成空文件。现在改成**顶层消息**发 `file://`，已修复。

### Q: 提示「拿不到插件数据目录」？

**A:** 插件拒绝把数据写进自己的安装目录。请检查 AstrBot 的 `data` 目录是否可写、
版本是否 >= 4.0。

### Q: 搜索结果比网页上少？

**A:** 官方接口的「热门 + 时间窗」返回的是不完整子集，插件会自动用发布时间
倒序补扫到窗口边界再合并。如果长图头部标了「已补全 N 条」就是补过的情况。
另外单页最多扫描 300 条原始结果、翻到第 10 页。

### Q: 请求太频繁被限流（HTTP 429）？

**A:** Pixiv 对高频请求会限流。调小 `download_concurrency`，或隔一会儿再试。
授权作者作品、连续翻页时最容易触发。

### Q: 长图内容被截断了？

**A:** 长图有上限（1080 宽、约 2960 高），一页最多 20 份。超出时多出的部分
会在图尾注明「还有 N 件未显示」，翻页即可看到。

### Q: 非会员能用「近期热门」吗？

**A:** 能，但会降级成「抓最新结果 → 本地按时间窗过滤 → 按收藏重排」，
长图头部会标注「本地重排（本账号非会员）」。

## 已知限制

- 动图（ugoira）只发静帧，头卡里会标注。
- 搜索最多翻到第 10 页、扫描上限 300 条原始结果。
- 小说系列文只取当前这一篇的正文。
- 长图上限 1080×2960，一页最多 20 份。
- `/px d` 的合并转发需要 OneBot v11（`metadata.yaml` 已声明 `support_platforms: [aiocqhttp]`）。

## 注意事项

- 本插件仅供学习交流使用。
- 请勿将下载的内容用于商业用途。
- 大量请求可能导致 IP 被限流或封禁。
- 请遵守当地法律法规。

## 许可证

[MIT License](./LICENSE)

Copyright (c) 2026 皂白

## 致谢

- [AstrBot](https://github.com/AstrBotDevs/AstrBot) — 机器人框架
- [Pillow](https://python-pillow.org/) — 图像处理
- [aiohttp](https://docs.aiohttp.org/) — 异步 HTTP
- 猫啃珠圆体（MaokenZhuyuanTi）— 自带字体，OFL 授权
