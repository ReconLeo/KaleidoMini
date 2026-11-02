# -*- coding: utf-8 -*-
"""M4 回归：km_export —— 错题 → v3.1.0 题库 / 错题本(MD/HTML)

关键断言：
  * 导出的题库是 **v3.1.0**，且用 [原始对象重建]：选项顺序、html_content、题型名保真
  * 导出内容 **包含答案与解析**（与练习载荷相反——导出文件是用户自己的资产）
  * 复合题只挑选中子题，父题（共用题干/选项）整体保留
  * 新 uuid + 可重签名（解决完整版"解密后明文验签必失败"的历史问题）
  * 导出结果能 [再解析] 回来（可再导入 Mini），条目数与选择一致
  * 含相对媒体引用时导出包可被 .kqb 核对通过

运行： python tests/test_export.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from kaleido_mini import km_crypto as KC     # noqa: E402
from kaleido_mini import km_export as KEX    # noqa: E402
from kaleido_mini import km_package as KQB   # noqa: E402
from kaleido_mini import km_parser as KP     # noqa: E402

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


def main():
    print("=" * 78)
    print("M4 / km_export 回归")
    print("=" * 78)

    v3 = os.path.join(STD, "QuizBank_sample_v3.json")
    if not os.path.isfile(v3):
        print(f"[SKIP] 缺少 {v3}")
        return 1
    src_bank = load(v3)
    parsed = KP.parse_bank(src_bank)
    by_id = {i.item_id: i for i in parsed.items}

    # ---------------- 1. 普通叶子题导出 ----------------
    sel = ["c1:q1", "c1:q2"]
    bank = KEX.build_export_bank(parsed, sel)
    check("导出为 v3.1.0", bank["format_version"] == [3, 1, 0], str(bank["format_version"]))
    check("生成新 uuid", bank["uuid"] and bank["uuid"] != src_bank.get("uuid"))
    check("顶层字段齐全",
          all(k in bank for k in ("format_version", "title", "author",
                                  "description", "uuid", "valid_key", "chapters")))
    check("只保留含选中题的章节", len(bank["chapters"]) == 1, str(len(bank["chapters"])))
    check("该章节含 2 题", len(bank["chapters"][0]["content"]) == 2,
          str(len(bank["chapters"][0]["content"])))

    # 关键：导出 [包含答案与解析]
    q1 = bank["chapters"][0]["content"][0]
    check("导出题目含 correct_answers", q1.get("correct_answers") == ["A", "B", "D"],
          str(q1.get("correct_answers")))
    check("导出题目含 analysis", "analysis" in q1, str(q1.get("analysis"))[:40])
    check("导出题目含 question_type", q1.get("question_type") == "X", str(q1.get("question_type")))
    check("导出题目含 html_content 标记", "html_content" in q1, str(q1.get("html_content")))
    check("选项顺序保真", list(q1.get("options", {}).keys()) == ["A", "B", "C", "D", "E"],
          str(list(q1.get("options", {}).keys())))

    # ---------------- 2. 导出结果可再解析（可再导入 Mini） ----------------
    reparsed = KP.parse_bank(bank)
    check("导出题库可被解析器识别", reparsed.format_version == (3, 1, 0),
          str(reparsed.format_version))
    check("再解析条目数 = 2", reparsed.item_count == 2, str(reparsed.item_count))
    check("再解析答案一致",
          [i.correct for i in reparsed.items] == [by_id["c1:q1"].correct, by_id["c1:q2"].correct],
          str([i.correct for i in reparsed.items]))

    # ---------------- 3. 复合题：只挑部分子题 ----------------
    bank2 = KEX.build_export_bank(parsed, ["c2:q8:s81"])
    a3 = [q for ch in bank2["chapters"] for q in ch["content"]]
    check("A3 只导出选中子题 → 1 个父题", len(a3) == 1, str(len(a3)))
    check("父题保留 shared_stem", bool(a3[0].get("shared_stem")),
          str(a3[0].get("shared_stem"))[:30])
    check("父题 content 只剩 1 个子题", len(a3[0].get("content") or []) == 1,
          str(len(a3[0].get("content") or [])))
    check("保留的子题是 s81",
          (a3[0]["content"][0].get("sub_question_id") == 81),
          str(a3[0]["content"][0].get("sub_question_id")))
    check("子题保留答案", a3[0]["content"][0].get("correct_answers") == ["A"],
          str(a3[0]["content"][0].get("correct_answers")))

    # B 型：共用选项
    bank3 = KEX.build_export_bank(parsed, ["c2:q9:s91", "c2:q9:s92"])
    b_parent = [q for ch in bank3["chapters"] for q in ch["content"]][0]
    check("B 型父题保留 shared_options", bool(b_parent.get("shared_options")),
          str(list((b_parent.get("shared_options") or {}).keys())))
    check("B 型导出 2 个子题", len(b_parent.get("content") or []) == 2,
          str(len(b_parent.get("content") or [])))

    # ---------------- 4. 签名 ----------------
    key_obj = KC.load_key_obj(os.path.join(KALEIDO, "key_obj.json")) \
        if os.path.isfile(os.path.join(KALEIDO, "key_obj.json")) else KC.generate_key_obj()
    crypto = KC.QuestionBankCrypto(key_obj)
    signed = KEX.build_export_bank(parsed, sel, signer=crypto.sign_question_bank)
    check("签名字段长度 344", len(signed.get("valid_key") or "") == 344,
          str(len(signed.get("valid_key") or "")))
    check("导出题库验签通过", crypto.verify_signature(signed).get("isValid") is True,
          str(crypto.verify_signature(signed)))
    unsigned = KEX.build_export_bank(parsed, sel)
    check("未提供 signer 时 valid_key 为空", unsigned.get("valid_key") == "")

    # ---------------- 5. 媒体引用收集 ----------------
    check("无媒体题库引用为空", KEX.collect_media_refs(bank) == [])
    fake_with_media = {
        "chapters": [{"chapter_id": 1, "chapter_name": "c", "content": [{
            "question_id": 1, "question": '<img src="/media/a.png">x',
            "analysis": '<img src="/media/b.png">',
            "options": {"A": '<img src="/media/a.png">'},
        }]}]
    }
    refs = KEX.collect_media_refs(fake_with_media)
    check("媒体引用去重保序", refs == ["a.png", "b.png"], str(refs))

    media, missing = KEX.collect_media(["a.png", "b.png"],
                                       lambda r: b"X" if r == "a.png" else None)
    check("collect_media 取到已有媒体", list(media.keys()) == ["a.png"], str(list(media)))
    check("collect_media 记录缺失", missing == ["b.png"], str(missing))

    # ---------------- 6. Markdown ----------------
    records = {"c1:q1": {"verdict": "wrong", "answer": ["A"]},
               "c1:q2": {"verdict": "correct", "answer": "B"}}
    md = KEX.build_markdown(parsed, sel, records, title="测试错题本")
    check("MD 含一级标题", md.startswith("# 测试错题本"), md.splitlines()[0])
    check("MD 含章节标题", "## 第一章 基础题型" in md)
    check("MD 含正确答案", "**正确答案**" in md)
    check("MD 含我的作答（答错）", "答错" in md)
    check("MD 含我的作答（答对）", "答对" in md)
    check("MD 含题目数量", "导出题目：2 条" in md, [l for l in md.splitlines() if "导出题目" in l])
    check("MD 非空且 > 200 字符", len(md) > 200, str(len(md)))

    # ---------------- 7. HTML ----------------
    doc = KEX.build_html(parsed, sel, records, title="测试错题本")
    check("HTML 是完整文档", doc.lstrip().startswith("<!DOCTYPE html>") and "</html>" in doc)
    check("HTML 含内联样式（自包含）", "<style>" in doc)
    check("HTML 含标题", "测试错题本" in doc)
    check("HTML 含正确答案", "正确答案" in doc)
    check("HTML 无外部脚本", "<script" not in doc)
    check("HTML 含打印样式", "@media print" in doc)

    # 摘要式断言：不要出现未净化的 script
    evil = {
        "format_version": [3, 1, 0], "title": "注入测试", "author": "", "description": "",
        "uuid": "", "valid_key": "",
        "chapters": [{"chapter_id": 1, "chapter_name": "c", "content": [{
            "question_id": 1, "question_type": "A1", "html_content": True,
            "question": '<b>题</b><script>alert(1)</script>',
            "options": {"A": "<i>甲</i>", "B": "乙"},
            "correct_answers": ["A"], "analysis": "<img src=x onerror=alert(2)>",
        }]}],
    }
    evil_parsed = KP.parse_bank(evil)
    doc2 = KEX.build_html(evil_parsed, ["c1:q1"], title="注入测试")
    check("HTML 剥离了 script", "<script" not in doc2.lower(), doc2[:80])
    check("HTML 剥离了 onerror", "onerror" not in doc2.lower())

    # ---------------- 8. 文件名 ----------------
    check("文件名净化非法字符",
          "/" not in KEX.sanitize_filename_part("a/b:c*d") and "\\" not in KEX.sanitize_filename_part("a\\b"),
          KEX.sanitize_filename_part("a/b:c*d"))
    check("文件名保留中文", KEX.sanitize_filename_part("眼科学（错题）") == "眼科学（错题）")
    base = KEX.build_base_filename(parsed, suffix="错题", date_str="20260101")
    check("文件名含题库名与日期", base.endswith("_错题_20260101") and parsed.title in base, base)

    # ---------------- 9. 空选择应报错 ----------------
    try:
        KEX.build_export_bank(parsed, [])
        check("空选择应报错", False)
    except KEX.ExportError:
        check("空选择抛 ExportError", True)

    # ---------------- 10. 真实题库 ----------------
    real = os.path.join(QBANKS, "眼科学", "眼科学题库_System.json")
    if os.path.isfile(real):
        real_parsed = KP.parse_bank(load(real))
        pick = [i.item_id for i in real_parsed.items[:30]]
        real_bank = KEX.build_export_bank(real_parsed, pick, title="真实错题")
        check("真实题库导出 30 题", sum(len(c["content"]) for c in real_bank["chapters"]) == 30,
              str(sum(len(c["content"]) for c in real_bank["chapters"])))
        media_refs = KEX.collect_media_refs(real_bank)
        check("真实题库导出含媒体引用", len(media_refs) > 0, str(len(media_refs)))

        md_real = KEX.build_markdown(real_parsed, pick, {}, title="真实错题本")
        check("真实题库 MD 生成成功", len(md_real) > 500, f"{len(md_real)} 字符")

        # 打 .kqb 并核对（媒体从真实目录读取）
        media_dir = os.path.join(QBANKS, "眼科学", "眼科学题库_System")
        present = set(os.listdir(media_dir)) if os.path.isdir(media_dir) else set()

        def loader(ref, _d=media_dir, _p=present):
            if ref not in _p:
                return None
            with open(os.path.join(_d, ref), "rb") as fp:
                return fp.read()

        got, miss = KEX.collect_media(media_refs, loader)
        check("真实媒体取到字节", len(got) == len(media_refs) - len(miss),
              f"取到 {len(got)} / 缺 {len(miss)}")
        pkg = KQB.build_package(
            KEX.build_export_bank(real_parsed, pick, title="真实错题",
                                  signer=crypto.sign_question_bank),
            got, key_obj=key_obj, placement=KQB.PLACEMENT_SELF_CONTAINED)
        result = KQB.inspect_package(pkg)
        check("真实错题导出包核对通过", result.ok,
              "；".join(f"[{c.title}]{c.detail}" for c in result.failures))
        check("导出包可解回 30 题",
              (result.summary()["bank_summary"] or {}).get("item_count") == 30,
              str((result.summary()["bank_summary"] or {}).get("item_count")))
    else:
        print(f"[SKIP] {real}")

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
