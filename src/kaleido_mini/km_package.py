# -*- coding: utf-8 -*-
"""km_package —— KQB v1 整包格式（.kqb）读写与核对

================================================================================
.kqb 是「题库 + 媒体」的整包容器，用于：
  * 含 /media/<name> 相对引用的题库（媒体必须随包走）
  * 错题汇总导出（再导入即可继续练习）
  * 加密分发（bank 与 media 全加密，见 km_crypto 的移植+扩展）

外层是 [标准 zip]，推荐扩展名 .kqb（也接受 .zip）。内部结构：
    manifest.json   必填  清单 + 摘要 + 签名
    bank.json       必填  v1/v2/v3/3.1.0 题库；明文 或 {uuid,valid_key,encrypted_content}
    key_obj.json    可选  placement=self_contained 时随包（全题库密钥文件）
    media/<name>    可选  mode=plain 时的明文媒体，[单段文件名]
    media.enc       可选  mode=encrypted 时的加密媒体容器（见下）
    README.txt      可选  人类可读说明

media.enc 容器（"KQBM" v1，非 zip —— 刻意规避解压炸弹）：
    magic "KQBM"(4) | version u16 | flags u16 | index_len u32 | index_json | blobs
    index_json = [ {name, offset, length, iv_b64, mac_b64, sha256_plaintext, mime} ]
    offset 相对 blobs 起点；每个文件独立 AES-256-CBC + 随机 IV + HMAC-SHA256。
    支持随机访问单个媒体、分块校验、篡改必被 MAC 拦截。

核对分级原则（由真实数据决定）：
  fatal —— 路径安全 / 资源上限 / 清单与摘要 / 解密验签 / 媒体完整性。任一不过 [绝不落盘]。
  warn  —— 媒体引用缺失或冗余、证书指纹不同、统计数不符。
           实测眼科学题库存在「引用 1.jpg 实际 1.png」「73 个引用无媒体目录」这类真实缺陷，
           若把缺媒体定为 fatal，用户手上的真实题库将完全无法导入 —— 故必须降级为 warn。
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import mimetypes
import os
import re
import struct
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

from . import km_crypto as KC

# ---------------------------------------------------------------------------
# 格式常量
# ---------------------------------------------------------------------------
KQB_FORMAT = "kaleido-qbank-package"
KQB_VERSION: Tuple[int, int, int] = (1, 0, 0)
KQB_EXTENSIONS: Tuple[str, ...] = (".kqb", ".zip")

MANIFEST_NAME = "manifest.json"
BANK_NAME = "bank.json"
KEY_OBJ_NAME = "key_obj.json"
MEDIA_DIR = "media"
MEDIA_CONTAINER_NAME = "media.enc"
README_NAME = "README.txt"

MEDIA_CONTAINER_MAGIC = b"KQBM"
MEDIA_CONTAINER_VERSION = 1

MEDIA_MODE_NONE = "none"
MEDIA_MODE_PLAIN = "plain"
MEDIA_MODE_ENCRYPTED = "encrypted"

PLACEMENT_SELF_CONTAINED = "self_contained"
PLACEMENT_DETACHED = "detached"

KIND_WRONG_ANSWER_EXPORT = "wrong_answer_export"
KIND_FULL_BANK = "full_bank"

#: 资源上限（防炸弹 / 防 OOM）
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_ENTRIES = 5000
MAX_ENTRY_BYTES = 200 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100.0

#: 媒体文件名单段校验（只允许常规安全字符，含中文）
_SAFE_MEDIA_NAME = re.compile(r"^[^/\\:*?\"<>|\x00-\x1f]+$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

#: 核对项编号（与 docs/KQB-v1-规范.md 的清单一一对应）
CHECK_LABELS = {
    1: "zip 路径安全",
    2: "资源上限",
    3: "manifest 存在与合法性",
    4: "manifest 签名",
    5: "bank.json 摘要",
    6: "bank 解密与验签",
    7: "媒体完整性",
    8: "密钥指纹一致性",
    9: "格式与统计一致性",
    10: "媒体引用覆盖",
}


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class KqbError(Exception):
    """KQB 处理错误基类。"""


class KqbSecurityError(KqbError):
    """路径穿越 / 解压炸弹 / 加密 zip 等安全类拒绝。"""


class KqbManifestError(KqbError):
    """manifest 缺失或结构非法。"""


class KqbIntegrityError(KqbError):
    """摘要 / MAC 不匹配（内容被改动）。"""


class KqbKeyError(KqbError):
    """密钥缺失或不匹配（加密包需要正确的密钥文件）。"""


class KqbUnsupportedError(KqbError):
    """格式版本不受支持。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _guess_mime(name: str) -> str:
    mime, _ = mimetypes.guess_type(name)
    return mime or "application/octet-stream"


def _compact_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _pretty_json(obj: Any) -> bytes:
    return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _version_str(version: Any) -> str:
    if isinstance(version, (list, tuple)):
        return ".".join(str(int(p)) for p in version)
    return str(version)


def _parse_version(raw: Any) -> Tuple[int, int, int]:
    if isinstance(raw, (list, tuple)):
        parts = [int(p) for p in raw]
    elif isinstance(raw, str):
        parts = [int(p) for p in re.split(r"[.,\s]+", raw.strip()) if p != ""]
    elif isinstance(raw, int):
        parts = [raw]
    else:
        raise KqbManifestError(f"版本字段类型非法：{type(raw).__name__}")
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


def validate_media_name(name: str) -> str:
    """校验媒体文件名为 [单段安全名]，返回规范化结果。"""
    if not isinstance(name, str) or not name.strip():
        raise KqbSecurityError("媒体文件名不能为空")
    cleaned = name.strip()
    if cleaned in (".", ".."):
        raise KqbSecurityError(f"媒体文件名非法：{cleaned!r}")
    if "/" in cleaned or "\\" in cleaned:
        raise KqbSecurityError(f"媒体文件名不得含路径分隔符：{cleaned!r}")
    if _DRIVE_RE.match(cleaned):
        raise KqbSecurityError(f"媒体文件名不得含盘符：{cleaned!r}")
    if not _SAFE_MEDIA_NAME.match(cleaned):
        raise KqbSecurityError(f"媒体文件名含非法字符：{cleaned!r}")
    return cleaned


# ---------------------------------------------------------------------------
# manifest 摘要与签名
# ---------------------------------------------------------------------------
def manifest_digest(manifest: Dict[str, Any]) -> str:
    """manifest 摘要：剔除 signature 后按 [键排序] 紧凑 JSON 求 sha256。

    键排序保证与 dict 插入顺序无关，任何实现（Python/JS）都能复现。
    """
    body = {k: v for k, v in manifest.items() if k != "signature"}
    return sha256_hex(_compact_json(body).encode("utf-8"))


def manifest_sign_text(manifest: Dict[str, Any]) -> str:
    """manifest 签名原文。

    版本取 [manifest 自身声明的 kqb_version]，而不是模块常量 —— 否则将来发布
    v1.1.0 的包会被用 v1.0.0 前缀去验签，必然失败（前向兼容）。
    """
    try:
        ver = _version_str(_parse_version(manifest.get("kqb_version", list(KQB_VERSION))))
    except KqbManifestError:
        ver = _version_str(KQB_VERSION)
    return f"{KQB_FORMAT}|{ver}|{manifest_digest(manifest)}"


def sign_manifest(manifest: Dict[str, Any], crypto: KC.QuestionBankCrypto) -> Dict[str, Any]:
    """为 manifest 追加 RSA 签名段（原地不修改入参）。"""
    signed = dict(manifest)
    signed.pop("signature", None)
    signed["signature"] = {
        "algorithm": f"RSA-{crypto.rsa_bits()}-PKCS1v15-SHA256",
        "target": "sha256(manifest_without_signature)",
        "value_b64": crypto.sign_text(manifest_sign_text(signed)),
        "public_key_fingerprint": crypto.public_key_fingerprint(),
    }
    return signed


# ---------------------------------------------------------------------------
# 统计（供 manifest / 核对使用）
# ---------------------------------------------------------------------------
def _bank_stats(bank_obj: Dict[str, Any]) -> Dict[str, Any]:
    """用 km_parser 计算归一化统计；解析失败时降级为最小信息。

    注意：这里的结果会直接进 .kqb 预检面板（前端渲染 bank_summary.title 等），
    因此除统计外也带上 title/author/description，否则界面上看不到题库名。
    """
    try:
        from . import km_parser as KP

        parsed = KP.parse_bank(bank_obj)
        return {
            "title": parsed.title,
            "author": parsed.author,
            "description": parsed.description,
            "uuid": parsed.uuid,
            "format_version": list(parsed.format_version),
            "chapter_count": parsed.chapter_count,
            "item_count": parsed.item_count,
            "type_counts": parsed.type_counts,
            "media_ref_count": parsed.media_ref_count,
            "media_refs": parsed.media_refs,
            "ok": True,
        }
    except Exception as exc:  # 解析失败不阻断打包，但记录原因
        return {
            "title": str(bank_obj.get("title") or ""),
            "author": str(bank_obj.get("author") or ""),
            "description": "",
            "uuid": str(bank_obj.get("uuid") or ""),
            "format_version": None,
            "chapter_count": None,
            "item_count": None,
            "type_counts": None,
            "media_ref_count": None,
            "media_refs": [],
            "ok": False,
            "parse_error": str(exc),
        }


def _media_refs_of(bank_obj: Dict[str, Any]) -> List[str]:
    try:
        from . import km_parser as KP

        return KP.parse_bank(bank_obj).media_refs
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 媒体容器编解码
# ---------------------------------------------------------------------------
def build_media_container(crypto: KC.QuestionBankCrypto,
                          media: Dict[str, bytes]) -> bytes:
    """构建 media.enc（每文件独立 CBC + 随机 IV + HMAC）。"""
    entries: List[Dict[str, Any]] = []
    blobs = bytearray()
    for name in sorted(media):
        safe_name = validate_media_name(name)
        plaintext = media[name]
        sealed = crypto.encrypt_bytes(plaintext, safe_name)
        ciphertext = sealed["ciphertext"]
        entries.append({
            "name": safe_name,
            "offset": len(blobs),
            "length": len(ciphertext),
            "iv_b64": _b64e(sealed["iv"]),
            "mac_b64": _b64e(sealed["mac"]),
            "sha256_plaintext": sha256_hex(plaintext),
            "mime": _guess_mime(safe_name),
            "plain_bytes": len(plaintext),
        })
        blobs.extend(ciphertext)

    index_json = json.dumps(entries, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    header = (
        MEDIA_CONTAINER_MAGIC
        + struct.pack(">H", MEDIA_CONTAINER_VERSION)
        + struct.pack(">H", 0)                     # flags 保留
        + struct.pack(">I", len(index_json))
    )
    return header + index_json + bytes(blobs)


def parse_media_container(crypto: KC.QuestionBankCrypto,
                          container: bytes) -> Dict[str, bytes]:
    """解析并校验 media.enc，返回 {name: plaintext}。"""
    if len(container) < 12:
        raise KqbIntegrityError("media.enc 过短，不是合法容器")
    if container[:4] != MEDIA_CONTAINER_MAGIC:
        raise KqbIntegrityError("media.enc 魔数不匹配（不是 KQBM 容器）")
    version, _flags, index_len = struct.unpack(">HHI", container[4:12])
    if version != MEDIA_CONTAINER_VERSION:
        raise KqbUnsupportedError(f"media.enc 版本 {version} 不受支持（仅支持 1）")
    if 12 + index_len > len(container):
        raise KqbIntegrityError("media.enc 索引长度越界")
    try:
        index = json.loads(container[12:12 + index_len].decode("utf-8"))
    except Exception as exc:
        raise KqbIntegrityError(f"media.enc 索引不是合法 JSON：{exc}") from exc
    if not isinstance(index, list):
        raise KqbIntegrityError("media.enc 索引必须是数组")

    blobs_base = 12 + index_len
    out: Dict[str, bytes] = {}
    for entry in index:
        if not isinstance(entry, dict):
            raise KqbIntegrityError("media.enc 索引项不是对象")
        name = validate_media_name(entry.get("name", ""))
        if name in out:
            raise KqbIntegrityError(f"media.enc 索引存在重复文件名：{name}")
        offset = int(entry.get("offset", -1))
        length = int(entry.get("length", -1))
        if offset < 0 or length < 0 or blobs_base + offset + length > len(container):
            raise KqbIntegrityError(f"media.enc 中 {name} 的偏移/长度越界")
        ciphertext = container[blobs_base + offset: blobs_base + offset + length]
        try:
            iv = _b64d(entry["iv_b64"])
            mac = _b64d(entry["mac_b64"])
        except Exception as exc:
            raise KqbIntegrityError(f"media.enc 中 {name} 的 iv/mac 不是合法 base64") from exc
        try:
            plaintext = crypto.decrypt_bytes(ciphertext, iv, mac, name)
        except KC.DecryptError as exc:
            raise KqbIntegrityError(str(exc)) from exc
        expected = entry.get("sha256_plaintext")
        if expected and sha256_hex(plaintext) != expected:
            raise KqbIntegrityError(f"媒体 {name} 明文摘要不匹配")
        out[name] = plaintext
    return out


# ---------------------------------------------------------------------------
# 打包
# ---------------------------------------------------------------------------
def build_package(
    bank_obj: Dict[str, Any],
    media: Optional[Dict[str, bytes]] = None,
    *,
    kind: str = KIND_FULL_BANK,
    key_obj: Optional[Dict[str, Any]] = None,
    placement: str = PLACEMENT_SELF_CONTAINED,
    encrypt_bank: bool = True,
    encrypt_media: bool = True,
    readme: str = "",
    extra_manifest: Optional[Dict[str, Any]] = None,
) -> bytes:
    """构建 .kqb 整包，返回 zip 字节。

    :param bank_obj: 题库对象（明文；若 encrypt_bank 会在包内加密）
    :param media: {单段文件名: 字节}
    :param key_obj: 全题库密钥文件；加密 / 签名都需要
    :param placement: self_contained（随包带 key_obj.json）/ detached（不带，需另发密钥）
    :param encrypt_bank: 是否加密 bank.json（默认 True，对应「媒体+bank 全加密」）
    :param encrypt_media: 媒体是否存在 media.enc（否则 media/ 明文）
    """
    if not isinstance(bank_obj, dict):
        raise KqbError("bank_obj 必须是字典")
    if placement not in (PLACEMENT_SELF_CONTAINED, PLACEMENT_DETACHED):
        raise KqbError(f"placement 非法：{placement}")
    media = dict(media or {})
    for name in media:
        validate_media_name(name)
        if not isinstance(media[name], (bytes, bytearray)):
            raise KqbError(f"媒体 {name} 的内容必须是 bytes")

    crypto: Optional[KC.QuestionBankCrypto] = None
    if key_obj is not None:
        crypto = KC.QuestionBankCrypto(key_obj)

    # 只有「确实要加密」时才强制要求密钥；明文打包（仅捆媒体）是合法场景。
    needs_crypto = encrypt_bank or (encrypt_media and bool(media))
    if needs_crypto and crypto is None:
        raise KqbKeyError(
            "本次打包需要密钥文件（bank 或媒体要加密），但未提供 key_obj"
        )
    # 没有密钥可随包时，放置方式只能是 detached（包内不会出现 key_obj.json）
    if crypto is None:
        placement = PLACEMENT_DETACHED

    stats = _bank_stats(bank_obj)
    media_refs = stats.get("media_refs") or []
    missing_refs = [r for r in media_refs if r not in media]
    unreferenced = [n for n in media if n not in media_refs]

    entries: Dict[str, bytes] = {}

    # ---- bank.json ----
    bank_section: Dict[str, Any] = {"path": BANK_NAME}
    if encrypt_bank:
        assert crypto is not None
        envelope = crypto.encrypt_question_bank(bank_obj)
        bank_bytes = _pretty_json(envelope)
        bank_section.update({
            "encrypted": True,
            "cipher": "AES-256-CBC+PKCS7/key_obj.iv",
            "sha256_ciphertext": sha256_hex(bank_bytes),
            "sha256_plaintext": None,
            "uuid": envelope.get("uuid"),
            "valid_key": envelope.get("valid_key"),
        })
    else:
        bank_bytes = _pretty_json(bank_obj)
        bank_section.update({
            "encrypted": False,
            "cipher": None,
            "sha256_ciphertext": None,
            "sha256_plaintext": sha256_hex(bank_bytes),
            "uuid": bank_obj.get("uuid"),
            "valid_key": bank_obj.get("valid_key"),
        })
    bank_section.update({
        "bytes": len(bank_bytes),
        "format_version": stats.get("format_version"),
        "title": bank_obj.get("title"),
        "author": bank_obj.get("author"),
        "chapter_count": stats.get("chapter_count"),
        "item_count": stats.get("item_count"),
        "type_counts": stats.get("type_counts"),
        "media_ref_count": stats.get("media_ref_count"),
    })
    entries[BANK_NAME] = bank_bytes

    # ---- 媒体 ----
    media_section: Dict[str, Any] = {
        "mode": MEDIA_MODE_NONE,
        "dir": None,
        "container": None,
        "count": 0,
        "total_bytes": 0,
        "naming": "basename",
        "files": [],
        "missing_refs": missing_refs,
        "unreferenced": unreferenced,
    }
    media_encryption: Optional[Dict[str, Any]] = None

    if media:
        files_meta = [
            {
                "name": name,
                "bytes": len(media[name]),
                "mime": _guess_mime(name),
                "sha256_plaintext": sha256_hex(media[name]),
            }
            for name in sorted(media)
        ]
        if encrypt_media:
            assert crypto is not None
            container = build_media_container(crypto, media)
            entries[MEDIA_CONTAINER_NAME] = container
            media_section.update({
                "mode": MEDIA_MODE_ENCRYPTED,
                "container": MEDIA_CONTAINER_NAME,
                "container_sha256": sha256_hex(container),
                "container_bytes": len(container),
                "count": len(media),
                "total_bytes": sum(len(v) for v in media.values()),
                "files": files_meta,
            })
            media_encryption = {
                "algorithm": "AES-256-CBC+PKCS7",
                "key_source": "key_obj.SYMMETRIC_KEY",
                "iv_source": "per_file_random",
                "mac": "HMAC-SHA256(name|iv|ciphertext)",
                "mac_key_derive": f"SHA256(utf8(SYMMETRIC_KEY + '{KC.MAC_DERIVE_SALT}'))",
                "crypto_js_compatible": True,
                "note": "纯 CryptoJS 可实现：AES-CBC + Pkcs7 + WordArray.random + HmacSHA256",
            }
        else:
            media_section.update({
                "mode": MEDIA_MODE_PLAIN,
                "dir": MEDIA_DIR,
                "count": len(media),
                "total_bytes": sum(len(v) for v in media.values()),
                "files": files_meta,
            })
            for name, blob in media.items():
                entries[f"{MEDIA_DIR}/{name}"] = blob

    # ---- 随包密钥 ----
    key_section: Dict[str, Any] = {
        "placement": placement,
        "path": None,
        "public_key_sha256": None,
        "rsa_bits": None,
    }
    if crypto is not None:
        key_section["public_key_sha256"] = crypto.public_key_fingerprint()
        key_section["rsa_bits"] = crypto.rsa_bits()
    if placement == PLACEMENT_SELF_CONTAINED and key_obj is not None:
        payload = {f: key_obj[f] for f in KC.KEY_OBJ_FIELDS}
        entries[KEY_OBJ_NAME] = _pretty_json(payload)
        key_section["path"] = KEY_OBJ_NAME

    if readme:
        entries[README_NAME] = (readme.rstrip() + "\n").encode("utf-8")

    # ---- manifest ----
    manifest: Dict[str, Any] = {
        "kqb_format": KQB_FORMAT,
        "kqb_version": list(KQB_VERSION),
        "created_at": _now_iso(),
        "generator": {
            "name": "Kaleido Mini",
            "plugin": "kaleido_mini",
            "version": extra_manifest.get("generator_version", "1.0.0")
            if extra_manifest else "1.0.0",
        },
        "kind": kind,
        "bank": bank_section,
        "key": key_section,
        "media": media_section,
        "integrity": {"algorithm": "sha256", "self_excluded": [MANIFEST_NAME, "SIGNATURE"]},
    }
    if media_encryption is not None:
        manifest["media_encryption"] = media_encryption
    else:
        manifest["media_encryption"] = None
    if extra_manifest:
        for key, value in extra_manifest.items():
            if key not in ("generator_version",):
                manifest[key] = value

    if crypto is not None:
        manifest = sign_manifest(manifest, crypto)
    else:
        manifest["signature"] = None

    entries[MANIFEST_NAME] = _pretty_json(manifest)
    return _write_zip(entries)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _write_zip(entries: Dict[str, bytes]) -> bytes:
    """按 [固定顺序] 写 zip，保证同内容可复现（manifest 放最后）。"""
    order = [BANK_NAME, KEY_OBJ_NAME, MEDIA_CONTAINER_NAME, README_NAME]
    names = [n for n in order if n in entries]
    names += sorted(n for n in entries if n not in order and n != MANIFEST_NAME)
    names += [MANIFEST_NAME]

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name in names:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, entries[name])
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 读取与核对
# ---------------------------------------------------------------------------
@dataclass
class CheckItem:
    id: int
    title: str
    level: str          # "fatal" | "warn"
    status: str         # "pass" | "fail" | "warn" | "skip"
    detail: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "level": self.level,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass
class PackageResult:
    """inspect / read 的统一结果。"""

    ok: bool = True
    checks: List[CheckItem] = field(default_factory=list)
    manifest: Optional[Dict[str, Any]] = None
    bank_obj: Optional[Dict[str, Any]] = None
    media: Dict[str, bytes] = field(default_factory=dict)
    key_obj_in_package: Optional[Dict[str, Any]] = None
    key_obj_used: Optional[Dict[str, Any]] = None

    def add(self, check_id: int, status: str, detail: str = "",
            level: str = "fatal") -> CheckItem:
        item = CheckItem(
            id=check_id,
            title=CHECK_LABELS.get(check_id, f"检查项 {check_id}"),
            level=level,
            status=status,
            detail=detail,
        )
        self.checks.append(item)
        if status == "fail" and level == "fatal":
            self.ok = False
        return item

    @property
    def failures(self) -> List[CheckItem]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def warnings(self) -> List[CheckItem]:
        return [c for c in self.checks if c.status == "warn"]

    def summary(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "fatal_count": len([c for c in self.failures if c.level == "fatal"]),
            "warn_count": len(self.warnings),
            "checks": [c.to_dict() for c in self.checks],
            "manifest": self.manifest,
            "bank_summary": _bank_stats(self.bank_obj) if self.bank_obj else None,
            "media_names": sorted(self.media),
            "media_bytes": sum(len(v) for v in self.media.values()),
            "key_in_package": self.key_obj_in_package is not None,
            "key_public_fingerprint": (
                KC.QuestionBankCrypto(self.key_obj_used).public_key_fingerprint()
                if self.key_obj_used and KC.CRYPTO_AVAILABLE else None
            ),
        }

    def raise_if_failed(self) -> "PackageResult":
        if not self.ok:
            detail = "；".join(f"[{c.title}] {c.detail}" for c in self.failures)
            raise KqbIntegrityError(f"整包核对未通过：{detail}")
        return self


def _open_zip(source: Union[str, bytes, bytearray]) -> zipfile.ZipFile:
    if isinstance(source, (bytes, bytearray)):
        return zipfile.ZipFile(io.BytesIO(bytes(source)))
    if isinstance(source, str):
        if not os.path.isfile(source):
            raise KqbError(f"文件不存在：{source}")
        return zipfile.ZipFile(source)
    raise KqbError(f"不支持的输入类型：{type(source).__name__}")


def _validate_zip_safety(zf: zipfile.ZipFile, result: PackageResult) -> Dict[str, zipfile.ZipInfo]:
    """核对项 1+2：路径安全与资源上限。返回 {name: ZipInfo}。"""
    entries: Dict[str, zipfile.ZipInfo] = {}
    problems: List[str] = []
    total = 0

    if len(zf.infolist()) > MAX_ENTRIES:
        problems.append(f"条目数 {len(zf.infolist())} 超过上限 {MAX_ENTRIES}")

    for info in zf.infolist():
        name = info.filename
        if info.is_dir():
            continue
        # 加密 zip 条目不支持
        if info.flag_bits & 0x1:
            problems.append(f"{name}：zip 条目带密码加密，不支持")
            continue
        # 反斜杠 / 绝对路径 / 盘符 / 上级目录
        if "\\" in name:
            problems.append(f"{name}：含反斜杠")
            continue
        if name.startswith("/") or _DRIVE_RE.match(name):
            problems.append(f"{name}：绝对路径")
            continue
        parts = name.split("/")
        if any(p in ("", ".", "..") for p in parts):
            problems.append(f"{name}：含空段或上级目录")
            continue
        if name in entries:
            problems.append(f"{name}：重复条目")
            continue
        # media/ 下必须单段
        if len(parts) == 2 and parts[0] == MEDIA_DIR:
            try:
                validate_media_name(parts[1])
            except KqbSecurityError as exc:
                problems.append(str(exc))
                continue
        elif parts[0] == MEDIA_DIR:
            problems.append(f"{name}：媒体不得嵌套子目录（仅支持 media/<单段文件名>）")
            continue

        size = info.file_size
        if size > MAX_ENTRY_BYTES:
            problems.append(f"{name}：单文件 {size} 超过上限 {MAX_ENTRY_BYTES}")
            continue
        if info.compress_size > 0 and size > 0:
            ratio = size / float(info.compress_size)
            if ratio > MAX_COMPRESSION_RATIO:
                problems.append(
                    f"{name}：压缩比 {ratio:.1f}:1 超过上限 {MAX_COMPRESSION_RATIO}:1"
                )
                continue
        total += size
        entries[name] = info

    if total > MAX_TOTAL_BYTES:
        problems.append(f"解压总量 {total} 超过上限 {MAX_TOTAL_BYTES}")

    if problems:
        result.add(1, "fail", "；".join(problems[:8]))
        result.add(2, "fail", "存在越界条目，已中止")
    else:
        result.add(1, "pass", f"{len(entries)} 个条目路径安全")
        result.add(2, "pass", f"解压总量 {total} 字节，条目 {len(entries)}")
    return entries


def _read_entry(zf: zipfile.ZipFile, name: str, expected_size: Optional[int] = None) -> bytes:
    data = zf.read(name)
    if expected_size is not None and len(data) != expected_size:
        raise KqbIntegrityError(f"{name} 实际长度 {len(data)} 与声明 {expected_size} 不符")
    if len(data) > MAX_ENTRY_BYTES:
        raise KqbSecurityError(f"{name} 超长，已拒绝")
    return data


def inspect_package(source: Union[str, bytes, bytearray], *,
                    key_obj: Optional[Dict[str, Any]] = None) -> PackageResult:
    """只读核对整包，返回逐项清单。不写盘、无副作用。"""
    result = PackageResult()
    try:
        zf = _open_zip(source)
    except (zipfile.BadZipFile, KqbError) as exc:
        result.add(3, "fail", f"不是合法 zip：{exc}")
        return result

    with zf:
        entries = _validate_zip_safety(zf, result)
        if not result.ok:
            return result

        # ---------- 核对项 3：manifest ----------
        if MANIFEST_NAME not in entries:
            result.add(3, "fail", f"缺少 {MANIFEST_NAME}")
            return result
        try:
            manifest = json.loads(_read_entry(zf, MANIFEST_NAME).decode("utf-8"))
        except Exception as exc:
            result.add(3, "fail", f"manifest 不是合法 JSON：{exc}")
            return result
        if not isinstance(manifest, dict):
            result.add(3, "fail", "manifest 必须是 JSON 对象")
            return result
        if manifest.get("kqb_format") != KQB_FORMAT:
            result.add(3, "fail", f"kqb_format 不是 {KQB_FORMAT}：{manifest.get('kqb_format')!r}")
            return result
        try:
            ver = _parse_version(manifest.get("kqb_version"))
        except KqbManifestError as exc:
            result.add(3, "fail", str(exc))
            return result
        if ver[0] != KQB_VERSION[0]:
            result.add(3, "fail", f"kqb_version {_version_str(ver)} 主版本不受支持")
            return result
        result.manifest = manifest
        result.add(3, "pass", f"kqb_version {_version_str(ver)}，kind={manifest.get('kind')}")

        # ---------- 包内密钥 ----------
        key_in_pkg = None
        if KEY_OBJ_NAME in entries:
            try:
                key_in_pkg = json.loads(_read_entry(zf, KEY_OBJ_NAME).decode("utf-8-sig"))
                errors = KC.validate_key_obj(key_in_pkg)
                if errors:
                    result.add(8, "fail", "包内密钥非法：" + "；".join(errors))
                    return result
                result.key_obj_in_package = key_in_pkg
            except Exception as exc:
                result.add(8, "fail", f"包内 {KEY_OBJ_NAME} 无法解析：{exc}")
                return result

        # ---------- 核对项 4：manifest 签名 ----------
        # 密钥优先级：[包内密钥 > 外部密钥]。
        # 自包含包内自带 key_obj.json 时它就是该包的权威密钥 —— 若仍优先用外部密钥，
        # 只要本机存过另一把密钥（例如换过密钥），自包含包就会一律验签失败。
        # 外部密钥此时只用于"提示你手上这把和包里的不是同一把"。
        package_key = key_in_pkg
        external_key = key_obj
        active_key = package_key if package_key is not None else external_key

        signature = manifest.get("signature")
        signed_fp = str((signature or {}).get("public_key_fingerprint") or "")
        if external_key is not None and package_key is not None:
            try:
                ext_fp = KC.QuestionBankCrypto(external_key).public_key_fingerprint()
                pkg_fp = KC.QuestionBankCrypto(package_key).public_key_fingerprint()
                if ext_fp != pkg_fp:
                    result.add(
                        8, "warn",
                        f"外部密钥 {ext_fp[:16]}… 与包内密钥 {pkg_fp[:16]}… 不同；"
                        "已按包内密钥校验（自包含包不受影响）",
                        level="warn",
                    )
            except KC.KaleidoCryptoError:
                pass

        if signature is None:
            result.add(4, "warn", "manifest 未签名（不阻断导入）", level="warn")
        elif active_key is None:
            result.add(4, "warn", "manifest 已签名但无可用密钥，跳过校验", level="warn")
        else:
            try:
                crypto = KC.QuestionBankCrypto(active_key)
            except KC.KaleidoCryptoError as exc:
                result.add(4, "fail", f"密钥不可用：{exc}")
                return result
            local_fp = crypto.public_key_fingerprint()
            if signed_fp and signed_fp != local_fp and package_key is None:
                result.add(
                    8, "warn",
                    f"manifest 签名公钥指纹 {signed_fp[:16]}… 与所用密钥 {local_fp[:16]}… 不同",
                    level="warn",
                )
            value = (signature or {}).get("value_b64") or ""
            if not value:
                result.add(4, "fail", "signature 缺少 value_b64")
                return result
            if crypto.verify_text(manifest_sign_text(manifest), value):
                result.add(4, "pass", "manifest 签名有效")
            else:
                result.add(4, "fail", "manifest 签名无效（清单被改动或密钥不符）")
                return result

        # ---------- 核对项 5/6：bank ----------
        bank_section = manifest.get("bank") or {}
        bank_entry = bank_section.get("path") or BANK_NAME
        if bank_entry not in entries:
            result.add(5, "fail", f"缺少题库文件 {bank_entry}")
            return result
        bank_bytes = _read_entry(zf, bank_entry, bank_section.get("bytes"))
        encrypted_bank = bool(bank_section.get("encrypted"))

        if encrypted_bank:
            expected = bank_section.get("sha256_ciphertext")
            actual = sha256_hex(bank_bytes)
            if expected and actual != expected:
                result.add(5, "fail", "bank.json 密文摘要不匹配（内容被改动）")
                return result
            result.add(5, "pass", "bank.json 密文摘要一致")
        else:
            expected = bank_section.get("sha256_plaintext")
            actual = sha256_hex(bank_bytes)
            if expected and actual != expected:
                result.add(5, "fail", "bank.json 明文摘要不匹配（内容被改动）")
                return result
            result.add(5, "pass", "bank.json 明文摘要一致")

        try:
            bank_raw = json.loads(bank_bytes.decode("utf-8-sig"))
        except Exception as exc:
            result.add(5, "fail", f"bank.json 不是合法 JSON：{exc}")
            return result

        if encrypted_bank or "encrypted_content" in bank_raw:
            if active_key is None:
                result.add(6, "fail",
                           "题库是加密的，但包内无密钥且未提供密钥文件"
                           "（请提供对应的 key_obj.json / 密钥文件）")
                return result
            try:
                crypto = KC.QuestionBankCrypto(active_key)
                plain = crypto.decrypt_question_bank(bank_raw)
            except KC.SignatureInvalidError as exc:
                result.add(6, "fail", f"加密题库验签失败（密钥不匹配）：{exc}")
                return result
            except KC.KaleidoCryptoError as exc:
                result.add(6, "fail", f"加密题库解密失败：{exc}")
                return result
            result.add(6, "pass", "加密题库解密并验签通过")
            bank_obj = plain
        else:
            bank_obj = bank_raw
            # 明文题库验签只作提示（实测大量合法题库 valid_key 为占位值或他钥签名）
            if active_key is not None and bank_obj.get("valid_key"):
                try:
                    check = KC.QuestionBankCrypto(active_key).verify_signature(bank_obj)
                    if check.get("isValid"):
                        result.add(6, "pass", "题库签名有效", level="warn")
                    else:
                        result.add(
                            6, "warn",
                            f"题库明文签名未通过（{check.get('error', '未知')}）—— "
                            "不影响导入与练习",
                            level="warn",
                        )
                except KC.KaleidoCryptoError:
                    result.add(6, "warn", "无法校验题库签名（密钥不可用）", level="warn")
            else:
                result.add(6, "skip", "题库为明文且无可用密钥，跳过验签", level="warn")

        result.bank_obj = bank_obj
        result.key_obj_used = active_key

        # ---------- 核对项 7：媒体 ----------
        media_section = manifest.get("media") or {}
        mode = media_section.get("mode") or MEDIA_MODE_NONE
        declared_files = {
            str(f.get("name")): f
            for f in (media_section.get("files") or [])
            if isinstance(f, dict) and f.get("name")
        }
        try:
            if mode == MEDIA_MODE_NONE:
                result.add(7, "pass", "无媒体（mode=none）")
            elif mode == MEDIA_MODE_PLAIN:
                media_map: Dict[str, bytes] = {}
                for name in declared_files:
                    entry_name = f"{MEDIA_DIR}/{name}"
                    if entry_name not in entries:
                        result.add(7, "fail", f"清单声明的媒体 {name} 不在包内")
                        return result
                    blob = _read_entry(zf, entry_name)
                    expected = declared_files[name].get("sha256_plaintext")
                    if expected and sha256_hex(blob) != expected:
                        result.add(7, "fail", f"媒体 {name} 摘要不匹配")
                        return result
                    media_map[name] = blob
                # 包内多余媒体
                for name in entries:
                    if name.startswith(f"{MEDIA_DIR}/"):
                        short = name[len(MEDIA_DIR) + 1:]
                        if short not in declared_files:
                            result.add(7, "warn",
                                       f"包内媒体 {short} 未在清单中声明", level="warn")
                result.media = media_map
                result.add(7, "pass", f"明文媒体 {len(media_map)} 个摘要一致")
            elif mode == MEDIA_MODE_ENCRYPTED:
                container_name = media_section.get("container") or MEDIA_CONTAINER_NAME
                if container_name not in entries:
                    result.add(7, "fail", f"缺少媒体容器 {container_name}")
                    return result
                container = _read_entry(zf, container_name)
                expected = media_section.get("container_sha256")
                if expected and sha256_hex(container) != expected:
                    result.add(7, "fail", "媒体容器摘要不匹配")
                    return result
                if crypto is None:
                    if active_key is None:
                        result.add(7, "fail", "媒体容器已加密，但无可用密钥")
                        return result
                    crypto = KC.QuestionBankCrypto(active_key)
                media_map = parse_media_container(crypto, container)
                for name, meta in declared_files.items():
                    if name not in media_map:
                        result.add(7, "fail", f"清单声明的媒体 {name} 不在容器内")
                        return result
                    expected_sha = meta.get("sha256_plaintext")
                    if expected_sha and sha256_hex(media_map[name]) != expected_sha:
                        result.add(7, "fail", f"媒体 {name} 明文摘要不匹配")
                        return result
                result.media = media_map
                result.add(7, "pass", f"加密媒体 {len(media_map)} 个解密且 MAC/摘要均通过")
            else:
                result.add(7, "fail", f"未支持的媒体 mode：{mode!r}")
                return result
        except KqbSecurityError as exc:
            result.add(7, "fail", str(exc))
            return result
        except KqbIntegrityError as exc:
            result.add(7, "fail", str(exc))
            return result

        # ---------- 核对项 9：格式与统计一致性 ----------
        notes: List[str] = []
        stats = _bank_stats(bank_obj)
        if not stats.get("ok"):
            result.add(9, "fail", f"题库结构无法解析：{stats.get('parse_error')}")
            return result
        declared_fv = bank_section.get("format_version")
        if declared_fv and list(declared_fv) != list(stats["format_version"]):
            notes.append(
                f"format_version 声明 {_version_str(declared_fv)} 实际 {_version_str(stats['format_version'])}"
            )
        for field_name, stat_key in (
            ("item_count", "item_count"),
            ("chapter_count", "chapter_count"),
        ):
            declared = bank_section.get(field_name)
            if declared is not None and declared != stats.get(stat_key):
                notes.append(f"{field_name} 声明 {declared} 实际 {stats.get(stat_key)}")
        if bank_section.get("type_counts") and bank_section["type_counts"] != stats.get("type_counts"):
            notes.append("type_counts 与实算不一致")
        if notes:
            result.add(9, "warn", "；".join(notes), level="warn")
        else:
            result.add(9, "pass",
                       f"v{_version_str(stats['format_version'])}，"
                       f"{stats['chapter_count']} 章 {stats['item_count']} 条")

        # ---------- 核对项 10：媒体引用覆盖 ----------
        refs = stats.get("media_refs") or []
        missing = [r for r in refs if r not in result.media]
        unreferenced = [n for n in result.media if n not in refs]
        declared_missing = set(media_section.get("missing_refs") or [])
        undeclared_missing = [m for m in missing if m not in declared_missing]
        if missing:
            detail = f"题库引用 {len(missing)} 个媒体在包内缺失，将降级为占位"
            if undeclared_missing:
                detail += f"（未在清单 missing_refs 中声明：{undeclared_missing[:5]}）"
            result.add(10, "warn", detail, level="warn")
        elif unreferenced:
            result.add(10, "warn", f"包内 {len(unreferenced)} 个媒体未被题库引用", level="warn")
        else:
            result.add(10, "pass", f"媒体引用全部覆盖（{len(refs)} 个）")

    return result


def read_package(source: Union[str, bytes, bytearray], *,
                 key_obj: Optional[Dict[str, Any]] = None) -> PackageResult:
    """核对并读取整包，核对不通过则抛异常（供导入流程调用）。"""
    result = inspect_package(source, key_obj=key_obj)
    return result.raise_if_failed()


# ---------------------------------------------------------------------------
# 便捷：从「题库文件 + 同名媒体目录」直接打包（对应 Kaleido 既有约定）
# ---------------------------------------------------------------------------
def collect_media_from_sibling_dir(bank_path: str) -> Dict[str, bytes]:
    """按 Kaleido 约定收集媒体：题库 <name>.json 的同名目录 <name>/ 下的单段文件。

    :return: {文件名: 字节}
    """
    base = os.path.splitext(os.path.abspath(bank_path))[0]
    if not os.path.isdir(base):
        return {}
    media: Dict[str, bytes] = {}
    for entry in sorted(os.listdir(base)):
        path = os.path.join(base, entry)
        if not os.path.isfile(path):
            continue
        try:
            validate_media_name(entry)
        except KqbSecurityError:
            continue
        with open(path, "rb") as fp:
            media[entry] = fp.read()
    return media


def is_kqb_filename(name: str) -> bool:
    return os.path.splitext(str(name))[1].lower() in KQB_EXTENSIONS


__all__ = [
    "KQB_FORMAT", "KQB_VERSION", "KQB_EXTENSIONS", "CHECK_LABELS",
    "BANK_NAME", "MANIFEST_NAME", "KEY_OBJ_NAME", "MEDIA_DIR", "MEDIA_CONTAINER_NAME",
    "MEDIA_MODE_NONE", "MEDIA_MODE_PLAIN", "MEDIA_MODE_ENCRYPTED",
    "PLACEMENT_SELF_CONTAINED", "PLACEMENT_DETACHED",
    "KIND_WRONG_ANSWER_EXPORT", "KIND_FULL_BANK",
    "KqbError", "KqbSecurityError", "KqbManifestError",
    "KqbIntegrityError", "KqbKeyError", "KqbUnsupportedError",
    "CheckItem", "PackageResult",
    "build_package", "inspect_package", "read_package",
    "build_media_container", "parse_media_container",
    "manifest_digest", "manifest_sign_text", "sign_manifest",
    "validate_media_name", "collect_media_from_sibling_dir", "is_kqb_filename",
    "sha256_hex",
]
