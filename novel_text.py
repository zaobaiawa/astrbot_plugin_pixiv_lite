"""把 Pixiv 小说的 novel/text 返回值（JSON 格式正文）转成可读纯文本。

/v1/novel/text 返回的 novel_text 是一段 JSON 字符串，形如：
    {"novel":{"text":[[0,"段落一"],[0,"段落二"],"\n",[1,"小标题",1],...]}}
节点形态（不同作品略有差异，两种都兼容）：
    文本节点：  [indent, "正文"]  或  [indent, ["正文", 2, [0,"注音",2], 1, 0]]
    小标题：    [indent, "标题", 1]
    注音 ruby：文本节点内部嵌套的 [0,"注音",2]，正文里丢掉避免重复
    "\n"：      空行
"""

from __future__ import annotations

import json

_RUBY_KINDS = (2, 3)


def _text_of(node: list) -> tuple[str, int]:
    """取出节点文本与外层类型；取不到就返回空串。

    注意：嵌套形态 [indent, ["正文", 2, [0,"注音",2], 1, 0]] 里那个 2 是
    “这段带注音”的标记，不代表节点本身是注音节点，所以只认外层的类型。
    """
    if len(node) < 2:
        return "", 0
    payload = node[1]
    kind = node[2] if len(node) > 2 and isinstance(node[2], int) else 0
    if isinstance(payload, str):
        return payload, kind
    if isinstance(payload, list):
        text = payload[0] if payload and isinstance(payload[0], str) else ""
        return text, kind
    return "", kind


def parse_novel_text(raw: str | list | None) -> str:
    """把 JSON 正文转成纯文本；解析失败时退回原始字符串，不抛异常。"""
    if not raw:
        return ""
    if isinstance(raw, str):
        # 注意：不能用 strip() 判断，全角空格（段首缩进）会被一起吃掉
        stripped = raw.lstrip("\r\n\t ")
        if stripped[:1] in ("{", "["):
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError:
                return normalize(raw)
        else:
            return normalize(raw)
    if isinstance(raw, dict):
        raw = (raw.get("novel") or {}).get("text") or []
    if not isinstance(raw, list):
        return normalize(str(raw))
    return normalize(_walk(raw))


def _walk(nodes: list) -> str:
    """Pixiv 的每个节点基本等于一个段落；节点之间补换行。"""
    out: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            out.append(node)          # 通常是 "\n"，用来空一行
            continue
        if not isinstance(node, list):
            continue
        text, kind = _text_of(node)
        if kind in _RUBY_KINDS or not text:
            continue                  # 注音节点丢掉，避免正文重复
        indent = node[0] if node and isinstance(node[0], int) else 0
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        if kind == 1:                 # 小标题
            out.append("\n" + text.strip() + "\n")
            continue
        if indent > 0:
            text = "　　" + text       # 首行缩进
        out.append(text + "\n")
    return "".join(out)


def normalize(text: str) -> str:
    """收敛空白：连续空行压成一个，去掉行尾空格，统一换行。

    注意只 rstrip（去行尾），不动行首——日文小说靠全角空格做首行缩进。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    result: list[str] = []
    blank = False
    for line in text.split("\n"):
        line = line.rstrip()
        if line.strip():
            blank = False
            result.append(line)
        elif not blank:
            blank = True
            result.append("")
    while result and not result[0].strip():
        result.pop(0)
    while result and not result[-1].strip():
        result.pop()
    return "\n".join(result)


def to_txt_bytes(title: str, meta_lines: list[str], body: str) -> bytes:
    """拼成最终 txt：标题 / 元信息 / 分隔线 / 正文。"""
    head = [title.strip() or "无标题", *[m for m in meta_lines if m], "=" * 32, ""]
    return ("\n".join(head) + "\n" + body + "\n").encode("utf-8")
