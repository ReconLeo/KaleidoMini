# -*- coding: utf-8 -*-
"""km_store —— 题库存储层（学科 / 题库 / 媒体 的增删查与路径安全）

================================================================================
存储布局（全部位于插件自属数据目录，capabilities 隐式豁免，无需声明）::

    <data_dir>/qbanks/
    ├── <学科 A>/
    │   ├── <题库 1>.json          题库本体（明文）
    │   ├── <题库 1>/              媒体目录（与题库文件 [同主名] —— Kaleido 既有约定）
    │   │   ├── 0.png
    │   │   └── 1.png
    │   ├── <题库 1>.kqb.json      导入来源的溯源侧车（可选）
    │   └── <题库 2>.json
    └── <学科 B>/

设计要点：
  1. [存储的是明文]。`.kqb` 整包（可能加密）在导入时被解包解密，落盘为明文题库 + 明文媒体。
     理由：Mini 是本地练习工具，落盘明文保证无密钥也能练习；加密的意义在于 [分发传输]。
     代价：数据目录内为明文（与"直接上传明文题库"的暴露面一致）。
  2. 媒体 [单段文件名]，绝不接受嵌套；解析时支持 [同主名换扩展名回退]
     （实测：眼科学随堂测试合集引用 1.jpg，实际文件是 1.png）。
  3. 一切路径先经 [_validate_component] 校验再做 realpath 前缀比对，防穿越。
  4. 写入一律「临时文件 + os.replace」原子替换，避免半写坏库。
  5. 解析摘要按 (路径, mtime_ns, size) 缓存，避免每次列表都重解析大题库。

本模块 [不依赖 Flask / 框架]，可独立单测。
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid as _uuid
from typing import Any, Dict, List, Optional, Tuple

from . import km_crypto as KC
from . import km_parser as KP
from . import km_package as KQB

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 允许的媒体扩展名（对齐完整版并适度扩充；[不含 svg] —— SVG 可内嵌脚本，
#: 以 inline 方式在同源下发会有 XSS 风险，故明确排除）
MEDIA_EXTS: Tuple[str, ...] = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".mp3", ".wav", ".ogg", ".m4a", ".aac",
    ".mp4", ".webm", ".flv", ".mov",
)

#: 扩展名 → MIME（下发时显式指定，并配合 nosniff）
MEDIA_MIME: Dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".flv": "video/x-flv",
    ".mov": "video/quicktime",
}

#: Windows 保留设备名（作为文件/目录名会出错）
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

#: 组件名中禁止的字符（含路径分隔符与控制字符）
_FORBIDDEN_CHARS = set('/\\:*?"<>|')

#: 错误提示里展示用（固定顺序，读起来是正常写法而不是排序后的乱序）
_FORBIDDEN_DISPLAY = r'/ \ : * ? " < > |'

MAX_SUBJECT_LEN = 80
MAX_FILENAME_LEN = 180
MAX_MEDIA_NAME_LEN = 180

#: 溯源侧车后缀
PROVENANCE_SUFFIX = ".kqb.json"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class StoreError(Exception):
    """存储层错误基类。"""


class StorePathError(StoreError):
    """路径/名称非法（含穿越尝试）。"""


class StoreConflictError(StoreError):
    """已存在冲突（学科非空、题库同名等）。"""


class StoreNotFoundError(StoreError):
    """找不到学科 / 题库 / 媒体。"""


class StoreBankError(StoreError):
    """题库内容非法（JSON 坏、结构不识别、加密但无密钥）。"""


# ---------------------------------------------------------------------------
# 名称 / 路径安全
# ---------------------------------------------------------------------------
def validate_subject_name(name: str) -> str:
    """校验并规范化学科名（允许中文与空格，拒绝穿越与保留名）。"""
    return _validate_component(name, "学科", MAX_SUBJECT_LEN)


def validate_bank_filename(filename: str) -> str:
    """校验题库文件名（必须 .json）。"""
    name = _validate_component(filename, "题库文件名", MAX_FILENAME_LEN)
    if not name.lower().endswith(".json"):
        raise StorePathError(f"题库文件名必须以 .json 结尾：{filename!r}")
    stem = name[:-5]
    if not stem.strip():
        raise StorePathError("题库文件名不能只有扩展名")
    return name


def validate_media_name(name: str) -> str:
    """校验媒体文件名（单段 + 扩展名白名单）。"""
    cleaned = _validate_component(name, "媒体文件名", MAX_MEDIA_NAME_LEN)
    ext = os.path.splitext(cleaned)[1].lower()
    if ext not in MEDIA_EXTS:
        raise StorePathError(
            f"不支持的媒体类型 {ext or '(无扩展名)'}；允许：{', '.join(MEDIA_EXTS)}"
        )
    return cleaned


def _validate_component(raw: Any, label: str, max_len: int) -> str:
    """通用单段名称校验。"""
    if not isinstance(raw, str):
        raise StorePathError(f"{label}必须是字符串")
    name = raw.strip()
    if not name:
        raise StorePathError(f"{label}不能为空")
    if len(name) > max_len:
        raise StorePathError(f"{label}过长（{len(name)} > {max_len}）")
    if name in (".", ".."):
        raise StorePathError(f"{label}不能是 {name!r}")
    if any(ch in _FORBIDDEN_CHARS for ch in name):
        raise StorePathError(
            f"{label}不得含以下字符：{_FORBIDDEN_DISPLAY}（{name!r}）"
        )
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise StorePathError(f"{label}不得含控制字符（{name!r}）")
    # Windows 不允许结尾的点或空格
    if name.endswith(".") or name.endswith(" "):
        raise StorePathError(f"{label}不能以点或空格结尾（{name!r}）")
    if name.split(".")[0].lower() in _RESERVED_NAMES:
        raise StorePathError(f"{label}使用了系统保留名（{name!r}）")
    return name


def _assert_within(base: str, path: str) -> str:
    """确认 path 位于 base 之内（realpath 前缀比对）。"""
    base_real = os.path.realpath(base)
    path_real = os.path.realpath(path)
    if path_real != base_real and not path_real.startswith(base_real + os.sep):
        raise StorePathError(f"路径越界：{path!r} 不在 {base!r} 之内")
    return path_real


def _atomic_write_bytes(path: str, payload: bytes) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp-{_uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "wb") as fp:
            fp.write(payload)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _safe_rmtree(path: str) -> None:
    """删除目录树（仅用于插件自属数据目录下的路径）。

    安全性由来：所有调用点传入的路径都先经 [_assert_within] 的 realpath 前缀校验，
    或由 subject_dir()/media_dir_for() 从已校验的「学科名 + 题库文件名」拼出，
    不含任何用户直传的路径片段。此处按框架规范标注 scan:ignore 豁免
    （rmtree 属"可约束 high"，命中声明或行内标注才放行）。
    """
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)  # scan:ignore


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
class QbankStore:
    """题库存储。所有公开方法均做名称校验与越界防护。"""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        #: {(path, mtime_ns, size): summary}
        self._summary_cache: Dict[Tuple[str, int, int], Dict[str, Any]] = {}

    # ---------------------------------------------------------------- 学科
    def list_subjects(self) -> List[Dict[str, Any]]:
        """列出学科及其题库概览。"""
        out: List[Dict[str, Any]] = []
        try:
            entries = sorted(os.listdir(self.root))
        except OSError:
            return out
        for name in entries:
            full = os.path.join(self.root, name)
            if not os.path.isdir(full):
                continue
            banks = self.list_banks(name)
            media_bytes = sum(b.get("media_bytes", 0) for b in banks)
            out.append({
                "name": name,
                "bank_count": len(banks),
                "item_count": sum(b.get("item_count") or 0 for b in banks),
                "media_count": sum(b.get("media_count") or 0 for b in banks),
                "media_bytes": media_bytes,
                "banks": banks,
            })
        return out

    def create_subject(self, name: str) -> Dict[str, Any]:
        safe = validate_subject_name(name)
        full = os.path.join(self.root, safe)
        if os.path.isdir(full):
            raise StoreConflictError(f"学科已存在：{safe}")
        os.makedirs(full, exist_ok=False)
        return {"name": safe, "bank_count": 0}

    def delete_subject(self, name: str, *, force: bool = False) -> Dict[str, Any]:
        safe = validate_subject_name(name)
        full = _assert_within(self.root, os.path.join(self.root, safe))
        if not os.path.isdir(full):
            raise StoreNotFoundError(f"学科不存在：{safe}")
        banks = self.list_banks(safe)
        if banks and not force:
            raise StoreConflictError(
                f"学科「{safe}」下还有 {len(banks)} 个题库，需显式确认才能删除"
            )
        _safe_rmtree(full)
        self.invalidate()
        return {"name": safe, "deleted_banks": len(banks)}

    # ---------------------------------------------------------------- 题库
    def subject_dir(self, subject: str) -> str:
        safe = validate_subject_name(subject)
        return _assert_within(self.root, os.path.join(self.root, safe))

    def bank_path(self, subject: str, filename: str) -> str:
        safe_subject = validate_subject_name(subject)
        safe_file = validate_bank_filename(filename)
        path = os.path.join(self.root, safe_subject, safe_file)
        return _assert_within(os.path.join(self.root, safe_subject), path)

    def media_dir_for(self, subject: str, filename: str) -> str:
        """题库的媒体目录：与题库文件同主名的目录（Kaleido 既有约定）。"""
        path = self.bank_path(subject, filename)
        return os.path.join(os.path.dirname(path), os.path.splitext(os.path.basename(path))[0])

    def list_banks(self, subject: str) -> List[Dict[str, Any]]:
        """列出学科下的题库（含解析摘要，按 mtime 缓存）。"""
        subj_dir = self.subject_dir(subject)
        if not os.path.isdir(subj_dir):
            raise StoreNotFoundError(f"学科不存在：{subject}")
        out: List[Dict[str, Any]] = []
        for entry in sorted(os.listdir(subj_dir)):
            if not entry.lower().endswith(".json"):
                continue
            if entry.endswith(PROVENANCE_SUFFIX):
                continue
            full = os.path.join(subj_dir, entry)
            if not os.path.isfile(full):
                continue
            out.append(self._describe_bank(subj_dir, entry, full))
        return out

    def _describe_bank(self, subj_dir: str, filename: str, full: str) -> Dict[str, Any]:
        stem = os.path.splitext(filename)[0]
        media_dir = os.path.join(subj_dir, stem)
        media_files = self._list_media_files(media_dir)
        media_bytes = 0
        for name in media_files:
            try:
                media_bytes += os.path.getsize(os.path.join(media_dir, name))
            except OSError:
                pass

        try:
            stat = os.stat(full)
            cache_key = (full, stat.st_mtime_ns, stat.st_size)
        except OSError:
            stat = None
            cache_key = None

        summary: Dict[str, Any]
        if cache_key is not None and cache_key in self._summary_cache:
            summary = dict(self._summary_cache[cache_key])
        else:
            summary = self._parse_summary(full)
            if cache_key is not None:
                self._summary_cache[cache_key] = dict(summary)

        result: Dict[str, Any] = {
            "filename": filename,
            "stem": stem,
            "bytes": os.path.getsize(full) if stat else None,
            "mtime": int(stat.st_mtime) if stat else None,
            "media_dir": stem,
            "media_count": len(media_files),
            "media_bytes": media_bytes,
        }
        result.update(summary)

        # 媒体引用覆盖情况（需要媒体文件清单）
        refs = summary.get("media_refs") or []
        present = set(media_files)
        resolved = [r for r in refs if self._resolve_media_name(present, r)]
        missing = [r for r in refs if r not in resolved]
        result["media_missing"] = missing
        result["media_missing_count"] = len(missing)
        return result

    def _parse_summary(self, full: str) -> Dict[str, Any]:
        """解析题库取摘要（不抛异常，失败信息写进结果）。"""
        try:
            bank_obj, encrypted = self._load_bank_object_file(full)
        except StoreBankError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "title": os.path.splitext(os.path.basename(full))[0],
                "encrypted": False,
                "item_count": 0,
                "chapter_count": 0,
                "type_counts": {},
                "media_refs": [],
            }
        return _bank_summary_of(bank_obj, encrypted)

    def _load_bank_object_file(self, full: str) -> Tuple[Dict[str, Any], bool]:
        """读取并（必要时）解密题库文件。返回 (题库对象, 是否原本加密)。

        若文件是加密信封且 [没有可用密钥]，抛 StoreBankError 说明原因。
        """
        try:
            with open(full, "r", encoding="utf-8-sig") as fp:
                data = json.load(fp)
        except json.JSONDecodeError as exc:
            raise StoreBankError(f"题库不是合法 JSON：{exc}") from exc
        except OSError as exc:
            raise StoreBankError(f"题库读取失败：{exc}") from exc
        if not isinstance(data, dict):
            raise StoreBankError("题库根节点必须是 JSON 对象")

        if "encrypted_content" not in data:
            return data, False

        key_obj = self.load_key_obj()
        if key_obj is None:
            raise StoreBankError(
                "题库是加密的，但未配置密钥文件。请先导入 key_obj.json"
            )
        try:
            crypto = KC.QuestionBankCrypto(key_obj)
            plain = crypto.decrypt_question_bank(data)
        except KC.KaleidoCryptoError as exc:
            raise StoreBankError(f"加密题库解密失败：{exc}") from exc
        return plain, True

    # ------------------------------------------------------------ 密钥文件
    def key_path(self) -> str:
        return os.path.join(os.path.dirname(self.root), "keys", "key_obj.json")

    def load_key_obj(self) -> Optional[Dict[str, Any]]:
        """读取已配置的全题库密钥文件；不存在或非法返回 None（不抛异常）。"""
        path = self.key_path()
        if not os.path.isfile(path):
            return None
        try:
            return KC.load_key_obj(path)
        except KC.KaleidoCryptoError:
            return None

    def save_key_obj(self, key_obj: Dict[str, Any]) -> str:
        return KC.save_key_obj(key_obj, self.key_path())

    def has_key(self) -> bool:
        return self.load_key_obj() is not None

    # ------------------------------------------------------------ 读取题库
    def load_bank_object(self, subject: str, filename: str) -> Tuple[Dict[str, Any], bool]:
        path = self.bank_path(subject, filename)
        if not os.path.isfile(path):
            raise StoreNotFoundError(f"题库不存在：{filename}")
        return self._load_bank_object_file(path)

    def get_bank_detail(self, subject: str, filename: str) -> Dict[str, Any]:
        """题库详情：摘要 + 媒体自检 + 章节结构。"""
        path = self.bank_path(subject, filename)
        if not os.path.isfile(path):
            raise StoreNotFoundError(f"题库不存在：{filename}")
        bank_obj, encrypted = self._load_bank_object_file(path)
        summary = _bank_summary_of(bank_obj, encrypted)

        media_dir = self.media_dir_for(subject, filename)
        present = self._list_media_files(media_dir)
        present_set = set(present)
        refs = summary.get("media_refs") or []
        resolved_map = {r: self._resolve_media_name(present_set, r) for r in refs}
        missing = [r for r in refs if not resolved_map[r]]
        fallback = {r: resolved_map[r] for r in refs if resolved_map[r] and resolved_map[r] != r}
        unreferenced = [n for n in present if n not in set(refs)]

        # 章节结构（供详情页展示）
        chapters = []
        try:
            parsed = KP.parse_bank(bank_obj)
            chapters = [
                {
                    "chapter_id": ch.chapter_id,
                    "chapter_name": ch.chapter_name,
                    "item_count": len(ch.items),
                    "type_counts": _count_by(ch.items, "qtype"),
                }
                for ch in parsed.chapters
            ]
        except KP.BankFormatError as exc:
            summary["parse_error"] = str(exc)

        return {
            "subject": validate_subject_name(subject),
            "filename": filename,
            "stem": os.path.splitext(filename)[0],
            "bytes": os.path.getsize(path),
            "mtime": int(os.path.getmtime(path)),
            "summary": summary,
            "chapters": chapters,
            "media": {
                "dir": os.path.basename(media_dir),
                "present": present,
                "present_count": len(present),
                "missing": missing,
                "missing_count": len(missing),
                "fallback": fallback,
                "unreferenced": unreferenced,
                "unreferenced_count": len(unreferenced),
            },
            "provenance": self.read_provenance(subject, filename),
        }

    # ------------------------------------------------------------ 写入题库
    def write_bank_json(self, subject: str, filename: str, raw: bytes,
                        *, overwrite: bool = False) -> Dict[str, Any]:
        """保存单个题库 JSON（明文或加密信封均可）。

        加密信封会被解密后 [以明文落盘]（Mini 的本地工作副本策略）。
        """
        safe_subject = validate_subject_name(subject)
        safe_file = validate_bank_filename(filename)
        subj_dir = self.subject_dir(safe_subject)
        if not os.path.isdir(subj_dir):
            raise StoreNotFoundError(f"学科不存在：{safe_subject}（请先创建学科）")
        path = os.path.join(subj_dir, safe_file)
        _assert_within(subj_dir, path)

        if os.path.exists(path) and not overwrite:
            raise StoreConflictError(f"题库已存在：{safe_file}（如需替换请显式确认）")

        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise StoreBankError(f"上传内容不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise StoreBankError("题库根节点必须是 JSON 对象")

        encrypted_in = "encrypted_content" in data
        if encrypted_in:
            key_obj = self.load_key_obj()
            if key_obj is None:
                raise StoreBankError("上传的是加密题库，但未配置密钥文件")
            try:
                data = KC.QuestionBankCrypto(key_obj).decrypt_question_bank(data)
            except KC.KaleidoCryptoError as exc:
                raise StoreBankError(f"加密题库解密失败：{exc}") from exc

        # 结构校验：必须能被解析器识别（否则是坏库，不应入库）
        try:
            parsed = KP.parse_bank(data)
        except KP.BankFormatError as exc:
            raise StoreBankError(f"题库结构无法识别：{exc}") from exc

        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        _atomic_write_bytes(path, payload)
        self.invalidate()

        return {
            "subject": safe_subject,
            "filename": safe_file,
            "decrypted_on_upload": encrypted_in,
            "item_count": parsed.item_count,
            "chapter_count": parsed.chapter_count,
            "type_counts": parsed.type_counts,
            "media_ref_count": parsed.media_ref_count,
        }

    def import_package(self, subject: str, payload: bytes,
                       *, overwrite: bool = False,
                       filename: str = "") -> Dict[str, Any]:
        """导入 .kqb 整包：核对 → 落盘（明文题库 + 明文媒体）。

        核对 fatal 不过则抛异常且 [不写任何文件]。
        """
        safe_subject = validate_subject_name(subject)
        subj_dir = self.subject_dir(safe_subject)
        if not os.path.isdir(subj_dir):
            raise StoreNotFoundError(f"学科不存在：{safe_subject}（请先创建学科）")
        key_obj = self.load_key_obj()

        result = KQB.inspect_package(payload, key_obj=key_obj)
        if not result.ok:
            detail = "；".join(f"[{c.title}] {c.detail}" for c in result.failures)
            raise StoreBankError(f"整包核对未通过：{detail}")

        bank_obj = result.bank_obj
        if not isinstance(bank_obj, dict):
            raise StoreBankError("整包内没有可用题库")

        summary = _bank_summary_of(bank_obj, False)
        title = (bank_obj.get("title") or "").strip()
        author = (bank_obj.get("author") or "").strip()

        target_name = filename.strip() if filename else _compose_filename(title, author)
        safe_file = validate_bank_filename(target_name)
        path = os.path.join(subj_dir, safe_file)
        _assert_within(subj_dir, path)
        if os.path.exists(path) and not overwrite:
            raise StoreConflictError(f"题库已存在：{safe_file}（如需替换请显式确认）")

        # 落盘前再确认题库可解析
        try:
            KP.parse_bank(bank_obj)
        except KP.BankFormatError as exc:
            raise StoreBankError(f"题库结构无法识别：{exc}") from exc

        media_dir = os.path.join(subj_dir, os.path.splitext(safe_file)[0])
        written_media: List[str] = []
        try:
            for name, blob in sorted(result.media.items()):
                safe_media = validate_media_name(name)
                _atomic_write_bytes(os.path.join(media_dir, safe_media), blob)
                written_media.append(safe_media)
            _atomic_write_bytes(
                path,
                json.dumps(bank_obj, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        except Exception:
            # 失败则回滚已写入的媒体，避免留下半套
            for name in written_media:
                try:
                    os.remove(os.path.join(media_dir, name))
                except OSError:
                    pass
            _safe_rmtree(media_dir)
            raise

        # 溯源侧车
        manifest = result.manifest or {}
        provenance = {
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": "kqb",
            "kqb_version": manifest.get("kqb_version"),
            "kind": manifest.get("kind"),
            "bank_encrypted_in_package": (manifest.get("bank") or {}).get("encrypted"),
            "media_mode": (manifest.get("media") or {}).get("mode"),
            "key_placement": (manifest.get("key") or {}).get("placement"),
            "public_key_sha256": (manifest.get("key") or {}).get("public_key_sha256"),
            "generator": manifest.get("generator"),
            "created_at": manifest.get("created_at"),
            "checks": [c.to_dict() for c in result.checks],
            "warnings": [c.to_dict() for c in result.warnings],
        }
        self._write_provenance(subj_dir, safe_file, provenance)
        self.invalidate()

        return {
            "subject": safe_subject,
            "filename": safe_file,
            "item_count": summary.get("item_count"),
            "chapter_count": summary.get("chapter_count"),
            "media_written": len(written_media),
            "warnings": [c.detail for c in result.warnings],
            "provenance": provenance,
        }

    def delete_bank(self, subject: str, filename: str) -> Dict[str, Any]:
        """删除题库，[连带] 删除同主名媒体目录与溯源侧车。"""
        path = self.bank_path(subject, filename)
        if not os.path.isfile(path):
            raise StoreNotFoundError(f"题库不存在：{filename}")
        media_dir = self.media_dir_for(subject, filename)
        removed_media = len(self._list_media_files(media_dir))
        prov = self._provenance_path(path)

        os.remove(path)
        _safe_rmtree(media_dir)
        try:
            if os.path.isfile(prov):
                os.remove(prov)
        except OSError:
            pass
        self.invalidate()
        return {
            "subject": validate_subject_name(subject),
            "filename": filename,
            "removed_media": removed_media,
            "removed_media_dir": os.path.basename(media_dir),
        }

    # ------------------------------------------------------------ 溯源侧车
    def _provenance_path(self, bank_path: str) -> str:
        return bank_path[: -len(".json")] + PROVENANCE_SUFFIX if bank_path.lower().endswith(".json") \
            else bank_path + PROVENANCE_SUFFIX

    def _write_provenance(self, subj_dir: str, filename: str, data: Dict[str, Any]) -> None:
        path = self._provenance_path(os.path.join(subj_dir, filename))
        _assert_within(subj_dir, path)
        _atomic_write_bytes(
            path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        )

    def read_provenance(self, subject: str, filename: str) -> Optional[Dict[str, Any]]:
        path = self._provenance_path(self.bank_path(subject, filename))
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8-sig") as fp:
                return json.load(fp)
        except (json.JSONDecodeError, OSError):
            return None

    # ---------------------------------------------------------------- 媒体
    def _list_media_files(self, media_dir: str) -> List[str]:
        if not os.path.isdir(media_dir):
            return []
        out: List[str] = []
        for entry in sorted(os.listdir(media_dir)):
            if not os.path.isfile(os.path.join(media_dir, entry)):
                continue
            if os.path.splitext(entry)[1].lower() in MEDIA_EXTS:
                out.append(entry)
        return out

    def list_media(self, subject: str, filename: str) -> List[str]:
        return self._list_media_files(self.media_dir_for(subject, filename))

    @staticmethod
    def _resolve_media_name(present: set, ref: str) -> Optional[str]:
        """把题库中的 /media/<ref> 解析到实际文件名。

        顺序：精确同名 → 同主名换扩展名 → None。
        """
        if not ref:
            return None
        if ref in present:
            return ref
        stem = os.path.splitext(ref)[0]
        for name in sorted(present):
            if os.path.splitext(name)[0] == stem:
                return name
        return None

    def resolve_media(self, subject: str, filename: str,
                      media_ref: str) -> Tuple[str, str]:
        """解析媒体引用 → (绝对路径, 实际文件名)。

        :raises StorePathError: 名称非法
        :raises StoreNotFoundError: 既无同名也无同主名文件
        """
        ref = str(media_ref or "")
        if not ref:
            raise StorePathError("媒体名为空")
        # 允许带扩展名的合法单段名；不允许任何路径成分
        ext = os.path.splitext(ref)[1].lower()
        if ext and ext in MEDIA_EXTS:
            ref = validate_media_name(ref)
        else:
            ref = _validate_component(ref, "媒体名", MAX_MEDIA_NAME_LEN)

        media_dir = self.media_dir_for(subject, filename)
        present = set(self._list_media_files(media_dir))
        actual = self._resolve_media_name(present, ref)
        if not actual:
            raise StoreNotFoundError(f"媒体不存在：{ref}")
        path = _assert_within(media_dir, os.path.join(media_dir, actual))
        return path, actual

    @staticmethod
    def media_mime(media_name: str) -> str:
        return MEDIA_MIME.get(os.path.splitext(media_name)[1].lower(),
                              "application/octet-stream")

    # ---------------------------------------------------------------- 缓存
    def invalidate(self) -> None:
        self._summary_cache.clear()


# ---------------------------------------------------------------------------
# 摘要辅助
# ---------------------------------------------------------------------------
def _bank_summary_of(bank_obj: Dict[str, Any], encrypted: bool) -> Dict[str, Any]:
    """把一个题库对象压成摘要（解析失败时返回可用的降级信息）。"""
    base: Dict[str, Any] = {
        "ok": False,
        "encrypted": encrypted,
        "title": str(bank_obj.get("title") or "").strip(),
        "author": str(bank_obj.get("author") or "").strip(),
        "description": str(bank_obj.get("description") or "").strip(),
        "uuid": str(bank_obj.get("uuid") or "").strip(),
        "has_valid_key": bool(bank_obj.get("valid_key")),
        "format_version": None,
        "chapter_count": 0,
        "item_count": 0,
        "type_counts": {},
        "display_counts": {},
        "media_refs": [],
        "media_ref_count": 0,
    }
    try:
        parsed = KP.parse_bank(bank_obj)
    except KP.BankFormatError as exc:
        base["error"] = str(exc)
        return base

    base.update({
        "ok": True,
        "format_version": list(parsed.format_version),
        "format_version_str": parsed.format_version_str(),
        "chapter_count": parsed.chapter_count,
        "item_count": parsed.item_count,
        "type_counts": parsed.type_counts,
        "display_counts": parsed.display_counts,
        "media_refs": parsed.media_refs,
        "media_ref_count": parsed.media_ref_count,
        "warnings": parsed.warnings[:20],
    })
    return base


def _count_by(items: List[KP.PracticeItem], attr: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in items:
        key = getattr(item, attr, "")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _compose_filename(title: str, author: str) -> str:
    """按 Kaleido 既有命名习惯生成 `<标题>_<作者>.json`；同名由调用方去重。"""
    clean_title = _sanitize_for_filename(title) or "未命名题库"
    clean_author = _sanitize_for_filename(author)
    stem = f"{clean_title}_{clean_author}" if clean_author else clean_title
    stem = stem[: MAX_FILENAME_LEN - 5]
    return stem + ".json"


def _sanitize_for_filename(text: str) -> str:
    if not text:
        return ""
    out = []
    for ch in str(text):
        if ch in _FORBIDDEN_CHARS or ord(ch) < 32 or ord(ch) == 127:
            out.append("_")
        else:
            out.append(ch)
    cleaned = "".join(out).strip().strip(".")
    return cleaned


def unique_filename(existing: set, desired: str) -> str:
    """在已有文件名集合中取一个不冲突的名字（追加 _2、_3 …）。"""
    if desired not in existing:
        return desired
    stem, ext = os.path.splitext(desired)
    index = 2
    while f"{stem}_{index}{ext}" in existing:
        index += 1
    return f"{stem}_{index}{ext}"


__all__ = [
    "MEDIA_EXTS", "MEDIA_MIME", "PROVENANCE_SUFFIX",
    "StoreError", "StorePathError", "StoreConflictError",
    "StoreNotFoundError", "StoreBankError",
    "QbankStore",
    "validate_subject_name", "validate_bank_filename", "validate_media_name",
    "unique_filename",
]
