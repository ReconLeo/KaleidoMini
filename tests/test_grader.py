# -*- coding: utf-8 -*-
"""M5 回归：km_grader —— DeepSeek 评卷客户端（离线，用注入的假 transport）

重点断言（安全与健壮性）：
  * **API Key 绝不外泄**：不出现在错误信息、结果对象、to_dict 里
  * 未配置 key → GraderNotConfigured
  * 网络失败 / 非 200 / 非 JSON / 空内容 → 分类异常，且信息已脱敏
  * 模型输出容错：带 ```json 围栏、多余文字、score 为字符串、verdict 缺失
  * prompt 携带题型/题干/参考答案/考生作答/解析，且超长会截断

运行： python tests/test_grader.py
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from kaleido_mini import km_grader as G  # noqa: E402

#: 假 key —— 与真实 key 前缀相同，便于验证掩码规则（不是真实凭据）
FAKE_KEY = "sk-0000000000000000000000000000abcd"

RESULTS = []


def check(name, cond, detail="", extra=""):
    RESULTS.append((name, bool(cond), detail))
    shown = extra or detail
    line = f"[{'PASS' if cond else 'FAIL'}] {name}"
    if shown:
        line += (f"  ({shown})" if cond else f"  << {shown}")
    print(line)
    return bool(cond)


def make_transport(status=200, content='{"score": 85, "verdict": "correct", "comment": "要点齐全"}',
                   *, raise_exc=None, raw_body=None):
    """构造假 transport，并记录被调用的 URL/headers/payload。"""
    captured = {}

    def transport(url, headers, payload, timeout):
        captured["url"] = url
        captured["headers"] = dict(headers)
        captured["payload"] = payload
        captured["timeout"] = timeout
        if raise_exc:
            raise raise_exc
        if raw_body is not None:
            return status, raw_body
        return status, json.dumps({
            "model": "deepseek-chat",
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150},
        }, ensure_ascii=False)

    return transport, captured


def main():
    print("=" * 78)
    print("M5 / km_grader 回归")
    print("=" * 78)

    # ---------------- 1. 掩码与脱敏 ----------------
    check("mask_key 保留前缀与后四位", G.mask_key(FAKE_KEY) == "sk-****abcd",
          G.mask_key(FAKE_KEY))
    check("mask_key 不含原始 key", FAKE_KEY not in G.mask_key(FAKE_KEY))
    check("mask_key 短串全遮蔽", G.mask_key("abc") == "***", G.mask_key("abc"))
    check("mask_key 空值返回空", G.mask_key("") == "")
    leaked = G.redact(f"出错了 key={FAKE_KEY} 结束", FAKE_KEY)
    check("redact 替换掉 key", FAKE_KEY not in leaked and "sk-****abcd" in leaked, leaked)

    # ---------------- 2. 未配置 ----------------
    empty = G.DeepSeekGrader("")
    check("空 key 标记未配置", empty.configured is False)
    try:
        empty.grade(question="q", reference="r", user_answer="u")
        check("未配置时 grade 应抛异常", False)
    except G.GraderNotConfigured:
        check("未配置抛 GraderNotConfigured", True)

    # ---------------- 3. 正常评卷 ----------------
    transport, captured = make_transport()
    grader = G.DeepSeekGrader(FAKE_KEY, transport=transport)
    check("有 key 标记已配置", grader.configured is True)
    result = grader.grade(
        question="简述急性心肌梗死的治疗原则",
        reference="再灌注治疗、抗血小板、抗凝、调脂、β受体阻滞剂",
        user_answer="尽快再灌注，用阿司匹林",
        qtype_label="essay",
        analysis="重点是再灌注时间窗",
    )
    check("返回 score=85", result.score == 85, str(result.score))
    check("返回 verdict=correct", result.verdict == "correct", result.verdict)
    check("返回点评", "要点齐全" in result.comment, result.comment)
    check("返回模型名", result.model == "deepseek-chat", result.model)
    check("返回 token 用量", result.usage.get("total_tokens") == 150, str(result.usage))

    # 请求侧
    check("请求 URL 指向 /chat/completions",
          captured["url"] == "https://api.deepseek.com/chat/completions", captured["url"])
    check("请求头带 Bearer",
          captured["headers"].get("Authorization") == f"Bearer {FAKE_KEY}")
    check("请求体含 model", captured["payload"].get("model") == "deepseek-chat")
    check("请求 temperature 低（求稳定）", captured["payload"].get("temperature") == 0.1)
    msgs = captured["payload"].get("messages") or []
    check("两条消息（system+user）", len(msgs) == 2, str(len(msgs)))
    user_msg = msgs[1]["content"]
    check("prompt 含题干", "急性心肌梗死" in user_msg)
    check("prompt 含参考答案", "再灌注治疗" in user_msg)
    check("prompt 含考生作答", "尽快再灌注" in user_msg)
    check("prompt 含解析", "再灌注时间窗" in user_msg)
    check("prompt 含题型提示", "简答题" in user_msg, user_msg[:40])

    # ---------------- 4. 结果对象不含 key ----------------
    dumped = json.dumps(result.to_dict(), ensure_ascii=False)
    check("GradeResult 不含 key", FAKE_KEY not in dumped)
    check("GradeResult.to_dict 字段齐备",
          set(result.to_dict()) == {"score", "verdict", "comment", "model", "usage"},
          str(sorted(result.to_dict())))
    check("masked_key 可用且不含原文",
          grader.masked_key() == "sk-****abcd" and FAKE_KEY not in grader.masked_key())

    # ---------------- 5. 输出容错 ----------------
    t2, _ = make_transport(content='```json\n{"score": 62, "verdict": "partial", "comment": "漏了抗凝"}\n```')
    r2 = G.DeepSeekGrader(FAKE_KEY, transport=t2).grade(
        question="q", reference="r", user_answer="u")
    check("容错：```json 围栏", r2.score == 62 and r2.verdict == "partial",
          f"{r2.score}/{r2.verdict}")

    t3, _ = make_transport(content='好的，评分如下：{"score": "70", "comment": "尚可"}')
    r3 = G.DeepSeekGrader(FAKE_KEY, transport=t3).grade(
        question="q", reference="r", user_answer="u")
    check("容错：多余文字 + 字符串分数", r3.score == 70, str(r3.score))
    check("容错：缺 verdict 时按分数推导", r3.verdict == "partial", r3.verdict)

    t4, _ = make_transport(content='{"score": 190, "verdict": "correct", "comment": "ok"}')
    r4 = G.DeepSeekGrader(FAKE_KEY, transport=t4).grade(
        question="q", reference="r", user_answer="u")
    check("容错：分数越界被夹到 100", r4.score == 100, str(r4.score))

    t5, _ = make_transport(content='{"score": -5, "verdict": "weird", "comment": "ok"}')
    r5 = G.DeepSeekGrader(FAKE_KEY, transport=t5).grade(
        question="q", reference="r", user_answer="u")
    check("容错：负分夹到 0", r5.score == 0, str(r5.score))
    check("容错：非法 verdict 按分数推导", r5.verdict == "incorrect", r5.verdict)

    t6, _ = make_transport(content="这道题回答得不错，覆盖了主要要点。")
    r6 = G.DeepSeekGrader(FAKE_KEY, transport=t6).grade(
        question="q", reference="r", user_answer="u")
    check("容错：非 JSON 输出降级为点评", r6.score is None and "不错" in r6.comment,
          f"score={r6.score}")

    # ---------------- 6. 错误路径（且已脱敏） ----------------
    def expect_error(exc_type, label, **kw):
        t, _ = make_transport(**kw)
        try:
            G.DeepSeekGrader(FAKE_KEY, transport=t).grade(
                question="q", reference="r", user_answer="u")
            check(label, False, "没有抛异常")
        except exc_type as exc:
            check(label, True, type(exc).__name__)
            check(label + "：信息不含 key", FAKE_KEY not in str(exc), str(exc)[:80])
        except Exception as exc:  # noqa: BLE001
            check(label, False, f"抛了 {type(exc).__name__}")

    expect_error(G.GraderResponseError, "非 200 → GraderResponseError", status=401,
                 raw_body='{"error":"invalid api key"}')
    expect_error(G.GraderNetworkError, "传输抛异常 → GraderNetworkError",
                 raise_exc=RuntimeError("boom"))
    expect_error(G.GraderResponseError, "响应非 JSON → GraderResponseError",
                 raw_body="<html>502</html>")
    expect_error(G.GraderResponseError, "choices 为空 → GraderResponseError",
                 raw_body=json.dumps({"choices": []}))
    expect_error(G.GraderResponseError, "content 为空 → GraderResponseError",
                 raw_body=json.dumps({"choices": [{"message": {"content": ""}}]}))

    # 401 的错误体里若回显了 key，也必须被脱敏
    t_leak, _ = make_transport(status=401, raw_body=f'{{"error":"bad key {FAKE_KEY}"}}')
    try:
        G.DeepSeekGrader(FAKE_KEY, transport=t_leak).grade(
            question="q", reference="r", user_answer="u")
    except G.GraderResponseError as exc:
        check("服务端回显 key 也会被脱敏", FAKE_KEY not in str(exc), str(exc)[:100])

    # ---------------- 7. 超长输入截断 ----------------
    t7, cap7 = make_transport()
    long_text = "字" * (G.MAX_PROMPT_FIELD + 500)
    G.DeepSeekGrader(FAKE_KEY, transport=t7).grade(
        question=long_text, reference="r", user_answer="u")
    user_content = cap7["payload"]["messages"][1]["content"]
    check("超长题干被截断", len(user_content) < G.MAX_PROMPT_FIELD + 1000,
          str(len(user_content)))
    check("截断有标记", "已截断" in user_content)

    # ---------------- 8. 自定义 base_url / model / timeout ----------------
    t8, cap8 = make_transport()
    G.DeepSeekGrader(FAKE_KEY, base_url="https://proxy.example.com/v1/",
                     model="deepseek-reasoner", timeout=7,
                     transport=t8).grade(question="q", reference="r", user_answer="u")
    check("自定义 base_url 去掉尾斜杠",
          cap8["url"] == "https://proxy.example.com/v1/chat/completions", cap8["url"])
    check("自定义 model 生效", cap8["payload"]["model"] == "deepseek-reasoner")
    check("自定义 timeout 生效", cap8["timeout"] == 7, str(cap8["timeout"]))
    check("timeout 越界被夹", G.DeepSeekGrader(FAKE_KEY, timeout=9999).timeout == 120,
          str(G.DeepSeekGrader(FAKE_KEY, timeout=9999).timeout))

    # ---------------- 9. 填空题提示 ----------------
    t9, cap9 = make_transport()
    G.DeepSeekGrader(FAKE_KEY, transport=t9).grade(
        question="中国的首都是____。", reference="北京", user_answer="北京",
        qtype_label="gap")
    check("填空题 prompt 用填空口径", "填空题" in cap9["payload"]["messages"][1]["content"],
          cap9["payload"]["messages"][1]["content"][:30])

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
