# -*- coding: utf-8 -*-
"""M2 回归：km_render —— HTML 白名单净化 / 媒体重写 / 缺失占位 / 注入防护

题库内容是不可信输入，本组用例是插件的 XSS 主防线。

运行： python tests/test_render.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from kaleido_mini import km_parser as KP     # noqa: E402
from kaleido_mini import km_render as KR     # noqa: E402

KALEIDO = os.environ.get("KALEIDO_PROJECT_ROOT", r"H:\Coding\Projects\Kaleido\Project")
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


def resolver_for(present, prefix="/api/kaleido_mini/media/"):
    def resolve(ref):
        return f"{prefix}{ref}" if ref in present else None
    return resolve


def main():
    print("=" * 78)
    print("M2 / km_render 回归")
    print("=" * 78)

    # ---------------- 1. 纯文本路径 ----------------
    r = KR.render_rich_text("<script>alert(1)</script>纯文本", is_html=False, wrap=False)
    check("is_html=False 时标签被转义", "&lt;script&gt;" in r["html"], r["html"])
    check("纯文本结果标记 is_html=False", r["is_html"] is False)

    r = KR.render_rich_text("第一行\n第二行", is_html=False, wrap=False)
    check("纯文本换行转 <br>", "<br>" in r["html"], r["html"])

    # 含标签即按 HTML 处理（html_content 标记不可全信）
    r = KR.render_rich_text("<b>粗体</b>", is_html=False, wrap=False)
    check("含标签时自动按 HTML 处理", "<b>粗体</b>" in r["html"], r["html"])

    # ---------------- 2. XSS / 危险内容 ----------------
    dangerous_snippets = [
        ("<script>alert(1)</script>安全", "<script"),
        ("<style>body{display:none}</style>安全", "<style"),
        ("<iframe src='http://evil.com'></iframe>安全", "<iframe"),
        ("<object data='x'></object>安全", "<object"),
        ("<embed src='x'>安全", "<embed"),
        ("<svg><script>alert(1)</script></svg>安全", "<svg"),
    ]
    for raw, needle in dangerous_snippets:
        out = KR.render_rich_text(raw, is_html=True, wrap=False)["html"]
        check(f"危险标签被清除：{needle}", needle not in out.lower(), out[:80])
        check(f"安全文本被保留：{needle}", "安全" in out, out[:80])

    out = KR.render_rich_text('<div onclick="alert(1)">文字</div>', is_html=True, wrap=False)["html"]
    check("on* 事件属性被剥离", "onclick" not in out.lower(), out)
    check("被剥属性的文本仍在", "文字" in out, out)

    out = KR.render_rich_text('<div style="position:fixed">x</div>', is_html=True, wrap=False)["html"]
    check("style 属性被剥离", "style=" not in out.lower(), out)

    out = KR.render_rich_text('<a href="javascript:alert(1)">点</a>', is_html=True, wrap=False)["html"]
    check("javascript: 链接被剥离", "javascript" not in out.lower(), out)
    out = KR.render_rich_text('<a href="vbscript:x">点</a>', is_html=True, wrap=False)["html"]
    check("vbscript: 链接被剥离", "vbscript" not in out.lower(), out)

    out = KR.render_rich_text('<img src="javascript:alert(1)">', is_html=True, wrap=False)["html"]
    check("img javascript: 被拦", "javascript" not in out.lower(), out)

    out = KR.render_rich_text('<img src="data:text/html;base64,PHNjcmlwdD4=">',
                              is_html=True, wrap=False)["html"]
    check("data:text/html 被拦", "data:text/html" not in out.lower(), out)

    out = KR.render_rich_text('<div class="ftk-card evil">x</div>', is_html=True, wrap=False)["html"]
    check("非白名单 class 被剥离", "ftk-card" not in out and "evil" not in out, out)

    out = KR.render_rich_text('<div class="img_container keep">x</div>',
                              is_html=True, wrap=False)["html"]
    check("白名单 class 被保留", 'class="img_container' in out, out)

    # ---------------- 3. 媒体重写 ----------------
    present = {"1.png", "2.png"}
    resolve = resolver_for(present)

    r = KR.render_rich_text('<img src="/media/1.png" data-img-type="url">',
                            is_html=True, resolve_media=resolve, wrap=False)
    check("相对媒体被重写为端点 URL",
          "/api/kaleido_mini/media/1.png" in r["html"], r["html"])
    check("重写后丢弃 data-img-type", "data-img-type" not in r["html"], r["html"])
    check("img 自动加 loading=lazy", 'loading="lazy"' in r["html"], r["html"])
    check("media_refs 记录引用", r["media_refs"] == ["1.png"], str(r["media_refs"]))

    r = KR.render_rich_text('<img src="/media/9.png">', is_html=True,
                            resolve_media=resolve, wrap=False)
    check("缺失媒体被记录", r["missing_media"] == ["9.png"], str(r["missing_media"]))
    check("缺失媒体输出占位", "kqb-media-missing" in r["html"], r["html"])
    check("缺失媒体不产生 img 标签", "<img" not in r["html"], r["html"])

    # 扩展名回退由 store 的 resolver 负责；这里只断言 renderer [原样透传] 引用名
    seen_refs = []

    def recording_resolver(ref):
        seen_refs.append(ref)
        return "/rewritten" if ref in present else None

    KR.render_rich_text('<img src="/media/1.jpg">', is_html=True,
                        resolve_media=recording_resolver, wrap=False)
    check("renderer 原样透传引用名给 resolver（不自行改扩展名）",
          seen_refs == ["1.jpg"], str(seen_refs))

    r = KR.render_rich_text('<img src="1.png">', is_html=True,
                            resolve_media=resolve, wrap=False)
    check("裸文件名被当作媒体引用", "media/1.png" in r["html"], r["html"])

    b64 = '<img src="data:image/png;base64,iVBORw0KGgo=" data-img-type="base64">'
    r = KR.render_rich_text(b64, is_html=True, resolve_media=resolve, wrap=False)
    check("base64 内联图片原样保留",
          "data:image/png;base64" in r["html"], r["html"])
    check("base64 不计入 media_refs", r["media_refs"] == [], str(r["media_refs"]))

    # ---------------- 4. 外部图片：默认允许 + 防跟踪 ----------------
    r = KR.render_rich_text('<img src="https://cdn.example.com/a.png">',
                            is_html=True, wrap=False)
    check("外链图片默认保留", "cdn.example.com" in r["html"], r["html"])
    check("外链图片加 referrerpolicy=no-referrer",
          'referrerpolicy="no-referrer"' in r["html"], r["html"])
    check("外链记录在 external_media",
          r["external_media"] == ["https://cdn.example.com/a.png"],
          str(r["external_media"]))

    r = KR.render_rich_text('<img src="https://cdn.example.com/a.png">',
                            is_html=True, allow_external_media=False, wrap=False)
    check("关闭外链后图片被拦",
          "cdn.example.com" not in r["html"], r["html"])
    check("关闭外链后给出占位", "kqb-media-missing" in r["html"], r["html"])

    # ---------------- 5. richtext 伪标签（含真实畸形数据） ----------------
    r = KR.render_rich_text("<richtext><b>暂无</b>解析<richtext>", is_html=True, wrap=False)
    check("richtext 标记被剥离", "richtext" not in r["html"].lower(), r["html"])
    check("richtext 内容保留", "暂无" in r["html"] and "解析" in r["html"], r["html"])

    # ---------------- 6. 标签平衡 ----------------
    out = KR.render_rich_text("<div><b>未闭合", is_html=True, wrap=False)["html"]
    check("未闭合标签被自动补齐",
          out.count("<div") == out.count("</div>") and out.count("<b") == out.count("</b>"), out)
    out = KR.render_rich_text("<p>a</div></p>b", is_html=True, wrap=False)["html"]
    check("错配闭合不产生负深度", out.count("<p") == out.count("</p>"), out)

    # ---------------- 7. 表格与富文本保留 ----------------
    table = "<table><tr><td>甲</td><td>乙</td></tr></table>"
    out = KR.render_rich_text(table, is_html=True, wrap=False)["html"]
    check("表格标签保留", "<table>" in out and "<td>甲</td>" in out, out)

    out = KR.render_rich_text('<video src="/media/2.png" controls poster="/media/1.png"></video>',
                              is_html=True, resolve_media=resolve, wrap=False)["html"]
    check("video src 被重写", 'src="/api/kaleido_mini/media/2.png"' in out, out)
    check("video poster 也被重写", 'poster="/api/kaleido_mini/media/1.png"' in out, out)

    out = KR.render_rich_text('<video poster="javascript:alert(1)" src="/media/1.png"></video>',
                              is_html=True, resolve_media=resolve, wrap=False)["html"]
    check("video poster 的 javascript: 被拦", "javascript" not in out.lower(), out)

    # ---------------- 8. 包裹容器 ----------------
    r = KR.render_rich_text("<b>x</b>", is_html=True, wrap=True)
    check("wrap=True 包裹 kqb-rt", 'class="kqb-rt"' in r["html"], r["html"])

    # ---------------- 9. 空值与超长 ----------------
    check("None 返回空 html", KR.render_rich_text(None)["html"] == "")
    check("空白返回空 html", KR.render_rich_text("   ")["html"] == "")
    long_text = "x" * (KR.MAX_FIELD_CHARS + 100)
    r = KR.render_rich_text(long_text, is_html=False, wrap=False)
    check("超长文本被截断并标记", r.get("truncated") is True and len(r["html"]) <= KR.MAX_FIELD_CHARS + 10,
          str(len(r["html"])))

    # ---------------- 10. 真实题库渲染 ----------------
    real = os.path.join(QBANKS, "眼科学", "眼科学题库_System.json")
    if os.path.isfile(real):
        with open(real, "r", encoding="utf-8-sig") as fp:
            bank = json.load(fp)
        parsed = KP.parse_bank(bank)
        media_dir = os.path.join(QBANKS, "眼科学", "眼科学题库_System")
        present_real = set(os.listdir(media_dir)) if os.path.isdir(media_dir) else set()
        resolve_real = resolver_for(present_real)

        total_items = 0
        missing_total = []
        injected = 0
        for item in parsed.items:
            total_items += 1
            rendered = KR.render_rich_text(
                item.stem, is_html=item.html, resolve_media=resolve_real)
            missing_total.extend(rendered["missing_media"])
            low = rendered["html"].lower()
            if "<script" in low or "onerror=" in low or "javascript:" in low:
                injected += 1
        check("真实题库全部条目渲染无异常", total_items > 600, f"{total_items} 条")
        check("真实题库渲染无 script/javascript 残留", injected == 0, str(injected))
        check("真实题库媒体全部解析成功（58 个都在）",
              not missing_total, str(missing_total[:5]))

        # 抽一条含图片的题干做具体断言
        with_img = [i for i in parsed.items if "/media/" in (i.stem or "")][:1]
        if with_img:
            out = KR.render_rich_text(with_img[0].stem, is_html=with_img[0].html,
                                      resolve_media=resolve_real, wrap=False)["html"]
            check("含图题干被重写为插件端点",
                  "/api/kaleido_mini/media/" in out, out[:120])
            check("含图题干保留 img 标签", "<img" in out, out[:120])
    else:
        print(f"[SKIP] {real}")

    # 缺媒体题库：渲染应产出占位而不是崩
    merged = os.path.join(QBANKS, "眼科学",
                          "眼科题库（合并）_大连医科大学附属第一医院眼科_1.json")
    if os.path.isfile(merged):
        with open(merged, "r", encoding="utf-8-sig") as fp:
            bank2 = json.load(fp)
        parsed2 = KP.parse_bank(bank2)
        missing_all = 0
        for item in parsed2.items:
            rendered = KR.render_rich_text(item.stem, is_html=item.html,
                                          resolve_media=resolver_for(set()))
            missing_all += len(rendered["missing_media"])
        check("73 个媒体全缺时渲染不崩且记录缺失", missing_all > 0, str(missing_all))
    else:
        print(f"[SKIP] {merged}")

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
