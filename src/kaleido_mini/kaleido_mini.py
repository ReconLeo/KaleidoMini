# -*- coding: utf-8 -*-
"""Kaleido Mini —— 只读题库 + 练习模式自测（FlaskToolkit 子项目插件）

================================================================================
定位：完整版 Kaleido 的「多模块多模板大插件」精简版。只做一件事 ——
把已有的 Kaleido 题库（v1/v2/v3/v3.1.0）读进来，提供逐题练习与错题导出。

边界：
  * 只读：不做题库编辑、组卷、docx 导入、成绩库。
  * 上传/导入/删除/设置 = 管理员；浏览/练习/错题导出 = 登录用户。
  * 明文题库验签失败 [不阻断]（实测大量既有题库的 valid_key 为占位值或他钥签名）。

权限矩阵（与 README / 方案文档一致）：
  页面  /plugin/kaleido_mini                        user
        /plugin/kaleido_mini/bank/<subject>/<file>  user
        /plugin/kaleido_mini/settings               admin   （M5 提供）
  API   GET    /subjects                             user    学科与题库列表
        POST   /subjects                             admin   新建学科
        DELETE /subjects/<subject>                   admin   删除学科
        GET    /qbanks/<subject>/<filename>          user    题库详情 + 媒体自检
        POST   /qbanks                                admin   上传单个 JSON 题库
        DELETE /qbanks/<subject>/<filename>          admin   删除题库（连带媒体）
        GET    /qbanks/<subject>/<filename>/media/<name>  user  媒体下发
        POST   /packages/inspect                     admin   .kqb 预检（不落盘）
        POST   /packages                             admin   .kqb 整包导入
        GET    /meta                                 user    角色 / 密钥状态 / 限额

数据落盘（自属数据目录，capabilities 隐式豁免）：
  plugins/data/kaleido_mini/
    ├── qbanks/<学科>/<题库>.json  +  <题库>/（媒体目录）
    └── keys/key_obj.json          全题库密钥文件（母密钥，见 km_crypto 文档）
"""

from __future__ import annotations

import io
import json
import os
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from flask import request, send_file

# 注意：base_plugin.py 位于 [上级包] plugins/，不在本插件目录内，
# 因此 [不能] 用相对导入 `.base_plugin`（会解析成 plugins.kaleido_mini.base_plugin
# 并抛 ModuleNotFoundError，导致插件整体加载失败）。
# 与官方示例 multitool_demo 保持一致使用绝对导入；同时保留相对导入兜底，
# 以兼容"插件目录内自带 base_plugin.py"的另类布局。
try:
    from plugins.base_plugin import BasePlugin, permission as permission_required
except ImportError:  # pragma: no cover - 兼容自带 base_plugin 的布局
    from .base_plugin import BasePlugin, permission as permission_required

from . import km_crypto as KC
from . import km_export as KEX
from . import km_grader as KG
from . import km_package as KQB
from . import km_parser as KP
from . import km_practice as KPrac
from . import km_render as KR
from . import km_store as KS

__all__ = ["KaleidoMini"]

#: 单文件上传上限（MB）—— 大题库约 400KB，.kqb 含媒体可达数十 MB
MAX_UPLOAD_MB = 512

#: 允许的上传类型
ALLOWED_UPLOAD_TYPES = (".json", ".zip", ".kqb")

#: 允许的媒体策略默认值（设置页可改，M5）
DEFAULT_ALLOW_EXTERNAL_MEDIA = True


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _today() -> str:
    from datetime import date

    return date.today().strftime("%Y%m%d")


def _truthy(value: Any, *, default: bool = False) -> bool:
    """宽松布尔解析（'1/true/yes/on' 为真；'0/false/no/off/空' 为假）。"""
    if value is None:
        return default
    text = str(value).strip().lower()
    if text == "":
        return default
    return text not in ("0", "false", "no", "off", "n", "f")


def _http_code_for(exc: Exception) -> int:
    if isinstance(exc, KS.StoreNotFoundError):
        return 404
    if isinstance(exc, KS.StoreConflictError):
        return 409
    if isinstance(exc, (KS.StorePathError, KS.StoreBankError)):
        return 400
    if isinstance(exc, (KQB.KqbSecurityError, KQB.KqbIntegrityError,
                        KQB.KqbKeyError, KQB.KqbManifestError,
                        KQB.KqbUnsupportedError, KQB.KqbError)):
        return 400
    if isinstance(exc, KC.KaleidoCryptoError):
        return 400
    if isinstance(exc, KP.BankFormatError):
        return 400
    return 500


class KaleidoMini(BasePlugin):
    name = "kaleido_mini"
    title = "Kaleido Mini 题库自测"
    description = (
        "只读题库 + 练习模式自测：学科文件夹管理、单题库上传/删除、"
        ".kqb 整包导入（含加密媒体）、逐题练习、答案显隐、错题汇总导出、"
        "DeepSeek 智能评卷。"
    )
    version = "0.3.0"
    author = "Kaleido"
    category = "学习工具"
    permission = "user"
    require_framework_version = "4.21.0"

    # ------------------------------------------------------------------ 基类
    @property
    def allowed_upload_types(self) -> List[str]:
        return list(ALLOWED_UPLOAD_TYPES)

    @property
    def max_upload_size(self):
        return MAX_UPLOAD_MB

    @property
    def routes(self) -> List[Dict[str, Any]]:
        return [
            # ---------------- 元信息 ----------------
            {"path": "/meta", "name": "插件元信息（角色/密钥/限额）",
             "methods": ["GET"], "view_func": self.api_meta},

            # ---------------- 学科 ----------------
            {"path": "/subjects", "name": "学科与题库列表",
             "methods": ["GET"], "view_func": self.api_list_subjects},
            {"path": "/subjects", "name": "新建学科（管理员）", "permission": "admin",
             "methods": ["POST"], "view_func": self.api_create_subject},
            {"path": "/subjects/<subject>", "name": "删除学科（管理员）", "permission": "admin",
             "methods": ["DELETE"], "view_func": self.api_delete_subject},

            # ---------------- 题库 ----------------
            {"path": "/qbanks", "name": "上传单个 JSON 题库（管理员）", "permission": "admin",
             "methods": ["POST"], "view_func": self.api_upload_bank},
            {"path": "/qbanks/<subject>/<filename>", "name": "题库详情与媒体自检",
             "methods": ["GET"], "view_func": self.api_bank_detail},
            {"path": "/qbanks/<subject>/<filename>", "name": "删除题库（管理员）",
             "permission": "admin",
             "methods": ["DELETE"], "view_func": self.api_delete_bank},
            {"path": "/qbanks/<subject>/<filename>/media/<media_name>",
             "name": "媒体文件下发",
             "methods": ["GET"], "view_func": self.api_media},

            # ---------------- 练习（M3）----------------
            {"path": "/qbanks/<subject>/<filename>/practice",
             "name": "练习载荷（不含答案）",
             "methods": ["GET"], "view_func": self.api_practice},
            {"path": "/qbanks/<subject>/<filename>/grade",
             "name": "单题判分 / 揭晓答案",
             "methods": ["POST"], "view_func": self.api_grade},
            {"path": "/qbanks/<subject>/<filename>/grade-ai",
             "name": "AI 评卷（简答/填空）",
             "methods": ["POST"], "view_func": self.api_grade_ai},
            {"path": "/qbanks/<subject>/<filename>/summary",
             "name": "练习战绩汇总",
             "methods": ["POST"], "view_func": self.api_summary},
            {"path": "/qbanks/<subject>/<filename>/export",
             "name": "错题/题库导出（v3.1.0 / .kqb / 错题本）",
             "methods": ["POST"], "view_func": self.api_export},

            # ---------------- 整包 ----------------
            {"path": "/packages/inspect", "name": ".kqb 预检（管理员）", "permission": "admin",
             "methods": ["POST"], "view_func": self.api_package_inspect},
            {"path": "/packages", "name": ".kqb 整包导入（管理员）", "permission": "admin",
             "methods": ["POST"], "view_func": self.api_package_import},

            # ---------------- 设置（M5，管理员）----------------
            {"path": "/settings", "name": "读取设置（管理员）", "permission": "admin",
             "methods": ["GET"], "view_func": self.api_get_settings},
            {"path": "/settings", "name": "保存设置（管理员）", "permission": "admin",
             "methods": ["POST"], "view_func": self.api_save_settings},
            {"path": "/settings/test-ai", "name": "AI 连通性自检（管理员）",
             "permission": "admin",
             "methods": ["POST"], "view_func": self.api_test_ai},
            {"path": "/crypto/key", "name": "密钥状态（管理员）", "permission": "admin",
             "methods": ["GET"], "view_func": self.api_key_status},
            {"path": "/crypto/key", "name": "上传/生成密钥（管理员）", "permission": "admin",
             "methods": ["POST"], "view_func": self.api_key_upsert},
            {"path": "/crypto/key/download", "name": "下载密钥文件（管理员）",
             "permission": "admin",
             "methods": ["GET"], "view_func": self.api_key_download},

            # ---------------- 页面 ----------------
            {"path": "/bank/<subject>/<filename>", "name": "题库详情页",
             "methods": ["GET"], "page": True, "template": "bank.html",
             "view_func": self.page_bank},
            {"path": "/exam/<subject>/<filename>", "name": "练习页（自测）",
             "methods": ["GET"], "page": True, "template": "exam.html",
             "view_func": self.page_exam},
            {"path": "/settings", "name": "设置页（管理员）",
             "methods": ["GET"], "page": True, "template": "settings.html",
             "view_func": self.page_settings},
        ]

    # ------------------------------------------------------------ 生命周期
    def on_load(self) -> None:
        try:
            root = self._store_root()
            os.makedirs(root, exist_ok=True)
            subjects = len(self._store().list_subjects())
            self.logger.info(
                f"Kaleido Mini 就绪：数据目录 {root}，{subjects} 个学科，"
                f"密钥{'已' if self._store().has_key() else '未'}配置"
            )
        except Exception as exc:  # 不因初始化失败阻断插件加载
            self.logger.warning(f"Kaleido Mini 初始化检查失败：{exc}")

    # ---------------------------------------------------------------- 内部
    def _store_root(self) -> str:
        return os.path.join(self.data_dir, "qbanks")

    def _store(self) -> KS.QbankStore:
        """取存储实例（带缓存，避免每次列表都重解析题库）。"""
        inst = getattr(self, "_km_store", None)
        if inst is None:
            inst = KS.QbankStore(self._store_root())
            self._km_store = inst
        return inst

    def _role(self) -> str:
        """当前用户角色。

        未安装 auth 插件时系统无登录体系，此时 [按管理员] 处理 ——
        否则管理功能永远不可用（corp_tools 的同类实现返回 public，会把入口永久藏起）。
        """
        try:
            import global_var  # noqa: PLC0415

            if "auth" not in global_var.plugins:
                return "admin"
        except Exception:
            pass
        user = getattr(request, "user", None)
        if isinstance(user, dict):
            role = user.get("role", "user")
            return role if role in ("public", "user", "admin") else "user"
        return "public"

    def _is_admin(self) -> bool:
        return self._role() == "admin"

    def _allow_external_media(self) -> bool:
        # 注意：BasePlugin.load_config() 只回填 self.config 并 [返回 None]，
        # 早期这里写 `load_config() or {}` 会永远拿到空 dict → 配置永不生效。
        conf = self.config if isinstance(self.config, dict) else {}
        return bool(conf.get("allow_external_media", DEFAULT_ALLOW_EXTERNAL_MEDIA))

    # ------------------------------------------------------------ AI 评卷配置
    def _deepseek_conf(self) -> Dict[str, Any]:
        conf = self.config if isinstance(self.config, dict) else {}
        ds = conf.get("deepseek")
        return dict(ds) if isinstance(ds, dict) else {}

    def _deepseek_status(self) -> Dict[str, Any]:
        """可安全外发的 AI 评卷配置（key 只暴露掩码）。"""
        ds = self._deepseek_conf()
        key = str(ds.get("api_key") or "")
        return {
            "enabled": bool(ds.get("enabled", False)),
            "configured": bool(key),
            "api_key_masked": KG.mask_key(key),
            "base_url": str(ds.get("base_url") or KG.DEFAULT_BASE_URL),
            "model": str(ds.get("model") or KG.DEFAULT_MODEL),
            "timeout": int(ds.get("timeout") or KG.DEFAULT_TIMEOUT),
            "max_timeout": 120,
        }

    def _make_grader(self) -> Optional[KG.DeepSeekGrader]:
        """按当前配置造一个评卷器；未启用/未配 key 返回 None。"""
        ds = self._deepseek_conf()
        if not ds.get("enabled"):
            return None
        key = str(ds.get("api_key") or "").strip()
        if not key:
            return None
        return KG.DeepSeekGrader(
            key,
            base_url=str(ds.get("base_url") or KG.DEFAULT_BASE_URL),
            model=str(ds.get("model") or KG.DEFAULT_MODEL),
            timeout=int(ds.get("timeout") or KG.DEFAULT_TIMEOUT),
        )

    def _fail(self, exc: Exception, prefix: str = ""):
        code = _http_code_for(exc)
        if code >= 500:
            self.logger.error(f"{prefix}内部错误：{exc}")
        message = f"{prefix}{exc}" if prefix else str(exc)
        return self.error_response(message, code)

    # ------------------------------------------------------------ 媒体 URL
    def _media_endpoint(self, subject: str, filename: str, ref: str) -> str:
        return "/api/{name}/qbanks/{subject}/{filename}/media/{ref}".format(
            name=self.name,
            subject=quote(subject, safe=""),
            filename=quote(filename, safe=""),
            ref=quote(ref, safe=""),
        )

    def _media_resolver(self, subject: str, filename: str,
                        present: Optional[set] = None) -> KR.MediaResolver:
        """构造渲染用的媒体解析器：存在则给端点 URL，缺失给 None。"""
        store = self._store()

        def resolve(ref: str) -> Optional[str]:
            try:
                if present is not None:
                    if not store._resolve_media_name(present, ref):  # noqa: SLF001
                        return None
                else:
                    store.resolve_media(subject, filename, ref)
            except Exception:
                return None
            return self._media_endpoint(subject, filename, ref)

        return resolve

    # =================================================================== API
    @permission_required("user")
    def api_meta(self):
        store = self._store()
        key_obj = store.load_key_obj()
        desc = KC.describe_key_obj(key_obj) if key_obj else None
        return self.success_response({
            "plugin": self.name,
            "title": self.title,
            "version": self.version,
            # 注意：只回指纹/位数，绝不回 SYMMETRIC_KEY 或私钥
            "key": {
                "configured": key_obj is not None,
                "fingerprint_short": (desc or {}).get("fingerprint_short"),
                "rsa_bits": (desc or {}).get("rsa_bits"),
                "public_matches_private": (desc or {}).get("public_matches_private"),
            },
            "crypto_available": KC.CRYPTO_AVAILABLE,
            "max_upload_mb": MAX_UPLOAD_MB,
            "allowed_upload_types": list(ALLOWED_UPLOAD_TYPES),
            "media_exts": list(KS.MEDIA_EXTS),
            "allow_external_media": self._allow_external_media(),
            # AI 评卷可用性（只回布尔与模型名，绝不回 key）
            "ai": {
                "available": self._make_grader() is not None,
                "enabled": bool(self._deepseek_conf().get("enabled")),
                "configured": bool(str(self._deepseek_conf().get("api_key") or "")),
                "model": self._deepseek_status().get("model"),
            },
            "role": self._role(),
            "is_admin": self._is_admin(),
            "can_manage": self._is_admin(),
        })

    # ---------------- 学科 ----------------
    @permission_required("user")
    def api_list_subjects(self):
        try:
            subjects = self._store().list_subjects()
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "读取学科列表失败：")
        # 对非管理员裁剪掉可能敏感的字段（有效签名等不在此列，这里主要是统一结构）
        if not self._is_admin():
            for subj in subjects:
                for bank in subj.get("banks", []):
                    bank.pop("uuid", None)
        return self.success_response({
            "subjects": subjects,
            "total": {
                "subjects": len(subjects),
                "banks": sum(s.get("bank_count", 0) for s in subjects),
                "items": sum(s.get("item_count") or 0 for s in subjects),
                "media": sum(s.get("media_count") or 0 for s in subjects),
            },
            "is_admin": self._is_admin(),
        })

    @permission_required("admin")
    def api_create_subject(self):
        payload = request.get_json(silent=True) or {}
        name = payload.get("name") or request.form.get("name") or ""
        try:
            result = self._store().create_subject(name)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "新建学科失败：")
        return self.success_response(result, "学科已创建")

    @permission_required("admin")
    def api_delete_subject(self, subject: str):
        force = str(request.args.get("force", "")).lower() in ("1", "true", "yes")
        try:
            result = self._store().delete_subject(subject, force=force)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "删除学科失败：")
        return self.success_response(result, "学科已删除")

    # ---------------- 题库 ----------------
    @permission_required("user")
    def api_bank_detail(self, subject: str, filename: str):
        try:
            detail = self._store().get_bank_detail(subject, filename)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "读取题库详情失败：")
        if not self._is_admin():
            detail.pop("provenance", None)
        return self.success_response(detail)

    @permission_required("admin")
    def api_upload_bank(self):
        subject = request.form.get("subject", "")
        overwrite = str(request.form.get("overwrite", "")).lower() in ("1", "true", "yes")
        try:
            temp_path, original = self.save_uploaded_file("file")
        except Exception as exc:  # noqa: BLE001  ValueError: 大小/类型不合规
            return self.error_response(f"上传被拒绝：{exc}", 400)

        try:
            with open(temp_path, "rb") as fp:
                payload = fp.read()
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

        ext = os.path.splitext(original)[1].lower()
        store = self._store()
        try:
            if ext in (".kqb", ".zip"):
                result = store.import_package(subject, payload, overwrite=overwrite)
                message = "整包导入成功"
            else:
                filename = KS.validate_bank_filename(original)
                result = store.write_bank_json(
                    subject, filename, payload, overwrite=overwrite)
                message = "题库上传成功"
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "上传失败：")
        result["kind"] = "package" if ext in (".kqb", ".zip") else "bank"
        return self.success_response(result, message)

    @permission_required("admin")
    def api_delete_bank(self, subject: str, filename: str):
        try:
            result = self._store().delete_bank(subject, filename)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "删除题库失败：")
        return self.success_response(result, "题库已删除")

    @permission_required("user")
    def api_media(self, subject: str, filename: str, media_name: str):
        try:
            path, actual = self._store().resolve_media(subject, filename, media_name)
        except KS.StoreNotFoundError:
            return self.error_response("媒体文件不存在", 404)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "读取媒体失败：")
        return self.send_file_response(
            path,
            mimetype=KS.QbankStore.media_mime(actual),
            as_attachment=False,
            count_download=False,          # 不计入插件热度统计
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=300",
            },
        )

    # ---------------- 练习（M3）----------------
    def _load_parsed(self, subject: str, filename: str):
        """取「已解析题库 + 媒体解析器」，供练习/判分复用。"""
        bank_obj, _encrypted = self._store().load_bank_object(subject, filename)
        parsed = KP.parse_bank(bank_obj)
        media_dir = self._store().media_dir_for(subject, filename)
        present = set(self._store()._list_media_files(media_dir))  # noqa: SLF001
        resolve = self._media_resolver(subject, filename, present)
        return parsed, resolve

    @permission_required("user")
    def api_practice(self, subject: str, filename: str):
        """练习载荷：题干与选项的净化 HTML，**不含正确答案与解析**。"""
        chapters_raw = request.args.get("chapters", "").strip()
        chapters: Optional[List[int]] = None
        if chapters_raw:
            try:
                chapters = [int(x) for x in chapters_raw.replace(" ", "").split(",") if x]
            except ValueError:
                return self.error_response("chapters 参数格式错误（应为逗号分隔的章节号）", 400)
        try:
            parsed, resolve = self._load_parsed(subject, filename)
        except KS.StoreNotFoundError:
            return self.error_response("题库不存在", 404)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "读取题库失败：")

        try:
            payload = KPrac.build_payload(
                parsed,
                subject=subject,
                filename=filename,
                chapters=chapters,
                resolve=resolve,
                allow_external=self._allow_external_media(),
                shuffle_options=_truthy(
                    request.args.get("shuffle_options", "1"), default=True),
            )
        except KPrac.PracticeError as exc:
            return self.error_response(str(exc), 400)
        return self.success_response(payload)

    @permission_required("user")
    def api_grade(self, subject: str, filename: str):
        """单题判分或揭晓答案。

        body: {"item_id": "...", "answer": <任意>, "reveal": false}
        判分规则在服务端（km_parser.grade_item 单一真源），前端不重复实现。
        """
        body = request.get_json(silent=True) or {}
        item_id = str(body.get("item_id") or "").strip()
        if not item_id:
            return self.error_response("缺少 item_id", 400)
        reveal = bool(body.get("reveal"))
        try:
            parsed, resolve = self._load_parsed(subject, filename)
        except KS.StoreNotFoundError:
            return self.error_response("题库不存在", 404)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "读取题库失败：")
        try:
            result = KPrac.grade_item_payload(
                parsed, item_id, body.get("answer"), reveal=reveal,
                resolve=resolve, allow_external=self._allow_external_media(),
            )
        except KPrac.PracticeError as exc:
            return self.error_response(str(exc), 404)
        return self.success_response(result)

    @permission_required("user")
    def api_summary(self, subject: str, filename: str):
        """练习战绩汇总（错题清单与分题型统计）。"""
        body = request.get_json(silent=True) or {}
        records = body.get("records") or {}
        if not isinstance(records, dict):
            return self.error_response("records 必须是对象", 400)
        try:
            parsed, resolve = self._load_parsed(subject, filename)
        except KS.StoreNotFoundError:
            return self.error_response("题库不存在", 404)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "读取题库失败：")

        chapters_raw = body.get("chapters") or []
        try:
            chapters = [int(c) for c in chapters_raw] if chapters_raw else None
            payload = KPrac.build_payload(
                parsed, subject=subject, filename=filename, chapters=chapters,
                resolve=resolve, allow_external=self._allow_external_media(),
            )
        except (KPrac.PracticeError, TypeError, ValueError) as exc:
            return self.error_response(str(exc), 400)

        summary = KPrac.summarize_session(payload, records)
        summary["wrong_items"] = KPrac.build_wrong_export_view(payload, records)
        return self.success_response(summary)

    # ---------------- 导出（M4）----------------
    @permission_required("user")
    def api_export(self, subject: str, filename: str):
        """导出为 v3.1.0 题库（含相对媒体引用时自动打 .kqb 整包），可附 Markdown/HTML 错题本。

        body: {
          scope: "wrong"(默认) | "items" | "all",
          items: [item_id, ...],          // scope=items 时必填
          records: {item_id: {verdict, answer}},   // scope=wrong 时用
          pack: "auto"(默认) | "json" | "kqb",
          encrypt: true(默认),
          placement: "self_contained"(默认) | "detached",
          extras: ["md", "html"],
          title: "自定义标题"
        }

        scope=all（导出整库）仅管理员可用。
        """
        body = request.get_json(silent=True) or {}
        scope = str(body.get("scope") or "wrong").lower()
        if scope not in ("wrong", "items", "all"):
            return self.error_response("scope 只能是 wrong / items / all", 400)
        if scope == "all" and not self._is_admin():
            return self.error_response("导出整库需管理员权限", 403)

        try:
            parsed, resolve = self._load_parsed(subject, filename)
        except KS.StoreNotFoundError:
            return self.error_response("题库不存在", 404)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "读取题库失败：")

        # ---- 选定题目 ----
        all_ids = [i.item_id for i in parsed.items]
        records = body.get("records") or {}
        if not isinstance(records, dict):
            return self.error_response("records 必须是对象", 400)
        if scope == "all":
            selected = all_ids
        elif scope == "items":
            wanted = body.get("items") or []
            if not isinstance(wanted, list):
                return self.error_response("items 必须是数组", 400)
            selected = [i for i in wanted if i in set(all_ids)]
        else:
            selected = [
                i for i in all_ids
                if (records.get(i) or {}).get("verdict") in ("wrong", "revealed")
            ]
        if not selected:
            return self.error_response(
                "没有可导出的题目（错题汇总为空？可改用 scope=items 指定题目）", 400)

        # ---- 密钥 ----
        want_encrypt = body.get("encrypt")
        want_encrypt = True if want_encrypt is None else bool(want_encrypt)
        placement = str(body.get("placement") or KQB.PLACEMENT_SELF_CONTAINED)
        if placement not in (KQB.PLACEMENT_SELF_CONTAINED, KQB.PLACEMENT_DETACHED):
            return self.error_response("placement 只能是 self_contained / detached", 400)

        key_obj = self._store().load_key_obj()
        key_generated = False
        crypto = None
        if want_encrypt or placement == KQB.PLACEMENT_SELF_CONTAINED:
            if key_obj is None:
                if not KC.CRYPTO_AVAILABLE:
                    return self.error_response(
                        "缺少 cryptography 依赖，无法加密导出；可传 encrypt=false 导出明文", 400)
                key_obj = KC.generate_key_obj()
                key_generated = True
            try:
                crypto = KC.QuestionBankCrypto(key_obj)
            except KC.KaleidoCryptoError as exc:
                return self.error_response(f"密钥不可用：{exc}", 400)

        # ---- 构建 v3.1.0 题库（用原始对象重建，并重签名）----
        suffix = "错题" if scope == "wrong" else ("整库" if scope == "all" else "选题")
        default_title = f"{parsed.title or '题库'}（{suffix}）"
        title = str(body.get("title") or default_title)
        try:
            bank = KEX.build_export_bank(
                parsed, selected,
                title=title,
                author=parsed.author,
                description=f"由 Kaleido Mini 导出的{suffix}；来源《{parsed.title or ''}》",
                signer=(crypto.sign_question_bank if crypto else None),
                chapter_note="（错题）" if scope == "wrong" else "",
                extra_meta={
                    "exported_by": "kaleido_mini",
                    "exported_at": _now_iso(),
                    "source_title": parsed.title,
                    "source_uuid": parsed.uuid,
                    "export_scope": scope,
                },
            )
        except KEX.ExportError as exc:
            return self.error_response(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "构建导出题库失败：")

        media_refs = KEX.collect_media_refs(bank)

        # ---- 决定容器：含媒体引用 → 必须 .kqb ----
        pack = str(body.get("pack") or "auto").lower()
        if pack not in ("auto", "json", "kqb"):
            return self.error_response("pack 只能是 auto / json / kqb", 400)
        if pack == "auto":
            pack = "kqb" if media_refs else "json"
        if pack == "json" and media_refs:
            return self.error_response(
                f"导出的题目引用了 {len(media_refs)} 个媒体，必须用 .kqb 整包（pack=kqb）", 400)

        # ---- 媒体（只取被引用的）----
        def loader(ref: str):
            try:
                path, _actual = self._store().resolve_media(subject, filename, ref)
            except Exception:
                return None
            try:
                with open(path, "rb") as fp:
                    return fp.read()
            except OSError:
                return None

        media, missing = KEX.collect_media(media_refs, loader)

        # ---- 组装文件 ----
        base = KEX.build_base_filename(parsed, suffix=suffix, date_str=_today())
        files: Dict[str, bytes] = {}
        primary_name = ""

        if pack == "json":
            primary_name = f"{base}.json"
            files[primary_name] = json.dumps(
                bank, ensure_ascii=False, indent=2).encode("utf-8")
        else:
            primary_name = f"{base}.kqb"
            try:
                files[primary_name] = KQB.build_package(
                    bank, media,
                    kind=KQB.KIND_WRONG_ANSWER_EXPORT
                    if scope == "wrong" else KQB.KIND_FULL_BANK,
                    key_obj=key_obj,
                    placement=placement,
                    encrypt_bank=want_encrypt,
                    encrypt_media=want_encrypt,
                    readme=(
                        f"Kaleido Mini 导出\n范围：{scope}\n题目：{len(selected)} 条\n"
                        f"媒体：{len(media)} 个（缺失 {len(missing)} 个）\n"
                        f"密钥放置：{placement}\n"
                        + ("本包为自包含（含 key_obj.json），导入无需另发密钥\n"
                           if placement == KQB.PLACEMENT_SELF_CONTAINED
                           else "本包为分离密钥，需同时保存 .key_obj.json\n")
                    ),
                )
            except (KQB.KqbError, KC.KaleidoCryptoError) as exc:
                return self.error_response(f"打包失败：{exc}", 400)
            if placement == KQB.PLACEMENT_DETACHED and key_obj is not None:
                files[f"{base}.key_obj.json"] = json.dumps(
                    {f: key_obj[f] for f in KC.KEY_OBJ_FIELDS},
                    ensure_ascii=False, indent=2).encode("utf-8")

        extras = body.get("extras") or []
        if not isinstance(extras, list):
            return self.error_response("extras 必须是数组", 400)
        extras = {str(e).lower() for e in extras}
        if "md" in extras:
            try:
                files[f"{base}.md"] = KEX.build_markdown(
                    parsed, selected, records, title=title).encode("utf-8")
            except KEX.ExportError as exc:
                return self.error_response(str(exc), 400)
        if "html" in extras:
            try:
                files[f"{base}.html"] = KEX.build_html(
                    parsed, selected, records, title=title,
                    media_loader=loader, inline_media=True).encode("utf-8")
            except KEX.ExportError as exc:
                return self.error_response(str(exc), 400)

        # ---- 单文件直发；多文件打 zip ----
        if len(files) == 1:
            name, payload = next(iter(files.items()))
            mimetype = ("application/json" if name.endswith(".json")
                        else "application/octet-stream")
            return self._send_memory(payload, name, mimetype)

        bundle_name = f"{base}.zip"
        import zipfile  # 局部导入：仅导出多文件时需要

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=6) as zf:
            for name, payload in files.items():
                zf.writestr(name, payload)
        return self._send_memory(buf.getvalue(), bundle_name, "application/zip")

    def _send_memory(self, payload: bytes, download_name: str, mimetype: str):
        """内存态文件下发（导出产物）；中文名由 werkzeug 按 RFC 5987 编码。"""
        resp = send_file(io.BytesIO(payload), mimetype=mimetype,
                         as_attachment=True, download_name=download_name)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------------- AI 评卷（M5）----------------
    @permission_required("user")
    def api_grade_ai(self, subject: str, filename: str):
        """AI 评卷：仅填空题 / 简答题。

        body: {"item_id": "...", "answer": "考生作答"}
        key 只在服务端使用，绝不回传；错误信息已脱敏。
        """
        body = request.get_json(silent=True) or {}
        item_id = str(body.get("item_id") or "").strip()
        if not item_id:
            return self.error_response("缺少 item_id", 400)
        grader = self._make_grader()
        if grader is None:
            return self.error_response(
                "AI 评卷未启用或未配置 DeepSeek API Key，请在设置页配置", 400)

        try:
            parsed, resolve = self._load_parsed(subject, filename)
        except KS.StoreNotFoundError:
            return self.error_response("题库不存在", 404)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "读取题库失败：")

        item = KPrac.index_items(parsed).get(item_id)
        if item is None:
            return self.error_response(f"题目不存在：{item_id}", 404)
        if item.display_type not in (KP.DISPLAY_GAP, KP.DISPLAY_ESSAY):
            return self.error_response(
                "AI 评卷只用于填空题与简答题；选择题/判断题请用自动判分", 400)

        answer = body.get("answer")
        if isinstance(answer, list):
            user_text = " ｜ ".join(str(a) for a in answer)
        else:
            user_text = "" if answer is None else str(answer)

        question_text = KR.plain_text(
            (item.shared_stem + " " if item.shared_stem else "") + item.stem)
        reference_text = " ｜ ".join(str(c) for c in item.correct)
        analysis_text = KR.plain_text(item.analysis)

        try:
            result = grader.grade(
                question=question_text,
                reference=reference_text,
                user_answer=user_text,
                qtype_label=item.display_type,
                analysis=analysis_text,
            )
        except KG.GraderNotConfigured as exc:
            return self.error_response(str(exc), 400)
        except KG.GraderNetworkError as exc:
            # 502：上游不可用，不是调用方的错
            return self.error_response(f"AI 评卷网络失败：{exc}", 502)
        except KG.GraderResponseError as exc:
            return self.error_response(f"AI 评卷接口异常：{exc}", 502)

        # 一并把参考答案/解析返回，前端无需再发一次请求
        detail = KPrac.grade_item_payload(
            parsed, item_id, answer, reveal=True, resolve=resolve,
            allow_external=self._allow_external_media())

        if result.verdict == "correct":
            suggested = "correct"
        elif result.score is None:
            suggested = None
        else:
            suggested = "wrong"

        return self.success_response({
            "item_id": item_id,
            "score": result.score,
            "verdict": result.verdict,
            "comment": result.comment,
            "model": result.model,
            "usage": result.usage,
            "suggested_verdict": suggested,
            "user_answer": user_text,
            "correct": detail.get("correct"),
            "correct_display": detail.get("correct_display"),
            "analysis_html": detail.get("analysis_html"),
            "missing_media": detail.get("missing_media"),
        }, "AI 评卷完成")

    # ---------------- 设置（M5，管理员）----------------
    def _settings_payload(self) -> Dict[str, Any]:
        store = self._store()
        key_obj = store.load_key_obj()
        desc = KC.describe_key_obj(key_obj) if key_obj else {}
        return {
            "allow_external_media": self._allow_external_media(),
            "deepseek": self._deepseek_status(),
            "key": {
                "configured": key_obj is not None,
                "fingerprint_short": desc.get("fingerprint_short"),
                "rsa_bits": desc.get("rsa_bits"),
                "public_matches_private": desc.get("public_matches_private"),
                "path": "plugins/data/%s/keys/key_obj.json" % self.name,
            },
            "crypto_available": KC.CRYPTO_AVAILABLE,
            "crypto_error": "" if KC.CRYPTO_AVAILABLE else KC.CRYPTO_IMPORT_ERROR,
            "role": self._role(),
            "is_admin": self._is_admin(),
            "limits": {
                "max_upload_mb": MAX_UPLOAD_MB,
                "allowed_upload_types": list(ALLOWED_UPLOAD_TYPES),
                "media_exts": list(KS.MEDIA_EXTS),
            },
            "api_base": f"/api/{self.name}",
            "page_base": self._page_base(),
            "version": self.version,
        }

    @permission_required("admin")
    def api_get_settings(self):
        return self.success_response(self._settings_payload())

    @permission_required("admin")
    def api_save_settings(self):
        """保存设置。API Key 语义：缺省/"__KEEP__"=不变，"__CLEAR__"/空=清除。"""
        body = request.get_json(silent=True) or {}
        conf = dict(self.config) if isinstance(self.config, dict) else {}

        if "allow_external_media" in body:
            conf["allow_external_media"] = bool(body.get("allow_external_media"))

        ds_in = body.get("deepseek")
        if isinstance(ds_in, dict):
            ds = dict(conf.get("deepseek") or {})
            if "enabled" in ds_in:
                ds["enabled"] = bool(ds_in.get("enabled"))
            for field_name in ("base_url", "model"):
                if field_name in ds_in:
                    value = str(ds_in.get(field_name) or "").strip()
                    ds[field_name] = value or (
                        KG.DEFAULT_BASE_URL if field_name == "base_url" else KG.DEFAULT_MODEL)
            if "timeout" in ds_in:
                try:
                    ds["timeout"] = max(5, min(120, int(ds_in.get("timeout"))))
                except (TypeError, ValueError):
                    ds["timeout"] = KG.DEFAULT_TIMEOUT

            if "api_key" in ds_in:
                raw = ds_in.get("api_key")
                text = "" if raw is None else str(raw).strip()
                if text in ("__KEEP__",):
                    pass                                   # 保持不变
                elif text in ("__CLEAR__", ""):
                    ds["api_key"] = ""                     # 清除
                else:
                    ds["api_key"] = text
                    ds.setdefault("enabled", True)   # 首次填入 key 时默认启用
            conf["deepseek"] = ds

        self.config = conf
        try:
            ok = self.save_config()
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "保存配置失败：")
        if not ok:
            return self.error_response("保存配置失败（写入 plugins/configs 失败）", 500)
        return self.success_response(self._settings_payload(), "设置已保存")

    @permission_required("admin")
    def api_test_ai(self):
        """AI 连通性自检：用极小请求验证 key 与网络。"""
        ds = self._deepseek_conf()
        key = str(ds.get("api_key") or "").strip()
        if not key:
            return self.error_response("尚未配置 DeepSeek API Key", 400)
        grader = KG.DeepSeekGrader(
            key,
            base_url=str(ds.get("base_url") or KG.DEFAULT_BASE_URL),
            model=str(ds.get("model") or KG.DEFAULT_MODEL),
            timeout=int(ds.get("timeout") or KG.DEFAULT_TIMEOUT),
        )
        try:
            result = grader.ping()
        except KG.GraderNetworkError as exc:
            return self.error_response(f"连通性测试失败（网络）：{exc}", 502)
        except KG.GraderError as exc:
            return self.error_response(f"连通性测试失败：{exc}", 502)
        return self.success_response({
            "ok": True,
            "model": result.model,
            "score": result.score,
            "verdict": result.verdict,
            "comment": result.comment,
            "usage": result.usage,
        }, "AI 连通正常")

    # ---------------- 密钥文件管理（M5，管理员）----------------
    @permission_required("admin")
    def api_key_status(self):
        return self.success_response(self._settings_payload()["key"])

    @permission_required("admin")
    def api_key_upsert(self):
        """上传用户已有的 key_obj.json，或按原算法自动生成一个。"""
        store = self._store()
        payload = request.get_json(silent=True) or {}

        if payload.get("generate"):
            if not KC.CRYPTO_AVAILABLE:
                return self.error_response(
                    f"缺少 cryptography 依赖，无法生成密钥：{KC.CRYPTO_IMPORT_ERROR}", 400)
            if store.has_key() and not payload.get("overwrite"):
                return self.error_response(
                    "已存在密钥文件；覆盖会使既有加密题库与导出包无法解开，"
                    "如确认覆盖请传 overwrite=true", 409)
            try:
                key_obj = KC.generate_key_obj()
                store.save_key_obj(key_obj)
            except KC.KaleidoCryptoError as exc:
                return self.error_response(f"生成密钥失败：{exc}", 400)
            desc = KC.describe_key_obj(key_obj)
            return self.success_response({
                "configured": True,
                "generated": True,
                "fingerprint_short": desc.get("fingerprint_short"),
                "rsa_bits": desc.get("rsa_bits"),
                "warning": "新密钥已生成；请立即下载备份，丢失后加密题库与导出包将无法恢复。",
            }, "密钥已生成")

        if "file" not in request.files:
            return self.error_response("请上传 key_obj.json 或传 generate=true", 400)
        try:
            temp_path, original = self.save_uploaded_file("file")
        except Exception as exc:  # noqa: BLE001
            return self.error_response(f"上传被拒绝：{exc}", 400)
        try:
            with open(temp_path, "rb") as fp:
                raw = fp.read()
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

        try:
            key_obj = json.loads(raw.decode("utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return self.error_response(f"密钥文件不是合法 JSON：{exc}", 400)
        errors = KC.validate_key_obj(key_obj)
        if errors:
            return self.error_response("密钥文件校验失败：" + "；".join(errors), 400)
        check = KC.verify_key_obj(key_obj)
        if not check["ok"]:
            return self.error_response(f"密钥自检未通过：{check['error']}", 400)
        try:
            store.save_key_obj(key_obj)
        except KC.KaleidoCryptoError as exc:
            return self.error_response(f"保存密钥失败：{exc}", 400)
        desc = KC.describe_key_obj(key_obj)
        return self.success_response({
            "configured": True,
            "source_filename": original,
            "fingerprint_short": desc.get("fingerprint_short"),
            "rsa_bits": desc.get("rsa_bits"),
        }, "密钥文件已导入并生效")

    @permission_required("admin")
    def api_key_download(self):
        """下载母密钥文件（高敏操作，仅在管理员显式确认后调用）。"""
        path = self._store().key_path()
        if not os.path.isfile(path):
            return self.error_response("尚未配置密钥文件", 404)
        return self.send_file_response(
            path,
            download_name="kaleido_mini_key_obj.json",
            mimetype="application/json",
            as_attachment=True,
            count_download=False,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "X-Kaleido-Warning": "master-key-handle-with-care",
            },
        )

    # ---------------- 整包 ----------------
    @permission_required("admin")
    def api_package_inspect(self):
        try:
            temp_path, original = self.save_uploaded_file("file")
        except Exception as exc:  # noqa: BLE001
            return self.error_response(f"上传被拒绝：{exc}", 400)
        try:
            with open(temp_path, "rb") as fp:
                payload = fp.read()
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

        result = KQB.inspect_package(payload, key_obj=self._store().load_key_obj())
        summary = result.summary()
        summary["filename"] = original
        summary["will_import"] = result.ok
        return self.success_response(
            summary,
            "整包核对通过，可以导入" if result.ok else "整包核对未通过",
        )

    @permission_required("admin")
    def api_package_import(self):
        subject = request.form.get("subject", "")
        overwrite = str(request.form.get("overwrite", "")).lower() in ("1", "true", "yes")
        filename = request.form.get("filename", "") or ""
        try:
            temp_path, original = self.save_uploaded_file("file")
        except Exception as exc:  # noqa: BLE001
            return self.error_response(f"上传被拒绝：{exc}", 400)
        try:
            with open(temp_path, "rb") as fp:
                payload = fp.read()
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

        try:
            result = self._store().import_package(
                subject, payload, overwrite=overwrite, filename=filename)
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "整包导入失败：")
        result["source_filename"] = original
        return self.success_response(result, "整包导入成功")

    # ================================================================ 页面
    def _page_base(self) -> str:
        return f"/plugin/{self.name}"

    def _page_error(self, message: str, code: int = 404):
        """页面出错时返回真正的 Response。

        必须这样做：页面分发器只认 Response 与 dict，若返回 `self.error_response(...)`
        的 `(jsonify, code)` 元组，分发器会把它当成空上下文去渲染目标模板 → 500 而非 404/403。
        （框架缺陷 FTK-KM-04，见 docs/框架协调清单.md）
        """
        from flask import make_response, render_template

        template = "404.html" if code == 404 else ("403.html" if code == 403 else "500.html")
        return make_response(render_template(template, message=message), code)

    def _bank_detail_url(self, subject: str, filename: str) -> str:
        return "{base}/bank/{subject}/{filename}".format(
            base=self._page_base(),
            subject=quote(subject, safe=""),
            filename=quote(filename, safe=""),
        )

    def _bank_exam_url(self, subject: str, filename: str) -> str:
        return "{base}/exam/{subject}/{filename}".format(
            base=self._page_base(),
            subject=quote(subject, safe=""),
            filename=quote(filename, safe=""),
        )

    def render_index(self) -> Dict[str, Any]:
        """主入口 /plugin/kaleido_mini 的上下文。"""
        try:
            subjects = self._store().list_subjects()
        except Exception as exc:  # noqa: BLE001
            subjects = []
            self.logger.warning(f"读取学科列表失败：{exc}")
        store = self._store()

        # 模板里避免处理 URL 编码（subject/filename 可含中文与空格），统一在服务端算好
        for subj in subjects:
            for bank in subj.get("banks", []):
                bank["detail_url"] = self._bank_detail_url(subj["name"], bank["filename"])
                bank["exam_url"] = self._bank_exam_url(subj["name"], bank["filename"])
            subj["delete_url"] = "/api/{n}/subjects/{s}".format(
                n=self.name, s=quote(subj["name"], safe=""))

        return {
            "name": self.name,
            "title": self.title,
            "version": self.version,
            "role": self._role(),
            "is_admin": self._is_admin(),
            "subjects": subjects,
            "has_key": store.has_key(),
            "crypto_available": KC.CRYPTO_AVAILABLE,
            "max_upload_mb": MAX_UPLOAD_MB,
            "allowed_upload_types": list(ALLOWED_UPLOAD_TYPES),
            "media_exts": list(KS.MEDIA_EXTS),
            "api_base": f"/api/{self.name}",
            "page_base": self._page_base(),
            # 注意：键名刻意避开 dict 的内置方法名（items/keys/values/get/copy…）。
            # Jinja2 的 `dict.items` 会优先命中内置方法，模板里写成 {{ summary.items }}
            # 会渲染出 "<built-in method items of dict object ...>"。
            "summary": {
                "subject_count": len(subjects),
                "bank_count": sum(s.get("bank_count", 0) for s in subjects),
                "item_count": sum(s.get("item_count") or 0 for s in subjects),
                "media_count": sum(s.get("media_count") or 0 for s in subjects),
                "media_missing_count": sum(
                    b.get("media_missing_count") or 0
                    for s in subjects for b in s.get("banks", [])
                ),
            },
        }

    def page_bank(self, subject: str, filename: str):
        """题库详情页（含少量题目的服务端安全渲染预览）。"""
        try:
            detail = self._store().get_bank_detail(subject, filename)
        except KS.StoreNotFoundError:
            return self._page_error("题库不存在", 404)
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"读取题库详情失败：{exc}")
            return self._page_error(f"读取题库失败：{exc}", 500)

        previews = self._render_previews(subject, filename, detail)
        return {
            "name": self.name,
            "title": self.title,
            "version": self.version,
            "role": self._role(),
            "is_admin": self._is_admin(),
            "subject": detail["subject"],
            "filename": detail["filename"],
            "detail": detail,
            "previews": previews,
            "api_base": f"/api/{self.name}",
            "exam_url": self._bank_exam_url(detail["subject"], detail["filename"]),
        }

    # 注意：**不能**给页面路由加 @permission_required("admin")。
    # 框架缺陷（FTK-KM-04）：页面分发器只处理 Response 与 dict，
    # 而 permission 装饰器拒绝页面请求时返回的是 `(render_template('403.html'), 403)` 元组
    # → 分发器把它当空上下文渲染目标模板 → 500 而不是 403。
    # 因此改为在视图内自行判权并返回真正的 Response（route 里的 permission 仅作文档）。
    def page_settings(self):
        """设置页（管理员）：密钥文件 / AI 评卷 / 外链图片策略。"""
        if not self._is_admin():
            from flask import make_response, render_template

            return make_response(
                render_template("403.html", message="仅管理员可访问插件设置页"), 403)
        payload = self._settings_payload()
        return {
            "name": self.name,
            "title": self.title,
            "version": self.version,
            "role": self._role(),
            "is_admin": self._is_admin(),
            "settings": payload,
            "api_base": payload["api_base"],
            "page_base": payload["page_base"],
        }

    def page_exam(self, subject: str, filename: str):
        """练习页（自测）。重数据走 /practice 接口按需拉取，页面只给骨架与元信息。"""
        try:
            detail = self._store().get_bank_detail(subject, filename)
        except KS.StoreNotFoundError:
            return self._page_error("题库不存在", 404)
        except Exception as exc:  # noqa: BLE001
            self.logger.error(f"读取题库详情失败：{exc}")
            return self._page_error(f"读取题库失败：{exc}", 500)

        summary = detail.get("summary") or {}
        return {
            "name": self.name,
            "title": self.title,
            "version": self.version,
            "role": self._role(),
            "is_admin": self._is_admin(),
            "subject": detail["subject"],
            "filename": detail["filename"],
            "bank_title": summary.get("title") or detail["stem"],
            "bank_author": summary.get("author") or "",
            "bank_format": ".".join(str(x) for x in (summary.get("format_version") or [])),
            "item_count": summary.get("item_count") or 0,
            "chapter_count": summary.get("chapter_count") or 0,
            "type_counts": summary.get("type_counts") or {},
            "media_missing_count": (detail.get("media") or {}).get("missing_count") or 0,
            "chapters": detail.get("chapters") or [],
            "api_base": f"/api/{self.name}",
            "page_base": self._page_base(),
            "detail_url": self._bank_detail_url(detail["subject"], detail["filename"]),
        }

    def _render_previews(self, subject: str, filename: str,
                         detail: Dict[str, Any], limit: int = 3) -> List[Dict[str, Any]]:
        """取前若干题做一次真实的安全渲染，供详情页预览（也顺带验证净化链路）。"""
        try:
            bank_obj, _ = self._store().load_bank_object(subject, filename)
            parsed = KP.parse_bank(bank_obj)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning(f"预览渲染失败：{exc}")
            return []

        media_dir = self._store().media_dir_for(subject, filename)
        present = set(self._store()._list_media_files(media_dir))  # noqa: SLF001
        resolve = self._media_resolver(subject, filename, present)
        allow_external = self._allow_external_media()

        previews: List[Dict[str, Any]] = []
        for item in parsed.items[:limit]:
            stem = KR.render_rich_text(
                item.stem, is_html=item.html,
                resolve_media=resolve, allow_external_media=allow_external)
            shared = KR.render_rich_text(
                item.shared_stem, is_html=item.html,
                resolve_media=resolve, allow_external_media=allow_external) \
                if item.shared_stem else None
            options = {
                key: KR.render_rich_text(
                    value, is_html=item.html,
                    resolve_media=resolve, allow_external_media=allow_external,
                    wrap=False)["html"]
                for key, value in item.options.items()
            }
            previews.append({
                "item_id": item.item_id,
                "chapter_name": item.chapter_name,
                "type_label": item.type_label,
                "display_type": item.display_type,
                "group_index": item.group_index,
                "group_size": item.group_size,
                "stem_html": stem["html"],
                "shared_stem_html": shared["html"] if shared else "",
                "options_html": options,
                "correct": item.correct,
                "missing_media": stem["missing_media"] + (shared["missing_media"] if shared else []),
                "analysis_html": KR.render_rich_text(
                    item.analysis, is_html=item.html,
                    resolve_media=resolve,
                    allow_external_media=allow_external)["html"],
            })
        return previews
