# -*- coding: utf-8 -*-
"""M1 回归：km_parser —— 四代格式归一化 / 复合题展开 / 题型基数判定 / 判分原语。

真实数据来自 Kaleido 项目（路径可用 KALEIDO_PROJECT_ROOT 覆盖）：
  * standardization/QuizBank_sample.json       v1
  * standardization/QuizBank_sample_v2.json    v2
  * standardization/QuizBank_sample_v3.json    v3（含 A3/A4/B 复合题）
  * backup/qbanks/眼科学/*.json                 v3 真实数据（含媒体引用与缺失媒体）

运行： python tests/test_parser.py
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from kaleido_mini import km_parser as P  # noqa: E402

KALEIDO = os.environ.get("KALEIDO_PROJECT_ROOT", r"H:\Coding\Projects\Kaleido\Project")
STD = os.path.join(KALEIDO, "standardization")
QBANKS = os.path.join(KALEIDO, "backup", "qbanks")

RESULTS = []


def check(name, cond, detail="", extra=""):
    RESULTS.append((name, bool(cond), detail))
    shown = extra or detail
    line = f"[{'PASS' if cond else 'FAIL'}] {name}"
    if shown:
        line += (f"  ({shown})" if cond else f"  << {shown}")
    print(line)
    return bool(cond)


def load(path):
    with open(path, "r", encoding="utf-8-sig") as fp:
        return json.load(fp)


def parse_file(path):
    data = load(path)
    return P.parse_bank(data, source_name=os.path.basename(path)), data


def raw_media_refs(path):
    """独立于解析器的媒体引用统计（对照基准）。

    注意必须排除反斜杠：题库 JSON 里的引号被转义成 \\"，若字符类不排除 '\\'
    会把 `img_001.png\\` 这类带转义尾巴的串当成文件名（此前就栽在这）。
    """
    with open(path, "r", encoding="utf-8-sig") as fp:
        text = fp.read()
    return set(re.findall(r"/media/([^\"'\s<>?#\\]+)", text))


def main():
    print("=" * 78)
    print("M1 / km_parser 回归")
    print("=" * 78)

    # ---------------- 1. 版本识别 ----------------
    check("无 format_version → v1", P.detect_format_version({"content": []}) == (1, 0, 0))
    check("format_version 列表 [2,0,0]",
          P.detect_format_version({"format_version": [2, 0, 0]}) == (2, 0, 0))
    check("format_version 字符串 '3.1.0'",
          P.detect_format_version({"format_version": "3.1.0"}) == (3, 1, 0))
    check("format_version 短列表补零 [3]",
          P.detect_format_version({"format_version": [3]}) == (3, 0, 0))
    check("format_version 数字 2 → (2,0,0)",
          P.detect_format_version({"format_version": 2}) == (2, 0, 0))
    try:
        P.detect_format_version({"encrypted_content": "x"})
        check("加密题库应抛 BankFormatError", False)
    except P.BankFormatError:
        check("加密题库抛 BankFormatError", True)
    try:
        P.assert_supported({"format_version": [9, 0, 0]})
        check("不支持的主版本应抛错", False)
    except P.BankFormatError:
        check("主版本 9 被拒绝", True)

    # ---------------- 2. v1 / v2 / v3 样例 ----------------
    p1 = os.path.join(STD, "QuizBank_sample.json")
    if os.path.isfile(p1):
        bank1, _ = parse_file(p1)
        check("v1 版本 = (1,0,0)", bank1.format_version == (1, 0, 0))
        check("v1 条目数 = 3", bank1.item_count == 3, str(bank1.item_count))
        check("v1 单章", bank1.chapter_count == 1)
        v1_item = bank1.items[0]
        check("v1 答案 3 个 → multiple", v1_item.display_type == P.DISPLAY_MULTIPLE,
              v1_item.display_type)
        check("v1 答案归一为字母", v1_item.correct == ["A", "B", "D"], str(v1_item.correct))
        v1_third = bank1.items[2]
        check("v1 第三题（无选项文本答案）判为文类",
              v1_third.display_type in (P.DISPLAY_GAP, P.DISPLAY_ESSAY),
              v1_third.display_type)
    else:
        print(f"[SKIP] {p1}")

    p2 = os.path.join(STD, "QuizBank_sample_v2.json")
    if os.path.isfile(p2):
        bank2, _ = parse_file(p2)
        check("v2 版本 = (2,0,0)", bank2.format_version == (2, 0, 0))
        check("v2 条目数 = 5", bank2.item_count == 5, str(bank2.item_count))
        types = {i.qtype for i in bank2.items}
        check("v2 保留原始题型名",
              {"multiple_choice", "single_choice", "judge", "gap_filling", "essay"} <= types,
              ",".join(sorted(types)))
        judge = [i for i in bank2.items if i.qtype == "judge"][0]
        check("v2 判断题答案归一为 [True]", judge.correct == [True], str(judge.correct))
        check("v2 判断题展示类型 = judge", judge.display_type == P.DISPLAY_JUDGE)
    else:
        print(f"[SKIP] {p2}")

    p3 = os.path.join(STD, "QuizBank_sample_v3.json")
    if os.path.isfile(p3):
        bank3, _ = parse_file(p3)
        check("v3 版本 = (3,0,0)", bank3.format_version == (3, 0, 0))
        check("v3 章节数 = 2", bank3.chapter_count == 2, str(bank3.chapter_count))
        check("v3 条目数 = 13（7 叶子 + 3 复合题各展开 2 子题）",
              bank3.item_count == 13, str(bank3.item_count))
        check("v3 第一章 7 条", len(bank3.chapters[0].items) == 7,
              str(len(bank3.chapters[0].items)))
        check("v3 第二章 6 条", len(bank3.chapters[1].items) == 6,
              str(len(bank3.chapters[1].items)))

        composites = [i for i in bank3.items if i.group_size > 1]
        check("复合题展开数 = 6", len(composites) == 6, str(len(composites)))
        a3 = [i for i in composites if i.group_id.endswith("q8")]
        check("A3 两子题共享 group_id", len(a3) == 2 and a3[0].group_id == a3[1].group_id)
        check("A3 子题携带 shared_stem", bool(a3[0].shared_stem), a3[0].shared_stem[:24])
        check("A3 子题 group_index = [1,2]",
              [i.group_index for i in a3] == [1, 2], str([i.group_index for i in a3]))
        check("A3 子题 item_id 含 :s",
              all(":s" in i.item_id for i in a3), a3[0].item_id)

        b_items = [i for i in composites if i.group_id.endswith("q9")]
        check("B 型子题继承 shared_options",
              all(i.options for i in b_items) and b_items[0].options == b_items[1].options,
              str(list(b_items[0].options)))
        check("B 型子题携带 shared_options",
              bool(b_items[0].shared_options), str(list(b_items[0].shared_options)))

        # A2 基数判定（实测数据把 A2 标为不定项，答案 3 个）
        a2 = [i for i in bank3.items if i.qtype == "A2"]
        check("A2 在本题库答案为 3 个", len(a2) == 1 and len(a2[0].correct) == 3,
              str(a2[0].correct) if a2 else "无")
        check("A2 按基数渲染为 multiple（不信任题型名）",
              a2[0].display_type == P.DISPLAY_MULTIPLE, a2[0].display_type)

        # <richtext> 畸形闭合不得影响解析
        rt = [i for i in bank3.items if "<richtext>" in (i.analysis or "")]
        check("含 <richtext> 的解析被保留", len(rt) >= 1, str(len(rt)))
        check("html 标记：A4 组为 HTML",
              any(i.html for i in composites if i.group_id.endswith("q10")))
    else:
        print(f"[SKIP] {p3}")

    # ---------------- 3. 真实题库（眼科学） ----------------
    real_files = [
        "眼科学题库_System.json",
        "眼科学随堂测试合集_眼科学教研室.json",
        "眼科题库（合并）_大连医科大学附属第一医院眼科_1.json",
    ]
    for name in real_files:
        path = os.path.join(QBANKS, "眼科学", name)
        if not os.path.isfile(path):
            print(f"[SKIP] {path}")
            continue
        bank, raw = parse_file(path)
        expected_media = raw_media_refs(path)
        check(f"{name[:14]}… 解析成功", bank.item_count > 0, f"{bank.item_count} 条")
        check(f"{name[:14]}… 媒体引用与独立统计一致",
              set(bank.media_refs) == expected_media,
              f"解析 {len(bank.media_refs)} vs 原始 {len(expected_media)}")
        check(f"{name[:14]}… 章节数 > 0", bank.chapter_count > 0, str(bank.chapter_count))
        # 每题都应拿到展示类型且有正确答案
        no_answer = [i.item_id for i in bank.items if not i.correct]
        check(f"{name[:14]}… 全部条目有正确答案",
              not no_answer, f"{len(no_answer)} 条缺答案：{no_answer[:3]}")
        bad_display = [i.item_id for i in bank.items if i.display_type not in P.DISPLAY_TYPES]
        check(f"{name[:14]}… 展示类型全部合法", not bad_display, str(bad_display[:3]))

    # 缺失媒体题库：解析层不得因媒体缺失而报错（渲染层才做占位）
    merged = os.path.join(QBANKS, "眼科学",
                          "眼科题库（合并）_大连医科大学附属第一医院眼科_1.json")
    if os.path.isfile(merged):
        bank, _ = parse_file(merged)
        check("缺媒体目录的题库仍可解析（73 引用）",
              bank.media_ref_count == 73, str(bank.media_ref_count))

    # 不一致扩展名：随堂测试合集引用 /media/1.jpg 但目录只有 1.png
    quiz = os.path.join(QBANKS, "眼科学", "眼科学随堂测试合集_眼科学教研室.json")
    if os.path.isfile(quiz):
        bank, _ = parse_file(quiz)
        check("随堂测试合集含 /media/1.jpg 引用",
              "1.jpg" in bank.media_refs, str(bank.media_refs))

    # ---------------- 4. 展示类型推导（基数优先） ----------------
    check("X + 1 答案 → single", P.display_type_for("X", 1, True) == P.DISPLAY_SINGLE)
    check("A1 + 2 答案 → multiple", P.display_type_for("A1", 2, True) == P.DISPLAY_MULTIPLE)
    check("K + 1 答案 → single", P.display_type_for("K", 1, True) == P.DISPLAY_SINGLE)
    check("judge → judge", P.display_type_for("judge", 1, False) == P.DISPLAY_JUDGE)
    check("gap_filling → gap", P.display_type_for("gap_filling", 1, False) == P.DISPLAY_GAP)
    check("essay → essay", P.display_type_for("essay", 1, False) == P.DISPLAY_ESSAY)

    # ---------------- 5. 题型推断（v1） ----------------
    check("有选项+多答案 → X",
          P.infer_qtype({"options": {"A": "x", "B": "y"}, "correct_answers": ["A", "B"]}) == "X")
    check("有选项+单答案 → A1",
          P.infer_qtype({"options": {"A": "x", "B": "y"}, "correct_answers": ["A"]}) == "A1")
    check("bool 答案 → judge",
          P.infer_qtype({"options": {}, "correct_answers": [True]}) == "judge")
    check("含空标记文本题 → gap_filling",
          P.infer_qtype({"options": {}, "question": "中国的首都是____。",
                         "correct_answers": ["北京"]}) == "gap_filling")
    check("长答案文本题 → essay",
          P.infer_qtype({"options": {}, "question": "请简述发病机制。",
                         "correct_answers": ["本病由多种因素共同作用导致"]}) == "essay")

    # ---------------- 6b. 选项键通用化（A…Z, AA…ZZ 的 26 进制） ----------------
    check("option_key_index A=1", P.option_key_index("A") == 1)
    check("option_key_index Z=26", P.option_key_index("Z") == 26)
    check("option_key_index AA=27", P.option_key_index("AA") == 27)
    check("option_key_index AB=28", P.option_key_index("AB") == 28)
    check("option_key_index ZZ=702", P.option_key_index("ZZ") == 702)
    check("option_key_index 忽略大小写与空白", P.option_key_index(" aA ") == 27,
          str(P.option_key_index(" aA ")))
    check("option_key_index 非字母返回 -1", P.option_key_index("①") == -1)
    check("excel_key 1=A", P.excel_key(1) == "A")
    check("excel_key 26=Z", P.excel_key(26) == "Z")
    check("excel_key 27=AA", P.excel_key(27) == "AA")
    check("excel_key 52=AZ", P.excel_key(52) == "AZ")
    check("excel_key 53=BA", P.excel_key(53) == "BA")
    check("excel_key 与 option_key_index 互逆（1..800）",
          all(P.excel_key(i) and P.option_key_index(P.excel_key(i)) == i
              for i in range(1, 801)))
    check("sort_option_keys 按语义排序（A,B,AA,AB）",
          P.sort_option_keys(["B", "AA", "A", "AB"]) == ["A", "B", "AA", "AB"],
          str(P.sort_option_keys(["B", "AA", "A", "AB"])))
    check("sort_option_keys 非字母键垫后且保持相对顺序",
          P.sort_option_keys(["②", "B", "①", "A"]) == ["A", "B", "②", "①"],
          str(P.sort_option_keys(["②", "B", "①", "A"])))

    # 多字母选项键不能被拆成单字母
    big_options = {P.excel_key(i): f"选项{i}" for i in range(1, 29)}   # A..AB 共 28 个
    answers, _dt = P.normalize_answers(["AA"], "X", big_options)
    check("答案 AA 不被拆成 A,A", answers == ["AA"], str(answers))
    answers, _dt = P.normalize_answers(["AB", "A"], "X", big_options)
    check("多字母+单字母混合答案正确归一", answers == ["A", "AB"], str(answers))
    answers, _dt = P.normalize_answers("A,B", "X", big_options)
    check("逗号分隔答案可拆分", answers == ["A", "B"], str(answers))
    answers, _dt = P.normalize_answers(["A", "B", "D"], "X",
                                       {"A": "1", "B": "2", "C": "3", "D": "4"})
    check("传统 A/B/D 答案不受影响", answers == ["A", "B", "D"], str(answers))
    # 27 项以上：判分必须按语义顺序而非字典序
    norm_correct, _ = P.normalize_answers(["AA", "B"], "X", big_options)
    big_item = P.PracticeItem(
        item_id="t28", chapter_id=1, chapter_name="c", qtype="X",
        display_type=P.DISPLAY_MULTIPLE, options=big_options, correct=norm_correct)
    check("28 选项：答案归一按语义序（B 在 AA 前）", big_item.correct == ["B", "AA"],
          str(big_item.correct))
    check("28 选项：选 AA+B 判对",
          P.grade_item(big_item, ["AA", "B"])["verdict"] == "correct")
    check("28 选项：只选 AA 判错",
          P.grade_item(big_item, ["AA"])["verdict"] == "wrong")
    check("28 选项：把 AA 当 A 判错",
          P.grade_item(big_item, ["A", "B"])["verdict"] == "wrong")

    # ---------------- 6. 判分原语 ----------------
    single = P.PracticeItem(item_id="t1", chapter_id=1, chapter_name="c",
                            qtype="A1", display_type=P.DISPLAY_SINGLE,
                            options={"A": "甲", "B": "乙"}, correct=["A"])
    check("单选正确", P.grade_item(single, "A")["verdict"] == "correct")
    check("单选字母小写也判定正确", P.grade_item(single, "a")["verdict"] == "correct")
    check("单选错误", P.grade_item(single, "B")["verdict"] == "wrong")
    check("单选空答案判定错误", P.grade_item(single, "")["verdict"] == "wrong")

    multi = P.PracticeItem(item_id="t2", chapter_id=1, chapter_name="c",
                           qtype="X", display_type=P.DISPLAY_MULTIPLE,
                           options={"A": "1", "B": "2", "C": "3"}, correct=["A", "C"])
    check("多选完全一致判定正确", P.grade_item(multi, ["C", "A"])["verdict"] == "correct")
    check("多选漏选判定错误", P.grade_item(multi, ["A"])["verdict"] == "wrong")
    check("多选多选判定错误", P.grade_item(multi, ["A", "B", "C"])["verdict"] == "wrong")

    judge = P.PracticeItem(item_id="t3", chapter_id=1, chapter_name="c",
                           qtype="judge", display_type=P.DISPLAY_JUDGE, correct=[True])
    check("判断 true 正确", P.grade_item(judge, True)["verdict"] == "correct")
    check("判断 '对' 正确", P.grade_item(judge, "对")["verdict"] == "correct")
    check("判断 false 错误", P.grade_item(judge, False)["verdict"] == "wrong")

    gap = P.PracticeItem(item_id="t4", chapter_id=1, chapter_name="c",
                         qtype="gap_filling", display_type=P.DISPLAY_GAP,
                         correct=["北京"])
    check("填空完全一致", P.grade_item(gap, "北京")["verdict"] == "correct")
    check("填空首尾空白忽略", P.grade_item(gap, "  北京  ")["verdict"] == "correct")
    check("填空大小写不敏感", P.grade_item(
        P.PracticeItem(item_id="t5", chapter_id=1, chapter_name="c",
                       qtype="gap_filling", display_type=P.DISPLAY_GAP,
                       correct=["Beijing"]), "beijing")["verdict"] == "correct")
    check("填空全角转半角", P.grade_item(
        P.PracticeItem(item_id="t6", chapter_id=1, chapter_name="c",
                       qtype="gap_filling", display_type=P.DISPLAY_GAP,
                       correct=["ABC123"]), "ＡＢＣ１２３")["verdict"] == "correct")

    essay = P.PracticeItem(item_id="t7", chapter_id=1, chapter_name="c",
                           qtype="essay", display_type=P.DISPLAY_ESSAY, correct=["参考"])
    check("简答不自动判分 → manual", P.grade_item(essay, "随便写")["verdict"] == "manual")

    check("normalize_gap_text 去所有空白",
          P.normalize_gap_text(" a b\tc\n") == "abc")

    # ---------------- 7. 章节过滤 ----------------
    if os.path.isfile(p3):
        bank3, _ = parse_file(p3)
        only2 = P.iter_items(bank3, [2])
        check("按章节过滤只取第二章", all(i.chapter_id == 2 for i in only2) and len(only2) == 6,
              f"{len(only2)} 条")
        check("chapter_ids=None 取全部", len(P.iter_items(bank3)) == bank3.item_count)

    # ---------------- 8. 异常输入 ----------------
    for bad, label in [
        ({}, "空对象"),
        ({"chapters": []}, "空 chapters"),
        ({"content": []}, "空 content"),
        ({"chapters": "not-a-list"}, "chapters 类型错误"),
    ]:
        try:
            P.parse_bank(bad)
            check(f"非法题库应报错：{label}", False)
        except P.BankFormatError:
            check(f"非法题库报错：{label}", True)

    # ---------------- 汇总 ----------------
    total = len(RESULTS)
    failed = [r for r in RESULTS if not r[1]]
    print("-" * 78)
    print(f"共 {total} 项，通过 {total - len(failed)}，失败 {len(failed)}")
    if failed:
        for name, _, detail in failed:
            print(f"  FAIL: {name}  {detail}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
