# -*- coding: utf-8 -*-
"""M2 回归：km_store —— 学科/题库/媒体 增删查、路径安全、.kqb 导入、媒体回退

运行： python tests/test_store.py
"""

import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from kaleido_mini import km_crypto as KC      # noqa: E402
from kaleido_mini import km_package as KP    # noqa: E402
from kaleido_mini import km_store as KS      # noqa: E402

KALEIDO = os.environ.get("KALEIDO_PROJECT_ROOT", r"H:\Coding\Projects\Kaleido\Project")
STD = os.path.join(KALEIDO, "standardization")
QBANKS = os.path.join(KALEIDO, "backup", "qbanks")
SANDBOX = os.path.join(ROOT, "dist", "_test_store")

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


def raises(fn, exc, label):
    try:
        fn()
    except exc:
        return check(label, True)
    except Exception as other:  # noqa: BLE001
        return check(label, False, f"抛了 {type(other).__name__}: {other}")
    return check(label, False, "没有抛异常")


def main():
    print("=" * 78)
    print("M2 / km_store 回归")
    print("=" * 78)

    if os.path.isdir(SANDBOX):
        shutil.rmtree(SANDBOX, ignore_errors=True)
    base = SANDBOX
    root = os.path.join(base, "qbanks")
    os.makedirs(root, exist_ok=True)
    store = KS.QbankStore(root)

    key_obj = KC.load_key_obj(os.path.join(KALEIDO, "key_obj.json")) \
        if os.path.isfile(os.path.join(KALEIDO, "key_obj.json")) else KC.generate_key_obj()
    crypto = KC.QuestionBankCrypto(key_obj)

    # ---------------- 1. 名称校验（路径安全） ----------------
    for bad, label in [
        ("", "空名字"), ("  ", "空白名字"), (".", "点"), ("..", "双点"),
        ("a/b", "正斜杠"), ("a\\b", "反斜杠"), ("a:b", "冒号"),
        ("a*b", "星号"), ("a?b", "问号"), ("a\"b", "引号"), ("a<b", "小于号"),
        ("CON", "保留名 CON"), ("lpt3", "保留名 lpt3"),
        ("x.", "结尾点"),
        ("x" * 200, "过长"),
    ]:
        raises(lambda b=bad: KS.validate_subject_name(b), KS.StorePathError,
               f"学科名拒绝 {label}")

    # 首尾空白是 [规范化] 而非拒绝（更符合用户预期）
    check("学科名首尾空白被规范化",
          KS.validate_subject_name("  x  ") == "x")
    check("合法中文带空格学科名通过",
          KS.validate_subject_name(" 眼科学 题库 ") == "眼科学 题库")
    raises(lambda: KS.validate_bank_filename("a.txt"), KS.StorePathError,
           "题库文件名非 .json 被拒")
    raises(lambda: KS.validate_bank_filename(".json"), KS.StorePathError,
           "题库文件名只有扩展名被拒")
    check("合法题库文件名通过",
          KS.validate_bank_filename("眼科学_System.json") == "眼科学_System.json")
    raises(lambda: KS.validate_media_name("a.exe"), KS.StorePathError,
           "媒体扩展名 .exe 被拒")
    raises(lambda: KS.validate_media_name("a.svg"), KS.StorePathError,
           "媒体扩展名 .svg 被拒（可内嵌脚本）")
    check("媒体 .png 通过", KS.validate_media_name("a.png") == "a.png")
    check("媒体中文名通过", KS.validate_media_name("图 1.png") == "图 1.png")

    # ---------------- 2. 学科 CRUD ----------------
    check("初始无学科", store.list_subjects() == [])
    store.create_subject("眼科学")
    store.create_subject("内科学")
    names = [s["name"] for s in store.list_subjects()]
    check("创建两个学科", names == ["内科学", "眼科学"] or set(names) == {"内科学", "眼科学"},
          str(names))
    raises(lambda: store.create_subject("眼科学"), KS.StoreConflictError, "重复创建学科被拒")
    raises(lambda: store.delete_subject("不存在的学科"), KS.StoreNotFoundError,
           "删除不存在学科报 NotFound")

    # ---------------- 3. 单 JSON 上传 ----------------
    sample = os.path.join(STD, "QuizBank_sample_v3.json")
    if not os.path.isfile(sample):
        print(f"[SKIP] 缺少 {sample}")
        return 1
    with open(sample, "rb") as fp:
        raw = fp.read()

    info = store.write_bank_json("眼科学", "样例题库.json", raw)
    check("上传 v3 题库 item_count=13", info["item_count"] == 13, str(info["item_count"]))
    check("上传非加密库 decrypted_on_upload=False", info["decrypted_on_upload"] is False)
    check("落盘文件存在",
          os.path.isfile(os.path.join(root, "眼科学", "样例题库.json")))

    raises(lambda: store.write_bank_json("眼科学", "样例题库.json", raw),
           KS.StoreConflictError, "同名题库不覆盖时被拒")
    check("覆盖写入成功",
          store.write_bank_json("眼科学", "样例题库.json", raw, overwrite=True)["item_count"] == 13)

    raises(lambda: store.write_bank_json("眼科学", "坏库.json", b"{not json"),
           KS.StoreBankError, "非法 JSON 被拒")
    raises(lambda: store.write_bank_json("眼科学", "坏库.json", b'{"a":1}'),
           KS.StoreBankError, "结构不可识别被拒")
    check("坏库未落盘", not os.path.exists(os.path.join(root, "眼科学", "坏库.json")))

    banks = store.list_banks("眼科学")
    check("列表含 1 个题库", len(banks) == 1, str(len(banks)))
    check("列表摘要含题型统计", banks[0]["type_counts"], str(banks[0]["type_counts"]))

    # 缓存生效：再次列表结果一致
    check("二次列表命中缓存结果一致",
          store.list_banks("眼科学")[0]["item_count"] == banks[0]["item_count"])

    # ---------------- 4. 加密题库上传（带密钥自动解密） ----------------
    plain = load(sample)
    plain["uuid"] = plain.get("uuid") or "22222222-2222-4222-8222-222222222222"
    plain["valid_key"] = ""
    signed = crypto.sign_question_bank(plain)
    envelope = crypto.encrypt_question_bank(signed)
    check("构造加密信封字段正确",
          sorted(envelope.keys()) == ["encrypted_content", "uuid", "valid_key"])

    raises(lambda: store.write_bank_json("眼科学", "加密库.json",
                                         json.dumps(envelope).encode()),
           KS.StoreBankError, "无密钥上传加密库被拒")

    store.save_key_obj(key_obj)
    check("密钥已保存", store.has_key())
    einfo = store.write_bank_json("眼科学", "加密库.json", json.dumps(envelope).encode())
    check("有密钥时加密库解密入库", einfo["decrypted_on_upload"] is True)
    check("解密后条目数正确", einfo["item_count"] == 13, str(einfo["item_count"]))
    with open(os.path.join(root, "眼科学", "加密库.json"), "r", encoding="utf-8") as fp:
        stored = json.load(fp)
    check("落盘为明文（不含 encrypted_content）", "encrypted_content" not in stored)
    check("落盘保留 title", stored.get("title") == plain.get("title"))

    store.delete_bank("眼科学", "加密库.json")
    check("删除加密库成功",
          not os.path.exists(os.path.join(root, "眼科学", "加密库.json")))

    # ---------------- 5. 媒体解析（精确 + 扩展名回退） ----------------
    media_dir = os.path.join(root, "眼科学", "样例题库")
    os.makedirs(media_dir, exist_ok=True)
    for name, blob in (("1.png", b"png-1"), ("2.png", b"png-2"), ("a.mp3", b"mp3")):
        with open(os.path.join(media_dir, name), "wb") as fp:
            fp.write(blob)

    check("list_media 返回 3 个", len(store.list_media("眼科学", "样例题库.json")) == 3)
    path, actual = store.resolve_media("眼科学", "样例题库.json", "1.png")
    check("精确同名解析", actual == "1.png" and os.path.isfile(path))
    path, actual = store.resolve_media("眼科学", "样例题库.json", "1.jpg")
    check("同主名换扩展名回退 1.jpg → 1.png", actual == "1.png", actual)
    raises(lambda: store.resolve_media("眼科学", "样例题库.json", "9.png"),
           KS.StoreNotFoundError, "不存在的媒体报 NotFound")
    raises(lambda: store.resolve_media("眼科学", "样例题库.json", "../x.png"),
           KS.StorePathError, "媒体名含穿越被拒")
    raises(lambda: store.resolve_media("眼科学", "样例题库.json", "sub/x.png"),
           KS.StorePathError, "媒体名含子目录被拒")
    check("媒体 MIME 识别", store.media_mime("a.mp3") == "audio/mpeg")

    # ---------------- 6. 题库详情（媒体自检） ----------------
    detail = store.get_bank_detail("眼科学", "样例题库.json")
    check("详情含章节列表", len(detail["chapters"]) == 2, str(len(detail["chapters"])))
    check("详情 media present_count = 3", detail["media"]["present_count"] == 3)
    check("v3 样例无媒体引用故 missing 为空",
          detail["media"]["missing_count"] == 0, str(detail["media"]["missing"]))
    check("未被引用的媒体被列出",
          set(detail["media"]["unreferenced"]) == {"1.png", "2.png", "a.mp3"},
          str(detail["media"]["unreferenced"]))

    # ---------------- 7. .kqb 导入 ----------------
    media = {"1.png": b"png-1", "2.png": b"png-2"}
    pkg = KP.build_package(signed, media, key_obj=key_obj,
                           placement=KP.PLACEMENT_SELF_CONTAINED)
    imp = store.import_package("内科学", pkg, filename="从整包导入.json")
    check("整包导入条目数 = 13", imp["item_count"] == 13, str(imp["item_count"]))
    check("整包导入媒体 2 个", imp["media_written"] == 2, str(imp["media_written"]))
    check("导入后题库落盘",
          os.path.isfile(os.path.join(root, "内科学", "从整包导入.json")))
    check("导入后媒体落盘",
          os.path.isfile(os.path.join(root, "内科学", "从整包导入", "1.png")))
    check("导入写了溯源侧车",
          store.read_provenance("内科学", "从整包导入.json") is not None)
    prov = store.read_provenance("内科学", "从整包导入.json")
    check("溯源记录 kind", prov.get("kind") == KP.KIND_FULL_BANK, str(prov.get("kind")))
    check("溯源记录 media_mode", prov.get("media_mode") == "encrypted",
          str(prov.get("media_mode")))

    raises(lambda: store.import_package("内科学", pkg, filename="从整包导入.json"),
           KS.StoreConflictError, "重复导入同名被拒")
    check("自动命名导入成功",
          store.import_package("内科学", pkg, overwrite=False)["filename"].endswith(".json"))

    # 坏包：不得落任何文件
    bad_pkg = KP.build_package(signed, {}, key_obj=key_obj,
                               placement=KP.PLACEMENT_SELF_CONTAINED)
    broken = bad_pkg[:-200]   # 截断 zip
    before = set(os.listdir(os.path.join(root, "内科学")))
    raises(lambda: store.import_package("内科学", broken, filename="坏包.json"),
           KS.StoreBankError, "坏包导入被拒")
    after = set(os.listdir(os.path.join(root, "内科学")))
    check("坏包导入未产生任何文件", before == after, f"{before} vs {after}")

    # ---------------- 8. 删除题库（连带媒体 + 侧车） ----------------
    delres = store.delete_bank("内科学", "从整包导入.json")
    check("删除报告移除媒体 2 个", delres["removed_media"] == 2, str(delres))
    check("媒体目录已删",
          not os.path.isdir(os.path.join(root, "内科学", "从整包导入")))
    check("侧车已删",
          not os.path.isfile(os.path.join(root, "内科学", "从整包导入.kqb.json")))
    check("题库文件已删",
          not os.path.exists(os.path.join(root, "内科学", "从整包导入.json")))
    raises(lambda: store.delete_bank("内科学", "从整包导入.json"),
           KS.StoreNotFoundError, "重复删除报 NotFound")

    # ---------------- 9. 删除学科 ----------------
    store.write_bank_json("内科学", "占位.json", raw)
    expect_banks = len(store.list_banks("内科学"))
    raises(lambda: store.delete_subject("内科学"), KS.StoreConflictError,
           "非空学科不确认时被拒")
    sub = store.delete_subject("内科学", force=True)
    check("强制删除学科报告题库数",
          sub["deleted_banks"] == expect_banks,
          f"报告 {sub['deleted_banks']}，实际 {expect_banks}")
    check("学科目录已删", not os.path.isdir(os.path.join(root, "内科学")))

    # ---------------- 10. 真实数据：眼科学题库（缺媒体场景） ----------------
    real = os.path.join(QBANKS, "眼科学", "眼科题库（合并）_大连医科大学附属第一医院眼科_1.json")
    if os.path.isfile(real):
        with open(real, "rb") as fp:
            real_raw = fp.read()
        store.write_bank_json("眼科学", "合并题库.json", real_raw)
        d2 = store.get_bank_detail("眼科学", "合并题库.json")
        check("真实缺媒体题库：缺失 73 个", d2["media"]["missing_count"] == 73,
              str(d2["media"]["missing_count"]))
        check("真实缺媒体题库：条目数 > 600", d2["summary"]["item_count"] > 600,
              str(d2["summary"]["item_count"]))
    else:
        print(f"[SKIP] {real}")

    quiz = os.path.join(QBANKS, "眼科学", "眼科学随堂测试合集_眼科学教研室.json")
    if os.path.isfile(quiz):
        with open(quiz, "rb") as fp:
            store.write_bank_json("眼科学", "随堂测试.json", fp.read())
        # 造一个 1.png，验证 1.jpg 引用被回退解析
        mdir = os.path.join(root, "眼科学", "随堂测试")
        os.makedirs(mdir, exist_ok=True)
        for name in ("0.png", "1.png", "2.png", "3.png", "4.png"):
            with open(os.path.join(mdir, name), "wb") as fp:
                fp.write(b"x")
        d3 = store.get_bank_detail("眼科学", "随堂测试.json")
        check("1.jpg 引用经扩展名回退被解析",
              "1.jpg" in d3["media"]["fallback"] and d3["media"]["fallback"]["1.jpg"] == "1.png",
              str(d3["media"]["fallback"]))
        check("回退后缺失媒体为 0", d3["media"]["missing_count"] == 0,
              str(d3["media"]["missing"]))
    else:
        print(f"[SKIP] {quiz}")

    # ---------------- 11. 清理 ----------------
    shutil.rmtree(SANDBOX, ignore_errors=True)
    check("沙箱清理完成", not os.path.isdir(SANDBOX))

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
