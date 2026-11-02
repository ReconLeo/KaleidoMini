# -*- coding: utf-8 -*-
"""M1 回归：km_package —— .kqb 整包构建 / 核对 / 读取 / 安全防护

核心验收（M1 约定）：
  * 自行打包 → 核对 → 导入闭环
  * 与原 key_obj.json 互通：加密包用原密钥解开
  * 真实数据：把 眼科学题库_System.json 与其 58 个媒体打包，再取回，逐字节一致
  * 安全：zip slip / 绝对路径 / 重复条目 / 加密 zip 条目 / 压缩炸弹 / 嵌套媒体 全部拒绝
  * 分级正确：题库引用缺失媒体只 warn 不 fail（真实眼科学题库就是这种）

运行： python tests/test_package.py
"""

import json
import os
import sys
import warnings
import zipfile
from io import BytesIO

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from kaleido_mini import km_crypto as KC      # noqa: E402
from kaleido_mini import km_package as KP    # noqa: E402

KALEIDO = os.environ.get("KALEIDO_PROJECT_ROOT", r"H:\Coding\Projects\Kaleido\Project")
KEY_FILE = os.path.join(KALEIDO, "key_obj.json")
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


def codes(result):
    return {c.id: c.status for c in result.checks}


def status_of(result, check_id):
    for c in result.checks:
        if c.id == check_id:
            return c.status
    return None


def build_raw_zip(entries, compression=zipfile.ZIP_DEFLATED):
    """构造任意（含恶意）zip，用于安全用例。"""
    buf = BytesIO()
    with warnings.catch_warnings():
        # 故意构造重复条目，屏蔽 zipfile 的 Duplicate name 警告
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buf, "w", compression=compression) as zf:
            for name, data in entries:
                zf.writestr(name, data)
    return buf.getvalue()


def build_zip_with_raw_name(raw_name, data, others):
    """构造含 [反斜杠 / 未归一化] 条目名的 zip。

    注意：zipfile.ZipInfo.__init__ 在 Windows 上会把 os.sep("\\") 替换成 "/"，
    所以直接 writestr("media\\a.png") 会变成 "media/a.png"，测不到反向斜杠。
    这里先构造合法 ZipInfo 再改写 .filename 绕过归一化。
    """
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, blob in others:
            zf.writestr(name, blob)
        zi = zipfile.ZipInfo("placeholder")
        zi.filename = raw_name          # 绕过构造器归一化
        zi.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(zi, data)
    return buf.getvalue()


def main():
    print("=" * 78)
    print("M1 / km_package 回归")
    print("=" * 78)

    if not KC.CRYPTO_AVAILABLE:
        print("缺少 cryptography，跳过。")
        return 1
    have_key = os.path.isfile(KEY_FILE)
    key_obj = load(KEY_FILE) if have_key else None
    if not have_key:
        print(f"[WARN] 未找到 {KEY_FILE}，将用自动生成的密钥跑通流程")
        key_obj = KC.generate_key_obj()
    crypto = KC.QuestionBankCrypto(key_obj)

    # ---------------- 基础题库 ----------------
    bank_path = os.path.join(STD, "QuizBank_sample_v3.json")
    if not os.path.isfile(bank_path):
        print(f"[SKIP] 缺少 {bank_path}")
        return 1
    bank = load(bank_path)
    bank.setdefault("uuid", "11111111-1111-4111-8111-111111111111")
    bank["valid_key"] = ""
    bank = crypto.sign_question_bank(bank)

    # ---------------- 1. 无媒体、加密 bank、自包含密钥 ----------------
    pkg = KP.build_package(bank, {}, kind=KP.KIND_FULL_BANK, key_obj=key_obj,
                           placement=KP.PLACEMENT_SELF_CONTAINED)
    check("打包产出非空 zip", len(pkg) > 0 and zipfile.is_zipfile(BytesIO(pkg)),
          f"{len(pkg)} 字节")
    with zipfile.ZipFile(BytesIO(pkg)) as zf:
        names = zf.namelist()
    check("包内含 manifest.json/bank.json/key_obj.json",
          {"manifest.json", "bank.json", "key_obj.json"} <= set(names), str(names))

    r = KP.inspect_package(pkg)
    check("自包含加密包核对通过（无外部密钥）", r.ok,
          "；".join(f"[{c.title}]{c.detail}" for c in r.failures))
    check("核对项 3 manifest pass", status_of(r, 3) == "pass", str(status_of(r, 3)))
    check("核对项 4 签名 pass", status_of(r, 4) == "pass", str(status_of(r, 4)))
    check("核对项 5 摘要 pass", status_of(r, 5) == "pass", str(status_of(r, 5)))
    check("核对项 6 解密 pass", status_of(r, 6) == "pass", str(status_of(r, 6)))
    check("核对项 7 无媒体 pass", status_of(r, 7) == "pass", str(status_of(r, 7)))
    check("核对项 9 统计一致 pass", status_of(r, 9) == "pass", str(status_of(r, 9)))
    check("bank 加密标记为 True", r.manifest["bank"]["encrypted"] is True)
    check("manifest mode = none", r.manifest["media"]["mode"] == "none")
    check("取出题库 item_count = 13",
          (r.summary()["bank_summary"] or {}).get("item_count") == 13,
          str((r.summary()["bank_summary"] or {}).get("item_count")))
    check("解出的题库标题一致", r.bank_obj.get("title") == bank.get("title"))
    r.raise_if_failed()   # 不抛即通过

    # ---------------- 2. 明文 bank（不加密） ----------------
    pkg_plain = KP.build_package(bank, {}, key_obj=key_obj, placement=KP.PLACEMENT_DETACHED,
                                 encrypt_bank=False)
    with zipfile.ZipFile(BytesIO(pkg_plain)) as zf:
        names_plain = set(zf.namelist())
    check("detached 不含 key_obj.json", "key_obj.json" not in names_plain, str(names_plain))
    r2 = KP.inspect_package(pkg_plain, key_obj=key_obj)
    check("detached 明文包（提供密钥）核对通过", r2.ok,
          "；".join(f"[{c.title}]{c.detail}" for c in r2.failures))
    check("明文 bank 未加密标记", r2.manifest["bank"]["encrypted"] is False)
    check("明文包 valid_key 验签 pass", status_of(r2, 6) == "pass", str(status_of(r2, 6)))

    r3 = KP.inspect_package(pkg_plain)
    check("detached 明文包（不提供密钥）仍可读", "bank.json" not in r3.media)
    check("未提供密钥时验签 skip/warn",
          status_of(r3, 6) in ("skip", "warn"), str(status_of(r3, 6)))

    # ---------------- 2b. 完全无密钥的明文包（仅捆媒体）----------------
    probe_media = {"a.png": b"png" * 10, "b.mp3": b"ID3" + b"\x01" * 50}
    pkg_nokey = KP.build_package(bank, probe_media, key_obj=None,
                                 encrypt_bank=False, encrypt_media=False)
    r_nokey = KP.inspect_package(pkg_nokey)
    check("无密钥明文包可构建且核对通过", r_nokey.ok,
          "；".join(f"[{c.title}]{c.detail}" for c in r_nokey.failures))
    check("无密钥时 placement 自动降级为 detached",
          r_nokey.manifest["key"]["placement"] == KP.PLACEMENT_DETACHED,
          r_nokey.manifest["key"]["placement"])
    check("无密钥包不含 key_obj.json",
          r_nokey.manifest["key"]["path"] is None)
    check("无密钥包 manifest 无签名（warn 而非 fail）",
          r_nokey.manifest["signature"] is None and status_of(r_nokey, 4) == "warn",
          str(status_of(r_nokey, 4)))
    check("无密钥包媒体仍完整",
          all(r_nokey.media.get(n) == b for n, b in probe_media.items()))

    # 需要加密却没给密钥 → 必须明确报错
    try:
        KP.build_package(bank, probe_media, key_obj=None, encrypt_bank=True)
        check("加密包缺密钥应报错", False)
    except KP.KqbKeyError:
        check("加密包缺密钥抛 KqbKeyError", True)

    # ---------------- 3. 加密媒体往返 ----------------
    media = {
        "图 1.png": b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4,
        "img_002.jpg": b"\xff\xd8\xff\xe0" + b"jpeg-body" * 100,
        "audio.mp3": b"ID3" + b"\x00" * 999,
    }
    pkg_media = KP.build_package(bank, media, key_obj=key_obj,
                                 placement=KP.PLACEMENT_DETACHED)
    r4 = KP.inspect_package(pkg_media, key_obj=key_obj)
    check("含媒体加密包核对通过", r4.ok,
          "；".join(f"[{c.title}]{c.detail}" for c in r4.failures))
    check("媒体个数一致", len(r4.media) == len(media), str(len(r4.media)))
    check("媒体逐字节还原一致",
          all(r4.media.get(n) == b for n, b in media.items()),
          str([n for n, b in media.items() if r4.media.get(n) != b]))
    check("媒体 mode = encrypted", r4.manifest["media"]["mode"] == "encrypted")
    check("media_encryption 声明 CryptoJS 兼容",
          r4.manifest["media_encryption"]["crypto_js_compatible"] is True)
    check("中文媒体名保留", "图 1.png" in r4.media)

    pkg_plainmedia = KP.build_package(bank, media, key_obj=key_obj,
                                      placement=KP.PLACEMENT_DETACHED,
                                      encrypt_media=False)
    with zipfile.ZipFile(BytesIO(pkg_plainmedia)) as zf:
        plain_names = zf.namelist()
    check("明文媒体走 media/ 目录",
          all(f"media/{n}" in plain_names for n in media), str(plain_names))
    r5 = KP.inspect_package(pkg_plainmedia, key_obj=key_obj)
    check("明文媒体包核对通过", r5.ok,
          "；".join(f"[{c.title}]{c.detail}" for c in r5.failures))
    check("明文媒体逐字节一致",
          all(r5.media.get(n) == b for n, b in media.items()))

    # ---------------- 4. 篡改检测 ----------------
    with zipfile.ZipFile(BytesIO(pkg_media)) as zf:
        orig = {n: zf.read(n) for n in zf.namelist()}

    def rebuild(mutate):
        entries = dict(orig)
        mutate(entries)
        return build_raw_zip(list(entries.items()))

    r6 = KP.inspect_package(rebuild(lambda e: e.__setitem__(
        "manifest.json", e["manifest.json"].replace(b'"kind": "full_bank"', b'"kind": "tampered"'))),
        key_obj=key_obj)
    check("篡改 manifest → 签名校验失败", not r6.ok and status_of(r6, 4) == "fail",
          str(status_of(r6, 4)))

    def tamper_bank(e):
        raw = bytearray(e["bank.json"])
        idx = raw.find(b'"title"')
        raw[idx + 8] = ord("X") if raw[idx + 8] != ord("X") else ord("Y")
        e["bank.json"] = bytes(raw)

    r7 = KP.inspect_package(rebuild(tamper_bank), key_obj=key_obj)
    check("篡改 bank.json → 摘要校验失败", not r7.ok and status_of(r7, 5) == "fail",
          str(status_of(r7, 5)))

    def tamper_media(e):
        raw = bytearray(e["media.enc"])
        raw[-1] ^= 0xFF
        e["media.enc"] = bytes(raw)

    r8 = KP.inspect_package(rebuild(tamper_media), key_obj=key_obj)
    check("篡改 media.enc → 容器摘要/媒体校验失败",
          not r8.ok and status_of(r8, 7) == "fail", str(status_of(r8, 7)))

    # ---------------- 5. 密钥优先级与不匹配 ----------------
    other_key = KC.generate_key_obj()
    r9 = KP.inspect_package(pkg_media, key_obj=other_key)
    check("用错误密钥 → 核对失败", not r9.ok, str([c.id for c in r9.failures]))
    # detached 包在错误密钥下会先在「manifest 签名」就拦下（正确：fail fast）
    check("错误密钥在核对项 4 被拦下（fail fast）",
          status_of(r9, 4) == "fail", str(status_of(r9, 4)))
    check("错误密钥提示含签名/密钥字样",
          any(("签名" in c.detail or "密钥" in c.detail) for c in r9.failures),
          str([c.detail for c in r9.failures])[:80])

    # 密钥优先级：自包含包内自带密钥时，[以包内密钥为准]；
    # 外部那把无关密钥只用于提示"不是同一把"，不得因此拒绝导入。
    pkg_self = KP.build_package(bank, {}, key_obj=key_obj,
                                placement=KP.PLACEMENT_SELF_CONTAINED)
    r_self = KP.inspect_package(pkg_self, key_obj=other_key)
    check("自包含包 + 无关外部密钥 → 仍核对通过", r_self.ok,
          "；".join(f"[{c.title}]{c.detail}" for c in r_self.failures))
    check("自包含包用包内密钥验签通过", status_of(r_self, 4) == "pass",
          str(status_of(r_self, 4)))
    check("自包含包对不一致的外部密钥给出提示", status_of(r_self, 8) == "warn",
          str(status_of(r_self, 8)))
    check("自包含包仍能解出题库", (r_self.bank_obj or {}).get("title") == bank.get("title"))

    # 纵深防御：构造「manifest 用 key A 签名有效、但媒体用 key B 加密」的包，
    # 证明媒体 MAC 是 [密钥绑定] 的，不只是摘要绑定。
    with zipfile.ZipFile(BytesIO(pkg_media)) as zf:
        layers = {n: zf.read(n) for n in zf.namelist()}
    other_crypto = KC.QuestionBankCrypto(other_key)
    layers["media.enc"] = KP.build_media_container(other_crypto, media)
    man = json.loads(layers["manifest.json"].decode("utf-8"))
    man["media"]["container_sha256"] = KP.sha256_hex(layers["media.enc"])
    man["media"]["container_bytes"] = len(layers["media.enc"])
    man = KP.sign_manifest(man, crypto)          # 仍用 key A 重签 → 清单本身合法
    layers["manifest.json"] = json.dumps(man, indent=2, ensure_ascii=False).encode("utf-8")
    r_cross = KP.inspect_package(
        build_raw_zip(list(layers.items())), key_obj=key_obj)
    check("清单合法但媒体用他钥加密 → 核对项 7 拦下",
          status_of(r_cross, 4) == "pass" and status_of(r_cross, 7) == "fail",
          f"check4={status_of(r_cross, 4)} check7={status_of(r_cross, 7)}")
    check("媒体密钥不符时整体核对失败", not r_cross.ok)

    # detached 包缺密钥
    pkg_det = KP.build_package(bank, {}, key_obj=key_obj, placement=KP.PLACEMENT_DETACHED)
    r10 = KP.inspect_package(pkg_det)
    check("detached 加密包缺密钥 → 核对失败（明确提示）",
          not r10.ok and status_of(r10, 6) == "fail", str(status_of(r10, 6)))
    check("缺密钥提示含「密钥」字样",
          "密钥" in (r10.failures[0].detail if r10.failures else ""),
          r10.failures[0].detail if r10.failures else "")

    # ---------------- 6. 安全防护 ----------------
    safe_bank = json.dumps(bank, ensure_ascii=False).encode()

    cases = [
        ("zip slip ../", [("bank.json", safe_bank), ("../evil.png", b"x"),
                          ("manifest.json", b"{}")]),
        ("绝对路径 /etc/x", [("bank.json", safe_bank), ("/etc/passwd", b"x"),
                           ("manifest.json", b"{}")]),
        ("盘符路径", [("bank.json", safe_bank), ("C:/win.ini", b"x"),
                    ("manifest.json", b"{}")]),
        ("嵌套媒体", [("bank.json", safe_bank), ("media/sub/a.png", b"x"),
                    ("manifest.json", b"{}")]),
        ("媒体名含上级目录", [("bank.json", safe_bank), ("media/..", b"x"),
                        ("manifest.json", b"{}")]),
    ]
    for label, entries in cases:
        rr = KP.inspect_package(build_raw_zip(entries))
        check(f"拒绝 {label}", not rr.ok and status_of(rr, 1) == "fail",
              str([c.detail for c in rr.failures])[:90])

    # 反斜杠条目名：必须绕过 zipfile 的路径归一化才能构造出来。
    # 平台事实：Windows 上 zipfile 读取时会先把 os.sep("\\") 归一为 "/"，
    # 所以 "media\\a.png" 会变成合法的 "media/a.png"（Python 已代为消化）；
    # 真正危险的是 "..\\evil.png" → "../evil.png"，必须由本模块拦下。
    pkg_backslash = build_zip_with_raw_name(
        "..\\evil.png", b"x",
        [("bank.json", safe_bank), ("manifest.json", b"{}")])
    rbs = KP.inspect_package(pkg_backslash)
    check("拒绝 反斜杠路径穿越 ..\\evil.png",
          not rbs.ok and status_of(rbs, 1) == "fail",
          str([c.detail for c in rbs.failures])[:90])

    # 反斜杠但归一后安全的条目，应被接受（不误杀）
    pkg_bs_safe = build_zip_with_raw_name(
        "media\\a.png", b"x",
        [("bank.json", safe_bank), ("manifest.json", b"{}")])
    rbs2 = KP.inspect_package(pkg_bs_safe)
    check("反斜杠安全条目归一后放行（核对项 1 pass）",
          status_of(rbs2, 1) == "pass", str(status_of(rbs2, 1)))

    dup = build_raw_zip([("bank.json", safe_bank), ("bank.json", safe_bank),
                         ("manifest.json", b"{}")])
    rd = KP.inspect_package(dup)
    check("拒绝重复条目", not rd.ok and status_of(rd, 1) == "fail",
          str([c.detail for c in rd.failures])[:80])

    # 压缩炸弹：大量零字节，压缩比极高
    bomb = build_raw_zip([("bank.json", safe_bank),
                          ("media/zeros.png", b"\x00" * (5 * 1024 * 1024)),
                          ("manifest.json", b"{}")])
    rb = KP.inspect_package(bomb)
    check("拒绝高压缩比（炸弹）", not rb.ok and status_of(rb, 1) == "fail",
          str([c.detail for c in rb.failures])[:90])

    # 非 zip 输入
    rn = KP.inspect_package(b"not a zip at all")
    check("非 zip 输入被拒", not rn.ok and status_of(rn, 3) == "fail", str(status_of(rn, 3)))

    # 媒体名非法
    for bad in ("a/b.png", "..", "a\\b.png", "C:x.png", "a\x00b"):
        try:
            KP.validate_media_name(bad)
            check(f"媒体名 {bad!r} 应被拒", False)
        except KP.KqbSecurityError:
            check(f"媒体名 {bad!r} 被拒", True)
    check("合法中文媒体名通过", KP.validate_media_name("图 1.png") == "图 1.png")

    # ---------------- 7. 真实数据：眼科学（含媒体） ----------------
    real_bank_path = os.path.join(QBANKS, "眼科学", "眼科学题库_System.json")
    if os.path.isfile(real_bank_path):
        real_bank = load(real_bank_path)
        real_media = KP.collect_media_from_sibling_dir(real_bank_path)
        check("收集到同名目录下媒体 = 58 个", len(real_media) == 58, str(len(real_media)))

        real_pkg = KP.build_package(
            real_bank, real_media, key_obj=key_obj, placement=KP.PLACEMENT_SELF_CONTAINED)
        rreal = KP.inspect_package(real_pkg)
        check("真实眼科学题库整包核对通过", rreal.ok,
              "；".join(f"[{c.title}]{c.detail}" for c in rreal.failures))
        check("真实题库取回条目数 > 600",
              (rreal.summary()["bank_summary"] or {}).get("item_count", 0) > 600,
              str((rreal.summary()["bank_summary"] or {}).get("item_count")))
        check("媒体引用全部覆盖（核对项 10 pass）",
              status_of(rreal, 10) == "pass", str(status_of(rreal, 10)))
        check("58 个媒体逐字节还原",
              all(rreal.media.get(n) == b for n, b in real_media.items()),
              str([n for n, b in real_media.items() if rreal.media.get(n) != b][:3]))

        # 缺媒体：只 warn 不 fail
        partial = dict(list(real_media.items())[:10])
        pkg_missing = KP.build_package(
            real_bank, partial, key_obj=key_obj, placement=KP.PLACEMENT_SELF_CONTAINED)
        rmiss = KP.inspect_package(pkg_missing)
        check("缺媒体只 warn 不 fail（真实场景）", rmiss.ok,
              "；".join(f"[{c.title}]{c.detail}" for c in rmiss.failures))
        check("缺媒体命中核对项 10 warn", status_of(rmiss, 10) == "warn",
              str(status_of(rmiss, 10)))
        check("manifest 记录 missing_refs = 48",
              len(rmiss.manifest["media"]["missing_refs"]) == 48,
              str(len(rmiss.manifest["media"]["missing_refs"])))
    else:
        print(f"[SKIP] {real_bank_path}")

    # 完全无媒体目录的题库（眼科题库合并，73 引用全缺）
    merged_path = os.path.join(
        QBANKS, "眼科学", "眼科题库（合并）_大连医科大学附属第一医院眼科_1.json")
    if os.path.isfile(merged_path):
        merged_bank = load(merged_path)
        pkg_nomedia = KP.build_package(
            merged_bank, {}, key_obj=key_obj, placement=KP.PLACEMENT_SELF_CONTAINED)
        rnm = KP.inspect_package(pkg_nomedia)
        check("73 个引用全缺媒体的题库仍可导入（warn）", rnm.ok,
              "；".join(f"[{c.title}]{c.detail}" for c in rnm.failures))
        check("全缺媒体时核对项 10 = warn", status_of(rnm, 10) == "warn",
              str(status_of(rnm, 10)))
    else:
        print(f"[SKIP] {merged_path}")

    # ---------------- 8. 错题导出包（kind） ----------------
    pkg_export = KP.build_package(
        bank, {"图 1.png": b"png-bytes"}, kind=KP.KIND_WRONG_ANSWER_EXPORT,
        key_obj=key_obj, placement=KP.PLACEMENT_SELF_CONTAINED)
    rex = KP.inspect_package(pkg_export)
    check("错题导出包核对通过", rex.ok,
          "；".join(f"[{c.title}]{c.detail}" for c in rex.failures))
    check("kind = wrong_answer_export",
          rex.manifest["kind"] == KP.KIND_WRONG_ANSWER_EXPORT, rex.manifest["kind"])

    # ---------------- 9. 幂等 / 可复现 ----------------
    p1 = KP.build_package(bank, media, key_obj=key_obj, placement=KP.PLACEMENT_DETACHED,
                          encrypt_media=False, encrypt_bank=False)
    p2 = KP.build_package(bank, media, key_obj=key_obj, placement=KP.PLACEMENT_DETACHED,
                          encrypt_media=False, encrypt_bank=False)
    with zipfile.ZipFile(BytesIO(p1)) as z1, zipfile.ZipFile(BytesIO(p2)) as z2:
        same_bank = z1.read("bank.json") == z2.read("bank.json")
        same_media = all(z1.read(n) == z2.read(n) for n in z1.namelist() if n.startswith("media/"))
    check("非加密包 bank.json 可复现", same_bank)
    check("非加密包 media 可复现", same_media)
    check("加密包因随机 IV 不复现（预期）",
          KP.build_package(bank, media, key_obj=key_obj, placement=KP.PLACEMENT_DETACHED) !=
          KP.build_package(bank, media, key_obj=key_obj, placement=KP.PLACEMENT_DETACHED))

    # ---------------- 10. manifest 签名与版本前向兼容 ----------------
    base_manifest = {"kqb_format": KP.KQB_FORMAT, "kqb_version": [1, 0, 0], "kind": "x"}
    signed_m = KP.sign_manifest(base_manifest, crypto)
    check("manifest 签名自验通过",
          crypto.verify_text(KP.manifest_sign_text(signed_m), signed_m["signature"]["value_b64"]))
    check("manifest 摘要对 key 顺序不敏感",
          KP.manifest_digest({"a": 1, "b": 2}) == KP.manifest_digest({"b": 2, "a": 1}))
    check("manifest 摘要排除 signature",
          KP.manifest_digest(signed_m) == KP.manifest_digest(base_manifest))

    # 次版本号不同的 manifest 应各自签验一致（不得用模块常量硬套）
    v101 = {"kqb_format": KP.KQB_FORMAT, "kqb_version": [1, 0, 1], "kind": "x"}
    signed_101 = KP.sign_manifest(v101, crypto)
    check("kqb_version 1.0.1 的 manifest 可自验（前向兼容）",
          crypto.verify_text(KP.manifest_sign_text(signed_101),
                             signed_101["signature"]["value_b64"]))
    check("1.0.0 签名不能冒用 1.0.1 版本",
          KP.manifest_sign_text(signed_101) != KP.manifest_sign_text(signed_m))
    check("篡改 kqb_version 导致签名失败",
          not crypto.verify_text(
              KP.manifest_sign_text({**signed_101, "kqb_version": [1, 0, 2]}),
              signed_101["signature"]["value_b64"]))

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
