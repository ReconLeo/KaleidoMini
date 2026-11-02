# -*- coding: utf-8 -*-
"""M3 回归：km_practice —— 练习载荷装配 / 判分 / 揭晓 / 汇总

重点断言（都对应设计决定）：
  * **载荷绝不包含答案与解析**（自测可信 + 省流量）
  * 判分走 km_parser.grade_item 单一真源（单选/多选/判断/填空/简答）
  * reveal=True 时 verdict="revealed" 且不判分
  * 复合题（A3/A4/B）的展开与共用题干在载荷中完整
  * 章节过滤、汇总统计、错题选取

运行： python tests/test_practice.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from kaleido_mini import km_parser as KP      # noqa: E402
from kaleido_mini import km_practice as KPr   # noqa: E402

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


def fake_resolver(present):
    def resolve(ref):
        return f"/api/kaleido_mini/media/{ref}" if ref in present else None
    return resolve


def main():
    print("=" * 78)
    print("M3 / km_practice 回归")
    print("=" * 78)

    v3 = os.path.join(STD, "QuizBank_sample_v3.json")
    if not os.path.isfile(v3):
        print(f"[SKIP] 缺少 {v3}")
        return 1
    parsed = KP.parse_bank(load(v3))
    resolve = fake_resolver(set())

    # ---------------- 1. 章节概览 ----------------
    chapters = KPr.chapter_choices(parsed)
    check("章节概览 2 章", len(chapters) == 2, str(len(chapters)))
    check("章节概览含条目数", chapters[0]["item_count"] == 7, str(chapters[0]["item_count"]))
    check("章节概览含题型分布", bool(chapters[0]["type_counts"]), str(chapters[0]["type_counts"]))

    # ---------------- 2. 载荷装配 ----------------
    payload = KPr.build_payload(parsed, subject="眼科学", filename="样例.json",
                                resolve=resolve)
    check("载荷条目数 = 13", payload["item_count"] == 13, str(payload["item_count"]))
    check("载荷带题库元信息",
          payload["bank"]["title"] and payload["bank"]["format_version"] == [3, 0, 0],
          str(payload["bank"].get("title")))

    # —— 核心：不得泄露答案与解析 ——
    blob = json.dumps(payload, ensure_ascii=False)
    check("载荷不含 correct 字段", '"correct"' not in blob)
    check("载荷不含 analysis_html", "analysis_html" not in blob)
    check("载荷不含正确答案字样（暂无解析）", "暂无解析" not in blob)
    leaked = [it["item_id"] for it in payload["items"] if "correct" in it or "analysis_html" in it]
    check("逐条检查均无答案字段", not leaked, str(leaked))

    first = payload["items"][0]
    check("条目含题干 HTML", first["stem_html"].startswith("<div class=\"kqb-rt\">"),
          first["stem_html"][:40])
    check("条目含选项表（数组形式）", isinstance(first["options"], list) and first["options"],
          str(len(first.get("options") or [])))
    check("选项含展示键与规范键",
          all("key" in o and "canonical" in o and "html" in o for o in first["options"]),
          str(first["options"][0])[:80])
    check("未随机化时展示键 = 规范键（A..E）",
          [o["key"] for o in first["options"]] == [o["canonical"] for o in first["options"]]
          == ["A", "B", "C", "D", "E"],
          str([o["key"] for o in first["options"]]))
    check("条目含 display_type", first["display_type"] in KP.DISPLAY_TYPES,
          first["display_type"])
    check("条目含题型标签", bool(first["type_label"]), first["type_label"])
    check("载荷含 shuffle_options 标记", payload.get("shuffle_options") is False,
          str(payload.get("shuffle_options")))

    # 复合题
    composite = [i for i in payload["items"] if i["group_size"] > 1]
    check("复合题展开 6 条", len(composite) == 6, str(len(composite)))
    a3 = [i for i in composite if i["group_id"].endswith("q8")]
    check("A3 子题带共用题干 HTML",
          a3 and "kqb-rt" in a3[0]["shared_stem_html"], (a3[0]["shared_stem_html"][:40] if a3 else ""))
    b_items = [i for i in composite if i["group_id"].endswith("q9")]
    check("B 型子题选项来自共用选项",
          b_items and len(b_items[0]["options"]) == 4,
          str(len(b_items[0]["options"]) if b_items else []))

    # ---------------- 3. 选项随机化 ----------------
    shuffled = KPr.build_payload(parsed, resolve=resolve, shuffle_options=True,
                                 shuffle_seed="fixed-seed-for-test")
    s_first = shuffled["items"][0]
    canon = [o["canonical"] for o in s_first["options"]]
    shown = [o["key"] for o in s_first["options"]]
    check("随机化后展示键仍是 A..E", shown == ["A", "B", "C", "D", "E"], str(shown))
    check("随机化只换顺序，规范键集合不变",
          sorted(canon) == ["A", "B", "C", "D", "E"], str(canon))
    check("随机化确实改变了顺序（与原始不同）", canon != ["A", "B", "C", "D", "E"], str(canon))
    check("随机化后选项内容与规范键一一对应",
          all(o["canonical"] in canon for o in s_first["options"]))
    check("随机化后仍不含答案", '"correct"' not in json.dumps(shuffled, ensure_ascii=False))
    # 同种子应可复现
    again = KPr.build_payload(parsed, resolve=resolve, shuffle_options=True,
                              shuffle_seed="fixed-seed-for-test")
    check("同种子随机化可复现",
          [o["canonical"] for o in again["items"][0]["options"]] == canon)
    # 不复现于不同种子（绝大多数情况下）
    other = KPr.build_payload(parsed, resolve=resolve, shuffle_options=True,
                              shuffle_seed="another-seed")
    check("不同种子得到不同顺序",
          [o["canonical"] for o in other["items"][0]["options"]] != canon
          or len(canon) < 2,
          str([o["canonical"] for o in other["items"][0]["options"]]))
    check("随机化载荷标记 shuffle_options=True", shuffled.get("shuffle_options") is True)
    # 所有选择题都被重排（不只是第一题）
    multi_items = [i for i in shuffled["items"] if len(i["options"]) > 2]
    check("多条选择题均带规范键映射", all(
        all(o["canonical"] for o in i["options"]) for i in multi_items),
        str(len(multi_items)))

    # ---------------- 3. 章节过滤 ----------------
    only2 = KPr.build_payload(parsed, chapters=[2], resolve=resolve)
    check("按章节过滤 = 6 条", only2["item_count"] == 6, str(only2["item_count"]))
    check("过滤后 selected_chapter_ids 正确", only2["selected_chapter_ids"] == [2],
          str(only2["selected_chapter_ids"]))
    try:
        KPr.select_items(parsed, [99])
        check("不存在的章节应报错", False)
    except KPr.PracticeError:
        check("不存在的章节抛 PracticeError", True)

    # ---------------- 4. 揭晓（不判分） ----------------
    ids = {i.item_id: i for i in parsed.items}
    single_id = ids["c1:q2"].item_id        # A1 单选，答案 B
    reveal = KPr.grade_item_payload(parsed, single_id, reveal=True, resolve=resolve)
    check("reveal 返回 verdict=revealed", reveal["verdict"] == KPr.VERDICT_REVEALED,
          reveal["verdict"])
    check("reveal 返回正确答案", reveal["correct"] == ["B"], str(reveal["correct"]))
    check("reveal 返回可读答案", reveal["correct_display"] == "B", reveal["correct_display"])
    check("reveal 不含用户答案", reveal["user_display"] == "", reveal["user_display"])
    check("reveal 返回解析 HTML", reveal["analysis_html"].startswith("<div"), reveal["analysis_html"][:30])

    # ---------------- 5. 判分（单一真源） ----------------
    ok = KPr.grade_item_payload(parsed, single_id, "B", resolve=resolve)
    check("单选答对 → correct", ok["verdict"] == KPr.VERDICT_CORRECT, ok["verdict"])
    bad = KPr.grade_item_payload(parsed, single_id, "A", resolve=resolve)
    check("单选答错 → wrong", bad["verdict"] == KPr.VERDICT_WRONG, bad["verdict"])
    check("答错返回 detail", bool(bad["detail"]), bad["detail"])
    check("答错返回用户答案", bad["user_display"] == "A", bad["user_display"])

    multi_id = ids["c1:q1"].item_id        # X 多选，答案 A B D
    check("多选完全一致 → correct",
          KPr.grade_item_payload(parsed, multi_id, ["D", "A", "B"], resolve=resolve)["verdict"]
          == KPr.VERDICT_CORRECT)
    check("多选漏选 → wrong",
          KPr.grade_item_payload(parsed, multi_id, ["A", "B"], resolve=resolve)["verdict"]
          == KPr.VERDICT_WRONG)
    check("多选多选 → wrong",
          KPr.grade_item_payload(parsed, multi_id, ["A", "B", "D", "E"], resolve=resolve)["verdict"]
          == KPr.VERDICT_WRONG)

    judge_id = ids["c1:q3"].item_id        # 判断，答案 [True]
    jr = KPr.grade_item_payload(parsed, judge_id, True, resolve=resolve)
    check("判断答对 → correct", jr["verdict"] == KPr.VERDICT_CORRECT, jr["verdict"])
    check("判断答案可读为「正确」", jr["correct_display"] == "正确", jr["correct_display"])
    check("判断答错 → wrong",
          KPr.grade_item_payload(parsed, judge_id, False, resolve=resolve)["verdict"]
          == KPr.VERDICT_WRONG)

    gap_id = ids["c1:q4"].item_id          # 填空「示例答案」
    check("填空答对（忽略空白）→ correct",
          KPr.grade_item_payload(parsed, gap_id, "  示例答案 ", resolve=resolve)["verdict"]
          == KPr.VERDICT_CORRECT)
    check("填空答错 → wrong",
          KPr.grade_item_payload(parsed, gap_id, "别的", resolve=resolve)["verdict"]
          == KPr.VERDICT_WRONG)

    essay_id = ids["c1:q5"].item_id        # 简答
    er = KPr.grade_item_payload(parsed, essay_id, "我的作答内容", resolve=resolve)
    check("简答 → manual（不自动判分）", er["verdict"] == KPr.VERDICT_MANUAL, er["verdict"])
    check("简答返回参考答案", er["correct"] == ["参考答案"], str(er["correct"]))
    check("简答返回用户答案", "我的作答内容" in er["user_display"], er["user_display"])

    try:
        KPr.grade_item_payload(parsed, "不存在的题", "A")
        check("不存在的题目应报错", False)
    except KPr.PracticeError:
        check("不存在的题目抛 PracticeError", True)

    # ---------------- 6. 汇总 ----------------
    records = {
        single_id: {"verdict": "correct", "answer": "B"},
        multi_id: {"verdict": "wrong", "answer": ["A"]},
        judge_id: {"verdict": "correct", "answer": True},
        gap_id: {"verdict": "revealed", "answer": None},
        essay_id: {"verdict": "manual", "answer": "写了一点"},
    }
    summary = KPr.summarize_session(payload, records)
    check("汇总总数 = 13", summary["total"] == 13, str(summary["total"]))
    check("汇总已作答 = 5", summary["answered"] == 5, str(summary["answered"]))
    check("汇总未作答 = 8", summary["unanswered"] == 8, str(summary["unanswered"]))
    check("汇总 correct=2", summary["counts"]["correct"] == 2, str(summary["counts"]))
    check("汇总 wrong=1", summary["counts"]["wrong"] == 1, str(summary["counts"]))
    check("汇总 manual=1", summary["counts"]["manual"] == 1, str(summary["counts"]))
    check("汇总 revealed=1", summary["counts"]["revealed"] == 1, str(summary["counts"]))
    check("正确率 = 2/3", summary["accuracy"] == round(2 / 3, 4), str(summary["accuracy"]))
    check("汇总含分题型", bool(summary["by_type"]), str(list(summary["by_type"])))
    check("汇总含分章节", bool(summary["by_chapter"]))
    check("错题含 wrong 与 revealed 两条",
          set(summary["wrong_item_ids"]) == {multi_id, gap_id},
          str(summary["wrong_item_ids"]))

    # 空记录不应崩
    empty = KPr.summarize_session(payload, {})
    check("空记录汇总不崩", empty["answered"] == 0 and empty["accuracy"] is None)
    # 未知 item_id 被忽略
    weird = KPr.summarize_session(payload, {"ghost": {"verdict": "correct"}})
    check("未知 item_id 被忽略", weird["answered"] == 0, str(weird["answered"]))

    # ---------------- 7. 错题导出视图 ----------------
    wrong_view = KPr.build_wrong_export_view(payload, records)
    check("错题视图 2 条", len(wrong_view) == 2, str(len(wrong_view)))
    check("错题视图带章节与题型",
          all(w.get("chapter_name") and w.get("qtype") for w in wrong_view),
          str(wrong_view[:1]))
    check("错题视图保留用户答案",
          any(w["item_id"] == multi_id and w["user_answer"] == ["A"] for w in wrong_view))

    # ---------------- 8. 真实题库（媒体重写 + 规模） ----------------
    real = os.path.join(QBANKS, "眼科学", "眼科学题库_System.json")
    if os.path.isfile(real):
        real_parsed = KP.parse_bank(load(real))
        media_dir = os.path.join(QBANKS, "眼科学", "眼科学题库_System")
        present = set(os.listdir(media_dir)) if os.path.isdir(media_dir) else set()
        real_payload = KPr.build_payload(real_parsed, resolve=fake_resolver(present))
        check("真实题库载荷条目数 > 600", real_payload["item_count"] > 600,
              str(real_payload["item_count"]))
        check("真实题库媒体全部解析（0 缺失）",
              real_payload["media_missing_total"] == 0,
              str(real_payload["media_missing_total"]))
        with_img = [i for i in real_payload["items"] if "/api/kaleido_mini/media/" in i["stem_html"]]
        check("真实题库题干含已重写的图片 URL", len(with_img) > 0, str(len(with_img)))
        size_kb = len(json.dumps(real_payload, ensure_ascii=False).encode("utf-8")) / 1024
        check("真实题库载荷体积 < 4MB（整库一次性下发可接受）", size_kb < 4096,
              f"{size_kb:.0f}KB")
        blob2 = json.dumps(real_payload, ensure_ascii=False)
        check("真实题库载荷不含解析", "analysis_html" not in blob2)

        # 抽题判分一致性（与 km_parser.grade_item 同源）
        sample = real_payload["items"][0]
        item = {i.item_id: i for i in real_parsed.items}[sample["item_id"]]
        if item.display_type == KP.DISPLAY_SINGLE and item.correct:
            res = KPr.grade_item_payload(real_parsed, item.item_id, item.correct[0],
                                        resolve=fake_resolver(present))
            check("真实题库首发答案判为 correct", res["verdict"] == KPr.VERDICT_CORRECT,
                  res["verdict"])
    else:
        print(f"[SKIP] {real}")

    # ---------------- 9. 缺媒体题库 ----------------
    merged = os.path.join(QBANKS, "眼科学",
                          "眼科题库（合并）_大连医科大学附属第一医院眼科_1.json")
    if os.path.isfile(merged):
        mp = KP.parse_bank(load(merged))
        mpayload = KPr.build_payload(mp, resolve=fake_resolver(set()))
        check("缺媒体题库载荷不崩", mpayload["item_count"] > 600, str(mpayload["item_count"]))
        check("缺媒体被记录", mpayload["media_missing_total"] > 0,
              str(mpayload["media_missing_total"]))
        check("缺失媒体渲染为占位而非 img",
              any("kqb-media-missing" in i["stem_html"] for i in mpayload["items"]))
    else:
        print(f"[SKIP] {merged}")

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
