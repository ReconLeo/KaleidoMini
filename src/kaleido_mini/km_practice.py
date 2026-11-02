# -*- coding: utf-8 -*-
"""km_practice —— 练习模式（自测）的数据装配与判分

================================================================================
把 ParsedBank 变成「练习页可直接用」的结构，并提供判分/揭晓接口。

三个刻意的设计决定：

1. **[练习载荷不含答案与解析]**。
   `build_payload()` 只输出题干/共用题干/选项的净化 HTML，**不输出 correct_answers
   与 analysis**。答案只在 `grade()` / `reveal()` 时按单题返回。
   好处：(a) 打开 DevTools 也翻不到答案，自测才可信；(b) 载荷体积大幅下降
   （大题库 626 条时约省一半流量，这对"整库一次性下发"很关键）。

2. **[判分只在服务端做一次]**。
   JS 端不重复实现"字母归一/判断布尔/填空全角半角"等规则 —— 那样必然与
   `km_parser.grade_item` 产生偏差。练习页每次提交只发一个题目 id + 答案，
   走后端判分，单一真源。

3. **[揭晓与判分是两个动作]**。
   `reveal=True` 表示"我只想看答案，不算作答"，返回 verdict="revealed"；
   否则按题型自动判分（essay 返回 verdict="manual" 交由用户自评）。

本模块 [不依赖 Flask / 框架]，可独立单测。
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Optional

from . import km_parser as KP
from . import km_render as KR

#: 判分结果里返回的 verdict 取值
VERDICT_CORRECT = "correct"
VERDICT_WRONG = "wrong"
VERDICT_MANUAL = "manual"       # 简答：需自评
VERDICT_REVEALED = "revealed"   # 只看答案，未作答

VERDICTS = (VERDICT_CORRECT, VERDICT_WRONG, VERDICT_MANUAL, VERDICT_REVEALED)


class PracticeError(ValueError):
    """练习相关错误（题目不存在、章节过滤无结果等）。"""


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------
def index_items(parsed: KP.ParsedBank) -> Dict[str, KP.PracticeItem]:
    """item_id → PracticeItem。"""
    return {item.item_id: item for item in parsed.items}


def chapter_choices(parsed: KP.ParsedBank) -> List[Dict[str, Any]]:
    """章节概览（供开始练习前的范围选择）。"""
    out: List[Dict[str, Any]] = []
    for chapter in parsed.chapters:
        out.append({
            "chapter_id": chapter.chapter_id,
            "chapter_name": chapter.chapter_name,
            "item_count": len(chapter.items),
            "type_counts": _count_types(chapter.items),
        })
    return out


def _count_types(items: Iterable[KP.PracticeItem]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        counts[item.qtype] = counts.get(item.qtype, 0) + 1
    return dict(sorted(counts.items()))


def select_items(parsed: KP.ParsedBank,
                 chapters: Optional[Iterable[int]] = None) -> List[KP.PracticeItem]:
    """按章节过滤取练习题。chapters 为 None/空 → 全部。"""
    if chapters:
        wanted = {int(c) for c in chapters}
        items = [i for i in parsed.items if i.chapter_id in wanted]
        if not items:
            raise PracticeError(
                f"所选章节 {sorted(wanted)} 下没有可练习题"
            )
        return items
    return list(parsed.items)


# ---------------------------------------------------------------------------
# 载荷装配
# ---------------------------------------------------------------------------
def _render(text: str, *, is_html: bool, resolve: Optional[KR.MediaResolver],
            allow_external: bool, wrap: bool) -> KR.RenderedText:
    return KR.render_rich_text(
        text, is_html=is_html, resolve_media=resolve,
        allow_external_media=allow_external, wrap=wrap,
    )


def _ordered_options(item: KP.PracticeItem, *, shuffle: bool,
                     rng_key: str) -> List[Dict[str, str]]:
    """产出 [显示顺序] 的选项表：每次展示都可能重标号为 A/B/C…

    - `key`       : 本次展示给用户的标签（A, B, …, Z, AA …）
    - `canonical` : 题库里的原始选项键（用于判分与导出）
    随机化只影响 [展示]，判分与导出始终用 canonical —— 因此不会污染题库与错题本。
    """
    canonical = KP.sort_option_keys(item.options.keys())
    if shuffle and len(canonical) > 1:
        import random

        rng = random.Random(rng_key)
        canonical = list(canonical)
        rng.shuffle(canonical)
    return [
        {"key": KP.excel_key(pos + 1), "canonical": ck}
        for pos, ck in enumerate(canonical)
    ]


def build_item_payload(item: KP.PracticeItem, *,
                       resolve: Optional[KR.MediaResolver] = None,
                       allow_external: bool = True,
                       shuffle_options: bool = False,
                       shuffle_seed: str = "") -> Dict[str, Any]:
    """单个练习条目 → 前端结构（**不含答案与解析**）。"""
    stem = _render(item.stem, is_html=item.html, resolve=resolve,
                   allow_external=allow_external, wrap=True)
    shared = None
    if item.shared_stem:
        shared = _render(item.shared_stem, is_html=item.html, resolve=resolve,
                         allow_external=allow_external, wrap=True)

    options: List[Dict[str, str]] = []
    missing_media = list(stem["missing_media"])
    if item.options:
        ordered = _ordered_options(
            item, shuffle=shuffle_options,
            rng_key=f"{shuffle_seed}:{item.item_id}",
        )
        for entry in ordered:
            rendered = _render(item.options[entry["canonical"]], is_html=item.html,
                               resolve=resolve, allow_external=allow_external,
                               wrap=False)
            entry["html"] = rendered["html"]
            options.append(entry)
            missing_media.extend(rendered["missing_media"])
    if shared:
        missing_media.extend(shared["missing_media"])

    return {
        "item_id": item.item_id,
        "chapter_id": item.chapter_id,
        "chapter_name": item.chapter_name,
        "qtype": item.qtype,
        "type_label": item.type_label,
        "display_type": item.display_type,
        "skippable": item.skippable,
        "is_html": item.html,
        "stem_html": stem["html"],
        "shared_stem_html": shared["html"] if shared else "",
        #: 展示顺序的选项表；key=展示标签，canonical=题库原键
        "options": options,
        "option_count": len(options),
        "group_id": item.group_id,
        "group_index": item.group_index,
        "group_size": item.group_size,
        "missing_media": _dedupe(missing_media),
        "warnings": list(item.warnings),
    }


def build_payload(parsed: KP.ParsedBank, *,
                  subject: str = "",
                  filename: str = "",
                  chapters: Optional[Iterable[int]] = None,
                  resolve: Optional[KR.MediaResolver] = None,
                  allow_external: bool = True,
                  max_items: int = 0,
                  shuffle_options: bool = False,
                  shuffle_seed: str = "") -> Dict[str, Any]:
    """练习页载荷。

    :param max_items: >0 时限制下发条目数（超大题库的兜底，防前端卡死）
    :param shuffle_options: 是否随机化选项展示顺序（只影响展示，不影响判分/导出）
    :param shuffle_seed: 随机化种子；空则本次载荷内随机生成一次（同一次载荷内自洽）
    """
    items = select_items(parsed, chapters)
    if shuffle_options and not shuffle_seed:
        import secrets

        shuffle_seed = secrets.token_hex(8)

    payload_items: List[Dict[str, Any]] = []
    for item in items:
        if max_items and len(payload_items) >= max_items:
            break
        payload_items.append(build_item_payload(
            item, resolve=resolve, allow_external=allow_external,
            shuffle_options=shuffle_options, shuffle_seed=shuffle_seed))

    missing_total = 0
    for entry in payload_items:
        missing_total += len(entry["missing_media"])

    return {
        "bank": {
            "subject": subject,
            "filename": filename,
            "title": parsed.title,
            "author": parsed.author,
            "format_version": list(parsed.format_version),
            "format_version_str": parsed.format_version_str(),
            "uuid": parsed.uuid,
            "item_count": parsed.item_count,
            "chapter_count": parsed.chapter_count,
        },
        "chapters": chapter_choices(parsed),
        "selected_chapter_ids": sorted({i.chapter_id for i in items}),
        "items": payload_items,
        "item_count": len(payload_items),
        "truncated": bool(max_items and len(items) > len(payload_items)),
        "total_matching": len(items),
        "total_items": parsed.item_count,
        "media_missing_total": missing_total,
        "type_counts": _count_types(items),
        "allow_external_media": allow_external,
        "shuffle_options": bool(shuffle_options),
    }


def _dedupe(values: Iterable[str]) -> List[str]:
    seen: List[str] = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return seen


# ---------------------------------------------------------------------------
# 揭晓 / 判分
# ---------------------------------------------------------------------------
def _format_answer(item: KP.PracticeItem, answers: List[Any]) -> str:
    """把归一化答案变成人可读的展示串。"""
    if not answers:
        return "（无）"
    if item.display_type == KP.DISPLAY_JUDGE:
        return "正确" if bool(answers[0]) else "错误"
    if item.display_type in (KP.DISPLAY_SINGLE, KP.DISPLAY_MULTIPLE):
        return " ".join(str(a) for a in answers)
    return " ｜ ".join(str(a) for a in answers)


def grade_item_payload(parsed: KP.ParsedBank, item_id: str, answer: Any = None,
                       *,
                       reveal: bool = False,
                       resolve: Optional[KR.MediaResolver] = None,
                       allow_external: bool = True) -> Dict[str, Any]:
    """判分或揭晓单题，返回含答案与解析的结构。

    :param reveal: True 表示"只看答案"，不判分
    :raises PracticeError: item_id 不存在
    """
    item = index_items(parsed).get(item_id)
    if item is None:
        raise PracticeError(f"题目不存在：{item_id}")

    analysis = _render(item.analysis, is_html=item.html, resolve=resolve,
                       allow_external=allow_external, wrap=True)
    shared = _render(item.shared_stem, is_html=item.html, resolve=resolve,
                     allow_external=allow_external, wrap=True) \
        if item.shared_stem else None

    result: Dict[str, Any] = {
        "item_id": item_id,
        "display_type": item.display_type,
        "type_label": item.type_label,
        "correct": copy.deepcopy(item.correct),
        "correct_display": _format_answer(item, item.correct),
        "analysis_html": analysis["html"],
        "missing_media": _dedupe(
            list(analysis["missing_media"])
            + (list(shared["missing_media"]) if shared else [])
        ),
        "verdict": VERDICT_REVEALED,
        "detail": "",
        "user_display": "",
    }

    if reveal:
        return result

    graded = KP.grade_item(item, answer)
    verdict = graded.get("verdict", VERDICT_WRONG)
    if verdict not in (VERDICT_CORRECT, VERDICT_WRONG, VERDICT_MANUAL):
        verdict = VERDICT_WRONG
    result["verdict"] = verdict
    result["detail"] = graded.get("detail", "")
    normalized = graded.get("normalized")
    #: 规范键形式（客户端据此映射成当前展示标签）
    result["user_canonical"] = normalized
    result["user_display"] = _format_user_answer(item, normalized)
    return result


def _format_user_answer(item: KP.PracticeItem, normalized: Any) -> str:
    if normalized is None:
        return ""
    if isinstance(normalized, bool):
        return "正确" if normalized else "错误"
    if isinstance(normalized, (list, tuple)):
        if not normalized:
            return "（未作答）"
        if item.display_type == KP.DISPLAY_JUDGE:
            return "正确" if bool(normalized[0]) else "错误"
        if item.display_type in (KP.DISPLAY_SINGLE, KP.DISPLAY_MULTIPLE):
            return " ".join(str(x) for x in normalized)
        return " ｜ ".join(str(x) for x in normalized)
    return str(normalized)


# ---------------------------------------------------------------------------
# 汇总（结束练习时用；错题导出在 M4 复用这里的选择逻辑）
# ---------------------------------------------------------------------------
def summarize_session(payload: Dict[str, Any],
                      records: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """按前端回传的作答记录汇总战绩。

    :param records: {item_id: {"verdict": ..., "answer": ..., "elapsed_ms": ...}}
    """
    items = {i["item_id"]: i for i in payload.get("items", [])}
    total = len(items)
    counts = {VERDICT_CORRECT: 0, VERDICT_WRONG: 0,
              VERDICT_MANUAL: 0, VERDICT_REVEALED: 0}
    by_type: Dict[str, Dict[str, int]] = {}
    by_chapter: Dict[str, Dict[str, int]] = {}
    wrong_ids: List[str] = []

    for item_id, entry in (records or {}).items():
        if item_id not in items:
            continue
        verdict = (entry or {}).get("verdict") or VERDICT_WRONG
        if verdict not in counts:
            verdict = VERDICT_WRONG
        counts[verdict] += 1
        meta = items[item_id]
        qtype = meta.get("qtype", "?")
        chapter = meta.get("chapter_name", "?")
        for bucket, key in ((by_type, qtype), (by_chapter, chapter)):
            stat = bucket.setdefault(key, {VERDICT_CORRECT: 0, VERDICT_WRONG: 0,
                                           VERDICT_MANUAL: 0, VERDICT_REVEALED: 0})
            stat[verdict] += 1
        if verdict in (VERDICT_WRONG, VERDICT_REVEALED):
            wrong_ids.append(item_id)

    answered = sum(counts.values())
    auto_graded = counts[VERDICT_CORRECT] + counts[VERDICT_WRONG]
    return {
        "total": total,
        "answered": answered,
        "unanswered": max(total - answered, 0),
        "counts": counts,
        "auto_graded": auto_graded,
        "accuracy": (round(counts[VERDICT_CORRECT] / auto_graded, 4)
                     if auto_graded else None),
        "by_type": by_type,
        "by_chapter": by_chapter,
        "wrong_item_ids": wrong_ids,
    }


def build_wrong_export_view(payload: Dict[str, Any],
                            records: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """错题清单（供 M4 生成 v3.1.0 题库 / .kqb；此处只做选取与摘要）。"""
    items = {i["item_id"]: i for i in payload.get("items", [])}
    out: List[Dict[str, Any]] = []
    for item_id, entry in (records or {}).items():
        verdict = (entry or {}).get("verdict")
        if verdict not in (VERDICT_WRONG, VERDICT_REVEALED):
            continue
        meta = items.get(item_id)
        if not meta:
            continue
        out.append({
            "item_id": item_id,
            "chapter_id": meta.get("chapter_id"),
            "chapter_name": meta.get("chapter_name"),
            "qtype": meta.get("qtype"),
            "display_type": meta.get("display_type"),
            "verdict": verdict,
            "user_answer": (entry or {}).get("answer"),
            "elapsed_ms": (entry or {}).get("elapsed_ms"),
        })
    return out


__all__ = [
    "VERDICT_CORRECT", "VERDICT_WRONG", "VERDICT_MANUAL", "VERDICT_REVEALED",
    "VERDICTS", "PracticeError",
    "index_items", "chapter_choices", "select_items",
    "build_item_payload", "build_payload",
    "grade_item_payload", "summarize_session", "build_wrong_export_view",
]
