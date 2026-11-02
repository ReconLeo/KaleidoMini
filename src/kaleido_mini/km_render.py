# -*- coding: utf-8 -*-
"""km_render —— 题库富文本渲染：HTML 白名单净化 + /media/ 重写 + 缺失占位

================================================================================
题库内容是 [不可信输入]（可能来自任意上传的题库文件），而考试页要把题干、选项、
解析直接显示出来 —— 这是本插件最大的注入面。本模块负责把不可信 HTML 变成
只含白名单标签的安全 HTML。

处理流程（一次遍历完成）：
    去危险块（script/style/iframe/object/embed/noscript/template/svg…）
    → 标签白名单过滤 → 属性白名单过滤 → URL 安全校验 + 媒体重写 → 重平衡标签

三种媒体形态（3.1.0 规范，见 docs/KQB-v1-规范.md）：
    data-img-type="base64"    src 为 data:…   → 原样保留（仅 image/audio/video）
    src="/media/<name>"       相对引用        → 经 resolve_media 换成插件媒体端点
    src="https://…"           外部链接        → 默认保留但加 referrerpolicy=no-referrer
                                                （防跟踪）；可用 allow_external_media=False 关闭

<richtext> 是 Kaleido 的伪标签且 [存在畸形闭合]（样例里写成开标签再出现一次
"<richtext>"），因此这里直接 [剥离标记]，由调用方在外层包一个容器 div —— 
比试图配平更稳，视觉等价。

本模块 [不依赖 Flask / 框架]，可独立单测。
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 白名单
# ---------------------------------------------------------------------------
#: 允许保留的标签
ALLOWED_TAGS = frozenset({
    "p", "div", "span", "br", "hr",
    "b", "strong", "i", "em", "u", "s", "strike", "del", "ins", "mark", "small",
    "sub", "sup", "code", "pre", "blockquote", "q", "cite", "abbr",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "colgroup", "col",
    "img", "figure", "figcaption",
    "audio", "video", "source", "track",
    "a",
})

#: 连同内容一起丢弃的标签（这些标签 [有] 闭合标签）
DROP_WITH_CONTENT = frozenset({
    "script", "style", "iframe", "object", "template", "svg", "math",
    "noscript", "applet", "frameset", "form", "button", "select", "textarea",
})

#: 只丢弃标签本身、[不得] 进入"丢弃块"模式的危险标签。
#: 关键：embed / input / link / meta 等是 void 标签，没有闭合标签，
#: 若进入 skip 模式将永不退出，把后续正文整段吞掉（曾把「安全」二字吃掉）。
DROP_VOID = frozenset({
    "embed", "input", "link", "meta", "base", "param", "area", "wbr", "frame",
})

#: 自闭合（void）标签
VOID_TAGS = frozenset({"br", "hr", "img", "source", "track", "col"})

#: 标签 → 允许的属性
ALLOWED_ATTRS: Dict[str, frozenset] = {
    "*": frozenset({"class", "title", "lang", "dir"}),
    "img": frozenset({"src", "alt", "width", "height", "data-img-type", "loading",
                      "referrerpolicy", "decoding"}),
    "audio": frozenset({"src", "controls", "loop", "muted", "preload"}),
    "video": frozenset({"src", "controls", "loop", "muted", "preload", "poster",
                        "width", "height"}),
    "source": frozenset({"src", "type"}),
    "track": frozenset({"src", "kind", "srclang", "label", "default"}),
    "a": frozenset({"href", "rel", "target"}),
    "td": frozenset({"colspan", "rowspan", "align"}),
    "th": frozenset({"colspan", "rowspan", "align", "scope"}),
    "ol": frozenset({"start", "reversed", "type"}),
    "col": frozenset({"span"}),
    "colgroup": frozenset({"span"}),
}

#: class 白名单（其余 class 一律丢弃，避免与框架样式冲突或被用于隐藏元素）
ALLOWED_CLASSES = frozenset({
    "img_container", "kqb-rt", "kqb-media", "kqb-media-missing", "kqb-math",
})

#: 单字段最大长度（防 DoS；超出则退化为截断纯文本）
MAX_FIELD_CHARS = 2_000_000

#: 危险块预清理（HTMLParser 会把 script 内容当文本吐出来，先整体删掉）
_DANGEROUS_BLOCK_RE = re.compile(
    r"<\s*(script|style|iframe|object|embed|noscript|template|svg|math|applet)"
    r"\b[^>]*>.*?<\s*/\s*\1\s*>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_RICHTEXT_OPEN_RE = re.compile(r"<\s*richtext\s*>", re.IGNORECASE)
_RICHTEXT_CLOSE_RE = re.compile(r"<\s*/\s*richtext\s*>", re.IGNORECASE)

#: 本模块自用的媒体端点占位（由调用方提供真实前缀）
MediaResolver = Callable[[str], Optional[str]]


# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------
class RenderedText(dict):
    """渲染结果（dict 便于直接塞进模板上下文 / JSON 响应）。

    keys: html, is_html, media_refs, missing_media, external_media, sanitized_out
    """

    @property
    def html(self) -> str:
        return self.get("html", "")

    @property
    def missing_media(self) -> List[str]:
        return self.get("missing_media") or []


def _new_result() -> RenderedText:
    return RenderedText({
        "html": "",
        "is_html": False,
        "media_refs": [],
        "missing_media": [],
        "external_media": [],
        "sanitized_out": 0,
    })


# ---------------------------------------------------------------------------
# URL 判定
# ---------------------------------------------------------------------------
_DATA_URL_PREFIXES = ("data:image/", "data:audio/", "data:video/")

def _classify_url(url: str, *, allow_external: bool) -> str:
    """把 URL 归类为：data / media / external / same-origin / unsafe。"""
    raw = (url or "").strip()
    if not raw:
        return "unsafe"
    low = raw.lower()
    if low.startswith(("javascript:", "vbscript:", "blob:", "file:")):
        return "unsafe"
    if low.startswith("data:"):
        return "data" if low.startswith(_DATA_URL_PREFIXES) else "unsafe"
    if low.startswith("/media/"):
        return "media"
    if low.startswith(("http://", "https://")) or low.startswith("//"):
        return "external" if allow_external else "unsafe"
    if raw.startswith("/"):
        return "same-origin"
    # 无协议、无前导斜杠：可能是裸文件名（早期提取产物常见）
    if "/" not in raw and ":" not in raw:
        return "media"
    return "unsafe"


def media_ref_from_url(url: str) -> Optional[str]:
    """从媒体 URL 里取出引用名（/media/<name> 或 裸文件名）。"""
    raw = (url or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("/media/"):
        rest = raw[len("/media/"):]
    elif "/" not in raw and ":" not in raw and not raw.lower().startswith("data:"):
        rest = raw
    else:
        return None
    rest = rest.split("?")[0].split("#")[0].strip()
    if not rest or "/" in rest or "\\" in rest:
        return None
    return rest


# ---------------------------------------------------------------------------
# 解析器
# ---------------------------------------------------------------------------
#: 含「白名单内标签」的判定 —— 用于在 html_content 标记缺失/不可信时自动按 HTML 处理。
#: 刻意 [只认白名单标签]：若内容里只有 <script> 这类危险标签，
#: 则走纯文本路径整段转义（原样显示），既不执行也不静默吞掉内容。
_ALLOWED_TAG_RE = re.compile(
    r"</?(" + "|".join(sorted(ALLOWED_TAGS)) + r")\b", re.IGNORECASE
)


class _Sanitizer(HTMLParser):
    def __init__(self, *, resolve_media: Optional[MediaResolver],
                 allow_external_media: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.resolve_media = resolve_media
        self.allow_external_media = allow_external_media
        self.out: List[str] = []
        self.stack: List[str] = []
        self.skip_depth = 0          # >0 表示正处于被整体丢弃的块内
        self.skip_tag: Optional[str] = None
        self.result = _new_result()
        self.result["is_html"] = True

    # -------------------------------------------------- 内部：属性处理
    def _filter_attrs(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> Dict[str, str]:
        allowed = set(ALLOWED_ATTRS.get("*", ())) | set(ALLOWED_ATTRS.get(tag, ()))
        out: Dict[str, str] = {}
        for raw_name, raw_value in attrs:
            name = (raw_name or "").lower()
            if name not in allowed:
                continue
            value = "" if raw_value is None else str(raw_value)
            if name == "class":
                kept = [
                    c for c in value.split()
                    if c in ALLOWED_CLASSES
                ]
                if kept:
                    out["class"] = " ".join(kept)
                continue
            out[name] = value
        return out

    def _handle_src(self, tag: str, attrs: Dict[str, str]) -> Tuple[bool, str]:
        """处理 src（以及 video 的 poster）。返回 (是否保留该标签, 占位 html)。"""
        # ---- 先净化 poster（容易被忽略的注入面）----
        if tag == "video" and "poster" in attrs:
            poster = attrs.pop("poster")
            pkind = _classify_url(poster, allow_external=self.allow_external_media)
            if pkind == "media":
                ref = media_ref_from_url(poster)
                resolved = self.resolve_media(ref) if (ref and self.resolve_media) else None
                if resolved:
                    attrs["poster"] = resolved
            elif pkind in ("external", "same-origin"):
                attrs["poster"] = poster.strip()
            # data / unsafe → 直接丢弃 poster

        url = attrs.get("src", "")
        kind = _classify_url(url, allow_external=self.allow_external_media)
        #: 只有图片在缺失时给占位；音视频/信源整体丢弃更安全（避免非法嵌套）
        emit_placeholder = tag == "img"

        if kind == "data":
            attrs["src"] = url.strip()
            if tag == "img":
                attrs.setdefault("loading", "lazy")
            return True, ""

        if kind == "media":
            ref = media_ref_from_url(url)
            if not ref:
                return False, self._placeholder(f"无效媒体引用 {url!r}")
            if ref not in self.result["media_refs"]:
                self.result["media_refs"].append(ref)
            resolved = self.resolve_media(ref) if self.resolve_media else None
            if not resolved:
                if ref not in self.result["missing_media"]:
                    self.result["missing_media"].append(ref)
                return False, (self._placeholder(f"缺失媒体 {ref}") if emit_placeholder else "")
            attrs["src"] = resolved
            attrs.pop("data-img-type", None)
            if tag == "img":
                attrs.setdefault("loading", "lazy")
            return True, ""

        if kind == "external":
            if url not in self.result["external_media"]:
                self.result["external_media"].append(url)
            attrs["src"] = url.strip()
            if tag == "img":
                # 防跟踪：不发送 Referer
                attrs["referrerpolicy"] = "no-referrer"
                attrs.setdefault("loading", "lazy")
            return True, ""

        if kind == "same-origin":
            attrs["src"] = url.strip()
            return True, ""

        return False, (self._placeholder("已拦截不安全媒体地址") if emit_placeholder else "")

    @staticmethod
    def _placeholder(reason: str) -> str:
        return f'<span class="kqb-media-missing">{escape(reason)}</span>'

    # -------------------------------------------------- HTMLParser 回调
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag in DROP_VOID:
            # void 型危险标签：只丢标签本身，不进入 skip 模式
            self.result["sanitized_out"] += 1
            return
        if tag in DROP_WITH_CONTENT:
            self.skip_depth = 1
            self.skip_tag = tag
            self.result["sanitized_out"] += 1
            return
        if tag not in ALLOWED_TAGS:
            self.result["sanitized_out"] += 1
            return

        filtered = self._filter_attrs(tag, attrs)

        if tag in ("img", "audio", "video", "source", "track"):
            keep, placeholder = self._handle_src(tag, filtered)
            if not keep:
                if placeholder:
                    self.out.append(placeholder)
                return

        if tag == "a":
            href = filtered.get("href", "")
            kind = _classify_url(href, allow_external=self.allow_external_media)
            if kind in ("unsafe",):
                filtered.pop("href", None)
            else:
                kind2 = "external" if kind == "external" else kind
                if kind2 == "external":
                    filtered["rel"] = "noopener noreferrer"
                    filtered.setdefault("target", "_blank")

        rendered = "".join(
            f' {name}="{escape(value, quote=True)}"'
            for name, value in filtered.items()
        )
        if tag in VOID_TAGS:
            self.out.append(f"<{tag}{rendered}>")
        else:
            self.out.append(f"<{tag}{rendered}>")
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        """自闭合写法 `<x/>`：危险标签整体丢弃，允许标签成对输出。"""
        tag = tag.lower()
        if self.skip_depth:
            return
        if tag in DROP_WITH_CONTENT or tag in DROP_VOID:
            self.result["sanitized_out"] += 1
            return
        self.handle_starttag(tag, attrs)
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS \
                and self.stack and self.stack[-1] == tag:
            self.stack.pop()
            self.out.append(f"</{tag}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth -= 1
            return
        if tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        if tag in self.stack:
            # 弹到最近的同名标签，途中未闭合的自动补上
            while self.stack:
                open_tag = self.stack.pop()
                self.out.append(f"</{open_tag}>")
                if open_tag == tag:
                    break

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if data:
            self.out.append(escape(data, quote=False))

    def close(self) -> None:
        super().close()
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")

    def html(self) -> str:
        return "".join(self.out)


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def _preclean(text: str) -> str:
    """去掉危险块、注释与 <richtext> 伪标签标记。"""
    cleaned = _DANGEROUS_BLOCK_RE.sub("", text)
    cleaned = _COMMENT_RE.sub("", cleaned)
    cleaned = _RICHTEXT_CLOSE_RE.sub("", cleaned)
    cleaned = _RICHTEXT_OPEN_RE.sub("", cleaned)
    return cleaned


def render_rich_text(text: Any, *, is_html: bool = False,
                     resolve_media: Optional[MediaResolver] = None,
                     allow_external_media: bool = True,
                     wrap: bool = True) -> RenderedText:
    """渲染题库富文本/纯文本为安全 HTML。

    :param text: 原始内容
    :param is_html: 是否按 HTML 处理（False 则转义并保留换行）
    :param resolve_media: 媒体引用 → URL 的解析函数；返回 None 表示缺失
    :param allow_external_media: 是否允许外链媒体（默认允许，但加 no-referrer 防跟踪）
    :param wrap: 是否用 <div class="kqb-rt"> 包裹
    """
    if text is None:
        result = _new_result()
        return result
    raw = text if isinstance(text, str) else str(text)
    if not raw.strip():
        result = _new_result()
        result["is_html"] = bool(is_html)
        return result

    truncated = False
    if len(raw) > MAX_FIELD_CHARS:
        raw = raw[:MAX_FIELD_CHARS]
        truncated = True

    # 是否按 HTML 处理：显式标记优先；标记为 False 时，只有出现 [白名单内标签]
    # 才自动转为 HTML（题库里 html_content 标记常有失真，但仅含危险标签的内容
    # 应走纯文本转义路径，避免把正文当标签块删掉）。
    looks_html = bool(_ALLOWED_TAG_RE.search(raw))
    effective_html = bool(is_html) or looks_html

    result = _new_result()
    if not effective_html:
        html = escape(raw, quote=False).replace("\n", "<br>")
        result["html"] = f'<div class="kqb-rt">{html}</div>' if wrap else html
        result["is_html"] = False
        if truncated:
            result["truncated"] = True
        return result

    parser = _Sanitizer(resolve_media=resolve_media,
                        allow_external_media=allow_external_media)
    try:
        parser.feed(_preclean(raw))
        parser.close()
    except Exception:  # 解析器异常也要给出可用结果，绝不把原始 HTML 直出
        result["html"] = escape(raw[:10000], quote=False)
        result["parse_error"] = True
        return result

    body = parser.html()
    result.update(parser.result)
    if wrap:
        body = f'<div class="kqb-rt">{body}</div>'
    result["html"] = body
    result["is_html"] = True
    if truncated:
        result["truncated"] = True
    return result


def render_plain(text: Any) -> RenderedText:
    """只做纯文本渲染的便捷入口（选项等短文本用）。"""
    return render_rich_text(text, is_html=False, wrap=False)


def sanitize_html(text: Any, *, allow_external_media: bool = True) -> str:
    """仅净化，不做媒体重写（媒体引用会因无 resolver 被判为缺失占位）。"""
    return render_rich_text(
        text, is_html=True, resolve_media=None,
        allow_external_media=allow_external_media,
    )["html"]


_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def plain_text(text: Any) -> str:
    """富文本 → 单行纯文本（供 AI 评卷等只需要文字的场景）。

    只做去标签 + 反转义 + 空白压缩；不执行任何净化（不输出到 HTML 上下文）。
    """
    if text is None:
        return ""
    raw = str(text)
    if not raw.strip():
        return ""
    raw = _RICHTEXT_OPEN_RE.sub(" ", raw)
    raw = _RICHTEXT_CLOSE_RE.sub(" ", raw)
    raw = _TAG_STRIP_RE.sub(" ", raw)
    from html import unescape as _unescape

    return re.sub(r"\s+", " ", _unescape(raw)).strip()


__all__ = [
    "ALLOWED_TAGS", "ALLOWED_ATTRS", "ALLOWED_CLASSES",
    "MediaResolver", "RenderedText",
    "render_rich_text", "render_plain", "sanitize_html",
    "media_ref_from_url", "plain_text",
]
