# -*- coding: utf-8 -*-
"""M2 端到端回归：在【隔离运行时】验证插件真实可用性（HTTP 层）

覆盖：
  * 插件加载、页面渲染、静态资源
  * 管理员：建学科 / 上传 JSON / 整包导入 / 媒体下发 / 删除
  * 权限分级：管理员 vs 普通用户 vs 未登录（逐接口比对状态码）
  * 媒体扩展名回退（引用 1.jpg、实际 1.png）经真实 HTTP 拉取验证

前置：
  1. 隔离运行时已解压到 .runtime 并装好 kaleido_mini
  2. 服务已启动：.runtime 下 `python app.py`
  3. 默认管理员 admin/admin123（auth 插件默认账户）

运行：
  python tests/e2e_runtime.py
  BASE_URL=http://127.0.0.1:5000 python tests/e2e_runtime.py
"""

import json
import os
import sys
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import requests  # noqa: E402

from kaleido_mini import km_crypto as KC      # noqa: E402
from kaleido_mini import km_package as KP     # noqa: E402

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:5000").rstrip("/")
ADMIN_USER = os.environ.get("KM_ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("KM_ADMIN_PASS", "admin123")
TEST_USER = "km_tester"
TEST_PASS = "km_tester_123"

KALEIDO = os.environ.get("KALEIDO_PROJECT_ROOT", r"H:\Coding\Projects\Kaleido\Project")
STD = os.path.join(KALEIDO, "standardization")
QBANKS = os.path.join(KALEIDO, "backup", "qbanks")

SUBJECT = "端到端测试学科"
PLUGIN_API = "/api/kaleido_mini"
PLUGIN_PAGE = "/plugin/kaleido_mini"

RESULTS = []


def check(name, cond, detail="", extra=""):
    RESULTS.append((name, bool(cond), detail))
    shown = extra or detail
    line = f"[{'PASS' if cond else 'FAIL'}] {name}"
    if shown:
        line += (f"  ({shown})" if cond else f"  << {shown}")
    print(line)
    return bool(cond)


class Client:
    """带 CSRF 双提交的会话封装。"""

    def __init__(self):
        self.s = requests.Session()

    @property
    def csrf(self):
        return self.s.cookies.get("csrf_token", "")

    def request(self, method, path, **kw):
        headers = kw.pop("headers", {}) or {}
        if method.upper() not in ("GET", "HEAD", "OPTIONS"):
            token = self.csrf
            if token:
                headers["X-CSRF-Token"] = token
        return self.s.request(method, BASE + path, headers=headers, timeout=120, **kw)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)

    def login(self, username, password):
        r = self.post("/api/auth/login", json={"username": username, "password": password})
        ok = r.status_code == 200 and bool(self.csrf)
        return ok, r

    def api_json(self, method, path, **kw):
        r = self.request(method, path, **kw)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"_raw": r.text[:200]}


def code_of(payload):
    return (payload or {}).get("code")


def main():
    print("=" * 78)
    print(f"M2 端到端回归 @ {BASE}")
    print("=" * 78)

    # ---------------- 0. 服务可达 ----------------
    try:
        health = requests.get(BASE + "/", timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] 无法连接 {BASE}：{exc}")
        print("        请先启动隔离运行时：cd .runtime && python app.py")
        return 1
    check("服务可达", health.status_code in (200, 302), f"HTTP {health.status_code}")

    # ---------------- 1. 未登录访问需 401 ----------------
    anon = Client()
    st, body = anon.api_json("GET", PLUGIN_API + "/subjects")
    check("未登录访问 API 返回 401", st == 401, f"HTTP {st}")

    # ---------------- 2. 管理员登录 ----------------
    admin = Client()
    ok, r = admin.login(ADMIN_USER, ADMIN_PASS)
    check("管理员登录成功", ok, f"HTTP {r.status_code} csrf={bool(admin.csrf)}")
    if not ok:
        print("[FATAL] 登录失败，后续用例跳过")
        return 1

    st, meta = admin.api_json("GET", PLUGIN_API + "/meta")
    check("meta 接口 200", st == 200 and code_of(meta) == 200, f"HTTP {st}")
    d = meta.get("data") or {}
    check("meta 显示角色为 admin", d.get("role") == "admin", str(d.get("role")))
    check("meta 显示 can_manage=True", d.get("can_manage") is True)
    check("meta 含密钥状态字段", "key" in d and "configured" in (d.get("key") or {}))
    check("meta 未泄露密钥内容",
          not any(k in json.dumps(d) for k in ("SYMMETRIC_KEY", "ENCRYPTED_PRIVATE_KEY")))
    check("cryptography 可用", d.get("crypto_available") is True)

    # ---------------- 3. 页面与静态资源 ----------------
    r = admin.get(PLUGIN_PAGE)
    check("主入口页面 200", r.status_code == 200, f"HTTP {r.status_code}")
    check("页面引用了 plugin_common.js（CSRF 前提）",
          "/static/js/plugin_common.js" in r.text)
    check("页面引用了插件样式",
          "/plugin-static/kaleido_mini/css/km.css" in r.text)
    r = admin.get("/plugin-static/kaleido_mini/css/km.css")
    check("插件 CSS 可访问", r.status_code == 200,
          f"HTTP {r.status_code} {len(r.content)}B")
    check("CSS 内容非空", "km-page" in r.text)
    r = admin.get("/plugin-static/kaleido_mini/js/km_index.js")
    check("插件 JS 可访问", r.status_code == 200, f"HTTP {r.status_code}")

    # ---- 页面结构回归（防 UI 缺陷复发）----
    page_html = admin.get(PLUGIN_PAGE).text
    check("统计区不出现 dict 方法泄漏（<built-in method）",
          "<built-in method" not in page_html)
    check("取消按钮为 type=button（否则必填校验会拦住取消）",
          page_html.count('type="button" class="km-btn" data-role="cancel"') >= 3,
          f"命中 {page_html.count('data-role=\"cancel\"')} 个 cancel")
    check("提交按钮显式为 type=submit",
          page_html.count('type="submit"') >= 3, str(page_html.count('type="submit"')))
    check("不存在旧的 value=cancel 写法", 'value="cancel"' not in page_html)
    check("先核对按钮为 type=button", 'type="button" class="km-btn" data-role="inspect"' in page_html)

    # ---------------- 4. 清理残留，建学科 ----------------
    admin.delete(PLUGIN_API + "/subjects/" + quote(SUBJECT) + "?force=1")
    st, body = admin.api_json("POST", PLUGIN_API + "/subjects", json={"name": SUBJECT})
    check("新建学科 200", st == 200 and code_of(body) == 200,
          f"HTTP {st} {body.get('message')}")

    st, body = admin.api_json("POST", PLUGIN_API + "/subjects", json={"name": SUBJECT})
    check("重复建学科返回 409", st == 409, f"HTTP {st}")

    st, body = admin.api_json("POST", PLUGIN_API + "/subjects", json={"name": "../逃逸"})
    check("非法学科名返回 400", st == 400, f"HTTP {st} {body.get('message')}")

    # ---------------- 5. 上传单个 JSON 题库 ----------------
    sample = os.path.join(STD, "QuizBank_sample_v3.json")
    if not os.path.isfile(sample):
        print(f"[SKIP] 缺少 {sample}")
        return 1
    with open(sample, "rb") as fp:
        raw = fp.read()

    st, body = admin.api_json(
        "POST", PLUGIN_API + "/qbanks",
        data={"subject": SUBJECT},
        files={"file": ("样例题库.json", raw, "application/json")})
    check("上传 JSON 题库 200", st == 200 and code_of(body) == 200,
          f"HTTP {st} {body.get('message')}")
    check("上传返回条目数 13", (body.get("data") or {}).get("item_count") == 13,
          str((body.get("data") or {}).get("item_count")))

    st, body = admin.api_json(
        "POST", PLUGIN_API + "/qbanks",
        data={"subject": SUBJECT},
        files={"file": ("样例题库.json", raw, "application/json")})
    check("同名重复上传返回 409", st == 409, f"HTTP {st}")

    # ---------------- 6. 整包导入（含媒体，验证回退）----------------
    quiz_bank = os.path.join(QBANKS, "眼科学", "眼科学随堂测试合集_眼科学教研室.json")
    if os.path.isfile(quiz_bank):
        with open(quiz_bank, "r", encoding="utf-8-sig") as fp:
            qb = json.load(fp)
        media = KP.collect_media_from_sibling_dir(quiz_bank)
        pack_key = KC.generate_key_obj()      # 自包含包：自带密钥，服务端无需预置
        pkg = KP.build_package(qb, media, key_obj=pack_key,
                               placement=KP.PLACEMENT_SELF_CONTAINED)

        st, body = admin.api_json(
            "POST", PLUGIN_API + "/packages/inspect",
            data={"subject": SUBJECT},
            files={"file": ("随堂测试.kqb", pkg, "application/octet-stream")})
        check("整包预检 200", st == 200 and code_of(body) == 200,
              f"HTTP {st} {body.get('message')}")
        pd = body.get("data") or {}
        check("预检报告 ok=True", pd.get("ok") is True, str(pd.get("fatal_count")))
        check("预检报告 will_import=True", pd.get("will_import") is True)
        check("预检统计含媒体数", (pd.get("bank_summary") or {}).get("media_ref_count") == 5,
              str((pd.get("bank_summary") or {}).get("media_ref_count")))
        check("预检摘要含题库标题（面板要显示）",
              bool((pd.get("bank_summary") or {}).get("title")),
              str((pd.get("bank_summary") or {}).get("title")))

        st, body = admin.api_json(
            "POST", PLUGIN_API + "/packages",
            data={"subject": SUBJECT, "filename": "随堂测试.json"},
            files={"file": ("随堂测试.kqb", pkg, "application/octet-stream")})
        check("整包导入 200", st == 200 and code_of(body) == 200,
              f"HTTP {st} {body.get('message')}")
        check("导入写入媒体 5 个", (body.get("data") or {}).get("media_written") == 5,
              str((body.get("data") or {}).get("media_written")))

        # 媒体端点 + 扩展名回退：题库引用 1.jpg，实际文件 1.png
        st, body = admin.api_json(
            "GET", PLUGIN_API + "/qbanks/" + quote(SUBJECT) + "/" + quote("随堂测试.json"))
        check("题库详情 200", st == 200 and code_of(body) == 200, f"HTTP {st}")
        detail = body.get("data") or {}
        check("详情含章节", len(detail.get("chapters") or []) == 4,
              str(len(detail.get("chapters") or [])))
        fallback = (detail.get("media") or {}).get("fallback") or {}
        check("详情记录 1.jpg → 1.png 回退", fallback.get("1.jpg") == "1.png", str(fallback))

        media_url = "{api}/qbanks/{subj}/{fn}/media/{name}".format(
            api=PLUGIN_API, subj=quote(SUBJECT), fn=quote("随堂测试.json"),
            name=quote("1.jpg"))
        r = admin.get(media_url)
        check("按引用名 1.jpg 拉取媒体返回 200（回退生效）", r.status_code == 200,
              f"HTTP {r.status_code}")
        check("回退媒体 content-type 为图片",
              (r.headers.get("Content-Type") or "").startswith("image/"),
              r.headers.get("Content-Type"))
        check("媒体响应带 nosniff",
              r.headers.get("X-Content-Type-Options") == "nosniff",
              str(r.headers.get("X-Content-Type-Options")))
        check("媒体内容与磁盘文件一致（png 魔数）", r.content[:8] == b"\x89PNG\r\n\x1a\n",
              str(r.content[:8]))

        r = admin.get("{api}/qbanks/{subj}/{fn}/media/{name}".format(
            api=PLUGIN_API, subj=quote(SUBJECT), fn=quote("随堂测试.json"),
            name=quote("不存在的.png")))
        check("不存在的媒体返回 404", r.status_code == 404, f"HTTP {r.status_code}")

        # 穿越尝试 A：含 '/'。Werkzeug 解码 %2F 后不再是单段，路由层即不匹配，
        # 根本到不了插件处理器 —— 只要不是 200 且不吐文件内容即为安全。
        # 注：当前框架派发器对未匹配路径返回 405（而非 404），且"支持的方法"为空，
        #     这是 dispatcher 的逻辑缺陷，已记入 docs/框架协调清单.md。
        r = admin.get("{api}/qbanks/{subj}/{fn}/media/{name}".format(
            api=PLUGIN_API, subj=quote(SUBJECT), fn=quote("随堂测试.json"),
            name=quote("../escape.png")))
        check("媒体名含 '/' 的穿越不被服务（非 200）",
              r.status_code != 200 and b"\x89PNG" not in r.content,
              f"HTTP {r.status_code}")

        # 穿越尝试 B：反斜杠。解码后仍是单段 → 会进到插件处理器，
        # 必须由 km_store 的名称校验挡下（400）。
        r = admin.get("{api}/qbanks/{subj}/{fn}/media/{name}".format(
            api=PLUGIN_API, subj=quote(SUBJECT), fn=quote("随堂测试.json"),
            name=quote("..\\escape.png")))
        check("媒体名含反斜杠的穿越被插件拒绝（400）", r.status_code == 400,
              f"HTTP {r.status_code}")

        # 详情页
        r = admin.get("{page}/bank/{subj}/{fn}".format(
            page=PLUGIN_PAGE, subj=quote(SUBJECT), fn=quote("随堂测试.json")))
        check("题库详情页 200", r.status_code == 200, f"HTTP {r.status_code}")
        check("详情页含媒体自检区块", "媒体自检" in r.text)
        check("详情页含扩展名回退提示", "扩展名回退" in r.text)
        check("详情页题干已净化（无原始 script）", "<script>alert" not in r.text)
    else:
        print(f"[SKIP] 缺少 {quiz_bank}")

    # ---------------- 6b. 页面级媒体重写（合成题库，精确验证）----------------
    # 随堂测试前 3 题不含媒体，故单独造一个「首题就带图」的题库验证渲染链路：
    # 净化 → /media/ 重写为插件端点 → 详情页出现该端点 URL。
    synth_key = KC.generate_key_obj()
    synth_question = {
        "question_id": 1, "question_type": "A1", "skippable": True,
        "html_content": True,
        "question": ('<richtext><img src="/media/pic.png" data-img-type="url">'
                     '看图选择正确答案<script>alert(1)</script></richtext>'),
        "options": {"A": "甲", "B": "乙"},
        "correct_answers": ["A"],
        "analysis": '<img src="https://cdn.example.com/x.png">外链解析',
    }
    synth_bank = {
        "format_version": [3, 1, 0],
        "title": "媒体渲染测试", "author": "e2e", "description": "含图题干",
        "uuid": "", "valid_key": "",
        "chapters": [
            {
                "chapter_id": 1,
                "chapter_name": "第一章",
                "content": [synth_question],
            }
        ],
    }
    synth_bank = KC.QuestionBankCrypto(synth_key).sign_question_bank(synth_bank)
    synth_pkg = KP.build_package(
        synth_bank, {"pic.png": b"\x89PNG\r\n\x1a\nFAKE"},
        key_obj=synth_key, placement=KP.PLACEMENT_SELF_CONTAINED)
    st, body = admin.api_json(
        "POST", PLUGIN_API + "/packages",
        data={"subject": SUBJECT, "filename": "媒体渲染测试.json"},
        files={"file": ("媒体渲染测试.kqb", synth_pkg, "application/octet-stream")})
    check("合成含图题库导入 200", st == 200 and code_of(body) == 200,
          f"HTTP {st} {body.get('message')}")

    r = admin.get("{page}/bank/{subj}/{fn}".format(
        page=PLUGIN_PAGE, subj=quote(SUBJECT), fn=quote("媒体渲染测试.json")))
    check("含图题库详情页 200", r.status_code == 200, f"HTTP {r.status_code}")
    check("详情页把 /media/ 重写为插件端点",
          "/api/kaleido_mini/qbanks/" in r.text and "pic.png" in r.text)
    check("详情页剥离了题库里的 <script>",
          "<script>alert(1)</script>" not in r.text and "alert(1)" not in r.text)
    check("详情页外链图片带 no-referrer（防跟踪）",
          "referrerpolicy" in r.text or "cdn.example.com" not in r.text)
    r_media = admin.get("{api}/qbanks/{subj}/{fn}/media/{name}".format(
        api=PLUGIN_API, subj=quote(SUBJECT), fn=quote("媒体渲染测试.json"),
        name=quote("pic.png")))
    check("合成题库媒体可取回", r_media.status_code == 200 and r_media.content[:4] == b"\x89PNG",
          f"HTTP {r_media.status_code}")

    # ---------------- 6c. 练习页与判分（M3）----------------
    SYNTH_FN = "媒体渲染测试.json"

    def export_path(fn):
        """导出接口路径（供 6c/6d 复用）"""
        return "{api}/qbanks/{subj}/{fn}/export".format(
            api=PLUGIN_API, subj=quote(SUBJECT), fn=quote(fn))

    practice_path = "{api}/qbanks/{subj}/{fn}/practice".format(
        api=PLUGIN_API, subj=quote(SUBJECT), fn=quote(SYNTH_FN))

    r = admin.get("{page}/exam/{subj}/{fn}".format(
        page=PLUGIN_PAGE, subj=quote(SUBJECT), fn=quote(SYNTH_FN)))
    check("练习页 200", r.status_code == 200, f"HTTP {r.status_code}")
    check("练习页含开始面板", "开始练习" in r.text)
    check("练习页引用了 km_exam.js",
          "/plugin-static/kaleido_mini/js/km_exam.js" in r.text)
    check("练习页引用 plugin_common.js（CSRF 前提）",
          "/static/js/plugin_common.js" in r.text)
    r = admin.get("/plugin-static/kaleido_mini/js/km_exam.js")
    check("练习页 JS 可访问", r.status_code == 200, f"HTTP {r.status_code}")

    # ---- 回归：作者样式曾覆盖 UA 的 [hidden]，导致开始前就显示练习主体/工具栏 ----
    exam_html = admin.get("{page}/exam/{subj}/{fn}".format(
        page=PLUGIN_PAGE, subj=quote(SUBJECT), fn=quote(SYNTH_FN))).text
    check("练习主体初始为 hidden", 'id="km-session" hidden' in exam_html)
    check("练习工具栏初始为 hidden", 'id="km-exam-actions" hidden' in exam_html)
    check("继续上次进度初始为 hidden", 'id="km-resume" hidden' in exam_html)
    css = admin.get("/plugin-static/kaleido_mini/css/km.css").text
    check("CSS 含 [hidden] 兜底规则（!important）",
          "[hidden]" in css and "display: none !important" in css)
    # 回归：`.km-page a` 曾压过 `.km-btn-primary` 的文字色 → 蓝底蓝字看不见按钮
    check("链接样式排除了 .km-btn（否则按钮文字与背景同色）",
          "a:not(.km-btn)" in css)
    check("按钮文字色用 .km-page 前缀提高特指度",
          ".km-page .km-btn-primary" in css and ".km-page .km-btn " in css)

    # ---- 回归：JS 里不得裸 fetch(apiPath(...))（缺 /api/<name> 前缀 → 404）----
    exam_js = admin.get("/plugin-static/kaleido_mini/js/km_exam.js").text
    check("练习页导出走 KM.requestRaw（不裸 fetch apiPath）",
          "KM.requestRaw('POST', apiPath('/export')" in exam_js
          and "fetch(apiPath(" not in exam_js)
    common_js = admin.get("/plugin-static/kaleido_mini/js/km_common.js").text
    check("KM 暴露 requestRaw 且负责拼 API 前缀",
          "requestRaw: requestRaw" in common_js
          and "path.indexOf('http') === 0 ? path : API_BASE + path" in common_js)

    st, body = admin.api_json("GET", practice_path)
    check("练习载荷 200", st == 200 and code_of(body) == 200, f"HTTP {st}")
    pdata = body.get("data") or {}
    check("练习载荷含 1 条题目", pdata.get("item_count") == 1, str(pdata.get("item_count")))
    pblob = json.dumps(pdata, ensure_ascii=False)
    check("练习载荷不含正确答案", '"correct"' not in pblob and "correct_display" not in pblob)
    check("练习载荷不含解析", "analysis_html" not in pblob)
    check("练习载荷含题干 HTML", pdata["items"][0]["stem_html"].startswith('<div class="kqb-rt">'),
          pdata["items"][0]["stem_html"][:40])
    check("练习载荷题干已含插件媒体端点",
          "/api/kaleido_mini/qbanks/" in pdata["items"][0]["stem_html"])
    item_id = pdata["items"][0]["item_id"]
    check("题目 id 形如 c1:q1", item_id == "c1:q1", item_id)

    # ---- 选项随机化（默认开启）与关闭 ----
    check("默认开启选项随机化", pdata.get("shuffle_options") is True,
          str(pdata.get("shuffle_options")))
    p_opts = pdata["items"][0]["options"]
    check("选项为有序数组且含展示键/规范键",
          isinstance(p_opts, list) and all("key" in o and "canonical" in o for o in p_opts),
          str(p_opts[:1]))
    check("随机化后展示键仍是连续的 A/B…",
          [o["key"] for o in p_opts] == ["A", "B"][:len(p_opts)],
          str([o["key"] for o in p_opts]))
    check("规范键集合与原题库一致",
          sorted(o["canonical"] for o in p_opts) == ["A", "B"],
          str([o["canonical"] for o in p_opts]))

    st, body2 = admin.api_json("GET", practice_path + "?shuffle_options=0")
    p2 = (body2.get("data") or {})
    check("关闭随机化后 shuffle_options=False", p2.get("shuffle_options") is False)
    check("关闭随机化后展示键 = 规范键",
          [o["key"] for o in p2["items"][0]["options"]]
          == [o["canonical"] for o in p2["items"][0]["options"]],
          str([(o["key"], o["canonical"]) for o in p2["items"][0]["options"]]))

    # ---- 选项键通用化：28 个选项（A..Z, AA, AB） ----
    def excel_keys(n):
        out = []
        for i in range(1, n + 1):
            n2, s = i, ""
            while n2 > 0:
                n2, rem = divmod(n2 - 1, 26)
                s = chr(65 + rem) + s
            out.append(s)
        return out

    big_keys = excel_keys(28)
    big_bank = {
        "format_version": [3, 1, 0],
        "title": "多选项测试", "author": "e2e", "description": "28 个选项",
        "uuid": "", "valid_key": "",
        "chapters": [{"chapter_id": 1, "chapter_name": "第一章", "content": [{
            "question_id": 1, "question_type": "X", "skippable": True,
            "html_content": False,
            "question": "请选出正确的两项（28 个选项）",
            "options": {k: f"选项{k}" for k in big_keys},
            "correct_answers": ["AA", "B"],
            "analysis": "AA 与 B 正确",
        }]}],
    }
    big_bank = KC.QuestionBankCrypto(KC.generate_key_obj()).sign_question_bank(big_bank)
    r = admin.request(
        "POST", PLUGIN_API + "/qbanks",
        data={"subject": SUBJECT},
        files={"file": ("多选项测试.json",
                        json.dumps(big_bank, ensure_ascii=False).encode(),
                        "application/json")})
    check("上传 28 选项题库 200", r.status_code == 200, f"HTTP {r.status_code}")

    big_practice = "{api}/qbanks/{subj}/{fn}/practice?shuffle_options=0".format(
        api=PLUGIN_API, subj=quote(SUBJECT), fn=quote("多选项测试.json"))
    st, body = admin.api_json("GET", big_practice)
    big_item = (body.get("data") or {}).get("items", [{}])[0]
    big_opts = big_item.get("options") or []
    check("28 选项全部下发", len(big_opts) == 28, str(len(big_opts)))
    check("第 27 个选项键为 AA", big_opts[26]["key"] == "AA",
          str(big_opts[26] if len(big_opts) > 26 else None))
    check("第 28 个选项键为 AB", big_opts[27]["key"] == "AB",
          str(big_opts[27] if len(big_opts) > 27 else None))
    check("选项键覆盖 A..Z 无重复",
          [o["key"] for o in big_opts[:26]] == excel_keys(26),
          str([o["key"] for o in big_opts[:5]]))

    big_grade = "{api}/qbanks/{subj}/{fn}/grade".format(
        api=PLUGIN_API, subj=quote(SUBJECT), fn=quote("多选项测试.json"))
    st, body = admin.api_json("POST", big_grade, json={
        "item_id": big_item["item_id"], "answer": ["B", "AA"]})
    check("多字母答案 AA+B 判对", (body.get("data") or {}).get("verdict") == "correct",
          str((body.get("data") or {}).get("verdict")))
    check("判分返回答案按语义序 B 在 AA 前",
          (body.get("data") or {}).get("correct") == ["B", "AA"],
          str((body.get("data") or {}).get("correct")))
    st, body = admin.api_json("POST", big_grade, json={
        "item_id": big_item["item_id"], "answer": ["B", "A"]})
    check("把 AA 当成 A 判错", (body.get("data") or {}).get("verdict") == "wrong",
          str((body.get("data") or {}).get("verdict")))

    # 导出必须用 [规范键与原始顺序]，不受随机化影响
    r = admin.request("POST", export_path("多选项测试.json"), json={
        "scope": "items", "items": [big_item["item_id"]], "pack": "json"})
    check("多选项题库导出 200", r.status_code == 200, f"HTTP {r.status_code}")
    exp_q = json.loads(r.content.decode("utf-8"))["chapters"][0]["content"][0]
    check("导出保留原始选项顺序 A..AB",
          list(exp_q["options"].keys()) == big_keys,
          str(list(exp_q["options"].keys())[:5]))
    # 导出 [忠实保存源题库的 correct_answers 顺序]，不重排；消费侧（判分）自行归一化
    check("导出答案为规范键且未被拆散", sorted(exp_q["correct_answers"]) == sorted(["B", "AA"]),
          str(exp_q["correct_answers"]))

    grade_path = "{api}/qbanks/{subj}/{fn}/grade".format(
        api=PLUGIN_API, subj=quote(SUBJECT), fn=quote(SYNTH_FN))
    st, body = admin.api_json("POST", grade_path, json={"item_id": item_id, "answer": "A"})
    check("判分（答对）200", st == 200 and code_of(body) == 200, f"HTTP {st}")
    gd = body.get("data") or {}
    check("答对 verdict=correct", gd.get("verdict") == "correct", str(gd.get("verdict")))
    check("判分返回正确答案", gd.get("correct") == ["A"], str(gd.get("correct")))
    check("判分返回解析 HTML", "analysis_html" in gd)

    st, body = admin.api_json("POST", grade_path, json={"item_id": item_id, "answer": "B"})
    check("答错 verdict=wrong", (body.get("data") or {}).get("verdict") == "wrong",
          str((body.get("data") or {}).get("verdict")))

    st, body = admin.api_json("POST", grade_path, json={"item_id": item_id, "reveal": True})
    check("揭晓 verdict=revealed", (body.get("data") or {}).get("verdict") == "revealed",
          str((body.get("data") or {}).get("verdict")))

    st, body = admin.api_json("POST", grade_path, json={"item_id": "不存在", "answer": "A"})
    check("判分不存在的题目 → 404", st == 404, f"HTTP {st}")
    st, body = admin.api_json("POST", grade_path, json={"answer": "A"})
    check("判分缺 item_id → 400", st == 400, f"HTTP {st}")

    summary_path = "{api}/qbanks/{subj}/{fn}/summary".format(
        api=PLUGIN_API, subj=quote(SUBJECT), fn=quote(SYNTH_FN))
    st, body = admin.api_json("POST", summary_path, json={
        "records": {item_id: {"verdict": "wrong", "answer": "B"}}})
    check("汇总 200", st == 200 and code_of(body) == 200, f"HTTP {st}")
    sd = body.get("data") or {}
    check("汇总 total=1", sd.get("total") == 1, str(sd.get("total")))
    check("汇总 accuracy=0", sd.get("accuracy") == 0, str(sd.get("accuracy")))
    check("汇总含错题清单", len(sd.get("wrong_items") or []) == 1,
          str(len(sd.get("wrong_items") or [])))

    # 未登录不得拉练习载荷
    st, _ = anon.api_json("GET", practice_path)
    check("未登录拉练习载荷 → 401", st == 401, f"HTTP {st}")

    # ---------------- 6d. 错题导出（M4）----------------
    # （export_path 定义在 6c 之前，供两处复用）

    # (a) 含媒体引用的题库 + 错题 → auto 应出 .kqb
    r = admin.request("POST", export_path(SYNTH_FN), json={
        "scope": "wrong",
        "records": {item_id: {"verdict": "wrong", "answer": "B"}},
        "pack": "auto", "encrypt": True, "placement": "self_contained",
    })
    check("导出（含媒体）200", r.status_code == 200, f"HTTP {r.status_code}")
    cd = r.headers.get("Content-Disposition") or ""
    check("含媒体时自动出 .kqb", ".kqb" in cd, cd[:100])
    check("导出响应带 nosniff",
          r.headers.get("X-Content-Type-Options") == "nosniff")
    check("导出响应 no-store", "no-store" in (r.headers.get("Cache-Control") or ""))
    exp_pkg = r.content
    check("导出内容非空", len(exp_pkg) > 200, f"{len(exp_pkg)} 字节")

    # 导出的 .kqb 必须能被自家核对器通过（闭环）
    exp_result = KP.inspect_package(exp_pkg)
    check("导出的 .kqb 通过核对（闭环）", exp_result.ok,
          "；".join(f"[{c.title}]{c.detail}" for c in exp_result.failures))
    exp_bank = exp_result.bank_obj or {}
    check("导出题库为 v3.1.0", exp_bank.get("format_version") == [3, 1, 0],
          str(exp_bank.get("format_version")))
    check("导出题库含 1 题", exp_result.summary()["bank_summary"]["item_count"] == 1,
          str(exp_result.summary()["bank_summary"]["item_count"]))
    check("导出题库含正确答案（与练习载荷不同）",
          exp_result.bank_obj["chapters"][0]["content"][0].get("correct_answers") == ["A"],
          str(exp_result.bank_obj["chapters"][0]["content"][0].get("correct_answers")))
    check("导出题库已重签名（valid_key 非空）",
          bool(exp_result.bank_obj.get("valid_key")))
    check("导出题库带溯源元信息",
          exp_result.bank_obj.get("exported_by") == "kaleido_mini",
          str(exp_result.bank_obj.get("exported_by")))
    check("导出包只含被引用的媒体（1 个）",
          len(exp_result.media) == 1, str(sorted(exp_result.media)))

    # (b) 附 Markdown/HTML → 多文件打成 zip
    r = admin.request("POST", export_path(SYNTH_FN), json={
        "scope": "wrong",
        "records": {item_id: {"verdict": "wrong", "answer": "B"}},
        "pack": "auto", "extras": ["md", "html"],
    })
    check("导出（含错题本）200", r.status_code == 200, f"HTTP {r.status_code}")
    cd2 = r.headers.get("Content-Disposition") or ""
    check("多文件时打成 .zip", ".zip" in cd2, cd2[:100])
    import io as _io
    import zipfile as _zipfile
    with _zipfile.ZipFile(_io.BytesIO(r.content)) as zf:
        names = zf.namelist()
        md_text = zf.read([n for n in names if n.endswith(".md")][0]).decode("utf-8")
        html_text = zf.read([n for n in names if n.endswith(".html")][0]).decode("utf-8")
    check("zip 含 .kqb/.md/.html 三件", len(names) == 3 and
          any(n.endswith(".kqb") for n in names)
          and any(n.endswith(".md") for n in names)
          and any(n.endswith(".html") for n in names), str(names))
    check("Markdown 含正确答案", "正确答案" in md_text)
    check("Markdown 含我的作答", "我的作答" in md_text)
    check("HTML 自包含无外部脚本",
          html_text.lstrip().startswith("<!DOCTYPE html>") and "<script" not in html_text.lower())
    check("HTML 内联了图片为 data URI", "data:image/" in html_text,
          "含 data URI" if "data:image/" in html_text else "未内联")

    # (c) 无媒体的题库 → auto 应出 .json
    PLAIN_FN = "样例题库.json"
    r = admin.request("POST", export_path(PLAIN_FN), json={
        "scope": "items", "items": ["c1:q1", "c1:q2"], "pack": "auto",
    })
    check("导出（无媒体）200", r.status_code == 200, f"HTTP {r.status_code}")
    cd3 = r.headers.get("Content-Disposition") or ""
    check("无媒体时出 .json", ".json" in cd3, cd3[:100])
    plain_bank = json.loads(r.content.decode("utf-8"))
    check("无媒体导出为 v3.1.0", plain_bank.get("format_version") == [3, 1, 0])
    check("无媒体导出含 2 题",
          sum(len(c["content"]) for c in plain_bank["chapters"]) == 2,
          str(sum(len(c["content"]) for c in plain_bank["chapters"])))

    # (d) 含媒体却强制 json → 400
    r = admin.request("POST", export_path(SYNTH_FN), json={
        "scope": "wrong",
        "records": {item_id: {"verdict": "wrong"}},
        "pack": "json",
    })
    check("含媒体却 pack=json → 400", r.status_code == 400, f"HTTP {r.status_code}")

    # (e) 分离密钥 → 包里带 .key_obj.json
    r = admin.request("POST", export_path(SYNTH_FN), json={
        "scope": "wrong",
        "records": {item_id: {"verdict": "wrong"}},
        "placement": "detached",
    })
    check("detached 导出 200", r.status_code == 200, f"HTTP {r.status_code}")
    with _zipfile.ZipFile(_io.BytesIO(r.content)) as zf:
        dnames = zf.namelist()
    check("detached 导出含 .key_obj.json",
          any(n.endswith(".key_obj.json") for n in dnames), str(dnames))
    check("detached 导出含 .kqb", any(n.endswith(".kqb") for n in dnames), str(dnames))

    # (f) 空错题 → 400
    r = admin.request("POST", export_path(SYNTH_FN), json={"scope": "wrong", "records": {}})
    check("无错题时导出 → 400", r.status_code == 400, f"HTTP {r.status_code}")

    # (g) 未登录不能导出
    st, _ = anon.api_json("POST", export_path(SYNTH_FN),
                          json={"scope": "wrong", "records": {item_id: {"verdict": "wrong"}}})
    check("未登录导出 → 401", st == 401, f"HTTP {st}")

    # ---------------- 7. 学科列表 ----------------
    st, body = admin.api_json("GET", PLUGIN_API + "/subjects")
    check("学科列表 200", st == 200 and code_of(body) == 200, f"HTTP {st}")
    subjects = (body.get("data") or {}).get("subjects") or []
    mine = [s for s in subjects if s["name"] == SUBJECT]
    check("列表含测试学科", len(mine) == 1, str([s["name"] for s in subjects]))
    if mine:
        check("测试学科下题库数 ≥ 2", mine[0]["bank_count"] >= 2,
              str(mine[0]["bank_count"]))
        check("学科统计含可练习题数", (mine[0].get("item_count") or 0) > 0,
              str(mine[0].get("item_count")))

    # ---- 统计数字必须是真实数字（回归：曾经渲染出 dict.items 方法对象）----
    import re as _re
    page_html = admin.get(PLUGIN_PAGE).text
    # 注意 \s* ：第 5 个统计卡片的 <b> 在换行缩进之后，不加 \s* 会漏匹配
    stat_values = _re.findall(
        r'<div class="km-stat[^"]*">\s*<b>([^<]*)</b>', page_html)
    check("统计卡片取到 5 个数值", len(stat_values) == 5, str(stat_values))
    check("统计数值全为整数（无方法对象泄漏）",
          all(v.strip().isdigit() for v in stat_values), str(stat_values))
    if mine:
        item_stat = stat_values[2] if len(stat_values) >= 3 else ""
        expected = sum(s.get("item_count") or 0 for s in subjects)
        check("可练习题数值与服务端统计一致",
              item_stat.strip() == str(expected), f"页面 {item_stat} vs 期望 {expected}")

    # ---------------- 8. 普通用户权限分级 ----------------
    st, body = admin.api_json(
        "POST", "/api/user_manage/create",
        json={"username": TEST_USER, "password": TEST_PASS, "role": "user",
              "nickname": "端到端测试用户"})
    # 首次创建 200；已存在时 user_manage 返回 400 且消息含「已存在」——两种都视为就绪
    already = st == 400 and "已存在" in str(body.get("message"))
    check("创建普通用户（或已存在）",
          (st == 200 and code_of(body) == 200) or already,
          f"HTTP {st} {body.get('message')}")

    user = Client()
    ok, r = user.login(TEST_USER, TEST_PASS)
    check("普通用户登录成功", ok, f"HTTP {r.status_code}")

    st, meta_u = user.api_json("GET", PLUGIN_API + "/meta")
    du = meta_u.get("data") or {}
    check("普通用户 meta 角色 = user", du.get("role") == "user", str(du.get("role")))
    check("普通用户 can_manage=False", du.get("can_manage") is False)

    st, _ = user.api_json("GET", PLUGIN_API + "/subjects")
    check("普通用户可读学科列表（200）", st == 200, f"HTTP {st}")

    st, _ = user.api_json("GET", PLUGIN_API + "/qbanks/" + quote(SUBJECT) + "/"
                          + quote("样例题库.json"))
    check("普通用户可读题库详情（200）", st == 200, f"HTTP {st}")

    r = user.get(PLUGIN_PAGE)
    check("普通用户可访问主页面（200）", r.status_code == 200, f"HTTP {r.status_code}")
    check("普通用户页面不渲染上传按钮", 'data-act="upload-json"' not in r.text)
    check("普通用户页面不渲染删除按钮", 'data-act="delete-bank"' not in r.text)
    check("普通用户页面显示权限提示", "需管理员权限" in r.text)

    # 越权写操作必须 403
    st, _ = user.api_json("POST", PLUGIN_API + "/subjects", json={"name": "越权学科"})
    check("普通用户建学科被拒（403）", st == 403, f"HTTP {st}")

    st, _ = user.api_json(
        "POST", PLUGIN_API + "/qbanks",
        data={"subject": SUBJECT},
        files={"file": ("x.json", raw, "application/json")})
    check("普通用户上传题库被拒（403）", st == 403, f"HTTP {st}")

    st, _ = user.api_json("DELETE", PLUGIN_API + "/qbanks/" + quote(SUBJECT) + "/"
                          + quote("样例题库.json"))
    check("普通用户删题库被拒（403）", st == 403, f"HTTP {st}")

    st, _ = user.api_json("DELETE", PLUGIN_API + "/subjects/" + quote(SUBJECT) + "?force=1")
    check("普通用户删学科被拒（403）", st == 403, f"HTTP {st}")

    st, _ = user.api_json(
        "POST", PLUGIN_API + "/packages/inspect",
        data={"subject": SUBJECT},
        files={"file": ("x.kqb", b"dummy", "application/octet-stream")})
    check("普通用户整包预检被拒（403）", st == 403, f"HTTP {st}")

    # 普通用户可正常练习（练习属 user 级）
    st, body = user.api_json("GET", practice_path)
    check("普通用户可拉练习载荷（200）", st == 200 and code_of(body) == 200, f"HTTP {st}")
    st, body = user.api_json("POST", grade_path, json={"item_id": item_id, "answer": "A"})
    check("普通用户可判分（200）", st == 200
          and (body.get("data") or {}).get("verdict") == "correct",
          f"HTTP {st} verdict={str((body.get('data') or {}).get('verdict'))}")
    # 普通用户可导出自己的错题，但不得导出整库
    r = user.request("POST", export_path(SYNTH_FN), json={
        "scope": "wrong", "records": {item_id: {"verdict": "wrong"}}})
    check("普通用户可导出自己的错题（200）", r.status_code == 200, f"HTTP {r.status_code}")
    r = user.request("POST", export_path(SYNTH_FN), json={"scope": "all"})
    check("普通用户导出整库被拒（403）", r.status_code == 403, f"HTTP {r.status_code}")
    r = admin.request("POST", export_path(SYNTH_FN), json={"scope": "all", "pack": "kqb"})
    check("管理员可导出整库（200）", r.status_code == 200, f"HTTP {r.status_code}")
    check("普通用户练习页可访问（200）",
          user.get("{page}/exam/{subj}/{fn}".format(
              page=PLUGIN_PAGE, subj=quote(SUBJECT), fn=quote(SYNTH_FN))).status_code == 200)

    # 普通用户仍可读媒体
    if os.path.isfile(quiz_bank):
        r = user.get("{api}/qbanks/{subj}/{fn}/media/{name}".format(
            api=PLUGIN_API, subj=quote(SUBJECT), fn=quote("随堂测试.json"),
            name=quote("0.png")))
        check("普通用户可读媒体（200）", r.status_code == 200, f"HTTP {r.status_code}")

    # ---------------- 8b. 设置页与密钥管理（M5）----------------
    # 注意：本段 [绝不覆写真实 API Key]，只做 __KEEP__ 语义与展示层校验
    st, body = admin.api_json("GET", PLUGIN_API + "/settings")
    check("管理员读取设置 200", st == 200 and code_of(body) == 200, f"HTTP {st}")
    cfg = body.get("data") or {}
    ds = cfg.get("deepseek") or {}
    check("设置回传不含完整 key",
          "api_key" not in ds, str(sorted(ds.keys())))
    check("设置回传 key 为掩码", ds.get("api_key_masked", "").find("****") > 0
          or ds.get("configured") is False, str(ds.get("api_key_masked")))
    check("设置含密钥文件状态", "key" in cfg and "configured" in (cfg.get("key") or {}))
    check("设置含限额信息", "limits" in cfg)
    original_model = ds.get("model") or "deepseek-chat"
    original_base = ds.get("base_url") or "https://api.deepseek.com"
    key_configured_before = bool(ds.get("configured"))
    mask_before = ds.get("api_key_masked") or ""

    # __KEEP__ 语义：改模型/外链策略但不动 key
    st, body = admin.api_json("POST", PLUGIN_API + "/settings", json={
        "allow_external_media": True,
        "deepseek": {"enabled": bool(ds.get("enabled")), "model": original_model,
                     "base_url": original_base, "api_key": "__KEEP__"},
    })
    check("保存设置（__KEEP__）200", st == 200 and code_of(body) == 200,
          f"HTTP {st} {body.get('message')}")
    ds2 = ((body.get("data") or {}).get("deepseek")) or {}
    check("__KEEP__ 不改变 key 配置状态",
          bool(ds2.get("configured")) == key_configured_before,
          f"{key_configured_before} -> {ds2.get('configured')}")
    if key_configured_before:
        check("__KEEP__ 不改变 key 掩码", ds2.get("api_key_masked") == mask_before,
              f"{mask_before} -> {ds2.get('api_key_masked')}")

    # meta 里的 AI 可用性
    st, body = admin.api_json("GET", PLUGIN_API + "/meta")
    ai = ((body.get("data") or {}).get("ai")) or {}
    check("meta 含 AI 可用性字段", "available" in ai and "configured" in ai, str(ai))
    check("meta 的 ai 不含 key", "api_key" not in json.dumps(ai))
    check("meta 的 available 与 configured/enabled 一致",
          ai.get("available") == bool(ai.get("enabled") and ai.get("configured")),
          str(ai))

    # grade-ai 的静态校验（不产生真实调用）
    # 合成题库里 c1:q1 是单选题，用它验证题型拦截
    st, body = admin.api_json("POST", grade_path.replace("/grade", "/grade-ai"),
                              json={"item_id": item_id, "answer": "A"})
    check("grade-ai 对选择题返回 400（或未启用 400）", st == 400,
          f"HTTP {st} {body.get('message')}")
    st, body = admin.api_json("POST", grade_path.replace("/grade", "/grade-ai"), json={})
    check("grade-ai 缺 item_id 返回 400", st == 400, f"HTTP {st}")

    # 设置页
    r = admin.get(PLUGIN_PAGE + "/settings")
    check("设置页（管理员）200", r.status_code == 200, f"HTTP {r.status_code}")
    check("设置页含密钥区块", "全题库密钥文件" in r.text)
    check("设置页含 AI 配置区块", "DeepSeek 智能评卷" in r.text)
    check("设置页引用了 km_settings.js",
          "/plugin-static/kaleido_mini/js/km_settings.js" in r.text)
    check("设置页不含明文 key（仅掩码占位）",
          "sk-" not in r.text or "****" in r.text)
    r = user.get(PLUGIN_PAGE + "/settings")
    check("设置页（普通用户）403", r.status_code == 403, f"HTTP {r.status_code}")
    st, _ = user.api_json("GET", PLUGIN_API + "/settings")
    check("普通用户读设置 API 403", st == 403, f"HTTP {st}")
    st, _ = user.api_json("POST", PLUGIN_API + "/settings", json={})
    check("普通用户写设置 API 403", st == 403, f"HTTP {st}")
    st, _ = user.api_json("POST", PLUGIN_API + "/crypto/key", json={"generate": True})
    check("普通用户生成密钥 403", st == 403, f"HTTP {st}")

    # 密钥文件管理：生成 → 状态 → 下载
    st, body = admin.api_json("POST", PLUGIN_API + "/crypto/key",
                              json={"generate": True, "overwrite": True})
    check("管理员生成密钥 200", st == 200 and code_of(body) == 200,
          f"HTTP {st} {body.get('message')}")
    check("生成后返回指纹", bool((body.get("data") or {}).get("fingerprint_short")),
          str((body.get("data") or {}).get("fingerprint_short")))
    check("生成后提示备份", "备份" in str((body.get("data") or {}).get("warning")),
          str((body.get("data") or {}).get("warning"))[:40])
    st, body = admin.api_json("GET", PLUGIN_API + "/crypto/key")
    check("密钥状态显示已配置", (body.get("data") or {}).get("configured") is True)
    check("密钥状态含 RSA 位数", (body.get("data") or {}).get("rsa_bits") == 2048,
          str((body.get("data") or {}).get("rsa_bits")))
    r = admin.get(PLUGIN_API + "/crypto/key/download")
    check("下载密钥 200", r.status_code == 200, f"HTTP {r.status_code}")
    check("下载密钥带保管警告头",
          r.headers.get("X-Kaleido-Warning") == "master-key-handle-with-care",
          str(r.headers.get("X-Kaleido-Warning")))
    check("下载密钥 no-store", "no-store" in (r.headers.get("Cache-Control") or ""))
    try:
        downloaded = json.loads(r.content.decode("utf-8"))
        check("下载内容是合法密钥文件",
              sorted(downloaded.keys()) == sorted(["SYMMETRIC_KEY",
                                                   "ENCRYPTED_PRIVATE_KEY",
                                                   "iv", "PUBLIC_KEY"]),
              str(sorted(downloaded.keys())))
    except Exception as exc:  # noqa: BLE001
        check("下载内容是合法密钥文件", False, str(exc))

    # 真实 AI 调用（默认关闭，避免每次回归花钱）：设 KM_E2E_AI=1 才跑
    if os.environ.get("KM_E2E_AI") == "1" and key_configured_before:
        st, body = admin.api_json("POST", PLUGIN_API + "/settings/test-ai", json={})
        check("真实 AI 连通性自检 200", st == 200, f"HTTP {st} {body.get('message')}")
        st, body = admin.api_json(
            "POST", grade_path.replace("/grade", "/grade-ai"),
            json={"item_id": item_id, "answer": "测试作答"})
        check("真实 AI 评卷返回分数",
              st == 200 and (body.get("data") or {}).get("score") is not None,
              f"HTTP {st} score={str((body.get('data') or {}).get('score'))}")
    else:
        print("[SKIP] 真实 AI 调用（设 KM_E2E_AI=1 可启用）")

    # ---------------- 9. 删除题库（连带媒体）----------------
    if os.path.isfile(quiz_bank):
        st, body = admin.api_json(
            "DELETE", PLUGIN_API + "/qbanks/" + quote(SUBJECT) + "/" + quote("随堂测试.json"))
        check("管理员删题库 200", st == 200 and code_of(body) == 200, f"HTTP {st}")
        check("删除连带媒体 5 个", (body.get("data") or {}).get("removed_media") == 5,
              str((body.get("data") or {}).get("removed_media")))
        r = admin.get("{api}/qbanks/{subj}/{fn}".format(
            api=PLUGIN_API, subj=quote(SUBJECT), fn=quote("随堂测试.json")))
        check("删除后详情 404", r.status_code == 404, f"HTTP {r.status_code}")

    # ---------------- 10. 删学科（非空需 force）----------------
    st, body = admin.api_json("DELETE", PLUGIN_API + "/subjects/" + quote(SUBJECT))
    check("非空学科不带 force 返回 409", st == 409, f"HTTP {st}")
    st, body = admin.api_json("DELETE", PLUGIN_API + "/subjects/" + quote(SUBJECT) + "?force=1")
    check("带 force 删学科 200", st == 200 and code_of(body) == 200,
          f"HTTP {st} {body.get('message')}")

    st, body = admin.api_json("GET", PLUGIN_API + "/subjects")
    subjects = (body.get("data") or {}).get("subjects") or []
    check("删学科后列表不再含它", not [s for s in subjects if s["name"] == SUBJECT],
          str([s["name"] for s in subjects]))

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
