# -*- coding: utf-8 -*-
"""kqb_pack —— 把「题库 JSON + 同名媒体目录」打成 KQB v1 整包（.kqb）

用法示例::

    # 自动发现同名媒体目录，沿用旁边的 key_obj.json；没有就自动生成
    python tools/kqb_pack.py 眼科学题库_System.json -o 眼科学题库_System.kqb

    # 指定密钥、明文包（不加密）
    python tools/kqb_pack.py bank.json --key key_obj.json --no-crypt

    # 分离密钥（包里不带 key_obj.json，需另行发送密钥文件）
    python tools/kqb_pack.py bank.json --key key_obj.json --placement detached

    # 打包错题导出（kind 标记，便于上层区分）
    python tools/kqb_pack.py 错题.json --kind wrong_answer_export

退出码：0 成功；1 参数/IO/核对错误；2 打包后自检未通过
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


def _load_json(path):
    with open(path, "r", encoding="utf-8-sig") as fp:
        return json.load(fp)


def _human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024.0
    return f"{n}B"


def build_parser():
    p = argparse.ArgumentParser(
        description="把题库 JSON 打成 KQB v1 整包（.kqb）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("bank", help="题库 JSON 文件路径")
    p.add_argument("-o", "--out", help="输出 .kqb 路径（默认与题库同名）")
    p.add_argument("--media-dir", help="媒体目录（默认取与题库同名的兄弟目录）")
    p.add_argument("--key", help="全题库密钥文件 key_obj.json")
    p.add_argument("--gen-key", action="store_true",
                   help="忽略自动发现，强制生成新密钥")
    p.add_argument("--key-out", help="生成密钥时的保存路径（默认 <输出名>.key_obj.json）")
    p.add_argument("--placement", choices=[KP.PLACEMENT_SELF_CONTAINED, KP.PLACEMENT_DETACHED],
                   default=KP.PLACEMENT_SELF_CONTAINED,
                   help="密钥放置方式：self_contained（随包）/ detached（分离），默认 self_contained")
    p.add_argument("--kind", default=KP.KIND_FULL_BANK,
                   help=f"包类型标记，默认 {KP.KIND_FULL_BANK}")
    p.add_argument("--no-crypt", action="store_true",
                   help="完全不加密（bank 与 media 均明文，无需密钥）")
    p.add_argument("--plain-bank", action="store_true", help="只把 bank 存为明文（媒体仍加密）")
    p.add_argument("--plain-media", action="store_true", help="只把媒体存为明文（bank 仍加密）")
    p.add_argument("--readme", help="写入包内 README.txt 的文本")
    p.add_argument("-q", "--quiet", action="store_true", help="减少输出")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    bank_path = os.path.abspath(args.bank)
    if not os.path.isfile(bank_path):
        print(f"[错误] 题库文件不存在：{bank_path}", file=sys.stderr)
        return 1
    try:
        bank_obj = _load_json(bank_path)
    except json.JSONDecodeError as exc:
        print(f"[错误] 题库不是合法 JSON：{exc}", file=sys.stderr)
        return 1

    if "encrypted_content" in bank_obj:
        print("[错误] 这是加密题库，请先解密再打包", file=sys.stderr)
        return 1

    stem = os.path.splitext(os.path.basename(bank_path))[0]
    out_path = os.path.abspath(args.out or os.path.join(os.path.dirname(bank_path),
                                                        f"{stem}.kqb"))

    # ---- 媒体 ----
    if args.media_dir:
        media_dir = os.path.abspath(args.media_dir)
        if not os.path.isdir(media_dir):
            print(f"[错误] 媒体目录不存在：{media_dir}", file=sys.stderr)
            return 1
        media = {}
        for name in sorted(os.listdir(media_dir)):
            full = os.path.join(media_dir, name)
            if not os.path.isfile(full):
                continue
            try:
                KP.validate_media_name(name)
            except KP.KqbSecurityError as exc:
                print(f"[跳过] {exc}")
                continue
            with open(full, "rb") as fp:
                media[name] = fp.read()
    else:
        media = KP.collect_media_from_sibling_dir(bank_path)

    if not args.quiet:
        print(f"题库：{bank_path}")
        print(f"同名媒体目录：{'有，共 ' + str(len(media)) + ' 个文件' if media else '无'}")

    # ---- 密钥 ----
    need_key = not args.no_crypt
    key_obj = None
    key_source = "未使用"

    if need_key:
        if args.key:
            key_path = os.path.abspath(args.key)
            try:
                key_obj = KC.load_key_obj(key_path)
            except KC.KaleidoCryptoError as exc:
                print(f"[错误] 密钥文件不可用：{exc}", file=sys.stderr)
                return 1
            key_source = f"指定文件 {key_path}"
        elif not args.gen_key:
            discovered = os.path.join(os.path.dirname(bank_path), "key_obj.json")
            if os.path.isfile(discovered):
                try:
                    key_obj = KC.load_key_obj(discovered)
                    key_source = f"自动发现 {discovered}"
                except KC.KaleidoCryptoError as exc:
                    print(f"[警告] 自动发现的密钥不可用（{exc}），将生成新密钥")
                    key_obj = None

        if key_obj is None:
            try:
                key_obj = KC.generate_key_obj()
            except KC.KaleidoCryptoError as exc:
                print(f"[错误] 生成密钥失败：{exc}", file=sys.stderr)
                return 1
            key_out = os.path.abspath(
                args.key_out or os.path.join(os.path.dirname(out_path),
                                             f"{stem}.key_obj.json"))
            try:
                KC.save_key_obj(key_obj, key_out)
            except KC.KaleidoCryptoError as exc:
                print(f"[错误] 保存密钥失败：{exc}", file=sys.stderr)
                return 1
            key_source = f"新生成 {key_out}"

        check = KC.verify_key_obj(key_obj)
        if not check["ok"]:
            print(f"[错误] 密钥自检未通过：{check['error']}", file=sys.stderr)
            return 1
        if not args.quiet:
            desc = KC.describe_key_obj(key_obj)
            print(f"密钥：{key_source}")
            print(f"      公钥指纹 {desc.get('fingerprint_short')}…  RSA {desc.get('rsa_bits')} bits")

    # ---- 打包 ----
    try:
        payload = KP.build_package(
            bank_obj,
            media,
            kind=args.kind,
            key_obj=key_obj,
            placement=args.placement,
            encrypt_bank=not (args.no_crypt or args.plain_bank),
            encrypt_media=not (args.no_crypt or args.plain_media),
            readme=args.readme or "",
        )
    except (KP.KqbError, KC.KaleidoCryptoError) as exc:
        print(f"[错误] 打包失败：{exc}", file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as fp:
        fp.write(payload)

    # ---- 打包后自检（防止生成了打不开的包）----
    result = KP.inspect_package(payload, key_obj=key_obj)
    if not args.quiet:
        print(f"输出：{out_path}（{_human_bytes(len(payload))}）")
        summary = result.summary()
        bs = summary.get("bank_summary") or {}
        if bs:
            fv = ".".join(str(x) for x in (bs.get("format_version") or []))
            print(f"题库：v{fv}  {bs.get('chapter_count')} 章  {bs.get('item_count')} 条  "
                  f"题型 {bs.get('type_counts')}")
        m = result.manifest or {}
        media_sec = m.get("media") or {}
        print(f"媒体：mode={media_sec.get('mode')}  {media_sec.get('count')} 个  "
              f"{_human_bytes(media_sec.get('total_bytes') or 0)}")
        missing = media_sec.get("missing_refs") or []
        if missing:
            print(f"      题库引用但未打包 {len(missing)} 个"
                  f"（导入后降级为占位）：{missing[:8]}")
        unreferenced = media_sec.get("unreferenced") or []
        if unreferenced:
            print(f"      打包但题库未引用 {len(unreferenced)} 个：{unreferenced[:8]}")
        # 显示「实际生效」的放置方式（无密钥时会自动降级为 detached）
        effective = (m.get("key") or {}).get("placement") or "-"
        note = "" if effective == args.placement else f"（请求 {args.placement}，已自动调整）"
        print(f"密钥放置：{effective}{note}")

    if not result.ok:
        print("[失败] 打包产物自检未通过：", file=sys.stderr)
        for c in result.failures:
            print(f"  [{c.id}] {c.title}：{c.detail}", file=sys.stderr)
        return 2

    for c in result.warnings:
        print(f"[提示] [{c.id}] {c.title}：{c.detail}")
    print("✅ 打包完成并已通过核对")
    return 0


if __name__ == "__main__":
    sys.exit(main())
