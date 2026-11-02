# -*- coding: utf-8 -*-
"""km_export —— 错题汇总导出

================================================================================
产物形态（按用户确认的口径）：
  1. **[主产物] v3.1.0 题库**
     - 无相对媒体引用 → 单个 `.json`
     - 含 `/media/<name>` 相对引用 → 必须打成 `.kqb` 整包（媒体随包走）
  2. [附加] Markdown 错题本 / 自包含 HTML（便于阅读与打印）

关键实现决定：
  * **用原始对象重建，而不是从归一化结果反推**。
    每个 PracticeItem 携带 `raw`（叶子题/子题的原始 dict）与 `parent_raw`（复合题父题），
    导出时按原始顺序与结构复制，只做字段补全，因此 `options` 顺序、`html_content`
    标记、题型名等全部保真 —— 导出的题库能原样再导入 Mini 或完整版。
  * 复合题（A3/A4/B）只挑**选中的子题**，父题（共用题干/共用选项）整体保留。
  * 导出题库是**新题库**：新 uuid；若配置了密钥则重新签名 `valid_key`（否则留空）。
    这解决了完整版"解密后明文验签必失败"的历史包袱。
  * 媒体只收集**导出题目实际引用到的**那些，不是整库媒体。

本模块 [不依赖 Flask / 框架]，可独立单测。
"""

from __future__ import annotations

import base64
import copy
import html as _html
import re
import uuid as _uuid
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import km_parser as KP
from . import km_render as KR

#: 导出的题库格式版本（用户口径：v3.1.0）
EXPORT_FORMAT_VERSION: Tuple[int, int, int] = (3, 1, 0)

#: HTML 内联媒体的单文件 / 总量上限（超出则退化为链接）
INLINE_MAX_FILE_BYTES = 2 * 1024 * 1024
INLINE_MAX_TOTAL_BYTES = 24 * 1024 * 1024

#: 媒体引用（相对形式）
_MEDIA_REF_RE = re.compile(r"/media/([^\"'\s<>?#\\]+)")

MEDIA_LOADER = Callable[[str], Optional[bytes]]


class ExportError(ValueError):
    """导出失败（无题目、无媒体来源等）。"""


# ---------------------------------------------------------------------------
# 1) 构建导出的 v3.1.0 题库（用原始对象重建）
# ---------------------------------------------------------------------------
def _deep(obj: Any) -> Any:
    return copy.deepcopy(obj) if isinstance(obj, (dict, list)) else obj


def _ensure_fields(question: Dict[str, Any], item: KP.PracticeItem,
                   *, is_sub: bool = False) -> Dict[str, Any]:
    """补全 v3.1.0 必需字段（不覆盖已有值）。"""
    out = _deep(question)
    if is_sub:
        out.setdefault("sub_question_id", item.sub_index)
    else:
        out.setdefault("question_id", item.group_id.rsplit("q", 1)[-1]
                       if "q" in item.group_id else 1)
    out.setdefault("question_type", item.qtype)
    out.setdefault("skippable", item.skippable)
    out.setdefault("html_content", bool(item.html))
    out.setdefault("question", item.stem)
    if not is_sub or "options" in question:
        out.setdefault("options", dict(item.options))
    elif item.options and "options" not in out:
        out["options"] = dict(item.options)
    out.setdefault("correct_answers", list(item.correct))
    out.setdefault("analysis", item.analysis)
    return out


def build_export_bank(parsed: KP.ParsedBank,
                      selected_ids: Iterable[str],
                      *,
                      title: str = "",
                      author: str = "",
                      description: str = "",
                      version: Sequence[int] = EXPORT_FORMAT_VERSION,
                      uuid_value: Optional[str] = None,
                      signer: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
                      chapter_note: str = "",
                      extra_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按选中的 item_id 重建一份 v3.1.0 题库。

    :param signer: 可选回调，接收题库 dict 返回签名后的 dict（用于生成 valid_key）
    :param chapter_note: 追加到章节名的后缀（如「（错题）」），空则不追加
    :param extra_meta: 额外写到顶层的自定义字段（完整版会忽略未知字段）
    """
    wanted = {str(i) for i in selected_ids}
    if not wanted:
        raise ExportError("没有选中任何题目")

    chapters_out: List[Dict[str, Any]] = []
    exported_ids: List[str] = []

    for chapter in parsed.chapters:
        chapter_items = [i for i in chapter.items if i.item_id in wanted]
        if not chapter_items:
            continue

        # 复合题：父题 id(object) -> 选中的子题集合
        parent_to_selected: Dict[int, set] = {}
        for item in chapter_items:
            if item.parent_raw:
                parent_to_selected.setdefault(id(item.parent_raw), set()).add(id(item.raw))

        emitted_parents: set = set()
        content_out: List[Dict[str, Any]] = []

        for item in chapter.items:              # 保持 [原始顺序]
            if item.item_id not in wanted:
                continue
            if item.parent_raw:
                pid = id(item.parent_raw)
                if pid in emitted_parents:
                    continue
                emitted_parents.add(pid)
                selected_subs = parent_to_selected.get(pid, set())
                sub_items = [
                    s for s in chapter.items
                    if s.parent_raw is item.parent_raw and id(s.raw) in selected_subs
                ]
                parent_out = {
                    k: _deep(v) for k, v in item.parent_raw.items() if k != "content"
                }
                parent_out.setdefault("question_type", item.parent_raw.get("question_type", "A3"))
                parent_out.setdefault("skippable", item.skippable)
                parent_out.setdefault("html_content", bool(item.html))
                parent_out["content"] = [
                    _ensure_fields(s.raw, s, is_sub=True) for s in sub_items
                ]
                content_out.append(parent_out)
                exported_ids.extend(s.item_id for s in sub_items)
            else:
                content_out.append(_ensure_fields(item.raw, item, is_sub=False))
                exported_ids.append(item.item_id)

        if not content_out:
            continue
        name = chapter.chapter_name + (chapter_note or "")
        chapters_out.append({
            "chapter_id": chapter.chapter_id,
            "chapter_name": name,
            "content": content_out,
        })

    if not chapters_out:
        raise ExportError("选中的题目无法重建（原始结构缺失）")

    bank: Dict[str, Any] = {
        "format_version": [int(v) for v in version],
        "title": title or (parsed.title or "错题汇总"),
        "author": author or (parsed.author or ""),
        "description": description or "",
        "uuid": uuid_value or str(_uuid.uuid4()),
        "valid_key": "",
        "chapters": chapters_out,
    }
    if parsed.format_version and tuple(parsed.format_version) > tuple(version):
        bank["source_format_version"] = list(parsed.format_version)
    for key, value in (extra_meta or {}).items():
        bank.setdefault(key, value)

    if signer is not None:
        bank = signer(bank)
    return bank


# ---------------------------------------------------------------------------
# 2) 媒体收集
# ---------------------------------------------------------------------------
def collect_media_refs(bank: Dict[str, Any]) -> List[str]:
    """扫描导出题库里所有 /media/<name> 引用（去重保序）。"""
    text = _flatten_strings(bank)
    seen: List[str] = []
    for name in _MEDIA_REF_RE.findall(text):
        if name not in seen:
            seen.append(name)
    return seen


def _flatten_strings(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return " ".join(_flatten_strings(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return " ".join(_flatten_strings(v) for v in obj)
    return ""


def collect_media(refs: Iterable[str], loader: MEDIA_LOADER) -> Tuple[Dict[str, bytes], List[str]]:
    """按引用列表取媒体字节。返回 (可用媒体, 缺失引用)。"""
    media: Dict[str, bytes] = {}
    missing: List[str] = []
    for ref in refs:
        blob = None
        try:
            blob = loader(ref)
        except Exception:
            blob = None
        if blob is None:
            missing.append(ref)
        else:
            media[ref] = blob
    return media, missing


# ---------------------------------------------------------------------------
# 3) Markdown 错题本
# ---------------------------------------------------------------------------
def _plain(text: str) -> str:
    """把富文本压成单行纯文本（Markdown 表格/标题里用）。"""
    if not text:
        return ""
    out = re.sub(r"<[^>]+>", "", str(text))
    out = _html.unescape(out)
    return re.sub(r"\s+", " ", out).strip()


def build_markdown(parsed: KP.ParsedBank,
                   selected_ids: Iterable[str],
                   records: Optional[Dict[str, Dict[str, Any]]] = None,
                   *,
                   title: str = "",
                   media_url: Optional[Callable[[str], str]] = None) -> str:
    """生成 Markdown 错题本（媒体以 /media/<name> 链接表示）。"""
    wanted = [str(i) for i in selected_ids]
    records = records or {}
    items_by_id = {i.item_id: i for i in parsed.items}
    missing = [i for i in wanted if i not in items_by_id]
    chosen = [items_by_id[i] for i in wanted if i in items_by_id]
    if not chosen:
        raise ExportError("没有可导出的题目")

    def media_resolve(ref: str) -> str:
        return (media_url or (lambda r: f"/media/{r}"))(ref)

    def body(text: str, is_html: bool) -> str:
        r = KR.render_rich_text(text, is_html=is_html, resolve_media=media_resolve,
                                allow_external_media=True, wrap=False)
        out = r["html"]
        # Markdown 里保留 HTML 片段是合法的；把图片单独提成 Markdown 语法更可读
        out = re.sub(r'<img[^>]*src="([^"]+)"[^>]*>', r"![](\1)", out)
        out = re.sub(r"</?(div|p|span|b|strong|i|em|u|br)[^>]*>", "", out)
        return out.strip()

    lines: List[str] = []
    title = title or f"{parsed.title or '题库'}（错题汇总）"
    lines.append(f"# {title}")
    lines.append("")
    meta = [
        f"- 来源题库：{parsed.title or '未命名'}" + (f"（{parsed.author}）" if parsed.author else ""),
        f"- 原始格式：v{parsed.format_version_str()}",
        f"- 导出题目：{len(chosen)} 条",
    ]
    if missing:
        meta.append(f"- 未找到的题目：{len(missing)} 条")
    lines.extend(meta)
    lines.append("")

    current_chapter = None
    for index, item in enumerate(chosen, start=1):
        if item.chapter_name != current_chapter:
            current_chapter = item.chapter_name
            lines.append(f"## {current_chapter}")
            lines.append("")
        rec = records.get(item.item_id) or {}
        verdict = rec.get("verdict") or ""
        lines.append(f"### {index}. 〔{item.type_label}〕")
        lines.append("")
        if item.shared_stem:
            lines.append(f"> 共用题干：{body(item.shared_stem, item.html)}")
            lines.append("")
        lines.append(body(item.stem, item.html))
        lines.append("")
        for key, value in item.options.items():
            lines.append(f"- **{key}.** {body(value, item.html)}")
        if item.options:
            lines.append("")
        answer = " ".join(str(c) for c in item.correct)
        if item.display_type == KP.DISPLAY_JUDGE:
            answer = "正确" if (item.correct and item.correct[0]) else "错误"
        lines.append(f"- **正确答案**：{answer}")
        if verdict:
            label = {"correct": "答对", "wrong": "答错",
                     "manual": "待自评", "revealed": "看过答案"}.get(verdict, verdict)
            user = rec.get("answer")
            if isinstance(user, list):
                user = " ".join(str(x) for x in user)
            lines.append(f"- **我的作答**：{label}"
                         + (f"（{user}）" if user not in (None, "") else ""))
        if item.analysis:
            lines.append("")
            lines.append(f"**解析**：{body(item.analysis, item.html)}")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 4) 自包含 HTML 错题本
# ---------------------------------------------------------------------------
_HTML_STYLE = """
:root { color-scheme: light dark; }
body { margin: 0 auto; max-width: 860px; padding: 28px 20px 60px;
       font-family: system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
       font-size: 15px; line-height: 1.75; color: #1f2328; background: #fff; }
h1 { font-size: 22px; margin: 0 0 6px; }
h2 { font-size: 17px; margin: 28px 0 10px; border-left: 4px solid #2563eb;
     padding-left: 10px; }
.q { border: 1px solid #e3e6ea; border-radius: 10px; padding: 14px 16px; margin: 0 0 16px; }
.q-meta { font-size: 12px; color: #6b7280; margin: 0 0 8px; }
.stem { margin: 0 0 8px; }
.shared { border-left: 3px solid #2563eb; background: #f5f8ff;
          padding: 8px 12px; border-radius: 0 8px 8px 0; margin: 0 0 10px; font-size: 14px; }
ol.opts { list-style: none; padding: 0; margin: 0 0 10px; }
ol.opts li { padding: 3px 0; }
.ans { font-size: 14px; }
.ans b { color: #16a34a; }
.mine { color: #b45309; font-size: 14px; }
.analysis { margin-top: 10px; padding-top: 10px; border-top: 1px dashed #e3e6ea;
            font-size: 14px; color: #374151; }
img, video { max-width: 100%; height: auto; border-radius: 6px; }
table { border-collapse: collapse; }
td, th { border: 1px solid #e3e6ea; padding: 4px 8px; }
.miss { border: 1px dashed #d97706; color: #d97706; border-radius: 6px;
        padding: 1px 6px; font-size: 12px; }
@media print { body { max-width: none; padding: 0; } .q { break-inside: avoid; } }
"""


def build_html(parsed: KP.ParsedBank,
               selected_ids: Iterable[str],
               records: Optional[Dict[str, Dict[str, Any]]] = None,
               *,
               title: str = "",
               media_loader: Optional[MEDIA_LOADER] = None,
               inline_media: bool = True) -> str:
    """生成自包含 HTML 错题本（媒体尽量内联为 data URI，便于打印/分发）。"""
    wanted = [str(i) for i in selected_ids]
    records = records or {}
    items_by_id = {i.item_id: i for i in parsed.items}
    chosen = [items_by_id[i] for i in wanted if i in items_by_id]
    if not chosen:
        raise ExportError("没有可导出的题目")

    budget = {"total": 0}

    def data_uri(ref: str) -> Optional[str]:
        if not (inline_media and media_loader):
            return None
        try:
            blob = media_loader(ref)
        except Exception:
            return None
        if blob is None or len(blob) > INLINE_MAX_FILE_BYTES:
            return None
        if budget["total"] + len(blob) > INLINE_MAX_TOTAL_BYTES:
            return None
        budget["total"] += len(blob)
        mime = _guess_mime(ref)
        return f"data:{mime};base64," + base64.b64encode(blob).decode("ascii")

    def resolve(ref: str) -> Optional[str]:
        return data_uri(ref) or f"/media/{ref}"

    def body(text: str, is_html: bool) -> str:
        return KR.render_rich_text(text, is_html=is_html, resolve_media=resolve,
                                   allow_external_media=True, wrap=False)["html"]

    title = title or f"{parsed.title or '题库'}（错题汇总）"
    parts: List[str] = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN"><head><meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
        f"<title>{_html.escape(title)}</title>",
        f"<style>{_HTML_STYLE}</style>",
        "</head><body>",
        f"<h1>{_html.escape(title)}</h1>",
        '<p class="q-meta">'
        f"来源：{_html.escape(parsed.title or '未命名')}"
        + (f"（{_html.escape(parsed.author)}）" if parsed.author else "")
        + f" · 原始格式 v{parsed.format_version_str()} · 共 {len(chosen)} 条"
        "</p>",
    ]

    current_chapter = None
    for index, item in enumerate(chosen, start=1):
        if item.chapter_name != current_chapter:
            current_chapter = item.chapter_name
            parts.append(f"<h2>{_html.escape(current_chapter)}</h2>")
        rec = records.get(item.item_id) or {}
        verdict = rec.get("verdict") or ""
        meta = f"第 {index} 题 · {_html.escape(item.type_label)}"
        if item.group_size > 1:
            meta += f" · 共用题干组 {item.group_index}/{item.group_size}"
        parts.append('<div class="q">')
        parts.append(f'<p class="q-meta">{meta}</p>')
        if item.shared_stem:
            parts.append(f'<div class="shared">{body(item.shared_stem, item.html)}</div>')
        parts.append(f'<div class="stem">{body(item.stem, item.html)}</div>')
        if item.options:
            parts.append('<ol class="opts">')
            for key, value in item.options.items():
                parts.append(f"<li><b>{_html.escape(key)}.</b> {body(value, item.html)}</li>")
            parts.append("</ol>")
        answer = " ".join(str(c) for c in item.correct)
        if item.display_type == KP.DISPLAY_JUDGE:
            answer = "正确" if (item.correct and item.correct[0]) else "错误"
        parts.append(f'<p class="ans">正确答案：<b>{_html.escape(answer)}</b></p>')
        if verdict:
            label = {"correct": "答对", "wrong": "答错",
                     "manual": "待自评", "revealed": "看过答案"}.get(verdict, verdict)
            user = rec.get("answer")
            if isinstance(user, list):
                user = " ".join(str(x) for x in user)
            mine = label + (f"（{_html.escape(str(user))}）" if user not in (None, "") else "")
            parts.append(f'<p class="mine">我的作答：{mine}</p>')
        if item.analysis:
            parts.append(f'<div class="analysis"><b>解析：</b>{body(item.analysis, item.html)}</div>')
        parts.append("</div>")

    parts.append("</body></html>")
    return "\n".join(parts)


def _guess_mime(name: str) -> str:
    import mimetypes

    mime, _ = mimetypes.guess_type(name)
    return mime or "application/octet-stream"


# ---------------------------------------------------------------------------
# 5) 文件名与产物装配
# ---------------------------------------------------------------------------
def sanitize_filename_part(text: str, *, limit: int = 60) -> str:
    """把标题压成安全的文件名词干（保留中文，去掉非法字符）。"""
    if not text:
        return "错题汇总"
    out = []
    for ch in str(text):
        if ch in '/\\:*?"<>|' or ord(ch) < 32 or ord(ch) == 127:
            out.append("_")
        else:
            out.append(ch)
    cleaned = "".join(out).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return (cleaned[:limit] or "错题汇总")


def build_base_filename(parsed: KP.ParsedBank, *, suffix: str = "错题",
                        date_str: str = "") -> str:
    """`<题库标题>_<后缀>[_<日期>]` 形式的文件名词干。"""
    stem = sanitize_filename_part(parsed.title or "题库")
    parts = [stem, suffix] if suffix else [stem]
    if date_str:
        parts.append(date_str)
    return "_".join(p for p in parts if p)


__all__ = [
    "EXPORT_FORMAT_VERSION", "INLINE_MAX_FILE_BYTES", "INLINE_MAX_TOTAL_BYTES",
    "ExportError", "MEDIA_LOADER",
    "build_export_bank", "collect_media_refs", "collect_media",
    "build_markdown", "build_html",
    "sanitize_filename_part", "build_base_filename",
]
