# -*- coding: utf-8 -*-
"""km_parser —— Kaleido 题库格式识别与归一键模

================================================================================
把 v1 / v2 / v3 / v3.1.0 四代题库统一归一化为 PracticeItem 列表，
供练习页与导出层消费。本模块 [只读]、[无副作用]、[不依赖 Flask/框架]。

支持的格式（依据 Kaleido/Project/standardization 与真实题库实测）：
  v1       无 format_version；单章；无 question_type（需推断）
  v2       format_version [2,0,0]；单章扁平；扁平题型名
  v3       format_version [3,0,0]；chapters[]；含 A1/A2/A3/A4/B/X/K/judge/gap_filling/essay
  v3.1.0   format_version [3,1,0]；同 v3，媒体带 data-img-type 语义（url/base64/filename）

三个来自实测的关键设计决定：
  1) [单选/多选一律按答案个数判定，不信任题型名]。
     实测证据：standardization/QuizBank_sample_v3.json 把 A2 标为「不定项选择题」且答案 3 个，
     而国考惯例 A2 是单选。按题型名渲染会做错控件，按答案基数渲染在两种数据上都正确。
  2) [html_content 标记不可全信]，题干含 HTML 标签时强制按 HTML 处理。
  3) 复合题（A3/A4/B）展开为多个可作答条目，同组用 group_id 串联，
     共用题干/共用选项在每条上重复携带，保证「逐题」练习时上下文完整。

复合题展开规则：
  A3 / A4  共用 shared_stem，子题各自 options
  B        共用 shared_options，子题通常无 options（从 shared_options 继承）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 展示类型（驱动前端控件形态）
DISPLAY_SINGLE = "single"      # 单选：radio
DISPLAY_MULTIPLE = "multiple"  # 多选：checkbox
DISPLAY_JUDGE = "judge"        # 判断：对/错
DISPLAY_GAP = "gap"            # 填空：input
DISPLAY_ESSAY = "essay"        # 简答：textarea（自评 / AI 评卷）

DISPLAY_TYPES = (
    DISPLAY_SINGLE,
    DISPLAY_MULTIPLE,
    DISPLAY_JUDGE,
    DISPLAY_GAP,
    DISPLAY_ESSAY,
)

#: 复合题（含子题）
COMPOSITE_TYPES = frozenset({"A3", "A4", "B"})

#: 选择题族（原始题型名）
CHOICE_TYPES = frozenset({
    "A1", "A2", "A3", "A4", "B", "K", "X",
    "single_choice", "multiple_choice",
})

JUDGE_TYPES = frozenset({"judge"})
GAP_TYPES = frozenset({"gap_filling"})
ESSAY_TYPES = frozenset({"essay"})

#: 题型展示名（中文标签，供模板直接用）
TYPE_LABELS: Dict[str, str] = {
    "A1": "A1 型题", "A2": "A2 型题", "A3": "A3 型题", "A4": "A4 型题",
    "B": "B 型题", "X": "X 型题（多选）", "K": "K 型题",
    "judge": "判断题", "gap_filling": "填空题", "essay": "简答题",
    "single_choice": "单选题", "multiple_choice": "多选题",
}

#: 判断题答案文本映射（对 → True）
_JUDGE_TRUE = re.compile(r"^(对|正确|是|真|true|t|yes|y|√|✓)$", re.IGNORECASE)
_JUDGE_FALSE = re.compile(r"^(错|错误|否|假|false|f|no|n|×|✗)$", re.IGNORECASE)

#: 题干中的填空标记（用于 v1 无题型时区分填空/简答）
_BLANK_RE = re.compile(r"(_{2,}|＿{2,}|（\s*）|\(\s*\)|【\s*】)")

#: 媒体相对引用：/media/<name>（3.1.0 filename 模式）
_MEDIA_REF_RE = re.compile(r"/media/([^\"'\s<>?#]+)")

#: 含 HTML 标签判定（用于强制 html 渲染）
_HTML_TAG_RE = re.compile(r"<[a-zA-Z][^>]*>")

#: 句子结束标点（用于判断答案是否像一句话 → 简答）
_SENTENCE_PUNCT = re.compile(r"[。！？；;!?]")

#: 简答判定阈值：答案长度超过该值且无填空标记 → 视为简答
_ESSAY_ANSWER_LEN = 12


class BankFormatError(ValueError):
    """题库结构非法，无法识别。"""


# ---------------------------------------------------------------------------
# 选项键（通用 26 进制：A…Z, AA…ZZ，对齐 Excel 列号）
# ---------------------------------------------------------------------------
#: 纯字母选项键
_OPTION_KEY_RE = re.compile(r"^[A-Z]+$")


def normalize_option_key(raw: Any) -> str:
    """规范化选项键：去空白、转大写。"""
    return re.sub(r"\s+", "", str(raw if raw is not None else "")).upper()


def option_key_index(key: Any) -> int:
    """选项键 → Excel 列号：A=1 … Z=26, AA=27 … ZZ=702。

    非「纯字母」键返回 -1（调用方应退化为原始顺序）。
    """
    text = normalize_option_key(key)
    if not text or not _OPTION_KEY_RE.match(text):
        return -1
    total = 0
    for ch in text:
        total = total * 26 + (ord(ch) - 64)
    return total


def excel_key(index: int) -> str:
    """Excel 列号 → 选项键：1→A, 26→Z, 27→AA（用于随机化后重新标号）。"""
    if index < 1:
        return ""
    out: List[str] = []
    n = int(index)
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out.append(chr(65 + rem))
    return "".join(reversed(out))


def sort_option_keys(keys: Iterable[str]) -> List[str]:
    """按选项语义顺序排序（A, B, …, Z, AA, …）；非字母键保持原有相对顺序垫后。"""
    ordered = list(keys)
    decorated = []
    for pos, key in enumerate(ordered):
        idx = option_key_index(key)
        if idx > 0:
            decorated.append(((0, idx, ""), key))
        else:
            decorated.append(((1, pos, str(key)), key))
    decorated.sort(key=lambda pair: pair[0])
    return [key for _, key in decorated]


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class PracticeItem:
    """一个可作答条目（复合题的一个子题也算一条）。"""

    item_id: str
    chapter_id: int
    chapter_name: str
    qtype: str
    display_type: str
    stem: str = ""
    options: Dict[str, str] = field(default_factory=dict)
    correct: List[Any] = field(default_factory=list)
    analysis: str = ""
    skippable: bool = True
    html: bool = False
    shared_stem: str = ""
    shared_options: Dict[str, str] = field(default_factory=dict)
    group_id: str = ""
    group_index: int = 1
    group_size: int = 1
    warnings: List[str] = field(default_factory=list)
    # ---- 原始对象引用（供导出精确重建；**绝不进入 to_dict / 练习载荷**）----
    #: 叶子题：题目本身的原始 dict；复合题子题：子题的原始 dict
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)
    #: 复合题子题：父题（含 shared_stem / shared_options）的原始 dict；叶子题为空
    parent_raw: Dict[str, Any] = field(default_factory=dict, repr=False)
    #: 在父题 content 中的 1-based 序号（复合题子题用）
    sub_index: int = 0

    @property
    def type_label(self) -> str:
        return TYPE_LABELS.get(self.qtype, self.qtype)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_id": self.item_id,
            "chapter_id": self.chapter_id,
            "chapter_name": self.chapter_name,
            "qtype": self.qtype,
            "type_label": self.type_label,
            "display_type": self.display_type,
            "stem": self.stem,
            "options": self.options,
            "correct": self.correct,
            "analysis": self.analysis,
            "skippable": self.skippable,
            "html": self.html,
            "shared_stem": self.shared_stem,
            "shared_options": self.shared_options,
            "group_id": self.group_id,
            "group_index": self.group_index,
            "group_size": self.group_size,
            "warnings": self.warnings,
        }


@dataclass
class Chapter:
    chapter_id: int
    chapter_name: str
    items: List[PracticeItem] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chapter_id": self.chapter_id,
            "chapter_name": self.chapter_name,
            "item_count": len(self.items),
        }


@dataclass
class ParsedBank:
    """归一化后的题库。"""

    format_version: Tuple[int, int, int]
    title: str = ""
    author: str = ""
    description: str = ""
    uuid: str = ""
    valid_key: str = ""
    chapters: List[Chapter] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # ---- 派生信息 ----
    @property
    def items(self) -> List[PracticeItem]:
        out: List[PracticeItem] = []
        for chapter in self.chapters:
            out.extend(chapter.items)
        return out

    @property
    def item_count(self) -> int:
        return sum(len(c.items) for c in self.chapters)

    @property
    def chapter_count(self) -> int:
        return len(self.chapters)

    @property
    def type_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in self.items:
            counts[item.qtype] = counts.get(item.qtype, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def display_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for item in self.items:
            counts[item.display_type] = counts.get(item.display_type, 0) + 1
        return dict(sorted(counts.items()))

    @property
    def media_refs(self) -> List[str]:
        """题库中引用到的媒体文件名（/media/<name>，去重保序）。"""
        seen: List[str] = []
        for item in self.items:
            for text in _item_texts(item):
                for name in _MEDIA_REF_RE.findall(text or ""):
                    if name not in seen:
                        seen.append(name)
        return seen

    @property
    def media_ref_count(self) -> int:
        return len(self.media_refs)

    @property
    def html_item_count(self) -> int:
        return sum(1 for i in self.items if i.html)

    def format_version_str(self) -> str:
        return ".".join(str(p) for p in self.format_version)

    def summary(self) -> Dict[str, Any]:
        return {
            "format_version": list(self.format_version),
            "format_version_str": self.format_version_str(),
            "title": self.title,
            "author": self.author,
            "description": self.description,
            "uuid": self.uuid,
            "chapter_count": self.chapter_count,
            "item_count": self.item_count,
            "type_counts": self.type_counts,
            "display_counts": self.display_counts,
            "html_item_count": self.html_item_count,
            "media_ref_count": self.media_ref_count,
            "media_refs": self.media_refs,
            "chapters": [c.to_dict() for c in self.chapters],
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# 格式识别
# ---------------------------------------------------------------------------
def detect_format_version(data: Dict[str, Any]) -> Tuple[int, int, int]:
    """识别题库格式版本，返回 (major, minor, patch)。

    * 存在 encrypted_content → 抛 BankFormatError（必须先解密，本模块不处理加密）
    * 无 format_version    → 视为 v1 (1,0,0)
    """
    if not isinstance(data, dict):
        raise BankFormatError("题库根节点必须是 JSON 对象")
    if "encrypted_content" in data:
        raise BankFormatError(
            "这是加密题库（含 encrypted_content），请先用密钥解密后再解析"
        )

    raw = data.get("format_version")
    if raw is None:
        return (1, 0, 0)

    parts: List[int] = []
    if isinstance(raw, (list, tuple)):
        seq: Sequence[Any] = raw
    elif isinstance(raw, str):
        seq = re.split(r"[.,\s]+", raw.strip())
    elif isinstance(raw, (int, float)):
        seq = [raw]
    else:
        raise BankFormatError(f"format_version 类型不支持：{type(raw).__name__}")

    for part in seq:
        if part is None or part == "":
            continue
        try:
            parts.append(int(str(part).strip()))
        except (TypeError, ValueError) as exc:
            raise BankFormatError(f"format_version 含非数字段：{part!r}") from exc
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def assert_supported(data: Dict[str, Any]) -> Tuple[int, int, int]:
    """识别版本并校验支持范围。"""
    version = detect_format_version(data)
    if version[0] not in (1, 2, 3):
        raise BankFormatError(
            f"不支持的题库主版本 {version[0]}（仅支持 v1 / v2 / v3 / v3.1.0）"
        )
    return version


# ---------------------------------------------------------------------------
# 答案归一化
# ---------------------------------------------------------------------------
def _as_answer_list(raw: Any) -> List[Any]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return [raw]


def _normalize_judge(answers: List[Any], options: Dict[str, str]) -> List[bool]:
    """把判断题答案归一为 [True] / [False]。"""
    if not answers:
        return []
    first = answers[0]
    if isinstance(first, bool):
        return [first]
    text = str(first).strip()
    if _JUDGE_TRUE.match(text):
        return [True]
    if _JUDGE_FALSE.match(text):
        return [False]
    # 答案给的是选项字母 → 用选项文本判断
    letter = text.upper()
    if letter in options:
        label = str(options[letter]).strip()
        if _JUDGE_TRUE.match(label):
            return [True]
        if _JUDGE_FALSE.match(label):
            return [False]
    return []


def _normalize_choice(answers: List[Any],
                      option_keys: Optional[Sequence[str]] = None) -> List[str]:
    """把选择题答案归一为 [按选项顺序排序] 的键列表。

    选项键 **不限于 A–E**：支持 A…Z, AA…ZZ（Excel 列式 26 进制）。
    因此不能简单地 re.findall(r"[A-Z]") —— 那会把 "AA" 拆成两个 "A"。
    策略：先用整串匹配选项键（"AA" 命中选项 "AA" 就整体采用），
    匹配不到才退化为逐字母拆分（兼容 "A,B,D"、"ABD" 这类写法），
    且拆出的字母必须真实存在于选项里。
    """
    keys = {normalize_option_key(k) for k in (option_keys or [])}
    keys.discard("")
    picked: List[str] = []
    for ans in answers:
        if isinstance(ans, bool) or ans is None:
            continue
        text = normalize_option_key(ans)
        if not text:
            continue
        # 先按 [非字母] 切分成 token：这样 "A,B" 不会被拼成 "AB" 去误匹配选项 "AB"
        tokens = [t for t in re.split(r"[^A-Z]+", text) if t]
        for token in tokens:
            if token in keys:
                picked.append(token)                 # 整体即选项键（含 AA/AB 多字母）
                continue
            if not keys:
                picked.extend(token)                 # 无选项信息：退化为逐字母
                continue
            # 逐字母兜底（"ABD" → A,B,D），且字母必须真实存在于选项里
            for ch in token:
                if ch in keys:
                    picked.append(ch)
    ordered: List[str] = []
    for key in picked:
        if key not in ordered:
            ordered.append(key)
    return sort_option_keys(ordered)


def _normalize_text(answers: List[Any]) -> List[str]:
    """填空/简答答案归一为字符串列表（去首尾空白，保留顺序与重复空位）。"""
    out: List[str] = []
    for ans in answers:
        if isinstance(ans, bool):
            out.append("对" if ans else "错")
        elif ans is None:
            continue
        else:
            out.append(str(ans).strip())
    return out


def normalize_answers(raw: Any, qtype: str,
                      options: Optional[Dict[str, str]] = None) -> Tuple[List[Any], str]:
    """按题型归一化答案，返回 (归一化答案, display_type)。"""
    options = options or {}
    answers = _as_answer_list(raw)
    display = display_type_for(qtype, len(answers), bool(options))

    if display == DISPLAY_JUDGE:
        return _normalize_judge(answers, options), display
    if display in (DISPLAY_SINGLE, DISPLAY_MULTIPLE):
        return _normalize_choice(answers, list(options.keys())), display
    return _normalize_text(answers), display


def display_type_for(qtype: str, answer_count: int, has_options: bool) -> str:
    """由题型 + 答案基数推导展示类型（[不信任题型名的单/多选语义]）。"""
    if qtype in JUDGE_TYPES:
        return DISPLAY_JUDGE
    if qtype in GAP_TYPES:
        return DISPLAY_GAP
    if qtype in ESSAY_TYPES:
        return DISPLAY_ESSAY
    if qtype in CHOICE_TYPES or has_options:
        # 关键：按答案个数决定单选/多选（实测 A2 在不同题库里语义不一致）
        return DISPLAY_MULTIPLE if answer_count > 1 else DISPLAY_SINGLE
    # 文本类无选项
    return DISPLAY_GAP if has_options else DISPLAY_ESSAY


# ---------------------------------------------------------------------------
# 题型推断（v1 无 question_type）
# ---------------------------------------------------------------------------
def _looks_like_gap(stem: str, answers: List[str]) -> bool:
    """判断文本类题是填空还是简答。"""
    if _BLANK_RE.search(stem or ""):
        return True
    if len(answers) > 1:
        # 多个答案通常对应多个空
        return True
    if len(answers) == 1:
        answer = answers[0]
        if len(answer) <= _ESSAY_ANSWER_LEN and not _SENTENCE_PUNCT.search(answer):
            return True
    return False


def infer_qtype(question: Dict[str, Any]) -> str:
    """为缺少 question_type 的题目推断题型（主要服务 v1）。"""
    declared = question.get("question_type")
    if isinstance(declared, str) and declared.strip():
        return declared.strip()

    raw_answers = _as_answer_list(question.get("correct_answers"))
    options = question.get("options") if isinstance(question.get("options"), dict) else {}

    if options:
        if raw_answers and all(isinstance(a, bool) for a in raw_answers):
            return "judge"
        return "X" if len(raw_answers) > 1 else "A1"

    if len(raw_answers) == 1 and isinstance(raw_answers[0], bool):
        return "judge"
    if raw_answers and all(not isinstance(a, bool) for a in raw_answers):
        stem = str(question.get("question") or "")
        texts = _normalize_text(raw_answers)
        return "gap_filling" if _looks_like_gap(stem, texts) else "essay"
    return "essay"


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _has_html_tags(text: Any) -> bool:
    return isinstance(text, str) and bool(_HTML_TAG_RE.search(text))


def _item_texts(item: PracticeItem) -> Iterable[str]:
    """遍历条目中可能含媒体引用的文本字段。"""
    yield item.stem
    yield item.shared_stem
    yield item.analysis
    for value in item.options.values():
        yield str(value)
    for value in item.shared_options.values():
        yield str(value)


def _clean_options(raw: Any) -> Dict[str, str]:
    """归一化选项 dict，键统一为大写字母。"""
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in raw.items():
        k = str(key).strip().upper()
        if not k:
            continue
        out[k] = "" if value is None else str(value)
    return out


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------------------
# 主解析入口
# ---------------------------------------------------------------------------
def parse_bank(data: Dict[str, Any], *, source_name: str = "") -> ParsedBank:
    """解析题库对象，返回归一化的 ParsedBank。

    :raises BankFormatError: 结构不合法或不支持的版本
    """
    version = assert_supported(data)

    bank = ParsedBank(
        format_version=version,
        title=str(data.get("title") or "").strip(),
        author=str(data.get("author") or "").strip(),
        description=str(data.get("description") or "").strip(),
        uuid=str(data.get("uuid") or "").strip(),
        valid_key=str(data.get("valid_key") or ""),
    )
    if source_name and not bank.title:
        bank.title = source_name

    raw_chapters = data.get("chapters")
    if isinstance(raw_chapters, list) and raw_chapters:
        for idx, raw_chapter in enumerate(raw_chapters, start=1):
            if not isinstance(raw_chapter, dict):
                bank.warnings.append(f"第 {idx} 个 chapters 元素不是对象，已跳过")
                continue
            chapter_id = _safe_int(raw_chapter.get("chapter_id"), idx)
            chapter_name = str(
                raw_chapter.get("chapter_name") or f"第{chapter_id}章"
            ).strip()
            chapter = Chapter(chapter_id=chapter_id, chapter_name=chapter_name)
            _fill_chapter(chapter, raw_chapter.get("content") or [], bank)
            if not chapter.items:
                bank.warnings.append(f"章节「{chapter_name}」没有可解析题目")
            bank.chapters.append(chapter)
    else:
        # v1 / v2：单章扁平
        raw_content = data.get("content")
        if not isinstance(raw_content, list) or not raw_content:
            raise BankFormatError(
                "题库既没有 chapters 也没有 content 题目数组，无法解析"
            )
        chapter_id = _safe_int(data.get("chapter"), 1)
        chapter_name = str(
            data.get("chapter_name") or "全部题目"
        ).strip()
        chapter = Chapter(chapter_id=chapter_id, chapter_name=chapter_name)
        _fill_chapter(chapter, raw_content, bank)
        bank.chapters.append(chapter)

    if not bank.items:
        raise BankFormatError("题库解析后没有任何可作答条目")

    return bank


def _fill_chapter(chapter: Chapter, raw_content: Sequence[Any],
                  bank: ParsedBank) -> None:
    """把原始 content 数组填进章节（含复合题展开）。"""
    seen_ids: set = set()
    for index, raw_question in enumerate(raw_content, start=1):
        if not isinstance(raw_question, dict):
            bank.warnings.append(
                f"章节「{chapter.chapter_name}」第 {index} 条不是对象，已跳过"
            )
            continue
        qid = _safe_int(raw_question.get("question_id"), index)
        for item in _expand_question(raw_question, chapter, qid, bank):
            if item.item_id in seen_ids:
                # 同章节内 question_id 重复时补序号，保证唯一
                suffix = 2
                while f"{item.item_id}#{suffix}" in seen_ids:
                    suffix += 1
                bank.warnings.append(
                    f"章节「{chapter.chapter_name}」存在重复 item_id {item.item_id}，"
                    f"已重命名为 {item.item_id}#{suffix}"
                )
                item.item_id = f"{item.item_id}#{suffix}"
            seen_ids.add(item.item_id)
            chapter.items.append(item)


def _expand_question(raw_question: Dict[str, Any], chapter: Chapter,
                     qid: int, bank: ParsedBank) -> List[PracticeItem]:
    """把一道原始题目展开为一个或多个 PracticeItem。"""
    qtype = infer_qtype(raw_question)
    group_id = f"c{chapter.chapter_id}:q{qid}"
    shared_stem = str(raw_question.get("shared_stem") or "")
    shared_options = _clean_options(raw_question.get("shared_options"))
    bank_html = bool(raw_question.get("html_content"))
    skippable = bool(raw_question.get("skippable", True))
    parent_analysis = str(raw_question.get("analysis") or "")

    children = raw_question.get("content")
    is_composite = (
        qtype in COMPOSITE_TYPES and isinstance(children, list) and bool(children)
    )

    if not is_composite:
        options = _clean_options(raw_question.get("options"))
        correct, display = normalize_answers(
            raw_question.get("correct_answers"), qtype, options
        )
        stem = str(raw_question.get("question") or "")
        item = PracticeItem(
            item_id=group_id,
            chapter_id=chapter.chapter_id,
            chapter_name=chapter.chapter_name,
            qtype=qtype,
            display_type=display,
            stem=stem,
            options=options,
            correct=correct,
            analysis=parent_analysis,
            skippable=skippable,
            html=bank_html or _has_html_tags(stem) or _has_html_tags(parent_analysis),
            shared_stem=shared_stem,
            shared_options=shared_options,
            raw=raw_question,
        )
        _annotate(item, qtype, options, correct, bank, chapter)
        return [item]

    # ---------- 复合题：展开子题 ----------
    items: List[PracticeItem] = []
    if qtype == "B" and not shared_options:
        bank.warnings.append(
            f"{group_id} 为 B 型题但缺少 shared_options，子题将各自使用自身选项"
        )
    total = len(children)
    for sub_index, sub in enumerate(children, start=1):
        if not isinstance(sub, dict):
            bank.warnings.append(f"{group_id} 第 {sub_index} 个子题不是对象，已跳过")
            continue
        sub_qid = _safe_int(sub.get("sub_question_id"), sub_index)
        sub_qtype = infer_qtype(sub)
        sub_options = _clean_options(sub.get("options"))
        if not sub_options and qtype == "B":
            sub_options = dict(shared_options)
        correct, display = normalize_answers(
            sub.get("correct_answers"), sub_qtype, sub_options
        )
        sub_stem = str(sub.get("question") or "")
        sub_html = bool(sub.get("html_content", bank_html))
        sub_analysis = str(sub.get("analysis") or parent_analysis)
        item = PracticeItem(
            item_id=f"{group_id}:s{sub_qid}",
            chapter_id=chapter.chapter_id,
            chapter_name=chapter.chapter_name,
            qtype=sub_qtype,
            display_type=display,
            stem=sub_stem,
            options=sub_options,
            correct=correct,
            analysis=sub_analysis,
            skippable=bool(sub.get("skippable", skippable)),
            html=(
                sub_html
                or bank_html
                or _has_html_tags(sub_stem)
                or _has_html_tags(shared_stem)
                or _has_html_tags(sub_analysis)
            ),
            shared_stem=shared_stem,
            shared_options=shared_options,
            group_id=group_id,
            group_index=sub_index,
            group_size=total,
            raw=sub,
            parent_raw=raw_question,
            sub_index=sub_index,
        )
        _annotate(item, sub_qtype, sub_options, correct, bank, chapter)
        items.append(item)
    return items


def _annotate(item: PracticeItem, qtype: str, options: Dict[str, str],
              correct: List[Any], bank: ParsedBank, chapter: Chapter) -> None:
    """补充校验性告警（不抛异常，只记录）。"""
    if not correct:
        item.warnings.append("没有解析到正确答案")
        bank.warnings.append(f"{item.item_id} 没有解析到正确答案")
    if item.display_type in (DISPLAY_SINGLE, DISPLAY_MULTIPLE):
        if not options:
            item.warnings.append("选择题但没有任何选项")
        missing = [c for c in correct if c not in options]
        if missing:
            item.warnings.append(
                f"答案 {missing} 不在选项 {sorted(options)} 中"
            )
    # 题型名与答案基数不一致时给出提示（不改变渲染 —— 渲染按基数走）
    if qtype in ("A1", "A2", "K", "single_choice") and len(correct) > 1:
        item.warnings.append(
            f"题型标注为 {qtype}（通常单选）但正确答案有 {len(correct)} 个，已按多选题渲染"
        )


# ---------------------------------------------------------------------------
# 便捷入口
# ---------------------------------------------------------------------------
def parse_bank_object(bank_object: Any, *, source_name: str = "") -> ParsedBank:
    """parse_bank 的类型守卫版，便于直接喂 JSON 反序列化结果。"""
    if not isinstance(bank_object, dict):
        raise BankFormatError("题库根节点必须是 JSON 对象")
    return parse_bank(bank_object, source_name=source_name)


def iter_items(parsed: ParsedBank, chapter_ids: Optional[Iterable[int]] = None) -> List[PracticeItem]:
    """按章节过滤取条目；chapter_ids 为 None 表示全部。"""
    if chapter_ids is None:
        return parsed.items
    wanted = {int(c) for c in chapter_ids}
    out: List[PracticeItem] = []
    for chapter in parsed.chapters:
        if chapter.chapter_id in wanted:
            out.extend(chapter.items)
    return out


def grade_item(item: PracticeItem, user_answer: Any) -> Dict[str, Any]:
    """对单个条目判分（确定性部分）。

    仅覆盖 single / multiple / judge / gap。
    essay 不在此处判分 —— 交前端自评或 AI 评卷。

    :return: {"verdict": "correct"|"wrong"|"manual",
              "normalized": ..., "expected": ..., "detail": str}
    """
    display = item.display_type
    if display == DISPLAY_ESSAY:
        return {
            "verdict": "manual",
            "normalized": user_answer,
            "expected": item.correct,
            "detail": "简答题不自动判分，请自评或调用 AI 评卷",
        }

    expected = item.correct
    if display == DISPLAY_JUDGE:
        got, _ = normalize_answers(user_answer, "judge", item.options)
        ok = bool(got) and bool(expected) and got[0] == expected[0]
        return {"verdict": "correct" if ok else "wrong",
                "normalized": got, "expected": expected, "detail": ""}

    if display in (DISPLAY_SINGLE, DISPLAY_MULTIPLE):
        got, _ = normalize_answers(user_answer, "X", item.options)
        exp = sort_option_keys([str(x) for x in expected])
        ok = got == exp
        detail = "" if ok else f"期望 {''.join(exp)}，实际 {''.join(got) or '未作答'}"
        return {"verdict": "correct" if ok else "wrong",
                "normalized": got, "expected": exp, "detail": detail}

    # 填空题：归一化后逐空比较（去空白、全角转半角、大小写不敏感）
    got, _ = normalize_answers(user_answer, "gap_filling", item.options)
    exp = [normalize_gap_text(str(e)) for e in expected]
    got_norm = [normalize_gap_text(g) for g in got]
    ok = bool(exp) and got_norm[:len(exp)] == exp
    detail = "" if ok else f"期望 {exp}，实际 {got_norm or ['未作答']}"
    return {"verdict": "correct" if ok else "wrong",
            "normalized": got_norm, "expected": exp, "detail": detail}


def normalize_gap_text(text: str) -> str:
    """填空比对用的文本归一化：去空白、全角转半角、统一大小写。"""
    if text is None:
        return ""
    out = str(text)
    # 全角 ASCII（U+FF01–U+FF5E）→ 半角
    out = "".join(
        chr(ord(ch) - 0xFEE0) if "！" <= ch <= "～" else ch for ch in out
    )
    out = out.replace("\u3000", " ")          # 全角空格
    out = re.sub(r"\s+", "", out)             # 去掉所有空白
    return out.strip().lower()
