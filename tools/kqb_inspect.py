# -*- coding: utf-8 -*-
"""kqb_inspect —— 只读核对 KQB v1 整包（.kqb），逐项打印核对清单

用法示例::

    python tools/kqb_inspect.py 眼科学题库_System.kqb
    python tools/kqb_inspect.py 包.kqb --key key_obj.json
    python tools/kqb_inspect.py 包.kqb --json > report.json

只读、无副作用：不解包落盘，仅在内存中核对。

退出码：0 核对通过；2 核对未通过（fatal）；1 参数/IO 错误
"""

from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from kaleido_mini import km_crypto as KC      # noqa: E402
from kaleido_mini import km_package as KP    # noqa: E402

_STATUS_MARK = {
    "pass": "✅ 通过",
    "fail": "❌ 失败",
    "warn": "⚠️  提示",
    "skip": "－  跳过",
}


def build_parser():
    p = argparse.ArgumentParser(
        description="只读核对 KQB v1 整包",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("package", help=".kqb / .zip 整包路径")
    p.add_argument("--key", help="全题库密钥文件 key_obj.json（分离密钥包必需）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出核对报告")
    p.add_argument("--show-media", action="store_true", help="列出全部媒体文件名")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    path = os.path.abspath(args.package)
    if not os.path.isfile(path):
        print(f"[错误] 文件不存在：{path}", file=sys.stderr)
        return 1

    key_obj = None
    if args.key:
        try:
            key_obj = KC.load_key_obj(os.path.abspath(args.key))
        except KC.KaleidoCryptoError as exc:
            print(f"[错误] 密钥文件不可用：{exc}", file=sys.stderr)
            return 1

    try:
        result = KP.inspect_package(path, key_obj=key_obj)
    except Exception as exc:  # 兜底：核对过程自身不应崩，但要报清楚
        print(f"[错误] 核对过程异常：{exc}", file=sys.stderr)
        return 1

    summary = result.summary()

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if result.ok else 2

    print("=" * 74)
    print(f"KQB 整包核对：{os.path.basename(path)}")
    print(f"大小：{os.path.getsize(path)} 字节")
    print("=" * 74)

    for check in result.checks:
        mark = _STATUS_MARK.get(check.status, check.status)
        print(f"[{check.id:>2}] {mark}  {check.title}")
        if check.detail:
            print(f"          {check.detail}")

    manifest = result.manifest or {}
    bank_sec = manifest.get("bank") or {}
    media_sec = manifest.get("media") or {}
    key_sec = manifest.get("key") or {}
    enc_sec = manifest.get("media_encryption") or {}

    print("-" * 74)
    print(f"格式版本   : {manifest.get('kqb_format')} v"
          f"{'.'.join(str(x) for x in (manifest.get('kqb_version') or []))}"
          f"   kind={manifest.get('kind')}")
    print(f"创建时间   : {manifest.get('created_at')}")
    generator = manifest.get("generator") or {}
    print(f"生成器     : {generator.get('name')} / {generator.get('plugin')} "
          f"{generator.get('version')}")
    print(f"题库       : {bank_sec.get('title')} — {bank_sec.get('author')}")
    print(f"           加密={bank_sec.get('encrypted')}  {bank_sec.get('bytes')} 字节  "
          f"uuid={bank_sec.get('uuid')}")
    bs = summary.get("bank_summary") or {}
    if bs:
        fv = ".".join(str(x) for x in (bs.get("format_version") or []))
        print(f"           格式 v{fv}  {bs.get('chapter_count')} 章  "
              f"{bs.get('item_count')} 条  题型 {bs.get('type_counts')}")
    print(f"媒体       : mode={media_sec.get('mode')}  {media_sec.get('count')} 个  "
          f"{media_sec.get('total_bytes')} 字节")
    if enc_sec:
        print(f"           加密算法 {enc_sec.get('algorithm')}  IV {enc_sec.get('iv_source')}  "
              f"MAC {enc_sec.get('mac')}")
        print(f"           CryptoJS 可实现：{enc_sec.get('crypto_js_compatible')}")
    if media_sec.get("missing_refs"):
        print(f"           引用但缺失：{len(media_sec['missing_refs'])} 个 "
              f"{media_sec['missing_refs'][:5]}")
    if media_sec.get("unreferenced"):
        print(f"           打包但未引用：{len(media_sec['unreferenced'])} 个")
    print(f"密钥       : placement={key_sec.get('placement')}  "
          f"RSA {key_sec.get('rsa_bits')}  指纹 {str(key_sec.get('public_key_sha256'))[:16]}…")
    sig = manifest.get("signature")
    print(f"清单签名   : {'有 ' + str(sig.get('algorithm')) if sig else '无'}")

    if args.show_media and result.media:
        print("-" * 74)
        print(f"媒体清单（{len(result.media)}）：")
        for name in sorted(result.media):
            print(f"  {len(result.media[name]):>10} B  {name}")

    print("-" * 74)
    if result.ok:
        print(f"结果：✅ 核对通过（{summary['warn_count']} 条提示）")
        return 0
    print("结果：❌ 核对未通过")
    for c in result.failures:
        print(f"  [{c.id}] {c.title}：{c.detail}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
