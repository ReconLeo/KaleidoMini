# -*- coding: utf-8 -*-
"""km_crypto —— Kaleido 题库加密方案（完整移植） + 二进制扩展

================================================================================
本模块是 Kaleido 既有题库加密体系的 [逐字级兼容移植]，
外加 .kqb 整包所需的 [二进制扩展]。

移植来源（唯一权威）：
    H:\\Coding\\Projects\\Kaleido\\Project\\kaleido-app.py :: class QuestionBankCrypto

兼容性铁律（改动前务必阅读）：
  * 签名算法、摘要归一化、AES 参数、JSON 序列化参数必须与原实现 [逐字一致]，
    否则 (a) 既有已签名题库验签失败、(b) 既有已加密题库无法解密。
  * 原实现有两个必须复刻的 [非对称细节]：
      1) MD5 归一化会丢弃 dict 中值为 None 的键（_preprocess_data），
         而 encrypted_content 的 JSON 序列化 [不] 做该归一化 → 二者不对称，属原设计。
      2) 加密题库解密后，valid_key 是从外层信封回填的（签名对象是信封而非明文），
         因此 [明文复原后整体验签必然为 False]，这不是缺陷，不得据此报错。
  * 密钥文件 key_obj.json 是 [母密钥]：它既能解密题库，也能解出 auth_config.enc
    中的登录凭据。本模块对其只做内存持有，绝不写日志、绝不返回明文。

本模块 [不依赖 Flask / 框架]，可脱离 FlaskToolkit 独立运行与单测。

二进制扩展（原方案没有，本插件新增，用于 .kqb 媒体容器）：
  * 算法：AES-256-CBC + PKCS7 + [每文件随机 16B IV]，外加 HMAC-SHA256
    （encrypt-then-MAC）实现认证，防篡改/防换文件。
  * 之所以不用 AES-GCM：已实证 crypto-js 4.2.0 内 "GCM" 出现 0 次，
    纯 CryptoJS 无法实现 GCM（其 mode 仅 CBC/CFB/CTR/CTRGladman/OFB/ECB）。
    为保证将来浏览器端可用 [纯 CryptoJS] 完成打包/解包，选择 CBC 路线。
  * MAC 密钥派生：mac_key = SHA256( utf8(SYMMETRIC_KEY + "|KQB-MAC-1") )
    —— 该式在 CryptoJS 中可一行复现：
       CryptoJS.SHA256(CryptoJS.enc.Utf8.parse(SYMMETRIC_KEY + '|KQB-MAC-1'))
  * MAC 覆盖：name_utf8 || iv(16B) || ciphertext，绑定文件名，防置换。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import uuid as _uuid
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# 依赖：cryptography 为可选依赖（惰性降级）。
# 不写入 plugin.json 的 pip_dependencies —— 框架已知缺陷 B1：声明 pip_dependencies
# 的插件在缺依赖机器上会安装成功但 [静默跳过加载]，整插件不可用。
# 改为此处探测，缺失时仅加密相关功能报错，其余功能（纯明文题库练习）不受影响。
# ---------------------------------------------------------------------------
try:  # pragma: no cover - 环境相关
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives import padding as _sym_padding
    from cryptography.hazmat.primitives.asymmetric import padding as _asym_padding
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    CRYPTO_AVAILABLE = True
    CRYPTO_IMPORT_ERROR = ""
except Exception as _exc:  # pragma: no cover
    CRYPTO_AVAILABLE = False
    CRYPTO_IMPORT_ERROR = str(_exc)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class KaleidoCryptoError(Exception):
    """加密/签名/密钥相关错误基类。"""


class CryptoUnavailableError(KaleidoCryptoError):
    """cryptography 依赖缺失。"""


class KeyObjectError(KaleidoCryptoError):
    """密钥文件结构/内容非法。"""


class SignatureInvalidError(KaleidoCryptoError):
    """签名验证失败（密钥不匹配或内容被改）。"""


class DecryptError(KaleidoCryptoError):
    """解密失败（密钥错误或密文损坏）。"""


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: key_obj.json 的四个必需字段（顺序与原实现保持一致，便于人工 diff）
KEY_OBJ_FIELDS: Tuple[str, ...] = (
    "SYMMETRIC_KEY",
    "ENCRYPTED_PRIVATE_KEY",
    "iv",
    "PUBLIC_KEY",
)

#: 原实现默认 RSA 位数
DEFAULT_RSA_BITS = 2048

#: 原实现默认 AES 密钥字节数（AES-256）
DEFAULT_AES_KEY_BYTES = 32

#: 二进制扩展 MAC 派生盐（改动会导致历史 .kqb 全部失效）
MAC_DERIVE_SALT = "|KQB-MAC-1"

#: 合法 AES 密钥长度（字节）
_VALID_AES_KEY_LENS = (16, 24, 32)

#: AES-CBC IV 长度（字节）
_AES_BLOCK_SIZE = 16

#: RSA 签名原文模板（uuid|md5）
_SIGN_JOINER = "|"

__all__ = [
    "CRYPTO_AVAILABLE",
    "CRYPTO_IMPORT_ERROR",
    "KaleidoCryptoError",
    "CryptoUnavailableError",
    "KeyObjectError",
    "SignatureInvalidError",
    "DecryptError",
    "KEY_OBJ_FIELDS",
    "DEFAULT_RSA_BITS",
    "DEFAULT_AES_KEY_BYTES",
    "MAC_DERIVE_SALT",
    "QuestionBankCrypto",
    "generate_key_obj",
    "load_key_obj",
    "save_key_obj",
    "validate_key_obj",
    "describe_key_obj",
    "verify_key_obj",
    "compute_bank_md5",
]


def _require_crypto() -> None:
    """确认 cryptography 可用，否则抛出明确错误（供上层转成 400 提示）。"""
    if not CRYPTO_AVAILABLE:  # pragma: no cover
        raise CryptoUnavailableError(
            "缺少 cryptography 依赖，题库加密/签名功能不可用。"
            "请先执行：pip install \"cryptography>=43.0.3\""
            f"（原始错误：{CRYPTO_IMPORT_ERROR}）"
        )


# ---------------------------------------------------------------------------
# 核心：与 kaleido-app.py 逐字兼容的实现
# ---------------------------------------------------------------------------
class QuestionBankCrypto:
    """Kaleido 题库加解密/签名器。

    用法::

        crypto = QuestionBankCrypto.from_file("key_obj.json")
        signed = crypto.sign_question_bank(bank)          # 生成 valid_key
        crypto.verify_signature(signed)                    # -> {"isValid": True}
        enc = crypto.encrypt_question_bank(signed)         # -> {uuid, valid_key, encrypted_content}
        plain = crypto.decrypt_question_bank(enc)          # 还原完整题库

    与 k_aleido-app.py 的差异（均为 [错误处理] 层面，不影响任何字节结果）：
      * 原实现失败时 `return {"error": ...}`；本实现抛类型化异常，
        避免把 "error" 键混入业务数据。需要兼容返回值时用 *_result 系列方法。
    """

    # ---------------------------------------------------------------- 构造
    def __init__(self, key_obj: Optional[Dict[str, Any]] = None,
                 *, key_file: Optional[str] = None) -> None:
        _require_crypto()
        if key_obj is None:
            if key_file is None:
                raise KeyObjectError("必须提供 key_obj 或 key_file")
            key_obj = load_key_obj(key_file)
        errors = validate_key_obj(key_obj)
        if errors:
            raise KeyObjectError("密钥文件非法：" + "；".join(errors))

        self.key_obj: Dict[str, Any] = dict(key_obj)
        # 原实现：AES 密钥与 IV 直接取字段的 UTF-8 字节（不是 base64 解码！）
        self.aes_key: bytes = self.key_obj["SYMMETRIC_KEY"].encode("utf-8")
        self.aes_iv: bytes = self.key_obj["iv"].encode("utf-8")
        self.public_key = self._load_public_key(self.key_obj["PUBLIC_KEY"])
        # 私钥惰性解封并缓存（仅签名时需要）
        self._private_key = None

    @classmethod
    def from_file(cls, path: str) -> "QuestionBankCrypto":
        return cls(key_file=path)

    # ------------------------------------------------------------ 密钥读取
    def _load_public_key(self, public_key_b64: str):
        """加载公钥。

        原实现字段名叫 PUBLIC_KEY 但内容其实是 [base64 的 SPKI DER]（非 PEM），
        却仍先做 PEM 头尾剥离 —— 对纯 base64 内容是无操作，故保留以兼容可能带头的输入。
        """
        cleaned = re.sub(
            r"-----BEGIN [^-]+-----|-----END [^-]+-----|\n", "", public_key_b64
        ).strip()
        try:
            der = base64.b64decode(cleaned)
            return serialization.load_der_public_key(der)
        except Exception as exc:
            raise KeyObjectError(f"PUBLIC_KEY 无法解析为 SPKI DER 公钥：{exc}") from exc

    def _decrypt_private_key(self):
        """用 AES(SYMMETRIC_KEY, iv) 解封 RSA 私钥 PEM。"""
        if self._private_key is not None:
            return self._private_key
        pem = self._aes_decrypt(self.key_obj["ENCRYPTED_PRIVATE_KEY"])
        try:
            self._private_key = serialization.load_pem_private_key(
                pem.encode("utf-8"), password=None
            )
        except Exception as exc:
            raise KeyObjectError(
                "ENCRYPTED_PRIVATE_KEY 解封失败（密钥文件可能被改动或残缺）："
                f"{exc}"
            ) from exc
        return self._private_key

    # ------------------------------------------------------- 摘要归一化
    @staticmethod
    def _preprocess_data(obj):
        """对齐 JS JSON.stringify 的序列化预处理。

        必须复刻的两点：
          * 整数值的 float → int（JS 无 int/float 之分）
          * dict 中值为 None 的键 → [丢弃]
        """
        if isinstance(obj, float):
            return int(obj) if obj.is_integer() else obj
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if v is not None:
                    out[k] = QuestionBankCrypto._preprocess_data(v)
            return out
        if isinstance(obj, list):
            return [QuestionBankCrypto._preprocess_data(i) for i in obj]
        return obj

    def _calculate_md5(self, data: Dict[str, Any]) -> str:
        """计算签名用 MD5（紧凑 JSON / 保持插入序 / 不转义非 ASCII）。"""
        processed = self._preprocess_data(data)
        text = json.dumps(
            processed, sort_keys=False, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    # ---------------------------------------------------------- AES 原语
    def _aes_encrypt(self, content: str) -> str:
        """AES-256-CBC + PKCS7 加密，返回 hex（原实现语义）。"""
        padder = _sym_padding.PKCS7(128).padder()
        padded = padder.update(content.encode("utf-8")) + padder.finalize()
        enc = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.aes_iv)).encryptor()
        return (enc.update(padded) + enc.finalize()).hex()

    def _aes_decrypt(self, encrypted_hex: str) -> str:
        """AES-256-CBC + PKCS7 解密（原实现语义）。"""
        try:
            raw = bytes.fromhex(encrypted_hex)
        except ValueError as exc:
            raise DecryptError(f"密文不是合法 hex：{exc}") from exc
        try:
            dec = Cipher(algorithms.AES(self.aes_key), modes.CBC(self.aes_iv)).decryptor()
            padded = dec.update(raw) + dec.finalize()
            unpadder = _sym_padding.PKCS7(128).unpadder()
            return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
        except Exception as exc:
            raise DecryptError(f"AES 解密失败（密钥不匹配或密文损坏）：{exc}") from exc

    # -------------------------------------------------------------- 签名
    def sign_text(self, text: str) -> str:
        """对任意文本做 RSA-PKCS1v15-SHA256 签名，返回 base64。"""
        private_key = self._decrypt_private_key()
        sig = private_key.sign(
            text.encode("utf-8"), _asym_padding.PKCS1v15(), hashes.SHA256()
        )
        return base64.b64encode(sig).decode("utf-8")

    def verify_text(self, text: str, signature_b64: str) -> bool:
        """校验任意文本的 RSA 签名，返回 bool（不抛异常）。"""
        try:
            self.public_key.verify(
                base64.b64decode(signature_b64),
                text.encode("utf-8"),
                _asym_padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return True
        except Exception:
            return False

    def generate_signature(self, uuid_str: str, md5_hash: str) -> str:
        """原实现：对 f"{uuid}|{md5}" 签名。"""
        return self.sign_text(f"{uuid_str}{_SIGN_JOINER}{md5_hash}")

    def verify_signature(self, signed_data: Dict[str, Any]) -> Dict[str, Any]:
        """验签题库，返回 {"isValid": bool, "error"?: str}（与原实现返回结构一致）。

        注意：明文题库的 valid_key 可能是占位随机值（实测 33 个真实题库中 12 个如此），
        调用方 [不得] 把验证失败当作拒绝理由，只应作为角标提示。
        """
        try:
            if "uuid" not in signed_data or "valid_key" not in signed_data:
                raise ValueError("题库必须包含 uuid 与 valid_key 字段")
            signature = signed_data["valid_key"]
            uuid_str = signed_data["uuid"]
            if not isinstance(signature, str) or not signature:
                raise ValueError("valid_key 为空")
            body = dict(signed_data)
            del body["valid_key"]
            md5_hash = self._calculate_md5(body)
            ok = self.verify_text(f"{uuid_str}{_SIGN_JOINER}{md5_hash}", signature)
            if ok:
                return {"isValid": True}
            return {"isValid": False, "error": "签名不匹配（非本密钥签名或内容已改动）"}
        except Exception as exc:
            return {"isValid": False, "error": str(exc)}

    # ------------------------------------------------------- 题库级操作
    def sign_question_bank(self, raw_data: Dict[str, Any]) -> Dict[str, Any]:
        """为题库生成/覆盖 valid_key（uuid 为空则自动生成 v4 UUID）。"""
        data = dict(raw_data)
        if "uuid" not in data or "valid_key" not in data:
            raise KaleidoCryptoError("输入对象必须包含 uuid 与 valid_key 字段（允许空字符串）")

        uuid_str = data["uuid"]
        if uuid_str:
            try:
                _uuid.UUID(str(uuid_str))
            except (ValueError, AttributeError) as exc:
                raise KaleidoCryptoError(f"uuid 格式无效：{uuid_str!r}") from exc
        else:
            uuid_str = str(_uuid.uuid4())
            data["uuid"] = uuid_str

        body = dict(data)
        del body["valid_key"]
        data["valid_key"] = self.generate_signature(uuid_str, self._calculate_md5(body))
        return data

    def encrypt_question_bank(self, raw_bank: Dict[str, Any]) -> Dict[str, Any]:
        """加密题库：仅保留 uuid / valid_key / encrypted_content。

        注意：待加密内容的 JSON 序列化 [不] 经过 _preprocess_data（保留 None），
        这是原实现的既有行为，改动会导致与历史密文不兼容。
        """
        if "uuid" not in raw_bank or "valid_key" not in raw_bank:
            raise KaleidoCryptoError("原始题库必须包含 uuid 与 valid_key 字段")

        copy = dict(raw_bank)
        uuid_str = copy.pop("uuid")
        copy.pop("valid_key")
        content_text = json.dumps(
            copy, sort_keys=False, separators=(",", ":"), ensure_ascii=False
        )
        encrypted = self._aes_encrypt(content_text)
        return self.sign_question_bank(
            {"uuid": uuid_str, "valid_key": "", "encrypted_content": encrypted}
        )

    def verify_envelope(self, encrypted_bank: Dict[str, Any]) -> Dict[str, Any]:
        """只校验加密信封的签名（不解密）。"""
        for field in ("uuid", "valid_key", "encrypted_content"):
            if field not in encrypted_bank:
                return {"isValid": False, "error": f"加密题库缺少字段 {field}"}
        return self.verify_signature(encrypted_bank)

    def decrypt_question_bank(self, encrypted_bank: Dict[str, Any]) -> Dict[str, Any]:
        """解密题库：先验签信封，再还原完整明文题库。

        原实现会把 [信封的] valid_key 回填进明文，因此解密结果的整体验签必然失败，
        这是设计使然（签名对象是信封）。如需明文可验签，调用 sign_question_bank 重签。
        """
        for field in ("uuid", "valid_key", "encrypted_content"):
            if field not in encrypted_bank:
                raise DecryptError(f"加密题库必须包含 uuid、valid_key、encrypted_content 字段")

        result = self.verify_envelope(encrypted_bank)
        if not result.get("isValid"):
            raise SignatureInvalidError(
                "加密题库验签失败：" + str(result.get("error", "未知原因"))
            )

        try:
            content = json.loads(self._aes_decrypt(encrypted_bank["encrypted_content"]))
        except DecryptError:
            raise
        except Exception as exc:
            raise DecryptError(f"解密内容不是合法 JSON：{exc}") from exc
        if not isinstance(content, dict):
            raise DecryptError("解密内容不是 JSON 对象")

        content["uuid"] = encrypted_bank["uuid"]
        content["valid_key"] = encrypted_bank["valid_key"]
        return content

    # ------------------------------------------------------ 元信息 / 指纹
    def public_key_fingerprint(self) -> str:
        """公钥 SPKI DER 的 sha256 十六进制摘要（用于识别"是不是同一套密钥"）。"""
        cleaned = re.sub(
            r"-----BEGIN [^-]+-----|-----END [^-]+-----|\n",
            "",
            self.key_obj["PUBLIC_KEY"],
        ).strip()
        return hashlib.sha256(base64.b64decode(cleaned)).hexdigest()

    def rsa_bits(self) -> int:
        return int(self.public_key.key_size)

    def public_key_matches_private_key(self) -> bool:
        """自检：私钥解封出的公钥与 PUBLIC_KEY 是否配对。"""
        try:
            private_key = self._decrypt_private_key()
            return (
                private_key.public_key().public_numbers()
                == self.public_key.public_numbers()
            )
        except KaleidoCryptoError:
            return False

    # ------------------------------------------- 二进制扩展（.kqb 媒体用）
    def _mac_key(self) -> bytes:
        """派生 MAC 密钥（与 CryptoJS 表达式一一对应）。"""
        material = self.key_obj["SYMMETRIC_KEY"] + MAC_DERIVE_SALT
        return hashlib.sha256(material.encode("utf-8")).digest()

    def _compute_mac(self, name: str, iv: bytes, ciphertext: bytes) -> bytes:
        msg = name.encode("utf-8") + iv + ciphertext
        return hmac.new(self._mac_key(), msg, hashlib.sha256).digest()

    def encrypt_bytes(self, plaintext: bytes, name: str) -> Dict[str, Any]:
        """二进制加密（CBC + 每文件随机 IV + HMAC-SHA256）。

        :param plaintext: 明文
        :param name: 单段文件名（同时参与 MAC，防换名/置换）
        :return: {"ciphertext": bytes, "iv": bytes(16), "mac": bytes(32)}
        """
        iv = os.urandom(_AES_BLOCK_SIZE)
        padder = _sym_padding.PKCS7(128).padder()
        padded = padder.update(plaintext) + padder.finalize()
        enc = Cipher(algorithms.AES(self.aes_key), modes.CBC(iv)).encryptor()
        ciphertext = enc.update(padded) + enc.finalize()
        return {
            "ciphertext": ciphertext,
            "iv": iv,
            "mac": self._compute_mac(name, iv, ciphertext),
        }

    def decrypt_bytes(self, ciphertext: bytes, iv: bytes, mac: bytes, name: str) -> bytes:
        """二进制解密（先常量时间校验 MAC，再解密）。"""
        if len(iv) != _AES_BLOCK_SIZE:
            raise DecryptError(f"IV 长度非法：{len(iv)}（应为 {_AES_BLOCK_SIZE}）")
        expected = self._compute_mac(name, iv, ciphertext)
        if not hmac.compare_digest(expected, mac):
            raise DecryptError(
                f"媒体 {name!r} 完整性校验失败（MAC 不匹配：文件被篡改或密钥不符）"
            )
        try:
            dec = Cipher(algorithms.AES(self.aes_key), modes.CBC(iv)).decryptor()
            padded = dec.update(ciphertext) + dec.finalize()
            unpadder = _sym_padding.PKCS7(128).unpadder()
            return unpadder.update(padded) + unpadder.finalize()
        except Exception as exc:
            raise DecryptError(f"媒体 {name!r} 解密失败：{exc}") from exc


# ---------------------------------------------------------------------------
# 密钥文件：校验 / 生成 / 读写
# ---------------------------------------------------------------------------
def validate_key_obj(key_obj: Any) -> list:
    """校验密钥对象结构，返回错误列表（空列表 = 合法）。"""
    errors: list = []
    if not isinstance(key_obj, dict):
        return ["密钥对象必须是 JSON 对象"]

    for field in KEY_OBJ_FIELDS:
        if field not in key_obj:
            errors.append(f"缺少字段 {field}")
        elif not isinstance(key_obj[field], str):
            errors.append(f"字段 {field} 必须是字符串")
    if errors:
        return errors

    sym = key_obj["SYMMETRIC_KEY"]
    sym_len = len(sym.encode("utf-8"))
    if sym_len not in _VALID_AES_KEY_LENS:
        errors.append(
            f"SYMMETRIC_KEY 长度 {sym_len} 字节非法（AES 仅支持 "
            f"{'/'.join(str(n) for n in _VALID_AES_KEY_LENS)} 字节）"
        )

    iv_len = len(key_obj["iv"].encode("utf-8"))
    if iv_len != _AES_BLOCK_SIZE:
        errors.append(f"iv 长度 {iv_len} 字节非法（AES-CBC 需 {_AES_BLOCK_SIZE} 字节）")

    try:
        bytes.fromhex(key_obj["ENCRYPTED_PRIVATE_KEY"])
    except ValueError:
        errors.append("ENCRYPTED_PRIVATE_KEY 不是合法 hex")
    else:
        if not CRYPTO_AVAILABLE:
            errors.append("缺少 cryptography 依赖，无法校验密钥内容")
        else:
            try:
                cleaned = re.sub(
                    r"-----BEGIN [^-]+-----|-----END [^-]+-----|\n",
                    "",
                    key_obj["PUBLIC_KEY"],
                ).strip()
                serialization.load_der_public_key(base64.b64decode(cleaned))
            except Exception as exc:
                errors.append(f"PUBLIC_KEY 无法解析：{exc}")
    return errors


def describe_key_obj(key_obj: Dict[str, Any]) -> Dict[str, Any]:
    """返回可安全外发的密钥摘要（[绝不] 含 SYMMETRIC_KEY / 私钥）。"""
    info: Dict[str, Any] = {
        "fields_ok": not validate_key_obj(key_obj),
        "rsa_bits": None,
        "public_key_fingerprint": None,
        "fingerprint_short": None,
        "aes_key_bytes": None,
        "iv_bytes": None,
    }
    try:
        if CRYPTO_AVAILABLE:
            crypto = QuestionBankCrypto(key_obj)
            info["rsa_bits"] = crypto.rsa_bits()
            fp = crypto.public_key_fingerprint()
            info["public_key_fingerprint"] = fp
            info["fingerprint_short"] = fp[:16]
            info["aes_key_bytes"] = len(crypto.aes_key)
            info["iv_bytes"] = len(crypto.aes_iv)
            info["public_matches_private"] = crypto.public_key_matches_private_key()
    except KaleidoCryptoError as exc:
        info["describe_error"] = str(exc)
    return info


def generate_key_obj(rsa_bits: int = DEFAULT_RSA_BITS,
                     aes_key_bytes: int = DEFAULT_AES_KEY_BYTES) -> Dict[str, str]:
    """生成新的密钥对象（[逐字复刻] 原 generate_key_obj 算法）。

    关键复刻点：SYMMETRIC_KEY 与 iv 都是 base64url(随机字节) 再 [截断] 到
    32 / 16 个字符 —— 不是标准 base64 编码值。虽然降低熵，但这是既有格式，
    改动会导致新密钥与完整版不通用。
    """
    _require_crypto()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=int(rsa_bits))
    public_key = private_key.public_key()

    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_der_b64 = base64.b64encode(
        public_key.public_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode("utf-8")

    symmetric_key = base64.urlsafe_b64encode(os.urandom(aes_key_bytes)).decode("utf-8")[
        :aes_key_bytes
    ]
    iv = base64.urlsafe_b64encode(os.urandom(_AES_BLOCK_SIZE)).decode("utf-8")[
        :_AES_BLOCK_SIZE
    ]

    padder = _sym_padding.PKCS7(128).padder()
    padded = padder.update(private_pem.encode("utf-8")) + padder.finalize()
    enc = Cipher(
        algorithms.AES(symmetric_key.encode("utf-8")), modes.CBC(iv.encode("utf-8"))
    ).encryptor()
    encrypted_private = (enc.update(padded) + enc.finalize()).hex()

    return {
        "SYMMETRIC_KEY": symmetric_key,
        "ENCRYPTED_PRIVATE_KEY": encrypted_private,
        "iv": iv,
        "PUBLIC_KEY": public_der_b64,
    }


def load_key_obj(path: str) -> Dict[str, Any]:
    """读取密钥文件（utf-8-sig 兼容 BOM —— 框架已知缺陷 FTK-001 的同类规避）。"""
    if not os.path.isfile(path):
        raise KeyObjectError(f"密钥文件不存在：{path}")
    try:
        with open(path, "r", encoding="utf-8-sig") as fp:
            obj = json.load(fp)
    except json.JSONDecodeError as exc:
        raise KeyObjectError(f"密钥文件不是合法 JSON：{exc}") from exc
    except OSError as exc:
        raise KeyObjectError(f"密钥文件读取失败：{exc}") from exc
    return obj


def save_key_obj(key_obj: Dict[str, Any], path: str) -> str:
    """保存密钥文件（固定四字段、2 空格缩进，便于与完整版互认）。"""
    errors = validate_key_obj(key_obj)
    if errors:
        raise KeyObjectError("拒绝保存非法密钥：" + "；".join(errors))
    payload = {field: key_obj[field] for field in KEY_OBJ_FIELDS}
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fp:
        json.dump(payload, fp, indent=2, ensure_ascii=False)
        fp.write("\n")
    os.replace(tmp, path)
    return path


def verify_key_obj(key_obj: Dict[str, Any]) -> Dict[str, Any]:
    """密钥自检：结构 → 公钥解析 → 私钥解封配对 → 签名/验签 → 题库加解密。

    返回 {"ok": bool, "steps": [...], "error": str|None}
    """
    steps: list = []

    def _step(name: str, ok: bool, detail: str = "") -> bool:
        steps.append({"step": name, "ok": ok, "detail": detail})
        return ok

    errors = validate_key_obj(key_obj)
    if not _step("结构校验", not errors, "；".join(errors)):
        return {"ok": False, "steps": steps, "error": "；".join(errors)}

    try:
        crypto = QuestionBankCrypto(key_obj)
    except KaleidoCryptoError as exc:
        _step("实例化", False, str(exc))
        return {"ok": False, "steps": steps, "error": str(exc)}
    _step("实例化", True)

    paired = crypto.public_key_matches_private_key()
    if not _step("公私钥配对", paired, "" if paired else "PUBLIC_KEY 与私钥不匹配"):
        return {"ok": False, "steps": steps, "error": "PUBLIC_KEY 与私钥不匹配"}

    try:
        signed = crypto.sign_question_bank(
            {"probe": "密钥自检", "uuid": "", "valid_key": ""}
        )
        verified = crypto.verify_signature(signed).get("isValid", False)
        _step("签名/验签", verified)
        if not verified:
            return {"ok": False, "steps": steps, "error": "签名无法自验"}

        encrypted = crypto.encrypt_question_bank(signed)
        decrypted = crypto.decrypt_question_bank(encrypted)
        roundtrip = decrypted.get("probe") == "密钥自检"
        _step("题库加解密", roundtrip, "" if roundtrip else "往返内容不一致")
        if not roundtrip:
            return {"ok": False, "steps": steps, "error": "加解密往返失败"}

        blob = b"\x00\x01binary-probe\xff" * 7
        sealed = crypto.encrypt_bytes(blob, "probe.bin")
        opened = crypto.decrypt_bytes(
            sealed["ciphertext"], sealed["iv"], sealed["mac"], "probe.bin"
        )
        bin_ok = opened == blob
        _step("二进制扩展", bin_ok, "" if bin_ok else "二进制往返不一致")
        if not bin_ok:
            return {"ok": False, "steps": steps, "error": "二进制加解密往返失败"}
    except KaleidoCryptoError as exc:
        steps.append({"step": "运行期", "ok": False, "detail": str(exc)})
        return {"ok": False, "steps": steps, "error": str(exc)}

    return {"ok": True, "steps": steps, "error": None}


def compute_bank_md5(bank: Dict[str, Any]) -> str:
    """便捷函数：对题库对象计算签名用 MD5（自动剔除 valid_key 字段）。

    与 QuestionBankCrypto._calculate_md5 完全同源，供外部预览/比对使用。
    """
    body = dict(bank)
    body.pop("valid_key", None)
    processed = QuestionBankCrypto._preprocess_data(body)
    text = json.dumps(
        processed, sort_keys=False, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.md5(text.encode("utf-8")).hexdigest()
