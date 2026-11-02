# -*- coding: utf-8 -*-
"""M1 回归：km_crypto —— 与原 Kaleido 加密方案的逐字兼容性验证。

设计原则：
  * 不只测自洽（自己签自己验），必须测 [与原实现产物互通]：
      - 用原 key_obj.json 解开原加密产物 auth_config.enc
      - 用原 key_obj.json 解开原加密题库 test/QuizBank_sample_v3.json
      - 重新加密已解密题库，密文必须与原文件 [字节相同]（验证序列化/填充/IV 全一致）
  * 敏感内容（auth_config.enc 解出的账号口令）只断言字段存在，[不打印值]。
  * Kaleido 项目路径通过环境变量 KALEIDO_PROJECT_ROOT 覆盖，缺失则相关用例 SKIP。

运行： python tests/test_crypto.py
"""

import contextlib
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from kaleido_mini import km_crypto as K  # noqa: E402

#: 运行期收集到的敏感值（只用于"未被打印"的断言，绝不输出）
SECRETS = []


class _Tee:
    """同时写真实 stdout 与缓冲区，供事后做泄露检测。"""

    def __init__(self, real):
        self.real = real
        self.buf = io.StringIO()

    def write(self, s):
        self.real.write(s)
        self.buf.write(s)
        return len(s)

    def flush(self):
        self.real.flush()

    def getvalue(self):
        return self.buf.getvalue()

KALEIDO = os.environ.get("KALEIDO_PROJECT_ROOT", r"H:\Coding\Projects\Kaleido\Project")
KEY_FILE = os.path.join(KALEIDO, "key_obj.json")
ENC_AUTH = os.path.join(KALEIDO, "auth_config.enc")
ENC_BANK = os.path.join(KALEIDO, "backup", "qbanks", "test", "QuizBank_sample_v3.json")

RESULTS = []


def check(name, cond, detail="", extra=""):
    RESULTS.append((name, bool(cond), detail))
    mark = "PASS" if cond else "FAIL"
    line = f"[{mark}] {name}"
    shown = extra or detail
    if shown:
        line += (f"  ({shown})" if cond else f"  << {shown}")
    print(line)
    return bool(cond)


def load_json(path):
    with open(path, "r", encoding="utf-8-sig") as fp:
        return json.load(fp)


def main():
    print("=" * 78)
    print("M1 / km_crypto 回归")
    print("=" * 78)

    # ---------------- 0. 依赖可用性 ----------------
    if not check("cryptography 可用", K.CRYPTO_AVAILABLE, K.CRYPTO_IMPORT_ERROR):
        print("\n缺少 cryptography，无法继续。")
        return 1

    if not os.path.isfile(KEY_FILE):
        print(f"\n[SKIP] 未找到原密钥文件：{KEY_FILE}")
        print("       设置 KALEIDO_PROJECT_ROOT 后可跑完整互通用例。")
        return 0

    # ---------------- 1. 密钥文件结构与自检 ----------------
    key_obj = K.load_key_obj(KEY_FILE)
    check("密钥文件四字段齐全", list(key_obj.keys()) == list(K.KEY_OBJ_FIELDS),
          ",".join(key_obj.keys()))
    errs = K.validate_key_obj(key_obj)
    check("密钥结构校验通过", not errs, "；".join(errs))
    check("SYMMETRIC_KEY = 32 字节(AES-256)", len(key_obj["SYMMETRIC_KEY"].encode()) == 32)
    check("iv = 16 字节", len(key_obj["iv"].encode()) == 16)

    crypto = K.QuestionBankCrypto(key_obj)
    check("RSA 位数 = 2048", crypto.rsa_bits() == 2048, str(crypto.rsa_bits()))
    fp = crypto.public_key_fingerprint()
    check("公钥指纹前缀 = 805e8677e103df9c", fp.startswith("805e8677e103df9c"), fp[:16])
    check("公私钥配对", crypto.public_key_matches_private_key())

    desc = K.describe_key_obj(key_obj)
    leaked = json.dumps(desc, ensure_ascii=False)
    check("describe 不含 AES 密钥明文", key_obj["SYMMETRIC_KEY"] not in leaked)
    check("describe 不含私钥密文", key_obj["ENCRYPTED_PRIVATE_KEY"][:32] not in leaked)

    v = K.verify_key_obj(key_obj)
    check("verify_key_obj 全链自检通过", v["ok"], str(v.get("error")),
          f"{len(v['steps'])} 步")

    # ---------------- 2. 签名/验签自洽 ----------------
    bank = {"format_version": [3, 0, 0], "title": "回归", "author": "t",
            "description": "d", "uuid": "", "valid_key": "",
            "chapters": [{"chapter_id": 1, "chapter_name": "c", "content": []}]}
    signed = crypto.sign_question_bank(bank)
    check("签名产生 uuid", bool(signed["uuid"]))
    check("签名长度 344 (RSA-2048 → 256B)", len(signed["valid_key"]) == 344,
          str(len(signed["valid_key"])))
    check("验签通过", crypto.verify_signature(signed).get("isValid") is True)

    # 归一化细节：None 键应被剔除，整数值 float 应转 int —— 改动内容应导致验签失败
    mutated = json.loads(json.dumps(signed))
    mutated["title"] = "回归-改"
    check("篡改内容后验签失败", crypto.verify_signature(mutated).get("isValid") is False)

    # ---------------- 3. 与[原实现产物]互通：auth_config.enc ----------------
    if os.path.isfile(ENC_AUTH):
        auth = load_json(ENC_AUTH)
        env = crypto.verify_envelope(auth)
        check("原 auth_config.enc 信封验签通过", env.get("isValid") is True, str(env.get("error")))
        plain = crypto.decrypt_question_bank(auth)
        check("原 auth_config.enc 解密成功", isinstance(plain, dict) and "uuid" in plain)
        check("解密后含预期字段 account/password",
              "account" in plain and "password" in plain,
              ",".join(sorted(plain.keys())))
        # 记录敏感值，供 __main__ 末尾做「全程未打印」检测
        for _field in ("account", "password"):
            _val = plain.get(_field)
            if isinstance(_val, str) and len(_val) >= 4:
                SECRETS.append(_val)
    else:
        print(f"[SKIP] 未找到 {ENC_AUTH}")

    # ---------------- 4. 与[原实现产物]互通：加密题库 ----------------
    if os.path.isfile(ENC_BANK):
        enc = load_json(ENC_BANK)
        check("加密题库字段 = {uuid,valid_key,encrypted_content}",
              sorted(enc.keys()) == ["encrypted_content", "uuid", "valid_key"],
              ",".join(sorted(enc.keys())))
        env = crypto.verify_envelope(enc)
        check("加密题库信封验签通过", env.get("isValid") is True, str(env.get("error")))

        plain = crypto.decrypt_question_bank(enc)
        check("解密得到 v3 结构",
              plain.get("format_version") == [3, 0, 0] and len(plain.get("chapters") or []) == 2,
              f"fv={plain.get('format_version')} chapters={len(plain.get('chapters') or [])}")
        n_top = sum(len(c.get("content") or []) for c in (plain.get("chapters") or []))
        check("解密后顶层题数 = 10", n_top == 10, str(n_top))

        # 关键：明文复原后整体验签必然 False（签名对象是信封）—— 这是原设计
        check("明文复原后整体验签为 False（符合原设计）",
              crypto.verify_signature(plain).get("isValid") is False)

        # 最硬的互通证据：重新加密 → 密文必须与原文件字节一致
        re_enc = crypto.encrypt_question_bank(plain)
        check("重新加密的 encrypted_content 与原文件完全一致",
              re_enc["encrypted_content"] == enc["encrypted_content"])
        check("重新加密的 valid_key 与原文件完全一致（签名确定性）",
              re_enc["valid_key"] == enc["valid_key"])
        check("重新加密的 uuid 与原文件一致", re_enc["uuid"] == enc["uuid"])

        # 重签后可正常验签
        resigned = crypto.sign_question_bank(plain)
        check("重签后明文可验签", crypto.verify_signature(resigned).get("isValid") is True)

        # 篡改检测
        tampered = dict(enc)
        ec = tampered["encrypted_content"]
        tampered["encrypted_content"] = ("0" if ec[0] != "0" else "1") + ec[1:]
        check("篡改密文后信封验签失败", crypto.verify_signature(tampered).get("isValid") is False)
        try:
            crypto.decrypt_question_bank(tampered)
            check("篡改密文解密应抛异常", False)
        except K.SignatureInvalidError:
            check("篡改密文解密抛 SignatureInvalidError", True)
    else:
        print(f"[SKIP] 未找到 {ENC_BANK}")

    # ---------------- 5. 二进制扩展 ----------------
    blob = bytes(range(256)) * 13 + b"\x00tail"
    sealed = crypto.encrypt_bytes(blob, "img_001.png")
    check("二进制 IV = 16 字节", len(sealed["iv"]) == 16)
    check("二进制 MAC = 32 字节", len(sealed["mac"]) == 32)
    check("密文长度按 PKCS7 对齐到 16 的倍数",
          len(sealed["ciphertext"]) % 16 == 0 and len(sealed["ciphertext"]) > len(blob))
    opened = crypto.decrypt_bytes(sealed["ciphertext"], sealed["iv"], sealed["mac"], "img_001.png")
    check("二进制加解密往返一致", opened == blob)

    sealed2 = crypto.encrypt_bytes(blob, "img_001.png")
    check("同一明文两次加密 IV 不同（随机 IV）", sealed["iv"] != sealed2["iv"])
    check("同一明文两次加密密文不同", sealed["ciphertext"] != sealed2["ciphertext"])

    try:
        crypto.decrypt_bytes(sealed["ciphertext"], sealed["iv"], sealed["mac"], "img_002.png")
        check("换文件名解密应失败", False)
    except K.DecryptError:
        check("换文件名解密被 MAC 拦截", True)

    bad = bytearray(sealed["ciphertext"])
    bad[0] ^= 0xFF
    try:
        crypto.decrypt_bytes(bytes(bad), sealed["iv"], sealed["mac"], "img_001.png")
        check("篡改密文解密应失败", False)
    except K.DecryptError:
        check("篡改密文被 MAC 拦截", True)

    # ---------------- 6. 密钥生成（复刻原算法） ----------------
    gen = K.generate_key_obj()
    check("生成密钥四字段", list(gen.keys()) == list(K.KEY_OBJ_FIELDS))
    check("生成 SYMMETRIC_KEY = 32 字符", len(gen["SYMMETRIC_KEY"]) == 32)
    check("生成 iv = 16 字符", len(gen["iv"]) == 16)
    check("生成密钥结构合法", not K.validate_key_obj(gen), "；".join(K.validate_key_obj(gen)))
    gen_crypto = K.QuestionBankCrypto(gen)
    check("生成密钥公私钥配对", gen_crypto.public_key_matches_private_key())
    check("生成密钥指纹与原密钥不同",
          gen_crypto.public_key_fingerprint() != crypto.public_key_fingerprint())
    gv = K.verify_key_obj(gen)
    check("生成密钥全链自检通过", gv["ok"], str(gv.get("error")))

    # 生成的密钥签出的题库，原密钥验签必须失败（密钥隔离）
    gsigned = gen_crypto.sign_question_bank(
        {"probe": 1, "uuid": "", "valid_key": ""})
    check("原密钥无法验证新密钥签名",
          crypto.verify_signature(gsigned).get("isValid") is False)

    # ---------------- 7. 非法密钥拒绝 ----------------
    bad1 = dict(gen)
    bad1["iv"] = "short"
    check("iv 长度非法被拒", bool(K.validate_key_obj(bad1)))
    bad2 = dict(gen)
    bad2["SYMMETRIC_KEY"] = "x" * 20
    check("AES 密钥长度非法被拒", bool(K.validate_key_obj(bad2)))
    bad3 = dict(gen)
    bad3["ENCRYPTED_PRIVATE_KEY"] = "zzzz"
    check("私钥非 hex 被拒", bool(K.validate_key_obj(bad3)))
    try:
        K.QuestionBankCrypto(bad1)
        check("非法密钥实例化应抛异常", False)
    except K.KeyObjectError:
        check("非法密钥实例化抛 KeyObjectError", True)

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
    _tee = _Tee(sys.stdout)
    with contextlib.redirect_stdout(_tee):
        _rc = main()

    # 全程输出泄漏检测：解密出的敏感值不得出现在任何输出中
    _out = _tee.getvalue()
    _leaked = [s for s in SECRETS if s in _out]
    if SECRETS:
        _ok = not _leaked
        sys.stdout.write(
            ("[PASS] 全程输出未泄露解密出的敏感值" if _ok
             else f"[FAIL] 输出中泄露了 {len(_leaked)} 个敏感值")
            + f"  (检测 {len(SECRETS)} 个)\n"
        )
        if not _ok:
            _rc = 1
    sys.exit(_rc)
